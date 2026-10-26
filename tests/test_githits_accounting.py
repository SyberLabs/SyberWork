"""Ground truth for the GitHits count: ``githits_lookups`` checked against an independent counter, E4-style.

The claim under test, from SyberLabs' scope reply to GitHits: every GitHits call is recorded by the factory itself, and
GitHits usage per work item is measured exactly, not reported by the agent. Arm B reaches GitHits only through the
host's launcher (``ClaudeCodeProvider(githits=True)``, ``docs/CLAUDE_CODE.md``): the host checkout's client signs one
record per call, the provider journals one ``lookup`` fact per authentic record (tool ``githits.<op>``), and
``metrics.githits_lookups`` counts those facts.

The independent counter is the server. ``CountingGitHits`` is the loopback ``FakeGitHits`` of ``tests/test_githits.py``
with a log of its own, written as each request arrives and before it is answered: method, path, the ``X-Request-ID``
it answers with (``srv-<n>``, unique), the session, agent and client headers, a digest of the Authorization header,
and its own SHA-256 of the canonical request it received (a POST's JSON body, a GET's parameters). Nothing in that log
comes from the factory. The cases run real ``ClaudeCodeProvider(githits=True)`` calls through the stand-in ``claude``
of ``tests/test_claude_code.py``, whose Bash tool runs the real launcher, which runs the host checkout's client against
the counter (three cases run the client alone, and ``MetricRule`` reads a hand-written journal). Per work item (one
thread; the client sends ``<thread>-b`` as the session, as ``syberlabs propose --githits`` configures it) a case
compares

- server: the requests the counter received with that session;
- journal: the thread's ``lookup`` facts with tool ``githits.<op>``;
- metric: ``githits_lookups`` from ``metrics.work_items``, and up to the first acceptance through ``experiment.runs``
  where a paired experiment reads it,

and pairs them: a fact that names a request id is the request the counter answered with that id, with the operation
of its path and the counter's digest of what it received; every request carries arm B's agent, the client's name and
the operator's token.

What should count. ``docs/METRICS.md`` defines ``githits_lookups`` as the lookups the host's client made and
recorded, a target it refused itself (``excluded_target``) included; a launcher command the query guard blocked never
reaches the client and is counted in ``lookups_blocked``. GitHits usage is what the server received; since #142 each
record also says whether its request was written (``sent``), and every case compares the facts with ``sent: true``
to the server too. ``Exact`` holds the cases where the count equals the server's. ``NotSent`` holds the two where the
client counts a lookup that never reached GitHits, by definition. ``Unknowable`` holds the cases where the journal
itself cannot say how many requests reached GitHits, for which ``githits_lookups`` is unknown instead of a number
(``MetricRule`` pins that rule on a hand-written journal). ``Fixed`` holds what this module pinned as expected
failures in files it does not own (``githits.py``, ``claude_code.py``) until #142 closed it.

No real GitHits, Claude Code or token is used: the counter listens on loopback and the token is random per test.
"""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import unittest
from typing import NamedTuple
from unittest import mock

from syberlabs import experiment, githits, metrics, query_guard
from syberlabs.canonical import canonical
from syberlabs.claude_code import CLIENT_TOOL, GITHITS_ENTRY, LOOKUP, THIRD_PARTY_SWITCHES, ClaudeCodeProvider
from tests.test_build_thread import GOOD, REPORT
from tests.test_claude_code import ClaudeCodeCase
from tests.test_githits import FakeGitHits, example_response, read_code_response, search_hit, search_response, source

ARM = "b"
AGENT = f"syberfactory/{ARM}"
BROKEN = {**GOOD, "src/report.py": "def rows():\n    return []\n"}  # the tests fail: the report rows are gone
SEARCH = 'search "requests retry backoff zq71" --target pypi:requests@2.32.3'
EXAMPLE = 'example "retry an HTTP call with backoff zq72" --language python'
READ = "read pypi:requests@2.32.3 --path src/requests/adapters.py --start-line 1 --end-line 40"
LIST = "list pypi:requests@2.32.3"
GREP = "grep max_retries --target pypi:requests@2.32.3 --literal"
GITHITS_SETTINGS = (githits.EXCLUDE_ENV, githits.EXCLUDE_REPLACE_ENV, githits.LICENSES_ENV, githits.SESSION_ENV,
                    githits.AGENT_ENV, githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV)
INT_PARAMS = ("start_line", "end_line", "wait_timeout_ms", "limit")  # GET parameters the client sends as integers
HOLD, DROP = "hold", "drop"
HOLD_SECONDS = 60
NULL_HIT = {**search_response([]), "result": {**search_response([])["result"], "results": [None]}}


