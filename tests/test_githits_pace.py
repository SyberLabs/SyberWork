"""pilot-readiness, item 2: host-side pacing of GitHits ``example`` calls, its record, and its accounting.

GitHits limits its example endpoint per organization to one or two requests a minute. The host's launcher entry
(``githits_cli_entry._pace``) holds each example until this host's last one is ``githits_pace.EXAMPLE_INTERVAL``
(40 s) old, across processes, under a lock and a time file outside any worktree; the client records how long it was
held (``paced_ms``) and never waits or retries itself; the experiment report leaves that time out of an arm's agent
time. Every test injects the clock or a short interval, and every call goes to a loopback fake GitHits.
"""

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from syberlabs import experiment, githits, githits_cli_entry, githits_pace, metrics
from syberlabs.claude_code import record_lookup
from tests.test_githits import ENTRY, FakeCase, launch_guard, run_entry
from tests.test_metrics import HOUR, T0, Journal, check

ROOT = Path(__file__).resolve().parents[1]
PACE_ENV = (githits_pace.PACE_DIR_ENV, githits_pace.PACE_SECONDS_ENV)


class FakeClock:
    """A wall clock that only ``sleep`` moves."""

    def __init__(self, now: float = 1_800_000_000.0):
        self.now, self.sleeps = now, []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Wait(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.dir = Path(self.folder.name) / "pace"
        self.clock = FakeClock()

    def wait(self, **kwargs):
        return githits_pace.wait(self.dir, clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_each_slot_is_the_interval_after_the_last_and_the_wait_is_returned(self):
        self.assertEqual(self.wait(), 0, "the first example on this host waits for nothing")
        self.assertEqual(self.wait(), 40_000, "the next one at once is held the whole interval")
        self.assertEqual(self.clock.sleeps, [40.0])
        self.clock.now += 15
        self.assertEqual(self.wait(), 25_000, "15 s later: held for the remaining 25 s")
        self.clock.now += 41
        self.assertEqual(self.wait(), 0, "an interval later: not held")
        self.assertEqual(float((self.dir / githits_pace.STAMP_FILE).read_text(encoding="ascii")), self.clock.now)
        self.assertEqual(githits_pace.EXAMPLE_INTERVAL, 40.0)

    def test_a_future_or_unreadable_stamp_never_holds_a_call_past_one_interval(self):
        self.dir.mkdir()
        (self.dir / githits_pace.STAMP_FILE).write_text(repr(self.clock.now + 10_000), encoding="ascii")
        self.assertEqual(self.wait(), 40_000)
        for text in ("garbage", "nan", "inf", ""):
            with self.subTest(stamp=text):
                (self.dir / githits_pace.STAMP_FILE).write_text(text, encoding="ascii")
                self.assertEqual(self.wait(), 0)
        with self.assertRaises(githits_pace.PaceError):
            self.wait(interval=-1)

    def test_the_directory_is_the_hosts_and_an_unusable_one_is_an_error(self):
        self.assertEqual(githits_pace.directory({}), Path(tempfile.gettempdir()) / githits_pace.DEFAULT_DIR_NAME)
        self.assertEqual(githits_pace.directory({githits_pace.PACE_DIR_ENV: str(self.dir)}), self.dir)
        with self.assertRaises(githits_pace.PaceError):
            githits_pace.directory({githits_pace.PACE_DIR_ENV: "relative/pace"})
        blocker = Path(self.folder.name) / "a-file"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(githits_pace.PaceError):
            githits_pace.wait(blocker / "pace", clock=self.clock, sleep=self.clock.sleep)

    def test_githits_itself_is_always_paced_at_40_s_and_a_loopback_stand_in_only_when_asked(self):
        for environ in ({}, {githits_pace.PACE_SECONDS_ENV: "0"}, {githits_pace.PACE_SECONDS_ENV: "0.5"}):
            with self.subTest(environ=environ):
                self.assertEqual(githits_pace.interval_for(False, environ), 40.0)
        self.assertIsNone(githits_pace.interval_for(True, {}))
        self.assertEqual(githits_pace.interval_for(True, {githits_pace.PACE_SECONDS_ENV: "2.5"}), 2.5)
        for bad in ("-1", "soon", "inf", "nan"):
            with self.subTest(bad=bad), self.assertRaises(githits_pace.PaceError):
                githits_pace.interval_for(True, {githits_pace.PACE_SECONDS_ENV: bad})


WAITER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from syberlabs import githits_pace
    held = githits_pace.wait(Path(sys.argv[2]), interval=float(sys.argv[3]))
    slot = (Path(sys.argv[2]) / githits_pace.STAMP_FILE).read_text(encoding="ascii")
    print(held, slot)
""")


class CrossProcess(FakeCase):
    INTERVAL = 2.0

    def setUp(self):
        super().setUp()
        self.pace = tempfile.TemporaryDirectory()
        self.addCleanup(self.pace.cleanup)

    def test_two_concurrent_processes_take_slots_at_least_the_interval_apart(self):
        script = Path(self.pace.name) / "waiter.py"
        script.write_text(WAITER, encoding="utf-8")
        folder = Path(self.pace.name) / "pace"
        argv = [sys.executable, "-I", str(script), str(ROOT), str(folder), str(self.INTERVAL)]
        processes = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        outputs = [process.communicate(timeout=60) for process in processes]
        for process, (_, err) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, err.decode("utf-8", "replace"))
        found = sorted((float(out.split()[1]), int(out.split()[0])) for out, _ in outputs)
        (first, held_first), (second, held_second) = found
        self.assertGreaterEqual(second - first, self.INTERVAL, "the second slot is a whole interval after the first")
        self.assertLess(held_first, 1000, "the first was not held")
        self.assertGreaterEqual(held_second, 1000, "the second was held, its lock wait included")

    def test_two_concurrent_launcher_examples_are_spaced_recorded_and_other_ops_unpaced(self):
        arrivals = []

        def example(request):
            arrivals.append(time.time())
            body = {"result": "No suitable example was found.\n", "search_status": "complete",
                    "solution_id": "12345678-1234-4234-8234-123456789abc"}
            return 200, {"Content-Type": "application/json", "X-Request-ID": f"req-ex{len(arrivals)}"}, \
                json.dumps(body).encode("utf-8")

        self.fake.script["/v1/examples/search"] = example
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "GITHITS_", "SYBERLABS_"))}
        env.update(self.env)
        env.update({githits_pace.PACE_DIR_ENV: str(Path(self.pace.name) / "pace"),
                    githits_pace.PACE_SECONDS_ENV: str(self.INTERVAL)})
        words = ["example", "retry with backoff"]
        argv = [sys.executable, "-I", str(ENTRY), *words]
        with launch_guard(words, words) as guard:  # the hook passed both launcher calls
            processes = [subprocess.Popen(argv, cwd=self.pace.name, env={**env, **guard}, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            for process in processes:
                _, err = process.communicate(timeout=120)
                self.assertEqual(process.returncode, 0, err.decode("utf-8", "replace"))
        self.assertEqual(len(arrivals), 2)
        # Each slot is stamped just before its request leaves, so arrivals sit a whole interval apart less the first
        # request's own few milliseconds of transit.
        self.assertGreaterEqual(abs(arrivals[1] - arrivals[0]), self.INTERVAL - 0.25)
        records = self.records().records
        self.assertEqual([r["op"] for r in records], ["example", "example"])
        held = sorted(r["paced_ms"] for r in records)
        self.assertLess(held[0], 1000)
        self.assertGreaterEqual(held[1], 1000)
        self.assertTrue(all(r["latency_ms"] < r["paced_ms"] or r["paced_ms"] < 1000 for r in records),
                        "latency_ms is the request's own time, never the pacing")
        # The other four operations, at once and with a long interval in force, are never held or stamped.
        stamp = (Path(self.pace.name) / "pace" / githits_pace.STAMP_FILE).read_text(encoding="ascii")
        with mock.patch.dict(os.environ, {githits_pace.PACE_DIR_ENV: env[githits_pace.PACE_DIR_ENV],
                                          githits_pace.PACE_SECONDS_ENV: "600"}):
            started = time.monotonic()
            for args in (["search", "q", "--target", "pypi:requests"], ["read", "pypi:requests"],
                         ["list", "pypi:requests"], ["grep", "Retry", "--target", "pypi:requests", "--literal"]):
                with self.subTest(op=args[0]):
                    code, _, err = run_entry(self.env, *args)
                    self.assertEqual((code, err), (0, ""))
            self.assertLess(time.monotonic() - started, 60)
        others = self.records().records[2:]
        self.assertEqual([r["op"] for r in others], ["search", "read", "list", "grep"])
        self.assertTrue(all("paced_ms" not in r for r in others))
        self.assertEqual((Path(self.pace.name) / "pace" / githits_pace.STAMP_FILE).read_text(encoding="ascii"), stamp)


class LauncherHook(FakeCase):
    def setUp(self):
        super().setUp()
        for name in PACE_ENV:
            patcher = mock.patch.dict(os.environ)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(name, None)

    def test_an_example_to_githits_is_paced_at_40_s_whatever_the_environment_says(self):
        request = githits.example_request("q")
        with mock.patch.dict(os.environ, {githits.URL_ENV: "https://api.githits.dev/v1",
                                          githits_pace.PACE_SECONDS_ENV: "0"}), \
                mock.patch.object(githits_pace, "wait", return_value=1234) as wait:
            self.assertEqual(githits_cli_entry._pace(request), 1234)
            self.assertEqual(wait.call_args.kwargs, {"interval": 40.0})
            os.environ.pop(githits.URL_ENV)
            self.assertEqual(githits_cli_entry._pace(request), 1234, "the default base is GitHits itself")
            for other in (githits.search_request("q", ["pypi:requests"]), githits.read_request("pypi:requests"),
                          githits.list_request("pypi:requests"), githits.grep_request("x", ["pypi:requests"])):
                self.assertIsNone(githits_cli_entry._pace(other))
            self.assertEqual(wait.call_count, 2)

    def test_a_loopback_stand_in_is_not_paced_unless_asked_and_a_bad_setting_sends_nothing(self):
        code, _, err = run_entry(self.env, "example", "retry")
        self.assertEqual((code, err), (0, ""))
        [record] = self.records().records
        self.assertNotIn("paced_ms", record, "unpaced: the record is exactly the fields it always had")
        with mock.patch.dict(os.environ, {githits_pace.PACE_SECONDS_ENV: "soon"}):
            code, _, err = run_entry(self.env, "example", "retry")
        self.assertEqual(code, 1)
        self.assertIn("pace_failed", err)
        self.assertEqual(len(self.fake.requests), 1, "nothing was sent")


class Records(FakeCase):
    def test_paced_ms_is_recorded_under_the_mac_and_read_back(self):
        self.client().call(githits.example_request("q"), paced_ms=39_250)
        self.client().search("q", ["pypi:requests"])
        records = self.records()
        self.assertEqual(records.ignored, 0)
        self.assertEqual([r.get("paced_ms") for r in records.records], [39_250, None])
        self.assertEqual(set(records.records[0]), set(githits.RECORD_FIELDS) | {"paced_ms"})
        self.assertEqual(set(records.records[1]), set(githits.RECORD_FIELDS))
        with self.assertRaises(githits.GitHitsError) as caught:
            self.client().call(githits.example_request("q"), paced_ms=-1)
        self.assertEqual(caught.exception.code, "bad_pacing")

    def test_a_tampered_or_malformed_paced_ms_is_ignored(self):
        self.client().call(githits.example_request("q"), paced_ms=40_000)
        path = Path(self.folder.name) / githits.RECORD_FILE
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        lines = [{**first, "paced_ms": 0}, {k: v for k, v in first.items() if k != "paced_ms"}]
        for bad in (None, -5, "40000", 1.5):
            forged = {**first, "paced_ms": bad}
            forged["mac"] = githits.record_mac({k: v for k, v in forged.items() if k != "mac"}, self.key)
            lines.append(forged)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        read = self.records()
        self.assertEqual((len(read.records), read.ignored), (1, 6))

    def test_the_lookup_fact_carries_paced_ms_only_when_the_record_does(self):
        self.client().call(githits.example_request("q"), paced_ms=12_000)
        self.client().search("q", ["pypi:requests"])
        paced, unpaced = (record_lookup(r, "pass") for r in self.records().records)
        self.assertEqual(paced["paced_ms"], 12_000)
        self.assertNotIn("paced_ms", unpaced)


NAME = "githits-pilot"


def paced_thread(case: str, paced=(), after=(), hours: float = 1.0, arm: str = "on"):
    """A labelled, accepted thread whose candidate passes ``hours`` after its search, with GitHits lookup facts
    holding ``paced`` milliseconds before the evaluation and ``after`` milliseconds after it."""
    j = Journal(case)
    j.add("case_created", {"actor": "agent@example.test", "contract": ["repo-change", 4],
                           "inputs": {"objective": "Work item X"}}, at=T0)
    j.fact("base", {"commit": "a" * 40})
    j.fact(experiment.KEY, {"name": NAME, "item": "X", "arm": arm, "tags": [], "by": "owner@example.test",
                            "at": "2026-10-24T00:00:00Z"}, version=NAME)
    search = j.add("search_started", {"id": "s1", "actor": "syberlabs.host"}, at=T0)
    for held in paced:
        j.fact("lookup", {"tool": "githits.example", "status": "ok", "guard": "pass", "ok": True, "paced_ms": held})
    j.fact("lookup", {"tool": "WebSearch", "guard": "pass", "ok": True, "paced_ms": 3_600_000})  # not GitHits' pacing
    signal = {"model_usage": {"input_tokens": 10, "output_tokens": 2, "model": "claude-sonnet-5-5"},
              "harness": {"name": "claude-code", "model": "claude-sonnet-5-5"}}
    if arm == "on":
        signal["githits"] = {"arm": "on", "records": len(paced), "launcher_calls": len(paced)}
    j.add("candidate_registered", {"id": "c1", "operator": "patch", "actor": "syberlabs.host", "signal": signal},
          at=T0 + 1)
    evaluated = j.add("candidate_evaluated", {"candidate": "c1", "actor": "syberlabs.host",
                                              "checks": [check("tests", "passed")]}, at=T0 + int(hours * HOUR))
    for held in after:
        j.fact("lookup", {"tool": "githits.example", "status": "ok", "guard": "pass", "ok": True, "paced_ms": held})
    j.add("proposed", {"id": "p1", "action": "accept_change", "actor": "runner@example.test",
                       "args": {"candidate": "c1", "commit": "b" * 40}}, at=T0 + int((hours + 1) * HOUR))
    j.add("decision", {"proposal_id": "p1", "status": "allowed", "reason": "allowed"}, at=T0 + int((hours + 1) * HOUR))
    j.add("effect_succeeded", {"proposal_id": "p1", "action": "accept_change", "output": {}},
          at=T0 + int((hours + 1) * HOUR) + 1)
    return j.events, search, evaluated


class Report(unittest.TestCase):
    def test_paced_time_is_left_out_of_time_to_pass_and_agent_time(self):
        events, search, evaluated = paced_thread("x-on", paced=(900_000, 900_000), after=(40_000,))
        [run] = experiment.runs(events, NAME)
        self.assertEqual(run.time_to_pass.value, 0.5, "one hour less the half hour the host held two examples")
        self.assertEqual(run.time_to_pass.basis, (search, evaluated))
        self.assertIn("less 1800 s the host held GitHits calls", run.time_to_pass.note)
        self.assertEqual(run.paced, 1840.0, "the whole thread's pacing, after the evaluation included")
        [plain] = experiment.runs(paced_thread("y-on")[0], NAME)
        self.assertEqual((plain.time_to_pass.value, plain.paced), (1.0, 0.0), "no pacing, nothing left out")
        self.assertNotIn("less", plain.time_to_pass.note)
        merged = dataclasses.replace(run, work=dataclasses.replace(
            run.work, agent_time=metrics.Metric(2.0, (), "case_created to merge")))
        summary = experiment._per_arm([merged])
        self.assertEqual((summary["agent_time_mean"], summary["paced_seconds"]), (round(2.0 - 1840 / 3600, 6), 1840.0))
        self.assertEqual(experiment._per_arm([plain])["paced_seconds"], 0)
        found = experiment.report(events + paced_thread("x-off", arm="off")[0], NAME)
        self.assertEqual((found["per_arm"]["on"]["paced_seconds"], found["per_arm"]["off"]["paced_seconds"]),
                         (1840.0, 0), found["rows"])
        self.assertIn("s of GitHits pacing left out of its time", experiment.table(found))


if __name__ == "__main__":
    unittest.main()
