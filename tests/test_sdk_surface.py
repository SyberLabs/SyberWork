"""Follow-up SDK surface: URL checks, contract shape, explain, economics, session, mapping."""

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from syberlabs.canonical import digest
from syberlabs.errors import Rejected
from syberlabs.mappings import translate_stabilize
from syberwork.core import Work


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return


def economic_contract():
    return {
        "id": "pay-invoice",
        "version": 1,
        "title": "Pay one invoice",
        "inputs": {"invoice_id": "string"},
        "actions": {
            "pay": {
                "required_facts": [{"key": "invoice", "source": "billing", "verified": True}],
                "arguments": {"amount_units": "fact:invoice.amount_units"},
            }
        },
        "acceptance": [{"id": "paid", "kind": "effect", "action": "pay"}],
    }


def economic_policy(cap="10"):
    return {
        "version": 1,
        "actions": {
            "pay": {
                "roles": ["operator"],
                "economic": {
                    "budget_id": "ops",
                    "budget_units": cap,
                    "max_amount_units": cap,
                    "asset": "USD",
                    "rail": "test",
                    "counterparties": ["vendor"],
                },
            }
        },
    }


class Surface(unittest.TestCase):
    def test_source_and_planner_use_trusted_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            work.install_source("inventory", {"kind": "http", "url": "http://127.0.0.1:9/items/{key}"})
            with self.assertRaises(Rejected) as bad:
                work.install_source("evil", {"kind": "http", "url": "http://127.0.0.1:1.evil.example/items/{key}"})
            self.assertEqual(bad.exception.code, "invalid_source")
            work.install_contract(json.loads((EXAMPLES / "contract.json").read_text()))
            work.install_policy(json.loads((EXAMPLES / "policy.json").read_text()))
            work.install_action("record_review", {"kind": "local"})
            work.install_action("issue_order", {"kind": "local"})
            case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
            with patch.dict(os.environ, {"SYBERWORK_PLANNER_URL": "http://127.0.0.1:1.evil.example/suggest"}):
                with self.assertRaises(Rejected) as planner:
                    work.model_propose(case, "planner", ["model"])
            self.assertEqual(planner.exception.code, "planner_unconfigured")
            work.close()

    def test_publish_rejects_unindexed_clauses_and_keeps_examples(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            work.install_contract(json.loads((EXAMPLES / "contract.json").read_text()))
            missing_action = json.loads((EXAMPLES / "contract.json").read_text())
            missing_action["version"] = 2
            missing_action["acceptance"] = [{"id": "order_sent", "kind": "effect"}]
            with self.assertRaises(Rejected) as error:
                work.install_contract(missing_action)
            self.assertEqual(error.exception.code, "invalid_contract")
            missing_fact = json.loads((EXAMPLES / "contract.json").read_text())
            missing_fact["version"] = 3
            missing_fact["actions"]["record_review"]["required_facts"] = [{"source": "inventory"}]
            with self.assertRaises(Rejected) as fact_error:
                work.install_contract(missing_fact)
            self.assertEqual(fact_error.exception.code, "invalid_contract")
            self.assertIn("required fact", fact_error.exception.detail)
            work.close()

    def test_explain_does_not_enter_the_hash_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            work.install_contract(json.loads((EXAMPLES / "contract.json").read_text()))
            work.install_policy(json.loads((EXAMPLES / "policy.json").read_text()))
            work.install_action("record_review", {"kind": "local"})
            work.install_action("issue_order", {"kind": "local"})
            case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
            proposed = work.propose(case, "record_review", {}, "operator", ["operator"])
            before = [event["hash"] for event in work.inspect(case)["events"]]
            found = work.explain_admission(case, proposed["proposal"]["id"])
            after = [event["hash"] for event in work.inspect(case)["events"]]
            self.assertEqual(before, after)
            self.assertEqual(found["reason"], proposed["decision"]["reason"])
            self.assertIn("rule", found)
            self.assertEqual(found["provenance"]["module"], "syberlabs/admission.py")
            self.assertTrue(work.verify_chain(case))
            for event in work.inspect(case)["events"]:
                if event["kind"] == "decision":
                    self.assertNotIn("rule", event["body"])
                    self.assertNotIn("provenance", event["body"])
            work.close()


class Economics(unittest.TestCase):
    def _server(self):
        class Pay(QuietHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                args = json.loads(raw)
                record = {
                    "state": "settled",
                    "idempotency_key": self.headers["Idempotency-Key"],
                    "request_digest": digest(args),
                    "amount_units": args["amount_units"],
                    "asset": args["asset"],
                    "counterparty": args["counterparty"],
                    "external_id": "pay-1",
                }
                body = json.dumps(record).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Pay)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_settlement_reserves_budget_and_local_effects_do_not(self):
        port = self._server()
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "work.sqlite3")
            work.install_contract({
                "id": "note", "version": 1, "inputs": {"name": "string"},
                "actions": {"note": {}},
                "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
            })
            work.install_policy({
                "version": 1,
                "actions": {"note": {"roles": ["operator"]}, **economic_policy("10")["actions"]},
            })
            work.install_action("note", {"kind": "local"})
            plain = work.create_case("note", 1, {"name": "a"}, "operator")
            noted = work.propose(plain, "note", {}, "operator", ["operator"])
            committed = work.commit(plain, noted["proposal"]["id"], "operator")
            self.assertEqual(committed["status"], "succeeded")
            started = next(event for event in work.inspect(plain)["events"] if event["kind"] == "effect_started")
            self.assertEqual(set(started["body"]), {"proposal_id", "action", "actor", "policy_version", "idempotency_key"})

            work.install_contract(economic_contract())
            work.install_action("pay", {
                "kind": "economic_http",
                "method": "POST",
                "url": f"http://127.0.0.1:{port}/pay",
                "status_url": f"http://127.0.0.1:{port}/status/{{key}}",
                "asset": "USD",
                "rail": "test",
                "counterparty": "vendor",
                "operation": "transfer",
            })
            with self.assertRaises(Rejected) as raised:
                work.install_policy({
                    "version": 2,
                    "actions": {"note": {"roles": ["operator"]}, **economic_policy("11")["actions"]},
                })
            self.assertEqual(raised.exception.code, "invalid_policy")

            first = self._pay(work, "inv-1", "10")
            self.assertEqual(first["status"], "succeeded")
            claim = next(event for event in work.inspect(first["case"])["events"] if event["kind"] == "effect_started")
            self.assertEqual(claim["body"]["budget_id"], "ops")
            self.assertIn("intent_snapshot", claim["body"])
            with work.tx() as db:
                state = db.execute("SELECT state, amount_units FROM economic_reservations").fetchone()
            self.assertEqual(state["state"], "settled")
            self.assertEqual(state["amount_units"], 10)

            second = work.create_case("pay-invoice", 1, {"invoice_id": "inv-2"}, "operator")
            observed = work.observe(second, "invoice", {"amount_units": "1"}, "billing", "v2", "operator", verified=True)
            denied = work.propose(second, "pay", self._args(observed, "1"), "operator", ["operator"])
            self.assertEqual(denied["decision"]["status"], "denied")
            self.assertEqual(denied["decision"]["reason"], "economic_budget_exceeded")
            self.assertNotIn("rule", denied["decision"])
            work.close()

    def _pay(self, work, invoice_id, amount):
        case = work.create_case("pay-invoice", 1, {"invoice_id": invoice_id}, "operator")
        observed = work.observe(case, "invoice", {"amount_units": amount}, "billing", "v1", "operator", verified=True)
        proposed = work.propose(case, "pay", self._args(observed, amount), "operator", ["operator"])
        self.assertEqual(proposed["decision"]["status"], "allowed", proposed["decision"])
        result = work.commit(case, proposed["proposal"]["id"], "operator")
        result["case"] = case
        return result

    def _args(self, observed, amount):
        return {
            "operation": "transfer",
            "amount_units": amount,
            "asset": "USD",
            "counterparty": "vendor",
            "purpose": "invoice",
            "evidence": [observed["hash"]],
            "expires_at": time.time() + 600,
        }


