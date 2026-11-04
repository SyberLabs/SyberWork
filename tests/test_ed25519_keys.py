"""A public key from outside the process is checked by ``syberlabs.ed25519.key_problem`` before anything is verified
under it: ``syberlabs verify --public``, a witness's read-only log, ``serve``'s ``SYBERWORK_WITNESS_PUBLIC``, and every
library verifier above ``ed25519.verify`` (``dsse.verified_payload``, ``verify_chain_head``, ``verify_certificate``)."""

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syberlabs import Rejected, ed25519
from syberlabs.certificate import read_public, verify_certificate
from syberlabs.cli import main as cli
from syberlabs.dsse import CHAIN_HEAD_TYPE, pae, sign_chain_head, verified_payload, verify_chain_head
from syberlabs.ed25519 import KEY_PROBLEMS, generate, key_problem, public_from_seed, usable_key
from syberlabs.tlog import TransparencyLog
from syberlabs.witness import WitnessClient
from syberwork.cli import _witness_log
from tests.test_certificate import PUBLIC, SEED, CertificateCase


IDENTITY = bytes([1]) + bytes(31)                                   # y = 1, x = 0
ORDER_2 = (ed25519._P - 1).to_bytes(32, "little")                   # y = -1
ORDER_4 = bytes(32)                                                 # y = 0
ORDER_8 = bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a")
MIXED = ed25519._encode(ed25519._decode(PUBLIC) + ed25519._decode(ORDER_4))   # a real key plus a point of order 4
NOT_A_POINT = (2).to_bytes(32, "little")                            # no x for y = 2
Y_IS_P = ed25519._P.to_bytes(32, "little")                          # y = p: a non-canonical 0
NEGATIVE_ZERO = IDENTITY[:31] + bytes([0x80])                       # the identity with the sign bit set
# R = B and s = 1: s·B = R + k·A for every challenge k when A is the identity.
ANYONE = ed25519._encode(ed25519._B) + (1).to_bytes(32, "little")

WEAK = {"identity": (IDENTITY, "small_order"), "order 2": (ORDER_2, "small_order"), "order 4": (ORDER_4, "small_order"),
        "order 8": (ORDER_8, "small_order"), "mixed order": (MIXED, "mixed_order"),
        "not a point": (NOT_A_POINT, "not_a_point"), "y = p": (Y_IS_P, "not_a_point"),
        "x = 0, sign 1": (NEGATIVE_ZERO, "not_a_point")}


