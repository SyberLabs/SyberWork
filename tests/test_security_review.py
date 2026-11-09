"""Regression tests for the trust-boundary findings of the 2026-11 SyberWork security review.

Each class names the finding it pins. No test opens a connection beyond loopback, moves funds or reads a
real service: name resolution is patched, and the servers are SQLite cells on free loopback ports.
"""

import copy
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
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
from syberlabs.evidence import acceptance_results
from syberlabs.targets import header_value_ok
from syberwork.worker import run_once
from tests.test_cell import NOTE, _drop_postgres, _install, _postgres_database

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
        # Builder views keep the builder's own status for a refusal (409), as its writes already did.
        status, body = self.call(f"/api/builder/work/{self.case}", "inv")
        self.assertEqual((status, body["error"]), (409, "forbidden"))


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


class GitPushTimeoutEndsTheTree(unittest.TestCase):
    """Follow-up: ``GitPush`` ran ``git push`` under ``subprocess.run(timeout=...)``, which ends ``git`` and
    leaves the transport it started (``ssh``) running. Its timeout now ends the whole session, as a publish
    command's and a check's do, for the push and for the ``ls-remote`` status read."""

    @unittest.skipUnless(hasattr(os, "killpg"), "the process-group path is POSIX; Windows uses taskkill /T")
    def test_a_timed_out_push_or_status_leaves_no_transport_running(self):
        from syberlabs.gitspace import Repo
        from syberlabs.publish import GitPush

        for step in ("apply", "status"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as folder:
                root, beat = Path(folder) / "repo", Path(folder) / "beat"
                child = ("import time,pathlib\n"
                         f"p=pathlib.Path({str(beat)!r})\n"
                         "while True:\n    p.open('a').write('.')\n    time.sleep(0.05)\n")
                transport = Path(folder) / "transport.py"
                transport.write_text("import subprocess,sys,time\n"
                                     f"subprocess.Popen([sys.executable,'-c',{child!r}])\ntime.sleep(60)\n")
                git = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
                root.mkdir()
                git("init", "-q")
                git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "c")
                git("remote", "add", "origin", "ssh://example.invalid/repo.git")
                # A stand-in for ssh that starts a child of its own and never answers.
                git("config", "core.sshCommand", f"{Path(sys.executable).as_posix()} {transport.as_posix()}")
                commit = git("rev-parse", "HEAD").stdout.decode().strip()

                class Kit:
                    repo = Repo(root)

                push = GitPush(Kit(), {"remote": "origin", "timeout_seconds": 1})
                started = time.monotonic()
                # Either way the outcome is unknown; what this test checks is what is left running.
                with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                    getattr(push, step)("abcdef0123", {"commit": commit}, "k")
                self.assertLess(time.monotonic() - started, 30)
                time.sleep(0.3)
                size = beat.stat().st_size if beat.exists() else 0
                time.sleep(0.6)
                self.assertEqual(beat.stat().st_size if beat.exists() else 0, size, "the transport's child kept running")


