# GitHits x SyberWork: verified dependency fixes

Owner: Seth Carlson. Acting agent: Codex. Class R implementation; Class P
draft meeting report. No product-performance claim is authorized by this code.
Claim ceiling: see the dated `REPORT.md` and its raw evidence for actual runs.

## Question and design

Does one fixed patch provider solve more dependency integration tasks when
given exact-version source retrieved through GitHits?

Reuse the `repair` arm in `benchmarks/model_vs_patch.py`: at most two model
calls, stopping at the first candidate with all required **host-run** checks
passing and a passing admission preview. Both arms use the same fixture base
commit, dependency pins, mutable scope, model, effort, checks, and repair rule.
The treatment adds preselected version-pinned GitHits source excerpts. Order
is shuffled with seed `20261015`. A fresh model session is used for every call.
Seed labels identify repetitions/order; the model has no deterministic seed.

This is a bounded patch-proposal experiment. The model has no shell, web,
MCP, local dependency-source, test-file, or repository-editing tools. It sees
the objective, current source and dependency manifest; repair calls see the
previous changed files and failing host output, as in the existing adapter.
The host retrieves context before the treatment. This does **not** measure
agent-driven GitHits discovery, interactive MCP use, retrieval efficiency,
or a full coding agent with unrestricted tools. It also lacks an equal-size
ordinary-documentation control, so an uplift would not isolate the value of
GitHits from the value of giving an agent additional relevant context.

Three authored integration regressions use actual installed HTTPX 0.28.1,
Pydantic 2.10.6, and SQLAlchemy 2.0.36. Their API migrations are documented by
upstream links in `fixtures.py`. They are convenience tasks, **not** sampled
upstream issues, customer failures, or a representative measure of coding.
Reference fixes are held out and must pass; each starting implementation
must fail behavior checks while passing its dependency-version check.

Checks execute through `syberlabs.checks` in detached temporary worktrees
with its scrubbed environment. Only `src/` is mutable. Test files and pins
cannot be changed. This is developer-permission execution, **not a security
sandbox**. No change is made to the core protocol, admission, or evaluator.

## Reproduce

The newest `upstream-v3` profile is registered in
[PREREGISTRATION-upstream-v3.md](PREREGISTRATION-upstream-v3.md). Install
`requirements-upstream-v3.txt` in place of the original requirements below.
Run the module `benchmarks.githits.upstream_v3` with mode `preflight` for
authenticated retrieval only, then mode `run --access ABSOLUTE_PREFLIGHT_PATH`
for the frozen-context model matrix. Both require `--python ABSOLUTE_INTERPRETER`
and `--write NEW_DIRECTORY`. Mode `verify --folder SAVED_MATRIX --write NEW_JSON`
replays saved trees without model calls or new retrieval. Do not regenerate
the committed registration. No core runtime dependency was added.

On Windows, [run-upstream-v3.ps1](run-upstream-v3.ps1) coordinates those modes.
Supply `-Python` with the pinned interpreter and keep `-OutputRoot` outside the
checkout. It uses an existing environment credential; **it never prompts by
default**. An explicit `-PromptForToken` permits one masked entry for a new
preflight. If preflight already finished and the model matrix has not started,
pass `-Access SAVED_PREFLIGHT_DIRECTORY` to verify/reuse those authenticated
receipts without a token, accounting runtime or new retrieval. If a matrix
already started, preserve its evidence and review it before any new attempt.
Completed matrices can be replayed with mode `verify`, without a token.

The launcher has six offline process-control tests in
[test-upstream-launcher.ps1](test-upstream-launcher.ps1), also run in Windows CI.
These use recording process doubles and make no API or model calls; their
passes are not dependency-benchmark outcomes.

For the **retained hard-v2** measurement/rerun, use a separate short checkout
at `9abb733846f8754386875008296361362309ecf6`. That publication contains its exact
frozen implementation and raw evidence. New upstream-v3 source files correctly
fail the older whole-source guard on a newer checkout. Original three-task
evidence remains separate, and all historical reports/registrations stay intact.

Python 3.11+, Git, the Codex CLI, and an existing model login are required.
The main packages remain free of runtime dependencies.

On Windows, enable long paths for the clone before checkout: the archived
host journals can exceed Git's default path limit in a deep workspace.
This setting applies to the new checkout only.
Keep the checkout path short as well: some Windows Python file operations
still fail at a 260-character full path even when Git can check it out.
If replay reports a missing journal file, move to a shorter checkout path;
do not alter the evidence manifest to hide the file.

```powershell
git clone --config core.longpaths=true --branch bench/githits-verified https://github.com/SyberLabs/SyberWork.git
```

In the checkout, make an isolated virtual environment and install this
benchmark's fully version-pinned extras:

```powershell
# Replace the first path with a working Python interpreter on this machine.
& 'C:\path\to\python.exe' -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r benchmarks/githits/requirements.txt
$benchPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
```

For live lookups, use the approved private MasterMind usage runtime introduced
upstream in `a1ccbdb`, with Node 24. Load `GITHITS_USAGE_NODE` and
`GITHITS_USAGE_HOME` before starting the benchmark; they are non-secret absolute
paths. On an existing Windows installation, reload the configured user values:

```powershell
$env:GITHITS_USAGE_NODE = [Environment]::GetEnvironmentVariable('GITHITS_USAGE_NODE', 'User')
$env:GITHITS_USAGE_HOME = [Environment]::GetEnvironmentVariable('GITHITS_USAGE_HOME', 'User')
```

