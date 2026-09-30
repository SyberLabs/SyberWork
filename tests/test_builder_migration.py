"""Builder migration and snapshot compatibility."""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from syberlabs.errors import Rejected
from syberwork.backup import export_cell, restore_cell
from syberwork.coordination import BuilderStore
from syberwork.core import Work
from tests.builder_fixtures import NOTE, POLICY
from tests.test_cell import FIXTURE

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
                self.assertIn("0005", versions)
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
            source.install_policy({"version": 1, "actions": {"promote": {"roles": ["operator"]}}})
            source.install_action("promote", {"kind": "local"})
            case_id = source.create_case("repo-change", 1, {"objective": "a"}, "operator")
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
                "builder_worlds",
                "builder_generation_worlds",
                "builder_evaluation_bindings",
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

    def test_a_complete_pre_world_snapshot_restores_and_a_partial_world_group_does_not(self):
        generation_body = {
            "objective": "persist",
            "base_revision": "abc123",
            "mode": "harden",
            "isolation": "independent",
            "diversity_threshold": 0.3,
            "min_approaches": 1,
            "selection_policy_id": "review",
            "selection_policy_version": 1,
        }
        with tempfile.TemporaryDirectory() as folder:
            source = Work(Path(folder) / "source.sqlite")
            source.install_contract(NOTE)
            source.install_policy({"version": 1, "actions": {"promote": {"roles": ["operator"]}}})
            source.install_action("promote", {"kind": "local"})
            case_id = source.create_case("repo-change", 1, {"objective": "a"}, "operator")
            store = BuilderStore(source)
            store.install_policy(POLICY, "operator")
            generation = store.create_generation({**generation_body, "case_id": case_id}, "operator")
            snapshot = Path(folder) / "pre-world"
            export_cell(source, snapshot)
            source.close()
            for name in ("builder_worlds", "builder_generation_worlds", "builder_evaluation_bindings"):
                (snapshot / f"{name}.json").unlink()
            restored = Work(Path(folder) / "restored.sqlite")
            restore_cell(restored, snapshot)
            try:
                self.assertTrue(restored.verify_chain(case_id))
                view = BuilderStore(restored).generation_view(generation["id"])
                self.assertEqual(view["generation"]["state"], "drafting")
                self.assertNotIn("world_definition", view)
            finally:
                restored.close()
            partial = Path(folder) / "partial"
            fresh = Work(Path(folder) / "fresh.sqlite")
            fresh.install_contract(NOTE)
            fresh.install_policy({"version": 1, "actions": {"promote": {"roles": ["operator"]}}})
            fresh.install_action("promote", {"kind": "local"})
            fresh.create_case("repo-change", 1, {"objective": "a"}, "operator")
            export_cell(fresh, partial)
            fresh.close()
            (partial / "builder_worlds.json").unlink()
            broken = Work(Path(folder) / "broken.sqlite")
            with self.assertRaises(Rejected) as refused:
                restore_cell(broken, partial)
            self.assertEqual(refused.exception.code, "restore_refused")
            self.assertIn("builder_worlds", refused.exception.detail)
            self.assertEqual(broken.list_cases(), [])
            broken.close()
            orphan = Path(folder) / "orphan"
            again = Work(Path(folder) / "again.sqlite")
            again.install_contract(NOTE)
            again.install_policy({"version": 1, "actions": {"promote": {"roles": ["operator"]}}})
            again.install_action("promote", {"kind": "local"})
            again.create_case("repo-change", 1, {"objective": "a"}, "operator")
            export_cell(again, orphan)
            again.close()
            for name in (
                "coordination_events", "builder_works", "builder_policies", "builder_generations",
                "builder_approaches", "builder_agents", "builder_feedback", "builder_integrity",
                "builder_selections", "builder_architecture", "builder_prototypes", "builder_commands",
                "builder_candidate_links",
            ):
                (orphan / f"{name}.json").unlink()
            refused_orphan = Work(Path(folder) / "orphan.sqlite")
            with self.assertRaises(Rejected) as older:
                restore_cell(refused_orphan, orphan)
            self.assertEqual(older.exception.code, "restore_refused")
            self.assertIn("coordination_events", older.exception.detail)
            self.assertEqual(refused_orphan.list_cases(), [])
            refused_orphan.close()


    def test_world_tables_are_created_without_altering_generations(self):
        from syberwork.storage import _MIGRATIONS

        script = dict(_MIGRATIONS)["0005"]
        self.assertNotIn("ALTER", script.upper())
        for name in ("builder_worlds", "builder_generation_worlds", "builder_evaluation_bindings"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {name}", script)
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "main.sqlite"
            shutil.copy(FIXTURE, copy)
            before = sqlite3.connect(copy)
            hashes = [row[0] for row in before.execute("SELECT hash FROM events ORDER BY case_id, seq")]
            before.close()
            opened = Work(copy)
            try:
                versions = [row["version"] for row in opened._db.execute("SELECT version FROM schema_migrations ORDER BY version")]
                self.assertIn("0005", versions)
                self.assertEqual([row["hash"] for row in opened._db.execute("SELECT hash FROM events ORDER BY case_id, seq")], hashes)
                columns = {row[1] for row in opened._db.execute("PRAGMA table_info(builder_generations)")}
                self.assertNotIn("world_digest", columns)
            finally:
                opened.close()


if __name__ == "__main__":
    unittest.main()
