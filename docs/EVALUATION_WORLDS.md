# Evaluation worlds

Status: **proposed**. Nothing in this document is implemented. It does not change `sdk.syberlabs.space/v0alpha1`, migration `0004`, or `sdk.syberlabs.space/builder/v0alpha1`.

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
| Snapshot of filesystem and database | **ADAPT** | A generation should be able to name an environment snapshot digest. SyberWork stores the digest and the provider id. The provider stores the bytes. |
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
| Contract tests of the fake against the real service | **DEFER** to the provider | That is how a world provider earns trust. SyberWork records the provider name and the world digest. It does not re-run the provider's contract suite. |
| Reset, seed, snapshot, event log | **ABSTRACT** | These are the operations a generation needs in order to freeze a world and compare siblings. |
| Official SDK compatibility | **REJECT** as a SyberWork concern | Candidates talk to whatever URL the world gives them. The cell never imports a vendor SDK. |
| Isolated world per agent | **ADAPT** | Sibling candidates in one generation should share one world definition and receive isolated instances of it. Sharing the definition is what makes the comparison mean something. Sharing one mutable instance would let candidate A poison candidate B. |
| Hosted trace scoring | **REJECT** | Selection already lists reasons. A score would hide veto, dissent, and missing evidence. |

## 4. Own, integrate, or defer

| Capability | Owner | Label |
| --- | --- | --- |
| Case history, admission, effects, reconciliation | SyberWork | **KEEP** |
| Generation barrier, diversity, isolation manifests, selection without promotion | SyberWork | **KEEP** |
| Integrity classes and the rule that selection ignores unverified claims | SyberWork | **KEEP** |
| Which world a generation froze, as an id and a digest | SyberWork coordination record | **ADAPT** |
| Whether sibling evaluations cite that same digest | SyberWork projection | **ADAPT** |
| Check argv, exit codes, output digests | Existing host evaluator | **KEEP** |
| Binding a check record to a world digest and a behavior-trace digest | Builder coordination record that cites the case event | **ADAPT** |
| Classifying a trace as host-verified or externally verified | Existing `assess_integrity` | **KEEP** |
| Creating, seeding, snapshotting, and destroying a world | World provider | **ABSTRACT** |
| Compute, filesystem, process lifecycle, browser, installed tools | Agent runtime | **ABSTRACT**, and **DEFER** until a second adapter exists |
| Credential material | Broker outside the agent. The cell stores the binding name, as `auth_env` already does | **ADAPT** |
| Packet filter, microVM, Kubernetes, customer VPC | External infrastructure | **DEFER** |
| Becoming the fake GitHub, Slack, or Stripe | Nobody in this repository | **REJECT** |
| Importing Islo or DoubleAgent as a library | Nobody in this repository | **REJECT** |

**INTEGRATE** means a later adapter may speak a provider's HTTP. It does not mean a dependency in `syberlabs` or `syberwork`. `syberlabs` must not import `syberwork`. A provider adapter, when one exists, lives at the edge the way `CommandProvider` does: a process boundary, no vendor SDK in the core.

## 5. Missing abstractions

Do not add a fifth plane. The objects below are coordination facts and integrity inputs.

**ABSTRACT** `EvaluationWorld`. A frozen input to a generation, the same kind of fact as `base_revision`. Fields the cell needs, and no others: `id`, `provider` (a short name), `digest` (SHA-256 of the canonical world document), `services` (names and `simulated` or `real`), `network_policy_ref`, `snapshot_ref`. The digest covers the seed, the policy, and the snapshot identity. It does not cover live process memory.

**ABSTRACT** `WorldProvider` only as a protocol once two implementations exist. The operations are `create`, `seed`, `snapshot`, `restore`, `connect`, `observe`, `compare`, `destroy`. The first implementation should be in-process and static (section 12). A DoubleAgent-shaped HTTP adapter is the second, and it is what justifies the protocol. Until then, a module function is enough. This repeats the reason `AgentRuntime` was deleted: a protocol with one implementation is a fiction.

**ABSTRACT** `BehaviorTrace` as a content-addressed list of external actions: `{service, method, path, outcome, at_seq}` plus a digest. `at_seq` is the world's sequence, not wall-clock time, so two runs can be compared. The trace is evidence. It is not an effect, and it is not a case event.

**ADAPT** `EnvironmentSnapshot` as a digest the world document names. The prototype row's `environment` string is a display label, not this object. Do not overload it.

**ADAPT** `NetworkPolicy` as data inside the world document: allowed destinations, denied destinations, and whether a denied attempt must appear in the trace. SyberWork does not enforce it. A verifier checks that the trace respects it.

