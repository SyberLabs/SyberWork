"""A GitHits API client with SyberLabs contamination control and host-verifiable records (``docs/GITHITS.md``).

Five read-only operations of the GitHits public REST API, request and response shapes from its OpenAPI document
(``tests/fixtures/githits_openapi_v1.json`` pins the part this client uses):

    search   POST /v1/searches          docs, code and symbols across 1 to 20 explicit targets
    example  POST /v1/examples/search   a generated example built from open-source references (Markdown)
    read     GET  /v1/read              a documentation page or a source file, by line range
    list     POST /v1/list              the files or pages of one package, repository or site
    grep     POST /v1/grep              a regular expression or literal across up to 20 targets

Standard library only. The token comes only from ``GITHITS_API_TOKEN`` (or the ``token`` argument) and goes only
into the ``Authorization`` header: it is never logged, never in an exception, a record or ``repr``. Redirects are
refused (a redirect could carry the header to another origin), plain HTTP is allowed only to a loopback host,
every call has a timeout, and a response larger than ``max_bytes`` is refused.

Contamination control, because GitHits indexes public code and SyberLabs' own repositories are public:

(a) a request whose target names an excluded owner or repository is refused (``excluded_target``) before any
    network call;
(b) a search, grep or list result, or a read, whose source identity (``locator.repo_url``, a grep hit's
    ``repo_url`` or ``page_url``, the served repository of a list or read) is under an excluded owner is dropped
    and counted (``results_dropped``);
(c) an example, or a read body, whose text or structured references mention ``github.com/<excluded owner>/``
    (case-insensitive; also GitLab, Codeberg, raw and gist hosts, ``<owner>.github.io``, and the URL-decoded
    text) is dropped whole and marked ``contamination_blocked``. This is detection, not prevention: GitHits may
    already have blended an excluded repository into the generated code without citing it, and nothing on this
    side can see that. Index-side exclusion is asked of GitHits.

License policy: permissive only by default (``PERMISSIVE``), because what an agent takes from a result can land in
this Apache-2.0 repository, which cannot meet a copyleft license's obligations. Server side, every example asks for
``license_mode: strict`` (the only license parameter of the five operations; the document does not say which
licenses ``strict`` admits). Client side, a result that carries a license is dropped and counted
(``results_dropped_license``) unless the license, an SPDX expression, is on the allow list: copyleft (GPL, LGPL,
AGPL, MPL, EPL, EUPL, CDDL, SSPL), ``NOASSERTION``, unlisted and missing licenses are dropped. In the 0.1.0 contract
only an example carries licenses, as ``(SPDX)`` after each source link of its Markdown; an example with a source
link that has no license, or with no source link, is dropped whole. Search, grep, list and read results carry no
license field, so they pass this check unjudged until GitHits adds one. ``SYBERLABS_GITHITS_LICENSES`` replaces
the allow list (a comma list of SPDX ids) or turns the client-side check off (``any``); every record carries the
digest of the policy in force (``license_policy``).

The exclusion list is ``SyberLabs`` unless ``SYBERLABS_GITHITS_EXCLUDE`` names others (a comma list of owners or
``owner/repo``); an empty value keeps the default.

With ``SYBERLABS_GITHITS_RECORD_DIR`` and ``SYBERLABS_GITHITS_RECORD_KEY`` set, every call, refused ones included,
appends one JSON line to ``githits-records.jsonl`` in that directory: ``RECORD_FIELDS`` and ``mac``, an
HMAC-SHA256 over the record's canonical JSON under the key (``query_guard.sign``, the MAC the query guard's
decisions use). No query text, no result and no token is stored; ``query_digest`` is the SHA-256 of the canonical
request. ``read_records`` returns the records whose MAC verifies and counts the rest.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from os import environ as _process_environ
from pathlib import Path
from typing import NamedTuple

from syberlabs.canonical import canonical
from syberlabs.query_guard import sign, utf8, verify

DEFAULT_BASE_URL = "https://api.githits.dev/v1"
API_PREFIX = "/v1"  # the OpenAPI paths start with it; the base URL ends with it
CLIENT_NAME = "syberlabs"
TOKEN_ENV, URL_ENV = "GITHITS_API_TOKEN", "GITHITS_API_URL"
SESSION_ENV, AGENT_ENV = "GITHITS_SESSION_ID", "GITHITS_AGENT"
EXCLUDE_ENV = "SYBERLABS_GITHITS_EXCLUDE"
LICENSES_ENV = "SYBERLABS_GITHITS_LICENSES"
RECORD_DIR_ENV, RECORD_KEY_ENV = "SYBERLABS_GITHITS_RECORD_DIR", "SYBERLABS_GITHITS_RECORD_KEY"
DEFAULT_EXCLUDE = ("SyberLabs",)
# The licenses a result may carry by default: permissive licenses whose only obligation is to keep a notice, which an
# Apache-2.0 project can meet. Anything not listed (copyleft, NOASSERTION, unknown, none) is dropped.
PERMISSIVE = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "Zlib", "PSF-2.0",
              "CC0-1.0")
ANY_LICENSE = "any"
LICENSE_KEYS = ("license_spdx_id", "spdx_id", "license_id", "license")  # none is in the 0.1.0 response schemas
RECORD_FILE = "githits-records.jsonl"
RECORD_FIELDS = ("op", "query_digest", "targets_count", "status", "http_status", "request_id", "results_returned",
                 "results_dropped", "results_dropped_license", "license_policy", "contamination_blocked", "latency_ms",
                 "rate_limit_remaining", "at")

OK, EXCLUDED, BLOCKED, ERROR = "ok", "excluded_target", "contamination_blocked", "error"
STATUSES = (OK, EXCLUDED, BLOCKED, ERROR)

# Lean defaults for an agent's context window; GitHits' own default in the comment.
SEARCH_LIMIT = 5          # hits per search (10)
SEARCH_FIELDS = "results"  # (results) sent explicitly: highlights, sources and progress cost tokens and are opt-in
READ_LINES = 200          # a read with no line bounds asks for lines 1-200 (GitHits: the whole file or page)
LIST_LIMIT = 50           # entries per list page (100)
GREP_MAX_MATCHES = 20     # matches per grep page (100); context lines stay 0 before and after (0)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # a larger response body is refused, not truncated
DEFAULT_TIMEOUT = 60.0    # seconds; a call that asks the server to wait gets that wait plus TIMEOUT_MARGIN
EXAMPLE_TIMEOUT = 120.0   # generation is slower than lookup
TIMEOUT_MARGIN = 30.0

SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_AGENT_BYTES, MAX_CLIENT_HEADER_BYTES = 160, 80
REGISTRIES = ("npm", "pypi", "hex", "crates", "vcpkg", "zig", "nuget", "maven", "packagist", "rubygems", "go", "swift")
REPOSITORY_PREFIXES = {"github": "https://github.com/", "gitlab": "https://gitlab.com/",
                       "codeberg": "https://codeberg.org/"}
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_HOSTS = (r"(?:[a-z0-9-]+\.)*(?:github\.com|gitlab\.com|codeberg\.org|bitbucket\.org|githubusercontent\.com)"
          r"|github|gitlab|codeberg")


class Operation(NamedTuple):
    method: str
    path: str          # as in the OpenAPI document
    request: str       # the request body's schema, or "query" for the parameters of a GET
    response: str      # the 200 response's schema


OPERATIONS = {
    "search": Operation("POST", "/v1/searches", "SearchRequest", "SearchResponse"),
    "example": Operation("POST", "/v1/examples/search", "ExampleRequest", "ExampleResponse"),
    "read": Operation("GET", "/v1/read", "query", "ReadResponse"),
    "list": Operation("POST", "/v1/list", "ListRequest", "ListResponse"),
    "grep": Operation("POST", "/v1/grep", "GrepRequest", "GrepResponse"),
}
# The response fields this client reads, as paths through the response schema ("[]" is an array's items).
# tests/test_githits.py resolves every one in the pinned OpenAPI fixture, so a renamed field fails a test.
READS = {
    "search": tuple(("result", "results", "[]", *tail) for tail in (
        ("locator", "repo_url"), ("locator", "source_url"), ("locator", "docs_read_target"), ("locator", "git_ref"),
        ("locator", "file_path"), ("locator", "start_line"), ("locator", "end_line"), ("locator", "package_name"),
        ("locator", "version"), ("score",), ("title",), ("summary",), ("target_label",))) + (
        ("result", "page", "has_more"), ("search_ref",), ("completed",)),
    "example": (("result",), ("search_status",), ("solution_id",)),
    "read": (("kind",), ("path",), ("content", "body"), ("content", "start_line"), ("content", "end_line"),
             ("metadata", "total_lines"), ("metadata", "title"), ("docs_read_target",), ("source", "repo_url"),
             ("source", "url"), ("target_resolution", "served", "repo_url"),
             ("target_resolution", "resolved_requested", "repo_url")),
    "list": (("entries", "[]", "path"), ("entries", "[]", "kind"), ("entries", "[]", "line_count"),
             ("entries", "[]", "read", "target"), ("entries", "[]", "browse", "target"), ("next_cursor",),
             ("has_more",), ("canonical_target",), ("target_resolution", "served", "repo_url"),
             ("target_resolution", "resolved_requested", "repo_url")),
    "grep": (("hits", "[]", "repo_url"), ("hits", "[]", "page_url"), ("hits", "[]", "file_path"),
             ("hits", "[]", "line"), ("hits", "[]", "line_content"), ("hits", "[]", "kind"),
             ("next_cursor",), ("total_matches",)),
}
HEADERS_SENT = ("X-GitHits-Client-Name", "X-GitHits-Client-Version", "X-GitHits-Session-ID", "X-GitHits-Agent")


class GitHitsError(Exception):
    """A call that failed: ``code`` is stable (``no_token``, ``http_429``, ``malformed_response``, ...). The message
    never holds the token: the client builds it from status codes and the server's problem ``code`` and ``title``,
    and removes the token from it if the server echoed it."""

    def __init__(self, code: str, message: str, http_status: int | None = None, request_id: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.http_status, self.request_id = code, http_status, request_id


class Request(NamedTuple):
    op: str
    body: dict | None      # the JSON body of a POST
    params: dict           # query parameters (a GET's whole request; ``fields`` on search)
    targets: tuple         # every target string the request names, for the exclusion check
    timeout: float


class Result(NamedTuple):
    op: str
    status: str                    # ok, excluded_target or contamination_blocked; errors raise GitHitsError
    data: dict | None              # the response with excluded results removed; None when refused or blocked
    http_status: int | None
    request_id: str | None
    results_returned: int
    results_dropped: int
    contamination_blocked: bool
    rate_limit_remaining: int | None
    results_dropped_license: int = 0   # dropped by the license policy, apart from results_dropped
    reason: str | None = None

    @property
    def refused(self) -> bool:
        return self.status in (EXCLUDED, BLOCKED)


class RecordRead(NamedTuple):
    records: list    # authentic records, in file order
    ignored: int     # lines whose MAC did not verify, or that were not records


# ---------------------------------------------------------------------------------------------------------- exclusions

class Exclusions:
    """Owners (``SyberLabs``) and repositories (``SyberLabs/SyberWork``) whose content must not reach a result,
    matched case-insensitively."""

    def __init__(self, entries):
        parsed = []
        for entry in entries:
            parts = [part.strip() for part in str(entry).strip().strip("/").split("/")]
            if not 1 <= len(parts) <= 2 or not all(_NAME.match(part) for part in parts):
                raise GitHitsError("bad_exclusion", f"an exclusion is an owner or owner/repo, got {str(entry)[:80]!r}")
            parsed.append(tuple(part.lower() for part in parts))
        if not parsed:
            raise GitHitsError("bad_exclusion", "the exclusion list is empty")
        self.entries = tuple(dict.fromkeys(parsed))
        mentions, bare = [], []
        for owner, *repo in self.entries:
            tail = (r"/" + re.escape(repo[0]) + r"(?:\.git)?(?![a-z0-9_.-])") if repo else r"(?![a-z0-9_-])"
            mentions.append(r"(?<![a-z0-9_.-])(?:" + _HOSTS + r")[/:]+(?:repos/)?" + re.escape(owner) + tail)
            mentions.append(r"(?<![a-z0-9_.-])" + re.escape(owner) + r"\.github\.io"
                            + (r"/" + re.escape(repo[0]) + r"(?![a-z0-9_.-])" if repo else r"(?![a-z0-9_.-])"))
            bare.append(r"^(?:[a-z0-9+.-]+:)?[@/]*" + re.escape(owner) + tail)
        self._mention = re.compile("|".join(mentions), re.IGNORECASE)
        self._bare = re.compile("|".join(bare), re.IGNORECASE)

    @classmethod
    def from_env(cls, environ=None) -> "Exclusions":
        environ = _process_environ if environ is None else environ
        listed = [item for item in environ.get(EXCLUDE_ENV, "").split(",") if item.strip()]
        return cls(listed or DEFAULT_EXCLUDE)

    def mentioned(self, text) -> bool:
        """True when free text names an excluded owner or repository on a code host (as typed or URL-decoded)."""
        if not isinstance(text, str) or not text:
            return False
        return bool(self._mention.search(text) or self._mention.search(urllib.parse.unquote(text)))

    def names(self, locator) -> bool:
        """True when a target or repository locator is under an excluded owner: a URL or ``github:owner/repo`` as
        ``mentioned`` reads it, or a bare ``owner/repo``, ``@owner/package`` or ``registry:owner``."""
        if not isinstance(locator, str) or not locator.strip():
            return False
        text = locator.strip()
        return self.mentioned(text) or bool(self._bare.search(text) or self._bare.search(urllib.parse.unquote(text)))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class LicensePolicy:
    """The SPDX licenses a result may carry (``None``: any, the client-side check off), case-insensitively."""

    def __init__(self, allowed=PERMISSIVE):
        if allowed is None:
            self.allowed = None
        else:
            ids = [str(item).strip() for item in allowed if str(item).strip()]
            if not ids or not all(re.fullmatch(r"[A-Za-z0-9.+-]+", item) for item in ids) \
                    or any(item.lower() == ANY_LICENSE for item in ids):
                raise GitHitsError("bad_license_policy",
                                   f"{LICENSES_ENV} is a comma list of SPDX ids, or {ANY_LICENSE}")
            self.allowed = tuple(sorted(dict.fromkeys(ids), key=str.lower))
        self._lower = None if self.allowed is None else {item.lower() for item in self.allowed}
        self.digest = hashlib.sha256(utf8(canonical({"allow": list(self.allowed) if self.allowed else ANY_LICENSE})
                                          )).hexdigest()

    @classmethod
    def from_env(cls, environ=None) -> "LicensePolicy":
        environ = _process_environ if environ is None else environ
        value = environ.get(LICENSES_ENV, "").strip()
        if value.lower() == ANY_LICENSE:
            return cls(None)
        listed = [item for item in value.split(",") if item.strip()]
        return cls(listed or PERMISSIVE)

    def allows(self, expression) -> bool:
        """True for an SPDX expression the policy admits: one ``OR`` branch whose every ``AND`` part (an exception
        after ``WITH`` aside) is allowed. A missing, blank or unparsable license is not."""
        if self.allowed is None:
            return True
        if not isinstance(expression, str) or not expression.strip():
            return False
        for branch in re.split(r"\s+OR\s+", expression.strip().strip("()"), flags=re.IGNORECASE):
            parts = [re.sub(r"\s+WITH\s+.*$", "", part.strip().strip("()").strip(), flags=re.IGNORECASE)
                     for part in re.split(r"\s+AND\s+", branch.strip().strip("()"), flags=re.IGNORECASE)]
            if parts and all(part.lower() in self._lower for part in parts):
                return True
        return False

    def admits(self, item) -> bool:
        """False only when ``item`` carries a license field (``LICENSE_KEYS``) the policy does not allow; an item
        with no license field is not judged."""
        if self.allowed is None or not isinstance(item, dict):
            return True
        for key in LICENSE_KEYS:
            if key in item:
                value = item[key]
                if isinstance(value, dict):
                    value = value.get("spdx_id") or value.get("id") or value.get("name")
                return self.allows(value)
        return True


_SOURCE_LINK = re.compile(r"\]\((https?://[^)\s]+)\)(?:[ \t]*\(([^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\))?")
_CODE_HOST = re.compile(r"^https?://(?:[a-z0-9-]+\.)*(?:github\.com|gitlab\.com|codeberg\.org|bitbucket\.org"
                        r"|githubusercontent\.com)/", re.IGNORECASE)
_LICENSE_LINE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?\**licen[cs]es?\**[ \t]*:[ \t]*(.+?)[ \t]*$",
                           re.IGNORECASE | re.MULTILINE)


def example_licenses(data: dict) -> list:
    """The licenses an example names for its sources: the ``(SPDX)`` after each code-host link of its Markdown,
    ``License:`` lines, and structured ``references`` when present. ``None`` stands for a source with no license,
    and an example with no source at all is ``[None]``."""
    found = []
    text = data.get("result") if isinstance(data.get("result"), str) else ""
    for url, license_text in _SOURCE_LINK.findall(text):
        if _CODE_HOST.match(url):
            found.append(license_text.strip() or None)
    found += [line.strip("* ") or None for line in _LICENSE_LINE.findall(text)]
    references = data.get("references") if isinstance(data.get("references"), list) else []
    for reference in references:
        if isinstance(reference, dict):
            value = next((reference[key] for key in LICENSE_KEYS if key in reference), None)
            found.append(value if isinstance(value, str) and value.strip() else None)
    return found or [None]


# ------------------------------------------------------------------------------------------------------------ requests

def search_target(target) -> dict:
    """A search target object from ``pypi:requests@2.31.0``, ``github:owner/repo@ref``, ``https://...[@ref]`` or
    ``site:host/path``; a dict is passed through."""
    if isinstance(target, dict):
        return dict(target)
    text = str(target).strip()
    scheme, _, rest = text.partition(":")
    if scheme == "site" and rest:
        return {"site": rest}
    if scheme in REGISTRIES and rest:
        name, version = _split_at(rest)
        return {"registry": scheme, "name": name, **({"version": version} if version else {})}
    if scheme in REPOSITORY_PREFIXES and rest:
        path, ref = _split_at(rest)
        return {"repo_url": REPOSITORY_PREFIXES[scheme] + path.strip("/"), **({"git_ref": ref} if ref else {})}
    if scheme in ("http", "https"):
        parsed = urllib.parse.urlsplit(text)
        path, ref = _split_at(parsed.path)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        return {"repo_url": url, **({"git_ref": ref} if ref else {})}
    raise GitHitsError("bad_target", "a search target is registry:name[@version], github:owner/repo[@ref], "
                                     "https://host/owner/repo[@ref] or site:host[/path]")


def _split_at(text: str) -> tuple[str, str | None]:
    """``name@version`` into its parts; a leading ``@`` (an npm scope) is part of the name."""
    at = text.rfind("@")
    return (text[:at], text[at + 1:] or None) if at > 0 else (text, None)


def _target_strings(target: dict) -> list[str]:
    found = [target.get("repo_url"), target.get("site")]
    if target.get("registry") or target.get("name"):
        found.append(f"{target.get('registry') or ''}:{target.get('name') or ''}")
    return [item for item in found if isinstance(item, str)]


def _wait_timeout(wait_timeout_ms, floor: float = DEFAULT_TIMEOUT) -> float:
    return max(floor, (wait_timeout_ms or 0) / 1000 + TIMEOUT_MARGIN)


def _drop_none(mapping: dict) -> dict:
    return {key: value for key, value in mapping.items() if value is not None}


def search_request(query: str, targets, *, sources=None, limit: int = SEARCH_LIMIT, offset: int | None = None,
                   fields: str | None = SEARCH_FIELDS, path_prefix: str | None = None, kind: str | None = None,
                   category: str | None = None, file_intent: str | None = None, public_only: bool | None = None,
                   allow_partial_results: bool | None = None, wait_timeout_ms: int | None = None) -> Request:
    objects = [search_target(target) for target in (targets or [])]
    filters = _drop_none({"path_prefix": path_prefix, "kind": kind, "category": category, "file_intent": file_intent,
                          "public_only": public_only})
    body = _drop_none({"query": query, "targets": objects, "sources": list(sources) if sources else None,
                       "limit": limit, "offset": offset, "filters": filters or None,
                       "allow_partial_results": allow_partial_results, "wait_timeout_ms": wait_timeout_ms})
    names = tuple(text for target in objects for text in _target_strings(target))
    return Request("search", body, _drop_none({"fields": fields}), names, _wait_timeout(wait_timeout_ms or 20000))


def example_request(query: str, *, language: str | None = None, license_mode: str | None = "strict") -> Request:
    body = _drop_none({"query": query, "language": language, "license_mode": license_mode})
    return Request("example", body, {}, (), EXAMPLE_TIMEOUT)


def read_request(target: str, *, path: str | None = None, start_line: int | None = None, end_line: int | None = None,
                 fields: str | None = None, wait_timeout_ms: int | None = None, all_lines: bool = False) -> Request:
    if not all_lines and start_line is None and end_line is None and "#" not in str(target):
        start_line, end_line = 1, READ_LINES  # a fragment names a section; line bounds would override it
    elif not all_lines and start_line is not None and end_line is None:
        end_line = start_line + READ_LINES - 1
    params = _drop_none({"target": target, "path": path or None, "start_line": start_line, "end_line": end_line,
                         "wait_timeout_ms": wait_timeout_ms, "fields": fields})
    return Request("read", None, params, (str(target),), _wait_timeout(wait_timeout_ms or 20000))


def list_request(target: str, *, paths=None, recursive: bool | None = None, file_types=None, languages=None,
                 intents=None, limit: int = LIST_LIMIT, after: str | None = None,
                 wait_timeout_ms: int | None = None) -> Request:
    body = _drop_none({"target": target, "paths": list(paths) if paths else None, "recursive": recursive,
                       "file_types": list(file_types) if file_types else None,
                       "languages": list(languages) if languages else None,
                       "intents": list(intents) if intents else None, "limit": limit, "after": after or None,
                       "wait_timeout_ms": wait_timeout_ms})
    return Request("list", body, {}, (str(target),), _wait_timeout(wait_timeout_ms))


def grep_request(pattern: str, targets, *, pattern_type: str | None = None, ignore_case: bool | None = None,
                 context_lines_before: int | None = None, context_lines_after: int | None = None,
                 max_matches: int = GREP_MAX_MATCHES, cursor: str | None = None, corpus: str | None = None,
                 globs=None, wait_timeout_ms: int | None = None) -> Request:
    objects = []
    for target in targets or []:
        item = dict(target) if isinstance(target, dict) else {"target": str(target)}
        if corpus and "corpus" not in item:
            item["corpus"] = corpus
        if globs and "path_selectors" not in item:
            item["path_selectors"] = [{"kind": "glob", "value": glob} for glob in globs]
        objects.append(item)
    body = _drop_none({"pattern": pattern, "targets": objects, "pattern_type": pattern_type,
                       "ignore_case": ignore_case, "context_lines_before": context_lines_before,
                       "context_lines_after": context_lines_after, "max_matches": max_matches,
                       "cursor": cursor or None, "wait_timeout_ms": wait_timeout_ms})
    names = tuple(str(item.get("target", "")) for item in objects)
    return Request("grep", body, {}, names, _wait_timeout(wait_timeout_ms))


# --------------------------------------------------------------------------------------------------------- filtering

def _filter(op: str, data: dict, exclude: Exclusions, licenses: LicensePolicy) -> tuple:
    """(status, data, returned, dropped for an excluded owner, dropped by the license policy): layers (b) and (c),
    then the license policy over what is left."""
    if op == "example":
        if any(exclude.mentioned(text) for text in _strings(data)):
            return BLOCKED, None, 0, 1, 0
        if not all(licenses.allows(item) for item in example_licenses(data)):
            return OK, None, 0, 0, 1
        return OK, data, 1, 0, 0
    if op == "read":
        served = data.get("target_resolution") if isinstance(data.get("target_resolution"), dict) else {}
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        identities = [(served.get(key) or {}).get("repo_url") for key in ("served", "resolved_requested")
                      if isinstance(served.get(key), dict)]
        identities += [source.get("repo_url"), source.get("url"), data.get("docs_read_target")]
        if any(exclude.names(item) for item in identities):
            return OK, None, 0, 1, 0
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        if exclude.mentioned(content.get("body")):
            return BLOCKED, None, 0, 1, 0
        if not (licenses.admits(data) and licenses.admits(data.get("metadata"))):
            return OK, None, 0, 0, 1
        return OK, data, 1 if content else 0, 0, 0
    if op == "search":
        result = data.get("result") if isinstance(data.get("result"), dict) else None
        hits = result.get("results") if result and isinstance(result.get("results"), list) else []
        clean = [hit for hit in hits if not _hit_excluded(hit, exclude)]
        kept = [hit for hit in clean if licenses.admits(hit) and licenses.admits(hit.get("locator"))]
        if result is not None and isinstance(result.get("results"), list):
            data = {**data, "result": {**result, "results": kept}}
        return OK, data, len(kept), len(hits) - len(clean), len(clean) - len(kept)
    if op == "grep":
        hits = data.get("hits") if isinstance(data.get("hits"), list) else []
        clean = [hit for hit in hits if not (isinstance(hit, dict) and (
            exclude.names(hit.get("repo_url")) or exclude.mentioned(hit.get("page_url"))
            or exclude.names(((hit.get("read") or {}) if isinstance(hit.get("read"), dict) else {}).get("target"))))]
        kept = [hit for hit in clean if licenses.admits(hit)]
        return OK, {**data, "hits": kept}, len(kept), len(hits) - len(clean), len(clean) - len(kept)
    if op == "list":
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        resolution = data.get("target_resolution") if isinstance(data.get("target_resolution"), dict) else {}
        identities = [(resolution.get(key) or {}).get("repo_url") for key in ("served", "resolved_requested")
                      if isinstance(resolution.get(key), dict)] + [data.get("canonical_target")]
        if any(exclude.names(item) for item in identities):
            clean = []
        else:
            clean = [entry for entry in entries if not (isinstance(entry, dict) and any(
                exclude.names((entry.get(action) or {}).get("target"))
                for action in ("read", "browse") if isinstance(entry.get(action), dict)))]
        kept = [entry for entry in clean if licenses.admits(entry)] if licenses.admits(data) else []
        return OK, {**data, "entries": kept}, len(kept), len(entries) - len(clean), len(clean) - len(kept)
    raise GitHitsError("unknown_operation", op)


def _hit_excluded(hit, exclude: Exclusions) -> bool:
    locator = hit.get("locator") if isinstance(hit, dict) and isinstance(hit.get("locator"), dict) else {}
    return (exclude.names(locator.get("repo_url")) or exclude.mentioned(locator.get("source_url"))
            or exclude.names(locator.get("docs_read_target")))


# ------------------------------------------------------------------------------------------------------------ records

def record_mac(record: dict, key: str) -> str:
    return sign({field: record.get(field) for field in RECORD_FIELDS}, key)


def write_record(directory, key: str, record: dict) -> None:
    """Append one record with its MAC as a single line. Raises OSError when the line cannot be written."""
    entry = {field: record.get(field) for field in RECORD_FIELDS}
    entry["mac"] = record_mac(entry, key)
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / RECORD_FILE, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")


def read_records(directory, key: str) -> RecordRead:
    """The records in ``directory`` whose MAC verifies under ``key``; every other line is ignored and counted."""
    path = Path(directory) / RECORD_FILE
    found, ignored = [], 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return RecordRead([], 0)
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            ignored += 1
            continue
        if (not isinstance(entry, dict) or set(entry) != set(RECORD_FIELDS) | {"mac"}
                or not verify({field: entry[field] for field in RECORD_FIELDS}, entry.get("mac"), key)):
            ignored += 1
            continue
        found.append({field: entry[field] for field in RECORD_FIELDS})
    return RecordRead(found, ignored)


def query_digest(request: Request) -> str:
    """SHA-256 of the canonical request: the JSON body of a POST, the query parameters of a GET."""
    return hashlib.sha256(utf8(canonical(request.body if request.body is not None else request.params))).hexdigest()


# ------------------------------------------------------------------------------------------------------------- client

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status, which the client refuses


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _client_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("syberwork")[:MAX_CLIENT_HEADER_BYTES]
        except PackageNotFoundError:
            return "source"
    except ImportError:  # pragma: no cover
        return "source"


class Client:
    """``Client.from_env()`` reads every setting from the environment; the keyword arguments override it."""

    def __init__(self, *, token: str | None = None, base_url: str = DEFAULT_BASE_URL, exclude=None,
                 record_dir=None, record_key: str | None = None, session_id: str | None = None,
                 agent: str | None = None, timeout: float | None = None, max_bytes: int = MAX_RESPONSE_BYTES,
                 licenses=None):
        self._token = token or None
        if self._token is not None and (not self._token.isprintable() or any(c.isspace() for c in self._token)):
            raise GitHitsError("bad_token", "the token holds whitespace or a control character")
        parsed = urllib.parse.urlsplit(base_url or DEFAULT_BASE_URL)
        host = parsed.hostname or ""
        if (parsed.scheme not in ("https", "http") or not host or parsed.username or parsed.password
                or parsed.query or parsed.fragment or (parsed.scheme == "http" and not _loopback(host))):
            raise GitHitsError("bad_base_url", "the base URL must be https:// (http:// only to a loopback host), "
                                               "with no credentials, query or fragment")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._loopback = _loopback(host)
        self.exclude = exclude if isinstance(exclude, Exclusions) else Exclusions(exclude or DEFAULT_EXCLUDE)
        self.licenses = licenses if isinstance(licenses, LicensePolicy) else LicensePolicy(licenses or PERMISSIVE)
        if bool(record_dir) != bool(record_key):
            raise GitHitsError("record_config", f"records need both {RECORD_DIR_ENV} and {RECORD_KEY_ENV}")
        self.record_dir, self._record_key = (Path(record_dir), record_key) if record_dir else (None, None)
        if session_id is not None and not SESSION_PATTERN.match(session_id):
            raise GitHitsError("bad_session_id", f"{SESSION_ENV} must match [A-Za-z0-9_-]{{1,64}}")
        if agent is not None and (len(agent.encode("utf-8")) > MAX_AGENT_BYTES or not agent.isprintable()
                                  or not agent.isascii()):
            raise GitHitsError("bad_agent", f"{AGENT_ENV} must be printable ASCII of at most {MAX_AGENT_BYTES} bytes")
        self.session_id, self.agent = session_id, agent
        self.timeout, self.max_bytes = timeout, max_bytes
        handlers = [_NoRedirect()] + ([urllib.request.ProxyHandler({})] if self._loopback else [])
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_env(cls, environ=None, **overrides) -> "Client":
        environ = _process_environ if environ is None else environ
        settings = {"token": environ.get(TOKEN_ENV) or None, "base_url": environ.get(URL_ENV) or DEFAULT_BASE_URL,
                    "exclude": Exclusions.from_env(environ), "licenses": LicensePolicy.from_env(environ),
                    "record_dir": environ.get(RECORD_DIR_ENV) or None,
                    "record_key": environ.get(RECORD_KEY_ENV) or None,
                    "session_id": environ.get(SESSION_ENV) or None, "agent": environ.get(AGENT_ENV) or None}
        settings.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**settings)

    def __repr__(self) -> str:
        return (f"Client(base_url={self.base_url!r}, token={'set' if self._token else 'unset'}, "
                f"exclude={['/'.join(entry) for entry in self.exclude.entries]!r}, "
                f"licenses={list(self.licenses.allowed) if self.licenses.allowed else ANY_LICENSE!r}, "
                f"records={'on' if self.record_dir else 'off'})")

    # The five operations. Each returns a Result, or raises GitHitsError after recording the failure.
    def search(self, query: str, targets, **options) -> Result:
        return self.call(search_request(query, targets, **options))

    def example(self, query: str, **options) -> Result:
        return self.call(example_request(query, **options))

    def read(self, target: str, **options) -> Result:
        return self.call(read_request(target, **options))

    def list(self, target: str, **options) -> Result:
        return self.call(list_request(target, **options))

    def grep(self, pattern: str, targets, **options) -> Result:
        return self.call(grep_request(pattern, targets, **options))

    def call(self, request: Request) -> Result:
        started = time.monotonic()
        record = {"op": request.op, "query_digest": query_digest(request), "targets_count": len(request.targets),
                  "status": ERROR, "http_status": None, "request_id": None, "results_returned": 0,
                  "results_dropped": 0, "results_dropped_license": 0, "license_policy": self.licenses.digest,
                  "contamination_blocked": False, "latency_ms": 0,
                  "rate_limit_remaining": None}
        if any(self.exclude.names(target) for target in request.targets):
            result = Result(request.op, EXCLUDED, None, None, None, 0, 0, False, None,
                            reason="a target names an excluded owner or repository; no request was sent")
            self._record(record, result, started)
            return result
        try:
            status, headers, raw = self._send(request)
        except GitHitsError as exc:
            record.update(http_status=exc.http_status, request_id=exc.request_id)
            self._record(record, None, started, headers=getattr(exc, "headers", None))
            raise
        request_id = _request_id(headers)
        record.update(http_status=status, request_id=request_id)
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            self._record(record, None, started, headers=headers)
            raise GitHitsError("malformed_response", f"HTTP {status} with a body that is not a JSON object",
                               status, request_id) from None
        state, kept, returned, dropped, unlicensed = _filter(request.op, data, self.exclude, self.licenses)
        result = Result(request.op, state, kept, status, request_id, returned, dropped, state == BLOCKED,
                        _remaining(headers), unlicensed,
                        "the response cites an excluded owner; it was dropped whole" if state == BLOCKED else None)
        self._record(record, result, started, headers=headers)
        return result

    def _send(self, request: Request) -> tuple[int, object, bytes]:
        if not self._token:
            raise GitHitsError("no_token", f"set {TOKEN_ENV}")
        operation = OPERATIONS[request.op]
        url = self.base_url + operation.path[len(API_PREFIX):]
        if request.params:
            url += "?" + urllib.parse.urlencode(request.params)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "X-GitHits-Client-Name": CLIENT_NAME, "X-GitHits-Client-Version": _client_version()}
        if self.session_id:
            headers["X-GitHits-Session-ID"] = self.session_id
        if self.agent:
            headers["X-GitHits-Agent"] = self.agent
        data = None
        if request.body is not None:
            data = json.dumps(request.body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        timeout = self.timeout or request.timeout
        try:
            with self._opener.open(urllib.request.Request(url, data=data, headers=headers, method=operation.method),
                             timeout=timeout) as response:
                return response.status, response.headers, self._body(response, response.headers, response.status)
        except urllib.error.HTTPError as exc:
            with exc:
                error = self._http_error(exc)
            raise error from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise GitHitsError("transport", self._scrub(f"{type(exc).__name__}: {reason}"[:200])) from None

    def _http_error(self, exc: urllib.error.HTTPError) -> GitHitsError:
        request_id = _request_id(exc.headers)
        if 300 <= exc.code < 400:
            error = GitHitsError("redirect_refused", f"HTTP {exc.code}: redirects are not followed", exc.code,
                                 request_id)
        else:
            try:
                problem = json.loads(self._body(exc, exc.headers, exc.code).decode("utf-8"))
            except (GitHitsError, ValueError, OSError):
                problem = None
            problem = problem if isinstance(problem, dict) else {}
            words = " ".join(str(problem.get(key))[:120] for key in ("code", "title") if problem.get(key))
            error = GitHitsError(f"http_{exc.code}", self._scrub(words or "no problem document"), exc.code,
                                 request_id)
        error.headers = exc.headers
        return error

    def _body(self, response, headers, status: int) -> bytes:
        length = headers.get("Content-Length") if headers is not None else None
        if length is not None and length.strip().isdigit() and int(length) > self.max_bytes:
            raise GitHitsError("response_too_large", f"Content-Length {int(length)} > {self.max_bytes} bytes",
                               status, _request_id(headers))
        raw = response.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise GitHitsError("response_too_large", f"more than {self.max_bytes} bytes", status, _request_id(headers))
        return raw

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[token]") if self._token else text

    def _record(self, record: dict, result: Result | None, started: float, headers=None) -> None:
        if result is not None:
            record.update(status=result.status, results_returned=result.results_returned,
                          results_dropped=result.results_dropped,
                          results_dropped_license=result.results_dropped_license,
                          contamination_blocked=result.contamination_blocked)
        record.update(latency_ms=int(round((time.monotonic() - started) * 1000)),
                      rate_limit_remaining=_remaining(headers),
                      at=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        if self.record_dir is None:
            return
        try:
            write_record(self.record_dir, self._record_key, record)
        except OSError as exc:
            raise GitHitsError("record_failed", f"cannot append to {RECORD_FILE}: {exc.strerror or type(exc).__name__}",
                               record.get("http_status"), record.get("request_id")) from None


def _request_id(headers) -> str | None:
    value = headers.get("X-Request-ID") if headers is not None else None
    return value.strip()[:128] if isinstance(value, str) and value.strip() else None


def _remaining(headers) -> int | None:
    value = headers.get("X-RateLimit-Remaining") if headers is not None else None
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------------------- CLI

def render(result: Result) -> str:
    """The compact text an agent reads: one header line, then one or two lines per result."""
    data = result.data or {}
    dropped = (f", {result.results_dropped} dropped (excluded owner)" if result.results_dropped else "") + (
        f", {result.results_dropped_license} dropped (license)" if result.results_dropped_license else "")
    lines = []
    if result.op == "search":
        evidence = data.get("result") if isinstance(data.get("result"), dict) else {}
        page = evidence.get("page") if isinstance(evidence.get("page"), dict) else {}
        more = " | more: yes" if page.get("has_more") else ""
        pending = "" if data.get("completed", True) else f" | incomplete, search_ref {data.get('search_ref')}"
        lines.append(f"search: {result.results_returned} results{dropped}{more}{pending}")
        for number, hit in enumerate(evidence.get("results") or [], 1):
            locator = hit.get("locator") or {}
            where = _where(locator)
            score = f"[{hit['score']:.2f}] " if isinstance(hit.get("score"), (int, float)) else ""
            lines.append(f"{number}. {score}{where} {_short(hit.get('title'), 100)}".rstrip())
            if hit.get("summary"):
                lines.append("   " + _short(hit["summary"], 200))
    elif result.op == "example" and not data:
        lines.append("example: dropped by the license policy (a source is copyleft, unlisted or has no license)")
    elif result.op == "example":
        lines.append(f"example: {data.get('search_status')}, solution {data.get('solution_id')}")
        lines.append(str(data.get("result") or "").rstrip())
    elif result.op == "read":
        if not data:
            lines.append(f"read: nothing returned{dropped}")
        else:
            content = data.get("content") if isinstance(data.get("content"), dict) else {}
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            span = (f" lines {content.get('start_line')}-{content.get('end_line')}"
                    if content.get("start_line") is not None else "")
            total = f" of {metadata.get('total_lines')}" if metadata.get("total_lines") is not None else ""
            name = data.get("path") or metadata.get("title") or data.get("docs_read_target") or ""
            lines.append(f"read: {name}{span}{total}")
            if content.get("body") is not None:
                lines.append(str(content["body"]).rstrip("\n"))
    elif result.op == "list":
        more = f" | next: {data['next_cursor']}" if data.get("next_cursor") else ""
        lines.append(f"list: {result.results_returned} entries{dropped}{more}")
        for entry in data.get("entries") or []:
            count = f"  {entry['line_count']} lines" if entry.get("line_count") is not None else ""
            lines.append(f"{entry.get('kind') or '?'}  {entry.get('path')}{count}")
    elif result.op == "grep":
        more = f" | next: {data['next_cursor']}" if data.get("next_cursor") else ""
        total = f" of {data['total_matches']}" if data.get("total_matches") is not None else ""
        lines.append(f"grep: {result.results_returned} matches{total}{dropped}{more}")
        for hit in data.get("hits") or []:
            origin = _bare_url(hit.get("repo_url")) + "/" + str(hit.get("file_path")) if hit.get("repo_url") \
                else _bare_url(hit.get("page_url"))
            lines.append(f"{origin}:{hit.get('line')}: {_short(hit.get('line_content'), 200)}")
    return "\n".join(lines)


def _where(locator: dict) -> str:
    base = _bare_url(locator.get("repo_url")) or (
        f"{locator.get('package_name')}@{locator.get('version')}" if locator.get("package_name") else "")
    if locator.get("git_ref") and locator.get("repo_url"):
        base += "@" + str(locator["git_ref"])
    path = locator.get("file_path") or ""
    span = f":{locator['start_line']}-{locator.get('end_line')}" if locator.get("start_line") is not None else ""
    return (f"{base} {path}{span}" if path else base or _bare_url(locator.get("source_url"))).strip()


def _bare_url(url) -> str:
    return re.sub(r"^https?://", "", url) if isinstance(url, str) else ""


def _short(text, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def add_parser(commands) -> None:
    """Register ``syberlabs githits ...`` on the CLI's subcommands."""
    tool = commands.add_parser("githits", help="look up public dependency code and docs through the GitHits API, "
                                               "with SyberLabs repositories excluded (docs/GITHITS.md)")
    steps = tool.add_subparsers(dest="githits_command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the filtered response and counts as JSON")
    common.add_argument("--timeout", type=float, help="seconds for the HTTP call (default: per operation)")
    waiting = argparse.ArgumentParser(add_help=False)
    waiting.add_argument("--wait-ms", dest="wait_timeout_ms", type=int, help="how long GitHits may wait for indexing")

    search = steps.add_parser("search", parents=[common, waiting], help="search docs, code and symbols of targets")
    search.add_argument("query")
    search.add_argument("--target", dest="targets", action="append", required=True,
                        help="registry:name[@version], github:owner/repo[@ref], https://... or site:host/path "
                             "(repeatable, 1 to 20)")
    search.add_argument("--source", dest="sources", action="append", choices=("docs", "code", "symbol"))
    search.add_argument("--limit", type=int, default=SEARCH_LIMIT, help=f"hits (default {SEARCH_LIMIT})")
    search.add_argument("--offset", type=int)
    search.add_argument("--fields", default=SEARCH_FIELDS, help=f"response groups (default {SEARCH_FIELDS})")
    search.add_argument("--path-prefix")
    search.add_argument("--kind")
    search.add_argument("--category")
    search.add_argument("--file-intent")
    search.add_argument("--public-only", action="store_true", default=None)
    search.add_argument("--allow-partial", dest="allow_partial_results", action="store_true", default=None)

    example = steps.add_parser("example", parents=[common], help="a generated example from open-source references")
    example.add_argument("query")
    example.add_argument("--language")
    example.add_argument("--license-mode", choices=("strict", "yolo", "custom"), default="strict")

    read = steps.add_parser("read", parents=[common, waiting], help="read a documentation page or source lines")
    read.add_argument("target")
    read.add_argument("--path")
    read.add_argument("--start-line", type=int)
    read.add_argument("--end-line", type=int)
    read.add_argument("--fields", help="metadata, content, or both (GitHits' default)")
    read.add_argument("--all-lines", action="store_true", help=f"no default window of {READ_LINES} lines")

    listing = steps.add_parser("list", parents=[common, waiting], help="list a target's files or pages")
    listing.add_argument("target")
    listing.add_argument("--path", dest="paths", action="append", help="path or glob (repeatable)")
    listing.add_argument("--recursive", action="store_true", default=None)
    listing.add_argument("--file-type", dest="file_types", action="append")
    listing.add_argument("--language", dest="languages", action="append")
    listing.add_argument("--intent", dest="intents", action="append")
    listing.add_argument("--limit", type=int, default=LIST_LIMIT, help=f"entries (default {LIST_LIMIT})")
    listing.add_argument("--after", help="the next cursor of the previous page")

    grep = steps.add_parser("grep", parents=[common, waiting], help="match a pattern across targets")
    grep.add_argument("pattern")
    grep.add_argument("--target", dest="targets", action="append", required=True,
                      help="npm:express@5.2.1, github:owner/repo or site:host/path (repeatable, 1 to 20)")
    grep.add_argument("--literal", dest="pattern_type", action="store_const", const="literal")
    grep.add_argument("--ignore-case", action="store_true", default=None)
    grep.add_argument("-B", "--before", dest="context_lines_before", type=int)
    grep.add_argument("-A", "--after-lines", dest="context_lines_after", type=int)
    grep.add_argument("--max-matches", type=int, default=GREP_MAX_MATCHES,
                      help=f"matches (default {GREP_MAX_MATCHES})")
    grep.add_argument("--cursor")
    grep.add_argument("--corpus", choices=("source", "documentation", "all"))
    grep.add_argument("--glob", dest="globs", action="append", help="path glob for every target (repeatable)")


