# Metrics: per-work-item numbers from the journal

```bash
syberlabs metrics                         # one row per thread in this home's journal
syberlabs metrics --json                  # every number with the event hashes it came from, and a summary
syberlabs metrics --csv --since 2026-10-20
syberlabs metrics --check                 # exit 1 when a delivered item has an unknown metric (for CI)
syberlabs metrics --from-export receipts/*.json   # records written by `syberlabs export`; no repository needed
```

The service serves the same fold at `GET /metrics/work_items` and `GET /metrics/work_items/<thread>` (bearer token, read only, beside `GET /metrics/<key>`), and the operator page (`python -m syberlabs.dashboard`) has a **Work items** section with the nine columns.

## The rule

Every number is computed from the journal, never counted alongside it. `syberlabs.metrics.work_items(events)` is a pure function of a thread's events: two readers of the same record get the same numbers, and each number carries a `basis`, the hashes of the events it came from (`provenance(item, metric)` lists them with their sequence numbers and kinds), so a certificate can cite them. Anything the journal did not carry becomes a journal record first (section 3) and a metric second. Reading writes nothing.

A **work item** is one Build Thread: from `case_created` to the merge of the pull request its `github_pull_request` effect opened, or to `case_cancelled`. An item that never publishes counts as started and not delivered.

**`unknown` is a value.** When the fact a metric needs is not in the record, the metric is `unknown` (`"unknown"` in JSON and CSV, `value: None` in Python), never 0. Zero means the record shows none; unknown means the record cannot say. `summary` computes means and medians over known values and counts the unknowns beside them.

## 2. The nine metrics

| Metric | Derivation from the journal | Unknown when |
|---|---|---|
| **cost** ($) | Per search, `search_finished.spend.dollars`; for a search with no `search_finished`, the `candidate_registered.spend.dollars` of its registrations. Refusal spend (`proposal_refused.value.spend`) is pending usage the next registration or the search total already carries, so it is not added again. Labelled `cost_basis`: `host_observed` when a call crossed the host's meter, `declared` when a provider only declared `model_usage`, `none` when nothing was recorded. | basis `declared` or `none`, or a model the contract does not price |
| **turns** (calls) | Records carrying declared `model_usage`: registrations and `proposal_refused` observations (adjacent records with identical usage are one call: the items after a repair call's first are refused `limit` with that call's usage), plus `critic_usage` facts. Migrations are not calls. | any registration carries no usage (its calls happened outside the host, for example `--from-worktree`) |
| **agent_time** (hours) | `pr_merged.merged_at` minus `case_created.at`. Human review time is inside the interval by construction. | no `pr_merged` fact |
| **fix_rounds** | Registrations that follow a failed evaluation (a required check of the contract not `passed`; every recorded check when the contract is not supplied), each failed evaluation counted once. First attempt right is `fix_rounds == 0` with the first candidate accepted. | never |
| **web_lookups** | Sum of `signal.tool_usage.web_search + web_fetch` over registrations. | any registration did not declare either |
| **githits_lookups** | Sum of `signal.tool_usage.githits` over registrations. | any registration did not declare it |
| **delivered** | `true` with a `pr_merged` fact (for the opened pull request's number); `false` when no pull request was opened or the thread was cancelled. `opened` is a separate column: a `github_pull_request` effect that succeeded, or a verified reconciliation of one. | a pull request is open and no `pr_merged` fact is in the record |
| **review_rejections** | Critic checks (`critic`, `critic.*`) that did not pass, or that passed with a host critic observation counting a high finding (what `critic.no_high_finding` refuses); plus `review` facts with verdict `fail` and `pr_review` facts with state `changes_requested`. | never |
| **human_interventions** | `human_intervention` facts, plus `proposed`, `approved` and `case_cancelled` events whose actor is neither `syberlabs.host` nor the thread's unattended runner (`unattended.by`) and whose proposal no fact names. | never |

`summary` reports `items`, `opened`, `cancelled`, `delivered` (`yes`, `no`, `unknown`), `delivered_rate` (only when no item's delivery is unknown; otherwise `delivered_rate_bounds` gives the range), `opened_rate`, the count per `cost_basis`, and per metric `known`, `unknown`, `total`, `mean`, `median`.

## 3. The facts, and who writes them

All are `observed` events written by the host (`actor: syberlabs.host`, `verified: true`) through `Session.observe`, the route `unattended`, `base_evaluation` and `proposal_refused` use. No event kind is added; `spec/event.schema.json` and the golden traces are unchanged.

- **`tool_usage`** on the candidate's `signal`. The adapter protocol's response may carry `tool_usage: {web_search, web_fetch, githits, other: {name: n}}` beside `usage`. `CommandProvider`, `RepairProvider` and `CommandMutator` (via the evolutionary provider's signal) copy it through `providers.tool_usage`: integer counts only, at most 16 `other` names of at most 64 characters. An item's own `signal.tool_usage` is dropped; only the call's declaration reaches the record, and an adapter that declares nothing leaves it absent. `adapters/anthropic_adapter.py` passes through the SDK's `usage.server_tool_use` (`web_search_requests`, `web_fetch_requests`) when the SDK reports it; it reports no GitHits count, so `githits_lookups` stays unknown for that adapter.
- **`human_intervention`**, value `{kind, by, at, note}` and `proposal` for an act on one; `kind` is `message`, `commit`, `approve`, `edit` or `override`. Written by `syberlabs note --intervention KIND [--note TEXT]`, and automatically by `syberlabs accept` (`approve`), `syberlabs approve` (`approve`) and `syberlabs check --rerun` (`override`) when the acting actor is a person: not `syberlabs.host` and not the thread's unattended runner. `syberlabs run` writes none. A note is at most 500 printable characters; one that looks like a credential is refused.
- **`review`**, value `{verdict, reviewer, revision, findings_high, comment_url, by, at}`, versioned by the revision. Written by `syberlabs review --record --verdict pass|fail --reviewer NAME --revision R [--findings-high N] [--url URL]`; `syberlabs review` lists them.
- **`critic_usage`**, value `{candidate, check, usage}`. A findings document may declare `usage` (`spec/finding.schema.json`); the check record's fields are frozen (`spec/candidate.schema.json`, `evolution.evaluation_record`), so the host writes it as a second observation beside the critic's, same source `check:<name>` and version (the output digest).
- **`pr_merged`** `{number, merged_at, merge_commit, merged_by}` and **`pr_review`** `{state, reviewer, at}` are read by the suite already; the poll that writes them is the next packet (`spec/GAPS.md` 49).

## 4. Accuracy, stated honestly

- **Cost** is exact for host-metered API lanes and unknown for others; the label says which. Critic usage is declared by the critic, so it counts as a turn and is left out of host-observed dollars.
- **Turns** count host-recorded calls with declared usage. A harness's internal loop is one call. A stand-in adapter that reports identical usage on two consecutive calls with nothing recorded between them would be read as one call.
- **Agent time** includes human review time; `human_intervention` facts let a reader split it, and the `unattended` flag marks items where no person was asked.
- **Lookups** are adapter-declared (`declared`, not `host_observed`) until a grounding station records GitHits reads itself.
- **Interventions** undercount messages to an agent made outside the CLI, and cannot tell an agent from a person when both act under one identity: on a thread without an unattended runner, every `proposed` by a non-host actor is a person's act.
- **Delivered** is `unknown`, not `false`, for every opened pull request until the merge poll exists; `--check` only judges items the record shows as delivered.
