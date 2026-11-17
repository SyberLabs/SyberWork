# Work: a plan checked by code, one agent per ticket, a thread whose scope is the plan

[Intake](FACTORY_INTAKE.md) turns a request into a ticket. This page is what happens between that ticket and a
Build Thread: a plan is written before any code and checked by code, one agent claims the ticket, and the thread
that builds it may change the plan's files and no others.

Package: `syberlabs/factory/plan.py`, `work.py`, `review_queue.py` and `work_cli.py`, on the ledger of
[FACTORY.md](FACTORY.md). Standard library only. Claim ceiling for this page: **implemented** and **tested**
(`tests/test_factory_work.py`, 35 tests; `tests/test_factory_review_queue.py`, 15 tests; the `factory` simulation
campaign). Nothing here is measured: no model has written a plan through it, no agent fleet has run on it, and
the queue's order has not been compared with the order a person would choose.

## The four rules

| Rule | How it is kept |
| --- | --- |
| An agent takes only planned work | `work claim` refuses a ticket whose request is not `agent_ready` as it reads now, that has no plan for that text, or whose plan waits for a person (`ticket_unplanned`) |
| One agent works a ticket | The claim is appended under the ledger's lock after the check, so of two agents claiming at once one is refused (`ticket_claimed`) |
| The agent follows the plan | `work start` writes a contract whose mutable scope is exactly the plan's files. The Build Thread's kernel refuses a candidate that changes any other path (`candidate_out_of_scope`), as it refuses every out-of-scope candidate |
| The change names its request | The thread's objective begins `Fixes owner/repository#N` for a GitHub issue, and the pull request body SyberWork writes carries the objective (`publish.py`). GitHub reads those words as a closing keyword when a pull request merges into the default branch; no pull request was opened to observe that here |

The same four are in `AGENTS.md` for the agents that read it. They are written there as what will be refused,
because that is what they are.

## A ticket's status

Nothing is stored about a ticket except events. `work.state` derives the status each time it is asked:

| Status | Means | Label |
| --- | --- | --- |
| `held` | The request is not `agent_ready` as it reads now: it was edited into another route, or a person declined it | its route's (`needs-human`, `unclear`, `duplicate`, `declined`) |
| `open` | No plan exists for the request's current text | `agent-ready` |
| `plan_held` | The newest plan waits for a person | `needs-human` |
| `plan_rejected` | A person rejected the newest plan; it is planned again | `agent-ready` |
| `planned` | An agent may claim it | `planned` |
| `claimed` | One agent holds it and has not started a thread on this plan | `agent:working` |
| `working` | The holder started a Build Thread | `agent:working` |
| `ready` | That thread holds an accepted change. Nothing here pushes or merges it | `ready` |

An edited request is another digest, so a plan made for the old text, and any claim on that plan, hold nothing:
the ticket is `open` or `held` again. A claim lapses when its `ttl` runs out, with no event written.

The label column is `work.label`: the status as a tracker would show it, for an operator who mirrors the ledger
onto issues. `work show` and `work list --json` carry it. The ledger is the record, and nothing here applies a
label: a label on an issue can be set by anyone who can edit the issue, an event on the chain cannot.

## The plan

`work plan TICKET --planner NAME=COMMAND` runs one command and records what came of it.

**What the planner reads.** On stdin, the ticket's id, kind, size and area; the request's visible title and body
from intake's snapshot (never the request as it reads now, and never anything the screen removed); the tracked
paths at HEAD; the files of the ticket's area; the limits; the risk kinds it may name. Its working directory is a
temporary checkout of HEAD (`gitspace.Repo.worktree`), so a planning agent can read the code. It is not the
operator's working tree: no uncommitted edit, no untracked file, no `.syberlabs`. Its environment is the one
intake gives a decider (`intake.run_reader`: the scrubbed environment of a check plus the operator's
`SYBERLABS_INTAKE_ENV` names, a time limit, an output cap). That narrows what it holds; it is not a sandbox.

**What it prints.** One JSON object:

```json
{"files": ["fxapp/rules.py"], "new_files": ["tests/test_rules_request.py"],
 "steps": ["Guard the division in decide.", "Add a test for quantity 0."],
 "risks": [{"kind": "other", "note": "One function changes."}]}
```

