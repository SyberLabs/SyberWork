"""The thin-slice driver (AM1): an approved feature map, compiled to tickets, built by Build Threads, stopped by an envelope.

    loop = Loop(repo_root=".", kit_home=".syberlabs", ledger=Ledger(".syberlabs/factory"), map=document, spec=spec,
                envelope=envelope, policy=OfflinePolicy(), proposer_argv=[python, "standin.py", "."],
                checks={"tests": {"argv": [python, "-m", "unittest", "-q"], "timeout_seconds": 300}},
                hidden_catalogue={"hidden.f1.marker": [python, "-c", "..."]}, human_owner="mateo")
    loop.compile()          # refuses a map without a current owner approval; tickets go on the ledger
    summary = loop.run()    # features end "ready for owner merge", parked, escalated or waiting; nothing merges

What it does, in order, every step: reads the kill switch; computes the eligible features from the map and
the record (dependencies ready, declared overlap order respected, act class inside the envelope, attempts
left, room in the envelope); asks the policy ``next_feature``, ``proposer_arm`` and ``budget_tier`` (each
answer is recorded on the factory ledger as a proposal before anything happens); reserves the tier on the
envelope ledger; starts one unattended Build Thread on a contract compiled from the feature's ticket (its
scope is the feature's scope, its checks are the repository's visible checks plus the hidden checks the
host holds under the names the ticket lists); runs the proposer command once through ``CommandProvider``;
then drives the thread with ``syberlabs run``'s own ``Runner`` (consolidation D17: recover, check and
accept are the kernel's, never re-implemented here) until the pass is idle; settles the envelope from the
thread's journal; and records the outcome. A failed attempt asks ``worth_continuing`` and ``after_failure``.

What it never does: merge, push, open a pull request, edit the map, the spec, the envelope or a contract
after compile, call a model or a network. The terminal state of a feature is ``ready_for_owner_merge``: an
accepted candidate on ``syberlabs/<thread>``, with the thread id and commit on the ledger for the owner.

Events go on #154's ledger. Its event vocabulary is owned by ``ledger.py`` (and by the goal-gate packet
that is editing it), so this slice writes its events as ``note`` with ``body.note`` naming the autonomy
event and ``body.refs`` holding the values that differ between two runs of the same fixture (thread ids,
commits, seconds). ``event_digests`` digests every autonomy event without ``refs``, so two runs of one
fixture give one list of digests. Tickets use the existing ``ticket_opened`` kind with ``source: map``.

The envelope ledger (D13) is the eval spend ledger's shape (one JSON object per line, appended before the
work and settled after it, read strictly) generalized to attempts: ``reserve`` before an attempt holds the
tier's dollars and one call, ``settle`` replaces the hold with what the journal recorded, ``stop`` closes
the run with a reason. Totals are the settled values plus unsettled holds, so a killed attempt stays counted.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from syberlabs.build import ACT_CLASSES, PROMOTE, Kit, Runner
from syberlabs.canonical import canonical, digest
from syberlabs.errors import Rejected
from syberlabs.factory import PROTOCOL, featuremap, policy as policymod
from syberlabs.providers import CommandProvider

ENVELOPE_SCHEMA = "syberfactory/envelope.v1"
ENVELOPE_LINE_SCHEMA = "syberlabs.factory/envelope-ledger.v1"
ENVELOPE_FILE = "envelope.jsonl"
ACTIONS = ("start_thread", "propose", "check", "accept_to_thread_branch")  # closed; no merge, push or publish
EVENTS = ("map_frozen", "policy_proposal", "envelope_reserved", "envelope_settled", "attempt_started", "thread_step",
          "feature_ready", "feature_failed", "feature_parked", "feature_escalated", "feature_waiting", "loop_stopped")
STATES = ("open", "building", "ready_for_owner_merge", "parked", "escalated", "waiting_owner_merge")
MAX_PASSES = 8  # Runner passes per attempt: check, accept, idle; more only when evidence ages or recovery runs


# -- the envelope ----------------------------------------------------------------------------------------------------

def new_envelope(map_digest: str, *, dollars_total: float, calls_total: int, seconds_total: float,
                 attempts_per_feature: int = 2, tiers: dict | None = None, arms: list[dict] | None = None,
                 act_classes: tuple = ("R",), max_candidates: int = 3, kill_switch: str | None = None) -> dict:
    """The owner's grant: what the loop may spend, which classes it may build, which arms it may run."""
    tiers = tiers or {"S": 0.0, "M": 0.0, "L": 0.0}
    arms = arms or [{"id": "standin", "tier_rank": 0, "revision": "standin"}]
    if set(tiers) != set(policymod.TIERS) or any(not isinstance(v, (int, float)) or v < 0 for v in tiers.values()):
        raise Rejected("envelope_invalid", "tiers must price S, M and L with non-negative dollars")
    if not arms or any(not isinstance(a, dict) or not a.get("id") for a in arms):
        raise Rejected("envelope_invalid", "arms must be a non-empty list of {id, tier_rank, revision}")
    if any(c not in ACT_CLASSES for c in act_classes):
        raise Rejected("envelope_invalid", f"act_classes must be from {ACT_CLASSES}")
    for name, value in (("dollars_total", dollars_total), ("seconds_total", seconds_total)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise Rejected("envelope_invalid", f"{name} must be a finite number of at least 0")
    if type(calls_total) is not int or calls_total < 0 or type(attempts_per_feature) is not int or attempts_per_feature < 1:
        raise Rejected("envelope_invalid", "calls_total is an int of at least 0 and attempts_per_feature an int of at least 1")
    return {"schema": ENVELOPE_SCHEMA, "map_digest": map_digest,
            "dollars": {"total": float(dollars_total), "tiers": {k: float(tiers[k]) for k in policymod.TIERS}},
            "calls": {"total": calls_total}, "seconds": {"total": float(seconds_total)},
            "attempts_per_feature": attempts_per_feature, "concurrency": 1, "act_classes": sorted(act_classes),
            "arms": [dict(a) for a in arms], "actions": list(ACTIONS), "max_candidates": max_candidates,
            "kill_switch": kill_switch}


def envelope_digest(envelope: dict) -> str:
    return digest(envelope)


def _strict_loads(text: str):
    def no_repeats(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"repeated key {key!r}")
            out[key] = value
        return out

    def no_constant(name):
        raise ValueError(f"non-finite number {name}")

    return json.loads(text, object_pairs_hook=no_repeats, parse_constant=no_constant)


class EnvelopeLedger:
    """Append-only lines under the factory home. Reserve, settle, stop; totals from the lines."""

    def __init__(self, path: str | Path, envelope: dict):
        self.path, self.envelope = Path(path), envelope
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def lines(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for number, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                value = _strict_loads(raw)
            except ValueError as exc:
                raise Rejected("envelope_unreadable", f"{self.path}: line {number}: {exc}") from None
            if not isinstance(value, dict) or value.get("schema") != ENVELOPE_LINE_SCHEMA:
                raise Rejected("envelope_unreadable", f"{self.path}: line {number} is not an envelope ledger line")
            out.append(value)
        return out

    def _append(self, line: dict) -> dict:
        line = {"schema": ENVELOPE_LINE_SCHEMA, "envelope_digest": envelope_digest(self.envelope), **line}
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(line) + "\n")
            handle.flush()
        return line

    def totals(self) -> dict:
        reserved: dict[tuple, dict] = {}
        settled: dict[tuple, dict] = {}
        stopped = None
        for line in self.lines():
            key = (line.get("feature"), line.get("attempt"))
            if line["kind"] == "reserve":
                reserved[key] = line
            elif line["kind"] == "settle":
                settled[key] = line
            elif line["kind"] == "stop":
                stopped = line["reason"]
        dollars = calls = seconds = 0.0
        for key, hold in reserved.items():
            actual = settled.get(key, hold)
            dollars += float(actual["dollars"])
            calls += int(actual["calls"])
            seconds += float(actual["seconds"])
        return {"dollars": round(dollars, 6), "calls": int(calls), "seconds": round(seconds, 3), "attempts": len(reserved),
                "unsettled": len(set(reserved) - set(settled)), "stopped": stopped,
                "dollars_left": round(self.envelope["dollars"]["total"] - dollars, 6),
                "calls_left": self.envelope["calls"]["total"] - int(calls),
                "seconds_left": round(self.envelope["seconds"]["total"] - seconds, 3)}

    def room(self, tier: str, calls: int = 1) -> bool:
        totals = self.totals()
        return (totals["stopped"] is None and totals["dollars_left"] >= self.envelope["dollars"]["tiers"][tier]
                and totals["calls_left"] >= calls and totals["seconds_left"] > 0)

    def reserve(self, feature: str, attempt: int, tier: str, arm: str) -> dict:
        if not self.room(tier):
            raise Rejected("envelope_exhausted", f"no room for tier {tier} on {feature}: {self.totals()}")
        return self._append({"kind": "reserve", "feature": feature, "attempt": attempt, "tier": tier, "arm": arm,
                             "dollars": self.envelope["dollars"]["tiers"][tier], "calls": 1, "seconds": 0.0})

    def settle(self, feature: str, attempt: int, *, dollars: float, calls: int, seconds: float, unknown_models: list) -> dict:
        if not isinstance(dollars, (int, float)) or not math.isfinite(dollars) or dollars < 0:
            raise Rejected("envelope_unpriced", f"{feature} attempt {attempt}: dollars are not a finite number of at least 0")
        return self._append({"kind": "settle", "feature": feature, "attempt": attempt, "dollars": float(dollars), "calls": int(calls),
                             "seconds": round(float(seconds), 3), "unknown_models": list(unknown_models)})

    def stop(self, reason: str) -> dict:
        return self._append({"kind": "stop", "reason": reason})


# -- ledger events -----------------------------------------------------------------------------------------------------

def record(ledger, note: str, map_digest: str, body: dict, refs: dict | None = None) -> dict:
    if note not in EVENTS:
        raise Rejected("autonomy_event", f"unknown autonomy event {note!r}")
    return ledger.append("note", {"note": note, "map_digest": map_digest, **body, "refs": dict(refs or {})})


def autonomy_events(ledger, map_digest: str | None = None) -> list[dict]:
    """The slice's events in ledger order: ``note`` events it wrote and ``ticket_opened`` with ``source: map``."""
    out = []
    for event in ledger.events():
        body = event["body"]
        if event["kind"] == "note" and body.get("note") in EVENTS or event["kind"] == "ticket_opened" and body.get("source") == "map":
            if map_digest is None or body.get("map_digest") == map_digest:
                out.append(event)
    return out


def event_digests(ledger, map_digest: str | None = None) -> list[str]:
    """One digest per autonomy event over its kind and body without ``refs``: the same fixture gives the same list."""
    return [digest({"kind": e["kind"], "body": {k: v for k, v in e["body"].items() if k != "refs"}})
            for e in autonomy_events(ledger, map_digest)]


def why(ledger, ticket_id: str) -> dict:
    """The §1.3 walk as far as the ledger goes: ticket -> feature -> map (its approval) -> spec reference."""
    opened = next((e for e in ledger.events() if e["kind"] == "ticket_opened" and e["body"].get("ticket_id") == ticket_id), None)
    if opened is None:
        raise Rejected("ticket_unknown", f"no ticket_opened event for {ticket_id}")
    body = opened["body"]
    frozen = next((e for e in ledger.events() if e["kind"] == "note" and e["body"].get("note") == "map_frozen"
                   and e["body"].get("map_digest") == body.get("map_digest")), None)
    ready = next((e for e in ledger.events() if e["kind"] == "note" and e["body"].get("note") == "feature_ready"
                  and e["body"].get("ticket_id") == ticket_id), None)
    return {"ticket": ticket_id, "feature": body.get("feature_id"), "requirements": body.get("req_ids"),
            "map": {"digest": body.get("map_digest"), "frozen_seq": frozen["seq"] if frozen else None,
                    "approved_by": (frozen or {}).get("body", {}).get("approved_by")},
            "spec": {"spec_id": body.get("spec_id"), "version": body.get("version"), "digest": (frozen or {}).get("body", {}).get("spec_digest")},
            "ready_for_owner_merge": {"seq": ready["seq"], **ready["body"]["refs"]} if ready else None}


# -- contracts -----------------------------------------------------------------------------------------------------------

def feature_contract(ticket: dict, *, checks: dict, hidden_catalogue: dict, max_candidates: int) -> dict:
    """One ``repo-change``-shaped contract per ticket: scope from the feature, the visible checks plus the hidden
    checks the host holds under the ticket's names, every check required. The id carries the ticket id so a
    changed map never collides with a published contract (contracts are immutable once installed)."""
    found = {name: {"argv": list(c["argv"]), "timeout_seconds": int(c.get("timeout_seconds", 300))} for name, c in checks.items()}
    for exit_criterion in ticket["exit_criteria"]:
        if not exit_criterion.startswith("hidden:"):
            continue
        name = exit_criterion[len("hidden:"):]
        argv = hidden_catalogue.get(name)
        if not argv:
            raise Rejected("hidden_check_unknown", f"ticket {ticket['ticket_id']} names hidden check {name!r} the host does not hold")
        if name in found:
            raise Rejected("hidden_check_collision", f"hidden check {name!r} has the name of a visible check")
        found[name] = {"argv": list(argv), "timeout_seconds": 120}
    if not found:
        raise Rejected("contract_unchecked", "a feature contract needs at least one check")
    return {"id": f"syberfactory-{ticket['ticket_id']}", "version": 1, "title": ticket["title"],
            "inputs": {"objective": "string", "act_class": "string"},
            "actions": {PROMOTE: {}}, "acceptance": [{"id": "accepted", "kind": "effect", "action": PROMOTE}],
            "evolution": {"scope": dict(ticket["scope"]), "operators": ["patch"],
                          "budget": {"max_candidates": max_candidates, "max_evaluations": 2 * max_candidates, "max_seconds": 900},
                          "evaluation": {"checks": found, "required": sorted(found), "max_age_seconds": 3600},
                          "promotion": {"action": PROMOTE, "roles": ["developer"]}}}


# -- the loop -------------------------------------------------------------------------------------------------------------

class Loop:
    def __init__(self, *, repo_root: str | Path, kit_home: str | Path, ledger, map: dict, spec: dict, envelope: dict,
                 policy, proposer_argv: list[str], checks: dict, hidden_catalogue: dict | None = None, human_owner: str,
                 baseline_run: str | None = None, actor: str = "factory@syberlabs.host", log=None, max_passes: int = MAX_PASSES):
        self.repo_root, self.kit_home, self.ledger = Path(repo_root), Path(kit_home), ledger
        self.map, self.spec, self.envelope, self.policy = map, spec, envelope, policy
        self.proposer_argv, self.checks, self.hidden = list(proposer_argv), dict(checks), dict(hidden_catalogue or {})
        self.human_owner, self.baseline_run, self.actor = human_owner, baseline_run, actor
        self.log = log or (lambda line: None)
        self.max_passes = max_passes
        self.map_digest = featuremap.map_digest(map)
        if envelope.get("map_digest") != self.map_digest:
            raise Rejected("envelope_map", "the envelope names a different map digest")
        self.spend = EnvelopeLedger(self.ledger.home / ENVELOPE_FILE, envelope)
        self.graph = None
        self.plan = None
        self.tickets: dict[str, dict] = {}
        self.features = {f["id"]: f for f in map["features"]}
        self.state = {fid: {"state": "open", "attempts": 0, "dollars": 0.0, "thread": None, "commit": None, "last_reason": None}
                      for fid in self.features}
        self.kit = None
        self.order: list[str] = []

    # -- compile -----------------------------------------------------------------------------------------------------------

    def compile(self) -> list[dict]:
        """Refuse a map without a current owner approval or with findings; write one contract per feature; log
        ``map_frozen`` and one ``ticket_opened`` per feature. Idempotent for one map digest."""
        if not featuremap.approved(self.map):
            raise Rejected("map_unapproved", "the loop runs only an owner-approved map whose approval covers its current digest")
        if self.spec.get("state") not in ("promoted", "canonical"):
            raise Rejected("spec_state", f"work opens under a promoted spec, not one in state {self.spec.get('state')!r}")
        findings = featuremap.mapcheck(self.map, self.spec)
        if findings:
            raise Rejected("map_invalid", "; ".join(findings))
        self.graph = featuremap.taskgraph(self.map)
        self.plan = featuremap.risk_plan(self.map, self.graph)
        tickets = featuremap.compile_tickets(self.map, self.spec, human_owner=self.human_owner, baseline_run=self.baseline_run,
                                             space=self.ledger.latest_space())
        contracts = self.kit_home / "contracts"
        contracts.mkdir(parents=True, exist_ok=True)
        for ticket in tickets:
            contract = feature_contract(ticket, checks=self.checks, hidden_catalogue=self.hidden,
                                        max_candidates=self.envelope["max_candidates"])
            path = contracts / f"{contract['id']}.v{contract['version']}.json"
            if not path.exists():
                path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.tickets[ticket["map_ref"]["feature_id"]] = ticket
        already = any(e["body"].get("note") == "map_frozen" and e["body"].get("map_digest") == self.map_digest
                      for e in self.ledger.events() if e["kind"] == "note")
        if not already:
            record(self.ledger, "map_frozen", self.map_digest, {
                "map_id": self.map["id"], "spec_id": self.spec["spec_id"], "spec_version": self.spec["version"],
                "spec_digest": self.map["spec_ref"]["spec_digest"], "approved_by": self.map["approval"]["actor"],
                "envelope_digest": envelope_digest(self.envelope), "features": [f["id"] for f in self.map["features"]],
                "critical_path": self.graph["critical_path"], "critical_path_length": self.graph["critical_path_length"],
                "width": self.graph["width"], "work_critical_ratio": self.graph["work_critical_ratio"],
                "risk": self.plan["r"], "verification_units": self.plan["units"], "tau": self.plan["tau"], "floor": self.plan["floor"]})
            for ticket in tickets:
                self.ledger.append("ticket_opened", {
                    "ticket_id": ticket["ticket_id"], "spec_id": self.spec["spec_id"], "version": self.spec["version"],
                    "req_id": ticket["spec_ref"]["req_ids"][0], "req_ids": ticket["spec_ref"]["req_ids"], "source": "map",
                    "map_digest": self.map_digest, "feature_id": ticket["map_ref"]["feature_id"],
                    "footprint": ticket["footprint"]["components"], "files": ticket["footprint"]["files"],
                    "revision": self.map["base"], "rubric_digest": ticket["rubric_digest"], "human_owner": ticket["human_owner"]})
        return tickets

    # -- state and eligibility --------------------------------------------------------------------------------------------

    def _ready(self, fid: str) -> bool:
        return self.state[fid]["state"] == "ready_for_owner_merge"

    def eligible(self) -> list[str]:
        out = []
        for fid, item in self.features.items():
            row = self.state[fid]
            if row["state"] != "open":
                continue
            if item["act_class"] not in self.envelope["act_classes"]:
                row["state"] = "escalated"
                record(self.ledger, "feature_escalated", self.map_digest, {"feature": fid, "reason": f"act_class_{item['act_class']}_outside_envelope"})
                continue
            if not all(self._ready(d) for d in item["depends_on"]):
                continue
            if item["depends_on"] and self.map["integration"] == "await_merge":
                row["state"] = "waiting_owner_merge"
                record(self.ledger, "feature_waiting", self.map_digest, {"feature": fid, "waits_for": list(item["depends_on"])})
                continue
            if any(o["order"] == f"{o['with']}_first" and not self._ready(o["with"]) and self.state[o["with"]]["state"] == "open"
                   for o in item["overlaps"]):
                continue  # the declared order says the other feature's scope goes first
            if row["attempts"] >= self.envelope["attempts_per_feature"]:
                continue
            out.append(fid)
        return out

    def policy_state(self) -> dict:
        """Ids and numbers only."""
        totals = self.spend.totals()
        tiers = self.envelope["dollars"]["tiers"]
        return {"features": {fid: {"bottom_level": self.graph["bottom_level"][fid], "slack": self.graph["slack"][fid],
                                   "topo_level": self.graph["topo_level"][fid], "risk": self.plan["r"][fid],
                                   "size_units": featuremap.DURATION[self.features[fid]["size"]],
                                   "act_rank": featuremap.ACT_RANK[self.features[fid]["act_class"]],
                                   "attempts": self.state[fid]["attempts"],
                                   "attempts_left": self.envelope["attempts_per_feature"] - self.state[fid]["attempts"],
                                   "dollars_used": self.state[fid]["dollars"], "map_order": self.order.index(fid),
                                   "state": STATES.index(self.state[fid]["state"])} for fid in self.features},
                "envelope": {"dollars_left": totals["dollars_left"], "calls_left": totals["calls_left"], "seconds_left": totals["seconds_left"],
                             "smallest_tier_dollars": min(tiers.values()), "attempts_per_feature": self.envelope["attempts_per_feature"],
                             "arms": [{"id": a["id"], "tier_rank": a.get("tier_rank", 0)} for a in self.envelope["arms"]],
                             "tiers": dict(tiers)}}

    def ask(self, key: str, feature: str | None, options: list[str], numbers: dict | None = None) -> policymod.Answer:
        question = policymod.Question(key, feature, tuple(options), dict(numbers or {}))
        answer = policymod.ask(self.policy, self.policy_state(), question)
        record(self.ledger, "policy_proposal", self.map_digest, {"question": key, "feature": feature, "options": list(options),
                                                                 "answer": answer.record(), "policy": getattr(self.policy, "name", "policy")})
        self.log(f"policy {key} {feature or '-'} -> {answer.value} ({answer.source}, {answer.reason})"
                 + (f"; shadow {answer.shadow['policy']} would say {answer.shadow['value']}" if answer.shadow else ""))
        return answer

    # -- run ----------------------------------------------------------------------------------------------------------------

    def run(self, max_steps: int = 50) -> dict:
        if not self.tickets:
            self.compile()
        self.order = [f["id"] for f in self.map["features"]]
        kill = self.envelope.get("kill_switch")
        stopped = None
        self.kit = Kit.local(self.kit_home, self.repo_root, actor=self.actor)
        try:
            for _ in range(max_steps):
                if kill and Path(kill).exists():
                    stopped = "kill_switch"
                    break
                if self.spend.totals()["stopped"]:
                    stopped = self.spend.totals()["stopped"]
                    break
                eligible = self.eligible()
                if not eligible:
                    stopped = "nothing_eligible"
                    break
                tiers = [t for t in policymod.TIERS if self.spend.room(t)]
                if not tiers:
                    stopped = "envelope_exhausted"
                    break
                chosen = self.ask("next_feature", None, eligible).value
                arms = [a["id"] for a in self.envelope["arms"]]
                arm = self.ask("proposer_arm", chosen, arms).value
                tier = self.ask("budget_tier", chosen, tiers).value
                self._attempt(chosen, arm, tier)
            else:
                stopped = "max_steps"
        finally:
            self.kit.close()
        self.spend.stop(stopped)
        record(self.ledger, "loop_stopped", self.map_digest, {"reason": stopped, "states": {fid: row["state"] for fid, row in self.state.items()},
                                                               "calls": self.spend.totals()["calls"], "dollars": self.spend.totals()["dollars"]})
        return self.summary(stopped)

    def summary(self, stopped: str | None = None) -> dict:
        totals = self.spend.totals()
        return {"stopped": stopped or totals["stopped"], "map_digest": self.map_digest,
                "features": {fid: {"state": row["state"], "attempts": row["attempts"], "ticket": self.tickets.get(fid, {}).get("ticket_id"),
                                   "thread": row["thread"], "commit": row["commit"], "last_reason": row["last_reason"]}
                             for fid, row in self.state.items()},
                "ready_for_owner_merge": [fid for fid, row in self.state.items() if row["state"] == "ready_for_owner_merge"],
                "envelope": totals, "events": len(autonomy_events(self.ledger, self.map_digest))}

    def _attempt(self, fid: str, arm_id: str, tier: str) -> None:
        item, ticket, row = self.features[fid], self.tickets[fid], self.state[fid]
        row["attempts"] += 1
        attempt = row["attempts"]
        self.spend.reserve(fid, attempt, tier, arm_id)
        record(self.ledger, "envelope_reserved", self.map_digest, {"feature": fid, "attempt": attempt, "tier": tier, "arm": arm_id,
                                                                   "dollars": self.envelope["dollars"]["tiers"][tier]})
        arm = next(a for a in self.envelope["arms"] if a["id"] == arm_id)
        started = time.monotonic()
        stacked = None
        if item["depends_on"] and self.map["integration"] == "stack":
            stacked = self.state[item["depends_on"][-1]]["commit"]  # one branch: the last dependency's accepted commit
            self.kit.repo.git("checkout", "-q", "-B", f"syberfactory/{self.map['id']}/{fid}", stacked)
        objective = f"{fid}: {ticket['objective']}\nCriteria: " + "; ".join(f"{c['id']} {c['text']}" for c in ticket["acceptance_criteria"])
        contract = f"syberfactory-{ticket['ticket_id']}.v1"
        thread = self.kit.start(objective, contract=contract, paths=item["scope"]["paths"], act_class=item["act_class"], unattended=True)
        row["state"], row["thread"] = "building", thread.id
        record(self.ledger, "attempt_started", self.map_digest, {"feature": fid, "attempt": attempt, "ticket_id": ticket["ticket_id"],
                                                                 "arm": arm_id, "tier": tier, "contract": contract, "stacked": stacked is not None},
               refs={"thread": thread.id, "base": thread.base, "stacked_on": stacked})
        provider = CommandProvider(self.proposer_argv, name=_name(arm_id), revision=str(arm.get("revision", "1")),
                                   timeout=int(arm.get("timeout_seconds", 300)))
        outcome, reason = "failed", None
        try:
            candidates = thread.propose(provider)
            if not candidates:
                reason = "no_candidate"
            else:
                outcome, reason = self._drive(thread, fid, attempt)
        except Rejected as exc:
            reason = exc.code
        seconds = time.monotonic() - started
        dollars, unknown = _spend(thread.history())
        row["dollars"] = round(row["dollars"] + dollars, 6)
        self.spend.settle(fid, attempt, dollars=dollars, calls=provider.calls, seconds=seconds, unknown_models=unknown)
        record(self.ledger, "envelope_settled", self.map_digest, {"feature": fid, "attempt": attempt, "dollars": dollars,
                                                                  "calls": provider.calls, "unknown_models": unknown},
               refs={"seconds": round(seconds, 3)})
        if outcome == "accepted":
            accepted = next(v for v in thread._views() if v["authoritative"])
            row["state"], row["commit"], row["last_reason"] = "ready_for_owner_merge", accepted["commit"], None
            record(self.ledger, "feature_ready", self.map_digest, {"feature": fid, "attempt": attempt, "ticket_id": ticket["ticket_id"],
                                                                   "exit_criteria": ticket["exit_criteria"], "next": "owner merges; nothing here merges"},
                   refs={"thread": thread.id, "commit": accepted["commit"], "target_ref": thread.target_ref})
            self.log(f"{fid} ready for owner merge: {thread.target_ref} at {accepted['commit'][:10]}")
            return
        row["last_reason"] = reason
        record(self.ledger, "feature_failed", self.map_digest, {"feature": fid, "attempt": attempt, "reason": reason}, refs={"thread": thread.id})
        self._after_failure(fid)

    def _drive(self, thread, fid: str, attempt: int) -> tuple[str, str | None]:
        """``syberlabs run``'s passes over this one thread until nothing is left: the kernel checks, accepts and
        recovers; this records what each pass did."""
        runner = Runner(self.kit, thread=thread.id, log=self.log)
        for number in range(1, self.max_passes + 1):
            steps = runner.once()
            for step in steps:
                record(self.ledger, "thread_step", self.map_digest, {"feature": fid, "attempt": attempt, "pass": number, "step": step.step,
                                                                     "outcome": step.outcome, "reason": step.reason}, refs={"thread": thread.id})
            if any(s.step == "accept" and s.outcome == "succeeded" for s in steps):
                return "accepted", None
            refused = [s for s in steps if s.refused]
            if refused:
                return "failed", refused[0].reason or refused[0].outcome
            if all(s.step in ("idle", "skip") for s in steps):
                idle = steps[-1] if steps else None
                return "failed", (idle.reason if idle else "no_step")
        return "failed", "passes_exhausted"

    def _after_failure(self, fid: str) -> None:
        row, item = self.state[fid], self.features[fid]
        left = self.envelope["attempts_per_feature"] - row["attempts"]
        options = ["continue", "stop"] if left > 0 else ["stop"]
        worth = self.ask("worth_continuing", fid, options).value
        choices = []
        if worth == "continue" and left > 0:
            choices.append("retry_same")
            if len(self.envelope["arms"]) > 1:
                choices.append("retry_escalate_arm")
        choices += ["escalate_owner", "park"]
        decision = self.ask("after_failure", fid, choices).value
        if decision in ("retry_same", "retry_escalate_arm"):
            row["state"] = "open"  # eligible again; the next step may pick it or another feature
            return
        row["state"] = "escalated" if decision == "escalate_owner" else "parked"
        record(self.ledger, "feature_escalated" if decision == "escalate_owner" else "feature_parked", self.map_digest,
               {"feature": fid, "reason": row["last_reason"], "attempts": row["attempts"]})


def _name(arm_id: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "_.-" else "-" for c in arm_id.lower())
    return (cleaned if cleaned[:1].isalpha() else "arm-" + cleaned)[:64]


def _spend(history: list[dict]) -> tuple[float, list[str]]:
    """Host-observed dollars from the thread's finished searches, and the models the host could not price."""
    dollars, unknown = 0.0, []
    for event in history:
        if event["kind"] == "search_finished" and isinstance(event["body"].get("spend"), dict):
            spend = event["body"]["spend"]
            dollars += float(spend.get("dollars") or 0.0)
            unknown += [m for m in spend.get("unknown_models", []) if m not in unknown]
    return round(dollars, 6), unknown


def load_json(path: str | Path, what: str) -> dict:
    try:
        return _strict_loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Rejected(f"{what}_unreadable", f"{path}: {type(exc).__name__}: {exc}") from None
