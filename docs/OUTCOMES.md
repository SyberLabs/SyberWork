# Outcomes: the intervention effect

Status: **implemented and tested** against a loopback metric source (`examples/metric_source.py`, `tests/test_outcome.py`). No real metric system has been connected, and nothing here says one works.

An admitted change is usually settled by its destination: the branch moved, the pull request exists, the command reported an id. An **intervention** is a publishing effect whose destination is a metric. The claim is settled by whether the metric moved as predeclared within a window after the change, and the kernel settles it with the machinery it already has for an effect whose outcome is unknown: a durable claim, `syberlabs recover`, and `Session.reconcile`. Nothing in `session.py`, `admission.py` or the protocol schemas changed for it.

## 1. What an intervention is

In the kernel's own vocabulary:

- **Claim.** `syberlabs publish measure_checkout` proposes `{commit: <accepted commit>}` under the same rules as a push (`requires_effect`, the verified `accepted` fact, the argument binding) and, once admitted, records `effect_started`.
- **Baseline.** The executor reads the metric once, right then, and records the reading as a verified `observed` event with key `intervention` at the sequence number after the claim. Then it leaves the claim open on purpose: the outcome cannot be known yet, so `commit` stores `effect_unknown` with error `window_open`, and `publish` returns `unknown`. That is the designed result, not a transport failure.
- **Settlement.** `syberlabs recover` asks the executor's `status`. Inside the window it answers `absent`, which the kernel records as pending (`absent_within_settle_window`). After the window it reads the metric again, records that reading as a second verified `observed` event, and answers `applied` when the metric moved by at least `min_delta` in the predeclared direction, or `not_applied` when it did not. `Session.reconcile` turns `applied` into a verified `reconciled` event and `not_applied` into `effect_rejected` with status `not_applied`, exactly as it does for a push.

```
syberlabs publish measure_checkout
  proposed {commit}            admitted like push_branch
  effect_started               the claim; idempotency key = proposal id
  observed intervention        phase baseline: value, version, observed_at, read_at    (seq = claim + 1)
  effect_unknown               error window_open                                        (publish returns unknown)
syberlabs recover              any time
  window still open            -> reconciliation_checked, absent_within_settle_window   (pending)
  metric cannot be read        -> reconciliation_checked, status_unavailable            (pending; retried)
  moved as predeclared         -> observed intervention, phase settlement, outcome moved          -> reconciled (verified)
  moved less than min_delta    -> observed intervention, phase settlement, outcome unchanged      -> effect_rejected not_applied
  moved the other way          -> observed intervention, phase settlement, outcome moved_against  -> effect_rejected not_applied
```

## 2. Declaring one

The destination and the expectation live in `.syberlabs/actions.json`, which is admin-owned and immutable by name. The expectation therefore cannot be edited after the fact, and a proposal cannot carry one.

```json
"measure_checkout": {
  "kind": "local",
  "effect": "intervention",
  "title": "Checkout conversion must rise by 0.5 points within a week",
  "metric_url": "http://127.0.0.1:8998/metrics/{key}",
  "metric_key": "checkout_conversion_7d",
  "window_seconds": 604800,
  "expectation": {"direction": "up", "min_delta": 0.5},
  "max_age_seconds": 3600,
  "auth_env": "METRICS_TOKEN",
  "timeout_seconds": 10
}
```

| Field | Rule |
| --- | --- |
| `kind`, `effect` | `local`, `intervention`. |
| `metric_url` | Exactly one `{key}`, in the path; no other braces, no query, no fragment. Loopback HTTP with an explicit port, or HTTPS to a public host (`targets.trusted_origin`; an HTTPS name is resolved and checked again at request time by `guard_request`). Redirects are refused. |
| `metric_key` | 1 to 200 printable characters, no slash, no control characters. Substituted into the URL percent-encoded. |
| `window_seconds` | Integer, 1 to 2,592,000 (30 days). The executor exposes it to the kernel as `settle_seconds`, so a `settle_seconds` field is refused as unknown. |
| `expectation` | Exactly `{direction, min_delta}`. `direction` is `up` or `down`; `min_delta` is a finite number greater than zero, so "unchanged" can never count as "moved". |
| `max_age_seconds` | Optional integer, 1 to 2,592,000. A reading whose `observed_at` is older than this is not used: before a baseline the claim is refused, and at settlement the claim stays pending. Without it, a source that reports no `observed_at` is accepted as is. |
| `auth_env` | Optional name of an environment variable (`^[A-Z_][A-Z0-9_]{0,63}$`) holding a bearer token. The value never enters the record. |
| `timeout_seconds` | Optional, 1 to 3600. A read is capped at 30 seconds whatever the value. |
| `title` | Optional string. |