class HistoryVerifiesAgainstTheWitness(unittest.TestCase):
    """Follow-up: the hash chain does not stop a database owner who rewrites and rehashes history. Verify history
    (``Work.verify_history``, ``GET /verify``, ``syberwork verify``) now also reads the witness's log, whose signed
    heads are outside the database, so a rehash after a head was signed fails verification."""

    def _rehash(self, database, work, case, seq):
        import sqlite3
        from syberlabs.canonical import canonical
        from syberlabs.events import GENESIS, event_digest
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE events SET body=? WHERE case_id=? AND seq=?", (canonical({"rewritten": True}), case, seq))
            db.execute("DELETE FROM event_side WHERE case_id=?", (case,))
        previous = GENESIS
        with closing(sqlite3.connect(database)) as db, db:
            for event in work.inspect(case)["events"]:
                event = dict(event, previous=previous)
                previous = event_digest(event)
                db.execute("UPDATE events SET previous=?, hash=? WHERE case_id=? AND seq=?", (event["previous"], previous, case, event["seq"]))

    def test_a_rehash_after_signing_fails_and_the_limit_is_stated_without_a_log(self):
        from syberlabs.ed25519 import generate
        from syberlabs.tlog import TransparencyLog
        from syberlabs.witness import Witness
        from tests.test_external_witness import _install_note

        seed, public = generate()
        # Registered first, so it runs last: Windows deletes an SQLite file only after every handle is closed.
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        folder = scratch.name
        database = Path(folder) / "case.sqlite"
        work = Work(database)
        self.addCleanup(work.close)
        _install_note(work)
        case = work.create_case("note", 1, {"name": "a"}, "operator")
        work.propose(case, "note", {}, "operator", ["operator"])
        witness = Witness(str(Path(folder) / "witness.sqlite"), seed)
        self.addCleanup(witness.close)
        log = TransparencyLog.open_readonly(Path(folder) / "witness.sqlite", public)
        self.addCleanup(log.close)

        unsigned = work.verify_history(case, log)
        self.assertEqual((unsigned["valid"], unsigned["witness"]["witnessed_through"]), (True, None))
        self.assertIn("no head", unsigned["note"])
        count = len(work.inspect(case)["events"])
        witness.attest(work.inspect(case)["events"])
        signed = work.verify_history(case, log)
        self.assertEqual((signed["valid"], signed["witness"]["witnessed_through"], signed["witness"]["conflicts"]),
                         (True, count, []))

        self._rehash(database, work, case, 1)
        self.assertTrue(work.verify_chain(case), "the chain alone cannot see a rehash")
        alone = work.verify_history(case)
        self.assertEqual((alone["valid"], alone["chain"], alone["witness"]), (True, True, None))
        self.assertIn("rehashed", alone["note"])
        checked = work.verify_history(case, log)
        self.assertEqual((checked["valid"], checked["chain"]), (False, True))
        self.assertEqual([c["verdict"] for c in checked["witness"]["conflicts"]], ["head_differs"])


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


