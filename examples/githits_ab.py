"""A rehearsal of the GitHits A/B pilot: the whole pipeline, with a stand-in where the model would be.

Seven work items run through both arms of the factory's default proposer path (the repair loop) on a throwaway
repository: real Build Threads, real host checks, real acceptances, a real journal, then ``syberlabs metrics`` and
``syberlabs experiment report`` over it. Two routes for arm B's GitHits:

- ``--route api`` (the default; the pilot's route since #134): both arms run the real Claude Code proposer,
  ``ClaudeCodeProvider`` on the api-hooked lane (the API key without ``--bare``, so the guard's hook runs), arm B
  with ``githits=True``. For each call the host writes the GitHits
  launcher, its query guard judges every lookup and command, the host's own GitHits client makes each lookup and signs
  its record, and the host journals one ``lookup`` per record and its ledger of the call on the candidate
  (``signal.githits``), which is what the report reads the arm from. Only the ``claude`` executable is a stand-in: a
  script that prints Claude Code's ``stream-json``, runs the ``--settings`` hook before each tool call as Claude Code
  does, and runs the launcher when its Bash tool is allowed to; GitHits is a loopback stand-in that answers searches.
  Nothing leaves the machine.
- ``--route mcp`` (the route before #134): the stand-in is a search-protocol command (``RepairProvider``) that
  declares its usage, lookups and connected MCP servers itself.

On either route:

- on an item that depends on a library's behaviour (tag ``needs_lookup``) the stand-in guesses wrong once when GitHits
  is off, so the host's checks fail and the repair call fixes it; with GitHits on it gets it right first time;
- on a routine item it is right first time in both arms (with GitHits on it looks one thing up and has nothing to add);
- on item ``LIB-4`` the treatment is misconfigured, so the report must refuse to count that item: on the API route it
  runs without ``--githits`` (no launcher), on the MCP route its GitHits server "fails to connect";
- every accepted change gets a blind review (``syberlabs review --record``), and one is turned down, so delivery is
  "accepted and passed review", as the pilot defines it.

The stand-ins' token usage and lookups are made up to exercise the report. **They are not results and say nothing
about GitHits.** The pilot itself runs a real model with a paid key (Class X: a named person runs it;
``docs/EXPERIMENTS.md`` and ``docs/GITHITS.md`` have the commands).

    python examples/githits_ab.py [--route api|mcp] [--out DIR]   # DIR receives report.json, report.md, metrics.csv
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs import Kit, experiment, githits, metrics  # noqa: E402
from syberlabs.build import default_contract  # noqa: E402
from syberlabs.claude_code import THIRD_PARTY_SWITCHES, ClaudeCodeProvider  # noqa: E402
from syberlabs.providers import RepairProvider  # noqa: E402

NAME = "githits-rehearsal"
ROUTES = ("api", "mcp")
RUNNER = "runner@example.test"
REVIEWER = "reviewer@example.test"
PRICES = {"stand-in-model": {"input_per_million": 3, "output_per_million": 15}}  # a made-up price for a made-up model
MISCONFIGURED = "LIB-4"  # the item whose treatment ran without GitHits


def module(name: str, body: str, test: str) -> dict:
    return {f"src/{name}.py": textwrap.dedent(body),
            f"tests/test_{name}.py": "import os, sys, unittest\n"
                                     "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
                                     + textwrap.dedent(test)}


# Each item: (objective, tag, the right change, the stand-in's wrong first guess or None).
ITEMS = {
    "LIB-1": ("Parse ISO dates with datetime, keeping the UTC offset", "needs_lookup",
              module("dates", '''\
                  from datetime import datetime
                  def parse(text):
                      return datetime.fromisoformat(text)
                  ''', '''\
                  import dates
                  class Dates(unittest.TestCase):
                      def test_offset_kept(self):
                          self.assertEqual(dates.parse("2026-10-24T09:00:00+03:00").utcoffset().total_seconds(), 10800)
                  '''),
              module("dates", '''\
                  from datetime import datetime
                  def parse(text):
                      return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
                  ''', '''\
                  import dates
                  class Dates(unittest.TestCase):
                      def test_offset_kept(self):
                          self.assertEqual(dates.parse("2026-10-24T09:00:00+03:00").utcoffset().total_seconds(), 10800)
                  ''')),
    "LIB-2": ("Write CSV rows with csv.DictWriter, header first", "needs_lookup",
              module("rows", '''\
                  import csv, io
                  def to_csv(rows):
                      out = io.StringIO()
                      writer = csv.DictWriter(out, fieldnames=["name", "qty"], lineterminator="\\n")
                      writer.writeheader()
                      writer.writerows(rows)
                      return out.getvalue()
                  ''', '''\
                  import rows
                  class Rows(unittest.TestCase):
                      def test_header(self):
                          self.assertEqual(rows.to_csv([{"name": "a", "qty": 2}]), "name,qty\\na,2\\n")
                  '''),
              module("rows", '''\
                  import csv, io
                  def to_csv(rows):
                      out = io.StringIO()
                      writer = csv.DictWriter(out, fieldnames=["name", "qty"])
                      writer.writerows(rows)
                      return out.getvalue()
                  ''', '''\
                  import rows
                  class Rows(unittest.TestCase):
                      def test_header(self):
                          self.assertEqual(rows.to_csv([{"name": "a", "qty": 2}]), "name,qty\\na,2\\n")
                  ''')),
    "LIB-3": ("Round prices half-up to cents with decimal", "needs_lookup",
              module("money", '''\
                  from decimal import Decimal, ROUND_HALF_UP
                  def cents(value):
                      return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                  ''', '''\
                  import money
                  class Money(unittest.TestCase):
                      def test_half_up(self):
                          self.assertEqual(money.cents("2.675"), "2.68")
                  '''),
              module("money", '''\
                  def cents(value):
                      return f"{round(float(value), 2):.2f}"
                  ''', '''\
                  import money
                  class Money(unittest.TestCase):
                      def test_half_up(self):
                          self.assertEqual(money.cents("2.675"), "2.68")
                  ''')),
    "LIB-4": ("Join URL paths with urllib.parse.urljoin", "needs_lookup",
              module("links", '''\
                  from urllib.parse import urljoin
                  def link(base, path):
                      return urljoin(base if base.endswith("/") else base + "/", path)
                  ''', '''\
                  import links
                  class Links(unittest.TestCase):
                      def test_join(self):
                          self.assertEqual(links.link("https://x.test/docs", "a"), "https://x.test/docs/a")
                  '''),
              module("links", '''\
                  from urllib.parse import urljoin
                  def link(base, path):
                      return urljoin(base, path)
                  ''', '''\
                  import links
                  class Links(unittest.TestCase):
                      def test_join(self):
                          self.assertEqual(links.link("https://x.test/docs", "a"), "https://x.test/docs/a")
                  ''')),
    "ROUTINE-1": ("Add a function that sums quantities", "routine",
                  module("totals", '''\
                      def total(rows):
                          return sum(row["qty"] for row in rows)
                      ''', '''\
                      import totals
                      class Totals(unittest.TestCase):
                          def test_total(self):
                              self.assertEqual(totals.total([{"qty": 2}, {"qty": 3}]), 5)
                      '''), None),
    "ROUTINE-2": ("Add a function that title-cases names", "routine",
                  module("names", '''\
                      def title(name):
                          return name.title()
                      ''', '''\
                      import names
                      class Names(unittest.TestCase):
                          def test_title(self):
                              self.assertEqual(names.title("ada lovelace"), "Ada Lovelace")
                      '''), None),
    "ROUTINE-3": ("Add a function that drops empty rows", "routine",
                  module("clean", '''\
                      def clean(rows):
                          return [row for row in rows if row]
                      ''', '''\
                      import clean
                      class Clean(unittest.TestCase):
                          def test_clean(self):
                              self.assertEqual(clean.clean([{}, {"a": 1}]), [{"a": 1}])
                      '''), None),
}

# The MCP route's stand-in "model". It reads the request on stdin and the arm from STAND_IN_ARM, and prints one
# candidate with the usage, lookups and MCP servers a real proposer would report. Every number in it is invented.
MCP_STAND_IN = textwrap.dedent('''\
    import json, os, sys
    items = json.loads(os.environ["STAND_IN_ITEMS"])
    request = json.load(sys.stdin)
    arm = os.environ["STAND_IN_ARM"]
    item = next(key for key, value in items.items() if value["objective"] == request["objective"])
    spec = items[item]
    lookup = spec["tag"] == "needs_lookup"
    broken = item == "LIB-4" and arm == "on"              # GitHits configured but not connected
    connected = arm == "on" and not broken
    repair = "feedback" in request
    wrong = lookup and spec["wrong"] is not None and not connected and not repair
    githits = (2 if lookup else 1) if connected and not repair else 0
    web = 3 if lookup and not connected and not repair else (1 if repair else 0)
    tokens = 1800 + 900 * githits + 600 * web + (1200 if repair else 0)
    servers = ["githits:" + ("connected" if connected else "failed")] if arm == "on" else []
    print(json.dumps({
        "candidates": [{"changes": spec["wrong"] if wrong else spec["right"], "message": item,
                        "signal": {"harness": {"name": "stand-in", "model": "stand-in-model", "mcp_servers": servers}}}],
        "recommended": [0],
        "usage": {"input_tokens": tokens, "output_tokens": 350, "model": "stand-in-model"},
        "tool_usage": {"githits": githits, "web_search": web, "web_fetch": 0}}))
    ''')

# The API route's stand-in for the ``claude`` executable: what the host reads from Claude Code, and no more. It answers
# ``--version``, reads the prompt on stdin, and acts as a model that can only call the tools it was given: with the
# launcher (``--allowedTools`` names ``Bash(<launcher>:*)``) it looks the library up through it, without it it searches
# the web. Before each tool call it runs the ``--settings`` hook (the host's query guard) as Claude Code's PreToolUse
# contract says, and its Bash tool runs only the launcher, as the launcher's ``exec`` line says (no shell needed). It
# writes its change in the working tree and prints ``stream-json``. Its token counts are the MCP stand-in's, invented.
CLAUDE_STAND_IN = textwrap.dedent('''\
    import json, os, re, shlex, subprocess, sys
    from pathlib import Path

    if "--version" in sys.argv:
        print("2.1.309 (Claude Code)")
        sys.exit(0)
    items = json.loads(Path(SPEC).read_text(encoding="utf-8"))
    prompt = sys.stdin.read()
    objective = prompt.split("\\n", 1)[0][len("Objective: "):]
    item = next(key for key, value in items.items() if value["objective"] == objective)
    spec = items[item]
    settings = json.loads(Path(sys.argv[sys.argv.index("--settings") + 1]).read_text(encoding="utf-8"))
    allowed = sys.argv[sys.argv.index("--allowedTools") + 1].split(",")
    launchers = [rule[5:-3] for rule in allowed if rule.startswith("Bash(") and rule.endswith(":*)")]
    launcher = launchers[0] if launchers else None
    lookup, repair = spec["tag"] == "needs_lookup", "did not pass the host's checks" in prompt
    topic = objective.lower().replace(",", "")
    if launcher and not repair:
        uses = [("Bash", {"command": f'{launcher} search "{topic} {n}" --target github:python/cpython',
                          "description": "look up a public API"}) for n in range(1, (2 if lookup else 1) + 1)]
    else:
        uses = [("WebSearch", {"query": f"python {topic} {n}"})
                for n in range(1, (1 if repair else 3 if lookup else 0) + 1)]
    wrong = lookup and spec["wrong"] is not None and launcher is None and not repair
    githits = sum(1 for name, _ in uses if name == "Bash")
    web = len(uses) - githits
    tokens = 1800 + 900 * githits + 600 * web + (1200 if repair else 0)

    def say(event):
        print(json.dumps(event), flush=True)

    def guard(name, tool_input, use_id):
        """Each PreToolUse hook whose matcher matches the tool, the call on stdin; it blocks on a non-zero exit (the
        host's hook sets onFailure block, so a crash blocks too), and its standard error is the reason."""
        for group in settings.get("hooks", {}).get("PreToolUse", []):
            if re.search(group.get("matcher", ""), name):
                for hook in group["hooks"]:
                    call = {"session_id": "rehearsal", "transcript_path": "", "cwd": os.getcwd(),
                            "permission_mode": "dontAsk", "hook_event_name": "PreToolUse", "tool_name": name,
                            "tool_input": tool_input, "tool_use_id": use_id}
                    done = subprocess.run([hook["command"], *hook.get("args", [])], capture_output=True,
                                          input=json.dumps(call).encode("utf-8"), timeout=hook.get("timeout", 60))
                    if done.returncode != 0:
                        return done.stderr.decode("utf-8", "replace") or "blocked"
        return None

    def run(name, tool_input):
        """The tool itself: Bash runs the launcher, as its exec line says, and nothing else; a search answers."""
        if name != "Bash":
            return False, "results for " + tool_input["query"]
        command = tool_input["command"]
        if launcher is None or not (command == launcher or command.startswith(launcher + " ")):
            return True, "Permission to use Bash with command denied"
        words = shlex.split(command)
        script = Path(words[0]).read_text(encoding="utf-8").splitlines()
        line = next(text for text in script if text.startswith("exec "))
        argv = [word for word in shlex.split(line)[1:] if word != "$@"] + words[1:]
        done = subprocess.run(argv, capture_output=True, timeout=120)
        return done.returncode != 0, (done.stdout + done.stderr).decode("utf-8", "replace")

    for path, text in (spec["wrong"] if wrong else spec["right"]).items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(text.encode("utf-8"))  # the exact text: no newline translation on Windows
    model, usage = "stand-in-model", {"input_tokens": tokens, "output_tokens": 350}
    say({"type": "system", "subtype": "init", "claude_code_version": "2.1.309", "model": model, "effort": "high",
         "apiKeySource": "ANTHROPIC_API_KEY" if os.environ.get("ANTHROPIC_API_KEY") else "none", "mcp_servers": [],
         "session_id": "rehearsal", "tools": ["Read", "Edit", "Write"] + (["Bash"] if launcher else [])})
    say({"type": "assistant", "message": {"id": "msg_1", "model": model, "usage": usage,
                                          "content": [{"type": "text", "text": "Working on " + item}]}})
    results = []
    for n, (name, tool_input) in enumerate(uses, 1):
        use_id = f"toolu_{n}"
        say({"type": "assistant", "message": {"id": "msg_1", "model": model, "usage": usage, "content": [
            {"type": "tool_use", "id": use_id, "name": name, "input": tool_input}]}})
        reason = guard(name, tool_input, use_id)
        failed, text = (True, reason) if reason is not None else run(name, tool_input)
        results.append({"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": failed})
    if results:
        say({"type": "user", "message": {"role": "user", "content": results}})
        say({"type": "assistant", "message": {"id": "msg_2", "model": model, "content": [
            {"type": "text", "text": "Done."}], "usage": {"input_tokens": 0, "output_tokens": 0}}})
    say({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2 if uses else 1, "result": "done",
         "total_cost_usd": 0.0, "session_id": "rehearsal", "usage": {}, "modelUsage": {}})
    ''')

# What the loopback GitHits answers every search with: one public hit (valid under the pinned contract's shapes).
SEARCH_ANSWER = {
    "completed": True, "expires_at": None, "query_warnings": [], "search_ref": "sr_rehearsal",
    "selected_fields": ["results"], "status": "complete", "targets": None,
    "result": {"evidence_notice": None, "partial_results": False, "query": "q", "query_warnings": [],
               "page": {"has_more": False, "limit": 5, "offset": 0, "returned": 1}, "source_status": [],
               "sources": ["code"], "results": [{
                   "coverage": None, "fresh_target_label": None, "freshness": None, "id": "hit-1",
                   "locator": {"commit_sha": None, "docs_read_target": None, "end_line": 20, "evidence_range": None,
                               "file_path": "Lib/example.py", "git_ref": None, "match_line": None,
                               "package_name": None, "page_id": None, "registry": None,
                               "repo_url": "https://github.com/python/cpython", "repository_file_path": None,
                               "requested_ref": None, "source_kind": None, "source_url": None, "start_line": 10,
                               "symbol_ref": None, "version": None},
                   "requested_target_label": None, "result_type": "code", "score": 0.875,
                   "served_target_label": None, "summary": "How the standard library does it.",
                   "target_label": "github:python/cpython", "title": "example"}]}}


class LoopbackGitHits:
    """A stand-in GitHits on 127.0.0.1 (the client accepts ``http://`` for loopback only): every search gets
    ``SEARCH_ANSWER``, anything else a 404 problem. It counts the requests it received and sends nothing on."""

    def __init__(self):
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests += 1
                found = self.command == "POST" and self.path.split("?", 1)[0] == "/v1/searches"
                body = json.dumps(SEARCH_ANSWER if found else {"code": "NOT_FOUND", "status": 404}).encode("utf-8")
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/json" if found else "application/problem+json")
                self.send_header("X-Request-ID", f"rehearsal-{outer.requests:04d}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextlib.contextmanager
def api_environment(url: str):
    """The API route's environment, put back afterwards: GitHits at the loopback stand-in under a placeholder token, a
    placeholder Anthropic key (the api-hooked lane requires one; the stand-in sends nothing anywhere), no GitHits
    setting from the shell, and no gateway or provider switch (``claude_code.THIRD_PARTY_SWITCHES`` would refuse the
    lane)."""
    saved = dict(os.environ)
    for name in (*THIRD_PARTY_SWITCHES, githits.EXCLUDE_ENV, githits.EXCLUDE_REPLACE_ENV, githits.LICENSES_ENV,
                 githits.SESSION_ENV, githits.AGENT_ENV, githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV):
        os.environ.pop(name, None)
    os.environ.update({githits.TOKEN_ENV: "rehearsal-placeholder-not-a-token", githits.URL_ENV: url,
                       "ANTHROPIC_API_KEY": "rehearsal-placeholder-not-a-key"})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def git(root: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def claude_code(kit: Kit, thread, script: Path, item: str, arm: str) -> ClaudeCodeProvider:
    """The API route's proposer, as the pilot builds it: ``syberlabs propose --claude-code --lane api-hooked
    --trusted-tree`` in arm A, the same with ``--githits --githits-arm on`` in arm B (never ``--bare``: Claude Code
    2.1.312 runs no ``--settings`` hook there), except on the misconfigured item, whose treatment forgot ``--githits``.
    The executable is the stand-in; everything else is the real provider."""
    return ClaudeCodeProvider(kit.repo, lane="api-hooked", trusted_tree=True, executable=[sys.executable, str(script)],
                              max_calls=3, githits=arm == "on" and item != MISCONFIGURED, arm=arm,
                              thread_label=thread.id)


@contextlib.contextmanager
def mcp_environment(arm: str, spec: dict):
    """The MCP route's stand-in reads its arm and the items from the environment; put back afterwards."""
    saved = dict(os.environ)
    os.environ.update({"STAND_IN_ARM": arm, "STAND_IN_ITEMS": json.dumps(spec)})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def rehearse(folder: Path, route: str = "api", log=print) -> tuple[list[dict], dict]:
    """Every item through both arms on ``route`` (``api`` or ``mcp``), in a new repository under ``folder``: the
    journal's events and the contracts, for ``syberlabs metrics`` and ``syberlabs experiment report``."""
    if route not in ROUTES:
        raise ValueError(f"route must be one of {', '.join(ROUTES)}")
    root = folder / "shop"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("")
    (root / "tests" / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n"
                                                  "    def test_true(self):\n        self.assertTrue(True)\n")
    git(folder, "init", "-q", "-b", "main", str(root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")

    contract = default_contract(root)
    contract["evolution"]["budget"] = {"max_candidates": 4, "max_evaluations": 4, "max_seconds": 120, "prices": PRICES}
    home = root / ".syberlabs"
    (home / "contracts").mkdir(parents=True)
    (home / "contracts" / f"{contract['id']}.v{contract['version']}.json").write_text(json.dumps(contract))
    spec = {item: {"objective": objective, "tag": tag, "right": right, "wrong": wrong}
            for item, (objective, tag, right, wrong) in ITEMS.items()}
    if route == "api":
        (folder / "items.json").write_text(json.dumps(spec), encoding="utf-8")
        script = folder / "claude_stand_in.py"
        script.write_text(f"SPEC = {str(folder / 'items.json')!r}\n" + CLAUDE_STAND_IN, encoding="utf-8")
    else:
        script = folder / "stand_in.py"
        script.write_text(MCP_STAND_IN)

    fake = LoopbackGitHits() if route == "api" else None
    kit = Kit.local(home, root, actor=RUNNER)
    try:
        with api_environment(fake.url) if fake else contextlib.nullcontext():
            for item, (objective, tag, _right, _wrong) in ITEMS.items():
                for arm in ("off", "on"):
                    thread = kit.start(objective, f"{contract['id']}.v{contract['version']}", paths=["src/", "tests/"],
                                       unattended=True, actor=RUNNER)
                    thread.record_experiment(NAME, item=item, arm=arm, by=RUNNER, tags=[tag])  # before any candidate
                    if route == "api":
                        found = thread.propose(claude_code(kit, thread, script, item, arm), seed=1)
                    else:
                        with mcp_environment(arm, spec):
                            found = thread.propose(RepairProvider([sys.executable, str(script)], name="stand-in",
                                                                  max_calls=3), seed=1)
                    receipt = thread.accept(found[-1].id, actor=RUNNER) if found else None
                    state = receipt.status if receipt else "no candidate"
                    if receipt and receipt.status == "succeeded":
                        # A reviewer who is not told the arm reads the accepted change (recorded as syberlabs review
                        # --record does). The stand-in reviewer turns down one change, so the rule has a failure to
                        # count.
                        verdict = "fail" if (item, arm) == ("LIB-2", "off") else "pass"
                        thread.record_review(verdict, reviewer="blind-reviewer", revision=receipt.commit, by=REVIEWER)
                        state += f", review {verdict}"
                    log(f"{item:10} {arm:3}  {len(found)} candidate(s), acceptance {state}")
        if fake is not None:
            log(f"The loopback GitHits received {fake.requests} requests; the real API received none.")
        events = [e for case in kit.journal.thread_ids() for e in kit.session.history(case)]
        return events, dict(kit.session.contracts)
    finally:
        kit.close()
        if fake is not None:
            fake.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--route", choices=ROUTES, default="api",
                        help="arm B's GitHits: the host's API launcher, the pilot's route (api, default), or an MCP "
                             "server, the route before #134 (mcp)")
    parser.add_argument("--out", help="write report.json, report.md and metrics.csv here")
    args = parser.parse_args()
    print("SIMULATED: a stand-in answers instead of a model. These numbers check the pipeline; they are not results.")
    print(f"Arm B's GitHits route: {args.route}\n")
    with tempfile.TemporaryDirectory() as folder:
        events, contracts = rehearse(Path(folder), args.route)
    items = metrics.work_items(events, contracts)
    found = experiment.report(events, NAME, contracts, delivered_by="review")
    print("\nPer work item (syberlabs metrics):")
    print(metrics.table(items))
    print("\nArms compared (syberlabs experiment report --delivered review):")
    print(experiment.table(found))
    print("\nAs the write-up prints it (--markdown):")
    print(experiment.markdown(found))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
        (out / "report.md").write_text(experiment.markdown(found) + "\n", encoding="utf-8")
        (out / "metrics.csv").write_text(metrics.to_csv(items), encoding="utf-8")
        print(f"\nwrote {out / 'report.json'}, {out / 'report.md'} and {out / 'metrics.csv'}")
    excluded = found["checks"].get("arm_conflict", [])
    return 0 if excluded == [MISCONFIGURED] and found["items"]["compared"] == 6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
