"""Effect evidence predicates shared by admission and acceptance."""

import json

# Top-level fields a destination uses to say its record is not a live effect. A flag that is
# present must be exactly false, ``livemode`` exactly true, and a label exactly a live label.
SIMULATION_FLAGS = ("simulated", "simulation", "sandbox", "dry_run", "test", "testnet", "preview")
SIMULATION_LABELS = ("mode", "environment", "env")
LIVE_LABELS = ("live", "production", "mainnet")


class AmbiguousRecord(ValueError):
    """A destination body that names one field twice has no single meaning."""


def _unique_fields(pairs: list) -> dict:
    record = {}
    for name, value in pairs:
        if name in record:
            raise AmbiguousRecord(f"duplicate field {name!r}")
        record[name] = value
    return record


def destination_record(raw: bytes):
    """Parse a destination body; duplicate keys at any depth raise ``AmbiguousRecord``, never last-wins."""
    return json.loads(raw, object_pairs_hook=_unique_fields)


def simulation_labeled(record) -> bool:
    """True when a destination record labels itself as simulated, sandbox, test or otherwise not live."""
    if not isinstance(record, dict):
        return False
    return (any(name in record and record[name] is not False for name in SIMULATION_FLAGS)
            or ("livemode" in record and record["livemode"] is not True)
            or any(name in record and record[name] not in LIVE_LABELS for name in SIMULATION_LABELS))


def verified_reconciliation(event: dict) -> bool:
    """Legacy manager attestations do not establish external execution."""
    body = event["body"]
    return (event["kind"] == "reconciled" and body.get("success") is True
            and isinstance(body.get("proof"), dict)
            and body["proof"].get("verified") is True
            and bool(body["proof"].get("external_id"))
            and bool(body["proof"].get("response_digest")))


def signer_is_effect_actor(contract: dict, history: list, actor: str, role: str) -> bool:
    """True when a completed ``after_action`` effect for this signoff was started by ``actor``.

    A signoff before that effect exists is allowed. Acceptance still fails
    until the effect and a later signature are both present.
    """
    for clause in contract.get("acceptance", []):
        if not isinstance(clause, dict) or clause.get("kind") != "signoff" or clause.get("role") != role:
            continue
        action = clause.get("after_action")
        if not action:
            continue
        completed = {
            event["body"].get("proposal_id")
            for event in history
            if (event.get("kind") == "effect_succeeded" or verified_reconciliation(event))
            and (event.get("body") or {}).get("action") == action
        }
        if not completed:
            continue
        for event in history:
            body = event.get("body") or {}
            if event.get("kind") == "effect_started" and body.get("proposal_id") in completed and body.get("actor") == actor:
                return True
    return False


def acceptance_results(contract: dict, history: list) -> list[dict]:
    results = []
    for clause in contract["acceptance"]:
        if clause["kind"] == "effect":
            passed = any(e["kind"] == "effect_succeeded" and e["body"]["action"] == clause["action"] or verified_reconciliation(e) and e["body"]["action"] == clause["action"] for e in history)
        elif clause["kind"] == "signoff":
            completed_seq = next((e["seq"] for e in history if
                                  (e["kind"] == "effect_succeeded" or verified_reconciliation(e))
                                  and e["body"]["action"] == clause.get("after_action")), 0)
            passed = any(e["kind"] == "signed" and e["body"]["role"] == clause["role"] and (not clause.get("after_action") or e["seq"] > completed_seq > 0) for e in history)
        elif clause["kind"] == "fact":
            passed = any(e["kind"] == "observed" and e["body"]["key"] == clause["key"] and e["body"]["value"] == clause.get("equals") for e in history)
        else:
            passed = False
        results.append({"id": clause["id"], "passed": passed})
    return results
