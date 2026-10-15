"""Read pinned public dependency source through the GitHits v1 REST API.

Only public package locators go out. Credentials stay in the environment and
are never included in evidence. A failed or mismatched read is not context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.githits.dev"
MAX_RESPONSE = 256_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_request(path: str, params: dict, *, token: str | None = None) -> dict:
    url = API + path + "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "X-GitHits-Client-Name": "SyberWork-benchmark",
               "X-GitHits-Client-Version": "1"}
    credential = os.environ.get("GITHITS_API_TOKEN") if token is None else token
    if credential:
        headers["Authorization"] = "Bearer " + credential
    started = time.monotonic()
    record = {"url": url, "at": datetime.now(timezone.utc).isoformat(), "authenticated": bool(credential)}
    try:
        response = urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url, headers=headers), timeout=45)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, TimeoutError, OSError):
        return {**record, "status": None, "error": "transport_error",
                "seconds": round(time.monotonic() - started, 3)}
    with response:
        body = response.read(MAX_RESPONSE + 1)
        record.update(status=response.code, request_id=response.headers.get("X-Request-ID"),
                      retry_after=response.headers.get("Retry-After"), body_sha256=digest(body),
                      seconds=round(time.monotonic() - started, 3))
    if len(body) > MAX_RESPONSE:
        return {**record, "error": "response_too_large"}
    try:
        record["body"] = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        record["error"] = "invalid_json"
    return record


def request(path: str, params: dict, *, token: str | None = None) -> dict:
    """Run one HTTP lookup through the approved private CLI accounting wrapper.

    Upstream adopted this runtime after the historical baseline runs. One
    wrapper invocation contains one lookup, but its ledger unit is CLI, not
    an API request metric. No direct fallback is allowed when it is missing.
    """
    started = time.monotonic()
    record = {"url": API + path + "?" + urllib.parse.urlencode(params),
              "at": datetime.now(timezone.utc).isoformat(), "transport": "private-accounted-cli",
              "authenticated": bool(token or os.environ.get("GITHITS_API_TOKEN"))}
    node, home = os.environ.get("GITHITS_USAGE_NODE", ""), os.environ.get("GITHITS_USAGE_HOME", "")
    runtime = Path(home) / "usage.mjs"
    if not node or not home or not Path(node).is_absolute() or not Path(node).is_file() or not runtime.is_file():
        return {**record, "status": None, "error": "accounting_runtime_unavailable", "seconds": 0.0}
    env = dict(os.environ)
    if token is not None:
        env["GITHITS_API_TOKEN"] = token
    env["SYBERLABS_GITHITS_ACCOUNTED"] = "1"
    env["GITHITS_USAGE_ARGS_JSON"] = json.dumps({"path": path, "params": params})
    argv = [node, str(runtime), "exec", "--db", str(Path(home) / "data/usage.sqlite"),
            "--repo", "SyberLabs/SyberWork", "--caller", "test-harness",
            "--reason", "GitHits verified dependency benchmark", "--endpoint", path,
            "--", sys.executable, str(Path(__file__).resolve()), "--accounted-request"]
    try:
        done = subprocess.run(argv, input=json.dumps({"path": path, "params": params}).encode(),
                              capture_output=True, env=env, timeout=60)
        # A nonzero HTTP outcome is also a failed CLI invocation in the private ledger.
        if done.returncode not in (0, 1) or len(done.stdout) > MAX_RESPONSE * 2:
            raise ValueError("accounting_failed")
        found = json.loads(done.stdout)
        if not isinstance(found, dict) or found.get("url") != record["url"] or "status" not in found:
            raise ValueError("accounting_failed")
        return {**found, "transport": record["transport"]}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {**record, "status": None, "error": "accounting_failed",
                "seconds": round(time.monotonic() - started, 3)}


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_read(body: dict, fixture: dict, read: dict) -> str:
    """Fail closed on fallback versions, missing identity, binary or empty data."""
    resolution = body.get("target_resolution", {})
    served = resolution.get("served") or {}
    identity = served
    # Repository-backed reads may omit package fields on the served artifact.
    # Bind it to the exact resolved package through both repository and commit.
    if served.get("registry") is None and served.get("name") is None:
        resolved = resolution.get("resolved_requested") or {}
        requested = resolution.get("requested") or {}
        if (resolution.get("freshness") != "current"
                or requested.get("registry") != "pypi"
                or normalize_name(str(requested.get("name"))) != normalize_name(fixture["package"])
                or requested.get("version") != fixture["version"]
                or not served.get("repo_url")
                or served.get("repo_url") != resolved.get("repo_url")
                or not re.fullmatch(r"[0-9a-f]{40}", str(served.get("commit_sha")))
                or served.get("commit_sha") != resolved.get("commit_sha")
                or resolved.get("version") != fixture["version"]):
            raise ValueError("unverified_pinned_context")
        identity = resolved
    content = body.get("content", {})
    if (body.get("kind") != "code" or body.get("is_binary") is not False
            or identity.get("registry") != "pypi"
            or normalize_name(str(identity.get("name"))) != normalize_name(fixture["package"])
            or served.get("version") != fixture["version"]
            or body.get("path") != read["path"]
            or not isinstance(content.get("body"), str) or not content["body"].strip()
            or type(content.get("start_line")) is not int or type(content.get("end_line")) is not int
            or content["start_line"] < read["start_line"] or content["end_line"] > read["end_line"]
            or content["end_line"] < content["start_line"]):
        raise ValueError("unverified_pinned_context")
    return content["body"]


def collect(fixture: dict) -> dict:
    records, excerpts = [], []
    target = f"pypi:{normalize_name(fixture['package'])}@{fixture['version']}"
    for read in fixture["reads"]:
        found = request("/v1/read", {"target": target, **read, "wait_timeout_ms": 20000})
        records.append(found)
        if found.get("status") != 200 or found.get("error"):
            code = found.get("body", {}).get("code", found.get("error", "http_error"))
            return {"status": "unavailable", "reason": code, "requests": records, "context": None}
        try:
            text = validate_read(found["body"], fixture, read)
        except (ValueError, TypeError, AttributeError):
            return {"status": "unavailable", "reason": "unverified_pinned_context", "requests": records, "context": None}
        excerpts.append({"target": target, "path": read["path"], "content": text,
                         "target_resolution": found["body"]["target_resolution"],
                         "start_line": found["body"]["content"]["start_line"],
                         "end_line": found["body"]["content"]["end_line"]})
    return {"status": "ready", "reason": None, "requests": records, "context": excerpts}


if __name__ == "__main__":
    if sys.argv[1:] != ["--accounted-request"] or os.environ.get("SYBERLABS_GITHITS_ACCOUNTED") != "1":
        sys.exit("Invoke this lookup through the private usage wrapper.")
    payload = json.load(sys.stdin)
    if payload.get("path") != "/v1/read" or not isinstance(payload.get("params"), dict):
        sys.exit("Only a public dependency read is supported.")
    result = _http_request(payload["path"], payload["params"])
    print(json.dumps(result))
    sys.exit(0 if result.get("status") == 200 and not result.get("error") else 1)
