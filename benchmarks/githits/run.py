"""Compare the same bounded patch provider with and without pinned GitHits context.

Run from the checkout: python -m benchmarks.githits.run --help
This extends model_vs_patch's repair arm and JSON row vocabulary. It never
accepts, pushes or merges a candidate. All solve decisions are host owned.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.githits.client import collect, digest
from benchmarks.githits.fixtures import FIXTURES, LEGACY_FIXTURES, HARD_FIXTURES
from benchmarks.model_vs_patch import Patches
from examples.evolve import git
from syberlabs import Kit
from syberlabs import checks
from syberlabs.events import verify_events
from syberlabs.providers import PatchProvider

ROOT = Path(__file__).resolve().parents[2]
ARMS = ("without_githits", "with_githits")
LAUNCH_CHECK = "import runpy,sys; sys.path.insert(0,'src'); runpy.run_path('checks.py',run_name='__main__')"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def contract(name: str, python: str, calls: int) -> dict:
    fixture = FIXTURES[name]
    identity = ("task-" if fixture.get("blind") else "githits-") + name
    return {"id": identity, "version": 1, "title": fixture["objective"],
            "inputs": {"objective": "string"}, "actions": {"accept_change": {}},
            "acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
            "evolution": {
                "scope": {"paths": ["src/"], "max_files": 1, "max_diff_bytes": 40_000},
                "operators": ["patch"],
                "budget": {"max_candidates": calls, "max_evaluations": calls, "max_seconds": calls * 600},
                "evaluation": {"checks": {
                    "dependency_pin": {"argv": [python, "-I", "-c", "import importlib.metadata as m; "
                        f"assert m.version({fixture['package']!r}) == {fixture['version']!r}"], "timeout_seconds": 30},
                    "behavior": {"argv": [python, "-I", "-c", LAUNCH_CHECK], "timeout_seconds": 30}},
                    "required": ["dependency_pin", "behavior"], "max_output_bytes": 64_000},
                "promotion": {"action": "accept_change", "roles": ["developer"]}}}


def create_base(name: str, root: Path) -> str:
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/integration.py").write_text(FIXTURES[name]["broken"], encoding="utf-8")
    (root / "checks.py").write_text(FIXTURES[name]["checks"], encoding="utf-8")
    shutil.copyfile(ROOT / "benchmarks/githits/requirements.txt", root / "requirements.txt")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Authored dependency integration regression: " + name)
    return git(root, "rev-parse", "HEAD")


def open_thread(name: str, root: Path, python: str, calls: int):
    home = root / ".syberlabs"
    doc = contract(name, python, calls)
    write_json(home / "contracts" / f"{doc['id']}.v1.json", doc)
    kit = Kit.local(home, root, actor="benchmark-host")
    thread = kit.start(FIXTURES[name]["objective"], f"{doc['id']}.v1", paths=["src", "requirements.txt"])
    return kit, thread, doc


def export(kit, thread, folder: Path) -> None:
    events = thread.history()
    if not verify_events(events):
        raise RuntimeError("invalid_host_event_chain")
    write_json(folder / "events.json", events)
    write_json(folder / "candidates.json", [dataclasses.asdict(c) for c in thread.candidates()])
    shutil.copytree(kit.home, folder / "host", dirs_exist_ok=True)
    # Candidate refs are intentionally not pushed. The bundle preserves their objects.
    git(kit.repo.root, "bundle", "create", str((folder / "repository.bundle").resolve()), "--all")


def validate_fixture(name: str, root: Path, evidence: Path, python: str) -> dict:
    kit, thread, doc = open_thread(name, root, python, 2)
    try:
        base = kit.repo.rev("HEAD")
        broken = checks.evaluate(kit.repo, {"id": "base", "commit": base, "tree": kit.repo.tree(base)},
                                 doc["evolution"]["evaluation"])
        by_name = {item["name"]: item for item in broken["checks"]}
        if by_name["dependency_pin"]["state"] != "passed" or by_name["behavior"]["state"] != "failed":
            raise RuntimeError(f"invalid_broken_baseline:{name}")
        [reference] = thread.propose(PatchProvider({"src/integration.py": FIXTURES[name]["reference"]},
                                                 name="reference", revision="authored-v1"))
        verdict = thread.check(reference.id)
        if not verdict.acceptable:
            raise RuntimeError(f"invalid_reference_fix:{name}")
        result = {"fixture": name, "base_commit": base, "base_tree": kit.repo.tree(base),
                  "broken": broken, "reference": dataclasses.asdict(thread.candidates()[0]),
                  "reference_acceptable": True, "source_url": FIXTURES[name]["source_url"]}
        write_json(evidence / "validation.json", result)
        export(kit, thread, evidence)
        return result
    finally:
        kit.close()


def run_arm(name: str, arm: str, seed: int, root: Path, evidence: Path, python: str,
            calls: int, model: str, effort: str, context: dict) -> dict:
    fixture = FIXTURES[name]
    row = {"fixture": name, "arm": arm, "seed": seed, "solved": None, "calls": 0,
           "input_tokens": 0, "output_tokens": 0, "dollars": None, "simulated": False,
           "candidates": 0, "evaluations": 0, "seconds": 0.0, "model_requested": model,
           "effort": effort, "package": fixture["package"], "version": fixture["version"],
           "evidence": evidence.name}
    if arm == "with_githits" and context["status"] != "ready":
        return {**row, "status": "unavailable", "reason": context["reason"]}
    evidence.mkdir(parents=True)
    kit, thread, doc = open_thread(name, root, python, calls)
    argv = [sys.executable, str(ROOT / "benchmarks/githits/codex_adapter.py"),
            "--record", str(evidence / "model"), "--model", model, "--effort", effort]
    if fixture.get("blind"):
        argv += ["--blind"]
    if arm == "with_githits":
        context_file = evidence / "context.json"
        write_json(context_file, context["context"])
        argv += ["--context", str(context_file)]
    provider = Patches(argv, calls, True, seed)
    provider.name, provider.revision = "codex-repair", model
    started, prior = time.monotonic(), Path.cwd()
    try:
        os.chdir(root)
        thread.propose(provider, seed=seed)
        passing = [c for c in thread.candidates() if c.evaluation == "passed"]
        solved = bool(passing) and thread.check(passing[0].id, run=False).acceptable
        events = thread.history()
        finished = [e["body"] for e in events if e["kind"] == "search_finished"][-1]
        state = "completed" if finished["error"] is None else "provider_error"
        row.update(status=state, reason=finished["error"], solved=solved if state == "completed" else None,
                   calls=provider.used, input_tokens=sum(u.get("input_tokens", 0) for u in provider.usage),
                   output_tokens=sum(u.get("output_tokens", 0) for u in provider.usage),
                   candidates=sum(e["kind"] == "candidate_registered" for e in events),
                   evaluations=sum(e["kind"] == "candidate_evaluated" for e in events),
                   seconds=round(time.monotonic() - started, 2), base_commit=thread.base,
                   base_tree=kit.repo.tree(thread.base),
                   solved_candidate=passing[0].id if solved else None)
        write_json(evidence / "contract.json", doc)
        export(kit, thread, evidence)
        write_json(evidence / "result.json", row)
        return row
    finally:
        os.chdir(prior)
        kit.close()


def summarize(rows: list[dict], calls: int) -> str:
    pairs = {}
    for row in rows:
        pairs.setdefault((row["fixture"], row["seed"]), {})[row["arm"]] = row
    complete = [p for p in pairs.values() if all(p.get(a, {}).get("status") == "completed" for a in ARMS)]
    lines = [f"Budget K = {calls} model calls per arm; stop at first host-verified acceptable candidate.",
             "Authored integration regressions against pinned packages; exploratory, not representative.",
             f"Completed matched pairs: {len(complete)}/{len(pairs)}."]
    if complete:
        without = sum(p[ARMS[0]]["solved"] for p in complete)
        with_context = sum(p[ARMS[1]]["solved"] for p in complete)
        wins = sum(p[ARMS[1]]["solved"] and not p[ARMS[0]]["solved"] for p in complete)
        losses = sum(p[ARMS[0]]["solved"] and not p[ARMS[1]]["solved"] for p in complete)
        lines += [f"Without GitHits: {without}/{len(complete)}; with GitHits: {with_context}/{len(complete)}.",
                  f"Discordant pairs: GitHits-only solves {wins}, baseline-only solves {losses}.",
                  "This small convenience sample does not establish a general improvement."]
    else:
        lines += ["No comparative result. GitHits improvement, regression and parity are unmeasured."]
    for row in rows:
        solve = str(row["solved"]) if row["solved"] is not None else "n/a"
        lines.append(f"{row['fixture']} {row['arm']} seed={row['seed']}: {row['status']}; "
                     f"solved={solve}; calls={row['calls']}; reason={row.get('reason') or 'none'}")
    return "\n".join(lines) + "\n"


def manifest(folder: Path) -> None:
    write_json(folder / "sha256.json", {p.relative_to(folder).as_posix(): digest(p.read_bytes())
               for p in sorted(folder.rglob("*")) if p.is_file() and p.name != "sha256.json"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", required=True, help="absolute interpreter with pinned benchmark dependencies")
    parser.add_argument("--write", type=Path, required=True, help="new evidence directory; must not exist")
    parser.add_argument("--calls", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--repeats", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--suite", choices=("original", "hard-v2"), default="original")
    parser.add_argument("--fixtures", nargs="+", choices=list(FIXTURES))
    parser.add_argument("--validate-only", action="store_true", help="no model calls; still records GitHits access preflight")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="high", choices=("low", "medium", "high", "xhigh"))
    args = parser.parse_args()
    args.fixtures = args.fixtures or list(HARD_FIXTURES if args.suite == "hard-v2" else LEGACY_FIXTURES)
    if args.suite == "original" and any(name in HARD_FIXTURES for name in args.fixtures):
        parser.error("hard-v2 tasks require the frozen --suite hard-v2 procedure")
    preregistration = None
    if args.suite == "hard-v2":
        from benchmarks.githits.preregister import verify_registration
        if (args.fixtures != list(HARD_FIXTURES) or args.calls != 2 or args.repeats != 1
                or args.model != "gpt-6.1-sol" or args.effort != "high"):
            parser.error("hard-v2 must use its complete frozen task set and preregistered configuration")
        preregistration = verify_registration()
        if git(ROOT, "diff", "HEAD"):
            parser.error("commit the frozen hard-v2 implementation before running")
    if not Path(args.python).is_absolute() or not Path(args.python).is_file():
        parser.error("--python must be an existing absolute interpreter path")
    packages = json.loads(subprocess.run([args.python, "-m", "pip", "list", "--format=json"],
                                        check=True, capture_output=True).stdout)
    installed = {item["name"].lower().replace("_", "-"): item["version"] for item in packages}
    for pin in (ROOT / "benchmarks/githits/requirements.txt").read_text().splitlines():
        name, version = pin.split("==")
        if installed.get(name.lower().replace("_", "-")) != version:
            parser.error(f"benchmark dependency is not pinned: {name}=={version}")
    output = args.write.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Hash the actual artifact bytes, preserving them through Windows Git checkouts.
    (output / ".gitattributes").write_bytes(b"* -text -whitespace\n")
    if preregistration:
        write_json(output / "preregistration.json", preregistration)
    saved_contexts = None
    if preregistration:
        from benchmarks.githits.preregister import registered_contexts
        saved_contexts = registered_contexts(preregistration)
    rows, validations, contexts = [], [], {}
    write_json(output / "environment.json", {
        "at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(), "machine": platform.machine(),
        "python_host": sys.version, "python_checks": args.python, "model_requested": args.model,
        "model_effort": args.effort, "calls_per_arm": args.calls, "repeats": args.repeats,
        "order_seed": 20261015, "source_commit": git(ROOT, "rev-parse", "HEAD"),
        "suite": args.suite,
        "preregistration_sha256": digest(json.dumps(preregistration, sort_keys=True).encode()) if preregistration else None,
        "source_diff_sha256": digest(git(ROOT, "diff", "HEAD").encode()),
        "requirements_sha256": digest((ROOT / "benchmarks/githits/requirements.txt").read_bytes()),
        "packages": packages})
    try:
        with tempfile.TemporaryDirectory(prefix="githits-bench-") as folder:
            work = Path(folder)
            for name in args.fixtures:
                base = work / (name + "-base")
                create_base(name, base)
                validations.append(validate_fixture(name, base, output / (name + "-validation"), args.python))
                contexts[name] = saved_contexts[name] if saved_contexts is not None else collect(FIXTURES[name])
                write_json(output / (name + "-context.json"), contexts[name])
            write_json(output / "validation.json", validations)
            if preregistration:
                unavailable = {name: c["reason"] for name, c in contexts.items() if c["status"] != "ready"}
                if unavailable != preregistration["allowed_unavailable"]:
                    raise RuntimeError("unregistered_context_availability_no_models_invoked")
            if not args.validate_only:
                order = [(name, seed, list(ARMS)) for seed in range(1, args.repeats + 1) for name in args.fixtures]
                rng = random.Random(20261015)
                for name, seed, arms in order:
                    rng.shuffle(arms)
                    for arm in arms:
                        label = f"cell-{len(rows) + 1:02}" if args.suite == "hard-v2" else f"{name}-{arm}-{seed}"
                        root = work / label
                        # Both arms clone the same base commit; reference candidate refs stay out.
                        git(work, "clone", "-q", "--no-hardlinks", str(work / (name + "-base")), str(root))
                        row = run_arm(name, arm, seed, root, output / label, args.python, args.calls,
                                      args.model, args.effort, contexts[name])
                        rows.append(row)
                        write_json(output / "results.json", rows)
                        (output / "results.txt").write_text(summarize(rows, args.calls), encoding="utf-8")
                        if args.suite == "hard-v2":
                            print(f"Completed cell {len(rows)}/{len(args.fixtures) * 2}; outcomes withheld until matrix completion.", flush=True)
                        else:
                            print(f"{label}: {row['status']} solved={row['solved']}", flush=True)
    except BaseException:
        write_json(output / "interrupted.json", {"completed_rows": len(rows), "status": "incomplete"})
        raise
    finally:
        manifest(output)
    print(summarize(rows, args.calls) if rows else "Fixture validation completed; no model comparison run.")


if __name__ == "__main__":
    main()
