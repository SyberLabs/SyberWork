"""G2 Bridge quota floor: SyberWork bridge red-team cases per category meet the §1 minimums.

Factory gate G2 (FACTORY red-team rubric §3) blocks a merge when red-team cases
per component and category fall below the §1 minimums. Attest's G2 counts the
Attest-side cells. This gate counts the SyberWork bridge cells, all seven of
them: PI 3, AB 3, DS 3, SIM 2, SCH 4, COST 1, REC 6 (22 in total).

Inventory: every module ``tests/test_bridge_redteam*.py``. Each one defines a
module-level ``CASES`` mapping a test method name to ``(category, rubric
lines)``. The gate imports each module, runs exactly the tagged tests once, and
counts a case only when:

* its status counts: ``active`` or ``xfail``. A ``stub`` never counts.
* its test ran and ended the way its status says: ``active`` passed, ``xfail``
  failed an assertion (an executable, observed gap). A case whose test is
  missing, skipped, errored, failed, passed unexpectedly, or whose expected
  failure is a crash rather than an assertion is listed and not counted.

Status comes from the test method, never from a list in the module:
``unittest.expectedFailure`` is ``xfail``; a skip decorator, or a body that is
only a docstring, ``pass``, ``...`` or ``raise NotImplementedError``, is
``stub``; anything else is ``active``.

It fails closed when any cell is under its floor, when no inventory module
exists (every cell is then 0), when a module does not import or has no valid
``CASES``, or when a case names a category outside the bridge row. A green
result means each cell has at least that many executable cases. It says nothing
about whether the cases pass their rubrics (G3), and it is not a coverage
figure.

Usage: PYTHONPATH=. python scripts/g2_bridge_quota_floor.py [repo_root]
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import sys
import textwrap
import unittest
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = "tests/test_bridge_redteam*.py"

# FACTORY-REDTEAM-AND-RUBRICS-2026-10-24 §1, SyberWork bridge row. Pinned here so
# a PR cannot pass G2 by lowering a floor inside a case module.
FLOORS = {"PI": 3, "AB": 3, "DS": 3, "SIM": 2, "SCH": 4, "COST": 1, "REC": 6}
MIN_TOTAL = 22

COUNTED = ("active", "xfail")  # Firstmate counting lock: active + xfail, stub never
EXPECTED_OUTCOME = {"active": "passed", "xfail": "xfailed"}


@dataclass
class Case:
    module: str
    name: str
    category: str
    status: str = "missing"  # active | xfail | stub | missing
    outcome: str = "not run"  # passed | xfailed | xfailed-crash | xpassed | skipped | failed | not run
    detail: str = ""

    @property
    def counted(self) -> bool:
        return self.status in COUNTED and self.outcome == EXPECTED_OUTCOME[self.status]

    @property
    def active_counted(self) -> bool:
        return self.status == "active" and self.outcome == "passed"

    @property
    def why_not(self) -> str:
        if self.status == "missing":
            return "no test method with this name"
        if self.status == "stub":
            return f"stub ({self.detail})" if self.detail else "stub"
        return f"{self.status} expects {EXPECTED_OUTCOME[self.status]}, ended {self.outcome}" + (
            f" ({self.detail})" if self.detail else "")


@dataclass
class Report:
    modules: list[str]
    cases: list[Case]
    problems: list[str] = field(default_factory=list)

    def cell(self, category: str) -> dict[str, int]:
        mine = [c for c in self.cases if c.category == category]
        statuses = Counter(c.status for c in mine)
        return {
            "active": statuses["active"],
            "xfail": statuses["xfail"],
            "stub": statuses["stub"],
            "missing": statuses["missing"],
            "counted": sum(c.counted for c in mine),
            "active_only": sum(c.active_counted for c in mine),
        }

    @property
    def short(self) -> list[str]:
        return [cat for cat, floor in FLOORS.items() if self.cell(cat)["counted"] < floor]

    @property
    def ok(self) -> bool:
        return not self.problems and not self.short


# -- status from the test method ------------------------------------------------------


def _is_placeholder(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return True  # docstring or ``...``
    if isinstance(statement, ast.Raise) and statement.exc is not None:
        exc = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def method_status(cls: type, method) -> tuple[str, str]:
    """(status, detail) for a test method, read from its decorators and its body."""
    if getattr(cls, "__unittest_skip__", False) or getattr(method, "__unittest_skip__", False):
        return "stub", "skip decorator"
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    except (OSError, TypeError, SyntaxError) as exc:
        return "stub", f"source unavailable: {exc}"
    function = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    if all(_is_placeholder(s) for s in function.body):
        return "stub", "placeholder body"
    if getattr(method, "__unittest_expecting_failure__", False):
        return "xfail", ""
    return "active", ""


# -- inventory ------------------------------------------------------------------------


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def find_test_methods(module) -> dict[str, list[type]]:
    """Test method name -> the TestCase classes defined in ``module`` that have it."""
    found: dict[str, list[type]] = {}
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__ or not issubclass(cls, unittest.TestCase):
            continue
        for name in dir(cls):
            if name.startswith("test") and callable(getattr(cls, name)):
                found.setdefault(name, []).append(cls)
    return found


def collect(root: Path, pattern: str = INVENTORY, package: str = "tests") -> tuple[list[str], list[Case], list, list[str]]:
    """Modules, tagged cases, (case, TestCase instance) pairs to run, and problems."""
    modules, cases, runnable, problems = [], [], [], []
    for path in sorted(root.glob(pattern)):
        rel = path.relative_to(root).as_posix()
        modules.append(rel)
        try:
            module = load_module(path, f"{package}.{path.stem}")
        except Exception as exc:  # any import failure is a finding, never a skip
            problems.append(f"{rel}: import failed: {type(exc).__name__}: {exc}")
            continue
        tagged = getattr(module, "CASES", None)
        if not isinstance(tagged, dict) or not tagged:
            problems.append(f"{rel}: no CASES mapping of test name -> (category, rubric lines)")
            continue
        methods = find_test_methods(module)
        for name, entry in tagged.items():
            if not (isinstance(entry, tuple) and entry and isinstance(entry[0], str)):
                problems.append(f"{rel}: CASES[{name!r}] is not (category, rubric lines)")
                continue
            case = Case(rel, name, entry[0])
            cases.append(case)
            if case.category not in FLOORS:
                problems.append(f"{rel}: {name} has category {case.category!r}, not in the bridge row {sorted(FLOORS)}")
            owners = methods.get(name, [])
            if len(owners) > 1:
                problems.append(f"{rel}: {name} is defined in several classes: {', '.join(c.__name__ for c in owners)}")
                continue
            if not owners:
                continue
            cls = owners[0]
            case.status, case.detail = method_status(cls, getattr(cls, name))
            if case.status in COUNTED:
                runnable.append((case, cls(name)))
    if not modules:
        problems.append(f"no inventory: nothing matches {pattern}")
    return modules, cases, runnable, problems


# -- one run --------------------------------------------------------------------------


class _Outcomes(unittest.TextTestResult):
    """Records one outcome per test; a failed subtest or teardown is a failed test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcome: dict[int, tuple[str, str]] = {}
        self.run_errors: list[str] = []

    def _set(self, test, outcome, detail=""):
        if self.outcome.get(id(test), ("",))[0] != "failed":
            self.outcome[id(test)] = (outcome, detail)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "failed", f"{err[0].__name__}")

    def addError(self, test, err):
        super().addError(test, err)
        if isinstance(test, unittest.TestCase):
            self._set(test, "failed", f"error {err[0].__name__}")
        else:  # setUpClass / setUpModule failure: its tests never ran
            self.run_errors.append(f"{test}: {err[0].__name__}: {err[1]}")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.outcome[id(test)] = ("failed", f"subtest {str(subtest)[len(str(test)):].strip()} {err[0].__name__}")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        if issubclass(err[0], test.failureException):
            self._set(test, "xfailed")
        else:  # a crash is not an observed gap
            self._set(test, "xfailed-crash", err[0].__name__)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "xpassed")


