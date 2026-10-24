# Experiments: two arms compared from the journal

```bash
syberlabs experiment label --name githits-pilot --item LIB-1 --arm off --tag needs_lookup   # before the first candidate
syberlabs experiment report --name githits-pilot                    # the arms compared, with every check that applies
syberlabs experiment report --name githits-pilot --delivered review # delivered = accepted and a blind review passed
syberlabs experiment report --name githits-pilot --markdown         # the table a write-up prints
syberlabs experiment report --name githits-pilot --json             # every item, its threads, values and checks
syberlabs experiment report --name githits-pilot --csv              # one row per item
syberlabs experiment report --name githits-pilot --from-export receipts/*.json   # records from syberlabs export
python examples/githits_ab.py                                       # the whole pipeline, a stand-in instead of a model
```

An experiment runs the same work items through the factory more than once and changes one thing between the runs. Each run is a Build Thread; each thread is in one **arm**. The first consumer is the owner-approved GitHits pilot (2026-10-23), whose arms differ only in the Claude Code proposer's `--mcp-config` (`docs/CLAUDE_CODE.md`, "GitHits A/B").

## The label

`syberlabs experiment label` writes one verified host observation on the current thread, key `experiment`, value `{name, item, arm, tags, by, at}`, versioned by the experiment's name (`Thread.record_experiment`, the route `note` and `review --record` use; no event kind is added). Names, items, arms and tags are short identifiers; a thread carries at most eight tags. The latest label for a name is the one in force.

Write the label right after `syberlabs start`, before the first candidate. The report reads the record to see whether you did: a label written after the thread's first candidate is marked `labelled_late`, and a thread whose labels for one experiment disagree is marked `relabelled`. Both stay in the comparison and are named in the report, so a reader can see them.

## The report

`syberlabs.experiment.report(events, name)` is a pure function of the journal, like `syberlabs.metrics.work_items`, which it reads for every per-item number. It adds two numbers of its own:

| Number | Derivation | Unknown when |
|---|---|---|
| `time_to_accept` (hours) | `case_created` to the contract's promotion action (`evolution.promotion.action`, default `accept_change`) succeeding: its `effect_succeeded`, or a verified reconciliation of it | not accepted |
| `accepted` | that effect is in the record | never |
| `reviewed` | accepted, and the latest `review` fact whose revision is the accepted commit (or a prefix of it, at least 7 characters) has verdict `pass`; false when it failed or the thread was not accepted | accepted, and no review of the accepted commit is recorded yet |

Both arms of an item can be accepted, but only one arm's pull request can merge. So the arms are compared on `time_to_accept`, and `agent_time` (to merge) and delivery are reported per arm, unpaired.

### The delivery rule

The report also reads one go/no-go off the valid items: **the treatment delivers at least as many items as the baseline, and over the items delivered in both arms its median paired cost is lower.** "Delivered" is `accepted` by default and `reviewed` with `--delivered review`, the pilot's definition (the host's checks passed, and a person who was not told the arm passed the change; record each verdict with `syberlabs review --record`). The rule is undecided, never guessed, while any item's delivery is unknown (a review not yet recorded), while a pair delivered in both arms has no price, or when no item was delivered in both arms.

The rule is read overall and again per tag. They can disagree, and the per-tag reading is the one to pre-register: if GitHits saves a lot on items that need a library lookup and costs a little more on routine ones (it is called and has nothing to add), the median over all items can land on the routine side even though GitHits helped exactly where it should. `examples/githits_ab.py` shows this: its rule is not met overall and met on `needs_lookup`. Name the group the decision rests on before the first run.

The report compares the **treatment** (default `on`) with the **baseline** (default `off`) over the items that have threads in both. For each metric (cost, turns, time to acceptance, fix rounds, web lookups, GitHits lookups, review rejections, human interventions) it gives the number of pairs and of unknowns, the arms' means and medians, the mean and median paired difference, the change in the total, wins, losses and ties, an exact two-sided sign test, and a 95% bootstrap interval for the mean difference. `accepted` gets each arm's rate and the items accepted in one arm only. Every comparison is repeated per tag.

## Checks

Each item is checked against the record. The first five take the item out of every comparison; the rest are named and the item stays in.

| Check | Effect | Meaning |
|---|---|---|
| `missing_arm` | excluded | the item has threads in one arm only |
| `base_mismatch` | excluded | the arms started from different commits (the `base` observation) |
| `objective_mismatch` | excluded | the arms were given different objectives |
| `arm_conflict` | excluded | the record contradicts the label: the server (`--server`, default `githits`) was connected in the baseline, or not connected in the treatment, as the proposer recorded it in `signal.harness.mcp_servers` |
| `contaminated` | excluded | the baseline made GitHits lookups |
| `base_unknown` | kept | a thread has no recorded base commit |
| `arm_unverified` | kept | no call recorded its MCP servers, so only the label says which arm ran |
| `server_mixed` | kept | the server was connected in some of a treatment thread's calls and not others |
| `unused` | kept | the treatment made no GitHits lookups |
| `labelled_late` | kept | the label was written after the thread's first candidate |
| `relabelled` | kept | the thread's labels for this experiment disagree |
| `model_mismatch` | kept | the arms were served by different models |
| `cost_basis_mismatch` | kept, cost not compared | the arms' cost was recorded on different bases or credential lanes |
| `tag_mismatch` | kept | the arms' threads carry different tags; their union is used |
| `repeats` | kept | an arm ran the item more than once; the arm's value is the mean, known only when every repeat's is |

