"""A destination acknowledgement counts only from an authoritative HTTP status.

The status lookup proves a record only from a 200. A 202 Accepted, 203 Non-Authoritative Information,
206 Partial Content or any other 2xx whose body is a fully matching record stays ``pending``
(``status_not_authoritative``). An economic settlement receipt counts only from a 200 or 201; any other
2xx leaves the effect ``unknown`` with its reservation held. Every case runs against a loopback
destination and moves no funds. Positive controls show the same fixtures still reconcile and settle.

A plain ``http`` write counts as done only from a 200, 201 or 204. A 202 Accepted ("processing has not been
completed"), a 203, a 205-226 or a response without a status leaves the effect ``unknown``
(``destination_not_completed``): acceptance stays unmet and only a 200 status lookup can resolve it.

A completed write whose body names an ``idempotency_key`` or ``request_digest`` other than this write's
acknowledged a different write (``destination_record_mismatch``), and a body with no single meaning cannot say
which (``destination_record_ambiguous``). Either leaves the effect ``unknown``. A body that names neither
field, or is not a JSON object, is not judged.

A body that opens a JSON object but is not strict JSON (a ``NaN``, nesting deeper than 64, trailing data, or cut
off at the 32 KB read, a raw control character in a string) is judged on the identity fields it names at its
top level before its first field that cannot be read, so none of those alone hides another write's key or digest.
The same body that names nothing else still counts from its status.

A body that opens a JSON object and carries further JSON values after it (concatenated or NDJSON-style records) is
judged on every top-level object in that sequence, read the same way, so a second record cannot hide another
write's key or digest behind a first record that names nothing else.
"""

import io
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from syberlabs.evidence import verified_reconciliation
from syberwork.core import HTTP, Rejected, Work, digest
from syberwork.worker import run_once

# 2xx statuses that are not "here is the destination's record". 204 and 205 carry no body.
NOT_AUTHORITATIVE = (201, 202, 203, 204, 205, 206, 207, 208, 226)
EMPTY = (204, 205)
# 2xx statuses that do not say a plain write completed. 299 stands for any unregistered 2xx.
NOT_COMPLETED = (202, 203, 205, 206, 207, 208, 226, 299)


