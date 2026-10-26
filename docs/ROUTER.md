# The lookup router (research, not the default)

```bash
syberlabs propose --repair "python adapter.py" --router         # the repair loop, the router between attempts
syberlabs propose --claude-code --bare --router                 # the same, with Claude Code as the call
syberlabs propose --repair "python adapter.py" --router \
    --router-max-lookups 2 --router-holdout 0.5 \
    --router-web "python web_search.py" --router-decider "python small_model.py"
```

The factory's default proposer path is the repair loop (`syberlabs propose --repair`, `providers.RepairProvider`;
owner decision of 2026-10-23, `docs/MODEL_COMPARISON.md`). It answers every failed attempt the same way: the next
model call gets the host's check results and the failed candidate's files. The router (`syberlabs/router.py`) puts
one decision between attempts, read from the host's own evaluation of the attempt that failed, and journals it
before it acts:

| Action | What the next call gets |
| --- | --- |
| `repair` | the failed check's output and the candidate's files, as in the repair loop |
| `lookup` | the same, plus what GitHits returned for the failing library at its installed version |
| `web` | the same, plus what a configured web search returned |
| `stop` | nothing: the search ends |

The first call of a search has nothing to route from and is recorded as `propose`. It is the feature SyberLabs
proposed to GitHits: the factory, not the model, decides when a lookup is worth making, so every lookup can be
attributed to the attempt it informed. That is what lets a third arm of the GitHits pilot show GitHits' value per
lookup, not only per item.

`--router` is opt-in and research. Without it nothing changes: `--repair` and `--claude-code` build the providers
they built before, no `route` fact is written, no request carries `evidence`, and the prompt of a request without
`evidence` is byte for byte what it was (`tests/test_router.py`, `DefaultUnchanged`).

## The baseline policy

Deterministic: one ordered list of named rules, and the first that holds decides, as admission is one ordered list
of rules. The first two are the router's own and no decider can override them.

| Rule | When | Action |
| --- | --- | --- |
| `stop.budget` | the search budget has no room for another call and its evaluation (candidates, evaluations, seconds, dollars on a priced contract) | stop |
| `stop.calls` | the search's model calls (`--max-calls`, else the candidate budget) are spent | stop |
| `stop.no_progress` | the same failure, with the same score, came back more than `patience` (2) times in a row | stop |
| `lookup.third_party` | the failure is a third-party library's and a GitHits lookup is open | lookup |
| `web.missing_dependency` | the failure names a module installed nowhere and a web search is open | web |
| `web.third_party` | the failure is a third-party library's, GitHits cannot answer it here, and a web search is open | web |
| `repair.repo` | the failure is in the repository's own code | repair |
| `repair.default` | anything else: the standard library, an unknown failure, no lookup left | repair |

