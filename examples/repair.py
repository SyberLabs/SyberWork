"""Repair search on a Build Thread, with SyberWork keeping authority.

The same toy pricing module as ``examples/evolve.py``: three rules, each checked
by its own required check. A stand-in "model" adapter answers the search
protocol: asked once, it fixes one rule; asked again with the host's check
results for its failed attempt (``feedback``), it fixes exactly the rules whose
checks failed. The repair provider registers each answer as a candidate, has the
host evaluate it, and stops at the first one that passes. Every candidate stays
on a non-authoritative ref. A person then checks and accepts one.

Repair is the factory's default proposer path (owner decision of 2026-10-23);
the evolutionary search in ``examples/evolve.py`` stays as research.

    python examples/repair.py [seed]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs import Kit  # noqa: E402
from syberlabs.providers import RepairProvider  # noqa: E402

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

# The stand-in adapter. It reads one search request on stdin and prints one candidate. Without ``feedback`` it
# fixes a single rule chosen by the request's seed; with ``feedback`` it reads the failed candidate's text from
# ``feedback.files`` and fixes the rules whose checks did not pass, which is what the host's results tell it.
ADAPTER = textwrap.dedent('''\
    import json, re, sys

    FIXED = {"discount": "return total * 0.9 if total > 100 else total",
             "tax": "return round(amount * 1.08, 2)",
             "shipping": "return weight * 5"}
    request = json.load(sys.stdin)
    feedback = request.get("feedback")
    if feedback:
        text = feedback["files"]["src/pricing.py"]
        rules = [c["name"] for c in feedback["checks"] if c["state"] != "passed"]
    else:
        with open("src/pricing.py", encoding="utf-8") as f:  # the provider runs the adapter in the repository
            text = f.read()
        rules = [sorted(FIXED)[request["seed"] % len(FIXED)]]
    for rule in rules:
        text = re.sub(rf"(def {rule}\\(\\w+\\):\\n    )return [^\\n]*", lambda m: m.group(1) + FIXED[rule], text, count=1)
    print(json.dumps({"candidates": [{"changes": {"src/pricing.py": text}, "message": "fix " + ", ".join(rules)}],
                      "recommended": [0],
                      "usage": {"input_tokens": len(text) // 4, "output_tokens": len(text) // 4,
                                "model": "stand-in", "simulated": True}}))
    ''')


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def check(expression: str) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.path.insert(0, 'src'); import pricing; assert {expression}"]


def contract() -> dict:
    return {
        "id": "pricing-repair", "version": 1, "title": "Fix the pricing rules",
        "inputs": {"objective": "string"},
        "actions": {"accept_change": {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
        "evolution": {
            "scope": {"paths": ["src/"], "max_files": 3, "max_diff_bytes": 20_000},
            "operators": ["patch"],
            "budget": {"max_candidates": 6, "max_evaluations": 6, "max_seconds": 300},
            "evaluation": {
                "checks": {"discount": {"argv": check("pricing.discount(200) == 180 and pricing.discount(50) == 50")},
                           "tax": {"argv": check("pricing.tax(100) == 108")},
                           "shipping": {"argv": check("pricing.shipping(3) == 15")}},
                "required": ["discount", "tax", "shipping"],
            },
            "promotion": {"action": "accept_change", "roles": ["developer"]},
        },
    }


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
    adapter = root.parent / "adapter.py"
    adapter.write_text(ADAPTER)
    home = root / ".syberlabs"
    (home / "contracts").mkdir(parents=True)
    (home / "contracts" / "pricing-repair.v1.json").write_text(json.dumps(contract()))
    kit = Kit.local(home, root, actor="dev@example.test")
    thread = kit.start("Make discount, tax, and shipping rules pass their checks", "pricing-repair.v1")
    provider = RepairProvider([sys.executable, str(adapter)], name="stand-in", cwd=root)
    found = thread.propose(provider, seed=seed)
    search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
    heads = git(root, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
    best = search["recommended"][0] if search["recommended"] else None
    verdict = thread.check(best) if best else None
    receipt = thread.accept(best) if verdict and verdict.acceptable else None
    result = {
        "candidates": len(found), "search": {k: search[k] for k in ("stopped", "evaluations", "recommended")},
        "calls": provider.calls, "trace": provider.trace,
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
    print(f"repair {'complete' if ok else 'did not find an acceptable candidate'}")
    raise SystemExit(0 if ok else 1)
