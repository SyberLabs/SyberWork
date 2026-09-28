import json
import tempfile
import unittest
from pathlib import Path

from syberlabs.admission import (
    ECONOMIC_INSERT_AFTER,
    RULES,
    AdmissionContext,
    admit,
    rule_order,
    rule_provenance,
)
from syberwork.core import Work


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

EXPECTED_ORDER = [
    "case.not_cancelled",
    "proposal.shape",
    "contract.action_listed",
    "policy.action_listed",
    "action.installed",
    "actor.role",
    "resolution.current",
    "effect.not_completed",
    "effect.unresolved",
    "limits.amount",
    "effect.prior",
    "facts.required",
    "economic.reserve",
    "bindings.inputs",
    "bindings.arguments",
    "candidate.promotable",
    "approval.required",
    "admission.passed",
]


def proposal(action="review", args=None, roles=None, proposal_id="p1"):
    return {
        "id": proposal_id,
        "action": action,
        "args": {} if args is None else args,
        "actor": "operator",
        "roles": ["operator"] if roles is None else roles,
        "origin": "human",
    }


def context(history=None, proposal_doc=None, contract_actions=None, policy_actions=None,
            installed=None, now=1_000_000.0, resolutions=None, bindings=None):
    return AdmissionContext(
        contract={
            "actions": {"review": {}} if contract_actions is None else contract_actions,
            "resolutions": {} if resolutions is None else resolutions,
            "input_bindings": {} if bindings is None else bindings,
        },
        policy={"actions": {"review": {"roles": ["operator"]}} if policy_actions is None else policy_actions},
        history=[] if history is None else history,
        proposal=proposal() if proposal_doc is None else proposal_doc,
        now=now,
        installed_actions={"review"} if installed is None else installed,
    )


COMMIT, TREE, BASE = "a" * 40, "b" * 40, "c" * 40


def promotion_cases():
    """One context per candidate.promotable reason. Evaluations are host events."""
    evolution = {
        "scope": {"paths": ["src/"]},
        "operators": ["patch"],
        "budget": {"max_candidates": 4},
        "evaluation": {"checks": {"tests": {"argv": ["true"]}, "lint": {"argv": ["true"]}},
                       "required": ["tests", "lint"], "max_age_seconds": 60},
        "promotion": {"action": "promote", "roles": ["developer"]},
    }

    def candidate(**extra):
        return {"kind": "candidate_registered", "at": 1_000_000.0, "hash": "h", "body": {
            "id": "c1", "commit": COMMIT, "tree": TREE, "base": BASE, "parents": [], "operator": "patch",
            "provider": {"name": "p", "revision": "1"}, "changed_paths": ["src/a.py"], "scope_violations": [],
            "limit_violations": [], "diff": {"digest": "d" * 64, "bytes": 1, "files": 1}, "signal": None,
            "note": "", "actor": "host", **extra}}

    def evaluation(at=1_000_000.0, **states):
        return {"kind": "candidate_evaluated", "at": at, "hash": "e", "body": {
            "candidate": "c1", "commit": COMMIT, "tree": TREE, "evaluator": "host",
            "checks": [{"name": name, "state": state, "exit_code": 0 if state == "passed" else 1, "duration_ms": 1,
                        "output_digest": "0" * 64, "output_tail": ""} for name, state in states.items()]}}

    def ctx(history=(), args=None, roles=("developer",), origin="human"):
        doc = proposal("promote", {"candidate": "c1", "commit": COMMIT, "base": BASE} if args is None else args,
                       list(roles))
        doc["origin"] = origin
        found = context(history=list(history), proposal_doc=doc,
                        contract_actions={"promote": {}}, policy_actions={"promote": {"roles": ["developer", "model", "reviewer"]}},
                        installed={"promote"})
        found.contract["evolution"] = evolution
        return found

    return {
        "candidate_promotion_origin": ctx(roles=("developer", "model"), origin="model"),
        "candidate_promotion_role": ctx(roles=("reviewer",)),
        "candidate_args_invalid": ctx(args={"candidate": "c1"}),
        "candidate_unknown": ctx(),
        "candidate_mismatch": ctx([candidate(commit="f" * 40)]),
        "candidate_out_of_scope": ctx([candidate(scope_violations=["setup.py"])]),
        "candidate_not_evaluated": ctx([candidate()]),
        "candidate_check_missing:lint": ctx([candidate(), evaluation(tests="passed")]),
        "candidate_check_failed:tests": ctx([candidate(), evaluation(tests="failed", lint="passed")]),
        "candidate_evidence_stale": ctx([candidate(), evaluation(at=0.0, tests="passed", lint="passed")]),
    }


