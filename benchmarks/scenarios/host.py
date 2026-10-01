"""Hosts the same scenario can drive.

SessionHost is syberlabs.Session. The others are architectural stand-ins.
They are not executions of named products.
"""

from __future__ import annotations

from syberlabs import Rejected, Session
from syberlabs.planner import StaticPlanner

from benchmarks.scenarios.world import Clock, Ledger


class SessionHost:
    name = "syberwork"
    kind = "syberlabs.Session"

    def __init__(self) -> None:
        self.clock = Clock()
        self.session = Session(clock=self.clock)
        self.world: Ledger | None = None
        self.case_id: str | None = None
        self.last_id: str | None = None
        self.ids: dict[str, str] = {}
        self.decisions: list[dict] = []
        self.effect_started_before_review = None
        self.unknown_status = None
        self.unknown_succeeded = None
        self.unknown_as_success = 0
        self.chain_valid = None
        self.complete = None
    def bind_world(self, world: Ledger) -> None:
        self.world = world

    def arm(self, *, stale: dict, live: dict) -> None:
        return None

    def install(self, contract: dict, policy: dict) -> None:
        self.session.install_contract(contract)
        self.session.install_policy(policy)
        self.session.install_action("record_review", {"kind": "local", "title": "Review invoice"})
        self.session.install_action("pay_vendor", {"kind": "local", "title": "Pay vendor", "effect": "bank"})

    def create_case(self, contract_id: str, inputs: dict, actor: str) -> str:
        self.case_id = self.session.create_case(contract_id, 1, inputs, actor)
        return self.case_id

    def observe(self, key: str, value: dict, source: str, version: str, actor: str, *, verified: bool) -> None:
        self.session.observe(self.case_id, key, value, source, version, actor, verified=verified)

    def jump(self, seconds: float) -> None:
        self.clock.jump(seconds)

    def propose(self, action: str, args: dict, actor: str, roles: list[str]) -> dict:
        result = self.session.propose(self.case_id, action, args, actor, roles)
        self._remember(action, result["proposal"]["id"])
        return result

    def suggest(self, action: str, args: dict) -> dict:
        result = self.session.suggest(
            self.case_id, "planner", ["model", "operator"], StaticPlanner(action, args),
        )
        self._remember(action, result["proposal"]["id"])
        return result

    def explain_last(self, expected_reason: str) -> dict:
        found = self.session.explain_admission(self.case_id, self.last_id)
        self.decisions.append({"reason": found["reason"], "rule": found["rule"], "expected": expected_reason})
        return found

    def checkpoint(self) -> None:
        history = self.session.history(self.case_id)
        self.effect_started_before_review = sum(1 for event in history if event["kind"] == "effect_started")

    def commit(self, slot: str, actor: str) -> dict:
        result = self.session.commit(self.case_id, self.ids[slot], actor)
        if slot == "pay":
            self.unknown_status = result.get("status")
            proposal_id = self.ids["pay"]
            self.unknown_succeeded = sum(
                1 for event in self.session.history(self.case_id)
                if event["kind"] == "effect_succeeded" and event["body"].get("proposal_id") == proposal_id
            )
        return result

    def approve(self, slot: str, actor: str, roles: list[str]) -> str | None:
        try:
            self.session.approve(self.case_id, self.ids[slot], actor, roles)
        except Rejected as error:
            self.decisions.append({"code": error.code, "rule": "approval.independent", "reason": None})
            return error.code
        return None

    def bind_bank(self, invoice: dict) -> None:
        world = self.world

        class Bank:
            def apply(self, case_id, args, key):
                world.pay(invoice, key)

            def status(self, case_id, args, key):
                receipt = world.payments[key]
                return "applied", {"external_id": receipt["external_id"]}

        self.session.bind_effect("pay_vendor", Bank())

    def reconcile(self, slot: str, actor: str, roles: list[str]) -> dict:
        return self.session.reconcile(self.case_id, self.ids[slot], actor, roles)

    def signoff(self, actor: str, roles: list[str], role: str) -> None:
        self.session.signoff(self.case_id, actor, roles, role)

    def finish(self) -> None:
        inspected = self.session.inspect(self.case_id)
        self.chain_valid = inspected["chain_valid"]
        self.complete = inspected["complete"]

    def report(self) -> dict:
        snap = self.world.snapshot()
        return {
            "name": self.name,
            "kind": self.kind,
            "decisions": list(self.decisions),
            "rules": [item["rule"] for item in self.decisions if item.get("rule")],
            "effect_started_before_review": self.effect_started_before_review,
            "unknown_status": self.unknown_status,
            "unknown_succeeded": self.unknown_succeeded,
            "unknown_as_success": self.unknown_as_success,
            "chain_valid": self.chain_valid,
            "complete": self.complete,
            **snap,
            "unsafe_payments": snap["invoices"].count("INV-STALE"),
        }

    def _remember(self, action: str, proposal_id: str) -> None:
        self.last_id = proposal_id
        self.ids[action] = proposal_id


