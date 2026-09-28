# Model-backed search versus a single model patch

The question: at the same model cost, does searching (independent samples, repair with check feedback, or EvoGit-style evolution) produce an acceptable change more often than one model patch? Nothing in this repository answers it for a real model yet. What exists is the apparatus to answer it honestly, run so far only against a simulated model.

## What is compared

`benchmarks/model_vs_patch.py` runs every arm through the same Build Thread. Every candidate is host-evaluated and recorded, and "solved" means a candidate that passed every required check *and* that admission would let a person accept. Every arm stops at its first passing candidate, so its call count is its cost to first solve, capped at K.

| Arm | Model calls | What the model sees |
| --- | --- | --- |
| `single` | 1 | Objective, scope, and files in scope. This is the baseline. |
| `best_of_k` | up to K | The same as `single`, K independent times |
| `repair` | up to K | After the first call, the previous candidate's files and the host's failing check output |
| `evolve` | up to K mutator calls | One parent's files per call. Crossover and selection cost no model call, but do cost evaluations. |

Cost is matched on **model calls** (K). Tokens are reported per arm from the adapter's own usage, and dollars are computed from them with the price table in the script. Evaluations (check runs) are reported separately, because they cost compute, not model spend.

## Fixtures

`benchmarks/fixtures.py` holds three small repositories: pricing rules, a slug function, and interval merging. Each has three required checks. Several of the checks interact: interval merging needs ordering, overlap, and joining to agree. They are small on purpose so a run is cheap. They are not real repositories, and a result on them says little about one.

## Running it with Claude

```sh
pip install anthropic                     # only the adapter needs it; syberlabs has no dependencies
export ANTHROPIC_API_KEY=...              # or: ant auth login
PYTHONPATH=. python benchmarks/model_vs_patch.py \
    --adapter "python adapters/anthropic_adapter.py" --calls 6 --repeats 3 \
    --write benchmarks/results/model-vs-patch-claude
```

`adapters/anthropic_adapter.py` uses the official SDK. It defaults to `claude-opus-5` at effort `high` (set `SYBERLABS_MODEL` and `SYBERLABS_EFFORT` to change them), with structured JSON output, the server-side refusal fallback (`fallbacks: "default"`), and each call's token usage returned to the host.

**This spends money.** The run above makes at most 3 fixtures × 3 repeats × (1 + 6 + 6 + 6) = 171 calls, and fewer when arms solve early. On these fixtures a call is a few thousand tokens in and a few thousand out, including thinking, so budget on the order of tens of dollars at Claude Opus 5 prices. Measure one repeat first. The script reports the actual tokens and dollars.

## Results so far (simulated model only)

`benchmarks/simulated_model.py` is not a model. It edits each fixture's known slots with fixed probabilities: a patch gets each slot right with p = 0.45, a repair fixes each failing slot with p = 0.6, and a mutation re-rolls one slot with p = 0.35 of being right. Its "tokens" are text length divided by four. The numbers below therefore test the harness and describe those probabilities. They are not evidence about any model, and in particular not about whether a real model's repair uses feedback well.

The simulated run (K = 6, 10 repeats per fixture and arm) writes `benchmarks/results/model-vs-patch-simulated.txt`. Its table is added here in the follow-up commit.

## What would count as evidence

A real-model run on these fixtures would show only whether the arms differ on toy tasks. The roadmap's bar is a real repository with several productive alternatives, a baseline, a failure case, and a decision that changed for a measured reason. Until that run exists, the claim stays as it is in `docs/ROADMAP_STATUS.md`: **not measured**.
