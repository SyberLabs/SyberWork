"""Ten operational scenarios. Two of them are expected to expose a current gap."""

import unittest

from benchmarks.scenarios.host import SessionHost
from benchmarks.scenarios.runner import load_scenarios, run_scenario


REQUIRED = {
    "stale_quote",
    "self_approval",
    "model_skips_step",
    "unverified_revocation",
    "over_limit",
    "model_cannot_promote",
    "registrar_cannot_evaluate",
    "tampered_chain",
    "asserted_invoice",
    "lying_reconciler",
}


class ScenarioSuite(unittest.TestCase):
    def test_ten_scenarios_match_their_expected_findings(self):
        found = {scenario.id: scenario for scenario in load_scenarios()}
        self.assertTrue(REQUIRED <= set(found))
        for identifier in sorted(REQUIRED):
            scenario = found[identifier]
            report = run_scenario(scenario, SessionHost())
            expected = set(getattr(scenario, "expected_failures", []))
            self.assertEqual(set(report["probe_failures"]), expected, identifier)
