"""Offline tests of the benchmark's information and evidence boundaries."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.githits import client, run
from benchmarks.githits.fixtures import FIXTURES


class Context(unittest.TestCase):
    def setUp(self):
        self.fixture = FIXTURES["httpx-asgi"]
        self.read = self.fixture["reads"][0]
        self.body = {"kind": "code", "is_binary": False, "path": self.read["path"],
                     "target_resolution": {"served": {"registry": "pypi", "name": "httpx", "version": "0.28.1"}},
                     "content": {"body": "class ASGITransport: pass", "start_line": 63, "end_line": 64}}

    def test_only_exact_served_version_counts(self):
        self.assertIn("ASGITransport", client.validate_read(self.body, self.fixture, self.read))
        self.body["target_resolution"]["served"]["version"] = "0.28.0"
        with self.assertRaises(ValueError):
            client.validate_read(self.body, self.fixture, self.read)

    def test_metadata_or_binary_does_not_count_as_context(self):
        for invalid in ({"kind": "documentation"}, {"is_binary": True}, {"content": {}}, {"path": "wrong.py"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                client.validate_read({**self.body, **invalid}, self.fixture, self.read)

    def test_unauthorized_arm_is_unavailable_and_stops_retrieval(self):
        with patch.object(client, "request", return_value={"status": 401, "body": {"code": "AUTHENTICATION_REQUIRED"}}) as fetch:
            found = client.collect(self.fixture)
        self.assertEqual(found["status"], "unavailable")
        self.assertIsNone(found["context"])
        self.assertEqual(fetch.call_count, 1)

    def test_unavailable_arm_never_invokes_model(self):
        with patch.object(run, "open_thread", side_effect=AssertionError("model path entered")):
            row = run.run_arm("httpx-asgi", "with_githits", 1, Path("unused"), Path("unused"), sys.executable,
                              2, "model", "high", {"status": "unavailable", "reason": "AUTHENTICATION_REQUIRED"})
        self.assertEqual((row["status"], row["calls"], row["solved"]), ("unavailable", 0, None))


class Summary(unittest.TestCase):
    def row(self, arm, *, status="completed", solved=True, seed=1):
        return {"fixture": "task", "seed": seed, "arm": arm, "status": status, "solved": solved, "calls": 1}

    def test_unavailable_is_not_zero_percent_or_parity(self):
        report = run.summarize([self.row("without_githits"),
                                self.row("with_githits", status="unavailable", solved=None)], 2)
        self.assertIn("Completed matched pairs: 0/1", report)
        self.assertIn("No comparative result", report)
        self.assertNotIn("Without GitHits: 1/", report)

    def test_only_completed_pairs_enter_denominator(self):
        report = run.summarize([self.row("without_githits", solved=False), self.row("with_githits"),
                                self.row("without_githits", seed=2),
                                self.row("with_githits", seed=2, status="provider_error", solved=None)], 2)
        self.assertIn("Completed matched pairs: 1/2", report)
        self.assertIn("Without GitHits: 0/1; with GitHits: 1/1", report)
        self.assertIn("GitHits-only solves 1, baseline-only solves 0", report)


class HostBoundary(unittest.TestCase):
    def run_patch(self, changes, calls=1):
        with tempfile.TemporaryDirectory() as folder:
            root, evidence = Path(folder) / "repo", Path(folder) / "evidence"
            toy = {"package": "test-only", "version": "1", "objective": "Return seven",
                   "broken": "def value(): return 0\n", "checks": "from integration import value\nassert value() == 7\n"}
            doc = run.contract("httpx-asgi", sys.executable, calls)
            doc["id"] = "githits-host-test"
            doc["evolution"]["evaluation"]["checks"]["dependency_pin"]["argv"] = [sys.executable, "-I", "-c", "pass"]
            response = {"candidates": [{"changes": changes, "signal": {"tests_pass": True}}], "recommended": [0]}
            with patch.dict(FIXTURES, {"host-test": toy}), patch.object(run, "contract", return_value=doc):
                run.create_base("host-test", root)
                with patch("benchmarks.model_vs_patch.run_json", return_value=response):
                    row = run.run_arm("host-test", "without_githits", 1, root, evidence, sys.executable,
                                      calls, "test-double", "high", {"status": "unavailable"})
            events = json.loads((evidence / "events.json").read_text(encoding="utf-8"))
            self.assertTrue((evidence / "repository.bundle").is_file())
            return row, events

    def test_provider_claim_cannot_turn_failing_check_into_solve(self):
        row, events = self.run_patch({"src/integration.py": "def value(): return 6\n"})
        self.assertFalse(row["solved"])
        evaluations = [e["body"] for e in events if e["kind"] == "candidate_evaluated"]
        self.assertEqual(len(evaluations), 1)
        self.assertIn("failed", [check["state"] for check in evaluations[0]["checks"]])

    def test_host_passing_candidate_counts(self):
        row, events = self.run_patch({"src/integration.py": "def value(): return 7\n"})
        self.assertTrue(row["solved"])
        self.assertEqual((row["calls"], row["evaluations"]), (1, 1))
        self.assertFalse(any(e["kind"] == "effect_succeeded" for e in events), "benchmark never auto-accepts")

    def test_check_rewrite_cannot_count_as_solve(self):
        row, events = self.run_patch({"src/integration.py": "def value(): return 7\n", "checks.py": "pass\n"})
        self.assertFalse(row["solved"])
        self.assertEqual(row["evaluations"], 0)


if __name__ == "__main__":
    unittest.main()
