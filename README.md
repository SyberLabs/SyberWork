# SyberWork

**Contracts govern work. A case history records what was proposed, admitted, observed, approved, and executed.**

This is a running application, with a browser operator console, a versioned contract studio, a policy boundary, source-system readers, action executors, an external planner interface, human approval, completion checks, and amendment replay. It is separate from SyberLabs' existing `cross-platform` instrument panel: the panel inspects existing research systems; SyberWork executes contracts.

## Start

Python 3.11 or newer; no runtime dependencies. In the project directory:

```sh
python -m syberwork.cli --home .syberwork init
python examples/reference_system.py --database .reference/system.sqlite3
```

In a second terminal:

```sh
python -m syberwork.cli --home .syberwork serve
```

Open `http://127.0.0.1:8766/`. Save the credentials printed by `init` somewhere private. The server retains only their hashes in `.syberwork/users.json`; `init` will never overwrite them. Use a different credential for each identity. All API routes except static assets require a bearer token. The service binds only to loopback.

`examples/reference_system.py` is a separate SQLite-backed source of inventory, supplier quotes, and orders. It is a real HTTP integration with durable order writes, version headers, idempotency keys, and conditional writes. Its seeded inventory record is `P-104`; its supplier quote is `Q-7` at 250. Replace its fixed URLs and data with actual customer systems by publishing new source and action names.

## Complete the included case

1. Connect as **operator**. Create contract `purchase-order` version `1` with input `{"part_number":"P-104","quantity":2}`.
2. Refresh key `part_number` from source `inventory`, record key `P-104`. Refresh key `quote` from source `supplier`, record key `Q-7`. The returned ETags and selected values are stored as observations. Manually typed observations remain marked `verified: false` and cannot satisfy this contract.
3. Connect as **scheduler** and choose **Propose next compiled step**. `record_review` is proposed with its argument bound to the verified inventory observation. Execute that proposal. The review becomes a case event.
4. Propose the next compiled step. `issue_order` receives the observed part number, quote, price, quote ID, and quote version. The decision requests independent manager approval.
5. Connect as **manager**, select the proposal from case history, and approve it. Reconnect as **scheduler**, select the same proposal, and execute. SyberWork submits a real POST to the reference source's `/orders` with an `Idempotency-Key` and `If-Match` header. The order is stored in the *other* database. You can fetch it from `http://127.0.0.1:8999/orders/{id}` using the ID in the effect response.
6. Reconnect as **manager** and sign off. The case becomes complete only after the order effect and the later signoff. **Verify history** checks the per-case hash chain.

The **Contract & connector studio** accepts immutable contract and policy JSON documents. The compiler produces a dependency-ordered known path when one is not authored, plus inspectable action gates, acceptance queries, and an input form schema. Publish a higher version, then use **Amendment replay** to compare decisions against the recorded history. Replay makes no model calls, destination writes, or live source reads.

## External planners

To run a planner, set `SYBERWORK_PLANNER_URL` to an HTTPS or loopback endpoint and optionally set `SYBERWORK_PLANNER_TOKEN`. Connect with the `planner` credential and click **Ask configured planner**, or POST to `/api/cases/{id}/suggest`. The endpoint receives JSON containing `objective`, `allowed_actions`, `contract`, `events`, and `acceptance`. It must return JSON shaped like:

```json
{"action":"record_review","args":{"part_number":"P-104"}}
```

This output only creates a proposal. Admission rechecks the bound observation, global policy, roles, prior effects, and required approval. The planner receives no executor credential. The `scheduler` credential generates the next proposed compiled step directly from fact bindings; neither proposer can bypass the case API.

## API and data ownership

- `POST /api/contracts`, `/api/policies`, `/api/actions/{name}`, `/api/sources/{name}`: administrator publication.
- `POST /api/cases`: create a case pinned to a contract version.
- `POST /api/cases/{id}/refresh`: fetch and record a versioned fact from a configured source.
- `POST /api/cases/{id}/facts`: record a manually asserted fact, tagged as unverified.
- `POST /api/cases/{id}/proposals`, `/compiled`, `/suggest`: human, compiled, or model proposal.
- `POST /api/cases/{id}/approve`, `/commit`, `/signoff`, `/reconcile`: decision operations.
- `GET /api/cases/{id}`, `/verify`; `POST /api/cases/{id}/replay`: inspection and counterfactual comparison.

The case SQLite database is authoritative for *SyberWork decisions and observed responses*. The source systems remain authoritative for inventory, quotes, and purchase orders. An observation stores its source, record version, observed value, observation time, and whether it came from a configured connector. An action rechecks admission when committed. The reference destination checks the quote version at write time. A failed or timed-out write remains `effect_unknown` until a manager checks the destination and records reconciliation with an evidence reference. Automatic retries cannot create a second order.

## Verification

```sh
PYTHONPATH=. python -m unittest discover -s tests -v
```

The tests cover the full case, independent approval, policy precedence, model and compiled proposals, amendment replay, tamper detection, concurrent-action claims, a real local HTTP source reader, and an HTTP effect with its idempotency key. A passing test demonstrates those paths in this implementation. It does not establish that a new customer's source systems or policies have been integrated correctly.

## Deployment boundary

The included service is local and single-installation. Its role-bearing tokens are provisioned locally. It does not provide SSO, multi-tenant isolation, a distributed transaction with an external API, a customer-specific semantic layer, or an automatic translation from arbitrary prose into a correct contract. The interface requires human review of published contracts. Preserve the `.syberwork` database and credentials together when moving the installation. A hash chain detects accidental editing against its local history; it does not prevent a database owner from replacing and rehashing the whole chain.
