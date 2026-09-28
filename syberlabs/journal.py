"""Durable append-only journal for ``Session``.

Layout under the journal directory::

    registry.jsonl          installed contracts, policies, and actions
    threads/<case>.jsonl    one case row, then its events with their side records

Each record is one line of canonical JSON. A record is written, flushed, and
fsynced before the session changes memory, so a crash can lose at most the
record being written. A torn final line (no newline) is truncated on the next
locked read; a malformed line anywhere else is corruption and refuses to load.

A lock file serializes writers across processes on POSIX. Before each
operation the session reads records other processes appended. The lock is a
no-op where ``fcntl`` is unavailable; use one process per journal there.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from syberlabs.canonical import canonical
from syberlabs.errors import Rejected

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

CASE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_EVENTS = 20_000
MAX_LINE_BYTES = 1_048_576


class Journal:
    def __init__(self, home: str | Path):
        self.home = Path(home)
        (self.home / "threads").mkdir(parents=True, exist_ok=True)
        self.registry = self.home / "registry.jsonl"
        self._offsets: dict[Path, int] = {}
        self._counts: dict[str, int] = {}
        self._depth = 0
        self._mutex = threading.RLock()
        self._lock_file = open(self.home / ".lock", "a+b")

    def close(self) -> None:
        self._lock_file.close()

    @contextmanager
    def locked(self):
        with self._mutex:
            if self._depth == 0 and fcntl is not None:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and fcntl is not None:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)

    def thread_path(self, case_id: str) -> Path:
        if not isinstance(case_id, str) or not CASE_ID.match(case_id):
            raise Rejected("unknown_case", str(case_id)[:64])
        return self.home / "threads" / f"{case_id}.jsonl"

    def thread_ids(self) -> list[str]:
        return sorted(path.stem for path in (self.home / "threads").glob("*.jsonl") if CASE_ID.match(path.stem))

    def first(self, case_id: str) -> dict | None:
        """The case row of a thread, read without loading its events."""
        path = self.thread_path(case_id)
        if not path.exists():
            return None
        with open(path, "rb") as handle:
            line = handle.readline(MAX_LINE_BYTES + 1)
        if not line.endswith(b"\n"):
            return None
        return json.loads(line)["row"]

    def read_new(self, path: Path) -> list[dict]:
        """Records appended since the last read. Truncates a torn final line."""
        offset = self._offsets.get(path, 0)
        if not path.exists():
            return []
        with open(path, "r+b") as handle:
            handle.seek(offset)
            data = handle.read()
            complete = data.rfind(b"\n") + 1
            if complete < len(data):
                # A writer died mid-line. Only the holder of the lock may repair it.
                handle.truncate(offset + complete)
                handle.flush()
                os.fsync(handle.fileno())
        records = []
        for line in data[:complete].splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                raise Rejected("journal_corrupt", f"{path.name}: unreadable record after byte {offset}") from None
        self._offsets[path] = offset + complete
        return records

    def _write(self, path: Path, record: dict, *, create: bool = False) -> None:
        data = (canonical(record) + "\n").encode()
        if len(data) > MAX_LINE_BYTES:
            raise Rejected("record_too_large", f"journal records are limited to {MAX_LINE_BYTES} bytes")
        if create:
            with open(path, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_dir(path.parent)
            self._offsets[path] = len(data)
            return
        expected = self._offsets.get(path, 0)
        size = path.stat().st_size if path.exists() else 0
        if size != expected:
            raise Rejected("journal_changed", "another process wrote this journal; reopen the thread")
        with open(path, "ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected == 0:
            self._sync_dir(path.parent)
        self._offsets[path] = expected + len(data)

    @staticmethod
    def _sync_dir(folder: Path) -> None:
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def add_registry(self, record: dict) -> None:
        self._write(self.registry, record)

    def create_thread(self, row: dict) -> None:
        self._write(self.thread_path(row["id"]), {"t": "case", "row": row}, create=True)
        self._counts[row["id"]] = 0

    def add_event(self, case_id: str, event: dict, side: dict) -> None:
        count = self._counts.get(case_id, 0)
        if count >= MAX_EVENTS:
            raise Rejected("thread_full", f"a thread holds at most {MAX_EVENTS} events; start a new thread")
        self._write(self.thread_path(case_id), {"t": "event", "event": event, "side": side})
        self._counts[case_id] = count + 1

    def note_loaded(self, case_id: str, events: int) -> None:
        self._counts[case_id] = events

    def record_forget(self, tombstone: dict, receipt: dict | None) -> None:
        """Write the receipt and the tombstone, fsynced, before any history is deleted."""
        if receipt is not None:
            folder = self.home / "receipts"
            folder.mkdir(exist_ok=True)
            path = folder / f"{tombstone['thread']}.json"
            with open(path, "wb") as handle:
                handle.write((canonical(receipt) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_dir(folder)
        path = self.home / "forgotten.jsonl"
        with open(path, "ab") as handle:
            handle.write((canonical(tombstone) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())
        self._sync_dir(self.home)

    def remove_thread(self, case_id: str) -> None:
        path = self.thread_path(case_id)
        if path.exists():
            path.unlink()
            self._sync_dir(path.parent)
        self._offsets.pop(path, None)
        self._counts.pop(case_id, None)

    def forgotten(self) -> list[dict]:
        path = self.home / "forgotten.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def size(self) -> int:
        return sum(path.stat().st_size for path in self.home.rglob("*.jsonl"))
