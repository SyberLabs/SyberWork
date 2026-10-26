# SyberEval: pre-registered evaluations through the real Build Thread

SyberEval measures SyberWork's agents as governed workflows. It asks whether a provider configuration, run through the real Build Thread, produces admitted changes that are correct, safe and cheap. This page covers the core: specs, tasks, arms, the runner, its evidence bundles, and the preflight gate (package `syberlabs/eval/`, packet `eval-core`). Scoring, the journal safety gate and the report come in the next packet, `eval-score`. The agent-facing safety tasks come in `eval-safety-fixtures` (`spec/GAPS.md`, 60). The E3 harness (`benchmarks/model_vs_patch.py`, [MODEL_COMPARISON.md](MODEL_COMPARISON.md)) is still the tool for quick research. SyberEval is the instrument whose numbers are published.

## Principles

1. **Three outcomes, never blended.** Safety is a hard gate. Task quality is host-verified acceptance on hidden checks, with partial results labelled partial. Efficiency is the nine metrics of [METRICS.md](METRICS.md), and unknown stays unknown. `eval-core` records all three. `eval-score` computes them.
2. **Pre-registered.** The question, arms, task set (by digest), metrics, budget, seeds, run order, exclusions, decision rule and reporting commitments are frozen in a spec whose digest every bundle cites. `run` refuses an unregistered or modified spec, and a task that changed since registration.
3. **Real workflow.** Every run is a Build Thread: a contract, host checks on the exact candidate tree, admission, a journal and a certificate. The admission decision is part of the result, and so are refused and blocked proposals.
4. **Hidden acceptance.** A task's acceptance checks are held by the host outside the run's repository. They are not in the agent's worktree, its requests or its candidates (see Hidden checks for what that does and does not cover).
5. **The task is the unit.** Arms are paired on the same task, base commit, seed and budget, in a randomized registered order. Seeds are aggregated within a task before tasks are compared.
6. **Evidence retained.** Every run leaves a bundle of digested files. Reports cite bundle digests.

## Files

```
evals/
  tasks/<task-id>/task.json            the task (schema syberlabs.eval/task.v1)
  tasks/<task-id>/hidden/...           its hidden acceptance checks and data, held by the host
  <eval-id>/spec.json                  the spec (schema syberlabs.eval/spec.v1)
  <eval-id>/registration.json          written once by register
  <eval-id>/preflight.json             the last preflight, for one commit
  <eval-id>/runs/<run-id>/bundle.json  one per (task, arm, seed), with the files beside it
```

A run id is `<task>.<arm>.s<seed>`. The package runs from a SyberWork checkout, not from the wheel: it reads `benchmarks/fixtures.py` and the adapters, and `pyproject.toml` packages `syberlabs` without its subpackages. The `syberlabs` command in a wheel install has no `eval` subcommand.

## Commands

```sh
syberlabs eval tasks import-fixtures [--fixtures NAME ...] [--force]  # default: the four v3 and four v4 fixtures
syberlabs eval tasks list [ID ...] [--json]      # each task's digest and the set digest of those listed
syberlabs eval register evals/<id>/spec.json     # validate, check the set digest, write registration.json
syberlabs eval preflight <id>                    # the deterministic suites, recorded for this commit
syberlabs eval run <id> [--only TASK,ARM,SEED] [--dry] [--work DIR] [--keep-work] [--seed-env NAME] [--json]
syberlabs eval score <id> | report <id>          # exit 2: "not yet: eval-score packet"
```

`--evals DIR` (before the subcommand) names another evals directory. In `--only`, an empty part or `*` matches anything. `--dry` prints the registered order with each run's state, `done` or `pending`, and runs nothing. It works before a preflight. `run` exits 1 when the eval stopped on its cap.

## The spec

`evals/<id>/spec.json`, checked against `spec.SCHEMA` (JSON Schema vocabulary, validated with the standard library) and then against cross-field rules:

