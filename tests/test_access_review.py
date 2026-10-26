"""Development test for a new project on the in-memory SDK."""

import importlib.util
import sqlite3
import statistics
import tempfile
import time
import unittest
from pathlib import Path

from benchmarks.run_bench import run_benchmark


ROOT = Path(__file__).resolve().parents[1]


def sqlite_commit_seconds(commits: int = 20) -> float:
    """The median time of one committed SQLite write on this machine, now: what a Work case is made of.

    A fresh database in the temporary directory, opened as ``syberwork.storage`` opens one (autocommit,
    default journal and synchronous settings), so it pays the same fsync per commit. Measured in this
    process, so it sees the same disk and the same load as the benchmark it calibrates.
    """
    with tempfile.TemporaryDirectory() as folder:
        db = sqlite3.connect(Path(folder) / "calibrate.sqlite3", isolation_level=None)
        try:
            db.execute("CREATE TABLE t (n INTEGER)")
            taken = []
            for n in range(commits):
                start = time.perf_counter()
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO t VALUES (?)", (n,))
                db.execute("COMMIT")
                taken.append(time.perf_counter() - start)
        finally:
            db.close()
    return statistics.median(taken)


def load_project():
    spec = importlib.util.spec_from_file_location("access_review", ROOT / "examples" / "access_review.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AccessReviewProject(unittest.TestCase):
    def test_new_project_development_paths(self):
        project = load_project()
        report = project.run()
        self.assertTrue(report["complete"])
        self.assertEqual(report["recorded_reason"], "all_checks_passed")
        self.assertEqual(report["current_reason"], "action_already_completed")
        self.assertEqual(report["planner_denied"], "required_prior_effect_missing")
        self.assertEqual(report["planner_effects"], 0)
        self.assertEqual(report["unverified"], "source_verification_required:roster")
        self.assertEqual(report["bad_contract"], "invalid_contract")
        self.assertIn("certify_scope:approval_required:manager", report["replay_changed"])
        self.assertTrue(report["tamper_detected"])

    def test_short_benchmark_stays_under_a_dev_ceiling(self):
        before = sqlite_commit_seconds()
        rows = {row["name"]: row for row in run_benchmark(session_cases=15, work_cases=8, observations=100)}
        commit = max(before, sqlite_commit_seconds())
        self.assertLess(rows["session.complete_case"]["median_us"], 50_000)
        # A Work case is about 15 committed SQLite writes: on a quiet development machine, 50 ms against
        # 3.4 ms per commit. Its cost is the disk's fsync, which a loaded shared runner can make a hundred
        # times slower (a 2.3 s median on Windows CI, run 40335353320). So the ceiling is 2 s or 100
        # commits measured in this process just before and after, whichever is larger: it still rejects
        # a stuck run (about seven times the expected cost), and a slow disk alone does not fail it.
        ceiling_us = max(2_000_000, 100 * commit * 1_000_000)
        self.assertLess(rows["work.complete_case"]["median_us"], ceiling_us,
                        f"one SQLite commit took {commit * 1000:.1f} ms here, so the ceiling was "
                        f"{ceiling_us / 1_000_000:.2f} s")
        self.assertLess(rows["admit.allowed"]["median_us"], 1_000)
        self.assertLess(rows["session.complete_case"]["median_us"], rows["work.complete_case"]["median_us"])


if __name__ == "__main__":
    unittest.main()
