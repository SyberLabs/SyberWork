"""G3 Bridge adversarial pass: every active SyberWork bridge red-team case passes its R1-R9 checks.

Factory gate G3 (FACTORY red-team rubric §3) blocks a merge when a §1 red-team
case fails the §2 rubrics R1-R9. Attest's G3 runs the Attest-side cases
(Maintain Docs, Reconcile Spec, Kane, Attest-side bridge). This gate runs the
SyberWork side of the bridge row.

Inventory: every module ``tests/test_bridge_redteam*.py``, found the same way as
the G2 counter (``scripts/g2_bridge_quota_floor.py``). Each module defines a
module-level ``CASES`` mapping a test method name to ``(category, rubric
lines)``. Positive controls are the test methods of a ``PositiveControl`` class
in the module, plus any name in a module-level ``POSITIVE_CONTROLS``. The rubric
expectations are the assertions already in those tests; this gate adds none.

Status comes from the test method, never from a list in the module (the same
rule as G2): ``unittest.expectedFailure`` is ``xfail``; a skip decorator, or a
body that is only a docstring, ``pass``, ``...`` or ``raise
NotImplementedError``, is ``stub``; anything else is ``active``.

The gate runs every active and xfail case and every positive control once, and
fails closed when

* an active case is missing, skipped at run time, fails or errors (including in
  a subtest or in ``setUpClass``). A skip is never a G3 pass;
* an xfail case passes unexpectedly (its hook landed: remove the decorator so it
  runs as active) or fails with a crash rather than an assertion (that is not an
  observed gap);
* a stub has no reason (skip reason or docstring);
* a positive control is missing, not active, or does not pass, or there is no
  positive control at all, so "refuse everything" cannot pass;
* there is no active adversarial case, so G3 cannot pass on nothing;
* no inventory module exists, a module does not import or has no valid
  ``CASES``, a case names a category outside the bridge row, its rubric lines
  are not a subset of R1-R9 or omit R1 (§2: R1 applies to every adversarial
  case), or a name is defined in more than one class.

xfail and stub cases are listed as open gaps. They do not fail the gate and they
never count as G3 passes: G3 over every §1 case holds only when no gap is left.
A green result covers only the active cases in the inventory at that commit. It
is not a coverage figure.

``--twins BASELINE.json`` runs the gate twice more on copies of the tree whose
destination match is mutated to refuse every lookup and to accept every lookup.
Each twin must turn the gate red with failures the baseline did not have: a
positive control for refuse-everything, an active adversarial case for
accept-everything. That proves the suite would catch either blanket answer.

Usage: PYTHONPATH=. python scripts/g3_bridge_adversarial_pass.py [--root DIR] [--report FILE]
       PYTHONPATH=. python scripts/g3_bridge_adversarial_pass.py --twins BASELINE.json
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = "tests/test_bridge_redteam*.py"

# FACTORY-REDTEAM-AND-RUBRICS-2026-10-24 §1, SyberWork bridge row, and the §2 rubric lines.
CATEGORIES = ("PI", "AB", "DS", "SIM", "SCH", "COST", "REC")
CONTROL = "CONTROL"
RUBRICS = tuple(f"R{n}" for n in range(1, 10))
# §2 "Applies to", for a bridge case. Only R1 is enforced on the tags; the rest are reported.
APPLICABLE = {"R1": CATEGORIES, "R6": ("PI",), "R7": ("COST",), "R8": CATEGORIES, "R9": CATEGORIES}
ENFORCED_TAG = "R1"
CONTROL_CLASS, CONTROL_LIST = "PositiveControl", "POSITIVE_CONTROLS"

EXPECTED_OUTCOME = {"active": "passed", "xfail": "xfailed"}


@dataclass
class Case:
    module: str
    name: str
    category: str
    rubrics: tuple[str, ...] = ()
    status: str = "missing"  # active | xfail | stub | missing
    outcome: str = "not run"  # passed | xfailed | xfailed-crash | xpassed | skipped | failed | not run
    detail: str = ""

    @property
    def adversarial(self) -> bool:
        return self.category != CONTROL

    @property
    def key(self) -> str:
        return f"{self.module}::{self.name}"

    @property
    def violation(self) -> str:
        """Why this case fails G3, or "" when it does not."""
        if self.status == "missing":
            return "no test method with this name"
        if not self.adversarial and self.status != "active":
            return f"positive control is {self.status}" + (f" ({self.detail})" if self.detail else "")
        if self.status == "stub":
            return "" if self.detail else "stub with no reason (give a skip reason or a docstring)"
        if self.outcome == EXPECTED_OUTCOME[self.status]:
            return ""
        if self.outcome == "xpassed":
            return "xfail passed unexpectedly: the hook may have landed, remove expectedFailure so it runs as active"
        if self.outcome == "xfailed-crash":
            return f"xfail crashed ({self.detail}) instead of failing an assertion: not an observed gap"
        return f"{self.status} expects {EXPECTED_OUTCOME[self.status]}, ended {self.outcome}" + (
            f" ({self.detail})" if self.detail else "")


@dataclass
class Report:
    modules: list[str]
    cases: list[Case]
    problems: list[str] = field(default_factory=list)
    untagged: list[str] = field(default_factory=list)

    @property
    def violations(self) -> list[Case]:
        return [c for c in self.cases if c.violation]

    @property
    def gaps(self) -> list[Case]:
        return [c for c in self.cases if c.status in ("xfail", "stub") and c.adversarial and not c.violation]

    @property
    def failures(self) -> list[str]:
        found = list(self.problems)
        found += [f"{c.key} ({c.category}, {','.join(c.rubrics) or '-'}): {c.violation}" for c in self.violations]
        active = [c for c in self.cases if c.status == "active"]
        if not any(c.adversarial for c in active):
            found.append("no active adversarial case in the inventory; G3 cannot pass on nothing")
        if not any(not c.adversarial for c in active):
            found.append("no active positive control in the inventory; refuse-everything would pass")
        return found

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_json(self) -> dict:
        return {"ok": self.ok, "modules": self.modules, "failures": self.failures,
                "cases": [asdict(c) | {"violation": c.violation} for c in self.cases]}


# -- status from the test method (same rule as G2) ------------------------------------


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
    """(status, detail) for a test method. For a stub, detail is its reason ("" if it gives none)."""
    if getattr(cls, "__unittest_skip__", False) or getattr(method, "__unittest_skip__", False):
        why = getattr(method, "__unittest_skip_why__", "") or getattr(cls, "__unittest_skip_why__", "")
        return "stub", str(why).strip()
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    except (OSError, TypeError, SyntaxError) as exc:
        return "stub", f"source unavailable: {exc}"
    function = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    if all(_is_placeholder(s) for s in function.body):
        return "stub", (ast.get_docstring(function) or "").strip()
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


def positive_controls(module, methods: dict[str, list[type]]) -> list[str]:
    """Positive-control test names: a ``PositiveControl`` class's tests and any ``POSITIVE_CONTROLS`` name."""
    names = [n for n, owners in methods.items() if any(c.__name__ == CONTROL_CLASS for c in owners)]
    listed = getattr(module, CONTROL_LIST, ())
    if isinstance(listed, (list, tuple)):
        names += [n for n in listed if isinstance(n, str) and n not in names]
    return sorted(names)


