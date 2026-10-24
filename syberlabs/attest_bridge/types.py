"""Opaque Attest records imported into SyberWork as observation data."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, TypeAlias

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

ATTEST_RECEIPT_SCHEMA = "attest.receipt/v0"
_RECEIPT_REQUIRED_FIELDS = (
    "schema", "case_id", "created_at", "doc", "claim", "authority",
    "proposal", "approval", "evidence", "handoff", "disposition", "verified",
)
_RECEIPT_OPTIONAL_FIELDS = ("ticket", "app", "worker")
_RECEIPT_FIELDS = _RECEIPT_REQUIRED_FIELDS + _RECEIPT_OPTIONAL_FIELDS
_RECEIPT_SECTION_FIELDS = {
    "doc": ("source", "sha256_before", "sha256_after"),
    "claim": ("id", "kind", "lines", "text", "expects_before", "expects_after"),
    "authority": ("policy", "decision", "rule"),
    "proposal": ("agent", "patch_sha256", "patch"),
    "approval": ("actor", "proposal_sha256"),
    "evidence": ("before", "after"),
}
_AUTHORITY_DECISIONS = {"doc_repair_allowed", "doc_repair_denied", "implementation_required"}
_DISPOSITIONS = {"repaired", "handoff_implementation", "implemented", "unresolved"}


def _require_fields(value: Any, *, name: str, required: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Attest receipt {name} must be an object")
    missing = set(required) - set(value)
    if missing:
        raise ValueError(f"Attest receipt {name} is missing fields: {', '.join(sorted(missing))}")
    return value


def _validate_receipt(receipt: Mapping[str, Any]) -> None:
    if receipt.get("schema") != ATTEST_RECEIPT_SCHEMA:
        raise ValueError(f"Attest receipt schema must be {ATTEST_RECEIPT_SCHEMA}")
    unknown = set(receipt) - set(_RECEIPT_FIELDS)
    if unknown:
        raise ValueError(f"unknown Attest receipt fields: {', '.join(sorted(unknown))}")
    missing = set(_RECEIPT_REQUIRED_FIELDS) - set(receipt)
    if missing:
        raise ValueError(f"Attest receipt is missing fields: {', '.join(sorted(missing))}")
    if not isinstance(receipt["case_id"], str) or not receipt["case_id"]:
        raise ValueError("Attest receipt case_id must be a non-empty string")
    if not isinstance(receipt["created_at"], str) or not receipt["created_at"]:
        raise ValueError("Attest receipt created_at must be a non-empty string")
    if receipt["verified"] is not False:
        raise ValueError("Attest receipt verified must be false")

    sections = {}
    for name, required in _RECEIPT_SECTION_FIELDS.items():
        sections[name] = _require_fields(receipt[name], name=name, required=required)
    for name in ("before", "after"):
        _require_fields(
            sections["evidence"][name],
            name=f"evidence.{name}",
            required=("status", "observed", "note", "screenshot"),
        )
    decision = sections["authority"]["decision"]
    if not isinstance(decision, str) or decision not in _AUTHORITY_DECISIONS:
        raise ValueError("Attest receipt authority.decision is not recognized")
    disposition = receipt["disposition"]
    if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
        raise ValueError("Attest receipt disposition is not recognized")

    if "ticket" in receipt:
        ticket = _require_fields(receipt["ticket"], name="ticket", required=("path", "title"))
        if not all(isinstance(ticket[field], str) for field in ("path", "title")):
            raise ValueError("Attest receipt ticket path and title must be strings")
    if "app" in receipt:
        app = _require_fields(receipt["app"], name="app", required=("original", "candidate", "patch_sha256"))
        if not isinstance(app["patch_sha256"], str):
            raise ValueError("Attest receipt app.patch_sha256 must be a string")
    if "worker" in receipt and not isinstance(receipt["worker"], Mapping):
        raise ValueError("Attest receipt worker must be an object")
    if receipt["disposition"] == "implemented" and not {"ticket", "app"}.issubset(receipt):
        raise ValueError("implemented Attest receipts require ticket and app fields")


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
    """Attest receipt/v0 data with a contract projection; its envelope stays unverified."""

    kind: ClassVar[str] = "run_receipt"
    observation_key: ClassVar[str] = "attest.run_receipt"
    schema_version: ClassVar[str] = ATTEST_RECEIPT_SCHEMA
    top_level_fields: ClassVar[tuple[str, ...]] = _RECEIPT_FIELDS
    section_fields: ClassVar[dict[str, tuple[str, ...]]] = _RECEIPT_SECTION_FIELDS

    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_receipt(self.payload)

    def observation_value(self) -> dict[str, JSONValue]:
        """Preserve the receipt and add a clearly separated contract projection."""
        value = super().observation_value()
        receipt = self.payload
        projection: dict[str, JSONValue] = {}
        if receipt["disposition"] == "implemented":
            ticket = receipt["ticket"]
            app = receipt["app"]
            projection["implementation"] = {
                "ticket_title": ticket["title"],
                "patch_sha256": app["patch_sha256"],
            }
        if receipt["evidence"]["after"].get("status") == "proven":
            projection["destination_verification"] = {
                "status": "verified",
                "method": "kane_recheck",
                "reported_by": "attest.receipt/v0 evidence.after.status",
            }
        if projection:
            value["projection"] = projection
        return value


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
