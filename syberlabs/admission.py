"""Deterministic admission. Moved from Work._admit without a change in order."""

from __future__ import annotations

from collections.abc import Callable

from syberlabs.canonical import digest
from syberlabs.evidence import verified_reconciliation
from syberlabs.values import at_path


def approval_roles(contract, policy, action):
    roles = [policy["actions"].get(action, {}).get("approval_role"), contract["actions"].get(action, {}).get("approval_role")]
    return list(dict.fromkeys(r for r in roles if r))


def admit(contract, policy, history, proposal, now, action_installed: Callable[[str], bool]):
    action, args = proposal["action"], proposal["args"]

    def deny(reason):
        return {"status": "denied", "reason": reason}

    if any(event["kind"] == "case_cancelled" for event in history):
        return deny("case_cancelled")
    if not isinstance(args, dict) or not isinstance(proposal.get("roles"), list):
        return deny("invalid_proposal_shape")
    if action not in contract["actions"]:
        return deny("action_not_in_contract")
    if action not in policy["actions"]:
        return deny("action_not_in_global_policy")
    if not action_installed(action):
        return deny("action_not_installed")
    local, global_rule = contract["actions"][action], policy["actions"][action]
    if not set(global_rule.get("roles", [])).intersection(proposal["roles"]):
        return deny("actor_role_missing")
    for key, resolution in contract.get("resolutions", {}).items():
        if action not in resolution["blocks_actions"]:
            continue
        task = next((e for e in history if e["kind"] == "resolution_requested" and e["body"]["key"] == key), None)
        if not task:
            continue
        closed = next((e for e in history if e["kind"] == "resolution_completed" and e["body"]["task_id"] == task["body"]["id"]), None)
        if not closed:
            return deny("resolution_open:" + key)
        result_spec = resolution["result"]
        latest = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == result_spec["key"]), None)
        if (not latest or not latest["body"].get("verified") or latest["body"]["source"] != result_spec["source"] or
                at_path(latest["body"]["value"], result_spec["value_path"]) != closed["body"]["choice"] or
                at_path(latest["body"]["value"], result_spec["identity_path"]) != task["body"]["record_key"]):
            return deny("resolution_changed:" + key)
    if any(e["kind"] == "effect_succeeded" and e["body"]["action"] == action or
           verified_reconciliation(e) and e["body"]["action"] == action for e in history):
        return deny("action_already_completed")
    proposals = {e["body"]["id"]: e["body"]["action"] for e in history if e["kind"] == "proposed"}
    verified_ids = {e["body"]["proposal_id"] for e in history if verified_reconciliation(e)}
    for event in history:
        if event["kind"] != "effect_unknown":
            continue
        effect = event["body"]
        if effect.get("action", proposals.get(effect["proposal_id"])) == action and effect["proposal_id"] not in verified_ids:
            return deny("effect_unresolved:" + action)
    for rule in (global_rule, local):
        if "max_amount" in rule:
            if type(args.get("amount")) not in (int, float):
                return deny("amount_required")
            if args["amount"] > rule["max_amount"] or args["amount"] < 0:
                return deny("amount_exceeds_limit")
    if local.get("requires_effect") and not any(e["kind"] == "effect_succeeded" and e["body"]["action"] == local["requires_effect"] or verified_reconciliation(e) and e["body"]["action"] == local["requires_effect"] for e in history):
        return deny("required_prior_effect_missing")
    for requirement in local.get("required_facts", []):
        fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == requirement["key"]), None)
        if not fact:
            return deny("missing_fact:" + requirement["key"])
        if fact["body"]["source"] != requirement["source"]:
            return deny("untrusted_fact_source:" + requirement["key"])
        if requirement.get("verified") and not fact["body"].get("verified"):
            return deny("source_verification_required:" + requirement["key"])
        if now - fact["at"] > requirement.get("max_age_seconds", 86400):
            return deny("stale_fact:" + requirement["key"])
    created = next((e for e in history if e["kind"] == "case_created"), None)
    for input_key, binding in contract.get("input_bindings", {}).items():
        fact_key, *path = binding[5:].split(".")
        fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == fact_key), None)
        value = fact["body"]["value"] if fact else None
        for component in path:
            value = value.get(component) if isinstance(value, dict) else None
        if not created or not fact or value is None or value != created["body"]["inputs"][input_key]:
            return deny("input_provenance:" + input_key)
    for param, binding in local.get("arguments", {}).items():
        if binding.startswith("version:"):
            name = binding[8:]
            fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == name), None)
            if not fact or args.get(param) != fact["body"]["version"]:
                return deny("argument_provenance:" + param)
        elif binding.startswith("fact:"):
            name, *path = binding[5:].split(".")
            fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == name), None)
            expected = fact["body"]["value"] if fact else None
            for component in path:
                expected = expected.get(component) if isinstance(expected, dict) else None
            if not fact or expected is None or param not in args or args[param] != expected:
                return deny("argument_provenance:" + param)
    for role in approval_roles(contract, policy, action):
        if not any(e["kind"] == "approved" and e["body"].get("proposal_id") == proposal["id"] and e["body"]["role"] == role and e["body"].get("args_hash") == digest(args) for e in history):
            return {"status": "needs_approval", "reason": "approval_required:" + role}
    return {"status": "allowed", "reason": "all_checks_passed"}