def _tag_problems(rel: str, name: str, category: str, rubrics) -> list[str]:
    found = []
    if category not in CATEGORIES:
        found.append(f"{rel}: {name} has category {category!r}, not in the bridge row {list(CATEGORIES)}")
    if not (isinstance(rubrics, (tuple, list)) and rubrics and set(rubrics) <= set(RUBRICS)):
        found.append(f"{rel}: {name} rubric lines {rubrics!r} must be a non-empty subset of R1-R9")
    elif ENFORCED_TAG not in rubrics:
        found.append(f"{rel}: {name} does not tag {ENFORCED_TAG} (fail closed applies to every adversarial case)")
    return found


def collect(root: Path, pattern: str = INVENTORY, package: str = "tests"):
    """Modules, cases (adversarial and controls), (case, TestCase) pairs to run, problems, untagged tests."""
    modules, cases, runnable, problems, untagged = [], [], [], [], []
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
        entries = []
        for name, entry in tagged.items():
            if not (isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], str)):
                problems.append(f"{rel}: CASES[{name!r}] is not (category, rubric lines)")
                continue
            category, rubrics = entry
            problems += _tag_problems(rel, name, category, rubrics)
            entries.append(Case(rel, name, category, tuple(rubrics) if isinstance(rubrics, (tuple, list)) else ()))
        for name in positive_controls(module, methods):
            if name in tagged:
                problems.append(f"{rel}: {name} is both a tagged case and a positive control")
                continue
            entries.append(Case(rel, name, CONTROL))
        known = {c.name for c in entries}
        untagged += [f"{rel}::{n}" for n in sorted(methods) if n not in known]
        for case in entries:
            cases.append(case)
            owners = methods.get(case.name, [])
            if len(owners) > 1:
                problems.append(f"{rel}: {case.name} is defined in several classes: "
                                f"{', '.join(c.__name__ for c in owners)}")
                continue
            if not owners:
                continue
            cls = owners[0]
            case.status, case.detail = method_status(cls, getattr(cls, case.name))
            if case.status in EXPECTED_OUTCOME:
                runnable.append((case, cls(case.name)))
    if not modules:
        problems.append(f"no inventory: nothing matches {pattern}")
    return modules, cases, runnable, problems, untagged


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
    """Runs the cases once, fills in each case's outcome, and returns run errors."""
    suite = unittest.TestSuite(test for _, test in runnable)
    runner = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=2, resultclass=_Outcomes)
    result = runner.run(suite)
    for case, test in runnable:
        case.outcome, outcome_detail = result.outcome.get(id(test), ("not run", ""))
        case.detail = case.detail or outcome_detail
    return [f"run error: {e}" for e in result.run_errors]


