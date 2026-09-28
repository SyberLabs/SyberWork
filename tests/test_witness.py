"""Public-key chain head, transparency log, integer timestamps, and HTTPS hosts."""

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syberlabs.admission import AdmissionContext, admit
from syberlabs.clock import as_seconds, stamp
from syberlabs.dsse import pae, sign_chain_head, verify_chain_head
from syberlabs.economic import denial
from syberlabs.ed25519 import public_from_seed, sign, verify
from syberlabs.errors import Rejected
from syberlabs.events import event_digest
from syberlabs.protocol import SIDE_PROTOCOL
from syberlabs.session import Session
from syberlabs.targets import guard_request, trusted_origin
from syberlabs.tlog import TransparencyLog, inclusion_proof, leaf_hash, merkle_root, verify_inclusion
from syberwork.core import Work


FIXTURE = Path(__file__).resolve().parents[1] / "conformance" / "fixtures"

SECRET_1 = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC_1 = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
SIG_1 = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)
SECRET_2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
PUBLIC_2 = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
SIG_2 = bytes.fromhex(
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)


def _note_session(clock=None):
    session = Session(clock=clock or (lambda: 1_700_000_000.25))
    session.install_contract({
        "id": "note",
        "version": 1,
        "inputs": {"name": "string"},
        "actions": {"note": {}},
        "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
    })
    session.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
    session.install_action("note", {"kind": "local"})
    return session


class Ed25519Vectors(unittest.TestCase):
    def test_rfc8032_empty_and_one_byte(self):
        self.assertEqual(public_from_seed(SECRET_1), PUBLIC_1)
        self.assertEqual(sign(SECRET_1, b""), SIG_1)
        self.assertTrue(verify(PUBLIC_1, b"", SIG_1))
        self.assertEqual(public_from_seed(SECRET_2), PUBLIC_2)
        self.assertEqual(sign(SECRET_2, b"r"), SIG_2)
        self.assertTrue(verify(PUBLIC_2, b"r", SIG_2))
        self.assertFalse(verify(PUBLIC_1, b"r", SIG_2))


class ChainHeadSignature(unittest.TestCase):
    def test_signature_verifies_and_stays_out_of_the_event(self):
        session = _note_session()
        case_id = session.create_case("note", 1, {"name": "a"}, "operator")
        events = session.inspect(case_id)["events"]
        seed, public = SECRET_1, PUBLIC_1
        envelope = sign_chain_head(case_id, events, seed)
        self.assertTrue(verify_chain_head(envelope, events, public))
        self.assertFalse(verify_chain_head(envelope, events, PUBLIC_2))
        encoded = json.dumps(events)
        self.assertNotIn(envelope["signatures"][0]["sig"], encoded)
        self.assertNotIn(seed.hex(), encoded)
        tampered = json.loads(json.dumps(envelope))
        payload = bytearray(tampered["payload"].encode("ascii"))
        payload[0] = ord("A") if payload[0] != ord("A") else ord("B")
        tampered["payload"] = payload.decode("ascii")
        self.assertFalse(verify_chain_head(tampered, events, public))

    def test_pae_uses_ascii_lengths(self):
        raw = pae("application/vnd.syberlabs.chain-head+json", b"{}")
        self.assertEqual(raw, b"DSSEv1 41 application/vnd.syberlabs.chain-head+json 2 {}")


