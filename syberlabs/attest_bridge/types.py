"""Opaque Attest records represented as ordinary SyberWork observations.

These wrappers define the SyberWork bridge envelope, not Attest's wire schema.
The source payload is preserved as JSON and never interpreted as authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, TypeAlias

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


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
    if not isinstance(copied, dict):  # keeps the return type and object-only contract explicit
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
    """An Attest run receipt imported as evidence, not a verified effect result."""

    kind: ClassVar[str] = "run_receipt"
    observation_key: ClassVar[str] = "attest.run_receipt"


@dataclass(frozen=True)
class AuthorityDecision(_AttestRecord):
    """An Attest authority decision recorded for context, not SyberWork approval."""

    kind: ClassVar[str] = "authority_decision"
    observation_key: ClassVar[str] = "attest.authority_decision"


@dataclass(frozen=True)
class HandoffPackage(_AttestRecord):
    """An Attest handoff package recorded as a proposal for a human/workflow."""

    kind: ClassVar[str] = "handoff_package"
    observation_key: ClassVar[str] = "attest.handoff_package"


AttestRecord: TypeAlias = RunReceipt | AuthorityDecision | HandoffPackage
