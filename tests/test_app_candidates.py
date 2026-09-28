"""Candidates in the SyberWork application: Work API and HTTP routes with separated credentials."""

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from syberwork.core import Rejected, Work
from syberwork.server import make_server
from tests.test_evolution import candidate, contract, evaluation, promote_args

POLICY = {"version": 1, "actions": {"promote": {"roles": ["developer", "operator", "model"]}}}


class Fixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Work(Path(folder.name) / "work.sqlite3")
        self.addCleanup(self.work.close)
        self.work.install_contract(contract())
        self.work.install_policy(POLICY)
        self.work.install_action("promote", {"kind": "local"})
        self.case = self.work.create_case("repo-change", 1, {"objective": "Add CSV export"}, "developer")


class WorkCandidates(Fixture):
    def test_search_registers_evaluator_judges_person_promotes(self):
        self.work.record_candidate(self.case, candidate(1), "search-host", ["search"])
        self.assertEqual(self.work.propose(self.case, "promote", promote_args(1), "developer", ["developer"])["decision"]["reason"],
                         "candidate_not_evaluated")
        self.work.record_evaluation(self.case, evaluation(1), "ci", ["evaluator"])
        proposal = self.work.propose(self.case, "promote", promote_args(1), "developer", ["developer"])
        self.assertEqual(proposal["decision"]["status"], "allowed")
        self.assertEqual(self.work.commit(self.case, proposal["proposal"]["id"], "developer")["status"], "succeeded")
        inspected = self.work.inspect(self.case)
        self.assertTrue(inspected["complete"])
        self.assertTrue(inspected["candidates"][0]["authoritative"])
        self.assertTrue(self.work.verify_chain(self.case))

    def test_credentials_are_separated(self):
        with self.assertRaises(Rejected):
            self.work.record_candidate(self.case, candidate(1), "guest", ["observer"])
        self.work.record_candidate(self.case, candidate(1), "search-host", ["search", "operator"])
        for roles in (["search"], ["operator"], ["evaluator", "model"], ["evaluator", "search"]):
            with self.subTest(roles), self.assertRaises(Rejected) as caught:
                self.work.record_evaluation(self.case, evaluation(1), "ci", roles)
            self.assertEqual(caught.exception.code, "evaluation_denied")
        with self.assertRaises(Rejected) as caught:
            self.work.record_evaluation(self.case, evaluation(1), "search-host", ["evaluator"])
        self.assertEqual(caught.exception.detail, "the actor that registered a candidate cannot evaluate it")

    def test_lineage_and_model_rules_hold_in_the_app(self):
        self.work.record_candidate(self.case, candidate(1), "search-host", ["search"])
        self.work.record_candidate(self.case, candidate(2), "search-host", ["search"])
        self.work.record_evaluation(self.case, evaluation(1), "ci", ["evaluator"])
        self.work.record_evaluation(self.case, evaluation(2), "ci", ["evaluator"])
        self.work.record_candidate(self.case, candidate(3, parents=("c1", "c2"), operator="crossover",
                                                        signal={"fitness": 1.0}), "search-host", ["search"])
        self.assertEqual(self.work.propose(self.case, "promote", promote_args(3), "developer", ["developer"])["decision"]["reason"],
                         "candidate_not_evaluated")
        model = self.work.propose(self.case, "promote", promote_args(1), "planner", ["operator", "model"], origin="model")
        self.assertEqual(model["decision"]["reason"], "candidate_promotion_origin")

    def test_cancelled_case_refuses_candidates(self):
        self.work.cancel_case(self.case, "abandoned", "manager", ["manager"])
        with self.assertRaises(Rejected) as caught:
            self.work.record_candidate(self.case, candidate(1), "search-host", ["search"])
        self.assertEqual(caught.exception.code, "case_cancelled")


class HttpCandidates(Fixture):
    def setUp(self):
        super().setUp()
        self.tokens = {"searcher": ["search"], "evaluator": ["evaluator"], "developer": ["operator", "developer"]}
        users = {name: {"hash": hashlib.sha256(name.encode()).hexdigest(), "roles": roles, "sources": []}
                 for name, roles in self.tokens.items()}
        self.server = make_server(self.work, users, port=0)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}/api/cases/{self.case}/"

    def call(self, who, path, data=None):
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                         headers={"Authorization": "Bearer " + who, "Content-Type": "application/json"},
                                         method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_routes(self):
        self.assertEqual(self.call("searcher", "candidates", candidate(1))[0], 200)
        status, body = self.call("searcher", "evaluations", evaluation(1))
        self.assertEqual((status, body["error"]), (409, "evaluation_denied"))
        self.assertEqual(self.call("evaluator", "evaluations", evaluation(1))[0], 200)
        search = {"phase": "started", "search": {"id": "s1", "provider": {"name": "remote", "revision": "1"},
                  "operators": ["patch"], "budget": {"max_candidates": 1, "max_evaluations": 1, "max_seconds": 1},
                  "base": "0" * 40, "context_digest": None}}
        self.assertEqual(self.call("searcher", "searches", search)[0], 200)
        status, views = self.call("developer", "candidates")
        self.assertEqual((status, views[0]["evaluation"]["state"]), (200, "passed"))
        status, proposal = self.call("developer", "proposals", {"action": "promote", "args": promote_args(1)})
        self.assertEqual(proposal["decision"]["status"], "allowed")
        status, done = self.call("developer", "commit", {"proposal_id": proposal["proposal"]["id"]})
        self.assertEqual(done["status"], "succeeded")
        status, case = self.call("developer", "")
        self.assertEqual(case["candidates"][0]["promotion"]["state"], "promoted")


if __name__ == "__main__":
    unittest.main()
