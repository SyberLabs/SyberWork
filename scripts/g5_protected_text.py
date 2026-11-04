#!/usr/bin/env python3
"""G5 Protected-text diff: fail when a change alters protected requirement text.

FACTORY red-team rubric section 3, gate G5, rubric R2, for SyberWork. Modeled on
attest #33 (``scripts/g5-protected-text.py``) and kept to the same hook and the
same decision format, so a rule or a decision means the same thing in both
repositories:

- Every ``attest.policy.yaml`` ``match`` rule protects each paragraph of a
  ``*.md`` file in the same directory that contains the match (case-folded,
  whitespace-folded). This is the file Attest's Maintain Docs reads beside a doc
  (``attest.policy.load_policy``), so the same rule also stops Attest from
  repairing that paragraph. The rule itself is protected too: dropping it
  removes the paragraph's protection and counts as a change. ``claim:`` rules
  name compiler claim ids, which cannot be resolved without compiling, and are
  not covered, as in attest.

In SyberWork the hook is ``docs/attest.policy.yaml``. Its rules protect the
bridge's reconciliation boundary (``docs/RECONCILIATION.md``,
``docs/ATTEST_RECONCILIATION_MAP.md``) and the console's token and rendering
rules (``docs/BUILD_THREAD.md`` inspector, ``docs/DASHBOARD.md``).

Each protected unit present at base must be byte-identical at head. The only
exception is an approved ``attest.change-decision/v0`` in
``docs/change-decisions/*.json`` that is already on the base revision and binds
the unit's path and exact before/after SHA-256. A decision added by the same
change does not count, so a change cannot approve itself.

The policy is read with the standard library: the reader accepts the subset
attest's hook uses (a ``protected:`` list of quoted ``match:``/``claim:``
items, and comments) and refuses anything else, so a policy it cannot read
fails the gate instead of protecting nothing.

One difference from attest: attest's hooks were on its ``main`` before its G5
landed, SyberWork's are added by the change that adds this gate. When the base
revision does not contain this script, the change is installing G5: there is
nothing at base to compare, and the gate passes only if the head protects at
least one unit. Once this script is on the base, a base with no protected text
fails (exit 2) exactly as in attest.

Exit codes: 0 pass, 1 protected text changed without a decision, 2 the gate
could not run (bad revision, unreadable policy, no protected text at base).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

GATE = "G5 Protected-text diff"
GATE_SCRIPT = "scripts/g5_protected_text.py"
POLICY_NAME = "attest.policy.yaml"
DECISION_DIR = "docs/change-decisions"
DECISION_SCHEMA = "attest.change-decision/v0"


class GateError(RuntimeError):
    """The gate cannot decide; callers must treat this as a failure."""


class GitTree:
    def __init__(self, rev: str, repo: Path = Path(".")):
        self.repo = repo
        self.rev = self._git("rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()

    def _git(self, *args: str) -> bytes:
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=False)
        if result.returncode:
            raise GateError(result.stderr.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
        return result.stdout

    def files(self) -> list[str]:
        return self._git("ls-tree", "-r", "-z", "--name-only", self.rev).decode().split("\0")[:-1]

    def read(self, path: str) -> bytes:
        return self._git("show", f"{self.rev}:{path}")


class DirTree:
    def __init__(self, root: Path):
        self.root = root

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())

    def read(self, path: str) -> bytes:
        return (self.root / path).read_bytes()


@dataclass(frozen=True)
class Unit:
    """One span of protected bytes and the hook that protects it."""

    source: str
    path: str
    key: str
    text: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text).hexdigest()

    @property
    def ident(self) -> tuple[str, str, str, str]:
        return (self.source, self.path, self.key, self.sha256)


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def _paragraphs(data: bytes) -> list[bytes]:
    blocks: list[bytes] = []
    current: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(b"".join(current))
            current = []
    if current:
        blocks.append(b"".join(current))
    return blocks


def _quoted(value: str) -> tuple[str, str] | None:
    """Split a YAML double- or single-quoted scalar from what follows it."""
    if value.startswith('"'):
        try:
            text, end = json.JSONDecoder().raw_decode(value)
        except json.JSONDecodeError:
            return None
        return (text, value[end:]) if isinstance(text, str) else None
    if value.startswith("'"):
        out, i = [], 1
        while i < len(value):
            if value[i] == "'":
                if value[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), value[i + 1:]
            out.append(value[i])
            i += 1
    return None


def parse_policy(text: str, path: str) -> list[dict]:
    """Read the protected list of an attest policy, or raise GateError.

    Accepts comments, blank lines, ``protected: []`` and ``protected:`` followed
    by ``- match: "..."`` or ``- claim: "..."`` items with quoted values. An empty
    document has no rules, as ``yaml.safe_load(...) or {}`` does in attest; a
    ``protected:`` key with no items is null in YAML and refused there, so here too.
    """
    lines = [(n, line.rstrip()) for n, line in enumerate(text.splitlines(), 1)]
    lines = [(n, line) for n, line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return []
    n, first = lines[0]
    head = first.split(" #", 1)[0].rstrip()
    if head == "protected: []" and len(lines) == 1:
        return []
    if head != "protected:":
        raise GateError(f"{path}:{n}: policy must contain a protected list")
    if len(lines) == 1:
        raise GateError(f"{path}:{n}: policy must contain a protected list")
    rules: list[dict] = []
    indent = None
    for n, line in lines[1:]:
        item = line.lstrip()
        if indent is None:
            indent = len(line) - len(item)
        if not item.startswith("- ") or len(line) - len(item) != indent:
            raise GateError(f"{path}:{n}: unsupported policy line; expected '- match: \"...\"' at one indentation")
        key, sep, value = item[2:].partition(":")
        parsed = _quoted(value.strip()) if sep and key in ("match", "claim") else None
        rest = parsed[1].strip() if parsed else None
        if parsed is None or (rest and not rest.startswith("#")):
            raise GateError(f"{path}:{n}: unsupported policy line; the G5 reader accepts quoted match/claim items only")
        rules.append({key: parsed[0]})
    return rules


def policy_units(tree, files: list[str]) -> list[Unit]:
    units: list[Unit] = []
    for policy_path in files:
        if PurePosixPath(policy_path).name != POLICY_NAME:
            continue
        try:
            text = tree.read(policy_path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GateError(f"{policy_path}: unreadable policy: {exc}") from exc
        rules = parse_policy(text, policy_path)
        folder = PurePosixPath(policy_path).parent
        docs = [f for f in files if PurePosixPath(f).parent == folder and f.endswith(".md")]
        for rule in rules:
            match = rule.get("match")
            if not isinstance(match, str) or not match.strip():
                continue
            needle = _fold(match)
            for doc in docs:
                for block in _paragraphs(tree.read(doc)):
                    if needle in _fold(block.decode("utf-8", errors="replace")):
                        units.append(Unit(f"policy:{policy_path}", doc, f"match:{match}", block))
    return units


def protected_units(tree) -> list[Unit]:
    return policy_units(tree, tree.files())


def approved_decisions(tree) -> tuple[list[dict], list[str]]:
    """Return valid approved decisions and a note for every ignored file."""
    decisions: list[dict] = []
    ignored: list[str] = []
    for path in tree.files():
        if PurePosixPath(path).parent != PurePosixPath(DECISION_DIR) or not path.endswith(".json"):
            continue
        try:
            data = json.loads(tree.read(path))
        except (UnicodeDecodeError, json.JSONDecodeError):
            ignored.append(f"{path}: not valid JSON")
            continue
        problem = _decision_problem(data)
        if problem:
            ignored.append(f"{path}: {problem}")
        else:
            decisions.append({**data, "_file": path})
    return decisions, ignored


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _decision_problem(data: object) -> str | None:
    if not isinstance(data, dict):
        return "not a JSON object"
    if data.get("schema") != DECISION_SCHEMA:
        return f"schema must be {DECISION_SCHEMA}"
    if data.get("status") != "approved":
        return "status is not approved"
    for field in ("decision_id", "approved_by", "path", "reason"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            return f"{field} is required"
    if not _is_sha(data.get("before_sha256")):
        return "before_sha256 must be a lowercase SHA-256"
    if data.get("after_sha256") is not None and not _is_sha(data["after_sha256"]):
        return "after_sha256 must be a lowercase SHA-256 or null"
    return None


def check(base, head) -> dict:
    """Compare protected units at base and head; raise GateError if base protects nothing."""
    base_units = protected_units(base)
    head_units = protected_units(head)
    bootstrap = False
    if not base_units:
        if GATE_SCRIPT in base.files():
            raise GateError("no protected text found at base; refusing to pass a vacuous gate")
        if not head_units:
            raise GateError("no protected text found at base or head; refusing to pass a vacuous gate")
        bootstrap = True
    decisions, ignored = approved_decisions(base)
    head_only_decisions = approved_decisions(head)[0]
    head_texts = _head_digests(head, {u.path for u in base_units})

    remaining = Counter(u.ident for u in head_units)
    violations: list[dict] = []
    approved: list[dict] = []
    for unit in base_units:
        if remaining[unit.ident] > 0:
            remaining[unit.ident] -= 1
            continue
        finding = {"source": unit.source, "path": unit.path, "key": unit.key, "before_sha256": unit.sha256}
        decision = _decision_for(unit, decisions, head_texts.get(unit.path, set()))
        if decision:
            approved.append({**finding, "decision_id": decision["decision_id"], "decision_file": decision["_file"]})
            continue
        head_only = [d for d in head_only_decisions if d["path"] == unit.path and d["before_sha256"] == unit.sha256]
        finding["reason"] = (
            "decision exists only in this change; land it on the base branch first"
            if head_only
            else "no approved change decision on the base branch binds this before/after text"
        )
        violations.append(finding)
    return {
        "gate": GATE,
        "bootstrap": bootstrap,
        "base_units": len(base_units),
        "head_units": len(head_units),
        "head_unit_list": [{"path": u.path, "key": u.key, "sha256": u.sha256} for u in head_units],
        "violations": violations,
        "approved_changes": approved,
        "ignored_decisions": ignored,
    }


def _head_digests(head, paths: set[str]) -> dict[str, set[str | None]]:
    """Digest every candidate replacement span at head; ``None`` means the text may be gone."""
    files = set(head.files())
    result: dict[str, set[str | None]] = {}
    for path in paths:
        digests: set[str | None] = {None}
        if path in files:
            digests |= {hashlib.sha256(block).hexdigest() for block in _paragraphs(head.read(path))}
        result[path] = digests
    return result


def _decision_for(unit: Unit, decisions: list[dict], head_digests: set[str | None]) -> dict | None:
    for decision in decisions:
        if decision["path"] != unit.path or decision["before_sha256"] != unit.sha256:
            continue
        after = decision.get("after_sha256")
        if after is None or after in head_digests:
            return decision
    return None


def summary(report: dict) -> str:
    lines = [f"## {GATE}", ""]
    if report["bootstrap"]:
        lines += [
            f"- BOOTSTRAP: the base has no `{GATE_SCRIPT}`, so this change installs G5 and nothing at base was compared.",
            "",
        ]
    lines += [
        f"- Protected units at base: {report['base_units']}",
        f"- Protected units at head: {report['head_units']}",
        f"- Changed with an approved decision: {len(report['approved_changes'])}",
        f"- Changed without an approved decision: {len(report['violations'])}",
        "",
    ]
    for item in report["violations"]:
        lines.append(f"- FAIL `{item['path']}` {item['key']} (`{item['before_sha256'][:12]}`): {item['reason']}")
    for item in report["approved_changes"]:
        lines.append(f"- approved `{item['path']}` {item['key']} by `{item['decision_id']}` ({item['decision_file']})")
    for note in report["ignored_decisions"]:
        lines.append(f"- ignored decision {note}")
    for unit in report["head_unit_list"]:
        lines.append(f"- protected at head: `{unit['path']}` {unit['key']} (`{unit['sha256'][:12]}`)")
    if lines[-1]:
        lines.append("")
    lines.append("These are counts of protected spans compared, not a coverage figure.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="base revision (e.g. the PR merge commit's first parent)")
    parser.add_argument("--head", default="HEAD", help="head revision (default HEAD)")
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        report = check(GitTree(args.base, args.repo), GitTree(args.head, args.repo))
    except GateError as exc:
        print(f"{GATE}: cannot run: {exc}", file=sys.stderr)
        return 2
    text = summary(report)
    print(text, end="")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
