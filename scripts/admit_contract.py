"""Write the ``repo-change`` contract that admits one tree through a Build Thread.

    python scripts/admit_contract.py [--home .syberlabs] [--check NAME=COMMAND ...]
        [--required NAME ...] [--scope PATH ...] [--max-files N] [--max-diff-bytes N]
        [--timeout SECONDS]

The contract has one action, ``accept_change``: no push, no pull request, no
command. Its checks are the repository's own, named here. ``{python}`` in a
command stands for the interpreter running this script, by absolute path, so a
check never depends on a bare ``python`` on PATH (checks run with a scrubbed
environment, and on Windows a bare name can be an App Execution Alias). Without
``--check`` the checks are SyberWork's own: unit tests and conformance. The file
is written as the next unused version of ``repo-change`` under
``<home>/contracts`` and its name (``repo-change.vN``) is printed, for
``syberlabs start --contract``.

Standard library only. It does not import ``syberlabs``, so another repository
can copy it next to its own workflow; the kit validates the contract on install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
VERSION = re.compile(r"^repo-change\.v(\d+)\.json$")
DEFAULT_CHECKS = (
    "tests={python} -m unittest discover -s tests -q",
    "conformance={python} -m conformance.run",
)


def parse_check(text: str) -> tuple[str, list[str]]:
    name, separator, command = text.partition("=")
    name = name.strip()
    if not separator or not NAME.match(name):
        raise SystemExit(f"admit_contract: a check is NAME=COMMAND with a lowercase name, not {text!r}")
    argv = shlex.split(command, posix=(os.name != "nt"))
    if not argv:
        raise SystemExit(f"admit_contract: check {name} has an empty command")
    return name, [sys.executable if part == "{python}" else part for part in argv]


def next_version(contracts: Path) -> int:
    versions = []
    for path in contracts.glob("repo-change.v*.json"):
        found = VERSION.match(path.name)
        if found:
            versions.append(int(found.group(1)))
    return max(versions, default=0) + 1


def contract(version: int, checks: dict[str, list[str]], required: list[str], scope: list[str],
             max_files: int, max_diff_bytes: int, timeout: int) -> dict:
    return {
        "id": "repo-change",
        "version": version,
        "title": "Admit one tree: host-run checks, journaled; accepting moves only the thread's branch",
        "inputs": {"objective": "string", "act_class": "string"},
        "actions": {"accept_change": {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
        "evolution": {
            "scope": {"paths": scope, "exclude": [], "max_files": max_files, "max_diff_bytes": max_diff_bytes},
            "operators": ["patch"],
            "budget": {"max_candidates": 4, "max_evaluations": 4, "max_seconds": 3600},
            "evaluation": {
                "checks": {name: {"argv": argv, "timeout_seconds": timeout} for name, argv in checks.items()},
                "required": required,
                "max_age_seconds": 10800,
                "max_output_bytes": 1_048_576,
            },
            "promotion": {"action": "accept_change", "roles": ["developer"]},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="admit_contract.py",
                                     description="Write the repo-change contract that admits one tree through a Build Thread.")
    parser.add_argument("--home", default=".syberlabs", help="the kit's home; the contract goes under <home>/contracts")
    parser.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND",
                        help="a check to run on the candidate's tree; {python} is this interpreter (repeatable)")
    parser.add_argument("--required", nargs="+", metavar="NAME", help="checks that must pass (default: all of them)")
    parser.add_argument("--scope", nargs="+", default=["."], metavar="PATH", help="paths a candidate may change (default: .)")
    parser.add_argument("--max-files", type=int, default=1000, help="most files one candidate may change (default: 1000)")
    parser.add_argument("--max-diff-bytes", type=int, default=10_000_000, help="largest diff one candidate may carry (default: 10000000)")
    parser.add_argument("--timeout", type=int, default=3000, help="seconds each check may run (default: 3000)")
    args = parser.parse_args(argv)

    if not sys.executable:
        raise SystemExit("admit_contract: this interpreter reports no executable path; run it from one that does")
    checks: dict[str, list[str]] = {}
    for text in args.check or DEFAULT_CHECKS:
        name, command = parse_check(text)
        if name in checks:
            raise SystemExit(f"admit_contract: check {name} is named twice")
        checks[name] = command
    required = list(dict.fromkeys(args.required)) if args.required else sorted(checks)
    missing = [name for name in required if name not in checks]
    if missing:
        raise SystemExit("admit_contract: required checks without a command: " + ", ".join(missing))
    if not 1 <= args.max_files <= 1000 or not 1 <= args.max_diff_bytes <= 10_000_000 or not 1 <= args.timeout <= 3600:
        raise SystemExit("admit_contract: --max-files is 1 to 1000, --max-diff-bytes 1 to 10000000, --timeout 1 to 3600")

    contracts = Path(args.home) / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    version = next_version(contracts)
    document = contract(version, checks, required, args.scope, args.max_files, args.max_diff_bytes, args.timeout)
    path = contracts / f"repo-change.v{version}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    for name, command in checks.items():
        print(f"check {name}: {' '.join(command)}", file=sys.stderr)
    print(f"repo-change.v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
