"""Exports that sit beside the hash chain. They do not change event bytes.

CloudEvents is the envelope other systems already route. The witness is
HMAC-SHA256 over the chain head, with a key the caller keeps. It is not
a public-key DSSE signature and it is not a transparency-log entry.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Mapping

from syberlabs.canonical import canonical
from syberlabs.events import DIGEST_FIELDS, GENESIS
from syberlabs.protocol import SIDE_PROTOCOL


CLOUD_EVENT_SOURCE = "https://sdk.syberlabs.space/v0alpha1"


def at_microseconds(at: float) -> int:
    """Integer microseconds for export. The stored ``at`` float stays in the hash."""
    return int(round(float(at) * 1_000_000))


def cloudevent(event: Mapping) -> dict:
    """One case event as a CloudEvents 1.0 envelope. The data is the hashed fields."""
    kind = event["kind"]
    return {
        "specversion": "1.0",
        "id": f"{event['case_id']}:{event['seq']}",
        "source": CLOUD_EVENT_SOURCE,
        "type": f"space.syberlabs.sdk.case.{kind}",
        "datacontenttype": "application/json",
        "data": {key: event[key] for key in DIGEST_FIELDS},
        "atmicroseconds": at_microseconds(event["at"]),
        "sideprotocol": SIDE_PROTOCOL,
    }


def witness(case_id: str, events: list, key: bytes) -> dict:
    """HMAC-SHA256 of ``{case_id, count, head}`` using the current canonical JSON.

    ``key`` is not stored and is not part of any event hash. ``head`` is the
    last event hash, or the genesis hash when the case has no events.
    """
    if isinstance(key, str):
        key = key.encode()
    payload = {
        "case_id": case_id,
        "count": len(events),
        "head": events[-1]["hash"] if events else GENESIS,
    }
    mac = hmac.new(key, canonical(payload).encode(), hashlib.sha256).hexdigest()
    return {"alg": "HMAC-SHA256", "payload": payload, "mac": mac}


def witness_matches(document: Mapping, events: list, key: bytes) -> bool:
    """True when ``document`` is the witness of these events under ``key``."""
    payload = document.get("payload")
    if not isinstance(payload, dict) or "case_id" not in payload:
        return False
    again = witness(payload["case_id"], events, key)
    return hmac.compare_digest(again["mac"], str(document.get("mac", ""))) and again["payload"] == dict(payload)
