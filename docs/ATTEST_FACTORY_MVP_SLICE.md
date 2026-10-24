# Attest factory MVP slice

**Status:** Source-checkout scaffold; proposed integration boundary. No live Kane or Attest process is invoked by this slice.

## Boundary

SyberWork owns the case contract, case history, admission decision, optional approval, effect execution, and acceptance projection. The bridge treats Attest `RunReceipt`, `AuthorityDecision`, and `HandoffPackage` as opaque JSON records and appends them only as observations. Its JSON Schema describes the SyberWork bridge envelope; it deliberately does not claim to define Attest's upstream wire format.

An Attest decision is not a SyberWork approval, and a receipt's claims are not destination evidence. `adapters/attest_observe.py` only constructs the keyword payload for `Session.observe` or `Work.observe`; it does not write a case, execute an action, or verify a destination. Every payload has `verified: false`, a caller-supplied source version, and an observation key identifying the record type.

## Slice flow

1. **Admit:** create a case under an installed SyberWork contract and policy. Propose the bounded factory action through the existing admission path; do not change `syberlabs/admission.py`.
2. **Observe Attest:** read a receipt JSON object and call `receipt_json_to_observation_payload(receipt, source_version=..., actor=...)`. Append the returned data to the case with `Session.observe(case_id, **payload)` (or the compatible `Work.observe`). An authority decision or handoff package uses `record_to_observation_payload` and remains observation data too.
3. **Optional approve:** if the SyberWork policy requires approval, an independent SyberWork approver calls `approve` on the admitted proposal. Attest text cannot approve it.
4. **Execute and reconcile:** for an external write, an ambiguous commit remains `unknown`. Only the configured destination lookup, verified by SyberWork's reconciliation path, can satisfy an effect acceptance clause. Attest text, Kane browser evidence, a supplied `success` field, or a claimed external ID never substitutes for that check. See [the reconciliation contract](RECONCILIATION.md).

Kane remains the browser-evidence component; this scaffold does not invoke Kane, validate its browser evidence, or turn browser text into destination proof. Attest's diagnosis and handoff can inform case history, while SyberWork retains authority over what is admitted and promoted.

## Offline fixture

Run from a checkout:

```bash
PYTHONPATH=. python3 examples/attest_factory_slice.py
PYTHONPATH=. python3 -m unittest discover -s tests -p test_attest_bridge.py -v
```

The example uses fictional JSON and an in-memory executor that simulates a stored write with a lost acknowledgement, then reports it through a status lookup. It needs no network, Kane, or Attest installation; its simulated reconciliation is not a deployment claim. The test separately exercises `Work` against the repository's local fictional ERP fixture and checks that the Attest observation does not complete effect acceptance before destination reconciliation.

## Scope and limitations

- The wrappers preserve opaque JSON objects; they do not assert the real Attest receipt schema or cryptographically validate receipt provenance.
- The adapter is read-only: the caller explicitly appends the returned observation to a case.
- This change stays within the requested scaffold paths. The current setuptools configuration explicitly lists only `syberlabs` and `syberwork` and packages only `syberlabs/static/*`; the new subpackage and its JSON Schema are therefore source-checkout only until a separate package-configuration change includes them in built wheels.
- This is an implemented/tested scaffold only—not a measurement, deployment, or externally validated integration. No coverage number is claimed.
