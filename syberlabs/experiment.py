"""A/B comparison of work items: two arms of one experiment, paired by item, every number from the journal.

    syberlabs experiment label --name githits-pilot --item ITEM-14 --arm on --tag needs_lookup
    syberlabs experiment report --name githits-pilot [--delivered review] [--items FILE] [--json | --csv | --markdown]

An experiment runs the same work items through the factory more than once and changes one thing between the runs, its
arms (the GitHits pilot: the proposer's MCP configuration, ``docs/CLAUDE_CODE.md``). A thread joins an experiment through
an ``experiment`` fact, a verified host observation written by ``syberlabs experiment label``
(``Thread.record_experiment``): ``{name, item, arm, tags, by, at}``, versioned by the experiment's name.

``report(events, name)`` folds a journal into one row per item: each arm's threads, their numbers from
``syberlabs.metrics.work_items``, and what the record says about the run. It then compares the treatment arm with the
baseline over the items that ran in both. Like ``metrics``, it reads events and nothing else: it imports nothing from
``build``, ``session`` or ``syberwork``, opens no file, and appends nothing.

What it adds to the per-item metrics:

- ``time_to_pass``: hours from the thread's first search to the host's evaluation of the candidate that was accepted.
  The time the change then waited for a person, or for ``syberlabs run``, is left out on both arms.
- ``accepted``: the contract's promotion succeeded (``effect_succeeded``, or a verified reconciliation of it). Both arms
  of an item can be accepted; only one arm's pull request can merge, so ``agent_time`` (to merge) is reported per arm.
- ``reviewed``: accepted, and a ``review`` fact on the accepted commit passed (unknown until one is recorded).
- every paired number counted up to the thread's first acceptance (cost, turns, fix rounds, lookups, review rejections,
  human interventions), so publishing one arm's change and not the other's, or working on it afterwards, cannot tilt a
  pair. What only the published arm can reach (a pull request, a merge, ``agent_time``) is reported per arm, unpaired.
- ``delivery_rule``: the treatment delivers at least as many items, and over the items delivered in both arms its median
  paired cost is lower; read overall and per tag, and undecided while a delivery or a delivered pair's price is unknown.
- the checks that make a pair comparable, or say why it is not (``CHECKS``), and, when a registered item list is given,
  the registered items that never ran.

Statistics, stated plainly. The item is the unit. Finishing (``accepted``, ``reviewed``) is compared over every valid
item; efficiency (cost, turns, time, fix rounds, lookups, rejections, interventions) over the valid items accepted in
both arms, so a cheap run that never finished is not counted as a win. Within that, a metric is compared over the items
whose value is known in both arms: an unknown is counted, never imputed. An arm that ran an item several times has the
mean of its runs, known only when every run's value is; arms that ran an item a different number of times are excluded.
Values are rounded to nine places before they are compared, so float noise never turns a tie into a loss. Per metric:
the arms' means and medians, the paired differences (treatment minus baseline), the change in the total, wins, losses
and ties, an exact two-sided sign test, and a 95% t interval for the mean difference. The t interval assumes roughly
normal differences; with few items and skewed costs it is too narrow, so read it as a guide. With fewer than six untied
pairs no sign test can reach p < 0.05. Nothing here corrects for testing several metrics at once.
"""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from syberlabs import metrics
from syberlabs.clock import as_seconds
from syberlabs.evidence import verified_reconciliation

KEY = "experiment"
PROMOTE = "accept_change"          # build.PROMOTE, the default contract's promotion action; experiment may not import build
BASELINE, TREATMENT = "off", "on"
SERVER = "githits"                 # the MCP server whose connection tells the arms apart in the GitHits pilot
MAX_TAGS = 8
PLACES = 9
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}$")

# Compared over pairs. Lower is better for all but ``githits_lookups``, which shows use and has no better direction.
COMPARED = ("cost", "turns", "time_to_pass", "fix_rounds", "web_lookups", "githits_lookups", "review_rejections",
            "human_interventions")
NEUTRAL = ("githits_lookups",)
UNITS = {"cost": "$ per item", "turns": "turns per item", "time_to_pass": "hours per item",
         "fix_rounds": "retries per item", "web_lookups": "searches per item", "githits_lookups": "calls per item",
         "review_rejections": "rejections per item", "human_interventions": "interventions per item"}
# Two-sided 95% quantiles of Student's t by degrees of freedom; between rows the smaller row is used (a wider interval).
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
        40: 2.021, 60: 2.000, 120: 1.980}

# A check in ``EXCLUDE`` takes the item out of every comparison; the others are listed and the item stays in.
EXCLUDE = ("missing_arm", "unequal_repeats", "base_mismatch", "objective_mismatch", "arm_conflict", "contaminated",
           "labelled_late", "unregistered", "registration_mismatch")
