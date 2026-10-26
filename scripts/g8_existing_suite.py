"""FACTORY gate G8: run the existing unit suite and fail closed on what it hides.

    PYTHONPATH=. python scripts/g8_existing_suite.py [--start tests]
        [--allow-skip REASON ...] [--report PATH]

Discovers and runs the same suite as ``python -m unittest discover -s tests``
(start directory ``tests``, pattern ``test*.py``, no top-level directory), then
exits nonzero when any of these holds:

- no test ran;
- a test failed or errored, including a module that did not import;
- a test marked ``expectedFailure`` passed;
- a test or module skipped for a reason that is not allowed.

The last point is what ``unittest`` alone does not check. ``test_cell`` skips
itself when psycopg is missing or PostgreSQL is unreachable, and the property
modules skip when hypothesis is missing, so a broken service or install would
otherwise leave the suite green with those tests unrun. Only skips for tools
the Linux runner may lack (node, npm, global playwright) are allowed by
default; a reason must match exactly.

The counts are written to ``--report`` as JSON and, under GitHub Actions, to
the step summary. They are test counts, not coverage.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import unittest
from collections import Counter
from pathlib import Path

# Exact skip reasons in tests/ for tools that are optional on the runner.
ALLOWED_SKIPS = (
    "node is not installed",
    "npm is not installed",
    "playwright for node is not installed",
    "no npm on PATH; detect_checks wrote no Node check",
)


def run(start: str, allowed: set[str], stream=None) -> dict:
    """Run the suite under ``start`` and return the gate's verdict and counts."""
    suite = unittest.TestLoader().discover(start)
    result = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=0).run(suite)
    skips = Counter(reason for _, reason in result.skipped)
    refused = sorted(reason for reason in skips if reason not in allowed)
    reasons = []
    if result.testsRun == 0:
        reasons.append("no tests ran")
    if result.failures:
        reasons.append(f"{len(result.failures)} failed")
    if result.errors:
        reasons.append(f"{len(result.errors)} errored")
    if result.unexpectedSuccesses:
        reasons.append(f"{len(result.unexpectedSuccesses)} unexpected successes")
    if refused:
        reasons.append(f"{sum(skips[reason] for reason in refused)} skipped for a reason not allowed")
    return {
        "gate": "G8 Existing suite",
        "passed": not reasons,
        "reasons": reasons,
        "python": platform.python_version(),
        "platform": sys.platform,
        "start": start,
        "ran": result.testsRun,
        "failed": len(result.failures),
        "errored": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "skips": [{"reason": reason, "count": count, "allowed": reason in allowed}
                  for reason, count in sorted(skips.items())],
        "failing": sorted(test.id() for test, _ in result.failures + result.errors),
    }


def summary(report: dict) -> str:
    verdict = "passed" if report["passed"] else "failed: " + "; ".join(report["reasons"])
    lines = [
        f"### G8 Existing suite: {verdict}",
        "",
        f"Python {report['python']} on {report['platform']}, `unittest discover -s {report['start']}`. "
        "Counts are tests, not coverage.",
        "",
        "| ran | failed | errored | skipped | expected failures | unexpected successes |",
        "|---|---|---|---|---|---|",
        f"| {report['ran']} | {report['failed']} | {report['errored']} | {report['skipped']} "
        f"| {report['expected_failures']} | {report['unexpected_successes']} |",
    ]
    if report["skips"]:
        lines += ["", "| skip reason | count | allowed |", "|---|---|---|"]
        lines += [f"| {skip['reason']} | {skip['count']} | {'yes' if skip['allowed'] else '**no**'} |"
                  for skip in report["skips"]]
    if report["failing"]:
        lines += ["", "Failing:", ""] + [f"- `{test}`" for test in report["failing"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="tests", help="start directory for discovery (default: tests)")
    parser.add_argument("--allow-skip", action="append", default=[], metavar="REASON",
                        help="also allow this exact skip reason (repeatable)")
    parser.add_argument("--report", type=Path, help="write the counts here as JSON")
    args = parser.parse_args(argv)
    report = run(args.start, set(ALLOWED_SKIPS) | set(args.allow_skip))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    text = summary(report)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
