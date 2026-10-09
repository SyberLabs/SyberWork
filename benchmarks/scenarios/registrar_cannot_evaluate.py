"""The host that registered a candidate tries to grade it."""

from __future__ import annotations

from syberlabs import Rejected

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.support import candidate, evaluation, evolution_contract
from benchmarks.scenarios.world import Ledger


class RegistrarCannotEvaluate:
    id = "registrar_cannot_evaluate"
    expected_failures: list[str] = []

    def world(self):
        return Ledger()

    def contract(self):
        return evolution_contract()

    def policy(self):
        return {"version": 1, "actions": {"promote": {"roles": ["developer"]}}}

    def play(self, host, world):
        work = host.open_work()
        work.install_contract(self.contract())
        work.install_policy(self.policy())
        work.install_action("promote", {"kind": "local"})
        case_id = work.create_case("repo-change", 1, {"objective": "Add CSV export"}, "developer")
        work.record_candidate(case_id, candidate(1), "search-host", ["search"])
        try:
            work.record_evaluation(case_id, evaluation(1), "search-host", ["evaluator"])
        except Rejected as error:
            host.note(code=error.code, rule="evaluation.actor")
            return
        host.note(code="evaluated", rule="evaluation.actor")

    def probes(self):
        return [Probe("registrar_blocked", code="evaluation_denied")]


SCENARIO = RegistrarCannotEvaluate()
