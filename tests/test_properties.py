"""Properties of the admission kernel over random event sequences.

Each test draws a random sequence of operations, drives it against an
in-memory ``syberlabs.Session`` (no journal) under a contract that reaches most
admission rules, and then checks one invariant over the whole recorded history.
Most drawn operations are well formed, so sequences reach admitted effects,
approvals, promotions and reconciliations; a minority are adversarial: wrong
roles, model-origin promotions, candidates whose provider signal claims
success, stale facts, executors that crash after writing, destinations that
answer wrongly, double commits and out-of-order approvals.

The module needs ``hypothesis`` (``pip install -e .[test]``). Without it the
module skips, so the plain suite still passes. ``docs/TESTING.md`` explains how
to read a counterexample and how to change the example budget.
"""

from __future__ import annotations

import os
import unittest
from collections import Counter

try:
    from hypothesis import HealthCheck, given, settings, strategies as st
except ImportError:  # the package has no dependencies; this module is dev-only
    raise unittest.SkipTest("hypothesis is not installed; pip install -e .[test]")

from syberlabs import Rejected, Session
from syberlabs.admission import RULES
from syberlabs.clock import as_seconds
from syberlabs.events import verify_events
from syberlabs.evidence import verified_reconciliation
from syberlabs.evolution import AUTOMATION_ROLES, CHECK_STATES, latest_evaluation, registered
from syberlabs.evolution import settings as evolution_settings
from syberlabs.session import NoWrite


# ---------------------------------------------------------------------------
# Budget. CI sets CI=true, which derandomizes the run so a red check reproduces.

