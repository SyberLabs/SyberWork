import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syberlabs.planner import StaticPlanner
from syberwork.core import Rejected, Work


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class PlannerInterface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Work(Path(self.tmp.name) / "work.sqlite3")
        self.work.install_contract(json.loads((EXAMPLES / "contract.json").read_text()))
        self.work.install_policy(json.loads((EXAMPLES / "policy.json").read_text()))
        self.work.install_action("record_review", {"kind": "local"})
        self.work.install_action("issue_order", {"kind": "local"})
        self.case = self.work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")

    def test_static_planner_is_still_admitted_and_can_be_denied(self):
        seen = {}

        class Spy(StaticPlanner):
            def propose(self, context):
                seen["context"] = dict(context)
                return super().propose(context)

        planner = Spy("issue_order", {"amount": 10})
        with patch.dict(os.environ, {"SYBERWORK_PLANNER_TOKEN": "executor-must-not-appear"}):
            result = self.work.model_propose(self.case, "planner", ["model", "operator"], planner=planner)
        self.assertEqual(set(seen["context"]), {"objective", "allowed_actions", "arguments", "required_facts", "acceptance"})
        encoded = json.dumps(seen["context"])
        self.assertNotIn("executor-must-not-appear", encoded)
        self.assertNotIn("P-104", encoded)
        self.assertEqual(set(seen["context"]["allowed_actions"]), {"record_review", "issue_order"})
        self.assertEqual(seen["context"]["acceptance"], [
            {"id": "order_sent", "passed": False},
            {"id": "manager_signed", "passed": False},
        ])
        self.assertEqual(result["proposal"]["origin"], "model")
        self.assertEqual(result["decision"]["status"], "denied")
        self.assertEqual(result["decision"]["reason"], "required_prior_effect_missing")
        self.assertFalse(any(event["kind"] == "effect_started" for event in self.work.inspect(self.case)["events"]))

    def test_missing_planner_url_is_unchanged(self):
        with patch.dict(os.environ, {"SYBERWORK_PLANNER_URL": ""}, clear=False):
            with self.assertRaises(Rejected) as error:
                self.work.model_propose(self.case, "planner", ["model"])
        self.assertEqual(error.exception.code, "planner_unconfigured")


if __name__ == "__main__":
    unittest.main()
