"""A deterministic, model-free critic for a Build Thread contract.

Declare it as a critic check (a check named ``critic`` or ``critic.<reviewer>``), by the absolute
path of this file in the host's own checkout, so a candidate cannot rewrite its reviewer:

    "critic.rules": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_rules.py"]}

It reads the host's request on stdin (``syberlabs.critic.request``: the candidate, the earlier
check results, the unified diff, the changed paths, the objective, and the contract's acceptance
clauses and mutable scope) and prints a findings document (``spec/finding.schema.json``). Two rules:

- high: a changed path outside the contract's mutable scope. The scope rule already refuses such
  a candidate, so this proves the plumbing rather than adding a judgment.
- medium: a changed source file when no test file changed in the same candidate.

Every finding names what would falsify it. No model is called and nothing is read from disk, so
the same request always yields the same document. A model-backed critic is a later packet; it must
print the same document shape and declare a ``reviewer`` that is not the candidate's provider.
"""

import json
import sys

REVIEWER = {"name": "rules", "revision": "1"}
TEST_MARKERS = ("tests/", "test/", "/test_", "_test.", "spec/")
SOURCE_SUFFIXES = (".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".c", ".h", ".cs", ".mjs")


def _in_scope(scope: dict, path: str) -> bool:
    def matches(entry: str) -> bool:
        return entry == "." or path == entry or path.startswith(entry.rstrip("/") + "/")
    if any(matches(entry) for entry in scope.get("exclude", [])):
        return False
    return any(matches(entry) for entry in scope.get("paths", ["."]))


def is_test(path: str) -> bool:
    marker = "/" + path
    return any(m in marker for m in TEST_MARKERS) or path.split("/")[-1].startswith("test_")


def review(request: dict) -> dict:
    changed = list(request.get("changed_paths", []))
    scope = (request.get("contract") or {}).get("scope") or {"paths": ["."], "exclude": []}
    findings = []
    for path in changed:
        if not _in_scope(scope, path):
            findings.append({"severity": "high", "file": path, "line": None,
                             "claim": "the candidate changes a path outside the contract's mutable scope",
                             "falsifier": "show the path under evolution.scope.paths and not under scope.exclude"})
    sources = [p for p in changed if p.endswith(SOURCE_SUFFIXES) and not is_test(p)]
    if sources and not any(is_test(p) for p in changed):
        for path in sources:
            findings.append({"severity": "medium", "file": path, "line": None,
                             "claim": "a source file changed and no test changed in the same candidate",
                             "falsifier": "a changed file under tests/ (or named test_*) that exercises this change"})
    summary = (f"{len(findings)} finding(s) over {len(changed)} changed path(s)" if findings
               else f"no findings over {len(changed)} changed path(s)")
    return {"version": 1, "reviewer": dict(REVIEWER), "findings": findings, "summary": summary}


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except ValueError:
        print("critic_rules: stdin is not a JSON request", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(review(request), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
