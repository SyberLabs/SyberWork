"""G2 Bridge quota floor: the counter's own rules, on synthetic inventories.

Factory gate G2 fails when SyberWork bridge red-team cases per category fall
below the §1 minimums. The counter is ``scripts/g2_bridge_quota_floor.py``.
These controls build throwaway ``test_bridge_redteam*.py`` modules in a
temporary tree and prove the counting lock: ``active`` and ``xfail`` count,
a ``stub`` never does, and a case counts only when its test ended the way its
status says. They also prove that a short cell, an empty inventory, or a
broken module fails the gate. They never read the real inventory, so they hold
on any tip.
"""

import importlib.util
import io
import shutil
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("g2_bridge_quota_floor", ROOT / "scripts" / "g2_bridge_quota_floor.py")
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
    "placeholder": '"""Stub: to be written."""',
    "not_implemented": "raise NotImplementedError",
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
    "skip_stub": ("@unittest.skip('stub')", "fail"),
    "placeholder_stub": ("", "placeholder"),
    "xfail_placeholder_stub": ("@unittest.expectedFailure", "placeholder"),
    "xfail_not_implemented_stub": ("@unittest.expectedFailure", "not_implemented"),
}


def module_source(cases, extra_cases=None, header=""):
    """A case module: ``cases`` is a list of (category, kind); names are generated."""
    tagged, methods = {}, []
    for i, (category, kind) in enumerate(cases):
        name = f"test_{category.lower()}_{kind}_{i}"
        tagged[name] = (category, ("R1",))
        decorator, body = KINDS[kind]
        methods.append(f"    {decorator}\n    def {name}(self):\n{textwrap.indent(BODIES[body], ' ' * 8)}\n")
    tagged.update(extra_cases or {})
    return (f"import unittest\n{header}\nCASES = {tagged!r}\n\n\nclass Bridge(unittest.TestCase):\n"
            + ("\n".join(methods) or "    pass\n"))


def full(**overrides):
    """Exactly the §1 floors, all active, with ``overrides`` (category -> kinds) replacing a cell."""
    cases = []
    for category, floor in gate.FLOORS.items():
        cases += [(category, k) for k in overrides.get(category, ["active"] * floor)]
    return cases


