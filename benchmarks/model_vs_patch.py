"""Model-backed search against a single model patch, at matched model-call cost.

    PYTHONPATH=. python benchmarks/model_vs_patch.py                 # simulated model, no cost
    PYTHONPATH=. python benchmarks/model_vs_patch.py --adapter "python adapters/anthropic_adapter.py" \\
        --calls 6 --repeats 3                                        # a real model: spends money

Arms, each run through the same Build Thread so every candidate is host-evaluated and recorded:

- ``single``: one patch from one model call (the baseline);
- ``best_of_k``: up to K independent patches, stopping at the first that passes;
- ``repair``: up to K calls, each given the previous candidate and its failing check output;
- ``evolve``: the EvoGit-style provider with a model mutator capped at K calls (crossover costs no call),
  branching by ``FixedPolicy`` revision 2 (the first passing seed or child from ``base`` ends the search);
- ``evolve_empirical``: the same provider, mutator, population, generations and K, branching by
  ``EmpiricalPolicy`` (the fixed schedule with the round kind learned by Thompson sampling);
- ``evolve_jev``: the same, branching by ``JevPolicy`` (each action bought by expected admission per
  dollar; its price prior comes from the contract's ``budget.prices`` when present);
- ``evolve_jevapi``: the same, branching by ``TypeSafeJevPolicy`` (``syberlabs.jev_api``): ``JevPolicy``'s
  offered actions chosen by TypeSafe's Jev API, with ``JevPolicy`` as the fallback. It calls
  ``https://api.typesafe.ai/v1/systemone`` with ``TYPESAFE_API_KEY`` from the environment, so it is not
  among the default arms: name it with ``--arms`` to run it. Its row adds ``jev_api`` (Jev's calls,
  decisions by source, fallbacks by reason, agreement with the local choice, input tokens, Jev's dollars
  at TypeSafe's published input rate, and the median latency); Jev's dollars are outside the host's
  ``host_dollars``, which observes the model adapter only.

The three ``evolve*`` arms differ in the branch policy and in nothing else, so a
difference between them is policy against policy, not "fill the population first"
against "stop at the first pass". Every row of an ``evolve*`` arm records ``policy``
(name and revision, from the provider's trace). The fixture contract is priced for
the run (``budget.prices`` from the table below plus a nominal rate for the simulated
model, and ``budget.max_dollars``), so the host observes spend; a row's
``host_dollars`` is that observation. For the simulated model it is a token count in
dollar units at the nominal rate, not money, and the row's ``dollars`` stays null.

Every arm stops at its first passing candidate, so "calls" is cost to first solve,
capped at K. A solve is a candidate whose host evaluation passed every required
check and that admission would let a person accept. Token counts come from the
adapter's reported usage; dollars use the price table below and are omitted for
the simulated model. With the simulated model, results describe the harness and
the simulator's probabilities, not a model.

Each row names the served model (``usage.model``, or ``simulated``) and carries
``provider_error`` (the ``provider_*`` code of a failed call, or null). With
``--write PREFIX`` every run's thread journal is exported beside the rows, to
``PREFIX-journals/<fixture>-<arm>-<seed>.json``, and the row's ``journal`` holds
that path relative to the rows file, so a branching decision or a call that
produced no candidate can be read back after the temporary worktree is gone.
``--profile NAME`` names a per-operator success profile of the simulated adapter
(``SIM_PROFILE``; ``benchmarks/simulated_model.py``) and is recorded in each row as
``profile``; it changes nothing for a real adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.fixtures import FIXTURES, contract  # noqa: E402
from examples.evolve import git  # noqa: E402
from syberlabs import Kit, Rejected  # noqa: E402
from benchmarks.simulated_model import PROFILES  # noqa: E402
from syberlabs.evolve import CommandMutator, EmpiricalPolicy, EvolutionaryProvider, JevPolicy  # noqa: E402
from syberlabs.jev_api import REASONS as JEV_REASONS, SOURCES as JEV_SOURCES, TypeSafeJevPolicy  # noqa: E402
from syberlabs.providers import model_usage, run_json, split_command, tool_usage  # noqa: E402

SIMULATED = f"{sys.executable} {ROOT / 'benchmarks' / 'simulated_model.py'}"
ARMS = ("single", "best_of_k", "repair", "evolve", "evolve_empirical", "evolve_jev")  # the default arms
JEV_API_ARM = "evolve_jevapi"  # a live TypeSafe call per decision: run only when named
ALL_ARMS = ARMS + (JEV_API_ARM,)
EVOLVE_ARMS = {"evolve": "fixed", "evolve_empirical": "empirical", "evolve_jev": "jev", JEV_API_ARM: "jev_api"}
# Dollars per million tokens: input, output. Cache reads bill at 0.1x input, cache writes at 1.25x.
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-opus-5-5": (4.0, 20.0), "claude-opus-4-8": (5.0, 25.0),
          "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
          "claude-haiku-5-5": (0.10, 0.50), "claude-fable-5-1": (10.0, 50.0)}
# The simulated model's nominal rate (the Sonnet 5.5 tier), so the host can price its "tokens" too; not money.
SIMULATED_PRICE = (2.0, 10.0)
MAX_DOLLARS = 50.0  # the contract's bound per run; far above K calls at any rate in the table, so it stops nothing
NOMINAL_CALL = (2000, 500)  # tokens in, out: the prior worth one attempt that Jev starts a mutation's price from


def price_table() -> dict:
    """``PRICES`` and the simulated rate as the contract's ``budget.prices``, cache rates as ``dollars`` bills them."""
    return {model: {"input_per_million": rate[0], "output_per_million": rate[1],
                    "cache_read_per_million": rate[0] * 0.1, "cache_creation_per_million": rate[0] * 1.25}
            for model, rate in {**PRICES, "simulated": SIMULATED_PRICE}.items()}


