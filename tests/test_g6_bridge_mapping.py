"""G6 Bridge mapping lint: Attest output and caller outcome fields never become ``reconciled``.

Factory gate G6 fails when an Attest artifact type can map to a ``reconciled``
event, or when caller-supplied ``success``/``evidence`` is accepted as proof.
The static half is ``scripts/g6_bridge_mapping_lint.py``; this file runs it on
the tree, proves it fails closed on seeded violations, and checks the same
rules at runtime. The positive control keeps "refuse everything" from passing:
a matching destination lookup must still reconcile.
"""

import contextlib
import hashlib
import io
import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from examples.attest_receipt_observe import receipt_to_observation
from syberlabs import attest_reconcile_map
from syberlabs.attest_reconcile_map import classify_attest_artifact
from syberlabs.canonical import digest
from syberlabs.evidence import verified_reconciliation
from syberlabs.session import Session
from syberwork.core import HTTP, Rejected, Work
from syberwork.server import make_server

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("g6_bridge_mapping_lint", ROOT / "scripts" / "g6_bridge_mapping_lint.py")
lint_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_module)

MANAGER_TOKEN = "g6-manager-token"


def proven_receipt(case_id: str) -> dict:
    """An Attest receipt whose Kane re-check passed: the strongest claim Attest can make."""
    return {
        "schema": "attest.receipt/v0", "case_id": case_id, "created_at": "2026-10-26T00:00:00Z",
        "doc": {"source": "demo/spec.md", "sha256_before": "before", "sha256_after": "after"},
        "claim": {"id": "claim-1", "kind": "descriptive", "lines": [1, 1], "text": "Order recorded."},
        "authority": {"policy": "attest.policy.yaml", "decision": "doc_repair_allowed", "rule": "descriptive"},
        "proposal": {"agent": "worker", "patch_sha256": "patch", "patch": "diff"},
        "approval": {"actor": "attest", "proposal_sha256": "patch"},
        "evidence": {"before": {"status": "failed", "observed": {}, "note": "before", "screenshot": None},
                     "after": {"status": "proven", "observed": {}, "note": "after", "screenshot": None}},
        "handoff": None, "disposition": "repaired", "verified": False,
    }


def caller_outcomes(case_id: str, proposal_id: str) -> list[dict]:
    """Outcome-shaped caller input: Attest artifacts, reconciled-shaped proof, and bare flags."""
    proof = {"verified": True, "external_id": "PO-FAKE", "request_digest": "0" * 64,
             "response_digest": "0" * 64, "idempotency_key": proposal_id}
    return [
        {"success": True},
        {"success": False},
        {"success": 0},
        {"success": ""},
        {"success": {}},
        {"evidence": "PO-FAKE"},
        {"evidence": proven_receipt(case_id)},
        {"evidence": {"kind": "AuthorityDecision", "decision": "approved", "actor": "attest"}},
        {"evidence": {"kind": "HandoffPackage", "status": "ready_for_human"}},
        {"evidence": proof},
        {"evidence": {"kind": "reconciled", "body": {"success": True, "proof": proof}}},
        {"success": True, "evidence": proof},
    ]


