"""Effect evidence predicates shared by admission and acceptance."""


def verified_reconciliation(event: dict) -> bool:
    """Legacy manager attestations do not establish external execution."""
    body = event["body"]
    return (event["kind"] == "reconciled" and body.get("success") is True
            and isinstance(body.get("proof"), dict)
            and body["proof"].get("verified") is True
            and bool(body["proof"].get("external_id"))
            and bool(body["proof"].get("response_digest")))
