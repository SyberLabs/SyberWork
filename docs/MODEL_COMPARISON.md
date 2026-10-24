# Model-backed search versus a single model patch

The question: at the same model cost, does searching (independent samples, repair with check feedback, or EvoGit-style evolution) produce an acceptable change more often than one model patch? One pilot against a real model exists (2026-10-21, below) and answers nothing about the arms, because its fixtures were too easy. What exists is the apparatus to answer it honestly, run against a simulated model and once against Claude Sonnet 5.5.

## What is compared

`benchmarks/model_vs_patch.py` runs every arm through the same Build Thread. Every candidate is host-evaluated and recorded, and "solved" means a candidate that passed every required check *and* that admission would let a person accept. Every arm stops at its first passing candidate, so its call count is its cost to first solve, capped at K.

| Arm | Model calls | What the model sees |
| --- | --- | --- |
| `single` | 1 | Objective, scope, and files in scope. This is the baseline. |
| `best_of_k` | up to K | The same as `single`, K independent times |
| `repair` | up to K | After the first call, the previous candidate's files and the host's failing check output |
| `evolve` | up to K mutator calls | One parent's files per call. Crossover and selection cost no model call, but do cost evaluations. |
| `evolve_empirical` | up to K | The same as `evolve`, branching by `EmpiricalPolicy` (see Policies below) |
| `evolve_jev` | up to K | The same as `evolve`, branching by `JevPolicy` (see Policies below) |

Cost is matched on **model calls** (K). Tokens are reported per arm from the adapter's own usage, and dollars are computed from them with the price table in the script. Evaluations (check runs) are reported separately, because they cost compute, not model spend.

## Fixtures

`benchmarks/fixtures.py` holds three small repositories: pricing rules, a slug function, and interval merging. Each has three required checks. Several of the checks interact: interval merging needs ordering, overlap, and joining to agree. They are small on purpose so a run is cheap. They are not real repositories, and a result on them says little about one.

## Running it with Claude

```sh
pip install anthropic                     # only the adapter needs it; syberlabs has no dependencies
export ANTHROPIC_API_KEY=...              # or: ant auth login
PYTHONPATH=. python benchmarks/model_vs_patch.py \
    --adapter "python $PWD/adapters/anthropic_adapter.py" --calls 6 --repeats 3 \
    --write benchmarks/results/model-vs-patch-claude
```

`adapters/anthropic_adapter.py` uses the official SDK. It defaults to `claude-opus-5` at effort `high` (set `SYBERLABS_MODEL` and `SYBERLABS_EFFORT` to change them), with structured JSON output, the server-side refusal fallback (`fallbacks: "default"`), and each call's token usage returned to the host.

**This spends money.** The run above makes at most 3 fixtures × 3 repeats × (1 + 6 + 6 + 6) = 171 calls, and fewer when arms solve early. On these fixtures a call is a few thousand tokens in and a few thousand out, including thinking, so budget on the order of tens of dollars at Claude Opus 5 prices. Measure one repeat first. The script reports the actual tokens and dollars.

## Results: simulated model (harness check only)

`benchmarks/simulated_model.py` is not a model. It edits each fixture's known slots with fixed probabilities: a patch gets each slot right with p = 0.45, a repair fixes each failing slot with p = 0.6, and a mutation re-rolls one slot with p = 0.35 of being right. Its "tokens" are text length divided by four. The numbers below therefore test the harness and describe those probabilities. They are not evidence about any model, and in particular not about whether a real model's repair uses feedback well.

From `benchmarks/results/model-vs-patch-simulated.txt` (K = 6, 10 repeats per fixture and arm):

```
SIMULATED MODEL: these numbers describe the harness, not a model. Budget K = 6 model calls per run; every arm stops at its first passing candidate.
fixture     arm         solved  calls(med)  tokens in/out (mean)   evaluations(med)  $ (mean)
pricing     single       2/10         1.0        150/102                  1.0        n/a
pricing     best_of_k    9/10         3.5        540/373                  3.5        n/a
pricing     repair      10/10         2.0        493/209                  2.0        n/a
pricing     evolve       1/10         6.0       1208/439                  7.0        n/a
slugify     single       1/10         1.0        171/128                  1.0        n/a
slugify     best_of_k    6/10         4.0        684/514                  4.0        n/a
slugify     repair       9/10         3.5       1116/424                  3.0        n/a
slugify     evolve       0/10         6.0       1491/590                  7.0        n/a
intervals   single       1/10         1.0        206/166                  1.0        n/a
intervals   best_of_k    6/10         5.5        907/726                  5.0        n/a
intervals   repair       6/10         4.0       1582/640                  3.0        n/a
intervals   evolve       0/10         6.0       1921/813                  6.0        n/a
```

