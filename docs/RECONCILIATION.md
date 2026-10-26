# Destination-verified effect outcomes

An HTTP action can install a same-origin `status_url` with exactly one `{key}` placeholder in its path. The origin is validated before publication, and redirects are refused. The destination must return a JSON object for a committed effect:

```json
{
  "state": "committed",
  "idempotency_key": "the-original-proposal-id",
  "request_digest": "sha256-of-the-exact-canonical-request-arguments",
  "external_id": "the-durable-destination-record-id"
}
```

SyberWork requests that URL with the original idempotency key and the action's configured authorization credential. It checks the state, key, request digest, and nonempty external ID. Only a matching response adds a `reconciled` event with a proof containing the response digest and external ID. Caller-provided `success` or `evidence` fields are rejected. The manager authorizes the check; the manager does not decide its result. An already persisted text-only reconciliation remains in history but does not satisfy acceptance or release the unknown effect.

Three further rules apply to the body. Each one fails closed to `unverified`, which, like any other unverified check, leaves the effect unresolved:

- **Strict JSON.** The body is parsed strictly. Some bodies are JSON by grammar, but their meaning depends on the parser that reads them (RFC 8259 sections 4, 6 and 8.2). Such a body is ambiguous: it records `unverified` with reason `destination_record_ambiguous` and is never read in Python's lenient way. A body is ambiguous when, anywhere in it:
  - an object names the same field twice, even if every copy of the field has the same value. The body is not read with last-wins semantics;
  - a number overflows a double, such as `1e400`;
  - a field name or string holds an unpaired UTF-16 surrogate, either as an escape such as `"\udc80"` or as raw surrogate bytes.

  The `NaN`, `Infinity` and `-Infinity` literals are not JSON, although Python's parser accepts them. A body that uses them records `pending` with reason `status_unavailable`. So does a body nested deeper than 64 levels of objects and arrays, counting the top level as 1, and so does a body that is not JSON, empty, truncated or larger than 64 KB. A paired surrogate, the largest finite double and `-0.0` are accepted. Integer size, float precision and the body's character encoding are not checked.
- **Simulation labels.** A record can match on state, key, digest and external ID and still label itself as not live. Such a record records `unverified` with reason `destination_record_simulated`. A record counts as labeled when any of these top-level fields is present:
  - `simulated`, `simulation`, `sandbox`, `dry_run`, `test`, `testnet` or `preview`, with any value other than JSON `false`;
  - `livemode`, with any value other than JSON `true`;
  - `mode`, `environment` or `env`, with any value other than exactly `"live"`, `"production"` or `"mainnet"`. The match is case-sensitive, so `"Production"` is refused.

  These fields are optional, and a record without them is judged on state, key, digest and external ID alone. Labels nested inside other objects, and label names not on this list, are not recognized. A destination that marks its test records some other way must keep those records off the installed `status_url`. A record that fails the match for any other reason, including a simulation `state` or a record wrapped in a simulation envelope, records `destination_record_mismatch`.
- **Rail.** This rule applies to `economic_http` actions. A settlement record may name its rail. If it does, the value must equal the installed action's `rail` exactly, or the record records `unverified` with reason `destination_record_mismatch`. This also applies to `null`, to an empty string and to a different case. A record that omits `rail` is still judged on the other receipt fields, and its proof states the installed rail. In that case the proof's `rail` is the installed configuration, not a statement the destination made.

The commit path for `economic_http` applies the same three rules to the settlement receipt. A receipt that is not strict JSON, carries a simulation label or names another rail is treated like any other non-matching receipt: the effect is `unknown` and its reservation is kept.

A 404, transport failure, nonterminal record, or mismatched response records `reconciliation_checked` as `pending` or `unverified`; none licenses acceptance or a new effect of the same action. The original proposal cannot be retried after an effect claim. If the destination has no authoritative idempotency lookup, unknown effects remain unresolved until an appropriate adapter is built; a human statement cannot override the uncertainty.

An interrupted worker may leave `effect_started` without `effect_unknown`. This is also unresolved and can be checked through the destination status lookup. A terminal outcome racing that lookup is rechecked under the case write lock; it cannot append a second terminal success. Economic actions retain their shared budget reservation while unresolved and cannot be cancelled until the outcome is established.

An integration may separately declare `no_write_statuses`, limited to explicit precondition rejection codes 409, 412, or 428. This declaration means **the destination guarantees that code was returned before any write**. Such a response records `effect_rejected` and permits a fresh proposal with fresh source observations and approval. A plain 404 from a later status lookup never establishes that no write occurred, because the destination may be eventually consistent. Do not declare a no-write status unless the destination contract makes that guarantee.

For the included reference integration, `POST /orders` enforces `If-Match` and idempotency; `GET /orders/by-key/{key}` returns the persisted request digest and external ID. Existing orders created before this schema included a request digest cannot be verified through this lookup. Keep those cases unresolved and migrate their destination evidence under a separate, explicitly designed process; do not manufacture a digest.

This mechanism relies on the authenticity and correctness of the configured destination, its credential, and its idempotency implementation. It is not a distributed transaction or protection against a compromised destination or database administrator.
