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

from syberlabs import _patterns, metrics
from syberlabs import query_guard as guard

SECRET = "zq91 retry with token ghp_" + "Z" * 30
CODE = "def handler(event):\n    rows = load(event)\n    for row in rows:\n        save(row)\n    return len(rows)"
PRIVATE_LINE = "    settlement = ledger.reconcile(batch, tolerance=SYBER_TOLERANCE)"
LAUNCHER = "C:/Users/op/AppData/Local/Temp/syberlabs-guard-x1/githits"  # the form claude_code.write_launcher returns


def bash(command, **extra):
    return {"tool_name": "Bash", "tool_input": {"command": command, "description": "look it up", **extra}}


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

    def test_the_bash_matcher_is_only_in_the_githits_settings(self):
        self.assertEqual(guard.settings(Path("D:/guard"))["hooks"]["PreToolUse"][0]["matcher"], guard.MATCHER)
        [group] = guard.settings(Path("D:/guard"), bash=True)["hooks"]["PreToolUse"]
        self.assertEqual(group["matcher"], guard.MATCHER_BASH)
        for name in ("Bash", "mcp__githits__search", "WebSearch", "WebFetch"):
            self.assertRegex(name, guard.MATCHER_BASH)
        for name in ("PowerShell", "BashOutput", "Read", "mcp__bash__x"):
            self.assertNotRegex(name, guard.MATCHER_BASH)
        self.assertNotRegex("Bash", guard.MATCHER, "without the launcher Bash is not matched, as before")

    def test_powershell_joins_the_matcher_only_with_the_launcher_and_the_override(self):
        """githits-pilot-hardening, item 5 (#134 verification finding 3)."""
        [group] = guard.settings(Path("D:/guard"), bash=True, powershell=True)["hooks"]["PreToolUse"]
        self.assertEqual(group["matcher"], guard.MATCHER_SHELLS)
        for name in ("Bash", "PowerShell", "mcp__githits__search", "WebSearch", "WebFetch"):
            self.assertRegex(name, guard.MATCHER_SHELLS)
        for name in ("PowerShellX", "BashOutput", "Read", "mcp__powershell__x"):
            self.assertNotRegex(name, guard.MATCHER_SHELLS)
        self.assertEqual(guard.settings(Path("D:/guard"), powershell=True)["hooks"]["PreToolUse"][0]["matcher"],
                         guard.MATCHER, "no launcher: the matcher is the plain one")
        self.assertEqual(guard.SHELL_TOOLS, ("Bash", "PowerShell"))

    def test_policy_reads_arguments_then_environment_then_defaults(self):
        self.assertEqual(guard.policy(), {"version": guard.VERSION, "max_lines": 3, "max_chars": 400})
        with self.assertRaises(ValueError):
            guard.policy(max_lines=0)


