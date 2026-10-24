"""A/B comparison of work items: two arms of one experiment, paired by item, every number from the journal.

    syberlabs experiment label --name githits-pilot --item ITEM-14 --arm on --tag needs_lookup
    syberlabs experiment report --name githits-pilot [--json | --csv | --markdown] [--from-export FILE ...]

An experiment runs the same work items through the factory more than once and changes one thing between the runs, its
arms (the GitHits pilot: the proposer's MCP configuration, ``docs/CLAUDE_CODE.md``). A thread joins an experiment through
an ``experiment`` fact, a verified host observation written by ``syberlabs experiment label``
(``Thread.record_experiment``): ``{name, item, arm, tags, by, at}``, versioned by the experiment's name.

``report(events, name)`` folds a journal into one row per item: each arm's threads, their numbers from
``syberlabs.metrics.work_items``, and what the record says about the run. It then compares the treatment arm with the
baseline over the items that ran in both. Like ``metrics``, it reads events and nothing else: it imports nothing from
``build``, ``session`` or ``syberwork``, opens no file, and appends nothing.

What it adds to the per-item metrics:

- ``time_to_accept``: hours from ``case_created`` to the contract's promotion succeeding (``effect_succeeded``, or a
  verified reconciliation of it). Both arms of an item can be accepted, while only one arm's pull request can merge, so
  the comparison uses this and reports ``agent_time`` (to merge) per arm, unpaired.
- ``accepted``: whether that effect is in the record.
- the checks that make a pair comparable, or say why it is not (``CHECKS``).

Statistics, stated plainly. The item is the unit. Each metric is compared over the valid items whose value is known in
both arms; an unknown is counted, never imputed, and an arm with several threads for one item (repeats) has the mean of
its threads, known only when every thread's value is. Per metric: the arms' means and medians, the mean and median of
the paired differences (treatment minus baseline), the change in the total, wins, losses and ties, an exact two-sided
sign test, and a 95% percentile bootstrap interval for the mean difference (2,000 resamples of items, seed 0). With fewer
than six pairs no sign test can reach p < 0.05. Nothing here corrects for testing several metrics at once.
"""

from __future__ import annotations

import csv
import io
import math
import random
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
RESAMPLES = 2000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}$")

# Compared over pairs. Lower is better for all but ``githits_lookups``, which shows use and has no better direction.
COMPARED = ("cost", "turns", "time_to_accept", "fix_rounds", "web_lookups", "githits_lookups", "review_rejections",
            "human_interventions")
NEUTRAL = ("githits_lookups",)
UNITS = {"cost": "$ per item", "turns": "turns per item", "time_to_accept": "hours per item",
         "fix_rounds": "retries per item", "web_lookups": "searches per item", "githits_lookups": "calls per item",
         "review_rejections": "rejections per item", "human_interventions": "interventions per item"}

