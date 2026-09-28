"""Economic effects use a local settlement simulator; these tests never move funds."""

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from syberwork.core import Rejected, Work, digest


class EconomicActions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Work(Path(tmp.name) / "work.sqlite3")
        self.records = {}
        self.mode = "success"
        owner = self

        class Settlement(BaseHTTPRequestHandler):
            def do_POST(self):
                key = self.headers["Idempotency-Key"]
                args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if owner.mode == "prewrite":
                    self.send_response(412)
                    self.end_headers()
                    return
                record = {"state": "settled", "idempotency_key": key,
                          "request_digest": digest(args), "amount_units": args["amount_units"],
                          "asset": args["asset"], "counterparty": args["counterparty"],
                          "external_id": "tx-" + key}
                owner.records[key] = record
                if owner.mode == "lost":
                    self.connection.shutdown(2)
                    return
                if owner.mode == "mismatch":
                    record = {**record, "amount_units": "1"}
                data = json.dumps(record).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                key = self.path.rsplit("/", 1)[-1]
                record = owner.records.get(key)
                if record is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(record).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Settlement)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        root = f"http://127.0.0.1:{self.server.server_port}"
        self.work.install_action("buy_data", {"kind": "economic_http",
            "url": root + "/settle", "status_url": root + "/status/{key}",
            "rail": "usdc_service", "asset": "USDC:base", "counterparty": "vendor-A",
            "operation": "purchase_capability",
            "no_write_statuses": [412]})
        self.work.install_contract({"id": "research", "version": 1,
            "inputs": {"subject": "string"}, "actions": {"buy_data": {
                "required_facts": [{"key": "quote", "source": "vendor", "verified": True, "max_age_seconds": 3600}],
                "arguments": {"amount_units": "fact:quote.units"},
                "approval_role": "manager", "max_amount_units": "1000000"}},
            "acceptance": [{"id": "data", "kind": "effect", "action": "buy_data"}]})
        self.policy = {"version": 1, "actions": {"buy_data": {
            "roles": ["operator"], "economic": {"budget_id": "research-q3",
                "budget_units": "1500000", "max_amount_units": "1000000",
                "asset": "USDC:base", "rail": "usdc_service",
                "counterparties": ["vendor-A"]}}}}
        self.work.install_policy(self.policy)

    def case(self, units="900000"):
        case = self.work.create_case("research", 1, {"subject": "chips"}, "operator")
        event = self.work.observe(case, "quote", {"units": units}, "vendor", "q1", "vendor", verified=True)
        return case, event["hash"]

    def propose(self, case, evidence, units="900000", **changes):
        args = {"operation": "purchase_capability", "amount_units": units,
                "asset": "USDC:base", "counterparty": "vendor-A",
                "purpose": "shipment evidence", "evidence": [evidence],
                "expires_at": time.time() + 600}
        args.update(changes)
        return self.work.propose(case, "buy_data", args, "operator", ["operator"])

    def approved(self, case, evidence, units="900000"):
        result = self.propose(case, evidence, units)
        self.assertEqual(result["decision"]["status"], "needs_approval")
        key = result["proposal"]["id"]
        self.work.approve(case, key, "manager", ["manager"])
        return key

    def test_intent_requires_exact_units_and_bound_evidence(self):
        case, evidence = self.case()
        for bad in ("0", "0900000", "1.5", 900000, "-1"):
            with self.subTest(bad=bad):
                self.assertEqual(self.propose(case, evidence, amount_units=bad)["decision"]["status"], "denied")
        self.assertEqual(self.propose(case, "0" * 64)["decision"]["reason"], "economic_evidence_mismatch")
        self.assertEqual(self.propose(case, evidence, counterparty="vendor-B")["decision"]["status"], "denied")
        self.assertEqual(self.propose(case, evidence, expires_at=time.time() - 1)["decision"]["status"], "denied")
        self.assertEqual(self.propose(case, evidence, pay_to="attacker-wallet")["decision"]["reason"], "economic_intent_invalid")
        self.assertEqual(self.propose(case, evidence, operation="transfer")["decision"]["reason"], "economic_operation_denied")
        self.assertEqual(self.propose(case, evidence, amount_units="9" * 10000)["decision"]["status"], "denied")

    def test_matching_settlement_receipt_and_budget(self):
        first, evidence = self.case()
        key = self.approved(first, evidence)
        result = self.work.commit(first, key, "operator")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(self.work.inspect(first)["complete"])
        self.assertTrue(self.work.verify_chain(first))
        claim = next(e for e in self.work.inspect(first)["events"] if e["kind"] == "effect_started")
        self.assertIn("evidence_snapshot", claim["body"])
        second, evidence2 = self.case()
        self.assertEqual(self.propose(second, evidence2)["decision"]["reason"], "economic_budget_exceeded")

    def test_concurrent_cases_share_one_budget(self):
        a, ae = self.case()
        b, be = self.case()
        ka, kb = self.approved(a, ae), self.approved(b, be)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: self.work.commit(*item, "operator"), ((a, ka), (b, kb))))
        self.assertCountEqual([r.get("status", r.get("decision", {}).get("reason")) for r in results],
                              ["succeeded", "economic_budget_exceeded"])
        self.assertEqual(len(self.records), 1)

    def test_lost_receipt_retains_budget_until_verified_reconciliation(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        self.mode = "lost"
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "unknown")
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["reason"], "economic_budget_exceeded")
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "verified")
        self.assertTrue(self.work.inspect(case)["complete"])
        proof = next(e["body"]["proof"] for e in self.work.inspect(case)["events"] if e["kind"] == "reconciled")
        self.assertEqual(proof["amount_units"], "900000")
        self.assertEqual(proof["asset"], "USDC:base")
        self.assertEqual(proof["counterparty"], "vendor-A")

    def test_mismatched_receipt_never_succeeds(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        self.mode = "mismatch"
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "unknown")
        self.assertFalse(self.work.inspect(case)["complete"])

    def test_unknown_status_404_or_mismatch_retains_reservation(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        self.mode = "lost"
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "unknown")
        record = self.records.pop(key)
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "pending")
        self.records[key] = {**record, "counterparty": "vendor-B"}
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "unverified")
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["reason"], "economic_budget_exceeded")
        self.assertFalse(self.work.inspect(case)["complete"])

    def test_amount_must_be_bound_to_verified_quote(self):
        broken = {"id": "unbound", "version": 1, "inputs": {"subject": "string"},
                  "actions": {"buy_data": {"required_facts": [{"key": "quote", "source": "vendor",
                      "verified": True}], "approval_role": "manager"}},
                  "acceptance": [{"id": "data", "kind": "effect", "action": "buy_data"}]}
        self.work.install_contract(broken)
        case = self.work.create_case("unbound", 1, {"subject": "chips"}, "operator")
        event = self.work.observe(case, "quote", {"units": "900000"}, "vendor", "q1", "vendor", verified=True)
        self.assertEqual(self.propose(case, event["hash"])["decision"]["reason"], "economic_amount_unbound")

    def test_prewrite_rejection_releases_budget(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        self.mode = "prewrite"
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "rejected")
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["status"], "needs_approval")

    def test_policy_and_adapter_are_fail_closed(self):
        with self.assertRaises(Rejected):
            self.work.install_action("unsafe", {"kind": "economic_http", "url": "https://example.com/pay",
                "asset": "USDC:base", "counterparty": "vendor-A", "rail": "usdc_service"})
        case, evidence = self.case()
        changed = {**self.policy, "version": 2, "actions": {"buy_data": {"roles": ["operator"]}}}
        self.work.install_policy(changed)
        self.assertEqual(self.propose(case, evidence)["decision"]["status"], "denied")

    def test_policy_revision_rechecks_before_claim(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        tightened = json.loads(json.dumps(self.policy))
        tightened["version"] = 2
        tightened["actions"]["buy_data"]["economic"]["max_amount_units"] = "100000"
        self.work.install_policy(tightened)
        self.assertEqual(self.work.commit(case, key, "operator")["decision"]["reason"], "economic_limit_exceeded")
        self.assertEqual(len(self.records), 0)
        self.assertFalse(any(e["kind"] == "effect_started" for e in self.work.inspect(case)["events"]))

    def test_budget_id_cannot_be_replenished_by_policy_version(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        self.work.commit(case, key, "operator")
        replenished = json.loads(json.dumps(self.policy))
        replenished["version"] = 2
        replenished["actions"]["buy_data"]["economic"]["budget_units"] = "1800000"
        with self.assertRaisesRegex(Rejected, "budget_id"):
            self.work.install_policy(replenished)
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["reason"], "economic_budget_exceeded")

    def test_malformed_counterparty_allowlist_is_rejected(self):
        malformed = json.loads(json.dumps(self.policy))
        malformed["version"] = 2
        malformed["actions"]["buy_data"]["economic"]["counterparties"] = "vendor-A"
        with self.assertRaisesRegex(Rejected, "counterparties"):
            self.work.install_policy(malformed)

    def test_started_claim_can_reconcile_after_worker_crash(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        with self.work.tx() as db:
            db.execute("INSERT INTO economic_reservations VALUES (?,?,?,?,?,?)",
                       (key, case, "research-q3", "USDC:base", 900000, "reserved"))
            self.work._append(db, case, "effect_started", {"proposal_id": key,
                "action": "buy_data", "idempotency_key": key})
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "pending")
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["reason"], "economic_budget_exceeded")
        with self.assertRaisesRegex(Rejected, "cancellation_denied"):
            self.work.cancel_case(case, "abandon", "manager", ["manager"])

    def test_unresolved_started_claim_blocks_another_payment(self):
        case, evidence = self.case("500000")
        key = self.approved(case, evidence, "500000")
        with self.work.tx() as db:
            db.execute("INSERT INTO economic_reservations VALUES (?,?,?,?,?,?)",
                       (key, case, "research-q3", "USDC:base", 500000, "reserved"))
            self.work._append(db, case, "effect_started", {"proposal_id": key,
                "action": "buy_data", "idempotency_key": key})
        second = self.propose(case, evidence, "500000")
        self.assertEqual(second["decision"]["reason"], "effect_unresolved:buy_data")

    def test_reconcile_during_commit_records_one_terminal_success(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        paid = threading.Event()
        resume = threading.Event()

        def execute_then_pause(action, args, proposal_id):
            output = Work._execute(action, args, proposal_id)
            paid.set()
            self.assertTrue(resume.wait(5))
            return output

        with patch.object(self.work, "_execute", side_effect=execute_then_pause):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.work.commit, case, key, "operator")
                self.assertTrue(paid.wait(5))
                try:
                    self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "verified")
                finally:
                    resume.set()
                self.assertEqual(pending.result()["status"], "succeeded")
        terminal = [e for e in self.work.inspect(case)["events"] if e["kind"] in ("reconciled", "effect_succeeded")]
        self.assertEqual(len(terminal), 1)


if __name__ == "__main__":
    unittest.main()
