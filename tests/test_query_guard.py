"""The query guard: the rules as a function, and the hook as the process Claude Code runs.

No model and no Claude Code run here. The process tests start ``python -I syberlabs/query_guard.py <directory>``
exactly as the settings document names it, write a ``PreToolUse`` input on its standard input, and read its exit
code, its output and the decision it leaves beside itself.
"""

import hashlib
import json
import os
import stat
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

    def test_unicode_evasions_are_judged_folded(self):
        """guard-hardening, finding 1: each of these passed ``judge`` on 92074e1cd0."""
        key = "ghp_" + "Z" * 36
        cases = [
            ("ghp_ZZZZZZ\u200b" + "Z" * 30, "secret", "a zero-width space inside a key"),
            ("ghp_" + "Z" * 18 + "\u2060" + "Z" * 18, "secret", "a word joiner inside a key"),
            ("use \uff53\uff4b-" + "a" * 24 + " in the header", "secret", "a fullwidth sk-"),
            ("g\u04bbp_" + "Z" * 36, "secret", "a Cyrillic shha in place of the h of ghp_"),
            ("\u0410\u041a\u0406\u0410" + "Z" * 16, "secret", "Cyrillic letters spelling AKIA"),
            ("\U0001d5c0\U0001d5c1\U0001d5c9_" + "Z" * 36, "secret", "mathematical sans-serif ghp"),
            ("ask jane.doe\uff20example.com about requests", "email", "a fullwidth at sign"),
            ("ask jane.doe@exam\u200cple.com about requests", "email", "a zero-width non-joiner in the domain"),
            ("\uff40\uff40\uff40python\nimport requests\n\uff40\uff40\uff40", "code_fence", "fullwidth backticks"),
        ]
        for text, rule, why in cases:
            self.assertEqual(guard.judge(text), rule, why)
        self.assertEqual(guard.fold("a\u200bb\u00adc"), "abc", "format characters (Cf) are removed")
        self.assertEqual(guard.judge(key), "secret", "the plain key, as before")
        for text in ("pydantic v2 model_validator usage", "risk-assessment library for python",
                     "\u041a\u0430\u043a \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442 requests retry",
                     "na\u00efve bayes in scikit-learn"):
            self.assertEqual(guard.judge(text), "pass", "plain and non-Latin questions still pass")

    def test_keys_and_webfetch_prompt_are_judged(self):
        """guard-hardening, finding 3: a credential used as a key, and WebFetch's prompt, passed on 92074e1cd0."""
        self.assertEqual(guard.decide({"tool_name": "mcp__githits__search",
                                       "tool_input": {"query": "retry", "filters": {SECRET: True}}}), "secret",
                         "a dictionary key leaves the machine like a value")
        self.assertEqual(guard.decide({"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example.test/a",
                                                                              "prompt": CODE}}), "code_lines",
                         "WebFetch's prompt is processed off this machine, so it is judged")
        self.assertEqual(guard.decide({"tool_name": "WebSearch", "tool_input": {"query": "csv", "extra": SECRET}}),
                         "secret", "a field WebSearch adds later is judged too")
        self.assertEqual(guard.outbound("WebFetch", {"url": "https://x.example.test/", "prompt": "summarize"}),
                         ["url", "https://x.example.test/", "prompt", "summarize"])

    def test_what_is_judged_is_what_leaves(self):
        self.assertEqual(guard.decide({"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example.test/a",
                                                                              "prompt": "the retry options"}}), "pass")
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


KEY = "k" * 64  # a test key; a real one is secrets.token_hex(32), per call


