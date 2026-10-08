# Agent operating principles

This file is for every agent working on SyberWork (Claude Code, Codex, Cursor,
or any other agent that reads `AGENTS.md`) and for humans. It carries the
SyberLabs rules that apply to every task, then this project's own notes.
Precedence: the human partner's direct instructions, then this file, then
`CLAUDE.md`, then your defaults.

The organizational rules are copied from SyberLabs/MasterMind
(`docs/org/AUTHORITY.md`, adopted 2026-10-02). That record wins if the two
ever differ; fix this file, do not argue from it.

## 1. Classify the act before you start

Authority is assigned by reversibility and audience, not by size. Generation
is free; effects need a gate. The review budget is spent per irreversible act,
not per pull request. Every piece of work is one of four classes:

| Class | What it means here | Who admits it | An agent may |
|---|---|---|---|
| **R — Reversible / internal** | Code, tests, docs, refactors, dead-code deletion, a dependency bump inside this repository, revertible by one commit. Examples: a typo or wording fix in `docs/`, a new unit test, a change to an example contract JSON under `examples/`, a bug fix in `syberlabs/admission.py` with its test. | **The agent self-admits.** No human key. | propose and merge |
| **C — Canonical** | Changes organizational state: a locked stance, the project registry, a gate definition, this `AGENTS.md`, a repository's existence, an architectural seam. Examples: changing the frozen protocol vocabulary in `spec/` (`sdk.syberlabs.space/v0alpha1`), splitting `syberlabs` into its own distribution, the rule that `syberlabs` never imports `syberwork`, editing this file. | **Human key: Mateo.** Seth may hold the key for purely technical canon on shared repositories. | draft only |
| **P — Public claim** | Anything a reader outside SyberLabs can take as a claim: `README.md` positioning, release notes, a published report or benchmark number, a paper. Examples: a line in `README.md` saying what the Build Thread guarantees, a release note, publishing a number from `benchmarks/`. | **Human key and an evidence grade.** Both required; neither is enough alone. | draft only |
| **X — External effect** | Reaches a person outside SyberLabs, spends money, licenses code, deploys to production, enrolls a participant, sends a message. Examples: publishing the wheel to a package index, running `benchmarks/model_vs_patch.py` against a real model with a paid API key, enrolling a developer in the Gate 1 quickstart test, pushing to a customer's system. | **A named human executes.** | prepare only; **never execute** |

When the act is Class C, P or X: stop and escalate, with no exceptions for a
small, obvious, or self-correcting change. Escalation is one line: the
blocking fact, the two references that conflict if any, and the smallest
decision that would unblock it. Also stop when two canonical stores disagree
(report both, pick neither), when the source-of-truth behavior of a system is
unclear, or when a model output would become an effect without a host gate.
Most of this repository's volume is Class R and should stay fast.

## 2. The attribution rule

Agents commit under human GitHub identities, so the record cannot tell a
human judgment from an agent generation unless the pull request says so.
From `AUTHORITY.md` section 4:

> Every PR body states the acting agent platform and the human whose key
> admitted it, and every head branch carries its platform prefix
> (`codex/`, `claude/`, `cursor/`, `wp/`). The convention exists informally
> already; this module makes it the condition of merge.

Every pull request body carries these three fields:

```
Class: R | C | P | X
Agent-platform: codex | claude | cursor | human
Admitted-by: <human GitHub handle, or "self (Class R)">
```

MasterMind's pull request template also carries `Propagates-to:` (Class C
and P only: every downstream surface the change invalidates, or "none") and
`Gates-affected:`. The house form in MasterMind pull requests adds a closing
line, `Written by a Claude Code agent at @<owner>'s direction.`, and the
agent's own generated-with trailer. Commits end with a `Co-Authored-By:`
line naming the agent model.

## 3. State the claim ceiling

SyberLabs is strict about the difference between code, tests, measurements,
deployment, and adoption (MasterMind `docs/CLAIMS_AND_EVIDENCE.md`). Every
pull request and every workstream document says which of these it reached:

