"""One conformance run across the in-memory and the durable session, plus journal failure modes."""

import errno
import importlib.util
import itertools
import json
import os
import tempfile
import threading
import types
import uuid
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from syberlabs import Rejected, Session
from syberlabs import journal as journal_module
from syberlabs.journal import Journal
from syberlabs.session import NoWrite

ROOT = Path(__file__).resolve().parents[1]


def example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_gate(session):
    """The release-gate flow from examples/release_gate.py on a given session."""
    gate = example("release_gate")
    session.install_contract(gate.CONTRACT)
    session.install_policy(gate.POLICY)
    session.install_action("record_checks", {"kind": "local"})
    session.install_action("publish", {"kind": "local"})
    case = session.create_case("release-gate", 1, {"version": "1.4.0"}, "engineer")
    session.observe(case, "checks", {"version": "1.4.0", "passed": True}, "ci", "build-90", "engineer", verified=True)
    review = session.propose(case, "record_checks", {"version": "1.4.0"}, "engineer", ["engineer"])
    session.commit(case, review["proposal"]["id"], "engineer")
    publish = session.propose(case, "publish", {"version": "1.4.0"}, "engineer", ["engineer"])
    session.approve(case, publish["proposal"]["id"], "maintainer", ["maintainer"])
    session.commit(case, publish["proposal"]["id"], "engineer")
    session.signoff(case, "maintainer", ["maintainer"], "maintainer")
    return case


def shape(session, case):
    inspected = session.inspect(case)
    return {"kinds": [e["kind"] for e in inspected["events"]],
            "decisions": [(e["body"]["status"], e["body"]["reason"]) for e in inspected["events"] if e["kind"] == "decision"],
            "status": inspected["status"], "acceptance": inspected["acceptance"], "chain": inspected["chain_valid"]}


class FakeMsvcrt:
    """Stands in for ``msvcrt`` on any platform: refuses the first ``busy`` LK_NBLCK calls.

    Records (mode, bytes, descriptor position) per call, then moves the position
    away from zero so only code that seeks before each call records zero.
    """

    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self, busy=0):
        self.busy = busy
        self.error = None
        self.calls = []

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        os.lseek(fd, 7, os.SEEK_SET)
        if mode == self.LK_NBLCK and self.error is not None:
            raise OSError(self.error, "broken handle")
        if mode == self.LK_NBLCK and self.busy:
            self.busy -= 1
            raise OSError(errno.EACCES, "locked by another handle")


