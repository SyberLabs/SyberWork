"""Exact-unit economic admission and settlement receipt validation."""

import re


MAX_UNITS = 2**63 - 1


def units(value):
    """Only canonical positive integer strings fit for exact accounting."""
    if not isinstance(value, str) or len(value) > 19 or not re.fullmatch(r"[1-9][0-9]*", value):
        return None
    number = int(value)
    return number if number <= MAX_UNITS else None


def receipt_matches(record, key, args, request_digest):
    return (isinstance(record, dict) and record.get("state") == "settled"
            and record.get("idempotency_key") == key
            and record.get("request_digest") == request_digest
            and record.get("amount_units") == args["amount_units"]
            and record.get("asset") == args["asset"]
            and record.get("counterparty") == args["counterparty"]
            and isinstance(record.get("external_id"), str) and bool(record["external_id"]))


def admit(db, config, policy, local, history, args, now):
    """Return a denial reason or None. Caller already checked source freshness."""
    economic = policy.get("economic")
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
    reserved = db.execute("SELECT COALESCE(SUM(amount_units),0) FROM economic_reservations "
                          "WHERE budget_id=? AND asset=? AND state!='released'",
                          (budget_id, config["asset"])).fetchone()[0]
    if reserved > cap - amount:
        return "economic_budget_exceeded"
    return None
