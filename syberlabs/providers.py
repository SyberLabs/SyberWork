"""Built-in search providers. Each only proposes; the host records and judges.

- ``PatchProvider`` submits one given edit: a person's patch, or the files a
  coding tool already changed in the working tree.
- ``FunctionProvider`` wraps a Python callable that receives the search space.
- ``CommandProvider`` runs an external program with a JSON request on stdin
  and reads JSON candidates from stdout. This is the model seam: an adapter
  for any model provider can be a command, and SyberLabs imports no model SDK.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.search import SearchSpace

PROTOCOL = "syberlabs.search/v0alpha1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class PatchProvider:
    operators = ("patch",)

    def __init__(self, changes: Mapping[str, str | None], message: str = "", *, name: str = "patch",
                 revision: str = "1", signal: Mapping | None = None):
        self.changes, self.message, self.signal = dict(changes), message, signal
        self.name, self.revision = name, revision

    def search(self, space: SearchSpace) -> Sequence[str]:
        return [space.submit(self.changes, operator="patch", message=self.message, signal=self.signal).id]


class FunctionProvider:
    def __init__(self, fn: Callable[[SearchSpace], Sequence[str] | None], *, name: str, revision: str,
                 operators: Sequence[str] = ("patch",)):
        self.fn, self.name, self.revision, self.operators = fn, name, revision, tuple(operators)

    def search(self, space: SearchSpace) -> Sequence[str] | None:
        return self.fn(space)


class CommandProvider:
    """Run ``argv`` once. It inherits the caller's environment, so it can read its own API keys.

    Request (stdin): ``{protocol, objective, base, scope, budget, context, files}``.
    Response (stdout): ``{"candidates": [{"changes": {path: text or null}, "message": str,
    "signal": object or null}], "recommended": [index, ...]}``.
    """

    operators = ("patch",)

    def __init__(self, argv: Sequence[str], *, name: str = "command", revision: str = "1", timeout: int = 300,
                 cwd: str | Path | None = None):
        if not argv or any(not isinstance(arg, str) for arg in argv):
            raise Rejected("invalid_provider", "argv must be a non-empty list of strings")
        self.argv, self.name, self.revision, self.timeout, self.cwd = list(argv), name, revision, timeout, cwd

    def search(self, space: SearchSpace) -> Sequence[str]:
        budget = space.remaining()
        request = {
            "protocol": PROTOCOL, "objective": space.objective, "base": space.base, "scope": list(space.scope),
            "budget": {"max_candidates": budget.max_candidates, "max_seconds": budget.max_seconds},
            "context": [dict(item) for item in space.context],
            "files": space.files()[:2000],
        }
        response = self._run(json.dumps(request).encode())
        if not isinstance(response, dict) or not isinstance(response.get("candidates"), list):
            raise Rejected("provider_shape", "the command must print {\"candidates\": [...]}")
        submitted = []
        for item in response["candidates"][: budget.max_candidates]:
            if (not isinstance(item, dict) or not isinstance(item.get("changes"), dict)
                    or any(not isinstance(k, str) or not (v is None or isinstance(v, str)) for k, v in item["changes"].items())):
                raise Rejected("provider_shape", "each candidate needs changes: {path: text or null}")
            message = item.get("message") if isinstance(item.get("message"), str) else ""
            signal = item.get("signal") if isinstance(item.get("signal"), dict) else None
            submitted.append(space.submit(item["changes"], operator="patch", message=message[:500], signal=signal).id)
        chosen = response.get("recommended", [])
        if not isinstance(chosen, list):
            return []
        return [submitted[i] for i in chosen if type(i) is int and 0 <= i < len(submitted)]

    def _run(self, request: bytes):
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=self.cwd,
                                        env=dict(os.environ), start_new_session=hasattr(os, "setsid"))
            except OSError as exc:
                raise Rejected("provider_unavailable", f"could not start {self.argv[0]!r}: {exc.strerror}") from None
            try:
                proc.stdin.write(request)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            deadline = time.monotonic() + self.timeout
            while proc.poll() is None:
                if time.monotonic() > deadline or os.fstat(out.fileno()).st_size > MAX_RESPONSE_BYTES:
                    proc.kill()
                    proc.wait()
                    raise Rejected("provider_timeout", "the provider command exceeded its time or output limit")
                time.sleep(0.02)
            if proc.returncode != 0:
                err.seek(0)
                raise Rejected("provider_failed", f"exit {proc.returncode}: {err.read(400).decode(errors='replace')}")
            out.seek(0)
            data = out.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise Rejected("provider_timeout", "the provider response exceeded its output limit")
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise Rejected("provider_shape", "the command did not print JSON") from None
