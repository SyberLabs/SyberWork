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

1. **Discovery, in a child.** The runner discovers the suite exactly as
   `python -m unittest discover -s tests` does: a fresh `unittest.TestLoader()`
   (what `unittest.defaultTestLoader` is in a process that has not used it
   yet), start directory `tests`, pattern `test*.py`, with the working directory
   first on `sys.path`. (`--start`, `--pattern` and `--top-level` mirror
   discover's `-s`, `-p` and `-t`.) A module that cannot be imported is a test
   that errors, as under discover. Discovery imports every test module, so it
   runs in a child process, never in the runner itself: a module that exits or
   crashes at import ends that child, and the run fails with
   `discovery_failed` instead of ending with whatever exit code the module
   chose. The child reports, for each test, its id, class, method, the file
   that defines it and its fixture group.
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
   Each shard is a subprocess (`python scripts/run_shards.py --child`) that
   discovers the suite again, keeps its assigned ids in discovery order, and
   runs them with `unittest.TextTestRunner` (warnings `default`, as
   `python -m unittest` sets). Its output, one line per test, goes to its own
   log, `shard-<k>.log`, in a new temporary directory or `--log-dir`.
4. **The result channel.** A child reports to the runner over a pipe it
   inherits as an extra handle (`pass_fds` on POSIX, a `STARTUPINFO` handle
   list on Windows), never over its stdout or stderr, which are the log. The
   runner hands the child its spec on stdin, with the pipe's handle and a
   random 128-bit nonce; the child opens the pipe, makes it non-inheritable
   (a process a test starts does not get it) and only then imports a test
   module. Every message is one JSON line carrying the nonce and a sequence
   number. A shard sends `start` when a test starts, `outcome` for each
   success, failure, error, skip, expected failure, unexpected success and
   failed subtest, `stop` when it stops, `fixture` for a class or module
   fixture that skipped or failed, and `done` with the totals and the
   tracebacks. The runner counts from the stream, not from `done`: `done`
   must agree with the stream, every assigned test must have started (or be
   held by a fixture, below), starts and stops must pair, and nothing may
   follow `done`. A line without this child's nonce or out of sequence
   discards the whole stream (`channel_tampered`); a stream without `done` is
   `shard_crashed`. There are no result files.
5. **Isolation.** Each shard, and each discovery child, gets its own scratch
   `TMP`, `TEMP` and `TMPDIR` and its own `HOME` and `USERPROFILE`, in a
   temporary directory apart from the logs, removed after the run (the logs
   are kept). So that the scratch home changes where tests write and not what
   they read, a child keeps reading the parent's Git global configuration
   (`GIT_CONFIG_GLOBAL` names the parent's `~/.gitconfig` when it exists and
   the variable is unset) and the parent's Python user site
   (`PYTHONUSERBASE`). Everything else in the environment is inherited.
6. **Summary.** The runner prints one line per shard as it finishes, the
   failures and errors of every shard in `unittest`'s format, and ends with
   the lines `unittest` prints: `Ran N tests in Xs`, a blank line, then
   `OK (...)` or `FAILED (...)`. N counts exactly what a serial run counts.
   The exit code is 0 only for `OK`. `--report FILE` writes the plan and every
   shard's result as JSON.

### The completeness guarantee

A test may never be dropped silently. The runner checks, and fails the run
otherwise:

- **Before running:** the union of the shards' ids equals the discovered set,
  and no id is in two shards. No discovered test may take a method that
  starts or reports it (`run`, `__call__`, `debug`, `id`, `_callTestMethod`,
  `_callSetUp`, `_callTearDown`, `_callCleanup`) from a class outside the
  `unittest` package or from the instance, and its `id()` must be
  `module.Class.method` (`harness_override`). Such a test could report a run
  that did not happen, or take another test's id.
- **While running:** each shard records the functions that start, run and
  report a test (those methods of `TestCase`, the suite's `run` and fixture
  handling, the result's `startTest` to `stop`, its own channel) before it
  imports a test module, and reports a test that starts after any of them
  changed (`harness_changed`): a test that patched `TestCase._callTestMethod`
  would otherwise have every later test pass without running its body.
- **After running:** every shard's stream is intact and ends with `done`; it
  started every id it was assigned, as many times as discovery found it,
  except a test that `unittest` itself never starts because its class's
  `setUpClass` or its module's `setUpModule` skipped or failed, which the
  stream names; it started nothing it was not assigned. Across shards, tests
  started plus tests held back by a fixture equals the discovered count.
  (`Ran N` counts started tests, skipped ones included, which is what
  `unittest` counts; the held-back tests are the only discovered tests it
  leaves out, and they are reported as one skip or one error per fixture, as
  under discover.)

A shard that exits without `done` (killed, crashed, or a test that called
`os._exit`, even with exit code 0) is `shard_crashed`, and the runner prints
the last test its stream started and the tail of its log. A shard still
running past its time budget is `shard_timeout`. The budget is
`--shard-timeout SECONDS`, or by default three times the shard's planned
seconds and at least 300 s; the runner prints it beside the plan. On the CI
runners of 2026-10-26 the slowest shard took 1.9 times its plan (Windows,
533 s against 286 s planned). The runner stops a timed-out shard with every
process it started. On Windows it runs `taskkill /T /F`. On POSIX it sends
each process of the tree SIGSTOP until a `ps` listing finds nothing new, then
SIGKILL, and the shards stay in the runner's session, so a host that ends
that session still ends them. It then prints the last test the stream
started and the tail of the log. Discovery has the same budget (the given
one, or 300 s), and a discovery past it is `discovery_failed`. Before this
budget a sleeping test was bounded only by the host check's own timeout,
which records `timed_out` and names no test. Any gap in the
accounting is `shard_incomplete`. Either one, any failure, error or
unexpected success, or a shard exit code that disagrees with its stream, ends
the run with `FAILED`. `tests/test_run_shards.py` proves each of these on a
small synthetic suite, and that the `Ran` and `OK`/`FAILED` lines equal a
serial `unittest discover` of the same suite.

### The floor: the test set may not shrink unless the diff says so

`--floor BASE`, or `SYBERLABS_BASE` in the environment (the Build Thread host
sets it for every check to the thread's base commit, `syberlabs/checks.py`),
adds three checks before anything runs. Each failure names its reason and
ends the run with `Ran 0 tests` and `FAILED`.

1. **The working tree is the committed tree** (`worktree_changed`). Checks run
   in name order in one worktree, and `conformance` and `scenarios` run
   candidate code before `tests` does, so they could write files the tests
   then import. The runner asks Git and refuses a tracked file that differs
   from `HEAD`, any untracked or ignored file under the start directory
   (`__pycache__` included: Python loads an unchecked-hash `.pyc` whatever its
   `.py` says), and any untracked or ignored file anywhere that Python could
   import (`.py`, `.pyc`, `.pyd`, `.pth`, `.so`, `.dll`), alone or inside an
   untracked or ignored directory. An ignored directory that holds none (the
   `*.egg-info/` that `pip install -e .` leaves) is not a change. The host's checks run
   with `PYTHONDONTWRITEBYTECODE=1`, so a fresh worktree after `conformance`,
   the critic and `scenarios` has none of these (checked on this
   repository). In a developer checkout with `__pycache__` directories the
   floor refuses; run without it there.
2. **The base's tests.** The runner reads the whole tree of BASE from Git's
   object database (`git archive`, extracted to a scratch directory; the
   repository and its worktrees are not touched) and discovers it in a child,
   exactly as it discovers the candidate. That runs the base's test modules'
   import code: the operator's own code at the base commit, which the host
   already trusts. If BASE is not a commit here, or its discovery fails, the
   run fails with `floor_unavailable`: a floor the host asked for and cannot
   build is not skipped.
3. **No base test is missing** (`test_set_shrank`). A test discovered on BASE
   and not on HEAD is excused only when the diff `BASE..HEAD` (renames
   detected) says it went:
   - the file that defines its class, or the file that defines its test
     method, is deleted or renamed;
   - a line `def <method>(` (or `async def`) is removed from either of those
     files; or
   - a line `class <Name>` for its class is removed from its module's file,
     and HEAD has a test of the same module and method name that BASE did not
     (a class renamed in the diff); each new test excuses one missing test.

   Tests whose class lives in `unittest` itself (a module that failed to
   import, or skipped at import, on the base) are not part of the floor. The
   rule is conservative: a test that disappears because a conditional deletes
   it (`del Keep.test_b`), because its class stops being a `TestCase`, or
   because an assignment hides it, is refused, since the diff shows none of
   the three. So is a test lost by deleting a package's `__init__.py`. A test
   that is still discovered but skipped is not missing: the floor counts the
   set, not what runs green, and a new `@skip` is for the critic and the
   reviewer to see in the diff.

Discovery of the base and of the candidate run in the same environment, so a
test set that depends on the environment (a dependency that is not
installed, the platform) is the same on both sides and does not trip the
floor.

**On CI.** The `Unit tests` step of the `test` matrix and of the `windows`
job (`.github/workflows/ci.yml`) sets `SYBERLABS_BASE` on a `pull_request`
event. It fetches the target branch and the tested commit 100 commits deep,
and sets the base to `git merge-base origin/<target> HEAD`. On the merge
commit GitHub checks out, that is the target branch's tip it was merged
onto. The step prints `floor base: <sha>`, and the runner's first line ends
`floor: all N tests discovered on the base are here or excused by the diff`.
A merge base it cannot compute fails the step. A `push` (to `main`, or to a
scratch branch) runs without a floor: it has no base to compare with. CI runs
the candidate's own runner, so a pull request that edits
`scripts/run_shards.py` is floored by its own copy. The host check, which
runs the host checkout's runner, does not have that weakness. The `g8-suite`
job is unchanged.

### Test integrity: what is caught, by what, and what is not

In the factory, test code is candidate code written by a model, and it runs
in the process whose report judges it. Three layers stand between a candidate
and a `tests` check that passes with a test unrun: the runner (discovery in a
child, the channel, the harness checks), the floor, and the reference
critic's test-integrity rule (`docs/CRITIC.md`), whose high finding refuses
admission through `critic.no_high_finding`. The adversarial suite of the
independent verification of #114, extended with the rows below, ran through
`run_shards.py --shards 2` before (`main` at `be1660f`) and after this
change, and the critic's rule ran over every file of every row. Before, five
rows exited 0 with a test unrun; after, every one exits non-zero or is a high
critic finding, and the honest rows still pass:

| Row | Before | After: runner | Critic |
|---|---|---|---|
| Test module calls `os._exit(0)` at import | **0, nothing ran** | 1, `discovery_failed` | high (`exit_at_import`, `process_exit`) |
| Test forges its shard's result file, then `os._exit(0)` | **0** | 1 (there is no result file; the test errors) | high (`process_exit`) |
| Test forges another shard's result; that shard dies | **0** | 1, `shard_crashed` | high (`process_exit`) |
| `run()` overridden to fake `startTest`/`addSuccess` | **0** | 1, `harness_override` | high (`harness_override`) |
| A test patches `TestCase._callTestMethod` at run time (new) | **0** | 1, `harness_changed` | high (`unittest_patch`) |
| A test writes a line without the nonce to the channel (new) | n/a | 1, `channel_tampered` | high (`runner_reach`) |
| A test finds the runner in its own process and writes a consistent, false stream (new) | n/a | **0: not caught** | high (`runner_reach`, `process_exit`) |
| The same forgery moved verbatim into a non-test module that the test imports and calls (verification of #116) | n/a | **0: not caught** | before: none; now high (`runner_reach`, 4 lines of the helper) |
| A test that sleeps (verification of #116) | host check `timed_out` | 1, `shard_timeout`, naming the test | none |
| A module exits at import only in the shard's environment (new) | 1 | 1, `shard_crashed` | high (`exit_at_import`, `process_exit`) |
| A test prints protocol lines to stdout (new) | 0, all ran | 0, all ran (the lines go to the log) | none |
| Test calls `os._exit(0)` / self-`SIGTERM` / raises `KeyboardInterrupt` | 1 | 1, `shard_crashed` | high (`process_exit`) for the first two |
| `id()` overridden to collide with a fixture class | 1 (a traceback) | 1, `harness_override` | high (`harness_override`) |
| `run()` overridden to do nothing; a test calls `result.stop()` | 1 | 1, `harness_override`; `shard_incomplete` | high (`harness_override`; `unittest_patch`) |
| `load_tests` that differs per process | 1 | 1, `shard_incomplete` | high (`load_tests`) |
| A test removed by a conditional, its `def` still in the diff (floor) | 0 | 1, `test_set_shrank` | none |
| Import error, `sys.exit(0)` in a test, fixture errors | 1 | 1 | none |
| Skips, fixture skips, `expectedFailure`, subtests, duplicate ids, a test added after the manifest | as serial | as serial | `load_tests` row only |

The guarantee, exactly: with the factory's `tests` check (the host
checkout's runner, `SYBERLABS_BASE` set) and the reference critic required, a
candidate passes `tests` with a discovered test unrun, or with a base test
gone that the diff does not delete, only if its test code, running in the
shard's own process, defeats the runner from inside (reaches the runner's
objects or memory and writes a stream consistent with the plan, or patches
something the harness check does not watch) and does it in a form the
critic's patterns do not match (for example through a name the patterns do
not list, code built at run time from data, or a compiled module already installed). Same-process
code can always do that: the nonce and the stream are in its own memory. The
critic reads every Python file the candidate adds or changes, not only test
files (`docs/CRITIC.md`), so moving the code into a module the test imports
does not hide it. Code the candidate did not change is the base's, and the
host already trusts it. Two further gaps are named in `spec/GAPS.md` 58:
- Test code can keep every test in the set but make tests vacuous: its own
  in the diff, or another's at run time by replacing its method. No count can
  see this.
- Outside test files, the patterns that end the process or write to raw
  descriptors do not apply, because production code does both.

A container or VM sandbox per shard, research register item N4, is the
eventual answer: the runner would then read the outcome from outside the
process that produced it.

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
contracts are written outside it. The factory's contract `repo-change.v5`
runs the check sharded:

```json
"tests": {"argv": ["<python>", "<host checkout>/scripts/run_shards.py"], "timeout_seconds": 3000}
```

with `<python>` the interpreter's absolute path (`sys.executable`) and the
runner named by absolute path in the host's own checkout, as `docs/CRITIC.md`
does for the critic, so that a candidate cannot edit the runner: it runs from
the host checkout. The host sets `SYBERLABS_BASE` to the thread's base
commit, so the floor above applies without a flag. The check's working
directory is the candidate's worktree, so discovery, the tests and
`tests/durations.json` are the candidate's. The candidate's test code still
runs in the shard's process; what that leaves open, and which critic rule is
the second layer, is under "Test integrity" above. Run the reference critic
(`critic.rules`) as a required check beside it. Keep the check's
`timeout_seconds` above the slowest shard's budget, so a hung shard is named
by the runner (`shard_timeout`) before the host stops the whole check.

## G8: the existing suite as one fail-closed check

`unittest` reports `OK` when a test skips, so a PostgreSQL service that never
came up, or a missing `psycopg` or `hypothesis`, leaves the suite green with
those tests unrun. Run locally without either package, the suite says
`OK (skipped=25)`, and 24 of those skips are tests that CI is meant to run.

`scripts/g8_existing_suite.py` runs the same discovery (`-s tests`) and fails
when no test ran, a test failed or errored (a module that does not import is an
error), an `expectedFailure` passed, or a test skipped for a reason other than
a missing node, npm or global playwright. Reasons match exactly, and
`tests/test_g8_existing_suite.py` checks that each allowed reason is still a
skip in the suite. It prints counts and writes them with `--report`. These are
test counts, not coverage.

```sh
PYTHONPATH=. python scripts/g8_existing_suite.py --report g8.json
```

In `ci.yml`, job `g8-suite` runs it on Linux with Python 3.12 and PostgreSQL 16,
then runs conformance. Job `g8` (check name `G8 Existing suite`) passes only
when every leg of `test`, `windows` and `g8-suite` succeeded; a failed,
cancelled or skipped leg fails it. The `windows` leg runs `scripts/run_shards.py`,
where the PostgreSQL skips are expected (it installs no driver). `compose` is not part of G8.

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
