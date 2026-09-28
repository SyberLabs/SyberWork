"""One conformance run across the in-memory and the durable session, plus journal failure modes."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syberlabs import Rejected, Session
from syberlabs import journal as journal_module
from syberlabs.journal import Journal
from syberlabs.session import NoWrite

ROOT = Path(__file__).resolve().parents[1]


def example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_gate(session):
    """The release-gate flow from examples/release_gate.py on a given session."""
    gate = example("release_gate")
    session.install_contract(gate.CONTRACT)
    session.install_policy(gate.POLICY)
    session.install_action("record_checks", {"kind": "local"})
    session.install_action("publish", {"kind": "local"})
    case = session.create_case("release-gate", 1, {"version": "1.4.0"}, "engineer")
    session.observe(case, "checks", {"version": "1.4.0", "passed": True}, "ci", "build-90", "engineer", verified=True)
    review = session.propose(case, "record_checks", {"version": "1.4.0"}, "engineer", ["engineer"])
    session.commit(case, review["proposal"]["id"], "engineer")
    publish = session.propose(case, "publish", {"version": "1.4.0"}, "engineer", ["engineer"])
    session.approve(case, publish["proposal"]["id"], "maintainer", ["maintainer"])
    session.commit(case, publish["proposal"]["id"], "engineer")
    session.signoff(case, "maintainer", ["maintainer"], "maintainer")
    return case


def shape(session, case):
    inspected = session.inspect(case)
    return {"kinds": [e["kind"] for e in inspected["events"]],
            "decisions": [(e["body"]["status"], e["body"]["reason"]) for e in inspected["events"] if e["kind"] == "decision"],
            "status": inspected["status"], "acceptance": inspected["acceptance"], "chain": inspected["chain_valid"]}


class Durable(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "journal"

    def durable(self):
        journal = Journal(self.home)
        self.addCleanup(journal.close)
        return Session(journal=journal)

    def test_same_conformance_run_on_both_stores(self):
        memory = Session()
        durable = self.durable()
        expected = shape(memory, release_gate(memory))
        case = release_gate(durable)
        self.assertEqual(shape(durable, case), expected)
        self.assertEqual(expected["status"], "complete")
        reopened = self.durable()
        self.assertEqual(shape(reopened, case), expected)
        self.assertTrue(reopened.verify_chain(case))
        self.assertEqual(reopened.side_channel(case), durable.side_channel(case))
        self.assertEqual(reopened.replay(case, 1, 1)["changed_decisions"], [])

    def test_access_review_example_runs_on_the_durable_store(self):
        review = example("access_review")
        durable = self.durable()
        with patch.object(review, "Session", lambda: durable):
            done = review.complete_review(review.open_session())
        self.assertEqual((done["status"], done["chain_valid"]), ("complete", True))
        reopened = self.durable()
        self.assertTrue(reopened.verify_chain(done["case_id"]))
        self.assertEqual(reopened.inspect(done["case_id"])["status"], "complete")

    def test_torn_final_record_is_dropped(self):
        durable = self.durable()
        case = release_gate(durable)
        path = self.home / "threads" / f"{case}.jsonl"
        whole = path.read_bytes()
        path.write_bytes(whole + b'{"t":"event","event":{"case_id"')
        reopened = self.durable()
        self.assertEqual(shape(reopened, case), shape(durable, case))
        self.assertEqual(path.read_bytes(), whole)

    def test_corruption_and_tampering_refuse_to_load(self):
        durable = self.durable()
        case = release_gate(durable)
        path = self.home / "threads" / f"{case}.jsonl"
        lines = path.read_text().splitlines()
        garbled = lines[:3] + ["{not json"] + lines[4:]
        path.write_text("\n".join(garbled) + "\n")
        with self.assertRaises(Rejected) as caught:
            self.durable().inspect(case)
        self.assertEqual(caught.exception.code, "journal_corrupt")
        record = json.loads(lines[3])
        record["event"]["body"]["actor"] = "someone-else"
        path.write_text("\n".join(lines[:3] + [json.dumps(record)] + lines[4:]) + "\n")
        with self.assertRaises(Rejected) as caught:
            self.durable().inspect(case)
        self.assertEqual(caught.exception.code, "journal_corrupt")

    def test_registry_contract_that_no_longer_validates(self):
        durable = self.durable()
        release_gate(durable)
        registry = self.home / "registry.jsonl"
        lines = registry.read_text().splitlines()
        record = json.loads(lines[0])
        record["doc"]["acceptance"].append({"id": "published", "kind": "effect", "action": "publish"})
        registry.write_text("\n".join([json.dumps(record)] + lines[1:]) + "\n")
        with self.assertRaises(Rejected) as caught:
            self.durable()
        self.assertEqual(caught.exception.code, "journal_corrupt")

    def test_two_processes_see_each_others_appends(self):
        first = self.durable()
        case = release_gate(first)
        second = self.durable()
        second.observe(case, "note", "from the second process", "ops", "v1", "engineer")
        first.observe(case, "note", "from the first process", "ops", "v2", "engineer")
        self.assertEqual(shape(first, case), shape(self.durable(), case))
        self.assertTrue(self.durable().verify_chain(case))
        self.assertEqual([e["body"]["value"] for e in first.inspect(case)["events"] if e["body"].get("key") == "note"],
                         ["from the second process", "from the first process"])

    def test_threads_are_bounded(self):
        durable = self.durable()
        with patch.object(journal_module, "MAX_EVENTS", 3):
            durable.install_contract(example("release_gate").CONTRACT)
            case = durable.create_case("release-gate", 1, {"version": "1"}, "engineer")
            durable.observe(case, "a", 1, "s", "v", "x")
            durable.observe(case, "b", 1, "s", "v", "x")
            with self.assertRaises(Rejected) as caught:
                durable.observe(case, "c", 1, "s", "v", "x")
        self.assertEqual(caught.exception.code, "thread_full")
        self.assertEqual(len(self.durable().inspect(case)["events"]), 3)


class Executors(unittest.TestCase):
    def setUp(self):
        gate = example("release_gate")
        self.session = Session()
        self.session.install_contract(gate.CONTRACT)
        self.session.install_policy(gate.POLICY)
        self.session.install_action("record_checks", {"kind": "local", "effect": "ci_record"})
        self.session.install_action("publish", {"kind": "local"})
        self.case = self.session.create_case("release-gate", 1, {"version": "1.4.0"}, "engineer")
        self.session.observe(self.case, "checks", {"version": "1.4.0"}, "ci", "b1", "engineer", verified=True)

    def proposal(self):
        return self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer", ["engineer"])["proposal"]["id"]

    def test_named_effect_needs_its_executor(self):
        pid = self.proposal()
        with self.assertRaises(Rejected) as caught:
            self.session.commit(self.case, pid, "engineer")
        self.assertEqual(caught.exception.code, "effect_unavailable")
        self.assertFalse(any(e["kind"] == "effect_started" for e in self.session.inspect(self.case)["events"]))
        with self.assertRaises(Rejected):
            self.session.bind_effect("publish", object())

    def test_no_write_and_unknown_outcomes(self):
        class Refuses:
            def apply(self, case_id, args, key):
                raise NoWrite(412, "precondition")

            def status(self, case_id, args, key):
                return "not_applied", {}

        class Fails:
            state = "unknown"

            def apply(self, case_id, args, key):
                raise OSError("disk")

            def status(self, case_id, args, key):
                return self.state, {"external_id": "x1"}

        self.session.bind_effect("record_checks", Refuses())
        self.assertEqual(self.session.commit(self.case, self.proposal(), "engineer")["status"], "rejected")
        failing = Fails()
        self.session.bind_effect("record_checks", failing)
        pid = self.proposal()
        self.assertEqual(self.session.commit(self.case, pid, "engineer")["status"], "unknown")
        self.assertEqual(self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer",
                                              ["engineer"])["decision"]["reason"], "effect_unresolved:record_checks")
        with self.assertRaises(Rejected):
            self.session.reconcile(self.case, pid, "guest", ["guest"])
        self.assertEqual(self.session.reconcile(self.case, pid, "engineer", ["engineer"])["status"], "pending")
        failing.state = "applied"
        self.assertEqual(self.session.reconcile(self.case, pid, "engineer", ["engineer"])["status"], "verified")
        with self.assertRaises(Rejected):
            self.session.reconcile(self.case, pid, "engineer", ["engineer"])
        self.assertEqual(self.session.inspect(self.case)["acceptance"][0], {"id": "published", "passed": False})

    def test_unknown_then_absent_frees_the_action_after_the_settle_window(self):
        class Remote:
            settle_seconds = 60
            state = "absent"

            def apply(self, case_id, args, key):
                raise OSError("connection reset")

            def status(self, case_id, args, key):
                return self.state, {}

        clock = [__import__("time").time()]
        self.session.clock = lambda: clock[0]
        self.session.bind_effect("record_checks", Remote())
        pid = self.proposal()
        self.assertEqual(self.session.commit(self.case, pid, "engineer")["status"], "unknown")
        found = self.session.reconcile(self.case, pid, "engineer", ["engineer"])
        self.assertEqual((found["status"], found["reason"]), ("pending", "absent_within_settle_window"))
        self.assertEqual(self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer",
                                              ["engineer"])["decision"]["reason"], "effect_unresolved:record_checks")
        clock[0] += 61
        self.assertEqual(self.session.reconcile(self.case, pid, "engineer", ["engineer"])["status"], "not_applied")
        self.assertEqual(self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer",
                                              ["engineer"])["decision"]["status"], "allowed")


if __name__ == "__main__":
    unittest.main()
