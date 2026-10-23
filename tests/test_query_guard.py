"""The query guard: the rules as a function, and the hook as the process Claude Code runs.

No model and no Claude Code run here. The process tests start ``python -I syberlabs/query_guard.py <directory>``
exactly as the settings document names it, write a ``PreToolUse`` input on its standard input, and read its exit
code, its output and the decision it leaves beside itself.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from syberlabs import query_guard as guard

SECRET = "zq91 retry with token ghp_" + "Z" * 30
CODE = "def handler(event):\n    rows = load(event)\n    for row in rows:\n        save(row)\n    return len(rows)"
PRIVATE_LINE = "    settlement = ledger.reconcile(batch, tolerance=SYBER_TOLERANCE)"


class Rules(unittest.TestCase):
    def test_each_rule_and_a_plain_public_question(self):
        cases = [
            ("pydantic v2 model_validator usage", "pass"),
            ("risk-assessment library for python", "pass"),
            (SECRET, "secret"),
            ("use key sk-ant-" + "a" * 24 + " in the header", "secret"),
            ("-----BEGIN RSA PRIVATE KEY-----", "secret"),
            ("clone https://deploy:hunter2@git.example.test/repo.git", "url_credentials"),
            ("ask jane.doe@example.com about requests", "email"),
            ("x" * 600, "too_long"),
            ("```python\nimport requests\n```", "code_fence"),
            (CODE, "code_lines"),
            ("a\nb\nc", "pass"),
        ]
        for text, rule in cases:
            self.assertEqual(guard.judge(text), rule, text[:40])
        self.assertEqual(guard.judge("x" * 600, max_chars=700), "pass", "the threshold is the host's policy")
        self.assertEqual(guard.judge(CODE, max_lines=5), "pass")

    def test_a_line_copied_from_a_tracked_file_is_blocked_by_its_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src" / "ledger.py").write_text("import os\n" + PRIVATE_LINE + "\r\n", encoding="utf-8")
            (root / "blob.bin").write_bytes(b"\0" * 10 + PRIVATE_LINE.encode())
            tree = guard.tree_digests(root, ["src/ledger.py", "blob.bin", "missing.py"])
        self.assertEqual(len(tree), 1, "short lines, binary and missing files are not hashed")
        self.assertEqual(guard.judge(PRIVATE_LINE, tree=tree), "tree_line")
        self.assertEqual(guard.judge("  settlement =   ledger.reconcile(batch, tolerance=SYBER_TOLERANCE)  ", tree=tree),
                         "tree_line", "whitespace is normalized")
        self.assertEqual(guard.judge("how does ledger reconcile work in python", tree=tree), "pass")
        self.assertEqual(guard.judge("import os", tree={guard.line_digest("import os")}), "pass",
                         f"a line shorter than {guard.MIN_TREE_LINE} characters is too common to block")
        self.assertTrue(all(len(d) == guard.TREE_DIGEST_CHARS for d in tree), "digests, never the text")

    def test_what_is_judged_is_what_leaves(self):
        self.assertEqual(guard.decide({"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example.test/a",
                                                                              "prompt": CODE}}), "pass",
                         "WebFetch's prompt goes to Anthropic's model; only its URL reaches the site")
        self.assertEqual(guard.decide({"tool_name": "WebFetch", "tool_input": {"url": "https://u:p@x.example.test/"}}),
                         "url_credentials")
        self.assertEqual(guard.decide({"tool_name": "WebSearch", "tool_input": {"query": "csv dialects",
                                                                               "allowed_domains": ["a@b.example.com"]}}),
                         "email")
        self.assertEqual(guard.decide({"tool_name": "mcp__githits__grep", "tool_input": {"pattern": "retry",
                                                                                        "options": {"path": SECRET}}}),
                         "secret", "every string of a GitHits input leaves the machine")
        self.assertEqual(guard.decide({"tool_name": None}), guard.ERROR)

    def test_the_settings_document_is_the_hook_alone(self):
        document = guard.settings(Path("D:/guard"), "C:/Python/python.exe")
        self.assertEqual(list(document), ["hooks"])
        self.assertEqual(list(document["hooks"]), ["PreToolUse"])
        [group] = document["hooks"]["PreToolUse"]
        [hook] = group["hooks"]
        self.assertEqual(group["matcher"], guard.MATCHER)
        self.assertEqual((hook["type"], hook["command"], hook["args"][0], hook["onFailure"]),
                         ("command", "C:/Python/python.exe", "-I", "block"))
        self.assertEqual(hook["args"][1], str(Path(guard.__file__).resolve()), "the host's own file, by absolute path")

    def test_policy_reads_arguments_then_environment_then_defaults(self):
        self.assertEqual(guard.policy(), {"version": guard.VERSION, "max_lines": 3, "max_chars": 400})
        with self.assertRaises(ValueError):
            guard.policy(max_lines=0)


class Process(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name) / "guard"
        self.settings = guard.prepare(self.dir, guard.policy(), {guard.line_digest(PRIVATE_LINE)})
        [group] = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        self.hook = group["hooks"][0]

    def run_hook(self, payload: bytes):
        return subprocess.run([self.hook["command"], *self.hook["args"]], input=payload, capture_output=True, timeout=120,
                              cwd=self.dir.parent)

    def call(self, name, tool_input, use_id="toolu_1"):
        return json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(self.dir.parent),
                           "tool_name": name, "tool_input": tool_input, "tool_use_id": use_id}).encode("utf-8")

    def decisions(self):
        return guard.read_decisions(self.dir)

    def test_blocked_queries_exit_2_with_the_rule_and_never_the_query(self):
        for use_id, (name, tool_input, rule) in enumerate([
                ("mcp__githits__search", {"query": SECRET}, "secret"),
                ("mcp__githits__search", {"query": "```\n" + CODE + "\n```"}, "code_fence"),
                ("mcp__githits__search", {"query": CODE}, "code_lines"),
                ("mcp__githits__get_example", {"query": PRIVATE_LINE}, "tree_line"),
                ("WebSearch", {"query": "q" * 600}, "too_long")]):
            done = self.run_hook(self.call(name, tool_input, f"toolu_{use_id}"))
            self.assertEqual(done.returncode, guard.BLOCK_EXIT, rule)
            self.assertEqual(done.stdout, b"")
            err = done.stderr.decode("utf-8")
            self.assertIn(f"rule: {rule}", err)
            for fragment in ("ghp_", "ZZZZ", "handler", "settlement", "qqqq"):
                self.assertNotIn(fragment, err, "the hook never prints the query")
        found = self.decisions()
        self.assertEqual(sorted(d["rule"] for d in found), sorted(["secret", "code_fence", "code_lines", "tree_line", "too_long"]))
        written = b"".join(p.read_bytes() for p in (self.dir / guard.DECISIONS).iterdir())
        for fragment in (b"ghp_", b"handler", b"settlement"):
            self.assertNotIn(fragment, written, "a decision holds a digest and a rule, never text")

    def test_an_allowed_query_passes_silently_and_is_recorded_as_pass(self):
        done = self.run_hook(self.call("mcp__githits__search", {"query": "pydantic v2 model_validator usage"}))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        [decision] = self.decisions()
        self.assertEqual(decision, {"tool_use_id": "toolu_1", "tool": "mcp__githits__search",
                                    "query_digest": guard.query_digest({"query": "pydantic v2 model_validator usage"}),
                                    "rule": "pass"})

    def test_a_guard_that_cannot_judge_blocks(self):
        done = self.run_hook(b"not json")
        self.assertEqual(done.returncode, guard.BLOCK_EXIT)
        self.assertIn(b"rule: guard_error", done.stderr)
        (self.dir / guard.POLICY_FILE).unlink()
        done = self.run_hook(self.call("WebSearch", {"query": "fine"}))
        self.assertEqual(done.returncode, guard.BLOCK_EXIT, "a missing policy fails closed")
        self.assertEqual(sorted(d["rule"] for d in self.decisions()), [guard.ERROR, guard.ERROR])
        done = subprocess.run([sys.executable, "-I", str(Path(guard.__file__).resolve())], input=b"{}", capture_output=True,
                              timeout=120)
        self.assertEqual(done.returncode, guard.BLOCK_EXIT, "no directory: blocked")

    def test_malformed_decision_files_are_skipped(self):
        (self.dir / guard.DECISIONS / "x.json").write_text("{", encoding="utf-8")
        (self.dir / guard.DECISIONS / "y.json").write_text(json.dumps({"rule": "made_up", "tool": "t", "query_digest": "d",
                                                                       "tool_use_id": None}), encoding="utf-8")
        self.assertEqual(self.decisions(), [])


if __name__ == "__main__":
    unittest.main()