**What `plan.check_plan` refuses** (`planner:invalid:<part>`, and the ticket waits for a person): anything but
one strict JSON object with these fields; a path that is absolute, climbs with `..`, or is under `.git` or
`.syberlabs`; a `files` entry that is not tracked at HEAD; a `new_files` entry that is (compared with case
folded), or whose name holds a character Windows refuses in a file name or a backtick; a path named twice; no
path at all; no step, more than twelve, or a step that is not one printable line; a risk whose kind is not one
of `auth`, `secrets`, `payments`, `data`, `release`, `external_effect`, `dependency`, `other`; and any step or
note that holds text a reader would not see (an HTML comment, a hidden element, stray variation selectors), or
the shape of a credential or of an order to an AI agent, by the screen intake puts a request through. A plan is
text a model wrote after reading a request, and its steps go into a builder's prompt and a pull request body, so
it is screened as the request was, and the person who approves it reads everything the builder will.

**What `plan.assess` computes**, from the paths and never from the planner:

- the **class floor** of every path: the highest-ranked rule it matches in a policy of `policy.json`'s shape
  (`--floors FILE`; this repository's own `policy.json` is one), `auto` when it matches none. Without a file the
  built-in floors hold at `keys` the paths this repository's `policy.json` holds there (`.env` files, `*.pem`,
  `*.key`, `secrets/`, deploy, release and publish workflows, `LICENSE`, `NOTICE`, `.mcp.json`, `.claude/**`,
  `CLAUDE.md`), and at `direction` `.github/**` and `policy.json`;
