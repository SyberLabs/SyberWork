# SyberLabs Agent protocol v1alpha1 (seam S0)

Protocol identity: `sdk.syberlabs.space/v1alpha1`. **Draft.** This directory specifies the one Agent type that the SyberLabs repositories converge on. It is additive to the frozen `sdk.syberlabs.space/v0alpha1` protocol in the parent directory: it reuses every v0alpha1 object by reference, adds no event kind, and changes the shape of nothing v0alpha1 records. All 23 golden traces, and every raw event behind them, validate under v1alpha1 unchanged (`tests/test_spec_v1.py`).

The design and the repository review behind it are in the SyberLabs consolidation proposal (2026-11-01). The seam itself is S0 in MasterMind.

## The law it encodes

> Inference may propose structure. It does not own admission, authority, or the fact that an effect occurred. (MasterMind `CHARTER.md` section 1)

An **Agent is the host loop, not a model.** A model is a port on the Agent: a generator port drafts proposals, a decision port picks from a menu the host built. Nothing a port returns is admission, authority, evidence, or proof of an effect.

## Objects

| Object | Schema | Owner | What it is | Taken from |
| --- | --- | --- | --- | --- |
| Agent | `agent.schema.json` | Host | identity, pinned contract, grant layers, ports, pinned capabilities, one history | this protocol |
| AgentIdentity | `identity.schema.json` | Host | `id, root_id, parent_id, subject_binding, platform` | Turtle `TurtleIdentity`; SyberWork Agent-platform convention |
| GrantRef | `grant-ref.schema.json` | Organization | pinned reference to one Turtle policy layer by digest | turtle-shells `turtle.syberlabs.space/v0alpha1`, which owns grant semantics |
| Capability | `capability.schema.json` | Integration owner | typed tool: ports, effect class, transport, credential slot, connection, digest | Flyspace `CapabilityManifest`; cross-platform `Capability` and `Port`; omni-agent `Affordance` |
| Port | `port.schema.json` | Integration owner | one typed input or output, versioned value type | cross-platform `Port` |
| DecisionCall | `decision-call.schema.json` | Host | one bounded call: provider pin, connection, deadline, System One request | RISE `decision/call.js`, Flyspace `jev-persona`, KevRT contract |
| DecisionOutcome | `decision-outcome.schema.json` | Host | admitted answers or one refusal code | RISE `DecisionError` codes plus `OUT_OF_MENU` |
| Ask | `ask.schema.json` | Host, answered by a human key | request for `approve`, `direction`, `keys` or `tiebreak` | MasterMind asks; SyberWork `approved` |
| Evidence | `evidence.schema.json` | Host | graded claim with sources, optionally sealed | charter evidence states; sectors `scar` seal |
| EventAttribution | `attribution.schema.json` | Runtime | which Agent wrote an event, and derived code provenance | cross-platform `SyberEvent` provenance |
| Event | `event.schema.json` | Runtime | the v0alpha1 envelope, by reference | v0alpha1 |

Contracts, policies, proposals, admission decisions, effect outcomes, reconciliations and acceptance clauses are the v0alpha1 objects, unchanged.

## The loop

Observe, propose, admit, ask, commit, effect, settle, reconcile, accept, as v0alpha1 already records them. v1alpha1 adds:

- **Spawn.** An Agent may create a child whose identity has `parent_id` set and whose grant layers are the parent's plus at least one narrower layer. Narrowing is checked by `turtle policy diff` (exit 2 if authority may widen), not by this protocol.
- **Decide.** A DecisionCall's outcome enters the case as a proposal or an observation, never as a decision event. A `decision` event is still only admission's verdict.
- **Attribute.** Every event written by an Agent has an EventAttribution side record, stored beside the envelope like the JCS digest and deciding rule id, outside `hash`.

## Invariants

1. Inference never admits, authorizes, or asserts that an effect occurred.
2. Requests carry no authority. Authority comes only from grant layers in trusted context.
3. A child's grant is a subset of its parent's, clause by clause.
4. An approval binds to the digest of the exact proposal, and the approver is not the proposer. An `approve` Ask never takes effect on silence; a `keys` Ask may, but only with the option that performs no effect (MasterMind ASK-009 is the model).
5. Unknown is a state. An uncertain effect is never retried automatically.
6. A decision answer outside the host's menu is refused (`OUT_OF_MENU`), not repaired. A Kev answer without the pinned revision is refused (`UNATTESTED`).
7. One case has one history. Nothing is mirrored into a second ledger.
8. Evidence grades are never promoted by wording. A grade above `tested` names a source. A model's signal is never a source.
9. A Capability never grants itself authority: `write` and `destructive` cannot be `auto`-approved, and approval is outside its digest.
10. The Decision Port never chooses who owns a credential: `connection` is recorded, not decided.

Schema-checkable invariants are enforced by the schemas and exercised by `fixtures/`. The rest are runtime rules, listed with their owners in `GAPS.md`.

## Fixtures and checks

`fixtures/<object>.json` holds `valid` and `invalid` instances, each named for the rule it shows; `capability.json` also holds a `digest_mismatch` case. Instances are drawn from real systems where one exists: Flyspace's Kev persona call, RISE's Jev recommendation, the Kev local warmup, one-shot-factory, cyber-insectly, `rise_present`, omni-agent's `tab.click`, MasterMind asks.

```sh
PYTHONPATH=. python3 -m spec.v1.check --golden     # fixtures, digests, and v0alpha1 golden events
PYTHONPATH=. python3 -m unittest tests.test_spec_v1
```

## What this draft does not do

It adds no runtime code. `syberlabs` and `syberwork` neither import nor enforce v1alpha1 yet. Shared code is earned only when two systems pass the same fixtures (charter section 5, Seams); the first such step is the Decision Port, consumed by RISE and Flyspace as separate packets after S0 is admitted.

Editing these files floors at direction under SyberWork's path policy; the owner's merge is the admission.