def received_digest(request: dict) -> str:
    """The counter's own SHA-256 of what it received: the canonical JSON body of a POST, or a GET's parameters with
    the integer ones as integers. ``githits.query_digest`` is the same function of what the client meant to send."""
    if request["method"] == "POST":
        value = json.loads(request["body"].decode("utf-8"))
    else:
        value = {key: int(values[0]) if key in INT_PARAMS and values[0].isdigit() else values[0]
                 for key, values in request["query"].items()}
    return hashlib.sha256(query_guard.utf8(canonical(value))).hexdigest()


def answer(body, status: int = 200, headers: dict | None = None):
    payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return lambda request: (status, {"Content-Type": "application/json", **(headers or {})}, payload)


def problem(code: str, status: int, headers: dict | None = None):
    return answer({"code": code, "title": code.lower(), "status": status}, status,
                  {"Content-Type": "application/problem+json", **(headers or {})})


class CountingGitHits(FakeGitHits):
    """The independent counter. ``plan[path]`` lists what the next requests to that path get, in order: a callable
    returning ``(status, headers, body)``, ``HOLD`` (answer only when the test releases it or ``HOLD_SECONDS`` pass),
    ``DROP`` (close the connection without an answer) or None (the fake's default answer)."""

    def __init__(self):
        self.log, self.plan, self.release, self._lock = [], {}, threading.Event(), threading.Lock()
        super().__init__()
        self.server.handle_error = lambda request, address: None  # a client that hung up is not the server's error

    def answer(self, request):
        headers = request["headers"]
        with self._lock:
            entry = {"request_id": f"srv-{len(self.log) + 1:04d}", "method": request["method"], "path": request["path"],
                     "session": headers.get("x-githits-session-id"), "agent": headers.get("x-githits-agent"),
                     "client": headers.get("x-githits-client-name"),
                     "authorization": hashlib.sha256(headers.get("authorization", "").encode()).hexdigest(),
                     "digest": received_digest(request), "status": None}
            self.log.append(entry)
            planned = self.plan.get(request["path"]) or []
            step = planned.pop(0) if planned else None
        if step == DROP:
            raise ConnectionAbortedError("closed without an answer")
        if step == HOLD:
            self.release.wait(HOLD_SECONDS)
            step = None
        found = super().answer(request) if step is None else step(request)
        entry["status"] = found[0]
        return (found[0], {**found[1], "X-Request-ID": entry["request_id"]}, *found[2:])

    def of(self, session: str) -> list[dict]:
        return [entry for entry in self.log if entry["session"] == session]

    def close(self):
        self.release.set()
        super().close()


def dead_url() -> str:
    """A loopback base URL nothing listens on: a port the system handed out, then closed."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def launcher(arguments: str) -> list:
    """A Bash tool call of the launcher; the stand-in puts the call's launcher path in place of ``{launcher}``."""
    return ["Bash", {"command": "{launcher} " + arguments, "description": "look up a public API"}]


def passed_and_recorded(candidate) -> tuple:
    """The provider's own reconciliation on the candidate: launcher calls the guard passed, authentic records."""
    return candidate.signal["githits"]["launcher_passed"], candidate.signal["githits"]["records"]


def client_facts(events) -> list[dict]:
    """The values of the ``lookup`` facts the provider made from GitHits client records, in journal order."""
    found = (metrics.fact(e, LOOKUP) for e in events)
    return [value for value in found if value is not None and str(value.get("tool", "")).startswith(CLIENT_TOOL)]


class Ledger(NamedTuple):
    server: int             # requests the counter received for the work item
    journal: int            # lookup facts with tool githits.<op>
    sent: int               # those whose record says the request was written to GitHits (``sent: true``, #142)
    metric: metrics.Metric  # githits_lookups over the whole thread
    paired: int             # facts naming a request id the counter issued, same operation and digest
    unpaired: list          # request ids of the other facts (None: the client never read an answer)
    unanswered: list        # the counter's request ids no fact names
    facts: list


