import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from syberlabs.attest_reconcile_map import classify_attest_artifact
from syberlabs.canonical import digest
from syberlabs.evidence import acceptance_results, verified_reconciliation
from syberwork.core import Work


class AttestArtifactClassification(unittest.TestCase):
    def test_run_receipt_is_observation_not_effect_success(self):
        classification = classify_attest_artifact("RunReceipt")
        schema_classification = classify_attest_artifact("attest.receipt/v0")
        self.assertTrue(schema_classification["observation"])
        self.assertFalse(schema_classification["verified_reconciliation"])
        self.assertEqual(
            classification,
            {
                "observation": True,
                "handoff": False,
                "refuse": False,
                "verified_reconciliation": False,
            },
        )
        receipt_event = {
            "kind": "observed",
            "body": {
                "key": "attest.run_receipt",
                "value": {
                    "schema": "attest.receipt/v0",
                    "kind": "RunReceipt",
                    "status": "proven",
                    "effect_succeeded": True,
                    "external_id": "attest-claimed-order",
                },
                "source": "attest",
                "version": "run-1",
                "actor": "attest",
                "verified": False,
            },
        }
        contract = {"acceptance": [{"id": "publish", "kind": "effect", "action": "publish"}]}

        self.assertFalse(verified_reconciliation(receipt_event))
        self.assertEqual(acceptance_results(contract, [receipt_event]), [{"id": "publish", "passed": False}])

    def test_handoff_package_is_not_reconciled(self):
        classification = classify_attest_artifact("HandoffPackage")
        handoff_reference = {
            "kind": "observed",
            "body": {
                "key": "attest.handoff",
                "value": {"kind": "HandoffPackage", "status": "ready_for_human"},
                "source": "attest",
                "version": "handoff-1",
                "actor": "attest",
                "verified": False,
            },
        }

        self.assertEqual(classification["handoff"], True)
        self.assertEqual(classification["observation"], False)
        self.assertEqual(classification["refuse"], False)
        self.assertIs(classification["verified_reconciliation"], False)
        self.assertFalse(verified_reconciliation(handoff_reference))

    def test_authority_decision_is_observation_not_syberwork_approval(self):
        classification = classify_attest_artifact("AuthorityDecision")

        self.assertTrue(classification["observation"])
        self.assertFalse(classification["handoff"])
        self.assertFalse(classification["refuse"])
        self.assertFalse(classification["verified_reconciliation"])

    def test_unknown_and_success_shaped_kinds_fail_closed(self):
        for kind in ("DestinationWriteReceipt", "reconciled", "effect_succeeded", None, 7):
            with self.subTest(kind=kind):
                classification = classify_attest_artifact(kind)
                self.assertTrue(classification["refuse"])
                self.assertFalse(classification["observation"])
                self.assertFalse(classification["handoff"])
                self.assertFalse(classification["verified_reconciliation"])

    def test_receipt_file_shape_and_404_after_write_do_not_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            erp = SimulatedERP(Path(directory) / "erp.sqlite3")
            work = Work(Path(directory) / "work.sqlite3")
            try:
                configure(work, erp)
                case_id = work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4812"}, "analyst")
                for source, key, record in (
                    ("requisitions", "request", "REQ-4812"),
                    ("sites", "site", "DC-WEST-4"),
                    ("finance", "budget", "CC-742"),
                    ("supplier", "quote", "REQ-4812"),
                ):
                    work.refresh_fact(case_id, source, key, record, "analyst", ["operator"])
                review = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                work.commit(case_id, review["proposal"]["id"], "scheduler")
                order = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                proposal_id = order["proposal"]["id"]
                work.approve(case_id, proposal_id, "manager", ["manager"])

                # The destination commits, but the write acknowledgement is lost.
                erp.drop_after_write = True
                self.assertEqual(work.commit(case_id, proposal_id, "scheduler")["status"], "unknown")
                destination_record = erp.order_for_key(proposal_id)
                # Live Attest path: <attest_home>/receipts/<case_id>.json.
                # Keep the shipped top-level receipt shape opaque to SyberWork.
                receipt_file = {
                    "schema": "attest.receipt/v0",
                    "case_id": case_id,
                    "created_at": "2026-10-24T00:00:00Z",
                    "doc": {
                        "source": "demo/spec.md",
                        "sha256_before": "before-digest",
                        "sha256_after": "after-digest",
                    },
                    "claim": {
                        "id": "claim-1",
                        "kind": "descriptive",
                        "lines": [1, 1],
                        "text": "The app displays the updated value.",
                        "expects_before": "old value",
                        "expects_after": "new value",
                    },
                    "authority": {
                        "policy": "attest.policy.yaml",
                        "decision": "doc_repair_allowed",
                        "rule": "descriptive claims may be repaired",
                    },
                    "proposal": {"agent": "worker", "patch_sha256": "patch-digest", "patch": "diff"},
                    "approval": {"actor": "attest", "proposal_sha256": "patch-digest"},
                    "evidence": {
                        "before": {
                            "status": "failed",
                            "observed": {"value": "old value"},
                            "note": "Kane evidence pack before-pack",
                            "screenshot": None,
                        },
                        "after": {
                            "status": "proven",
                            "observed": {"value": "new value"},
                            "note": "Kane evidence pack after-pack",
                            "screenshot": None,
                        },
                    },
                    "handoff": None,
                    "disposition": "repaired",
                    "verified": False,
                    "ticket": {"path": "tickets/case-1.json", "title": "Updated value"},
                    "app": {"original": "app", "candidate": "candidate", "patch_sha256": "patch-digest"},
                    "worker": "syberwork-dispatch",
                }
                self.assertTrue(classify_attest_artifact(receipt_file["schema"])["observation"])
                self.assertFalse(receipt_file["verified"])
                self.assertEqual(receipt_file["evidence"]["after"]["status"], "proven")
                # Kane proved the app claim; that does not reconcile an external write.
                self.assertFalse(classify_attest_artifact(receipt_file["schema"])["verified_reconciliation"])
                observation = work.observe(
                    case_id,
                    key="attest.run_receipt",
                    value=receipt_file,
                    source="attest",
                    version="attest.receipt/v0",
                    actor="attest",
                    verified=False,
                )
                self.assertFalse(verified_reconciliation(observation))
                acceptance = work.inspect(case_id)["acceptance"]
                self.assertFalse(next(row for row in acceptance if row["id"] == "order_recorded")["passed"])

                # A temporarily missing lookup result does not undo or disprove the write.
                status_url = f"{erp.base}/orders/by-key/{proposal_id}"
                missing = urllib.error.HTTPError(status_url, 404, "Not Found", {}, None)
                with patch("syberwork.core.HTTP.open", side_effect=missing):
                    checked = work.reconcile(case_id, proposal_id, "manager", ["manager"])
                self.assertEqual((checked["status"], checked["reason"]), ("pending", "not_found_not_proof"))
                self.assertEqual(checked["event"]["kind"], "reconciliation_checked")
                self.assertEqual(erp.order_count(), 1)
                self.assertFalse(verified_reconciliation(checked["event"]))
                acceptance = work.inspect(case_id)["acceptance"]
                self.assertFalse(next(row for row in acceptance if row["id"] == "order_recorded")["passed"])
            finally:
                work.close()
                erp.close()

    def test_only_destination_status_url_path_verifies_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            erp = SimulatedERP(Path(directory) / "erp.sqlite3")
            work = Work(Path(directory) / "work.sqlite3")
            try:
                configure(work, erp)
                case_id = work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4812"}, "analyst")
                for source, key, record in (
                    ("requisitions", "request", "REQ-4812"),
                    ("sites", "site", "DC-WEST-4"),
                    ("finance", "budget", "CC-742"),
                    ("supplier", "quote", "REQ-4812"),
                ):
                    work.refresh_fact(case_id, source, key, record, "analyst", ["operator"])
                review = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                work.commit(case_id, review["proposal"]["id"], "scheduler")

                proposal = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                proposal_id = proposal["proposal"]["id"]
                work.approve(case_id, proposal_id, "manager", ["manager"])
                erp.drop_after_write = True
                self.assertEqual(work.commit(case_id, proposal_id, "scheduler")["status"], "unknown")

                result = work.reconcile(case_id, proposal_id, "manager", ["manager"])
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["event"]["kind"], "reconciled")
                self.assertTrue(verified_reconciliation(result["event"]))
                proof = result["event"]["body"]["proof"]
                destination_record = erp.order_for_key(proposal_id)
                self.assertEqual(proof["idempotency_key"], proposal_id)
                self.assertEqual(proof["request_digest"], digest(proposal["proposal"]["args"]))
                self.assertEqual(proof["request_digest"], destination_record["request_digest"])
                self.assertEqual(proof["external_id"], destination_record["external_id"])
                self.assertFalse(classify_attest_artifact("attest.receipt/v0")["verified_reconciliation"])
            finally:
                work.close()
                erp.close()


if __name__ == "__main__":
    unittest.main()
