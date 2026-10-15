# GitHits x SyberWork: no comparative result yet

**Draft meeting brief — October 15, 2026, Pacific time.**
Owner: Seth Carlson. Acting agent: Codex. Claim ceiling: implemented and
tested harness; measured baseline; GitHits comparison unavailable.

**The baseline produced 3/3 host-verified fixes, each on its first model call.
GitHits returned HTTP 401 for all three pinned-source requests. Completed
matched pairs: 0/3. We cannot claim improvement, regression, or parity.**

[One-screen brief](phone.html) · [Raw final results](results/2026-10-15-run-002/results.json)
· [Model-free recheck](results/2026-10-15-recheck-002.json)

## What was actually measured

Run 002 began at 00:10 PDT, using source
`9e81b6bf5070983f0bad34929df69975984e88f0` with no source diff.
The requested model was `gpt-6.1-sol`, effort `high`, with at most two calls
per task and a stop at the first host-verified fix. No repair call was needed.

- **HTTPX 0.28.1:** migrate the removed ASGI client shortcut. Host checks passed.
- **Pydantic 2.10.6:** migrate attribute/mapping validation and serialization.
  Host checks passed.
- **SQLAlchemy 2.0.36:** migrate statement execution and row mappings while
  retaining transaction rollback. Host checks passed.

These are authored integration regressions using real installed packages,
not sampled upstream issues or customer bugs. Every starting version failed
behavior checks with the correct dependency version; every held-out reference
fix passed before the model run. Each model fix then passed its own pinned
version check, behavior suite and admission preview. SyberWork ran these
checks on the candidate Git tree; model assertions were not evidence.

The final evidence is committed at
[`6a7abf6`](https://github.com/SyberLabs/SyberWork/commit/6a7abf663265faa4fd5b0d2231b5c04098327bcc).
It includes exact model stdin/output, host events, candidate commit/tree
identities, package pins, Git bundles and raw-byte hashes. The model-free
recheck reproduced all three broken baselines, reference fixes and model
fixes. This is a recheck on the same host, not external validation.

## Why there is no GitHits number

All three `/v1/read` responses were `401 AUTHENTICATION_REQUIRED`, with
request IDs and bodies saved in the per-task context records. No existing
GitHits token was available in the environment. Hosted MCP initialization
also returned 401. No account was created or new access granted.

While this task was running, upstream `main` adopted private usage accounting
in `a1ccbdb`. The rerun path now honors that requirement and refuses a direct
fallback. A [later accounted access check](results/accounted-access-2026-10-15.json)
still returned 401 for all three dependencies. Accounting was available;
credentials were not. Historical model results and their original transport
records were retained without alteration.

Those arms are **unavailable**, with `solved: null`; they are not failed fixes
and do not enter a success-rate denominator. Public OpenAPI access does not
prove authenticated package retrieval. The positive retrieval integration
is implemented against that schema and tested offline, but remains
unverified against an authenticated live response.

## Limits and next step

The model was a bounded JSON patch proposer with tools disabled. The host
would add preselected GitHits source excerpts in the treatment. This does
not measure unrestricted coding agents, agent-driven MCP retrieval, or a
GitHits-specific advantage over equal-size ordinary documentation.
The CLI reports the requested model alias, not an immutable served snapshot.
Calls are matched; input tokens and dollars are not. No dollar estimate or
latency comparison is asserted. Host: Windows 11, CPython 3.12.14, Ryzen 7
9800X3D, about 32 GB memory; full conditions are in the evidence.

The baseline saturated this task set, so it has no remaining room to show
more fixes. Supply an approved `GITHITS_API_TOKEN` and complete the existing
paired run to verify the integration. To test uplift, preregister a harder,
blinded task set before running either arm. Retain this result.

Run 001 is also retained, with its
[Windows prompt-capture limitation](results/2026-10-15-run-001-NOTES.md).
Run 002 repeated the unchanged tasks after fixing capture; the runs are not
pooled. All ambiguous choices are in [DECISIONS.md](DECISIONS.md).

## Reproduce and validation

[README.md](README.md) gives the environment, commands and evidence format.
The recheck needs the pinned dependencies and Git, with no model, token or
network. [VALIDATION.md](VALIDATION.md) records the repository checks and the
stock Windows clean-install failure, including the passing environment
workaround. Host checks are controlled subprocess execution, not a sandbox.

This task pushed only `bench/githits-verified`. No pull request, merge,
deployment or write to `main` is part of this work.
