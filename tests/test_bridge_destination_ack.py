"""A destination acknowledgement counts only from an authoritative HTTP status.

The status lookup proves a record only from a 200. A 202 Accepted, 203 Non-Authoritative Information,
206 Partial Content or any other 2xx whose body is a fully matching record stays ``pending``
(``status_not_authoritative``). An economic settlement receipt counts only from a 200 or 201; any other
2xx leaves the effect ``unknown`` with its reservation held. Every case runs against a loopback
destination and moves no funds. Positive controls show the same fixtures still reconcile and settle.
"""

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from syberlabs.evidence import verified_reconciliation
from syberwork.core import Rejected, Work, digest

# 2xx statuses that are not "here is the destination's record". 204 and 205 carry no body.
NOT_AUTHORITATIVE = (201, 202, 203, 204, 205, 206, 207, 208, 226)
EMPTY = (204, 205)


class Destination:
    """Loopback destination: POST stores a record by Idempotency-Key, GET /status/{key} returns it."""

    def __init__(self, record_for):
        self.record_for = record_for
        self.records, self.posts, self.lookups = {}, 0, 0
        self.write_code, self.lookup_code, self.drop_ack = 200, 200, False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code, record):
                data = b"" if record is None or code in EMPTY else json.dumps(record).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                owner.posts += 1
                key = self.headers["Idempotency-Key"]
                args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.records[key] = owner.record_for(key, args)
                if owner.drop_ack:
                    self.connection.shutdown(2)
                    return
                self.reply(owner.write_code, owner.records[key])

            def do_GET(self):
                owner.lookups += 1
                record = owner.records.get(self.path.rsplit("/", 1)[-1])
                self.reply(owner.lookup_code if record else 404, record)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def committed(key, args):
    return {"state": "committed", "idempotency_key": key, "request_digest": digest(args), "external_id": "SHIP-" + key}


def settled(key, args):
    return {"state": "settled", "idempotency_key": key, "request_digest": digest(args),
            "amount_units": args["amount_units"], "asset": args["asset"],
            "counterparty": args["counterparty"], "external_id": "tx-" + key}


class Cell(unittest.TestCase):
    record_for = staticmethod(committed)

    def setUp(self):
        self.fresh()

    def fresh(self):
        """A new store, destination and installation. The shared budget admits one settled purchase."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dest = Destination(self.record_for)
        self.addCleanup(self.dest.close)
        self.work = Work(Path(folder.name) / "work.sqlite3")
        self.addCleanup(self.work.close)
        self.install()

    def history(self, case):
        return self.work.inspect(case)["events"]

    def assert_unresolved(self, case, key, fresh):
        """No verified reconciliation, acceptance unmet, no retry and no fresh effect of the same action."""
        self.assertFalse(any(verified_reconciliation(e) for e in self.history(case)))
        self.assertFalse(self.work.inspect(case)["complete"])
        with self.assertRaisesRegex(Rejected, "effect_claimed"):
            self.work.commit(case, key, "operator")
        self.assertEqual(fresh()["decision"]["reason"], "effect_unresolved:" + self.action)

    def assert_verified(self, case, key, result):
        self.assertEqual((result["status"], result["reason"]), ("verified", "destination_record_matched"))
        record = self.dest.records[key]
        proof = result["event"]["body"]["proof"]
        self.assertEqual((proof["idempotency_key"], proof["request_digest"], proof["external_id"]),
                         (key, record["request_digest"], record["external_id"]))
        self.assertEqual(proof["response_digest"], digest(record))
        self.assertTrue(self.work.inspect(case)["complete"])


class StatusLookupAck(Cell):
    """``http`` action: only a 200 status lookup is the destination's record."""

    action = "ship"

    def install(self):
        self.work.install_action("ship", {"kind": "http", "url": self.dest.base + "/ship",
                                          "status_url": self.dest.base + "/status/{key}"})
        self.work.install_contract({"id": "ack", "version": 1, "inputs": {"subject": "string"},
            "actions": {"ship": {"required_facts": [{"key": "order", "source": "ledger", "verified": True}],
                                 "arguments": {"order_id": "fact:order.id"}}},
            "acceptance": [{"id": "shipped", "kind": "effect", "action": "ship"}]})
        self.work.install_policy({"version": 1, "actions": {"ship": {"roles": ["operator"]}}})

    def unknown(self):
        case = self.work.create_case("ack", 1, {"subject": "lost ack"}, "operator")
        self.work.observe(case, "order", {"id": "O-1"}, "ledger", "v1", "ledger", verified=True)
        key = self.propose(case)["proposal"]["id"]
        self.dest.drop_ack = True
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "unknown")
        self.dest.drop_ack = False
        self.assertIn(key, self.dest.records)
        return case, key

    def propose(self, case):
        return self.work.propose(case, "ship", {"order_id": "O-1"}, "operator", ["operator"])

    def test_matching_record_on_a_non_200_success_status_stays_pending(self):
        case, key = self.unknown()
        for code in NOT_AUTHORITATIVE:
            with self.subTest(code=code):
                self.dest.lookup_code = code
                result = self.work.reconcile(case, key, "manager", ["manager"])
                self.assertEqual((result["status"], result["reason"]), ("pending", "status_not_authoritative"))
                self.assertEqual(result["event"]["kind"], "reconciliation_checked")
                self.assert_unresolved(case, key, lambda: self.propose(case))
        self.assertEqual(self.dest.posts, 1)
        self.assertEqual(self.dest.lookups, len(NOT_AUTHORITATIVE))
        # The pending checks do not poison the effect: the same record on a 200 reconciles once.
        self.dest.lookup_code = 200
        self.assert_verified(case, key, self.work.reconcile(case, key, "manager", ["manager"]))
        self.assertEqual(self.dest.posts, 1)

    def test_positive_control_200_lookup_reconciles(self):
        case, key = self.unknown()
        self.assert_verified(case, key, self.work.reconcile(case, key, "manager", ["manager"]))
        with self.assertRaisesRegex(Rejected, "already reconciled"):
            self.work.reconcile(case, key, "manager", ["manager"])