CHECKS = {
    "missing_arm": "the item has threads in one arm only",
    "unequal_repeats": "the arms ran the item a different number of times, so a mean of several runs would face one run",
    "base_mismatch": "the arms started from different commits",
    "objective_mismatch": "the arms were given different objectives",
    "arm_conflict": "the record contradicts the label: the server was connected in the baseline, or not in the treatment",
    "contaminated": "the baseline made lookups on the treatment's server",
    "labelled_late": "a thread's label was written or changed after its first candidate, so it was not fixed before the "
                     "work ran",
    "unregistered": "the item is not in the registered item list",
    "registration_mismatch": "the item's tags differ from the registered item list",
    "base_unknown": "a thread has no recorded base commit, so the start could not be compared",
    "arm_unverified": "no call recorded its MCP servers, so only the label says which arm ran",
    "server_mixed": "the server was connected in some of a thread's calls and not in others",
    "unused": "the treatment made no lookups on its server",
    "contamination_unknown": "the baseline did not record its lookups on the server, so it cannot be shown clean",
    "relabelled": "a thread's labels disagree, but the one in force is the one that stood when its work began",
    "model_mismatch": "the arms were served by different models",
    "cost_basis_mismatch": "the arms' costs were recorded on different bases or lanes; cost is not compared for this item",
    "outcome_mismatch": "one arm was accepted and the other was not; efficiency is not compared for this item",
    "tag_mismatch": "the arms' threads carry different tags; their union is used",
    "repeats": "each arm ran the item more than once; each arm's mean is used",
    "other_arm": "the item also has threads in arms this report does not compare",
    "unlisted_run": "another thread ran this item's objective from the same commit without this experiment's label",
}


def label_value(name: str, item: str, arm: str, by: str, at: str, tags: Sequence[str] = ()) -> dict:
    """``experiment``: ``{name, item, arm, tags, by, at}``. Names are short identifiers; tags are at most eight."""
    for what, value in (("name", name), ("item", item), ("arm", arm)):
        if not isinstance(value, str) or not _NAME.match(value):
            raise ValueError(f"{what} must be a short name: letters, digits, _ . : + - and at most 64 characters")
    found = list(dict.fromkeys(tags or ()))
    if len(found) > MAX_TAGS or any(not isinstance(tag, str) or not _NAME.match(tag) for tag in found):
        raise ValueError(f"at most {MAX_TAGS} tags, each a short name")
    if not isinstance(by, str) or not 0 < len(by) <= 254 or not by.isprintable():
        raise ValueError("by must name the acting person")
    return {"name": name, "item": item, "arm": arm, "tags": sorted(found), "by": by, "at": at}


