"""Exercise one Compose cell: queue an effect, let a worker settle it, then restore a backup.

The case id is the only text written to stdout. Progress goes to stderr.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from syberwork.backup import export_cell, restore_cell
from syberwork.core import Work
from syberwork.trace import TraceLog
from syberwork.worker import run_once


NOTE = {
    "id": "cell-smoke",
    "version": 1,
    "inputs": {"name": "string"},
    "actions": {"note": {}},
    "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
}


def _database() -> str:
    url = os.getenv("SYBERWORK_DATABASE", "")
    if not url:
        raise SystemExit("SYBERWORK_DATABASE is required")
    return url


def _home() -> Path:
    return Path(os.getenv("SYBERWORK_HOME", "/home/syber/cell"))


def _work(url: str | None = None) -> Work:
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    return Work(url or _database(), effects="worker", trace=TraceLog(home / "traces.jsonl"))


def _install(work: Work) -> None:
    work.bind_organization(os.getenv("SYBERWORK_ORGANIZATION", "example"))
    work.install_contract(NOTE)
    # init already published policy version 1 for the example contract.
    work.install_policy({"version": 2, "actions": {"note": {"roles": ["operator"]}}})
    work.install_action("note", {"kind": "local"})


def _settled(work: Work, case_id: str) -> bool:
    events = work.inspect(case_id)["events"]
    return any(event["kind"] == "effect_succeeded" for event in events) and work.verify_chain(case_id)


def queue() -> None:
    work = _work()
    try:
        _install(work)
        case_id = work.create_case("cell-smoke", 1, {"name": "compose"}, "operator")
        proposed = work.propose(case_id, "note", {}, "operator", ["operator"])
        queued = work.commit(case_id, proposed["proposal"]["id"], "operator")
        if queued["status"] != "queued":
            raise SystemExit(f"expected a queued effect, got {queued['status']}")
        if _settled(work, case_id):
            raise SystemExit("the effect settled before a worker ran")
    finally:
        work.close()
    print(case_id, flush=True)


def wait(case_id: str) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        work = _work()
        try:
            if _settled(work, case_id):
                return
        finally:
            work.close()
        time.sleep(0.5)
    raise SystemExit("worker did not settle the effect")


def verify(case_id: str) -> None:
    work = _work()
    try:
        if not _settled(work, case_id):
            raise SystemExit("case is not settled")
        state = work.inspect(case_id)
        if not state["complete"]:
            raise SystemExit("case is not complete")
    finally:
        work.close()


def backup_restore(case_id: str) -> None:
    import psycopg

    url = _database()
    home = _home()
    snapshot = home / "backup"
    work = _work(url)
    try:
        export_cell(work, snapshot)
    finally:
        work.close()
    name = "syberwork_restore"
    admin = url.rsplit("/", 1)[0] + "/postgres"
    connection = psycopg.connect(admin, autocommit=True)
    try:
        connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
        connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
        connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        connection.close()
    restored = _work(url.rsplit("/", 1)[0] + "/" + name)
    try:
        restore_cell(restored, snapshot)
        if not _settled(restored, case_id):
            raise SystemExit("restored case did not verify")
    finally:
        restored.close()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"queue", "wait", "verify", "backup-restore"}:
        raise SystemExit("usage: python -m syberwork.smoke queue|wait|verify|backup-restore [case_id]")
    command, rest = argv[0], argv[1:]
    if command == "queue":
        queue()
        return
    if len(rest) != 1:
        raise SystemExit(f"{command} requires a case id")
    {"wait": wait, "verify": verify, "backup-restore": backup_restore}[command](rest[0])


if __name__ == "__main__":
    main()
