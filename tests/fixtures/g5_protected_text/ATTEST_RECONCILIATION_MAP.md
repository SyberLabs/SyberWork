# Attest artifacts and SyberWork reconciliation

This mapping is a one-way import boundary: Attest records what its own run or
review produced; SyberWork owns case admission, external effects, and
reconciliation. Attest receipt files are emitted at
`<attest_home>/receipts/<case_id>.json` with schema identifier
`attest.receipt/v0` and top-level keys `schema`, `case_id`, `created_at`,
`doc`, `claim`, `authority`, `proposal`, `approval`, `evidence`, `handoff`,
`disposition`, and `verified` (Attest emits `verified: false`). The receipt's
`verified` field is always `false`. Spec mode may also add
`ticket` (`path`, `title`), `app` (`original`, `candidate`, `patch_sha256`),
and `worker`. SyberWork treats the file and payload as observation data: even
`evidence.after.status == "proven"` means only that Kane re-checked the app
claim, not that a SyberWork destination write occurred. Do not translate an
Attest artifact into `effect_succeeded`, `reconciled`, or a successful
`reconciliation_checked` result.

| Attest artifact | SyberWork event kind | What it may license |
| --- | --- | --- |
| Receipt file `<attest_home>/receipts/<case_id>.json` (`attest.receipt/v0`) | `observed` (unverified observation) | Record receipt fields as Attest claims. `evidence.after.status == "proven"` may describe Kane's app-claim re-check, but is not destination-write proof. Never `effect_succeeded`, verified `reconciled`, or acceptance. |
| `RunReceipt` | `observed` (unverified observation) | Record Attest execution, comparison, authority, evidence, and repair states as Attest claims only. Never proof of a SyberWork destination effect or acceptance. |
| `AuthorityDecision` | `observed` (unverified observation) | Record the decision as an observation. It is not a SyberWork `decision` or `approved` event and does not authorize an effect or acceptance. |
| `HandoffPackage` | `observed` (unverified handoff reference) | Hand the package to a human for review. A human may proceed through SyberWork's separate admission and approval path; the handoff itself never means `reconciled` or accepted. |
| Unknown kind, or an artifact presented as external-write proof | none — refuse | Do not create a success, reconciliation, or approval event from it. Unknown kinds fail closed. |

The observation should retain the source and version/provenance of the Attest
artifact and must not be marked as destination-verified. The pure classifier in
[`syberlabs/attest_reconcile_map.py`](../syberlabs/attest_reconcile_map.py)
returns one of `observation`, `handoff`, or `refuse`; its
`verified_reconciliation` result is always `false`.

## Destination verification remains separate

An Attest artifact cannot emit `reconciliation_checked` or `reconciled`.
Kane's `evidence.after.status == "proven"` is app-behavior evidence, not a
SyberWork external-effect lookup. A `reconciliation_checked` event is produced
by SyberWork when its configured status lookup is pending or mismatched; it
does not satisfy acceptance. A verified `reconciled` event is produced only
after SyberWork calls the installed action's `status_url` and matches the
committed state, original idempotency key, exact request digest, and nonempty
destination external ID. The resulting
proof includes the destination response digest. Caller-provided success or
evidence is refused. A 404 is recorded as pending (`not_found_not_proof`): it
never proves that no write occurred, since a destination may be eventually
consistent. A 404 therefore cannot release the unresolved effect or satisfy
acceptance.

See the [destination reconciliation contract](RECONCILIATION.md) and the
[reconciliation event schema](../spec/reconciliation.schema.json). In
particular, only the schema's `verified` shape can license acceptance;
`checked` and historical `legacy` shapes cannot. The Attest receipt schema is
not duplicated or validated here; its payload remains untrusted observation
data regardless of its internal claims.