`--server none` turns off the server checks for an experiment that does not switch an MCP server.

## Statistics, stated plainly

- The **item** is the unit, not the thread or the call.
- A metric is compared over the valid items whose value is known in **both** arms. An unknown is counted beside the comparison and never imputed: `unknown` means the record cannot say, never zero.
- The sign test is exact and two-sided over untied pairs. **With fewer than six untied pairs no result can reach p < 0.05**, even if the treatment wins every one. With ten, 9 wins and 1 loss gives p = 0.021, and 8 and 2 gives p = 0.109.
- The bootstrap interval resamples items with replacement, 2,000 times, seed 0, so two readers get the same interval. With few pairs it is wide, and it should be read that way.
- Nine metrics are compared at once and nothing corrects for that. Name the primary metric before the pilot starts and read the others as description.
- "Change" is the change in the total over the compared items. It is "-" when the baseline's total is zero: a ratio of nothing, not an unknown.

## The GitHits pilot

The pilot itself runs a real model with a paid key: Class X (`AGENTS.md`). A named person executes it. What follows is the protocol to execute it by.

**Before the first run, written down and shared with GitHits:**

1. The item list: each work item's id, objective, scope, and its tag, `needs_lookup` when the work depends on a library or API the agent is unlikely to know well (new, niche, or changed since the model's training), else `routine`.
2. The primary metric (for example fix rounds on `needs_lookup` items).
3. The exclusion rules: the five excluded checks above, and nothing else.
4. The frozen setup: the SyberWork commit, Claude Code version, model, effort, `--allowed-tools`, `--max-turns`, the query guard's thresholds, and the credential lane. Use the `api` lane (`--bare`) in both arms: it is the lane whose cost is billed and known, and in bare mode the tree's `CLAUDE.md` is not read in either arm. The contract prices every model the runs report (`evolution.budget.prices`); a run that used a model the contract does not price has an unknown cost. Anything that learns from runs stays off for the week. AutoSkill is a design (`docs/AUTOSKILL.md`) and is in no proposer path; the `jev` and `empirical` branch policies belong to `--evolve`, which the pilot does not use.

**Each item, each arm, from the same `HEAD`** (accepting moves only the thread's own branch, so `HEAD` stays put):

```bash
syberlabs start "Parse ISO dates with the UTC offset" --paths src tests --unattended
syberlabs experiment label --name githits-pilot --item LIB-1 --arm off --tag needs_lookup
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,mcp__githits
syberlabs run --once                     # one safe step per thread; here, accepting the passing candidate

syberlabs start "Parse ISO dates with the UTC offset" --paths src tests --unattended
syberlabs experiment label --name githits-pilot --item LIB-1 --arm on --tag needs_lookup
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,mcp__githits \
    --mcp-config githits.json            # the only difference between the arms
syberlabs run --once
```

`--claude-code` is the proposer of SyberWork #80 and the query guard of #82; this report works with any proposer, and needs them for the server check and for the lookups to be counted (`arm_unverified` and unknown lookups otherwise). Alternate which arm runs first from item to item, so drift in the model or the network during the week does not fall on one arm. Accept on the host's checks (`--unattended`), or have a person accept without being told the arm: the label is in the journal, not in the diff or the pull request. Publish one arm's accepted change per item; which one does not change the comparison.

**At the end of the week:**

```bash
syberlabs settle                                              # merges, for agent time to merge (SyberWork #79)
syberlabs experiment report --name githits-pilot --delivered review   # read the checks first
syberlabs experiment report --name githits-pilot --delivered review --markdown > pilot.md
syberlabs experiment report --name githits-pilot --delivered review --csv > pilot.csv
```

What leaves the factory is the report and the CSV. The journal, the prompts and the code stay where they are; the record holds lookups as digests only (`docs/CLAUDE_CODE.md`).

## Limits

- The report compares what the record holds. A lookup the proposer did not declare, a model call outside the host, or a person's message to an agent outside the CLI is unknown or missing to it, as it is to `syberlabs metrics` (`docs/METRICS.md`, section 4).
- The server check reads what the proposer recorded about its own MCP servers. It catches a GitHits server that failed to connect; it cannot catch a proposer that misreports.
- One week of items is a small sample. The report says how small, metric by metric; it does not turn a small sample into a finding.
- `examples/githits_ab.py` runs every step with a stand-in instead of a model. Its numbers are invented to exercise the report and say nothing about GitHits.