def parse_items(text: str) -> dict[str, tuple[str, ...]]:
    """A registered item list: one item per line, ``ITEM-ID [tag ...]``; blank lines and ``#`` comments are skipped."""
    found: dict[str, tuple[str, ...]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        if any(not _NAME.match(word) for word in words) or len(words) - 1 > MAX_TAGS:
            raise ValueError(f"line {number}: an item id and at most {MAX_TAGS} tags, each a short name")
        if words[0] in found:
            raise ValueError(f"line {number}: {words[0]} is listed twice")
        found[words[0]] = tuple(sorted(set(words[1:])))
    return found


def _label(value) -> bool:
    return (isinstance(value, dict) and all(isinstance(value.get(k), str) and _NAME.match(value[k])
                                            for k in ("name", "item", "arm"))
            and isinstance(value.get("tags", []), list) and all(isinstance(t, str) for t in value.get("tags", [])))


def _identity(value: dict) -> tuple:
    return value["item"], value["arm"], tuple(sorted(set(value.get("tags", []))))


@dataclass(frozen=True)
class Run:
    """One thread in one experiment: its label, the metrics station's numbers, and what the record says about the run."""

    thread: str
    item: str
    arm: str
    tags: tuple[str, ...]
    label: str                     # hash of the label in force
    labelled_late: bool
    relabelled: bool
    base: str | None
    objective: str | None
    server: bool | str | None      # connected in every call that recorded its servers, in none, "mixed", or None
    models: tuple[str, ...]
    accepted: metrics.Metric
    time_to_pass: metrics.Metric
    reviewed: metrics.Metric       # accepted, and a review of the accepted commit passed (unknown while unreviewed)
    work: metrics.WorkItemMetrics
    upto: metrics.WorkItemMetrics | None = None  # the same fold over the events up to acceptance

    def value(self, name: str) -> metrics.Metric:
        """A compared number. An accepted thread's work metrics are read up to its first acceptance, so what happened
        to one arm's change afterwards (publishing it, review comments on the pull request, more work) cannot tilt a
        pair. The checks still read the whole thread."""
        if name in ("time_to_pass", "accepted", "reviewed"):
            return getattr(self, name)
        return (self.upto or self.work).metric(name)


def _promotion(contracts, contract) -> str:
    if contracts and isinstance(contract, (list, tuple)) and len(contract) == 2:
        doc = contracts.get(tuple(contract)) or contracts.get(f"{contract[0]}.v{contract[1]}") or {}
        found = ((doc.get("evolution") or {}).get("promotion") or {}).get("action")
        if isinstance(found, str):
            return found
    return PROMOTE


def _servers(registrations: list[dict], server: str | None) -> bool | str | None:
    """Was ``server`` connected? Read from the ``signal.harness.mcp_servers`` a harness recorded (``name:status``)."""
    if server is None:
        return None
    seen = []
    for event in registrations:
        signal = event["body"].get("signal")
        harness = signal.get("harness") if isinstance(signal, dict) else None
        if not isinstance(harness, dict):
            continue
        connected = False
        for entry in harness.get("mcp_servers") or []:
            if isinstance(entry, str):
                name, _, status = entry.partition(":")
            elif isinstance(entry, dict):
                name, status = entry.get("name"), entry.get("status")
            else:
                continue
            connected = connected or (name == server and status == "connected")
        seen.append(connected)
    if not seen:
        return None
    return True if all(seen) else False if not any(seen) else "mixed"


def _models(registrations: list[dict]) -> tuple[str, ...]:
    found = set()
    for event in registrations:
        signal = event["body"].get("signal") if isinstance(event["body"].get("signal"), dict) else {}
        usage, harness = signal.get("model_usage"), signal.get("harness")
        for source in (usage, harness):
            if isinstance(source, dict) and isinstance(source.get("model"), str):
                found.add(source["model"])
    return tuple(sorted(found))


def _reviewed(events: list[dict], accept: dict | None, commit: str | None) -> metrics.Metric:
    """Accepted and passed review: the latest ``review`` fact whose revision is the accepted commit (or a prefix of it, at
    least 7 characters) decides. Not accepted is False; accepted with no review of that commit is unknown."""
    if accept is None:
        return metrics.Metric(False, (), "not accepted")
    found = [(e, v) for e, v in ((e, metrics.fact(e, metrics.REVIEW)) for e in events)
             if v is not None and v.get("verdict") in metrics.VERDICTS and isinstance(v.get("revision"), str)
             and commit is not None and len(v["revision"]) >= 7 and commit.startswith(v["revision"])]
    if not found:
        return metrics.Metric(None, (accept["hash"],), "accepted; no review of the accepted commit in the record")
    event, value = found[-1]
    return metrics.Metric(value["verdict"] == "pass", (accept["hash"], event["hash"]),
                          f"review {value['verdict']} by {value.get('reviewer')}")


def _objective(events: list[dict]) -> str | None:
    created = next((e for e in events if e["kind"] == "case_created"), None)
    inputs = (created["body"].get("inputs") if created else None) or {}
    return inputs.get("objective") if isinstance(inputs, dict) and isinstance(inputs.get("objective"), str) else None


def _base(events: list[dict]) -> str | None:
    bases = [metrics.fact(e, "base") for e in events]
    return next((b.get("commit") for b in reversed(bases) if b and isinstance(b.get("commit"), str)), None)


def _run(case_id: str, events: list[dict], name: str, work: metrics.WorkItemMetrics, contracts,
         server: str | None) -> Run | None:
    labels = [(e, v) for e, v in ((e, metrics.fact(e, KEY)) for e in events) if _label(v) and v["name"] == name]
    if not labels:
        return None
    event, value = labels[-1]
    created = next((e for e in events if e["kind"] == "case_created"), None)
    body = created["body"] if created else {}
    registrations = [e for e in events if e["kind"] == "candidate_registered" and e["body"].get("operator") != "migration"]
    first = registrations[0]["seq"] if registrations else None
    standing = [v for e, v in labels if first is None or e["seq"] < first]  # the labels in force when the work began
    late = first is not None and (not standing or _identity(standing[-1]) != _identity(value))

    action = _promotion(contracts, body.get("contract"))
    proposals = {e["body"].get("id"): e["body"] for e in events if e["kind"] == "proposed"}
    accept = next((e for e in events
                   if (e["kind"] == "effect_succeeded" and e["body"].get("action") == action)
                   or (verified_reconciliation(e) and (proposals.get(e["body"].get("proposal_id")) or {}).get("action")
                       == action)), None)
    accepted = metrics.Metric(accept is not None, (accept["hash"],) if accept else (),
                              f"{action} succeeded" if accept else f"no {action} effect in the record")
    args = (proposals.get(accept["body"].get("proposal_id")) or {}).get("args") if accept else None
    args = args if isinstance(args, dict) else {}
    commit = args.get("commit") if isinstance(args.get("commit"), str) else None
    candidate = args.get("candidate") if isinstance(args.get("candidate"), str) else None

    start = next((e for e in events if e["kind"] == "search_started"), None)
    passed = next((e for e in reversed(events) if accept is not None and e["kind"] == "candidate_evaluated"
                   and e["seq"] < accept["seq"] and e["body"].get("candidate") == candidate), None)
    if start is not None and passed is not None:
        hours = round((as_seconds(passed["at"]) - as_seconds(start["at"])) / 3600, 4)
        time_to_pass = metrics.Metric(hours, (start["hash"], passed["hash"]),
                                      "first search to the evaluation of the accepted candidate")
    else:
        time_to_pass = metrics.Metric(None, (accept["hash"],) if accept else (),
                                      "not accepted" if accept is None else "no search or evaluation of the accepted "
                                                                            "candidate in the record")
    upto = None
    if accept is not None:
        [upto] = metrics.work_items([e for e in events if e["seq"] <= accept["seq"]], contracts)
    return Run(thread=case_id, item=value["item"], arm=value["arm"], tags=tuple(sorted(set(value.get("tags", [])))),
               label=event["hash"], labelled_late=late, relabelled=len({_identity(v) for _e, v in labels}) > 1,
               base=_base(events), objective=_objective(events), server=_servers(registrations, server),
               models=_models(registrations), accepted=accepted, time_to_pass=time_to_pass,
               reviewed=_reviewed(events, accept, commit), work=work, upto=upto)


def _threads(events: Iterable[dict]) -> dict[str, list[dict]]:
    threads: dict[str, list[dict]] = {}
    for event in events:
        threads.setdefault(event["case_id"], []).append(event)
    return {case: sorted(found, key=lambda e: e["seq"]) for case, found in threads.items()}


def runs(events: Iterable[dict], name: str, contracts: Mapping | None = None, server: str | None = SERVER) -> list[Run]:
    """Every thread labelled into experiment ``name``, in order of first appearance. Pure."""
    events = list(events)
    work = {item.thread: item for item in metrics.work_items(events, contracts)}
    found = []
    for case_id, found_events in _threads(events).items():
        run = _run(case_id, found_events, name, work[case_id], contracts, server)
        if run is not None:
            found.append(run)
    return found


def _mean(runs_: Sequence[Run], name: str) -> float | None:
    """An arm's value for one item: the mean over its runs, known only when every run's value is, rounded to nine places."""
    values = [run.value(name) for run in runs_]
    if not values or any(not v.known for v in values):
        return None
    return round(statistics.fmean(float(v.value) for v in values), PLACES)


def _flags(item_runs: dict[str, list[Run]], baseline: str, treatment: str, server: str | None) -> list[str]:
    base_runs, treat_runs = item_runs.get(baseline, []), item_runs.get(treatment, [])
    everyone = base_runs + treat_runs
    flags = []
    if not base_runs or not treat_runs:
        flags.append("missing_arm")
    elif len(base_runs) != len(treat_runs):
        flags.append("unequal_repeats")
    bases = {run.base for run in everyone}
    if None in bases:
        flags.append("base_unknown")
    if len(bases - {None}) > 1:
        flags.append("base_mismatch")
    if len({run.objective for run in everyone}) > 1:
        flags.append("objective_mismatch")
    if server is not None:
        if any(run.server in (True, "mixed") for run in base_runs) or any(run.server is False for run in treat_runs):
            flags.append("arm_conflict")
        if any(run.server is None for run in everyone):
            flags.append("arm_unverified")
        if any(run.server == "mixed" for run in treat_runs):
            flags.append("server_mixed")
        if server == SERVER:
            if any((run.work.githits_lookups.value or 0) > 0 for run in base_runs):
                flags.append("contaminated")
            elif any(not run.work.githits_lookups.known for run in base_runs):
                flags.append("contamination_unknown")
            if treat_runs and all(run.work.githits_lookups.value == 0 for run in treat_runs):
                flags.append("unused")
    every_arm = [run for found in item_runs.values() for run in found]  # a run moved into another arm still counts
    if any(run.labelled_late for run in every_arm):
        flags.append("labelled_late")
    if any(run.relabelled for run in every_arm):
        flags.append("relabelled")
    models = [set(run.models) for run in everyone if run.models]
    if models and any(m != models[0] for m in models):
        flags.append("model_mismatch")
    if len({(run.work.cost_basis, getattr(run.work, "lane", None)) for run in everyone}) > 1:
        flags.append("cost_basis_mismatch")
    if len({run.tags for run in everyone}) > 1:
        flags.append("tag_mismatch")
    if len(base_runs) > 1 and len(base_runs) == len(treat_runs):
        flags.append("repeats")
    if set(item_runs) - {baseline, treatment}:
        flags.append("other_arm")
    return flags


def sign_test(wins: int, losses: int) -> float | None:
    """Exact two-sided sign test over the untied pairs; None when every pair is tied."""
    n = wins + losses
    if n == 0:
        return None
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 6)


