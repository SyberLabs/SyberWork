"""SyberEval: pre-registered evaluations of provider configurations, run through the real Build Thread.

    syberlabs eval tasks import-fixtures              # evals/tasks/<id>/task.json + hidden/ from benchmarks/fixtures.py
    syberlabs eval tasks list v3-journal_chain ...    # each task's digest and the set digest
    syberlabs eval register evals/<id>/spec.json      # freeze the spec: evals/<id>/registration.json
    syberlabs eval preflight <id>                     # the deterministic suites, recorded for this commit
    syberlabs eval run <id> [--only T,A,S] [--dry]    # every task x arm x seed, in the registered order
    syberlabs eval score <id>                         # the safety gate, quality and efficiency of every run
    syberlabs eval report <id> [--partial]            # the paired analysis with the decision rule applied

The pieces (``docs/EVAL.md``):

- ``spec``: the eval spec, checked against ``spec.SCHEMA``; ``register`` freezes its canonical digest with the
  digests of its tasks and arms, and ``run`` refuses a spec, task or arm whose digest moved (``spec_modified``,
  ``task_modified``).
- ``tasks``: ``evals/tasks/<id>/task.json`` and its ``hidden/`` directory. The hidden acceptance checks are staged
  by the host into a directory outside the run's repository and appended to the run's contract as required checks
  named ``hidden.<check>``, by absolute path, so the agent's tree, its requests and its candidates never hold them.
- ``arms``: named provider configurations (``single``, ``repair``, ``evolve``, ``evolve_jev``, ``claude-code``)
  built into the kernel's own providers; each has a canonical digest.
- ``runner``: the run, the evidence bundle with every hash of the hidden copy, the spend ledger and the eval's
  dollar cap, and the preflight gate.
- ``gate``: the safety gate computed from a run's journal and bundle, rule by rule.
- ``score``: per run, the gate, task quality on the hidden checks, and the metrics suite; ``index.json``.
- ``report``: tasks paired, the decision rule applied in exact fractions, a sign test and a bootstrap beside it.

The wheel carries this package, but an eval runs from a SyberWork checkout: it reads ``benchmarks/fixtures.py``, the
adapters and ``evals/`` from there.
"""

from __future__ import annotations

VERSION = "2"  # the eval package's own revision, recorded in every bundle as harness.eval