A lookup is **open** when its route is ready (a GitHits token, or a web command), the failure is one it can answer
(for GitHits: an installed distribution to target), the search has lookups left (`--router-max-lookups`, default 2,
GitHits and web together), the same question was not asked on that route before in this search, and the query
guard passes the query (`query_guard.judge`: the same rules the hook applies to an agent's lookups, its tree rule
over the failed candidate's changed files and the first 500 in-scope files at the base). `repair` and `stop` are
always open.

### The diagnosis

`router.diagnose` reads the failed checks' output tails as the host recorded them (`candidate_evaluated`), required
checks first, then by name, and picks the first third-party failure, else the first with a Python exception, else
the first failing check:

1. The last Python exception line and the traceback frames printed before it (CPython's format; pytest's short
   format and the location lines of its long format). Frames of an earlier or chained traceback are not its own.
2. Each frame is the repository's when its path ends with an in-scope file, third-party when it lies under a
   `site-packages` or `dist-packages` directory, the standard library's when it lies under the interpreter's
   library, else unknown. Windows paths are read the same way.
3. An `ImportError` or `ModuleNotFoundError` names its module; an `AttributeError` on a module names it. An
   `AttributeError` on a class or an object, and a `TypeError` on a call, name a symbol: its module is the
   third-party package the innermost frame lies in, else the module the failing repository file binds that name to
   with an import (`router.import_bindings`, read with `ast`, never run), else the imported module the failing line
   calls it through (`fakelib.fetch(` with `import fakelib`).
4. That module is the repository's own (`repo`), the standard library's (`stdlib`), an installed distribution
   (`third_party`), installed nowhere (`missing_dependency`), or not known (`unknown`). A lookup-class error whose
   module cannot be traced is `unknown`, not the repository's.

The installed version comes from the `*.dist-info` beside the traceback's own `site-packages` frames, the
environment the check ran in (`traceback_site`); else from the host interpreter's metadata (`host_metadata`); else
it is unavailable. An unavailable version is never guessed and never replaced by a declared range (`CLAUDE.md`): the
failure is not a GitHits lookup's (there is nothing to pin), and when a lookup does run with a version from
`host_metadata`, the evidence the model reads says where the version came from.

Two attempts failed **the same way** when their signatures are equal: every failing check's name, state, exit code,
exception class and message, with addresses, paths and durations replaced, digested.

## A lookup

**GitHits** (`router.GitHitsRoute`, the host's own client `syberlabs/githits.py` used as a library and not changed):

- the query is a few words naming the package, its version and the symbol, with a phrase for the failure's class,
  as the GitHits skill asks: `fakelib 2.0.0 fetch_all import name`, `fakelib 2.0.0 fetch signature parameters`;
- the target is pinned: `pypi:<distribution>@<version>`;
- one `search`, then one `read` of the top hit's lines (its documentation page, or its file's span under the pinned
  target); a lookup decision is at most two GitHits calls;
- the client applies its exclusions (`SyberLabs` always, whatever `SYBERLABS_GITHITS_EXCLUDE_REPLACE` says), its
  license policy and its redirect, size and token rules, and signs a record of every call in a record directory
  and under a key that are fresh per decision; the router journals those records and then removes the directory;
- `X-GitHits-Agent` is `syberfactory/router` and `X-GitHits-Session-ID` is `<thread>-router`, so GitHits can tell
  the third arm's traffic from arm B's (`syberfactory/b`, `<thread>-b`);
- a failing call (a 429, a 503, a transport error) is recorded and the search goes on with a repair: a lookup never
  ends a search.

**Web** (`router.WebRoute`): none is built in; the host has no web client of its own. `--router-web COMMAND` is a
process seam: the command reads `{"protocol": "syberlabs.web/v0alpha1", "query": "...", "limit": 5}` on stdin and
prints `{"results": [{"title", "url", "snippet"}], "request_id"}`. Its queries are the router's own and pass the same
guard (`python package that provides module fakelib_two`, `python fakelib 2.0.0 fetch signature parameters`).

**What the next call reads.** A lookup that returned something adds one item to the request's optional
`evidence` list (`{route, decision, target, pin, text}`, the text at most 6,000 characters, rendered as the
GitHits client renders results for an agent). `adapters/anthropic_adapter.py: build_prompt`, the prompt both the
JSON adapter and Claude Code use, shows each item after the failing checks as untrusted reference material. An item
rides on every call while the failure it answered is the current one, and is dropped when the failure changes.

## A decider model

`--router-decider COMMAND` (`router.CommandDecider`) is the seam for a small decider model. It reads
`{"protocol": "syberlabs.route/v0alpha1", "situation": {...}}` on stdin and prints
`{"action": "repair|lookup|web|stop", "reason": "...", "usage": {...}}`. The situation is what the policy reads:
the diagnosis as names, counts and digests, plus the exception line (the one piece of output text a decider sees,
never journaled); the scores, repeats, calls and lookups left; the routes and the actions open; the lookups the
open routes would make (digests, not queries); the last eight decisions with their outcomes; and the baseline's
choice. Inside a search the host's meter permits and prices it like any model call (`providers.current_meter`), so
its spend lands in the next registration's, as a model call's does. Its choice stands only when the action is open;
otherwise, and when the command fails, the baseline's choice stands. Either way the route fact records the decider,
what it proposed, why it was refused or failed, and its declared usage. A Python `router.FunctionDecider` does the
same in process. `router.Decider` is the protocol.

The decider and web commands run in the caller's environment without the GitHits variables (`GITHITS_*`,
`SYBERLABS_GITHITS_*`, the token included: the router holds the GitHits route and they do not need it) and without
the certificate signing seed, as Claude Code's environment loses them in arm A. So does the model's own command
under the router (`--repair ADAPTER --router`; the plain repair loop's command still inherits the whole environment,
as before), and a routed Claude Code run has no GitHits route of its own: `--githits` and `--mcp-config` are refused
with `--router` (an MCP server could hand the model GitHits), so its environment loses the variables as in arm A.
Only the factory can look up. That is least privilege, not a sandbox: they inherit everything else, as a provider
command does.

