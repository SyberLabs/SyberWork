# AGENTS.md — SyberWork operating rules

You are working in SyberLabs' agent infrastructure: the admission kernel (`syberlabs`), the
application and cell (`syberwork`), and the Build Thread, which is how a governed change is made
to a Git repository.

The law this repository exists to enforce, and must therefore obey:

> **Inference may propose. It does not own admission, authority, or the fact that an effect occurred.**

## Mission

Keep admission, evidence and effects correct. Everything else is negotiable.

A model, a search provider, or an automation may propose a candidate. Only a person, or a gate a
person explicitly delegated, admits it. Only a destination decides whether an effect happened.
A change that blurs either boundary is wrong even when every test passes.

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

## Act classes

From the organization's authority rule. Authority follows reversibility and audience, not size.

| Class | What it is, here | Who admits | An agent may |
|---|---|---|---|
| **R — reversible / internal** | Code, tests, docs, refactors, dead-code deletion inside this repository, revertible by one commit | **Agent self-admits** on green CI. No human key | propose and merge |
| **C — canonical** | This file, an architectural seam, the protocol identity, a frozen schema, a new subsystem, a repository setting, a gate definition | **Human key: Mateo** (`sykosyber`); Seth (`sdcarlson`) may hold the key for purely technical canon | draft only |
| **P — public claim** | `README.md` positioning, `NOTICE`, release notes, a published measurement, anything a reader outside SyberLabs can take as a claim | **Human key plus an evidence grade.** Both; neither alone | draft only |
| **X — external effect** | Publishing a package, deploying a cell off loopback, spending money, contacting anyone, moving a real payment through an `economic_http` action | **A named human executes** | prepare only; never execute |

The review budget is spent per irreversible act, not per pull request. Class R volume is fine and
should stay fast. The defect to avoid is a Class C decision hidden inside a large Class R diff.

## Attribution

Agents commit under human GitHub identities, so the record cannot otherwise tell a judgment from a
generation. **Every pull request body states three fields.** A pull request opened by hand also
carries its platform prefix on the head branch (`claude/`, `codex/`, `cursor/`, `wp/`):

```
Class: R | C | P | X
Agent-platform: claude | codex | cursor | human
Admitted-by: <human GitHub handle, or "self (Class R)">
```

A Build Thread writes these for you, on a `syberlabs/<thread>` branch, with the platform in the body
rather than the branch name:

- `syberlabs start --class C` records the class inside the case hash chain. The default contract
  records R when no class is given. A contract with no `act_class` input refuses `--class` rather
  than drop it.
- `Agent-platform` is the provider of the accepted candidate as `name@revision`. Name it with
  `syberlabs propose --name claude --revision <model>`; a working-tree snapshot defaults to `patch`.
- `Admitted-by` is the actor whose proposal promoted the candidate, and `Approved-by` names every
  independent approver the contract required (`syberlabs/publish.py:attribution`).

**The class is recorded and stated, not enforced.** A Build Thread does not refuse a C, P or X
candidate that a `developer` accepts, and its `--actor` and `--role` are the caller's own claims, so a
local kit cannot prove whose key it was. For C, P and X the human key is a named person reviewing and
merging the pull request. The ruleset on `main` requires the CI checks but no approval, so that key is
convention too, not a gate. Making the kit refuse those classes without an independent approval would
change admission, and is an owner decision that has not been made.

## Work-package rule

One target repository, one branch, one pull request, one bounded objective, independently testable,
revertible without rolling back unrelated work. Do not bundle a protocol change, a new subsystem and
a refactor in one pull request.

## Checks

Python 3.11 or newer. No runtime dependencies, and that is a property to preserve: `syberlabs`
imports only the standard library, and never imports `syberwork` (`tests/test_package.py`).

```sh
PYTHONPATH=. python -m unittest discover -s tests -q
PYTHONPATH=. python -m conformance.run          # 23 golden traces; exits nonzero on any difference
python -m conformance.clean_install             # wheel, fresh venv, examples from outside the checkout
```

CI runs CPython 3.11–3.13 with PostgreSQL, a Compose cell boot, and Windows. **Windows is a primary
development platform**: do not name a bare interpreter or shell builtin in a check or a default, and
do not assume `fcntl`, `os.setsid` or POSIX file locking exists. The suite must pass on a default
Windows install, not only on `windows-latest`.

A golden trace difference is a behavior change. Either it is intended, and you add or update traces
deliberately in the same pull request and say so, or it is a regression.

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

## Stop and escalate

Stop, and state the blocking fact in one line, when:

1. the same authoritative state would be owned by two systems;
2. a model output would become an effect without a host gate;
3. a new shared package is proposed only to reduce duplication;
4. the work needs broad implicit memory or hidden cross-tenant context;
5. a domain invariant must be weakened to fit a generic interface;
6. the source-of-truth behavior is unclear;
7. **the act is Class C, P or X** — no exceptions, including small, obvious, and correcting your own
   earlier work;
8. two canonical documents disagree: report the contradiction with both references, do not pick one;
9. a change would make a declared gate unrunnable: re-point or strike it in the same pull request,
   or stop.

## Evidence discipline

State which of these you have, and never promote one into another by wording:

**implemented** — the behavior exists in code · **tested** — a named test exercises it ·
**measured** — there is data under stated conditions · **deployed** — it ran in a real deployment ·
**externally validated** — someone outside SyberLabs used it successfully.

"CI is green" is `tested`. It is not `measured` and never `externally validated`. A number with no
named run behind it does not go in a document.

## Handoff

Every completion leaves: what changed; the exact files; the tests and measured results with the
commands that produced them; known failures and limitations; unresolved questions; the next
permissible work package; and the branch and pull request identifiers.