class GroundTruth(ClaudeCodeCase):
    def setUp(self):
        super().setUp()
        self.server = CountingGitHits()
        self.addCleanup(self.server.close)
        self.token = "ghtest_" + secrets.token_hex(16)
        self.bearer = hashlib.sha256(f"Bearer {self.token}".encode()).hexdigest()
        kept = {k: v for k, v in os.environ.items() if k not in THIRD_PARTY_SWITCHES + GITHITS_SETTINGS}
        patcher = mock.patch.dict(os.environ, {**kept, githits.TOKEN_ENV: self.token, githits.URL_ENV: self.server.url},
                                  clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def arm_b(self, kit, thread, claude, **kwargs) -> ClaudeCodeProvider:
        """Arm B as ``syberlabs propose --claude-code --bare --githits`` builds it: the thread's id is the session
        label (``cli.py``)."""
        kwargs.setdefault("max_calls", 1)
        kwargs.setdefault("thread_label", thread.id)
        return ClaudeCodeProvider(kit.repo, bare=True, executable=claude, githits=True, **kwargs)

    def work_item(self, kit, name: str, lookups: list, **stand_in):
        thread = kit.start(f"Add a CSV export ({name})", paths=["src/", "tests/"])
        return thread, self.stand_in(name, lookups=lookups, **stand_in)

    def ledger(self, thread, events=None) -> Ledger:
        events = thread.history() if events is None else events
        served = self.server.of(f"{thread.id}-{ARM}")
        facts = client_facts(events)
        [item] = metrics.work_items(events)
        issued = {entry["request_id"]: entry for entry in served}
        paired, unpaired = [], []
        for value in facts:
            entry = issued.get(value["request_id"])
            if entry is None:
                unpaired.append(value["request_id"])
                continue
            op = value["tool"][len(CLIENT_TOOL):]
            operation = githits.DOCTOR if op == "doctor" else githits.OPERATIONS[op]
            self.assertEqual((entry["method"], entry["path"]), (operation.method, operation.path), entry["request_id"])
            self.assertEqual(entry["digest"], value["query_digest"], "the record's digest is of what the server got")
            paired.append(entry["request_id"])
        for entry in served:
            self.assertEqual((entry["agent"], entry["client"]), (AGENT, githits.CLIENT_NAME))
            self.assertEqual(entry["authorization"], self.bearer, "the operator's token, in the Authorization header")
        return Ledger(len(served), len(facts), sum(1 for value in facts if value.get("sent") is True),
                      item.githits_lookups, len(paired), unpaired,
                      [entry["request_id"] for entry in served if entry["request_id"] not in paired], facts)

    def server_body(self, path: str) -> bytes:
        """The one request body the counter received on ``path`` (``FakeGitHits`` keeps each request it served)."""
        [request] = [request for request in self.server.requests if request["path"] == path]
        return request["body"]

    def stopped(self, thread) -> tuple:
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        return search["stopped"], search["error"]


class Exact(GroundTruth):
    """The count equals what the server received, request for request, and so does the number of facts whose record
    says the request was written (``sent: true``)."""

    def test_a_lookup_is_one_request_one_fact_and_one_count(self):
        kit = self.kit()
        thread, claude = self.work_item(kit, "normal", [launcher(SEARCH)])
        [candidate] = thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (1, 1, 1, 1))
        self.assertEqual((found.paired, found.unpaired, found.unanswered), (1, [], []))
        self.assertEqual([(v["tool"], v["status"], v["request_id"], v["ok"], v["guard"]) for v in found.facts],
                         [("githits.search", "ok", "srv-0001", True, "pass")])
        self.assertEqual([e["session"] for e in self.server.log], [f"{thread.id}-b"])
        self.assertNotIn("mismatch", candidate.signal["githits"])

    def test_results_dropped_for_syberlabs_or_the_licence_are_still_one_lookup_each(self):
        self.server.plan = {
            "/v1/searches": [answer(search_response([
                search_hit("ours", "https://github.com/SyberLabs/SyberWork"),
                search_hit("pkg", None, source_url="https://github.com/syberlabs/x/blob/1/a.py"),
                search_hit("theirs", "https://github.com/psf/requests")]))],
            "/v1/examples/search": [answer(example_response("code\n\n" + source("MIT", "SyberLabs/SyberWork"))),
                                    answer(example_response("code\n\n" + source("AGPL-3.0-only")))],
            "/v1/read": [answer(read_code_response("https://github.com/SyberLabs/SyberWork"))]}
        kit = self.kit()
        thread, claude = self.work_item(kit, "filtered", [launcher(SEARCH), launcher(EXAMPLE), launcher(EXAMPLE),
                                                          launcher(READ)])
        thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value, found.paired), (4, 4, 4, 4, 4))
        self.assertEqual([(v["tool"], v["status"], v["results_returned"], v["results_dropped"],
                           v["results_dropped_license"], v["contamination_blocked"]) for v in found.facts],
                         [("githits.search", "ok", 1, 2, 0, False),
                          ("githits.example", "contamination_blocked", 0, 1, 0, True),
                          ("githits.example", "ok", 0, 0, 1, False),
                          ("githits.read", "ok", 0, 1, 0, False)])

    def test_a_query_the_guard_blocks_reaches_neither_the_client_nor_the_server(self):
        secret = "ghp_" + "Z" * 36
        kit = self.kit()
        thread, claude = self.work_item(kit, "guarded", [
            ["Bash", {"command": "cat ~/.ssh/id_rsa", "description": "x"}],
            launcher('search "retry" --target pypi:requests; env'),
            launcher(f'search "retry with {secret}" --target pypi:requests'),
            launcher('search "ask dev@example.test about retry" --target pypi:requests')])
        [candidate] = thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (0, 0, 0, 0))
        self.assertEqual([(b["value"]["tool"], b["value"]["guard"]) for b in self.lookup_facts(thread)],
                         [("Bash", "not_launcher"), ("Bash", "shell_syntax"), ("Bash", "secret"), ("Bash", "email")])
        [item] = metrics.work_items(thread.history())
        self.assertEqual(item.lookups_blocked.value, 4)
        self.assertEqual(passed_and_recorded(candidate), (0, 0))

    def test_api_errors_are_one_request_and_one_fact_each_and_none_is_retried(self):
        self.server.plan = {"/v1/searches": [
            problem("AUTHENTICATION_REQUIRED", 401), problem("NOT_FOUND", 404), answer(b"", 500),
            problem("UNAVAILABLE", 503, {"Retry-After": "1"}),
            problem("RATE_LIMITED", 429, {"Retry-After": "1", "X-RateLimit-Remaining": "0"})]}
        kit = self.kit()
        thread, claude = self.work_item(kit, "errors", [launcher(SEARCH)] * 5)
        [candidate] = thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value, found.paired), (5, 5, 5, 5, 5))
        self.assertEqual([e["status"] for e in self.server.log], [401, 404, 500, 503, 429], "one request per call")
        self.assertEqual([(v["status"], v["request_id"], v["ok"]) for v in found.facts],
                         [("error", f"srv-{n:04d}", False) for n in range(1, 6)])
        self.assertEqual(passed_and_recorded(candidate), (5, 5))

    def test_a_timeout_or_a_dropped_connection_is_one_request_and_one_fact_without_an_id(self):
        self.server.plan = {"/v1/searches": [HOLD, DROP]}
        kit = self.kit()
        thread, claude = self.work_item(kit, "transport", [launcher(SEARCH + " --timeout 1"), launcher(SEARCH)])
        thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (2, 2, 2, 2),
                         "both requests were written before the answer failed: sent: true")
        self.assertEqual((found.paired, found.unpaired, found.unanswered), (0, [None, None], ["srv-0001", "srv-0002"]),
                         "the client never read an answer, so no X-Request-ID reached the record")
        self.assertEqual([(v["status"], v["query_digest"]) for v in found.facts],
                         [("error", entry["digest"]) for entry in self.server.log], "paired by order and digest")

    def test_several_lookups_in_one_call_are_one_request_and_one_fact_each_in_order(self):
        kit = self.kit()
        lookups = [launcher(text) for text in (SEARCH, EXAMPLE, READ, LIST, GREP)]
        thread, claude = self.work_item(kit, "several", lookups)
        [candidate] = thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value, found.paired), (5, 5, 5, 5, 5))
        self.assertEqual([v["request_id"] for v in found.facts], [e["request_id"] for e in self.server.log],
                         "the journal's order is the order the requests arrived")
        self.assertEqual([(v["tool"], e["path"]) for v, e in zip(found.facts, self.server.log)], [
            ("githits.search", "/v1/searches"), ("githits.example", "/v1/examples/search"),
            ("githits.read", "/v1/read"), ("githits.list", "/v1/list"), ("githits.grep", "/v1/grep")])
        self.assertEqual(json.loads(self.server_body("/v1/examples/search"))["license_mode"], "strict")
        self.assertEqual(candidate.signal["tool_usage"]["githits"], 5)

    def test_arm_a_holds_no_githits_route_and_counts_zero(self):
        """Arm A, the baseline: no launcher, and (#135) no GitHits variable in Claude Code's environment, so the same
        launcher-shaped command is denied and nothing reaches the server; the count is a known 0."""
        kit = self.kit()
        thread, claude = self.work_item(kit, "arm-a", [launcher(SEARCH)], log_env=True)
        [candidate] = thread.propose(ClaudeCodeProvider(kit.repo, bare=True, executable=claude, max_calls=1))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (0, 0, 0, 0))
        self.assertEqual(self.server.log, [])
        [run] = self.runs("arm-a")
        self.assertNotIn(githits.TOKEN_ENV, run["env"], "arm A holds no GitHits credential")
        self.assertNotIn("githits", candidate.signal)

    def test_launcher_processes_running_at_once_each_leave_one_authentic_record(self):
        """Claude Code may run tool calls side by side; the client appends one line per call to one file."""
        folder, key = self.root.parent / "parallel-records", secrets.token_hex(32)
        env = {**os.environ, githits.RECORD_DIR_ENV: str(folder), githits.RECORD_KEY_ENV: key,
               githits.SESSION_ENV: "parallel-b", githits.AGENT_ENV: AGENT}
        processes = [subprocess.Popen([sys.executable, "-I", str(GITHITS_ENTRY), "search", f"retry backoff zq9{n}",
                                       "--target", "pypi:requests@2.32.3"], env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for n in range(8)]
        self.assertEqual([process.wait(timeout=120) for process in processes], [0] * 8)
        read = githits.read_records(folder, key)
        self.assertEqual((len(self.server.of("parallel-b")), len(read.records), read.ignored), (8, 8, 0))
        self.assertEqual({record["sent"] for record in read.records}, {True})
        digests = {entry["request_id"]: entry["digest"] for entry in self.server.log}
        self.assertEqual(sorted(digests), sorted(record["request_id"] for record in read.records))
        self.assertTrue(all(digests[record["request_id"]] == record["query_digest"] for record in read.records))

    def test_lookups_across_fix_rounds_count_up_to_the_first_acceptance(self):
        kit = self.kit()
        thread, claude = self.work_item(kit, "rounds", [launcher(SEARCH), launcher(GREP)], first=BROKEN,
                                        repair={"src/report.py": REPORT})
        thread.record_experiment("ground-truth", item="fix-rounds", arm="on", by="dev@example.test")
        found = thread.propose(self.arm_b(kit, thread, claude, max_calls=3))
        self.assertEqual([(c.id, c.evaluation) for c in found], [("c1", "failed"), ("c2", "passed")])
        self.assertEqual(thread.accept("c2").status, "succeeded")
        at_acceptance = len(self.server.log)
        later = self.stand_in("after", first={**GOOD, "src/notes.py": "NOTE = 1\n"}, lookups=[launcher(SEARCH)])
        thread.propose(self.arm_b(kit, thread, later))
        events = thread.history()
        whole = self.ledger(thread, events)
        self.assertEqual((whole.server, whole.journal, whole.sent, whole.metric.value, whole.paired), (5, 5, 5, 5, 5))
        accept = next(e for e in events
                      if e["kind"] == "effect_succeeded" and e["body"].get("action") == experiment.PROMOTE)
        before = client_facts(e for e in events if e["seq"] <= accept["seq"])
        [run] = experiment.runs(events, "ground-truth")
        self.assertEqual((at_acceptance, len(before), run.value("githits_lookups").value), (4, 4, 4),
                         "server, journal and the experiment's number, each up to the first acceptance")
        self.assertEqual([v["request_id"] for v in before], [e["request_id"] for e in self.server.log[:4]])
        self.assertEqual((run.value("fix_rounds").value, run.work.githits_lookups.value), (1, 5))

    def test_an_answer_the_client_cannot_read_is_still_one_request_and_one_fact(self):
        """A search hit that is not an object, outside the pinned contract. Before #142 it crashed the client after the
        request left, with no record (``githits_unrecorded``); #142 records it: ``malformed_response``, sent."""
        self.server.plan = {"/v1/searches": [None, answer(NULL_HIT)]}
        kit = self.kit()
        thread, claude = self.work_item(kit, "unreadable", [launcher(SEARCH), launcher(SEARCH)])
        [candidate] = thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value, found.paired), (2, 2, 2, 2, 2))
        self.assertEqual([(v["status"], v["request_id"], v["sent"]) for v in found.facts],
                         [("ok", "srv-0001", True), ("error", "srv-0002", True)])
        self.assertNotIn("mismatch", candidate.signal["githits"])


