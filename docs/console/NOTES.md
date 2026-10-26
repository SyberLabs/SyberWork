# Operator console: UI walkthrough

The browser console served by `syberwork serve` at `http://127.0.0.1:8766/`. It is a client of the
existing case API in `syberwork/server.py`. Every button calls a route that already existed before
this change, except for the read-only routes listed under "Routes added". The server stays the
authority: the console predicts which call will be accepted and explains why one will not, but it
never decides an outcome.

Claim ceiling: **implemented** and **tested** (`tests/test_console.py`, `tests/console_model.test.mjs`,
`tests/console_browser.mjs`). It has not been deployed or used by anyone outside this branch.

## Run it

```sh
python -m syberwork.cli --home .syberwork init          # prints one token per identity
python examples/reference_system.py --database .reference/system.sqlite3
python -m syberwork.cli --home .syberwork serve
```

Open the page, press `i`, and paste the `operator`, `scheduler`, `manager`, `planner` and `admin`
tokens one at a time. They are kept in this tab's `sessionStorage` (as the old console kept one token)
and are sent only as a bearer header to the loopback service. They never appear in the URL. Leave the
tab or choose **Forget all** to drop them.

## The included purchase-order path

| Step | Identity | What you do | What the console shows |
|---|---|---|---|
| 1 | operator | `n`, keep `purchase-order v1` with `P-104` and `2`, **Open case** | The form is generated from the contract's input schema (`string`, `integer`); the exact JSON sent is previewed. |
| 2 | operator | Flow → **Refresh part_number from inventory (P-104)** | The fact chip turns *verified* with the source ETag `inventory-v1` and its age against `max_age_seconds`. |
| 3 | operator | — | **Propose next compiled step** is disabled: *compiled proposer credential required*, with **⇄ switch to scheduler**. |
| 4 | scheduler | Propose `record_review`, then **Execute** (press twice) | Effect *Applied · recorded locally*: a local action touches no external system. |
| 5 | scheduler | **Refresh quote from supplier…** opens Evidence with key and source filled; type `Q-7` | Two verified observations; the required-facts table for `issue_order` is all verified. |
| 5a | scheduler | Optional: Propose (`3`) → By hand, **Check admission** with `{}`, then **Fill from contract bindings** | *If proposed now: denied · amount_required · limits.amount*, then the bound arguments with *needs approval · approval_required:manager*. Nothing is recorded: the case still has one proposal. Editing the arguments marks the answer *out of date*. |
| 6 | scheduler | Propose `issue_order` | Decision *needs approval · approval_required:manager*. **Approve** is disabled for the proposer: *the proposer cannot approve its own proposal*. |
| 7 | manager | **Approve** (press twice) | The armed button names the action, the role, and the exact arguments the approval binds to. |
| 8 | scheduler | **Execute** (press twice) | A real POST to the reference system with `Idempotency-Key` and `If-Match`; *Applied · destination acknowledged HTTP 201*. Only the original proposer can execute; anyone else gets *only the original proposer (scheduler) may execute* with a switch link. |
| 9 | manager | **Sign off as manager** | The sign-off is offered only after `issue_order` has applied, because the server accepts an earlier signature but it never satisfies the clause. The case becomes *Complete*. |
| 10 | any | History (`5`), then `v` | The server's `/verify` result is recorded with the head it covered (`intact through #13 …`). It is marked stale when newer events arrive. |

If the destination is down at step 8, the effect becomes *Outcome unknown · check destination*, the
case list shows *effect unknown*, and the next move is a manager's **Check destination status**. A 404
from the status lookup is shown as *pending · not_found_not_proof* and the outcome stays unknown. Only a
`reconciled` event that passes `verified_reconciliation` (committed record, idempotency key, request
digest, external id, response digest) is drawn as *Reconciled · destination-verified*.

## Where things are

- **Top bar**: views (Cases `g c`, Studio `g s`), cell counters from `GET /metrics/<key>` (open cases,
  effects pending, worker queue in worker mode, denied proposals), the work-item count from
  `GET /metrics/work_items` (with *merged · unknown* once any pull request was opened; an unsettled
  delivery is counted as unknown, never as no; *work items unavailable* with the error code if the read
  fails), and the identity switcher with role chips.
