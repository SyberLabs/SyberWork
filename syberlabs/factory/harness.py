"""Time: record a run once at full speed, replay it from its journal at any speed.

A ``Recorder`` owns a journal and a profile hook (``Recorder.hook``); ``record``
runs a program in a child process that installs that hook and writes one
JSON line per event to the journal: a ``run`` header, a ``call`` and a ``return``
for every Python function under the recorded root, an ``effect`` for every
value that crossed ``Context.run``, a ``marker`` for every value that must not
be recomputed (time, random, environment), and an ``end``. Every record carries
``seq``, a logical clock that counts events, and ``ns``, nanoseconds since the
run started. Replay paces ``seq``; wall time is for display.

``Context.run(name, fn, *args)`` is the effect boundary. Recording calls ``fn``
and journals its JSON-serializable result. Replaying does not call ``fn``: it
returns the next journaled result under that name, and raises ``Diverged`` when
the program asks for an effect the journal did not record next. Replay is
therefore the detector of a changed program before any trace diff runs.

The hook sees one thread. Threads the program starts are not recorded, and
nothing here is a sandbox. Files under the recorded root are mapped to
components with the Space graph's file mapping, so every call carries the
component it ran in; a file no component owns records ``component: null``.

Nothing in this file installs the hook. The repository's integrity rule
(``examples/critic_rules.py``, ``runner_reach``) refuses a trace or profile
hook in any file a test can import, because such a hook reaches the process
that runs the tests. So ``record`` starts a child interpreter with a bootstrap
string that builds the ``Recorder``, installs ``recorder.hook`` with
``sys.setprofile``, runs the entrypoint, and reports; the parent only reads the
journal the child wrote. A program that wants to record itself in process can
install ``recorder.hook`` the same way, outside this repository.

The child is the candidate's process, so nothing it says is taken on trust:

- it runs under ``python -I`` (no site customisation, no ``PYTHON*`` variables,
  no current directory on ``sys.path``) in a host-owned scratch directory that
  is outside the worktree and outside the ledger; the child is never told where
  the ledger is, and the parent moves the journal into the ledger afterwards;
- every journal record carries a seal, an HMAC chained from the previous
  record under a per-run key the parent generated; ``verify_journal`` refuses a
  journal with a record written around the recorder;
- the child's report comes back over a private pipe and must carry the run's
  nonce; a line printed to stdout is never a report, and the status in the
  report must agree with the ``end`` record the parent reads from the journal.

The residual is memory: code in the child can reach the recorder (and its key)
through ``sys.modules``, because io observation needs to run in the candidate's
process. That is recorded in ``spec/GAPS.md``; a sandbox is not claimed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from syberlabs._patterns import SECRET
from syberlabs.canonical import canonical, digest
from syberlabs.errors import Rejected

RECORDER_VERSION = "profile-0.3"
MAX_CHANNEL_BYTES = 4 * 1024 * 1024
MAX_TAIL_CHARS = 4000
# What the child observes without the program's cooperation. Each is a wrapper installed in the child before
# the run: a subprocess's argv and cwd, a socket's peer. Observed, journaled as ``io``, never replayed.
OBSERVERS = ("subprocess", "socket")
INLINE_PAYLOAD_BYTES = 65_536
MAX_RECORDS = 5_000_000
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
_CURRENT: list["Recorder"] = []


def current() -> "Context":
    """The context of the recorder that is running this program (``syber record`` sets it up); a script says
    ``ctx = current()`` and routes its effects through ``ctx.run``. Raises when no recorder is active.

    The child loads the host's harness under the private name ``_factory_harness`` so the program's own
    ``syberlabs`` import is never the recorder's; this looks there first."""
    private = sys.modules.get("_factory_harness")
    stack = getattr(private, "_CURRENT", None) if private is not None and private is not sys.modules.get(__name__) else None
    stack = stack or _CURRENT
    if not stack:
        raise Rejected("no_recorder", "no recorder is active; run under `python -m syberlabs.factory record`")
    return stack[-1].context


class Diverged(Rejected):
    """Replayed code asked for an effect the journal did not record next, or ran out of journal."""

    def __init__(self, seq: int, asked: str, journaled: str | None):
        detail = f"at seq {seq} the program asked for effect {asked!r}; the journal has {journaled!r}"
        super().__init__("replay_diverged", detail)
        self.seq = seq
        self.asked = asked
        self.journaled = journaled


_HEX = re.compile(r"^[0-9a-f]{7,64}$")
_HEX_INSIDE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{20,64}")
_PATHY = re.compile(r"[\\/]")
_TEMP = re.compile(r"^(tmp|syberlabs-[a-z]+-)[A-Za-z0-9_]{6,}$")


