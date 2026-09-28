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

## Trust boundaries

- The records are the host's claims, like `observe(..., verified=True)`. The runtime recomputes scope from the host's changed paths. It does not re-read Git.
- The Build Thread runs the project's own check commands against candidate code on the developer's machine, in a temporary worktree with a scrubbed environment, a timeout, and bounded output. That is not a sandbox. A contract that treats tests as the oracle should exclude the test files from mutable scope, so a candidate cannot weaken its own judge.
- `syberwork.Work` validates and stores evolution contracts, but it has no API to register candidates, so its promotion proposals fail closed with `candidate_unknown`.
- EvoGit is AGPL-3.0. SyberWork does not import it. An EvoGit-style provider behind `SearchProvider` must be an independent implementation of the published method, or run EvoGit as a separate program.
