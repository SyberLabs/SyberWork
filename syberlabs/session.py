"""In-memory case session for a project that does not use SyberWork's database.

Local actions only. Admission is the only path to an effect. The hash chain
uses the same digest as Work. This module does not import syberwork.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from syberlabs.admission import AdmissionContext, admit, approval_roles, explain
from syberlabs.canonical import canonical, digest
from syberlabs.contracts import prepare_contract
from syberlabs.economic import policy_has_economic, validate_policy_budgets
from syberlabs.errors import Rejected
from syberlabs.events import event_digest, verify_events
from syberlabs.evidence import acceptance_results
from syberlabs.planner import HttpPlanner


class Session:
    def __init__(self, *, clock: Callable[[], float] | None = None):
        self.clock = clock or time.time
        self.contracts: dict[tuple[str, int], dict] = {}
        self.policies: dict[int, dict] = {}
        self.actions: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}

    def install_contract(self, doc: dict) -> None:
        prepared = prepare_contract(doc)
        key = (prepared["id"], prepared["version"])
        stored = json.loads(canonical(prepared))
        prior = self.contracts.get(key)
        if prior is not None and prior != stored:
            raise Rejected("immutable_contract", "publish a new version")
        self.contracts[key] = stored

    def install_policy(self, doc: dict) -> None:
        if not isinstance(doc.get("version"), int) or not isinstance(doc.get("actions"), dict):
            raise Rejected("invalid_policy", "version and actions required")
        version = doc["version"]
        stored = json.loads(canonical(doc))
        prior = self.policies.get(version)
        if prior is not None and prior != stored:
            raise Rejected("immutable_policy", "publish a new version")
        if self.policies and version < max(self.policies) and prior is None:
            raise Rejected("policy_version", "new policy version must advance")
        if prior is None and policy_has_economic(doc):
            validate_policy_budgets(doc, list(self.policies.values()))
        if prior is None:
            self.policies[version] = stored

    def install_action(self, name: str, doc: dict) -> None:
        if doc.get("kind") != "local":
            raise Rejected("invalid_action", "the in-memory session executes local actions")
        stored = json.loads(canonical(doc))
        prior = self.actions.get(name)
        if prior is not None and prior != stored:
            raise Rejected("immutable_action", "action definitions cannot change while cases exist; use a new name")
        if prior is None:
            self.actions[name] = stored

    def create_case(self, contract_id: str, version: int, inputs: dict, actor: str) -> str:
        contract = self._contract(contract_id, version)
        if set(inputs) != set(contract["inputs"]):
            raise Rejected("input_schema", "input keys must exactly match contract")
        for key, kind in contract["inputs"].items():
            value = inputs[key]
            if kind == "string" and not isinstance(value, str) or kind == "integer" and (type(value) is not int):
                raise Rejected("input_schema", f"invalid {key}: expected {kind}")
        case_id = str(uuid.uuid4())
        self.cases[case_id] = {
            "id": case_id,
            "contract_id": contract_id,
            "contract_version": version,
            "inputs": inputs,
            "created": self.clock(),
        }
        self.events[case_id] = []
        self._append(case_id, "case_created", {"actor": actor, "inputs": inputs, "contract": [contract_id, version]})
        return case_id

    def observe(self, case_id: str, key: str, value: Any, source: str, version: str, actor: str, verified: bool = False) -> dict:
        if not all((key, source, version)):
            raise Rejected("invalid_observation", "key, source and source version are required")
        self._case(case_id)
        return self._append(case_id, "observed", {
            "key": key, "value": value, "source": source, "version": version, "actor": actor, "verified": verified,
        })

    def propose(self, case_id: str, action: str, args: dict, actor: str, roles: list[str], origin: str = "human") -> dict:
        if origin not in ("human", "model", "compiled"):
            raise Rejected("invalid_origin", "origin must be human, model or compiled")
        if origin != "human" and origin not in roles:
            raise Rejected("origin_denied", "the actor cannot claim this proposer origin")
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        policy = self._policy()
        history = list(self.events[case_id])
        proposal_id = str(uuid.uuid4())
        proposal = {"id": proposal_id, "action": action, "args": args, "actor": actor, "roles": roles, "origin": origin}
        self._append(case_id, "proposed", proposal)
        decision = self._public_decision(contract, policy, history, proposal, self.clock())
        self._append(case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], **decision})
        return {"proposal": proposal, "decision": decision}

    def suggest(self, case_id: str, actor: str, roles: list[str], planner) -> dict:
        if "model" not in roles:
            raise Rejected("origin_denied", "model proposer credential required")
        if isinstance(planner, HttpPlanner):
            planner.ensure_configured()
        case = self.inspect(case_id)
        suggestion = planner.propose({
            "objective": case["contract"].get("title", case["contract"]["id"]),
            "allowed_actions": list(case["contract"]["actions"]),
            "contract": case["contract"],
            "events": case["events"],
            "acceptance": case["acceptance"],
        })
        if not isinstance(suggestion, dict) or not isinstance(suggestion.get("action"), str) or not isinstance(suggestion.get("args"), dict):
            raise Rejected("planner_shape", "planner must return {action, args}")
        return self.propose(case_id, suggestion["action"], suggestion["args"], actor, roles, origin="model")

    def approve(self, case_id: str, proposal_id: str, actor: str, roles: list[str]) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        policy = self._policy()
        history = self.events[case_id]
        proposal = self._proposal(history, proposal_id)
        required_roles = approval_roles(contract, policy, proposal["action"])
        selected_role = next((role for role in required_roles if role in roles and not any(
            event["kind"] == "approved" and event["body"]["proposal_id"] == proposal_id and event["body"]["role"] == role
            for event in history
        )), None)
        if not selected_role or actor == proposal["actor"]:
            raise Rejected("approval_denied", "independent authorized approver required")
        return self._append(case_id, "approved", {
            "proposal_id": proposal_id, "actor": actor, "role": selected_role, "args_hash": digest(proposal["args"]),
        })

    def commit(self, case_id: str, proposal_id: str, actor: str) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        policy = self._policy()
        history = list(self.events[case_id])
        proposal = self._proposal(history, proposal_id)
        if actor != proposal["actor"]:
            raise Rejected("actor_mismatch", "only the original proposer may commit")
        if any(event["kind"] in ("effect_started", "effect_succeeded", "effect_unknown", "effect_rejected") and event["body"]["proposal_id"] == proposal_id for event in history):
            raise Rejected("effect_claimed", "already started; inspect or reconcile")
        decision = self._public_decision(contract, policy, history, proposal, self.clock())
        if decision["status"] != "allowed":
            self._append(case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], "phase": "commit", **decision})
            return {"decision": decision}
        action = self.actions.get(proposal["action"])
        if not action or action.get("kind") != "local":
            raise Rejected("unsupported_effect", "the in-memory session executes local actions")
        claim = self._append(case_id, "effect_started", {
            "proposal_id": proposal_id, "action": proposal["action"], "actor": actor,
            "policy_version": policy["version"], "idempotency_key": proposal_id,
        })
        output = {"recorded": True, "args": proposal["args"]}
        result = self._append(case_id, "effect_succeeded", {
            "proposal_id": proposal_id, "action": proposal["action"], "output": output, "claim_hash": claim["hash"],
        })
        return {"status": "succeeded", "event": result}

    def signoff(self, case_id: str, actor: str, roles: list[str], role: str) -> dict:
        if role not in roles:
            raise Rejected("signoff_denied", "actor lacks role")
        self._case(case_id)
        if any(event["kind"] == "case_cancelled" for event in self.events[case_id]):
            raise Rejected("case_cancelled", "case is closed")
        return self._append(case_id, "signed", {"actor": actor, "role": role})

    def explain_admission(self, case_id: str, proposal_id: str) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        policy = self._policy()
        history = list(self.events[case_id])
        proposal = self._proposal(history, proposal_id)
        before = len(history)
        found = explain(self._context(contract, policy, history, proposal, self.clock()))
        if len(self.events[case_id]) != before:
            raise RuntimeError("explain_admission appended an event")
        return found

    def inspect(self, case_id: str) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        history = list(self.events[case_id])
        clauses = acceptance_results(contract, history)
        cancelled = any(event["kind"] == "case_cancelled" for event in history)
        complete = not cancelled and bool(clauses) and all(item["passed"] for item in clauses)
        return {
            "case": dict(row),
            "contract": contract,
            "events": history,
            "acceptance": clauses,
            "complete": complete,
            "status": "cancelled" if cancelled else "complete" if complete else "in_progress",
            "chain_valid": verify_events(history),
        }

    def replay(self, case_id: str, contract_version: int, policy_version: int) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], contract_version)
        policy = self._policy(policy_version)
        events = list(self.events[case_id])
        changed = []
        for index, event in enumerate(events):
            if event["kind"] != "proposed":
                continue
            proposal = event["body"]
            original = next((item["body"] for item in events[index + 1:] if item["kind"] == "decision" and item["body"]["proposal_id"] == proposal["id"]), None)
            updated = self._public_decision(contract, policy, events[:index], proposal, event["at"])
            if original and (original["status"], original.get("reason")) != (updated["status"], updated.get("reason")):
                changed.append({"proposal_id": proposal["id"], "action": proposal["action"], "before": original, "after": updated})
        original_contract = self._contract(row["contract_id"], row["contract_version"])
        return {
            "case_id": case_id,
            "contract_version": contract_version,
            "policy_version": policy_version,
            "changed_decisions": changed,
            "acceptance_before": acceptance_results(original_contract, events),
            "acceptance_after": acceptance_results(contract, events),
        }

    def verify_chain(self, case_id: str) -> bool:
        self._case(case_id)
        return verify_events(self.events[case_id])

    def _context(self, contract, policy, history, proposal, now) -> AdmissionContext:
        return AdmissionContext(
            contract, policy, history, proposal, now, set(self.actions),
            action_configs=dict(self.actions), budget_reserved=None,
        )

    def _public_decision(self, contract, policy, history, proposal, now) -> dict:
        decision = admit(self._context(contract, policy, history, proposal, now))
        return {"status": decision["status"], "reason": decision["reason"]}

    def _contract(self, contract_id: str, version: int) -> dict:
        found = self.contracts.get((contract_id, version))
        if not found:
            raise Rejected("unknown_contract", f"{contract_id}@{version}")
        return found

    def _policy(self, version: int | None = None) -> dict:
        if version is None:
            if not self.policies:
                raise Rejected("unknown_policy", "install policy first")
            version = max(self.policies)
        found = self.policies.get(version)
        if not found:
            raise Rejected("unknown_policy", "install policy first")
        return found

    def _case(self, case_id: str) -> dict:
        found = self.cases.get(case_id)
        if not found:
            raise Rejected("unknown_case", case_id)
        return found

    def _proposal(self, history, proposal_id: str) -> dict:
        for event in history:
            if event["kind"] == "proposed" and event["body"]["id"] == proposal_id:
                return event["body"]
        raise Rejected("unknown_proposal", proposal_id)

    def _append(self, case_id: str, kind: str, body: dict) -> dict:
        previous = self.events[case_id][-1] if self.events[case_id] else None
        event = {
            "case_id": case_id,
            "seq": previous["seq"] + 1 if previous else 1,
            "kind": kind,
            "body": body,
            "at": self.clock(),
            "previous": previous["hash"] if previous else "0" * 64,
        }
        event["hash"] = event_digest(event)
        self.events[case_id].append(event)
        return event
