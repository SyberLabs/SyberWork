# G3 Bridge adversarial pass

Factory gate **G3 Adversarial pass** (FACTORY red-team rubric §3) blocks a merge
when a §1 red-team case fails the §2 rubrics R1–R9. Attest's G3 runs the
Attest-side cases: Maintain Docs, Reconcile Spec, Kane and the Attest side of the
bridge. This gate runs the **SyberWork side of the bridge row**. The two gates
are separate, and neither one's result says anything about the other.

- Job: `G3 Bridge adversarial pass` in `.github/workflows/g3-bridge-adversarial-pass.yml`.
  It has its own workflow, so it does not touch `ci.yml` (G8), `factory-gates.yml`
  (G6) or `g2-bridge-quota-floor.yml` (G2).
- Gate: `scripts/g3_bridge_adversarial_pass.py`. Gate controls: `tests/test_g3_bridge_adversarial_pass.py`.
- Whether the job is required is decided by branch protection. This change does not set it.

## What runs

The inventory is found the same way as the G2 counter: every
`tests/test_bridge_redteam*.py` module and its module-level `CASES`, which maps
a test method name to `(category, rubric lines)`. Positive controls are the test
methods of a `PositiveControl` class in the module, plus any name listed in a
module-level `POSITIVE_CONTROLS`. No parallel inventory is kept here.

The rubric expectations are the assertions already in those tests. G3 adds no
new cases and no new expectations. It runs them and maps each outcome back to
its case.

Status is read from the test method, with the same rule as G2:

| The method is | Status | What G3 needs |
|---|---|---|
| plain | `active` | it passes |
| decorated with `unittest.expectedFailure` | `xfail` | it still fails an **assertion** (an observed gap) |
| skip-decorated, or its body is only a docstring, `pass`, `...` or `raise NotImplementedError` | `stub` | a reason: a skip reason or a docstring. It is not run |

This follows the counting lock: G2 counts active + xfail and never a stub, and
G3 asserts only that the **active** cases pass. xfail and stub cases are listed
as open gaps. They do not fail G3, and they never count as G3 passes. G3 over
every §1 case holds only when no gap is left.

## When it fails

The job fails closed when any of these is true:

- an active case is missing, skipped at run time, fails or errors, including in
  a subtest or in `setUpClass`. A skip is never a G3 pass;
- an xfail case passes unexpectedly. Its product hook has probably landed:
  remove the decorator so it runs as active;
- an xfail case fails with a crash instead of an assertion, which is not an
  observed gap;
- a stub gives no reason;
- a positive control is missing, is xfail or stub, or does not pass, or there is
  no positive control at all, so "refuse everything" cannot pass;
- there is no active adversarial case, so G3 cannot pass on nothing;
- no inventory module exists, a module does not import or has no valid `CASES`,
  a case names a category outside the bridge row (PI, AB, DS, SIM, SCH, COST,
  REC), its rubric lines are not a non-empty subset of R1–R9 or omit R1, or a
  test name is defined in more than one class.

R1 (fail closed) is the only rubric tag G3 enforces, because §2 applies it to
every adversarial case and every bridge case already tags it. The summary also
reports, without failing, how many passing cases do not tag a rubric that §2
says applies to them: R8 (Bridge), R9 (all), R6 (PI) and R7 (COST). A tag is
metadata. The test asserts what it asserts, so a missing tag is not coverage
G3 can add.

## Refuse-everything and accept-everything twins

A suite that refuses everything, or one whose assertions went soft, could pass
every adversarial case for the wrong reason. The third step reruns the gate on
two throwaway copies of the tree. In each copy, the destination match in
`Work.reconcile` (`if matched:` in `syberwork/core.py`) is replaced:

| Twin | Destination match becomes | Must newly fail |
|---|---|---|
| refuse-everything | never matches | at least one positive control |
| accept-everything | always matches | at least one active adversarial case |

Each twin passes only if the gate exits non-zero **and** it fails cases that the
baseline run did not. If the line is not found exactly once, the baseline report
is missing, or the baseline has no active case, the step fails. Product code in
the checkout is never changed. The gate controls include a soft suite that
asserts nothing about the outcome, and check that it is reported as
**NOT CAUGHT** under accept-everything.

## Status

**Proposed.** Measured status is recorded below only from real Actions output.

## Notes

- **Red on `main` until the inventory merges.** `main` has no
  `tests/test_bridge_redteam*.py`, so the job fails closed there: no inventory,
  no active case, no positive control. The inventory is in open PRs #94, #96 and
  #97. Like G2 (#101), this gate has no "inactive" switch.
- **Discovery is copied from #101, not imported.** #101 is still open, so the
  discovery and status rules are duplicated in this gate. They should be
  shared once both land.
- **Attest's G3 is separate.** It runs `tests/redteam` in the attest repo and is
  not affected by this job.
- **No coverage figure.** Counts are counts of tagged test methods.
