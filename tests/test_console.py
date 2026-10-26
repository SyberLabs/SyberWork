"""The operator console: its read-only routes, its static files, its projections, and a real browser run.

The browser run walks the included purchase-order path against ``examples/reference_system.py`` on a
free port: refresh, compiled proposals, independent manager approval, execution, signoff, verify,
publishing contract v2 in the studio and replaying the case against it. It skips when node or the node
``playwright`` package is not installed, like ``tests/test_inspector.py``.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

from syberlabs.admission import approval_roles
from syberlabs.build import Kit
from syberlabs.canonical import canonical
from syberlabs.certificate import certify, write_certificate
from syberlabs.dsse import CHAIN_HEAD_TYPE, CHECKPOINT_TYPE, sign_chain_head, sign_dsse
from syberlabs import ed25519
from syberlabs.ed25519 import generate, public_from_seed
from syberlabs.errors import Rejected
from syberlabs.evidence import verified_reconciliation
from syberlabs.evolution import AUTOMATION_ROLES
from syberlabs.jcs import envelope_jcs, jcs_bytes
from syberlabs.metrics import METRICS, UNKNOWN, Metric, _cell
from syberlabs.tlog import TransparencyLog, merkle_root
from syberlabs.witness import Witness, WitnessClient, WitnessServer
from syberwork import certificates as shelf_module
from syberwork.certificates import LIMIT as CERTIFICATE_LIMIT, CertificateShelf
from syberwork.cli import _certificates, _witness_log, _witness_request
from syberwork.core import Work
from syberwork.server import POLICY_READERS, make_server
from syberwork.trace import TraceLog
from tests.test_build_thread import GOOD, REPORT, TEST_REPORT, git
from tests.test_certificate import OTHER_PUBLIC, OTHER_SEED, PUBLIC, SEED

# A third key that no test configures: a certificate signed under it is never trusted.
THIRD_SEED = hashlib.sha256(b"syberwork console third certificate key").digest()
THIRD_PUBLIC = public_from_seed(THIRD_SEED)

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


@contextlib.contextmanager
def connect_db(path):
    """Open sqlite, commit on success, and close.

    ``with sqlite3.connect()`` commits and leaves the connection open. Windows
    then cannot delete the temporary database.
    """
    db = sqlite3.connect(path)
    try:
        yield db
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()
    finally:
        db.close()


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


def certified_home(folder: Path, seed: bytes = SEED) -> tuple[Path, dict]:
    """A Build Thread home whose one candidate was checked, accepted and certified, as ``syberlabs certify`` leaves it.

    Two files under ``<home>/certificates``: the default in-toto statement and the ``--bare`` form, each with its ``.pub``.
    Returns the home and the payload.
    """
    root = folder / "project"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "report.py").write_text(REPORT)
    (root / "tests" / "test_report.py").write_text(TEST_REPORT)
    git(folder, "init", "-q", "-b", "main", str(root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    home = root / ".syberlabs"
    kit = Kit.local(home, root, actor="dev@example.test")
    try:
        thread = kit.start("Add a CSV export for report rows", paths=["src/", "tests/"])
        thread.propose(changes=GOOD, message="CSV export")
        if not thread.check("c1").acceptable or thread.accept("c1").status != "succeeded":
            raise RuntimeError("the fixture candidate was not accepted")
        for bare, name in ((False, "c1.json"), (True, "c1.bare.json")):
            envelope, payload = certify(kit, thread.id, "c1", seed=seed, bare=bare)
            write_certificate(envelope, public_from_seed(seed), home / "certificates" / f"{thread.id[:8]}-{name}")
    finally:
        kit.close()
    return home, payload


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

    def __init__(self, folder: Path, reference_port: int = 8999, witness_log=None, witness_client=None, witness_role=None,
                 certificates=None):
        self.database = folder / "work.sqlite3"
        self.work = Work(self.database)
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
        self.server = make_server(self.work, users, port=0, witness_log=witness_log, witness_client=witness_client, witness_role=witness_role,
                                  certificates=certificates)
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
    # True: an in-process Witness writes the log serve reads. "server": a loopback WitnessServer on witness_port, which
    # serve may ask to sign for witness_role.
    witness = False
    witness_role = "manager"
    # True: serve reads a Build Thread home holding one certified candidate (certified_home), signed under PUBLIC, and
    # trusts certificate_keys and revokes the digests in certificate_revoked.
    certificates = False
    certificate_keys = (PUBLIC,)
    certificate_revoked = ()

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
        reader = client = None
        if self.witness:
            self.seed, self.public = generate()
            self.log_path = Path(folder.name) / "witness.sqlite3"
            if self.witness == "server":
                server = WitnessServer(str(self.log_path), self.seed, 0)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.close)
                self.addCleanup(server.shutdown)
                self.witness_port, self.witness_log = server.server_address[1], server.witness._log
            else:
                writer = Witness(str(self.log_path), self.seed)
                self.addCleanup(writer.close)
                self.attest, self.witness_log = writer.attest, writer._log
            reader = TransparencyLog.open_readonly(self.log_path, self.public)
            self.addCleanup(reader.close)
            if self.witness == "server":
                client = WitnessClient(self.witness_port, self.public, str(self.log_path))
                self.addCleanup(client.close)
        shelf = None
        if self.certificates:
            self.cert_home, self.cert_payload = certified_home(Path(folder.name) / "build")
            shelf = self.shelf = CertificateShelf(self.cert_home, self.certificate_keys, self.certificate_revoked)
        self.cell = Cell(Path(folder.name), port, reader, client, self.witness_role if client else None, shelf)
        self.addCleanup(self.cell.close)


class ConsoleRoutes(CellCase):
    def test_every_new_route_requires_a_bearer_token(self):
        for path in ("/api/console", "/api/contracts", "/api/contracts/purchase-order/1", "/api/policies", "/api/actions", "/api/sources",
                     "/api/cases/any/side", "/api/cases/any/witness"):
            with self.subTest(path):
                status, body = self.cell.json(path, who=None)
                self.assertEqual((status, body["error"]), (401, "unauthorized"))

    def test_console_reports_planner_configuration_without_disclosing_it(self):
        url = "http://127.0.0.1:9/plan?secret=abc"
        previous = os.environ.pop("SYBERWORK_PLANNER_URL", None)
        self.addCleanup(lambda: os.environ.__setitem__("SYBERWORK_PLANNER_URL", previous) if previous is not None else os.environ.pop("SYBERWORK_PLANNER_URL", None))
        status, body = self.cell.json("/api/console")
        self.assertEqual((status, body), (200, {"planner_configured": False, "effects": "inline", "witness": {"log": False, "request_role": None},
                                         "certificates": {"configured": False}}))
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
        status, policies = self.cell.json("/api/policies", who="manager")
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
                     "/metrics/work_items", f"/metrics/work_items/{case}", f"/api/cases/{case}/side"):
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

    def test_read_roles_that_are_not_a_list_of_names_are_answered_as_unknown(self):
        """install_source does not validate roles; the projection never forecasts from a value refresh_fact misreads."""
        self.cell.work.install_source("odd", {"kind": "http", "url": "http://127.0.0.1:9/odd/{key}", "roles": "admin"})
        self.cell.work.install_source("unset", {"kind": "http", "url": "http://127.0.0.1:9/unset/{key}", "roles": None})
        self.cell.work.install_source("closed", {"kind": "http", "url": "http://127.0.0.1:9/closed/{key}", "roles": []})
        sources = self.cell.json("/api/sources")[1]
        self.assertEqual((sources["odd"], sources["unset"], sources["closed"]), ({"roles": None}, {"roles": None}, {"roles": []}))

    def test_a_refused_builder_read_keeps_its_status(self):
        """Only the console's reads moved to 403; a builder GET refused as forbidden still answers 409."""
        users = {"odd": {"hash": hashlib.sha256(b"token-odd").hexdigest(), "roles": "admin", "sources": []}}
        server = make_server(self.cell.work, users, port=0)
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for path, expected in (("/api/builder/policies", 409), ("/api/policies", 403)):
            with self.subTest(path):
                request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", headers={"Authorization": "Bearer token-odd"})
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual((refused.exception.code, json.loads(refused.exception.read())["error"]), (expected, "forbidden"))

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
        app = read_text(STATIC / "app.js")
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
        with connect_db(self.cell.database) as db:
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


class ConsoleSideRecords(CellCase):
    """GET /api/cases/{id}/side: Work.side_channel over HTTP, read only. A case has no certificate route."""

    def case_with_decision(self):
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        made = self.cell.json(f"/api/cases/{case}/proposals", {"action": "issue_order", "args": {}})[1]
        self.assertEqual(made["decision"]["reason"], "amount_required")
        return case

    def test_side_records_are_work_side_channel_for_any_principal(self):
        case = self.case_with_decision()
        events = self.cell.work.inspect(case)["events"]
        expected = self.cell.work.side_channel(case)
        for who in ("operator", "manager", "supplier", "planner"):
            with self.subTest(who):
                self.assertEqual(self.cell.json(f"/api/cases/{case}/side", who=who), (200, expected))
        self.assertEqual([row["seq"] for row in expected], [e["seq"] for e in events], "every new event has its row")
        self.assertEqual([row["jcs"] for row in expected], [envelope_jcs(e) for e in events])
        rules = {row["seq"]: row["rule"] for row in expected}
        self.assertEqual([rules[e["seq"]] for e in events if e["kind"] == "decision"], ["limits.amount"])
        self.assertTrue(all(rules[e["seq"]] is None for e in events if e["kind"] != "decision"))
        self.assertEqual(self.cell.json(f"/api/cases/{case}/side", who=None)[0], 401)

    def test_side_read_appends_nothing_and_an_unknown_case_is_refused(self):
        case = self.case_with_decision()
        with connect_db(self.cell.database) as db:
            before = list(db.iterdump())
        self.cell.call(f"/api/cases/{case}/side")
        with connect_db(self.cell.database) as db:
            self.assertEqual(list(db.iterdump()), before)
        for path in ("/api/cases/no-such-case/side", "/api/cases/%2e%2e/side"):
            with self.subTest(path):
                status, body = self.cell.json(path)
                self.assertNotEqual(status, 500)
                self.assertEqual(body["error"], "unknown_case")

    def test_the_route_reports_a_stored_digest_as_stored_and_verify_judges_it(self):
        """The route is a read, not a check: an altered row comes back as written, and only verify refuses it."""
        case = self.case_with_decision()
        with connect_db(self.cell.database) as db:
            db.execute("UPDATE event_side SET jcs=? WHERE case_id=? AND seq=1", ("0" * 64, case))
        rows = self.cell.json(f"/api/cases/{case}/side")[1]
        self.assertEqual(rows[0]["jcs"], "0" * 64)
        self.assertEqual(self.cell.json(f"/api/cases/{case}/verify"), (200, {"valid": False}))
        with connect_db(self.cell.database) as db:
            db.execute("DELETE FROM event_side WHERE case_id=? AND seq=1", (case,))
        self.assertEqual([row["seq"] for row in self.cell.json(f"/api/cases/{case}/side")[1]][:1], [2])
        self.assertEqual(self.cell.json(f"/api/cases/{case}/verify")[1], {"valid": True},
                         "verify cannot notice a missing row, which is why the console says so instead of calling it legacy")
        # Nor does verify check a row with no digest, or a rule id: the console presents neither as checked.
        decision = next(e["seq"] for e in self.cell.work.inspect(case)["events"] if e["kind"] == "decision")
        with connect_db(self.cell.database) as db:
            db.execute("UPDATE event_side SET jcs=NULL, rule='invented.rule' WHERE case_id=? AND seq=?", (case, decision))
        row = next(r for r in self.cell.json(f"/api/cases/{case}/side")[1] if r["seq"] == decision)
        self.assertEqual((row["jcs"], row["rule"]), (None, "invented.rule"))
        self.assertEqual(self.cell.json(f"/api/cases/{case}/verify")[1], {"valid": True})
        app = read_text(STATIC / "app.js")
        for wording in ("it cannot notice a missing row", "Verify history does not check a row without a digest",
                        "the rule ids are in no hash and nothing checks them", "only Verify history tells them apart"):
            self.assertIn(wording, app)

    def test_a_case_has_no_certificate_route(self):
        """Certificates are about Build Threads, not cases: there is no per-case route, and History says why."""
        case = self.case_with_decision()
        self.assertEqual(self.cell.json(f"/api/cases/{case}/certificate"), (404, {"error": "not_found"}))
        app = read_text(STATIC / "app.js")
        self.assertNotIn("GET /api/cases/{id}/certificate", app)
        self.assertNotIn("'Show certificate'", app)
        self.assertIn("a case is not a Build Thread and has none", app)


