"""Install the local service, serve it, or inspect a case without a browser."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

from .core import Work
from .server import serve


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
    args = parser.parse_args()
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    db = Work(home / "work.sqlite3")
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
            ("inventory", ["observer"], ["inventory"]),
            ("supplier", ["observer"], ["supplier"]),
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
        print("Save the tokens now. They are stored only as hashes. The included HTTP action is a template; publish a new action name and contract version for a real destination.")
    elif args.command == "serve":
        if not userfile.exists():
            parser.error("run init first")
        serve(db, json.loads(userfile.read_text()), port=args.port)
    elif args.command == "inspect":
        print(json.dumps(db.inspect(args.case_id), indent=2))
    elif args.command == "verify":
        print(json.dumps({"valid": db.verify_chain(args.case_id)}))


if __name__ == "__main__":
    main()
