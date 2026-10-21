"""The critic station: an independent reviewer run as a host check, with its findings in the record.

A critic is a check with a schema. The contract declares it like any other check under
``evolution.evaluation.checks``; the host treats a check as a critic when its name is ``critic``
or starts with ``critic.`` (the contract validator accepts no ``kind`` field yet; see
``spec/GAPS.md`` 28). The host runs the command in the candidate's worktree with a JSON request
on stdin (``request``) and reads a findings document from its stdout (``spec/finding.schema.json``).
Exit code 0 means the document is the result; any other exit code is a check ``error`` as today.

What the host checks itself, so that a review is a fact in the chain and not a claim in a
comment:

- the document parses and conforms (``parse``); otherwise the check is ``error`` with output
  ``findings_invalid:<path>``;
- the reviewer differs from the provider that produced the candidate (``independent``); a critic
  that names the candidate's own ``name@revision`` is ``error`` with ``reviewer_not_independent``;
- the parsed document is stored as an ``observed`` event with key ``critic`` and source
  ``check:<name>`` (``observe``), bounded to ``MAX_VALUE_BYTES``; ``findings_digest`` is the
  SHA-256 of the full stdout bytes, which is the same digest the evaluation record keeps for
  that check, so the observation and the check result name the same bytes.

Admission (``critic.no_high_finding``) and attribution (``Reviewed-by``) read the observation
through ``observation``; nothing reads the critic's free text to decide anything. The host
cannot see which program a provider ran, so independence is checked on the recorded
``name@revision`` and the value says ``independence: "declared"``.

This module imports nothing from ``build``, ``session`` or ``admission``; they import it.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from syberlabs.canonical import canonical

KEY = "critic"
HOST = "syberlabs.host"            # the same actor name build.HOST uses; critic may not import build
VERSION = 1
SEVERITIES = ("high", "medium", "low")
MAX_VALUE_BYTES = 65_536           # the observed value, canonical JSON
MAX_DOCUMENT_BYTES = 1_048_576     # a document the host will even try to parse
MAX_DIFF_BYTES = 1_000_000         # the unified diff handed to the critic on stdin
MAX_FINDINGS = 1000
MAX_TEXT = 2000
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PATH = re.compile(r"^[^\x00-\x1f\x7f]{1,4096}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def is_critic(name: str) -> bool:
    """A check is a critic by name: ``critic`` or ``critic.<reviewer>``."""
    return isinstance(name, str) and (name == "critic" or name.startswith("critic."))


def critic_checks(names) -> list[str]:
    return [name for name in names if is_critic(name)]


def request(candidate: dict, results: list[dict], diff: str, context: dict | None) -> dict:
    """What a critic reads on stdin. ``results`` are the checks that ran before it, in order."""
    context = context or {}
    return {
        "protocol": "syberlabs.critic/1",
        "candidate": {key: candidate.get(key) for key in ("id", "commit", "tree", "provider", "operator", "parents", "note")},
        "base": candidate.get("base"),
        "result": [dict(item) for item in results],
        "changed_paths": list(candidate.get("changed_paths", [])),
        "diff": diff,
        "objective": context.get("objective", ""),
        "contract": {"acceptance": context.get("acceptance", []), "scope": context.get("scope", {"paths": ["."], "exclude": []})},
    }


class Invalid(Exception):
    """The document does not conform. ``path`` is a slash-separated location in the document."""

    def __init__(self, path: str):
        super().__init__("findings_invalid:" + path)
        self.path = path

    @property
    def code(self) -> str:
        return "findings_invalid:" + self.path


def _text(value: Any, path: str, *, required: bool = True, max_length: int = MAX_TEXT) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > max_length or "\x00" in value:
        raise Invalid(path)
    return value


def parse(raw: bytes) -> dict:
    """The findings document in ``raw``, normalized, or ``Invalid`` naming the first bad field."""
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise Invalid("size")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Invalid("json") from None
    if not isinstance(document, dict):
        raise Invalid("document")
    if document.get("version") != VERSION:
        raise Invalid("version")
    reviewer = document.get("reviewer")
    if not isinstance(reviewer, dict) or set(reviewer) != {"name", "revision"}:
        raise Invalid("reviewer")
    if not isinstance(reviewer["name"], str) or not _NAME.match(reviewer["name"]):
        raise Invalid("reviewer/name")
    if not isinstance(reviewer["revision"], str) or not 0 < len(reviewer["revision"]) <= 128 or not reviewer["revision"].isprintable():
        raise Invalid("reviewer/revision")
    findings = document.get("findings")
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise Invalid("findings")
    cleaned = []
    for index, item in enumerate(findings):
        where = f"findings/{index}"
        if not isinstance(item, dict):
            raise Invalid(where)
        unknown = set(item) - {"severity", "file", "line", "claim", "falsifier", "acceptance_ref"}
        if unknown:
            raise Invalid(f"{where}/{sorted(unknown)[0]}")
        if item.get("severity") not in SEVERITIES:
            raise Invalid(where + "/severity")
        file = _text(item.get("file"), where + "/file", max_length=4096)
        if not _PATH.match(file):
            raise Invalid(where + "/file")
        line = item.get("line")
        if line is not None and (type(line) is not int or line < 0):
            raise Invalid(where + "/line")
        found = {"severity": item["severity"], "file": file, "line": line,
                 "claim": _text(item.get("claim"), where + "/claim"),
                 "falsifier": _text(item.get("falsifier"), where + "/falsifier")}
        if "acceptance_ref" in item:
            found["acceptance_ref"] = _text(item["acceptance_ref"], where + "/acceptance_ref", max_length=128)
        cleaned.append(found)
    summary = _text(document.get("summary"), "summary", required=False, max_length=4000)
    unknown = set(document) - {"version", "reviewer", "findings", "summary"}
    if unknown:
        raise Invalid(sorted(unknown)[0])
    return {"version": VERSION, "reviewer": {"name": reviewer["name"], "revision": reviewer["revision"]},
            "findings": cleaned, "summary": summary}


def counts(document: dict) -> dict:
    return {severity: sum(1 for f in document["findings"] if f["severity"] == severity) for severity in SEVERITIES}


def identity(party: dict) -> str:
    return f"{party['name']}@{party['revision']}"


def independent(document: dict, provider: dict) -> bool:
    """The host's own test of independence: the reviewer is not the provider that wrote the candidate."""
    return identity(document["reviewer"]) != identity(provider)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def value(candidate_id: str, check: str, document: dict, raw: bytes) -> dict:
    """The observed value, bounded to ``MAX_VALUE_BYTES``. Counts and the digest always cover the whole document."""
    found = {"candidate": candidate_id, "check": check, "reviewer": dict(document["reviewer"]),
             "counts": counts(document), "findings_digest": digest(raw), "independence": "declared",
             "truncated": False, "summary": document["summary"], "findings": list(document["findings"])}
    while len(canonical(found).encode()) > MAX_VALUE_BYTES:
        found["truncated"] = True
        if found["findings"]:
            found["findings"] = found["findings"][:-1]
        elif found["summary"]:
            found["summary"] = ""
        else:
            raise Invalid("size")   # a single finding is bounded well below the limit; this cannot happen
    return found


