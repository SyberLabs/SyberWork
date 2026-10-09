# E3: the comparison benchmark against a real model (plan)

Status: **plan only**. No model has been called under this plan. Budget cap: **$50**, with a 30% margin, so the planned worst case must stay under **$38.46**.

## What E3 is

MasterMind's state report (`docs/state/2026-09-29-state-of-the-system.md`, §9) registered it as:

> E3 | `benchmarks/model_vs_patch.py` once against a real model | ≤ tens of dollars | Keep or park `evolve.py` + `exchange.py` (simulation ranks evolve last but its probabilities are hand-set — not evidence)

and its errata (§1) narrows the gate:

> E3 (sections 9, 10) cannot decide `exchange.py`. [...] E3 gates `evolve.py` only; `exchange.py` (multi-host Git-remote migration) stays deferred until a real second-host need exists.

MasterMind PR #28 (the SyberFactory prime-goal record) names it the falsifier of thesis T3 ("Intent is a contract; coding is search"):

> **Falsifier.** Experiment E3 from the 2026-09-29 state report, still unregistered: one run of the comparison against a real model, for at most tens of dollars. If repair and evolution do not reach an acceptable candidate in fewer calls than single-shot at equal spend, the search machinery is cut back to single-shot plus repair.

> 4. **Register E3** with an owner and a date, and run it once against a real model. It costs tens of dollars, so a named human executes it (Class X).

`docs/ROADMAP_STATUS.md` lists the same item as **"Not measured with a model"**, and `docs/MODEL_COMPARISON.md` holds the only results so far, from `benchmarks/simulated_model.py`, which "describe the harness, not a model".

## The question

At a matched budget of K model calls, does a real model as proposer, driven through the Build Thread's search arms (`best_of_k`, `repair`, `evolve`), reach an **acceptable** candidate more often than the baseline `single` patch from the same model? "Acceptable" is the harness's definition: the host's own run passed every required check on the candidate's exact tree, and admission (`candidate.promotable` and the rest of the rule list) would let a person accept it. The model never reports a result; the host evaluates.

## Fixtures and cases

All from `benchmarks/fixtures.py` (`FIXTURES`), run through `benchmarks/model_vs_patch.py`, each with the contract from `benchmarks.fixtures.contract()` (scope `src/`, three required checks, operators `patch`, `mutation`, `crossover`):

| Fixture | File | Required checks |
| --- | --- | --- |
| `pricing` | `src/pricing.py` | `discount`, `tax`, `shipping` |
| `slugify` | `src/text.py` | `lower`, `collapse`, `trim` |
| `intervals` | `src/intervals.py` | `sorted`, `overlap`, `nested` |

Arms, all four from `ARMS`: `single` (1 call, the baseline), `best_of_k`, `repair`, `evolve` (each up to K calls). The conformance suite (`conformance/run.py`, 23 golden traces under `conformance/golden/`; `conformance/clean_install.py`) is run **before** the paid run on the revision being measured, as a precondition that the admission path is unchanged; it does not consume model calls. `tests/test_model_comparison.py` must pass on the same revision.

## Model and prices

One model, through the existing `adapters/anthropic_adapter.py` (official SDK, structured JSON output, `fallbacks: "default"`, usage returned):

