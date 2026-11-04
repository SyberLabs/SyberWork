"""Reviewer independence by provider family, not by the name a caller typed.

A proposer and a reviewer are independent when the models that served them belong to different provider
families. The family is read from what was recorded about the party: a kernel candidate's
``signal.harness.model`` (``docs/EVOLUTION.md``), a party's own ``model`` or ``family``, or its
``provider.model``. A party whose family cannot be told is not independent of anyone: the check refuses
rather than guesses. Small and importable on purpose, so ``syberlabs.critic.independent`` can adopt it; this
module changes nothing in ``critic``.
"""

from __future__ import annotations

FAMILIES = {
    "anthropic": ("claude",),
    "openai": ("gpt-", "gpt4", "gpt5", "o1", "o3", "o4", "chatgpt", "codex"),
    "google": ("gemini", "gemma"),
    "xai": ("grok",),
    "meta": ("llama",),
    "mistral": ("mistral", "mixtral", "codestral", "devstral"),
    "deepseek": ("deepseek",),
    "alibaba": ("qwen",),
    "amazon": ("nova",),
    "cohere": ("command",),
}


def model_of(party: dict | None) -> str | None:
    """The model id recorded for a party: ``signal.harness.model``, ``model``, or ``provider.model``."""
    if not isinstance(party, dict):
        return None
    harness = (party.get("signal") or {}).get("harness") if isinstance(party.get("signal"), dict) else None
    for candidate in ((harness or {}).get("model"), party.get("model"), (party.get("provider") or {}).get("model")
                      if isinstance(party.get("provider"), dict) else None):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def family(party: dict | None) -> str | None:
    """The provider family of a party, or None when it is not recorded and cannot be told from the model id.
    An explicit ``family`` wins over the model id; an unknown family name is still None."""
    if isinstance(party, dict) and isinstance(party.get("family"), str) and party["family"].lower() in FAMILIES:
        return party["family"].lower()
    model = model_of(party)
    if model is None:
        return None
    lowered = model.lower()
    for name, prefixes in FAMILIES.items():
        if any(lowered.startswith(prefix) or ("/" + prefix) in lowered for prefix in prefixes):
            return name
    return None


def independence(proposer: dict | None, reviewer: dict | None) -> tuple[bool, str]:
    """Whether ``reviewer`` may judge what ``proposer`` made, and why. Independent only when both families
    are known and differ; an unknown family refuses."""
    a, b = family(proposer), family(reviewer)
    if a is None or b is None:
        who = "the proposer" if a is None else "the reviewer"
        return False, f"{who}'s provider family is not recorded (no model id or an unknown one): refuse"
    if a == b:
        return False, f"the proposer and the reviewer are both served by {a}: not independent"
    return True, f"proposer {a}, reviewer {b}"
