# The autonomy thin slice (AM1): an approved feature map, built to "ready for owner merge"

Packet C4 of the consolidation plan (`reports/2026-11-01-syberfactory-consolidation.md`, section 5), built on the
factory of `docs/FACTORY.md`: the same ledger, the same spec object, the same tickets, the same Build Thread. It
realises the first slice of the autonomous typed mode (`2026-10-28-syberfactory-autonomous-typed-mode.md`) as
three modules under `syberlabs/factory/` and one CLI group, at $0, offline, with a stand-in command where a
model would be.

Claim ceiling for this page: **implemented** and **tested** (`tests/test_factory_autonomy.py`, 25 tests, run on
Windows with Python 3.12 and in CI). Nothing here is measured on a real spec, deployed, or validated by anyone
outside the repository. This slice sits on the goal-gate packet (C1, `syberlabs/factory-goal-gate`): the spec it
builds under is one the owner promoted through `factory approve` and `factory spec promote`, and the loop checks
that on the ledger, not in the spec file.

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
| `loop.py` | `Loop.compile`: refuses a map without a current owner approval (`map_unapproved`), a spec the ledger did not promote (`spec_unpromoted`, see `promotion_record`), or a map with findings; writes one contract per feature; logs `map_frozen` (with `promoted_by`) and `ticket_opened{source: map}`. `Loop.run`: kill switch, eligibility, three policy questions recorded as proposals, envelope reservation, one unattended Build Thread, one proposer call through `CommandProvider`, then `syberlabs run`'s `Runner` drives the thread (D17), the envelope settles from the journal, the outcome is recorded; a `Rejected` while running still writes the envelope's `stop` and the ledger's `loop_stopped{reason: <code>}` before it propagates. `promotion_record`: the goal gate's record on the ledger for exactly this spec (the `spec_changed` promote whose `spec_digest` is the file's, the `approval_recorded` it cites, the envelope under `approvals/`), re-verified under the owner key the ledger pinned at that point (`owner_key_set`, read through the ledger's verified owner-key lineage with its stored rotations, `Ledger.load_rotation`); a lineage that does not verify (a bare second pin, an unsigned or stale rotation, a malformed key) pins nothing and refuses `owner_key_lineage`; `owner_key_unset` and `owner_key_mismatch` refuse. `EnvelopeLedger`: the eval spend ledger's shape generalized to attempts (D13), one file per envelope digest under `envelopes/`, strict on every line. `why`: ticket -> feature -> map (approval) -> spec (promotion), from the ledger. `event_digests`: one digest per autonomy event without its volatile `refs`. |
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
`feature_ready` event's `refs`. `main` never moves, and the owner's checkout is as it was: HEAD on the same branch
at the same commit, no scratch branch left. Under `integration: stack` F2's thread starts from F1's accepted
commit; under `await_merge` F2 is recorded `feature_waiting` and the loop stops with F1 and F3 ready, because only
the owner's merge can release it. The same fixture run twice gives the same `event_digests` list: no timestamp,
thread id, commit, elapsed time, approval digest or kill-switch path enters a digest.

After a failure, `retry_same` offers the next attempt every arm again; `retry_escalate_arm` (offered only when the
envelope holds an arm ranked above the one that failed) offers only the stronger arms, so the arm on
`attempt_started` is the arm the recorded decision named. The test runs `cheap, cheap, strong` then parks.

### From a terminal

```sh
python -m syberlabs.factory --home .syberlabs/factory owner --public owner.pub           # pin the owner key on the ledger (once)
python -m syberlabs.factory --home .syberlabs/factory spec validate spec.json          # the spec, as in FACTORY.md
python -m syberlabs.factory --home .syberlabs/factory approve spec.json --verdict VERDICT --actor human:mateo \
    --seed-env SYBERLABS_FACTORY_OWNER_SEED --out approval.json                         # the goal gate (C1): the owner signs
python -m syberlabs.factory --home .syberlabs/factory spec promote spec.json --verdict VERDICT --actor human:mateo \
    --approval approval.json                                                            # verified under the pinned key, recorded, cited
python -m syberlabs.factory autonomy check --map map.json --spec spec.json              # findings, graph, risk plan; exit 1 on findings
python -m syberlabs.factory autonomy approve --map map.json --by mateo                  # the owner's act; any edit leaves it stale
python -m syberlabs.factory autonomy envelope --map map.json --calls 6 --attempts 2 --out envelope.json
python -m syberlabs.factory --home .syberlabs/factory autonomy run --map map.json --spec spec.json --envelope envelope.json \
    --owner mateo --proposer "/abs/python standin.py ." --check "tests=/abs/python -m unittest -q" \
    --hidden "hidden.f1.marker=/abs/python -c ..."                                      # exit 0 when a feature is ready
python -m syberlabs.factory --home .syberlabs/factory autonomy why TICKET
```

