"""scripts/run_shards.py: every discovered test runs exactly once, and any failure, crash or gap fails the run."""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=300)


def _serial(folder: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=300)


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
                units, stats = run_shards.units_for(tests, durations)
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
            self.assertNotIn("shard_incomplete", sharded.stdout)  # the fixture accounts for test_never

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


if __name__ == "__main__":
    unittest.main()
