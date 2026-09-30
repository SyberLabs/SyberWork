"""One organization per database, with the same admission results on SQLite and PostgreSQL."""

import json
import os
import shutil
import tempfile
import threading
import unittest
import uuid
import urllib.request
from pathlib import Path

from syberlabs.errors import Rejected
from syberwork.backup import export_cell, restore_cell
from syberwork.core import Work
from syberwork.server import make_server
from syberwork.trace import TraceLog
from syberwork.worker import run_once


FIXTURE = Path(__file__).resolve().parents[1] / "conformance" / "fixtures" / "main_history.sqlite"
NOTE = {
    "id": "note",
    "version": 1,
    "inputs": {"name": "string"},
    "actions": {"note": {}},
    "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
}


def _install(work: Work) -> str:
    work.install_contract(NOTE)
    work.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
    work.install_action("note", {"kind": "local"})
    return work.create_case("note", 1, {"name": "a"}, "operator")


def _decision_trace(work: Work, case_id: str) -> list:
    rows = []
    for event in work.inspect(case_id)["events"]:
        if event["kind"] == "decision":
            rows.append([event["kind"], event["body"]["status"], event["body"]["reason"]])
        else:
            rows.append([event["kind"]])
    return rows


def _postgres_database():
    base = os.getenv("SYBERWORK_POSTGRES_URL", "postgresql://syber:syber@127.0.0.1:5432/syberwork")
    try:
        import psycopg
    except ImportError:
        raise unittest.SkipTest("psycopg is not installed")
    name = "syber_cell_" + uuid.uuid4().hex[:12]
    admin = base.rsplit("/", 1)[0] + "/postgres"
    try:
        connection = psycopg.connect(admin, autocommit=True, connect_timeout=3)
    except Exception:
        raise unittest.SkipTest("PostgreSQL is not reachable")
    connection.execute(f'CREATE DATABASE "{name}"')
    connection.close()
    return base.rsplit("/", 1)[0] + "/" + name, admin, name


def _drop_postgres(admin: str, name: str) -> None:
    import psycopg
    connection = psycopg.connect(admin, autocommit=True)
    connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
    connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
    connection.close()


