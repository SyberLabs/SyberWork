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
| `web` | the same, plus what a configured web search returned for the same question |
| `stop` | nothing: the search ends |

The first call of a search has nothing to route from and is recorded as `propose`. It is the feature SyberLabs
proposed to GitHits: the factory, not the model, decides when a lookup is worth making, so every lookup can be
attributed to the attempt it informed. That is what lets a third arm of the GitHits pilot show GitHits' value per
lookup, not only per item.

`--router` is opt-in and research. Without it nothing changes: `--repair` and `--claude-code` build the providers
they built before, no `route` fact is written, no request carries `evidence`, the plain repair loop's command still
inherits the whole environment, and the prompt of a request without `evidence` is byte for byte what it was
(`tests/test_router.py`, `DefaultUnchanged`).

## The baseline policy

Deterministic: one ordered list of named rules, and the first that holds decides, as admission is one ordered list
of rules. The first two are the router's own and no decider can override them.

| Rule | When | Action |
| --- | --- | --- |
| `stop.budget` | the search budget has no room for another call and its evaluation (candidates, evaluations, seconds, dollars on a priced contract); read before every decision and again after a lookup | stop |
| `stop.calls` | the search's model calls (`--max-calls`, else the candidate budget) are spent | stop |
| `stop.no_progress` | the same failure, with the same score, came back `patience` (2) times in a row: the third identical failed attempt ends the search | stop |
| `lookup.third_party` | the failure is a published third-party library's and a GitHits lookup is open | lookup |
| `web.third_party` | the same, GitHits cannot answer it here (no route, already asked, or the guard blocked it) and a web search is open | web |
| `repair.repo` | the failure is in the repository's own code | repair |
| `repair.default` | anything else: the standard library, a local install, a module installed nowhere, an unknown failure, no lookup left | repair |

A lookup is **open** when all of these hold:

- its route is ready (a GitHits token, or a web command);
- the failure is a published third-party distribution's (`third_party`, with a version) and the diagnosis kept a name
  to ask about (see "What leaves the machine");
