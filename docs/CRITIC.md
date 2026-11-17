# The critic station

> Some code this page names may have been removed as dead code. `git log -S<name>` finds where it last lived.

An independent reviewer runs as a host check on every candidate, its findings are recorded in the thread's hash chain, an admission rule refuses acceptance while a high finding stands, and the pull request's `Reviewed-by` line is written from that record. The review is evidence a verifier can re-read, not a comment someone remembers.

## A critic is a check with a schema

Declare it like any other check in the contract's `evolution.evaluation.checks`, under the name `critic` or `critic.<reviewer>`, and list it in `evaluation.required` if it is to block:

```json
"evaluation": {
  "checks": {
    "tests":  {"argv": ["C:/Python312/python.exe", "-m", "unittest", "discover", "-s", "tests", "-q"]},
    "critic": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_rules.py"], "timeout_seconds": 120}
  },
  "required": ["tests", "critic"]
}
```

The name is the marker. The contract validator accepts no `kind` field on a check yet (`spec/GAPS.md` 30), so a check is a critic when its name is `critic` or starts with `critic.`; `critical` is an ordinary check. Name the critic program by an absolute path in the host's own checkout, not a path inside the candidate: the check runs in a temporary worktree of the candidate, and a candidate that could rewrite its own reviewer would review itself.

The host runs the command in the candidate's worktree with the usual scrubbed environment (plus the operator's `SYBERLABS_CHECK_ENV` allow-list, under "Credentials" below) and a JSON request on stdin:

```
{"protocol": "syberlabs.critic/1",
 "candidate": {"id", "commit", "tree", "provider", "operator", "parents", "note"},
 "base": "<thread base commit>",
 "result": [<the checks that ran before this one, with state, exit code, digest and tail>],
 "changed_paths": [...], "diff": "<unified diff against the base, up to 1 MB>",
 "objective": "...", "contract": {"acceptance": [...], "scope": {"paths": [...], "exclude": [...]}}}
```

Checks run in the order the stored contract lists them, which is by name because a published contract is kept as canonical JSON; `critic` therefore runs before `tests` and sees `result: []`, while a check named to sort before it (`a_lint`) is in `result` with its state, exit code, digest and tail. The command must print a findings document (`spec/finding.schema.json`, identity `sdk.syberlabs.space/critic/1`):

```json
{"version": 1, "reviewer": {"name": "rules", "revision": "2"},
 "findings": [{"severity": "high|medium|low", "file": "src/export.py", "line": 12,
               "claim": "what is wrong", "falsifier": "what would show the claim false", "acceptance_ref": "accepted"}],
 "summary": "optional, 4000 characters at most"}
```

Exit code 0 means the document is the result. Any other exit code is a check `error`, as for every check, with the same record: state, exit code, duration, a SHA-256 of all output, and a 4 KB tail. Nothing in the critic's prose is read by admission.

## What the host checks itself

- **The document conforms.** `syberlabs.critic.parse` refuses anything else and the check becomes `error` with output `findings_invalid:<path>` (`json`, `version`, `reviewer/revision`, `findings/3/severity`, ...). The reviewer's name is a lowercase name and its revision is printable, so neither can carry a line break into a pull request body.
- **The reviewer is independent.** The reviewer's `name@revision` must differ from the candidate's provider `name@revision` as the candidate record stores it; a match is `error` with output `reviewer_not_independent`. The host cannot see which program a provider ran, so this is checked on the recorded identities and the observation says `independence: "declared"`. A model reviewing a model: use a different revision, or a different reviewer.
- **The findings are in the chain.** After the evaluation record is written, the host stores each accepted document as an `observed` event: key `critic`, source `check:<name>`, actor `syberlabs.host`, `verified: true`, version = the check's `output_digest`, and value `{candidate, check, reviewer, counts: {high, medium, low}, findings_digest, independence, truncated, summary, findings}`. `findings_digest` is the SHA-256 of the critic's full stdout, which is the digest the evaluation record keeps for that check, so the two events name the same bytes. The value is bounded to 64 KiB: a longer document keeps its counts and digest and drops findings from the end with `truncated: true`. The check's `max_output_bytes` (1 MiB by default) still bounds what the critic may print at all.

## The rule

