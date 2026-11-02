# @syberlabs/agent: the S0 Decision Port client

One bounded decision call for TypeScript and JavaScript hosts, implementing the DecisionCall and DecisionOutcome objects of the Agent protocol `sdk.syberlabs.space/v1alpha1` (seam S0, `spec/v1/`). **Draft. Not published, and no product uses it.**

**Claim ceiling:** implemented and tested. It is conformance-tested against the S0 fixtures, and parity-tested against the clients it would replace by running their real code on simulated provider responses. It is not measured against a live provider and not deployed.

**Merge is gated.** This package exists only if MasterMind ASK-010 (Seth, keys) resolves to building it. If that ask is answered no, or lapses on 2026-11-09, this directory is deleted.

## Why it exists

S0 needs one implementation of the Decision Port so that decisions can be recorded as DecisionOutcomes with a request digest and the model build that answered. Removing duplicated code is a consequence, not the reason. RISE and Flyspace each implement the same contract today: a pinned model, an `x-kev-revision` check for local Kev, one deadline, no retry, and answers accepted only from the pinned provider. COMMONS has a third copy.

- **The host owns the transport.** The client never sees a credential. A reader-owned OpenRouter key (RISE) and a server-held key (Flyspace) are both transports the host builds.
- **One call.** One deadline, no retry, no fallback. A timeout, a cancellation or a refusal ends the request.
- **Answers are proposals.** An admitted outcome is never authority, admission or evidence.

| Function | What it does |
| --- | --- |
| `validateCall(call)` | Checks a DecisionCall against S0, including the counts `spec/v1/check.py` adds and that the request holds only JSON values. Throws `DecisionCallError`. |
| `callDecision(transport, call, { signal, compat })` | Makes the call. Returns the provider's JSON, accepted only from the pinned provider, or throws `DecisionError` with an S0 `code` and a `phase` (`request` or `response`). Does not check the menu, so each product keeps its own answer checks. |
| `admitMenu(call, value)` | Checks S0 answer rules: at least one answer, each one to an asked question of the right type, each choice offered. Stricter than today's products, which ignore answers to questions they did not ask. Used by `decide`, not by product consumers. |
| `decide(transport, call)` | `callDecision` plus `admitMenu`, always strict, returned as an S0 DecisionOutcome including `served_model`. Never throws for a provider outcome. Needs Web Crypto, which browsers expose only in secure contexts. |
| `requestDigest(request)` | SHA-256 over canonical JSON: keys sorted by code point, compact, non-ASCII preserved. Matches `syberlabs.canonical` byte for byte except for integral floats, which Python writes as `1.0` and JavaScript as `1`. |

Zero dependencies. ESM with type declarations; runs in browsers, Cloudflare Workers and Node 20+.

## Provider acceptance

A served model label is accepted only from the provider that owns it:

| Pin | Accepted response | Source |
| --- | --- | --- |
| Kev, with a 40-hex revision | `x-kev-revision` equals the pin; `model` equals the pin; `provider` absent or `Kev` | RISE `providers.js` |
| TypeSafe via OpenRouter (`typesafe/...`) | `provider: "TypeSafe"`; `model` is the pin, or the pin plus `-YYYYMMDD` | RISE `providers.js`, Flyspace `jev-persona` |
| TypeSafe direct (`jev-latest`) | `model` is a resolved `jev-X.Y[.Z]` | Flyspace `registryIntent` |
| rules | `model` equals the pin | S0 |

By default, a response is `INVALID_RESPONSE` if it has an `error` field, has no `answers` object, or is over 262,144 UTF-16 code units. That is the size cap RISE applies; it is not a byte count.

## Compat: keeping Flyspace's current behavior

Flyspace is more lenient than RISE and S0 in three ways. Phase 1 allows no change a product's users can see, so Flyspace's consumers pass `compat` to keep all three:

| `compat` | Flyspace today | Strict default |
| --- | --- | --- |
| `kevProviderLabel: 'any'` | accepts a Kev answer whatever its `provider` field says (persona route) | refuses another provider's label |
| `errorField: 'ignore'` | accepts an `error` field next to valid answers (both routes) | refuses it |
| `maxResponseChars: Infinity` | reads a response of any size (both routes) | refuses over the cap |

Whether Flyspace should drop this leniency is MasterMind ASK-011, a separate question from Phase 1. The test `STRICT_DIFFERENCES` in `parity/parity.test.mjs` pins exactly what dropping it would change.

## Parity with the clients it would replace

`parity/parity.test.mjs` runs each product's real code, unmodified, next to the consumer code it would become, on identical simulated responses. Every comparison must be equal: same value, or same error code and the same reader-facing message.

| Comparison | Cases | Equal |
| --- | --- | --- |
| RISE `decision/call.js`, against its replacement `parity/stubs/rise-call-shared.mjs` | 30 (29 scenarios plus no connection) | 30 |
| RISE `decideRoute` (`route.js`), re-imported to run on the shared-backed `call.js` | 29 | 29 |
| Flyspace `jev-persona/route.ts`, against `personaViaShared` | 30 (29 scenarios plus a client disconnect) | 30 |
| Flyspace `registryIntent.ts`, against `classifyViaShared` | 19 (whitespace and length rules, no key, an abort before and during the body read, an extra answer, an oversized body) | 19 |

In every case the endpoint, the credential header and the request body are compared too, and all of them match. That is 108 comparisons. The test file also has 3 `STRICT_DIFFERENCES` checks, and with the 5 parent tests the node runner reports 116 tests.

**What parity is not.** It shows each consumer behaves exactly as the product does today. It does not show that the products pass the S0 fixtures. RISE's and Flyspace's own code never runs against `spec/v1/fixtures/`; only this client does.

**Mutation check.** Each of these mutations was introduced into `src/decision.js` and made at least one unit or parity test fail:

- removing the declared-length check
- removing the error-field check
- removing the Kev label check
- removing the Kev header attestation
- giving a parse failure the wrong phase
- admitting unasked answers (caught by unit tests only, because product consumers do not use `admitMenu`)

## How a product would use it

Each product keeps its own menus, answer checks and user-facing messages. Only the call changes.

- **RISE:** `call.js` becomes `parity/stubs/rise-call-shared.mjs`. It keeps RISE's `DecisionError` class, so readers keep RISE's messages.
- **Flyspace:** `personaViaShared` and `classifyViaShared` in `parity/parity.test.mjs` are the routes' new upstream calls. The request envelope, the status codes and the failure names stay the same.

`registryIntent` is not named in ASK-010, which covers RISE `src/core/decision/` and Flyspace `jev-persona`. Moving it is a further question for Seth.

How a product gets this module is not decided. It could be a published package, a pinned git dependency, or a vendored copy checked by digest. That is part of ASK-010.

## Run

```sh
npm test                                   # 50 unit and conformance tests
npm run typecheck                          # strict tsc over src/decision.d.ts (declarations only)
RISE_DIR=../../../RISE FLYSPACE_DIR=../../../Flyspace npm run parity
```

In PowerShell:

```powershell
$env:RISE_DIR = '..\..\..\RISE'; $env:FLYSPACE_DIR = '..\..\..\Flyspace'; npm run parity
```

Without `RISE_DIR` and `FLYSPACE_DIR`, the parity tests skip.

`tests/test_agent_ts.py` runs the Node suite from the Python suite. It validates every emitted DecisionOutcome against `spec/v1/decision-outcome.schema.json`, and checks request digests (fixtures, floats, astral-plane keys, nesting) against `syberlabs.canonical`. It skips with the reason G8 allows when node is not on PATH.
