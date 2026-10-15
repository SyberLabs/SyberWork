"""Preregister, run and replay upstream-derived bugs using the existing host runner."""

from __future__ import annotations

import argparse
import json
import platform
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.githits import run, verify
from benchmarks.githits.client import API, collect, digest, normalize_name, validate_read
from benchmarks.githits.upstream_fixtures import DATA, UPSTREAM_FIXTURES
from examples.evolve import git
from urllib.parse import urlencode

ROOT = run.ROOT
REGISTRATION = ROOT / "benchmarks/githits/PREREGISTRATION-upstream-v3.json"
REQUIREMENTS = ROOT / "benchmarks/githits/requirements-upstream-v3.txt"
CONFIGURATION = {"model": "gpt-6.1-sol", "effort": "high", "calls_per_arm": 2,
                 "repeats": 1, "order_seed": 20261015, "planned_pairs": 4, "max_model_calls": 16}


def definitions():
    return {name: digest(json.dumps(fixture, sort_keys=True).encode())
            for name, fixture in UPSTREAM_FIXTURES.items()}


def source_hashes():
    files = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py").splitlines()
    paths = {ROOT / name for name in files}
    paths.update(path for path in DATA.iterdir() if path.is_file())
    paths.update([REQUIREMENTS, ROOT / "benchmarks/githits/requirements.txt",
                  ROOT / "benchmarks/githits/PREREGISTRATION-upstream-v3.md"])
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes().replace(b"\r\n", b"\n"))
            for p in sorted(paths)}


def freeze(path):
    if path.exists():
        raise ValueError("refuse_to_overwrite_registration")
    run.write_json(path, {"suite": "upstream-v3", "registered_at": datetime.now(timezone.utc).isoformat(),
        "parent_commit": git(ROOT, "rev-parse", "HEAD"), "configuration": CONFIGURATION,
        "fixture_sha256": definitions(), "source_sha256_lf": source_hashes(),
        "prior_ties": [{"suite": "original", "without": 3, "with": 3, "pairs": 3},
                       {"suite": "hard-v2", "without": 5, "with": 5, "pairs": 5, "planned": 6}],
        "availability_rule": "all baselines; matched score only completed pairs; no replacement or fallback",
        "blinding": "hidden checks/references, opaque cells, unbranded context, arm-unaware checks; operator unblinded"})


def registration(saved=None):
    value = verify.load(REGISTRATION)
    if (value.get("suite") != "upstream-v3" or value.get("configuration") != CONFIGURATION
            or value.get("fixture_sha256") != definitions()
            or value.get("source_sha256_lf") != source_hashes()
            or (saved is not None and value != saved)):
        raise ValueError("upstream_registration_changed")
    if verify.load(REGISTRATION) != json.loads(git(ROOT, "show", "HEAD:" + REGISTRATION.relative_to(ROOT).as_posix())):
        raise ValueError("registration_must_be_committed_before_trials")
    return value


def install_fixtures():
    # The shared dictionary is the existing runner/replayer's fixture seam.
    run.FIXTURES.update(UPSTREAM_FIXTURES)


def check_interpreter(python):
    if not Path(python).is_absolute() or not Path(python).is_file():
        raise ValueError("absolute_existing_interpreter_required")
    packages = json.loads(subprocess.run([python, "-m", "pip", "list", "--format=json"],
                                        check=True, capture_output=True).stdout)
    installed = {p["name"].lower().replace("_", "-"): p["version"] for p in packages}
    for pin in REQUIREMENTS.read_text().splitlines():
        name, version = pin.split("==")
        if installed.get(name.lower().replace("_", "-")) != version:
            raise ValueError("dependency_pin_mismatch:" + pin)
    return packages


