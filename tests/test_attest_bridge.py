"""Attest artifacts remain observations; external effects need destination proof."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adapters.attest_observe import (
    receipt_file_to_observation_payload,
    receipt_json_to_observation_payload,
    record_to_observation_payload,
)
from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from syberlabs.attest_bridge import AuthorityDecision, HandoffPackage, RunReceipt
from syberwork.core import Work

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "syberlabs" / "attest_bridge"


def receipt_fixture(name="attest-receipt-v0-fixture.json"):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class AttestObservationTypes(unittest.TestCase):
    def test_records_are_tagged_and_always_unverified(self):
        records = (
            (RunReceipt(receipt_fixture()), "attest.run_receipt", "run_receipt"),
            (AuthorityDecision({"decision": "approved"}), "attest.authority_decision", "authority_decision"),
            (HandoffPackage({"owner": "release-manager"}), "attest.handoff_package", "handoff_package"),
        )
        for record, key, kind in records:
            with self.subTest(kind=kind):
                observation = record_to_observation_payload(
                    record, source_version="attest-fixture-v1", actor="attest-importer"
                )
                self.assertEqual(observation["key"], key)
                self.assertEqual(observation["value"]["kind"], kind)
                self.assertEqual(observation["value"]["payload"], dict(record.payload))
                self.assertEqual(observation["source"], "attest")
                self.assertEqual(observation["version"], "attest-fixture-v1")
                self.assertFalse(observation["verified"])

    def test_receipt_payload_must_be_json_and_versioned_by_importer(self):
        with self.assertRaises(TypeError):
            receipt_json_to_observation_payload(
                {"bad": object()}, source_version="v1", actor="importer"
            )
        with self.assertRaises(ValueError):
            receipt_json_to_observation_payload(receipt_fixture(), source_version="", actor="importer")


class AttestReceiptFileImport(unittest.TestCase):
    def test_receipt_file_becomes_unverified_observation(self):
        receipt = receipt_fixture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attest-home" / "receipts" / "fixture-42.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(receipt), encoding="utf-8")
            observation = receipt_file_to_observation_payload(path, actor="attest-importer")

        self.assertEqual(observation["key"], "attest.run_receipt")
        self.assertEqual(observation["value"]["kind"], "run_receipt")
        self.assertEqual(observation["value"]["payload"], receipt)
        self.assertEqual(observation["version"], "attest.receipt/v0")
        self.assertFalse(observation["verified"])
        self.assertEqual(
            observation["value"]["projection"]["destination_verification"]["method"],
            "kane_recheck",
        )

    def test_receipt_file_requires_attest_v0_schema_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "receipt.json"
            path.write_text('{"schema": "attest.receipt/v1"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "attest.receipt/v0"):
                receipt_file_to_observation_payload(path, actor="attest-importer")

    def test_spec_mode_projects_ticket_patch_and_kane_recheck(self):
        receipt = receipt_fixture("attest-receipt-v0-spec-fixture.json")
        observation = receipt_json_to_observation_payload(
            receipt, source_version="attest.receipt/v0", actor="attest-importer"
        )

        self.assertFalse(observation["verified"])
        self.assertEqual(observation["value"]["payload"], receipt)
        self.assertEqual(
            observation["value"]["projection"]["implementation"],
            {
                "ticket_title": "Missing capability: demo action",
                "patch_sha256": receipt["app"]["patch_sha256"],
            },
        )
        self.assertEqual(
            observation["value"]["projection"]["destination_verification"],
            {
                "status": "verified",
                "method": "kane_recheck",
                "reported_by": "attest.receipt/v0 evidence.after.status",
            },
        )

    def test_receipt_rejects_unknown_fields_and_verified_claim(self):
        unknown_field_receipt = receipt_fixture()
        unknown_field_receipt["unexpected"] = "not part of receipt/v0"
        with self.assertRaisesRegex(ValueError, "unknown Attest receipt fields"):
            RunReceipt(unknown_field_receipt)

        claimed_verified_receipt = receipt_fixture()
        claimed_verified_receipt["verified"] = True
        with self.assertRaisesRegex(ValueError, "verified must be false"):
            RunReceipt(claimed_verified_receipt)

    def test_cli_reads_receipt_path_and_labels_reconciliation_as_simulated(self):
        receipt = receipt_fixture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attest-home" / "receipts" / "fixture-42.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(receipt), encoding="utf-8")
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run(
                [sys.executable, "examples/attest_factory_slice.py", "--receipt", str(path)],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

        output = json.loads(result.stdout)
        self.assertEqual(output["attest_receipt_source_version"], "attest.receipt/v0")
        self.assertFalse(output["attest_receipt_verified"])
        self.assertEqual(output["simulated_syberwork_destination_reconciliation"], "verified")
        self.assertTrue(output["destination_is_simulated"])
        self.assertFalse(output["receipt_fixture_only"])


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

        # Even a valid receipt with a proven Kane re-check remains an observation,
        # not SyberWork reconciliation for this independent ERP effect.
        attest_receipt = receipt_fixture()
        attest_receipt["evidence"]["after"]["status"] = "proven"
        observation = receipt_json_to_observation_payload(
            attest_receipt,
            source_version="attest.receipt/v0",
            actor="attest-importer",
        )
        event = self.work.observe(self.case, **observation)
        self.assertFalse(event["body"]["verified"])
        self.assertEqual(
            event["body"]["value"]["projection"]["destination_verification"]["method"],
            "kane_recheck",
        )
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
