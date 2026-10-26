"""The GitHits pilot's dry run as one command: preflight, canary, both arms per item, the paired report.

    python scripts/pilot_dry_run.py --template > plan.json     # a plan to fill in
    python scripts/pilot_dry_run.py --plan plan.json            # preflight, confirm, canary, runs, review, report

It runs the documented pilot commands (``docs/EXPERIMENTS.md``, the GitHits pilot) against a home of its own, on the
operator's machine, under the operator's own credentials. Running it spends money (Class X, ``AGENTS.md``): the
operator executes it; nothing here was run against a real model or the real GitHits API.

How Mateo runs it
-----------------

1. Check out the base commit the items start from, in the repository they change (``git checkout <commit>``). Every
   thread starts from that ``HEAD``; the script refuses to start if ``--base`` is given and ``HEAD`` is elsewhere,
   and stops if ``HEAD`` moves during the run.
2. Put both credentials in the shell's environment, never in a file::

       export ANTHROPIC_API_KEY=...          # your own key: the api lane (claude -p --bare)
       export GITHITS_API_TOKEN=...
       unset ANTHROPIC_BASE_URL              # and any CLAUDE_CODE_USE_* switch: the api lane refuses them

   (PowerShell: ``$env:ANTHROPIC_API_KEY = "..."``; ``Remove-Item Env:ANTHROPIC_BASE_URL``.)
3. Write the plan: ``python scripts/pilot_dry_run.py --template > plan.json``, then fill in 3 to 5 items (id, tags,
   objective, paths) and the four rates of the model your Claude Code runs: input, output, cache read and cache
   write, in dollars per million tokens (the canary names the model if it is not the one priced).
4. Run ``python scripts/pilot_dry_run.py --plan plan.json`` from the SyberWork checkout (Windows:
   ``py scripts/pilot_dry_run.py --plan plan.json``; ``--repo PATH`` when the items change another repository;
   ``--budget 8`` is the default scheduling budget in dollars; ``--seed N`` repeats an order). It prints the preflight, asks to
   start, runs the canary and then every run, asks for the blind review of each accepted change (shown as R1, R2, ...
   in a shuffled order, without its arm: hand the keyboard to the reviewer, or answer yourself without looking up the
   mapping), prints the paired report and the PASS/FAIL table, and writes ``receipt.json``. Exit 0 is PASS, 1 FAIL,
   2 refused before anything was spent (a preflight failure, or no go).
5. Send ``receipt.json`` and ``report.md`` from the run directory (printed at the end). The journal, the diffs and
   ``commands.log`` stay on the machine: they hold the items' objectives and code.

The plan (JSON): ``items``, 3 to 5 of ``{id, tags, objective, paths}``; ``prices``, dollars per million tokens per
model (``evolution.budget.prices``) with all four rates above 0 (input, output, cache read, cache creation: a cache
rate left out would be priced at the input rate, one at 0 as free), or instead a whole ``contract`` that holds such
prices, ``max_dollars`` and ``strict``; and what both arms share: ``max_calls`` (3: one shot, two repairs),
``max_turns`` (30, Claude Code's ``--max-turns``), ``allowed_tools`` (the pilot's, ``docs/GITHITS.md``:
``Read,Edit,Write,Glob,Grep,WebSearch,WebFetch``; no shell), ``guard_max_lines`` and ``guard_max_chars``, ``checks``
(the derived contract's checks; default: those ``syberlabs init`` detects) and ``canary_paths``.

The sequence
------------

Preflight refuses to start on any failure: the repository and its base commit; Claude Code ``2.1.295`` or newer
(``claude_code.MIN_VERSION``: ``onFailure: "block"`` on the guard's hook); the api lane on the operator's own
``ANTHROPIC_API_KEY`` with no third-party routing switch (``claude_code.third_party``: ``ANTHROPIC_BASE_URL``,
``CLAUDE_CODE_USE_*``, ``CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST``); ``GITHITS_API_TOKEN`` in the environment and in no
file the run touches (the repository's tracked and untracked files, its ``.env*``, ``.mcp.json`` and local Claude
settings, the plan); ``syberlabs githits doctor`` ``ok``, with its call record verifying under a key the host set;
the query guard's key reaching the hook (``CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`` off, and the host's own hook, run as
Claude Code runs it, signing a block and a pass that verify under a key the host set); the scheduling budget; and the
contracts the run installs.

The canary is one cheap call in the treatment's mode (``propose --claude-code --bare --githits``, one call, a few
turns, without the web tools so the launcher is its only way to look anything up) whose objective asks for two
launcher lookups, one the guard must refuse (it holds an email address). It passes when the guard refused a lookup
for what it says and passed one that GitHits answered (an authentic client record), every decision verified and none
``unguarded``, and every model the call used is priced; a call that shows only one of the two is inconclusive and
fails, since it proves either no refusal or no GitHits. Then each item runs in both arms from the same ``HEAD``,
both arms back to back, the items and which arm goes first set by a seeded shuffle: ``start --unattended``,
``experiment label`` (before the first candidate), ``propose --claude-code --bare [--githits]``, ``run --once``
(accepts on the host's checks), ``export``. Then the blind review (``review --record``), ``experiment report --items
FILE --delivered review [--json | --markdown]``, and the bar.

The bar: the nine metrics known for every thread
------------------------------------------------

Cost, turns, agent time, fix rounds, web lookups, GitHits lookups, delivered, review rejections and human
interventions (``metrics.METRICS``), per thread as the paired report reads them (``experiment.Run.value``: counted
up to the thread's first acceptance). Two of the nine are read the way the report pairs them. Agent time is the
report's time to a passing change (``time_to_pass``): its merge-based form needs a merged pull request, which a dry
run never has and the pilot has for at most one arm per item. Delivered is ``reviewed``, the pilot's definition
(accepted, and the blind review passed: ``--delivered review``): its merge-based form is known in a dry run but is
"no" for every thread, which measures nothing. The merge-based values are in the receipt beside them (``strict``);
``syberlabs settle`` and agent time to merge, the pilot's end-of-week step, are not exercised. A thread that is not
accepted has no time to a passing change, so the bar fails, and the reasons say why it was not accepted.

Budget
------

``--budget`` (default $8) is the cap for the whole run, canary included. Each thread's contract ``max_dollars`` is
an equal share of it (``--thread-budget``; default the cap over the runs plus the canary), so the host's own meter
stops a thread's next call past its share and its promotion is refused; and a run starts only while the dollars
already spent plus one share fit under the cap. Every call is bounded by the plan's ``max_turns``. So the total
stays under the cap plus the cost of one call, and the receipt says what it was. A call that ended
``provider_failed``, ``provider_timeout`` or ``provider_error`` after Claude Code ran was never priced (it raised
before the meter), so its thread's cost and the total are unknown and no further run starts. Spend is the host's
own: tokens priced at the plan's ``prices`` through the host's meter (``docs/METRICS.md``), never Claude Code's
estimate. A fee the API charges per server-side web search is not a token, so neither the meter nor the cap sees it;
each thread's web lookups in the receipt say how many searches there were.

The paired report
-----------------

It must compare every item: an item the report excludes (``experiment.EXCLUDE``: ``arm_conflict``,
``contaminated``, ``missing_arm``, ...) or a registered item it never saw fails the run, and a check that keeps an
item but says something about it (``unused``, ``use_unknown``, ``two_routes``, ...) is printed as a warning.
The report reads the treatment's GitHits from the host's launcher ledger (``signal.githits``, SyberWork #141, branch
``fix/experiment-report-githits-api-arm``); a report without that change excludes every item as ``arm_conflict``,
and the run fails.

Standard library only; nothing in ``syberlabs`` is changed. Tests: ``tests/test_pilot_dry_run.py`` (a stand-in
``claude`` and a loopback GitHits; no model, no network).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

ROOT = Path(__file__).resolve().parents[1]  # the SyberWork checkout this script belongs to: the host's code
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from syberlabs import Rejected, claude_code, experiment, githits, metrics, query_guard  # noqa: E402
from syberlabs.build import default_contract  # noqa: E402
from syberlabs.economic import check_prices  # noqa: E402

RECEIPT = "syberlabs.pilot-dry-run/1"
DEFAULT_BUDGET = 8.0
DEFAULT_EXPERIMENT = "githits-dry-run"
DEFAULT_MAX_CALLS = 3        # one shot and two repairs per thread
DEFAULT_MAX_TURNS = 30       # Claude Code's agentic turns per call: bounds a call's cost
DEFAULT_MAX_SECONDS = 3600   # per search: a call, its repairs and the checks
DEFAULT_REVIEWER = "blind-1"
MIN_ITEMS, MAX_ITEMS = 3, 5
# Both arms' tools in the pilot's documented commands (docs/GITHITS.md, "The A/B report"); the treatment adds its
# launcher. No shell: a shell could read the guard's key (claude_code.DEFAULT_TOOLS).
WEB_TOOLS = ("WebSearch", "WebFetch")
PILOT_TOOLS = (*claude_code.DEFAULT_TOOLS, *WEB_TOOLS)
# Every rate of a priced model: Claude Code's calls are mostly cache reads and writes, and a cache rate left out is
# priced at the input rate (economic.price_usage), one at 0 makes those tokens free. Either way the cost is wrong.
RATES = ("input_per_million", "output_per_million", "cache_read_per_million", "cache_creation_per_million")
# claude_code raises the first two after Claude Code ran and before the host's meter priced the call, and the search
# records an unexpected fault there as the third: what such a call spent is lost.
UNMETERED = ("provider_failed", "provider_timeout", "provider_error")
# The guard's rules that judge what a lookup says. Its others refuse a command that is not the launcher, or report the
# guard's own failure: a refusal under them shows neither that a content rule fires nor that Claude Code obeys a refused
# launcher call (under dontAsk it would not run a command outside its allowed tools anyway).
CONTENT_RULES = tuple(rule for rule in query_guard.RULES
                      if rule not in (query_guard.NOT_LAUNCHER, query_guard.SHELL_SYNTAX, query_guard.ERROR))
REACHED = (githits.OK, githits.BLOCKED)  # a client record of a request GitHits answered
BASELINE, TREATMENT = experiment.BASELINE, experiment.TREATMENT  # "off" and "on", the report's defaults
CANARY_CONTRACT = "pilot-canary"
CANARY_TURNS = 6
NINE = ("cost", "turns", "agent_time", "fix_rounds", "web_lookups", "githits_lookups", "delivered",
        "review_rejections", "human_interventions")  # metrics.METRICS, in its order
PAIRED = {"agent_time": "time_to_pass", "delivered": "reviewed"}  # read as the paired report reads them
LABELS = {"cost": "cost", "turns": "turns", "agent_time": "agent time", "fix_rounds": "fix", "web_lookups": "web",
          "githits_lookups": "githits", "delivered": "delivered", "review_rejections": "rejections",
          "human_interventions": "interventions"}
PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
SCRUB_ENV = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
API_KEY_ENV = "ANTHROPIC_API_KEY"
# Files beside the code where a credential tends to be written; scanned whether or not git ignores them.
SECRET_FILES = (".env", ".mcp.json", ".claude/settings.json", ".claude/settings.local.json", "CLAUDE.local.md")
MAX_SCAN_BYTES = 20 * 1024 * 1024
MIN_SECRET_CHARS = 12  # a credential shorter than this is not searched for in the run's files (no false alarms)
PLAN_KEYS = ("experiment", "items", "prices", "contract", "checks", "allowed_tools", "max_calls", "max_turns",
             "guard_max_lines", "guard_max_chars", "canary_paths")
ITEM_KEYS = ("id", "tags", "objective", "paths")
CANARY_TARGET = "pypi:requests@2.32.3"
CANARY_BLOCKED = f'search "guard canary contact canary@example.invalid" --target {CANARY_TARGET}'
CANARY_PASSING = f'search "Session mount HTTPAdapter max_retries" --target {CANARY_TARGET}'
CANARY_OBJECTIVE = (
    "Query-guard canary, the operator's test of this machine's lookup filter. No code change is wanted: do not "
    "create, edit or delete any file. Using your Bash tool, run the GitHits launcher named in your instructions "
    f"exactly twice and then stop. Call 1 passes the arguments `{CANARY_BLOCKED}`: the filter is expected to refuse "
    "it because it holds an email address, and that refusal is the point of the test, so do not rephrase or retry "
    f"it. Call 2 passes the arguments `{CANARY_PASSING}`.")
TEMPLATE = {
    "_comment": "3 to 5 items. Keys starting with _ are ignored. Objectives stay on this machine: the receipt "
                "holds their digests only. Prices: the model id Claude Code reports, and all four of its rates in "
                "dollars per million tokens, none 0 (Claude Code's calls are mostly cache reads and writes).",
    "experiment": DEFAULT_EXPERIMENT,
    "items": [
        {"id": "LIB-1", "tags": ["needs_lookup"], "objective": "One sentence: the change this item asks for",
         "paths": ["syberlabs", "tests"]},
        {"id": "LIB-2", "tags": ["needs_lookup"], "objective": "Another change", "paths": ["syberlabs", "tests"]},
        {"id": "RT-1", "tags": ["routine"], "objective": "A routine change", "paths": ["syberlabs", "tests"]},
    ],
    "prices": {"<the model id Claude Code reports>": {"input_per_million": 0, "output_per_million": 0,
                                                     "cache_read_per_million": 0, "cache_creation_per_million": 0}},
    "max_calls": DEFAULT_MAX_CALLS,
    "max_turns": DEFAULT_MAX_TURNS,
    "allowed_tools": ",".join(PILOT_TOOLS),
}


class PlanError(ValueError):
    """The plan cannot be run as written; the message says why."""


class Done(NamedTuple):
    code: int
    out: str
    err: str


Runner = Callable[[list, dict, float], Done]
Ask = Callable[[str], str]


def say(text: str = "") -> None:
    print(text, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(value) -> str:
    """SHA-256 of a string, or of a JSON value in a fixed form."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truthy(value) -> bool:
    """Set, as Claude Code reads a switch: anything but empty, 0, false, no and off."""
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def tail(text: str, limit: int = 300) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else "..." + flat[-limit:]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {tail(done.stderr.decode('utf-8', 'replace'), 200)}")
    return done.stdout.decode("utf-8", "replace").strip()


