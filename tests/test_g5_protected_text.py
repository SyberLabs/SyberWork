"""Controls for gate G5 Protected-text diff (scripts/g5_protected_text.py).

Every tree here is built from the frozen copies in tests/fixtures/g5_protected_text,
never from the live docs/, so a change that edits protected text fails in the
gate's diff step and not here. The fixture file names keep them out of the live
scan: only a file named attest.policy.yaml is a hook.
"""

import contextlib
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "g5_protected_text"
DOCS = ("RECONCILIATION.md", "ATTEST_RECONCILIATION_MAP.md", "BUILD_THREAD.md", "DASHBOARD.md")

_spec = importlib.util.spec_from_file_location("g5_protected_text", ROOT / "scripts" / "g5_protected_text.py")
g5 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = g5  # dataclasses look the module up while the class is built
_spec.loader.exec_module(g5)

# One edit per protected rule: each changes words inside exactly one protected paragraph.
CALLER_FIELDS = (b"Caller-provided `success` or `evidence` fields are rejected.",
                 b"Caller-provided `success` or `evidence` fields are accepted when signed.")
NOT_FOUND = (b"A 404, transport failure,", b"A transport failure,")
MAPPING_ROW = (b"Never `effect_succeeded`, verified `reconciled`, or acceptance.", b"")
CANNOT_EMIT = (b"An Attest artifact cannot emit", b"An Attest artifact should not emit")
TRANSLATE = (b"Do not translate an", b"Avoid translating an")
INSPECTOR_TOKEN = (b"which the page sends as a header and removes from the address bar",
                   b"which the page sends as a header")
DASHBOARD_ESCAPE = (b"Every value is passed through `html.escape`", b"Most values are passed through `html.escape`")
# Outside every protected paragraph: the descriptive note on the reference integration.
DESCRIPTIVE = (b"For the included reference integration,", b"For the reference integration shipped here,")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _paragraph(data: bytes, needle: bytes) -> bytes:
    found = [block for block in g5._paragraphs(data) if needle in block]
    assert len(found) == 1, needle
    return found[0]


