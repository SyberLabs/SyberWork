"""Pure Builder coordination rules."""

import unittest
from pathlib import Path
from typing import get_args

from syberlabs.builder import (
    PROTOCOL,
    architecture_diff,
    assess_integrity,
    context_manifest,
    derive_authorities,
    diversity_evidence,
    evaluate_selection,
    nodes_for_paths,
    normalize_integrity,
    structural_distance,
    validate_snapshot,
)
from syberlabs.errors import Rejected
from syberlabs.protocol import SIDE_PROTOCOL, EventKind

from tests.builder_fixtures import POLICY, architecture, descriptor

class Domain(unittest.TestCase):
    def test_protocol_is_separate_from_the_frozen_case_vocabulary(self):
        self.assertEqual(PROTOCOL, "sdk.syberlabs.space/builder/v0alpha1")
        self.assertNotIn("builder", SIDE_PROTOCOL)
        self.assertNotIn("generation_created", get_args(EventKind))
        source = Path(__import__("syberlabs.builder", fromlist=["builder"]).__file__).read_text()
        self.assertNotIn("import syberwork", source)
        self.assertNotIn("from syberwork", source)

    def test_structural_distance_rejects_identical_claims(self):
        left, right = descriptor("spa_local"), descriptor("mpa_server")
        self.assertEqual(structural_distance(left, left), 0.0)
        self.assertEqual(structural_distance(left, right), 1.0)
        same_claims = descriptor("quantum_lattice")
        same_claims["distinguishing_claims"] = ["spa_local"]
        report = diversity_evidence(same_claims, [{"id": "sib", "descriptor": left}], 0.3)
        self.assertFalse(report["passed"])
        self.assertEqual(report["sibling_id"], "sib")
        pair = report["evidence"]["pairs"][0]
        self.assertTrue(pair["claims_equal"])
        self.assertGreaterEqual(pair["structural_distance"], 0.3)
        self.assertEqual(report["evidence"]["authoritative"], "structural")
        copied = diversity_evidence(left, [{"id": "sib", "descriptor": left}], 0.3)
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

    def test_feedback_authority_keeps_every_grant_for_the_kind(self):
        self.assertEqual(derive_authorities(["intended_user"], "preference", POLICY), ["advisory"])
        self.assertEqual(derive_authorities(["engineer", "manager"], "concern", POLICY), ["veto", "decision"])
        self.assertEqual(derive_authorities(["veto"], "dissent", POLICY), ["veto"])
        with self.assertRaises(Rejected) as caught:
            derive_authorities(["observer"], "comment", POLICY)
        self.assertEqual(caught.exception.code, "feedback_authority")
        with self.assertRaises(Rejected):
            derive_authorities(["intended_user"], "dissent", POLICY)

    def test_selection_keeps_dimensions_and_does_not_promote(self):
        candidates = ["c1", "c2"]
        feedback = [
            {"id": "f1", "target_kind": "candidate", "target_id": "c1", "kind": "concern", "authorities": ["veto", "decision"],
             "actor": "ada", "text": "do not ship", "at": 1},
            {"id": "f2", "target_kind": "candidate", "target_id": "c2", "kind": "preference", "authorities": ["advisory"],
             "actor": "bao", "text": "this one", "at": 2},
            {"id": "f3", "target_kind": "candidate", "target_id": "c2", "kind": "preference", "authorities": ["advisory"],
             "actor": "cam", "text": "also this", "at": 3},
        ]
        forged = assess_integrity(normalize_integrity({
            "target_kind": "candidate", "target_id": "c2", "independence": "signed_external", "result": "pass",
            "claim": "external seal", "source": "caller", "verifier": "bao", "digest": "a" * 64,
        }), principal_kind="human")
        forged["id"] = "forged"
        decision = evaluate_selection(POLICY, candidates, feedback, [forged], ["manager"])
        self.assertFalse(decision["promotes_git"])
        self.assertTrue(decision["promotion_authorized"])
        self.assertEqual(decision["rejected"][0]["id"], "c1")
        self.assertEqual(decision["rejected"][0]["reasons"], ["veto:ada"])
        self.assertEqual(decision["unresolved"][0]["id"], "c2")
        self.assertEqual(decision["unresolved"][0]["reasons"], ["integrity_required:human_reviewed"])
        self.assertEqual(forged["verification_status"], "unverified")
        self.assertIsNone(forged["verified_independence"])
        self.assertEqual(decision["advanced"], [])
        self.assertEqual(decision["dimensions"]["c2"]["feedback"]["by_kind"]["preference"][0]["text"], "this one")
        self.assertNotIn("aggregate", decision["dimensions"]["c2"])
        open_policy = {**POLICY, "required_integrity": []}
        advice = [item for item in feedback if item["target_id"] == "c2"]
        self.assertEqual(evaluate_selection(open_policy, ["c2"], advice, [], ["manager"])["advanced"], [])
        counted = {**open_policy, "consensus_authorities": ["advisory"], "consensus_threshold": 2}
        self.assertEqual(evaluate_selection(counted, ["c2"], advice, [], ["manager"])["advanced"][0]["id"], "c2")

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


