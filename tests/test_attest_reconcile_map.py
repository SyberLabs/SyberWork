import tempfile
import unittest
from pathlib import Path

from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from syberlabs.attest_reconcile_map import classify_attest_artifact
from syberlabs.evidence import acceptance_results, verified_reconciliation
from syberwork.core import Work


class AttestArtifactClassification(unittest.TestCase):
    def test_run_receipt_is_observation_not_effect_success(self):
        classification = classify_attest_artifact("RunReceipt")
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
                "value": {"kind": "RunReceipt", "status": "proven", "effect_succeeded": True},
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
                self.assertEqual(result["event"]["body"]["proof"]["idempotency_key"], proposal_id)
                self.assertFalse(classify_attest_artifact("RunReceipt")["verified_reconciliation"])
            finally:
                work.close()
                erp.close()


if __name__ == "__main__":
    unittest.main()
