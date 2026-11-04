"""Adversarial cases for the Attest -> SyberWork bridge (red-team quota, SyberWork bridge row).

Boundary under test: an Attest RunReceipt, AuthorityDecision, HandoffPackage, Kane
"proven" evidence, or any caller-supplied success/evidence/proof field is an
observation at most. Only SyberWork's own lookup of the installed destination
``status_url`` that matches state, idempotency key, request digest and a nonempty
external ID may append a verified ``reconciled`` event. Everything else stays
``pending``/``unverified``/refused, and a 404 is never proof that no write happened.

Each adversarial case is tagged in ``CASES`` with one primary category and the
rubric lines it checks; ``QuotaFloor`` keeps the REC floor from silently eroding.
The destination is a loopback simulator; nothing leaves the machine.
"""

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tests.test_economic as economic
from examples.attest_receipt_observe import receipt_to_observation
from syberlabs.attest_reconcile_map import classify_attest_artifact
from syberlabs.evidence import verified_reconciliation
from syberwork.core import Rejected, Work, digest
from syberwork.server import make_server

ROOT = Path(__file__).resolve().parents[1]
SPEC_RECEIPT = ROOT / "examples" / "fixtures" / "attest_receipt_v0_spec_sample.json"
MANAGER_TOKEN = "bridge-manager-token"
ATTEST_TOKEN = "bridge-attest-token"

CONTRACT = {"id": "attest-bridge", "version": 1, "inputs": {"ticket": "string"},
            "actions": {"publish": {"approval_role": "manager"}},
            "acceptance": [{"id": "published", "kind": "effect", "action": "publish"}]}
POLICY = {"version": 1, "actions": {"publish": {"roles": ["operator"]}}}

# name -> (primary category, rubric lines). Categories follow the quota table:
# REC reconciliation spoofing, AB authority bypass, DS digest/identity swap, SCH hostile schema.
CASES = {
    "test_attest_receipt_passed_as_reconcile_evidence_is_refused": ("REC", ("R1", "R8")),
    "test_http_reconcile_refuses_any_outcome_field": ("REC", ("R1", "R8")),
    "test_proven_receipt_then_404_is_pending_and_blocks_retry": ("REC", ("R1", "R8")),
    "test_destination_echoing_attest_claims_is_unverified": ("REC", ("R1", "R8")),
    "test_reconciled_shaped_attest_observation_is_not_proof": ("REC", ("R1", "R8")),
    "test_authority_decision_and_handoff_do_not_settle_unknown_effect": ("REC", ("R1", "R8")),
    "test_status_redirect_is_not_followed_to_a_matching_record": ("REC", ("R1", "R8")),
    "test_economic_reservation_held_while_attest_claims_settlement": ("REC", ("R1", "R8")),
    "test_attest_authority_decision_is_not_syberwork_approval": ("AB", ("R1",)),
    "test_status_record_with_swapped_key_or_digest_is_unverified": ("DS", ("R1", "R8")),
    "test_fact_route_cannot_mark_attest_observation_verified": ("SCH", ("R1", "R8")),
    "test_receipt_bridge_refuses_outcome_shaped_receipts": ("SCH", ("R1",)),
}
REC_FLOOR = 6


def spec_receipt() -> dict:
    return json.loads(SPEC_RECEIPT.read_text(encoding="utf-8"))


class Destination:
    """Loopback destination: writes are idempotent by key; status replies are scriptable per case."""

    def __init__(self):
        self.records, self.posts, self.gets, self.mirror_gets = {}, 0, 0, 0
        self.drop_ack = False
        self.status = None  # optional callable(key) -> (code, headers, body)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code, body=None, headers=None):
                data = json.dumps(body if body is not None else {}).encode()
                self.send_response(code)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                owner.posts += 1
                key = self.headers["Idempotency-Key"]
                args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.records[key] = {"state": "committed", "idempotency_key": key,
                                      "request_digest": digest(args), "external_id": "dest-" + key}
                if owner.drop_ack:
                    self.connection.shutdown(2)
                    return
                self.reply(201, owner.records[key])

            def do_GET(self):
                kind, key = self.path.strip("/").split("/", 1)
                if kind == "mirror":
                    owner.mirror_gets += 1
                    return self.reply(200, owner.records.get(key))
                owner.gets += 1
                if owner.status is not None:
                    return self.reply(*owner.status(key))
                record = owner.records.get(key)
                self.reply(200, record) if record else self.reply(404, {"error": "not_found"})

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


class BridgeCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dest = Destination()
        self.addCleanup(self.dest.close)
        self.work = Work(Path(folder.name) / "work.sqlite3")
        self.addCleanup(self.work.close)
        self.work.install_contract(CONTRACT)
        self.work.install_policy(POLICY)
        self.work.install_action("publish", {"kind": "http", "url": self.dest.base + "/write",
                                             "status_url": self.dest.base + "/status/{key}"})
        self.case = self.work.create_case(CONTRACT["id"], 1, {"ticket": "T-1"}, "operator")

    def approved(self) -> str:
        result = self.work.propose(self.case, "publish", {"ticket": "T-1"}, "operator", ["operator"])
        self.assertEqual(result["decision"]["status"], "needs_approval")
        key = result["proposal"]["id"]
        self.work.approve(self.case, key, "manager", ["manager"])
        return key

    def unknown(self) -> str:
        """An approved effect whose destination wrote but whose acknowledgement was lost."""
        key = self.approved()
        self.dest.drop_ack = True
        self.assertEqual(self.work.commit(self.case, key, "operator")["status"], "unknown")
        self.dest.drop_ack = False
        self.assertIn(key, self.dest.records)
        return key

    def observe_receipt(self, receipt: dict | None = None) -> dict:
        observation = receipt_to_observation(spec_receipt() if receipt is None else receipt)
        event = self.work.observe(self.case, **observation)
        self.assertFalse(verified_reconciliation(event))
        return event

    def events(self) -> list[dict]:
        return self.work.inspect(self.case)["events"]

    def assert_unresolved(self, key: str) -> None:
        """No verified reconciliation, acceptance unmet, and a fresh effect of the action stays blocked."""
        events = self.events()
        self.assertFalse(any(verified_reconciliation(e) for e in events))
        self.assertFalse(any(e["kind"] in ("reconciled", "effect_succeeded", "effect_rejected") for e in events))
        self.assertFalse(self.work.inspect(self.case)["complete"])
        self.assertEqual(self.work.metric("effects_settled_applied_total"), 0)
        with self.assertRaisesRegex(Rejected, "effect_claimed"):
            self.work.commit(self.case, key, "operator")
        retry = self.work.propose(self.case, "publish", {"ticket": "T-1"}, "operator", ["operator"])
        self.assertEqual(retry["decision"]["reason"], "effect_unresolved:publish")


