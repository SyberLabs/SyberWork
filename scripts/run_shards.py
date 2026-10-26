"""Run the unit suite as parallel shards, and prove that every discovered test ran.

    python scripts/run_shards.py                      # N = min(os.cpu_count(), 8)
    python scripts/run_shards.py --shards 4 --report shards.json

The test set is exactly what ``python -m unittest discover -s tests`` finds: the same
loader, start directory and pattern. Tests are packed into N shards by test id, longest
first onto the least-loaded shard, using the committed timing manifest
``tests/durations.json`` (test id -> seconds); a test the manifest does not name gets the
manifest's median. The tests of a class that defines ``setUpClass`` (or a module that
defines ``setUpModule``) stay in one shard, so a shared fixture is built once and its
tests keep the order and state ``unittest`` gives them. Each shard is a subprocess that
discovers the suite again, keeps its assigned ids in discovery order, and runs them
with ``unittest.TextTestRunner``; its output goes to its own log, and it gets its own
``TMP``/``TEMP``/``TMPDIR`` and ``HOME``/``USERPROFILE`` scratch directories.

Completeness, checked twice and never assumed:

* before running, the union of the shards' ids equals the discovered set and no id is
  in two shards;
* after running, every shard wrote a result, found every id it was assigned, and
  started each of them (or names the class or module fixture that skipped or failed
  before it could start, as ``unittest`` itself does); the totals add up to the
  discovered count.

Anything else is ``shard_incomplete`` or ``shard_crashed`` and fails the run. The run
also fails when any shard has a failure, an error, or an unexpected success. The last
lines have the shape ``unittest`` prints: ``Ran N tests in Xs``, then ``OK (...)`` or
``FAILED (...)``. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SEPARATOR1 = "=" * 70
SEPARATOR2 = "-" * 70
DEFAULT_SECONDS = 1.0  # used only when the manifest is missing or empty


# --- discovery -------------------------------------------------------------------------------


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def discover(start: str, pattern: str, top_level: str | None) -> list:
    """The test cases ``python -m unittest discover -s START -p PATTERN [-t TOP]`` would run, in its order.

    A fresh ``TestLoader``, which is what ``unittest.defaultTestLoader`` is in a process that has not
    discovered before: on Python 3.11 the shared loader keeps the top-level directory of an earlier
    ``discover`` call, so a second call in the same process could resolve a different start directory.
    """
    return list(_flatten(unittest.TestLoader().discover(start, pattern, top_level)))


def _fixture_key(test) -> str | None:
    """A key shared by tests that must stay in one shard: a class with ``setUpClass``, a module with ``setUpModule``."""
    cls = type(test)
    module = sys.modules.get(cls.__module__)
    if module is not None and callable(getattr(module, "setUpModule", None)):
        return "module:" + cls.__module__
    if isinstance(test, unittest.TestCase) and cls.setUpClass.__func__ is not unittest.TestCase.setUpClass.__func__:
        return f"class:{cls.__module__}.{cls.__qualname__}"
    return None


# --- packing ---------------------------------------------------------------------------------


def load_durations(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object of test id -> seconds")
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def units_for(tests: list, durations: dict) -> tuple[list, dict]:
    """Group discovered tests into packing units: ``[(ids, seconds)]`` in first-discovery order."""
    median = statistics.median(durations.values()) if durations else DEFAULT_SECONDS
    order: list = []
    groups: dict = {}
    timed = unknown = 0
    seen: set = set()
    for test in tests:
        test_id = test.id()
        key = _fixture_key(test) or "test:" + test_id
        if key not in groups:
            groups[key] = []
            order.append(key)
        if test_id not in seen:  # a repeated id is one unit entry; every occurrence runs where the id goes
            seen.add(test_id)
            groups[key].append(test_id)
            if test_id in durations:
                timed += 1
            else:
                unknown += 1
    units = []
    for key in order:
        ids = groups[key]
        units.append((ids, sum(durations.get(i, median) for i in ids)))
    stats = {"timed": timed, "unknown": unknown, "median": median,
             "stale": len(set(durations) - seen)}
    return units, stats


def pack(units: list, n: int) -> list:
    """Greedy longest-processing-time packing: each unit, longest first, onto the least-loaded shard.

    Returns ``[{"ids": [...], "seconds": planned}]``. Ties break on fewer tests, then the lower shard
    index (units of equal length go in order of their first id), so the plan is deterministic and
    tests with no recorded time still spread. Empty shards are dropped.
    """
    n = max(1, n)
    shards = [{"ids": [], "seconds": 0.0} for _ in range(n)]
    for ids, seconds in sorted(units, key=lambda u: (-u[1], u[0][0])):
        target = min(range(n), key=lambda i: (shards[i]["seconds"], len(shards[i]["ids"]), i))
        shards[target]["ids"].extend(ids)
        shards[target]["seconds"] += seconds
    return [shard for shard in shards if shard["ids"]]


def check_plan(shards: list, discovered: list) -> list:
    """Problems with a plan: an id in two shards, an id in no shard, an id that was not discovered."""
    problems = []
    owner: dict = {}
    for index, shard in enumerate(shards):
        for test_id in shard["ids"]:
            if test_id in owner:
                problems.append(f"{test_id} is in shard {owner[test_id] + 1} and shard {index + 1}")
            owner[test_id] = index
    wanted = set(discovered)
    problems += [f"{test_id} is in no shard" for test_id in sorted(wanted - set(owner))]
    problems += [f"{test_id} was not discovered" for test_id in sorted(set(owner) - wanted)]
    return problems


# --- one shard (the child process) -----------------------------------------------------------


class _ShardResult(unittest.TextTestResult):
    def __init__(self, *args, progress=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress = progress
        self.started: list = []
        self.durations: dict = {}
        self.fixture_events: list = []  # (kind, description) of setUpClass/setUpModule errors and skips
        self._mark = time.perf_counter()

    def startTest(self, test):
        self.started.append(test.id())
        if self.progress is not None:
            self.progress.write(test.id() + "\n")
            self.progress.flush()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        now = time.perf_counter()
        # Time since the previous test ended: includes this test's class and module fixtures.
        self.durations[test.id()] = self.durations.get(test.id(), 0.0) + (now - self._mark)
        self._mark = now

    def _note_fixture(self, kind, test):
        if not isinstance(test, unittest.TestCase):  # unittest.suite._ErrorHolder
            self.fixture_events.append((kind, test.id()))

    def addError(self, test, err):
        self._note_fixture("error", test)
        super().addError(test, err)

    def addSkip(self, test, reason):
        self._note_fixture("skip", test)
        super().addSkip(test, reason)


def _blocked_by_fixture(test, events: list) -> bool:
    """True when ``unittest`` recorded a setUpClass/setUpModule skip or error that kept ``test`` from starting."""
    cls = type(test)
    names = {f"setUpClass ({cls.__module__}.{cls.__qualname__})", f"setUpModule ({cls.__module__})"}
    return any(description in names for _, description in events)


def run_child(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    assigned = spec["ids"]
    wanted = set(assigned)
    started_at = time.perf_counter()
    tests = [t for t in discover(spec["start"], spec["pattern"], spec["top_level"]) if t.id() in wanted]
    found = {t.id() for t in tests}
    suite = unittest.TestSuite(tests)
    with open(spec["progress"], "w", encoding="utf-8") as progress:
        runner = unittest.TextTestRunner(
            stream=sys.stderr, verbosity=2,
            warnings=None if sys.warnoptions else "default",
            resultclass=lambda *a, **k: _ShardResult(*a, progress=progress, **k))
        result = runner.run(suite)
    started = set(result.started)
    fixture_blocked = sorted({t.id() for t in tests if t.id() not in started
                              and _blocked_by_fixture(t, result.fixture_events)})
    missing = sorted(wanted - found)
    not_started = sorted({t.id() for t in tests} - started - set(fixture_blocked))

    def described(pairs):
        return [{"id": t.id(), "description": result.getDescription(t), "text": text} for t, text in pairs]

    report = {
        "assigned": len(assigned),
        "occurrences": len(tests),
        "tests_run": result.testsRun,
        "started": len(result.started),
        "fixture_blocked": fixture_blocked,
        "missing": missing,
        "not_started": not_started,
        "failures": described(result.failures),
        "errors": described(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": [{"id": t.id(), "description": result.getDescription(t)}
                                 for t in result.unexpectedSuccesses],
        "durations": {k: round(v, 3) for k, v in result.durations.items()},
        "seconds": round(time.perf_counter() - started_at, 3),
    }
    out = Path(spec["result"])
    partial = out.with_suffix(".partial")
    partial.write_text(json.dumps(report), encoding="utf-8")
    os.replace(partial, out)
    ok = result.wasSuccessful() and not missing and not not_started
    return 0 if ok else 1


# --- the parent ------------------------------------------------------------------------------


def _shard_env(base: Path, index: int) -> dict:
    env = dict(os.environ)
    tmp, home = base / f"{index}t", base / f"{index}h"
    tmp.mkdir()
    home.mkdir()
    # Keep reading the same Git global config and Python user site as the parent: the scratch home
    # isolates writes, it should not change what a test sees.
    real_home = Path(os.environ.get("HOME") or Path.home())
    if "GIT_CONFIG_GLOBAL" not in env and (real_home / ".gitconfig").is_file():
        env["GIT_CONFIG_GLOBAL"] = str(real_home / ".gitconfig")
    if "PYTHONUSERBASE" not in env:
        import site
        env["PYTHONUSERBASE"] = site.getuserbase()
    env.update({"TMP": str(tmp), "TEMP": str(tmp), "TMPDIR": str(tmp), "HOME": str(home), "USERPROFILE": str(home)})
    return env


def _last_line(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return lines[-1] if lines else None


def _tail(path: Path, count: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(lines[-count:])


def _print_errors(flavour: str, items: list) -> None:
    for item in items:
        print(SEPARATOR1)
        print(f"{flavour}: {item['description']}")
        print(SEPARATOR2)
        print(item["text"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shards", type=int, default=None, help="number of shards (default min(os.cpu_count(), 8))")
    parser.add_argument("--start", "-s", default="tests", help="discovery start directory (default tests)")
    parser.add_argument("--pattern", "-p", default="test*.py", help="discovery pattern (default test*.py)")
    parser.add_argument("--top-level", "-t", default=None, help="top-level directory (default: the start directory)")
    parser.add_argument("--durations", default=None, help="timing manifest (default <start>/durations.json)")
    parser.add_argument("--log-dir", default=None, help="where the shard logs go (default: a new temporary directory)")
    parser.add_argument("--report", default=None, help="write a JSON report of the plan and every shard's result")
    parser.add_argument("--write-durations", default=None,
                        help="write the measured seconds per test id here (to refresh the manifest)")
    parser.add_argument("--child", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return run_child(args.child)
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except AttributeError:
        pass

    began = time.perf_counter()
    shards_wanted = args.shards or min(os.cpu_count() or 1, 8)
    if shards_wanted < 1:
        parser.error("--shards must be at least 1")
    tests = discover(args.start, args.pattern, args.top_level)
    discovered_ids = [t.id() for t in tests]
    unique_ids = list(dict.fromkeys(discovered_ids))
    if not tests:
        print(SEPARATOR2)
        print(f"Ran 0 tests in {time.perf_counter() - began:.3f}s")
        print()
        print("NO TESTS RAN")
        return 5
    manifest = Path(args.durations) if args.durations else Path(args.start) / "durations.json"
    durations = load_durations(manifest)
    units, stats = units_for(tests, durations)
    shards = pack(units, shards_wanted)
    problems = check_plan(shards, unique_ids)
    if problems:
        for problem in problems:
            print(f"run_shards: shard_incomplete: plan: {problem}")
        print(SEPARATOR2)
        print("Ran 0 tests in 0.000s")
        print()
        print(f"FAILED (shard_incomplete={len(problems)})")
        return 1
    multiplicity = {test_id: discovered_ids.count(test_id) for test_id in unique_ids} \
        if len(unique_ids) != len(discovered_ids) else None
    print(f"run_shards: {len(discovered_ids)} tests discovered in {args.start} ({args.pattern}); "
          f"{len(shards)} shards on {os.cpu_count()} CPUs; manifest {manifest}: {stats['timed']} timed, "
          f"{stats['unknown']} at the median {stats['median']:.2f} s, {stats['stale']} stale entries ignored")
    print("run_shards: planned seconds per shard: " + ", ".join(f"{s['seconds']:.0f}" for s in shards))

    work = Path(tempfile.mkdtemp(prefix="rs"))
    logs = Path(args.log_dir) if args.log_dir else work / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    procs = []
    try:
        for index, shard in enumerate(shards, 1):
            spec = {"ids": shard["ids"], "start": args.start, "pattern": args.pattern, "top_level": args.top_level,
                    "result": str((work / f"{index}.json").resolve()),
                    "progress": str((work / f"{index}.progress").resolve())}
            spec_path = work / f"{index}.spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            log_path = logs / f"shard-{index}.log"
            log = open(log_path, "wb")
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(spec_path.resolve())],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    env=_shard_env(work, index))
            procs.append({"index": index, "proc": proc, "log": log, "log_path": log_path, "spec": spec,
                          "planned": shard["seconds"], "launched": time.perf_counter()})
        pending = list(procs)
        while pending:
            for item in list(pending):
                code = item["proc"].poll()
                if code is None:
                    continue
                item["code"] = code
                item["wall"] = time.perf_counter() - item["launched"]
                item["log"].close()
                pending.remove(item)
                _collect(item)
                print(f"run_shards: shard {item['index']}/{len(procs)}: {item['summary']}", flush=True)
            if pending:
                time.sleep(0.1)
    finally:
        for item in procs:
            if item["proc"].poll() is None:
                item["proc"].kill()
                item["proc"].wait()
            if not item["log"].closed:
                item["log"].close()

    # Completeness: every shard accounted for every id it was given, and the totals add up.
    incomplete: list = []
    crashed = 0
    tests_run = skipped = expected = 0
    failures: list = []
    errors: list = []
    unexpected: list = []
    accounted = 0
    for item in procs:
        result = item.get("result")
        if result is None:
            crashed += 1
            print(f"run_shards: shard_crashed: shard {item['index']} exited with code {item['code']} and wrote no "
                  f"result; last test started: {item['last_started'] or '(none)'}; log {item['log_path']}")
            print(_tail(item["log_path"]))
            tests_run += item["progress_count"]
            incomplete.append(f"shard {item['index']} crashed: none of its {len(item['spec']['ids'])} assigned "
                              f"ids has a result ({item['progress_count']} started)")
            continue
        expected_here = sum(multiplicity[i] for i in item["spec"]["ids"]) if multiplicity else len(item["spec"]["ids"])
        if result["missing"]:
            incomplete.append(f"shard {item['index']} did not discover {len(result['missing'])} assigned ids: "
                              + ", ".join(result["missing"][:10]))
        if result["not_started"]:
            incomplete.append(f"shard {item['index']} never started {len(result['not_started'])} tests: "
                              + ", ".join(result["not_started"][:10]))
        if result["occurrences"] != expected_here:
            incomplete.append(f"shard {item['index']} found {result['occurrences']} tests for "
                              f"{expected_here} assigned")
        if item["code"] not in (0, 1) or (item["code"] == 0 and (result["failures"] or result["errors"])):
            incomplete.append(f"shard {item['index']} exited with code {item['code']}")
        blocked = len(result["fixture_blocked"])
        accounted += result["started"] + blocked
        tests_run += result["tests_run"]
        skipped += result["skipped"]
        expected += result["expected_failures"]
        failures += result["failures"]
        errors += result["errors"]
        unexpected += result["unexpected_successes"]
    if accounted != len(discovered_ids):
        incomplete.append(f"{accounted} tests accounted for, {len(discovered_ids)} discovered")
    for line in incomplete:
        print(f"run_shards: shard_incomplete: {line}")

    _print_errors("ERROR", errors)
    _print_errors("FAIL", failures)
    if unexpected:
        print(SEPARATOR1)
        print("Unexpected successes:")
        for item in unexpected:
            print(f"  {item['description']}")
    if crashed or incomplete or failures or errors:
        print(f"run_shards: logs in {logs}")

    wall = time.perf_counter() - began
    if args.report:
        _write_report(Path(args.report), procs, discovered_ids, wall, stats, crashed, incomplete)
    if args.write_durations:
        measured: dict = {}
        for item in procs:
            measured.update((item.get("result") or {}).get("durations", {}))
        Path(args.write_durations).write_text(
            json.dumps({k: round(v, 2) for k, v in sorted(measured.items())}, indent=1) + "\n", encoding="utf-8",
            newline="\n")
    for item in procs:  # scratch homes and temp dirs go; the logs stay
        for suffix in ("t", "h"):
            shutil.rmtree(work / f"{item['index']}{suffix}", ignore_errors=True)

    print(SEPARATOR2)
    print(f"Ran {tests_run} test{'' if tests_run == 1 else 's'} in {wall:.3f}s")
    print()
    infos = []
    if failures:
        infos.append(f"failures={len(failures)}")
    if errors:
        infos.append(f"errors={len(errors)}")
    if skipped:
        infos.append(f"skipped={skipped}")
    if expected:
        infos.append(f"expected failures={expected}")
    if unexpected:
        infos.append(f"unexpected successes={len(unexpected)}")
    if crashed:
        infos.append(f"shard_crashed={crashed}")
    if incomplete:
        infos.append(f"shard_incomplete={len(incomplete)}")
    ok = not (failures or errors or unexpected or crashed or incomplete)
    print(("OK" if ok else "FAILED") + (f" ({', '.join(infos)})" if infos else ""))
    return 0 if ok else 1


def _collect(item: dict) -> None:
    result_path = Path(item["spec"]["result"])
    progress = Path(item["spec"]["progress"])
    item["last_started"] = _last_line(progress)
    try:
        item["progress_count"] = len(progress.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        item["progress_count"] = 0
    try:
        item["result"] = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        item["result"] = None
    result = item["result"]
    if result is None:
        item["summary"] = f"CRASHED (exit {item['code']}) after {item['wall']:.1f} s"
        return
    bad = len(result["failures"]) + len(result["errors"]) + len(result["unexpected_successes"])
    state = "OK" if item["code"] == 0 and not bad else "FAILED"
    item["summary"] = (f"{result['tests_run']} tests, planned {item['planned']:.0f} s, took {item['wall']:.1f} s, "
                       f"{state}")


def _write_report(path: Path, procs: list, discovered: list, wall: float, stats: dict, crashed: int,
                  incomplete: list) -> None:
    shards = []
    for item in procs:
        result = item.get("result") or {}
        shards.append({
            "index": item["index"], "ids": item["spec"]["ids"], "planned_seconds": round(item["planned"], 3),
            "wall_seconds": round(item["wall"], 3), "exit_code": item["code"], "crashed": item.get("result") is None,
            "tests_run": result.get("tests_run"), "skipped": result.get("skipped"),
            "failures": [f["id"] for f in result.get("failures", [])],
            "errors": [e["id"] for e in result.get("errors", [])],
            "fixture_blocked": result.get("fixture_blocked", []), "log": str(item["log_path"]),
        })
    path.write_text(json.dumps({
        "discovered": len(discovered), "cpu_count": os.cpu_count(), "wall_seconds": round(wall, 3),
        "manifest": stats, "shards": shards, "crashed": crashed, "incomplete": incomplete,
    }, indent=1) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    # ``python -m unittest`` puts the working directory first on sys.path; do the same, in place of
    # this script's own directory, so tests import exactly as they do under ``unittest discover``.
    sys.path[0] = os.getcwd()
    sys.exit(main())
