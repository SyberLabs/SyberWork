"""A SyberWork cell on a free loopback port, with the init roles and a trace."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from syberwork.core import Work
from syberwork.server import make_server
from syberwork.trace import TraceLog


ROLES = {
    "admin": (["admin", "operator"], []),
    "operator": (["operator"], []),
    "scheduler": (["operator", "compiled"], []),
    "manager": (["manager"], []),
    "planner": (["operator", "model"], []),
    "supplier": (["observer"], ["supplier"]),
    "inventory": (["observer"], ["inventory"]),
}


class Cell:
    def __init__(self, home: Path):
        self.home = home
        self.tokens = {name: f"sim-{name}" for name in ROLES}
        users = {}
        for name, (roles, sources) in ROLES.items():
            token = self.tokens[name]
            users[name] = {
                "hash": hashlib.sha256(token.encode()).hexdigest(),
                "roles": roles,
                "sources": sources,
            }
        self.trace = TraceLog(home / "traces.jsonl")
        self.work = Work(home / "work.sqlite3", effects="worker", trace=self.trace)
        self.server = make_server(self.work, users, port=0)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, method: str, path: str, token: str | None = None, data=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.work.close()
