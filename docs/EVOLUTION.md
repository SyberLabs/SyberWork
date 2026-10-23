# Candidates and search providers

SyberWork is the authority and evidence layer. A search provider is an artifact-search layer: it proposes changes. The provider can be a person's patch, a model adapter, or an EvoGit-style evolutionary search. Its output is **provisional** until the same contract, evidence, policy, and approval gates that govern every other SyberWork action admit it.

> Lineage is provenance, not evidence of superiority.

A candidate records where it came from: its parents, the operator that produced it, and the provider name and revision. None of that counts toward promotion. A crossover child of two passing parents, carrying the provider's own `fitness: 1.0`, is still `candidate_not_evaluated` until the host runs the contract's checks on that child's exact tree.

**Status, 2026-10-23: the evolve arms are research; repair is the default proposer.** On the E3 fixture sets v3 and v4 (Claude Sonnet 5.5, K = 6 and K = 12; [MODEL_COMPARISON.md](MODEL_COMPARISON.md)) the `repair` arm, one proposal and then proposals that carry the host's check results for the failed attempt, beat every evolve arm on solves and on parts per dollar at every budget tested, and beat `single` on solves, and the owner made repair the factory's default proposer path: `syberlabs.providers.RepairProvider`, `syberlabs propose --repair COMMAND` ([BUILD_THREAD.md](BUILD_THREAD.md), "Providers"). It sends the same `feedback` checks a mutation gets (`mutation_feedback`, below) plus the failed candidate's file texts, so a model is directed by the host's results under both protocols. `EvolutionaryProvider`, its mutators, the three branch policies and the exchange stay in the package and in the harness as research: nothing here is removed or changed by that decision, and the contract still cannot name a provider kind (`spec/GAPS.md`, 47), so the choice is made where the provider is built, as a policy's is.

## The `evolution` contract section

The section is optional and is versioned with the contract like everything else. Once a version is published it cannot change, and a thread stays pinned to the version it started with. Unknown fields are refused at publish, so a typo cannot silently widen authority.

```json
"evolution": {
  "scope": {"paths": ["src/", "tests/"], "exclude": ["src/vendor/"], "max_files": 20, "max_diff_bytes": 200000},
  "operators": ["patch", "mutation", "crossover"],
  "budget": {"max_candidates": 16, "max_evaluations": 24, "max_seconds": 600},
  "evaluation": {
    "checks": {"tests": {"argv": ["python", "-m", "unittest", "-q"], "timeout_seconds": 300}},
    "required": ["tests"],
    "max_age_seconds": 3600
  },
  "promotion": {"action": "accept_change", "roles": ["developer"], "approval_role": "maintainer",
                "target_ref": "refs/heads/syberlabs/{thread}"}
}
```

