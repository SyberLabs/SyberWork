"""EvoGit-style search on a Build Thread, with SyberWork keeping authority.

A toy pricing module has three rules, each checked by its own required check.
A stand-in "model" mutator rewrites one rule at a time, sometimes correctly.
The evolutionary provider keeps a population of Git-lineaged candidates,
selects on the host's own check results, and merges individuals so partial
fixes from different lineages can combine. Every individual stays on a
non-authoritative ref. A person then checks and accepts one.

    python examples/evolve.py [seed]
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs import Kit  # noqa: E402
from syberlabs.evolve import EvolutionaryProvider  # noqa: E402

PRICING = '''"""Pricing rules for the demo shop."""


def discount(total):
    return total


# Taxes are applied after discounts.


def tax(amount):
    return amount


# Shipping depends on weight.


def shipping(weight):
    return 0
'''

VARIANTS = {
    "discount": ["return total", "return total * 0.9", "return total * 0.9 if total > 100 else total", "return total - 10"],
    "tax": ["return amount", "return amount * 1.08", "return round(amount * 1.08, 2)", "return amount + 8"],
    "shipping": ["return 0", "return weight * 5", "return weight * 4", "return 5"],
}


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def check(expression: str) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.path.insert(0, 'src'); import pricing; assert {expression}"]


def contract() -> dict:
    return {
        "id": "pricing-evolution", "version": 1, "title": "Fix the pricing rules",
        "inputs": {"objective": "string"},
        "actions": {"accept_change": {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
        "evolution": {
            "scope": {"paths": ["src/"], "max_files": 3, "max_diff_bytes": 20_000},
            "operators": ["patch", "mutation", "crossover"],
            "budget": {"max_candidates": 40, "max_evaluations": 40, "max_seconds": 300},
            "evaluation": {
                "checks": {"discount": {"argv": check("pricing.discount(200) == 180 and pricing.discount(50) == 50")},
                           "tax": {"argv": check("pricing.tax(100) == 108")},
                           "shipping": {"argv": check("pricing.shipping(3) == 15")}},
                "required": ["discount", "tax", "shipping"],
            },
            "promotion": {"action": "accept_change", "roles": ["developer"]},
        },
    }


class RuleMutator:
    """Stands in for a model: rewrites one rule's return line, right or wrong."""

    name = "rule-mutator"

    def __call__(self, space, parent: str, rng: random.Random):
        text = space.read(parent, "src/pricing.py")
        rule = rng.choice(sorted(VARIANTS))
        pattern = re.compile(rf"(def {rule}\(\w+\):\n    )return [^\n]*")
        return {"src/pricing.py": pattern.sub(lambda m: m.group(1) + rng.choice(VARIANTS[rule]), text, count=1)}


def make_repository(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "pricing.py").write_text(PRICING)
    git(root.parent, "init", "-q", "-b", "main", str(root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")


def lineage(thread) -> list[str]:
    lines = []
    for candidate in thread.candidates():
        passed = sum(c.state == "passed" for c in candidate.checks)
        origin = "+".join(candidate.parents) or "base"
        lines.append(f"{candidate.id:4} {candidate.operator:9} from {origin:8} checks {passed}/3  {candidate.evaluation}")
    return lines


def run(root: Path, seed: int = 7) -> dict:
    make_repository(root)
    home = root / ".syberlabs"
    (home / "contracts").mkdir(parents=True)
    (home / "contracts" / "pricing-evolution.v1.json").write_text(json.dumps(contract()))
    kit = Kit.local(home, root, actor="dev@example.test")
    thread = kit.start("Make discount, tax, and shipping rules pass their checks", "pricing-evolution.v1")
    provider = EvolutionaryProvider(RuleMutator(), population=4, generations=10, crossover_every=3)
    found = thread.propose(provider, seed=seed)
    search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
    heads = git(root, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
    best = search["recommended"][0] if search["recommended"] else None
    verdict = thread.check(best) if best else None
    receipt = thread.accept(best) if verdict and verdict.acceptable else None
    result = {
        "candidates": len(found), "search": {k: search[k] for k in ("stopped", "evaluations", "recommended")},
        "branches_before_accept": heads, "lineage": lineage(thread),
        "recommended": best, "verdict": verdict.reason if verdict else None,
        "accepted": receipt.status if receipt else None, "branch": receipt.ref if receipt else None,
    }
    kit.close()
    return result


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        report = run(Path(folder) / "shop", int(sys.argv[1]) if len(sys.argv) > 1 else 7)
    print("\n".join(report.pop("lineage")))
    print(json.dumps(report, indent=2))
    ok = report["accepted"] == "succeeded" and report["branches_before_accept"] == ["refs/heads/main"]
    print(f"evolve {'complete' if ok else 'did not find an acceptable candidate'}")
    raise SystemExit(0 if ok else 1)