Any other field is refused with `invalid_action`.

The contract binds the intervention exactly like a push: it requires the deploying effect, the verified `accepted` fact, and the accepted commit. Put it after the effect that makes the change live, so the baseline is read once the change is in place.

```json
// contract actions (a new contract version)
"measure_checkout": {"requires_effect": "push_branch",
                     "required_facts": [{"key": "accepted", "source": "git", "verified": true}],
                     "arguments": {"commit": "fact:accepted.commit"}}
// .syberlabs/policy.json
"measure_checkout": {"roles": ["developer"]}
```

## 3. Running one

`syberlabs publish measure_checkout` returns one of:

| Receipt | Meaning |
| --- | --- |
| `unknown`, reason `effect_unknown` | The baseline was read and recorded and the claim is open until the window ends. This is the designed result. Run `syberlabs recover` once `window_seconds` have passed. |
| `rejected`, reason `metric_unavailable`, `metric_shape`, `metric_too_large` or `missing_credential` (stored status 409) | No baseline could be read. Nothing was written anywhere; the action is free to try again. |
| `rejected`, reason `source_unversioned` or `baseline_stale` (stored status 428) | The source returned no `ETag` or `X-Record-Version`, or a reading older than `max_age_seconds`. Nothing was written. |
| `denied`, reason `required_prior_effect_missing` | The deploying effect has not happened yet. |
| `denied`, reason `effect_unresolved:measure_checkout` | A claim of this action is still open. Recover it first. |
| `denied`, reason `action_already_completed` | This thread's intervention already settled `applied`. |

`syberlabs recover` returns, for the open claim:

| Receipt | Recorded | Meaning |
| --- | --- | --- |
| `pending`, `absent_within_settle_window` | `reconciliation_checked` | The window has not elapsed. The action stays occupied. |
| `pending`, `status_unavailable` | `reconciliation_checked` | The metric could not be read, was unversioned or malformed, or its reading is older than `max_age_seconds`. Retried on the next `recover`; never timed out. |
| `succeeded`, external id = the settlement version | `observed` (settlement, outcome `moved`), then `reconciled` | The metric moved by at least `min_delta` in the predeclared direction. |
| `not_applied`, `destination_state_unchanged` | `observed` (settlement, outcome `unchanged` or `moved_against`), then `effect_rejected` | It did not. The action is free; a new claim reads a new baseline. |
| `not_applied`, `destination_state_unchanged`, with no settlement observation | `effect_rejected` | The claim has no baseline (the process died between the claim and the baseline) and the window has passed. The claim is void. |

While a claim is open, `syberlabs status` lists `syberlabs recover` as the next step and no publishing step. After `applied` it no longer lists `syberlabs publish measure_checkout`; after `not_applied` it lists it again.

## 4. What is recorded

Three events carry the intervention, and the kernel's own events wrap them.

**The baseline**: an `observed` event with key `intervention`, source `metric:<metric_key>`, version = the source's version, actor `syberlabs.host`, `verified: true`, at the sequence number right after `effect_started`.

```json
{"proposal_id": "...", "phase": "baseline", "metric": "checkout_conversion_7d", "commit": "<accepted commit>",
 "value": 2.0, "version": "checkout_conversion_7d-v1", "observed_at": 1760700000.0, "read_at": 1760700012.345678}
```

**The open claim**: `effect_unknown` with `error: "window_open"`.

**The settlement**: an `observed` event with the same key, source, actor and `verified`, written at most once per claim, immediately before the kernel's settlement event.