`critic.no_high_finding` is one admission rule, ordered after `candidate.promotable` and before `approval.required`. It is a no-op unless the promotion's contract lists a critic check in `evaluation.required`, so a contract without one, and every golden trace, is unchanged. `candidate.promotable` has already required that every required check, the critic included, passed on the candidate's exact tree. Then, for each required critic, the rule reads the host's observation for that check result (matched on candidate, check and output digest) and denies with `high_finding:<check>:<count>` while `counts.high > 0`. Medium and low findings never block. A passed critic check with no observation is denied with `critic_observation_missing:<check>`: a review that is not in the record is not evidence.

The verdict from `syberlabs check` says so (`denied: high_finding:critic:1; rule critic.no_high_finding`), `syberlabs findings c1` prints each finding as `severity file:line claim`, and the fix is a new candidate: the critic runs again on the new tree and the new evaluation carries its own observation.

Whether a medium finding should block when the thread's class is C, P or X is an open owner question; the default is no, because class enforcement is a separate decision.

## Reviewed-by

`publish.attribution` adds the critic of the promoting evaluation to the pull request body, from the observation:

```
Reviewed-by: rules@6 (critic check critic: high 0, medium 1, low 0)
```

An independent approver under an `approval_role` still renders as `actor (role)`, and both can appear on the one line, comma-separated. Every part is rendered on one line, so a reviewer name or revision cannot add a field, and the parser refuses a revision with a line break before it can be observed at all. Without either kind of reviewer the line still reads `Reviewed-by: none recorded`.

## The reference critic

`examples/critic_rules.py` is deterministic and model-free: high for a changed path outside the contract's mutable scope (the scope rule already refuses such a candidate, so this proves the plumbing), medium for a changed source file when no test changed in the same candidate, high for test code, or any other changed Python file, that can subvert the test run, high for a generated-looking credential on a line the candidate adds, and medium for a test the candidate removed, newly skipped or left asserting less than it did (all three below). Every finding names its falsifier. It lets the suite and the golden traces run without a model. The model-backed critic below prints the same document and declares a reviewer that is not the candidate's provider. Whether the rule stays blocking or becomes advisory is for E3 to measure against human review; nothing here claims precision.

### The test-integrity rule

In the factory, test code is candidate code, and it runs inside the process whose report the `tests` check reads. `scripts/run_shards.py` catches what it can from outside that process (`docs/TESTING.md`, "Test integrity"); this rule is the second layer, for what only the source shows. A line the candidate adds to a Python file under a `tests/` directory, or named `test*.py`, is a **high** finding, and so refuses admission through `critic.no_high_finding`, when it:

