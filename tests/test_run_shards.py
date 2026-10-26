"""scripts/run_shards.py: every discovered test runs exactly once, and any failure, crash or gap fails the run."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from examples import critic_rules
from syberlabs import checks
from syberlabs.gitspace import Repo

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_shards.py"

_spec = importlib.util.spec_from_file_location("run_shards", SCRIPT)
run_shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_shards)

# A small suite that exercises what unittest counts: passes, a method skip, a class skip, an
# expected failure, subtests, a class fixture, and a class whose setUpClass skips (its tests never start).
SUITE = {
    "test_alpha.py": """
        import unittest

        class Plain(unittest.TestCase):
            def test_one(self): pass
            def test_two(self): pass
            def test_three(self): pass
            @unittest.skip("not here")
            def test_skipped(self): pass
            @unittest.expectedFailure
            def test_expected(self): self.fail("expected")
            def test_subtests(self):
                for i in range(3):
                    with self.subTest(i=i):
                        self.assertGreaterEqual(i, 0)

        @unittest.skip("whole class")
        class Skipped(unittest.TestCase):
            def test_a(self): pass
            def test_b(self): pass
    """,
    "test_beta.py": """
        import unittest

        class Fixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.shared = []
            def test_first(self): self.shared.append(1)
            def test_second(self): self.shared.append(2)
            def test_third(self): self.assertEqual(len(self.shared), 2)

        class NoBackend(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("no backend here")
            def test_p(self): pass
            def test_q(self): pass

        class More(unittest.TestCase):
            def test_x(self): pass
            def test_y(self): pass
            def test_z(self): pass
    """,
    "test_gamma.py": "import unittest\n\nclass Gamma(unittest.TestCase):\n"
                     + "".join(f"    def test_{n:02d}(self): pass\n" for n in range(12)),
}


def _write_suite(folder: Path, extra: dict | None = None, durations: dict | None = None) -> Path:
    start = folder / "tests"
    start.mkdir()
    for name, body in {**SUITE, **(extra or {})}.items():
        (start / name).write_text(textwrap.dedent(body), encoding="utf-8")
    if durations is not None:
        (start / "durations.json").write_text(json.dumps(durations), encoding="utf-8")
    return start


def _shards(folder: Path, *args: str) -> subprocess.CompletedProcess:
    # Under a Build Thread host this suite itself runs with SYBERLABS_BASE set; the suites here are not that repository.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", run_shards.BASE_ENV)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=300)


def _serial(folder: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=300)


def _serial_suite(extra: dict) -> str:
    """Serial ``unittest discover`` of the synthetic suite plus ``extra``: its output."""
    with tempfile.TemporaryDirectory() as folder:
        _write_suite(Path(folder), extra)
        return _serial(Path(folder)).stderr


def _summary(text: str) -> tuple:
    """(tests run, final status line) from unittest-shaped output."""
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.MULTILINE)
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return (int(ran[-1]) if ran else None), lines[-1]


class Packing(unittest.TestCase):
    def ids(self, count):
        return [f"m.C.test_{n:03d}" for n in range(count)]

    def test_every_id_lands_in_exactly_one_shard_and_the_plan_checks_clean(self):
        ids = self.ids(97)
        units = [([i], float(n % 7) + 0.5) for n, i in enumerate(ids)]
        units.append((["m.F.test_a", "m.F.test_b"], 3.0))  # a fixture group stays together
        all_ids = ids + ["m.F.test_a", "m.F.test_b"]
        for n in (1, 2, 3, 8, 16, 200):
            shards = run_shards.pack(units, n)
            flat = [i for shard in shards for i in shard["ids"]]
            self.assertEqual(sorted(flat), sorted(all_ids), n)
            self.assertEqual(len(flat), len(set(flat)), n)
            self.assertEqual(run_shards.check_plan(shards, all_ids), [], n)
            self.assertEqual(len(shards), min(n, len(units)))
            owners = {i: k for k, shard in enumerate(shards) for i in shard["ids"]}
            self.assertEqual(owners["m.F.test_a"], owners["m.F.test_b"])

    def test_the_plan_check_names_a_duplicate_a_gap_and_a_stranger(self):
        shards = [{"ids": ["a", "b"], "seconds": 1}, {"ids": ["b", "z"], "seconds": 1}]
        problems = run_shards.check_plan(shards, ["a", "b", "c"])
        self.assertTrue(any("b is in shard 1 and shard 2" in p for p in problems), problems)
        self.assertIn("c is in no shard", problems)
        self.assertIn("z was not discovered", problems)

    def test_longest_first_packing_balances_the_shards(self):
        # The sum binds: the shards end within one small test of each other.
        units = [([f"t{n}"], s) for n, s in enumerate([90, 45, 39, 38, 32, 23, 21, 19, 19] + [1.0] * 500)]
        shards = run_shards.pack(units, 8)
        loads = sorted(shard["seconds"] for shard in shards)
        self.assertLessEqual(loads[-1] - loads[0], 1.0)
        self.assertLessEqual(loads[-1], sum(s for _, s in units) / 8 + 1.0)
        self.assertEqual(run_shards.pack(units, 8), shards)  # deterministic
        # The longest test binds: nothing is packed on top of it.
        units = [([f"t{n}"], s) for n, s in enumerate([90] + [1.0] * 100)]
        loads = sorted(shard["seconds"] for shard in run_shards.pack(units, 4))
        self.assertEqual(loads[-1], 90)

    def test_tests_with_no_recorded_time_still_spread_over_the_shards(self):
        units = [([f"t{n:02d}"], 0.0) for n in range(12)]
        self.assertEqual([len(shard["ids"]) for shard in run_shards.pack(units, 4)], [3, 3, 3, 3])

    def test_a_test_missing_from_the_manifest_gets_the_median(self):
        with tempfile.TemporaryDirectory() as folder:
            start = _write_suite(Path(folder))
            durations = {"test_alpha.Plain.test_one": 1.0, "test_alpha.Plain.test_two": 3.0,
                         "test_alpha.Plain.test_three": 5.0, "gone.Old.test_removed": 2.0}
            cwd, path = os.getcwd(), list(sys.path)
            os.chdir(folder)
            try:
                tests = run_shards.discover(str(start), "test*.py", None)
                records = [run_shards.describe(t, Path(folder).resolve()) for t in tests]
                units, stats = run_shards.units_for(records, durations)
            finally:
                os.chdir(cwd)
                sys.path[:] = path
                for name in ("test_alpha", "test_beta", "test_gamma"):
                    sys.modules.pop(name, None)
            self.assertEqual(stats["median"], 2.5)
            self.assertEqual(stats["timed"], 3)
            self.assertEqual(stats["stale"], 1)
            seconds = {tuple(ids): s for ids, s in units}
            self.assertEqual(seconds[("test_alpha.Plain.test_three",)], 5.0)
            self.assertEqual(seconds[("test_gamma.Gamma.test_00",)], 2.5)
            fixture = ("test_beta.Fixture.test_first", "test_beta.Fixture.test_second", "test_beta.Fixture.test_third")
            self.assertEqual(seconds[fixture], 7.5)
            self.assertEqual(sum(len(ids) for ids, _ in units), len(tests))


class EndToEnd(unittest.TestCase):
    def test_the_summary_matches_a_serial_run(self):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder))
            serial = _serial(Path(folder))
            self.assertEqual(serial.returncode, 0, serial.stderr)
            for shards in ("1", "3", "8"):
                sharded = _shards(Path(folder), "--shards", shards, "--report", "report.json")
                self.assertEqual(sharded.returncode, 0, sharded.stdout + sharded.stderr)
                self.assertEqual(_summary(sharded.stdout), _summary(serial.stderr), (shards, sharded.stdout))
                report = json.loads((Path(folder) / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["discovered"], 28)
                ids = [i for shard in report["shards"] for i in shard["ids"]]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(len(ids), 28)
                blocked = [i for shard in report["shards"] for i in shard["fixture_blocked"]]
                self.assertEqual(sorted(blocked), ["test_beta.NoBackend.test_p", "test_beta.NoBackend.test_q"])
            # unittest counts neither test of NoBackend in "Ran"; it reports one skip for the class.
            self.assertEqual(_summary(serial.stderr), (26, "OK (skipped=4, expected failures=1)"))

    def test_a_failing_test_fails_the_run_and_is_printed(self):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), {"test_delta.py": """
                import unittest

                class Broken(unittest.TestCase):
                    def test_fails(self): self.assertEqual(1, 2, "deliberate")
                    def test_errors(self): raise RuntimeError("deliberate error")

                class BrokenFixture(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls): raise RuntimeError("deliberate fixture error")
                    def test_never(self): pass

                class Parts(unittest.TestCase):  # each reports through a path other than add<Outcome>(self)
                    def test_only_subtests_fail(self):
                        for i in range(2):
                            with self.subTest(i=i):
                                self.fail("deliberate subtest")
                    def test_a_subtest_skips(self):
                        with self.subTest(i=0):
                            self.skipTest("deliberate subtest skip")
                    def test_cleanup_errors(self):
                        self.addCleanup(lambda: 1 / 0)
            """})
            serial = _serial(Path(folder))
            sharded = _shards(Path(folder), "--shards", "4")
            self.assertEqual(sharded.returncode, 1, sharded.stdout)
            self.assertNotEqual(serial.returncode, 0)
            self.assertEqual(_summary(sharded.stdout), _summary(serial.stderr))
            self.assertIn("FAIL: test_fails (test_delta.Broken", sharded.stdout)
            self.assertIn("ERROR: test_errors (test_delta.Broken", sharded.stdout)
            self.assertIn("deliberate error", sharded.stdout)
            self.assertIn("ERROR: setUpClass (test_delta.BrokenFixture)", sharded.stdout)
            # The fixture accounts for test_never; every Parts test reported an outcome before it stopped.
            self.assertNotIn("shard_incomplete", sharded.stdout)

    def test_a_module_that_cannot_import_fails_the_run(self):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), {"test_broken_import.py": "import no_such_module_anywhere\n"})
            sharded = _shards(Path(folder), "--shards", "2")
            self.assertEqual(sharded.returncode, 1, sharded.stdout)
            self.assertEqual(_summary(sharded.stdout), _summary(_serial(Path(folder)).stderr))

    def test_a_crashed_shard_fails_the_run_even_with_exit_code_zero(self):
        for how in ("os._exit(0)", "os._exit(3)", "os.kill(os.getpid(), signal.SIGTERM)"):
            with self.subTest(how=how), tempfile.TemporaryDirectory() as folder:
                _write_suite(Path(folder), {"test_crash.py": f"""
                    import os, signal, unittest

                    class Crash(unittest.TestCase):
                        def test_dies(self):
                            {how}
                """})
                sharded = _shards(Path(folder), "--shards", "3", "--report", "report.json")
                self.assertEqual(sharded.returncode, 1, sharded.stdout)
                self.assertIn("shard_crashed", sharded.stdout)
                self.assertIn("last test started: test_crash.Crash.test_dies", sharded.stdout)
                self.assertTrue(_summary(sharded.stdout)[1].startswith("FAILED ("), sharded.stdout)
                self.assertIn("shard_incomplete", _summary(sharded.stdout)[1])
                report = json.loads((Path(folder) / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["crashed"], 1)

    def test_a_test_added_after_the_manifest_still_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            start = _write_suite(Path(folder))
            base = _shards(Path(folder), "--shards", "2", "--write-durations", "measured.json")
            self.assertEqual(base.returncode, 0, base.stdout)
            manifest = json.loads((Path(folder) / "measured.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest), 26)  # NoBackend's two tests never start, so they have no time
            (start / "durations.json").write_text(json.dumps(manifest), encoding="utf-8")
            (start / "test_late.py").write_text(textwrap.dedent("""
                import os, unittest
                from pathlib import Path

                class Late(unittest.TestCase):
                    def test_added_later(self):
                        Path(os.environ["RUN_SHARDS_MARK"]).write_text("ran")
            """), encoding="utf-8")
            mark = Path(folder) / "mark.txt"
            env_before = os.environ.get("RUN_SHARDS_MARK")
            os.environ["RUN_SHARDS_MARK"] = str(mark)
            try:
                sharded = _shards(Path(folder), "--shards", "2", "--report", "report.json")
            finally:
                if env_before is None:
                    os.environ.pop("RUN_SHARDS_MARK", None)
                else:
                    os.environ["RUN_SHARDS_MARK"] = env_before
            self.assertEqual(sharded.returncode, 0, sharded.stdout)
            self.assertEqual(mark.read_text(), "ran")
            # test_late, and NoBackend's two tests, which never started in the measured run.
            self.assertIn("3 at the median", sharded.stdout)
            self.assertIn("2 shards", sharded.stdout)
            self.assertEqual(_summary(sharded.stdout)[0], 27)
            report = json.loads((Path(folder) / "report.json").read_text(encoding="utf-8"))
            self.assertIn("test_late.Late.test_added_later", [i for s in report["shards"] for i in s["ids"]])

    def test_each_shard_has_its_own_temporary_and_home_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            body = """
                import os, tempfile, unittest
                from pathlib import Path

                class Where(unittest.TestCase):
            """ + "".join(f"""
                    def test_{n}(self):
                        Path(os.environ["RUN_SHARDS_MARK"], "{n}").write_text(
                            tempfile.gettempdir() + "|" + str(Path.home()) + "|" + os.environ["TMP"])
            """ for n in range(4))
            _write_suite(Path(folder), {"test_where.py": body})
            marks = Path(folder) / "marks"
            marks.mkdir()
            env_before = os.environ.get("RUN_SHARDS_MARK")
            os.environ["RUN_SHARDS_MARK"] = str(marks)
            try:
                sharded = _shards(Path(folder), "--shards", "4")
            finally:
                if env_before is None:
                    os.environ.pop("RUN_SHARDS_MARK", None)
                else:
                    os.environ["RUN_SHARDS_MARK"] = env_before
            self.assertEqual(sharded.returncode, 0, sharded.stdout)
            seen = [p.read_text().split("|") for p in marks.iterdir()]
            self.assertEqual(len(seen), 4)
            for temp, home, tmp in seen:
                self.assertEqual(os.path.normcase(temp), os.path.normcase(tmp))
                self.assertNotEqual(os.path.normcase(temp), os.path.normcase(tempfile.gettempdir()))
                self.assertNotEqual(os.path.normcase(home), os.path.normcase(str(Path.home())))
            self.assertGreater(len({temp for temp, _, _ in seen}), 1)  # the four tests spread over shards



# --- the result channel: what a shard's stream must say ------------------------------------------

RECORDS = {
    "m.A.test_1": {"id": "m.A.test_1", "module": "m", "cls": "A"},
    "m.A.test_2": {"id": "m.A.test_2", "module": "m", "cls": "A"},
    "m.B.test_3": {"id": "m.B.test_3", "module": "m", "cls": "B"},
}
ASSIGNED = list(RECORDS)


def _ran(*ids, kind="success"):
    events = []
    for test_id in ids:
        events += [{"ev": "start", "id": test_id}, {"ev": "outcome", "id": test_id, "kind": kind},
                   {"ev": "stop", "id": test_id}]
    return events


def _done(tests_run, **counts):
    return {"ev": "done", "tests_run": tests_run, "failures": counts.get("failures", []),
            "errors": counts.get("errors", []), "unexpected": counts.get("unexpected", []),
            "skipped": counts.get("skipped", 0), "expected_failures": counts.get("expected_failures", 0)}


class Stream(unittest.TestCase):
    """The verdict comes from the stream of start, outcome and stop events; the summary must agree with it."""

    FIXTURE_SKIP = {"ev": "fixture", "kind": "skip", "description": "setUpClass (m.B)"}

    def account(self, messages):
        return run_shards.account(messages, ASSIGNED, {i: 1 for i in ASSIGNED}, RECORDS)

    def test_an_honest_stream_accounts_for_every_test(self):
        found = self.account(_ran("m.A.test_1", "m.A.test_2") + [self.FIXTURE_SKIP, _done(2, skipped=1)])
        self.assertEqual(found["problems"], [])
        self.assertFalse(found["crashed"])
        self.assertEqual(found["fixture_blocked"], ["m.B.test_3"])
        self.assertEqual((found["found"]["tests_run"], found["found"]["skipped"]), (2, 1))

    def test_a_summary_that_disagrees_with_the_stream_is_a_problem(self):
        for done in (_done(3, skipped=1), _done(2, skipped=1, failures=[{"id": "m.A.test_1"}]), _done(2, skipped=0)):
            with self.subTest(done=done):
                found = self.account(_ran("m.A.test_1", "m.A.test_2") + [self.FIXTURE_SKIP, done])
                self.assertEqual(len(found["problems"]), 1, found["problems"])
                self.assertIn("summary", found["problems"][0])

    def test_a_test_the_stream_never_started_is_a_problem_whatever_the_summary_says(self):
        found = self.account(_ran("m.A.test_1") + [self.FIXTURE_SKIP, _done(1, skipped=1)])
        self.assertEqual(found["problems"], ["never started 1 tests: m.A.test_2"])

    def test_events_out_of_their_order_are_problems(self):
        done = [self.FIXTURE_SKIP]
        cases = {
            "started m.A.test_2 while m.A.test_1 was running":
                [{"ev": "start", "id": "m.A.test_1"}] + _ran("m.A.test_2") + [{"ev": "stop", "id": "m.A.test_1"}]
                + done + [_done(2, skipped=1)],
            "outcome for m.A.test_2 outside its start and stop":
                _ran("m.A.test_1") + [{"ev": "outcome", "id": "m.A.test_2", "kind": "success"}] + _ran("m.A.test_2")
                + done + [_done(2, skipped=1)],
            "not assigned: m.Z.test_9": _ran("m.A.test_1", "m.A.test_2", "m.Z.test_9") + done + [_done(3, skipped=1)],
            "after done": _ran("m.A.test_1", "m.A.test_2") + done + [_done(2, skipped=1)] + _ran("m.B.test_3"),
            "after test code changed TestCase.run":
                [{"ev": "harness_changed", "id": "m.A.test_2", "what": ["TestCase.run"]}]
                + _ran("m.A.test_1", "m.A.test_2") + done + [_done(2, skipped=1)],
            "test code changed A.subTest while m.A.test_1 ran":
                _ran("m.A.test_1") + [{"ev": "harness_changed", "id": "m.A.test_1", "what": ["A.subTest"], "at": "stop"}]
                + _ran("m.A.test_2") + done + [_done(2, skipped=1)],
        }
        for expected, events in cases.items():
            with self.subTest(expected=expected):
                found = self.account(events)
                self.assertTrue(any(expected in problem for problem in found["problems"]), found["problems"])

    def test_a_test_that_stops_without_an_outcome_is_a_problem(self):
        silent = [{"ev": "start", "id": "m.A.test_2"}, {"ev": "stop", "id": "m.A.test_2"}]
        found = self.account(_ran("m.A.test_1") + silent + [self.FIXTURE_SKIP, _done(2, skipped=1)])
        self.assertEqual(found["problems"], ["stopped 1 tests that reported no outcome: m.A.test_2"])
        # A failed subtest is an outcome: unittest then reports no addFailure for the test itself.
        subtests = [{"ev": "start", "id": "m.A.test_2"}, {"ev": "outcome", "id": "m.A.test_2", "kind": "subfailure"},
                    {"ev": "stop", "id": "m.A.test_2"}]
        found = self.account(_ran("m.A.test_1") + subtests + [self.FIXTURE_SKIP,
                                                              _done(2, skipped=1, failures=[{"id": "m.A.test_2"}])])
        self.assertEqual(found["problems"], [])

    def test_a_stream_without_done_is_a_crash_with_its_last_started_test(self):
        found = self.account(_ran("m.A.test_1") + [{"ev": "start", "id": "m.A.test_2"}])
        self.assertTrue(found["crashed"])
        self.assertEqual(found["last_started"], "m.A.test_2")

    def test_only_lines_with_this_childs_nonce_in_sequence_are_trusted(self):
        def child(lines, overflow=False):
            made = object.__new__(run_shards._Child)
            made.nonce, made.lines, made.overflow = "n0nce", lines, overflow
            made._reader = threading.Thread(target=lambda: None)
            made._reader.start()
            made._reader.join()
            return made

        def line(seq, nonce="n0nce", event="start"):
            return json.dumps({"n": nonce, "seq": seq, "ev": event, "id": "m.A.test_1"}).encode() + b"\n"

        messages, broken = child([line(0), line(1, event="stop")]).messages()
        self.assertIsNone(broken)
        self.assertEqual([m["ev"] for m in messages], ["start", "stop"])
        for lines, reason in (([line(0), line(1, nonce="guess")], "nonce"), ([line(0), line(2)], "sequence"),
                              ([line(0), b"{not json\n"], "not JSON"), ([b"[1]\n"], "nonce")):
            with self.subTest(reason=reason):
                self.assertIn(reason, child(lines).messages()[1])
        self.assertIn("more than", child([line(0)], overflow=True).messages()[1])


# --- test code that tries to defeat the runner ---------------------------------------------------

RUN_FAKES_A_PASS = """
    import unittest

    class Fake(unittest.TestCase):
        def run(self, result=None):
            result.startTest(self); result.addSuccess(self); result.stopTest(self); return result
        def test_a(self): raise AssertionError("must run")
"""
ID_COLLIDES = """
    import unittest

    class Hidden(unittest.TestCase):
        def test_b(self): raise AssertionError("must run")
        def id(self): return "test_alpha.Plain.test_one"
"""
RUNTIME_PATCH = """
    import unittest

    class AAPatch(unittest.TestCase):
        @classmethod
        def setUpClass(cls): pass
        def test_0_patch(self):
            unittest.TestCase._callTestMethod = lambda self, method: None
        def test_1_body_never_runs(self): raise AssertionError("must run")
"""
CHANNEL_GARBAGE = """
    import gc, unittest
    import __main__

    class AAGarbage(unittest.TestCase):
        def test_0(self):
            channel = next(o for o in gc.get_objects() if isinstance(o, __main__._Channel))
            channel._out.write(b'{"ev": "done"}' + bytes([10]))
        def test_1(self): pass
"""
MUTES_ERRORS = """
    import unittest
    import unittest.case

    class AAMute(unittest.TestCase):
        @classmethod
        def setUpClass(cls): pass
        def test_0_mute(self):
            unittest.case._addError = lambda result, test, exc_info: None
        def test_1_failure_vanishes(self): raise AssertionError("must fail")
"""
REPORTS_XPASS_AS_PASS = """
    import unittest

    class Xpass(unittest.TestCase):
        def _addUnexpectedSuccess(self, result):
            result.addSuccess(self)
        @unittest.expectedFailure
        def test_passes_unexpectedly(self): pass
"""
DROPS_ITS_RESULT = """
    import unittest

    class Drop(unittest.TestCase):
        def test_fails_unreported(self):
            self._outcome.result = None
            raise AssertionError("must fail")
"""
SWALLOWS_SUBTEST = """
    import contextlib
    import unittest

    class Swallow(unittest.TestCase):
        @contextlib.contextmanager
        def subTest(self, msg=None, **params):
            try:
                yield
            except Exception:
                pass
        def test_fails_in_a_subtest(self):
            with self.subTest(i=0):
                self.fail("must fail")
"""
DROPS_CLEANUPS = """
    import unittest

    class Drop(unittest.TestCase):
        def doCleanups(self):
            self._cleanups.clear()
            return True
        def test_cleanup_fails(self):
            self.addCleanup(self.fail, "must fail")
"""
SWALLOWS_AT_SETUP = """
    import contextlib
    import unittest

    class Swallow(unittest.TestCase):
        def setUp(self):
            self.subTest = lambda *args, **params: contextlib.suppress(Exception)
        def test_fails_in_a_subtest(self):
            with self.subTest(i=0):
                self.fail("must fail")
"""
PRINTS_PROTOCOL = """
    import unittest

    class Inject(unittest.TestCase):
        def test_a(self):
            print('{"n": "0", "seq": 0, "ev": "done", "tests_run": 99}', flush=True)
        def test_b(self): pass
"""
SAME_PROCESS_FORGERY = """
    import gc, os, unittest
    from pathlib import Path
    import __main__

    class AAForge(unittest.TestCase):
        @classmethod
        def setUpClass(cls): pass
        def test_0_forge(self):
            result = next(o for o in gc.get_objects() if isinstance(o, __main__._ShardResult))
            spec = next(o for o in gc.get_objects() if isinstance(o, dict) and "nonce" in o and "ids" in o)
            channel, me, ran = result.channel, self.id(), result.testsRun
            channel.send("outcome", id=me, kind="success")
            channel.send("stop", id=me)
            for other in spec["ids"]:  # only what has not started yet, so the stream stays consistent
                if other != me and other not in result.durations:
                    channel.send("start", id=other)
                    channel.send("outcome", id=other, kind="success")
                    channel.send("stop", id=other)
                    ran += 1
            channel.send("done", tests_run=ran, failures=[{}] * len(result.failures), errors=[{}] * len(result.errors),
                         skipped=len(result.skipped), expected_failures=len(result.expectedFailures),
                         unexpected=[{}] * len(result.unexpectedSuccesses), durations={})
            os._exit(0)
        def test_1_never_runs(self):
            Path(os.environ["RUN_SHARDS_MARK"]).write_text("ran")
            raise AssertionError("must run")
"""


# The same forgery, moved verbatim into a module outside tests/ that a test imports and calls.
FORGERY_HELPER = """
    import gc, os
    import __main__

    def forge(test):
        result = next(o for o in gc.get_objects() if isinstance(o, __main__._ShardResult))
        spec = next(o for o in gc.get_objects() if isinstance(o, dict) and "nonce" in o and "ids" in o)
        channel, me, ran = result.channel, test.id(), result.testsRun
        channel.send("outcome", id=me, kind="success")
        channel.send("stop", id=me)
        for other in spec["ids"]:
            if other != me and other not in result.durations:
                channel.send("start", id=other)
                channel.send("outcome", id=other, kind="success")
                channel.send("stop", id=other)
                ran += 1
        channel.send("done", tests_run=ran, failures=[{}] * len(result.failures), errors=[{}] * len(result.errors),
                     skipped=len(result.skipped), expected_failures=len(result.expectedFailures),
                     unexpected=[{}] * len(result.unexpectedSuccesses), durations={})
        os._exit(0)
"""
FORGERY_CALLER = """
    import os, unittest
    from pathlib import Path
    from forge_helper import forge

    class AAForge(unittest.TestCase):
        @classmethod
        def setUpClass(cls): pass
        def test_0_forge(self):
            forge(self)
        def test_1_never_runs(self):
            Path(os.environ["RUN_SHARDS_MARK"]).write_text("ran")
            raise AssertionError("must run")
"""


class Integrity(unittest.TestCase):
    """Test code runs in the shard's process; these are the ways it can try to lie, and what catches each."""

    def run_suite(self, extra, *args, mark=None):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), extra)
            before = os.environ.get("RUN_SHARDS_MARK")
            if mark:
                os.environ["RUN_SHARDS_MARK"] = mark
            try:
                return _shards(Path(folder), "--shards", "2", *args)
            finally:
                if before is None:
                    os.environ.pop("RUN_SHARDS_MARK", None)
                else:
                    os.environ["RUN_SHARDS_MARK"] = before

    def test_a_module_that_exits_at_import_fails_discovery_and_not_the_judge(self):
        for how in ("os._exit(0)", "os.kill(os.getpid(), signal.SIGTERM)"):
            with self.subTest(how=how):
                ran = self.run_suite({"test_zz.py": f"import os, signal\n{how}\n"})
                self.assertEqual(ran.returncode, 1, ran.stdout)
                self.assertIn("run_shards: discovery_failed: discovery exited with code", ran.stdout)
                self.assertEqual(_summary(ran.stdout), (0, "FAILED (discovery_failed=1)"))
        # SystemExit is an exception: unittest's loader turns it into a module that failed to import.
        ran = self.run_suite({"test_zz.py": "raise SystemExit(0)\n"})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("ERROR: test_zz (unittest.loader._FailedTest", ran.stdout)

    def test_a_test_that_takes_over_how_it_is_started_or_named_is_refused_before_anything_runs(self):
        for name, body, says in (("test_fake.py", RUN_FAKES_A_PASS, "test_fake.Fake.test_a takes run from outside"),
                                 ("test_hidden.py", ID_COLLIDES, "test_alpha.Plain.test_one takes id from outside")):
            with self.subTest(name=name):
                ran = self.run_suite({name: body})
                self.assertEqual(ran.returncode, 1, ran.stdout)
                self.assertIn(f"run_shards: harness_override: {says} unittest", ran.stdout)
                self.assertEqual(_summary(ran.stdout), (0, "FAILED (harness_override=1)"))

    def test_a_test_that_patches_unittest_while_the_shard_runs_fails_it(self):
        ran = self.run_suite({"test_patch.py": RUNTIME_PATCH})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("test code changed TestCase._callTestMethod while test_patch.AAPatch.test_0_patch ran",
                      ran.stdout)

    def test_a_test_that_mutes_how_unittest_reports_an_error_fails_the_shard(self):
        """``TestCase.run`` reports errors and skips through functions of ``unittest.case``; a no-op there makes
        every later failure vanish, and serial ``unittest`` says OK."""
        ran = self.run_suite({"test_mute.py": MUTES_ERRORS})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("test code changed unittest.case._addError while test_mute.AAMute.test_0_mute ran", ran.stdout)
        self.assertIn("stopped 1 tests that reported no outcome: test_mute.AAMute.test_1_failure_vanishes",
                      ran.stdout)
        self.assertTrue(_summary(_serial_suite({"test_mute.py": MUTES_ERRORS}))[1].startswith("OK"))  # serial unittest is fooled

    def test_a_test_that_reports_its_own_unexpected_success_as_a_pass_is_refused_before_anything_runs(self):
        ran = self.run_suite({"test_xpass.py": REPORTS_XPASS_AS_PASS})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("run_shards: harness_override: test_xpass.Xpass.test_passes_unexpectedly takes "
                      "_addUnexpectedSuccess from outside unittest", ran.stdout)
        self.assertEqual(_summary(ran.stdout), (0, "FAILED (harness_override=1)"))
        self.assertTrue(_summary(_serial_suite({"test_xpass.py": REPORTS_XPASS_AS_PASS}))[1].startswith("OK"))  # serial unittest is fooled

    def test_a_test_that_stops_without_reporting_an_outcome_fails_the_shard(self):
        """No watched function changes here: the test drops the result its failure is reported to. The stream
        shows a start and a stop and nothing between, which unittest never sends for a test it ran."""
        ran = self.run_suite({"test_drop.py": DROPS_ITS_RESULT})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("stopped 1 tests that reported no outcome: test_drop.Drop.test_fails_unreported", ran.stdout)
        self.assertNotIn("after test code changed", ran.stdout)
        self.assertTrue(_summary(_serial_suite({"test_drop.py": DROPS_ITS_RESULT}))[1].startswith("OK"))  # serial unittest is fooled

    def test_the_critic_flags_each_way_of_misreporting_an_outcome(self):
        for name, body, rule in (("test_mute.py", MUTES_ERRORS, "unittest_patch"),
                                 ("test_xpass.py", REPORTS_XPASS_AS_PASS, "harness_override"),
                                 ("test_drop.py", DROPS_ITS_RESULT, "unittest_patch")):
            with self.subTest(name=name):
                findings = critic_rules.integrity_findings(f"tests/{name}", textwrap.dedent(body).lstrip(), None)
                self.assertEqual({f["claim"].split("(")[1].split(")")[0] for f in findings}, {rule})
                self.assertTrue(all(f["severity"] == "high" for f in findings))

    def test_a_test_that_swallows_its_own_subtest_or_cleanup_failures_is_refused_before_anything_runs(self):
        for name, body, method, test_id in (("test_swallow.py", SWALLOWS_SUBTEST, "subTest",
                                             "test_swallow.Swallow.test_fails_in_a_subtest"),
                                            ("test_cleanups.py", DROPS_CLEANUPS, "doCleanups",
                                             "test_cleanups.Drop.test_cleanup_fails")):
            with self.subTest(method=method):
                ran = self.run_suite({name: body})
                self.assertEqual(ran.returncode, 1, ran.stdout)
                self.assertIn(f"run_shards: harness_override: {test_id} takes {method} from outside unittest",
                              ran.stdout)
                self.assertEqual(_summary(ran.stdout), (0, "FAILED (harness_override=1)"))
                self.assertTrue(_summary(_serial_suite({name: body}))[1].startswith("OK"))  # serial unittest is fooled

    def test_each_cleanup_and_subtest_method_is_refused_in_a_test_class_whatever_it_does(self):
        """Refused by where the method comes from, not by what it does: even ``unittest``'s own function,
        assigned in the class, is taken from outside ``unittest``."""
        body = "import unittest\n\n" + "".join(
            f"class T{i}(unittest.TestCase):\n    {name} = unittest.TestCase.{name}\n    def test_a(self): pass\n\n"
            for i, name in enumerate(run_shards.CLEANUP_METHODS))
        ran = self.run_suite({"test_takes.py": body})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        for i, name in enumerate(run_shards.CLEANUP_METHODS):
            self.assertIn(f"harness_override: test_takes.T{i}.test_a takes {name} from outside unittest", ran.stdout)
        self.assertEqual(_summary(ran.stdout),
                         (0, f"FAILED (harness_override={len(run_shards.CLEANUP_METHODS)})"))

    def test_a_test_that_swallows_its_subtests_from_set_up_fails_the_shard(self):
        """The instance takes ``subTest`` in ``setUp``, after the check at the test's start; the check at its
        stop sees it."""
        ran = self.run_suite({"test_setup.py": SWALLOWS_AT_SETUP})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("test code changed Swallow.subTest while test_setup.Swallow.test_fails_in_a_subtest ran",
                      ran.stdout)
        self.assertTrue(_summary(_serial_suite({"test_setup.py": SWALLOWS_AT_SETUP}))[1].startswith("OK"))  # serial unittest is fooled

    def test_the_critic_flags_each_way_of_swallowing_a_failure(self):
        for name, body, rule in (("test_swallow.py", SWALLOWS_SUBTEST, "harness_override"),
                                 ("test_cleanups.py", DROPS_CLEANUPS, "harness_override"),
                                 ("test_setup.py", SWALLOWS_AT_SETUP, "unittest_patch")):
            with self.subTest(name=name):
                findings = critic_rules.integrity_findings(f"tests/{name}", textwrap.dedent(body).lstrip(), None)
                self.assertEqual({f["claim"].split("(")[1].split(")")[0] for f in findings}, {rule})
                self.assertTrue(all(f["severity"] == "high" for f in findings))

    def test_bytes_on_the_channel_without_the_nonce_discard_the_shard(self):
        ran = self.run_suite({"test_garbage.py": CHANNEL_GARBAGE})
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("run_shards: channel_tampered: shard", ran.stdout)
        self.assertIn("does not carry this child's nonce", ran.stdout)
        self.assertIn("channel_tampered=1", _summary(ran.stdout)[1])

    def test_protocol_lines_a_test_prints_go_to_its_log_and_count_for_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), {"test_inject.py": PRINTS_PROTOCOL})
            ran = _shards(Path(folder), "--shards", "2", "--log-dir", "logs")
            self.assertEqual(ran.returncode, 0, ran.stdout)
            self.assertEqual(_summary(ran.stdout), _summary(_serial(Path(folder)).stderr))
            logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (Path(folder) / "logs").iterdir())
            self.assertIn('"tests_run": 99', logs)

    def test_same_process_forgery_passes_the_runner_and_the_critic_flags_it(self):
        """The residual that docs/TESTING.md and spec/GAPS.md name: test code that reaches the runner in its own
        process can write a stream that is consistent and false. The runner cannot tell; the reference critic's
        test-integrity rule flags the lines that do it; only a sandbox would close it."""
        with tempfile.TemporaryDirectory() as folder:
            mark = Path(folder) / "mark.txt"
            ran = self.run_suite({"test_forge.py": SAME_PROCESS_FORGERY}, mark=str(mark))
            self.assertEqual(ran.returncode, 0, ran.stdout)  # fooled: test_1_never_runs is counted and never ran
            self.assertFalse(mark.exists())
        source = textwrap.dedent(SAME_PROCESS_FORGERY).lstrip()
        findings = critic_rules.integrity_findings("tests/test_forge.py", source, None)
        self.assertEqual({f["claim"].split("(")[1].split(")")[0] for f in findings}, {"runner_reach", "process_exit"})
        self.assertTrue(all(f["severity"] == "high" for f in findings))

    def test_the_same_forgery_moved_into_a_helper_module_passes_the_runner_and_the_critic_flags_the_helper(self):
        """Verifier finding 1 on #116: the forgery one ``import`` away from the test. The runner is still fooled
        (only a sandbox closes that); the critic now reads every changed Python file, so the helper is high."""
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), {"test_forge.py": FORGERY_CALLER})
            (Path(folder) / "forge_helper.py").write_text(textwrap.dedent(FORGERY_HELPER), encoding="utf-8")
            mark = Path(folder) / "mark.txt"
            before = os.environ.get("RUN_SHARDS_MARK")
            os.environ["RUN_SHARDS_MARK"] = str(mark)
            try:
                ran = _shards(Path(folder), "--shards", "2")
            finally:
                if before is None:
                    os.environ.pop("RUN_SHARDS_MARK", None)
                else:
                    os.environ["RUN_SHARDS_MARK"] = before
            self.assertEqual(ran.returncode, 0, ran.stdout)  # fooled: test_1_never_runs is counted and never ran
            self.assertFalse(mark.exists())
        helper = critic_rules.integrity_findings("forge_helper.py", textwrap.dedent(FORGERY_HELPER).lstrip(), None)
        self.assertEqual({f["claim"].split("(")[1].split(")")[0] for f in helper}, {"runner_reach"})
        self.assertTrue(all(f["severity"] == "high" for f in helper))
        self.assertEqual(critic_rules.integrity_findings("tests/test_forge.py",
                                                         textwrap.dedent(FORGERY_CALLER).lstrip(), None), [])


