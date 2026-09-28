"""Does crossover help on the pricing fixture? Compare against mutation-only on the same seeds.

Both arms use the same contract, budget, population, generations, and mutator.
The only difference is whether every third generation merges pairs. The metric
is the host's evaluation: whether a candidate passed every required check, and
how many evaluations were spent before the first one did.

This is a toy fixture with three independent rules, which favors recombination
by construction. It says nothing about real repositories.

    PYTHONPATH=. python benchmarks/evolve_baseline.py [seeds] [--write]
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.evolve import RuleMutator, contract, make_repository  # noqa: E402
from syberlabs import Kit  # noqa: E402
from syberlabs.evolve import EvolutionaryProvider  # noqa: E402

ARMS = {"crossover every 3rd generation": 3, "mutation only": 10_000}


def trial(folder: Path, seed: int, crossover_every: int) -> dict:
    root = folder / f"s{seed}-{crossover_every}"
    make_repository(root)
    home = root / ".syberlabs"
    (home / "contracts").mkdir(parents=True)
    (home / "contracts" / "pricing-evolution.v1.json").write_text(json.dumps(contract()))
    kit = Kit.local(home, root, actor="bench")
    thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
    thread.propose(EvolutionaryProvider(RuleMutator(), population=4, generations=10, crossover_every=crossover_every),
                   seed=seed)
    evaluations = [e["body"] for e in thread.history() if e["kind"] == "candidate_evaluated"]
    first = next((n for n, body in enumerate(evaluations, 1) if all(c["state"] == "passed" for c in body["checks"])), None)
    kit.close()
    return {"found": first is not None, "evaluations_to_pass": first, "evaluations": len(evaluations)}


def main(seeds: int) -> str:
    lines = [f"Pricing fixture, {seeds} seeds, population 4, 10 generations, budget 40 candidates and 40 evaluations."]
    with tempfile.TemporaryDirectory() as folder:
        for label, every in ARMS.items():
            runs = [trial(Path(folder), seed, every) for seed in range(1, seeds + 1)]
            found = [r["evaluations_to_pass"] for r in runs if r["found"]]
            lines.append(f"  {label:32} passing candidate found {len(found)}/{seeds}; evaluations to first pass: "
                         f"median {statistics.median(found) if found else '-'}, "
                         f"mean {statistics.mean(found):.1f}" if found else f"  {label:32} found 0/{seeds}")
    lines.append("A found candidate is still provisional: a person must check and accept it.")
    return "\n".join(lines)


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 20
    report = main(count)
    print(report)
    if "--write" in sys.argv:
        out = ROOT / "benchmarks" / "results" / "evolve-baseline.txt"
        out.write_text(report + "\n")
        print(f"wrote {out}")
