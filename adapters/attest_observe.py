"""Read-only conversion of Attest JSON artifacts into SyberWork observations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from syberlabs.attest_bridge.types import (
    AttestRecord,
    AuthorityDecision,
    HandoffPackage,
    RunReceipt,
)

_RECORD_TYPES = (RunReceipt, AuthorityDecision, HandoffPackage)
ATTEST_RECEIPT_SCHEMA = "attest.receipt/v0"


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


def receipt_file_to_observation_payload(
    receipt_path: str | Path, *, actor: str
) -> dict[str, Any]:
    """Read an Attest ``attest.receipt/v0`` JSON file as an unverified observation.

    The receipt remains opaque payload data. Its schema tag identifies the
    input format; it is not a signature check, authority grant, or destination
    verification.
    """
    path = Path(receipt_path)
    try:
        receipt_json = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid Attest receipt JSON in {path}") from exc
    if not isinstance(receipt_json, dict):
        raise TypeError("Attest receipt file must contain a JSON object")
    if receipt_json.get("schema") != ATTEST_RECEIPT_SCHEMA:
        raise ValueError(f"Attest receipt schema must be {ATTEST_RECEIPT_SCHEMA}")
    return receipt_json_to_observation_payload(
        receipt_json, source_version=ATTEST_RECEIPT_SCHEMA, actor=actor
    )


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