class CellRuntime(unittest.TestCase):
    def test_inline_commit_still_settles_and_a_legacy_database_migrates(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite")
            case_id = _install(work)
            proposed = work.propose(case_id, "note", {}, "operator", ["operator"])
            committed = work.commit(case_id, proposed["proposal"]["id"], "operator")
            self.assertEqual(committed["status"], "succeeded")
            self.assertTrue(work.verify_chain(case_id))
            versions = [row["version"] for row in work._db.execute("SELECT version FROM schema_migrations ORDER BY version")]
            self.assertEqual(versions, ["0001", "0002", "0003", "0004"])
            work.close()
            copy = Path(folder) / "main.sqlite"
            shutil.copy(FIXTURE, copy)
            opened = Work(copy)
            meta = json.loads((FIXTURE.parent / "main_history.json").read_text())
            self.assertTrue(opened.verify_chain(meta["case_id"]))
            self.assertIs(type(opened.inspect(meta["case_id"])["events"][0]["at"]), float)
            opened.close()

    def test_worker_survives_restart_and_an_expired_lease_stays_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "work.sqlite"
            trace = TraceLog(Path(folder) / "traces.jsonl")
            work = Work(path, effects="worker", trace=trace)
            case_id = _install(work)
            proposed = work.propose(case_id, "note", {}, "operator", ["operator"])
            queued = work.commit(case_id, proposed["proposal"]["id"], "operator")
            self.assertEqual(queued["status"], "queued")
            self.assertNotIn("effect_succeeded", [event["kind"] for event in work.inspect(case_id)["events"]])
            work.close()
            resumed = Work(path, effects="worker", trace=trace)
            settled = run_once(resumed)
            self.assertEqual(settled["status"], "succeeded")
            self.assertTrue(resumed.verify_chain(case_id))
            self.assertIsNone(run_once(resumed))
            names = [row["name"] for row in trace.read()]
            self.assertIn("admission", names)
            self.assertIn("effect_claim", names)
            self.assertIn("effect_settlement", names)

            other = Work(Path(folder) / "interrupted.sqlite", effects="worker")
            other_case = _install(other)
            other_proposal = other.propose(other_case, "note", {}, "operator", ["operator"])
            held = other.commit(other_case, other_proposal["proposal"]["id"], "operator")
            with other.tx() as db:
                db.execute(
                    "UPDATE effect_obligations SET state='leased', lease_until=? WHERE proposal_id=?",
                    (0, held["proposal_id"]),
                )
            interrupted = run_once(other, now=10)
            self.assertEqual(interrupted["status"], "unknown")
            unknown = next(event for event in other.inspect(other_case)["events"] if event["kind"] == "effect_unknown")
            self.assertEqual(unknown["body"]["error"], "worker_interrupted")
            self.assertIsNone(run_once(other, now=10))
            resumed.close()
            other.close()

    def test_backup_restore_and_organization_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Work(Path(folder) / "source.sqlite")
            case_id = _install(source)
            source.propose(case_id, "note", {}, "operator", ["operator"])
            cell = source.bind_organization("acme")
            self.assertEqual(cell["organization"], "acme")
            with self.assertRaises(Rejected) as refused:
                source.bind_organization("other")
            self.assertEqual(refused.exception.code, "cell_bound")
            snapshot = Path(folder) / "snapshot"
            manifest = export_cell(source, snapshot)
            self.assertEqual(manifest["cases"], [case_id])
            restored = Work(Path(folder) / "restored.sqlite")
            restore_cell(restored, snapshot)
            self.assertEqual(_decision_trace(source, case_id), _decision_trace(restored, case_id))
            self.assertTrue(restored.verify_chain(case_id))
            self.assertEqual(restored.bind_organization("acme")["organization"], "acme")
            source.close()
            restored.close()

    def test_principal_is_a_cell_authority_record(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite")
            work.register_principal("agent-1", "agent", "reviewer", "acme", ["operator"], token="cell-token", delegation="human:ada")
            server = make_server(work, {}, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/me",
                headers={"Authorization": "Bearer cell-token"},
            )
            with urllib.request.urlopen(request) as response:
                profile = json.loads(response.read())
            self.assertEqual(profile["name"], "reviewer")
            self.assertEqual(profile["kind"], "agent")
            self.assertEqual(profile["organization"], "acme")
            self.assertEqual(profile["delegation"], "human:ada")
            self.assertEqual(profile["roles"], ["operator"])
            with self.assertRaises(Rejected) as blocked:
                make_server(work, {}, "0.0.0.0", 0)
            self.assertEqual(blocked.exception.code, "unsafe_bind")
            work.close()


class CellGuards(unittest.TestCase):
    def test_concurrent_open_applies_each_migration_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "work.sqlite"
            errors = []
            barrier = threading.Barrier(4)

            def open_db():
                barrier.wait()
                item = None
                try:
                    item = Work(path)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    if item is not None:
                        item.close()

            threads = [threading.Thread(target=open_db) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            check = Work(path)
            versions = [row["version"] for row in check._db.execute("SELECT version FROM schema_migrations ORDER BY version")]
            self.assertEqual(versions, ["0001", "0002", "0003", "0004"])
            check.close()

    def test_failed_restore_does_not_keep_the_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Work(Path(folder) / "source.sqlite")
            case_id = _install(source)
            source.propose(case_id, "note", {}, "operator", ["operator"])
            snapshot = Path(folder) / "snapshot"
            export_cell(source, snapshot)
            events = json.loads((snapshot / "events.json").read_text(encoding="utf-8"))
            events[0]["hash"] = "0" * 64
            (snapshot / "events.json").write_text(json.dumps(events), encoding="utf-8")
            restored = Work(Path(folder) / "restored.sqlite")
            with self.assertRaises(Rejected) as refused:
                restore_cell(restored, snapshot)
            self.assertEqual(refused.exception.code, "restore_refused")
            self.assertEqual(restored.list_cases(), [])
            source.close()
            restored.close()

    def test_image_build_context_excludes_local_secrets(self):
        ignored = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(encoding="utf-8")
        for name in (".git", ".syberwork", ".env", "*.sqlite", "*.sqlite3"):
            self.assertIn(name, ignored)
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER syber", dockerfile)
        self.assertIn("/home/syber/cell", dockerfile)
        self.assertFalse(any(line.strip() == "COPY . ." for line in dockerfile.splitlines()))
        compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("/home/syber/cell", compose)
        self.assertNotIn("/var/lib/syberwork", compose)
        self.assertIn('command: ["syberwork", "worker"]', compose)


class PostgresCell(unittest.TestCase):
    def test_postgres_matches_sqlite_decisions_and_worker_settlement(self):
        url, admin, name = _postgres_database()
        self.addCleanup(_drop_postgres, admin, name)
        with tempfile.TemporaryDirectory() as folder:
            sqlite_work = Work(Path(folder) / "work.sqlite")
            pg = Work(url)
            traces = []
            for work in (sqlite_work, pg):
                case_id = _install(work)
                proposed = work.propose(case_id, "note", {}, "operator", ["operator"])
                self.assertEqual(proposed["decision"]["reason"], "all_checks_passed")
                committed = work.commit(case_id, proposed["proposal"]["id"], "operator")
                self.assertEqual(committed["status"], "succeeded")
                self.assertTrue(work.verify_chain(case_id))
                traces.append(_decision_trace(work, case_id))
                work.close()
            self.assertEqual(traces[0], traces[1])
            worker = Work(url, effects="worker")
            case_id = _install(worker)
            proposed = worker.propose(case_id, "note", {}, "operator", ["operator"])
            queued = worker.commit(case_id, proposed["proposal"]["id"], "operator")
            self.assertEqual(queued["status"], "queued")
            worker.close()
            resumed = Work(url, effects="worker")
            self.assertEqual(run_once(resumed)["status"], "succeeded")
            self.assertTrue(resumed.verify_chain(case_id))
            resumed.close()


if __name__ == "__main__":
    unittest.main()
