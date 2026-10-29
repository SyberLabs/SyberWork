"""The runner: every (task, arm, seed) of a registered spec through a real unattended Build Thread, one bundle each.

``run_eval`` refuses a spec that is not exactly as registered (``spec.registered``) and, unless told it is a dry
run, a checkout whose preflight for the current commit did not pass (``preflight_gate``). It expands the spec's
tasks x arms x seeds in spec order and shuffles them with ``order_seed`` (``plan``; a Fisher-Yates shuffle drawn
from ``random.Random(order_seed).random()``, whose sequence Python guarantees across versions). Each run:

1. a fresh work directory holding only the run's repository at the task's pinned base (``tasks.materialize``);
   the kit home and the host's copy of the task's hidden files (``tasks.stage_hidden``, read-only) are each a
   directory with a random 128-bit name under a host root of their own (``--hidden-root``, default
   ``<system temp>/syberlabs-eval-host``), so neither is a path the run's repository leads to;
2. the run contract ``eval-run.v1`` (``run_contract``): the task's scope, its visible checks and its hidden checks
   as required checks ``hidden.<name>`` by absolute path, the spec's price table, ``max_dollars`` the run's dollar
   ceiling (the spec's per-run dollars, or the eval's remaining dollars when less), ``max_seconds`` its seconds;
   one action, ``accept_change``, and no publishing action at all;
3. ``Kit.start(..., unattended=True)`` as ``syberlabs.eval``; the arm's provider (``arms.build``) proposes with the
   run's seed, in the arm's environment (``arms.environment``), the host's governor pricing every call, a ``repair``
   or ``claude-code`` provider's feedback filtered by the spec's ``hidden_feedback`` and recorded as sent;
4. the candidate the search recommends (else its last in-scope candidate) is checked by the host
   (``Thread.check``: every check on the exact candidate tree) and accepted only if admission allows it
   (``Thread.accept``, which moves the thread's branch in the run's own repository and nothing else). Candidate
   code runs inside the checks with the host's permissions, so the host's copy of the hidden files is hashed
   (``tasks.tree_digests``) when staged, after the proposal, after the check and immediately before acceptance, and
   compared with the copy as staged (``tasks.hidden_changes``); on any change nothing more runs, nothing is
   accepted, and the outcome is ``not_acceptable`` with reason ``hidden_tampered``. This detects a rewrite that
   stays, not one undone between two hashes;
5. evidence: the exported journal, the provider's trace, the run contract, the feedback each request carried
   (``feedback.json``, repair and claude-code arms), a ``git bundle`` of the run's repository, and, when the run
   accepted a candidate and the seed variable is set, a certificate signed with that key and verified at once
   against the repository and the journal (``uncertified`` otherwise);
6. ``evals/<id>/runs/<run_id>/bundle.json``: the digests of the spec, task and arm, the seed, the order index, the
   ``hidden_feedback`` mode, the base, the harness, provider, Python, git and (for a claude-code arm) Claude Code
   versions, the times, the dollar ceiling, every hash of the hidden copy (``hidden_integrity``), a scan of the
   evidence files for the values of the host's credential variables (names only, never a value), the paths of the
   evidence files with every file's SHA-256, and the outcome.

A run's files are written into ``<run_id>.partial`` and renamed when complete, so a run killed midway leaves no
bundle and is run again; a run with a bundle is skipped (resumable by run id). A bundle is read only when it is the
evidence of the run its directory names (``EvalRun.verify``; ``bundle_tampered`` otherwise). Every run, completed
or not, appends
one line to the eval's spend ledger ``evals/<id>/spend.jsonl`` before its ``.partial`` or any of its directories is
deleted: the dollars its journal records (finished searches' ``spend``, plus the registrations of a search that never
finished). The eval's dollar cap counts the ledger (and any bundle older than it), not completed bundles, so a run
that spent and then raised, was interrupted, or failed to certify is still counted. A run starts only when the
ledger plus the spec's per-run dollars fit under ``budget.total_dollars`` (else ``eval_budget_exhausted``), and its
governor ceiling is the lesser of the per-run dollars and the eval's remaining dollars. The governor lets the call
that crosses a ceiling complete and refuses the next (``docs/BUILD_THREAD.md``), so the eval's recorded spend
exceeds ``total_dollars`` by at most one call's cost. The ledger is read strictly (``spec.loads``: no repeated key,
no ``NaN`` or ``Infinity``), and a line that is not a record, whose dollars are not a finite number of at least
zero, or whose spend names a model the price table does not price stops the eval (``eval_spend_unknown``). One
runner holds an eval at a time (``runs/.lock``; a second is refused ``eval_running``), and the preflight gate is
asked again before every run, so a checkout that moves or changes mid-eval stops it.

``preflight`` runs the deterministic suites (``scripts/run_shards.py``, ``conformance.run``,
``benchmarks/run_scenarios.py``, and this package's tests) from the checkout and records the result for the
commit in ``evals/<id>/preflight.json``.
"""

