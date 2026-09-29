"""A case database created on main still verifies and replays."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from syberwork.core import Work


FIXTURE = Path(__file__).resolve().parents[1] / "conformance" / "fixtures"


class HistoryFromMain(unittest.TestCase):
    def test_verify_and_replay(self):
        meta = json.loads((FIXTURE / "main_history.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "work.sqlite"
            shutil.copy(FIXTURE / "main_history.sqlite", copy)
            work = Work(copy)
            self.assertTrue(work.verify_chain(meta["case_id"]))
            state = work.inspect(meta["case_id"])
            self.assertEqual([event["kind"] for event in state["events"]], meta["event_kinds"])
            decisions = [
                [event["kind"], event["body"]["status"], event["body"]["reason"]]
                for event in state["events"] if event["kind"] == "decision"
            ]
            self.assertEqual(decisions, meta["decision_events"])
            self.assertTrue(state["complete"])
            replay = work.replay(meta["case_id"], meta["contract_version"], meta["policy_version"])
            self.assertEqual(replay["changed_decisions"], [])
            self.assertEqual(replay["acceptance_before"], replay["acceptance_after"])
            work.close()