| Rule | What the added line does |
|---|---|
| `process_exit` | ends or kills a process: `os._exit`, `os.abort`, `os.kill` (except `os.kill(pid, 0)`, which only asks whether `pid` is alive), `os.killpg`, `signal.raise_signal`, `signal.pthread_kill`, also when reached through `from os import _exit`, `__import__("os")` or `getattr(os, "_exit")` |
| `signal_handler` | installs a signal handler or timer: `signal.signal`, `signal.alarm`, `signal.setitimer`, `signal.set_wakeup_fd`, `signal.siginterrupt`, `signal.pthread_sigmask` |
| `exit_at_import` | calls `sys.exit`, `exit`, `quit`, `os._exit` or `unittest.main`, or raises `SystemExit`, `KeyboardInterrupt`, `GeneratorExit` or `BaseException`, at module level (not under `if __name__ == "__main__":`) or in `setUpModule`, `tearDownModule`, `setUpClass` or `tearDownClass` |
| `harness_override` | defines or assigns `run`, `__call__`, `debug`, `id`, `_callTestMethod`, `_callSetUp`, `_callTearDown`, `_callCleanup`, `_addExpectedFailure`, `_addUnexpectedSuccess`, `countTestCases` or `__getattribute__`, or a method that registers or runs a test's cleanups or runs its subtests (`addCleanup`, `enterContext`, `doCleanups`, `addClassCleanup`, `enterClassContext`, `doClassCleanups`, `subTest`), in a class that derives from a name ending in `TestCase` or from `unittest`, or that defines a `test*` method |
| `load_tests` | defines or assigns `load_tests` |
| `test_replaced` | assigns, deletes or `setattr`s an attribute named `test...` on anything other than `self` (`Victim.test_a = ...`, `cls.test_a = ...`, `del type(t).test_a`), or patches one with `mock.patch("pkg.Cls.test_a")`, `patch.object(Cls, "test_a")` or `patch.multiple(Cls, test_a=...)` |
| `unittest_patch` | assigns or deletes an attribute of anything imported from `unittest`; assigns a result callback (`startTest`, `stopTest`, `addSuccess`, ..., `wasSuccessful`, `testsRun`, `shouldStop`, `stop`), a test's identity (`_testMethodName`, `__qualname__`, `__module__`) or one of the cleanup and subtest methods above on anything; uses `setattr` or `delattr` for those; patches a `unittest.*` or `sys.modules` target with `mock.patch`; reads `_outcome`, `_resultForDoCleanups` or `shouldStop` |
| `sys_modules` | writes `sys.modules['unittest...']` or `sys.modules['__main__']` (assignment, `del`, `pop`, `setdefault`, `__setitem__`), or calls `sys.modules.update` or `sys.modules.clear` |
| `runner_reach` | imports `__main__`, `gc`, `ctypes`, `msvcrt` or `_winapi`; reads `sys.modules['__main__']`; calls `gc.get_objects`, `gc.get_referrers`, `gc.get_referents`, `sys._getframe`, `sys._current_frames`, `inspect.currentframe`, `inspect.stack`, `inspect.getouterframes`, `inspect.getinnerframes`, `inspect.trace`, `os.dup`, `os.dup2`, `os.write`, `os.fdopen`, `os.set_inheritable`, `os.set_handle_inheritable`, `sys.settrace`, `sys.setprofile`, `sys.addaudithook`, `threading.settrace` or `threading.setprofile`; reads a frame attribute (`f_back`, `f_locals`, `f_globals`, `tb_frame`, `gi_frame`, `cr_frame`, `ag_frame`); or holds a string naming the shard runner's directories (`syberlabs-shard...`) |
| `dynamic_code` | calls `exec`, `eval` or `compile`; imports a computed name (`__import__`, `importlib.import_module`, `importlib.reload`), unless the name's package is fixed in the source (`f"pkg.{name}"` or `"pkg." + name`, where `pkg` is not one of the modules named in this table); or gets, sets or deletes a computed attribute of `os`, `sys`, `signal`, `unittest`, `gc`, `inspect` or `__main__` |

Why these: each is a way for test code to end the process with an exit code it chooses (the shard runner then sees a crash, but serial `unittest` sees success), to replace how a test is started or counted, to choose the test set at discovery, or to reach the runner's channel and nonce in its own memory and write a false report, which the runner cannot detect (`spec/GAPS.md` 58).

