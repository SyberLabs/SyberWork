"""G4 Positive reconcile: a matching destination lookup must still reconcile.

Factory gate G4 (FACTORY red-team rubric §2, §3) guards against "refuse everything"
passing the adversarial gates. The SyberWork positive control: an approved order whose
write the destination committed, but whose acknowledgement was lost, reconciles once the
installed status lookup returns ``state=committed`` with the same idempotency key, request
digest and a non-empty external id.

- Controls run that path through ``Work.reconcile`` and the HTTP route.
- Twins change one destination field each and must not reconcile; the real lookup still
  reconciles afterwards.
- Mutants refuse everything at one named product stage, in process, and the control
  oracle must report a miss.
- Source mutants edit a copied tree so the product refuses every match, then run the
  gate's control and existing sections there; the gate must fail on each listed id.

The gate runner is ``scripts/g4_positive_reconcile.py``; its manifest names every id.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
from syberlabs import evidence
from syberlabs.canonical import digest
from syberwork import core
from syberwork.core import HTTP, Rejected, Work
from syberwork.server import make_server

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path("scripts") / "g4_positive_reconcile.py"
MANAGER_TOKEN = "g4-manager-token"


class LostAcknowledgement(unittest.TestCase):
    """An approved order the destination committed while its write acknowledgement was lost."""

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
        self.assertEqual(self.erp.order_count(), 1)

    def reconcile(self) -> dict:
        return self.work.reconcile(self.case, self.proposal["id"], "manager", ["manager"])

    def order_recorded(self) -> bool:
        return next(r for r in self.work.inspect(self.case)["acceptance"] if r["id"] == "order_recorded")["passed"]

    def control_misses(self, result) -> list[str]:
        """Every way the positive control can miss; empty only when the lookup reconciled."""
        key = self.proposal["id"]
        record = self.erp.order_for_key(key) or {}
        result = result if isinstance(result, dict) else {}
        event = result.get("event") or {}
        proof = (event.get("body") or {}).get("proof") or {}
        misses = []
        if (result.get("status"), result.get("reason")) != ("verified", "destination_record_matched"):
            misses.append(f"status {result.get('status')}/{result.get('reason')}")
        if event.get("kind") != "reconciled" or not evidence.verified_reconciliation(event):
            misses.append(f"event {event.get('kind')} is not a verified reconciliation")
        if not record:
            misses.append("destination has no record for the key")
        if not (proof.get("idempotency_key") == key == record.get("idempotency_key")):
            misses.append("idempotency key")
        if not (proof.get("request_digest") == digest(self.proposal["args"]) == record.get("request_digest")):
            misses.append("request digest")
        if not (isinstance(proof.get("external_id"), str) and proof["external_id"]
                and proof["external_id"] == record.get("external_id")):
            misses.append("external id")
        if not self.order_recorded():
            misses.append("order_recorded acceptance")
        if self.erp.order_count() != 1:
            misses.append(f"{self.erp.order_count()} orders at the destination")
        return misses

    def attempt(self) -> dict | None:
        """Reconcile, treating a refusal as a missing result."""
        try:
            return self.reconcile()
        except Rejected:
            return None


class PositiveReconcileControl(LostAcknowledgement):
    def test_pc_rec_01_matching_lookup_reconciles(self):
        self.assertFalse(self.order_recorded())
        self.assertEqual(self.control_misses(self.reconcile()), [])
        # The contract's signoff must follow the effect; the case completes after it.
        self.assertFalse(self.work.inspect(self.case)["complete"])
        self.work.signoff(self.case, "manager", ["manager"], "manager")
        self.assertTrue(self.work.inspect(self.case)["complete"])

    def test_pc_rec_02_matching_lookup_reconciles_over_http(self):
        users = {"manager": {"hash": hashlib.sha256(MANAGER_TOKEN.encode()).hexdigest(),
                             "roles": ["manager"], "sources": []}}
        server = make_server(self.work, users, port=0)
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/cases/{self.case}/reconcile",
            data=json.dumps({"proposal_id": self.proposal["id"]}).encode(), method="POST",
            headers={"Authorization": "Bearer " + MANAGER_TOKEN, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(self.control_misses(body), [])


class PositiveReconcileTwin(LostAcknowledgement):
    """One destination field differs: no reconciliation, and the real lookup still reconciles after."""

    def lookup_returns(self, change):
        original = HTTP.open

        def open_(request, *args, **kwargs):
            with original(request, *args, **kwargs) as response:
                record = json.loads(response.read())
            change(record)
            # Keep the destination's HTTP status: reconcile reads a record only from a 200.
            body = io.BytesIO(json.dumps(record).encode())
            body.status = response.status
            return body

        return patch.object(HTTP, "open", side_effect=open_)

    def assert_twin_refused_then_control_holds(self, change):
        with self.lookup_returns(change):
            result = self.reconcile()
        self.assertEqual((result["status"], result["reason"]), ("unverified", "destination_record_mismatch"))
        self.assertEqual(result["event"]["kind"], "reconciliation_checked")
        self.assertNotEqual(self.control_misses(result), [])
        self.assertFalse(self.order_recorded())
        self.assertEqual(self.control_misses(self.reconcile()), [])

    def test_twin_other_idempotency_key(self):
        self.assert_twin_refused_then_control_holds(lambda r: r.update(idempotency_key="other-key"))

    def test_twin_other_request_digest(self):
        self.assert_twin_refused_then_control_holds(lambda r: r.update(request_digest="0" * 64))

    def test_twin_external_id_missing_or_empty(self):
        for name, change in (("empty", lambda r: r.update(external_id="")),
                             ("absent", lambda r: r.pop("external_id")),
                             ("not a string", lambda r: r.update(external_id=4812))):
            with self.subTest(external_id=name):
                with self.lookup_returns(change):
                    result = self.reconcile()
                self.assertEqual(result["status"], "unverified")
                self.assertFalse(self.order_recorded())
        self.assertEqual(self.control_misses(self.reconcile()), [])

    def test_twin_state_not_committed(self):
        self.assert_twin_refused_then_control_holds(lambda r: r.update(state="proven"))


class PositiveReconcileMutant(LostAcknowledgement):
    """Refuse everything at one product stage, in process: the control oracle must miss."""

    def assert_mutant_caught(self, mutant, expected_miss):
        with mutant:
            result = self.attempt()
            misses = self.control_misses(result)
        self.assertTrue(any(expected_miss in miss for miss in misses), misses)
        # The same product without the mutant passes, so the miss came from the mutant. A
        # mutant downstream of the lookup leaves the real reconciliation in place: recheck it.
        if not (result and result["status"] == "verified"):
            result = self.reconcile()
        self.assertEqual(self.control_misses(result), [])

    def test_mutant_reconcile_refuses_every_call(self):
        self.assert_mutant_caught(
            patch.object(Work, "reconcile", side_effect=Rejected("reconciliation_denied", "refuse everything")),
            "status None/None")

    def test_mutant_target_guard_refuses_every_url(self):
        self.assert_mutant_caught(
            patch.object(core, "guard_request", side_effect=Rejected("unsafe_url", "refuse everything")),
            "status None/None")

    def test_mutant_lookup_reports_not_found(self):
        missing = urllib.error.HTTPError("status", 404, "Not Found", {}, None)
        self.assert_mutant_caught(patch.object(HTTP, "open", side_effect=missing),
                                  "status pending/not_found_not_proof")

    def test_mutant_lookup_unavailable(self):
        self.assert_mutant_caught(patch.object(HTTP, "open", side_effect=urllib.error.URLError("refused")),
                                  "status pending/status_unavailable")

    def test_mutant_request_digest_never_matches(self):
        self.assert_mutant_caught(patch.object(core, "digest", return_value="0" * 64),
                                  "status unverified/destination_record_mismatch")

    def test_mutant_acceptance_ignores_every_proof(self):
        self.assert_mutant_caught(patch.object(evidence, "verified_reconciliation", return_value=False),
                                  "order_recorded acceptance")


# Each source mutant makes the product refuse every match of one kind. Anchors must occur
# exactly once, so a refactor that moves them fails here instead of silently passing.
HTTP_CONTROLS = (
    "tests.test_g4_positive_reconcile.PositiveReconcileControl.test_pc_rec_01_matching_lookup_reconciles",
    "tests.test_g4_positive_reconcile.PositiveReconcileControl.test_pc_rec_02_matching_lookup_reconciles_over_http",
    "tests.test_reconciliation.VerifiedExternalOutcome.test_matching_status_lookup_completes_only_after_signoff",
    "tests.test_attest_reconcile_map.AttestArtifactClassification.test_only_destination_status_url_path_verifies_reconciliation",
)
ECONOMIC_CONTROLS = (
    "tests.test_economic.EconomicActions.test_lost_receipt_retains_budget_until_verified_reconciliation",
)
SOURCE_MUTANTS = {
    "http_match_refuses": ("syberwork/core.py", "                matched = True\n",
                           "                matched = False\n", HTTP_CONTROLS),
    "verified_status_refuses": ("syberwork/core.py", 'status, reason = "verified", "destination_record_matched"',
                                'status, reason = "unverified", "destination_record_mismatch"',
                                HTTP_CONTROLS + ECONOMIC_CONTROLS),
    "proof_predicate_refuses": ("syberlabs/evidence.py", '    return (event["kind"] == "reconciled"',
                                '    return False and (event["kind"] == "reconciled"',
                                HTTP_CONTROLS + ECONOMIC_CONTROLS),
    "economic_match_refuses": ("syberlabs/economic.py", '    return (isinstance(record, dict) and record.get("state") == "settled"',
                               '    return False and (isinstance(record, dict) and record.get("state") == "settled"',
                               ECONOMIC_CONTROLS),
}


class PositiveReconcileSourceMutant(unittest.TestCase):
    """Run the gate's control and existing sections against a copied, mutated tree."""

    def copy_tree(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "repo"
        shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv"))
        return root

    def run_gate(self, root: Path) -> tuple[int, dict, str]:
        out = root / "g4-report.json"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run([sys.executable, str(RUNNER), "--sections", "control,existing", "--json", str(out)],
                              cwd=root, env=env, capture_output=True, text=True, timeout=300)
        report = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {"rows": []}
        return proc.returncode, {test_id: outcome for _, test_id, outcome in report["rows"]}, proc.stdout + proc.stderr

    def assert_gate_fails(self, name):
        rel, old, new, expected = SOURCE_MUTANTS[name]
        root = self.copy_tree()
        path = root / rel
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"{name}: anchor not found exactly once in {rel}")
        path.write_text(text.replace(old, new), encoding="utf-8")
        code, outcomes, output = self.run_gate(root)
        self.assertEqual(code, 1, output)
        for test_id in expected:
            self.assertIn(outcomes.get(test_id), ("failed", "error"), f"{name}: {test_id}\n{output}")

    def test_source_unmutated_copy_passes(self):
        code, outcomes, output = self.run_gate(self.copy_tree())
        self.assertEqual(code, 0, output)
        self.assertEqual(set(outcomes.values()), {"passed"}, output)
        self.assertTrue(set(HTTP_CONTROLS + ECONOMIC_CONTROLS) <= set(outcomes), output)

    def test_source_http_match_refuses(self):
        self.assert_gate_fails("http_match_refuses")

    def test_source_verified_status_refuses(self):
        self.assert_gate_fails("verified_status_refuses")

    def test_source_proof_predicate_refuses(self):
        self.assert_gate_fails("proof_predicate_refuses")

    def test_source_economic_match_refuses(self):
        self.assert_gate_fails("economic_match_refuses")


if __name__ == "__main__":
    unittest.main()
