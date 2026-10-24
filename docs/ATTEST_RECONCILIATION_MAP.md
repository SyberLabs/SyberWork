# Attest artifacts and SyberWork reconciliation

This mapping is a one-way import boundary: Attest records what its own run or
review produced; SyberWork owns case admission, external effects, and
reconciliation. An Attest receipt or handoff is not evidence that a destination
write occurred. Do not translate an Attest artifact into `effect_succeeded`,
`reconciled`, or a successful `reconciliation_checked` result.

| Attest artifact | SyberWork event kind | What it may license |
| --- | --- | --- |
| `RunReceipt` | `observed` (unverified observation) | Record the receipt as an observation, such as its provenance, digest, or Attest run state. Observation only; never an effect outcome or acceptance. |
| `AuthorityDecision` | `observed` (unverified observation) | Record the decision as an observation. It is not a SyberWork `decision` or `approved` event and does not authorize an effect or acceptance. |
| `HandoffPackage` | `observed` (unverified handoff reference) | Hand the package to a human for review. A human may proceed through SyberWork's separate admission and approval path; the handoff itself never means `reconciled` or accepted. |
| Unknown kind, or an artifact presented as external-write proof | none — refuse | Do not create a success, reconciliation, or approval event from it. Unknown kinds fail closed. |

The observation should retain the source and version/provenance of the Attest
artifact and must not be marked as destination-verified. The pure classifier in
[`syberlabs/attest_reconcile_map.py`](../syberlabs/attest_reconcile_map.py)
returns one of `observation`, `handoff`, or `refuse`; its
`verified_reconciliation` result is always `false`.

## Destination verification remains separate

An Attest artifact cannot emit `reconciliation_checked` or `reconciled`. A
`reconciliation_checked` event is produced by SyberWork when its configured
status lookup is pending or mismatched; it does not satisfy acceptance. A
verified `reconciled` event is produced only after SyberWork calls the installed
action's `status_url` and matches the committed state, original idempotency
key, exact request digest, and nonempty destination external ID. The resulting
proof includes the destination response digest. Caller-provided success or
evidence is refused.

See the [destination reconciliation contract](RECONCILIATION.md) and the
[reconciliation event schema](../spec/reconciliation.schema.json). In
particular, only the schema's `verified` shape can license acceptance;
`checked` and historical `legacy` shapes cannot.