class _StandIn:
    kind = "stand-in"
    name = "stand-in"

    def __init__(self) -> None:
        self.world: Ledger | None = None
        self.decisions: list[dict] = []
        self.effect_started_before_review = 0
        self.unknown_status = None
        self.unknown_succeeded = 0
        self.unknown_as_success = 0
        self.chain_valid = None
        self.complete = None
        self._review_done = False
        self.ids = {"review": "review", "pay": "pay", "record_review": "review", "pay_vendor": "pay"}
        self.last_id = "review"
        self.stale = None
        self.live = None

    def bind_world(self, world: Ledger) -> None:
        self.world = world

    def arm(self, *, stale: dict, live: dict) -> None:
        self.stale = stale
        self.live = live

    def install(self, contract: dict, policy: dict) -> None:
        return None

    def create_case(self, contract_id: str, inputs: dict, actor: str) -> str:
        return "stand-in"

    def observe(self, key: str, value: dict, source: str, version: str, actor: str, *, verified: bool) -> None:
        return None

    def jump(self, seconds: float) -> None:
        return None

    def propose(self, action: str, args: dict, actor: str, roles: list[str]) -> dict:
        self._on_propose(action, args, roles)
        self.last_id = self.ids.get(action, "review")
        return {"proposal": {"id": self.last_id}, "decision": {"status": "allowed", "reason": "stand_in"}}

    def suggest(self, action: str, args: dict) -> dict:
        return self.propose(action, args, "planner", ["model", "operator"])

    def explain_last(self, expected_reason: str) -> dict:
        return {"reason": None, "rule": None, "expected": expected_reason}

    def checkpoint(self) -> None:
        return None

    def commit(self, slot: str, actor: str) -> dict:
        if slot == "review":
            self._review_done = True
            return {"status": "succeeded"}
        return self._on_pay()

    def approve(self, slot: str, actor: str, roles: list[str]) -> str | None:
        return None

    def bind_bank(self, invoice: dict) -> None:
        return None

    def reconcile(self, slot: str, actor: str, roles: list[str]) -> dict:
        return {"status": "skipped"}

    def signoff(self, actor: str, roles: list[str], role: str) -> None:
        return None

    def finish(self) -> None:
        return None

    def report(self) -> dict:
        snap = self.world.snapshot()
        return {
            "name": self.name,
            "kind": self.kind,
            "decisions": list(self.decisions),
            "rules": [],
            "effect_started_before_review": self.effect_started_before_review,
            "unknown_status": self.unknown_status,
            "unknown_succeeded": self.unknown_succeeded,
            "unknown_as_success": self.unknown_as_success,
            "chain_valid": self.chain_valid,
            "complete": self.complete,
            **snap,
            "unsafe_payments": snap["invoices"].count("INV-STALE"),
        }

    def _on_propose(self, action: str, args: dict, roles: list[str]) -> None:
        return None

    def _on_pay(self) -> dict:
        return {"status": "succeeded"}

    def _pay_once(self, invoice: dict, key: str) -> None:
        if self.world.payments:
            return
        try:
            self.world.pay(invoice, key)
        except TimeoutError:
            pass
        self.effect_started_before_review = 1


class DirectToolHost(_StandIn):
    name = "direct_tool"
    kind = "stand-in: model tool executes"

    def _on_propose(self, action: str, args: dict, roles: list[str]) -> None:
        self._pay_once(self.stale, "model-tool")


class RoleGateHost(_StandIn):
    name = "role_gate"
    kind = "stand-in: role permit is the write"

    def _on_propose(self, action: str, args: dict, roles: list[str]) -> None:
        if "operator" in roles:
            self._pay_once(self.stale, "role-gate")


class RetryWorkflowHost(_StandIn):
    name = "retry_workflow"
    kind = "stand-in: timeout retried as success"

    def _on_pay(self) -> dict:
        try:
            self.world.pay(self.live, "attempt-1")
        except TimeoutError:
            self.unknown_as_success = 1
        self.world.pay(self.live, "attempt-2")
        self.unknown_status = "succeeded"
        self.unknown_succeeded = 1
        return {"status": "succeeded"}
