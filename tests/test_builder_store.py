"""Builder coordination stored in a cell database."""

import tempfile
import unittest
from pathlib import Path

from syberlabs.builder import nodes_for_paths, project
from syberlabs.errors import Rejected
from syberwork.coordination import BuilderStore
from syberwork.core import Work
from tests.builder_fixtures import (
    NOTE, POLICY, AcceptingRuntime, CellCase, _check, architecture, descriptor, git_candidate,
)
from tests.test_cell import _drop_postgres, _postgres_database

class StoreCases(CellCase):
    dialect = "sqlite"

    def test_generation_barrier_blocks_candidates_until_seal(self):
        generation = self._generation(min_approaches=2)
        first = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        candidate_id = self._record(1, ["syberwork/server.py"])
        with self.assertRaises(Rejected) as blocked:
            self.store.link_candidate(generation["id"], first["id"], candidate_id, "operator")
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
        with self.assertRaises(Rejected) as missing:
            self.store.link_candidate(generation["id"], first["id"], "not-a-candidate", "operator")
        self.assertEqual(missing.exception.code, "candidate_unrecorded")
        with self.assertRaises(Rejected) as paths:
            self.store.link_candidate(generation["id"], first["id"], candidate_id, "operator", ["syberwork/other.py"])
        self.assertEqual(paths.exception.code, "candidate_paths_mismatch")
        linked = self.store.link_candidate(generation["id"], first["id"], candidate_id, "operator")
        self.assertEqual(linked["candidate_id"], candidate_id)
        self.assertEqual(linked["changed_paths"], ["syberwork/server.py"])
        self.assertEqual(linked["commit"], f"{1:040x}")
        self.assertEqual(self.store.launch(generation["id"], "operator")["state"], "launched")
        self.assertEqual([kind for kind, _digest in self._authority()], ["case_created", "candidate_registered"])
        self.assertTrue(self.work.verify_chain(self.case))

    def test_similar_approach_is_rejected_and_recorded(self):
        generation = self._generation()
        first = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        with self.assertRaises(Rejected) as caught:
            self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.assertEqual(caught.exception.code, f"approach_too_similar:{first['id']}")
        approaches = self.store.generation_view(generation["id"])["approaches"]
        rejected = next(item for item in approaches if item["state"] == "rejected")
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
        self._record(1, ["syberwork/server.py"])
        self.store.link_candidate(generation["id"], first["id"], "c1", "operator")
        self.assertEqual(self.store.manifest(agent["id"])["implementations"], [])

        collaborative = self._generation(isolation="collaborative", min_approaches=2, objective="Share implementation state")
        own = self.store.register_approach(collaborative["id"], descriptor("cli_files"), "operator")
        sibling = self.store.register_approach(collaborative["id"], descriptor("native_sqlite"), "operator")
        self.store.seal(collaborative["id"], "operator")
        self._record(2, ["syberwork/core.py"])
        self.store.link_candidate(collaborative["id"], sibling["id"], "c2", "operator")
        other = self.store.register_agent({
            "generation_id": collaborative["id"],
            "approach_id": own["id"],
            "principal": "agent-b",
            "role": "implementer",
        }, "operator")
        shared = self.store.manifest(other["id"])
        self.assertEqual(shared["implementations"], [{
            "candidate_id": "c2",
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
        }, "bao", ["intended_user"])
        self.assertEqual(recorded["authorities"], ["advisory"])
        _check(recorded, "feedback.schema.json")
        with self.assertRaises(Rejected) as caught:
            self.store.record_feedback({
                "case_id": self.case,
                "generation_id": generation["id"],
                "target_kind": "generation",
                "target_id": generation["id"],
                "kind": "comment",
                "text": "hello",
            }, "guest", ["observer"])
        self.assertEqual(caught.exception.code, "feedback_authority")

    def test_selection_does_not_promote_or_touch_the_case_chain(self):
        generation = self._generation()
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        self.store.seal(generation["id"], "operator")
        self.store.launch(generation["id"], "operator")
        self._record(1, ["syberwork/server.py"])
        self.store.link_candidate(generation["id"], approach["id"], "c1", "operator")
        self.store.record_feedback({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "candidate",
            "target_id": "c1",
            "kind": "dissent",
            "text": "stop",
        }, "ada", ["veto"])
        with self.assertRaises(Rejected) as ghost:
            self.store.record_feedback({
                "case_id": self.case,
                "generation_id": generation["id"],
                "target_kind": "candidate",
                "target_id": "missing",
                "kind": "preference",
                "text": "ghost",
            }, "bao", ["intended_user"])
        self.assertEqual(ghost.exception.code, "unknown_target")
        selection = self.store.select(generation["id"], "manager", ["manager"])
        self.assertFalse(selection["promotes_git"])
        self.assertEqual(selection["rejected"][0]["id"], "c1")
        self.assertTrue(selection["promotion_authorized"])
        self.assertFalse(selection["promotes_git"])
        self.assertTrue(selection["evidence_digest"])
        _check(selection, "selection.schema.json")
        self.assertEqual(
            [kind for kind, _digest in self._authority()],
            ["case_created", "candidate_registered"],
        )
        self.assertTrue(self.work.verify_chain(self.case))

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
        }, "operator", principal_kind="human")
        claimed = self.store.record_integrity({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "claim": "external signature",
            "source": "caller",
            "verifier": "operator",
            "independence": "signed_external",
            "result": "pass",
            "digest": digest,
        }, "operator", principal_kind="human")
        host = self.store.record_host_integrity({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "claim": "the host recomputed the diff",
            "source": "syberwork",
            "verifier": "operator",
            "independence": "host_verified",
            "result": "pass",
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
        view = self.store.integrity_view(generation["id"], case_id=self.case, target_kind="generation", generation_id=generation["id"])
        self.assertEqual(view["kind"], "IntegrityProjection")
        self.assertFalse(view["authoritative"])
        self.assertEqual([item["id"] for item in view["self_report"]], [internal["id"]])
        self.assertEqual(internal["verification_method"], "self_report")
        self.assertEqual({item["id"] for item in view["independent_evidence"]}, {human["id"], host["id"]})
        self.assertEqual(claimed["verification_status"], "unverified")
        self.assertIsNone(claimed["verified_independence"])
        self.assertEqual([item["id"] for item in view["unverified_claims"]], [claimed["id"]])
        self.assertEqual(host["verification_method"], "host_mechanism")
        self.assertEqual(human["verified_independence"], "human_reviewed")
        _check(human, "integrity-observation.schema.json")
        _check(claimed, "integrity-observation.schema.json")

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
        inspect = self.store.record_activity(agent["id"], {"activity": "inspect", "artifact": "syberwork/server.py"}, "agent-a", ["agent"])
        produced = self.store.record_activity(agent["id"], {
            "activity": "produce_candidate",
            "candidate_id": "cand-1",
            "caused_by": [inspect["id"]],
            "finding": "route table",
        }, "agent-a", ["agent"])
        actor = self.store.actor_view(agent["id"])
        self.assertIn({
            "from": inspect["id"],
            "to": produced["id"],
            "activity": "produce_candidate",
            "kind": "agent_activity_recorded",
        }, actor["causality"])
        self.assertEqual(self.store.generation_view(generation["id"])["generation"]["state"], "evaluating")
        with self.assertRaises(Rejected):
            self.store.record_activity(agent["id"], {"activity": "inspect", "chain_of_thought": "hidden"}, "agent-a", ["agent"])

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
        self._record(3, ["syberwork/server.py"])
        self.store.link_candidate(generation["id"], approach["id"], "c3", "operator")
        candidate = self.store.candidate_views(generation["id"])[0]
        self.assertEqual(candidate["architecture_nodes"][0]["id"], "api")
        self.assertEqual(candidate["changed_paths"], ["syberwork/server.py"])
        self.assertEqual(candidate["commit"], f"{3:040x}")
        self.assertIsNone(candidate["promotion_state"])
        later = dict(architecture(self.case, generation["id"], revision="def456"))
        later["baseline_id"] = snapshot["id"]
        later["nodes"] = [{"id": "api", "label": "API", "paths": ["syberwork/builder_http.py"]}]
        later["edges"] = []
        published = self.store.publish_architecture(later, "operator")
        diff = self.store.architecture_view(published["id"], snapshot["id"])["diff"]
        self.assertEqual(diff["changed_nodes"][0]["id"], "api")
        framed = project(self.store.actor_view(agent["id"]), {"name": "bao", "roles": ["intended_user"]})
        self.assertNotIn("hypothesis_summary", framed["agent"])
        self.assertEqual(framed["redacted"], ["hypothesis_summary", "open_uncertainties"])
        stored = self.store.actor_view(agent["id"])["agent"]["hypothesis_summary"]
        self.assertEqual(stored, "keep the handler thin")
        engineer = project(self.store.actor_view(agent["id"]), {"name": "ada", "roles": ["engineer"]})
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
        self._record(4, ["syberwork/server.py"])
        self.store.link_candidate(generation["id"], approach["id"], "c4", "operator")
        prototype = self.store.register_prototype({
            "case_id": self.case,
            "generation_id": generation["id"],
            "candidate_id": "c4",
            "artifact_ref": "build://c4",
            "environment": "local",
            "provider": "registry",
            "state": "building",
        }, "operator")
        ready = self.store.set_prototype_state(prototype["id"], "ready", "operator")
        _check(ready, "prototype.schema.json")
        for target in ("building", "ready", "bogus"):
            with self.assertRaises(Rejected) as refused:
                self.store.set_prototype_state(prototype["id"], target, "operator")
            self.assertEqual(refused.exception.code, "invalid_prototype")
        self.assertEqual(self.store.generation_view(generation["id"])["prototypes"][0]["state"], "ready")
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
        first_event = self.store.record_activity(agents[0]["id"], {"activity": "inspect", "artifact": paths[0]}, "operator", ["operator"])
        self.store.record_activity(agents[0]["id"], {
            "activity": "produce_candidate",
            "candidate_id": "c1",
            "caused_by": [first_event["id"]],
        }, "operator", ["operator"])
        for index, path in enumerate(paths):
            self._record(index + 1, [path])
        with self.assertRaises(Rejected) as ghost:
            self.store.record_integrity({
                "case_id": self.case,
                "generation_id": generation["id"],
                "target_kind": "candidate",
                "target_id": "c1",
                "claim": "too early",
                "source": "review",
                "verifier": "engineer",
                "independence": "signed_external",
                "result": "pass",
                "digest": "b" * 64,
            }, "engineer")
        self.assertEqual(ghost.exception.code, "unknown_target")
        for index, approach in enumerate(approaches):
            self.store.link_candidate(generation["id"], approach["id"], f"c{index + 1}", "operator")
        snapshot = self.store.publish_architecture(architecture(self.case, generation["id"]), "operator")
        mapped = [nodes_for_paths(snapshot, [path]) for path in paths]
        self.assertEqual([item[0]["id"] for item in mapped], ["api", "core", "storage", "builder"])
        prototype = self.store.register_prototype({
            "case_id": self.case,
            "generation_id": generation["id"],
            "candidate_id": "c1",
            "artifact_ref": "build://c1",
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
            "target_id": "c1",
            "claim": "the export handler drops an error path",
            "source": "engineer-review",
            "verifier": "engineer",
            "independence": "human_reviewed",
            "result": "concern",
            "evidence_refs": ["syberwork/server.py"],
        }, "engineer", principal_kind="human")
        forged = self.store.record_integrity({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "candidate",
            "target_id": "c1",
            "claim": "a caller-supplied seal",
            "source": "caller",
            "verifier": "engineer",
            "independence": "signed_external",
            "result": "pass",
            "digest": "a" * 64,
        }, "engineer", principal_kind="human")
        self.assertEqual(forged["verification_status"], "unverified")
        self.assertIsNone(forged["verified_independence"])
        preference = self.store.record_feedback({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "candidate",
            "target_id": "c1",
            "kind": "preference",
            "text": "the server-rendered flow matches the task",
        }, "bao", ["intended_user"])
        self.assertEqual(preference["authorities"], ["advisory"])
        projection = self.store.generation_view(generation["id"])
        self.assertEqual(projection["kind"], "GenerationProjection")
        self.assertFalse(projection["authoritative"])
        self.assertEqual(len([item for item in projection["approaches"] if item["state"] == "frozen"]), 4)
        self.assertEqual(projection["feedback_dimensions"]["by_kind"]["preference"][0]["text"], preference["text"])
        self.assertEqual(projection["feedback_dimensions"]["by_authority"]["advisory"][0]["authorities"], ["advisory"])
        self.assertEqual(projection["integrity"]["independent_evidence"][0]["result"], "concern")
        self.assertEqual(projection["integrity"]["unverified_claims"][0]["independence_claim"], "signed_external")
        chosen = next(item for item in projection["candidates"] if item["candidate_id"] == "c1")
        self.assertEqual(chosen["architecture_nodes"][0]["id"], "api")
        self.assertEqual(chosen["commit"], f"{1:040x}")
        self.assertEqual(chosen["prototype"]["state"], "ready")
        self.assertEqual(chosen["changed_paths"], ["syberwork/server.py"])
        selection = self.store.select(generation["id"], "manager", ["manager"])
        self.assertFalse(selection["promotes_git"])
        self.assertEqual(selection["advanced"], [])
        self.assertEqual(
            next(item for item in selection["unresolved"] if item["id"] == "c1")["reasons"],
            ["integrity_required:human_reviewed"],
        )
        self.assertEqual(selection["dimensions"]["c1"]["feedback"]["by_kind"]["preference"][0]["authorities"], ["advisory"])
        self.assertNotIn("aggregate", selection["dimensions"]["c1"])
        self.assertFalse(selection["promotes_git"])
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
        self.assertEqual(world["case_authority"]["event_count"], 5)
        case_kinds = [event["kind"] for event in self.work.inspect(self.case)["events"]]
        self.assertEqual(case_kinds, ["case_created", "candidate_registered", "candidate_registered", "candidate_registered", "candidate_registered"])
        self.assertEqual([item["ordinal"] for item in world["generations"]], [1, 2])
        self.assertTrue(self.work.verify_chain(self.case))
        self.assertFalse({"decision", "effect_started", "effect_succeeded", "approved"} & set(case_kinds))


    def test_an_agent_cannot_complete_another_agents_session(self):
        generation = self._generation(min_approaches=2)
        first = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        second = self.store.register_approach(generation["id"], descriptor("mpa_server"), "operator")
        self.store.seal(generation["id"], "operator")
        self.store.launch(generation["id"], "operator")
        agent_a = self.store.register_agent({
            "generation_id": generation["id"], "approach_id": first["id"], "principal": "agent-a", "role": "implementer",
        }, "operator")
        self.store.register_agent({
            "generation_id": generation["id"], "approach_id": second["id"], "principal": "agent-b", "role": "implementer",
        }, "operator")
        with self.assertRaises(Rejected) as caught:
            self.store.record_activity(agent_a["id"], {"activity": "complete"}, "agent-b", ["agent"])
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(self.store.actor_view(agent_a["id"])["agent"]["state"], "registered")
        self.store.record_activity(agent_a["id"], {"activity": "inspect"}, "operator", ["operator"])
        self.assertEqual(self.store.actor_view(agent_a["id"])["agent"]["state"], "active")
        self.store.command(agent_a["id"], "cancel", {}, "manager", AcceptingRuntime())
        with self.assertRaises(Rejected) as cancelled:
            self.store.record_activity(agent_a["id"], {"activity": "complete"}, "agent-a", ["agent"])
        self.assertEqual(cancelled.exception.code, "agent_state")
        self.assertEqual(self.store.actor_view(agent_a["id"])["agent"]["state"], "cancelled")

    def test_redirect_rereads_the_assignment_after_the_runtime_returns(self):
        generation = self._generation(min_approaches=2)
        first = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        second = self.store.register_approach(generation["id"], descriptor("mpa_server"), "operator")
        self.store.seal(generation["id"], "operator")
        agent = self.store.register_agent({
            "generation_id": generation["id"], "approach_id": first["id"], "principal": "agent-a", "role": "implementer",
        }, "operator")

        class DuringRuntime:
            def apply(self, command):
                self_store.command(agent["id"], "redirect", {"objective": "from the runtime", "approach_id": second["id"]}, "manager", AcceptingRuntime())
                return {"applied": True, "reason": "accepted"}

        self_store = self.store
        calls = []

        class Recording:
            def apply(self, command):
                calls.append(command)
                return {"applied": True, "reason": "accepted"}

        with self.assertRaises(Rejected) as unknown:
            self.store.command(agent["id"], "redirect", {"objective": "missing", "approach_id": "no-such-approach"}, "manager", Recording())
        self.assertEqual(unknown.exception.code, "unknown_approach")
        self.assertEqual(calls, [])
        with self.assertRaises(Rejected) as shape:
            self.store.command(agent["id"], "redirect", [1], "manager", Recording())
        self.assertEqual(shape.exception.code, "invalid_command")
        self.store.command(agent["id"], "redirect", {"objective": "from the caller"}, "manager", DuringRuntime())
        assignment = self.store.actor_view(agent["id"])["agent"]["assignment"]
        self.assertEqual(assignment["approach_id"], second["id"])
        self.assertEqual(assignment["objective"], "from the caller")

    def test_feedback_requires_its_generation(self):
        generation = self._generation()
        body = {
            "case_id": self.case,
            "target_kind": "generation",
            "target_id": generation["id"],
            "kind": "preference",
            "text": "ship it",
            "selection_policy_id": "other",
        }
        with self.assertRaises(Rejected) as caught:
            self.store.record_feedback(body, "bao", ["intended_user"])
        self.assertEqual(caught.exception.code, "invalid_feedback")
        recorded = self.store.record_feedback({**body, "generation_id": generation["id"]}, "bao", ["intended_user"])
        self.assertEqual(recorded["authorities"], ["advisory"])

    def test_integrity_and_architecture_ids_do_not_collide_across_scopes(self):
        self.store.install_policy(POLICY, "operator")
        first = self._generation(objective="first map")
        second = self._generation(objective="second map")
        left = self.store.publish_architecture({**architecture(self.case, first["id"]), "id": "main"}, "operator")
        right = self.store.publish_architecture({**architecture(self.case, second["id"]), "id": "main"}, "operator")
        self.assertNotEqual(left["id"], right["id"])
        self.assertNotEqual(left["id"], "main")
        for generation, result in ((first, "pass"), (second, "fail")):
            self.store.record_integrity({
                "case_id": self.case,
                "generation_id": generation["id"],
                "target_kind": "architecture_node",
                "target_id": "api",
                "claim": result,
                "source": "review",
                "verifier": "operator",
                "independence": "human_reviewed",
                "result": result,
            }, "operator", principal_kind="human")
        scoped = self.store.integrity_view("api", case_id=self.case, target_kind="architecture_node", generation_id=first["id"])
        self.assertEqual([item["result"] for item in scoped["observations"]], ["pass"])
        other = self.store.integrity_view("api", case_id=self.case, target_kind="architecture_node", generation_id=second["id"])
        self.assertEqual([item["result"] for item in other["observations"]], ["fail"])
        with self.assertRaises(Rejected) as unscoped:
            self.store.integrity_view("api", case_id=self.case, target_kind="architecture_node")
        self.assertEqual(unscoped.exception.code, "invalid_integrity")


class PostgresStoreCases(StoreCases):
    dialect = "postgres"