def t_interval(differences: Sequence[float]) -> list[float] | None:
    """A 95% Student's t interval for the mean difference. None below two pairs, or when every difference is the same
    (there is no spread to estimate the uncertainty from)."""
    n = len(differences)
    if n < 2:
        return None
    spread = statistics.stdev(differences)
    if spread == 0:
        return None
    t = T975[max(df for df in T975 if df <= n - 1)]
    mean, half = statistics.fmean(differences), t * spread / math.sqrt(n)
    return [round(mean - half, 6), round(mean + half, 6)]


def compare(pairs: Sequence[tuple[float, float]], lower_is_better: bool | None = True) -> dict:
    """Baseline-treatment pairs of one metric: descriptive numbers, wins and losses, the sign test and the interval."""
    found = {"pairs": len(pairs), "baseline_mean": None, "treatment_mean": None, "baseline_median": None,
             "treatment_median": None, "mean_difference": None, "median_difference": None, "change": None,
             "wins": None, "losses": None, "ties": None, "p_value": None, "interval": None}
    if not pairs:
        return found
    base, treat = [b for b, _t in pairs], [t for _b, t in pairs]
    differences = [round(t - b, PLACES) for b, t in pairs]
    found.update({"baseline_mean": round(statistics.fmean(base), 6), "treatment_mean": round(statistics.fmean(treat), 6),
                  "baseline_median": round(statistics.median(base), 6),
                  "treatment_median": round(statistics.median(treat), 6),
                  "mean_difference": round(statistics.fmean(differences), 6),
                  "median_difference": round(statistics.median(differences), 6),
                  "change": round((sum(treat) - sum(base)) / sum(base), 6) if sum(base) > 0 else None,
                  "interval": t_interval(differences)})
    if lower_is_better is not None:
        wins = sum(1 for d in differences if (d < 0 if lower_is_better else d > 0))
        ties = sum(1 for d in differences if d == 0)
        found.update({"wins": wins, "losses": len(differences) - wins - ties, "ties": ties,
                      "p_value": sign_test(wins, len(differences) - wins - ties)})
    return found