def mask(token: str) -> str:
    """A subprocess argument with its run-specific identity removed: a path becomes its basename, a hex id
    becomes ``<hex>``, a temporary name becomes ``<tmp>``, so two runs that reach for the same program the
    same way share an io key."""
    if _HEX.match(token):
        return "<hex>"
    if _TEMP.match(token):
        return "<tmp>"
    if _PATHY.search(token):
        base = token.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return "<path>/" + ("<hex>" if _HEX.match(base) else "<tmp>" if _TEMP.match(base) else _HEX_INSIDE.sub("<hex>", base))
    return _HEX_INSIDE.sub("<hex>", token)


REDACTED = "<redacted>"
_CREDENTIAL_KEY = re.compile(r"(?i)(token|secret|passw(or)?d|api[_-]?key|authorization|credential|bearer|extraheader|cookie)")
_CREDENTIAL_FLAGS = frozenset({"--token", "--password", "--passwd", "--secret", "--api-key", "--apikey", "--auth-token",
                               "--access-token", "--bearer", "-H", "--header", "--cookie"})
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)


def scrub(text):
    """``text`` with credential shapes removed before it is journaled: the kernel's shared credential pattern
    (``syberlabs._patterns.SECRET``, the one ``metrics`` and the query guard use), bearer tokens, PEM private
    keys, and the value of a ``KEY=value`` pair whose key names a credential (``GITHUB_TOKEN=``,
    ``http.extraheader=``). Anything that is not a string is returned as it is."""
    if not isinstance(text, str):
        return text
    text = _PEM.sub(REDACTED, _BEARER.sub("Bearer " + REDACTED, SECRET.sub(REDACTED, text)))
    head, sep, tail = text.partition("=")
    if sep and tail and " " not in head and _CREDENTIAL_KEY.search(head):
        return head + "=" + REDACTED
    return text


def scrub_argv(argv) -> list[str]:
    """A subprocess argv with credentials removed: the value after a credential flag (``--token X``, ``-H
    'Authorization: ...'``), the value of ``--flag=X`` for the same flags, and :func:`scrub` on the rest."""
    out = []
    redact_next = False
    for item in argv:
        item = str(item)
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        flag, sep, _value = item.partition("=")
        if item in _CREDENTIAL_FLAGS:
            out.append(item)
            redact_next = True
        elif sep and flag in _CREDENTIAL_FLAGS:
            out.append(flag + "=" + REDACTED)
        else:
            out.append(scrub(item))
    return out


def scrub_text(text: str, limit: int = MAX_TAIL_CHARS) -> str:
    """A bounded tail of free text (a test runner's output) with credential shapes removed, line by line."""
    tail = text[-limit:] if limit else text
    return "\n".join(scrub(line) for line in tail.split("\n"))


def new_run_id() -> str:
    """Time-ordered, unique: microseconds since the epoch in hex, then 8 random hex digits."""
    return f"{int(time.time() * 1_000_000):014x}{secrets.token_hex(4)}"


def _to_json(value):
    """A JSON-serializable form of an effect result. Anything else is stored as its repr with a flag."""
    try:
        canonical(value)
        return value, True
    except (TypeError, ValueError):
        return repr(value), False


class Context:
    """What the recorded program receives: the effect boundary and markers."""

    def __init__(self, recorder: "Recorder"):
        self._recorder = recorder

    def run(self, name: str, fn, *args, **kwargs):
        """Call ``fn`` and journal its result, or return the journaled result when replaying."""
        return self._recorder._effect(name, fn, args, kwargs)

    def marker(self, name: str, value=None, fn=None):
        """A value that must not be recomputed on replay: a time, a random draw, an environment read."""
        return self._recorder._marker(name, value, fn)


