# G2 Bridge quota floor

Factory gate **G2 Quota floor** (FACTORY red-team rubric §3) blocks a merge when
red-team cases per component and category fall below the §1 minimums. Attest's
G2 counts the Attest-side cells and reports Bridge PI/AB. It explicitly does not
enforce the SyberWork side of the bridge row. This gate enforces all seven
SyberWork bridge cells.

| | PI | AB | DS | SIM | SCH | COST | REC | Total |
|---|---|---|---|---|---|---|---|---|
| §1 minimum, SyberWork bridge (N3/N4) | 3 | 3 | 3 | 2 | 4 | 1 | 6 | **22** |

- Job: `G2 Bridge quota floor` in `.github/workflows/g2-bridge-quota-floor.yml`.
  It has its own workflow, so it does not touch `ci.yml` (G8) or `factory-gates.yml` (G6).
- Counter: `scripts/g2_bridge_quota_floor.py`. Gate controls: `tests/test_g2_bridge_quota_floor.py`.
- Whether the job is required is decided by branch protection. This change does not set it.

## What counts

The inventory is every `tests/test_bridge_redteam*.py` module. Each one defines a
module-level `CASES` that maps a test method name to `(category, rubric lines)`.
The counter imports each module, runs exactly the tagged tests once, and counts
a case only when both of these hold:

1. **The status counts.** `active` and `xfail` count. A `stub` never counts.
   This is the counting lock: active + xfail, stub never.
2. **The test ended the way its status says.** An `active` case must pass. An
   `xfail` case must fail an assertion: it is an executable, observed gap. It
   counts for G2, and G3 must fail it once G3 is turned on.

Status is read from the test method, never from a list in the module:

| The method is | Status |
|---|---|
| decorated with `unittest.expectedFailure` | `xfail` |
| skip-decorated (method or class), or its body is only a docstring, `pass`, `...` or `raise NotImplementedError` | `stub` |
| anything else | `active` |

These cases are listed and not counted:
- the test is missing;
- the test is skipped at run time;
- the test fails or errors, including in a subtest or in `setUpClass`;
- an `xfail` passes unexpectedly;
- an `xfail` fails with a crash instead of an assertion.

The gate fails closed when any of these is true:
- any cell is under its floor;
- no inventory module exists, so every cell is 0;
- a module does not import or has no valid `CASES`;
- a case names a category outside the bridge row;
- a test name is defined in more than one class;
- the pinned floors do not sum to 22.

The floors are pinned in the counter, so a PR cannot pass G2 by lowering
`FLOORS` or `REC_FLOOR` inside a case module.

A green result means each cell has at least that many executable cases. It does
not say that the cases pass their rubrics (that is G3). It is not a coverage figure.

## Status

**Measured (SyberWork bridge, scoped).** Not on `main` and not required. This
section changes only from real Actions output.

The case modules are still open PRs, so the job was measured on disposable refs.
These are not pull requests:

| Ref | Contents | Run | Result |
|---|---|---|---|
| `ci/g2-bridge-integration-r23` | `main` + #94 + #96 + #97 + the PR #101 commit | [run 40304511544](https://github.com/SyberLabs/SyberWork/actions/runs/40304511544) | **PASS**, 22/22, table below |
| `ci/g2-bridge-negctl-r23` | the same, plus #96's `test_injected_receipt_text_is_inert` skip-decorated into a stub | [run 40304584830](https://github.com/SyberLabs/SyberWork/actions/runs/40304584830) | **FAIL**, PI 2 < 3, the stub listed as not counted, 21/22 |
| `feat/ci-g2-bridge-quota-floor` (PR #101 head, `main` + this change) | no inventory | [run 40304496103](https://github.com/SyberLabs/SyberWork/actions/runs/40304496103) | **FAIL**, every cell 0, 0/22 |

The 15 gate controls passed in all three runs.

### Counts

These counts are from run 40304511544 (Python 3.12, ubuntu-latest). They match
the local run on Python 3.13.5. The tree was `main` (`e2e6408`) with #94
(`d84cf1e`), #96 (`23f9dcc`) and #97 (`ff65d89`) merged. They are counts of
tagged test methods, not coverage.

| Cat | Floor | active | xfail | stub | Counted (active+xfail) | Active-only | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| PI | 3 | 3 | 0 | 0 | 3 | 3 | met |
| AB | 3 | 4 | 0 | 0 | 4 | 4 | met |
| DS | 3 | 9 | 0 | 0 | 9 | 9 | met |
| SIM | 2 | 4 | 2 | 0 | 6 | 4 | met |
| SCH | 4 | 10 | 2 | 0 | 11 | 9 | met |
| COST | 1 | 3 | 0 | 0 | 3 | 3 | met |
| REC | 6 | 10 | 0 | 0 | 10 | 10 | met |

Counted toward floors: 22/22. The table has 47 tagged cases (12 from #94, 17
from #96, 18 from #97). One is not counted:
`tests/test_bridge_redteam_floors.py::test_malformed_http_reconcile_requests_never_reconcile`
(SCH, #96) fails in the combined tree. See the notes below.

On `main` alone, the inventory is empty and the gate fails with every cell at 0.

### When the floors activate

The gate goes green only after **both #94 and #96** are merged. Merged alone,
each one fails:
- #94 alone: REC is met, and every other cell is short.
- #96 alone: REC is 0 of 6.
- #97 alone: PI, AB and REC are short.
- #94 + #97: PI and AB are short.
- #96 + #97: REC is 2 of 6.

With #94 + #96, every cell is met, and SIM sits exactly at its floor (2). #97
adds margin: DS +5, SIM +4 (2 active and 2 xfail), SCH +5, COST +2 and REC +2.
All of these subsets were run locally with this counter. None was run in CI.

## Notes

- **The doc conflicts with the counting lock.** #96 lists its
  `@unittest.expectedFailure` case in `STUBS` and says that STUBS never count.
  #97's PR text calls its `expectedFailure` cases stubs and does not count them.
  Under the counting lock, an executable `expectedFailure` that fails an
  assertion is an **xfail**, and it counts. This gate follows the lock and the
  FACTORY G2/G3 split. It reads status from the decorator, not from `STUBS` or
  `MISSING_HOOK`. Those modules' own `QuotaFloor` tests are stricter
  (active-only) and still hold. Neither PR is amended here.
- **#97's two REC cases count.** The #97 PR text says its two REC cases are
  "reported but not counted toward the REC floor". G2 counts by primary tag with
  one rule, so they count. REC is met by #94 alone (8 of 6), so this changes no
  result.
- **#94 and #96 interact.** With both merged, #96's SCH case
  `test_malformed_http_reconcile_requests_never_reconcile` (subtest
  `body='missing_id'`) expects `409 invalid_request`. #94 now refuses any field
  other than `proposal_id` first, so it gets `409 manual_reconciliation_disabled`.
  G2 does not count that case, and SCH is still met. The full suite (`ci.yml`)
  will fail on that test until whichever PR merges second updates the
  expectation.
- **Themes are not checked.** §1 required bridge themes are not checked. The
  SyberWork case modules carry no theme tags, and mapping case names to themes
  here would invent coverage. Attest's G2 also leaves the bridge themes it cannot
  see as pending.
- **G3 is out of scope.** G2 does not judge whether a case passes its rubric. An
  active case that fails is simply not counted, and `ci.yml` reports it.
