"""The G4 Positive reconcile runner fails closed, and its manifest names every G4 test."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("g4_positive_reconcile", ROOT / "scripts" / "g4_positive_reconcile.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

G4_MODULES = ("tests.test_g4_positive_reconcile", "tests.test_g4_positive_reconcile_gate")

FAKE_TESTS = textwrap.dedent('''
    import unittest

    class Fake(unittest.TestCase):
        def test_pass_1(self): pass
        def test_pass_2(self): pass
        def test_pass_3(self): pass
        def test_pass_4(self): pass
        def test_pass_5(self): pass
        def test_pass_6(self): pass
        def test_fail(self): self.fail("control missed")
        def test_error(self): raise RuntimeError("boom")
        def test_skip(self): self.skipTest("no destination")
        @unittest.expectedFailure
        def test_xfail(self): self.fail("known")
        @unittest.expectedFailure
        def test_xpass(self): pass
        def test_subtest_fail(self):
            for value in (1, 2):
                with self.subTest(value=value):
                    self.assertEqual(value, 1)
''')


def all_ids(module: str) -> set[str]:
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    return {test.id() for test in runner.flatten(suite)}


class G4Gate(unittest.TestCase):
    def test_manifest_lists_every_g4_test(self):
        manifest = runner.read_manifest(ROOT / runner.MANIFEST)
        listed = {test_id for name, ids in manifest.items() if name != "existing" for test_id in ids}
        defined = set().union(*(all_ids(module) for module in G4_MODULES))
        self.assertEqual(sorted(defined - listed), [], "G4 tests missing from the manifest")
        self.assertEqual(sorted(listed - defined), [], "manifest ids with no G4 test")
        for test_id in manifest["existing"]:
            with self.subTest(test_id=test_id):
                tests = list(runner.flatten(unittest.defaultTestLoader.loadTestsFromName(test_id)))
                self.assertEqual([test.id() for test in tests], [test_id])

    def test_every_required_section_is_listed(self):
        manifest = runner.read_manifest(ROOT / runner.MANIFEST)
        for name in runner.REQUIRED_SECTIONS:
            with self.subTest(section=name):
                self.assertTrue(manifest.get(name))


class G4RunnerFailsClosed(unittest.TestCase):
    """Run the real runner on a scratch tree with a fake test module."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_g4fake.py").write_text(FAKE_TESTS, encoding="utf-8")

    def manifest(self, control="test_pass_1", extra=""):
        lines = []
        for number, name in enumerate(runner.REQUIRED_SECTIONS, 1):
            method = control if name == "control" else f"test_pass_{number}"
            lines += [f"[{name}]", f"tests.test_g4fake.Fake.{method}" if method else ""]
        (self.root / runner.MANIFEST).write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")

    def gate(self) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "g4_positive_reconcile.py"),
                               "--root", str(self.root)], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout + proc.stderr

    def assert_fails(self, message):
        code, output = self.gate()
        self.assertEqual(code, 1, output)
        self.assertIn("G4 Positive reconcile: FAILED", output)
        self.assertIn(message, output)

    def test_all_listed_ids_passing_is_ok(self):
        self.manifest()
        code, output = self.gate()
        self.assertEqual(code, 0, output)
        self.assertIn("6 listed ids passed", output)

    def test_missing_manifest_fails(self):
        self.assert_fails("manifest is missing")

    def test_missing_test_file_fails(self):
        self.manifest()
        (self.root / "tests" / "test_g4fake.py").unlink()
        self.assert_fails("test file is missing")

    def test_unknown_id_fails(self):
        self.manifest(control="test_renamed")
        self.assert_fails("test_renamed: not found")

    def test_empty_section_fails(self):
        self.manifest(control="")
        self.assert_fails("section [control] is empty or absent")

    def test_unknown_section_and_duplicate_id_fail(self):
        self.manifest(extra="[bonus]\n")
        self.assert_fails("unknown section [bonus]")
        self.manifest(extra="[control]\n")
        self.assert_fails("section [control] repeated")
        self.manifest(extra="tests.test_g4fake.Fake.test_pass_1\n")
        self.assert_fails("duplicate id")

    def test_every_non_pass_outcome_fails(self):
        for method, outcome in (("test_fail", "failed"), ("test_error", "error"), ("test_skip", "skipped"),
                                ("test_xfail", "xfail"), ("test_xpass", "xpass"), ("test_subtest_fail", "failed")):
            with self.subTest(method=method):
                self.manifest(control=method)
                self.assert_fails(f"tests.test_g4fake.Fake.{method}: {outcome}")


if __name__ == "__main__":
    unittest.main()
