"""Shared records for candidate scenarios. Not a scenario module."""

from __future__ import annotations

BASE = "0" * 40


def candidate(n: int = 1) -> dict:
    return {
        "id": f"c{n}",
        "commit": f"{n:040x}",
        "tree": f"{1000 + n:040x}",
        "base": BASE,
        "parents": [],
        "operator": "patch",
        "provider": {"name": "fixture", "revision": "1"},
        "changed_paths": ["src/app.py"],
        "diff": {"digest": "d" * 64, "bytes": 80, "files": 1},
    }


def evaluation(n: int = 1) -> dict:
    body = candidate(n)
    return {
        "candidate": body["id"],
        "commit": body["commit"],
        "tree": body["tree"],
        "evaluator": "host.checks/1",
        "checks": [{
            "name": "tests",
            "state": "passed",
            "exit_code": 0,
            "duration_ms": 4,
            "output_digest": "e" * 64,
            "output_tail": "ok",
        }],
    }


def evolution_contract() -> dict:
    return {
        "id": "repo-change",
        "version": 1,
        "inputs": {"objective": "string"},
        "actions": {"promote": {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "promote"}],
        "evolution": {
            "scope": {"paths": ["src/"], "max_files": 3, "max_diff_bytes": 5000},
            "operators": ["patch"],
            "budget": {"max_candidates": 4, "max_evaluations": 4, "max_seconds": 60},
            "evaluation": {
                "checks": {"tests": {"argv": ["python", "-m", "unittest"], "timeout_seconds": 60}},
                "required": ["tests"],
                "max_age_seconds": 600,
            },
            "promotion": {"action": "promote", "roles": ["developer"]},
        },
    }
