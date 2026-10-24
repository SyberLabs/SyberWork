"""A deterministic, offline command-worker example for attest_dispatch.py.

This emits a harmless blank-line-only patch to demonstrate the worker protocol;
it does not implement a ticket's acceptance criteria. Replace it with a real
harness for useful changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from examples.attest_dispatch import _file_diff  # noqa: E402


def main() -> int:
    try:
        ticket = json.load(sys.stdin)
        files = ticket["app"]["files"]
        path = sorted(files)[0]
        old = files[path]
        if not isinstance(old, str):
            raise ValueError("example worker expects text file contents")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        print(f"command_worker_example: invalid ticket: {exc}", file=sys.stderr)
        return 2

    new = old + ("\n" if old.endswith("\n") else "\n\n")
    sys.stdout.write(_file_diff(path, old, new))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