_OPTIONS = {
    "search": ("sources", "limit", "offset", "fields", "path_prefix", "kind", "category", "file_intent", "public_only",
               "allow_partial_results", "wait_timeout_ms"),
    "example": ("language", "license_mode"),
    "read": ("path", "start_line", "end_line", "fields", "wait_timeout_ms", "all_lines"),
    "list": ("paths", "recursive", "file_types", "languages", "intents", "limit", "after", "wait_timeout_ms"),
    "grep": ("pattern_type", "ignore_case", "context_lines_before", "context_lines_after", "max_matches", "cursor",
             "corpus", "globs", "wait_timeout_ms"),
}


def run(args, environ=None, stdout=None, stderr=None) -> int:
    """``syberlabs githits ...``: 0 a result, 2 refused (an excluded target or a blocked result), 1 an error."""
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    op = args.githits_command
    options = {name: getattr(args, name) for name in _OPTIONS[op]}
    try:
        client = Client.from_env(environ, timeout=args.timeout)
        if op == "search":
            request = search_request(args.query, args.targets, **options)
        elif op == "example":
            request = example_request(args.query, **options)
        elif op == "read":
            request = read_request(args.target, **options)
        elif op == "list":
            request = list_request(args.target, **options)
        else:
            request = grep_request(args.pattern, args.targets, **options)
        result = client.call(request)
    except GitHitsError as exc:
        stderr.write(f"error: {exc}\n")
        return 1
    if args.json:
        stdout.write(json.dumps({**{key: getattr(result, key) for key in Result._fields}, "refused": result.refused},
                                ensure_ascii=False, sort_keys=True) + "\n")
    elif not result.refused:
        stdout.write(render(result) + "\n")
    if result.refused:
        stderr.write(f"refused: {result.status}: {result.reason}\n")
        return 2
    return 0
