# Build Thread

A Build Thread is one governed change to one local Git repository. It shows why a candidate was accepted or refused, what it changed, and what actually happened. It can be resumed from its record after a crash. It needs Python 3.11+ and `git`, and no account, model, or network.

## Ten minutes

```sh
pip install <this wheel>                   # or PYTHONPATH=<checkout>
cd your-repository
syberlabs init                             # writes .syberlabs/ and excludes it from Git
$EDITOR .syberlabs/contracts/repo-change.v1.json   # name your real checks
syberlabs start "Add a CSV export" --paths src tests
syberlabs context                          # exactly what a provider will read
# make the edit yourself, or let a coding tool make it, then:
syberlabs propose --from-worktree
syberlabs check c1                         # runs the checks on c1's exact tree
syberlabs diff c1
syberlabs accept c1                        # moves refs/heads/syberlabs/<thread> only
syberlabs status
```

`init` guesses the test command (`tests/test*.py`, `package.json`, `Cargo.toml`, `go.mod`). When it cannot, it writes a `configure` check that fails until you name a real one. An unchecked change never looks accepted.

The same thing in Python (`examples/build_thread.py` is the complete, runnable version):

```python
from syberlabs import Kit
from syberlabs.providers import PatchProvider

kit = Kit.local(".syberlabs")
thread = kit.start("Add a CSV export", contract="repo-change.v1", paths=["src/", "tests/"])
[candidate] = thread.propose(PatchProvider({"src/export.py": source}))
verdict = thread.check(candidate.id)
print(verdict.summary())
if verdict.acceptable:
    receipt = thread.accept(candidate.id)
```

## What each step does

| Step | Records | Never does |
| --- | --- | --- |
| `start` | `case_created`, and the base commit as a verified `git` observation | change branches or the working tree |
| `attach_source` / `--paths` | the read scope, as an observation | widen the contract's mutable scope |
| `context` | path, blob id, line range, byte count, and the reason each excerpt was chosen, plus a digest. Scoring uses path and symbol matches from `git grep`, with no embeddings. | store the excerpt text; it is re-read from Git when a provider runs |
| `propose` | `search_started`, one `candidate_registered` per candidate (Git commit on `refs/syberlabs/candidates/<thread>/cN`, parents, operator, provider revision, changed paths), `search_finished` | touch the working tree or index; candidates are written with a temporary index and `commit-tree` |
| `check` | `candidate_evaluated`: per-check state, exit code, duration, a SHA-256 of all output, and a 4 KB tail. Nothing reruns while the tree has a fresh result. | run code from a candidate that is outside scope, or count a provider's claim as a result |
| `accept` | `proposed`, `decision`, then `effect_started` and `effect_succeeded` | push, merge, open a PR, or move `main`. The only write is a compare-and-swap of the thread's target branch. |
| `recover` | `reconciled` (the branch has the commit) or `effect_rejected` with status `not_applied` (it does not) | guess: the target branch is read under the journal lock |

The verdict answers one question: *if you accepted this now, what would admission say?* It runs the same rule list as `accept`, via `Session.preview`, and records nothing. It lists every declared check as `passed`, `failed`, `timed_out`, `error`, or `not_run`, and names any required check that has no result. A provider's `signal` is printed as "provider says (unverified)" and never changes the answer.

## Providers

A provider gets a `SearchSpace` and nothing else. It can list and read files in the read scope, merge text, submit edits, ask the host to evaluate a candidate, and see its remaining budget. It cannot reach the session, approvals, the target branch, or `accept`.

- `PatchProvider(changes)` submits one edit. `syberlabs propose --from-worktree` uses it with the working-tree edits inside both the attached sources and the contract scope, and lists what it left out.
- `CommandProvider(argv)` is the model seam. It writes a JSON request to the command's stdin (objective, base, scope, budget, context excerpts, file list) and reads `{"candidates": [{"changes": {...}, "message": ..., "signal": ...}], "recommended": [...]}` from its stdout. The adapter for Kev, Jev, or any other model is that command. SyberLabs imports no model SDK and makes no model call itself.
- `FunctionProvider(fn)` wraps a Python callable.
- `EvolutionaryProvider(mutator)` is an EvoGit-style search. It keeps a population of candidates, mutates them, merges non-ancestors, and selects on the host's check results. It needs a contract whose `operators` include `mutation` and `crossover`. See [EVOLUTION.md](EVOLUTION.md).

An in-process provider runs as Python code in your process. The interface keeps model *output* away from authority; it is not a sandbox for untrusted provider *code*. Run untrusted providers as a `CommandProvider`.

## Contracts, policy, and people

`.syberlabs/contracts/<id>.v<N>.json` files are installed when the kit opens. A published version is immutable. Editing `repo-change.v1.json` after its first use is refused with the file name to use instead, and `syberlabs contract-diff repo-change.v1 repo-change.v2` shows what changed. A thread stays on the version it started with. `.syberlabs/policy.json` is the global policy; a contract cannot widen it.

For a team, set the same `approval_role` on the promotion action and on `evolution.promotion`. Then `accept` returns `needs_approval`, a different person with that role runs `syberlabs approve c1 --actor lead --role maintainer`, and the proposer runs `accept` again. Proposers holding `model`, `compiled`, or `search` are refused whatever origin they claim.

To land an accepted change on `main`, merge `syberlabs/<thread>` yourself. Setting `promotion.target_ref` to an existing branch makes acceptance a fast-forward from the thread base instead: the branch must still be at the base (otherwise 412, no write), and it must not be checked out (otherwise 409, no write).