class NotSent(GroundTruth):
    """Counted as lookups by definition (the client made the call), though nothing reached GitHits. Each fact's record
    says so (``sent: false``, #142), so what GitHits received is still read off the journal exactly."""

    def test_a_target_the_client_refuses_is_counted_and_never_reaches_the_server(self):
        kit = self.kit()
        refused = [launcher('search "retry" --target github:SyberLabs/SyberWork'), launcher("list SyberLabs/SyberWork")]
        thread, claude = self.work_item(kit, "excluded", refused + [launcher(SEARCH)])
        thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value, found.paired), (1, 3, 1, 3, 1))
        self.assertEqual([(v["tool"], v["status"], v["request_id"], v["sent"]) for v in found.facts],
                         [("githits.search", "excluded_target", None, False),
                          ("githits.list", "excluded_target", None, False),
                          ("githits.search", "ok", "srv-0001", True)])

    def test_a_connection_that_never_opened_is_counted_and_the_server_saw_nothing(self):
        kit = self.kit()
        thread, claude = self.work_item(kit, "refused", [launcher(SEARCH)])
        with mock.patch.dict(os.environ, {githits.URL_ENV: dead_url()}):
            thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (0, 1, 0, 1))
        self.assertEqual([(v["status"], v["request_id"], v["sent"]) for v in found.facts], [("error", None, False)],
                         "unlike a timeout after GitHits got the request (sent: true), its record says it never left")


