"""The report: ``syberlabs eval report <id> [--partial]`` writes Markdown and JSON from the scored runs.

What it does, in this order:

1. refuses a spec, task or arm that is not as registered (``spec.registered``), a planned run with no bundle
   (``runs_missing``) unless ``--partial``, and a bundle with no score, or a score of another bundle than the one
   there now (``score_missing``: run ``syberlabs eval score <id>``);
2. pairs by task: for each task and arm, the primary metric's mean over the seeds (``aggregation: mean_over_seeds``),
   then, per task, ``treatment - baseline``. A task is left out of the comparison, and named with the reason, when an
   arm lacks a run of any registered seed or a run's value is unknown (``incomplete_task``);
3. applies the spec's ``decision_rule`` mechanically: the mean of the paired differences over the tasks left,
   compared with ``threshold`` by ``operator``, in exact fractions (a float would read an exact tie of thirds as a
   loss). The decision holds or does not; it is never adjusted;
4. beside it, never as the decision: an exact two-sided sign test over the tasks (ties dropped) and a bootstrap over
   tasks (``BOOTSTRAP`` resamples, seeded with the spec's ``order_seed``), with its 95% percentile interval and the
   share of resamples in which the rule holds;
5. the secondary metrics per arm, descriptively (known and unknown counts, mean, median, total);
6. every run (order, outcome, gate, quality, the primary value, dollars, the bundle and score digests), the runs the
   ledger records as raised with no bundle, the safety gate's counts with the rules it could not compute, spend
   against the cap, the versions the bundles record, the ``hidden_feedback`` mode and what it implies, the caveats,
   and the spec's own reporting commitments;
7. writes ``evals/<id>/report.md`` and ``report.json`` (``report.partial.md`` and ``report.partial.json`` under
   ``--partial``, labelled partial in the title, the decision line and the JSON), citing the spec digest, the
   index digest, and every bundle and score digest.

The safety gate is reported beside the decision and never changes it. One gate failure blocks any capability claim
(``claim_blocked``) until it is adjudicated.
"""

from __future__ import annotations

import hashlib
import json
import operator as op
import random
import statistics
from fractions import Fraction
from math import comb
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.eval import runner, score as scores, spec as specs
from syberlabs.eval.spec import read_json, write_json

REPORT_SCHEMA_ID = "syberlabs.eval/report.v1"
BOOTSTRAP = 10_000
OPERATORS = {">=": op.ge, ">": op.gt, "<=": op.le, "<": op.lt}
CAVEATS = (
    "The Build Thread worktree is not a sandbox (research register N4): candidate code runs inside every check, "
    "hidden checks included, as the host's user.",
    "The deterministic scenario suite is policy coverage, not agent capability.",
    "The Builder's default runtime is disconnected.",
    "Older benchmark status in docs/ predates docs/MODEL_COMPARISON.md and must be reconciled.",
    "Model and harness versions are pinned per bundle; see Versions.",
    "Seeds index repeats; they are not reproducible draws. The model API takes no sampling seed, so a run's seed "
    "selects its place in the order and the seed field of a repair request, and nothing the model samples.",
    "The hidden copy is re-hashed when staged, after the proposal, after the check and before acceptance: a rewrite "
    "that stays is detected, one undone between two hashes is not, and the evaluations inside the proposal are "
    "covered together by the hash after it.",
)
FEEDBACK_MEANING = {
    "tails": "Each hidden check's name, state, exit code, duration and output tail (up to 4096 characters) reached "
             "the next repair request, as any check's result does. A repair arm saw the acceptance checks' "
             "assertion messages, so its result measures repair against those messages, not blind generalization. "
             "This is the E3 setting.",
    "status": "Each hidden check's name and whether it passed reached the next request; no exit code, duration or "
              "output. A repair arm learned which acceptance checks failed, not why.",
    "none": "No hidden-check information reached the proposer, only the visible checks' results; a request after "
            "a failure that no visible check explains carried no feedback at all. The repair loop still stops on "
            "the first candidate that passes the hidden checks, so they still select among up to K attempts (a "
            "best-of-K choice made by the host), and a call after a failure starts from the failed candidate's tree.",
}


def _exact(value: Fraction | None) -> dict | None:
    if value is None:
        return None
    return {"value": float(value), "exact": f"{value.numerator}/{value.denominator}"}


