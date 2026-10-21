"""Direct tests of ``Session`` behaviour that the property suites found first.

A recorded effect (an action with no ``effect`` and so no executor) whose
commit dies between its claim and its completion is settled by ``reconcile``
from the record alone, exactly once.
"""
import unittest

from spec.validate import validate_events
from syberlabs import Rejected, Session
from tests.test_properties import ACTIONS, CONTRACT, POLICY


class Crash(BaseException):
    """The process dies; not an ``Exception`` so no handler in commit swallows it."""


class InterruptedRecordedEffect(unittest.TestCase):
    def setUp(self):
        self.session = Session(clock=lambda: 1_700_000_000.0)
        self.session.install_contract(CONTRACT)
        self.session.install_policy(POLICY)
        for name, doc in ACTIONS.items():
            self.session.install_action(name, doc)
        self.case = self.session.create_case(CONTRACT["id"], 1, {"objective": "recover a recorded effect"}, "alice")

    def claim_without_completion(self, action="review", args=None):
        """Commit ``action`` and die right after the claim is durable, as a process would."""
        proposal = self.session.propose(self.case, action, args or {}, "alice", ["operator"])
        self.assertEqual(proposal["decision"]["status"], "allowed")
        proposal_id = proposal["proposal"]["id"]
        original = self.session._append

        def dying(case_id, kind, body, **kwargs):
            event = original(case_id, kind, body, **kwargs)
            if kind == "effect_started":
                raise Crash()
            return event

        self.session._append = dying
        try:
            with self.assertRaises(Crash):
                self.session.commit(self.case, proposal_id, "alice")
        finally:
            self.session._append = original
        claim = self.session.events[self.case][-1]
        self.assertEqual(claim["kind"], "effect_started")
        self.assertEqual(claim["body"]["proposal_id"], proposal_id)
        return proposal_id, claim

    def test_reconcile_completes_the_claim_from_the_record_once(self):
        proposal_id, claim = self.claim_without_completion(args={"note": "n1"})
        # Until settled, the action is occupied.
        denied = self.session.propose(self.case, "review", {}, "alice", ["operator"])["decision"]
        self.assertEqual((denied["status"], denied["reason"]), ("denied", "effect_unresolved:review"))

        result = self.session.reconcile(self.case, proposal_id, "bob", ["manager"])
        self.assertEqual(result["status"], "recovered")
        event = result["event"]
        self.assertEqual(event["kind"], "effect_succeeded")
        self.assertEqual(event["body"], {
            "proposal_id": proposal_id, "action": "review",
            "output": {"recorded": True, "args": {"note": "n1"}}, "claim_hash": claim["hash"],
        })
        self.assertIs(event, self.session.events[self.case][-1])
        self.assertEqual(validate_events(self.session.events[self.case], "recovered"), [])

        # Exactly once: the second reconcile reports the existing settlement.
        with self.assertRaises(Rejected) as refused:
            self.session.reconcile(self.case, proposal_id, "bob", ["manager"])
        self.assertEqual(refused.exception.code, "reconciliation_denied")
        self.assertEqual(sum(1 for e in self.session.events[self.case] if e["kind"] == "effect_succeeded"), 1)

        # The same state as after an uninterrupted commit: completed once in this case, no longer unresolved.
        after = self.session.propose(self.case, "review", {}, "alice", ["operator"])["decision"]
        self.assertEqual((after["status"], after["reason"]), ("denied", "action_already_completed"))

    def test_recovery_still_requires_a_role_that_may_propose_or_manager(self):
        proposal_id, _ = self.claim_without_completion()
        with self.assertRaises(Rejected) as refused:
            self.session.reconcile(self.case, proposal_id, "carol", ["guest"])
        self.assertEqual(refused.exception.code, "reconciliation_denied")
        self.assertEqual(self.session.events[self.case][-1]["kind"], "effect_started")
        self.assertEqual(self.session.reconcile(self.case, proposal_id, "alice", ["operator"])["status"], "recovered")

    def test_a_completed_recorded_effect_is_not_reconcilable(self):
        proposal = self.session.propose(self.case, "review", {}, "alice", ["operator"])
        self.session.commit(self.case, proposal["proposal"]["id"], "alice")
        with self.assertRaises(Rejected) as refused:
            self.session.reconcile(self.case, proposal["proposal"]["id"], "bob", ["manager"])
        self.assertEqual(refused.exception.code, "reconciliation_denied")

    def test_an_action_with_an_effect_and_no_executor_is_still_unavailable(self):
        # A missing binding is not a record: an action that declares an effect, claimed
        # by a process that died in its executor and reopened without the binding, keeps
        # refusing reconciliation as before.
        session = Session(clock=lambda: 1_700_000_000.0)
        session.install_contract(CONTRACT)
        session.install_policy(POLICY)
        for name, doc in ACTIONS.items():
            session.install_action(name, dict(doc, effect="flaky") if name == "pay" else doc)

        class DiesInApply:
            def apply(self, case_id, args, key):
                raise Crash()

        session.bind_effect("pay", DiesInApply())
        case = session.create_case(CONTRACT["id"], 1, {"objective": "lose a binding"}, "alice")
        proposal_id = session.propose(case, "pay", {"amount": 10}, "alice", ["operator"])["proposal"]["id"]
        with self.assertRaises(Crash):
            session.commit(case, proposal_id, "alice")
        self.assertEqual(session.events[case][-1]["kind"], "effect_started")
        del session._executors["pay"]  # the next process did not bind the destination
        with self.assertRaises(Rejected) as refused:
            session.reconcile(case, proposal_id, "bob", ["manager"])
        self.assertEqual(refused.exception.code, "reconciliation_unavailable")
        self.assertEqual(session.events[case][-1]["kind"], "effect_started")


if __name__ == "__main__":
    unittest.main()