class Recorder:
    """Record (or replay) one run of a program under ``root`` into ``journal``.

    ``file_components`` maps posix-relative file paths to component ids (``space.index`` then
    ``space.map_components`` produce it as ``graph['files'][path]['component']``). ``replay_of`` is the
    path of a recorded journal; when set, effects and markers come from it instead of running."""

    def __init__(self, root: str | Path, journal: str | Path, file_components: dict | None = None, *,
                 run_id: str | None = None, revision: str | None = None, entrypoint: str | None = None,
                 fixture_digest: str | None = None, space_digest: str | None = None,
                 replay_of: str | Path | None = None, blobs: str | Path | None = None, key: str | None = None):
        self.root = Path(root).resolve()
        # The seal: each record's HMAC, chained from the previous record's, under the per-run key ``record``
        # generated in the parent. A line written to the journal file around the recorder has no valid seal.
        self._key = key.encode("ascii") if key else None
        self._mac = ""
        self._root_prefix = str(self.root) + os.sep
        self.journal = Path(journal)
        self.file_components = dict(file_components or {})
        self.run_id = run_id or new_run_id()
        self.revision = revision
        self.entrypoint = entrypoint
        self.fixture_digest = fixture_digest
        self.space_digest = space_digest
        self.blobs = Path(blobs) if blobs else None
        self.replay_of = Path(replay_of) if replay_of else None
        self._replay_effects: list[dict] = []
        self._replay_cursor = 0
        self._replayed_run: str | None = None
        self.seq = 0
        self.counts = {"call": 0, "effect": 0, "marker": 0, "io": 0}
        self._stack: list[tuple[int, object]] = []
        self._handle = None
        self._t0 = 0
        self._busy = False
        self.context = Context(self)
        self.status: str | None = None

    # -- lifecycle -------------------------------------------------------------------------------------------

    def __enter__(self) -> "Recorder":
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        if self.replay_of is not None:
            self._load_replay()
        self._handle = self.journal.open("w", encoding="utf-8", newline="\n")
        self._t0 = time.perf_counter_ns()
        header = {"t": "run", "seq": 0, "ns": 0, "run_id": self.run_id, "revision": self.revision,
                  "entrypoint": self.entrypoint, "fixture_digest": self.fixture_digest,
                  "space_digest": self.space_digest, "recorder": RECORDER_VERSION,
                  "replay_of": self._replayed_run, "started_at": int(time.time() * 1_000_000),
                  "root": self.root.name, "python": sys.version.split()[0], "sealed": self._key is not None}
        self._write(header)
        _CURRENT.append(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if _CURRENT and _CURRENT[-1] is self:
            _CURRENT.pop()
        self._busy = True
        if exc_type is None or (isinstance(exc, SystemExit) and exc.code in (None, 0)):
            status, exc_type = "ok", None  # a script's clean sys.exit() is a normal end
        else:
            status = "diverged" if isinstance(exc, Diverged) else "error"
        unconsumed = len(self._replay_effects) - self._replay_cursor if self.replay_of is not None else 0
        if status == "ok" and unconsumed:
            status = "diverged"  # the program asked for fewer effects than it did when recorded
        self.seq += 1
        self._write({"t": "end", "seq": self.seq, "ns": self._now(), "status": status,
                     "error": None if exc_type is None else f"{exc_type.__name__}: {str(exc)[:300]}",
                     "counts": dict(self.counts), "open_spans": len(self._stack), "unconsumed_effects": unconsumed})
        self.status = status
        self._handle.close()
        self._handle = None
        return False

    def _now(self) -> int:
        return time.perf_counter_ns() - self._t0

    def _write(self, record: dict) -> None:
        if self.seq > MAX_RECORDS:
            raise Rejected("journal_too_long", f"more than {MAX_RECORDS} records; stop the run")
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str)
        if self._key is not None:
            self._mac = _seal(self._key, self._mac, line)
            line = line[:-1] + ',"m":"' + self._mac + '"}'
        self._handle.write(line + "\n")

    # -- the profile hook ------------------------------------------------------------------------------------

    def hook(self, frame, event, arg) -> None:
        """A profile function. ``record`` installs it in the child it starts; nothing in this package does."""
        if self._busy or event not in ("call", "return"):
            return
        code = frame.f_code
        filename = code.co_filename
        if not filename.startswith(self._root_prefix) or filename.startswith(_SELF_DIR):
            return
        self._busy = True
        try:
            if event == "call":
                relpath = os.path.relpath(filename, self.root).replace(os.sep, "/")
                self.seq += 1
                self.counts["call"] += 1
                span = self.seq
                parent = self._stack[-1][0] if self._stack else None
                self._stack.append((span, frame))
                self._write({"t": "call", "seq": span, "ns": self._now(), "span": span, "parent": parent,
                             "component": self.file_components.get(relpath), "file": relpath,
                             "line": frame.f_lineno, "fn": code.co_qualname})
            else:
                # A frame returns in nesting order, so everything above it on the stack has already left or is
                # leaving with it. A frame the hook never saw enter (profiling started inside it) is ignored.
                depth = None
                for index in range(len(self._stack) - 1, -1, -1):
                    if self._stack[index][1] is frame:
                        depth = index
                        break
                if depth is not None:
                    while len(self._stack) > depth:
                        span, _ = self._stack.pop()
                        self.seq += 1
                        self._write({"t": "return", "seq": self.seq, "ns": self._now(), "span": span})
        finally:
            self._busy = False

    # -- effects and markers ---------------------------------------------------------------------------------

    def _effect(self, name: str, fn, args, kwargs):
        if self.replay_of is not None:
            return self._replay_next("effect", name)
        # The body of an effect is recorded (it ran) but marked, because replay does not run it: a worldline
        # compared across record and replay drops the calls between ``effect_begin`` and ``effect``.
        self._busy = True
        try:
            self.seq += 1
            self._write({"t": "effect_begin", "seq": self.seq, "ns": self._now(), "name": name,
                         "span": self._stack[-1][0] if self._stack else None})
        finally:
            self._busy = False
        value = fn(*args, **kwargs)
        self._journal_value("effect", name, value)
        return value

    def _marker(self, name: str, value, fn):
        if self.replay_of is not None:
            return self._replay_next("marker", name)
        if fn is not None:
            value = fn()
        self._journal_value("marker", name, value)
        return value

    def io(self, name: str, value, key=None) -> None:
        """An observation of the program reaching outside itself that nobody wrapped in ``ctx.run``: a subprocess
        started, a socket opened. Journaled as an ``io`` record whose digest is of ``key`` when given (the
        observation with its run-specific parts masked: temporary paths, commit hashes) and of ``value``
        otherwise; the payload keeps the real ``value``. Counted in the trace diff like an effect, never
        replayed (replay runs the program's own code, so the io happens again and is observed again). The
        child bootstrap installs the observers; see ``OBSERVERS``."""
        if self._handle is None:
            return
        self._journal_value("io", name, value, key)

    def _journal_value(self, kind: str, name: str, value, key=None) -> None:
        self._busy = True
        try:
            payload, native = _to_json(value)
            text = canonical(payload)
            sha = digest(payload if key is None else key)
            self.seq += 1
            self.counts[kind] += 1
            record = {"t": kind, "seq": self.seq, "ns": self._now(), "name": name, "digest": sha, "json": native,
                      "span": self._stack[-1][0] if self._stack else None}
            if len(text) <= INLINE_PAYLOAD_BYTES or self.blobs is None:
                record["payload"] = payload
            else:
                self.blobs.mkdir(parents=True, exist_ok=True)
                (self.blobs / sha).write_text(text, encoding="utf-8")
                record["blob"] = sha
            self._write(record)
        finally:
            self._busy = False

    def _load_replay(self) -> None:
        records = read_journal(self.replay_of)
        if not records or records[0].get("t") != "run":
            raise Rejected("journal_unreadable", f"{self.replay_of} has no run header")
        self._replayed_run = records[0]["run_id"]
        self._replay_effects = [r for r in records if r.get("t") in ("effect", "marker")]

    def _replay_next(self, kind: str, name: str):
        self._busy = True
        try:
            if self._replay_cursor >= len(self._replay_effects):
                raise Diverged(self.seq + 1, f"{kind}:{name}", None)
            recorded = self._replay_effects[self._replay_cursor]
            if recorded["t"] != kind or recorded["name"] != name:
                raise Diverged(self.seq + 1, f"{kind}:{name}", f"{recorded['t']}:{recorded['name']}")
            self._replay_cursor += 1
            if "payload" in recorded:
                payload = recorded["payload"]
            else:
                payload = json.loads((self.blobs / recorded["blob"]).read_text(encoding="utf-8"))
            self.seq += 1
            self.counts[kind] += 1
            self._write({"t": kind, "seq": self.seq, "ns": self._now(), "name": name, "digest": recorded["digest"],
                         "json": recorded.get("json", True), "span": self._stack[-1][0] if self._stack else None,
                         "payload": payload, "replayed": True})
            return payload
        finally:
            self._busy = False