class Process(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name) / "guard"
        self.addCleanup(guard.remove, self.dir)
        self.settings = guard.prepare(self.dir, guard.policy(), {guard.line_digest(PRIVATE_LINE)})
        [group] = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        self.hook = group["hooks"][0]
        self.env = {**os.environ, **guard.hook_environment(self.dir, KEY)}

    def run_hook(self, payload: bytes, env=None):
        return subprocess.run([self.hook["command"], *self.hook["args"]], input=payload, capture_output=True, timeout=120,
                              cwd=self.dir.parent, env=self.env if env is None else env)

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
        entry = {"tool_use_id": "toolu_1", "tool": "mcp__githits__search",
                 "query_digest": guard.query_digest({"query": "pydantic v2 model_validator usage"}), "rule": "pass"}
        self.assertEqual(decision, {**entry, "mac": guard.mac(entry, KEY)})
        self.assertTrue(guard.authentic(decision, KEY))
        self.assertFalse(guard.authentic({**decision, "rule": "secret"}, KEY), "the MAC covers the rule")
        self.assertFalse(guard.authentic(decision, "other" * 8), "and only the call's key verifies it")
        written = b"".join(p.read_bytes() for p in self.dir.rglob("*") if p.is_file())
        self.assertNotIn(KEY.encode(), written, "the key is in the hook's environment, never in a file beside it")

    def test_a_lone_surrogate_is_judged_and_recorded_under_the_hosts_digest(self):
        """pre-pilot-cleanup, from #84's verification: on 029d27c7e1 the hook could not digest a query holding a lone
        surrogate (a JSON escape of U+DC00 decodes to one), so it blocked with guard_error and recorded nothing."""
        query = "zq84 python csv " + chr(0xDC00) + " sniff dialect"
        self.assertEqual(guard.query_digest({"query": query}),
                         hashlib.sha256(query.encode("utf-8", "surrogatepass")).hexdigest())
        self.assertEqual(guard.utf8("plain text"), b"plain text", "valid text encodes as before")
        done = self.run_hook(self.call("WebSearch", {"query": query}))
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        [decision] = self.decisions()
        self.assertEqual((decision["rule"], decision["query_digest"]), ("pass", guard.query_digest({"query": query})))
        self.assertTrue(guard.authentic(decision, KEY))

    def test_a_guard_that_cannot_judge_blocks(self):
        done = self.run_hook(b"not json")
        self.assertEqual(done.returncode, guard.BLOCK_EXIT)
        self.assertIn(b"rule: guard_error", done.stderr)
        os.chmod(self.dir / guard.POLICY_FILE, stat.S_IREAD | stat.S_IWRITE)
        (self.dir / guard.POLICY_FILE).unlink()
        done = self.run_hook(self.call("WebSearch", {"query": "fine"}))
        self.assertEqual(done.returncode, guard.BLOCK_EXIT, "a missing policy fails closed")
        self.assertEqual(sorted(d["rule"] for d in self.decisions()), [guard.ERROR, guard.ERROR])
        done = subprocess.run([sys.executable, "-I", str(Path(guard.__file__).resolve())], input=b"{}", capture_output=True,
                              timeout=120)
        self.assertEqual(done.returncode, guard.BLOCK_EXIT, "no directory: blocked")

    def test_the_policy_is_read_only_and_a_changed_policy_or_tree_file_blocks(self):
        """guard-hardening, finding 2(c): on 92074e1cd0 a rewritten policy.json was obeyed."""
        for name in (guard.POLICY_FILE, guard.TREE_FILE, guard.SETTINGS_FILE):
            mode = stat.S_IMODE(os.stat(self.dir / name).st_mode)
            self.assertEqual(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), 0, f"{name} is written read-only")
        self.assertEqual(self.env[guard.POLICY_DIGEST_ENV], guard.file_digest(self.dir / guard.POLICY_FILE))
        long_query = {"query": "q" * 600}
        for name, text in ((guard.POLICY_FILE, json.dumps({"version": "2", "max_lines": 99, "max_chars": 100000})),
                           (guard.TREE_FILE, "")):
            path = self.dir / name
            original = path.read_bytes()
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)  # what a process with the operator's rights could do
            path.write_text(text, encoding="utf-8")
            done = self.run_hook(self.call("WebSearch", long_query if name == guard.POLICY_FILE else {"query": PRIVATE_LINE}))
            self.assertEqual(done.returncode, guard.BLOCK_EXIT, f"a changed {name} is refused, not obeyed")
            self.assertIn(b"rule: guard_error", done.stderr)
            path.write_bytes(original)
        self.assertEqual([d["rule"] for d in self.decisions()], [guard.ERROR, guard.ERROR])

    def test_without_the_key_or_a_tool_use_id_the_hook_blocks(self):
        """guard-hardening, finding 2(a)/(b): a hook run outside the host's environment, or a call it cannot tie
        to a tool use id, blocks; neither decision verifies, so the host reads the lookup as unguarded."""
        env = {k: v for k, v in self.env.items() if k != guard.KEY_ENV}
        done = self.run_hook(self.call("WebSearch", {"query": "fine"}), env=env)
        self.assertEqual(done.returncode, guard.BLOCK_EXIT, "no key: blocked")
        done = self.run_hook(self.call("WebSearch", {"query": "fine"}, use_id=None))
        self.assertEqual(done.returncode, guard.BLOCK_EXIT, "no tool_use_id: blocked")
        found = sorted(self.decisions(), key=lambda d: d["mac"] is None)
        self.assertEqual([d["rule"] for d in found], [guard.ERROR, guard.ERROR])
        self.assertEqual([guard.authentic(d, KEY) for d in found], [True, False], "a key-less decision has no MAC")
        self.assertIsNone(found[0]["tool_use_id"])

    def test_malformed_decision_files_are_skipped(self):
        (self.dir / guard.DECISIONS / "x.json").write_text("{", encoding="utf-8")
        (self.dir / guard.DECISIONS / "y.json").write_text(json.dumps({"rule": "made_up", "tool": "t", "query_digest": "d",
                                                                       "tool_use_id": None}), encoding="utf-8")
        self.assertEqual(self.decisions(), [])


if __name__ == "__main__":
    unittest.main()