- **Case list**: status per case, plus *effect unknown* and *needs approval* flags. `/` filters,
  `j`/`k` move.
- **Case tabs** (`1`–`8`):
  1. *Flow*: the compiled path as steps, each with its required facts, proposal, decision, deciding
     rule, approvals and effect, and a **Next** banner that names the move and the role that can make it.
     A credential that cannot read the global policy is told so under the banner, with the reader roles
     and a switch to a saved identity that holds one; its approval forecast then comes from the contract
     only.
  2. *Evidence*: required facts per action, refresh from a configured source, observations newest
     first (superseded rows dimmed), and a collapsed, amber **Manual assertion** form. A typed value is
     stored `verified: false` and labelled *unverified*. An Attest receipt is labelled *Attest claim ·
     unverified · not destination proof*. Under the refresh form, each source the case's facts name is
     listed with its read role and whether this credential holds it.
  3. *Propose*: the compiled step, a proposal by hand (origins the credential does not hold are
     disabled), and the external planner. The By hand form starts at the next compiled action and keeps what was
     typed when the page redraws (`r`, or after any action); a browser reload starts it again. **Fill from contract bindings** asks the server to bind the arguments from the newest
     observations, and **Check admission** asks what proposing them would record now (`POST …/preview`); both
     record nothing. The answer shows the decision, the deciding rule, the head and policy version it was computed
     against and when it was read. It turns *out of date* when the form changes, or when a redraw brings in a
     new event (there is no push).
     When `SYBERWORK_PLANNER_URL` is unset the planner panel is an
     empty state naming the variable; when set, **Ask configured planner** is enabled for a `model`
     credential. All proposals with their decisions and actions. An expanded proposal shows its
     deciding rule and, while it has not been executed and was not denied, what admission answers now.
  4. *Effects*: every claimed effect with its outcome, idempotency key, destination response or proof,
     and status checks. Attest observations on the case are called out as reconciling nothing.
  5. *History*: the hash chain, head first. Each row shows its hash; expanded, its predecessor hash,
     its JCS side record (digest, and the deciding rule for a decision) and body. A link-break check runs
     in the page, but only the server's verification counts. Above the timeline: the side-record coverage
     (`GET /api/cases/{id}/side`), this case's work item (`GET /metrics/work_items/{id}`: each metric's
     value, the events it came from and the server's note), and **Witness and certificate**, two disabled
     buttons that name the read route each one lacks.
  6. *Replay*: choose a contract and a policy version. The table shows each changed decision as recorded
     and as replayed, and each acceptance clause before and after. The server's note (no re-read, no
     mutation) is shown verbatim.
  7. *Closure*: resolution tasks (request, verify the source decision, escalate when overdue) and
     cancellation with a required reason.
  8. *Candidates*: shown only for a contract with an `evolution` section.
- **Studio**: published contracts and policies (immutable, with digest and pinned case count),
  compiled artifacts (path, input form schema, action gates, acceptance), a **Draft vN+1** editor
  that validates JSON, refuses an existing version, and lists the changed paths before **Publish**.
  Actions and sources are listed and registered by administrators only. Other credentials see each
  source's read role and whether they hold it, and *admin or manager role required* in place of the policy
  list.

## Visual system

- One neutral surface scale and one ink scale, dark by default and light under
  `prefers-color-scheme: light`. No gradients, no brand accent: the primary button is ink on surface.
- Seven evidence tones, each with a glyph and words so that no state depends on color alone:
  *verified* `✓` (solid), *unverified* `!` (dashed, hatched), *pending* `◷` (dotted), *unknown* `?`,
  *applied* `✓`, *reconciled* `◆` (double border), *denied* `✕`.
- Measured contrast of every text token against both surfaces (WCAG relative luminance): dark theme
  5.9:1 at the lowest (`--ink-3`), light theme 5.0:1 at the lowest (`--verified`). All pass AA for body text.