def observe(session, thread_id: str, body: dict, documents: list) -> list[dict]:
    """Write one ``observed`` event per parsed document, each versioned by the check's output digest.

    ``body`` is the evaluation record the host just stored and ``documents`` the list
    ``checks.evaluate`` filled: ``(check name, stdout bytes, parsed document)``. Only a document
    whose check passed and whose bytes match the recorded digest is observed; the record and the
    observation then name the same bytes.
    """
    results = {item["name"]: item for item in body["checks"]}
    events = []
    for name, raw, document in documents:
        result = results.get(name)
        if result is None or result["state"] != "passed" or result["output_digest"] != digest(raw):
            continue
        found = value(body["candidate"], name, document, raw)
        events.append(session.observe(thread_id, KEY, found, "check:" + name, result["output_digest"], HOST, verified=True))
    return events


def observation(history: list, candidate_id: str, check: str, output_digest: str) -> dict | None:
    """The host's critic observation for this check result, or None.

    Matched on the candidate, the check, and the digest of the check's output as the evaluation
    recorded it, and only when the host wrote it as verified. A later evaluation with different
    output has a different digest, so the observation found is the one for the evaluation asked about.
    """
    for event in reversed(history):
        if event.get("kind") != "observed":
            continue
        body = event["body"]
        found = body.get("value")
        if (body.get("key") == KEY and body.get("verified") is True and body.get("actor") == HOST
                and body.get("source") == "check:" + check and body.get("version") == output_digest
                and isinstance(found, dict) and found.get("candidate") == candidate_id
                and found.get("check") == check and isinstance(found.get("counts"), dict)
                and isinstance(found.get("reviewer"), dict)):
            return found
    return None


def reviews(history: list, view: dict) -> list[tuple[str, dict]]:
    """``(check, value)`` for every passed critic check in the view's latest evaluation that has an observation."""
    found = []
    for result in view["evaluation"]["checks"]:
        if is_critic(result["name"]) and result["state"] == "passed":
            seen = observation(history, view["id"], result["name"], result["output_digest"])
            if seen is not None:
                found.append((result["name"], seen))
    return found