def _finished(row: dict, baseline: str, treatment: str) -> bool:
    return row["values"][baseline].get("accepted") == 1.0 and row["values"][treatment].get("accepted") == 1.0


def _comparisons(rows: list[dict], baseline: str, treatment: str) -> dict:
    """Efficiency over the items accepted in both arms; finishing (accepted, reviewed) over every item."""
    finished = [row for row in rows if _finished(row, baseline, treatment)]
    out = {}
    for name in COMPARED:
        pairs, unknown = [], 0
        for row in finished:
            b, t = row["values"][baseline].get(name), row["values"][treatment].get(name)
            if (name == "cost" and "cost_basis_mismatch" in row["flags"]) or b is None or t is None:
                unknown += 1
            else:
                pairs.append((b, t))
        out[name] = {**compare(pairs, None if name in NEUTRAL else True), "unknown": unknown,
                     "unfinished": len(rows) - len(finished), "unit": UNITS[name]}
    out["accepted"] = _binary(rows, "accepted", baseline, treatment, "share of items accepted")
    out["reviewed"] = _binary(rows, "reviewed", baseline, treatment, "share of items accepted and passed review")
    return out


def _binary(rows: list[dict], name: str, baseline: str, treatment: str, unit: str) -> dict:
    """A yes/no outcome per item and arm (a rate when an arm ran the item more than once): each arm's rate over the items
    known in both, the items where only one arm got it, and the sign test over those. Unknown (for example, not yet
    reviewed) is counted, never read as no."""
    pairs = [(row["values"][baseline].get(name), row["values"][treatment].get(name)) for row in rows]
    known = [(b, t) for b, t in pairs if b is not None and t is not None]
    treatment_only = sum(1 for b, t in known if t > b)
    baseline_only = sum(1 for b, t in known if b > t)
    return {"pairs": len(known), "unknown": len(pairs) - len(known), "unit": unit,
            "baseline_rate": round(statistics.fmean(b for b, _ in known), 6) if known else None,
            "treatment_rate": round(statistics.fmean(t for _, t in known), 6) if known else None,
            "treatment_only": treatment_only, "baseline_only": baseline_only,
            "p_value": sign_test(treatment_only, baseline_only)}


def delivery_rule(rows: list[dict], baseline: str, treatment: str, delivered_by: str) -> dict:
    """The go/no-go read from the valid items: the treatment delivers at least as many items as the baseline, and over
    the items delivered in both arms its median paired cost is lower. ``delivered_by`` is ``accept`` (the promotion
    succeeded) or ``review`` (it succeeded and a review of the accepted commit passed).

    ``met`` is False once nothing is pending and the treatment delivered fewer items. It is None (undecided) while an
    item's delivery is unknown, while a pair delivered in both arms has no comparable price, or when no item was
    delivered in both arms, so a pending review never reads as a pass or a fail."""
    key = "accepted" if delivered_by == "accept" else "reviewed"
    pairs = [(row["values"][baseline].get(key), row["values"][treatment].get(key)) for row in rows]
    pending = sum(1 for b, t in pairs if b is None or t is None)
    known = [(b, t) for b, t in pairs if b is not None and t is not None]
    delivered = {baseline: round(sum(b for b, _ in known), 6), treatment: round(sum(t for _, t in known), 6)}
    both = [row for row in rows if row["values"][baseline].get(key) == 1.0 and row["values"][treatment].get(key) == 1.0]
    priced = [(row["values"][baseline]["cost"], row["values"][treatment]["cost"]) for row in both
              if "cost_basis_mismatch" not in row["flags"]
              and row["values"][baseline]["cost"] is not None and row["values"][treatment]["cost"] is not None]
    median = round(statistics.median(round(t - b, PLACES) for b, t in priced), 6) if priced else None
    at_least = delivered[treatment] >= delivered[baseline]
    if not pending and not at_least:
        met = False
    elif pending or len(priced) < len(both) or not priced:
        met = None
    else:
        met = median < 0
    return {"delivered_by": delivered_by, "delivered": delivered, "pending": pending,
            "treatment_at_least_as_many": at_least, "cost_pairs": len(priced), "cost_unknown": len(both) - len(priced),
            "median_cost_difference": median, "treatment_cheaper": (median < 0) if median is not None else None,
            "met": met,
            "rule": "the treatment delivers at least as many items, and over the items delivered in both arms its "
                    "median paired cost is lower"}


