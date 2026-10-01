# Adversarial payment

Status: **measured on this simulation**. Not a customer. Not a run of OPA, Cedar, Temporal, LangGraph, or any other named product.

The problem is one bank balance of 10000 and two invoices. `INV-LIVE` is 4000 and fresh. `INV-STALE` is 8000 and expired. The bank debits and then drops the HTTP response. A second attempt with a new idempotency key charges the account again.

`benchmarks/adversarial_payment.py` runs four rows on that ledger:

| Row | What it actually is |
| --- | --- |
| `syberwork` | `syberlabs.Session`, the admission rules, and reconciliation |
| `direct_tool` | A stand-in that pays whatever invoice the caller names |
| `role_gate` | A stand-in that treats an operator role as permission to pay |
| `retry_workflow` | A stand-in that records a timeout as success and retries with a new key |

The stand-ins copy the failure modes named in `docs/SDK_FEEDBACK.md`. They do not contain those products.

SyberWork refuses the unverified invoice, the stale invoice, the model’s early pay, and the proposer’s self-approval. The live debit happens once. The dropped response stays `unknown` until reconciliation reads the bank and records one verified payment. The chain checks. Spent is 4000.

The tool stand-in and the role stand-in each spend 8000 on the stale invoice. The retry stand-in spends 8000 as two live debits and counts the timeout as success.

The table written by the run is `benchmarks/results/adversarial-payment.txt`. `tests/test_adversarial_payment.py` fails if those outcomes change.
