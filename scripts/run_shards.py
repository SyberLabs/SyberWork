"""Run the unit suite as parallel shards, and prove that every discovered test ran.

    python scripts/run_shards.py                        # N = min(os.cpu_count(), 8)
    python scripts/run_shards.py --shards 4 --report shards.json
    python scripts/run_shards.py --floor <base commit>  # also refuse a test set that shrank (the host check)
    python scripts/run_shards.py --shard-timeout 600    # stop a shard after 600 s (default: from the manifest)

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

This process never imports a test module. Discovery runs in a child too, so test code
that exits or crashes at import cannot end the judge. Every child reports to this
process over a private pipe it inherits (never its stdout, which is the log), opened
before any test module is imported; each message carries a random per-child nonce,
handed over on the child's stdin, and a sequence number. A shard streams ``start``,
``outcome`` and ``stop`` for every test as it happens, then a ``done`` summary; the
verdict is computed here from the stream, and the summary must agree with it.

Completeness, checked and never assumed:

* before running, the union of the shards' ids equals the discovered set and no id is
  in two shards, and no discovered test takes the ``unittest`` machinery that starts
  and reports it (``run``, ``__call__``, ``debug``, ``id``, ...) from outside ``unittest``;
* after running, every shard's stream is intact and finished, it started every id it
  was assigned (or names the class or module fixture that skipped or failed before it
  could start, as ``unittest`` itself does), and the totals add up;
* with ``--floor BASE`` (or ``SYBERLABS_BASE`` in the environment, which the Build
  Thread host sets for its checks): the working tree is the committed tree, and every
  test discovered on BASE is discovered here too, unless the diff BASE..HEAD deletes or
  renames its file, removes its ``def``, or renames its class (``test_set_shrank``).

A shard that runs past its time budget (``--shard-timeout``, or by default three times
its planned seconds and at least five minutes) is stopped with every process it started,
and the run fails with ``shard_timeout``, naming the last test the shard started.
Discovery has the same budget (the given one, or five minutes).

Anything else fails the run with a named reason, as does any failure, error, or
unexpected success. The last lines have the shape ``unittest`` prints: ``Ran N tests
in Xs``, then ``OK (...)`` or ``FAILED (...)``. Standard library only.

Test code still runs in the shard's own process, so it can read the nonce and write a
false stream; ``docs/TESTING.md`` says what that leaves open and which critic rule is
the second layer. Only a sandbox closes it.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import secrets
import shutil
import signal
import site
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

if os.name == "nt":
    import msvcrt

SEPARATOR1 = "=" * 70
SEPARATOR2 = "-" * 70
DEFAULT_SECONDS = 1.0  # used only when the manifest is missing or empty
WORK_PREFIX = "syberlabs-shards-"  # the logs; the shards' scratch homes live in a separate directory
SCRATCH_PREFIX = "syberlabs-shard-scratch-"
BASE_ENV = "SYBERLABS_BASE"
# The default time budget of a shard: this many times its planned seconds, and never less than the floor.
# The floor is also the default budget of a discovery child. On the CI runners of 2026-10-26 the slowest
# shard took 1.9 times its plan (Windows, 533 s against 286 s planned).
SHARD_TIMEOUT_FACTOR = 3.0
SHARD_TIMEOUT_FLOOR = 300.0
MAX_CHANNEL_BYTES = 64 * 1024 * 1024
# The TestCase methods that start a test and report it. A discovered test whose class (or instance)
# takes one of these from outside the ``unittest`` package is refused: it could report a test that
# never ran.
HARNESS_METHODS = ("run", "__call__", "debug", "id", "_callTestMethod", "_callSetUp", "_callTearDown",
                   "_callCleanup")
# What a shard's stream may say about a test, and which ``unittest`` count it adds to.
OUTCOMES = {"success": None, "failure": "failures", "error": "errors", "skip": "skipped",
            "xfail": "expected_failures", "xpass": "unexpected", "subfailure": "failures", "suberror": "errors"}
# The functions that start, run and report a test, by class. A shard records them before it imports any
# test module and reports a test that starts after one of them changed (``harness_changed``).
RESULT_METHODS = ("startTest", "stopTest", "addSuccess", "addError", "addFailure", "addSkip", "addSubTest",
                  "addExpectedFailure", "addUnexpectedSuccess", "wasSuccessful", "stop")
SUITE_METHODS = ("run", "__call__", "__iter__", "_handleClassSetUp", "_handleModuleFixture", "_tearDownPreviousClass",
                 "_handleModuleTearDown")
# Files Python could import: one of these, untracked, anywhere in the worktree, is a change a test run sees.
IMPORTABLE = (".py", ".pyc", ".pyo", ".pyd", ".pth", ".so", ".dll")


# --- discovery (in a child) ------------------------------------------------------------------


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


def _unittest_module(name) -> bool:
    return isinstance(name, str) and (name == "unittest" or name.startswith("unittest."))


def _overrides(test) -> list:
    """The harness methods ``test`` takes from outside the ``unittest`` package (its class or the instance)."""
    found = []
    for name in HARNESS_METHODS:
        if name in vars(test):
            found.append(name)
            continue
        owner = next((klass for klass in type(test).__mro__ if name in vars(klass)), None)
        if owner is not None and not _unittest_module(owner.__module__):
            found.append(name)
    return found


def _relative(path, root: Path) -> str | None:
    """``path`` relative to ``root`` with forward slashes, or None when it is outside ``root``."""
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def describe(test, root: Path) -> dict:
    """What the parent needs to know about one discovered test, so that it never imports one itself."""
    cls = type(test)
    module = sys.modules.get(cls.__module__)
    method = getattr(test, "_testMethodName", None) if isinstance(test, unittest.TestCase) else None
    func = getattr(cls, method, None) if method else None
    while hasattr(func, "__wrapped__"):  # skip, expectedFailure and friends keep the original
        func = func.__wrapped__
    code = getattr(func, "__code__", None)
    return {"id": test.id(), "canonical": f"{cls.__module__}.{cls.__qualname__}.{method}" if method else None,
            "case": isinstance(test, unittest.TestCase), "module": cls.__module__, "cls": cls.__qualname__,
            "method": method, "fixture": _fixture_key(test),
            "file": _relative(getattr(module, "__file__", None), root),
            "method_file": _relative(code.co_filename if code else None, root),
            "overrides": _overrides(test)}


# --- packing ---------------------------------------------------------------------------------


def load_durations(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object of test id -> seconds")
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def units_for(records: list, durations: dict) -> tuple[list, dict]:
    """Group discovered tests (``describe`` records) into packing units: ``[(ids, seconds)]`` in first-discovery order."""
    median = statistics.median(durations.values()) if durations else DEFAULT_SECONDS
    order: list = []
    groups: dict = {}
    timed = unknown = 0
    seen: set = set()
    for record in records:
        test_id = record["id"]
        key = record["fixture"] or "test:" + test_id
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
        if ids:  # a group whose ids all went to an earlier group (an id collision) packs nothing
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


# --- stopping a child and everything it started ---------------------------------------------


def _descendants(pid: int) -> list:
    """POSIX: the processes below ``pid``, from ``ps`` (empty when ``ps`` cannot be run)."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict = collections.defaultdict(list)
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            children[int(fields[1])].append(int(fields[0]))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), ()):
            found.append(child)
            todo.append(child)
    return found


