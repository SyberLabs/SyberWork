"""``skills/githits/SKILL.md``, arm B's GitHits skill: every command it teaches runs, and every rule it states holds.

The skill's body rides in arm B's system prompt on every request (``claude_code.githits_skill``), so it is kept
short, and these tests keep it true. Each command it shows (a line starting with ``{launcher} ``), with a launcher
path in place of the placeholder:

- passes the query guard (``query_guard.decide``) under the default policy and the tree-line digests of this
  checkout's tracked files, the skill itself included;
- runs through the launcher's entry (``githits_cli_entry``) against the loopback fake GitHits (exit 0, one record),
  and what reaches the fake is valid under the pinned OpenAPI fixture;
- stays within the caps the skill states, which are tighter than the client's defaults.

The characters the skill says to quote, and those it says never to type, are checked against the guard both ways, so
a guard change the skill does not follow fails here.
``GitHitsLauncher.test_the_skills_commands_run_end_to_end_in_arm_b`` (``tests/test_claude_code.py``) runs the same
commands through a whole arm-B call.
"""

import json
import re
import shutil
import subprocess
import unittest

from syberlabs import githits, query_guard
from syberlabs.claude_code import SKILL_PATH, githits_skill
from tests import test_githits as api

LAUNCHER = "/tmp/syberlabs-guard-0000/githits"  # stands in for the per-call path the host substitutes
PROMPT_BUDGET = 3000  # bytes of the body in every arm-B request; raise it only for a rule that pays for itself
OPS = {"/v1/searches": "search", "/v1/read": "read", "/v1/grep": "grep"}


def skill_commands(template: str) -> list[str]:
    """The launcher commands the skill teaches, as the model would type them."""
    return [line.strip().replace("{launcher}", LAUNCHER, 1) for line in template.splitlines()
            if line.strip().startswith("{launcher} ")]


def words_of(command: str) -> list[str]:
    rule, words = query_guard.bash_arguments(command, LAUNCHER)
    if rule != query_guard.PASS:
        raise AssertionError(f"{command!r}: {rule}")
    return words


def judge(argument: str) -> str:
    """The guard's decision on a search whose query argument is ``argument``, typed as written."""
    command = f"{LAUNCHER} search {argument} --target pypi:urllib3@2.2.1"
    return query_guard.decide({"tool_name": "Bash", "tool_input": {"command": command}}, launcher=LAUNCHER)


