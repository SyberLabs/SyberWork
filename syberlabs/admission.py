"""Ordered admission rules. The first rule that returns a decision stops the chain."""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from syberlabs.canonical import digest
from syberlabs.clock import as_seconds
from syberlabs.economic import denial as economic_denial
from syberlabs.evidence import verified_reconciliation
from syberlabs.values import at_path


# economic.reserve sits after facts.required and before bindings.inputs.
# Its source check says freshness was already checked. Non-economic actions return None.
ECONOMIC_RULE = "economic.reserve"
ECONOMIC_INSERT_AFTER = "facts.required"

RULES: list[tuple[str, Callable]] = []


def rule(name: str, *reasons: str):
    def decorate(fn: Callable) -> Callable:
        fn.rule_name = name
        fn.reasons = reasons
        RULES.append((name, fn))
        return fn
    return decorate


def deny(reason: str, rule_name: str) -> dict:
    return {"status": "denied", "reason": reason, "rule": rule_name}


def latest_observation(history: list[dict], key: str) -> dict | None:
    return next((event for event in reversed(history) if event["kind"] == "observed" and event["body"]["key"] == key), None)


@dataclass
class AdmissionContext:
    contract: dict
    policy: dict
    history: list
    proposal: dict
    now: float
    installed_actions: Collection[str]
    action_configs: dict | None = None
    budget_reserved: Callable[[str, str], int] | None = None

    @property
    def action(self) -> Any:
        return self.proposal["action"]

    @property
    def args(self) -> Any:
        return self.proposal["args"]

    def local_action(self) -> dict:
        return self.contract["actions"][self.action]

    def global_action(self) -> dict:
        return self.policy["actions"][self.action]

    def action_config(self) -> dict:
        configs = self.action_configs or {}
        found = configs.get(self.action) if isinstance(configs, dict) else None
        return found if isinstance(found, dict) else {}


@rule("case.not_cancelled", "case_cancelled")
def case_not_cancelled(ctx: AdmissionContext) -> dict | None:
    if any(event["kind"] == "case_cancelled" for event in ctx.history):
        return deny("case_cancelled", "case.not_cancelled")
    return None


@rule("proposal.shape", "invalid_proposal_shape")
def proposal_shape(ctx: AdmissionContext) -> dict | None:
    if not isinstance(ctx.args, dict) or not isinstance(ctx.proposal.get("roles"), list):
        return deny("invalid_proposal_shape", "proposal.shape")
    return None


@rule("contract.action_listed", "action_not_in_contract")
def contract_action_listed(ctx: AdmissionContext) -> dict | None:
    if ctx.action not in ctx.contract["actions"]:
        return deny("action_not_in_contract", "contract.action_listed")
    return None


@rule("policy.action_listed", "action_not_in_global_policy")
def policy_action_listed(ctx: AdmissionContext) -> dict | None:
    if ctx.action not in ctx.policy["actions"]:
        return deny("action_not_in_global_policy", "policy.action_listed")
    return None


@rule("action.installed", "action_not_installed")
def action_installed(ctx: AdmissionContext) -> dict | None:
    if ctx.action not in ctx.installed_actions:
        return deny("action_not_installed", "action.installed")
    return None


@rule("actor.role", "actor_role_missing")
def actor_role(ctx: AdmissionContext) -> dict | None:
    if not set(ctx.global_action().get("roles", [])).intersection(ctx.proposal["roles"]):
        return deny("actor_role_missing", "actor.role")
    return None


@rule("resolution.current", "resolution_open:", "resolution_changed:")
def resolution_current(ctx: AdmissionContext) -> dict | None:
    for key, resolution in ctx.contract.get("resolutions", {}).items():
        if ctx.action not in resolution["blocks_actions"]:
            continue
        task = next((event for event in ctx.history if event["kind"] == "resolution_requested" and event["body"]["key"] == key), None)
        if not task:
            continue
        closed = next((event for event in ctx.history if event["kind"] == "resolution_completed" and event["body"]["task_id"] == task["body"]["id"]), None)
        if not closed:
            return deny("resolution_open:" + key, "resolution.current")
        result_spec = resolution["result"]
        latest = latest_observation(ctx.history, result_spec["key"])
        if (not latest or not latest["body"].get("verified") or latest["body"]["source"] != result_spec["source"] or
                at_path(latest["body"]["value"], result_spec["value_path"]) != closed["body"]["choice"] or
                at_path(latest["body"]["value"], result_spec["identity_path"]) != task["body"]["record_key"]):
            return deny("resolution_changed:" + key, "resolution.current")
    return None


