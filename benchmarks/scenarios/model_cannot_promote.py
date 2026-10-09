"""A reviewed candidate still cannot be promoted by the model that searched for it."""

from __future__ import annotations

from syberlabs import Rejected

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.support import candidate, evaluation, evolution_contract
from benchmarks.scenarios.world import Ledger


class ModelCannotPromote:
    id = "model_cannot_promote"
    expected_failures: list[str] = []

    def world(self):
        return Ledger()

    def contract(self):
        return evolution_contract()

    def policy(self):
        return {"version": 1, "actions": {"promote": {"roles": ["developer", "model"]}}}

    def play(self, host, world):
        work = host.open_work()
        body = candidate(1)
        work.install_contract(self.contract())
        work.install_policy(self.policy())
        work.install_action("promote", {"kind": "local"})
        case_id = work.create_case("repo-change", 1, {"objective": "Add CSV export"}, "developer")
        work.record_candidate(case_id, body, "search-host", ["search"])
        work.record_evaluation(case_id, evaluation(1), "ci", ["evaluator"])
        try:
            result = work.propose(
                case_id, "promote",
                {"candidate": body["id"], "commit": body["commit"], "base": body["base"]},
                "planner", ["developer", "model"], origin="model",
            )
        except Rejected as error:
            host.note(code=error.code, rule="candidate.promotable")
            return
        host.note(reason=result["decision"]["reason"], rule="candidate.promotable")

    def probes(self):
        return [Probe("model_promotion_refused", reason="candidate_promotion_origin")]


SCENARIO = ModelCannotPromote()
