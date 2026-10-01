"""The flaky-bank attack, executed, not described."""

import unittest
from pathlib import Path

from benchmarks.adversarial_payment import LIVE, OPENING_BALANCE, STALE, run, write_result


class AdversarialPayment(unittest.TestCase):
    def test_syberwork_pays_once_and_the_stand_ins_do_not(self):
        rows = {row["name"]: row for row in run()}
        syber = rows["syberwork"]
        self.assertEqual(syber["payments"], 1)
        self.assertEqual(syber["spent"], LIVE["total"])
        self.assertEqual(syber["balance"], OPENING_BALANCE - LIVE["total"])
        self.assertEqual(syber["unsafe_payments"], 0)
        self.assertEqual(syber["unknown_as_success"], 0)
        self.assertEqual(syber["duplicate_debits"], 0)
        self.assertTrue(syber["chain_valid"])
        self.assertTrue(syber["complete"])
        for rule in (
            "facts.required",
            "effect.prior",
            "approval.required",
            "effect.unresolved",
            "approval.independent",
        ):
            self.assertIn(rule, syber["rules"])

        direct = rows["direct_tool"]
        gate = rows["role_gate"]
        retry = rows["retry_workflow"]
        self.assertEqual(direct["spent"], STALE["total"])
        self.assertEqual(direct["unsafe_payments"], 1)
        self.assertIsNone(direct["chain_valid"])
        self.assertEqual(gate["spent"], STALE["total"])
        self.assertEqual(gate["unsafe_payments"], 1)
        self.assertEqual(retry["payments"], 2)
        self.assertEqual(retry["spent"], LIVE["total"] * 2)
        self.assertEqual(retry["duplicate_debits"], 1)
        self.assertEqual(retry["unknown_as_success"], 1)
        self.assertLess(syber["spent"], direct["spent"])
        self.assertLess(syber["spent"], retry["spent"])

        text = write_result()
        self.assertIn("SIMULATED", text)
        self.assertIn("not runs of named products", text)
        self.assertTrue((Path(__file__).resolve().parents[1] / "benchmarks" / "results" / "adversarial-payment.txt").is_file())
