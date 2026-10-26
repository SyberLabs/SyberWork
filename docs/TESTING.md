# Testing

How the suites are run, and what the property suite states about the
admission kernel.

## The suites

```sh
PYTHONPATH=. python scripts/run_shards.py               # unit tests, sharded (what CI runs)
PYTHONPATH=. python -m unittest discover -s tests -q    # the same tests, one process
PYTHONPATH=. python -m conformance.run                   # golden traces; any difference fails
python -m conformance.clean_install                      # wheel in a fresh venv
```

Both unit commands run the same tests; the sharded one is described under
"Sharded unit suite" below.

The package has no runtime dependencies and the unit suite needs none. Two
modules, `tests/test_properties.py` and `tests/test_properties_journal.py`,
need `hypothesis`; install it with the `test` extra and they run, otherwise
each reports itself as skipped and the rest of the suite is unchanged:

```sh
python -m pip install -e ".[test]"
PYTHONPATH=. python -m unittest tests.test_properties -v
```

CI installs the extra in the `test` (3.11, 3.12, 3.13) and `windows` jobs
before the unit tests. On Windows use `python`, never `python3`.

## Sharded unit suite

`scripts/run_shards.py` (standard library only) runs the unit suite as N
parallel processes. Every test still runs on every run; sharding changes only
how the work is spread, never which tests run.

### How it works