def stop_tree(proc: subprocess.Popen) -> None:
    """End ``proc`` and every process it started.

    Windows: ``taskkill /T /F``, as ``syberlabs.checks`` does. POSIX: each process of the tree is
    stopped with SIGSTOP, so none can start another, until a listing finds nothing new; then every
    one gets SIGKILL. The shards stay in this runner's session, so a host that ends this
    runner's session still ends them. A process that left the tree before the listing (one that
    detached itself) is not found here; the host's session kill is what ends it.
    """
    if proc.poll() is None:
        try:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
                subprocess.run([str(taskkill) if taskkill.exists() else "taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=60, check=False)
            else:
                frozen: list = []
                for _ in range(10):
                    fresh = [pid for pid in [proc.pid, *_descendants(proc.pid)] if pid not in frozen]
                    if not fresh:
                        break
                    for pid in fresh:
                        try:
                            os.kill(pid, signal.SIGSTOP)
                        except OSError:
                            pass
                    frozen += fresh
                for pid in frozen:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            proc.kill()
    proc.wait()


# --- the channel: a private pipe from each child to this process -----------------------------


class _Channel:
    """The child's end: one JSON line per message, each with the nonce and the next sequence number."""

    def __init__(self, handle, nonce: str):
        if os.name == "nt":
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            os.set_handle_inheritable(handle, False)  # a process a test starts does not get it
        else:
            fd = handle
            os.set_inheritable(fd, False)
        self._out = os.fdopen(fd, "wb", buffering=0)
        self._nonce = nonce
        self._seq = 0

    def send(self, event: str, **fields) -> None:
        line = json.dumps({"n": self._nonce, "seq": self._seq, "ev": event, **fields}, separators=(",", ":"))
        self._seq += 1
        self._out.write(line.encode("utf-8") + b"\n")


class _Child:
    """The parent's end: a child process, its log, and the lines its channel carried."""

    def __init__(self, spec: dict, cwd, env: dict, log_path: Path):
        self.nonce = secrets.token_hex(16)
        self.log_path = log_path
        self.lines: list = []
        self.overflow = False
        self.timed_out = False
        read, write = os.pipe()
        kwargs: dict = {}
        if os.name == "nt":
            handle = msvcrt.get_osfhandle(write)
            os.set_handle_inheritable(handle, True)
            info = subprocess.STARTUPINFO()
            info.lpAttributeList = {"handle_list": [handle]}  # only this pipe is inherited, beside the std handles
            kwargs["startupinfo"] = info
            channel = handle
        else:
            kwargs["pass_fds"] = (write,)
            channel = write
        self.log = open(log_path, "wb")
        try:
            self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child"], cwd=cwd, env=env,
                                         stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT,
                                         close_fds=True, **kwargs)
        except BaseException:
            os.close(read)
            self.log.close()
            raise
        finally:
            os.close(write)
        self._reader = threading.Thread(target=self._read, args=(read,), daemon=True)
        self._reader.start()
        try:
            self.proc.stdin.write(json.dumps({**spec, "channel": channel, "nonce": self.nonce}).encode() + b"\n")
            self.proc.stdin.close()
        except OSError:  # the child is already gone; its missing stream says so
            pass

    def _read(self, read: int) -> None:
        total = 0
        with os.fdopen(read, "rb") as stream:
            for line in stream:
                total += len(line)
                if total > MAX_CHANNEL_BYTES:
                    self.overflow = True
                    continue
                self.lines.append(line)

    def finish(self, timeout: float | None = None) -> int:
        """Wait for the child; past ``timeout`` seconds, stop it and everything it started (``timed_out``)."""
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            stop_tree(self.proc)
            code = self.proc.returncode
        self._reader.join(timeout=30)
        if not self.log.closed:
            self.log.close()
        return code

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if not self.log.closed:
            self.log.close()

    def messages(self) -> tuple[list, str | None]:
        """The messages, in order, or the first reason the stream cannot be trusted."""
        if self._reader.is_alive():
            return [], "the channel stayed open after the child exited"
        if self.overflow:
            return [], f"the channel carried more than {MAX_CHANNEL_BYTES} bytes"
        messages = []
        for seq, raw in enumerate(self.lines):
            try:
                message = json.loads(raw)
            except ValueError:
                return messages, f"message {seq + 1} is not JSON"
            if not isinstance(message, dict) or message.get("n") != self.nonce:
                return messages, f"message {seq + 1} does not carry this child's nonce"
            if message.get("seq") != seq:
                return messages, f"message {seq + 1} is out of sequence"
            messages.append(message)
        return messages, None


# --- one child -------------------------------------------------------------------------------


def _watched() -> dict:
    return {unittest.TestCase: HARNESS_METHODS, unittest.TestSuite: SUITE_METHODS,
            unittest.suite.BaseTestSuite: SUITE_METHODS, unittest.TestResult: RESULT_METHODS,
            unittest.TextTestResult: RESULT_METHODS, _ShardResult: RESULT_METHODS + ("_send", "_report"),
            _Channel: ("send",), unittest.case._Outcome: ("testPartExecutor",)}


def _harness_snapshot() -> dict:
    """The harness functions by identity, as the classes hold them now."""
    return {(cls.__qualname__, name): vars(cls).get(name) for cls, names in _watched().items() for name in names}


class _ShardResult(unittest.TextTestResult):
    def __init__(self, *args, channel=None, pristine=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel = channel
        self.pristine = pristine
        self.changed = False
        self.durations: dict = {}
        self._mark = time.perf_counter()

    def _check_harness(self, test):
        """Report, once, a test that starts after test code changed how tests are run or reported."""
        if self.pristine is None or self.changed:
            return
        now = _harness_snapshot()
        what = sorted(f"{cls}.{name}" for (cls, name), value in now.items() if self.pristine.get((cls, name)) is not value)
        what += sorted(f"result.{name}" for name in RESULT_METHODS if name in vars(self))
        what += [f"{type(test).__qualname__}.{name}" for name in _overrides(test)]
        if what:
            self.changed = True
            self._send("harness_changed", id=test.id(), what=what)

    def _send(self, event, **fields):
        if self.channel is not None:
            self.channel.send(event, **fields)

    def _report(self, kind, test):
        case = getattr(test, "test_case", test)  # a skipped subtest is reported against its test
        if isinstance(case, unittest.TestCase):
            self._send("outcome", id=case.id(), kind=kind)
        else:  # unittest.suite._ErrorHolder: a setUpClass/setUpModule (or teardown) that skipped or failed
            self._send("fixture", kind=kind, description=test.id())

    def startTest(self, test):
        self._send("start", id=test.id())
        self._check_harness(test)
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        now = time.perf_counter()
        # Time since the previous test ended: includes this test's class and module fixtures.
        self.durations[test.id()] = self.durations.get(test.id(), 0.0) + (now - self._mark)
        self._mark = now
        self._send("stop", id=test.id())

    def addSuccess(self, test):
        self._report("success", test)
        super().addSuccess(test)

    def addFailure(self, test, err):
        self._report("failure", test)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._report("error", test)
        super().addError(test, err)

    def addSkip(self, test, reason):
        self._report("skip", test)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._report("xfail", test)
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._report("xpass", test)
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self._send("outcome", id=test.id(), kind="subfailure" if failed else "suberror")
        super().addSubTest(test, subtest, err)


def run_child() -> int:
    """A child: read the spec from stdin, open the channel, then (and only then) import the tests."""
    spec = json.loads(sys.stdin.readline())
    channel = _Channel(spec["channel"], spec["nonce"])
    devnull = os.open(os.devnull, os.O_RDONLY)  # tests read nothing, as before
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdin = open(os.devnull, encoding="utf-8")
    if spec["mode"] == "discover":
        root = Path(spec["root"]).resolve()
        tests = discover(spec["start"], spec["pattern"], spec["top_level"])
        for test in tests:
            channel.send("test", **describe(test, root))
        channel.send("done", count=len(tests))
        return 0

    wanted = set(spec["ids"])
    pristine = _harness_snapshot()  # before any test module is imported
    tests = [t for t in discover(spec["start"], spec["pattern"], spec["top_level"]) if t.id() in wanted]
    runner = unittest.TextTestRunner(
        stream=sys.stderr, verbosity=2, warnings=None if sys.warnoptions else "default",
        resultclass=lambda *a, **k: _ShardResult(*a, channel=channel, pristine=pristine, **k))
    result = runner.run(unittest.TestSuite(tests))

    def described(pairs):
        return [{"id": t.id(), "description": result.getDescription(t), "text": text} for t, text in pairs]

    channel.send("done", tests_run=result.testsRun, failures=described(result.failures),
                 errors=described(result.errors), skipped=len(result.skipped),
                 expected_failures=len(result.expectedFailures),
                 unexpected=[{"id": t.id(), "description": result.getDescription(t)} for t in result.unexpectedSuccesses],
                 durations={k: round(v, 3) for k, v in result.durations.items()})
    return 0 if result.wasSuccessful() and {t.id() for t in tests} == wanted else 1


# --- reading a shard's stream ----------------------------------------------------------------


def _blocked(record: dict, fixtures: list) -> bool:
    """True when ``unittest`` recorded a setUpClass/setUpModule skip or error that kept this test from starting."""
    names = {f"setUpClass ({record['module']}.{record['cls']})", f"setUpModule ({record['module']})"}
    return any(description in names for _, description in fixtures)


def account(messages: list, assigned: list, multiplicity: dict, records: dict) -> dict:
    """A shard's result, computed from its stream alone; ``problems`` lists every way it is incomplete."""
    found = {"tests_run": 0, "failures": 0, "errors": 0, "skipped": 0, "expected_failures": 0, "unexpected": 0}
    starts: collections.Counter = collections.Counter()
    fixtures: list = []
    problems: list = []
    open_id = last_started = done = None
    for message in messages:
        event, test_id = message.get("ev"), message.get("id")
        if done is not None:
            problems.append(f"sent {event!r} after done")
            break
        if event == "start":
            if open_id is not None:
                problems.append(f"started {test_id} while {open_id} was running")
            open_id = last_started = test_id
            starts[test_id] += 1
            found["tests_run"] += 1
        elif event == "outcome":
            if test_id != open_id or message.get("kind") not in OUTCOMES:
                problems.append(f"reported an outcome for {test_id} outside its start and stop")
                continue
            counter = OUTCOMES[message["kind"]]
            if counter:
                found[counter] += 1
        elif event == "fixture":
            kind = message.get("kind")
            if kind == "error":
                found["errors"] += 1
            elif kind == "skip":
                found["skipped"] += 1
            else:
                problems.append(f"reported a fixture event of kind {kind!r}")
            fixtures.append((kind, message.get("description")))
        elif event == "stop":
            if test_id != open_id:
                problems.append(f"stopped {test_id} while {open_id} was running")
            open_id = None
        elif event == "harness_changed":
            problems.append(f"ran {test_id} after test code changed {', '.join(map(str, message.get('what') or []))}")
        elif event == "done":
            done = message
        else:
            problems.append(f"sent an unknown event {event!r}")
    crashed = done is None
    if not crashed:
        if open_id is not None:
            problems.append(f"never stopped {open_id}")
        for key in ("tests_run", "skipped", "expected_failures"):
            if done.get(key) != found[key]:
                problems.append(f"says {key} {done.get(key)} in its summary and {found[key]} in its stream")
        for key in ("failures", "errors", "unexpected"):
            if not isinstance(done.get(key), list) or len(done[key]) != found[key]:
                problems.append(f"lists {key} in its summary that its stream ({found[key]}) does not")
    strangers = sorted(set(starts) - set(assigned))
    if strangers:
        problems.append(f"started {len(strangers)} tests it was not assigned: " + ", ".join(strangers[:10]))
    blocked = sorted(i for i in assigned if starts[i] == 0 and _blocked(records[i], fixtures))
    if not crashed:
        not_started = [i for i in assigned if starts[i] == 0 and i not in blocked]
        if not_started:
            problems.append(f"never started {len(not_started)} tests: " + ", ".join(not_started[:10]))
        repeated = [i for i in assigned if starts[i] not in (0, multiplicity.get(i, 1))]
        if repeated:
            problems.append(f"started {len(repeated)} tests a number of times other than discovered: "
                            + ", ".join(repeated[:10]))
    return {"crashed": crashed, "done": done, "problems": problems, "found": found, "starts": starts,
            "fixture_blocked": blocked, "last_started": last_started}


# --- the floor: the host's own evidence that the test set did not shrink ---------------------


def _git(args: list, cwd, *, binary: bool = False):
    proc = subprocess.run(["git", "-c", "core.quotepath=off", "--no-optional-locks", *args], cwd=cwd,
                          stdin=subprocess.DEVNULL, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()[:200]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")


def _importable_under(folder: Path) -> str | None:
    """The first file under ``folder`` that Python could import, relative to it, or None."""
    for dirpath, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name.endswith(IMPORTABLE):
                return Path(dirpath, name).relative_to(folder).as_posix()
    return None


def worktree_changes(start: str, cwd=".") -> list:
    """What makes the working tree differ from HEAD in a way a test run could see.

    A tracked file modified or deleted anywhere; an untracked or ignored file under the start
    directory; an untracked or ignored file anywhere that Python could import, alone or inside an
    untracked or ignored directory. An earlier check that ran candidate code in the same worktree
    could have written any of them.
    """
    root = Path(_git(["rev-parse", "--show-toplevel"], cwd).strip()).resolve()
    start_rel = (_relative(Path(cwd, start), root) or "").rstrip("/")
    raw = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"], cwd, binary=True)
    changes = []
    entries = iter(raw.decode("utf-8", errors="replace").split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        state, path = entry[:2], entry[3:]
        if state[0] in "RC":
            next(entries, None)  # the source path of a rename or copy
        if state in ("??", "!!"):
            what = "untracked" if state == "??" else "ignored"
            inside = start_rel in ("", ".") or path == start_rel or path.startswith(start_rel + "/")
            if inside or path.endswith(IMPORTABLE):
                changes.append(f"{what} {path}")
            elif path.endswith("/"):  # a whole directory (``pip install -e`` leaves an ignored *.egg-info/)
                held = _importable_under(root / path)
                if held:
                    changes.append(f"{what} {path} (holds {held})")
        else:
            changes.append(f"modified {path}")
    return changes


def _extract(base: str, cwd, into: Path) -> None:
    """The whole tree of ``base``, from the object database (read-only), under ``into``."""
    archive = into.parent / "base.tar"
    with open(archive, "wb") as out:
        proc = subprocess.run(["git", "archive", "--format=tar", base], cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=out, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("git archive failed: " + proc.stderr.decode(errors="replace").strip()[:200])
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="data")
        else:  # Python 3.11 before 3.11.4
            tar.extractall(into)
    archive.unlink()


def diff_facts(base: str, cwd=".") -> tuple[set, dict]:
    """``(paths deleted or renamed away, {old path: [removed lines]})`` for BASE..HEAD, renames detected."""
    flags = ["--no-color", "--no-ext-diff", "--no-textconv", "-M"]
    gone: set = set()
    status = _git(["diff", *flags, "--name-status", "-z", base, "HEAD"], cwd).split("\0")
    index = 0
    while index < len(status):
        code = status[index]
        if not code:
            index += 1
            continue
        if code[0] in "RC":
            if code[0] == "R":
                gone.add(status[index + 1])
            index += 3
        else:
            if code[0] == "D":
                gone.add(status[index + 1])
            index += 2
    removed: dict = collections.defaultdict(list)
    current, header = None, False
    for line in _git(["diff", *flags, "-U0", base, "HEAD"], cwd).splitlines():
        if line.startswith("diff --git "):
            current, header = None, True
        elif line.startswith("@@"):
            header = False
        elif header and line.startswith("--- "):
            name = line[4:].rstrip("\t")
            current = name[2:] if name.startswith("a/") else None
        elif not header and line.startswith("-") and current is not None:
            removed[current].append(line[1:])
    return gone, removed


def floor(base_records: list, records: list, gone: set, removed: dict) -> list:
    """Every base test still discovered, or excused by the diff. Returns ``[(id, reason)]`` for the rest.

    A base test absent from the candidate is excused only when the diff BASE..HEAD
    (a) deletes or renames the file that defines its class or its test method;
    (b) removes a ``def <method>(`` line from either of those files; or
    (c) removes a ``class <Name>`` line for its class from its module's file, and the candidate has a
        test of the same module and method name that the base did not have (a class renamed in the
        diff; each new test excuses one missing test).
    Base tests whose class lives in ``unittest`` itself (a module that failed to import, or skipped
    at import, on the base) are not part of the floor.
    """
    have = {r["id"] for r in records}
    base_ids = {r["id"] for r in base_records}
    fresh: dict = collections.defaultdict(list)
    for r in records:
        if r["id"] not in base_ids and r["method"]:
            fresh[(r["module"], r["method"])].append(r["id"])
    shrank = []
    for r in base_records:
        if r["id"] in have or _unittest_module(r["module"]):
            continue
        files = {f for f in (r["file"], r["method_file"]) if f}
        if files & gone:
            continue
        if r["method"]:
            definition = re.compile(r"^\s*(?:async\s+)?def\s+" + re.escape(r["method"]) + r"\s*\(")
            if any(definition.match(line) for f in files for line in removed.get(f, ())):
                continue
            header = re.compile(r"^\s*class\s+" + re.escape(r["cls"].rsplit(".", 1)[-1]) + r"\b")
            if r["file"] and any(header.match(line) for line in removed.get(r["file"], ())) \
                    and fresh.get((r["module"], r["method"])):
                fresh[(r["module"], r["method"])].pop(0)
                continue
        shrank.append((r["id"], "the diff deletes neither its file, its def nor its class"))
    return shrank


# --- the parent ------------------------------------------------------------------------------


def _scratch_env(scratch: Path, tmp_name: str, home_name: str) -> dict:
    env = dict(os.environ)
    tmp, home = scratch / tmp_name, scratch / home_name
    tmp.mkdir()
    home.mkdir()
    # Keep reading the same Git global config and Python user site as the parent: the scratch home
    # isolates writes, it should not change what a test sees.
    real_home = Path(os.environ.get("HOME") or Path.home())
    if "GIT_CONFIG_GLOBAL" not in env and (real_home / ".gitconfig").is_file():
        env["GIT_CONFIG_GLOBAL"] = str(real_home / ".gitconfig")
    if "PYTHONUSERBASE" not in env:
        env["PYTHONUSERBASE"] = site.getuserbase()
    env.update({"TMP": str(tmp), "TEMP": str(tmp), "TMPDIR": str(tmp), "HOME": str(home), "USERPROFILE": str(home)})
    return env


def _tail(path: Path, count: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(lines[-count:])


def _print_errors(flavour: str, items: list) -> None:
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("description"), str) and isinstance(item.get("text"), str):
            print(SEPARATOR1)
            print(f"{flavour}: {item['description']}")
            print(SEPARATOR2)
            print(item["text"])


def _discover_in_child(args, cwd, root: Path, scratch: Path, name: str, log: Path) -> tuple[list | None, str | None]:
    """The ``describe`` records of every test discovered in ``cwd``, from a child, or why there are none."""
    child = _Child({"mode": "discover", "start": args.start, "pattern": args.pattern, "top_level": args.top_level,
                    "root": str(root)}, cwd, _scratch_env(scratch, f"{name}-tmp", f"{name}-home"), log)
    budget = args.shard_timeout or SHARD_TIMEOUT_FLOOR
    try:
        code = child.finish(timeout=budget)
    finally:
        child.kill()
    if child.timed_out:
        return None, (f"discovery ran past its budget of {budget:.0f} s and was stopped with its process tree; "
                      f"log {log}:\n{_tail(log)}")
    messages, broken = child.messages()
    if broken:
        return None, f"{broken} (log {log})"
    if not messages or messages[-1].get("ev") != "done":
        return None, f"discovery exited with code {code} before it finished; log {log}:\n{_tail(log)}"
    records = [{k: v for k, v in m.items() if k not in ("n", "seq", "ev")} for m in messages[:-1]
               if m.get("ev") == "test"]
    if code != 0 or len(records) != messages[-1].get("count") or len(records) != len(messages) - 1:
        return None, f"discovery's stream does not add up (exit code {code}); log {log}"
    return records, None


def _refuse(problems: list, began: float) -> int:
    for code, text in problems:
        print(f"run_shards: {code}: {text}")
    counts = collections.Counter(code for code, _ in problems)
    print(SEPARATOR2)
    print(f"Ran 0 tests in {time.perf_counter() - began:.3f}s")
    print()
    print("FAILED (" + ", ".join(f"{code}={n}" for code, n in counts.items()) + ")")
    return 1


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
    parser.add_argument("--floor", default=os.environ.get(BASE_ENV) or None, metavar="BASE",
                        help=f"refuse a test set that shrank against this commit (default ${BASE_ENV})")
    parser.add_argument("--shard-timeout", type=float, default=None, metavar="SECONDS",
                        help=f"stop a shard (or discovery) that runs longer, with every process it started; default "
                             f"{SHARD_TIMEOUT_FACTOR:g} times the shard's planned seconds, at least "
                             f"{SHARD_TIMEOUT_FLOOR:.0f} (discovery: {SHARD_TIMEOUT_FLOOR:.0f})")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return run_child()
    if args.shard_timeout is not None and not args.shard_timeout > 0:
        parser.error("--shard-timeout must be a positive number of seconds")
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except AttributeError:
        pass

    began = time.perf_counter()
    shards_wanted = args.shards or min(os.cpu_count() or 1, 8)
    if shards_wanted < 1:
        parser.error("--shards must be at least 1")
    logs = Path(args.log_dir) if args.log_dir else Path(tempfile.mkdtemp(prefix=WORK_PREFIX)) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    try:
        return _run(args, shards_wanted, began, logs, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)  # scratch homes, temp dirs and the base tree go; the logs stay


def _integrity(records: list) -> list:
    """Discovered tests that could report a run that did not happen."""
    refusals = []
    for record in records:
        if record["overrides"]:
            refusals.append(("harness_override", f"{record['id']} takes {', '.join(record['overrides'])} "
                                                 "from outside unittest"))
        elif not record["case"]:
            refusals.append(("harness_override", f"{record['id']} is not a unittest.TestCase"))
        elif record["id"] != record["canonical"]:
            refusals.append(("harness_override", f"{record['id']} reports an id other than its own "
                                                 f"({record['canonical']})"))
    return refusals


def _run(args, shards_wanted: int, began: float, logs: Path, scratch: Path) -> int:
    refusals: list = []
    base_records = None
    root = Path.cwd().resolve()
    if args.floor:
        try:
            base = _git(["rev-parse", "--verify", "--quiet", args.floor + "^{commit}"], ".").strip()
            root = Path(_git(["rev-parse", "--show-toplevel"], ".").strip()).resolve()
            refusals += [("worktree_changed", change) for change in worktree_changes(args.start)]
            prefix = _git(["rev-parse", "--show-prefix"], ".").strip()
            base_root = scratch / "base"
            _extract(base, ".", base_root)
            gone, removed = diff_facts(base, ".")
        except (RuntimeError, OSError, tarfile.TarError) as exc:
            return _refuse([("floor_unavailable", f"base {args.floor}: {exc}")], began)
        base_records, broken = _discover_in_child(args, base_root / prefix, base_root.resolve(), scratch, "base",
                                                  logs / "discover-base.log")
        if broken:
            return _refuse([("floor_unavailable", f"discovery on the base {base[:10]} failed: {broken}")], began)
    records, broken = _discover_in_child(args, ".", root, scratch, "discovery", logs / "discover.log")
    if broken:
        return _refuse(refusals + [("discovery_failed", broken)], began)
    refusals += _integrity(records)
    if base_records is not None:
        refusals += [("test_set_shrank", f"{test_id}: {why}") for test_id, why in
                     floor(base_records, records, gone, removed)]
    if refusals:
        return _refuse(refusals, began)

    discovered_ids = [r["id"] for r in records]
    unique_ids = list(dict.fromkeys(discovered_ids))
    by_id = {r["id"]: r for r in records}
    if not records:
        print(SEPARATOR2)
        print(f"Ran 0 tests in {time.perf_counter() - began:.3f}s")
        print()
        print("NO TESTS RAN")
        return 5
    manifest = Path(args.durations) if args.durations else Path(args.start) / "durations.json"
    durations = load_durations(manifest)
    units, stats = units_for(records, durations)
    shards = pack(units, shards_wanted)
    problems = check_plan(shards, unique_ids)
    if problems:
        return _refuse([("shard_incomplete", f"plan: {problem}") for problem in problems], began)
    multiplicity = collections.Counter(discovered_ids)
    floor_note = (f"; floor: all {len(base_records)} tests discovered on the base are here or excused by the diff"
                  if base_records is not None else "")
    print(f"run_shards: {len(discovered_ids)} tests discovered in {args.start} ({args.pattern}); "
          f"{len(shards)} shards on {os.cpu_count()} CPUs; manifest {manifest}: {stats['timed']} timed, "
          f"{stats['unknown']} at the median {stats['median']:.2f} s, {stats['stale']} stale entries ignored"
          + floor_note)
    budgets = [args.shard_timeout or max(SHARD_TIMEOUT_FLOOR, SHARD_TIMEOUT_FACTOR * s["seconds"]) for s in shards]
    print("run_shards: planned seconds per shard: " + ", ".join(f"{s['seconds']:.0f}" for s in shards)
          + "; time budget per shard: " + ", ".join(f"{b:.0f}" for b in budgets)
          + ("" if args.shard_timeout else f" ({SHARD_TIMEOUT_FACTOR:g}x planned, at least {SHARD_TIMEOUT_FLOOR:.0f})"))

    procs = []
    try:
        for index, shard in enumerate(shards, 1):
            spec = {"mode": "run", "ids": shard["ids"], "start": args.start, "pattern": args.pattern,
                    "top_level": args.top_level}
            child = _Child(spec, ".", _scratch_env(scratch, f"{index}t", f"{index}h"), logs / f"shard-{index}.log")
            procs.append({"index": index, "child": child, "ids": shard["ids"], "planned": shard["seconds"],
                          "budget": budgets[index - 1], "launched": time.perf_counter()})
        pending = list(procs)
        while pending:
            for item in list(pending):
                if item["child"].proc.poll() is None:
                    if time.perf_counter() - item["launched"] <= item["budget"]:
                        continue
                    item["child"].finish(timeout=0)  # past its budget: stop it and everything it started
                item["code"] = item["child"].finish()
                item["wall"] = time.perf_counter() - item["launched"]
                pending.remove(item)
                _collect(item, multiplicity, by_id)
                print(f"run_shards: shard {item['index']}/{len(procs)}: {item['summary']}", flush=True)
            if pending:
                time.sleep(0.1)
    finally:
        for item in procs:
            item["child"].kill()

    # Completeness: every shard accounted for every id it was given, and the totals add up.
    incomplete: list = []
    tampered: list = []
    crashed = timed_out = 0
    tests_run = skipped = expected = 0
    failures: list = []
    errors: list = []
    unexpected: list = []
    accounted = 0
    for item in procs:
        result = item["result"]
        log_path = item["child"].log_path
        if item["broken"]:
            tampered.append(f"shard {item['index']}: {item['broken']}; none of its {len(item['ids'])} assigned ids "
                            f"counts (exit code {item['code']}, log {log_path})")
            continue
        if item["child"].timed_out:
            timed_out += 1
            print(f"run_shards: shard_timeout: shard {item['index']} ran past its budget of {item['budget']:.0f} s "
                  f"and was stopped with its process tree; last test started: {result['last_started'] or '(none)'}; "
                  f"log {log_path}")
            print(_tail(log_path))
            tests_run += result["found"]["tests_run"]
            continue
        if result["crashed"]:
            crashed += 1
            print(f"run_shards: shard_crashed: shard {item['index']} exited with code {item['code']} and wrote no "
                  f"result; last test started: {result['last_started'] or '(none)'}; log {log_path}")
            print(_tail(log_path))
            tests_run += result["found"]["tests_run"]
            incomplete.append(f"shard {item['index']} crashed: none of its {len(item['ids'])} assigned "
                              f"ids has a result ({result['found']['tests_run']} started)")
            continue
        found, done = result["found"], result["done"]
        incomplete += [f"shard {item['index']} {problem}" for problem in result["problems"]]
        bad = bool(found["failures"] or found["errors"] or found["unexpected"] or result["problems"])
        if item["code"] not in (0, 1) or (item["code"] == 0) == bad:
            incomplete.append(f"shard {item['index']} exited with code {item['code']}")
        accounted += sum(result["starts"][i] for i in item["ids"]) + len(result["fixture_blocked"])
        tests_run += found["tests_run"]
        skipped += found["skipped"]
        expected += found["expected_failures"]
        failures += done["failures"] if isinstance(done.get("failures"), list) else []
        errors += done["errors"] if isinstance(done.get("errors"), list) else []
        unexpected += done["unexpected"] if isinstance(done.get("unexpected"), list) else []
    if accounted != len(discovered_ids) and not (crashed or tampered or timed_out):
        incomplete.append(f"{accounted} tests accounted for, {len(discovered_ids)} discovered")
    for line in tampered:
        print(f"run_shards: channel_tampered: {line}")
    for line in incomplete:
        print(f"run_shards: shard_incomplete: {line}")

    _print_errors("ERROR", errors)
    _print_errors("FAIL", failures)
    if unexpected:
        print(SEPARATOR1)
        print("Unexpected successes:")
        for item in unexpected:
            print(f"  {item.get('description') if isinstance(item, dict) else item}")
    if crashed or timed_out or incomplete or tampered or failures or errors:
        print(f"run_shards: logs in {logs}")

    wall = time.perf_counter() - began
    if args.report:
        _write_report(Path(args.report), procs, discovered_ids, wall, stats, crashed, incomplete + tampered,
                      base_records)
    if args.write_durations:
        measured: dict = {}
        for item in procs:
            seen = (item["result"]["done"] or {}).get("durations", {})
            if isinstance(seen, dict):
                measured.update(seen)
        Path(args.write_durations).write_text(
            json.dumps({k: round(v, 2) for k, v in sorted(measured.items())}, indent=1) + "\n", encoding="utf-8",
            newline="\n")

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
    if timed_out:
        infos.append(f"shard_timeout={timed_out}")
    if tampered:
        infos.append(f"channel_tampered={len(tampered)}")
    if incomplete:
        infos.append(f"shard_incomplete={len(incomplete)}")
    ok = not (failures or errors or unexpected or crashed or timed_out or incomplete or tampered)
    print(("OK" if ok else "FAILED") + (f" ({', '.join(infos)})" if infos else ""))
    return 0 if ok else 1


def _collect(item: dict, multiplicity: dict, by_id: dict) -> None:
    messages, item["broken"] = item["child"].messages()
    if item["broken"]:
        messages = []  # a stream that cannot be trusted counts for nothing
    item["result"] = result = account(messages, item["ids"], multiplicity, by_id)
    if item["broken"]:
        item["summary"] = f"CHANNEL TAMPERED (exit {item['code']}) after {item['wall']:.1f} s"
        return
    if item["child"].timed_out:
        item["summary"] = f"TIMED OUT after {item['wall']:.1f} s (budget {item['budget']:.0f} s), stopped"
        return
    if result["crashed"]:
        item["summary"] = f"CRASHED (exit {item['code']}) after {item['wall']:.1f} s"
        return
    found = result["found"]
    bad = found["failures"] + found["errors"] + found["unexpected"] + len(result["problems"])
    state = "OK" if item["code"] == 0 and not bad else "FAILED"
    item["summary"] = (f"{found['tests_run']} tests, planned {item['planned']:.0f} s, took {item['wall']:.1f} s, "
                       f"{state}")


def _write_report(path: Path, procs: list, discovered: list, wall: float, stats: dict, crashed: int,
                  incomplete: list, base_records) -> None:
    shards = []
    for item in procs:
        result = item["result"]
        done = result["done"] or {}
        shards.append({
            "index": item["index"], "ids": item["ids"], "planned_seconds": round(item["planned"], 3),
            "wall_seconds": round(item["wall"], 3), "budget_seconds": round(item["budget"], 3),
            "timed_out": item["child"].timed_out, "exit_code": item["code"], "crashed": result["crashed"],
            "channel": item["broken"] or "intact", "tests_run": result["found"]["tests_run"],
            "skipped": result["found"]["skipped"],
            "failures": [f.get("id") for f in done.get("failures", []) if isinstance(f, dict)],
            "errors": [e.get("id") for e in done.get("errors", []) if isinstance(e, dict)],
            "fixture_blocked": result["fixture_blocked"], "log": str(item["child"].log_path),
        })
    path.write_text(json.dumps({
        "discovered": len(discovered), "cpu_count": os.cpu_count(), "wall_seconds": round(wall, 3),
        "manifest": stats, "floor": None if base_records is None else {"base_tests": len(base_records)},
        "shards": shards, "crashed": crashed, "incomplete": incomplete,
    }, indent=1) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--child"]:
        # ``python -m unittest`` puts the working directory first on sys.path; a child does the same, in
        # place of this script's own directory, so tests import exactly as they do under ``unittest
        # discover``. The parent never imports from the working directory.
        sys.path[0] = os.getcwd()
    sys.exit(main())
