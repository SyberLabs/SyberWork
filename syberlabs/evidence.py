"""Effect evidence predicates shared by admission and acceptance."""


def verified_reconciliation(event: dict) -> bool:
    """Legacy manager attestations do not establish external execution."""
    body = event["body"]
    return (event["kind"] == "reconciled" and body.get("success") is True
            and isinstance(body.get("proof"), dict)
            and body["proof"].get("verified") is True
            and bool(body["proof"].get("external_id"))
            and bool(body["proof"].get("response_digest")))


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
