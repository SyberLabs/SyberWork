"""RFC 8032 Ed25519 in the standard library.

The arithmetic is not constant-time. The signing key stays in the caller's
process. It is not written into a case database.
"""

from __future__ import annotations

import functools
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


def _seed_bytes(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    return bytes(seed)


@functools.lru_cache(maxsize=8)
def _public_for(seed: bytes) -> bytes:
    """The public key of ``seed``, derived once per seed and kept for the eight most recent seeds.

    Only this module computes the key that enters a signature's challenge; no caller can supply one. Two signatures
    of one message under one seed with two different keys there would reveal the private scalar. The cache keeps
    up to eight seeds for the life of the process, as a transparency log or a witness already keeps its own."""
    digest = hashlib.sha512(seed).digest()
    return _encode(_mul(_B, _clamp(digest[:32])))


def public_from_seed(seed: bytes) -> bytes:
    return _public_for(_seed_bytes(seed))


def sign(seed: bytes, message: bytes) -> bytes:
    """Detached signature over ``message``. ``seed`` is the 32-byte private key.

    The public key is derived from ``seed`` here (``_public_for``, once per seed), so signing again under a seed
    already used costs one scalar multiplication, not two."""
    seed = _seed_bytes(seed)
    digest = hashlib.sha512(seed).digest()
    scalar = _clamp(digest[:32])
    public = _public_for(seed)
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


KEY_PROBLEMS = ("not_32_bytes", "not_a_point", "small_order", "mixed_order")


def key_problem(public: object) -> str | None:
    """Why ``public`` is not a key only its seed's holder can sign under, or None when it is one.

    ``verify`` takes any 32 bytes, as RFC 8032 does. Under the identity point (``01 00 … 00``) the signature
    ``R = B, s = 1`` verifies every message; under another point of small order, ``R = B + j·A`` for one of at most
    eight ``j`` does. Whoever chooses such a key can sign without a seed, so a caller that takes a public key from
    outside its process checks it here first. A key ``public_from_seed`` derives is a point of the prime-order subgroup: it decodes,
    ``8·A`` is not the identity, and ``L·A`` is. The answer is one of ``KEY_PROBLEMS``, and stays that word:

    - ``not_32_bytes``: not a ``bytes`` of length 32;
    - ``not_a_point``: no curve point has this encoding, or it is not the canonical one (``y`` at or above ``p``,
      or ``x = 0`` with the sign bit set);
    - ``small_order``: ``8·A`` is the identity, the identity included;
    - ``mixed_order``: a prime-order point plus a small-order one; no seed derives it.
    """
    if not isinstance(public, bytes) or len(public) != 32:
        return "not_32_bytes"
    return _point_problem(public)


@functools.lru_cache(maxsize=32)
def _point_problem(public: bytes) -> str | None:
    """``key_problem`` past the length, kept for the 32 most recent keys: ``L·A`` costs about as much as a third of a
    ``verify``, and a reader checks the same configured key on every open."""
    try:
        point = _decode(public)
    except ValueError:
        return "not_a_point"
    identity = _Point(0, 1)
    if _mul(point, 8) == identity:
        return "small_order"
    if _mul(point, _L) != identity:
        return "mixed_order"
    return None


def usable_key(public: object) -> bool:
    """Whether ``public`` is a prime-order Ed25519 point: ``key_problem`` has nothing to say about it."""
    return key_problem(public) is None


def generate() -> tuple[bytes, bytes]:
    """Return ``(seed, public_key)``. The seed is the private key."""
    seed = os.urandom(32)
    return seed, public_from_seed(seed)
