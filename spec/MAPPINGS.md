# Mappings onto AdmissionDecision and EffectOutcome

Every row is **PROPOSED**. Nothing in this file is implemented. Names come from the reference systems as read for this change; they are not a claim that those systems already speak this protocol.

SyberWork separates two questions. `AdmissionDecision.status` is `allowed`, `needs_approval`, or `denied`: was the action legal? `EffectOutcome.state` is `succeeded`, `rejected`, or `unknown`: did the external write happen? `rejected` means the destination declared that it did not write. `unknown` stays unknown until reconciliation. An API `Rejected` is neither of these; it is a refusal to accept the call.

| System | Their outcome | Proposed SyberWork object | Notes |
| --- | --- | --- | --- |
| SyberRuntime | Operation appended; obligations open but not floor-blocking | AdmissionDecision `allowed` | The log records the attempt. Open non-blocking debt is not a denial. |
| SyberRuntime | `StabilizationBlockedError` because floor obligations are open | AdmissionDecision `denied` | `stabilize` refuses to make the artifact authoritative. That is a license decision, not an unknown write. |
| SyberRuntime | `stabilize` appends a `STABILIZE` operation | EffectOutcome `succeeded` | The runtime itself committed the authoritative transition. There is no external write to reconcile. |
| SyberRuntime | Hash-chain mismatch on the operation log | Neither | Integrity failure of the log. SyberWork's analogue is `verify_chain` returning false, which is not an admission reason. |
| Barn | Authorize spawn or reuse | AdmissionDecision `allowed` | Barn remains the authority for that license. Bough advice would not be a decision. |
| Barn | Authorize reject | AdmissionDecision `denied` | The specialist is not licensed. No effect follows. |
| Barn | Independent verification passes | EffectOutcome `succeeded` | Verification here is the evidence that the artifact holds, closest to a completed effect rather than to admission. |
| Barn | Independent verification fails | AdmissionDecision `denied` | The artifact must not become authoritative. This is not `EffectOutcome.rejected`, which SyberWork reserves for a destination no-write status. |
| GrokCell | `admit` | AdmissionDecision `allowed` | Legality passed and a rewrite may proceed. The rewrite itself is a later effect. |
| GrokCell | `hold_unresolved` | AdmissionDecision `needs_approval` | Closest fit only: work is not denied and not yet allowed. SyberWork's `needs_approval` is specifically a missing role approval, not a general hold queue. |
| GrokCell | `reject` | AdmissionDecision `denied` | No rewrite. |
| GrokCell | `outcome_unknown` | EffectOutcome `unknown` | The claim grammar already separates an unknown outcome from a rejection. Reconciliation would be a new adapter, not a text note. |
| Relay | Parked `authorization` call | AdmissionDecision `needs_approval` | The tool call is held until a person authorizes it. It is not an effect. |
| Relay | Parked `answer` call | AdmissionDecision `needs_approval` | Same shape, different missing input. SyberWork has no distinct "needs answer" status. |
| Relay | `AgentRuntimeRefusal` (paused, off, bad mode, budget reservation refused) | Neither; API `Rejected` | The call never becomes a proposal. Mapping it to `denied` would hide that distinction. |
| Relay | Tool result `success: false` | EffectOutcome `rejected` | Only if the tool contract guarantees no write. Otherwise this should be `unknown`. |
| Turtle | Policy effect `permit` | AdmissionDecision `allowed` | The envelope licensed the action. |
| Turtle | Policy effect `deny` | AdmissionDecision `denied` | The envelope refused it. Turtle does not record an external effect outcome in the policy schema. |
