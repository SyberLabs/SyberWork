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

To read a witness's log, start the witness first and give `serve` its log and public key. The witness
prints `{"public": "<hex>", "port": <n>}`; both variables or neither, or `serve` stops:

```sh
python -m syberlabs.witness --log .witness/log.sqlite3     # keep it running; it holds the signing seed
SYBERWORK_WITNESS_LOG=.witness/log.sqlite3 SYBERWORK_WITNESS_PUBLIC=<hex> python -m syberwork.cli --home .syberwork serve
```

To let the console ask that witness to sign, also give `serve` the witness's `port` and the one role whose holders may
ask. Both or neither, and only beside the log and key; `serve` stops otherwise. No role is chosen by default:

```sh
SYBERWORK_WITNESS_LOG=.witness/log.sqlite3 SYBERWORK_WITNESS_PUBLIC=<hex> \
SYBERWORK_WITNESS_PORT=<n> SYBERWORK_WITNESS_ROLE=manager python -m syberwork.cli --home .syberwork serve
```

To list Build Thread admission certificates in Studio, give `serve` the Build Thread home that `syberlabs certify` wrote
under and the signer's public key: the 64 hex characters `certify` printed and wrote to the `.pub` file. Both or neither, or `serve` stops.
Take the key from the signer's own record, kept apart from the folder. Do not copy it from a `.pub` file in the folder
`serve` reads: anyone who can write a certificate there can write a `.pub` beside it.

```sh
SYBERWORK_CERTIFICATE_HOME=../project/.syberlabs SYBERWORK_CERTIFICATE_PUBLIC=<signer key, 64 hex> \
python -m syberwork.cli --home .syberwork serve
```

To rotate the signing key, list the new key and the old ones, separated by commas (at most 8, each once), and restart
`serve`. Certificates signed under either key are read, and each signed row names the key that verified it. Nothing is
revoked: a listed key is trusted until it is taken off the list.