class SpecialPurposeAddressesAreNotPublic(unittest.TestCase):
    """Review F6: ``is_global`` alone let IANA special-purpose addresses through on some interpreters."""

    def test_special_purpose_ranges_are_refused_whatever_ipaddress_says(self):
        from syberlabs.targets import _public_ip
        for address in ("192.0.0.9", "192.0.0.10", "2001:1::1", "2001:1::2", "fec0::1", "2001::1", "2002:7f00:1::1",
                        "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "198.18.0.1", "100.64.0.1", "::ffff:127.0.0.1"):
            with self.subTest(address=address):
                self.assertFalse(_public_ip(ipaddress.ip_address(address)))
        for address in ("93.184.216.34", "1.1.1.1", "2606:4700::1111", "2001:4860:4860::8888", "64:ff9b::808:808"):
            with self.subTest(address=address):
                self.assertTrue(_public_ip(ipaddress.ip_address(address)))

    def test_a_name_resolving_to_one_is_refused(self):
        answer = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:1::1", 443, 0, 0))]
        with patch("syberlabs.targets.socket.getaddrinfo", return_value=answer):
            with self.assertRaises(Rejected):
                guard_request("https://example.com/x")


class ConnectionIsPinnedToTheCheckedAddress(unittest.TestCase):
    """Review F11: the guard resolved once and urllib resolved again, so a rebinding name could reach loopback."""

    def test_the_connection_resolves_checks_and_dials_only_the_accepted_address(self):
        dialed = []

        class Recorder:
            def __init__(self, *args):
                pass

            def settimeout(self, timeout):
                pass

            def connect(self, sockaddr):
                dialed.append(sockaddr)
                raise OSError("stop before TLS")

            def close(self):
                pass

        opener = credentialed_opener()
        with patch("syberlabs.targets.socket.getaddrinfo", return_value=PUBLIC), \
                patch("syberlabs.targets.socket.socket", Recorder):
            with self.assertRaises(urllib.error.URLError):
                opener.open(urllib.request.Request("https://example.com/x"), timeout=2)
        self.assertEqual(dialed, [("93.184.216.34", 443)])

    def test_a_name_that_rebinds_to_loopback_after_the_guard_is_never_dialed(self):
        loopback = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        answers = iter([PUBLIC, loopback])
        opener = credentialed_opener()
        with patch("syberlabs.targets.socket.getaddrinfo", side_effect=lambda *a, **k: next(answers)), \
                patch("syberlabs.targets.socket.socket", side_effect=AssertionError("no socket for a refused address")):
            guard_request("https://example.com/x")
            with self.assertRaises(urllib.error.URLError) as refused:
                opener.open(urllib.request.Request("https://example.com/x"), timeout=2)
        self.assertIn("refused target address", str(refused.exception.reason))


class HeaderValuesAreChecked(unittest.TestCase):
    """Review F14: ``version_arg`` was copied into If-Match with no character check."""

    def test_folded_or_padded_values_are_refused(self):
        for value in ("v1\r\n X-Injected: 1", "v1\n\tmore", " v1", "v1 ", "v\x00", "", "vé"):
            with self.subTest(value=value):
                self.assertFalse(header_value_ok(value))
        self.assertTrue(header_value_ok('"etag-1" W/x'))

    def test_commit_refuses_before_the_claim_and_execute_refuses_before_a_request(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            contract = dict(NOTE, id="versioned")
            work.install_contract(contract)
            work.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
            work.install_action("note", {"kind": "http", "url": "http://127.0.0.1:9/notes", "version_arg": "v"})
            case = work.create_case("versioned", 1, {"name": "a"}, "operator")
            proposed = work.propose(case, "note", {"v": "q-1\r\n X-Injected: yes"}, "operator", ["operator"])
            self.assertEqual(proposed["decision"]["status"], "allowed", proposed)
            with self.assertRaises(Rejected) as refused:
                work.commit(case, proposed["proposal"]["id"], "operator")
            self.assertEqual(refused.exception.code, "invalid_argument")
            self.assertFalse(any(e["kind"] == "effect_started" for e in work.inspect(case)["events"]))
            work.close()
        with patch.object(core_module.HTTP, "open", side_effect=AssertionError("no request")):
            with self.assertRaises(Rejected) as refused:
                Work._execute({"kind": "http", "url": "http://127.0.0.1:9/x", "version_arg": "v"}, {"v": "a\n b"}, "k")
        self.assertEqual(refused.exception.code, "invalid_header")


class ResolutionNeedsAChangedVersion(unittest.TestCase):
    """Review F15: a result read under a key other than the trigger counted as a change by itself."""

    def setUp(self):
        from case_studies.enterprise_procurement import CONTRACT, SimulatedERP, configure
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.erp = SimulatedERP(Path(scratch.name) / "erp.sqlite3")
        self.addCleanup(self.erp.close)
        self.work = Work(Path(scratch.name) / "w.sqlite3")
        self.addCleanup(self.work.close)
        configure(self.work, self.erp)
        self.contract = CONTRACT

    def _quote_task(self, version):
        case = self.work.create_case(self.contract["id"], version, {"request_id": "REQ-4814"}, "analyst")
        for source, key in (("requisitions", "request"), ("quote_options", "quote_options")):
            self.work.refresh_fact(case, source, key, "REQ-4814", "analyst", ["operator"])
        return case, self.work.request_resolution(case, "quote", "analyst", ["operator"])["body"]["id"]

    def test_without_a_confirmation_or_captured_version_the_result_does_not_complete(self):
        unconfirmed = copy.deepcopy(self.contract)
        unconfirmed["version"] = 2
        del unconfirmed["resolutions"]["quote"]["confirmation"]
        self.work.install_contract(unconfirmed)
        case, task = self._quote_task(2)
        self.erp.select_quote("procurement", "REQ-4814", "Q-882A")
        result = self.work.resolve_resolution(case, task, "p.soto", ["procurement"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "source_decision_unverified"))

    def test_a_confirmation_that_rereads_the_trigger_still_completes(self):
        case, task = self._quote_task(1)
        self.erp.select_quote("procurement", "REQ-4814", "Q-882A")
        self.assertEqual(self.work.resolve_resolution(case, task, "p.soto", ["procurement"])["status"], "completed")


class InterruptedEffectsKeepTheirOutcome(unittest.TestCase):
    """Review F16: a second worker could relabel a stored ``effect_unknown`` as settled."""

    def test_one_worker_recovers_an_expired_lease_and_unknown_stays_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite", effects="worker")
            case = _install(work)
            proposal = work.propose(case, "note", {}, "operator", ["operator"])["proposal"]["id"]
            work.commit(case, proposal, "operator")
            with work.tx() as db:
                db.execute("UPDATE effect_obligations SET state='leased', lease_until=? WHERE proposal_id=?", (0, proposal))
            first = work.lease_obligation(now=10)
            self.assertEqual(first["state"], "interrupted")
            self.assertIsNone(work.lease_obligation(now=10), "the expired lease is handed to one worker only")
            self.assertEqual(work.settle_interrupted(first)["status"], "unknown")
            # A worker that took the same job before the swap existed sees the stored outcome, not "settled".
            self.assertEqual(work.settle_interrupted(dict(first))["status"], "unknown")
            with work.tx() as db:
                state = db.execute("SELECT state FROM effect_obligations WHERE proposal_id=?", (proposal,)).fetchone()["state"]
            self.assertEqual(state, "unknown")
            self.assertEqual(sum(e["kind"] == "effect_unknown" for e in work.inspect(case)["events"]), 1)
            work.close()

    def test_a_recovery_that_died_is_picked_up_again(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite", effects="worker")
            case = _install(work)
            proposal = work.propose(case, "note", {}, "operator", ["operator"])["proposal"]["id"]
            work.commit(case, proposal, "operator")
            with work.tx() as db:
                db.execute("UPDATE effect_obligations SET state='recovering', lease_until=? WHERE proposal_id=?", (0, proposal))
            self.assertEqual(work.metric("queue_depth"), 1)
            self.assertEqual(run_once(work, now=10)["status"], "unknown")
            work.close()


class FactAcceptanceReadsTheNewestObservation(unittest.TestCase):
    """Review F17: a fact clause passed on any historical observation, verified or not."""

    @staticmethod
    def _observed(seq, value, verified, source="inventory"):
        return {"seq": seq, "kind": "observed", "body": {"key": "status", "value": value, "verified": verified, "source": source}}

    def test_an_older_match_does_not_pass_and_verified_is_honored(self):
        clause = {"id": "ready", "kind": "fact", "key": "status", "equals": "ready"}
        contract = {"acceptance": [clause]}
        superseded = [self._observed(1, "ready", True), self._observed(2, "held", True)]
        self.assertFalse(acceptance_results(contract, superseded)[0]["passed"])
        current = [self._observed(1, "held", True), self._observed(2, "ready", False)]
        self.assertTrue(acceptance_results(contract, current)[0]["passed"])
        self.assertFalse(acceptance_results({"acceptance": [dict(clause, verified=True)]}, current)[0]["passed"])
        self.assertFalse(acceptance_results({"acceptance": [dict(clause, source="erp")]}, current)[0]["passed"])

    def test_an_acceptance_fact_key_is_not_open_to_manual_attestation(self):
        contract = {"actions": {}, "acceptance": [{"id": "ready", "kind": "fact", "key": "status", "equals": "ready"}]}
        self.assertFalse(Work._attestable(contract, "status", "supplier"))
        self.assertTrue(Work._attestable(contract, "note", "supplier"))


class ConsoleCannotBeFramed(unittest.TestCase):
    """Review F18: the console's policy set default-src, which does not govern framing."""

    def test_the_console_document_refuses_framing(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Work(Path(folder) / "w.sqlite")
            server = make_server(work, {}, port=0)
            server.RequestHandlerClass.log_message = lambda *args: None
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                    headers = response.headers
            finally:
                server.shutdown()
                server.server_close()
                work.close()
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")


class PlannerUrlTakesNoQuery(unittest.TestCase):
    """Review F3, planner half: a credential could ride in the planner URL's query string."""

    def test_a_planner_url_with_a_query_is_unconfigured(self):
        with patch.dict(os.environ, {"SYBERWORK_PLANNER_URL": "https://planner.example/plan?key=s3cret"}):
            with self.assertRaises(planner_module.PlannerRefusal) as refused:
                planner_module.HttpPlanner().ensure_configured()
        self.assertEqual(refused.exception.code, "planner_unconfigured")


if __name__ == "__main__":
    unittest.main()