| Field | What it holds |
| --- | --- |
| `schema`, `id` | `syberlabs.eval/spec.v1`; the id is also the directory's name |
| `question`, `hypothesis` | text |
| `arms` | the arm configurations (below); names are distinct |
| `tasks` | `{ids, set_digest}`; `register` refuses `task_set_mismatch` unless the tasks as they are digest to `set_digest` (print it with `eval tasks list`) |
| `primary_metric`, `secondary_metrics` | `{name, definition}` |
| `budget` | `calls`, `dollars` and `seconds` per run; `total_dollars` for the eval; `prices`, the governor's price table (`economic.check_prices`); `strict` (default true: an unpriced model refuses acceptance) |
| `seeds`, `order_seed` | integers |
| `exclusions` | `[{id, rule}]` |
| `decision_rule` | `{metric, comparison: paired_difference, treatment, baseline, operator, threshold, unit: task, aggregation: mean_over_seeds, test}`; the metric is the primary one, and treatment and baseline are two different arms of the spec |
| `reporting` | what is reported whatever the outcome |

`register` writes `registration.json`: `{spec_digest, task_digests, task_set_digest, arm_digests, registered_at, git_commit, git_dirty}`. The spec digest is `syberlabs.canonical.digest` of the parsed spec, so whitespace and line endings do not move it and any field does. Registering the same spec again returns the registration. Registering a changed spec is refused (`already_registered`), because a changed question is a new eval id. Before it runs anything, `run` refuses `spec_unregistered`, `spec_modified` (the spec, or an arm, no longer digests to its registration) and `task_modified`.

## Tasks

`evals/tasks/<id>/task.json` (`tasks.TASK_SCHEMA`):

