"""Arms: named provider configurations, each built into one of the kernel's own providers, each with a digest.

An arm is an object in the spec's ``arms`` list (``spec.ARM_SCHEMA``). ``kind`` picks the provider; ``model`` and
``effort`` are always explicit; ``adapter`` is the model command (argv; ``{python}`` is the host's interpreter and
``{harness}`` the SyberWork checkout this package runs from):

- ``single``: ``providers.CommandProvider(adapter)``, one call (the kernel's one-shot path; its request carries no
  ``seed``, so a seeded adapter is seeded only on the other arms);
- ``repair``: ``providers.RepairProvider(adapter, max_calls=K)``, the factory's default proposer path;
- ``evolve``: ``evolve.EvolutionaryProvider(CommandMutator(adapter, max_calls=K))`` with the fixed branch policy
  stopping at the first pass, population ``min(4, K)`` unless given, the E3 harness's shape;
- ``evolve_jev``: the same provider with ``JevPolicy``, its price prior from the contract's price table;
- ``claude-code``: ``claude_code.ClaudeCodeProvider`` with ``lane``, ``bare``, ``trusted_tree``, ``tools``,
  ``max_turns``, ``mcp_config`` and ``executable`` (a stand-in ``claude`` for tests), and ``githits`` passed through
  by name to the provider's keyword of that name (refused ``arm_unsupported`` when the installed provider has none).
  The runner does not set Claude Code's model: the arm's ``model`` and ``effort`` are the spec's declaration, and the
  model that served is in the candidate's ``signal.harness``.

K is ``min(arm.max_calls or budget.calls, budget.calls)``. ``arm_digest`` is ``syberlabs.canonical.digest`` of the
arm object as written in the spec. ``environment`` is the process environment while the arm proposes: the adapter
reads ``SYBERLABS_MODEL`` and ``SYBERLABS_EFFORT`` from it (``adapters/anthropic_adapter.py``), the simulated model
reads ``SYBERLABS_FIXTURE``, and the certificate seed and GitHub tokens are removed so no provider process inherits
them.
"""

from __future__ import annotations

import inspect
import os
import statistics
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from syberlabs.canonical import digest
from syberlabs.errors import Rejected
from syberlabs.eval.spec import HARNESS

JSON_ADAPTER_KINDS = ("single", "repair", "evolve", "evolve_jev")
EVOLVE_ONLY = ("population", "generations", "crossover_every")
CLAUDE_ONLY = ("lane", "bare", "trusted_tree", "tools", "max_turns", "githits", "mcp_config", "executable")
# Removed from the environment while an arm proposes; restored after (the certificate seed variable is added).
WITHHELD_ENV = ("GITHUB_TOKEN", "GH_TOKEN")
NOMINAL_CALL = (2000, 500)  # tokens in, out: Jev's prior price of one mutation (benchmarks/model_vs_patch.py)


def check(arm: dict, budget: dict) -> list[str]:
    """What is wrong with ``arm`` for its kind (the schema has already passed)."""
    errors = []
    kind = arm["kind"]
    if kind in JSON_ADAPTER_KINDS and "adapter" not in arm:
        errors.append("adapter: required for a JSON-adapter arm")
    if kind == "claude-code" and "adapter" in arm:
        errors.append("adapter: a claude-code arm runs claude (name a stand-in with executable)")
    if kind != "claude-code":
        errors += [f"{key}: only for claude-code" for key in CLAUDE_ONLY if key in arm]
    if kind not in ("evolve", "evolve_jev"):
        errors += [f"{key}: only for evolve arms" for key in EVOLVE_ONLY if key in arm]
    if kind == "single" and arm.get("max_calls", 1) != 1:
        errors.append("max_calls: a single arm makes one call")
    if kind == "claude-code" and arm.get("bare") and arm.get("lane") in ("subscription_local", "seat_team_enterprise"):
        errors.append("lane: --bare never reads a Claude login")
    if "max_calls" in arm and arm["max_calls"] > budget["calls"]:
        errors.append(f"max_calls: above the spec's budget.calls ({budget['calls']})")
    return errors


def arm_digest(arm: dict) -> str:
    return digest(arm)


def calls(arm: dict, budget: dict) -> int:
    return 1 if arm["kind"] == "single" else min(arm.get("max_calls", budget["calls"]), budget["calls"])