**ADAPT** `CredentialBroker` as the existing `auth_env` rule, extended in documentation and in the world document. A capability is a name (`github_issue_write`). The runtime or the gateway resolves the name. The agent session's `assignment` may list capability names. It must not gain a secret field. `_refuse_hidden` already rejects hidden reasoning fields; a secret key belongs on that refusal list when the world document is normalized.

**DEFER** `AgentRuntime` as a protocol. `DisconnectedRuntime` stays the default. The next real adapter should be a local process that runs `syberlabs.checks` against a static world, not an Islo client. When a second adapter exists, the protocol returns `{applied, reason}` as `DisconnectedRuntime` already does, plus a trace digest when the command was an evaluation. It does not gain a method that mutates Git.

**REJECT** restoring `PrototypeProvider`. Prototype state (`building|ready|failed|expired`) is a registry. Provisioning stays outside.

**REJECT** `ExternalServiceSimulation` as a SyberWork type. That is an implementation detail of a world provider. The cell sees service names and a simulated-or-real flag.

## 6. Proposed architecture

```text
case chain (authority)
  candidate_registered
  candidate_evaluated          # unchanged shape
        ▲
        │ cites event hash
coordination log (not authority)
  generation
    base_revision
    evaluation_world {id, provider, digest}    # frozen at seal
  evaluation_binding
    candidate_id, case_event_hash, world_digest, trace_digest
  behavior_trace
    digest, events[]
  integrity_observation
    verified_independence, digest = trace_digest

world provider (replaceable)
  static fixture  |  later: DoubleAgent HTTP  |  later: customer staging
        ▲
runtime (replaceable)
  disconnected  |  later: local process  |  not a microVM product
```

SyberWork governs assignment, context permissions (`context_manifest`), lifecycle commands, authority, generation and candidate association, and which evidence selection is allowed to read.

The runtime governs compute, filesystem, process lifetime, installed tools, and browser. The world provider governs external service state. Neither one appends a case event. The host evaluator, which is already forbidden from being the registering actor, is the only component that may call `record_host_integrity` for a trace it observed. The agent that produced the candidate may record `internal` only.

An evaluation the cell can defend becomes:

> Candidate C was evaluated by evaluator E inside world W at snapshot S, and the external behavior trace is T.

The check record remains the admission input. The binding is how a Builder projection and a later proposal show that the checks ran in a named world. A proposal may cite the binding's digest the way it may already cite a selection `evidence_digest`. Admission does not start requiring a world until a new contract field says so, in a later protocol version. Until then, a missing world means "world not recorded," which selection can treat as unresolved when the policy lists a required integrity class, and which admission ignores.

Sibling candidates share one world digest because the generation freezes it at seal, next to the approach descriptors. Each candidate receives its own instance, created from that digest. Comparison is meaningful when the binding digests match. A projection field `worlds_differ` is true when any linked candidate's binding names another digest. That is a fact, not a score.

Worlds across generations:

| Use | How it is represented | Label |
| --- | --- | --- |
| Controlled comparison | Child generation copies the parent world digest | **ADAPT** |
| Deliberate mutation | New world document, new digest, `parent_world_digest` set | **ADAPT** |
| Adversarial | A world whose policy or seed is marked `intent: adversarial` by the operator who sealed the generation | **ADAPT** |
| Historical | A digest that already exists. No clock is rewound inside the cell | **ADAPT** |
| Customer-specific | `provider` names the customer's world provider. The digest is still what siblings share | **DEFER** the provider; **KEEP** the digest rule |

This is an experimental record of software evolution: each generation names the system it varied and the world it varied against. It is not an agent-development UI.

## 7. Data and event model changes

**KEEP** `candidate_evaluated` frozen. Adding `world_id` to that body would change `evaluation_record`'s exact field set, the golden traces, and every admission rule that assumes the newest evaluation of a tree is comparable to an older one. Do not do that in v0alpha1.

**ADAPT** the builder protocol, in a later change, not in this document's commit:

- `normalize_generation` gains an optional `evaluation_world`. When present it is `{id, provider, digest}` with `provider` a short token and `digest` 64 hex characters. When absent, current generations stay valid. Seal copies it onto the generation row and refuses a later rewrite, the same way descriptors freeze.
- A coordination event `evaluation_bound` (builder namespace only) carries `candidate_id`, `case_event_hash`, `world_digest`, `trace_digest`. The case event hash must be a `candidate_evaluated` event for that candidate. The world digest must equal the generation's frozen digest.
- A coordination event `behavior_trace_recorded` carries the canonical event list and its digest. The actor is the host or a registered verifier, never the candidate's registering principal.
- `builder_integrity.digest` already exists. A host or external observation sets it to the trace digest. `evidence_refs` may name the coordination sequence. Selection already ignores the row unless `verified_independence` matches.

