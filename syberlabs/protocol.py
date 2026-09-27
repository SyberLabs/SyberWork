"""Names for the v0alpha1 objects. These types do not validate input."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# Side channel beside the hash chain. Not an input to event_digest.
SIDE_PROTOCOL = "sdk.syberlabs.space/v0alpha1+jcs1"


AdmissionStatus = Literal["allowed", "needs_approval", "denied"]
EffectState = Literal["succeeded", "rejected", "unknown"]
EventKind = Literal[
    "case_created",
    "observed",
    "proposed",
    "decision",
    "approved",
    "effect_started",
    "effect_succeeded",
    "effect_rejected",
    "effect_unknown",
    "reconciliation_checked",
    "reconciled",
    "signed",
    "resolution_requested",
    "resolution_checked",
    "resolution_completed",
    "resolution_escalated",
    "case_cancelled",
]


class AdmissionDecision(TypedDict):
    status: AdmissionStatus
    reason: str


class EffectOutcome(TypedDict, total=False):
    state: EffectState
    proposal_id: str
    action: str
    output: dict[str, Any]
    claim_hash: str
    http_status: int
    error: str


class Event(TypedDict):
    case_id: str
    seq: int
    kind: str
    body: dict[str, Any]
    at: float
    previous: str
    hash: str
