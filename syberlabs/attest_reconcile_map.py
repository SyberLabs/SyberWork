"""Conservative classification for Attest artifacts entering SyberWork.

Attest artifacts can be recorded as observations or handed to a human. They
are never destination proof and this module never licenses reconciliation.
"""


_CLASSIFICATIONS = {
    # The file schema is an identifier, not destination-write authority.
    "attest.receipt/v0": "observation",
    "receipt": "observation",
    "RunReceipt": "observation",
    "run_receipt": "observation",
    "AuthorityDecision": "observation",
    "authority_decision": "observation",
    "HandoffPackage": "handoff",
    "handoff_package": "handoff",
}


def classify_attest_artifact(kind: object) -> dict[str, bool]:
    """Classify a known Attest artifact; unknown kinds fail closed.

    Exactly one of ``observation``, ``handoff``, or ``refuse`` is true.
    ``verified_reconciliation`` is always false: only SyberWork's installed
    destination status lookup can produce that evidence.
    """
    classification = _CLASSIFICATIONS.get(kind) if isinstance(kind, str) else None
    return {
        "observation": classification == "observation",
        "handoff": classification == "handoff",
        "refuse": classification is None,
        "verified_reconciliation": False,
    }