class Unknowable(GroundTruth):
    """Cases where the journal cannot say how many requests reached GitHits: the count is unknown, never a guess."""

    def test_a_launcher_call_that_left_no_record_makes_the_count_unknown(self):
        """A usage error (``search`` with no ``--target``) exits 2 before a client exists: nothing is sent or recorded,
        and the guard passed the call, so the call reports ``githits_unrecorded``. Since #142 the client records every
        call it starts (an answer it cannot read, a SIGTERM), but a launcher killed outright (SIGKILL) or a record that
        cannot be written still leaves this same journal after its request left, so 1 could be one short."""
        kit = self.kit()
        thread, claude = self.work_item(kit, "usage", [launcher(SEARCH), launcher("search")])  # no --target
        [candidate] = thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (1, 1, 1, None))
        self.assertEqual({k: candidate.signal["githits"].get(k) for k in ("launcher_passed", "records", "mismatch")},
                         {"launcher_passed": 2, "records": 1, "mismatch": "githits_unrecorded"})
        self.assertIn("left no GitHits client record", found.metric.note)
        self.assertIn("(1 recorded)", found.metric.note)
        self.assertEqual(found.metric.basis, tuple(e["hash"] for e in thread.history()
                                                   if e["kind"] == "candidate_registered"))

    def test_a_command_the_launcher_refuses_sends_nothing_and_still_makes_the_count_unknown(self):
        """#135: the launcher's entry runs only the five lookups, and ``example`` only with ``license_mode: strict``;
        ``doctor``, ``--help`` and ``--license-mode`` exit 2 before a client exists, so nothing is sent or recorded.
        The guard passed each, though, so the call reports ``githits_unrecorded`` like any launcher call without a
        record, and the journal cannot tell these refusals from a request that left without one: unknown."""
        kit = self.kit()
        thread, claude = self.work_item(kit, "refused-commands", [
            launcher(SEARCH), launcher("doctor"), launcher("--help"),
            launcher('example "retry an HTTP call" --license-mode yolo')])
        [candidate] = thread.propose(self.arm_b(kit, thread, claude))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (1, 1, 1, None))
        self.assertEqual([entry["path"] for entry in self.server.log], ["/v1/searches"], "no doctor, no example")
        self.assertEqual({k: candidate.signal["githits"].get(k) for k in ("launcher_passed", "records", "mismatch")},
                         {"launcher_passed": 4, "records": 1, "mismatch": "githits_unrecorded"})
        self.assertIn("(1 recorded)", found.metric.note)

    def test_a_record_with_no_guarded_launcher_call_makes_the_count_unknown(self):
        kit = self.kit()
        thread, claude = self.work_item(kit, "unexpected", [launcher(SEARCH)], records_written=[True])
        self.assertEqual(thread.propose(self.arm_b(kit, thread, claude)), [])
        self.assertEqual(self.stopped(thread), ("error", "provider_guard_inactive"))
        found = self.ledger(thread)
        self.assertEqual((found.server, found.journal, found.sent, found.metric.value), (1, 2, 1, None))
        self.assertEqual((found.paired, found.unpaired), (1, ["req-forged"]),
                         "one record names a request the server never answered")
        self.assertEqual([(v["guard"], v["sent"]) for v in found.facts], [("unguarded", None), ("unguarded", True)])
        self.assertIn("no guarded launcher call", found.metric.note)

    def test_a_lookup_in_flight_when_the_call_times_out_makes_the_count_unknown(self):
        """The host ends a Claude Code call past its timeout by killing its session (SIGKILL), so a launcher whose
        request has left writes no record, and a failed call registers no candidate to carry the mismatch. Before
        #142 the journal held no trace and the count was a known number one short (this module's finding 3). Since
        #142 the failed call's records are journaled ``unguarded``, and when it has none, one ``githits.unrecorded``
        fact stands for each launcher call the guard passed: either way the count is unknown."""
        self.server.plan = {"/v1/searches": [None, HOLD, HOLD]}
        kit = self.kit()
        recorded, claude = self.work_item(kit, "in-flight", [launcher(SEARCH), launcher(SEARCH)])
        self.assertEqual(recorded.propose(self.arm_b(kit, recorded, claude, timeout=15)), [])
        alone, claude = self.work_item(kit, "in-flight-alone", [launcher(SEARCH)])
        self.assertEqual(alone.propose(self.arm_b(kit, alone, claude, timeout=10)), [])
        for thread in (recorded, alone):
            self.assertEqual(self.stopped(thread), ("error", "provider_timeout"))
        first, second = self.ledger(recorded), self.ledger(alone)
        self.assertEqual((first.server, first.journal, first.sent, first.metric.value), (2, 1, 1, None))
        self.assertEqual([(v["tool"], v["guard"], v["sent"]) for v in first.facts],
                         [("githits.search", "unguarded", True)], "the answered one, no longer reconciled")
        self.assertEqual((second.server, second.journal, second.sent, second.metric.value), (1, 1, 0, None))
        self.assertEqual([(v["tool"], v["guard"], v["sent"], v["request_id"]) for v in second.facts],
                         [("githits.unrecorded", "unguarded", None, None)], "a trace for the one in flight")