def jev_prices(doc: dict) -> dict | None:
    """Jev's price prior per action kind from the contract's ``budget.prices``, or None when it prices nothing.

    A mutation is one nominal call at the mean of the table's rates (the host's observation of the
    first call replaces it); a crossover and an evaluation make no model call.
    """
    table = doc.get("evolution", {}).get("budget", {}).get("prices")
    if not table:
        return None
    calls = [(NOMINAL_CALL[0] * rates["input_per_million"] + NOMINAL_CALL[1] * rates["output_per_million"]) / 1e6
             for rates in table.values()]
    return {"mutate": round(statistics.mean(calls), 6), "crossover": 0.0, "evaluate": 0.0}


def dollars(usage: list[dict]) -> float | None:
    total = 0.0
    for item in usage:
        price = PRICES.get(item.get("model", ""))
        if price is None:
            return None
        total += (item.get("input_tokens", 0) * price[0] + item.get("output_tokens", 0) * price[1]
                  + item.get("cache_read_input_tokens", 0) * price[0] * 0.1
                  + item.get("cache_creation_input_tokens", 0) * price[0] * 1.25) / 1e6
    return total


class Patches:
    """single, best_of_k, and repair: model calls through the search protocol, one candidate each."""

    operators = ("patch",)

    def __init__(self, argv: list[str], calls: int, repair: bool, seed: int):
        self.argv, self.calls, self.repair, self.seed = argv, calls, repair, seed
        self.name, self.revision = ("repair" if repair else "patches"), "1"
        self.usage, self.used = [], 0

    def search(self, space):
        previous = None
        for number in range(self.calls):
            request = {"protocol": "syberlabs.search/v0alpha1", "objective": space.objective, "base": space.base,
                       "scope": list(space.scope), "context": [dict(item) for item in space.context],
                       "files": space.files(), "seed": self.seed * 1000 + number}
            if self.repair and previous is not None:
                found = space.candidate(previous)
                request["feedback"] = {"candidate": previous,
                                       "files": {p: space.read(previous, p) for p in found.changed_paths if space.read(previous, p) is not None},
                                       "checks": [c.__dict__ for c in found.checks]}
            self.used += 1
            response = run_json(self.argv, request, timeout=600, cwd=None)
            usage = model_usage(response)
            if usage:
                self.usage.append(usage)
            items = response.get("candidates", []) if isinstance(response, dict) else []
            if not items:
                continue
            try:
                made = space.submit(items[0]["changes"], parents=[previous] if (self.repair and previous) else [],
                                    operator="patch", message=items[0].get("message", "")[:200],
                                    signal={**(items[0].get("signal") or {}), **({"model_usage": usage} if usage else {}),
                                            **({"tool_usage": tool_usage(response)} if tool_usage(response) else {})})
            except Rejected as exc:
                if exc.code in ("no_change", "invalid_change"):
                    continue
                raise
            if space.evaluate(made.id).passed:
                return [made.id]
            previous = made.id
        return []


