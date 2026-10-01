"""Phase 1 evaluation worlds: definition identity and host bindings."""

from syberlabs.builder import evaluate_selection
from syberlabs.errors import Rejected
from tests.builder_fixtures import POLICY, CellCase, descriptor, world_body


def _evaluation(n: int) -> dict:
    return {
        "candidate": f"c{n}",
        "commit": f"{n:040x}",
        "tree": f"{1000 + n:040x}",
        "evaluator": "syberlabs.checks/1",
        "checks": [{
            "name": "tests",
            "state": "passed",
            "exit_code": 0,
            "duration_ms": 5,
            "output_digest": "e" * 64,
            "output_tail": "ok",
        }],
    }


class WorldCases(CellCase):
    def _prepared(self, body=None, candidates=(1,)):
        generation = self._generation(min_approaches=1)
        approach = self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        recorded = self.store.register_world(body if body is not None else world_body(), "operator")
        self.store.seal(generation["id"], "operator", recorded["digest"])
        self.store.launch(generation["id"], "operator")
        for number in candidates:
            self._record(number, ["syberwork/server.py"])
            self.store.link_candidate(generation["id"], approach["id"], f"c{number}", "operator")
        return generation, recorded

    def test_the_host_binds_the_evaluation_it_just_recorded(self):
        generation, recorded = self._prepared()
        bound = self.store.record_bound_evaluation(generation["id"], _evaluation(1), "host", ["evaluator"])
        self.assertEqual(bound["binding"]["world_digest"], recorded["digest"])
        self.assertEqual(bound["binding"]["evaluator"], "host")
        self.assertIsNone(bound["binding"]["world_instance_digest"])
        self.assertIsNone(bound["binding"]["trace_digest"])
        self.assertEqual(bound["event"]["kind"], "candidate_evaluated")
        with self.assertRaises(Rejected) as late:
            self.store.bind_evaluation(generation["id"], bound["event"]["hash"], "host")
        self.assertEqual(late.exception.code, "binding_retrospective")
        view = self.store.generation_view(generation["id"])
        self.assertEqual(view["world_definition"]["digest"], recorded["digest"])
        self.assertEqual(view["world_definition"]["provider"]["revision"], "fixture-1")
        self.assertTrue(view["evaluation_binding_present"])
        self.assertTrue(view["world_definition_matches"])
        self.assertEqual(view["instance_equivalence"], "unverified")
        self.assertTrue(view["candidates"][0]["evaluation_binding_present"])
        self.assertIsNone(view["candidates"][0]["promotion_state"])
        again = self.store.register_world(world_body(), "operator")
        self.assertEqual(again["digest"], recorded["digest"])

    def test_a_failed_binding_insert_rolls_back_the_evaluation(self):
        from syberwork.coordination import BuilderStore

        generation, _recorded = self._prepared()
        original = BuilderStore._write_binding

        def boom(store, db, generation_id, event, actor, world_digest):
            raise RuntimeError("binding insert failed")

        BuilderStore._write_binding = boom
        try:
            with self.assertRaises(RuntimeError):
                self.store.record_bound_evaluation(generation["id"], _evaluation(1), "host", ["evaluator"])
        finally:
            BuilderStore._write_binding = original
        kinds = [event["kind"] for event in self.work.inspect(self.case)["events"]]
        self.assertNotIn("candidate_evaluated", kinds)
        self.assertTrue(self.work.verify_chain(self.case))

    def test_a_missing_binding_is_not_a_match_and_selection_does_not_read_it(self):
        generation, recorded = self._prepared(candidates=(1, 2))
        self.store.record_bound_evaluation(generation["id"], _evaluation(1), "host", ["evaluator"])
        view = self.store.generation_view(generation["id"])
        self.assertFalse(view["world_definition_matches"])
        present = {item["candidate_id"]: item["evaluation_binding_present"] for item in view["candidates"]}
        self.assertEqual(present, {"c1": True, "c2": False})
        live = world_body(reproducibility="externally_mutable", services=[{"name": "github", "mode": "real"}])
        other = self._generation(min_approaches=1)
        approach = self.store.register_approach(other["id"], descriptor("mpa_server"), "operator")
        minted = self.store.register_world(live, "operator")
        self.store.seal(other["id"], "operator", minted["digest"])
        self.assertNotEqual(minted["digest"], recorded["digest"])
        self._record(3, ["syberlabs/builder.py"])
        self.store.link_candidate(other["id"], approach["id"], "c3", "operator")
        self.store.launch(other["id"], "operator")
        self.store.record_bound_evaluation(other["id"], _evaluation(3), "host", ["evaluator"])
        live_view = self.store.generation_view(other["id"])
        self.assertEqual(live_view["instance_equivalence"], "impossible")
        self.assertTrue(live_view["world_definition_matches"])
        plain = self._generation(min_approaches=1)
        self.store.register_approach(plain["id"], descriptor("cli_files"), "operator")
        self.store.seal(plain["id"], "operator")
        bare = self.store.generation_view(plain["id"])
        self.assertNotIn("world_definition", bare)
        self.assertNotIn("world_definition_matches", bare)
        self.assertNotIn("instance_equivalence", bare)
        feedback = [{
            "id": "n1",
            "target_kind": "candidate",
            "target_id": "c1",
            "kind": "preference",
            "authorities": ["decision"],
            "actor": "ada",
            "text": "ship",
            "at": 1,
        }]
        expected = evaluate_selection(POLICY, ["c1", "c2"], feedback, [], ["manager"])
        self.store.record_feedback({
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "candidate",
            "target_id": "c1",
            "kind": "preference",
            "text": "ship",
        }, "ada", ["manager"])
        selection = self.store.select(generation["id"], "manager", ["manager"])
        self.assertEqual(selection["unresolved"], expected["unresolved"])
        self.assertEqual(selection["advanced"], expected["advanced"])
        self.assertEqual(selection["rejected"], expected["rejected"])
        self.assertFalse(selection["promotes_git"])
        self.assertNotIn("world", str(selection["dimensions"]["c1"]["reasons"]))

    def test_an_unknown_digest_cannot_be_frozen(self):
        generation = self._generation(min_approaches=1)
        self.store.register_approach(generation["id"], descriptor("spa_local"), "operator")
        with self.assertRaises(Rejected) as missing:
            self.store.seal(generation["id"], "operator", "ab" * 32)
        self.assertEqual(missing.exception.code, "unknown_world")
        self.assertEqual(self.store.generation_view(generation["id"])["generation"]["state"], "drafting")


class PostgresWorldCases(WorldCases):
    dialect = "postgres"
