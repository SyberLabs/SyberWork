"""When a thread's history may be deleted, and what must survive it.

The rule lives in the organization's policy, not in the thread, as an
optional ``retention`` section::

    "retention": {"effect_history_days": 365, "unaccepted_history_days": 0}

- A thread with an effect whose outcome is not known is never deleted.
  Its history is what reconciliation needs.
- A thread whose history proves an effect (an accepted change, a push, a
  pull request, a publish) may be deleted only after ``effect_history_days``
  since its last effect (default 365). Deleting it leaves a retention
  receipt: the chain head hash, the event count, and for each effect its
  action, proposal id, destination, external id, and time. The receipt
  holds no objective, file content, or event bodies.
- Any other thread may be deleted after ``unaccepted_history_days`` since
  it was created (default 0).

Forgetting deletes records. It never undoes an effect: an accepted branch,
a pushed ref, or a pull request stays where it is.
"""

from __future__ import annotations

from syberlabs.clock import as_seconds
from syberlabs.errors import Rejected
from syberlabs.evidence import verified_reconciliation

DEFAULTS = {"effect_history_days": 365, "unaccepted_history_days": 0}
DAY = 86_400


def check(policy: dict) -> dict:
    """Validate a policy's retention section and return it with defaults."""
    section = policy.get("retention", {})
    if not isinstance(section, dict) or set(section) - set(DEFAULTS):
        raise Rejected("invalid_policy", "retention fields are " + ", ".join(sorted(DEFAULTS)))
    for key, value in section.items():
        if type(value) is not int or not 0 <= value <= 36_500:
            raise Rejected("invalid_policy", f"retention.{key} must be 0 to 36500 days")
    return {**DEFAULTS, **section}


def effects(history: list) -> list[dict]:
    """Effects this history proves, from success events and verified reconciliations."""
    proposals = {e["body"]["id"]: e["body"] for e in history if e["kind"] == "proposed"}
    found = []
    for event in history:
        body = event["body"]
        if event["kind"] == "effect_succeeded":
            output = body.get("output") or {}
            destination = output.get("destination") or output.get("ref")
            external = output.get("external_id") or output.get("new")
        elif verified_reconciliation(event):
            destination, external = None, body["proof"]["external_id"]
        else:
            continue
        found.append({"action": body["action"], "proposal_id": body["proposal_id"], "destination": destination,
                      "external_id": external, "args": (proposals.get(body["proposal_id"]) or {}).get("args"),
                      "at": as_seconds(event["at"])})
    return found


def unresolved(history: list) -> list[str]:
    done = {e["body"]["proposal_id"] for e in history
            if e["kind"] in ("effect_succeeded", "effect_rejected") or verified_reconciliation(e)}
    return [e["body"]["proposal_id"] for e in history if e["kind"] == "effect_started" and e["body"]["proposal_id"] not in done]


def decide(history: list, created: float, rules: dict, now: float) -> dict:
    """Whether the thread may be forgotten now, and whether a receipt is required."""
    if unresolved(history):
        return {"allowed": False, "reason": "retention_unresolved", "receipt": True,
                "detail": "an effect has no known outcome; run syberlabs recover first"}
    proven = effects(history)
    if proven:
        last = max(item["at"] for item in proven)
        until = last + rules["effect_history_days"] * DAY
        if now < until:
            return {"allowed": False, "reason": "retention_required", "receipt": True, "until": until,
                    "detail": f"history that proves an effect is kept {rules['effect_history_days']} days after it"}
        return {"allowed": True, "reason": "retention_elapsed", "receipt": True, "effects": proven}
    until = created + rules["unaccepted_history_days"] * DAY
    if now < until:
        return {"allowed": False, "reason": "retention_required", "receipt": False, "until": until,
                "detail": f"threads are kept {rules['unaccepted_history_days']} days after they start"}
    return {"allowed": True, "reason": "no_effects", "receipt": False, "effects": []}
