"""Admission certificates: certify from the journal, verify with the public key, and the mutation set."""

import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spec.validate import SchemaError, check as check_schema, validate_events
from syberlabs import Rejected, build, certificate
from syberlabs.build import GitRefEffect
from syberlabs.certificate import (DEFAULT_SEED_ENV, PAYLOAD_TYPE, certify, check_payload, first_difference,
                                   format_report, load_seed, verify_certificate, write_certificate)
from syberlabs.cli import main as cli
from syberlabs.dsse import CHAIN_HEAD_TYPE, pae, sign_dsse
from syberlabs.gitspace import Repo
from syberlabs.jcs import jcs_bytes
from tests.test_build_thread import GOOD, Crash, RepoCase, git, git_ref

# RFC 8032 vectors (also in tests/test_witness.py): a deterministic keyid for the genuine key, and a foreign key.
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
OTHER_SEED = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
OTHER_PUBLIC = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
NOW = 1_800_000_000.5


def decoded(envelope: dict) -> dict:
    return json.loads(base64.standard_b64decode(envelope["payload"]))


def copy_of(payload: dict) -> dict:
    return json.loads(json.dumps(payload))


def resign(payload: dict, seed: bytes = SEED) -> dict:
    return sign_dsse(PAYLOAD_TYPE, jcs_bytes(payload), seed)


def unsigned(envelope: dict, payload: dict) -> dict:
    """The envelope with its payload replaced and its signature left as it was."""
    return {**envelope, "payload": base64.standard_b64encode(jcs_bytes(payload)).decode("ascii")}


class CertificateCase(RepoCase):
    def seed_file(self, data: bytes = SEED, name: str = DEFAULT_SEED_ENV) -> Path:
        path = self.root.parent / "seed.bin"
        path.write_bytes(data)
        patcher = patch.dict(os.environ, {name: str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def accepted(self, contract=None, policy=None):
        kit = self.kit(contract, policy)
        thread = kit.start("Add a CSV export for report rows", paths=["src/", "tests/"])
        thread.propose(changes=GOOD, message="CSV export")
        verdict = thread.check("c1")
        self.assertTrue(verdict.acceptable, verdict.summary())
        self.assertEqual(thread.accept("c1").status, "succeeded")
        return kit, thread

    def certified(self, **kwargs):
        kit, thread = self.accepted(**kwargs)
        envelope, payload = certify(kit, thread.id, "c1", seed=SEED, now=NOW)
        return kit, thread, envelope, payload

    def verify(self, envelope, public=PUBLIC, *, repo=True, home=True):
        return verify_certificate(envelope, public, repo=Repo(self.root) if repo else None,
                                  home=self.root / ".syberlabs" if home else None)

    def journal_path(self, thread) -> Path:
        return self.root / ".syberlabs" / "journal" / "threads" / f"{thread.id}.jsonl"


class Genuine(CertificateCase):
    def test_genuine_certificate_passes_every_layer(self):
        kit, thread, envelope, payload = self.certified()
        alone = verify_certificate(envelope, PUBLIC)
        self.assertTrue(alone.passed)
        self.assertEqual((alone.signature, alone.repository, alone.journal, alone.reason), ("ok", None, None, None))
        self.assertEqual(format_report(alone)[-1], "verdict    pass (repository and journal not checked)")
        report = self.verify(envelope)
        self.assertTrue(report.passed, report.reason)
        self.assertEqual((report.signature, report.repository, report.journal), ("ok", "ok", "ok"))
        self.assertEqual(report.keyid, PUBLIC.hex())
        self.assertEqual(report.payload, payload)
        lines = format_report(report)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[-1], "verdict    pass")
        self.assertIn(f"target {thread.target_ref} at {payload['result']['commit'][:10]}", lines[1])
        self.assertIn("every certified field matches", lines[2])
        # The envelope is sign_dsse over the JCS bytes of the payload, under the certificate's own type.
        self.assertEqual(envelope["payloadType"], PAYLOAD_TYPE)
        self.assertEqual(base64.standard_b64decode(envelope["payload"]), jcs_bytes(payload))
        self.assertEqual(envelope["signatures"][0]["keyid"], PUBLIC.hex())
        self.assertEqual(pae(PAYLOAD_TYPE, b"{}"), b"DSSEv1 52 application/vnd.syberlabs.admission-certificate+json 2 {}")
        # The schema and the strict shape check agree on the genuine payload.
        check_schema(payload, "certificate.schema.json")
        self.assertIs(check_payload(payload), payload)
        self.assertEqual(certificate.HOST, build.HOST)
        self.assertEqual(certificate.ACT_CLASSES, build.ACT_CLASSES)
        # The files beside each other: the certificate, and the public key as 64 hex characters plus LF.
        out = self.root.parent / "certs" / "c1.json"
        cert, pub = write_certificate(envelope, PUBLIC, out)
        self.assertEqual((cert, pub), (out, out.with_suffix(".pub")))
        self.assertEqual(pub.read_bytes(), PUBLIC.hex().encode() + b"\n")
        written = cert.read_bytes()
        self.assertNotIn(b"\r\n", written)
        self.assertEqual(json.loads(written), envelope)
        self.assertEqual(verify_certificate(written, PUBLIC).passed, True)
        for secret in (SEED.hex().encode(), SEED):
            self.assertNotIn(secret, written)
            self.assertNotIn(secret, pub.read_bytes())

    def test_payload_names_what_the_journal_says(self):
        kit, thread, envelope, payload = self.certified()
        history = thread.history()
        self.assertEqual(list(payload), ["protocol", "payload_type", "version", "issuer", "issued_at", "thread", "objective",
                                         "act_class", "contract", "policy_version", "base", "result", "provider",
                                         "evaluation", "admission", "chain"])
        self.assertEqual((payload["protocol"], payload["payload_type"], payload["version"]),
                         ("sdk.syberlabs.space/certificate/v0alpha1", PAYLOAD_TYPE, 0))
        self.assertEqual(payload["issuer"], {"tool": "syberlabs.certificate/0", "host": "syberlabs.host",
                                             "seed_env": "SYBERLABS_CERTIFICATE_SEED", "signing": "in-process"})
        self.assertEqual(payload["issued_at"], 1_800_000_000_500_000)
        self.assertEqual((payload["thread"], payload["objective"], payload["act_class"]), (thread.id, thread.objective, "R"))
        self.assertEqual(payload["contract"], {"id": "repo-change", "version": 1})
        self.assertEqual(payload["policy_version"], 1)
        self.assertEqual(payload["base"], {"commit": self.head, "tree": git(self.root, "rev-parse", self.head + "^{tree}"),
                                           "branch": "refs/heads/main"})
        result = payload["result"]
        self.assertEqual(result["commit"], git(self.root, "rev-parse", thread.target_ref))
        self.assertEqual(result["tree"], git(self.root, "rev-parse", result["commit"] + "^{tree}"))
        self.assertEqual((result["candidate"], result["operator"], result["origin"]), ("c1", "patch", None))
        self.assertEqual(result["changed_paths"], ["src/export.py", "tests/test_export.py"])
        self.assertEqual(result["files"], 2)
        self.assertGreater(result["diff_bytes"], 0)
        self.assertEqual(payload["provider"], {"name": "patch", "revision": "1"})
        evaluated = [e for e in history if e["kind"] == "candidate_evaluated"][-1]
        self.assertEqual((payload["evaluation"]["evaluator"], payload["evaluation"]["event_hash"], payload["evaluation"]["at"]),
                         ("syberlabs.checks/1", evaluated["hash"], evaluated["at"]))
        [check] = payload["evaluation"]["checks"]
        self.assertEqual({key: check[key] for key in ("name", "required", "state", "exit_code")},
                         {"name": "tests", "required": True, "state": "passed", "exit_code": 0})
        self.assertEqual(set(check), {"name", "required", "state", "exit_code", "duration_ms", "output_digest"})
        self.assertEqual(check["output_digest"], evaluated["body"]["checks"][0]["output_digest"])
        self.assertNotIn("output_tail", json.dumps(payload))
        self.assertNotIn("signal", result)
        admission = payload["admission"]
        self.assertEqual((admission["actor"], admission["roles"], admission["origin"], admission["approvers"]),
                         ("dev@example.test", ["developer"], "human", []))
        self.assertEqual(admission["decision"], {"status": "allowed", "reason": "all_checks_passed"})
        claim = next(e for e in history if e["kind"] == "effect_started")
        effect = next(e for e in history if e["kind"] == "effect_succeeded")
        self.assertEqual(admission["proposal_id"], claim["body"]["proposal_id"])
        self.assertEqual((admission["claim_hash"], admission["effect_hash"], admission["effect_kind"], admission["target_ref"]),
                         (claim["hash"], effect["hash"], "effect_succeeded", thread.target_ref))
        self.assertEqual(payload["chain"], {"head": history[-1]["hash"], "count": len(history)})

    def test_approvers_and_needs_approval_decision_are_recorded(self):
        contract = self.contract(**{"actions.accept_change": {"approval_role": "maintainer"},
                                    "evolution.promotion.approval_role": "maintainer"})
        kit = self.kit(contract)
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        self.assertEqual(thread.accept("c1").status, "needs_approval")
        with self.assertRaises(Rejected) as caught:
            certify(kit, thread.id, "c1", seed=SEED)
        self.assertEqual(caught.exception.code, "not_accepted")
        self.assertEqual(thread.approve("c1", actor="lead@example.test", roles=["maintainer"]).status, "approved")
        self.assertEqual(thread.accept("c1").status, "succeeded")
        envelope, payload = certify(kit, thread.id, "c1", seed=SEED)
        self.assertEqual(payload["admission"]["actor"], "dev@example.test")
        self.assertEqual(payload["admission"]["approvers"], [{"actor": "lead@example.test", "role": "maintainer"}])
        self.assertEqual(payload["admission"]["decision"], {"status": "needs_approval", "reason": "approval_required:maintainer"})
        report = self.verify(envelope)
        self.assertTrue(report.passed, report.reason)

    def test_reconciled_acceptance_certifies_with_the_asserted_ref(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        original = GitRefEffect.apply

        def dies(effect, case_id, args, key):
            original(effect, case_id, args, key)
            raise Crash()

        with patch.object(GitRefEffect, "apply", dies), self.assertRaises(Crash):
            thread.accept("c1")
        kit.close()
        kit = self.kit()
        thread = kit.open(thread.id)
        with self.assertRaises(Rejected) as caught:
            certify(kit, thread.id, "c1", seed=SEED)
        self.assertEqual(caught.exception.code, "not_accepted")
        [receipt] = thread.recover()
        self.assertEqual(receipt.status, "succeeded")
        envelope, payload = certify(kit, thread.id, "c1", seed=SEED)
        admission = payload["admission"]
        reconciled = next(e for e in thread.history() if e["kind"] == "reconciled")
        self.assertEqual((admission["effect_kind"], admission["effect_hash"], admission["target_ref"]),
                         ("reconciled", reconciled["hash"], thread.target_ref))
        self.assertEqual(admission["decision"]["status"], "allowed")
        report = self.verify(envelope)
        self.assertTrue(report.passed, report.reason)
        self.assertIn(f"target {thread.target_ref} at {payload['result']['commit'][:10]}", report.notes[1])

    def test_a_contract_without_act_class_certifies_null(self):
        legacy = self.contract()
        legacy["inputs"] = {"objective": "string"}
        kit, thread, envelope, payload = self.certified(contract=legacy)
        self.assertIsNone(payload["act_class"])
        check_schema(payload, "certificate.schema.json")
        report = self.verify(envelope)
        self.assertTrue(report.passed, report.reason)
        self.assertTrue(format_report(report)[0].endswith("(repo-change.v1)"))

    def test_certificate_remains_valid_as_a_prefix_after_more_events(self):
        kit, thread, envelope, payload = self.certified()
        count = payload["chain"]["count"]
        again = thread.accept("c1")
        self.assertEqual((again.status, again.reason), ("denied", "action_already_completed"))
        self.assertEqual(len(thread.history()), count + 2)
        report = self.verify(envelope)
        self.assertTrue(report.passed, report.reason)
        self.assertIn(f"{count} of {count + 2} events certified", format_report(report)[2])

    def test_certify_appends_no_events(self):
        kit, thread = self.accepted()
        before = thread.history()
        target = git_ref(self.root, thread.target_ref)
        for _ in range(2):
            certify(kit, thread.id, "c1", seed=SEED)
        after = thread.history()
        self.assertEqual(after, before)
        self.assertEqual(validate_events(after, "certified"), [])
        self.assertTrue(kit.session.verify_chain(thread.id))
        self.assertEqual(git_ref(self.root, thread.target_ref), target)
        self.assertTrue(self.clean())

    def test_certify_refuses_an_unaccepted_candidate(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        with self.assertRaises(Rejected) as caught:
            certify(kit, thread.id, "c1", seed=SEED)
        self.assertEqual(caught.exception.code, "not_accepted")
        self.assertEqual(thread.accept("c1").status, "succeeded")
        thread.propose(changes={"src/export.py": GOOD["src/export.py"] + "# second\n"})
        with self.assertRaises(Rejected) as caught:
            certify(kit, thread.id, "c2", seed=SEED)
        self.assertEqual(caught.exception.code, "not_accepted")
        with self.assertRaises(Rejected) as caught:
            certify(kit, thread.id, "c9", seed=SEED)
        self.assertEqual(caught.exception.code, "unknown_candidate")
        with self.assertRaises(Rejected) as caught:
            certify(kit, thread.id, "c1", seed=SEED, seed_env="lower")
        self.assertEqual(caught.exception.code, "seed_env_invalid")
        envelope, payload = certify(kit, thread.id, "c1", seed=SEED, seed_env="MY_SEED")
        self.assertEqual((payload["result"]["candidate"], payload["issuer"]["seed_env"]), ("c1", "MY_SEED"))


class SeedRules(unittest.TestCase):
    def test_seed_rules(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        name = "SYBERLABS_TEST_SEED"
        details = []

        def refused(code):
            with self.assertRaises(Rejected) as caught:
                load_seed(name)
            self.assertEqual(caught.exception.code, code)
            details.append(caught.exception.detail)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(name, None)
            refused("seed_missing")
            os.environ[name] = ""
            refused("seed_missing")
            short = root / "short.bin"
            short.write_bytes(SEED[:31])
            os.environ[name] = str(short)
            refused("seed_invalid")
            os.environ[name] = str(root / "missing.bin")
            refused("seed_invalid")
            os.environ[name] = SEED.hex()[:-1]
            refused("seed_invalid")
            os.environ[name] = SEED.hex()
            self.assertEqual(load_seed(name), SEED)
            os.environ[name] = SEED.hex().upper()
            self.assertEqual(load_seed(name), SEED)
            hexfile = root / "seed.hex"
            hexfile.write_bytes(SEED.hex().encode() + b"\n")
            os.environ[name] = str(hexfile)
            self.assertEqual(load_seed(name), SEED)
            raw = root / "seed.bin"
            raw.write_bytes(SEED)
            os.environ[name] = str(raw)
            self.assertEqual(load_seed(name), SEED)
            os.environ[DEFAULT_SEED_ENV] = SEED.hex()
            self.assertEqual(load_seed(), SEED)
        for bad in ("lower", "", "1ABC", "A-B", "a" * 70):
            with self.assertRaises(Rejected) as caught:
                load_seed(bad)
            self.assertEqual(caught.exception.code, "seed_env_invalid")
            details.append(caught.exception.detail)
        for detail in details:
            self.assertNotIn(SEED.hex(), detail.lower())
            self.assertNotIn(str(root), detail)


class Mutations(CertificateCase):
    def test_changed_result_tree(self):
        kit, thread, envelope, payload = self.certified()
        forged = copy_of(payload)
        forged["result"]["tree"] = payload["base"]["tree"]
        self.assertEqual(self.verify(unsigned(envelope, forged)).reason, "signature_invalid")
        report = self.verify(resign(forged))
        self.assertEqual((report.signature, report.repository, report.journal, report.reason),
                         ("ok", "tree_mismatch:result", None, "tree_mismatch:result"))
        lines = format_report(report)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith(f"repository FAIL tree_mismatch:result  certified {payload['base']['tree'][:10]}, "
                                            f"repository {payload['result']['tree'][:10]}"), lines[1])
        self.assertEqual(lines[2], "verdict    fail: tree_mismatch:result")
        # Signature-only verification cannot see the tree: the documented limit, said in the verdict.
        alone = verify_certificate(resign(forged), PUBLIC)
        self.assertTrue(alone.passed)
        self.assertEqual(format_report(alone)[-1], "verdict    pass (repository and journal not checked)")

    def test_swapped_check_state(self):
        kit, thread, envelope, payload = self.certified()
        forged = copy_of(payload)
        forged["evaluation"]["checks"][0]["state"] = "failed"
        self.assertEqual(self.verify(unsigned(envelope, forged)).reason, "signature_invalid")
        report = self.verify(resign(forged))
        self.assertEqual((report.signature, report.repository, report.journal),
                         ("ok", "ok", "field_mismatch:evaluation.checks[0].state"))
        self.assertEqual(report.reason, "field_mismatch:evaluation.checks[0].state")
        self.assertIn('certified "failed", journal "passed"', format_report(report)[2])

    def test_forged_key(self):
        kit, thread, envelope, payload = self.certified()
        forged = resign(payload, OTHER_SEED)
        report = self.verify(forged)
        self.assertEqual((report.signature, report.repository, report.journal, report.payload, report.reason),
                         ("signature_invalid", None, None, None, "signature_invalid"))
        self.assertEqual(format_report(report), [
            f"signature  FAIL signature_invalid  no signature verifies under ed25519 {PUBLIC.hex()[:10]}",
            "verdict    fail: signature_invalid"])
        self.assertTrue(self.verify(forged, OTHER_PUBLIC).passed)  # the same bytes are a certificate under the other key
        self.assertEqual(self.verify(envelope, OTHER_PUBLIC).reason, "signature_invalid")
        # A genuine signature under a foreign keyid: the verifier narrows by keyid and never tries it.
        foreign = copy_of(envelope)
        foreign["signatures"][0]["keyid"] = OTHER_PUBLIC.hex()
        self.assertEqual(self.verify(foreign).reason, "signature_invalid")
        del foreign["signatures"][0]["keyid"]
        self.assertTrue(self.verify(foreign).passed)

    def test_truncated_chain(self):
        kit, thread, envelope, payload = self.certified()
        kit.close()
        path = self.journal_path(thread)
        original = path.read_bytes()
        lines = original.split(b"\n")
        keep = next(index for index, line in enumerate(lines) if b'"kind":"candidate_evaluated"' in line)
        path.write_bytes(b"\n".join(lines[:keep + 1]) + b"\n")
        report = self.verify(envelope)
        self.assertEqual((report.repository, report.journal, report.reason), ("ok", "chain_truncated", "chain_truncated"))
        self.assertIn(f"certified {payload['chain']['count']} events, journal has {keep}", format_report(report)[2])
        path.write_bytes(b"\n".join(lines[:3] + lines[4:]))
        report = self.verify(envelope)
        self.assertEqual((report.repository, report.journal, report.reason), ("ok", "journal_corrupt", "journal_corrupt"))
        path.write_bytes(original)
        self.assertTrue(self.verify(envelope).passed)

    def test_wrong_contract_version_and_policy_version(self):
        kit, thread, envelope, payload = self.certified()
        forged = copy_of(payload)
        forged["contract"]["version"] += 1
        self.assertEqual(self.verify(resign(forged)).reason, "field_mismatch:contract.version")
        forged = copy_of(payload)
        forged["policy_version"] += 1
        self.assertEqual(self.verify(resign(forged)).reason, "field_mismatch:policy_version")

    def test_tampered_field(self):
        kit, thread, envelope, payload = self.certified()
        forged = copy_of(payload)
        forged["act_class"] = "C"
        self.assertEqual(self.verify(resign(forged)).reason, "field_mismatch:act_class")
        forged = copy_of(payload)
        forged["admission"]["approvers"] = [{"actor": "lead@example.test", "role": "maintainer"}]
        self.assertEqual(self.verify(resign(forged)).reason, "field_mismatch:admission.approvers")
        forged = copy_of(payload)
        forged["admission"]["actor"] = "someone@else.test"
        self.assertEqual(self.verify(resign(forged)).reason, "field_mismatch:admission.actor")
        forged = copy_of(payload)
        head = payload["chain"]["head"]
        forged["chain"]["head"] = head[:-1] + ("0" if head[-1] != "0" else "1")
        report = self.verify(resign(forged))
        self.assertEqual((report.repository, report.reason), ("ok", "chain_head_mismatch"))
        forged = copy_of(payload)
        forged["result"]["changed_paths"] = payload["result"]["changed_paths"][:1]
        forged["result"]["files"] = 1
        report = self.verify(resign(forged))
        self.assertEqual((report.repository, report.journal, report.reason), ("changed_paths_mismatch", None, "changed_paths_mismatch"))

    def test_identity_fields_are_checked(self):
        kit, thread, envelope, payload = self.certified()
        self.assertEqual(self.verify({**envelope, "payloadType": CHAIN_HEAD_TYPE}).reason, "payload_type_mismatch")
        forged = copy_of(payload)
        forged["version"] = 1
        self.assertEqual(self.verify(resign(forged)).reason, "certificate_version_unsupported")
        forged = copy_of(payload)
        forged["protocol"] = "sdk.syberlabs.space/certificate/v1"
        self.assertEqual(self.verify(resign(forged)).reason, "certificate_version_unsupported")
        forged = copy_of(payload)
        forged["payload_type"] = CHAIN_HEAD_TYPE
        self.assertEqual(self.verify(resign(forged)).reason, "certificate_version_unsupported")
        forged = copy_of(payload)
        forged["note"] = "an extra field"
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:note")
        with self.assertRaises(SchemaError):
            check_schema(forged, "certificate.schema.json")
        forged = copy_of(payload)
        forged["version"] = False
        check_schema(forged, "certificate.schema.json")  # the validator's const 0 accepts False; the shape check does not
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:version")
        forged = copy_of(payload)
        forged["issued_at"] = 1.5  # JCS writes 1.0 as 1; only a fractional value reaches the wire as a float
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:issued_at")
        forged = copy_of(payload)
        forged["issuer"]["signing"] = "witnessed"
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:issuer.signing")
        forged = copy_of(payload)
        del forged["chain"]
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:chain")
        forged = copy_of(payload)
        forged["evaluation"]["checks"][0]["output_tail"] = "Ran 1 test"
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:evaluation.checks[0].output_tail")
        forged = copy_of(payload)
        forged["evaluation"]["checks"][0]["exit_code"] = 1
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:evaluation.checks[0].exit_code")
        forged = copy_of(payload)
        forged["admission"]["origin"] = "model"
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:admission.origin")
        forged = copy_of(payload)
        forged["result"]["files"] = 3
        self.assertEqual(self.verify(resign(forged)).reason, "payload_shape:result.files")
        # The same object serialized another way is signed, parses, and has the right shape, but is not canonical.
        loose = sign_dsse(PAYLOAD_TYPE, json.dumps(payload, indent=1).encode(), SEED)
        self.assertEqual(self.verify(loose).reason, "payload_not_canonical")
        self.assertEqual(self.verify(sign_dsse(PAYLOAD_TYPE, b"[]", SEED)).reason, "payload_unreadable")
        self.assertEqual(self.verify(sign_dsse(PAYLOAD_TYPE, b"\xff", SEED)).reason, "payload_unreadable")
        self.assertEqual(first_difference({"a": [1, 2]}, {"a": [1, 3]}), "a[1]")
        self.assertEqual(first_difference({"a": 1}, {"a": True}), "a")
        self.assertIsNone(first_difference(payload, copy_of(payload)))


class CommandLine(CertificateCase):
    def run_cli(self, *args, repo=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(repo or self.root), "--home", str(self.root / ".syberlabs"),
                        "--actor", "dev@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_certify_and_verify(self):
        seed_path = self.seed_file()
        self.assertEqual(self.run_cli("init")[0], 0)
        self.run_cli("start", "Add a CSV export for report rows", "--paths", "src", "tests")
        for path, text in GOOD.items():
            (self.root / path).write_text(text)
        self.assertEqual(self.run_cli("propose", "--from-worktree")[0], 0)
        code, _, err = self.run_cli("certify", "c1")
        self.assertEqual(code, 2)
        self.assertIn("not_accepted", err)
        self.assertIn("Accept a candidate first", err)
        self.assertEqual(self.run_cli("check", "c1")[0], 0)
        self.assertEqual(self.run_cli("accept", "c1")[0], 0)
        with patch.dict(os.environ, {DEFAULT_SEED_ENV: ""}):
            code, _, err = self.run_cli("certify", "c1")
        self.assertEqual(code, 2)
        self.assertIn("seed_missing", err)
        code, _, err = self.run_cli("certify", "c1", "--seed-env", "lower")
        self.assertEqual(code, 2)
        self.assertIn("seed_env_invalid", err)
        status_before = (git(self.root, "status", "--porcelain"), git(self.root, "rev-parse", "HEAD"))
        code, out, err = self.run_cli("certify", "c1")
        self.assertEqual(code, 0, err)
        self.assertIn("certified c1 of thread ", out)
        self.assertIn(" on repo-change.v1, class R; checks tests passed; chain head ", out)
        self.assertIn("wrote ", out)
        self.assertIn(f"(ed25519 key {PUBLIC.hex()[:10]}, seed from $SYBERLABS_CERTIFICATE_SEED, signed in this process)", out)
        self.assertIn("next: syberlabs verify ", out)
        for secret in (SEED.hex(), str(seed_path)):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, err)
        thread_id = (self.root / ".syberlabs" / "current").read_text().strip()
        cert = self.root / ".syberlabs" / "certificates" / f"{thread_id[:8]}-c1.json"
        pub = cert.with_suffix(".pub")
        self.assertTrue(cert.is_file() and pub.is_file())
        self.assertNotIn(SEED.hex(), cert.read_text())
        self.assertEqual(pub.read_bytes(), PUBLIC.hex().encode() + b"\n")
        self.assertEqual((git(self.root, "status", "--porcelain"), git(self.root, "rev-parse", "HEAD")), status_before,
                         "certifying must not touch the working tree")
        with patch.dict(os.environ, {"MY_SEED": SEED.hex()}):
            code, out, _ = self.run_cli("certify", "c1", "--out", str(self.root.parent / "cert.json"), "--seed-env", "MY_SEED")
        self.assertEqual(code, 0)
        self.assertIn("seed from $MY_SEED", out)
        other = json.loads((self.root.parent / "cert.json").read_text())
        self.assertEqual(decoded(other)["issuer"]["seed_env"], "MY_SEED")
        self.assertTrue((self.root.parent / "cert.pub").is_file())

        code, out, err = self.run_cli("verify", str(cert), "--public", str(pub))
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith(f"signature  ok   ed25519 {PUBLIC.hex()[:10]}  thread {thread_id[:8]} candidate c1 result "), lines[0])
        self.assertTrue(lines[0].endswith("(repo-change.v1, class R)"))
        self.assertEqual(lines[1], "repository skipped  (pass --repo PATH to re-derive commits, trees and the diff)")
        self.assertTrue(lines[2].startswith("journal    skipped  "))
        self.assertEqual(lines[3], "verdict    pass (repository and journal not checked)")
        code, out, err = self.run_cli("verify", str(cert), "--public", str(pub), "--repo", str(self.root))
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("repository ok   base "))
        self.assertIn(f"2 changed paths and diff digest match; target refs/heads/syberlabs/{thread_id[:8]} at ", lines[1])
        self.assertTrue(lines[2].startswith("journal    ok   "))
        self.assertIn(" events certified, head ", lines[2])
        self.assertEqual(lines[3], "verdict    pass")
        forged = decoded(json.loads(cert.read_text()))
        forged["result"]["tree"] = forged["base"]["tree"]
        bad = self.root.parent / "bad.json"
        bad.write_text(json.dumps(resign(forged)))
        code, out, _ = self.run_cli("verify", str(bad), "--public", str(pub), "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[-1], "verdict    fail: tree_mismatch:result")
        code, out, _ = self.run_cli("verify", str(bad), "--public", str(pub))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "verdict    pass (repository and journal not checked)")
        # verify builds no Kit: the global --repo may point at a directory that is not a repository. A ``.git``
        # file that is not a gitfile makes git refuse the directory wherever the temporary directory lives.
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / ".git").write_text("not a gitfile\n")
        code, out, err = self.run_cli("verify", str(cert), "--public", str(pub), repo=elsewhere)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], "verdict    pass (repository and journal not checked)")
        self.assertEqual([item.name for item in elsewhere.iterdir()], [".git"])
        # Its own --repo must name a repository, the key must be a key, and the file must exist: each is a Rejected.
        code, _, err = self.run_cli("verify", str(cert), "--public", str(pub), "--repo", str(elsewhere))
        self.assertEqual(code, 2)
        self.assertIn("not_a_repository", err)
        code, _, err = self.run_cli("verify", str(cert), "--public", str(cert))
        self.assertEqual(code, 2)
        self.assertIn("public_key_invalid", err)
        code, _, err = self.run_cli("verify", str(self.root.parent / "none.json"), "--public", str(pub))
        self.assertEqual(code, 2)
        self.assertIn("certificate_unreadable", err)

    def test_verify_handles_hostile_files(self):
        big = b"{" + b" " * (2 * 1024 * 1024) + b"}"
        report = verify_certificate(big, PUBLIC)
        self.assertEqual((report.passed, report.reason, report.payload), (False, "certificate_too_large", None))
        self.assertEqual(verify_certificate(b"[]", PUBLIC).reason, "certificate_unreadable")
        self.assertEqual(verify_certificate([], PUBLIC).reason, "certificate_unreadable")
        self.assertEqual(verify_certificate(b"\xff\xfe", PUBLIC).reason, "certificate_unreadable")
        self.assertEqual(verify_certificate(b'{"payloadType": "x"}', PUBLIC).reason, "payload_type_mismatch")
        self.assertEqual(verify_certificate({"payloadType": PAYLOAD_TYPE, "payload": 5, "signatures": []}, PUBLIC).reason,
                         "signature_invalid")
        pub = self.root.parent / "key.pub"
        pub.write_bytes(PUBLIC.hex().encode() + b"\n")
        path = self.root.parent / "big.json"
        path.write_bytes(big)
        code, out, _ = self.run_cli("verify", str(path), "--public", str(pub))
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["signature  FAIL certificate_too_large  more than 1048576 bytes",
                                            "verdict    fail: certificate_too_large"])
        path.write_bytes(b"[1, 2, 3]")
        code, out, _ = self.run_cli("verify", str(path), "--public", str(pub), "--repo", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[-1], "verdict    fail: certificate_unreadable")
        # A path-like commit is refused by the shape check before any git call.
        kit, thread, envelope, payload = self.certified()
        forged = copy_of(payload)
        forged["result"]["commit"] = "../../etc"
        with patch.object(Repo, "rev") as rev, patch.object(Repo, "tree") as tree:
            report = self.verify(resign(forged))
        self.assertEqual(report.reason, "payload_shape:result.commit")
        rev.assert_not_called()
        tree.assert_not_called()
        self.assertEqual(format_report(report)[-1], "verdict    fail: payload_shape:result.commit")


if __name__ == "__main__":
    unittest.main()
