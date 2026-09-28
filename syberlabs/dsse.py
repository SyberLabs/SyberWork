"""DSSE envelopes for objects that must not enter the hash chain.

The signature is Ed25519 over the PAE bytes, not over the payload alone.
The arithmetic is not constant-time. The seed is the caller's; it is not
written into a case database.
"""

from __future__ import annotations

import base64
import json
from typing import Mapping

from syberlabs.canonical import canonical
from syberlabs.ed25519 import public_from_seed, sign, verify
from syberlabs.events import GENESIS


CHAIN_HEAD_TYPE = "application/vnd.syberlabs.chain-head+json"
CHECKPOINT_TYPE = "application/vnd.syberlabs.tlog-checkpoint+json"


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding. Lengths are ASCII, not uint64."""
    kind = payload_type.encode("ascii")
    return b"DSSEv1 " + str(len(kind)).encode("ascii") + b" " + kind + b" " + str(len(payload)).encode("ascii") + b" " + payload


def head_payload(case_id: str, events: list) -> bytes:
    """Canonical bytes of ``{case_id, count, head}``. ``head`` is the last hash or genesis."""
    document = {
        "case_id": case_id,
        "count": len(events),
        "head": events[-1]["hash"] if events else GENESIS,
    }
    return canonical(document).encode("ascii")


def sign_dsse(payload_type: str, payload: bytes, seed: bytes) -> dict:
    """One-signature DSSE envelope. ``keyid`` is the public key, hex-encoded."""
    public = public_from_seed(seed)
    signature = sign(seed, pae(payload_type, payload))
    return {
        "payloadType": payload_type,
        "payload": base64.standard_b64encode(payload).decode("ascii"),
        "signatures": [{
            "keyid": public.hex(),
            "sig": base64.standard_b64encode(signature).decode("ascii"),
        }],
    }


def verified_payload(envelope: Mapping, public: bytes, payload_type: str) -> bytes | None:
    """Payload bytes after the signature verifies. None when it does not.

    A ``keyid`` that is present and is not ``public`` is ignored. The payload
    is not parsed until a signature matches.
    """
    if not isinstance(envelope, Mapping) or envelope.get("payloadType") != payload_type:
        return None
    encoded = envelope.get("payload")
    signatures = envelope.get("signatures")
    if not isinstance(encoded, str) or not isinstance(signatures, list):
        return None
    try:
        payload = base64.standard_b64decode(encoded)
    except (ValueError, TypeError):
        return None
    message = pae(payload_type, payload)
    keyid = public.hex()
    for item in signatures:
        if not isinstance(item, Mapping):
            continue
        declared = item.get("keyid")
        if declared is not None and declared != keyid:
            continue
        encoded_sig = item.get("sig")
        if not isinstance(encoded_sig, str):
            continue
        try:
            signature = base64.standard_b64decode(encoded_sig)
        except (ValueError, TypeError):
            continue
        if verify(public, message, signature):
            return payload
    return None


def sign_chain_head(case_id: str, events: list, seed: bytes) -> dict:
    """DSSE envelope over the chain head. Not an event, and not stored on the case."""
    return sign_dsse(CHAIN_HEAD_TYPE, head_payload(case_id, events), seed)


def verify_chain_head(envelope: Mapping, events: list, public: bytes) -> bool:
    """True when ``envelope`` is this public key's signature of these events' head."""
    payload = verified_payload(envelope, public, CHAIN_HEAD_TYPE)
    if payload is None:
        return False
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict) or not isinstance(document.get("case_id"), str):
        return False
    if events and any(event.get("case_id") != document["case_id"] for event in events):
        return False
    return payload == head_payload(document["case_id"], events)
