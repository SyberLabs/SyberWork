"""RFC 8785 JSON Canonicalization Scheme for the types this SDK stores.

This is not the hash-chain encoding. ``canonical`` still links events.
A JCS digest is written beside that link so another language can check
the same bytes. Non-finite floats and integers outside the IEEE-754
safe range are rejected instead of being hashed.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from syberlabs.events import DIGEST_FIELDS


class JcsError(ValueError):
    """The value cannot be serialized as RFC 8785 JSON."""


_INT_MAX = 2**53 - 1
_INT_MIN = -(2**53) + 1

_ESCAPE = re.compile(r'[\x00-\x1f\\"\b\f\n\r\t]')
_ESCAPE_DCT = {chr(i): f"\\u{i:04x}" for i in range(0x20)}
_ESCAPE_DCT.update({
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
})


def _string(value: str) -> bytes:
    def replace(match: re.Match) -> str:
        return _ESCAPE_DCT[match.group(0)]

    try:
        encoded = _ESCAPE.sub(replace, value).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JcsError("string is not Unicode") from exc
    return b'"' + encoded + b'"'


def _number(value: float) -> bytes:
    """ECMA-262 number format, as amended by RFC 8785 section 3.2.2.3."""
    if math.isnan(value) or math.isinf(value):
        raise JcsError("non-finite number")
    if value == 0:
        return b"0"
    if value < 0:
        return b"-" + _number(-value)

    text = str(value)
    exponent = ""
    exponent_value = 0
    marker = text.find("e")
    if marker > 0:
        exponent = text[marker:]
        if len(exponent) > 2 and exponent[2] == "0":
            exponent = exponent[:2] + exponent[3:]
        text = text[:marker]
        exponent_value = int(exponent[1:])

    first = text
    dot = ""
    last = ""
    point = text.find(".")
    if point > 0:
        dot = "."
        first = text[:point]
        last = text[point + 1:]
    if last == "0":
        dot = ""
        last = ""

    if 0 < exponent_value < 21:
        first += last
        last = ""
        dot = ""
        exponent = ""
        pad = exponent_value - len(first)
        while pad >= 0:
            pad -= 1
            first += "0"
    elif -7 < exponent_value < 0:
        last = first + last
        first = "0"
        dot = "."
        exponent = ""
        shift = exponent_value
        while shift < -1:
            shift += 1
            last = "0" + last
    return f"{first}{dot}{last}{exponent}".encode("ascii")


def jcs_bytes(value: Any) -> bytes:
    """Serialize ``value`` to RFC 8785 bytes."""
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int):
        if value < _INT_MIN or value > _INT_MAX:
            raise JcsError("integer exceeds the JSON safe range")
        return str(value).encode("ascii")
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return b"[]"
        parts = [jcs_bytes(item) for item in value]
        return b"[" + b",".join(parts) + b"]"
    if isinstance(value, dict):
        if not value:
            return b"{}"
        try:
            ordered = sorted(value.items(), key=lambda item: item[0].encode("utf-16-be"))
        except AttributeError as exc:
            raise JcsError("object keys must be strings") from exc
        parts = []
        for key, item in ordered:
            if not isinstance(key, str):
                raise JcsError("object keys must be strings")
            parts.append(_string(key) + b":" + jcs_bytes(item))
        return b"{" + b",".join(parts) + b"}"
    raise JcsError(f"unsupported type: {type(value).__name__}")


def jcs_digest(value: Any) -> str:
    return hashlib.sha256(jcs_bytes(value)).hexdigest()


def jcs_digest_or_none(value: Any) -> str | None:
    try:
        return jcs_digest(value)
    except JcsError:
        return None


def envelope_jcs(event: dict) -> str | None:
    """JCS SHA-256 of the fields the chain hashes. Not itself a chain link."""
    return jcs_digest_or_none({key: event[key] for key in DIGEST_FIELDS})