- Desktop first. Under 860 px the case list stacks above the case and the timeline drops its hash and
  kind columns.

## Confirmation without nagging

Execute, Approve, Sign off, Publish, Register and Cancel arm on the first press: the button turns
orange, reads *Confirm · Execute*, and a note says exactly what will happen (for Execute: which kind of
action, that admission is rechecked first, that the claim is durable before any I/O, and the
arguments). The second press acts. It disarms after six seconds or on `Esc`. There is no modal. Reads,
refreshes and proposals act at once, because they are either read-only or pass through admission.

## Keys

`i` identity (then `1`–`9`) · `/` filter · `j`/`k` next/previous case · `n` new case · `1`–`8` tabs ·
`v` verify history · `r` reload · `g c` cases · `g s` studio · `Esc` disarm or close · `?` help.

## Routes added

All require a bearer token and append nothing (`tests/test_console.py` `test_reads_append_nothing`,
`test_explain_appends_nothing_and_writes_no_row`, `test_side_read_appends_nothing_and_an_unknown_case_is_refused`,
`test_preview_appends_nothing_for_anyone`). All are `GET` except `POST /api/cases/{id}/preview`, which carries
the proposal's arguments in its body and, like the existing `replay` POST, writes nothing. As a POST, its
refusals answer `409`, including a missing token (`unauthorized`).

| Route | Who | Returns |
|---|---|---|
| `/api/console` | any principal | `{planner_configured, effects}`. The planner URL is not disclosed. |
| `/api/contracts` | any principal | `[{id, version, title, compiled_path, digest}]` |
| `/api/contracts/{id}/{version}` | any principal | the stored contract (the same document `GET /api/cases/{id}` already returns for a case) |
| `/api/policies` | `admin` or `manager`, holding no automation role (`model`, `compiled`, `search`); others `403 forbidden` | `[{version, body, digest}]` |
| `/api/actions` | admin; others `403 forbidden` | installed definitions by name. They contain credential variable names, never values. |
| `/api/sources` | admin: installed definitions, as above. Any other principal: read roles only | `{name: {roles}}`. `roles` is what `refresh_fact` checks (`["operator"]` when the definition names none; `null` when the stored value is not a list of names, and the console then says the server decides). No URL, credential variable, value path or kind. |
| `/api/cases/{id}/side` | any principal, like `/api/cases/{id}` | `Work.side_channel` unchanged: `[{seq, jcs, rule}]`, the RFC 8785 SHA-256 of the fields each event's hash covers and, for a decision, the deciding rule id. Events written before the side table have no row. An unknown case answers `unknown_case`. |
| `POST /api/cases/{id}/preview` with `{action, args?, origin?}` | any principal, like `POST /api/cases/{id}/proposals` | `Work.preview_admission`: `status`, `reason`, `rule`, `provenance` (named as the explain route names it), the `args` admitted (`bound: true` when the body had no `args` key and the server bound them from the contract, as the compiled step does; any value present, `null` included, is admitted as given), `origin`, `policy_version`, `head: {seq, hash}`, `written: false` and a note. The origin is checked as propose checks it (`invalid_origin`, `origin_denied`). Binding refuses `missing_fact`, `missing_fact_field`, `invalid_binding` or `action_not_in_contract`. |
| `/api/cases/{id}/proposals/{proposal}/explain?when=recorded\|now` | any principal, like `/api/cases/{id}` | `Work.explain_admission` (`status`, `reason`, `rule`, `provenance: {module, symbol, line}`) and `recorded`: the proposal's first decision as stored, with the rule id written beside it on the side channel (`null` for rows older than that table). `when` defaults to `now`, as in `Work`. |

A malformed version in `/api/contracts/{id}/{version}[/artifacts]` is now `404 not_found` instead of
`500 internal_error`.