class TransparencyLogTests(unittest.TestCase):
    def test_rfc6962_roots_and_inclusion(self):
        one = b"only"
        self.assertEqual(merkle_root([one]), leaf_hash(one))
        self.assertEqual(merkle_root([]), hashlib.sha256(b"").digest())
        left, right = b"left", b"right"
        pair = hashlib.sha256(b"\x01" + leaf_hash(left) + leaf_hash(right)).digest()
        self.assertEqual(merkle_root([left, right]), pair)
        for size in range(1, 17):
            entries = [f"entry-{index}".encode() for index in range(size)]
            root = merkle_root(entries)
            leaves = [leaf_hash(entry) for entry in entries]
            for index in range(size):
                proof = inclusion_proof(leaves, index)
                self.assertTrue(verify_inclusion(index, size, leaves[index], proof, root))

    def test_log_is_a_different_file_and_checks_inclusion(self):
        session = _note_session()
        case_id = session.create_case("note", 1, {"name": "a"}, "operator")
        events = session.inspect(case_id)["events"]
        envelope = sign_chain_head(case_id, events, SECRET_1)
        with tempfile.TemporaryDirectory() as folder:
            case_db = Path(folder) / "case.sqlite"
            work = Work(case_db)
            work.install_contract({
                "id": "note",
                "version": 1,
                "inputs": {"name": "string"},
                "actions": {"note": {}},
                "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
            })
            work.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
            work.install_action("note", {"kind": "local"})
            created = work.create_case("note", 1, {"name": "a"}, "operator")
            with self.assertRaises(Rejected) as blocked:
                TransparencyLog(case_db, SECRET_1)
            self.assertEqual(blocked.exception.code, "log_path")
            self.assertTrue(work.verify_chain(created))
            log_path = Path(folder) / "log.sqlite"
            log = TransparencyLog(log_path, SECRET_1)
            self.assertEqual(log.append(envelope), 0)
            self.assertTrue(log.verify_witnessed(envelope, events))
            other = sign_chain_head(case_id, [], SECRET_2)
            with self.assertRaises(Rejected) as bad:
                log.append(other)
            self.assertEqual(bad.exception.code, "invalid_witness")
            stored = Path(log_path).read_bytes()
            self.assertNotIn(SECRET_1.hex().encode(), stored)
            log.close()
            connection = sqlite3.connect(log_path)
            connection.execute("UPDATE checkpoints SET envelope='{}'")
            connection.commit()
            connection.close()
            again = TransparencyLog(log_path, SECRET_1)
            self.assertFalse(again.verify_witnessed(envelope, events))
            again.close()
            work.close()