@rule("effect.not_completed", "action_already_completed")
def effect_not_completed(ctx: AdmissionContext) -> dict | None:
    if any(event["kind"] == "effect_succeeded" and event["body"]["action"] == ctx.action or
           verified_reconciliation(event) and event["body"]["action"] == ctx.action for event in ctx.history):
        return deny("action_already_completed", "effect.not_completed")
    return None


def _finished_proposal_ids(history: list) -> set:
    """Proposals whose effect has succeeded, been rejected, or been verified."""
    finished = set()
    for event in history:
        kind = event.get("kind")
        body = event.get("body") or {}
        if kind in ("effect_succeeded", "effect_rejected") or verified_reconciliation(event):
            proposal_id = body.get("proposal_id")
            if proposal_id is not None:
                finished.add(proposal_id)
    return finished


@rule("effect.unresolved", "effect_unresolved:")
def effect_unresolved(ctx: AdmissionContext) -> dict | None:
    proposals = {event["body"]["id"]: event["body"]["action"] for event in ctx.history if event["kind"] == "proposed"}
    verified_ids = {event["body"]["proposal_id"] for event in ctx.history if verified_reconciliation(event)}
    for event in ctx.history:
        if event["kind"] != "effect_unknown":
            continue
        effect = event["body"]
        if effect.get("action", proposals.get(effect["proposal_id"])) == ctx.action and effect["proposal_id"] not in verified_ids:
            return deny("effect_unresolved:" + ctx.action, "effect.unresolved")
    # An effect_started with no terminal outcome occupies the action.
    # effect_rejected is terminal, so a fresh proposal of that action can proceed.
    finished = _finished_proposal_ids(ctx.history)
    for event in ctx.history:
        if event["kind"] != "effect_started":
            continue
        effect = event["body"]
        if effect.get("action") == ctx.action and effect.get("proposal_id") not in finished:
            return deny("effect_unresolved:" + ctx.action, "effect.unresolved")
    return None


@rule("limits.amount", "amount_required", "amount_exceeds_limit")
def limits_amount(ctx: AdmissionContext) -> dict | None:
    for spec in (ctx.global_action(), ctx.local_action()):
        if "max_amount" in spec:
            amount = ctx.args.get("amount")
            if type(amount) not in (int, float) or not math.isfinite(amount):
                return deny("amount_required", "limits.amount")
            if amount > spec["max_amount"] or amount < 0:
                return deny("amount_exceeds_limit", "limits.amount")
    return None


@rule("effect.prior", "required_prior_effect_missing")
def effect_prior(ctx: AdmissionContext) -> dict | None:
    required = ctx.local_action().get("requires_effect")
    if required and not any(event["kind"] == "effect_succeeded" and event["body"]["action"] == required or verified_reconciliation(event) and event["body"]["action"] == required for event in ctx.history):
        return deny("required_prior_effect_missing", "effect.prior")
    return None


@rule("facts.required", "missing_fact:", "untrusted_fact_source:", "source_verification_required:", "stale_fact:")
def facts_required(ctx: AdmissionContext) -> dict | None:
    for requirement in ctx.local_action().get("required_facts", []):
        fact = latest_observation(ctx.history, requirement["key"])
        if not fact:
            return deny("missing_fact:" + requirement["key"], "facts.required")
        if fact["body"]["source"] != requirement["source"]:
            return deny("untrusted_fact_source:" + requirement["key"], "facts.required")
        if requirement.get("verified") and not fact["body"].get("verified"):
            return deny("source_verification_required:" + requirement["key"], "facts.required")
        if as_seconds(ctx.now) - as_seconds(fact["at"]) > requirement.get("max_age_seconds", 86400):
            return deny("stale_fact:" + requirement["key"], "facts.required")
    return None


@rule(
    "economic.reserve",
    "economic_adapter_required",
    "economic_policy_missing",
    "economic_intent_invalid",
    "economic_amount_unbound",
    "economic_operation_denied",
    "economic_policy_mismatch",
    "economic_destination_denied",
    "economic_purpose_required",
    "economic_expired",
    "economic_verified_evidence_required",
    "economic_evidence_mismatch",
    "economic_policy_invalid",
    "economic_limit_exceeded",
    "economic_budget_exceeded",
)
def economic_reserve(ctx: AdmissionContext) -> dict | None:
    """No-op unless the action is economic_http or the policy action carries economic."""
    config = ctx.action_config()
    policy_action = ctx.global_action()
    if config.get("kind") != "economic_http":
        if isinstance(policy_action, dict) and "economic" in policy_action:
            return deny("economic_adapter_required", "economic.reserve")
        return None
    reason = economic_denial(
        config, policy_action, ctx.local_action(), ctx.history, ctx.args, ctx.now, ctx.budget_reserved,
    )
    if reason:
        return deny(reason, "economic.reserve")
    return None


