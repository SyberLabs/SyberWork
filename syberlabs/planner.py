"""Planners propose an action. They do not admit it and they hold no executor credential."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Protocol

from syberlabs.canonical import canonical
from syberlabs.errors import Rejected
from syberlabs.targets import guard_request, trusted_origin


class PlannerRefusal(Rejected):
    """The planner did not return a proposal. This is not an admission decision."""


class Planner(Protocol):
    def propose(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Return ``{"action", "args"}`` or raise PlannerRefusal."""


def planning_context(contract: Mapping[str, Any], acceptance: list) -> dict[str, Any]:
    """What a planner may see: names and sources, not fact values or credentials.

    ``allowed_actions`` stays so an existing caller can still read that list.
    """
    actions = contract.get("actions") or {}
    arguments: dict[str, list] = {}
    required_facts = []
    for name, spec in actions.items():
        spec = spec if isinstance(spec, dict) else {}
        arguments[name] = sorted((spec.get("arguments") or {}))
        for fact in spec.get("required_facts") or []:
            if isinstance(fact, dict) and fact.get("key") and fact.get("source"):
                required_facts.append({"action": name, "key": fact["key"], "source": fact["source"]})
    return {
        "objective": contract.get("title") or contract.get("id"),
        "allowed_actions": list(actions),
        "arguments": arguments,
        "required_facts": required_facts,
        "acceptance": [{"id": item["id"], "passed": bool(item.get("passed"))} for item in acceptance],
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTP = urllib.request.build_opener(_NoRedirect)


class HttpPlanner:
    """POST the planning context to SYBERWORK_PLANNER_URL."""

    def ensure_configured(self) -> str:
        target = os.getenv("SYBERWORK_PLANNER_URL", "")
        try:
            trusted_origin(target)
        except Rejected:
            raise PlannerRefusal("planner_unconfigured", "configure an HTTPS or local planner endpoint") from None
        return target

    def propose(self, context: Mapping[str, Any]) -> dict[str, Any]:
        target = self.ensure_configured()
        guard_request(target)
        headers = {"Content-Type": "application/json"}
        if os.getenv("SYBERWORK_PLANNER_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["SYBERWORK_PLANNER_TOKEN"]
        request = urllib.request.Request(target, data=canonical(dict(context)).encode(), headers=headers, method="POST")
        try:
            with _HTTP.open(request, timeout=30) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise PlannerRefusal("planner_size", "planner response exceeds 1 MB")
            suggestion = json.loads(raw)
            if not isinstance(suggestion, dict) or not isinstance(suggestion.get("action"), str) or not isinstance(suggestion.get("args"), dict):
                raise PlannerRefusal("planner_shape", "planner must return {action, args}")
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise PlannerRefusal("planner_unavailable", str(exc)[:200]) from exc
        return suggestion


class StaticPlanner:
    """In-process planner that always returns one proposal. For tests."""

    def __init__(self, action: str, args: dict):
        self.action = action
        self.args = args

    def propose(self, context: Mapping[str, Any]) -> dict[str, Any]:
        return {"action": self.action, "args": dict(self.args)}
