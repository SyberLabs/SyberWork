"""Candidates, the evolution contract section, and the candidate.promotable admission rule.

These tests drive syberlabs.Session with fixed Git object ids. The Build Thread
tests exercise the same records against a real repository.
"""

import copy
import tempfile
import unittest
from pathlib import Path

from spec.validate import validate_events
from syberlabs import Rejected, Session
from syberlabs.contracts import prepare_contract
from syberlabs.planner import StaticPlanner
from syberlabs.search import Candidate
from syberwork.core import Work

BASE = "0" * 40


def oid(n: int) -> str:
    return f"{n:040x}"


EVOLUTION = {
    "scope": {"paths": ["src/", "tests/"], "exclude": ["src/vendor/"], "max_files": 3, "max_diff_bytes": 5000},
    "operators": ["patch", "mutation", "crossover"],
    "budget": {"max_candidates": 6, "max_evaluations": 8, "max_seconds": 60},
    "evaluation": {
        "checks": {"tests": {"argv": ["python", "-m", "unittest"], "timeout_seconds": 60},
                   "lint": {"argv": ["python", "-m", "pyflakes", "src"]}},
        "required": ["tests"],
        "max_age_seconds": 600,
    },
    "promotion": {"action": "promote", "roles": ["developer"]},
}


def contract(version=1, evolution=None, approval=None):
    promote = {} if approval is None else {"approval_role": approval}
    doc = {
        "id": "repo-change",
        "version": version,
        "inputs": {"objective": "string"},
        "actions": {"promote": promote},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "promote"}],
        "evolution": copy.deepcopy(EVOLUTION if evolution is None else evolution),
    }
    if approval is not None:
        doc["evolution"]["promotion"]["approval_role"] = approval
    return doc


POLICY = {"version": 1, "actions": {"promote": {"roles": ["developer", "model"]}}}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def candidate(n, parents=(), operator="patch", paths=("src/app.py",), signal=None, bytes_=100):
    return {
        "id": f"c{n}", "commit": oid(n), "tree": oid(1000 + n), "base": BASE, "parents": list(parents),
        "operator": operator, "provider": {"name": "fixture", "revision": "1"},
        "changed_paths": list(paths), "diff": {"digest": "d" * 64, "bytes": bytes_, "files": len(paths)},
        "signal": signal,
    }


def evaluation(n, tests="passed", lint=None):
    checks = [{"name": "tests", "state": tests, "exit_code": 0 if tests == "passed" else 1, "duration_ms": 5,
               "output_digest": "e" * 64, "output_tail": "ok" if tests == "passed" else "FAIL: test_export"}]
    if lint is not None:
        checks.append({"name": "lint", "state": lint, "exit_code": 0 if lint == "passed" else 1, "duration_ms": 3,
                       "output_digest": "f" * 64, "output_tail": ""})
    return {"candidate": f"c{n}", "commit": oid(n), "tree": oid(1000 + n), "evaluator": "host.checks/1", "checks": checks}


def promote_args(n):
    return {"candidate": f"c{n}", "commit": oid(n), "base": BASE}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = Session(clock=self.clock)

    def open(self, doc=None, policy=POLICY):
        doc = doc or contract()
        self.session.install_contract(doc)
        if not self.session.policies:
            self.session.install_policy(policy)
        self.session.install_action("promote", {"kind": "local", "title": "Accept candidate"})
        return self.session.create_case(doc["id"], doc["version"], {"objective": "Add CSV export"}, "developer")

    def promote(self, case, n, actor="developer", roles=("developer",)):
        return self.session.propose(case, "promote", promote_args(n), actor, list(roles))