from __future__ import annotations

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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from syberlabs.canonical import digest
from syberlabs.errors import Rejected
from syberlabs.eval import VERSION, arms, spec as specs, tasks
from syberlabs.eval.spec import HARNESS, harness_state, now_utc, read_json, write_json

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - not Windows
    msvcrt = None

RUNNER_ACTOR = "syberlabs.eval"
LOCK = ".lock"                # runs/.lock: one runner per eval at a time
HIDDEN_TAMPERED = "hidden_tampered"  # the host's copy of the hidden files changed while the run's checks ran
CONTRACT_ID = "eval-run"
BUNDLE_SCHEMA_ID = "syberlabs.eval/bundle.v1"
PREFLIGHT_SCHEMA_ID = "syberlabs.eval/preflight.v1"
DEFAULT_SEED_ENV = "SYBERLABS_CERTIFICATE_SEED"
PREFLIGHT = (("tests", ["{python}", "scripts/run_shards.py"]),
             ("conformance", ["{python}", "-m", "conformance.run"]),
             ("scenarios", ["{python}", "benchmarks/run_scenarios.py"]),
             ("eval_tests", ["{python}", "-m", "unittest", "discover", "-s", "tests", "-p", "test_eval_*.py"]))
PREFLIGHT_TIMEOUT = 7200
TAIL = 1500
SPEND_SCHEMA_ID = "syberlabs.eval/spend.v1"
SPEND_FILE = "spend.jsonl"
HOST_ROOT = "syberlabs-eval-host"  # under the system temporary directory unless --hidden-root names another root
FEEDBACK_KINDS = ("repair", "claude-code")  # the arms whose requests carry feedback (arms.feedback_filter)
INTEGRITY_POINTS = ("staged", "after_propose", "after_check", "before_accept")
# Host variables whose values the evidence must never hold; matched by name, values of 16 characters or more that are
# not a path on this machine. The bundle records the names checked and where a value was found, never a value.
CREDENTIAL_NAME = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD|SEED|_KEY)$", re.IGNORECASE)


@dataclass(frozen=True)
class Planned:
    order_index: int
    run_id: str
    task: str
    arm: str
    seed: int


def run_id(task: str, arm: str, seed: int) -> str:
    return f"{task}.{arm}.s{seed}"


def plan(doc: dict) -> list[Planned]:
    """Every (task, arm, seed) of the spec, in the order ``order_seed`` gives (see the module docstring)."""
    cells = [(task, arm["name"], seed) for task in doc["tasks"]["ids"] for arm in doc["arms"] for seed in doc["seeds"]]
    rng = random.Random(doc["order_seed"])
    for i in range(len(cells) - 1, 0, -1):
        j = int(rng.random() * (i + 1))
        cells[i], cells[j] = cells[j], cells[i]
    return [Planned(index, run_id(*cell), *cell) for index, cell in enumerate(cells)]


def select(planned: list[Planned], only: str | None) -> list[Planned]:
    """``--only task,arm,seed``: each part a value, or empty or ``*`` for any."""
    if not only:
        return planned
    parts = (only.split(",") + ["", "", ""])[:3]
    if len(only.split(",")) > 3:
        raise Rejected("invalid_only", "--only is task,arm,seed")
    want = [None if part.strip() in ("", "*") else part.strip() for part in parts]
    return [p for p in planned if (want[0] in (None, p.task) and want[1] in (None, p.arm)
                                   and want[2] in (None, str(p.seed)))]


