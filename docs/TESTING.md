# Testing

How the suites are run, and what the property suite states about the
admission kernel.

## The suites

```sh
PYTHONPATH=. python -m unittest discover -s tests -q    # unit tests, about two minutes
PYTHONPATH=. python -m conformance.run                   # golden traces; any difference fails
python -m conformance.clean_install                      # wheel in a fresh venv
```

The package has no runtime dependencies and the unit suite needs none. One
module, `tests/test_properties.py`, needs `hypothesis`; install it with the
`test` extra and the module runs, otherwise it reports itself as skipped and
the rest of the suite is unchanged:

```sh
python -m pip install -e ".[test]"
PYTHONPATH=. python -m unittest tests.test_properties -v
```

CI installs the extra in the `test` (3.11, 3.12, 3.13) and `windows` jobs
before the unit tests. On Windows use `python`, never `python3`.

## Property suite: the admission kernel over random event sequences

`tests/test_properties.py` does not script scenarios. Each test draws a random
sequence of up to sixty operations, runs it against a fresh in-memory
`syberlabs.Session` (no journal), and then checks one invariant over the whole
recorded history. Hypothesis shrinks a failing sequence to the shortest one
that still fails and prints it.

### The contract it drives

One contract, `kernel-properties@1`, with four local actions chosen to reach
most admission rules:

| action | what it exercises |
|---|---|
| `review` | the plain path: `actor.role`, `effect.not_completed` on a second proposal |
| `pay` | `limits.amount` in the contract (100) and the policy (50), including NaN, infinities, strings and a missing amount |
| `ship` | `effect.prior` (needs `review`), `facts.required` (a verified `build` fact from source `ci`, fresh within 600 s), `bindings.arguments` (`commit` bound to that fact), `approval.required` (`manager`), and a bound executor that can succeed, refuse before writing, crash before writing, or crash after writing; reconciliation runs against a destination that answers truthfully, `absent`, `not_applied`, `applied`, something indeterminate, or raises |
| `promote` | `candidate.promotable` under an evolution section: scope `src/`, at most two files and 1000 bytes, checks `tests` and `lint` both required, evidence valid for 300 s, promotion role `developer` |

The operations are proposals (any action, any roles, any origin, well-formed or
malformed arguments), compiled proposals, commits by the proposer or someone
else, approvals, reconciliations, observations (verified or not, from `ci` or
not), candidate registrations (in scope, too many files, outside scope, with
or without a provider signal that claims both checks passed), host evaluations
(any check state, a missing check, a wrong commit), clock advances up to 700
seconds, executor mode changes, and signoffs. Most drawn operations are well
formed so that sequences reach admitted effects, approvals, promotions and
reconciliations. Seven composite operations expand into the primitives above
for the same reason: `flow_review`, `flow_ship` and `flow_promote` are the
happy paths; `flow_promote_failed` (the host saw a check fail while the
provider's signal claims success), `flow_promote_stale` (a passing evaluation
that ages past `max_age_seconds` before the proposal or the commit),
`flow_promote_automation` (a `model`, `search` or `compiled` proposer, or a
human also holding that role) and `flow_ship_interrupted` (a write refused,
lost, or lost after writing, reconciled before and after the settle window,
then proposed again) are the adversarial ones. The `valid` flags shrink
toward true, so a counterexample keeps only the adversarial steps it needs.
In a sample of 300 sequences every decision reason the contract can reach
appeared, and so did every effect outcome kind, including verified
reconciliations and `effect_rejected` from both a refused write and a settled
`not_applied`.

### The six properties

Each is one test. "History" is the case's event list after the sequence.

1. **The chain verifies.** `verify_events(history)` holds, the JCS side digest
   still matches every event (`verify_chain`), and `seq` is contiguous.
2. **An effect is admitted once.** No `allowed` decision for an action follows
   an `effect_succeeded` or verified `reconciled` for that action; no proposal
   has two `effect_started` claims; no action completes twice; a refused
   commit is refused only for `unknown_proposal`, `actor_mismatch` or
   `effect_claimed`.
3. **Only a host-passed candidate is promotable.** Wherever `promote` is
   admitted or claims an effect, the history before that point contains the
   candidate, with matching commit and base, inside scope, and the host's
   newest evaluation of its exact commit and tree with every required check
   `passed` and younger than `max_age_seconds`; the proposal is human-origin
   from a promotion role. The check is computed from the history alone, not
   through `promotion_denial`, and never reads the candidate's `signal`.
4. **Every outcome follows its claim.** Every `effect_succeeded`,
   `effect_rejected`, `effect_unknown`, `reconciled` and
   `reconciliation_checked` has an earlier `effect_started` for the same
   proposal and action; succeeded and rejected outcomes carry that claim's
   hash; the claim's idempotency key is the proposal id.
5. **Every refusal carries a documented reason.** Each decision reason is
   owned by exactly one rule in `syberlabs.admission.RULES` (a declared reason
   ending in `:` owns a prefix), the side channel names that rule as the
   deciding rule, `allowed` pairs only with `all_checks_passed` and
   `needs_approval` only with `approval_required:<role>`; every `Rejected` the
   session raised carries a code from `SESSION_REFUSALS`, the set collected
   from `syberlabs/session.py`, `evolution.py`, `bindings.py` and
   `contracts.py`.
6. **Replay reproduces every decision.** `Session.replay(case, 1, 1)` reports
   no changed decision, and `explain_admission(..., when="recorded")` returns
   the stored decision for every proposal, without appending an event.

### Reading a failure

Hypothesis prints `Falsifying example: test_...(operations=[...])`: the
shortest operation list it found. Each entry is a tuple whose first element
is the operation and whose remaining elements are the drawn values in the
order the strategy lists them (`tests/test_properties.py`, `OPERATIONS`).
Rerun that exact list through `Driver().run(operations)` in a shell, then
`driver.history()` and `driver.refusals`, to see what the kernel recorded.
The assertion message names the event sequence number involved.

A failing property is a kernel defect or a wrong property, never a reason to
loosen the property. Record it: keep the test, mark it
`unittest.expectedFailure`, put the counterexample in its docstring, and fix
the kernel in its own change.

### Budget and determinism

`SYBERLABS_PROPERTY_EXAMPLES` sets the examples per property (default 150).
Each example is a few milliseconds; the module runs in under ten seconds on a
laptop. With `CI` set in the environment, as GitHub Actions does, the run is
derandomized so that a red check reproduces locally with `CI=1`; without it
every run explores new sequences. No example database is written
(`database=None`), so the suite leaves nothing in the checkout.

### What it does not cover

The in-memory session only: not `syberwork.core.Work`, its SQLite or
PostgreSQL stores, or the HTTP layer, which share the rule list but not the
session code. Not the `economic.reserve` rule, `resolution.current`,
`bindings.inputs`, retention or `forget`, or the journal. Not the Build Thread
(`syberlabs/build.py`), whose checks run real commands. A passing run says the
six properties held for the sequences drawn on that run; it is not a proof.