# ------------------------------------------------------------------------------------------------------------- plan

def _name(value, what: str) -> str:
    if not isinstance(value, str) or not experiment._NAME.match(value):
        raise PlanError(f"{what} must be a short name: letters, digits, _ . : + - and at most 64 characters")
    return value


def relative_path(path) -> bool:
    """A repository path the plan may name: relative, no drive, no ``..`` part (either separator, for Windows)."""
    return (isinstance(path, str) and bool(path.strip()) and not path.startswith(("/", "\\")) and ":" not in path
            and ".." not in re.split(r"[\\/]", path))


def file_name(text: str) -> str:
    """``text`` as part of a file name on any system: an item id may hold ``:``, an NTFS stream separator."""
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", text)


def missing_rates(prices) -> str | None:
    """Why a price table cannot price a Claude Code call in full, or None: every model needs a real id and all four
    rates (``RATES``) above 0."""
    for model, rates in (prices or {}).items():
        if "<" in model:
            return f"fill in the model id Claude Code reports, not {model!r}"
        zero = [rate for rate in RATES if not (isinstance(rates.get(rate), (int, float))
                                              and not isinstance(rates.get(rate), bool) and rates[rate] > 0)]
        if zero:
            return (f"fill in {model}'s {', '.join(zero)} (dollars per million tokens, above 0): Claude Code's calls "
                    "are mostly cache reads and writes, and a cache rate left out is priced at the input rate, one "
                    "at 0 as free")
    return None


def _count(raw: dict, key: str, low: int, high: int, default):
    value = raw.get(key, default)
    if value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise PlanError(f"{key} must be a whole number from {low} to {high}")
    return value


def validate_plan(raw) -> dict:
    """The plan, normalized. 3 to 5 items, each ``{id, tags, objective, paths}``; ``prices`` (or a whole ``contract``
    that has them); the knobs both arms share."""
    if not isinstance(raw, dict):
        raise PlanError("the plan must be a JSON object")
    unknown = sorted(key for key in raw if not str(key).startswith("_") and key not in PLAN_KEYS)
    if unknown:
        raise PlanError("unknown plan keys: " + ", ".join(unknown))
    name = _name(raw.get("experiment", DEFAULT_EXPERIMENT), "experiment")
    items = raw.get("items")
    if not isinstance(items, list) or not MIN_ITEMS <= len(items) <= MAX_ITEMS:
        raise PlanError(f"items must list {MIN_ITEMS} to {MAX_ITEMS} work items (a dry run, not the pilot)")
    found, objectives = [], set()
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) - set(ITEM_KEYS) or not {"id", "objective"} <= set(item):
            raise PlanError(f"item {number} must be an object with id and objective, and optionally tags and paths")
        item_id = _name(item["id"], f"item {number}'s id")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or len(tags) > experiment.MAX_TAGS:
            raise PlanError(f"item {item_id}: tags must be a list of at most {experiment.MAX_TAGS} names")
        tags = sorted(set(_name(tag, f"item {item_id}'s tag") for tag in tags))
        objective = item["objective"]
        if (not isinstance(objective, str) or not objective.strip() or len(objective) > 4000
                or objective.strip().startswith("-")):
            raise PlanError(f"item {item_id}: objective must be a sentence of at most 4000 characters, not starting "
                            "with '-'")
        if objective.strip() in objectives:
            raise PlanError(f"item {item_id}: another item has the same objective")
        objectives.add(objective.strip())
        paths = item.get("paths", [])
        if not isinstance(paths, list) or not all(relative_path(p) for p in paths):
            raise PlanError(f"item {item_id}: paths must be relative repository paths")
        found.append({"id": item_id, "tags": tags, "objective": objective.strip(), "paths": list(paths)})
    if len({item["id"] for item in found}) != len(found):
        raise PlanError("item ids must be distinct")
    try:
        experiment.parse_items(items_text(found, name))
    except ValueError as exc:
        raise PlanError(f"the registered item list: {exc}") from None
    contract, prices = raw.get("contract"), raw.get("prices")
    if contract is not None and prices is not None:
        raise PlanError("give prices or a whole contract (whose budget holds its prices), not both")
    if contract is None:
        reason = check_prices(prices) if prices is not None else "prices are required: the cost metric needs them"
        reason = reason or missing_rates(prices)
        if reason:
            raise PlanError(f"prices: {reason}")
    elif not isinstance(contract, dict) or not isinstance(contract.get("evolution"), dict):
        raise PlanError("contract must be a whole contract document with an evolution section")
    checks = raw.get("checks")
    if checks is not None and (contract is not None or not isinstance(checks, dict) or not checks):
        raise PlanError("checks must be a non-empty object of named checks, and only without a whole contract")
    tools = raw.get("allowed_tools", ",".join(PILOT_TOOLS))
    tools = claude_code.parse_tools(tools) if isinstance(tools, str) else tools
    if not isinstance(tools, list) or not tools or any(not isinstance(t, str) or not t for t in tools):
        raise PlanError("allowed_tools must be a comma list of tool names")
    if claude_code.code_tools(tools):
        raise PlanError("allowed_tools: no Bash or PowerShell; the treatment's launcher is its only command")
    canary_paths = raw.get("canary_paths")
    if canary_paths is not None and (not isinstance(canary_paths, list)
                                     or not all(relative_path(p) for p in canary_paths)):
        raise PlanError("canary_paths must be a list of relative repository paths")
    return {"experiment": name, "items": found, "prices": prices, "contract": contract, "checks": checks,
            "allowed_tools": ",".join(tools), "max_calls": _count(raw, "max_calls", 1, 12, DEFAULT_MAX_CALLS),
            "max_turns": _count(raw, "max_turns", 1, 200, DEFAULT_MAX_TURNS),
            "guard_max_lines": _count(raw, "guard_max_lines", 1, 100, None),
            "guard_max_chars": _count(raw, "guard_max_chars", 1, 100_000, None), "canary_paths": canary_paths}


