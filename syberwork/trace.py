"""Semantic trace for one cell. One JSON object per line.

The names follow the case grammar: admission, effect claim, destination
invocation, settlement, reconciliation. This is not a generic HTTP access log.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class TraceLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, name: str, **fields) -> None:
        row = {"name": name, "at": time.time(), **fields}
        line = json.dumps(row, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows
