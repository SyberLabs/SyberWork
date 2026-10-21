# Scenario suite

Status: **measured** on `syberlabs.Session` and, for the two candidate cases, `syberwork.Work`. Fictional operations. Not a customer. Not a run of another product.

`tests/test_scenario_suite.py` loads these modules and compares `probe_failures` with `expected_failures`. A scenario with an empty failure list held. A scenario with a named failure exposed a gap that is still present.

| Scenario | Operation | Result |
| --- | --- | --- |
| `stale_quote` | Spare-part quote older than 15 minutes | Refused `stale_fact:quote`. No debit. |
| `self_approval` | Operator who also holds treasurer approves their own wire | Refused `approval_denied`. No debit. |
| `model_skips_step` | Model publishes a release before CI is recorded | Refused `required_prior_effect_missing`. No effect started. |
| `unverified_revocation` | Reviewer types a directory roster | Refused `source_verification_required:roster`. |
| `over_limit` | Wire of 9000 against a 5000 ceiling | Refused `amount_exceeds_limit`. No debit. |
| `model_cannot_promote` | Evaluated candidate, model origin proposes promotion | Refused `candidate_promotion_origin`. |
| `registrar_cannot_evaluate` | The host that registered a candidate evaluates it | Refused `evaluation_denied`. |
| `tampered_chain` | Actor field rewritten on a stored event | Original chain verifies. The forged copy does not. |
| `asserted_invoice` | Host marks `INV-GHOST` verified. The world has no such invoice | **Gap.** The payment is admitted. Probe `unknown_invoice_refused` fails. `Session.observe(..., verified=True)` does not read the world. |
| `lying_reconciler` | Bank debits, status reports `not_applied`, a second wire is sent | **Gap.** Two debits land. Probe `single_debit` fails. Reconciliation trusts `executor.status()`. |

`flaky_bank` remains the comparison against stand-ins. Its table is unchanged. These ten modules run on `SessionHost` only.

## As a check

`benchmarks/run_scenarios.py` replays every scenario module in-process on `SessionHost` and compares the report with the record in `benchmarks/scenarios/expected.json` (written once on `main` with `--record`) and with each module's `expected_failures`. It is standard library only, starts no server, and reads no clock: `world.Clock` is fixed and the reports carry no ids, so two runs print the same lines and the same digest.

```
python benchmarks/run_scenarios.py [--scenario NAME] [--json] [--expected PATH]
```

Exit codes: `0` when every recorded expectation holds; `1` on the first divergence, printed as `divergence: <scenario>: <field>: expected X, observed Y`; `2` when a scenario module or the record is malformed (no `SCENARIO`, a probe with an unsupported field, `expected_failures` naming an unknown probe, an unreadable record, an unknown `--scenario`). The last line always carries a SHA-256 digest of the replayed reports, so the kernel's own output digest (`syberlabs/checks.py`) and the runner's digest can be compared across hosts.

To run it as a contract check next to the unit tests and conformance:

```json
"scenarios": {"argv": [<sys.executable>, "benchmarks/run_scenarios.py"], "timeout_seconds": 600}
```

A divergence means the candidate changed what the SDK does under a recorded operation: a refusal that no longer happens, a debit that now lands or no longer lands, a chain that no longer verifies, or a known gap (`asserted_invoice`, `lying_reconciler`) that closed. The last case is progress, and it still fails the check until the record is rewritten with `--record` in a reviewed change, so that an expectation never moves silently. The two gap scenarios are recorded as failing their named probe; the runner compares `probe_failures` with `expected_failures` before anything else.

All eleven modules replay in-process. Two (`model_cannot_promote`, `registrar_cannot_evaluate`) open a `syberwork.Work` on a temporary SQLite file. None needs the HTTP service or PostgreSQL. The Builder coordination substrate (`syberlabs/builder.py`, `syberwork/coordination.py`, `builder_http.py`) has no scenario module yet; its behaviour is exercised by unit tests only.

Add a module under `benchmarks/scenarios/` that publishes `SCENARIO`. Set `expected_failures` to the probe ids that should fail on the current SDK. An empty list means the scenario is a strength. A named id means the run is expected to show that weakness until the SDK changes.
