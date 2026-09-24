import tempfile
import unittest
from pathlib import Path

from case_studies.enterprise_procurement import run_study


class EnterpriseProcurementStudy(unittest.TestCase):
    def test_ten_isolated_scenarios_close_or_cancel_authoritatively(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_study(Path(folder))
        scenarios = {item["id"]: item for item in report["scenarios"]}
        self.assertEqual(len(scenarios), 10)
        self.assertEqual(scenarios["complete"]["outcome"], "complete")
        self.assertEqual(scenarios["missing_input"]["outcome"], "input_rejected")
        self.assertEqual(scenarios["missing_site"]["outcome"], "resolved_site")
        self.assertEqual(scenarios["ambiguous_quote"]["outcome"], "resolved_quote")
        self.assertEqual(scenarios["policy_tightened"]["outcome"], "blocked_at_commit")
        self.assertEqual(scenarios["stale_quote"]["outcome"], "recovered_after_refresh")
        self.assertEqual(scenarios["lost_ack"]["outcome"], "reconciled_complete")
        self.assertEqual(scenarios["forged_claim"]["outcome"], "blocked_unproven_claim")
        self.assertEqual(scenarios["mismatched_status"]["outcome"], "blocked_mismatched_record")
        self.assertEqual(scenarios["quote_cancelled"]["status"], "cancelled")
        self.assertEqual([scenarios[key]["external_orders"] for key in scenarios], [1, 0, 1, 1, 0, 1, 1, 0, 1, 0])
        self.assertEqual(scenarios["missing_site"]["external_state"]["updates"][0]["actor_role"], "logistics")
        self.assertEqual(scenarios["ambiguous_quote"]["external_state"]["updates"][0]["actor_role"], "procurement")
        self.assertTrue(all(item["chain_valid"] for item in scenarios.values() if item["case_id"]))
        self.assertTrue(all(item["acceptance_complete"] for item in scenarios.values() if item["outcome"] in ("complete", "recovered_after_refresh", "reconciled_complete", "resolved_site", "resolved_quote")))
        self.assertFalse(any(item["acceptance_complete"] for item in scenarios.values() if item["outcome"].startswith("blocked")))
        self.assertFalse(scenarios["quote_cancelled"]["acceptance_complete"])


if __name__ == "__main__":
    unittest.main()
