"""Work: a ticket from intake is planned, claimed by one agent, and built in a Build Thread whose scope is its plan.

The four build rules, as records on the factory ledger instead of labels on an issue:

1. **Only planned work.** ``claim`` refuses a ticket whose request is not ``agent_ready`` as it reads now, that
   has no plan for that text, or whose plan waits for a person.
2. **One agent per ticket.** A claim is appended under the ledger's lock after the check, so of two agents
   claiming at once one is refused (``ticket_claimed``). A claim lapses after its ``ttl``; the claimant renews it
   by claiming again, and the claimant or a person releases it.
3. **Follow the plan.** ``start`` writes a contract whose mutable scope is exactly the plan's files and starts a
   Build Thread on it. The kernel refuses a candidate that changes any other path, so the rule is enforced where
   every other scope is, not by asking the agent.
4. **The change names its request.** The thread's objective begins ``Fixes owner/repository#N`` for a GitHub
   issue, and the pull request body SyberWork writes carries the objective; GitHub reads those words as a
   closing keyword when the pull request merges into the default branch.

A ticket's status is derived from the ledger every time it is asked for (``state``): ``held`` (its request is
not ``agent_ready`` now), ``open`` (no plan for the text as it reads now), ``plan_held`` (the plan waits for a
person), ``plan_rejected``, ``planned``, ``claimed``, ``working`` (a thread was started) and ``ready`` (the
thread holds an accepted candidate; pushing and merging are later acts). Nothing is stored about a ticket except events, so an
edited request, a lapsed claim or a new plan changes the status without anyone updating it.

``make_plan`` runs the planner the way intake runs a decider (``intake.run_reader``: scrubbed environment, the
operator's allow-list, a time limit) with one difference: its working directory is a temporary checkout of the
base commit, so a planning agent can read the code and nothing else of the operator's (no working tree, no
untracked file, no ledger). What it prints is checked and assessed by ``plan.py``; this module records the outcome.

What this module never does: propose a change, run a check, accept, push, open a pull request or merge. ``start``
ends with a thread waiting for a proposer, and accepting is a person's step, as on every thread that was not
started unattended.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from syberlabs.build import PROMOTE, Kit
from syberlabs.canonical import canonical, digest
from syberlabs.errors import Rejected
from syberlabs.factory import intake
from syberlabs.factory import plan as planmod
from syberlabs.gitspace import Repo

EVENTS = ("ticket_planned", "plan_resolved", "ticket_claimed", "ticket_released", "work_started", "work_ready")
STATUSES = ("held", "open", "plan_held", "plan_rejected", "planned", "claimed", "working", "ready")
ARTIFACTS = "plans"
DEFAULT_TTL = 4 * 3600
MIN_TTL, MAX_TTL = 60, 7 * 24 * 3600
MAX_PLAN_BYTES = 256 * 1024
MICRO = 1_000_000
# A status as a label a tracker shows, for an operator who mirrors the ledger there. ``held`` has no entry: the
# request's own route says why (``intake.label``). A rejected plan leaves the ticket where an open one is.
LABELS = {"open": "agent-ready", "plan_held": "needs-human", "plan_rejected": "agent-ready", "planned": "planned",
          "claimed": "agent:working", "working": "agent:working", "ready": "ready"}


# -- lookups ---------------------------------------------------------------------------------------------------------

def find(ledger, text: str, events: list[dict] | None = None) -> dict:
    """The intake item behind a ticket: by ticket id (or its first eight or more characters), else by whatever
    names a request for ``intake.find``."""
    known = intake.items(ledger, events)
    hits = [item for item in known.values()
            if intake.ticket_id(item["source"]) == text or (len(text) >= 8 and intake.ticket_id(item["source"]).startswith(text))]
    return hits[0] if len(hits) == 1 else intake.find(ledger, text, events)


def _notes(events: list[dict], tid: str) -> list[dict]:
    return [e for e in events if e["kind"] == "note" and e["body"].get("note") in EVENTS and e["body"].get("ticket_id") == tid]


def _actor(value) -> dict:
    if not isinstance(value, dict) or value.get("kind") not in ("human", "model", "compiled") or not value.get("id"):
        raise Rejected("actor_invalid", "an actor is {kind, id} with kind human, model or compiled")
    return {"kind": value["kind"], "id": str(value["id"])}


def _name(actor: dict) -> str:
    return f"{actor['kind']}:{actor['id']}"


def _reason(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300 or not value.isprintable():
        raise Rejected("work_reason", f"{what} says why, on one line of at most 300 characters")
    if intake.credential_shapes(value):
        raise Rejected("work_reason", "the reason looks like it holds a credential; it would stay on the ledger")
    return value.strip()


def state(ledger, text: str, *, events: list[dict] | None = None, now: int | None = None) -> dict:
    """One ticket as the ledger has it now. ``now`` is microseconds since the epoch (the unit of an event's
    ``at``); a claim is live while ``now`` is before its event's ``at`` plus its ``ttl_seconds``.

    ``plan`` is the newest ``ticket_planned`` for the request's current text, with a person's later resolution of
    that very plan applied; ``claim`` is the newest claim made on that plan and not released since; ``work`` the
    newest thread started on it; ``ready`` the record that the thread holds an accepted candidate."""
    events = ledger.events() if events is None else events
    now = int(time.time() * MICRO) if now is None else now
    item = find(ledger, text, events)
    tid = intake.ticket_id(item["source"])
    out = {"ticket_id": tid, "source": item["source"], "request_digest": item["request_digest"], "request_route": item["route"],
           "human_owner": item["human_owner"], "status": "held", "plan": None, "claim": None, "work": None, "ready": None}
    opened = any(e["kind"] == "ticket_opened" and e["body"].get("ticket_id") == tid for e in events)
    if item["route"] != "agent_ready" or not opened:
        return out
    notes = _notes(events, tid)
    planned = [e for e in notes if e["body"]["note"] == "ticket_planned" and e["body"]["request_digest"] == item["request_digest"]]
    if not planned:
        return {**out, "status": "open"}
    event = planned[-1]
    body = event["body"]
    planner = body.get("planner")
    wrote = "code" if not planner else _name(planner["by"]) if planner.get("by") else f"planner:{planner['name']}"
    found = {"plan_digest": body["plan_digest"], "route": body["route"], "rule": body["rule"], "reason": body["reason"],
             "by": wrote, "base": body["base"], "files": body["files"],
             "new_files": body["new_files"], "assessment": body["assessment"], "seq": event["seq"]}
    later = [e for e in notes if e["seq"] > event["seq"]]
    for resolution in later:
        if resolution["body"]["note"] == "plan_resolved" and resolution["body"]["plan_digest"] == found["plan_digest"]:
            found.update(route=resolution["body"]["route"], rule="resolved", reason=resolution["body"]["reason"],
                         by=_name(resolution["body"]["actor"]))
    out["plan"] = found
    if found["route"] != "planned":
        return {**out, "status": "plan_held" if found["route"] == "needs_human" else "plan_rejected"}
    claim = None
    for entry in later:
        note, entry_body = entry["body"]["note"], entry["body"]
        if entry_body.get("plan_digest") != found["plan_digest"]:
            continue
        if note == "ticket_claimed":
            claim = {"by": entry_body["by"], "seq": entry["seq"], "at": entry["at"], "ttl_seconds": entry_body["ttl_seconds"],
                     "until": entry["at"] + entry_body["ttl_seconds"] * MICRO}
        elif note == "ticket_released":
            claim = None
        elif note == "work_started":
            out["work"] = {"by": entry_body["by"], "contract": entry_body["contract"], "seq": entry["seq"], **entry_body["refs"]}
        elif note == "work_ready":
            out["ready"] = {"seq": entry["seq"], **entry_body["refs"]}
    if claim is not None and claim["until"] > now:
        out["claim"] = claim
    if out["ready"] is not None:
        status = "ready"
    elif out["claim"] is None:
        status = "planned"
    else:
        status = "working" if out["work"] is not None and out["work"]["by"] == out["claim"]["by"] else "claimed"
    return {**out, "status": status}


def label(current: dict) -> str:
    """One ticket's state as the label a tracker shows: ``agent-ready`` (waiting for a plan), ``needs-human`` (its
    plan waits for a person), ``planned``, ``agent:working`` (claimed, or a thread started), ``ready`` (an accepted
    change waits for a person); for a held ticket, the label of its request's route. The ledger is the record;
    nothing here applies a label."""
    return LABELS.get(current["status"]) or intake.label(current["request_route"])


def tickets(ledger, *, now: int | None = None) -> list[dict]:
    """Every ticket intake opened, in the order requests were first decided, each as ``state`` gives it."""
    events = ledger.events()
    opened = {e["body"]["ticket_id"] for e in events if e["kind"] == "ticket_opened" and e["body"].get("source") == "intake"}
    return [state(ledger, intake.ticket_id(item["source"]), events=events, now=now)
            for _, item in sorted(intake.items(ledger, events).items(), key=lambda pair: pair[1]["decided_seq"])
            if intake.ticket_id(item["source"]) in opened]


# -- the plan ------------------------------------------------------------------------------------------------------------

def _artifact(ledger, sha: str) -> Path:
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise Rejected("plan_missing", f"a plan digest is 64 lowercase hex characters, not {sha!r}")
    return Path(ledger.home) / ARTIFACTS / f"{sha}.json"


def load_plan(ledger, sha: str) -> dict:
    """The plan a ``ticket_planned`` event named, refused unless the file still digests to that name: an edited
    plan cannot become a thread's scope."""
    path = _artifact(ledger, sha)
    try:
        found = intake.strict_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Rejected("plan_missing", f"{ARTIFACTS}/{sha[:12]} is missing or unreadable") from None
    if not isinstance(found, dict) or found.get("schema") != planmod.PLAN_SCHEMA or digest(found) != sha:
        raise Rejected("artifact_tampered", f"{ARTIFACTS}/{sha[:12]}.json does not digest to its name")
    return found


