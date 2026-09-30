"""Shared Builder test fixtures."""

import tempfile
import unittest
from pathlib import Path

from syberwork.coordination import BuilderStore
from syberwork.core import Work
from tests.test_cell import _drop_postgres, _postgres_database

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


def world_body(**overrides) -> dict:
    document = {
        "provider": {"name": "static", "revision": "fixture-1"},
        "services": [{"name": "github", "mode": "simulated"}],
        "seed_ref": "seed.json",
        "seed_digest": "ab" * 32,
        "environment_snapshot": {"snapshot_ref": "snap-1", "snapshot_digest": "cd" * 32},
        "network_policy": {"allowed": ["github.example"], "denied": ["evil.example"], "record_denied": True},
        "time_policy": {"mode": "fixed", "epoch": 1700000000000000, "timezone": "UTC"},
        "entropy_policy": {"mode": "none", "seed_digest": None},
        "reproducibility": "unknown",
        "parent_world_digest": None,
        "intent": "comparison",
    }
    document.update(overrides)
    return document


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




class CellCase(unittest.TestCase):
    dialect = "sqlite"

    def setUp(self):
        if self.dialect == "postgres":
            url, admin, name = _postgres_database()
            self.addCleanup(lambda: _drop_postgres(admin, name))
            self.work = Work(url)
        else:
            folder = tempfile.TemporaryDirectory()
            self.addCleanup(folder.cleanup)
            self.work = Work(Path(folder.name) / "cell.sqlite")
        self.addCleanup(self.work.close)
        self.store = BuilderStore(self.work)
        self.work.install_contract(NOTE)
        self.work.install_policy({"version": 1, "actions": {"promote": {"roles": ["operator"]}}})
        self.work.install_action("promote", {"kind": "local"})
        self.case = self.work.create_case("repo-change", 1, {"objective": "export"}, "operator")
        self.authority = self._authority()
        self.work.propose = self._forbid("propose")
        self.work.commit = self._forbid("commit")

    def _record(self, n: int, paths) -> str:
        body = git_candidate(n, paths)
        self.work.record_candidate(self.case, body, "operator", ["operator"])
        return body["id"]

    def _forbid(self, name):
        def wrapped(*args, **kwargs):
            raise AssertionError(name)
        return wrapped

    def _authority(self):
        return [(event["kind"], event["hash"]) for event in self.work.inspect(self.case)["events"]]

    def _assert_authority_unchanged(self):
        self.assertEqual(self._authority(), self.authority)
        self.assertTrue(self.work.verify_chain(self.case))

    def _generation(self, **overrides):
        self.store.install_policy(POLICY, "operator")
        self.store.open_work(self.case, "Ship a reviewable export", "operator")
        document = {
            "case_id": self.case,
            "objective": "Compare structures before implementation",
            "base_revision": "abc123",
            "mode": "explore",
            "isolation": "aware",
            "diversity_threshold": 0.3,
            "min_approaches": 1,
            "selection_policy_id": "review",
            "selection_policy_version": 1,
        }
        document.update(overrides)
        return self.store.create_generation(document, "operator")

    def _events(self):
        events, _truncated = self.store.replay(self.case, 0, 200)
        return events

