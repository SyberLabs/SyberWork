# Economic Actions Implementation Plan

> **For agentic workers:** Execute these tasks inline with test-driven-development; retain red and green evidence.

**Goal:** Make a USDC capable settlement service a constrained, evidence licensed SyberWork effect.

**Architecture:** Reuse the existing proposal, admission, approval, effect claim and reconciliation path. Add an `economic_http` adapter with fixed destination and exact minor unit validation. A SQLite budget ledger reserves capacity under the same transaction as the effect claim and retains uncertain spends.

**Tech Stack:** Python 3.11, SQLite, stdlib HTTP and unittest.

**Spec:** `docs/ECONOMIC_ACTIONS.md`

## Global constraints

- Do not send funds or call a live settlement endpoint in tests.
- Do not claim an x402 implementation or independently verified chain finality.
- Keep legacy non-economic effects unchanged.

## Review focus

- Concurrent cases attempting to overspend one budget: one claim must be denied.
- A lost success response: reservation remains and status reconciliation can settle it.
- A destination 404: retain reservation; never treat as a no-write proof.
- A prewrite rejection: release reservation for a new proposal.
- Malformed or mismatched receipt: never count as success.

### Task 1: Policy and intent admission

**Files:** `syberwork/economic.py`, `syberwork/core.py`, `tests/test_economic.py`

**Interface:** Validate admin installed `economic_http`, global economic policy and proposal arguments. `_admit` returns a reasoned denial for malformed, stale, unproven or over-budget proposals.

- [x] Write policy/intent tests and run `python -m unittest tests.test_economic -q` expecting feature failures.
- [x] Add exact minor unit, evidence hash, recipient, asset, rail, expiry and budget checks.
- [x] Run the tests expecting pass.

### Task 2: Atomic reservation and settlement

**Files:** `syberwork/core.py`, `syberwork/economic.py`, `tests/test_economic.py`

**Interface:** `commit` reserves under the SQLite write lock; the adapter sends a fixed request keyed by proposal ID, and verifies a receipt before success.

- [x] Write concurrent budget, matching receipt, rejection and uncertain response tests; run expecting failure.
- [x] Implement the reservation ledger and adapter response validation.
- [x] Run the tests expecting pass.

### Task 3: Reconciliation and documentation

**Files:** `syberwork/core.py`, `docs/ARCHITECTURE.md`, `docs/ECONOMIC_ACTIONS.md`, `tests/test_economic.py`

**Interface:** Status lookup verifies the same payment tuple before recording settlement; unknown and 404 retain reservations.

- [x] Write lost response and status mismatch tests; run expecting failure.
- [x] Implement economic reconciliation and document proof boundary.
- [x] Run `python -m unittest discover -s tests -q` expecting 0 failures.