class Durable(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "journal"

    def durable(self):
        journal = Journal(self.home)
        self.addCleanup(journal.close)
        return Session(journal=journal)

    def test_same_conformance_run_on_both_stores(self):
        memory = Session()
        durable = self.durable()
        expected = shape(memory, release_gate(memory))
        case = release_gate(durable)
        self.assertEqual(shape(durable, case), expected)
        self.assertEqual(expected["status"], "complete")
        reopened = self.durable()
        self.assertEqual(shape(reopened, case), expected)
        self.assertTrue(reopened.verify_chain(case))
        self.assertEqual(reopened.side_channel(case), durable.side_channel(case))
        self.assertEqual(reopened.replay(case, 1, 1)["changed_decisions"], [])

    def test_access_review_example_runs_on_the_durable_store(self):
        review = example("access_review")
        durable = self.durable()
        with patch.object(review, "Session", lambda: durable):
            done = review.complete_review(review.open_session())
        self.assertEqual((done["status"], done["chain_valid"]), ("complete", True))
        reopened = self.durable()
        self.assertTrue(reopened.verify_chain(done["case_id"]))
        self.assertEqual(reopened.inspect(done["case_id"])["status"], "complete")

    def test_torn_final_record_is_dropped(self):
        durable = self.durable()
        case = release_gate(durable)
        path = self.home / "threads" / f"{case}.jsonl"
        whole = path.read_bytes()
        path.write_bytes(whole + b'{"t":"event","event":{"case_id"')
        reopened = self.durable()
        self.assertEqual(shape(reopened, case), shape(durable, case))
        self.assertEqual(path.read_bytes(), whole)

    def test_corruption_and_tampering_refuse_to_load(self):
        durable = self.durable()
        case = release_gate(durable)
        path = self.home / "threads" / f"{case}.jsonl"
        lines = path.read_text().splitlines()
        garbled = lines[:3] + ["{not json"] + lines[4:]
        path.write_text("\n".join(garbled) + "\n")
        with self.assertRaises(Rejected) as caught:
            self.durable().inspect(case)
        self.assertEqual(caught.exception.code, "journal_corrupt")
        record = json.loads(lines[3])
        record["event"]["body"]["actor"] = "someone-else"
        path.write_text("\n".join(lines[:3] + [json.dumps(record)] + lines[4:]) + "\n")
        with self.assertRaises(Rejected) as caught:
            self.durable().inspect(case)
        self.assertEqual(caught.exception.code, "journal_corrupt")

    def test_registry_contract_that_no_longer_validates(self):
        durable = self.durable()
        release_gate(durable)
        registry = self.home / "registry.jsonl"
        lines = registry.read_text().splitlines()
        record = json.loads(lines[0])
        record["doc"]["acceptance"].append({"id": "published", "kind": "effect", "action": "publish"})
        registry.write_text("\n".join([json.dumps(record)] + lines[1:]) + "\n")
        with self.assertRaises(Rejected) as caught:
            self.durable()
        self.assertEqual(caught.exception.code, "journal_corrupt")

    def test_two_processes_see_each_others_appends(self):
        first = self.durable()
        case = release_gate(first)
        second = self.durable()
        second.observe(case, "note", "from the second process", "ops", "v1", "engineer")
        first.observe(case, "note", "from the first process", "ops", "v2", "engineer")
        self.assertEqual(shape(first, case), shape(self.durable(), case))
        self.assertTrue(self.durable().verify_chain(case))
        self.assertEqual([e["body"]["value"] for e in first.inspect(case)["events"] if e["body"].get("key") == "note"],
                         ["from the second process", "from the first process"])

    def test_threads_are_bounded(self):
        durable = self.durable()
        with patch.object(journal_module, "MAX_EVENTS", 3):
            durable.install_contract(example("release_gate").CONTRACT)
            case = durable.create_case("release-gate", 1, {"version": "1"}, "engineer")
            durable.observe(case, "a", 1, "s", "v", "x")
            durable.observe(case, "b", 1, "s", "v", "x")
            with self.assertRaises(Rejected) as caught:
                durable.observe(case, "c", 1, "s", "v", "x")
        self.assertEqual(caught.exception.code, "thread_full")
        self.assertEqual(len(self.durable().inspect(case)["events"]), 3)

    def test_windows_lock_path_acquires_once_and_releases_once(self):
        fake = FakeMsvcrt(busy=2)
        journal = Journal(self.home)
        self.addCleanup(journal.close)
        # Only the ``time`` that syberlabs.journal sees is replaced (its one use is the lock's sleep); patching
        # ``sleep`` through ``journal_module.time`` would patch the ``time`` module for the whole process.
        sleep = Mock()
        with patch.object(journal_module, "fcntl", None), patch.object(journal_module, "msvcrt", fake), \
                patch.object(journal_module, "LOCKING", True), \
                patch.object(journal_module, "time", types.SimpleNamespace(sleep=sleep)):
            with journal.locked():
                with journal.locked():
                    self.assertEqual(fake.calls, [(fake.LK_NBLCK, 1, 0)] * 3)
            self.assertEqual(fake.calls, [(fake.LK_NBLCK, 1, 0)] * 3 + [(fake.LK_UNLCK, 1, 0)])
            self.assertEqual(sleep.call_args_list, [call(0.05), call(0.05)])
            fake.error = errno.EBADF
            with self.assertRaises(OSError) as caught:
                with journal.locked():
                    self.fail("entered the lock through a broken handle")
            self.assertEqual(caught.exception.errno, errno.EBADF)
            self.assertEqual(fake.calls[-1], (fake.LK_NBLCK, 1, 0))
            self.assertEqual(sleep.call_count, 2)

    @unittest.skipUnless(journal_module.LOCKING, "neither fcntl nor msvcrt: the lock is in-process only")
    def test_the_lock_serializes_two_journals_on_one_home(self):
        first = Journal(self.home)
        self.addCleanup(first.close)
        second = Journal(self.home)
        self.addCleanup(second.close)
        entered = threading.Event()

        def contend():
            with second.locked():
                entered.set()

        worker = threading.Thread(target=contend, daemon=True)
        with first.locked():
            worker.start()
            self.assertFalse(entered.wait(0.5), "a second handle entered while the first held the lock")
        self.assertTrue(entered.wait(5), "the second handle never entered after release")
        worker.join(5)
        self.assertFalse(worker.is_alive())


class Executors(unittest.TestCase):
    def setUp(self):
        gate = example("release_gate")
        self.session = Session()
        self.session.install_contract(gate.CONTRACT)
        self.session.install_policy(gate.POLICY)
        self.session.install_action("record_checks", {"kind": "local", "effect": "ci_record"})
        self.session.install_action("publish", {"kind": "local"})
        self.case = self.session.create_case("release-gate", 1, {"version": "1.4.0"}, "engineer")
        self.session.observe(self.case, "checks", {"version": "1.4.0"}, "ci", "b1", "engineer", verified=True)

    def proposal(self):
        return self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer", ["engineer"])["proposal"]["id"]

    def test_named_effect_needs_its_executor(self):
        pid = self.proposal()
        with self.assertRaises(Rejected) as caught:
            self.session.commit(self.case, pid, "engineer")
        self.assertEqual(caught.exception.code, "effect_unavailable")
        self.assertFalse(any(e["kind"] == "effect_started" for e in self.session.inspect(self.case)["events"]))
        with self.assertRaises(Rejected):
            self.session.bind_effect("publish", object())

    def test_no_write_and_unknown_outcomes(self):
        class Refuses:
            def apply(self, case_id, args, key):
                raise NoWrite(412, "precondition")

            def status(self, case_id, args, key):
                return "not_applied", {}

        class Fails:
            state = "unknown"

            def apply(self, case_id, args, key):
                raise OSError("disk")

            def status(self, case_id, args, key):
                return self.state, {"external_id": "x1"}

        self.session.bind_effect("record_checks", Refuses())
        self.assertEqual(self.session.commit(self.case, self.proposal(), "engineer")["status"], "rejected")
        failing = Fails()
        self.session.bind_effect("record_checks", failing)
        pid = self.proposal()
        self.assertEqual(self.session.commit(self.case, pid, "engineer")["status"], "unknown")
        self.assertEqual(self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer",
                                              ["engineer"])["decision"]["reason"], "effect_unresolved:record_checks")
        with self.assertRaises(Rejected):
            self.session.reconcile(self.case, pid, "guest", ["guest"])
        self.assertEqual(self.session.reconcile(self.case, pid, "engineer", ["engineer"])["status"], "pending")
        failing.state = "applied"
        self.assertEqual(self.session.reconcile(self.case, pid, "engineer", ["engineer"])["status"], "verified")
        with self.assertRaises(Rejected):
            self.session.reconcile(self.case, pid, "engineer", ["engineer"])
        self.assertEqual(self.session.inspect(self.case)["acceptance"][0], {"id": "published", "passed": False})

    def test_unknown_then_absent_frees_the_action_after_the_settle_window(self):
        class Remote:
            settle_seconds = 60
            state = "absent"

            def apply(self, case_id, args, key):
                raise OSError("connection reset")

            def status(self, case_id, args, key):
                return self.state, {}

        clock = [__import__("time").time()]
        self.session.clock = lambda: clock[0]
        self.session.bind_effect("record_checks", Remote())
        pid = self.proposal()
        self.assertEqual(self.session.commit(self.case, pid, "engineer")["status"], "unknown")
        found = self.session.reconcile(self.case, pid, "engineer", ["engineer"])
        self.assertEqual((found["status"], found["reason"]), ("pending", "absent_within_settle_window"))
        self.assertEqual(self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer",
                                              ["engineer"])["decision"]["reason"], "effect_unresolved:record_checks")
        clock[0] += 61
        self.assertEqual(self.session.reconcile(self.case, pid, "engineer", ["engineer"])["status"], "not_applied")
        self.assertEqual(self.session.propose(self.case, "record_checks", {"version": "1.4.0"}, "engineer",
                                              ["engineer"])["decision"]["status"], "allowed")