What this shows about the **harness**: every arm stays within its call budget, stops at its first solve, and records tokens and evaluations; `solved` is decided by the host's checks and admission.

What it shows about **these simulator settings**, and nothing more:
- repair and best-of-K solve far more often than a single patch at up to 6 calls;
- evolution rarely solves at K = 6, because its whole call budget goes into single-slot mutations for the initial population and crossover then has too little to combine.

A real model's mutations and repairs may behave quite differently, so none of this predicts which arm wins with Claude.

## Results: pilot of 2026-10-21 against Claude Sonnet 5.5 (measured)

Run by the orchestrator under the owner's "ready for experimentation" (session 2026-10-21), following `docs/E3_BENCHMARK_PLAN.md`: SyberWork revision `c507871204d62aeed924db3882b85715e98554c6`, `SYBERLABS_MODEL=claude-sonnet-5-5`, `SYBERLABS_EFFORT=medium`, K = 6, one repeat (the full run's first), Python 3.12.6 on Windows 11, adapter `adapters/anthropic_adapter.py` at SHA-256 prefix `a736f5a1a3992ac0` (the file at that revision hashes to the same prefix). Rows and summary: `benchmarks/results/model-vs-patch-sonnet55-pilot.json` and `.txt`, copied unchanged from the run. The rows carry fixture, arm, seed, `solved`, `calls`, `input_tokens`, `output_tokens`, `dollars`, `simulated`, `candidates`, `evaluations`, `seconds`; they do not carry `usage.model`, so the served model is not confirmed from the committed file. What the file does show: every row has a non-null `dollars`, so every served model was in the harness's price table, and each row's dollars equal its tokens at $2.00 / $10.00 per million, the Sonnet 5 and 5.5 tier and no other in the table.

```
Budget K = 6 model calls per run; every arm stops at its first passing candidate.
fixture     arm         solved  calls(med)  tokens in/out (mean)   evaluations(med)  $ (mean)
pricing     single       1/1            1        654/268                    1     0.0040
pricing     best_of_k    1/1            1        654/265                    1     0.0040
pricing     repair       1/1            1        654/271                    1     0.0040
pricing     evolve       1/1            6       4113/1650                   1     0.0247
slugify     single       1/1            1        683/334                    1     0.0047
slugify     best_of_k    1/1            1        683/331                    1     0.0047
slugify     repair       1/1            1        683/326                    1     0.0046
slugify     evolve       1/1            6       4287/2005                   1     0.0286
intervals   single       1/1            1        718/396                    1     0.0054
intervals   best_of_k    1/1            1        718/386                    1     0.0053
intervals   repair       1/1            1        718/387                    1     0.0053
intervals   evolve       1/1            5       3747/2060                   4     0.0281
```

Totals, summed from the JSON rows: 12 runs, 12 of 12 solved, 26 model calls, 18,312 input and 8,679 output tokens, **$0.1234 at list price**, 178.6 s of wall time. Per arm: `single` 3 calls, $0.0141; `best_of_k` 3 calls, $0.0139; `repair` 3 calls, $0.0140; `evolve` 17 calls, $0.0814. Run latency is 6.4 to 7.4 s for the one-call arms and 36 to 40 s for `evolve` (p50 and p95 are not meaningful on one repeat). `calls - candidates` is 0 for every one-call arm, 5 for `evolve` on `pricing` and `slugify`, and 1 for `evolve` on `intervals`.

**Decision by the plan's rule: uninformative.** The plan's rule is that the result is uninformative when `single` solves 8 or more of 9 runs, because fixtures the model solves first time leave no room for search to beat it. The pilot is the first of the three repeats: `single` solved 3 of 3, each in one call with 268 to 396 output tokens, and so did `best_of_k` and `repair`, each stopping at its first candidate as the harness requires. `evolve` reached the same answer at 5 to 6 calls and about six times the cost, because the provider's fill phase keeps calling the mutator until the population holds `min(4, K)` individuals or the mutator is exhausted, and only then asks whether one of them already passes (`FixedPolicy` in `syberlabs/evolve.py`); on `pricing` and `slugify` five of its six calls produced no registered candidate (a duplicate of the first, a no-change or an invalid response; the rows cannot distinguish these, and the per-call reasons were in the thread journals the harness writes to a temporary directory). Nothing in this says one arm is better than another. The two remaining repeats were not bought: they could only add single-shot failures on tasks every arm solved at its first candidate, which is not the comparison E3 asks for. No keep-or-park decision on `evolve.py` is drawn from this run, as the plan requires.

