# Research and red team: closing the standards gap

Date of this pass: 2026-09-27. The question is not "which product should replace this SDK". It is which properties the leading standards already require, where this repository fails them, and which change closes each gap without pretending the SDK is something else.

Standards used, and why:

| Standard | What it is the reference for |
| --- | --- |
| [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) JSON Canonicalization Scheme | Byte-identical JSON for hashes across languages |
| [DSSE](https://github.com/secure-systems-lab/dsse) and the [in-toto statement envelope](https://github.com/in-toto/attestation/blob/v1.0/spec/v1.0/envelope.md) | A signature over payload bytes and a payload type, so the verifier does not parse before it checks |
| [SLSA Provenance v1.2](https://slsa.dev/spec/v1.2/build-provenance) | A claim names the builder, the external inputs, and the resolved dependencies |
| [CloudEvents 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md) | The envelope other systems already route: `id`, `source`, `specversion`, `type` |
| [Cedar authorization](https://docs.cedarpolicy.com/auth/authorization.html) | A request is principal, action, resource, and context. The response names the policies that decided. Policies are validated against a schema before they are stored |
| Sigstore Rekor | An append-only log of signed envelopes, so the database owner is not the only witness |

Claim grades are **MEASURED** (a command or a direct read of this tree), **KNOWN** (the code does this and the docs already say so), or **PROPOSED** (a closure, not built).

## What already matches

These are not gaps. A closure plan that throws them out would get worse.

- Admission and effect are different answers. Cedar and OPA stop at allow or deny. This SDK also records whether the write happened. That split should stay.
- A planner returns `{action, args}` and does not hold the executor credential. **KNOWN** from `syberlabs/planner.py` and `Work.model_propose`.
- Commit runs admission again before an effect. **KNOWN** from `Work.commit` and `Session.commit`.
- HTTP redirects are not followed. Source and action URLs reject userinfo, fragments, and non-loopback HTTP. **KNOWN** from `syberlabs/targets.py`.
- The operator API binds to loopback, caps a JSON body at 1 MB, compares token hashes with `hmac.compare_digest`, and sets a content security policy on the console. **KNOWN** from `syberwork/server.py`.
- SQL uses bound parameters. **KNOWN** from `syberwork/core.py`.

## Findings

### 1. The digest is not RFC 8785

**MEASURED** against `rfc8785` 0.1.4, which was installed only for this comparison and is not a project dependency:

| Value | `syberlabs.canonical.canonical` | RFC 8785 |
| --- | --- | --- |
| `1.0` | `1.0` | `1` |
| `-0.0` | `-0.0` | `0` |
| keys U+E000 and U+1F600 | private-use character first | emoji first (UTF-16 code units) |
| ASCII keys `Z`, `a` | same bytes | same bytes |
| `NaN`, `Infinity` | the tokens `NaN` and `Infinity` | rejected (`FloatDomainError`) |

`canonical` is `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. Python's sort is by Unicode code point. RFC 8785 sorts object keys as UTF-16 code units and serializes numbers with the ECMAScript rules. DSSE's authors dropped JSON canonicalization from the signature layer because small differences like these fork the hash.

A second language cannot verify a history from this database and get the same digest, even if it implements the prose in `spec/SPEC.md`.

**Close it** in a new protocol version, not by editing `canonical` in place. Histories written on `main` (`a2f909b`) must keep verifying, and those digests use today's function. Dual-write: keep the current digest as the chain link, and add a JCS digest beside it for new events. Verifiers accept the old link until every stored event has a JCS digest. Do not add the `rfc8785` package as a runtime dependency; a stdlib JCS subset for the types this SDK actually stores (strings, finite ints, lists, objects, bools, null) is enough if non-finite floats are rejected first.

### 2. The hash chain is not an attestation

**KNOWN.** `event_digest` is SHA-256 over the canonical envelope. `verify_chain` recomputes that. Anyone who can write the database can rewrite every row and recompute every link. The README already says this. DSSE requires a signature over the payload and the payload type, checked before the payload is trusted. SLSA provenance names the builder separately from the claim. Rekor keeps a witness outside the database.

**Close it** by signing the latest chain head, not each event's JSON. The signed bytes are the head hash plus the case id and the event count. The signing key lives outside the database (an environment key or a KMS). Verification is: the chain links still match, and the head signature matches a trusted key. A transparency-log entry for that head is the step after the signature, not the first step. Do not put the signature inside the hashed body; that is a different object, the way DSSE wraps a statement.

### 3. Two effects of one action can be in flight

**KNOWN** from `syberlabs/admission.py`. `effect.not_completed` denies only `effect_succeeded` and a verified reconciliation. `effect.unresolved` denies only `effect_unknown`. `effect_started` does not deny a second proposal of the same action.

`Work.commit` holds `BEGIN IMMEDIATE` only until `effect_started` is stored. The HTTP call happens after that transaction commits. A second proposal of the same action can pass admission in a later transaction and start a second call. `Session` has no lock around that sequence at all.

Industry durable-execution SDKs treat "started and not finished" as occupying the action. This repository's comment on `commit` says the claim is one effect. The admission rule does not enforce that.

**Close it** by extending `effect.unresolved` so an `effect_started` with no later `effect_succeeded`, `effect_rejected`, or verified reconciliation for that proposal denies the same action. `effect_rejected` must still allow a fresh proposal. This is a behavior change. It needs a golden trace for two proposals of one action, and it must not change the existing 20 traces, which do not start two effects of one action.

### 4. Non-finite amounts and unknown input kinds pass

**KNOWN** from `limits.amount` and `create_case`, and **MEASURED** for the serializer. `NaN` is a float, and every comparison with `NaN` is false, so it is neither above the limit nor below zero. `create_case` rejects a bad string or a bad integer only because of how `and` and `or` bind. Any other input kind accepts any value. `canonical` then emits `NaN` or `Infinity`, which is not JSON. RFC 8785 rejects those values instead of hashing them.

**Close it** in the current version. Reject non-finite amounts with `amount_required`. Reject an input kind other than `string` or `integer` with `invalid_contract` at publish, and reject a value of the wrong kind in `create_case` with `input_schema`. Neither case is in the golden traces. Do not invent a new reason code for these.

### 5. The schema does not govern the decision

Cedar validates a policy against a schema before the policy is stored, and the authorizer returns the policies that determined the decision. This repository's JSON Schemas are checked by `spec/validate.py` in tests. `Work.install_contract` checks the fields admission indexes. Other documents are stored as canonical JSON with extra properties kept. A second implementation can accept a document this one rejects, or the reverse. The determining rule is available from `explain_admission` and is deliberately absent from the hashed decision, so an auditor who has only the database cannot see which rule fired without re-running this Python package.

**Close it** in two layers. Enforce the v0alpha1 schema inside `prepare_contract`, `install_policy`, and `install_action` for new writes, and keep the current extra-property behavior only for documents already stored. Log the rule id (`economic.reserve`, not a file path or a line number) in a new event kind or a side record. Do not put a path or a line number into the hash. Line numbers change when the file is edited and would fork replay for no semantic reason.

### 6. The planner receives the whole case

**KNOWN** from `Session.suggest` and `Work.model_propose`. The planner context is `objective`, `allowed_actions`, the full contract, every event, and acceptance. Cedar's context is the attributes the policy named. Sending every observation gives the planner values the contract did not ask it to see.

**Close it** by a planner view: action names, the argument names the contract binds, the keys and sources of required facts, and acceptance results. Not the fact values, not unrelated events, not credentials. The existing tests assert the current key set. Changing the view is a behavior change and needs those tests updated on purpose.

### 7. "Verified" and signoff are caller claims

On the HTTP API, a manual fact is stored unverified, and only `refresh_fact` sets `verified: true`. **KNOWN** from `syberwork/server.py`. `Session.observe` and `Work.observe` accept `verified=True` from the caller. A project on `Session` can satisfy `source_verification_required` by saying so.

`approve` refuses the proposal's own actor. `signoff` checks that the role is in the caller's role list and does not check that the signer is someone other than the actor who committed the effect. One local token that holds both roles completes both steps.

**Close it** for the SDK by making `verified=True` a method that only a source read can call (`refresh` / a host callback that returns the bytes and the version). The access-review example should pass a fetcher, not a boolean. For signoff, require the signer's actor to differ from the actor on the effect the clause waits for, using the same independence rule approval already uses. Both are behavior changes for callers who currently self-attest.

### 8. The envelope does not route anywhere else

CloudEvents requires `id`, `source`, `specversion`, and `type`. This envelope is `case_id`, `seq`, `kind`, `body`, `at`, `previous`, `hash`. `at` is a Python float from `time.time()`, not an RFC 3339 timestamp. `Session` accepts any injected clock, so a test clock can also make a stale fact look fresh. SLSA separates external parameters from what the platform resolved. Here the contract, the policy version, and the observation version are inside the body, which is the right raw material, but there is no exported statement a SLSA or in-toto verifier can read.

**Close it** with an export function, not a new chain. Map one event to a CloudEvent (`specversion` `1.0`, `source` `https://sdk.syberlabs.space/v0alpha1`, `type` `space.syberlabs.sdk.case.<kind>`, `id` `case_id:seq`). Map a finished case to an in-toto statement whose subject is the chain head and whose predicate names the contract id and version, the policy version, and the observation hashes the acceptance clauses used. Keep the internal envelope. Changing `at` to an integer microsecond timestamp belongs in the same protocol version as the JCS digest, because `at` is inside the current hash.

### 9. Two runtimes, one rule list, two copies of everything else

`Session` and `Work` both implement propose, commit, compiled binding, and explain. Admission is shared. Binding and the effect claim are not. They already match on the access-review path. They will not stay matched by accident. `Work` is also about 40× slower per case on the measured benchmark because each call opens a SQLite transaction (`benchmarks/results/dev-session.txt`). That is a connection-per-call cost, not an admission cost.

**Close it** by one case store interface used by both: append, read prefix, and a single-flight claim. `Session` is the in-memory store. `Work` is the SQLite store. HTTP execution stays on `Work`. `compiled_propose` moves to one function. Reuse one SQLite connection for the life of a `Work` object, still with `BEGIN IMMEDIATE` around the claim.

### 10. Egress and error text

`trusted_origin` allows any HTTPS host. An administrator token can install an action URL the process can reach, including a link-local address, as long as the URL has a path and no userinfo. Destination failures are stored as `str(exc)[:200]` or `[:400]` on `effect_unknown` and `source_unavailable`. Those strings often contain the URL that was called.

**Close it** with an allowlist of hosts on the installed action, defaulting to the host that was published, and by storing an error code (`destination_unreachable`, `destination_status`) instead of the exception text. The operator can still read the connector logs on the host.

## What not to adopt

- Do not replace the admission registry with Cedar or OPA. Those systems do not record an effect or pin a contract version to a case. A Cedar policy can sit beside the registry later for the role and attribute checks. It does not become the chain.
- Do not replace `Session` with Temporal. Temporal owns retries and worker failover. This SDK owns whether the action was legal. A Temporal activity may call `commit`. The activity is not the license.
- Do not switch existing hashes to RFC 8785 in place. That makes `verify_chain` fail on the `main` fixture and on every case already stored.

## Order of work

1. **Current version, no new hash.** Reject non-finite amounts. Reject unknown input kinds. Treat an in-flight `effect_started` as occupying that action. Stop storing raw destination exception text. Add the golden traces those three admission changes require, and confirm the existing 20 still match.
2. **One store.** Move compiled binding and the effect claim behind the store interface. Keep HTTP on `Work`. Hold one SQLite connection.
3. **Protocol version.** Integer timestamps, a JCS digest beside the current digest, a stable rule id outside the old decision body, and a CloudEvents export. Publish conformance vectors as the JCS bytes plus the expected SHA-256, so another language can fail closed.
4. **Witness.** Sign the chain head with a key that is not in the database. A transparency log is the step after a verifier exists outside this process.
5. **Planner view and signoff independence.** Shrink the planner context. Require the signer to be a different actor from the effect actor.

Items 1 and 2 are code in this repository. Item 3 is a version bump when it changes bytes that are hashed. Items 4 and 5 are product boundaries: a key and an identity that this process does not invent for itself.

## Status after the implementation pass

The chain link is still `canonical`. Histories from `main` (`a2f909b`) keep verifying. `PYTHONPATH=. python -m conformance.run` matches the original 20 traces plus three new ones: `admit_nonfinite_amount`, `admit_inflight_effect`, and `admit_unknown_input_kind`.

| Step | Status | What landed |
| --- | --- | --- |
| 1 | Closed for the listed checks | Non-finite amounts are `amount_required`. Input kinds other than `string` and `integer` are `invalid_contract` at publish and `input_schema` at create. An `effect_started` with no `effect_succeeded`, `effect_rejected`, or verified reconciliation occupies the action as `effect_unresolved:<action>`. `effect_rejected` still allows a fresh proposal. `effect_unknown.error` and `source_unavailable` detail are codes (`destination_http`, `destination_unreachable`, `destination_error`, or `Rejected.code`), not exception text. |
| 2 | Closed for binding and the connection | `bind_arguments` and `next_compiled` are shared. `Work` keeps one SQLite connection and a lock. `Session` uses a re-entrant lock. HTTP execution stays on `Work`. |
| 3 | Closed beside the hash, not inside it | New events store a JCS SHA-256 of the hashed fields on `event_side` / `Session.side_channel`. `verify_chain` checks that digest when a row has one. Old rows have none and still verify. The deciding rule id is on that side record. `cloudevent` exports CloudEvents 1.0. `at_microseconds` is export-only. `conformance/jcs_vectors.json` is the byte vector. Integer `at` inside `hash` is still **PROPOSED**. |
| 4 | HMAC only | `witness` is HMAC-SHA256 over canonical `{case_id, count, head}` with a caller-supplied key. The mac is not stored in the event. It is not a public-key DSSE signature. A transparency log is still **PROPOSED**. |
| 5 | Closed | The planner context is `objective`, `allowed_actions`, `arguments`, `required_facts`, and acceptance `{id, passed}`. Fact values are not included. A signoff is refused when that role's `after_action` effect has completed and the signer started it. A signoff before that effect still records; acceptance still fails. |

Still **PROPOSED**, and not built here: replacing admission with Cedar or OPA, replacing `Session` with a durable workflow engine, rewriting existing hashes as JCS, putting microseconds into `at`, a public-key signature, a transparency log, and an HTTPS host allowlist tighter than the installed action URL. `Session.observe(..., verified=True)` is still the host's claim. The loopback HTTP API still marks a fact verified only after a source read.

**MEASURED** with the default FULL sync, one connection: `explain.allowed` median 3.8 µs (it was 106 µs before the provenance cache). `work.complete_case` median 25.9 ms. An empty transaction on that connection was about 7 µs, against about 29 µs when each call opened a connection. A non-default WAL + `synchronous=NORMAL` trial completed one case in about 1.3 ms and was not adopted, because `NORMAL` can lose the latest commits on power loss.
