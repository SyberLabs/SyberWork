"""Event envelope digest and hash-chain verification."""

from __future__ import annotations

from syberlabs.canonical import digest


DIGEST_FIELDS = ("case_id", "seq", "kind", "body", "at", "previous")
GENESIS = "0" * 64


def event_digest(event: dict) -> str:
    """SHA-256 of the canonical envelope, excluding the stored hash."""
    return digest({key: event[key] for key in DIGEST_FIELDS})


def verify_events(events: list[dict]) -> bool:
    previous = GENESIS
    for event in events:
        if event["previous"] != previous or event["hash"] != event_digest(event):
            return False
        previous = event["hash"]
    return True