def evaluate(root: Path = ROOT, pattern: str = INVENTORY, package: str = "tests", stream=None) -> Report:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    modules, cases, runnable, problems, untagged = collect(root, pattern, package)
    problems += run(runnable, stream)
    return Report(modules, cases, problems, untagged)


# -- output ---------------------------------------------------------------------------


def summary_markdown(report: Report) -> str:
    lines = [
        f"### G3 Bridge adversarial pass: {'PASS (active cases)' if report.ok else 'FAIL'}",
        "",
        "Runs every active SyberWork bridge red-team case and positive control. An active case passes only when "
        "every assertion in its test holds; xfail and stub cases are open gaps, never G3 passes. "
        "Counts of tagged test methods, not coverage.",
        "",
        "| Cat | Active passed | Active not passing | xfail (observed gap) | stub (open gap) | Other violations |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cat in (*CATEGORIES, CONTROL):
        mine = [c for c in report.cases if c.category == cat]
        n = Counter()
        for c in mine:
            if c.status == "active":
                n["fail" if c.violation else "pass"] += 1
            elif c.violation:
                n["other"] += 1
            else:
                n[c.status] += 1
        if cat == CONTROL:
            cat = "positive control"
        lines.append(f"| {cat} | {n['pass']} | {n['fail']} | {n['xfail']} | {n['stub']} | {n['other']} |")
    passing = [c for c in report.cases if c.adversarial and c.status == "active" and not c.violation]
    tags = Counter(r for c in passing for r in c.rubrics)
    lines += ["", "Rubric tags on passing active adversarial cases: "
              + (", ".join(f"{r} {tags[r]}" for r in RUBRICS if tags[r]) or "none")]
    untagged_rubrics = []
    for rubric, applies in APPLICABLE.items():
        if rubric == ENFORCED_TAG:
            continue
        miss = sum(1 for c in passing if c.category in applies and rubric not in c.rubrics)
        if miss:
            untagged_rubrics.append(f"{rubric} {miss}")
    if untagged_rubrics:
        lines.append("Applicable per §2 but not tagged (reported, not enforced; the test asserts what it asserts): "
                     + ", ".join(untagged_rubrics))
    lines += ["", "Inventory: " + (", ".join(f"`{m}`" for m in report.modules) or f"none (`{INVENTORY}`)")]
    if report.untagged:
        lines.append(f"Untagged test methods not run by G3: {len(report.untagged)}")
    if report.gaps:
        lines += ["", "Open gaps (not G3 passes; G3 over every §1 case is not met while any remain):"]
        lines += [f"- `{c.key}` ({c.category}, {c.status}" + (f": {c.detail}" if c.status == "stub" else "") + ")"
                  for c in report.gaps]
    if report.failures:
        lines += ["", "Failures:"] + [f"- {p}" for p in report.failures]
    lines += ["", "A pass covers only the active cases above at this commit. Attest's G3 is a separate gate."]
    return "\n".join(lines) + "\n"


def emit(text: str) -> None:
    print("\n" + text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


# -- refuse-everything / accept-everything twins --------------------------------------

# The destination match in ``Work.reconcile``. Each twin replaces this one line in a copy of the tree.
MATCH_FILE = "syberwork/core.py"
MATCH_LINE = "            if matched:\n                status, reason = \"verified\", \"destination_record_matched\"\n"
TWINS = {
    # name: (replacement for ``if matched:``, the kind of case that must newly fail)
    "refuse-everything": ("            if False:\n", CONTROL),
    "accept-everything": ("            if True:\n", "adversarial"),
}


def mutate(tree: Path, replacement: str) -> str:
    """Applies one twin to ``tree``; returns "" or why it could not."""
    target = tree / MATCH_FILE
    source = target.read_text(encoding="utf-8") if target.is_file() else ""
    if source.count(MATCH_LINE) != 1:
        return f"{MATCH_FILE}: the destination match line was found {source.count(MATCH_LINE)} times, expected 1"
    rest = MATCH_LINE.split("\n", 1)[1]
    target.write_text(source.replace(MATCH_LINE, replacement + rest), encoding="utf-8")
    return ""


def newly_failing(baseline: dict, twin: dict, kind: str) -> list[str]:
    before = {c["module"] + "::" + c["name"] for c in baseline["cases"] if c["violation"]}
    return sorted(c["module"] + "::" + c["name"] for c in twin["cases"]
                  if c["violation"] and c["status"] == "active"
                  and (c["category"] == CONTROL) == (kind == CONTROL)
                  and c["module"] + "::" + c["name"] not in before)


def run_twins(root: Path, baseline_path: Path) -> tuple[bool, str]:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["cases"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"### G3 twins: FAIL\n\nNo usable baseline report at `{baseline_path}` ({exc}).\n"
    if not any(c.get("status") == "active" for c in baseline["cases"]):
        return False, "### G3 twins: FAIL\n\nThe baseline has no active case, so there is no suite to mutate against.\n"
    lines = ["| Twin | Gate exit | Must newly fail | Newly failing | Result |", "|---|---:|---|---:|---|"]
    ok, details = True, []
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT")}
    for name, (replacement, kind) in TWINS.items():
        label = "positive control" if kind == CONTROL else "active adversarial case"
        with tempfile.TemporaryDirectory(prefix=f"g3-{name}-") as scratch:
            tree = Path(scratch) / "tree"
            shutil.copytree(root, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            if why := mutate(tree, replacement):
                ok = False
                lines.append(f"| {name} | - | {label} | - | **FAIL**: {why} |")
                continue
            out = Path(scratch) / "report.json"
            proc = subprocess.run(
                [sys.executable, str(tree / "scripts" / Path(__file__).name), "--root", str(tree), "--report", str(out)],
                cwd=tree, env=env | {"PYTHONPATH": str(tree)}, capture_output=True, text=True, timeout=600)
            try:
                twin = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                ok = False
                lines.append(f"| {name} | {proc.returncode} | {label} | - | **FAIL**: the gate wrote no report |")
                details += [f"- {name} output tail:", "```", (proc.stdout + proc.stderr)[-4000:], "```"]
                continue
        caught = newly_failing(baseline, twin, kind)
        good = proc.returncode != 0 and bool(caught)
        ok &= good
        lines.append(f"| {name} | {proc.returncode} | {label} | {len(caught)} | "
                     f"{'caught' if good else '**NOT CAUGHT**: the suite would pass this blanket answer'} |")
        details += [f"- {name} newly failing: `{c}`" for c in caught]
    lines += ["", f"Mutated line: `{MATCH_LINE.splitlines()[0].strip()}` in `{MATCH_FILE}` (`Work.reconcile`), "
              "in a throwaway copy. Product code in the checkout is unchanged."]
    if details:
        lines += [""] + details
    return ok, "\n".join([f"### G3 twins (refuse-everything, accept-everything): {'PASS' if ok else 'FAIL'}", "",
                           *lines]) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--report", type=Path, help="also write the result as JSON")
    parser.add_argument("--twins", type=Path, metavar="BASELINE", help="run the twins against this baseline report")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.twins:
        ok, text = run_twins(root, args.twins)
        emit(text)
        return 0 if ok else 1
    report = evaluate(root)
    if args.report:
        args.report.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    emit(summary_markdown(report))
    for failure in report.failures:
        print(f"::error title=G3 Bridge adversarial pass::{failure}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
