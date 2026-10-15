# GitHits x SyberWork: 3/3 fixes in both arms

**Draft meeting brief — October 15, 2026, Pacific time.**
Owner: Seth Carlson. Acting agent: Codex. Evidence grade: same-host measured
and replayed candidate checks; exploratory authored task set.

**Three matched pairs completed. Without GitHits: 3/3 host-verified fixes.
With GitHits: 3/3. All six fixes passed on their first model call. GitHits-only
solves: 0; baseline-only solves: 0. No uplift was observed on this task set.**

[One-screen brief](phone.html) · [Raw paired results](results/2026-10-15-run-004/results.json)
· [Host replay](results/2026-10-15-recheck-004.json)

## Preregistered follow-up

The separate [hard-v2 preregistration](PREREGISTRATION-v2.md) freezes six new
higher-complexity authored tasks before model trials. Its source and task
hashes are in [PREREGISTRATION-v2.json](PREREGISTRATION-v2.json). The same
requested model and host checks will be used for both arms. Checks/reference
fixes are hidden from the model; the provider receives unbranded source
context and automatic scoring is arm-unaware. The operator is not blinded,
and the extra context remains visible to the model. Results are pending.
The original **3/3 versus 3/3 tie above stays separate** and will not be pooled.

## What the result supports

The authenticated GitHits integration supplied the six planned source
excerpts at the pinned package versions. The same patch provider solved
every task with and without that context. This is an observed tie on three
authored integration regressions, not evidence of general equivalence or
improvement. The baseline reached the ceiling on this set.

- **HTTPX 0.28.1:** migrate the removed ASGI client shortcut. Both arms passed.
- **Pydantic 2.10.6:** migrate attribute/mapping validation and serialization.
  Both arms passed.
- **SQLAlchemy 2.0.36:** migrate statement execution and row mappings while
  retaining transaction rollback. Both arms passed.

These use real installed packages, but are not sampled upstream issues or
customer bugs. Before either model arm, every starting implementation failed
behavior checks with the correct dependency version, and every held-out
reference fix passed. Each model fix passed the pinned-version check, behavior
suite and admission preview. SyberWork ran the checks on exact candidate Git
trees; model assertions never counted.

## How it was measured

Run 004 began at **13:23 PDT**. Both arms used the same per-task base commit,
pins, objective, mutable `src/` scope, checks, requested `gpt-6.1-sol` model,
high effort and two-call maximum, stopping at the first passing candidate.
There was one repeat and a fixed shuffled arm order. No repair call was needed.

The model was a bounded JSON patch proposer with tools disabled. The host
added preselected GitHits source in the treatment. This does not measure
unrestricted coding agents, agent-driven MCP discovery, or a GitHits-specific
advantage over equal-size ordinary documentation. The CLI reports a requested
model alias, not an immutable served snapshot. Calls are matched; input tokens
and dollars are not. No cost or latency comparison is asserted.

All six planned `/v1/read` requests returned HTTP 200 through approved private
usage accounting. The validator matched each requested/resolved package to
the served repository, exact version and full commit, and checked path and
line bounds. Saved receipts and exact model stdin show the supplied context.
The token stayed in a temporary environment; neither it nor the private
accounting ledger is included. Accounting units are CLI invocations, not an
API-volume measurement.

The measured checkout was `166d51fa15bbddb9741400e55da31e9e0f30cc01` plus the
[exact saved source patch](results/2026-10-15-run-004-source.patch), SHA-256
`348a4cd69bb41eea0d381ce2509672fa342bdc52c2c5e7be19287ef905322f2a`.
It fixes authenticated response validation and adds regression coverage;
it does not change tasks, source ranges, prompts, budgets or host checks.
Commit [`4188cac`](https://github.com/SyberLabs/SyberWork/commit/4188cac7ab36adcead0f2b12351d44e791646993)
contains the same implementation. The recorded source diff is intentional
provenance, not a clean-checkout claim.
The runner hashes stripped diff text, so the saved patch omits its terminal
newline. Add one newline before passing it to `git apply`; this reconstruction
was checked against both committed files in `4188cac`.

The 140 manifest-covered evidence files retain exact model input/output,
host events, candidate identities, pins and self-contained Git bundles. The
model-free replay reproduced all **12 tree evaluations**: three broken
baselines, three reference fixes and six model fixes. This is a replay on the
same host, not independent external validation. Host: Windows 11, CPython
3.12.14, Ryzen 7 9800X3D, about 32 GB memory.

## Earlier attempts and limits

Runs 001 and 002 lacked a token and completed zero pairs; their standalone
baselines are retained and are not pooled into this result. Run 001 also has
a [Windows prompt-capture limitation](results/2026-10-15-run-001-NOTES.md).
Run 003 authenticated successfully, but our validator rejected a valid
repository-backed response: package identity appeared in `resolved_requested`
while `served` carried repository/version/commit with null package fields.
It completed zero pairs. Its [raw evidence](results/2026-10-15-run-003/results.json)
and [replay](results/2026-10-15-recheck-003.json) are preserved. Run 004 repeats
both arms after that fix; no tasks or excerpts were selected based on outcomes.

To test whether GitHits produces more fixes, preregister a harder, blinded
task set before running either arm. Keep this saturated result visible.

[README.md](README.md) provides reproduction commands and evidence format.
[VALIDATION.md](VALIDATION.md) records repository checks and the existing
Windows clean-install limitation. Host checks are controlled subprocess
execution, not a security sandbox. Decisions are in [DECISIONS.md](DECISIONS.md).
Only `bench/githits-verified` is pushed; no PR, merge or deployment is included.