# A check in ``EXCLUDE`` takes the item out of every comparison; the others are listed and the item stays in.
EXCLUDE = ("missing_arm", "base_mismatch", "objective_mismatch", "arm_conflict", "contaminated")
CHECKS = {
    "missing_arm": "the item has threads in one arm only",
    "base_mismatch": "the arms started from different commits",
    "objective_mismatch": "the arms were given different objectives",
    "arm_conflict": "the record contradicts the label: the server was connected in the baseline, or not in the treatment",
    "contaminated": "the baseline made lookups on the treatment's server",
    "base_unknown": "a thread has no recorded base commit, so the start could not be compared",
    "arm_unverified": "no call recorded its MCP servers, so only the label says which arm ran",
    "server_mixed": "the server was connected in some of a thread's calls and not in others",
    "unused": "the treatment made no lookups on its server",
    "labelled_late": "the label was written after the thread's first candidate, not before it",
    "relabelled": "the thread carries different labels for this experiment; the latest is used",
    "model_mismatch": "the arms were served by different models",
    "cost_basis_mismatch": "the arms' costs were recorded on different bases or lanes; cost is not compared for this item",
    "tag_mismatch": "the arms' threads carry different tags; their union is used",
    "repeats": "an arm has more than one thread for this item; their mean is used",
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


def _label(value) -> bool:
    return (isinstance(value, dict) and all(isinstance(value.get(k), str) and _NAME.match(value[k])
                                            for k in ("name", "item", "arm"))
            and isinstance(value.get("tags", []), list) and all(isinstance(t, str) for t in value.get("tags", [])))


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
    time_to_accept: metrics.Metric
    work: metrics.WorkItemMetrics

    def value(self, name: str) -> metrics.Metric:
        if name == "time_to_accept":
            return self.time_to_accept
        if name == "accepted":
            return self.accepted
        return self.work.metric(name)


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


def _run(case_id: str, events: list[dict], name: str, work: metrics.WorkItemMetrics, contracts,
         server: str | None) -> Run | None:
    labels = [(e, v) for e, v in ((e, metrics.fact(e, KEY)) for e in events) if _label(v) and v["name"] == name]
    if not labels:
        return None
    event, value = labels[-1]
    created = next((e for e in events if e["kind"] == "case_created"), None)
    body = created["body"] if created else {}
    inputs = body.get("inputs") if isinstance(body.get("inputs"), dict) else {}
    registrations = [e for e in events if e["kind"] == "candidate_registered" and e["body"].get("operator") != "migration"]
    first = registrations[0]["seq"] if registrations else None
    bases = [metrics.fact(e, "base") for e in events]
    base = next((b.get("commit") for b in reversed(bases) if b and isinstance(b.get("commit"), str)), None)

    action = _promotion(contracts, body.get("contract"))
    proposals = {e["body"].get("id"): e["body"] for e in events if e["kind"] == "proposed"}
    accept = next((e for e in events
                   if (e["kind"] == "effect_succeeded" and e["body"].get("action") == action)
                   or (verified_reconciliation(e) and (proposals.get(e["body"].get("proposal_id")) or {}).get("action")
                       == action)), None)
    accepted = metrics.Metric(accept is not None, (accept["hash"],) if accept else (),
                              f"{action} succeeded" if accept else f"no {action} effect in the record")
    if accept is not None and created is not None:
        hours = round((as_seconds(accept["at"]) - as_seconds(created["at"])) / 3600, 3)
        time_to_accept = metrics.Metric(hours, (created["hash"], accept["hash"]), "case_created to the promotion succeeding")
    else:
        time_to_accept = metrics.Metric(None, (created["hash"],) if created else (),
                                        "not accepted" if accept is None else "no case_created")
    distinct = {(v["item"], v["arm"], tuple(sorted(v.get("tags", [])))) for _e, v in labels}
    return Run(thread=case_id, item=value["item"], arm=value["arm"], tags=tuple(sorted(set(value.get("tags", [])))),
               label=event["hash"], labelled_late=first is not None and event["seq"] > first,
               relabelled=len(distinct) > 1, base=base,
               objective=inputs.get("objective") if isinstance(inputs.get("objective"), str) else None,
               server=_servers(registrations, server), models=_models(registrations), accepted=accepted,
               time_to_accept=time_to_accept, work=work)


def runs(events: Iterable[dict], name: str, contracts: Mapping | None = None, server: str | None = SERVER) -> list[Run]:
    """Every thread labelled into experiment ``name``, in order of first appearance. Pure."""
    events = list(events)
    threads: dict[str, list[dict]] = {}
    for event in events:
        threads.setdefault(event["case_id"], []).append(event)
    work = {item.thread: item for item in metrics.work_items(events, contracts)}
    found = []
    for case_id, found_events in threads.items():
        run = _run(case_id, sorted(found_events, key=lambda e: e["seq"]), name, work[case_id], contracts, server)
        if run is not None:
            found.append(run)
    return found


def _mean(runs_: Sequence[Run], name: str) -> float | None:
    """An arm's value for one item: the mean over its threads, known only when every thread's value is."""
    values = [run.value(name) for run in runs_]
    if not values or any(not v.known for v in values):
        return None
    return statistics.fmean(float(v.value) for v in values)


def _flags(item_runs: dict[str, list[Run]], baseline: str, treatment: str, server: str | None) -> list[str]:
    everyone = [run for arm in (baseline, treatment) for run in item_runs.get(arm, [])]
    flags = []
    if not item_runs.get(baseline) or not item_runs.get(treatment):
        flags.append("missing_arm")
    bases = {run.base for run in everyone}
    if None in bases:
        flags.append("base_unknown")
    if len(bases - {None}) > 1:
        flags.append("base_mismatch")
    if len({run.objective for run in everyone}) > 1:
        flags.append("objective_mismatch")
    if server is not None:
        if (any(run.server in (True, "mixed") for run in item_runs.get(baseline, []))
                or any(run.server is False for run in item_runs.get(treatment, []))):
            flags.append("arm_conflict")
        if any(run.server is None for run in everyone):
            flags.append("arm_unverified")
        if any(run.server == "mixed" for run in item_runs.get(treatment, [])):
            flags.append("server_mixed")
        if server == SERVER:
            if any((run.work.githits_lookups.value or 0) > 0 for run in item_runs.get(baseline, [])):
                flags.append("contaminated")
            if item_runs.get(treatment) and all(run.work.githits_lookups.value == 0 for run in item_runs[treatment]):
                flags.append("unused")
    if any(run.labelled_late for run in everyone):
        flags.append("labelled_late")
    if any(run.relabelled for run in everyone):
        flags.append("relabelled")
    models = [set(run.models) for run in everyone if run.models]
    if models and any(m != models[0] for m in models):
        flags.append("model_mismatch")
    if len({(run.work.cost_basis, getattr(run.work, "lane", None)) for run in everyone}) > 1:
        flags.append("cost_basis_mismatch")
    if len({run.tags for run in everyone}) > 1:
        flags.append("tag_mismatch")
    if any(len(item_runs.get(arm, [])) > 1 for arm in (baseline, treatment)):
        flags.append("repeats")
    return flags


def sign_test(wins: int, losses: int) -> float | None:
    """Exact two-sided sign test over the untied pairs; None when every pair is tied."""
    n = wins + losses
    if n == 0:
        return None
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 6)


