"""Per-work-item metrics: every number a pure function of the journal, never counted alongside it.

    syberlabs metrics [--thread ID] [--since DATE] [--json | --csv] [--check]

A work item is one Build Thread, from ``case_created`` to the merge of the pull request its
``github_pull_request`` effect opened, or to ``case_cancelled``. ``work_items(events)`` folds a journal's
events into one ``WorkItemMetrics`` per thread with nine metrics, each a ``Metric`` whose ``basis`` names the
event hashes it came from. A fact the journal does not carry is ``unknown`` (``value`` None), never 0: a
reader can tell "not recorded" from "none". ``summary`` aggregates over known values only and counts the
unknowns beside them; ``provenance`` lists the events behind one number so a certificate can cite them.

Derivations (``docs/METRICS.md`` has the full table):

- ``cost``: dollars from the host's spend observations (``search_finished.spend``, or for a search without one
  the ``candidate_registered.spend`` of its registrations), labelled ``cost_basis``: ``host_observed`` when a
  model call crossed the host's meter, ``declared`` when a provider only declared ``model_usage`` (dollars
  unknown), ``none`` when nothing was recorded (dollars unknown: the work happened outside the host).
  ``lane`` is the registrations' declared credential lane (``signal.lane``: ``api``, ``subscription_local``,
  ``seat_team_enterprise``, ``provider_3p``; ``mixed`` when they differ); on a lane whose ``signal.cost_basis`` is
  ``none`` (no per-call bill) cost stays unknown and says why.
- ``turns``: model calls with declared usage: registrations and ``proposal_refused`` records carrying
  ``model_usage`` (adjacent records with identical usage are one call; a registration whose signal declares
  ``harness_turns``, a harness run such as Claude Code's ``num_turns``, counts as that many), plus ``critic_usage``
  facts. Unknown when any registration carries no usage (that work happened outside the host's view).
- ``agent_time``: hours from ``case_created`` to the ``pr_merged`` fact's ``merged_at``; unknown without it.
- ``fix_rounds``: registrations that follow a failed evaluation (a required check not ``passed``).
- ``web_lookups`` / ``githits_lookups``: the adapter-declared ``signal.tool_usage`` counts; unknown unless
  every registration declares them. When the record holds ``lookup`` facts from GitHits client records (tool
  ``githits.<op>``, ``claude_code`` with ``githits=True``), ``githits_lookups`` is their number instead: host-observed,
  and counted for calls whose answer was refused too. A ``githits.doctor`` fact (a readiness check, the operator's)
  is never counted.
- ``delivered``: true with a ``pr_merged`` fact; false when no pull request was opened, the thread was
  cancelled, or a ``pr_closed`` fact says the pull request was closed unmerged; unknown while a pull request is
  open and no merge is in the record. ``opened`` is separate. ``syberlabs.settle`` writes the ``pr_*`` facts.
  every registration declares them. They count attempted lookups, blocked ones included; beside them
  ``lookups_blocked`` counts the host's ``lookup`` facts whose query guard decision is a rule (not ``pass``).
- ``delivered``: true with a ``pr_merged`` fact; false when no pull request was opened or the thread was
  cancelled; unknown while a pull request is open and no merge is in the record. ``opened`` is separate.
- ``review_rejections``: critic checks that did not pass or whose observation counts a high finding, plus
  ``review`` facts with verdict ``fail`` and ``pr_review`` facts with state ``changes_requested``.
- ``human_interventions``: ``human_intervention`` facts, plus ``proposed``, ``approved`` and ``case_cancelled``
  events whose actor is neither ``syberlabs.host`` nor the thread's unattended runner and that no fact names.

This module reads events and nothing else: it imports nothing from ``build``, ``session`` or ``syberwork``, opens
no file, and appends nothing. It also owns the shapes of the facts the host writes for it (``intervention_value``,
``review_value``), so the writer and the reader agree on one definition.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from syberlabs import _patterns, critic
from syberlabs.clock import as_seconds

HOST = "syberlabs.host"            # the same actor name build.HOST uses; metrics may not import build
UNATTENDED = "unattended"          # build.UNATTENDED
PROPOSAL_REFUSED = "proposal_refused"  # build.PROPOSAL_REFUSED
HUMAN_INTERVENTION = "human_intervention"
REVIEW = "review"
PR_MERGED = "pr_merged"            # written by the settlement poll, syberlabs.settle
PR_CLOSED = "pr_closed"            # the same poll: closed without a merge
PR_REVIEW = "pr_review"
LOOKUP = "lookup"                  # claude_code.LOOKUP; its ``guard`` is query_guard's decision
CLIENT_LOOKUP = "githits."         # claude_code.CLIENT_TOOL: a lookup fact made from an authentic GitHits client record
CLIENT_DOCTOR = CLIENT_LOOKUP + "doctor"  # a readiness check's record, not a lookup: never in ``githits_lookups``
INTERVENTION_KINDS = ("message", "commit", "approve", "edit", "override")
VERDICTS = ("pass", "fail")
MAX_NOTE = 500
UNKNOWN = "unknown"
METRICS = ("cost", "turns", "agent_time", "fix_rounds", "web_lookups", "githits_lookups", "delivered",
           "review_rejections", "human_interventions")
COST_BASES = ("host_observed", "declared", "none")
_FAILED = ("failed", "error", "timed_out")
_PERSON_ACTS = ("proposed", "approved", "case_cancelled")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}$")
_URL = re.compile(r"^https://[^\s\x00-\x1f\x7f]{1,500}$")
# Shapes of common credentials. A note is free text a person types; one that looks like a key is refused, not stored.
_SECRET = _patterns.SECRET  # one object, shared with the query guard (``syberlabs._patterns``)


@dataclass(frozen=True)
class Metric:
    """One number and the event hashes it came from. ``value`` None is unknown, never zero."""

    value: Any
    basis: tuple[str, ...] = ()
    note: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {"value": UNKNOWN if self.value is None else self.value, "basis": list(self.basis), "note": self.note}


@dataclass(frozen=True)
class WorkItemMetrics:
    thread: str
    contract: str | None
    act_class: str | None
    started_at: int | float | None
    unattended: bool
    cancelled: bool
    pull_request: str | None
    cost_basis: str
    opened: Metric
    cost: Metric
    turns: Metric
    agent_time: Metric
    fix_rounds: Metric
    web_lookups: Metric
    githits_lookups: Metric
    delivered: Metric
    review_rejections: Metric
    human_interventions: Metric
    index: Mapping[str, tuple[int, str]] = field(default_factory=dict, repr=False, compare=False)
    lane: str | None = None
    lookups_blocked: Metric = field(default_factory=lambda: Metric(None, (), "not computed"))

    def metric(self, name: str) -> Metric:
        if name not in METRICS + ("opened", "lookups_blocked"):
            raise KeyError(name)
        return getattr(self, name)

    def unknown(self) -> list[str]:
        return [name for name in METRICS if not self.metric(name).known]

    def to_dict(self) -> dict:
        found = {"thread": self.thread, "contract": self.contract, "act_class": self.act_class,
                 "started_at": utc(self.started_at), "unattended": self.unattended, "cancelled": self.cancelled,
                 "pull_request": self.pull_request, "cost_basis": self.cost_basis, "lane": self.lane,
                 "opened": self.opened.to_dict()}
        found.update({name: self.metric(name).to_dict() for name in METRICS})
        found["lookups_blocked"] = self.lookups_blocked.to_dict()
        return found


def utc(at: int | float | None) -> str | None:
    """An event's ``at`` (integer microseconds, or legacy float seconds) as ISO 8601 UTC."""
    if at is None:
        return None
    return datetime.fromtimestamp(as_seconds(at), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> float | None:
    """``YYYY-MM-DD`` or ISO 8601 (``Z`` allowed) as epoch seconds, UTC when no offset is given; None if unreadable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    return found.timestamp()


def fact(event: dict, key: str) -> dict | None:
    """The value of a verified host observation under ``key``, or None."""
    body = event.get("body") if isinstance(event.get("body"), dict) else {}
    if (event.get("kind") == "observed" and body.get("key") == key and body.get("verified") is True
            and body.get("actor") == HOST and isinstance(body.get("value"), dict)):
        return body["value"]
    return None


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _failed(checks: list, required: Sequence[str] | None) -> bool:
    return any(isinstance(c, dict) and c.get("state") != "passed" and (required is None or c.get("name") in required)
               for c in checks)


def _usage(value) -> dict | None:
    return value if isinstance(value, dict) and value else None


def _required(contracts, contract) -> list[str] | None:
    if not contracts or not isinstance(contract, (list, tuple)) or len(contract) != 2:
        return None
    doc = contracts.get(tuple(contract)) or contracts.get(f"{contract[0]}.v{contract[1]}")
    found = ((doc or {}).get("evolution") or {}).get("evaluation", {}).get("required")
    return list(found) if isinstance(found, list) else None


def _cost(events: list[dict]) -> tuple[str, Metric]:
    """Dollars per search from ``search_finished.spend``; a search that never finished adds its registrations' spend."""
    dollars: list = []
    basis: list[str] = []
    host_observed = unpriced = declared = False
    pending: list[tuple[str, dict]] = []  # registrations' spend in a search that has not finished
    for event in events:
        kind, body = event["kind"], event["body"]
        spend = body.get("spend") if kind in ("candidate_registered", "search_finished") else None
        refused = fact(event, PROPOSAL_REFUSED)
        if refused is not None:
            declared = declared or _usage(refused.get("model_usage")) is not None
            spend = refused.get("spend")
        spend = spend if isinstance(spend, dict) else None
        host_observed = host_observed or (spend is not None and spend.get("host_observed") is True)
        if kind == "search_started":
            for found in pending:  # an earlier search that never finished
                _add(dollars, basis, *found)
            pending = []
        elif kind == "candidate_registered":
            signal = body.get("signal") if isinstance(body.get("signal"), dict) else {}
            declared = declared or _usage(signal.get("model_usage")) is not None
            if spend is not None:
                pending.append((event["hash"], spend))
        elif kind == "search_finished":
            pending = []
            if spend is not None:
                unpriced = unpriced or bool(spend.get("unknown_models"))
                _add(dollars, basis, event["hash"], spend)
    for found in pending:
        _add(dollars, basis, *found)
    if host_observed:
        if unpriced or any(d is None for d in dollars):
            return "host_observed", Metric(None, tuple(basis), "a model the contract does not price was used")
        return "host_observed", Metric(round(sum(dollars), 6), tuple(basis),
                                       "proposer calls metered by the host; critic usage excluded")
    if declared:
        return "declared", Metric(None, tuple(basis), "usage declared by the provider, not metered by the host")
    return "none", Metric(None, tuple(basis), "no model call crossed the host's meter; the work happened outside it")


def _add(dollars: list, basis: list, event_hash: str, spend: dict) -> None:
    value = spend.get("dollars")
    dollars.append(value if isinstance(value, (int, float)) and not isinstance(value, bool) else None)
    basis.append(event_hash)


def _harness_turns(signal) -> int:
    """The calls one registration stands for: ``signal.harness_turns`` when a harness declared it, else one."""
    turns = signal.get("harness_turns") if isinstance(signal, dict) else None
    return turns if type(turns) is int and turns >= 1 else 1


def _lane(registrations: list[dict], cost: Metric) -> tuple[str | None, Metric]:
    """The registrations' declared lane, and the cost labelled with it. A lane without a per-call bill
    (``signal.cost_basis`` ``none``) leaves an unknown cost unknown, with the reason."""
    signals = [e["body"].get("signal") for e in registrations]
    lanes = {s.get("lane") for s in signals if isinstance(s, dict) and isinstance(s.get("lane"), str)}
    if not lanes:
        return None, cost
    lane = lanes.pop() if len(lanes) == 1 else "mixed"
    bases = {s.get("cost_basis") for s in signals if isinstance(s, dict) and isinstance(s.get("lane"), str)}
    if cost.value is None and bases == {"none"}:
        return lane, Metric(None, cost.basis, f"lane {lane}: no per-call bill (cost_basis none); Claude Code's "
                                              "estimated_usd is an estimate, not spend")
    return lane, Metric(cost.value, cost.basis, f"{cost.note}; lane {lane}, cost_basis {'/'.join(sorted(map(str, bases)))}")


def _thread(case_id: str, events: list[dict], contracts) -> WorkItemMetrics:
    created = next((e for e in events if e["kind"] == "case_created"), None)
    body = created["body"] if created else {}
    inputs = body.get("inputs") if isinstance(body.get("inputs"), dict) else {}
    contract = body.get("contract")
    required = _required(contracts, contract)
    runner = next((f.get("by") for f in (fact(e, UNATTENDED) for e in events) if f and f.get("unattended") is True), None)
    agents = {HOST} | ({runner} if isinstance(runner, str) else set())
    registrations = [e for e in events if e["kind"] == "candidate_registered" and e["body"].get("operator") != "migration"]
    cancel = next((e for e in events if e["kind"] == "case_cancelled"), None)

    cost_basis, cost = _cost(events)
    lane, cost = _lane(registrations, cost)

    # Turns: model calls whose usage is declared on a record. One call can leave several records with the same usage
    # (the items after a repair call's first are refused ``limit``); adjacent records with identical usage are one call.
    calls, previous, count = [], None, 0
    for event in events:
        critic_usage = fact(event, critic.USAGE_KEY)
        if critic_usage is not None and _usage(critic_usage.get("usage")):
            calls.append(event["hash"])
            count += 1
            previous = None
            continue
        usage, refused, turns = None, fact(event, PROPOSAL_REFUSED), 1
        if event["kind"] == "candidate_registered" and isinstance(event["body"].get("signal"), dict):
            usage = _usage(event["body"]["signal"].get("model_usage"))
            turns = _harness_turns(event["body"]["signal"])
        elif refused is not None:
            usage = _usage(refused.get("model_usage"))
        if usage is not None and usage != previous:
            calls.append(event["hash"])
            count += turns
        previous = usage
    blind = [e["hash"] for e in registrations
             if not (isinstance(e["body"].get("signal"), dict) and _usage(e["body"]["signal"].get("model_usage")))]
    if blind or not calls:
        turns = Metric(None, tuple(blind), "a registration carries no declared usage: its calls were outside the host"
                       if blind else "no model call is recorded")
    else:
        turns = Metric(count, tuple(calls), "host-recorded calls with declared usage (a harness run counts its "
                                            "declared harness_turns), critic calls included")

    # Opened and delivered.
    opened_event, number = None, None
    for event in events:
        output = event["body"].get("output") if event["kind"] == "effect_succeeded" else None
        proof = event["body"].get("proof") if event["kind"] == "reconciled" and event["body"].get("success") is True else None
        found = output if isinstance(output, dict) else proof if isinstance(proof, dict) and proof.get("verified") is True else None
        if found and str(found.get("destination") or "").startswith("github:") and found.get("external_id"):
            opened_event, number = event, str(found["external_id"])
    opened = Metric(opened_event is not None, (opened_event["hash"],) if opened_event else (),
                    f"pull request #{number}" if number else "no pull request opened")
    merged = next(((e, f) for e, f in ((e, fact(e, PR_MERGED)) for e in events)
                   if f is not None and (number is None or str(f.get("number")) == number)), None)
    closed = [(e, f) for e, f in ((e, fact(e, PR_CLOSED)) for e in events)
              if f is not None and number is not None and str(f.get("number")) == number]
    if merged is not None:
        delivered = Metric(True, (merged[0]["hash"],), f"merged {merged[1].get('merged_at')}")
    elif cancel is not None:
        delivered = Metric(False, (cancel["hash"],), "cancelled")
    elif closed:
        delivered = Metric(False, (closed[-1][0]["hash"],), f"closed unmerged {closed[-1][1].get('closed_at')}")
    elif opened_event is None:
        delivered = Metric(False, (), "no pull request opened")
    else:
        delivered = Metric(None, (opened_event["hash"],), "pull request open; no pr_merged fact in the record")

    merged_at = parse_time(merged[1].get("merged_at")) if merged is not None else None
    if merged_at is not None and created is not None:
        agent_time = Metric(round((merged_at - as_seconds(created["at"])) / 3600, 3), (created["hash"], merged[0]["hash"]),
                            "case_created to merge, human review time included")
    else:
        agent_time = Metric(None, (created["hash"],) if created else (),
                            "no pr_merged fact" if merged is None else "merged_at unreadable")

    # Fix rounds: a registration after a failed evaluation, each failed evaluation counted once.
    rounds, failed_eval = [], None
    for event in events:
        if event["kind"] == "candidate_evaluated":
            failed_eval = event if _failed(event["body"].get("checks") or [], required) else None
        elif event["kind"] == "candidate_registered" and failed_eval is not None:
            rounds += [failed_eval["hash"], event["hash"]]
            failed_eval = None
    fix_rounds = Metric(len(rounds) // 2, tuple(rounds), "registrations after a failed evaluation")

    def lookups(keys: tuple[str, ...], label: str) -> Metric:
        total, basis, missing = 0, [], []
        for event in registrations:
            signal = event["body"].get("signal")
            tools = signal.get("tool_usage") if isinstance(signal, dict) else None
            counts = [_count(tools.get(k)) for k in keys] if isinstance(tools, dict) else []
            if not any(c is not None for c in counts):
                missing.append(event["hash"])
                continue
            total += sum(c for c in counts if c is not None)
            basis.append(event["hash"])
        if missing or not registrations:
            return Metric(None, tuple(missing), f"{label} not declared by the adapter" if missing else "no registration")
        return Metric(total, tuple(basis), f"{label} declared by the adapter (signal.tool_usage)")

    web = lookups(("web_search", "web_fetch"), "web_search and web_fetch")
    # GitHits through the API client: the host journaled one lookup fact per authentic client record.
    client = [(e["hash"], fact(e, LOOKUP)["tool"]) for e in events
              if isinstance((fact(e, LOOKUP) or {}).get("tool"), str) and fact(e, LOOKUP)["tool"].startswith(CLIENT_LOOKUP)]
    counted = tuple(digest for digest, tool in client if tool != CLIENT_DOCTOR)
    githits = (Metric(len(counted), counted, "host-observed: authentic GitHits client records (lookup facts; "
                                             "doctor excluded)")
               if client else lookups(("githits",), "githits"))

    # Lookups the host's query guard blocked: ``lookup`` facts whose ``guard`` names a rule.
    seen = [(e, f) for e, f in ((e, fact(e, LOOKUP)) for e in events) if f is not None]
    if any(not isinstance(f.get("guard"), str) for _e, f in seen):
        blocked = Metric(None, tuple(e["hash"] for e, f in seen if not isinstance(f.get("guard"), str)),
                         "a lookup was recorded without a query guard decision")
    elif seen:
        hits = [e["hash"] for e, f in seen if f["guard"] not in ("pass", "unguarded")]
        blocked = Metric(len(hits), tuple(hits), "lookup facts whose query guard decision is a rule")
    elif web.value == 0 and githits.value == 0:
        blocked = Metric(0, (), "no lookups")
    else:
        blocked = Metric(None, (), "no lookup facts in the record")

    # Review rejections: the critic in the record, then recorded verdicts.
    rejections = []
    for event in events:
        if event["kind"] == "candidate_evaluated":
            for check in event["body"].get("checks") or []:
                if not isinstance(check, dict) or not critic.is_critic(check.get("name")):
                    continue
                if check.get("state") in _FAILED:
                    rejections.append(event["hash"])
                elif check.get("state") == "passed":
                    seen = critic.observation(events, event["body"].get("candidate"), check["name"], check.get("output_digest"))
                    if seen and (seen.get("counts") or {}).get("high", 0) > 0:
                        rejections.append(event["hash"])
        review, pr_review = fact(event, REVIEW), fact(event, PR_REVIEW)
        if (review and review.get("verdict") == "fail") or (pr_review and pr_review.get("state") == "changes_requested"):
            rejections.append(event["hash"])
    review_rejections = Metric(len(rejections), tuple(rejections),
                               "critic checks not passed or with a high finding, review fail, changes requested")

    # Human interventions: facts first, then acts by a person that no fact names.
    facts = [(e, f) for e, f in ((e, fact(e, HUMAN_INTERVENTION)) for e in events) if f is not None]
    named = {f.get("proposal") for _e, f in facts if f.get("proposal")}
    acts = []
    for event in events:
        act = event["body"]
        if event["kind"] in _PERSON_ACTS and isinstance(act.get("actor"), str) and act["actor"] not in agents:
            if (act.get("id") if event["kind"] == "proposed" else act.get("proposal_id")) not in named:
                acts.append(event["hash"])
    interventions = Metric(len(facts) + len(acts), tuple([e["hash"] for e, _f in facts] + acts),
                           "human_intervention facts and acts by an actor other than the host and the unattended runner")

    return WorkItemMetrics(
        thread=case_id, contract=f"{contract[0]}.v{contract[1]}" if isinstance(contract, list) and len(contract) == 2 else None,
        act_class=inputs.get("act_class"), started_at=created["at"] if created else None,
        unattended=runner is not None, cancelled=cancel is not None, pull_request=number, cost_basis=cost_basis, lane=lane,
        opened=opened, cost=cost, turns=turns, agent_time=agent_time, fix_rounds=fix_rounds, web_lookups=web,
        githits_lookups=githits, delivered=delivered, review_rejections=review_rejections,
        human_interventions=interventions, index={e["hash"]: (e["seq"], e["kind"]) for e in events},
        lookups_blocked=blocked)


def work_items(events: Iterable[dict], contracts: Mapping | None = None) -> list[WorkItemMetrics]:
    """One ``WorkItemMetrics`` per thread in ``events``, in order of first appearance. Pure.

    ``contracts`` optionally maps ``(contract_id, version)`` (or ``"id.vN"``) to the contract document, so a fix round
    is judged on the contract's required checks; without it every recorded check counts as required.
    """
    threads: dict[str, list[dict]] = {}
    for event in events:
        threads.setdefault(event["case_id"], []).append(event)
    return [_thread(case_id, sorted(found, key=lambda e: e["seq"]), contracts) for case_id, found in threads.items()]


def provenance(item: WorkItemMetrics, metric: str) -> list[dict]:
    """The events behind one number: ``[{hash, seq, kind}]`` in journal order."""
    rows = [{"hash": h, "seq": item.index.get(h, (None, None))[0], "kind": item.index.get(h, (None, None))[1]}
            for h in dict.fromkeys(item.metric(metric).basis)]
    return sorted(rows, key=lambda row: (row["seq"] is None, row["seq"] or 0))


def since(items: Iterable[WorkItemMetrics], start: str | float | None) -> list[WorkItemMetrics]:
    """Items started at or after ``start`` (epoch seconds, or a date or ISO time)."""
    if start is None:
        return list(items)
    bound = start if isinstance(start, (int, float)) else parse_time(start)
    if bound is None:
        raise ValueError(f"unreadable date {start!r}; use YYYY-MM-DD")
    return [i for i in items if i.started_at is not None and as_seconds(i.started_at) >= bound]


def summary(items: Iterable[WorkItemMetrics], since_: str | float | None = None) -> dict:
    """Counts, means and medians over known values, the unknowns counted beside them, and the delivered rate."""
    items = since(items, since_)
    rows: dict[str, dict] = {}
    for name in METRICS:
        if name == "delivered":
            continue
        values = [float(i.metric(name).value) for i in items if i.metric(name).known]
        rows[name] = {"known": len(values), "unknown": len(items) - len(values),
                      "total": round(sum(values), 6) if values else None,
                      "mean": round(statistics.fmean(values), 6) if values else None,
                      "median": round(statistics.median(values), 6) if values else None}
    delivered = sum(1 for i in items if i.delivered.value is True)
    pending = sum(1 for i in items if not i.delivered.known)
    opened = sum(1 for i in items if i.opened.value is True)
    return {"items": len(items), "since": since_, "opened": opened, "cancelled": sum(1 for i in items if i.cancelled),
            "delivered": {"yes": delivered, "no": sum(1 for i in items if i.delivered.value is False), "unknown": pending},
            "delivered_rate": round(delivered / len(items), 6) if items and not pending else None,
            "delivered_rate_bounds": [round(delivered / len(items), 6), round((delivered + pending) / len(items), 6)]
            if items else None,
            "opened_rate": round(opened / len(items), 6) if items else None,
            "cost_basis": {basis: sum(1 for i in items if i.cost_basis == basis) for basis in COST_BASES},
            "metrics": rows}


# Rendering for the terminal: a table, CSV, and the check CI can run.

COLUMNS = ("thread", "class", "started", "cost", "turns", "hours", "fix", "web", "githits", "opened", "delivered",
           "rejections", "interventions")


def _cell(metric: Metric, money: bool = False) -> str:
    if not metric.known:
        return UNKNOWN
    if isinstance(metric.value, bool):
        return "yes" if metric.value else "no"
    return f"${metric.value:.4f}" if money else str(metric.value)


def table_rows(items: Iterable[WorkItemMetrics]) -> list[list[str]]:
    rows = []
    for i in items:
        rows.append([i.thread[:8], i.act_class or "", (utc(i.started_at) or "")[:16].replace("T", " "),
                     f"{_cell(i.cost, money=True)} ({i.cost_basis}{', ' + i.lane if i.lane else ''})", _cell(i.turns),
                     _cell(i.agent_time),
                     _cell(i.fix_rounds), _cell(i.web_lookups), _cell(i.githits_lookups),
                     ("#" + i.pull_request) if i.opened.value else "no", _cell(i.delivered),
                     _cell(i.review_rejections), _cell(i.human_interventions)])
    return rows


def table(items: Sequence[WorkItemMetrics]) -> str:
    rows = [list(COLUMNS)] + table_rows(items)
    widths = [max(len(row[n]) for row in rows) for n in range(len(COLUMNS))]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows)


def to_csv(items: Iterable[WorkItemMetrics]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["thread", "contract", "act_class", "started_at", "unattended", "cancelled", "pull_request",
                     "cost_basis", "lane", "opened", *METRICS])
    for i in items:
        writer.writerow([i.thread, i.contract or "", i.act_class or "", utc(i.started_at) or "", i.unattended, i.cancelled,
                         i.pull_request or "", i.cost_basis, i.lane or "", i.opened.value,
                         *[UNKNOWN if not i.metric(name).known else i.metric(name).value for name in METRICS]])
    return out.getvalue()


def incomplete(items: Iterable[WorkItemMetrics]) -> list[tuple[str, list[str]]]:
    """Delivered items with an unknown metric: what ``syberlabs metrics --check`` fails on."""
    return [(i.thread, i.unknown()) for i in items if i.delivered.value is True and i.unknown()]


# The facts the host writes for this suite. Their shapes live here so the writer (build) and the reader agree.

def _text(value, limit: int, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        raise ValueError(f"{what} must be printable text of at most {limit} characters")
    if _SECRET.search(value):
        raise ValueError(f"{what} looks like it contains a credential; leave secrets out of the record")
    return value


def intervention_value(kind: str, by: str, at: str, note: str | None = None, proposal: str | None = None) -> dict:
    """``human_intervention``: ``{kind, by, at, note}`` and, for an act on a proposal, its id."""
    if kind not in INTERVENTION_KINDS:
        raise ValueError("kind must be one of " + ", ".join(INTERVENTION_KINDS))
    if not isinstance(by, str) or not 0 < len(by) <= 254 or not by.isprintable():
        raise ValueError("by must name the acting person")
    found = {"kind": kind, "by": by, "at": at, "note": _text(note, MAX_NOTE, "note")}
    if proposal is not None:
        found["proposal"] = _text(proposal, 64, "proposal")
    return found


def review_value(verdict: str, reviewer: str, revision: str, by: str, at: str, findings_high: int | None = None,
                 url: str | None = None) -> dict:
    """``review``: a verifier's verdict on one revision, ``{verdict, reviewer, revision, findings_high, comment_url, by, at}``."""
    if verdict not in VERDICTS:
        raise ValueError("verdict must be pass or fail")
    if not isinstance(reviewer, str) or not _NAME.match(reviewer):
        raise ValueError("reviewer must be a short name (letters, digits, _ . @ + -)")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,127}", revision):
        raise ValueError("revision must be a commit or a short revision name")
    if findings_high is not None and _count(findings_high) is None:
        raise ValueError("findings_high must be a count")
    if url is not None and (not isinstance(url, str) or not _URL.match(url)):
        raise ValueError("url must be an https URL")
    return {"verdict": verdict, "reviewer": reviewer, "revision": revision, "findings_high": findings_high,
            "comment_url": url, "by": _text(by, 254, "by"), "at": at}
