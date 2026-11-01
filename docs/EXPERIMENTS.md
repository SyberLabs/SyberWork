# Experiments: two arms compared from the journal

```bash
syberlabs experiment label --name githits-pilot --item LIB-1 --arm off --tag needs_lookup   # right after start
syberlabs experiment report --name githits-pilot                    # the arms compared, with every check that applies
syberlabs experiment report --name githits-pilot --items items.txt  # against the item list registered before the first run
syberlabs experiment report --name githits-pilot --delivered review # delivered = accepted and a blind review passed
syberlabs experiment report --name githits-pilot --markdown         # the table a write-up prints
syberlabs experiment report --name githits-pilot --json             # every item, its threads, values and checks
syberlabs experiment report --name githits-pilot --csv              # one row per item
syberlabs experiment report --name githits-pilot --from-export receipts/*.json   # records from syberlabs export
python examples/githits_ab.py [--route api|mcp]                     # the whole pipeline, a stand-in instead of a model
```

An experiment runs the same work items through the factory more than once and changes one thing between the runs. Each run is a Build Thread; each thread is in one **arm**. The first consumer is the owner-approved GitHits pilot (2026-10-23), whose arms differ only in whether the Claude Code proposer has GitHits: since SyberWork #134 through the host's API client, `--githits` (`docs/CLAUDE_CODE.md`, "GitHits through the launcher"; `docs/GITHITS.md`, "The A/B report"), and before it through an MCP server, `--mcp-config` (`docs/CLAUDE_CODE.md`, "GitHits A/B through MCP"). The report reads both routes.

## The label

`syberlabs experiment label` writes one verified host observation on the current thread, key `experiment`, value `{name, item, arm, tags, by, at}`, versioned by the experiment's name (`Thread.record_experiment`, the route `note` and `review --record` use; no event kind is added). Names, items, arms and tags are short identifiers; a thread carries at most eight tags. The latest label for a name is the one in force.

Write the label right after `syberlabs start`, before the first candidate. The report reads the record to see whether you did. A thread whose label in force is not the one that stood before its first candidate (written after the work began, or changed after it) is `labelled_late`, and its item is excluded: a label settled once the result is known could move a run between arms or groups. A thread whose labels disagree, but whose label in force stood before its first candidate, is `relabelled` and stays in.

## The item list

`--items FILE` is the item list written down before the first run: one item per line, its id and then its tags; `#` starts a comment.

```text
# githits-pilot, registered 2026-10-24
LIB-1 needs_lookup
LIB-2 needs_lookup
RT-1  routine
```

With it, an item the list does not name is `unregistered` and an item whose threads carry other tags than the list gives it is `registration_mismatch`, both excluded; the report groups items by the list's tags; and a listed item that never ran is named under `not_run`, so a reader sees what was dropped. Without it the report has only the labels to go on, and cannot know about an item that was planned and never run.

## The report

`syberlabs.experiment.report(events, name)` is a pure function of the journal, like `syberlabs.metrics.work_items`, which it reads for every per-item number. It adds three numbers of its own:

| Number | Derivation | Unknown when |
|---|---|---|
| `time_to_pass` (hours) | the thread's first `search_started` to the host's `candidate_evaluated` of the candidate that was then accepted (its last evaluation before the acceptance) | not accepted, or the record has no search or no evaluation of the accepted candidate |
| `accepted` | the contract's promotion action (`evolution.promotion.action`, default `accept_change`) succeeded: its `effect_succeeded`, or a verified reconciliation of it | never |
| `reviewed` | accepted, and the latest `review` fact whose revision is the accepted commit (or a prefix of it, at least 7 characters) has verdict `pass`; false when that review failed or the thread was not accepted | accepted, and no review of the accepted commit is recorded yet |

`time_to_pass` stops at the evaluation, not at the acceptance: how long a passing change then waits for a person, or for `syberlabs run`, depends on who was at the keyboard and not on the arm.