class G6StaticLint(unittest.TestCase):
    """The lint passes on this tree and fails closed on each seeded violation."""

    def test_repository_passes(self):
        self.assertEqual(lint_module.lint(ROOT), [])

    def copy_tree(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for folder in lint_module.PRODUCT_DIRS:
            if (ROOT / folder).is_dir():
                shutil.copytree(ROOT / folder, root / folder,
                                ignore=shutil.ignore_patterns("__pycache__", "static", "*.json"))
        return root

    def mutate(self, root: Path, rel: str, old: str, new: str) -> None:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"mutation anchor not unique in {rel}: {old!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")

    MUTATIONS = [
        # Attest kind -> reconciled in the classifier table.
        ("attest kind mapped to reconciled", "syberlabs/attest_reconcile_map.py",
         '"RunReceipt": "observation"', '"RunReceipt": "reconciled"', "maps to 'reconciled'"),
        ("attest kind mapped to effect success", "syberlabs/attest_reconcile_map.py",
         '"AuthorityDecision": "observation"', '"AuthorityDecision": "effect_succeeded"', "maps to"),
        ("classifier licenses reconciliation", "syberlabs/attest_reconcile_map.py",
         '"verified_reconciliation": False', '"verified_reconciliation": classification == "observation"',
         "verified_reconciliation must be the literal False"),
        ("classifier table extended at runtime", "syberlabs/attest_reconcile_map.py",
         "\n\ndef classify_attest_artifact", '\n_CLASSIFICATIONS["Receipt"] = "observation"\n\ndef classify_attest_artifact',
         "is mutated"),
        ("receipt bridge marks observation verified", "examples/attest_receipt_observe.py",
         '        "verified": False,\n    }', '        "verified": receipt["verified"] is False,\n    }',
         "Attest bridge sets 'verified'"),
        ("receipt bridge names reconciled kind", "examples/attest_receipt_observe.py",
         'OBSERVATION_KEY = "attest_receipt"', 'OBSERVATION_KEY = "reconciled"', "outcome event kind"),
        ("receipt bridge calls reconcile", "examples/attest_receipt_observe.py",
         "    return {\n        \"key\": OBSERVATION_KEY,",
         "    work.reconcile(receipt)\n    return {\n        \"key\": OBSERVATION_KEY,", "calls reconcile()"),
        # Caller success/evidence accepted.
        ("success guard weakened", "syberwork/core.py",
         "if success is not None or evidence is not None:", "if success is True or evidence is not None:",
         "must first refuse any non-None"),
        ("success/evidence flow into proof", "syberwork/core.py",
         '"success": True, "proof": proof, "actor": actor,', '"success": True, "proof": evidence or proof, "actor": actor,',
         "reads caller 'evidence' outside the refusal guard"),
        ("reconcile accepts **kwargs", "syberwork/core.py",
         "*, success=None, evidence=None) -> dict:", "*, success=None, evidence=None, **extra) -> dict:",
         "accepts *args/**kwargs"),
        ("session reconcile takes caller success", "syberlabs/session.py",
         "def reconcile(self, case_id: str, proposal_id: str, actor: str, roles: list[str]) -> dict:",
         "def reconcile(self, case_id: str, proposal_id: str, actor: str, roles: list[str], *, success=None) -> dict:",
         "must first refuse any non-None success"),
        ("server forwards caller evidence", "syberwork/server.py",
         'result = work.reconcile(case_id, data["proposal_id"], name, roles)',
         'result = work.reconcile(case_id, data["proposal_id"], name, roles, evidence=data.get("evidence"))',
         "forwards keyword evidence"),
        ("server guard removed", "syberwork/server.py",
         '                        if set(data) != {"proposal_id"}:\n'
         '                            raise Rejected("manual_reconciliation_disabled", "the destination must report the outcome")\n',
         "", "without first raising"),
        # A second writer of reconciled, direct or through a variable.
        ("observe can emit reconciled", "syberwork/core.py",
         'return self._append(db, case_id, "observed", {',
         'return self._append(db, case_id, "reconciled" if verified else "observed", {',
         "Work.observe uses 'reconciled'"),
        ("kind smuggled through a variable", "syberwork/core.py",
         "    def signoff(self, case_id: str, actor: str, roles: list[str], role: str) -> dict:\n",
         "    def signoff(self, case_id: str, actor: str, roles: list[str], role: str) -> dict:\n"
         "        kind = \"reconciled\"\n",
         "Work.signoff uses 'reconciled'"),
    ]

    def test_lint_fails_closed_on_seeded_violations(self):
        for label, rel, old, new, expected in self.MUTATIONS:
            with self.subTest(label):
                root = self.copy_tree()
                self.mutate(root, rel, old, new)
                findings = lint_module.lint(root)
                self.assertTrue(any(expected in f for f in findings), findings)

    def test_missing_hooks_fail_closed(self):
        root = self.copy_tree()
        (root / "syberlabs" / "attest_reconcile_map.py").unlink()
        self.mutate(root, "syberwork/server.py", 'elif operation == "reconcile":', 'elif operation == "settle":')
        findings = lint_module.lint(root)
        self.assertTrue(any("attest_reconcile_map.py: cannot parse" in f for f in findings), findings)
        self.assertTrue(any("route, found 0" in f for f in findings), findings)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(lint_module.main([str(root)]), 1)
        self.assertIn("G6 FAIL", printed.getvalue())

    def test_copied_tree_starts_clean(self):
        self.assertEqual(lint_module.lint(self.copy_tree()), [])

    def test_allowlist_route_guard_still_passes(self):
        # The route refuses every field but proposal_id. That stricter shape is not a violation.
        root = self.copy_tree()
        source = (root / "syberwork" / "server.py").read_text(encoding="utf-8")
        self.assertIn('if set(data) != {"proposal_id"}:', source)
        self.assertEqual(lint_module.lint(root), [])


