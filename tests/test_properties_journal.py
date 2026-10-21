"""The kernel properties through the journal and the Build Thread, with process death between appends.

``tests/test_properties.py`` states six invariants of the admission kernel over
random operation sequences against an in-memory ``Session``. This module drives
the same operations through ``syberlabs.journal.Journal`` in a temporary home
and, between random steps, reopens a fresh ``Session`` on that home (a new
process, in effect) or kills the process right after a journal append: the
record is durable, nothing after it ran. It then checks the same six
invariants, that every interrupted claim recovers to exactly one settlement
that agrees with the destination, that a torn final record is repaired or
refused and never half-accepted, and that two openers of one home interleave
under the lock and read the same history.

The second half does the same to the Build Thread (``syberlabs.build``) on a
real Git repository with a bare remote and a loopback metric source: death
after any append or after a destination write, then ``recover`` through the
thread, for the ``git_ref``, ``git_push`` and ``intervention`` effects.

Death is a ``BaseException`` raised by the journal after its append or by the
executor after its write, so no ``except Exception`` in the kernel can turn
it into a recorded outcome, exactly as a killed process records nothing.

Needs ``hypothesis`` (``pip install -e .[test]``); skips without it.
``docs/TESTING.md`` describes the driver and the budget.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

try:
    from hypothesis import HealthCheck, Phase, assume, example, given, note, settings, strategies as st
except ImportError:  # the package has no dependencies; this module is dev-only
    raise unittest.SkipTest("hypothesis is not installed; pip install -e .[test]")

from examples.metric_source import MetricSource
from spec.validate import validate_events
from syberlabs import Rejected, Session
from syberlabs import build as build_module
from syberlabs.build import HOST, GitRefEffect, Kit, default_contract
from syberlabs.canonical import canonical
from syberlabs.clock import as_seconds
from syberlabs.events import verify_events
from syberlabs.evidence import verified_reconciliation
from syberlabs.journal import Journal
from syberlabs.publish import GitPush
from tests import test_properties as kernel
from tests.test_build_thread import GOOD, REPORT, TEST_REPORT, Crash, git


# ---------------------------------------------------------------------------
# Budget. Every journaled append is an fsync, so examples are fewer and shorter
# than the in-memory suite's; the whole module stays under three minutes.

EXAMPLES = int(os.environ.get("SYBERLABS_JOURNAL_EXAMPLES", "30"))
THREAD_EXAMPLES = int(os.environ.get("SYBERLABS_THREAD_EXAMPLES", "8"))
MAX_OPERATIONS = 40
COMMON = dict(deadline=None, database=None, derandomize=bool(os.environ.get("CI")),
              suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
PROPERTY = settings(max_examples=EXAMPLES, **COMMON)
SHORT = settings(max_examples=max(EXAMPLES // 2, 1), **COMMON)
# A Build Thread example is a Git repository and a few subprocesses, seconds each. The drawn
# values are one crash point and a few metric values, so shrinking would only replay the same
# few seconds many times; the falsifying example is printed as drawn.
THREAD = settings(max_examples=THREAD_EXAMPLES, phases=(Phase.explicit, Phase.reuse, Phase.generate), **COMMON)

CASE = kernel.CONTRACT["id"]
SETTLE = kernel.FlakyExecutor.settle_seconds
# Codes the durable session may raise in addition to the in-memory set: both are documented in
# syberlabs/journal.py. ``journal_changed`` is left out on purpose: under the lock no second
# writer can slip in between a sync and an append, so its appearance would be a defect.
DOCUMENTED = kernel.SESSION_REFUSALS | {"record_too_large", "thread_full"}
TERMINAL = ("effect_succeeded", "effect_rejected")


# ---------------------------------------------------------------------------
# Death.

class Death:
    """A schedule of process deaths, counted in journaled event appends across reopens."""

    def __init__(self, after=()):
        self.after = set(after)
        self.appends = 0
        self.died = 0
        self.origin = 0

    def arm(self, offset: int) -> None:
        """Die after ``offset`` more appends from now; ``since_armed`` counts from here."""
        self.origin = self.appends
        self.after.add(self.appends + offset)

    @property
    def since_armed(self) -> int:
        return self.appends - self.origin

    def appended(self) -> None:
        self.appends += 1
        if self.appends in self.after:
            self.died += 1
            raise Crash()


class DyingJournal(Journal):
    """A journal whose process can die right after an event append is durable."""

    death: Death = Death()

    def add_event(self, case_id, event, side):
        super().add_event(case_id, event, side)
        self.death.appended()


class DyingExecutor(kernel.FlakyExecutor):
    """The flaky destination plus two deaths: before the write, and after it."""

    APPLY_MODES = kernel.FlakyExecutor.APPLY_MODES + ("die_before_write", "die_after_write")

    def apply(self, case_id, args, key):
        if self.apply_mode == "die_before_write":
            raise Crash()
        if self.apply_mode == "die_after_write":
            self.written.add(key)
            raise Crash()
        return super().apply(case_id, args, key)


# ---------------------------------------------------------------------------
# Operations: the kernel suite's, plus reopen, the two executor deaths, and a
# ship whose commit dies, then is reconciled after the settle window.

FLOWS = dict(kernel.FLOWS)
FLOWS["flow_ship_dies"] = lambda who, k, where, wait: [
    ("propose", "review", True, who, [], "human", (0, True, True, None)),
    ("commit", -1, True, who),
    ("executor", where, "truth"),
    ("observe", True, "build", "ci", True, k),
    ("propose", "ship", True, who, [], "human", (k, True, True, 0)),
    ("approve", -1, True, who, []),
    ("commit", -1, True, who),
    ("executor", "succeed", "truth"),
    ("advance", wait),
    ("reconcile", -1, True, who, []),
]

OPERATIONS = st.one_of(
    kernel.OPERATIONS,
    kernel.OPERATIONS,
    st.just(("reopen",)),
    st.tuples(st.just("executor"), st.sampled_from(("die_before_write", "die_after_write")),
              st.sampled_from(DyingExecutor.STATUS_MODES)),
    st.tuples(st.just("flow_ship_dies"), kernel.actor, kernel.index,
              st.sampled_from(("die_before_write", "die_after_write")), st.integers(min_value=0, max_value=120)),
)
SEQUENCES = st.lists(OPERATIONS, min_size=3, max_size=MAX_OPERATIONS)
DEATHS = st.frozensets(st.integers(min_value=1, max_value=150), max_size=6)
TURNS = st.lists(st.integers(min_value=0, max_value=1), min_size=MAX_OPERATIONS * 12, max_size=MAX_OPERATIONS * 12)


class JournalDriver(kernel.Driver):
    """The kernel driver over a journal in ``home``; reopens and dies on request.

    ``openers`` sessions share the home; ``turns`` says which one runs each
    primitive step (one opener unless a test asks for two). The destination
    (``executor``) outlives every session, as a real destination does.
    """

    def __init__(self, home: Path, deaths=(), openers: int = 1, turns=None):
        self.home = Path(home)
        self.clock = kernel.Clock()
        self.executor = DyingExecutor()
        self.death = Death()
        self.sessions: list[Session] = []
        self.turn = 0
        self.turns = list(turns or [])
        self.proposals, self.candidates, self.registered, self.refusals = [], [], 0, []
        self.crashes = self.reopens = 0
        self.settlements: list[tuple[str, str, str]] = []  # proposal, status mode at the time, outcome kind
        self.sessions = [self._open() for _ in range(openers)]
        first = self.sessions[0]
        first.install_contract(kernel.CONTRACT)
        first.install_policy(kernel.POLICY)
        for name, doc in kernel.ACTIONS.items():
            first.install_action(name, doc)
        for session in self.sessions:
            session.bind_effect("ship", self.executor)
        self.case = first.create_case(CASE, 1, {"objective": "keep the kernel honest on disk"}, "alice")
        # Deaths count appends from the first operation; the fixture above is the process that survives.
        self.death.after = {self.death.appends + n for n in deaths}

    @property
    def session(self) -> Session:
        return self.sessions[self.turn % len(self.sessions)]

    def _open(self) -> Session:
        journal = DyingJournal(self.home)
        journal.death = self.death
        try:
            return Session(clock=self.clock, journal=journal)
        except BaseException:
            journal.close()
            raise

    def reopen(self, index: int | None = None) -> None:
        """Close one opener and start a fresh session on the same home: a new process."""
        index = self.turn % len(self.sessions) if index is None else index
        self.sessions[index]._journal.close()
        self.sessions[index] = self._open()
        self.sessions[index].bind_effect("ship", self.executor)
        self.reopens += 1

    def close(self) -> None:
        for session in self.sessions:
            session._journal.close()

    def run(self, operations) -> "JournalDriver":
        step_count = 0
        for operation in operations:
            name, *drawn = operation
            if name == "reopen":
                self.reopen()
                continue
            for step in (FLOWS[name](*drawn) if name in FLOWS else [operation]):
                if self.turns:
                    self.turn = self.turns[step_count % len(self.turns)]
                step_count += 1
                name, *drawn = step
                try:
                    getattr(self, "op_" + name)(*drawn)
                except Rejected as exc:
                    self.refusals.append((name, exc.code))
                except Crash:
                    self.crashes += 1
                    self.reopen()
        return self

    def op_reconcile(self, k, well_formed, who, held):
        mode = self.executor.status_mode
        before = len(self.history())
        super().op_reconcile(k, well_formed, who, held)
        for event in self.history()[before:]:
            if event["kind"] == "reconciled" or event["kind"] == "effect_rejected":
                self.settlements.append((event["body"]["proposal_id"], mode, event["kind"]))

    def open_claims(self, history=None) -> list[dict]:
        history = self.history() if history is None else history
        done = {e["body"]["proposal_id"] for e in history if e["kind"] in TERMINAL or verified_reconciliation(e)}
        return [e for e in history if e["kind"] == "effect_started" and e["body"]["proposal_id"] not in done]

    def recover(self) -> dict[str, str | None]:
        """What ``syberlabs recover`` does: a new process, a truthful destination, every open claim reconciled."""
        self.reopen()
        self.executor.status_mode = "truth"
        self.clock.now += SETTLE + 1
        outcomes = {}
        for claim in self.open_claims():
            proposal_id = claim["body"]["proposal_id"]
            try:
                self.session.reconcile(self.case, proposal_id, "bob", ["manager"])
                outcomes[proposal_id] = self.history()[-1]["kind"]
                self.settlements.append((proposal_id, "truth", outcomes[proposal_id]))
            except Rejected as exc:
                outcomes[proposal_id] = None
                self.refusals.append(("recover", exc.code))
        return outcomes


# ---------------------------------------------------------------------------
# The six kernel invariants, as checks over a driver (bodies as in test_properties).

def check_chain(tc, driver):
    history = driver.history()
    tc.assertTrue(verify_events(history), "event hash chain broke")
    tc.assertTrue(driver.session.verify_chain(driver.case), "JCS side digest no longer matches an event")
    tc.assertEqual([e["seq"] for e in history], list(range(1, len(history) + 1)), "seq is not contiguous")


def check_admitted_once(tc, driver):
    history = driver.history()
    for position, event in enumerate(history):
        if event["kind"] == "decision" and event["body"]["status"] == "allowed":
            action = kernel.proposal_of(history, event["body"]["proposal_id"])["action"]
            earlier = [e["seq"] for e in history[:position] if kernel.completed(e) and e["body"]["action"] == action]
            tc.assertEqual(earlier, [], f"{action} was admitted at seq {event['seq']} after it completed at {earlier}")
    claims = Counter(e["body"]["proposal_id"] for e in history if e["kind"] == "effect_started")
    tc.assertTrue(all(count == 1 for count in claims.values()), f"a proposal claimed twice: {claims}")
    completions = Counter(e["body"]["action"] for e in history if kernel.completed(e))
    tc.assertTrue(all(count == 1 for count in completions.values()), f"an action completed twice: {completions}")
    terminal = Counter(e["body"]["proposal_id"] for e in history if e["kind"] in TERMINAL or verified_reconciliation(e))
    tc.assertTrue(all(count == 1 for count in terminal.values()), f"a claim settled twice: {terminal}")
    for name, code in driver.refusals:
        if name == "commit":
            tc.assertIn(code, ("unknown_proposal", "actor_mismatch", "effect_claimed"))


def check_promotable(tc, driver):
    history = driver.history()
    for position, event in enumerate(history):
        kind, body = event["kind"], event["body"]
        if kind == "decision" and body["status"] == "allowed" or kind == "effect_started":
            proposal = kernel.proposal_of(history, body["proposal_id"])
        else:
            continue
        if proposal["action"] != "promote":
            continue
        denial = kernel.promotable_in(history[:position], proposal, event["at"])
        tc.assertIsNone(denial, f"promote {kind} at seq {event['seq']} although: {denial}")


def check_outcomes_follow_claims(tc, driver):
    history = driver.history()
    claims = {}
    for event in history:
        body = event["body"]
        if event["kind"] == "effect_started":
            claims[body["proposal_id"]] = event
            tc.assertEqual(body["action"], kernel.proposal_of(history, body["proposal_id"])["action"])
            tc.assertEqual(body["idempotency_key"], body["proposal_id"])
        elif event["kind"] in kernel.EFFECT_OUTCOMES:
            claim = claims.get(body["proposal_id"])
            tc.assertIsNotNone(claim, f"{event['kind']} at seq {event['seq']} has no prior effect_started")
            tc.assertLess(claim["seq"], event["seq"])
            tc.assertEqual(body["action"], claim["body"]["action"])
            if event["kind"] in TERMINAL:
                tc.assertEqual(body["claim_hash"], claim["hash"], "outcome does not name its claim")


def check_reasons(tc, driver):
    history = driver.history()
    deciding = {row["seq"]: row["rule"] for row in driver.session.side_channel(driver.case)}
    for event in history:
        if event["kind"] != "decision":
            continue
        body = event["body"]
        owners = kernel.reason_owner(body["reason"])
        tc.assertEqual(len(owners), 1, f"reason {body['reason']!r} is owned by {owners}")
        tc.assertEqual(deciding[event["seq"]], owners[0], f"side channel names another rule at seq {event['seq']}")
        tc.assertIn(body["status"], ("allowed", "denied", "needs_approval"))
        tc.assertEqual(body["status"] == "allowed", body["reason"] == "all_checks_passed")
        tc.assertEqual(body["status"] == "needs_approval", body["reason"].startswith("approval_required:"))
    for name, code in driver.refusals:
        tc.assertIn(code, DOCUMENTED, f"{name} raised an undocumented code {code!r}")


def check_replay(tc, driver):
    history = driver.history()
    replay = driver.session.replay(driver.case, 1, 1)
    tc.assertEqual(replay["changed_decisions"], [])
    undecided = 0
    for event in history:
        if event["kind"] != "proposed":
            continue
        proposal_id = event["body"]["id"]
        stored = next((e["body"] for e in history
                       if e["kind"] == "decision" and e["body"]["proposal_id"] == proposal_id and "phase" not in e["body"]), None)
        if stored is None:
            # propose is two appends; death between them leaves a proposal the kernel never decided.
            # Commit recomputes admission, so the record is incomplete but not wrong.
            undecided += 1
            continue
        explained = driver.session.explain_admission(driver.case, proposal_id, when="recorded")
        tc.assertEqual((explained["status"], explained["reason"]), (stored["status"], stored["reason"]))
    tc.assertLessEqual(undecided, driver.crashes, "a proposal without a decision although no process died")
    tc.assertEqual(driver.history(), history, "replay or explain appended an event")


CHECKS = (check_chain, check_admitted_once, check_promotable, check_outcomes_follow_claims, check_reasons, check_replay)


def temporary_home():
    return tempfile.TemporaryDirectory(prefix="syberlabs-props-", ignore_cleanup_errors=True)


class JournalProperties(unittest.TestCase):
    """The kernel's invariants when every operation goes through the journal, across reopens and deaths."""

    @PROPERTY
    @given(SEQUENCES, DEATHS)
    def test_the_six_invariants_hold_across_reopens_and_deaths(self, operations, deaths):
        """(1)-(6) of tests/test_properties.py, over a journal-backed session that is reopened between
        random steps and killed after random appends; the history is read back by a fresh session."""
        with temporary_home() as folder:
            driver = JournalDriver(Path(folder) / "journal", deaths).run(operations)
            try:
                driver.reopen()
                for check in CHECKS:
                    check(self, driver)
            finally:
                driver.close()

    @PROPERTY
    @given(SEQUENCES, DEATHS)
    def test_recovery_settles_every_interrupted_bound_effect_once_and_truthfully(self, operations, deaths):
        """After deaths, a new process reconciling against the truthful destination settles every open
        claim of the executor-bound action exactly once, and never the other way round: a claim whose
        write happened is verified, a claim whose write did not happen is not_applied."""
        with temporary_home() as folder:
            driver = JournalDriver(Path(folder) / "journal", deaths).run(operations)
            try:
                open_before = [c["body"]["proposal_id"] for c in driver.open_claims() if c["body"]["action"] == "ship"]
                outcomes = driver.recover()
                for proposal_id in open_before:
                    self.assertIn(outcomes[proposal_id], ("reconciled", "effect_rejected"),
                                  f"claim {proposal_id} did not settle: {outcomes[proposal_id]}")
                self.assertEqual([c for c in driver.open_claims() if c["body"]["action"] == "ship"], [])
                for proposal_id, mode, kind in driver.settlements:
                    if mode != "truth":
                        continue  # a lying destination: the kernel records what it was told, by design
                    written = proposal_id in driver.executor.written
                    self.assertEqual(kind == "reconciled", written,
                                     f"claim {proposal_id} settled {kind} although written={written}")
                check_admitted_once(self, driver)
                check_chain(self, driver)
            finally:
                driver.close()

    @unittest.expectedFailure
    @SHORT
    @given(st.integers(min_value=0, max_value=20))
    def test_recovery_settles_an_interrupted_recorded_effect(self, wait):
        """An action without an executor whose commit dies between effect_started and effect_succeeded
        can never be settled: reconcile refuses (reconciliation_unavailable) and every later proposal of
        that action is denied effect_unresolved, forever.

        Counterexample: operations=[("flow_review", "alice")], death after append 3 counted from the first
        operation (proposed, decision, effect_started: the claim); then reconcile ->
        Rejected("reconciliation_unavailable") and propose review -> denied effect_unresolved:review.
        Believed cause: syberlabs/session.py, commit's executor-less branch (``output = {"recorded":
        True, ...}`` between the two appends) and reconcile's ``if executor is None: raise
        Rejected("reconciliation_unavailable", ...)``.
        """
        with temporary_home() as folder:
            driver = JournalDriver(Path(folder) / "journal", deaths=[3]).run([("flow_review", "alice"), ("advance", wait)])
            try:
                self.assertEqual(driver.crashes, 1)
                [claim] = driver.open_claims()
                self.assertEqual(claim["body"]["action"], "review")
                outcomes = driver.recover()
                self.assertIn(outcomes[claim["body"]["proposal_id"]], ("reconciled", "effect_rejected"))
                decision = driver.session.propose(driver.case, "review", {}, "alice", ["operator"])["decision"]
                self.assertEqual(decision["status"], "allowed")
            finally:
                driver.close()

    @SHORT
    @given(SEQUENCES, st.integers(min_value=0, max_value=100_000), st.booleans(),
           st.one_of(st.none(), st.binary(min_size=1, max_size=40).filter(lambda b: b"\n" not in b)))
    def test_a_torn_tail_is_repaired_or_refused_never_half_accepted(self, operations, cut, registry, garbage):
        """A partial final record (no newline) on the thread file or the registry: reopening either yields
        exactly the history before the tear and restores the file, or refuses with journal_corrupt."""
        with temporary_home() as folder:
            driver = JournalDriver(Path(folder) / "journal").run(operations)
            try:
                before = driver.history()
                side = driver.session.side_channel(driver.case)
                home = driver.home
                path = home / "registry.jsonl" if registry else home / "threads" / f"{driver.case}.jsonl"
                whole = path.read_bytes()
                if garbage is None:
                    # The prefix of a record a dying writer could have been in the middle of.
                    next_record = {"t": "event", "event": {**before[-1], "seq": len(before) + 1}, "side": side[-1]}
                    if registry:
                        next_record = {"t": "action", "name": "extra", "doc": {"kind": "local"}}
                    data = canonical(next_record).encode()
                    torn = data[:1 + cut % (len(data) - 1)]
                else:
                    torn = garbage
                driver.close()
                path.write_bytes(whole + torn)
                try:
                    driver.sessions = [driver._open()]
                    after = driver.history()
                except Rejected as exc:
                    self.assertEqual(exc.code, "journal_corrupt")
                    return
                self.assertEqual(after, before, "the reopened history differs from the one before the tear")
                self.assertEqual(path.read_bytes(), whole, "the torn bytes were not removed")
                self.assertTrue(driver.session.verify_chain(driver.case))
                driver.session.bind_effect("ship", driver.executor)
                driver.session.observe(driver.case, "after", "the tear", "ops", "v1", "alice")
                self.assertEqual(len(driver.history()), len(before) + 1)
                check_chain(self, driver)
            finally:
                driver.close()

    @SHORT
    @given(SEQUENCES, TURNS)
    def test_two_openers_interleave_under_the_lock_and_agree(self, operations, turns):
        """Two sessions on one home, each step run by whichever the sequence names: no journal_changed,
        and at the end both read the same verified history and side channel."""
        with temporary_home() as folder:
            driver = JournalDriver(Path(folder) / "journal", openers=2, turns=turns).run(operations)
            try:
                first, second = driver.sessions
                self.assertEqual(first.history(driver.case), second.history(driver.case))
                self.assertEqual(first.side_channel(driver.case), second.side_channel(driver.case))
                self.assertTrue(first.verify_chain(driver.case) and second.verify_chain(driver.case))
                self.assertNotIn("journal_changed", [code for _, code in driver.refusals])
                for check in CHECKS:
                    check(self, driver)
            finally:
                driver.close()

    def test_two_threads_append_concurrently_without_losing_or_tearing_a_record(self):
        """Two sessions in two threads append at the same time; the lock serializes them."""
        rounds = 12
        with temporary_home() as folder:
            driver = JournalDriver(Path(folder) / "journal", openers=2)
            try:
                errors = []

                def worker(session, name):
                    try:
                        for n in range(rounds):
                            session.observe(driver.case, name, n, "ops", "v1", "alice")
                    except BaseException as exc:  # pragma: no cover - reported below
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(session, f"t{i}")) for i, session in enumerate(driver.sessions)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
                self.assertEqual(errors, [])
                first, second = driver.sessions
                self.assertEqual(first.history(driver.case), second.history(driver.case))
                history = driver.sessions[0].history(driver.case)
                self.assertEqual(Counter(e["body"]["key"] for e in history if e["kind"] == "observed"), {"t0": rounds, "t1": rounds})
                self.assertTrue(verify_events(history))
                driver.reopen(0)
                self.assertEqual(driver.sessions[0].history(driver.case), history)
            finally:
                driver.close()