**Claim ceiling: measured, on three toy fixtures, one model, one effort, one repeat.** It establishes that the apparatus runs end to end against a real model with real usage accounting and host evaluation, and that these three fixtures are too easy for Claude Sonnet 5.5 to serve as the E3 benchmark. It says nothing about search on a real repository, about another model or effort, or about `exchange.py`.

**Two process findings.**

1. The plan's commands passed the adapter as `python adapters/anthropic_adapter.py`, a relative path. `benchmarks/model_vs_patch.py` runs the provider with the fixture worktree as its working directory (`os.chdir(root)` for the patch arms, `CommandMutator(cwd=root)` for `evolve`), where that path does not exist, so the first pilot attempt recorded twelve calls with zero tokens and zero candidates and spent nothing. The plan and the command above now pass an absolute path.
2. Those twelve rows looked like results: `run()` prices an empty usage list as $0.0 and the summary printed `0/1` with zero tokens and no error. The `Rejected("provider_failed")` from `run_json` was recorded only in the thread journal's `search_finished.error`, which the harness then discarded with the temporary directory. The row now carries `provider_error` (the `provider_*` code from that event, or `null`); the summary text does not show it yet, which is the next harness packet.

**Next fixtures.** The fixtures that make E3 informative are tasks from this repository's own history where single-shot is known to fail or to need a second look, each with checks a host can run on Windows and POSIX:

