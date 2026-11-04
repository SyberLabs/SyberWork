"""G3 Bridge adversarial pass: the gate's own rules, on synthetic inventories.

Factory gate G3 fails when an active SyberWork bridge red-team case does not
pass. The gate is ``scripts/g3_bridge_adversarial_pass.py``. These controls
build throwaway ``test_bridge_redteam*.py`` modules in a temporary tree and
prove that an active case passes only by passing, that an xfail must still
fail an assertion, that a stub needs a reason and is never a pass, and that a
missing or failing positive control fails the gate. The twin controls prove
that a refuse-everything or accept-everything destination match turns the gate
red, and that a soft suite is reported as not caught. They never read the real
inventory, so they hold on any tip.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "g3_bridge_adversarial_pass.py"
_spec = importlib.util.spec_from_file_location("g3_bridge_adversarial_pass", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(gate)

# Test bodies by kind. Each is the body of a TestCase method.
BODIES = {
    "pass": "self.assertTrue(True)",
    "fail": "self.assertEqual(1, 2)",
    "subtest_fail": "for i in range(2):\n    with self.subTest(i=i):\n        self.assertEqual(i, 0)",
    "runtime_skip": "self.skipTest('not ready')",
    "crash": "raise RuntimeError('incidental')",
    "placeholder": '"""Stub: the product hook does not exist yet."""',
    "bare_placeholder": "pass",
}
# kind -> (decorator, body kind)
KINDS = {
    "active": ("", "pass"),
    "active_fail": ("", "fail"),
    "active_subtest_fail": ("", "subtest_fail"),
    "active_runtime_skip": ("", "runtime_skip"),
    "active_crash": ("", "crash"),
    "xfail": ("@unittest.expectedFailure", "fail"),
    "xfail_crash": ("@unittest.expectedFailure", "crash"),
    "xpass": ("@unittest.expectedFailure", "pass"),
    "skip_stub": ("@unittest.skip('hook not built')", "fail"),
    "skip_stub_no_reason": ("@unittest.skip('')", "fail"),
    "placeholder_stub": ("", "placeholder"),
    "bare_placeholder_stub": ("", "bare_placeholder"),
}


def module_source(cases, controls=("active",), rubrics=("R1", "R8"), header="", extra_cases=None, tail=""):
    """A case module: ``cases`` is a list of (category, kind); ``controls`` are PositiveControl kinds."""
    tagged, methods, control_methods = {}, [], []
    for i, (category, kind) in enumerate(cases):
        name = f"test_{category.lower()}_{kind}_{i}"
        tagged[name] = (category, tuple(rubrics))
        decorator, body = KINDS[kind]
        methods.append(f"    {decorator}\n    def {name}(self):\n{textwrap.indent(BODIES[body], ' ' * 8)}\n")
    for i, kind in enumerate(controls):
        decorator, body = KINDS[kind]
        control_methods.append(f"    {decorator}\n    def test_control_{kind}_{i}(self):\n"
                               f"{textwrap.indent(BODIES[body], ' ' * 8)}\n")
    tagged.update(extra_cases or {})
    source = f"import unittest\n{header}\nCASES = {tagged!r}\n\n\nclass Bridge(unittest.TestCase):\n"
    source += "\n".join(methods) or "    pass\n"
    if control_methods:
        source += "\n\nclass PositiveControl(unittest.TestCase):\n" + "\n".join(control_methods)
    return source + tail


class Inventory(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        (self.folder / "tests").mkdir()
        self.package = f"_g3_fixture_{uuid.uuid4().hex}"

    def tearDown(self):
        for name in [n for n in sys.modules if n.startswith(self.package)]:
            del sys.modules[name]
        while str(self.folder) in sys.path:
            sys.path.remove(str(self.folder))

    def write(self, stem, source):
        (self.folder / "tests" / f"{stem}.py").write_text(source, encoding="utf-8")

    def evaluate(self):
        return gate.evaluate(self.folder, package=self.package, stream=io.StringIO())

    def assertPasses(self, report):
        self.assertTrue(report.ok, gate.summary_markdown(report))
        self.assertIn("G3 Bridge adversarial pass: PASS (active cases)", gate.summary_markdown(report))

    def assertFailsWith(self, report, *fragments):
        text = gate.summary_markdown(report)
        self.assertFalse(report.ok, text)
        self.assertIn("G3 Bridge adversarial pass: FAIL", text)
        for fragment in fragments:
            self.assertTrue(any(fragment in f for f in report.failures), f"{fragment!r} not in {report.failures}")


ALL_ACTIVE = [(c, "active") for c in gate.CATEGORIES]


class ActiveCases(Inventory):
    def test_every_active_case_and_control_passing_is_a_pass(self):
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE))
        report = self.evaluate()
        self.assertPasses(report)
        self.assertEqual(len(report.cases), len(gate.CATEGORIES) + 1)

    def test_cases_across_modules_and_unrelated_modules_ignored(self):
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE[:3], controls=()))
        self.write("test_bridge_redteam_more", module_source(ALL_ACTIVE[3:]))
        self.write("test_unrelated", module_source([("PI", "active_fail")]))  # outside the inventory glob
        report = self.evaluate()
        self.assertPasses(report)
        self.assertEqual(len(report.modules), 2)

    def test_an_active_case_that_does_not_pass_fails_the_gate(self):
        for kind, detail in (("active_fail", "ended failed"), ("active_subtest_fail", "subtest (i=1) AssertionError"),
                             ("active_runtime_skip", "ended skipped"), ("active_crash", "error RuntimeError")):
            with self.subTest(kind=kind):
                self.setUp()
                self.write("test_bridge_redteam", module_source(ALL_ACTIVE + [("REC", kind)]))
                report = self.evaluate()
                self.assertFailsWith(report, f"test_rec_{kind}_", detail)
                self.assertEqual(len(report.violations), 1)
                self.tearDown()

    def test_a_tagged_case_with_no_test_fails(self):
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE, extra_cases={"test_gone": ("REC", ("R1",))}))
        self.assertFailsWith(self.evaluate(), "test_gone", "no test method")

    def test_set_up_class_error_fails_and_its_cases_do_not_pass(self):
        tail = ("\n\nclass Broken(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
                "        raise RuntimeError('fixture down')\n\n    def test_broken(self):\n        self.assertTrue(True)\n")
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE, extra_cases={"test_broken": ("REC", ("R1",))},
                                                        tail=tail))
        self.assertFailsWith(self.evaluate(), "run error", "test_broken (REC, R1): active expects passed, ended not run")

    def test_no_active_adversarial_case_fails(self):
        self.write("test_bridge_redteam", module_source([("REC", "xfail"), ("PI", "skip_stub")]))
        self.assertFailsWith(self.evaluate(), "no active adversarial case")


class Gaps(Inventory):
    def test_xfail_that_fails_an_assertion_is_an_open_gap_not_a_pass(self):
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE + [("SIM", "xfail")]))
        report = self.evaluate()
        self.assertPasses(report)
        self.assertEqual([c.status for c in report.gaps], ["xfail"])
        text = gate.summary_markdown(report)
        self.assertIn("Open gaps", text)
        self.assertIn("| SIM | 1 | 0 | 1 | 0 | 0 |", text)

    def test_xfail_that_passes_or_crashes_fails_the_gate(self):
        for kind, fragment in (("xpass", "passed unexpectedly"), ("xfail_crash", "not an observed gap")):
            with self.subTest(kind=kind):
                self.setUp()
                self.write("test_bridge_redteam", module_source(ALL_ACTIVE + [("SCH", kind)]))
                self.assertFailsWith(self.evaluate(), fragment)
                self.tearDown()

    def test_stub_with_a_reason_is_an_open_gap(self):
        for kind, reason in (("skip_stub", "hook not built"), ("placeholder_stub", "product hook does not exist")):
            with self.subTest(kind=kind):
                self.setUp()
                self.write("test_bridge_redteam", module_source(ALL_ACTIVE + [("SCH", kind)]))
                report = self.evaluate()
                self.assertPasses(report)
                self.assertEqual([c.status for c in report.gaps], ["stub"])
                self.assertIn(reason, gate.summary_markdown(report))
                self.tearDown()

    def test_stub_without_a_reason_fails(self):
        for kind in ("skip_stub_no_reason", "bare_placeholder_stub"):
            with self.subTest(kind=kind):
                self.setUp()
                self.write("test_bridge_redteam", module_source(ALL_ACTIVE + [("SCH", kind)]))
                self.assertFailsWith(self.evaluate(), "stub with no reason")
                self.tearDown()

    def test_stubs_never_stand_in_for_active_cases(self):
        self.write("test_bridge_redteam", module_source([(c, "skip_stub") for c in gate.CATEGORIES]))
        self.assertFailsWith(self.evaluate(), "no active adversarial case")


class PositiveControls(Inventory):
    def test_no_positive_control_fails(self):
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE, controls=()))
        self.assertFailsWith(self.evaluate(), "no active positive control")

    def test_a_failing_or_inactive_positive_control_fails(self):
        for kind, fragment in (("active_fail", "ended failed"), ("xfail", "positive control is xfail"),
                               ("skip_stub", "positive control is stub")):
            with self.subTest(kind=kind):
                self.setUp()
                self.write("test_bridge_redteam", module_source(ALL_ACTIVE, controls=("active", kind)))
                self.assertFailsWith(self.evaluate(), fragment)
                self.tearDown()

    def test_listed_positive_controls_are_run_and_must_exist(self):
        tail = "\n\nclass Other(unittest.TestCase):\n    def test_listed_control(self):\n        self.assertTrue(True)\n"
        header = "POSITIVE_CONTROLS = ('test_listed_control', 'test_missing_control')"
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE, controls=(), header=header, tail=tail))
        report = self.evaluate()
        self.assertFailsWith(report, "test_missing_control", "no test method")
        listed = next(c for c in report.cases if c.name == "test_listed_control")
        self.assertEqual((listed.category, listed.outcome), (gate.CONTROL, "passed"))


class InventoryShape(Inventory):
    def test_no_inventory_fails(self):
        self.assertFailsWith(self.evaluate(), "no inventory", "no active adversarial case", "no active positive control")

    def test_import_failure_and_missing_cases_fail(self):
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE))
        self.write("test_bridge_redteam_broken", "import not_a_module_anywhere\n")
        self.write("test_bridge_redteam_untagged", "import unittest\n")
        self.assertFailsWith(self.evaluate(), "import failed", "no CASES mapping")

    def test_bad_tags_fail(self):
        for extra, fragment in (({"test_x": "REC"}, "is not (category, rubric lines)"),
                                ({"test_x": ("ISO", ("R1",))}, "not in the bridge row"),
                                ({"test_x": ("REC", ("R1", "R10"))}, "subset of R1-R9"),
                                ({"test_x": ("REC", ())}, "subset of R1-R9"),
                                ({"test_x": ("REC", ("R8",))}, "does not tag R1")):
            with self.subTest(extra=extra):
                self.setUp()
                tail = "\n\nclass X(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
                self.write("test_bridge_redteam", module_source(ALL_ACTIVE, extra_cases=extra, tail=tail))
                self.assertFailsWith(self.evaluate(), fragment)
                self.tearDown()

    def test_name_in_two_classes_or_both_case_and_control_fails(self):
        tail = "\n\nclass Twice(unittest.TestCase):\n    def test_rec_active_6(self):\n        self.assertTrue(True)\n"
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE, tail=tail))
        self.assertFailsWith(self.evaluate(), "defined in several classes")
        self.tearDown()
        self.setUp()
        header = "POSITIVE_CONTROLS = ('test_rec_active_6',)"
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE, header=header))
        self.assertFailsWith(self.evaluate(), "both a tagged case and a positive control")

    def test_main_exit_code_and_json_report(self):
        self.write("test_bridge_redteam", module_source(ALL_ACTIVE + [("REC", "active_fail")]))
        out = self.folder / "report.json"
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("GITHUB_STEP_SUMMARY", None)
            with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                code = gate.main(["--root", str(self.folder), "--report", str(out)])
        self.assertEqual(code, 1)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual([c["name"] for c in data["cases"] if c["violation"]], ["test_rec_active_fail_7"])


# A miniature tree for the twins: a destination match carrying the exact line the twins rewrite.
TWIN_CORE = '''def reconcile(record):
            matched = record == "match"
            if False:
                status = "skip"
''' + gate.MATCH_LINE + '''            else:
                status, reason = "unverified", "destination_record_mismatch"
            return status
'''
TWIN_SUITE = '''import unittest
from syberwork.core import reconcile

CASES = {"test_mismatch_stays_unverified": ("REC", ("R1", "R8"))}


class Rec(unittest.TestCase):
    def test_mismatch_stays_unverified(self):
        %s


class PositiveControl(unittest.TestCase):
    def test_match_verifies(self):
        self.assertEqual(reconcile("match"), "verified")
'''
STRICT = 'self.assertEqual(reconcile("other"), "unverified")'
SOFT = 'self.assertIn(reconcile("other"), ("verified", "unverified"))'  # asserts nothing about the outcome


class Twins(unittest.TestCase):
    def tree(self, case_body: str, core: str = TWIN_CORE) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for folder in ("scripts", "syberwork", "tests"):
            (root / folder).mkdir()
        shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
        (root / "syberwork" / "__init__.py").write_text("", encoding="utf-8")
        (root / "syberwork" / "core.py").write_text(core, encoding="utf-8")
        (root / "tests" / "test_bridge_redteam.py").write_text(TWIN_SUITE % case_body, encoding="utf-8")
        return root

    def baseline(self, root: Path) -> Path:
        out = root.parent / f"{root.name}-baseline.json"
        self.addCleanup(out.unlink, True)
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        proc = subprocess.run([sys.executable, str(root / "scripts" / SCRIPT.name), "--report", str(out)],
                              cwd=root, env=env | {"PYTHONPATH": str(root)}, capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out

    def test_strict_suite_catches_both_twins(self):
        root = self.tree(STRICT)
        ok, text = gate.run_twins(root, self.baseline(root))
        self.assertTrue(ok, text)
        self.assertIn("| refuse-everything | 1 | positive control | 1 | caught |", text)
        self.assertIn("| accept-everything | 1 | active adversarial case | 1 | caught |", text)
        self.assertEqual((root / "syberwork" / "core.py").read_text(encoding="utf-8"), TWIN_CORE)

    def test_soft_suite_is_reported_as_not_caught(self):
        root = self.tree(SOFT)
        ok, text = gate.run_twins(root, self.baseline(root))
        self.assertFalse(ok, text)
        self.assertIn("refuse-everything | 1 | positive control | 1 | caught", text)
        self.assertIn("accept-everything | 0 | active adversarial case | 0 | **NOT CAUGHT**", text)

    def test_missing_match_line_or_baseline_fails_closed(self):
        root = self.tree(STRICT)
        baseline = self.baseline(root)
        (root / "syberwork" / "core.py").write_text(TWIN_CORE.replace("if matched:", "if matched is True:"),
                                                    encoding="utf-8")
        ok, text = gate.run_twins(root, baseline)
        self.assertFalse(ok)
        self.assertIn("found 0 times, expected 1", text)
        ok, text = gate.run_twins(root, root / "absent.json")
        self.assertFalse(ok)
        self.assertIn("No usable baseline report", text)
        empty = root / "empty.json"
        empty.write_text(json.dumps({"cases": []}), encoding="utf-8")
        ok, text = gate.run_twins(root, empty)
        self.assertFalse(ok)
        self.assertIn("no active case", text)


if __name__ == "__main__":
    unittest.main()
