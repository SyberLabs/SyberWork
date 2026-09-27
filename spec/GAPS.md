# Gaps between the schemas and the code

The schemas describe SyberWork as it behaves. They are not enforced at runtime. These are the places where a tighter reading would be wrong, or where the protocol name and the stored JSON differ. None of these were changed.

1. **EffectOutcome.state is not stored.** `Work.commit` returns `status` with `succeeded`, `rejected`, or `unknown`. `effect_rejected` events reuse `status` for an HTTP code (409, 412, or 428). `effect_succeeded` and `effect_unknown` have no status field; the event kind is the state. `spec/validate.py` projects those events onto `state` only while checking the schema.

2. **AdmissionDecision is the `{status, reason}` object.** The `decision` event body also stores `proposal_id`, `policy_version`, and, on a failed commit recheck, `phase: "commit"`. Replay compares status and reason only.

3. **`install_contract` does not check the shapes admission later indexes.** A `required_facts` item without `key`, an acceptance `effect` clause without `action`, or an input kind other than `string` or `integer` can be published. Admission or acceptance then raises `KeyError`, or `create_case` accepts any value for an unrecognized input kind because the kind test is two `and` clauses joined by `or`.

4. **Source URLs and planner URLs use a prefix test.** `install_source` requires `https://` or `http://127.0.0.1:` and the substring `{key}`. `model_propose` requires the same prefix on `SYBERWORK_PLANNER_URL`. Neither path uses `trusted_origin`. A host that only begins with `http://127.0.0.1:` passes. Action install does use `trusted_origin`. This was left as-is.

5. **Proposals are appended before admission.** A non-dict `args` or a missing role list is stored, then denied with `invalid_proposal_shape`. The Proposal schema therefore does not require `args` to be an object.

6. **Amount checks use `type()`.** `True` is not an amount (`amount_required`). A `NaN` float is not greater than the limit and is not less than zero, so it passes. Negative numbers fail as `amount_exceeds_limit`.

7. **Acceptance clause ids must be unique in code.** The clause schema cannot say that by itself.

8. **`compiled_path` is optional on input.** Install writes the compiled path into the stored document. Examples already include one.

9. **Inspect projects resolution status.** `open`, `overdue`, `escalated`, `completed`, and `cancelled` are computed. The stored `resolution_requested` body has no status field. `ResolutionTask` describes the stored body.

10. **A legacy `reconciled` body may carry `evidence` and no `proof`.** `verified_reconciliation` does not treat it as success. The schema allows that historical shape.

11. **Contracts, policies, sources, and actions keep unknown properties.** Install stores canonical JSON of the document it accepted. The schemas set `additionalProperties` so those documents still validate.

12. **The checker treats only a non-bool `int` as `integer`.** That matches `type(value) is int` in `create_case`. Draft 2020-12 would also accept `1.0`.

13. **`trusted_origin` is not encoded as a pattern.** It rejects userinfo, fragments, missing paths, and non-loopback HTTP by parsing the URL. The action schema only requires a string URL for `kind: http`.

14. **The deciding rule name is not part of the stored decision.** `syberlabs.admission.admit` returns `rule` for inspection, including module, symbol, and line via `rule_provenance`. `Work._admit` drops `rule` before the decision is returned or appended, so decision events stay `{proposal_id, policy_version, status, reason}` plus an optional `phase`. Putting a source path or line number in the hash would make replay report a difference that is not semantic.