class Fixed(GroundTruth):
    """Findings this module pinned as expected failures until #142 closed them (this branch carries #142's commits
    until #142 merges). The fourth, a lookup in flight when the call times out, is ``Unknowable`` above."""

    def test_githits_an_answer_the_client_cannot_read_is_still_recorded(self):
        """githits.py, ``Client.call``: an exception after the request was sent ended the call before ``_record``: a
        search hit that is not an object (``_filter`` called ``hit.get``), or JSON nested past the recursion limit
        (``json.loads`` raised RecursionError). GitHits served the request and no record existed. #142 records every
        failure: here ``malformed_response``, with the answer's request id, sent."""
        folder, key = self.root.parent / "unfiltered-records", secrets.token_hex(32)
        client = githits.Client(token=self.token, base_url=self.server.url, record_dir=folder, record_key=key)
        self.server.plan = {"/v1/searches": [answer(NULL_HIT)]}
        with self.assertRaises(githits.GitHitsError) as caught:
            client.search("q", ["pypi:requests"])
        self.assertEqual(caught.exception.code, "malformed_response")
        self.assertEqual(len(self.server.log), 1, "the request reached the server")
        [record] = githits.read_records(folder, key).records
        self.assertEqual((record["status"], record["request_id"], record["sent"]), ("error", "srv-0001", True))

    def test_githits_a_transport_failure_record_says_whether_the_request_was_sent(self):
        """githits.py, ``RECORD_FIELDS``: a timeout after GitHits received the request and a connection refused before
        anything received it left the same record (``status: error``, ``http_status`` and ``request_id`` null), so the
        requests GitHits received could not be read off the journal once a transport failure occurred. #142 adds
        ``sent``: true once the request was written, false when it never left."""
        folder, key = self.root.parent / "transport-records", secrets.token_hex(32)
        self.server.plan = {"/v1/searches": [HOLD]}
        for base_url in (self.server.url, dead_url()):
            client = githits.Client(token=self.token, base_url=base_url, record_dir=folder, record_key=key, timeout=1)
            with self.assertRaises(githits.GitHitsError):
                client.search("requests retry", ["pypi:requests"])
        self.assertEqual(len(self.server.log), 1, "the timed-out request reached the server; the refused one did not")
        timed_out, refused = githits.read_records(folder, key).records
        self.assertEqual([(r["status"], r["http_status"], r["request_id"], r["sent"]) for r in (timed_out, refused)],
                         [("error", None, None, True), ("error", None, None, False)])

    def test_claude_code_a_provider_built_in_python_names_its_thread_in_the_session(self):
        """claude_code.py, ``_session``: the label was ``thread_label``, else ``space.thread_id``, which
        ``GitSearchSpace`` does not have, else the search id. Only the CLI passes ``thread_label``, so a provider built
        in Python sent ``s1-b`` for the first search of every thread, and GitHits' own per-session view of usage
        merged work items. #142 reads the space's thread."""
        kit = self.kit()
        threads = []
        for name in ("label-1", "label-2"):
            thread, claude = self.work_item(kit, name, [launcher(SEARCH)])
            thread.propose(ClaudeCodeProvider(kit.repo, bare=True, executable=claude, githits=True, max_calls=1))
            threads.append(thread)
        self.assertEqual(len(self.server.log), 2)
        self.assertEqual([entry["session"] for entry in self.server.log], [f"{t.id}-{ARM}" for t in threads])


