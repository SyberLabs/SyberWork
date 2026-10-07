## Admission

<!-- See AGENTS.md. Class R self-admits on green CI; C, P and X need a named human key.
     A Build Thread writes these three fields for you. -->

Class: <!-- R (reversible/internal) | C (canonical) | P (public claim) | X (external effect) -->
Agent-platform: <!-- claude | codex | cursor | human -->
Admitted-by: <!-- human GitHub handle, or "self (Class R)" -->

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

## Claim ceiling

<!-- implemented | tested | measured | deployed | externally validated -->

## Known limitations

-
