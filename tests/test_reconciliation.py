import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from syberwork.core import Rejected, Work


class VerifiedExternalOutcome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.erp = SimulatedERP(Path(self.temp.name) / "erp.sqlite3")
        self.addCleanup(self.erp.close)
        self.work = Work(Path(self.temp.name) / "work.sqlite3")
        configure(self.work, self.erp)
        self.case = self.work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4812"}, "analyst")
        for source, key, record in (("requisitions", "request", "REQ-4812"),
                                    ("sites", "site", "DC-WEST-4"),
                                    ("finance", "budget", "CC-742"),
                                    ("supplier", "quote", "REQ-4812")):
            self.work.refresh_fact(self.case, source, key, record, "analyst", ["operator"])
        review = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.work.commit(self.case, review["proposal"]["id"], "scheduler")

    def order(self):
        proposal = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        key = proposal["proposal"]["id"]
        self.work.approve(self.case, key, "manager", ["manager"])
        return key

    def test_text_claim_cannot_complete_order(self):
        key = self.order()
        self.erp.drop_after_write = True
        self.assertEqual(self.work.commit(self.case, key, "scheduler")["status"], "unknown")
        with self.assertRaisesRegex(Rejected, "manual_reconciliation_disabled"):
            self.work.reconcile(self.case, key, "manager", ["manager"], success=True, evidence="PO-FAKE")
        self.assertFalse(self.work.inspect(self.case)["complete"])

    def test_legacy_unverified_reconciliation_never_satisfies_acceptance(self):
        key = self.order()
        with patch.object(self.work, "_execute", side_effect=ConnectionError("response lost")):
            self.work.commit(self.case, key, "scheduler")
        with self.work.tx() as db:
            self.work._append(db, self.case, "reconciled", {
                "proposal_id": key, "action": "issue_order", "success": True,
                "evidence": "PO-FAKE", "actor": "manager",
            })
        self.work.signoff(self.case, "manager", ["manager"], "manager")
        self.assertFalse(self.work.inspect(self.case)["complete"])

    def test_matching_status_lookup_completes_only_after_signoff(self):
        key = self.order()
        self.erp.drop_after_write = True
        self.work.commit(self.case, key, "scheduler")
        self.work.signoff(self.case, "manager", ["manager"], "manager")
        result = self.work.reconcile(self.case, key, "manager", ["manager"])
        self.assertEqual(result["status"], "verified")
        self.assertFalse(self.work.inspect(self.case)["complete"])
        self.work.signoff(self.case, "manager", ["manager"], "manager")
        self.assertTrue(self.work.inspect(self.case)["complete"])
        self.assertEqual(self.erp.order_count(), 1)

    def test_mismatched_external_record_never_counts_as_proof(self):
        key = self.order()
        self.erp.drop_after_write = True
        self.work.commit(self.case, key, "scheduler")
        with sqlite3.connect(self.erp.path) as db:
            db.execute("UPDATE orders SET request_digest=? WHERE idempotency_key=?", ("0" * 64, key))
        result = self.work.reconcile(self.case, key, "manager", ["manager"])
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(self.work.inspect(self.case)["complete"])
        retry = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.assertEqual(retry["decision"]["reason"], "effect_unresolved:issue_order")

    def test_not_found_is_pending_and_blocks_new_effect(self):
        key = self.order()
        with patch.object(self.work, "_execute", side_effect=ConnectionError("response lost")):
            self.assertEqual(self.work.commit(self.case, key, "scheduler")["status"], "unknown")
        self.assertEqual(self.work.reconcile(self.case, key, "manager", ["manager"])["status"], "pending")
        self.assertEqual(self.erp.order_count(), 0)
        second = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.assertEqual(second["decision"]["reason"], "effect_unresolved:issue_order")
        self.assertEqual(self.work.commit(self.case, second["proposal"]["id"], "scheduler")["decision"]["reason"], "effect_unresolved:issue_order")

    def test_declared_no_write_precondition_failure_allows_fresh_proposal(self):
        key = self.order()
        self.erp.change_quote_version("quote:4")
        self.assertEqual(self.work.commit(self.case, key, "scheduler")["status"], "rejected")
        self.assertEqual(self.erp.order_count(), 0)
        self.work.refresh_fact(self.case, "supplier", "quote", "REQ-4812", "analyst", ["operator"])
        newer = self.order()
        self.assertEqual(self.work.commit(self.case, newer, "scheduler")["status"], "succeeded")
        self.assertEqual(self.erp.order_count(), 1)

    def test_status_lookup_is_bound_to_the_action_origin(self):
        for url in ("http://127.0.0.1:8999@evil.example/orders/by-key/{key}",
                    "https://different.example/orders/by-key/{key}",
                    "http://127.0.0.1:8999/orders/by-key/{key}?next={key}"):
            with self.subTest(url=url), self.assertRaises(Rejected):
                self.work.install_action("bad-lookup", {
                    "kind": "http", "url": "http://127.0.0.1:8999/orders", "status_url": url,
                })


if __name__ == "__main__":
    unittest.main()