- the search has lookups left (`--router-max-lookups`, default 2, GitHits and web together);
- the same question was not asked on that route before in this search, and was not held out (see "The holdout");
- the query guard passes the query (`query_guard.judge`: the same rules the hook applies to an agent's lookups, with
  its tree rule over the failed candidate's changed files and the first 500 in-scope files at the base).

`repair` and `stop` are always open. A module installed nowhere is never searched for: its name may be the
repository's own (a misspelled or out-of-scope package), and no search can install it, so the model gets the error
and repairs.

### The diagnosis

`router.diagnose` reads the failed checks' output tails as the host recorded them (`candidate_evaluated`), required
checks first, then by name, and picks the first third-party failure, else the first with a Python exception, else
the first failing check:

1. The last Python exception line and the traceback frames printed before it (CPython's format; pytest's short
   format and the location lines of its long format, Windows drive-letter paths included). Frames of an earlier or
   chained traceback are not its own.
2. Each frame is the repository's when its path ends with an in-scope file, third-party when it lies under a
   `site-packages` or `dist-packages` directory, the standard library's when it lies under the interpreter's
   library, else unknown.
3. An `ImportError` or `ModuleNotFoundError` names its module; an `AttributeError` on a module names it. An
   `AttributeError` on a class or an object, and a `TypeError` on a call, name a symbol: its module is the
   third-party package the innermost frame lies in (the library raised it), else the module the failing repository
   file imports that name from (`router.import_origins`, read with `ast`, never run; the library's own name is kept,
   never the repository's alias for it), else the imported module the failing line calls it through
   (`fakelib.fetch(` with `import fakelib`).
4. That module is the repository's own (`repo`), the standard library's (`stdlib`), a published installed
   distribution (`third_party`), a distribution installed from a local path or a URL (`local`: PEP 610's
   `direct_url.json`, an editable install included), installed nowhere (`missing_dependency`), or not known
   (`unknown`). A name the repository's own Python files make importable is the repository's, unless the output
   places the module's file under a `site-packages` directory, which wins. A lookup-class error whose module cannot
   be traced is `unknown`, not the repository's.

The distribution is the one whose `RECORD` holds the module's files: a regular package or module is its
distribution's alone; a namespace package shared by several (`google`) is resolved by the deepest path the failure
names (`google.cloud.storage`), and when none or two hold it, it is not resolved. The installed version comes from
the `*.dist-info` beside the traceback's own `site-packages` frames, the environment the check ran in
(`traceback_site`); else from the host interpreter's metadata (`host_metadata`); else it is unavailable. An
unavailable version is never guessed and never replaced by a declared range (`CLAUDE.md`): nothing is looked up
(there is nothing to pin), and when a lookup does run with a version from `host_metadata`, the evidence the model
reads says where the version came from.

Two attempts failed **the same way** when their signatures are equal: every failing check's name, state, exit code,
exception class and message, with addresses, paths and durations replaced, digested.

### What leaves the machine

GitHits indexes public code; the repository's code is SyberLabs' and never goes there. A query is four things and
nothing else: the published distribution's name, its installed version, the library's own name for the failing
symbol, and a phrase for the failure's class (`fakelib 2.0.0 fetch_all import name`). `router.vouch` decides which
names of the symbol may be sent:

- a name the repository's readable Python files define (a function, class or method, or a name or attribute they
  assign; the first 500 in-scope files at the base and the failed candidate's changed files) is dropped, and so is a
  module of the repository's own;
- when the library raised the failure, the object or callback the message names may be the caller's
  (`compute_acme_customer_ssn_hash() takes 0 positional arguments`, `'AcmeInternalLedgerEntry' object has no
  attribute 'to_dict'`), so a name is kept only if the distribution's own files show it (`to_dict` is; the
  callback and the class are not);
- a name the failing code imports from the library (`from fakelib import fetch_all`) is the library's by the code's
  own account;
- with no name left, nothing is looked up.

A `local` distribution is never named: it may be private, and no PyPI pin describes it. Then the query guard judges
the query. No output text, path or file content is ever part of one. What remains possible: a model that writes an
import of a made-up name from a library makes the router send that name, an identifier, at most `--router-max-lookups`
times per search (arm B's model can send any query it likes, so this is narrower than arm B, not closed); and a
distribution installed from a private index looks published.

## A lookup

**GitHits** (`router.GitHitsRoute`, the host's own client `syberlabs/githits.py` used as a library and not changed):

- the target is pinned: `pypi:<distribution>@<version>`;
- one `search`, then one `read` of the top hit's lines (its documentation page, or its file's span under the pinned
  target); a lookup decision is at most two GitHits calls, each given at most a quarter of the seconds left;
- the client applies its exclusions (`SyberLabs` always, whatever `SYBERLABS_GITHITS_EXCLUDE_REPLACE` says), its
  license policy and its redirect, size and token rules, and signs a record of every call in a record directory
  and under a key that are fresh per decision; the router journals those records whatever happened after a request
  left (a refusal, an error, an answer it could not render) and then removes the directory;
- `X-GitHits-Agent` is `syberfactory/router` and `X-GitHits-Session-ID` is `<thread>-router`, so GitHits can tell
  the third arm's traffic from arm B's (`syberfactory/b`, `<thread>-b`);
- a failing call (a 429, a 503, a transport error, a malformed answer) is recorded and the search goes on: a lookup
  never ends a search. After it the budget is read again, and with no room left the next decision is `stop.budget`,
  never a model call past the budget.

**Web** (`router.WebRoute`): none is built in; the host has no web client. `--router-web COMMAND` is a process seam:
the command reads `{"protocol": "syberlabs.web/v0alpha1", "query": "...", "limit": 5}` on stdin and prints
`{"results": [{"title", "url", "snippet"}], "request_id"}`. It is asked the GitHits question, the same words after
`python` (`python fakelib 2.0.0 fetch_all import name`), when GitHits cannot answer it here.

**What the next call reads.** A lookup that returned something adds one item to the request's optional
`evidence` list (`{route, decision, target, pin, text}`, the text at most 6,000 characters, rendered as the
GitHits client renders results for an agent). `adapters/anthropic_adapter.py: build_prompt`, the prompt both the
JSON adapter and Claude Code use, shows each item after the failing checks as untrusted reference material; a
`<reference` or `</reference` inside the text is broken with a backslash, so a result cannot close its block. An item
rides on every call while the failure it answered is the current one, and is dropped when the failure changes.

## A decider model

`--router-decider COMMAND` (`router.CommandDecider`) is the seam for a small decider model. It reads
`{"protocol": "syberlabs.route/v0alpha1", "situation": {...}}` on stdin and prints
`{"action": "repair|lookup|web|stop", "reason": "...", "usage": {...}}`. The situation is what the policy reads:
the diagnosis as names, counts and digests, plus the exception line (the one piece of output text a decider sees,
never journaled); the scores, repeats, calls and lookups left; the routes, the actions open and the routes closed
with why; the lookups the open routes would make (digests, not queries); the last eight decisions with their
outcomes; and the baseline's choice. Inside a search the host's meter permits and prices it like any model call
(`providers.current_meter`), so its spend is booked at the next registration, as a refused call's is. Its choice
stands only when the action is open; otherwise, and when the command or a custom policy fails in any way, the
baseline's choice stands (a budget or deadline stop from the meter still ends the search, as at a model call).
Either way the route fact records the decider, what it proposed, why it was refused or failed, and its declared
usage. A Python `router.FunctionDecider` does the same in process. `router.Decider` is the protocol.

Only the factory looks up. The decider and web commands, and under the router the model's own command (the JSON
adapter, through `providers.run_json`'s optional `env`), run in the caller's environment without the GitHits
variables (`GITHITS_*`, `SYBERLABS_GITHITS_*`, the token included) and without the certificate signing seed; a routed
Claude Code run has no GitHits route of its own (`--router` refuses `--githits` and `--mcp-config`, since an MCP
server could hand the model GitHits), so its environment loses them as in arm A. That is least privilege, not a
sandbox: they inherit everything else, as a provider command does.

## The holdout

`--router-holdout P` withholds lookups at random, with probability `P`, so their value can be measured against their
absence:

- **once per question.** The draw is made at the first decision that would ask a question (the failure's
  distribution, version, error and symbol, `Diagnosis.need()`). A held-out question stays unasked for the rest of the
  search on every route: GitHits and the web are closed for it (`blocked: {"lookup": "holdout", "web": "holdout"}`).
  A question that drew "look up" is asked, and a later web search for it draws nothing new.
- **its own stream per work item.** The stream is seeded by the thread, the search and the seed, separately from the
  request seeds, so every item draws its own sequence although `--seed` is 0 for all of them by default, and the
  same thread, search and seed draw the same again.
- **recorded.** A held-out decision is `action: repair, rule: holdout`, with `{p, u, chosen, rule, held_out}` (`u` the
  uniform number drawn). A held-out lookup reaches no one.

`P = 1` is the router with every lookup withheld: the control for the router's other behaviour.

## What is journaled

All through `SearchSpace.observe` (`GitSearchSpace.observe` writes verified host observations; its closed set of
provider keys gains `route`, which only the router writes), so the third arm's record is the same journal every
other number comes from:

| Fact | Version | Value |
| --- | --- | --- |
| `route`, one per decision, written before anything it decides runs | `<search>.r<n>` | `router` (policy name and revision), `search`, `decision`, `call` (model calls made before it), `after` (the candidate whose evaluation it read), `last_outcome` (what became of the attempt after the previous decision: `failed`, `out_of_scope`, `unevaluated`, `refused:<reason>`, `none`), `action`, `rule`, `reason`, `eligible`, `blocked` (routes closed for this failure, with the guard's rule or `holdout`), `decider`, `holdout`, `diagnosis` (check, state, kind, error, module, distribution, version, version source, pin, how the module was found, frame counts, the kept symbol's and the message's digests, how the symbol was vouched for, the signature), `score`, `best`, `repeats`, `lookup` (route, target, pin, query digest and length, guard), `carried` (earlier decisions whose evidence the next call carries), `routes` (`githits`: `ready` or why not; `web`), `budget` left |
| `lookup`, one per GitHits call that left | `<search>.r<n>.<k>` | built from the client's own signed record exactly as the Claude Code launcher route builds it (`claude_code.record_lookup`): tool `githits.<op>`, query digest, request id, status, results returned and dropped, latency, `ok`, `guard` |
| `lookup`, one per web search | `<search>.r<n>.1` | tool `router.web`, query digest and length, status, results, request id, latency, `ok`, `guard` |

Never journaled: query text, result text, the evidence, the exception line, file content, the token. A decider's
`reason` is its own text, kept to 200 printable characters. The host's evaluation record keeps each check's output
tail, as it always has.

Because the GitHits facts have the launcher route's shape, `syberlabs metrics` counts the router's lookups in
`githits_lookups`, host-observed, with no change to `metrics.py`. Two numbers it does not see: the router's web
searches are not in `web_lookups` (which counts what a model's own tools declared), and a query the guard blocked is
never sent, so it is in the route fact's `blocked`, not in `lookups_blocked`.

## How the third arm will be scored

**The arms.** One work item list, one frozen setup (`docs/EXPERIMENTS.md`, the GitHits pilot): the same model,
effort, lane, contract, `--max-calls` and base commit in every arm, the arm alternated from item to item.

| Arm | Command | Who decides to look up |
| --- | --- | --- |
| `off` | `syberlabs propose --claude-code --bare` | nobody: no GitHits |
| `on` | `syberlabs propose --claude-code --bare --githits` | the model, through the launcher |
| `router` | `syberlabs propose --claude-code --bare --router --router-holdout 0.5` | the factory, between attempts; half of its questions held out |
| `router-control` (optional) | `syberlabs propose --claude-code --bare --router --router-holdout 1` | nobody: the router with every lookup withheld |

`router` differs from `off` in more than lookups: its `stop.no_progress` rule can end a search before `--max-calls`,
and it withholds half its questions. `router` against `router-control` holds the router's other behaviour equal, so
the item-level difference is the lookups' (at the rate the holdout lets them run); the per-lookup comparison below
needs no extra arm.

**Per item**, as for the first two arms: `syberlabs experiment report --name githits-pilot --baseline off
--treatment router --server none`, and `--baseline on --treatment router`, with `--items` and `--delivered review`
as registered. The delivery rule, cost, turns, fix rounds and `githits_lookups` per item come out as they do for
`on`. `--server none` is needed today: the report's arm check reads the MCP servers each call recorded in
`signal.harness.mcp_servers` (and, with the launcher route's open change, the launcher's per-call ledger
`signal.githits`), and a router call records neither, since the model has no GitHits route, so with
`--server githits` every router item is `arm_conflict` and excluded (a JSON adapter records no harness, and its items
are `arm_unverified`). The contamination the server check would have caught is read from the per-item values
instead: the `off` arm's `githits_lookups` must be 0 on every item.

**Per lookup**, from the same journal, by `router.attribution` and `router.summary` (pure functions, like
`metrics.work_items`):

```python
from syberlabs import Kit, router

kit = Kit.local(".syberlabs", ".")
events = [e for case in kit.journal.thread_ids() for e in kit.session.history(case)]
rows = router.attribution(events, dict(kit.session.contracts))   # one row per decision
print(router.summary(rows))
```

Each row is one decision and the attempt that followed it: `after.outcome` (`passed`, `failed`, `out_of_scope`,
`unevaluated`, `refused:<reason>`, `provider_error`, `none`), `trigger_fixed` (the check the decision read passed),
`same_failure` (the next decision read the same signature), `gained` (required checks gained), `evidence_passed` (a
lookup that returned results: whether an attempt that carried its evidence passed, its own or a later one),
`calls_to_pass` and `dollars_to_pass` (from this decision to the search's first pass, its own call included), and the
cost: `booked_dollars`, the spend the host booked at the attempt's registration, which also holds every metered call
since the previous registration that registered nothing (a refused answer, a decider's call; `calls_booked` counts
them), the call's own tokens, the evaluation's milliseconds, and the lookup's GitHits calls, results and latency.

The numbers that answer "what is one lookup worth", in the order to read them:

1. **The holdout comparison** (`summary(rows)["holdout"]`): the decisions that drew "look up" against those the draw
   held out, and the differences (looked up minus held out) in `pass_rate` (the next attempt passed) and
   `search_pass_rate` (the search passed after it at all). Every rate is per drawn decision: an attempt that was
   refused, went unevaluated or failed counts as not passed, never as a row dropped after the draw. Read
   `first_per_thread` first: one draw per work item, so its units are independent; `all` adds later draws within an
   item, which are not. The draw is the only thing that separates the two groups. Pre-register `P` (0.5 above), the
   minimum count of drawn items, and the outcome before reading it; with a handful of items it is a description, not
   a finding.
2. **Calls and dollars per lookup**: `lookup.calls_per_lookup` (GitHits calls per lookup decision),
   `dollars_to_pass` after a lookup against after a held-out one, and `lookup.evidence_passed`. GitHits' own price
   per call is not known to the host; the call count is what it will be multiplied by.
3. **Description only**: `by_action` (the pass rate after `lookup` against after `repair`) compares lookups with
   repairs of other failures, which the policy chose because they were different; read it as context, never as the
   lookup's effect.

**Before reading any of it**, check the record: every router thread's `routes.githits` is `ready` (not `no_token`);
the `off` arm made no GitHits lookup; the holdout's two groups are not wildly unequal in size; how many lookups ran
with a version from `traceback_site`, from `host_metadata`, or not at all (`local`, `missing_dependency`, or no
symbol kept: `diagnosis.vouched` is null); and how many questions the guard blocked (`blocked` with a rule other than
`holdout`). Report all of them beside the numbers.

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
- **Conservative about names.** A library name that the repository also defines (its own `Session` wrapper) is
  dropped, and with no name left there is no lookup. The privacy rules above leave two cases open: a made-up import
  name, and a distribution from a private index.
- **One search and one read per lookup.** No `grep`, `list` or `example`, no retry on a 429, no polling of an
  incomplete search: the GitHits client's own deferred items (`spec/GAPS.md`, 59) apply.
- **Results are untrusted text in the prompt.** The prompt says so and the host's checks and a person still decide,
  but a result can try to instruct the model, as it can in arm B, where the model reads results itself. Search and
  read results carry no license data in the GitHits contract 0.1.0, so the license policy cannot judge them.
- **The holdout estimates the effect of a lookup where the policy would make one**, not of a lookup anywhere, and
  `booked_dollars` can include a call made before the decision whose answer was refused.
- **The experiment report needs `--server none` for this arm** (above). The change that would let it check the
  router arm itself, reading the router's `route` facts as the arm's GitHits route, belongs to
  `syberlabs/experiment.py` and is described in the pull request, not made.

Claim ceiling: **implemented and tested**, against stand-ins only. Nothing is measured, deployed or externally
validated.
