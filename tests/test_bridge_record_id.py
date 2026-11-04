"""A destination record ID settles an effect only when a reader can tell it apart from another ID.

Both reconcile paths turn a destination's record ID into the ``external_id`` of a verified
``reconciled`` proof: ``Work.reconcile`` from the ``http`` status lookup, ``Session.reconcile`` from a
bound executor's ``applied`` proof. Each used to accept any nonempty string. An ID that is only
spaces names no record, and one that carries a control, format, separator or surrogate character
(a NUL, a newline, U+200B zero-width space, U+202E right-to-left override, U+00A0 no-break space...)
renders as, or splits into, an ID it is not. ``syberlabs.evidence.record_id`` refuses both. The
status lookup then records ``unverified`` ``destination_record_mismatch``, as it does for an empty ID;
the Session records ``unverified`` ``external_id_missing``, as it does for an empty ID. The effect
stays unresolved and a later record with a usable ID settles it once.

Not judged: whether the ID is the right record, an ID with leading or trailing U+0020 spaces,
confusable visible characters (Latin ``a`` against Cyrillic ``а``), length, and the ``economic_http``
settlement receipt (``receipt_matches``), which keeps its nonempty-string rule.
"""

import unittest

import tests.test_bridge_destination_ack as ack
from examples import release_gate
from spec.validate import validate_events
from syberlabs import Rejected, Session
from syberlabs.evidence import record_id, verified_reconciliation
from tests.test_session_applied_proof import LostWrite

# Strings that name no record. Each would have met the nonempty-string rule.
UNUSABLE = {
    "one space": " ",
    "spaces": "   ",
    "tab": "\t",
    "newline": "\n",
    "CRLF inside": "SHIP-1\r\nSHIP-2",
    "NUL": "SHIP-1\x00",
    "DEL": "SHIP-1\x7f",
    "C1 next line": "SHIP-1\x85",
    "zero-width space": "SHIP-1​",
    "right-to-left override": "‮1-PIHS",
    "byte order mark": "﻿SHIP-1",
    "no-break space": "SHIP 1",
    "ideographic space": "　",
    "line separator": "SHIP-1 ",
    "paragraph separator": "SHIP-1 ",
    "lone surrogate": "SHIP-\ud800",
}
# Over HTTP a lone surrogate never reaches the ID rule: the strict parser already calls that body ambiguous.
HTTP_UNUSABLE = {name: value for name, value in UNUSABLE.items() if name != "lone surrogate"}
# Strings that still name a record: the rule is about visibility, not ASCII.
USABLE = ("SHIP-1", "v1.4.0", "1400", "a b", "refs/tags/v1.4.0", "ü-ä", "日本-7", "x")


class RecordIdPredicate(unittest.TestCase):
    def test_unusable_ids_are_refused(self):
        for name, value in UNUSABLE.items():
            with self.subTest(name):
                self.assertFalse(record_id(value))

    def test_usable_ids_and_non_strings(self):
        for value in USABLE:
            with self.subTest(value):
                self.assertTrue(record_id(value))
        for value in ("", None, 0, 1400, True, ["SHIP-1"], {"id": "SHIP-1"}, b"SHIP-1"):
            with self.subTest(value=value):
                self.assertFalse(record_id(value))


class StatusLookupRecordId(ack.Cell):
    """``Work.reconcile``: a committed record matching key and digest, but with an unusable ``external_id``."""

    action = "ship"
    install = ack.StatusLookupAck.install
    unknown = ack.StatusLookupAck.unknown
    propose = ack.StatusLookupAck.propose

    def test_status_record_with_an_unusable_id_is_a_mismatch(self):
        case, key = self.unknown()
        record = self.dest.records[key]
        for name, value in HTTP_UNUSABLE.items():
            with self.subTest(name):
                record["external_id"] = value
                result = self.work.reconcile(case, key, "manager", ["manager"])
                self.assertEqual((result["status"], result["reason"]), ("unverified", "destination_record_mismatch"))
                self.assertEqual(result["event"]["kind"], "reconciliation_checked")
                self.assert_unresolved(case, key, lambda: self.propose(case))
        self.assertEqual(self.dest.posts, 1)
        self.assertEqual(self.dest.lookups, len(HTTP_UNUSABLE))
        self.assertEqual(validate_events(self.history(case), "unusable"), [])
        # Positive control: the same record with a usable ID reconciles, exactly once.
        record["external_id"] = "SHIP-" + key
        self.assert_verified(case, key, self.work.reconcile(case, key, "manager", ["manager"]))
        with self.assertRaisesRegex(Rejected, "already reconciled"):
            self.work.reconcile(case, key, "manager", ["manager"])
        self.assertEqual(self.dest.posts, 1)

    def test_usable_ids_reconcile(self):
        for value in USABLE:
            with self.subTest(value):
                self.fresh()
                case, key = self.unknown()
                self.dest.records[key]["external_id"] = value
                result = self.work.reconcile(case, key, "manager", ["manager"])
                self.assert_verified(case, key, result)
                self.assertEqual(result["event"]["body"]["proof"]["external_id"], value)


class SessionRecordId(unittest.TestCase):
    """``Session.reconcile``: an ``applied`` proof whose ``external_id`` is an unusable string."""

    def setUp(self):
        self.session = Session()
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

    def reconcile(self, proof) -> dict:
        self.executor.state, self.executor.proof = "applied", proof
        return self.session.reconcile(self.case, self.key, "engineer", ["engineer"])

    def published(self) -> bool:
        return next(r for r in self.session.inspect(self.case)["acceptance"] if r["id"] == "published")["passed"]

    def test_applied_proof_with_an_unusable_id_is_unverified(self):
        for name, value in UNUSABLE.items():
            with self.subTest(name):
                result = self.reconcile({"external_id": value, "ref": "refs/tags/v1.4.0"})
                self.assertEqual((result["status"], result["reason"]), ("unverified", "external_id_missing"))
                self.assertEqual(result["event"]["body"], {
                    "proposal_id": self.key, "action": "publish",
                    "status": "unverified", "reason": "external_id_missing", "actor": "engineer",
                })
        events = self.session.events[self.case]
        self.assertFalse(any(e["kind"] in ("reconciled", "effect_succeeded", "effect_rejected")
                             and e["body"].get("action") == "publish" for e in events))
        self.assertEqual(validate_events(events, "unusable"), [])
        self.assertTrue(self.session.verify_chain(self.case))
        self.assertFalse(self.published())
        retry = self.session.propose(self.case, "publish", {"version": "1.4.0"}, "engineer", ["engineer"])
        self.assertEqual(retry["decision"]["reason"], "effect_unresolved:publish")

        # Positive control: a usable ID then settles the same claim, exactly once.
        result = self.reconcile({"external_id": "v1.4.0", "ref": "refs/tags/v1.4.0"})
        self.assertEqual((result["status"], result["event"]["kind"]), ("verified", "reconciled"))
        self.assertTrue(verified_reconciliation(result["event"]))
        self.assertTrue(self.published())
        with self.assertRaises(Rejected) as refused:
            self.reconcile({"external_id": "v1.4.0"})
        self.assertEqual(refused.exception.code, "reconciliation_denied")

    def test_a_non_ascii_visible_id_settles(self):
        result = self.reconcile({"external_id": "日本-7"})
        self.assertEqual((result["status"], result["event"]["kind"]), ("verified", "reconciled"))
        self.assertEqual(result["event"]["body"]["proof"]["external_id"], "日本-7")
        self.assertTrue(self.published())


if __name__ == "__main__":
    unittest.main()