def load_plan(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"{path}: {exc.strerror or type(exc).__name__}") from None
    except ValueError as exc:
        raise PlanError(f"{path}: not JSON ({exc})") from None
    return validate_plan(raw)


def items_text(items: list, name: str) -> str:
    """The registered item list (``experiment report --items``), written before the first run."""
    lines = [f"# {name}: registered by scripts/pilot_dry_run.py before the first run"]
    lines += [" ".join([item["id"], *item["tags"]]) for item in items]
    return "\n".join(lines) + "\n"


def build_contracts(plan: dict, repo: Path, thread_budget: float) -> tuple[dict, dict]:
    """The arms' contract and the canary's. Without a whole contract in the plan, the repository's default contract
    (``build.default_contract``: its detected checks, or the plan's) with the plan's prices, ``max_dollars`` the
    thread's share of the cap, ``strict`` on (a model the prices do not name refuses promotion), and one hour per
    search. The canary's is the same with one trivial check: it is never accepted, and nothing it writes is judged."""
    if plan["contract"] is not None:
        main = copy.deepcopy(plan["contract"])
    else:
        main = default_contract(repo)
        if plan["checks"] is not None:
            main["evolution"]["evaluation"]["checks"] = copy.deepcopy(plan["checks"])
            main["evolution"]["evaluation"]["required"] = sorted(plan["checks"])
        main["evolution"]["budget"] = {"max_candidates": 12, "max_evaluations": 24, "max_seconds": DEFAULT_MAX_SECONDS,
                                       "prices": copy.deepcopy(plan["prices"]), "max_dollars": thread_budget,
                                       "strict": True}
    canary = copy.deepcopy(main)
    canary.update({"id": CANARY_CONTRACT, "version": 1, "title": "Query-guard canary (never accepted)"})
    canary["evolution"]["evaluation"] = {"checks": {"canary": {"argv": [sys.executable, "-c", "pass"],
                                                               "timeout_seconds": 60}},
                                         "required": ["canary"], "max_age_seconds": 3600}
    budget = canary["evolution"]["budget"]
    budget.update({"max_candidates": 2, "max_evaluations": 2, "max_seconds": min(budget.get("max_seconds", 900), 900)})
    return main, canary


def contract_name(doc: dict) -> str:
    return f"{doc['id']}.v{doc['version']}"


def schedule(items: list[str], seed: int, baseline: str = BASELINE, treatment: str = TREATMENT) -> list[tuple]:
    """Both arms of every item, back to back, so drift during the run falls on both arms of an item alike. A seeded
    shuffle sets the order of the items and which arm runs first; the first arms are balanced (as many items start
    with one arm as with the other, give or take one)."""
    rng = random.Random(seed)
    order = list(items)
    rng.shuffle(order)
    firsts = ([baseline, treatment] * len(order))[:len(order)]
    rng.shuffle(firsts)
    runs = []
    for item, first in zip(order, firsts):
        runs += [(item, first), (item, treatment if first == baseline else baseline)]
    return runs


# --------------------------------------------------------------------------------------------- the syberlabs CLI

def subprocess_runner(argv: list, env: dict | None = None, timeout: float | None = None) -> Done:
    """``python -m syberlabs ARGV`` with this interpreter, from the host checkout (so its ``syberlabs`` runs): what
    the operator would type. UTF-8 output, so a diff decodes the same on Windows."""
    done = subprocess.run([sys.executable, "-m", "syberlabs", *argv], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}, capture_output=True,
                          timeout=timeout)
    return Done(done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))


class Syberlabs:
    """The ``syberlabs`` commands of the run, against its own home (``<run>/home``). Every command is logged to
    ``commands.log`` with its output (local only: it holds the objectives). The receipt keeps each command's argv with
    every objective as a digest and the run's directory as ``<run>``, so it can travel without them."""

    def __init__(self, home: Path, repo: Path, actor: str, runner: Runner, log: Path | None = None):
        self.home, self.repo, self.actor, self.runner, self.log = home, repo, actor, runner, log
        self.commands: list[dict] = []
        self.private: set[str] = set()  # texts the receipt holds only as digests (objectives)

    def shown(self, arg: str) -> str:
        if arg in self.private:
            return f"<objective sha256:{digest(arg)[:16]}>"
        return arg.replace(str(self.home.parent), "<run>")

    def __call__(self, *args: str, env: dict | None = None, timeout: float | None = 900) -> Done:
        args = tuple(map(str, args))
        argv = ["--home", str(self.home), "--repo", str(self.repo), "--actor", self.actor, *args]
        try:
            done = self.runner(argv, env or {}, timeout)
        except subprocess.TimeoutExpired:
            done = Done(124, "", f"timed out after {timeout} seconds")
        self.commands.append({"argv": [self.shown(arg) for arg in args], "code": done.code})
        if self.log is not None:
            with open(self.log, "a", encoding="utf-8") as handle:
                handle.write(f"$ syberlabs {' '.join(args)}\n[exit {done.code}]\n{done.out}{done.err}\n")
        return done

    def current(self) -> str:
        return (self.home / "current").read_text(encoding="utf-8").strip()

    def export(self, thread: str, path: Path) -> dict | None:
        done = self("--thread", thread, "export", "--out", str(path))
        if done.code != 0 or not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


# -------------------------------------------------------------------------------------------------------- preflight

class Check(NamedTuple):
    name: str
    status: str
    detail: str
    data: dict | None = None  # what the receipt keeps beside the line (the base commit, the version read)

    def value(self, key: str):
        return (self.data or {}).get(key)


def check_repository(repo: Path, base: str | None) -> Check:
    try:
        head = git(repo, "rev-parse", "--verify", "HEAD")
    except (RuntimeError, OSError) as exc:
        return Check("repository", FAIL, f"{repo} is not a git repository with a commit ({exc})")
    if base:
        try:
            wanted = git(repo, "rev-parse", "--verify", base + "^{commit}")
        except RuntimeError:
            return Check("repository", FAIL, f"--base {base} is not a commit of {repo}")
        if wanted != head:
            return Check("repository", FAIL, f"HEAD is {head[:10]}, not the base {wanted[:10]}: check out the base "
                                             f"first (git checkout {wanted[:10]})")
    host = host_checkout()
    note = ""
    if host["commit"]:
        note = f"; host checkout {host['commit'][:10]}" + (" with local changes" if host["dirty"] else "")
    return Check("repository", PASS, f"base {head[:10]} (HEAD){note}", {"base": head, "host": host})


def host_checkout() -> dict:
    """The SyberWork checkout running this script: its commit and whether it has local changes."""
    try:
        return {"commit": git(ROOT, "rev-parse", "HEAD"), "dirty": bool(git(ROOT, "status", "--porcelain"))}
    except (RuntimeError, OSError):
        return {"commit": None, "dirty": None}