class G6ClassifierNeverReconciles(unittest.TestCase):
    def test_every_known_kind_is_observation_or_handoff(self):
        self.assertTrue(attest_reconcile_map._CLASSIFICATIONS)
        for kind, label in attest_reconcile_map._CLASSIFICATIONS.items():
            with self.subTest(kind=kind):
                self.assertIn(label, {"observation", "handoff"})
                result = classify_attest_artifact(kind)
                self.assertEqual(set(result), {"observation", "handoff", "refuse", "verified_reconciliation"})
                self.assertIs(result["verified_reconciliation"], False)
                self.assertFalse(result["refuse"])
                self.assertEqual(sum(result[k] for k in ("observation", "handoff", "refuse")), 1)

    def test_outcome_shaped_and_hostile_kinds_refuse(self):
        for kind in ("reconciled", "Reconciled", "effect_succeeded", "reconciliation_checked", "success",
                     "evidence", "verified", "DestinationReceipt", "runreceipt", " RunReceipt", "", None,
                     True, 0, ["RunReceipt"], {"kind": "RunReceipt"}):
            with self.subTest(kind=kind):
                result = classify_attest_artifact(kind)
                self.assertEqual(result, {"observation": False, "handoff": False, "refuse": True,
                                          "verified_reconciliation": False})

    def test_session_reconcile_takes_no_caller_outcome(self):
        # The SDK session has no outcome parameter at all; binding one fails before the body runs.
        bare = SimpleNamespace(_lock=threading.RLock(), _journal=None)
        for outcome in ({"success": True}, {"evidence": {"verified": True}}):
            with self.subTest(outcome=sorted(outcome)):
                with self.assertRaises(TypeError):
                    Session.reconcile(bare, "case", "proposal", "manager", ["manager"], **outcome)


