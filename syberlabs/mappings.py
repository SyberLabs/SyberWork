"""Translate one SyberRuntime stabilize outcome into protocol objects.

This function does not import or run SyberRuntime. The caller passes the
outcome it already observed. The other rows in ``spec/MAPPINGS.md`` stay
proposed.
"""

from __future__ import annotations

from typing import Any, Mapping


def translate_stabilize(outcome: Mapping[str, Any]) -> dict:
    """Map a stabilize result onto an admission decision, an effect, or neither.

    ``event`` is one of:

    - ``operation_appended``: an operation was logged. Open obligations that
      are not floor-blocking become ``AdmissionDecision`` ``allowed``.
      ``floor_blocking: true`` becomes ``denied``.
    - ``stabilization_blocked``: ``StabilizationBlockedError`` because floor
      obligations are open. ``AdmissionDecision`` ``denied``.
    - ``stabilize_appended``: a ``Stabilize`` operation was appended.
      ``EffectOutcome`` ``succeeded``.
    - ``chain_mismatch``: the operation log failed its hash check. This is
      not an admission decision and not an effect outcome.
    """
    event = outcome.get("event")
    if event == "chain_mismatch":
        return {"mapped": False, "reason": "integrity_failure"}
    if event == "stabilization_blocked":
        return {
            "mapped": True,
            "object": "AdmissionDecision",
            "status": "denied",
            "reason": "floor_obligations_open",
        }
    if event == "operation_appended":
        if outcome.get("floor_blocking"):
            return {
                "mapped": True,
                "object": "AdmissionDecision",
                "status": "denied",
                "reason": "floor_obligations_open",
            }
        return {
            "mapped": True,
            "object": "AdmissionDecision",
            "status": "allowed",
            "reason": "obligations_open",
        }
    if event == "stabilize_appended":
        return {"mapped": True, "object": "EffectOutcome", "state": "succeeded"}
    raise ValueError("unrecognized stabilize outcome")