1. **Discovery.** The runner discovers the suite exactly as
   `python -m unittest discover -s tests` does: `unittest.defaultTestLoader`,
   start directory `tests`, pattern `test*.py`, with the working directory
   first on `sys.path`. (`--start`, `--pattern` and `--top-level` mirror
   discover's `-s`, `-p` and `-t`.) A module that cannot be imported is a test
   that errors, as under discover.
2. **Packing.** Tests are packed by test id, not by module, from the timing
   manifest `tests/durations.json` (test id to seconds). A test the manifest
   does not name, for example one added after the manifest, gets the
   manifest's median; entries for tests that no longer exist are ignored.
   Packing is greedy longest-first: each test, longest first, goes to the shard
   with the least planned time (ties: fewer tests, then lower shard index), so
   the plan is deterministic. One exception keeps `unittest`'s fixture
   semantics: the tests of a class that defines `setUpClass`, or of a module
   that defines `setUpModule`, form one unit and stay in one shard, so the
   fixture is built once and its tests share it as they do in one process.
3. **Running.** N defaults to `min(os.cpu_count(), 8)`; `--shards N` sets it.
   Each shard is a subprocess (`python scripts/run_shards.py --child ...`) that
   discovers the suite again, keeps its assigned ids in discovery order, and
   runs them with `unittest.TextTestRunner` (warnings `default`, as
   `python -m unittest` sets). Its output, one line per test, goes to its own
   log, `shard-<k>.log`, in a new temporary directory or `--log-dir`.
4. **Isolation.** Each shard gets its own scratch `TMP`, `TEMP` and `TMPDIR`
   and its own `HOME` and `USERPROFILE`, removed after the run (the logs are
   kept). So that the scratch home changes where tests write and not what
   they read, a shard keeps reading the parent's Git global configuration
   (`GIT_CONFIG_GLOBAL` names the parent's `~/.gitconfig` when it exists and
   the variable is unset) and the parent's Python user site
   (`PYTHONUSERBASE`). Everything else in the environment is inherited.
5. **Summary.** The runner prints one line per shard as it finishes, the
   failures and errors of every shard in `unittest`'s format, and ends with
   the lines `unittest` prints: `Ran N tests in Xs`, a blank line, then
   `OK (...)` or `FAILED (...)`. N counts exactly what a serial run counts.
   The exit code is 0 only for `OK`. `--report FILE` writes the plan and every
   shard's result as JSON.

### The completeness guarantee

A test may never be dropped silently. The runner checks, and fails the run
otherwise:

- **Before running:** the union of the shards' ids equals the discovered set,
  and no id is in two shards.
- **After running:** every shard wrote a result file (written last, by
  rename); it discovered every id it was assigned; and it started every one of
  them, except a test that `unittest` itself never starts because its class's
  `setUpClass` or its module's `setUpModule` skipped or failed, which the
  shard names. Across shards, tests started plus tests held back by a fixture
  equals the discovered count. (`Ran N` counts started tests, skipped ones
  included, which is what `unittest` counts; the held-back tests are the only
  discovered tests it leaves out, and they are reported as one skip or one
  error per fixture, as under discover.)

A shard that exits without a result (killed, crashed, or a test that called
`os._exit`, even with exit code 0) is `shard_crashed`, and the runner prints
the last test it started and the tail of its log. Any gap in the accounting
is `shard_incomplete`. Either one, any failure, error or unexpected success,
or a shard exit code that disagrees with its result, ends the run with
`FAILED`. `tests/test_run_shards.py` proves each of these on a small
synthetic suite, and that the `Ran` and `OK`/`FAILED` lines equal a serial
`unittest discover` of the same suite.

### Refreshing `tests/durations.json`

The manifest only steers packing; a stale or missing entry costs balance,
never coverage. The committed file is one `--shards 1` run of 646 tests
(Windows 11, 16 logical CPUs, CPython 3.12.6, 2026-10-26, on `main` at
`e2e6408`), 1,078 s in total. A first manifest built from the efficiency
audit's per-test timings of the same day planned eight equal shards of 145 s,
but one shard took 255 s: the audit's timer ran from `startTest` to
`stopTest` and so left out class fixtures, and
`test_model_comparison.PolicyArms.setUpClass` alone takes about 85 s. The
measured file charges that time to the class's first test. To refresh it from
a run on your machine:

```sh
PYTHONPATH=. python scripts/run_shards.py --shards 1 --write-durations tests/durations.json
```

`--write-durations` records, per test, the time from the end of the previous
test to the end of this one, so a class fixture's cost lands on its first
test. `--shards 1` measures without contention; a run with more shards also
works and records somewhat inflated times. Commit the file in its own change.

### Measured

One Windows 11 machine (AMD Ryzen 7 3700X, 8 cores, 16 logical CPUs),
CPython 3.12.6, 646 tests, `main` at `e2e6408` plus this runner, 2026-10-26,
nothing else running; every run `OK (skipped=22)`:

| Command | Runs | Wall, median (s) | Slowest shard (s) |
|---|---|---|---|
| `python -m unittest discover -s tests -q` | 1 | 990 | - |
| `run_shards.py --shards 1` | 1 | 1,080 | 1,079 |
| `run_shards.py --shards 2` | 3 | 526 (520-534) | 525 |
| `run_shards.py --shards 4` | 3 | 291 (291-302) | 290 |
| `run_shards.py --shards 8` | 3 | 191 (187-196) | 191 |

The bound at eight shards is max(the longest unit, the sum over eight):
the `PolicyArms` class with its fixture is about 86 s and the sum is 1,078 s,
so 135 s. The runs reach 191 s: each shard ends within about 25 s of the
others, but eight shards on eight cores take about 1.33 times as long as one
process does for the same tests (the shards' times add up to about 1,430 s),
because many tests start Git and Python subprocesses of their own.

On GitHub's hosted runners (`ci` workflow, 4 CPUs each, so N is 4), the
`Unit tests` step, serial on `e2e6408` (three runs, 635 tests) against
sharded on this change (one run, 646 tests, the same skips):

| Job | Serial, median of 3 (s) | Sharded, N=4 (s) | Slowest shard (s) |
|---|---|---|---|
| `test (3.11)` | 306 | 86 | 85 |
| `test (3.12)` | 315 | 113 | 112 |
| `test (3.13)` | 340 | 84 | 82 |
| `windows` (3.12) | 1,260 (925-1,333) | 557 | 554 |

In the 3.11 run one of the runner's own tests failed (Python 3.11's shared
`defaultTestLoader` keeps state between `discover` calls in one process; the
runner now uses a fresh loader); all 646 tests ran, so the time stands.

### The `tests` check of a Build Thread contract

The Build Thread's `tests` check is a contract `argv`; this repository's
contracts are written outside it. To run the check sharded, a contract should
use:

```json
"tests": {"argv": ["<python>", "<host checkout>/scripts/run_shards.py"], "timeout_seconds": 3000}
```

with `<python>` the interpreter's absolute path (`sys.executable`) and the
runner named by absolute path in the host's own checkout, as `docs/CRITIC.md`
does for the critic, so that a candidate cannot weaken the completeness check
that judges it. The check's working directory is the candidate's worktree, so
discovery, the tests and `tests/durations.json` are the candidate's. A
contract is immutable under its identity, so the switch is a new contract
version, not an edit of `repo-change.v4`.

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
`bindings.inputs`, retention or `forget`. The journal and the Build Thread
are the next section's subject. A passing run says the six properties held
for the sequences drawn on that run; it is not a proof.

## Journal and Build Thread properties: death between appends

`tests/test_properties_journal.py` drives the same operations as the kernel
suite through `syberlabs.journal.Journal` in a temporary home, and then drives
the Build Thread on a real repository, with the process killed at drawn
points. Death is a `BaseException` raised by the journal right after an event
append is durable, or by an executor right after its destination write, so no
`except Exception` in the kernel can turn it into a recorded outcome: the
record stops where a killed process would stop. Every death is followed by a
fresh `Session` or `Kit.local` on the same home, which is what a restarted
process does.

### The journal-backed driver

`JournalDriver` is the kernel suite's `Driver` with a journal, two extra
executor modes (`die_before_write`, `die_after_write`), a `reopen` operation
that replaces the session with a new one, and a death schedule: a set of
append counters, counted from the first operation, at which the process dies.
Its `recover` is what `syberlabs recover` does at the session level: a new
process, the destination answering truthfully, every open claim reconciled
after the settle window. The tests:

- **The six invariants hold across reopens and deaths.** One drawn sequence
  with reopens and deaths, then all six checks of the kernel suite over the
  history a fresh session reads back from disk. One torn window is tolerated
  and counted: `propose` is two appends (`proposed`, `decision`), so a death
  between them leaves a proposal the kernel never decided; `commit`
  recomputes admission, so the record is incomplete but not wrong, and the
  count of such proposals may not exceed the number of deaths.
- **Recovery settles every interrupted bound effect once and truthfully.**
  After the sequence, every open claim of the executor-bound action settles
  to exactly one of verified `reconciled` or `effect_rejected`, and whenever
  the destination answered truthfully the settlement agrees with it: a claim
  whose write happened is never `not_applied`, a claim whose write did not
  happen is never verified.
- **Recovery settles an interrupted recorded effect.** `review` has no
  executor; its commit dies after the claim (`effect_started`) and before
  `effect_succeeded`. `recover` (one `reconcile` per open claim) settles the
  claim, and a later proposal of `review` is allowed. Found failing on the
  first run, see below; fixed in the kernel afterwards.
- **A torn tail is repaired or refused, never half accepted.** After a
  sequence, a partial final record with no newline (a prefix of a plausible
  next record, or random bytes) is appended to the thread file or the
  registry. Reopening either yields exactly the history before the tear and
  restores the file byte for byte, or refuses with `journal_corrupt`; it then
  keeps appending with a valid chain.
- **Two openers interleave under the lock and agree.** Two sessions on one
  home, each primitive step run by whichever the drawn turn list names; no
  `journal_changed`, the same history and side channel from both, all six
  invariants. A plain test also appends from two threads at once.

### The Build Thread driver

`Factory` makes a project in a temporary directory with a bare remote, a
contract whose check is a no-op interpreter call (the kernel's check runner is
exercised, the sample tests are not what is under test), `push_branch` as a
`git_push` action and, when asked, `measure` as an `intervention` against a
loopback `MetricSource` on a fake clock. `drive` does what an operator does at
the command line: run `status`, run the first thing it names next that is not
denied, `recover` when it reports an interrupted effect, move the clock past
a window when a claim is pending on it, and stop when the same step has come
up twice. A death inside any step reopens the kit and continues.

- **Accept and push survive a death anywhere.** The process dies after the
  n-th append counted from the end of `start` (the flow has about fourteen),
  or right after `GitRefEffect.apply` moved the target ref, or right after
  `GitPush.apply` pushed. Afterwards every claim has one settlement, the
  acceptance and the push each completed exactly once, the spec validator
  accepts every event, and the target ref, the remote branch and the record
  agree.
- **A death during start leaves a thread that reopens or refuses.** Expected
  failure, see below.
- **An intervention settles once from its first durable reading.** Expected
  failure, see below.

### What the first run found

Three properties failed on the first run. Each test was kept, marked
`expectedFailure`, with its counterexample and the kernel code believed
responsible in its docstring; none was weakened. When the kernel is fixed the
test reports an unexpected success, which is the signal to remove the mark.
Finding 2 is fixed and its mark is off; findings 1 and 3 still carry theirs.

1. **An intervention measured twice, settled the other way** (the window
   found on #35). `publish measure`, baseline 10.0, metric 12.0 after the
   window, death right after the settlement observation and before the
   `reconciled` event, metric back to 10.0, `recover`. `Intervention.status`
   reads the metric again, appends a second settlement observation (outcome
   `unchanged`) and the claim settles `effect_rejected not_applied` although
   the first durable settlement said `moved`. PR #38 (`f555ab2`) added the
   read-back of a durable settlement, but it was merged into
   `syberlabs/c323757b` three seconds after #35 was squashed into `main`, so
   the fix is on neither `main` nor this branch. The property reproduces the
   window by construction; it needs no knowledge of the review that found it.
2. **A recorded effect could not be recovered** (fixed). An action without
   an executor (`review` here) whose commit died between `effect_started` and
   `effect_succeeded`: `reconcile` refused with `reconciliation_unavailable`
   because no executor was bound, and every later proposal of the action was
   denied `effect_unresolved`, with no path out. `syberlabs/session.py`,
   `commit`'s executor-less branch and `reconcile`'s first refusal. Now, for
   an action that declares no `effect`, the record is the effect:
   `Session.reconcile` (after the usual role check and the check that the
   claim is unresolved) appends `effect_succeeded` with the same output commit
   would have written, `{"recorded": true, "args": <the proposal's args>}`,
   and `claim_hash` set to the claim's hash, and returns `status:
   "recovered"`. The event body is unchanged in shape, so the frozen schemas
   still hold; the recovery is read from the record's structure (a success
   appended by reconcile after the claim, in a later process) and from the
   return value, not from a flag. A second `reconcile` is refused with
   `reconciliation_denied` as for any settled claim. An action that declares
   an effect but has no executor bound is still refused
   `reconciliation_unavailable`: a missing binding is not a record. The
   property's assertions now also accept `effect_succeeded` as the
   settlement and expect the proposal after recovery to be denied
   `action_already_completed` (the state after any completed effect of the
   action in a case, by `effect.not_completed`), not `allowed` as first
   written; `tests/test_session.py` pins the direct case.
3. **A thread that died during `start` cannot be opened.** `start` is three
   appends (`case_created`, the `base` observation, the context). After a
   death between the first two the journal lists the thread, `syberlabs
   threads` shows it, and `Kit.open` raises `TypeError` from
   `Thread.__init__` reading the missing base, not a `Rejected` with a code.
   `syberlabs/build.py`, `Thread.__init__`.

### Budget

`SYBERLABS_JOURNAL_EXAMPLES` (default 30) sets the examples per journal
property and `SYBERLABS_THREAD_EXAMPLES` (default 8) per Build Thread
property. Every journaled append is an fsync, so journal examples cost about
a second each; a Build Thread example is a Git repository and a few
subprocesses, about four seconds. The module ran in 75 seconds on the Windows
machine that wrote it. The Build Thread tests do not shrink: the drawn values
are one crash point and a few metric values, so shrinking would only replay
the same seconds, and the failing example is printed as drawn; the known
counterexamples are pinned with `@example` so an expected failure cannot
become a lucky pass. Determinism and the absence of an example database are
as for the kernel suite.

### What it does not cover

Deaths inside the registry writes and `create_thread` (a thread file with its
case row and no events), `forget`, the `github_pull_request` and `command`
executors, and the lock on a platform with neither `fcntl` nor `msvcrt`. Two
openers are two `Session` objects in one process holding two lock handles,
not two operating-system processes.
