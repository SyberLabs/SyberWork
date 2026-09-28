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
| `budget` | Candidates and evaluations per thread; seconds per search | `budget_exhausted` at registration or evaluation. The host enforces the deadline. |
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
| `candidate_registered` | id, commit, tree, base, parent candidate ids, operator, provider, changed paths, recomputed scope and limit violations, diff digest and size, provider `signal`, note | The host wrote the candidate's commit on a non-authoritative ref |
| `candidate_evaluated` | candidate, commit, tree, evaluator, and per check: state, exit code, duration, output digest, output tail | The host ran the checks |
| `search_finished` | why it stopped, candidates, recommended ids, evaluations used, elapsed time, error code | The provider returned, ran out of budget, or failed |

`signal` and `recommended` are the provider's own opinion. They are shown to the developer as such and are never read by admission.

## Promotion

A promotion is a proposal of the contract's promotion action with exactly `{candidate, commit, base}`. Admission runs the whole ordered rule list. `candidate.promotable` sits after the argument bindings and before `approval.required`. For the promotion action it denies, in order:

1. `candidate_promotion_origin`: the proposal's origin is not `human`, or the proposer holds an automation role (`model`, `compiled`, `search`).
2. `candidate_promotion_role`: the proposer holds none of `promotion.roles`.
3. `candidate_args_invalid`: the arguments are not exactly `{candidate, commit, base}` as strings.
4. `candidate_unknown` or `candidate_mismatch`: no registered candidate, or its commit or base differs.
5. `candidate_out_of_scope`: any scope or size violation.
6. `candidate_not_evaluated`, `candidate_check_missing:<name>`, `candidate_check_failed:<name>`, or `candidate_evidence_stale`: judged by the newest host evaluation of that candidate's registered commit and tree.

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