No new case-event kind. No new migration in the slice that only writes the report. The implementation slice adds columns with `IF NOT EXISTS` semantics inside the next builder migration, or stores the world document in the generation's existing JSON if a column is unnecessary. Prefer a column for `world_digest` so sibling comparison is a predicate, not a scan of bodies. Do not reopen `0004`: deployed databases already applied it. A new migration is `0005` only when the code lands, and only as `ADD COLUMN` statements that are safe to retry, which the removed `0005` was not.

Content to hash into the world digest: provider name, service list and simulated-or-real flags, seed document, network policy, snapshot id. Exclude: endpoints that contain secrets, wall-clock time, process ids, and the candidate id. The instance is derived; the definition is addressed.

## 8. Authority and integrity implications

The failure this must not repeat is an agent certifying its own integrity.

| Evidence | Who may verify it | Class | Selection |
| --- | --- | --- | --- |
| "I created one issue" | The acting agent | `internal` / `self_report` | Only if the policy requires `internal` |
| A person inspected the trace | Authenticated human principal | `human_reviewed` | When required |
| The host runner observed the trace file the world returned | `record_host_integrity` | `host_verified` | When required |
| A verifier recomputed the digest and checked predicates | `IntegrityVerifier`, independence unchanged | `external` or `signed_external` | When required |
| A SHA-256 that merely looks like a signature | Nobody | stays `unverified` | Ignored |

Predicates worth recording, each as a claim string on an observation whose digest is the trace:

- the trace contains one `github.issues.create` and no other GitHub write;
- no event destination is outside the world policy;
- a named database key changed from the seed value to the expected value, and no other seeded key changed;
- denied destinations appear as denied, not as missing;
- a budget counter on the world did not exceed the generation's declared ceiling.

The cell does not interpret GitHub. The world document lists the predicates. The verifier returns pass or fail for each claim. A failed verifier sets `verification_status` to `failed` and does not upgrade the class (`assess_integrity` already does this).

Network activity is evidence only as events inside the trace. SyberWork does not capture packets. A runtime that cannot produce a trace cannot produce `host_verified` network evidence. Silence is `unverified`, not a pass.

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

| Question | Read from |
| --- | --- |
| What world did this candidate experience? | Generation `evaluation_world`, repeated on the candidate view. |
| Were the services simulated or real? | World document `services`. |
| What did the candidate actually do? | `BehaviorTrace` events, not the agent's activity text. |
| What evidence did those actions produce? | Integrity projection: `independent_evidence` versus `self_report` versus `unverified_claims`. |
| Was the evidence independent? | `verified_independence` and `verification_method`. |
| How did behavior differ from siblings? | A structural diff of traces that share a world digest. `worlds_differ` when they do not. |
| Why was it selected? | Existing selection `reasons`. Do not add a rank. |
| Who had authority to promote it? | Existing `promotion_authorized` and the case admission rule. The Builder does not gain a promote button that skips admission. |

**REJECT** a DevOps dashboard: CPU, pod status, image tags, and a live desktop of the sandbox. Runtime health, if shown at all, is the command result the cell already stores (`applied`, `reason`) plus whether the bound trace digest matches the generation world. Architecture diffs stay the graph diff `architecture_diff` already computes. A behavioral diff is a second panel on the same candidate comparison, not a new product surface.

Replay is "restore this world digest and re-read the trace," performed by the provider. The UI stores nothing that would let it become the provider.

## 10. Failure modes and architectural risks

| Risk | What goes wrong | Guard |
| --- | --- | --- |
| Self-certifying trace | The candidate's agent posts the trace and a passing observation | Trace actor cannot be the registering principal. HTTP cannot call `record_host_integrity`. |
| Shared mutable world | Sibling A consumes the only issue id sibling B needed | One definition, isolated instances. Compare digests, not live servers. |
| World digest omits the seed | Two generations share an id and differ in behavior | Digest covers seed, policy, and snapshot id. Id alone is not identity. |
| Frozen protocol drift | World fields slipped into `candidate_evaluated` | Exact field set in `evaluation_record` stays the admission contract. Bindings cite the event hash from the builder log. |
| Provider becomes authority | A simulated merge is treated as promotion | `promotes_git` stays false. Traces are not effects. |
| Verifier relabels a self-report as signed | Selection treats the agent as an independent party | `assess_integrity` already rejects an independence mismatch. |
| Non-determinism | Trace timestamps and map iteration make digests differ for the same actions | Canonical JSON, world sequence numbers, sorted keys. The same `canonical()` rules as the case hash, without putting the trace in the chain. |
| Secret in the world document | A seed file contains a token, which then enters a snapshot and a backup | Normalization refuses secret-like keys. Backups already export builder tables; a world body must be safe to export. |
| Partial migration | An `ALTER` in the middle of `0005` leaves PostgreSQL half-applied | Do not add `0005` until the statements are individually retryable. `0004` stays a sequence of `CREATE TABLE IF NOT EXISTS` and is not wrapped in a transaction. |
| Runtime protocol fiction | One adapter and a protocol, again | No `AgentRuntime` protocol until the static world runner and one other adapter both exist. |
| Comparison theater | Candidates share a digest but one check reached the public internet | Network policy is inside the digest. A trace that contains a destination outside the policy fails the verifier. A runtime that cannot see the network cannot claim `host_verified` for that predicate. |

