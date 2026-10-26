"""Local operator API and inspectable browser interface."""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from syberlabs.evolution import AUTOMATION_ROLES
from syberlabs.planner import HttpPlanner

from .builder_http import dispatch_get, dispatch_post
from .core import Rejected, Work, digest

STATIC = Path(__file__).parent / "static"
# The directory holding syberlabs/ and syberwork/. Explained rule modules are named relative to it.
PACKAGES = Path(__file__).resolve().parents[1]
# The console's files. Anything else under static/ is not served.
ASSETS = {"app.js": "text/javascript", "model.js": "text/javascript", "style.css": "text/css"}
# Who may read published policy bodies (GET /api/policies). An economic policy carries budget limits, so the
# reader is an administrator or the approver role, and not an automation credential (model, compiled, search).
# There is no separate policy-read role; model.js mirrors this to suggest an identity that can read them.
POLICY_READERS = frozenset({"admin", "manager"})


def _planner_configured() -> bool:
    """Whether SYBERWORK_PLANNER_URL names a usable endpoint. The URL itself is not disclosed."""
    try:
        HttpPlanner().ensure_configured()
        return True
    except Rejected:
        return False


def _policy_reader(roles) -> bool:
    return bool(POLICY_READERS.intersection(roles)) and not AUTOMATION_ROLES.intersection(roles)


def _source_roles(work: Work) -> dict:
    """Each installed source's read roles, as refresh_fact checks them, and nothing else of its definition.

    install_source does not validate ``roles``; a value that is not a list of names is answered as None, so the
    console says the server decides instead of forecasting from a value refresh_fact would misread.
    """
    found = {}
    for name, doc in work.registry("sources").items():
        roles = doc.get("roles", ["operator"])
        found[name] = {"roles": roles if isinstance(roles, list) and all(isinstance(r, str) for r in roles) else None}
    return found


def _version(text: str) -> int:
    if not text.isdecimal() or len(text) > 9:
        raise Rejected("not_found", "contract version must be a positive integer")
    return int(text)


def _module(module) -> str | None:
    """A rule's module relative to the installed packages, or None when it lies outside them.

    Provenance names the module relative to the working directory, else to the packages, else in
    full (syberlabs/admission.py ``_rule_provenance``). From ``/`` the first form carries this host's
    layout, so the file is found again in the same order and named from the packages only.
    """
    if not isinstance(module, str) or not module:
        return None
    for root in (Path.cwd(), PACKAGES):
        try:
            found = (root / module).resolve()
            if found.is_file():
                return found.relative_to(PACKAGES).as_posix()
        except (OSError, ValueError):
            return None
    return None


def _explanation(work: Work, case_id: str, proposal_id: str, when: str) -> dict:
    """Work.explain_admission for an HTTP reader, plus the decision as recorded. Appends nothing.

    A rule module outside the installed packages (a rule loaded from elsewhere) is withheld rather
    than disclose this host's filesystem layout; the rule and symbol are still answered.
    """
    explained = work.explain_admission(case_id, proposal_id, when=when)
    found = explained.get("provenance") or {}
    module = _module(found.get("module"))
    return {"proposal_id": proposal_id, "when": when, "status": explained["status"], "reason": explained["reason"],
            "rule": explained["rule"], "provenance": {"module": module, "symbol": found.get("symbol"), "line": found.get("line")},
            "recorded": work.recorded_decision(case_id, proposal_id)}


def serve(work: Work, users: dict, host: str = "127.0.0.1", port: int = 8766):
    httpd = make_server(work, users, host, port)
    print(f"SyberWork listening at http://{host}:{httpd.server_port}", flush=True)
    httpd.serve_forever()


