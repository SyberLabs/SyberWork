"""Crash-recoverable effect worker.

The API claims an effect and returns. This process leases the obligation,
performs the destination call, and settles the case. A lease that expired
during a call is recorded as unknown so reconciliation, not a second guess,
decides the outcome.
"""

from __future__ import annotations

import time

from syberlabs.errors import Rejected


def run_once(work, *, now: float | None = None) -> dict | None:
    """Settle one obligation. ``None`` means the queue is empty."""
    if work.effects != "worker":
        raise Rejected("worker_disabled", "this cell settles effects inline")
    job = work.lease_obligation(now=now)
    if job is None:
        return None
    if job["state"] == "interrupted":
        return work.settle_interrupted(job)
    return work.finish_claimed(job["case_id"], job["proposal_id"], job["proposal"], job["action"], job["claim"])


def run_available(work, *, limit: int = 100) -> list[dict]:
    """Settle queued obligations, stopping at an empty queue or ``limit``."""
    done = []
    for _ in range(limit):
        result = run_once(work)
        if result is None:
            break
        done.append(result)
    return done


def serve(work, *, interval: float = 0.5) -> None:
    """Poll until the process is stopped. One worker, one cell."""
    while True:
        if run_once(work) is None:
            time.sleep(interval)
