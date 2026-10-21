"""Scenario modules record probe failures instead of hiding them."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from benchmarks.scenarios.flaky_bank import SCENARIO
from benchmarks.scenarios.host import SessionHost
from benchmarks.scenarios.probe import Probe, evaluate
from benchmarks.scenarios.world import Ledger
from benchmarks.scenarios.runner import load_scenarios, run_scenario
from benchmarks.scenarios.world import Ledger


class _EmptyHost:
    name = "empty"
    kind = "test"

    def bind_world(self, world):
        self.world = world

    def install(self, contract, policy):
        return None

    def report(self):
        snap = self.world.snapshot()
        return {
            "name": self.name,
            "kind": self.kind,
            "decisions": [],
            "rules": [],
            "effect_started_before_review": 0,
            "unknown_status": None,
            "unknown_succeeded": 0,
            "unknown_as_success": 0,
            "chain_valid": None,
            "complete": None,
            **snap,
        }


class _NoPlay:
    id = "empty_world"

    def world(self):
        return Ledger()

    def contract(self):
        return {}

    def policy(self):
        return {}

    def play(self, host, world):
        return None

    def probes(self):
        return [Probe("single_debit", payments=1, spent=4000)]


class ScenarioInfrastructure(unittest.TestCase):
    def test_modules_are_discovered_and_a_miss_is_a_finding(self):
        found = {scenario.id: scenario for scenario in load_scenarios()}
        self.assertIn("flaky_bank", found)
        report = run_scenario(_NoPlay(), _EmptyHost())
        self.assertEqual(report["probe_failures"], ["single_debit"])
        self.assertFalse(report["probes"][0]["passed"])
        self.assertEqual(report["probes"][0]["expected"]["payments"], 1)
        self.assertEqual(report["probes"][0]["observed"]["payments"], 0)

    def test_unknown_probe_fields_and_mismatched_metrics_fail(self):
        with self.assertRaises(ValueError):
            Probe("typo", not_a_field=1)
        result = evaluate(Probe("no_duplicates", duplicate_debits=0), {"duplicate_debits": 1, "decisions": []})
        self.assertFalse(result["passed"])
        self.assertEqual(result["observed"]["duplicate_debits"], 1)
        ledger = Ledger(faults=["drop_after_debit"])
        try:
            ledger.pay({"id": "INV-OTHER", "total": 10}, "k")
        except TimeoutError:
            pass
        self.assertEqual(ledger.payments["k"]["invoice_id"], "INV-OTHER")
        self.assertEqual(ledger.payments["k"]["amount"], 10)

    def test_flaky_bank_on_the_session_passes_every_probe(self):
        report = run_scenario(SCENARIO, SessionHost())
        self.assertEqual(report["probe_failures"], [])
        self.assertEqual([item["id"] for item in report["probes"]], [
            "unverified_refused",
            "stale_refused",
            "model_did_not_start_effect",
            "approval_required",
            "self_approval_refused",
            "unknown_not_success",
            "retry_blocked",
            "single_debit",
            "chain_valid",
            "complete",
        ])


class ScenarioCheck(unittest.TestCase):
    """benchmarks/run_scenarios.py replays the record as a host check: exit 0, 1 or 2."""

    ROOT = Path(__file__).resolve().parent.parent
    RUNNER = ROOT / "benchmarks" / "run_scenarios.py"
    RECORD = ROOT / "benchmarks" / "scenarios" / "expected.json"

    def _run(self, *argv):
        env = dict(os.environ, PYTHONPATH=str(self.ROOT), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, str(self.RUNNER), *argv], cwd=self.ROOT, env=env,
                              capture_output=True, text=True, timeout=600)

    def test_the_record_holds_on_this_tree_and_two_runs_agree(self):
        first = self._run("--json")
        second = self._run("--json")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        outcome = json.loads(first.stdout)
        self.assertEqual(outcome["exit_code"], 0)
        self.assertRegex(outcome["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual({item["status"] for item in outcome["scenarios"]}, {"held"})
        self.assertEqual(len(outcome["scenarios"]), 11)

    def test_a_mutated_expectation_exits_one_and_names_the_field(self):
        record = json.loads(self.RECORD.read_text(encoding="utf-8"))
        record["scenarios"]["over_limit"]["report"]["payments"] = 1
        with tempfile.TemporaryDirectory() as folder:
            mutated = Path(folder) / "expected.json"
            mutated.write_text(json.dumps(record), encoding="utf-8")
            result = self._run("--scenario", "over_limit", "--expected", str(mutated))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("divergence: over_limit: report.payments: expected 1, observed 0", result.stdout)

    def test_a_malformed_record_or_scenario_exits_two(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / "expected.json"
            broken.write_text("{\"scenarios\": []}", encoding="utf-8")
            result = self._run("--expected", str(broken))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("malformed:", result.stdout)
        self.assertEqual(self._run("--scenario", "no_such_scenario").returncode, 2)
        from benchmarks.run_scenarios import Malformed, validate_scenario

        class NoProbes(_NoPlay):
            def probes(self):
                return []

        class BadRecord(_NoPlay):
            expected_failures = ["not_a_probe"]

        with self.assertRaises(Malformed):
            validate_scenario(NoProbes())
        with self.assertRaises(Malformed):
            validate_scenario(BadRecord())
        validate_scenario(_NoPlay())