# --- a shard's time budget ---------------------------------------------------------------------

SLEEPER = """
    import os, subprocess, sys, time, unittest

    BEAT = "import sys, time\\nwhile True:\\n    open(sys.argv[1], 'w').write(str(time.time()))\\n    time.sleep(0.1)\\n"

    class Sleeper(unittest.TestCase):
        def test_sleeps(self):
            subprocess.Popen([sys.executable, "-c", BEAT, os.environ["RUN_SHARDS_MARK"]])
            time.sleep(120)
"""


class Timeout(unittest.TestCase):
    """``--shard-timeout``: a shard past its budget is stopped with every process it started, and named."""

    def test_a_shard_past_its_budget_is_stopped_with_its_process_tree_and_names_its_last_test(self):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), {"test_sleep.py": SLEEPER})
            beat = Path(folder) / "beat.txt"
            before = os.environ.get("RUN_SHARDS_MARK")
            os.environ["RUN_SHARDS_MARK"] = str(beat)
            try:
                ran = _shards(Path(folder), "--shards", "1", "--shard-timeout", "6")
            finally:
                if before is None:
                    os.environ.pop("RUN_SHARDS_MARK", None)
                else:
                    os.environ["RUN_SHARDS_MARK"] = before
            self.assertEqual(ran.returncode, 1, ran.stdout)
            self.assertIn("run_shards: shard_timeout: shard 1 ran past its budget of 6 s and was stopped with its "
                          "process tree; last test started: test_sleep.Sleeper.test_sleeps", ran.stdout)
            self.assertIn("run_shards: shard 1/1: TIMED OUT after", ran.stdout)
            self.assertEqual(_summary(ran.stdout)[1], "FAILED (shard_timeout=1)")
            # The process the test started is gone too: its heartbeat stops.
            self.assertTrue(beat.exists(), "the sleeping test never started its child")
            first = beat.stat().st_mtime_ns
            time.sleep(1.5)
            self.assertEqual(beat.stat().st_mtime_ns, first, "the test's child outlived its shard")

    def test_discovery_that_hangs_at_import_is_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), {"test_zz_hang.py": "import time\ntime.sleep(120)\n"})
            ran = _shards(Path(folder), "--shards", "2", "--shard-timeout", "4")
            self.assertEqual(ran.returncode, 1, ran.stdout)
            self.assertIn("run_shards: discovery_failed: discovery ran past its budget of 4 s and was stopped with "
                          "its process tree", ran.stdout)
            self.assertEqual(_summary(ran.stdout), (0, "FAILED (discovery_failed=1)"))

    def test_the_default_budget_is_a_multiple_of_the_plan_and_at_least_five_minutes(self):
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder), durations={"test_gamma.Gamma.test_00": 120.0, "test_alpha.Plain.test_one": 2.0})
            ran = _shards(Path(folder), "--shards", "2")
            self.assertEqual(ran.returncode, 0, ran.stdout)
            line = next(l for l in ran.stdout.splitlines() if l.startswith("run_shards: planned seconds per shard: "))
            planned, budgets = line.split(": ", 2)[2].split("; time budget per shard: ")
            budgets = budgets.split(" (")[0]
            planned = [float(x) for x in planned.split(", ")]
            self.assertEqual([float(x) for x in budgets.split(", ")],
                             [float(f"{max(run_shards.SHARD_TIMEOUT_FLOOR, run_shards.SHARD_TIMEOUT_FACTOR * x):.0f}")
                              for x in planned])
            self.assertTrue(any(x > 100 for x in planned), line)  # the 120 s test makes one budget exceed the floor
            self.assertIn(f"({run_shards.SHARD_TIMEOUT_FACTOR:g}x planned, at least 300)", line)
        with tempfile.TemporaryDirectory() as folder:
            _write_suite(Path(folder))
            for bad in ("0", "-5"):
                with self.subTest(bad=bad):
                    ran = _shards(Path(folder), "--shard-timeout", bad)
                    self.assertEqual(ran.returncode, 2, ran.stderr)
                    self.assertIn("--shard-timeout must be a positive number of seconds", ran.stderr)


