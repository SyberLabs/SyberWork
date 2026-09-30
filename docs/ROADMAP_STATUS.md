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
| Candidates in `syberwork.Work` | Implemented | `Work.record_candidate` / `record_evaluation` / `record_search` / `candidates`; HTTP routes; `searcher` and `evaluator` credentials, where an evaluator cannot hold an automation role or evaluate its own candidate; a console card. `tests/test_app_candidates.py` |

## Build Thread — phase 2 (roadmap §4, Gate 1 and part of Gate 2)

| Roadmap item | Status | Where |
| --- | --- | --- |
| Start: one initializer, visible scope, cost ceiling, source access; no account | Implemented | `syberlabs init` / `Kit.local`; contract budget and scope; `start --paths` |
| Orient: path and symbol retrieval before embeddings; developer sees and removes sources | Implemented | `syberlabs/retrieval.py`, `syberlabs context [--drop]`. No embeddings. |
| Propose: structured proposal, source references, provider revision, context digest, budget use, diff | Implemented | `search_started` / `candidate_registered` / `search_finished`; `syberlabs diff` |
| Validate: project-owned checks in a controlled environment; passing test vs untested claim; model cannot turn red green | Implemented | `syberlabs/checks.py` (temporary worktree, argv, scrubbed env, timeouts, output cap), `Verdict`. It is not a sandbox. |
| Accept: explicit, separate from publishing; idempotent; unknown stays unknown until reconciled | Implemented | `accept` = admitted compare-and-swap of `refs/heads/syberlabs/<thread>`. |
| Push, pull request, publish as separate effects with their own authority and idempotency | Implemented | `syberlabs/publish.py`, `syberlabs publish ACTION`: `git_push` (lease), `github_pull_request` (lookup before create), `command` (idempotency key and status argv); a settle window before a remote absence frees the action. `tests/test_publish.py` against a bare remote and a fake GitHub API. No real GitHub or package index was called. |
| Resume: rebuild from a compact event record; open obligations; next permissible action | Implemented | `Thread.status`, `syberlabs status` |
| Durable store, restart test, one conformance suite across stores | Implemented | `syberlabs/journal.py`, `Session(journal=...)`, `tests/test_journal.py`, `tests/test_build_thread.py::Recovery` |
| Crash after an effect claim cannot cause a duplicate effect | Implemented and tested | Crash before and after the ref write, then `recover` |
| Memory inspect, export, prune, forget | Implemented | `syberlabs memory`, `export`, `prune`, `forget`. Forget follows `policy.json` `retention`: refused while an effect is unresolved; history proving an effect is kept `effect_history_days` (default 365), then leaves a receipt with no objective or bodies; a tombstone for every forget. `syberlabs/retention.py`, `tests/test_forget.py`. |
| Contract diff and guided fixes | Implemented | `syberlabs contract-diff`; `hint()` on every refusal |
| Model seam | Implemented as a process boundary | `CommandProvider`. No Kev or Jev adapter is included, and no model call was made. |
| Clean wheel runs the Build Thread | Measured | `conformance/clean_install.py` runs `examples/build_thread.py` and `syberlabs --help` from the installed wheel |
| Machine time to first verdict on the fixture | Measured: 186 ms | `benchmarks/results/build-thread-dev.txt` |
| Gate 1 exit: 2 of 3 unfamiliar developers finish the quickstart and change a contract within 15 minutes | **Not measured** | Needs people. The quickstart and contract-change path exist for that test. |
| Local inspector UI | Implemented, read-only | `syberlabs inspect`, `syberlabs/inspector.py`, `syberlabs/static/`. Token, loopback `Host` check, GET only, strict CSP, text-only rendering. `tests/test_inspector.py` includes a headless Chromium render with no console or CSP errors, when node and playwright are installed. Actions stay in the CLI. |

## Operational Module × EvoGit — phase 3: evolutionary provider

