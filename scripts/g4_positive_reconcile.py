"""G4 Positive reconcile: fail closed unless every listed positive control passes.

Factory gate G4 (FACTORY red-team rubric §2, §3) blocks a product that passes the
adversarial gates by refusing everything. On the SyberWork side the positive control
is: one matching destination status lookup (idempotency key, request digest and a
non-empty external id) still yields a verified ``reconciled`` event.

The runner reads the manifest ``tests/g4_positive_reconcile.txt`` and runs each listed
unittest id. It fails when the manifest or a named test file is missing, a required
section is empty, or any listed id is not found, skipped, failed, errored, an expected
failure or an unexpected success. Standard library only.

    python scripts/g4_positive_reconcile.py [--root DIR] [--sections a,b] [--json PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("tests") / "g4_positive_reconcile.txt"
REQUIRED_SECTIONS = ("control", "existing", "twin", "mutant", "source-mutant", "gate")


class GateError(Exception):
    pass


def read_manifest(path: Path) -> dict[str, list[str]]:
    """``[section]`` headers, then one ``tests.<module>.<Class>.<method>`` per line; ``#`` starts a comment."""
    if not path.is_file():
        raise GateError(f"manifest is missing: {path}")
    sections: dict[str, list[str]] = {}
    current = None
    seen = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            if current not in REQUIRED_SECTIONS:
                raise GateError(f"{path.name}:{number}: unknown section [{current}]")
            if current in sections:
                raise GateError(f"{path.name}:{number}: section [{current}] repeated")
            sections[current] = []
        elif current is None:
            raise GateError(f"{path.name}:{number}: id outside a section: {line}")
        elif line in seen:
            raise GateError(f"{path.name}:{number}: duplicate id {line}")
        else:
            seen.add(line)
            sections[current].append(line)
    return sections


class Recorder(unittest.TestResult):
    """Per-id outcome. A failing subtest marks its parent test failed."""

    def __init__(self):
        super().__init__()
        self.outcomes: dict[str, str] = {}
        self.details: dict[str, list[str]] = {}

    def _mark(self, test, outcome, err=None):
        test_id = test.id()
        if outcome != "passed" or self.outcomes.get(test_id) in (None, "passed"):
            self.outcomes[test_id] = outcome
        if err is not None:
            self.details.setdefault(test_id, []).append("".join(traceback.format_exception(*err)))

    def addSuccess(self, test):
        self._mark(test, "passed")

    def addFailure(self, test, err):
        self._mark(test, "failed", err)

    def addError(self, test, err):
        self._mark(test, "error", err)

    def addSkip(self, test, reason):
        self._mark(test, "skipped")
        self.details.setdefault(test.id(), []).append(f"skipped: {reason}")

    def addExpectedFailure(self, test, err):
        self._mark(test, "xfail", err)

    def addUnexpectedSuccess(self, test):
        self._mark(test, "xpass")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            failed = "failed" if issubclass(err[0], test.failureException) else "error"
            self._mark(test, failed, err)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run(root: Path, sections: list[str] | None = None) -> dict:
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    manifest = read_manifest(root / MANIFEST)
    wanted = sections or list(REQUIRED_SECTIONS)
    problems = [f"section [{name}] is empty or absent" for name in wanted if not manifest.get(name)]
    rows = []
    recorder = Recorder()
    for section in wanted:
        for test_id in manifest.get(section, []):
            module = test_id.split(".")
            test_file = root.joinpath(*module[:2]).with_suffix(".py") if len(module) >= 4 else None
            if test_file is None or not test_file.is_file():
                rows.append((section, test_id, "missing"))
                problems.append(f"test file is missing for {test_id}")
                continue
            try:
                tests = list(flatten(unittest.defaultTestLoader.loadTestsFromName(test_id)))
            except Exception as exc:  # an import error or a bad name both mean "not run"
                tests = []
                recorder.details.setdefault(test_id, []).append(f"{type(exc).__name__}: {exc}")
            if [test.id() for test in tests] != [test_id]:
                rows.append((section, test_id, "not found"))
                problems.append(f"{test_id}: not found")
                continue
            tests[0].run(recorder)
            outcome = recorder.outcomes.get(test_id, "not run")
            rows.append((section, test_id, outcome))
            if outcome != "passed":
                problems.append(f"{test_id}: {outcome}")
    return {"rows": rows, "problems": problems, "details": recorder.details}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--sections", help="comma-separated subset of " + ", ".join(REQUIRED_SECTIONS))
    parser.add_argument("--json", type=Path, help="write per-id outcomes here")
    args = parser.parse_args(argv)
    sections = args.sections.split(",") if args.sections else None
    if sections and not set(sections) <= set(REQUIRED_SECTIONS):
        parser.error(f"unknown section in --sections {args.sections}")
    try:
        report = run(args.root, sections)
    except GateError as exc:
        report = {"rows": [], "problems": [str(exc)], "details": {}}
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for section, test_id, outcome in report["rows"]:
        print(f"{outcome:>9}  [{section}] {test_id}")
    for test_id, lines in report["details"].items():
        print(f"\n--- {test_id}\n" + "\n".join(lines))
    passed = sum(1 for row in report["rows"] if row[2] == "passed")
    counts = {}
    for section, _, outcome in report["rows"]:
        if outcome == "passed":
            counts[section] = counts.get(section, 0) + 1
    summary = ", ".join(f"{name} {count}" for name, count in counts.items())
    if report["problems"]:
        print("\nG4 Positive reconcile: FAILED")
        for problem in report["problems"]:
            print("  - " + problem)
        return 1
    print(f"\nG4 Positive reconcile: ok, {passed} listed ids passed ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
