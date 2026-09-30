"""Persistence for one SyberWork cell.

A cell has one database. Admission does not import this module. SQLite is the
local appliance. PostgreSQL is the managed cell. Both execute the same
statements; only the connection and a few dialect spellings differ.

``CaseStore``, ``EventStore``, ``ContractStore``, ``PolicyStore``,
``ActionRegistry``, ``SourceRegistry``, and ``ReservationStore`` are the
persistence seams. ``Work`` is the runtime that calls them. An in-memory case
store remains ``syberlabs.Session``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from syberlabs.errors import Rejected


_REAL = "DOUBLE PRECISION"


def _statements(sql: str) -> list[str]:
    return [part.strip() for part in sql.split(";") if part.strip()]


_MIGRATIONS: list[tuple[str, str]] = [
    ("0001", f"""
        CREATE TABLE IF NOT EXISTS cell_lock (
            id INTEGER PRIMARY KEY,
            nonce INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO cell_lock VALUES (1, 0);
        CREATE TABLE IF NOT EXISTS contracts (
            id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY (id, version)
        );
        CREATE TABLE IF NOT EXISTS policies (
            version INTEGER PRIMARY KEY, body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS actions (
            name TEXT PRIMARY KEY, body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            name TEXT PRIMARY KEY, body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY, contract_id TEXT NOT NULL,
            contract_version INTEGER NOT NULL, inputs TEXT NOT NULL,
            created {_REAL} NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            case_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
            body TEXT NOT NULL, at {_REAL} NOT NULL, previous TEXT NOT NULL,
            hash TEXT NOT NULL, at_json TEXT, PRIMARY KEY (case_id, seq)
        );
        CREATE TABLE IF NOT EXISTS economic_reservations (
            proposal_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
            budget_id TEXT NOT NULL, asset TEXT NOT NULL,
            amount_units INTEGER NOT NULL, state TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_side (
            case_id TEXT NOT NULL, seq INTEGER NOT NULL,
            jcs TEXT, rule TEXT, PRIMARY KEY (case_id, seq)
        );
    """),
    ("0002", f"""
        CREATE TABLE IF NOT EXISTS effect_obligations (
            proposal_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            state TEXT NOT NULL,
            lease_until {_REAL},
            attempts INTEGER NOT NULL
        );
    """),
    ("0003", """
        CREATE TABLE IF NOT EXISTS principals (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            organization TEXT NOT NULL,
            token_hash TEXT,
            roles TEXT NOT NULL,
            delegation TEXT
        );
        CREATE TABLE IF NOT EXISTS cell (
            id INTEGER PRIMARY KEY,
            organization TEXT NOT NULL,
            cell_version TEXT NOT NULL
        );
    """),
    ("0004", f"""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id TEXT PRIMARY KEY,
            seq INTEGER NOT NULL,
            case_id TEXT NOT NULL,
            generation_id TEXT,
            kind TEXT NOT NULL,
            actor TEXT NOT NULL,
            at BIGINT NOT NULL,
            body TEXT NOT NULL,
            UNIQUE (case_id, seq)
        );
        CREATE INDEX IF NOT EXISTS coordination_events_case_seq
            ON coordination_events (case_id, seq);
        CREATE TABLE IF NOT EXISTS builder_works (
            case_id TEXT PRIMARY KEY,
            objective TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_policies (
            id TEXT NOT NULL,
            version INTEGER NOT NULL,
            body TEXT NOT NULL,
            PRIMARY KEY (id, version)
        );
        CREATE TABLE IF NOT EXISTS builder_generations (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            objective TEXT NOT NULL,
            base_revision TEXT NOT NULL,
            mode TEXT NOT NULL,
            isolation TEXT NOT NULL,
            diversity_threshold {_REAL} NOT NULL,
            min_approaches INTEGER NOT NULL,
            selection_policy_id TEXT NOT NULL,
            selection_policy_version INTEGER NOT NULL,
            state TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            UNIQUE (case_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS builder_approaches (
            id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL,
            state TEXT NOT NULL,
            descriptor TEXT NOT NULL,
            evidence TEXT NOT NULL,
            created_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_agents (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            approach_id TEXT,
            candidate_id TEXT,
            principal TEXT NOT NULL,
            role TEXT NOT NULL,
            authority TEXT NOT NULL,
            assignment TEXT NOT NULL,
            working_set TEXT NOT NULL,
            state TEXT NOT NULL,
            hypothesis_summary TEXT NOT NULL,
            evidence_refs TEXT NOT NULL,
            open_uncertainties TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_feedback (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            generation_id TEXT,
            target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            authorities TEXT NOT NULL,
            actor TEXT NOT NULL,
            body TEXT NOT NULL,
            at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_integrity (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            generation_id TEXT,
            target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            claim TEXT NOT NULL,
            source TEXT NOT NULL,
            verifier TEXT NOT NULL,
            independence_claim TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            verification_method TEXT,
            verified_independence TEXT,
            evidence_refs TEXT NOT NULL,
            result TEXT NOT NULL,
            digest TEXT,
            at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_selections (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            actor TEXT NOT NULL,
            body TEXT NOT NULL,
            at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_architecture (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            generation_id TEXT,
            repository TEXT NOT NULL,
            revision TEXT NOT NULL,
            provider TEXT NOT NULL,
            body TEXT NOT NULL,
            at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_prototypes (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            generation_id TEXT,
            candidate_id TEXT NOT NULL,
            artifact_ref TEXT NOT NULL,
            environment TEXT NOT NULL,
            provider TEXT NOT NULL,
            endpoint TEXT,
            state TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            expires_at BIGINT,
            digest TEXT
        );
        CREATE TABLE IF NOT EXISTS builder_commands (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            command TEXT NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,
            actor TEXT NOT NULL,
            at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_candidate_links (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            approach_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            at BIGINT NOT NULL,
            authority TEXT NOT NULL,
            UNIQUE (generation_id, candidate_id)
        );
    """),
    ("0005", """
        CREATE TABLE IF NOT EXISTS builder_worlds (
            digest TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_generation_worlds (
            generation_id TEXT PRIMARY KEY,
            world_digest TEXT NOT NULL,
            sealed_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS builder_evaluation_bindings (
            id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            evaluation_event_hash TEXT NOT NULL,
            evaluator TEXT NOT NULL,
            world_digest TEXT NOT NULL,
            world_instance_digest TEXT,
            trace_digest TEXT,
            evaluation_run_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            at BIGINT NOT NULL,
            UNIQUE (evaluation_event_hash)
        );
    """),
]


def translate(sql: str, dialect: str) -> str:
    """Keep ``?`` and ``INSERT OR IGNORE`` in callers. PostgreSQL gets its spellings."""
    if dialect != "postgres":
        return sql
    if sql.lstrip().startswith("INSERT OR IGNORE"):
        sql = "INSERT" + sql.lstrip()[len("INSERT OR IGNORE"):]
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return sql.replace("?", "%s")


class Store(Protocol):
    dialect: str

    def execute(self, sql: str, params: tuple = ()): ...

    def begin(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


# Advisory lock key for PostgreSQL migration. It does not depend on cell_lock existing yet.
_MIGRATION_LOCK = 87261001


class _SqliteStore:
    dialect = "sqlite"

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._db = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=15000")
        migrate(self)

    def execute(self, sql: str, params: tuple = ()):
        return self._db.execute(translate(sql, self.dialect), params)

    def begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._db.commit()

    def rollback(self) -> None:
        db = self._db
        if db is None:
            return
        try:
            db.rollback()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            db.close()


class _PostgresStore:
    dialect = "postgres"

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise Rejected("postgres_unavailable", "install psycopg to open a PostgreSQL cell") from None
        try:
            self._db = psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=5)
        except Exception:
            raise Rejected("postgres_unavailable", "PostgreSQL connection failed") from None
        migrate(self)

    def execute(self, sql: str, params: tuple = ()):
        return self._db.execute(translate(sql, self.dialect), params)

    def begin(self) -> None:
        self._db.execute("BEGIN")
        self._db.execute("SELECT nonce FROM cell_lock WHERE id = 1 FOR UPDATE")

    def commit(self) -> None:
        self._db.commit()

    def rollback(self) -> None:
        db = self._db
        if db is None:
            return
        try:
            db.rollback()
        except Exception:
            pass

    def close(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            db.close()


def open_store(target: str | Path):
    """Open the cell database. A ``postgresql://`` URL selects PostgreSQL."""
    text = str(target)
    scheme = urlsplit(text).scheme
    if scheme in ("postgresql", "postgres"):
        return _PostgresStore(text)
    return _SqliteStore(text)


def _column_names(store, table: str) -> set[str]:
    if store.dialect == "sqlite":
        rows = store.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            return set()
        names = set()
        for row in rows:
            try:
                names.add(row["name"])
            except (KeyError, IndexError, TypeError):
                names.add(row[1])
        return names
    rows = store.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def migrate(store) -> list[str]:
    """Apply unapplied migrations. One opener at a time; a second waits.

    SQLite takes ``BEGIN IMMEDIATE``. PostgreSQL takes a session advisory lock,
    which works before ``cell_lock`` exists. Existing histories keep their rows
    and hashes.
    """
    if store.dialect == "sqlite":
        store.execute("BEGIN IMMEDIATE")
    else:
        store.execute("SELECT pg_advisory_lock(?)", (_MIGRATION_LOCK,))
    try:
        store.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        if store.dialect == "sqlite" and _column_names(store, "events") and "at_json" not in _column_names(store, "events"):
            store.execute("ALTER TABLE events ADD COLUMN at_json TEXT")
        applied = {row["version"] for row in store.execute("SELECT version FROM schema_migrations").fetchall()}
        ran = []
        for version, script in _MIGRATIONS:
            if version in applied:
                continue
            for statement in _statements(script):
                store.execute(statement)
            store.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (version, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            ran.append(version)
        if store.dialect == "sqlite":
            store.commit()
        return ran
    except BaseException:
        if store.dialect == "sqlite":
            store.rollback()
        raise
    finally:
        if store.dialect == "postgres":
            store.execute("SELECT pg_advisory_unlock(?)", (_MIGRATION_LOCK,))


class ContractStore:
    """Immutable contract documents."""

    def __init__(self, store):
        self.store = store

    def get(self, contract_id: str, version: int):
        return self.store.execute(
            "SELECT body FROM contracts WHERE id=? AND version=?",
            (contract_id, version),
        ).fetchone()

    def insert_new(self, contract_id: str, version: int, body: str) -> None:
        self.store.execute(
            "INSERT OR IGNORE INTO contracts VALUES (?,?,?)",
            (contract_id, version, body),
        )


class PolicyStore:
    def __init__(self, store):
        self.store = store

    def max_version(self):
        return self.store.execute("SELECT max(version) AS v FROM policies").fetchone()["v"]

    def get(self, version: int):
        return self.store.execute("SELECT body FROM policies WHERE version=?", (version,)).fetchone()

    def latest(self):
        return self.store.execute("SELECT body FROM policies ORDER BY version DESC LIMIT 1").fetchone()

    def bodies(self) -> list:
        return self.store.execute("SELECT body FROM policies").fetchall()

    def insert_new(self, version: int, body: str) -> None:
        self.store.execute("INSERT OR IGNORE INTO policies VALUES (?,?)", (version, body))


class ActionRegistry:
    def __init__(self, store):
        self.store = store

    def get(self, name: str):
        return self.store.execute("SELECT body FROM actions WHERE name=?", (name,)).fetchone()

    def names(self) -> set[str]:
        return {row["name"] for row in self.store.execute("SELECT name FROM actions")}

    def documents(self) -> dict:
        return {row["name"]: row["body"] for row in self.store.execute("SELECT name, body FROM actions")}

    def insert_new(self, name: str, body: str) -> None:
        self.store.execute("INSERT OR IGNORE INTO actions VALUES (?,?)", (name, body))


class SourceRegistry:
    def __init__(self, store):
        self.store = store

    def get(self, name: str):
        return self.store.execute("SELECT body FROM sources WHERE name=?", (name,)).fetchone()

    def insert_new(self, name: str, body: str) -> None:
        self.store.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (name, body))


class CaseStore:
    def __init__(self, store):
        self.store = store

    def get(self, case_id: str):
        return self.store.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()

    def insert(self, case_id: str, contract_id: str, version: int, inputs: str, created: float) -> None:
        self.store.execute(
            "INSERT INTO cases VALUES (?,?,?,?,?)",
            (case_id, contract_id, version, inputs, created),
        )

    def list_all(self) -> list:
        return self.store.execute("SELECT * FROM cases ORDER BY created DESC").fetchall()


class EventStore:
    def __init__(self, store):
        self.store = store

    def previous(self, case_id: str):
        return self.store.execute(
            "SELECT seq, hash FROM events WHERE case_id=? ORDER BY seq DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    def append(self, case_id: str, seq: int, kind: str, body: str, at, previous: str, digest: str, at_json: str) -> None:
        self.store.execute(
            "INSERT INTO events (case_id, seq, kind, body, at, previous, hash, at_json) VALUES (?,?,?,?,?,?,?,?)",
            (case_id, seq, kind, body, at, previous, digest, at_json),
        )

    def append_side(self, case_id: str, seq: int, jcs: str | None, rule: str | None) -> None:
        self.store.execute(
            "INSERT INTO event_side VALUES (?,?,?,?)",
            (case_id, seq, jcs, rule),
        )

    def read(self, case_id: str) -> list:
        return self.store.execute(
            "SELECT case_id, seq, kind, body, at, previous, hash, at_json FROM events WHERE case_id=? ORDER BY seq",
            (case_id,),
        ).fetchall()

    def side(self, case_id: str) -> list:
        return self.store.execute(
            "SELECT seq, jcs, rule FROM event_side WHERE case_id=? ORDER BY seq",
            (case_id,),
        ).fetchall()

    def side_digests(self, case_id: str) -> dict:
        return {
            row["seq"]: row["jcs"]
            for row in self.store.execute("SELECT seq, jcs FROM event_side WHERE case_id=?", (case_id,))
        }


class ReservationStore:
    def __init__(self, store):
        self.store = store

    def reserve(self, proposal_id, case_id, budget_id, asset, amount, state="reserved") -> None:
        self.store.execute(
            "INSERT INTO economic_reservations VALUES (?,?,?,?,?,?)",
            (proposal_id, case_id, budget_id, asset, amount, state),
        )

    def set_state(self, proposal_id: str, state: str, *, only_reserved: bool = False) -> None:
        if only_reserved:
            self.store.execute(
                "UPDATE economic_reservations SET state=? WHERE proposal_id=? AND state='reserved'",
                (state, proposal_id),
            )
            return
        self.store.execute(
            "UPDATE economic_reservations SET state=? WHERE proposal_id=?",
            (state, proposal_id),
        )

    def reserved_total(self, budget_id: str, asset: str) -> int:
        row = self.store.execute(
            "SELECT COALESCE(SUM(amount_units),0) AS total FROM economic_reservations "
            "WHERE budget_id=? AND asset=? AND state!='released'",
            (budget_id, asset),
        ).fetchone()
        return int(row["total"])


SNAPSHOT_TABLES = (
    "contracts",
    "policies",
    "actions",
    "sources",
    "cases",
    "events",
    "event_side",
    "economic_reservations",
    "effect_obligations",
    "principals",
    "cell",
    "coordination_events",
    "builder_works",
    "builder_policies",
    "builder_generations",
    "builder_approaches",
    "builder_agents",
    "builder_feedback",
    "builder_integrity",
    "builder_selections",
    "builder_architecture",
    "builder_prototypes",
    "builder_commands",
    "builder_candidate_links",
    "builder_worlds",
    "builder_generation_worlds",
    "builder_evaluation_bindings",
)
