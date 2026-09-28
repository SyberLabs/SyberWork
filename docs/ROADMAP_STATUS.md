# Roadmap status

This file tracks the next-stage roadmap (27 September 2026) against what the repository actually does. Each row says whether a claim is **implemented** (code and tests in this repository), **measured** (a number from a named run), or **proposed** (not built). A passing unit test is not evidence that a developer outside SyberLabs can use the kit.

## Gate 0 — integrate the foundation

| Item | Status | Where |
| --- | --- | --- |
| SDK PR #3 (`sdk-foundation`, 061fb3b) reconciled with `main` | Implemented | merge commit on this branch. `main` only changed the README provider note since the SDK branch point; the Kev wording is kept. |
| Economic PR #2 (`feat/economic-actions`, 17e7ac7) reconciled with the SDK core | Implemented | #3 already carried a refactor of #2's admission (`syberlabs/economic.py`, rule `economic.reserve`). #2's duplicate `syberwork/economic.py` is dropped; #2's docs and 15 tests run against the SDK core. |
| Behaviors #2 had and #3 lacked | Implemented | `reconcile` accepts an interrupted `effect_started`; `commit`/`reconcile` never append a second terminal outcome; `cancel_case` refuses while an `economic_http` effect is claimed. Without the first, an interrupted claim occupied its action with no path to release. |
| Economic docs named "Barn" as the admitting system | Fixed | Barn is a separate system. The docs now name SyberWork's `economic.reserve` rule. |
| Clean-install release gate | Implemented | `python -m conformance.clean_install` builds the wheel, installs it in a new venv, and runs `examples/release_gate.py` and `examples/access_review.py` from outside the checkout. |
| Unit tests, conformance, schemas on the integrated head | Measured | 83 unit tests, 23 golden traces, schema validation of examples and golden events: all pass on CPython 3.11, 3.12, and 3.13 in this container. |
| First public protocol vocabulary | Frozen as `sdk.syberlabs.space/v0alpha1` | `spec/`. |

## Operational Module × EvoGit — phase 1: candidates and the evolution section

| Item | Status | Where |
| --- | --- | --- |
| Optional `evolution` section in versioned contracts (scope, operators, budget, checks, promotion authority) | Implemented | `syberlabs/evolution.py`, `spec/evolution.schema.json`. Unknown fields fail closed. |
| First-class candidates with Git lineage, provenance, evaluation evidence, status, and promotion state | Implemented | `candidate_registered` / `candidate_evaluated` / `search_*` events. `Session.candidates` and `inspect` project the evaluation state and promotion state. `syberlabs.search.Candidate` is the typed view. |
| Provider-neutral `SearchProvider` / `SearchSpace` interface | Implemented as types | `syberlabs/search.py`. The Git-backed space is in the Build Thread PR. |
| No model or agent can promote | Implemented | `candidate.promotable`: human origin, no automation role, a promotion role, plus any `approval_role`. |
| Lineage is provenance, not evidence | Implemented and tested | The rule reads only the candidate's own newest evaluation of its exact tree. `test_lineage_is_provenance_not_evidence`. |
| Candidates in `syberwork.Work` | Proposed | `Work` stores evolution contracts and fails closed. It has no candidate API or console view. |

### Decisions that belong to the owner (not made here)

1. **License.** There is no `LICENSE` file. Choosing one is a legal decision for SyberLabs. Until one is added, outside developers have no grant to use the code, which blocks the Gate 4 adoption test. EvoGit is AGPL-3.0. An EvoGit-style search provider in this repository must be an independent implementation of the published method and must not copy EvoGit code, or it would constrain this decision.
2. **Package split.** The wheel is still one distribution, `syberwork`, containing both `syberwork` (application) and `syberlabs` (SDK). The roadmap recommends a separately identifiable `syberlabs` distribution. The code is ready for that (`syberlabs` imports nothing from `syberwork`, which `tests/test_package.py` checks); the remaining work is a second build configuration and a version pin from `syberwork` to `syberlabs`. It is not done here because the distribution name on a package index is a public, hard-to-reverse choice.

### Limitations stated, not fixed

- `Session.observe(..., verified=True)` is the host's claim. The in-memory session does not independently read a source.
- The witness is a separate process, not a separate OS user or a public transparency service.
- Only `syberwork.Work` has durable HTTP effects. `Session` is in memory.
