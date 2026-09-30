"""Versioned Builder HTTP API.

Reads are projections. Writes are coordination records. The case routes in
``server.py`` are unchanged. The live tail is Server-Sent Events read from the
coordination log, not from a process-local queue. The poll below is a
development transport: the database remains the source of truth.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs

from syberlabs.builder import COORDINATION_ROLES, PROTOCOL, REPLAY_LIMIT, project, visible_event
from syberlabs.canonical import canonical
from syberlabs.errors import Rejected

from .coordination import BuilderStore


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
    if not set(_roles(user)) & COORDINATION_ROLES:
        raise Rejected("forbidden", "builder mutation requires a coordination role")


def dispatch_get(work, user, parts: list[str], query: str, last_event_id: str | None):
    """Return a role-framed projection, or an iterator of SSE frames for the stream."""
    _actor(user)
    roles = _roles(user)
    store = BuilderStore(work)
    params = parse_qs(query)
    match parts[2:]:
        case ["work", case_id]:
            view = store.world(case_id)
        case ["work", case_id, "stream"]:
            if last_event_id not in (None, "") and not str(last_event_id).isdecimal():
                raise Rejected("invalid_cursor", "Last-Event-ID must be a sequence number")
            after = int(last_event_id or 0)
            store.replay(case_id, after, 1)
            return iter_sse(work, case_id, roles, after, params.get("once", ["0"])[0] == "1")
        case ["generations", generation_id]:
            view = store.generation_view(generation_id)
        case ["generations", generation_id, "candidates"]:
            view = {
                "protocol": PROTOCOL,
                "kind": "CandidateProjection",
                "authoritative": False,
                "candidates": store.candidate_views(generation_id),
            }
        case ["agents", agent_id]:
            view = store.actor_view(agent_id)
        case ["architecture", snapshot_id]:
            view = store.architecture_view(snapshot_id, params.get("baseline", [None])[0])
        case ["integrity", target]:
            view = store.integrity_view(
                target,
                case_id=params.get("case_id", [None])[0],
                target_kind=params.get("target_kind", [None])[0],
                generation_id=params.get("generation_id", [None])[0],
            )
        case _:
            raise Rejected("not_found", "builder route")
    return project(view, user)


def dispatch_post(work, user, parts: list[str], data: dict) -> dict:
    actor = _actor(user)
    roles = _roles(user)
    store = BuilderStore(work)
    match parts[2:]:
        case ["feedback"]:
            return store.record_feedback(data, actor, roles)
        case ["integrity"]:
            return store.record_integrity(data, actor, principal_kind=user.get("kind"))
        case ["agents", agent_id, "activity"]:
            if not (set(roles) & COORDINATION_ROLES or "agent" in roles):
                raise Rejected("forbidden", "agent activity requires a coordination or agent role")
            return store.record_activity(agent_id, data, actor, roles)
    _require_mutation(user)
    match parts[2:]:
        case ["policies"]:
            return store.install_policy(data, actor)
        case ["work", case_id]:
            return store.open_work(case_id, data.get("objective", ""), actor)
        case ["generations"]:
            return store.create_generation(data, actor)
        case ["generations", generation_id, "approaches"]:
            return store.register_approach(generation_id, data.get("descriptor", data), actor)
        case ["generations", generation_id, "seal"]:
            return store.seal(generation_id, actor)
        case ["generations", generation_id, "launch"]:
            return store.launch(generation_id, actor)
        case ["generations", generation_id, "close"]:
            return store.close_generation(generation_id, actor)
        case ["approaches", approach_id, "revisions"]:
            return store.revise_approach(approach_id, data.get("descriptor", data), actor)
        case ["agents"]:
            return store.register_agent(data, actor)
        case ["agents", agent_id, "commands"]:
            return store.command(agent_id, data.get("command"), data.get("body") or {}, actor)
        case ["generations", generation_id, "candidates"]:
            return store.link_candidate(
                generation_id, data["approach_id"], data["candidate_id"], actor,
                changed_paths=data.get("changed_paths"),
            )
        case ["generations", generation_id, "selection"]:
            return store.select(generation_id, actor, roles)
        case ["architecture"]:
            return store.publish_architecture(data, actor)
        case ["prototypes"]:
            return store.register_prototype(data, actor)
        case ["prototypes", prototype_id, "state"]:
            return store.set_prototype_state(prototype_id, data.get("state"), actor)
        case _:
            raise Rejected("not_found", "builder route")


def iter_sse(work, case_id: str, roles: list[str], after_seq: int, once: bool):
    """Development transport. The database is the source of truth.

    This loop polls through the cell's single transaction path about once a
    second. That is enough for tests and a local console. It is not the
    production wakeup path: a later revision should NOTIFY on PostgreSQL, or
    signal a local condition on SQLite, and then read the durable log after
    the cursor.
    """
    store = BuilderStore(work)
    cursor = after_seq
    yield ": heartbeat\n\n"
    while True:
        events, truncated = store.replay(case_id, cursor, REPLAY_LIMIT)
        if truncated:
            yield ": replay-truncated\n\n"
        for event in events:
            cursor = event["seq"]
            if visible_event(event, roles):
                yield _frame(event)
        if once:
            return
        if not events:
            yield ": heartbeat\n\n"
        time.sleep(1)


def _frame(event: dict) -> str:
    return f"id: {event['seq']}\ndata: {canonical(event)}\n\n"