class ConsoleWitness(CellCase):
    """GET /api/cases/{id}/witness: Work.witness_status over a read-only reader of the witness's log."""

    witness = True

    def case_events(self, *, decided=True):
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        if decided:
            self.cell.json(f"/api/cases/{case}/proposals", {"action": "issue_order", "args": {}})
        return case, self.cell.work.inspect(case)["events"]

    def forge(self, envelope):
        """Append a leaf the witness API would refuse, then sign a checkpoint over the log as the witness would."""
        db = self.witness_log._db
        size = db.execute("SELECT COUNT(*) AS n FROM leaves").fetchone()["n"]
        db.execute("INSERT INTO leaves (idx, body) VALUES (?, ?)", (size, canonical(envelope)))
        bodies = [row["body"].encode() for row in db.execute("SELECT body FROM leaves ORDER BY idx")]
        checkpoint = sign_dsse(CHECKPOINT_TYPE, canonical({"root": merkle_root(bodies).hex(), "size": size + 1}).encode("ascii"), self.seed)
        db.execute("INSERT INTO checkpoints (size, envelope) VALUES (?, ?)", (size + 1, canonical(checkpoint)))

    def test_the_route_requires_a_token_and_a_known_case(self):
        self.assertEqual(self.cell.json("/api/cases/any/witness", who=None)[1]["error"], "unauthorized")
        for path in ("/api/cases/no-such-case/witness", "/api/cases/%2e%2e/witness"):
            with self.subTest(path):
                status, body = self.cell.json(path)
                self.assertNotEqual(status, 500)
                self.assertEqual(body["error"], "unknown_case")

    def test_a_signed_head_is_witnessed_through_its_count_and_later_events_are_not(self):
        case, events = self.case_events()
        other, other_events = self.case_events(decided=False)
        self.attest(other_events)
        self.assertEqual(self.cell.json(f"/api/cases/{case}/witness")[1]["entries"], [], "another case's leaf is not this case's")
        self.attest(events)
        for who in ("operator", "manager", "supplier", "planner"):
            with self.subTest(who):
                status, body = self.cell.json(f"/api/cases/{case}/witness", who=who)
                self.assertEqual(status, 200)
                self.assertEqual(body["entries"], [{"index": 1, "count": len(events), "head": events[-1]["hash"], "verdict": "included"}])
                self.assertEqual((body["configured"], body["checkpoint"], body["witnessed_through"]), (True, True, len(events)))
                self.assertEqual(body["public_key"], self.public.hex())
        # A later event: the signed head still covers the events it named, and only those.
        self.cell.json(f"/api/cases/{case}/proposals", {"action": "record_review", "args": {}}, who="scheduler")
        body = self.cell.json(f"/api/cases/{case}/witness")[1]
        self.assertGreater(body["count"], len(events))
        self.assertEqual((body["witnessed_through"], body["entries"][0]["verdict"]), (len(events), "included"))
        reader = TransparencyLog.open_readonly(self.log_path, self.public)
        self.addCleanup(reader.close)
        self.assertEqual(body, self.cell.work.witness_status(case, reader), "the route is Work.witness_status unchanged")

    def test_a_head_this_history_does_not_have_is_named_not_counted(self):
        case, events = self.case_events()
        self.attest(events)
        rewritten = [*events[:-1], dict(events[-1], hash="0" * 64)]
        longer = [*events, dict(events[-1], seq=len(events) + 1, hash="1" * 64)]
        self.witness_log.append(sign_chain_head(case, rewritten, self.seed))
        self.witness_log.append(sign_chain_head(case, longer, self.seed))
        other_seed, _ = generate()
        self.forge(sign_chain_head(case, events, other_seed))
        self.forge(sign_dsse(CHAIN_HEAD_TYPE, canonical({"case_id": case, "count": "all", "head": "x"}).encode("ascii"), self.seed))
        body = self.cell.json(f"/api/cases/{case}/witness")[1]
        self.assertEqual([(e["count"], e["verdict"]) for e in body["entries"]],
                         [(len(events), "included"), (len(events), "head_differs"), (len(events) + 1, "beyond_history"),
                          (None, "bad_signature"), (None, "malformed")])
        self.assertEqual(body["entries"][1]["head"], "0" * 64)
        self.assertIsNone(body["witnessed_through"], "a head this history does not have outranks a witnessed one")

    def test_a_history_changed_after_signing_is_not_witnessed(self):
        """The head matches the hash stored on the last event, but an earlier event no longer hashes into it."""
        case, events = self.case_events()
        self.attest(events)
        self.assertEqual(self.cell.json(f"/api/cases/{case}/witness")[1]["witnessed_through"], len(events))
        with closing(sqlite3.connect(self.cell.database)) as db, db:
            db.execute("UPDATE events SET body=? WHERE case_id=? AND seq=1", (canonical({"tampered": True}), case))
        self.assertEqual(self.cell.json(f"/api/cases/{case}/verify")[1], {"valid": False})
        body = self.cell.json(f"/api/cases/{case}/witness")[1]
        self.assertEqual(([e["verdict"] for e in body["entries"]], body["witnessed_through"]), (["chain_broken"], None))

    def test_an_unreadable_leaf_is_skipped_not_a_crash(self):
        case, events = self.case_events()
        self.attest(events)
        self.forge({"payloadType": CHAIN_HEAD_TYPE, "payload": "[" * 100000})
        db = self.witness_log._db
        size = db.execute("SELECT COUNT(*) AS n FROM leaves").fetchone()["n"]
        db.execute("INSERT INTO leaves (idx, body) VALUES (?, ?)", (size, b"\x00binary"))
        status, body = self.cell.json(f"/api/cases/{case}/witness")
        self.assertEqual(status, 200)
        self.assertEqual(([e["verdict"] for e in body["entries"]], body["checkpoint"]), (["not_included"], False))

    def test_a_leaf_the_newest_checkpoint_does_not_cover_is_not_included(self):
        case, events = self.case_events()
        self.attest(events)
        self.witness_log._db.execute("INSERT INTO leaves (idx, body) VALUES (?, ?)", (1, canonical({"payloadType": "x"})))
        body = self.cell.json(f"/api/cases/{case}/witness")[1]
        self.assertEqual((body["checkpoint"], body["entries"][0]["verdict"], body["witnessed_through"]), (False, "not_included", None))

    def test_the_read_appends_nothing_to_the_case_or_the_log(self):
        case, events = self.case_events()
        self.attest(events)
        with closing(sqlite3.connect(self.cell.database)) as db, db:
            before = list(db.iterdump())
        with closing(sqlite3.connect(self.log_path)) as db, db:
            log_before = list(db.iterdump())
        self.assertEqual(self.cell.call(f"/api/cases/{case}/witness")[0], 200)
        with closing(sqlite3.connect(self.cell.database)) as db, db:
            self.assertEqual(list(db.iterdump()), before)
        with closing(sqlite3.connect(self.log_path)) as db, db:
            self.assertEqual(list(db.iterdump()), log_before)

    def test_serve_reads_a_log_only_when_both_variables_name_one(self):
        parser = argparse.ArgumentParser()
        def configured(**names):
            environment = {k: v for k, v in os.environ.items() if not k.startswith("SYBERWORK_WITNESS_")}
            with unittest.mock.patch.dict(os.environ, {**environment, **names}, clear=True):
                return _witness_log(parser)
        self.assertIsNone(configured())
        for names in ({"SYBERWORK_WITNESS_LOG": str(self.log_path)}, {"SYBERWORK_WITNESS_PUBLIC": self.public.hex()},
                      {"SYBERWORK_WITNESS_LOG": str(self.log_path), "SYBERWORK_WITNESS_PUBLIC": "not hex"},
                      {"SYBERWORK_WITNESS_LOG": str(self.log_path), "SYBERWORK_WITNESS_PUBLIC": "00"},
                      {"SYBERWORK_WITNESS_LOG": str(self.cell.database), "SYBERWORK_WITNESS_PUBLIC": self.public.hex()}):
            with self.subTest(sorted(names)), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr"):
                configured(**names)
        log = configured(SYBERWORK_WITNESS_LOG=str(self.log_path), SYBERWORK_WITNESS_PUBLIC=self.public.hex())
        self.addCleanup(log.close)
        self.assertEqual(log.public, self.public)
        with self.assertRaises(Rejected):
            log.append(sign_chain_head("c", [], self.seed))

    def test_without_a_log_the_route_says_so(self):
        cell = Cell(Path(tempfile.mkdtemp(dir=self.log_path.parent)), 8999)
        self.addCleanup(cell.close)
        case = cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        status, body = cell.json(f"/api/cases/{case}/witness")
        self.assertEqual(status, 200)
        self.assertEqual((body["configured"], body["count"]), (False, 1))
        self.assertIn("SYBERWORK_WITNESS_LOG", body["note"])
        self.assertNotIn("entries", body)


