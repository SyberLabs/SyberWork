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
syberlabs findings c1                      # what the contract's critic found, if it names one
syberlabs diff c1
syberlabs accept c1                        # moves refs/heads/syberlabs/<thread> only
syberlabs status
```

`init` guesses the test command (`tests/test*.py`, `package.json`, `Cargo.toml`, `go.mod`). A Python check names the interpreter running SyberLabs by its absolute path, never a bare `python` or `python3`, and `npm`, `cargo` and `go` are resolved the same way, at `init`, to the absolute path `shutil.which` finds (`npm.cmd` on Windows): checks run as argv without a shell, so a bare name is resolved against a PATH the caller did not choose, or not at all. A `package.json` gets two checks, `install` (`npm ci` with a lockfile, `npm install` without) and `tests`, because every candidate is checked in a fresh worktree that has no `node_modules`. A tool that is not on PATH gets no check and a one-line note on stderr. When it cannot guess, or the interpreter reports no path, it writes a `configure` check that fails until you name a real one. An unchecked change never looks accepted. Running a thread on a Node, Rust or Go repository, with a worked example, is in [CONSUMERS.md](CONSUMERS.md).

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

Checks run in canonical name order, not declaration order: a contract is stored as canonical JSON, and the runner sorts the names again, so `install` runs before `tests` and `tests` before `typecheck` whatever the file says. A check named `setup` or `install` prepares the worktree (`npm ci`, `cargo fetch`, a code generator). When it does not pass, the checks after it are not started and are recorded `error` with the tail `setup_failed`, so the verdict names the setup as the failure instead of one failure per check. The frozen schema has no `setup` field, which is why the step is a check by name (`spec/GAPS.md`, 32). The checks' environment is scrubbed, with a temporary `HOME`, `TMP` and `TEMP`; it keeps `PATH`, the locale, the Windows system variables, and the places package managers cache downloads (`APPDATA`, `LOCALAPPDATA` and `USERPROFILE` on Windows, `XDG_CACHE_HOME` on POSIX, and `npm_config_cache`, `CARGO_HOME`, `RUSTUP_HOME`, `GOPATH`, `GOCACHE`, `GOMODCACHE` when set), so a candidate does not reinstall the world. A critic check (`critic` or `critic.<reviewer>`) additionally receives the variables the operator names in `SYBERLABS_CHECK_ENV` in the host's own shell, comma-separated, each only when it is set: `SYBERLABS_CHECK_ENV=ANTHROPIC_API_KEY,SYBERLABS_CRITIC_MODEL,SYBERLABS_CRITIC_EFFORT` lets a model-backed critic authenticate. That list is host policy and never contract data: a `tests` check never sees it, the contract cannot name a variable, a candidate cannot change the list because it lives in the operator's environment and not in the repository or the journal, and a listed name cannot replace a scrubbed value (`HOME`, `TMP`, `SYBERLABS_CANDIDATE`). Nothing about the pass-through enters the evaluation record: no name, no value, no count. A check that outruns its `timeout_seconds` is `timed_out`, and everything it started goes with it: the process group on POSIX, the process tree (`taskkill /T`) on Windows.

A check named `critic` or `critic.<reviewer>` is a critic: an independent reviewer the host runs like any other check, with a JSON request on stdin and a findings document on stdout. The host parses the document, refuses a reviewer that is the candidate's own provider, records the findings as a verified `observed` event right after the evaluation, and the `critic.no_high_finding` rule refuses acceptance while a high finding stands; `syberlabs findings c1` prints them. See [CRITIC.md](CRITIC.md).

## Providers

A provider gets a `SearchSpace` and nothing else. It can list and read files in the read scope, merge text, submit edits, ask the host to evaluate a candidate, and see its remaining budget. It cannot reach the session, approvals, the target branch, or `accept`.

- `PatchProvider(changes)` submits one edit. `syberlabs propose --from-worktree` uses it with the working-tree edits inside both the attached sources and the contract scope, and lists what it left out.
- `CommandProvider(argv)` is the model seam. It writes a JSON request to the command's stdin (objective, base, scope, budget, context excerpts, file list) and reads `{"candidates": [{"changes": {...}, "message": ..., "signal": ...}], "recommended": [...]}` from its stdout. The adapter for Kev, Jev, or any other model is that command. SyberLabs imports no model SDK and makes no model call itself.
- `FunctionProvider(fn)` wraps a Python callable.
- `EvolutionaryProvider(mutator)` is an EvoGit-style search. It keeps a population of candidates, mutates them, merges non-ancestors, and selects on the host's check results. It needs a contract whose `operators` include `mutation` and `crossover`. See [EVOLUTION.md](EVOLUTION.md).

An in-process provider runs as Python code in your process. The interface keeps model *output* away from authority; it is not a sandbox for untrusted provider *code*. Run untrusted providers as a `CommandProvider`.

Not every proposal becomes a candidate. The host refuses one whose edit leaves its parent's tree unchanged, one identical to a candidate it already holds, one it cannot read, one the search's budget has no room for, and one that declares a model and zero tokens; a provider discards an empty or unparsable answer itself. Each such proposal leaves one record in the thread's journal, a verified `observed` event with key `proposal_refused`, versioned by the search, naming the search, the operator, the parents, a reason from a closed set (`no_change`, `duplicate`, `unparsable`, `limit`, `zero_usage`, `provider_error`, `out_of_scope`), the digest and size of its diff when it parsed, the usage it declared, and the host's spend observation under a priced contract; never file content or prompt text. So a search that made six model calls and registered nothing is readable from the journal alone: six refusals say what became of each call. `search_finished` itself is unchanged; it is a frozen record. The shape and the reasons are in [EVOLUTION.md](EVOLUTION.md), "Records".

## Spend and the dollar budget

A contract may price model usage. `evolution.budget.prices` maps a model name to dollars per million tokens (`input_per_million`, `output_per_million`; optional `cache_read_per_million` and `cache_creation_per_million`, which default to the input rate), `max_dollars` bounds the thread, and `strict: true` makes usage of an unpriced model a refusal rather than a note. Without `prices` nothing below exists: no record gains a field, no rule decides, and `status` prints no spend. The schemas are `v0alpha1` and do not list these fields yet (`spec/GAPS.md`, 31).

```json
"budget": {"max_candidates": 12, "max_evaluations": 24, "max_seconds": 1800, "max_dollars": 5,
           "prices": {"claude-opus-5": {"input_per_million": 15, "output_per_million": 75}}}