class SettlementAck(Cell):
    """``economic_http`` action: the receipt and the status lookup both need an authoritative status."""

    action = "buy_data"
    record_for = staticmethod(settled)

    def install(self):
        self.work.install_action("buy_data", {"kind": "economic_http",
            "url": self.dest.base + "/settle", "status_url": self.dest.base + "/status/{key}",
            "rail": "usdc_service", "asset": "USDC:base", "counterparty": "vendor-A",
            "operation": "purchase_capability"})
        self.work.install_contract({"id": "research", "version": 1, "inputs": {"subject": "string"},
            "actions": {"buy_data": {
                "required_facts": [{"key": "quote", "source": "vendor", "verified": True, "max_age_seconds": 3600}],
                "arguments": {"amount_units": "fact:quote.units"},
                "approval_role": "manager", "max_amount_units": "1000000"}},
            "acceptance": [{"id": "data", "kind": "effect", "action": "buy_data"}]})
        self.work.install_policy({"version": 1, "actions": {"buy_data": {
            "roles": ["operator"], "economic": {"budget_id": "research-q3",
                "budget_units": "1500000", "max_amount_units": "1000000",
                "asset": "USDC:base", "rail": "usdc_service", "counterparties": ["vendor-A"]}}}})

    def case(self):
        case = self.work.create_case("research", 1, {"subject": "chips"}, "operator")
        evidence = self.work.observe(case, "quote", {"units": "900000"}, "vendor", "q1", "vendor", verified=True)["hash"]
        return case, evidence

    def propose(self, case, evidence):
        return self.work.propose(case, "buy_data", {
            "operation": "purchase_capability", "amount_units": "900000", "asset": "USDC:base",
            "counterparty": "vendor-A", "purpose": "shipment evidence", "evidence": [evidence],
            "expires_at": time.time() + 600}, "operator", ["operator"])

    def approved(self):
        case, evidence = self.case()
        result = self.propose(case, evidence)
        self.assertEqual(result["decision"]["status"], "needs_approval")
        key = result["proposal"]["id"]
        self.work.approve(case, key, "manager", ["manager"])
        return case, evidence, key

    def reservation(self, key):
        with self.work.tx() as db:
            return db.execute("SELECT state FROM economic_reservations WHERE proposal_id=?", (key,)).fetchone()[0]

    def assert_budget_held(self):
        other, evidence = self.case()
        self.assertEqual(self.propose(other, evidence)["decision"]["reason"], "economic_budget_exceeded")

    def assert_settled(self, case, key, result):
        self.assert_verified(case, key, result)
        self.assertEqual(self.reservation(key), "settled")

    def test_receipt_on_accepted_or_partial_status_is_unknown(self):
        for code in (202, 203, 206, 207, 208, 226):
            with self.subTest(code=code):
                self.fresh()
                case, evidence, key = self.approved()
                self.dest.write_code = code
                result = self.work.commit(case, key, "operator")
                self.assertEqual(result["status"], "unknown")
                self.assertIn(key, self.dest.records)  # the destination did write
                kinds = [e["kind"] for e in self.history(case)]
                self.assertNotIn("effect_succeeded", kinds)
                self.assertEqual(next(e for e in self.history(case) if e["kind"] == "effect_unknown")["body"]["error"],
                                 "destination_error")
                self.assertEqual(self.reservation(key), "reserved")
                self.assert_budget_held()
                self.assert_unresolved(case, key, lambda: self.propose(case, evidence))
                # Only the destination's 200 lookup settles it, and the budget stays consumed.
                self.assert_settled(case, key, self.work.reconcile(case, key, "manager", ["manager"]))
                self.assert_budget_held()

    def test_lookup_on_a_non_200_success_status_retains_reservation(self):
        case, evidence, key = self.approved()
        self.dest.drop_ack = True
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "unknown")
        self.dest.drop_ack = False
        for code in NOT_AUTHORITATIVE:
            with self.subTest(code=code):
                self.dest.lookup_code = code
                result = self.work.reconcile(case, key, "manager", ["manager"])
                self.assertEqual((result["status"], result["reason"]), ("pending", "status_not_authoritative"))
                self.assertEqual(self.reservation(key), "reserved")
                self.assert_budget_held()
                self.assert_unresolved(case, key, lambda: self.propose(case, evidence))
        self.dest.lookup_code = 200
        self.assert_settled(case, key, self.work.reconcile(case, key, "manager", ["manager"]))
        self.assertEqual(self.dest.posts, 1)

    def test_positive_control_200_and_201_receipts_settle(self):
        for code in (200, 201):
            with self.subTest(code=code):
                self.fresh()
                case, _evidence, key = self.approved()
                self.dest.write_code = code
                result = self.work.commit(case, key, "operator")
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["event"]["body"]["output"], {"settlement": self.dest.records[key]})
                self.assertEqual(self.reservation(key), "settled")
                self.assertTrue(self.work.inspect(case)["complete"])
                self.assert_budget_held()


if __name__ == "__main__":
    unittest.main()