class ConsoleWitnessRequest(CellCase):
    """POST /api/cases/{id}/witness: Work.request_witness asks a loopback WitnessServer, for the role serve was given."""

    witness = "server"

    def open_case(self, *, decided=True):
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        if decided:
            self.cell.json(f"/api/cases/{case}/proposals", {"action": "issue_order", "args": {}})
        return case

    def leaves(self) -> int:
        with closing(sqlite3.connect(self.log_path)) as db:
            return db.execute("SELECT COUNT(*) FROM leaves").fetchone()[0]

    def dump(self) -> list:
        with closing(sqlite3.connect(self.cell.database)) as db:
            return list(db.iterdump())

    def test_only_the_configured_role_may_ask_and_the_case_gets_nothing(self):
        case = self.open_case()
        events = self.cell.work.inspect(case)["events"]
        self.cell.work.register_principal("agent-1", "agent", "agent", "local", ["manager", "model"], token="token-agent")
        self.cell.tokens["agent"] = "token-agent"
        before = self.dump()
        self.assertEqual(self.cell.json(f"/api/cases/{case}/witness", {}, who=None)[1]["error"], "unauthorized")
        for who in ("operator", "scheduler", "planner", "admin", "supplier"):
            with self.subTest(who):
                self.assertEqual(self.cell.json(f"/api/cases/{case}/witness", {}, who=who),
                                 (409, {"error": "forbidden", "detail": "manager role required to ask the witness, without an automation role"}))
        # An agent principal that holds the role and an automation role is refused, as for policy bodies.
        self.assertEqual(self.cell.json(f"/api/cases/{case}/witness", {}, who="agent")[1]["error"], "forbidden")
        self.assertEqual(self.leaves(), 0, "a refused request reaches no witness")
        status, body = self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["case_id"], body["count"], body["head"], body["written"]), (case, len(events), events[-1]["hash"], False))
        self.assertEqual(self.leaves(), 1)
        self.assertEqual(self.dump(), before, "the case database is unchanged: no event, no side row")
        # The envelope is the witness's, in its log, under the configured key; the read route now counts it.
        reader = TransparencyLog.open_readonly(self.log_path, self.public)
        self.addCleanup(reader.close)
        self.assertTrue(reader.verify_witnessed(body["envelope"], events))
        read = self.cell.json(f"/api/cases/{case}/witness")[1]
        self.assertEqual((read["witnessed_through"], [e["verdict"] for e in read["entries"]]), (len(events), ["included"]))

    def test_a_covered_head_is_not_signed_again_and_a_later_one_is(self):
        case = self.open_case()
        self.assertEqual(self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")[0], 200)
        count = len(self.cell.work.inspect(case)["events"])
        self.assertEqual(self.cell.json(f"/api/cases/{case}/witness", {}, who="manager"),
                         (409, {"error": "already_witnessed", "detail": f"a signed head in the log already covers #1 to #{count}"}))
        self.assertEqual(self.leaves(), 1)
        self.cell.json(f"/api/cases/{case}/proposals", {"action": "record_review", "args": {}}, who="scheduler")
        status, body = self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")
        self.assertEqual((status, self.leaves()), (200, 2))
        self.assertGreater(body["count"], count)
        read = self.cell.json(f"/api/cases/{case}/witness")[1]
        self.assertEqual([(e["count"], e["verdict"]) for e in read["entries"]], [(count, "included"), (body["count"], "included")])
        self.assertEqual(read["witnessed_through"], body["count"])

    def test_presses_at_once_append_one_head(self):
        case = self.open_case()
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(self.cell.json(f"/api/cases/{case}/witness", {}, who="manager"))) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(status for status, _ in answers), [200, 409, 409, 409])
        self.assertEqual({body["error"] for status, body in answers if status == 409}, {"already_witnessed"})
        self.assertEqual(self.leaves(), 1)

    def test_a_log_that_holds_a_head_this_history_does_not_have_is_refused(self):
        case = self.open_case()
        events = self.cell.work.inspect(case)["events"]
        self.witness_log.append(sign_chain_head(case, [*events[:-1], dict(events[-1], hash="0" * 64)], self.seed))
        status, body = self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")
        self.assertEqual((status, body["error"]), (409, "witness_conflict"))
        self.assertIn("head_differs", body["detail"])
        self.assertEqual(self.leaves(), 1, "the witness was not asked to sign beside the conflicting head")
        # A history changed after signing: the stored head still matches, the bodies no longer hash into it.
        other = self.open_case()
        self.assertEqual(self.cell.json(f"/api/cases/{other}/witness", {}, who="manager")[0], 200)
        self.cell.json(f"/api/cases/{other}/proposals", {"action": "record_review", "args": {}}, who="scheduler")
        with closing(sqlite3.connect(self.cell.database)) as db, db:
            db.execute("UPDATE events SET body=? WHERE case_id=? AND seq=1", (canonical({"tampered": True}), other))
        self.assertEqual(self.cell.json(f"/api/cases/{other}/witness", {}, who="manager")[1]["error"], "witness_conflict")
        self.assertEqual(self.leaves(), 2)

    def test_the_events_judged_are_the_events_sent(self):
        """One read: an append between the judgement and the send is neither judged nor signed."""
        case = self.open_case()
        count = len(self.cell.work.inspect(case)["events"])
        judge = Work._witness_judgement
        def judged_then_appended(case_id, events, log):
            found = judge(case_id, events, log)
            self.cell.work.propose(case_id, "record_review", {}, "scheduler", ["operator", "compiled"])
            return found
        with unittest.mock.patch.object(Work, "_witness_judgement", staticmethod(judged_then_appended)):
            body = self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")[1]
        self.assertEqual(body["count"], count)
        self.assertGreater(len(self.cell.work.inspect(case)["events"]), count)

    def test_a_broken_chain_is_refused_by_this_server_before_the_witness(self):
        case = self.open_case()
        with closing(sqlite3.connect(self.cell.database)) as db, db:
            db.execute("UPDATE events SET body=? WHERE case_id=? AND seq=1", (canonical({"tampered": True}), case))
        self.assertEqual(self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")[1]["error"], "invalid_chain")
        self.assertEqual(self.leaves(), 0)

    def test_unknown_cases_and_a_silent_witness_are_refused_not_crashed(self):
        for path in ("/api/cases/no-such-case/witness", "/api/cases/%2e%2e/witness"):
            with self.subTest(path):
                self.assertEqual(self.cell.json(path, {}, who="manager"), (409, {"error": "unknown_case", "detail": "no-such-case" if "no-such" in path else ".."}))
        reader = TransparencyLog.open_readonly(self.log_path, self.public)
        self.addCleanup(reader.close)
        silent = WitnessClient(free_port(), self.public, str(self.log_path))
        self.addCleanup(silent.close)
        cell = Cell(Path(tempfile.mkdtemp(dir=self.log_path.parent)), 8999, reader, silent, "manager")
        self.addCleanup(cell.close)
        case = cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        self.assertEqual(cell.json(f"/api/cases/{case}/witness", {}, who="manager"),
                         (409, {"error": "witness_unavailable", "detail": "the witness did not answer on its port"}))
        # Something on the port that answers a frame that is not JSON text.
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        def answer_garbage():
            conn, _ = listener.accept()
            with conn:
                size = int.from_bytes(conn.recv(4), "big")
                while size > 0:
                    size -= len(conn.recv(min(size, 65536)) or b"x" * size)
                conn.sendall((2).to_bytes(4, "big") + b"\xff\xfe")
        threading.Thread(target=answer_garbage, daemon=True).start()
        garbled = WitnessClient(listener.getsockname()[1], self.public, str(self.log_path))
        self.addCleanup(garbled.close)
        cell = Cell(Path(tempfile.mkdtemp(dir=self.log_path.parent)), 8999, reader, garbled, "manager")
        self.addCleanup(cell.close)
        case = cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        self.assertEqual(cell.json(f"/api/cases/{case}/witness", {}, who="manager"),
                         (409, {"error": "witness_unavailable", "detail": "the witness's answer was unreadable"}))
        # No answer within the client's timeout: the witness may have signed, and the refusal says so.
        with unittest.mock.patch.object(garbled, "submit", side_effect=TimeoutError()):
            self.assertEqual(cell.json(f"/api/cases/{case}/witness", {}, who="manager"),
                             (409, {"error": "witness_unavailable", "detail": "the witness did not answer in time; it may still have signed, so read its log again"}))

    def test_each_request_is_counted_and_traced_with_its_actor(self):
        self.cell.work.trace = TraceLog(self.log_path.parent / "traces.jsonl")
        case = self.open_case()
        self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")
        self.cell.json(f"/api/cases/{case}/witness", {}, who="manager")
        self.cell.json(f"/api/cases/{case}/witness", {}, who="operator")
        self.cell.json("/api/cases/no-such-case/witness", {}, who="manager")
        self.cell.work.register_principal("person-1", "human", "lead", "local", ["manager"], token="token-lead")
        self.cell.tokens["lead"] = "token-lead"
        self.cell.json(f"/api/cases/{case}/witness", {}, who="lead")
        rows = [r for r in self.cell.work.trace.read() if r["name"] == "witness_request"]
        self.assertEqual([(r["case_id"], r["actor"], r["principal"], r["status"]) for r in rows],
                         [(case, "manager", None, "signed"), (case, "manager", None, "already_witnessed"),
                          ("no-such-case", "manager", None, "unknown_case"), (case, "lead", "person-1", "already_witnessed")],
                         "a role refusal never reaches Work")
        self.assertEqual(rows[0]["head"], self.cell.work.inspect(case)["events"][-1]["hash"])
        self.assertEqual(self.cell.work.metrics["witness_request"], 4)

    def test_the_console_is_told_who_may_ask(self):
        self.assertEqual(self.cell.json("/api/console", who="supplier")[1]["witness"], {"log": True, "request_role": "manager"})
        cell = Cell(Path(tempfile.mkdtemp(dir=self.log_path.parent)), 8999)
        self.addCleanup(cell.close)
        self.assertEqual(cell.json("/api/console")[1]["witness"], {"log": False, "request_role": None})
        case = cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        self.assertEqual(cell.json(f"/api/cases/{case}/witness", {}, who="manager")[1]["error"], "witness_not_configured")

    def test_serve_asks_only_when_both_variables_name_a_witness_beside_its_log(self):
        parser = argparse.ArgumentParser()
        log = TransparencyLog.open_readonly(self.log_path, self.public)
        self.addCleanup(log.close)
        def configured(reader, **names):
            environment = {k: v for k, v in os.environ.items() if not k.startswith("SYBERWORK_WITNESS_")}
            with unittest.mock.patch.dict(os.environ, {**environment, **names}, clear=True):
                return _witness_request(parser, reader)
        self.assertEqual(configured(log), (None, None))
        self.assertEqual(configured(None), (None, None))
        port = str(self.witness_port)
        for reader, names in ((log, {"SYBERWORK_WITNESS_PORT": port}), (log, {"SYBERWORK_WITNESS_ROLE": "manager"}),
                              (None, {"SYBERWORK_WITNESS_PORT": port, "SYBERWORK_WITNESS_ROLE": "manager"}),
                              (log, {"SYBERWORK_WITNESS_PORT": "0", "SYBERWORK_WITNESS_ROLE": "manager"}),
                              (log, {"SYBERWORK_WITNESS_PORT": "70000", "SYBERWORK_WITNESS_ROLE": "manager"}),
                              (log, {"SYBERWORK_WITNESS_PORT": "-1", "SYBERWORK_WITNESS_ROLE": "manager"}),
                              (log, {"SYBERWORK_WITNESS_PORT": port, "SYBERWORK_WITNESS_ROLE": "manager admin"}),
                              (log, {"SYBERWORK_WITNESS_PORT": port, "SYBERWORK_WITNESS_ROLE": "model"})):
            with self.subTest(sorted(names), log=reader is not None), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr"):
                configured(reader, **names)
        client, role = configured(log, SYBERWORK_WITNESS_PORT=port, SYBERWORK_WITNESS_ROLE="manager")
        self.addCleanup(client.close)
        self.assertEqual((client.port, client.public, role), (self.witness_port, self.public, "manager"))
        # make_server holds the same rule for a caller that builds it directly.
        users = {"m": {"hash": "x", "roles": ["manager"]}}
        for kwargs in ({"witness_client": client}, {"witness_role": "manager"}, {"witness_client": client, "witness_role": "manager"},
                       {"witness_log": log, "witness_client": client, "witness_role": ""},
                       {"witness_log": log, "witness_client": client, "witness_role": "compiled"}):
            with self.subTest(sorted(kwargs)), self.assertRaises(Rejected):
                make_server(self.cell.work, users, port=0, **kwargs)
        other = TransparencyLog.open_readonly(self.log_path, generate()[1])
        self.addCleanup(other.close)
        with self.assertRaises(Rejected):
            make_server(self.cell.work, users, port=0, witness_log=other, witness_client=client, witness_role="manager")


class ConsoleCertificates(CellCase):
    """GET /api/certificates: the certificates under a Build Thread home, each verified by serve under the configured key."""

    certificates = True

    def listing(self, who="operator"):
        status, body = self.cell.json("/api/certificates", who=who)
        self.assertEqual(status, 200, body)
        return body

    def rows(self):
        return {row["file"]: row for row in self.listing()["certificates"]}

    def folder(self) -> Path:
        return self.cert_home / "certificates"

    def journal(self) -> Path:
        return self.cert_home / "journal" / "threads" / f"{self.cert_payload['thread']}.jsonl"

    def test_the_route_requires_a_token_and_is_read_by_any_principal(self):
        self.assertEqual(self.cell.json("/api/certificates", who=None)[0], 401)
        expected = self.listing()
        for who in ("manager", "supplier", "planner", "admin"):
            with self.subTest(who):
                self.assertEqual(self.listing(who), expected)
        self.assertEqual(self.cell.json("/api/console", who="supplier")[1]["certificates"], {"configured": True})

    def test_a_certificate_signed_under_the_key_and_matching_its_journal_passes_both_layers(self):
        body = self.listing()
        self.assertEqual((body["configured"], body["public_keys"], body["total"], body["shown"]), (True, [PUBLIC.hex()], 2, 2))
        self.assertEqual((body["checked"], body["not_checked"]), (["signature", "journal"], ["repository"]))
        self.assertIn("syberlabs verify FILE --public KEY --repo PATH", body["note"])
        payload = self.cert_payload
        thread8 = payload["thread"][:8]
        rows = self.rows()
        self.assertEqual(sorted(rows), [f"{thread8}-c1.bare.json", f"{thread8}-c1.json"], "the .pub files are not listed")
        for name, form in ((f"{thread8}-c1.json", "statement"), (f"{thread8}-c1.bare.json", "bare")):
            with self.subTest(form):
                row = rows[name]
                self.assertEqual((row["verdict"], row["reason"], row["signature"], row["journal"], row["journal_present"], row["repository"]),
                                 ("pass", None, "ok", "ok", True, None))
                stated = row["statement"]
                self.assertEqual(stated["form"], form)
                self.assertEqual(stated["keyid"], PUBLIC.hex())
                self.assertEqual((stated["thread"], stated["candidate"], stated["objective"]),
                                 (payload["thread"], "c1", "Add a CSV export for report rows"))
                self.assertEqual(stated["result"], {k: payload["result"][k] for k in ("commit", "tree", "files")})
                self.assertEqual((stated["chain"], stated["contract"], stated["target_ref"]),
                                 (payload["chain"], payload["contract"], payload["admission"]["target_ref"]))
                self.assertEqual(stated["signing"], "in-process")
                self.assertEqual([c["name"] for c in stated["checks"]], [c["name"] for c in payload["evaluation"]["checks"]])
                self.assertNotIn("output_tail", json.dumps(stated))
        # The home's path is not disclosed, in either separator (json.dumps would double a Windows backslash).
        def strings(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, str):
                yield value
        for text in strings(body):
            for path in {str(self.cert_home), self.cert_home.as_posix(), str(self.cert_home.parent)}:
                self.assertNotIn(path, text)

    def test_another_key_or_changed_bytes_fail_and_state_nothing(self):
        """The .pub written beside a file is not read: a certificate under another key fails though its .pub matches it."""
        payload = self.cert_payload
        good = json.loads((self.folder() / f"{payload['thread'][:8]}-c1.bare.json").read_text(encoding="utf-8"))
        foreign = sign_dsse(good["payloadType"], base64.standard_b64decode(good["payload"]), OTHER_SEED)
        write_certificate(foreign, OTHER_PUBLIC, self.folder() / "foreign.json")
        changed = dict(payload, objective="Something else")
        tampered = {**good, "payload": base64.standard_b64encode(canonical(changed).encode()).decode("ascii")}
        (self.folder() / "tampered.json").write_text(json.dumps(tampered), encoding="utf-8")
        (self.folder() / "notjson.json").write_text("{not json", encoding="utf-8")
        (self.folder() / "toolarge.json").write_bytes(b" " * (1_048_576 + 1))
        (self.folder() / "nested.json").write_bytes(b"[" * 200_000)  # json.loads raises RecursionError, not ValueError
        rows = self.rows()
        expected = {"foreign.json": "signature_invalid", "tampered.json": "signature_invalid",
                    "notjson.json": "certificate_unreadable", "toolarge.json": "certificate_too_large",
                    "nested.json": "unverifiable"}
        for name, reason in expected.items():
            with self.subTest(name):
                row = rows[name]
                self.assertEqual((row["verdict"], row["reason"], row["statement"], row["journal"]), ("fail", reason, None, None))
        self.assertNotIn("Something else", json.dumps(rows), "unsigned content is never answered")
        self.assertEqual(sum(1 for row in rows.values() if row["verdict"] == "pass"), 2)

    def test_a_link_or_a_folder_is_refused_not_followed(self):
        os.mkdir(self.folder() / "folder.json")
        try:
            os.symlink(self.folder() / f"{self.cert_payload['thread'][:8]}-c1.json", self.folder() / "link.json")
        except (OSError, NotImplementedError):
            linked = False  # Windows without the symlink privilege
        else:
            linked = True
        rows = self.rows()
        self.assertEqual((rows["folder.json"]["verdict"], rows["folder.json"]["reason"]), ("fail", "not_a_regular_file"))
        if linked:
            self.assertEqual((rows["link.json"]["verdict"], rows["link.json"]["reason"], rows["link.json"]["statement"]),
                             ("fail", "not_a_regular_file", None))

    def test_the_journal_decides_and_its_absence_is_said_not_passed_over(self):
        name = f"{self.cert_payload['thread'][:8]}-c1.json"
        journal = self.journal().read_bytes()
        lines = journal.splitlines(keepends=True)
        # Fewer events than certified: the signature still holds, the journal layer does not.
        self.journal().write_bytes(b"".join(lines[:3]))
        row = self.rows()[name]
        self.assertEqual((row["verdict"], row["reason"], row["signature"], row["journal"]), ("fail", "chain_truncated", "ok", "chain_truncated"))
        self.assertIsNotNone(row["statement"], "what the key signed is shown beside the failure, not as certified")
        # No journal for the thread under this home: signature only, and the answer says the journal was not checked.
        self.journal().unlink()
        row = self.rows()[name]
        self.assertEqual((row["verdict"], row["signature"], row["journal"], row["journal_present"]), ("pass", "ok", None, False))
        # A journal that cannot be read safely (here a folder in its place; a FIFO or a device is refused the same way)
        # is never passed over as absent: the row fails as unreadable, after the signature-only pass above was cached.
        self.journal().mkdir()
        row = self.rows()[name]
        self.assertEqual((row["verdict"], row["reason"], row["signature"], row["journal"], row["journal_present"]),
                         ("fail", "journal_unreadable:not_a_regular_file", "ok", "journal_unreadable:not_a_regular_file", True))
        self.journal().rmdir()
        # A journal larger than serve will read is refused before the verifier reads it.
        self.journal().write_bytes(journal)
        with unittest.mock.patch.object(shelf_module, "MAX_JOURNAL_BYTES", len(journal) - 1):
            self.assertEqual(self.rows()[name]["reason"], "journal_unreadable:too_large")
        # The journal back as it was: both layers again. The verdict follows the bytes, not a cached answer.
        self.journal().write_bytes(journal)
        row = self.rows()[name]
        self.assertEqual((row["verdict"], row["journal"], row["journal_present"]), ("pass", "ok", True))

    def test_a_verdict_is_computed_once_per_input_bytes(self):
        journal = self.journal().read_bytes()
        with unittest.mock.patch.object(shelf_module, "verify_certificate", wraps=shelf_module.verify_certificate) as spy:
            first = self.listing()
            self.assertEqual(spy.call_count, 2)
            self.assertEqual(self.listing(), first)
            self.assertEqual(spy.call_count, 2, "a second read of unchanged files verifies nothing again")
            self.journal().write_bytes(self.journal().read_bytes() + b"\n")
            self.listing()
            self.assertEqual(spy.call_count, 4, "a changed journal is judged again")
            # A write between hashing the inputs and verifying is not cached under the earlier hash: the journal is
            # corrupted while each file is verified, then put back, and the next read verifies again and passes.
            self.journal().write_bytes(journal)
            self.shelf._cache.clear()
            real = shelf_module.verify_certificate
            def corrupting(*args, **kwargs):
                with open(self.journal(), "ab") as handle:
                    handle.write(b"{not a record}\n")
                return real(*args, **kwargs)
            spy.side_effect = corrupting
            self.assertTrue(all(row["verdict"] == "fail" for row in self.listing()["certificates"]))
            spy.side_effect = None
            self.journal().write_bytes(journal)
            calls = spy.call_count
            self.assertTrue(all(row["verdict"] == "pass" for row in self.listing()["certificates"]))
            self.assertEqual(spy.call_count, calls + 2, "the verdicts computed across the write were not kept")

    def test_the_read_writes_nothing(self):
        def snapshot():
            return {str(p.relative_to(self.cert_home.parent)): p.read_bytes() if p.is_file() else None
                    for p in sorted(self.cert_home.parent.rglob("*")) if ".git" not in p.parts}
        before = snapshot()
        with connect_db(self.cell.database) as db:
            dump = list(db.iterdump())
        self.listing()
        self.assertEqual(snapshot(), before)
        with connect_db(self.cell.database) as db:
            self.assertEqual(list(db.iterdump()), dump)

    def test_at_most_the_limit_is_verified_and_the_total_is_said(self):
        for index in range(CERTIFICATE_LIMIT):
            (self.folder() / f"zz-{index:03}.json").write_text("{}", encoding="utf-8")
        body = self.listing()
        self.assertEqual((body["total"], body["shown"], len(body["certificates"])), (CERTIFICATE_LIMIT + 2, CERTIFICATE_LIMIT, CERTIFICATE_LIMIT))
        self.assertEqual(sum(1 for row in body["certificates"] if row["verdict"] == "pass"), 2, "name order: the two genuine files come first")

    def test_without_a_home_the_route_says_so(self):
        cell = Cell(Path(tempfile.mkdtemp(dir=self.cert_home.parent)), 8999)
        self.addCleanup(cell.close)
        status, body = cell.json("/api/certificates")
        self.assertEqual(status, 200)
        self.assertEqual(body["configured"], False)
        self.assertIn("SYBERWORK_CERTIFICATE_HOME", body["note"])
        self.assertNotIn("certificates", body)
        self.assertEqual(cell.json("/api/console")[1]["certificates"], {"configured": False})

    def test_serve_reads_certificates_only_when_both_variables_name_them(self):
        parser = argparse.ArgumentParser()
        def configured(**names):
            environment = {k: v for k, v in os.environ.items() if not k.startswith("SYBERWORK_CERTIFICATE_")}
            with unittest.mock.patch.dict(os.environ, {**environment, **names}, clear=True):
                return _certificates(parser)
        self.assertIsNone(configured())
        home = str(self.cert_home)
        for names in ({"SYBERWORK_CERTIFICATE_HOME": home}, {"SYBERWORK_CERTIFICATE_PUBLIC": PUBLIC.hex()},
                      {"SYBERWORK_CERTIFICATE_HOME": home, "SYBERWORK_CERTIFICATE_PUBLIC": "not hex"},
                      {"SYBERWORK_CERTIFICATE_HOME": home, "SYBERWORK_CERTIFICATE_PUBLIC": "00"},
                      {"SYBERWORK_CERTIFICATE_HOME": str(self.journal()), "SYBERWORK_CERTIFICATE_PUBLIC": PUBLIC.hex()},
                      {"SYBERWORK_CERTIFICATE_HOME": home + "-missing", "SYBERWORK_CERTIFICATE_PUBLIC": PUBLIC.hex()}):
            with self.subTest(sorted(names)), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr"):
                configured(**names)
        shelf = configured(SYBERWORK_CERTIFICATE_HOME=home, SYBERWORK_CERTIFICATE_PUBLIC=PUBLIC.hex() + "\n")
        self.assertEqual((shelf.home, shelf.keys), (self.cert_home, (PUBLIC,)))


class ConsoleCertificateKeys(CellCase):
    """More than one trusted key: the new key and the old ones during a rotation. Each file passes under the key that
    signed it, is tried only under the keys its signatures could match, and the row names that key."""

    certificates = True
    certificate_keys = (OTHER_PUBLIC, PUBLIC)   # OTHER_PUBLIC the new key; the fixture's certificates are under the old one

    def folder(self) -> Path:
        return self.cert_home / "certificates"

    def rows(self):
        status, body = self.cell.json("/api/certificates")
        self.assertEqual(status, 200, body)
        return body, {row["file"]: row for row in body["certificates"]}

    def envelope(self, seed=OTHER_SEED, payload=None):
        good = json.loads((self.folder() / f"{self.cert_payload['thread'][:8]}-c1.bare.json").read_text(encoding="utf-8"))
        data = base64.standard_b64decode(good["payload"]) if payload is None else canonical(payload).encode()
        return sign_dsse(good["payloadType"], data, seed)

    def put(self, name, envelope):
        (self.folder() / name).write_text(json.dumps(envelope), encoding="utf-8")

    def test_each_listed_key_verifies_what_it_signed_and_the_row_names_it(self):
        self.put("rotated.json", self.envelope(OTHER_SEED))
        self.put("untrusted.json", self.envelope(THIRD_SEED))
        body, rows = self.rows()
        self.assertEqual(body["public_keys"], [OTHER_PUBLIC.hex(), PUBLIC.hex()], "in configured order")
        thread8 = self.cert_payload["thread"][:8]
        for name, key in ((f"{thread8}-c1.json", PUBLIC), (f"{thread8}-c1.bare.json", PUBLIC), ("rotated.json", OTHER_PUBLIC)):
            with self.subTest(name):
                row = rows[name]
                self.assertEqual((row["verdict"], row["signature"], row["journal"], row["journal_present"]), ("pass", "ok", "ok", True))
                self.assertEqual(row["statement"]["keyid"], key.hex())
        row = rows["untrusted.json"]
        self.assertEqual((row["verdict"], row["reason"], row["statement"]), ("fail", "signature_invalid", None))
        self.assertNotIn(THIRD_PUBLIC.hex(), json.dumps(body), "a key nobody configured is never named")

    def test_a_file_is_tried_only_under_the_keys_its_signatures_could_match(self):
        """verified_payload tries a signature only under the key its keyid names, or under every key when it names none."""
        unnamed = self.envelope(SEED)                    # the old key, keyid removed: tried under the new key, then the old
        del unnamed["signatures"][0]["keyid"]
        self.put("unnamed.json", unnamed)
        misnamed = self.envelope(OTHER_SEED)             # signed by the new key but naming the old: the old key only, so it fails
        misnamed["signatures"][0]["keyid"] = PUBLIC.hex()
        self.put("misnamed.json", misnamed)
        self.put("untrusted.json", self.envelope(THIRD_SEED))
        (self.folder() / "notjson.json").write_text("{not json", encoding="utf-8")
        shaped = {k: v for k, v in self.cert_payload.items() if k != "objective"}
        # A malformed payload, keyid removed: the failure is the signer's. Under the old key, the new key is tried first and
        # fails on the signature; under the new key, the run stops there and the old key is not tried.
        for name, seed in (("shape-old.json", SEED), ("shape-new.json", OTHER_SEED)):
            bad = self.envelope(seed, shaped)
            del bad["signatures"][0]["keyid"]
            self.put(name, bad)
        names = {path.read_bytes(): path.name for path in self.folder().glob("*.json")}
        tried = {}
        real = shelf_module.verify_certificate
        def spy(data, key, **kwargs):
            tried.setdefault(names[data], []).append(key)
            return real(data, key, **kwargs)
        with unittest.mock.patch.object(shelf_module, "verify_certificate", side_effect=spy):
            _, rows = self.rows()
        thread8 = self.cert_payload["thread"][:8]
        expected = {f"{thread8}-c1.json": ([PUBLIC], "pass", None), f"{thread8}-c1.bare.json": ([PUBLIC], "pass", None),
                    "unnamed.json": ([OTHER_PUBLIC, PUBLIC], "pass", None),
                    "misnamed.json": ([PUBLIC], "fail", "signature_invalid"),
                    "untrusted.json": ([OTHER_PUBLIC], "fail", "signature_invalid"),
                    "notjson.json": ([OTHER_PUBLIC], "fail", "certificate_unreadable")}
        for name, (keys, verdict, reason) in expected.items():
            with self.subTest(name):
                self.assertEqual(tried[name], keys)
                self.assertEqual((rows[name]["verdict"], rows[name]["reason"]), (verdict, reason))
        self.assertEqual(rows["unnamed.json"]["statement"]["keyid"], PUBLIC.hex())
        for name, keys in (("shape-old.json", [OTHER_PUBLIC, PUBLIC]), ("shape-new.json", [OTHER_PUBLIC])):
            with self.subTest(name):
                self.assertEqual(tried[name], keys)
                self.assertTrue(rows[name]["reason"].startswith("payload_shape"), rows[name])
                self.assertIsNone(rows[name]["statement"])

    def test_removing_a_key_stops_trusting_what_it_signed(self):
        """There is no revocation: a key is trusted until the deployment takes it off the list and restarts serve."""
        self.put("rotated.json", self.envelope(OTHER_SEED))
        verdicts = lambda shelf: {row["file"]: row["verdict"] for row in shelf.listing()["certificates"]}
        thread8 = self.cert_payload["thread"][:8]
        self.assertEqual(verdicts(CertificateShelf(self.cert_home, (OTHER_PUBLIC,))),
                         {f"{thread8}-c1.bare.json": "fail", f"{thread8}-c1.json": "fail", "rotated.json": "pass"})
        self.assertEqual(verdicts(CertificateShelf(self.cert_home, PUBLIC)),
                         {f"{thread8}-c1.bare.json": "pass", f"{thread8}-c1.json": "pass", "rotated.json": "fail"})

    def test_the_shelf_takes_one_to_eight_distinct_keys(self):
        self.assertEqual(CertificateShelf(self.cert_home, PUBLIC).keys, (PUBLIC,))
        eight = tuple(public_from_seed(hashlib.sha256(bytes([i])).digest()) for i in range(shelf_module.MAX_KEYS))
        self.assertEqual(CertificateShelf(self.cert_home, eight).keys, eight, "exactly MAX_KEYS keys are accepted")
        nine = (*eight, PUBLIC)
        for keys in ((), nine, (PUBLIC, PUBLIC), (PUBLIC, b"\0" * 31), (PUBLIC, PUBLIC.hex())):
            with self.subTest(len(keys)), self.assertRaises(Rejected):
                CertificateShelf(self.cert_home, keys)

    def test_a_key_anyone_could_sign_under_is_refused(self):
        """syberlabs.ed25519.verify takes any 32 bytes: under the identity point, R = B and s = 1 verify any message."""
        identity, order4 = bytes([1]) + bytes(31), bytes(32)
        forged = base64.standard_b64encode(ed25519._encode(ed25519._B) + (1).to_bytes(32, "little")).decode("ascii")
        self.assertTrue(ed25519.verify(identity, b"any statement", base64.standard_b64decode(forged)), "the forgery the check exists for")
        mixed = ed25519._encode(ed25519._decode(PUBLIC) + ed25519._decode(order4))     # a real key plus a point of order 4
        not_a_point = bytes([2]) + bytes(31)
        for name, key in (("identity", identity), ("order 4", order4), ("mixed order", mixed), ("not a point", not_a_point)):
            with self.subTest(name):
                self.assertFalse(shelf_module.usable_key(key))
                with self.assertRaises(Rejected):
                    CertificateShelf(self.cert_home, (PUBLIC, key))
        self.assertTrue(all(shelf_module.usable_key(key) for key in (PUBLIC, OTHER_PUBLIC, THIRD_PUBLIC)))

    def test_a_file_with_more_signatures_than_allowed_is_refused_unverified(self):
        """A key-less signature is tried under every key, so the count of signatures bounds what one file costs."""
        envelope = self.envelope(SEED)
        unnamed = dict(envelope["signatures"][0])
        del unnamed["keyid"]
        envelope["signatures"] = [unnamed] * (shelf_module.MAX_SIGNATURES + 1)
        self.put("many.json", envelope)
        envelope["signatures"] = [unnamed] * shelf_module.MAX_SIGNATURES
        self.put("two.json", envelope)
        with unittest.mock.patch.object(shelf_module, "verify_certificate", wraps=shelf_module.verify_certificate) as spy:
            _, rows = self.rows()
        self.assertEqual((rows["many.json"]["verdict"], rows["many.json"]["reason"], rows["many.json"]["statement"]),
                         ("fail", "too_many_signatures", None))
        self.assertEqual((rows["two.json"]["verdict"], rows["two.json"]["statement"]["keyid"]), ("pass", PUBLIC.hex()))
        self.assertEqual(spy.call_count, 2 + 2, "the two fixture files once each, two.json under both keys, many.json never")

    def test_narrowing_gives_the_verdict_of_trying_every_key(self):
        """Odd envelopes: the shelf's answer is the one trying every configured key in order would give."""
        good = self.envelope(SEED)
        sig = good["signatures"][0]
        bare = {k: v for k, v in sig.items() if k != "keyid"}
        odd = {
            "nondict.json": {**good, "signatures": [3, sig]},
            "null.json": {**good, "signatures": [{**sig, "keyid": None}]},
            "intkeyid.json": {**good, "signatures": [{**sig, "keyid": 7}]},
            "upper.json": {**good, "signatures": [{**sig, "keyid": PUBLIC.hex().upper()}]},
            "listkeyid.json": {**good, "signatures": [{**sig, "keyid": [PUBLIC.hex()]}]},
            "badsig.json": {**good, "signatures": [{**bare, "sig": "AAAA"}, {**sig, "keyid": OTHER_PUBLIC.hex()}]},
            "sigdict.json": {**good, "signatures": {"keyid": PUBLIC.hex(), "sig": sig["sig"]}},
            "sigstring.json": {**good, "signatures": "x"},
            "array.json": [good],
        }
        for name, envelope in odd.items():
            self.put(name, envelope)
        _, rows = self.rows()
        for name in odd:
            with self.subTest(name):
                data = (self.folder() / name).read_bytes()
                for key in self.certificate_keys:
                    report = shelf_module.verify_certificate(data, key, home=self.cert_home)
                    if report.reason != "signature_invalid":
                        break
                row = rows[name]
                self.assertEqual((row["verdict"], row["reason"], row["statement"]["keyid"] if row["statement"] else None),
                                 ("pass" if report.passed else "fail", report.reason, report.keyid if report.signature == "ok" else None))
        self.assertEqual({name for name in odd if rows[name]["verdict"] == "pass"}, {"nondict.json", "null.json"})

    def test_serve_reads_a_comma_separated_list_and_refuses_a_malformed_one(self):
        parser = argparse.ArgumentParser()
        def configured(public):
            environment = {k: v for k, v in os.environ.items() if not k.startswith("SYBERWORK_CERTIFICATE_")}
            names = {"SYBERWORK_CERTIFICATE_HOME": str(self.cert_home), "SYBERWORK_CERTIFICATE_PUBLIC": public}
            with unittest.mock.patch.dict(os.environ, {**environment, **names}, clear=True):
                return _certificates(parser)
        self.assertEqual(configured(f" {OTHER_PUBLIC.hex().upper()} , {PUBLIC.hex()}\n").keys, (OTHER_PUBLIC, PUBLIC))
        eight = [public_from_seed(hashlib.sha256(bytes([i])).digest()) for i in range(8)]
        self.assertEqual(configured(",".join(key.hex() for key in eight)).keys, tuple(eight), "exactly eight keys are accepted")
        nine = ",".join((bytes([i]) * 32).hex() for i in range(9))
        spaced = PUBLIC.hex()[:32] + " " + PUBLIC.hex()[32:]
        for public in (spaced, (bytes([1]) + bytes(31)).hex(), f"{PUBLIC.hex()},{bytes(32).hex()}", f"{PUBLIC.hex()},", f",{PUBLIC.hex()}", f"{PUBLIC.hex()},,{OTHER_PUBLIC.hex()}", f"{PUBLIC.hex()};{OTHER_PUBLIC.hex()}",
                       f"{PUBLIC.hex()} {OTHER_PUBLIC.hex()}", f"{PUBLIC.hex()},{PUBLIC.hex()}", f"{PUBLIC.hex()},{PUBLIC.hex().upper()}",
                       f"{PUBLIC.hex()},00", nine):
            with self.subTest(public[:20]), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr"):
                configured(public)


class ConsoleCertificateRevocation(CellCase):
    """SYBERWORK_CERTIFICATE_REVOKED: a certificate whose digest the deployment lists fails as revoked under every key,
    and still shows what it states. The digest is of the signed payload, so no rewrite of the file escapes it."""

    certificates = True
    certificate_keys = (OTHER_PUBLIC, PUBLIC)

    def folder(self) -> Path:
        return self.cert_home / "certificates"

    def good(self) -> dict:
        return json.loads((self.folder() / f"{self.cert_payload['thread'][:8]}-c1.bare.json").read_text(encoding="utf-8"))

    def put(self, name, envelope, **dump):
        (self.folder() / name).write_text(json.dumps(envelope, **dump), encoding="utf-8")

    def reissued(self) -> dict:
        """The same candidate certified again, later: another certificate, under the new key."""
        payload = dict(self.cert_payload, issued_at=self.cert_payload["issued_at"] + 1)
        return sign_dsse(self.good()["payloadType"], jcs_bytes(payload), OTHER_SEED)

    def digest(self, name=None) -> str:
        """The SHA-256 of the payload as signed: the bare form's payload bytes, or the JCS bytes of a statement's predicate."""
        if name is None:
            return hashlib.sha256(base64.standard_b64decode(self.good()["payload"])).hexdigest()
        statement = json.loads(base64.standard_b64decode(json.loads((self.folder() / name).read_text(encoding="utf-8"))["payload"]))
        return hashlib.sha256(jcs_bytes(statement["predicate"])).hexdigest()

    def statement(self) -> str:
        return f"{self.cert_payload['thread'][:8]}-c1.json"

    def rows(self, shelf):
        body = shelf.listing()
        return body, {row["file"]: row for row in body["certificates"]}

    def test_the_digest_is_of_the_signed_payload_whatever_key_or_form_signed_it(self):
        good = self.good()
        self.put("rotated.json", sign_dsse(good["payloadType"], base64.standard_b64decode(good["payload"]), OTHER_SEED))
        self.put("reissued.json", self.reissued())
        self.put("untrusted.json", sign_dsse(good["payloadType"], base64.standard_b64decode(good["payload"]), THIRD_SEED))
        status, body = self.cell.json("/api/certificates")
        self.assertEqual((status, body["revoked"]), (200, []))
        rows = {row["file"]: row for row in body["certificates"]}
        thread8 = self.cert_payload["thread"][:8]
        digest = self.digest()
        self.assertEqual(digest, shelf_module.certificate_digest(self.cert_payload))
        for name in (f"{thread8}-c1.bare.json", "rotated.json"):
            with self.subTest(name):
                self.assertEqual((rows[name]["verdict"], rows[name]["digest"]), ("pass", digest))
        # The statement form signs a wrapper; its digest is its predicate's. The fixture certified it in a second run of
        # certify, at another issued_at, so it is another certificate.
        self.assertEqual((rows[self.statement()]["verdict"], rows[self.statement()]["digest"]), ("pass", self.digest(self.statement())))
        self.assertNotEqual(self.digest(self.statement()), digest)
        self.assertEqual(rows["reissued.json"]["verdict"], "pass")
        self.assertNotEqual(rows["reissued.json"]["digest"], digest, "another issued_at is another certificate")
        self.assertIsNone(rows["untrusted.json"]["digest"], "a file no configured key signed has no digest")

    def test_a_listed_digest_fails_as_revoked_under_every_key_however_the_file_is_written(self):
        good = self.good()
        signed = base64.standard_b64decode(good["payload"])
        self.put("rotated.json", sign_dsse(good["payloadType"], signed, OTHER_SEED))
        self.put("reissued.json", self.reissued())
        # A folder writer rewrites a revoked file: indented, keys reordered, the keyid dropped. The signed bytes are the same.
        unnamed = {k: v for k, v in good["signatures"][0].items() if k != "keyid"}
        self.put("rewritten.json", {"signatures": [unnamed], "payloadType": good["payloadType"], "payload": good["payload"]}, indent=2)
        # The statement's predicate signed in the bare form: another file and another form, the same certificate.
        predicate = json.loads(base64.standard_b64decode(json.loads((self.folder() / self.statement()).read_text(encoding="utf-8"))["payload"]))["predicate"]
        self.put("unwrapped.json", sign_dsse(good["payloadType"], jcs_bytes(predicate), SEED))
        digest, statement = self.digest(), self.digest(self.statement())
        shelf = CertificateShelf(self.cert_home, self.certificate_keys, (bytes(32).hex(), digest, statement))
        body, rows = self.rows(shelf)
        self.assertEqual(body["revoked"], [bytes(32).hex(), digest, statement], "in configured order")
        thread8 = self.cert_payload["thread"][:8]
        for name, key, listed in ((self.statement(), PUBLIC, statement), (f"{thread8}-c1.bare.json", PUBLIC, digest),
                                  ("rotated.json", OTHER_PUBLIC, digest), ("rewritten.json", PUBLIC, digest),
                                  ("unwrapped.json", PUBLIC, statement)):
            with self.subTest(name):
                row = rows[name]
                self.assertEqual((row["verdict"], row["reason"], row["signature"], row["journal"], row["journal_present"], row["digest"]),
                                 ("fail", "revoked", "ok", "ok", True, listed))
                self.assertEqual((row["statement"]["keyid"], row["statement"]["candidate"]), (key.hex(), "c1"), "what was revoked is shown")
        self.assertEqual((rows["reissued.json"]["verdict"], rows["reissued.json"]["reason"]), ("pass", None),
                         "revoking a certificate is not revoking its key or its candidate")
        # Revocation wins over a journal failure too: the row names revoked, not the journal.
        journal = self.cert_home / "journal" / "threads" / f"{self.cert_payload['thread']}.jsonl"
        journal.write_bytes(b"\n".join(journal.read_bytes().splitlines()[:2]) + b"\n")
        _, rows = self.rows(shelf)
        self.assertEqual((rows["rotated.json"]["reason"], rows["rotated.json"]["journal"]), ("revoked", "chain_truncated"))
        self.assertEqual(rows["reissued.json"]["reason"], "chain_truncated")

    def test_revocation_holds_on_a_verdict_read_from_the_cache(self):
        shelf = CertificateShelf(self.cert_home, self.certificate_keys, (self.digest(), self.digest(self.statement())))
        with unittest.mock.patch.object(shelf_module, "verify_certificate", wraps=shelf_module.verify_certificate) as spy:
            first = self.rows(shelf)[1]
            calls = spy.call_count
            again = self.rows(shelf)[1]
            self.assertEqual(spy.call_count, calls, "the second read is served from the cache")
        self.assertEqual(again, first)
        self.assertTrue(all(row["reason"] == "revoked" for row in again.values()), again)

    def test_the_route_answers_the_configured_list(self):
        self.assertEqual(self.shelf.revoked, (), "nothing is revoked unless the deployment lists it")
        cell = Cell(Path(tempfile.mkdtemp(dir=self.cert_home.parent)), 8999, certificates=CertificateShelf(self.cert_home, PUBLIC, (self.digest(),)))
        self.addCleanup(cell.close)
        status, body = cell.json("/api/certificates", who="supplier")
        self.assertEqual((status, body["revoked"], body["public_keys"]), (200, [self.digest()], [PUBLIC.hex()]))
        rows = {row["file"]: row["reason"] for row in body["certificates"]}
        self.assertEqual(rows, {f"{self.cert_payload['thread'][:8]}-c1.bare.json": "revoked", self.statement(): None})

    def test_the_shelf_takes_up_to_the_limit_of_distinct_lowercase_digests(self):
        many = tuple(hashlib.sha256(bytes([i % 256, i // 256])).hexdigest() for i in range(shelf_module.MAX_REVOKED))
        self.assertEqual(CertificateShelf(self.cert_home, PUBLIC, many).revoked, many, "exactly MAX_REVOKED digests are accepted")
        digest = self.digest()
        for revoked in ((*many, digest), (digest, digest), (digest.upper(),), (digest[:63],), (digest + "0",), (" " + digest,),
                        (bytes.fromhex(digest),), ("",)):
            with self.subTest(repr(revoked[-1])[:20]), self.assertRaises(Rejected):
                CertificateShelf(self.cert_home, PUBLIC, revoked)

    def test_serve_reads_a_comma_separated_revocation_list_and_refuses_a_malformed_one(self):
        parser = argparse.ArgumentParser()
        digest, other = self.digest(), hashlib.sha256(b"another certificate").hexdigest()
        def configured(revoked=None, home=str(self.cert_home), public=PUBLIC.hex()):
            environment = {k: v for k, v in os.environ.items() if not k.startswith("SYBERWORK_CERTIFICATE_")}
            names = {k: v for k, v in (("SYBERWORK_CERTIFICATE_HOME", home), ("SYBERWORK_CERTIFICATE_PUBLIC", public),
                                       ("SYBERWORK_CERTIFICATE_REVOKED", revoked)) if v is not None}
            with unittest.mock.patch.dict(os.environ, {**environment, **names}, clear=True):
                return _certificates(parser)
        self.assertEqual(configured().revoked, ())
        self.assertEqual(configured("").revoked, ())
        self.assertEqual(configured(f" {digest.upper()} , {other}\n").revoked, (digest, other))
        many = ",".join(hashlib.sha256(bytes([i % 256, i // 256])).hexdigest() for i in range(shelf_module.MAX_REVOKED + 1))
        for revoked in (" ", f"{digest},", f",{digest}", f"{digest},,{other}", f"{digest};{other}", f"{digest} {other}",
                        f"{digest},{digest}", f"{digest},{digest.upper()}", digest[:63], digest[:32] + " " + digest[32:], many):
            with self.subTest(revoked[:20]), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr"):
                configured(revoked)
        # A revocation list with no certificates to apply it to is a mistake, not a configuration.
        with self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr"):
            configured(digest, home=None, public=None)


class ConsolePreview(CellCase):
    """POST /api/cases/{id}/preview: what proposing would record now (Work.preview_admission). It records nothing."""

    def case(self):
        return self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]

    def dump(self):
        with connect_db(self.cell.database) as db:
            return list(db.iterdump())

    def assert_preview_is_what_propose_records(self, case, body, who):
        """The preview, then the proposal made at once: same status, reason and deciding rule as written."""
        status, preview = self.cell.json(f"/api/cases/{case}/preview", body, who=who)
        self.assertEqual(status, 200, preview)
        args = body["args"] if "args" in body else preview["args"]
        status, made = self.cell.json(f"/api/cases/{case}/proposals", {"action": body["action"], "args": args, "origin": body.get("origin", "human")}, who=who)
        self.assertEqual(status, 200, made)
        written = {row["seq"]: row["rule"] for row in self.cell.work.side_channel(case)}
        decision = self.cell.work.inspect(case)["events"][-1]
        self.assertEqual(decision["kind"], "decision")
        self.assertEqual((preview["status"], preview["reason"], preview["rule"]),
                         (made["decision"]["status"], made["decision"]["reason"], written[decision["seq"]]))
        return preview, made

    def test_preview_requires_a_bearer_token(self):
        case = self.case()
        status, body = self.cell.json(f"/api/cases/{case}/preview", {"action": "record_review"}, who=None)
        self.assertEqual((status, body["error"]), (409, "unauthorized"), "a refused POST answers 409, as every POST here does")

    def test_preview_is_what_propose_then_records(self):
        work, case = self.cell.work, self.case()
        preview, _ = self.assert_preview_is_what_propose_records(case, {"action": "issue_order", "args": {}}, "operator")
        self.assertEqual((preview["status"], preview["reason"], preview["rule"]), ("denied", "amount_required", "limits.amount"))
        self.assertEqual(preview["provenance"]["module"], "syberlabs/admission.py")
        self.assertEqual(preview["provenance"]["symbol"], "limits_amount")
        # Arguments present but not an object are admitted as given, as propose admits them; only a missing key binds.
        for args in (None, [1], "x"):
            preview, _ = self.assert_preview_is_what_propose_records(case, {"action": "record_review", "args": args}, "operator")
            self.assertEqual((preview["reason"], preview["bound"], preview["args"]), ("invalid_proposal_shape", False, args))
        self.assert_preview_is_what_propose_records(case, {"action": "record_review", "args": {"part_number": "P-104"}}, "operator")
        work.observe(case, "part_number", "P-104", "inventory", "inventory-v1", "operator", verified=True)
        preview, _ = self.assert_preview_is_what_propose_records(case, {"action": "record_review"}, "supplier")
        self.assertEqual(preview["reason"], "actor_role_missing")
        preview, made = self.assert_preview_is_what_propose_records(case, {"action": "record_review", "origin": "compiled"}, "scheduler")
        self.assertEqual((preview["status"], preview["bound"], preview["args"]), ("allowed", True, {"part_number": "P-104"}))
        self.cell.json(f"/api/cases/{case}/commit", {"proposal_id": made["proposal"]["id"]}, who="scheduler")
        work.observe(case, "quote", {"id": "Q-7", "price": 10, "part_number": "P-104", "supplier": "s"}, "supplier", "supplier-v1", "operator", verified=True)
        preview, _ = self.assert_preview_is_what_propose_records(case, {"action": "issue_order"}, "operator")
        self.assertEqual((preview["status"], preview["reason"], preview["rule"]), ("needs_approval", "approval_required:manager", "approval.required"))
        # The bound arguments are the ones the compiled step binds from the same events.
        compiled = self.cell.json(f"/api/cases/{case}/compiled", {}, who="scheduler")[1]
        self.assertEqual(preview["args"], compiled["proposal"]["args"])
        preview, _ = self.assert_preview_is_what_propose_records(case, {"action": "issue_order", "args": {**preview["args"], "amount": 20000}}, "operator")
        self.assertEqual(preview["reason"], "amount_exceeds_limit")
        head = work.inspect(case)["events"][-3]
        self.assertEqual(preview["head"], {"seq": head["seq"], "hash": head["hash"]}, "the head the answer was computed against")
        self.assertEqual((preview["written"], preview["policy_version"], preview["origin"]), (False, 1, "human"))

    def test_each_preview_is_counted_and_traced_though_no_event_records_it(self):
        case = self.case()
        recorded = []
        self.cell.work.trace = type("Trace", (), {"record": lambda _self, name, **fields: recorded.append((name, fields))})()
        self.addCleanup(setattr, self.cell.work, "trace", None)
        before = self.cell.work.metrics.get("admission_preview", 0)
        self.cell.json(f"/api/cases/{case}/preview", {"action": "issue_order", "args": {"amount": 30000}}, who="manager")
        self.assertEqual(self.cell.work.metrics["admission_preview"], before + 1)
        self.assertEqual(recorded, [("admission_preview", {"case_id": case, "actor": "manager", "action": "issue_order", "status": "denied",
                                                           "reason": "actor_role_missing", "rule": "actor.role"})])

    def test_a_contract_binding_that_is_not_a_string_is_refused_not_crashed(self):
        contract = json.loads((EXAMPLES / "contract.json").read_text(encoding="utf-8"))
        contract.update(id="odd-bindings", compiled_path=["record_review"])
        contract["actions"]["record_review"]["arguments"] = {"part_number": 5}
        contract["acceptance"] = [{"id": "reviewed", "kind": "effect", "action": "record_review"}]
        self.cell.work.install_contract(contract)
        case = self.cell.json("/api/cases", {"contract_id": "odd-bindings", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        self.cell.work.observe(case, "part_number", "P-104", "inventory", "inventory-v1", "operator", verified=True)
        for path, body, who in ((f"/api/cases/{case}/preview", {"action": "record_review"}, "supplier"), (f"/api/cases/{case}/compiled", {}, "scheduler")):
            with self.subTest(path):
                self.assertEqual(self.cell.json(path, body, who=who), (409, {"error": "invalid_binding", "detail": "contract arguments must map each parameter to a binding string"}))

    def test_preview_appends_nothing_for_anyone(self):
        case = self.case()
        self.cell.work.observe(case, "part_number", "P-104", "inventory", "inventory-v1", "operator", verified=True)
        before, events = self.dump(), self.cell.work.inspect(case)["events"]
        bodies = ({"action": "record_review"}, {"action": "issue_order"}, {"action": "issue_order", "args": {"amount": 5}},
                  {"action": "record_review", "origin": "compiled"}, {"action": "record_review", "origin": "elsewhere"},
                  {"action": "absent", "args": {}}, {"action": "absent"}, {"args": {}}, {"action": "record_review", "args": []})
        for body in bodies:
            for who in ("operator", "scheduler", "manager", "planner", "supplier", None):
                self.cell.call(f"/api/cases/{case}/preview", body, who=who)
        self.assertEqual(self.cell.work.inspect(case)["events"], events)
        self.assertEqual(self.dump(), before, "no table changed, including event_side and reservations")
        self.assertTrue(self.cell.work.verify_chain(case))

    def test_preview_checks_the_origin_as_propose_does_and_refuses_without_crashing(self):
        case = self.case()
        for body, who, code in (({"action": "record_review", "origin": "elsewhere"}, "operator", "invalid_origin"),
                                ({"action": "record_review", "origin": "compiled"}, "operator", "origin_denied"),
                                ({"action": "record_review", "origin": "model"}, "scheduler", "origin_denied"),
                                ({"action": "record_review"}, "operator", "missing_fact"),
                                ({"action": "absent"}, "operator", "action_not_in_contract"),
                                ({"args": {}}, "operator", "invalid_request")):
            with self.subTest(body=body, who=who):
                status, answer = self.cell.json(f"/api/cases/{case}/preview", body, who=who)
                self.assertEqual((status, answer["error"]), (409, code), answer)
                if body.get("origin") in ("elsewhere", "compiled", "model"):
                    self.assertEqual(self.cell.json(f"/api/cases/{case}/proposals", {**body, "args": {}}, who=who)[1]["error"], code)
        for case_id in ("no-such-case", ".."):
            with self.subTest(case_id):
                status, answer = self.cell.json(f"/api/cases/{case_id}/preview", {"action": "record_review", "args": {}})
                self.assertEqual(status, 409, answer)
                self.assertNotEqual(answer["error"], "internal_error")
        status, answer = self.cell.json(f"/api/cases/{case}/preview", {"action": "absent", "args": {}})
        self.assertEqual((status, answer["reason"], answer["rule"]), (200, "action_not_in_contract", "contract.action_listed"))

    def test_preview_reads_other_cases_reservations_and_reserves_nothing(self):
        """An economic action: the budget another case holds is counted; the preview itself holds none."""
        work = self.cell.work
        closed = f"http://127.0.0.1:{free_port()}"
        work.install_action("buy_data", {"kind": "economic_http", "url": closed + "/settle", "status_url": closed + "/status/{key}",
                                         "rail": "usdc_service", "asset": "USDC:base", "counterparty": "vendor-A",
                                         "operation": "purchase_capability", "no_write_statuses": [412]})
        work.install_contract({"id": "research", "version": 1, "inputs": {"subject": "string"}, "actions": {"buy_data": {
            "required_facts": [{"key": "quote", "source": "vendor", "verified": True, "max_age_seconds": 3600}],
            "arguments": {"amount_units": "fact:quote.units"}, "max_amount_units": "1000000"}},
            "acceptance": [{"id": "data", "kind": "effect", "action": "buy_data"}]})
        work.install_policy({"version": 2, "actions": {**json.loads((EXAMPLES / "policy.json").read_text())["actions"], "buy_data": {
            "roles": ["operator"], "economic": {"budget_id": "research-q3", "budget_units": "1500000", "max_amount_units": "1000000",
                                                "asset": "USDC:base", "rail": "usdc_service", "counterparties": ["vendor-A"]}}}})

        def opened():
            case = work.create_case("research", 1, {"subject": "chips"}, "operator")
            evidence = work.observe(case, "quote", {"units": "900000"}, "vendor", "q1", "vendor", verified=True)["hash"]
            return case, {"operation": "purchase_capability", "amount_units": "900000", "asset": "USDC:base", "counterparty": "vendor-A",
                          "purpose": "shipment evidence", "evidence": [evidence], "expires_at": time.time() + 600}
        first, args = opened()
        preview = self.cell.json(f"/api/cases/{first}/preview", {"action": "buy_data", "args": args})[1]
        self.assertEqual(preview["status"], "allowed", preview)
        before = self.dump()
        for _ in range(3):
            self.assertEqual(self.cell.json(f"/api/cases/{first}/preview", {"action": "buy_data", "args": args})[1]["status"], "allowed")
        self.assertEqual(self.dump(), before, "a preview of an economic action reserves nothing")
        made = work.propose(first, "buy_data", args, "operator", ["operator"])
        self.assertEqual(work.commit(first, made["proposal"]["id"], "operator")["status"], "unknown", "the settlement is unreachable; 900000 stays reserved")
        second, args = opened()
        preview, _ = self.assert_preview_is_what_propose_records(second, {"action": "buy_data", "args": args}, "operator")
        self.assertEqual((preview["status"], preview["reason"], preview["rule"]), ("denied", "economic_budget_exceeded", "economic.reserve"))


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
                "/metrics/proposals_denied_total", "/metrics/work_items", f"/api/cases/{case}/side", f"/metrics/work_items/{case}",
                f"/api/cases/{case}/witness", "/api/certificates"]
        proposal = self.cell.json(f"/api/cases/{case}/proposals", {"action": "record_review", "args": {}}, who="admin")[1]["proposal"]["id"]
        gets += [f"/api/cases/{case}/proposals/{proposal}/explain?when={when}" for when in ("recorded", "now")]
        for path in gets:
            with self.subTest(path):
                self.assertEqual(self.cell.call(path, who="admin")[0], 200, path)
        app = read_text(STATIC / "app.js")
        self.assertIn("'/proposals/' + encodeURIComponent(p.id) + '/explain?when=' + when", app)
        self.assertNotIn("not exposed over HTTP", app)
        # The History reads build their URL from the case being drawn (headRead), not from the selection.
        self.assertIn("headRead('side', id => '/api/cases/' + encodeURIComponent(id) + '/side', info)", app)
        self.assertIn("headRead('witness', id => '/api/cases/' + encodeURIComponent(id) + '/witness', info)", app)
        self.assertIn("headRead('work_item', id => '/metrics/work_items/' + encodeURIComponent(id), info)", app)
        self.assertIn("api(route(info.case.id))", app)
        self.assertIn("api('/api/certificates')", app)
        # The admission preview and the witness request post to the case being drawn, like the History reads.
        self.assertIn("api('/api/cases/' + encodeURIComponent(info.case.id) + '/preview', body)", app)
        self.assertIn("api('/api/cases/' + encodeURIComponent(info.case.id) + '/witness', {})", app)
        self.assertNotEqual(self.cell.json(f"/api/cases/{case}/witness", {}, who="admin")[1].get("error"), "unknown_route")
        self.assertEqual(self.cell.json(f"/api/cases/{case}/preview", {"action": "record_review", "args": {}}, who="admin")[0], 200)
        for operation in ("refresh", "facts", "proposals", "compiled", "suggest", "approve", "commit", "signoff", "reconcile",
                          "replay", "cancel", "resolution-request", "resolution-resolve", "resolution-escalate", "candidates", "evaluations"):
            with self.subTest(operation):
                self.assertIn(f"casePath('{operation}')", app)
                status, body = self.cell.json(f"/api/cases/{case}/{operation}", {}, who="admin")
                self.assertNotEqual(body.get("error"), "unknown_route", operation)


class ConsoleModel(CellCase):
    witness = "server"
    certificates = True

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
        parity["read_scope"] = {"policy_readers": sorted(POLICY_READERS), "automation_roles": sorted(AUTOMATION_ROLES)}
        # What this server answers for a denied proposal, read by model.js as the console reads it.
        case = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        denied = self.cell.json(f"/api/cases/{case}/proposals", {"action": "issue_order", "args": {}})[1]["proposal"]["id"]
        parity["explain"] = [{"body": self.cell.json(f"/api/cases/{case}/proposals/{denied}/explain?when={when}")[1], "when": when,
                              "rule": "limits.amount", "provenance": "syberlabs/admission.py:"} for when in ("recorded", "now")]
        # The side records and the work item of the same case, as GET .../side and /metrics/work_items/{case} answer them.
        parity["work_metrics"] = list(METRICS)
        parity["side"] = {"events": self.cell.json(f"/api/cases/{case}")[1]["events"], "rows": self.cell.json(f"/api/cases/{case}/side")[1]}
        item_body = self.cell.json(f"/metrics/work_items/{case}")[1]
        item = item_body["items"][0]
        cells = [[name, _cell(Metric(None if item[name]["value"] == UNKNOWN else item[name]["value"]), money=name == "cost")] for name in METRICS]
        parity["work_item"] = {"case": case, "body": item_body, "cells": cells}
        # What POST .../preview answers on a second case, before and after its first fact.
        fresh = self.cell.json("/api/cases", {"contract_id": "purchase-order", "version": 1, "inputs": {"part_number": "P-104", "quantity": 2}})[1]["id"]
        unbound = self.cell.json(f"/api/cases/{fresh}/preview", {"action": "issue_order", "args": {}})[1]
        self.cell.work.observe(fresh, "part_number", "P-104", "inventory", "inventory-v1", "operator", verified=True)
        bound = self.cell.json(f"/api/cases/{fresh}/preview", {"action": "record_review"})[1]
        parity["preview"] = [{"body": unbound, "expected": ["denied", "amount_required", "limits.amount", False, 1]},
                             {"body": bound, "expected": ["allowed", "all_checks_passed", "admission.passed", True, 2]}]
        # What GET .../witness answers once the witness signed this case's head and one event came after it, and what
        # GET /api/console says about asking it.
        self.assertEqual(self.cell.json(f"/api/cases/{fresh}/witness", {}, who="manager")[0], 200)
        parity["console"] = self.cell.json("/api/console")[1]
        self.cell.json(f"/api/cases/{fresh}/proposals", {"action": "record_review", "args": {}}, who="scheduler")
        parity["witness"] = {"events": self.cell.json(f"/api/cases/{fresh}")[1]["events"], "body": self.cell.json(f"/api/cases/{fresh}/witness")[1]}
        self.assertEqual(parity["witness"]["body"]["witnessed_through"], len(parity["witness"]["events"]) - 2)
        # What GET /api/certificates answers for the two genuine forms and a file signed under another key.
        good = json.loads((self.cert_home / "certificates" / f"{self.cert_payload['thread'][:8]}-c1.json").read_text(encoding="utf-8"))
        foreign = sign_dsse(good["payloadType"], base64.standard_b64decode(good["payload"]), OTHER_SEED)
        write_certificate(foreign, OTHER_PUBLIC, self.cert_home / "certificates" / "foreign.json")
        thread8 = self.cert_payload["thread"][:8]
        parity["certificates"] = {"body": self.cell.json("/api/certificates")[1], "count": self.cert_payload["chain"]["count"],
                                  "expected": [[f"{thread8}-c1.bare.json", "certified", "verified"], [f"{thread8}-c1.json", "certified", "verified"],
                                               ["foreign.json", "not_signed", "denied"]]}
        # And what a shelf over the same home answers when it trusts the foreign key as the new one and PUBLIC as the old.
        parity["certificates"]["rotated"] = {
            "body": CertificateShelf(self.cert_home, (OTHER_PUBLIC, PUBLIC)).listing(),
            "expected": [[f"{thread8}-c1.bare.json", "certified", 2], [f"{thread8}-c1.json", "certified", 2], ["foreign.json", "certified", 1]]}
        # And one that also revokes the statement form's certificate, which foreign.json re-signs under the new key.
        bare = json.loads((self.cert_home / "certificates" / f"{thread8}-c1.bare.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(base64.standard_b64decode(bare["payload"])).hexdigest()
        statement = hashlib.sha256(jcs_bytes(json.loads(base64.standard_b64decode(good["payload"]))["predicate"])).hexdigest()
        parity["certificates"]["revoked"] = {
            "body": CertificateShelf(self.cert_home, (OTHER_PUBLIC, PUBLIC), (statement,)).listing(),
            "expected": [[f"{thread8}-c1.bare.json", "certified", 2, digest], [f"{thread8}-c1.json", "revoked", 2, statement],
                         ["foreign.json", "revoked", 1, statement]]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
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
    witness = "server"
    certificates = True
    # A rotation: serve trusts a new key (OTHER_PUBLIC) and the old one (PUBLIC) the fixture's certificates are under.
    certificate_keys = (OTHER_PUBLIC, PUBLIC)

    def test_the_purchase_order_path_completes_in_a_browser(self):
        # The strict suite allows only the inspector's exact reasons for a missing browser tool.
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        root = playwright_root()
        if root is None:
            if shutil.which("npm") is None:
                self.skipTest("npm is not installed")
            self.skipTest("playwright for node is not installed")
        # One certificate signed under the new key, beside the two the fixture signed under the old one.
        good = json.loads((self.cert_home / "certificates" / f"{self.cert_payload['thread'][:8]}-c1.bare.json").read_text(encoding="utf-8"))
        rotated = sign_dsse(good["payloadType"], base64.standard_b64decode(good["payload"]), OTHER_SEED)
        (self.cert_home / "certificates" / "rotated.json").write_text(json.dumps(rotated), encoding="utf-8")
        # And one the same candidate was certified with again, later, under the new key, which the deployment revokes. Its
        # digest exists only now, so the served shelf's list is set here, before anything is read.
        withdrawn = dict(self.cert_payload, issued_at=self.cert_payload["issued_at"] + 1)
        (self.cert_home / "certificates" / "withdrawn.json").write_text(json.dumps(sign_dsse(good["payloadType"], jcs_bytes(withdrawn), OTHER_SEED)), encoding="utf-8")
        revoked = shelf_module.certificate_digest(withdrawn)
        self.shelf.revoked = (revoked,)
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
        # History: the JCS side records, this case's work item and the witness log, read from the server; a case has no certificate.
        self.assertRegex(report["side_summary"], r"(\d+) of \1 events carry a side record · 2 of 2 decisions name their deciding rule", report["side_summary"])
        self.assertNotIn("have no side record", report["side_summary"])
        self.assertIn("JCS", report["side_row"])
        self.assertIn("same as the chain hash", report["side_row"])
        cells = dict(report["work_item"])
        self.assertEqual(list(cells), list(METRICS), report["work_item"])
        self.assertIn("unknown", cells["cost"], "no model call is metered for a purchase order")
        self.assertIn("no pull request opened", cells["delivered"])
        self.assertEqual(report["certificate_buttons"], [["Open certificates in Studio", False]])
        self.assertIn("a case is not a Build Thread and has none", report["certificate_text"])
        self.assertIn("Nothing on this case is drawn as certified", report["certificate_text"])
        # Studio → Certificates: both genuine forms pass the signature and the journal under the old key, the rotated file
        # under the new one, and each row names its key; the revoked file reads revoked, beside what it states and its
        # digest. The repository is named as not checked.
        thread8 = self.cert_payload["thread"][:8]
        self.assertEqual([state for state, _ in report["certificate_rows"]], ["certified"] * 3 + ["revoked"], report["certificate_rows"])
        for (_, row), name, key, verdict in zip(report["certificate_rows"], (f"{thread8}-c1.bare.json", f"{thread8}-c1.json", "rotated.json", "withdrawn.json"),
                                                ("key 2 · " + PUBLIC.hex()[:16], "key 2 · " + PUBLIC.hex()[:16], "key 1 · " + OTHER_PUBLIC.hex()[:16], "key 1 · " + OTHER_PUBLIC.hex()[:16]),
                                                ("certified", "certified", "certified", "revoked")):
            for text in (name, thread8, "c1", self.cert_payload["result"]["commit"][:10], verdict, "Add a CSV export", key, "digest "):
                self.assertIn(text, row)
        self.assertIn("digest " + revoked, report["certificate_rows"][3][1])
        self.assertIn("serve's configuration revokes this certificate: do not treat it as certified", report["certificate_rows"][3][1])
        for text in ("3 of 4 certificates certified", "2 keys 1 " + OTHER_PUBLIC.hex()[:16] + "…, 2 " + PUBLIC.hex()[:16] + "…",
                     "1 certificate digest revoked", "Not checked: repository", "syberlabs verify FILE --public KEY --repo .",
                     "SYBERWORK_CERTIFICATE_REVOKED lists reads revoked under every key; there is no validity window",
                     "The .pub file beside each certificate is not read", "A SyberWork case has none"):
            self.assertIn(text, report["certificate_page"])
        # Before the witness signs, the log holds nothing for the case; after, every event up to the head is witnessed.
        self.assertIn("not witnessed", report["witness_before"])
        self.assertIn("No leaf in the log names this case", report["witness_before"])
        # Asking is the manager's, as this server was configured: the operator sees why and a switch; the manager arms
        # the button and the second press asks. One leaf is appended, for this head.
        self.assertEqual(report["witness_ask_operator"][0], True, report["witness_ask_operator"])
        self.assertIn("manager role required to ask the witness", report["witness_ask_operator"][1])
        self.assertIn("switch to manager", report["witness_ask_operator"][1])
        for text in ("Ask the witness to sign this history", "A leaf cannot be removed", "Nothing is written to this case", "manager role"):
            self.assertIn(text, report["witness_confirm"])
        self.assertIn("Witness signed #1–#", report["witness_toast"])
        events = report["history_events"]
        with closing(sqlite3.connect(self.log_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leaves").fetchone()[0], 1)
        self.assertEqual(report["witness_ask_after"][0], True, "a covered head is not offered again")
        self.assertIn(f"already covers #1–#{events}", report["witness_ask_after"][1])
        self.assertIn(f"witnessed through #{events}", report["witness_after"])
        self.assertNotIn("not witnessed", report["witness_after"])
        self.assertIn(self.public.hex()[:16], report["witness_after"])
        self.assertEqual([verdict for verdict, _ in report["witness_rows"]], ["included"])
        self.assertIn(f"#1–#{events}", report["witness_rows"][0][1])
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
        # Propose → By hand, before issue_order: the server's admission preview, recorded nowhere.
        self.assertEqual(report["preview_default_action"], "issue_order", "the form starts at the next compiled step")
        for text in ("If proposed now", "denied", "amount_required", "limits.amount", "Nothing was recorded"):
            self.assertIn(text, report["preview_empty"])
        for text in ("needs approval", "approval_required:manager", "approval.required", "bound by the server"):
            self.assertIn(text, report["preview_bound"])
        self.assertNotIn("out of date", report["preview_bound"])
        self.assertEqual(sorted(report["preview_args"]), ["amount", "part_number", "quote", "quote_id", "quote_version"])
        self.assertIn("out of date", report["preview_changed"])
        self.assertIn("the form changed since this check", report["preview_changed"])
        self.assertEqual(report["preview_proposals_before"], report["preview_proposals_after"])
        cases = self.cell.work.list_cases()
        done_case = next(c for c in cases if self.cell.work.inspect(c["id"])["complete"])
        self.assertTrue(self.cell.work.verify_chain(done_case["id"]))
        reasons = [e["body"]["reason"] for e in self.cell.work.inspect(done_case["id"])["events"] if e["kind"] == "decision"]
        self.assertEqual(reasons, ["all_checks_passed", "approval_required:manager"], "no preview reached the case history")


if __name__ == "__main__":
    unittest.main()
