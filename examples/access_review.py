"""Quarterly access review, built as a new project on syberlabs.Session.

No SyberWork database, no procurement ERP, no HTTP connector. The directory
roster is an observation the host project already verified.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs import Rejected, Session
from syberlabs.events import verify_events
from syberlabs.planner import StaticPlanner


CONTRACT = {
    "id": "access-review",
    "version": 1,
    "title": "Quarterly access review",
    "inputs": {"review_id": "string"},
    "actions": {
        "certify_scope": {
            "required_facts": [{"key": "roster", "source": "directory", "verified": True, "max_age_seconds": 86400}],
            "arguments": {"review_id": "fact:roster.review_id", "account_count": "fact:roster.account_count"},
        },
        "revoke_extra": {
            "requires_effect": "certify_scope",
            "approval_role": "manager",
            "required_facts": [{"key": "roster", "source": "directory", "verified": True, "max_age_seconds": 86400}],
            "arguments": {"review_id": "fact:roster.review_id"},
        },
    },
    "acceptance": [
        {"id": "extras_revoked", "kind": "effect", "action": "revoke_extra"},
        {"id": "auditor_signed", "kind": "signoff", "role": "auditor", "after_action": "revoke_extra"},
    ],
}

POLICY = {
    "version": 1,
    "actions": {
        "certify_scope": {"roles": ["reviewer"]},
        "revoke_extra": {"roles": ["reviewer"], "approval_role": "manager"},
    },
}

POLICY_TIGHTER = {
    "version": 2,
    "actions": {
        "certify_scope": {"roles": ["reviewer"], "approval_role": "manager"},
        "revoke_extra": {"roles": ["reviewer"], "approval_role": "manager"},
    },
}


def open_session() -> Session:
    session = Session()
    session.install_contract(CONTRACT)
    session.install_policy(POLICY)
    session.install_action("certify_scope", {"kind": "local", "title": "Certify the roster"})
    session.install_action("revoke_extra", {"kind": "local", "title": "Revoke extra access"})
    return session


def roster(review_id: str, accounts: int = 12) -> dict:
    return {"review_id": review_id, "account_count": accounts}


def complete_review(session: Session, review_id: str = "AR-100") -> dict:
    """Happy path: verified roster, compiled steps, manager approval, auditor signoff."""
    case_id = session.create_case("access-review", 1, {"review_id": review_id}, "reviewer")
    session.observe(case_id, "roster", roster(review_id), "directory", "snap-1", "reviewer", verified=True)
    certified = session.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
    if certified["decision"]["status"] != "allowed":
        raise RuntimeError(certified["decision"])
    session.commit(case_id, certified["proposal"]["id"], "reviewer")
    revoke = session.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
    if revoke["decision"]["reason"] != "approval_required:manager":
        raise RuntimeError(revoke["decision"])
    session.approve(case_id, revoke["proposal"]["id"], "manager", ["manager"])
    committed = session.commit(case_id, revoke["proposal"]["id"], "reviewer")
    if committed["status"] != "succeeded":
        raise RuntimeError(committed)
    session.signoff(case_id, "auditor", ["auditor"], "auditor")
    inspected = session.inspect(case_id)
    if not inspected["complete"] or not inspected["chain_valid"]:
        raise RuntimeError(inspected["status"])
    recorded = session.explain_admission(case_id, certified["proposal"]["id"], when="recorded")
    current = session.explain_admission(case_id, certified["proposal"]["id"], when="now")
    return {
        "case_id": case_id,
        "status": inspected["status"],
        "events": len(inspected["events"]),
        "chain_valid": inspected["chain_valid"],
        "recorded_rule": recorded["rule"],
        "recorded_reason": recorded["reason"],
        "current_reason": current["reason"],
        "certify_proposal": certified["proposal"]["id"],
    }


def planner_skips_ahead(session: Session, review_id: str = "AR-200") -> dict:
    """A planner that jumps to revoke is denied, and no effect is recorded."""
    case_id = session.create_case("access-review", 1, {"review_id": review_id}, "reviewer")
    session.observe(case_id, "roster", roster(review_id), "directory", "snap-2", "reviewer", verified=True)
    seen = {}

    class Spy(StaticPlanner):
        def propose(self, context):
            seen["keys"] = set(context)
            return super().propose(context)

    result = session.suggest(case_id, "planner", ["model", "reviewer"], Spy("revoke_extra", {"review_id": review_id}))
    started = [event for event in session.inspect(case_id)["events"] if event["kind"] == "effect_started"]
    return {
        "reason": result["decision"]["reason"],
        "context_keys": seen["keys"],
        "effects": len(started),
        "case_id": case_id,
    }


def unverified_roster(session: Session, review_id: str = "AR-300") -> str:
    case_id = session.create_case("access-review", 1, {"review_id": review_id}, "reviewer")
    session.observe(case_id, "roster", roster(review_id), "directory", "typed", "reviewer", verified=False)
    result = session.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
    return result["decision"]["reason"]


def reject_bad_contract() -> str:
    session = Session()
    bad = {
        "id": "access-review",
        "version": 1,
        "inputs": {"review_id": "string"},
        "actions": {"certify_scope": {"required_facts": [{"key": "roster"}]}},
        "acceptance": [{"id": "done", "kind": "effect", "action": "certify_scope"}],
    }
    try:
        session.install_contract(bad)
    except Rejected as error:
        return error.code
    raise RuntimeError("bad contract was installed")


def tighten_and_replay(session: Session, case_id: str) -> list[str]:
    session.install_policy(POLICY_TIGHTER)
    replayed = session.replay(case_id, 1, 2)
    return [item["action"] + ":" + item["after"]["reason"] for item in replayed["changed_decisions"]]


def tamper_is_detected(session: Session, case_id: str) -> bool:
    events = session.inspect(case_id)["events"]
    forged = [{**event, "body": {**event["body"], "actor": "forged"}} if index == 0 else event for index, event in enumerate(events)]
    return session.verify_chain(case_id) and not verify_events(forged)


def run() -> dict:
    session = open_session()
    done = complete_review(session)
    skipped = planner_skips_ahead(session)
    unverified = unverified_roster(session)
    replayed = tighten_and_replay(session, done["case_id"])
    report = {
        "complete": done["status"] == "complete" and done["chain_valid"],
        "events": done["events"],
        "recorded_reason": done["recorded_reason"],
        "current_reason": done["current_reason"],
        "planner_denied": skipped["reason"],
        "planner_effects": skipped["effects"],
        "unverified": unverified,
        "bad_contract": reject_bad_contract(),
        "replay_changed": replayed,
        "tamper_detected": tamper_is_detected(session, done["case_id"]),
    }
    if not report["complete"] or report["planner_effects"] != 0 or not report["tamper_detected"]:
        raise RuntimeError(report)
    return report


if __name__ == "__main__":
    report = run()
    print(
        "access-review complete={complete} events={events} "
        "recorded={recorded_reason} now={current_reason} "
        "planner={planner_denied} unverified={unverified} "
        "replay={replay_changed} tamper_detected={tamper_detected}".format(**report)
    )
