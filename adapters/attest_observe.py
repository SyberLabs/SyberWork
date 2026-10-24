"""Read-only conversion of Attest JSON artifacts into SyberWork observations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from syberlabs.attest_bridge.types import (
    AttestRecord,
    AuthorityDecision,
    HandoffPackage,
    RunReceipt,
)

_RECORD_TYPES = (RunReceipt, AuthorityDecision, HandoffPackage)


def record_to_observation_payload(
    record: AttestRecord, *, source_version: str, actor: str
) -> dict[str, Any]:
    """Map one typed Attest artifact to ``Session.observe`` keyword arguments.

    The adapter only constructs data. It does not append to a case, approve a
    proposal, execute an effect, or verify a destination write.
    """
    if not isinstance(record, _RECORD_TYPES):
        raise TypeError("record must be a RunReceipt, AuthorityDecision, or HandoffPackage")
    if not isinstance(source_version, str) or not source_version.strip():
        raise ValueError("source_version is required")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor is required")
    return {
        "key": record.observation_key,
        "value": record.observation_value(),
        "source": "attest",
        "version": source_version,
        "actor": actor,
        "verified": False,
    }


def receipt_json_to_observation_payload(
    receipt_json: Mapping[str, Any], *, source_version: str, actor: str
) -> dict[str, Any]:
    """Convert a parsed Attest RunReceipt JSON object into an unverified observation.

    ``source_version`` is supplied by the importer; the opaque receipt is not
    trusted to identify its own schema or prove the version of the source.
    """
    return record_to_observation_payload(
        RunReceipt(receipt_json), source_version=source_version, actor=actor
    )