class G6Reconciliation(unittest.TestCase):
    """Runtime checks on an unknown effect whose write the destination did commit."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.erp = SimulatedERP(Path(temp.name) / "erp.sqlite3")
        self.addCleanup(self.erp.close)
        self.work = Work(Path(temp.name) / "work.sqlite3")
        self.addCleanup(self.work.close)
        configure(self.work, self.erp)
        self.case = self.work.create_case(CONTRACT["id"], 1, {"request_id": "REQ-4812"}, "analyst")
        for source, key, record in (("requisitions", "request", "REQ-4812"), ("sites", "site", "DC-WEST-4"),
                                    ("finance", "budget", "CC-742"), ("supplier", "quote", "REQ-4812")):
            self.work.refresh_fact(self.case, source, key, record, "analyst", ["operator"])
        review = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.work.commit(self.case, review["proposal"]["id"], "scheduler")
        order = self.work.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.proposal = order["proposal"]
        self.work.approve(self.case, self.proposal["id"], "manager", ["manager"])
        self.erp.drop_after_write = True
        self.assertEqual(self.work.commit(self.case, self.proposal["id"], "scheduler")["status"], "unknown")
        # The strongest Attest claim is on the case before any reconcile attempt.
        observation = receipt_to_observation(proven_receipt(self.case))
        event = self.work.observe(self.case, **observation)
        self.assertEqual(event["kind"], "observed")
        self.assertFalse(verified_reconciliation(event))

    def events(self) -> list[dict]:
        return self.work.inspect(self.case)["events"]

    def order_recorded(self) -> bool:
        return next(r for r in self.work.inspect(self.case)["acceptance"] if r["id"] == "order_recorded")["passed"]

    def assert_matching_lookup_reconciles(self, result: dict) -> None:
        self.assertEqual((result["status"], result["reason"]), ("verified", "destination_record_matched"))
        self.assertEqual(result["event"]["kind"], "reconciled")
        self.assertTrue(verified_reconciliation(result["event"]))
        proof, record = result["event"]["body"]["proof"], self.erp.order_for_key(self.proposal["id"])
        self.assertEqual(proof["idempotency_key"], self.proposal["id"])
        self.assertEqual(proof["request_digest"], digest(self.proposal["args"]))
        self.assertEqual(proof["request_digest"], record["request_digest"])
        self.assertEqual(proof["external_id"], record["external_id"])
        self.assertTrue(self.order_recorded())
        self.assertEqual(self.erp.order_count(), 1)

    def test_caller_success_or_evidence_is_refused_before_lookup(self):
        before = self.events()
        with patch.object(HTTP, "open", wraps=HTTP.open) as lookup:
            for outcome in caller_outcomes(self.case, self.proposal["id"]):
                with self.subTest(outcome=sorted(outcome)):
                    with self.assertRaises(Rejected) as refused:
                        self.work.reconcile(self.case, self.proposal["id"], "manager", ["manager"], **outcome)
                    self.assertEqual(refused.exception.code, "manual_reconciliation_disabled")
            self.assertEqual(lookup.call_count, 0)
        self.assertEqual(self.events(), before)
        self.assertFalse(any(e["kind"] == "reconciled" for e in self.events()))
        self.assertFalse(self.order_recorded())

    def test_positive_control_matching_destination_lookup_reconciles(self):
        result = self.work.reconcile(self.case, self.proposal["id"], "manager", ["manager"])
        self.assert_matching_lookup_reconciles(result)

    def test_http_route_refuses_caller_outcome_then_reconciles_on_lookup(self):
        users = {"manager": {"hash": hashlib.sha256(MANAGER_TOKEN.encode()).hexdigest(),
                             "roles": ["manager"], "sources": []}}
        server = make_server(self.work, users, port=0)
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/api/cases/{self.case}/reconcile"

        def post(body):
            request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
                "Authorization": "Bearer " + MANAGER_TOKEN, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        before = self.events()
        outcomes = caller_outcomes(self.case, self.proposal["id"]) + [{"success": None}, {"evidence": None}]
        with patch.object(HTTP, "open", wraps=HTTP.open) as lookup:
            for outcome in outcomes:
                with self.subTest(outcome=sorted(outcome)):
                    status, body = post({"proposal_id": self.proposal["id"], **outcome})
                    self.assertEqual((status, body.get("error")), (409, "manual_reconciliation_disabled"))
            self.assertEqual(lookup.call_count, 0)
        self.assertEqual(self.events(), before)

        # Positive control over HTTP: naming only the proposal triggers the destination lookup.
        status, body = post({"proposal_id": self.proposal["id"]})
        self.assertEqual(status, 200, body)
        self.assert_matching_lookup_reconciles(body)


if __name__ == "__main__":
    unittest.main()