class IntegerTimestamp(unittest.TestCase):
    def test_new_events_hash_integer_microseconds_and_old_rows_stay_float(self):
        session = _note_session()
        case_id = session.create_case("note", 1, {"name": "a"}, "operator")
        event = session.inspect(case_id)["events"][0]
        self.assertIs(type(event["at"]), int)
        self.assertEqual(event["at"], stamp(1_700_000_000.25))
        self.assertEqual(event["hash"], event_digest(event))
        self.assertTrue(session.verify_chain(case_id))
        self.assertEqual(SIDE_PROTOCOL, "sdk.syberlabs.space/v0alpha1+jcs1+us")
        meta = json.loads((FIXTURE / "main_history.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "work.sqlite"
            shutil.copy(FIXTURE / "main_history.sqlite", copy)
            opened = Work(copy)
            state = opened.inspect(meta["case_id"])
            self.assertIs(type(state["events"][0]["at"]), float)
            self.assertTrue(opened.verify_chain(meta["case_id"]))
            opened.close()
            fresh_db = Work(Path(folder) / "new.sqlite")
            fresh_db.install_contract({
                "id": "note",
                "version": 1,
                "inputs": {"name": "string"},
                "actions": {"note": {}},
                "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
            })
            fresh_db.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
            fresh_db.install_action("note", {"kind": "local"})
            created = fresh_db.create_case("note", 1, {"name": "a"}, "operator")
            fresh = fresh_db.inspect(created)["events"][0]
            self.assertIs(type(fresh["at"]), int)
            self.assertEqual(fresh["hash"], event_digest(fresh))
            self.assertTrue(fresh_db.verify_chain(created))
            row = fresh_db._db.execute("SELECT at, at_json FROM events WHERE case_id=?", (created,)).fetchone()
            self.assertIs(type(row["at"]), float)
            self.assertIs(type(json.loads(row["at_json"])), int)
            self.assertNotIn("at_json", fresh)
            fresh_db.close()

    def test_integer_and_float_times_compare_as_seconds(self):
        fresh_at = stamp(1_000_000 - 5)
        stale_at = stamp(1_000_000 - 100)
        actions = {"review": {"required_facts": [{"key": "part", "source": "inventory", "max_age_seconds": 10}]}}
        body = {"key": "part", "source": "inventory", "value": "P", "verified": True}

        def decision(at, now):
            ctx = AdmissionContext(
                contract={"actions": actions, "resolutions": {}, "input_bindings": {}},
                policy={"actions": {"review": {"roles": ["operator"]}}},
                history=[{"kind": "observed", "at": at, "body": body}],
                proposal={"id": "p1", "action": "review", "args": {}, "actor": "operator", "roles": ["operator"], "origin": "human"},
                now=now,
                installed_actions={"review"},
            )
            return admit(ctx)

        self.assertEqual(decision(fresh_at, 1_000_000.0)["reason"], "all_checks_passed")
        self.assertEqual(decision(stale_at, 1_000_000.0)["reason"], "stale_fact:part")
        self.assertEqual(decision(fresh_at, stamp(1_000_000.0))["reason"], "all_checks_passed")
        self.assertEqual(as_seconds(stamp(1.5)), 1.5)
        config = {"operation": "transfer", "asset": "USD", "rail": "test", "counterparty": "vendor"}
        policy_action = {"economic": {
            "budget_id": "ops", "budget_units": "10", "max_amount_units": "10",
            "asset": "USD", "rail": "test", "counterparties": ["vendor"],
        }}
        local = {
            "arguments": {"amount_units": "fact:invoice.amount_units"},
            "required_facts": [{"key": "invoice", "verified": True}],
        }
        history = [{"kind": "observed", "hash": "abc", "body": {"key": "invoice", "verified": True}}]
        args = {
            "operation": "transfer", "amount_units": "1", "asset": "USD", "counterparty": "vendor",
            "purpose": "pay", "evidence": ["abc"], "expires_at": 1_000_000 + 100,
        }
        self.assertIsNone(denial(config, policy_action, local, history, args, 1_000_000.0, lambda *_: 0))
        self.assertIsNone(denial(config, policy_action, local, history, args, stamp(1_000_000.0), lambda *_: 0))
        self.assertEqual(
            denial(config, policy_action, local, history, args, stamp(1_000_000 + 5000), lambda *_: 0),
            "economic_expired",
        )


class HttpsHosts(unittest.TestCase):
    def test_blocks_non_public_hosts_and_keeps_loopback_http(self):
        self.assertEqual(trusted_origin("http://127.0.0.1:9/items/{key}"), ("http", "127.0.0.1", 9))
        self.assertEqual(
            trusted_origin("https://different.example/orders/by-key/abc"),
            ("https", "different.example", 443),
        )
        for url in (
            "https://169.254.169.254/latest/meta-data/",
            "https://10.1.1.1/secret",
            "https://127.0.0.1/path",
            "https://224.0.0.1/path",
            "https://localhost/path",
            "https://metadata.google.internal/path",
            "https://printer.local/path",
            "https://2130706433/path",
            "https://0x7f000001/path",
        ):
            with self.assertRaises(Rejected) as blocked:
                trusted_origin(url)
            self.assertEqual(blocked.exception.code, "invalid_target", url)
        with patch.dict(os.environ, {"SYBERWORK_HTTPS_HOSTS": "allowed.example"}):
            self.assertEqual(trusted_origin("https://allowed.example/a"), ("https", "allowed.example", 443))
            with self.assertRaises(Rejected):
                trusted_origin("https://other.example/a")

    def test_request_guard_checks_resolved_addresses(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("syberlabs.targets.socket.getaddrinfo", return_value=private) as lookup:
            with self.assertRaises(Rejected) as blocked:
                guard_request("https://example.com/path")
            self.assertEqual(blocked.exception.code, "invalid_target")
            self.assertIn("non-public", blocked.exception.detail)
            lookup.assert_called()
        with patch("syberlabs.targets.socket.getaddrinfo", return_value=public):
            guard_request("https://example.com/path")
        with patch("syberlabs.targets.socket.getaddrinfo", side_effect=AssertionError("dns")):
            guard_request("http://127.0.0.1:9/items/1")
        with patch("syberlabs.targets.socket.getaddrinfo", side_effect=socket.gaierror()):
            guard_request("https://example.com/path")


if __name__ == "__main__":
    unittest.main()