| Item | Status | Where |
| --- | --- | --- |
| EvoGit-style mutation and crossover as a replaceable `SearchProvider` | Implemented | `syberlabs/evolve.py`, `syberlabs propose --evolve`. An independent implementation; no EvoGit code (AGPL-3.0). |
| EvoGit-generated branches remain non-authoritative | Implemented and tested | Population on `refs/syberlabs/candidates/`; no `refs/heads` change until a person accepts |
| Candidates provisional until contract, evidence, policy, and approval gates admit them | Implemented and tested | Selection uses host evaluations; promotion goes through `candidate.promotable` and approval |
| Model-backed mutation | Implemented as a process seam | `CommandMutator`. No model adapter is included and no model was called. |
| Baseline versus mutation only | Measured on a toy fixture | 25/30 with crossover, 19/30 without, re-measured after duplicate refusal. The first run was 22/30 either way. Suggestive, not established. |
| Evidence that model-backed search beats a single model patch at the same cost | **Not measured with a model** | The apparatus is implemented: `benchmarks/model_vs_patch.py` (single, best-of-K, repair with check feedback, evolve; matched on model calls, tokens and dollars reported), `adapters/anthropic_adapter.py` (official SDK, `claude-opus-5`, structured output, refusal fallback, usage returned), three fixtures, and tests. It has only been run against `benchmarks/simulated_model.py`, whose results describe the harness, not a model. No API credential was available in this environment. See `docs/MODEL_COMPARISON.md`. |
| Distributed hosts and migration between them (EvoGit's multi-host mode) | Implemented | `syberlabs/exchange.py`, `EvolutionaryProvider(exchange=...)`, `propose --exchange`. Per-host namespaces on a shared remote; migrants are verified against the published ref and the local base, evaluated locally, and recorded with an `origin`. `tests/test_multihost.py` with two host clones and a bare remote. |

### Decisions that belong to the owner (not made here)

1. **License.** There is no `LICENSE` file. Choosing one is a legal decision for SyberLabs. Until one is added, outside developers have no grant to use the code, which blocks the Gate 4 adoption test. EvoGit is AGPL-3.0. An EvoGit-style search provider in this repository must be an independent implementation of the published method and must not copy EvoGit code, or it would constrain this decision.
2. **Package split.** The wheel is still one distribution, `syberwork`, containing both `syberwork` (application) and `syberlabs` (SDK). The roadmap recommends a separately identifiable `syberlabs` distribution. The code is ready for that (`syberlabs` imports nothing from `syberwork`, which `tests/test_package.py` checks); the remaining work is a second build configuration and a version pin from `syberwork` to `syberlabs`. It is not done here because the distribution name on a package index is a public, hard-to-reverse choice.

## Industrial Cell v0.1

The local appliance remains the default. A cell is one organization and one database, not a `tenant_id` column. Admission is unchanged. See [INDUSTRIAL_CELL.md](INDUSTRIAL_CELL.md).

| Item | Status | Where |
| --- | --- | --- |
| Storage seam: SQLite and PostgreSQL, same statements | Implemented | `syberwork/storage.py`. `tests/test_cell.py` compares one case on both when PostgreSQL is reachable |
| Migration ledger, including a pre-migration SQLite history | Implemented | `0001`–`0004`. The `main` fixture still verifies. `0004` is Builder coordination, not case history |
| Effect worker and expired-lease uncertainty | Implemented | `syberwork/worker.py`. Inline commit remains the default |
| Cell organization and principal records | Implemented | `bind_organization`, `register_principal`. Not SSO |
| Semantic trace, backup/restore | Implemented | `syberwork/trace.py`, `syberwork/backup.py` |
| Container and CI | Implemented | `Dockerfile`, `.dockerignore`, and Compose share `/home/syber/cell`. `sh scripts/compose_cell.sh` boots that cell. Linux, PostgreSQL, and Windows jobs are green on the stabilized head. Making those checks required on `main` is a repository setting this agent cannot change. |
| SSO, KMS witness, shared multi-tenant database, fleet control plane | **Not built** | Out of this slice |

### Limitations stated, not fixed

- `Session.observe(..., verified=True)` is the host's claim. The in-memory session does not independently read a source.
- The witness is a separate process, not a separate OS user or a public transparency service.
- Only `syberwork.Work` has durable HTTP effects. `Session` is in memory unless given a `Journal`; its only durable local effect is the Build Thread's compare-and-swap ref update.

## Builder substrate

The future Builder UI has a coordination, integrity, and projection backend. It is not the visual Builder, and it is not admission. See [spec/BUILDER.md](../spec/BUILDER.md).

| Item | Status | Where |
| --- | --- | --- |
| Generations, approach descriptors, diversity gate, isolation manifests | Implemented | `syberlabs/builder.py`, `syberwork/coordination.py`. A candidate cannot link before seal. Sealed descriptors are immutable. Embeddings are not the gate. |
| Coordination log, separate from the case chain | Implemented | Migration `0004`. SQLite and PostgreSQL. |
| Feedback authority, selection policy, integrity classes | Implemented | Grants are per role and feedback kind, and they are not collapsed to one rank. Advisory preferences do not select unless the policy lists them. Selection reads verified integrity only. `promotes_git` is false. |
| Agent sessions, activity causality, command contract | Implemented | No chain-of-thought field. The default command result is `runtime_not_connected`. `send_context` and `redirect` are different operations. |
| Architecture graph snapshots and prototype registry | Implemented | Graph AST with repository paths. No diagram vendor. The prototype record hides how it was provisioned. |
| Projections, role lens, Builder HTTP, SSE | Implemented | `GET /api/builder/...` and `GET /api/builder/work/:id/stream`. Existing case routes are unchanged. |
| Final Builder visual system | **Not built** | This slice is the substrate those screens can read. |
| Evaluation worlds (Islo and DoubleAgent as replaceable providers) | **Proposed** | [EVALUATION_WORLDS.md](EVALUATION_WORLDS.md). Not a schema, a migration, or a runtime. `candidate_evaluated` stays frozen. |

A coordination record is not a case event. A displayed comment, diversity score, agent claim, or architecture snapshot does not admit or promote anything. The command adapter does not attach to a process. Integrity digests are stored, not verified.