`403` is new: a `GET` the principal's roles do not cover (`/api/policies`, `/api/actions`, admin
`/api/sources`) now answers `403 forbidden` (it was `409`). `POST` refusals, such as an operator-only
`POST /api/cases`, still answer `409`, and `/api/builder/*` keeps its own statuses. The console also reads
`GET /metrics/work_items` and `GET /metrics/work_items/{id}`, which already existed (`Work.work_items`,
docs/METRICS.md); no branch in this stack adds a metrics route.

## Completed capabilities and gaps

| # | Asked | State |
|---|---|---|
| 1 | Identity switcher, role affordances | Done. Disabled actions name the missing role or the required actor and offer a saved identity that has it. |
| 2 | Case list and detail, hash-chain timeline, Verify history | Done. Verification runs are kept for the session only; they are not case events. |
| 3 | Contract and connector studio | Done: immutable JSON, compiled artifacts, input form schema, draft and publish of a higher version, register a new action or source name. |
| 4 | Observations with ETag and verified markers | Done. A manual value is always *unverified*. |
| 5 | Propose → execute → approve → execute → sign off | Done and exercised end to end in a browser against the reference system. |
| 6 | Amendment replay with decision diff | Done. Replay is the existing server route, which reads no source and writes nothing. |
| 7 | External planner | Done, with an empty state when unset (`GET /api/console`). Not exercised against a live planner. |
| 8 | Effect and reconciliation status | Done. Attest text is never drawn as reconciled; legacy `reconciled` rows without proof are flagged. |
| 9 | Keyboard, contrast, states, responsive | Done for keyboard, contrast and loading/empty/error/success states. Responsive layout is checked at 820 px only. |
| 10 | Tests | `tests/test_console.py` (routes, static safety, the node projection tests with Python parity, the browser walkthrough). |

Gaps from #95, closed or still open:

