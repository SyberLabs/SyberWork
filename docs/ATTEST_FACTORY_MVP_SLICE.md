# Attest factory MVP slice

**Status:** Source-checkout scaffold; proposed integration boundary. No live Kane or Attest process is invoked by this slice.

## Boundary

SyberWork owns the case contract, case history, admission decision, optional approval, effect execution, and acceptance projection. The bridge treats Attest `RunReceipt`, `AuthorityDecision`, and `HandoffPackage` as opaque JSON records and appends them only as observations. The receipt-file reader accepts the `attest.receipt/v0` schema tag and preserves the receipt object as payload; it does not validate signatures, authority, or the rest of Attest's upstream schema. The JSON Schema describes only the SyberWork bridge envelope.

An Attest decision is not a SyberWork approval, and a receipt's claims are not destination evidence. `adapters/attest_observe.py` only constructs the keyword payload for `Session.observe` or `Work.observe`; it does not write a case, execute an action, or verify a destination. Every payload has `verified: false`, a caller-supplied source version, and an observation key identifying the record type.

## Slice flow

1. **Admit:** create a case under an installed SyberWork contract and policy. Propose the bounded factory action through the existing admission path; do not change `syberlabs/admission.py`.
2. **Observe Attest:** import an existing receipt file with `receipt_file_to_observation_payload(path, actor=...)`, or convert a parsed JSON object with `receipt_json_to_observation_payload`. Append the returned data to the case with `Session.observe(case_id, **payload)` (or the compatible `Work.observe`). An authority decision or handoff package uses `record_to_observation_payload` and remains observation data too. Each receipt observation has `verified: false`.
3. **Optional approve:** if the SyberWork policy requires approval, an independent SyberWork approver calls `approve` on the admitted proposal. Attest text cannot approve it.
4. **Execute and reconcile:** for an external write, an ambiguous commit remains `unknown`. Only the configured destination lookup, verified by SyberWork's reconciliation path, can satisfy an effect acceptance clause. Attest text, Kane browser evidence, a supplied `success` field, or a claimed external ID never substitutes for that check. See [the reconciliation contract](RECONCILIATION.md).

Kane remains the browser-evidence component; this scaffold does not invoke Kane, validate its browser evidence, or turn browser text into destination proof. Attest's diagnosis and handoff can inform case history, while SyberWork retains authority over what is admitted and promoted.

## Offline fixture

Run from a checkout:

```bash
# Offline fixture receipt
PYTHONPATH=. python3 examples/attest_factory_slice.py

# Import a receipt from a completed Attest run
PYTHONPATH=. python3 examples/attest_factory_slice.py --receipt "$ATTEST_HOME/receipts/CASE_ID.json"
PYTHONPATH=. python3 adapters/attest_observe.py "$ATTEST_HOME/receipts/CASE_ID.json"

PYTHONPATH=. python3 -m unittest discover -s tests -p test_attest_bridge.py -v
```

The receipt-path option reads JSON already written by Attest; it does not start Attest or Kane. Both CLI modes then run the same fictional SyberWork case and in-memory destination fixture. Any displayed `verified` reconciliation result is explicitly from that simulated destination lookup, not from the Attest receipt. The focused test checks receipt-file parsing and separately exercises `Work` against the repository's local fictional ERP fixture to prove an Attest observation cannot complete effect acceptance before destination reconciliation.

## Scope and limitations

- Attest writes receipt files at `<attest_home>/receipts/<case_id>.json`. Receipt files must use schema `attest.receipt/v0`; the bridge preserves their JSON object but does not cryptographically validate receipt provenance or turn the `authority`, `proposal`, `approval`, or `evidence` text into destination proof. Imported observations always have `verified: false`.
- The adapter is read-only: the caller explicitly appends the returned observation to a case.
- This change stays within the requested scaffold paths. The current setuptools configuration explicitly lists only `syberlabs` and `syberwork` and packages only `syberlabs/static/*`; the new subpackage and its JSON Schema are therefore source-checkout only until a separate package-configuration change includes them in built wheels.
- This is an implemented/tested scaffold only—not a measurement, deployment, or externally validated integration. No coverage number is claimed.
