# Builder coordination protocol v0alpha1

Protocol identity: `sdk.syberlabs.space/builder/v0alpha1`.

This protocol is not `sdk.syberlabs.space/v0alpha1`. It does not add case-event kinds, and it does not change the case hash. Schemas live in `spec/builder/`. The runtime that stores them is `syberwork.coordination.BuilderStore`. The pure rules are `syberlabs.builder`, which does not import `syberwork`.

## Planes

| Plane | What it holds | Authority |
| --- | --- | --- |
| Authority | Existing contracts, case histories, admission, candidates, evaluations, effects, approvals, promotion, reconciliation | Yes. Unchanged. |
| Coordination | Generations, approach descriptors, feedback, agent sessions, prototype references, selection records, the coordination log | No. A record may cite a case id, a candidate id, a commit, or a digest. |
| Integrity | Attributed observations with a claim and a verification result | No. Selection reads `verified_independence` only. A digest is not a signature check. |
| Projection | World, Generation, Candidate, Actor, and Integrity views, plus the SSE tail | No. `authoritative` is false. A role lens filters a copy. |

A later promotion still goes through SyberWork admission. The selection record carries an `evidence_digest` a proposal may cite. `promotes_git` is false on every selection.

## Invariants

- A generation cannot launch before it is sealed.
- Sealing requires the configured number of approach descriptors and a passing diversity gate. Rejected approaches do not count.
- After seal, approach descriptors are immutable.
- A candidate link requires a sealed generation and a frozen approach in that generation. The link does not call `record_candidate`.
- Feedback stores every grant that matches the caller's roles and that feedback kind. A person who holds both veto and decision keeps both. The request cannot supply the class. Advisory preferences do not advance a candidate and do not count toward consensus unless `consensus_authorities` lists `advisory`.
- Feedback is not a fitness number. Projections list kind and authority separately. A policy may store an aggregate beside those lists. The aggregate is not the decision.
- A candidate link resolves a `candidate_registered` case event. Changed paths, commit, tree, base, operator, and provider are copied from that event. An id that is not in the case is `candidate_unrecorded` and cannot be selected. Selection marks any link that is not `eligible` as `candidate_unrecorded` and does not advance it.
- Feedback and integrity targets must resolve: the generation belongs to the case, the approach or prototype belongs to the generation, the candidate is linked there, and an architecture node exists in the current snapshot.
- Selection does not promote Git state and does not append a case event.
- Integrity keeps `independence_claim` apart from `verified_independence`. Selection gates read only a `verification_status` of `verified`. `internal` is verified as a self-report. `human_reviewed` is verified when the principal kind is `human`. `host_verified` is emitted only by the host mechanism, which the HTTP API does not call. `external` and `signed_external` are verified only by a registered verifier adapter, which cannot relabel the claim. A SHA-256 digest does not, by itself, verify a signature.
- Isolation is a context manifest. `independent` is the common root plus the agent's own approach. `aware` adds sibling descriptors and not implementations. `collaborative` adds sibling implementation summaries (candidate id, approach id, changed paths) and not private hypotheses.
- Diversity is a structural Jaccard distance plus an explicit distinguishing-claim check. A semantic distance may be stored on the evidence and cannot flip the gate. A failure reason is `approach_too_similar:<sibling-id>`.
- Architecture snapshots are a graph of nodes, edges, and repository paths. Mermaid is refused. Snapshot ids are minted by the cell. Nothing in this package imports a diagram vendor.
- Prototype records are a registry. `PrototypeProvider` may fill a launch reference. Readers use the stored record.
- Agent commands (`pause`, `resume`, `cancel`, `send_context`, `restrict_scope`, `redirect`) are durable. `send_context` does not change the assignment. `redirect` does. The default runtime returns `runtime_not_connected` and does not change the session. No command mutates an OS process in this package.
- Agent sessions do not accept chain-of-thought fields.
- The coordination log is append-only and separate from `events`. Sequence numbers are per case and monotonic. SSE ids are those sequence numbers.
- Role lenses change representation. They do not write a second authoritative state. An `intended_user` lens without an engineer, manager, decision, or admin role omits `hypothesis_summary`, `open_uncertainties`, and `agent_activity_recorded` frames. Omitted stream events keep their sequence numbers, so ids may gap.

## Lifecycle

`drafting` → `sealed` → `launched` → `evaluating` (first agent activity) → `selecting` (a selection record) → `closed`.

Agents may be assigned once the generation is sealed. Candidate links are allowed in `sealed`, `launched`, `evaluating`, and `selecting`.

## HTTP

Existing `/api/cases` routes are unchanged. Builder routes require the same bearer token.

Reads:

- `GET /api/builder/work/:id`
- `GET /api/builder/generations/:id`
- `GET /api/builder/generations/:id/candidates`
- `GET /api/builder/agents/:id`
- `GET /api/builder/architecture/:snapshot` (`?baseline=` adds a diff)
- `GET /api/builder/integrity/:target`
- `GET /api/builder/work/:id/stream`

The stream is Server-Sent Events. `id` is the coordination sequence. `Last-Event-ID` resumes after that sequence. The first frame is a heartbeat comment. A reconnect reads the log in the cell database. The replay window is 200 events. `?once=1` ends after the current window so a client can test the tail without holding the connection. There is no WebSocket.

The current tail is a development transport. Each client polls the coordination log about once a second through the cell's single transaction path, and the response carries `X-Syber-Stream: development-poll`. The database remains the source of truth. That poll is not the production wakeup path. A later revision should use PostgreSQL `NOTIFY` or a SQLite condition, then read the durable rows after the cursor, so Builder traffic does not take the authoritative transaction path on a timer.

Mutations are HTTP POST. Coordination roles (`admin`, `operator`, `engineer`, `manager`, `decision`) create generations, approaches, agents, links, architecture, prototypes, and selections. Any authenticated principal may submit feedback; the policy assigns the authority class. Integrity observations are attributed to the caller (`verifier` must be that principal).

## Storage

Migration `0004` adds the coordination tables to the cell database, SQLite and PostgreSQL. They are included in logical snapshots. A snapshot taken before those files existed still restores: missing table files are left empty. Case hashes are not rewritten.