- the **instruction files** among them: `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `.mcp.json` and anything
  under a `.claude` directory, at any depth, whatever the floors say. This is its own rule because the floors
  differ on these files: `policy.json` floors `AGENTS.md` at `auto`, and a plan from a stranger's request that
  rewrites what the next agent is told still goes to a person;
- the **components** the paths belong to, from the Space graph: an indexed file has the one the graph mapped it
  to, and any other path (a file that does not exist yet, or one the graph does not index) belongs to every
  component whose declared path globs match it, so a new file under a sensitive component is seen as part of it. Then every component that depends on them
  (`features.impact`), and the components outside the area the ticket named;
- whether a **test** is among the paths.

**The route** (`plan.plan_route`, an ordered rule list; the first that applies decides):

| Rule | Applies when | Route | Reason code |
| --- | --- | --- | --- |
| `planner.absent` | No planner and no written plan | `needs_human` | `no_planner` |
| `planner.refused` | It timed out, failed, or its output was refused | `needs_human` | `planner:<code>` |
| `plan.instruction_files` | A path is a file an agent reads as instructions | `needs_human` | `instruction_files` |
| `plan.class_floor` | A path floors above `auto` | `needs_human` | `class_floor:<class>` |
| `plan.sensitive_area` | A component is one of `--sensitive-area` | `needs_human` | `sensitive_area:<component>` |
| `plan.risk` | The planner named `auth`, `secrets`, `payments`, `data`, `release` or `external_effect` | `needs_human` | `risk:<kind>` |
| `plan.too_large` | More paths than `--max-files` (6 by default, 12 at most) | `needs_human` | `plan_too_large:<count>` |
| `planned` | None of the above | `planned` | `planned` |

So a request that said nothing about credentials still reaches a person when the plan to resolve it names a
`.env` file: the risk is read from the paths, before any code exists.

A person decides a held plan with `work approve TICKET --by human:ID --reason "..."` (or `--reject`), and can
write the plan themselves: `work plan TICKET --plan-file plan.json --by human:ID` puts a person's plan through
the same checks and the same rules. Only a human actor approves or writes; a model's plan comes through a planner
command.

`ticket_planned` records the paths, the count of steps, the planner's name, model and outcome, the assessment, the
digests of the floors and the limits, the route, the rule and the reason. The steps and the notes, which are a
model's words, are in the plan file under `plans/` in the ledger home, named by its digest (`load_plan` refuses
one that no longer digests to its name). `work show TICKET --text` renders the plan for a person.

## The claim

`work claim TICKET --by KIND:ID [--ttl SECONDS]` takes a planned ticket, or renews the holder's own claim. It is
refused for a ticket that is not planned, and for one another agent holds. `work release TICKET --by KIND:ID
--reason "..."` gives it up: the holder releases its own claim, a person releases anyone's. A claim lasts four
hours unless `--ttl` says otherwise (one minute to seven days) and then holds nothing.

A claimed ticket is not planned again and its plan is not decided on again until it is released.

## The thread

`work start TICKET --by KIND:ID --check NAME=COMMAND ...` is for the holder. It:

1. reads the plan (by digest) and checks it still fits HEAD: every planned file is tracked and no file it meant
   to add exists (`plan_stale` otherwise);
2. writes a contract under the Build Thread home: scope `paths` are the plan's files and new files, `max_files`
   is their count, `max_diff_bytes` is the limit the plan was made under, every `--check` is required;
3. starts a thread on it from HEAD with the objective below, attended, in the class the plan's paths floor at:
   `auto` is R, `auto` with an evidence grade is P, `direction` is C, `keys` is X (the aliases in `AGENTS.md`).
   A candidate cannot leave those paths, so none can floor above the plan;
4. records `work_started` with the thread id and that class.

```text
Fixes acme/app#12: rules crash on quantity zero
Plan 5852ed18187a: 1. Guard the division in decide.; 2. Add a test for quantity 0.
```

The first line is the request's visible title behind `Fixes <ref>` (for a source that is not a GitHub issue:
the title, then the source in parentheses). The second carries the plan's steps to whatever proposes, since the
objective is what a provider's request holds. Then any proposer works the thread as it works any thread
([BUILD_THREAD.md](BUILD_THREAD.md)): `syberlabs use THREAD`, `syberlabs propose --repair COMMAND` or
`--claude-code`, `syberlabs check`, and a person runs `syberlabs accept`. A second `work start` by the same holder
returns the thread it already started.

`work settle TICKET` reads that thread's status and records `work_ready` once it holds an accepted candidate,
with the thread, the candidate, the commit and the branch. That is the end of this line: `ready` means the
accepted change is on `syberlabs/<thread>`. Pushing it and opening a pull request are the thread's own publishing
actions, and merging is the act [BUILD_THREAD.md](BUILD_THREAD.md) leaves to a person on GitHub.

## The queue for the person who merges

A factory that builds faster than a person can read moves the wait to the merge. `work queue` does not shorten
the reading; it orders it. It lists every `ready` ticket, first to read first, and for each change the order to
read its files in. It is computed from the ledger, the Space graph and Git. No model is asked.

**The order of the queue** is an ordered list of keys; the first difference decides (`review_queue.sort_key`):

| Key | First | Why |
| --- | --- | --- |
| `blocks` | the change more tickets are waiting on | A ticket that is not ready yet waits on a change when its plan names a path the change touches. Merging that change first is what lets the others build on what will be there |
| `files` | fewer files | Small before large is shortest job first: the order in which the changes behind wait least in total |
| `diff_bytes` | the smaller diff | The same, when the file counts are equal |
| `waiting_since` | the one ready longest | So nothing waits forever behind newer work |

A change whose accepted commit is not in the repository the queue is read from (another clone, a deleted
branch) keeps its row, lists its plan's paths, and goes last: what cannot be read is not read first. Every row
carries the keys that placed it, so a person who orders differently sees what they overrule. Kind and size from
intake are shown and are not keys: nothing here knows which request matters more to the business.

**The reading order of one change** (`review_queue.reading_order`) groups the files the accepted commit changes
by component and puts a component after the ones it depends on (the Space graph's `depends_on`), so the code
that is called is read before the code that calls it. Each group is a layer; files with no component are the
last one. Components that depend on each other, and ties, go by name. The paths are the accepted commit's
against the thread's base, not the plan's: a plan may allow a file the change did not touch.

```text
1. github_issue:acme/app#12  rules crash on quantity zero
   ticket 5c0f6f0be7b9d6b4  bug S  class R  owner mateo
   placed by: blocks 1, 3 file(s), 612 bytes, ready since event 41
   waiting on it: 9a41c2d07e6f1b35
   branch syberlabs/7f3a9c at 1e2d3c4b5a; accepted, not pushed or merged here
   read in 3 layer(s), each after what it depends on:
     1. rules: fxapp/rules.py
     2. main: fxapp/main.py
     3. (no component): tests/test_rules_request.py