def run_contract(task: dict, checks: dict, required: list[str], budget: dict) -> dict:
    """The contract of one run (step 2 of the module docstring)."""
    scope = task["scope"]
    return {
        "id": CONTRACT_ID, "version": 1, "title": f"Eval run of task {task['id']}",
        "inputs": {"objective": "string", "act_class": "string"},
        "actions": {"accept_change": {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
        "evolution": {
            "scope": {"paths": list(scope["paths"]), "exclude": list(scope.get("exclude", [])),
                      "max_files": scope.get("max_files", 20), "max_diff_bytes": scope.get("max_diff_bytes", 200_000)},
            "operators": ["patch", "mutation", "crossover"],
            "budget": {"max_candidates": 400, "max_evaluations": 400, "max_seconds": budget["seconds"],
                       "prices": budget["prices"], "max_dollars": budget["dollars"],
                       "strict": budget.get("strict", True)},
            "evaluation": {"checks": checks, "required": required,
                           "max_age_seconds": min(604_800, max(3600, 2 * budget["seconds"]))},
            "promotion": {"action": "accept_change", "roles": ["developer"]},
        },
    }


def _sha256(path: Path) -> str:
    found = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            found.update(chunk)
    return found.hexdigest()


def _version(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (done.stdout or done.stderr).strip().splitlines()
    return lines[0][:200] if done.returncode == 0 and lines else None


def _chosen(events: list[dict], views: list[dict]) -> str | None:
    """The search's recommendation, else its last in-scope candidate, else None."""
    finished = next((e["body"] for e in reversed(events) if e["kind"] == "search_finished"), None)
    if finished is None:
        return None
    in_scope = {v["id"] for v in views if not v["scope_violations"] and not v["limit_violations"]}
    for cid in finished.get("recommended") or []:
        if cid in in_scope:
            return cid
    return next((cid for cid in reversed(finished.get("candidates") or []) if cid in in_scope), None)


def _git_bundle(repo: Path, out: Path) -> bool:
    done = subprocess.run(["git", "-C", str(repo), "bundle", "create", str(out), "--all"], capture_output=True,
                          timeout=600)
    return done.returncode == 0 and out.is_file()


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold ``path`` locked for one runner (``flock``, or ``msvcrt.locking`` on Windows); refused, not waited for,
    when another process holds it (``eval_running``): two runners would each count only the bundles they found
    and pass the eval's dollar cap together, and both would start the same run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    locked = False
    try:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                os.lseek(handle.fileno(), 0, os.SEEK_SET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError:
            raise Rejected("eval_running", f"another runner holds {path}; wait for it to finish") from None
        yield
    finally:
        if locked and fcntl is None and msvcrt is not None:
            os.lseek(handle.fileno(), 0, os.SEEK_SET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()  # releases the flock


def hidden_hash(copy: Path, staged: dict[str, str] | None, point: str) -> dict:
    """One hash of a run's hidden copy at ``point``: every file's SHA-256 (``tasks.tree_digests``), their digest, and
    what changed since the copy was staged (``tasks.hidden_changes`` against ``staged``; nothing at staging)."""
    files = tasks.tree_digests(copy)
    changed = [] if staged is None else tasks.hidden_changes(staged, files)
    return {"point": point, "at": now_utc(), "digest": digest(files), "files": files, "changed": changed,
            "match": not changed}


def hidden_record(task_digest: str, hashes: list[dict]) -> dict:
    """``hidden_integrity`` in the bundle: the registered task digest, the staged copy's digest every later hash is
    compared with, every hash, and the paths changed at any of them."""
    changed = sorted({path for entry in hashes for path in entry["changed"]})
    return {"task_digest": task_digest, "expected": hashes[0]["digest"] if hashes else None,
            "tampered": bool(changed) if hashes else None, "changed": changed, "hashes": hashes}


def ledger_dollars(line: dict) -> float | None:
    """A ledger line's dollars when they are a finite number of at least zero (not a bool), else None: unknown.

    An integer too large for a float (``spec.loads`` reads one of up to 4,300 digits) is unknown too: ``math.isfinite``
    and ``float`` raise ``OverflowError`` on it, which would reach ``EvalRun.spent`` and ``report.build`` uncoded."""
    value = line.get("dollars")
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
        return float(value)
    except OverflowError:
        return None


def recorded_spend(events: list[dict]) -> dict:
    """The dollars a run's journal records: every finished search's ``spend``, plus the registration spend of a
    search that never finished (``evolution.spend_total`` counts finished searches only)."""
    from syberlabs.economic import dollars
    from syberlabs.evolution import spend_total

    total = spend_total(events)
    finished = {e["body"]["id"] for e in events if e["kind"] == "search_finished"}
    unknown, extra, unfinished, open_search = list(total["unknown_models"]), [], 0, False
    for event in events:
        if event["kind"] == "search_started":
            open_search = event["body"]["id"] not in finished
            unfinished += open_search
        elif event["kind"] == "candidate_registered" and open_search and event["body"].get("spend"):
            spend = event["body"]["spend"]
            if spend.get("dollars") is None:
                unknown.append("unpriced")
            else:
                extra.append(spend["dollars"])
    return {"dollars": dollars([total["dollars"], *extra]), "host_observed": bool(total["host_observed"] or extra),
            "unknown_models": sorted(set(unknown)), "searches": total["searches"] + unfinished,
            "unfinished_searches": unfinished}


def _looks_secret(value: str) -> bool:
    if len(value) < 16 or any(c.isspace() for c in value):
        return False
    try:
        return not os.path.exists(value)
    except (OSError, ValueError):
        return True


def credential_scan(folder: Path) -> dict:
    """Which host credential variables (by name) have a value in which evidence file of ``folder``; never a value."""
    names = sorted(k for k, v in os.environ.items() if CREDENTIAL_NAME.search(k) and _looks_secret(v))
    found = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix == ".json"):
        data = path.read_bytes()
        found += [{"variable": name, "file": path.name} for name in names if os.environ[name].encode() in data]
    return {"variables": names, "found": found}


class EvalRun:
    """One eval: the registered spec, the order, the cap, and the runs."""

    def __init__(self, evals_root: str | Path, eval_id: str, *, harness: Path = HARNESS, work_root: Path | None = None,
                 keep_work: bool = False, seed_env: str = DEFAULT_SEED_ENV, log: Callable[[str], None] | None = None,
                 hidden_root: Path | None = None):
        self.root = Path(evals_root).resolve()
        self.eval_id, self.harness, self.keep_work, self.seed_env = eval_id, Path(harness), keep_work, seed_env
        self.work_root = Path(work_root) if work_root else None
        self.hidden_root = Path(hidden_root) if hidden_root else None
        self.log = log or (lambda line: None)
        self.spec_path = self.root / eval_id / "spec.json"
        self.doc, self.registration = specs.registered(self.spec_path)
        self.runs_dir = self.root / eval_id / "runs"
        self.tasks_root = self.root / "tasks"
        self.arms = {arm["name"]: arm for arm in self.doc["arms"]}

    # Bundles already written

    def bundles(self) -> dict[str, dict]:
        """Every completed run's bundle, by run id; ``<run>.partial`` directories are runs that did not complete.
        A bundle under another spec digest is refused (``bundle_mismatch``), and so is one that is not the evidence
        of the run its directory names (``bundle_tampered``, ``verify``)."""
        found = {}
        planned = {item.run_id: item for item in plan(self.doc)}
        if self.runs_dir.is_dir():
            for path in sorted(self.runs_dir.glob("*/bundle.json")):
                if path.parent.name.endswith(".partial"):
                    continue
                bundle = read_json(path, "bundle_unreadable")
                if not isinstance(bundle, dict) or bundle.get("spec_digest") != self.registration["spec_digest"]:
                    raise Rejected("bundle_mismatch", f"{path} was written under another spec digest")
                reason = self.verify(path.parent, bundle, planned.get(path.parent.name))
                if reason:
                    raise Rejected("bundle_tampered", f"{path}: {reason}")
                found[bundle["run_id"]] = bundle
        return found

    def verify(self, folder: Path, bundle: dict, item: Planned | None) -> str | None:
        """Why ``bundle`` is not the evidence of the planned run ``item`` its directory names, or None: its identity
        and digests are the registration's, every file it lists is there with its SHA-256 and nothing else is, and
        the dollars, unknown models and acceptance it reports are its journal's. Edits are detected, not prevented:
        nothing signs ``bundle.json``, so a writer who rewrites every digest consistently is not."""
        from syberlabs.evolution import spend_total

        if item is None:
            return "its directory names no run of this eval"
        expected = {"schema": BUNDLE_SCHEMA_ID, "eval": self.eval_id, "run_id": item.run_id, "task": item.task,
                    "arm": item.arm, "seed": item.seed, "order_index": item.order_index,
                    "task_digest": self.registration["task_digests"].get(item.task),
                    "arm_digest": self.registration["arm_digests"].get(item.arm), "journal_path": "journal.json"}
        wrong = sorted(key for key, value in expected.items() if bundle.get(key) != value)
        if wrong:
            return "it is not this run's: " + ", ".join(wrong)
        files, present = bundle.get("files"), sorted(p.name for p in folder.iterdir() if p.name != "bundle.json")
        if not isinstance(files, dict) or sorted(files) != present:
            return "the files beside it are not the files it lists"
        changed = [name for name in present if not (folder / name).is_file() or _sha256(folder / name) != files[name]]
        if changed:
            return "changed since the run: " + ", ".join(changed)
        journal = read_json(folder / "journal.json", "bundle_unreadable")
        events = journal.get("events") if isinstance(journal, dict) else None
        outcome = bundle.get("outcome")
        if not isinstance(events, list) or not isinstance(outcome, dict):
            return "no journal events or no outcome"
        spend = spend_total(events)
        if (outcome.get("dollars"), outcome.get("unknown_models")) != (spend["dollars"], spend["unknown_models"]):
            return "its dollars are not its journal's"
        if (outcome.get("status") == "accepted") != any(e.get("kind") == "effect_succeeded" for e in events):
            return "its acceptance is not its journal's"
        return None

    @staticmethod
    def dollars(bundle: dict) -> float:
        return float((bundle.get("outcome") or {}).get("dollars") or 0.0)

    # The spend ledger

    @property
    def ledger_path(self) -> Path:
        return self.root / self.eval_id / SPEND_FILE

    def ledger(self) -> list[dict]:
        """Every line of ``spend.jsonl``, read as strictly as a spec (``spec.loads``: a repeated key, ``NaN`` and
        ``Infinity`` are refused); a line that is not one JSON object is ``{"unreadable": <line number>}``."""
        found = []
        if self.ledger_path.is_file():
            try:
                data = self.ledger_path.read_bytes()
            except OSError as exc:
                raise Rejected("eval_spend_unknown",
                               f"{self.ledger_path}: {exc.strerror or type(exc).__name__}") from None
            for number, raw in enumerate(data.split(b"\n"), 1):
                if raw.strip():
                    try:
                        value = specs.loads(raw.decode("utf-8"))
                    except ValueError:  # not UTF-8, not JSON, a repeated key, NaN or Infinity
                        value = None
                    found.append(value if isinstance(value, dict) else {"unreadable": number})
        return found

    def spent(self, bundles: dict[str, dict]) -> tuple[float, list[str]]:
        """The eval's recorded dollars (the ledger, plus any bundle with no ledger line) and what made it unknown."""
        values, unknown, threads = [], [], set()
        for line in self.ledger():
            label = line.get("run_id") or f"line {line.get('unreadable')}"
            dollars = None if "unreadable" in line else ledger_dollars(line)
            if dollars is None:
                unknown.append(f"{label}: dollars unknown (not a finite number of at least 0)")
            else:
                values.append(dollars)
            models = line.get("unknown_models")
            if models:
                unknown.append(f"{label}: unpriced " + (", ".join(map(str, models)) if isinstance(models, list)
                                                        else repr(models)))
            if isinstance(line.get("thread"), str):
                threads.add(line["thread"])
        for bundle in bundles.values():  # written before the ledger existed
            if bundle.get("thread") not in threads:
                values.append(self.dollars(bundle))
                if (bundle.get("outcome") or {}).get("unknown_models"):
                    unknown.append(f"{bundle.get('run_id')}: unpriced")
        return round(sum(values), 6), unknown

    def _charge(self, item: Planned, thread, state: str, error: BaseException | None, ceiling: float) -> None:
        """Append the run's recorded spend to the ledger (flushed and synced before anything is deleted)."""
        try:
            spend = recorded_spend(thread.history()) if thread is not None else {
                "dollars": 0.0, "host_observed": False, "unknown_models": [], "searches": 0, "unfinished_searches": 0}
        except Exception as exc:  # the journal cannot be read: the spend is unknown, and the eval stops on it
            spend = {"dollars": None, "host_observed": None, "unknown_models": [], "unreadable": type(exc).__name__}
        line = {"schema": SPEND_SCHEMA_ID, "eval": self.eval_id, "run_id": item.run_id,
                "order_index": item.order_index, "thread": getattr(thread, "id", None), "state": state,
                "error": None if error is None else (error.code if isinstance(error, Rejected) else type(error).__name__),
                "ceiling": ceiling, "at": now_utc(), **spend}
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "ab") as handle:
            handle.write((json.dumps(line, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    def _host_root(self) -> Path:
        root = self.hidden_root or Path(tempfile.gettempdir()) / HOST_ROOT
        root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
        return root.resolve()

    # The eval

    def run(self, *, only: str | None = None, dry: bool = False, require_preflight: bool = True) -> dict:
        gate = None
        if require_preflight:
            try:
                gate = preflight_gate(self.root, self.eval_id, self.harness)
            except Rejected as exc:
                if not dry:
                    raise
                gate = {"refused": exc.code}
        if dry:
            return self._run(only, True, require_preflight, gate)
        with _exclusive(self.runs_dir / LOCK):
            return self._run(only, False, require_preflight, gate)

    def _run(self, only: str | None, dry: bool, require_preflight: bool, gate: dict | None) -> dict:
        planned = select(plan(self.doc), only)
        done = self.bundles()
        budget = self.doc["budget"]
        spent, unknown = self.spent(done)
        rows, stopped = [], None
        seed = None
        if not dry and os.environ.get(self.seed_env):
            from syberlabs.certificate import load_seed
            seed = load_seed(self.seed_env)  # checked once, held in memory, never printed
        for item in planned:
            if item.run_id in done:
                rows.append({"run_id": item.run_id, "order_index": item.order_index, "state": "done",
                             "outcome": done[item.run_id]["outcome"]["status"]})
                continue
            if dry:
                rows.append({"run_id": item.run_id, "order_index": item.order_index, "state": "pending"})
                continue
            if unknown:
                stopped = "eval_spend_unknown"
                self.log("stop: eval_spend_unknown (" + "; ".join(unknown[:3]) + ")")
                break
            if spent + budget["dollars"] > budget["total_dollars"]:
                stopped = "eval_budget_exhausted"
                self.log(f"stop: eval_budget_exhausted (spent ${spent:.4f}; a run may cost ${budget['dollars']:.4f}; "
                         f"cap ${budget['total_dollars']:.4f})")
                break
            if require_preflight:
                try:  # again before every run: the checkout may have moved or changed since the eval started
                    preflight_gate(self.root, self.eval_id, self.harness)
                except Rejected as exc:
                    stopped = exc.code
                    self.log(f"stop: {exc.code} ({exc.detail})")
                    break
            ceiling = round(min(budget["dollars"], budget["total_dollars"] - spent), 6)
            self.log(f"run {item.order_index} {item.run_id}")
            bundle = self.run_one(item, seed, ceiling=ceiling)
            spent, unknown = self.spent(self.bundles())
            rows.append({"run_id": item.run_id, "order_index": item.order_index, "state": "ran",
                         "outcome": bundle["outcome"]["status"]})
            self.log(f"  {bundle['outcome']['status']}" + (f" ({bundle['outcome']['reason']})"
                                                             if bundle["outcome"].get("reason") else "")
                     + f"; ${self.dollars(bundle):.4f}")
            if unknown:
                stopped = "eval_spend_unknown"
                self.log("stop: eval_spend_unknown (" + "; ".join(unknown[:3]) + ")")
                break
        return {"eval": self.eval_id, "spec_digest": self.registration["spec_digest"], "dry": dry,
                "preflight": ("not required" if gate is None else gate.get("refused", "passed")),
                "planned": len(planned), "runs": rows, "stopped": stopped, "spent_dollars": round(spent, 6),
                "total_dollars": budget["total_dollars"]}

    # One run

    def run_one(self, item: Planned, seed: bytes | None, ceiling: float | None = None) -> dict:
        from syberlabs.build import Kit
        from syberlabs.evolution import spend_total

        started_at, started = now_utc(), time.monotonic()
        arm = self.arms[item.arm]
        task = tasks.load_task(self.tasks_root, item.task)
        budget = dict(self.doc["budget"])
        if ceiling is not None:
            budget["dollars"] = ceiling
        mode = self.doc["hidden_feedback"]
        if self.work_root:
            self.work_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="syberlabs-eval-", dir=self.work_root))
        host_root = self._host_root()
        home, hidden = host_root / secrets.token_hex(16), host_root / secrets.token_hex(16)
        partial = self.runs_dir / (item.run_id + ".partial")
        if partial.exists():
            shutil.rmtree(partial)
        partial.mkdir(parents=True)
        repo_dir = work / "repo"
        expected = self.registration["task_digests"][item.task]
        hashes, sent = [], []
        kit = thread = staged = None
        charged = False

        def intact(point: str) -> bool:  # hash the copy again; True while nothing has changed since it was staged
            hashes.append(hidden_hash(staged, hashes[0]["files"], point))
            return not hashes[-1]["changed"]

        try:
            base = tasks.materialize(task, repo_dir)
            staged = tasks.stage_hidden(self.tasks_root, item.task, hidden, expected)
            hashes.append(hidden_hash(staged, None, "staged"))
            checks, required = tasks.contract_checks(task, staged)
            contract = run_contract(task, checks, required, budget)
            write_json(home / "contracts" / f"{CONTRACT_ID}.v1.json", contract)
            kit = Kit.local(home, repo_dir, actor=RUNNER_ACTOR)
            thread = kit.start(task["objective"], f"{CONTRACT_ID}.v1", paths=task["scope"]["paths"], act_class="R",
                               unattended=True)
            provider = arms.build(arm, repo=repo_dir, budget=budget, prices=budget["prices"], harness=self.harness,
                                  thread_label=thread.id, hidden_feedback=mode, sent=sent)
            with arms.environment(arm, task, withheld=(self.seed_env, DEFAULT_SEED_ENV)):
                thread.propose(provider, seed=item.seed, max_seconds=budget["seconds"])
            unchanged = intact("after_propose")
            events, views = thread.history(), thread._views()
            chosen = _chosen(events, views)
            verdict = receipt = None
            if chosen is not None and unchanged:
                verdict = thread.check(chosen)
                # Candidate code runs inside the hidden checks with the host's permissions, and the host's copy is
                # an ordinary directory: a copy that changed while they ran judged nothing, so nothing is accepted.
                if intact("after_check") and verdict.acceptable and intact("before_accept"):
                    receipt = thread.accept(chosen)
            events, views = thread.history(), thread._views()
            outcome = self._summary(events, views, chosen, verdict, receipt, provider, spend_total(events))
            integrity = hidden_record(expected, hashes)
            outcome["hidden_intact"], outcome["hidden_changed"] = not integrity["changed"], integrity["changed"][:16]
            if integrity["changed"]:
                outcome.update(status="not_acceptable", reason=HIDDEN_TAMPERED)
            certified = self._certify(kit, thread, chosen, receipt, seed, partial, repo_dir, home)
            outcome["certification"] = certified["state"]
            write_json(partial / "journal.json", kit.export(thread.id))
            write_json(partial / "contract.json", contract)
            write_json(partial / "trace.json", list(getattr(provider, "trace", None) or []))
            records = ["contract.json", "trace.json"]
            if arm["kind"] in FEEDBACK_KINDS:
                write_json(partial / "feedback.json", {"hidden_feedback": mode, "requests": sent})
                records.append("feedback.json")
            if _git_bundle(repo_dir, partial / "repo.bundle"):
                records.append("repo.bundle")
            ended_at = now_utc()
            bundle = {
                "schema": BUNDLE_SCHEMA_ID, "eval": self.eval_id, "run_id": item.run_id, "task": item.task,
                "arm": item.arm, "seed": item.seed, "order_index": item.order_index,
                "spec_digest": self.registration["spec_digest"],
                "task_digest": self.registration["task_digests"][item.task],
                "arm_digest": self.registration["arm_digests"][item.arm],
                "hidden_feedback": mode, "budget_ceiling": budget["dollars"],
                "hidden_integrity": integrity,
                "base": {"commit": base, "tree": kit.repo.tree(base), "source": task["source"]},
                "thread": thread.id,
                "harness": {**harness_state(self.harness), "eval": VERSION},
                "provider": {"kind": arm["kind"], "name": provider.name, "revision": provider.revision,
                             "class": type(provider).__name__, "model": arm["model"], "effort": arm["effort"],
                             "adapter_digests": {p.name: _sha256(p) for p in arms.adapter_files(arm, self.harness)}},
                "versions": {"python": sys.version.split()[0], "platform": platform.platform(terse=True),
                             "git": _version(["git", "--version"]),
                             "claude": (_version(arms.resolve(arm.get("executable") or ["claude"], self.harness)
                                                 + ["--version"]) if arm["kind"] == "claude-code" else None)},
                "started_at": started_at, "ended_at": ended_at, "seconds": round(time.monotonic() - started, 3),
                "journal_path": "journal.json", "certificate_path": certified["path"],
                "records_paths": records, "outcome": outcome,
                "work": ({"root": str(work), "kit": str(home), "hidden": str(staged)}
                         if self.keep_work else None),
            }
            bundle["credential_scan"] = credential_scan(partial)
            bundle["files"] = {p.name: _sha256(p) for p in sorted(partial.iterdir()) if p.is_file()}
            write_json(partial / "bundle.json", bundle)
            self._charge(item, thread, "completed", None, budget["dollars"])
            charged = True
        except BaseException as exc:
            if not charged:
                self._charge(item, thread, "aborted", exc, budget["dollars"])
            shutil.rmtree(partial, ignore_errors=True)
            raise
        finally:
            if kit is not None:
                kit.close()
            if not self.keep_work:
                shutil.rmtree(work, ignore_errors=True)
                shutil.rmtree(home, ignore_errors=True)
                tasks.release(hidden)
        final = self.runs_dir / item.run_id
        if final.exists():
            shutil.rmtree(final)
        partial.rename(final)
        return bundle

    @staticmethod
    def _summary(events, views, chosen, verdict, receipt, provider, spend) -> dict:
        finished = next((e["body"] for e in reversed(events) if e["kind"] == "search_finished"), {})
        view = next((v for v in views if v["id"] == chosen), None)
        checks = {c["name"]: c["state"] for c in (view or {}).get("evaluation", {}).get("checks", [])}
        if receipt is not None and receipt.status == "succeeded":
            status, reason = "accepted", None
        elif chosen is None:
            status, reason = "no_candidate", finished.get("error") or finished.get("stopped")
        elif receipt is not None:
            status, reason = "refused", receipt.reason
        else:
            status, reason = "not_acceptable", verdict.reason if verdict else None
        calls = getattr(provider, "calls", None)
        if calls is None and hasattr(provider, "mutate"):
            calls = getattr(provider.mutate, "calls", None)
        return {"status": status, "reason": reason, "candidate": chosen,
                "accepted_commit": receipt.commit if receipt is not None and receipt.status == "succeeded" else None,
                "checks": checks,
                "hidden": {name: state for name, state in checks.items() if name.startswith(tasks.HIDDEN_PREFIX)},
                "search": {"stopped": finished.get("stopped"), "error": finished.get("error"),
                           "recommended": finished.get("recommended", [])},
                "candidates": sum(1 for e in events if e["kind"] == "candidate_registered"),
                "evaluations": sum(1 for e in events if e["kind"] == "candidate_evaluated"),
                "refused_proposals": sum(1 for e in events if e["kind"] == "observed"
                                         and e["body"]["key"] == "proposal_refused"),
                "calls": calls, "dollars": spend["dollars"], "spend_seconds": spend["seconds"],
                "host_observed": spend["host_observed"], "unknown_models": spend["unknown_models"]}

    def _certify(self, kit, thread, chosen, receipt, seed, partial: Path, repo_dir: Path, home: Path) -> dict:
        if receipt is None or receipt.status != "succeeded":
            return {"state": "not_accepted", "path": None}
        if seed is None:
            return {"state": "uncertified", "path": None}
        from syberlabs import certificate
        from syberlabs.ed25519 import public_from_seed
        from syberlabs.gitspace import Repo

        envelope, _payload = certificate.certify(kit, thread.id, chosen, seed=seed, seed_env=self.seed_env)
        public = public_from_seed(seed)
        cert, _pub = certificate.write_certificate(envelope, public, partial / "certificate.json")
        report = certificate.verify_certificate(cert.read_bytes(), public, repo=Repo(repo_dir), home=home)
        if not report.passed:
            raise Rejected("certificate_unverified", f"the run's own certificate does not verify: {report}")
        return {"state": "certified:" + public.hex()[:10], "path": "certificate.json"}


def run_eval(evals_root: str | Path, eval_id: str, *, only: str | None = None, dry: bool = False,
             require_preflight: bool = True, **options) -> dict:
    return EvalRun(evals_root, eval_id, **options).run(only=only, dry=dry, require_preflight=require_preflight)


# Preflight

def _scrubbed_env(harness: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (DEFAULT_SEED_ENV,) + arms.WITHHELD_ENV}
    env["PYTHONPATH"] = str(harness)
    return env


def preflight(evals_root: str | Path, eval_id: str, *, harness: Path = HARNESS,
              commands: tuple = PREFLIGHT, log: Callable[[str], None] | None = None) -> dict:
    """Run ``commands`` (name, argv with ``{python}``) from the checkout and record ``evals/<id>/preflight.json``."""
    log = log or (lambda line: None)
    folder = Path(evals_root).resolve() / eval_id
    if not (folder / "spec.json").is_file():
        raise Rejected("spec_unknown", f"no spec at {folder / 'spec.json'}")
    state = harness_state(harness)
    started_at, results = now_utc(), []
    for name, argv in commands:
        resolved = [sys.executable if arg == "{python}" else arg for arg in argv]
        began = time.monotonic()
        with tempfile.TemporaryFile() as out:
            try:
                code = subprocess.run(resolved, cwd=harness, env=_scrubbed_env(Path(harness)), stdout=out,
                                      stderr=subprocess.STDOUT, timeout=PREFLIGHT_TIMEOUT).returncode
            except subprocess.TimeoutExpired:
                code = None
            except OSError as exc:
                out.write(f"could not start {resolved[0]!r}: {exc}\n".encode())
                code = None
            out.seek(0)
            data = out.read()
        results.append({"name": name, "argv": list(argv), "exit_code": code,
                        "seconds": round(time.monotonic() - began, 1),
                        "output_digest": hashlib.sha256(data).hexdigest(),
                        "tail": data.decode(errors="replace")[-TAIL:]})
        log(f"{name}: exit {code} ({results[-1]['seconds']}s)")
    passed = all(r["exit_code"] == 0 for r in results)
    doc = {"schema": PREFLIGHT_SCHEMA_ID, "eval": eval_id, "commit": state["commit"], "dirty": state["dirty"],
           "python": sys.version.split()[0], "platform": platform.platform(terse=True),
           "started_at": started_at, "ended_at": now_utc(), "passed": passed, "results": results}
    write_json(folder / "preflight.json", doc)
    return doc


def preflight_gate(evals_root: str | Path, eval_id: str, harness: Path = HARNESS) -> dict:
    """The recorded preflight, when it passed on the checkout's current commit with no change outside ``evals/``."""
    path = Path(evals_root).resolve() / eval_id / "preflight.json"
    if not path.is_file():
        raise Rejected("preflight_missing", f"run it first: syberlabs eval preflight {eval_id}")
    doc = read_json(path, "preflight_unreadable")
    if not isinstance(doc, dict) or doc.get("schema") != PREFLIGHT_SCHEMA_ID:
        raise Rejected("preflight_unreadable", f"{path} is not a preflight record")
    if not doc.get("passed"):
        failed = [r["name"] for r in doc.get("results", []) if r.get("exit_code") != 0]
        raise Rejected("preflight_failed", "the recorded preflight failed: " + ", ".join(failed))
    state = harness_state(harness)
    if not state["commit"] or doc.get("commit") != state["commit"]:
        raise Rejected("preflight_stale", f"the preflight ran on {str(doc.get('commit'))[:10]}; HEAD is "
                       f"{str(state['commit'])[:10]}: run syberlabs eval preflight {eval_id} again")
    if doc.get("dirty") is not False or state["dirty"] is not False:
        raise Rejected("preflight_dirty", "the checkout differs from its commit outside evals/; commit or stash, "
                       "then run the preflight again")
    return doc
