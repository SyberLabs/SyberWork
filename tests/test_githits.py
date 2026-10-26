"""``syberlabs.githits``: the GitHits API client, its contamination control, its records and its CLI.

Every call here goes to a loopback fake GitHits (``FakeGitHits``) that serves the five operations with response
bodies valid under the pinned OpenAPI fixture, ``X-Request-ID`` and the rate-limit headers. No test reaches the
real API and no real token exists: each test makes a random one and checks where it went.

The fixture ``tests/fixtures/githits_openapi_v1.json`` is ``fixture_bytes`` of the public document fetched once
from https://api.githits.dev/v1/openapi.json on 2026-10-26 (1,364,570 bytes, SHA-256 ``UPSTREAM_SHA256``): the five
operations this client calls with every component they reference, values unchanged, as compact sorted ASCII JSON.
The whole document is over the change-size limit of the contract this repository is built under, so only that
closed subset is pinned. ``SpecPin`` fails when the fixture changes, and when the client sends or reads a field
the fixture does not have.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from syberlabs import cli, githits
from syberlabs.canonical import canonical

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "githits_openapi_v1.json"
FIXTURE_SHA256 = "c388ab7e53a25fdfbd914c10634d799ae923b01ab8880750c750e775c21f1b9c"
UPSTREAM_SHA256 = "e58048e3e56d7b0d6346e24722614d2d8907748960f18f18204895a354ab8a6a"
ENV_NAMES = (githits.TOKEN_ENV, githits.URL_ENV, githits.SESSION_ENV, githits.AGENT_ENV, githits.EXCLUDE_ENV,
             githits.LICENSES_ENV, githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV)


# ----------------------------------------------------------------------------------------------- the pinned contract

def _refs(node, found: set) -> set:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            found.add(node["$ref"])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)
    return found


def openapi_subset(spec: dict) -> dict:
    """The client's five operations of an OpenAPI document with every component they reference, transitively."""
    paths = {}
    for operation in githits.OPERATIONS.values():
        paths.setdefault(operation.path, {})[operation.method.lower()] = spec["paths"][operation.path][operation.method.lower()]
    wanted, pending = set(), _refs(paths, set())
    while pending:
        ref = pending.pop()
        if ref in wanted:
            continue
        wanted.add(ref)
        _refs(_resolve(spec, ref), pending)
    components = {"securitySchemes": spec["components"]["securitySchemes"]}
    for ref in sorted(wanted):
        _, _, kind, name = ref.split("/")
        components.setdefault(kind, {})[name] = spec["components"][kind][name]
    return {"openapi": spec["openapi"], "info": spec["info"], "servers": spec["servers"], "paths": paths,
            "components": components}


