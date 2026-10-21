"""Run a contract's checks against one candidate commit.

Each candidate gets a detached temporary worktree outside the repository.
Checks run in canonical name order (contracts are stored as canonical JSON, so
this is sorted order), as argv lists without a shell, with a scrubbed
environment, a per-check timeout, and an output cap. Output goes to a file,
not memory; the record keeps a SHA-256 of all of it and a short tail.

A check named ``setup`` or ``install`` prepares the worktree (``npm ci``,
``cargo fetch``, a code generator): the fresh worktree has no ``node_modules``
or build output, and name order runs it before ``tests``. When it does not
pass, the checks after it are not started and are recorded ``error`` with the
tail ``setup_failed``, so a missing dependency tree is one failure, not one per
check. The frozen ``v0alpha1`` evaluation schema has no ``setup`` field, which
is why the step is a check by name (``spec/GAPS.md``, 31).

The scrubbed environment keeps the variables package managers use to find
their caches (``APPDATA``, ``LOCALAPPDATA`` and ``USERPROFILE`` on Windows,
``XDG_CACHE_HOME`` on POSIX, and the explicit ``npm_config_cache``,
``CARGO_HOME``, ``RUSTUP_HOME``, ``GOPATH``, ``GOCACHE`` and ``GOMODCACHE``
when set), so a candidate does not re-download every package. ``HOME`` is
still the temporary home.

A check whose name is ``critic`` or starts with ``critic.`` is a critic
(``syberlabs.critic``): it reads a JSON request on stdin, its stdout must be a
findings document, and the host parses that document, checks that the reviewer
is not the candidate's provider, and hands the parsed documents back through
``documents`` so the Build Thread can record them as observations.

This is a controlled environment, not a sandbox. The commands are the
project's own and run with the developer's permissions against candidate code.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from syberlabs import critic
from syberlabs.evolution import MAX_TAIL_CHARS
from syberlabs.gitspace import Repo

EVALUATOR = "syberlabs.checks/1"
WINDOWS = os.name == "nt"
# The cache allowance: where npm, cargo, rustup and go keep downloads. Passed through only when
# set; HOME, TMP and TEMP still point at the temporary home.
CACHE_ENV = ("npm_config_cache", "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOCACHE", "GOMODCACHE")
PASS_ENV = (("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR")
            + (("APPDATA", "LOCALAPPDATA", "USERPROFILE") if WINDOWS else ("XDG_CACHE_HOME",)) + CACHE_ENV)
# A check with one of these names prepares the worktree; the checks after it depend on it.
SETUP_NAMES = ("setup", "install")
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def _environment(home: Path, candidate: str) -> dict:
    env = {key: os.environ[key] for key in PASS_ENV if key in os.environ}
    env.update({"HOME": str(home), "TMPDIR": str(home), "TEMP": str(home), "TMP": str(home),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "CI": "1",
                "SYBERLABS_CANDIDATE": candidate})
    return env


def _stop(proc: subprocess.Popen) -> None:
    """End the check and everything it started: the session on POSIX, the process tree on Windows."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        elif WINDOWS:
            # ``proc.kill`` ends one process; npm, cargo and go leave their children running.
            taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run([str(taskkill) if taskkill.exists() else "taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=False)
            proc.kill()
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        pass
    proc.wait()


def run_check(argv: list[str], cwd: Path, env: dict, timeout: int, max_output: int, log: Path,
              stdin: Path | None = None) -> dict:
    """Run one check. ``stdin`` is a file handed to the command's standard input; without one it reads nothing."""
    started = time.monotonic()
    state, code = "error", None
    with open(log, "wb") as out, (open(stdin, "rb") if stdin else open(os.devnull, "rb")) as feed:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=feed, stdout=out,
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


def _review(log: Path, found: dict, candidate: dict) -> tuple | None:
    """Judge a critic's output. Returns ``(bytes, document)`` to observe, or None after marking the result."""
    if found["state"] != "passed":
        return None
    raw = log.read_bytes()
    try:
        document = critic.parse(raw)
    except critic.Invalid as exc:
        found.update(state="error", output_tail=exc.code)
        return None
    if not critic.independent(document, candidate["provider"]):
        found.update(state="error", output_tail="reviewer_not_independent")
        return None
    return raw, document


def evaluate(repo: Repo, candidate: dict, evaluation: dict, *, context: dict | None = None,
             documents: list | None = None) -> dict:
    """Return an evaluation record body for ``Session.record_evaluation``.

    ``context`` (objective, acceptance, scope) goes to critic checks on stdin. ``documents``, when
    given, receives ``(check name, stdout bytes, parsed document)`` for every critic whose document
    the host accepted; the caller records them (``critic.observe``). Callers that pass neither get
    today's behaviour, and a critic's result is still recorded like any check's.
    """
    results = []
    diff = None
    setup_failed = False
    with tempfile.TemporaryDirectory(prefix="syberlabs-home-") as home, repo.worktree(candidate["commit"]) as tree:
        env = _environment(Path(home), candidate["id"])
        for name, check in sorted(evaluation["checks"].items()):
            if setup_failed:
                results.append({"name": name, "state": "error", "exit_code": None, "duration_ms": 0,
                                "output_digest": EMPTY_DIGEST, "output_tail": "setup_failed"})
                continue
            log = Path(home) / f"{name}.log"
            feed = None
            if critic.is_critic(name):
                if diff is None:
                    text, size, _digest = repo.diff(candidate["base"], candidate["commit"], max_bytes=critic.MAX_DIFF_BYTES)
                    diff = text if size <= critic.MAX_DIFF_BYTES else text + f"\n[diff truncated: {size} bytes total]\n"
                feed = Path(home) / f"{name}.request.json"
                feed.write_bytes(json.dumps(critic.request(candidate, results, diff, context)).encode())
            found = run_check(check["argv"], tree, env, check["timeout_seconds"], evaluation["max_output_bytes"], log, stdin=feed)
            if critic.is_critic(name):
                reviewed = _review(log, found, candidate)
                if reviewed is not None and documents is not None:
                    documents.append((name, *reviewed))
            results.append({"name": name, **found})
            if name in SETUP_NAMES and found["state"] != "passed":
                setup_failed = True
    return {"candidate": candidate["id"], "commit": candidate["commit"], "tree": candidate["tree"],
            "evaluator": EVALUATOR, "checks": results}


def environment_note() -> str:
    return f"{platform.system()} {platform.machine()} python {sys.version.split()[0]}"
