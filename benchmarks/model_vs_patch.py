"""Model-backed search against a single model patch, at matched model-call cost.

    PYTHONPATH=. python benchmarks/model_vs_patch.py                 # simulated model, no cost
    PYTHONPATH=. python benchmarks/model_vs_patch.py --adapter "python adapters/anthropic_adapter.py" \\
        --calls 6 --repeats 3                                        # a real model: spends money

Arms, each run through the same Build Thread so every candidate is host-evaluated and recorded:

- ``single``: one patch from one model call (the baseline);
- ``best_of_k``: up to K independent patches, stopping at the first that passes;
- ``repair``: up to K calls, each given the previous candidate and its failing check output;
- ``evolve``: the EvoGit-style provider with a model mutator capped at K calls (crossover costs no call).

Every arm stops at its first passing candidate, so "calls" is cost to first solve,
capped at K. A solve is a candidate whose host evaluation passed every required
check and that admission would let a person accept. Token counts come from the
adapter's reported usage; dollars use the price table below and are omitted for
the simulated model. With the simulated model, results describe the harness and
the simulator's probabilities, not a model.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
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
from syberlabs.evolve import CommandMutator, EvolutionaryProvider  # noqa: E402
from syberlabs.providers import model_usage, run_json  # noqa: E402

SIMULATED = f"{sys.executable} {ROOT / 'benchmarks' / 'simulated_model.py'}"
ARMS = ("single", "best_of_k", "repair", "evolve")
# Dollars per million tokens: input, output. Cache reads bill at 0.1x input, cache writes at 1.25x.
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-opus-5-5": (4.0, 20.0), "claude-opus-4-8": (5.0, 25.0),
          "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0), "claude-fable-5-1": (10.0, 50.0)}


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
                                    signal={**(items[0].get("signal") or {}), **({"model_usage": usage} if usage else {})})
            except Rejected as exc:
                if exc.code in ("no_change", "invalid_change"):
                    continue
                raise
            if space.evaluate(made.id).passed:
                return [made.id]
            previous = made.id
        return []


def run(fixture: str, arm: str, adapter: str, calls: int, seed: int, folder: Path) -> dict:
    root = folder / f"{fixture}-{arm}-{seed}"
    (root / "src").mkdir(parents=True)
    for path, text in FIXTURES[fixture]["files"].items():
        (root / path).write_text(text)
    git(root.parent, "init", "-q", "-b", "main", str(root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    home = root / ".syberlabs"
    (home / "contracts").mkdir(parents=True)
    doc = contract(fixture)
    (home / "contracts" / f"{doc['id']}.v1.json").write_text(json.dumps(doc))
    os.environ["SYBERLABS_FIXTURE"] = fixture
    argv = shlex.split(adapter)
    kit = Kit.local(home, root, actor="bench")
    thread = kit.start(FIXTURES[fixture]["objective"], f"{doc['id']}.v1", paths=["src"])
    started = time.monotonic()
    if arm == "evolve":
        mutator = CommandMutator(argv, name="model", max_calls=calls, cwd=root)
        provider = EvolutionaryProvider(mutator, population=min(4, calls), generations=200, crossover_every=3)
        thread.propose(provider, seed=seed)
        used, usage = mutator.calls, mutator.usage
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
    passing = [c for c in thread.candidates() if c.evaluation == "passed"]
    solved = bool(passing) and thread.check(passing[0].id, run=False).acceptable
    events = thread.history()
    kit.close()
    return {"fixture": fixture, "arm": arm, "seed": seed, "solved": solved, "calls": used,
            "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
            "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
            "dollars": None if any(u.get("simulated") for u in usage) else dollars(usage),
            "simulated": any(u.get("simulated") for u in usage),
            "candidates": sum(1 for e in events if e["kind"] == "candidate_registered"),
            "evaluations": sum(1 for e in events if e["kind"] == "candidate_evaluated"), "seconds": round(seconds, 2)}


def summarize(rows: list[dict], calls: int) -> str:
    simulated = any(r["simulated"] for r in rows)
    lines = [f"{'SIMULATED MODEL: these numbers describe the harness, not a model. ' if simulated else ''}"
             f"Budget K = {calls} model calls per run; every arm stops at its first passing candidate.",
             "fixture     arm         solved  calls(med)  tokens in/out (mean)   evaluations(med)  $ (mean)"]
    for fixture in dict.fromkeys(r["fixture"] for r in rows):
        for arm in ARMS:
            group = [r for r in rows if r["fixture"] == fixture and r["arm"] == arm]
            if not group:
                continue
            cost = [r["dollars"] for r in group if r["dollars"] is not None]
            lines.append(f"{fixture:11} {arm:11} {sum(r['solved'] for r in group):>2}/{len(group):<4} "
                         f"{statistics.median(r['calls'] for r in group):>9} "
                         f"{statistics.mean(r['input_tokens'] for r in group):>10.0f}/{statistics.mean(r['output_tokens'] for r in group):<9.0f} "
                         f"{statistics.median(r['evaluations'] for r in group):>14} "
                         f"{('%.4f' % statistics.mean(cost)) if cost and len(cost) == len(group) else 'n/a':>10}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--adapter", default=SIMULATED, help="model adapter command (default: the simulated model)")
    parser.add_argument("--calls", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--fixtures", nargs="+", default=list(FIXTURES))
    parser.add_argument("--arms", nargs="+", default=list(ARMS))
    parser.add_argument("--write", help="write the JSON rows and the summary to this path prefix")
    options = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory() as folder:
        for fixture in options.fixtures:
            for arm in options.arms:
                for seed in range(1, options.repeats + 1):
                    rows.append(run(fixture, arm, options.adapter, options.calls, seed, Path(folder)))
    report = summarize(rows, options.calls)
    print(report)
    if options.write:
        Path(options.write + ".json").write_text(json.dumps(rows, indent=1) + "\n")
        Path(options.write + ".txt").write_text(report + "\n")


if __name__ == "__main__":
    main()
