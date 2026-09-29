"""Run a synthetic medical-device procurement operation against a separate ERP.

Everything is local and fictional. Work and the ERP use different SQLite databases;
HTTP reads, conditional writes, response loss and subsequent lookup are real I/O.
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from syberwork.core import Rejected, Work, digest


@contextmanager
def sqlite_session(path, timeout=5.0):
    """Open SQLite and close it. The stdlib context manager commits, but it does not close."""
    connection = sqlite3.connect(path, timeout=timeout)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


CONTRACT = {
    "id": "northstar-spare-procurement", "version": 1,
    "title": "Restore Northstar Medical Devices packaging line without ungoverned spend",
    "inputs": {"request_id": "string"},
    "input_bindings": {"request_id": "fact:request.id"},
    "resolutions": {
        "site": {
            "owner_role": "logistics", "escalate_role": "manager", "due_seconds": 1800,
            "blocks_actions": ["record_review", "issue_order"],
            "record_key_input": "request_id",
            "trigger": {"key": "request", "source": "requisitions", "missing_path": "site_id", "identity_path": "id"},
            "choices": {"key": "site_options", "source": "site_options", "list_path": "ids", "identity_path": "request_id"},
            "result": {"key": "request", "source": "requisitions", "value_path": "site_id", "identity_path": "id"},
        },
        "quote": {
            "owner_role": "procurement", "escalate_role": "manager", "due_seconds": 3600,
            "blocks_actions": ["issue_order"],
            "record_key_input": "request_id",
            "trigger": {"key": "quote_options", "source": "quote_options", "missing_path": "selection", "identity_path": "request_id"},
            "choices": {"key": "quote_options", "source": "quote_options", "list_path": "ids", "identity_path": "request_id"},
            "result": {"key": "quote", "source": "supplier", "value_path": "id", "identity_path": "request_id"},
            "confirmation": {"key": "quote_options", "source": "quote_options", "value_path": "selection"},
        },
    },
    "cancel_role": "manager",
    "actions": {
        "record_review": {
            "required_facts": [
                {"key": "request", "source": "requisitions", "verified": True, "max_age_seconds": 3600},
                {"key": "site", "source": "sites", "verified": True, "max_age_seconds": 3600},
            ],
            "arguments": {"request_id": "fact:request.id", "site_id": "fact:site.id"},
        },
        "issue_order": {
            "required_facts": [
                {"key": "request", "source": "requisitions", "verified": True, "max_age_seconds": 3600},
                {"key": "site", "source": "sites", "verified": True, "max_age_seconds": 3600},
                {"key": "quote", "source": "supplier", "verified": True, "max_age_seconds": 900},
                {"key": "budget", "source": "finance", "verified": True, "max_age_seconds": 900},
            ],
            "arguments": {
                "request": "fact:request", "site": "fact:site", "quote": "fact:quote",
                "budget": "fact:budget", "amount": "fact:quote.total",
                "quote_version": "version:quote",
            },
            "requires_effect": "record_review", "approval_role": "manager", "max_amount": 5000,
        },
    },
    "acceptance": [
        {"id": "order_recorded", "kind": "effect", "action": "issue_order"},
        {"id": "manager_signed_after_order", "kind": "signoff", "role": "manager", "after_action": "issue_order"},
    ],
    "compiled_path": ["record_review", "issue_order"],
}
POLICY = {"version": 1, "actions": {
    "record_review": {"roles": ["operator"]},
    "issue_order": {"roles": ["operator"], "approval_role": "manager", "max_amount": 10000},
}}


class SimulatedERP:
    """Independent records and conditional order endpoint, backed by SQLite."""

    def __init__(self, path: Path):
        self.path = path
        self.drop_after_write = False
        self.drop_before_write = False
        self.tokens = {role: uuid.uuid4().hex for role in ("logistics", "procurement")}
        with sqlite_session(path) as db:
            db.executescript("""
                CREATE TABLE requests(id TEXT PRIMARY KEY, part TEXT, quantity INTEGER,
                    site_id TEXT, cost_center TEXT, state TEXT, version TEXT);
                CREATE TABLE sites(id TEXT PRIMARY KEY, address TEXT, state TEXT, version TEXT);
                CREATE TABLE quotes(id TEXT PRIMARY KEY, request_id TEXT, vendor TEXT,
                    unit_price INTEGER, total INTEGER, currency TEXT, version TEXT);
                CREATE TABLE quote_selections(request_id TEXT PRIMARY KEY, quote_id TEXT, version INTEGER);
                CREATE TABLE budgets(id TEXT PRIMARY KEY, available INTEGER, currency TEXT, version INTEGER);
                CREATE TABLE orders(id TEXT PRIMARY KEY, request_id TEXT, quote_id TEXT,
                    site_id TEXT, amount INTEGER, idempotency_key TEXT UNIQUE,
                    request_digest TEXT NOT NULL);
                CREATE TABLE update_audit(actor_role TEXT, target TEXT, record_key TEXT,
                    selected TEXT, old_version TEXT, new_version TEXT);
                INSERT INTO requests VALUES ('REQ-4812','P-104',2,'DC-WEST-4','CC-742','approved','req:6');
                INSERT INTO requests VALUES ('REQ-4813','P-104',2,NULL,'CC-742','approved','req:3');
                INSERT INTO requests VALUES ('REQ-4814','P-104',2,'DC-WEST-4','CC-742','approved','req:4');
                INSERT INTO sites VALUES ('DC-WEST-4','901 Harbor Way, Oakland CA','active','site:12');
                INSERT INTO sites VALUES ('DC-EAST-2','12 Research Road, Boston MA','active','site:7');
                INSERT INTO quotes VALUES ('Q-881','REQ-4812','Alder Components',1250,2500,'USD','quote:3');
                INSERT INTO quotes VALUES ('Q-883','REQ-4813','Alder Components',1250,2500,'USD','quote:2');
                INSERT INTO quotes VALUES ('Q-882A','REQ-4814','Alder Components',1250,2500,'USD','quote:1');
                INSERT INTO quotes VALUES ('Q-882B','REQ-4814','Beacon Parts',1320,2640,'USD','quote:1');
                INSERT INTO quote_selections VALUES ('REQ-4812','Q-881',1);
                INSERT INTO quote_selections VALUES ('REQ-4813','Q-883',1);
                INSERT INTO quote_selections VALUES ('REQ-4814',NULL,1);
                INSERT INTO budgets VALUES ('CC-742',5000,'USD',8);
            """)

        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def respond(self, value, status=200, version=None):
                encoded = json.dumps(value, sort_keys=True).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                if version:
                    self.send_header("ETag", version)
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/")]
                with sqlite_session(path) as db:
                    db.row_factory = sqlite3.Row
                    if len(parts) == 3 and parts[:2] == ["orders", "by-key"]:
                        row = db.execute("SELECT * FROM orders WHERE idempotency_key=?", (parts[2],)).fetchone()
                        if row:
                            return self.respond({"state": "committed", "external_id": row["id"],
                                                 "request_digest": row["request_digest"],
                                                 "idempotency_key": row["idempotency_key"]})
                        return self.respond({"error": "not_found"}, 404)
                    if len(parts) != 2:
                        return self.respond({"error": "not_found"}, 404)
                    table, key = parts
                    if table == "requests":
                        row = db.execute("SELECT * FROM requests WHERE id=?", (key,)).fetchone()
                        if row:
                            record = {k: row[k] for k in ("id", "part", "quantity", "site_id", "cost_center", "state")}
                            return self.respond({"request": record}, version=row["version"])
                    elif table == "sites":
                        row = db.execute("SELECT * FROM sites WHERE id=?", (key,)).fetchone()
                        if row:
                            return self.respond({"site": {k: row[k] for k in ("id", "address", "state")}}, version=row["version"])
                    elif table == "budgets":
                        row = db.execute("SELECT * FROM budgets WHERE id=?", (key,)).fetchone()
                        if row:
                            return self.respond({"budget": {k: row[k] for k in ("id", "available", "currency")}}, version=f'budget:{row["version"]}')
                    elif table == "quotes":
                        rows = db.execute("SELECT * FROM quotes WHERE request_id=? ORDER BY id", (key,)).fetchall()
                        selection = db.execute("SELECT quote_id,version FROM quote_selections WHERE request_id=?", (key,)).fetchone()
                        if len(rows) > 1 and selection and not selection["quote_id"]:
                            return self.respond({"options": [row["id"] for row in rows], "selection": None}, version="selection:1")
                        if rows:
                            row = next((row for row in rows if selection and row["id"] == selection["quote_id"]), rows[0])
                            quote = {k: row[k] for k in ("id", "request_id", "vendor", "unit_price", "total", "currency")}
                            return self.respond({"quote": quote}, version=row["version"])
                    elif table == "site-options":
                        request = db.execute("SELECT id FROM requests WHERE id=?", (key,)).fetchone()
                        if request:
                            ids = [row["id"] for row in db.execute("SELECT id FROM sites WHERE state='active' ORDER BY id DESC")]
                            return self.respond({"options": {"request_id": key, "ids": ids}}, version="site-options:1")
                    elif table == "quote-options":
                        selection = db.execute("SELECT quote_id,version FROM quote_selections WHERE request_id=?", (key,)).fetchone()
                        if selection:
                            ids = [row["id"] for row in db.execute("SELECT id FROM quotes WHERE request_id=? ORDER BY id", (key,))]
                            return self.respond({"options": {"request_id": key, "ids": ids, "selection": selection["quote_id"]}}, version=f'selection:{selection["version"]}')
                self.respond({"error": "unknown_record"}, 404)

            def do_POST(self):
                parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/")]
                if len(parts) == 3 and parts[2] in ("select-site", "select-quote"):
                    role = "logistics" if parts[2] == "select-site" else "procurement"
                    if self.headers.get("Authorization") != "Bearer " + fixture.tokens[role]:
                        return self.respond({"error": "forbidden"}, 403)
                    try:
                        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    except (ValueError, KeyError):
                        return self.respond({"error": "invalid_body"}, 400)
                    with sqlite_session(path, 10) as db:
                        db.row_factory = sqlite3.Row
                        db.execute("BEGIN IMMEDIATE")
                        key = parts[1]
                        if parts[0] == "requests" and role == "logistics":
                            request = db.execute("SELECT site_id,version FROM requests WHERE id=?", (key,)).fetchone()
                            site = db.execute("SELECT id FROM sites WHERE id=? AND state='active'", (data.get("site_id"),)).fetchone()
                            if not request or request["site_id"] or not site or request["version"] != self.headers.get("If-Match"):
                                return self.respond({"error": "invalid_or_stale_site_choice"}, 412)
                            new_version = f'req:{int(request["version"].split(":")[1]) + 1}'
                            db.execute("UPDATE requests SET site_id=?, version=? WHERE id=?", (site["id"], new_version, key))
                            selected, old_version = site["id"], request["version"]
                        elif parts[0] == "quotes" and role == "procurement":
                            selection = db.execute("SELECT quote_id,version FROM quote_selections WHERE request_id=?", (key,)).fetchone()
                            quote = db.execute("SELECT id FROM quotes WHERE id=? AND request_id=?", (data.get("quote_id"), key)).fetchone()
                            if not selection or selection["quote_id"] or not quote or f'selection:{selection["version"]}' != self.headers.get("If-Match"):
                                return self.respond({"error": "invalid_or_stale_quote_choice"}, 412)
                            new_version = f'selection:{selection["version"] + 1}'
                            db.execute("UPDATE quote_selections SET quote_id=?,version=version+1 WHERE request_id=?", (quote["id"], key))
                            selected, old_version = quote["id"], f'selection:{selection["version"]}'
                        else:
                            return self.respond({"error": "not_found"}, 404)
                        db.execute("INSERT INTO update_audit VALUES (?,?,?,?,?,?)",
                                   (role, parts[0], key, selected, old_version, new_version))
                    return self.respond({"selected": selected, "version": new_version})
                if urlsplit(self.path).path != "/orders":
                    return self.respond({"error": "not_found"}, 404)
                if fixture.drop_before_write:
                    fixture.drop_before_write = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                key = self.headers.get("Idempotency-Key")
                version = self.headers.get("If-Match")
                if not key or not version:
                    return self.respond({"error": "precondition_required"}, 428)
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                except (ValueError, KeyError):
                    return self.respond({"error": "invalid_body"}, 400)
                with sqlite_session(path, 10) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("SELECT id FROM orders WHERE idempotency_key=?", (key,)).fetchone()
                    if old:
                        return self.respond({"id": old["id"], "replayed": True})
                    request = db.execute("SELECT * FROM requests WHERE id=?", (body.get("request", {}).get("id"),)).fetchone()
                    site = db.execute("SELECT * FROM sites WHERE id=?", (body.get("site", {}).get("id"),)).fetchone()
                    quote = db.execute("SELECT * FROM quotes WHERE id=?", (body.get("quote", {}).get("id"),)).fetchone()
                    selection = db.execute("SELECT quote_id FROM quote_selections WHERE request_id=?", (request["id"],)).fetchone() if request else None
                    budget = db.execute("SELECT * FROM budgets WHERE id=?", (request["cost_center"],)).fetchone() if request else None
                    total = body.get("amount")
                    valid = (request and site and quote and budget and selection and
                             selection["quote_id"] == quote["id"] and request["state"] == "approved"
                             and request["site_id"] == site["id"] and site["state"] == "active"
                             and body["request"]["part"] == request["part"]
                             and body["request"]["quantity"] == request["quantity"]
                             and body["request"]["cost_center"] == budget["id"]
                             and quote["request_id"] == request["id"] and quote["version"] == version
                             and body["quote"]["total"] == quote["total"]
                             and body["quote"]["currency"] == quote["currency"] == budget["currency"]
                             and quote["unit_price"] * request["quantity"] == total == quote["total"]
                             and 0 <= total <= budget["available"])
                    if not valid:
                        return self.respond({"error": "stale_or_inconsistent_order"}, 412)
                    order_id = f'PO-{db.execute("SELECT count(*) FROM orders").fetchone()[0] + 9001}'
                    db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
                               (order_id, request["id"], quote["id"], site["id"], total, key, digest(body)))
                    db.execute("UPDATE budgets SET available=available-?, version=version+1 WHERE id=?", (total, budget["id"]))
                if fixture.drop_after_write:
                    fixture.drop_after_write = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.respond({"id": order_id, "replayed": False}, 201)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def change_quote_version(self, version: str):
        with sqlite_session(self.path) as db:
            db.execute("UPDATE quotes SET version=? WHERE id='Q-881'", (version,))

    def order_for_key(self, key: str):
        try:
            with urllib.request.urlopen(f"{self.base}/orders/by-key/{key}", timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def order_count(self):
        with sqlite_session(self.path) as db:
            return db.execute("SELECT count(*) FROM orders").fetchone()[0]

    def select_site(self, role: str, request_id: str, site_id: str):
        with sqlite_session(self.path) as db:
            version = db.execute("SELECT version FROM requests WHERE id=?", (request_id,)).fetchone()[0]
        return self._select(role, f"/requests/{request_id}/select-site", {"site_id": site_id}, version)

    def select_quote(self, role: str, request_id: str, quote_id: str):
        with sqlite_session(self.path) as db:
            version = db.execute("SELECT version FROM quote_selections WHERE request_id=?", (request_id,)).fetchone()[0]
        return self._select(role, f"/quotes/{request_id}/select-quote", {"quote_id": quote_id}, f"selection:{version}")

    def _select(self, role: str, path: str, body: dict, version: str):
        headers = {"Content-Type": "application/json", "If-Match": version,
                   "Authorization": "Bearer " + self.tokens.get(role, "invalid")}
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise Rejected("source_update_denied", f"source returned {error.code}") from error

    def snapshot(self):
        with sqlite_session(self.path) as db:
            db.row_factory = sqlite3.Row
            return {
                "orders": [dict(row) for row in db.execute("SELECT * FROM orders ORDER BY id")],
                "budget": dict(db.execute("SELECT * FROM budgets WHERE id='CC-742'").fetchone()),
                "updates": [dict(row) for row in db.execute("SELECT * FROM update_audit ORDER BY rowid")],
            }


def configure(work: Work, erp: SimulatedERP):
    work.install_contract(CONTRACT)
    work.install_policy(POLICY)
    work.install_action("record_review", {"kind": "local"})
    work.install_action("issue_order", {"kind": "http", "url": erp.base + "/orders",
                                        "status_url": erp.base + "/orders/by-key/{key}",
                                        "no_write_statuses": [412], "version_arg": "quote_version"})
    for name, path, value in (("requisitions", "requests", "request"),
                              ("sites", "sites", "site"),
                              ("supplier", "quotes", "quote"),
                              ("finance", "budgets", "budget"),
                              ("site_options", "site-options", "options"),
                              ("quote_options", "quote-options", "options")):
        allowed = ["operator", "logistics"] if name == "requisitions" else ["operator", "procurement"] if name in ("supplier", "quote_options") else ["operator"]
        work.install_source(name, {"kind": "http", "url": f"{erp.base}/{path}/{{key}}", "value_path": [value], "roles": allowed})


def run_scenario(scenario_id: str, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    erp = SimulatedERP(folder / "erp.sqlite3")
    work = Work(folder / "work.sqlite3")
    configure(work, erp)
    result = {"id": scenario_id, "case_id": None, "trace": []}

    def step(action, **detail):
        result["trace"].append({"action": action, **detail})

    try:
        if scenario_id == "missing_input":
            try:
                work.create_case(CONTRACT["id"], 1, {}, "a.rivera")
            except Rejected as error:
                step("submit_incomplete_requisition", result=error.code)
                result["outcome"] = "input_rejected"
            else:
                raise AssertionError("Missing input was accepted")
        else:
            request_id = "REQ-4813" if scenario_id == "missing_site" else "REQ-4814" if scenario_id in ("ambiguous_quote", "quote_cancelled") else "REQ-4812"
            case_id = work.create_case(CONTRACT["id"], 1, {"request_id": request_id}, "a.rivera")
            result["case_id"] = case_id
            step("create_case", requisition=request_id)
            request = work.refresh_fact(case_id, "requisitions", "request", request_id, "a.rivera", ["operator"])["body"]["value"]
            step("read_requisition", version="req:3" if scenario_id == "missing_site" else "req:4" if scenario_id in ("ambiguous_quote", "quote_cancelled") else "req:6", state=request["state"])

            if scenario_id == "missing_site":
                work.refresh_fact(case_id, "site_options", "site_options", request_id, "a.rivera", ["operator"])
                task = work.request_resolution(case_id, "site", "a.rivera", ["operator"])
                step("request_site_resolution", task_id=task["body"]["id"], owner_role="logistics",
                     due_at=task["body"]["due_at"], choices=task["body"]["choices"])
                proposal = work.propose(case_id, "record_review", {"request_id": request_id, "site_id": "DC-WEST-4"}, "a.rivera", ["operator"])
                step("propose_review_with_guessed_site", decision=proposal["decision"])
                pending = work.resolve_resolution(case_id, task["body"]["id"], "l.chen", ["logistics"])
                step("check_site_before_source_change", result=pending["status"])
                source_change = erp.select_site("logistics", request_id, "DC-WEST-4")
                step("logistics_selects_site_in_erp", version=source_change["version"])
                resolved = work.resolve_resolution(case_id, task["body"]["id"], "l.chen", ["logistics"])
                assert resolved["status"] == "completed"
                step("verify_site_resolution", choice=resolved["choice"])
                work.refresh_fact(case_id, "sites", "site", "DC-WEST-4", "a.rivera", ["operator"])
                work.refresh_fact(case_id, "finance", "budget", request["cost_center"], "a.rivera", ["operator"])
                work.refresh_fact(case_id, "supplier", "quote", request_id, "a.rivera", ["operator"])
                review = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                assert work.commit(case_id, review["proposal"]["id"], "scheduler")["status"] == "succeeded"
                order = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                work.approve(case_id, order["proposal"]["id"], "d.patel", ["manager"])
                committed = work.commit(case_id, order["proposal"]["id"], "scheduler")
                assert committed["status"] == "succeeded"
                work.signoff(case_id, "d.patel", ["manager"], "manager")
                step("place_order_after_site_resolution", result=committed["status"])
                result["outcome"] = "resolved_site"
            else:
                work.refresh_fact(case_id, "sites", "site", request["site_id"], "a.rivera", ["operator"])
                work.refresh_fact(case_id, "finance", "budget", request["cost_center"], "a.rivera", ["operator"])
                if scenario_id in ("ambiguous_quote", "quote_cancelled"):
                    work.refresh_fact(case_id, "quote_options", "quote_options", request_id, "a.rivera", ["operator"])
                    task = work.request_resolution(case_id, "quote", "a.rivera", ["operator"])
                    step("request_quote_resolution", task_id=task["body"]["id"], owner_role="procurement",
                         due_at=task["body"]["due_at"], choices=task["body"]["choices"])
                    try:
                        work.refresh_fact(case_id, "supplier", "quote", request_id, "a.rivera", ["operator"])
                    except Rejected as error:
                        step("read_competing_quotes", candidate_ids=["Q-882A", "Q-882B"], result=error.code)
                    else:
                        raise AssertionError("Unselected quote was treated as one quote")
                else:
                    work.refresh_fact(case_id, "supplier", "quote", request_id, "a.rivera", ["operator"])
                    step("read_verified_records", sources=["sites", "finance", "supplier"], quote_version="quote:3")

                review = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                assert work.commit(case_id, review["proposal"]["id"], "scheduler")["status"] == "succeeded"
                step("record_review", event="effect_succeeded")

                if scenario_id in ("ambiguous_quote", "quote_cancelled"):
                    proposed = work.propose(case_id, "issue_order", {"amount": 2500}, "a.rivera", ["operator"])
                    step("propose_without_selected_quote", decision=proposed["decision"])
                    if scenario_id == "quote_cancelled":
                        work.cancel_case(case_id, "Supplier qualification unresolved", "d.patel", ["manager"])
                        step("manager_cancels_unresolved_case", reason="Supplier qualification unresolved")
                        result["outcome"] = "cancelled"
                    else:
                        pending = work.resolve_resolution(case_id, task["body"]["id"], "p.soto", ["procurement"])
                        step("check_quote_before_source_change", result=pending["status"])
                        source_change = erp.select_quote("procurement", request_id, "Q-882A")
                        step("procurement_selects_quote_in_erp", version=source_change["version"])
                        resolved = work.resolve_resolution(case_id, task["body"]["id"], "p.soto", ["procurement"])
                        assert resolved["status"] == "completed"
                        step("verify_quote_resolution", choice=resolved["choice"])
                        order = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                        work.approve(case_id, order["proposal"]["id"], "d.patel", ["manager"])
                        committed = work.commit(case_id, order["proposal"]["id"], "scheduler")
                        assert committed["status"] == "succeeded"
                        work.signoff(case_id, "d.patel", ["manager"], "manager")
                        step("place_order_after_quote_resolution", result=committed["status"])
                        result["outcome"] = "resolved_quote"
                else:
                    order = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                    key = order["proposal"]["id"]
                    step("propose_order", decision=order["decision"], amount=order["proposal"]["args"]["amount"])
                    if scenario_id == "complete":
                        before = work.commit(case_id, key, "scheduler")
                        step("attempt_without_approval", decision=before["decision"])
                    work.approve(case_id, key, "d.patel", ["manager"])
                    step("independent_approval", role="manager")

                    if scenario_id == "policy_tightened":
                        work.install_policy({"version": 2, "actions": {"record_review": POLICY["actions"]["record_review"]}})
                        denied = work.commit(case_id, key, "scheduler")
                        step("commit_after_policy_change", decision=denied["decision"])
                        replay = work.replay(case_id, 1, 2)
                        step("amendment_replay", changed_decisions=len(replay["changed_decisions"]))
                        result["outcome"] = "blocked_at_commit"
                    elif scenario_id == "stale_quote":
                        erp.change_quote_version("quote:4")
                        rejected = work.commit(case_id, key, "scheduler")
                        step("submit_with_stale_quote", result=rejected["status"], http_status=rejected["event"]["body"]["status"])
                        assert erp.order_for_key(key) is None
                        work.refresh_fact(case_id, "supplier", "quote", request_id, "a.rivera", ["operator"])
                        newer = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                        work.approve(case_id, newer["proposal"]["id"], "d.patel", ["manager"])
                        committed = work.commit(case_id, newer["proposal"]["id"], "scheduler")
                        step("submit_after_refresh", result=committed["status"], quote_version="quote:4")
                        work.signoff(case_id, "d.patel", ["manager"], "manager")
                        result["outcome"] = "recovered_after_refresh"
                    elif scenario_id == "lost_ack":
                        erp.drop_after_write = True
                        unknown = work.commit(case_id, key, "scheduler")
                        step("submit_response_lost", result=unknown["status"])
                        verified = work.reconcile(case_id, key, "d.patel", ["manager"])
                        assert verified["status"] == "verified"
                        step("reconcile_verified_write", external_order=verified["event"]["body"]["proof"]["external_id"])
                        work.signoff(case_id, "d.patel", ["manager"], "manager")
                        result["outcome"] = "reconciled_complete"
                    elif scenario_id == "forged_claim":
                        erp.drop_before_write = True
                        unknown = work.commit(case_id, key, "scheduler")
                        step("submit_without_destination_write", result=unknown["status"])
                        try:
                            work.reconcile(case_id, key, "d.patel", ["manager"], success=True, evidence="PO-FAKE")
                        except Rejected as error:
                            step("submit_fabricated_reference", result=error.code)
                        else:
                            raise AssertionError("Unverified manager claim was accepted")
                        pending = work.reconcile(case_id, key, "d.patel", ["manager"])
                        step("check_destination", result=pending["status"], reason=pending["reason"])
                        again = work.compiled_propose(case_id, "scheduler", ["operator", "compiled"])
                        step("attempt_another_order", decision=again["decision"])
                        result["outcome"] = "blocked_unproven_claim"
                    elif scenario_id == "mismatched_status":
                        erp.drop_after_write = True
                        unknown = work.commit(case_id, key, "scheduler")
                        step("submit_response_lost", result=unknown["status"])
                        with sqlite_session(erp.path) as db:
                            db.execute("UPDATE orders SET request_digest=? WHERE idempotency_key=?", ("0" * 64, key))
                        check = work.reconcile(case_id, key, "d.patel", ["manager"])
                        step("check_mismatched_destination_record", result=check["status"], reason=check["reason"])
                        work.signoff(case_id, "d.patel", ["manager"], "manager")
                        result["outcome"] = "blocked_mismatched_record"
                    else:
                        committed = work.commit(case_id, key, "scheduler")
                        step("submit_order", result=committed["status"])
                        work.signoff(case_id, "d.patel", ["manager"], "manager")
                        result["outcome"] = "complete"

        result["external_orders"] = erp.order_count()
        result["external_state"] = erp.snapshot()
        if result["case_id"]:
            state = work.inspect(result["case_id"])
            result["status"] = state["status"]
            result["resolutions"] = state["resolutions"]
            result["acceptance_complete"] = state["complete"]
            result["acceptance"] = state["acceptance"]
            result["event_count"] = len(state["events"])
            result["event_kinds"] = [event["kind"] for event in state["events"]]
            result["case_events"] = state["events"]
            result["chain_head"] = state["events"][-1]["hash"]
            result["chain_valid"] = work.verify_chain(result["case_id"])
        else:
            result.update(status=None, resolutions=[], acceptance_complete=False, acceptance=[], event_count=0,
                          event_kinds=[], case_events=[], chain_head=None, chain_valid=None)
        return result
    finally:
        erp.close()
        work.close()


def run_study(directory: Path) -> dict:
    """Execute ten separate cases, each with its own application and ERP databases."""
    return {
        "organization": "Northstar Medical Devices (fictional)",
        "workflow": "Emergency packaging-line spare-part purchase order",
        "execution": "Local HTTP plus two isolated SQLite databases per case; no actual enterprise system",
        "scenarios": [run_scenario(sid, directory / sid) for sid in (
            "complete", "missing_input", "missing_site", "ambiguous_quote",
            "policy_tightened", "stale_quote", "lost_ack", "forged_claim", "mismatched_status", "quote_cancelled")],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the observed result as JSON")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as root:
        report = run_study(Path(root))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
