# Case Study 001: an emergency spare part with incomplete and conflicting information

**Organization:** Northstar Medical Devices, a fictional medical device manufacturer.  
**Operational situation:** Packaging line 3 is stopped. Manufacturing needs two P-104 drive components. A requisition exists, but a purchase order may be issued only against an approved request, an identified delivery site, an unambiguous supplier quote, available budget, and an independent purchasing approval.  
**Evidence:** [Recorded JSON execution](results/enterprise_procurement_run_001.json); [executable simulator](enterprise_procurement.py). The results below were produced by running the application, not by hand-authoring a plausible transcript.

## What was actually exercised

Each scenario started with a new SyberWork SQLite case database and a **separate** SQLite-backed simulated ERP exposed over a real loopback HTTP server. The application fetched versioned requisition, site, quote, and budget records over HTTP, recorded observations in its case history, admitted or denied proposals, and attempted conditional HTTP writes to the ERP. The simulated ERP stored orders and debited a cost center. A case-history hash-chain check and a separate query of ERP orders followed each scenario. Scenario data and organizational identities are fictional. This exercise does not demonstrate integration with an actual enterprise ERP, supplier, identity provider, or payment system.

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
| Procurement analyst `a.rivera` | Opens the case and reads registered systems | Independent approval; an asserted fact cannot impersonate a verified source read |
| Compiled scheduler | Proposes the next contract action using observed values | A direct destination write or an exemption from admission |
| Purchasing manager `d.patel` | Independent approval, reconciliation attestation, final signoff | The source of inventory, quote, site, or budget records |
| Requisition registry | Request ID, part, quantity, delivery site, cost center, approval state | Supplier price or purchase completion |
| Site registry | A versioned, active site and address | Which of two plausible sites a missing requisition intended |
| Supplier quote endpoint | Selected quote, unit price, total, currency, version | A selection from two candidates when selection is absent |
| Finance ledger | Cost center CC-742, USD 5,000 available at start | Whether an order reached the ERP |
| Simulated ERP order endpoint | Validates current records, quote version, request and site consistency, budget; writes an order once per idempotency key | Whether SyberWork recorded final manager signoff |

The ordinary case uses two parts at USD 1,250 each, a USD 2,500 total, selected quote **Q-881** from Alder Components, and delivery to **DC-WEST-4**, 901 Harbor Way, Oakland. Requisition **REQ-4812** is approved and charged to **CC-742**. This choice of figures creates a visible budget effect: USD 5,000 before the purchase and USD 2,500 afterward. The simulated ERP checks the arithmetic and current budget again on write.

## Contract and controls

The contract is version 1 of `northstar-spare-procurement`, with one required case input: `request_id`. The added `input_bindings` rule ties that pinned input to the verified `request.id`; an unrelated requisition cannot be silently substituted. Both actions require source-backed request and site facts. `issue_order` additionally requires a selected supplier quote and finance record, with 15-minute freshness limits for quote and budget; it also requires a successfully recorded review. All submitted fields are bound to observed values, and the write carries the quote's ETag in `If-Match` plus a proposal-scoped `Idempotency-Key`.

The contract caps the order at USD 5,000. The organization policy grants the operator role, caps the action at USD 10,000, and requires a manager approval. Both caps apply, so the effective cap is USD 5,000. The original proposer alone may commit. Completion requires an external `issue_order` success, or a reconciled confirmed success, **followed by** a manager signoff. Initial approval does not count as completion.

These are controls in the executable code and in this particular synthetic contract. The simulated ERP supplies additional consistency checks. SyberWork's generic policy language does not yet express every cross-record business rule that the ERP enforces.

## Observed scenario matrix

| Scenario | Decisive event | SyberWork outcome | ERP orders | Acceptance |
| --- | --- | --- | ---: | --- |
| Approved, complete information | Independent approval, conditional write, subsequent signoff | `complete` | 1 | Complete |
| Requisition ID omitted | Case creation returns `input_schema` | `input_rejected` | 0 | No case opened |
| Delivery destination unresolved | Guessing a site yields `missing_fact:site` | `blocked_missing_site` | 0 | Incomplete |
| Two plausible supplier quotes | Source yields options without a selected `quote`; `source_shape`, then `missing_fact:quote` | `blocked_ambiguous_quote` | 0 | Incomplete |
| Policy tightened after approval | Commit rechecks policy and returns `action_not_in_global_policy` | `blocked_at_commit` | 0 | Incomplete |
| Quote changed before submit | ERP returns 412; old effect becomes unknown; external lookup finds no order; quote refresh leads to a new approved proposal | `recovered_after_refresh` | 1 | Complete |
| ERP wrote but response disappeared | Application records unknown; external lookup finds order by idempotency key; manager reconciles and signs | `reconciled_complete` | 1 | Complete |

