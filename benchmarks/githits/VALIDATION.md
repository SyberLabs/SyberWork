# Validation record — October 15, 2026

Claim ceiling: tested harness and measured baseline; zero completed GitHits
pairs. This is not an authenticated GitHits integration acceptance record.

## Benchmark evidence

Final run: [2026-10-15-run-002](results/2026-10-15-run-002/results.json).
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

Next permissible package: provide an existing approved GitHits credential,
run into a fresh evidence directory, inspect exact served versions and
complete the matched pairs. A harder task set requires a new preregistration
before observing either arm. Do not reuse this result as a claim of uplift.
