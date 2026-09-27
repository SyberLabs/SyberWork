"""Exact-unit economic admission and settlement receipt checks.

Budget totals are supplied by the caller. This module does not open a database
and does not import syberwork.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from syberlabs.errors import Rejected


MAX_UNITS = 2**63 - 1


def units(value):
    """Only canonical positive integer strings fit for exact accounting."""
    if not isinstance(value, str) or len(value) > 19 or not re.fullmatch(r"[1-9][0-9]*", value):
        return None
    number = int(value)
    return number if number <= MAX_UNITS else None


def receipt_matches(record, key, args, request_digest) -> bool:
    return (isinstance(record, dict) and record.get("state") == "settled"
            and record.get("idempotency_key") == key
            and record.get("request_digest") == request_digest
            and record.get("amount_units") == args["amount_units"]
            and record.get("asset") == args["asset"]
            and record.get("counterparty") == args["counterparty"]
            and isinstance(record.get("external_id"), str) and bool(record["external_id"]))


def policy_has_economic(doc: dict) -> bool:
    actions = doc.get("actions")
    if not isinstance(actions, dict):
        return False
    return any(isinstance(rule, dict) and "economic" in rule for rule in actions.values())


def validate_policy_budgets(doc: dict, prior_docs: Sequence[dict]) -> None:
    """Reject a publish that retargets a budget or raises its cap."""
    budgets = {}
    for existing in prior_docs:
        actions = existing.get("actions", {})
        if not isinstance(actions, dict):
            continue
        for rule in actions.values():
            economic_rule = rule.get("economic") if isinstance(rule, dict) else None
            if isinstance(economic_rule, dict):
                key = economic_rule.get("budget_id")
                amount = units(economic_rule.get("budget_units"))
                if isinstance(key, str) and amount is not None:
                    earlier = budgets.get(key)
                    if earlier and earlier[0] != economic_rule.get("asset"):
                        raise Rejected("invalid_policy", "budget_id cannot change asset")
                    budgets[key] = (economic_rule.get("asset"), min(amount, earlier[1]) if earlier else amount)
    in_version = {}
    for rule in doc["actions"].values():
        economic_rule = rule.get("economic") if isinstance(rule, dict) else None
        if economic_rule is None:
            continue
        if not isinstance(economic_rule, dict):
            raise Rejected("invalid_policy", "economic rule must be an object")
        key = economic_rule.get("budget_id")
        asset = economic_rule.get("asset")
        amount = units(economic_rule.get("budget_units"))
        if not isinstance(key, str) or not key or not isinstance(asset, str) or not asset or amount is None:
            raise Rejected("invalid_policy", "budget_id, asset and exact budget_units required")
        counterparties = economic_rule.get("counterparties")
        if (not isinstance(counterparties, list) or not counterparties or
                any(not isinstance(item, str) or not item for item in counterparties) or
                len(set(counterparties)) != len(counterparties)):
            raise Rejected("invalid_policy", "counterparties must be a nonempty list of unique identifiers")
        if (not isinstance(economic_rule.get("rail"), str) or not economic_rule["rail"] or
                units(economic_rule.get("max_amount_units")) is None):
            raise Rejected("invalid_policy", "rail and exact max_amount_units required")
        if key in in_version and in_version[key] != (asset, amount):
            raise Rejected("invalid_policy", "budget_id must have one asset and cap per policy version")
        in_version[key] = (asset, amount)
        if key in budgets and (budgets[key][0] != asset or amount > budgets[key][1]):
            raise Rejected("invalid_policy", "budget_id cannot change asset or increase allocation")


def denial(config, policy_action, local, history, args, now, budget_reserved: Callable[[str, str], int] | None):
    """Return a denial reason or None. Caller already checked source freshness."""
    economic = policy_action.get("economic")
    if not isinstance(economic, dict):
        return "economic_policy_missing"
    if set(args) != {"operation", "amount_units", "asset", "counterparty",
                     "purpose", "evidence", "expires_at"}:
        return "economic_intent_invalid"
    binding = local.get("arguments", {}).get("amount_units", "")
    required_keys = {r.get("key") for r in local.get("required_facts", []) if isinstance(r, dict) and r.get("verified")}
    if not isinstance(binding, str) or not binding.startswith("fact:") or binding[5:].split(".")[0] not in required_keys:
        return "economic_amount_unbound"
    amount = units(args.get("amount_units"))
    if amount is None or args.get("operation") not in ("purchase_capability", "transfer"):
        return "economic_intent_invalid"
    if args["operation"] != config.get("operation"):
        return "economic_operation_denied"
    for name in ("asset", "rail"):
        if economic.get(name) != config.get(name):
            return "economic_policy_mismatch"
    allowed = economic.get("counterparties")
    if (not isinstance(allowed, list) or args.get("asset") != config.get("asset") or
            args.get("counterparty") != config.get("counterparty") or
            config.get("counterparty") not in allowed):
        return "economic_destination_denied"
    if not isinstance(args.get("purpose"), str) or not 1 <= len(args["purpose"].strip()) <= 500:
        return "economic_purpose_required"
    expiry = args.get("expires_at")
    if type(expiry) not in (int, float) or not now < expiry <= now + 3600:
        return "economic_expired"
    required = local.get("required_facts", [])
    if not required or any(not r.get("verified") for r in required):
        return "economic_verified_evidence_required"
    expected = []
    for requirement in required:
        fact = next((e for e in reversed(history) if e["kind"] == "observed"
                     and e["body"]["key"] == requirement["key"]), None)
        if not fact or not fact["body"].get("verified"):
            return "economic_evidence_mismatch"
        expected.append(fact["hash"])
    if (not isinstance(args.get("evidence"), list) or len(set(expected)) != len(expected)
            or args["evidence"] != expected):
        return "economic_evidence_mismatch"
    budget_id = economic.get("budget_id")
    cap = units(economic.get("budget_units"))
    limits = [units(economic.get("max_amount_units"))]
    if "max_amount_units" in local:
        limits.append(units(local["max_amount_units"]))
    if not isinstance(budget_id, str) or not budget_id or cap is None or None in limits:
        return "economic_policy_invalid"
    if amount > min(limits):
        return "economic_limit_exceeded"
    if budget_reserved is None:
        reserved = 0
    else:
        reserved = budget_reserved(budget_id, config["asset"])
    if type(reserved) is not int or reserved > cap - amount:
        return "economic_budget_exceeded" if type(reserved) is int else "economic_policy_invalid"
    return None
