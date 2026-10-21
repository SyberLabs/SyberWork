# Candidates and search providers

SyberWork is the authority and evidence layer. A search provider is an artifact-search layer: it proposes changes. The provider can be a person's patch, a model adapter, or an EvoGit-style evolutionary search. Its output is **provisional** until the same contract, evidence, policy, and approval gates that govern every other SyberWork action admit it.

> Lineage is provenance, not evidence of superiority.

A candidate records where it came from: its parents, the operator that produced it, and the provider name and revision. None of that counts toward promotion. A crossover child of two passing parents, carrying the provider's own `fitness: 1.0`, is still `candidate_not_evaluated` until the host runs the contract's checks on that child's exact tree.

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

Where the provider branches is a seam. Before every step, `EvolutionaryProvider` asks a `BranchPolicy` (`syberlabs.search`) for the next action and hands it a `Situation`: the population as `Individual(id, score, passed)` slots in order, the capacity, the remaining budget and whether there is room, the outcomes so far per action kind (`OperatorStats`: attempted, produced, evaluated, improved, passed), whether the mutator is exhausted, whether an exchange is configured, the number of decisions taken, and the provider's seeded random stream. A policy never sees the session, the repository or file contents. Its `name` and `revision` are checked like a provider's when the provider is built (`invalid_provider`).

The action set is closed: `Mutate(parent, slot, generation)`, `Crossover(a, b, slots, generation)`, `Migrate(generation)`, `Evaluate(candidate, generation)` and `Stop(reason, generation)`; `ACTION_KINDS` lists their kinds. Actions name slots, not only ids, because a crossover win leaves one id in two slots. The provider executes each action with the mechanics above and keeps the pairwise replacement rule itself, so a policy cannot keep a worse child. It refuses, with `invalid_policy`, an action outside the set, a generation label lower than the last one or above `generations + 1`, and a slot that does not hold the id the action names; the search then ends with `stopped: "error"` and `error: "invalid_policy"`.

`FixedPolicy` is today's behaviour as a policy and the default when `EvolutionaryProvider(...)` is given no `policy`: seeds are evaluated while there is room, the population is filled from `base` with at most four attempts per slot, then each generation is a mutation round over every slot in order, or a crossover round over a random pairing every `crossover_every` generations and whenever the mutator is exhausted, followed by a migration on its cadence; the search stops at the top of a generation when the generations are used up, there is no room, an individual passes (with `stop_when_passing`) or the population is empty, and inside a crossover round when the mutator is exhausted and recombination improved nothing. Room is every budget dimension above zero; on a host that prices usage `remaining()` also carries `max_dollars` (None without a dollar bound), so the dollars running out is `Stop("budget")` like the counts, and the host writes `stopped: "budget_exhausted"`. `tests/test_evolve.py` holds it to the loop of `main` `d0bebb8` twice over: against a recording taken on `main` (each candidate's id, operator, parents, evaluation and checks passed; the legacy-shaped trace; the recommendation and the evaluation count) and side by side with a verbatim copy of the old loop on identical fixtures, including the stop on an exhausted mutator and rounds over a single slot. Commit ids are not compared: they differ run to run. Its `_round_kind` hook is the one place a later policy chooses between a mutation round and a crossover round.

**One draw.** The mutator and the crossover draw from the provider's seeded stream in a fixed order, so a policy may draw from `situation.rng` only once per round and only for one thing: `rng.sample(range(n), n)` to pair a crossover round, at the start of that round. Any other randomness a policy needs comes from its own `random.Random`, seeded from a constant and `situation.decisions`. An extra draw changes every child after it, which the equality tests catch for `FixedPolicy`.

**Generation labels.** The policy labels its rounds and the label never decreases. The provider closes a generation with the usual `{"generation", "population"}` trace entry when the label advances; a label the policy skips is a generation in which nothing happened and is closed the same way. `Stop` with `budget`, `passing`, `empty` or `generations` ends the search at a generation boundary; any other reason, such as `exhausted`, ends it inside the round the label names and closes that round with a `"stopped"` entry, as before.

**Records.** Every decision is an entry in `provider.trace`, interleaved with the entries above: `{"decision": n, "generation": g, "policy": {"name", "revision"}, "action": kind, ...}` with `parent` and `slot`, `pair` and `slots`, `candidate`, or `reason`, and for an executed action an `outcome` of `{"candidate", "score", "improved", "evaluated"}`. A reader that wants the old trace filters `[e for e in trace if "decision" not in e]`; the stop decision is always the last entry. Every child the provider registers carries `signal.policy = {"name", "revision", "decision"}`, so a `candidate_registered` event links to its decision; the signal stays far below the host's 4,096-byte bound (tested under 1,024 bytes with a 64-character mutator name, a 128-character revision and a full `model_usage`), and `search_started` and `search_finished` are unchanged. Migrants carry no signal and a seed evaluation registers nothing, so those decisions are in the trace only. The trace lives in memory on the provider object and is not journaled; a policy learns from one search.

**Later policies.** This packet ships `FixedPolicy` only, claims nothing about any policy's merit, and re-publishes no number. The directions behind the same seam, each a later packet: `EmpiricalPolicy`, overriding `_round_kind` with a bandit over this search's `OperatorStats`; a Bough policy consulting a table compiled offline from a population summary; and a Jev policy choosing among priced actions. Any claim about any of them, `FixedPolicy` included, waits on E3 (`docs/E3_BENCHMARK_PLAN.md`), a real-model benchmark that a named human runs.

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
