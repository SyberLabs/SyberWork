# Architecture and invariants

## Owners

| Owner | Artifact | Changes independently |
| --- | --- | --- |
| Organization | Versioned global action policy and credential roles | Yes; active policy governs new admissions and commits |
| Workflow owner | Immutable versioned work contract | Yes; running cases remain pinned |
| Integration owner | Immutable named HTTP source and action definitions | New names for changed integrations |
| Source system | Actual inventory, quotes, purchase orders | Yes; SyberWork records observations and effects |
| SyberWork | Case history, proposals, decisions, and acceptance projections | Append-only at the API boundary |

`syberlabs` holds the parts another runtime can reuse: canonical JSON, digests, the event envelope and its hash chain, a JCS digest of those same fields that is not the chain link, the protocol names, the admission rules, the planner interface, and `Session`, an in-memory case store with the same admission and hash chain. `syberwork` holds this installation: SQLite, the loopback HTTP service, the CLI, the console, source and action connectors, and economic budget rows. Admission is an ordered list of named rules. `economic.reserve` sits after `facts.required` and returns no decision unless the installed action is `economic_http` or the policy action carries an `economic` block. Commit and replay call that same list. `explain_admission` returns the deciding rule, with its module, symbol, and line, and does not store either in the decision event. A planner returns `{action, args}` and receives no executor credential; the proposal it produces is admitted by the same rules as a human proposal. `examples/release_gate.py` is a non-procurement project on `Session`. `syberlabs.mappings.translate_stabilize` translates a SyberRuntime stabilize outcome the caller already has; it does not run that runtime.

The contract defines inputs, declared actions, argument bindings to recorded facts or source versions, required observations, history predicates such as prior completed effects, local approval restrictions, a compiled path, and acceptance clauses. Global policy can deny an action or require an additional approval. The work contract cannot widen global action availability or roles. Case history is causal *within a case*; hash chaining is integrity evidence within the current database trust boundary.

Optional `input_bindings` tie a case input to a path in the latest observed fact, for example `{"request_id":"fact:request.id"}`. Admission rechecks this equality. The relevant action must independently require that fact from a verified, fresh source; the binding alone does not authenticate an observation.

Optional `resolutions` declare a missing decision, candidate source, assigned role, due time, escalation role, authoritative result, and actions to block. The task stores observation hashes and source versions. The assigned owner confirms a changed, versioned source record whose selected option and identity match the task and case. Admission requires task closure and keeps checking the latest result's choice and identity. See [resolution tasks](RESOLUTION_TASKS.md).

## State transitions

`proposed → decision → approved (when required) → effect_started → effect_succeeded | effect_rejected | effect_unknown → reconciliation_checked | reconciled`

Only `effect_succeeded`, or a reconciliation independently verified against the installed destination status lookup, licenses an `effect` acceptance clause. A historical text-only reconciliation does not qualify. A decision of `allowed` does not imply the external write occurred. A signer can attest after the required effect; a prior signature does not satisfy an `after_action` clause. `commit` serializes the recheck and the single effect claim with SQLite `BEGIN IMMEDIATE` on one connection held for the life of `Work`. The network call occurs after the claim. An `effect_started` that has not reached success, an explicit no-write rejection, or a verified reconciliation occupies that action the same way an unknown outcome does. A destination 404 is inconclusive. An explicit adapter-declared no-write precondition status produces `effect_rejected`, allowing a fresh proposal. A signoff whose clause names `after_action` must be a different actor from the one who started the completed effect. See [reconciliation contract](RECONCILIATION.md).

## Evidence and freshness

Human-entered observations are assertions, even if the actor claims a source. The source reader calls a fixed admin-installed URL and records the returned ETag or record version. Contracts can require `verified: true` and a maximum age. For arguments with `fact:quote.price`, the current observation must equal the proposal value; `version:quote` binds to the observed version. The destination should use `If-Match` or an equivalent condition to detect changes between read and write; the included reference integration does.

## Replay

For each past proposal, replay evaluates a selected new contract and policy against the historical event prefix at the proposal's recorded time. It reports changed decisions and acceptance clauses without mutating the case or external systems. Replay of recorded facts cannot prove whether today's source data would produce the same answer. The operator can independently refresh live data.

## Integration

HTTP sources and effects are bound by administrator-installed definitions and do not accept an LLM-provided destination URL. Authorization headers use environment variable names in definitions; secrets never enter the contract. Cross-origin redirects are blocked. Source URLs and the planner URL go through `trusted_origin`. Model suggestions are proposals accepted under separate model-bearing credentials, never direct network effects. The included `reference_system.py` is one fully running integration. `translate_stabilize` is a tested translator for one SyberRuntime outcome, not a live adapter. Barn, OmniOS, and customer tools still need adapters matching their authority and event semantics. The other rows in `spec/MAPPINGS.md` stay proposed.
