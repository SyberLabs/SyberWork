"""Tests for the standalone Attest receipt observation example."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from examples.attest_receipt_observe import (
    RECEIPT_SCHEMA,
    load_receipt_observation,
    receipt_to_observation,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "attest_receipt_observe.py"
FIXTURE = ROOT / "examples" / "fixtures" / "attest_receipt_v0_sample.json"
SPEC_FIXTURE = ROOT / "examples" / "fixtures" / "attest_receipt_v0_spec_sample.json"


class AttestReceiptObserveExample(unittest.TestCase):
    def setUp(self):
        self.receipt = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_happy_path_maps_receipt_to_syberwork_observation(self):
        observation = load_receipt_observation(FIXTURE)
        self.assertEqual(observation["key"], "attest_receipt")
        self.assertEqual(observation["value"], self.receipt)
        self.assertEqual(observation["source"], "attest")
        self.assertEqual(observation["version"], RECEIPT_SCHEMA)
        self.assertEqual(observation["actor"], "attest-bridge")
        self.assertIs(observation["verified"], False)

    def test_spec_mode_implemented_receipt_maps_ticket_patch_and_kane_recheck(self):
        receipt = json.loads(SPEC_FIXTURE.read_text(encoding="utf-8"))
        observation = load_receipt_observation(SPEC_FIXTURE)
        value = observation["value"]

        self.assertEqual(value["implementation"], {
            "ticket_title": receipt["ticket"]["title"],
            "app_patch_sha256": receipt["app"]["patch_sha256"],
        })
        self.assertEqual(value["destination_verification"], {
            "status": "proven",
            "method": "Kane re-check",
        })
        self.assertIs(value["verified"], False)
        self.assertIs(observation["verified"], False)

    def test_implemented_receipt_requires_ticket_title_and_patch_digest(self):
        receipt = json.loads(SPEC_FIXTURE.read_text(encoding="utf-8"))
        receipt["app"]["patch_sha256"] = None
        with self.assertRaisesRegex(ValueError, "must include app.patch_sha256"):
            receipt_to_observation(receipt)

    def test_wrong_schema_is_rejected(self):
        self.receipt["schema"] = "attest.receipt/v1"
        with self.assertRaisesRegex(ValueError, "schema must be attest.receipt/v0"):
            receipt_to_observation(self.receipt)

    def test_unexpected_fields_are_rejected(self):
        self.receipt["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unexpected fields: unexpected"):
            receipt_to_observation(self.receipt)

    def test_import_never_marks_receipt_or_observation_verified(self):
        observation = receipt_to_observation(self.receipt)
        self.assertIs(self.receipt["verified"], False)
        self.assertIs(observation["verified"], False)

        claimed_verified = dict(self.receipt, verified=True)
        with self.assertRaisesRegex(ValueError, "verified must be false"):
            receipt_to_observation(claimed_verified)

    def test_example_cli_prints_observation_and_exits_successfully(self):
        result = subprocess.run(
            [sys.executable, str(EXAMPLE), str(FIXTURE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observation = json.loads(result.stdout)
        self.assertEqual(observation["value"], self.receipt)
        self.assertIs(observation["verified"], False)


if __name__ == "__main__":
    unittest.main()