# -- recording in a child process ----------------------------------------------------------------------------

def _seal(key: bytes, previous: str, line: str) -> str:
    """The seal of one journal line: HMAC-SHA256 over the previous seal and the line, under the run's key."""
    return hmac.new(key, (previous + line).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_journal(path: str | Path, key: str) -> str | None:
    """None when every line of the journal carries the seal the run's key gives it, else the first problem.
    A last line without a newline is torn (the child died writing it), which is a problem too: a sealed
    journal ends with its ``end`` record."""
    secret = key.encode("ascii")
    previous = ""
    text = Path(path).read_text(encoding="utf-8")
    if not text:
        return "the journal is empty"
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    else:
        return f"line {len(lines)} is torn"
    for index, line in enumerate(lines, 1):
        body, sep, tail = line.rpartition(',"m":"')
        if not sep or len(tail) != 34 or not tail.endswith('"}'):
            return f"line {index} carries no seal"
        expected = _seal(secret, previous, body + "}")
        if not hmac.compare_digest(expected, tail[:32]):
            return f"line {index} was not written by this run's recorder"
        previous = expected
    return None


# The child's whole program. It is a string so that no importable file in this repository installs a profile
# hook (see the module docstring). The parent hands it one JSON object on stdin; it answers one JSON line over
# the private channel, never on stdout. Everything lives in one function so that the program under test finds
# no configuration, key or channel in ``__main__``; what it can still reach is the recorder itself.
BOOTSTRAP = r"""
def _main():
    import importlib, importlib.util, json, os, runpy, sys
    config = json.loads(sys.stdin.read())
    if os.name == "nt":
        import msvcrt
        channel_fd = msvcrt.open_osfhandle(config["channel"], os.O_WRONLY)
        os.set_handle_inheritable(config["channel"], False)  # a process the program starts does not get it
    else:
        channel_fd = config["channel"]
        os.set_inheritable(channel_fd, False)
    channel = os.fdopen(channel_fd, "wb", buffering=0)
    nonce = config["nonce"]
    # The recorder is the host's, loaded by file under a private name; the program under test then imports
    # whatever ``syberlabs`` its own tree holds (on this repository, the candidate's). The two never share a name.
    sys.path.insert(0, config["syberlabs_parent"])
    spec = importlib.util.spec_from_file_location("_factory_harness", config["harness_path"])
    harness = importlib.util.module_from_spec(spec)
    sys.modules["_factory_harness"] = harness
    spec.loader.exec_module(harness)
    for name in [n for n in sys.modules if n == "syberlabs" or n.startswith("syberlabs.")]:
        del sys.modules[name]
    sys.path.remove(config["syberlabs_parent"])
    sys.path.insert(0, config["root"])
    recorder = harness.Recorder(config["root"], config["journal"], config["file_components"], run_id=config["run_id"],
                                revision=config["revision"], entrypoint=config["entry"], fixture_digest=config["fixture_digest"],
                                space_digest=config["space_digest"], replay_of=config["replay_of"], blobs=config["blobs"],
                                key=config["key"])
    report = {"n": nonce, "run_id": config["run_id"], "status": None, "error": None, "outcome": None, "counts": None}
    if "subprocess" in config["observe"]:
        import subprocess as _sp
        _popen_init = _sp.Popen.__init__
        def _observed_init(self, args, *rest, **kwargs):
            argv = harness.scrub_argv([str(a) for a in (args if isinstance(args, (list, tuple)) else [args])][:64])
            recorder.io("subprocess", {"argv": argv, "cwd": harness.scrub(str(kwargs.get("cwd") or ""))},
                        key={"argv": [harness.mask(a) for a in argv]})
            return _popen_init(self, args, *rest, **kwargs)
        _sp.Popen.__init__ = _observed_init
    if "socket" in config["observe"]:
        import socket as _so
        _connect = _so.socket.connect
        def _observed_connect(self, address):
            recorder.io("socket.connect", {"address": harness.scrub(str(address))})
            return _connect(self, address)
        _so.socket.connect = _observed_connect
    function = config["function"]
    module_name, script, fixture = config["module"], config["script"], config["fixture"]
    del config
    try:
        with recorder:
            if function:
                # The import is not the run: resolve the function first, then record only the call.
                target = getattr(importlib.import_module(module_name), function)
            sys.setprofile(recorder.hook)
            try:
                if function:
                    outcome = target(recorder.context, fixture)
                    try:
                        json.dumps(outcome)
                        report["outcome"] = outcome
                    except (TypeError, ValueError):
                        report["outcome"] = repr(outcome)[:2000]
                else:
                    runpy.run_path(script, run_name="__main__")
            finally:
                sys.setprofile(None)
    except harness.Diverged as exc:
        report["error"] = exc.detail
    except SystemExit as exc:
        if exc.code not in (None, 0):
            report["error"] = "exited with " + str(exc.code)
    except BaseException as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)[:300]
    report["status"] = recorder.status
    report["counts"] = recorder.counts
    channel.write((json.dumps(report) + "\n").encode("utf-8"))
    channel.close()
_main()
"""


class _Channel:
    """The parent's end of the private pipe: the child inherits only the write end, a thread collects the
    lines, and ``report`` accepts exactly one message that carries this run's nonce."""

    def __init__(self):
        self._read, self._write = os.pipe()
        self.lines: list[bytes] = []
        self.overflow = False
        if os.name == "nt":
            import msvcrt
            self.handle = msvcrt.get_osfhandle(self._write)
            os.set_handle_inheritable(self.handle, True)
            info = subprocess.STARTUPINFO()
            info.lpAttributeList = {"handle_list": [self.handle]}  # only this pipe is inherited, beside the std handles
            self.popen_kwargs = {"startupinfo": info}
        else:
            self.handle = self._write
            self.popen_kwargs = {"pass_fds": (self._write,)}
        self._reader = threading.Thread(target=self._collect, daemon=True)

    def started(self) -> None:
        """The child holds the write end now (or never will): drop the parent's copy and start reading."""
        os.close(self._write)
        self._reader.start()

    def _collect(self) -> None:
        total = 0
        with os.fdopen(self._read, "rb") as stream:
            for line in stream:
                total += len(line)
                if total > MAX_CHANNEL_BYTES:
                    self.overflow = True
                    continue
                self.lines.append(line)

    def report(self, nonce: str) -> tuple[dict | None, str | None]:
        """The one report bound to ``nonce``, or the reason there is none."""
        self._reader.join(timeout=30)
        if self._reader.is_alive():
            return None, "the channel stayed open after the child exited"
        if self.overflow:
            return None, f"the channel carried more than {MAX_CHANNEL_BYTES} bytes"
        if len(self.lines) != 1:
            return None, f"the channel carried {len(self.lines)} messages, not one"
        try:
            message = json.loads(self.lines[0])
        except ValueError:
            return None, "the message is not JSON"
        if not isinstance(message, dict) or not hmac.compare_digest(str(message.get("n")), nonce):
            return None, "the message does not carry this run's nonce"
        message.pop("n", None)
        return message, None


def _tail(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - MAX_TAIL_CHARS * 4))
            return handle.read().decode("utf-8", errors="replace")[-MAX_TAIL_CHARS:]
    except OSError:
        return ""


