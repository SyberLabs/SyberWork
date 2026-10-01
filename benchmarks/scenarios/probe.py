"""Probes compare a report with an expectation. They do not score it."""

from __future__ import annotations

PROBE_FIELDS = frozenset({
    "reason",
    "code",
    "effect_started_before_review",
    "unknown_status",
    "unknown_succeeded",
    "payments",
    "spent",
    "balance",
    "duplicate_debits",
    "unsafe_payments",
    "chain_valid",
    "complete",
})


class Probe:
    def __init__(self, identifier: str, **expected):
        unknown = set(expected) - PROBE_FIELDS
        if unknown:
            raise ValueError("unsupported probe field: " + ", ".join(sorted(unknown)))
        self.id = identifier
        self.expected = expected


def evaluate(probe: Probe, report: dict) -> dict:
    """Return one finding. A miss does not raise. Every expected field is compared."""
    observed: dict = {}
    passed = True
    decisions = report.get("decisions") or []
    for key, want in probe.expected.items():
        if key == "reason":
            reasons = [item.get("reason") for item in decisions]
            observed[key] = want if want in reasons else reasons
            passed = passed and want in reasons
        elif key == "code":
            codes = [item.get("code") for item in decisions]
            observed[key] = want if want in codes else codes
            passed = passed and want in codes
        else:
            observed[key] = report.get(key)
            passed = passed and report.get(key) == want
    return {"id": probe.id, "passed": passed, "observed": observed, "expected": dict(probe.expected)}
