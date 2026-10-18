## Admission

<!-- See AGENTS.md. Class R self-admits on green CI; C, P and X need a named human key.
     A Build Thread writes Class, Agent-platform, Admitted-by, Reviewed-by and Evidence from
     its journal (and Approved-by when an approval was recorded); its Reviewed-by names the
     recorded independent approvers or says "none recorded", and its Evidence is tested or
     implemented, never more. The placeholders below are for a hand-authored pull request:
     Reviewed-by is written by an independent session or person who did not author the
     change, and Evidence is the claim ceiling you reached. -->

Class: <!-- R (reversible/internal) | C (canonical) | P (public claim) | X (external effect) -->
Agent-platform: <!-- claude | codex | cursor | human -->
Reviewed-by: <!-- independent session or human, not the author's own -->
Admitted-by: <!-- human GitHub handle, or "self (Class R)" -->
Evidence: <!-- implemented | tested | measured | deployed | externally validated -->

## Objective

<!-- One bounded objective. One repository, one branch, one pull request. -->

## What changed

-

## What did not change

<!-- Name the invariants in AGENTS.md this touches but does not weaken. -->

-

## Evidence

<!-- The commands you ran and what they printed. A golden-trace difference is a behavior change:
     say whether it was intended. -->

```
PYTHONPATH=. python -m unittest discover -s tests -q
PYTHONPATH=. python -m conformance.run
```

## Known limitations

-