```

The host measures; the provider only reports. The one place a model call crosses the host is `providers.run_json`, the subprocess boundary `CommandProvider` and `CommandMutator` use. Inside a search, every `run_json` call asks the host's ledger for permission before the command starts and hands it the adapter's reported `usage` afterwards, stamped `host_observed`. That usage, priced under the contract's table, is the thread's spend. A `model_usage` a provider writes into a candidate's `signal`, or any usage reported by code that did not go through `run_json`, is the provider's declaration: it stays in the signal, is shown as "provider says (unverified)", and is never priced. Wall time is measured with the monotonic clock.

The spend is in the record. Under a priced contract each `candidate_registered` carries `spend: {dollars, seconds, host_observed}`: the dollars of the host-observed calls made since the previous registration in that search (`null` when one of them named a model the table does not price), the seconds since the search started, and whether any usage was observed at all. Each `search_finished` carries `spend: {dollars, seconds, evaluations, candidates, host_observed, unknown_models}` for the whole search. `syberlabs status` sums the finished searches: `spend     $0.0405 / $5.0000, 12.3s over 2 searches (max 1800s each)`, and `export` includes the same object.

The budget stops a search by itself. Once the ledger, pending usage included, is over `max_dollars`, no further command is started (`budget_exhausted`, before the purchase); the call that crossed the line still gets its candidate registered, with that spend on it, and the next registration or evaluation is refused. `space.remaining().max_dollars` tells a provider what is left (`None` without a dollar bound), so the evolutionary provider stops cleanly with `Stop("budget")` exactly as it does for candidates and evaluations; a provider that returns normally while over budget is recorded as `stopped: "budget_exhausted"` all the same, the existing word for a host-enforced stop. Seconds keep their own stop, `deadline`.

Two admission rules read these records, after `critic.no_high_finding` and before `approval.required`, and decide nothing under an unpriced contract. `budget.search` refuses a candidate whose own registration was over budget (`search_over_budget:dollars`, `search_over_budget:seconds`) or that a priced contract's host registered without measuring (`spend_unrecorded:<id>`). `budget.promotion` sums the thread's finished searches and refuses while the thread is over `max_dollars` (`over_budget:dollars`), so a thread cannot accept a candidate it could not afford to produce; with `strict`, an unpriced model refuses too (`unknown_spend:<model>`). The dollar bound is per thread. A per-actor or per-day budget needs a store outside one thread and is not built.

## Contracts, policy, and people

`.syberlabs/contracts/<id>.v<N>.json` files are installed when the kit opens. A published version is immutable. Editing `repo-change.v1.json` after its first use is refused with the file name to use instead, and `syberlabs contract-diff repo-change.v1 repo-change.v2` shows what changed. A thread stays on the version it started with. `.syberlabs/policy.json` is the global policy; a contract cannot widen it.

For a team, set the same `approval_role` on the promotion action and on `evolution.promotion`. Then `accept` returns `needs_approval`, a different person with that role runs `syberlabs approve c1 --actor lead --role maintainer`, and the proposer runs `accept` again. Proposers holding `model`, `compiled`, or `search` are refused whatever origin they claim.

To land an accepted change on `main`, merge `syberlabs/<thread>` yourself. Setting `promotion.target_ref` to an existing branch makes acceptance a fast-forward from the thread base instead: the branch must still be at the base (otherwise 412, no write), and it must not be checked out (otherwise 409, no write).

## Inspector

`syberlabs inspect` serves a read-only page on 127.0.0.1 and prints its address. It shows threads, the candidate lineage graph (crossover edges dashed, each node colored by its own check result, the accepted candidate ringed), each candidate's verdict and diff, and the event history. It re-reads the journal on every request. A thread whose start was interrupted (`state: incomplete`) is listed with that state and its remedy (start again with the same objective, or forget) and is not opened, so the listing still renders; asking for that thread alone answers 409 `thread_incomplete`. It cannot change anything: only GET is served, and its verdicts never run checks. The address carries a per-run token in the URL fragment, which the page sends as a header and removes from the address bar. The API refuses requests without the token or with a non-loopback `Host`, which blocks DNS-rebinding pages. The page has a strict Content-Security-Policy and renders every value as text.

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

A pull request body states who did what. It carries the thread's act class as `Class`, the accepted candidate's provider as `Agent-platform` (`name@revision`), the actor whose proposal promoted the candidate as `Admitted-by`, and, when the contract requires an `approval_role`, every independent approver as `Approved-by`. The actor and the approvers are read from the proposal that promoted the candidate. Two more fields come from the record, never from a hand-typed template. `Reviewed-by` names the approvers of that proposal whose actor is not the admitting actor, in the same `actor (role)` form; the kit refuses an approval by the proposer, so this is every recorded approver, and, when the contract names a critic check, the critic of the promoting evaluation as `name@revision (critic check <name>: high H, medium M, low L)`, read from the host's observation ([CRITIC.md](CRITIC.md)); both can appear on the line, and when there is neither it reads `Reviewed-by: none recorded` rather than being left blank or inferred. `Evidence` is the host's own claim about the accepted tree: `tested` when the latest host evaluation of the candidate's exact commit and tree passed every check the contract lists as `required` (which `candidate.promotable` demanded for the promotion), `implemented` otherwise. The kit cannot know that a change was measured, deployed, or externally validated, so it never writes those words; a person who can show it adds that in review. Every line of the body starts with a fixed label, the objective included (`Objective: ...`), and each value is written on one line, so text in an objective or a provider revision cannot add or impersonate a field. Agents commit under human GitHub identities, so these fields are what lets a reader tell a judgment from a generation. The class is recorded and stated, not enforced: the kit does not refuse a C, P or X candidate, and its roles are the caller's own claims, so the human key for those classes is whoever reviews and merges the pull request.

The class is recorded at case creation, inside the hash chain, not written by hand at publish time. The default contract declares `"act_class": "string"` among its inputs and records R unless `syberlabs start --class C` (or P, X) says otherwise. A contract written before that input existed refuses `--class` with `act_class_undeclared` instead of dropping it, and its pull requests state no class; publish a new version with the input to fix that. Name the provider with `syberlabs propose --name claude --revision <model>`; the default is `patch` for `--from-worktree` and `command` for `--command` and `--evolve`.

| Effect | Idempotency and no-write | Status lookup |
| --- | --- | --- |
| `git_push` | `--force-with-lease`: the branch must be absent, or at the thread base for a branch without `{thread}`. A refused ref is a guaranteed no-write (412). | `git ls-remote` |
| `github_pull_request` | Looks for a pull request from the branch at that commit before creating one. 401, 403, 404, and 422 happen before creation (409, no write). The body cites the thread, the candidate, and the host's check results. | The same lookup |
| `command` | Admin-fixed argv run in a worktree of the accepted commit, with `SYBERLABS_IDEMPOTENCY_KEY`, `SYBERLABS_COMMIT`, and `SYBERLABS_THREAD`. The last stdout line must be `{"external_id": ...}`. Declared `no_write_exit_codes` mean no write (409). | `status_argv` prints `{"state": "applied" \| "absent" \| "unknown", ...}` |
| `intervention` | Reads a metric as the baseline and leaves the claim open on purpose (`effect_unknown`, `window_open`). Refusals before the baseline are no-writes. | Re-reads the metric after `window_seconds`: `applied` when it moved as predeclared, `not_applied` when it did not. See [OUTCOMES.md](OUTCOMES.md). |

A transport failure is `unknown`, and `syberlabs recover` asks the destination. A remote that does not show the write is `absent`. That becomes `not_applied`, which frees the action for a fresh proposal, only after the action's `settle_seconds` (120 by default). Inside that window the effect stays pending, because a remote that has not shown a write yet is not proof it never will.

## Durability and recovery

Every installed document and event is one fsynced line in `.syberlabs/journal/` before memory changes. `Session(journal=Journal(path))` gives any SDK project the same store, and the same conformance run passes on the in-memory and durable sessions. On reopening:

- a torn final line from a crash is dropped;
- a malformed line, an event that breaks its hash chain, or a registry contract that no longer validates refuses to load (`journal_corrupt`);
- another process's appends are read before each operation (a cross-process file lock: `flock` on POSIX, `msvcrt.locking` on Windows).

An acceptance interrupted after its durable claim shows as an open item, and `syberlabs recover` settles it from the branch. A thread holds at most 20,000 events.

`start` is several appends: `case_created`, then the verified `base` observation, then the attached `sources`. A process that dies after the first leaves a thread the journal lists but no later step can use, because every step reads the base. Such a thread is recognisable rather than broken: `syberlabs threads` marks it `[incomplete]`, any attempt to open it (`use`, `status`, `export`, `prune THREAD`) is refused with `thread_incomplete` and names the two ways out, and `syberlabs start` with the same objective, contract and class completes it under the same thread id by appending the missing records (its base is HEAD at completion time; the chain stays valid). A `start` with different inputs creates a new thread as usual and leaves the interrupted one listed; `syberlabs forget THREAD --reason ...` removes it. Only the most recent thread is completed this way, and only while it has no base. A death after the base and before `sources` leaves a thread that opens normally with the contract's default scope; `syberlabs context` or a new `start` is the remedy there.

## Running unattended

`syberlabs run` drives every thread the journal lists through its next safe step, with no person at the terminal. One pass, oldest thread first: an interrupted start is skipped with a note; `recover` settles any interrupted acceptance or publishing effect exactly as `syberlabs recover` would; then at most one step per thread, chosen from the record alone:

| The record says | The runner does |
| --- | --- |
| The newest candidate has no evaluation, or its evidence is stale, or a passed evaluation would be within 60 seconds of the contract's `max_age_seconds` | `check` (a re-run, so the kernel never has to refuse `candidate_evidence_stale` on an acceptance the runner could see coming) |
| The newest candidate is acceptable and the thread was started `--unattended` | `accept` |
| The newest candidate is acceptable on a thread started without `--unattended` | nothing; the line says `syberlabs accept cN` is a person's step |
| A candidate is accepted and `--publish` was passed | the first publishing action the contract lists whose prerequisite effect is recorded (push before pull request) |
| A failed check, an out-of-scope candidate, a thread waiting for approval, an exhausted budget, no candidate | nothing; the line says why |

The runner never proposes: generation is another station's job, and a `run` loop over a thread with no candidate stays idle until someone or something proposes. `--unattended` is a verified host observation on the thread (key `unattended`), not a contract input, because the contract's inputs are the objective and the act class and a new input would be a new contract version; `syberlabs status` shows `mode      unattended` when it is set.

Every step is a kernel operation whose idempotence comes from the journal: a pending proposal is reused, a claimed effect is reconciled rather than repeated, a candidate already evaluated on this tree is not evaluated again. So a runner killed anywhere (a host restart, a `KeyboardInterrupt` between steps, a refusal mid-pass) leaves a valid chain, and the next pass picks up exactly where it stopped; `tests/test_runner.py` kills it at every step boundary and checks that no step repeats. One line per step: `825cb563 check c1 -> acceptable`, `825cb563 accept c1 -> succeeded`, `825cb563 idle c1 -> nothing to do (approval_required)`, then `pass: N steps, M refused`.

```
syberlabs run --once                 # one pass; exit 0 when nothing was refused, 1 when a step was refused, 2 on an unexpected error
syberlabs run --interval 60          # a loop, one pass a minute, until Ctrl-C
syberlabs run --thread 825cb5 --publish
```

No daemon, no threads, no new dependency: a loop with `time.sleep`, a clean stop on `KeyboardInterrupt`, Windows included. A refused publishing action (a push the remote rejects, a token that is not in the environment) is retried on the next pass, as the kernel allows, and reported each time; a failed check is not retried, because the candidate's tree cannot change.

## Memory and forgetting

`syberlabs memory` reports what is kept. The thread record (the journal) is kept because it is what proves an accepted change. Context is kept as references only. Candidate refs are removed by `syberlabs prune` for finished threads. There is no project-knowledge store and no personal profile. Bounds: context 24 KB per selection by default, 256 KB per file read, 1 MB per submitted file and 4 MB per candidate, check output capped per contract (`evaluation.max_output_bytes`, 1 MB by default since real test runners print more than the earlier 64 KB; over the cap is an `error`), a 4 KB output tail in the record, and contract budgets on candidates, evaluations, seconds and, when the contract prices usage, dollars.

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