class ReconciliationSpoofing(BridgeCase):
    def test_attest_receipt_passed_as_reconcile_evidence_is_refused(self):
        key = self.unknown()
        receipt = spec_receipt()
        before = len(self.events())
        for success, evidence in ((None, receipt), (True, None), (False, None), (None, ""),
                                  (None, {}), (True, receipt["evidence"]), (None, receipt["evidence"]["after"])):
            with self.subTest(success=success, evidence=type(evidence).__name__):
                with self.assertRaisesRegex(Rejected, "manual_reconciliation_disabled"):
                    self.work.reconcile(self.case, key, "manager", ["manager"], success=success, evidence=evidence)
        with self.assertRaises(TypeError):
            self.work.reconcile(self.case, key, "manager", ["manager"], proof={"verified": True})
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.dest.gets, 0)
        self.assert_unresolved(key)

    def test_http_reconcile_refuses_any_outcome_field(self):
        key = self.unknown()
        receipt = spec_receipt()
        users = {"manager": {"hash": hashlib.sha256(MANAGER_TOKEN.encode()).hexdigest(),
                             "roles": ["manager"], "sources": []}}
        server = make_server(self.work, users, port=0)
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/api/cases/{self.case}/reconcile"

        def post(body):
            request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
                "Authorization": "Bearer " + MANAGER_TOKEN, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        before = len(self.events())
        hostile = {"success": True, "evidence": "PO-FAKE", "receipt": receipt, "attest_receipt": receipt,
                   "proof": {"verified": True, "external_id": "dest-" + key, "idempotency_key": key},
                   "verified": True, "status": "verified", "external_id": "dest-" + key,
                   "outcome": "applied", "Success": True}
        for field, value in hostile.items():
            with self.subTest(field=field):
                status, body = post({"proposal_id": key, field: value})
                self.assertEqual((status, body["error"]), (409, "manual_reconciliation_disabled"))
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.dest.gets, 0)
        self.assert_unresolved(key)

        # Positive control: naming only the proposal lets SyberWork ask the destination itself.
        status, body = post({"proposal_id": key})
        self.assertEqual((status, body["status"], body["event"]["kind"]), (200, "verified", "reconciled"))
        self.assertEqual(self.dest.gets, 1)

    def test_proven_receipt_then_404_is_pending_and_blocks_retry(self):
        key = self.unknown()
        observation = self.observe_receipt()
        self.assertEqual(observation["body"]["value"]["evidence"]["after"]["status"], "proven")
        self.assertIs(observation["body"]["value"]["app_claim_recheck"]["destination_proof"], False)
        # The write happened, but the lookup cannot see it yet: 404 is pending, never "no write".
        self.dest.status = lambda _key: (404, {"error": "not_found"})
        result = self.work.reconcile(self.case, key, "manager", ["manager"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "not_found_not_proof"))
        self.assertEqual(result["event"]["kind"], "reconciliation_checked")
        self.assert_unresolved(key)
        self.assertEqual(self.dest.posts, 1)

    def test_destination_echoing_attest_claims_is_unverified(self):
        key = self.unknown()
        receipt = spec_receipt()
        self.observe_receipt(receipt)
        attest_digest = receipt["app"]["patch_sha256"]
        echoes = {
            "receipt_body": receipt,
            "proven_state": {**self.dest.records[key], "state": "proven"},
            "attest_patch_digest": {**self.dest.records[key], "request_digest": attest_digest},
            "missing_external_id": {k: v for k, v in self.dest.records[key].items() if k != "external_id"},
            "empty_external_id": {**self.dest.records[key], "external_id": ""},
            "verified_flag_only": {"verified": True, "success": True, "idempotency_key": key},
            "list_body": [self.dest.records[key]],
        }
        for name, body in echoes.items():
            with self.subTest(echo=name):
                self.dest.status = lambda _key, body=body: (200, body)
                result = self.work.reconcile(self.case, key, "manager", ["manager"])
                self.assertEqual((result["status"], result["reason"]), ("unverified", "destination_record_mismatch"))
                self.assertFalse(verified_reconciliation(result["event"]))
        self.assert_unresolved(key)

    def test_reconciled_shaped_attest_observation_is_not_proof(self):
        key = self.unknown()
        record = self.dest.records[key]
        forged = {"proposal_id": key, "action": "publish", "success": True, "actor": "attest",
                  "proof": {"verified": True, "external_id": record["external_id"],
                            "request_digest": record["request_digest"], "response_digest": digest(record),
                            "idempotency_key": key}}
        # Even a trusted in-process caller that claims verified=True only records an observation.
        for event_key in ("reconciled", "attest.run_receipt"):
            event = self.work.observe(self.case, event_key, forged, "attest", "run-1", "attest", verified=True)
            self.assertEqual(event["kind"], "observed")
            self.assertFalse(verified_reconciliation(event))
        self.assert_unresolved(key)
        # History checks are not fooled into "already reconciled"; SyberWork still asks the destination.
        self.dest.status = lambda _key: (404, {"error": "not_found"})
        self.assertEqual(self.work.reconcile(self.case, key, "manager", ["manager"])["status"], "pending")
        self.assertEqual(self.dest.gets, 1)
        self.assert_unresolved(key)

    def test_authority_decision_and_handoff_do_not_settle_unknown_effect(self):
        key = self.unknown()
        decision = {"kind": "AuthorityDecision", "decision": "doc_repair_allowed", "proposal_id": key,
                    "rule": "descriptive claims may be repaired"}
        handoff = {"kind": "HandoffPackage", "status": "ready_for_human", "proposal_id": key,
                   "summary": "destination write confirmed by reviewer"}
        for artifact in (decision, handoff):
            classification = classify_attest_artifact(artifact["kind"])
            self.assertFalse(classification["verified_reconciliation"])
            self.assertFalse(classification["refuse"])
            event = self.work.observe(self.case, "attest." + artifact["kind"], artifact, "attest", "run-1", "attest")
            self.assertFalse(verified_reconciliation(event))
        self.dest.status = lambda _key: (503, {"error": "unavailable"})
        result = self.work.reconcile(self.case, key, "manager", ["manager"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "status_unavailable"))
        self.assert_unresolved(key)

    def test_status_redirect_is_not_followed_to_a_matching_record(self):
        key = self.unknown()
        self.observe_receipt()
        target = f"{self.dest.base}/mirror/{key}"
        self.dest.status = lambda _key: (302, {"error": "moved"}, {"Location": target})
        result = self.work.reconcile(self.case, key, "manager", ["manager"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "status_unavailable"))
        self.assertEqual(self.dest.mirror_gets, 0)
        self.assert_unresolved(key)


