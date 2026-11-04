"""The program the factory records to check this repository: the Build Thread example, then the two paths it
does not exercise. A candidate is checked a second time without ``rerun`` (the newest evaluation must be
reused, so no check process starts), and a third time with ``rerun`` (one must start). Under the factory's
io observation that is the difference between zero and some ``subprocess`` records, so a change to how
``Thread.check`` decides to rerun is visible in the trace diff.

    python -m syberlabs.factory bootstrap --declared docs/architecture.declared.json \\
        --entry examples/factory_self_check.py --paths syberlabs syberwork --max-drift 0
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_thread import EXPORT, TEST_EXPORT, make_repository  # noqa: E402  (the sibling example)
from syberlabs import Kit  # noqa: E402


def run(root: Path) -> dict:
    make_repository(root)
    kit = Kit.local(root / ".syberlabs", root, actor="dev@example.test")
    thread = kit.start("Add a CSV export for report rows", paths=["src/", "tests/"])
    [patch] = thread.propose(changes={"src/export.py": EXPORT, "tests/test_export.py": TEST_EXPORT}, message="CSV export")
    first = thread.check(patch.id)
    reused = thread.check(patch.id)              # no rerun: the newest evaluation stands, nothing starts
    again = thread.check(patch.id, rerun=True)   # rerun: the checks start once more
    receipt = thread.accept(patch.id)
    kit.close()
    return {"first": first.reason, "reused": reused.reason, "again": again.reason, "accept": receipt.status}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        print(json.dumps(run(Path(folder) / "project"), indent=2))
    print("factory-self-check complete")
