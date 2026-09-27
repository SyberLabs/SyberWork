"""RFC 8032 Ed25519 in the standard library.

The arithmetic is not constant-time. The signing key stays in the caller's
process. It is not written into a case database.
"""

from __future__ import annotations

import hashlib
import os


_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


class _Point:
    def __init__(self, x: int, y: int):
        self.x = x % _P
        self.y = y % _P

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Point) and self.x == other.x and self.y == other.y

    def __add__(self, other: "_Point") -> "_Point":
        x1, y1, x2, y2 = self.x, self.y, other.x, other.y
        product = (_D * x1 * x2 * y1 * y2) % _P
        x3 = (x1 * y2 + y1 * x2) * pow(1 + product, _P - 2, _P)
        y3 = (y1 * y2 + x1 * x2) * pow(1 - product, _P - 2, _P)
        return _Point(x3, y3)


def _clamp(first_half: bytes) -> int:
    raw = bytearray(first_half)
    raw[0] &= 248
    raw[31] &= 63
    raw[31] |= 64
    return int.from_bytes(raw, "little")


def _recover_x(y: int, sign: int) -> int:
    if y >= _P:
        raise ValueError("y out of range")
    y2 = (y * y) % _P
    x2 = ((y2 - 1) * pow(_D * y2 + 1, _P - 2, _P)) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = (x * _I) % _P
    if (x * x - x2) % _P != 0:
        raise ValueError("not a curve point")
    if x == 0 and sign == 1:
        raise ValueError("invalid sign bit")
    if (x & 1) != sign:
        x = (-x) % _P
    return x


def _decode(data: bytes) -> _Point:
    if len(data) != 32:
        raise ValueError("point must be 32 bytes")
    raw = int.from_bytes(data, "little")
    sign = (raw >> 255) & 1
    y = raw & ((1 << 255) - 1)
    return _Point(_recover_x(y, sign), y)


def _encode(point: _Point) -> bytes:
    return (point.y | ((point.x & 1) << 255)).to_bytes(32, "little")


def _mul(point: _Point, scalar: int) -> _Point:
    result = _Point(0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = result + addend
        addend = addend + addend
        scalar >>= 1
    return result


def _base() -> _Point:
    y = (4 * pow(5, _P - 2, _P)) % _P
    return _Point(_recover_x(y, 0), y)


_B = _base()


def public_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    return _encode(_mul(_B, _clamp(digest[:32])))


def sign(seed: bytes, message: bytes) -> bytes:
    """Detached signature over ``message``. ``seed`` is the 32-byte private key."""
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = _clamp(digest[:32])
    public = _encode(_mul(_B, scalar))
    nonce = int.from_bytes(hashlib.sha512(digest[32:] + message).digest(), "little") % _L
    commitment = _encode(_mul(_B, nonce))
    challenge = int.from_bytes(hashlib.sha512(commitment + public + message).digest(), "little") % _L
    response = (nonce + challenge * scalar) % _L
    return commitment + response.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True when ``signature`` is an Ed25519 signature of ``message`` by ``public``."""
    if len(public) != 32 or len(signature) != 64:
        return False
    try:
        commitment = _decode(signature[:32])
        point = _decode(public)
    except ValueError:
        return False
    response = int.from_bytes(signature[32:], "little")
    if response >= _L:
        return False
    challenge = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    return _mul(_B, response) == commitment + _mul(point, challenge)


def generate() -> tuple[bytes, bytes]:
    """Return ``(seed, public_key)``. The seed is the private key."""
    seed = os.urandom(32)
    return seed, public_from_seed(seed)