```sh
SYBERWORK_CERTIFICATE_HOME=../project/.syberlabs SYBERWORK_CERTIFICATE_PUBLIC=<new key>,<old key> \
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
| 11 | manager | History → Witness → **Ask witness to sign** (press twice) | Before: *not witnessed*. Another credential sees the button disabled with *manager role required to ask the witness* and **⇄ switch to manager** (the role is `SYBERWORK_WITNESS_ROLE`). The armed note says the witness appends a leaf that cannot be removed and nothing is written to the case. After: *Witness signed #1–#13*, the panel reads *witnessed through #13* with the leaf, its signed head and *in the log*, and the button is disabled: *a signed head in the log already covers #1–#13*. An event added later is listed as *not witnessed*, and asking again signs the longer history. Without a configured log, **Witness** reads *not configured* and names the variables; a script can still call `Work.submit_witness`. |

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
     value, the events it came from and the server's note), **Witness** (`GET /api/cases/{id}/witness`: each
     leaf of the witness's log that names this case, the events it covers and the server's verdict, and
     **Ask witness to sign**, `POST /api/cases/{id}/witness`, for the role `serve` was given) and
     **Certificate**, which says a case has none (certificates are about Build Threads) and, when `serve` reads a
     certificate home, offers **Open certificates in Studio**.
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
  list. Under **Build Threads**, **Certificates** lists the admission certificates `serve` reads
  (`GET /api/certificates`), each with the verdict `serve` reached under the configured keys: *certified*, *signed ·
  journal not checked*, *journal differs*, *journal unreadable* or *not certified*, with the thread, candidate, result commit, contract,
  checks and certified chain prefix of each signed one, and the key that verified it (*key 2 · d75a…* when `serve`
  trusts more than one).

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

Execute, Approve, Sign off, Publish, Register, Cancel and Ask witness to sign arm on the first press: the button turns
orange, reads *Confirm · Execute*, and a note says exactly what will happen (for Execute: which kind of
action, that admission is rechecked first, that the claim is durable before any I/O, and the
arguments). The second press acts. It disarms after six seconds or on `Esc`. There is no modal. Reads,
refreshes and proposals act at once, because they are either read-only or pass through admission.

## Keys

`i` identity (then `1`–`9`) · `/` filter · `j`/`k` next/previous case · `n` new case · `1`–`8` tabs ·
`v` verify history · `r` reload · `g c` cases · `g s` studio · `Esc` disarm or close · `?` help.

## Routes added

All require a bearer token and append nothing to the case (`tests/test_console.py` `test_reads_append_nothing`,
`test_explain_appends_nothing_and_writes_no_row`, `test_side_read_appends_nothing_and_an_unknown_case_is_refused`,
`test_preview_appends_nothing_for_anyone`, `test_the_read_appends_nothing_to_the_case_or_the_log`,
`test_only_the_configured_role_may_ask_and_the_case_gets_nothing`, `ConsoleCertificates.test_the_read_writes_nothing`).
All are `GET` except two POSTs.
`POST /api/cases/{id}/preview` carries the proposal's arguments in its body and, like the existing `replay` POST,
writes nothing. `POST /api/cases/{id}/witness` is the one route here with an effect, and it is outside this
database: the witness appends a leaf to its own log. As POSTs, their refusals answer `409`, including a missing
token (`unauthorized`) and a role the caller lacks (`forbidden`).

| Route | Who | Returns |
|---|---|---|
| `/api/console` | any principal | `{planner_configured, effects, witness: {log, request_role}, certificates: {configured}}`. The planner URL, the witness port and the certificate home are not disclosed; `request_role` is the role `POST …/witness` requires, or `null` when `serve` has no witness to ask. |
| `/api/certificates` | any principal | Without a configured home: `{configured: false, note}`. With one: `{configured, public_keys, total, shown, checked: ["signature", "journal"], not_checked: ["repository"], certificates, note}`, one row per `*.json` under `<home>/certificates`, at most 50 in name order: `{file, verdict, reason, signature, journal, journal_present, repository: null, statement}`. `verdict` and the layer states are `syberlabs.certificate.verify_certificate`'s, run by `serve` with the home as the journal home, under the keys in `SYBERWORK_CERTIFICATE_PUBLIC` (`public_keys`, in that order) that a signature in the file could match. `statement` (thread, objective, candidate, contract, policy version, base and result commits, target ref, admission actor, approvers and decision, checks, chain prefix, `issued_at`, `signing`, form and key id: the configured key that verified it) is present only when the signature layer passed. A link, a folder or an unreadable file is refused (`not_a_regular_file`, `certificate_unreadable`); a file the verifier raises on (a deeply nested one raises `RecursionError`) is `unverifiable`. A thread journal or `registry.jsonl` that is not a regular file, is over 64 MiB, or cannot be read fails a signed row as `journal_unreadable:not_a_regular_file`, `:too_large` or `:io_error`, and the verifier does not read it. No layer note is returned, so no host path is disclosed. |
| `/api/contracts` | any principal | `[{id, version, title, compiled_path, digest}]` |
| `/api/contracts/{id}/{version}` | any principal | the stored contract (the same document `GET /api/cases/{id}` already returns for a case) |
| `/api/policies` | `admin` or `manager`, holding no automation role (`model`, `compiled`, `search`); others `403 forbidden` | `[{version, body, digest}]` |
| `/api/actions` | admin; others `403 forbidden` | installed definitions by name. They contain credential variable names, never values. |
| `/api/sources` | admin: installed definitions, as above. Any other principal: read roles only | `{name: {roles}}`. `roles` is what `refresh_fact` checks (`["operator"]` when the definition names none; `null` when the stored value is not a list of names, and the console then says the server decides). No URL, credential variable, value path or kind. |
| `/api/cases/{id}/side` | any principal, like `/api/cases/{id}` | `Work.side_channel` unchanged: `[{seq, jcs, rule}]`, the RFC 8785 SHA-256 of the fields each event's hash covers and, for a decision, the deciding rule id. Events written before the side table have no row. An unknown case answers `unknown_case`. |
| `POST /api/cases/{id}/preview` with `{action, args?, origin?}` | any principal, like `POST /api/cases/{id}/proposals` | `Work.preview_admission`: `status`, `reason`, `rule`, `provenance` (named as the explain route names it), the `args` admitted (`bound: true` when the body had no `args` key and the server bound them from the contract, as the compiled step does; any value present, `null` included, is admitted as given), `origin`, `policy_version`, `head: {seq, hash}`, `written: false` and a note. The origin is checked as propose checks it (`invalid_origin`, `origin_denied`). Binding refuses `missing_fact`, `missing_fact_field`, `invalid_binding` or `action_not_in_contract`. |
| `/api/cases/{id}/witness` | any principal, like `/api/cases/{id}` | `Work.witness_status`. Without a log: `{case_id, configured: false, count, head, note}`. With one: also `public_key`, `checkpoint` (the newest signed checkpoint covers the log as read), `witnessed_through`, and `entries: [{index, count, head, verdict}]`, one per leaf naming this case. Verdicts: `included`, `not_included`, `head_differs`, `chain_broken`, `beyond_history`, `bad_signature`, `malformed`. `witnessed_through` is the largest `included` count above zero, and `null` while any leaf is `head_differs`, `chain_broken` or `beyond_history`. An unknown case answers `unknown_case`. |
| `POST /api/cases/{id}/witness` with `{}` | holders of `SYBERWORK_WITNESS_ROLE` that hold no automation role; others `forbidden`. Without that configuration, everyone gets `witness_not_configured` | `Work.request_witness`: `{case_id, count, head, envelope, written: false, note}`, the head the witness signed and its DSSE envelope, after `WitnessClient.submit` checked the signature against the events sent and the log's checkpoint covering it. Refusals: `unknown_case`, `witness_conflict` (the log holds a `head_differs`, `chain_broken` or `beyond_history` leaf for this case), `already_witnessed` (a covered head already names every event), `invalid_chain` (this server's own `verify` fails), `witness_unavailable` (nothing answered, the answer was unreadable, or none came in time), and any refusal the witness returns. |
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
| 10 | Tests | `tests/test_console.py` (routes, static safety, the node projection tests with Python parity, the browser walkthrough, which also has the manager ask the witness to sign the completed case and reads it back, then opens the Build Thread certificates from History). |

Gaps from #95, closed or still open:

| Gap | State | Where |
|---|---|---|
| Deciding rule | **Closed** (#98) | `GET /api/cases/{id}/proposals/{p}/explain`, see below |
| Per-work-item metrics (`/metrics/work_items`) | **Closed**: the top bar reads the summary, and History draws the case's own row (all nine metrics with value, basis events and note) from the existing per-case route | `workItemCounters`, `workItemView` in `app.js`; `M.workItemRows`; `test_work_item_summary_carries_what_the_top_bar_reads`; the History panels render only in the browser walkthrough, which skips without node `playwright` |
| Source read roles for non-admins | **Closed**: every credential reads each source's read roles (only); the refresh button names the missing role and offers a switch; a source that is not installed is said so | `GET /api/sources`; `test_other_credentials_see_each_sources_read_roles_and_nothing_else`, `test_listed_read_roles_are_the_ones_refresh_enforces` |
| `/api/policies` read scope | **Closed**: admin or manager, without an automation role; `model`, `compiled`, `operator` and observer credentials get `403` | `POLICY_READERS` in `server.py`; `ConsoleReadScope` |
| JCS side records (`Work.side_channel`) | **Closed**: a read-only route returns `Work.side_channel` as stored; History shows coverage and each event's digest and rule. The console does not recompute digests | `GET /api/cases/{id}/side`; `M.sideRecords`; `ConsoleSideRecords` |
| Witness | **Closed for reading** (#115): `serve` opens the witness's log read-only when `SYBERWORK_WITNESS_LOG` and `SYBERWORK_WITNESS_PUBLIC` name it, and History draws what the server judged. **Closed for asking** (this branch): with `SYBERWORK_WITNESS_PORT` and `SYBERWORK_WITNESS_ROLE` also set, **Ask witness to sign** has `serve` send the history to the witness, for that role only (below) | `GET` and `POST /api/cases/{id}/witness` (`Work.witness_status`, `Work.request_witness`); `M.witnessView`, `can(…, 'witness_request')`; `ConsoleWitness`, `ConsoleWitnessRequest`; the browser walkthrough |
| Certificate | **Closed for reading Build Thread certificates** (#121): with `SYBERWORK_CERTIFICATE_HOME` and `SYBERWORK_CERTIFICATE_PUBLIC`, `serve` verifies each file under `<home>/certificates` (signature, then the journal) and Studio → Certificates draws the verdicts. **Several keys** (this branch): the variable may list up to 8 keys for a rotation, and each signed row names the key that verified it. **A case still has none**: History says so and links to Studio. The repository layer is not run by `serve`. Nothing is revoked | `GET /api/certificates` (`syberwork/certificates.py`); `M.certificateView`; `studioCertificates`, `witnessPanel` in `app.js`; `ConsoleCertificates`, `ConsoleCertificateKeys`, `test_a_case_has_no_certificate_route`, the browser walkthrough |
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
- **Witness** (closed for reading): before this, `serve` configured no witness. `Work.submit_witness` asked a
  loopback `python -m syberlabs.witness` process to sign the chain head and returned the DSSE envelope to its
  caller, and nothing read the log back. Now `syberwork serve` opens the witness's transparency log with
  `TransparencyLog.open_readonly` when `SYBERWORK_WITNESS_LOG` and `SYBERWORK_WITNESS_PUBLIC` (the hex the witness
  prints) are both set. Setting only one, or a key that is not 32 bytes of hex, stops `serve`. So does a path that is the case
  database or not a log. The reader has no seed and cannot append. `GET /api/cases/{id}/witness` returns
  `Work.witness_status`: each leaf whose payload names this case, judged against the events as they are now. A
  signed head covers events #1 to its `count`, so it stays `included` when later events arrive, and those later
  events are listed as *not witnessed*. `head_differs` (event `count` has another hash) and `beyond_history` (the
  witness signed more events than the case holds) mean this history is not the one that was witnessed. So does
  `chain_broken`: the signed head equals the hash stored on event `count`, but events #1 to `count` no longer
  hash to it (`verify_events` on that prefix, the check the witness ran before signing). Without it, a body
  rewritten after signing still read *included* (`test_a_history_changed_after_signing_is_not_witnessed`). Any of
  the three makes `witnessed_through` `null`, and the console reads *history differs*, even when another leaf is
  `included`. `bad_signature` is a leaf the configured key did not sign. `malformed` is one it signed that is
  not a chain head. `not_included` is a matching head when the newest signed checkpoint does not cover the log
  as read. Leaves and checkpoint are read in one transaction, so a concurrent append cannot split them. An
  empty log with no checkpoint counts as covered. A leaf body that is not JSON text is skipped and makes the
  checkpoint not cover the log, instead of answering 500 (`test_an_unreadable_leaf_is_skipped_not_a_crash`).
  - **Who judges.** The server checks every signature, the checkpoint and the Merkle root under the configured
    public key; the console checks none. It does refuse an `included` answer whose head is not the hash it shows
    at that event (*unknown · press r*). It counts only `included` heads at an event it holds. A head past the
    events on the page, which the server read later, is not drawn as witnessing them; the panel says the server
    read more events (`r`). The cache key is the case and its head `seq`, not its hash, so a history rewritten at
    the same length keeps the earlier answer until `r`. So the panel is only as independent as the process that serves it and
    the public key it was given. A reader who doubts the server checks the same log with
    `TransparencyLog.verify_witnessed` under the witness key. The known limits in `docs/SDK_REDTEAM.md` row 4
    still apply: a same-user process can open the log file writable, an operator who holds the seed can rebuild
    the log, and this is not Sigstore Rekor.
  - **Selection.** Leaves are picked by the `case_id` in their unsigned payload; every verdict is then reached
    under the key. A leaf of another case is not verified, so a case's read costs one Ed25519 check per leaf
    naming it plus one for the checkpoint. On the machine this branch was built on, the pure-Python verify took
    about 0.14 s per signature (one measurement, not a benchmark). To keep that cost off every redraw, the
    console keeps an answer per case and head, and `r` asks again, because the log moves without the case.
  - **SDK change.** `syberlabs/tlog.py` gains `TransparencyLog.entries()` (every leaf and whether the newest
    signed checkpoint covers them, in one read transaction, rolled back if a read fails). The checkpoint check
    moved into `_covers`, which `verify_witnessed` now shares. One difference: a checkpoint row that is not text
    now returns False where it raised `TypeError`. `tests/test_witness.py` and `tests/test_external_witness.py`
    are unchanged and pass.
  - **Independent review.** A Claude session that did not write the change reviewed it and found that a
    history changed after signing still read *included*. That is now `chain_broken`. Its other findings are
    also fixed. `witnessed_through` now reports nothing while a leaf conflicts, and was 0 for an empty head.
    An unreadable leaf no longer answers 500. A failed read now rolls back its transaction. The console counts
    only heads at events it holds. The browser test's signing assertion could not fail; it now can. The reviewer
    also ran 60 appends from another process against about 90,000 reads. No read saw the leaves and checkpoint
    out of step, none hit a lock error, and none left a transaction open.
  - **Asking from the console** (closed on this branch; it was *still open* in #115). #115 left it open because
    `Work.submit_witness` needs the witness's port and nothing in the code says which role may ask. This branch
    still does not choose that role in code. `serve` takes it from `SYBERWORK_WITNESS_ROLE`, beside
    `SYBERWORK_WITNESS_PORT` (the `port` the witness prints): both or neither, and only with the log and key, or
    `serve` stops (`_witness_request` in `syberwork/cli.py`; `make_server` refuses the same half configurations).
    It is one role name, matched exactly against the caller's roles, and a caller that also holds an automation role
    (`model`, `compiled`, `search`) is refused, as for policy bodies; an automation role cannot be configured. The
    rationale is the precedent of
    `SYBERWORK_PLANNER_URL`: an operator setting of the deployment, with the console naming the variables while it
    is unset. The walkthrough and its tests use `manager`, the role that signs off the purchase order. That is a test
    choice, not a recommendation.
    - **What a request does.** `POST /api/cases/{id}/witness` runs `Work.request_witness`. It reads the events once,
      in one transaction, and uses that one read for everything: it judges the log against it as the read route
      does, runs `verify` (side digests included) on it, and sends exactly those events. An append between the
      check and the send is neither judged nor signed (`test_the_events_judged_are_the_events_sent`). It refuses
      `witness_conflict` when any leaf for this case is `head_differs`, `chain_broken` or `beyond_history`: the
      witness checks only the links it is sent, so it would sign a changed history beside the head it signed
      before. It refuses `already_witnessed` when a covered head already names every event, and `invalid_chain`
      when `verify` fails. `WitnessClient.submit` accepts the envelope only if it verifies over the events sent and
      the log's newest checkpoint covers it. `witness_unavailable` replaces a `500` in three cases, each with its
      own detail: nothing listens on the port; the answer is unreadable; or no answer came within the client's
      30 s. In the last case the witness may still have signed, and the detail says to read the log again (the
      console does). `Work.submit_witness` is unchanged and still serves scripts. Requests run one at a time in a process, so four
      presses at once append one head (`test_presses_at_once_append_one_head`). Two `serve` processes over one
      database could still both append the same head; that costs a duplicate leaf, not a wrong verdict.
    - **Nothing is written to the case.** The test compares a full dump of the case database before and after. The
      witness appends one leaf and one checkpoint to its own log. A leaf cannot be removed through any API, so the
      button arms first and its note says so.
    - **Who asked.** The log records no requester. Each request that reaches `Work` is counted
      (`witness_request` in `Work.metrics`) and written to the trace when one is configured. Each record has the
      case, the actor's name, its `principal_id` for a database principal (`null` for a `users.json` identity) and
      the outcome: `signed` with count and head, or the refusal code, `unknown_case` included. A role refusal is
      answered by the server before `Work` and is not traced, like the other role checks in `server.py`.
    - **The console.** It forecasts with `can(me, 'witness_request', {console, view})`: disabled with the variables
      named when `/api/console` gives no `request_role`; disabled for another role with a switch to an identity
      that holds it; disabled while the last read shows *history differs* or a head covering every event shown. When
      the read failed or the server read more events than the page shows, it lets the server decide. The POST goes
      to the case being drawn, not the selection, as the preview does. Afterwards `refreshAll` drops every cached
      witness read and reads again, whether the request succeeded or not.
    - **Independent review.** A Claude session that did not write the change reviewed it and found no blocking
      defect. All five of its findings are fixed:
      - The log was judged on one read of the events and a later read was sent, so a rewrite between the two
        could be signed unchecked. There is now one read.
      - `unknown_case` and an unreadable witness answer were not traced. The second also came back as a generic
        `invalid_request`.
      - A timeout was reported as "nothing answered", though the witness may have signed.
      - Automation credentials could ask, and the trace did not name the principal.
      - `make_server` accepted a client whose key differs from the log's.
    - **Not done.** The console still verifies no signature itself. No rate limit applies beyond one request at a
      time and the `already_witnessed` refusal: a holder of the role can ask once per new event. The witness's own
      protocol, `syberlabs/witness.py`, `syberlabs/tlog.py` and `spec/` are unchanged.
- **Certificate** (closed for reading Build Thread certificates in #121, under several keys on this branch; it was
  *open, shown as missing* in #118). Admission certificates (`syberlabs certify`, docs/CERTIFICATE.md) belong to Build Threads and are files under
  `<home>/certificates`. A SyberWork case is not a Build Thread, so there is still no per-case route
  (`GET /api/cases/{id}/certificate` stays `404 not_found`) and History draws nothing on a case as certified. It now
  says why, and **Open certificates in Studio** goes to the list when `serve` reads one. #118 named the per-case route as
  the missing piece. Building that would have meant inventing a link between a case and a thread that the code does
  not have, so #121 added a listing instead.
  - **Configuration.** `syberwork serve` takes `SYBERWORK_CERTIFICATE_HOME` (the Build Thread home, `.syberlabs`) and
    `SYBERWORK_CERTIFICATE_PUBLIC` (the 64 hex characters `certify` writes to the `.pub` file). It needs both or neither.
    A key that is not 32 bytes of hex, or a home that is not a directory, stops `serve` (`_certificates` in
    `syberwork/cli.py`). Nothing in the code chooses the key. Since this branch the variable may list several keys
    (below). The `.pub` file beside each certificate is never read:
    whoever can write the folder can write it too, so a certificate signed under another key fails even when its own
    `.pub` matches it (`test_another_key_or_changed_bytes_fail_and_state_nothing`).
  - **What serve checks.** `CertificateShelf.listing` (`syberwork/certificates.py`) runs
    `syberlabs.certificate.verify_certificate(bytes, key, home=<home>)` on each file, as `syberlabs verify FILE --public
    KEY` would with the journal layer added. The signature layer covers the size, the `payloadType`, the Ed25519
    signature over the DSSE PAE, the statement wrapper, the strict payload shape, the subject and the canonical bytes.
    The journal layer runs when `<home>/journal/threads/<thread>.jsonl` exists, and checks the certified prefix, its
    head and `claims()` field by field. The **repository layer is not run**: it needs Git and the project's working
    tree, and `serve` runs no Git. Every answer says so (`not_checked: ["repository"]`), and the console names
    `syberlabs verify FILE --public KEY --repo .` for it. No layer note is returned, because those notes carry host
    paths.
  - **What the console draws** (`M.certificateView`). *certified* only for `verdict: pass`, `signature: ok`,
    `journal: ok`, `journal_present: true` and a statement of the expected shape. *signed · journal not checked* for a
    pass with no journal under the home, as `verify` prints `pass (journal not checked)`. *journal differs* for a signed
    file whose journal layer failed (truncated, another head, a changed field). The statement is shown beside it, under
    *do not treat it as certified*. *journal unreadable* for a signed file whose journal `serve` could not or would not read.
    *not certified* for a failed signature or an unreadable file, with nothing stated.
    Any other combination, including a pass whose journal fields disagree, is *unknown*. The console checks no
    signature itself.
  - **Cost and freshness.** Each first verification is a pure-Python Ed25519 check (about 0.14 s, as measured for the
    witness). A verdict depends only on the certificate bytes, the key, `registry.jsonl` and the thread's journal. So
    `serve` keeps up to 256 verdicts keyed on the SHA-256 of the certificate and the state of the other two. That state
    is *absent*, the SHA-256 of a regular file of at most 64 MiB (read in chunks), or *refused*. A missing journal and
    an unreadable one are different states. A verdict is kept only when nothing was refused and both files hash the same
    after verifying as before. A write in between would otherwise file the new content's verdict under the old content's
    key (`test_a_verdict_is_computed_once_per_input_bytes` corrupts the journal during verification and requires a
    fresh pass once it is put back). The registry and each journal are hashed once per read, however many rows name
    them. The thread used to pick the journal is read from the unverified file, only after it matches the thread id
    pattern, and is never answered. One read verifies at most 50 files in name order and reports the total. Reads run
    one at a time. The console keeps one answer until `r`.
  - **Trust boundary.** The home is the deployment's, like the witness log. A process that can write it can swap a
    journal between `serve`'s check and the verifier's read; the size and file-type checks keep a FIFO or a device from
    hanging the route, not from that race.
  - **Who reads.** Any principal, like the witness read. A certificate is built to travel (an in-toto Statement for
    Sigstore and Rekor), and it carries an objective, actor and approver names, roles and commit ids. Restricting it to
    a role would be a deployment choice this branch does not make.
  - **Nothing is written.** The test compares every file under the Build Thread project (outside `.git`) and a full
    dump of the case database before and after a read. `verify_certificate` opens the journal read-only and takes no
    lock.
  - **Independent review.** A Claude session that did not write the change reviewed it and found no input that makes a
    row read *certified* without `serve`'s verification. All seven of its findings are fixed:
    - A deeply nested file raised `RecursionError` past the thread lookup and answered `500` for the whole listing.
    - The cache could not tell a missing journal from an unreadable one, and could file a verdict under the hash read
      before an append.
    - Journal and registry reads had no size cap, followed a FIFO, and were repeated for every row.
    - The run example took the trusted key from the folder.
    - An unreadable journal was drawn as *journal differs*.
    - Signed-only rows drew their checks green.
    - The path-disclosure test could not fail on Windows.
  - **Several keys** (this branch, on main after #121). #121 left *more than one trusted key, rotation or revocation* open.
    This branch closes the first two for reading and leaves revocation open.
    - **Configuration.** `SYBERWORK_CERTIFICATE_PUBLIC` may list keys separated by commas: the new key and the ones being
      rotated out, at most `MAX_KEYS` (8). Space around an entry is ignored. `serve` stops on any of these:
      - an empty entry;
      - an entry that is not exactly 64 hex characters (`bytes.fromhex` alone would take spaces between the pairs);
      - a key listed twice, in either letter case;
      - a ninth key;
      - a key that is not usable (`usable_key`): it does not decode to a curve point, or it has small or mixed order.
        `syberlabs.ed25519.verify` accepts any 32 bytes as a key. Under the identity point `01 00 … 00` (or `00 … 00`,
        of order 4), the signature `R = B, s = 1` verifies any message. A test performs that forgery and shows the key
        refused. A key `certify` derives from a seed is in the prime-order subgroup and passes. This also applied to the
        one key #121 took.

      `CertificateShelf` refuses the same (`test_the_shelf_takes_one_to_eight_distinct_keys`,
      `test_a_key_anyone_could_sign_under_is_refused`, `test_serve_reads_a_comma_separated_list_and_refuses_a_malformed_one`;
      exactly eight are accepted). One key behaves as in #121.
    - **Which key is tried.** `syberlabs.dsse.verified_payload` tries a signature only under the key its `keyid` names,
      or under any key when it names none. So `serve` tries a file only under the configured keys a signature in it could
      match, in configured order (`_candidates`). A skipped key could only have failed as `signature_invalid`, so the
      verdict is the same as trying every key (`test_narrowing_gives_the_verdict_of_trying_every_key`, over envelopes with
      non-object entries, a null, integer, list or upper-case `keyid`, a bad signature beside a good one, `signatures` as
      an object or a string, and a top-level array). The first key whose
      report is not `signature_invalid` decides: its pass, or its own later failure (shape, subject, journal). Another key's
      `signature_invalid` never hides it. A file that cannot be read that far is verified under the first key, which
      names the failure. As with `syberlabs verify`, a genuine signature whose `keyid` names another key is not tried,
      and fails. `test_a_file_is_tried_only_under_the_keys_its_signatures_could_match` checks the keys tried for each
      case. Each of these mutations fails a test: trying every key, trying only the first, stopping at the first key
      whatever it reports, trying keys past the signer, dropping the key check or the signature limit, parsing a key
      with `bytes.fromhex` alone, and refusing an eighth key.
    - **Cost.** A key-less signature is tried under every key, and `verified_payload` tries every signature entry. A
      file may therefore carry at most `MAX_SIGNATURES` (2: the certifying process's, and the witness co-signature
      docs/CERTIFICATE.md plans). A file with more is refused as `too_many_signatures` before any check is run
      (`test_a_file_with_more_signatures_than_allowed_is_refused_unverified`). One file costs at most 2 × the number of
      keys Ed25519 checks, 16 with 8 keys, about 0.14 s each the first time it is seen. #121 had no such limit: a 1 MiB
      file of key-less entries cost on the order of ten thousand checks under its one key.
    - **What is answered.** `public_keys` replaces #121's `public_key`, in configured order. `statement.keyid` is the
      configured key that verified the file, never a `keyid` read from the file. A key no one configured is never named
      (`test_each_listed_key_verifies_what_it_signed_and_the_row_names_it`).
    - **What the console draws.** `M.certificateView` reads a statement only when its `keyid` is one of `public_keys`
      in the same answer. Otherwise a pass is *unknown* and a signed file whose journal failed is *unknown*: no statement
      and no key. Each signed row carries `key`, the 1-based position of its key in that list. Studio shows the keys in
      order and, with more than one, *key N · d75a…* under each signed row's verdict. The browser walkthrough serves a
      new key and the old one: the fixture's two files read *certified · key 2* and one re-signed under the new key reads
      *certified · key 1*. The parity test runs the node projection on a real `CertificateShelf` answer under both keys.
    - **Not done.** Revocation, or any validity window: a listed key is trusted for any certificate it signs, whatever
      its `issued_at`, until it is taken off the list and `serve` restarts (`test_removing_a_key_stops_trusting_what_it_signed`).
      The cache is per process and the key list is fixed for its life, so a verdict is never kept under a changed list.
      `syberlabs verify` still takes one `--public` key and accepts any 32 bytes as one. `SYBERWORK_WITNESS_PUBLIC` is
      not checked with `usable_key` either: the weak-key check covers certificate keys only. The check belongs in
      `syberlabs.ed25519` and was not moved there. `syberlabs/`, docs/CERTIFICATE.md and `spec/` are unchanged.
    - **Independent review.** A Claude session that did not write the change reviewed it, without being told my
      conclusions. It differential-fuzzed `_candidates` against trying every key (400 odd envelopes, 3 trusted keys and 1
      untrusted) and found no mismatch, and no input that reads *certified* without a configured key's signature or that
      names the wrong key. Its findings, all fixed on this branch:
      - A weak key (the identity point, or another of small order) was accepted, as in #121, and anyone could sign under it.
      - A file of many key-less signatures could hold the listing for hours, now up to 8 times longer than with one key.
        The NOTES claim of one check per key was false for a file with several signatures.
      - No test checked that exactly 8 keys are accepted, or the odd envelope shapes against trying every key.
      - Spaces inside a key were accepted.
  - **Not done.** The repository layer from `serve`. Revocation (above). Any link from a case to a thread. The witness co-signature, which is docs/CERTIFICATE.md's
    next packet; v0 certificates are signed in the certifying process, and the page says so. A push when a new file
    appears (press `r`). `syberlabs/certificate.py`, docs/CERTIFICATE.md and `spec/` are unchanged.
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
