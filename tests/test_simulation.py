"""Loopback campaigns, plus the guards that do not hold yet.

A campaign failure is a product failure. An expected failure is a guard the
source still lacks: when that guard lands, the probe passes and this module
fails until the marker is removed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.targets import guard_request
from syberwork.backup import export_cell
from syberwork.core import Work

from tests.simulation.campaigns import run_cell, run_kernel, run_libraries
from tests.simulation.catalog import gaps
from tests.simulation.cell import Cell


class Catalog(unittest.TestCase):
    def test_every_production_module_has_one_campaign(self):
        missing, extra = gaps()
        self.assertEqual(missing, set(), f"no campaign drives {sorted(missing)}")
        self.assertEqual(extra, set(), f"campaigns name modules that do not exist: {sorted(extra)}")


class Campaigns(unittest.TestCase):
    def test_cell(self):
        run_cell()

    def test_kernel(self):
        run_kernel()

    def test_libraries(self):
        run_libraries()


class KnownDefects(unittest.TestCase):
    @unittest.expectedFailure
    def test_encoded_loopback_host_is_rejected(self):
        with self.assertRaises(Rejected):
            guard_request("https://127%2e0%2e0%2e1/x")

    @unittest.expectedFailure
    def test_source_host_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            try:
                with self.assertRaises(Rejected):
                    work.install_source("pivot", {"kind": "http", "url": "https://{key}/resource"})
            finally:
                work.close()

    @unittest.expectedFailure
    def test_action_query_string_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            try:
                with self.assertRaises(Rejected):
                    work.install_action("charge", {
                        "kind": "http", "method": "POST",
                        "url": "https://payments.example/charge?k=v",
                    })
            finally:
                work.close()

    @unittest.expectedFailure
    def test_unverified_fact_does_not_hide_a_verified_one(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            try:
                work.install_contract({
                    "id": "shadow", "version": 1,
                    "inputs": {"part_number": "string"},
                    "actions": {"record_review": {
                        "required_facts": [{"key": "part_number", "source": "inventory", "verified": True}],
                        "arguments": {"part_number": "fact:part_number"},
                    }},
                    "acceptance": [{"id": "noted", "kind": "effect", "action": "record_review"}],
                })
                work.install_policy({"version": 1, "actions": {"record_review": {"roles": ["operator"]}}})
                work.install_action("record_review", {"kind": "local"})
                case_id = work.create_case("shadow", 1, {"part_number": "P-104"}, "operator")
                work.observe(case_id, "part_number", "P-104", "inventory", "v1", "inventory", verified=True)
                work.observe(case_id, "part_number", "P-104", "inventory", "v2", "inventory", verified=False)
                decision = work.propose(
                    case_id, "record_review", {"part_number": "P-104"}, "operator", ["operator"],
                )["decision"]
                self.assertEqual(decision["status"], "allowed")
            finally:
                work.close()

    @unittest.expectedFailure
    def test_observer_cannot_read_a_case(self):
        with tempfile.TemporaryDirectory() as folder:
            cell = Cell(Path(folder))
            try:
                cell.work.install_contract({
                    "id": "note", "version": 1, "inputs": {"name": "string"},
                    "actions": {"note": {}},
                    "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
                })
                cell.work.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
                cell.work.install_action("note", {"kind": "local"})
                created = cell.call("POST", "/api/cases", cell.tokens["operator"], {
                    "contract_id": "note", "version": 1, "inputs": {"name": "private"},
                })
                self.assertEqual(created[0], 200, created)
                status, _body = cell.call("GET", f"/api/cases/{created[1]['id']}", cell.tokens["supplier"])
                self.assertEqual(status, 403)
            finally:
                cell.stop()

    @unittest.expectedFailure
    def test_backup_does_not_follow_a_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work = Work(root / "work.sqlite3")
            victim = root / "victim.txt"
            victim.write_text("original", encoding="utf-8")
            snapshot = root / "snapshot"
            snapshot.mkdir()
            link = snapshot / "manifest.json"
            try:
                link.symlink_to(victim)
            except OSError:
                work.close()
                self.skipTest("this platform cannot create the snapshot symlink")
            try:
                try:
                    export_cell(work, snapshot)
                except OSError:
                    pass
                self.assertEqual(victim.read_text(encoding="utf-8"), "original")
            finally:
                work.close()