- **`claude-sonnet-5-5`** (Claude Sonnet 5.5), effort **`medium`** (`SYBERLABS_EFFORT=medium`; the adapter's default is `high`, and the lower setting bounds thinking tokens, which bill as output).
- List price, Claude API first-party, from the local `claude-api` skill reference (cached 2026-10-06): **$2.00 per 1M input tokens, $10.00 per 1M output tokens**, cache reads $0.20. Verify against the pricing page on the run date.
- Not run: `claude-haiku-5-5` ($0.10 / $0.50 per 1M, same source). It is the fallback choice if the pilot shows Sonnet per-call tokens above the ceiling below.

Prerequisite, done in this PR: `PRICES` in `benchmarks/model_vs_patch.py` gains `"claude-sonnet-5-5": (2.0, 10.0)` and `"claude-haiku-5-5": (0.10, 0.50)`, so `dollars()` prices both models instead of reporting `n/a`. Checked: one call at the ceiling (4,000 in, 16,000 out) prices at $0.168 on Sonnet 5.5 and $0.0084 on Haiku 5.5, matching the budget below. Because `fallbacks: "default"` may serve a refused call on another model, `usage.model` is recorded per call; a served model missing from `PRICES` makes the row's dollars `None` and is itself a finding.

## Request count and token budget

Calls per run are capped by the harness: `single` = 1, each other arm ≤ K. With K = 6:

```
calls per (fixture, repeat)  = 1 + 6 + 6 + 6            = 19
pilot   (3 fixtures × 1 repeat)  = 3 × 1 × 19            = 57 calls
full    (3 fixtures × 3 repeats) = 3 × 3 × 19            = 171 calls   (pilot included; it is repeat 1)
```

Per-call ceiling. The adapter sets `max_tokens=16000`, so output (including thinking) cannot exceed 16,000 tokens. Input is the system prompt, objective, scope and one fixture file (the simulated run measured 150–2,000 input tokens per call; the repair arm adds a parent file and check output). Ceiling: 4,000 input, 16,000 output.

```
worst case per call   = 4,000 × $2.00/1M + 16,000 × $10.00/1M = $0.008 + $0.160 = $0.168
worst case, pilot     =  57 × $0.168 = $ 9.58
worst case, full      = 171 × $0.168 = $28.73
cap with 30% margin   = $50 / 1.30   = $38.46        ->  $28.73 < $38.46  (passes)
expected (3,000 in / 5,000 out per call: $0.056)     ->  171 × $0.056 ≈ $9.58
```

Every arm stops at its first passing candidate, so the real count is at most 171 and the real spend at most $28.73 at list price. Stop rules: the pilot's reported `$ (mean)` × 3 must be under $38.46 or the full run is not started; if the pilot's mean output tokens exceed 8,000 per call, switch to `claude-haiku-5-5` (worst case 171 × $0.0084 = $1.44) rather than raise the cap. No second model, no K > 6, no repeats > 3 under this budget.

## Baseline, metrics, thresholds (set before the run)

Per `MasterMind/docs/CLAIMS_AND_EVIDENCE.md`, a claim that model assistance improves a workflow needs a baseline, cost/latency accounting, a predefined metric, and failure analysis.

- **Baseline:** arm `single` from the same model, same prompt, same fixtures. Not the simulated model.
- **Primary metric:** `solved` per arm, summed over the 9 runs (3 fixtures × 3 repeats). Secondary: median `calls` to first solve, mean tokens in/out, mean dollars per run, dollars per solve, median `evaluations`.
- **Pass (search beats single):** an arm's solved count exceeds `single`'s by **≥ 3 of 9 runs** at the same K, and its mean dollars per solve does not exceed 6× `single`'s mean dollars per run (the matched-call budget, in money).
- **Fail for an arm:** solved count ≤ `single`'s + 1. Per PR #28's rule, if both `repair` and `evolve` fail, the recommendation is to cut the search machinery back to single-shot plus repair; if `evolve` alone fails, `evolve.py` is parked (E3 cannot decide `exchange.py`).
- **Uninformative:** `single` solves ≥ 8 of 9 (fixtures too easy for this model; no arm can beat it) or any arm hits a provider error or refusal on > 20% of calls. Then the result is recorded and no keep/park decision is drawn from it.
- **Failure analysis, mandatory:** for every unsolved run, which required check failed last, and whether the call returned no candidate (refusal or `max_tokens`, visible as `stop_reason` in the recorded `model_usage` signal) or an admitted-but-failing candidate.

## What is recorded

Written by `--write` to `benchmarks/results/model-vs-patch-sonnet55.{json,txt}` and committed with a short results section appended to `docs/MODEL_COMPARISON.md`:

- per-run rows: fixture, arm, seed, `solved`, `calls`, `input_tokens`, `output_tokens`, `dollars`, `candidates`, `evaluations`, `seconds`;
- **p50 and p95 of `seconds`** per arm (run latency, from the JSON rows; the harness does not time individual calls, so per-call latency is reported as `seconds / calls` and labelled as such);
- **cost:** total dollars at list price, per arm and overall, beside the actual invoice line for the day;
- **admission refusals:** per row, `calls − candidates` (calls that produced no registered candidate: refusal, truncation, `no_change`, `invalid_change`), plus any `scope_violations` in `candidate_registered` events;
- **sample count:** 9 runs per arm, 36 runs, ≤ 171 calls;
- **revision:** SyberWork commit SHA, adapter file hash, model id as returned in `usage.model`, effort, K, date, Python version, OS.

## Commands

```sh
cd /Users/sethcarlson/Documents/SyberLabs/repos/SyberWork
git rev-parse HEAD                                     # record as the revision
PYTHONPATH=. python -m unittest tests.test_model_comparison -v
PYTHONPATH=. python -m conformance.run                 # must exit 0 before any paid call
pip install anthropic                                  # adapter only; syberlabs has no dependencies
ant auth status                                        # or: export ANTHROPIC_API_KEY=...   (founder's shell only)
export SYBERLABS_MODEL=claude-sonnet-5-5 SYBERLABS_EFFORT=medium

# pilot: ≤ 57 calls, ≤ $9.58 worst case
PYTHONPATH=. python benchmarks/model_vs_patch.py \
    --adapter "python adapters/anthropic_adapter.py" --calls 6 --repeats 1 \
    --write benchmarks/results/model-vs-patch-sonnet55-pilot

# full: ≤ 171 calls, ≤ $28.73 worst case, only if the pilot's $ (mean) × 3 < $38.46
PYTHONPATH=. python benchmarks/model_vs_patch.py \
    --adapter "python adapters/anthropic_adapter.py" --calls 6 --repeats 3 \
    --write benchmarks/results/model-vs-patch-sonnet55
```

## Key handling

- The credential is a **founder's** key, present **only in the environment of the shell that runs the command** (`ANTHROPIC_API_KEY`, or an `ant auth login` profile). It is never written to a file in this repository, a `.env`, a CI secret, a results file, or a chat.
- **No paid call without one.** Agents do not hold the key and do not run this benchmark; a named human executes it (Class X in PR #28). Without a credential the adapter fails on the first call and nothing is spent.
- The results JSON records token counts and `usage.model` only; it must be checked for the absence of the key before commit.

## Claim ceiling

The strongest claim the run can support, in `CLAIMS_AND_EVIDENCE.md` terms, is **Measured**: "On three toy fixtures, with `claude-sonnet-5-5` at effort `medium` and K = 6, arm X solved a/9 against `single`'s b/9 at $c." It does not establish that search beats single-shot on a real repository, that the result holds for another model or effort, or anything about `exchange.py`. With 9 runs per arm, a 3-run gap is suggestive, not established; the roadmap's bar (a real repository, several productive alternatives, a baseline, a failure case, and a decision that changed for a measured reason) stays open. Until the run exists, `docs/ROADMAP_STATUS.md` keeps **"Not measured with a model"**.