- `source`: `fixture:<name>` (a fixture of `benchmarks/fixtures.py`; `source_digest` pins its visible files, and a run refuses `task_source_changed` when they moved) or `repo:<git url>@<40-hex base>`.
- `objective` and `scope` (`paths`, `exclude`, `max_files`, `max_diff_bytes`: the run contract's mutable scope and the paths a provider may read).
- `checks`: visible checks, which run like any contract check. They may be empty, and they may not name `{hidden}`.
- `hidden.checks`: the acceptance checks. In their argv, `{python}` is the host's interpreter and `{hidden}` is the absolute path of the host's copy of `hidden/`.
- `safety` (`expectations`, `canaries`: strings planted in protected data, which `eval-score` searches outbound records for), `provenance`, `contamination` (`status`: `unknown`, `private`, `public_repository` or `public_benchmark`, plus a `note`), and `labels`.

The task digest is `canonical.digest({"task": <parsed task.json>, "hidden": {<path>: <sha256>}})` over every file under `hidden/`. A hidden file that is UTF-8 without NUL bytes is text, and its CRLF becomes LF before it is digested and staged, so a Windows checkout and a POSIX one agree. Any other file is used byte for byte.

`eval tasks import-fixtures` writes `<generation>-<fixture>` for each fixture (`v3-journal_chain`, `v4-cell_metrics`, ...). Each fixture check's code moves into `hidden/checks.json`, and its hidden check runs it through `tasks.LOADER`. `task.json` holds no check code. The run's repository holds the fixture's visible files and nothing else, committed with a fixed identity and fixed dates, so every run of a task starts from the same base commit. The importer is deterministic, and `tests/test_eval_core.py` checks that the committed `evals/tasks` is what it writes from the current fixtures. All eight tasks are `public_repository`: the fixtures and their checks have been in this public repository since 2026-10-23 (#67, #75). They are hidden from the agent's worktree and requests, not from a model trained on this repository or from a lookup into it.

## Hidden checks

For each run the host copies the task's `hidden/` into a directory of its own (`<work>/host/hidden`) outside the run's repository and its kit home. It verifies the copy against the registered task digest, then appends every hidden check to the run's contract as a required check named `hidden.<name>`, with argv naming that copy by absolute path. This is the pattern `critic.rules` and the sharded test runner use ([CRITIC.md](CRITIC.md)). The check runs as every check does (`syberlabs/checks.py`): in a detached worktree of the candidate, with a scrubbed environment, reading the candidate's files from its working directory. There is no contract schema change, no new event kind and no kernel change.

What this gives, and `tests/test_eval_runner.py` shows with canary strings planted in a hidden check's code and in a hidden data file:

- **Not in the worktree.** No hidden file and no canary is in the run's repository: not in its working tree, and not in any object of any ref.
- **Not in a request.** No canary and no path of the host's copy appears in any request the provider was sent. `GitSearchSpace` serves the repository only.
- **Not in a candidate.** No canary is in any candidate's diff, nor in any file of the bundle, the journal included.
- **Not editable.** A candidate that writes its own `src/hidden/checks.json` changes nothing, because the contract names the host's copy.
- **No source in output.** `tasks.LOADER` compiles a check's code under the name `<hidden:NAME>`, so a traceback prints no source line and no host path.

What it does not give:

- **Results reach the agent.** A hidden check's state and output tail are recorded like any check's, and a repair or evolve arm receives them as feedback. This is the E3 design, where an assertion message names the expectation it checks, and it is what lets `repair` converge. A check that should reveal less must print less.
- **Generic argv can leak.** A hidden check that runs a script directly (`["{python}", "{hidden}/check.py"]`) prints that script's path and failing line in a traceback. The fixture importer always uses `LOADER`.
- **Not a sandbox.** The host's copy is an ordinary directory. An agent that runs with file tools outside its worktree, such as a `claude-code` arm, could read it if it learned the path, which appears only in the run contract under the kit home. The worktree is not a sandbox (research register N4).

## Arms

An arm is an object in the spec's `arms`, with an explicit `model` and `effort`. Its digest is `canonical.digest` of the object. `adapter` is the model command; `{python}` and `{harness}` (this checkout) are resolved at run time. K is `min(max_calls or budget.calls, budget.calls)`.

| `kind` | Provider |
| --- | --- |
| `single` | `providers.CommandProvider(adapter)`: one call. Its request carries no `seed`, so a seeded adapter is seeded only on the other arms. |
| `repair` | `providers.RepairProvider(adapter, max_calls=K)`, the default proposer path |
| `evolve` | `evolve.EvolutionaryProvider(CommandMutator(adapter, max_calls=K))`: fixed policy, stops at the first pass, population `min(4, K)` unless given (the E3 shape) |
| `evolve_jev` | the same with `JevPolicy`, its price prior from the spec's price table |
| `claude-code` | `claude_code.ClaudeCodeProvider` with `lane`, `bare`, `trusted_tree`, `tools`, `max_turns`, `mcp_config`, `executable` (a stand-in for tests), and `githits` passed by name to the provider's keyword (`arm_unsupported` if it has none) |

While an arm proposes, the process environment holds `SYBERLABS_MODEL` and `SYBERLABS_EFFORT` (read by `adapters/anthropic_adapter.py`), plus `SYBERLABS_FIXTURE` for the simulated model only. The certificate seed variable, `GITHUB_TOKEN` and `GH_TOKEN` are removed, so no provider process inherits them, and the environment is restored afterwards. The runner does not set Claude Code's model. A `claude-code` arm's `model` and `effort` are the spec's declaration, and the model that actually served is in the candidate's `signal.harness`.

## The runner and its bundles

`eval run` expands tasks × arms × seeds in spec order and shuffles them with a Fisher-Yates shuffle drawn from `random.Random(order_seed).random()`, a sequence Python keeps stable across versions. Each run:

1. Creates a fresh work directory holding the run's repository at the task's base (a fixture's visible files in a new repository with `core.autocrlf=false`, or a clone checked out as `eval-base`), a kit home outside it, and the host's hidden copy.
2. Writes the run contract `eval-run.v1`. It holds the task's scope, the visible checks and the `hidden.*` checks (all required), the spec's prices, `max_dollars` and `max_seconds` from the per-run budget, and `strict` from the spec. It has one action, `accept_change`, and no publishing action, so an evaluation cannot push and the tests check that it never proposes anything else.
3. Starts the thread with `Kit.start(..., act_class="R", unattended=True)` as actor `syberlabs.eval`, and `propose`s through the arm with the run's seed. The host's governor prices every call.
4. Takes the search's recommended candidate (else its last in-scope one), runs `Thread.check` (every check on that exact tree), and runs `Thread.accept` only when admission allows. Acceptance moves the thread's branch in the run's own repository and nothing else.
5. Keeps the evidence: `journal.json` (`Kit.export`), `trace.json` (the provider's trace), `contract.json`, and `repo.bundle` (`git bundle --all` of the run's repository, so candidates can be re-derived). When the run accepted a candidate and `--seed-env` names a set variable, it also signs `certificate.json` and `certificate.pub` with that key and verifies them at once against the repository and the journal. Otherwise the run is recorded `uncertified` or `not_accepted`.
6. Writes `bundle.json`: `spec_digest`, `task_digest`, `arm_digest`, `seed`, `order_index`, `base` (commit, tree, source), `thread`, `harness` (`commit`, `dirty`, eval package `VERSION`), `provider` (kind, name, revision, class, model, effort, the SHA-256 of each adapter file), `versions` (Python, platform, git, and Claude Code for a `claude-code` arm), `started_at`, `ended_at`, `journal_path`, `certificate_path`, `records_paths`, `outcome`, and `files` (the SHA-256 of every other file in the run's directory).

`outcome` is descriptive, not a score. It holds `status` (`accepted`, `not_acceptable`, `refused` or `no_candidate`), `reason`, `candidate`, `accepted_commit`, the chosen candidate's check states (`checks` and the `hidden` subset), `search` (`stopped`, `error`, `recommended`), counts of candidates, evaluations and refused proposals, the provider's `calls`, host-observed `dollars`, `host_observed`, `unknown_models`, and `certification`.

**Resuming.** A run writes into `runs/<run-id>.partial`, which is renamed when it completes. A run that was killed leaves no bundle and runs again; a run with a bundle is skipped. A bundle written under another spec digest is refused (`bundle_mismatch`).

**The dollar cap.** A run starts only when the dollars the eval's bundles record, plus the spec's per-run `dollars`, fit under `total_dollars`. Otherwise the eval stops with `eval_budget_exhausted`. Within a run the governor's own bound applies: the call that crosses `max_dollars` is recorded, and the next is refused ([BUILD_THREAD.md](BUILD_THREAD.md), Spend and the dollar budget). A run whose spend names a model the table does not price stops the eval with `eval_spend_unknown`, because its cost cannot be counted. On a `claude-code` login lane (`subscription_local`, `seat_team_enterprise`) the host prices nothing, so the cap does not bound those runs; only calls, seconds and `max_turns` do (`spec/GAPS.md`, 60).

## Preflight

`eval preflight <id>` runs `scripts/run_shards.py`, `python -m conformance.run`, `benchmarks/run_scenarios.py` and `python -m unittest discover -s tests -p "test_eval_*.py"` from the checkout, with `PYTHONPATH` set to it and with the certificate seed and GitHub tokens removed from the environment. It writes `preflight.json` with the commit, whether the checkout differs from it outside `evals/`, and each command's exit code, seconds, output digest and output tail. `run` refuses `preflight_missing`, `preflight_failed`, `preflight_stale` (the record names another commit) and `preflight_dirty` (the checkout differs from its commit outside `evals/`, now or when the preflight ran). Untracked files under `evals/` are not dirt, because that is where runs write.

## E5 and E6

**E5, `evals/e5-single-vs-repair`, framework validation: registered, not run.** It compares `single` with `repair` on the eight v3 and v4 tasks, using Claude Sonnet 5.5 at medium effort with K = 6, and seeds 1, 2 and 3 (48 runs in the order `order_seed` 20261026 gives). The budget is $1.00, 1800 s and 6 calls per run, with a $10.00 cap on the eval. The primary metric is the per-task pass rate on the hidden checks. The decision rule is repair ≥ single on the paired per-task difference, with a sign test over tasks beside it. E3 already answered this question (repair wins), so E5 checks the instrument, not the hypothesis. To run it, which spends money:

```sh
export ANTHROPIC_API_KEY=...                      # the adapter's credential; never written by SyberEval
export SYBERLABS_CERTIFICATE_SEED=...             # the factory key, to certify accepted runs
syberlabs eval preflight e5-single-vs-repair      # on a clean checkout of the commit you will run
syberlabs eval run e5-single-vs-repair --dry      # the order
syberlabs eval run e5-single-vs-repair            # resumable; stops at the cap
```

**E6, the GitHits pilot.** It is registered once the 21 pilot items are signed off. It will use two `claude-code` arms that differ only in `githits`, on `repo:` tasks pinned to their base commits.

## Not here yet

`eval-score` will add the safety gate computed from the journal, the scorer (pass, partial and fail on the hidden checks; the metrics suite; critic findings; review facts), the paired analysis with the decision rule applied mechanically, and `score` and `report`. `eval-safety-fixtures` will add the agent-facing safety tasks (`evals/tasks/safety-*`) and the validation of the gate. What else is open is in `spec/GAPS.md`, 60.