class StabilizeMapping(unittest.TestCase):
    def test_translator_covers_the_four_stabilize_rows(self):
        allowed = translate_stabilize({"event": "operation_appended", "floor_blocking": False, "open_obligations": ["o1"]})
        self.assertEqual(allowed, {"mapped": True, "object": "AdmissionDecision", "status": "allowed", "reason": "obligations_open"})
        denied = translate_stabilize({"event": "stabilization_blocked", "open_floor_obligations": ["o1"]})
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(denied["object"], "AdmissionDecision")
        done = translate_stabilize({"event": "stabilize_appended", "operation": "Stabilize"})
        self.assertEqual(done, {"mapped": True, "object": "EffectOutcome", "state": "succeeded"})
        integrity = translate_stabilize({"event": "chain_mismatch"})
        self.assertFalse(integrity["mapped"])
        self.assertNotIn("status", integrity)
        self.assertNotIn("state", integrity)


class ArbitraryProject(unittest.TestCase):
    def test_release_gate_runs_without_syberwork(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("release_gate", EXAMPLES / "release_gate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run()
        self.assertTrue(result["complete"])
        self.assertTrue(result["chain_valid"])
        self.assertEqual(result["status"], "complete")
        kinds = [event["kind"] for event in result["events"]]
        self.assertEqual(kinds.count("effect_succeeded"), 2)
        self.assertEqual(kinds.count("signed"), 1)
        self.assertTrue(all("rule" not in event["body"] for event in result["events"] if event["kind"] == "decision"))


if __name__ == "__main__":
    unittest.main()
