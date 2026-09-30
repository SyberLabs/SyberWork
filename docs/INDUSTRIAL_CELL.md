# Industrial Cell v0.1

A cell is one organization and one database. Tenant isolation is that boundary. Case rows do not carry a tenant id.

Admission still lives in `syberlabs` and does not import the database. `Work` calls `ContractStore`, `PolicyStore`, `ActionRegistry`, `SourceRegistry`, `CaseStore`, `EventStore`, and `ReservationStore`. `open_store` selects SQLite for a file path and PostgreSQL for a `postgresql://` URL. Migrations `0001` through `0004` are applied on open. A database created before those migrations keeps its event hashes; `at_json` is added when it is missing. Migration `0004` adds Builder coordination tables. They are not case events. A snapshot from before that migration restores when it contains none of those table files. A snapshot missing an older table, or only some of the Builder files, is refused and the restore rolls back.

## What this cell does

- Persist the same case history in SQLite or PostgreSQL. A test runs one local case on both and compares event kinds and decision reasons.
- Claim an effect, return, and let `syberwork worker` settle it after a restart (`SYBERWORK_EFFECTS=worker`). The default remains inline, which is what the existing suite calls.
- Treat an expired execution lease as `effect_unknown` / `worker_interrupted`. Reconciliation is still how an ambiguous destination call becomes success.
- Record principals (`human`, `service`, `agent`) with roles and an optional delegation. The token is stored as a hash. Loopback bearer tokens in `users.json` still authenticate the local appliance.
- Append a semantic trace (`admission`, `effect_claim`, `effect_invocation`, `effect_settlement`, `reconciliation`) when a trace log is attached.
- Export and restore a logical snapshot, and refuse a backup whose case chain does not verify.
- Build a container with `docker compose`. The image and Compose both use `/home/syber/cell`, owned by uid 10001. `.dockerignore` keeps `.syberwork`, `.env`, databases, and `.git` out of the build context. The worker is a second process on the same database. The server binds beyond loopback only when `SYBERWORK_BIND` is that address. `sh scripts/compose_cell.sh` initializes a cell, queues one local effect, lets the worker settle it, restarts both processes, and restores a backup into a fresh database.
- Migrations take a lock, so a server and a worker can open the same database during an upgrade. A restore that fails chain verification rolls the import back.

## What this cell does not do

- SSO, SAML, or SCIM.
- A shared multi-tenant database.
- KMS or HSM for the witness key. The witness is still a separate process with the seed in its memory.
- Fleet control, billing, or connector sandboxing.
- A change to admission behavior. The golden traces stay on the inline SQLite path.

`docker compose up` does not initialize credentials. Run `syberwork init` against `SYBERWORK_DATABASE` before serving a new cell.
