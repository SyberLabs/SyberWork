# SyberLabs SDK protocol v0alpha1

Protocol identity: `sdk.syberlabs.space/v0alpha1`. These schemas describe the objects SyberWork already accepts and emits. They do not add checks the runtime does not perform. Where the prose below is stricter than a schema, the schema is authoritative for the wire shape and `GAPS.md` records the difference.

| Object | Owner | What it is | Invariants the runtime already keeps |
| --- | --- | --- | --- |
| Contract | Workflow owner | Immutable `(id, version)` document: inputs, action rules, acceptance, optional bindings, resolutions, and a compiled path | A published version cannot change bytes. Cases stay pinned to the version they were created with. The contract cannot add an action the global policy does not list. |
| Policy | Organization | Versioned allow-list of actions, with roles, optional approval, and optional `max_amount` | The active version governs new admissions and commits. A contract cannot widen it. |
| SourceDefinition | Integration owner | Named HTTP read connector | The URL is fixed at install. Callers pass a record key, not a URL. Secrets stay in the environment. |
| ActionDefinition | Integration owner | Named local record or HTTP write, plus an optional same-origin status lookup | Definitions are immutable under a name. HTTP methods are writes. No-write statuses are only 409, 412, or 428. |
| Observation | Source system, recorded by the runtime | One `observed` event: key, value, source, version, actor, verified | A human assertion is `verified: false`. Only a connector read is verified. Later observations of the same key supersede earlier ones for admission. |
| Proposal | A human, compiled, or model proposer | `{action, args}` plus actor, roles, and origin | Origin `model` or `compiled` requires that role. The proposal is history. It is not authority. |
| AdmissionDecision | Runtime | `status` of `allowed`, `needs_approval`, or `denied`, plus a reason code | This is the only license for an effect. `allowed` does not mean the external write happened. |
| EffectOutcome | Source system, recorded by the runtime | `state` of `succeeded`, `rejected`, or `unknown` | `rejected` is only an adapter-declared no-write HTTP status. Anything else uncertain is `unknown`. Unknown blocks another effect of that action. |
| Reconciliation | Source system, checked by the runtime | The only path from `unknown` to a verified result | The destination must show `committed`, the original idempotency key, the request digest, and a nonempty external id. A caller's success flag is refused. A 404 stays pending. |
| AcceptanceClause | Workflow owner | `effect`, `signoff`, or `fact` | An effect clause passes only on `effect_succeeded` or a verified reconciliation. A signoff before the required effect does not pass. |
| ResolutionTask | Workflow owner declares it; the source system decides it; the runtime records the task | Open decision that blocks named actions until a changed, versioned source record matches a captured choice and the case | Choosing a value in SyberWork does not complete the task. Cancellation closes the case in SyberWork and does not undo an external write. |
| Event | Runtime | Envelope `case_id`, `seq`, `kind`, `body`, `at`, `previous`, `hash` | `hash` is SHA-256 of canonical JSON over the other digest fields. Canonical JSON is sorted keys, compact separators, and non-ASCII preserved. The chain is integrity evidence inside the current database, not a defense against an operator who rewrites every row. |

Admission and effect stay separate. A decision event records whether the action was legal. An effect event records what the destination did. Replay compares admission status and reason against a historical prefix and does not call the destination.
