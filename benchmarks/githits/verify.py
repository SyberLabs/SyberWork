"""Recheck saved Git objects on the host, with no model or GitHits account.

Check outputs contain elapsed times and temporary paths; replay compares
states and exit codes, not output hashes. Original evidence stays unchanged.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.githits.client import digest
from benchmarks.githits.fixtures import FIXTURES
from benchmarks.githits.run import ROOT, contract, write_json
from examples.evolve import git
from syberlabs import checks
from syberlabs.events import verify_events
from syberlabs.gitspace import Repo


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify_manifest(folder: Path) -> int:
    expected = load(folder / "sha256.json")
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*")
              if p.is_file() and p.name != "sha256.json"}
    if actual != set(expected):
        raise ValueError("evidence_file_set_changed")
    for name, sha in expected.items():
        target = (folder / name).resolve()
        if not target.is_relative_to(folder.resolve()) or digest(target.read_bytes()) != sha:
            raise ValueError("evidence_digest_mismatch:" + name)
    return len(expected)


def same_states(original: dict, repeated: dict) -> bool:
    return {c["name"]: (c["state"], c["exit_code"]) for c in original["checks"]} == {
        c["name"]: (c["state"], c["exit_code"]) for c in repeated["checks"]}


def verify_bundle(name: str, folder: Path, python: str, broken: dict | None = None) -> list[dict]:
    events = load(folder / "events.json")
    if not verify_events(events):
        raise ValueError("invalid_event_chain")
    registered = {e["body"]["id"]: e["body"] for e in events if e["kind"] == "candidate_registered"}
    evaluations = [e["body"] for e in events if e["kind"] == "candidate_evaluated"]
    repeated = []
    with tempfile.TemporaryDirectory(prefix="githits-recheck-") as tmp:
        root = Path(tmp) / "repo"
        git(Path(tmp), "clone", "-q", "--config", "core.autocrlf=false", str(folder / "repository.bundle"), str(root))
        repo = Repo(root)
        base = repo.rev("HEAD")
        if (repo.read(base, "src/integration.py").decode().replace("\r\n", "\n") != FIXTURES[name]["broken"]
                or repo.read(base, "checks.py").decode().replace("\r\n", "\n") != FIXTURES[name]["checks"]
                or repo.read(base, "requirements.txt").decode().replace("\r\n", "\n") !=
                (ROOT / "benchmarks/githits/requirements.txt").read_text()):
            raise ValueError("fixture_base_changed")
        evaluation = contract(name, python, max(1, len(evaluations)))["evolution"]["evaluation"]
        if broken:
            found = checks.evaluate(repo, {"id": "base", "commit": base, "tree": repo.tree(base)}, evaluation)
            if base != broken["commit"] or not same_states(broken, found):
                raise ValueError("broken_baseline_did_not_reproduce")
            repeated.append(found)
        for original in evaluations:
            candidate = registered[original["candidate"]]
            if (candidate["base"] != base or candidate["commit"] != original["commit"]
                    or repo.tree(candidate["commit"]) != original["tree"]
                    or candidate["tree"] != original["tree"]
                    or repo.changed_paths(base, candidate["commit"]) != ["src/integration.py"]):
                raise ValueError("candidate_identity_or_scope_mismatch")
            found = checks.evaluate(repo, {"id": candidate["id"], **original}, evaluation)
            if not same_states(original, found):
                raise ValueError("candidate_result_did_not_reproduce")
            repeated.append(found)
    return repeated


def verify(folder: Path, python: str) -> dict:
    files = verify_manifest(folder)
    environment = load(folder / "environment.json")
    if environment.get("suite") == "hard-v2":
        from benchmarks.githits.preregister import check_registration, registered_contexts
        registration = check_registration(load(folder / "preregistration.json"))
        if digest(json.dumps(registration, sort_keys=True).encode()) != environment["preregistration_sha256"]:
            raise ValueError("preregistration_receipt_changed")
        for name, context in registered_contexts(registration).items():
            if load(folder / (name + "-context.json")) != context:
                raise ValueError("registered_context_changed:" + name)
    observations = []
    for validation in load(folder / "validation.json"):
        name = validation["fixture"]
        repeated = verify_bundle(name, folder / (name + "-validation"), python, validation["broken"])
        observations.append({"fixture": name, "arm": "validation", "evaluations": repeated})
    for row in load(folder / "results.json"):
        if row["status"] != "completed":
            continue
        repeated = verify_bundle(row["fixture"], folder / row["evidence"], python)
        solved = any(all(c["state"] == "passed" for c in e["checks"]) for e in repeated)
        if solved != row["solved"]:
            raise ValueError("summary_disagrees_with_host_checks")
        observations.append({"fixture": row["fixture"], "arm": row["arm"], "evaluations": repeated})
    return {"status": "verified", "at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(), "python_checks": python, "evidence_files": files,
            "source_manifest_sha256": digest((folder / "sha256.json").read_bytes()),
            "observations": observations}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--write", type=Path, required=True, help="new output file outside original evidence directory")
    args = parser.parse_args()
    if (args.write.exists() or args.write.resolve().is_relative_to(args.folder.resolve())
            or not Path(args.python).is_absolute() or not Path(args.python).is_file()):
        parser.error("name an existing absolute interpreter and a new output outside the original evidence directory")
    result = verify(args.folder.resolve(), args.python)
    write_json(args.write, result)
    print(f"Verified {result['evidence_files']} evidence files and {len(result['observations'])} fixture/arm records.")


if __name__ == "__main__":
    main()