def record(root: str | Path, journal: str | Path, entry: str, *, file_components: dict | None = None,
           fixture: dict | None = None, run_id: str | None = None, revision: str | None = None,
           space_digest: str | None = None, replay_of: str | Path | None = None, blobs: str | Path | None = None,
           python: str | None = None, timeout: float = 3600.0, observe: tuple = OBSERVERS,
           strip_env: tuple = ()) -> dict:
    """Run ``entry`` under the recorder in an isolated child interpreter and return its report.

    ``entry`` is ``module:function`` (imported from ``root``, called as ``function(ctx, fixture)``) or a script
    path (run as ``__main__``; it takes its context from :func:`current`). The report has ``run_id``, ``status``
    (``ok``, ``diverged``, ``error``, or ``killed`` on timeout), ``counts``, ``outcome``, ``error``, and the tails
    of ``stdout`` and ``stderr``. ``observe`` names the io observers the child installs (default: subprocesses
    and socket connections); pass ``()`` for none.

    The child runs ``python -I`` in a host-owned scratch directory and writes its journal and blobs there; it is
    never told where ``journal`` or ``blobs`` are. Afterwards the parent verifies the journal's seal under the
    per-run key, reads the ``end`` record itself, requires the child's report (one message on a private pipe,
    bound by the run's nonce) to agree with it, and only then moves the journal to ``journal`` and the blobs to
    ``blobs``. A journal that fails its seal is not delivered, and neither is the journal of a run that was
    killed. On timeout the whole process tree is stopped.

    The child runs under the Build Thread's scrubbed environment (``checks.scrubbed_environment``: ``PATH``,
    the locale, the package managers' cache variables, a temporary ``HOME`` and ``TMP`` inside the scratch
    directory, ``SYBERLABS_CANDIDATE`` and ``SYBERLABS_BASE`` as the host has them), never this process's
    whole environment: the host's signing seed (``certificate.DEFAULT_SEED_ENV``) and every variable named in
    ``strip_env`` (a ``--seed-env``) are not in it, whatever the host holds. ``-I`` drops the ``PYTHON*``
    variables besides. This is still not a sandbox."""
    from syberlabs.certificate import DEFAULT_SEED_ENV
    from syberlabs.checks import scrubbed_environment, stop_tree  # the Build Thread's own; parent side only

    root = Path(root).resolve()
    destination = Path(journal).resolve()
    run_id = run_id or new_run_id()
    module = function = script = None
    if ":" in entry and not Path(entry).exists():
        module, function = entry.rsplit(":", 1)
    else:
        script = str(Path(entry).resolve())
        if not Path(script).exists():
            raise Rejected("entry_missing", f"{entry} is neither module:function nor a file")
    nonce, key = secrets.token_hex(16), secrets.token_hex(32)
    report = {"run_id": run_id, "status": "error", "error": None, "counts": None, "outcome": None,
              "stdout": "", "stderr": "", "returncode": None}
    scratch = Path(tempfile.mkdtemp(prefix="syberlabs-factory-"))  # host-owned: outside the worktree and the ledger
    try:
        child_home = scratch / "home"
        child_home.mkdir()
        env = scrubbed_environment(child_home, os.environ.get("SYBERLABS_CANDIDATE") or run_id, os.environ.get("SYBERLABS_BASE"))
        for name in (DEFAULT_SEED_ENV, *strip_env):
            env.pop(name, None)
        child_journal = scratch / "journal.jsonl"
        child_blobs = scratch / "blobs" if blobs is not None else None
        child_replay = None
        if replay_of is not None:
            child_replay = scratch / "replay.jsonl"
            shutil.copyfile(replay_of, child_replay)
            if blobs is not None:
                child_blobs.mkdir()
                for recorded in read_journal(child_replay):
                    blob = recorded.get("blob")
                    if blob and (Path(blobs) / blob).exists():
                        shutil.copyfile(Path(blobs) / blob, child_blobs / blob)
        config = {
            "root": str(root), "journal": str(child_journal), "file_components": dict(file_components or {}),
            "run_id": run_id, "revision": revision, "entry": entry, "fixture": fixture if fixture is not None else {},
            "fixture_digest": digest(fixture if fixture is not None else {}), "space_digest": space_digest,
            "replay_of": str(child_replay) if child_replay else None, "blobs": str(child_blobs) if child_blobs else None,
            "module": module, "function": function, "script": script, "observe": list(observe),
            "syberlabs_parent": str(Path(__file__).resolve().parents[2]), "harness_path": str(Path(__file__).resolve()),
            "nonce": nonce, "key": key,
        }
        channel = _Channel()
        killed = False
        with (scratch / "stdout").open("wb") as out, (scratch / "stderr").open("wb") as err:
            try:
                proc = subprocess.Popen([python or sys.executable, "-I", "-c", BOOTSTRAP], stdin=subprocess.PIPE, stdout=out,
                                        stderr=err, cwd=str(root), env=env, close_fds=True,
                                        start_new_session=hasattr(os, "setsid"), **channel.popen_kwargs)
            finally:
                channel.started()
            try:
                proc.stdin.write(json.dumps({**config, "channel": channel.handle}).encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass  # the child is already gone; its missing report says so
            deadline = time.monotonic() + timeout
            while True:
                try:
                    report["returncode"] = proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() > deadline:
                    stop_tree(proc)
                    killed = True
                    break
        report["stdout"], report["stderr"] = _tail(scratch / "stdout"), _tail(scratch / "stderr")
        message, problem = channel.report(nonce)
        deliver = child_journal.exists()
        if killed:
            report["status"], report["error"] = "killed", f"no end after {timeout} seconds"
            deliver = False  # a run that was stopped has no end record; its journal is not evidence and is not delivered
        elif not deliver:
            report["error"] = "the child wrote no journal"
        else:
            unsealed = verify_journal(child_journal, key)
            if unsealed is not None:
                report["error"] = f"journal_unsealed: {unsealed}"
                deliver = False
            else:
                records = read_journal(child_journal)
                end = records[-1] if records and records[-1].get("t") == "end" else None
                if end is None or records[0].get("t") != "run" or records[0].get("run_id") != run_id:
                    report["error"] = "the journal has no end record for this run"
                    deliver = False  # a journal the recorder never closed is not evidence of a run
                else:
                    report["status"], report["counts"] = end["status"], end.get("counts")
        if message is None:
            report["error"] = report["error"] or f"no report over the private channel: {problem}"
            if report["status"] == "ok":
                report["status"] = "error"
        elif message.get("run_id") != run_id or message.get("status") != report["status"]:
            if report["status"] != "killed":
                report["status"] = "error"
            report["error"] = report["error"] or "the child's report does not match its journal"
        else:
            report["outcome"] = message.get("outcome")
            report["error"] = report["error"] or message.get("error")
        if deliver:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(child_journal), str(destination))
            if child_blobs is not None and child_blobs.exists():
                Path(blobs).mkdir(parents=True, exist_ok=True)
                for blob in child_blobs.iterdir():
                    if not (Path(blobs) / blob.name).exists():
                        shutil.move(str(blob), str(Path(blobs) / blob.name))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return report