# ---------------------------------------------------------------------------------- the metric's rule, on a journal

T0 = 1_792_000_000_000_000


class Journal:
    """One thread's events, numbered and hashed in order, written by hand."""

    def __init__(self):
        self.events = []

    def add(self, kind: str, body: dict) -> str:
        seq = len(self.events) + 1
        self.events.append({"case_id": "thread-g", "seq": seq, "kind": kind, "body": body, "at": T0 + seq,
                            "previous": "0" * 64, "hash": f"{seq:064x}"})
        return self.events[-1]["hash"]

    def lookup(self, tool: str = "githits.search", guard: str = "pass") -> str:
        value = {"tool": tool, "query_digest": "0" * 64, "request_id": f"srv-{len(self.events)}", "status": "ok",
                 "at": "2026-10-26T00:00:00.000Z", "ok": guard == "pass", "guard": guard}
        return self.add("observed", {"key": LOOKUP, "value": value, "source": metrics.HOST, "actor": metrics.HOST,
                                     "version": f"s1.1.{len(self.events) + 1}", "verified": True})

    def registration(self, githits_signal: dict | None = None, declared: int = 0) -> str:
        signal = {"tool_usage": {"web_search": 0, "web_fetch": 0, "githits": declared, "other": {}}}
        if githits_signal is not None:
            signal["githits"] = githits_signal
        return self.add("candidate_registered", {"id": f"c{len(self.events)}", "operator": "patch",
                                                 "actor": metrics.HOST, "signal": signal})

    def githits_lookups(self) -> metrics.Metric:
        [item] = metrics.work_items(self.events)
        return item.githits_lookups