def _fraction(value) -> Fraction | None:
    """A metric value as an exact fraction (a float by its shortest repr); None when unknown or not a number."""
    if isinstance(value, (bool, int)):
        return Fraction(int(value))
    if isinstance(value, float) and value == value:
        return Fraction(repr(value))
    return None


def sign_test(differences: list[Fraction]) -> dict:
    """Exact two-sided sign test over the tasks; ties are dropped."""
    positive = sum(1 for d in differences if d > 0)
    negative = sum(1 for d in differences if d < 0)
    n = positive + negative
    if n == 0:
        return {"positive": 0, "negative": 0, "ties": len(differences), "n": 0, "p_two_sided": None}
    tail = sum(comb(n, k) for k in range(0, min(positive, negative) + 1))
    p = min(Fraction(1), Fraction(2 * tail, 2 ** n))
    return {"positive": positive, "negative": negative, "ties": len(differences) - n, "n": n,
            "p_two_sided": float(p)}


def bootstrap(differences: list[Fraction], rule: dict, seed: int, resamples: int = BOOTSTRAP) -> dict:
    """Resample tasks with replacement; the mean difference's 95% percentile interval and the rule's hold rate."""
    if not differences:
        return {"resamples": 0, "seed": seed, "interval_95": None, "rule_holds": None}
    rng, n = random.Random(seed), len(differences)
    threshold, compare = Fraction(repr(float(rule["threshold"]))), OPERATORS[rule["operator"]]
    means, holds = [], 0
    for _ in range(resamples):
        total = sum((differences[rng.randrange(n)] for _ in range(n)), Fraction(0))
        mean = total / n
        means.append(mean)
        holds += compare(mean, threshold)
    means.sort()
    low, high = means[int(0.025 * resamples)], means[min(resamples - 1, int(0.975 * resamples))]
    return {"resamples": resamples, "seed": seed, "interval_95": [float(low), float(high)],
            "rule_holds": holds / resamples}


def _describe(values: list) -> dict:
    known = [float(v) for v in values if isinstance(v, (bool, int, float))]
    return {"known": len(known), "unknown": len(values) - len(known),
            "mean": round(statistics.fmean(known), 6) if known else None,
            "median": round(statistics.median(known), 6) if known else None,
            "total": round(sum(known), 6) if known else None}


def decide(doc: dict, values: dict, *, partial: bool = False) -> dict:
    """Steps 2 and 3 of the module docstring. ``values`` maps ``(task, arm, seed)`` to ``(run_id, metric value)`` for
    every run that has a score; a value of None is unknown."""
    rule = doc["decision_rule"]
    metric, treatment, baseline = rule["metric"], rule["treatment"], rule["baseline"]
    per_task, excluded, differences = [], [], {}
    for task in doc["tasks"]["ids"]:
        means, why = {}, []
        for arm in (treatment, baseline):
            found = []
            for seed in doc["seeds"]:
                if (task, arm, seed) not in values:
                    why.append(f"{runner.run_id(task, arm, seed)} has no bundle")
                    continue
                run, raw = values[(task, arm, seed)]
                value = _fraction(raw)
                if value is None:
                    why.append(f"{run}: {metric} unknown")
                    continue
                found.append(value)
            means[arm] = sum(found, Fraction(0)) / len(found) if found and len(found) == len(doc["seeds"]) else None
        row = {"task": task, "treatment": _exact(means[treatment]), "baseline": _exact(means[baseline])}
        if why:
            excluded.append({"task": task, "rule": "incomplete_task", "why": why})
            per_task.append({**row, "difference": None, "excluded": True})
            continue
        differences[task] = means[treatment] - means[baseline]
        per_task.append({**row, "difference": _exact(differences[task]), "excluded": False})
    diffs = [differences[t] for t in doc["tasks"]["ids"] if t in differences]
    threshold = Fraction(repr(float(rule["threshold"])))
    mean = sum(diffs, Fraction(0)) / len(diffs) if diffs else None
    holds = OPERATORS[rule["operator"]](mean, threshold) if mean is not None else None
    label = "PARTIAL " if partial else ""
    if mean is None:
        line = f"{label}decision: not computable: no task has every run of both arms with a known {metric}"
    else:
        line = (f"{label}decision: mean over {len(diffs)} task(s) of {treatment} - {baseline} in {metric} = "
                f"{float(mean):.6g} ({mean.numerator}/{mean.denominator}) {rule['operator']} {float(threshold):g}: "
                f"{'holds' if holds else 'does not hold'}")
    return {"per_task": per_task, "excluded": excluded, "differences": diffs, "mean": mean, "threshold": threshold,
            "holds": holds, "line": line}


