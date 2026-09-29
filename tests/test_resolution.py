import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from syberwork.core import Rejected, Work


class AuthoritativeResolution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.erp = SimulatedERP(Path(self.temp.name) / "erp.sqlite3")
        self.addCleanup(self.erp.close)
        self.work = Work(Path(self.temp.name) / "work.sqlite3")
        self.addCleanup(self.work.close)
        configure(self.work, self.erp)

    def site_case(self):
        case = self.work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4813"}, "analyst")
        self.work.refresh_fact(case, "requisitions", "request", "REQ-4813", "analyst", ["operator"])
        self.work.refresh_fact(case, "site_options", "site_options", "REQ-4813", "analyst", ["operator"])
        return case

    def quote_case(self):
        case = self.work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4814"}, "analyst")
        self.work.refresh_fact(case, "requisitions", "request", "REQ-4814", "analyst", ["operator"])
        self.work.refresh_fact(case, "quote_options", "quote_options", "REQ-4814", "analyst", ["operator"])
        return case

    def test_site_task_needs_authoritative_choice_and_owner(self):
        case = self.site_case()
        task = self.work.request_resolution(case, "site", "analyst", ["operator"])
        self.assertEqual(task["body"]["owner_role"], "logistics")
        self.assertEqual(task["body"]["choices"], ["DC-WEST-4", "DC-EAST-2"])
        self.assertEqual(self.work.resolve_resolution(case, task["body"]["id"], "logistics", ["logistics"])["status"], "pending")
        with self.assertRaises(Rejected):
            self.erp.select_site("procurement", "REQ-4813", "DC-WEST-4")
        self.erp.select_site("logistics", "REQ-4813", "DC-WEST-4")
        with self.assertRaises(Rejected):
            self.work.resolve_resolution(case, task["body"]["id"], "analyst", ["operator"])
        done = self.work.resolve_resolution(case, task["body"]["id"], "l.chen", ["logistics"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["choice"], "DC-WEST-4")
        self.assertEqual(self.work.inspect(case)["resolutions"][0]["status"], "completed")
        self.assertTrue(self.work.verify_chain(case))

    def test_task_evidence_must_belong_to_case_input(self):
        case = self.work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4812"}, "analyst")
        self.work.refresh_fact(case, "requisitions", "request", "REQ-4813", "analyst", ["operator"])
        self.work.refresh_fact(case, "site_options", "site_options", "REQ-4813", "analyst", ["operator"])
        with self.assertRaises(Rejected) as error:
            self.work.request_resolution(case, "site", "analyst", ["operator"])
        self.assertEqual(error.exception.code, "resolution_evidence_mismatch")

    def test_quote_choice_is_source_backed_and_case_bound(self):
        case = self.quote_case()
        task = self.work.request_resolution(case, "quote", "analyst", ["operator"])
        self.assertEqual(self.work.resolve_resolution(case, task["body"]["id"], "p.soto", ["procurement"])["status"], "pending")
        self.erp.select_quote("procurement", "REQ-4814", "Q-882A")
        done = self.work.resolve_resolution(case, task["body"]["id"], "p.soto", ["procurement"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["choice"], "Q-882A")
        with self.assertRaises(Rejected):
            self.work.resolve_resolution(case, task["body"]["id"], "p.soto", ["procurement"])

    def test_open_site_task_blocks_review_even_after_source_changes(self):
        case = self.site_case()
        task = self.work.request_resolution(case, "site", "analyst", ["operator"])
        self.erp.select_site("logistics", "REQ-4813", "DC-WEST-4")
        self.work.refresh_fact(case, "requisitions", "request", "REQ-4813", "analyst", ["operator"])
        self.work.refresh_fact(case, "sites", "site", "DC-WEST-4", "analyst", ["operator"])
        before = self.work.compiled_propose(case, "scheduler", ["operator", "compiled"])
        self.assertEqual(before["decision"]["reason"], "resolution_open:site")
        self.work.resolve_resolution(case, task["body"]["id"], "l.chen", ["logistics"])
        after = self.work.compiled_propose(case, "scheduler", ["operator", "compiled"])
        self.assertEqual(after["decision"]["status"], "allowed")

    def test_escalation_and_explicit_cancellation_are_terminal(self):
        case = self.quote_case()
        task = self.work.request_resolution(case, "quote", "analyst", ["operator"])
        with self.assertRaises(Rejected):
            self.work.escalate_resolution(case, task["body"]["id"], "manager", ["manager"])
        with patch("syberwork.core.time.time", return_value=task["body"]["due_at"] + 1):
            escalated = self.work.escalate_resolution(case, task["body"]["id"], "manager", ["manager"])
        self.assertEqual(escalated["kind"], "resolution_escalated")
        with self.assertRaises(Rejected):
            self.work.cancel_case(case, "supplier qualification unresolved", "analyst", ["operator"])
        self.work.cancel_case(case, "supplier qualification unresolved", "manager", ["manager"])
        self.assertEqual(self.work.inspect(case)["status"], "cancelled")
        proposed = self.work.propose(case, "record_review", {}, "analyst", ["operator"])
        self.assertEqual(proposed["decision"]["reason"], "case_cancelled")
        self.assertEqual(self.erp.order_count(), 0)


if __name__ == "__main__":
    unittest.main()
