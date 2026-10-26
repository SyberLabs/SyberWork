"""The operator console: its read-only routes, its static files, its projections, and a real browser run.

The browser run walks the included purchase-order path against ``examples/reference_system.py`` on a
free port: refresh, compiled proposals, independent manager approval, execution, signoff, verify,
publishing contract v2 in the studio and replaying the case against it. It skips when node or the node
``playwright`` package is not installed, like ``tests/test_inspector.py``.
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from syberlabs.admission import approval_roles
from syberlabs.evidence import verified_reconciliation
from syberwork.core import Work
from syberwork.server import make_server

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


def read_text(path: Path) -> str:
    """Read a repository file as UTF-8. Windows would otherwise use the ANSI code page."""
    return path.read_text(encoding="utf-8")


def stop_process(proc: subprocess.Popen) -> None:
    """Stop a child and wait so Windows can delete files it still has open."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def wait_until_listening(port: int, proc: subprocess.Popen, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"reference system exited {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    stop_process(proc)
    raise RuntimeError("reference system did not accept connections")


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
        root = subprocess.run([shutil.which("npm"), "root", "-g"], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return root if root and (Path(root) / "playwright").exists() else None


class Cell:
    """The included example installed as ``init`` installs it, with sources and actions on ``reference_port``."""

    def __init__(self, folder: Path, reference_port: int = 8999):
        self.work = Work(folder / "work.sqlite3")
        self.tokens = {name: "token-" + name for name in IDENTITIES}
        users = {name: {"hash": hashlib.sha256(self.tokens[name].encode()).hexdigest(), "roles": roles, "sources": sources}
                 for name, (roles, sources) in IDENTITIES.items()}
        retarget = lambda doc: json.loads(json.dumps(doc).replace("127.0.0.1:8999", f"127.0.0.1:{reference_port}"))
        self.work.install_policy(json.loads(read_text(EXAMPLES / "policy.json")))
        self.work.install_contract(json.loads(read_text(EXAMPLES / "contract.json")))
        for name, doc in json.loads(read_text(EXAMPLES / "actions.json")).items():
            self.work.install_action(name, retarget(doc))
        for name, doc in json.loads(read_text(EXAMPLES / "sources.json")).items():
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
            # A subprocess, not a daemon thread: its sqlite handles die with the process, so
            # Windows can delete the temporary database. Cleanup runs after the cell closes.
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "examples" / "reference_system.py"),
                 "--database", str(Path(folder.name) / "reference.sqlite3"), "--port", str(port)])
            self.addCleanup(stop_process, proc)
            wait_until_listening(port, proc)
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
        contract = json.loads(read_text(EXAMPLES / "contract.json"))
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
        status, policies = self.cell.json("/api/policies")
        self.assertEqual((status, [p["version"] for p in policies]), (200, [1]))
        self.assertEqual(policies[0]["body"], json.loads(read_text(EXAMPLES / "policy.json")))

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
        for path in ("/api/actions", "/api/sources"):
            with self.subTest(path):
                self.assertEqual(self.cell.json(path)[1]["error"], "forbidden")
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
        for path in ("/api/console", "/api/contracts", "/api/contracts/purchase-order/1", "/api/policies"):
            self.cell.call(path)
        for path in ("/api/actions", "/api/sources"):
            self.cell.call(path, who="admin")
        self.assertEqual(self.cell.work.inspect(case)["events"], before)
        self.assertTrue(self.cell.work.verify_chain(case))


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
        html = read_text(STATIC / "index.html")
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)
        self.assertIn('<script type="module" src="/app.js">', html)
        for name in ("app.js", "model.js"):
            source = read_text(STATIC / name)
            for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(unsafe, source, f"{name} uses {unsafe}")
        app = read_text(STATIC / "app.js")
        self.assertNotIn("localStorage", app, "credentials stay in session storage")
        self.assertNotIn("location.search", app)

    def test_every_route_the_console_calls_exists(self):
        """Each API path in app.js is answered by the server (not 404 not_found) for an authorized caller."""
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        gets = ["/api/me", "/api/console", "/api/cases", f"/api/cases/{case}", f"/api/cases/{case}/verify", "/api/contracts",
                "/api/contracts/purchase-order/1", "/api/contracts/purchase-order/1/artifacts", "/api/policies",
                "/api/actions", "/api/sources", "/metrics/cases_open", "/metrics/effects_pending", "/metrics/queue_depth",
                "/metrics/proposals_denied_total"]
        for path in gets:
            with self.subTest(path):
                self.assertEqual(self.cell.call(path, who="admin")[0], 200, path)
        app = read_text(STATIC / "app.js")
        for operation in ("refresh", "facts", "proposals", "compiled", "suggest", "approve", "commit", "signoff", "reconcile",
                          "replay", "cancel", "resolution-request", "resolution-resolve", "resolution-escalate", "candidates", "evaluations"):
            with self.subTest(operation):
                self.assertIn(f"casePath('{operation}')", app)
                status, body = self.cell.json(f"/api/cases/{case}/{operation}", {}, who="admin")
                self.assertNotEqual(body.get("error"), "unknown_route", operation)


class ConsoleModel(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_projections_match_the_python_rules(self):
        contract = json.loads(read_text(EXAMPLES / "contract.json"))
        policy = json.loads(read_text(EXAMPLES / "policy.json"))
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
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(parity, handle)
        self.addCleanup(os.unlink, handle.name)
        # Pin the TAP reporter. A newer Node prints the spec reporter on stdout, which has no
        # "# fail 0" line, even when every test passed. TAP is ASCII and stable on Windows.
        done = subprocess.run(
            ["node", "--test", "--test-reporter=tap", str(MODEL_TESTS)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env={**os.environ, "PARITY": handle.name}, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("# fail 0", done.stdout)
        self.assertIn("# skipped 0", done.stdout)


class ConsoleBrowser(CellCase):
    reference = True

    def test_the_purchase_order_path_completes_in_a_browser(self):
        # The strict suite allows only the inspector's exact reasons for a missing browser tool.
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        root = playwright_root()
        if root is None:
            if shutil.which("npm") is None:
                self.skipTest("npm is not installed")
            self.skipTest("playwright for node is not installed")
        env = {**os.environ, "NODE_PATH": root, "CONSOLE_URL": self.cell.base + "/", "CONSOLE_TOKENS": json.dumps(self.cell.tokens)}
        env.pop("SHOTS", None)
        if Path("/opt/pw-browsers/chromium").exists():
            env.setdefault("CHROMIUM", "/opt/pw-browsers/chromium")
        done = subprocess.run(["node", str(BROWSER_CHECK)], capture_output=True, text=True, encoding="utf-8", env=env, timeout=240)
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
        self.assertIn("amount_exceeds_limit", report["replay"])
        self.assertEqual(len(report["manual"]), 2)
        self.assertTrue(all("unverified" in row for row in report["manual"]))
        self.assertIn("Attest claim", report["manual"][0])
        self.assertIn("not destination-write proof", report["attest_notice"])
        self.assertFalse(report["script_injected"])
        self.assertFalse(report["storage_has_token_in_url"])
        cases = self.cell.work.list_cases()
        done_case = next(c for c in cases if self.cell.work.inspect(c["id"])["complete"])
        self.assertTrue(self.cell.work.verify_chain(done_case["id"]))


if __name__ == "__main__":
    unittest.main()
