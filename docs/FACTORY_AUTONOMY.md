# The autonomy thin slice (AM1): an approved feature map, built to "ready for owner merge"

Packet C4 of the consolidation plan (`reports/2026-11-01-syberfactory-consolidation.md`, section 5), built on the
factory of `docs/FACTORY.md`: the same ledger, the same spec object, the same tickets, the same Build Thread. It
realises the first slice of the autonomous typed mode (`2026-10-28-syberfactory-autonomous-typed-mode.md`) as
three modules under `syberlabs/factory/` and one CLI group, at $0, offline, with a stand-in command where a
model would be.

Claim ceiling for this page: **implemented** and **tested** (`tests/test_factory_autonomy.py`, 17 tests, run on
Windows with Python 3.12 and in CI). Nothing here is measured on a real spec, deployed, or validated by anyone
outside the repository.

## What it does

```text
spec (promoted)  ─►  FeatureMap (typed DAG)  ─►  mapcheck  ─►  owner approval  ─►  tickets  ─►  one Build Thread per attempt  ─►  ready for owner merge
                     featuremap.py                                                   loop.compile      loop.run (Runner from `syberlabs run`)
                                                 FeaturePolicy asks: next_feature, proposer_arm, budget_tier, after_failure, worth_continuing
                                                 envelope ledger: reserve before, settle after, stop on the line
```

| Module | What it holds |
| --- | --- |
| `featuremap.py` | `feature-map.v1`: features with id, objective, `req_ids` (the spec requirements they trace to), scope paths, shown criteria, hidden-check names (names only), `depends_on`, act class, size, risk inputs. `spec_from_markdown` turns a sectioned Markdown spec (`## R1: text`, `- given, when, then` bullets) into #154's spec object (owner decision 11, D2). `mapcheck` is the deterministic floor: schema, spec digest, coverage, acyclic DAG, scope conflicts (an intersection must be declared with an order that agrees with the DAG), act class, size ceilings, hidden-check names, approval digest. `taskgraph`: critical path, total work, work/critical ratio, Dilworth width with a maximum antichain, conflict graph. `risk_plan`: seven risk inputs computed by code, `r_i` as a weighted sum, standardized `z_i`, `v_i = V * softmax(z_i / tau)` as integer units above a floor that is listed beside every allocation and never moves. `compile_ticket`: one feature to one ticket in `ticket_from_spec`'s shape (D5). `degenerate_map`: the map `ticket_from_spec` implies, one feature per requirement. |
| `policy.py` | `Question` (closed options built by code; the state a policy reads is ids and numbers only), `Answer`, `validate` (an answer outside the options is `policy_invalid`), `OfflinePolicy` (critical path first, cheapest arm and tier, retry then stronger arm then owner, park when the cap is spent, stop on the envelope), `ShadowPolicy` (acts on one policy, records what an injected second one would have answered and whether it agrees; an invalid or failing shadow is recorded and changes nothing), `recorded_answers` (a shadow over a fixture). |
| `loop.py` | `Loop.compile`: refuses a map without a current owner approval (`map_unapproved`), a spec that is not promoted, or a map with findings; writes one contract per feature; logs `map_frozen` and `ticket_opened{source: map}`. `Loop.run`: kill switch, eligibility, three policy questions recorded as proposals, envelope reservation, one unattended Build Thread, one proposer call through `CommandProvider`, then `syberlabs run`'s `Runner` drives the thread (D17), the envelope settles from the journal, the outcome is recorded. `EnvelopeLedger`: the eval spend ledger's shape generalized to attempts (D13). `why`: ticket -> feature -> map (approval) -> spec, from the ledger. `event_digests`: one digest per autonomy event without its volatile `refs`. |
| `autonomy_cli.py` | `syberlabs factory autonomy check | approve | envelope | run | why`, registered from one hook line at the end of `factory/cli.py`. |

### A run on the fixture

`tests/fixtures/autonomy/` holds a three-feature fixture over the factory's own `factory_app`: `spec.md` (three
sections), `map.json` (F1 -> F2 a dependency chain, F3 independent and larger), `hidden_checks.json` (the host's
catalogue, name to argv) and `standin_proposer.py` (the process boundary a model adapter sits behind: it reads the
request, appends a delivery marker to the one file in scope, and answers one candidate; `--break` and `--outside`
make it fail the visible check or edit outside its scope).

The end-to-end test's ledger, in order:

```text
map_frozen  ticket_opened ×3
policy_proposal(next_feature=F1) policy_proposal(proposer_arm) policy_proposal(budget_tier)
envelope_reserved attempt_started thread_step(check acceptable) thread_step(accept succeeded) envelope_settled feature_ready
policy_proposal(next_feature=F3) ... feature_ready
policy_proposal(next_feature=F2) ... feature_ready
loop_stopped(nothing_eligible)
```

F1 goes first (bottom level 2: a dependent waits on it), then F3 (size M, bottom level 2), then F2. Each feature
ends as an accepted candidate on `syberlabs/<thread>`, with the thread id, commit and target ref in the
`feature_ready` event's `refs`. `main` never moves. Under `integration: stack` F2's thread starts from F1's accepted
commit; under `await_merge` F2 is recorded `feature_waiting` and the loop stops with F1 and F3 ready, because only
the owner's merge can release it. The same fixture run twice gives the same `event_digests` list: no timestamp,
thread id, commit or elapsed time enters a digest.

### From a terminal

