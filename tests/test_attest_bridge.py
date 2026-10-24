"""Attest artifacts remain observations; external effects need destination proof."""

import tempfile
import unittest
from pathlib import Path

from adapters.attest_observe import (
    receipt_json_to_observation_payload,
    record_to_observation_payload,
)
from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from syberlabs.attest_bridge import AuthorityDecision, HandoffPackage, RunReceipt
from syberwork.core import Work


class AttestObservationTypes(unittest.TestCase):
    def test_records_are_tagged_and_always_unverified(self):
        records = (
            (RunReceipt({"run_id": "fixture-1", "status": "passed"}), "attest.run_receipt", "run_receipt"),
            (AuthorityDecision({"decision": "approved"}), "attest.authority_decision", "authority_decision"),
            (HandoffPackage({"owner": "release-manager"}), "attest.handoff_package", "handoff_package"),
        )
        for record, key, kind in records:
            with self.subTest(kind=kind):
                observation = record_to_observation_payload(
                    record, source_version="attest-fixture-v1", actor="attest-importer"
                )
                self.assertEqual(observation["key"], key)
                self.assertEqual(observation["value"], {"kind": kind, "payload": dict(record.payload)})
                self.assertEqual(observation["source"], "attest")
                self.assertEqual(observation["version"], "attest-fixture-v1")
                self.assertFalse(observation["verified"])

    def test_receipt_payload_must_be_json_and_versioned_by_importer(self):
        with self.assertRaises(TypeError):
            receipt_json_to_observation_payload(
                {"bad": object()}, source_version="v1", actor="importer"
            )
        with self.assertRaises(ValueError):
            receipt_json_to_observation_payload({}, source_version="", actor="importer")


class AttestCannotReconcileAnExternalWrite(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.erp = SimulatedERP(Path(self.temp.name) / "erp.sqlite3")
        self.addCleanup(self.erp.close)
        self.work = Work(Path(self.temp.name) / "work.sqlite3")
        self.addCleanup(self.work.close)
        configure(self.work, self.erp)
        self.case = self.work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4812"}, "analyst")
        for source, key, record in (
            ("requisitions", "request", "REQ-4812"),
            ("sites", "site", "DC-WEST-4"),
            ("finance", "budget", "CC-742"),
            ("supplier", "quote", "REQ-4812"),
        ):
            self.work.refresh_fact(self.case, source, key, record, "analyst", ["operator"])
        review = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.work.commit(self.case, review["proposal"]["id"], "scheduler")

    def test_attest_claim_does_not_satisfy_effect_acceptance(self):
        order = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        proposal_id = order["proposal"]["id"]
        self.work.approve(self.case, proposal_id, "manager", ["manager"])

        # The destination wrote the order but its acknowledgement was lost.
        self.erp.drop_after_write = True
        outcome = self.work.commit(self.case, proposal_id, "scheduler")
        self.assertEqual(outcome["status"], "unknown")

        # Even an Attest receipt that says the write succeeded is just observation data.
        observation = receipt_json_to_observation_payload(
            {
                "run_id": "offline-fixture-7",
                "effect": {"status": "succeeded", "external_id": "attest-claimed-order"},
                "message": "The order was created.",
            },
            source_version="attest-fixture-v1",
            actor="attest-importer",
        )
        event = self.work.observe(self.case, **observation)
        self.assertFalse(event["body"]["verified"])
        state = self.work.inspect(self.case)
        effect_clause = next(row for row in state["acceptance"] if row["id"] == "order_recorded")
        self.assertFalse(effect_clause["passed"])
        self.assertFalse(state["complete"])

        # Only SyberWork's independent status lookup verifies this destination record.
        reconciliation = self.work.reconcile(self.case, proposal_id, "manager", ["manager"])
        self.assertEqual(reconciliation["status"], "verified")
        state = self.work.inspect(self.case)
        effect_clause = next(row for row in state["acceptance"] if row["id"] == "order_recorded")
        self.assertTrue(effect_clause["passed"])
        self.assertFalse(state["complete"], "the separate required signoff is still outstanding")
        self.work.signoff(self.case, "manager", ["manager"], "manager")
        self.assertTrue(self.work.inspect(self.case)["complete"])


if __name__ == "__main__":
    unittest.main()
