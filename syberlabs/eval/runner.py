"""The runner: every (task, arm, seed) of a registered spec through a real unattended Build Thread, one bundle each.

``run_eval`` refuses a spec that is not exactly as registered (``spec.registered``) and, unless told it is a dry
run, a checkout whose preflight for the current commit did not pass (``preflight_gate``). It expands the spec's
tasks x arms x seeds in spec order and shuffles them with ``order_seed`` (``plan``; a Fisher-Yates shuffle drawn
from ``random.Random(order_seed).random()``, whose sequence Python guarantees across versions). Each run:

1. a fresh work directory: the run's repository at the task's pinned base (``tasks.materialize``), a kit home
   outside it, and the host's copy of the task's hidden files outside both (``tasks.stage_hidden``);
2. the run contract ``eval-run.v1`` (``run_contract``): the task's scope, its visible checks and its hidden checks
   as required checks ``hidden.<name>`` by absolute path, the spec's price table, ``max_dollars`` the spec's per-run
   dollars, ``max_seconds`` its seconds; one action, ``accept_change``, and no publishing action at all;
3. ``Kit.start(..., unattended=True)`` as ``syberlabs.eval``; the arm's provider (``arms.build``) proposes with the
   run's seed, in the arm's environment (``arms.environment``), the host's governor pricing every call;
4. the candidate the search recommends (else its last in-scope candidate) is checked by the host
   (``Thread.check``: every check on the exact candidate tree) and accepted only if admission allows it
   (``Thread.accept``, which moves the thread's branch in the run's own repository and nothing else) and the
   host's copy of the hidden files is still what was staged (else ``not_acceptable``, ``hidden_tampered``: candidate
   code runs inside the checks with the host's permissions; this detects a rewrite that stays, not one undone);
5. evidence: the exported journal, the provider's trace, the run contract, a ``git bundle`` of the run's
   repository, and, when the run accepted a candidate and the seed variable is set, a certificate signed with that
   key and verified at once against the repository and the journal (``uncertified`` otherwise);
6. ``evals/<id>/runs/<run_id>/bundle.json``: the digests of the spec, task and arm, the seed, the order index, the
   base, the harness, provider, Python, git and (for a claude-code arm) Claude Code versions, the times, the paths
   of the evidence files with every file's SHA-256, and the outcome.

A run's files are written into ``<run_id>.partial`` and renamed when complete, so a run killed midway leaves no
bundle and is run again; a run with a bundle is skipped (resumable by run id). A bundle is read only when it is the
evidence of the run its directory names (``EvalRun.verify``; ``bundle_tampered`` otherwise). Before a run starts,
the dollars the eval's bundles record, plus what every run that raised had spent (``runs/failed.jsonl``, written
as it raised), plus the spec's per-run dollars must fit under ``budget.total_dollars``, or the eval stops
``eval_budget_exhausted``; within a run the governor's bound applies (the call that crosses ``max_dollars`` is
recorded and the next is refused, ``docs/BUILD_THREAD.md``). A run whose spend names a model the price table does
not price stops the eval (``eval_spend_unknown``): its cost cannot be counted against the cap. One runner holds an
eval at a time (``runs/.lock``; a second is refused ``eval_running``), and the preflight gate is asked again before
every run, so a checkout that moves or changes mid-eval stops it.

``preflight`` runs the deterministic suites (``scripts/run_shards.py``, ``conformance.run``,
``benchmarks/run_scenarios.py``, and this package's tests) from the checkout and records the result for the
commit in ``evals/<id>/preflight.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

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
FAILED = "failed.jsonl"       # runs/failed.jsonl: what each run that raised had spent, counted under the cap
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


class EvalRun:
    """One eval: the registered spec, the order, the cap, and the runs."""

    def __init__(self, evals_root: str | Path, eval_id: str, *, harness: Path = HARNESS, work_root: Path | None = None,
                 keep_work: bool = False, seed_env: str = DEFAULT_SEED_ENV, log: Callable[[str], None] | None = None):
        self.root = Path(evals_root).resolve()
        self.eval_id, self.harness, self.keep_work, self.seed_env = eval_id, Path(harness), keep_work, seed_env
        self.work_root = Path(work_root) if work_root else None
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

    def failed(self) -> list[dict]:
        """The runs that raised after they started (``failed.jsonl``, ``run_one``): what each had spent."""
        path = self.runs_dir / FAILED
        rows = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                if not isinstance(row, dict):
                    raise Rejected("ledger_unreadable", f"{path}: a line is not a record; the spend of a failed run "
                                   "cannot be counted")
                rows.append(row)
        return rows

    @staticmethod
    def dollars(bundle: dict) -> float:
        return float((bundle.get("outcome") or {}).get("dollars") or 0.0)

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
        done, failed = self.bundles(), self.failed()
        budget = self.doc["budget"]
        # The cap counts what the bundles record and what every run that raised had spent before it raised.
        spent = sum(self.dollars(b) for b in done.values()) + sum(float(f.get("dollars") or 0.0) for f in failed)
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
            if any(f.get("dollars") is None or f.get("unknown_models") for f in failed):
                stopped = "eval_spend_unknown"
                self.log("stop: eval_spend_unknown (a run that raised spent what the price table cannot count)")
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
            self.log(f"run {item.order_index} {item.run_id}")
            bundle = self.run_one(item, seed)
            spent += self.dollars(bundle)
            rows.append({"run_id": item.run_id, "order_index": item.order_index, "state": "ran",
                         "outcome": bundle["outcome"]["status"]})
            self.log(f"  {bundle['outcome']['status']}" + (f" ({bundle['outcome']['reason']})"
                                                             if bundle["outcome"].get("reason") else "")
                     + f"; ${self.dollars(bundle):.4f}")
            if bundle["outcome"].get("unknown_models"):
                stopped = "eval_spend_unknown"
                self.log("stop: eval_spend_unknown (a model the price table does not price was used)")
                break
        return {"eval": self.eval_id, "spec_digest": self.registration["spec_digest"], "dry": dry,
                "preflight": ("not required" if gate is None else gate.get("refused", "passed")),
                "planned": len(planned), "runs": rows, "stopped": stopped, "spent_dollars": round(spent, 6),
                "total_dollars": budget["total_dollars"]}

    # One run

    def run_one(self, item: Planned, seed: bytes | None) -> dict:
        from syberlabs.build import Kit
        from syberlabs.evolution import spend_total

        started_at, started = now_utc(), time.monotonic()
        arm = self.arms[item.arm]
        task = tasks.load_task(self.tasks_root, item.task)
        budget = self.doc["budget"]
        if self.work_root:
            self.work_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="syberlabs-eval-", dir=self.work_root))
        partial = self.runs_dir / (item.run_id + ".partial")
        if partial.exists():
            shutil.rmtree(partial)
        partial.mkdir(parents=True)
        repo_dir, home, host = work / "repo", work / "kit", work / "host"
        kit = thread = None
        try:
            base = tasks.materialize(task, repo_dir)
            staged = tasks.stage_hidden(self.tasks_root, item.task, host / tasks.HIDDEN,
                                        self.registration["task_digests"][item.task])
            intact = tasks.tree_digests(staged)
            checks, required = tasks.contract_checks(task, staged)
            contract = run_contract(task, checks, required, budget)
            write_json(home / "contracts" / f"{CONTRACT_ID}.v1.json", contract)
            kit = Kit.local(home, repo_dir, actor=RUNNER_ACTOR)
            thread = kit.start(task["objective"], f"{CONTRACT_ID}.v1", paths=task["scope"]["paths"], act_class="R",
                               unattended=True)
            provider = arms.build(arm, repo=repo_dir, budget=budget, prices=budget["prices"], harness=self.harness,
                                  thread_label=thread.id)
            with arms.environment(arm, task, withheld=(self.seed_env, DEFAULT_SEED_ENV)):
                thread.propose(provider, seed=item.seed, max_seconds=budget["seconds"])
            events, views = thread.history(), thread._views()
            chosen = _chosen(events, views)
            verdict = receipt = None
            if chosen is not None:
                verdict = thread.check(chosen)
            # Candidate code runs inside the hidden checks with the host's permissions, and the host's copy is an
            # ordinary directory: a copy that changed while they ran judged nothing, so nothing is accepted on it.
            changed = tasks.hidden_changes(intact, tasks.tree_digests(staged))
            if verdict is not None and verdict.acceptable and not changed:
                receipt = thread.accept(chosen)
            events, views = thread.history(), thread._views()
            outcome = self._summary(events, views, chosen, verdict, receipt, provider, spend_total(events))
            outcome["hidden_intact"], outcome["hidden_changed"] = not changed, changed[:16]
            if changed and chosen is not None:
                outcome.update(status="not_acceptable", reason=HIDDEN_TAMPERED)
            certified = self._certify(kit, thread, chosen, receipt, seed, partial, repo_dir, home)
            outcome["certification"] = certified["state"]
            write_json(partial / "journal.json", kit.export(thread.id))
            write_json(partial / "contract.json", contract)
            write_json(partial / "trace.json", list(getattr(provider, "trace", None) or []))
            records = ["contract.json", "trace.json"]
            if _git_bundle(repo_dir, partial / "repo.bundle"):
                records.append("repo.bundle")
            ended_at = now_utc()
            bundle = {
                "schema": BUNDLE_SCHEMA_ID, "eval": self.eval_id, "run_id": item.run_id, "task": item.task,
                "arm": item.arm, "seed": item.seed, "order_index": item.order_index,
                "spec_digest": self.registration["spec_digest"],
                "task_digest": self.registration["task_digests"][item.task],
                "arm_digest": self.registration["arm_digests"][item.arm],
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
                "work": str(work) if self.keep_work else None,
            }
            bundle["files"] = {p.name: _sha256(p) for p in sorted(partial.iterdir()) if p.is_file()}
            write_json(partial / "bundle.json", bundle)
        except BaseException as exc:
            shutil.rmtree(partial, ignore_errors=True)
            self._failed(item, thread, exc)
            raise
        finally:
            if kit is not None:
                kit.close()
            if not self.keep_work:
                shutil.rmtree(work, ignore_errors=True)
        final = self.runs_dir / item.run_id
        if final.exists():
            shutil.rmtree(final)
        partial.rename(final)
        return bundle

    def _failed(self, item: Planned, thread, exc: BaseException) -> None:
        """Append what a run that raised had spent to ``failed.jsonl``, which the cap counts as it counts bundles:
        nothing was spent before its thread started; ``dollars`` is null when its journal cannot be read. A runner
        killed outright (SIGKILL) writes nothing here (``spec/GAPS.md``)."""
        from syberlabs.evolution import spend_total

        spend = {"dollars": 0.0, "unknown_models": []}
        if thread is not None:
            try:
                spend = spend_total(thread.history())
            except Exception:
                spend = {"dollars": None, "unknown_models": []}
        row = {"run_id": item.run_id, "dollars": spend["dollars"], "unknown_models": spend["unknown_models"],
               "error": exc.code if isinstance(exc, Rejected) else type(exc).__name__, "at": now_utc()}
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        with open(self.runs_dir / FAILED, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

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