class ContractSection(unittest.TestCase):
    def test_valid_section_is_stored_unchanged(self):
        stored = prepare_contract(contract())
        self.assertEqual(stored["evolution"], EVOLUTION)

    def test_invalid_sections_fail_closed(self):
        def broken(mutate):
            doc = contract()
            mutate(doc["evolution"], doc)
            return doc

        cases = {
            "unknown": lambda e, d: e.update(strategy="anything"),
            "traversal": lambda e, d: e["scope"].update(paths=["../outside"]),
            "absolute": lambda e, d: e["scope"].update(paths=["/etc"]),
            "backslash": lambda e, d: e["scope"].update(paths=["src\\x"]),
            "model promoter": lambda e, d: e["promotion"].update(roles=["developer", "model"]),
            "search promoter": lambda e, d: e["promotion"].update(roles=["search"]),
            "unknown action": lambda e, d: e["promotion"].update(action="merge"),
            "action not an object": lambda e, d: d["actions"].update(promote="yes"),
            "approval mismatch": lambda e, d: e["promotion"].update(approval_role="maintainer"),
            "undeclared check": lambda e, d: e["evaluation"].update(required=["typecheck"]),
            "shell string": lambda e, d: e["evaluation"]["checks"]["tests"].update(argv="python -m unittest"),
            "empty operators": lambda e, d: e.update(operators=[]),
            "budget ceiling": lambda e, d: e["budget"].update(max_candidates=5000),
            "remote ref": lambda e, d: e["promotion"].update(target_ref="refs/remotes/origin/main"),
            "ref traversal": lambda e, d: e["promotion"].update(target_ref="refs/heads/../main"),
        }
        for label, mutate in cases.items():
            with self.subTest(label), self.assertRaises(Rejected) as caught:
                prepare_contract(broken(mutate))
            self.assertEqual(caught.exception.code, "invalid_contract")

    def test_contract_without_section_is_unaffected(self):
        doc = contract()
        del doc["evolution"]
        self.assertNotIn("evolution", prepare_contract(doc))