# -- reading a journal ---------------------------------------------------------------------------------------

def read_journal(path: str | Path) -> list[dict]:
    """Every record of a journal, in order. A torn last line is dropped; a malformed earlier line is refused."""
    records = []
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except ValueError:
            if index == len(lines) - 1:
                break
            raise Rejected("journal_unreadable", f"{path}: line {index + 1} is not JSON") from None
    return records


def spans(records: list[dict]) -> list[dict]:
    """Closed-over spans: one per ``call`` with its ``return`` folded in. An unreturned span has ``end`` None.

    ``inside_effect`` is true for a call made while an effect's body ran (between ``effect_begin`` and its
    ``effect``); replay never makes those calls."""
    found: dict[int, dict] = {}
    order: list[int] = []
    effect_depth = 0
    for record in records:
        kind = record.get("t")
        if kind == "call":
            found[record["span"]] = {"span": record["span"], "parent": record["parent"], "component": record["component"],
                                     "file": record["file"], "line": record["line"], "fn": record["fn"],
                                     "start": record["seq"], "end": None, "start_ns": record["ns"], "end_ns": None,
                                     "effects": [], "inside_effect": effect_depth > 0}
            order.append(record["span"])
        elif kind == "return" and record["span"] in found:
            found[record["span"]]["end"] = record["seq"]
            found[record["span"]]["end_ns"] = record["ns"]
        elif kind == "effect_begin":
            effect_depth += 1
        elif kind in ("effect", "marker", "io"):
            if kind == "effect" and effect_depth and not record.get("replayed"):
                effect_depth -= 1
            if record.get("span") in found:
                found[record["span"]]["effects"].append({"seq": record["seq"], "t": kind, "name": record["name"],
                                                         "digest": record["digest"]})
    return [found[s] for s in order]


