"""Rehearsal of the denials a paid pilot has to show.

This is not a customer, a price, or a license. It runs the access-review
contract and one candidate cell so an engineer can point at a case history
before Gate 0 of docs/PAID_PROBLEM.md names a real workflow.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples import access_review
from syberlabs import Rejected, Session
from syberwork.core import Work

BASE = "0" * 40


def _candidate(n: int) -> dict:
    return {
        "id": f"c{n}",
        "commit": f"{n:040x}",
        "tree": f"{1000 + n:040x}",
        "base": BASE,
        "parents": [],
        "operator": "patch",
        "provider": {"name": "fixture", "revision": "1"},
        "changed_paths": ["src/app.py"],
        "diff": {"digest": "d" * 64, "bytes": 80, "files": 1},
    }


def _evaluation(n: int) -> dict:
    return {
        "candidate": f"c{n}",
        "commit": f"{n:040x}",
        "tree": f"{1000 + n:040x}",
        "evaluator": "host.checks/1",
        "checks": [{
            "name": "tests",
            "state": "passed",
            "exit_code": 0,
            "duration_ms": 5,
            "output_digest": "e" * 64,
            "output_tail": "ok",
        }],
    }


def _evolution_contract() -> dict:
    return {
        "id": "repo-change",
        "version": 1,
        "inputs": {"objective": "string"},
        "actions": {"promote": {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "promote"}],
        "evolution": {
            "scope": {"paths": ["src/"], "max_files": 3, "max_diff_bytes": 5000},
            "operators": ["patch"],
            "budget": {"max_candidates": 4, "max_evaluations": 4, "max_seconds": 60},
            "evaluation": {
                "checks": {"tests": {"argv": ["python", "-m", "unittest"], "timeout_seconds": 60}},
                "required": ["tests"],
                "max_age_seconds": 600,
            },
            "promotion": {"action": "promote", "roles": ["developer"]},
        },
    }


def _role_denial(session: Session) -> dict:
    case_id = session.create_case("access-review", 1, {"review_id": "AR-role"}, "guest")
    session.observe(case_id, "roster", access_review.roster("AR-role"), "directory", "snap", "reviewer", verified=True)
    result = session.propose(case_id, "certify_scope", {}, "guest", ["auditor"])
    explained = session.explain_admission(case_id, result["proposal"]["id"])
    return {"reason": result["decision"]["reason"], "rule": explained["rule"], "case_id": case_id}


def _approval_denial(session: Session) -> dict:
    case_id = session.create_case("access-review", 1, {"review_id": "AR-approve"}, "reviewer")
    session.observe(case_id, "roster", access_review.roster("AR-approve"), "directory", "snap", "reviewer", verified=True)
    certified = session.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
    session.commit(case_id, certified["proposal"]["id"], "reviewer")
    revoke = session.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
    explained = session.explain_admission(case_id, revoke["proposal"]["id"])
    return {"reason": revoke["decision"]["reason"], "rule": explained["rule"], "case_id": case_id}


def _unverified(session: Session) -> dict:
    case_id = session.create_case("access-review", 1, {"review_id": "AR-fact"}, "reviewer")
    session.observe(case_id, "roster", access_review.roster("AR-fact"), "directory", "typed", "reviewer", verified=False)
    result = session.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
    explained = session.explain_admission(case_id, result["proposal"]["id"])
    return {"reason": result["decision"]["reason"], "rule": explained["rule"], "case_id": case_id}


def _prior_effect(session: Session) -> dict:
    skipped = access_review.planner_skips_ahead(session, "AR-prior")
    explained = session.explain_admission(skipped["case_id"], _proposal_id(session, skipped["case_id"]))
    return {"reason": skipped["reason"], "rule": explained["rule"], "effects": skipped["effects"], "case_id": skipped["case_id"]}


def _proposal_id(session: Session, case_id: str) -> str:
    proposed = next(event for event in reversed(session.history(case_id)) if event["kind"] == "proposed")
    return proposed["body"]["id"]


def _self_evaluation() -> str:
    folder = tempfile.TemporaryDirectory()
    work = Work(Path(folder.name) / "cell.sqlite")
    try:
        work.install_contract(_evolution_contract())
        work.install_policy({"version": 1, "actions": {"promote": {"roles": ["developer"]}}})
        work.install_action("promote", {"kind": "local"})
        case_id = work.create_case("repo-change", 1, {"objective": "Add CSV export"}, "developer")
        work.record_candidate(case_id, _candidate(1), "search-host", ["search"])
        try:
            work.record_evaluation(case_id, _evaluation(1), "search-host", ["evaluator"])
        except Rejected as error:
            return error.code
        raise RuntimeError("the registering actor was allowed to evaluate")
    finally:
        work.close()
        folder.cleanup()


class _Unconfirmed:
    def apply(self, case_id, args, key):
        raise OSError("destination did not confirm")

    def status(self, case_id, args, key):
        return "unknown", {}


def _unknown_effect() -> dict:
    session = Session()
    session.install_contract(access_review.CONTRACT)
    session.install_policy(access_review.POLICY)
    session.install_action("certify_scope", {"kind": "local", "title": "Certify", "effect": "directory"})
    session.install_action("revoke_extra", {"kind": "local", "title": "Revoke"})
    session.bind_effect("certify_scope", _Unconfirmed())
    case_id = session.create_case("access-review", 1, {"review_id": "AR-unknown"}, "reviewer")
    session.observe(case_id, "roster", access_review.roster("AR-unknown"), "directory", "snap", "reviewer", verified=True)
    proposed = session.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
    committed = session.commit(case_id, proposed["proposal"]["id"], "reviewer")
    events = session.inspect(case_id)["events"]
    succeeded = [
        event for event in events
        if event["kind"] == "effect_succeeded" and event["body"].get("proposal_id") == proposed["proposal"]["id"]
    ]
    return {
        "status": committed["status"],
        "succeeded": len(succeeded),
        "chain_valid": session.verify_chain(case_id),
        "case_id": case_id,
    }


def rehearse() -> dict:
    """Run the public stand-in for Gate 1. No customer identifiers are included."""
    session = access_review.open_session()
    done = access_review.complete_review(session, "AR-100")
    role = _role_denial(session)
    approval = _approval_denial(session)
    fact = _unverified(session)
    prior = _prior_effect(session)
    unknown = _unknown_effect()
    report = {
        "customer": None,
        "price": None,
        "license": None,
        "denials": {
            "actor_role": role["reason"],
            "approval": approval["reason"],
            "unverified_fact": fact["reason"],
            "prior_effect": prior["reason"],
            "self_evaluation": _self_evaluation(),
            "unknown_effect": unknown["status"],
        },
        "explanations": {
            "actor_role": role["rule"],
            "approval": approval["rule"],
            "unverified_fact": fact["rule"],
            "prior_effect": prior["rule"],
        },
        "unknown_succeeded_count": unknown["succeeded"],
        "prior_effects": prior["effects"],
        "chain_valid": done["chain_valid"] and unknown["chain_valid"] and session.verify_chain(role["case_id"]),
        "local_effect_succeeded": done["status"] == "complete",
    }
    if report["prior_effects"] != 0 or report["unknown_succeeded_count"] != 0 or not report["chain_valid"]:
        raise RuntimeError(report)
    return report


if __name__ == "__main__":
    report = rehearse()
    print(
        "pilot rehearsal customer={customer} price={price} license={license} "
        "role={actor_role} approval={approval} fact={unverified_fact} "
        "prior={prior_effect} self_evaluation={self_evaluation} unknown={unknown_effect} "
        "unknown_succeeded={succeeded} chain={chain}".format(
            customer=report["customer"],
            price=report["price"],
            license=report["license"],
            succeeded=report["unknown_succeeded_count"],
            chain=report["chain_valid"],
            **report["denials"],
        )
    )
