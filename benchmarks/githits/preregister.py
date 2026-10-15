"""Freeze and check the harder suite before any model outcomes exist."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.githits.client import API, digest, normalize_name, validate_read
from urllib.parse import urlencode
from benchmarks.githits.hard_fixtures import HARD_FIXTURES
from examples.evolve import git

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = "benchmarks/githits/PREREGISTRATION-v2.1.json"
CONTEXT_SOURCE = "benchmarks/githits/results/hard-v2-access-001"
CONFIGURATION = {"model": "gpt-6.1-sol", "effort": "high", "calls_per_arm": 2,
                 "repeats": 1, "order_seed": 20261015, "planned_pairs": 6,
                 "max_model_calls": 24}


def code_files():
    names = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py").splitlines()
    return sorted(set(names + ["benchmarks/githits/requirements.txt", "benchmarks/githits/PREREGISTRATION-v2.md",
                               "benchmarks/githits/PREREGISTRATION-v2.1.md"]))


def source_digest(path):
    return digest((ROOT / path).read_bytes().replace(b"\r\n", b"\n"))


def fixture_digests():
    return {name: digest(json.dumps(fixture, sort_keys=True, ensure_ascii=False).encode())
            for name, fixture in HARD_FIXTURES.items()}


def build_registration():
    initial = json.loads((ROOT / "benchmarks/githits/PREREGISTRATION-v2.json").read_text(encoding="utf-8"))
    if fixture_digests() != initial["fixture_sha256"] or CONFIGURATION != initial["configuration"]:
        raise ValueError("coverage_amendment_cannot_change_tasks_or_budget")
    return {"suite": "hard-v2", "registered_at": datetime.now(timezone.utc).isoformat(),
            "revision": "v2.1-coverage-before-models", "initial_registration_commit": "291a7a1c2d4f257967d93add19ab9a6809e9481f",
            "context_source": {"path": CONTEXT_SOURCE, "manifest_sha256": digest((ROOT / CONTEXT_SOURCE / "sha256.json").read_bytes())},
            "allowed_unavailable": {"T04": "VERSION_NOT_FOUND"},
            "parent_commit": git(ROOT, "rev-parse", "HEAD"), "configuration": CONFIGURATION,
            "tasks": list(HARD_FIXTURES), "fixture_sha256": fixture_digests(),
            "source_sha256_lf": {name: source_digest(name) for name in code_files()},
            "blinding": "hidden checks/references; unbranded context; automatic arm-unaware checks; operator unblinded",
            "prior_result": {"run": "2026-10-15-run-004", "pairs": 3, "without": 3, "with": 3}}


def check_registration(registration):
    if (registration.get("suite") != "hard-v2" or registration.get("configuration") != CONFIGURATION
            or registration.get("allowed_unavailable") != {"T04": "VERSION_NOT_FOUND"}
            or registration.get("context_source") != {"path": CONTEXT_SOURCE,
                "manifest_sha256": digest((ROOT / CONTEXT_SOURCE / "sha256.json").read_bytes())}
            or registration.get("tasks") != list(HARD_FIXTURES)
            or registration.get("fixture_sha256") != fixture_digests()
            or set(registration.get("source_sha256_lf", {})) != set(code_files())):
        raise ValueError("preregistration_definition_changed")
    for name, expected in registration["source_sha256_lf"].items():
        if source_digest(name) != expected:
            raise ValueError("preregistered_source_changed:" + name)
    return registration


def validate_context_receipt(name, fixture, receipt):
    target = f"pypi:{normalize_name(fixture['package'])}@{fixture['version']}"
    requests = receipt.get("requests", [])
    ready = receipt.get("status") == "ready"
    if len(requests) != (len(fixture["reads"]) if ready else 1):
        raise ValueError("cached_request_count_changed")
    for request, read in zip(requests, fixture["reads"]):
        url = API + "/v1/read?" + urlencode({"target": target, **read, "wait_timeout_ms": 20000})
        if (request.get("url") != url or request.get("authenticated") is not True
                or request.get("transport") != "private-accounted-cli"):
            raise ValueError("cached_request_identity_changed")
    if not ready:
        if (name != "T04" or receipt.get("status") != "unavailable" or receipt.get("context") is not None
                or receipt.get("reason") != "VERSION_NOT_FOUND" or requests[0].get("status") != 404
                or requests[0].get("body", {}).get("code") != "VERSION_NOT_FOUND"):
            raise ValueError("unregistered_unavailable_context")
        return receipt
    excerpts = []
    for request, read in zip(requests, fixture["reads"]):
        if request.get("status") != 200 or request.get("error"):
            raise ValueError("cached_context_not_successful")
        body = request["body"]
        text = validate_read(body, fixture, read)
        excerpts.append({"target": target, "path": read["path"], "content": text,
                         "target_resolution": body["target_resolution"],
                         "start_line": body["content"]["start_line"], "end_line": body["content"]["end_line"]})
    if receipt.get("context") != excerpts:
        raise ValueError("cached_excerpt_changed")
    return receipt


def registered_contexts(registration):
    from benchmarks.githits.verify import verify_manifest
    folder = ROOT / registration["context_source"]["path"]
    verify_manifest(folder)
    if json.loads((folder / "interrupted.json").read_text())["completed_rows"] != 0:
        raise ValueError("context_source_contains_prior_model_cells")
    if json.loads((folder / "environment.json").read_text())["source_commit"] != registration["initial_registration_commit"]:
        raise ValueError("context_preflight_source_changed")
    return {name: validate_context_receipt(name, fixture, json.loads((folder / (name + "-context.json")).read_text(encoding="utf-8")))
            for name, fixture in HARD_FIXTURES.items()}


def verify_registration():
    path = ROOT / REGISTRATION
    registration = check_registration(json.loads(path.read_text(encoding="utf-8")))
    committed = json.loads(git(ROOT, "show", "HEAD:" + REGISTRATION))
    if registration != committed:
        raise ValueError("preregistration_must_be_committed_before_models")
    return registration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args()
    if args.write.exists():
        parser.error("refuse to overwrite a preregistration")
    registration = build_registration()
    args.write.write_text(json.dumps(registration, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Frozen six tasks, source hashes and configuration; commit and push before running models.")


if __name__ == "__main__":
    main()
