"""Ingest: turn a raw payload into a record."""

from fxapp import rules


def load(payload):
    record = {"part": str(payload.get("part", "")), "quantity": int(payload.get("quantity", 0))}
    return rules.normalize(record)
