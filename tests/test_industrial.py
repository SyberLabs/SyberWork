"""Closures from the standards red team. The original executor tests stay untouched."""

import hashlib
import json
import math
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from syberlabs.admission import AdmissionContext, admit
from syberlabs.errors import Rejected
from syberlabs.events import DIGEST_FIELDS
from syberlabs.interop import at_microseconds, cloudevent, witness, witness_matches
from syberlabs.jcs import JcsError, envelope_jcs, jcs_bytes
from syberlabs.protocol import SIDE_PROTOCOL
from syberlabs.session import Session
from syberwork.core import Work


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
FIXTURE = ROOT / "conformance" / "fixtures"
VECTORS = json.loads((ROOT / "conformance" / "jcs_vectors.json").read_text(encoding="utf-8"))


def _construct(name: str):
    if name == "1.0":
        return 1.0
    if name == "-0.0":
        return -0.0
    if name == "math.pi":
        return math.pi
    if name == "emoji-then-private-use":
        return {"\U0001F600": 1, "\ue000": 2}
    if name == "nan":
        return float("nan")
    if name == "2**53":
        return 2**53
    raise AssertionError(name)


def _review_context(history):
    return AdmissionContext(
        contract={"actions": {"review": {}}, "resolutions": {}, "input_bindings": {}},
        policy={"actions": {"review": {"roles": ["operator"]}}},
        history=history,
        proposal={
            "id": "new",
            "action": "review",
            "args": {},
            "actor": "operator",
            "roles": ["operator"],
            "origin": "human",
        },
        now=1_000_000.0,
        installed_actions={"review"},
    )


class IndustrialAdmission(unittest.TestCase):
    def test_nonfinite_amount_is_amount_required(self):
        for amount in (float("nan"), float("inf"), float("-inf")):
            ctx = AdmissionContext(
                contract={"actions": {"review": {"max_amount": 10}}, "resolutions": {}, "input_bindings": {}},
                policy={"actions": {"review": {"roles": ["operator"], "max_amount": 10}}},
                history=[],
                proposal={
                    "id": "p",
                    "action": "review",
                    "args": {"amount": amount},
                    "actor": "operator",
                    "roles": ["operator"],
                    "origin": "human",
                },
                now=1.0,
                installed_actions={"review"},
            )
            self.assertEqual(admit(ctx)["reason"], "amount_required")

    def test_bool_amount_stays_amount_required(self):
        ctx = AdmissionContext(
            contract={"actions": {"review": {}}, "resolutions": {}, "input_bindings": {}},
            policy={"actions": {"review": {"roles": ["operator"], "max_amount": 10}}},
            history=[],
            proposal={
                "id": "p",
                "action": "review",
                "args": {"amount": True},
                "actor": "operator",
                "roles": ["operator"],
                "origin": "human",
            },
            now=1.0,
            installed_actions={"review"},
        )
        self.assertEqual(admit(ctx)["reason"], "amount_required")

    def test_inflight_effect_occupies_the_action_and_rejection_does_not(self):
        started = {"kind": "effect_started", "body": {"action": "review", "proposal_id": "old", "actor": "operator"}}
        self.assertEqual(admit(_review_context([started]))["reason"], "effect_unresolved:review")
        rejected = {"kind": "effect_rejected", "body": {"action": "review", "proposal_id": "old", "status": 409}}
        self.assertEqual(admit(_review_context([started, rejected]))["reason"], "all_checks_passed")

    def test_unknown_input_kind_is_rejected_at_publish_and_create(self):
        session = Session()
        doc = {
            "id": "shape",
            "version": 1,
            "inputs": {"note": "boolean"},
            "actions": {"note": {}},
            "acceptance": [{"id": "done", "kind": "effect", "action": "note"}],
        }
        with self.assertRaises(Rejected) as error:
            session.install_contract(doc)
        self.assertEqual(error.exception.code, "invalid_contract")
        self.assertIn("input note must be string or integer", error.exception.detail)
        session.install_contract({**doc, "inputs": {"count": "integer"}})
        session.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
        with self.assertRaises(Rejected) as bad:
            session.create_case("shape", 1, {"count": True}, "operator")
        self.assertEqual(bad.exception.code, "input_schema")