def make_server(work: Work, users: dict, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    """Build the loopback server without starting it. Port 0 picks a free port."""
    loopback = host in ("127.0.0.1", "::1", "localhost")
    if not loopback and os.getenv("SYBERWORK_BIND", "") != host:
        raise Rejected("unsafe_bind", "the operator service must bind to loopback")
    started = time.monotonic()

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

        def _json(self, data, status=200, headers=None):
            body = json.dumps(data, separators=(",", ":"), default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
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
                if len(parts) == 1 and parts[0] in ASSETS:
                    self._static(parts[0], ASSETS[parts[0]])
                    return
                user = self._user()
                if len(parts) in (2, 3) and parts[:2] == ["metrics", "work_items"]:
                    # Per-work-item metrics (docs/METRICS.md): every item, or one thread's, with each number's basis.
                    try:
                        self._json(work.work_items(unquote(parts[2]) if len(parts) == 3 else None))
                    except Rejected as e:
                        self._json({"error": e.code, "detail": e.detail}, 404 if e.code == "unknown_case" else 409)
                elif len(parts) == 2 and parts[0] == "metrics":
                    self._metric(unquote(parts[1]))
                elif parts == ["api", "me"]:
                    profile = {"name": user["name"], "roles": user.get("roles", []), "sources": user.get("sources", [])}
                    for key in ("principal_id", "kind", "organization", "delegation"):
                        if key in user:
                            profile[key] = user[key]
                    self._json(profile)
                elif parts == ["api", "console"]:
                    # What the console needs to render honest states; no secret or URL is disclosed.
                    self._json({"planner_configured": _planner_configured(), "effects": work.effects})
                elif parts == ["api", "cases"]:
                    self._json(work.list_cases())
                elif parts == ["api", "contracts"]:
                    self._json(work.list_contracts())
                elif len(parts) == 4 and parts[:2] == ["api", "contracts"]:
                    self._json(work.contract(unquote(parts[2]), _version(parts[3])))
                elif len(parts) == 5 and parts[:2] == ["api", "contracts"] and parts[4] == "artifacts":
                    self._json(work.artifacts(unquote(parts[2]), _version(parts[3])))
                elif parts == ["api", "policies"]:
                    if not _policy_reader(user.get("roles", [])):
                        raise Rejected("forbidden", "admin or manager role required, without an automation role")
                    self._json(work.list_policies())
                elif parts == ["api", "sources"] and "admin" not in user.get("roles", []):
                    # Anyone else sees only the read roles, so a refusal can name the role it needs.
                    self._json(_source_roles(work))
                elif parts in (["api", "actions"], ["api", "sources"]):
                    if "admin" not in user.get("roles", []):
                        raise Rejected("forbidden", "admin role required")
                    self._json(work.registry(parts[1]))
                elif len(parts) == 3 and parts[:2] == ["api", "cases"]:
                    self._json(work.inspect(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "candidates":
                    self._json(work.candidates(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "verify":
                    self._json({"valid": work.verify_chain(parts[2])})
                elif len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "side":
                    # Work.side_channel: the JCS digest and deciding rule id stored beside each event. Neither is
                    # in the event hash; events written before that table existed have no row.
                    self._json(work.side_channel(unquote(parts[2])))
                elif len(parts) == 6 and parts[:2] == ["api", "cases"] and parts[3] == "proposals" and parts[5] == "explain":
                    # The deciding rule for a stored proposal: ?when=recorded (the original decision) or now (the default).
                    when = parse_qs(urlsplit(self.path).query).get("when", ["now"])[-1]
                    self._json(_explanation(work, unquote(parts[2]), unquote(parts[4]), when))
                elif parts[:2] == ["api", "builder"]:
                    outcome = dispatch_get(work, user, parts, urlsplit(self.path).query, self.headers.get("Last-Event-ID"))
                    if isinstance(outcome, dict):
                        self._json(outcome)
                    else:
                        self._sse(outcome)
                else:
                    self._json({"error": "not_found"}, 404)
            except Rejected as e:
                status = {"unauthorized": 401, "not_found": 404, "unknown_metric": 404}.get(e.code, 409)
                if e.code == "forbidden" and self._parts()[:2] != ["api", "builder"]:
                    status = 403  # a console read the principal's roles do not cover; the builder keeps its statuses
                self._json({"error": e.code, "detail": e.detail}, status)
            except Exception:
                self._json({"error": "internal_error"}, 500)

        def _metric(self, key):
            """GET /metrics/<key>: one number as the intervention effect reads it (docs/OUTCOMES.md section 5).

            ``observed_at`` is the time of this read in epoch seconds, which is the form the effect's
            freshness rule understands; ``observed_at_utc`` is the same instant for people. The version is
            a digest of key, value and ``observed_at``, so a repeated read of an unchanged value still
            carries a new stamp and a new version. It is sent as ``X-Record-Version`` and, quoted, as the ETag.
            """
            observed_at = time.time()
            value = round(time.monotonic() - started, 3) if key == "uptime_seconds" else work.metric(key)
            version = digest({"key": key, "value": value, "observed_at": observed_at})
            when = datetime.fromtimestamp(observed_at, timezone.utc).isoformat(timespec="milliseconds")
            self._json({"key": key, "value": value, "observed_at": observed_at,
                        "observed_at_utc": when.replace("+00:00", "Z"), "version": version},
                       headers={"ETag": '"' + version + '"', "X-Record-Version": version})

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
                        # Only the proposal is named. Any outcome-shaped field (success, evidence,
                        # proof, an Attest receipt, ...) is refused rather than ignored.
                        if set(data) != {"proposal_id"}:
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
