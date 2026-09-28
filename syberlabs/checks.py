"""Run a contract's checks against one candidate commit.

Each candidate gets a detached temporary worktree outside the repository.
Checks run in declaration order, as argv lists without a shell, with a scrubbed
environment, a per-check timeout, and an output cap. Output goes to a file,
not memory; the record keeps a SHA-256 of all of it and a short tail.

This is a controlled environment, not a sandbox. The commands are the
project's own and run with the developer's permissions against candidate code.
"""

from __future__ import annotations

import hashlib
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from syberlabs.evolution import MAX_TAIL_CHARS
from syberlabs.gitspace import Repo

EVALUATOR = "syberlabs.checks/1"
PASS_ENV = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR")


def _environment(home: Path, candidate: str) -> dict:
    env = {key: os.environ[key] for key in PASS_ENV if key in os.environ}
    env.update({"HOME": str(home), "TMPDIR": str(home), "TEMP": str(home), "TMP": str(home),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "CI": "1",
                "SYBERLABS_CANDIDATE": candidate})
    return env


def _stop(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def run_check(argv: list[str], cwd: Path, env: dict, timeout: int, max_output: int, log: Path) -> dict:
    started = time.monotonic()
    state, code = "error", None
    with open(log, "wb") as out:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=hasattr(os, "setsid"))
        except OSError as exc:
            out.write(f"could not start {argv[0]!r}: {exc.strerror or exc}\n".encode())
        else:
            deadline = started + timeout
            while True:
                try:
                    code = proc.wait(timeout=0.05)
                    state = "passed" if code == 0 else "failed"
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() > deadline:
                    _stop(proc)
                    state, code = "timed_out", None
                    break
                if log.stat().st_size > max_output:
                    _stop(proc)
                    break
            if state != "timed_out" and log.stat().st_size > max_output:
                # Checked while running and again at exit: a fast writer can finish between polls.
                state, code = "error", None
                out.write(f"\n[syberlabs] output exceeded {max_output} bytes; the check does not count\n".encode())
    sha = hashlib.sha256()
    size = log.stat().st_size
    with open(log, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            sha.update(chunk)
        handle.seek(max(0, size - MAX_TAIL_CHARS * 4))
        tail = handle.read().decode(errors="replace")[-MAX_TAIL_CHARS:]
    return {"state": state, "exit_code": code, "duration_ms": int((time.monotonic() - started) * 1000),
            "output_digest": sha.hexdigest(), "output_tail": tail}


def evaluate(repo: Repo, candidate: dict, evaluation: dict) -> dict:
    """Return an evaluation record body for ``Session.record_evaluation``."""
    results = []
    with tempfile.TemporaryDirectory(prefix="syberlabs-home-") as home, repo.worktree(candidate["commit"]) as tree:
        env = _environment(Path(home), candidate["id"])
        for name, check in evaluation["checks"].items():
            log = Path(home) / f"{name}.log"
            found = run_check(check["argv"], tree, env, check["timeout_seconds"], evaluation["max_output_bytes"], log)
            results.append({"name": name, **found})
    return {"candidate": candidate["id"], "commit": candidate["commit"], "tree": candidate["tree"],
            "evaluator": EVALUATOR, "checks": results}


def environment_note() -> str:
    return f"{platform.system()} {platform.machine()} python {sys.version.split()[0]}"
