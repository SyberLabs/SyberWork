# Plan: a company pays because SyberWork solved a problem it has

Status: **rehearsal implemented**. Gates 0, 2, 3, and 4 are not implemented. They need a real workflow, a price, a license choice, and a pilot cell. This document still names no customer.

**Goal.** A company that is not SyberLabs pays money, and the reason they state is a problem whose failure mode SyberWork's recorded behavior prevented. A payment for a roadmap, a workshop, or "an AI coding platform" does not meet the goal.

**Architecture of the plan.** Sell the license to execute. Do not sell unmeasured search. The repository can already show that a proposal is admitted or denied by a named rule, that a model does not hold the effect credential, and that an unknown external write stays unknown until reconciliation. It cannot yet show that evolutionary search beats one model patch. The first payment is for the first capability, on one workflow the buyer already runs. Search stays a separate evidence track and is not on the critical path.

**Constraints copied from the repository.**

- Protocol `sdk.syberlabs.space/v0alpha1` is frozen. A pilot does not add event kinds to it.
- `syberlabs` does not import `syberwork`. A pilot that only needs local effects uses `syberlabs.Session`. HTTP effects and economic reservations use `syberwork.Work`.
- Secrets stay in environment names on installed action definitions. They do not enter the contract, the case, or the prompt.
- A model origin cannot promote. `candidate.promotable` requires a human origin, no automation role, and the host's evaluation of that candidate's exact tree.
- Checks in `syberlabs/checks.py` are a controlled worktree, not a sandbox.
- There is no `LICENSE` file. Counsel at a paying company will stop on that. Choosing one is an owner decision, recorded in `docs/ROADMAP_STATUS.md`.
- There is no SSO, no multi-tenant database, and no fleet control plane. A first pilot is one cell, local credentials, and their staging system.
- The procurement case study, the access review, and the release gate use fictional or local data. None of them is a customer deployment.

## 1. The problem this repository can defend

From `docs/SDK_FEEDBACK.md`, which is a capability comparison and not a market survey:

> A host application that already has its own data, and needs a versioned contract, a global policy, a recorded proposal, and a hash-chained history before a local or HTTP effect.

The worked shapes are already in the tree:

| Example | Effect the contract governs | What a model is not allowed to do |
| --- | --- | --- |
| `examples/access_review.py` | Certify a roster, approve a revocation, auditor signs | Skip the prior effect, or treat an unverified roster as a fact |
| `examples/release_gate.py` | Record checks, then publish | Publish without the prior effect |
| `case_studies/enterprise_procurement.py` | Issue an order against a fresh quote, site, and budget | Write the ERP without approval, or treat an unknown HTTP result as success |
| `docs/BUILD_THREAD.md` | Accept a candidate onto `refs/heads/syberlabs/<thread>` | Count the model's own "tests passed" as the evaluation, or push as part of accept |

A company has this problem when all of the following are true:

1. The effect already exists in a system they operate (an order, a revocation, a publish, a production change).
2. The wrong effect is expensive in money, access, or an audit finding they have already had or already fear.
3. A person or a model is about to be allowed to propose that effect, and they do not want that proposer to hold the credential that performs it.
4. They can name the fact that must be fresh, the role that must approve, and what "the write might have happened" means for their API.

If a conversation does not produce those four sentences in their nouns, they do not have the problem this plan sells. "We want agents to write code faster" is a different product. Cursor, Claude Code, and a hosted sandbox already sell that. This plan does not.

## 2. The problem this repository must not sell yet

`docs/MODEL_COMPARISON.md` and `docs/ROADMAP_STATUS.md` agree: whether search beats one patch at the same model cost is **not measured** on a real model. The harness `benchmarks/model_vs_patch.py` has been run only against `benchmarks/simulated_model.py`. Those numbers describe the harness.

Evaluation worlds phase 1 records that a host bound an evaluation to a world definition. `instance_equivalence` is `unverified` or `impossible`. It does not show that two candidates experienced the same world. Do not tell a buyer that sibling candidates were compared under controlled external state.

Do not sell:

- a coding-agent orchestrator
- a cloud sandbox or microVM
- a visual Builder
- multi-tenant SaaS
- "evolutionary search is more effective than a patch"
- a benchmark on arbitrary repositories as the thing they are buying

Arbitrary-repository benchmarking stays off the critical path. It becomes relevant only if a buyer’s paid workflow is governed code change and their checks are local argv. Section 6 says when that track starts. It does not start in order to find the first buyer.

