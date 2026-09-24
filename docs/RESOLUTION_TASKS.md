# Source-backed resolution tasks

A contract can assign a missing decision to a specific role and block dependent actions while that decision is unresolved. A task is evidence of work owed, not an instruction to guess a value. The owner changes the authoritative system under that system's own access controls; SyberWork verifies the changed record through an installed read connector.

## Contract shape

`case_studies/enterprise_procurement.py` defines two working examples. The `site` resolution reads a missing `request.site_id`, fetches active site identifiers from `site_options`, and blocks `record_review` and `issue_order`. Its owner is `logistics`. The `quote` resolution reads a missing `quote_options.selection`, fetches quote identifiers, verifies the selected supplier quote and the options selection, and blocks `issue_order`. Its owner is `procurement`. Both use the case's `request_id` for the source lookup and verify that the resulting record belongs to that request. A manager is the escalation and cancellation authority.

Each resolution specifies `owner_role`, `escalate_role`, `due_seconds`, `record_key_input`, `blocks_actions`, `trigger` (`key`, `source`, `missing_path`, `identity_path`), `choices` (`key`, `source`, `list_path`, `identity_path`), `result` (`key`, `source`, `value_path`, `identity_path`), and optionally `confirmation` (`key`, `source`, `value_path`). The configured trigger and choices must have been independently refreshed as verified, versioned observations whose identity paths equal the case input. A task captures their event hashes, versions, distinct candidate identifiers, owner role, and deadline.

## Lifecycle

| Operation | Who | Evidence and outcome |
| --- | --- | --- |
| Request | Operator | Missing trigger and nonempty choices from configured sources; `resolution_requested` |
| Change authoritative record | Source-system owner | Source credentials and conditional write enforced outside SyberWork; no SyberWork task completion yet |
| Verify | Assigned role | Fresh result whose identity matches the case, selected identifier was in the captured choices, and source version changed; optional confirmation agrees; `resolution_completed` or a pending check |
| Escalate | Escalation role after deadline | `resolution_escalated`; the underlying decision stays open |
| Cancel | Contract cancellation role with reason | `case_cancelled`; pending tasks project as cancelled, dependent actions and signoff are denied |

The console offers **Request resolution**, **Verify source decision**, **Escalate overdue task**, and **Cancel case**. Corresponding POST routes are `/api/cases/{id}/resolution-request` with `{ "key": "site" }`, `/resolution-resolve` and `/resolution-escalate` with `{ "task_id": "…" }`, and `/cancel` with `{ "reason": "…" }`. `GET /api/cases/{id}` projects task status, choices, owner, deadline, and completed choice alongside the append-only events. An owner must separately use the source system to make the selection; the SyberWork routes do not accept a user-supplied answer as proof.

Admission checks a requested task's completion before its `blocks_actions` can proceed and checks the latest verified source result still has the completed choice and belongs to this case. The destination also validates current records and conditional versions at write time. A cancelled case rejects further proposals and signoffs. Cancellation rejects cases with claimed HTTP effects whose outcome is not an explicit no-write rejection, so a lost acknowledgement cannot be hidden by cancellation.

## Operational boundaries

Deadline status is projected from the clock; escalation is an explicit manager action rather than a background scheduler or notification. Roles and local bearer tokens in the included application are illustrative, not SSO. The enterprise case study's source updates use separate simulated ERP tokens and optimistic versions; a real connector must provide equivalent authorization, audit, identity, and concurrency guarantees. Source responses and task events are evidence within the configured trust boundary, and hash chaining alone does not protect against a database administrator rewriting all history. A manager cancellation closes the workflow in SyberWork; it does not undo prior external writes. Cases with an uncertain external write require reconciliation and further investigation.