def _per_arm(item_runs: Iterable[Run]) -> dict:
    """Unpaired: what each arm's threads show for the metrics only one arm of a pair can reach."""
    found = list(item_runs)
    hours = [float(r.work.agent_time.value) for r in found if r.work.agent_time.known]
    blocked = []
    for run in found:
        try:
            metric = run.work.metric("lookups_blocked")
        except KeyError:
            continue
        if metric.known:
            blocked.append(int(metric.value))
    return {"threads": len(found), "accepted": sum(1 for r in found if r.accepted.value is True),
            "opened": sum(1 for r in found if r.work.opened.value is True),
            "delivered": sum(1 for r in found if r.work.delivered.value is True),
            "agent_time_known": len(hours), "agent_time_mean": round(statistics.fmean(hours), 6) if hours else None,
            "lookups_blocked": sum(blocked) if blocked else None}


def report(events: Iterable[dict], name: str, contracts: Mapping | None = None, *, baseline: str = BASELINE,
           treatment: str = TREATMENT, server: str | None = SERVER, delivered_by: str = "accept",
           registered: Mapping[str, Sequence[str]] | None = None, forgotten: Sequence[Mapping] | None = None) -> dict:
    """The experiment's items, their checks, and the treatment compared with the baseline. Pure.

    ``server`` names the MCP server whose recorded connection must match the arm (``githits`` for the GitHits pilot);
    None skips the arm, contamination and use checks for an experiment that does not turn a server on and off.
    ``delivered_by`` (``accept`` or ``review``) is what counts as delivered in ``delivery_rule``. ``registered`` is the
    item list written down before the first run (``parse_items``): an item it does not list, or lists with other tags,
    is excluded, and a listed item that never ran is named. ``forgotten`` is the home's forget tombstones
    (``Journal.forgotten``): those written after the experiment's first thread started are named, since a forgotten
    thread leaves nothing else behind.
    """
    if baseline == treatment:
        raise ValueError("the baseline and the treatment must be different arms")
    if delivered_by not in ("accept", "review"):
        raise ValueError("delivered_by must be accept or review")
    events = list(events)
    # Threads in start order, then by id, so the rows come out the same whatever order the journal lists threads in.
    found = sorted(runs(events, name, contracts, server),
                   key=lambda r: (r.work.started_at is None, as_seconds(r.work.started_at or 0), r.thread))
    labelled = {run.thread for run in found}
    others: dict[tuple, list[str]] = {}
    for case_id, thread_events in _threads(events).items():
        if case_id not in labelled:
            others.setdefault((_objective(thread_events), _base(thread_events)), []).append(case_id)
    items: dict[str, dict[str, list[Run]]] = {}
    for run in found:
        items.setdefault(run.item, {}).setdefault(run.arm, []).append(run)
    rows = []
    for item, by_arm in items.items():
        flags = _flags(by_arm, baseline, treatment, server)
        everyone = [run for arm in (baseline, treatment) for run in by_arm.get(arm, [])]
        tags = sorted({tag for run in everyone for tag in run.tags})
        if registered is not None:
            if item not in registered:
                flags.append("unregistered")
            else:
                if any(set(run.tags) != set(registered[item]) for run in everyone):
                    flags.append("registration_mismatch")
                tags = sorted(set(registered[item]))
        unlisted = sorted({case for run in everyone for case in others.get((run.objective, run.base), [])})
        if unlisted:
            flags.append("unlisted_run")
        values = {arm: {metric: _mean(by_arm.get(arm, []), metric) for metric in COMPARED + ("accepted", "reviewed")}
                  for arm in (baseline, treatment)}
        b_ok, t_ok = values[baseline]["accepted"], values[treatment]["accepted"]
        if b_ok is not None and t_ok is not None and b_ok != t_ok:
            flags.append("outcome_mismatch")
        rows.append({"item": item, "tags": tags, "flags": flags, "excluded": [f for f in flags if f in EXCLUDE],
                     "threads": {arm: [run.thread for run in by_arm.get(arm, [])] for arm in (baseline, treatment)},
                     "values": values, "other_arms": sorted(set(by_arm) - {baseline, treatment}),
                     "unlisted": unlisted})
    valid = [row for row in rows if not row["excluded"]]
    tags = sorted({tag for row in valid for tag in row["tags"]})
    groups = [(tag, [row for row in valid if tag in row["tags"]]) for tag in tags]
    if tags:
        groups.append(("(untagged)", [row for row in valid if not row["tags"]]))
    by_tag = {tag: {"items": len(chosen), "metrics": _comparisons(chosen, baseline, treatment),
                    "rule": delivery_rule(chosen, baseline, treatment, delivered_by)}
              for tag, chosen in groups if chosen}
    kept = {row["item"] for row in valid}
    first_start = min((as_seconds(r.work.started_at) for r in found if r.work.started_at is not None), default=None)
    gone = [{"thread": t.get("thread"), "forgotten_at": t.get("forgotten_at"), "reason": t.get("reason")}
            for t in (forgotten or ()) if isinstance(t, Mapping) and first_start is not None
            and isinstance(t.get("forgotten_at"), (int, float)) and float(t["forgotten_at"]) >= first_start]
    return {
        "experiment": name, "baseline": baseline, "treatment": treatment, "server": server,
        "items": {"labelled": len(rows), "compared": len(valid), "excluded": len(rows) - len(valid)},
        "threads": len(found),
        "metrics": _comparisons(valid, baseline, treatment),
        "rule": delivery_rule(valid, baseline, treatment, delivered_by),
        "by_tag": by_tag,
        "per_arm": {arm: _per_arm(run for run in found if run.arm == arm and run.item in kept)
                    for arm in (baseline, treatment)},
        "checks": {flag: [row["item"] for row in rows if flag in row["flags"]] for flag in CHECKS
                   if any(flag in row["flags"] for row in rows)},
        "not_run": sorted(set(registered) - set(items)) if registered is not None else None,
        "forgotten": gone if forgotten is not None else None,
        "rows": rows,
        "method": ("item is the unit; finishing over every valid item, efficiency over items accepted in both arms; "
                   "pairs where both arms' value is known; unknown counted, never imputed; repeats averaged, unequal "
                   "repeats excluded; every paired number counted up to the first acceptance; exact two-sided sign test; "
                   "95% t interval of the mean difference; no correction for several metrics"),
    }