def bootstrap(differences: Sequence[float], resamples: int = RESAMPLES, seed: int = 0) -> list[float] | None:
    """A 95% percentile interval for the mean difference, resampling items with replacement; None below two pairs."""
    if len(differences) < 2:
        return None
    rng, n = random.Random(seed), len(differences)
    means = sorted(statistics.fmean(rng.choice(differences) for _ in range(n)) for _ in range(resamples))
    cut = int(resamples * 0.025)
    return [round(means[cut], 6), round(means[resamples - 1 - cut], 6)]


def compare(pairs: Sequence[tuple[float, float]], lower_is_better: bool | None = True) -> dict:
    """Baseline-treatment pairs of one metric: descriptive numbers, wins and losses, the sign test and the interval."""
    found = {"pairs": len(pairs), "baseline_mean": None, "treatment_mean": None, "baseline_median": None,
             "treatment_median": None, "mean_difference": None, "median_difference": None, "change": None,
             "wins": None, "losses": None, "ties": None, "p_value": None, "interval": None}
    if not pairs:
        return found
    base, treat = [b for b, _t in pairs], [t for _b, t in pairs]
    differences = [t - b for b, t in pairs]
    found.update({"baseline_mean": round(statistics.fmean(base), 6), "treatment_mean": round(statistics.fmean(treat), 6),
                  "baseline_median": round(statistics.median(base), 6),
                  "treatment_median": round(statistics.median(treat), 6),
                  "mean_difference": round(statistics.fmean(differences), 6),
                  "median_difference": round(statistics.median(differences), 6),
                  "change": round((sum(treat) - sum(base)) / sum(base), 6) if sum(base) > 0 else None,
                  "interval": bootstrap(differences)})
    if lower_is_better is not None:
        better = [d < 0 if lower_is_better else d > 0 for d in differences]
        wins = sum(better)
        ties = sum(1 for d in differences if d == 0)
        found.update({"wins": wins, "losses": len(differences) - wins - ties, "ties": ties,
                      "p_value": sign_test(wins, len(differences) - wins - ties)})
    return found


