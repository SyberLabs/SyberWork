# GitHits x SyberWork benchmark decisions

Owner: Seth Carlson. Acting agent: Codex. Branch: `bench/githits-verified`.
Claim ceiling at start: proposed; no model comparison measured.

## 2026-10-14, before running models

1. The supplied prompt ends immediately after the deliverables heading. Use
   the explicit research question as the scope: first a reproducible paired
   benchmark with host-owned checks; then a real run if access permits; then
   a concise report with the evidence and limitations. Ship in that order.
2. Work from upstream `62ef58d016f4adf0bdcf91cf53cfc48c573b9877` in a
   fresh clone. Use the explicitly requested branch name, which overrides the
   normal agent-platform branch prefix. Never merge or open a pull request.
3. The user's direct request authorizes this bounded benchmark, its model
   invocations using existing access, commits, and pushing this branch. It
   does not authorize purchasing access, creating accounts, publishing a
   release, changing repository settings, or contacting anyone. Reports
   remain draft meeting material; an empirical result is not endorsement.
4. Started around 23:48 PDT on October 14. Finish within six hours, with
   06:00 PDT October 15 as the absolute ceiling. Aim to commit well before
   either limit. Do not schedule work after the deadline.
5. Reuse `Kit`, the search protocol, the adapter's pure prompt helpers,
   repair feedback, and `syberlabs.checks.run_check`. Do not change admission,
   the protocol, or create a sandbox subsystem for an unspecified deliverable.
   Host subprocess checks are controlled execution with developer permissions,
   not a security sandbox.
6. Compare one fixed model and configuration, identical starting files,
   dependency pins, budgets, repair feedback, and host checks. The treatment
   may add only version-pinned GitHits dependency context. Preserve the
   exact requests, responses, checks, candidate commits/trees, timings, and
   failures. Never count model assertions as host evidence.
7. Prefer a few curated dependency API migration/regression tasks using real
   published packages. Identify them as authored tasks unless an upstream
   issue/commit is actually reproduced. Establish broken baselines and an
   independently authored reference fix before measuring model responses.
8. Access preflight: the supplied OpenAPI document is publicly reachable.
   An unauthenticated package request returns HTTP 401 with code
   `AUTHENTICATION_REQUIRED`. No GitHits token name is present in the process,
   user, or machine environment. Codex CLI reports an existing ChatGPT login;
   no Anthropic key is present. Check existing authorized access, never invent
   credentials or substitute ordinary docs for GitHits treatment data.
9. An unavailable GitHits arm is unavailable, not a failed fix, zero percent,
   a tie, or evidence of improvement. Record zero completed pairs and no
   comparative conclusion if authentication cannot be resolved.
10. The existing signed-in Codex CLI is the available model route. Pin the
    requested model to `gpt-6.1-sol`, effort `high`, maximum two calls per arm,
    one repeat initially. It proposes JSON files from an empty directory with
    tools disabled; the host runs the checks. The first result is a bounded
    patch-provider experiment, not a measurement of unrestricted coding agents.
    The CLI does not expose an immutable served model snapshot in its events.
11. GitHits treatment is deterministic, preselected source retrieval through
    REST. It does not measure the model's retrieval decisions. Both arms have
    identical call limits, not identical input-token counts. An additional
    ordinary-documentation control would be needed to isolate GitHits-specific
    value from the benefit of supplying relevant extra context.
12. The hosted MCP initialization also returns HTTP 401. The browser opens an
    unauthenticated sign-up page requiring acceptance of terms; do not create
    an account, accept terms, or grant a new OAuth app access. The public index
    exposes package metadata but was not used as authenticated REST source.
13. Before any benchmark model run, all three real-dependency baselines failed
    behavior checks with correct package versions, and all three held-out
    reference fixes passed host checks and admission preview. Nine offline
    boundary tests passed. Store the dated validation evidence with the report.
14. Run 001 at 00:00 PDT October 15: all three baseline tasks solved on the
    first model call, each with host-verified version and behavior checks.
    All three GitHits reads returned `AUTHENTICATION_REQUIRED`. Completed
    matched pairs: zero. Do not add tasks after seeing this ceiling merely
    to search for a favorable headline. A harder task set would be a new,
    explicitly preregistered experiment, with this result still retained.
15. Preserve raw evidence bytes with a run-local `.gitattributes`; otherwise
    Windows Git line-ending conversion invalidates artifact SHA-256 hashes.
    Save self-contained Git bundles and provide a model-free host recheck.
    A replay compares pass/fail states and exit codes; timings and temporary
    paths make byte-for-byte check-output replay inappropriate.
16. Before publishing the evidence, found that Windows text-mode output
    converted line endings in saved prompts. Fix capture to write the exact
    UTF-8 bytes passed to model stdin; add a regression test; retain run 001
    unchanged with a capture-limitation note; repeat the unchanged task set
    as run 002 for the final report. Do not pool the runs or choose outcomes.
    Raw archives intentionally retain their whitespace; Git whitespace lint
    remains active for source and documentation outside those archives.
17. The standard clean-install check fails on this Windows runtime because
    its minimal environment drops `SYSTEMROOT`, preventing SSL context
    initialization. A minimal reproduction fails without it and passes with
    it. Running the same wheel/origin/example checks with only `SYSTEMROOT`
    preserved passed. Keep the unrelated conformance source unchanged and
    report the standard-command failure alongside this workaround.