```sh
python -m syberlabs.factory --home .syberlabs/factory spec validate spec.json          # the spec, as in FACTORY.md
python -m syberlabs.factory --home .syberlabs/factory spec promote spec.json --verdict review-1 --actor human:mateo
python -m syberlabs.factory autonomy check --map map.json --spec spec.json              # findings, graph, risk plan; exit 1 on findings
python -m syberlabs.factory autonomy approve --map map.json --by mateo                  # the owner's act; any edit leaves it stale
python -m syberlabs.factory autonomy envelope --map map.json --calls 6 --attempts 2 --out envelope.json
python -m syberlabs.factory --home .syberlabs/factory autonomy run --map map.json --spec spec.json --envelope envelope.json \
    --owner mateo --proposer "/abs/python standin.py ." --check "tests=/abs/python -m unittest -q" \
    --hidden "hidden.f1.marker=/abs/python -c ..."                                      # exit 0 when a feature is ready
python -m syberlabs.factory --home .syberlabs/factory autonomy why TICKET
```

## What it does not do

- **Publish or merge.** No action in the loop pushes, opens a pull request or merges; `loop.ACTIONS` is the closed
  list `start_thread, propose, check, accept_to_thread_branch`, and a test asserts the three modules hold no merge
  or network call. The terminal state is `ready_for_owner_merge`. The draft PR is C9.
- **Call Jev, or any model.** `ShadowPolicy` accepts an injected callable and records its answers; nothing imports
  `jev_api` or opens a socket. Jev stays in shadow until E7 (owner decision 7). The adapter that reuses
  `jev_api`'s request and validation code is a later packet; `recorded_answers` is its fixture-side stand-in.
- **Author the map.** The map is hand-written or produced elsewhere; `mapcheck` refuses what it cannot accept.
  The variant loop as map author is C4's follow-up in `factory/variants.py` (D14), not this slice.
- **Grade with a replay.** The hidden checks run in the candidate's worktree as contract checks the host holds
  (never in the repository), which is the Build Thread's own evidence. The independent replay grade (a fresh clone,
  a fresh hidden copy, the SyberEval gate) is the `replay` arm, left for C9/C10 with the eval runner.
- **Run with concurrency.** `concurrency` is 1. The task graph's width and conflict graph are computed and logged
  so the question of whether `c > 1` pays can be answered from the record; the scope lock is a check on declared
  overlap order, not a scheduler.
- **Price anything.** Every proposer call here is a stand-in at $0. The envelope's dollar tiers are read and
  reserved; a priced arm would settle from `search_finished.spend` exactly as the eval runner does.
- **Add an event kind.** `ledger.KINDS` belongs to `ledger.py` and to the goal-gate packet editing it. The slice
  writes `note` events whose `body.note` names the autonomy event (`loop.EVENTS`) and `ticket_opened` with
  `source: map`. When the vocabulary is extended, `loop.record` is the one place to change.

## Glossary (per the consolidation report)

- **Spec**: #154's spec object (`spec.py`). An L1 objective. Only an owner promotes it (C1 makes that code).
- **FeatureMap**: the typed task DAG of this page. L2 work. Frozen by the owner's approval digest.
- **Feature**: one node of the map; compiles to one ticket; built by one or more Build Threads (attempts).
- **Ticket**: `ticket_from_spec`'s shape with `map_ref`, scope as footprint, shown criteria, exit criteria naming
  evidence kinds and hidden checks, `rubric_digest` over the hidden-check names, a mandatory `human_owner`.
- **Codemap**: #154's `features.py`, hashed vectors per symbol and component. An index the map author may query
  and the `moved` sensor uses. It is never the feature map (D3; the rename to `codemap.py` is C1's).
- **Envelope**: the owner's grant of autonomy: dollars by tier, calls, seconds, attempts per feature, act classes,
  arms, the closed action list, the kill switch. Its ledger reserves before and settles after every attempt.
- **Floor**: host checks, hidden checks, critic, safety gate. Every feature, every risk score. Extra verification
  is only ever conjoined.
- **Promote**: an owner act on a spec. Not an attestation's verdict, not an accepted candidate (D4).
- **Shadow**: a policy whose answers are recorded beside the acting policy's and never acted on.

## Next packets

- **C5 `factory-check-real-checkout`**: `bootstrap` on a fresh SyberWork clone with `examples/factory_self_check.py`
  as the golden; the published contract shape committed as an example; the four FACTORY.md candidates against the
  real contract as a receipt.
- **C6 `independent-review`**: one `independence()` over the host-recorded served model's provider family (D8);
  `review.independent` recorded but not blocking; `auto_merge_eligible` computed and journaled per feature; the
  variant `judge` and the critic share it. The feature loop then records the composition of section 4 in shadow.
- **C9 `factory-publish` and the feature certificate**: the draft PR per ready feature with Feature, Map, Ticket,
  Envelope and Grade fields from the record; a grep test that no merge call exists; `feature-delivered/v1` as a
  third predicate referencing the child certificate and attestation digests (D6). Reads `settle` only.
- **C10 E8 `risk-allocation`**: planted defects in 2- to 4-feature maps; arms are allocation policies over one
  budget V (uniform, softmax at tau = 1, argmax, water-filling); the metric is defects caught per unit and escapes
  by risk decile. `risk_plan` here records the propensities that evaluation needs.

## Limits of this slice

- Stacking (`integration: stack`) starts a dependent from its last dependency's accepted commit by checking out a
  branch `syberfactory/<map>/<feature>` in the working repository. A feature with two dependencies on separate
  branches is not integrated here; the owner's merge order is.
- A hidden check runs as a contract check in the candidate's worktree, so it is as exposed to candidate code as any
  check (register N4). The fixture's stand-in writes no code that runs; a model's would, and then the host decision
  (C7) applies.
- The envelope ledger has no cross-process lock, as the factory ledger has none (D1 puts both on the journal's lock
  in C1).
- The risk weights and tau are priors. Nothing has calibrated them; `risk_plan` logs what calibration would need.
