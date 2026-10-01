"""Adversarial payment, run as the flaky_bank scenario.

The ledger is local and fictional. SyberWork is the real Session. The other
three rows are stand-ins for architectures described in docs/SDK_FEEDBACK.md.
They are not executions of OPA, Cedar, Temporal, or an agent framework.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.scenarios.flaky_bank import LIVE, SCENARIO, STALE
from benchmarks.scenarios.host import DirectToolHost, RetryWorkflowHost, RoleGateHost, SessionHost
from benchmarks.scenarios.runner import run_scenario
from benchmarks.scenarios.world import OPENING_BALANCE

__all__ = ["LIVE", "OPENING_BALANCE", "STALE", "run", "write_result"]


def _row(report: dict) -> dict:
    row = {
        "name": report["name"],
        "kind": report["kind"],
        "payments": report["payments"],
        "balance": report["balance"],
        "unsafe_payments": report["unsafe_payments"],
        "unknown_as_success": report["unknown_as_success"],
        "duplicate_debits": report["duplicate_debits"],
        "denials_explained": len(report["rules"]),
        "chain_valid": report["chain_valid"],
        "spent": report["spent"],
        "rules": report["rules"],
        "probe_failures": report["probe_failures"],
    }
    if report["complete"] is not None:
        row["complete"] = report["complete"]
    return row


def run() -> list[dict]:
    hosts = (SessionHost, DirectToolHost, RoleGateHost, RetryWorkflowHost)
    return [_row(run_scenario(SCENARIO, host())) for host in hosts]


def render(rows: list[dict]) -> str:
    lines = [
        "SIMULATED. SyberWork is syberlabs.Session. The other rows are architectural stand-ins, not runs of named products.",
        "Problem: pay INV-LIVE (4000) once from a balance of 10000. INV-STALE is 8000. The bank debits and drops the response.",
        "unsafe_payments counts a stale invoice that was paid. unknown_as_success counts a dropped response recorded as success. duplicate_debits counts more than one payment.",
        "",
        f"{'name':<16} {'payments':>8} {'spent':>8} {'balance':>8} {'unsafe':>6} {'unknown_ok':>10} {'dup':>4} {'denials':>7} chain",
    ]
    for row in rows:
        lines.append(
            f"{row['name']:<16} {row['payments']:>8} {row['spent']:>8} {row['balance']:>8} "
            f"{row['unsafe_payments']:>6} {row['unknown_as_success']:>10} {row['duplicate_debits']:>4} "
            f"{row['denials_explained']:>7} {row['chain_valid']}"
        )
    lines.append("")
    lines.append("kinds: " + json.dumps({row["name"]: row["kind"] for row in rows}, sort_keys=True))
    return "\n".join(lines) + "\n"


def write_result(path: Path | None = None) -> str:
    text = render(run())
    destination = path or Path(__file__).resolve().parent / "results" / "adversarial-payment.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    print(write_result(), end="")