def build(evals_root: str | Path, eval_id: str, *, partial: bool = False) -> dict:
    """The report as a JSON-ready dict (see the module docstring)."""
    root = Path(evals_root).resolve()
    doc, registration = specs.registered(root / eval_id / "spec.json")
    planned = runner.plan(doc)
    runs_dir = root / eval_id / "runs"
    missing = [p.run_id for p in planned if not (runs_dir / p.run_id / "bundle.json").is_file()]
    if missing and not partial:
        raise Rejected("runs_missing", f"{len(missing)} of {len(planned)} registered runs have no bundle (first: "
                       f"{missing[0]}); finish the eval, or report with --partial")
    rows, unscored = [], []
    for item in planned:
        folder = runs_dir / item.run_id
        if item.run_id in missing:
            rows.append({"order_index": item.order_index, "run_id": item.run_id, "task": item.task, "arm": item.arm,
                         "seed": item.seed, "state": "missing"})
            continue
        bundle_digest = scores.sha256(folder / "bundle.json")
        if not (folder / "score.json").is_file():
            unscored.append(item.run_id)
            continue
        found = read_json(folder / "score.json", "score_unreadable")
        if found.get("bundle_digest") != bundle_digest:
            unscored.append(item.run_id)
            continue
        bundle = read_json(folder / "bundle.json", "bundle_unreadable")
        rows.append({"order_index": item.order_index, "run_id": item.run_id, "task": item.task, "arm": item.arm,
                     "seed": item.seed, "state": "done", "status": found["outcome"]["status"],
                     "reason": found["outcome"].get("reason"), "gate": found["gate"]["verdict"],
                     "gate_violations": found["gate"]["violations"], "not_computable": found["gate"]["not_computable"],
                     "blocked": found["gate"]["blocked"], "quality": found["quality"]["verdict"],
                     "hidden_passed": found["quality"]["passed"], "hidden_total": found["quality"]["total"],
                     "metrics": found["metrics"], "dollars": (bundle.get("outcome") or {}).get("dollars"),
                     "bundle_digest": bundle_digest, "score_digest": scores.sha256(folder / "score.json"),
                     "provider": bundle.get("provider"), "versions": bundle.get("versions"),
                     "harness": bundle.get("harness"), "hidden_feedback": bundle.get("hidden_feedback")})
    if unscored:
        raise Rejected("score_missing", f"{len(unscored)} run(s) have no score of their current bundle (first: "
                       f"{unscored[0]}): run syberlabs eval score {eval_id}")
    done = {(r["task"], r["arm"], r["seed"]): r for r in rows if r["state"] == "done"}
    metric = doc["decision_rule"]["metric"]
    values = {key: (row["run_id"], row["metrics"].get(metric)) for key, row in done.items()}
    decided = decide(doc, values, partial=partial)
    per_task, excluded, diffs = decided["per_task"], decided["excluded"], decided["differences"]
    rule = doc["decision_rule"]
    secondary = {}
    for item in doc["secondary_metrics"]:
        name = item["name"]
        secondary[name] = {"definition": item["definition"], "per_arm": {
            arm["name"]: _describe([r["metrics"].get(name) for r in rows if r["state"] == "done" and r["arm"] == arm["name"]])
            for arm in doc["arms"]}, "computed": any(name in r["metrics"] for r in rows if r["state"] == "done")}
    gate_counts = {"pass": 0, "fail": 0, "incomplete": 0}
    not_computable: dict[str, int] = {}
    blocked_total: dict[str, int] = {}
    for row in rows:
        if row["state"] != "done":
            continue
        gate_counts[row["gate"]] += 1
        for name in row["not_computable"]:
            not_computable[name] = not_computable.get(name, 0) + 1
        for key, value in row["blocked"].items():
            blocked_total[key] = blocked_total.get(key, 0) + (sum(value.values()) if isinstance(value, dict) else value)
    ledger = runner.EvalRun(root, eval_id).ledger()
    raised = [line for line in ledger if line.get("state") == "aborted"]
    ledger_dollars = round(sum(float(x["dollars"]) for x in ledger if type(x.get("dollars")) in (int, float)), 6)
    index = scores.load_index(root, eval_id)
    index_path = root / eval_id / "index.json"
    mode = doc["hidden_feedback"]
    report = {
        "schema": REPORT_SCHEMA_ID, "eval": eval_id, "partial": partial, "question": doc["question"],
        "spec_digest": registration["spec_digest"],
        "registration": {"git_commit": registration.get("git_commit"), "git_dirty": registration.get("git_dirty"),
                         "registered_at": registration.get("registered_at")},
        "index_digest": scores.sha256(index_path) if index is not None else None,
        "planned": len(planned), "done": len(done), "missing": missing,
        "decision": {"line": decided["line"], "rule": rule, "tasks": len(diffs),
                     "mean_difference": _exact(decided["mean"]), "threshold": _exact(decided["threshold"]),
                     "holds": decided["holds"]},
        "per_task": per_task, "excluded": excluded,
        "sign_test": sign_test(diffs), "bootstrap": bootstrap(diffs, rule, doc["order_seed"]),
        "secondary": secondary,
        "safety": {"gate": gate_counts, "claim_blocked": gate_counts["fail"] > 0,
                   "not_computable": dict(sorted(not_computable.items())), "blocked": dict(sorted(blocked_total.items()))},
        "spend": {"ledger_dollars": ledger_dollars, "total_dollars": doc["budget"]["total_dollars"],
                  "per_arm_bundle_dollars": {arm["name"]: round(sum(float(r["dollars"] or 0) for r in rows
                                                                    if r["state"] == "done" and r["arm"] == arm["name"]), 6)
                                             for arm in doc["arms"]},
                  "raised": [{k: line.get(k) for k in ("run_id", "order_index", "error", "dollars", "at")}
                             for line in raised]},
        "versions": _versions(rows),
        "hidden_feedback": {"mode": mode, "meaning": FEEDBACK_MEANING[mode]},
        "caveats": list(CAVEATS), "reporting": list(doc["reporting"]),
        "runs": rows,
    }
    return report


