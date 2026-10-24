"""Claude Code as a proposer: the repair loop with ``claude -p`` as the call, usage and lookups read from its stream.

No model is called and no Claude Code runs here. ``claude`` is a stand-in Python script that logs its argv, its
prompt and whether an API key reached it (outside the repository), writes files in its working directory, and
prints a recorded ``stream-json`` transcript of two turns: one ``mcp__githits__search`` lookup, one ``WebSearch``
lookup and one ``Edit``.
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from spec.validate import validate_events
from syberlabs import Rejected, metrics
from syberlabs.claude_code import (BIN_ENV, DEFAULT_TOOLS, LOOKUP, ClaudeCodeProvider, Transcript, parse_tools,
                                   query_text)
from syberlabs.cli import main as cli
from syberlabs.events import verify_events
from syberlabs.providers import RepairProvider
from tests.test_build_thread import GOOD, REPORT, RepoCase

PY = sys.executable
BROKEN = {**GOOD, "src/report.py": "def rows():\n    return []\n"}  # tests fail: the report rows are gone
GITHITS_QUERY = "zq81 githits query: how does requests retry on 503"
WEB_QUERY = "zq82 web query: python csv DictWriter newline windows"
PRICES = {"claude-opus-5": {"input_per_million": 15, "output_per_million": 75}}
# The two turns' usage, as the stand-in prints it (each message's usage appears on every block event of it).
TURNS = [{"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10},
         {"input_tokens": 1500, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]
SUMMED = {"input_tokens": 2500, "output_tokens": 500, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10,
          "model": "claude-opus-5", "stop_reason": "success"}
# The host's arithmetic under PRICES (cache tokens at the input rate when the table has no cache rate).
COST = round((2500 + 50 + 10) * 15 / 1_000_000 + 500 * 75 / 1_000_000, 6)

STAND_IN = r'''
import json, os, sys
from pathlib import Path

config = json.loads(Path(CONFIG).read_text(encoding="utf-8"))
prompt = sys.stdin.read()
log = Path(LOG)
calls = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                        "api_key": "ANTHROPIC_API_KEY" in os.environ,
                        "seed": "SYBERLABS_CERTIFICATE_SEED" in os.environ}) + "\n")
if config.get("exit"):
    print("not json: a warning", flush=True)
    sys.exit(config["exit"])
repair = "did not pass the host's checks" in prompt
for path, text in config["repair" if repair else "first"].items():
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
servers = [{"name": "githits", "status": "connected"}] if "--mcp-config" in sys.argv else []
bare = "--bare" in sys.argv
turns, model = config["turns"], "claude-opus-5"

def say(event):
    print(json.dumps(event), flush=True)

say({"type": "system", "subtype": "init", "claude_code_version": "2.1.309", "model": model, "effort": "high",
     "apiKeySource": "ANTHROPIC_API_KEY" if bare else "none", "mcp_servers": servers, "session_id": "x",
     "tools": ["Read", "Edit"]})
first = {"id": "msg_1", "model": model, "usage": turns[0]}
say({"type": "assistant", "message": {**first, "content": [{"type": "text", "text": "Looking up " + config["githits"]}]},
     "parent_tool_use_id": None})
say({"type": "assistant", "message": {**first, "content": [{"type": "tool_use", "id": "t1", "name": "mcp__githits__search",
                                                              "input": {"query": config["githits"]}}]},
     "parent_tool_use_id": None})
say({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                              "content": "result " + config["githits"]}]}})
second = {"id": "msg_2", "model": model, "usage": turns[1]}
say({"type": "assistant", "message": {**second, "content": [{"type": "tool_use", "id": "t2", "name": "WebSearch",
                                                               "input": {"query": config["web"]}}]}})
say({"type": "assistant", "message": {**second, "content": [{"type": "tool_use", "id": "t3", "name": "Edit",
                                                               "input": {"file_path": "x"}}]}})
say({"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "t2", "content": "pages about " + config["web"]},
    {"type": "tool_result", "tool_use_id": "t3", "content": "ok"}]}})
say({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "done: " + config["web"],
     "total_cost_usd": 0.0421, "session_id": "x", "usage": {}, "modelUsage": {}})
'''


class ClaudeCodeCase(RepoCase):
    def stand_in(self, name, *, first=None, repair=None, exit=0):
        """A stand-in ``claude``: ``first`` is what it writes on a first call, ``repair`` when the prompt quotes failed
        checks. Returns the argv prefix that runs it."""
        config = self.root.parent / f"{name}.json"
        config.write_text(json.dumps({"first": first if first is not None else GOOD, "repair": repair or {},
                                      "exit": exit, "turns": TURNS, "githits": GITHITS_QUERY, "web": WEB_QUERY}),
                          encoding="utf-8")
        script = self.root.parent / f"{name}.py"
        script.write_text(f"CONFIG = {str(config)!r}\nLOG = {str(self.root.parent / (name + '.log'))!r}\n"
                          + textwrap.dedent(STAND_IN), encoding="utf-8")
        return [PY, str(script)]

    def runs(self, name):
        log = self.root.parent / f"{name}.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def journal_bytes(self) -> bytes:
        return b"".join(p.read_bytes() for p in (self.root / ".syberlabs").rglob("*") if p.is_file())

    def provider(self, kit, thread, claude, **kwargs):
        def observe(key, value, version):
            kit.session.observe(thread.id, key, value, "syberlabs.host", version, "syberlabs.host", verified=True)
        kwargs.setdefault("trusted_tree", True)
        return ClaudeCodeProvider(kit.repo, executable=claude, observe=observe, **kwargs)


class OneShot(ClaudeCodeCase):
    def test_the_change_usage_turns_tools_and_lookups_come_from_the_worktree_and_the_stream(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        claude = self.stand_in("one")
        provider = self.provider(kit, thread, claude, max_calls=1)
        [candidate] = thread.propose(provider)
        self.assertEqual(candidate.changed_paths, tuple(sorted(GOOD)), "the paths the stand-in wrote in the worktree")
        self.assertEqual(candidate.evaluation, "passed")
        for path, text in GOOD.items():
            self.assertEqual(thread.kit.repo.read(candidate.commit, path).decode(), text, "the file as written, not prose")
        self.assertTrue(self.clean(), "the developer's working tree is never touched; the run had its own worktree")
        [run] = self.runs("one")
        self.assertNotEqual(Path(run["cwd"]).resolve(), self.root.resolve(), "Claude Code ran in a temporary worktree")
        self.assertFalse(Path(run["cwd"]).exists(), "and the worktree is gone")
        signal = candidate.signal
        self.assertEqual(signal["model_usage"], SUMMED, "one usage per message id, summed over the two turns")
        self.assertEqual(signal["harness_turns"], 2)
        self.assertEqual(signal["tool_usage"], {"web_search": 1, "web_fetch": 0, "githits": 1, "other": {"Edit": 1}})
        self.assertEqual((signal["lane"], signal["cost_basis"], signal["estimated_usd"]), ("subscription_local", "none", 0.0421))
        self.assertEqual(signal["harness"], {"name": "claude-code", "version": "2.1.309", "model": "claude-opus-5",
                                             "effort": "high", "auth_source": "none"})
        self.assertNotIn("model_usage_by_model", signal, "one model served the run")
        history = thread.history()
        lookups = [e["body"] for e in history if e["kind"] == "observed" and e["body"]["key"] == LOOKUP]
        self.assertEqual([(b["version"], b["verified"], b["actor"], b["source"]) for b in lookups],
                         [("s1.1.1", True, "syberlabs.host", "syberlabs.host"), ("s1.1.2", True, "syberlabs.host", "syberlabs.host")])
        values = [b["value"] for b in lookups]
        self.assertEqual([(v["tool"], v["query_digest"], v["query_chars"], v["ok"]) for v in values], [
            ("mcp__githits__search", hashlib.sha256(GITHITS_QUERY.encode()).hexdigest(), len(GITHITS_QUERY), True),
            ("WebSearch", hashlib.sha256(WEB_QUERY.encode()).hexdigest(), len(WEB_QUERY), True)])
        self.assertTrue(all(v["at"].endswith("Z") for v in values))
        self.assertEqual(provider.lookups, values)
        registered = next(i for i, e in enumerate(history) if e["kind"] == "candidate_registered")
        self.assertTrue(all(history.index(e) < registered for e in history if e["kind"] == "observed"
                            and e["body"]["key"] == LOOKUP), "a call's lookups are recorded before its candidate")
        record = self.journal_bytes()
        for text in (GITHITS_QUERY, WEB_QUERY, "zq81", "zq82"):
            self.assertNotIn(text.encode(), record, "no query text anywhere in the journal")
        self.assertNotIn(b"Looking up", record, "nor the model's prose")
        self.assertEqual(validate_events(history, "claude-code"), [])
        self.assertTrue(verify_events(history))
        # The metrics suite folds the same record.
        [item] = metrics.work_items(history)
        self.assertEqual((item.githits_lookups.value, item.web_lookups.value, item.turns.value), (1, 1, 2))
        self.assertEqual((item.lane, item.cost.value), ("subscription_local", None))
        self.assertIn("no per-call bill", item.cost.note)
        self.assertIn("subscription_local", metrics.table([item]))

    def test_the_prompt_is_the_adapters_prompt_for_the_same_request(self):
        from adapters.anthropic_adapter import build_prompt

        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        claude = self.stand_in("prompt")
        thread.propose(self.provider(kit, thread, claude, max_calls=1))
        [run] = self.runs("prompt")
        self.assertTrue(run["prompt"].startswith("Objective: Add a CSV export\n\nMutable scope: src, tests"))
        self.assertIn("Propose one patch that meets the objective.", run["prompt"])
        self.assertIn('<file path="src/report.py">', run["prompt"], "the base's files, read from the worktree")
        request = {"protocol": "syberlabs.search/v0alpha1", "objective": "Add a CSV export", "scope": ["src", "tests"],
                   "files": ["src/report.py"], "context": []}
        self.assertEqual(build_prompt(request, self.root).split("\n\n")[:3], run["prompt"].split("\n\n")[:3])


class Repair(ClaudeCodeCase):
    def test_a_second_call_runs_on_the_failed_candidate_and_carries_its_failed_checks(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        claude = self.stand_in("repair", first=BROKEN, repair={"src/report.py": REPORT})
        provider = self.provider(kit, thread, claude)
        found = thread.propose(provider, seed=4)
        self.assertEqual([(c.id, c.evaluation, c.parents) for c in found], [("c1", "failed", ()), ("c2", "passed", ("c1",))])
        first, second = self.runs("repair")
        self.assertNotIn("did not pass", first["prompt"])
        self.assertIn("The previous attempt below did not pass the host's checks. Fix it. Failing checks:\n--- tests (failed)",
                      second["prompt"])
        self.assertIn('<file path="src/report.py">\ndef rows():\n    return []', second["prompt"], "the failed candidate's texts")
        self.assertEqual(set(found[1].changed_paths), set(GOOD), "c2 restored report.py on top of c1; against base only GOOD differs")
        self.assertEqual([t.get("outcome") for t in provider.trace[:-1]], ["failed", "passed"])
        self.assertEqual(provider.trace[-1]["stopped"], "passing")
        versions = [e["body"]["version"] for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == LOOKUP]
        self.assertEqual(versions, ["s1.1.1", "s1.1.2", "s1.2.1", "s1.2.2"])
        [item] = metrics.work_items(thread.history())
        self.assertEqual((item.turns.value, item.githits_lookups.value, item.web_lookups.value, item.fix_rounds.value), (4, 2, 2, 1))

    def test_a_failed_run_is_a_provider_error_and_an_unchanged_tree_is_refused(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        self.assertEqual(thread.propose(self.provider(kit, thread, self.stand_in("fails", exit=3))), [])
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual((search["stopped"], search["error"]), ("error", "provider_failed"))
        self.assertEqual(thread.propose(self.provider(kit, thread, self.stand_in("nothing", first={}), max_calls=2)), [])
        refused = [e["body"]["value"] for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == "proposal_refused"]
        self.assertEqual([r["reason"] for r in refused], ["provider_error", "no_change", "no_change"])
        self.assertEqual(refused[-1]["model_usage"], SUMMED, "the call's usage is on its refusal")


class Lanes(ClaudeCodeCase):
    def test_bare_is_the_api_lane_billed_at_the_contracts_prices(self):
        budget = {"max_candidates": 6, "max_evaluations": 6, "max_seconds": 600, "prices": PRICES, "max_dollars": 1.0}
        kit = self.kit(self.contract(**{"evolution.budget": budget}))
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-placeholder", "SYBERLABS_CERTIFICATE_SEED": "00"}):
            [candidate] = thread.propose(ClaudeCodeProvider(kit.repo, bare=True, executable=self.stand_in("bare"), max_calls=1))
        [run] = self.runs("bare")
        self.assertIn("--bare", run["argv"])
        self.assertEqual((run["api_key"], run["seed"]), (True, False), "the key reaches a bare run; a signing seed never does")
        self.assertEqual((candidate.signal["lane"], candidate.signal["cost_basis"]), ("api", "billed"))
        body = [e["body"] for e in thread.history() if e["kind"] == "candidate_registered"][-1]
        self.assertEqual((body["spend"]["dollars"], body["spend"]["host_observed"]), (COST, True))
        [item] = metrics.work_items(thread.history())
        self.assertEqual((item.cost_basis, item.cost.value, item.lane), ("host_observed", COST, "api"))
        self.assertIn("lane api, cost_basis billed", item.cost.note)
        self.assertIn("host_observed, api", metrics.table([item]))

    def test_an_inherited_login_is_subscription_local_unpriced_and_never_sees_an_api_key(self):
        budget = {"max_candidates": 6, "max_evaluations": 6, "max_seconds": 600, "prices": PRICES, "max_dollars": 1.0}
        kit = self.kit(self.contract(**{"evolution.budget": budget}))
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-placeholder"}):
            [candidate] = thread.propose(self.provider(kit, thread, self.stand_in("login"), max_calls=1))
        [run] = self.runs("login")
        self.assertNotIn("--bare", run["argv"])
        self.assertFalse(run["api_key"], "a subscription run must not silently bill an API key")
        self.assertEqual((candidate.signal["lane"], candidate.signal["cost_basis"]), ("subscription_local", "none"))
        body = [e["body"] for e in thread.history() if e["kind"] == "candidate_registered"][-1]
        self.assertEqual((body["spend"]["dollars"], body["spend"]["host_observed"]), (0.0, False), "nothing priced")
        self.assertEqual(candidate.signal["estimated_usd"], 0.0421, "Claude Code's estimate is recorded as an estimate")
        declared = ClaudeCodeProvider(kit.repo, trusted_tree=True, lane="seat_team_enterprise", executable=["x"])
        self.assertEqual((declared.lane, declared.cost_basis), ("seat_team_enterprise", "none"))
        with self.assertRaises(Rejected) as caught:
            ClaudeCodeProvider(kit.repo, bare=True, lane="subscription_local", executable=["x"])
        self.assertEqual(caught.exception.code, "invalid_provider")

    def test_a_non_bare_run_on_an_undeclared_tree_is_refused_and_says_why(self):
        kit = self.kit()
        with self.assertRaises(Rejected) as caught:
            ClaudeCodeProvider(kit.repo, executable=["x"])
        self.assertEqual(caught.exception.code, "untrusted_tree")
        self.assertIn("hooks", caught.exception.detail)
        self.assertIn(".mcp.json", caught.exception.detail)
        self.assertIn("--trusted-tree", caught.exception.detail)
        ClaudeCodeProvider(kit.repo, bare=True, executable=["x"])  # bare needs no declaration


class GitHitsAB(ClaudeCodeCase):
    def test_two_arms_differ_only_in_the_mcp_config_flags(self):
        kit = self.kit()
        config = self.root.parent / "githits.json"
        config.write_text(json.dumps({"mcpServers": {"githits": {"command": "githits-stand-in"}}}), encoding="utf-8")
        claude = self.stand_in("ab")
        off = ClaudeCodeProvider(kit.repo, bare=True, executable=claude)
        on = ClaudeCodeProvider(kit.repo, bare=True, executable=claude, mcp_config=config)
        self.assertEqual(on.command(), off.command() + ["--mcp-config", str(config.resolve())])
        command = off.command()
        for flag in ("-p", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, command)
        for flag, value in (("--output-format", "stream-json"), ("--permission-mode", "dontAsk"),
                            ("--allowedTools", ",".join(DEFAULT_TOOLS)), ("--setting-sources", "user")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--verbose", command)
        # Both arms run through the same factory on the same work item; only the flags differ in what ran.
        for name, provider in (("off", off), ("on", on)):
            thread = kit.start(f"Add a CSV export ({name})", paths=["src/", "tests/"])
            provider.max_calls = 1
            [candidate] = thread.propose(provider)
            harness = candidate.signal["harness"]
            self.assertEqual(harness.get("mcp_servers"), ["githits:connected"] if name == "on" else None)
        off_run, on_run = self.runs("ab")
        self.assertEqual(on_run["argv"], off_run["argv"] + ["--mcp-config", str(config.resolve())])
        self.assertEqual(on_run["prompt"].split("\n\n")[0], "Objective: Add a CSV export (on)")
        self.assertEqual(parse_tools("Read, Edit mcp__githits"), ["Read", "Edit", "mcp__githits"])


class StreamParsing(unittest.TestCase):
    def test_server_tools_errors_and_unparsable_lines(self):
        lines = [("t0", "not json"),
                 ("t1", json.dumps({"type": "assistant", "message": {"id": "a", "model": "m", "usage": {"input_tokens": 3},
                                    "content": [{"type": "server_tool_use", "id": "s1", "name": "web_fetch",
                                                 "input": {"url": "https://example.test/x"}},
                                                {"type": "web_fetch_tool_result", "tool_use_id": "s1",
                                                 "content": {"type": "web_fetch_tool_result_error"}}]}})),
                 ("t2", json.dumps({"type": "assistant", "message": {"id": "b", "model": "n", "usage": {"output_tokens": 4},
                                    "content": [{"type": "tool_use", "id": "u1", "name": "mcp__githits__get_example",
                                                 "input": {"package": "requests"}}]}})),
                 ("t3", json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1",
                                                                             "is_error": True}]}})),
                 ("t4", json.dumps({"type": "result", "subtype": "error_max_turns", "num_turns": 2}))]
        transcript = Transcript(lines)
        self.assertEqual(transcript.tool_usage(), {"web_search": 0, "web_fetch": 1, "githits": 1})
        self.assertEqual([(x["tool"], x["ok"], x["at"]) for x in transcript.lookups()],
                         [("web_fetch", False, "t1"), ("mcp__githits__get_example", False, "t2")])
        self.assertEqual(transcript.lookups()[1]["query_digest"],
                         hashlib.sha256(query_text({"package": "requests"}).encode()).hexdigest())
        usage = transcript.usage()
        self.assertNotIn("model", usage, "two models served the run: no single model to price the sum at")
        self.assertEqual(set(transcript.usage_by_model()), {"m", "n"})
        self.assertEqual(usage["stop_reason"], "error_max_turns")

    def test_the_provider_is_the_repair_loop(self):
        self.assertTrue(issubclass(ClaudeCodeProvider, RepairProvider))


class CommandLine(ClaudeCodeCase):
    def run_cli(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, env or {}):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"),
                        "--actor", "dev@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def test_propose_claude_code_from_the_terminal(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("start", "Add a CSV export for report rows", "--paths", "src", "tests")[0], 0)
        env = {BIN_ENV: " ".join(self.stand_in("cli"))}
        code, _, err = self.run_cli("propose", "--claude-code", env=env)
        self.assertEqual(code, 2)
        self.assertIn("untrusted_tree", err)
        self.assertEqual(self.runs("cli"), [], "refused before anything ran")
        code, out, err = self.run_cli("propose", "--claude-code", "--trusted-tree", "--max-calls", "1",
                                      "--allowed-tools", "Read,Edit,mcp__githits", "--max-turns", "8", env=env)
        self.assertEqual(code, 0, out + err)
        self.assertIn("c1: 2 files", out)
        self.assertIn("search s1 completed; provider recommends c1", out)
        [run] = self.runs("cli")
        self.assertEqual(run["argv"][run["argv"].index("--allowedTools") + 1], "Read,Edit,mcp__githits")
        self.assertEqual(run["argv"][run["argv"].index("--max-turns") + 1], "8")
        self.assertIn("[claude-code@1]", self.run_cli("status")[1])
        code, out, _ = self.run_cli("metrics", "--json")
        self.assertEqual(code, 0)
        [item] = json.loads(out)["items"]
        self.assertEqual((item["lane"], item["turns"]["value"], item["githits_lookups"]["value"]), ("subscription_local", 2, 1))


if __name__ == "__main__":
    unittest.main()
