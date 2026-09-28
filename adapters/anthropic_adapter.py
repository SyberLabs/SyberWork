#!/usr/bin/env python3
"""A Claude model adapter for SyberLabs' provider seam.

    syberlabs propose --command "python adapters/anthropic_adapter.py"
    syberlabs propose --evolve  "python adapters/anthropic_adapter.py"

It reads one JSON request on stdin and prints one JSON response. It speaks the
two protocols the kit uses:

- ``syberlabs.search/v0alpha1`` (CommandProvider): propose one patch; an
  optional ``feedback`` object carries a parent candidate's files and failing
  check output for iterative repair;
- ``syberlabs.mutate/v0alpha1`` (CommandMutator): rewrite part of one parent.

The response carries ``usage`` (tokens and the model that served the call) so
the host can account cost. The model only proposes: whatever it returns is a
provisional candidate that the host scopes, evaluates, and a person accepts.

This file is outside the ``syberlabs`` package, which has no dependencies. It
uses the official SDK: ``pip install anthropic``, and credentials from the
environment (``ANTHROPIC_API_KEY``, or an ``ant auth login`` profile).

Environment: ``SYBERLABS_MODEL`` (default ``claude-opus-5``), ``SYBERLABS_EFFORT``
(default ``high``), ``SYBERLABS_MAX_FILE_BYTES`` (default 65536).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MODEL = os.environ.get("SYBERLABS_MODEL", "claude-opus-5")
EFFORT = os.environ.get("SYBERLABS_EFFORT", "high")
MAX_FILE_BYTES = int(os.environ.get("SYBERLABS_MAX_FILE_BYTES", "65536"))

SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "rationale": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "delete": {"type": "boolean"}},
                "required": ["path", "content", "delete"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["message", "rationale", "files"],
    "additionalProperties": False,
}

SYSTEM = (
    "You change a software repository to meet an objective. You propose; a separate host runs the project's "
    "checks and a person decides whether to accept. Return complete new contents for each file you change, "
    "only for files in the stated scope. Keep the change as small as the objective allows. Do not weaken tests "
    "or checks to make them pass. Set delete true (with empty content) only to remove a file. The rationale is "
    "one or two sentences a reviewer can check against the diff."
)


def _files(request: dict, cwd: Path) -> dict[str, str]:
    """Current text of the files the host listed, read from the base checkout, within a byte budget."""
    found, spent = {}, 0
    wanted = [item["path"] for item in request.get("context", []) if isinstance(item, dict) and "path" in item]
    wanted += [path for path in request.get("files", []) if isinstance(path, str)]
    for path in dict.fromkeys(wanted):
        file = (cwd / path).resolve()
        if cwd.resolve() not in file.parents or not file.is_file():
            continue
        data = file.read_bytes()
        if b"\0" in data[:8000] or spent + len(data) > MAX_FILE_BYTES:
            continue
        try:
            found[path] = data.decode()
        except UnicodeDecodeError:
            continue
        spent += len(data)
    return found


def build_prompt(request: dict, cwd: Path) -> str:
    """The user turn for either protocol. Pure, so it can be tested without the SDK."""
    protocol = request.get("protocol")
    if protocol == "syberlabs.mutate/v0alpha1":
        files = request.get("files", {})
        task = ("Make one focused change toward the objective, as a mutation of this candidate. "
                f"Candidate: {request.get('parent')}. Vary your approach; seed {request.get('seed')}.")
    elif protocol == "syberlabs.search/v0alpha1":
        feedback = request.get("feedback")
        if feedback:
            files = feedback.get("files", {})
            failing = "\n".join(f"--- {c['name']} ({c['state']})\n{c.get('output_tail', '')[-2000:]}"
                                for c in feedback.get("checks", []) if c.get("state") != "passed")
            task = ("The previous attempt below did not pass the host's checks. Fix it. Failing checks:\n" + failing)
        else:
            files = _files(request, cwd)
            task = "Propose one patch that meets the objective."
    else:
        raise ValueError(f"unknown protocol {protocol!r}")
    parts = [f"Objective: {request.get('objective')}", f"Mutable scope: {', '.join(request.get('scope', []))}", task]
    for path, text in files.items():
        parts.append(f"<file path={json.dumps(path)}>\n{text}\n</file>")
    return "\n\n".join(parts)


def to_changes(parsed: dict) -> dict:
    changes = {}
    for item in parsed.get("files", []):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            changes[item["path"]] = None if item.get("delete") else str(item.get("content", ""))
    return changes


def usage_of(response) -> dict:
    usage = response.usage
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None) or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
            "model": response.model, "stop_reason": response.stop_reason}


def call_model(prompt: str) -> tuple[dict | None, dict]:
    import anthropic  # imported here so the pure helpers above work without the SDK

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        # Server-side refusal fallback, routed by refusal category.
        extra_body={"fallbacks": "default"},
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = usage_of(response)
    if response.stop_reason != "end_turn":
        return None, usage  # refusal (after fallbacks) or truncation: no patch
    text = next((block.text for block in response.content if block.type == "text"), "")
    return json.loads(text), usage


def respond(request: dict, cwd: Path, call=call_model) -> dict:
    parsed, usage = call(build_prompt(request, cwd))
    changes = to_changes(parsed) if parsed else {}
    if request.get("protocol") == "syberlabs.mutate/v0alpha1":
        return {"changes": changes, "usage": usage}
    if not changes:
        return {"candidates": [], "recommended": [], "usage": usage}
    return {"candidates": [{"changes": changes, "message": parsed.get("message", "")[:500],
                            "signal": {"rationale": parsed.get("rationale", "")[:1000]}}],
            "recommended": [0], "usage": usage}


if __name__ == "__main__":
    print(json.dumps(respond(json.load(sys.stdin), Path.cwd())))