class Trees(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._count = 0

    def tearDown(self):
        self._tmp.cleanup()

    def tree(self, edits=None, *, policy=None, remove=(), extra=None, gate_script=True, hooks=True):
        """A repository tree with the frozen hook; ``edits`` maps a doc name to (old, new) byte pairs."""
        self._count += 1
        root = self.tmp / f"t{self._count}"
        (root / "docs").mkdir(parents=True)
        if gate_script:
            (root / "scripts").mkdir()
            (root / "scripts" / "g5_protected_text.py").write_bytes(b"# the gate\n")
        for name in DOCS:
            data = (FIXTURES / name).read_bytes()
            for old, new in (edits or {}).get(name, ()):
                self.assertIn(old, data)
                data = data.replace(old, new, 1)
            (root / "docs" / name).write_bytes(data)
        if hooks:
            text = policy if policy is not None else (FIXTURES / "policy.yaml").read_bytes()
            (root / "docs" / g5.POLICY_NAME).write_bytes(text)
        for path, data in (extra or {}).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(data)
        for path in remove:
            (root / path).unlink()
        return g5.DirTree(root)

    def decision(self, path, before, after, **fields):
        body = {"schema": g5.DECISION_SCHEMA, "decision_id": "CD-0001", "status": "approved",
                "approved_by": "protected-requirement owner", "path": path, "before_sha256": before,
                "after_sha256": after, "reason": "test", **fields}
        return {"docs/change-decisions/CD-0001.json": json.dumps(body).encode()}

    def violations(self, report):
        return [(v["path"], v["key"]) for v in report["violations"]]


class PositiveControls(Trees):
    def test_the_unchanged_tree_compares_seven_units_and_passes(self):
        report = g5.check(self.tree(), self.tree())
        self.assertEqual((report["base_units"], report["head_units"]), (7, 7))
        self.assertEqual(report["violations"], [])
        self.assertFalse(report["bootstrap"])

    def test_every_rule_protects_exactly_one_fixture_paragraph(self):
        keys = [u.key for u in g5.protected_units(self.tree())]
        self.assertEqual(len(keys), len(set(keys)))
        rules = g5.parse_policy((FIXTURES / "policy.yaml").read_text(encoding="utf-8"), "policy.yaml")
        self.assertEqual(sorted(keys), sorted(f"match:{r['match']}" for r in rules))

    def test_a_descriptive_edit_outside_protected_text_passes(self):
        report = g5.check(self.tree(), self.tree({"RECONCILIATION.md": [DESCRIPTIVE]}))
        self.assertEqual(report["violations"], [])

    def test_adding_protected_text_passes(self):
        policy = (FIXTURES / "policy.yaml").read_bytes() + b'  - match: "a new promise"\n'
        head = self.tree(policy=policy, extra={"docs/NEW.md": b"This is a new promise.\n"})
        report = g5.check(self.tree(), head)
        self.assertEqual((report["head_units"], report["violations"]), (8, []))


class NegativeControls(Trees):
    def assertOneViolation(self, head, path, key_part):
        found = self.violations(g5.check(self.tree(), head))
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][0], path)
        self.assertIn(key_part, found[0][1])

    def test_each_protected_paragraph_fails_when_weakened(self):
        cases = [
            ("RECONCILIATION.md", CALLER_FIELDS, "Only a matching response"),
            ("RECONCILIATION.md", NOT_FOUND, "none licenses acceptance"),
            ("ATTEST_RECONCILIATION_MAP.md", MAPPING_ROW, "Unknown kinds fail closed"),
            ("ATTEST_RECONCILIATION_MAP.md", CANNOT_EMIT, "cannot emit"),
            ("ATTEST_RECONCILIATION_MAP.md", TRANSLATE, "Do not translate"),
            ("BUILD_THREAD.md", INSPECTOR_TOKEN, "removes from the address bar"),
            ("DASHBOARD.md", DASHBOARD_ESCAPE, "html.escape"),
        ]
        self.assertEqual(len(cases), 7, "one weakening per rule")
        for name, edit, key in cases:
            with self.subTest(name=name, key=key):
                self.assertOneViolation(self.tree({name: [edit]}), f"docs/{name}", key)

    def test_trailing_whitespace_in_a_protected_paragraph_is_a_change(self):
        old, _ = CALLER_FIELDS
        self.assertOneViolation(self.tree({"RECONCILIATION.md": [(old, old + b" ")]}),
                                "docs/RECONCILIATION.md", "Only a matching response")

    def test_strengthening_is_a_change_too(self):
        edit = (b"Caller-provided `success` or `evidence` fields are rejected.",
                b"Caller-provided `success` or `evidence` fields are always rejected.")
        self.assertOneViolation(self.tree({"RECONCILIATION.md": [edit]}),
                                "docs/RECONCILIATION.md", "Only a matching response")

    def test_dropping_a_rule_fails(self):
        policy = (FIXTURES / "policy.yaml").read_bytes().replace(
            b'  - match: "Every value is passed through `html.escape`"\n', b"")
        self.assertOneViolation(self.tree(policy=policy), "docs/DASHBOARD.md", "html.escape")

    def test_deleting_the_policy_fails_for_every_unit(self):
        report = g5.check(self.tree(), self.tree(hooks=False))
        self.assertEqual(len(report["violations"]), 7)

    def test_deleting_a_protected_doc_fails(self):
        report = g5.check(self.tree(), self.tree(remove=["docs/DASHBOARD.md"]))
        self.assertEqual(self.violations(report), [("docs/DASHBOARD.md", "match:Every value is passed through `html.escape`")])

    def test_moving_a_protected_paragraph_to_another_doc_fails(self):
        dashboard = (FIXTURES / "DASHBOARD.md").read_bytes()
        block = _paragraph(dashboard, b"html.escape")
        head = self.tree({"DASHBOARD.md": [(block, b"")]}, extra={"docs/MOVED.md": block})
        found = self.violations(g5.check(self.tree(), head))
        self.assertEqual(found, [("docs/DASHBOARD.md", "match:Every value is passed through `html.escape`")])


class Decisions(Trees):
    def setUp(self):
        super().setUp()
        data = (FIXTURES / "RECONCILIATION.md").read_bytes()
        self.path = "docs/RECONCILIATION.md"
        self.before = _sha(_paragraph(data, CALLER_FIELDS[0]))
        self.after = _sha(_paragraph(data.replace(*CALLER_FIELDS), CALLER_FIELDS[1]))
        self.edit = {"RECONCILIATION.md": [CALLER_FIELDS]}

    def test_an_approved_decision_on_base_allows_exactly_the_bound_change(self):
        decision = self.decision(self.path, self.before, self.after)
        report = g5.check(self.tree(extra=decision), self.tree(self.edit, extra=decision))
        self.assertEqual(report["violations"], [])
        self.assertEqual([a["decision_id"] for a in report["approved_changes"]], ["CD-0001"])
        other = {"RECONCILIATION.md": [(CALLER_FIELDS[0], b"Caller fields are fine.")]}
        self.assertEqual(len(g5.check(self.tree(extra=decision), self.tree(other, extra=decision))["violations"]), 1)

    def test_a_decision_added_by_the_same_change_does_not_count(self):
        decision = self.decision(self.path, self.before, self.after)
        report = g5.check(self.tree(), self.tree(self.edit, extra=decision))
        self.assertEqual(len(report["violations"]), 1)
        self.assertIn("only in this change", report["violations"][0]["reason"])

    def test_a_decision_with_a_wrong_field_approves_nothing(self):
        cases = {
            "status": self.decision(self.path, self.before, self.after, status="proposed"),
            "approved_by": self.decision(self.path, self.before, self.after, approved_by=" "),
            "schema": self.decision(self.path, self.before, self.after, schema="syberwork.change-decision/v0"),
            "before": self.decision(self.path, "0" * 64, self.after),
            "after": self.decision(self.path, self.before, "0" * 64),
            "path": self.decision("docs/ATTEST_RECONCILIATION_MAP.md", self.before, self.after),
            "uppercase": self.decision(self.path, self.before.upper(), self.after),
        }
        for name, decision in cases.items():
            with self.subTest(name):
                report = g5.check(self.tree(extra=decision), self.tree(self.edit, extra=decision))
                self.assertEqual(len(report["violations"]), 1)

    def test_a_null_after_digest_allows_removal(self):
        dashboard = (FIXTURES / "DASHBOARD.md").read_bytes()
        decision = self.decision("docs/DASHBOARD.md", _sha(_paragraph(dashboard, b"html.escape")), None)
        report = g5.check(self.tree(extra=decision), self.tree(remove=["docs/DASHBOARD.md"], extra=decision))
        self.assertEqual(report["violations"], [])


