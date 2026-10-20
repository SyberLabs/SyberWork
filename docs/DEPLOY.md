# Deploying SyberWork: the factory's first deployment

Status: **implemented and tested** on one machine with the files already in this repository. The stack below is what CI's `compose` job boots (`scripts/compose_cell.sh`). It has not been run as a standing deployment anywhere, and nothing here says it has.

The first deployment of the factory is the SyberWork service, its worker and its database on one host. It is not a thread runner, it is not public, and it holds no secrets for anyone else. What it adds over a developer's `.syberwork` directory is a durable cell: PostgreSQL instead of SQLite, effects settled by a separate process, and a metric source the Build Thread's `intervention` effect can settle against (`GET /metrics/{key}`, below).

## 1. What runs

`docker-compose.yml` defines three services on one Compose network and two named volumes:

| Service | Image | Role |
| --- | --- | --- |
| `db` | `postgres:16` | The cell's database `syberwork`, user `syber`, on volume `syberwork-db`. Health-checked with `pg_isready`. |
| `syberwork` | built from `Dockerfile` | The operator service, `syberwork serve --port 8766`, published on host port 8766. Binds `0.0.0.0` inside the container because `SYBERWORK_BIND` says so; the container port is reachable only through the published port. |
| `worker` | the same image | `syberwork worker`: leases queued effect obligations and settles them. Needed because `SYBERWORK_EFFECTS=worker` makes the service claim effects and return. |

Both processes share `SYBERWORK_HOME=/home/syber/cell` on volume `syberwork-home`, which holds `users.json` (credential hashes), `cell.json` and `traces.jsonl`. The environment (`SYBERWORK_DATABASE`, `SYBERWORK_ORGANIZATION=example`, `SYBERWORK_TRACE=1`) is the one in the file; this document changes nothing in it.

## 2. Running it on one host

The host needs Docker with the Compose plugin and a checkout of this repository. The commands are the ones `scripts/compose_cell.sh` runs, in order, without the smoke steps.

```sh
docker compose build
docker compose up -d db --wait
docker compose run --rm -T syberwork syberwork init
docker compose up -d syberwork worker
curl -fsS http://127.0.0.1:8766/
```

`syberwork init` runs once per cell. It prints eleven credentials (`admin`, `operator`, `planner`, `scheduler`, `manager`, `logistics`, `procurement`, `inventory`, `supplier`, `searcher`, `evaluator`), stores only their SHA-256 hashes in `users.json`, installs the example contract, policy, actions and sources, and binds the database to `SYBERWORK_ORGANIZATION`. It refuses to run again (`already initialized; credentials are never overwritten`). The printed tokens exist nowhere else: whoever runs `init` must save them at that moment.

To check the stack is up: `curl -fsS http://127.0.0.1:8766/` returns the console page, and an authenticated read works:

```sh
curl -fsS -H "Authorization: Bearer <operator token>" http://127.0.0.1:8766/api/me
curl -fsS -i -H "Authorization: Bearer <operator token>" http://127.0.0.1:8766/metrics/cases_total
```

To stop it and keep the data: `docker compose down`. To stop it and delete the database and the credentials: `docker compose down -v` (what the CI script does at the end). A backup is `docker compose run --rm -T syberwork syberwork backup <directory>` against the running `db`; `docs/INDUSTRIAL_CELL.md` describes restore.

On Windows the same commands run in Git Bash or PowerShell with Docker Desktop; nothing in them is shell-specific.

## 3. The service as a metric source

`GET /metrics/{key}` on the service meets the metric source contract in [docs/OUTCOMES.md](OUTCOMES.md) section 5:

```
GET /metrics/cases_total
Authorization: Bearer <any credential from init>

200
ETag: "6f1c...e2"           (the version, quoted)
X-Record-Version: 6f1c...e2
{"key":"cases_total","value":3,"observed_at":1760800000.123456,"observed_at_utc":"2026-10-18T16:26:40.123Z","version":"6f1c...e2"}
```

| Key | What it counts |
| --- | --- |
| `cases_open` | Cases neither cancelled nor complete, by the rule `inspect` applies. |
| `cases_total` | All cases in the cell. |
| `proposals_admitted_total` | Recorded admission decisions with status `allowed`. |
| `proposals_denied_total` | Recorded admission decisions with status `denied` (`needs_approval` is neither). |
| `effects_pending` | Claimed effects not yet settled: queued, running, or `effect_unknown` awaiting reconciliation. |
| `effects_settled_applied_total` | Claimed effects with `effect_succeeded` or a verified `reconciled`. |
| `effects_settled_not_applied_total` | Claimed effects with `effect_rejected` (a no-write refusal or `not_applied`). |
| `queue_depth` | Worker obligations queued or leased. Always 0 when effects settle inline. |
| `uptime_seconds` | Seconds since this server process started. |

Every claimed effect is in exactly one of the three effect counters. `observed_at` is the epoch time of the read, the form the effect's `max_age_seconds` rule understands; the version is a digest of key, value and `observed_at`, so a repeated read of an unchanged value is still a fresh, distinct reading. An unknown key is `404 {"error": "unknown_metric"}`. The route needs a bearer token like every other read on the service; there is no anonymous metrics endpoint.

A Build Thread points an intervention at it from `.syberlabs/actions.json`:

```json
"measure_cases": {
  "kind": "local",
  "effect": "intervention",
  "metric_url": "http://127.0.0.1:8766/metrics/{key}",
  "metric_key": "cases_total",
  "window_seconds": 3600,
  "expectation": {"direction": "up", "min_delta": 1},
  "max_age_seconds": 600,
  "auth_env": "SYBERWORK_METRIC_TOKEN"
}
```

with `SYBERWORK_METRIC_TOKEN` set in the environment of the process that runs `syberlabs publish measure_cases` and `syberlabs recover` to one of the credentials `init` printed. The thread runs on the same host, because the effect accepts loopback HTTP with an explicit port and refuses plain HTTP anywhere else. `tests/test_server.py` runs this exact shape against the service on a free port with SQLite: baseline, a case created through `POST /api/cases`, settlement `applied` with the settlement reading's version as the external id.

## 4. What is not there yet

- **No thread runner service.** Build Threads run from a checkout with `python -m syberlabs`; nothing in the stack starts, schedules or recovers them. That is a later packet.
- **No public exposure and no TLS.** Docker publishes port 8766 on the host's interfaces as `docker-compose.yml` is written; whether the host firewall closes it, or the mapping is narrowed to `127.0.0.1:8766:8766`, is the operator's decision on the host. The service speaks plain HTTP; the intervention effect will only read it over loopback.
- **No secrets broker.** The database password is the literal in `docker-compose.yml`, the credentials are what `init` printed, and the thread's metric token lives in an environment variable. Rotation is `down -v` and a new `init`.
- **No principal management from the command line.** `Work.register_principal` exists for cell principals with a token hash and a delegation; the CLI exposes only the `init` credentials.
- **Single host, single worker.** One Compose project, one database, one worker process. `syberwork backup` is the only redundancy.
- **Counters are read on request.** `cases_open` reads every case's history on each call; fine for a cell this size, not a design for thousands of cases.

## 5. The one keys act

Choosing the host and holding its credentials is a keys act (MasterMind `CHARTER.md` section 3; `AGENTS.md` Class X). The owner picks the machine, runs `syberwork init` there once, and keeps the printed tokens and the database password. An agent prepares and documents the commands, as this file does, and does not run them against a host it was not handed.