# Rendering: a terminal table, the case study's Markdown table, and one CSV row per item.

def _number(value, money: bool = False) -> str:
    if value is None:
        return metrics.UNKNOWN
    if money:
        return f"-${-value:.4f}" if value < 0 else f"${value:.4f}"
    if isinstance(value, float) and abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.3g}" if isinstance(value, float) else str(value)


def _change(value) -> str:
    """The change in the total; "-" when the baseline's total is zero (a ratio of nothing, not an unknown)."""
    return "-" if value is None else f"{value * 100:+.0f}%"


LABELS = {"cost": "Cost", "turns": "Turns", "time_to_pass": "Time to a passing change", "fix_rounds": "Fix rounds",
          "web_lookups": "Web lookups", "githits_lookups": "GitHits lookups", "review_rejections": "Review rejections",
          "human_interventions": "Human interventions"}
TAG_COLUMNS = ("cost", "turns", "fix_rounds", "web_lookups", "githits_lookups")
VERDICT = {True: "met", False: "not met", None: "not decided"}


def _shift(m: dict, name: str) -> str:
    """One metric for one tag: baseline mean to treatment mean, with the change in the total."""
    if not m["pairs"]:
        return metrics.UNKNOWN
    money = name == "cost"
    change = "" if m["change"] is None or name in NEUTRAL else f" ({_change(m['change'])})"
    return f"{_number(m['baseline_mean'], money)} -> {_number(m['treatment_mean'], money)}{change}"


def _rule_line(found: dict) -> str:
    r, base, treat = found["rule"], found["baseline"], found["treatment"]
    why = []
    if r["pending"]:
        why.append(f"{r['pending']} items' delivery unknown")
    if r["cost_unknown"]:
        why.append(f"{r['cost_unknown']} delivered pairs without a comparable price")
    if not why and not r["cost_pairs"]:
        why.append("no item delivered in both arms")
    cost = "-" if r["median_cost_difference"] is None else _number(r["median_cost_difference"], money=True)
    return (f"rule ({'accepted' if r['delivered_by'] == 'accept' else 'accepted and reviewed'} = delivered): "
            f"{VERDICT[r['met']]}" + (f" ({'; '.join(why)})" if why and r["met"] is None else "")
            + f". Delivered{' so far' if r['pending'] else ''} {_number(r['delivered'][treat])} {treat} vs "
              f"{_number(r['delivered'][base])} {base}; median paired cost over {r['cost_pairs']} items delivered in "
              f"both: {cost}")


def _report_level(found: dict) -> list[str]:
    lines = []
    if found["not_run"]:
        lines.append(f"registered but never ran: {', '.join(found['not_run'])}")
    if found["forgotten"]:
        lines.append(f"forgotten since the experiment began: {len(found['forgotten'])} threads ("
                     + ", ".join(f"{t['thread'][:8]}: {t['reason']}" for t in found["forgotten"]) + ")")
    elif found["forgotten"] is None:
        lines.append("forgotten threads: not visible in exported records")
    return lines