```json
{"proposal_id": "...", "phase": "settlement", "metric": "checkout_conversion_7d", "commit": "<accepted commit>",
 "baseline": 2.0, "baseline_version": "checkout_conversion_7d-v1",
 "value": 2.6, "version": "checkout_conversion_7d-v2", "observed_at": 1761304800.0, "read_at": 1761304812.0,
 "delta": 0.6, "direction": "up", "min_delta": 0.5, "window_seconds": 604800, "outcome": "moved"}
```

`outcome` is `moved` (a gain of at least `min_delta` in the predeclared direction), `unchanged` (a gain below `min_delta`, zero included) or `moved_against` (a move the other way). `moved` settles `applied`; the other two settle `not_applied`. A move against the expectation is a measured, final answer for that window, and in this kernel the only alternative, an `unknown` from `status`, is a pending state that would keep the action occupied and the thread unforgettable with no way out. The settlement reading keeps the cases apart.

The kernel's `reconciled` proof carries `external_id` = the settlement version and `response_digest` = the digest of the executor's proof `{external_id, outcome, baseline, baseline_version, value, delta}`, so the verdict is bound to the numbers in the chain.

`status` reads the baseline only from the event at `claim.seq + 1`, and only when that event is a verified `intervention` observation by the host for this proposal, in phase `baseline`, with a numeric value and a version. The baseline is written inside `commit`, under the session lock, as the next append after the claim, so nothing else can hold that position. An observation appended later with the same shape is ignored; a proposal argument other than `commit` is never read; `reconcile` takes no caller-supplied outcome. Given the same chain, the same clock and the same source response, `status` returns the same answer, in the same process or after reopening the kit. Once settled, the verdict is a hash-chained event; `reconcile` refuses to run again, and a later move of the metric changes nothing.

## 5. The metric source contract

`GET` the `metric_url` with `{key}` substituted, with `Accept: application/json` and, when `auth_env` is set, `Authorization: Bearer <token>`. The source answers:

- `200` with a JSON object `{"value": <finite number>, "observed_at": <epoch seconds, or null>}`. Other members are ignored.
- An `ETag` or `X-Record-Version` header that changes whenever the value does. It is the reading's version and becomes the `external_id` of a verified settlement.
- At most 65,536 bytes. No redirects.

Loopback HTTP needs an explicit port; anything else needs HTTPS to a public host. `python examples/metric_source.py --port 8998` runs a source on loopback, seeded with `checkout_conversion_7d = 2.0`; `POST` the same path with `{"value": ..., "observed_at": ...}` to move it. It is an example, not a product.

## 6. Limits

- **Attribution is not causation.** `applied` records that the predeclared movement was observed in the window after the change. It does not say the change caused it. There is no control group, no seasonality correction and no significance test; a window is a minimum wait, not an experiment design.
- **Measured, not proved.** The source is trusted for its own numbers, as the reference system is trusted for its inventory. The kit checks the shape, the version and the age of a reading, not its truth.
- **One intervention per action name per thread.** After `applied`, `effect.not_completed` denies another proposal of that action. After `not_applied`, a fresh proposal is admitted and reads a new baseline.
- **A claim without a baseline is void after the window.** If the process dies between the claim and the baseline, `recover` reports pending until the window ends and then `not_applied`. Nothing is measured.
- **`unknown` at settlement is retried, not timed out.** A source that stays unreadable keeps the claim open, the action occupied and the thread unforgettable (`retention_unresolved`), because nobody may assert the outcome.
- **One process per journal where there is no file lock.** The `claim.seq + 1` rule relies on the session lock holding across `apply`. The journal's cross-process lock is a no-op where `fcntl` is unavailable, as `syberlabs/journal.py` already states.
- **Wording.** While any claim is open, `syberlabs status` says "an acceptance was interrupted; its outcome is unknown until recovered". For an intervention that is the designed state; the next step it prints, `syberlabs recover`, is right.

## 7. What it is not

Not a Place of Interest, not a rule engine, not a learning station, not a metrics platform. It records two readings and a verdict in the thread's chain under the kernel's existing rules. Whatever reads those records later is a separate consumer.
