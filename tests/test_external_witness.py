"""The signing seed stays in a witness process. The case writer only reads the log."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from syberlabs.ed25519 import generate
from syberlabs.errors import Rejected
from syberlabs.session import Session
from syberlabs.tlog import TransparencyLog
from syberlabs.witness import Witness, WitnessClient
from syberwork.core import Work


ROOT = Path(__file__).resolve().parents[1]


def _note_contract():
    return {
        "id": "note",
        "version": 1,
        "inputs": {"name": "string"},
        "actions": {"note": {}},
        "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
    }


def _install_note(store):
    store.install_contract(_note_contract())
    store.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
    store.install_action("note", {"kind": "local"})


class ExternalWitness(unittest.TestCase):
    def test_witness_process_signs_and_the_case_writer_cannot_append(self):
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder) / "witness.sqlite"
            process = subprocess.Popen(
                [sys.executable, "-m", "syberlabs.witness", "--log", str(log_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                ready = json.loads(process.stdout.readline())
                self.assertNotIn("seed", ready)
                self.assertNotIn("--seed", process.args)
                public = bytes.fromhex(ready["public"])
                client = WitnessClient(ready["port"], public, str(log_path))
                self.assertIsNone(client._log._seed)
                with self.assertRaises(Rejected) as readonly:
                    client._log.append({"payloadType": "x"})
                self.assertEqual(readonly.exception.code, "log_readonly")
                with self.assertRaises(sqlite3.OperationalError):
                    client._log._db.execute("INSERT INTO leaves (idx, body) VALUES (99, 'rewrite')")

                session = Session()
                _install_note(session)
                case_id = session.create_case("note", 1, {"name": "a"}, "operator")
                events = session.inspect(case_id)["events"]
                envelope = session.submit_witness(case_id, client)
                self.assertNotIn(envelope["signatures"][0]["sig"], json.dumps(events))
                broken = json.loads(json.dumps(events))
                broken[0]["body"] = {"rewritten": True}
                with self.assertRaises(Rejected) as refused:
                    client.submit(broken)
                self.assertEqual(refused.exception.code, "invalid_chain")

                work_db = Path(folder) / "case.sqlite"
                work = Work(work_db)
                _install_note(work)
                work_case = work.create_case("note", 1, {"name": "b"}, "operator")
                recorded = work.submit_witness(work_case, client)
                stored = json.dumps(work.inspect(work_case)["events"])
                self.assertNotIn(recorded["signatures"][0]["sig"], stored)
                self.assertTrue(work.verify_chain(work_case))
                work.close()
                client.close()
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

    def test_a_broken_chain_is_refused_before_it_is_logged(self):
        seed, public = generate()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "witness.sqlite"
            witness = Witness(str(path), seed)
            with self.assertRaises(Rejected) as refused:
                witness.attest([{"case_id": "c", "hash": "nope"}])
            self.assertEqual(refused.exception.code, "invalid_chain")
            reader = TransparencyLog.open_readonly(path, public)
            self.assertEqual(reader._db.execute("SELECT COUNT(*) AS n FROM leaves").fetchone()["n"], 0)
            reader.close()
            witness.close()

    def test_client_refuses_a_non_loopback_witness(self):
        with self.assertRaises(Rejected) as refused:
            WitnessClient(9, b"\x00" * 32, "missing.sqlite", host="example.com")
        self.assertEqual(refused.exception.code, "invalid_target")
