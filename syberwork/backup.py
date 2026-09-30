"""Logical backup and restore of one cell.

The snapshot is JSON, not a server-specific dump, so a SQLite appliance and a
PostgreSQL cell can be compared. Restore opens a new database, migrates it,
then loads the rows. Every case chain is checked before the backup returns
and again after restore.
"""

from __future__ import annotations

import json
from pathlib import Path

from syberlabs.errors import Rejected

from .storage import SNAPSHOT_TABLES


# Tables introduced by migration 0004. A snapshot from before that migration
# has no file for them. Every older table is authoritative and must be present.
OPTIONAL_SNAPSHOT_TABLES = frozenset(name for name in SNAPSHOT_TABLES if name == "coordination_events" or name.startswith("builder_"))


def _columns(store, table: str) -> list[str]:
    row = store.execute(f"SELECT * FROM {table} LIMIT 0")
    return [item[0] for item in row.description]


def export_cell(work, directory: str | Path) -> dict:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with work.tx() as db:
        case_ids = [row["id"] for row in db.execute("SELECT id FROM cases").fetchall()]
    for case_id in case_ids:
        if not work.verify_chain(case_id):
            raise Rejected("backup_refused", f"case {case_id} failed verification")
    manifest = {"format": "syberwork-cell-snapshot-v0.1", "cases": case_ids}
    with work.tx() as db:
        for table in SNAPSHOT_TABLES:
            columns = _columns(db, table)
            rows = []
            for record in db.execute(f"SELECT * FROM {table}").fetchall():
                rows.append({column: record[column] for column in columns})
            (directory / f"{table}.json").write_text(json.dumps(rows, separators=(",", ":"), default=str), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def restore_cell(work, directory: str | Path) -> dict:
    """Load a snapshot into an empty cell database. Refuses a database that already has cases."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    with work.tx() as db:
        existing = db.execute("SELECT id FROM cases LIMIT 1").fetchone()
        if existing:
            raise Rejected("restore_refused", "restore only into an empty cell database")
        for table in SNAPSHOT_TABLES:
            path = directory / f"{table}.json"
            if not path.exists():
                if table in OPTIONAL_SNAPSHOT_TABLES:
                    continue
                raise Rejected("restore_refused", f"snapshot is missing {table}")
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not rows:
                continue
            columns = list(rows[0])
            placeholders = ",".join("?" for _ in columns)
            quoted = ",".join(columns)
            for row in rows:
                db.execute(
                    f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
                    tuple(row[column] for column in columns),
                )
        for case_id in manifest["cases"]:
            if not work.chain_ok(db, case_id):
                raise Rejected("restore_refused", f"restored case {case_id} failed verification")
    return manifest