Use `GITHITS_API_TOKEN` from the existing secret environment; never put its
value in a file, argument, transcript or commit. Each HTTP lookup runs through
the private `usage.mjs exec` wrapper; its ledger remains outside this repository.
Missing accounting configuration stops retrieval, with no direct fallback.
The accounting unit is one CLI invocation, not an API request-volume metric.
The runner sends only public package/version/path locators to
`https://api.githits.dev/v1/read`. It rejects
missing content, binary data, path mismatches, and different **served** package
versions, including a fallback to another release. It never substitutes
ordinary documentation or an authored reference patch for GitHits data.
Repository-backed responses may omit served package name/registry. In that
case the validator requires the requested/resolved package identity and an
exact match of served version, repository and full commit, with current
freshness. It does not infer identity from the requested version alone.

```powershell
# Fixture/reference checks and access preflight: no model calls.
& $benchPython -m benchmarks.githits.run --python $benchPython --validate-only --write work/githits-validation

# At most 3 tasks * 2 arms * 2 calls = 12 model invocations per repeat.
# Uses existing ChatGPT access; consumes its quota. No dollar estimate is asserted.
& $benchPython -m benchmarks.githits.run --python $benchPython --calls 2 --repeats 1 --write work/githits-run
```

The output directory must be new. Tests need no model or GitHits account:

```powershell
& $benchPython -m unittest tests.test_githits -v

# Re-run saved candidate trees: no model, token, or network.
& $benchPython -m benchmarks.githits.verify work/githits-run --python $benchPython --write work/githits-recheck.json
```

The Codex adapter uses the installed CLI's supported noninteractive JSON
output and strict schema, ignores personal configuration/rules, runs in an
empty temporary directory, and disables tools. It rejects any observed tool
execution. [Official noninteractive documentation](https://developers.openai.com/codex/noninteractive).
The requested model is recorded; the CLI does not report an immutable served
model snapshot in these events. There is no claim of bitwise model replay.

## Evidence and missing data

For the preregistered harder follow-up, use the same pinned environment and
the `hard-v2` profile. Keep environments outside the checkout or in ignored
`.venv/`: the registration rejects additional unignored Python source files.
The source guard verifies all frozen code and both registration documents;
do not regenerate the registration to bypass a mismatch.

```powershell
# Frozen-context rerun: six baselines, five available treatments; up to 22 calls.
# Existing model login required. No GitHits token or new retrieval is needed.
& $benchPython -m benchmarks.githits.run --suite hard-v2 --python $benchPython --calls 2 --repeats 1 --write work/hard-v2-rerun

# Replay the published evidence without model calls, token or network.
& $benchPython -m benchmarks.githits.verify benchmarks/githits/results/2026-10-15-hard-v2-run-001 --python $benchPython --write work/hard-v2-recheck.json
```

The [v2 registration](PREREGISTRATION-v2.md) was pushed at `291a7a1` before
any model calls. Its authenticated preflight found T04's exact pinned context
unavailable and stopped with zero trials. The [v2.1 amendment](PREREGISTRATION-v2.1.md),
pushed at `2895110` before trials, retains all six tasks and freezes that
65-file retrieval snapshot. Five pairs can complete; T04's baseline stays
standalone. The runner verifies these receipts before every run. This is
partial blinding with an unblinded operator, not an independent blind study.
See [the harder report](REPORT-hard-v2.md) and the original [3/3 tie](REPORT-run-004.md).

### Saved evidence format

`results.json` retains the existing benchmark row fields (`fixture`, `arm`,
`seed`, `solved`, `calls`, token counts, `dollars`, `simulated`, candidate and
evaluation counts, and `seconds`) plus availability and provenance. Tokens
are adapter/CLI-reported, not host measurements. Calls are the budget,
not equal tokens or dollars. A treatment request may cost more context.

Each completed run saves exact model input/output, CLI events, host contract,
host events and event hashes, check states/exit codes/durations/output hashes
and tails, candidate commit/tree identities, and a self-contained Git bundle.
Candidate refs are included in the bundle; they are not pushed as branches.
The core stores only a 4 KB check-output tail, not full original check logs.
`sha256.json` covers evidence files. `environment.json` records interpreter,
platform, package versions, source revision and requirements digest.
Every run includes `.gitattributes` to preserve raw artifact bytes across Git
checkouts on Windows. The recheck verifies the manifest, event hashes, Git
commit/tree identities, untouched tests/pins and candidate scope before
re-running version and behavior checks. It compares states and exit codes;
elapsed times and temporary paths can change output hashes on replay.

`with_githits` is **unavailable** when access or pinned context fails. Its
`solved` is null, and it cannot enter a matched denominator. A model/transport
error is also separate from an evaluated failed patch. The summary uses only
completed pairs. Zero pairs means no answer about uplift, parity or regression.
All standalone baseline rows remain visible. Results are written after every
arm so interrupted work retains completed observations.

See the dated `REPORT.md` for authenticated retrieval, paired measurements
and the exact source revision/diff used. Offline tests exercise fail-closed
identity validation; saved authenticated receipts retain both resolved and
served artifact identities.
For the measured run 004 implementation, checkout `4188cac`. The original
run recorded `166d51f` plus the saved source diff, which reconstructs the same
two changed files. That raw diff matches the runner's stripped-text digest;
append a newline before using it as input to `git apply`.

Historical runs 001 and 002 used the direct REST transport before the upstream
accounting change. They remain unchanged. The later
`results/accounted-access-2026-10-15.json` records all three dependency reads
through the approved wrapper; each still returned 401. Installing accounting
did not supply an API token or an existing OAuth profile. No new model run was
needed for that early access check. The later user-authorized token entry
enabled run 004's complete paired comparison; the earlier attempts remain
visible and are not pooled into its matched results.