The recorded run contained **16, 0, 4, 10, 13, 22, and 16 case events** respectively. The six opened cases passed their local hash-chain checks. The missing-input scenario never created a case and has no chain to verify. The three completing scenarios each left CC-742 at USD 2,500; blocked cases left it at USD 5,000.

## The operating sequence and alternate paths

### 1. Complete request

The operator opens `REQ-4812`. Four registered sources return an approved requisition, active delivery site, a single quote, and budget, all with versions. The compiled scheduler proposes `record_review` using the observed requisition and site IDs; the local effect succeeds. It then proposes `issue_order` for USD 2,500. Admission returns `needs_approval`, and an attempt to commit before approval returns the same decision without claiming an effect. Manager `d.patel` approves the specific proposal and its argument hash. On commit, SyberWork rechecks admission and sends the conditional HTTP write. The ERP stores **PO-9001**, debits the budget once, and returns 201. A later manager signoff completes both acceptance clauses. The case history reports 16 events and a valid chain.

### 2. An incomplete input at intake

An operator submits `{}` instead of a request ID. The case API rejects the input schema before creating a case. There is no speculative requisition lookup, fabricated default, or external order. **Limit of this control:** the generic `string` input type does not reject every semantically unusable string, such as whitespace. An enterprise integration should validate its request identifiers at intake against the source registry.

### 3. An underdetermined delivery destination

`REQ-4813` is approved but has no selected site. West Oakland and Boston are both plausible facilities; urgency alone does not determine the right destination. The operator reads the requisition but cannot read a site for the missing key. A proposed review that guesses `DC-WEST-4` is denied `missing_fact:site`. There is no order and no manager signoff. The needed next action is a **human amendment in the authoritative requisition system** selecting a site; the operator would then refresh it through the registered source. The application does not infer a shipping address from context.

### 4. An underdetermined supplier choice

`REQ-4814` names an active site and budget, but the supplier system returns **Q-882A** at USD 2,500 and **Q-882B** at USD 2,640 with no selection. The configured connector requires one `quote` value and rejects the response as `source_shape`. The review can be recorded because the request and site are known, but an order proposal is denied `missing_fact:quote`. Selecting the lower price automatically would be an unauthorized procurement decision. A real integration needs a selection and qualification step in the authoritative source, followed by a fresh versioned quote read. This case remains open rather than being labeled complete.

### 5. A rule changes after approval

The operator prepares a valid order and the manager approves it. Before commit, organization policy version 2 removes `issue_order`. Rechecking the active policy at commit denies the action `action_not_in_global_policy`; no HTTP write occurs. Amendment replay against recorded events reports the changed decision without rerunning model calls or rewriting the ERP. Approval is evidence of an earlier decision, not a perpetual license to act under a withdrawn rule.

### 6. A quote changes after observation

The operator observes quote version `quote:3`, gains approval, and attempts the order. Before the write, the supplier's authoritative quote advances to `quote:4`. The ERP enforces `If-Match` and returns 412; SyberWork conservatively records `effect_unknown` because its HTTP execution path does not distinguish a definitive rejection from a lost response. In this strongly consistent synthetic ERP, a lookup by the proposal's idempotency key returns 404. The manager records **negative reconciliation** with that external lookup reference. The operator refreshes the quote, creates a **new proposal and approval**, and writes PO-9001 exactly once. Signoff completes the new effect. In an eventually consistent real ERP, an immediate 404 would be insufficient to declare non-execution; the integration must define a safe settlement window or a stronger status endpoint.

### 7. The order succeeds but its acknowledgement is lost

The simulated ERP commits PO-9001 and reduces the budget, then deliberately closes the HTTP connection before sending an acknowledgement. SyberWork records `effect_unknown` and does not retry that proposal. The manager checks the ERP **by the original idempotency key**, finds the order, and records a positive reconciliation with its ID. A later signoff completes the case. This execution exposed and fixed a real completion defect: the signoff clause previously counted only a direct `effect_succeeded` event, not a confirmed reconciliation. The new test covers that path. The application still **trusts the manager's external evidence string**; it does not independently authenticate the lookup during reconciliation.

## What this licenses, and what it does not

The execution shows deterministic denial on missing or conflicting facts, commit-time policy rechecks, a conditional ERP write, independent approval, idempotent effect claims, and a path through uncertain results in this synthetic environment. It does **not** establish enterprise deployment readiness. The highest priority engineering gaps before connecting a real customer include authenticated and machine-verifiable reconciliation evidence, source-specific identifier and shape validation, a deliberate quote-selection workflow, SSO and role provisioning, operational backup and restore, accounting controls for concurrent orders across cases, and a clear settlement protocol for eventually consistent destinations. A manager can currently attest a fake reference through the API; the case history would faithfully record the attestation without proving the referenced order exists.

The case study also demonstrates a distinction essential to this architecture: uncertainty can legitimately keep a case open. “High realism” here means preserving that open state and its missing authority, rather than making the workflow finish by supplying a plausible answer.