**Beyond test files.** A test can import any module, and the module's code then runs in the shard's process. So moving a forgery verbatim out of the test file and into a helper the test calls passed both layers under revision 1 (the verification of #116, finding 1). Every Python file (`.py`, `.pyw`) the candidate adds or changes is now held to the families that tamper with the harness from inside that process: `harness_override`, `unittest_patch`, `sys_modules`, `runner_reach`, `dynamic_code` and `test_replaced` (`HARNESS_RULES`). Four families stay test-scoped (`TEST_ONLY_RULES`): `process_exit`, `signal_handler`, `exit_at_import` and `load_tests`. Production code legitimately exits, kills child processes and installs handlers, and `load_tests` only means something in a module that discovery loads. Two parts of `runner_reach` also stay test-scoped:
- The write side: `os.write`, `os.dup`, `os.dup2`, `os.fdopen`, `os.set_inheritable`, `os.set_handle_inheritable`, `msvcrt` and `_winapi`. This is ordinary I/O; `syberlabs/journal.py` locks with `msvcrt`, and the runner itself uses the rest. A false report needs the channel's nonce. Without it, a write through these is `channel_tampered`. Only reading the process's memory gets the nonce (`gc`, frames, `__main__`, `ctypes`, trace and audit hooks), and that read side applies to every file.
- The runner's directory names: since results moved to the channel, those directories hold only logs.

A compiled module the candidate adds or changes (`.pyc`, `.pyd`, `.so`) is a high finding of its own: Python can import it and this rule cannot read it. A file the candidate did not change is the base's, which the operator already trusts, and is not read.

How it reads the code. The rule runs in the check's working directory, the candidate's worktree, and reads each changed Python file at the candidate commit (`git show <commit>:<path>`) and the lines the candidate added (`git diff -U0 <base> <commit> -- <path>`). Both are content-addressed, so the same request still yields the same document, and nothing in the worktree, which earlier checks may have changed, is read. It parses the file with `ast` and reports a construct only when it sits on an added line, so a pattern in a string or a comment is not code, and code already on the base is not flagged again when a nearby line changes. Names resolve through the file's imports (`import os as o`, `from unittest import TestCase as Base`). Without a repository (a request built by hand) a new file is rebuilt from the request's diff; a modified file it cannot read, a file the truncated diff does not show, and a file that does not parse are each a high finding of their own, not a pass. At most 40 integrity findings are listed; the summary counts the rest.

False positives on this repository: none. Every Python file was read as if it were added whole, test code or not. The refs were `main` at `18c31dc` and at `2b1aabb` (193 files each, 73 of them test code), this change, and the heads of every pull request open on 2026-10-26: #117 `7bbd9c8` and #110 `89c2f11` (both merged that day), #115 `ad89dee` and #96 `23f9dcc`. None yields a finding. #116's head `49695ca` yields six, all the runner's `_outcome` method, which this change renames (below). `tests/test_critic.py` keeps that true for every Python file of the checkout it runs in.

What it took outside test files:
- The test-scoped parts above.
- Anchored computed imports. `syberlabs/__init__.py` resolves its lazy names with `import_module(f"syberlabs.{module}")`, and the scenario loader in `benchmarks/run_scenarios.py` uses `import_module(f"benchmarks.scenarios.{name}")`. A name whose package is fixed in the source can only import that package's submodules, and a changed submodule is read itself.
- One rename. The runner's own result class named its reporting method `_outcome`, the name of `unittest`'s internal, so it read as `unittest_patch`. It is now `_report`.
- The exemptions that make it so are deliberate: `os.kill(pid, 0)` (`tests/test_checks.py` asks whether a process is alive), `sys.exit` inside a test method (`unittest` records it as an error, and the runner counts it), writes to `sys.modules` under other names (the G2, G3 and G5 tests register the gate modules they load), helper classes with `__call__` or `run` that are not test cases, and every pattern inside a string (the runner's own tests carry their adversarial fixtures as strings).

What it does not catch:
- anything outside the listed names: another route to the same objects, such as `getattr(self, "_out" + "come")` on a test or an alias built at run time from data;
- outside test files, ending the process and raw-descriptor writes, as above;
- a test made vacuous without any of these constructs. The weakened-test rule below reports the plain ways (an assertion gone or swallowed, a skip, a test removed); it counts constructs and does not judge what an assertion is worth.

It is a pattern list, not an analysis; a candidate that knows the list can write around it. Its precision against human review is unmeasured.

The reviewer declares revision `6` (`Reviewed-by: rules@6`), or `6-block-weakened` when the check's argv carries the option described under "A weakened test". Revision 6 adds the two rules after this section and changes nothing in this one. Revision 5 added `test_replaced`: a test that replaced another test's method (in its body, at import, or in a class fixture) made the replaced test report whatever the replacement did, and revision 4 did not see it (`scripts/run_shards.py` now fails the shard too, `docs/TESTING.md`). It matches names, not objects, so it also flags a test that builds its own inner `TestCase` and replaces that one's method; `setattr(Cls, f"test_{n}", ...)` with a computed name is not matched, which is how tests are generated at import. Before that, its verdicts changed four times: with the test-integrity rule, again when the rule's scope grew to every changed Python file, again when `harness_override` came to name `_addExpectedFailure` and `_addUnexpectedSuccess`, and again when it came to name the cleanup and subtest methods. Through the first two `TestCase.run` reports an expected failure and an expected failure that passed; a test class that defines `_addUnexpectedSuccess` to call `addSuccess` turned an outcome that fails the run into a pass, and revision 2 did not see it (`scripts/run_shards.py` now refuses it too, `docs/TESTING.md`). Through the others a test runs its own cleanups and subtests; a test class that defines `subTest` to suppress what its block raises, or `doCleanups` to drop its cleanups, turned its own failure into a pass, and revision 3 did not see it, nor `self.subTest = ...` in `setUp` (`unittest_patch` now names that assignment; the runner refuses the first two and fails the shard on the third). `tests/test_publish.py` pins the line.

### A credential on an added line

A line the candidate adds to any changed file holds the shape of a credential when it has one of the key prefixes of `syberlabs/_patterns.py` (`ghp_`, `gho_`, `ghs_`, `github_pat_`, `sk-ant-`, `sk-`, `xoxb-` and its kin, `AKIA`) followed by eight or more key characters and not glued to a preceding letter or digit, or a PEM private-key header. Two severities, by whether the value looks generated:

| Severity | The added line holds |
|---|---|
| **high** | a prefix followed by twenty or more characters with both lower-case and upper-case letters; or a private-key header with forty or more base64 characters after it on the line or on the next added line |
| **low** | the shape without that: a short value, one case only, a bare header. This is what a fixture typed by hand looks like. An `AKIA` id is upper case throughout and is always low: it is an identifier, and the secret that goes with it has no prefix to match |

The claim names the shape, the file and the line. It never repeats the value: the document is stored in the thread's chain, and a finding that quoted a key would keep it there.

Why high for the first: accepting moves a branch, and a later push publishes every commit on it, so a key in a candidate is stopped here or after it has left. Why not high for every shape: this repository tests code that masks credentials, so its tests hold values in the shape of one, and a rule that refuses them all would be wrong every time it fired here (measured below). A generated key mixes the two cases by construction: of 36 random letters and digits, about six in a billion lack one of them. A fixture that does look generated costs one edit: write it from parts (`"ghp_" + "..."`), as `tests/test_critic.py` writes its own.

The added lines come from `git diff -U0 <base> <commit>` in the candidate's worktree, or from the request's diff when there is no repository. Lines the candidate removes or leaves alone are not read, so a fixture already on the base is not reported when a nearby line changes. At most 20 findings are listed; the summary counts the rest. The prefix pattern is a copy of `_patterns.SECRET`, because the critic runs alone by its path; `tests/test_critic.py::SecretAdded` keeps the two equal.

Measured on this repository, each of the 149 commits on `main`'s first-parent history from 2026-09-24 to `9abce6d` read as a candidate against its parent: 17 low findings in 7 commits, every one a fixture in a test or a spec fixture file (one is the example in `_patterns.py`'s own docstring), and no high finding. Read as if every tracked file were added whole, the checkout has no high finding either, and `tests/test_critic.py` keeps that true. That is a count of false alarms avoided. No real key was in that history, so it says nothing about how many the rule would catch.

What it does not catch: a URL with a password in it, a bearer token, a key with no listed prefix, a key in one case only, a key split over lines or encoded, anything in a binary file. It is a prefix list with a length and a case test. A scanner with a larger rule set (for secrets, for known-vulnerable dependencies, for code patterns) is declared like any other check, a command run on the candidate's exact tree; none was run for this change.

### A weakened test

The floor (`docs/TESTING.md`) counts discovered tests: it excuses a test whose `def` the diff removes, and a test that is still discovered and skipped is not missing. The integrity rule stops test code that lies about its result. Neither sees a candidate that makes the `tests` check pass honestly by asking less of it. This rule compares each changed Python test file on the base (`git show <base>:<path>`) with the candidate's (`git show <commit>:<path>`), test by test, and reports one finding per file and rule:

| Rule | What changed between the base and the candidate |
|---|---|
| `test_removed` | a test (a function named `test...` at module level or in a class) that was on the base is gone. One that kept its method name under another class, or in another changed test file, moved; one whose new name in the same file is alike enough (a `difflib` ratio of 0.6) was renamed. Both are compared with what they became, not reported as removed |
| `test_skipped` | a test has more markers in force than it had: a decorator whose last name is `skip`, `skipIf`, `skipUnless`, `skipif`, `expectedFailure` or `xfail` (`unittest.skip`, `pytest.mark.skipif`, a team's own) on it or on a class around it; `.skipTest(...)`, `pytest.skip(...)`, `pytest.xfail(...)` or `raise SkipTest` in its body, in its class's `setUp`, `asyncSetUp` or `setUpClass`, or in `setUpModule`; a class that sets `__unittest_skip__`. A new test is reported only for an unconditional skip: a test added so that "a test changed" holds, which never runs. A new test under a condition is a test, and so is a new one expected to fail: it runs, and it is how this repository writes down a known defect |
| `assertions_removed` | a test has fewer assertions that can fail it than it had. Counted: `assert`, a call of a method or function named `assert...`, `self.fail(...)`, `pytest.raises`, `pytest.warns`, `pytest.fail`. Not counted: an assertion in a `try` whose handler catches `AssertionError`, `Exception`, `BaseException` or everything and neither raises nor asserts; one under `contextlib.suppress` of those; one under `if False:`; anything after a `return` at the top of the test's body |
| `test_skipped_by_text` | a line added to a JavaScript or TypeScript test file (under `tests/`, or named `x.test.ts`, `x.spec.js`, or under `__tests__/`) holds `it.skip(`, `test.skip(`, `describe.skip(`, the same with `.only`, `.todo` or `.fixme`, or `xit(`, `xdescribe(`, `xtest(`. Matched as text: the critic parses Python only, so a marker in a string or a comment matches too |

Each is **medium** by default. Each has honest uses (a feature removed with its tests, a test that cannot run on one platform, assertions moved into a helper), a high finding has no override short of a new candidate, and a count cannot tell the honest use from the other. So the finding goes into the chain and onto the `Reviewed-by` line (`medium 1`), and the person who accepts reads it with `syberlabs findings`.

Where nobody reads before acceptance (a thread started `--unattended`), or where the operator wants weakening refused outright, add one option to the check's argv:

```json
"critic": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_rules.py", "--block-weakened-tests"]}
```

The four rules are then **high**, `critic.no_high_finding` refuses the candidate, and the reviewer declares revision `6-block-weakened`, so the `Reviewed-by` line says which reviewer ran. The option is part of the contract, which is immutable once published; a candidate cannot change it. `tests/test_critic.py::WeakenedThroughTheThread` runs both: a candidate that changes `report.rows()` and puts `@unittest.skip` on the test that would fail passes the `tests` check; without the option it is acceptable with one medium finding recorded, with it the verdict is `denied: high_finding:critic:1`, and the candidate that fixes the test instead is acceptable.

What the base already had is not reported: a test skipped on the base and still skipped, or one whose assertions were already swallowed, compares equal. A file that does not parse is the integrity rule's finding, not this one's. Without a repository only a new file can be read, so only the new-test case applies; when the base commit cannot be read, the summary says that nothing was compared.

Measured on the same 149 commits: 25 findings in 11 commits (13 `test_removed`, 10 `assertions_removed`, 2 `test_skipped`). Fifteen of the 25 are in the two commits that deleted dead code together with its tests (#156 and #166). The other ten, in nine commits: an assertion dropped from a test that was edited (three), a test removed or replaced by one with another name (five), and a test that gained a conditional skip (two: a tool that may be missing, a signal Windows lacks). Each finding states a fact about its diff, and each of these commits was a person's decision, which is the case for medium: with the option, all 11 would have been refused. Whether a finding is a weakening is not something this count can say, and no commit in that history was written to get past a check, so the count says nothing about how many of those the rule would catch.

What it does not catch: an assertion made weaker without being removed (`assertEqual(x, 5)` to `assertIsNotNone(x)`, an expected value changed to match the bug); a skip that comes from a base class or from a decorator of the candidate's own; a conditional `return`, or a loop that never runs; a helper the test calls, emptied in a file that is not test code; tests in other languages, beyond the text markers above. It counts constructs. Whether the assertions that remain still test the behavior is a reviewer's question, and the model-backed critic below is the place for it.

## Model-backed critic

`examples/critic_model.py` is the same shape as the reference critic with a model behind it: it reads the host's request on stdin, asks one Claude model for findings as structured JSON, and prints the findings document. Declare it beside or instead of `critic.rules`, by an absolute path in the host's own checkout:

```json
"critic.model": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_model.py"], "timeout_seconds": 300}
```

- **Identity and independence.** The reviewer is `{"name": "critic.model", "revision": "<model>@<effort>@1"}`, for example `critic.model@claude-sonnet-5-5@medium@1`. It equals a proposer's `name@revision` only if the two are configured identically, and then `reviewer_not_independent` fires, which is correct. The model does not choose its identity: the schema it fills has `findings` and `summary` only; `version` and `reviewer` are written by the adapter.
- **Model and effort** come from the environment of the process that runs the check, never from the request: `SYBERLABS_CRITIC_MODEL` (default `claude-sonnet-5-5`) and `SYBERLABS_CRITIC_EFFORT` (default `medium`). The call uses the official SDK, structured output under a strict schema (severity `high|medium|low`, `line` integer or null, closed objects), and the server-side refusal fallback, as `adapters/anthropic_adapter.py` does. Structured outputs accept no string-length constraints, so the caps of `spec/finding.schema.json` (claim and falsifier 2000, file 4096, summary 4000) are enforced in the adapter after parsing; a finding without a usable severity, file, claim or falsifier is dropped, not invented; at most 20 findings are kept. The host's parser remains the authority.
- **Errors are errors.** An SDK error, a refusal that survives the fallback, or an answer cut at `max_tokens` exits 2 with one line on stderr, so the host records the check as `failed` with that line in its tail, never as a pass.
- **Cost.** The adapter appends `[critic.model usage: <served model> in=<tokens> out=<tokens> cache_read=<tokens>]` to the document's `summary`, so the cost of every review is in the chain, and prints the same as `usage:{...}` on stderr when stderr is not stdout (under the host it is: `run_check` merges them and parses everything as the document, so there the adapter writes only the document). The diff sent to the model is cut at `SYBERLABS_CRITIC_MAX_DIFF_BYTES` characters (default 200000, about 50k tokens), which bounds one review at about $0.26 at Sonnet 5.5 prices ($2 / $10 per million) with the 16000-token answer cap. The one recorded call (`tests/fixtures/critic_model/sonnet55-medium-critic-md.json`, 2026-10-21, Claude Sonnet 5.5 at `medium`, reviewing this section's and the adapter's own 22 KB diff) used 9,705 input and 937 output tokens: $0.029. At the E3 pilot's sizes (a toy fixture's diff of a few kilobytes; the proposer's own calls were about 700 input tokens) a review is a few thousand input tokens, about a cent. The reviewer's `revision` names the configured model; the served model, which the fallback may change, is the one in the summary's usage note.
- **Replay.** `--replay PATH` (or `SYBERLABS_CRITIC_REPLAY=PATH`) reads a recorded response instead of calling the model; `--record PATH` writes one from a live call. `tests/test_critic_model.py` runs the adapter through the host with a replayed response and no key or network. The argument form exists because the host runs checks with a scrubbed environment (`syberlabs.checks.PASS_ENV`) that passes no `SYBERLABS_*` variable unless the operator lists it.
- **Credentials.** The scrubbed environment carries no `ANTHROPIC_API_KEY` and replaces `HOME`, so the live adapter has no credential and no `ant` profile unless the host passes one through. The host does so for critic checks only, from an allow-list the operator sets in the host's own shell: `SYBERLABS_CHECK_ENV=ANTHROPIC_API_KEY,SYBERLABS_CRITIC_MODEL,SYBERLABS_CRITIC_EFFORT`. Each named variable is passed only when it is set, only to a check named `critic` or `critic.<reviewer>`; a `tests` check never sees it. The list is host policy and never contract data: the contract cannot name a variable and a candidate cannot change the list, because it lives in the operator's environment and not in the repository or the journal, and nothing about the pass-through is recorded (no name, no value, no count; `spec/GAPS.md` 39). A critic program is then a credential holder: it must be named by an absolute path outside the candidate's scope, as this document already requires, because a critic a candidate can rewrite would hand the candidate the key. The reviewer's precision is unmeasured until the E3 addendum runs; nothing here says whether its high findings should block or advise.

## Trust boundary

The critic is a program the contract names, run with the developer's permissions against candidate code in a temporary worktree; it is not a sandbox, and neither is the test runner it backs up. Its output is a claim until the host has parsed it, checked the reviewer against the provider, and stored it in the chain; only then does admission read it, and only the counts.