- the CRLF snapshot defect fixed in #24 (`541a519`): a candidate's bytes hashed differently from what Git stores under `core.autocrlf`, fixed with `git hash-object --path`;
- the Flyspace lessons in `docs/CONSUMERS.md`: `npm` not starting on Windows (`npm.cmd`), a worktree without `node_modules`, a 117 KB passing output counted as `error` under the 65,536 cap, a cp1252 console raising on a check mark, and a timeout leaving npm's children alive;
- the process-death windows #48 drives through `tests/test_properties_journal.py`: a death right after an append, between `propose`'s two appends, and an executor's death before and after the destination write, each of which recovery must settle exactly once;
- the Windows assumptions `AGENTS.md` forbids and the kit has had to remove: bare interpreter names (#26), `fcntl`-only locking (#34, `msvcrt.locking`), executable resolution for Node, Rust and Go repositories (#51).

Four of these are now fixtures (the note below). Each becomes a fixture by taking the pre-fix tree, the objective from the pull request, and the test that the fix added as the required check. Until such a run exists the comparison between the arms is not measured.

## Note of 2026-10-21 (later the same day): fixtures v2 exist and are unrun (superseded the same day: see the v3 note below)

`benchmarks/fixtures.py` now holds four more fixtures under `V2`, built from defects this program hit rather than from toy functions: `crlf_export` (the Flyspace `fieldHarness` CRLF comparison), `oauth_state` (the Flyspace `oauthConnect` one-in-N no-op tamper and a prefix comparison), `settlement` (the #35/#38 two-append crash window) and `tool_path` (the #51 bare-name launch under a scrubbed `PATH`). `docs/E3_BENCHMARK_PLAN.md` ("Fixtures v2") names each one's checks, provenance, the pilot command and the recomputed worst case ($13.98 for the 76-call v2 pilot at a 12,000-token input ceiling; the full run at three repeats does not fit the margin at that ceiling and falls back to two).

They are unrun against any model. What exists is harness evidence only: with the simulated adapter, whose slots for these fixtures hold no correct variant, the run of 2026-10-21 (K = 6, 3 repeats, all arms) solved 0 of 48 runs, and `tests/test_model_comparison.py` holds a reference fix per fixture under which every check passes. Whether a real model solves them single-shot is exactly what the v2 pilot measures, and nothing here predicts it.

Two harness changes from the pilot's process findings are in: rows name the served model (`model`, from `usage.model`, or `simulated`) and the summary prints `provider_error` and the model per arm; and with `--write` every run's thread journal is exported beside the rows (`<prefix>-journals/<fixture>-<arm>-<seed>.json`, named in the row's `journal`), so the per-call reasons that the pilot discarded with its temporary directory survive the run.

## Note of 2026-10-21 (later still): fixtures v3, built so that one call is unlikely to be enough

The v2 pilots answered the question v2 was built for, and the answer closes it: at Claude Sonnet 5.5 (medium, K = 6, one repeat) the four v2 fixtures were solved 16 of 16, every one-call arm at its first candidate, and at Claude Haiku 5.5 the `single` arm solved 8 of 8 over two repeats, both on 2026-10-21 (the session's run logs; the v2 rows are not committed on this branch and belong to the pilot-results packet). With v1 at 12 of 12 (above), both generations are uninformative by the plan's rule. The reason is in `adapters/anthropic_adapter.py: build_prompt`: the model sees the objective, the mutable scope and the files the host lists, and it sees a check's output only in the `repair` arm's feedback. A defect that is visible from the objective and the files is one model call away, whatever its provenance.

**The v3 design rule.** `benchmarks/fixtures.V3` holds four fixtures built so that what the checks require is *not* visible from the objective and the files, while the checks' output, when they fail, says enough for a careful second attempt to converge:

- the objective states the symptom or the requirement at the level a real issue would ("every cross reference must follow the renumbering", "verify must report the layers in a fixed format"), never the fix;
- each check pins a convention that is reasonable but not derivable from the visible files alone, chosen because this program's own history pinned it: an exact line format, canonical JSON with sorted keys and compact separators, the order of three calls, which of several plausible sites across three or four files is the right one;
- a failing check's assertion message names the expectation (the expected lines, the expected hash and the fields it digests, the expected call order), so `repair` and `evolve`, which read check output, can win where `single` has to guess;
- each fixture has three or four independently checked parts, so partial credit exists and `best_of_k` and `evolve` have a gradient;
- no part is solvable by the simulated model (`correct: []`), and every fixture runs its checks in a few seconds, on Windows and POSIX, with no new dependency.

| Fixture | Provenance | Parts | What the checks pin that the files do not show | The plausible wrong reading (and the part that catches it) |
| --- | --- | --- | --- | --- |
| `gaps_renumber` | #58 (`db87875`): GAPS renumbered, references updated in docs, two module docstrings and a test docstring | `sequence`, `references`, `code`, `testdoc` | that references in a docstring, a test docstring and a function's return value follow the renumbering, and that numbers which are not references (a plan step "4 of 7", a byte count, a date, `range(7)`) stay | a textual substitution of each old number by its new one (`references`) |
| `journal_chain` | `syberlabs/events.py`, `syberlabs/canonical.py`: the journal's hash chain | `canonical`, `genesis`, `chain` | sorted keys at every level, separators `,` and `:`, `ensure_ascii=False`, a genesis `previous` of 64 zeros, the six digested fields; three recorded hashes produced by the kit's own functions | `json.dumps(value, sort_keys=True)` with the default separators and escapes, and `None` as the first `previous` (`canonical`, `genesis`) |
| `worktree_order` | #61 (`e19c74b`), superseding #59: `gitspace._remove_worktree` | `order`, `retry`, `tolerated` | remove, then rmtree, then prune; the retry around the removal only, six attempts, sleeps 0.1 s doubling to 1.0 s; a failing prune tolerated; never raises | the retry wrapped around all three operations, which is indistinguishable until the removal keeps failing and then leaves the directory and the registration behind (`tolerated`) |
| `verify_layers` | `syberlabs/certificate.py: format_report`, the lines every packet receipt quotes | `passing`, `failing`, `skipped` | label left-justified to 10, then `ok` and three spaces, `skipped` and two, or `FAIL code` and two with the trailing space stripped; stop at the first failing layer; `verdict    pass (x and y not checked)`, `verdict    fail: reason` | the right words with single spaces and every layer printed after a failure (`passing`) |

**What is measured, and what is not.** `tests/test_model_comparison.py` (`FixturesV3`) holds a reference fix per fixture under which every check passes, shows that every check fails on the broken source with an assertion message, and shows that each wrong reading above fails its named part with a message containing the expectation; it also re-derives the chain vectors from `syberlabs.events` and the verify lines from `syberlabs.certificate`, so the pinned conventions are the kit's and not the fixture author's. With the simulated adapter, `single` and `repair` at K = 6 solved 0 of 8 runs on 2026-10-21 (the stand-in knows only line swaps). None of this says what a model does: whether Claude Sonnet 5.5 or Haiku 5.5 solves a v3 fixture in one call, and whether `repair` or `evolve` reaches a pass from the checks' output, is what the v3 pilot measures. The honest expectation is that `gaps_renumber`, whose conventions are mostly visible in the files, is the easiest of the four and may still fall single-shot; it is kept because the non-reference numbers are the trap #58 had to step around.
## Policies: the three `evolve*` arms (simulated, not evidence about a model)

`benchmarks/model_vs_patch.py` has three evolutionary arms that differ in the branch policy of `syberlabs.evolve` and in nothing else: `evolve` (`FixedPolicy` revision 2, the first passing seed or child from `base` ends the search), `evolve_empirical` (`EmpiricalPolicy`, the fixed schedule with the round kind learned by Thompson sampling, salted with the run's seed) and `evolve_jev` (`JevPolicy`, each action bought by expected admission per dollar). All three get the same mutator, population `min(4, K)`, 200 generations and the same K; every row records `policy` (name and revision, from the provider's trace, the same as every child's `signal.policy`). The harness now prices the fixture contract (`budget.prices` from its table plus a nominal Sonnet-tier rate for the simulated model, `budget.max_dollars` 50 per run, far above K calls at any rate), so Jev runs priced, its price prior for a mutation comes from the contract (`jev_prices`), and the host observes every call's spend; the row's `host_dollars` is that observation. For the simulated model it is a token count in dollar units, not money, and the row's `dollars` stays null.

`benchmarks/simulated_model.py` takes a per-operator success profile for the mutate protocol (`SIM_PROFILE`, the harness's `--profile`): `mutation_rare` (a mutation of `base` fixes one slot; a mutation of a child fixes one of its remaining slots one time in five and otherwise rewrites one remaining slot wrong; children fix different slots, so a crossover combines them) and `mutation_common` (a mutation rewrites every remaining slot, each right three times in five; children conflict in every slot, so a crossover combines nothing a mutation would not). A crossover never calls the stand-in, so its success in a profile is structural, not a rolled probability. Without a profile the stand-in behaves exactly as before, draw for draw (checked against the previous version on 300 requests across three fixtures and both protocols).

**The table below is simulated. It is not evidence about a model.** It describes the harness, the three policies and these two profiles. K = 12, seeds 1 to 5, the three v1 fixtures, from `python benchmarks/model_vs_patch.py --profile <name> --arms evolve evolve_empirical evolve_jev --calls 12 --repeats 5 --fixtures pricing slugify intervals` on 2026-10-22; `calls` and `host$` are sums over the five seeds, counted to the first passing candidate and capped at K.

```
profile          fixture    arm               policy       solved  calls calls(med)    host$  evals
mutation_rare    pricing    evolve            fixed/2        2/5      60         12   0.0698     45
mutation_rare    pricing    evolve_empirical  empirical/2    4/5      41          8   0.0473     40
mutation_rare    pricing    evolve_jev        jev/1          5/5      20          3   0.0230     27
mutation_rare    slugify    evolve            fixed/2        3/5      60         12   0.0900     52
mutation_rare    slugify    evolve_empirical  empirical/2    5/5      37          8   0.0551     44
mutation_rare    slugify    evolve_jev        jev/1          5/5      20          3   0.0296     27
mutation_rare    intervals  evolve            fixed/2        1/5      60         12   0.1199     21
mutation_rare    intervals  evolve_empirical  empirical/2    1/5      60         12   0.1199     21
mutation_rare    intervals  evolve_jev        jev/1          5/5      21          3   0.0421     26
mutation_common  pricing    evolve            fixed/2        5/5      16          2   0.0193     15
mutation_common  pricing    evolve_empirical  empirical/2    5/5      16          2   0.0193     15
mutation_common  pricing    evolve_jev        jev/1          5/5      14          2   0.0169     16
mutation_common  slugify    evolve            fixed/2        5/5      25          5   0.0384     23
mutation_common  slugify    evolve_empirical  empirical/2    5/5      25          5   0.0383     24
mutation_common  slugify    evolve_jev        jev/1          5/5      23          5   0.0354     28
mutation_common  intervals  evolve            fixed/2        5/5      28          8   0.0576     23
mutation_common  intervals  evolve_empirical  empirical/2    5/5      28          8   0.0576     26
mutation_common  intervals  evolve_jev        jev/1          5/5      18          4   0.0368     19
```

What it shows about the **harness and the policies on these profiles**: on `mutation_rare`, where a crossover combines what two mutations fixed, Jev buys crossovers as soon as two individuals exist and reaches a passing candidate in fewer model calls and fewer simulated dollars than the fixed schedule on every fixture, and Empirical needs fewer calls than the fixed schedule on `pricing` and `slugify` and the same on `intervals` (the artifact below) (`tests/test_model_comparison.py`, `PolicyArms`, asserts exactly these three facts on `pricing` over the five seeds). On `mutation_common`, where mutation alone solves the task, every policy solves every seed, the fixed schedule and Empirical take the same decisions (most seeds are solved inside the fill, before a round kind is chosen), and Jev needs fewer calls than both on each fixture, at the cost of more evaluations on `slugify`. One artifact to read with it: on `intervals`, whose three slots each hold a single correct variant, only three distinct children of `base` exist under `mutation_rare`, so the fixed schedule's fill of four spends its remaining calls on duplicates and never reaches a round; that flatters the learning policies there and is a property of the stand-in (`spec/GAPS.md` 43).

**Measurement procedure and decision rule**, copied from the packet spec (`jev-policy`, "Measurement"): "Run the three `evolve*` arms plus `single` on the v2 fixtures (step 2) at K = 6, one repeat first, Sonnet 5.5 medium. The decision rule for Jev: it is kept as the provider's default if, on the v2 fixtures, `evolve_jev` solves at least as many as `evolve` with at most half the dollars, over the three repeats; otherwise it stays available and not default. Budget line: **$15** of the $50 cap (see the allocation in the review). The run is a named human's act with the key in that shell, or the orchestrator's under the owner's standing "ready for experimentation", stated in the results packet either way."

**Where that run goes.** The v2 set is uninformative at every tier measured on 2026-10-21: `single` solved all four v2 fixtures at Claude Sonnet 5.5 (16 of 16 runs, medium, K = 6, one repeat) and at Claude Haiku 5.5 (8 of 8, two repeats, about $0.003), so a comparison on v2 could only add single-shot failures on tasks every arm solves at its first candidate. The paid Jev-against-fixed comparison therefore goes to a set on which `single` fails, the v3 fixtures (a parallel packet), by the owner's decision; until it is run and reported, `fixed` stays the provider's default and the simulated table above is not evidence for the rule.

## Note of 2026-10-22: every `evolve*` row so far was taken with an empty scope in the mutate request

All `evolve*` rows before the `mutate-scope` change (the pilot of 2026-10-21, the v2 runs of 2026-10-21, and the v3 control and Jev repeat 1 of 2026-10-22) were taken with an empty scope in the mutate request; those numbers understate the evolve arms and are superseded by runs after this change. The cause: `CommandMutator` sent `{protocol, objective, parent, files, context, seed}` with no `scope`, while the search request carried one; `adapters/anthropic_adapter.py` printed `Mutable scope:` followed by nothing for both protocols, and its system prompt told the model to change files only within the stated scope, so the model returned `changes: {}`. The host counts an empty proposal as a call that produced no child and writes nothing to the journal for it, which is how an `evolve*` arm could make K calls and register zero candidates on a fixture (`verify_layers`: 6 calls, 0 evaluations, in all three policies on 2026-10-22). Measured on one hand-made mutate request for `verify_layers` against Claude Sonnet 5.5: without `scope`, 65 output tokens and no files; with `scope` set to the file list, a real patch. The `single` rows are unaffected (the search request always carried `scope`). After the change the mutate request carries `scope` (the evolution scope's paths, the same list as the search request), and the adapter says `Mutable scope: the files shown below` whenever a request states none, so a mutator that is handed no scope is no longer told to write nowhere. The `evolve*` numbers above are kept as the record of what was run; no number in this file is evidence for or against the evolve arms until they are re-measured.

Second note of 2026-10-22 (`mutate-feedback`): every `evolve*` row before this change, the runs after `mutate-scope` included (v3, Claude Sonnet 5.5, K = 6, one repeat: `repair` 4/4 in one or two calls each; `evolve`, `evolve_empirical` and `evolve_jev` 2/4 each, neither `worktree_order` nor `verify_layers`), was taken without feedback in mutations: the mutate request carried the parent's files and no check results, so every mutation was a blind re-sample under "Vary your approach", while `repair`'s second call carried the failing checks of its first. The mutate request now carries `feedback` (`docs/EVOLUTION.md`); the evolve arms are not re-measured in this change.

Third note of 2026-10-22 (`base-evaluation`): every `evolve*` row before this change, the rows after `mutate-feedback` included, was taken with an undirected fill: a mutation of `base` had no feedback because the host never evaluated the base tree, and under K = 6 with a population of `min(4, K)` the fill is most of the budget. The search now evaluates the base once before the fill (one evaluation, no model call; `docs/EVOLUTION.md`), so every fill request carries the base's checks; K counts model calls, so an arm's call budget is unchanged and each `evolve*` run makes one more evaluation, which this harness's `evaluations` column (counted from `candidate_evaluated` events) does not include. The evolve arms are not re-measured in this change.

## Note of 2026-10-22: fixtures v4, built so that recombination has a case to make

The v3 runs of 2026-10-22 at Claude Sonnet 5.5 (medium, K = 6, three repeats) read: `repair` 12 of 12 at $0.23, `evolve_jev` 12 of 12 at $0.37, `evolve_fixed` 12 of 12 at $0.47; the directed fill of #74 added tokens and no solves on one repeat. A v3 fixture is one file, three or four parts, one convention: a two-call repair closes it. Evolution's only remaining case is an objective whose parts are independent enough that recombination (crossover is model-free) and a population pay. Either that case exists and we measure it, or we stop paying for evolve arms.

**The v4 design rule.** `benchmarks/fixtures.V4` holds four fixtures of three to five files and six to eight independently checked parts, where (a) a single model call rarely fixes more than two or three parts, because the parts live in different files under different conventions and the objective states each area's symptom, not the fix; (b) the parts are separable: the reference fix of one part touches a region no other part's fix touches (`tests/test_model_comparison.py: PARTS_V4` holds each part as its own replacement and the oracle asserts the regions are disjoint), so a change that fixes part i does not break part j and two children that fixed disjoint parts crossover into a child that passes both; (c) a failing check's assertion message begins with the part's name and states the expectation, so a directed mutation of a partial child can target what is left; (d) no part is solvable by the simulated model (slots `correct: []`); (e) the oracle shows, per fixture, that the broken tree fails every part by name, that the reference fix passes every part, that two disjoint half fixes A and B (the parts at even and at odd positions, so most files hold a part of each half) each pass their half and fail the other half by name, that the union of A and B passes every part, and that a plausible wrong reading of two parts fails the named part and only it. Every file is under `src/`; every check runs in seconds on Windows and POSIX with no new dependency.

| Fixture | Provenance | Files | Parts | Why one call rarely fixes more than two or three |
| --- | --- | --- | --- | --- |
| `stack_landing` | the merge chains of 2026-10-20 and 2026-10-22 (the owner's chain script): one landing touched the GAPS closure notes, a packets table, a test docstring and a module constant | `chain.log` (the data), `gaps.md`, `status.md`, `test_landing.py`, `release.py` | `closed_entry`, `narrowed_entry`, `table_rows`, `head_line`, `test_doc`, `landed_order`, `head_constant` | four records in four conventions read from one log: the table and the constant in merge order, the docstring in number order, 7-character ids, `closed.` against `closed in part.` for a narrowed entry, and data that must not change beside each record |
| `certificate_layers` | `syberlabs/dsse.py`, `syberlabs/certificate.py`, GAPS entries 26 and 28: the envelope, the in-toto statement, the identity block and the three verify layers | `envelope.py`, `statement.py`, `payload.py`, `layers.py` | `pae`, `envelope_fields`, `statement_shape`, `subject`, `unwrap_codes`, `identity`, `target_ref`, `layer_order` | two public standards (the DSSE PAE, the in-toto Statement v1) and six of this program's own pins: the three unwrap codes and their order, the issuer block with `seed_env` as a name, `refs/heads/syberlabs/<8>`, `None` for a skipped layer and a stop at the first failure |
| `cell_metrics` | `syberwork/core.py: Work.metric` and `server.py` (GAPS 29), `syberlabs/inspector.py` (the incomplete rows of GAPS 33), `syberlabs/dashboard.py: TONE` | `counters.py`, `reading.py`, `rows.py`, `tone.py` | `cases_open`, `proposals`, `effects`, `version`, `etag`, `incomplete_row`, `refusal_status`, `tone` | eight rules of three services: which case is open, which decision counts, which reconciliation is applied, the digest a version is, a strong ETag beside `X-Record-Version`, the incomplete row's five fields and remedy, 404 against 409, and nineteen state colours plus the unknown-state rule |
| `thread_stations` | the Build Thread's stations: `build.py` status rows, `checks.py` (sorted order, the setup gate of GAPS 32), `search.py: Evaluation.score`, `evolution.py`'s promotion states, `publish.py`'s destinations, `certificate.summary` | `register.py`, `evaluate.py`, `promote.py`, `publish.py`, `summary.py` | `registered`, `check_order`, `setup_gate`, `score`, `promotion_state`, `acceptable`, `publish_lines`, `certify_summary` | five stations, each with its own recorded field: `name@revision`, sorted names, the five-field gated record, the score pair, the outcome-over-decision ranking, the `git:` and `github:` lines, and the certify line's exact prefixes |

The wrong readings the oracle holds: `stack_landing` writes `*2026-10-22, narrowed.*` (`narrowed_entry`) and `LANDED` in number order (`landed_order`); `certificate_layers` encodes the PAE lengths as 8-byte integers (`pae`) and puts the seed's value under `seed_env` (`identity`); `cell_metrics` sends a weak ETag (`etag`) and counts every reconciliation as applied (`effects`); `thread_stations` records a gated check as `skipped` (`setup_gate`) and drops the `unresolved` state (`promotion_state`). Each fails its part by name while every other part passes.

**Measurement after it lands, and the prediction, copied from the packet spec as written before any run.** Same five arms, K=6 then K=12, Sonnet 5.5 medium, one repeat then three. Prediction to be falsified, written before the run: at K=12 evolve_jev solves more parts per dollar than repair on v4; if it does not, the evolve arms are kept only as research and the default proposer path for the factory is repair.

**What is measured, and what is not.** With the simulated adapter on 2026-10-22 (K = 6, one repeat, arms `single`, `repair`, `evolve`), 0 of 12 runs solved: `single` 1 call and 1 evaluation on each fixture, `repair` 6 calls and 3 evaluations (median), `evolve` 6 calls and 2 evaluations (the stand-in knows only line swaps). `tests/test_model_comparison.py` (`FixturesV4`) re-derives the pins from the kit (`dsse.pae`, the certificate constants, `DEFAULT_TARGET_REF`, `certificate.summary`, `checks.SETUP_NAMES` and `EMPTY_DIGEST`, `PROMOTION_RANK`, `dashboard.TONE`, `inspector.INCOMPLETE_REMEDY`). Nothing here says what a model does. "Parts per dollar" in the prediction needs a per-part count the harness summary does not print (the per-part states are in each exported journal's `candidate_evaluated` events); that column belongs to the packet that owns `benchmarks/model_vs_patch.py` (`spec/GAPS.md`, 47). The `evolve_jev` arm is on #68's branch, so the v4 measurement runs on the stack, not on this branch alone.
Decision of 2026-10-23 (`repair-proposer`): on the v3 and v4 sets (Claude Sonnet 5.5, K = 6 and K = 12; the v4 note and its rows are in the `e3-v4-multipart` packet, #75, a parallel stack of 2026-10-22, with the receipts in the officer workspace) `repair` beat every evolve arm on solves and on parts per dollar at every budget tested, and beat `single` on solves; the prediction that evolution overtakes repair at a larger budget was falsified at K = 12 (42 against 65 parts per dollar, repair ahead). The owner's decision: the default proposer path is repair (`syberlabs.providers.RepairProvider`, `syberlabs propose --repair`), and the evolve arms stay as research. No row in this file is rewritten by that decision.

## What would count as evidence

A real-model run on these fixtures would show only whether the arms differ on toy tasks. The roadmap's bar is a real repository with several productive alternatives, a baseline, a failure case, and a decision that changed for a measured reason. Until that run exists, the claim in `docs/ROADMAP_STATUS.md` stays at what the pilot supports: **pilot measured 2026-10-21; uninformative by rule; harder fixtures next**.
