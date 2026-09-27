# Gaps between the schemas and the code

The schemas describe SyberWork as it behaves. They are not enforced at runtime except where `prepare_contract` and URL checks now match them. These are the places where a tighter reading would be wrong, or where the protocol name and the stored JSON differ.

1. **EffectOutcome.state is not stored.** `Work.commit` returns `status` with `succeeded`, `rejected`, or `unknown`. `effect_rejected` events reuse `status` for an HTTP code (409, 412, or 428). `effect_succeeded` and `effect_unknown` have no status field; the event kind is the state. `spec/validate.py` projects those events onto `state` only while checking the schema.

2. **AdmissionDecision is the `{status, reason}` object.** The `decision` event body also stores `proposal_id`, `policy_version`, and, on a failed commit recheck, `phase: "commit"`. Replay compares status and reason only.

3. **`install_contract` checks the fields admission indexes, including input kinds.** An acceptance `effect` without `action`, a `signoff` without `role`, a `fact` without `key`, or a `required_facts` item without `key` and `source` is rejected at publish. An input kind other than `string` or `integer` is `invalid_contract`. `create_case` rejects a value of the wrong kind with `input_schema`, including a boolean passed where an integer is required.

4. **`trusted_origin` is procedural.** Source install and `SYBERWORK_PLANNER_URL` now call it. The public errors stay `invalid_source` and `planner_unconfigured`. A host that only begins with `http://127.0.0.1:` is rejected. The schemas still do not encode the parser (no userinfo, no fragment, path required, HTTP only for `127.0.0.1` with a port).

5. **Proposals are appended before admission.** A non-dict `args` or a missing role list is stored, then denied with `invalid_proposal_shape`. The Proposal schema therefore does not require `args` to be an object.

6. **Amount checks use `type()` and `math.isfinite`.** `True` is not an amount (`amount_required`). `NaN` and infinities are not finite, so they are `amount_required` rather than compared to the limit. Negative numbers fail as `amount_exceeds_limit`.

7. **Acceptance clause ids must be unique in code.** The clause schema cannot say that by itself.

8. **`compiled_path` is optional on input.** Install writes the compiled path into the stored document. Examples already include one.

9. **Inspect projects resolution status.** `open`, `overdue`, `escalated`, `completed`, and `cancelled` are computed. The stored `resolution_requested` body has no status field. `ResolutionTask` describes the stored body.

10. **A legacy `reconciled` body may carry `evidence` and no `proof`.** `verified_reconciliation` does not treat it as success. The schema allows that historical shape.

11. **Contracts, policies, sources, and actions keep unknown properties.** Install stores canonical JSON of the document it accepted. The schemas set `additionalProperties` so those documents still validate.

12. **The checker treats only a non-bool `int` as `integer`.** That matches `type(value) is int` in `create_case`. Draft 2020-12 would also accept `1.0`.

13. **`trusted_origin` is not encoded as a pattern.** It rejects userinfo, fragments, missing paths, and non-loopback HTTP by parsing the URL. The action schema only requires a string URL for `kind: http`.

14. **The deciding rule name is not part of the hashed decision.** `explain` and `Work.explain_admission` return `rule` plus module, symbol, and line. They do not append an event. The public decision and the hashed body stay `{proposal_id, policy_version, status, reason}` plus an optional `phase`. The rule id is written on the side channel (`event_side.rule`, or `Session.side_channel`) and is not an input to `hash`. The file path and line stay inspection-only.

15. **Economic reservations are application state.** `economic.reserve` returns no decision for an action that is not `economic_http` and whose policy has no `economic` block, so non-economic traces stay the same. A committed `economic_http` effect stores `budget_id` and three snapshot digests on `effect_started` only. The budget total lives in `economic_reservations`, which is not part of the hash chain.
