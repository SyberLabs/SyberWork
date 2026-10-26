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

A 404, transport failure, nonterminal record, or mismatched response records `reconciliation_checked` as `pending` or `unverified`; none licenses acceptance or a new effect of the same action. The original proposal cannot be retried after an effect claim. If the destination has no authoritative idempotency lookup, unknown effects remain unresolved until an appropriate adapter is built; a human statement cannot override the uncertainty.

An interrupted worker may leave `effect_started` without `effect_unknown`. This is also unresolved and can be checked through the destination status lookup. A terminal outcome racing that lookup is rechecked under the case write lock; it cannot append a second terminal success. Economic actions retain their shared budget reservation while unresolved and cannot be cancelled until the outcome is established.

An integration may separately declare `no_write_statuses`, limited to explicit precondition rejection codes 409, 412, or 428. This declaration means **the destination guarantees that code was returned before any write**. Such a response records `effect_rejected` and permits a fresh proposal with fresh source observations and approval. A plain 404 from a later status lookup never establishes that no write occurred, because the destination may be eventually consistent. Do not declare a no-write status unless the destination contract makes that guarantee.

For the included reference integration, `POST /orders` enforces `If-Match` and idempotency; `GET /orders/by-key/{key}` returns the persisted request digest and external ID. Existing orders created before this schema included a request digest cannot be verified through this lookup. Keep those cases unresolved and migrate their destination evidence under a separate, explicitly designed process; do not manufacture a digest.

This mechanism relies on the authenticity and correctness of the configured destination, its credential, and its idempotency implementation. It is not a distributed transaction or protection against a compromised destination or database administrator.
