"""Install the local service, serve it, or inspect a case without a browser."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

from .backup import export_cell, restore_cell
from .core import Work
from .server import serve
from .trace import TraceLog
from .worker import run_available, serve as serve_worker


def main():
    parser = argparse.ArgumentParser(prog="syberwork")
    parser.add_argument("--home", default=os.getenv("SYBERWORK_HOME", ".syberwork"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("serve").add_argument("--port", type=int, default=8766)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("case_id")
    verify = sub.add_parser("verify")
    verify.add_argument("case_id")
    backup = sub.add_parser("backup")
    backup.add_argument("directory")
    restore = sub.add_parser("restore")
    restore.add_argument("directory")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    target = os.getenv("SYBERWORK_DATABASE") or str(home / "work.sqlite3")
    effects = "worker" if args.command == "worker" else os.getenv("SYBERWORK_EFFECTS", "inline")
    trace = TraceLog(home / "traces.jsonl") if (home / "cell.json").exists() or os.getenv("SYBERWORK_TRACE") == "1" else None
    db = Work(target, effects=effects, trace=trace)
    userfile = home / "users.json"
    if args.command == "init":
        if userfile.exists():
            parser.error("already initialized; credentials are never overwritten")
        users = {}
        for name, roles, sources in (
            ("admin", ["admin", "operator"], []),
            ("operator", ["operator"], []),
            ("planner", ["operator", "model"], []),
            ("scheduler", ["operator", "compiled"], []),
            ("manager", ["manager"], []),
            ("logistics", ["logistics"], []),
            ("procurement", ["procurement"], []),
            ("inventory", ["observer"], ["inventory"]),
            ("supplier", ["observer"], ["supplier"]),
            ("searcher", ["search"], []),
            ("evaluator", ["evaluator"], []),
        ):
            token = secrets.token_urlsafe(32)
            users[name] = {"hash": hashlib.sha256(token.encode()).hexdigest(), "roles": roles, "sources": sources}
            print(f"{name}: {token}")
        userfile.write_text(json.dumps(users, indent=2))
        userfile.chmod(0o600)
        examples = Path(__file__).resolve().parent / "examples"
        db.install_policy(json.loads((examples / "policy.json").read_text()))
        db.install_contract(json.loads((examples / "contract.json").read_text()))
        for name, action in json.loads((examples / "actions.json").read_text()).items():
            db.install_action(name, action)
        for name, source in json.loads((examples / "sources.json").read_text()).items():
            db.install_source(name, source)
        organization = os.getenv("SYBERWORK_ORGANIZATION", "local")
        cell = db.bind_organization(organization)
        (home / "cell.json").write_text(json.dumps({
            "cell_version": cell["cell_version"],
            "organization": cell["organization"],
            "database": target,
            "isolation": "cell",
        }, indent=2) + "\n")
        print("Save the tokens now. They are stored only as hashes. The included HTTP action is a template; publish a new action name and contract version for a real destination.")
    elif args.command == "serve":
        if not userfile.exists():
            parser.error("run init first")
        serve(db, json.loads(userfile.read_text()), host=os.getenv("SYBERWORK_BIND", "127.0.0.1"), port=args.port)
    elif args.command == "backup":
        print(json.dumps(export_cell(db, args.directory)))
    elif args.command == "restore":
        print(json.dumps(restore_cell(db, args.directory)))
    elif args.command == "worker":
        if args.once:
            print(json.dumps(run_available(db), default=str))
        else:
            serve_worker(db)
    elif args.command == "inspect":
        print(json.dumps(db.inspect(args.case_id), indent=2))
    elif args.command == "verify":
        print(json.dumps({"valid": db.verify_chain(args.case_id)}))


if __name__ == "__main__":
    main()
