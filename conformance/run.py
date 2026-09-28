"""Diff a fresh capture against conformance/golden and check event bodies.

    PYTHONPATH=. python -m conformance.run

Exits nonzero when a trace differs or an event body no longer matches
the v0alpha1 schemas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conformance.capture import GOLDEN, collect, golden_document
from spec.validate import validate_events


def main() -> None:
    traces = collect()
    failed = False
    seen = set()
    for trace in traces:
        seen.add(trace["id"])
        path = GOLDEN / f"{trace['id']}.json"
        actual = golden_document(trace)
        if not path.exists():
            print(f"missing golden file for {trace['id']}")
            failed = True
        else:
            expected = json.loads(path.read_text())
            if actual != expected:
                print(f"mismatch {trace['id']}")
                print("expected:", json.dumps(expected, sort_keys=True))
                print("actual:  ", json.dumps(actual, sort_keys=True))
                failed = True
        for error in validate_events(trace["raw_events"], trace["id"]):
            print(error)
            failed = True
    for path in sorted(GOLDEN.glob("*.json")):
        if path.stem not in seen:
            print(f"unexpected golden file {path.name}")
            failed = True
    if failed:
        raise SystemExit(1)
    print(f"matched {len(traces)} golden traces")


if __name__ == "__main__":
    main()
