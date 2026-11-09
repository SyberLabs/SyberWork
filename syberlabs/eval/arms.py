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
  by name to the provider's keyword of that name;
  a ``githits`` arm's lookups carry the run's thread and the arm's name (``X-GitHits-Session-ID``, ``-Agent``).
  ``check`` refuses at spec validation what the provider would refuse when built: ``githits`` with ``bare`` (no
  ``--settings`` hook runs under ``--bare``), ``bare`` with lane ``api-hooked`` (that lane is the API key without
  ``--bare``), lane ``api`` without ``bare`` (use ``api-hooked``), and, as the spec has no override, ``bare`` with
  ``WebSearch`` or ``WebFetch`` in ``tools`` or with any ``mcp_config`` (those lookups would leave unscreened; the
  provider builds an ``mcp_config`` naming no server, which an arm has no use for).
  The runner does not set Claude Code's model: the arm's ``model`` and ``effort`` are the spec's declaration, and the
  model that served is in the candidate's ``signal.harness``.

K is ``min(arm.max_calls or budget.calls, budget.calls)``. ``arm_digest`` is ``syberlabs.canonical.digest`` of the
arm object as written in the spec. ``environment`` is the process environment while the arm proposes: the adapter
reads ``SYBERLABS_MODEL`` and ``SYBERLABS_EFFORT`` from it (``adapters/anthropic_adapter.py``), the simulated model
reads ``SYBERLABS_FIXTURE``, and the certificate seed and GitHub tokens are removed so no provider process inherits
them.

``hidden_view`` is the spec's ``hidden_feedback`` mode applied to one ``feedback`` object (``providers.repair_feedback``):
``tails`` returns it unchanged; ``status`` keeps each hidden check's name and ``passed`` or ``failed`` and drops its
exit code, duration and output tail; ``none`` drops every hidden check, and the whole ``feedback`` when no visible
check is left that did not pass (a request that said "the last attempt failed" would be hidden information).
Visible checks and the candidate's own ``files`` pass unchanged in every mode. ``build`` sets it as the
``feedback_filter`` of a ``repair`` or ``claude-code`` provider, recording what each request carried; a ``single``
arm sends no feedback, and an evolve arm is refused any mode but ``tails`` by ``spec.check``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
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
NOMINAL_CALL = (2000, 500)  # tokens in, out: Jev's prior price of one mutation
HIDDEN_PREFIX = "hidden."  # tasks.HIDDEN_PREFIX: the contract names every hidden check hidden.<name>
# Adapters this checkout ships whose every call fails before it reaches a model unless the adapter's interpreter can
# import a package; the repository has no runtime dependencies, so nothing installs it (``readiness``).
ADAPTER_REQUIRES = {"anthropic_adapter.py": "anthropic"}
# The variables an adapter's SDK reads its credential from. None set is a warning, not a refusal: the adapter also
# names an ``ant auth login`` profile, which this check cannot see.
ADAPTER_CREDENTIALS = {"anthropic_adapter.py": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}


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
    if kind == "claude-code":
        errors += claude_code_lane(arm)
    if "max_calls" in arm and arm["max_calls"] > budget["calls"]:
        errors.append(f"max_calls: above the spec's budget.calls ({budget['calls']})")
    return errors


def claude_code_lane(arm: dict) -> list[str]:
    """The provider's lane refusals (``claude_code.ClaudeCodeProvider``) for a claude-code arm, as spec errors."""
    from syberlabs.claude_code import HOOKED, web_tools
    bare, lane, errors = arm.get("bare") is True, arm.get("lane"), []
    if bare and lane in ("subscription_local", "seat_team_enterprise"):
        errors.append("lane: --bare never reads a Claude login")
    if bare and lane == HOOKED:
        errors.append(f"lane: {HOOKED} is the API-key lane without --bare, so the query guard's hook runs; drop bare")
    if not bare and lane == "api":
        errors.append(f"lane: the api lane is --bare; for the API key without --bare use {HOOKED}")
    if bare and arm.get("githits") is True:
        errors.append(f"githits: refused with bare, where no --settings hook runs; use lane {HOOKED} with trusted_tree")
    if bare and web_tools(arm.get("tools") or []):
        errors.append(f"tools: {', '.join(web_tools(arm['tools']))} with bare would leave unscreened (no hook runs "
                      f"under --bare); use lane {HOOKED} with trusted_tree")
    if bare and "mcp_config" in arm:
        errors.append(f"mcp_config: an MCP server's lookups with bare would leave unscreened (no hook runs under "
                      f"--bare); use lane {HOOKED} with trusted_tree")
    return errors


def arm_digest(arm: dict) -> str:
    return digest(arm)


def calls(arm: dict, budget: dict) -> int:
    return 1 if arm["kind"] == "single" else min(arm.get("max_calls", budget["calls"]), budget["calls"])


def resolve(argv: list[str], harness: Path = HARNESS) -> list[str]:
    """``{python}`` and ``{harness}`` in an adapter or executable argv."""
    return [sys.executable if arg == "{python}" else arg.replace("{harness}", str(harness)) for arg in argv]