# --- the floor: a test discovered on the base may not silently disappear -------------------------

GIT = ["git", "-c", "user.name=run_shards test", "-c", "user.email=run-shards@example.test",
       "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
KEEP = """
    import unittest

    class Keep(unittest.TestCase):
        def test_a(self): pass
        def test_b(self): pass
"""
GONE = """
    import unittest

    class Gone(unittest.TestCase):
        def test_x(self): pass
"""


def _git(folder: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=folder, capture_output=True, text=True, check=True).stdout.strip()


class Floor(unittest.TestCase):
    """``--floor BASE``: every test discovered on BASE is discovered on HEAD, or the diff BASE..HEAD says it went."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        _git(self.folder, "init", "-q")
        self.commit({"tests/test_keep.py": KEEP, "tests/test_gone.py": GONE}, "base")
        self.base = _git(self.folder, "rev-parse", "HEAD")

    def write(self, files):
        for name, body in files.items():
            path = self.folder / name
            if body is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8", newline="\n")

    def commit(self, files, message="candidate"):
        self.write(files)
        _git(self.folder, "add", "-A")
        _git(self.folder, "commit", "-q", "--allow-empty", "-m", message)

    def reset(self):
        _git(self.folder, "reset", "-q", "--hard", self.base)
        _git(self.folder, "clean", "-q", "-fdx")

    def floor(self, base=None):
        return _shards(self.folder, "--shards", "2", "--floor", base or self.base)

    def test_a_candidate_that_adds_tests_passes(self):
        self.commit({"tests/test_new.py": "import unittest\n\nclass New(unittest.TestCase):\n    def test_n(self): pass\n"})
        ran = self.floor()
        self.assertEqual(ran.returncode, 0, ran.stdout)
        self.assertIn("floor: all 3 tests discovered on the base are here or excused by the diff", ran.stdout)
        self.assertEqual(_summary(ran.stdout), (4, "OK"))

    def test_a_test_file_the_diff_deletes_passes(self):
        self.commit({"tests/test_gone.py": None})
        ran = self.floor()
        self.assertEqual(ran.returncode, 0, ran.stdout)
        self.assertEqual(_summary(ran.stdout), (2, "OK"))

    def test_a_test_that_stops_being_defined_without_the_diff_removing_it_is_refused(self):
        cases = {
            "a conditional deletes it": (KEEP + "\n    if True:\n        del Keep.test_b\n", ["test_keep.Keep.test_b"]),
            "its class stops being a TestCase": (
                KEEP.replace("class Keep(unittest.TestCase):",
                             "Base = object if True else unittest.TestCase\n\n    class Keep(Base):"),
                ["test_keep.Keep.test_a", "test_keep.Keep.test_b"]),
            "a module-level assignment hides it": (KEEP + "\n    Keep.test_a = None\n", ["test_keep.Keep.test_a"]),
        }
        for name, (body, missing) in cases.items():
            with self.subTest(name):
                self.reset()
                self.commit({"tests/test_keep.py": body})
                ran = self.floor()
                self.assertEqual(ran.returncode, 1, ran.stdout)
                for test_id in missing:
                    self.assertIn(f"run_shards: test_set_shrank: {test_id}: the diff deletes neither its file, "
                                  "its def nor its class", ran.stdout)
                self.assertEqual(_summary(ran.stdout), (0, f"FAILED (test_set_shrank={len(missing)})"))

    def test_a_skipped_test_is_still_discovered_so_the_floor_does_not_see_skips(self):
        self.commit({"tests/test_keep.py": KEEP.replace("    def test_b", "    @unittest.skip('later')\n        def test_b")})
        ran = self.floor()
        self.assertEqual(ran.returncode, 0, ran.stdout)
        self.assertEqual(_summary(ran.stdout), (3, "OK (skipped=1)"))

    def test_a_removal_or_rename_in_the_diff_passes(self):
        cases = {
            "a renamed test": {"tests/test_keep.py": KEEP.replace("def test_b", "def test_b_renamed")},
            "a removed test": {"tests/test_keep.py": KEEP.replace("        def test_b(self): pass\n", "")},
            "a renamed class": {"tests/test_keep.py": KEEP.replace("class Keep(", "class Kept(")},
            "a moved module": {"tests/test_gone.py": None, "tests/test_moved.py": GONE},
        }
        for name, files in cases.items():
            with self.subTest(name):
                self.reset()
                self.commit(files)
                ran = self.floor()
                self.assertEqual(ran.returncode, 0, ran.stdout)

    def test_the_working_tree_must_be_the_committed_tree(self):
        cases = {
            "untracked tests/test_extra.py": {"tests/test_extra.py": GONE.replace("Gone", "Extra")},
            "modified tests/test_keep.py": {"tests/test_keep.py": KEEP.replace("pass", "self.fail()")},
            "ignored tests/__pycache__/": {"tests/__pycache__/test_keep.cpython-312.pyc": "not really bytecode"},
            "untracked helper.pth": {"helper.pth": "import os"},
            "ignored build/ (holds lib/helper.py)": {"build/lib/helper.py": "import os"},
        }
        for change, files in cases.items():
            with self.subTest(change):
                self.reset()
                self.commit({".gitignore": "__pycache__/\n*.egg-info/\nbuild/\n"})
                self.write(files)
                ran = self.floor()
                self.assertEqual(ran.returncode, 1, ran.stdout)
                self.assertIn(f"run_shards: worktree_changed: {change}", ran.stdout)

    def test_an_ignored_directory_with_nothing_importable_is_not_a_change(self):
        """``pip install -e .`` leaves an ignored ``*.egg-info/`` beside the code; CI floors after that install."""
        self.commit({".gitignore": "*.egg-info/\n"})
        self.write({"demo.egg-info/PKG-INFO": "Metadata-Version: 2.1\n", "demo.egg-info/top_level.txt": "demo\n"})
        ran = self.floor()
        self.assertEqual(ran.returncode, 0, ran.stdout)
        self.assertIn("floor: all 3 tests discovered on the base are here or excused by the diff", ran.stdout)

    def test_a_base_that_is_not_a_commit_here_fails_closed(self):
        ran = self.floor("0" * 40)
        self.assertEqual(ran.returncode, 1, ran.stdout)
        self.assertIn("run_shards: floor_unavailable: base 0000000000", ran.stdout)

    def test_the_host_hands_the_base_to_the_tests_check(self):
        """Through ``syberlabs.checks.evaluate``: SYBERLABS_BASE reaches the check, so the host-checkout runner floors."""
        evaluation = {"checks": {"tests": {"argv": [sys.executable, str(SCRIPT), "--shards", "2"], "timeout_seconds": 300}},
                      "max_output_bytes": 1_000_000}
        for name, files, state, says in (
                ("shrunk", {"tests/test_keep.py": KEEP + "\n    del Keep.test_b\n"}, "failed",
                 "test_set_shrank: test_keep.Keep.test_b"),
                ("grown", {"tests/test_new.py": GONE.replace("Gone", "New")}, "passed", "floor: all 3 tests")):
            with self.subTest(name):
                self.reset()
                self.commit(files)
                head = _git(self.folder, "rev-parse", "HEAD")
                candidate = {"id": "c1", "commit": head, "tree": _git(self.folder, "rev-parse", "HEAD^{tree}"),
                             "base": self.base, "provider": {"name": "fixture", "revision": "1"}}
                record = checks.evaluate(Repo(self.folder), candidate, evaluation)
                [result] = record["checks"]
                self.assertEqual(result["state"], state, result["output_tail"])
                self.assertIn(says, result["output_tail"])


if __name__ == "__main__":
    unittest.main()