def active_at(records: list[dict], seq: int) -> list[dict]:
    """The spans open at logical time ``seq``: started at or before it and not yet returned. Outermost first."""
    return [s for s in spans(records) if s["start"] <= seq and (s["end"] is None or s["end"] > seq)]


def worldline(records: list[dict], components_only: bool = False, skip_effect_bodies: bool = True) -> list[tuple]:
    """The run as an ordered path: (seq, component, fn) per call, and (seq, kind:name, digest) per effect.

    With ``skip_effect_bodies`` the calls made inside an effect's body are left out, so a recording and its
    replay project to the same path when the program did not change."""
    path = []
    inside = {s["span"] for s in spans(records) if s["inside_effect"]} if skip_effect_bodies else set()
    for record in records:
        kind = record.get("t")
        if kind == "call" and record["span"] not in inside:
            path.append((record["seq"], record["component"], None if components_only else record["fn"]))
        elif kind in ("effect", "marker", "io"):
            path.append((record["seq"], f"{kind}:{record['name']}", record["digest"]))
    return path


LEVELS = ("component", "function")


def project(records: list[dict], components: set | None = None, level: str = "component") -> list[tuple]:
    """A run reduced to what a trace diff compares.

    ``component`` level: the sequence of component transitions (consecutive calls in one component collapse
    to one entry) and every effect and marker as (kind:name, result digest). A helper added, a function
    renamed, or two calls reordered inside one component changes nothing here; code moving across a
    component boundary, or an effect changing, does. This is the ledger's own unit and the default.

    ``function`` level: every call as (component, qualified function name) plus the effects, the strict
    form a ticket can ask for when the spec claims a path, not just a footprint.

    ``components`` restricts calls to those components; effects, markers and io observations are kept
    whatever span they ran in, since reaching outside the program is never a refactor."""
    if level not in LEVELS:
        raise Rejected("diff_level", f"a diff level is one of {LEVELS}, not {level!r}")
    out: list[tuple] = []
    for _seq, head, tail in worldline(records):
        is_effect = isinstance(head, str) and head.startswith(("effect:", "marker:", "io:"))
        if is_effect:
            out.append((head, tail))
            continue
        if components is not None and head not in components:
            continue
        entry = (head, tail) if level == "function" else (head, None)
        if level == "component" and out and out[-1] == entry:
            continue
        out.append(entry)
    return out


