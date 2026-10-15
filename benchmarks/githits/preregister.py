"""Freeze and check the harder suite before any model outcomes exist."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.githits.client import digest
from benchmarks.githits.hard_fixtures import HARD_FIXTURES
from examples.evolve import git

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = "benchmarks/githits/PREREGISTRATION-v2.json"
CONFIGURATION = {"model": "gpt-6.1-sol", "effort": "high", "calls_per_arm": 2,
                 "repeats": 1, "order_seed": 20261015, "planned_pairs": 6,
                 "max_model_calls": 24}


def code_files():
    names = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py").splitlines()
    return sorted(set(names + ["benchmarks/githits/requirements.txt", "benchmarks/githits/PREREGISTRATION-v2.md"]))


def source_digest(path):
    return digest((ROOT / path).read_bytes().replace(b"\r\n", b"\n"))


def fixture_digests():
    return {name: digest(json.dumps(fixture, sort_keys=True, ensure_ascii=False).encode())
            for name, fixture in HARD_FIXTURES.items()}


def build_registration():
    return {"suite": "hard-v2", "registered_at": datetime.now(timezone.utc).isoformat(),
            "parent_commit": git(ROOT, "rev-parse", "HEAD"), "configuration": CONFIGURATION,
            "tasks": list(HARD_FIXTURES), "fixture_sha256": fixture_digests(),
            "source_sha256_lf": {name: source_digest(name) for name in code_files()},
            "blinding": "hidden checks/references; unbranded context; automatic arm-unaware checks; operator unblinded",
            "prior_result": {"run": "2026-10-15-run-004", "pairs": 3, "without": 3, "with": 3}}


def check_registration(registration):
    if (registration.get("suite") != "hard-v2" or registration.get("configuration") != CONFIGURATION
            or registration.get("tasks") != list(HARD_FIXTURES)
            or registration.get("fixture_sha256") != fixture_digests()
            or set(registration.get("source_sha256_lf", {})) != set(code_files())):
        raise ValueError("preregistration_definition_changed")
    for name, expected in registration["source_sha256_lf"].items():
        if source_digest(name) != expected:
            raise ValueError("preregistered_source_changed:" + name)
    return registration


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