## The holdout

`--router-holdout P` replaces a lookup (or a web search) the policy chose with a repair, with probability `P`, from
a stream seeded by the search's seed and separate from the request seeds (the run can be repeated). The decision
is recorded as `action: repair, rule: holdout` with `{p, chosen, rule, held_out}`. A held-out lookup reaches no one.
`P = 1` is the router with every lookup withheld: the control for the router's other behaviour.

## What is journaled

All through `SearchSpace.observe` (`GitSearchSpace.observe` writes verified host observations; its closed set of
provider keys gains `route`, which only the router writes), so the third arm's record is the same journal every
other number comes from:

| Fact | Version | Value |
| --- | --- | --- |
| `route`, one per decision, written before anything it decides runs | `<search>.r<n>` | `router` (policy name and revision), `search`, `decision`, `call` (model calls made before it), `after` (the candidate whose evaluation it read), `action`, `rule`, `reason`, `eligible`, `decider`, `holdout`, `diagnosis` (check, state, kind, error, module, distribution, version, version source, pin, how the module was found, frame counts, the symbol's and the message's digests, the signature), `score`, `best`, `repeats`, `lookup` (route, target, pin, query digest and length, guard), `blocked` (a route whose query the guard blocked, with its rule), `carried` (earlier decisions whose evidence the next call carries), `routes` (`githits`: `ready` or why not; `web`), `budget` left |
| `lookup`, one per GitHits call | `<search>.r<n>.<k>` | built from the client's own signed record exactly as the Claude Code launcher route builds it (`claude_code.record_lookup`): tool `githits.<op>`, query digest, request id, status, results returned and dropped, latency, `ok`, `guard` |
| `lookup`, one per web search | `<search>.r<n>.1` | tool `router.web`, query digest and length, status, results, request id, latency, `ok`, `guard` |

Never journaled: query text, result text, the evidence, the exception line, file content, the token. A decider's
`reason` is its own text, kept to 200 printable characters. The host's evaluation record keeps each check's output
tail, as it always has.

Because the GitHits facts have the launcher route's shape, `syberlabs metrics` counts the router's lookups in
`githits_lookups`, host-observed, with no change to `metrics.py`. The router's web searches are not in
`web_lookups`, which counts what a model's own tools declared.

## How the third arm will be scored

**The arms.** One work item list, one frozen setup (`docs/EXPERIMENTS.md`, the GitHits pilot): the same model,
effort, lane, contract, `--max-calls` and base commit in every arm, the arm alternated from item to item.

| Arm | Command | Who decides to look up |
| --- | --- | --- |
| `off` | `syberlabs propose --claude-code --bare` | nobody: no GitHits |
| `on` | `syberlabs propose --claude-code --bare --githits` | the model, through the launcher |
| `router` | `syberlabs propose --claude-code --bare --router --router-holdout 0.5` | the factory, between attempts |

**Per item**, as for the first two arms: `syberlabs experiment report --name githits-pilot --baseline off
--treatment router --server none`, and `--baseline on --treatment router`, with `--items` and `--delivered review`
as registered. The delivery rule, cost, turns, fix rounds and `githits_lookups` per item come out as they do for
`on`. `--server none` is needed today: the report's arm check (`experiment._servers`) reads the MCP servers each call
recorded in `signal.harness.mcp_servers`, and a Claude Code call that connected none records a harness without
them, so with `--server githits` every router item whose call is Claude Code is `arm_conflict` and excluded (a JSON
adapter records no harness, and its items are `arm_unverified`). The same reading applies to the launcher arm
(`--githits`), whose GitHits route is not an MCP server either. The contamination the server check would have caught
is read from the per-item values instead: the `off` arm's `githits_lookups` must be 0 on every item.

