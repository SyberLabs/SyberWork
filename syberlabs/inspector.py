"""A read-only local inspector for Build Threads.

    syberlabs inspect            # prints http://127.0.0.1:PORT/#token=...

It shows threads, candidates with their lineage graph, diffs, verdicts, and
the event history, read fresh from the journal on every request. It cannot
change anything: only GET is served, and a verdict here never runs checks.

Protections, for a page that shows source code and history:

- binds to 127.0.0.1 only;
- every API request needs ``Authorization: Bearer <token>``. The token is
  random per run and is carried in the URL fragment, which browsers do not
  send to servers or in ``Referer`` headers;
- the ``Host`` header must name the loopback address and port, which stops
  DNS-rebinding pages from reading the API;
- static assets carry a strict Content-Security-Policy, frame denial, and
  ``nosniff``; the page renders every value with ``textContent``.
"""

from __future__ import annotations

import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from syberlabs.errors import Rejected

STATIC = Path(__file__).parent / "static"
ASSETS = {"inspector.html": "text/html", "inspector.js": "text/javascript", "inspector.css": "text/css"}
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _verdict(verdict) -> dict:
    return {"candidate": verdict.candidate, "acceptable": verdict.acceptable, "status": verdict.status,
            "reason": verdict.reason, "rule": verdict.rule, "base_current": verdict.base_current,
            "checks": [check.__dict__ for check in verdict.checks], "untested": list(verdict.untested),
            "scope_violations": list(verdict.scope_violations), "limit_violations": list(verdict.limit_violations),
            "signal_unverified": verdict.signal, "hint": verdict.hint}


def make_inspector(kit, *, port: int = 0, token: str | None = None) -> ThreadingHTTPServer:
    token = token or secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        server_version = "syberlabs-inspector"
        sys_version = ""

        def log_message(self, fmt, *args):
            return

        def _send(self, status: int, body: bytes, mime: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            port_ = self.server.server_port
            return self.headers.get("Host", "") in (f"127.0.0.1:{port_}", f"localhost:{port_}")

        def do_GET(self):
            if not self._host_ok():
                self._json({"error": "bad_host"}, 421)
                return
            parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
            if not parts:
                parts = ["inspector.html"]
            if len(parts) == 1 and parts[0] in ASSETS:
                self._send(200, (STATIC / parts[0]).read_bytes(), ASSETS[parts[0]])
                return
            if parts == ["favicon.ico"]:
                self.send_response(204)
                for name, value in HEADERS.items():
                    self.send_header(name, value)
                self.end_headers()
                return
            if parts[0] != "api":
                self._json({"error": "not_found"}, 404)
                return
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
                self._json({"error": "unauthorized"}, 401)
                return
            try:
                self._api(parts[1:])
            except Rejected as exc:
                self._json({"error": exc.code, "detail": exc.detail}, 404 if exc.code.startswith("unknown") else 409)
            except Exception:
                self._json({"error": "internal_error"}, 500)

        do_HEAD = do_GET

        def _refuse(self):
            self._json({"error": "read_only", "detail": "the inspector does not change anything; use the syberlabs CLI"}, 405)

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

        def _api(self, parts: list[str]) -> None:
            if parts == ["threads"]:
                rows = []
                for row in kit.threads():
                    status = kit.open(row["id"]).status()
                    rows.append({**row, "status": status["status"], "accepted": status["accepted"],
                                 "candidates": len(status["candidates"]), "chain_valid": status["chain_valid"]})
                self._json(rows)
            elif parts == ["memory"]:
                self._json(kit.memory())
            elif len(parts) >= 2 and parts[0] == "threads":
                thread = kit.open(parts[1])
                rest = parts[2:]
                if not rest:
                    self._json({"status": thread.status(), "candidates": thread._views(),
                                "searches": [e["body"] for e in thread.history() if e["kind"].startswith("search_")]})
                elif rest == ["events"]:
                    self._json(thread.history())
                elif len(rest) == 3 and rest[0] == "candidates" and rest[2] == "diff":
                    self._send(200, thread.diff(rest[1]).encode(), "text/plain")
                elif len(rest) == 3 and rest[0] == "candidates" and rest[2] == "verdict":
                    view = thread._view(rest[1])
                    self._json({**_verdict(thread.check(rest[1], run=False)), "promotion": view["promotion"]["state"]})
                else:
                    self._json({"error": "not_found"}, 404)
            else:
                self._json({"error": "not_found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = token
    server.url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    return server