`run` reads the spec's promotion from the same `--home` the promote wrote to and re-verifies its approval under the
owner key that ledger pins; no key is passed on the command line. Map compilation honours the ledger's owner-key
lineage (FACTORY.md): the key is the one the verified chain of `owner_key_set` events pinned before the approval,
each rotation checked against its stored envelope, and a ledger whose lineage does not verify compiles nothing
(`owner_key_lineage`), whatever its newest `owner_key_set` says. An envelope is one grant: its
ledger lives at `envelopes/<envelope digest>.jsonl`, a second map or a new envelope in the same home starts
clean, and a run over an envelope whose ledger already carries a `stop` is refused (`envelope_stopped`). On
Windows, `--check` and `--hidden` commands with nested quotes need a script file (`split_command` is non-POSIX
there; pre-existing in #154).

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

- Stacking (`integration: stack`) starts a dependent from its last dependency's accepted commit. `Kit.start` starts
  from HEAD, so the loop moves HEAD to that commit on a scratch branch `syberfactory/<map>/<feature>` for the start
  only, then puts the owner's branch (or detached commit) back and deletes the scratch branch, whether or not the
  start succeeded. A working tree with uncommitted tracked changes is refused first (`repository_dirty`), so nothing
  is carried into a thread base or lost. A feature with two dependencies on separate branches is not integrated
  here; the owner's merge order is.
- A hidden check runs as a contract check in the candidate's worktree, so it is as exposed to candidate code as any
  check (register N4). The fixture's stand-in writes no code that runs; a model's would, and then the host decision
  (C7) applies.
- The envelope ledger has no cross-process lock. C1 put the factory ledger on the journal's lock; the envelope file
  is still appended without one (two loops on one envelope in one home is not a supported run).
- The risk weights and tau are priors. Nothing has calibrated them; `risk_plan` logs what calibration would need.
  `risk_plan(weights=)` is validated (the known inputs, each in [0, 1], summing to 1) but not reachable from the CLI.
- **The map approval is a record, not a credential.** `featuremap.approve` writes `{digest, actor}` and
  `approved()` checks the digest against the map, not the actor against a signature: swapping `approval.actor.id`
  leaves the map approved. The spec's promotion is now bound to the owner's signed approval through C1's seam
  (`promotion_record`, re-verified under the owner key the ledger's verified lineage pins); the map's approval has no such seam yet. Binding it is a
  signed `approval_payload` over the map digest, which is C1's shape applied to a second artifact; proposed for the
  packet that gives the factory a kernel case to hang approvals on.
- **The floor is listed, not enforced, by this slice.** `FLOOR` names host checks, hidden checks, critic and safety
  gate, and every `buys` list carries all four. Only the first two are contract-required here (`required` in the
  compiled contract is the visible checks plus the hidden checks). No critic runs and no safety gate is computed
  until C6 (`independent-review`) and C9 bring them; "every feature, never loosened" is true of the two that exist.

## Deviations from C4 as written

The consolidation report's C4 row (section 5) asked for more than this slice ships. Each deviation is deliberate,
and none of them is hidden in "Deliberately not in this slice" above:

- **The envelope ledger is re-implemented, not imported from #150's spend ledger (D13 "build it once").** The eval
  spend ledger lives in `syberlabs/eval/`; `docs/architecture.declared.json` declares no `factory -> eval` edge, and
  the layering test (`test_repository_layering_has_no_drift`) refuses an undeclared import. `EnvelopeLedger` copies
  the shape (one JSON object per line, reserve before, settle after, strict read, non-finite refused) rather than the
  code. Proposed for C11: declare `syberlabs-factory -> syberlabs-eval` (or lift the spend ledger into a module both
  can import) and delete this copy.
- **`RecordedJevPolicy` does not reuse `jev_api`'s validator (D11).** Same layering: `jev_api` is outside the
  factory's declared edges. `policy.validate` is the closed-option check; `recorded_answers` is the fixture-side
  shadow. The adapter that reuses `jev_api`'s request and validation code is the packet that declares that edge.
- **Decisions are factory-ledger `note` events, not kernel-journal `observed{policy_decision}`, and `propose` is
  untagged.** The kernel's `Thread.propose` takes no origin field today (an `origin` is recorded only for migrated
  candidates, through `migrate`), and the kernel's event vocabulary is not this packet's to extend. Every policy
  answer is a `policy_proposal` note on the factory ledger before it is acted on. Proposed for C6: an
  `observed{policy_decision}` on the case and `propose(origin="model")` once the kernel accepts an origin on a
  proposal.
- **`factory autonomy why` takes a ticket id only**, not a PR or thread id: there is no PR until C9, and the thread
  id is in the `feature_ready` event the ticket walk already returns.
- **The fixture is `tests/fixtures/autonomy/`, not `factory/fixtures/spec-slugify-pricing/`.** It reuses the
  factory's own `factory_app`, so the Space graph, the ledger and the hidden checks are the ones the rest of the
  factory tests already exercise. A separate product fixture adds a second app to maintain for no extra coverage in
  this slice.
- **`OfflinePolicy` is critical-path-first, not risk-per-unit-of-work-first.** `next_feature` orders by bottom
  level, then risk, then map order. Risk per unit of work needs a cost model per feature that `risk_plan` does not
  yet have (its units are verification units, not build cost); deferred until C10's E8 gives risk a measured scale.
