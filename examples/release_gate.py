"""Release gate driven only by syberlabs.Session.

This is not the procurement ERP. An outside project can copy this file,
swap the contract, and run propose, admission, approval, commit, signoff,
explain, and replay without SyberWork.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs import Session
from syberlabs.planner import StaticPlanner


CONTRACT = {
    "id": "release-gate",
    "version": 1,
    "title": "Ship a tagged build",
    "inputs": {"version": "string"},
    "actions": {
        "record_checks": {
            "required_facts": [{"key": "checks", "source": "ci", "verified": True}],
            "arguments": {"version": "fact:checks.version"},
        },
        "publish": {
            "requires_effect": "record_checks",
            "approval_role": "maintainer",
            "required_facts": [{"key": "checks", "source": "ci", "verified": True}],
            "arguments": {"version": "fact:checks.version"},
        },
    },
    "acceptance": [
        {"id": "published", "kind": "effect", "action": "publish"},
        {"id": "signed", "kind": "signoff", "role": "maintainer", "after_action": "publish"},
    ],
}

POLICY = {
    "version": 1,
    "actions": {
        "record_checks": {"roles": ["engineer"]},
        "publish": {"roles": ["engineer"], "approval_role": "maintainer"},
    },
}


def run() -> dict:
    session = Session()
    session.install_contract(CONTRACT)
    session.install_policy(POLICY)
    session.install_action("record_checks", {"kind": "local", "title": "Record CI"})
    session.install_action("publish", {"kind": "local", "title": "Publish tag"})
    case_id = session.create_case("release-gate", 1, {"version": "1.4.0"}, "engineer")
    session.observe(case_id, "checks", {"version": "1.4.0", "passed": True}, "ci", "build-90", "engineer", verified=True)

    review = session.propose(case_id, "record_checks", {"version": "1.4.0"}, "engineer", ["engineer"])
    if review["decision"]["status"] != "allowed":
        raise RuntimeError(review["decision"])
    session.commit(case_id, review["proposal"]["id"], "engineer")

    blocked = session.suggest(
        case_id, "engineer", ["engineer", "model"],
        StaticPlanner("publish", {"version": "1.4.0"}),
    )
    if blocked["decision"]["reason"] != "approval_required:maintainer":
        raise RuntimeError(blocked["decision"])
    explained = session.explain_admission(case_id, blocked["proposal"]["id"])
    if explained["rule"] != "approval.required" or "rule" in blocked["decision"]:
        raise RuntimeError(explained)
    session.approve(case_id, blocked["proposal"]["id"], "maintainer", ["maintainer"])
    published = session.commit(case_id, blocked["proposal"]["id"], "engineer")
    if published["status"] != "succeeded":
        raise RuntimeError(published)
    session.signoff(case_id, "maintainer", ["maintainer"], "maintainer")
    inspected = session.inspect(case_id)
    if not inspected["complete"] or not inspected["chain_valid"]:
        raise RuntimeError(inspected["status"])
    replayed = session.replay(case_id, 1, 1)
    if replayed["changed_decisions"]:
        raise RuntimeError(replayed["changed_decisions"])
    if any("rule" in event["body"] for event in inspected["events"] if event["kind"] == "decision"):
        raise RuntimeError("rule was stored in a decision event")
    return inspected


if __name__ == "__main__":
    result = run()
    print(f"release-gate {result['status']} events={len(result['events'])} chain={result['chain_valid']}")
