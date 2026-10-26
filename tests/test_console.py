"""The operator console: its read-only routes, its static files, its projections, and a real browser run.

The browser run walks the included purchase-order path against ``examples/reference_system.py`` on a
free port: refresh, compiled proposals, independent manager approval, execution, signoff, verify,
publishing contract v2 in the studio and replaying the case against it. It skips when node or the node
``playwright`` package is not installed, like ``tests/test_inspector.py``.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from syberlabs.admission import approval_roles
from syberlabs.evidence import verified_reconciliation
from syberlabs.evolution import AUTOMATION_ROLES
from syberwork.core import Work
from syberwork.server import POLICY_READERS, make_server

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "syberwork" / "examples"
STATIC = ROOT / "syberwork" / "static"
MODEL_TESTS = Path(__file__).with_name("console_model.test.mjs")
BROWSER_CHECK = Path(__file__).with_name("console_browser.mjs")
IDENTITIES = {
    "admin": (["admin", "operator"], []), "operator": (["operator"], []), "planner": (["operator", "model"], []),
    "scheduler": (["operator", "compiled"], []), "manager": (["manager"], []), "supplier": (["observer"], ["supplier"]),
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def playwright_root() -> str | None:
    """A node_modules holding playwright: NODE_PATH when it has one, else the global one, else None."""
    if not shutil.which("node"):
        return None
    for entry in filter(None, os.environ.get("NODE_PATH", "").split(os.pathsep)):
        if (Path(entry) / "playwright").exists():
            return entry
    if not shutil.which("npm"):
        return None
    try:
        root = subprocess.run([shutil.which("npm"), "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return root if root and (Path(root) / "playwright").exists() else None


class Cell:
    """The included example installed as ``init`` installs it, with sources and actions on ``reference_port``."""

    def __init__(self, folder: Path, reference_port: int = 8999):
        self.database = folder / "work.sqlite3"
        self.work = Work(self.database)
        self.tokens = {name: "token-" + name for name in IDENTITIES}
        users = {name: {"hash": hashlib.sha256(self.tokens[name].encode()).hexdigest(), "roles": roles, "sources": sources}
                 for name, (roles, sources) in IDENTITIES.items()}
        retarget = lambda doc: json.loads(json.dumps(doc).replace("127.0.0.1:8999", f"127.0.0.1:{reference_port}"))
        self.work.install_policy(json.loads((EXAMPLES / "policy.json").read_text()))
        self.work.install_contract(json.loads((EXAMPLES / "contract.json").read_text()))
        for name, doc in json.loads((EXAMPLES / "actions.json").read_text()).items():
            self.work.install_action(name, retarget(doc))
        for name, doc in json.loads((EXAMPLES / "sources.json").read_text()).items():
            self.work.install_source(name, retarget(doc))
        self.server = make_server(self.work, users, port=0)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, path, data=None, who="operator"):
        headers = {"Authorization": "Bearer " + self.tokens[who]} if who else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                         headers=headers, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, path, data=None, who="operator"):
        status, _, body = self.call(path, data, who)
        return status, json.loads(body)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.work.close()


class CellCase(unittest.TestCase):
    reference = False

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        port = 8999
        if self.reference:
            port = free_port()
            spec = importlib.util.spec_from_file_location("reference_system", ROOT / "examples" / "reference_system.py")
            reference = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reference)
            threading.Thread(target=reference.run, args=(Path(folder.name) / "reference.sqlite3", port), daemon=True).start()
        self.cell = Cell(Path(folder.name), port)
        self.addCleanup(self.cell.close)


class ConsoleRoutes(CellCase):
    def test_every_new_route_requires_a_bearer_token(self):
        for path in ("/api/console", "/api/contracts", "/api/contracts/purchase-order/1", "/api/policies", "/api/actions", "/api/sources"):
            with self.subTest(path):
                status, body = self.cell.json(path, who=None)
                self.assertEqual((status, body["error"]), (401, "unauthorized"))

    def test_console_reports_planner_configuration_without_disclosing_it(self):
        url = "http://127.0.0.1:9/plan?secret=abc"
        previous = os.environ.pop("SYBERWORK_PLANNER_URL", None)
        self.addCleanup(lambda: os.environ.__setitem__("SYBERWORK_PLANNER_URL", previous) if previous is not None else os.environ.pop("SYBERWORK_PLANNER_URL", None))
        status, body = self.cell.json("/api/console")
        self.assertEqual((status, body), (200, {"planner_configured": False, "effects": "inline"}))
        os.environ["SYBERWORK_PLANNER_URL"] = url
        status, raw = self.cell.call("/api/console")[0], self.cell.call("/api/console")[2]
        self.assertEqual(json.loads(raw)["planner_configured"], True)
        self.assertNotIn(b"127.0.0.1:9", raw)
        self.assertNotIn(b"secret", raw)
        os.environ["SYBERWORK_PLANNER_URL"] = "http://planner.example.com/plan"
        self.assertFalse(self.cell.json("/api/console")[1]["planner_configured"], "plain HTTP off loopback is not a usable planner")

    def test_published_documents_are_listed_and_read_back_unchanged(self):
        contract = json.loads((EXAMPLES / "contract.json").read_text())
        status, listing = self.cell.json("/api/contracts")
        self.assertEqual(status, 200)
        self.assertEqual([(c["id"], c["version"], c["title"]) for c in listing], [("purchase-order", 1, contract["title"])])
        status, body = self.cell.json("/api/contracts/purchase-order/1")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.cell.work.contract("purchase-order", 1))
        self.assertEqual(body["compiled_path"], ["record_review", "issue_order"])
        bumped = dict(contract, version=2)
        bumped["actions"] = json.loads(json.dumps(contract["actions"]))
        bumped["actions"]["issue_order"]["max_amount"] = 100
        self.assertEqual(self.cell.json("/api/contracts", bumped, who="admin"), (200, {"installed": True}))
        self.assertEqual([c["version"] for c in self.cell.json("/api/contracts")[1]], [1, 2])
        self.assertEqual(self.cell.json("/api/contracts/purchase-order/1")[1], body, "version 1 is unchanged")
        status, policies = self.cell.json("/api/policies", who="manager")
        self.assertEqual((status, [p["version"] for p in policies]), (200, [1]))
        self.assertEqual(policies[0]["body"], json.loads((EXAMPLES / "policy.json").read_text()))

    def test_unknown_or_malformed_contract_reads_are_refused_not_crashed(self):
        self.assertEqual(self.cell.json("/api/contracts/purchase-order/9")[1]["error"], "unknown_contract")
        for path in ("/api/contracts/purchase-order/abc", "/api/contracts/purchase-order/-1", "/api/contracts/purchase-order/abc/artifacts"):
            with self.subTest(path):
                status, body = self.cell.json(path)
                self.assertEqual((status, body["error"]), (404, "not_found"))
        status, artifacts = self.cell.json("/api/contracts/purchase-order/1/artifacts")
        self.assertEqual((status, artifacts["path"]), (200, ["record_review", "issue_order"]))

    def test_connector_definitions_are_for_administrators_and_hold_no_secret(self):
        self.cell.work.install_source("crm", {"kind": "http", "url": "http://127.0.0.1:9/crm/{key}", "auth_env": "CRM_TOKEN"})
        os.environ["CRM_TOKEN"] = "never-served"
        self.addCleanup(os.environ.pop, "CRM_TOKEN", None)
        self.assertEqual(self.cell.json("/api/actions"), (403, {"error": "forbidden", "detail": "admin role required"}))
        for path in ("/api/actions", "/api/sources"):
            with self.subTest(path):
                status, raw = self.cell.call(path, who="admin")[0], self.cell.call(path, who="admin")[2]
                self.assertEqual(status, 200)
                self.assertNotIn(b"never-served", raw)
        sources = self.cell.json("/api/sources", who="admin")[1]
        self.assertEqual(sorted(sources), ["crm", "inventory", "supplier"])
        self.assertEqual(sources["crm"]["auth_env"], "CRM_TOKEN")
        self.assertEqual(sorted(self.cell.json("/api/actions", who="admin")[1]), ["issue_order", "record_review"])

    def test_reads_append_nothing(self):
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        before = self.cell.work.inspect(case)["events"]
        for path in ("/api/console", "/api/contracts", "/api/contracts/purchase-order/1", "/api/policies", "/api/sources",
                     "/metrics/work_items", f"/metrics/work_items/{case}"):
            self.cell.call(path)
        for path in ("/api/actions", "/api/sources", "/api/policies"):
            self.cell.call(path, who="admin")
        self.assertEqual(self.cell.work.inspect(case)["events"], before)
        self.assertTrue(self.cell.work.verify_chain(case))


class ConsoleReadScope(CellCase):
    """Who may read policy bodies and source read roles. An economic policy carries budget limits."""

    def test_policy_bodies_are_for_admin_and_manager_only(self):
        self.assertEqual(sorted(POLICY_READERS), ["admin", "manager"])
        for who in ("admin", "manager"):
            with self.subTest(who):
                status, policies = self.cell.json("/api/policies", who=who)
                self.assertEqual((status, [p["version"] for p in policies]), (200, [1]))
        # operator; operator with model; operator with compiled; an observer.
        for who in ("operator", "planner", "scheduler", "supplier"):
            with self.subTest(who):
                status, _, raw = self.cell.call("/api/policies", who=who)
                self.assertEqual((status, json.loads(raw)["error"]), (403, "forbidden"))
                self.assertNotIn(b"max_amount", raw)
                self.assertNotIn(b"25000", raw)

    def test_a_model_credential_cannot_read_policies_even_with_a_reader_role(self):
        work = self.cell.work
        work.register_principal("agent-model", "agent", "model-agent", "local", ["model"], token="token-model-agent")
        work.register_principal("agent-manager", "agent", "manager-agent", "local", ["manager", "model"], token="token-manager-agent")
        work.register_principal("human-manager", "human", "manager-person", "local", ["manager"], token="token-manager-person")
        self.cell.tokens.update({"model-agent": "token-model-agent", "manager-agent": "token-manager-agent", "manager-person": "token-manager-person"})
        for who in ("model-agent", "manager-agent"):
            with self.subTest(who):
                self.assertEqual(self.cell.json("/api/policies", who=who)[0], 403)
        self.assertEqual(self.cell.json("/api/policies", who="manager-person")[0], 200, "a registered principal is checked by the same rule")
        for role in AUTOMATION_ROLES:
            with self.subTest(role):
                work.register_principal("admin-" + role, "service", "admin-" + role, "local", ["admin", role], token="token-admin-" + role)
                self.cell.tokens["admin-" + role] = "token-admin-" + role
                self.assertEqual(self.cell.json("/api/policies", who="admin-" + role)[0], 403)

    def test_other_credentials_see_each_sources_read_roles_and_nothing_else(self):
        self.cell.work.install_source("crm", {"kind": "http", "url": "http://127.0.0.1:9/crm/{key}", "auth_env": "CRM_TOKEN"})
        self.cell.work.install_source("ledger", {"kind": "http", "url": "http://127.0.0.1:9/ledger/{key}", "roles": ["logistics"],
                                                 "value_path": ["balance"], "timeout_seconds": 3})
        for who in ("operator", "scheduler", "planner", "manager", "supplier"):
            with self.subTest(who):
                status, _, raw = self.cell.call("/api/sources", who=who)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"crm": {"roles": ["operator"]}, "inventory": {"roles": ["operator"]},
                                                   "ledger": {"roles": ["logistics"]}, "supplier": {"roles": ["operator"]}})
                for withheld in (b"127.0.0.1", b"CRM_TOKEN", b"auth_env", b"balance", b"url", b"kind"):
                    self.assertNotIn(withheld, raw)
        self.assertEqual(self.cell.json("/api/sources", who=None)[0], 401)

    def test_listed_read_roles_are_the_ones_refresh_enforces(self):
        """The roles /api/sources lists are what refresh_fact checks: a credential without one is refused."""
        self.cell.work.install_source("ledger", {"kind": "http", "url": "http://127.0.0.1:9/ledger/{key}", "roles": ["logistics"]})
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        roles = self.cell.json("/api/sources")[1]["ledger"]["roles"]
        self.assertNotIn("operator", roles)
        status, body = self.cell.json(f"/api/cases/{case}/refresh", {"source": "ledger", "key": "balance", "record_key": "x"})
        self.assertEqual((status, body["error"]), (409, "source_denied"))

    def test_work_item_summary_carries_what_the_top_bar_reads(self):
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        for who in ("operator", "manager", "supplier"):
            with self.subTest(who):
                status, body = self.cell.json("/metrics/work_items", who=who)
                self.assertEqual(status, 200)
                summary = body["summary"]
                self.assertEqual((summary["items"], summary["opened"], summary["cancelled"]), (1, 0, 0))
                self.assertEqual(set(summary["delivered"]), {"yes", "no", "unknown"})
                self.assertIn("cost_basis", summary)
        self.assertEqual([item["thread"] for item in self.cell.json(f"/metrics/work_items/{case}")[1]["items"]], [case])
        self.assertEqual(self.cell.json("/metrics/work_items/no-such-case")[0], 404)
        self.assertEqual(self.cell.json("/metrics/work_items", who=None)[0], 401)
        app = (STATIC / "app.js").read_text()
        self.assertIn("api('/metrics/work_items')", app)
        for field in ("w.items", "w.opened", "w.delivered.yes", "w.delivered.unknown", "w.cost_basis", "w.cancelled"):
            self.assertIn(field, app)


class ConsoleExplain(CellCase):
    """GET /api/cases/{id}/proposals/{p}/explain: Work.explain_admission over HTTP, read only."""

    def proposal(self, action="issue_order", args=None, who="operator"):
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        status, made = self.cell.json(f"/api/cases/{case}/proposals", {"action": action, "args": args or {}}, who=who)
        self.assertEqual(status, 200, made)
        return case, made

    def dump(self):
        with sqlite3.connect(self.cell.database) as db:
            return list(db.iterdump())

    def test_explain_requires_a_bearer_token(self):
        case, made = self.proposal()
        for when in ("", "?when=recorded", "?when=now"):
            with self.subTest(when):
                status, body = self.cell.json(f"/api/cases/{case}/proposals/{made['proposal']['id']}/explain{when}", who=None)
                self.assertEqual((status, body["error"]), (401, "unauthorized"))

    def test_explain_returns_the_rule_written_with_the_decision(self):
        case, made = self.proposal()
        pid = made["proposal"]["id"]
        self.assertEqual(made["decision"], {"status": "denied", "reason": "amount_required"})
        status, recorded = self.cell.json(f"/api/cases/{case}/proposals/{pid}/explain?when=recorded", who="manager")
        self.assertEqual(status, 200)
        self.assertEqual({k: recorded[k] for k in ("proposal_id", "when", "status", "reason", "rule")},
                         {"proposal_id": pid, "when": "recorded", "status": "denied", "reason": "amount_required", "rule": "limits.amount"})
        self.assertEqual(recorded["provenance"]["module"], "syberlabs/admission.py")
        self.assertEqual(recorded["provenance"]["symbol"], "limits_amount")
        self.assertIsInstance(recorded["provenance"]["line"], int)
        side = {row["seq"]: row["rule"] for row in self.cell.work.side_channel(case)}
        self.assertEqual(recorded["recorded"], {"seq": recorded["recorded"]["seq"], "status": "denied", "reason": "amount_required",
                                                "phase": "propose", "rule": "limits.amount"})
        self.assertEqual(side[recorded["recorded"]["seq"]], "limits.amount", "the rule written beside the decision event")
        self.assertEqual(self.cell.json(f"/api/cases/{case}/proposals/{pid}/explain")[1]["when"], "now", "now is the default, as in Work")
        self.assertEqual({k: v for k, v in recorded.items() if k != "when"},
                         {k: v for k, v in self.cell.json(f"/api/cases/{case}/proposals/{pid}/explain?when=now")[1].items() if k != "when"})

    def test_explain_now_and_recorded_differ_once_history_moves(self):
        """An approved issue_order: recorded says approval was required; now says commit would be admitted."""
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        work = self.cell.work
        work.observe(case, "part_number", "P-104", "inventory", "inventory-v1", "operator", verified=True)
        work.observe(case, "quote", {"id": "Q-7", "price": 10, "part_number": "P-104", "supplier": "s"}, "supplier", "supplier-v1", "operator", verified=True)
        status, made = self.cell.json(f"/api/cases/{case}/compiled", {}, who="scheduler")
        self.assertEqual((status, made["proposal"]["action"]), (200, "record_review"), made)
        self.cell.json(f"/api/cases/{case}/commit", {"proposal_id": made["proposal"]["id"]}, who="scheduler")
        made = self.cell.json(f"/api/cases/{case}/compiled", {}, who="scheduler")[1]
        self.assertEqual(made["decision"]["status"], "needs_approval", made)
        pid = made["proposal"]["id"]
        path = f"/api/cases/{case}/proposals/{pid}/explain"
        self.assertEqual(self.cell.json(path + "?when=recorded")[1]["rule"], "approval.required")
        self.assertEqual(self.cell.json(path + "?when=now")[1]["rule"], "approval.required")
        self.assertEqual(self.cell.json(f"/api/cases/{case}/approve", {"proposal_id": pid}, who="manager")[0], 200)
        recorded, now = self.cell.json(path + "?when=recorded")[1], self.cell.json(path + "?when=now")[1]
        self.assertEqual((recorded["rule"], recorded["status"], recorded["recorded"]["rule"]), ("approval.required", "needs_approval", "approval.required"))
        self.assertEqual(now["status"], "allowed")
        self.assertEqual(now["rule"], "admission.passed")

    def test_explain_appends_nothing_and_writes_no_row(self):
        case, made = self.proposal()
        pid = made["proposal"]["id"]
        before, events = self.dump(), self.cell.work.inspect(case)["events"]
        for path in (f"/api/cases/{case}/proposals/{pid}/explain", f"/api/cases/{case}/proposals/{pid}/explain?when=recorded",
                     f"/api/cases/{case}/proposals/{pid}/explain?when=now", f"/api/cases/{case}/proposals/missing/explain",
                     f"/api/cases/{case}/proposals/{pid}/explain?when=later"):
            for who in ("operator", "manager", "planner", "supplier", None):
                self.cell.call(path, who=who)
        self.assertEqual(self.cell.work.inspect(case)["events"], events)
        self.assertEqual(self.dump(), before, "no table changed, including event_side and reservations")
        self.assertTrue(self.cell.work.verify_chain(case))

    def test_unknown_or_malformed_explain_is_refused_not_crashed(self):
        case, made = self.proposal()
        pid = made["proposal"]["id"]
        for path, code in ((f"/api/cases/{case}/proposals/missing/explain", "unknown_proposal"),
                           (f"/api/cases/no-such-case/proposals/{pid}/explain", "unknown_case"),
                           (f"/api/cases/{case}/proposals/{pid}/explain?when=later", "invalid_explain")):
            with self.subTest(path):
                status, body = self.cell.json(path)
                self.assertEqual((status, body["error"]), (409, code))
        self.assertEqual(self.cell.json(f"/api/cases/{case}/proposals/{pid}")[0], 404)

    def test_explain_discloses_no_path_secret_or_planner_url(self):
        case, made = self.proposal()
        path = f"/api/cases/{case}/proposals/{made['proposal']['id']}/explain?when=recorded"
        os.environ["SYBERWORK_PLANNER_URL"] = "http://127.0.0.1:9/plan?secret=abc"
        os.environ["SYBERWORK_PLANNER_TOKEN"] = "planner-token-value"
        self.addCleanup(os.environ.pop, "SYBERWORK_PLANNER_URL", None)
        self.addCleanup(os.environ.pop, "SYBERWORK_PLANNER_TOKEN", None)
        status, _, raw = self.cell.call(path)
        self.assertEqual(status, 200)
        for leak in (b"127.0.0.1:9", b"secret=abc", b"planner-token-value", self.cell.tokens["operator"].encode(), str(ROOT).encode()):
            self.assertNotIn(leak, raw)
        # A rule whose source is outside the package and the working directory has only an absolute
        # path: the module is withheld, the rule and symbol are still answered.
        real = self.cell.work.explain_admission
        def outside(*args, **kwargs):
            found = real(*args, **kwargs)
            return {**found, "provenance": {**found["provenance"], "module": "/home/someone/rules/custom.py"}}
        self.cell.work.explain_admission = outside
        self.addCleanup(vars(self.cell.work).pop, "explain_admission", None)
        status, _, raw = self.cell.call(path)
        body = json.loads(raw)
        self.assertEqual((status, body["rule"], body["provenance"]["module"]), (200, "limits.amount", None))
        self.assertEqual(body["provenance"]["symbol"], "limits_amount")
        self.assertNotIn(b"/home/someone", raw)

    def test_explained_module_does_not_depend_on_the_working_directory(self):
        """From / provenance names the module by its full path less the leading slash; it is re-anchored."""
        from syberlabs import admission
        case, made = self.proposal()
        path = f"/api/cases/{case}/proposals/{made['proposal']['id']}/explain"
        here = os.getcwd()
        self.addCleanup(admission._PROVENANCE.clear)
        self.addCleanup(os.chdir, here)
        for folder in ("/", str(ROOT.parent), tempfile.gettempdir()):
            with self.subTest(folder):
                os.chdir(folder)
                admission._PROVENANCE.clear()
                self.assertNotEqual(admission.rule_provenance("limits.amount")["module"], "syberlabs/admission.py") if folder == "/" else None
                status, _, raw = self.cell.call(path)
                self.assertEqual((status, json.loads(raw)["provenance"]["module"]), (200, "syberlabs/admission.py"))
                self.assertNotIn(str(ROOT).strip("/").encode(), raw)


class ConsoleStatic(CellCase):
    def test_console_files_are_served_with_the_content_security_policy(self):
        for name, kind in (("", "text/html"), ("app.js", "text/javascript"), ("model.js", "text/javascript"), ("style.css", "text/css")):
            with self.subTest(name):
                status, headers, body = self.cell.call("/" + name, who=None)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(kind))
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertTrue(body)

    def test_only_the_named_files_are_served(self):
        for path in ("/app.js.map", "/../core.py", "/%2e%2e/server.py", "/static/app.js", "/examples/contract.json"):
            with self.subTest(path):
                self.assertNotEqual(self.cell.call(path, who=None)[0], 200)

    def test_scripts_build_the_page_without_html_strings_or_inline_code(self):
        html = (STATIC / "index.html").read_text()
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)
        self.assertIn('<script type="module" src="/app.js">', html)
        for name in ("app.js", "model.js"):
            source = (STATIC / name).read_text()
            for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(unsafe, source, f"{name} uses {unsafe}")
        app = (STATIC / "app.js").read_text()
        self.assertNotIn("localStorage", app, "credentials stay in session storage")
        self.assertNotIn("location.search", app)

    def test_every_route_the_console_calls_exists(self):
        """Each API path in app.js is answered by the server (not 404 not_found) for an authorized caller."""
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        gets = ["/api/me", "/api/console", "/api/cases", f"/api/cases/{case}", f"/api/cases/{case}/verify", "/api/contracts",
                "/api/contracts/purchase-order/1", "/api/contracts/purchase-order/1/artifacts", "/api/policies",
                "/api/actions", "/api/sources", "/metrics/cases_open", "/metrics/effects_pending", "/metrics/queue_depth",
                "/metrics/proposals_denied_total", "/metrics/work_items"]
        proposal = self.cell.json(f"/api/cases/{case}/proposals", {"action": "record_review", "args": {}}, who="admin")[1]["proposal"]["id"]
        gets += [f"/api/cases/{case}/proposals/{proposal}/explain?when={when}" for when in ("recorded", "now")]
        for path in gets:
            with self.subTest(path):
                self.assertEqual(self.cell.call(path, who="admin")[0], 200, path)
        app = (STATIC / "app.js").read_text()
        self.assertIn("'/proposals/' + encodeURIComponent(p.id) + '/explain?when=' + when", app)
        self.assertNotIn("not exposed over HTTP", app)
        for operation in ("refresh", "facts", "proposals", "compiled", "suggest", "approve", "commit", "signoff", "reconcile",
                          "replay", "cancel", "resolution-request", "resolution-resolve", "resolution-escalate", "candidates", "evaluations"):
            with self.subTest(operation):
                self.assertIn(f"casePath('{operation}')", app)
                status, body = self.cell.json(f"/api/cases/{case}/{operation}", {}, who="admin")
                self.assertNotEqual(body.get("error"), "unknown_route", operation)


class ConsoleModel(CellCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_projections_match_the_python_rules(self):
        contract = json.loads((EXAMPLES / "contract.json").read_text())
        policy = json.loads((EXAMPLES / "policy.json").read_text())
        proof = {"verified": True, "external_id": "ord-1", "response_digest": "r"}
        events = [
            {"kind": "reconciled", "body": {"success": True, "proof": proof}},
            {"kind": "reconciled", "body": {"success": True, "proof": dict(proof, verified="true")}},
            {"kind": "reconciled", "body": {"success": True, "proof": dict(proof, external_id="")}},
            {"kind": "reconciled", "body": {"success": True, "proof": dict(proof, response_digest=None)}},
            {"kind": "reconciled", "body": {"success": True, "proof": dict(proof, external_id=[])}},
            {"kind": "reconciled", "body": {"success": True, "proof": dict(proof, response_digest={})}},
            {"kind": "reconciled", "body": {"success": True, "proof": [proof]}},
            {"kind": "reconciled", "body": {"success": False, "proof": proof}},
            {"kind": "reconciled", "body": {"success": True, "evidence": "manager says so"}},
            {"kind": "reconciliation_checked", "body": {"success": True, "proof": proof}},
            {"kind": "observed", "body": {"value": {"schema": "attest.receipt/v0", "verified": True}, "proof": proof, "success": True}},
        ]
        parity = {
            "reconciliation": [{"event": e, "expected": verified_reconciliation(e)} for e in events],
            "approval": [{"contract": contract, "policy": policy, "action": a, "expected": approval_roles(contract, policy, a)}
                         for a in ("record_review", "issue_order", "absent")],
        }
        self.assertEqual([c["expected"] for c in parity["reconciliation"]], [True] + [False] * 10)
        parity["read_scope"] = {"policy_readers": sorted(POLICY_READERS), "automation_roles": sorted(AUTOMATION_ROLES)}
        # What this server answers for a denied proposal, read by model.js as the console reads it.
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        denied = self.cell.json(f"/api/cases/{case}/proposals", {"action": "issue_order", "args": {}})[1]["proposal"]["id"]
        parity["explain"] = [{"body": self.cell.json(f"/api/cases/{case}/proposals/{denied}/explain?when={when}")[1], "when": when,
                              "rule": "limits.amount", "provenance": "syberlabs/admission.py:"} for when in ("recorded", "now")]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(parity, handle)
        self.addCleanup(os.unlink, handle.name)
        done = subprocess.run(["node", "--test", str(MODEL_TESTS)], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PARITY": handle.name}, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("# fail 0", done.stdout)
        self.assertIn("# skipped 0", done.stdout)


class ConsoleBrowser(CellCase):
    reference = True

    def test_the_purchase_order_path_completes_in_a_browser(self):
        root = playwright_root()
        if root is None:
            self.skipTest("node playwright is not installed")
        env = {**os.environ, "NODE_PATH": root, "CONSOLE_URL": self.cell.base + "/", "CONSOLE_TOKENS": json.dumps(self.cell.tokens)}
        env.pop("SHOTS", None)
        if Path("/opt/pw-browsers/chromium").exists():
            env.setdefault("CHROMIUM", "/opt/pw-browsers/chromium")
        done = subprocess.run(["node", str(BROWSER_CHECK)], capture_output=True, text=True, env=env, timeout=240)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        report = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertNotIn("failure", report, report)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["steps"], ["identities", "case_opened", "record_review_applied", "approved", "order_applied", "signed", "published_v2"])
        self.assertEqual(report["compiled_denied_reason"], "compiled proposer credential required")
        self.assertEqual(report["offers_switch"], 1)
        self.assertTrue(report["self_approval_blocked"])
        self.assertIn("Complete", report["status"])
        self.assertIn("intact", report["verify"])
        self.assertTrue(any("destination acknowledged HTTP 201" in row for row in report["effects"]), report["effects"])
        self.assertTrue(any("recorded locally" in row for row in report["effects"]), report["effects"])
        self.assertIn("No external planner configured", report["planner"])
        # The deciding rule is the server's: GET .../explain, written beside each decision.
        self.assertEqual(len(report["flow_rules"]), 2, report["flow_rules"])
        self.assertIn("admission.passed", report["flow_rules"][0])
        self.assertIn("approval.required", report["flow_rules"][1])
        self.assertTrue(all("Deciding rule" in row and "written with decision #" in row for row in report["flow_rules"]), report["flow_rules"])
        self.assertIn("syberlabs/admission.py:", report["flow_rules"][1])
        self.assertEqual(sorted(row.split(" ")[0] for row in report["propose_rules"]), ["admission.passed", "approval.required"], report["propose_rules"])
        self.assertIn("allowed", report["now_rule"])
        self.assertIn("admission.passed", report["now_rule"])
        self.assertIn(" · read ", report["now_rule"], "a forecast of now says when the server answered")
        self.assertEqual(report["placeholder"], 0)
        self.assertTrue(report["mismatch_rules"] and all("unknown" in row for row in report["mismatch_rules"]), report["mismatch_rules"])
        self.assertFalse(any("invented.rule" in row for row in report["mismatch_rules"]), "a rule that does not reproduce the decision is never shown")
        self.assertTrue(report["refused_rules"] and all("unavailable" in row and "unknown_case" in row for row in report["refused_rules"]), report["refused_rules"])
        self.assertIn("amount_exceeds_limit", report["replay"])
        self.assertEqual(len(report["manual"]), 2)
        self.assertTrue(all("unverified" in row for row in report["manual"]))
        self.assertIn("Attest claim", report["manual"][0])
        self.assertIn("not destination-write proof", report["attest_notice"])
        self.assertFalse(report["script_injected"])
        # Read scope: the operator is told why the policy is not shown and who can read it; the manager reads it.
        self.assertIn("admin or manager role required", report["policy_note"])
        self.assertIn("switch to manager", report["policy_note"])
        self.assertEqual(report["manager_policy_note"], 0)
        self.assertEqual(report["refresh_notes"], [], "the operator's refresh is forecast from the listed read role")
        self.assertIn("work items", report["metrics"])
        self.assertEqual(sorted(report["source_roles"]), ["inventoryoperator✕no read role", "supplieroperator✕no read role"])
        self.assertIn("admin or manager role required", report["policy_nav"])
        self.assertFalse(report["storage_has_token_in_url"])
        cases = self.cell.work.list_cases()
        done_case = next(c for c in cases if self.cell.work.inspect(c["id"])["complete"])
        self.assertTrue(self.cell.work.verify_chain(done_case["id"]))


if __name__ == "__main__":
    unittest.main()
