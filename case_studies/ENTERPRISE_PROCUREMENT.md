# Case Study 001: an emergency spare part with incomplete and conflicting information

**Organization:** Northstar Medical Devices, a fictional medical device manufacturer.  
**Operational situation:** Packaging line 3 is stopped. Manufacturing needs two P-104 drive components. A requisition exists, but a purchase order may be issued only against an approved request, an identified delivery site, an unambiguous supplier quote, available budget, and an independent purchasing approval.  
**Evidence:** [Recorded JSON execution](results/enterprise_procurement_run_001.json); [executable simulator](enterprise_procurement.py). The results below were produced by running the application, not by hand-authoring a plausible transcript.

## What was actually exercised

Each scenario started with a new SyberWork SQLite case database and a **separate** SQLite-backed simulated ERP exposed over a real loopback HTTP server. The application fetched versioned requisition, site, quote, budget, and candidate records over HTTP, recorded observations in its case history, admitted or denied proposals, and attempted conditional HTTP writes to the ERP. Assigned logistics and procurement identities made source-system selections through separate role checked ERP endpoints with `If-Match`; those selections were verified through fresh reads before dependent actions proceeded. The simulated ERP stored orders, debited a cost center, and logged source updates. A case-history hash-chain check and separate queries of ERP orders and updates followed each scenario. Scenario data and organizational identities are fictional. This exercise does not demonstrate integration with an actual enterprise ERP, supplier, identity provider, or payment system.

Run locally with Python 3.11+ and no runtime dependencies:

```sh
PYTHONPATH=. python -m case_studies.enterprise_procurement --output case_studies/results/new_run.json
PYTHONPATH=. python -m unittest discover -s tests -v
```

The runner is repeatable in behavior; case UUIDs, timestamps, and hash heads differ on each run. The committed JSON is one observed execution and contains the full per-case events, their hashes, external orders, budget balances, and a concise operator trace. The event log records propositions and state changes inside SyberWork. The ERP snapshots demonstrate which external records were written in this simulation.

## Organizational map and authority

| Actor or system | Information or authority | What it cannot establish alone |
| --- | --- | --- |
| Manufacturing requester `m.liu` | Business need and requisition `REQ-4812` | A vendor selection, live budget, or an approved purchase order |
| Procurement analyst `a.rivera` | Opens the case, reads registered systems, requests resolution tasks | Independent approval; an asserted fact cannot impersonate a verified source read |
| Logistics owner `l.chen` | Selects a site in the simulated ERP using logistics credentials and verifies its source record | Purchase approval or a guessed site without a source update |
| Procurement owner `p.soto` | Selects a supplier quote in the simulated ERP using procurement credentials and verifies its source record | Purchase approval or a guessed quote without a source update |
| Compiled scheduler | Proposes the next contract action using observed values | A direct destination write or an exemption from admission |
| Purchasing manager `d.patel` | Independent approval, permission to check destination status, final signoff | A self-authored claim that an order exists |
| Requisition registry | Request ID, part, quantity, delivery site, cost center, approval state | Supplier price or purchase completion |
| Site registry | A versioned, active site and address | Which of two plausible sites a missing requisition intended |
| Supplier quote endpoint | Selected quote, unit price, total, currency, version | A selection from two candidates when selection is absent |
| Finance ledger | Cost center CC-742, USD 5,000 available at start | Whether an order reached the ERP |
| Simulated ERP order endpoint | Validates current records, quote version, request and site consistency, budget; writes an order once per idempotency key | Whether SyberWork recorded final manager signoff |

The ordinary case uses two parts at USD 1,250 each, a USD 2,500 total, selected quote **Q-881** from Alder Components, and delivery to **DC-WEST-4**, 901 Harbor Way, Oakland. Requisition **REQ-4812** is approved and charged to **CC-742**. This choice of figures creates a visible budget effect: USD 5,000 before the purchase and USD 2,500 afterward. The simulated ERP checks the arithmetic and current budget again on write.

## Contract and controls

The contract is version 1 of `northstar-spare-procurement`, with one required case input: `request_id`. The added `input_bindings` rule ties that pinned input to the verified `request.id`; an unrelated requisition cannot be silently substituted. Both actions require source-backed request and site facts. `issue_order` additionally requires a selected supplier quote and finance record, with 15-minute freshness limits for quote and budget; it also requires a successfully recorded review. All submitted fields are bound to observed values, and the write carries the quote's ETag in `If-Match` plus a proposal-scoped `Idempotency-Key`.