def _comparisons(rows: list[dict], baseline: str, treatment: str) -> dict:
    out = {}
    for name in COMPARED:
        pairs, unknown = [], 0
        for row in rows:
            if name == "cost" and "cost_basis_mismatch" in row["flags"]:
                unknown += 1
                continue
            b, t = row["values"][baseline].get(name), row["values"][treatment].get(name)
            if b is None or t is None:
                unknown += 1
            else:
                pairs.append((b, t))
        out[name] = {**compare(pairs, None if name in NEUTRAL else True), "unknown": unknown, "unit": UNITS[name]}
    accepted = [(row["values"][baseline]["accepted"], row["values"][treatment]["accepted"]) for row in rows]
    treatment_only = sum(1 for b, t in accepted if t > b)
    baseline_only = sum(1 for b, t in accepted if b > t)
    out["accepted"] = {"pairs": len(accepted), "unit": "share of items accepted",
                       "baseline_rate": round(statistics.fmean(b for b, _ in accepted), 6) if accepted else None,
                       "treatment_rate": round(statistics.fmean(t for _, t in accepted), 6) if accepted else None,
                       "treatment_only": treatment_only, "baseline_only": baseline_only,
                       "p_value": sign_test(treatment_only, baseline_only)}
    return out


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
           treatment: str = TREATMENT, server: str | None = SERVER) -> dict:
    """The experiment's items, their checks, and the treatment compared with the baseline. Pure.

    ``server`` names the MCP server whose recorded connection must match the arm (``githits`` for the GitHits pilot);
    None skips the arm, contamination and use checks for an experiment that does not turn a server on and off.
    """
    if baseline == treatment:
        raise ValueError("the baseline and the treatment must be different arms")
    # Threads in start order, then by id, so the rows come out the same whatever order the journal lists threads in.
    found = sorted(runs(events, name, contracts, server),
                   key=lambda r: (r.work.started_at is None, as_seconds(r.work.started_at or 0), r.thread))
    items: dict[str, dict[str, list[Run]]] = {}
    for run in found:
        items.setdefault(run.item, {}).setdefault(run.arm, []).append(run)
    rows = []
    for item, by_arm in items.items():
        flags = _flags(by_arm, baseline, treatment, server)
        everyone = [run for arm in (baseline, treatment) for run in by_arm.get(arm, [])]
        values = {arm: {metric: _mean(by_arm.get(arm, []), metric) for metric in COMPARED + ("accepted",)}
                  for arm in (baseline, treatment)}
        rows.append({"item": item, "tags": sorted({tag for run in everyone for tag in run.tags}), "flags": flags,
                     "excluded": [flag for flag in flags if flag in EXCLUDE],
                     "threads": {arm: [run.thread for run in by_arm.get(arm, [])] for arm in (baseline, treatment)},
                     "values": values, "other_arms": sorted(set(by_arm) - {baseline, treatment})})
    valid = [row for row in rows if not row["excluded"]]
    tags = sorted({tag for row in valid for tag in row["tags"]})
    groups = [(tag, [row for row in valid if tag in row["tags"]]) for tag in tags]
    if tags:
        groups.append(("(untagged)", [row for row in valid if not row["tags"]]))
    by_tag = {tag: {"items": len(chosen), "metrics": _comparisons(chosen, baseline, treatment)}
              for tag, chosen in groups if chosen}
    return {
        "experiment": name, "baseline": baseline, "treatment": treatment, "server": server,
        "items": {"labelled": len(rows), "compared": len(valid), "excluded": len(rows) - len(valid)},
        "threads": len(found),
        "metrics": _comparisons(valid, baseline, treatment),
        "by_tag": by_tag,
        "per_arm": {arm: _per_arm(run for run in found if run.arm == arm) for arm in (baseline, treatment)},
        "checks": {flag: [row["item"] for row in rows if flag in row["flags"]] for flag in CHECKS
                   if any(flag in row["flags"] for row in rows)},
        "rows": rows,
        "method": ("item is the unit; pairs where both arms' value is known; unknown counted, never imputed; repeats "
                   "averaged; exact two-sided sign test; 95% percentile bootstrap of the mean difference, "
                   f"{RESAMPLES} resamples, seed 0; no correction for several metrics"),
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
        rows.append([name, f"{m['pairs']} (+{m['unknown']} unknown)" if m["unknown"] else str(m["pairs"]),
                     _number(m["baseline_mean"], money), _number(m["treatment_mean"], money),
                     _number(m["mean_difference"], money), _change(m["change"]),
                     "-" if m["wins"] is None else f"{m['wins']}/{m['losses']}/{m['ties']}",
                     "-" if m["p_value"] is None else f"{m['p_value']:.3g}", interval])
    a = found["metrics"]["accepted"]
    rows.append(["accepted", str(a["pairs"]), _number(a["baseline_rate"]), _number(a["treatment_rate"]), "-", "-",
                 f"{a['treatment_only']}/{a['baseline_only']}/-", "-" if a["p_value"] is None else f"{a['p_value']:.3g}",
                 "-"])
    widths = [max(len(row[n]) for row in rows) for n in range(len(head))]
    lines += ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]
    for tag, group in found["by_tag"].items():
        lines.append(f"by tag {tag} ({group['items']} items): "
                     + ", ".join(f"{name} {_shift(group['metrics'][name], name)}" for name in TAG_COLUMNS))
    for arm in (base, treat):
        p = found["per_arm"][arm]
        lines.append(f"{arm:>5}: {p['threads']} threads, {p['accepted']} accepted, {p['opened']} pull requests opened, "
                     f"{p['delivered']} merged, agent time to merge known for {p['agent_time_known']}"
                     + ("" if p["lookups_blocked"] is None else f", {p['lookups_blocked']} lookups blocked by the guard"))
    for flag, names in found["checks"].items():
        effect = "excluded" if flag in EXCLUDE else "kept"
        lines.append(f"check {flag} ({effect}): {', '.join(names)}: {CHECKS[flag]}")
    lines.append("w/l/t: items where the treatment was better, worse, equal. p: exact sign test. "
                 "Unknown is never zero.")
    return "\n".join(lines)