class FailClosed(Trees):
    def test_a_base_with_the_gate_and_no_protected_text_cannot_pass(self):
        with self.assertRaisesRegex(g5.GateError, "vacuous"):
            g5.check(self.tree(hooks=False), self.tree())

    def test_installing_the_gate_passes_only_if_the_head_protects_something(self):
        report = g5.check(self.tree(gate_script=False, hooks=False), self.tree())
        self.assertTrue(report["bootstrap"])
        self.assertEqual((report["base_units"], report["head_units"], report["violations"]), (0, 7, []))
        self.assertIn("BOOTSTRAP", g5.summary(report))
        with self.assertRaisesRegex(g5.GateError, "base or head"):
            g5.check(self.tree(gate_script=False, hooks=False), self.tree(hooks=False))

    def test_an_unreadable_policy_cannot_pass(self):
        cases = {
            "plain scalar": b"protected:\n  - match: increments by one\n",
            "null list": b"protected:\n",
            "other key": b"protected:\n  - match: \"x\"\nextra: true\n",
            "mapping item": b"protected:\n  - match: \"x\"\n    note: \"y\"\n",
            "unknown rule": b"protected:\n  - regex: \"x\"\n",
            "junk after value": b"protected:\n  - match: \"x\" y\n",
            "not a list": b"protected: {}\n",
            "not utf-8": b"protected:\n  - match: \"\xff\"\n",
            "mixed indentation": b"protected:\n  - match: \"x\"\n- match: \"y\"\n",
        }
        for name, policy in cases.items():
            with self.subTest(name):
                with self.assertRaises(g5.GateError):
                    g5.check(self.tree(policy=policy), self.tree())

    def test_the_reader_accepts_the_subset_attest_uses(self):
        text = ("# comment\nprotected:\n  # rule\n  - match: \"a `b`\"  # why\n"
                "  - match: 'it''s'\n  - claim: \"req-1\"\n")
        self.assertEqual(g5.parse_policy(text, "p"), [{"match": "a `b`"}, {"match": "it's"}, {"claim": "req-1"}])
        self.assertEqual(g5.parse_policy("protected: []\n", "p"), [])
        self.assertEqual(g5.parse_policy("# only a comment\n", "p"), [])

    def test_claim_rules_protect_nothing(self):
        report = g5.check(self.tree(), self.tree(policy=b'protected:\n  - claim: "req-1"\n'))
        self.assertEqual(len(report["violations"]), 7)


class Cli(Trees):
    """The command the workflow runs, against a real temporary git repository."""

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=g5", "-c", "user.email=g5@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
                              capture_output=True, check=True, text=True).stdout.strip()

    def commit(self, tree):
        for path in self.repo.iterdir():
            if path.name != ".git":
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        shutil.copytree(tree.root, self.repo, dirs_exist_ok=True)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "rev")
        return self.git("rev-parse", "HEAD")

    def run_gate(self, base, head):
        with tempfile.TemporaryFile("w+") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                return g5.main(["--repo", str(self.repo), "--base", base, "--head", head])

    def test_exit_codes(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        empty = self.commit(self.tree(hooks=False))
        base = self.commit(self.tree())
        weakened = self.commit(self.tree({"RECONCILIATION.md": [CALLER_FIELDS]}))
        restored = self.commit(self.tree())
        self.assertEqual(self.run_gate(base, restored), 0)
        self.assertEqual(self.run_gate(base, weakened), 1)
        self.assertEqual(self.run_gate(empty, base), 2)
        self.assertEqual(self.run_gate("no-such-revision", "HEAD"), 2)


if __name__ == "__main__":
    unittest.main()