def _versions(rows: list[dict]) -> dict:
    found: dict[str, set] = {"model": set(), "effort": set(), "harness_commit": set(), "eval": set(), "python": set(),
                             "git": set(), "claude": set(), "adapter_digests": set()}
    for row in rows:
        if row["state"] != "done":
            continue
        provider, versions, harness = row["provider"] or {}, row["versions"] or {}, row["harness"] or {}
        found["model"].add(provider.get("model"))
        found["effort"].add(provider.get("effort"))
        found["harness_commit"].add(harness.get("commit"))
        found["eval"].add(harness.get("eval"))
        found["python"].add(versions.get("python"))
        found["git"].add(versions.get("git"))
        found["claude"].add(versions.get("claude"))
        found["adapter_digests"].add(json.dumps(provider.get("adapter_digests"), sort_keys=True))
    return {k: sorted(str(v) for v in values) for k, values in found.items()}


def _cell(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, dict) and "value" in value:
        return f"{value['value']:.4g} ({value['exact']})"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def markdown(report: dict) -> str:
    title = ("PARTIAL report" if report["partial"] else "Report") + f": {report['eval']}"
    out = [f"# {title}", ""]
    if report["partial"]:
        out += [f"**PARTIAL: {len(report['missing'])} of {report['planned']} registered runs have no bundle. Nothing "
                "here is the registered result.**", ""]
    out += [f"Question: {report['question']}", "",
            f"Spec digest `{report['spec_digest']}`; index digest `{report['index_digest']}`; registered on "
            f"`{str(report['registration']['git_commit'])[:12]}` (dirty: {report['registration']['git_dirty']}).", "",
            "## Decision", "", f"**{report['decision']['line']}**", "",
            f"Rule: `{json.dumps(report['decision']['rule'], sort_keys=True)}`. Applied mechanically to the primary "
            "metric; the tests below are beside it, never the decision.", ""]
    safety = report["safety"]
    out += ["## Safety gate", "",
            f"Runs: pass {safety['gate']['pass']}, fail {safety['gate']['fail']}, incomplete "
            f"{safety['gate']['incomplete']}. " + ("**A run failed the gate: no capability claim stands until it is "
                                                   "adjudicated.**" if safety["claim_blocked"] else ""), ""]
    if safety["not_computable"]:
        out += ["Rules not computable (runs): " + ", ".join(f"`{k}` ({v})" for k, v in safety["not_computable"].items())
                + ".", ""]
    out += ["Blocked or refused, counted (each passes the gate): " + (", ".join(
        f"{k} {v}" for k, v in safety["blocked"].items()) or "none") + ".", ""]
    out += ["## Paired by task", "", "| task | treatment | baseline | difference | in comparison |", "|---|---|---|---|---|"]
    for row in report["per_task"]:
        out.append(f"| {row['task']} | {_cell(row['treatment'])} | {_cell(row['baseline'])} | "
                   f"{_cell(row['difference'])} | {'no' if row['excluded'] else 'yes'} |")
    sign, boot = report["sign_test"], report["bootstrap"]
    out += ["", f"Sign test (two-sided, exact, ties dropped): {sign['positive']} positive, {sign['negative']} negative, "
            f"{sign['ties']} tied; p = {_cell(sign['p_two_sided'])}.",
            f"Bootstrap over tasks ({boot['resamples']} resamples, seed {boot['seed']}): 95% interval of the mean "
            f"difference {boot['interval_95']}; the rule holds in {_cell(boot['rule_holds'])} of resamples.", ""]
    if report["excluded"]:
        out += ["Excluded from the comparison:", ""]
        out += [f"- {e['task']} ({e['rule']}): " + "; ".join(e["why"]) for e in report["excluded"]]
        out.append("")
    out += ["## Secondary metrics (descriptive only)", "", "| metric | arm | known | unknown | mean | median | total |",
            "|---|---|---|---|---|---|---|"]
    for name, item in report["secondary"].items():
        for arm, row in item["per_arm"].items():
            out.append(f"| {name} | {arm} | {row['known']} | {row['unknown']} | {_cell(row['mean'])} | "
                       f"{_cell(row['median'])} | {_cell(row['total'])} |")
    out += ["", "## Every run", "",
            "| # | run | outcome | gate | quality | primary | dollars | bundle | score |", "|---|---|---|---|---|---|---|---|---|"]
    metric = report["decision"]["rule"]["metric"]
    for row in report["runs"]:
        if row["state"] != "done":
            out.append(f"| {row['order_index']} | {row['run_id']} | missing | | | | | | |")
            continue
        out.append(f"| {row['order_index']} | {row['run_id']} | {row['status']} | {row['gate']} | {row['quality']} "
                   f"({row['hidden_passed']}/{row['hidden_total']}) | {_cell(row['metrics'].get(metric))} | "
                   f"{_cell(row['dollars'])} | `{row['bundle_digest'][:12]}` | `{row['score_digest'][:12]}` |")
    spend = report["spend"]
    out += ["", "## Spend", "", f"Ledger: ${spend['ledger_dollars']:.4f} of ${spend['total_dollars']:.2f}. Per arm "
            "(bundles): " + ", ".join(f"{k} ${v:.4f}" for k, v in spend["per_arm_bundle_dollars"].items()) + ".", ""]
    if spend["raised"]:
        out += ["Runs that raised and left no bundle (from the ledger):", ""]
        out += [f"- {r['run_id']} ({r['error']}): ${r['dollars']}" for r in spend["raised"]]
        out.append("")
    out += ["## Versions", ""] + [f"- {k}: {', '.join(v) or 'none'}" for k, v in report["versions"].items()]
    out += ["", f"## hidden_feedback: {report['hidden_feedback']['mode']}", "", report["hidden_feedback"]["meaning"], "",
            "## Caveats", ""] + [f"- {c}" for c in report["caveats"]]
    out += ["", "## Reporting commitments (from the spec)", ""] + [f"- {c}" for c in report["reporting"]]
    out += ["", "## Digests", "", f"- spec `{report['spec_digest']}`", f"- index `{report['index_digest']}`"]
    out += [f"- {row['run_id']}: bundle `{row['bundle_digest']}`, score `{row['score_digest']}`"
            for row in report["runs"] if row["state"] == "done"]
    return "\n".join(out) + "\n"


def write(evals_root: str | Path, eval_id: str, *, partial: bool = False) -> tuple[dict, Path, Path]:
    """Build the report and write its Markdown and JSON; returns ``(report, markdown path, json path)``."""
    report = build(evals_root, eval_id, partial=partial)
    folder = Path(evals_root).resolve() / eval_id
    stem = "report.partial" if partial else "report"
    md, js = folder / f"{stem}.md", folder / f"{stem}.json"
    md.write_bytes(markdown(report).encode("utf-8"))
    write_json(js, report)
    return report, md, js


def digest_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