## Publishing an accepted change

Accepting never publishes. Pushing the branch, opening a pull request, and running a publish command are three more contract actions. Each has its own authority and its own effect, and all are admitted like any other action. Add them to a new contract version, list them in `policy.json`, and define their destinations in `.syberlabs/actions.json`:

```json
// contract actions
"push_branch":       {"requires_effect": "accept_change", "required_facts": [{"key": "accepted", "source": "git", "verified": true}], "arguments": {"commit": "fact:accepted.commit"}},
"open_pull_request": {"requires_effect": "push_branch",   "required_facts": [...same...], "arguments": {"commit": "fact:accepted.commit"}},
"publish_package":   {"requires_effect": "accept_change", "required_facts": [...same...], "arguments": {"commit": "fact:accepted.commit"}, "approval_role": "maintainer"}

// .syberlabs/actions.json (immutable by name, like every action definition)
{"push_branch": {"kind": "local", "effect": "git_push", "remote": "origin"},
 "open_pull_request": {"kind": "local", "effect": "github_pull_request", "api": "https://api.github.com",
                       "repository": "owner/name", "base": "main", "token_env": "GITHUB_TOKEN"},
 "publish_package": {"kind": "local", "effect": "command", "argv": ["./scripts/publish"],
                     "status_argv": ["./scripts/publish-status"], "no_write_exit_codes": [75]}}
```

Then run `syberlabs publish push_branch`, `syberlabs publish open_pull_request`, or `syberlabs publish publish_package`. Before proposing, the host reads the target branch and records it as the verified `accepted` fact. The argument binding therefore makes it impossible to publish anything but the accepted commit.

| Effect | Idempotency and no-write | Status lookup |
| --- | --- | --- |
| `git_push` | `--force-with-lease`: the branch must be absent, or at the thread base for a branch without `{thread}`. A refused ref is a guaranteed no-write (412). | `git ls-remote` |
| `github_pull_request` | Looks for a pull request from the branch at that commit before creating one. 401, 403, 404, and 422 happen before creation (409, no write). The body cites the thread, the candidate, and the host's check results. | The same lookup |
| `command` | Admin-fixed argv run in a worktree of the accepted commit, with `SYBERLABS_IDEMPOTENCY_KEY`, `SYBERLABS_COMMIT`, and `SYBERLABS_THREAD`. The last stdout line must be `{"external_id": ...}`. Declared `no_write_exit_codes` mean no write (409). | `status_argv` prints `{"state": "applied" \| "absent" \| "unknown", ...}` |

A transport failure is `unknown`, and `syberlabs recover` asks the destination. A remote that does not show the write is `absent`. That becomes `not_applied`, which frees the action for a fresh proposal, only after the action's `settle_seconds` (120 by default). Inside that window the effect stays pending, because a remote that has not shown a write yet is not proof it never will.

## Durability and recovery

Every installed document and event is one fsynced line in `.syberlabs/journal/` before memory changes. `Session(journal=Journal(path))` gives any SDK project the same store, and the same conformance run passes on the in-memory and durable sessions. On reopening:

- a torn final line from a crash is dropped;
- a malformed line, an event that breaks its hash chain, or a registry contract that no longer validates refuses to load (`journal_corrupt`);
- another process's appends are read before each operation (POSIX file lock).

An acceptance interrupted after its durable claim shows as an open item, and `syberlabs recover` settles it from the branch. A thread holds at most 20,000 events.

## Memory and forgetting

`syberlabs memory` reports what is kept. The thread record (the journal) is kept because it is what proves an accepted change. Context is kept as references only. Candidate refs are removed by `syberlabs prune` for finished threads. There is no project-knowledge store and no personal profile. Bounds: context 24 KB per selection by default, 256 KB per file read, 1 MB per submitted file and 4 MB per candidate, check output capped per contract (64 KB by default; over the cap is an `error`), a 4 KB output tail in the record, and contract budgets on candidates, evaluations, and seconds.

`syberlabs forget THREAD --reason "..." [--export FILE]` deletes a thread's history and its candidate refs, under a retention rule in `policy.json`:

```json
"retention": {"effect_history_days": 365, "unaccepted_history_days": 0}
```

| Thread | Forget |
| --- | --- |
| An effect has no known outcome | Refused (`retention_unresolved`). Run `syberlabs recover` first. |
| Its history proves an effect (accepted, pushed, pull request, published) | Refused until `effect_history_days` after the last effect (default 365; `retention_required` says from when). It then leaves `.syberlabs/journal/receipts/<thread>.json`: chain head hash, event count, and each effect's action, destination, external id, and time. The receipt keeps no objective, file content, or event bodies. |
| Anything else | Allowed after `unaccepted_history_days` (default 0) |

Every forget appends a tombstone (thread id, time, actor, reason, event count, head hash) to `forgotten.jsonl`. The receipt and tombstone are fsynced before the history is deleted. Forgetting never undoes an effect: the accepted branch, pushed refs, and pull requests stay. `--export` writes the full record first if you want a copy outside SyberLabs. A retention rule is policy, so a change is a new policy version.

## Measured

`benchmarks/build_thread_dx.py` records machine time on the pinned fixture in `benchmarks/results/build-thread-dev.txt`. That covers the quickstart steps, the repeat check, admission preview latency, reopen time, and peak Python memory at 20 and 100 candidates. It does not measure how long a person takes; the roadmap's first-use test with three unfamiliar developers has not been run.
