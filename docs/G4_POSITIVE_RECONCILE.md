# G4 Positive reconcile

Factory gate G4 (FACTORY red-team rubric §2, §3) blocks merge when a positive control
fails. It guards against a product that passes every adversarial gate by refusing
everything. Attest covers the Maintain Docs, Kane and Reconcile Spec controls. This
gate covers the SyberWork half: **one matching destination lookup reconciles**.

A lookup matches only when the installed status URL returns `state=committed` with the
proposal's idempotency key, the request digest of its arguments, and a non-empty string
`external_id` (see [RECONCILIATION.md](RECONCILIATION.md)). The control then requires a
verified `reconciled` event whose proof equals the destination record on all three
fields, a passing `order_recorded` acceptance, and exactly one order at the destination.

## Status

**Measured (SyberWork, scoped)** on PR #102's branch. These are hosted Actions runs, and
the status changes only from real CI output:

| Run | Head | What it shows | G4 result |
|---|---|---|---|
| [40307569607](https://github.com/SyberLabs/SyberWork/actions/runs/40307569607) | `5317ad1` | gate added | **success**: 29 listed ids passed (control 2, existing 3, twin 4, mutant 6, source-mutant 5, gate 9) |
| [40307678572](https://github.com/SyberLabs/SyberWork/actions/runs/40307678572) | `612d72d` | temporary: the `http` match in `Work.reconcile` refuses everything | **failure**: 16 ids failed, including both controls and the two `existing` HTTP controls. The economic control passed, as expected, because it uses a different matcher |
| [40307771525](https://github.com/SyberLabs/SyberWork/actions/runs/40307771525) | `ab51394` | temporary: mutant reverted, manifest removed | **failure**: `manifest is missing` |
| [40307823858](https://github.com/SyberLabs/SyberWork/actions/runs/40307823858) | `91f706e` | manifest restored (tree identical to `5317ad1`) | **success**: 29 passed |

Not required on `main`. Not covered: see Scope.

## Job

Workflow `.github/workflows/g4-positive-reconcile.yml`, job **`G4 Positive reconcile`**,
on push and pull request. It runs on ubuntu-latest with Python 3.12, using the standard
library only. Run it locally with:

```
python scripts/g4_positive_reconcile.py
```

The runner executes every id in `tests/g4_positive_reconcile.txt` and fails when:

- the manifest or a named test file is missing,
- a required section is empty: `control`, `existing`, `twin`, `mutant`, `source-mutant`, `gate`,
- any listed id is not found, skipped, failed, errored, an expected failure, or an
  unexpected success. A failing subtest fails its test.

A gate self-test fails if a test in the two G4 modules is added or renamed without being
listed in the manifest.

## Sections

| Section | What it shows |
|---|---|
| `control` | PC-REC-01 `Work.reconcile` and PC-REC-02 the HTTP `reconcile` route. Each runs on an approved order the destination committed while its acknowledgement was lost. PC-REC-01 also checks that the case completes after the later signoff |
| `existing` | The positive controls already on `main`, run read-only: `test_reconciliation`, `test_attest_reconcile_map`, and the economic settlement lookup in `test_economic` |
| `twin` | One destination field differs each time: key, digest, `external_id` (empty, absent, or not a string), or `state` (`proven`). The result must be `unverified` and must not reconcile. The real lookup still reconciles afterwards |
| `mutant` | In-process patches that refuse everything at one stage: `Work.reconcile`, the target guard, lookup 404, lookup unavailable, digest never matches, and acceptance ignores every proof. The control oracle must report the miss, and the unpatched product passes again |
| `source-mutant` | A copied tree has one source line edited so the product refuses every match. The gate's `control` and `existing` sections run there, and the runner must exit 1 with each affected id failed. The mutated lines are the HTTP match in `syberwork/core.py`, the `verified` status line, `verified_reconciliation`, and `receipt_matches`. An unmutated copy must pass. The anchors must occur exactly once, so a refactor that moves them fails the test instead of passing it silently |
| `gate` | The runner fails closed on a scratch tree: missing manifest or file, unknown id, empty or unknown section, duplicate id, and fail/error/skip/xfail/xpass/subtest failure |

## Scope

- **Covered:** the `http` destination lookup in `Work.reconcile`, using the enterprise
  procurement `SimulatedERP` over loopback HTTP, and the operator HTTP route. The
  `economic_http` lookup is covered through the existing `test_economic` control and the
  `receipt_matches` source mutant.
- **Not covered:** `Session.reconcile` (`syberlabs/session.py`), whose executor decides
  what a destination read is. Postgres-backed storage is also not covered: the job runs
  on SQLite and installs nothing. No live third-party destination is involved.
- **Mutants:** the in-process mutants patch named functions. The four source mutants are
  single-line edits. Together they show the gate catches blanket refusal at those stages
  only. This is not general mutation testing and not a coverage figure.
- **Twins** are not the §1 adversarial cases. Those belong to G2 and G3.
- **Overlap:** `ci.yml` discovers the same test modules, so the full suite also runs
  them on Python 3.11 to 3.13 and on Windows. #99 (G6) runs `test_reconciliation` and
  `test_attest_reconcile_map` and has its own positive control. This gate does not amend
  that work. On the temporary match mutant (`612d72d`), `ci`'s full suite also failed
  ([run 40307678789](https://github.com/SyberLabs/SyberWork/actions/runs/40307678789):
  18 failures on 3.13, 4 of them pre-existing tests). For that mutant, G4 and the full
  suite both block. What G4 adds is a fixed, named list that cannot be deleted, skipped
  or xfailed without failing.
- **Required checks:** not set. Whether `G4 Positive reconcile` becomes required is a
  decision for the captain or an admin.
