"""Event time. New envelopes store integer microseconds. Old rows keep float seconds.

Both forms are inside the hash. ``event_digest`` does not change. A row verifies
with the ``at`` value that was stored, so a history written with float seconds
still matches. Comparisons convert both forms to seconds.
"""

from __future__ import annotations


def stamp(seconds: int | float) -> int:
    """Integer microseconds for a new event's hashed ``at``."""
    if type(seconds) is bool:
        raise TypeError("time must be a number of seconds")
    return int(round(float(seconds) * 1_000_000))


def as_seconds(value: int | float) -> float:
    """Seconds since the epoch. Integers are microseconds. Floats are legacy seconds."""
    if type(value) is int:
        return value / 1_000_000
    return float(value)


def at_microseconds(at: int | float) -> int:
    """Export form. An integer ``at`` is already microseconds."""
    if type(at) is int:
        return at
    return stamp(at)
