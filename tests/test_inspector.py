"""The read-only inspector: access control, read-only guarantees, and a real browser render."""

import http.client
import json
import os
import shutil
import subprocess
import threading
import unittest
from pathlib import Path

from syberlabs.inspector import make_inspector
from tests.test_build_thread import GOOD, RepoCase

BROWSER_CHECK = Path(__file__).with_name("inspector_browser.mjs")


class Inspector(RepoCase):
    def setUp(self):
        super().setUp()
        self.kit_ = self.kit()
        self.thread = self.kit_.start("Add a CSV export <script>alert(1)</script>")
        self.thread.propose(changes=GOOD)
        self.thread.propose(changes={"src/export.py": "broken(\n"})
        self.thread.check("c1")
        self.server = make_inspector(self.kit_, token="secret-token")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def request(self, path, *, method="GET", token="secret-token", host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), body

    def test_api_needs_the_token_and_a_loopback_host(self):
        self.assertEqual(self.request("/api/threads", token=None)[0], 401)
        self.assertEqual(self.request("/api/threads", token="wrong")[0], 401)
        self.assertEqual(self.request("/api/threads", host="attacker.example:80")[0], 421)
        status, _, body = self.request("/api/threads")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["id"], self.thread.id)

    def test_nothing_can_be_changed_through_it(self):
        before = len(self.thread.history())
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.request(f"/api/threads/{self.thread.id}", method=method)[0], 405)
        status, _, body = self.request(f"/api/threads/{self.thread.id}/candidates/c2/verdict")
        verdict = json.loads(body)
        self.assertEqual((status, verdict["reason"], verdict["untested"]), (200, "candidate_not_evaluated", ["tests"]))
        self.assertEqual(len(self.thread.history()), before, "viewing a verdict must not run checks or record anything")

    def test_views(self):
        status, _, body = self.request(f"/api/threads/{self.thread.id[:8]}")
        data = json.loads(body)
        self.assertEqual([c["id"] for c in data["candidates"]], ["c1", "c2"])
        self.assertEqual(data["status"]["objective"], "Add a CSV export <script>alert(1)</script>")
        status, headers, diff = self.request(f"/api/threads/{self.thread.id}/candidates/c1/diff")
        self.assertIn(b"csv.DictWriter", diff)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(json.loads(self.request(f"/api/threads/{self.thread.id}/events")[2])[0]["kind"], "case_created")
        self.assertEqual(self.request("/api/threads/ffffffff")[0], 404)

    def test_static_assets_are_locked_down(self):
        status, headers, body = self.request("/", token=None)
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual((headers["X-Frame-Options"], headers["X-Content-Type-Options"]), ("DENY", "nosniff"))
        self.assertNotIn(b"secret-token", body)
        for path in ("/../journal/registry.jsonl", "/%2e%2e/pyproject.toml", "/static/../inspector.py"):
            self.assertEqual(self.request(path, token=None)[0], 404, path)
        js = self.request("/inspector.js", token=None)[2].decode()
        self.assertNotIn("innerHTML", js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_renders_in_a_browser_without_errors(self):
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        if not (Path(root) / "playwright").exists():
            self.skipTest("playwright for node is not installed")
        env = {**os.environ, "NODE_PATH": root, "INSPECTOR_URL": f"http://127.0.0.1:{self.port}/#token=secret-token",
               "THREAD": self.thread.id[:8]}
        if Path("/opt/pw-browsers/chromium").exists():
            env.setdefault("CHROMIUM", "/opt/pw-browsers/chromium")
        done = subprocess.run(["node", str(BROWSER_CHECK)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        report = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["objective"], "Add a CSV export <script>alert(1)</script>")
        self.assertFalse(report["script_injected"])
        self.assertEqual(report["nodes"], 3)
        self.assertIn("csv.DictWriter", report["diff"])
        self.assertNotIn("token", report["url"])


if __name__ == "__main__":
    unittest.main()
