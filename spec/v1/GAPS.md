# Gaps in v1alpha1

Where a schema cannot say the whole rule, where the code does not yet do what the protocol says, or where a fact could not be confirmed. Each gap names the packet that would close it.

1. **No runtime enforces v1alpha1.** The schemas and fixtures are a specification. `syberlabs` writes no EventAttribution, checks no GrantRef, and calls no DecisionCall. Closed by the SyberSDK packets in Phase 2 of the consolidation plan.

2. **Root identity equality is procedural.** A root Agent has `parent_id: null` and `root_id == id`; a child shares its parent's `root_id`. JSON Schema cannot compare two fields, so `spec/v1/check.py` does not check it either. The runtime that writes identities must.

3. **Attenuation is Turtle's, and Turtle P0 has known gaps.** GrantRef pins a policy; it does not encode clauses. turtle-shells P0 ignores `ctx.now` for expiry, never settles budgets, does not roll back a multi-metric reservation, does not match the credential on a request, and leaves `DecisionEvidence.policy_digest` empty. Until those are fixed, a GrantRef is a pin, not an enforced boundary. Closed in turtle-shells, then by an `authority.grant` admission rule.

4. **`policy_digest` has no agreed encoding yet.** Turtle domain-separates its digest with `turtle-policy-v0alpha1\n`; this protocol only requires 64 hex characters. The fixture digests are placeholders. The Turtle packet that fills `DecisionEvidence.policy_digest` defines it.

5. **Noul and score answer fields are unconfirmed.** KevRT documents that noul returns p(yes) and score returns an expectation over levels, but no repository records the response field names for them. DecisionOutcome therefore requires only `type` on those answers, and `type` plus `choice` on a choice answer. Closed by capturing one real Kev response of each type into the fixtures.

6. **Menu membership is procedural.** `OUT_OF_MENU` means the answer was not one of the criteria keys the host offered. The schema cannot relate an outcome to its call. The Decision Port client checks it, and sybershoke invariant I1 (`PlansOnMenu`) checks it over histories.

7. **`request_digest` is optional.** RISE and Flyspace do not hash their requests today. It becomes required when the shared Decision Port client writes outcomes into a case.

8. **Capability ids have two forms.** Dotted names (`video.render`) for hand-written manifests, and Flyspace's `cap_<hash>` for compiled ones. Both are accepted until Flyspace's compiler emits v1alpha1 manifests (Phase 3).

9. **Dates in Ask are loosely typed.** `opened` and `expires` must start with `YYYY-MM-DD`, matching MasterMind's ask files, which use dates, and case approvals, which use timestamps. Silence handling (when a default takes effect) is MasterMind's ledger, not this schema.

10. **A keys default must be inaction, and the schema cannot tell.** MasterMind's keys asks carry a `default_on_silence` that keeps the status quo (ASK-009: Contact stays parked). v1alpha1 allows a keys default for that reason and forbids a default only on `approve`. That a keys default performs no effect is a rule for MasterMind's ledger and any runtime that applies defaults.

11. **Evidence `verified` is not checkable from the document.** Only the host knows whether a configured source or checker produced it. A model-reported `verified: true` is a runtime violation the schema cannot see.

12. **The Agent-platform list is closed.** `claude`, `codex`, `cursor`, `grok`, `human`, `host`. A new platform is a schema change.
