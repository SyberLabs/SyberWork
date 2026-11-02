# @syberlabs/agent: the S0 Decision Port client

One bounded decision call for TypeScript and JavaScript hosts, implementing the DecisionCall and DecisionOutcome objects of the Agent protocol `sdk.syberlabs.space/v1alpha1` (seam S0, `spec/v1/`). **Draft. Not published, and no product uses it yet.**

**Claim ceiling:** implemented and tested. Conformance-tested against the S0 fixtures, and parity-tested against the clients it would replace, using their real code on simulated provider responses. Not measured against a live provider, not deployed.

## What it is

RISE and Flyspace each hand-roll the same Jev/Kev client: a pinned model, an `x-kev-revision` check for local Kev, one deadline, no retry, and answers admitted only from the pinned provider and only from the host's menu. COMMONS has a third copy. This module is that client once.

- **The host owns the transport.** `decide(transport, call)` never sees a credential. A reader-owned OpenRouter key (RISE) and a server-held key (Flyspace) are both transports the host builds.
- **One call.** One deadline, no retry, no fallback. A timeout, cancellation or refusal ends the request.
- **Answers are proposals.** An admitted outcome is never authority, admission or evidence.

| Function | What it does |
| --- | --- |
| `validateCall(call)` | Checks a DecisionCall against S0, including the counts `spec/v1/check.py` adds. Throws `DecisionCallError`. |
| `callDecision(transport, call)` | Makes the call. Returns the provider's JSON, accepted only from the pinned provider, or throws `DecisionError` with an S0 code. Leaves menu checks to the product. |
| `admitMenu(call, value)` | Checks answers against the menu the host offered (`OUT_OF_MENU`). |
| `decide(transport, call)` | `callDecision` plus `admitMenu`, returned as an S0 DecisionOutcome. Never throws for a provider outcome. |
| `requestDigest(request)` | SHA-256 over canonical JSON, byte-identical to the Python runtime's. |

Zero dependencies. ESM with type declarations; runs in browsers, Cloudflare Workers and Node 20+.

## Provider acceptance

A served model label is accepted only from the provider that owns it:

| Pin | Accepted response | Source |
| --- | --- | --- |
| Kev, with a 40-hex revision | `x-kev-revision` equals the pin; `model` equals the pin; `provider` absent or `Kev` | RISE `providers.js` |
| TypeSafe via OpenRouter (`typesafe/...`) | `provider: "TypeSafe"`; `model` is the pin or the pin plus `-YYYYMMDD` | RISE `providers.js`, Flyspace `jev-persona` |
| TypeSafe direct (`jev-latest`) | `model` is a resolved `jev-X.Y[.Z]` | Flyspace `registryIntent` |
| rules | `model` equals the pin | S0 |

Any response with an `error` field, without an `answers` object, or over 256 KB is `INVALID_RESPONSE`.

## Parity with the clients it would replace

`parity/parity.test.mjs` runs each product's real module, unmodified, next to the consumer code that module would become (written in the test file). Both get identical simulated responses.

| Product module | Scenarios | Identical | Intended differences |
| --- | --- | --- | --- |
| RISE `src/core/decision/call.js` (a908c67) | 26 | 26 | none |
| Flyspace `src/app/api/jev-persona/route.ts` (8a21350) | 26 | 23 | 3, below |
| Flyspace `src/core/services/server/registryIntent.ts` (8a21350) | 11 | 10 | 1, below |

Endpoint, credential header and request body are also compared on every scenario, and every one matches.

Every difference is the shared client refusing something a Flyspace route accepts today; RISE already refuses all of them:

1. A Kev answer whose `provider` field names another provider (persona route).
2. A body with an `error` field next to a valid answer (persona route and registry intent).
3. A response over 256 KB that also contains a valid answer (persona route).

Whether Flyspace takes these tightenings is part of MasterMind ASK-010 (Seth, keys).

## How a product would use it

Each product keeps its own menus, answer checks and user-facing messages; only the call changes. In RISE, the parity test's `reanswer` plus the S0 provider pins replace `decision/call.js`. RISE keeps its `MESSAGES` map and reports `OUT_OF_MENU` as `INVALID_RESPONSE`, as it does now. In Flyspace, `personaViaShared` and `classifyViaShared` in `parity/parity.test.mjs` are the routes' new bodies, and every failure keeps its current status code and failure name.

How a product gets the module (a published package, a pinned git dependency, or a vendored copy checked by digest) is not decided. It is part of ASK-010.

## Run

```sh
node --test test/decision.test.js                    # 41 unit and conformance tests
node test/emit.mjs                                   # outcomes and digests, read by tests/test_agent_ts.py
RISE_DIR=../../../RISE FLYSPACE_DIR=../../../Flyspace \
  node --experimental-strip-types --import ./parity/register.mjs --test parity/parity.test.mjs
```

`tests/test_agent_ts.py` runs the Node suite from the Python suite. It validates every emitted DecisionOutcome against `spec/v1/decision-outcome.schema.json` and checks the request digests against `syberlabs.canonical`. It skips when Node 20+ is not on PATH.