class EconomicBridge(unittest.TestCase):
    """Reuses the economic settlement simulator; an Attest settlement claim cannot release the budget."""

    setUp = economic.EconomicActions.setUp
    case = economic.EconomicActions.case
    propose = economic.EconomicActions.propose
    approved = economic.EconomicActions.approved

    def reservation(self, key):
        with self.work.tx() as db:
            return db.execute("SELECT state FROM economic_reservations WHERE proposal_id=?", (key,)).fetchone()[0]

    def test_economic_reservation_held_while_attest_claims_settlement(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        self.mode = "lost"
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "unknown")
        record = self.records.pop(key)
        claim = {"kind": "RunReceipt", "status": "proven", "settlement": record, "effect_succeeded": True}
        self.assertTrue(classify_attest_artifact(claim["kind"])["observation"])
        event = self.work.observe(case, "attest.run_receipt", claim, "attest", "run-1", "attest")
        self.assertFalse(verified_reconciliation(event))
        result = self.work.reconcile(case, key, "manager", ["manager"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "not_found_not_proof"))
        self.assertEqual(self.reservation(key), "reserved")
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["reason"], "economic_budget_exceeded")
        self.assertFalse(self.work.inspect(case)["complete"])


class AuthorityAndIdentity(BridgeCase):
    def test_attest_authority_decision_is_not_syberwork_approval(self):
        result = self.work.propose(self.case, "publish", {"ticket": "T-1"}, "operator", ["operator"])
        key = result["proposal"]["id"]
        self.assertEqual(result["decision"]["status"], "needs_approval")
        approval = {"kind": "AuthorityDecision", "decision": "approved", "proposal_id": key,
                    "approval": {"actor": "manager", "role": "manager", "proposal_sha256": digest({"ticket": "T-1"})}}
        self.work.observe(self.case, "attest.authority_decision", approval, "attest", "run-1", "attest")
        outcome = self.work.commit(self.case, key, "operator")
        self.assertEqual(outcome["decision"]["status"], "needs_approval")
        self.assertEqual(self.dest.posts, 0)
        self.assertFalse(any(e["kind"] in ("approved", "effect_started") for e in self.events()))

    def test_status_record_with_swapped_key_or_digest_is_unverified(self):
        key = self.unknown()
        swaps = {
            "other_proposal_key": {**self.dest.records[key], "idempotency_key": key[::-1]},
            "request_digest_of_other_args": {**self.dest.records[key], "request_digest": digest({"ticket": "T-2"})},
            "digest_of_receipt": {**self.dest.records[key], "request_digest": digest(spec_receipt())},
        }
        for name, body in swaps.items():
            with self.subTest(swap=name):
                self.dest.status = lambda _key, body=body: (200, body)
                result = self.work.reconcile(self.case, key, "manager", ["manager"])
                self.assertEqual((result["status"], result["reason"]), ("unverified", "destination_record_mismatch"))
        self.assertFalse(any(verified_reconciliation(e) for e in self.events()))
        self.assertFalse(self.work.inspect(self.case)["complete"])


