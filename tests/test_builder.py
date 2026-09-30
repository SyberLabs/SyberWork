"""Builder coordination substrate. It must not change the case hash chain."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from spec.validate import load_schema, validate
from syberlabs.builder import (
    PROTOCOL,
    architecture_diff,
    context_manifest,
    derive_authority,
    diversity_evidence,
    evaluate_selection,
    nodes_for_paths,
    structural_distance,
    validate_snapshot,
)
from syberlabs.errors import Rejected
from syberlabs.protocol import SIDE_PROTOCOL, EventKind
from syberwork.backup import export_cell, restore_cell
from syberwork.coordination import BuilderStore
from syberwork.core import Work
from syberwork.server import make_server
from tests.test_cell import FIXTURE, _drop_postgres, _postgres_database
from typing import get_args


NOTE = {
    "id": "note",
    "version": 1,
    "inputs": {"name": "string"},
    "actions": {"note": {}},
    "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
}
POLICY = {
    "id": "review",
    "version": 1,
    "required_integrity": ["human_reviewed"],
    "stakeholder_authority": {
        "operator": "informative",
        "engineer": "advisory",
        "intended_user": "advisory",
        "stakeholder": "advisory",
        "consensus": "consensus",
        "veto": "veto",
        "manager": "decision",
        "decision": "decision",
    },
    "advisory_dimensions": ["comment", "critique", "preference", "concern", "dissent", "trait_request"],
    "consensus_threshold": 2,
    "promotion_roles": ["manager"],
    "weights": {"preference": 1, "concern": -1},
}
SPEC = Path(__file__).resolve().parents[1] / "spec" / "builder"


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


class FarSemantic:
    def distance(self, left, right) -> float:
        return 1.0


class AcceptingRuntime:
    def apply(self, command: dict) -> dict:
        return {"applied": True, "reason": "accepted"}


def _check(document, name: str) -> None:
    path = SPEC / name
    schema = load_schema(path)
    validate(document, schema, base=path)


class Domain(unittest.TestCase):
    def test_protocol_is_separate_from_the_frozen_case_vocabulary(self):
        self.assertEqual(PROTOCOL, "sdk.syberlabs.space/builder/v0alpha1")
        self.assertNotIn("builder", SIDE_PROTOCOL)
        self.assertNotIn("generation_created", get_args(EventKind))
        source = Path(__import__("syberlabs.builder", fromlist=["builder"]).__file__).read_text()
        self.assertNotIn("import syberwork", source)
        self.assertNotIn("from syberwork", source)

    def test_structural_distance_and_semantic_signal_are_not_the_same_gate(self):
        left, right = descriptor("spa_local"), descriptor("mpa_server")
        self.assertEqual(structural_distance(left, left), 0.0)
        self.assertEqual(structural_distance(left, right), 1.0)
        same_claims = descriptor("quantum_lattice")
        same_claims["distinguishing_claims"] = ["spa_local"]
        report = diversity_evidence(same_claims, [{"id": "sib", "descriptor": left}], 0.3, FarSemantic())
        self.assertFalse(report["passed"])
        self.assertEqual(report["sibling_id"], "sib")
        pair = report["evidence"]["pairs"][0]
        self.assertTrue(pair["claims_equal"])
        self.assertEqual(pair["semantic_distance"], 1.0)
        self.assertGreaterEqual(pair["structural_distance"], 0.3)
        self.assertEqual(report["evidence"]["authoritative"], "structural")
        copied = diversity_evidence(left, [{"id": "sib", "descriptor": left}], 0.3, FarSemantic())
        self.assertFalse(copied["passed"])
        self.assertEqual(copied["evidence"]["pairs"][0]["structural_distance"], 0.0)

    def test_isolation_manifests_are_data_not_prompt_text(self):
        generation = {"id": "g", "objective": "export", "base_revision": "abc", "mode": "explore", "isolation": "independent"}
        own = {"id": "a", "descriptor": descriptor("spa_local"), "state": "frozen"}
        sibling = {"id": "b", "descriptor": descriptor("mpa_server"), "state": "frozen"}
        implementations = [{"candidate_id": "c2", "approach_id": "b", "changed_paths": ["syberwork/core.py"], "hypothesis_summary": "secret"}]
        independent = context_manifest(generation, own, [own, sibling], implementations)
        self.assertEqual(independent["siblings"], [])
        self.assertEqual(independent["implementations"], [])
        generation["isolation"] = "aware"
        aware = context_manifest(generation, own, [own, sibling], implementations)
        self.assertEqual(aware["siblings"], [{"id": "b", "descriptor": sibling["descriptor"]}])
        self.assertEqual(aware["implementations"], [])
        generation["isolation"] = "collaborative"
        shared = context_manifest(generation, own, [own, sibling], implementations)
        self.assertEqual(shared["implementations"], [{
            "candidate_id": "c2",
            "approach_id": "b",
            "changed_paths": ["syberwork/core.py"],
        }])
        self.assertNotIn("hypothesis_summary", shared["implementations"][0])

    def test_feedback_authority_comes_from_the_policy(self):
        policy = {
            "stakeholder_authority": POLICY["stakeholder_authority"],
        }
        self.assertEqual(derive_authority(["intended_user"], policy), "advisory")
        self.assertEqual(derive_authority(["intended_user", "manager"], policy), "decision")
        self.assertEqual(derive_authority(["veto"], policy), "veto")
        with self.assertRaises(Rejected) as caught:
            derive_authority(["observer"], policy)
        self.assertEqual(caught.exception.code, "feedback_authority")

    def test_selection_keeps_dimensions_and_does_not_promote(self):
        candidates = [{"id": "c1"}, {"id": "c2"}]
        feedback = [
            {"id": "f1", "target_kind": "candidate", "target_id": "c1", "kind": "dissent", "authority": "veto",
             "actor": "ada", "text": "do not ship", "at": 1},
            {"id": "f2", "target_kind": "candidate", "target_id": "c2", "kind": "preference", "authority": "advisory",
             "actor": "bao", "text": "this one", "at": 2},
        ]
        integrity = [
            {"id": "i1", "target_kind": "candidate", "target_id": "c2", "independence": "internal", "result": "pass",
             "claim": "self", "verifier": "model", "digest": None, "source": "model"},
        ]
        decision = evaluate_selection(POLICY, candidates, feedback, integrity, ["manager"])
        self.assertFalse(decision["promotes_git"])
        self.assertTrue(decision["promotion_authorized"])
        self.assertEqual(decision["rejected"][0]["id"], "c1")
        self.assertEqual(decision["rejected"][0]["reasons"], ["veto:ada"])
        self.assertEqual(decision["unresolved"][0]["reasons"], ["integrity_required:human_reviewed"])
        self.assertEqual(decision["dimensions"]["c2"]["feedback"]["by_kind"]["preference"][0]["text"], "this one")
        self.assertEqual(decision["dimensions"]["c2"]["aggregate"], 1)
        self.assertNotIn("c1", {item["id"] for item in decision["advanced"]})

    def test_architecture_paths_map_and_diff_without_mermaid(self):
        with self.assertRaises(Rejected):
            validate_snapshot({"repository": "r", "revision": "1", "provider": "static", "nodes": [], "edges": [], "mermaid": "graph TD"})
        snapshot = validate_snapshot(architecture("case", "gen"))
        self.assertEqual([node["id"] for node in nodes_for_paths(snapshot, ["syberwork/server.py"])], ["api"])
        self.assertEqual([node["id"] for node in nodes_for_paths(snapshot, ["syberwork/worker.py"])], [])
        other = validate_snapshot({
            "id": "later",
            "repository": "SyberLabs/SyberWork",
            "revision": "def",
            "provider": "static",
            "nodes": [
                {"id": "api", "label": "API", "paths": ["syberwork/server.py", "syberwork/builder_http.py"]},
                {"id": "extra", "label": "Extra", "paths": ["syberwork/coordination.py"]},
            ],
            "edges": [{"from": "extra", "to": "api"}],
        })
        diff = architecture_diff(snapshot, other)
        self.assertEqual(diff["authoritative"], False)
        self.assertEqual([node["id"] for node in diff["added_nodes"]], ["extra"])
        self.assertIn("core", {node["id"] for node in diff["removed_nodes"]})
        self.assertEqual(diff["added_edges"], [{"from": "extra", "to": "api"}])


class StoreCases(unittest.TestCase):
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
        self.work.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
        self.work.install_action("note", {"kind": "local"})
        self.case = self.work.create_case("note", 1, {"name": "a"}, "operator")
        self.authority = self._authority()
        self.work.record_candidate = self._forbid("record_candidate")
        self.work.propose = self._forbid("propose")
        self.work.commit = self._forbid("commit")

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

    def test_generation_barrier_blocks_candidates_until_seal(self):
        generation = self._generation(min_approaches=2)
        first = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        with self.assertRaises(Rejected) as blocked:
            self.store.link_candidate(generation["id"], first["id"], "cand-1", ["syberwork/server.py"], "operator")
        self.assertEqual(blocked.exception.code, "generation_unsealed")
        with self.assertRaises(Rejected) as early:
            self.store.launch(generation["id"], "operator")
        self.assertEqual(early.exception.code, "generation_unsealed")
        with self.assertRaises(Rejected) as short:
            self.store.seal(generation["id"], "operator")
        self.assertEqual(short.exception.code, "diversity_short")
        self.store.register_approach(generation["id"], descriptor("mpa_server"), "operator")
        sealed = self.store.seal(generation["id"], "operator")
        self.assertEqual(sealed["state"], "sealed")
        linked = self.store.link_candidate(generation["id"], first["id"], "cand-1", ["syberwork/server.py"], "operator")
        self.assertEqual(linked["candidate_id"], "cand-1")
        self.assertEqual(self.store.launch(generation["id"], "operator")["state"], "launched")
        self._assert_authority_unchanged()

    def test_similar_approach_is_rejected_and_recorded(self):
        generation = self._generation()
        first = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        with self.assertRaises(Rejected) as caught:
            self.store.register_approach(generation["id"], descriptor("spa_local"), "operator", FarSemantic())
        self.assertEqual(caught.exception.code, f"approach_too_similar:{first['id']}")
        approaches = self.store.generation_view(generation["id"])["approaches"]
        rejected = next(item for item in approaches if item["state"] == "rejected")
        self.assertEqual(rejected["evidence"]["pairs"][0]["semantic_distance"], 1.0)
        self.assertEqual(rejected["evidence"]["authoritative"], "structural")
        self.assertEqual(rejected["evidence"]["pairs"][0]["structural_distance"], 0.0)
        kinds = [event["kind"] for event in self._events()]
        self.assertIn("approach_rejected", kinds)
        self._assert_authority_unchanged()

    def test_sealed_descriptors_are_immutable(self):
        generation = self._generation()
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        revised = self.store.revise_approach(approach["id"], descriptor("spa_revised"), "operator")
        self.assertEqual(revised["descriptor"]["intent"], "spa_revised")
        self.store.seal(generation["id"], "operator")
        with self.assertRaises(Rejected) as caught:
            self.store.revise_approach(approach["id"], descriptor("spa_again"), "operator")
        self.assertEqual(caught.exception.code, "approach_frozen")
        with self.assertRaises(Rejected):
            self.store.register_approach(generation["id"], descriptor("mpa_server"), "operator")
        stored = self.store.generation_view(generation["id"])["approaches"][0]
        self.assertEqual(stored["state"], "frozen")
        self.assertEqual(stored["descriptor"]["intent"], "spa_revised")

    def test_store_manifest_matches_the_isolation_policy(self):
        generation = self._generation(isolation="aware", min_approaches=2)
        first = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.store.register_approach(generation["id"], descriptor("mpa_server"), "operator")
        self.store.seal(generation["id"], "operator")
        agent = self.store.register_agent({
            "generation_id": generation["id"],
            "approach_id": first["id"],
            "principal": "agent-a",
            "role": "implementer",
            "hypothesis_summary": "local cache",
            "working_set": ["syberwork/server.py"],
        }, "operator")
        manifest = self.store.manifest(agent["id"])
        self.assertEqual(manifest["isolation"], "aware")
        self.assertEqual(len(manifest["siblings"]), 1)
        self.assertEqual(manifest["implementations"], [])
        self.assertNotIn("hypothesis_summary", manifest["approach"])
        self.store.link_candidate(generation["id"], first["id"], "cand-1", ["syberwork/server.py"], "operator")
        self.assertEqual(self.store.manifest(agent["id"])["implementations"], [])

        collaborative = self._generation(isolation="collaborative", min_approaches=2, objective="Share implementation state")
        own = self.store.register_approach(collaborative["id"], descriptor("cli_files"), "operator")
        sibling = self.store.register_approach(collaborative["id"], descriptor("native_sqlite"), "operator")
        self.store.seal(collaborative["id"], "operator")
        self.store.link_candidate(collaborative["id"], sibling["id"], "cand-cli", ["syberwork/core.py"], "operator")
        other = self.store.register_agent({
            "generation_id": collaborative["id"],
            "approach_id": own["id"],
            "principal": "agent-b",
            "role": "implementer",
        }, "operator")
        shared = self.store.manifest(other["id"])
        self.assertEqual(shared["implementations"], [{
            "candidate_id": "cand-cli",
            "approach_id": sibling["id"],
            "changed_paths": ["syberwork/core.py"],
        }])
        self.assertEqual([item["id"] for item in shared["siblings"]], [sibling["id"]])
        partner = self._generation(isolation="independent", min_approaches=2, objective="Hide siblings")
        left = self.store.register_approach(partner["id"], descriptor("native_sqlite"), "operator")
        self.store.register_approach(partner["id"], descriptor("mpa_server"), "operator")
        self.store.seal(partner["id"], "operator")
        hidden = self.store.register_agent({
            "generation_id": partner["id"],
            "approach_id": left["id"],
            "principal": "agent-c",
            "role": "implementer",
        }, "operator")
        self.assertEqual(self.store.manifest(hidden["id"])["siblings"], [])

    def test_feedback_authority_is_not_caller_supplied(self):
        generation = self._generation()
        self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.store.seal(generation["id"], "operator")
        recorded = self.store.record_feedback({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "kind": "preference",
            "text": "the simpler interaction",
            "authority": "decision",
        }, "bao", ["intended_user"])
        self.assertEqual(recorded["authority"], "advisory")
        _check(recorded, "feedback.schema.json")
        with self.assertRaises(Rejected) as caught:
            self.store.record_feedback({
                "case_id": self.case,
                "generation_id": generation["id"],
                "target_kind": "generation",
                "target_id": generation["id"],
                "kind": "comment",
                "text": "hello",
                "authority": "decision",
            }, "guest", ["observer"])
        self.assertEqual(caught.exception.code, "feedback_authority")

    def test_selection_does_not_promote_or_touch_the_case_chain(self):
        generation = self._generation()
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.store.seal(generation["id"], "operator")
        self.store.launch(generation["id"], "operator")
        self.store.link_candidate(generation["id"], approach["id"], "cand-1", ["syberwork/server.py"], "operator")
        self.store.record_feedback({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "candidate",
            "target_id": "cand-1",
            "kind": "dissent",
            "text": "stop",
        }, "ada", ["veto"])
        selection = self.store.select(generation["id"], "manager", ["manager"])
        self.assertFalse(selection["promotes_git"])
        self.assertEqual(selection["rejected"][0]["id"], "cand-1")
        self.assertTrue(selection["promotion_authorized"])
        reference = self.store.promotion_reference(selection)
        self.assertFalse(reference["promotes_git"])
        self.assertEqual(reference["evidence_digest"], selection["evidence_digest"])
        _check(selection, "selection.schema.json")
        self._assert_authority_unchanged()

    def test_integrity_classes_stay_distinguishable(self):
        generation = self._generation()
        digest = "a" * 64
        internal = self.store.record_integrity({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "claim": "model says it passes",
            "source": "provider",
            "verifier": "operator",
            "independence": "internal",
            "result": "pass",
        }, "operator")
        human = self.store.record_integrity({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "claim": "an engineer read the diff",
            "source": "review",
            "verifier": "operator",
            "independence": "human_reviewed",
            "result": "concern",
        }, "operator")
        signed = self.store.record_integrity({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "claim": "external signature",
            "source": "ci",
            "verifier": "operator",
            "independence": "signed_external",
            "result": "pass",
            "digest": digest,
        }, "operator")
        with self.assertRaises(Rejected) as missing:
            self.store.record_integrity({
                "case_id": self.case,
                "generation_id": generation["id"],
                "target_kind": "generation",
                "target_id": generation["id"],
                "claim": "unsigned",
                "source": "ci",
                "verifier": "operator",
                "independence": "signed_external",
                "result": "pass",
            }, "operator")
        self.assertEqual(missing.exception.code, "integrity_digest_required")
        view = self.store.integrity_view(generation["id"])
        self.assertEqual(view["kind"], "IntegrityProjection")
        self.assertFalse(view["authoritative"])
        self.assertEqual([item["id"] for item in view["self_report"]], [internal["id"]])
        self.assertEqual({item["id"] for item in view["independent_evidence"]}, {human["id"], signed["id"]})
        self.assertFalse(human["runtime_verified"])
        _check(human, "integrity-observation.schema.json")

    def test_agent_activity_keeps_causal_references(self):
        generation = self._generation()
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.store.seal(generation["id"], "operator")
        self.store.launch(generation["id"], "operator")
        agent = self.store.register_agent({
            "generation_id": generation["id"],
            "approach_id": approach["id"],
            "principal": "agent-a",
            "role": "reviewer",
            "hypothesis_summary": "the handler is the seam",
            "open_uncertainties": ["lease expiry"],
        }, "operator")
        self.assertEqual(agent["authority"], "advisory")
        inspect = self.store.record_activity(agent["id"], {"activity": "inspect", "artifact": "syberwork/server.py"}, "agent-a")
        produced = self.store.record_activity(agent["id"], {
            "activity": "produce_candidate",
            "candidate_id": "cand-1",
            "caused_by": [inspect["id"]],
            "finding": "route table",
        }, "agent-a")
        actor = self.store.actor_view(agent["id"])
        self.assertIn({
            "from": inspect["id"],
            "to": produced["id"],
            "activity": "produce_candidate",
            "kind": "agent_activity_recorded",
        }, actor["causality"])
        self.assertEqual(self.store.generation_view(generation["id"])["generation"]["state"], "evaluating")
        with self.assertRaises(Rejected):
            self.store.record_activity(agent["id"], {"activity": "inspect", "chain_of_thought": "hidden"}, "agent-a")

    def test_commands_do_not_mutate_a_process_and_redirect_is_not_send_context(self):
        generation = self._generation()
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.store.seal(generation["id"], "operator")
        agent = self.store.register_agent({
            "generation_id": generation["id"],
            "approach_id": approach["id"],
            "principal": "agent-a",
            "role": "implementer",
            "working_set": ["syberwork/server.py"],
        }, "operator")
        disconnected = self.store.command(agent["id"], "pause", {}, "manager")
        self.assertEqual(disconnected["result"]["reason"], "runtime_not_connected")
        self.assertEqual(self.store.actor_view(agent["id"])["agent"]["state"], "registered")
        _check(disconnected, "agent-command.schema.json")
        self.store.command(agent["id"], "pause", {}, "manager", AcceptingRuntime())
        self.assertEqual(self.store.actor_view(agent["id"])["agent"]["state"], "paused")
        before = self.store.actor_view(agent["id"])["agent"]["assignment"]
        seen = {event["id"] for event in self._events()}
        self.store.command(agent["id"], "send_context", {"text": "read the route table"}, "manager", AcceptingRuntime())
        self.assertEqual(self.store.actor_view(agent["id"])["agent"]["assignment"], before)
        self.store.command(agent["id"], "redirect", {"objective": "narrow the export"}, "manager", AcceptingRuntime())
        fresh = [event for event in self._events() if event["id"] not in seen]
        self.assertEqual([event["kind"] for event in fresh], ["agent_activity_recorded", "agent_assignment_changed"])
        self.assertEqual(fresh[0]["body"]["activity"], "send_context")
        self.assertEqual(fresh[1]["body"]["assignment"]["objective"], "narrow the export")
        self.assertEqual(self.store.actor_view(agent["id"])["agent"]["assignment"]["objective"], "narrow the export")
        self.assertEqual(self.store.actor_view(agent["id"])["agent"]["state"], "paused")

    def test_path_mapping_and_role_lens_do_not_fork_state(self):
        generation = self._generation()
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.store.seal(generation["id"], "operator")
        agent = self.store.register_agent({
            "generation_id": generation["id"],
            "approach_id": approach["id"],
            "principal": "agent-a",
            "role": "implementer",
            "hypothesis_summary": "keep the handler thin",
            "open_uncertainties": ["which node"],
        }, "operator")
        snapshot = self.store.publish_architecture(architecture(self.case, generation["id"]), "operator")
        _check(snapshot, "architecture-snapshot.schema.json")
        self.store.link_candidate(generation["id"], approach["id"], "cand-1", ["syberwork/server.py"], "operator")
        candidate = self.store.candidate_views(generation["id"])[0]
        self.assertEqual(candidate["architecture_nodes"][0]["id"], "api")
        self.assertFalse(candidate["case_candidate_recorded"])
        self.assertIsNone(candidate["promotion_state"])
        later = dict(architecture(self.case, generation["id"], revision="def456"))
        later["baseline_id"] = snapshot["id"]
        later["nodes"] = [{"id": "api", "label": "API", "paths": ["syberwork/builder_http.py"]}]
        later["edges"] = []
        published = self.store.publish_architecture(later, "operator")
        diff = self.store.architecture_view(published["id"], snapshot["id"])["diff"]
        self.assertEqual(diff["changed_nodes"][0]["id"], "api")
        framed = self.store.project(self.store.actor_view(agent["id"]), {"name": "bao", "roles": ["intended_user"]})
        self.assertNotIn("hypothesis_summary", framed["agent"])
        self.assertEqual(framed["redacted"], ["hypothesis_summary", "open_uncertainties"])
        stored = self.store.actor_view(agent["id"])["agent"]["hypothesis_summary"]
        self.assertEqual(stored, "keep the handler thin")
        engineer = self.store.project(self.store.actor_view(agent["id"]), {"name": "ada", "roles": ["engineer"]})
        self.assertEqual(engineer["agent"]["hypothesis_summary"], stored)
        self.assertFalse(engineer["authoritative"])
        _check(self.store.world(self.case), "projection.schema.json")

    def test_contracts_round_trip_through_the_schemas(self):
        generation = self._generation()
        _check(generation, "generation.schema.json")
        _check(self.store.install_policy(POLICY, "operator"), "selection-policy.schema.json")
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        _check(approach, "approach.schema.json")
        event = self._events()[0]
        _check(event, "coordination-event.schema.json")
        self.store.seal(generation["id"], "operator")
        self.store.launch(generation["id"], "operator")
        agent = self.store.register_agent({
            "generation_id": generation["id"],
            "approach_id": approach["id"],
            "principal": "agent-a",
            "role": "implementer",
        }, "operator")
        _check(agent, "agent-session.schema.json")
        self.store.link_candidate(generation["id"], approach["id"], "cand-1", ["syberwork/server.py"], "operator")
        prototype = self.store.register_prototype({
            "case_id": self.case,
            "generation_id": generation["id"],
            "candidate_id": "cand-1",
            "artifact_ref": "build://cand-1",
            "environment": "local",
            "provider": "registry",
            "state": "building",
        }, "operator")
        ready = self.store.set_prototype_state(prototype["id"], "ready", "operator")
        _check(ready, "prototype.schema.json")
        for event in self._events():
            _check(event, "coordination-event.schema.json")
        _check(self.store.generation_view(generation["id"]), "projection.schema.json")

    def test_end_to_end_coordination_leaves_admission_authoritative(self):
        self.store.install_policy(POLICY, "operator")
        _check(self.store.install_policy(POLICY, "operator"), "selection-policy.schema.json")
        work = self.store.open_work(self.case, "Ship a reviewable export", "operator")
        self.assertEqual(work["objective"], "Ship a reviewable export")
        generation = self.store.create_generation({
            "case_id": self.case,
            "objective": "Four structures before any implementation",
            "base_revision": "abc123",
            "mode": "explore",
            "isolation": "aware",
            "diversity_threshold": 0.3,
            "min_approaches": 4,
            "selection_policy_id": "review",
            "selection_policy_version": 1,
        }, "operator")
        traits = ["spa_local", "mpa_server", "cli_files", "native_sqlite"]
        paths = ["syberwork/server.py", "syberwork/core.py", "syberwork/storage.py", "syberlabs/builder.py"]
        approaches = [self.store.register_approach(generation["id"], descriptor(trait), "operator") for trait in traits]
        with self.assertRaises(Rejected) as similar:
            self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.assertTrue(similar.exception.code.startswith("approach_too_similar:"))
        sealed = self.store.seal(generation["id"], "operator")
        self.assertEqual(sealed["state"], "sealed")
        agents = []
        for approach in approaches:
            agents.append(self.store.register_agent({
                "generation_id": generation["id"],
                "approach_id": approach["id"],
                "principal": "agent-" + approach["descriptor"]["intent"],
                "role": "implementer",
                "hypothesis_summary": "owned by " + approach["descriptor"]["intent"],
                "working_set": ["syberwork/server.py"],
            }, "operator"))
        launched = self.store.launch(generation["id"], "operator")
        self.assertEqual(launched["state"], "launched")
        first_event = self.store.record_activity(agents[0]["id"], {"activity": "inspect", "artifact": paths[0]}, "operator")
        self.store.record_activity(agents[0]["id"], {
            "activity": "produce_candidate",
            "candidate_id": "cand-1",
            "caused_by": [first_event["id"]],
        }, "operator")
        for index, approach in enumerate(approaches):
            self.store.link_candidate(generation["id"], approach["id"], f"cand-{index + 1}", [paths[index]], "operator")
        snapshot = self.store.publish_architecture(architecture(self.case, generation["id"]), "operator")
        mapped = [self.store.map_paths(snapshot["id"], [path]) for path in paths]
        self.assertEqual([item[0]["id"] for item in mapped], ["api", "core", "storage", "builder"])
        prototype = self.store.register_prototype({
            "case_id": self.case,
            "generation_id": generation["id"],
            "candidate_id": "cand-1",
            "artifact_ref": "build://cand-1",
            "environment": "local",
            "provider": "registry",
            "endpoint": "http://127.0.0.1:9",
            "state": "building",
        }, "operator")
        ready = self.store.set_prototype_state(prototype["id"], "ready", "operator")
        self.assertEqual(ready["state"], "ready")
        self.store.record_integrity({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "candidate",
            "target_id": "cand-1",
            "claim": "the export handler drops an error path",
            "source": "engineer-review",
            "verifier": "engineer",
            "independence": "human_reviewed",
            "result": "concern",
            "evidence_refs": ["syberwork/server.py"],
        }, "engineer")
        preference = self.store.record_feedback({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "candidate",
            "target_id": "cand-1",
            "kind": "preference",
            "text": "the server-rendered flow matches the task",
            "authority": "decision",
        }, "bao", ["intended_user"])
        self.assertEqual(preference["authority"], "advisory")
        projection = self.store.generation_view(generation["id"])
        self.assertEqual(projection["kind"], "GenerationProjection")
        self.assertFalse(projection["authoritative"])
        self.assertEqual(len([item for item in projection["approaches"] if item["state"] == "frozen"]), 4)
        self.assertEqual(projection["feedback_dimensions"]["by_kind"]["preference"][0]["text"], preference["text"])
        self.assertEqual(projection["feedback_dimensions"]["by_authority"]["advisory"][0]["authority"], "advisory")
        self.assertEqual(projection["integrity"]["independent_evidence"][0]["result"], "concern")
        self.assertEqual(projection["candidates"][0]["architecture_nodes"][0]["id"], "api")
        self.assertEqual(projection["candidates"][0]["prototype"]["state"], "ready")
        self.assertFalse(projection["candidates"][0]["case_candidate_recorded"])
        selection = self.store.select(generation["id"], "manager", ["manager"])
        self.assertFalse(selection["promotes_git"])
        self.assertEqual(selection["unresolved"][0]["reasons"], ["integrity_required:human_reviewed"])
        self.assertEqual(selection["dimensions"]["cand-1"]["feedback"]["by_kind"]["preference"][0]["authority"], "advisory")
        self.assertIn("aggregate", selection["dimensions"]["cand-1"])
        reference = self.store.promotion_reference(selection)
        self.assertFalse(reference["promotes_git"])
        nxt = self.store.create_generation({
            "case_id": self.case,
            "objective": "Refine the selected direction",
            "base_revision": "abc123",
            "mode": "refine",
            "isolation": "aware",
            "diversity_threshold": 0.3,
            "min_approaches": 2,
            "selection_policy_id": "review",
            "selection_policy_version": 1,
        }, "operator")
        self.assertEqual(nxt["ordinal"], 2)
        self.assertEqual(nxt["state"], "drafting")
        kinds = [event["kind"] for event in self._events()]
        self.assertLess(kinds.index("generation_sealed"), kinds.index("candidate_linked"))
        self.assertLess(kinds.index("approach_rejected"), kinds.index("generation_sealed"))
        self.assertLess(kinds.index("generation_launched"), kinds.index("agent_activity_recorded"))
        self.assertLess(kinds.index("selection_recorded"), kinds.index("generation_created", 1))
        self.assertEqual([event["seq"] for event in self._events()], list(range(1, len(kinds) + 1)))
        world = self.store.world(self.case)
        self.assertEqual(world["kind"], "WorldProjection")
        self.assertEqual(world["case_authority"]["event_kinds"], ["case_created"])
        self.assertEqual([item["ordinal"] for item in world["generations"]], [1, 2])
        self._assert_authority_unchanged()


class PostgresStoreCases(StoreCases):
    dialect = "postgres"


class HttpCases(StoreCases):
    def setUp(self):
        super().setUp()
        users = {
            name: {"hash": hashlib.sha256(name.encode()).hexdigest(), "roles": roles, "sources": []}
            for name, roles in {
                "operator": ["operator"],
                "engineer": ["engineer"],
                "intended": ["intended_user"],
                "manager": ["manager"],
                "observer": ["observer"],
            }.items()
        }
        self.server = make_server(self.work, users, port=0)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, who, path, data=None, headers=None):
        head = {"Authorization": "Bearer " + who}
        if data is not None:
            head["Content-Type"] = "application/json"
        if headers:
            head.update(headers)
        request = urllib.request.Request(
            self.base + path,
            data=None if data is None else json.dumps(data).encode(),
            headers=head,
            method="GET" if data is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                content = response.headers.get("Content-Type", "")
                if content.startswith("text/event-stream"):
                    return response.status, raw.decode()
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_routes_stay_compatible_and_the_stream_replays(self):
        status, missing = self.call("operator", "/api/builder/work/" + self.case)
        self.assertEqual(status, 200)
        self.assertEqual(missing["objective"], None)
        status, created = self.call("operator", "/api/cases", {"contract_id": "note", "version": 1, "inputs": {"name": "b"}})
        self.assertEqual(status, 200)
        self.assertIn("id", created)
        status, listed = self.call("operator", "/api/cases")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(listed), 1)
        denied = urllib.request.Request(self.base + "/api/builder/work/" + self.case)
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(denied, timeout=5)
        self.assertEqual(blocked.exception.code, 401)
        self.assertEqual(self.call("observer", "/api/builder/generations", {
            "case_id": self.case,
            "objective": "no",
            "base_revision": "abc123",
            "mode": "explore",
            "isolation": "aware",
            "min_approaches": 1,
            "selection_policy_id": "review",
            "selection_policy_version": 1,
        })[0], 409)
        self.assertEqual(self.call("operator", "/api/builder/policies", POLICY)[0], 200)
        self.assertEqual(self.call("operator", f"/api/builder/work/{self.case}", {"objective": "Ship a reviewable export"})[0], 200)
        status, generation = self.call("operator", "/api/builder/generations", {
            "case_id": self.case,
            "objective": "One approach is enough for the route test",
            "base_revision": "abc123",
            "mode": "explore",
            "isolation": "aware",
            "diversity_threshold": 0.3,
            "min_approaches": 1,
            "selection_policy_id": "review",
            "selection_policy_version": 1,
        })
        self.assertEqual(status, 200)
        status, stream = self.call("operator", f"/api/builder/work/{self.case}/stream?once=1")
        self.assertEqual(status, 200)
        self.assertIn(": heartbeat\n", stream)
        first = _sse(stream)
        self.assertEqual(first[0][1]["kind"], "generation_created")
        self.assertEqual(self.call("operator", f"/api/builder/generations/{generation['id']}/approaches", {
            "descriptor": descriptor("spa_local"),
        })[0], 200)
        status, replay = self.call(
            "operator",
            f"/api/builder/work/{self.case}/stream?once=1",
            headers={"Last-Event-ID": str(first[-1][0])},
        )
        self.assertEqual(status, 200)
        second = _sse(replay)
        self.assertEqual([item[1]["kind"] for item in second], ["approach_registered"])
        self.assertGreater(second[0][0], first[-1][0])
        self.assertEqual(self.call("operator", f"/api/builder/generations/{generation['id']}/seal", {})[0], 200)
        status, agent = self.call("operator", "/api/builder/agents", {
            "generation_id": generation["id"],
            "approach_id": self.store.generation_view(generation["id"])["approaches"][0]["id"],
            "principal": "agent-a",
            "role": "implementer",
            "hypothesis_summary": "private to engineers",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.call("operator", f"/api/builder/generations/{generation['id']}/launch", {})[0], 200)
        self.assertEqual(self.call("operator", f"/api/builder/agents/{agent['id']}/activity", {"activity": "inspect"})[0], 200)
        self.assertEqual(self.call("intended", "/api/builder/feedback", {
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "kind": "preference",
            "text": "keep it visible",
            "authority": "decision",
        })[1]["authority"], "advisory")
        engineer = _sse(self.call("engineer", f"/api/builder/work/{self.case}/stream?once=1")[1])
        intended = _sse(self.call("intended", f"/api/builder/work/{self.case}/stream?once=1")[1])
        self.assertIn("agent_activity_recorded", [item[1]["kind"] for item in engineer])
        self.assertNotIn("agent_activity_recorded", [item[1]["kind"] for item in intended])
        self.assertIn("feedback_recorded", [item[1]["kind"] for item in intended])
        self.assertEqual([item[0] for item in engineer], sorted(item[0] for item in engineer))
        status, world = self.call("intended", f"/api/builder/work/{self.case}")
        self.assertEqual(status, 200)
        self.assertFalse(world["authoritative"])
        self.assertEqual(world["lens"], ["intended_user"])
        status, candidates = self.call("engineer", f"/api/builder/generations/{generation['id']}/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(candidates["kind"], "CandidateProjection")
        self.assertEqual(self.call("engineer", f"/api/builder/agents/{agent['id']}")[1]["kind"], "ActorProjection")
        self.assertNotIn("hypothesis_summary", self.call("intended", f"/api/builder/agents/{agent['id']}")[1]["agent"])
        snapshot = self.call("operator", "/api/builder/architecture", architecture(self.case, generation["id"]))[1]
        self.assertEqual(self.call("engineer", f"/api/builder/architecture/{snapshot['id']}")[1]["snapshot"]["id"], snapshot["id"])
        self.assertEqual(self.call("engineer", "/api/builder/integrity", {
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "claim": "read the diff",
            "source": "review",
            "verifier": "engineer",
            "independence": "human_reviewed",
            "result": "concern",
        })[0], 200)
        sign = self.call("engineer", f"/api/builder/integrity/{generation['id']}")[1]
        self.assertEqual(sign["kind"], "IntegrityProjection")
        self.assertEqual(sign["independent_evidence"][0]["independence"], "human_reviewed")
        self.assertEqual(self._authority(), self.authority)


def _sse(text: str):
    found = []
    for block in text.split("\n\n"):
        if not block.startswith("id:"):
            continue
        seq = None
        data = None
        for line in block.split("\n"):
            if line.startswith("id:"):
                seq = int(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        found.append((seq, data))
    return found


class Migration(unittest.TestCase):
    def test_main_fixture_keeps_its_hashes_and_old_snapshots_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "main.sqlite"
            shutil.copy(FIXTURE, copy)
            before = sqlite3.connect(copy)
            hashes = [row[0] for row in before.execute("SELECT hash FROM events ORDER BY case_id, seq")]
            before.close()
            opened = Work(copy)
            meta = json.loads((FIXTURE.parent / "main_history.json").read_text())
            try:
                versions = [row["version"] for row in opened._db.execute("SELECT version FROM schema_migrations ORDER BY version")]
                self.assertIn("0004", versions)
                self.assertTrue(opened.verify_chain(meta["case_id"]))
                self.assertEqual(opened.inspect(meta["case_id"])["events"][0]["kind"], "case_created")
                after = [row["hash"] for row in opened._db.execute("SELECT hash FROM events ORDER BY case_id, seq")]
                self.assertEqual(after, hashes)
                count = opened._db.execute("SELECT COUNT(*) AS n FROM coordination_events").fetchone()["n"]
                self.assertEqual(count, 0)
            finally:
                opened.close()

            source = Work(Path(folder) / "source.sqlite")
            source.install_contract(NOTE)
            source.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
            source.install_action("note", {"kind": "local"})
            case_id = source.create_case("note", 1, {"name": "a"}, "operator")
            store = BuilderStore(source)
            store.install_policy(POLICY, "operator")
            generation = store.create_generation({
                "case_id": case_id,
                "objective": "persist",
                "base_revision": "abc123",
                "mode": "harden",
                "isolation": "independent",
                "diversity_threshold": 0.3,
                "min_approaches": 1,
                "selection_policy_id": "review",
                "selection_policy_version": 1,
            }, "operator")
            snapshot = Path(folder) / "snapshot"
            export_cell(source, snapshot)
            source.close()
            restored = Work(Path(folder) / "restored.sqlite")
            restore_cell(restored, snapshot)
            try:
                self.assertTrue(restored.verify_chain(case_id))
                self.assertEqual(BuilderStore(restored).generation_view(generation["id"])["generation"]["state"], "drafting")
            finally:
                restored.close()
            for name in (
                "coordination_events",
                "builder_works",
                "builder_policies",
                "builder_generations",
                "builder_approaches",
                "builder_agents",
                "builder_feedback",
                "builder_integrity",
                "builder_selections",
                "builder_architecture",
                "builder_prototypes",
                "builder_commands",
                "builder_candidate_links",
            ):
                (snapshot / f"{name}.json").unlink()
            legacy = Work(Path(folder) / "legacy.sqlite")
            restore_cell(legacy, snapshot)
            try:
                self.assertTrue(legacy.verify_chain(case_id))
                self.assertIsNone(BuilderStore(legacy).world(case_id)["objective"])
                self.assertEqual(BuilderStore(legacy).world(case_id)["generations"], [])
            finally:
                legacy.close()


if __name__ == "__main__":
    unittest.main()
