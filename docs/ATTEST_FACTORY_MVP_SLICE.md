# Attest factory MVP slice

**Status:** Source-checkout scaffold; proposed integration boundary. No live Kane or Attest process is invoked by this slice.

## Boundary

SyberWork owns the case contract, case history, admission decision, optional approval, effect execution, and acceptance projection. The bridge preserves Attest `RunReceipt`, `AuthorityDecision`, and `HandoffPackage` records as observation data. The receipt-file reader enforces the `attest.receipt/v0` core shape and the optional spec-mode `ticket`, `app`, and `worker` fields, rejecting unknown top-level fields; it does not authenticate signatures or grant SyberWork authority. The JSON Schema describes the SyberWork bridge envelope and the receipt contract it accepts.

An Attest decision is not a SyberWork approval. For `disposition: "implemented"`, the receipt projection names the ticket title and `app.patch_sha256`; when `evidence.after.status == "proven"`, it records the contract's Kane re-check as destination verification. These remain observation data: every payload has top-level `verified: false`, and neither projection creates a SyberWork reconciliation event or settles an unrelated external effect. `adapters/attest_observe.py` only constructs the keyword payload for `Session.observe` or `Work.observe`; it does not write a case or execute an action.

## Slice flow

1. **Admit:** create a case under an installed SyberWork contract and policy. Propose the bounded factory action through the existing admission path; do not change `syberlabs/admission.py`.
2. **Observe Attest:** import an existing receipt file with `receipt_file_to_observation_payload(path, actor=...)`, or convert a parsed JSON object with `receipt_json_to_observation_payload`. Append the returned data to the case with `Session.observe(case_id, **payload)` (or the compatible `Work.observe`). An authority decision or handoff package uses `record_to_observation_payload` and remains observation data too. Each receipt observation has `verified: false`.
3. **Optional approve:** if the SyberWork policy requires approval, an independent SyberWork approver calls `approve` on the admitted proposal. Attest text cannot approve it.
4. **Execute and reconcile:** for a SyberWork external write, an ambiguous commit remains `unknown`. Only the configured destination lookup, verified by SyberWork's reconciliation path, can satisfy that effect's acceptance clause. The Attest `evidence.after.status == "proven"` projection records Kane's re-check under the Attest contract; it does not independently authenticate that evidence or reconcile a separate SyberWork effect. See [the reconciliation contract](RECONCILIATION.md).

Kane remains the browser-evidence component; this scaffold does not invoke Kane or independently authenticate its evidence. Attest's diagnosis, implementation summary, and handoff can inform case history, while SyberWork retains authority over what is admitted and promoted.

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

- Attest writes receipt files at `<attest_home>/receipts/<case_id>.json`. Receipt files must use schema `attest.receipt/v0`; the bridge rejects unknown top-level fields and a receipt that claims `verified: true`. It preserves the input record, projects the spec implementation summary and Kane re-check when present, and keeps the imported observation `verified: false`.
- The adapter is read-only: the caller explicitly appends the returned observation to a case.
- This change stays within the requested scaffold paths. The current setuptools configuration explicitly lists only `syberlabs` and `syberwork` and packages only `syberlabs/static/*`; the new subpackage and its JSON Schema are therefore source-checkout only until a separate package-configuration change includes them in built wheels.
- This is an implemented/tested scaffold only—not a measurement, deployment, or externally validated integration. No coverage number is claimed.