def fixture_bytes(spec: dict) -> bytes:
    return json.dumps(openapi_subset(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _resolve(spec: dict, ref: str):
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def load_spec() -> dict:
    return json.loads(FIXTURE.read_bytes().decode("ascii"))


def problems(spec: dict, schema: dict, value, where: str = "$") -> list[str]:
    """What makes ``value`` invalid under ``schema``: the parts of JSON Schema the fixture uses for these shapes."""
    if "$ref" in schema:
        return problems(spec, _resolve(spec, schema["$ref"]), value, where)
    found = []
    for part in schema.get("allOf", []):
        found += problems(spec, part, value, where)
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            attempts = [problems(spec, part, value, where) for part in schema[keyword]]
            if not any(not attempt for attempt in attempts):
                found.append(f"{where}: matches no {keyword} alternative ({min(attempts, key=len)[:2]})")
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds or []
    actual = ("null" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int)
              else "number" if isinstance(value, float) else "string" if isinstance(value, str)
              else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "?")
    if kinds and actual not in kinds and not (actual == "integer" and "number" in kinds):
        return found + [f"{where}: {actual} is not {kinds}"]
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        found.append(f"{where}: {value!r} is not {schema['const']!r}")
    if actual in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
            found.append(f"{where}: {value} is out of bounds")
    if actual == "string" and ("maxLength" in schema and len(value) > schema["maxLength"]
                               or "minLength" in schema and len(value) < schema["minLength"]):
        found.append(f"{where}: length {len(value)} is out of bounds")
    if actual == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            found.append(f"{where}: more than {schema['maxItems']} items")
        for index, item in enumerate(value):
            found += problems(spec, schema.get("items", {}), item, f"{where}[{index}]")
    if actual == "object":
        properties = schema.get("properties", {})
        found += [f"{where}: missing {key}" for key in schema.get("required", []) if key not in value]
        if schema.get("additionalProperties") is False:
            found += [f"{where}: unknown {key}" for key in value if key not in properties]
        for key, item in value.items():
            if key in properties:
                found += problems(spec, properties[key], item, f"{where}.{key}")
    return found


def has_path(spec: dict, schema: dict, path: tuple) -> bool:
    """True when the field ``path`` (``[]`` for an array's items) exists in ``schema`` through some alternative."""
    if "$ref" in schema:
        return has_path(spec, _resolve(spec, schema["$ref"]), path)
    if not path:
        return True
    for keyword in ("allOf", "oneOf", "anyOf"):
        if any(has_path(spec, part, path) for part in schema.get(keyword, [])):
            return True
    head, rest = path[0], path[1:]
    if head == "[]":
        return "items" in schema and has_path(spec, schema["items"], rest)
    return head in schema.get("properties", {}) and has_path(spec, schema["properties"][head], rest)


def operation_of(spec: dict, op: str) -> dict:
    operation = githits.OPERATIONS[op]
    return spec["paths"][operation.path][operation.method.lower()]


def response_schema(spec: dict, op: str) -> dict:
    return operation_of(spec, op)["responses"]["200"]["content"]["application/json"]["schema"]


# ------------------------------------------------------------------------------------------- responses the fake serves

LOCATOR_FIELDS = ("commit_sha", "docs_read_target", "end_line", "evidence_range", "file_path", "git_ref", "match_line",
                  "package_name", "page_id", "registry", "repo_url", "repository_file_path", "requested_ref",
                  "source_kind", "source_url", "start_line", "symbol_ref", "version")
IDENTITY_FIELDS = ("commit_sha", "git_ref", "kind", "registry", "repo_url", "version")


def search_hit(title: str, repo_url=None, **locator) -> dict:
    return {"coverage": None, "fresh_target_label": None, "freshness": None, "id": f"hit-{title}",
            "locator": {**{key: None for key in LOCATOR_FIELDS}, "repo_url": repo_url, "file_path": f"src/{title}.py",
                        "start_line": 10, "end_line": 20, **locator},
            "requested_target_label": None, "result_type": "code", "score": 0.875, "served_target_label": None,
            "summary": f"How {title} works.", "target_label": "github:psf/requests", "title": title}


def search_response(hits: list, has_more: bool = False) -> dict:
    return {"completed": True, "expires_at": None, "query_warnings": [], "search_ref": "sr_123",
            "selected_fields": ["results"], "status": "complete", "targets": None,
            "result": {"evidence_notice": None, "partial_results": False, "query": "q", "query_warnings": [],
                       "page": {"has_more": has_more, "limit": 5, "offset": 0, "returned": len(hits)},
                       "results": hits, "source_status": [], "sources": ["code"]}}


def example_response(markdown: str) -> dict:
    return {"result": markdown, "search_status": "complete", "solution_id": "6f1c1a52-6a43-4cf7-9d3b-2f7f6f2b9a10"}


def identity(repo_url=None, extra: str = "name") -> dict:
    return {**{key: None for key in IDENTITY_FIELDS}, extra: None, "repo_url": repo_url}


def read_code_response(repo_url: str, body: str = "def get(url):\n    return url\n") -> dict:
    return {"kind": "code", "code_index_state": "ready", "indexing_ref": None, "is_binary": False,
            "content": {"body": body, "start_line": 1, "end_line": 2}, "metadata": {"language": "Python",
                                                                                   "total_lines": 2},
            "path": "src/api.py", "repository_file_path": "src/api.py",
            "target_resolution": {"freshness": None, "freshness_reason": None, "requested": identity(repo_url),
                                  "resolved_requested": identity(repo_url), "served": identity(repo_url)}}


def read_doc_response(body: str, url: str = "https://requests.readthedocs.io/en/latest/") -> dict:
    return {"kind": "documentation", "docs_read_target": "doc_1", "id": "doc_1",
            "content": {"anchor": None, "body": body, "end_line": 2, "start_line": 1, "total_lines": 2},
            "metadata": {"base_url": None, "breadcrumbs": [], "content_format": "markdown", "last_updated_at": None,
                         "owner": None, "requested_ref": None, "source_label": None, "title": "Quickstart"},
            "source": {"kind": "site", "url": url}}


def list_entry(path: str, read_target: str = "github:psf/requests") -> dict:
    return {"browse": None, "byte_size": 10, "content_hash": None, "file_type": "source", "intent": "production",
            "kind": "file", "language": "Python", "line_count": 12, "path": path,
            "read": {"path": path, "target": read_target}, "title": None}


def list_response(entries: list, served_repo: str = "https://github.com/psf/requests") -> dict:
    served = identity(served_repo, extra="package_name")
    return {"available_versions": None, "canonical_target": None, "code_index_state": None, "coverage_reason": None,
            "coverage_state": None, "crawl_status": None, "entries": entries, "has_more": False,
            "indexed_version": None, "indexing_estimate": None, "indexing_ref": None, "indexing_status": None,
            "inventory_kind": "source", "inventory_state": None, "next_cursor": None, "preparation": None,
            "requested_target": "github:psf/requests", "resolution": None,
            "target_resolution": {"available_refs": [], "available_versions": [], "freshness": None,
                                  "freshness_reason": None, "indexing_ref": None, "requested": served,
                                  "resolved_requested": served, "served": served, "suggested_refs": []}}


def grep_hit(line: int, repo_url=None, page_url=None) -> dict:
    content = f"retry = Retry(total={line})"
    evidence = {"content_safety": {"filtered": False, "modifications": []}, "context_after_slices": [],
                "context_before_slices": [], "line": line, "line_content": content,
                "line_slice": {"content": content, "end_byte": len(content), "original_line_bytes": len(content),
                               "start_byte": 0},
                "match_end_byte": 5, "match_start_byte": 0,
                "read": {"end_line": line, "path": None if page_url else "src/adapters.py", "start_line": line,
                         "target": "github:psf/requests"},
                "source_match_end_byte": 5, "source_match_start_byte": 0, "target_index": 0}
    if page_url:
        return {**evidence, "kind": "site", "page_url": page_url}
    return {**evidence, "kind": "repository", "commit_sha": "a" * 40, "file_path": "src/adapters.py",
            "repo_url": repo_url, "repository_file_path": "src/adapters.py"}


def grep_response(hits: list) -> dict:
    return {"hits": hits, "next_cursor": None, "targets": [], "total_matches": len(hits), "traversal": "complete",
            "unavailable_targets": []}


DEFAULTS = {
    "/v1/searches": lambda: search_response([search_hit("get", "https://github.com/psf/requests")]),
    "/v1/examples/search": lambda: example_response("```python\nrequests.get(url, timeout=5)\n```\n\n"
                                                    "[Source 1](https://github.com/psf/requests/blob/abc/x.py) (Apache-2.0)"),
    "/v1/read": lambda: read_code_response("https://github.com/psf/requests"),
    "/v1/list": lambda: list_response([list_entry("src/api.py")]),
    "/v1/grep": lambda: grep_response([grep_hit(12, repo_url="https://github.com/psf/requests")]),
}
RATE_LIMITED = ("/v1/searches", "/v1/read", "/v1/list", "/v1/grep")  # the spec documents no rate headers for examples


class FakeGitHits:
    """A loopback GitHits. ``script[path]`` replaces the default answer: a callable taking the captured request and
    returning (status, headers, body bytes) or (status, headers, body bytes, send_content_length)."""

    def __init__(self):
        self.requests, self.script, self.counter = [], {}, 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                parsed = urlsplit(self.path)
                request = {"method": self.command, "path": parsed.path, "query": parse_qs(parsed.query),
                           "headers": {key.lower(): value for key, value in self.headers.items()},
                           "body": self.rfile.read(length) if length else b""}
                outer.requests.append(request)
                answer = outer.answer(request)
                status, headers, payload = answer[:3]
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if len(answer) < 4 or answer[3]:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def answer(self, request: dict):
        self.counter += 1
        if request["path"] in self.script:
            return self.script[request["path"]](request)
        if request["path"] not in DEFAULTS:
            return 404, {"Content-Type": "application/problem+json"}, b'{"code":"NOT_FOUND","status":404}'
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store",
                   "X-Request-ID": f"req{self.counter:04d}"}
        if request["path"] in RATE_LIMITED:
            headers.update({"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "97", "X-RateLimit-Reset": "1793000000"})
        return 200, headers, json.dumps(DEFAULTS[request["path"]]()).encode("utf-8")

    def serve(self, path: str, body, status: int = 200, headers: dict | None = None):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        merged = {"Content-Type": "application/json", "X-Request-ID": "req-scripted", **(headers or {})}
        self.script[path] = lambda request: (status, merged, payload)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHits()
        self.addCleanup(self.fake.close)
        self.token = "ghtest_" + secrets.token_hex(16)
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.key = secrets.token_hex(16)
        self.env = {githits.TOKEN_ENV: self.token, githits.URL_ENV: self.fake.url,
                    githits.RECORD_DIR_ENV: self.folder.name, githits.RECORD_KEY_ENV: self.key}

    def client(self, **overrides) -> githits.Client:
        return githits.Client.from_env(dict(self.env), **overrides)

    def records(self):
        return githits.read_records(self.folder.name, self.key)

    def record_text(self) -> str:
        path = Path(self.folder.name) / githits.RECORD_FILE
        return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------------------------------------- the tests

class SpecPin(unittest.TestCase):
    def test_the_fixture_is_the_pinned_document(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)

    def test_the_fixture_is_exactly_the_closed_subset_for_the_client_operations(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(fixture_bytes(json.loads(raw)), raw)
        spec = load_spec()
        self.assertEqual((spec["info"]["title"], spec["info"]["version"]), ("GitHits Public API", "0.1.0"))
        self.assertEqual(spec["servers"][0]["url"] + githits.API_PREFIX, githits.DEFAULT_BASE_URL)

    def test_every_operation_path_method_and_schema_name_is_in_the_fixture(self):
        spec = load_spec()
        for op, operation in githits.OPERATIONS.items():
            with self.subTest(op=op):
                found = operation_of(spec, op)
                self.assertEqual(found.get("x-githits-mcp-tool"), "get_example" if op == "example" else op)
                self.assertEqual(response_schema(spec, op)["$ref"].split("/")[-1], operation.response)
                if operation.request != "query":
                    body = found["requestBody"]["content"]["application/json"]["schema"]
                    self.assertEqual(body["$ref"].split("/")[-1], operation.request)

    def test_every_request_the_client_builds_is_valid_under_the_fixture(self):
        spec = load_spec()
        full = {
            "search": githits.search_request(
                "retry", ["pypi:requests@2.32.3", "github:psf/requests@main", "site:docs.python.org/3",
                          "https://gitlab.com/group/project@v1", {"registry": "npm", "name": "@scope/pkg"}],
                sources=["code", "docs", "symbol"], limit=100, offset=5, path_prefix="src/", kind="function",
                category="callable", file_intent="production", public_only=True, allow_partial_results=True,
                wait_timeout_ms=60000),
            "example": githits.example_request("retry with backoff", language="python", license_mode="custom"),
            "list": githits.list_request("npm:express@5.2.1", paths=["lib/**"], recursive=True,
                                         file_types=["config"], languages=["JavaScript"], intents=["test"],
                                         limit=500, after="cursor1", wait_timeout_ms=210000),
            "grep": githits.grep_request("Retry\\(", ["github:psf/requests", {"target": "site:requests.readthedocs.io"}],
                                         pattern_type="literal", ignore_case=True, context_lines_before=10,
                                         context_lines_after=10, max_matches=1000, cursor="c2", corpus="source",
                                         globs=["src/**"], wait_timeout_ms=210000),
        }
        for op, request in full.items():
            with self.subTest(op=op):
                schema = operation_of(spec, op)["requestBody"]["content"]["application/json"]["schema"]
                self.assertEqual(problems(spec, schema, request.body), [])
        lean = [githits.search_request("q", ["pypi:requests"]), githits.example_request("q"),
                githits.list_request("pypi:requests"), githits.grep_request("x", ["pypi:requests"])]
        for request in lean:
            schema = operation_of(spec, request.op)["requestBody"]["content"]["application/json"]["schema"]
            self.assertEqual(problems(spec, schema, request.body), [], request.op)
        search_fields = {p["name"]: p for p in operation_of(spec, "search")["parameters"] if p["in"] == "query"}
        self.assertEqual(set(full["search"].params), {"fields"})
        self.assertEqual(problems(spec, search_fields["fields"]["schema"], full["search"].params["fields"]), [])
        read_params = {p["name"]: p for p in operation_of(spec, "read")["parameters"] if p["in"] == "query"}
        for request in (githits.read_request("pypi:requests@2.32.3", path="src/requests/api.py", start_line=3,
                                             end_line=9, fields="content", wait_timeout_ms=60000),
                        githits.read_request("pypi:requests")):
            self.assertLessEqual(set(request.params), set(read_params))
            for name, value in request.params.items():
                self.assertEqual(problems(spec, read_params[name]["schema"], value), [], name)
        self.assertTrue(read_params["target"]["required"])

    def test_every_response_field_the_client_reads_is_in_the_fixture(self):
        spec = load_spec()
        for op, paths in githits.READS.items():
            for path in paths:
                with self.subTest(op=op, path=path):
                    self.assertTrue(has_path(spec, response_schema(spec, op), path))
        self.assertFalse(has_path(spec, response_schema(spec, "search"), ("result", "results", "[]", "repo")))

    def test_every_header_the_client_sends_or_reads_is_in_the_fixture(self):
        spec = load_spec()
        for op in githits.OPERATIONS:
            with self.subTest(op=op):
                operation = operation_of(spec, op)
                sent = {p["name"].lower() for p in operation["parameters"] if p["in"] == "header"}
                self.assertLessEqual({name.lower() for name in githits.HEADERS_SENT}, sent)
                session = next(p for p in operation["parameters"] if p["name"] == "X-GitHits-Session-ID")
                self.assertEqual(session["schema"]["pattern"], githits.SESSION_PATTERN.pattern)
                answered = {name.lower() for name in operation["responses"]["200"].get("headers", {})}
                self.assertIn("x-request-id", answered)
                self.assertEqual("x-ratelimit-remaining" in answered, op != "example")

    def test_the_fake_serves_responses_valid_under_the_fixture(self):
        spec = load_spec()
        bodies = {
            "search": [search_response([search_hit("a", "https://github.com/psf/requests"),
                                        search_hit("b", None, package_name="requests", version="2.32.3")], True)],
            "example": [example_response("text")],
            "read": [read_code_response("https://github.com/psf/requests"), read_doc_response("# Quickstart")],
            "list": [list_response([list_entry("a.py"), list_entry("b.py")])],
            "grep": [grep_response([grep_hit(3, repo_url="https://github.com/psf/requests"),
                                    grep_hit(4, page_url="https://requests.readthedocs.io/en/latest/")])],
        }
        for op, examples in bodies.items():
            for body in examples:
                with self.subTest(op=op):
                    self.assertEqual(problems(spec, response_schema(spec, op), body), [])
        self.assertTrue(problems(spec, response_schema(spec, "example"), {"result": "x"}), "the validator bites")

    def test_only_the_example_request_carries_a_license_and_no_result_schema_has_one(self):
        """The license policy's evidence in 0.1.0: ``license_mode`` on examples, licenses in example Markdown, and
        no license field on any result. When GitHits adds one, this fails and the client should read it by name."""
        spec = load_spec()
        mode = spec["components"]["schemas"]["ExampleRequest"]["properties"]["license_mode"]
        self.assertEqual(mode["default"], "strict")
        self.assertEqual(spec["components"]["schemas"]["LicenseMode"]["enum"], ["strict", "yolo", "custom"])
        for op in githits.OPERATIONS:
            names, pending, seen = set(), [response_schema(spec, op)], set()
            while pending:
                node = pending.pop()
                if isinstance(node, dict):
                    ref = node.get("$ref")
                    if ref and ref not in seen:
                        seen.add(ref)
                        pending.append(_resolve(spec, ref))
                    names |= set(node.get("properties", {}))
                    pending += list(node.values())
                elif isinstance(node, list):
                    pending += node
            with self.subTest(op=op):
                self.assertFalse({name for name in names if "licen" in name or "spdx" in name})
            if op != "read":
                parameters = {p["name"] for p in operation_of(spec, op).get("parameters", [])}
                self.assertFalse({name for name in parameters if "licen" in name})


class LicensePolicy(unittest.TestCase):
    def test_spdx_expressions_against_the_permissive_default(self):
        policy = githits.LicensePolicy()
        self.assertEqual(policy.allowed, tuple(sorted(githits.PERMISSIVE, key=str.lower)))
        for expression in ("MIT", "apache-2.0", "MIT OR GPL-2.0-only", "(BSD-3-Clause AND ISC)", "0BSD",
                           "Apache-2.0 WITH LLVM-exception", "CC0-1.0", "PSF-2.0", "Unlicense", "Zlib"):
            with self.subTest(expression=expression):
                self.assertTrue(policy.allows(expression))
        for expression in ("GPL-3.0-only", "LGPL-2.1-or-later", "AGPL-3.0", "MPL-2.0", "EPL-2.0", "EUPL-1.2",
                           "CDDL-1.0", "SSPL-1.0", "MIT AND GPL-2.0-only", "GPL-2.0-only WITH Classpath-exception-2.0",
                           "NOASSERTION", "Proprietary", "", None, 7):
            with self.subTest(expression=expression):
                self.assertFalse(policy.allows(expression))

    def test_the_environment_replaces_the_list_or_turns_the_check_off(self):
        self.assertEqual(githits.LicensePolicy.from_env({}).digest, githits.LicensePolicy().digest)
        self.assertEqual(githits.LicensePolicy.from_env({githits.LICENSES_ENV: " "}).allowed,
                         githits.LicensePolicy().allowed)
        only_mit = githits.LicensePolicy.from_env({githits.LICENSES_ENV: "mit"})
        self.assertTrue(only_mit.allows("MIT"))
        self.assertFalse(only_mit.allows("Apache-2.0"))
        anything = githits.LicensePolicy.from_env({githits.LICENSES_ENV: "ANY"})
        self.assertTrue(anything.allows("GPL-3.0-only") and anything.allows(None))
        self.assertEqual(len({only_mit.digest, anything.digest, githits.LicensePolicy().digest}), 3)
        for value in ("any,MIT", "MIT;GPL", "Apache 2.0"):
            with self.subTest(value=value), self.assertRaises(githits.GitHitsError):
                githits.LicensePolicy.from_env({githits.LICENSES_ENV: value})

    def test_example_licenses_come_from_source_links_license_lines_and_references(self):
        read = githits.example_licenses
        self.assertEqual(read({"result": "x [Source 1](https://github.com/a/b/blob/1/c.py) (MIT)\n"
                                         "[Source 2](https://gitlab.com/a/b/-/blob/1/c.py) (GPL-3.0-only)"}),
                         ["MIT", "GPL-3.0-only"])
        self.assertEqual(read({"result": "[Source 1](https://github.com/a/b/blob/1/c.py)"}), [None])
        self.assertEqual(read({"result": "see [the docs](https://docs.python.org/3/) only"}), [None])
        self.assertEqual(read({"result": "code\n\n**License:** BSD-3-Clause"}), ["BSD-3-Clause"])
        self.assertEqual(read({"result": "", "references": [{"license_spdx_id": "MPL-2.0"}, {"url": "u"}]}),
                         ["MPL-2.0", None])


class Exclusion(unittest.TestCase):
    def test_owner_and_repository_locators_in_every_spelling(self):
        exclude = githits.Exclusions(githits.DEFAULT_EXCLUDE)
        for text in ("https://github.com/SyberLabs/SyberWork", "github:syberlabs/flyspace@main", "SyberLabs/SyberWork",
                     "github.com/SYBERLABS/x", "git@github.com:SyberLabs/SyberWork.git", "npm:@syberlabs/sdk",
                     "pypi:syberlabs", "https://raw.githubusercontent.com/SyberLabs/SyberWork/main/README.md",
                     "https://syberlabs.github.io/docs/", "https://api.github.com/repos/SyberLabs/SyberWork",
                     "https%3A%2F%2Fgithub.com%2FSyberLabs%2FSyberWork", "gitlab:SyberLabs/mirror"):
            with self.subTest(text=text):
                self.assertTrue(exclude.names(text))
        for text in ("https://github.com/psf/requests", "github:SyberLabsX/fork", "github:other/SyberLabs",
                     "pypi:syberlabs-sdk", "https://github.com/syberlabs-archive/x", "npm:express@5.2.1",
                     "site:docs.python.org/3", "https://notgithub.com/SyberLabs/x", ""):
            with self.subTest(text=text):
                self.assertFalse(exclude.names(text))

    def test_free_text_counts_only_a_code_host_mention(self):
        exclude = githits.Exclusions(githits.DEFAULT_EXCLUDE)
        self.assertTrue(exclude.mentioned("see [Source 2](https://GitHub.com/syberlabs/SyberWork/blob/1/a.py) (MIT)"))
        self.assertTrue(exclude.mentioned("from github.com%2FSyberLabs%2FSyberWork"))
        self.assertFalse(exclude.mentioned("SyberLabs/SyberWork is mentioned without a host"))
        self.assertFalse(exclude.mentioned("https://github.com/psf/requests/blob/main/src/requests/api.py"))

    def test_the_environment_replaces_the_default_and_an_empty_value_keeps_it(self):
        custom = githits.Exclusions.from_env({githits.EXCLUDE_ENV: " Acme , Other/Repo "})
        self.assertTrue(custom.names("github:acme/anything"))
        self.assertTrue(custom.names("https://github.com/other/repo"))
        self.assertFalse(custom.names("https://github.com/other/else"))
        self.assertFalse(custom.names("github:SyberLabs/SyberWork"))
        for value in ("", " , "):
            self.assertTrue(githits.Exclusions.from_env({githits.EXCLUDE_ENV: value}).names("github:SyberLabs/x"))
        with self.assertRaises(githits.GitHitsError):
            githits.Exclusions(["a/b/c"])


class Token(FakeCase):
    def test_the_token_goes_only_into_the_authorization_header(self):
        client = self.client(session_id="arm-on_7", agent="claude-code/2.1.309")
        client.search("how to retry", ["pypi:requests@2.32.3"])
        client.read("pypi:requests@2.32.3", path="src/requests/api.py")
        self.assertEqual(len(self.fake.requests), 2)
        for request in self.fake.requests:
            headers = request["headers"]
            self.assertEqual(headers["authorization"], f"Bearer {self.token}")
            others = {key: value for key, value in headers.items() if key != "authorization"}
            self.assertNotIn(self.token, json.dumps(others) + json.dumps(request["query"]) + request["path"])
            self.assertNotIn(self.token.encode(), request["body"])
            self.assertEqual(headers["x-githits-client-name"], "syberlabs")
            self.assertTrue(headers["x-githits-client-version"])
            self.assertEqual(headers["x-githits-session-id"], "arm-on_7")
            self.assertEqual(headers["x-githits-agent"], "claude-code/2.1.309")
        self.assertNotIn(self.token, self.record_text())
        self.assertNotIn(self.token, repr(client))

    def test_no_session_or_agent_header_unless_set(self):
        self.client().example("retry with backoff")
        headers = self.fake.requests[0]["headers"]
        self.assertNotIn("x-githits-session-id", headers)
        self.assertNotIn("x-githits-agent", headers)

    def test_an_echoed_token_is_scrubbed_from_the_error_and_the_cli(self):
        problem = {"code": "AUTHENTICATION_REQUIRED", "title": f"bad token {self.token}", "status": 401}
        self.fake.serve("/v1/searches", problem, 401, {"Content-Type": "application/problem+json"})
        with self.assertRaises(githits.GitHitsError) as caught:
            self.client().search("q", ["pypi:requests"])
        error = caught.exception
        self.assertEqual((error.code, error.http_status, error.request_id), ("http_401", 401, "req-scripted"))
        self.assertIn("AUTHENTICATION_REQUIRED", str(error))
        self.assertNotIn(self.token, str(error) + repr(error.args) + repr(error.__context__) + repr(error.__cause__))
        code, out, err = run_cli(self.env, "githits", "search", "q", "--target", "pypi:requests")
        self.assertEqual(code, 1)
        self.assertNotIn(self.token, out + err)
        self.assertNotIn(self.token, self.record_text())

    def test_without_a_token_nothing_is_sent_and_the_call_is_recorded(self):
        env = dict(self.env)
        del env[githits.TOKEN_ENV]
        with self.assertRaises(githits.GitHitsError) as caught:
            githits.Client.from_env(env).example("q")
        self.assertEqual(caught.exception.code, "no_token")
        self.assertEqual(self.fake.requests, [])
        self.assertEqual([r["status"] for r in self.records().records], ["error"])

    def test_settings_are_checked_before_any_request(self):
        for settings, code in (({"base_url": "http://example.com/v1"}, "bad_base_url"),
                               ({"base_url": "https://user:pw@api.githits.dev/v1"}, "bad_base_url"),
                               ({"session_id": "has space"}, "bad_session_id"),
                               ({"agent": "x" * 161}, "bad_agent"),
                               ({"token": "two words"}, "bad_token"),
                               ({"record_dir": self.folder.name}, "record_config")):
            with self.subTest(code=code), self.assertRaises(githits.GitHitsError) as caught:
                githits.Client(**{"token": self.token, **settings})
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(githits.Client(token=self.token).base_url, "https://api.githits.dev/v1")
        self.assertEqual(self.fake.requests, [])


class Contamination(FakeCase):
    def test_an_excluded_target_is_refused_without_a_request(self):
        client = self.client()
        calls = [lambda: client.search("q", ["pypi:requests", "github:SyberLabs/SyberWork@main"]),
                 lambda: client.search("q", [{"repo_url": "https://GITHUB.com/syberlabs/flyspace"}]),
                 lambda: client.read("https://github.com/SyberLabs/SyberWork/blob/main/README.md"),
                 lambda: client.list("SyberLabs/SyberWork"),
                 lambda: client.grep("x", ["npm:express", {"target": "github:syberlabs/x"}])]
        for call in calls:
            result = call()
            self.assertEqual((result.status, result.data, result.refused), (githits.EXCLUDED, None, True))
        self.assertEqual(self.fake.requests, [])
        read = self.records()
        self.assertEqual(read.ignored, 0)
        self.assertEqual([(r["op"], r["status"], r["http_status"], r["targets_count"]) for r in read.records],
                         [("search", "excluded_target", None, 2), ("search", "excluded_target", None, 1),
                          ("read", "excluded_target", None, 1), ("list", "excluded_target", None, 1),
                          ("grep", "excluded_target", None, 2)])

    def test_search_hits_under_an_excluded_owner_are_dropped_and_counted(self):
        self.fake.serve("/v1/searches", search_response([
            search_hit("ours", "https://github.com/SyberLabs/SyberWork"),
            search_hit("pkg", None, package_name="syberwork", source_url="https://github.com/syberlabs/syberwork/blob/1/a.py"),
            search_hit("theirs", "https://github.com/psf/requests")]), headers={"X-RateLimit-Remaining": "41"})
        result = self.client().search("q", ["pypi:requests"])
        self.assertEqual((result.status, result.results_returned, result.results_dropped), ("ok", 1, 2))
        self.assertEqual([hit["title"] for hit in result.data["result"]["results"]], ["theirs"])
        self.assertEqual(result.rate_limit_remaining, 41)
        record = self.records().records[-1]
        self.assertEqual((record["results_returned"], record["results_dropped"], record["rate_limit_remaining"]),
                         (1, 2, 41))

    def test_grep_list_and_read_results_under_an_excluded_owner_are_dropped(self):
        self.fake.serve("/v1/grep", grep_response([grep_hit(1, repo_url="https://github.com/SyberLabs/SyberWork"),
                                                   grep_hit(2, page_url="https://syberlabs.github.io/guide"),
                                                   grep_hit(3, repo_url="https://github.com/psf/requests")]))
        grep = self.client().grep("Retry", ["pypi:requests"])
        self.assertEqual((grep.results_returned, grep.results_dropped, [h["line"] for h in grep.data["hits"]]),
                         (1, 2, [3]))
        self.fake.serve("/v1/list", list_response([list_entry("a.py"), list_entry("b.py")],
                                                  served_repo="https://github.com/SyberLabs/SyberWork"))
        served = self.client().list("pypi:syberwork-mirror")
        self.assertEqual((served.status, served.results_returned, served.results_dropped, served.data["entries"]),
                         ("ok", 0, 2, []))
        self.fake.serve("/v1/list", list_response([list_entry("a.py"), list_entry("b.py", "github:SyberLabs/x")]))
        entry = self.client().list("pypi:requests")
        self.assertEqual((entry.results_returned, entry.results_dropped), (1, 1))
        self.fake.serve("/v1/read", read_code_response("https://github.com/SyberLabs/SyberWork"))
        read = self.client().read("pypi:syberwork-mirror")
        self.assertEqual((read.status, read.data, read.results_returned, read.results_dropped), ("ok", None, 0, 1))
        self.assertEqual([r["results_dropped"] for r in self.records().records], [2, 2, 1, 1])

    def test_an_example_citing_an_excluded_owner_is_dropped_whole(self):
        cases = [example_response("code\n\n[Source 1](https://GitHub.com/syberlabs/SyberWork/blob/abc/x.py) (MIT)"),
                 example_response("taken from github.com%2FSyberLabs%2FSyberWork"),
                 {**example_response("clean text"), "references": [
                     {"url": "https://example.org/a", "source": {"target": {"repo_url": "https://github.com/SyberLabs/Flyspace"}}}]}]
        for body in cases:
            self.fake.serve("/v1/examples/search", body)
            result = self.client().example("retry with backoff")
            self.assertEqual((result.status, result.data, result.contamination_blocked, result.results_dropped),
                             ("contamination_blocked", None, True, 1))
        del self.fake.script["/v1/examples/search"]
        result = self.client().example("retry with backoff")
        self.assertEqual((result.status, result.results_returned, result.contamination_blocked), ("ok", 1, False))
        self.assertIn("requests.get", result.data["result"])
        records = self.records().records
        self.assertEqual([r["contamination_blocked"] for r in records], [True, True, True, False])
        self.assertEqual({r["rate_limit_remaining"] for r in records}, {None})

    def test_a_read_body_citing_an_excluded_owner_is_blocked(self):
        self.fake.serve("/v1/read", read_doc_response("Fork of https://github.com/SyberLabs/SyberWork"))
        result = self.client().read("https://requests.readthedocs.io/en/latest/")
        self.assertEqual((result.status, result.data, result.contamination_blocked), ("contamination_blocked", None, True))
        self.fake.serve("/v1/read", read_doc_response("# Quickstart\nrequests.get(url)"))
        self.assertEqual(self.client().read("https://requests.readthedocs.io/en/latest/").results_returned, 1)


def source(license_text: str | None, repo: str = "psf/requests") -> str:
    tail = f" ({license_text})" if license_text else ""
    return f"[Source 1](https://github.com/{repo}/blob/0123abc/src/a.py){tail}"


class Licenses(FakeCase):
    def test_a_copyleft_hit_is_dropped_and_counted_and_a_permissive_one_kept(self):
        self.fake.serve("/v1/searches", search_response([
            {**search_hit("gpl", "https://github.com/a/gpl"), "license_spdx_id": "GPL-3.0-only"},
            {**search_hit("apache", "https://github.com/a/apache"), "license_spdx_id": "Apache-2.0"},
            {**search_hit("dual", "https://github.com/a/dual"), "license_spdx_id": "MIT OR GPL-2.0-only"},
            {**search_hit("both", "https://github.com/a/both"), "license_spdx_id": "MIT AND GPL-2.0-only"},
            search_hit("unknown", "https://github.com/a/unknown", license="NOASSERTION"),
            search_hit("unlabelled", "https://github.com/a/plain"),
            {**search_hit("ours", "https://github.com/SyberLabs/SyberWork"), "license_spdx_id": "Apache-2.0"}]))
        result = self.client().search("q", ["pypi:requests"])
        self.assertEqual([hit["title"] for hit in result.data["result"]["results"]], ["apache", "dual", "unlabelled"])
        self.assertEqual((result.results_returned, result.results_dropped, result.results_dropped_license), (3, 1, 3))
        self.fake.serve("/v1/grep", grep_response([{**grep_hit(1, repo_url="https://github.com/a/b"),
                                                    "license_spdx_id": "LGPL-2.1-or-later"},
                                                   grep_hit(2, repo_url="https://github.com/a/c")]))
        grep = self.client().grep("x", ["pypi:requests"])
        self.assertEqual((grep.results_returned, grep.results_dropped, grep.results_dropped_license), (1, 0, 1))
        records = self.records().records
        self.assertEqual([(r["results_dropped"], r["results_dropped_license"]) for r in records], [(1, 3), (0, 1)])
        self.assertEqual({r["license_policy"] for r in records}, {githits.LicensePolicy().digest})

    def test_an_example_citing_copyleft_or_unlicensed_sources_is_dropped(self):
        for markdown in (source("AGPL-3.0-only"), source("MIT") + "\n" + source("AGPL-3.0-or-later"),
                         source(None), "```python\nprint(1)\n```", "code\n\nLicense: GPL-2.0-only"):
            self.fake.serve("/v1/examples/search", example_response(markdown))
            result = self.client().example("retry with backoff")
            with self.subTest(markdown=markdown):
                self.assertEqual((result.status, result.data, result.results_dropped_license,
                                  result.contamination_blocked, result.results_dropped), ("ok", None, 1, False, 0))
        self.fake.serve("/v1/examples/search", {**example_response("clean " + source("MIT")),
                                                "references": [{"url": "https://github.com/a/b", "license_spdx_id": "MPL-2.0"}]})
        self.assertEqual(self.client().example("q").results_dropped_license, 1)
        self.fake.serve("/v1/examples/search", example_response(source("MIT") + "\n" + source("BSD-3-Clause", "a/b")))
        kept = self.client().example("q")
        self.assertEqual((kept.results_returned, kept.results_dropped_license), (1, 0))
        self.assertEqual({json.loads(r["body"])["license_mode"] for r in self.fake.requests}, {"strict"})
        self.assertEqual([r["results_dropped_license"] for r in self.records().records], [1, 1, 1, 1, 1, 1, 0])
        code, out, _ = run_cli(self.env, "githits", "example", "q")
        self.assertEqual(code, 0)
        self.fake.serve("/v1/examples/search", example_response(source("AGPL-3.0-only")))
        code, out, _ = run_cli(self.env, "githits", "example", "q")
        self.assertEqual((code, out.splitlines()), (0, ["example: dropped by the license policy (a source is "
                                                        "copyleft, unlisted or has no license)"]))

    def test_contamination_is_judged_before_the_license(self):
        self.fake.serve("/v1/examples/search", example_response(source("GPL-3.0-only", "SyberLabs/SyberWork")))
        result = self.client().example("q")
        self.assertEqual((result.status, result.results_dropped, result.results_dropped_license),
                         ("contamination_blocked", 1, 0))

    def test_any_restores_pass_through(self):
        self.env[githits.LICENSES_ENV] = "any"
        self.fake.serve("/v1/searches", search_response([
            {**search_hit("gpl", "https://github.com/a/gpl"), "license_spdx_id": "GPL-3.0-only"}]))
        self.fake.serve("/v1/examples/search", example_response(source("AGPL-3.0-only")))
        client = self.client()
        search, example = client.search("q", ["pypi:requests"]), client.example("q")
        self.assertEqual((search.results_returned, search.results_dropped_license), (1, 0))
        self.assertEqual((example.results_returned, example.results_dropped_license), (1, 0))
        self.assertIn("AGPL-3.0-only", example.data["result"])
        self.assertEqual(json.loads(self.fake.requests[-1]["body"])["license_mode"], "strict")
        records = self.records().records
        self.assertEqual({r["license_policy"] for r in records}, {githits.LicensePolicy(None).digest})
        self.assertNotEqual(records[0]["license_policy"], githits.LicensePolicy().digest)
        self.env[githits.LICENSES_ENV] = "MIT"
        self.fake.serve("/v1/searches", search_response([
            {**search_hit("apache", "https://github.com/a/apache"), "license_spdx_id": "Apache-2.0"}]))
        self.assertEqual(self.client().search("q", ["pypi:requests"]).results_dropped_license, 1)


class Transport(FakeCase):
    def test_lean_defaults_are_sent(self):
        client = self.client()
        client.search("q", ["pypi:requests"])
        client.read("pypi:requests", path="src/requests/api.py")
        client.read("https://docs.python.org/3/library/json.html%23json.dumps")
        client.read("https://docs.python.org/3/library/json.html#basic-usage")
        client.list("pypi:requests")
        client.grep("Retry", ["pypi:requests"])
        client.example("q")
        search, read, read_encoded, read_fragment, listing, grep, example = self.fake.requests
        self.assertEqual(json.loads(search["body"])["limit"], githits.SEARCH_LIMIT)
        self.assertEqual(search["query"], {"fields": ["results"]})
        self.assertEqual((read["method"], read["query"]["start_line"], read["query"]["end_line"]), ("GET", ["1"], ["200"]))
        self.assertIn("end_line", read_encoded["query"])
        self.assertNotIn("start_line", read_fragment["query"])
        self.assertEqual(json.loads(listing["body"]), {"target": "pypi:requests", "limit": githits.LIST_LIMIT})
        self.assertEqual(json.loads(grep["body"])["max_matches"], githits.GREP_MAX_MATCHES)
        self.assertEqual(json.loads(example["body"]), {"query": "q", "license_mode": "strict"})
        self.assertEqual([r["path"] for r in self.fake.requests],
                         ["/v1/searches", "/v1/read", "/v1/read", "/v1/read", "/v1/list", "/v1/grep", "/v1/examples/search"])

    def test_a_redirect_is_refused_and_not_followed(self):
        target = self.fake.url + "/capture"
        self.fake.script["/v1/searches"] = lambda request: (302, {"Location": target, "X-Request-ID": "req-302"}, b"")
        with self.assertRaises(githits.GitHitsError) as caught:
            self.client().search("q", ["pypi:requests"])
        self.assertEqual((caught.exception.code, caught.exception.http_status), ("redirect_refused", 302))
        self.assertEqual([r["path"] for r in self.fake.requests], ["/v1/searches"])
        self.assertEqual([(r["status"], r["http_status"], r["request_id"]) for r in self.records().records],
                         [("error", 302, "req-302")])

    def test_an_oversize_body_is_refused_with_or_without_a_length(self):
        big = json.dumps(example_response("x" * 4000)).encode()
        for with_length in (True, False):
            self.fake.script["/v1/examples/search"] = lambda request, w=with_length: (
                200, {"Content-Type": "application/json", "X-Request-ID": "req-big"}, big, w)
            with self.subTest(with_length=with_length), self.assertRaises(githits.GitHitsError) as caught:
                self.client(max_bytes=1024).example("q")
            self.assertEqual(caught.exception.code, "response_too_large")
        self.assertEqual([(r["status"], r["http_status"]) for r in self.records().records],
                         [("error", 200), ("error", 200)])

    def test_a_non_200_answer_is_an_error_and_its_status_is_recorded(self):
        self.fake.serve("/v1/grep", {"code": "RATE_LIMITED", "title": "Too many requests", "status": 429},
                        429, {"Content-Type": "application/problem+json", "Retry-After": "30",
                              "X-RateLimit-Remaining": "0", "X-Request-ID": "req-429"})
        with self.assertRaises(githits.GitHitsError) as caught:
            self.client().grep("x", ["pypi:requests"])
        self.assertEqual((caught.exception.code, caught.exception.request_id), ("http_429", "req-429"))
        self.assertIn("RATE_LIMITED", str(caught.exception))
        self.fake.serve("/v1/list", b"", 500)
        with self.assertRaises(githits.GitHitsError) as caught:
            self.client().list("pypi:requests")
        self.assertEqual(caught.exception.code, "http_500")
        records = self.records().records
        self.assertEqual([(r["op"], r["status"], r["http_status"], r["request_id"], r["rate_limit_remaining"])
                          for r in records], [("grep", "error", 429, "req-429", 0), ("list", "error", 500, "req-scripted", None)])

    def test_a_malformed_body_is_an_error_and_recorded(self):
        for body in (b"{not json", b"[1, 2]", b"\xff\xfe"):
            self.fake.serve("/v1/searches", body)
            with self.assertRaises(githits.GitHitsError) as caught:
                self.client().search("q", ["pypi:requests"])
            self.assertEqual((caught.exception.code, caught.exception.http_status), ("malformed_response", 200))
        self.assertEqual([(r["status"], r["http_status"]) for r in self.records().records], [("error", 200)] * 3)


class Records(FakeCase):
    def test_records_hold_digests_and_counts_never_the_query_and_verify_by_mac(self):
        query = "private-looking query " + secrets.token_hex(8)
        client = self.client()
        client.search(query, ["pypi:requests"])
        client.grep(query, ["pypi:requests"])
        text = self.record_text()
        self.assertNotIn(query, text)
        read = self.records()
        self.assertEqual(read.ignored, 0)
        self.assertEqual(len(read.records), 2)
        for record, request in zip(read.records, self.fake.requests):
            self.assertEqual(tuple(sorted(record)), tuple(sorted(githits.RECORD_FIELDS)))
            self.assertEqual(record["query_digest"],
                             hashlib.sha256(canonical(json.loads(request["body"])).encode("utf-8")).hexdigest())
            self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record["at"]))
            self.assertIsInstance(record["latency_ms"], int)
            self.assertEqual((record["status"], record["http_status"], record["rate_limit_remaining"]), ("ok", 200, 97))
        self.assertEqual([r["request_id"] for r in read.records], ["req0001", "req0002"])

    def test_forged_tampered_and_malformed_lines_are_ignored_and_counted(self):
        client = self.client()
        client.search("q", ["pypi:requests"])
        client.example("q")
        path = Path(self.folder.name) / githits.RECORD_FILE
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        tampered = {**first, "results_dropped": 0, "contamination_blocked": True}
        forged = {**{k: first[k] for k in githits.RECORD_FIELDS}, "status": "ok"}
        forged["mac"] = githits.record_mac(forged, "not-the-key")
        unsigned = {k: first[k] for k in githits.RECORD_FIELDS}
        extra = {**first, "query": "smuggled"}
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            for line in (json.dumps(tampered), json.dumps(forged), json.dumps(unsigned), json.dumps(extra), "garbage", "[]"):
                handle.write(line + "\n")
        read = self.records()
        self.assertEqual(([r["op"] for r in read.records], read.ignored), (["search", "example"], 6))
        self.assertEqual(githits.read_records(self.folder.name, "wrong-key"), githits.RecordRead([], 8))
        self.assertEqual(githits.read_records(self.folder.name, ""), githits.RecordRead([], 8))
        self.assertEqual(githits.read_records(Path(self.folder.name) / "missing", self.key), githits.RecordRead([], 0))

    def test_no_record_without_the_record_settings(self):
        env = {k: v for k, v in self.env.items() if k not in (githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV)}
        githits.Client.from_env(env).search("q", ["pypi:requests"])
        self.assertEqual(self.record_text(), "")


def run_cli(env: dict, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env):
        for name in ENV_NAMES:
            if name not in env:
                os.environ.pop(name, None)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Cli(FakeCase):
    def test_exit_codes(self):
        self.assertEqual(run_cli(self.env, "githits", "search", "q", "--target", "pypi:requests")[0], 0)
        code, out, err = run_cli(self.env, "githits", "list", "github:SyberLabs/SyberWork")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("excluded_target", err)
        self.fake.serve("/v1/examples/search", example_response("see https://github.com/SyberLabs/SyberWork"))
        code, out, err = run_cli(self.env, "githits", "example", "retry")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("contamination_blocked", err)
        self.fake.serve("/v1/read", b"", 503)
        code, out, err = run_cli(self.env, "githits", "read", "pypi:requests")
        self.assertEqual(code, 1)
        self.assertIn("error: http_503", err)
        self.assertEqual(run_cli({}, "githits", "grep", "x", "--target", "pypi:requests")[0], 1)
        self.assertEqual(len(self.fake.requests), 3)

    def test_compact_output_by_default_and_json_on_request(self):
        self.fake.serve("/v1/searches", search_response([search_hit("ours", "https://github.com/SyberLabs/SyberWork"),
                                                         search_hit("get", "https://github.com/psf/requests",
                                                                    git_ref="v2.32.3")], has_more=True))
        code, out, _ = run_cli(self.env, "githits", "search", "how to retry", "--target", "pypi:requests@2.32.3",
                               "--source", "code", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["search: 1 results, 1 dropped (excluded owner) | more: yes",
                                            "1. [0.88] github.com/psf/requests@v2.32.3 src/get.py:10-20 get",
                                            "   How get works."])
        sent = json.loads(self.fake.requests[-1]["body"])
        self.assertEqual((sent["limit"], sent["sources"], sent["targets"]),
                         (2, ["code"], [{"registry": "pypi", "name": "requests", "version": "2.32.3"}]))
        code, out, _ = run_cli(self.env, "githits", "search", "q", "--target", "pypi:requests", "--json")
        parsed = json.loads(out)
        self.assertEqual((parsed["status"], parsed["results_dropped"], parsed["refused"]), ("ok", 1, False))
        code, out, _ = run_cli(self.env, "githits", "grep", "Retry", "--target", "pypi:requests", "--literal", "-B", "1")
        self.assertEqual(out.splitlines(), ["grep: 1 matches of 1",
                                            "github.com/psf/requests/src/adapters.py:12: retry = Retry(total=12)"])
        self.assertEqual(json.loads(self.fake.requests[-1]["body"])["pattern_type"], "literal")
        code, out, _ = run_cli(self.env, "githits", "read", "pypi:requests", "--path", "src/api.py", "--start-line", "1")
        self.assertEqual(out.splitlines(), ["read: src/api.py lines 1-2 of 2", "def get(url):", "    return url"])
        code, out, _ = run_cli(self.env, "githits", "list", "pypi:requests", "--recursive")
        self.assertEqual(out.splitlines(), ["list: 1 entries", "file  src/api.py  12 lines"])
        code, out, _ = run_cli(self.env, "githits", "example", "retry", "--language", "python")
        self.assertTrue(out.startswith("example: complete, solution 6f1c1a52"))
        self.assertTrue(all(record["status"] == "ok" for record in self.records().records))