The contract declares site and quote resolution tasks with source-backed options, owners, deadlines, escalation roles, and gates for dependent actions. A recorded choice alone cannot complete a task: the authoritative source must change and a fresh observation must match the choice and case identity. The contract caps the order at USD 5,000. The organization policy grants the operator role, caps the action at USD 10,000, and requires a manager approval. Both caps apply, so the effective cap is USD 5,000. The original proposer alone may commit. Completion requires an external `issue_order` success, or a destination-verified reconciliation, **followed by** a manager signoff. Initial approval does not count as completion. The destination status lookup must match the original idempotency key and canonical request digest.

These are controls in the executable code and in this particular synthetic contract. The simulated ERP supplies additional consistency checks. SyberWork's generic policy language does not yet express every cross-record business rule that the ERP enforces.

## Observed scenario matrix

| Scenario | Decisive event | SyberWork outcome | ERP orders | Acceptance |
| --- | --- | --- | ---: | --- |
| Approved, complete information | Independent approval, conditional write, subsequent signoff | `complete` | 1 | Complete |
| Requisition ID omitted | Case creation returns `input_schema` | `input_rejected` | 0 | No case opened |
| Delivery destination unresolved | Open task denies guessed site; logistics changes requisition in ERP; fresh read closes task | `resolved_site` | 1 | Complete |
| Two plausible supplier quotes | Open task denies order; procurement selects quote in ERP; fresh quote and selection reads close task | `resolved_quote` | 1 | Complete |
| Policy tightened after approval | Commit rechecks policy and returns `action_not_in_global_policy` | `blocked_at_commit` | 0 | Incomplete |
| Quote changed before submit | ERP guarantees its 412 is a no-write rejection; quote refresh leads to a new approved proposal | `recovered_after_refresh` | 1 | Complete |
| ERP wrote but response disappeared | Application records unknown; status lookup matches original idempotency key and request digest; manager signs later | `reconciled_complete` | 1 | Complete |
| Manager supplies a fabricated order reference | Manual claim rejected; destination 404 remains pending; another proposal denied | `blocked_unproven_claim` | 0 | Incomplete |
| Destination record has the wrong payload digest | Status lookup rejects mismatch even though an external order exists | `blocked_mismatched_record` | 1 | Incomplete |
| Supplier qualification cannot be concluded | Quote task remains open; manager records explicit cancellation reason | `cancelled` | 0 | Cancelled, incomplete |

The recorded run contained **16, 0, 23, 22, 13, 21, 16, 17, 16, and 13 case events** respectively. All nine opened cases passed their local hash-chain checks. The missing-input scenario never created a case and has no chain to verify. The five completing scenarios each left CC-742 at USD 2,500. The mismatched-status case also debited USD 2,500 externally while remaining incomplete internally; this is intentional fail-closed behavior, not a second order.

## The operating sequence and alternate paths

### 1. Complete request

The operator opens `REQ-4812`. Four registered sources return an approved requisition, active delivery site, a single quote, and budget, all with versions. The compiled scheduler proposes `record_review` using the observed requisition and site IDs; the local effect succeeds. It then proposes `issue_order` for USD 2,500. Admission returns `needs_approval`, and an attempt to commit before approval returns the same decision without claiming an effect. Manager `d.patel` approves the specific proposal and its argument hash. On commit, SyberWork rechecks admission and sends the conditional HTTP write. The ERP stores **PO-9001**, debits the budget once, and returns 201. A later manager signoff completes both acceptance clauses. The case history reports 16 events and a valid chain.

### 2. An incomplete input at intake

An operator submits `{}` instead of a request ID. The case API rejects the input schema before creating a case. There is no speculative requisition lookup, fabricated default, or external order. **Limit of this control:** the generic `string` input type does not reject every semantically unusable string, such as whitespace. An enterprise integration should validate its request identifiers at intake against the source registry.

### 3. An underdetermined delivery destination

`REQ-4813` is approved but has no selected site. West Oakland and Boston are both plausible facilities; urgency alone does not determine the right destination. The operator reads the requisition and source-backed site options, then opens a task assigned to logistics with a 30-minute deadline. A proposed review guessing `DC-WEST-4` is denied `resolution_open:site`. An initial task check remains pending because the requisition has not changed. Logistics owner `l.chen` selects `DC-WEST-4` through the ERP's role checked, conditional update; its version advances from `req:3` to `req:4`. The owner verifies the changed record, closing the task against that requisition. The operator refreshes site, budget, and quote; the compiled review, independently approved order, and later signoff complete the case. The ERP has one source-update audit record and one purchase order. The application did not infer a shipping address from context.