class IndustrialStore(unittest.TestCase):
    def _session(self):
        session = Session(clock=lambda: 1_700_000_000.0)
        session.install_contract({
            "id": "gate",
            "version": 1,
            "title": "Gate",
            "inputs": {"name": "string"},
            "actions": {
                "publish": {"approval_role": "maintainer"},
            },
            "acceptance": [
                {"id": "published", "kind": "effect", "action": "publish"},
                {"id": "signed", "kind": "signoff", "role": "maintainer", "after_action": "publish"},
            ],
        })
        session.install_policy({"version": 1, "actions": {"publish": {"roles": ["engineer"], "approval_role": "maintainer"}}})
        session.install_action("publish", {"kind": "local"})
        return session

    def test_side_channel_keeps_rule_and_jcs_out_of_the_hash(self):
        session = self._session()
        case_id = session.create_case("gate", 1, {"name": "a"}, "engineer")
        proposed = session.propose(case_id, "publish", {}, "engineer", ["engineer"])
        self.assertEqual(set(proposed["decision"]), {"status", "reason"})
        events = session.inspect(case_id)["events"]
        decision = next(event for event in events if event["kind"] == "decision")
        self.assertNotIn("rule", decision["body"])
        self.assertEqual(set(decision), {"case_id", "seq", "kind", "body", "at", "previous", "hash"})
        side = session.side_channel(case_id)
        row = next(item for item in side if item["seq"] == decision["seq"])
        self.assertEqual(row["rule"], "approval.required")
        self.assertEqual(row["jcs"], envelope_jcs(decision))
        self.assertEqual(len(row["jcs"]), 64)
        self.assertTrue(session.verify_chain(case_id))
        self.assertEqual(SIDE_PROTOCOL, "sdk.syberlabs.space/v0alpha1+jcs1+us")

    def test_signoff_requires_a_different_actor_only_after_the_effect(self):
        session = self._session()
        case_id = session.create_case("gate", 1, {"name": "a"}, "engineer")
        early = session.signoff(case_id, "engineer", ["engineer", "maintainer"], "maintainer")
        self.assertEqual(early["kind"], "signed")
        proposed = session.propose(case_id, "publish", {}, "engineer", ["engineer"])
        session.approve(case_id, proposed["proposal"]["id"], "maintainer", ["maintainer"])
        committed = session.commit(case_id, proposed["proposal"]["id"], "engineer")
        self.assertEqual(committed["status"], "succeeded")
        with self.assertRaises(Rejected) as same:
            session.signoff(case_id, "engineer", ["engineer", "maintainer"], "maintainer")
        self.assertEqual(same.exception.code, "signoff_denied")
        self.assertIn("effect actor", same.exception.detail)
        session.signoff(case_id, "maintainer", ["maintainer"], "maintainer")
        self.assertTrue(session.inspect(case_id)["complete"])

    def test_work_reuses_one_connection_and_redacts_destination_errors(self):
        class Boom(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(500)
                self.end_headers()

            def log_message(self, *_args):
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Boom)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "work.sqlite3"
            work = Work(path)
            first = work._db
            work.install_contract({
                "id": "ping",
                "version": 1,
                "inputs": {"name": "string"},
                "actions": {"ping": {}},
                "acceptance": [{"id": "sent", "kind": "effect", "action": "ping"}],
            })
            work.install_policy({"version": 1, "actions": {"ping": {"roles": ["operator"]}}})
            work.install_action("ping", {"kind": "http", "method": "POST", "url": "http://127.0.0.1:1/ping"})
            self.assertIs(work._db, first)
            case_id = work.create_case("ping", 1, {"name": "a"}, "operator")
            proposed = work.propose(case_id, "ping", {}, "operator", ["operator"])
            result = work.commit(case_id, proposed["proposal"]["id"], "operator")
            self.assertEqual(result["status"], "unknown")
            unknown = next(event for event in work.inspect(case_id)["events"] if event["kind"] == "effect_unknown")
            self.assertEqual(unknown["body"]["error"], "destination_unreachable")
            self.assertNotIn("127.0.0.1", unknown["body"]["error"])
            self.assertTrue(work.verify_chain(case_id))
            side = work.side_channel(case_id)
            self.assertEqual(len(side), len(work.inspect(case_id)["events"]))
            self.assertIs(work._db, first)

            work.install_action("fail", {"kind": "http", "method": "POST", "url": f"http://127.0.0.1:{port}/fail"})
            # A second action needs a contract that lists it. Use a new version.
            work.install_contract({
                "id": "ping",
                "version": 2,
                "inputs": {"name": "string"},
                "actions": {"fail": {}},
                "acceptance": [{"id": "sent", "kind": "effect", "action": "fail"}],
            })
            work.install_policy({"version": 2, "actions": {"fail": {"roles": ["operator"]}}})
            http_case = work.create_case("ping", 2, {"name": "b"}, "operator")
            http_proposal = work.propose(http_case, "fail", {}, "operator", ["operator"])
            http_result = work.commit(http_case, http_proposal["proposal"]["id"], "operator")
            self.assertEqual(http_result["status"], "unknown")
            http_unknown = next(event for event in work.inspect(http_case)["events"] if event["kind"] == "effect_unknown")
            self.assertEqual(http_unknown["body"]["error"], "destination_http")
            self.assertNotIn(str(port), http_unknown["body"]["error"])

            with work.tx() as db:
                db.execute("UPDATE event_side SET jcs=? WHERE case_id=? AND seq=1", ("0" * 64, case_id))
            self.assertFalse(work.verify_chain(case_id))

            work.install_source("down", {"kind": "http", "url": "http://127.0.0.1:1/r/{key}"})
            with self.assertRaises(Rejected) as source_error:
                work.refresh_fact(case_id, "down", "part", "P-1", "operator", ["operator"])
            self.assertEqual(source_error.exception.code, "source_unavailable")
            self.assertEqual(source_error.exception.detail, "destination_unreachable")
            work.close()

    def test_history_from_main_has_no_side_rows_and_still_verifies(self):
        meta = json.loads((FIXTURE / "main_history.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "work.sqlite"
            shutil.copy(FIXTURE / "main_history.sqlite", copy)
            work = Work(copy)
            self.assertTrue(work.verify_chain(meta["case_id"]))
            self.assertEqual(work.side_channel(meta["case_id"]), [])
            work.close()


class IndustrialInterop(unittest.TestCase):
    def test_jcs_vectors_match_published_bytes(self):
        for vector in VECTORS:
            if "error" in vector:
                with self.assertRaises(JcsError) as error:
                    jcs_bytes(_construct(vector["construct"]))
                self.assertIn(vector["error"], str(error.exception))
                continue
            value = vector["document"] if "document" in vector else _construct(vector["construct"])
            raw = jcs_bytes(value)
            self.assertEqual(raw.decode("utf-8"), vector["jcs"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), vector["sha256"])

    def test_cloudevent_and_witness_are_outside_the_chain(self):
        session = Session(clock=lambda: 1_700_000_000.25)
        session.install_contract({
            "id": "note",
            "version": 1,
            "inputs": {"name": "string"},
            "actions": {"note": {}},
            "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
        })
        session.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
        session.install_action("note", {"kind": "local"})
        case_id = session.create_case("note", 1, {"name": "a"}, "operator")
        events = session.inspect(case_id)["events"]
        event = events[0]
        exported = cloudevent(event)
        self.assertEqual(exported["specversion"], "1.0")
        self.assertEqual(exported["source"], "https://sdk.syberlabs.space/v0alpha1")
        self.assertEqual(exported["type"], "space.syberlabs.sdk.case.case_created")
        self.assertEqual(exported["id"], f"{case_id}:1")
        self.assertEqual(set(DIGEST_FIELDS), set(exported["data"]))
        self.assertEqual(exported["atmicroseconds"], at_microseconds(event["at"]))
        self.assertEqual(at_microseconds(1.5), 1_500_000)
        key = b"caller-supplied-key"
        signed = witness(case_id, events, key)
        self.assertTrue(witness_matches(signed, events, key))
        self.assertFalse(witness_matches(signed, events, b"other-key"))
        self.assertNotIn(signed["mac"], json.dumps({key: event[key] for key in DIGEST_FIELDS}))
        self.assertTrue(session.verify_chain(case_id))


if __name__ == "__main__":
    unittest.main()
