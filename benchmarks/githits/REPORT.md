# GitHits x SyberWork: two separate observed ties

**Draft benchmark brief — October 15, 2026. Owner: Seth Carlson. Agent: Codex.**

**Original task set: 3/3 host-verified fixes without GitHits, 3/3 with it
(three matched pairs). That tie remains unchanged.**

**Preregistered harder set: 5/5 without context, 5/5 with frozen GitHits context
(five of six planned pairs). No additional fixes were observed.**

**New upstream-bug set: preregistered, awaiting GitHits authentication.**
Four real upstream-derived cases are frozen at `0ab8736`; no new API or model
calls have run. [Status and remaining action](REPORT-upstream-v3.md) ·
[Preregistration](PREREGISTRATION-upstream-v3.md). This adds no comparative result.

All paired fixes in both sets passed on their first model call. The harder
set's T04 treatment is unavailable: pinned `pydantic-core 2.27.2` returned
`VERSION_NOT_FOUND`. Its standalone baseline solved on call two and is
excluded from matched scoring. It is not a failed treatment or a paired win.
The baseline solved every planned task within budget; neither set shows
headroom for more fixes. Missing treatment data do not establish parity.

[One-screen brief](phone.html) · [Original 3/3 tie and evidence](REPORT-run-004.md)
· [Harder-set report and every task outcome](REPORT-hard-v2.md)
· [Initial preregistration](PREREGISTRATION-v2.md) · [Pre-model amendment](PREREGISTRATION-v2.1.md)

## What was measured

The same bounded Codex patch provider proposed fixes with and without
preselected, pinned-version dependency source from GitHits. SyberWork ran
version checks, behavior checks and admission previews on exact candidate
Git trees. Model claims did not count. Each arm used the same base, model,
effort, call cap and checks within its task set. No product fix was accepted,
merged or pushed. This task pushed only the benchmark branch.

The newer set froze six authored tasks with 36 behavior test methods before
model trials. Its first preflight stopped before any model call because one
version was unavailable. A separately pushed amendment retained every task,
check, pin and source range, made that missing pair explicit, and reused the
already authenticated receipts. The model run used clean source `2895110`,
one repeat, requested `gpt-6.1-sol` at high effort and a two-call cap per arm.
No extra lookups or outcome-based task replacements were made.

Checks and reference fixes were hidden from the model, treatment branding was
removed and automatic grading was arm-unaware. This was **partial blinding**:
the operator knew the mapping and the model could notice extra context.
Outcomes were inspected only after the complete matrix finished.

## Claim ceiling

Both are small authored convenience sets, not representative upstream bugs.
Do not pool them into an eight- or nine-task rate or generalize an observed
tie into equivalence. The newer matched result is conditional on five available
contexts. There is no equal-size ordinary-documentation control, unrestricted
agent study, immutable served-model identity, or cost/latency comparison.

The harder-set replay verified all 265 artifact hashes and 24 saved tree
evaluations, including the failed first T04 baseline candidate. The original
12-tree replay and its raw evidence remain intact. These are host measurements
and same-host replays, not independent external validation or a sandbox.

[README.md](README.md) gives commands; [VALIDATION.md](VALIDATION.md) and
[DECISIONS.md](DECISIONS.md) record verification, limits and choices.