def run(runnable: list, stream=None) -> list[str]:
    """Runs the tagged tests once, fills in each case's outcome, and returns run errors."""
    suite = unittest.TestSuite(test for _, test in runnable)
    runner = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=2, resultclass=_Outcomes)
    result = runner.run(suite)
    for case, test in runnable:
        case.outcome, case.detail = result.outcome.get(id(test), ("not run", ""))
    return [f"run error: {e}" for e in result.run_errors]


def evaluate(root: Path = ROOT, pattern: str = INVENTORY, package: str = "tests", stream=None) -> Report:
    problems = []
    if sum(FLOORS.values()) != MIN_TOTAL:
        problems.append(f"floors sum to {sum(FLOORS.values())}, §1 bridge total is {MIN_TOTAL}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    modules, cases, runnable, found = collect(root, pattern, package)
    problems += found
    problems += run(runnable, stream)
    return Report(modules, cases, problems)


# -- output ---------------------------------------------------------------------------


def summary_markdown(report: Report) -> str:
    lines = [
        f"### G2 Bridge quota floor: {'PASS' if report.ok else 'FAIL'}",
        "",
        "Counts `active + xfail`; a stub never counts. A case counts only when its test ran and ended as its "
        "status says (active passed, xfail failed an assertion). Counts of tagged test methods, not coverage.",
        "",
        "| Cat | Floor | active | xfail | stub | Counted (active+xfail) | Active-only | Result |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    total = 0
    for cat, floor in FLOORS.items():
        c = report.cell(cat)
        total += min(c["counted"], floor)
        result = "met" if c["counted"] >= floor else "**SHORT**"
        lines.append(f"| {cat} | {floor} | {c['active']} | {c['xfail']} | {c['stub']} | {c['counted']}"
                     f" | {c['active_only']} | {result} |")
    lines += ["", f"Counted toward floors: {total}/{MIN_TOTAL}.", "",
              "Inventory: " + (", ".join(f"`{m}`" for m in report.modules) or f"none (`{INVENTORY}`)")]
    skipped = [c for c in report.cases if not c.counted]
    if skipped:
        lines += ["", "Not counted:"] + [f"- `{c.module}::{c.name}` ({c.category}): {c.why_not}" for c in skipped]
    failures = report.problems + [
        f"{cat}: {report.cell(cat)['counted']} counted < floor {FLOORS[cat]}" for cat in report.short]
    if failures:
        lines += ["", "Failures:"] + [f"- {p}" for p in failures]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).resolve() if argv else ROOT
    report = evaluate(root)
    text = summary_markdown(report)
    print("\n" + text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