- **Implemented** — the behavior exists in source code.
- **Tested** — a named automated or manual fixture exercises it.
- **Measured** — numbers exist under documented conditions (hardware,
  sample count, workload, distribution, failure rate, date and revision).
- **Deployed** — exercised in an actual deployment. Staging is not
  production; a personal instance is not a customer deployment.
- **Externally validated** — independent people use it without SyberLabs
  authors operating the system for them.

Never promote one state into another by rhetoric. A passing test shows the
path works in this implementation; it does not show that a new customer's
source systems, payment provider or policies integrate correctly. A valid
model response proves only that the provider returned something the gate
accepted for that case. `docs/ROADMAP_STATUS.md` is the running example of
this discipline: each row is implemented, measured, or proposed.

## 4. Write the smallest correct change

State your assumptions. Decide and proceed when the choice is reversible and
follows from the request; stop and ask when it is hard to reverse or changes
what the user gets. Touch only what the task needs and match the surrounding
style. Turn the task into a check you can run, and never claim a result you
did not see. Use plain words and spell out abbreviations a newcomer would not
know. One owner per part, task, and pull request.

---

# SyberWork project: development notes

The non-obvious things. Standard commands are in `README.md`.

## What this repository is

Two packages, one wheel, one version. `syberlabs` is the reusable SDK core:
canonical JSON, the event hash chain, the HMAC witness and Ed25519 signature
of the chain head, the admission rules, the planner interface, the Build
Thread, and an in-memory `Session`. `syberwork` is the application: storage,
the HTTP API, the CLI, the browser console, and connectors. **`syberlabs`
does not import `syberwork`**; `tests/test_package.py` checks it. The
protocol their objects follow is `sdk.syberlabs.space/v0alpha1` in `spec/`
and is frozen. A release of `syberlabs` under its own distribution name is
an owner decision not yet made (`docs/ROADMAP_STATUS.md`).

## Environment

- Python 3.11 or newer; `pyproject.toml` says `>=3.11`. **No runtime
  dependencies.** The only optional extra is `postgres` (`psycopg`).
- Nothing to install for development. Run from the checkout with
  `PYTHONPATH=.`.
- `import syberlabs` stays small: the Build Thread (`Kit`, `Thread`,
  `Verdict`, `Receipt`) loads on first use through a module `__getattr__`.
  On an interpreter older than 3.11 one `unittest.mock` dotted patch in
  `tests/test_witness.py` trips over this; that is not a supported setup.

## Tests and checks

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -q    # about two minutes
PYTHONPATH=. python3 -m conformance.run                   # golden traces
python3 -m conformance.clean_install                      # wheel in a fresh venv
```

- `conformance/run` recaptures case-study and admission traces and diffs
  them against `conformance/golden`; any difference exits nonzero.
- `conformance/clean_install` fails if `syberlabs` or `syberwork` is
  imported from the checkout instead of the installed wheel.
- The suite includes a case database created on `main` (`a2f909b`); it must
  keep verifying and replaying. Do not regenerate it.
- Economic and publish tests run against local simulators, a bare Git
  remote, and a fake GitHub API. They move no funds and call no real
  service. Keep it that way.
- Making the CI checks required on `main` is a repository setting an agent
  cannot change.

## Models and providers

The `README.md` banner records the provider direction: SyberLabs is migrating
existing bounded decisions from **Jev to Kev**, and this repository has no
active Kev provider call. The model seam is a process boundary
(`CommandProvider`, `CommandMutator`): the adapter for Kev, Jev, or any other
model is the command, and `syberlabs` imports no model SDK. The one real
adapter, `adapters/anthropic_adapter.py`, exists for
`benchmarks/model_vs_patch.py` and has only been run against
`benchmarks/simulated_model.py`. A run against a real model spends money and
is Class X: prepare it, report the exact command and expected cost, and let a
named human execute it.
