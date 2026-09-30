"""Versioned Builder HTTP API.

Reads are projections. Writes are coordination records. The case routes in
``server.py`` are unchanged. The live tail is Server-Sent Events read from the
coordination log, not from a process-local queue.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs

from syberlabs.builder import PROTOCOL, REPLAY_LIMIT, visible_event
from syberlabs.canonical import canonical
from syberlabs.errors import Rejected

from .coordination import BuilderStore


MUTATION_ROLES = {"admin", "operator", "engineer", "manager", "decision"}


def _store(work) -> BuilderStore:
    return BuilderStore(work)


def _roles(user: dict) -> list[str]:
    roles = user.get("roles") or []
    if not isinstance(roles, list):
        raise Rejected("forbidden", "principal roles are required")
    return roles


def _actor(user: dict) -> str:
    name = user.get("name")
    if not isinstance(name, str) or not name:
        raise Rejected("unauthorized", "principal name required")
    return name


def _require_mutation(user: dict) -> None:
    if not set(_roles(user)) & MUTATION_ROLES:
        raise Rejected("forbidden", "builder mutation requires a coordination role")


def dispatch_get(work, user, parts: list[str], query: str, last_event_id: str | None):
    _actor(user)
    roles = _roles(user)
    store = _store(work)
    params = parse_qs(query)
    if len(parts) == 4 and parts[:3] == ["api", "builder", "work"]:
        return ("json", store.project(store.world(parts[3]), user), 200)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "work"] and parts[4] == "stream":
        after = 0
        if last_event_id not in (None, ""):
            if not str(last_event_id).isdigit():
                raise Rejected("invalid_cursor", "Last-Event-ID must be a sequence number")
            after = int(last_event_id)
        once = params.get("once", ["0"])[0] == "1"
        store.replay(parts[3], after, 1)
        return ("sse", iter_sse(work, parts[3], roles, after, once))
    if len(parts) == 4 and parts[:3] == ["api", "builder", "generations"]:
        return ("json", store.project(store.generation_view(parts[3]), user), 200)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "generations"] and parts[4] == "candidates":
        view = {
            "protocol": PROTOCOL,
            "kind": "CandidateProjection",
            "authoritative": False,
            "candidates": store.candidate_views(parts[3]),
        }
        return ("json", store.project(view, user), 200)
    if len(parts) == 4 and parts[:3] == ["api", "builder", "agents"]:
        return ("json", store.project(store.actor_view(parts[3]), user), 200)
    if len(parts) == 4 and parts[:3] == ["api", "builder", "architecture"]:
        baseline = params.get("baseline", [None])[0]
        return ("json", store.project(store.architecture_view(parts[3], baseline), user), 200)
    if len(parts) == 4 and parts[:3] == ["api", "builder", "integrity"]:
        return ("json", store.project(store.integrity_view(parts[3]), user), 200)
    raise Rejected("not_found", "builder route")


def dispatch_post(work, user, parts: list[str], data: dict) -> dict:
    actor = _actor(user)
    roles = _roles(user)
    store = _store(work)
    if parts == ["api", "builder", "policies"]:
        _require_mutation(user)
        return store.install_policy(data, actor)
    if len(parts) == 4 and parts[:3] == ["api", "builder", "work"]:
        _require_mutation(user)
        return store.open_work(parts[3], data.get("objective", ""), actor)
    if parts == ["api", "builder", "generations"]:
        _require_mutation(user)
        return store.create_generation(data, actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "generations"] and parts[4] == "approaches":
        _require_mutation(user)
        return store.register_approach(parts[3], data.get("descriptor", data), actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "generations"] and parts[4] == "seal":
        _require_mutation(user)
        return store.seal(parts[3], actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "generations"] and parts[4] == "launch":
        _require_mutation(user)
        return store.launch(parts[3], actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "generations"] and parts[4] == "close":
        _require_mutation(user)
        return store.close_generation(parts[3], actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "approaches"] and parts[4] == "revisions":
        _require_mutation(user)
        return store.revise_approach(parts[3], data.get("descriptor", data), actor)
    if parts == ["api", "builder", "agents"]:
        _require_mutation(user)
        return store.register_agent(data, actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "agents"] and parts[4] == "activity":
        if not (set(roles) & MUTATION_ROLES or "agent" in roles):
            raise Rejected("forbidden", "agent activity requires a coordination or agent role")
        return store.record_activity(parts[3], data, actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "agents"] and parts[4] == "commands":
        _require_mutation(user)
        return store.command(parts[3], data.get("command"), data.get("body") or {}, actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "generations"] and parts[4] == "candidates":
        _require_mutation(user)
        return store.link_candidate(parts[3], data["approach_id"], data["candidate_id"], data.get("changed_paths", []), actor)
    if parts == ["api", "builder", "feedback"]:
        return store.record_feedback(data, actor, roles)
    if parts == ["api", "builder", "integrity"]:
        return store.record_integrity(data, actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "generations"] and parts[4] == "selection":
        _require_mutation(user)
        return store.select(parts[3], actor, roles)
    if parts == ["api", "builder", "architecture"]:
        _require_mutation(user)
        return store.publish_architecture(data, actor)
    if parts == ["api", "builder", "prototypes"]:
        _require_mutation(user)
        return store.register_prototype(data, actor)
    if len(parts) == 5 and parts[:3] == ["api", "builder", "prototypes"] and parts[4] == "state":
        _require_mutation(user)
        return store.set_prototype_state(parts[3], data.get("state"), actor)
    raise Rejected("not_found", "builder route")


def iter_sse(work, case_id: str, roles: list[str], after_seq: int, once: bool):
    """Monotonic ``id`` fields are coordination sequence numbers."""
    yield ": heartbeat\n\n"
    store = _store(work)
    events, truncated = store.replay(case_id, after_seq, REPLAY_LIMIT)
    if truncated:
        yield ": replay-truncated\n\n"
    cursor = after_seq
    for event in events:
        cursor = event["seq"]
        if visible_event(event, roles):
            yield _frame(event)
    if once:
        return
    while True:
        time.sleep(1)
        fresh, _truncated = store.replay(case_id, cursor, REPLAY_LIMIT)
        if not fresh:
            yield ": heartbeat\n\n"
            continue
        for event in fresh:
            cursor = event["seq"]
            if visible_event(event, roles):
                yield _frame(event)


def _frame(event: dict) -> str:
    return f"id: {event['seq']}\ndata: {canonical(event)}\n\n"
