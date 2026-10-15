# Validation record — October 15, 2026

Claim ceiling: two separate authored task sets, one repeat each, measured and
replayed on this host. Original: 3/3 versus 3/3. Harder: 5/5 versus 5/5 across
five of six planned pairs. Neither observed tie establishes general parity
or improvement. Results are not pooled.

## Preregistered harder follow-up

Initial registration `291a7a1` passed 283 local tests with 22 skips in 364.755
seconds, 21 focused tests and all 23 conformance golden traces. All six broken
starts failed behavior with correct dependency versions; all six references
passed their 36 behavior methods. The authenticated preflight then saved 65
manifest-covered files: ten HTTP 200 reads and T04's HTTP 404 `VERSION_NOT_FOUND`.
It stopped with zero model cells. No fixture or acceptance check was changed.

Coverage amendment `2895110` passed 23 focused tests and
[all five CI jobs](https://github.com/SyberLabs/SyberWork/actions/runs/38810257111).
The [CI receipt](results/hard-v2-source-ci.json) identifies that measured source.
A fresh remote checkout verified its 140 frozen source hashes and the entire
65-file cached-context archive before model trials. The model matrix began on
that clean revision at 14:29 PDT. No subsequent GitHits requests were made.

The [matrix](results/2026-10-15-hard-v2-run-001/results.json) completed 12 planned
cells: 11 evaluated cells, 12 model calls, five matched pairs. Both matched arms
solved all five tasks on call one. T04's standalone baseline solved on call two;
its unavailable treatment made zero calls and has null solved. All six baselines
therefore solved within the two-call cap. No treatment outcome is imputed.

The [model-free replay](results/2026-10-15-hard-v2-recheck-001.json) verified 265
evidence hashes and 17 fixture/arm records, reproducing 24 tree evaluations:
six broken starts, six references and twelve model candidates. It also verifies
the registration, source hashes and original cached receipts. Manual review of
all twelve patches found only allowed source edits and no check bypasses. All
twelve calls used distinct sessions and identical requested model, effort and
tool settings; no tool execution was observed. First-call prompts exclude check
and reference source; treatment excerpts match the frozen receipts exactly.
The operator inspected outcomes only after the whole matrix completed.

Partial blinding and availability-conditioned sampling are limits. This is
same-host replay, not independent validation. See [the full report](REPORT-hard-v2.md).
The updated brief shows both ties and the coverage gap at 390 by 844 CSS pixels
without horizontal or vertical overflow.

## Authenticated paired follow-up

Run [004](results/2026-10-15-run-004/results.json), at 13:23 PDT, completed all
three pairs. Both arms solved all tasks on the first call. All six planned
authenticated reads returned HTTP 200 through private accounting, with exact
version/repository/commit identity checked. Treatment prompts contain their
saved context, and each pair shares one base commit. No GitHits token pattern
or private usage runtime path was found in the public evidence scan.

The [replay](results/2026-10-15-recheck-004.json) verified 140 evidence-file
hashes and nine fixture/arm records, reproducing 12 tree evaluations: three
broken baselines, three references and six model fixes. Source is `166d51f`
plus the [saved patch](results/2026-10-15-run-004-source.patch); its exact digest
is recorded in `environment.json` and `REPORT.md`. Commit `4188cac` contains
that same validator/test implementation. This run did not start from a clean
checkout. Tasks, source ranges, model configuration and host checks stayed fixed.

Run [003](results/2026-10-15-run-003/results.json) obtained HTTP 200 for all
three initial reads but rejected valid metadata in our validator. Its unchanged
86-file archive and [replay](results/2026-10-15-recheck-003.json) retain three
baseline fixes and zero completed pairs. It is not pooled with 004. Two added
regression tests cover the real response shape and rejection of missing or
conflicting package, repository, commit and version evidence. All 16 focused
offline tests passed after the fix.

The local full suite passed: **278 tests, 22 skipped**, in 392.591 seconds,
using the pinned benchmark virtual environment. This is separate from the
earlier overnight suite results below.

The 23 conformance golden traces matched after the fix. GitHub CI passed for
the validator/source commit [`4188cac`](https://github.com/SyberLabs/SyberWork/actions/runs/38801619308).
The brief was inspected at a 390 by 844 CSS-pixel viewport without overflow.
An actual remote clone of `b5a2d98`, with `core.autocrlf=true` and
`core.longpaths=true`, also verified all 140 hashes and replayed all 12 tree
evaluations. One deliberately deeper checkout placed a journal file at a
260-character full path: Git checked it out, but this Python runtime could
not open it through the ordinary path. The same file was present through
Windows extended-path syntax. A shorter checkout replayed successfully.
Use both a short checkout path and Git's long-path setting on Windows;
the archived bytes and manifest were not changed.

## Benchmark evidence

Earlier run: [2026-10-15-run-002](results/2026-10-15-run-002/results.json).
Source: `9e81b6bf5070983f0bad34929df69975984e88f0`, empty source diff.
Requested model: `gpt-6.1-sol`, high effort; Codex CLI
`0.162.0-alpha.17.2`; one repeat, two-call cap. All three baseline fixes passed
on the first call. Three GitHits requests returned HTTP 401. No pairs completed.

The [recheck](results/2026-10-15-recheck-002.json) verified 86 evidence-file
hashes and six fixture/arm records: three validation records, each containing
the broken baseline and held-out reference, plus three model-fix records.
All nine saved Git-tree evaluations reproduced their pass/fail states and
exit codes. Each evaluation has a dependency-version check and a behavior
check. The candidate trees change only `src/integration.py`.

A fresh clone from the saved branch bundle at `7cb9928`, with Windows
`core.autocrlf=true`, also passed the same 86-file verification and nine-tree
host recheck. The first deep-path checkout hit Git's Windows filename limit;
cloning with `--config core.longpaths=true` succeeded. The README and handoff
include that per-checkout setting. No system setting was changed.

After upstream adopted accounting in `a1ccbdb`, the benchmark's live lookup
path was adapted to the private CLI wrapper. All three real, accounted reads
returned HTTP 401; the public receipt is
[accounted-access-2026-10-15.json](results/accounted-access-2026-10-15.json).
No private ledger, token or private runtime path is copied into this repository.
These are CLI-invocation accounting records, not an API-volume measurement.
The two additional offline tests cover missing-runtime failure with no direct
fallback and environment-only credential propagation to the accounted child.

The exact archived bytes also matched after staging in Git. Run-local and
parent `.gitattributes` preserve raw bytes; whitespace lint remains active on
source and documentation. Early run 001 is retained with a separate capture
limitation note and is not pooled into the final result.

## Repository checks

Host Python:
`C:\Users\scarl\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`.
Benchmark Python:
`C:\Users\scarl\Documents\Codex\2026-10-14\overnight-task-githits-x-syberwork-verified\work\githits-venv\Scripts\python.exe`.

- `python -m unittest discover -s tests -q`: **OK**, 276 tests, 22 skipped,
  259.758 seconds after the accounting transport update.
  [Captured output](results/validation-final-unit.txt).
  Optional database/browser checks were not all exercised locally.
- `python -m unittest tests.test_githits -q`: **OK**, 14 offline tests,
  including false provider claims, check rewrites, unavailable arms,
  wrong-version context, credential handling, artifact tampering and exact
  model-stdin capture across mixed newline conventions.
- `python -m conformance.run`: **matched all 23 golden traces**.
  [Captured output](results/validation-final-conformance.txt).
- `git diff --cached --check`: passed with raw-archive attributes applied.
- [CI for measured source 9e81b6b](https://github.com/SyberLabs/SyberWork/actions/runs/38696940120):
  completed successfully. This is distinct from the later report-only head.
- Phone brief: rendered in the browser at 390 × 844 CSS pixels; no horizontal
  or vertical overflow. This checks layout, not efficacy or external acceptance.

## Stock clean-install failure and environment workaround

The unmodified `python -m conformance.clean_install` command failed while
importing the installed wheel, before the public examples:

```text
ssl.SSLError: [SSL] unknown error (_ssl.c:3036)
```

`conformance/clean_install.py` gives its subprocesses `PATH`, `HOME` and
`PYTHONNOUSERSITE`, dropping Windows `SYSTEMROOT`. A separate minimal
`ssl.create_default_context()` subprocess returned exit 1 in that environment
and exit 0 with `SYSTEMROOT` added. No TLS verification was disabled.

The same wheel-build, installed-origin and public-example checks passed with
only that variable preserved. This was the diagnostic wrapper used, not a
change to the repository's conformance command:

```python
import os, sys
from conformance import clean_install

original_run = clean_install.run

def windows_run(argv, *, cwd, env=None):
    if env is not None:
        env = {**env, "SYSTEMROOT": os.environ["SYSTEMROOT"]}
    return original_run(argv, cwd=cwd, env=env)

clean_install.run = windows_run
sys.argv = ["conformance.clean_install"]
clean_install.main()
```

Observed terminal summary:

```text
release-gate complete events=12 chain=True
access-review complete=True events=12 recorded=all_checks_passed now=action_already_completed planner=required_prior_effect_missing unverified=source_verification_required:roster replay=['certify_scope:approval_required:manager'] tamper_detected=True
build-thread complete
evolve complete
usage: syberlabs [-h] [--home HOME] [--repo REPO] [--thread THREAD]
wheel=syberwork-0.1.0-py3-none-any.whl python=Python 3.12.14 seconds=35.2
```

Do not report the stock command as passing on this host. The conformance
source was kept unchanged to keep this work scoped to the benchmark.

## Handoff

The report, brief, runner, pinned dependencies, raw evidence and recheck are
under `benchmarks/githits/`; regression tests are in `tests/test_githits.py`.
No core package, protocol, policy, admission rule or runtime dependency changed.
Branch: `bench/githits-verified`. No pull request or merge.

Both paired follow-ups are complete, with T04's missing treatment explicitly
unavailable. Further tasks or repeats require a new preregistration before
observing outcomes. Do not reuse either tie as general parity or uplift.
