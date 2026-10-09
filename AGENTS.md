# Agent operating principles

This file is for every agent working on SyberWork (Claude Code, Codex, Cursor,
or any other agent that reads `AGENTS.md`) and for humans. It carries the
SyberLabs rules that apply to every task, then this project's own notes.
Precedence: the human's direct instructions, this file, `CLAUDE.md`, defaults.

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

A Build Thread (SyberWork #21, #23, #24) writes them from the record, on a
`syberlabs/<thread>` branch with no platform prefix: `Class` from `start
--class` (default R), `Agent-platform` as `name@revision`, `Admitted-by` as
the promoting actor, plus `Approved-by` and an `Objective:` label
(`syberlabs/publish.py:attribution`). The class is recorded, not enforced:
neither the kit nor the `main` ruleset requires an approval.

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

The non-obvious things; standard commands are in `README.md`.

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
- Nothing to install for development; run from the checkout with `PYTHONPATH=.`.
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
- CI runs CPython 3.11–3.13 with PostgreSQL, a Compose cell boot, and Windows.
  **Windows is a primary development platform**: do not name a bare interpreter
  or shell builtin in a check or a default, and do not assume `fcntl`,
  `os.setsid` or POSIX file locking exists. The suite must pass on a default
  Windows install, not only on `windows-latest`.
- A golden trace difference is a behavior change. Either it is intended, and you
  add or update traces deliberately in the same pull request and say so, or it
  is a regression.

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

## Before doing work

1. Read `README.md` for what the system does today.
2. Read `docs/ARCHITECTURE.md` for the owners, the state transitions and the invariants.
3. Read `docs/BUILD_THREAD.md` if you touch `syberlabs/build.py`, `checks.py`, `gitspace.py`,
   `providers.py`, `publish.py`, or `evolve.py`.
4. Read `spec/SPEC.md` and `spec/GAPS.md` before changing anything on the wire. `GAPS.md` records
   every place the schemas and the code deliberately differ; if your change closes or widens one,
   update that entry in the same pull request.
5. Classify your act **R / C / P / X** (below) before you start. Class R self-admits. Class C, P
   and X require a named human key, and you may only draft them.
6. Inspect the current `main`, the open pull requests and the CI state before proposing. Do not
   infer them from a document.

## Invariants you may not weaken

1. Admission is one ordered list of named rules in `syberlabs/admission.py`, with one reason code per
   rule, called identically at propose, commit and replay. The deciding rule id stays out of the
   hashed decision body.
2. A provider's claim is never evidence. Only the host's own evaluation of a candidate's exact tree
   counts, and only its newest one. Lineage is provenance.
3. `candidate.promotable` requires a human-origin proposal from a promotion role. An actor holding
   `model`, `compiled` or `search` cannot promote, whatever origin it claims.
4. An effect is claimed durably before any I/O, with the proposal id as its idempotency key. An
   unknown outcome stays unknown until the destination says otherwise. A caller may not assert
   success.
5. Accepting moves one non-authoritative branch by compare-and-swap. It never pushes, merges, or
   touches the working tree. Pushing, opening a pull request and running a publish command are
   separate admitted actions.
6. Published contracts, policies, actions and sources are immutable under their identity. Changing
   behavior means a new version or a new name.
7. Secrets stay in the environment behind `auth_env` and `token_env` names. Destination failures are
   stored as codes, never as exception text that may carry a URL.
8. Checks run in a temporary worktree with a scrubbed environment (`syberlabs/checks.py`). Publish
   commands also run in a temporary worktree but inherit the caller's full environment, and so do
   provider commands (`publish.py`, `providers.run_json`). **None of this is a sandbox.** Do not
   describe it as one, and do not widen what a provider process receives.

## Scope discipline

This repository has more subsystems than consumers. These are reachable from the CLI, the package,
the benchmarks or the case study, but no consumer outside this repository is named for them:
`syberlabs/evolve.py` and `exchange.py` (evolutionary search and multi-host migration), the
`economic_http` action path, `resolutions` in `syberwork/core.py`, `syberlabs/mappings.py`, and the
`reference_system.py` ERP with its case study. Keep them green and correct. **Adding scope to them,
or adding a new subsystem, is a Class C act that needs an owner decision and a named first
consumer.**

The current objective for this repository is to be used by SyberLabs' own agent fleet before anyone
else. A change that serves a hypothetical external operator and no internal consumer is not in scope.

## Work-package rule

One target repository, one branch, one pull request, one bounded objective, independently testable,
revertible without rolling back unrelated work. Do not bundle a protocol change, a new subsystem and
a refactor in one pull request.

## Handoff

Every completion leaves: what changed; the exact files; the tests and measured results with the
commands that produced them; known failures and limitations; unresolved questions; the next
permissible work package; and the branch and pull request identifiers.