**Every paired number stops at the thread's first acceptance.** Cost, turns, fix rounds, lookups, review rejections and human interventions are read from the events up to the first promotion effect; the checks still read the whole thread. Both arms of an item can be accepted, but only one arm's change can be published, and what follows publishing (review comments on the pull request, more work on the change, a person merging it) happens to that arm alone. Counting it would tilt every pair toward the arm that was not published. So what only the published arm can reach (pull requests opened, merges, `agent_time` to the merge) is reported per arm, unpaired, and the blind review that decides `reviewed` is recorded on both arms' accepted commits. A thread that is accepted, fails review and is worked on again is judged on its first accepted change.

### The comparison

The report compares the **treatment** (default `on`) with the **baseline** (default `off`) over the valid items: those with threads in both arms and no check that excludes them.

- **Finishing** (`accepted`, `reviewed`) is compared over every valid item: each arm's rate, the items only one arm finished, and the sign test over those. An unknown (a review not yet recorded) is counted beside it, never read as no.
- **Efficiency** (cost, turns, time to a passing change, fix rounds, web lookups, GitHits lookups, review rejections, human interventions) is compared over the valid items **accepted in both arms**, so a cheap run that never finished does not count as a win; the items left out are counted as `unfinished`. Per metric: the pairs and the unknowns, the arms' means and medians, the mean and median paired difference (treatment minus baseline), the change in the total, wins, losses and ties, an exact two-sided sign test, and a 95% t interval for the mean difference. GitHits lookups show use and have no better direction, so they get no wins and no test.

Every comparison is repeated per tag.

### The delivery rule

The report also reads one go/no-go off the valid items: **the treatment delivers at least as many items as the baseline, and over the items delivered in both arms its median paired cost is lower.** "Delivered" is `accepted` by default and `reviewed` with `--delivered review`, the pilot's definition (the host's checks passed, and a person who was not told the arm passed the change; record each verdict with `syberlabs review --record`). The rule reads `not met` as soon as nothing is pending and the treatment delivered fewer items. It is undecided, never guessed, while any item's delivery is unknown (a review not yet recorded), while a pair delivered in both arms has no comparable price, or when no item was delivered in both arms.

The rule is read overall and again per tag. They can disagree, and the per-tag reading is the one to pre-register: if GitHits saves a lot on items that need a library lookup and costs a little more on routine ones (it is called and has nothing to add), the median over all items can land on the routine side even though GitHits helped exactly where it should. `examples/githits_ab.py` shows this: its rule is not met overall and met on `needs_lookup`. Name the group the decision rests on before the first run.

## Checks

Each item is checked against the record. The first nine take the item out of every comparison; the rest are named and the item stays in.

