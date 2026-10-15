# Preregistration v2: higher-complexity dependency tasks

Owner: Seth Carlson. Acting agent: Codex. Authorized by the user's request
to preregister, run both arms and push only the benchmark branch.
Status at registration: authored and host-validated; **no model trials** on
these tasks. This document and its JSON digest manifest must be committed
and pushed before any model trial. This is a new exploratory experiment.

The existing run 004 result stays visible: **3/3 fixes in each arm on three
authored tasks**. Its data are not pooled into this experiment. No replacement
tasks will be added after seeing outcomes.

## Question and fixed workload

Does the same bounded patch provider solve more of six new pinned dependency
integration tasks when supplied with preselected GitHits source excerpts?

Six opaque task codes, two integration tasks per dependency family:

- T01: HTTPX 0.28.1, streamed compressed NDJSON, bounded parsing and ownership.
- T02: HTTPX 0.28.1, safe replay policy, exception handling and response ownership.
- T03: Pydantic 2.10.6, discriminated unions, per-call context and serialization.
- T04: pydantic-core 2.27.2 with Pydantic 2.10.6, custom type validation,
  serialization and JSON schema across Python/JSON modes.
- T05: SQLAlchemy 2.0.36, per-row savepoints inside a caller-owned transaction.
- T06: SQLAlchemy 2.0.36, distinct-parent pagination, literal filtering and
  joined collection loading in one query.

These are authored integration tasks against actual installed packages,
not sampled upstream bugs. They are higher-complexity by construction: each
has six behavioral test methods and multiple interacting constraints, rather
than an isolated API replacement. Their empirical difficulty is unknown until
the frozen run. No model baseline pilot or GitHits-assisted pilot selects them.
Reference fixes are authored using official documentation and the pinned local
packages. The reference must pass and the broken implementation must fail
behavior checks while passing its version check before registration.

Full objectives, starting files, held-out reference files, check programs and
two preselected source ranges per task are in `hard_fixtures.py`. Their hashes
and all repository Python source hashes are frozen in `PREREGISTRATION-v2.json`.
Requirements retain the existing 13 exact package pins. No live network is
needed inside task checks: HTTP uses fake transports and SQL uses SQLite.

## Blinding and its limits

The patch provider receives opaque task codes, the objective, current source
and pins. It receives no acceptance-check source, reference patch, prior tie
result, other arm's patch, or arm name. Each call uses a fresh ephemeral model
session with all tools disabled. The context supplier's brand is removed from
the treatment preamble. Data excerpts include their exact version provenance.

Task check programs receive only a candidate tree and fixed inputs; they do
not receive arm labels or provider claims. There is no subjective human grade.
Cell directories have neutral sequential labels. Progress prints cell counts;
the operator does not inspect model outcomes until all 12 cells finish.
Raw rows are still saved after each cell to preserve interrupted evidence.

This is **partial blinding**, not an independently authored, double-blind
study. The operator knows the reference fixes and arm mapping. A model can
notice extra context; it cannot be blinded to that difference. A repair call
sees the same bounded failing-check feedback as the other arm. The source
cannot be retrieved by the model because it has no tools; public preregistration
does not make acceptance tests available in its supplied prompt.

## Fixed procedure and budget

Requested model `gpt-6.1-sol`, high effort, existing signed-in Codex CLI;
two model calls maximum per arm, stop at the first host-verified acceptable
candidate. One repeat: six pairs, 12 cells, at most 24 model calls. Both arms
use identical base commits, package pins, source scope, host checks, repair
feedback policy and model configuration. Only treatment adds the frozen
source excerpts. Arm order uses Python Random seed 20261015. The model itself
has no deterministic seed; the CLI does not expose an immutable served snapshot.

Before any model call, validate all six baselines/references and obtain all
12 source reads through approved private accounting. Require HTTP 200,
verified exact served identities and current source. If any context is
unavailable or mismatched, stop before models, retain all receipts and report
the blocked experiment. No public-docs substitute, different version, direct
unaccounted fallback, or removal of an inconvenient task is permitted.

After registration, do not change objectives, tests, references, ranges,
model, effort, budget, task selection or analysis based on outcomes. Do not
pilot or rerun individual cells after a completed model response. Provider
errors remain separate from evaluated failed fixes. Any necessary amendment
must be separately committed, state what changed and whether any outcomes
were seen, and preserve the original attempt; it cannot silently replace it.

## Scoring and reporting

Primary measure: host-verified solves with versus without context among
completed pairs. Publish the numerator, completed-pair denominator and all
six planned pairs; also publish GitHits-only solves and baseline-only solves.
Unmatched/unavailable/provider-error cells have null solved values and stay
visible, rather than becoming zeroes or disappearing. A candidate counts only
when SyberWork's version and behavior checks and admission preview pass on
its exact Git tree. Model assertions do not count. Only `src/integration.py`
is mutable. No model candidate is accepted, merged or pushed as a product fix.

Report all outcomes including another ceiling, losses, and unavailable data.
No pooled nine-task rate, optional stopping, task replacement, cost comparison
or latency headline. Equal call caps are not equal input tokens or dollars.
The set is a small convenience sample; results do not establish general uplift,
general equivalence, unrestricted-agent performance, or an advantage over an
equal-size ordinary-documentation control.

Save exact prompts, model events, responses, host event chains, check states,
candidate identities, bundles, dependency receipts and a raw-byte manifest.
Recheck all saved candidate trees without a model/token/network before writing
the final report. Keep the original tie prominently visible and this new
experiment separate. Push only `bench/githits-verified`; no PR or merge.