def resolve(argv: list[str], harness: Path = HARNESS) -> list[str]:
    """``{python}`` and ``{harness}`` in an adapter or executable argv."""
    return [sys.executable if arg == "{python}" else arg.replace("{harness}", str(harness)) for arg in argv]


def jev_prices(prices: dict | None) -> dict | None:
    """Jev's price prior per action kind from a contract price table: one nominal call at the mean of the rates."""
    if not prices:
        return None
    costs = [(NOMINAL_CALL[0] * r["input_per_million"] + NOMINAL_CALL[1] * r["output_per_million"]) / 1e6
             for r in prices.values()]
    return {"mutate": round(statistics.mean(costs), 6), "crossover": 0.0, "evaluate": 0.0}


def build(arm: dict, *, repo: Path, budget: dict, prices: dict | None, harness: Path = HARNESS):
    """The provider for one run of ``arm`` against the repository at ``repo``."""
    kind, k = arm["kind"], calls(arm, budget)
    timeout = arm.get("timeout_seconds", 600)
    name = arm["name"] if arm["name"][0].isalpha() else "arm-" + arm["name"]
    if kind == "single":
        from syberlabs.providers import CommandProvider
        return CommandProvider(resolve(arm["adapter"], harness), name=name, revision="1", timeout=timeout, cwd=repo)
    if kind == "repair":
        from syberlabs.providers import RepairProvider
        return RepairProvider(resolve(arm["adapter"], harness), name=name, revision="1", timeout=timeout, cwd=repo,
                              max_calls=k)
    if kind in ("evolve", "evolve_jev"):
        from syberlabs.evolve import CommandMutator, EvolutionaryProvider, JevPolicy
        mutator = CommandMutator(resolve(arm["adapter"], harness), name="model", timeout=timeout, cwd=repo, max_calls=k)
        shape = {"population": arm.get("population", min(4, max(2, k))), "generations": arm.get("generations", 200),
                 "crossover_every": arm.get("crossover_every", 3)}
        if kind == "evolve":
            return EvolutionaryProvider(mutator, **shape, stop_on_first_pass=True, name=name, revision="1")
        return EvolutionaryProvider(mutator, **shape, name=name, revision="1",
                                    policy=JevPolicy(generations=shape["generations"], prices=jev_prices(prices)))
    from syberlabs.claude_code import ClaudeCodeProvider
    options = {"bare": arm.get("bare", False), "trusted_tree": arm.get("trusted_tree", False),
               "mcp_config": arm.get("mcp_config"), "allowed_tools": arm.get("tools"),
               "max_turns": arm.get("max_turns"), "lane": arm.get("lane"), "name": name, "revision": "1",
               "max_calls": k, "timeout": timeout}
    if "executable" in arm:
        options["executable"] = resolve(arm["executable"], harness)
    if "githits" in arm:
        if "githits" not in inspect.signature(ClaudeCodeProvider.__init__).parameters:
            raise Rejected("arm_unsupported", "this ClaudeCodeProvider has no githits option")
        options["githits"] = arm["githits"]
    return ClaudeCodeProvider(repo, **options)


@contextmanager
def environment(arm: dict, task: dict, withheld: tuple[str, ...] = ()) -> Iterator[None]:
    """The process environment while ``arm`` proposes on ``task`` (see the module docstring); restored after."""
    saved = dict(os.environ)
    try:
        for key in WITHHELD_ENV + tuple(withheld):
            os.environ.pop(key, None)
        if arm["kind"] in JSON_ADAPTER_KINDS:
            os.environ["SYBERLABS_MODEL"] = arm["model"]
            os.environ["SYBERLABS_EFFORT"] = arm["effort"]
            if arm["model"] == "simulated" and task["source"].startswith("fixture:"):
                os.environ["SYBERLABS_FIXTURE"] = task["source"].split(":", 1)[1]
            else:
                os.environ.pop("SYBERLABS_FIXTURE", None)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def adapter_files(arm: dict, harness: Path = HARNESS) -> list[Path]:
    """The files an arm's adapter or executable argv names, for the bundle's provider versions."""
    argv = resolve(arm.get("adapter") or arm.get("executable") or [], harness)
    return [Path(arg) for arg in argv[1:] if Path(arg).is_file()]