@rule("bindings.inputs", "input_provenance:")
def bindings_inputs(ctx: AdmissionContext) -> dict | None:
    created = next((event for event in ctx.history if event["kind"] == "case_created"), None)
    for input_key, binding in ctx.contract.get("input_bindings", {}).items():
        fact_key, *path = binding[5:].split(".")
        fact = latest_observation(ctx.history, fact_key)
        value = fact["body"]["value"] if fact else None
        for component in path:
            value = value.get(component) if isinstance(value, dict) else None
        if not created or not fact or value is None or value != created["body"]["inputs"][input_key]:
            return deny("input_provenance:" + input_key, "bindings.inputs")
    return None


@rule("bindings.arguments", "argument_provenance:")
def bindings_arguments(ctx: AdmissionContext) -> dict | None:
    for param, binding in ctx.local_action().get("arguments", {}).items():
        if binding.startswith("version:"):
            name = binding[8:]
            fact = latest_observation(ctx.history, name)
            if not fact or ctx.args.get(param) != fact["body"]["version"]:
                return deny("argument_provenance:" + param, "bindings.arguments")
        elif binding.startswith("fact:"):
            name, *path = binding[5:].split(".")
            fact = latest_observation(ctx.history, name)
            expected = fact["body"]["value"] if fact else None
            for component in path:
                expected = expected.get(component) if isinstance(expected, dict) else None
            if not fact or expected is None or param not in ctx.args or ctx.args[param] != expected:
                return deny("argument_provenance:" + param, "bindings.arguments")
    return None


@rule("approval.required", "approval_required:")
def approval_required(ctx: AdmissionContext) -> dict | None:
    for role in approval_roles(ctx.contract, ctx.policy, ctx.action):
        if not any(event["kind"] == "approved" and event["body"].get("proposal_id") == ctx.proposal["id"] and event["body"]["role"] == role and event["body"].get("args_hash") == digest(ctx.args) for event in ctx.history):
            return {"status": "needs_approval", "reason": "approval_required:" + role, "rule": "approval.required"}
    return None


@rule("admission.passed", "all_checks_passed")
def admission_passed(ctx: AdmissionContext) -> dict | None:
    return {"status": "allowed", "reason": "all_checks_passed", "rule": "admission.passed"}


def approval_roles(contract, policy, action):
    roles = [policy["actions"].get(action, {}).get("approval_role"), contract["actions"].get(action, {}).get("approval_role")]
    return list(dict.fromkeys(role for role in roles if role))


def rule_order() -> list[str]:
    return [name for name, _fn in RULES]


_PROVENANCE: dict[str, dict] = {}


def rule_provenance(name: str) -> dict:
    """Module, symbol, and line of the rule function, resolved when inspected."""
    cached = _PROVENANCE.get(name)
    if cached is not None:
        return dict(cached)
    found = _rule_provenance(name)
    _PROVENANCE[name] = found
    return dict(found)


def _rule_provenance(name: str) -> dict:
    target = inspect.unwrap(dict(RULES)[name])
    try:
        source_file = inspect.getsourcefile(target) or inspect.getfile(target)
    except TypeError:
        source_file = None
    module = None
    if source_file:
        resolved = Path(source_file).resolve()
        module = resolved.as_posix()
        for root in (Path.cwd(), Path(__file__).resolve().parents[1]):
            try:
                module = resolved.relative_to(root).as_posix()
                break
            except ValueError:
                continue
    try:
        _lines, line = inspect.getsourcelines(target)
    except (OSError, TypeError):
        line = None
    symbol = getattr(target, "__qualname__", getattr(target, "__name__", None))
    return {"rule": name, "module": module, "symbol": symbol, "line": line}


def admit(ctx: AdmissionContext) -> dict:
    for _name, fn in RULES:
        result = fn(ctx)
        if result is not None:
            return result
    raise RuntimeError("admission registry returned no decision")


def proposal_prefix(history: list, proposal_id: str):
    """Events admission saw for this proposal, and the proposal's recorded time."""
    for index, event in enumerate(history):
        body = event.get("body") or {}
        if event.get("kind") == "proposed" and body.get("id") == proposal_id:
            return history[:index], event["at"]
    return None


def explain(ctx: AdmissionContext) -> dict:
    """Admission plus the deciding rule's provenance. Nothing here is persisted."""
    decision = admit(ctx)
    return {
        "status": decision["status"],
        "reason": decision["reason"],
        "rule": decision["rule"],
        "provenance": rule_provenance(decision["rule"]),
    }
