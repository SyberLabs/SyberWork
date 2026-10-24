"""Opaque Attest records imported into SyberWork as observation data."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, TypeAlias

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

ATTEST_RECEIPT_SCHEMA = "attest.receipt/v0"
_RECEIPT_FIELDS = (
    "schema",
    "case_id",
    "created_at",
    "doc",
    "claim",
    "authority",
    "proposal",
    "approval",
    "evidence",
    "handoff",
    "disposition",
    "verified",
)
_RECEIPT_SECTION_FIELDS = {
    "doc": ("source", "sha256_before", "sha256_after"),
    "claim": ("id", "kind", "lines", "text", "expects_before", "expects_after"),
    "authority": ("policy", "decision", "rule"),
    "proposal": ("agent", "patch_sha256", "patch"),
    "approval": ("actor", "proposal_sha256"),
    "evidence": ("before", "after"),
}


def _json_value(value: Any) -> JSONValue:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Attest payload numbers must be finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Attest payload object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    raise TypeError(f"Attest payload contains a non-JSON value: {type(value).__name__}")


def _json_object(value: Mapping[str, Any]) -> dict[str, JSONValue]:
    if not isinstance(value, Mapping):
        raise TypeError("Attest record payload must be a JSON object")
    copied = _json_value(value)
    if not isinstance(copied, dict):
        raise TypeError("Attest record payload must be a JSON object")
    return copied


@dataclass(frozen=True)
class _AttestRecord:
    """A typed tag around an opaque, copied Attest JSON object."""

    payload: Mapping[str, Any]
    kind: ClassVar[str]
    observation_key: ClassVar[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _json_object(self.payload))

    def observation_value(self) -> dict[str, JSONValue]:
        """Return the bridge envelope while preserving the source record as data."""
        return {"kind": self.kind, "payload": _json_object(self.payload)}


@dataclass(frozen=True)
class RunReceipt(_AttestRecord):
    """Attest receipt/v0 shape, kept opaque and never promoted to verified evidence."""

    kind: ClassVar[str] = "run_receipt"
    observation_key: ClassVar[str] = "attest.run_receipt"
    schema_version: ClassVar[str] = ATTEST_RECEIPT_SCHEMA
    top_level_fields: ClassVar[tuple[str, ...]] = _RECEIPT_FIELDS
    section_fields: ClassVar[dict[str, tuple[str, ...]]] = _RECEIPT_SECTION_FIELDS

    def __post_init__(self) -> None:
        super().__post_init__()
        receipt = self.payload
        # File imports enforce the v0 tag. Keep the JSON mapping opaque here so
        # callers can preserve partial/historical records without granting them
        # evidence status; any supplied schema/version claim must still match.
        if "schema" in receipt and receipt["schema"] != self.schema_version:
            raise ValueError(f"Attest receipt schema must be {self.schema_version}")
        if "verified" in receipt and receipt["verified"] is not False:
            raise ValueError("Attest receipt verified must be false")


@dataclass(frozen=True)
class AuthorityDecision(_AttestRecord):
    """An Attest authority decision recorded for context, not SyberWork approval."""

    kind: ClassVar[str] = "authority_decision"
    observation_key: ClassVar[str] = "attest.authority_decision"


@dataclass(frozen=True)
class HandoffPackage(_AttestRecord):
    """An Attest handoff package recorded as proposal context for a human/workflow."""

    kind: ClassVar[str] = "handoff_package"
    observation_key: ClassVar[str] = "attest.handoff_package"


AttestRecord: TypeAlias = RunReceipt | AuthorityDecision | HandoffPackage
