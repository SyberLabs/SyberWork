# Hard-v2: 5/5 fixes in each available matched arm

**Draft benchmark report — October 15, 2026, Pacific time.**
Owner: Seth Carlson. Acting agent: Codex. Evidence grade: measured and
replayed on this host, with authored tasks and partial blinding.

**Five of six planned pairs completed. Without GitHits context: 5/5 fixes.
With frozen GitHits context: 5/5 fixes. All ten paired fixes passed on the
first call. GitHits-only solves: 0; baseline-only solves: 0. No uplift observed.**

T04's treatment is unavailable: the pinned `pydantic-core 2.27.2` request
returned HTTP 404 `VERSION_NOT_FOUND` during preflight. Its standalone
baseline solved on call two. It is not a paired win/loss or a failed treatment.
All six baselines solved within the two-call cap, so this task set also
reached the baseline ceiling. The missing T04 treatment outcome remains unknown.

[Raw matrix](results/2026-10-15-hard-v2-run-001/results.json) ·
[Model-free host replay](results/2026-10-15-hard-v2-recheck-001.json) ·
[Initial registration](PREREGISTRATION-v2.md) ·
[Pre-model amendment](PREREGISTRATION-v2.1.md)

The earlier **3/3 versus 3/3 tie on three different tasks remains visible**
in the [main report](REPORT.md) and [original report](REPORT-run-004.md).
These experiments are not pooled into an eight- or nine-task rate.

## All planned outcomes

- **T01, streamed HTTPX NDJSON:** both arms solved on call one.
- **T02, HTTPX retry policy and ownership:** both arms solved on call one.
- **T03, Pydantic discriminated validation and serialization:** both arms
  solved on call one.
- **T04, Pydantic/core custom type:** baseline solved on call two; treatment
  unavailable, zero model calls. The failed first baseline candidate is retained.
- **T05, SQLAlchemy savepoints:** both arms solved on call one.
- **T06, SQLAlchemy pagination and collection loading:** both arms solved
  on call one.

There were 12 planned cells, 11 evaluated cells and 12 actual model calls.
Every scored fix passed the dependency-version check, six behavior test
methods and the admission preview on its exact Git tree. Each starting
implementation failed behavior checks and each held-out reference passed.
Only `src/integration.py` changed in candidate trees. No candidate was accepted
or pushed as a product fix; bundles preserve the candidate objects as evidence.

## Preregistration and coverage amendment

Initial registration [`291a7a1`](https://github.com/SyberLabs/SyberWork/commit/291a7a1c2d4f257967d93add19ab9a6809e9481f)
froze all six task definitions before any model trials. Its authenticated
preflight made 11 planned source reads: ten returned HTTP 200 and T04's first
read returned 404. The all-context gate stopped with zero model cells. Its
[65-file receipt manifest](results/hard-v2-access-001/sha256.json) and original
registration remain unchanged.

Amendment [`2895110`](https://github.com/SyberLabs/SyberWork/commit/2895110e08c440cd91820756edc093ea4713a475)
was committed and pushed before any model call. It retained the identical
six task digests, checks, pins, source ranges and model configuration. It
explicitly allowed T04's unavailable treatment and pinned the original receipt
manifest for frozen-context reuse. No task was replaced and no other version
or source was substituted. No further GitHits requests were made during the
model matrix. The five matched pairs are conditional on this retrieval
snapshot's availability, which is a material selection limit.

## Controls and blinding

The model run began at **14:29 PDT**, on clean source `2895110`; the recorded
source diff is empty. Requested model `gpt-6.1-sol`, high effort, Codex CLI
`0.162.0-alpha.17.2`, one repeat, two-call cap per arm, stopping on the first
host-verified acceptable candidate. Both arms use the same per-task base
commit, objective, pins, mutable scope, checks and repair-feedback policy.
Order is fixed by seed 20261015. Calls, not input tokens or dollars, are matched.
The CLI reports a requested alias, not an immutable served model snapshot.

The model has no tools and sees opaque task labels, starting code and pins.
First-call prompts contain neither acceptance-test source nor reference fixes.
Treatment adds the exact frozen dependency excerpts with supplier branding
removed. A repair call sees bounded failing-check feedback. All 12 model
calls used distinct fresh sessions and matching model/effort/CLI/tool settings;
no tool-execution event was observed. The operator did not inspect outcomes
until all 12 cells finished. Deterministic host checks receive no arm label.

This is **partial blinding**: the author/operator knows the fixtures and arm
mapping, and a model can notice additional context. It is not an independent,
double-blind study. Tasks are authored integration exercises against installed
packages, not sampled upstream bugs or customer issues. Complexity increased
by construction, but all available matched tasks still saturated on call one.
This does not establish general equivalence, a product advantage, or performance
of unrestricted agents, agent-driven retrieval, or equal-size ordinary docs.

## Verification and limits

The model-free replay verified **265 evidence-file hashes**, 17 fixture/arm
records and **24 tree evaluations**: six broken baselines, six references and
12 model candidates, including T04's failed first candidate. It also checks
the frozen registration, source hashes, initial 65-file manifest and exact
cached contexts. Candidate review found no test-file edits or test bypasses.
This is same-host replay, not independent external validation or a security
sandbox. Host: Windows 11, CPython 3.12.14; [hardware](results/hard-v2-host.json).

The initial preregistration source passed 283 local tests with 22 skips.
The amendment passed 23 focused offline tests and all five CI jobs on `2895110`.
The 23 conformance traces passed before registration; no core code changed.
See [VALIDATION.md](VALIDATION.md) for conditions, reproduction and the existing
Windows limitations. Credentials and the private usage ledger are excluded.

No new tasks, repeats or reduced-call comparison were added after seeing
outcomes. A future experiment needs a new preregistration. The present result
shows no additional fixes with GitHits on either available matched set.
