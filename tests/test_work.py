import json
import os
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path

from syberwork.core import Rejected, Work

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class WorkFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Work(Path(self.tmp.name) / "work.db")
        self.contract = json.loads((EXAMPLES / "contract.json").read_text())
        self.policy = json.loads((EXAMPLES / "policy.json").read_text())
        self.db.install_contract(self.contract)
        self.db.install_policy(self.policy)
        self.db.install_action("record_review", {"kind": "local"})
        self.db.install_action("issue_order", {"kind": "local"})
        self.case = self.db.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")

    def facts(self):
        self.db.observe(self.case, "part_number", "P-104", "inventory", "inv:7", "inventory", verified=True)
        self.db.observe(self.case, "quote", {"id": "Q-7", "price": 250}, "supplier", "quote:1", "supplier", verified=True)

    def test_complete_case_and_separate_approval(self):
        self.facts()
        review = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(review["decision"]["status"], "allowed")
        self.assertEqual(self.db.commit(self.case, review["proposal"]["id"], "operator")["status"], "succeeded")
        order = self.db.propose(self.case, "issue_order", {"part_number": "P-104", "quote": {"id": "Q-7", "price": 250}, "quote_id": "Q-7", "amount": 250, "quote_version": "quote:1"}, "operator", ["operator", "model"], "model")
        self.assertEqual(order["decision"]["status"], "needs_approval")
        with self.assertRaises(Rejected):
            self.db.approve(self.case, order["proposal"]["id"], "operator", ["manager"])
        self.assertEqual(self.db.commit(self.case, order["proposal"]["id"], "operator")["decision"]["status"], "needs_approval")
        self.db.approve(self.case, order["proposal"]["id"], "manager", ["manager"])
        self.assertEqual(self.db.commit(self.case, order["proposal"]["id"], "operator")["status"], "succeeded")
        self.assertFalse(self.db.inspect(self.case)["complete"])
        self.db.signoff(self.case, "manager", ["manager"], "manager")
        self.assertTrue(self.db.inspect(self.case)["complete"])
        self.assertTrue(self.db.verify_chain(self.case))
        with self.assertRaises(Rejected):
            self.db.commit(self.case, order["proposal"]["id"], "operator")

    def test_signoff_after_reconciled_success_completes_case(self):
        self.facts()
        review = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.db.commit(self.case, review["proposal"]["id"], "operator")
        order = self.db.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        order_id = order["proposal"]["id"]
        self.db.approve(self.case, order_id, "manager", ["manager"])
        with patch.object(self.db, "_execute", side_effect=ConnectionError("response lost")):
            self.assertEqual(self.db.commit(self.case, order_id, "scheduler")["status"], "unknown")
        self.db.signoff(self.case, "manager", ["manager"], "manager")
        self.db.reconcile(self.case, order_id, True, "ERP order ID PO-51", "manager", ["manager"])
        self.assertFalse(self.db.inspect(self.case)["complete"])
        self.db.signoff(self.case, "manager", ["manager"], "manager")
        self.assertTrue(self.db.inspect(self.case)["complete"])

    def test_denies_untrusted_and_inferred_facts(self):
        self.db.observe(self.case, "part_number", "P-104", "user", "asserted", "operator")
        result = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(result["decision"]["reason"], "untrusted_fact_source:part_number")
        self.db.observe(self.case, "part_number", "P-104", "inventory", "v1", "inventory", verified=True)
        result = self.db.propose(self.case, "record_review", {"part_number": "P-999"}, "operator", ["operator"])
        self.assertEqual(result["decision"]["reason"], "argument_provenance:part_number")

    def test_case_input_must_match_verified_fact_when_contract_binds_it(self):
        contract = json.loads(json.dumps(self.contract))
        contract["version"] = 2
        contract["input_bindings"] = {"part_number": "fact:part_number"}
        self.db.install_contract(contract)
        case = self.db.create_case("purchase-order", 2, {"part_number": "P-104", "quantity": 2}, "operator")
        self.db.observe(case, "part_number", "P-999", "inventory", "inventory:9", "inventory", verified=True)
        mismatched = self.db.propose(case, "record_review", {"part_number": "P-999"}, "operator", ["operator"])
        self.assertEqual(mismatched["decision"], {"status": "denied", "reason": "input_provenance:part_number"})
        self.db.observe(case, "part_number", "P-104", "inventory", "inventory:10", "inventory", verified=True)
        matched = self.db.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(matched["decision"]["status"], "allowed")

    def test_replay_amendment_and_original_immutability(self):
        self.facts()
        proposal = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        revised = json.loads(json.dumps(self.contract))
        revised["version"] = 2
        revised["actions"].pop("record_review")
        revised["actions"]["issue_order"].pop("requires_effect")
        revised["compiled_path"].remove("record_review")
        self.db.install_contract(revised)
        diff = self.db.replay(self.case, 2, 1)
        self.assertEqual(diff["changed_decisions"][0]["proposal_id"], proposal["proposal"]["id"])
        self.assertEqual(diff["changed_decisions"][0]["after"]["reason"], "action_not_in_contract")
        self.assertEqual(self.db.inspect(self.case)["contract"]["version"], 1)
        with self.assertRaises(Rejected):
            self.db.install_contract({**self.contract, "title": "Changed"})

    def test_contract_compiles_path_and_rejects_cycles(self):
        contract = json.loads(json.dumps(self.contract))
        contract["id"], contract["version"] = "auto-path", 1
        contract.pop("compiled_path")
        self.db.install_contract(contract)
        artifacts = self.db.artifacts("auto-path", 1)
        self.assertEqual(artifacts["path"], ["record_review", "issue_order"])
        self.assertIn("quote_version", artifacts["action_gates"]["issue_order"]["arguments"])
        broken = json.loads(json.dumps(contract))
        broken["id"] = "cyclic-path"
        broken["actions"]["record_review"]["requires_effect"] = "issue_order"
        with self.assertRaisesRegex(Rejected, "cycle"):
            self.db.install_contract(broken)

    def test_global_policy_restriction_overrides_local_contract(self):
        restrictive = json.loads(json.dumps(self.policy))
        restrictive["version"] = 2
        restrictive["actions"].pop("record_review")
        self.db.install_policy(restrictive)
        self.facts()
        decision = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])["decision"]
        self.assertEqual(decision["reason"], "action_not_in_global_policy")

    def test_manual_source_assertion_cannot_substitute_for_connector_read(self):
        self.db.observe(self.case, "part_number", "P-104", "inventory", "invented", "inventory")
        result = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(result["decision"]["reason"], "source_verification_required:part_number")

    def test_global_approval_cannot_be_overridden_by_contract(self):
        revised = json.loads(json.dumps(self.policy))
        revised["version"] = 2
        revised["actions"]["record_review"]["approval_role"] = "compliance"
        self.db.install_policy(revised)
        self.facts()
        result = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(result["decision"]["reason"], "approval_required:compliance")
        self.db.approve(self.case, result["proposal"]["id"], "compliance", ["compliance"])
        self.assertEqual(self.db.commit(self.case, result["proposal"]["id"], "operator")["status"], "succeeded")

    def test_compiled_path_uses_observed_values_without_model_inference(self):
        self.facts()
        result = self.db.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.assertEqual(result["proposal"]["args"], {"part_number": "P-104"})
        self.assertEqual(result["decision"]["status"], "allowed")
        self.db.commit(self.case, result["proposal"]["id"], "scheduler")
        order = self.db.compiled_propose(self.case, "scheduler", ["operator", "compiled"])
        self.assertEqual(order["proposal"]["args"]["amount"], 250)
        self.assertEqual(order["proposal"]["args"]["quote_version"], "quote:1")
        self.assertEqual(order["decision"]["status"], "needs_approval")

    def test_admission_rechecks_source_and_policy_at_commit(self):
        self.facts()
        proposal = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.db.observe(self.case, "part_number", "P-105", "inventory", "inv:8", "inventory", verified=True)
        result = self.db.commit(self.case, proposal["proposal"]["id"], "operator")
        self.assertEqual(result["decision"]["reason"], "argument_provenance:part_number")
        self.assertFalse(any(e["kind"] == "effect_started" for e in self.db.inspect(self.case)["events"]))

    def test_competing_commits_claim_effect_once(self):
        self.facts()
        proposal = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])["proposal"]["id"]
        def attempt(_):
            try:
                return self.db.commit(self.case, proposal, "operator")["status"]
            except Rejected as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, ["succeeded", "effect_claimed"])
        self.assertEqual(sum(e["kind"] == "effect_started" for e in self.db.inspect(self.case)["events"]), 1)

    def test_tamper_detection(self):
        with self.db.tx() as tx:
            tx.execute("UPDATE events SET body=? WHERE case_id=? AND seq=1", ('{"actor":"forged"}', self.case))
        self.assertFalse(self.db.verify_chain(self.case))

    def test_real_http_read_and_effect_with_idempotency_key(self):
        received = []

        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"part_number": "P-104"}).encode()
                self.send_response(200)
                self.send_header("ETag", '"inventory-v1"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                received.append((self.headers["Idempotency-Key"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(201)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.db.install_source("inventory", {"kind": "http", "url": f"http://127.0.0.1:{port}/items/{{key}}", "value_path": ["part_number"]})
        observation = self.db.refresh_fact(self.case, "inventory", "part_number", "P-104", "operator", ["operator"])
        self.assertTrue(observation["body"]["verified"])
        self.assertEqual(observation["body"]["version"], '"inventory-v1"')
        result = self.db.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(result["decision"]["status"], "allowed")
        self.db.install_action("external-review", {"kind": "http", "url": f"http://127.0.0.1:{port}/review"})
        amended = json.loads(json.dumps(self.contract))
        amended["version"] = 2
        amended["actions"]["external-review"] = {"required_facts": [{"key": "part_number", "source": "inventory", "verified": True}], "arguments": {"part_number": "fact:part_number"}}
        amended["compiled_path"].append("external-review")
        self.db.install_contract(amended)
        newer = self.db.create_case("purchase-order", 2, {"part_number": "P-104", "quantity": 2}, "operator")
        self.db.refresh_fact(newer, "inventory", "part_number", "P-104", "operator", ["operator"])
        new_policy = json.loads(json.dumps(self.policy))
        new_policy["version"] = 2
        new_policy["actions"]["external-review"] = {"roles": ["operator"]}
        self.db.install_policy(new_policy)
        proposal = self.db.propose(newer, "external-review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(self.db.commit(newer, proposal["proposal"]["id"], "operator")["status"], "succeeded")
        self.assertEqual(received, [(proposal["proposal"]["id"], {"part_number": "P-104"})])

    def test_planner_output_is_only_a_proposal(self):
        class Planner(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.received = request
                body = json.dumps({"action": "issue_order", "args": {"amount": 10}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Planner)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {"SYBERWORK_PLANNER_URL": f"http://127.0.0.1:{server.server_address[1]}/suggest"}):
            result = self.db.model_propose(self.case, "planner", ["model", "operator"])
        self.assertEqual(set(server.received["allowed_actions"]), {"record_review", "issue_order"})
        self.assertEqual(result["proposal"]["origin"], "model")
        self.assertEqual(result["decision"]["status"], "denied")
        self.assertFalse(any(e["kind"] == "effect_started" for e in self.db.inspect(self.case)["events"]))


if __name__ == "__main__":
    unittest.main()