## 11. Opportunities to simplify

**KEEP** the four planes. An evaluation world is a frozen generation input, not a new plane and not a second hash chain.

**REJECT** putting traces, world documents, or runtime logs into `events`. The coordination log already exists so that Builder facts do not move the case hash.

**ADAPT** by not growing `candidate_evaluated`. The temptation is one richer evaluation event. The exact field set is what keeps old trees comparable and the 23 golden traces stable. Cite the event from the side.

**KEEP** `DisconnectedRuntime` as the only runtime type until a second one is real. Deleting the unused protocol was the simplification. Do not undo it in the same change that adds a world digest.

**KEEP** selection as reasons in three buckets. A world does not add a fitness term, a weight, or an embedding.

**ADAPT** the prototype `environment` string: leave it as a label. Do not make prototypes the world store. Worlds attach to generations so that every sibling shares them. A prototype is one candidate's deployed artifact.

**DEFER** PostgreSQL `NOTIFY`. The SSE poll is already documented as a development transport. World events use the same coordination log and the same cursor. They do not justify a new wakeup path.

## 12. Phased roadmap

| Phase | Lands in the repository | Label | Does not include |
| --- | --- | --- | --- |
| 0. This document | `docs/EVALUATION_WORLDS.md` | — | Schema, migration, or runtime |
| 1. Static world digest | Optional `evaluation_world` frozen at seal. In-process fixture: seed JSON, predicates, no socket. Binding cites `candidate_evaluated`. Sibling projection sets `worlds_differ`. | **ADAPT** | DoubleAgent, Islo, Docker, a new case event |
| 2. Host trace | The check runner, when given a world, writes a `BehaviorTrace` and the host records `host_verified` through `record_host_integrity`. The agent cannot. One predicate: expected actions only. | **ADAPT** | Packet capture, a sandbox |
| 3. Second provider | HTTP adapter with `seed`, `reset`, `events`, matching DoubleAgent's shape but importing nothing. Protocol appears here because two implementations exist. | **ABSTRACT** | Vendoring DoubleAgent |
| 4. Runtime adapter | Local process runtime beside `DisconnectedRuntime`. Commands stay coordination commands. Evaluation is a host action, not `redirect`. | **ABSTRACT** | Kubernetes, microVMs, browsers |
| 5. Contract opt-in | A future evolution-section field may require a world digest before promotion. That is a new protocol version, with new goldens. | **DEFER** | Silent change to v0alpha1 |

Phases 1 and 2 are the experiment. Phases 3 through 5 wait until a generation with two candidates produces a comparison that admission-quality evidence could not: same base revision, same world digest, different traces, integrity classified by a party that did not register the candidate.

## Smallest slice that would prove the idea

Implement phase 1 only, in the builder protocol.

1. Extend `normalize_generation` with optional `evaluation_world`. Seal freezes it. Existing callers that omit it keep today's behavior, so current builder tests stay valid.
2. Add a pure function, next to `diversity_evidence`, that canonicalizes a static world document and returns its digest. The document is seed plus predicates plus a network policy. No I/O.
3. When a candidate is linked and the generation has a world, require an `evaluation_bound` coordination record before selection can treat a new integrity class `world_bound` as satisfied. Do not add that class to any default policy. A policy that does not list it selects exactly as it does now.
4. Project `worlds_differ` on the generation candidate view: false when every binding digest equals the generation digest, true otherwise, absent when the generation has no world.
5. Tests: two linked candidates, one shared digest, `worlds_differ` false; a second generation with a one-field seed change, digests differ; an agent principal attempting `host_verified` is refused, which is already true and should be re-stated against a trace digest; `candidate_evaluated` schema and the 23 golden traces unchanged; `promotes_git` still false.

That slice answers one question: can two sibling candidates be shown to have been judged against the same world, with the world identity independent of either candidate's claim? If the projection is unused by selection unless a policy asks for it, the slice cannot accidentally become a second promotion path. If the digest is too weak to capture the differences that matter, the failure shows up as two worlds that hash the same and behave differently, which is the predicate phase 2 exists to catch. No VM is required to learn that.
