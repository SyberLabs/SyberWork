"""Shared Builder test fixtures."""

import json
from pathlib import Path

from spec.validate import load_schema, validate

NOTE = {
    "id": "repo-change",
    "version": 1,
    "inputs": {"objective": "string"},
    "actions": {"promote": {}},
    "acceptance": [{"id": "accepted", "kind": "effect", "action": "promote"}],
    "evolution": {
        "scope": {"paths": ["syberwork/", "syberlabs/"], "max_files": 8, "max_diff_bytes": 20000},
        "operators": ["patch"],
        "budget": {"max_candidates": 8, "max_evaluations": 8, "max_seconds": 60},
        "evaluation": {"checks": {"tests": {"argv": ["python", "-m", "unittest"], "timeout_seconds": 60}}, "required": ["tests"]},
        "promotion": {"action": "promote", "roles": ["operator"]},
    },
}
GRANTS = [
    {"role": "operator", "kind": "comment", "authority": "informative"},
    {"role": "engineer", "kind": "concern", "authority": "veto"},
    {"role": "engineer", "kind": "critique", "authority": "advisory"},
    {"role": "engineer", "kind": "preference", "authority": "advisory"},
    {"role": "intended_user", "kind": "preference", "authority": "advisory"},
    {"role": "intended_user", "kind": "comment", "authority": "advisory"},
    {"role": "stakeholder", "kind": "preference", "authority": "advisory"},
    {"role": "consensus", "kind": "preference", "authority": "consensus"},
    {"role": "veto", "kind": "dissent", "authority": "veto"},
    {"role": "veto", "kind": "concern", "authority": "veto"},
    {"role": "veto", "kind": "critique", "authority": "veto"},
    {"role": "manager", "kind": "preference", "authority": "decision"},
    {"role": "manager", "kind": "dissent", "authority": "decision"},
    {"role": "manager", "kind": "concern", "authority": "decision"},
    {"role": "product_lead", "kind": "preference", "authority": "decision"},
]
POLICY = {
    "id": "review",
    "version": 1,
    "required_integrity": ["human_reviewed"],
    "grants": GRANTS,
    "consensus_authorities": ["consensus"],
    "consensus_threshold": 2,
    "promotion_roles": ["manager"],
}
SPEC = Path(__file__).resolve().parents[1] / "spec" / "builder"


def git_candidate(n: int, paths) -> dict:
    return {
        "id": f"c{n}",
        "commit": f"{n:040x}",
        "tree": f"{1000 + n:040x}",
        "base": "0" * 40,
        "parents": [],
        "operator": "patch",
        "provider": {"name": "fixture", "revision": "1"},
        "changed_paths": list(paths),
        "diff": {"digest": "d" * 64, "bytes": 80, "files": len(paths)},
    }


def descriptor(trait: str) -> dict:
    return {
        "intent": trait,
        "interaction_model": trait,
        "architecture": trait,
        "state_model": trait,
        "data_model": trait,
        "primary_abstraction": trait,
        "dependencies": [trait],
        "expected_strengths": [trait],
        "expected_weaknesses": [trait],
        "distinguishing_claims": [trait],
        "structural_traits": [trait],
    }


def architecture(case_id: str, generation_id: str, revision: str = "abc123") -> dict:
    return {
        "case_id": case_id,
        "generation_id": generation_id,
        "repository": "SyberLabs/SyberWork",
        "revision": revision,
        "provider": "static",
        "groups": [{"id": "runtime", "label": "Runtime"}],
        "nodes": [
            {"id": "api", "label": "API", "paths": ["syberwork/server.py"], "group": "runtime"},
            {"id": "core", "label": "Core", "paths": ["syberwork/core.py"], "group": "runtime"},
            {"id": "storage", "label": "Storage", "paths": ["syberwork/storage.py"]},
            {"id": "builder", "label": "Builder", "paths": ["syberlabs/builder.py"]},
        ],
        "edges": [{"from": "api", "to": "core"}, {"from": "core", "to": "storage"}],
    }


class AcceptingRuntime:
    def apply(self, command: dict) -> dict:
        return {"applied": True, "reason": "accepted"}


def _check(document, name: str) -> None:
    path = SPEC / name
    schema = load_schema(path)
    validate(document, schema, base=path)