EXAMPLES = int(os.environ.get("SYBERLABS_PROPERTY_EXAMPLES", "150"))
MAX_OPERATIONS = 60
# The profile keeps the deadline off for every Hypothesis test in this process, decorated or not;
# PROPERTY inherits from it.
settings.register_profile("syberlabs", deadline=None)
settings.load_profile("syberlabs")
PROPERTY = settings(
    max_examples=EXAMPLES,
    deadline=None,
    database=None,
    derandomize=bool(os.environ.get("CI")),
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------
# The contract under test. One plain action, one amount-limited action, one
# action with a prior effect, a verified fact, a bound argument, an approval and
# a flaky executor, and one promotion action under an evolution section.

def oid(n: int) -> str:
    return f"{n:040x}"


BASE = oid(0)
DIGEST = "0" * 64
ACTION_NAMES = ("review", "pay", "ship", "promote")

CONTRACT = {
    "id": "kernel-properties",
    "version": 1,
    "inputs": {"objective": "string"},
    "actions": {
        "review": {},
        "pay": {"max_amount": 100},
        "ship": {
            "requires_effect": "review",
            "required_facts": [{"key": "build", "source": "ci", "verified": True, "max_age_seconds": 600}],
            "arguments": {"commit": "fact:build.commit"},
            "approval_role": "manager",
        },
        "promote": {},
    },
    "acceptance": [{"id": "shipped", "kind": "effect", "action": "ship"}],
    "evolution": {
        "scope": {"paths": ["src/"], "max_files": 2, "max_diff_bytes": 1000},
        "operators": ["patch"],
        "budget": {"max_candidates": 6, "max_evaluations": 12, "max_seconds": 60},
        "evaluation": {
            "checks": {"tests": {"argv": ["true"]}, "lint": {"argv": ["true"]}},
            "required": ["tests", "lint"],
            "max_age_seconds": 300,
        },
        "promotion": {"action": "promote", "roles": ["developer"]},
    },
}
POLICY = {
    "version": 1,
    "actions": {
        "review": {"roles": ["operator"]},
        "pay": {"roles": ["operator"], "max_amount": 50},
        "ship": {"roles": ["operator"]},
        "promote": {"roles": ["developer", "model", "search", "reviewer"]},
    },
}
ACTIONS = {
    "review": {"kind": "local"},
    "pay": {"kind": "local"},
    "ship": {"kind": "local", "effect": "flaky"},
    "promote": {"kind": "local"},
}
EVOLUTION = evolution_settings(CONTRACT)
PROPOSER_ROLE = {"review": "operator", "pay": "operator", "ship": "operator", "promote": "developer", "missing": "operator"}
ROLE_NAMES = ("operator", "developer", "manager", "reviewer", "model", "search", "compiled", "guest")
ACTOR_NAMES = ("alice", "bob", "bot")

# Every refusal code the in-memory session and its record validators raise
# (syberlabs/session.py, evolution.py, bindings.py, contracts.py). A code the
# kernel raises that is not here is a finding: either the code is new and
# undocumented, or an exception escaped as the wrong type.
SESSION_REFUSALS = frozenset({
    # session.py
    "invalid_origin", "origin_denied", "unknown_case", "unknown_contract", "unknown_policy",
    "unknown_proposal", "actor_mismatch", "effect_claimed", "unsupported_effect", "effect_unavailable",
    "approval_denied", "reconciliation_unavailable", "reconciliation_denied", "case_cancelled",
    "signoff_denied", "invalid_observation", "path_complete", "planner_shape", "invalid_explain",
    "forget_denied", "invalid_action", "immutable_action", "immutable_contract", "immutable_policy",
    "invalid_policy", "policy_version", "invalid_chain", "journal_corrupt",
    # evolution.py record validators
    "evolution_not_enabled", "invalid_candidate", "operator_not_permitted", "budget_exhausted",
    "unknown_candidate", "evaluation_mismatch",
    # bindings.py and contracts.py
    "missing_fact", "missing_fact_field", "invalid_contract", "input_schema",
})
EFFECT_OUTCOMES = ("effect_succeeded", "effect_rejected", "effect_unknown", "reconciled", "reconciliation_checked")


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class FlakyExecutor:
    """A local destination whose write and status answers the sequence controls.

    ``truth`` answers from what was really written. The other status modes
    stand for destinations that are unreachable, eventually consistent, or
    wrong; the kernel must stay consistent whatever they say.
    """

    settle_seconds = 60
    APPLY_MODES = ("succeed", "no_write", "crash_before_write", "crash_after_write")
    STATUS_MODES = ("truth", "absent", "not_applied", "applied", "unknown", "raise")

    def __init__(self):
        self.apply_mode = "succeed"
        self.status_mode = "truth"
        self.written: set[str] = set()

    def apply(self, case_id, args, key):
        if self.apply_mode == "no_write":
            raise NoWrite(412, "expected prior state no longer holds")
        if self.apply_mode == "crash_before_write":
            raise ConnectionError("connection reset before the write")
        self.written.add(key)
        if self.apply_mode == "crash_after_write":
            raise ConnectionError("response lost after the write")
        return {"written": key}

    def status(self, case_id, args, key):
        if self.status_mode == "raise":
            raise RuntimeError("status endpoint down")
        if self.status_mode == "truth":
            return ("applied", {"external_id": "ext-" + key}) if key in self.written else ("absent", {})
        if self.status_mode == "applied":
            return "applied", {"external_id": "ext-" + key}
        if self.status_mode in ("absent", "not_applied"):
            return self.status_mode, {}
        return "indeterminate", {}


# ---------------------------------------------------------------------------
# Operations. Each is a tuple whose first element names a Driver method; the
# rest are drawn values. ``valid`` shrinks toward True, so a counterexample
# keeps only the adversarial steps it needs. References to earlier proposals
# or candidates are indices taken modulo what exists.

valid = st.integers(min_value=0, max_value=9).map(lambda n: n < 8)
index = st.integers(min_value=0, max_value=7)
actor = st.sampled_from(ACTOR_NAMES)
roles = st.lists(st.sampled_from(ROLE_NAMES), max_size=3, unique=True)
amount = st.one_of(
    st.integers(min_value=0, max_value=70),
    st.integers(min_value=-5, max_value=150),
    st.floats(),  # NaN and infinities included: limits.amount owns them
    st.just("ten"),
    st.none(),
)
# Candidate changed_paths: in scope, too many files, and outside the scope.
candidate_paths = st.sampled_from((
    ("src/a.py",), ("src/a.py",), ("src/a.py", "src/b.py"), ("src/a.py", "src/b.py", "src/c.py"), ("src/a.py", "setup.py"),
))
seconds = st.one_of(st.integers(min_value=0, max_value=30), st.integers(min_value=0, max_value=700))

OPERATIONS = st.one_of(
    st.tuples(st.just("propose"), st.sampled_from(ACTION_NAMES + ("missing",)), valid, actor, roles,
              st.sampled_from(("human", "model", "compiled", "alien")), st.tuples(index, st.booleans(), st.booleans(), amount)),
    st.tuples(st.just("propose"), st.sampled_from(ACTION_NAMES), valid, actor, roles,
              st.sampled_from(("human", "model", "compiled", "alien")), st.tuples(index, st.booleans(), st.booleans(), amount)),
    st.tuples(st.just("compiled"), valid, actor, roles),
    st.tuples(st.just("commit"), index, valid, actor),
    st.tuples(st.just("commit"), index, valid, actor),
    st.tuples(st.just("approve"), index, valid, actor, roles),
    st.tuples(st.just("reconcile"), index, valid, actor, roles),
    st.tuples(st.just("observe"), valid, st.sampled_from(("build", "other")), st.sampled_from(("ci", "untrusted")),
              st.booleans(), index),
    st.tuples(st.just("candidate"), candidate_paths, st.booleans(), st.integers(min_value=0, max_value=2000), st.booleans()),
    st.tuples(st.just("evaluate"), index, valid, st.sampled_from(CHECK_STATES),
              st.one_of(st.none(), st.sampled_from(CHECK_STATES)), st.booleans()),
    st.tuples(st.just("advance"), seconds),
    st.tuples(st.just("executor"), st.sampled_from(FlakyExecutor.APPLY_MODES), st.sampled_from(FlakyExecutor.STATUS_MODES)),
    st.tuples(st.just("signoff"), actor, st.sampled_from(("manager", "operator")), roles),
    # Flows expand into the primitive operations above, so deep states (a
    # shipped effect, a promoted candidate) are reached often enough to matter.
    st.tuples(st.just("flow_review"), actor),
    st.tuples(st.just("flow_ship"), actor, index),
    st.tuples(st.just("flow_promote"), actor, candidate_paths, st.booleans()),
    st.tuples(st.just("flow_promote_failed"), actor, st.sampled_from(CHECK_STATES[1:]), st.booleans()),
    st.tuples(st.just("flow_promote_stale"), actor, st.integers(min_value=250, max_value=400)),
    st.tuples(st.just("flow_promote_automation"), actor, st.sampled_from(sorted(AUTOMATION_ROLES)), st.booleans()),
    st.tuples(st.just("flow_ship_interrupted"), actor, index, st.sampled_from(FlakyExecutor.APPLY_MODES[1:]),
              st.sampled_from(FlakyExecutor.STATUS_MODES), st.integers(min_value=0, max_value=120)),
)
SEQUENCES = st.lists(OPERATIONS, min_size=3, max_size=MAX_OPERATIONS)

FLOWS = {
    # A candidate whose provider claims success while the host saw a check fail.
    "flow_promote_failed": lambda who, state, first: [
        ("candidate", ("src/a.py",), True, 100, False),
        ("evaluate", -1, False, state if first else "passed", "passed" if first else state, False),
        ("propose", "promote", True, who, [], "human", (0, True, True, 0)),
        ("commit", -1, True, who),
    ],
    # A passing evaluation that is too old by the time promotion is proposed or committed.
    "flow_promote_stale": lambda who, wait: [
        ("candidate", ("src/a.py",), True, 100, False),
        ("evaluate", -1, True, "passed", "passed", False),
        ("advance", wait),
        ("propose", "promote", True, who, [], "human", (0, True, True, 0)),
        ("advance", wait),
        ("commit", -1, True, who),
    ],
    # An automated proposer, or a human actor also holding an automation role.
    "flow_promote_automation": lambda who, role, as_human: [
        ("candidate", ("src/a.py",), True, 100, False),
        ("evaluate", -1, True, "passed", "passed", False),
        ("propose", "promote", False, who, ["developer", role] if as_human else [role], "human" if as_human else role,
         (0, True, True, 0)),
        ("commit", -1, True, who),
    ],
    # A ship whose write is refused, lost, or lost after writing; then reconcile before and after the settle window.
    "flow_ship_interrupted": lambda who, k, apply_mode, status_mode, wait: [
        ("propose", "review", True, who, [], "human", (0, True, True, None)),
        ("commit", -1, True, who),
        ("executor", apply_mode, status_mode),
        ("observe", True, "build", "ci", True, k),
        ("propose", "ship", True, who, [], "human", (k, True, True, 0)),
        ("approve", -1, True, who, []),
        ("commit", -1, True, who),
        ("reconcile", -1, True, who, []),
        ("advance", wait),
        ("reconcile", -1, True, who, []),
        ("executor", "succeed", "truth"),
        ("propose", "ship", True, who, [], "human", (k, True, True, 0)),
    ],
    "flow_review": lambda who: [
        ("propose", "review", True, who, [], "human", (0, True, True, None)),
        ("commit", -1, True, who),
    ],
    "flow_ship": lambda who, k: [
        ("propose", "review", True, who, [], "human", (0, True, True, None)),
        ("commit", -1, True, who),
        ("observe", True, "build", "ci", True, k),
        ("propose", "ship", True, who, [], "human", (k, True, True, 0)),
        ("approve", -1, True, who, []),
        ("commit", -1, True, who),
    ],
    "flow_promote": lambda who, paths, claims: [
        ("candidate", paths, claims, 100, False),
        ("evaluate", -1, True, "passed", "passed", False),
        ("propose", "promote", True, who, [], "human", (0, True, True, 0)),
        ("commit", -1, True, who),
    ],
}


class Driver:
    """Interpret an operation sequence against a fresh session; keep what the kernel refused."""

    def __init__(self):
        self.clock = Clock()
        self.session = Session(clock=self.clock)
        self.executor = FlakyExecutor()
        self.session.install_contract(CONTRACT)
        self.session.install_policy(POLICY)
        for name, doc in ACTIONS.items():
            self.session.install_action(name, doc)
        self.session.bind_effect("ship", self.executor)
        self.case = self.session.create_case(CONTRACT["id"], 1, {"objective": "keep the kernel honest"}, "alice")
        self.proposals: list[dict] = []
        self.candidates: list[dict] = []
        self.registered = 0
        self.refusals: list[tuple[str, str]] = []

    def run(self, operations) -> "Driver":
        for operation in operations:
            name, *drawn = operation
            steps = FLOWS[name](*drawn) if name in FLOWS else [operation]
            for step in steps:
                name, *drawn = step
                try:
                    getattr(self, "op_" + name)(*drawn)
                except Rejected as exc:
                    self.refusals.append((name, exc.code))
        return self

    def history(self) -> list[dict]:
        return self.session.history(self.case)

    def _pick(self, items, k):
        return items[k % len(items)] if items else None

    def _latest_build(self):
        for event in reversed(self.history()):
            if event["kind"] == "observed" and event["body"]["key"] == "build":
                return event["body"]["value"]
        return None

    def _args(self, action, well_formed, spec):
        k, match_a, match_b, value = spec
        if well_formed:
            match_a = match_b = True
            if action == "pay":
                value = k * 10
            elif action in ("ship", "promote") and value is None:
                value = 0
        if action == "pay":
            return {} if value is None else {"amount": value}
        if action == "ship":
            fact = self._latest_build()
            if value is None:
                return {}
            commit = fact["commit"] if match_a and isinstance(fact, dict) else oid(k + 1)
            return {"commit": commit}
        if action == "promote":
            candidate = self._pick(self.candidates, -1 if well_formed else k)
            args = {
                "candidate": candidate["id"] if candidate else "c-none",
                "commit": candidate["commit"] if candidate and match_a else oid(999),
                "base": BASE if match_b else oid(998),
            }
            if value is None:
                del args["base"]
            return args
        if action == "review" and not match_b:
            return []  # not an object: the proposal.shape rule owns this
        return {}

    # operations ------------------------------------------------------------

    def op_propose(self, action, well_formed, who, held, origin, spec):
        if well_formed:
            held, origin = [PROPOSER_ROLE[action]], "human"
        result = self.session.propose(self.case, action, self._args(action, well_formed, spec), who, list(held), origin)
        self.proposals.append(result["proposal"])

    def op_compiled(self, well_formed, who, held):
        if well_formed:
            held = ["operator", "compiled"]
        result = self.session.compiled_propose(self.case, who, list(held))
        self.proposals.append(result["proposal"])

    def op_commit(self, k, same_actor, who):
        proposal = self._pick(self.proposals, k)
        if proposal is None:
            self.session.commit(self.case, "no-such-proposal", who)
            return
        self.session.commit(self.case, proposal["id"], proposal["actor"] if same_actor else who)

    def op_approve(self, k, well_formed, who, held):
        proposal = self._pick(self.proposals, k)
        if proposal is None:
            self.session.approve(self.case, "no-such-proposal", who, list(held))
            return
        if well_formed:
            who = next(name for name in ACTOR_NAMES if name != proposal["actor"])
            held = ["manager"]
        self.session.approve(self.case, proposal["id"], who, list(held))

    def op_reconcile(self, k, well_formed, who, held):
        proposal = self._pick(self.proposals, k)
        if well_formed:
            held = ["manager"]
        self.session.reconcile(self.case, proposal["id"] if proposal else "no-such-proposal", who, list(held))

    def op_observe(self, well_formed, key, source, verified, k):
        if well_formed:
            key, source, verified = "build", "ci", True
        value = {"commit": oid(k + 1)} if key == "build" else k
        self.session.observe(self.case, key, value, source, "v1", "alice", verified)

    def op_candidate(self, paths, claims, size, with_parent):
        self.registered += 1
        n = self.registered
        parent = self.candidates[-1]["id"] if with_parent and self.candidates else None
        record = {
            "id": f"c{n}", "commit": oid(100 + n), "tree": oid(200 + n), "base": BASE,
            "parents": [parent] if parent else [], "operator": "patch",
            "provider": {"name": "fixture", "revision": "1"}, "changed_paths": list(paths),
            "diff": {"digest": DIGEST, "bytes": size, "files": len(paths)},
            # A provider's claim. Admission must never read it.
            "signal": {"tests": "passed", "lint": "passed", "score": 1.0} if claims else None,
        }
        event = self.session.record_candidate(self.case, record, "host")
        self.candidates.append(event["body"])

    def op_evaluate(self, k, well_formed, tests, lint, wrong_commit):
        candidate = self._pick(self.candidates, -1 if well_formed else k)
        if well_formed:
            tests, lint, wrong_commit = "passed", "passed", False
        checks = [self._check("tests", tests)]
        if lint is not None:
            checks.append(self._check("lint", lint))
        self.session.record_evaluation(self.case, {
            "candidate": candidate["id"] if candidate else "c-none",
            "commit": oid(555) if wrong_commit or candidate is None else candidate["commit"],
            "tree": candidate["tree"] if candidate else oid(556),
            "evaluator": "host.checks/1", "checks": checks,
        }, "host")

    @staticmethod
    def _check(name, state):
        return {"name": name, "state": state, "exit_code": 0 if state == "passed" else 1, "duration_ms": 1,
                "output_digest": DIGEST, "output_tail": ""}

    def op_advance(self, elapsed):
        self.clock.now += elapsed

    def op_executor(self, apply_mode, status_mode):
        self.executor.apply_mode = apply_mode
        self.executor.status_mode = status_mode

    def op_signoff(self, who, role, held):
        self.session.signoff(self.case, who, list(held), role)


# ---------------------------------------------------------------------------
# Helpers over a recorded history.

def proposal_of(history, proposal_id):
    return next(e["body"] for e in history if e["kind"] == "proposed" and e["body"]["id"] == proposal_id)


def completed(event) -> bool:
    return event["kind"] == "effect_succeeded" or verified_reconciliation(event)


def reason_owner(reason: str) -> list[str]:
    """Rules whose declared reasons match this reason; a trailing ':' marks a prefix."""
    owners = []
    for name, fn in RULES:
        for declared in fn.reasons:
            if declared == reason or (declared.endswith(":") and reason.startswith(declared)):
                owners.append(name)
    return owners


def promotable_in(prefix, proposal, now) -> str | None:
    """Why the candidate named by ``proposal`` could not be promoted at ``now``, or None.

    Computed from the history alone, independently of syberlabs.evolution.promotion_denial.
    """
    if proposal["origin"] != "human" or AUTOMATION_ROLES.intersection(proposal["roles"]):
        return "automation may not promote"
    if not set(EVOLUTION["promotion"]["roles"]).intersection(proposal["roles"]):
        return "no promotion role"
    args = proposal["args"]
    candidate = registered(prefix, args.get("candidate")) if isinstance(args, dict) else None
    if candidate is None:
        return "candidate unknown"
    if candidate["commit"] != args.get("commit") or candidate["base"] != args.get("base"):
        return "candidate mismatch"
    if candidate["scope_violations"] or candidate["limit_violations"]:
        return "candidate out of scope"
    evaluation = latest_evaluation(prefix, candidate)
    if evaluation is None:
        return "not evaluated by the host"
    states = {item["name"]: item["state"] for item in evaluation["body"]["checks"]}
    for name in EVOLUTION["evaluation"]["required"]:
        if states.get(name) != "passed":
            return f"required check {name} is {states.get(name, 'missing')}"
    if as_seconds(now) - as_seconds(evaluation["at"]) > EVOLUTION["evaluation"]["max_age_seconds"]:
        return "evidence stale"
    return None


class KernelProperties(unittest.TestCase):
    """Six invariants of the admission kernel, each over random operation sequences."""

    @PROPERTY
    @given(SEQUENCES)
    def test_chain_verifies_after_any_sequence(self, operations):
        """(1) verify_events(history) holds after any sequence; the side channel still matches."""
        driver = Driver().run(operations)
        history = driver.history()
        self.assertTrue(verify_events(history), "event hash chain broke")
        self.assertTrue(driver.session.verify_chain(driver.case), "JCS side digest no longer matches an event")
        self.assertEqual([e["seq"] for e in history], list(range(1, len(history) + 1)), "seq is not contiguous")

    @PROPERTY
    @given(SEQUENCES)
    def test_an_effect_is_admitted_once(self, operations):
        """(2) a completed action is never admitted again, and a proposal claims an effect at most once."""
        driver = Driver().run(operations)
        history = driver.history()
        for position, event in enumerate(history):
            if event["kind"] == "decision" and event["body"]["status"] == "allowed":
                action = proposal_of(history, event["body"]["proposal_id"])["action"]
                earlier = [e["seq"] for e in history[:position] if completed(e) and e["body"]["action"] == action]
                self.assertEqual(earlier, [], f"{action} was admitted at seq {event['seq']} after it completed at {earlier}")
        claims = Counter(e["body"]["proposal_id"] for e in history if e["kind"] == "effect_started")
        self.assertTrue(all(count == 1 for count in claims.values()), f"a proposal claimed twice: {claims}")
        completions = Counter(e["body"]["action"] for e in history if completed(e))
        self.assertTrue(all(count == 1 for count in completions.values()), f"an action completed twice: {completions}")
        for name, code in driver.refusals:
            if name == "commit":
                self.assertIn(code, ("unknown_proposal", "actor_mismatch", "effect_claimed"))

    @PROPERTY
    @given(SEQUENCES)
    def test_only_a_host_passed_candidate_is_promotable(self, operations):
        """(3) promote is admitted or claimed only when the host's newest evaluation passed every required check.

        Independent of operation order and of the provider's signal: candidate
        records may carry a signal claiming both checks passed.
        """
        driver = Driver().run(operations)
        history = driver.history()
        for position, event in enumerate(history):
            kind, body = event["kind"], event["body"]
            if kind == "decision" and body["status"] == "allowed" or kind == "effect_started":
                proposal = proposal_of(history, body["proposal_id"])
            else:
                continue
            if proposal["action"] != "promote":
                continue
            denial = promotable_in(history[:position], proposal, event["at"])
            self.assertIsNone(denial, f"promote {kind} at seq {event['seq']} although: {denial}")

    @PROPERTY
    @given(SEQUENCES)
    def test_every_outcome_follows_its_claim(self, operations):
        """(4) no effect outcome exists without an earlier effect_started for the same proposal and action."""
        driver = Driver().run(operations)
        history = driver.history()
        claims = {}
        for event in history:
            body = event["body"]
            if event["kind"] == "effect_started":
                claims[body["proposal_id"]] = event
                self.assertEqual(body["action"], proposal_of(history, body["proposal_id"])["action"])
                self.assertEqual(body["idempotency_key"], body["proposal_id"])
            elif event["kind"] in EFFECT_OUTCOMES:
                claim = claims.get(body["proposal_id"])
                self.assertIsNotNone(claim, f"{event['kind']} at seq {event['seq']} has no prior effect_started")
                self.assertLess(claim["seq"], event["seq"])
                self.assertEqual(body["action"], claim["body"]["action"])
                if event["kind"] in ("effect_succeeded", "effect_rejected"):
                    self.assertEqual(body["claim_hash"], claim["hash"], "outcome does not name its claim")

    @PROPERTY
    @given(SEQUENCES)
    def test_every_refusal_carries_a_documented_reason(self, operations):
        """(5) each decision reason belongs to exactly one rule, that rule is the recorded deciding rule,
        and every Rejected the session raised carries a documented code."""
        driver = Driver().run(operations)
        history = driver.history()
        deciding = {row["seq"]: row["rule"] for row in driver.session.side_channel(driver.case)}
        for event in history:
            if event["kind"] != "decision":
                continue
            body = event["body"]
            owners = reason_owner(body["reason"])
            self.assertEqual(len(owners), 1, f"reason {body['reason']!r} is owned by {owners}")
            self.assertEqual(deciding[event["seq"]], owners[0], f"side channel names another rule at seq {event['seq']}")
            self.assertIn(body["status"], ("allowed", "denied", "needs_approval"))
            self.assertEqual(body["status"] == "allowed", body["reason"] == "all_checks_passed")
            self.assertEqual(body["status"] == "needs_approval", body["reason"].startswith("approval_required:"))
        for name, code in driver.refusals:
            self.assertIn(code, SESSION_REFUSALS, f"{name} raised an undocumented code {code!r}")

    @PROPERTY
    @given(SEQUENCES)
    def test_replay_reproduces_every_decision(self, operations):
        """(6) replaying the history under the same contract and policy changes no decision,
        and explaining each proposal as recorded returns the stored decision."""
        driver = Driver().run(operations)
        history = driver.history()
        replay = driver.session.replay(driver.case, 1, 1)
        self.assertEqual(replay["changed_decisions"], [])
        for event in history:
            if event["kind"] != "proposed":
                continue
            proposal_id = event["body"]["id"]
            stored = next(e["body"] for e in history
                          if e["kind"] == "decision" and e["body"]["proposal_id"] == proposal_id and "phase" not in e["body"])
            explained = driver.session.explain_admission(driver.case, proposal_id, when="recorded")
            self.assertEqual((explained["status"], explained["reason"]), (stored["status"], stored["reason"]))
        self.assertEqual(driver.history(), history, "replay or explain appended an event")


if __name__ == "__main__":
    unittest.main()
