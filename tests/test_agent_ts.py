"""The TypeScript/JavaScript S0 Decision Port client in packages/agent-ts, checked from Python.

Runs the client's own Node test suite, then checks across languages that every DecisionOutcome
the client emits validates against spec/v1/decision-outcome.schema.json and that its request
digest equals SHA-256 over this repository's canonical JSON. Skips when node is not on PATH
(the reason G8 allows); CI's Linux and Windows images have it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from spec.v1.check import check_v1
from syberlabs.canonical import canonical

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "agent-ts"
NODE = shutil.which("node")


def node_major() -> int:
    if not NODE:
        return 0
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    try:
        return int(out.lstrip("v").split(".", 1)[0])
    except ValueError:
        return 0


# The skip reason is one scripts/g8_existing_suite.py allows. A Node older than 20 is not skipped: the tests run and fail.
@unittest.skipUnless(NODE, "node is not installed")
class DecisionPortClientTests(unittest.TestCase):
    def run_node(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([NODE, *args], cwd=PACKAGE, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)

    def test_node_suite_passes(self):
        result = self.run_node("--test", "test/decision.test.js")
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])

    def test_emitted_outcomes_validate_and_digests_match_python(self):
        result = self.run_node("test/emit.mjs")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        emitted = json.loads(result.stdout)
        self.assertGreater(len(emitted["outcomes"]), 20)
        for item in emitted["outcomes"]:
            with self.subTest(item["name"]):
                check_v1(item["outcome"], "decision-outcome.schema.json")
        self.assertGreater(len(emitted["digests"]), 0)
        for item in emitted["digests"]:
            with self.subTest(item["name"]):
                expected = hashlib.sha256(canonical(item["request"]).encode("utf-8")).hexdigest()
                self.assertEqual(item["digest"], expected)


if __name__ == "__main__":
    unittest.main()