def begin(output, python, phase):
    frozen = registration()
    packages = check_interpreter(python)
    if git(ROOT, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("clean_committed_checkout_required")
    output.mkdir(parents=True, exist_ok=False)
    (output / ".gitattributes").write_bytes(b"* -text -whitespace\n")
    run.write_json(output / "preregistration.json", frozen)
    run.write_json(output / "environment.json", {"suite": "upstream-v3", "phase": phase,
        "at": datetime.now(timezone.utc).isoformat(), "source_commit": git(ROOT, "rev-parse", "HEAD"),
        "source_diff_sha256": digest(git(ROOT, "diff", "HEAD").encode()),
        "configuration": CONFIGURATION, "packages": packages, "python_checks": python,
        "platform": platform.platform(), "python_host": sys.version,
        "preregistration_sha256": digest(json.dumps(frozen, sort_keys=True).encode())})


def preflight(output, python):
    begin(output, python, "preflight")
    validations = []
    run.write_json(output / "results.json", [])
    try:
        with tempfile.TemporaryDirectory(prefix="upstream-preflight-") as folder:
            for name, fixture in UPSTREAM_FIXTURES.items():
                root = Path(folder) / name
                run.create_base(name, root)
                validations.append(run.validate_fixture(name, root, output / (name + "-validation"), python))
                receipt = collect(fixture)
                if receipt["status"] == "ready":
                    try:
                        for excerpt in receipt["context"]:
                            check_upstream_excerpt(fixture, excerpt)
                    except ValueError:
                        receipt = {**receipt, "status": "unavailable", "reason": "upstream_snapshot_mismatch", "context": None}
                run.write_json(output / (name + "-context.json"), receipt)
        run.write_json(output / "validation.json", validations)
    finally:
        run.manifest(output)
    print("Preflight complete; zero model calls. Context availability retained without replacements.")


def check_upstream_excerpt(fixture, excerpt):
    served = excerpt["target_resolution"]["served"]
    if (served.get("commit_sha") != fixture["provenance"]["tag_commit"]
            or served.get("repo_url", "").removesuffix(".git").rstrip("/") != fixture["provenance"]["repository"]
            or digest("\n".join(excerpt["content"].splitlines()).encode()) != fixture["expected_context_sha256"][excerpt["path"]]):
        raise ValueError("upstream_snapshot_mismatch")


def checked_contexts(folder):
    verify.verify_manifest(folder)
    env = verify.load(folder / "environment.json")
    frozen = registration(verify.load(folder / "preregistration.json"))
    if (env.get("phase") != "preflight" or verify.load(folder / "results.json") != []
            or env.get("preregistration_sha256") != digest(json.dumps(frozen, sort_keys=True).encode())):
        raise ValueError("invalid_preflight_source")
    contexts = {}
    for name, fixture in UPSTREAM_FIXTURES.items():
        receipt = verify.load(folder / (name + "-context.json"))
        requests = receipt.get("requests", [])
        if not requests or len(requests) > len(fixture["reads"]):
            raise ValueError("invalid_context_request_count")
        target = f"pypi:{normalize_name(fixture['package'])}@{fixture['version']}"
        excerpts = []
        for request, read in zip(requests, fixture["reads"]):
            url = API + "/v1/read?" + urlencode({"target": target, **read, "wait_timeout_ms": 20000})
            if (request.get("url") != url or request.get("transport") != "private-accounted-cli"
                    or request.get("authenticated") is not True):
                raise ValueError("context_not_authenticated_accounted_target")
            if receipt.get("status") == "ready":
                if request.get("status") != 200 or request.get("error"):
                    raise ValueError("context_not_successful")
                body = request["body"]
                text = validate_read(body, fixture, read)
                excerpts.append({"target": target, "path": read["path"], "content": text,
                    "target_resolution": body["target_resolution"],
                    "start_line": body["content"]["start_line"], "end_line": body["content"]["end_line"]})
                check_upstream_excerpt(fixture, excerpts[-1])
        if receipt.get("status") == "ready":
            if len(requests) != len(fixture["reads"]) or receipt.get("context") != excerpts:
                raise ValueError("context_excerpt_mismatch")
        elif receipt.get("status") != "unavailable" or receipt.get("context") is not None or not receipt.get("reason"):
            raise ValueError("invalid_unavailable_context")
        contexts[name] = receipt
    return contexts


def matrix(output, python, access):
    contexts = checked_contexts(access)
    begin(output, python, "matrix")
    # Keep the authenticated snapshot self-contained and covered by this manifest.
    import shutil
    shutil.copytree(access, output / "access")
    environment = verify.load(output / "environment.json")
    environment["context_manifest_sha256"] = digest((access / "sha256.json").read_bytes())
    run.write_json(output / "environment.json", environment)
    rows, validations = [], []
    try:
        with tempfile.TemporaryDirectory(prefix="upstream-matrix-") as folder:
            work = Path(folder)
            for name in UPSTREAM_FIXTURES:
                run.create_base(name, work / (name + "-base"))
                validations.append(run.validate_fixture(name, work / (name + "-base"), output / (name + "-validation"), python))
                run.write_json(output / (name + "-context.json"), contexts[name])
            run.write_json(output / "validation.json", validations)
            rng = random.Random(CONFIGURATION["order_seed"])
            for name in UPSTREAM_FIXTURES:
                arms = list(run.ARMS)
                rng.shuffle(arms)
                for arm in arms:
                    label = f"cell-{len(rows) + 1:02}"
                    root = work / label
                    git(work, "clone", "-q", "--no-hardlinks", str(work / (name + "-base")), str(root))
                    rows.append(run.run_arm(name, arm, 1, root, output / label, python, 2,
                                           CONFIGURATION["model"], CONFIGURATION["effort"], contexts[name]))
                    run.write_json(output / "results.json", rows)
                    print(f"Completed cell {len(rows)}/8; outcomes withheld until matrix completion.", flush=True)
    except BaseException:
        run.write_json(output / "interrupted.json", {"completed_rows": len(rows), "status": "incomplete"})
        raise
    finally:
        run.manifest(output)
    print(run.summarize(rows, 2).replace("Authored integration regressions against pinned packages; exploratory, not representative.",
          "Four upstream-derived bugs in one pinned dependency; convenience sample, not representative."))


def replay(folder, python, output):
    env = verify.load(folder / "environment.json")
    if env.get("suite") != "upstream-v3" or env.get("phase") != "matrix":
        raise ValueError("not_an_upstream_v3_matrix")
    frozen = registration(verify.load(folder / "preregistration.json"))
    if env.get("preregistration_sha256") != digest(json.dumps(frozen, sort_keys=True).encode()):
        raise ValueError("registration_receipt_changed")
    if env.get("context_manifest_sha256") != digest((folder / "access/sha256.json").read_bytes()):
        raise ValueError("preflight_manifest_changed")
    contexts = checked_contexts(folder / "access")
    for name, context in contexts.items():
        if verify.load(folder / (name + "-context.json")) != context:
            raise ValueError("matrix_context_changed")
    if output.exists() or output.is_relative_to(folder):
        raise ValueError("new_replay_path_outside_evidence_required")
    result = verify.verify(folder, python)
    access_result = verify.verify(folder / "access", python)
    result["observations"] += [{**row, "arm": "preflight_validation"} for row in access_result["observations"]]
    result["preflight_evidence_files"] = access_result["evidence_files"]
    run.write_json(output, result)
    print(f"Verified {result['evidence_files']} hashes; {len(result['observations'])} fixture/arm records.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "preflight", "run", "verify"))
    parser.add_argument("--write", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--access", type=Path)
    parser.add_argument("--folder", type=Path)
    args = parser.parse_args()
    install_fixtures()
    if args.mode == "freeze":
        freeze(args.write.resolve())
    elif args.mode == "preflight":
        preflight(args.write.resolve(), args.python)
    elif args.mode == "run":
        if args.access is None:
            parser.error("--access requires the completed authenticated preflight")
        matrix(args.write.resolve(), args.python, args.access.resolve())
    else:
        if args.folder is None:
            parser.error("--folder requires the saved matrix")
        check_interpreter(args.python)
        replay(args.folder.resolve(), args.python, args.write.resolve())


if __name__ == "__main__":
    main()
