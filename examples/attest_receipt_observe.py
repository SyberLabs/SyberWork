"""Print an unverified SyberWork observation from an Attest v0 receipt file.

Attest writes these receipts under `<attest_home>/receipts/<case_id>.json`.
This example only translates the JSON into Work.observe-compatible arguments;
it does not append to a case or verify receipt authority.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "attest.receipt/v0"
OBSERVATION_KEY = "attest_receipt"
OBSERVATION_SOURCE = "attest"
OBSERVATION_ACTOR = "attest-bridge"
RECEIPT_FIELDS = {
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
}


def receipt_to_observation(receipt: Any) -> dict[str, Any]:
    """Return SyberWork `observe` keyword fields for a live Attest receipt."""
    if not isinstance(receipt, dict):
        raise ValueError("Attest receipt must be a JSON object")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ValueError(f"receipt schema must be {RECEIPT_SCHEMA}")

    missing = RECEIPT_FIELDS - receipt.keys()
    if missing:
        raise ValueError("receipt is missing required fields: " + ", ".join(sorted(missing)))
    unexpected = receipt.keys() - RECEIPT_FIELDS
    if unexpected:
        raise ValueError("receipt has unexpected fields: " + ", ".join(sorted(unexpected)))
    if receipt["verified"] is not False:
        raise ValueError("receipt verified must be false")

    return {
        "key": OBSERVATION_KEY,
        "value": receipt,
        "source": OBSERVATION_SOURCE,
        "version": RECEIPT_SCHEMA,
        "actor": OBSERVATION_ACTOR,
        "verified": False,
    }


def load_receipt_observation(receipt_path: str | Path) -> dict[str, Any]:
    """Load one receipt from disk and translate it to an unverified observation."""
    path = Path(receipt_path)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read a UTF-8 JSON receipt from {path}") from exc
    return receipt_to_observation(receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a SyberWork observation for an Attest attest.receipt/v0 file"
    )
    parser.add_argument(
        "receipt",
        type=Path,
        help="receipt path (Attest writes <attest_home>/receipts/<case_id>.json)",
    )
    args = parser.parse_args(argv)
    try:
        observation = load_receipt_observation(args.receipt)
    except ValueError as exc:
        print(f"attest_receipt_observe: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(observation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