def reference_read_new(self, path):
    """``Journal.read_new`` as it was before perf-packet-a, which opened the file on every call."""
    offset = self._offsets.get(path, 0)
    if not path.exists():
        return []
    with open(path, "r+b") as handle:
        handle.seek(offset)
        data = handle.read()
        complete = data.rfind(b"\n") + 1
        if complete < len(data):
            handle.truncate(offset + complete)
            handle.flush()
            os.fsync(handle.fileno())
    records = []
    for line in data[:complete].splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            raise Rejected("journal_corrupt", f"{path.name}: unreadable record after byte {offset}") from None
    self._offsets[path] = offset + complete
    return records


class StatBeforeOpen(unittest.TestCase):
    """perf-packet-a (efficiency audit 2026-10-26, item 2): ``read_new`` does not open a file whose size equals the
    offset it has read. Every guarded operation reads the registry and each loaded thread, and on Windows an open of
    a file this process has just written waits on the file-system filter (6.2 ms per append without fsync)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def scenario(self, home, reader):
        """Two sessions interleave on one home, a writer dies mid-record, a third session repairs it and the first
        writes again. Returns every journal byte, every read, and what each session shows."""
        reads, journals = [], []
        ids = itertools.count(1)
        ticks = itertools.count(1_800_000_000)

        def logged(journal, path):
            records = reader(journal, path)
            reads.append((path.name, records))
            return records

        def session():
            journal = Journal(home)
            journals.append(journal)
            return Session(journal=journal, clock=lambda: float(next(ticks)))

        with patch.object(Journal, "read_new", logged), patch("syberlabs.session.uuid.uuid4",
                                                              lambda: uuid.UUID(int=next(ids))):
            first = session()
            case = release_gate(first)
            second = session()
            second.observe(case, "note", "from the second process", "ops", "v1", "engineer")
            first.observe(case, "note", "from the first process", "ops", "v2", "engineer")
            with open(home / "threads" / f"{case}.jsonl", "ab") as handle:
                handle.write(b'{"t":"event","event":{"case_id"')
            shown = [shape(session(), case), shape(first, case), shape(second, case)]
            first.observe(case, "note", "after the repair", "ops", "v3", "engineer")
            shown.append(shape(second, case))
        for journal in journals:
            journal.close()
        files = {p.relative_to(home).as_posix(): p.read_bytes() for p in sorted(home.rglob("*.jsonl"))}
        return files, reads, shown

    def test_every_byte_and_every_read_equal_the_reader_that_always_opened(self):
        new = Journal.read_new
        expected = self.scenario(self.root / "reference", reference_read_new)
        self.assertEqual(self.scenario(self.root / "stat", new), expected)
        files, reads, shown = expected
        self.assertEqual(len(files), 2, "the registry and one thread")
        self.assertTrue(any(records == [] for _name, records in reads), "unchanged files were read")
        self.assertEqual(len({repr(s) for s in shown[:3]}), 1)

    def test_an_unchanged_file_is_not_opened_and_a_grown_one_is_read_once(self):
        journal = Journal(self.root / "journal")
        self.addCleanup(journal.close)
        session = Session(journal=journal)
        case = release_gate(session)
        real_open, opened = open, []

        def spy(file, mode="r", *args, **kwargs):
            opened.append((Path(file).name, mode))
            return real_open(file, mode, *args, **kwargs)

        with patch("builtins.open", spy):
            before = session.inspect(case)["events"]
            session.inspect(case)
        self.assertEqual([item for item in opened if item[1] == "r+b"], [], "no new bytes: nothing is re-opened")
        other = Journal(self.root / "journal")
        self.addCleanup(other.close)
        Session(journal=other).observe(case, "note", "from another process", "ops", "v1", "engineer")
        opened.clear()
        with patch("builtins.open", spy):
            after = session.inspect(case)["events"]
        self.assertEqual([item for item in opened if item[1] == "r+b"], [(f"{case}.jsonl", "r+b")])
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1]["body"]["value"], "from another process")


class RefusedStaysRefused(unittest.TestCase):
    """perf-a-followups (verification of #105, finding 4): ``read_new`` moves the read offset past the records it
    returns before the session checks their chain. A thread refused once was then served from its verified prefix
    on the next access in the same process, so a long-running reader showed altered history as healthy from the
    second request on. A thread or a registry that does not verify stays refused, in this session, in another on the
    same journal and in a new one, until its file verifies from the first record."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "journal"

    def durable(self, journal=None):
        if journal is None:
            journal = Journal(self.home)
            self.addCleanup(journal.close)
        return Session(journal=journal)

    def assertRefused(self, operation):
        with self.assertRaises(Rejected) as caught:
            operation()
        self.assertEqual(caught.exception.code, "journal_corrupt")

    def test_an_altered_thread_is_refused_on_every_access(self):
        case = release_gate(self.durable())
        path = self.home / "threads" / f"{case}.jsonl"
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        record = json.loads(lines[3])
        record["event"]["body"]["actor"] = "someone-else"
        path.write_bytes(b"".join(lines[:3] + [(json.dumps(record) + "\n").encode()] + lines[4:]))
        altered = path.read_bytes()
        session = self.durable()
        for _attempt in range(3):
            self.assertRefused(lambda: session.inspect(case))
            self.assertRefused(lambda: session.history(case))
            self.assertRefused(lambda: session.verify_chain(case))
        self.assertRefused(lambda: self.durable(session._journal).inspect(case))
        self.assertRefused(lambda: self.durable().inspect(case))
        self.assertEqual(path.read_bytes(), altered, "refusing writes nothing")
        path.write_bytes(original)
        self.assertEqual(session.inspect(case)["status"], "complete")
        self.assertTrue(session.verify_chain(case))

    def test_an_altered_append_to_a_loaded_thread_stays_refused_and_nothing_is_appended_after_it(self):
        first = self.durable()
        case = release_gate(first)
        other = release_gate(first)
        self.durable().observe(case, "note", "appended by another process", "ops", "v1", "engineer")
        path = self.home / "threads" / f"{case}.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        record = json.loads(lines[-1])
        record["event"]["body"]["value"] = "altered"
        path.write_bytes(b"".join(lines[:-1] + [(json.dumps(record) + "\n").encode()]))
        altered = path.read_bytes()
        self.assertRefused(lambda: first.inspect(other))  # the first operation after the change, as before
        self.assertEqual(first.inspect(other)["status"], "complete", "other threads are served again")
        for _attempt in range(2):
            self.assertRefused(lambda: first.inspect(case))
            self.assertRefused(lambda: first.observe(case, "note", "after the altered record", "ops", "v2", "engineer"))
        self.assertEqual(path.read_bytes(), altered, "nothing was appended after the altered record")

    def test_a_registry_that_stops_validating_stays_refused(self):
        first = self.durable()
        case = release_gate(first)
        registry = self.home / "registry.jsonl"
        original = registry.read_bytes()
        record = json.loads(original.splitlines()[0])
        record["doc"]["acceptance"].append({"id": "published", "kind": "effect", "action": "publish"})
        registry.write_bytes(original + (json.dumps(record) + "\n").encode())
        for _attempt in range(3):
            self.assertRefused(lambda: first.inspect(case))
        registry.write_bytes(original)
        self.assertEqual(first.inspect(case)["status"], "complete")

    def test_an_honest_append_after_a_torn_line_repair_still_loads(self):
        first = self.durable()
        case = release_gate(first)
        path = self.home / "threads" / f"{case}.jsonl"
        whole = path.read_bytes()
        with open(path, "ab") as handle:
            handle.write(b'{"t":"event","event":{"case_id"')
        second = self.durable()
        self.assertEqual(shape(second, case), shape(first, case))
        self.assertEqual(path.read_bytes(), whole, "the torn record was cut")
        second.observe(case, "note", "after the repair", "ops", "v1", "engineer")
        first.observe(case, "note", "and from the first", "ops", "v2", "engineer")
        third = self.durable()
        for session in (first, second, third, first):
            self.assertTrue(session.verify_chain(case))
            self.assertEqual(shape(session, case), shape(third, case))
        self.assertEqual([e["body"]["value"] for e in third.inspect(case)["events"] if e["body"].get("key") == "note"],
                         ["after the repair", "and from the first"])


if __name__ == "__main__":
    unittest.main()