class Records(Fixture):
    def test_scope_is_recomputed_by_the_runtime(self):
        case = self.open()
        with self.assertRaises(Rejected):
            self.session.record_candidate(case, {**candidate(1), "scope_violations": []}, "host")
        event = self.session.record_candidate(case, candidate(1, paths=("src/app.py", "setup.py", "src/vendor/x.py", ".git/config")), "host")
        self.assertEqual(event["body"]["scope_violations"], [".git/config", "setup.py", "src/vendor/x.py"])
        self.assertEqual(event["body"]["limit_violations"], ["max_files"])

    def test_operators_budget_and_lineage_are_enforced(self):
        case = self.open()
        with self.assertRaises(Rejected) as caught:
            self.session.record_candidate(case, candidate(1, operator="rewrite_everything"), "host")
        self.assertEqual(caught.exception.code, "operator_not_permitted")
        with self.assertRaises(Rejected):
            self.session.record_candidate(case, candidate(1, parents=("c9",)), "host")
        for n in range(1, 7):
            self.session.record_candidate(case, candidate(n), "host")
        with self.assertRaises(Rejected) as caught:
            self.session.record_candidate(case, candidate(7), "host")
        self.assertEqual((caught.exception.code, caught.exception.detail), ("budget_exhausted", "max_candidates"))
        with self.assertRaises(Rejected):
            self.session.record_candidate(case, candidate(1), "host")

    def test_evaluation_must_name_the_registered_tree(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        wrong_tree = {**evaluation(1), "tree": oid(9999)}
        with self.assertRaises(Rejected) as caught:
            self.session.record_evaluation(case, wrong_tree, "host")
        self.assertEqual(caught.exception.code, "evaluation_mismatch")
        forged = evaluation(1)
        forged["checks"][0]["exit_code"] = 1
        with self.assertRaises(Rejected):
            self.session.record_evaluation(case, forged, "host")
        undeclared = evaluation(1)
        undeclared["checks"][0]["name"] = "typecheck"
        with self.assertRaises(Rejected):
            self.session.record_evaluation(case, undeclared, "host")
        with self.assertRaises(Rejected) as caught:
            self.session.record_evaluation(case, evaluation(2), "host")
        self.assertEqual(caught.exception.code, "unknown_candidate")

    def test_records_are_bounded(self):
        case = self.open()
        with self.assertRaises(Rejected):
            self.session.record_candidate(case, candidate(1, signal={"rationale": "x" * 5000}), "host")
        with self.assertRaises(Rejected):
            self.session.record_candidate(case, candidate(1, paths=tuple(f"src/{i}.py" for i in range(1001))), "host")

    def test_evaluation_budget(self):
        doc = contract(evolution={**EVOLUTION, "budget": {"max_candidates": 2, "max_evaluations": 1}})
        case = self.open(doc)
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        with self.assertRaises(Rejected) as caught:
            self.session.record_evaluation(case, evaluation(1), "host")
        self.assertEqual(caught.exception.detail, "max_evaluations")

    def test_cancelled_thread_refuses_records(self):
        case = self.open()
        self.session._append(case, "case_cancelled", {"actor": "manager", "role": "manager", "reason": "stop"})
        with self.assertRaises(Rejected) as caught:
            self.session.record_candidate(case, candidate(1), "host")
        self.assertEqual(caught.exception.code, "case_cancelled")

    def test_contract_without_evolution_refuses_candidates(self):
        doc = contract()
        del doc["evolution"]
        case = self.open(doc)
        with self.assertRaises(Rejected) as caught:
            self.session.record_candidate(case, candidate(1), "host")
        self.assertEqual(caught.exception.code, "evolution_not_enabled")


class Promotion(Fixture):
    def test_evaluated_candidate_is_promoted_by_a_human(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        self.assertEqual(self.promote(case, 1)["decision"]["reason"], "candidate_not_evaluated")
        self.session.record_evaluation(case, evaluation(1), "host")
        proposal = self.promote(case, 1)
        self.assertEqual(proposal["decision"], {"status": "allowed", "reason": "all_checks_passed"})
        self.assertEqual(self.session.commit(case, proposal["proposal"]["id"], "developer")["status"], "succeeded")
        inspected = self.session.inspect(case)
        self.assertTrue(inspected["complete"])
        self.assertTrue(self.session.verify_chain(case))
        view = Candidate.from_view(inspected["candidates"][0])
        self.assertTrue(view.authoritative)
        self.assertEqual((view.evaluation, view.promotion), ("passed", "promoted"))

    def test_no_model_or_agent_can_promote(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        suggested = self.session.suggest(case, "planner", ["model"], StaticPlanner("promote", promote_args(1)))
        self.assertEqual(suggested["decision"]["reason"], "candidate_promotion_origin")
        disguised = self.promote(case, 1, actor="planner", roles=("developer", "model"))
        self.assertEqual(disguised["decision"]["reason"], "candidate_promotion_origin")
        explained = self.session.explain_admission(case, disguised["proposal"]["id"])
        self.assertEqual(explained["rule"], "candidate.promotable")
        self.assertEqual(self.session.candidates(case)[0]["promotion"]["state"], "denied")
        self.assertFalse(self.session.candidates(case)[0]["authoritative"])

    def test_lineage_is_provenance_not_evidence(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1, operator="mutation"), "host")
        self.session.record_candidate(case, candidate(2, operator="mutation"), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        self.session.record_evaluation(case, evaluation(2), "host")
        child = candidate(3, parents=("c1", "c2"), operator="crossover",
                          signal={"fitness": 1.0, "passed": True, "parents_passed": True})
        self.session.record_candidate(case, child, "host")
        self.assertEqual(self.promote(case, 3)["decision"]["reason"], "candidate_not_evaluated")
        self.session.record_evaluation(case, evaluation(3, tests="failed"), "host")
        self.assertEqual(self.promote(case, 3)["decision"]["reason"], "candidate_check_failed:tests")
        views = {view["id"]: view for view in self.session.candidates(case)}
        self.assertEqual(views["c3"]["parents"], ["c1", "c2"])
        self.assertEqual(views["c3"]["evaluation"]["state"], "failed")
        self.assertEqual(views["c1"]["evaluation"]["state"], "passed")

    def test_latest_evaluation_of_the_same_tree_decides(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        self.session.record_evaluation(case, evaluation(1, tests="timed_out"), "host")
        self.assertEqual(self.promote(case, 1)["decision"]["reason"], "candidate_check_failed:tests")
        self.session.record_evaluation(case, evaluation(1), "host")
        self.assertEqual(self.promote(case, 1)["decision"]["status"], "allowed")

    def test_evidence_goes_stale(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        proposal = self.promote(case, 1)
        self.assertEqual(proposal["decision"]["status"], "allowed")
        self.clock.now += 601
        committed = self.session.commit(case, proposal["proposal"]["id"], "developer")
        self.assertEqual(committed["decision"]["reason"], "candidate_evidence_stale")
        self.assertEqual(self.session.candidates(case)[0]["promotion"]["state"], "denied")

    def test_scope_and_limits_block_promotion_even_when_checks_pass(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1, paths=("src/app.py", "pyproject.toml")), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        self.assertEqual(self.promote(case, 1)["decision"]["reason"], "candidate_out_of_scope")
        self.session.record_candidate(case, candidate(2, bytes_=50_000), "host")
        self.session.record_evaluation(case, evaluation(2), "host")
        self.assertEqual(self.promote(case, 2)["decision"]["reason"], "candidate_out_of_scope")

    def test_arguments_must_name_the_registered_candidate(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        swapped = self.session.propose(case, "promote", {**promote_args(1), "commit": oid(2)}, "developer", ["developer"])
        self.assertEqual(swapped["decision"]["reason"], "candidate_mismatch")
        extra = self.session.propose(case, "promote", {**promote_args(1), "ref": "refs/heads/main"}, "developer", ["developer"])
        self.assertEqual(extra["decision"]["reason"], "candidate_args_invalid")

    def test_required_approval_is_independent_and_human(self):
        case = self.open(contract(approval="maintainer"),
                         {"version": 1, "actions": {"promote": {"roles": ["developer"]}}})
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        proposal = self.promote(case, 1)
        self.assertEqual(proposal["decision"]["reason"], "approval_required:maintainer")
        self.assertEqual(self.session.candidates(case)[0]["promotion"]["state"], "needs_approval")
        pid = proposal["proposal"]["id"]
        with self.assertRaises(Rejected):
            self.session.approve(case, pid, "developer", ["developer", "maintainer"])
        self.session.approve(case, pid, "lead", ["maintainer"])
        self.assertEqual(self.session.commit(case, pid, "developer")["status"], "succeeded")

    def test_policy_change_between_proposal_and_commit(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        proposal = self.promote(case, 1)
        self.session.install_policy({"version": 2, "actions": {}})
        committed = self.session.commit(case, proposal["proposal"]["id"], "developer")
        self.assertEqual(committed["decision"]["reason"], "action_not_in_global_policy")
        self.assertFalse(self.session.candidates(case)[0]["authoritative"])

    def test_replay_against_a_stricter_contract(self):
        case = self.open()
        self.session.record_candidate(case, candidate(1), "host")
        self.session.record_evaluation(case, evaluation(1), "host")
        self.promote(case, 1)
        stricter = contract(version=2)
        stricter["evolution"]["evaluation"]["required"] = ["tests", "lint"]
        self.session.install_contract(stricter)
        replayed = self.session.replay(case, 2, 1)
        self.assertEqual(replayed["changed_decisions"][0]["after"]["reason"], "candidate_check_missing:lint")

    def test_events_match_the_protocol_schemas(self):
        case = self.open()
        self.session.record_search(case, "started", {
            "id": "s1", "provider": {"name": "fixture", "revision": "1"}, "operators": ["patch"],
            "budget": {"max_candidates": 2, "max_evaluations": 2, "max_seconds": 10}, "base": BASE,
            "context_digest": None}, "host")
        self.session.record_candidate(case, candidate(1, signal={"rationale": "smallest diff"}), "host")
        self.session.record_evaluation(case, evaluation(1, lint="failed"), "host")
        self.session.record_search(case, "finished", {
            "id": "s1", "stopped": "completed", "candidates": ["c1"], "recommended": ["c1"],
            "evaluations": 1, "elapsed_ms": 12, "error": None}, "host")
        proposal = self.promote(case, 1)
        self.session.commit(case, proposal["proposal"]["id"], "developer")
        self.assertEqual(validate_events(self.session.inspect(case)["events"], "evolution"), [])
        with self.assertRaises(Rejected):
            self.session.record_search(case, "finished", {
                "id": "s1", "stopped": "completed", "candidates": [], "recommended": [],
                "evaluations": 0, "elapsed_ms": 0, "error": None}, "host")

    def test_work_fails_closed_without_candidate_records(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            work.install_contract(contract())
            work.install_policy(POLICY)
            work.install_action("promote", {"kind": "local"})
            case = work.create_case("repo-change", 1, {"objective": "x"}, "developer")
            result = work.propose(case, "promote", promote_args(1), "developer", ["developer"])
            self.assertEqual(result["decision"]["reason"], "candidate_unknown")
            work.close()


if __name__ == "__main__":
    unittest.main()
