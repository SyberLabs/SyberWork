"""The public rehearsal of the denials a paid pilot has to show."""

import importlib.util
import unittest
from pathlib import Path


def load_rehearsal():
    path = Path(__file__).resolve().parents[1] / "examples" / "pilot_rehearsal.py"
    spec = importlib.util.spec_from_file_location("pilot_rehearsal", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PilotRehearsal(unittest.TestCase):
    def test_rehearsal_shows_the_pilot_denials_and_names_no_customer(self):
        report = load_rehearsal().rehearse()
        self.assertIsNone(report["customer"])
        self.assertIsNone(report["price"])
        self.assertIsNone(report["license"])
        self.assertEqual(report["denials"]["actor_role"], "actor_role_missing")
        self.assertEqual(report["denials"]["approval"], "approval_required:manager")
        self.assertEqual(report["denials"]["unverified_fact"], "source_verification_required:roster")
        self.assertEqual(report["denials"]["prior_effect"], "required_prior_effect_missing")
        self.assertEqual(report["denials"]["self_evaluation"], "evaluation_denied")
        self.assertEqual(report["denials"]["unknown_effect"], "unknown")
        self.assertEqual(report["unknown_succeeded_count"], 0)
        self.assertTrue(report["chain_valid"])
        self.assertTrue(report["local_effect_succeeded"])
        self.assertEqual(report["explanations"]["actor_role"], "actor.role")
        self.assertEqual(report["explanations"]["approval"], "approval.required")
        self.assertEqual(report["explanations"]["unverified_fact"], "facts.required")
        self.assertEqual(report["explanations"]["prior_effect"], "effect.prior")
