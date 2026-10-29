"""Time: record a run once at full speed, replay it from its journal at any speed.

A ``Recorder`` installs a profile hook for the current thread and writes one
JSON line per event to a journal: a ``run`` header, a ``call`` and a ``return``
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
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

from syberlabs.canonical import canonical, digest
from syberlabs.errors import Rejected

RECORDER_VERSION = "profile-0.1"
INLINE_PAYLOAD_BYTES = 65_536
MAX_RECORDS = 5_000_000
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
_CURRENT: list["Recorder"] = []


def current() -> "Context":
    """The context of the recorder that is running this program (``syber record`` sets it up); a script says
    ``ctx = current()`` and routes its effects through ``ctx.run``. Raises when no recorder is active."""
    if not _CURRENT:
        raise Rejected("no_recorder", "no recorder is active; run under `python -m syberlabs.factory record`")
    return _CURRENT[-1].context


class Diverged(Rejected):
    """Replayed code asked for an effect the journal did not record next, or ran out of journal."""

    def __init__(self, seq: int, asked: str, journaled: str | None):
        detail = f"at seq {seq} the program asked for effect {asked!r}; the journal has {journaled!r}"
        super().__init__("replay_diverged", detail)
        self.seq = seq
        self.asked = asked
        self.journaled = journaled


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
                 replay_of: str | Path | None = None, blobs: str | Path | None = None):
        self.root = Path(root).resolve()
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
        self.counts = {"call": 0, "effect": 0, "marker": 0}
        self._stack: list[tuple[int, object]] = []
        self._handle = None
        self._t0 = 0
        self._previous_profile = None
        self._busy = False
        self._open_spans: dict[int, dict] = {}
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
                  "root": self.root.name, "python": sys.version.split()[0]}
        self._write(header)
        self._previous_profile = sys.getprofile()
        _CURRENT.append(self)
        sys.setprofile(self._profile)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        sys.setprofile(self._previous_profile)
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
        self._handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str) + "\n")

    # -- the profile hook ------------------------------------------------------------------------------------

    def _profile(self, frame, event, arg) -> None:
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

    def _journal_value(self, kind: str, name: str, value) -> None:
        self._busy = True
        try:
            payload, native = _to_json(value)
            text = canonical(payload)
            sha = digest(payload)
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
        elif kind in ("effect", "marker"):
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
        elif kind in ("effect", "marker"):
            path.append((record["seq"], f"{kind}:{record['name']}", record["digest"]))
    return path


def compare(before: list[dict], after: list[dict], components: set | None = None) -> dict:
    """Where two runs' worldlines part: projected to (component, fn) per call and (kind:name, digest) per
    effect, optionally restricted to ``components`` (effects are kept whatever their span's component).

    ``equal`` is true when the projections match element for element. Otherwise ``first_divergence`` is the
    index in the projection and the two sides' entries there (one may be None when a run is shorter)."""
    def project(records):
        out = []
        for _seq, head, tail in worldline(records):
            is_effect = isinstance(head, str) and head.startswith(("effect:", "marker:"))
            if not is_effect and components is not None and head not in components:
                continue
            out.append((head, tail))
        return out

    a, b = project(before), project(after)
    first = None
    for index in range(max(len(a), len(b))):
        left = a[index] if index < len(a) else None
        right = b[index] if index < len(b) else None
        if left != right:
            first = {"index": index, "before": left, "after": right}
            break
    return {"equal": first is None, "steps_before": len(a), "steps_after": len(b), "first_divergence": first}


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
