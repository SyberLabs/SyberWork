"""Local operator API and inspectable browser interface."""

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .builder_http import dispatch_get, dispatch_post
from .core import Rejected, Work

STATIC = Path(__file__).parent / "static"


def serve(work: Work, users: dict, host: str = "127.0.0.1", port: int = 8766):
    httpd = make_server(work, users, host, port)
    print(f"SyberWork listening at http://{host}:{httpd.server_port}", flush=True)
    httpd.serve_forever()


def make_server(work: Work, users: dict, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    """Build the loopback server without starting it. Port 0 picks a free port."""
    loopback = host in ("127.0.0.1", "::1", "localhost")
    if not loopback and os.getenv("SYBERWORK_BIND", "") != host:
        raise Rejected("unsafe_bind", "the operator service must bind to loopback")

    class Handler(BaseHTTPRequestHandler):
        def _user(self):
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            hashed = hashlib.sha256(token.encode()).hexdigest()
            for name, info in users.items():
                if token and hmac.compare_digest(info["hash"], hashed):
                    return {"name": name, **info}
            principal = work.principal_for_token(token)
            if principal:
                return principal
            raise Rejected("unauthorized", "valid bearer token required")

        def _json(self, data, status=200):
            body = json.dumps(data, separators=(",", ":"), default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024 or length < 0:
                raise Rejected("payload_size", "maximum body is 1 MB")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise Rejected("body_type", "JSON object required")
            return data

        def _parts(self):
            return [p for p in urlsplit(self.path).path.split("/") if p]

        def do_GET(self):
            try:
                parts = self._parts()
                if not parts or parts == ["index.html"]:
                    self._static("index.html", "text/html")
                    return
                if parts in (["app.js"], ["style.css"]):
                    self._static(parts[0], "text/javascript" if parts[0].endswith(".js") else "text/css")
                    return
                user = self._user()
                if parts == ["api", "me"]:
                    profile = {"name": user["name"], "roles": user.get("roles", []), "sources": user.get("sources", [])}
                    for key in ("principal_id", "kind", "organization", "delegation"):
                        if key in user:
                            profile[key] = user[key]
                    self._json(profile)
                elif parts == ["api", "cases"]:
                    self._json(work.list_cases())
                elif len(parts) == 5 and parts[:2] == ["api", "contracts"] and parts[4] == "artifacts":
                    self._json(work.artifacts(parts[2], int(parts[3])))
                elif len(parts) == 3 and parts[:2] == ["api", "cases"]:
                    self._json(work.inspect(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "candidates":
                    self._json(work.candidates(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "verify":
                    self._json({"valid": work.verify_chain(parts[2])})
                elif parts[:2] == ["api", "builder"]:
                    outcome = dispatch_get(work, user, parts, urlsplit(self.path).query, self.headers.get("Last-Event-ID"))
                    if isinstance(outcome, dict):
                        self._json(outcome)
                    else:
                        self._sse(outcome)
                else:
                    self._json({"error": "not_found"}, 404)
            except Rejected as e:
                status = {"unauthorized": 401, "not_found": 404}.get(e.code, 409)
                self._json({"error": e.code, "detail": e.detail}, status)
            except Exception:
                self._json({"error": "internal_error"}, 500)

        def _static(self, name, mime):
            body = (STATIC / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, chunks):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Syber-Stream", "development-poll")
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

        def do_POST(self):
            try:
                user, parts, data = self._user(), self._parts(), self._body()
                name, roles = user["name"], user.get("roles", [])
                if parts == ["api", "contracts"] and "admin" in roles:
                    work.install_contract(data); result = {"installed": True}
                elif parts == ["api", "policies"] and "admin" in roles:
                    work.install_policy(data); result = {"installed": True}
                elif len(parts) == 3 and parts[:2] == ["api", "actions"] and "admin" in roles:
                    work.install_action(parts[2], data); result = {"installed": True}
                elif len(parts) == 3 and parts[:2] == ["api", "sources"] and "admin" in roles:
                    work.install_source(parts[2], data); result = {"installed": True}
                elif parts == ["api", "cases"]:
                    if "operator" not in roles:
                        raise Rejected("forbidden", "operator role required")
                    result = {"id": work.create_case(data["contract_id"], data["version"], data["inputs"], name)}
                elif len(parts) == 4 and parts[:2] == ["api", "cases"]:
                    case_id, operation = parts[2:]
                    if operation == "facts":
                        if data["source"] not in user.get("sources", []):
                            raise Rejected("source_denied", "actor cannot attest this source")
                        result = work.observe(case_id, data["key"], data["value"], data["source"], data["version"], name)
                    elif operation == "refresh":
                        result = work.refresh_fact(case_id, data["source"], data["key"], data["record_key"], name, roles)
                    elif operation == "resolution-request":
                        result = work.request_resolution(case_id, data["key"], name, roles)
                    elif operation == "resolution-resolve":
                        result = work.resolve_resolution(case_id, data["task_id"], name, roles)
                    elif operation == "resolution-escalate":
                        result = work.escalate_resolution(case_id, data["task_id"], name, roles)
                    elif operation == "cancel":
                        result = work.cancel_case(case_id, data["reason"], name, roles)
                    elif operation == "proposals":
                        result = work.propose(case_id, data["action"], data["args"], name, roles, data.get("origin", "human"))
                    elif operation == "compiled":
                        result = work.compiled_propose(case_id, name, roles)
                    elif operation == "suggest":
                        result = work.model_propose(case_id, name, roles)
                    elif operation == "approve":
                        result = work.approve(case_id, data["proposal_id"], name, roles)
                    elif operation == "commit":
                        result = work.commit(case_id, data["proposal_id"], name)
                    elif operation == "signoff":
                        result = work.signoff(case_id, name, roles, data["role"])
                    elif operation == "reconcile":
                        if "success" in data or "evidence" in data:
                            raise Rejected("manual_reconciliation_disabled", "the destination must report the outcome")
                        result = work.reconcile(case_id, data["proposal_id"], name, roles)
                    elif operation == "candidates":
                        result = work.record_candidate(case_id, data, name, roles)
                    elif operation == "evaluations":
                        result = work.record_evaluation(case_id, data, name, roles)
                    elif operation == "searches":
                        result = work.record_search(case_id, data["phase"], data["search"], name, roles)
                    elif operation == "replay":
                        result = work.replay(case_id, data["contract_version"], data["policy_version"])
                    else:
                        raise Rejected("unknown_route", operation)
                elif parts[:2] == ["api", "builder"]:
                    result = dispatch_post(work, user, parts, data)
                else:
                    raise Rejected("forbidden", "route unavailable or admin role required")
                self._json(result)
            except (Rejected, KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
                self._json({"error": e.code if isinstance(e, Rejected) else "invalid_request", "detail": e.detail if isinstance(e, Rejected) else str(e)[:200]}, 409)
            except Exception:
                self._json({"error": "internal_error"}, 500)

    return ThreadingHTTPServer((host, port), Handler)