| Field | Meaning | Enforced by |
| --- | --- | --- |
| `scope.paths`, `scope.exclude` | Mutable scope, as path prefixes. `.git` and `.syberlabs` are always excluded. | `record_candidate` recomputes `scope_violations` from the changed paths. The provider cannot supply them. |
| `scope.max_files`, `scope.max_diff_bytes` | Size limits on one candidate | `limit_violations`, recomputed at registration |
| `operators` | Operator names a provider may use | Registration refuses others (`operator_not_permitted`) |
| `budget` | Candidates and evaluations per thread; seconds per search. Optionally `prices` (dollars per million tokens by model), `max_dollars` per thread and `strict`; see [BUILD_THREAD.md](BUILD_THREAD.md#spend-and-the-dollar-budget). | `budget_exhausted` at registration or evaluation, and before a model command starts once the host-observed dollars are spent. The host enforces the deadline. |
| `evaluation.checks` | Project-owned commands, as argv lists. No shell is used. | The host runs them. The provider never reports a result. |
| `evaluation.required` | Checks that must pass on the candidate's exact tree | `candidate.promotable` |
| `evaluation.max_age_seconds` | Freshness of that evidence | `candidate_evidence_stale` at proposal and again at commit |
| `promotion.action` | The contract action that promotes a candidate | Ordinary admission, plus `candidate.promotable` |
| `promotion.roles` | Human roles that may propose promotion. `model`, `compiled`, and `search` are refused at publish. | `candidate_promotion_role` |
| `promotion.approval_role` | An independent approver. It must also be set on the action, so the existing `approval.required` rule enforces it. | `approval.required` |
| `promotion.target_ref` | The authoritative branch a promotion writes. It defaults to `refs/heads/syberlabs/{thread}`. | The Build Thread's effect executor. Neither a proposal nor a provider names it. |

## Records

Four event kinds join the case hash chain. A provider never appends them: the host does, after computing each field itself.

| Kind | Body | Written when |
| --- | --- | --- |
| `search_started` | provider name and revision, operators, budget for this search, base commit, context digest | A provider is invoked |
| `candidate_registered` | id, commit, tree, base, parent candidate ids, operator, provider, changed paths, recomputed scope and limit violations, diff digest and size, provider `signal` (from the evolutionary provider it carries `policy`: the branch policy's name, revision and decision number), note; under a priced contract `spend` (dollars of host-observed usage, seconds since the search started, `host_observed`) | The host wrote the candidate's commit on a non-authoritative ref |
| `candidate_evaluated` | candidate, commit, tree, evaluator, and per check: state, exit code, duration, output digest, output tail | The host ran the checks |
| `search_finished` | why it stopped, candidates, recommended ids, evaluations used, elapsed time, error code; under a priced contract `spend` (dollars, seconds, evaluations, candidates, `host_observed`, `unknown_models`) | The provider returned, ran out of budget, or failed |

`signal` and `recommended` are the provider's own opinion. They are shown to the developer as such and are never read by admission. A `model_usage` in the signal is a declaration; the `spend` fields are the host's own measurement at the `providers.run_json` boundary and exist only when the contract prices usage.

## Promotion

A promotion is a proposal of the contract's promotion action with exactly `{candidate, commit, base}`. Admission runs the whole ordered rule list. `candidate.promotable` sits after the argument bindings and before `approval.required`. For the promotion action it denies, in order:

1. `candidate_promotion_origin`: the proposal's origin is not `human`, or the proposer holds an automation role (`model`, `compiled`, `search`).
2. `candidate_promotion_role`: the proposer holds none of `promotion.roles`.
3. `candidate_args_invalid`: the arguments are not exactly `{candidate, commit, base}` as strings.
4. `candidate_unknown` or `candidate_mismatch`: no registered candidate, or its commit or base differs.
5. `candidate_out_of_scope`: any scope or size violation.
6. `candidate_not_evaluated`, `candidate_check_missing:<name>`, `candidate_check_failed:<name>`, or `candidate_evidence_stale`: judged by the newest host evaluation of that candidate's registered commit and tree.

Under a contract that prices usage, two more rules follow the critic: `budget.search` (`spend_unrecorded:<id>`, `search_over_budget:<dimension>`) reads the candidate's own `spend`, and `budget.promotion` (`over_budget:dollars`, `unknown_spend:<model>`) sums the thread's finished searches against `max_dollars`. Neither decides anything without `prices`.

Then approval applies as it does for any action. Commit rechecks everything, and a policy or contract change between proposal and commit is caught there. Replay compares decisions under another contract version, for example one that adds a required check. The existing `effect.not_completed` rule allows one promotion per thread. Promoting another change means a new thread.

## The EvoGit-style provider

`syberlabs.evolve.EvolutionaryProvider` is an independent implementation of the method published as EvoGit (Huang et al., 2025, arXiv:2506.02049). It is one `SearchProvider` among others; removing it changes nothing on the authority side.

| EvoGit idea | Here | Deliberate difference |
| --- | --- | --- |
| Population of Git commits; ancestry as a phylogenetic graph | Candidates on `refs/syberlabs/candidates/<thread>/cN`; parents recorded and matching the Git parents | Not branches under `refs/heads`, and never authoritative |
| Mutation by a language model rewriting part of a file | A pluggable `Mutator`. `CommandMutator` is the model seam (JSON on stdin and stdout). | The contract must list `mutation`. Scope and size are recomputed by the host. |
| Crossover by `git merge` of non-ancestor commits, with conflicts resolved at random | Three-way `git merge-file` per file from the merge base, with conflict regions settled by a seeded coin (`accept_ours`). Skipped when one parent contains the other. | The contract must list `crossover` |
| Pairwise selection: a child replaces its parent if better; a merge replaces both parents if better than both | Same rule, comparing the host's `Evaluation.score()` (required checks passed, then all checks passed) | Fitness is the host's own run of the contract's checks. A model's opinion of a diff, which EvoGit also supports, is not used. |
| Human as product manager: goals, periodic review, human-tagged commits migrated into the population | The objective; `seeds=[...]` (or `--from c3 c7`) put a person's candidates into the population | Promotion always needs a person, through admission |

**The mutate request.** `CommandMutator` writes `{"protocol": "syberlabs.mutate/v0alpha1", "objective", "scope", "parent", "files": {path: text}, "context", "seed"}` on the mutator's stdin and reads `{"changes": {path: text or null}}`. `scope` is the evolution scope's paths (`SearchSpace.scope`), the same list the search protocol's request carries (`providers.CommandProvider`), so an adapter tells its model the same mutable scope under both protocols. It was added on 2026-10-22: until then the mutate request had no `scope`, `adapters/anthropic_adapter.py` printed `Mutable scope:` followed by nothing, and a model told to change only files in the stated scope returned `changes: {}` (one hand-made mutate request for the v3 fixture `verify_layers` against Claude Sonnet 5.5: 65 output tokens and no files; the same request with `scope` set proposed a real patch). The host counts an empty proposal as a mutator call that produced no child: no candidate, no evaluation, nothing in the journal, and the search goes on to its stop, which is why the `evolve*` arms in `docs/MODEL_COMPARISON.md` could make K calls and register nothing. The key is additive, the protocol name is unchanged, a mutator that ignores unknown keys is unaffected, and no frozen schema or golden trace enumerates the request. The adapter now writes `Mutable scope: the files shown below` when a request states no scope, under either protocol, and its system prompt says the same.
| Git notes caching fitness | Evaluations are `candidate_evaluated` events in the thread's hash chain | Nothing reruns while a tree has a fresh result |

### Several hosts

EvoGit runs populations on several hosts that exchange individuals through a shared Git remote. `syberlabs.exchange.Exchange` does the same with a remote every host can push to:

- A host writes only its own namespace: `refs/syberlabs/exchange/<topic>-<base12>/<host>/` holds its candidate commits and a `manifest` commit listing them with operator, local parents, and the host's own score.
- Every `migrate_every` generations, `EvolutionaryProvider(..., exchange=kit.exchange("origin", host="a", topic="pricing"))` publishes its population, fetches the other hosts' namespaces, and tries up to `migrants` offers, the best-reported first.
- A migrant becomes a local candidate with operator `migration`, which the contract must list. It has no local parents; its `origin` (host and candidate) records where it came from.
- The receiving host verifies that the commit is exactly the one the other host published under that name, and that it descends from this thread's base. It recomputes scope and evaluates the migrant itself. A migrant outside scope is recorded and never run.
- A migrant replaces the worst individual only if its local evaluation is better. A reported score only decides which offers to try; a forged score cannot get a failing migrant selected or accepted (tested).

From the terminal: `syberlabs propose --evolve ./mutator --exchange origin --host a --topic pricing --migrate-every 2`. The namespaces are only as trustworthy as the remote's access control, but a migrant gains no authority whatever its source: it is judged locally and accepted only by a person.

From the terminal: `syberlabs propose --evolve "./my-model-adapter" --population 4 --generations 6`. The provider's recommendation is printed as "a signal, not a verdict".

### Branch policy

Where the provider branches is a seam. Before every step, `EvolutionaryProvider` asks a `BranchPolicy` (`syberlabs.search`) for the next action and hands it a `Situation`: the population as `Individual(id, score, passed)` slots in order, the capacity, the remaining budget and whether there is room, the outcomes so far per action kind (`OperatorStats`: attempted, produced, evaluated, improved, passed, and dollars on a host with a dollar bound), whether the mutator is exhausted, whether an exchange is configured, the number of decisions taken, and the provider's seeded random stream. A policy never sees the session, the repository or file contents. Its `name` and `revision` are checked like a provider's when the provider is built (`invalid_provider`).

The action set is closed: `Mutate(parent, slot, generation)`, `Crossover(a, b, slots, generation)`, `Migrate(generation)`, `Evaluate(candidate, generation)` and `Stop(reason, generation)`; `ACTION_KINDS` lists their kinds. Actions name slots, not only ids, because a crossover win leaves one id in two slots. The provider executes each action with the mechanics above and keeps the pairwise replacement rule itself, so a policy cannot keep a worse child. It refuses, with `invalid_policy`, an action outside the set, a generation label lower than the last one or above `generations + 1`, and a slot that does not hold the id the action names; the search then ends with `stopped: "error"` and `error: "invalid_policy"`.

`FixedPolicy` is today's behaviour as a policy and the default when `EvolutionaryProvider(...)` is given no `policy`: seeds are evaluated while there is room, the population is filled from `base` with at most four attempts per slot, then each generation is a mutation round over every slot in order, or a crossover round over a random pairing every `crossover_every` generations and whenever the mutator is exhausted, followed by a migration on its cadence; the search stops at the top of a generation when the generations are used up, there is no room, an individual passes (with `stop_when_passing`) or the population is empty, and inside a crossover round when the mutator is exhausted and recombination improved nothing. Room is every budget dimension above zero; on a host that prices usage `remaining()` also carries `max_dollars` (None without a dollar bound), so the dollars running out is `Stop("budget")` like the counts, and the host writes `stopped: "budget_exhausted"`. `tests/test_evolve.py` holds it to the loop of `main` `d0bebb8` twice over: against a recording taken on `main` (each candidate's id, operator, parents, evaluation and checks passed; the legacy-shaped trace; the recommendation and the evaluation count) and side by side with a verbatim copy of the old loop on identical fixtures, including the stop on an exhausted mutator and rounds over a single slot. Commit ids are not compared: they differ run to run. Its `_round_kind` hook is the one place a later policy chooses between a mutation round and a crossover round.

**Two revisions.** `FixedPolicy` evaluates every seed and every fill child as it makes it (the host judges each child's tree at once), but revision `"1"` only looks at the verdicts at the top of a generation, so with `stop_when_passing` a task that a single mutator call solves still spends `population` calls filling the population before the search stops (the E3 pilot of 2026-10-21 in `docs/MODEL_COMPARISON.md`: `evolve` solved the same fixtures as `single` with 5 or 6 calls instead of 1). `FixedPolicy(..., stop_on_first_pass=True)`, also `EvolutionaryProvider(..., stop_on_first_pass=True)` for the default policy, is revision `"2"`: during the seed and fill phases, as soon as any individual has passed, the next decision is `Stop("passing", 0)`, a clean stop at generation 0, so the trace closes generation 0 with the usual `{"generation": 0, "population": [...]}` entry and the stop decision carries `"generation": 0`; a passing seed ends the search before any mutator call. Nothing else changes: the one-draw rule, the replacement rule, the generation labels and the `search_started` and `search_finished` bodies are the same, and when no seed or fill child passes the two revisions take identical decisions (tested on a recorded fixture). Every decision entry and every child's `signal.policy` names the revision, so a trace shows which rule was in force. The default is revision `"1"`, the legacy construction, because the recorded-equivalence tests above and `tests/test_model_comparison.py` (which pins three mutator calls on a fixture whose first child passes) hold the provider to the old loop; the equivalence tests construct both sides with `stop_on_first_pass=False` explicitly. The construction sites that would opt in, `syberlabs/cli.py` (`propose --evolve`), `benchmarks/model_vs_patch.py`, `benchmarks/evolve_baseline.py` and `examples/evolve.py`, do not yet; the baseline numbers below were measured under revision `"1"` and are not re-published.

**One draw.** The mutator and the crossover draw from the provider's seeded stream in a fixed order, so a policy may draw from `situation.rng` only once per round and only for one thing: `rng.sample(range(n), n)` to pair a crossover round, at the start of that round. Any other randomness a policy needs comes from its own `random.Random`, seeded from a constant and `situation.decisions`. An extra draw changes every child after it, which the equality tests catch for `FixedPolicy`.

**Generation labels.** The policy labels its rounds and the label never decreases. The provider closes a generation with the usual `{"generation", "population"}` trace entry when the label advances; a label the policy skips is a generation in which nothing happened and is closed the same way. `Stop` with `budget`, `passing`, `empty` or `generations` ends the search at a generation boundary; any other reason, such as `exhausted`, ends it inside the round the label names and closes that round with a `"stopped"` entry, as before.

**Records.** Every decision is an entry in `provider.trace`, interleaved with the entries above: `{"decision": n, "generation": g, "policy": {"name", "revision"}, "action": kind, ...}` with `parent` and `slot`, `pair` and `slots`, `candidate`, or `reason`, and for an executed action an `outcome` of `{"candidate", "score", "improved", "evaluated"}`. A reader that wants the old trace filters `[e for e in trace if "decision" not in e]`; the stop decision is always the last entry. Every child the provider registers carries `signal.policy = {"name", "revision", "decision"}`, so a `candidate_registered` event links to its decision; the signal stays far below the host's 4,096-byte bound (tested under 1,024 bytes with a 64-character mutator name, a 128-character revision and a full `model_usage`), and `search_started` and `search_finished` are unchanged. Migrants carry no signal and a seed evaluation registers nothing, so those decisions are in the trace only. The trace lives in memory on the provider object and is not journaled; a policy learns from one search.

**Later policies.** The seam claims nothing about any policy's merit and re-publishes no number. Behind it today: `FixedPolicy`, and the two learning policies of the next section; a Bough policy consulting a table compiled offline from a population summary is still a later packet. Any claim about any of them, `FixedPolicy` included, waits on E3 (`docs/E3_BENCHMARK_PLAN.md`), a real-model benchmark that a named human runs.

### Learning policies

Two policies learn from this search's own outcomes behind the same seam. Both are chosen by construction only: `EvolutionaryProvider(..., policy=EmpiricalPolicy(...))` or `policy=JevPolicy(...)`, and from the terminal `syberlabs propose --evolve <command> --policy empirical|jev`. The contract does not name a policy (`spec/GAPS.md` entry 41); the default everywhere is `fixed`.

**`EmpiricalPolicy`** (`name` `empirical`; revisions `"1"` and `"2"` mean what they mean for `FixedPolicy`) is the control. It is the fixed schedule in everything (seeds, the fill from `base`, rounds over every slot, the migration cadence, the stops) except `_round_kind`, which chooses a mutation round or a crossover round by Thompson sampling: one Beta posterior per operator over `improved / attempted` in this search's rounds (the fill is not a choice and is left out; the prior is `(1, 1)`), one sample from each, the larger wins. The sampler is the policy's own `random.Random`, seeded from a constant, `salt` and `situation.decisions`, so the one-draw rule holds: inside `decide` the only draw from the provider's stream is the pairing of a crossover round, which `tests/test_jev.py` counts. An exhausted mutator still forces a crossover round and a population of one slot a mutation round. The first decision of every round carries `posterior`, `sampled` and `chosen_by: "thompson"` in its trace entry.

**`JevPolicy`** (`name` `jev`, revision `"1"`) decides per action, not per round, from `Situation` alone. The actions it can take at a decision: evaluate the next seed; cross the two best distinct individuals it has not crossed yet; mutate `base` while the population is below capacity, otherwise the best individual (ties: the slot mutated least); migrate once when an exchange is configured. Each is scored:

- *value*: the posterior probability that the action yields a child that passes, `(passed + a) / (attempted + a + b)` over the `OperatorStats` of its kind, Beta prior `(a, b) = (1, 1)`; a seed evaluation counts under `evaluate`, a migration under `migrate` (zero attempts and no exchange: not offered);
- *price*: its expected cost in the unit the host bounds. On a host that reports `max_dollars`: the mean host-observed dollars of past actions of that kind (`OperatorStats.dollars`, below), with the constructor's `prices[kind]` as a prior worth one attempt, plus the evaluations the action is expected to consume valued at the shadow price `remaining dollars / remaining evaluations`, so a crossover, which makes no model call, is cheaper than a mutation but not free while it spends an evaluation. Without a dollar bound the unit is evaluations. The price is never below one micro-dollar, the host's rounding unit;
- *score*: `(value + bonus) / price`, with an exploration bonus `explore / ((1 + decisions) * (1 + attempted))`, default `explore = 0.1`, that decays with the decisions taken.

It takes the highest score (ties in the order evaluate, crossover, mutate, migrate). It returns `Stop("passing")` as soon as an individual passes, `Stop("budget")` when the host reports no room or when the best action's expected passes within the remaining budget, `value * remaining / price`, fall under `floor` (default `1.0`: the budget left is not expected to buy one pass), `Stop("generations")` after `generations` actions beyond seeds and fill, and `Stop("empty")` when no action is available. Seed evaluations and fill mutations keep the current generation label; every other action is a generation of its own, so with the provider's `generations` bound Jev takes at most that many actions after the fill. It draws no randomness at all. Every Jev decision entry, stops included, carries `expected_value`, `expected_dollars` (`None` without a dollar bound) and `chosen_by` (`value_per_dollar`; `exploration` when the bonus decided; `only_option`; `rule:<reason>` for a stop), plus `price` (the price and the expected evaluations), `expected_passes`, `budget` (`unit`, `remaining`, `shadow`) and `alternatives`, every offered action with its estimate, so a reader can tell from the entry alone why the dollar went where it did. The policy never sees the session, the repository or file contents.

**Dollars in the situation.** `OperatorStats` gained `dollars`: the host-observed dollars the decisions of that kind cost, measured by the provider as what `remaining().max_dollars` lost across each action, `None` on a host without a dollar bound (then `remaining()` has no dollars to lose). The number is the host's own attribution, which is to the next registered candidate: a model call that registers nothing (`no_change`, `duplicate`, no changes) is attributed to whatever registers next, a crossover included, so a kind's dollars are what its registrations carried, not what its calls cost. The measure is floored at zero and does not see spend past the bound, where the host stops the search anyway. On a priced host each executed decision's `outcome` also carries `dollars`; an unpriced trace is exactly what it was.

**The audit seam.** A policy with an `audit()` method has the mapping it returns merged into the trace entry of the decision it just made; `FixedPolicy` has none, so its entries are unchanged. The provider refuses (`invalid_policy`) an audit that shadows one of its own fields. The trace is still in memory on the provider object and not journaled: what reaches the journal is each child's `signal.policy = {name, revision, decision}`, which links a `candidate_registered` event to its decision, and the host's `candidate_registered.spend`. The audit fields are not in the signal, which stays far below the 4,096-byte bound (asserted on every candidate of every simulated search and on the real-thread searches).

**Measured on simulated operator profiles.** `tests/test_jev.py` runs the three policies on the pricing fixture of `examples/evolve.py` with a stand-in mutator that has a per-operator success profile, in an in-memory `SearchSpace` that keeps the host's rules a policy can feel (the `no_change` and `duplicate` refusals, one evaluation per judged tree, spend attributed to the next registration, `$0.0135` a call). `mutation_rare`: from `base` a mutation always fixes one rule, from a child it fixes another one time in about seven, and otherwise writes a new wrong body, so children touch different rules and a crossover combines them; `mutation_common`: a child's mutation fixes a rule six times in seven but rewrites every other unfixed rule too, so two children conflict in every region and a crossover rarely helps. Population 3, budget 60 candidates, 60 evaluations and $5, ten seeds (0 to 9), calls and dollars counted up to the first passing candidate:

| Profile | Policy | Solved | Calls (sum) | Calls (median) | Dollars (sum) | Evaluations (sum) |
| --- | --- | --- | --- | --- | --- | --- |
| mutation_rare | fixed | 10/10 | 136 | 15 | 1.8360 | 125 |
| mutation_rare | empirical | 10/10 | 103 | 11 | 1.3905 | 97 |
| mutation_rare | jev | 10/10 | 51 | 5 | 0.6885 | 50 |
| mutation_common | fixed | 10/10 | 72 | 7 | 0.9720 | 72 |
| mutation_common | empirical | 10/10 | 72 | 7 | 0.9720 | 73 |
| mutation_common | jev | 10/10 | 48 | 5 | 0.6480 | 78 |

Simulated, and nothing about a model: the profiles were chosen so the policies can be told apart, the in-memory world is not the Build Thread, and the fixture has three independent rules that favour recombination by construction. On the real Build Thread the module checks, over two seeds with a priced contract and a command mutator that reports usage, that the host's attributions reach `OperatorStats.dollars` kind by kind, that every Jev decision entry carries the audit fields, that the signal stays within the bound, and that Jev spends fewer host-observed dollars than Empirical. The recorded-equivalence tests for `FixedPolicy` are unchanged and still pass.

**Decision rule.** From the packet's measurement section: "The decision rule for Jev: it is kept as the provider's default if, on the v2 fixtures, `evolve_jev` solves at least as many as `evolve` with at most half the dollars, over the three repeats; otherwise it stays available and not default." That is: Jev becomes the default only if it solves at least as many v2 fixtures as `fixed` with at most half the dollars over three repeats. The E3 v2 pilot of 2026-10-21 (Claude Sonnet 5.5, medium, K = 6, one repeat; `docs/MODEL_COMPARISON.md`) solved all four v2 fixtures single-shot, 16 of 16 runs in 28 calls for about $0.24, with `evolve` costing four to six times `single` on every fixture, so by the plan's own rule the v2 set is uninformative for that model and the paid Jev-against-fixed comparison will not run on v2 at Sonnet 5.5 as this packet's spec assumed. The simulated comparison above is the evidence in this packet; the paid comparison is deferred to a fixture set on which `single` fails (Haiku 5.5 on v2, or a v3 set), a choice the owner makes. Until that run is made and reported, `fixed` is the default and the table above is not evidence for the rule.

### Measured on a toy fixture

`examples/evolve.py` is a pricing module with three independently checked rules and a stand-in mutator. `benchmarks/evolve_baseline.py` compares the provider with and without crossover on 30 seeds, with the same budget. The result is in `benchmarks/results/evolve-baseline.txt`:

| Arm | Passing candidate found | Evaluations to first pass (median, mean) |
| --- | --- | --- |
| Crossover every third generation | 25/30 | 16, 15.4 |
| Mutation only | 19/30 | 17, 16.0 |

Re-measured after the host began refusing duplicate candidates and the provider stopped spending evaluations once exhausted; the first run, before those changes, was 22/30 either way. On this fixture, crossover found a passing candidate in 25 of 30 seeds against 19 without it, at similar evaluation counts. With 30 seeds that gap is suggestive, not established. The fixture's three independent rules favor recombination by construction, so this says nothing about real repositories. No model was called. Whether a model-backed mutator beats a single patch from the same model at the same cost is the next measurement to make, not a claim.

## Trust boundaries

- The records are the host's claims, like `observe(..., verified=True)`. The runtime recomputes scope from the host's changed paths. It does not re-read Git.
- The Build Thread runs the project's own check commands against candidate code on the developer's machine, in a temporary worktree with a scrubbed environment, a timeout, and bounded output. That is not a sandbox. A contract that treats tests as the oracle should exclude the test files from mutable scope, so a candidate cannot weaken its own judge.
- In the `syberwork` application, candidates arrive over HTTP: `POST /api/cases/{id}/candidates` and `/searches` need a `search` or `operator` credential. `POST /api/cases/{id}/evaluations` needs an `evaluator` credential that holds no `model`, `compiled`, or `search` role and is not the actor that registered the candidate. The application does not read Git itself, so these records are the search host's and evaluator's claims, kept apart by credential. `syberwork init` issues `searcher` and `evaluator` tokens. The console lists candidates with their lineage, evaluation, and promotion state, and prepares a promotion proposal for a person to submit.
- EvoGit is AGPL-3.0. SyberWork does not import it, and `syberlabs/evolve.py` is written from the published method, not from EvoGit's source. Running EvoGit itself would mean running it as a separate program behind a `CommandProvider`, which is a separate licensing decision.
