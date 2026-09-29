"""Development test for a new project on the in-memory SDK."""

import importlib.util
import os
import unittest
from pathlib import Path

from benchmarks.run_bench import run_benchmark


ROOT = Path(__file__).resolve().parents[1]


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
        rows = {row["name"]: row for row in run_benchmark(session_cases=15, work_cases=8, observations=100)}
        self.assertLess(rows["session.complete_case"]["median_us"], 50_000)
        # 100 ms is the Linux development ceiling. Windows CI pays more for each fsync.
        work_ceiling = 2_000_000 if os.name == "nt" else 100_000
        self.assertLess(rows["work.complete_case"]["median_us"], work_ceiling)
        self.assertLess(rows["admit.allowed"]["median_us"], 1_000)
        self.assertLess(rows["session.complete_case"]["median_us"], rows["work.complete_case"]["median_us"])


if __name__ == "__main__":
    unittest.main()