def compare(before: list[dict], after: list[dict], components: set | None = None, level: str = "component") -> dict:
    """Where two runs part at ``level`` (see :func:`project`). ``equal`` is true when the projections match
    element for element; otherwise ``first_divergence`` is the index and the two sides' entries there (one
    may be None when a run is shorter)."""
    a, b = project(before, components, level), project(after, components, level)
    first = None
    for index in range(max(len(a), len(b))):
        left = a[index] if index < len(a) else None
        right = b[index] if index < len(b) else None
        if left != right:
            first = {"index": index, "before": left, "after": right}
            break
    return {"level": level, "equal": first is None, "steps_before": len(a), "steps_after": len(b), "first_divergence": first}


def chrome_trace(records: list[dict]) -> dict:
    """The run as Chrome trace-event JSON (complete ``X`` events), for Perfetto or chrome://tracing."""
    events = []
    for span in spans(records):
        end_ns = span["end_ns"] if span["end_ns"] is not None else span["start_ns"]
        events.append({"name": span["fn"], "cat": span["component"] or "unmapped", "ph": "X",
                       "ts": span["start_ns"] / 1000, "dur": max(end_ns - span["start_ns"], 0) / 1000,
                       "pid": 1, "tid": 1, "args": {"seq": span["start"], "file": span["file"], "line": span["line"],
                                                    "effects": [e["name"] for e in span["effects"]]}})
    header = records[0] if records and records[0].get("t") == "run" else {}
    return {"traceEvents": events, "displayTimeUnit": "ms",
            "metadata": {"run_id": header.get("run_id"), "revision": header.get("revision")}}


def journal_digest(path: str | Path) -> str:
    """SHA-256 of the journal file's bytes."""
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
