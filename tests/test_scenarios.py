"""Scenario modules record probe failures instead of hiding them."""

import unittest

from benchmarks.scenarios.flaky_bank import SCENARIO
from benchmarks.scenarios.host import SessionHost
from benchmarks.scenarios.probe import Probe
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
