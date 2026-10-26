# Two-arm GitHits operator handoff

Claim ceiling: implemented and tested offline. No live pilot, provider call,
real GitHits lookup, spend or comparative result is established by these tests.
Mateo owns credentials, provider selection and spending. The draft integration
pull request and its delivery handoff identify the exact tested commit; approve
that commit before a run. A later source change requires fresh checks.

## Candidate and prerequisites

The candidate combines #136, #139 and #142 as one accounting boundary, #141,
#143 and #140, and reconciles #138's output behavior. #139 already includes
#142 through `40d98d0`; its final `1cab541` documentation change is retained.
#143 includes #141 through `d10fc3d`. #138's original history repeats changes
already on main, so only `49c8aca` was applied and its conflicts reconciled.
The Windows writer now uses the existing journal lock around open, append,
flush and sync. Interrupted fragments remain ignored; no record is invented.
Synthetic gaps and unreconciled records remain unknown, including `no_change`.
The report keeps attempt counts distinct from guard-passed use. Mixed routes
are counted and still flagged `two_routes`.

SyberEval is not a dependency: this runner imports `experiment`, whose existing
`delivery_rule` requires lower median paired cost and at least as many
deliveries. No eval redesign, third arm, cache or dashboard is required.

Required before spending:

- Approval naming provider, exact model/route, run plan, spending authority,
  scheduling budget and treatment of search fees and overshoot.
- Python 3.11+; Git; Claude Code 2.1.295+ on PATH (or the approved
  `SYBERLABS_CLAUDE_BIN` executable); Git Bash on Windows.
- A plain temporary path without spaces or shell punctuation for launchers.
- The exact approved candidate checked out cleanly. Do not reuse another
  worker's checkout or branch, and do not commit the plan or run directory.
- A plan with 3–5 real work items: unique IDs, tags, objectives and allowed
  paths. Freeze it before either arm. Price the exact model at all four
  nonzero token rates: input, output, cache read and cache creation.
- Mateo supplies credentials in the process environment through his approved
  credential mechanism. Do not put credentials in command history, files,
  arguments, commits or shared output. Use an alias actor such as `mateo`.
- The API lane must pass its routing-switch checks. A subscription, proxy or
  provider-managed session is not evidence of the approved priced API lane.

## Safe preparation (no credentials or paid calls needed)

Use the exact SHA from the delivery handoff for `$pin`:

```powershell
git clone https://github.com/SyberLabs/SyberWork.git SyberWork-githits-operator
Set-Location SyberWork-githits-operator
git fetch origin codex/githits-two-arm-integrated
git switch --detach $pin
if ((git rev-parse HEAD) -ne $pin) { throw 'Wrong candidate' }
if (git status --porcelain) { throw 'Checkout must be clean' }
$env:PYTHONPATH = '.'
python scripts/pilot_dry_run.py --template > ..\githits-plan.json
python scripts/pilot_dry_run.py --help
```

Mateo completes `..\githits-plan.json`; template zeros and example objectives
are deliberately unusable. Record its SHA-256 with `Get-FileHash`, and retain
the approved plan privately. The final delivery supplies focused and aggregate
test commands/results on the pinned head. Do not infer approval from green CI.

## Approved live sequence (Mateo executes)

Only after explicit run/spend approval and environment-only credential setup:

```powershell
$pin = git rev-parse HEAD  # must equal the approved SHA recorded above
python scripts/pilot_dry_run.py --repo . --base $pin --plan ..\githits-plan.json --budget 8 --seed 7 --actor mateo
$status = $LASTEXITCODE
if ($status -ne 0) { throw "Dry run did not pass; exit $status. Inspect locally and stop." }
```

Keep the interactive confirmation enabled. Preflight checks the repository,
version, credential lane, token handling, doctor, guard mechanism, budgets and
contracts. Doctor contacts GitHits once. After confirmation the runner performs
the live canary before any paired item: one content-rule block and one allowed
lookup answered successfully by GitHits, authentic guard decisions, no
unreconciled lookup. An inconclusive or failed canary stops the schedule.
There is no separate canary-only switch; the single command enforces this order.

If the canary passes, the frozen seed schedules both arms of every item. The
baseline has no GitHits route or credential; treatment uses the API launcher.
The same checks, objectives, base, prices and call/turn limits apply to each arm.
A reviewer who did not watch the runs supplies pass/fail/skip on the accepted
diffs. A skipped review is unknown. No change is merged or deployed by this run.

Expected outputs under `.syberlabs/pilot-dry-run/<UTC-time>-7/`:

- `receipt.json`: final verdict, pinned host/base, plan/objective digests,
  versions, schedule, guard/canary proof, metric provenance, report and scan.
- `report.json`, `report.md`, `metrics.json`: paired results and unknowns.
- Private local journal, exports, contracts and `commands.log`. These may hold
  objectives and code; do not publish or send them automatically.

Exit 0 means the final receipt and printed verdict are PASS; 1 means failed or
aborted; 2 means refused before spending. PASS proves the measurement path
worked for that run. It does not mean the delivery rule was met or a pilot
succeeded. Review `receipt.json` and `report.md` locally before sharing them.

## Budget and stop conditions

`--budget 8` is a token-cost scheduling budget, **not a hard all-in $8 cap**.
The host checks spend between calls; one call can overshoot. Server-side search
fees are excluded. Failed or timed-out provider calls can have unmetered usage;
the runner marks spend unknown and stops, rather than treating it as free.
Approval must cover those limits; a strict all-in cap requires a separate
provider-enforced limit and fee accounting established by Mateo.

Stop on any failed/inconclusive preflight or canary; unreadable or leaked
output; unknown spend or required metric; missing/partial/unguarded lookup
evidence; a moved base; an unpriced model; an excluded or missing pair; or a
`two_routes` warning. Keep receipts private. Do not rerun only the losing arm,
erase failed items, reinterpret unknown as zero, or add fabricated records.
Any canary retry consumes money and needs to remain within explicit approval.
Report the precise failure and request a bounded correction before resuming.