def readiness(arm: dict, harness: Path = HARNESS, _imports: dict | None = None) -> dict:
    """Why every call of a JSON-adapter ``arm`` would fail before it reaches a model (``blocking``), and what may
    make it fail (``warnings``), without running the adapter or calling a model: the interpreter is found; each
    ``.py`` file the argv names is an absolute path that exists (the adapter runs with the run's repository as its
    working directory, where a relative path does not exist: E3, 2026-10-21); the interpreter can import the package
    an adapter of ``ADAPTER_REQUIRES`` needs (a child process that only imports it); one of the adapter's
    ``ADAPTER_CREDENTIALS`` is set. ``_imports`` caches the import probe by (interpreter, module)."""
    blocking, warnings = [], []
    if arm["kind"] not in JSON_ADAPTER_KINDS:
        return {"blocking": blocking, "warnings": warnings}
    argv, label = resolve(arm["adapter"], harness), arm["name"]
    if not argv or not (Path(argv[0]).is_file() or shutil.which(argv[0])):
        return {"blocking": [f"{label}: interpreter {argv[0] if argv else None!r} not found"], "warnings": warnings}
    imports = {} if _imports is None else _imports
    for arg in argv[1:]:
        path = Path(arg)
        if path.suffix != ".py":
            continue
        if not path.is_absolute():
            blocking.append(f"{label}: adapter {arg} is a relative path; the adapter runs in the run's repository")
            continue
        if not path.is_file():
            blocking.append(f"{label}: adapter {arg} not found")
            continue
        module = ADAPTER_REQUIRES.get(path.name)
        if module:
            key = (argv[0], module)
            if key not in imports:
                try:
                    imports[key] = subprocess.run([argv[0], "-c", f"import {module}"], capture_output=True,
                                                  timeout=120).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    imports[key] = False
            if not imports[key]:
                blocking.append(f"{label}: {path.name} needs the {module} package, which {argv[0]} cannot import "
                                f"(install it: {argv[0]} -m pip install {module})")
        names = ADAPTER_CREDENTIALS.get(path.name)
        if names and not any(os.environ.get(name) for name in names):
            warnings.append(f"{label}: none of {', '.join(names)} is set; every call fails unless another credential "
                            f"source the SDK reads serves")
    return {"blocking": blocking, "warnings": warnings}


def jev_prices(prices: dict | None) -> dict | None:
    """Jev's price prior per action kind from a contract price table: one nominal call at the mean of the rates."""
    if not prices:
        return None
    costs = [(NOMINAL_CALL[0] * r["input_per_million"] + NOMINAL_CALL[1] * r["output_per_million"]) / 1e6
             for r in prices.values()]
    return {"mutate": round(statistics.mean(costs), 6), "crossover": 0.0, "evaluate": 0.0}


def githits_arm(name: str) -> str:
    """The arm's name as the GitHits client's arm label (``[A-Za-z0-9_-]{1,16}``, ``X-GitHits-Agent``)."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)[:16]


def _hidden(check) -> bool:
    return isinstance(check, dict) and isinstance(check.get("name"), str) and check["name"].startswith(HIDDEN_PREFIX)


def hidden_view(feedback: dict, mode: str) -> dict | None:
    """``feedback`` as the ``hidden_feedback`` ``mode`` lets the proposer see it, or None for no feedback at all."""
    if mode == "tails":
        return feedback
    checks = feedback.get("checks") or []
    if mode == "status":
        return {**feedback, "checks": [{"name": c["name"], "state": "passed" if c.get("state") == "passed" else "failed"}
                                       if _hidden(c) else c for c in checks]}
    if mode == "none":
        visible = [c for c in checks if not _hidden(c)]
        if all(isinstance(c, dict) and c.get("state") == "passed" for c in visible):
            return None
        return {**feedback, "checks": visible}
    raise Rejected("invalid_spec", f"hidden_feedback must be one of tails, status, none, not {mode!r}")


def feedback_filter(mode: str, sent: list | None = None):
    """A provider's ``feedback_filter`` for ``mode``; each result, as sent, is appended to ``sent``."""
    def apply(feedback: dict) -> dict | None:
        shown = hidden_view(feedback, mode)
        if sent is not None:
            sent.append({"candidate": feedback.get("candidate"), "sent": shown is not None,
                         "feedback": None if shown is None else json.loads(json.dumps(shown))})
        return shown
    return apply


def build(arm: dict, *, repo: Path, budget: dict, prices: dict | None, harness: Path = HARNESS,
          thread_label: str | None = None, hidden_feedback: str = "tails", sent: list | None = None):
    """The provider for one run of ``arm`` against the repository at ``repo``. ``thread_label`` (the run's thread
    id) and the arm's name label a ``githits`` arm's lookups, so each run is its own GitHits session. A ``repair`` or
    ``claude-code`` provider filters its feedback through ``hidden_feedback`` and records each one it sends in
    ``sent``."""
    kind, k = arm["kind"], calls(arm, budget)
    timeout = arm.get("timeout_seconds", 600)
    name = arm["name"] if arm["name"][0].isalpha() else "arm-" + arm["name"]
    if kind == "single":
        from syberlabs.providers import CommandProvider
        return CommandProvider(resolve(arm["adapter"], harness), name=name, revision="1", timeout=timeout, cwd=repo)
    if kind == "repair":
        from syberlabs.providers import RepairProvider
        return RepairProvider(resolve(arm["adapter"], harness), name=name, revision="1", timeout=timeout, cwd=repo,
                              max_calls=k, feedback_filter=feedback_filter(hidden_feedback, sent))
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
        options["githits"] = arm["githits"]
        if arm["githits"]:
            options.update(arm=githits_arm(arm["name"]), thread_label=thread_label)
    provider = ClaudeCodeProvider(repo, **options)
    provider.feedback_filter = feedback_filter(hidden_feedback, sent)  # the RepairProvider hook it inherits
    return provider


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