```

A row also names the other ready changes that touch the same files: whichever merges second is rebased.

The queue reads `work_ready` records, and those are written by `work settle`. `work settle` with no ticket looks
at every ticket that has a thread and records the ones that hold an accepted change, so run it before reading the
queue.

What it is not: it splits nothing. A change with several layers can be read layer by layer, and a person who
wants one pull request per layer makes them; the layers say where the cuts are. It pushes and merges nothing,
appends no event and moves no ref. It reads one ledger, so it lists the work of the agents that share it.

## From a terminal

```sh
python -m syberlabs.factory work list                                  # every ticket and its status
python -m syberlabs.factory work plan OWNER/REPO#12 --planner "claude=/abs/python /abs/planner.py" --floors policy.json
python -m syberlabs.factory work show OWNER/REPO#12 --text             # the plan as a person reads it
python -m syberlabs.factory work approve OWNER/REPO#12 --by human:mateo --reason "read it"
python -m syberlabs.factory work claim OWNER/REPO#12 --by model:claude-1
python -m syberlabs.factory work start OWNER/REPO#12 --by model:claude-1 --check "tests=/abs/python -m unittest -q"
python -m syberlabs.factory work settle OWNER/REPO#12                  # exit 0 once the thread holds an accepted change
python -m syberlabs.factory work release OWNER/REPO#12 --by model:claude-1 --reason "blocked on a decision"
python -m syberlabs.factory work settle                                # the same for every ticket that has a thread
python -m syberlabs.factory work queue                                 # ready work, first to read first; --json for a tool
```

A ticket is named by its id, the first eight or more characters of it, or its request. `--home` is the factory
ledger and `--root` the repository. A `NAME=COMMAND` is split as every factory command splits one
(`providers.split_command`): on Windows quote characters stay in the words, so write the command without quotes
or put it in a script file.

## What it does not do

- **Propose, check, accept, push, open a pull request or merge.** `work start` ends with a thread waiting for a
  proposer. Everything after is the Build Thread's, with a person accepting.
- **Call a model.** The planner is a command; this packet ran only the stand-in
  (`tests/fixtures/intake/standin_planner.py`). A planning agent behind it, Claude Code with read-only tools for
  example, is an adapter nobody has written here.
- **Choose which ticket an agent takes.** `work list --status planned` shows what may be taken, in the order the
  requests were decided. The queue orders what a person merges, not what an agent starts.
- **Enforce a class.** The floors route a plan and name the thread's class, so a plan a person approved above
  `auto` does not become a pull request that says R. The class is still recorded, not enforced (`AGENTS.md`):
  neither the kit nor a ruleset asks for the key it names. What a candidate cannot do is leave the plan's paths.
- **Add an event kind.** `ticket_planned`, `plan_resolved`, `ticket_claimed`, `ticket_released`, `work_started`
  and `work_ready` are `note` events.

## Limits

- The floors are path globs. A plan that changes what a credential is used for, in a file no rule names, floors
  at `auto`. The planner's named risks are the second layer, and they are a model's words.
- A plan names files, not lines. A builder may change anything inside a planned file. The steps are guidance in
  the objective, and nothing checks a candidate against them.
- The steps are in the thread's objective, so they are in the pull request's title (cut at 200 characters) and
  body. A person may want to retitle it. A step is plain text here; where it is rendered as Markdown, a link or
  a mention in it is live. Nothing hidden is in it, and nothing checks where a link points.
- `work start` starts the thread and then records it. If the ticket changed in between (its claim lapsed and
  another agent took it), the thread exists and is not recorded as the ticket's; the refusal names it, and
  `syberlabs forget` removes it.
- A claim is a record on one ledger. Agents on two machines with two ledgers do not see each other's claims. A
  claim that lapsed while its holder was still working lets a second agent start a second thread.
- `work settle` is asked for; nothing polls. A thread accepted yesterday is `working` until someone settles it.
- The stand-in planner plans the first file of the ticket's area and one new test file. Nothing about it says
  how a model would plan.

## Class and first consumer

This is the second half of the subsystem [FACTORY_INTAKE.md](FACTORY_INTAKE.md) starts, so it is Class C on the
same terms: an agent drafted it, a person with the key admits it, and the first consumer proposed there is the
one proposed here. The `AGENTS.md` section it adds is canonical text and waits for the same key.

## Next packets

- **A planning agent**: Claude Code with read-only tools behind the planner command, on its own credential lane
  ([CLAUDE_CODE.md](CLAUDE_CODE.md)).
- **Claims across machines**: the ledger anchored somewhere two operators can both read.
