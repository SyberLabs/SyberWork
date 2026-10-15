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
18. Final run 002 repeated the same three tasks at source `9e81b6b` with an
    empty source diff. All three baselines solved on the first call; all
    three GitHits reads returned 401; zero matched pairs completed. Archive
    the result and its successful model-free recheck before writing the
    report. Do not aggregate the repeated early run into a larger sample.
19. Final validation: 274 tests, 22 skipped, OK; 12 focused offline tests,
    OK; all 23 golden traces matched. CI passed for measured source `9e81b6b`.
    The one-screen HTML was rendered at 390 by 844 pixels without overflow.
    The report-only commit is a separate evidence boundary from that source.
20. Final claim ceiling: implementation and offline boundary checks tested;
    baseline fixes measured and reproduced on this host; authenticated
    GitHits retrieval and the causal comparison remain unmeasured. Stop at
    this concrete credential boundary rather than fabricating a treatment
    or spending the remaining window on an unregistered replacement task.
21. A portable branch bundle must include both the branch ref and `HEAD`,
    and Windows clones of deeply nested evidence need
    `--config core.longpaths=true` before checkout. A fresh clone using that
    setting and `core.autocrlf=true` passed the 86-file manifest verification
    and all nine host re-evaluations. Add the setting to the handoff; keep
    runtime code, fixtures and measurements unchanged.
22. Upstream main advanced externally to `a1ccbdb` at 00:27 PDT, after the
    report was first committed. It requires private usage accounting. Do
    not merge main or rewrite measured source/results. Adapt only the
    benchmark transport to call the approved private CLI wrapper, keeping
    private paths and the ledger out of public evidence and refusing a
    direct fallback. Its unit is CLI invocations, not API request volume.
23. The approved runtime paths became available as user environment values.
    No API token, default bridge OAuth store or alternative OAuth-store
    setting was present. All three real dependency reads through the wrapper
    returned `AUTHENTICATION_REQUIRED`. This later access check confirms
    the same credential boundary; do not synthesize treatment context or
    add a further model repeat. Positive authenticated retrieval is still
    unverified. Preserve the original pre-accounting runs as historical data.
24. After the accounting-only transport adaptation, the full suite passed:
    276 tests, 22 skipped, in 259.758 seconds; all 14 focused offline tests
    also passed. The primary model run, its fixtures, host checks, candidate
    objects and manifest remain unchanged. No private runtime or ledger
    files are included in the source branch or downloadable handoff.
25. The user explicitly supplied a token through a masked, temporary local
    environment and requested the paired report and branch push. Run 003
    obtained HTTP 200 for all three first-source reads, but our validator
    rejected their repository-backed identity shape. Preserve that complete
    attempt and its replay; do not count it as a GitHits model failure or pool
    its standalone baselines into the comparison.
26. In the real response, `resolved_requested` identifies the exact package;
    `served` identifies its repository, version and commit, with null package
    name/registry. Accept this shape only when the requested and resolved
    package match the fixture, freshness is current, and served repository,
    version and full commit match the resolved artifact. Continue rejecting
    fallback versions, missing commits, conflicting names and wrong paths.
    Two regression tests cover success and conflicting/missing identity.
27. Run 004 repeats both arms on the same three authored tasks, pins, source
    ranges, model, effort and two-call budget. A fresh token entry permits all
    six planned live reads through accounting. No source range or task was
    changed after viewing results. Run 004 began on `166d51f` with the two-file
    validator/test diff; preserve its exact stripped UTF-8 `git diff HEAD`
    bytes in `2026-10-15-run-004-source.patch`, SHA-256
    `348a4cd69bb41eea0d381ce2509672fa342bdc52c2c5e7be19287ef905322f2a`.
    Source commit `4188cac` contains that same implementation and patch. Do
    not describe the measured checkout as clean or silently replace its
    recorded source identity. The token is cleared when the launcher exits.
28. Run 004 completed three matched pairs: both arms solved all three tasks
    on their first calls. Report the observed tie and zero discordant pairs,
    not general equivalence or a GitHits advantage. Both the launcher's replay
    and the subsequent review replay verified all 140 artifact hashes and
    reproduced 12 saved tree evaluations. All six model configurations match;
    no tool-execution event was observed. The updated brief fits a 390 by 844
    CSS-pixel phone viewport without overflow.
29. The runner hashes stripped diff text, so preserve the source-diff bytes
    without a terminal newline to match `environment.json`. Document that
    one framing newline must be added before `git apply`. Applying that form
    to `166d51f` reconstructed both measured files exactly as committed in
    `4188cac`; the latter is the clean checkout for reproducing the code.
30. Follow-up validation passed: 278 local tests with 22 skips in 392.591
    seconds; 16 focused tests; 23 conformance golden traces; successful CI
    on source `4188cac`. Report/evidence publication is a later commit and
    remains a distinct revision boundary. Only the benchmark branch is pushed.
31. Fresh remote checkout `b5a2d98` preserved all evidence bytes. A deep
    checkout put one journal file at exactly 260 characters: Git's long-path
    setting handled it, but ordinary Python file access did not. Confirmed
    the file existed through the Windows extended-path prefix; a shorter
    checkout then verified all 140 hashes and replayed all 12 evaluations.
    Document the short-path requirement rather than editing archived data
    or weakening manifest verification. No runtime code changed for this.