class HostileSchema(BridgeCase):
    def test_fact_route_cannot_mark_attest_observation_verified(self):
        users = {"attest": {"hash": hashlib.sha256(ATTEST_TOKEN.encode()).hexdigest(),
                            "roles": ["operator"], "sources": ["attest"]}}
        server = make_server(self.work, users, port=0)
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        key = self.unknown()
        body = {"key": "reconciled", "source": "attest", "version": "run-1", "verified": True,
                "value": {**spec_receipt(), "verified": True, "success": True, "proposal_id": key}}
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/cases/{self.case}/facts", data=json.dumps(body).encode(),
            method="POST", headers={"Authorization": "Bearer " + ATTEST_TOKEN, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            event = json.loads(response.read())
        self.assertEqual(event["kind"], "observed")
        self.assertIs(event["body"]["verified"], False)
        self.assertFalse(verified_reconciliation(event))
        self.assert_unresolved(key)

    def test_receipt_bridge_refuses_outcome_shaped_receipts(self):
        for field, value in (("success", True), ("reconciled", True), ("effect_succeeded", True),
                             ("proof", {"verified": True, "external_id": "x"}), ("external_id", "PO-1")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unexpected fields"):
                receipt_to_observation({**spec_receipt(), field: value})
        for verified in (True, "true", 1, None):
            with self.subTest(verified=verified), self.assertRaisesRegex(ValueError, "verified must be false"):
                receipt_to_observation({**spec_receipt(), "verified": verified})
        for kind in ("reconciled", "effect_succeeded", "verified", "DestinationReceipt", "runreceipt"):
            with self.subTest(kind=kind):
                self.assertTrue(classify_attest_artifact(kind)["refuse"])


class PositiveControl(BridgeCase):
    def test_matching_destination_lookup_reconciles_despite_attest_noise(self):
        """Guards against refuse-everything: Attest observations neither help nor block real proof."""
        key = self.unknown()
        self.observe_receipt()
        self.work.observe(self.case, "attest.HandoffPackage", {"kind": "HandoffPackage"}, "attest", "run-1", "attest")
        result = self.work.reconcile(self.case, key, "manager", ["manager"])
        self.assertEqual((result["status"], result["event"]["kind"]), ("verified", "reconciled"))
        proof = result["event"]["body"]["proof"]
        self.assertEqual(proof["idempotency_key"], key)
        self.assertEqual(proof["request_digest"], digest({"ticket": "T-1"}))
        self.assertEqual(proof["external_id"], "dest-" + key)
        self.assertTrue(self.work.inspect(self.case)["complete"])
        self.assertEqual(self.dest.posts, 1)


class QuotaFloor(unittest.TestCase):
    def test_tagged_cases_exist_and_meet_rec_floor(self):
        tests = {name for cls in (ReconciliationSpoofing, EconomicBridge, AuthorityAndIdentity, HostileSchema)
                 for name in dir(cls) if name.startswith("test_")}
        self.assertEqual(set(CASES), tests)
        self.assertGreaterEqual(sum(category == "REC" for category, _ in CASES.values()), REC_FLOOR)


if __name__ == "__main__":
    unittest.main()