def make_plan(ledger, text: str, *, repo_root: str | Path, planner: tuple[str, list[str]] | None = None,
              written: tuple[dict, object] | None = None, floors: dict | None = None, limits: dict | None = None,
              sensitive_areas: list[str] | tuple = (), timeout: int = 300) -> dict:
    """Plan one ``agent_ready`` ticket and record the outcome. Returns ``{"planned", "plan", "state"}``: the body
    of the ``ticket_planned`` event, the plan (None when there is none) and the ticket's status afterwards.

    ``planner`` is ``(name, argv)``: the command reads the request on stdin in a temporary checkout of HEAD and
    prints a plan. ``written`` is ``(human actor, plan document)`` for a plan a person wrote; it is checked and
    routed by the same code. With neither, the outcome is ``no_planner`` and the ticket waits for a person.
    ``floors`` is a class-floor policy in ``policy.json``'s shape (default: ``plan.DEFAULT_FLOORS``);
    ``sensitive_areas`` are component ids a plan may not touch without a person (intake's policy list)."""
    floors = planmod.check_floors(floors if floors is not None else planmod.DEFAULT_FLOORS)
    limits = planmod.check_limits(limits)
    if planner is not None and written is not None:
        raise Rejected("plan_source", "a plan comes from a planner command or from a person, not both")
    party = intake.named_command(planner, "a planner") if planner is not None else None
    author = _actor(written[0]) if written is not None else None
    if author is not None and author["kind"] != "human":
        raise Rejected("plan_source", "a written plan is a person's (human:<id>); a model's plan comes through a planner command")
    events = ledger.events()
    current = state(ledger, text, events=events)
    item = find(ledger, text, events)
    if current["status"] == "held":
        raise Rejected("ticket_held", f"{current['ticket_id']} is not agent_ready as its request reads now "
                                      f"({item['route']}: {item['reason']}); only agent_ready work is planned")
    if current["status"] == "ready":
        raise Rejected("ticket_ready", f"{current['ticket_id']} holds an accepted change waiting for a person; nothing is left to plan")
    if current["status"] in ("claimed", "working"):
        raise Rejected("ticket_claimed", f"{current['ticket_id']} is {current['status']}; release it before planning it again")
    ticket = intake.ticket_of(ledger, item)
    document = intake.load_snapshot(ledger, item["snapshot_digest"])
    repo = Repo(repo_root)
    base = repo.rev("HEAD")
    if base is None:
        raise Rejected("empty_repository", "commit something first; a plan names files at HEAD")
    tracked = repo.files(base)
    space = ledger.latest_space()
    request = planmod.planner_request(ticket, document, base=base, tracked=tracked, space=space, limits=limits)
    record, raw = None, None
    if party is not None:
        with repo.worktree(base) as tree:
            outcome, raw, sha = intake.run_reader(party[1], request, timeout=timeout, cwd=tree, max_bytes=MAX_PLAN_BYTES)
        record = {"name": party[0], "outcome": outcome, "model": None, "output_digest": sha}
    elif author is not None:
        record, raw = {"name": "person", "outcome": "answered", "model": None, "output_digest": None, "by": author}, written[1]
    found = None
    if record is not None and record["outcome"] == "answered":
        try:
            found = planmod.check_plan(raw, ticket_id=ticket["ticket_id"], request_digest=item["request_digest"], base=base,
                                       tracked=tracked, limits=limits)
        except Rejected as exc:
            record["outcome"] = f"{exc.code.removeprefix('plan_')}:{exc.detail}"
        else:
            record["model"] = found["model"]
    assessment = planmod.assess(found, floors=floors, space=space, ticket=ticket, sensitive_areas=sensitive_areas) if found else None
    decided = planmod.plan_route(record, assessment, limits)
    sha = None
    if found is not None:
        sha = digest(found)
        path = _artifact(ledger, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(canonical(found), encoding="utf-8")
    body = {"note": "ticket_planned", "ticket_id": ticket["ticket_id"], "source": item["source"],
            "request_digest": item["request_digest"], "base": base, "plan_digest": sha, "planner": record,
            "files": found["files"] if found else [], "new_files": found["new_files"] if found else [],
            "steps": len(found["steps"]) if found else 0, "assessment": assessment, "floors_digest": digest(floors),
            "limits": limits, **decided}

    def unchanged(now_events):
        after = state(ledger, text, events=now_events)
        if after["request_digest"] != item["request_digest"] or after["status"] in ("held", "claimed", "working", "ready"):
            raise Rejected("ticket_changed", f"{ticket['ticket_id']} changed while it was being planned ({after['status']}); plan it again")
        return None

    event = ledger.append_once("note", body, unchanged)
    return {"planned": event["body"], "plan": found, "state": state(ledger, text)}


def approve(ledger, text: str, actor: dict, *, reason: str, reject: bool = False) -> dict:
    """A person's decision on a ticket's current plan: ``planned`` (an agent may claim it) or, with ``reject``,
    ``rejected`` (plan it again). Only a human actor decides, and only a plan that exists: when the planner
    gave none there is nothing to approve. Returns the ticket's state afterwards."""
    actor = _actor(actor)
    if actor["kind"] != "human":
        raise Rejected("actor_invalid", "a plan is approved or rejected by a person (human:<id>), never a model")
    reason = _reason(reason, "a decision on a plan")
    current = state(ledger, text)
    found = current["plan"]
    if found is None or found["plan_digest"] is None:
        raise Rejected("plan_missing", f"{current['ticket_id']} has no plan to decide on ({current['status']}); plan it first")
    if current["status"] in ("claimed", "working", "ready"):
        raise Rejected("ticket_claimed", f"{current['ticket_id']} is {current['status']}; its plan is in use and is not decided on again")
    load_plan(ledger, found["plan_digest"])
    body = {"note": "plan_resolved", "ticket_id": current["ticket_id"], "plan_digest": found["plan_digest"],
            "from": {"route": found["route"], "rule": found["rule"], "reason": found["reason"], "by": found["by"]},
            "route": "rejected" if reject else "planned", "reason": reason, "actor": actor}

    def same_plan(events):
        after = state(ledger, text, events=events)
        if after["plan"] is None or after["plan"]["seq"] != found["seq"] or after["status"] in ("claimed", "working", "ready"):
            raise Rejected("ticket_changed", f"{current['ticket_id']} changed while its plan was being decided on; read it again")
        return None

    ledger.append_once("note", body, same_plan)
    return state(ledger, text)


# -- the claim -------------------------------------------------------------------------------------------------------------

def claim(ledger, text: str, by: dict, *, ttl: int = DEFAULT_TTL, now: int | None = None) -> dict:
    """Take a planned ticket for one agent, or renew that agent's own claim. Returns the ticket's state.

    Refused: a ticket that is not planned (``ticket_unplanned``, with its status), and one another agent holds
    (``ticket_claimed``, naming the holder). The check and the append are one acquisition of the ledger's lock."""
    by = _actor(by)
    if type(ttl) is not int or not MIN_TTL <= ttl <= MAX_TTL:
        raise Rejected("claim_ttl", f"a claim lasts {MIN_TTL} to {MAX_TTL} seconds")
    tid = state(ledger, text, now=now)["ticket_id"]

    def free(events):
        current = state(ledger, tid, events=events, now=now)
        if current["status"] in ("claimed", "working") and current["claim"]["by"] != by:
            raise Rejected("ticket_claimed", f"{tid} is held by {_name(current['claim']['by'])}; one agent works a ticket")
        if current["status"] not in ("planned", "claimed", "working"):
            raise Rejected("ticket_unplanned", f"{tid} is {current['status']}; an agent takes only planned work")
        body["plan_digest"] = current["plan"]["plan_digest"]
        return None

    body = {"note": "ticket_claimed", "ticket_id": tid, "plan_digest": None, "by": by, "ttl_seconds": ttl}
    ledger.append_once("note", body, free)
    return state(ledger, tid, now=now)


def release(ledger, text: str, by: dict, *, reason: str, now: int | None = None) -> dict:
    """Give a claim up. The claimant releases its own; a person releases anyone's. Returns the ticket's state."""
    by = _actor(by)
    reason = _reason(reason, "a release")
    tid = state(ledger, text, now=now)["ticket_id"]

    def held(events):
        current = state(ledger, tid, events=events, now=now)
        if current["claim"] is None:
            raise Rejected("ticket_unclaimed", f"{tid} holds no live claim ({current['status']})")
        if current["claim"]["by"] != by and by["kind"] != "human":
            raise Rejected("ticket_claimed", f"{tid} is held by {_name(current['claim']['by'])}; its holder or a person releases it")
        body.update(plan_digest=current["plan"]["plan_digest"], claim_seq=current["claim"]["seq"])
        return None

    body = {"note": "ticket_released", "ticket_id": tid, "plan_digest": None, "claim_seq": None, "by": by, "reason": reason}
    ledger.append_once("note", body, held)
    return state(ledger, tid, now=now)


# -- the thread ------------------------------------------------------------------------------------------------------------

def objective(document: dict, found: dict, plan_digest: str) -> str:
    """The thread's objective: a first line that names the request (``Fixes owner/repository#N`` for a GitHub
    issue, which closes it when the pull request merges) and the snapshot's title, then the plan's steps, which
    a proposer reads as part of the objective."""
    source = document["source"]
    title = " ".join(document["visible"]["title"].split())[:160] or "untitled request"
    head = f"Fixes {source['ref']}: {title}" if source["kind"] == "github_issue" else f"{title} ({source['kind']} {source['ref']})"
    return f"{head}\nPlan {plan_digest[:12]}: " + "; ".join(f"{number}. {step}" for number, step in enumerate(found["steps"], 1))


def work_contract(ticket_id: str, plan_digest: str, title: str, found: dict, *, checks: dict, limits: dict, max_candidates: int = 6) -> dict:
    """One ``repo-change``-shaped contract for one plan: the mutable scope is the plan's files and nothing else,
    at most that many files may change, every named check is required. The id carries the ticket and the plan,
    so a new plan is a new contract (a published contract is immutable)."""
    paths = found["files"] + found["new_files"]
    if not checks or any(not isinstance(check, dict) or not check.get("argv") for check in checks.values()):
        raise Rejected("contract_unchecked", "a work contract needs at least one check, each with an argv")
    return {"id": f"syberfactory-work-{ticket_id}-{plan_digest[:8]}", "version": 1, "title": title[:120] or ticket_id,
            "inputs": {"objective": "string", "act_class": "string"},
            "actions": {PROMOTE: {}}, "acceptance": [{"id": "accepted", "kind": "effect", "action": PROMOTE}],
            "evolution": {"scope": {"paths": paths, "exclude": [], "max_files": len(paths), "max_diff_bytes": limits["max_diff_bytes"]},
                          "operators": ["patch"],
                          "budget": {"max_candidates": max_candidates, "max_evaluations": 2 * max_candidates, "max_seconds": 1800},
                          "evaluation": {"checks": {name: {"argv": list(check["argv"]), "timeout_seconds": int(check.get("timeout_seconds", 300))}
                                                    for name, check in checks.items()},
                                         "required": sorted(checks), "max_age_seconds": 3600},
                          "promotion": {"action": PROMOTE, "roles": ["developer"]}}}


def start(ledger, text: str, by: dict, *, repo_root: str | Path, kit_home: str | Path, checks: dict, max_candidates: int = 6,
          now: int | None = None) -> dict:
    """Start the Build Thread of a ticket its claimant holds. Returns ``{"thread", "contract", "objective",
    "scope", "state"}``; a second call by the same claimant returns the thread it already started.

    Refused: a ticket ``by`` does not hold (``ticket_unclaimed``), and a plan whose files no longer fit HEAD
    (``plan_stale``: a planned file is gone, or a file it meant to add exists). The thread is attended: SyberWork
    runs the checks, a person accepts. Its class is the one the plan's paths floor at (``plan.act_class``), so a
    plan a person approved above ``auto`` does not become a thread that says R."""
    by = _actor(by)
    current = state(ledger, text, now=now)
    tid = current["ticket_id"]
    if current["status"] == "working" and current["work"]["by"] == by:
        return {"thread": current["work"]["thread"], "contract": current["work"]["contract"], "objective": None,
                "scope": current["plan"]["files"] + current["plan"]["new_files"], "state": current, "already": True}
    if current["status"] != "claimed" or current["claim"]["by"] != by:
        raise Rejected("ticket_unclaimed", f"{tid} is {current['status']}; {_name(by)} claims it before starting work")
    found = load_plan(ledger, current["plan"]["plan_digest"])
    item = find(ledger, tid)
    document = intake.load_snapshot(ledger, item["snapshot_digest"])
    root = Path(repo_root).resolve()
    repo = Repo(root)
    head = repo.rev("HEAD")
    tracked = set(repo.files(head)) if head else set()
    folded = {path.casefold() for path in tracked}
    gone = [path for path in found["files"] if path not in tracked]
    taken = [path for path in found["new_files"] if path.casefold() in folded]
    if gone or taken:
        raise Rejected("plan_stale", f"the plan was made at {found['base'][:10]} and no longer fits HEAD: "
                                     f"missing {gone or 'none'}, already there {taken or 'none'}; plan the ticket again")
    limits = planmod.check_limits(current_limits(ledger, tid, current["plan"]["seq"]))
    contract = work_contract(tid, current["plan"]["plan_digest"], document["visible"]["title"], found, checks=checks, limits=limits,
                             max_candidates=max_candidates)
    home = Path(kit_home)
    home = home if home.is_absolute() else root / home
    contracts = home / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    path = contracts / f"{contract['id']}.v{contract['version']}.json"
    if not path.exists():
        path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    goal = objective(document, found, current["plan"]["plan_digest"])
    assessment = current["plan"]["assessment"]
    declared = planmod.act_class(assessment["class_floor"], assessment["requires"])  # computed from the paths, not declared
    kit = Kit.local(home, root)
    try:
        thread = kit.start(goal, contract=f"{contract['id']}.v{contract['version']}", paths=contract["evolution"]["scope"]["paths"],
                           act_class=declared)
        refs = {"thread": thread.id, "base": thread.base, "target_ref": thread.target_ref, "act_class": declared}
    finally:
        kit.close()

    def still_held(events):
        after = state(ledger, tid, events=events, now=now)
        if after["status"] != "claimed" or after["claim"]["by"] != by or after["plan"]["plan_digest"] != current["plan"]["plan_digest"]:
            raise Rejected("ticket_changed", f"{tid} changed while its thread was being started ({after['status']}); thread "
                                             f"{refs['thread']} was started and is not recorded as this ticket's")
        return None

    ledger.append_once("note", {"note": "work_started", "ticket_id": tid, "plan_digest": current["plan"]["plan_digest"], "by": by,
                                "contract": f"{contract['id']}.v{contract['version']}", "refs": refs}, still_held)
    return {"thread": refs["thread"], "contract": f"{contract['id']}.v{contract['version']}", "objective": goal,
            "scope": contract["evolution"]["scope"]["paths"], "state": state(ledger, tid, now=now), "already": False}


def current_limits(ledger, tid: str, seq: int) -> dict:
    """The limits the plan at ``seq`` was made under, so the contract's diff bound is the one the plan was held to."""
    event = next(e for e in ledger.events() if e["seq"] == seq)
    return dict(event["body"]["limits"])


def settle(ledger, text: str, *, repo_root: str | Path, kit_home: str | Path, now: int | None = None) -> dict:
    """Read the ticket's thread and record, once, that it holds an accepted candidate (``work_ready``). Nothing is
    accepted, pushed or merged here; this only reads the thread's status. Returns the ticket's state."""
    current = state(ledger, text, now=now)
    if current["work"] is None:
        raise Rejected("work_unstarted", f"{current['ticket_id']} has no thread ({current['status']})")
    if current["ready"] is not None:
        return current
    root = Path(repo_root).resolve()
    home = Path(kit_home)
    kit = Kit.local(home if home.is_absolute() else root / home, root)
    try:
        status = kit.open(current["work"]["thread"]).status()
    finally:
        kit.close()
    if not status["accepted"]:
        return current
    tid = current["ticket_id"]

    def once(events):
        return next((e for e in _notes(events, tid) if e["body"]["note"] == "work_ready"
                     and e["body"]["plan_digest"] == current["plan"]["plan_digest"]), None)

    ledger.append_once("note", {"note": "work_ready", "ticket_id": tid, "plan_digest": current["plan"]["plan_digest"],
                                "next": "accepted on its branch; pushing and merging are separate acts and nothing here does either",
                                "refs": {"thread": status["thread"], "candidate": status["accepted"], "commit": status["target"],
                                         "target_ref": status["target_ref"]}}, once)
    return state(ledger, tid, now=now)


def settle_all(ledger, *, repo_root: str | Path, kit_home: str | Path, now: int | None = None) -> list[dict]:
    """``settle`` for every ticket that has a thread and no ``work_ready`` yet, whether or not its claim is still
    live: the step before reading the queue. Returns the state of each ticket it looked at."""
    return [settle(ledger, row["ticket_id"], repo_root=repo_root, kit_home=kit_home, now=now)
            for row in tickets(ledger, now=now) if row["work"] is not None and row["ready"] is None]
