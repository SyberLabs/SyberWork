"""Read-only conversion of Attest JSON artifacts into SyberWork observations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs.attest_bridge.types import (
    ATTEST_RECEIPT_SCHEMA,
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


def receipt_file_to_observation_payload(
    receipt_path: str | Path, *, actor: str
) -> dict[str, Any]:
    """Read an Attest ``<attest_home>/receipts/<case_id>.json`` as an unverified observation.

    The reader enforces the receipt/v0 contract shape, including spec-mode
    fields, while preserving the full source object. This is not a signature
    check or SyberWork authority grant; the observation envelope stays
    ``verified: false``. Contract projections remain observation data and do
    not reconcile SyberWork effects.
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


def main(argv: list[str] | None = None) -> int:
    """Print the observation JSON for one Attest receipt file."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("receipt_path", type=Path, help="<attest_home>/receipts/<case_id>.json")
    parser.add_argument("--actor", default="attest-importer", help="SyberWork observation actor")
    args = parser.parse_args(argv)
    try:
        observation = receipt_file_to_observation_payload(args.receipt_path, actor=args.actor)
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(observation, sort_keys=True))
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
