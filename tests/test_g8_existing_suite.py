import importlib.util
import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("g8_existing_suite", ROOT / "scripts" / "g8_existing_suite.py")
g8 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g8)


class G8Gate(unittest.TestCase):
    """The G8 runner fails closed on a fake suite; module names are unique so discovery never reuses one."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.start = self.folder.name
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(lambda: sys.path.remove(self.start) if self.start in sys.path else None)

    def module(self, body: str) -> str:
        name = "test_g8_fake_" + uuid.uuid4().hex[:12]
        (Path(self.start) / f"{name}.py").write_text("import unittest\n\n" + body)
        self.addCleanup(sys.modules.pop, name, None)
        return name

    def run_gate(self, *allowed: str) -> dict:
        return g8.run(self.start, set(g8.ALLOWED_SKIPS) | set(allowed), stream=io.StringIO())

    def test_a_green_suite_with_an_allowed_skip_passes(self):
        self.module(
            "class Case(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n"
            "    @unittest.skip('node is not installed')\n"
            "    def test_node(self):\n        pass\n"
        )
        report = self.run_gate()
        self.assertTrue(report["passed"], report["reasons"])
        self.assertEqual((report["ran"], report["skipped"]), (2, 1))
        self.assertEqual(report["skips"], [{"reason": "node is not installed", "count": 1, "allowed": True}])

    def test_a_module_skipped_because_postgres_is_unreachable_fails(self):
        self.module(
            "raise unittest.SkipTest('PostgreSQL is not reachable')\n"
        )
        self.module("class Case(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        report = self.run_gate()
        self.assertFalse(report["passed"])
        self.assertEqual(report["reasons"], ["1 skipped for a reason not allowed"])
        self.assertFalse(report["skips"][0]["allowed"])

    def test_an_extra_allowed_reason_must_match_exactly(self):
        self.module("raise unittest.SkipTest('hypothesis is not installed')\n")
        self.module("class Case(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        self.assertFalse(self.run_gate("hypothesis is not installed ")["passed"])
        self.assertFalse(self.run_gate("Hypothesis is not installed")["passed"])
        self.assertTrue(self.run_gate("hypothesis is not installed")["passed"])

    def test_a_failure_an_error_and_an_import_failure_fail(self):
        name = self.module(
            "class Case(unittest.TestCase):\n"
            "    def test_fails(self):\n        self.assertEqual(1, 2)\n"
            "    def test_errors(self):\n        raise RuntimeError('boom')\n"
        )
        self.module("import a_module_that_does_not_exist\n")
        report = self.run_gate()
        self.assertFalse(report["passed"])
        self.assertEqual((report["failed"], report["errored"]), (1, 2))
        self.assertIn(f"{name}.Case.test_fails", report["failing"])

    def test_an_unexpected_success_fails(self):
        self.module(
            "class Case(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n"
            "    def test_fixed(self):\n        pass\n"
        )
        report = self.run_gate()
        self.assertFalse(report["passed"])
        self.assertEqual(report["reasons"], ["1 unexpected successes"])

    def test_no_tests_fails(self):
        report = self.run_gate()
        self.assertFalse(report["passed"])
        self.assertEqual(report["reasons"], ["no tests ran"])

    def test_main_writes_the_report_and_exits_nonzero_on_failure(self):
        self.module("class Case(unittest.TestCase):\n    def test_fails(self):\n        self.fail()\n")
        out = Path(self.start) / "g8.json"
        stderr, stdout = sys.stderr, sys.stdout
        sys.stderr = sys.stdout = io.StringIO()
        try:
            code = g8.main(["--start", self.start, "--report", str(out)])
        finally:
            sys.stderr, sys.stdout = stderr, stdout
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.read_text())["failed"], 1)


class G8Allowlist(unittest.TestCase):
    def test_every_allowed_reason_is_a_skip_in_the_suite(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "tests").glob("test_*.py")
                           if path.name != "test_g8_existing_suite.py")
        for reason in g8.ALLOWED_SKIPS:
            self.assertIn(f'"{reason}"', source, reason)

    def test_service_and_dependency_skips_are_not_allowed(self):
        for reason in ("psycopg is not installed", "PostgreSQL is not reachable",
                       "hypothesis is not installed; pip install -e .[test]"):
            self.assertNotIn(reason, g8.ALLOWED_SKIPS)


if __name__ == "__main__":
    unittest.main()
