"""Contract 2's receiving end as it runs today: the ``syberlabs`` steps Syber's SyberWork builder calls.

SyberLabs/syber ``lib/factory/syberwork-builder.mjs`` (read at 5a4667f, 2026-11-04) runs each job as a Build
Thread by starting this CLI in the job's worktree, with no ``--repo`` or ``--home``:

    init
    start <objective> --class R
    propose --claude-code --bare --lane provider_3p --max-calls N --name syber-line0 --revision 1
    status --json            reads thread, candidates[].id, candidates[].evaluation == "passed", candidates[].in_scope
    check <id> --json        reads acceptable, reason, checks[].name, checks[].state
    diff <id>                counts the unified diff's +/- lines when several candidates passed
    accept <id>
    status --json            reads target (40 hex) and accepted == <id>
    export                   writes stdout to a file as the thread's journal

These tests pin exactly what that builder relies on, and no more of the output, so a change here that would
break Syber fails in this repository first. They are not a protocol: the S0 Agent protocol (``spec/v1/``) is the
seam's specification, and no runtime implements it yet.
"""

import contextlib
import io
import json
import re
import sys
import unittest
from unittest import mock

from syberlabs import Rejected
from syberlabs.cli import main as cli
from tests.test_build_thread import GOOD, RepoCase, git

SYBER_OBJECTIVE = "Work order 1: add a CSV export for report rows"
SYBER_PROPOSE = ["propose", "--claude-code", "--bare", "--lane", "provider_3p", "--max-calls", "2",
                 "--name", "syber-line0", "--revision", "1"]
BROKEN = {**GOOD, "src/report.py": "def rows():\n    return []\n"}  # the existing report test fails


class SyberBuilderSeam(RepoCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"),
                        "--actor", "syber@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def json_of(self, *args):
        code, out, err = self.run_cli(*args)
        try:
            return code, json.loads(out)
        except ValueError:
            self.fail(f"`syberlabs {' '.join(args)}` did not print JSON (exit {code}): {out[-400:]} {err[-400:]}")

    def propose(self, changes):
        for path, text in changes.items():
            (self.root / path).write_text(text)
        code, out, err = self.run_cli("propose", "--from-worktree", "--name", "syber-line0", "--revision", "1")
        self.assertEqual(code, 0, out + err)

    def test_a_passing_candidate_reads_back_through_the_fields_syber_uses(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        code, out, err = self.run_cli("start", SYBER_OBJECTIVE, "--class", "R")
        self.assertEqual(code, 0, out + err)
        self.propose(GOOD)

        code, status = self.json_of("status", "--json")
        self.assertEqual(code, 0)
        thread = status["thread"]
        self.assertIsInstance(thread, str)
        self.assertTrue(thread)
        [candidate] = status["candidates"]
        self.assertEqual(candidate["id"], "c1")
        self.assertIs(candidate["in_scope"], True)
        self.assertNotEqual(candidate["evaluation"], "passed", "not passed before the host checked it")

        code, verdict = self.json_of("check", "c1", "--json")
        self.assertEqual(code, 0)
        self.assertIs(verdict["acceptable"], True)
        self.assertIsInstance(verdict["reason"], str)
        self.assertTrue(verdict["reason"])
        self.assertIn(("tests", "passed"), [(c["name"], c["state"]) for c in verdict["checks"]])

        code, status = self.json_of("status", "--json")
        self.assertEqual([(c["id"], c["evaluation"], c["in_scope"]) for c in status["candidates"]],
                         [("c1", "passed", True)], "Syber's passedCandidates filter: in_scope and evaluation passed")

        code, diff, _ = self.run_cli("diff", "c1")
        self.assertEqual(code, 0)
        self.assertIn("+++ b/src/export.py", diff)
        self.assertTrue(any(line.startswith("+") and not line.startswith("+++") for line in diff.splitlines()),
                        "Syber counts the added and removed lines below the headers")

        code, out, err = self.run_cli("accept", "c1")
        self.assertEqual(code, 0, out + err)
        code, after = self.json_of("status", "--json")
        self.assertEqual(after["accepted"], "c1")
        self.assertRegex(after["target"], r"^[0-9a-f]{40}$")
        self.assertEqual(after["target"], git(self.root, "rev-parse", after["target_ref"]),
                         "the commit Syber lays into the job's worktree is the thread branch's tip")

        code, record = self.json_of("export")
        self.assertEqual(code, 0)
        self.assertIs(record["chain_valid"], True)
        self.assertEqual(record["status"]["accepted"], "c1")

    def test_a_failing_candidate_is_not_passed_and_its_check_says_why(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("start", SYBER_OBJECTIVE, "--class", "R")[0], 0)
        self.propose(BROKEN)

        code, verdict = self.json_of("check", "c1", "--json")
        self.assertEqual(code, 1)
        self.assertIs(verdict["acceptable"], False)
        self.assertIsInstance(verdict["reason"], str)
        self.assertTrue(verdict["reason"], "Syber reports the refusal as `<id>: <reason>`")

        code, status = self.json_of("status", "--json")
        [candidate] = status["candidates"]
        self.assertNotEqual(candidate["evaluation"], "passed", "Syber would not take it")
        self.assertIsNone(status["accepted"])

    def test_syber_propose_argv_builds_the_claude_code_provider_it_asks_for(self):
        """Syber's propose line must parse and build a provider under today's refusals (#162 made ``--bare`` refuse
        unscreened lookups): routed through a key proxy (``provider_3p``), bare, Syber's name and revision, no
        GitHits. No Claude Code runs: the executable answers ``--version`` only, and the search is stopped before
        any call."""
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("start", SYBER_OBJECTIVE, "--class", "R")[0], 0)
        seen = []

        def stop(thread, provider, **kwargs):
            seen.append(provider)
            raise Rejected("seam_stop", "stopped before any call")

        version_only = [sys.executable, "-c", "print('2.1.312 (Claude Code)')"]
        with mock.patch("syberlabs.claude_code.claude_executable", return_value=version_only), \
                mock.patch("syberlabs.build.Thread.propose", stop):
            code, _, err = self.run_cli(*SYBER_PROPOSE)
        self.assertEqual(code, 2)
        self.assertIn("seam_stop", err, "the provider was built; nothing refused Syber's argv")
        [provider] = seen
        self.assertEqual((provider.bare, provider.lane, provider.cost_basis), (True, "provider_3p", "estimated"))
        self.assertEqual((provider.name, provider.revision, provider.max_calls), ("syber-line0", "1", 2))
        self.assertIs(provider.githits, False)
        self.assertEqual(provider.mcp_config, None)
        self.assertFalse(any(re.match(r"^(Bash|PowerShell|WebSearch|WebFetch)$", t) for t in provider.allowed_tools),
                         "Syber's builder relies on the default tools: no shell, no web lookups")


if __name__ == "__main__":
    unittest.main()