class Destination:
    """Loopback destination: POST stores a record by Idempotency-Key, GET /status/{key} returns it."""

    def __init__(self, record_for):
        self.record_for = record_for
        self.records, self.posts, self.lookups = {}, 0, 0
        self.write_code, self.lookup_code, self.drop_ack = 200, 200, False
        self.ack = None  # (key, args, record) -> the write reply body, when it differs from the stored record
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code, record):
                if record is None or code in EMPTY:
                    data = b""
                else:
                    data = record if isinstance(record, bytes) else json.dumps(record).encode()
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
                body = owner.ack(key, args, owner.records[key]) if owner.ack else owner.records[key]
                self.reply(owner.write_code, body)

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

    def fresh(self, **work):
        """A new store, destination and installation. The shared budget admits one settled purchase."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dest = Destination(self.record_for)
        self.addCleanup(self.dest.close)
        self.work = Work(Path(folder.name) / "work.sqlite3", **work)
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

    def test_lookup_without_an_http_status_stays_pending(self):
        case, key = self.unknown()
        record = json.dumps(self.dest.records[key]).encode()
        with patch.object(HTTP, "open", return_value=io.BytesIO(record)):
            result = self.work.reconcile(case, key, "manager", ["manager"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "status_not_authoritative"))
        self.assert_unresolved(case, key, lambda: self.propose(case))

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


class Ship(Cell):
    """A plain ``http`` write action ``ship``, with or without a status lookup."""

    action = "ship"
    status_lookup = True

    def install(self):
        action = {"kind": "http", "url": self.dest.base + "/ship"}
        if self.status_lookup:
            action["status_url"] = self.dest.base + "/status/{key}"
        self.work.install_action("ship", action)
        self.work.install_contract({"id": "ack", "version": 1, "inputs": {"subject": "string"},
            "actions": {"ship": {"required_facts": [{"key": "order", "source": "ledger", "verified": True}],
                                 "arguments": {"order_id": "fact:order.id"}}},
            "acceptance": [{"id": "shipped", "kind": "effect", "action": "ship"}]})
        self.work.install_policy({"version": 1, "actions": {"ship": {"roles": ["operator"]}}})

    def proposed(self):
        case = self.work.create_case("ack", 1, {"subject": "accepted write"}, "operator")
        self.work.observe(case, "order", {"id": "O-1"}, "ledger", "v1", "ledger", verified=True)
        return case, self.propose(case)["proposal"]["id"]

    def propose(self, case):
        return self.work.propose(case, "ship", {"order_id": "O-1"}, "operator", ["operator"])

    def assert_unknown(self, case, key, result, error):
        """The destination wrote, but the write is ``unknown``: not succeeded, not applied, not accepted."""
        self.assertEqual(result["status"], "unknown")
        self.assertIn(key, self.dest.records)
        kinds = [e["kind"] for e in self.history(case)]
        self.assertNotIn("effect_succeeded", kinds)
        self.assertEqual(next(e for e in self.history(case) if e["kind"] == "effect_unknown")["body"]["error"], error)
        self.assertEqual(self.work.metric("effects_settled_applied_total"), 0)
        self.assertEqual(self.work.metric("effects_pending"), 1)
        self.assert_unresolved(case, key, lambda: self.propose(case))


class WriteAck(Ship):
    """Plain ``http`` write: only 200, 201 or 204 says the write completed; anything else is an unknown effect."""

    def assert_not_completed(self, case, key, result):
        self.assert_unknown(case, key, result, "destination_not_completed")

    def test_write_accepted_but_not_completed_is_unknown(self):
        for code in NOT_COMPLETED:
            with self.subTest(code=code):
                self.fresh()
                case, key = self.proposed()
                self.dest.write_code = code
                self.assert_not_completed(case, key, self.work.commit(case, key, "operator"))
                # Only the destination's 200 lookup resolves it, and the write is not repeated.
                self.assert_verified(case, key, self.work.reconcile(case, key, "manager", ["manager"]))
                self.assertEqual(self.work.metric("effects_settled_applied_total"), 1)
                self.assertEqual(self.dest.posts, 1)

    def test_write_without_an_http_status_is_unknown(self):
        case, key = self.proposed()
        record = json.dumps(committed(key, {"order_id": "O-1"})).encode()
        self.dest.records[key] = json.loads(record)
        with patch.object(HTTP, "open", return_value=io.BytesIO(record)):
            self.assert_not_completed(case, key, self.work.commit(case, key, "operator"))

    def test_worker_settles_an_accepted_write_as_unknown(self):
        self.fresh(effects="worker")
        case, key = self.proposed()
        self.dest.write_code = 202
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "queued")
        self.assert_not_completed(case, key, run_once(self.work))
        self.assertEqual(self.work.metric("queue_depth"), 0)

    def test_positive_control_200_201_204_writes_succeed(self):
        for code in (200, 201, 204):
            with self.subTest(code=code):
                self.fresh()
                case, key = self.proposed()
                self.dest.write_code = code
                result = self.work.commit(case, key, "operator")
                self.assertEqual(result["status"], "succeeded")
                body = "" if code in EMPTY else json.dumps(self.dest.records[key])
                self.assertEqual(result["event"]["body"]["output"], {"status": code, "body": body})
                self.assertTrue(self.work.inspect(case)["complete"])
                self.assertEqual(self.work.metric("effects_settled_applied_total"), 1)
                self.assertEqual(self.dest.posts, 1)


class WriteAckWithoutLookup(WriteAck):
    """Without a status lookup, a write that was only accepted stays unresolved; nobody can declare it done.

    The inherited cases run again without a lookup; only the reconcile case differs.
    """

    status_lookup = False

    def test_write_accepted_but_not_completed_is_unknown(self):
        case, key = self.proposed()
        self.dest.write_code = 202
        self.assert_not_completed(case, key, self.work.commit(case, key, "operator"))
        with self.assertRaisesRegex(Rejected, "reconciliation_unavailable"):
            self.work.reconcile(case, key, "manager", ["manager"])
        self.assert_unresolved(case, key, lambda: self.propose(case))
        self.assertEqual(self.dest.lookups, 0)


def named(**fields):
    """A write reply: the stored record with ``fields`` replaced."""
    return lambda _key, _args, record: {**record, **fields}


# Write replies that name another write. Each is acknowledged with a completion status.
OTHER_WRITE = {
    "another key": lambda key, _args, record: {**record, "idempotency_key": "other-" + key},
    "key in another case": lambda key, _args, record: {**record, "idempotency_key": key.upper()},
    "null key": named(idempotency_key=None),
    "key in a list": lambda key, _args, record: {**record, "idempotency_key": [key]},
    "another request digest": named(request_digest=digest({"order_id": "O-2"})),
    "digest of the arguments with a field added": lambda _key, args, record: {
        **record, "request_digest": digest({**args, "note": "rewritten"})},
    "only a key, another one": lambda key, _args, _record: {"id": "SHIP-1", "idempotency_key": "other-" + key},
    "only a digest, another one": lambda _key, _args, _record: {"id": "SHIP-1", "request_digest": "0" * 64},
}

# Write replies with no single meaning (RFC 8259 sections 4, 6 and 8.2). The first one reads as this write's
# key under last-wins and as another write's under first-wins.
AMBIGUOUS = {
    "key named twice": lambda key, _args, _record:
        ('{"idempotency_key": "other-%s", "idempotency_key": "%s"}' % (key, key)).encode(),
    "unrelated field named twice": lambda key, _args, _record:
        ('{"id": "SHIP-1", "id": "SHIP-2", "idempotency_key": "%s"}' % key).encode(),
    "number overflows a double": lambda key, _args, _record:
        ('{"idempotency_key": "%s", "amount": 1e400}' % key).encode(),
    "unpaired surrogate": lambda key, _args, _record:
        ('{"idempotency_key": "%s", "note": "\\udc80"}' % key).encode(),
}

# Write replies that do not name another write: no identity field, the same identity, or no JSON object.
NAMES_NOTHING_ELSE = {
    "matching record": None,
    "reference ERP reply": lambda _key, _args, _record: {"id": "SHIP-1", "replayed": False},
    "replayed reply": lambda _key, _args, _record: {"id": "SHIP-1", "replayed": True},
    "this key only": lambda key, _args, _record: {"id": "SHIP-1", "idempotency_key": key},
    "this digest only": lambda _key, args, _record: {"request_digest": digest(args)},
    "JSON array": lambda key, _args, _record: [{"idempotency_key": "other-" + key}],
    "JSON string": lambda _key, _args, _record: "ok",
    "plain text": lambda _key, _args, _record: b"accepted and written",
    "nested identity fields": lambda key, _args, _record: {"data": {"idempotency_key": "other-" + key}},
}


def text(template):
    """A write reply as raw bytes: ``template`` with ``{key}`` and ``{digest}`` filled in for this write."""
    return lambda key, args, _record: template.replace("{key}", key).replace("{digest}", digest(args)).encode()


DEEP = "[" * 65 + "]" * 65  # one level past the strict parser's 64
CUT = '"pad": "' + "z" * 40000 + '"}'  # the reply is longer than the 32 KB that is read, so it arrives cut off

# Write replies that are not strict JSON and name another write before their first unreadable field.
NOT_STRICT_OTHER_WRITE = {
    "NaN after another key": text('{"idempotency_key": "other-{key}", "weight": NaN}'),
    "Infinity before another digest": text('{"weight": Infinity, "request_digest": "%s"}' % ("0" * 64)),
    "the key is NaN": text('{"idempotency_key": NaN, "request_digest": "{digest}"}'),
    "trailing data after another key": text('{"idempotency_key": "other-{key}"} written'),
    "deeper than 64 before another digest": text('{"trace": %s, "request_digest": "%s"}' % (DEEP, "0" * 64)),
    "cut off after another key": text('{"idempotency_key": "other-{key}", ' + CUT),
    "raw newline in a note before another key": text('{"note": "line 1\nline 2", "idempotency_key": "other-{key}"}'),
    "this key, then another digest, cut off": text('{"idempotency_key": "{key}", "request_digest": "%s", %s'
                                                   % ("0" * 64, CUT)),
}

# The same shapes naming nothing else, plus the limits: a field after an unreadable one, a nested field,
# and a body that is not an object are not read, so these count from the status alone.
NOT_STRICT_NAMES_NOTHING_ELSE = {
    "NaN, no identity": text('{"id": "SHIP-1", "weight": NaN}'),
    "NaN, this key and digest": text('{"idempotency_key": "{key}", "request_digest": "{digest}", "weight": NaN}'),
    "trailing data after this key": text('{"idempotency_key": "{key}"} written'),
    "deeper than 64, this digest": text('{"trace": %s, "request_digest": "{digest}"}' % DEEP),
    "cut off after this key": text('{"idempotency_key": "{key}", ' + CUT),
    "cut off before any identity": text('{' + CUT),
    "NaN, another key nested": text('{"data": {"idempotency_key": "other-{key}"}, "weight": NaN}'),
    "another key after an unreadable field": text('{"weight": undefined, "idempotency_key": "other-{key}"}'),
    "NaN array": text('[{"idempotency_key": "other-{key}"}, NaN]'),
    "raw newline in a note, this key": text('{"note": "line 1\nline 2", "idempotency_key": "{key}"}'),
}


# Write replies that carry more than one JSON value and name another write in an object after the first.
SEQUENCE_OTHER_WRITE = {
    "NDJSON, this record then another key": text('{"id": "SHIP-1", "idempotency_key": "{key}", "request_digest": '
                                                 '"{digest}"}\n{"id": "SHIP-2", "idempotency_key": "other-{key}"}\n'),
    "concatenated, nothing then another digest": text('{"status": "ok"}{"request_digest": "%s"}' % ("0" * 64)),
    "empty objects, then another key": text('{}{} {"idempotency_key": "other-{key}"}'),
    "a string and an array between, then another key": text('{"id": "SHIP-1"} "ok" [1] {"idempotency_key": "other-{key}"}'),
    "CRLF records, NaN before another key": text('{"id": "SHIP-1"}\r\n{"weight": NaN, "idempotency_key": "other-{key}"}'),
    "second record cut off after another key": text('{"id": "SHIP-1"}\n{"idempotency_key": "other-{key}", ' + CUT),
    "this key, then the key as null": text('{"idempotency_key": "{key}"}\n{"idempotency_key": null}'),
}

# The same sequences naming nothing else, plus the limits: a record after text that is not JSON, after a record that
# cannot be read to its end, after an RFC 7464 record separator, and a field nested in a later record are not read.
SEQUENCE_NAMES_NOTHING_ELSE = {
    "NDJSON, no identity": text('{"id": "SHIP-1"}\n{"status": "written"}\n'),
    "NDJSON, this key on every record": text('{"idempotency_key": "{key}", "step": 1}\n{"idempotency_key": "{key}", '
                                             '"step": 2}\n'),
    "concatenated, this digest": text('{"id": "SHIP-1"}{"request_digest": "{digest}"}'),
    "an array after the record": text('{"id": "SHIP-1"} [{"idempotency_key": "other-{key}"}]'),
    "another key after text that is not JSON": text('{"id": "SHIP-1"} written {"idempotency_key": "other-{key}"}'),
    "another key after an unreadable record": text('{"id": undefined}\n{"idempotency_key": "other-{key}"}'),
    "another key after a record separator": text('{"id": "SHIP-1"}\x1e{"idempotency_key": "other-{key}"}'),
    "another key nested in a later record": text('{"id": "SHIP-1"}\n{"data": {"idempotency_key": "other-{key}"}}'),
}


class WriteAckIdentity(Ship):
    """A completed write whose own body names another key or digest did not acknowledge this write."""

    def committed_with(self, ack, code=200):
        case, key = self.proposed()
        self.dest.write_code, self.dest.ack = code, ack
        return case, key, self.work.commit(case, key, "operator")

    def test_write_ack_naming_another_write_is_unknown(self):
        for index, (name, ack) in enumerate(OTHER_WRITE.items()):
            code = (200, 201)[index % 2]  # both completion statuses that carry a body
            with self.subTest(ack=name, code=code):
                self.fresh()
                case, key, result = self.committed_with(ack, code)
                self.assert_unknown(case, key, result, "destination_record_mismatch")
                self.assertEqual(self.dest.posts, 1)
                self.resolved_only_by_lookup(case, key)

    def test_ambiguous_write_ack_is_unknown(self):
        for name, ack in AMBIGUOUS.items():
            with self.subTest(ack=name):
                self.fresh()
                case, key, result = self.committed_with(ack)
                self.assert_unknown(case, key, result, "destination_record_ambiguous")
                self.resolved_only_by_lookup(case, key)

    def resolved_only_by_lookup(self, case, key):
        """The destination's own 200 record of this write resolves it; the write is not repeated."""
        self.assert_verified(case, key, self.work.reconcile(case, key, "manager", ["manager"]))
        self.assertEqual(self.work.metric("effects_settled_applied_total"), 1)
        self.assertEqual(self.dest.posts, 1)

    def test_destination_that_recorded_another_request_stays_unresolved(self):
        # A proxy rewrote the body: the destination wrote a request nobody approved and says so in both replies.
        self.dest.record_for = lambda key, args: committed(key, {**args, "order_id": "O-2"})
        case, key, result = self.committed_with(None)
        self.assert_unknown(case, key, result, "destination_record_mismatch")
        result = self.work.reconcile(case, key, "manager", ["manager"])
        self.assertEqual((result["status"], result["reason"]), ("unverified", "destination_record_mismatch"))
        self.assert_unresolved(case, key, lambda: self.propose(case))
        self.assertEqual(self.dest.posts, 1)

    def test_worker_settles_a_write_ack_naming_another_write_as_unknown(self):
        self.fresh(effects="worker")
        case, key = self.proposed()
        self.dest.ack = OTHER_WRITE["another key"]
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "queued")
        self.assert_unknown(case, key, run_once(self.work), "destination_record_mismatch")
        self.assertEqual(self.work.metric("queue_depth"), 0)

    def test_not_strict_write_ack_naming_another_write_is_unknown(self):
        for index, (name, ack) in enumerate(NOT_STRICT_OTHER_WRITE.items()):
            code = (200, 201)[index % 2]
            with self.subTest(ack=name, code=code):
                self.fresh()
                case, key, result = self.committed_with(ack, code)
                self.assert_unknown(case, key, result, "destination_record_mismatch")
                self.resolved_only_by_lookup(case, key)

    def test_not_strict_write_ack_naming_the_key_twice_is_ambiguous(self):
        # Last-wins reads this write's key; first-wins reads another's. The NaN keeps the strict parser from saying so.
        case, key, result = self.committed_with(
            text('{"idempotency_key": "other-{key}", "idempotency_key": "{key}", "weight": NaN}'))
        self.assert_unknown(case, key, result, "destination_record_ambiguous")
        self.resolved_only_by_lookup(case, key)

    def test_positive_control_not_strict_write_ack_naming_nothing_else_succeeds(self):
        for index, (name, ack) in enumerate(NOT_STRICT_NAMES_NOTHING_ELSE.items()):
            code = (200, 201)[index % 2]
            with self.subTest(ack=name, code=code):
                self.fresh()
                case, key, result = self.committed_with(ack, code)
                self.assertEqual(result["status"], "succeeded")
                self.assertTrue(self.work.inspect(case)["complete"])
                self.assertEqual(self.work.metric("effects_settled_applied_total"), 1)
                self.assertEqual(self.dest.posts, 1)

    def test_write_ack_sequence_naming_another_write_is_unknown(self):
        for index, (name, ack) in enumerate(SEQUENCE_OTHER_WRITE.items()):
            code = (200, 201)[index % 2]
            with self.subTest(ack=name, code=code):
                self.fresh()
                case, key, result = self.committed_with(ack, code)
                self.assert_unknown(case, key, result, "destination_record_mismatch")
                self.resolved_only_by_lookup(case, key)

    def test_write_ack_sequence_naming_the_key_twice_in_one_record_is_ambiguous(self):
        # The first record names nothing; the second reads as this write's key under last-wins and another's under
        # first-wins.
        case, key, result = self.committed_with(
            text('{"id": "SHIP-1"}\n{"idempotency_key": "other-{key}", "idempotency_key": "{key}"}'))
        self.assert_unknown(case, key, result, "destination_record_ambiguous")
        self.resolved_only_by_lookup(case, key)

    def test_worker_settles_a_write_ack_sequence_naming_another_write_as_unknown(self):
        self.fresh(effects="worker")
        case, key = self.proposed()
        self.dest.ack = SEQUENCE_OTHER_WRITE["NDJSON, this record then another key"]
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "queued")
        self.assert_unknown(case, key, run_once(self.work), "destination_record_mismatch")
        self.assertEqual(self.work.metric("queue_depth"), 0)

    def test_positive_control_write_ack_sequence_naming_nothing_else_succeeds(self):
        for index, (name, ack) in enumerate(SEQUENCE_NAMES_NOTHING_ELSE.items()):
            code = (200, 201)[index % 2]
            with self.subTest(ack=name, code=code):
                self.fresh()
                case, key, result = self.committed_with(ack, code)
                self.assertEqual(result["status"], "succeeded")
                self.assertTrue(self.work.inspect(case)["complete"])
                self.assertEqual(self.work.metric("effects_settled_applied_total"), 1)
                self.assertEqual(self.dest.posts, 1)

    def test_positive_control_write_ack_naming_nothing_else_succeeds(self):
        for index, (name, ack) in enumerate(NAMES_NOTHING_ELSE.items()):
            code = (200, 201)[index % 2]
            with self.subTest(ack=name, code=code):
                self.fresh()
                case, key, result = self.committed_with(ack, code)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["event"]["body"]["output"]["status"], code)
                self.assertTrue(self.work.inspect(case)["complete"])
                self.assertEqual(self.work.metric("effects_settled_applied_total"), 1)
                self.assertEqual(self.dest.posts, 1)
        # 204 carries no body, so it names nothing.
        self.fresh()
        self.assertEqual(self.committed_with(OTHER_WRITE["another key"], 204)[2]["status"], "succeeded")


class WriteAckIdentityWithoutLookup(WriteAckIdentity):
    """Without a status lookup, a write acknowledged as another write stays unresolved; nobody can declare it done."""

    status_lookup = False

    def resolved_only_by_lookup(self, case, key):
        with self.assertRaisesRegex(Rejected, "reconciliation_unavailable"):
            self.work.reconcile(case, key, "manager", ["manager"])
        self.assert_unresolved(case, key, lambda: self.propose(case))
        self.assertEqual((self.dest.posts, self.dest.lookups), (1, 0))

    def test_destination_that_recorded_another_request_stays_unresolved(self):
        self.dest.record_for = lambda key, args: committed(key, {**args, "order_id": "O-2"})
        case, key, result = self.committed_with(None)
        self.assert_unknown(case, key, result, "destination_record_mismatch")
        self.resolved_only_by_lookup(case, key)


if __name__ == "__main__":
    unittest.main()