class Inventory(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        (self.folder / "tests").mkdir()
        self.package = f"_g2_fixture_{uuid.uuid4().hex}"

    def tearDown(self):
        for name in [n for n in sys.modules if n.startswith(self.package)]:
            del sys.modules[name]

    def write(self, stem, source):
        (self.folder / "tests" / f"{stem}.py").write_text(source, encoding="utf-8")

    def evaluate(self):
        return gate.evaluate(self.folder, package=self.package, stream=io.StringIO())

    def counted(self, report, category):
        return report.cell(category)["counted"]


class CountingLock(Inventory):
    def test_positive_control_exact_floors_pass(self):
        self.write("test_bridge_redteam", module_source(full()))
        report = self.evaluate()
        self.assertTrue(report.ok, gate.summary_markdown(report))
        self.assertEqual({c: self.counted(report, c) for c in gate.FLOORS}, gate.FLOORS)
        self.assertIn("G2 Bridge quota floor: PASS", gate.summary_markdown(report))

    def test_cases_across_modules_add_up(self):
        cases = full()
        self.write("test_bridge_redteam", module_source(cases[:10]))
        self.write("test_bridge_redteam_more", module_source(cases[10:]))
        self.write("test_unrelated", module_source([("PI", "active_fail")]))  # outside the inventory glob
        report = self.evaluate()
        self.assertTrue(report.ok, gate.summary_markdown(report))
        self.assertEqual(len(report.modules), 2)

    def test_xfail_counts_and_active_only_column_shows_the_difference(self):
        self.write("test_bridge_redteam", module_source(full(SCH=["active", "active", "active", "xfail"])))
        report = self.evaluate()
        self.assertTrue(report.ok, gate.summary_markdown(report))
        cell = report.cell("SCH")
        self.assertEqual((cell["active"], cell["xfail"], cell["counted"], cell["active_only"]), (3, 1, 4, 3))

    def test_stubs_never_count_and_short_cell_fails(self):
        for stub in ("skip_stub", "placeholder_stub", "xfail_placeholder_stub", "xfail_not_implemented_stub"):
            with self.subTest(stub=stub):
                self.setUp()
                self.write("test_bridge_redteam", module_source(full(PI=["active", "active", stub])))
                report = self.evaluate()
                cell = report.cell("PI")
                self.assertEqual((cell["stub"], cell["counted"]), (1, 2))
                self.assertFalse(report.ok)
                self.assertEqual(report.short, ["PI"])
                text = gate.summary_markdown(report)
                self.assertIn("G2 Bridge quota floor: FAIL", text)
                self.assertIn("PI: 2 counted < floor 3", text)
                self.tearDown()

    def test_extra_stubs_do_not_pad_a_cell(self):
        self.write("test_bridge_redteam", module_source(full(REC=["active"] * 5 + ["skip_stub"] * 10)))
        report = self.evaluate()
        self.assertEqual(self.counted(report, "REC"), 5)
        self.assertEqual(report.short, ["REC"])

    def test_case_counts_only_when_its_test_ends_as_its_status_says(self):
        for kind in ("active_fail", "active_subtest_fail", "active_runtime_skip", "active_crash",
                     "xfail_crash", "xpass"):
            with self.subTest(kind=kind):
                self.setUp()
                self.write("test_bridge_redteam", module_source(full(COST=[kind])))
                report = self.evaluate()
                self.assertEqual(self.counted(report, "COST"), 0)
                self.assertEqual(report.short, ["COST"])
                (case,) = [c for c in report.cases if c.category == "COST"]
                self.assertIn(case.status, ("active", "xfail"))
                self.assertIn(case.name, gate.summary_markdown(report))
                self.tearDown()

    def test_status_comes_from_the_method_not_from_a_list(self):
        # A module-level list naming an active case does not make it a stub or an xfail.
        cases = full()
        source = module_source(cases, header="STUBS = {'test_pi_active_0': 'claimed stub'}")
        self.write("test_bridge_redteam", source)
        report = self.evaluate()
        self.assertTrue(report.ok, gate.summary_markdown(report))
        (case,) = [c for c in report.cases if c.name == "test_pi_active_0"]
        self.assertEqual((case.status, case.outcome), ("active", "passed"))

    def test_tagged_case_without_a_test_is_not_counted(self):
        cases = full(AB=["active", "active"])
        self.write("test_bridge_redteam", module_source(cases, extra_cases={"test_ab_ghost": ("AB", ("R1",))}))
        report = self.evaluate()
        self.assertEqual(report.cell("AB")["missing"], 1)
        self.assertEqual(report.short, ["AB"])
        self.assertIn("no test method with this name", gate.summary_markdown(report))


class FailClosed(Inventory):
    def test_no_inventory_fails_with_every_cell_zero(self):
        report = self.evaluate()
        self.assertFalse(report.ok)
        self.assertEqual(report.short, list(gate.FLOORS))
        self.assertTrue(any(p.startswith("no inventory") for p in report.problems))

    def test_import_error_fails_even_when_floors_are_met(self):
        self.write("test_bridge_redteam", module_source(full()))
        self.write("test_bridge_redteam_broken", "import module_that_does_not_exist\n")
        report = self.evaluate()
        self.assertEqual(report.short, [])
        self.assertFalse(report.ok)
        self.assertTrue(any("import failed" in p for p in report.problems))

    def test_module_without_cases_fails(self):
        self.write("test_bridge_redteam", module_source(full()))
        self.write("test_bridge_redteam_untagged", "import unittest\n\n\nclass T(unittest.TestCase):\n"
                   "    def test_x(self):\n        pass\n")
        report = self.evaluate()
        self.assertFalse(report.ok)
        self.assertTrue(any("no CASES" in p for p in report.problems))

    def test_category_outside_the_bridge_row_fails(self):
        self.write("test_bridge_redteam", module_source(full() + [("ISO", "active")]))
        report = self.evaluate()
        self.assertFalse(report.ok)
        self.assertTrue(any("'ISO'" in p for p in report.problems))

    def test_same_test_name_in_two_classes_is_refused(self):
        source = module_source(full()) + "\n\nclass Again(unittest.TestCase):\n    def test_pi_active_0(self):\n        pass\n"
        self.write("test_bridge_redteam", source)
        report = self.evaluate()
        self.assertFalse(report.ok)
        self.assertEqual(self.counted(report, "PI"), 2)
        self.assertTrue(any("several classes" in p for p in report.problems))

    def test_set_up_class_error_fails_and_counts_nothing_in_that_class(self):
        source = module_source(full()).replace(
            "class Bridge(unittest.TestCase):\n",
            "class Bridge(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        raise OSError('port')\n\n")
        self.write("test_bridge_redteam", source)
        report = self.evaluate()
        self.assertFalse(report.ok)
        self.assertEqual(report.short, list(gate.FLOORS))
        self.assertTrue(any(p.startswith("run error") for p in report.problems))


class PinnedFloors(unittest.TestCase):
    def test_floors_are_the_section_1_bridge_row(self):
        # FACTORY-REDTEAM-AND-RUBRICS-2026-10-24 §1, SyberWork bridge (N3/N4). Lowering one fails here.
        self.assertEqual(gate.FLOORS, {"PI": 3, "AB": 3, "DS": 3, "SIM": 2, "SCH": 4, "COST": 1, "REC": 6})
        self.assertEqual(gate.MIN_TOTAL, 22)
        self.assertEqual(gate.COUNTED, ("active", "xfail"))
        self.assertEqual(gate.INVENTORY, "tests/test_bridge_redteam*.py")


if __name__ == "__main__":
    unittest.main()
