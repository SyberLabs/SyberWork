"""Conservative classification for Attest artifacts entering SyberWork.

Attest artifacts can be recorded as observations or handed to a human. They
are never destination proof and this module never licenses reconciliation.
"""


_CLASSIFICATIONS = {
    "RunReceipt": "observation",
    "AuthorityDecision": "observation",
    "HandoffPackage": "handoff",
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