class KeyProblem(unittest.TestCase):
    def test_the_small_order_points_are_what_their_names_say(self):
        identity = ed25519._Point(0, 1)
        for name, key, order in (("identity", IDENTITY, 1), ("order 2", ORDER_2, 2), ("order 4", ORDER_4, 4),
                                 ("order 8", ORDER_8, 8)):
            with self.subTest(name):
                point = ed25519._decode(key)
                self.assertEqual(ed25519._mul(point, order), identity)
                self.assertTrue(order == 1 or ed25519._mul(point, order // 2) != identity)

    def test_the_forgery_the_check_exists_for(self):
        """``verify`` takes any 32 bytes, as RFC 8032 does: under a small-order key, one signature verifies anything."""
        self.assertTrue(ed25519.verify(IDENTITY, b"any statement", ANYONE))
        self.assertTrue(ed25519.verify(IDENTITY, b"another statement", ANYONE))

    def test_each_weak_key_has_its_stable_word(self):
        for name, (key, problem) in WEAK.items():
            with self.subTest(name):
                self.assertEqual(key_problem(key), problem)
                self.assertFalse(usable_key(key))
        for value in (b"", bytes(31), bytes(33), PUBLIC.hex(), bytearray(PUBLIC), None, 0):
            with self.subTest(repr(value)[:20]):
                self.assertEqual(key_problem(value), "not_32_bytes")
                self.assertFalse(usable_key(value))
        self.assertEqual(KEY_PROBLEMS, ("not_32_bytes", "not_a_point", "small_order", "mixed_order"))

    def test_every_key_a_seed_derives_is_usable(self):
        self.assertTrue(usable_key(PUBLIC))
        for _ in range(8):
            seed, public = generate()
            self.assertIsNone(key_problem(public))
            self.assertEqual(public, public_from_seed(seed))
        # Each order of point added to a real key is refused: no seed derives any of them.
        real = ed25519._decode(PUBLIC)
        for small in (ORDER_2, ORDER_4, ORDER_8):
            with self.subTest(small.hex()[:8]):
                self.assertEqual(key_problem(ed25519._encode(real + ed25519._decode(small))), "mixed_order")


class VerifyPublic(CertificateCase):
    """``syberlabs verify --public`` reads the key with ``read_public``, which refuses a weak one before any file."""

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"), *args])
        return code, out.getvalue(), err.getvalue()

    def write_key(self, key: bytes, name: str = "key.pub") -> Path:
        path = self.root.parent / name
        path.write_text(key.hex() + "\n")
        return path

    def test_read_public_refuses_each_weak_key_with_its_word(self):
        self.assertEqual(read_public(self.write_key(PUBLIC)), PUBLIC)
        for name, (key, problem) in WEAK.items():
            with self.subTest(name), self.assertRaises(Rejected) as refused:
                read_public(self.write_key(key))
            self.assertEqual(refused.exception.code, "public_key_invalid")
            self.assertTrue(refused.exception.detail.endswith(": " + problem), refused.exception.detail)

    def test_the_cli_refuses_an_identity_key_a_forged_certificate_would_pass_under(self):
        _, _, envelope, _ = self.certified()
        forged = dict(envelope, signatures=[{"keyid": IDENTITY.hex(), "sig": base64.standard_b64encode(ANYONE).decode()}])
        # The signature verifies under the identity; verify_certificate refuses the key before it reads the envelope.
        self.assertTrue(ed25519.verify(IDENTITY, pae(forged["payloadType"], base64.standard_b64decode(forged["payload"])),
                                       ANYONE))
        self.assertEqual(verify_certificate(forged, IDENTITY).reason, "public_key_invalid")
        path = self.root.parent / "forged.json"
        path.write_text(json.dumps(forged))
        code, out, err = self.run_cli("verify", str(path), "--public", str(self.write_key(IDENTITY)))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("public_key_invalid", err)
        self.assertIn("small_order", err)
        code, out, err = self.run_cli("verify", str(path), "--public", str(self.write_key(PUBLIC)))
        self.assertEqual(code, 1, out + err)
        self.assertIn("signature_invalid", out)


class LibraryVerifiers(CertificateCase):
    """``ed25519.verify`` is RFC 8032 and takes any 32 bytes; every verifier above it refuses a weak key itself, so a
    library caller that hands one a key from outside without ``key_problem`` still gets no forgery through."""

    def forge_under(self, envelope: dict, key: bytes) -> dict:
        """``envelope`` re-signed under the small-order ``key`` without a seed: ``R = s·B + j·A`` verifies with ``s``
        when ``(k + j)·A`` is the identity, ``k`` the challenge. Under the identity the first try does; under a point
        of order ``n``, about one try in ``n``."""
        message = pae(envelope["payloadType"], base64.standard_b64decode(envelope["payload"]))
        point, identity = ed25519._decode(key), ed25519._Point(0, 1)
        multiple = identity
        for response in range(1, 65):
            multiple = multiple + ed25519._B
            for j in range(8):
                commitment = ed25519._encode(multiple + ed25519._mul(point, j))
                challenge = int.from_bytes(hashlib.sha512(commitment + key + message).digest(), "little") % ed25519._L
                if ed25519._mul(point, (challenge + j) % 8) == identity:
                    signature = commitment + response.to_bytes(32, "little")
                    self.assertTrue(ed25519.verify(key, message, signature), "the forgery verifies")
                    return dict(envelope, signatures=[{"keyid": key.hex(), "sig": base64.standard_b64encode(signature).decode()}])
        self.fail(f"no R = s·B + j·A verifies under {key.hex()[:8]}")

    def test_no_small_order_key_passes_verified_payload_or_verify_certificate(self):
        _, _, envelope, _ = self.certified()
        self.assertIsNotNone(verified_payload(envelope, PUBLIC, envelope["payloadType"]))
        self.assertTrue(verify_certificate(envelope, PUBLIC).passed)
        for name in ("identity", "order 2", "order 4", "order 8"):
            key = WEAK[name][0]
            with self.subTest(name):
                forged = self.forge_under(envelope, key)
                self.assertIsNone(verified_payload(forged, key, forged["payloadType"]))
                report = verify_certificate(forged, key)
                self.assertEqual((report.passed, report.reason, report.signature, report.repository, report.journal,
                                  report.payload, report.keyid), (False, "public_key_invalid", "public_key_invalid",
                                                                  None, None, None, key.hex()))
                self.assertTrue(report.notes[0].endswith(": small_order"), report.notes[0])

    def test_verify_certificate_names_each_weak_key_before_reading_the_envelope(self):
        for name, (key, problem) in WEAK.items():
            for envelope in (b"not json", {"payloadType": "x"}):
                with self.subTest(name, envelope=envelope):
                    report = verify_certificate(envelope, key)
                    self.assertEqual(report.reason, "public_key_invalid")
                    self.assertTrue(report.notes[0].endswith(": " + problem), report.notes[0])
        for value in (bytes(31), PUBLIC.hex(), bytearray(PUBLIC), None):
            with self.subTest(repr(value)[:20]):
                report = verify_certificate(b"{}", value)
                self.assertEqual((report.reason, report.keyid), ("public_key_invalid", None))
                self.assertTrue(report.notes[0].endswith(": not_32_bytes"), report.notes[0])
                self.assertIsNone(verified_payload({}, value, "x"))

    def test_a_mixed_order_key_is_refused_though_a_signature_verifies_under_it(self):
        """``MIXED`` is ``PUBLIC`` plus a point ``T`` of order 4. ``PUBLIC``'s seed signs under it with ``R = r·B + j·T``
        when ``j + k`` is a multiple of 4, ``k`` the challenge; no seed derives ``MIXED``, so the verifiers refuse it as
        the CLI and the witness do."""
        _, _, envelope, _ = self.certified()
        message = pae(envelope["payloadType"], base64.standard_b64decode(envelope["payload"]))
        scalar, small = ed25519._clamp(hashlib.sha512(SEED).digest()[:32]), ed25519._decode(ORDER_4)
        for nonce in range(1, 65):
            j = next((j for j in range(4) if self._challenge(ed25519._mul(ed25519._B, nonce) + ed25519._mul(small, j),
                                                            message) % 4 == (-j) % 4), None)
            if j is not None:
                break
        commitment = ed25519._encode(ed25519._mul(ed25519._B, nonce) + ed25519._mul(small, j))
        response = (nonce + self._challenge(ed25519._decode(commitment), message) * scalar) % ed25519._L
        signature = commitment + response.to_bytes(32, "little")
        self.assertTrue(ed25519.verify(MIXED, message, signature), "a signature verifies under the mixed-order key")
        resigned = dict(envelope, signatures=[{"keyid": MIXED.hex(), "sig": base64.standard_b64encode(signature).decode()}])
        self.assertIsNone(verified_payload(resigned, MIXED, envelope["payloadType"]))
        report = verify_certificate(resigned, MIXED)
        self.assertEqual(report.reason, "public_key_invalid")
        self.assertTrue(report.notes[0].endswith(": mixed_order"), report.notes[0])

    @staticmethod
    def _challenge(commitment, message: bytes) -> int:
        encoded = ed25519._encode(commitment)
        return int.from_bytes(hashlib.sha512(encoded + MIXED + message).digest(), "little") % ed25519._L


class WitnessPublic(unittest.TestCase):
    """A witness's key enters through ``TransparencyLog.open_readonly``: ``WitnessClient`` and ``serve`` both use it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.seed, self.public = generate()
        self.log_path = Path(folder.name) / "log.sqlite3"
        TransparencyLog(self.log_path, self.seed).close()

    def test_a_chain_head_under_the_identity_is_refused_though_its_signature_verifies(self):
        """What a reader trusting a small-order witness key would have accepted: ``verify`` takes it, ``verify_chain_head``
        does not."""
        signed = sign_chain_head("c", [], self.seed)
        forged = dict(signed, signatures=[{"keyid": IDENTITY.hex(), "sig": base64.standard_b64encode(ANYONE).decode()}])
        self.assertTrue(ed25519.verify(IDENTITY, pae(CHAIN_HEAD_TYPE, base64.standard_b64decode(forged["payload"])), ANYONE))
        self.assertFalse(verify_chain_head(forged, [], IDENTITY))
        self.assertFalse(verify_chain_head(forged, [], self.public))
        self.assertTrue(verify_chain_head(signed, [], self.public))

    def test_open_readonly_and_the_client_refuse_each_weak_key(self):
        log = TransparencyLog.open_readonly(self.log_path, self.public)
        log.close()
        for name, (key, problem) in WEAK.items():
            for opening in (lambda: TransparencyLog.open_readonly(self.log_path, key),
                            lambda: WitnessClient(1, key, str(self.log_path))):
                with self.subTest(name), self.assertRaises(Rejected) as refused:
                    opening()
                self.assertEqual(refused.exception.code, "invalid_witness")
                self.assertTrue(refused.exception.detail.endswith(": " + problem), refused.exception.detail)

    def test_serve_stops_on_a_weak_witness_key(self):
        parser = argparse.ArgumentParser()
        environment = {k: v for k, v in os.environ.items() if not k.startswith("SYBERWORK_WITNESS_")}

        def configured(public):
            names = {"SYBERWORK_WITNESS_LOG": str(self.log_path), "SYBERWORK_WITNESS_PUBLIC": public}
            with patch.dict(os.environ, {**environment, **names}, clear=True):
                return _witness_log(parser)

        log = configured(self.public.hex())
        self.addCleanup(log.close)
        self.assertEqual(log.public, self.public)
        for name, (key, problem) in WEAK.items():
            err = io.StringIO()
            with self.subTest(name), self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
                configured(key.hex())
            self.assertIn("witness log: invalid_witness: ", err.getvalue())
            self.assertIn(problem, err.getvalue())


if __name__ == "__main__":
    unittest.main()
