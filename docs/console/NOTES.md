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
  effects pending, worker queue in worker mode, denied proposals), and the identity switcher with role
  chips.
- **Case list**: status per case, plus *effect unknown* and *needs approval* flags. `/` filters,
  `j`/`k` move.
- **Case tabs** (`1`–`8`):
  1. *Flow*: the compiled path as steps, each with its required facts, proposal, decision, approvals
     and effect, and a **Next** banner that names the move and the role that can make it.
  2. *Evidence*: required facts per action, refresh from a configured source, observations newest
     first (superseded rows dimmed), and a collapsed, amber **Manual assertion** form. A typed value is
     stored `verified: false` and labelled *unverified*. An Attest receipt is labelled *Attest claim ·
     unverified · not destination proof*.
  3. *Propose*: the compiled step, a proposal by hand (origins the credential does not hold are
     disabled), and the external planner. When `SYBERWORK_PLANNER_URL` is unset the planner panel is an
     empty state naming the variable; when set, **Ask configured planner** is enabled for a `model`
     credential. All proposals with their decisions and actions.
  4. *Effects*: every claimed effect with its outcome, idempotency key, destination response or proof,
     and status checks. Attest observations on the case are called out as reconciling nothing.
  5. *History*: the hash chain, head first. Each row shows its hash; expanded, its predecessor hash and
     body. A link-break check runs in the page, but only the server's verification counts.
  6. *Replay*: choose a contract and a policy version. The table shows each changed decision as recorded
     and as replayed, and each acceptance clause before and after. The server's note (no re-read, no
     mutation) is shown verbatim.
  7. *Closure*: resolution tasks (request, verify the source decision, escalate when overdue) and
     cancellation with a required reason.
  8. *Candidates*: shown only for a contract with an `evolution` section.
- **Studio**: published contracts and policies (immutable, with digest and pinned case count),
  compiled artifacts (path, input form schema, action gates, acceptance), a **Draft vN+1** editor
  that validates JSON, refuses an existing version, and lists the changed paths before **Publish**.
  Actions and sources are listed and registered by administrators only.

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

All are `GET`, require a bearer token, and append nothing (`tests/test_console.py`
`test_reads_append_nothing`).

| Route | Who | Returns |
|---|---|---|
| `/api/console` | any principal | `{planner_configured, effects}`. The planner URL is not disclosed. |
| `/api/contracts` | any principal | `[{id, version, title, compiled_path, digest}]` |
| `/api/contracts/{id}/{version}` | any principal | the stored contract (the same document `GET /api/cases/{id}` already returns for a case) |
| `/api/policies` | any principal | `[{version, body, digest}]` |
| `/api/actions`, `/api/sources` | admin | installed definitions by name. They contain credential variable names, never values. |

A malformed version in `/api/contracts/{id}/{version}[/artifacts]` is now `404 not_found` instead of
`500 internal_error`.

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

Known gaps, stated rather than faked:

- **Deciding rule**: `Work.explain_admission` has no HTTP route. Each proposal shows *Deciding rule:
  not exposed over HTTP* instead of guessing it.
- **JCS side records** (`Work.side_channel`), **witness and certificate**, and **per-work-item metrics**
  (`/metrics/work_items`) are not shown in the console.
- **Builder API** (`/api/builder/*`) is a separate surface and is not part of this console.
- **Source read roles** are only visible to administrators (`GET /api/sources`). For other
  credentials, refresh says *the source read role is checked by the server*.
- **Attest detection** is a display rule (value `schema` starting with `attest.`, or a source named
  like `attest`). It only adds a warning label; it cannot make anything verified.
- **Case statuses** in the list come from one `GET /api/cases/{id}` per case, four at a time, capped
  at 60 cases. There is no push: press `r` to reload.
- The README's API list does not mention the new read routes. README wording is a public claim
  (Class P in `AGENTS.md`), so that line is left for a human key.
