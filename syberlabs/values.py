"""Path lookup used by admission and resolution checks."""

from __future__ import annotations

from typing import Any


def at_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value