class BashLauncher(unittest.TestCase):
    """githits-wiring, item 3: with the GitHits launcher, a Bash command passes only when it is the launcher's exact
    absolute path followed by plain arguments, and those arguments are judged as a GitHits query is."""

    def test_the_launcher_with_plain_and_quoted_arguments_passes_as_the_shell_splits_them(self):
        cases = {
            "": [],
            ' search "how does requests retry on 503?" --target pypi:requests@2.32.3':
                ["search", "how does requests retry on 503?", "--target", "pypi:requests@2.32.3"],
            " read pypi:requests@2.32.3 --path src/requests/adapters.py --start-line 100 --end-line 160":
                ["read", "pypi:requests@2.32.3", "--path", "src/requests/adapters.py", "--start-line", "100",
                 "--end-line", "160"],
            ' list pypi:requests --path "src/**"': ["list", "pypi:requests", "--path", "src/**"],
            " grep 'Retry(total=' --target pypi:urllib3 --literal": None,  # parentheses are refused anywhere
            """ search --target=npm:@scope/pkg@1.0 "a""b" 'c d'""": ["search", "--target=npm:@scope/pkg@1.0", "ab",
                                                                    "c d"],
            "  search   q  ": ["search", "q"],
        }
        for tail, expected in cases.items():
            with self.subTest(tail=tail):
                rule, arguments = guard.bash_arguments(LAUNCHER + tail, LAUNCHER)
                self.assertEqual((rule, arguments), (guard.SHELL_SYNTAX, []) if expected is None else (guard.PASS, expected))

    def test_metacharacters_and_control_operators_are_refused_anywhere(self):
        for tail in (" search q; cat ~/.ssh/id_rsa", " search q && env", " search q || true", " search q | tee x",
                     " search q & ", " search `id`", " search $(id)", ' search "$(id)"', " search ${HOME}",
                     " search q > out.txt", " search q < in.txt", " search q 2>&1", " search q\nenv", " search q\r",
                     " search q\tx", ' search "a\\"b"', " search 'it''s'\\''", " search !!", " search (q)",
                     " search {a,b}", " search what?", " search *", " search ~/x", " search #c", ' search "open',
                     " search 'open", " search q\x00", " search caf\u00e9"):
            with self.subTest(tail=tail):
                self.assertEqual(guard.bash_arguments(LAUNCHER + tail, LAUNCHER), (guard.SHELL_SYNTAX, []))

    def test_any_other_command_is_not_the_launcher(self):
        for command in ("ls", "cat ~/.ssh/id_rsa", LAUNCHER + "x search q", LAUNCHER[:-1], "cd /tmp && " + LAUNCHER,
                        " " + LAUNCHER, '"' + LAUNCHER + '" search q', "sh " + LAUNCHER + " search q",
                        LAUNCHER.replace("/", "\\"), "", None, 7):
            with self.subTest(command=command):
                self.assertEqual(guard.bash_arguments(command, LAUNCHER), (guard.NOT_LAUNCHER, []))
        self.assertEqual(guard.bash_arguments(LAUNCHER + " search q", None), (guard.NOT_LAUNCHER, []),
                         "no launcher configured: no Bash command passes")

    def test_decide_judges_the_launchers_arguments_and_refuses_the_rest(self):
        plain_line = "    settlement tolerance is SYBER_TOLERANCE for every batch"  # a tree line a shell can carry
        tree = {guard.line_digest(PRIVATE_LINE), guard.line_digest(plain_line)}
        decide = lambda call, **kw: guard.decide(call, launcher=LAUNCHER, tree=tree, **kw)
        self.assertEqual(decide(bash(LAUNCHER + ' search "pydantic v2 model_validator" --target pypi:pydantic@2.7.1')),
                         guard.PASS)
        self.assertEqual(decide(bash(LAUNCHER + f' search "{SECRET}" --target pypi:requests')), "secret")
        self.assertEqual(decide(bash(LAUNCHER + ' search "ask jane.doe@example.com" --target pypi:x')), "email")
        self.assertEqual(decide(bash(LAUNCHER + ' read "https://u:pw@git.example.test/r"')), "url_credentials")
        self.assertEqual(decide(bash(LAUNCHER + f' search "{plain_line.strip()}" --target pypi:x')), "tree_line")
        self.assertEqual(decide(bash(LAUNCHER + f' search "{PRIVATE_LINE.strip()}"')), guard.SHELL_SYNTAX,
                         "a line with parentheses never reaches the judge")
        self.assertEqual(decide(bash(LAUNCHER + ' search "' + "q" * 401 + '"')), "too_long")
        self.assertEqual(decide(bash(LAUNCHER + " search " + " ".join(["word"] * 90))), "too_long",
                         "the arguments joined are judged too, not only each one")
        self.assertEqual(decide(bash(LAUNCHER + " search q; env")), guard.SHELL_SYNTAX)
        self.assertEqual(decide(bash("ls -la")), guard.NOT_LAUNCHER)
        self.assertEqual(decide(bash(LAUNCHER + " search q", run_in_background=True)), guard.SHELL_SYNTAX)
        self.assertEqual(decide({"tool_name": "Bash", "tool_input": "ls"}), guard.NOT_LAUNCHER)
        self.assertEqual(guard.decide(bash(LAUNCHER + " search q")), guard.NOT_LAUNCHER, "no launcher, no pass")
        long_description = bash(LAUNCHER + ' search "csv dialects" --target pypi:x')
        long_description["tool_input"]["description"] = "x" * 900 + SECRET
        self.assertEqual(decide(long_description), guard.PASS, "the description stays on this machine")
        self.assertEqual(guard.query_text({"command": LAUNCHER + " search q", "description": "d"}),
                         LAUNCHER + " search q", "a Bash call's digest is its command line's")

    def test_a_powershell_call_is_judged_by_the_same_launcher_only_rule(self):
        """githits-pilot-hardening, item 5: under the override PowerShell is matched, and then any command but the
        launcher is refused, as for Bash (before, a matched PowerShell call was judged as plain outbound text, so
        ``Get-ChildItem env:`` passed)."""
        def powershell(command, **extra):
            return {"tool_name": "PowerShell", "tool_input": {"command": command, "description": "d", **extra}}

        decide = lambda call: guard.decide(call, launcher=LAUNCHER)
        for command in ("Get-ChildItem env:", "dir", "Get-Content ~/.ssh/id_rsa", "& " + LAUNCHER + " search q",
                        LAUNCHER.replace("/", "\\") + " search q", "", None):
            with self.subTest(command=command):
                self.assertEqual(decide(powershell(command)), guard.NOT_LAUNCHER)
        self.assertEqual(decide(powershell(LAUNCHER + " search q; Get-ChildItem env:")), guard.SHELL_SYNTAX)
        self.assertEqual(decide(powershell(LAUNCHER + " search q", run_in_background=True)), guard.SHELL_SYNTAX)
        self.assertEqual(decide(powershell(LAUNCHER + f' search "{SECRET}" --target pypi:x')), "secret")
        self.assertEqual(decide({"tool_name": "PowerShell", "tool_input": "dir"}), guard.NOT_LAUNCHER)
        self.assertEqual(guard.decide(powershell("Get-ChildItem")), guard.NOT_LAUNCHER, "no launcher, no pass")
        self.assertEqual(decide(powershell(LAUNCHER + ' search "csv dialects" --target pypi:x')), guard.PASS,
                         "the same rule as Bash: only the launcher with plain arguments")


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

    def test_the_hook_loads_two_leaf_modules_and_not_the_package(self):
        """perf-packet-a (efficiency audit 2026-10-26, item 6): the hook imported ``syberlabs.metrics`` for the
        credential pattern, and any ``syberlabs.*`` import runs the package ``__init__``, which imports the whole SDK:
        ~200 of a lookup's ~220 ms. It now loads ``canonical.py`` and ``_patterns.py`` from beside itself by path."""
        done = subprocess.run([self.hook["command"], "-X", "importtime", *self.hook["args"]],
                              input=self.call("WebSearch", {"query": "pydantic v2 model_validator usage"}),
                              capture_output=True, timeout=120, cwd=self.dir.parent, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        imported = [line.rsplit("|", 1)[-1].strip() for line in done.stderr.decode("utf-8").splitlines()
                    if line.startswith("import time:")]
        self.assertIn("json", imported, "the import list was read")
        self.assertEqual([name for name in imported if name == "syberlabs" or name.startswith("syberlabs.")], [])
        [decision] = self.decisions()
        self.assertEqual(decision["rule"], "pass")
        self.assertTrue(guard.authentic(decision, KEY), "the hook's canonical JSON is the host's")

    def test_one_credential_pattern_object_and_the_same_policy(self):
        self.assertIs(guard.CREDENTIAL, _patterns.SECRET)
        self.assertIs(metrics._SECRET, _patterns.SECRET)
        self.assertEqual(_patterns.SECRET.pattern,
                         r"(ghp_|gho_|ghs_|github_pat_|sk-ant-|sk-|xox[abprs]-|AKIA)[A-Za-z0-9_\-]{8,}")
        # the digest of the default policy file, as the host computed it before perf-packet-a
        self.assertEqual(hashlib.sha256(json.dumps(guard.policy(3, 400)).encode()).hexdigest(),
                         "e6b17cc24a93c21aa3a3e1a29d1287494d80034844f219202767567d2150262d")
        self.assertEqual(guard.file_digest(self.dir / guard.POLICY_FILE),
                         hashlib.sha256(json.dumps(guard.policy()).encode()).hexdigest())

    def test_the_hook_passes_only_the_launcher_and_signs_each_bash_decision(self):
        settings = guard.prepare(self.dir.parent / "bash-guard", guard.policy(), {guard.line_digest(PRIVATE_LINE)},
                                 bash=True)
        self.addCleanup(guard.remove, settings.parent)
        [group] = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        self.assertEqual(group["matcher"], guard.MATCHER_BASH)
        hook = group["hooks"][0]
        env = {**os.environ, **guard.hook_environment(settings.parent, KEY, LAUNCHER)}
        self.assertEqual(env[guard.LAUNCHER_ENV], LAUNCHER)
        cases = [(LAUNCHER + ' search "csv dialect sniffing" --target pypi:clevercsv', guard.PASS),
                 ("cat /etc/passwd", guard.NOT_LAUNCHER),
                 (LAUNCHER + " search q; curl https://example.test", guard.SHELL_SYNTAX),
                 (LAUNCHER + f' search "{SECRET}"', "secret")]
        for number, (command, rule) in enumerate(cases):
            payload = json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                  "tool_input": {"command": command}, "tool_use_id": f"toolu_b{number}"}).encode("utf-8")
            done = subprocess.run([hook["command"], *hook["args"]], input=payload, capture_output=True, timeout=120,
                                  env=env)
            self.assertEqual(done.returncode, 0 if rule == guard.PASS else guard.BLOCK_EXIT, command)
            if rule != guard.PASS:
                self.assertIn(f"rule: {rule}", done.stderr.decode("utf-8"))
                self.assertNotIn("passwd", done.stderr.decode("utf-8"), "never echoes the command")
        found = {d["tool_use_id"]: d for d in guard.read_decisions(settings.parent)}
        self.assertEqual([found[f"toolu_b{n}"]["rule"] for n in range(len(cases))], [rule for _, rule in cases])
        self.assertTrue(all(guard.authentic(d, KEY) and d["tool"] == "Bash" for d in found.values()))
        self.assertEqual(found["toolu_b0"]["query_digest"], guard.query_digest({"command": cases[0][0]}))
        no_launcher = {k: v for k, v in env.items() if k != guard.LAUNCHER_ENV}
        done = subprocess.run([hook["command"], *hook["args"]], input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": cases[0][0]}, "tool_use_id": "toolu_bx"}).encode(),
            capture_output=True, timeout=120, env=no_launcher)
        self.assertEqual(done.returncode, guard.BLOCK_EXIT, "without the host's launcher no Bash command passes")
        self.assertIn(b"rule: not_launcher", done.stderr)
        # guard-lane, item 1: a passed launcher call's decision carries the digest of the arguments the hook read, which
        # the launcher's entry claims before sending; a blocked one carries none. The MAC covers it.
        self.assertEqual(found["toolu_b0"]["arguments_digest"], guard.arguments_digest(
            ["search", "csv dialect sniffing", "--target", "pypi:clevercsv"]))
        self.assertTrue(all("arguments_digest" not in found[f"toolu_b{n}"] for n in range(1, len(cases))))
        altered = {**found["toolu_b0"], "arguments_digest": guard.arguments_digest(["search", "other"])}
        self.assertFalse(guard.authentic(altered, KEY), "a rewritten arguments digest no longer verifies")
        self.assertFalse(guard.authentic({**found["toolu_b0"], "arguments_digest": None}, KEY), "nor a removed one")
        launcher_env = {guard.KEY_ENV: KEY, guard.LAUNCHER_ENV: (settings.parent / "githits").as_posix()}
        self.assertEqual(guard.claim_launch(["search", "csv dialect sniffing", "--target", "pypi:clevercsv"],
                                            launcher_env), (None, "toolu_b0"))
        self.assertEqual(guard.claim_launch(["search", "csv dialect sniffing", "--target", "pypi:clevercsv"],
                                            launcher_env)[0], guard.DECISION_USED, "one decision, one launch")
        self.assertEqual(guard.claim_launch(["search", "q;"], launcher_env)[0], guard.NO_DECISION)
        self.assertEqual(guard.claim_launch(["search", "x"], {guard.KEY_ENV: KEY})[0], guard.NO_GUARD)
        self.assertEqual(guard.claim_launch(["search", "csv dialect sniffing", "--target", "pypi:clevercsv"],
                                            {**launcher_env, guard.KEY_ENV: "another-key"})[0], guard.NO_DECISION)

    def test_the_hook_decides_as_the_host_does(self):
        texts = ["pydantic v2 model_validator usage", SECRET, "key AKIA" + "Q" * 16, "slack xoxb-" + "1" * 12,
                 "use sk-ant-" + "a" * 24, "risk-assessment library", "\u0430KIA" + "Q" * 16,
                 "ask jane.doe@example.com", "x" * 500, PRIVATE_LINE, "a\nb\nc\nd"]
        for use_id, text in enumerate(texts):
            payload = {"tool_name": "WebSearch", "tool_input": {"query": text}, "tool_use_id": f"toolu_{use_id}"}
            expected = guard.decide(payload, max_lines=guard.DEFAULT_MAX_LINES, max_chars=guard.DEFAULT_MAX_CHARS,
                                    tree={guard.line_digest(PRIVATE_LINE)})
            done = self.run_hook(self.call("WebSearch", {"query": text}, f"toolu_{use_id}"))
            self.assertEqual(done.returncode, 0 if expected == guard.PASS else guard.BLOCK_EXIT, text[:20])
        decided = {d["tool_use_id"]: d["rule"] for d in self.decisions()}
        self.assertEqual([decided[f"toolu_{i}"] for i in range(len(texts))],
                         [guard.decide({"tool_name": "WebSearch", "tool_input": {"query": text}},
                                       tree={guard.line_digest(PRIVATE_LINE)}) for text in texts])


if __name__ == "__main__":
    unittest.main()