# ---------------------------------------------------------------------------
# The Build Thread: a real repository, a bare remote, a metric source, and death.

WINDOW = 600
MEASURE = {"kind": "local", "effect": "intervention", "metric_key": "conversion", "window_seconds": WINDOW,
           "expectation": {"direction": "up", "min_delta": 1}, "max_age_seconds": 3600}
FACTS = [{"key": "accepted", "source": "git", "verified": True}]
BOUND = {"commit": "fact:accepted.commit"}
STEPS = 24


def dying_after_write(cls):
    """Patch ``cls.apply`` so the process dies right after the destination write succeeded."""
    original = cls.apply

    def dies(self, case_id, args, key):
        original(self, case_id, args, key)
        raise Crash()

    return patch.object(cls, "apply", dies)


class Factory:
    """One temporary project under a Build Thread whose process can die.

    ``measure`` adds the intervention action against a loopback metric source.
    ``drive`` does what an operator at the CLI does: run ``status`` and the first
    thing it names next, ``recover`` when it reports an interrupted effect,
    and move the fake clock past a window when a claim is pending on it.
    """

    def __init__(self, folder: Path, death: Death, *, measure: bool):
        self.root = folder / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "report.py").write_text(REPORT)
        (self.root / "tests" / "test_report.py").write_text(TEST_REPORT)
        git(folder, "init", "-q", "-b", "main", str(self.root))
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "initial")
        self.bare = folder / "remote.git"
        git(folder, "init", "-q", "--bare", str(self.bare))
        git(self.root, "remote", "add", "origin", str(self.bare))
        git(self.root, "push", "-q", "origin", "main")
        self.death = death
        self.clock = [time.time()]
        self.source = MetricSource(port=0) if measure else None
        self.kit = None
        doc = default_contract(self.root)
        doc["evolution"]["evaluation"]["checks"] = {"tests": {"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60}}
        doc["evolution"]["evaluation"]["required"] = ["tests"]
        doc["actions"]["push_branch"] = {"requires_effect": "accept_change", "required_facts": FACTS, "arguments": BOUND}
        actions = {"push_branch": {"kind": "local", "effect": "git_push", "remote": "origin", "settle_seconds": 0}}
        if measure:
            doc["actions"]["measure"] = {"requires_effect": "push_branch", "required_facts": FACTS, "arguments": BOUND}
            actions["measure"] = {**MEASURE, "metric_url": self.source.url}
        home = self.root / ".syberlabs"
        (home / "contracts").mkdir(parents=True)
        (home / "contracts" / "repo-change.v1.json").write_text(json.dumps(doc))
        (home / "policy.json").write_text(json.dumps({"version": 1, "actions": {n: {"roles": ["developer"]} for n in doc["actions"]}}))
        (home / "actions.json").write_text(json.dumps(actions))

    def open(self) -> Kit:
        """A kit on this home: the first one, or a new process after a death."""
        if self.kit is not None:
            self.kit.close()
        with patch.object(build_module, "Journal", self._journal):
            self.kit = Kit.local(self.root / ".syberlabs", self.root, actor="dev@example.test")
        self.kit.session.clock = lambda: self.clock[0]
        return self.kit

    def _journal(self, home):
        journal = DyingJournal(home)
        journal.death = self.death
        return journal

    def close(self) -> None:
        if self.kit is not None:
            self.kit.close()
        if self.source is not None:
            self.source.close()

    def metric(self, value: float) -> None:
        self.source.set("conversion", value, observed_at=self.clock[0])

    def start(self):
        kit = self.open()
        return kit.start("Add a CSV export", paths=["src/", "tests/"])

    def drive(self, thread, *, on_step=None, on_death=None, steps: int = STEPS):
        """Follow ``status`` until nothing is next, or the next step was already taken twice (an effect
        that settled not_applied is offered again; an operator would stop). Returns the steps taken.
        A death reopens the kit; ``self.thread`` is always the thread of the live kit."""
        taken = []
        self.thread = thread
        for _ in range(steps):
            try:
                status = thread.status()
                if status["open"]:
                    taken.append("recover")
                    receipts = thread.recover()
                    if any(r.status == "pending" for r in receipts):
                        self.clock[0] += WINDOW + 1
                        taken.append("wait")
                    continue
                # status lists publishing actions in contract order (canonical JSON sorts them), so an
                # action whose prior effect is still missing comes up first and admission denies it; an
                # operator reads the denial and runs the next one, and so does this loop.
                candidates = [step for step in status["next"] if taken.count(step) < 2]
                if not candidates:
                    return taken
                for step in candidates:
                    taken.append(step)
                    if on_step is not None:
                        on_step(step)
                    verb, _, argument = step.partition(" ")[2].partition(" ")
                    if verb == "propose":
                        thread.propose(changes=GOOD)
                    elif verb == "check":
                        thread.check(argument)
                    elif verb == "accept":
                        thread.accept(argument)
                    elif verb == "publish":
                        receipt = thread.publish(argument)
                        if receipt.status == "denied":
                            continue
                        if receipt.status == "unknown":
                            self.clock[0] += WINDOW + 1
                            taken.append("wait")
                    else:  # pragma: no cover - a step this driver does not know
                        raise AssertionError(f"unknown step {step!r}")
                    break
            except Crash:
                taken.append("death")
                if on_death is not None:
                    on_death()
                thread = self.thread = self.open().open(thread.id)
        raise AssertionError(f"the thread did not settle in {steps} steps: {taken}")


def by_proposal(history, kinds):
    return Counter(e["body"]["proposal_id"] for e in history if e["kind"] in kinds or (kinds is None and verified_reconciliation(e)))


class BuildThreadDeaths(unittest.TestCase):
    """Death after any journal append or destination write, then recovery through the Build Thread."""

    def assert_consistent(self, factory, thread):
        history = thread.history()
        self.assertEqual(validate_events(history, "thread"), [])
        self.assertTrue(factory.kit.session.verify_chain(thread.id))
        claims = by_proposal(history, ("effect_started",))
        self.assertTrue(all(n == 1 for n in claims.values()), f"a proposal claimed twice: {claims}")
        settled = Counter(e["body"]["proposal_id"] for e in history if e["kind"] in TERMINAL or verified_reconciliation(e))
        self.assertTrue(all(n == 1 for n in settled.values()), f"a claim settled twice: {settled}")
        self.assertEqual(set(claims), set(settled), "an interrupted claim was left open after recovery")
        completed = Counter(e["body"]["action"] for e in history if kernel.completed(e))
        self.assertEqual(completed["accept_change"], 1, f"acceptance completed {completed['accept_change']} times")
        self.assertEqual(completed["push_branch"], 1, f"push completed {completed['push_branch']} times")
        # The record and the destinations agree.
        [accepted] = [v for v in factory.kit.session.candidates(thread.id) if v["authoritative"]]
        self.assertEqual(git(factory.root, "rev-parse", thread.target_ref), accepted["commit"])
        self.assertEqual(git(factory.bare, "rev-parse", thread.target_ref), accepted["commit"])
        self.assertEqual(git(factory.root, "rev-parse", "main"), git(factory.bare, "rev-parse", "main"))
        return history

    @THREAD
    @given(st.one_of(st.tuples(st.just("append"), st.integers(min_value=1, max_value=22)),
                     st.tuples(st.just("after_write"), st.sampled_from(("git_ref", "git_push")))))
    def test_accept_and_push_survive_a_death_anywhere(self, where):
        """propose, check, accept, push_branch: the process dies after the n-th journal append from the
        start of the thread, or right after the ref update or the push wrote its destination. A new
        process recovers through the thread; every claim settles once, in agreement with Git."""
        kind, point = where
        with temporary_home() as folder:
            factory = Factory(Path(folder), Death(), measure=False)
            try:
                thread = factory.start()
                if kind == "append":
                    factory.death.arm(point)  # counted from the first step after start; start has its own test
                if kind == "after_write":
                    cls = {"git_ref": GitRefEffect, "git_push": GitPush}[point]
                    with dying_after_write(cls):
                        try:
                            factory.drive(thread, on_step=None, steps=6)
                        except (Crash, AssertionError):
                            pass
                    thread = factory.open().open(thread.id)
                taken = factory.drive(thread)
                note(f"appends after start: {factory.death.since_armed}; steps: {taken}")
                self.assert_consistent(factory, factory.thread)
                if kind == "append" and point <= factory.death.since_armed:
                    self.assertEqual(factory.death.died, 1, taken)
                    self.assertIn("death", taken)
            finally:
                factory.close()

    # Fixed on main by #54 (thread_incomplete and an idempotent start); kept as a regression property.
    @THREAD
    @example(1)
    @given(st.integers(min_value=1, max_value=3))
    def test_a_death_during_start_leaves_a_thread_that_reopens_or_refuses(self, point):
        """start is several appends (case_created, the base, the context). After death between them,
        opening the thread either works or refuses with a Rejected code, never an uncaught error.

        Counterexample (this branch): point=1, death right after case_created and before the base
        observation. The thread is listed by the journal but ``Kit.open`` raises
        ``TypeError: 'NoneType' object is not subscriptable``. Believed cause: syberlabs/build.py,
        ``Thread.__init__`` (``self.base = self._latest("base")["value"]["commit"]``), reached from
        ``Kit.open``; ``Kit.start`` writes case_created, the base and the context as separate appends.
        """
        with temporary_home() as folder:
            factory = Factory(Path(folder), Death([point]), measure=False)
            try:
                thread_id = None
                try:
                    thread_id = factory.start().id
                except Crash:
                    pass
                self.assertEqual(factory.death.died, 1)
                kit = factory.open()
                [thread_id] = kit.journal.thread_ids() if thread_id is None else [thread_id]
                try:
                    thread = kit.open(thread_id)
                except Rejected:
                    return
                self.assertTrue(kit.session.verify_chain(thread.id))
                thread.status()
            finally:
                factory.close()

    # Fixed on main by #49 (the settlement read-back); kept as a regression property.
    @THREAD
    @example(7, 12.0, 10.0)
    @given(st.integers(min_value=0, max_value=7), st.sampled_from((12.0, 10.0, 8.0)), st.sampled_from((12.0, 10.0, 8.0)))
    def test_an_intervention_settles_once_from_its_first_durable_reading(self, point, moved_to, later):
        """publish measure: baseline 10.0, the metric at ``moved_to`` after the window, then the process
        dies after the n-th append from the first publish of measure, the metric moves to ``later``, and a
        new process recovers. One claim gets at most one settlement reading, and the verdict is the one
        that reading supports.

        Counterexample (this branch): point=7 (death right after the settlement observation, before the
        reconciled event), moved_to=12.0, later=10.0. Recovery reads the metric again, appends a second
        settlement observation (outcome unchanged) and settles effect_rejected not_applied although the
        first durable settlement said moved: two readings, verdict the other way. Believed cause:
        syberlabs/outcome.py, Intervention.status re-reads the metric without first looking for a
        settlement observation already in the chain; PR #38 (f555ab2) added that read-back, but it was
        merged into syberlabs/c323757b after #35 was squashed, so it is on neither main nor this branch.
        """
        with temporary_home() as folder:
            factory = Factory(Path(folder), Death(), measure=True)
            try:
                factory.metric(10.0)
                thread = factory.start()
                armed = []

                def arm(step):
                    # The first publish of measure that admission can allow: push_branch has completed.
                    pushed = any(kernel.completed(e) and e["body"]["action"] == "push_branch" for e in factory.thread.history())
                    if step == "syberlabs publish measure" and pushed and not armed:
                        armed.append(step)
                        factory.death.arm(point)
                        factory.pending_move = moved_to

                # The metric moves once the window has passed, before the settlement reading.
                original_recover = thread.__class__.recover

                def recover_moving(this, **kwargs):
                    if getattr(factory, "pending_move", None) is not None and factory.clock[0] - start[0] > WINDOW:
                        factory.metric(factory.pending_move)
                        factory.pending_move = None
                    return original_recover(this, **kwargs)

                start = [factory.clock[0]]
                with patch.object(thread.__class__, "recover", recover_moving):
                    taken = factory.drive(thread, on_step=arm, on_death=lambda: factory.metric(later))
                note(f"appends after start: {factory.death.appends}; steps: {taken}")
                history = self.assert_consistent(factory, factory.thread)
                claims = [e for e in history if e["kind"] == "effect_started" and e["body"]["action"] == "measure"]
                assume(claims)  # the process died before the effect was claimed: there is nothing to settle
                for claim in claims:
                    key = claim["body"]["proposal_id"]
                    readings = [e for e in history if e["kind"] == "observed" and e["body"]["key"] == "intervention"
                                and e["body"]["value"].get("proposal_id") == key]
                    phases = Counter(r["body"]["value"]["phase"] for r in readings)
                    self.assertLessEqual(phases["baseline"], 1, f"two baselines for one claim: {taken}")
                    self.assertLessEqual(phases["settlement"], 1, f"two settlement readings for one claim: {taken}")
                    settlement = next((r["body"]["value"] for r in readings if r["body"]["value"]["phase"] == "settlement"), None)
                    verdict = next(e for e in history if e["body"].get("proposal_id") == key
                                   and (e["kind"] in TERMINAL or verified_reconciliation(e)))
                    if settlement is not None:
                        expected = "reconciled" if settlement["outcome"] == "moved" else "effect_rejected"
                        self.assertEqual(verdict["kind"], expected, f"verdict the other way from the durable reading: {taken}")
            finally:
                factory.close()


if __name__ == "__main__":
    unittest.main()