**Per lookup**, from the same journal, by `router.attribution` and `router.summary` (pure functions, like
`metrics.work_items`):

```python
from syberlabs import Kit, router

kit = Kit.local(".syberlabs", ".")
events = [e for case in kit.journal.thread_ids() for e in kit.session.history(case)]
rows = router.attribution(events, dict(kit.session.contracts))   # one row per decision
print(router.summary(rows))
```

Each row is one decision and the attempt that followed it: `after.outcome` (`passed`, `failed`, `unevaluated`,
`refused:<reason>`, `none`), `trigger_fixed` (the check the decision read passed), `same_failure` (the next
decision read the same signature), `gained` (required checks gained), `calls_to_pass` (model calls from this
decision to the search's first pass, its own included), and the cost: the call's host-observed dollars and tokens
(the registration's `spend` and `signal.model_usage`), the evaluation's milliseconds, and the lookup's GitHits calls,
results and latency.

The numbers that answer "what is one lookup worth", in the order to read them:

1. **The holdout comparison** (`summary(rows)["holdout"]`): among the decisions where the policy chose a lookup,
   the next-attempt pass rate of those that looked up against those the draw held out, and the difference. The draw
   is the only thing that separates the two groups, so this is the one per-lookup number free of the policy's own
   selection. Pre-register `P` (0.5 above) and the minimum count of drawn decisions before reading it; with a
   handful of decisions it is a description, not a finding.
2. **Calls and dollars per lookup**: `lookup.githits_calls_per_lookup`, the mean dollars of the attempt after a
   lookup, and `calls_to_pass` after a lookup against after a held-out one. GitHits' own price per call is not
   known to the host; the call count is what it will be multiplied by.
3. **Description only**: `by_action` (the pass rate after `lookup` against after `repair`) compares lookups with
   repairs of other failures, which the policy chose because they were different; read it as context, never as the
   lookup's effect.

**Before reading any of it**, check the record: every router thread's `routes.githits` is `ready` (not `no_token`);
the `off` arm made no GitHits lookup; the holdout's two groups are not wildly unequal in size; how many lookups ran
with a version from `traceback_site`, from `host_metadata`, or unpinned; and how many lookups the guard blocked
(`blocked`). Report all of them beside the numbers.

## Limits, stated plainly

- **Not measured.** Every test runs against a stand-in model, a stand-in `claude` and a loopback fake GitHits that
  follows the pinned schemas. Nothing here says whether a lookup helps a real model; that is the third arm's to
  measure, and it is Class X (a real model, a paid key, the real GitHits API): a named person runs it.
- **The diagnosis reads Python.** A failure in another language, a timeout or a tool error is `unknown` and gets a
  repair. pytest's long format is read for its exception and location lines, not its source lines. A symbol whose
  module the failing file does not import by name (an object returned by a library call) is `unknown`.
- **The version is the environment's, when the record shows it.** A traceback that prints no `site-packages` frame
  leaves the host interpreter's metadata, which is right when the checks run on the host's interpreter (the default
  contract names it) and can be wrong when they run another.
- **One search and one read per lookup.** No `grep`, `list` or `example`, no retry on a 429, no polling of an
  incomplete search: the GitHits client's own deferred items (`spec/GAPS.md`, 59) apply. So does its target limit:
  a distribution whose name does not name an excluded owner reaches GitHits (a module of the repository's own
  in-scope files never does: it is `repo`), and only the client's result filter can then drop what it serves from an
  excluded owner.
- **Results are untrusted text in the prompt.** The prompt says so and the host's checks and a person still decide,
  but a result can try to instruct the model, as it can in arm B, where the model reads results itself. Search and
  read results carry no license data in the GitHits contract 0.1.0, so the license policy cannot judge them.
- **The holdout estimates the effect of a lookup where the policy would make one**, not of a lookup anywhere.
- **The experiment report needs `--server none` for this arm** (above). The change that would let it check the
  router arm itself, reading the router's `lookup` facts as the arm's GitHits route, belongs to `syberlabs/experiment.py`
  and is described in the pull request, not made.

Claim ceiling: **implemented and tested**, against stand-ins only. Nothing is measured, deployed or externally
validated.
