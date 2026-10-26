"""SyberEval: pre-registered evaluations of provider configurations, run through the real Build Thread.

    syberlabs eval tasks import-fixtures              # evals/tasks/<id>/task.json + hidden/ from benchmarks/fixtures.py
    syberlabs eval tasks list v3-journal_chain ...    # each task's digest and the set digest
    syberlabs eval register evals/<id>/spec.json      # freeze the spec: evals/<id>/registration.json
    syberlabs eval preflight <id>                     # the deterministic suites, recorded for this commit
    syberlabs eval run <id> [--only T,A,S] [--dry]    # every task x arm x seed, in the registered order

The pieces (``docs/EVAL.md``):

- ``spec``: the eval spec, checked against ``spec.SCHEMA``; ``register`` freezes its canonical digest with the
  digests of its tasks and arms, and ``run`` refuses a spec, task or arm whose digest moved (``spec_modified``,
  ``task_modified``).
- ``tasks``: ``evals/tasks/<id>/task.json`` and its ``hidden/`` directory. The hidden acceptance checks are staged
  by the host into a directory outside the run's repository and appended to the run's contract as required checks
  named ``hidden.<check>``, by absolute path, so the agent's tree, its requests and its candidates never hold them.
- ``arms``: named provider configurations (``single``, ``repair``, ``evolve``, ``evolve_jev``, ``claude-code``)
  built into the kernel's own providers; each has a canonical digest.
- ``runner``: the run, the evidence bundle, the eval's dollar cap, and the preflight gate.

Scoring and reporting are the next packet (``eval-score``). This package runs from a SyberWork checkout: it reads
``benchmarks/fixtures.py`` and the adapters beside it, and it is not in the wheel (``pyproject.toml`` lists the
``syberlabs`` package only, not its subpackages).
"""

from __future__ import annotations

VERSION = "1"  # the eval package's own revision, recorded in every bundle as harness.eval