def tracked_files(root) -> list[str]:
    """The checkout's tracked files, or every file under it when git cannot list them (a superset: the tree-line
    check only gets stricter)."""
    git = shutil.which("git")
    if git and (root / ".git").exists():
        try:
            done = subprocess.run([git, "ls-files", "-z"], cwd=root, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0:
            return [name for name in done.stdout.decode("utf-8").split("\0") if name]
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


class SkillCommands(api.FakeCase):
    """Each command the skill shows passes the guard, runs, fits the contract and keeps the stated caps."""

    def setUp(self):
        super().setUp()
        self.template = githits_skill()[0]
        self.commands = skill_commands(self.template)
        self.parser = githits.launcher_parser()

    def test_the_skill_shows_one_command_per_operation_it_teaches_within_its_budget(self):
        self.assertEqual([words_of(c)[0] for c in self.commands], ["search", "read", "grep"])
        self.assertEqual(self.template.count("{launcher}"), len(self.commands),
                         "the per-call launcher path appears only where the model copies it")
        budget = re.search(r"At most (\w+) launcher commands per task", self.template)[1]
        self.assertEqual({"three": 3}.get(budget), len(self.commands), "the worked example fits the stated budget")
        self.assertLessEqual(len(self.template.encode("utf-8")), PROMPT_BUDGET)

    def test_the_registries_it_names_are_ones_the_client_accepts(self):
        """A registry the client does not know fails a search before any request (``bad_target``), unrecorded."""
        named = re.search(r"`<registry>:<package>@<installed version>` \(([a-z, ]+)\.\.\.\)", self.template)[1]
        for registry in named.split(", "):
            with self.subTest(registry=registry):
                self.assertIn(registry, githits.REGISTRIES)
                self.assertEqual(githits.search_target(f"{registry}:somepkg@1.2.3")["version"], "1.2.3")
        self.assertNotIn("version", githits.search_target("pypi:somepkg"), "no @version means the latest")

    def test_every_command_passes_the_guard_with_this_checkouts_tree(self):
        root = SKILL_PATH.parents[2]
        tree = query_guard.tree_digests(root, tracked_files(root))
        self.assertTrue(tree)
        for command in self.commands:
            with self.subTest(command=command):
                call = {"tool_name": "Bash", "tool_input": {"command": command, "description": "look up"}}
                self.assertEqual(query_guard.decide(call, max_lines=query_guard.DEFAULT_MAX_LINES,
                                                    max_chars=query_guard.DEFAULT_MAX_CHARS, tree=tree,
                                                    launcher=LAUNCHER), query_guard.PASS)

    def test_every_command_runs_through_the_launchers_entry_and_fits_the_pinned_contract(self):
        for command in self.commands:
            words = words_of(command)
            with self.subTest(command=command):
                code, out, err = api.run_entry(self.env, *words)
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.startswith(words[0] + ": "), out)
        self.assertEqual([r["op"] for r in self.records().records], ["search", "read", "grep"])
        spec = api.load_spec()
        for request in self.fake.requests:
            operation = api.operation_of(spec, OPS[request["path"]])
            with self.subTest(path=request["path"]):
                if request["method"] == "POST":
                    schema = operation["requestBody"]["content"]["application/json"]["schema"]
                    self.assertEqual(api.problems(spec, schema, json.loads(request["body"])), [])
                else:
                    params = {p["name"]: p["schema"] for p in operation["parameters"] if p["in"] == "query"}
                    for name, [value] in request["query"].items():
                        typed = int(value) if value.isdigit() else value
                        self.assertEqual(api.problems(spec, params[name], typed), [], name)

    def test_the_commands_keep_the_caps_the_skill_states_and_they_beat_the_clients_defaults(self):
        search, read, grep = (self.parser.parse_args(words_of(c)) for c in self.commands)
        limit = int(re.search(r"`--limit (\d+)`", self.template)[1])
        matches = int(re.search(r"`--max-matches (\d+)`", self.template)[1])
        lines = int(re.search(r"at most (\d+) lines", self.template)[1])
        self.assertEqual((limit, matches, lines), (3, 5, 60))
        self.assertLess(limit, githits.SEARCH_LIMIT)
        self.assertLess(matches, githits.GREP_MAX_MATCHES)
        self.assertLess(lines, githits.READ_LINES)
        self.assertEqual((search.limit, search.sources), (limit, ["symbol"]))
        self.assertEqual((grep.max_matches, grep.pattern_type), (matches, "literal"))
        self.assertIsNotNone(grep.wait_timeout_ms, "grep waits for indexing (the server's default wait is 0)")
        self.assertTrue(read.start_line and read.end_line and read.end_line - read.start_line + 1 <= lines)
        self.assertFalse(read.all_lines)
        self.assertEqual((grep.context_lines_before, grep.context_lines_after), (None, None))
        self.assertFalse(any(args.json for args in (search, read, grep)), "the agent reads the compact text")
        targets = {*search.targets, read.target, *grep.targets}
        self.assertEqual(len(targets), 1, "one package at one pinned version throughout")
        self.assertRegex(targets.pop(), r"^[a-z]+:[A-Za-z0-9_.-]+@\d[\w.]*$", "registry:package@version")

    def test_the_grep_claims_hold_in_the_pinned_contract(self):
        """"it searches source, changelogs and docs", and why grep waits: the skill's grep sends no corpus, so the
        server's default (all: the target's files and its hosted docs) applies, and the server waits 0 ms for grep by
        default, so without --wait-ms a target still being indexed would read as no match."""
        schemas = api.load_spec()["components"]["schemas"]
        default_wait = schemas["GrepRequest"]["properties"]["wait_timeout_ms"]["default"]
        self.assertEqual((schemas["GrepTarget"]["properties"]["corpus"]["default"], default_wait), ("all", 0))
        self.assertIn("it searches source, changelogs and docs", self.template)
        [grep] = [self.parser.parse_args(words_of(c)) for c in self.commands if words_of(c)[0] == "grep"]
        self.assertIsNone(grep.corpus, "no corpus is sent, so the server's default applies")
        self.assertGreater(grep.wait_timeout_ms, default_wait)

    def test_the_stop_rule_names_what_the_launcher_prints(self):
        """"Stop at a `refused:` or `error:` line" and "Results the host drops (license, excluded owner)": the
        launcher's own words for a refused target, a failed call and dropped results."""
        self.assertIn("`refused:` or `error:` line", self.template)
        self.assertIn("(license, excluded owner)", self.template)
        code, _, err = api.run_entry(self.env, "search", "Retry", "--target", "github:SyberLabs/SyberWork")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("refused: "), err)
        no_token = {name: value for name, value in self.env.items() if name != githits.TOKEN_ENV}
        code, _, err = api.run_entry(no_token, "search", "Retry", "--target", "pypi:urllib3@2.2.1")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "), err)
        dropped = githits.Result("search", githits.OK, api.search_response([]), 200, None, 0, 1, False, None, 1)
        self.assertIn("1 dropped (excluded owner), 1 dropped (license)", githits.render(dropped))

    def test_the_githits_clis_own_flags_are_usage_errors_here(self):
        """"Flags: only those above; this is not the githits CLI": forms from the upstream skill
        (githits-com/githits-cli skills/githits-code/SKILL.md at the pinned commit) exit 2, send and record nothing."""
        self.assertIn("this is not the githits CLI", self.template)
        for words in (["search", "router", "--target", "npm:express@5.2.1", "--in", "npm:express@5.2.1"],
                      ["grep", "router", "--target", "npm:express@5.2.1", "-F"],
                      ["grep", "router", "--target", "npm:express@5.2.1", "-C", "3"],
                      ["read", "npm:express@5.2.1", "--path", "lib/express.js", "--lines", "1-90"]):
            with self.subTest(words=words):
                self.assertEqual(api.run_entry(self.env, *words)[0], 2)
        self.assertEqual((self.fake.requests, self.record_text()), ([], ""))

    def test_grep_context_lines_would_never_reach_the_agent(self):
        """Why the skill reads a bounded window instead of passing -A/-B: the compact text prints the match line
        only, so context lines would cost bytes and show nothing."""
        hit = api.grep_hit(12, repo_url="https://github.com/psf/requests")
        hit["context_after_slices"] = [{"content": "NEXT-LINE", "start_byte": 0, "end_byte": 9,
                                        "original_line_bytes": 9}]
        result = githits.Result("grep", githits.OK, api.grep_response([hit]), 200, None, 1, 0, False, None)
        self.assertNotIn("NEXT-LINE", githits.render(result))


