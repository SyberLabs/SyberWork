"""A Build Thread from start to resume, without a model, a network, or an account.

Creates a small repository in a temporary directory, then:

1. starts a thread scoped to src/ and tests/ and shows what a provider would read;
2. a stand-in "model" provider proposes a change that breaks a test and claims it passes;
3. the host runs the tests itself: the claim is not evidence, and acceptance is refused;
4. a person's patch passes and is accepted, which moves only the thread's branch;
5. a new process reopens the thread from its journal and replays it under a stricter contract.

Run from a checkout or an installed wheel:

    python examples/build_thread.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs import Kit
from syberlabs.providers import FunctionProvider

REPORT = 'def rows():\n    return [{"name": "a", "qty": 2}, {"name": "b", "qty": 3}]\n'
TEST_REPORT = textwrap.dedent('''\
    import os, sys, unittest
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import report

    class Report(unittest.TestCase):
        def test_rows(self):
            self.assertEqual(len(report.rows()), 2)
    ''')
EXPORT = textwrap.dedent('''\
    import csv, io
    from report import rows

    def to_csv():
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=["name", "qty"])
        writer.writeheader()
        writer.writerows(rows())
        return out.getvalue()
    ''')
TEST_EXPORT = textwrap.dedent('''\
    import os, sys, unittest
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import export

    class Export(unittest.TestCase):
        def test_header(self):
            self.assertEqual(export.to_csv().splitlines()[0], "name,qty")
    ''')


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def make_repository(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "report.py").write_text(REPORT)
    (root / "tests" / "test_report.py").write_text(TEST_REPORT)
    git(root.parent, "init", "-q", "-b", "main", str(root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")


def overconfident_model(space):
    """Stands in for a model adapter. It rewrites rows() and says the tests pass."""
    changes = {"src/export.py": EXPORT, "tests/test_export.py": TEST_EXPORT,
               "src/report.py": "def rows():\n    return [{'name': 'a', 'qty': 2}]\n"}
    return [space.submit(changes, operator="patch", message="CSV export, simplified rows",
                         signal={"tests": "passed", "confidence": 0.97}).id]


def run(root: Path) -> dict:
    make_repository(root)
    kit = Kit.local(root / ".syberlabs", root, actor="dev@example.test")
    thread = kit.start("Add a CSV export for report rows", paths=["src/", "tests/"])
    context = [f"{e.path}:{e.start}-{e.end}" for e in thread.context()]

    [model] = thread.propose(FunctionProvider(overconfident_model, name="stand-in-model", revision="demo-1"))
    refused = thread.check(model.id)
    denied = thread.accept(model.id)
    [patch] = thread.propose(changes={"src/export.py": EXPORT, "tests/test_export.py": TEST_EXPORT},
                             message="CSV export")
    verdict = thread.check(patch.id)
    receipt = thread.accept(patch.id)
    untouched = git(root, "status", "--porcelain") == "" and git(root, "rev-parse", "main") == git(root, "rev-parse", thread.base)
    kit.close()

    resumed_kit = Kit.local(root / ".syberlabs", root, actor="dev@example.test")
    resumed = resumed_kit.open(thread.id[:8])
    status = resumed.status()
    stricter = json.loads((root / ".syberlabs" / "contracts" / "repo-change.v1.json").read_text())
    stricter["version"] = 2
    stricter["evolution"]["evaluation"]["checks"]["lint"] = {"argv": [sys.executable, "-m", "pyflakes", "src"]}
    stricter["evolution"]["evaluation"]["required"] = ["lint", "tests"]
    resumed_kit.session.install_contract(stricter)
    replayed = resumed_kit.session.replay(resumed.id, 2, 1)
    resumed_kit.close()
    return {
        "context": context,
        "model": {"candidate": model.id, "claimed": model.signal, "verdict": refused.reason,
                  "accept": f"{denied.status}:{denied.reason}"},
        "patch": {"candidate": patch.id, "verdict": verdict.reason, "accept": receipt.status,
                  "branch": receipt.ref, "commit": receipt.commit},
        "working_tree_and_main_untouched": untouched,
        "resumed": {"status": status["status"], "accepted": status["accepted"], "events": status["events"],
                    "chain_valid": status["chain_valid"]},
        "replay_under_v2": [c["after"]["reason"] for c in replayed["changed_decisions"]],
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        report = run(Path(folder) / "project")
    ok = (report["model"]["accept"] == "denied:candidate_check_failed:tests" and report["patch"]["accept"] == "succeeded"
          and report["working_tree_and_main_untouched"] and report["resumed"]["chain_valid"])
    print(json.dumps(report, indent=2))
    print(f"build-thread {'complete' if ok else 'FAILED'}")
    raise SystemExit(0 if ok else 1)