class AdmissionRegistry(unittest.TestCase):
    def test_registry_order(self):
        self.assertEqual(rule_order(), EXPECTED_ORDER)
        self.assertEqual(EXPECTED_ORDER.index("facts.required") + 1, EXPECTED_ORDER.index("economic.reserve"))
        self.assertEqual(EXPECTED_ORDER.index("economic.reserve") + 1, EXPECTED_ORDER.index("bindings.inputs"))
        self.assertEqual(ECONOMIC_INSERT_AFTER, "facts.required")

    def test_each_reason_code_has_one_rule(self):
        owners = {}
        for name, fn in RULES:
            for reason in fn.reasons:
                self.assertNotIn(reason, owners, reason)
                owners[reason] = name
        cases = {
            "case_cancelled": context(history=[{"kind": "case_cancelled", "body": {}}], proposal_doc=proposal(args=[])),
            "invalid_proposal_shape": context(proposal_doc=proposal(args=[])),
            "action_not_in_contract": context(proposal_doc=proposal("missing")),
            "action_not_in_global_policy": context(policy_actions={}),
            "action_not_installed": context(installed=set()),
            "actor_role_missing": context(proposal_doc=proposal(roles=["guest"])),
            "resolution_open:site": context(
                resolutions={"site": {"blocks_actions": ["review"], "result": {"key": "request", "source": "requisitions", "value_path": "site_id", "identity_path": "id"}}},
                history=[{"kind": "resolution_requested", "body": {"id": "t1", "key": "site", "record_key": "REQ"}}],
            ),
            "resolution_changed:site": context(
                resolutions={"site": {"blocks_actions": ["review"], "result": {"key": "request", "source": "requisitions", "value_path": "site_id", "identity_path": "id"}}},
                history=[
                    {"kind": "resolution_requested", "body": {"id": "t1", "key": "site", "record_key": "REQ"}},
                    {"kind": "resolution_completed", "body": {"task_id": "t1", "choice": "DC"}},
                ],
            ),
            "action_already_completed": context(history=[{"kind": "effect_succeeded", "body": {"action": "review"}}]),
            "effect_unresolved:review": context(history=[{"kind": "effect_unknown", "body": {"action": "review", "proposal_id": "old"}}]),
            "amount_required": context(policy_actions={"review": {"roles": ["operator"], "max_amount": 10}}),
            "amount_exceeds_limit": context(
                policy_actions={"review": {"roles": ["operator"], "max_amount": 10}},
                proposal_doc=proposal(args={"amount": 11}),
            ),
            "required_prior_effect_missing": context(contract_actions={"review": {"requires_effect": "other"}}),
            "missing_fact:part": context(contract_actions={"review": {"required_facts": [{"key": "part", "source": "inventory"}]}}),
            "untrusted_fact_source:part": context(
                contract_actions={"review": {"required_facts": [{"key": "part", "source": "inventory"}]}},
                history=[{"kind": "observed", "at": 1_000_000.0, "body": {"key": "part", "source": "user", "value": "P", "verified": True}}],
            ),
            "source_verification_required:part": context(
                contract_actions={"review": {"required_facts": [{"key": "part", "source": "inventory", "verified": True}]}},
                history=[{"kind": "observed", "at": 1_000_000.0, "body": {"key": "part", "source": "inventory", "value": "P", "verified": False}}],
            ),
            "stale_fact:part": context(
                contract_actions={"review": {"required_facts": [{"key": "part", "source": "inventory", "verified": True, "max_age_seconds": 10}]}},
                history=[{"kind": "observed", "at": 0.0, "body": {"key": "part", "source": "inventory", "value": "P", "verified": True}}],
            ),
            "input_provenance:part": context(
                bindings={"part": "fact:part"},
                history=[
                    {"kind": "case_created", "body": {"inputs": {"part": "P-104"}}},
                    {"kind": "observed", "at": 1_000_000.0, "body": {"key": "part", "value": "P-999", "source": "inventory", "verified": True}},
                ],
            ),
            "argument_provenance:part": context(
                contract_actions={"review": {"arguments": {"part": "fact:part"}}},
                proposal_doc=proposal(args={"part": "P-999"}),
                history=[{"kind": "observed", "at": 1_000_000.0, "body": {"key": "part", "value": "P-104", "source": "inventory", "verified": True}}],
            ),
            "approval_required:manager": context(policy_actions={"review": {"roles": ["operator"], "approval_role": "manager"}}),
            "economic_adapter_required": context(policy_actions={"review": {"roles": ["operator"], "economic": {"budget_id": "ops"}}}),
            **promotion_cases(),
            "all_checks_passed": context(),
        }
        seen = {}
        for reason, ctx in cases.items():
            result = admit(ctx)
            self.assertEqual(result["reason"], reason)
            template = reason if reason in owners else reason.split(":", 1)[0] + ":"
            self.assertIn(template, owners)
            self.assertEqual(result["rule"], owners[template])
            seen.setdefault(reason, set()).add(result["rule"])
        for reason, rules in seen.items():
            self.assertEqual(rules, {owners[reason if reason in owners else reason.split(":", 1)[0] + ":"]})

    def test_resolution_and_fact_checks_keep_inner_order(self):
        changed_then_open = context(
            resolutions={
                "site": {"blocks_actions": ["review"], "result": {"key": "request", "source": "requisitions", "value_path": "site_id", "identity_path": "id"}},
                "quote": {"blocks_actions": ["review"], "result": {"key": "quote", "source": "supplier", "value_path": "id", "identity_path": "request_id"}},
            },
            history=[
                {"kind": "resolution_requested", "body": {"id": "site-task", "key": "site", "record_key": "REQ"}},
                {"kind": "resolution_completed", "body": {"task_id": "site-task", "choice": "DC"}},
                {"kind": "resolution_requested", "body": {"id": "quote-task", "key": "quote", "record_key": "REQ"}},
            ],
        )
        self.assertEqual(admit(changed_then_open)["reason"], "resolution_changed:site")
        stale_then_missing = context(
            contract_actions={"review": {"required_facts": [
                {"key": "a", "source": "inventory", "verified": True, "max_age_seconds": 10},
                {"key": "b", "source": "inventory"},
            ]}},
            history=[{"kind": "observed", "at": 0.0, "body": {"key": "a", "source": "inventory", "value": "P", "verified": True}}],
        )
        self.assertEqual(admit(stale_then_missing)["reason"], "stale_fact:a")
        global_limit = context(
            contract_actions={"review": {"max_amount": 100}},
            policy_actions={"review": {"roles": ["operator"], "max_amount": 40}},
            proposal_doc=proposal(args={"amount": 50}),
        )
        self.assertEqual(admit(global_limit)["reason"], "amount_exceeds_limit")

    def test_provenance_is_inspection_not_a_stored_path(self):
        for name, fn in RULES:
            found = rule_provenance(name)
            self.assertEqual(found["rule"], name)
            self.assertEqual(found["symbol"], fn.__qualname__)
            self.assertEqual(found["module"], "syberlabs/admission.py")
            self.assertIsInstance(found["line"], int)
            self.assertGreater(found["line"], 0)

    def test_public_decision_omits_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            work.install_contract(json.loads((EXAMPLES / "contract.json").read_text()))
            work.install_policy(json.loads((EXAMPLES / "policy.json").read_text()))
            work.install_action("record_review", {"kind": "local"})
            work.install_action("issue_order", {"kind": "local"})
            case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
            result = work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
            self.assertEqual(set(result["decision"]), {"status", "reason"})
            decision = next(event for event in work.inspect(case)["events"] if event["kind"] == "decision")
            self.assertNotIn("rule", decision["body"])


if __name__ == "__main__":
    unittest.main()