def check_claude() -> Check:
    try:
        argv = claude_code.claude_executable()
    except Rejected as exc:
        return Check("claude code", FAIL, exc.detail)
    floor = ".".join(map(str, claude_code.MIN_VERSION))
    try:
        done = subprocess.run([*argv, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("claude code", FAIL, f"`claude --version` did not answer ({type(exc).__name__})")
    found = claude_code.version_tuple(done.stdout.decode("utf-8", "replace")) if done.returncode == 0 else None
    if found is None:
        return Check("claude code", FAIL, f"could not read the version from `claude --version` (expected the whole "
                                          f"output to be X.Y.Z (Claude Code)); {floor} or later is required")
    version = ".".join(map(str, found))
    if found < claude_code.MIN_VERSION:
        return Check("claude code", FAIL, f"{version} is older than {floor}, the version that honours onFailure: "
                                          "\"block\" on the query guard's hook", {"version": version})
    return Check("claude code", PASS, f"{version} (floor {floor})", {"version": version})


def check_lane(env) -> Check:
    problems = []
    switches = claude_code.third_party(env)
    if switches:
        problems.append(", ".join(switches) + " set: a --bare run would go to another endpoint or provider, not "
                        "Anthropic's API under your key; unset it (this dry run runs the api lane only)")
    if not str(env.get(API_KEY_ENV, "")).strip():
        problems.append(f"{API_KEY_ENV} is not set: the api lane (--bare) runs on your own key from the environment")
    if problems:
        return Check("credential lane", FAIL, "; ".join(problems))
    return Check("credential lane", PASS, f"api (--bare, billed) on {API_KEY_ENV} from this environment; no "
                                          "third-party routing switch set", {"lane": "api", "cost_basis": "billed"})


def scan_paths(repo: Path, extra: list) -> list[Path]:
    """Where the token must not be: every tracked and untracked (not ignored) file of the repository, the usual
    credential files beside it and beside the host checkout whether ignored or not, and ``extra``."""
    paths = set()
    try:
        names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
        paths |= {repo / name for name in names if name}
    except (RuntimeError, OSError):
        pass
    for root in {repo.resolve(), ROOT}:
        paths |= {root / name for name in SECRET_FILES} | set(root.glob(".env.*"))
    paths |= {Path(p) for p in extra}
    return sorted(p for p in paths if p.is_file())


def holds(path: Path, needle: bytes, block: int = 1 << 20) -> bool:
    """Whether the file's bytes hold ``needle``, read a block at a time (a match across two blocks included)."""
    keep, carry = len(needle) - 1, b""
    with open(path, "rb") as handle:
        while True:
            data = handle.read(block)
            if not data:
                return False
            data = carry + data
            if needle in data:
                return True
            carry = data[-keep:] if keep else b""


def files_holding(secret: str, paths, limit: int | None = MAX_SCAN_BYTES) -> tuple[list[Path], int, int]:
    """The files among ``paths`` whose bytes hold ``secret``, how many were read, and how many were not (larger than
    ``limit``, or unreadable). The secret is never printed."""
    needle, found, read, skipped = secret.encode("utf-8"), [], 0, 0
    for path in paths:
        try:
            if limit is not None and path.stat().st_size > limit:
                skipped += 1
                continue
            hit = holds(path, needle)
        except OSError:
            skipped += 1
            continue
        read += 1
        if hit:
            found.append(path)
    return found, read, skipped


def check_token(env, repo: Path, plan_path: Path | None) -> Check:
    token = str(env.get(githits.TOKEN_ENV, ""))
    if not token:
        return Check("githits token", FAIL, f"{githits.TOKEN_ENV} is not set in the environment")
    if not token.isprintable() or any(ch.isspace() for ch in token):
        return Check("githits token", FAIL, f"{githits.TOKEN_ENV} holds whitespace or a control character")
    found, read, skipped = files_holding(token, scan_paths(repo, [plan_path] if plan_path else []))
    if found:
        names = ", ".join(str(p) for p in found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else "")
        return Check("githits token", FAIL, f"the token is written in {names}: remove it there and keep it in the "
                                            "environment only")
    unread = f"; {skipped} not read (over {MAX_SCAN_BYTES >> 20} MB or unreadable)" if skipped else ""
    return Check("githits token", PASS, f"in the environment only: in none of the {read} files scanned (the "
                                        "repository's tracked and untracked files, .env*, .mcp.json, local Claude "
                                        f"settings, the plan){unread}",
                 {"files_scanned": read, "files_unread": skipped})


def check_doctor(sy: Syberlabs) -> Check:
    """``syberlabs githits doctor --json`` with records on, under a key the host draws: ``ok``, and exactly one
    authentic ``doctor`` record."""
    folder = Path(tempfile.mkdtemp(prefix="syberlabs-doctor-"))
    key = secrets.token_hex(32)
    try:
        done = sy("githits", "doctor", "--json", env={githits.RECORD_DIR_ENV: str(folder),
                                                       githits.RECORD_KEY_ENV: key}, timeout=300)
        try:
            report = json.loads(done.out.strip().splitlines()[-1]) if done.out.strip() else {}
        except ValueError:
            report = {}
        records = githits.read_records(folder, key)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    report = report if isinstance(report, dict) else {}
    data = {k: report.get(k) for k in ("status", "http_status", "request_id", "latency_ms", "base_url",
                                       "rate_limit_remaining")}
    if done.code != 0 or report.get("status") != "ok":
        why = report.get("problem") or tail(done.err, 160) or "no answer"
        return Check("githits doctor", FAIL, f"{report.get('status') or 'error'} ({why})", data)
    if [r.get("op") for r in records.records] != ["doctor"] or records.ignored:
        return Check("githits doctor", FAIL, "the doctor's call record did not verify under the key the host set "
                                             f"({len(records.records)} authentic, {records.ignored} ignored)", data)
    return Check("githits doctor", PASS, f"ok (HTTP {report.get('http_status')}, {report.get('latency_ms')} ms, base "
                                         f"{report.get('base_url')}); its record verified under a key the host set",
                 data)


def check_guard_key(env) -> Check:
    """The query guard's per-call key reaches its hook: Claude Code's subprocess scrub is off, and the host's own hook
    (``query_guard.hook_command``, the exec form Claude Code runs) signs a block and a pass under a key the host put in
    its environment, both verifying. Claude Code itself running the hook under ``--bare`` is the canary's to show."""
    problems = []
    if truthy(env.get(SCRUB_ENV)):
        problems.append(f"{SCRUB_ENV} is set: Claude Code would strip the guard's key and the GitHits token from the "
                        "hook's and the launcher's environment, so every lookup would be blocked unguarded")
    directory = Path(tempfile.mkdtemp(prefix="syberlabs-guard-check-"))
    launcher = "/syberlabs-preflight/githits"
    expected = {"preflight-block": (query_guard.NOT_LAUNCHER, query_guard.BLOCK_EXIT),
                "preflight-pass": (query_guard.PASS, 0)}
    calls = {"preflight-block": {"command": "echo guard-canary"},
             "preflight-pass": {"command": f'{launcher} search "json loads parse_float" --target {CANARY_TARGET}'}}
    codes = {}
    try:
        query_guard.prepare(directory, query_guard.policy(), set(), bash=True)
        key = secrets.token_hex(32)
        hook = query_guard.hook_command(directory)
        hook_env = {**os.environ, **query_guard.hook_environment(directory, key, launcher)}
        for use_id, tool_input in calls.items():
            payload = {"session_id": "preflight", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": tool_input, "tool_use_id": use_id}
            try:
                done = subprocess.run([hook["command"], *hook["args"]], input=json.dumps(payload).encode("utf-8"),
                                      capture_output=True, env=hook_env, timeout=60)
                codes[use_id] = done.returncode
            except (OSError, subprocess.TimeoutExpired):
                codes[use_id] = None
        verified = {d["tool_use_id"]: d["rule"] for d in query_guard.read_decisions(directory)
                    if query_guard.authentic(d, key)}
    finally:
        query_guard.remove(directory)
    if {k: (verified.get(k), codes.get(k)) for k in expected} != expected:
        problems.append("the host's query guard hook did not sign its decisions under the key the host set (decisions "
                        f"{verified}, exit codes {codes})")
    if problems:
        return Check("guard key", FAIL, "; ".join(problems))
    return Check("guard key", PASS, f"a key the host set reached the hook, which refused a non-launcher command "
                                    f"({query_guard.NOT_LAUNCHER}) and passed a launcher lookup, both verified; "
                                    f"{SCRUB_ENV} off", {"guard_version": query_guard.VERSION})


def check_budget(cap, contract: dict, runs: int) -> Check:
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not math.isfinite(cap) or cap <= 0:
        return Check("budget cap", FAIL, "--budget must be a positive number of dollars")
    budget = contract.get("evolution", {}).get("budget", {})
    budget = budget if isinstance(budget, dict) else {}
    reason = check_prices(budget.get("prices")) if "prices" in budget else "the contract prices no model"
    reason = reason or missing_rates(budget["prices"])
    if reason:
        return Check("budget cap", FAIL, f"the arms' contract: {reason}: the cost metric needs host-priced usage")
    share = budget.get("max_dollars")
    if share is None:
        return Check("budget cap", FAIL, "the arms' contract sets no budget.max_dollars: the host's meter would not "
                                         "stop a thread")
    if not isinstance(share, (int, float)) or isinstance(share, bool) or not math.isfinite(share) or share <= 0:
        return Check("budget cap", FAIL, "the arms' contract budget.max_dollars must be a positive number of dollars")
    if share > cap:
        return Check("budget cap", FAIL, f"the arms' contract max_dollars ${share:.4f} is above the cap ${cap:.2f}")
    if budget.get("strict") is not True:
        return Check("budget cap", FAIL, "the arms' contract needs budget.strict true: usage of a model its prices do "
                                         "not name must refuse promotion, or cost turns unknown")
    return Check("budget cap", PASS, f"${cap:.2f} hard cap for the canary and {runs} runs; each thread's contract "
                                     f"max_dollars ${share:.4f}; a run starts only while spent + ${share:.4f} <= "
                                     f"${cap:.2f}; priced: {', '.join(sorted(budget['prices']))}",
                 {"cap": cap, "thread_budget": share})


def check_contracts(sy: Syberlabs, main: dict, canary: dict) -> Check:
    folder = sy.home / "contracts"
    folder.mkdir(parents=True, exist_ok=True)
    for doc in (main, canary):
        (folder / f"{contract_name(doc)}.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    done = sy("init")
    if done.code != 0:
        return Check("contracts", FAIL, "the run's contracts were not accepted: " + tail(done.err or done.out, 240))
    checks = sorted(main["evolution"]["evaluation"]["checks"])
    return Check("contracts", PASS, f"{contract_name(main)} (checks: {', '.join(checks)}) and {contract_name(canary)} "
                                    f"installed in the run's own home", {"main": contract_name(main),
                                                                         "main_sha256": digest(main)})


# ----------------------------------------------------------------------------------------------------------- records

def facts(events: list, key: str) -> list[dict]:
    return [v for v in (metrics.fact(e, key) for e in events) if v is not None]


def last_search(events: list) -> dict | None:
    found = [e["body"] for e in events if e["kind"] == "search_finished"]
    return found[-1] if found else None


def registrations(events: list) -> list[dict]:
    return [e["body"] for e in events
            if e["kind"] == "candidate_registered" and e["body"].get("operator") != "migration"]


def contracts_of(records: list[dict]) -> dict:
    found = {}
    for record in records:
        doc = record.get("contract") if isinstance(record, dict) else None
        if isinstance(doc, dict) and "id" in doc and "version" in doc:
            found[(doc["id"], doc["version"])] = doc
    return found


def unmetered(events: list) -> str | None:
    """Why the host's meter missed part of what the threads in ``events`` spent, or None: a call that ended
    ``provider_failed``, ``provider_timeout`` or ``provider_error`` (``UNMETERED``: raised after Claude Code ran and
    before the host's meter priced the call), or a search that never finished (the process died inside it)."""
    threads: dict = {}
    for event in events:
        threads.setdefault(event.get("case_id"), []).append(event)
    for found in threads.values():
        searching = False
        for event in sorted(found, key=lambda e: e.get("seq", 0)):
            if event["kind"] == "search_started":
                searching = True
            elif event["kind"] == "search_finished":
                searching = False
                if event["body"].get("error") in UNMETERED:
                    return (f"a call ended {event['body']['error']} after Claude Code ran, before the host's meter "
                            "priced it")
        if searching:
            return "a search never finished, so the host's meter never settled its calls"
    return None


def spend_of(events: list, contracts: dict) -> float | None:
    """Host-observed dollars over the threads in ``events``: a thread whose calls crossed the meter at a model the
    prices name counts its dollars, and one where nothing crossed it counts nothing. A model the prices do not name,
    or a call the meter missed (``unmetered``), makes the whole sum unknown."""
    if unmetered(events):
        return None
    total = 0.0
    for item in metrics.work_items(events, contracts):
        if item.cost_basis == "host_observed":
            if not item.cost.known:
                return None
            total += float(item.cost.value)
        elif item.cost_basis == "declared":
            return None
    return round(total, 6)


def harness_of(events: list) -> dict:
    """What the registrations' signals say about the harness and the lane (host-recorded, never the model's)."""
    found: dict = {}
    for body in registrations(events):
        signal = body.get("signal") if isinstance(body.get("signal"), dict) else {}
        harness = signal.get("harness") if isinstance(signal.get("harness"), dict) else {}
        for key, value in (("lane", signal.get("lane")), ("cost_basis", signal.get("cost_basis")),
                           ("version", harness.get("version")), ("installed_version", harness.get("installed_version")),
                           ("model", harness.get("model")), ("auth_source", harness.get("auth_source"))):
            if isinstance(value, str):
                found.setdefault(key, set()).add(value)
        ledger = signal.get("githits")
        if isinstance(ledger, dict):
            found.setdefault("githits_route", set()).add("api")
            if isinstance(ledger.get("license_policy"), str):  # a digest: part of the frozen setup
                found.setdefault("license_policy", set()).add(ledger["license_policy"])
        for entry in harness.get("mcp_servers") or []:
            if isinstance(entry, str) and entry.startswith("githits:"):
                found.setdefault("githits_mcp", set()).add(entry)
    return {key: sorted(values) for key, values in found.items()}


# ----------------------------------------------------------------------------------------------------------- canary

def claude_flags(plan: dict, arm: str, max_calls: int, max_turns: int, tools: str | None = None) -> list[str]:
    """``propose`` flags: the same in both arms but for ``--githits`` (and its arm name) in the treatment."""
    flags = ["propose", "--claude-code", "--bare", "--max-calls", str(max_calls), "--max-turns", str(max_turns),
             "--allowed-tools", tools or plan["allowed_tools"]]
    if plan["guard_max_lines"] is not None:
        flags += ["--guard-max-lines", str(plan["guard_max_lines"])]
    if plan["guard_max_chars"] is not None:
        flags += ["--guard-max-chars", str(plan["guard_max_chars"])]
    if arm == TREATMENT:
        flags += ["--githits", "--githits-arm", TREATMENT]
    return flags


def judge_canary(events: list, contracts: dict) -> tuple[str, str, dict]:
    """PASS when the guard refused at least one lookup for what it says (``CONTENT_RULES``) and passed at least one
    that GitHits answered (an authentic client record, ``REACHED``), every decision verified and none ``unguarded``,
    and every model the call used is priced; FAIL otherwise, with the reason. A refused launcher call proves that the
    content rules fire and that Claude Code obeys the hook: had it run the call anyway, its client record would match
    no passed call and every record would be ``unguarded``. A call that shows only one of the two proves too little:
    a model that leaves the address out, or a launcher Claude Code will not run, would pass a weaker test and leave
    the arms unguarded or without GitHits."""
    lookups = facts(events, claude_code.LOOKUP)
    finished = last_search(events) or {}
    unguarded = [x for x in lookups if x.get("guard") == query_guard.UNGUARDED]
    decided = [x for x in lookups if isinstance(x.get("guard"), str) and x["guard"] != query_guard.UNGUARDED]
    refused = [x for x in decided if x["guard"] in CONTENT_RULES]
    reached = [x for x in decided if x["guard"] == query_guard.PASS and x.get("status") in REACHED
               and str(x.get("tool", "")).startswith(claude_code.CLIENT_TOOL)]
    spend = finished.get("spend") if isinstance(finished.get("spend"), dict) else {}
    data = {"lookups": [{"tool": x.get("tool"), "guard": x.get("guard"), "ok": x.get("ok")} for x in lookups],
            "search": {"stopped": finished.get("stopped"), "error": finished.get("error")},
            "spend": spend_of(events, contracts), "unknown_models": spend.get("unknown_models"),
            "harness": harness_of(events)}
    if finished.get("error") == "provider_guard_inactive" or unguarded:
        return FAIL, (f"the query guard did not fire: {len(unguarded)} lookup(s) ran with no verified decision "
                      "(unguarded): under this Claude Code and --bare the --settings hook did not run, or its record "
                      "was not the hook's. Do not run the pilot in this mode"), data
    if not refused or not reached:
        missing = ([] if refused else ["no lookup the guard refused for what it says (the canary asks for one "
                                       "holding an email address)"]) + \
                  ([] if reached else ["no lookup the guard passed that GitHits answered (the launcher did not run, "
                                       "or GitHits did not answer)"])
        why = f"; the call ended {finished['error']}" if finished.get("error") else ""
        return FAIL, (f"inconclusive: {' and '.join(missing)}{why}; run again, and read the canary's thread in "
                      "commands.log if it repeats"), data
    if finished.get("error"):
        return FAIL, f"the canary's call failed: {finished['error']}", data
    if spend.get("unknown_models"):
        return FAIL, (f"Claude Code ran {', '.join(spend['unknown_models'])}, which the plan does not price: add it to "
                      "prices, or cost will be unknown for every thread"), data
    if spend.get("host_observed") is not True or data["spend"] is None:
        return FAIL, "no usage of the call crossed the host's meter, so cost could not be known", data
    rules = ", ".join(sorted({x["guard"] for x in refused}))
    return PASS, (f"the guard refused {len(refused)} lookup(s) ({rules}) and passed {len(reached)} that GitHits "
                  f"answered, every decision verified, 0 unguarded; ${data['spend']:.4f}"), data


# ------------------------------------------------------------------------------------------------------------- runs

def gate(spent: float | None, reserve: float, cap: float) -> str | None:
    """Why the next run may not start, or None."""
    if spent is None:
        return ("the spend so far is unknown (a model the prices do not name, a call the meter missed, or a thread "
                "whose record could not be read), so the cap cannot be enforced")
    if spent + reserve > cap + 1e-9:
        return (f"${spent:.4f} spent; another run may cost up to ${reserve:.4f} (a thread's max_dollars), which the "
                f"${cap:.2f} cap cannot cover")
    return None


def thread_record(record: dict | None) -> dict:
    """What one exported thread says about its run."""
    if not record:
        return {"accepted": False, "candidate": None, "commit": None, "search": None, "base": None, "candidates": []}
    status = record.get("status") if isinstance(record.get("status"), dict) else {}
    events = record.get("events") or []
    finished = last_search(events) or {}
    return {"accepted": status.get("accepted") is not None, "candidate": status.get("accepted"),
            "commit": status.get("target") if status.get("accepted") else None,
            "search": {"stopped": finished.get("stopped"), "error": finished.get("error")} if finished else None,
            "base": status.get("base"),
            "candidates": [{"evaluation": c.get("evaluation"), "promotion": c.get("promotion")}
                           for c in status.get("candidates") or [] if isinstance(c, dict)]}


def not_accepted(entry: dict) -> str:
    """Why a run's thread has no accepted change, from its record: what its candidates' checks and promotions came to,
    the runner's last word on it, and how its search ended."""
    found = entry.get("candidates") or []
    passed = [c for c in found if c.get("evaluation") == "passed"]
    if passed:
        why = f"a candidate passed the host's checks, and its promotion is {passed[-1].get('promotion')}"
    elif found:
        why = f"none of its {len(found)} candidate(s) passed the host's checks"
    else:
        why = "it has no candidate"
    if entry.get("runner"):
        why += f"; the runner: {entry['runner']}"
    error = (entry.get("search") or {}).get("error")
    return why + (f"; its search ended {error}" if error else "")


def diffstat(diff: str) -> str:
    files, plus, minus = 0, 0, 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return f"{files} file(s), +{plus} -{minus}"


def ask_verdict(ask: Ask, prompt: str) -> str | None:
    """pass, fail, or None for skip; an empty answer asks again, and the end of input skips."""
    while True:
        try:
            answer = ask(prompt).strip().lower()
        except EOFError:
            return None
        if answer in ("pass", "p"):
            return "pass"
        if answer in ("fail", "f"):
            return "fail"
        if answer in ("skip", "s"):
            return None
        say("   answer pass, fail or skip")


def blind_review(entries: list, sy: Syberlabs, ask: Ask, seed: int, out: Path, reviewer: str,
                 objectives: dict) -> None:
    """Every accepted change, in a shuffled order and named R1, R2, ..., without its arm or thread: the reviewer reads
    its objective and diff (written to ``review/R<n>.diff``) and answers pass or fail, recorded with
    ``review --record`` on its thread. The mapping is in the receipt, written after the review."""
    accepted = [entry for entry in entries if entry.get("accepted")]
    if not accepted:
        return
    random.Random(f"{seed}:review").shuffle(accepted)
    folder = out / "review"
    folder.mkdir(exist_ok=True)
    say(f"\nBlind review: {len(accepted)} accepted change(s), shuffled, arms hidden. Read each diff and answer pass "
        f"or fail (skip leaves it unreviewed). Reviewer: {reviewer}.")
    for number, entry in enumerate(accepted, 1):
        rid = f"R{number}"
        diff = sy("--thread", entry["thread"], "diff", entry["candidate"]).out
        path = folder / f"{rid}.diff"
        path.write_text(diff, encoding="utf-8")
        say(f"\n{rid} of {len(accepted)}: item {entry['item']}: {objectives[entry['item']]}")
        say(f"   {diffstat(diff)}; the whole diff: {path}")
        verdict = ask_verdict(ask, f"   {rid} verdict [pass/fail/skip]: ")
        entry["review"] = {"id": rid, "verdict": verdict, "reviewer": reviewer if verdict else None}
        if verdict:
            done = sy("--thread", entry["thread"], "review", "--record", "--verdict", verdict, "--reviewer", reviewer,
                      "--revision", entry["commit"])
            if done.code != 0:
                entry["review"]["error"] = tail(done.err, 200)


# ------------------------------------------------------------------------------------------------------- evaluation

def metric_view(metric) -> dict:
    value = metric.value
    return {"known": metric.known, "value": round(value, 6) if isinstance(value, float) else value,
            "note": metric.note}


def evaluate(entries: list, records: list[dict], name: str) -> None:
    """Each run's nine metrics as the paired report reads them (``PAIRED`` for agent time and delivered), with the
    merge-based forms of those two beside them, from the exported records. A thread with a call the host's meter
    missed (``unmetered``) has an unknown cost here, whatever the fold says: part of what it spent was never priced."""
    events = [event for record in records for event in (record.get("events") or [])]
    contracts = contracts_of(records)
    runs = {run.thread: run for run in experiment.runs(events, name, contracts)}
    work = {item.thread: item for item in metrics.work_items(events, contracts)}
    for entry in entries:
        run = runs.get(entry.get("thread") or "")
        if run is None:
            why = "not run" if not entry.get("thread") else "no experiment label in the record"
            entry["metrics"] = {m: {"known": False, "value": None, "note": why, "source": PAIRED.get(m, m)}
                                for m in NINE}
            entry["strict"] = {}
        else:
            entry["metrics"] = {m: {**metric_view(run.value(PAIRED.get(m, m))), "source": PAIRED.get(m, m)}
                                for m in NINE}
            item = work[run.thread]
            entry["strict"] = {m: metric_view(item.metric(m)) for m in PAIRED}
            entry["lookups_blocked"] = metric_view(item.lookups_blocked)
            missed = unmetered([event for event in events if event.get("case_id") == run.thread])
            if missed:
                entry["metrics"]["cost"] = {"known": False, "value": None, "note": missed, "source": "cost",
                                            "metered": entry["metrics"]["cost"]["value"]}
        entry["unknown"] = [m for m in NINE if not entry["metrics"][m]["known"]]


# --------------------------------------------------------------------------------------------------------- printing

def cell(metric: dict, name: str) -> str:
    if not metric["known"]:
        return "UNKNOWN"
    value = metric["value"]
    if isinstance(value, bool):
        return "yes" if value else "no"
    if name == "cost":
        return f"${value:.4f}"
    if name == "agent_time":
        return f"{value:.2f}h"
    return f"{value:g}" if isinstance(value, float) else str(value)


def table(rows: list[list[str]]) -> list[str]:
    if not rows:  # an interrupt before the first preflight check: nothing to line up
        return []
    widths = [max(len(row[n]) for row in rows) for n in range(len(rows[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]


def render(receipt: dict, out: Path) -> str:
    """The PASS/FAIL table: the preflight, the canary, one row per run with its nine metrics, the checks after the
    runs, the total spend, the reasons for a FAIL, and where the receipt is."""
    lines = [f"pilot dry run: {receipt['verdict']}"]
    lines.append(f"  base {str(receipt['commit'].get('base') or '-')[:10]}, seed {receipt['seed']}, Claude Code "
                 f"{receipt['versions'].get('claude_code') or '-'}, experiment {receipt.get('experiment') or '-'}")
    if receipt["preflight"] and all(c["status"] == PASS for c in receipt["preflight"]):
        lines.append(f"preflight: {len(receipt['preflight'])} checks PASS ("
                     + ", ".join(c["name"] for c in receipt["preflight"]) + ")")
    else:
        lines.append("preflight")
        lines += ["  " + line for line in table([[c["status"], c["name"], c["detail"]] for c in receipt["preflight"]])]
    if receipt.get("canary"):
        lines.append("canary")
        lines.append(f"  {receipt['canary']['status']}  {receipt['canary']['detail']}")
    if receipt["threads"]:
        lines.append("runs (agent time = time to a passing change, not to a merge; delivered = accepted and passed the "
                     "blind review)")
        head = ["#", "item", "arm", "thread"] + [LABELS[m] for m in NINE] + ["verdict"]
        rows = [head]
        for entry in receipt["threads"]:
            rows.append([str(entry["order"]), entry["item"], entry["arm"], (entry.get("thread") or "-")[:8]]
                        + [cell(entry["metrics"][m], m) for m in NINE]
                        + [PASS if not entry["unknown"] else FAIL])
        lines += ["  " + line for line in table(rows)]
    for row in receipt.get("checks", []):
        lines.append(f"  {row['status']}  {row['name']}: {row['detail']}")
    spend = receipt["spend"]
    total = "unknown" if spend["total"] is None else f"${spend['total']:.4f}"
    canary = "" if spend.get("canary") is None else f" (canary ${spend['canary']:.4f})"
    cap = "no valid cap" if spend.get("cap") is None else f"${spend['cap']:.2f}"
    lines.append(f"spend   {total} of {cap}{canary}")
    shown = {f"preflight {c['name']}: {c['detail']}" for c in receipt["preflight"] if c["status"] == FAIL}
    lines += [f"reason  {reason}" for reason in receipt["reasons"] if reason not in shown]  # a FAIL row says it
    send = " (send it with report.md; the rest stays here)" if (out / "report.md").is_file() else ""
    lines.append(f"receipt {out / 'receipt.json'}{send}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------- main

def default_actor(repo: Path) -> str:
    try:
        found = git(repo, "config", "user.email")
    except (RuntimeError, OSError):
        found = ""
    return found or "pilot-dry-run"


def exclude_from_git(repo: Path, folder: Path) -> None:
    """Keep the run's directory out of ``git status`` when it lies inside the repository (``.git/info/exclude``, as
    the kit does for its own home)."""
    try:
        relative = folder.resolve().relative_to(repo.resolve())
        common = Path(git(repo, "rev-parse", "--git-common-dir"))
    except (ValueError, RuntimeError, OSError):
        return
    common = common if common.is_absolute() else repo / common
    exclude = common / "info" / "exclude"
    line = "/" + relative.as_posix().rstrip("/") + "/"
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if line not in existing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as handle:
            handle.write(("\n" if existing and existing[-1] else "") + line + "\n")


def versions(claude: str | None) -> dict:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            syberwork = version("syberwork")
        except PackageNotFoundError:
            syberwork = "source"
    except ImportError:  # pragma: no cover
        syberwork = "source"
    try:
        git_version = subprocess.run(["git", "--version"], capture_output=True, timeout=30).stdout.decode().strip()
    except (OSError, subprocess.TimeoutExpired):
        git_version = None
    try:
        skill = claude_code.githits_skill()[1]
    except Rejected:
        skill = None
    return {"claude_code": claude, "python": platform.python_version(), "platform": platform.platform(),
            "syberwork": syberwork, "git": git_version, "query_guard": query_guard.VERSION,
            "githits_skill_sha256": skill, "script": RECEIPT}


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="pilot_dry_run", description="The GitHits pilot's dry run as one command "
                                     "(module docstring: how to run it).")
    parser.add_argument("--plan", help="the plan: items, prices and the shared knobs (--template prints one)")
    parser.add_argument("--template", action="store_true", help="print a plan to fill in, and exit")
    parser.add_argument("--repo", default=".", help="the repository the items change (default: here)")
    parser.add_argument("--base", help="the commit every thread starts from; must be HEAD (default: HEAD)")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                        help=f"token-cost scheduling budget, canary included (default {DEFAULT_BUDGET:g}); "
                             "may overshoot by one call and excludes server-side search fees")
    parser.add_argument("--thread-budget", type=float,
                        help="each thread's contract max_dollars (default: the cap over the runs plus the canary)")
    parser.add_argument("--seed", type=int, help="the shuffle's seed (default: drawn, and printed)")
    parser.add_argument("--out", help="the run's directory, new or empty (default "
                                      ".syberlabs/pilot-dry-run/<time>-<seed> in the repository)")
    parser.add_argument("--actor", help="who runs it, as recorded (default: git user.email)")
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER, help=f"the blind reviewer's name (default "
                                                                      f"{DEFAULT_REVIEWER})")
    parser.add_argument("--yes", action="store_true", help="start without asking after the preflight")
    return parser.parse_args(argv)


class DryRun:
    """One dry run. Each stage writes what it found into ``receipt`` and says whether the run can go on: the
    preflight (``preflight``), the operator's go (``confirm``), the canary (``canary``), both arms of every item
    (``arms``), then the blind review, the report and the bar (``conclude``). ``finish`` writes the receipt and prints
    the table, whatever happened before it."""

    def __init__(self, args, out: Path, seed: int, ask: Ask, runner: Runner):
        self.args, self.out, self.seed, self.ask, self.cap = args, out, seed, ask, args.budget
        self.repo = Path(args.repo).resolve()
        self.sy = Syberlabs(out / "home", self.repo, args.actor or default_actor(self.repo), runner,
                            out / "commands.log")
        self.plan: dict | None = None
        self.main_contract: dict | None = None
        self.canary_contract: dict | None = None
        self.base: str | None = None
        self.order: list[tuple] = []
        self.exports: dict[str, dict] = {}  # thread -> its exported record, the latest
        self.entries: list[dict] = []
        self.receipt = {"receipt": RECEIPT, "verdict": None, "reasons": [], "started_at": utc_now(),
                        "finished_at": None, "seed": seed, "commit": {"base": None, "host": host_checkout()},
                        "versions": versions(None), "experiment": None,
                        "arms": {"baseline": BASELINE, "treatment": TREATMENT}, "preflight": [], "canary": None,
                        "schedule": [], "threads": [], "checks": [], "report": None, "settings": {},
                        "spend": {"cap": self.cap if math.isfinite(self.cap) else None, "total": None, "canary": None,
                                  "by_thread": {}},
                        "files": {"receipt": "receipt.json", "commands_log": "commands.log"}}

    # The record, as exported thread by thread.

    def keep(self, thread: str, record: dict | None) -> dict:
        """The thread's latest export. One that could not be read keeps the export before it, and is a reason to
        fail; with none before it the thread's spend is unknown."""
        if record is not None:
            self.exports[thread] = record
        else:
            self.receipt["reasons"].append(f"the export of thread {thread[:8]} could not be read"
                                           + ("; the one before it is kept" if self.exports.get(thread) else ""))
            self.exports.setdefault(thread, {})
        return self.exports[thread]

    def events(self, thread: str | None = None) -> list:
        found = [self.exports.get(thread, {})] if thread else list(self.exports.values())
        return [event for record in found for event in (record.get("events") or [])]

    def spent(self, thread: str | None = None) -> float | None:
        """Dollars the host metered over one thread or all of them; None when any of them is unknown, a thread whose
        export was never read included."""
        found = [self.exports.get(thread, {})] if thread else list(self.exports.values())
        if any(not record.get("events") for record in found):
            return None
        return spend_of(self.events(thread), contracts_of(found))

    @property
    def share(self) -> float:
        return self.main_contract["evolution"]["budget"]["max_dollars"]

    def fail(self, reason: str) -> bool:
        self.receipt["reasons"].append(reason)
        return False

    def run(self) -> int:
        if not self.preflight() or not self.confirm():
            self.receipt["verdict"] = "REFUSED"
            self.receipt["spend"]["total"] = 0.0  # no model was called
            return 2
        if not self.canary():
            self.receipt["spend"]["total"] = self.spent()
            return 1
        self.arms()
        self.conclude()
        return 0 if self.receipt["verdict"] == PASS else 1

    # Preflight.

    def budget(self) -> Check | None:
        cap, plan, share = self.cap, self.plan, self.args.thread_budget
        if not math.isfinite(cap) or cap <= 0:
            return Check("budget cap", FAIL, "--budget must be a positive number of dollars")
        if plan is None:
            return None  # the plan's own row says why
        runs = 2 * len(plan["items"])
        if plan["contract"] is not None and share is not None:
            return Check("budget cap", FAIL, "--thread-budget sets the derived contract's max_dollars; the plan's own "
                                             "contract sets its own")
        share = share if share is not None else round(cap / (runs + 1), 4)
        if not math.isfinite(share) or not 0 < share <= cap:
            return Check("budget cap", FAIL, "--thread-budget must be a positive number of dollars, at most the cap")
        self.main_contract, self.canary_contract = build_contracts(plan, self.repo, share)
        return check_budget(cap, self.main_contract, runs)

    def preflight(self) -> bool:
        say(f"pilot dry run: run directory {self.out}")
        plan_path = Path(self.args.plan).resolve()
        checks: list[Check] = []
        try:
            self.plan = load_plan(plan_path)
            plan = self.plan
            checks.append(Check("plan", PASS, f"{len(plan['items'])} items of {plan['experiment']}; max "
                                              f"{plan['max_calls']} calls of {plan['max_turns']} turns per thread; "
                                              f"tools {plan['allowed_tools']}"))
        except PlanError as exc:
            checks.append(Check("plan", FAIL, str(exc)))
        repository = check_repository(self.repo, self.args.base)
        claude = check_claude()
        checks += [repository, claude, check_lane(os.environ), check_token(os.environ, self.repo, plan_path),
                   check_doctor(self.sy), check_guard_key(os.environ)]
        budget = self.budget()
        checks += [budget] if budget else []
        if all(check.status == PASS for check in checks):
            checks.append(check_contracts(self.sy, self.main_contract, self.canary_contract))
        self.base = repository.value("base")
        receipt = self.receipt
        receipt["preflight"] = [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]
        receipt["versions"] = versions(claude.value("version"))
        receipt["commit"]["base"] = self.base
        if self.plan is not None:
            plan = self.plan
            receipt["experiment"] = plan["experiment"]
            receipt["plan"] = {"sha256": digest(plan_path.read_text(encoding="utf-8")),
                               "items": [{"id": i["id"], "tags": i["tags"], "paths": i["paths"],
                                          "objective_sha256": digest(i["objective"])} for i in plan["items"]]}
            receipt["settings"] = {"lane": "api", "max_calls": plan["max_calls"], "max_turns": plan["max_turns"],
                                   "allowed_tools": plan["allowed_tools"], "canary_turns": CANARY_TURNS,
                                   "guard": {"max_lines": plan["guard_max_lines"],
                                             "max_chars": plan["guard_max_chars"]},
                                   "reviewer": self.args.reviewer}
        if self.main_contract is not None:
            budget = self.main_contract["evolution"]["budget"]
            receipt["contract"] = {"main": contract_name(self.main_contract), "sha256": digest(self.main_contract),
                                   "budget": budget, "canary": contract_name(self.canary_contract),
                                   "checks": sorted(self.main_contract["evolution"]["evaluation"]["checks"])}
            receipt["spend"].update(thread_budget=budget.get("max_dollars"),
                                    prices_sha256=digest(budget.get("prices")))
        say("preflight")
        for line in table([[c.status, c.name, c.detail] for c in checks]):
            say("  " + line)
        failed = [check for check in checks if check.status == FAIL]
        receipt["reasons"] += [f"preflight {check.name}: {check.detail}" for check in failed]
        return not failed

    def confirm(self) -> bool:
        plan = self.plan
        self.order = schedule([item["id"] for item in plan["items"]], self.seed)
        self.receipt["schedule"] = [{"order": n, "item": item, "arm": arm}
                                    for n, (item, arm) in enumerate(self.order, 1)]
        (self.out / "items.txt").write_text(items_text(plan["items"], plan["experiment"]), encoding="utf-8")
        (self.out / "plan.json").write_text(Path(self.args.plan).read_text(encoding="utf-8"), encoding="utf-8")
        self.receipt["files"]["items"] = "items.txt"
        say(f"\nready: the canary, then {len(self.order)} runs ({len(plan['items'])} items x 2 arms) from "
            f"{self.base[:10]} in the order of seed {self.seed}; up to ${self.cap:.2f} on your Anthropic key (each "
            f"thread ${self.share:.4f}) and a few GitHits lookups")
        if self.args.yes:
            return True
        try:
            answer = self.ask("start? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        return answer in ("y", "yes") or self.fail("not started: the operator did not confirm")

    # The canary: one cheap call in the treatment's mode.

    def canary(self) -> bool:
        plan, repo = self.plan, self.repo
        say("\ncanary: one call with --bare --githits; the guard must refuse one lookup and pass one to GitHits")
        stop = gate(self.spent(), self.share, self.cap)
        if stop:
            return self.fail(f"budget: {stop}")
        self.sy.private.add(CANARY_OBJECTIVE)
        paths = plan["canary_paths"] or (["README.md"] if (repo / "README.md").is_file() else plan["items"][0]["paths"])
        done = self.sy("start", CANARY_OBJECTIVE, "--contract", contract_name(self.canary_contract),
                       *(["--paths", *paths] if paths else []))
        if done.code != 0:
            self.receipt["canary"] = {"status": FAIL, "detail": "could not start: " + tail(done.err or done.out)}
            return self.fail("canary: " + self.receipt["canary"]["detail"])
        thread = self.sy.current()
        # The treatment's flags without the web tools: the launcher is the call's only way to look anything up.
        tools = ",".join(tool for tool in claude_code.parse_tools(plan["allowed_tools"]) if tool not in WEB_TOOLS)
        proposed = self.sy("--thread", thread, *claude_flags(plan, TREATMENT, 1, CANARY_TURNS, tools), timeout=None)
        record = self.keep(thread, self.sy.export(thread, self.out / "canary.json"))
        status, detail, data = judge_canary(self.events(thread), contracts_of([record]))
        if proposed.code != 0:  # refused before any call: say why, not "no lookup"
            status, detail = FAIL, f"propose refused: {tail(proposed.err or proposed.out)}"
        self.receipt["canary"] = {"status": status, "detail": detail, "thread": thread, **data}
        self.receipt["spend"]["canary"] = data["spend"]
        say(f"  {status}  {detail}")
        return status == PASS or self.fail(f"canary: {detail}")

    # The runs: both arms of each item from the same HEAD, in the seeded order.

    def arms(self) -> None:
        plan, sy = self.plan, self.sy
        sy.private.update(item["objective"] for item in plan["items"])
        items = {item["id"]: item for item in plan["items"]}
        stop = None
        for number, (item_id, arm) in enumerate(self.order, 1):
            entry = {"order": number, "item": item_id, "arm": arm, "tags": items[item_id]["tags"], "thread": None,
                     "accepted": False}
            self.entries.append(entry)
            stop = stop or self.ready(number)
            if stop:
                entry["skipped"] = stop
                continue
            say(f"[{number}/{len(self.order)}] {item_id} {arm}: running")
            stop = self.one(entry, items[item_id])
        if stop:
            self.receipt["reasons"].append(stop)

    def ready(self, number: int) -> str | None:
        """Why run ``number`` may not start: the cap, or a ``HEAD`` that moved."""
        stop = gate(self.spent(), self.share, self.cap)
        if stop:
            return f"budget: stopped before run {number} of {len(self.order)}: {stop}"
        try:
            head = git(self.repo, "rev-parse", "HEAD")
        except (RuntimeError, OSError) as exc:
            head = str(exc)
        if head != self.base:
            return f"HEAD moved from {self.base[:10]} to {head[:10]} during the run; every run starts from the base"
        return None

    def one(self, entry: dict, item: dict) -> str | None:
        """One run: start, label, propose, accept on the host's checks, export. Returns why no further run may start,
        or None."""
        sy, number, arm, plan = self.sy, entry["order"], entry["arm"], self.plan
        done = sy("start", item["objective"], "--contract", contract_name(self.main_contract),
                  *(["--paths", *item["paths"]] if item["paths"] else []), "--unattended")
        if done.code != 0:
            entry["skipped"] = f"run {number}: start failed: {tail(done.err or done.out)}"
            return entry["skipped"]
        thread = entry["thread"] = sy.current()
        stop = None
        label = sy("--thread", thread, "experiment", "label", "--name", plan["experiment"], "--item", item["id"],
                   "--arm", arm, *[flag for tag in item["tags"] for flag in ("--tag", tag)])
        if label.code != 0:
            stop = f"run {number}: the label was refused: {tail(label.err or label.out)}"
        else:
            proposed = sy("--thread", thread, *claude_flags(plan, arm, plan["max_calls"], plan["max_turns"]),
                          timeout=None)
            if proposed.code != 0:
                stop = f"run {number}: propose failed: {tail(proposed.err or proposed.out)}"
            else:
                ran = sy("--thread", thread, "run", "--once")
                said = [line.strip()[len(thread[:8]):].strip() for line in ran.out.splitlines()
                        if line.strip().startswith(thread[:8])]
                entry["runner"] = tail(said[-1], 200) if said else None
        path = self.out / "threads" / f"{number:02d}-{file_name(item['id'])}-{arm}-{thread[:8]}.json"
        path.parent.mkdir(exist_ok=True)
        record = self.keep(thread, sy.export(thread, path))
        entry.update(thread_record(record), export=path.relative_to(self.out).as_posix())
        search = entry.get("search") or {}
        if entry.get("base") not in (None, self.base):
            stop = stop or f"run {number}: the thread started from {str(entry['base'])[:10]}, not the base"
        if search.get("error") == "provider_guard_inactive":
            stop = stop or f"run {number}: the query guard did not fire (provider_guard_inactive); no further run"
        missed = unmetered(record.get("events") or []) if record.get("events") else "its export could not be read"
        if missed:
            stop = stop or (f"run {number}: {missed}, so what it spent is unknown and the cap cannot be enforced; no "
                            "further run")
        cost = self.spent(thread)
        state = "accepted" if entry["accepted"] else f"not accepted ({search.get('error') or search.get('stopped')})"
        say(f"    {state}; " + ("cost unknown" if cost is None else f"${cost:.4f}"))
        return stop

    # The blind review, the report and the bar.

    def conclude(self) -> None:
        plan, sy, out, receipt = self.plan, self.sy, self.out, self.receipt
        blind_review(self.entries, sy, self.ask, self.seed, out, self.args.reviewer,
                     {item["id"]: item["objective"] for item in plan["items"]})
        for entry in self.entries:  # again: the reviews are on the record now
            if entry.get("thread"):
                self.keep(entry["thread"], sy.export(entry["thread"], out / entry["export"]))
        evaluate(self.entries, list(self.exports.values()), plan["experiment"])
        for entry in self.entries:
            entry["harness"] = harness_of(self.events(entry["thread"]) if entry.get("thread") else [])
        receipt["threads"] = self.entries
        receipt["spend"]["total"] = self.spent()
        receipt["spend"]["by_thread"] = {thread: self.spent(thread) for thread in self.exports}
        found = self.report()
        receipt["report"] = None if found is None else {
            "compared": found["items"]["compared"], "excluded": found["items"]["excluded"], "checks": found["checks"],
            "not_run": found.get("not_run"), "rule": found["rule"]}
        rows, reasons = receipt["checks"], receipt["reasons"]
        unknown = [entry for entry in self.entries if entry["unknown"]]
        for e in self.entries:  # why a thread has no time to a passing change, before saying that it has none
            if e.get("thread") and not e.get("accepted"):
                reasons.append(f"run {e['order']} ({e['item']} {e['arm']}): not accepted: {not_accepted(e)}")
        reasons += [f"run {e['order']} ({e['item']} {e['arm']}): unknown " + ", ".join(LABELS[m] for m in e["unknown"])
                    for e in unknown]
        rows.append({"status": PASS if self.entries and not unknown else FAIL, "name": "nine metrics",
                     "detail": f"known for {len(self.entries) - len(unknown)} of {len(self.entries)} threads"})
        rows += report_rows(found, len(plan["items"]), reasons)
        lanes = sorted({lane for entry in self.entries for lane in entry["harness"].get("lane", [])})
        bases = sorted({basis for entry in self.entries for basis in entry["harness"].get("cost_basis", [])})
        if lanes and (lanes != ["api"] or bases != ["billed"]):
            rows.append({"status": FAIL, "name": "lane", "detail": f"the candidates recorded lane {lanes}, cost basis "
                         f"{bases}; the dry run runs the api lane only"})
            reasons.append("a candidate ran outside the api lane")
        total = receipt["spend"]["total"]
        receipt["spend"]["within_cap"] = within = total is not None and total <= self.cap + 1e-9
        rows.append({"status": PASS if within else FAIL, "name": "spend",
                     "detail": ("unknown" if total is None else f"${total:.4f}") + f" of the ${self.cap:.2f} cap"})
        if not within:
            reasons.append("spend " + ("unknown" if total is None else f"${total:.4f} is over the ${self.cap:.2f} cap"))
        receipt["verdict"] = PASS if not reasons and all(row["status"] != FAIL for row in rows) else FAIL

    def report(self) -> dict | None:
        """``experiment report --items FILE --delivered review``, as JSON (read) and Markdown (printed), and the
        nine metrics per thread as ``syberlabs metrics`` folds them, all written into the run's directory."""
        out = self.out
        flags = ["experiment", "report", "--name", self.plan["experiment"], "--items", str(out / "items.txt"),
                 "--delivered", "review"]
        as_json = self.sy(*flags, "--json")
        try:
            found = json.loads(as_json.out) if as_json.out.strip().startswith("{") else None
        except ValueError:
            found = None
        markdown = self.sy(*flags, "--markdown").out
        (out / "report.json").write_text(as_json.out, encoding="utf-8")
        (out / "report.md").write_text(markdown, encoding="utf-8")
        work = metrics.work_items(self.events(), contracts_of(list(self.exports.values())))
        (out / "metrics.json").write_text(json.dumps([w.to_dict() for w in work], indent=2) + "\n", encoding="utf-8")
        self.receipt["files"].update(report_json="report.json", report_md="report.md", metrics="metrics.json")
        if markdown.strip():
            say("\nthe paired report (experiment report --items items.txt --delivered review --markdown)")
            say(markdown.rstrip())
        if found is None:
            self.receipt["report_error"] = tail(as_json.err or as_json.out, 200)
        return found

    def finish(self) -> None:
        """Write the receipt and print the table: every way out of ``main`` ends here. Before it is written, every
        file of the run's directory, and the receipt itself, is checked for the operator's credentials."""
        receipt, out = self.receipt, self.out
        receipt["finished_at"] = utc_now()
        receipt["commands"] = self.sy.commands
        if receipt["verdict"] is None:
            receipt["verdict"] = FAIL
        credentials = [(name, os.environ.get(name, "")) for name in (githits.TOKEN_ENV, API_KEY_ENV)]
        credentials = [(name, value) for name, value in credentials if len(value) >= MIN_SECRET_CHARS]
        files = [path for path in out.rglob("*") if path.is_file()]
        written, read, unread = [], 0, 0
        for name, value in credentials:  # every file, whatever its size
            found, read, unread = files_holding(value, files, limit=None)
            written += [f"{name} in {path.relative_to(out).as_posix()}" for path in found]
        receipt["credential_scan"] = {"files": read, "unread": unread, "found": written}
        if unread:
            receipt["verdict"] = FAIL
            receipt["reasons"].append(f"{unread} file(s) of the run's directory could not be read for the credential "
                                      "scan")
        if written:  # never expected: nothing in the factory writes a credential
            receipt["verdict"] = FAIL
            receipt["reasons"].append("a credential was written in the run's directory: " + "; ".join(written))
        text = json.dumps(receipt, indent=2, sort_keys=True)
        leaked = [name for name, value in credentials if value in text]
        if leaked:  # the receipt is built from the record, never from the environment; checked all the same
            receipt["verdict"] = FAIL
            receipt["reasons"].append("a credential reached the receipt and was redacted: " + ", ".join(leaked))
            text = json.dumps(receipt, indent=2, sort_keys=True)
            for name, value in credentials:
                text = text.replace(value, f"[{name} redacted]")
        (out / "receipt.json").write_text(text + "\n", encoding="utf-8")
        say("")
        say(render(json.loads(text), out))


def report_rows(found: dict | None, expected: int, reasons: list) -> list[dict]:
    """The table's rows about the report: it compares all ``expected`` items of the plan, or the run fails with what
    kept the others out (a check that excludes an item, or a registered item that never ran); then a warning for each
    check that keeps an item but says something about it."""
    if found is None:
        reasons.append("the paired report could not be read")
        return [{"status": FAIL, "name": "report", "detail": "the report could not be read"}]
    checks = found.get("checks") or {}
    why = [f"{flag}: {', '.join(items)}" for flag, items in sorted(checks.items()) if flag in experiment.EXCLUDE]
    if found.get("not_run"):
        why.append("not run: " + ", ".join(found["not_run"]))
    compared = found["items"]["compared"]
    detail = f"{compared} of {expected} items compared"
    if why or compared != expected:
        detail += "".join(f"; {reason}" for reason in why)
        reasons.append(f"the paired report did not compare every item: {detail}")
        rows = [{"status": FAIL, "name": "report", "detail": detail}]
    else:
        verdict = experiment.VERDICT[found["rule"]["met"]]
        rows = [{"status": PASS, "name": "report", "detail": f"{detail}; rule {verdict}"}]
    rows += [{"status": WARN, "name": flag, "detail": f"{', '.join(items)}: {experiment.CHECKS.get(flag, flag)}"}
             for flag, items in sorted(checks.items()) if flag not in experiment.EXCLUDE]
    return rows


def survive_unicode() -> None:
    """Never crash on a console that cannot encode an objective (cp1252 on Windows): replace, as the CLI does."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list | None = None, *, ask: Ask | None = None, runner: Runner | None = None) -> int:
    survive_unicode()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.template:
        say(json.dumps(TEMPLATE, indent=2))
        return 0
    if not args.plan:
        say("error: --plan is required (python scripts/pilot_dry_run.py --template > plan.json)")
        return 2
    if not metrics._NAME.match(args.reviewer):
        say("error: --reviewer must be a short name (letters, digits, _ . @ + -)")
        return 2
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        say(f"error: --repo {repo} is not a directory")
        return 2
    seed = args.seed if args.seed is not None else secrets.randbelow(2 ** 31)
    runs_dir = repo / ".syberlabs" / "pilot-dry-run"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out).resolve() if args.out else runs_dir / f"{stamp}-{seed}"
    if out.exists() and any(out.iterdir()):
        say(f"error: {out} is not empty; name another --out")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    exclude_from_git(repo, out if args.out else runs_dir)
    dry = DryRun(args, out, seed, ask or input, runner or subprocess_runner)
    try:
        status = dry.run()
    except KeyboardInterrupt:
        dry.receipt["verdict"] = "ABORTED"
        dry.receipt["reasons"].append("interrupted by the operator")
        status = 1
    except Exception as exc:  # exception text and tracebacks can contain credentials or private output
        dry.receipt["verdict"] = FAIL
        dry.receipt["reasons"].append(f"internal error: {type(exc).__name__}")
        status = 1
    finally:
        dry.finish()
    return 1 if dry.receipt["verdict"] == FAIL else status


if __name__ == "__main__":
    raise SystemExit(main())
