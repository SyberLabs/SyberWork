"""Probes compare a report with an expectation. They do not score it."""

from __future__ import annotations


class Probe:
    def __init__(self, identifier: str, **expected):
        self.id = identifier
        self.expected = expected


def evaluate(probe: Probe, report: dict) -> dict:
    """Return one finding. A miss does not raise."""
    observed: dict = {}
    passed = True
    decisions = report.get("decisions") or []
    if "reason" in probe.expected:
        reasons = [item.get("reason") for item in decisions]
        observed["reason"] = probe.expected["reason"] if probe.expected["reason"] in reasons else reasons
        passed = passed and probe.expected["reason"] in reasons
    if "code" in probe.expected:
        codes = [item.get("code") for item in decisions]
        observed["code"] = probe.expected["code"] if probe.expected["code"] in codes else codes
        passed = passed and probe.expected["code"] in codes
    for key in ("effect_started_before_review", "unknown_status", "unknown_succeeded", "payments", "spent", "chain_valid", "complete"):
        if key not in probe.expected:
            continue
        observed[key] = report.get(key)
        passed = passed and report.get(key) == probe.expected[key]
    return {"id": probe.id, "passed": passed, "observed": observed, "expected": dict(probe.expected)}
