"""Compiled-path binding shared by the in-memory session and the SQLite store."""

from __future__ import annotations

from syberlabs.errors import Rejected
from syberlabs.evidence import verified_reconciliation


def next_compiled(contract: dict, history: list) -> str | None:
    completed = {
        event["body"]["action"]
        for event in history
        if event["kind"] == "effect_succeeded" or verified_reconciliation(event)
    }
    return next((action for action in contract.get("compiled_path", []) if action not in completed), None)


def bind_arguments(contract: dict, history: list, action: str) -> dict:
    """Build proposal arguments from the contract's fact and version bindings."""
    args = {}
    for param, binding in contract["actions"][action].get("arguments", {}).items():
        name = binding.removeprefix("version:").removeprefix("fact:").split(".")[0]
        fact = next(
            (event for event in reversed(history) if event["kind"] == "observed" and event["body"]["key"] == name),
            None,
        )
        if not fact:
            raise Rejected("missing_fact", name)
        value = fact["body"]["version"] if binding.startswith("version:") else fact["body"]["value"]
        if binding.startswith("fact:"):
            for field in binding[5:].split(".")[1:]:
                if not isinstance(value, dict) or field not in value:
                    raise Rejected("missing_fact_field", binding)
                value = value[field]
        args[param] = value
    return args
