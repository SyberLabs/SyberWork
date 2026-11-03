"""Regression tests for the trust-boundary findings of the 2026-11 SyberWork security review.

Each class names the finding it pins. No test opens a connection beyond loopback, moves funds or reads a
real service: name resolution is patched, and the servers are SQLite cells on free loopback ports.
"""

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from syberlabs import planner as planner_module
from syberlabs import publish as publish_module
from syberlabs.errors import Rejected
from syberlabs.publish import Command
from syberlabs.targets import credentialed_opener, guard_request, trusted_origin
from syberwork import core as core_module
from syberwork.backup import export_cell
from syberwork.core import Work
from syberwork.server import UserFile, make_server
from syberwork.storage import ReservationStore
from tests.test_cell import _drop_postgres, _install, _postgres_database

EXAMPLES = Path(__file__).resolve().parents[1] / "syberwork" / "examples"
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class HostCheckMatchesDialTarget(unittest.TestCase):
    """Finding 1: the guard classified ``%31%32%37.0.0.1`` as a name while urllib dialed ``127.0.0.1``."""

    ENCODED = ("https://%31%32%37.0.0.1/orders", "https://%6c%6fcalhost/x", "https://exa%6dple.com/x",
               "https://169.254.169.%32%35%34/latest", "https://ex_ample.com/x", "https://éx.example/x",
               "https://exa mple.com/x", "https://{key}.example.com/x")

    def test_a_host_urllib_would_rewrite_is_refused_before_classification(self):
        for url in self.ENCODED:
            with self.subTest(url=url), self.assertRaises(Rejected) as refused:
                trusted_origin(url)
            self.assertEqual(refused.exception.code, "invalid_target")

    def test_plain_names_and_ip_literals_still_pass(self):
        self.assertEqual(trusted_origin("https://EXAMPLE.com/x"), ("https", "example.com", 443))
        self.assertEqual(trusted_origin("https://xn--bcher-kva.example/x"), ("https", "xn--bcher-kva.example", 443))
        self.assertEqual(trusted_origin("https://[2606:4700::1]:8443/x"), ("https", "2606:4700::1", 8443))
        self.assertEqual(trusted_origin("http://127.0.0.1:9/items/{key}"), ("http", "127.0.0.1", 9))

    def test_the_guard_refuses_an_encoded_loopback_without_a_lookup(self):
        with patch("syberlabs.targets.socket.getaddrinfo", side_effect=AssertionError("no lookup")):
            with self.assertRaises(Rejected):
                guard_request("https://%31%32%37.0.0.1/orders")

    def test_a_name_the_guard_cannot_resolve_is_refused_not_allowed(self):
        for failure in (socket.gaierror(), UnicodeError("label"), OSError("resolver")):
            with self.subTest(failure=type(failure).__name__):
                with patch("syberlabs.targets.socket.getaddrinfo", side_effect=failure):
                    with self.assertRaises(urllib.error.URLError):
                        guard_request("https://example.com/path")
        with patch("syberlabs.targets.socket.getaddrinfo", return_value=[]):
            with self.assertRaises(urllib.error.URLError):
                guard_request("https://example.com/path")
        with patch("syberlabs.targets.socket.getaddrinfo", return_value=PUBLIC):
            guard_request("https://example.com/path")

    def test_an_unresolvable_source_is_unavailable_and_makes_no_request(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            case = _install(work)
            work.install_source("crm", {"kind": "http", "url": "https://crm.example/records/{key}"})
            with patch("syberlabs.targets.socket.getaddrinfo", side_effect=socket.gaierror()), \
                    patch.object(core_module.HTTP, "open", side_effect=AssertionError("no request")):
                with self.assertRaises(Rejected) as refused:
                    work.refresh_fact(case, "crm", "c", "R-1", "operator", ["operator"])
            self.assertEqual((refused.exception.code, refused.exception.detail), ("source_unavailable", "destination_unreachable"))
            work.close()


class SourceKeyStaysInThePath(unittest.TestCase):
    """Finding 2: ``{key}`` in a source host let the case input pick the host that receives the bearer."""

    def test_install_source_refuses_key_outside_the_path(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            for url in ("https://{key}.example.com/records", "https://records.example/{key}/{key}",
                        "https://records.example/r?id={key}", "https://records.example/{key}?token=abc",
                        "https://records.example/{key}/{other}", "https://records.example/r#{key}"):
                with self.subTest(url=url), self.assertRaises(Rejected) as refused:
                    work.install_source("crm", {"kind": "http", "url": url, "auth_env": "CRM_TOKEN"})
                self.assertEqual(refused.exception.code, "invalid_source")
            work.install_source("crm", {"kind": "http", "url": "https://records.example/r/{key}", "auth_env": "CRM_TOKEN"})
            self.assertIn("crm", work.registry("sources"))
            work.close()


class ActionUrlCarriesNoQuery(unittest.TestCase):
    """Finding 3: a query string (where a secret rides) was stored in the action and served by ``registry``."""

    def test_install_action_refuses_a_query_on_the_write_or_status_url(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            for doc in ({"kind": "http", "url": "https://orders.example/po?api_key=s3cret"},
                        {"kind": "http", "url": "https://orders.example/po?"},
                        {"kind": "http", "url": "https://orders.example/{key}"},
                        {"kind": "http", "url": "https://orders.example/po",
                         "status_url": "https://orders.example/po/{key}?api_key=s3cret"}):
                with self.subTest(doc=doc), self.assertRaises(Rejected) as refused:
                    work.install_action("issue", doc)
                self.assertEqual(refused.exception.code, "invalid_action")
            self.assertEqual(work.registry("actions"), {})
            work.install_action("issue", {"kind": "http", "url": "https://orders.example/po",
                                          "status_url": "https://orders.example/po/{key}", "auth_env": "ORDERS_TOKEN"})
            self.assertNotIn("?", json.dumps(work.registry("actions")))
            work.close()


class ObserverCannotStallOrRead(unittest.TestCase):
    """Finding 4: an observer token could shadow a verified fact with an unverified one and read every case."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Work(Path(folder.name) / "w.sqlite")
        self.addCleanup(self.work.close)
        self.work.install_policy(json.loads((EXAMPLES / "policy.json").read_text()))
        self.work.install_contract(json.loads((EXAMPLES / "contract.json").read_text()))
        self.work.install_action("record_review", {"kind": "local"})
        self.work.install_action("issue_order", {"kind": "local"})
        self.case = self.work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        self.users = {
            "inventory": {"hash": _token_hash("inv"), "roles": ["observer"], "sources": ["inventory"]},
            "supplier": {"hash": _token_hash("sup"), "roles": ["observer"], "sources": ["supplier"]},
            "operator": {"hash": _token_hash("op"), "roles": ["operator"], "sources": []},
        }
        self.server = make_server(self.work, self.users, port=0)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, path, token, data=None):
        headers = {"Authorization": "Bearer " + token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                         headers=headers, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_an_attestation_cannot_shadow_a_fact_the_contract_reads_verified(self):
        self.work.observe(self.case, "part_number", "P-104", "inventory", "v1", "sync", verified=True)
        before = len(self.work.inspect(self.case)["events"])
        # The supplier attests the inventory's key, and the inventory attests its own key unverified: the contract
        # reads part_number only verified and only from inventory, so neither may land as the newest observation.
        for token, source in (("sup", "supplier"), ("inv", "inventory")):
            with self.subTest(source=source):
                status, body = self.call(f"/api/cases/{self.case}/facts", token,
                                         {"key": "part_number", "source": source, "version": "x", "value": "P-999"})
                self.assertEqual((status, body["error"]), (409, "attestation_denied"))
        # A key used only through an argument binding (``quote.id``) is protected too.
        status, body = self.call(f"/api/cases/{self.case}/facts", "sup",
                                 {"key": "quote", "source": "supplier", "version": "x", "value": {"id": "Q", "price": 1}})
        self.assertEqual((status, body["error"]), (409, "attestation_denied"))
        self.assertEqual(len(self.work.inspect(self.case)["events"]), before)
        proposed = self.work.propose(self.case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        self.assertEqual(proposed["decision"]["status"], "allowed")

    def test_a_key_the_contract_does_not_read_is_still_recorded_unverified(self):
        status, body = self.call(f"/api/cases/{self.case}/facts", "inv",
                                 {"key": "shelf_note", "source": "inventory", "version": "n1", "value": "bin 4"})
        self.assertEqual(status, 200)
        self.assertIs(body["body"]["verified"], False)

    def test_a_contract_that_accepts_a_sources_word_still_takes_it(self):
        contract = json.loads((EXAMPLES / "contract.json").read_text())
        contract["version"] = 2
        contract["actions"]["record_review"]["required_facts"][0]["verified"] = False
        self.work.install_contract(contract)
        case = self.work.create_case("purchase-order", 2, {"part_number": "P-104", "quantity": 2}, "operator")
        status, _ = self.call(f"/api/cases/{case}/facts", "inv",
                              {"key": "part_number", "source": "inventory", "version": "n1", "value": "P-104"})
        self.assertEqual(status, 200)
        status, body = self.call(f"/api/cases/{case}/facts", "sup",
                                 {"key": "part_number", "source": "supplier", "version": "n1", "value": "P-1"})
        self.assertEqual((status, body["error"]), (409, "attestation_denied"))

    def test_an_observer_reads_no_case_contract_certificate_or_metric(self):
        for path in ("/api/cases", f"/api/cases/{self.case}", f"/api/cases/{self.case}/verify",
                     f"/api/cases/{self.case}/side", f"/api/cases/{self.case}/candidates", "/api/contracts",
                     "/api/contracts/purchase-order/1", "/api/contracts/purchase-order/1/artifacts",
                     "/api/certificates", "/metrics/cases_total", "/metrics/work_items"):
            with self.subTest(path=path):
                status, body = self.call(path, "inv")
                self.assertEqual((status, body["error"]), (403, "forbidden"))
                status, _ = self.call(path, "op")
                self.assertEqual(status, 200)
        status, body = self.call("/api/me", "inv")
        self.assertEqual((status, body["name"]), (200, "inventory"))


class BackupDoesNotFollowLinks(unittest.TestCase):
    """Finding 5: ``export_cell`` replaced the target of a ``manifest.json`` symlink with the snapshot."""

    @unittest.skipIf(sys.platform == "win32", "creating symlinks needs a privilege on Windows")
    def test_a_planted_link_is_refused_and_its_target_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            _install(work)
            victim = Path(folder) / "victim.txt"
            victim.write_text("keep me", encoding="utf-8")
            for name in ("manifest.json", "cases.json"):
                with self.subTest(name=name):
                    snapshot = Path(folder) / ("snap-" + name)
                    snapshot.mkdir()
                    (snapshot / name).symlink_to(victim)
                    with self.assertRaises(Rejected) as refused:
                        export_cell(work, snapshot)
                    self.assertEqual(refused.exception.code, "backup_refused")
                    self.assertEqual(victim.read_text(encoding="utf-8"), "keep me")
                    self.assertTrue((snapshot / name).is_symlink())
            clean = Path(folder) / "clean"
            manifest = export_cell(work, clean)
            self.assertEqual(json.loads((clean / "manifest.json").read_text(encoding="utf-8")), manifest)
            self.assertEqual([p.name for p in clean.iterdir() if p.name.startswith(".")], [])
            work.close()


class NoEnvironmentProxy(unittest.TestCase):
    """Finding 6: ``build_opener`` installed a ProxyHandler from ``http_proxy`` at import, so the proxy got the bearer."""

    def test_credentialed_openers_carry_no_proxy(self):
        for opener in (core_module.HTTP, publish_module.HTTP, planner_module._HTTP):
            with self.subTest(opener=opener):
                self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in opener.handlers))
                self.assertFalse(any(name.endswith("_open") and "proxy" in name for h in opener.handlers for name in dir(h)
                                     if isinstance(h, urllib.request.ProxyHandler)))

    def test_a_proxy_set_before_the_opener_is_built_is_not_used(self):
        hits = []

        class Proxy(threading.Thread):
            def __init__(self):
                super().__init__(daemon=True)
                self.sock = socket.socket()
                self.sock.bind(("127.0.0.1", 0))
                self.sock.listen(1)
                self.sock.settimeout(5)

            def run(self):
                try:
                    conn, _ = self.sock.accept()
                    hits.append(conn.recv(4096))
                    conn.close()
                except OSError:
                    pass

        proxy = Proxy()
        proxy.start()
        self.addCleanup(proxy.sock.close)
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        self.addCleanup(target.close)
        address = f"http://127.0.0.1:{proxy.sock.getsockname()[1]}"
        with patch.dict(os.environ, {"http_proxy": address, "HTTP_PROXY": address, "no_proxy": "", "NO_PROXY": ""}):
            opener = credentialed_opener(core_module.NoRedirect)
            request = urllib.request.Request(f"http://127.0.0.1:{target.getsockname()[1]}/x",
                                             headers={"Authorization": "Bearer secret"})
            with self.assertRaises((urllib.error.URLError, OSError)):
                opener.open(request, timeout=1)
        proxy.sock.close()
        proxy.join(2)
        self.assertEqual(hits, [])


class ReservationAmountsAre64Bit(unittest.TestCase):
    """Follow-up: PostgreSQL stored ``amount_units`` as int4 while ``units`` admits up to 2**63-1."""

    LARGE = 2**62 + 7

    def _reserve(self, work):
        with work.tx() as db:
            ReservationStore(db).reserve("p-large", "c", "b", "USD", self.LARGE)
        with work.tx() as db:
            self.assertEqual(ReservationStore(db).reserved_total("b", "USD"), self.LARGE)

    def test_sqlite_keeps_a_large_reservation(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            self._reserve(work)
            work.close()

    def test_postgresql_keeps_a_large_reservation(self):
        url, admin, name = _postgres_database()
        self.addCleanup(_drop_postgres, admin, name)
        work = Work(url)
        try:
            self._reserve(work)
            kind = work._db.execute("SELECT data_type::text AS data_type FROM information_schema.columns "
                                    "WHERE table_name = 'economic_reservations' AND column_name = 'amount_units'").fetchone()
            self.assertEqual(kind["data_type"], "bigint")
        finally:
            work.close()


class PublishTimeoutEndsTheTree(unittest.TestCase):
    """Follow-up: a publish command's timeout killed one process while a check's kills the whole tree."""

    @unittest.skipUnless(hasattr(os, "killpg"), "the process-group path is POSIX; Windows uses taskkill /T")
    def test_a_timed_out_command_leaves_no_child_running(self):
        with tempfile.TemporaryDirectory() as folder:
            beat = Path(folder) / "beat"
            child = ("import time,pathlib\n"
                     f"p=pathlib.Path({str(beat)!r})\n"
                     "while True:\n    p.open('a').write('.')\n    time.sleep(0.05)\n")
            parent = (f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child!r}])\ntime.sleep(60)\n")

            class Repo:
                @contextmanager
                def worktree(self, commit):
                    yield Path(folder)

            class Kit:
                repo = Repo()

            command = Command(Kit(), {"timeout_seconds": 1, "argv": [sys.executable, "-c", parent]})
            started = time.monotonic()
            self.assertEqual(command._run(command.doc["argv"], "case", {"commit": "c"}, "k"), (None, ""))
            self.assertLess(time.monotonic() - started, 30)
            time.sleep(0.3)
            size = beat.stat().st_size if beat.exists() else 0
            time.sleep(0.6)
            self.assertEqual(beat.stat().st_size if beat.exists() else 0, size, "the grandchild kept running")


class BindAndUsers(unittest.TestCase):
    """Follow-ups: an empty SYBERWORK_BIND passed the loopback guard, and users.json was read once."""

    def test_an_empty_host_never_binds(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            for env in ("", None):
                with self.subTest(env=env):
                    environ = {"SYBERWORK_BIND": env} if env is not None else {}
                    with patch.dict(os.environ, environ, clear=False):
                        if env is None:
                            os.environ.pop("SYBERWORK_BIND", None)
                        with self.assertRaises(Rejected) as refused:
                            make_server(work, {}, "", 0)
                    self.assertEqual(refused.exception.code, "unsafe_bind")
            work.close()

    def test_users_json_changes_apply_without_a_restart(self):
        # Cleanups run last in, first out: the folder is removed after the server and the database close,
        # which Windows needs before it will delete an open SQLite file.
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        folder = scratch.name
        path = Path(folder) / "users.json"
        path.write_text(json.dumps({"op": {"hash": _token_hash("one"), "roles": ["operator"], "sources": []}}))
        work = Work(Path(folder) / "w.sqlite")
        server = make_server(work, UserFile(path), port=0)
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(work.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def me(token):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/me",
                                             headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(me("one"), 200)
        path.write_text(json.dumps({"op": {"hash": _token_hash("two-rotated"), "roles": ["operator"], "sources": []}}))
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 1_000_000))
        self.assertEqual((me("one"), me("two-rotated")), (401, 200))
        path.write_text("not json")
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 2_000_000))
        self.assertEqual(me("two-rotated"), 401)
        path.unlink()
        self.assertEqual(me("two-rotated"), 401)


if __name__ == "__main__":
    unittest.main()
