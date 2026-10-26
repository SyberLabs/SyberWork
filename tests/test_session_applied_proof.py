"""An executor's ``applied`` proof settles an effect only when it names the destination's record.

``Session.reconcile`` turns a bound executor's ``applied`` status into a
``reconciled`` event. That event licenses acceptance only when its proof cites
a nonempty string ``external_id`` (``verified_reconciliation``, and the
``reconciled`` schema in ``spec/reconciliation.schema.json``). An ``applied``
proof without one is recorded as ``reconciliation_checked`` ``unverified`` with
reason ``external_id_missing``; the effect stays unresolved.
"""
import tempfile
import unittest
from pathlib import Path

from examples import release_gate
from spec.validate import validate_events
from syberlabs import Rejected, Session
from syberlabs.evidence import verified_reconciliation
from syberlabs.journal import Journal


class LostWrite:
    """A local executor whose write is lost in transit and whose status reports a scripted proof."""

    def __init__(self):
        self.state, self.proof = "applied", {}

    def apply(self, case_id, args, key):
        raise OSError("connection reset after the write")

    def status(self, case_id, args, key):
        return self.state, self.proof


# ``applied`` proofs that name no destination record: no ``external_id``, or one that is not a nonempty string.
UNIDENTIFIED = {
    "empty object": {},
    "other fields only": {"ref": "refs/tags/v1.4.0", "destination": "git:origin:refs/tags/v1.4.0"},
    "empty string": {"external_id": "", "ref": "refs/tags/v1.4.0"},
    "null": {"external_id": None},
    "zero": {"external_id": 0},
    "integer": {"external_id": 1400},
    "true": {"external_id": True},
    "list": {"external_id": ["v1.4.0"]},
    "object": {"external_id": {"id": "v1.4.0"}},
    "proof is None": None,
    "proof is a string": "v1.4.0",
    "proof is a list": [{"external_id": "v1.4.0"}],
}


class AppliedProofWithoutExternalId(unittest.TestCase):
    """``applied`` without the destination's record id is unverified, never a ``reconciled`` event."""

    def setUp(self):
        self.open(Session())

    def open(self, session: Session) -> None:
        """Leave one ``publish`` claim unknown: the write was sent, its reply was lost."""
        self.session = session
        self.session.install_contract(release_gate.CONTRACT)
        self.session.install_policy(release_gate.POLICY)
        self.session.install_action("record_checks", {"kind": "local"})
        self.session.install_action("publish", {"kind": "local", "effect": "tag_push"})
        self.case = self.session.create_case("release-gate", 1, {"version": "1.4.0"}, "engineer")
        self.session.observe(self.case, "checks", {"version": "1.4.0"}, "ci", "build-90", "engineer", verified=True)
        review = self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer", ["engineer"])
        self.session.commit(self.case, review["proposal"]["id"], "engineer")
        self.executor = LostWrite()
        self.session.bind_effect("publish", self.executor)
        proposal = self.session.propose(self.case, "publish", {"version": "1.4.0"}, "engineer", ["engineer"])
        self.key = proposal["proposal"]["id"]
        self.session.approve(self.case, self.key, "maintainer", ["maintainer"])
        self.assertEqual(self.session.commit(self.case, self.key, "engineer")["status"], "unknown")

    def reconcile(self, state, proof) -> dict:
        self.executor.state, self.executor.proof = state, proof
        return self.session.reconcile(self.case, self.key, "engineer", ["engineer"])

    def published(self) -> bool:
        return next(r for r in self.session.inspect(self.case)["acceptance"] if r["id"] == "published")["passed"]

    def test_applied_without_a_record_id_is_unverified(self):
        for name, proof in UNIDENTIFIED.items():
            with self.subTest(name):
                result = self.reconcile("applied", proof)
                self.assertEqual((result["status"], result["reason"]), ("unverified", "external_id_missing"))
                self.assertEqual(result["event"]["kind"], "reconciliation_checked")
                self.assertEqual(result["event"]["body"], {
                    "proposal_id": self.key, "action": "publish",
                    "status": "unverified", "reason": "external_id_missing", "actor": "engineer",
                })
                self.assertIs(result["event"], self.session.events[self.case][-1])
        events = self.session.events[self.case]
        self.assertFalse(any(e["kind"] in ("reconciled", "effect_succeeded", "effect_rejected")
                             and e["body"].get("action") == "publish" for e in events))
        self.assertEqual(validate_events(events, "unidentified"), [])
        self.assertTrue(self.session.verify_chain(self.case))
        self.assertFalse(self.published())
        retry = self.session.propose(self.case, "publish", {"version": "1.4.0"}, "engineer", ["engineer"])
        self.assertEqual(retry["decision"]["reason"], "effect_unresolved:publish")

        # Positive control: the destination's record id then settles the same claim, exactly once.
        result = self.reconcile("applied", {"external_id": "v1.4.0", "ref": "refs/tags/v1.4.0"})
        self.assertEqual((result["status"], result["reason"]), ("verified", "destination_state_matched"))
        self.assertEqual(result["event"]["kind"], "reconciled")
        self.assertEqual(result["event"]["body"]["proof"]["external_id"], "v1.4.0")
        self.assertTrue(verified_reconciliation(result["event"]))
        self.assertEqual(validate_events(self.session.events[self.case], "settled"), [])
        self.assertTrue(self.published())
        with self.assertRaises(Rejected) as refused:
            self.reconcile("applied", {"external_id": "v1.4.0"})
        self.assertEqual(refused.exception.code, "reconciliation_denied")

    def test_a_reopened_journal_keeps_the_effect_unresolved(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = Path(folder.name) / "journal"
        journal = Journal(home)
        self.addCleanup(journal.close)
        self.open(Session(journal=journal))
        self.assertEqual(self.reconcile("applied", {"external_id": ""})["reason"], "external_id_missing")

        reopened_journal = Journal(home)
        self.addCleanup(reopened_journal.close)
        self.session = Session(journal=reopened_journal)
        self.session.bind_effect("publish", self.executor)
        self.assertEqual(self.session.history(self.case)[-1]["body"]["reason"], "external_id_missing")
        self.assertTrue(self.session.verify_chain(self.case))
        self.assertFalse(self.published())
        retry = self.session.propose(self.case, "publish", {"version": "1.4.0"}, "engineer", ["engineer"])
        self.assertEqual(retry["decision"]["reason"], "effect_unresolved:publish")
        result = self.reconcile("applied", {"external_id": "v1.4.0"})
        self.assertEqual((result["status"], result["event"]["kind"]), ("verified", "reconciled"))
        self.assertTrue(self.published())

    def test_other_states_do_not_need_a_record_id(self):
        # The rule judges only ``applied``: absence of a write names no record, as before.
        self.assertEqual(self.reconcile("unknown", {})["reason"], "status_unavailable")
        self.assertEqual(self.reconcile("absent", None)["reason"], "absent_within_settle_window")
        result = self.reconcile("not_applied", {})
        self.assertEqual((result["status"], result["event"]["kind"]), ("not_applied", "effect_rejected"))
        self.assertEqual(validate_events(self.session.events[self.case], "not_applied"), [])
        after = self.session.propose(self.case, "publish", {"version": "1.4.0"}, "engineer", ["engineer"])
        self.assertNotEqual(after["decision"]["reason"], "effect_unresolved:publish")


if __name__ == "__main__":
    unittest.main()