def jev_summary(policy: TypeSafeJevPolicy, trace: list[dict]) -> dict:
    """The ``evolve_jevapi`` row's account of Jev: from the policy's counters and its decision entries."""
    decisions = [e for e in trace if "decision" in e]
    asked = [e for e in decisions if e.get("source") == "jev_api"]
    fallbacks = [e["fallback"]["reason"] for e in decisions if e.get("fallback")]
    latencies = sorted(e["latency_ms"] for e in decisions if e.get("latency_ms") is not None)
    return {"model": policy.model, "calls": policy.calls,
            "by_source": {s: sum(1 for e in decisions if e.get("source") == s) for s in JEV_SOURCES},
            "fallbacks": {r: fallbacks.count(r) for r in JEV_REASONS if r in fallbacks},
            "switched_to_local": any((e.get("fallback") or {}).get("switched_to_local") for e in decisions),
            "agreed": sum(1 for e in asked if e["action"] == e["local_action"]), "asked": len(asked),
            "input_tokens": policy.input_tokens, "dollars": policy.jev_dollars,
            "latency_ms_median": statistics.median(latencies) if latencies else None}


def run(fixture: str, arm: str, adapter: str, calls: int, seed: int, folder: Path,
        journals: Path | None = None, profile: str | None = None, jev_transport=None) -> dict:
    """One run. ``journals`` is a directory to export the thread's journal into, or None to keep nothing.
    ``profile`` names a simulated profile (``SIM_PROFILE`` for the adapter) or clears it. ``jev_transport``
    replaces the ``evolve_jevapi`` arm's HTTP call (``syberlabs.jev_api.http_transport``), for tests."""
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"profile must be one of {sorted(PROFILES)}")
    if profile is None:
        os.environ.pop("SIM_PROFILE", None)
    else:
        os.environ["SIM_PROFILE"] = profile
    root = folder / f"{fixture}-{arm}-{seed}"
    (root / "src").mkdir(parents=True)
    for path, text in FIXTURES[fixture]["files"].items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            (root / path).write_bytes(text)
        else:
            (root / path).write_text(text)
    git(root.parent, "init", "-q", "-b", "main", str(root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    home = root / ".syberlabs"
    (home / "contracts").mkdir(parents=True)
    doc = contract(fixture)
    doc["evolution"]["budget"] = {**doc["evolution"]["budget"], "prices": price_table(), "max_dollars": MAX_DOLLARS}
    (home / "contracts" / f"{doc['id']}.v1.json").write_text(json.dumps(doc))
    os.environ["SYBERLABS_FIXTURE"] = fixture
    argv = split_command(adapter)
    kit = Kit.local(home, root, actor="bench")
    thread = kit.start(FIXTURES[fixture]["objective"], f"{doc['id']}.v1", paths=["src"])
    started = time.monotonic()
    policy = None
    if arm in EVOLVE_ARMS:
        mutator = CommandMutator(argv, name="model", max_calls=calls, cwd=root)
        shape = {"population": min(4, calls), "generations": 200, "crossover_every": 3}
        if EVOLVE_ARMS[arm] == "fixed":
            provider = EvolutionaryProvider(mutator, **shape, stop_on_first_pass=True)
        elif EVOLVE_ARMS[arm] == "empirical":
            provider = EvolutionaryProvider(mutator, **shape,
                                            policy=EmpiricalPolicy(**shape, stop_on_first_pass=True, salt=seed))
        elif EVOLVE_ARMS[arm] == "jev":
            provider = EvolutionaryProvider(mutator, **shape,
                                            policy=JevPolicy(generations=shape["generations"], prices=jev_prices(doc)))
        else:
            provider = EvolutionaryProvider(mutator, **shape, policy=TypeSafeJevPolicy(
                generations=shape["generations"], prices=jev_prices(doc), transport=jev_transport))
        thread.propose(provider, seed=seed)
        used, usage = mutator.calls, mutator.usage
        policy = next((dict(e["policy"]) for e in provider.trace if "decision" in e), None)
    else:
        provider = Patches(argv, 1 if arm == "single" else calls, arm == "repair", seed)
        provider.argv = argv
        os.chdir(root)
        try:
            thread.propose(provider, seed=seed)
        finally:
            os.chdir(ROOT)
        used, usage = provider.used, provider.usage
    seconds = time.monotonic() - started
    jev = jev_summary(provider.policy, provider.trace) if arm == JEV_API_ARM else None
    passing = [c for c in thread.candidates() if c.evaluation == "passed"]
    solved = bool(passing) and thread.check(passing[0].id, run=False).acceptable
    events = thread.history()
    journal = None
    if journals is not None:
        journals.mkdir(parents=True, exist_ok=True)
        journal = f"{journals.name}/{fixture}-{arm}-{seed}.json"
        (journals / f"{fixture}-{arm}-{seed}.json").write_text(json.dumps(kit.export(thread.id), indent=1) + "\n",
                                                               encoding="utf-8")
        if jev is not None:  # the provider's trace is not journaled; Jev's decision entries are kept beside it
            jev["decisions"] = f"{journals.name}/{fixture}-{arm}-{seed}.decisions.json"
            (journals / f"{fixture}-{arm}-{seed}.decisions.json").write_text(
                json.dumps([e for e in provider.trace if "decision" in e], indent=1) + "\n", encoding="utf-8")
    kit.close()
    simulated = any(u.get("simulated") for u in usage)
    served = sorted({u.get("model") for u in usage if u.get("model")})
    finished = next((e["body"] for e in reversed(events) if e["kind"] == "search_finished"), None)
    spend = (finished or {}).get("spend") or {}
    row = {"fixture": fixture, "arm": arm, "seed": seed, "solved": solved, "calls": used,
            "model": "simulated" if simulated else ("+".join(served) if served else None),
            "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
            "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
            "dollars": None if simulated else dollars(usage),
            "host_dollars": spend.get("dollars"),
            "simulated": simulated, "policy": policy, "profile": profile,
            "provider_error": next((e["body"]["error"] for e in events if e["kind"] == "search_finished" and str(e["body"].get("error") or "").startswith("provider_")), None),
            "candidates": sum(1 for e in events if e["kind"] == "candidate_registered"),
            "evaluations": sum(1 for e in events if e["kind"] == "candidate_evaluated"), "seconds": round(seconds, 2),
            "journal": journal}
    if jev is not None:
        row["jev_api"] = jev
    return row


def summarize(rows: list[dict], calls: int) -> str:
    simulated = any(r["simulated"] for r in rows)
    profiles = sorted({r.get("profile") for r in rows if r.get("profile")})
    lines = [f"{'SIMULATED MODEL: these numbers describe the harness, not a model. ' if simulated else ''}"
             f"Budget K = {calls} model calls per run; every arm stops at its first passing candidate."
             f"{(' Simulated profile: ' + ', '.join(profiles) + '.') if profiles else ''}",
             "fixture     arm               solved  calls(med)  tokens in/out (mean)   evaluations(med)  $ (mean)  "
             "host$ (mean)  model                 policy        provider_error"]
    for fixture in dict.fromkeys(r["fixture"] for r in rows):
        for arm in ALL_ARMS:
            group = [r for r in rows if r["fixture"] == fixture and r["arm"] == arm]
            if not group:
                continue
            cost = [r["dollars"] for r in group if r["dollars"] is not None]
            host = [r["host_dollars"] for r in group if r.get("host_dollars") is not None]
            models = sorted({r.get("model") or "-" for r in group})
            policies = sorted({f"{r['policy']['name']}/{r['policy']['revision']}" for r in group if r.get("policy")})
            errors = [r["provider_error"] for r in group if r.get("provider_error")]
            lines.append(f"{fixture:11} {arm:17} {sum(r['solved'] for r in group):>2}/{len(group):<4} "
                         f"{statistics.median(r['calls'] for r in group):>9} "
                         f"{statistics.mean(r['input_tokens'] for r in group):>10.0f}/{statistics.mean(r['output_tokens'] for r in group):<9.0f} "
                         f"{statistics.median(r['evaluations'] for r in group):>14} "
                         f"{('%.4f' % statistics.mean(cost)) if cost and len(cost) == len(group) else 'n/a':>10}  "
                         f"{('%.4f' % statistics.mean(host)) if host and len(host) == len(group) else 'n/a':>12}  "
                         f"{'+'.join(models):21} "
                         f"{('+'.join(policies) if policies else '-'):13} "
                         f"{(str(len(errors)) + '/' + str(len(group)) + ' ' + ','.join(sorted(set(errors)))) if errors else 'none':}")
    jev_rows = [r for r in rows if r.get("jev_api")]
    if jev_rows:
        lines.append("fixture     jev model    calls  asked  agreed  fallbacks  switched  input_tokens  jev$ (sum)  latency_ms(med)")
        for fixture in dict.fromkeys(r["fixture"] for r in jev_rows):
            group = [r["jev_api"] for r in jev_rows if r["fixture"] == fixture]
            medians = [g["latency_ms_median"] for g in group if g["latency_ms_median"] is not None]
            lines.append(f"{fixture:11} {group[0]['model']:12} {sum(g['calls'] for g in group):>5} "
                         f"{sum(g['asked'] for g in group):>6} {sum(g['agreed'] for g in group):>7} "
                         f"{sum(sum(g['fallbacks'].values()) for g in group):>10} "
                         f"{sum(g['switched_to_local'] for g in group):>9} {sum(g['input_tokens'] for g in group):>13} "
                         f"{sum(g['dollars'] for g in group):>11.6f}  "
                         f"{statistics.median(medians) if medians else 'n/a'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--adapter", default=SIMULATED, help="model adapter command (default: the simulated model)")
    parser.add_argument("--calls", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--fixtures", nargs="+", default=list(FIXTURES))
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ALL_ARMS),
                        help=f"default: every arm but {JEV_API_ARM}, which calls TypeSafe's Jev API")
    parser.add_argument("--write", help="write the JSON rows and the summary to this path prefix")
    parser.add_argument("--profile", choices=sorted(PROFILES),
                        help="a per-operator success profile of the simulated adapter (SIM_PROFILE); nothing for a real one")
    options = parser.parse_args()
    rows = []
    journals = Path(options.write + "-journals") if options.write else None
    with tempfile.TemporaryDirectory() as folder:
        for fixture in options.fixtures:
            for arm in options.arms:
                for seed in range(1, options.repeats + 1):
                    rows.append(run(fixture, arm, options.adapter, options.calls, seed, Path(folder), journals,
                                    options.profile))
    report = summarize(rows, options.calls)
    print(report)
    if options.write:
        Path(options.write + ".json").write_text(json.dumps(rows, indent=1) + "\n")
        Path(options.write + ".txt").write_text(report + "\n")


if __name__ == "__main__":
    main()
