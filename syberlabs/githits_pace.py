"""Host-side pacing of GitHits ``example`` calls: at most one per ``EXAMPLE_INTERVAL`` seconds on this host.

GitHits limits its example endpoint per organization to one or two requests a minute. The pilot runs both arms, and
any number of Claude Code calls, under one organization's token, so the limit is shared by every process that sends
an example. The host's launcher entry (``githits_cli_entry.py``) calls ``wait`` before an ``example`` is sent:

    paced_ms = wait(directory)   # blocks until this host's last example is EXAMPLE_INTERVAL seconds old

``wait`` takes a cross-process lock on ``<directory>/example.lock`` (``flock`` where ``fcntl`` exists,
``msvcrt.locking`` on Windows, as ``journal.Journal`` does), reads the time of the last example from
``<directory>/example.last``, sleeps until ``EXAMPLE_INTERVAL`` seconds have passed since it, writes the new time and
releases the lock. The lock is held while it sleeps, so callers queue and each slot is at least the interval after the
one before, across processes, threads, arms and homes. It returns the milliseconds the caller was held, lock wait
included, which the launcher records on the call's client record as ``paced_ms``; ``syberlabs experiment report``
leaves that time out of an arm's agent time.

The directory is the host's, never a worktree's: ``SYBERLABS_GITHITS_PACE_DIR`` when the operator sets it (an absolute
path), else ``syberlabs-githits-pace`` in the system temporary directory, shared by every process of this user. Only
``example`` is paced; search, read, list and grep are not. This is pacing before a request leaves, not a retry: the
client (``githits.py``) still sends each request once and never waits or retries on its own.

Which calls are paced (``interval_for``): every example to a GitHits base URL that is not a loopback host, at
``EXAMPLE_INTERVAL``, whatever the environment says. A loopback base is a stand-in (the client accepts ``http://``
only there; the tests' fake GitHits), whose calls never reach GitHits' organization limit, so it is not paced unless
``SYBERLABS_GITHITS_PACE_SECONDS`` sets an interval for it (the tests of this module do). That variable is ignored
for any other base.

Limits: the time file is plain text a process of the same user could rewrite, so it paces cooperating processes and
is no security boundary (a stale or future time is bounded: no call waits more than one interval for it). With neither
``fcntl`` nor ``msvcrt`` the lock is skipped, as the journal's is. Calls queued behind several others wait one interval
each, so a Bash tool timeout shorter than the queue ends the call before it is sent (the record then never exists and
the host flags the launcher call unrecorded).
"""

from __future__ import annotations

import errno
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - not Windows
    msvcrt = None

EXAMPLE_INTERVAL = 40.0                  # seconds between two example calls on this host (GitHits: 1-2 per minute)
PACED_OPS = ("example",)                 # the only operation paced
PACE_DIR_ENV = "SYBERLABS_GITHITS_PACE_DIR"
PACE_SECONDS_ENV = "SYBERLABS_GITHITS_PACE_SECONDS"  # an interval for a loopback stand-in only (tests); else ignored
DEFAULT_DIR_NAME = "syberlabs-githits-pace"
LOCK_FILE, STAMP_FILE = "example.lock", "example.last"
_BUSY = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)})


class PaceError(Exception):
    """The pacing directory cannot be used: the call is not sent."""


def directory(environ=None) -> Path:
    """The host's pacing directory: ``SYBERLABS_GITHITS_PACE_DIR`` (absolute), else the temporary-directory default."""
    environ = os.environ if environ is None else environ
    named = (environ.get(PACE_DIR_ENV) or "").strip()
    if named:
        path = Path(named)
        if not path.is_absolute():
            raise PaceError(f"{PACE_DIR_ENV} must be an absolute path outside any worktree")
        return path
    return Path(tempfile.gettempdir()) / DEFAULT_DIR_NAME


def interval_for(loopback: bool, environ=None) -> float | None:
    """The pacing interval for a call to a base URL that is (``loopback``) or is not a loopback host: always
    ``EXAMPLE_INTERVAL`` for GitHits itself; for a loopback stand-in, ``SYBERLABS_GITHITS_PACE_SECONDS`` when set,
    else None (not paced)."""
    if not loopback:
        return EXAMPLE_INTERVAL
    environ = os.environ if environ is None else environ
    text = (environ.get(PACE_SECONDS_ENV) or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise PaceError(f"{PACE_SECONDS_ENV} is a non-negative number of seconds") from None
    if not (value >= 0 and value != float("inf")):
        raise PaceError(f"{PACE_SECONDS_ENV} is a non-negative number of seconds")
    return value


@contextmanager
def _locked(path: Path):
    with open(path, "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if msvcrt is None:  # pragma: no cover - neither lock exists: unlocked, as the journal is
            yield
            return
        fd = handle.fileno()
        while True:  # LK_LOCK gives up after about ten seconds; a slot can take longer, so poll LK_NBLCK
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in _BUSY:
                    raise
                time.sleep(0.05)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _last(path: Path) -> float | None:
    try:
        value = float(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def wait(folder=None, *, interval: float = EXAMPLE_INTERVAL, clock=time.time, sleep=time.sleep) -> int:
    """Hold the caller until this host's last example slot is ``interval`` seconds old, take the next slot, and
    return how long the caller was held in milliseconds (lock wait included). ``clock`` and ``sleep`` are injectable
    for tests; the stamp is wall-clock seconds so that separate processes compare it."""
    if not (isinstance(interval, (int, float)) and interval >= 0):
        raise PaceError("the pacing interval is a non-negative number of seconds")
    folder = Path(folder) if folder is not None else directory()
    started = clock()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with _locked(folder / LOCK_FILE):
            last = _last(folder / STAMP_FILE)
            now = clock()
            if last is not None and now < last + interval:
                sleep(min(last + interval - now, interval))  # a stamp in the future never holds a call longer
                now = clock()
            stamp = folder / STAMP_FILE
            temporary = folder / f"{STAMP_FILE}.{os.getpid()}.tmp"
            temporary.write_text(repr(now), encoding="ascii")
            os.replace(temporary, stamp)
    except OSError as exc:
        raise PaceError(f"cannot pace the example call in {folder}: {exc.strerror or type(exc).__name__}") from None
    return max(0, int(round((clock() - started) * 1000)))