| Check | Effect | Meaning |
|---|---|---|
| `missing_arm` | excluded | the item has threads in one arm only |
| `unequal_repeats` | excluded | the arms ran the item a different number of times, so a mean of several runs would face one run |
| `base_mismatch` | excluded | the arms started from different commits (the `base` observation) |
| `objective_mismatch` | excluded | the arms were given different objectives |
| `arm_conflict` | excluded | the record contradicts the label: GitHits (`--server`, default `githits`) was there in a baseline call, or in none of a treatment thread's calls (below, "How the arm is read"). On the API route: arm B run without `--githits`, or arm A with it or with a GitHits `--mcp-config` |
| `contaminated` | excluded | the baseline shows a GitHits lookup: its GitHits count is above zero, or the host observed one (below) |
| `labelled_late` | excluded | a thread's label in force, in any arm, is not the one that stood before its first candidate |
| `unregistered` | excluded | `--items` was given and does not list the item |
| `registration_mismatch` | excluded | the item's threads carry other tags than `--items` gives it |
| `base_unknown` | kept | a thread has no recorded base commit |
| `arm_unverified` | kept | no call recorded its MCP servers or the GitHits launcher, so only the label says which arm ran |
| `server_mixed` | kept | GitHits was there in some of a treatment thread's calls and not in others |
| `two_routes` | kept | a treatment thread had GitHits by both routes: the API launcher and a connected GitHits MCP server, usually a `--mcp-config` left in arm B by mistake. It is still the treatment, but not the single route the pilot registers, and its count includes authentic client records plus host-observed MCP attempts (`spec/GAPS.md` 59). What to do: take the GitHits server out of arm B's `--mcp-config` before the next item; say in the write-up that it ran with both routes; do not treat it as the registered single-route experiment |
| `unused` | kept | the treatment shows no GitHits lookup, and its count is known to be zero |
| `use_unknown` | kept | the treatment shows no GitHits lookup, but its count is unknown, so it can be shown neither to have used GitHits nor not to. The count is unknown when a call declared none, or, where `syberlabs metrics` says so (SyberWork #139), when the record cannot say how many lookups reached GitHits: a call's `signal.githits.mismatch` (a launcher call the guard passed left no client record) or an `unguarded` client record. What to do: `syberlabs metrics --thread <id> --json` gives the reason in the count's note; fix the cause (a client that failed before recording, a proposer that declares no lookups) before the next item, and do not read the item as one where GitHits went unused |
| `contamination_unknown` | kept | the baseline shows no GitHits lookup, but its count is unknown, so it cannot be shown clean |
| `relabelled` | kept | a thread's labels disagree, but the one in force stood before its first candidate |
| `model_mismatch` | kept | the arms were served by different models |
| `cost_basis_mismatch` | kept, cost not compared | the arms' cost was recorded on different bases or credential lanes |
| `outcome_mismatch` | kept, efficiency not compared | one arm was accepted and the other was not |
| `tag_mismatch` | kept | the arms' threads carry different tags; their union is used (the registered tags, with `--items`) |
| `repeats` | kept | each arm ran the item the same number of times, more than once; an arm's value is the mean of its runs, known only when every run's is |
| `other_arm` | kept | the item also has threads in arms this report does not compare |
| `unlisted_run` | kept | another thread ran this item's objective from the same commit without this experiment's label |

Two lines are about the experiment as a whole. `not_run` names the registered items that never ran. `forgotten` names the forget tombstones written since the experiment's first thread started, because a forgotten thread leaves nothing else behind; under the default retention policy a thread with an accepted change is kept for 365 days, so a tombstone here is usually a run that was not accepted and was removed. Exported records carry no tombstones, so a report `--from-export` says it cannot see forgotten threads. `--from-export` also refuses a record whose hash chain does not verify (`export_unverified`): an edited export is not counted.

### How the arm is read

The report does not take the label's word for the arm. It reads, call by call, the host's record of whether the call had GitHits, by either route:

- **The API route** (`--githits`, the pilot's since SyberWork #134): the candidate carries `signal.githits`, the ledger the Claude Code proposer writes on the candidate of every call it gave the host's GitHits launcher, and of no other. It holds the host's own count of the client records it verified under the call's key (`records`), and it is there whether or not the model used the launcher, so a treatment that looked nothing up is `unused`, not in conflict. What the model drove (`signal.tool_usage`, `signal.githits.launcher_calls`), the skill text it was shown (`signal.harness.skill`) and the operator's name for the arm (`signal.githits.arm`, from `--githits-arm`) are not read for the arm.
- **The MCP route** (`--mcp-config`, the pilot's before #134): `signal.harness.mcp_servers` lists the server `connected`, as Claude Code's `init` event said. The proposer leaves `mcp_servers` out when its session started with none, so a call whose harness recorded its session's start (a `version` or `model`) and lists no servers had none connected; a call that recorded neither the ledger nor anything of its start is not read.

A thread whose calls all had GitHits is the treatment's; one where none did is the baseline's; anything else is `server_mixed` in the treatment and `arm_conflict` in the baseline. A treatment thread with both routes is `two_routes`.

A GitHits lookup is shown by the thread's GitHits count (`syberlabs metrics`' `githits_lookups`; on the API route, the number of the host's `lookup` facts from authentic client records) above zero, or by a `lookup` fact the host observed and can vouch for: a GitHits MCP call (`mcp__githits__*`), or a client record that a guarded launcher call stands behind (`githits.<op>`, guard `pass`; a `githits.doctor` readiness check is not a lookup). That includes the lookups of a call that failed before its candidate, which no registration counts. An unknown count is never read as zero: it shows no baseline clean (`contamination_unknown`) and no treatment unused (`use_unknown`).

`--server none` turns these checks off for an experiment that does not switch GitHits; another `--server` name reads that MCP server only, never the launcher. `contaminated`, `contamination_unknown`, `unused`, `use_unknown` and `two_routes` read GitHits lookups and routes, and apply only with `--server githits`.

## Statistics, stated plainly

- The **item** is the unit, not the thread or the call.
- A metric is compared over the valid items whose value is known in **both** arms. An unknown is counted beside the comparison and never imputed: `unknown` means the record cannot say, never zero. Values are rounded to nine places first, so float noise never turns a tie into a loss.
- The sign test is exact and two-sided over untied pairs. **With fewer than six untied pairs no result can reach p < 0.05**, even if the treatment wins every one. With ten, 9 wins and 1 loss gives p = 0.021, and 8 and 2 gives p = 0.109.
- The sign test and the median answer "on how many items did the treatment do better"; the mean and the change in the total answer "how much did it save overall". When a few items save a lot and most are about the same, the two disagree and both are true. Decide before the first run which question the decision rests on.
- The interval is Student's t on the paired differences: the mean, plus or minus t times their standard deviation over the square root of the number of pairs, t read from a table by degrees of freedom (the next smaller row when the exact one is not in it, which widens the interval). It is not given below two pairs, or when every difference is the same. It holds its 95% only when the differences are roughly normal. Simulated, 20,000 experiments per cell, the interval as `experiment.t_interval` computes it:

  | Paired differences | 6 items | 10 items | 20 items |
  |---|---|---|---|
  | normal | 95% | 95% | 95% |
  | lognormal, σ = 1 | 82% | 84% | 87% |
  | lognormal, σ = 1.5 | 69% | 71% | 76% |
  | 85% about zero, 15% a large saving (exponential) | 53% | 65% | 76% |

  The last row is the shape the GitHits hypothesis predicts. Read the interval as a guide, and lead with the per-item table and the sign test.
- Nine sign tests are reported at once and nothing corrects for that. Name the primary metric before the pilot starts and read the others as description.
- "Change" is the change in the total over the compared items. It is "-" when the baseline's total is zero: a ratio of nothing, not an unknown.

## The GitHits pilot

The pilot itself runs a real model with a paid key: Class X (`AGENTS.md`). A named person executes it. What follows is the protocol to execute it by.

**Before the first run, written down and shared with GitHits:**

1. The item list, as the `--items` file: each work item's id and its tag, `needs_lookup` when the work depends on a library or API the agent is unlikely to know well (new, niche, or changed since the model's training), else `routine`; beside it, each item's objective and scope.
2. The primary metric and the group the decision rests on (for example: the delivery rule on `needs_lookup` items, with `--delivered review`).
3. The exclusion rules: the nine excluded checks above, and nothing else.
4. The frozen setup: the SyberWork commit, Claude Code version, model, effort, `--allowed-tools`, `--max-turns`, the query guard's thresholds, the credential lane, and arm B's GitHits route (`--githits`) with its license policy (`SYBERLABS_GITHITS_LICENSES`, recorded as `signal.githits.license_policy`). Use the `api-hooked` lane (`--lane api-hooked --trusted-tree`) in both arms: the API key, so the cost is billed and known, without `--bare`, because Claude Code 2.1.312 runs no `--settings` hook under `--bare` and the provider refuses `--githits` there (`docs/CLAUDE_CODE.md`, the hooked API lane); each call gets an empty config directory and home. Not in bare mode, the tree's `CLAUDE.md` is read, equally in both arms: record it with the conditions. The contract prices every model the runs report (`evolution.budget.prices`); a run that used a model the contract does not price has an unknown cost. Anything that learns from runs stays off for the week. AutoSkill is a design (`docs/AUTOSKILL.md`) and is in no proposer path; the `jev` and `empirical` branch policies belong to `--evolve`, which the pilot does not use.

**Before the week, the dry run** (Class X as well, a few dollars): `python scripts/pilot_dry_run.py --plan plan.json` runs this protocol on 3 to 5 items on the API route (`--githits`, `docs/GITHITS.md`, "The A/B report"), in a home of its own: a preflight that refuses to start on any failure, one canary call that shows the query guard fires on the `api-hooked` lane, both arms per item from one `HEAD` in a seeded order, the blind review and this report under a hard budget cap (default $8). It passes when the nine metrics are known for every thread and the report compares every item; its docstring says how to run it, and its `receipt.json` and `report.md` are what to send.

**Each item, each arm, from the same `HEAD`** (accepting moves only the thread's own branch, so `HEAD` stays put):

```bash
syberlabs start "Parse ISO dates with the UTC offset" --paths src tests --unattended
syberlabs experiment label --name githits-pilot --item LIB-1 --arm off --tag needs_lookup
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch
syberlabs run --once                     # one safe step per thread; here, accepting the passing candidate

syberlabs start "Parse ISO dates with the UTC offset" --paths src tests --unattended
syberlabs experiment label --name githits-pilot --item LIB-1 --arm on --tag needs_lookup
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch \
    --githits --githits-arm on           # the only difference between the arms: the launcher and the skill
syberlabs run --once
```

Arm B needs `GITHITS_API_TOKEN` in the environment (the proposer refuses `--githits` without it, and never writes it down). Arm A drops every GitHits variable before Claude Code starts (#135), so both arms can run from the same shell. Pass no GitHits `--mcp-config` in either arm: in arm A it is `arm_conflict`, in arm B `two_routes`. Before #134 the pilot's route was an MCP server: `--mcp-config githits.json` in arm B, with `mcp__githits` in both arms' `--allowed-tools`. The report still reads an experiment run that way.

`--claude-code` is the proposer of SyberWork #80, with the query guard of #82 and the GitHits launcher of #134. This report works with any proposer, but needs those to read the arm and count the lookups (`arm_unverified` and unknown lookups otherwise). Alternate which arm runs first from item to item, so drift in the model or the network during the week does not fall on one arm. Accept on the host's checks (`--unattended`), or have a person accept without being told the arm: the label is in the journal, not in the diff or the pull request. Then have a person who is not told the arm review **both** arms' accepted commits, and record each verdict on its thread:

```bash
syberlabs review --record --verdict pass --reviewer blind-1 --revision 3f9c2ab
```

Publish at most one arm's change per item. The paired numbers stop at acceptance, so which arm you publish does not move them; it decides which arm's pull request, merge and agent time the per-arm lines show. An item that fails in one arm is not run again in that arm alone (`unequal_repeats`), and a run is not hidden with `syberlabs forget` (its tombstone is named).

**At the end of the week:**

```bash
syberlabs settle                                              # merges, for agent time to merge (SyberWork #79)
syberlabs experiment report --name githits-pilot --items items.txt --delivered review   # read the checks first
syberlabs experiment report --name githits-pilot --items items.txt --delivered review --markdown > pilot.md
syberlabs experiment report --name githits-pilot --items items.txt --delivered review --csv > pilot.csv
```

What leaves the factory is the report and the CSV. The journal, the prompts and the code stay where they are; the record holds lookups as digests only (`docs/CLAUDE_CODE.md`).

## Limits

- The report compares what the record holds. A lookup the proposer did not declare, a model call outside the host, or a person's message to an agent outside the CLI is unknown or missing to it, as it is to `syberlabs metrics` (`docs/METRICS.md`, section 4).
- The checks catch a label settled after the work began, an item outside the registered list, an unlabelled run of the same objective from the same commit, and a forgotten thread. They cannot catch a run made in another home, or one whose objective was reworded.
- The arm check reads what the proposer recorded: the GitHits launcher's ledger and its MCP servers. It catches a treatment run without `--githits` (or whose GitHits server failed to connect), and a baseline given GitHits by either route. It cannot catch a proposer that misreports: a search-protocol adapter writes its own `signal`, so it could declare a ledger or a server it never had.
- One week of items is a small sample. The report says how small, metric by metric; it does not turn a small sample into a finding.
- `examples/githits_ab.py` runs every step with a stand-in instead of a model. On its default route (`--route api`), that is the real Claude Code proposer with a stand-in `claude` and a loopback GitHits; `--route mcp` uses a search-protocol stand-in that declares its MCP servers. Its numbers are invented to exercise the report and say nothing about GitHits.