32. The user requested a harder blinded follow-up. Define six new authored
    tasks across the same pinned HTTPX, Pydantic/core and SQLAlchemy families,
    six behavioral test methods each, without a model pilot. Higher complexity
    refers to interacting constraints, not a guaranteed lower solve rate.
    Keep run 004's 3/3-versus-3/3 tie prominent and unpooled. Freeze objectives,
    checks, references, reads, code hashes, model/budget and reporting rules
    in a pushed preregistration before the first model call.
33. Use partial blinding: hidden checks/references, opaque task/cell labels,
    no supplier branding in treatment input, and deterministic arm-unaware
    host checks. Do not claim independent authoring, double blinding or that
    a model cannot notice extra context. Do not inspect cell outcomes until
    the 12-cell matrix completes; raw rows remain saved for interrupted work.
34. During reference-only authoring, T04 initially failed its JSON-schema
    check. Correct the reference to emit forbidden-extra schema metadata
    through the supported JSON-schema hook before freezing; do not relax its
    acceptance checks. All six final references pass their 36 behavior methods
    and all six starting implementations fail behavior with correct versions.
    No new-suite model or GitHits calls preceded these authoring decisions.
35. The v2 authenticated preflight at source `291a7a1` produced ten successful
    reads for five tasks; T04's first pinned `pydantic-core 2.27.2` read returned
    HTTP 404 `VERSION_NOT_FOUND`. The all-context gate stopped with zero model
    cells. Preserve all 65 manifest-covered files under `hard-v2-access-001`.
    Do not replace the task, relax its checks, fetch a different version, or
    count the missing treatment as a failed model fix.
36. Publish v2.1 before any model trials: keep the same six task digests and
    configuration; allow exactly the observed T04 unavailable treatment and
    run all six baselines plus five available treatments. Report five possible
    matched pairs out of six planned and the standalone T04 baseline. Reuse
    the exact authenticated preflight contexts after manifest, identity and
    excerpt verification, avoiding duplicate traffic or another token entry.
    Freeze the receipt manifest digest in the amendment. Original v2 files
    remain unchanged. This availability-conditioned sample is an explicit
    limitation, not an estimate of the missing treatment or general coverage.
37. The complete harder matrix produced five matched pairs with 5/5 solves
    per arm, all on call one. T04's baseline solved on call two; its treatment
    stays unavailable with zero calls. Report 5/6 planned pairs, zero discordant
    pairs and all six baselines solved within cap. Keep the original 3/3 tie
    visible and separate. Do not add tasks, repeats or a reduced-call analysis
    after seeing outcomes, or interpret missing context as a failed model fix.
38. Review all twelve candidate patches and replay the exact saved trees.
    The replay verifies 265 hashes, 17 fixture/arm records and 24 evaluations,
    including the failed first T04 candidate. Source remains the clean frozen
    `2895110` implementation; publication adds documentation and evidence only.
    Describe higher authored complexity without claiming measured difficulty,
    full blinding, independent validation, general parity or product advantage.
    Publish both ties and the coverage gap in the report and phone brief.
39. The user requested real upstream dependency bugs. Select four traceable,
    pure-Python Werkzeug cases reproducible at one pinned version, using whole
    unmodified upstream modules rather than injecting authored bugs. Record
    source blob hashes, issue/fix links, BSD license and exact context hashes.
    This is one dependency's convenience sample, not a representative sample.
    No model pilot or outcome guided selection. Keep both existing ties visible.
40. Before registration, host authoring found two harness defects: Windows
    newline conversion inflated full-module diffs, and routing package imports
    needed initialization before module replacement. Preserve source bytes and
    initialize packages in host-only loading. An LF-only separator plus trailing
    payload CR is ambiguous; use an unambiguous LF-only regression case instead.
    U01's upstream reference also emitted a CR at a partial closing delimiter;
    extend its lookbehind by two bytes and retain that regression check. Label
    this reference as an authored extension, not an unchanged upstream repair.
41. Freeze four cases and 20 host test methods, same gpt-6.1-sol/high and
    two-call budget, eight cells/16-call maximum. Predeclare unavailable context
    handling: preserve every baseline, null unavailable treatments, matched-only
    scoring out of four planned. No replacement, alternate version or direct
    fallback. Authenticate five fixed reads after pushing registration, verify
    exact tag commit and line content, clear token, then reuse frozen receipts.
42. Extend the existing fixture, host runner and replay seams for full upstream
    modules and optional benchmark-only requirements. Keep all core product and
    protocol code untouched. Older hard-v2's whole-source guard intentionally
    rejects the newly added Python files; replay that retained set at publication
    commit 9abb733, where its original frozen source and evidence coexist.
    Do not weaken old registrations to make a changed implementation appear frozen.
43. After registration 0ab8736 was pushed, the required token was unavailable
    on this machine. Presence-only checks found no process/user/machine token
    and no callable authenticated connector. Close only the unused prompt we
    created. Do not issue avoidable unauthenticated reads, spend model calls
    on a baseline-only attempt, or manufacture unavailable rows without a run.
    Preserve the frozen set and prepare a portable launcher. This is a local
    credential blocker, not a new API coverage result or comparative outcome.
44. The user chose a ready-to-run handoff and objected to repeated token entry.
    Do not ask for the token again. The portable launcher reuses an existing
    environment credential when supplied, otherwise offers one masked entry
    only when explicitly launched. It retrieves context once, clears the
    credential before models, and reuses saved receipts for the whole matrix
    and replay. No credential persistence or new access is configured silently.