def table(found: dict) -> str:
    base, treat = found["baseline"], found["treatment"]
    lines = [f"experiment {found['experiment']}: {treat} against {base}, "
             f"{found['items']['compared']} items compared, {found['items']['excluded']} excluded, "
             f"{found['threads']} threads"]
    head = ["metric", "pairs", f"{base} mean", f"{treat} mean", "difference", "change", "w/l/t", "p", "95% interval"]
    rows = [head]
    for name in COMPARED:
        m = found["metrics"][name]
        money = name == "cost"
        interval = ("[" + ", ".join(_number(v, money) for v in m["interval"]) + "]") if m["interval"] else "-"
        extra = [f"+{m['unknown']} unknown"] if m["unknown"] else []
        extra += [f"+{m['unfinished']} unfinished"] if m["unfinished"] else []
        rows.append([name, f"{m['pairs']} ({', '.join(extra)})" if extra else str(m["pairs"]),
                     _number(m["baseline_mean"], money), _number(m["treatment_mean"], money),
                     _number(m["mean_difference"], money), _change(m["change"]),
                     "-" if m["wins"] is None else f"{m['wins']}/{m['losses']}/{m['ties']}",
                     "-" if m["p_value"] is None else f"{m['p_value']:.3g}", interval])
    reviews = any(r["values"][arm]["accepted"] == 1.0 and r["values"][arm]["reviewed"] is not None
                  for r in found["rows"] for arm in (base, treat))
    for name in ("accepted", "reviewed"):
        a = found["metrics"][name]
        if name == "reviewed" and not reviews:
            continue  # no accepted change has a review recorded: nothing to show
        pairs = f"{a['pairs']} (+{a['unknown']} unknown)" if a["unknown"] else str(a["pairs"])
        rows.append([name, pairs, _number(a["baseline_rate"]), _number(a["treatment_rate"]), "-", "-",
                     f"{a['treatment_only']}/{a['baseline_only']}/-",
                     "-" if a["p_value"] is None else f"{a['p_value']:.3g}", "-"])
    widths = [max(len(row[n]) for row in rows) for n in range(len(head))]
    lines += ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]
    lines.append(_rule_line(found))
    for tag, group in found["by_tag"].items():
        lines.append(f"by tag {tag} ({group['items']} items): "
                     + ", ".join(f"{name} {_shift(group['metrics'][name], name)}" for name in TAG_COLUMNS)
                     + f"; rule {VERDICT[group['rule']['met']]}")
    for arm in (base, treat):
        p = found["per_arm"][arm]
        lines.append(f"{arm:>5}: {p['threads']} threads, {p['accepted']} accepted, {p['opened']} pull requests opened, "
                     f"{p['delivered']} merged, agent time to merge known for {p['agent_time_known']}"
                     + ("" if p["lookups_blocked"] is None else f", {p['lookups_blocked']} lookups blocked by the guard"))
    for flag, names in found["checks"].items():
        effect = "excluded" if flag in EXCLUDE else "kept"
        lines.append(f"check {flag} ({effect}): {', '.join(names)}: {CHECKS[flag]}")
    lines += _report_level(found)
    lines.append("w/l/t: items where the treatment was better, worse, equal. Efficiency compares items accepted in "
                 "both arms. p: exact sign test. Unknown is never zero.")
    return "\n".join(lines)


def markdown(found: dict) -> str:
    """The comparison as the case study prints it: one row per metric, with the pairs it rests on, and every check."""
    base, treat = found["baseline"], found["treatment"]
    out = [f"| Metric | Unit | {base} (mean) | {treat} (mean) | Change | Items (w/l/t) | p |",
           "| --- | --- | --- | --- | --- | --- | --- |"]
    for name in COMPARED:
        m, money = found["metrics"][name], name == "cost"
        wlt = "" if m["wins"] is None else f" ({m['wins']}/{m['losses']}/{m['ties']})"
        out.append(f"| {LABELS[name]} | {m['unit']} | {_number(m['baseline_mean'], money)} | "
                   f"{_number(m['treatment_mean'], money)} | {_change(m['change'])} | {m['pairs']}{wlt} | "
                   f"{'-' if m['p_value'] is None else format(m['p_value'], '.3g')} |")
    for name, label in (("accepted", "Accepted"), ("reviewed", "Accepted and passed review")):
        a = found["metrics"][name]
        pairs = f"{a['pairs']} (+{a['unknown']} unknown)" if a["unknown"] else str(a["pairs"])
        p_value = "-" if a["p_value"] is None else format(a["p_value"], ".3g")
        out.append(f"| {label} | {a['unit']} | {_number(a['baseline_rate'])} | {_number(a['treatment_rate'])} | | "
                   f"{pairs} | {p_value} |")
    if found["by_tag"]:
        out += ["", f"| Tag | Items | {' | '.join(LABELS[name] for name in TAG_COLUMNS)} | Rule |",
                "| --- | --- |" + " --- |" * len(TAG_COLUMNS) + " --- |"]
        for tag, group in found["by_tag"].items():
            out.append(f"| {tag} | {group['items']} | "
                       + " | ".join(_shift(group["metrics"][name], name) for name in TAG_COLUMNS)
                       + f" | {VERDICT[group['rule']['met']]} |")
    out += ["", _rule_line(found)[0].upper() + _rule_line(found)[1:] + ".", ""]
    checks = [f"{flag} ({'excluded' if flag in EXCLUDE else 'kept'}): {', '.join(items)}"
              for flag, items in found["checks"].items()]
    out.append(f"{found['items']['compared']} items compared, {found['items']['excluded']} excluded. "
               f"Checks: {'; '.join(checks) if checks else 'none'}. "
               + " ".join(line[0].upper() + line[1:] + "." for line in _report_level(found))
               + f" Method: {found['method']}.")
    return "\n".join(out)


def to_csv(found: dict) -> str:
    base, treat = found["baseline"], found["treatment"]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    names = COMPARED + ("accepted", "reviewed")
    writer.writerow(["item", "tags", "flags", "excluded", f"{base}_threads", f"{treat}_threads",
                     *[f"{base}_{n}" for n in names], *[f"{treat}_{n}" for n in names]])
    for row in found["rows"]:
        writer.writerow([row["item"], " ".join(row["tags"]), " ".join(row["flags"]), bool(row["excluded"]),
                         " ".join(row["threads"][base]), " ".join(row["threads"][treat]),
                         *[metrics.UNKNOWN if row["values"][base][n] is None else row["values"][base][n] for n in names],
                         *[metrics.UNKNOWN if row["values"][treat][n] is None else row["values"][treat][n] for n in names]])
    return out.getvalue()
