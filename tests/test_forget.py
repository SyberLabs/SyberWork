"""Forgetting a thread under the policy's retention rule."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syberlabs import Rejected, Session
from syberlabs.build import GitRefEffect, default_contract
from syberlabs.cli import main as cli
from syberlabs.journal import Journal
from tests.test_build_thread import GOOD, Crash, RepoCase, git, git_ref

SECRET = "Rotate the payroll export credentials"
DAY = 86_400


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def gate(retention=None):
    return ({"id": "note", "version": 1, "inputs": {"objective": "string"}, "actions": {"record": {}},
             "acceptance": [{"id": "done", "kind": "effect", "action": "record"}]},
            {"version": 1, "actions": {"record": {"roles": ["dev"]}}, **({"retention": retention} if retention is not None else {})})


class SessionForget(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "journal"
        self.clock = Clock()

    def journal(self):
        journal = Journal(self.home)
        self.addCleanup(journal.close)
        return journal

    def session(self, retention=None):
        journal = Journal(self.home)
        self.addCleanup(journal.close)
        session = Session(clock=self.clock, journal=journal)
        contract, policy = gate(retention)
        session.install_contract(contract)
        if not session.policies:
            session.install_policy(policy)
            session.install_action("record", {"kind": "local"})
        return session

    def case(self, session, effect=False):
        case = session.create_case("note", 1, {"objective": SECRET}, "dev")
        if effect:
            proposal = session.propose(case, "record", {}, "dev", ["dev"])
            session.commit(case, proposal["proposal"]["id"], "dev")
        return case

    def test_history_without_effects_can_be_forgotten(self):
        session = self.session()
        case = self.case(session)
        result = session.forget(case, "dev", "scratch thread")
        self.assertIsNone(result["receipt"])
        self.assertEqual(self.journal().thread_ids(), [])
        with self.assertRaises(Rejected):
            self.session().inspect(case)
        [tombstone] = self.journal().forgotten()
        self.assertEqual((tombstone["thread"], tombstone["receipt"], tombstone["reason"]), (case, False, "scratch thread"))

    def test_history_that_proves_an_effect_is_kept_then_leaves_a_receipt(self):
        session = self.session({"effect_history_days": 30})
        case = self.case(session, effect=True)
        head = session.inspect(case)["events"][-1]["hash"]
        with self.assertRaises(Rejected) as caught:
            session.forget(case, "dev", "tidy up")
        self.assertEqual(caught.exception.code, "retention_required")
        self.assertIn("allowed from", caught.exception.detail)
        self.clock.now += 31 * DAY
        result = session.forget(case, "dev", "tidy up")
        receipt = result["receipt"]
        self.assertEqual((receipt["head"], receipt["events"], receipt["chain_valid"]), (head, 5, True))
        self.assertEqual([e["action"] for e in receipt["effects"]], ["record"])
        stored = (self.home / "receipts" / f"{case}.json").read_text() + (self.home / "forgotten.jsonl").read_text()
        self.assertNotIn(SECRET, stored, "receipts and tombstones must not keep the objective or bodies")

    def test_unresolved_effect_blocks_forgetting(self):
        session = self.session({"effect_history_days": 0})
        case = self.case(session)
        proposal = session.propose(case, "record", {}, "dev", ["dev"])
        session._append(case, "effect_started", {"proposal_id": proposal["proposal"]["id"], "action": "record",
                                                  "actor": "dev", "policy_version": 1,
                                                  "idempotency_key": proposal["proposal"]["id"]})
        with self.assertRaises(Rejected) as caught:
            session.forget(case, "dev", "tidy up")
        self.assertEqual(caught.exception.code, "retention_unresolved")

    def test_policy_rules_are_validated_and_applied(self):
        with self.assertRaises(Rejected):
            self.session({"keep_forever": True})
        session = self.session({"unaccepted_history_days": 7})
        case = self.case(session)
        with self.assertRaises(Rejected) as caught:
            session.forget(case, "dev", "early")
        self.assertEqual(caught.exception.code, "retention_required")
        with self.assertRaises(Rejected):
            session.forget(case, "dev", "  ")
        self.clock.now += 8 * DAY
        self.assertEqual(session.forget(case, "dev", "later")["forgotten"], case)

    def test_crash_between_receipt_and_removal_keeps_the_history(self):
        session = self.session({"effect_history_days": 0})
        case = self.case(session, effect=True)
        with patch.object(Journal, "remove_thread", side_effect=Crash()), self.assertRaises(Crash):
            session.forget(case, "dev", "tidy up")
        self.assertTrue((self.home / "receipts" / f"{case}.json").exists())
        self.assertTrue(self.session().verify_chain(case), "the history is intact until removal succeeds")


class KitForget(RepoCase):
    def test_forget_removes_records_not_effects(self):
        policy = {"version": 1, "actions": {"accept_change": {"roles": ["developer"]}}, "retention": {"effect_history_days": 0}}
        kit = self.kit(default_contract(self.root), policy)
        thread = kit.start(SECRET)
        thread.propose(changes=GOOD)
        thread.propose(changes={"src/export.py": "# other\n"})
        thread.check("c1")
        thread.accept("c1")
        accepted = git_ref(self.root, thread.target_ref)
        result = kit.forget(thread.id[:8], reason="done")
        self.assertEqual(result["candidate_refs_removed"], 2)
        self.assertEqual(git_ref(self.root, thread.target_ref), accepted, "the accepted branch is an effect and stays")
        self.assertEqual(git(self.root, "for-each-ref", "refs/syberlabs"), "")
        self.assertEqual(kit.threads(), [])
        self.assertEqual(result["receipt"]["effects"][0]["external_id"], accepted)
        self.assertEqual(kit.memory()["thread_records"]["forgotten"], 1)

    def test_accepted_thread_is_kept_under_the_default_policy(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        thread.accept("c1")
        with self.assertRaises(Rejected) as caught:
            kit.forget(thread.id, reason="tidy")
        self.assertEqual(caught.exception.code, "retention_required")
        self.assertEqual(len(kit.threads()), 1)

    def test_interrupted_acceptance_cannot_be_forgotten(self):
        policy = {"version": 1, "actions": {"accept_change": {"roles": ["developer"]}}, "retention": {"effect_history_days": 0}}
        kit = self.kit(default_contract(self.root), policy)
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        with patch.object(GitRefEffect, "apply", side_effect=Crash()), self.assertRaises(Crash):
            thread.accept("c1")
        with self.assertRaises(Rejected) as caught:
            kit.forget(thread.id, reason="tidy")
        self.assertEqual(caught.exception.code, "retention_unresolved")

    def test_cli_exports_before_forgetting(self):
        out = io.StringIO()
        export = self.root.parent / "thread.json"
        base = ["--repo", str(self.root), "--home", str(self.root / ".syberlabs"), "--actor", "dev"]
        with contextlib.redirect_stdout(out):
            cli(base + ["start", "Scratch idea"])
            thread = (self.root / ".syberlabs" / "current").read_text().strip()
            code = cli(base + ["forget", thread[:8], "--reason", "not needed", "--export", str(export)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(export.read_text())["status"]["objective"], "Scratch idea")
        self.assertIn("forgot", out.getvalue())
        self.assertFalse((self.root / ".syberlabs" / "current").exists())


if __name__ == "__main__":
    unittest.main()
