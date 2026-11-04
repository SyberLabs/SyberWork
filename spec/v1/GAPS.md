# Gaps in v1alpha1

Where a schema cannot say the whole rule, where the code does not yet do what the protocol says, or where v1alpha1 differs from the system it was derived from. Each gap names what would close it.

## Rules no schema here can state

1. **No runtime enforces v1alpha1.** The schemas and fixtures are a specification. `syberlabs` writes no EventAttribution, checks no GrantRef, and makes no DecisionCall. Closed by the SyberSDK packets in Phase 2 of the consolidation plan.

2. **Root identity equality is procedural.** A root Agent has `parent_id: null` and `root_id == id`; a child shares its parent's `root_id`. JSON Schema cannot compare two fields, and `spec/v1/check.py` does not check it. The runtime that writes identities must.

3. **Menu membership is procedural.** `OUT_OF_MENU` means the answer was not one of the criteria keys the host offered. The schema cannot relate an outcome to its call. The Decision Port client checks it.

4. **Counts are checked by `check_decision_call`, not by the schema.** `spec/validate.py` does not support `minProperties` or `maxProperties`, so "at least one question", "1 to 255 options per choice" (KevRT `docs/architecture/KevRT-architecture.md` section 15) and "`request.model` equals `provider.model`" live in `spec/v1/check.py`. A consumer that validates with a full JSON Schema implementation must apply them too.

5. **A keys default must be inaction, and the schema cannot tell.** MasterMind's keys asks carry a `default_on_silence` that keeps the status quo (ASK-009: Contact stays parked). v1alpha1 allows a keys default for that reason and forbids a default only on `approve`. That a keys default performs no effect is a rule for MasterMind's ledger and any runtime that applies defaults.

6. **Evidence `verified` is not checkable from the document.** Only the host knows whether a configured source or checker produced it. A model-reported `verified: true` is a runtime violation the schema cannot see.

7. **Runtime-only invariants and their owners.** SPEC.md invariants that no v1alpha1 schema enforces:

   | Invariant | Enforced today by | v1alpha1 owner |
   | --- | --- | --- |
   | 1. Inference never admits, authorizes or asserts an effect | `syberlabs/admission.py` (`candidate.promotable`, origin and role rules) | SyberSDK runtime |
   | 2. Requests carry no authority | Turtle `TrustedContext` (P0, see item 9) | turtle-shells, then an `authority.grant` admission rule |
   | 3. Child grant is a subset of the parent's | `turtle policy diff`, `check_attenuation` | turtle-shells |
   | 4. Approver is not the proposer | `syberwork/core.py` `approve` | SyberSDK runtime |
   | 5. Unknown is a state, never retried | `effect_unknown` and `reconcile` in `syberwork/core.py` | SyberSDK runtime |
   | 7. One case, one history | not enforced; SyberWork itself keeps two journals | SyberSDK, Phase 2 |

## Where the code does not yet match

8. **Turtle P0 has known gaps.** GrantRef pins a policy; it does not encode clauses. turtle-shells P0 ignores `ctx.now` for expiry, never settles budgets, does not roll back a multi-metric reservation, does not match the credential on a request, and leaves `DecisionEvidence.policy_digest` empty. Until those are fixed, a GrantRef is a pin, not an enforced boundary.

9. **`policy_digest` has no agreed encoding.** Turtle domain-separates its digest with `turtle-policy-v0alpha1\n`; this protocol only requires 64 hex characters. The fixture digests are placeholders. The Turtle packet that fills `DecisionEvidence.policy_digest` defines it.

10. **Score answer fields are unconfirmed.** A noul answer carries `noul`, p(yes), as COMMONS (`packages/integrations/src/jev.ts`) and RISE (`src/enterprise/device-decider.test.js`) read it. No repository records the field a score answer uses, so DecisionOutcome requires only `type` on score answers. Closed by capturing one real score response into the fixtures.

11. **`request_digest` is optional.** RISE and Flyspace do not hash their requests today. It becomes required when the shared Decision Port client writes outcomes into a case.

## Where v1alpha1 differs from the system it was derived from

12. **Capability is derived from Flyspace's manifest, not identical to it.** Flyspace field to v1alpha1:

    | Flyspace (`src/core/capabilities/manifest.ts`, `identity.ts`) | v1alpha1 |
    | --- | --- |
    | `id` `cap_<hash>` or pinned `cap_speech_speak` | `cap_[a-z0-9_]{3,64}`, or a dotted name for hand-written manifests |
    | `auth` slot `cred_<20 hex>` | `auth.slot`: an environment-style name or `cred_<20 hex>` |
    | `transport.http` with `baseUrl` and `path` | `origin` only; the path belongs to the operation, not the capability identity |
    | `transport.local` with `handler` | `command` array |
    | `transport.async` with `runtimeId`, `operation` | `job` with `queue`, `host_bound` |
    | `effectAllowedForMethod` | same rule: DELETE is destructive, POST, PUT and PATCH are write or destructive |
    | read and compute must be `auto` | same rule |
    | `source.kind` (openapi, mcp, bring, web_data, managed_integration) | `provenance.source_kind` (openapi, mcp, affordance, registry, manual) |

    Flyspace's compiler emitting v1alpha1 manifests (Phase 3) closes this, or this table becomes the mapping it implements.

13. **AgentIdentity is stricter than Turtle's TurtleIdentity.** v1alpha1 adds a required `platform`, prefixes ids with `agt_`, and requires `subject_binding` to name a principal (`github:`, `google:`, `service:`). `TurtleIdentity::root` sets `subject_binding` to its own id and Turtle's tests use bare ids, so a Turtle identity does not validate as an AgentIdentity as is. The runtime that creates both maps one to the other.

14. **`OUT_OF_MENU` is a new code.** RISE reports an out-of-menu answer as `INVALID_RESPONSE` (`src/core/decision/recommend.js`, `route.js`) and also emits host-side codes before any call (`INVALID_REQUEST`, `OPTIONS_UNAVAILABLE`, `CATALOG_UNAVAILABLE`) that are not call outcomes. Flyspace returns a bare 502 or 503 with no code. A consumer packet that adopts DecisionOutcome keeps its product-facing codes unchanged by mapping `OUT_OF_MENU` to its current behavior; otherwise the Phase 1 gate (no behavior change) fails.

15. **sybershoke cannot check DecisionOutcome yet.** sybershoke checks its own virtual-time model of RISE's Worker (`crates/shoke-jev`), not DecisionOutcome records. I1 (answers on the menu) and I2 (an answer or a visible error) can be checked from DecisionOutcome plus its call. I3 needs the decision source (model, cache or fallback) and I5 the number of provider calls, which DecisionOutcome does not carry, and Flyspace has no decision cache for I3 to apply to. A sybershoke adapter for the shared client is new work.

16. **The Agent-platform list is closed.** `claude`, `codex`, `cursor`, `grok`, `human`, `host`. A new platform is a schema change.

17. **Ask dates are loosely typed.** `opened` and `expires` must start with `YYYY-MM-DD`, matching MasterMind's ask files (dates) and case approvals (timestamps). When a default takes effect is MasterMind's ledger, not this schema.