LABELS = {"cost": "Cost", "turns": "Turns", "time_to_accept": "Time to acceptance", "fix_rounds": "Fix rounds",
          "web_lookups": "Web lookups", "githits_lookups": "GitHits lookups", "review_rejections": "Review rejections",
          "human_interventions": "Human interventions"}
TAG_COLUMNS = ("cost", "turns", "fix_rounds", "web_lookups", "githits_lookups")


def _shift(m: dict, name: str) -> str:
    """One metric for one tag: baseline mean to treatment mean, with the change in the total."""
    if not m["pairs"]:
        return metrics.UNKNOWN
    money = name == "cost"
    change = "" if m["change"] is None or name in NEUTRAL else f" ({_change(m['change'])})"
    return f"{_number(m['baseline_mean'], money)} -> {_number(m['treatment_mean'], money)}{change}"


def markdown(found: dict) -> str:
    """The comparison as the case study prints it: one row per metric, with the pairs it rests on."""
    base, treat = found["baseline"], found["treatment"]
    out = [f"| Metric | Unit | {base} (mean) | {treat} (mean) | Change | Items (w/l/t) | p |",
           "| --- | --- | --- | --- | --- | --- | --- |"]
    for name in COMPARED:
        m, money = found["metrics"][name], name == "cost"
        wlt = "" if m["wins"] is None else f" ({m['wins']}/{m['losses']}/{m['ties']})"
        out.append(f"| {LABELS[name]} | {m['unit']} | {_number(m['baseline_mean'], money)} | "
                   f"{_number(m['treatment_mean'], money)} | {_change(m['change'])} | {m['pairs']}{wlt} | "
                   f"{'-' if m['p_value'] is None else format(m['p_value'], '.3g')} |")
    a = found["metrics"]["accepted"]
    out.append(f"| Accepted | {a['unit']} | {_number(a['baseline_rate'])} | {_number(a['treatment_rate'])} | | "
               f"{a['pairs']} | {'-' if a['p_value'] is None else format(a['p_value'], '.3g')} |")
    if found["by_tag"]:
        out += ["", f"| Tag | Items | {' | '.join(LABELS[name] for name in TAG_COLUMNS)} |",
                "| --- | --- |" + " --- |" * len(TAG_COLUMNS)]
        for tag, group in found["by_tag"].items():
            out.append(f"| {tag} | {group['items']} | "
                       + " | ".join(_shift(group["metrics"][name], name) for name in TAG_COLUMNS) + " |")
    out.append("")
    out.append(f"{found['items']['compared']} items compared, {found['items']['excluded']} excluded "
               f"({', '.join(f'{k}: {len(v)}' for k, v in found['checks'].items() if k in EXCLUDE) or 'none'}). "
               f"Method: {found['method']}.")
    return "\n".join(out)


def to_csv(found: dict) -> str:
    base, treat = found["baseline"], found["treatment"]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    names = COMPARED + ("accepted",)
    writer.writerow(["item", "tags", "flags", "excluded", f"{base}_threads", f"{treat}_threads",
                     *[f"{base}_{n}" for n in names], *[f"{treat}_{n}" for n in names]])
    for row in found["rows"]:
        writer.writerow([row["item"], " ".join(row["tags"]), " ".join(row["flags"]), bool(row["excluded"]),
                         " ".join(row["threads"][base]), " ".join(row["threads"][treat]),
                         *[metrics.UNKNOWN if row["values"][base][n] is None else row["values"][base][n] for n in names],
                         *[metrics.UNKNOWN if row["values"][treat][n] is None else row["values"][treat][n] for n in names]])
    return out.getvalue()
