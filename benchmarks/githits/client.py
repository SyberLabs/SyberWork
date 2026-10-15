"""Read pinned public dependency source through the GitHits v1 REST API.

Only public package locators go out. Credentials stay in the environment and
are never included in evidence. A failed or mismatched read is not context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.githits.dev"
MAX_RESPONSE = 256_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(path: str, params: dict, *, token: str | None = None) -> dict:
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


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_read(body: dict, fixture: dict, read: dict) -> str:
    """Fail closed on fallback versions, missing identity, binary or empty data."""
    served = body.get("target_resolution", {}).get("served", {})
    content = body.get("content", {})
    if (body.get("kind") != "code" or body.get("is_binary") is not False
            or served.get("registry") != "pypi"
            or normalize_name(str(served.get("name"))) != normalize_name(fixture["package"])
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