## 3. Gates

Each gate has an exit artifact. Later gates do not start by building around a failed exit. No calendar duration is attached. A gate is blocked until its artifact exists.

### Gate 0. Name the workflow in their words

**Owner:** SyberLabs, in a conversation. Not an agent inventing a persona.

**Exit artifact:** one page, stored outside the product repo if it contains their names, with:

- the effect, the system of record, and who performs it today
- the fact that must be fresh, and how they know
- the approval that is required, and who may not approve their own proposal
- the last time an effect was wrong, unknown, or unauditable, or the control they already pay for to prevent that
- the sentence they would use to describe success, in their nouns
- whether they will discuss a price against a pilot of the current software

**Stop.** If they will not discuss price, or the workflow fails the four tests in section 1, stop. Do not write a contract for them yet. Do not open a Builder or sandbox workstream to make the conversation more impressive.

**Pass.** They named an effect, a fact, an approval, and a price conversation.

### Gate 1. Replay their workflow on the current SDK

**Owner:** an engineer, against their staging or a redacted fixture they confirm.

**Files to copy, not to generalize:**

- Local effects only: start from `examples/access_review.py` or `examples/release_gate.py` on `syberlabs.Session`.
- An HTTP write to a system they run: start from `syberwork.Work` and `examples/reference_system.py`. Install their source and action. `auth_env` names the secret. `trusted_origin` and `guard_request` constrain the URL.
- A Git acceptance: start from `docs/BUILD_THREAD.md`. Accept moves `refs/heads/syberlabs/<thread>` only. Push is a separate admitted effect.

The public stand-in, with no customer identifiers, is `PYTHONPATH=. python examples/pilot_rehearsal.py`. It records `actor_role_missing`, `approval_required:manager`, `source_verification_required:roster`, `required_prior_effect_missing`, `evaluation_denied` when the registering actor evaluates a candidate, and an effect that stays `unknown` with no `effect_succeeded`. `tests/test_pilot_rehearsal.py` locks that report. Their replay replaces those nouns. It does not add an event kind.

**Exit artifact:** a case history they can verify, plus `explain_admission` for at least these denials, using their identifiers:

- proposer lacks the approval role
- required fact missing or unverified
- required prior effect missing
- the actor who registered a candidate evaluates it, if the workflow is a code change
- an HTTP effect whose destination did not confirm, left `unknown`, and was not recorded as success

**Tests.** Their replay is a script under `examples/` or a private repository only if it contains their data. The public tree gets a synthetic twin with no customer names, following `case_studies/enterprise_procurement.py`: fictional data, real HTTP, hash chain checked. `PYTHONPATH=. python3 -m unittest discover -s tests -q` and `PYTHONPATH=. python3 -m conformance.run` stay green. The frozen protocol does not gain an event kind.

**Stop.** If their effect needs retries, worker failover, or a policy language they already operate (OPA, Cedar), `docs/SDK_FEEDBACK.md` already says not to start here. Refer them to that system. Do not wrap it.

### Gate 2. Ask for money before more platform

**Owner:** SyberLabs. The price is theirs. This plan does not invent one.

**Exit artifact:** a written pilot whose success criteria are fixed before the work:

- a count of real effects on their staging system
- zero of those effects recorded `succeeded` while the destination was `unknown`
- every denial they care about reproducible by `explain_admission` with a named rule
- the proposing credential cannot perform the effect
- a date on which they either pay, extend in writing, or stop

**Pass.** They sign or pay against those criteria.

**Stop.** If the criteria drift into "developers prefer it," "time to first PR," or "an agent platform," the pilot is no longer this objective. End it.

### Gate 3. The legal grant

**Owner:** SyberLabs counsel. Blocked on Gate 0 only in the sense that a license is useless without a buyer, and a buyer’s counsel will refuse without a license. Do not block Gate 1’s private replay on the public grant. Do block sending them a wheel they would ship.

**Exit artifact:** a `LICENSE` file whose grant covers the pilot. The evolutionary search in this repository is an independent implementation. It must not copy EvoGit code. EvoGit is AGPL-3.0, which is why the license choice is not a drive-by edit.

**Not required for the first payment:** a separate `syberlabs` package on a public index, SSO, KMS, multi-tenant hosting, or a status page.

### Gate 4. Operate one cell through the pilot

**Owner:** the same engineer as Gate 1, on their staging.

**Use what exists:**

