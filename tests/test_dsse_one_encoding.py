"""A DSSE envelope verifies in one encoding: ``payload`` and each ``sig`` exactly as ``sign_dsse`` writes them.

``ed25519.verify`` refuses ``s ≥ L`` and a non-canonical ``R``, so an Ed25519 signature has one byte form and nobody
without the seed can write a second signature of the same message. ``dsse.verified_payload`` used to decode with
``base64.standard_b64decode``, which drops characters outside the alphabet and ignores the unused bits before the
padding, so whoever held one envelope could write many others that verified: every one of them a different file, a
different leaf, the same signature. Each test below shows the variant was read as the same bytes before
(``LAX``) and that ``ed25519.verify`` still takes those bytes, so the refusal comes from the encoding alone."""

import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from syberlabs import Rejected, ed25519
from syberlabs.cli import main as cli
from syberlabs.dsse import (CHAIN_HEAD_TYPE, _canonical_b64, pae, sign_chain_head, sign_dsse, verified_payload,
                            verify_chain_head)
from syberlabs.ed25519 import generate
from syberlabs.tlog import TransparencyLog
from tests.test_certificate import PUBLIC, CertificateCase


LAX = base64.standard_b64decode   # what verified_payload decoded with before
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def variants(encoded: str) -> dict[str, str]:
    """Strings other than ``encoded`` that ``LAX`` reads as the same bytes: the envelopes a lenient reader takes."""
    found = {
        "trailing newline": encoded + "\n",
        "line break inside": encoded[:20] + "\n" + encoded[20:],
        "CRLF at the end": encoded + "\r\n",
        "space inside": encoded[:8] + " " + encoded[8:],
        "tab first": "\t" + encoded,
        "a dash inside": encoded[:4] + "-" + encoded[4:],
        "an underscore inside": encoded[:4] + "_" + encoded[4:],
        "a junk character inside": encoded[:4] + "!" + encoded[4:],
    }
    if encoded.endswith("=="):
        found["a third padding character"] = encoded + "="
    unused = {2: 4, 1: 2, 0: 0}[len(encoded) - len(encoded.rstrip("="))]   # bits the last data character carries spare
    if unused:
        stem = encoded.rstrip("=")
        value = ALPHABET.index(stem[-1])
        for bits in range(1, 1 << unused):
            found[f"unused bits {bits:0{unused}b}"] = stem[:-1] + ALPHABET[value | bits] + encoded[len(stem):]
    for name, text in found.items():
        assert text != encoded and LAX(text) == LAX(encoded), name
    return found


def with_sig(envelope: dict, sig: object) -> dict:
    return dict(envelope, signatures=[dict(envelope["signatures"][0], sig=sig)])


class OneEncoding(unittest.TestCase):
    def setUp(self):
        self.seed, self.public = generate()
        self.signed = sign_chain_head("case-1", [], self.seed)
        self.signature = LAX(self.signed["signatures"][0]["sig"])
        self.message = pae(CHAIN_HEAD_TYPE, LAX(self.signed["payload"]))

    def test_what_sign_dsse_writes_still_verifies(self):
        """The negative control: the encoding ``sign_dsse`` writes, for every padding length, an empty payload included."""
        self.assertTrue(verify_chain_head(self.signed, [], self.public))
        self.assertEqual(self.signed["signatures"][0]["sig"].count("="), 2, "64 bytes: two padding characters")
        for size in range(7):
            payload = bytes(range(250, 250 - size, -1))
            with self.subTest(size=size):
                envelope = sign_dsse("application/x-test", payload, self.seed)
                self.assertEqual(_canonical_b64(envelope["payload"]), payload)
                self.assertEqual(verified_payload(envelope, self.public, "application/x-test"), payload)

    def test_every_other_encoding_of_the_signature_is_refused(self):
        found = variants(self.signed["signatures"][0]["sig"])
        self.assertEqual(sum(name.startswith("unused bits") for name in found), 15)
        for name, sig in found.items():
            with self.subTest(name):
                self.assertTrue(ed25519.verify(self.public, self.message, LAX(sig)), "the signature itself verifies")
                envelope = with_sig(self.signed, sig)
                self.assertIsNone(verified_payload(envelope, self.public, CHAIN_HEAD_TYPE))
                self.assertFalse(verify_chain_head(envelope, [], self.public))

    def test_every_other_encoding_of_the_payload_is_refused(self):
        found = variants(self.signed["payload"])
        self.assertTrue(any(name.startswith("unused bits") for name in found), "this payload has unused bits")
        for name, payload in found.items():
            with self.subTest(name):
                envelope = dict(self.signed, payload=payload)
                self.assertTrue(ed25519.verify(self.public, pae(CHAIN_HEAD_TYPE, LAX(payload)), self.signature))
                self.assertIsNone(verified_payload(envelope, self.public, CHAIN_HEAD_TYPE))
                self.assertFalse(verify_chain_head(envelope, [], self.public))

    def test_a_sig_or_payload_that_is_not_an_ascii_string_is_refused(self):
        sig = self.signed["signatures"][0]["sig"]
        for value in (sig.encode(), bytearray(sig.encode()), None, 0, [sig], sig[:-2] + "＝＝",
                      "٠" + sig):
            with self.subTest(repr(value)[:24]):
                self.assertIsNone(_canonical_b64(value))
                self.assertIsNone(verified_payload(with_sig(self.signed, value), self.public, CHAIN_HEAD_TYPE))
                self.assertIsNone(verified_payload(dict(self.signed, payload=value), self.public, CHAIN_HEAD_TYPE))

    def test_one_good_signature_among_variants_still_verifies(self):
        """A variant is skipped as a signature that does not verify, not a reason to refuse the envelope: the
        canonical one beside it still counts, as a second key's signature beside it always did."""
        sig = self.signed["signatures"][0]["sig"]
        keyid = self.signed["signatures"][0]["keyid"]
        both = dict(self.signed, signatures=[{"keyid": keyid, "sig": sig + "\n"}, {"keyid": keyid, "sig": sig}])
        self.assertTrue(verify_chain_head(both, [], self.public))
        self.assertFalse(verify_chain_head(dict(both, signatures=both["signatures"][:1]), [], self.public))


class WitnessLog(unittest.TestCase):
    """The log appends one leaf per signature: a re-encoded copy of a head it signed is not appended or found."""

    def test_append_and_verify_witnessed_take_only_the_signed_encoding(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        seed, _ = generate()
        log = TransparencyLog(Path(folder.name) / "log.sqlite3", seed)
        self.addCleanup(log.close)
        signed = sign_chain_head("case-1", [], seed)
        respelled = with_sig(signed, signed["signatures"][0]["sig"] + "\n")
        with self.assertRaises(Rejected) as refused:
            log.append(respelled)
        self.assertEqual(refused.exception.code, "invalid_witness")
        self.assertEqual(log.append(signed), 0)
        self.assertTrue(log.verify_witnessed(signed, []))
        self.assertFalse(log.verify_witnessed(respelled, []))
        self.assertEqual(len(log.entries()[0]), 1)


class Certificate(CertificateCase):
    """``verify_certificate`` and ``syberlabs verify`` fail a re-encoded certificate as ``signature_invalid``."""

    def test_a_certificate_whose_sig_or_payload_was_respelled_fails_the_signature_layer(self):
        _, _, envelope, _ = self.certified()
        self.assertTrue(self.verify(envelope, repo=False, home=False).passed)
        message = pae(envelope["payloadType"], LAX(envelope["payload"]))
        changed = {**{f"sig, {name}": with_sig(envelope, sig)
                      for name, sig in variants(envelope["signatures"][0]["sig"]).items()},
                   **{f"payload, {name}": dict(envelope, payload=payload)
                      for name, payload in variants(envelope["payload"]).items()}}
        for name, respelled in changed.items():
            with self.subTest(name):
                self.assertTrue(ed25519.verify(PUBLIC, pae(respelled["payloadType"], LAX(respelled["payload"])),
                                               LAX(respelled["signatures"][0]["sig"])))
                self.assertEqual(pae(respelled["payloadType"], LAX(respelled["payload"])), message)
                report = self.verify(respelled, repo=False, home=False)
                self.assertEqual((report.passed, report.reason, report.signature),
                                 (False, "signature_invalid", "signature_invalid"))

    def test_the_cli_exits_1_on_a_respelled_certificate_and_0_on_the_written_one(self):
        _, _, envelope, _ = self.certified()
        key = self.root.parent / "key.pub"
        key.write_text(PUBLIC.hex() + "\n")
        for name, document, expected in (("written", envelope, 0),
                                         ("respelled", with_sig(envelope, envelope["signatures"][0]["sig"] + "\n"), 1)):
            path = self.root.parent / f"{name}.json"
            path.write_text(json.dumps(document))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"),
                            "verify", str(path), "--public", str(key)])
            with self.subTest(name):
                self.assertEqual(code, expected, out.getvalue() + err.getvalue())
                if expected:
                    self.assertIn("signature_invalid", out.getvalue())


if __name__ == "__main__":
    unittest.main()