### 4. An underdetermined supplier choice

`REQ-4814` names an active site and budget, but the supplier system returns **Q-882A** at USD 2,500 and **Q-882B** at USD 2,640 with no selection. The configured connector requires one `quote` value and rejects the response as `source_shape`. The operator opens a procurement task using verified quote options and records the review; the order proposal is denied `resolution_open:quote`. An initial check stays pending. Procurement owner `p.soto` selects Q-882A in the ERP using its own credential and a matching selection version. Fresh quote and selection reads establish the chosen ID, case identity, and changed version; the task closes. The order receives independent approval and a later signoff and completes once. The lower price was not selected automatically by SyberWork.

### 5. A rule changes after approval

The operator prepares a valid order and the manager approves it. Before commit, organization policy version 2 removes `issue_order`. Rechecking the active policy at commit denies the action `action_not_in_global_policy`; no HTTP write occurs. Amendment replay against recorded events reports the changed decision without rerunning model calls or rewriting the ERP. Approval is evidence of an earlier decision, not a perpetual license to act under a withdrawn rule.

### 6. A quote changes after observation

The operator observes quote version `quote:3`, gains approval, and attempts the order. Before the write, the supplier's authoritative quote advances to `quote:4`. The ERP enforces `If-Match` and returns 412 **before writing**. Its installed action definition explicitly declares 412 a no-write precondition result, so SyberWork records `effect_rejected`, not `effect_unknown`. The operator refreshes the quote, creates a **new proposal and approval**, and writes PO-9001 exactly once. Signoff completes the new effect. This declaration must come from the real destination's contract; a later 404 lookup cannot prove a write did not happen.

### 7. The order succeeds but its acknowledgement is lost

The simulated ERP commits PO-9001 and reduces the budget, then deliberately closes the HTTP connection before sending an acknowledgement. SyberWork records `effect_unknown` and does not retry that proposal. The manager triggers the registered status lookup. The application itself compares the returned `state`, idempotency key, durable order ID, and digest of the exact approved arguments. Only then does it record verified reconciliation. A later signoff completes the case.

### 8. A fabricated manager claim and an inconclusive lookup

The simulated connection closes **before** the ERP writes. The manager submits a fabricated reference `PO-FAKE` with `success=true`. SyberWork rejects the claim as `manual_reconciliation_disabled`. Its authorized status lookup receives 404, records `pending`, and refuses a new proposal for `issue_order` with `effect_unresolved:issue_order`. No external order exists in this run, yet the case remains open: a 404 alone is not an authoritative negative resolution in an eventually consistent system.

### 9. A committed order with the wrong status digest

The ERP writes an order and loses its acknowledgement, but the status record is then altered to carry the wrong request digest. The manager's lookup returns `unverified` with `destination_record_mismatch`. The order exists in the simulated ERP, and the manager even signs; SyberWork correctly remains incomplete because it cannot establish that this order matches the approved proposal. This case requires investigation and repair of the destination evidence, not a second order.

### 10. Qualification cannot be resolved

The competing quote task is opened for `REQ-4814`, but no owner selection is entered into the ERP. A review may be recorded because the site is known, while the order is denied `resolution_open:quote`. Manager `d.patel` records `Supplier qualification unresolved` as a cancellation reason. The projected status is `cancelled`, the resolution task projects as cancelled, acceptance remains incomplete, and the ERP has no selection update or order. Cancellation cannot conceal a claimed external HTTP effect with an uncertain outcome.

## What this licenses, and what it does not

The execution shows source-backed resolution, role checked updates, explicit cancellation, commit-time policy rechecks, a conditional ERP write, independent approval, idempotent effect claims, and a destination-verified path through uncertain results in this synthetic environment. It does **not** establish enterprise deployment readiness. Remaining work before connecting a real customer includes source-specific identifier and shape validation, supplier qualification rules, SSO and role provisioning, operational backup and restore, deadline notifications, accounting controls for concurrent orders across cases, and adapter contracts that genuinely guarantee no-write statuses and durable idempotency lookups. A compromised or incorrect destination can still return false status data; the application is trusting that system's authenticated response.

The case study preserves open cases for inconclusive effect evidence and closes unresolved qualification through explicit cancellation. The source owners resolve the two actionable ambiguities by changing authoritative records, with no fabricated answer from the workflow engine.