- one organization, one database (`docs/INDUSTRIAL_CELL.md`)
- backup and restore, including a complete pre-world snapshot (`syberwork/backup.py`, `SNAPSHOT_GROUPS`)
- inline effects unless they need the worker; an expired lease stays `unknown`
- reconciliation before anyone treats an absent remote write as proof

**Exit artifact:** the pilot’s case ids, the chain verification, and the destination’s own record of the same effects. Those two lists match on success and stay explicitly unmatched on `unknown`.

**Stop the pilot** if a model credential can commit an effect, if a secret appears in a case body, or if a restore of their snapshot loses an authoritative table. Those are product failures, not pilot noise.

## 4. What the first pilot is allowed to change in the repository

Only what their workflow proved missing, and only after Gate 0’s page names it.

Allowed, if the replay fails for a reason a test can state:

- a contract or example that encodes the synthetic twin
- a denial reason that admission should already produce and does not
- a reconciliation status their API actually returns, mapped to the existing unknown / rejected / succeeded outcomes
- documentation of the pilot’s public shape, with their name included only if they agree

Not allowed as part of finding the first buyer:

- Builder visual system
- evaluation-world phase 2, a world provider, or a runtime
- a sandbox, microVM, or browser host
- PostgreSQL `NOTIFY`, SSO, KMS, or a multi-tenant schema
- a corpus runner for arbitrary repositories
- a change to `candidate_evaluated` or to the 23 golden traces

If the replay works with the files in section 3, the repository change for the pilot is the synthetic twin and nothing else.

## 5. The smallest slice that can reach the goal

1. Hold Gate 0 with one company whose effect is an order, a revocation, a publish, or a production change they already make. Write their four sentences down. Ask whether a pilot of the current software is something they would pay for if the denials in Gate 1 hold.
2. If yes, replay Gate 1 on their staging or on a fixture they certify. `examples/pilot_rehearsal.py` is the checklist to copy. Show the denials and one reconciled effect in their nouns.
3. If they still recognize the problem, write Gate 2’s criteria and the price in the same document. Gate 3’s license is the only repository change that must exist before they receive a wheel.
4. Run the pilot. Payment against the written criteria is the goal. Anything short of that is a finding, and the finding goes back to Gate 0 rather than into a new subsystem.

## 6. Where benchmarking sits

Benchmarking is how Offer 2 (governed search) earns the right to be sold. It is not how Offer 1 (license to execute) earns the first payment.

| Measurement | Ready when | Blocks the first payment? |
| --- | --- | --- |
| One real-model repeat of `benchmarks/model_vs_patch.py` on the three fixtures | An API credential and a budget. The command is already in `docs/MODEL_COMPARISON.md`. | No |
| One repository the team trusts, with an evolution contract and local check argv | That contract exists and the base revision passes the checks. | Only if the buyer’s workflow is code acceptance |
| Arbitrary repositories | A contract template bound to an existing test command, a corpus runner that does not promote, and a decision that `syberlabs/checks.py` may run that argv on the host. Untrusted argv needs a runtime this plan does not build. | No |

Run the one-repeat model measurement when a credential exists. Record it in `docs/MODEL_COMPARISON.md` as a measurement of those fixtures, not of arbitrary code. Do not start a corpus in order to create a buyer. Start it only after a buyer’s paid workflow is code change and their checks are local.

Arbitrary-project benchmarking is ready for repositories you trust when Gate 1 for that repository is a Build Thread contract whose checks pass on the base. It is not ready for untrusted repositories, and it is not ready for projects whose checks need network credentials or a live external service. Those need the scrubbed environment to be a deliberate policy and, for external services, evaluation-world phase 2. Neither is on the path to the first payment unless Gate 0 names them.

## 7. Failure modes

| Failure | What it looks like | What to do |
| --- | --- | --- |
| Invented buyer | The workflow is Northstar, the access review, or a persona | Gate 0 is not passed |
| Selling search | The pitch leads with evolution, worlds, or a benchmark | Return to section 1. Search is unmeasured |
| Platform detour | SSO, sandbox, or Builder UI starts before a signed pilot | Stop. Those do not create the problem |
| Pilot criteria drift | Success becomes satisfaction or speed | End the pilot. That payment would not meet the goal |
| Secret in the case | A token appears in an event body | Stop the pilot. The product rule already forbids it |
| False success | An unknown HTTP result is recorded as `succeeded` | Stop. Reconciliation is the feature they would be paying for |
| License surprise | Their counsel asks what they may do with the code | Gate 3. Do not improvise a grant in a pull request |
