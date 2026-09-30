# Evaluation worlds

Status: **Phase 1 implemented**. Phases 2–5 remain proposed. Phase 1 does not change `sdk.syberlabs.space/v0alpha1` or migration `0004`. Migration `0005` adds world tables with `CREATE TABLE IF NOT EXISTS` and does not alter `builder_generations`. A host binding cites a `candidate_evaluated` event. It is not an integrity class, it does not enter selection, and it does not claim the candidate experienced the world.

SyberWork should stay the governance, evolution, evidence, and authority layer. Persistent computers and simulated external services are replaceable infrastructure. Adopting either product wholesale would collapse the cell into a coding-agent orchestrator or a sandbox vendor. Each recommendation below is labeled **KEEP**, **ADAPT**, **INTEGRATE**, **ABSTRACT**, **DEFER**, or **REJECT**.

Sources for the outside products are their public descriptions, not a run of their software: [Islo sandboxes](https://islo.dev/sandboxes/), [security](https://islo.dev/security/), [gateway](https://islo.dev/gateway/), [BYOC](https://islo.dev/blog/introducing-islo-byoc/), and [islo-labs/doubleagent](https://github.com/islo-labs/doubleagent). Claims about those products are **INFERRED** from those pages. Claims about this repository are **KNOWN** from the files named below.

## 1. Current SyberWork capability map

Four planes already exist and must stay distinct (`spec/BUILDER.md`).

| Plane | What it holds today | Authority |
| --- | --- | --- |
| Authority | Contracts, case hash chain, admission, candidates, evaluations, effects, approvals, promotion, reconciliation | Yes. Frozen as `sdk.syberlabs.space/v0alpha1`. |
| Coordination | Generations, approaches, agents, feedback, prototypes, selections, coordination log | No. `sdk.syberlabs.space/builder/v0alpha1`. |
| Integrity | Attributed observations. Selection reads `verified_independence` only | No. A digest is not a signature check. |
| Projection | World, Generation, Candidate, Actor, Integrity views, plus a development SSE poll | No. `authoritative` is false. |

**KEEP** the case chain. Event kinds, including `candidate_evaluated`, live in `syberlabs/protocol.py` and `spec/event-body.schema.json`. `evaluation_record` (`syberlabs/evolution.py`) accepts exactly `{candidate, commit, tree, evaluator, checks}`. Each check is `{name, state, exit_code, duration_ms, output_digest, output_tail}`. `passed` requires exit code 0. Admission (`evaluation_denial`, `promotion_denial`) uses the newest evaluation of that candidate's registered commit and tree. Parent evaluations, lineage, and provider `signal` are unused. A model origin cannot promote (`candidate_promotion_origin`).

**KEEP** the check runner as a host mechanism, and keep its stated limit. `syberlabs/checks.py` runs declared argv in a detached temporary worktree, with a scrubbed environment (`PASS_ENV` plus a candidate id), a timeout, and an output cap. The module says this is a controlled environment, not a sandbox. The evaluator name is `syberlabs.checks/1`. `environment_note()` returns the host platform and is not part of the evaluation body. `Work.record_evaluation` requires an `evaluator` credential with no automation role, and refuses the actor that registered the candidate.

**KEEP** the generation barrier. `normalize_generation` stores `case_id`, `objective`, `base_revision` (a string, not checked against Git), `mode` (`explore|refine|harden`), `isolation`, diversity threshold, minimum approaches, and a selection-policy id and version. The table is `builder_generations` in migration `0004`. States run `drafting → sealed → launched → evaluating → selecting → closed`. Seal freezes descriptors. A candidate link requires a `candidate_registered` case event and copies `{id, commit, tree, base, operator, provider, changed_paths}` into `builder_candidate_links.authority`. Linking does not call promotion. `evaluate_selection` always sets `promotes_git` false.

**KEEP** structural diversity and isolation manifests. Distance is Jaccard on architecture tokens plus distinguishing claims (`structural_distance`, `diversity_evidence`). `context_manifest` is the permission object: `independent` is the root plus the agent's approach; `aware` adds sibling descriptors; `collaborative` adds sibling implementation summaries (candidate id, approach id, changed paths), not hypotheses.

**KEEP** integrity classification. `assess_integrity` separates `independence_claim` from `verified_independence`. `internal` verifies only as `self_report`. `human_reviewed` verifies only when `principal_kind` is `human`. `host_verified` is recorded by the host mechanism (`record_host_integrity`); HTTP does not call it. `external` and `signed_external` go through `IntegrityVerifier.verify`, which cannot return a different independence class. A 64-hex digest is required for `signed_external` and verifies nothing by itself. `_verdict` matches only `verification_status == verified`, the required class, and `result == "pass"`.

**KEEP** the credential boundary that already exists for effects. Installed action and source definitions store `auth_env`, a name. `syberwork/core.py` reads the secret from the process environment at effect time. Planner context is the objective, allowed actions, arguments, required facts, and acceptance, without fact values or credentials. `trusted_origin` and `guard_request` constrain the HTTP target.

**KEEP** the command contract, and keep the runtime disconnected. Commands are `pause`, `resume`, `cancel`, `send_context`, `restrict_scope`, and `redirect`. `command_effect` describes coordination changes. `send_context` records activity and does not change the assignment. `redirect` changes the assignment and must name a frozen approach in the same generation. `DisconnectedRuntime.apply` returns `{applied: false, reason: runtime_not_connected}`. The `AgentRuntime` protocol and the `PrototypeProvider` class were removed because they had no second implementation. Activity roles are required. A cancelled or paused agent cannot post `complete`.

**KEEP** architecture snapshots as a graph of nodes with repository paths (`validate_snapshot`). Mermaid is refused. Snapshot ids are minted. Prototype rows store `environment`, `provider`, `endpoint`, `artifact_ref`, and `digest` as a registry. They do not provision anything.

What a generation does **not** freeze: an execution environment, fixture or database state, external service state, a network policy, or an evaluation suite beyond the contract's declared checks. Sibling candidates can be evaluated on different hosts, at different times, against whatever the check process can reach. The statement the cell can defend today is "candidate C's tree passed named checks under evaluator E." It cannot defend "C and its siblings experienced the same world."

## 2. Islo concepts relevant to SyberWork

Islo, as publicly described, is a persistent computer for an agent: its own kernel, a long-lived filesystem, snapshots that can include database state, a browser and installed tools, and a network gateway that injects credentials on the way out. The agent holds a binding. The gateway holds the secret, applies host, path, and method policy, and logs allowed and denied calls. Bring-your-own-cloud keeps the computer and the injected credentials in the customer's network while a control plane can stay elsewhere.

| Islo idea | SyberWork stance | Why |
| --- | --- | --- |
| Persistent per-agent computer, microVM, own kernel | **REJECT** as a SyberWork subsystem | VM lifecycle, image builds, and kernel isolation are commodity infrastructure. Competing there abandons the cell's thesis. |
| Long-running session that survives disconnect | **ABSTRACT** | The cell already has a durable agent session and a command log. Liveness belongs to a runtime adapter. The coordination row stays the assignment. |
| Snapshot of filesystem and database | **ADAPT** | The world definition names `snapshot_ref` for operations and `snapshot_digest` for identity. A provider label such as `snapshot-17` does not prove the bytes. SyberWork stores both. The provider stores the bytes. |
| Credential injection outside the agent | **ADAPT** | This matches `auth_env`. Generalize the name from "effect secret" to "capability binding" without ever putting the secret in a case body, a coordination row, or a prompt. |
| Outbound allow/deny and an exportable call log | **ADAPT** as evidence | The policy and the log digest are world facts. The firewall is not ours. A denied call is useful integrity evidence only if a party other than the acting agent records it. |
| LLM judge on egress | **REJECT** | A model must not acquire authority by classifying its own or a sibling's traffic. Policy is data, checked by a verifier. |
| Browser and installed tool images | **DEFER** | Useful inside a runtime. The cell should not host browsers. |
| Human gates and failure routing in a software factory | **KEEP** the ones we have | Admission, approval roles, veto, and dissent already route failure. Do not add a second workflow engine. |
| BYOC: computers stay in the customer network | **DEFER** | The right long-term deployment, once a runtime adapter exists. Not a reason to build a control plane. |

## 3. DoubleAgent concepts relevant to SyberWork

DoubleAgent, as publicly described, is a stateful fake of external APIs (GitHub, Slack, Stripe, and others) that speaks the official SDK, is contract-tested against the real API, and can be reset, seeded, and observed (`/_doubleagent/reset`, `/_doubleagent/seed`, `/_doubleagent/events`). Each agent gets an isolated world. There is no rate limit because the world is local.

Related work that should not be copied: hosted scoring of agent traces, and capture-only twins whose score is the product. SyberWork already refuses to collapse evidence into a hidden fitness number.

| DoubleAgent idea | SyberWork stance | Why |
| --- | --- | --- |
| Service state machine instead of a canned mock | **ABSTRACT** behind a world provider | The cell needs reproducible external state. It does not need to implement GitHub. |
| Contract tests of the fake against the real service | **DEFER** to the provider | That is how a world provider earns trust. SyberWork records the provider name, the provider revision, and the world-definition digest. It does not re-run the provider's contract suite. |
| Reset, seed, snapshot, event log | **ABSTRACT** | These are provider operations. Comparison of the observations they return stays in SyberWork. |
| Official SDK compatibility | **REJECT** as a SyberWork concern | Candidates talk to whatever URL the world gives them. The cell never imports a vendor SDK. |
| Isolated world per agent | **ADAPT** | Siblings share one world *definition* and, later, receive isolated *instances* of it. A shared definition digest does not by itself mean they experienced the same state. One shared mutable instance would let candidate A poison candidate B. |
| Hosted trace scoring | **REJECT** | Selection already lists reasons. A score would hide veto, dissent, and missing evidence. |

These four distinctions govern every later section:

```text
world identity           ≠  integrity independence
declared world           ≠  experienced world
same definition          ≠  same mutable state
snapshot reference       ≠  snapshot proof
```

Integrity classes answer who verified a claim. A world digest answers which definition was named. A host binding answers that the evaluator mechanism attached an evaluation event to that definition. An instance digest, present only after a runner consumes the definition, is what would support a claim that the candidate executed inside it. Phase 1 records the first two and does not pretend to record the third.

## 4. Own, integrate, or defer

| Capability | Owner | Label |
| --- | --- | --- |
| Case history, admission, effects, reconciliation | SyberWork | **KEEP** |
| Generation barrier, diversity, isolation manifests, selection without promotion | SyberWork | **KEEP** |
| Integrity classes (`internal`, `human_reviewed`, `host_verified`, `external`, `signed_external`) | SyberWork | **KEEP** |
| Immutable world definition, content-addressed | SyberWork coordination record | **ADAPT** |
| Generation freezes a definition digest, not a copy of the document | SyberWork | **ADAPT** |
| Host binding from a `candidate_evaluated` event hash to that digest | The evaluator mechanism, same trust path as `record_host_integrity` | **ADAPT** |
| Whether sibling bindings cite that digest | SyberWork projection. No selection effect in Phase 1 | **ADAPT** |
| Check argv, exit codes, output digests | Existing host evaluator | **KEEP** |
| Claim kind versus independence, once a trace exists | A later policy shape. Not an extra integrity class | **DEFER** |
| Creating, seeding, snapshotting, restoring, connecting, observing, destroying a world | World provider | **ABSTRACT**, and **DEFER** until Phase 3 |
| Comparing traces | SyberWork pure function. Providers return observations | **ADAPT** |
| Compute, filesystem, process lifecycle, browser, installed tools | Agent runtime | **DEFER** until a second adapter exists |
| Credential material | Broker outside the agent. The cell stores the binding name, as `auth_env` already does | **ADAPT** |
| Packet filter, microVM, Kubernetes, customer VPC | External infrastructure | **DEFER** |
| `world_bound` as an integrity class | Nobody | **REJECT** |
| Becoming the fake GitHub, Slack, or Stripe | Nobody in this repository | **REJECT** |
| Importing Islo or DoubleAgent as a library | Nobody in this repository | **REJECT** |
| An operator POSTing a binding onto an evaluation that already finished | Nobody | **REJECT** |

**INTEGRATE** means a later adapter may speak a provider's HTTP. It does not mean a dependency in `syberlabs` or `syberwork`. `syberlabs` must not import `syberwork`. A provider adapter, when one exists, lives at the edge the way `CommandProvider` does: a process boundary, no vendor SDK in the core.

## 5. Missing abstractions

Do not add a fifth plane. The objects below are coordination facts. They become integrity inputs only in Phase 2, and only through the existing independence classes.

**ADAPT** `EvaluationWorldDefinition`. An immutable document addressed by the SHA-256 of its canonical form. The generation stores the digest, not a growing embedded object. Two generations can name the same digest. A mutated world is a new document whose `parent_world_digest` names the previous one.

```text
EvaluationWorldDefinition
├── digest
├── provider
│     ├── name
│     └── revision
├── services[]
│     ├── name
│     └── mode: simulated | real
├── seed_ref
├── seed_digest
├── environment_snapshot
│     ├── snapshot_ref
│     └── snapshot_digest
├── network_policy
├── time_policy
│     ├── mode: fixed
│     ├── epoch
│     └── timezone
├── entropy_policy
├── reproducibility: deterministic | snapshot_replayable | externally_mutable | unknown
├── parent_world_digest
└── intent: comparison | mutated | adversarial | historical | customer
```

`provider.revision` is part of the digest. `provider.name = doubleagent` is not an identity: two revisions of the same fake can implement GitHub differently. For a Phase 1 static document the revision is the content hash of the fixture text the operator names, not a running server.

`snapshot_digest` is the proof of a separate artifact. `snapshot_ref` is how a later provider finds those bytes. Null for both means this definition does not name a snapshot. The definition digest is not a legal value for `snapshot_digest`: that field is inside the hashed document, so using the definition's own digest would be a self-reference. A missing toolchain does not hash as "any toolchain."

`time_policy` is the world's clock, not the clock of the evaluation run. Software depends on dates, expiry, and calendars. A fixed epoch and timezone belong in the digest. The wall time at which the host happened to run the checks does not. The same split applies to `entropy_policy`: a fixed seed or an explicit `none`, never the host's live RNG.

`reproducibility` is a claim about the definition, not a measurement of a run. Phase 1 accepts `unknown` and `externally_mutable` only. `externally_mutable` is the class for a live service such as GitHub. `unknown` is the class when nobody has shown that two instances can be reconstructed. `deterministic` and `snapshot_replayable` stay refused until Phase 2 records a host observation of isolated instances, or Phase 3 shows a second provider can replay them. An operator cannot write those two values in advance.

**REJECT** storing `{id, provider, digest}` on the generation as the world. That triple cannot answer which services, which policy, which parent, or which intent, and it invites a later column for each new field. The generation freezes `world_digest` only.

**ABSTRACT** `WorldProvider`, and **DEFER** the protocol until a second implementation exists. Operations, when that day comes: `create`, `seed`, `snapshot`, `restore`, `connect`, `observe`, `destroy`. **REJECT** `compare` on the provider. The provider returns observations. `syberlabs` decides whether two traces differ. A protocol with one implementation is the fiction that got `AgentRuntime` deleted.

**DEFER** `BehaviorTrace` to Phase 2. The event shape has to be fixed before a runner emits one, because `{service, method, path, outcome}` cannot support the predicates in section 8. `POST /issues` proves an endpoint was called. It does not prove which issue, which body, or which state change.

```text
BehaviorEvent
├── seq                  # world sequence, not wall-clock time
├── service
├── operation            # github.issues.create, not only POST /issues
├── request_digest
├── response_digest
├── resource_ref
├── outcome
├── policy_decision      # allowed | denied
└── state_transition
      ├── before_digest
      └── after_digest
```

No raw credentials. No raw request body in the coordination row. The digests address the bodies, which stay with the provider. A structural diff of two traces is then a diff of operations, resource refs, and state-transition digests. Phase 1 records `trace_digest` as null.

**ADAPT** `EnvironmentSnapshot` as the pair above. The prototype row's `environment` string remains a display label. Do not overload it. OS image, language version, dependency set, database engine, and browser version land here when a definition claims them. Phase 1 does not require them. Their absence is visible because `snapshot_digest` is null rather than omitted from the canonical document.

**ADAPT** `NetworkPolicy` as data inside the definition. SyberWork does not enforce it. A Phase 2 verifier checks that a trace respects it.

**ADAPT** `CredentialBroker` as the existing `auth_env` rule. A capability is a name. The agent session may list names. It must not gain a secret field. Normalization of a world definition refuses secret-like keys, for the same reason `_refuse_hidden` refuses hidden reasoning fields.

**DEFER** `AgentRuntime`. `DisconnectedRuntime` stays the only runtime. Phase 1 does not run a world.

**REJECT** restoring `PrototypeProvider`.

**REJECT** `ExternalServiceSimulation` as a SyberWork type. The definition's `services[].mode` is the whole distinction the cell needs.

**REJECT** adding `world_bound` to the integrity classes. Those classes are `internal`, `human_reviewed`, `host_verified`, `external`, and `signed_external`. They answer who established a claim. "This evaluation is bound to world W" answers what was established. A bound evaluation can later be `host_verified` or `signed_external`. Those are combinations, so they are different axes. The later policy shape, not used in Phase 1, is:

```text
required_evidence:
  - claim: evaluation_world_bound
    independence: host_verified
```

`_verdict` keeps matching `verification_status`, `verified_independence`, and `result`. A claim kind is a further predicate on the observation, added only when a policy of that shape exists. Phase 1 does not add it, and selection does not read world rows.

## 6. Proposed architecture

Two statements stay independent.

```text
AUTHORITY

"The registered tree passed the contract's required checks."
```

That statement is `candidate_evaluated` plus admission. Its shape does not change.

```text
EXPERIMENTAL CONTEXT

Phase 1: "The host bound that evaluation event to world definition W."
Phase 2: "The host observed the candidate execute in an isolated instance of W, and trace T is that observation."
```

Phase 1 does not license the Phase 2 sentence. A binding is provenance. Experience is a later observation.

```text
case chain (authority)
  candidate_registered
  candidate_evaluated                 # unchanged shape
        │
        │ exact event hash, written by the evaluator mechanism
        ▼
coordination (not authority)
  EvaluationWorldDefinition           # immutable, content-addressed
        │
        │ digest only
        ▼
  Generation.world_digest             # frozen at seal
        │
        ├── Candidate A
        │     └── EvaluationBinding → definition digest
        └── Candidate B
              └── EvaluationBinding → definition digest

Phase 2, not Phase 1:
  world instance digest
  BehaviorTrace
  integrity observation
    claim_kind: evaluation_world_bound
    verified_independence: host_verified | signed_external | …
```

SyberWork governs assignment, context permissions (`context_manifest`), lifecycle commands, authority, which definition a generation froze, and which evidence selection is allowed to read. In Phase 1, selection is allowed to read nothing new.

The runtime, when one exists, governs compute, filesystem, process lifetime, installed tools, and browser. The world provider, when one exists, governs external service state and returns observations. Neither appends a case event. Comparison of those observations is a pure function in `syberlabs`. The host evaluator, already forbidden from being the registering actor, is the only component that may record a binding, and later the only component that may call `record_host_integrity` for a trace it observed. `record_host_integrity` is not an HTTP route. The binding follows that pattern: the HTTP API does not offer it to a coordination role.

An operator may register a definition and freeze its digest onto a generation before any evaluation. That is intent. The operator may not, afterward, attach that digest to an evaluation that already exists. The binding is written in the evaluator workflow that just appended `candidate_evaluated`, and it carries that event's hash.

```text
evaluator principal
    runs the contract checks
        ↓
records candidate_evaluated
        ↓
receives the exact event hash
        ↓
host records EvaluationBinding
    candidate_id
    evaluation_event_hash
    evaluator principal
    world_definition_digest
    world_instance_digest    # null in Phase 1
    trace_digest             # null in Phase 1
    evaluation_run_id
```

The evaluator principal on the binding must be the actor on the case event. The definition digest must equal the generation's frozen digest. A second binding for the same event hash is a replay of the same record or a refusal, not a chance to name a different world.

Sibling candidates share a definition because the generation freezes one digest. Phase 1 does not create instances. The projection therefore reports:

```text
world_definition_matches
evaluation_binding_present
world_definition
instance_equivalence: unverified | impossible
```

`world_definition_matches` is true only when every linked candidate has a host binding and every binding's definition digest equals the generation digest. A missing binding is not a match. `instance_equivalence` is `unverified` when reproducibility is `unknown`, because Phase 1 has no instance digest. It is `impossible` when reproducibility is `externally_mutable`, because a live service at two times is two universes even when the definition digest matches. Phase 1 never emits `verified`. That value waits until Phase 2 records instance digests from isolated runs and the host checks them.

`worlds_differ: false` is the wrong field. It reads as "equivalent environments."

Worlds across generations are references to immutable definitions:

```text
Generation 4 → W17
Generation 5 → W17
Generation 6 → W18 (parent W17, intent adversarial)
```

| Use | How it is represented | Label |
| --- | --- | --- |
| Controlled comparison | Another generation stores the same definition digest | **ADAPT** |
| Deliberate mutation | New definition, new digest, `parent_world_digest` set, `intent: mutated` | **ADAPT** |
| Adversarial | New definition, `intent: adversarial`, parent set | **ADAPT** |
| Historical | An existing digest. The cell does not rewind a clock. `time_policy` inside that definition is the clock that counts | **ADAPT** |
| Customer-specific | `provider.name` names the customer's provider, and `provider.revision` is inside the digest | **DEFER** the provider; **KEEP** the digest rule |

This is an experimental record of software evolution: each generation names the system it varied and the world definition it varied against. It is not an agent-development UI. The environment of selection is that frozen definition. It is not yet evidence that the candidates experienced it.

## 7. Data and event model changes

**KEEP** `candidate_evaluated` frozen. Adding a world field to that body would change `evaluation_record`'s exact field set, the golden traces, and every admission rule that assumes the newest evaluation of a tree is comparable to an older one. Do not do that in v0alpha1.

**KEEP** `builder_generations` as migration `0004` created it. Do not add a world column to that table.

**ADAPT** with three new tables in migration `0005`. `migrate` runs one shared statement list on both dialects. SQLite wraps that list in `BEGIN IMMEDIATE`. PostgreSQL connects with `autocommit=True` and holds `pg_advisory_lock` around the statements, so a failure leaves earlier statements committed and does not insert `schema_migrations`. The next open retries every statement. `CREATE TABLE IF NOT EXISTS` survives that retry. `ADD COLUMN` does not, unless each dialect grows its own existence check. The only such check today is `_column_names` for SQLite's `events.at_json`, outside the shared scripts. New world state therefore follows `0004`: new tables, not `ALTER`.

```text
builder_worlds
    digest          PRIMARY KEY
    body            canonical EvaluationWorldDefinition, minus the digest field
    created_by
    created_at

builder_generation_worlds
    generation_id   PRIMARY KEY
    world_digest
    sealed_at

builder_evaluation_bindings
    id              PRIMARY KEY
    generation_id
    candidate_id
    evaluation_event_hash
    evaluator
    world_digest
    world_instance_digest    NULL in Phase 1
    trace_digest             NULL in Phase 1
    evaluation_run_id
    actor
    at
    UNIQUE (evaluation_event_hash)
```

`SNAPSHOT_TABLES` in `syberwork/storage.py` is an explicit list. Restore treats optional files as migration groups (`SNAPSHOT_GROUPS` in `syberwork/backup.py`). A pre-builder snapshot, which has none of the Builder files, still restores. A complete Builder snapshot from before the world tables, which has every `0004` file and none of the three world files, restores with those tables empty. A group that is only partly present is refused, and so is a world-table file that arrives without the earlier Builder files. A missing authoritative file such as `policies.json` is refused. The refusal rolls the restore transaction back.

Seal inserts `builder_generation_worlds` in the same transaction that moves the generation to `sealed`, and only when the operator supplied a digest that already exists in `builder_worlds`. A generation with no row there has no world. Existing generations stay valid. The row is immutable after seal.

The binding insert is not an HTTP mutation for `admin`, `operator`, `engineer`, `manager`, or `decision`. It is a host method beside `record_host_integrity`. It refuses unless all of the following hold: the case event is `candidate_evaluated` for `candidate_id`; its actor equals `evaluator`; the generation is sealed with `world_digest`; the event was appended by this same host call, not selected from older history. Phase 1 stores null instance and trace digests. A coordination role cannot update them later. Phase 2 fills them in the host call that observed the run, not in a second operator request.

No new case-event kind. `evaluate_selection` does not query these tables. `builder_integrity` is unchanged in Phase 1.

Content hashed into the definition digest: `provider.name`, `provider.revision`, services and their mode, `seed_digest`, `snapshot_ref`, `snapshot_digest` (including an explicit null), `network_policy`, `time_policy`, `entropy_policy`, `reproducibility`, `parent_world_digest`, `intent`. Excluded: secret-bearing endpoints, the wall-clock time of a run, process ids, candidate ids, and instance digests. The instance is not part of the definition. A live service's definition digest can match while its instances do not, which is why `externally_mutable` forces `instance_equivalence: impossible`.

## 8. Authority and integrity implications

The failure this must not repeat is an agent certifying its own integrity, or an operator certifying an evaluation's world after the fact.

Phase 1 bindings are not integrity observations and do not enter `_verdict`. The table below is the Phase 2 classification, using the classes that already exist. The claim kind is `evaluation_world_bound` or a narrower predicate. It is not a new independence class.

| Evidence | Who may verify it | Independence | Selection |
| --- | --- | --- | --- |
| "I created one issue" | The acting agent | `internal` / `self_report` | Only if the policy requires `internal` |
| A person inspected the trace | Authenticated human principal | `human_reviewed` | When required |
| The host runner observed the trace | `record_host_integrity` | `host_verified` | When a future policy requires that class for that claim |
| A verifier recomputed the digest and checked predicates | `IntegrityVerifier`, independence unchanged | `external` or `signed_external` | When required |
| A SHA-256 that merely looks like a signature | Nobody | stays `unverified` | Ignored |
| A coordination role asserts the world after the checks finished | Nobody | not recorded | Ignored |

Predicates worth recording in Phase 2, each as a claim whose digest addresses the trace, and whose independence is whatever mechanism actually checked it:

- one `github.issues.create` whose `request_digest` matches the expected body digest, and no other GitHub write;
- every event's `policy_decision` is `allowed`, or each `denied` event names a destination the policy forbids;
- `state_transition.before_digest` and `after_digest` match the seed and the expected mutation, and no other seeded key changed;
- a budget counter on the instance did not exceed the generation's declared ceiling.

`POST /issues` alone satisfies none of these. The cell does not interpret GitHub. The definition lists the predicates. The verifier returns pass or fail for each claim and cannot relabel independence (`assess_integrity` already refuses that).

Network activity is evidence only as events inside a Phase 2 trace. SyberWork does not capture packets. Silence is `unverified`, not a pass. A definition digest is not a substitute for that trace.

Effects stay the authority path for real side effects. A simulated GitHub write is a trace event. It must not become an `effect_started` case event and must not satisfy an effect contract. A real staging write is still an effect, with `auth_env`, idempotency, and reconciliation, if the contract says so. The world binding does not replace that.

Capability flow:

```text
agent assignment lists capability names
        │
        ▼
runtime asks the broker by name
        │
        ▼
broker injects the secret at the boundary
        │
        ▼
trace records the service action, never the secret
```

This is the same shape as an installed action: the definition names `auth_env`, the worker reads the environment, the case body never contains the token. **KEEP** that shape. **REJECT** a coordination field named `token`, `secret`, or `authorization`.

## 9. Builder UX implications

The visual Builder is not part of this work. The substrate should make the following questions answerable from projections, which already set `authoritative: false`.

| Question | Phase 1 reads | Later |
| --- | --- | --- |
| Which definition was this generation sealed with? | `world_definition` loaded by digest: services, mode, policy, snapshot digest, time policy, parent, intent, provider revision | Same object |
| Were the evaluations bound to it? | `evaluation_binding_present` per candidate, `world_definition_matches` for the generation | Same |
| Did they experience equivalent instances? | `instance_equivalence` is `unverified` or `impossible`. Never `verified` | `verified` only after host-recorded instance digests |
| Were the services simulated or real? | `services[].mode` on the definition | A real service displays `impossible` for instance equivalence |
| What did the candidate actually do? | Not answered. `trace_digest` is null | `BehaviorEvent` fields, compared by a pure function |
| Was the evidence independent? | Not a world question yet | Existing integrity projection |
| Why was it selected? | Existing selection `reasons`. Bindings are not among them | Still not a rank |
| Who had authority to promote it? | Existing `promotion_authorized` and case admission | Unchanged |

**REJECT** a DevOps dashboard. **REJECT** a Phase 1 screen that says the candidate experienced the world. Architecture diffs stay `architecture_diff`. A behavioral diff is a Phase 2 panel that compares traces SyberWork already stored, not a call to the provider's `compare`.

Replay, when a provider exists, is the provider restoring a snapshot digest. The UI does not become the provider.

## 10. Failure modes and architectural risks

| Risk | What goes wrong | Guard |
| --- | --- | --- |
| Integrity class used as a property | `world_bound` sits beside `host_verified`, so a claim cannot be both | **REJECT** the class. Claim kind and independence stay separate axes. Phase 1 has neither in selection. |
| Declared world read as experienced world | A shared digest is shown as "both candidates ran in W" | Projection copy is `world_definition_matches`. Phase 1 `instance_equivalence` is `unverified` or `impossible`, never `verified`. |
| Live service, shared definition | Candidate A sees GitHub at T1 and candidate B at T2 | `reproducibility: externally_mutable` forces `instance_equivalence: impossible`. |
| Provider name without revision | DoubleAgent v1 and v3 hash as one world | `provider.revision` is inside the definition digest. |
| Snapshot id without a digest | `snapshot-17` is treated as immutable | `snapshot_digest` is the identity. `snapshot_ref` is a locator. |
| Clock stripped entirely | Expiry, calendars, and token lifetime fall out of the world | `time_policy` is a fixed epoch and timezone. The run's wall clock stays out. |
| Retrospective binding | An operator attaches today's world to last week's evaluation | Host method only, in the call that just appended the case event. HTTP coordination roles cannot write it. |
| Self-certifying trace | The candidate's agent posts the trace and a passing observation | Phase 2. Trace actor cannot be the registering principal. HTTP cannot call `record_host_integrity`. |
| Shared mutable instance | Sibling A consumes the only issue id sibling B needed | Phase 2 isolates instances. Phase 1 creates none. |
| Frozen protocol drift | World fields slip into `candidate_evaluated` | Exact field set in `evaluation_record` stays the admission contract. |
| Provider becomes authority | A simulated merge is treated as promotion | `promotes_git` stays false. Traces are not effects. |
| Comparison delegated to the provider | The world vendor decides which candidate behaved better | `compare` is not a provider operation. |
| Secret in the world document | A seed contains a token, which then enters a backup | Normalization refuses secret-like keys. New tables join `SNAPSHOT_TABLES` and are exported with the other builder tables. |
| Partial migration | An `ALTER` on PostgreSQL survives a crash and fails on retry | New `CREATE TABLE IF NOT EXISTS` tables. Do not reopen `0004`. Do not `ADD COLUMN` on `builder_generations`. |
| Runtime protocol fiction | One adapter and a protocol, again | No `AgentRuntime` and no `WorldProvider` protocol in Phase 1. |

## 11. Opportunities to simplify

**KEEP** the four planes. A world definition is a coordination object the generation references. It is not a new plane and not a second hash chain.

**REJECT** putting definitions, bindings, traces, or runtime logs into `events`.

**ADAPT** by not growing `candidate_evaluated`, and by not growing `builder_generations`. The definition lives in `builder_worlds`. The freeze is a row in `builder_generation_worlds`. The exact check-record field set stays what keeps old trees comparable.

**KEEP** `DisconnectedRuntime` as the only runtime type. Phase 1 does not add a second one.

**KEEP** selection as reasons in three buckets. Phase 1 does not add a required class, a claim kind, a fitness term, or a read of the binding tables.

**ADAPT** the prototype `environment` string: leave it as a label. The world store is the definition table.

**DEFER** PostgreSQL `NOTIFY`. World rows do not justify a new wakeup path.

**REJECT** `compare` on a future provider. One less method is the simplification.

## 12. Phased roadmap

| Phase | What it licenses | Lands in the repository | Does not include |
| --- | --- | --- | --- |
| 0. This document | The distinctions in this file | `docs/EVALUATION_WORLDS.md` | Schema, migration, runtime |
| 1. World identity and binding | "The host bound these evaluation events to the same immutable definition." | Implemented. `world_definition` in `syberlabs/builder.py`. Migration `0005`. `BuilderStore.register_world`, `seal(..., world_digest)`, `record_bound_evaluation`. `bind_evaluation` refuses. HTTP `POST /api/builder/worlds` registers a definition. There is no bind route. | Trace, instance, selection rule, integrity class, provider protocol, runner |
| 2. World execution and trace verification | "The host observed these candidates execute in isolated instances of that definition." | Instance digest, `BehaviorEvent`, `record_host_integrity` citing the trace, `instance_equivalence: verified` only then | A new independence class. Claim kind stays separate from independence |
| 3. Provider reproducibility | "Another implementation can reconstruct and replay the definition." | A second observer. `WorldProvider` appears because two implementations exist. `compare` stays in `syberlabs` | Vendoring DoubleAgent or Islo |
| 4. Runtime adapter | The cell can name who runs the checks | A local process beside `DisconnectedRuntime` | Kubernetes, microVMs, browsers |
| 5. Contract opt-in | Admission may require a world digest | A new protocol version and new goldens | A silent change to v0alpha1 |

Phase 1 is provenance. Phase 2 is the first time the cell may say a candidate experienced a world. Phase 3 is the first time a second party can replay that world. Selection policy grows a `required_evidence` entry only with Phase 2, and only as `{claim, independence}`, never as a new value inside `verified_independence`.

## Smallest slice

Phase 1, as specified below, is what landed. `tests/test_evaluation_worlds.py` covers the binding, the retrospective refusal, a partial match, `externally_mutable` as `impossible`, and a selection result that matches `evaluate_selection` with no world input.

1. A pure function canonicalizes an `EvaluationWorldDefinition` and returns its digest. The document includes provider name and revision, services and mode, seed digest, snapshot ref and snapshot digest, network policy, time policy, entropy policy, reproducibility, parent digest, and intent. No I/O. Two documents that differ by provider revision, snapshot digest, or fixed epoch hash differently. Explicit nulls are part of the canonical form.
2. `builder_worlds`, `builder_generation_worlds`, and `builder_evaluation_bindings`, created with `CREATE TABLE IF NOT EXISTS` and added to `SNAPSHOT_TABLES`. No `ALTER` of `builder_generations`.
3. Seal optionally freezes an existing definition digest. Omitting it leaves current generations valid. The freeze row cannot be updated.
4. The host method that records `candidate_evaluated` may also insert a binding to the generation's frozen digest, with null instance and trace digests. A later call that names an older event hash is refused. HTTP coordination roles cannot insert a binding.
5. The generation candidate view adds `world_definition`, `evaluation_binding_present`, `world_definition_matches`, and `instance_equivalence`. It does not add `worlds_differ`. `instance_equivalence` is `impossible` for `externally_mutable` and `unverified` otherwise. It is never `verified` in this slice.
6. `evaluate_selection` is unchanged. Tests assert a sealed world and two matching bindings do not move a candidate between `advanced`, `unresolved`, and `rejected`.
7. `candidate_evaluated` and the 23 golden traces stay as they are. `promotes_git` stays false.

That slice answers one question: can the cell show that the trusted evaluator bound two sibling evaluations to one immutable world definition, without claiming they experienced it, and without letting that fact change selection? A runner, a trace, and a provider are how Phase 2 and Phase 3 earn the stronger sentences. They are not required to learn whether the provenance record is honest.