| Gap | State | Where |
|---|---|---|
| Deciding rule | **Closed** (#98) | `GET /api/cases/{id}/proposals/{p}/explain`, see below |
| Per-work-item metrics (`/metrics/work_items`) | **Closed**: the top bar reads the summary, and History draws the case's own row (all nine metrics with value, basis events and note) from the existing per-case route | `workItemCounters`, `workItemView` in `app.js`; `M.workItemRows`; `test_work_item_summary_carries_what_the_top_bar_reads`; the History panels render only in the browser walkthrough, which skips without node `playwright` |
| Source read roles for non-admins | **Closed**: every credential reads each source's read roles (only); the refresh button names the missing role and offers a switch; a source that is not installed is said so | `GET /api/sources`; `test_other_credentials_see_each_sources_read_roles_and_nothing_else`, `test_listed_read_roles_are_the_ones_refresh_enforces` |
| `/api/policies` read scope | **Closed**: admin or manager, without an automation role; `model`, `compiled`, `operator` and observer credentials get `403` | `POLICY_READERS` in `server.py`; `ConsoleReadScope` |
| JCS side records (`Work.side_channel`) | **Closed**: a read-only route returns `Work.side_channel` as stored; History shows coverage and each event's digest and rule. The console does not recompute digests | `GET /api/cases/{id}/side`; `M.sideRecords`; `ConsoleSideRecords` |
| Witness and certificate | **Open, shown as missing**: no read path exists. History has two disabled buttons naming `GET /api/cases/{id}/witness` and `GET /api/cases/{id}/certificate` and why neither exists | `witnessPanel` in `app.js`; `test_witness_and_certificate_are_named_as_missing_not_drawn` |
| Admission before proposing | **Closed**: Propose → By hand binds arguments from the contract and asks the server what proposing would record now, before anything is written | `POST /api/cases/{id}/preview` (`Work.preview_admission`); `M.previewView`, `M.previewCurrent`; `ConsolePreview` |
| Builder API (`/api/builder/*`) | Open | a separate surface, not part of this console |
| Push updates | Open | there is no push: press `r` to reload |

- **Deciding rule** (closed): the Flow step and the Propose card read
  `GET /api/cases/{id}/proposals/{p}/explain`. The rule shown is the one the server wrote beside the
  decision (*written with decision #n*). A rule re-derived today stands in only when no rule was written
  (an older row) and the re-derivation reproduces the recorded status and reason; if it does not, the
  console says *unknown* instead of naming a rule. A re-derivation that disagrees with the written rule
  is shown as a warning below it, never in its place. A failed read shows *unavailable* and the error
  code. *If executed now* is never cached: the answer also depends on the policy in force, the clock and
  other cases' reservations, so each render asks again and shows when the server answered. The route
  names the rule's module relative to the installed packages, whatever directory `serve` runs from, and
  withholds a module outside them, so it never discloses the host's filesystem layout.
- **Policy read scope** (closed): an economic policy carries budget limits, so `GET /api/policies`
  answers `admin` or `manager` credentials that hold no automation role (`AUTOMATION_ROLES` in
  `syberlabs/evolution.py`, the same exclusion promotion and evaluation already apply). The code has no
  policy-read role, so none was invented. `manager` is included because the console reads the policy's
  `approval_role` to forecast approvals, and the manager is the approver. `operator` is not: an operator,
  scheduler or planner credential sees the Flow note and an approval forecast from the contract alone.
  Admission is unchanged and still applies the policy for everyone. Replay then offers the policy versions
  recorded in the case's decisions. `model.js` mirrors `POLICY_READERS` and `AUTOMATION_ROLES` only to
  name the role and suggest an identity; a parity test checks both against Python.
- **Source read roles** (closed): non-admins get `{name: {roles}}` from `GET /api/sources`. The roles
  are authorization metadata the server enforces anyway; contracts already name the sources. URLs,
  credential variable names, value paths and timeouts stay admin only. `/api/actions` stays admin only.
- **Work items** (closed for the top bar): `delivered` in docs/METRICS.md means a merged pull request,
  so for purchase-order cases the console shows only the item count, with opened, cancelled and cost
  basis in the tooltip. The *merged · unknown* counter appears once a pull request was opened or a merge
  is recorded.
  There is still no scalar `work_items.*` key under `GET /metrics/<key>` (`spec/GAPS.md` #52): the
  console reads the `{items, summary}` route instead, and no storage or key was added for it.
- **JCS side records** (closed): `GET /api/cases/{id}/side` returns `Work.side_channel` as stored, under
  the same read scope as `GET /api/cases/{id}` (the digests can be computed from the events, and the rule
  ids are already in the explain route). History shows *n of n events carry a side record* and *n of n
  decisions name their deciding rule*, and lists the events with no row. It does not call those legacy.
  `verify` (`Work.chain_ok`) compares a row's digest with its event only when the digest is a string. It
  does not notice a missing row or a row whose digest is null, and no check reads the rule id. So the
  console says *written before the side table existed, or the row is absent*, describes a null digest as
  *not checked by verify*, and labels a rule *as written, in no hash*. An expanded event shows its digest,
  marked *same as the chain hash* when the canonical and RFC 8785 bytes agree. In the included path, the
  supplier quote and the `issue_order` proposal give different bytes, so their digests differ from the
  chain hash. The console lists such events and says only `verify` can tell an encoding difference from a
  changed digest. A changed row comes back from the route unchanged. `verify` refuses a digest changed to
  another string, and accepts a nulled digest or a changed rule
  (`test_the_route_reports_a_stored_digest_as_stored_and_verify_judges_it`).
- **Witness and certificate** (open, shown as missing): `syberwork serve` configures no witness.
  `Work.submit_witness` asks a loopback `python -m syberlabs.witness` process to sign the chain head and
  returns the DSSE envelope to its caller; nothing stores it in the case database, and no HTTP route reads
  it or the transparency log. Admission certificates (`syberlabs certify`, docs/CERTIFICATE.md) belong to
  Build Threads and are files under `<home>/certificates`; a case here has none. So the console draws
  nothing as witnessed or certified. It shows two disabled buttons, *Show witness* and *Show certificate*,
  each naming the GET it would need. The routes are not served (`404 not_found`). Closing this needs a
  stored witness envelope, or a read-only log reader configured in `serve`, before any route can exist.
- **Work item per case** (closed): History reads `GET /metrics/work_items/{id}` (an existing route) and
  shows each of the nine metrics (`syberlabs.metrics.METRICS`, with a parity test) as `_cell` writes it.
  *unknown* stays a badge, a boolean is yes or no, cost has four decimals. One exception: a whole-number
  float such as `2.0` reads as `2`, because JSON does not keep the difference. Each row also lists the events
  it was computed from and the server's note. For a purchase order the cost and turns are *unknown* (no
  metered model call) and delivered is *no · no pull request opened*, as the metrics define it.
- **Admission before proposing** (closed): a hand proposal is admitted as it is written, so before this a
  mistyped argument left a permanent denied decision on the case history (an empty `{}` for `issue_order`
  records *amount_required*), and the arguments had to match the bound facts exactly or
  `bindings.arguments` refused them. `POST /api/cases/{id}/preview` runs the same ordered rules as
  `propose`, on the same history (the events before the proposal), policy in force and clock, with a fresh
  proposal id, and writes nothing: no event, no side row, no reservation (full database dump compared). A test
  previews and then proposes at once across five states (no facts, wrong role, compiled origin, approval
  needed, amount over the limit) and checks that the recorded decision and the written rule equal the
  preview. Another case with an economic action shows that the preview counts the budget other cases have
  reserved and reserves none itself. The preview is **not a promise**: proposing admits again, and the
  answer can change when the case, the policy, the clock (a fact turning stale) or another case's
  reservation moves first. So the console shows the head and the policy version it was computed against,
  and when it was read. It marks the answer *out of date* when a new event arrives or the form changes, and
  it never carries a preview into the proposal. With no `args`, the server binds them from the contract
  (`bind_arguments`, the same function the compiled step uses). That is why **Fill from contract bindings**
  is offered to every credential, not only `compiled`; a binding that needs an absent fact answers
  `missing_fact`, and the console points to Evidence. A contract whose `arguments` map a parameter to anything
  but a string used to crash the compiled step with a 500. `bind_arguments` now refuses it as `invalid_binding`,
  for the compiled step and the preview alike (`test_a_contract_binding_that_is_not_a_string_is_refused_not_crashed`).
  The route is open to any principal, as `propose` is. The arguments it binds come from events
  `GET /api/cases/{id}` already returns.
  **Open decision: probing without a record.** A proposal can reveal the same facts the preview does: the
  policy's `max_amount` (`limits.amount`) and, for an economic action, how much of a shared budget other cases
  hold. Before this route, each probe left a denied decision on the case history. A preview leaves none.
  Each preview is counted (`admission_preview` in `Work.metrics`) and written to the trace when one is
  configured (`syberwork/trace.py`), with the actor, action, status, reason and rule. The case history holds
  nothing. Replay does not cover this: it re-decides only proposals already recorded. Policy bodies are
  restricted to admin and manager because they carry budget limits. If a recordless probe of those limits is
  unacceptable, the route could answer only credentials that hold the action's role, or be rate-limited.
  Neither is done here. It is a `POST` because the arguments are a JSON body. The Flow banner does not call it: the
  compiled step is proposed with exactly the bound arguments, and its forecast already comes from
  `model.js`.
- The **Builder API** and **push updates** remain open as listed. No backend for them was added.
- **Still readable by any principal, unchanged here**: contract budgets (`evolution.budget`, `prices`)
  through `GET /api/contracts/{id}/{v}` and `GET /api/cases/{id}`, and `POST /api/cases/{id}/replay`
  with any `policy_version`, which answers how that version would decide (decisions and reasons, not
  the policy body). Narrowing them is a separate decision.
- **Attest detection** is a display rule (value `schema` starting with `attest.`, or a source named
  like `attest`). It only adds a warning label; it cannot make anything verified.
- **Case statuses** in the list come from one `GET /api/cases/{id}` per case, four at a time, capped
  at 60 cases.
- The README's API list does not mention the new read routes. README wording is a public claim
  (Class P in `AGENTS.md`), so that line is left for a human key.
