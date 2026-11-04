"""Conservative classification for Attest artifacts entering SyberWork.

Attest artifacts can be recorded as observations or handed to a human. They
are never destination proof and this module never licenses reconciliation.
``attest_artifact`` recognizes one offered where destination proof belongs.
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


# Kinds that name an Attest artifact on a payload offered as destination proof: the classifier's own,
# except the generic "receipt", which an ordinary settlement record may well use as its type.
ATTEST_KINDS = frozenset(kind for kind in _CLASSIFICATIONS if kind != "receipt" and "/" not in kind)
KIND_FIELDS = ("kind", "type", "artifact")
SCHEMA_FIELDS = ("schema", "version")
SCHEMA_PREFIX = "attest."
# Fields every Attest receipt carries and a destination record has no reason to carry together.
RECEIPT_FIELDS = ("claim", "authority", "disposition")
# The bridge's own label for a passed Kane re-check (examples/attest_receipt_observe.py).
RECHECK_LABEL = "app_claim_recheck"


def attest_artifact(payload: object) -> bool:
    """True when a payload offered as destination proof is an Attest artifact, or shaped like one.

    A payload counts when, at its top level, ``schema`` or ``version`` is a string that starts
    with ``attest.``; ``kind``, ``type`` or ``artifact`` names an Attest artifact kind (see
    ``ATTEST_KINDS``); it carries all of the receipt fields ``claim``, ``authority`` and
    ``disposition``; or it carries the bridge's ``app_claim_recheck`` label. Only the top level is
    read: a destination may store an Attest receipt as the content of a write, and its record of
    that write may then quote the receipt in a nested field.
    """
    if not isinstance(payload, dict):
        return False
    return (any(isinstance(payload.get(f), str) and payload[f].startswith(SCHEMA_PREFIX) for f in SCHEMA_FIELDS)
            or any(isinstance(payload.get(f), str) and payload[f] in ATTEST_KINDS for f in KIND_FIELDS)
            or all(f in payload for f in RECEIPT_FIELDS)
            or RECHECK_LABEL in payload)