class SkillShellRule(unittest.TestCase):
    """The skill's "Shell:" rule names exactly the characters the guard treats specially."""

    def setUp(self):
        [line] = [text for text in githits_skill()[0].splitlines() if text.startswith("- Shell:")]
        self.quoted, self.never, self.good, self.bad = re.findall(r"`([^`]+)`", line)[:4]

    def test_what_it_says_never_to_type_is_blocked_even_inside_quotes(self):
        never = set(self.never.split()) | {"`"}  # the backtick is named in words
        self.assertEqual(never, set(query_guard._SHELL_FORBIDDEN))
        for ch in sorted(never):
            with self.subTest(ch=ch):
                self.assertEqual(judge(f'"retry{ch}backoff"'), query_guard.SHELL_SYNTAX)
                self.assertEqual(judge(f"retry{ch}backoff"), query_guard.SHELL_SYNTAX)
        self.assertEqual((judge(self.good), judge(self.bad)), (query_guard.PASS, query_guard.SHELL_SYNTAX))

    def test_what_it_says_to_quote_is_blocked_bare_and_passes_quoted(self):
        printable = {chr(code) for code in range(0x20, 0x7F)}
        plain = {ch for ch in printable if query_guard._PLAIN.fullmatch(ch)}
        quoted = set(self.quoted.split()) | {" "}  # a space is named in words
        self.assertEqual(quoted, printable - plain - set(query_guard._SHELL_FORBIDDEN) - {'"'})
        for ch in sorted(quoted - {" "}):
            with self.subTest(ch=ch):
                self.assertEqual(judge(f"retry{ch}backoff"), query_guard.SHELL_SYNTAX)
                self.assertEqual(judge(f'"retry{ch}backoff"'), query_guard.PASS)
        _, words = query_guard.bash_arguments(f'{LAUNCHER} search "retry backoff" --target pypi:x', LAUNCHER)
        self.assertEqual(words[1], "retry backoff", "a quoted space stays inside one argument")


if __name__ == "__main__":
    unittest.main()