def counts(passed: int, records: int, mismatch: str | None = None) -> dict:
    found = {"arm": ARM, "launcher_calls": passed, "launcher_passed": passed, "records": records,
             "records_ignored": 0, "bash_refused": 0, "license_policy": "0" * 64}
    return {**found, "mismatch": mismatch} if mismatch else found


class MetricRule(unittest.TestCase):
    """``githits_lookups`` is the number of client-record facts only when the journal says every launcher call the
    guard passed left one record and every record had such a call behind it; otherwise it is unknown."""

    def test_the_client_records_are_the_count_when_calls_and_records_agree(self):
        j = Journal()
        facts = (j.lookup(), j.lookup("githits.example"), j.lookup("Bash", guard="secret"))
        j.registration(counts(2, 2), declared=2)
        found = j.githits_lookups()
        self.assertEqual((found.value, found.basis), (2, facts[:2]))
        self.assertIn("host-observed", found.note)

    def test_a_launcher_call_without_a_record_makes_it_unknown(self):
        j = Journal()
        j.lookup()
        registration = j.registration(counts(3, 1, "githits_unrecorded"), declared=1)
        found = j.githits_lookups()
        self.assertEqual((found.value, found.basis), (None, (registration,)))
        self.assertIn("1 call(s) report launcher calls the query guard passed that left no GitHits client record",
                      found.note)
        self.assertIn("(1 recorded)", found.note)
        self.assertEqual(found.to_dict()["value"], "unknown")

    def test_without_any_client_record_a_mismatch_is_unknown_not_the_declared_zero(self):
        j = Journal()
        registration = j.registration(counts(1, 0, "githits_unrecorded"), declared=0)
        self.assertEqual((j.githits_lookups().value, j.githits_lookups().basis), (None, (registration,)))

    def test_an_unguarded_client_record_makes_it_unknown(self):
        j = Journal()
        j.lookup()
        unguarded = j.lookup(guard="unguarded")
        found = j.githits_lookups()
        self.assertEqual((found.value, found.basis), (None, (unguarded,)))
        self.assertIn("1 GitHits client record(s) had no guarded launcher call behind them", found.note)

    def test_declared_counts_are_read_as_before_without_client_records(self):
        j = Journal()
        j.lookup("mcp__githits__search")
        j.registration(declared=3)
        self.assertEqual(j.githits_lookups().value, 3)


if __name__ == "__main__":
    unittest.main()
