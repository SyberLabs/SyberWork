"""The service's own metrics: GET /metrics/<key> is a fresh, versioned reading an intervention can settle against."""

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from spec.validate import validate_events
from syberlabs.build import default_contract
from syberlabs.canonical import digest
from syberwork.core import METRIC_KEYS, Work
from syberwork.server import make_server
from syberwork.worker import run_once
from tests.test_build_thread import GOOD, RepoCase

TOKEN = "operator-token"
TOKEN_ENV = "TEST_SYBERWORK_METRIC_TOKEN"
WINDOW = 600
FIELDS = {"key", "value", "observed_at", "observed_at_utc", "version"}
NOTE = {"id": "note", "version": 1, "inputs": {"name": "string"}, "actions": {"note": {}},
        "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}]}


class Service:
    """A SQLite-backed service on a free loopback port with one operator credential, hashed as ``init`` stores it."""

    def __init__(self, folder: Path, effects: str = "inline"):
        self.work = Work(folder / "work.sqlite", effects=effects)
        users = {"operator": {"hash": hashlib.sha256(TOKEN.encode()).hexdigest(), "roles": ["operator"], "sources": []}}
        self.server = make_server(self.work, users, port=0)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.url = self.base + "/metrics/{key}"

    def call(self, path, data=None, token=TOKEN):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                         headers=headers, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read())

    def metric(self, key):
        return self.call("/metrics/" + key)

    def install(self):
        self.work.install_contract(NOTE)
        self.work.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
        self.work.install_action("note", {"kind": "local"})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.work.close()


class ServiceCase(unittest.TestCase):
    effects = "inline"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.service = Service(Path(folder.name), self.effects)
        self.addCleanup(self.service.close)

    def read(self, key):
        status, headers, body = self.service.metric(key)
        self.assertEqual(status, 200, body)
        return headers, body

    def values(self):
        return {key: self.read(key)[1]["value"] for key in METRIC_KEYS}


class Metrics(ServiceCase):
    def test_a_reading_is_a_versioned_number_with_a_fresh_stamp(self):
        before = time.time()
        headers, body = self.read("cases_total")
        self.assertEqual(set(body), FIELDS)
        self.assertEqual((body["key"], body["value"]), ("cases_total", 0))
        self.assertIs(type(body["observed_at"]), float)
        self.assertLessEqual(before, body["observed_at"])
        self.assertLessEqual(body["observed_at"], time.time())
        self.assertRegex(body["observed_at_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(body["version"], digest({"key": "cases_total", "value": 0, "observed_at": body["observed_at"]}))
        self.assertEqual(headers["X-Record-Version"], body["version"])
        self.assertEqual(headers["ETag"], '"' + body["version"] + '"')
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        time.sleep(0.1)
        _, again = self.read("cases_total")
        self.assertEqual(again["value"], 0)
        self.assertGreater(again["observed_at"], body["observed_at"])
        self.assertNotEqual(again["version"], body["version"], "an unchanged value still gets a fresh stamp and version")
        _, queue = self.read("queue_depth")
        self.assertEqual((queue["key"], queue["value"]), ("queue_depth", 0))
        _, uptime = self.read("uptime_seconds")
        self.assertGreaterEqual(uptime["value"], 0)
        self.assertLess(uptime["value"], 60)

    def test_every_key_reads_as_a_number_and_anything_else_is_refused(self):
        for key in METRIC_KEYS + ("uptime_seconds",):
            with self.subTest(key):
                headers, body = self.read(key)
                self.assertIn(type(body["value"]), (int, float))
                self.assertEqual(headers["X-Record-Version"], body["version"])
        status, headers, body = self.service.metric("nope")
        self.assertEqual((status, body["error"]), (404, "unknown_metric"))
        self.assertNotIn("X-Record-Version", headers)
        self.assertNotIn("ETag", headers)
        for path in ("/metrics", "/metrics/", "/metrics/cases_total/extra"):
            self.assertEqual(self.service.call(path)[0], 404, path)
        status, _, body = self.service.call("/metrics/cases_total", token=None)
        self.assertEqual((status, body["error"]), (401, "unauthorized"), "a metric is a read and needs a token like every read")
        self.assertEqual(self.service.call("/metrics/cases_total", token="wrong")[0], 401)
        self.assertEqual(self.service.call("/metrics/cases_total", data={})[0], 409, "the route is read only")

    def test_counts_follow_the_store(self):
        self.assertEqual(self.values(), dict.fromkeys(METRIC_KEYS, 0))
        self.service.install()
        work = self.service.work
        first = work.create_case("note", 1, {"name": "a"}, "operator")
        second = work.create_case("note", 1, {"name": "b"}, "operator")
        opened = self.values()
        self.assertEqual((opened["cases_total"], opened["cases_open"]), (2, 2))
        denied = work.propose(first, "note", {}, "stranger", [])
        self.assertEqual(denied["decision"]["status"], "denied")
        allowed = work.propose(first, "note", {}, "operator", ["operator"])
        self.assertEqual(work.commit(first, allowed["proposal"]["id"], "operator")["status"], "succeeded")
        work.cancel_case(second, "not needed", "manager", ["manager"])
        self.assertEqual(self.values(), {
            "cases_open": 0, "cases_total": 2, "proposals_admitted_total": 1, "proposals_denied_total": 1,
            "effects_pending": 0, "effects_settled_applied_total": 1, "effects_settled_not_applied_total": 0, "queue_depth": 0})


class WorkerMetrics(ServiceCase):
    effects = "worker"

    def test_queue_depth_and_pending_effects_follow_the_worker(self):
        self.service.install()
        work = self.service.work
        case = work.create_case("note", 1, {"name": "a"}, "operator")
        proposed = work.propose(case, "note", {}, "operator", ["operator"])
        self.assertEqual(work.commit(case, proposed["proposal"]["id"], "operator")["status"], "queued")
        queued = self.values()
        self.assertEqual((queued["queue_depth"], queued["effects_pending"], queued["effects_settled_applied_total"], queued["cases_open"]),
                         (1, 1, 0, 1))
        self.assertEqual(run_once(work)["status"], "succeeded")
        settled = self.values()
        self.assertEqual((settled["queue_depth"], settled["effects_pending"], settled["effects_settled_applied_total"], settled["cases_open"]),
                         (0, 0, 1, 0))


class InterventionAgainstTheService(RepoCase):
    """A Build Thread's intervention reads ``cases_total`` from the live service and settles once a case exists."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.service = Service(Path(folder.name))
        self.addCleanup(self.service.close)
        self.service.install()
        os.environ[TOKEN_ENV] = TOKEN
        self.addCleanup(os.environ.pop, TOKEN_ENV, None)
        self.clock = [time.time()]

    def thread(self):
        doc = default_contract(self.root)
        doc["actions"]["measure"] = {"requires_effect": "accept_change",
                                     "required_facts": [{"key": "accepted", "source": "git", "verified": True}],
                                     "arguments": {"commit": "fact:accepted.commit"}}
        policy = {"version": 1, "actions": {name: {"roles": ["developer"]} for name in doc["actions"]}}
        home = self.root / ".syberlabs"
        home.mkdir(exist_ok=True)
        (home / "actions.json").write_text(json.dumps({"measure": {
            "kind": "local", "effect": "intervention", "metric_url": self.service.url, "metric_key": "cases_total",
            "window_seconds": WINDOW, "expectation": {"direction": "up", "min_delta": 1},
            "max_age_seconds": 3600, "auth_env": TOKEN_ENV}}))
        kit = self.kit(doc, policy)
        kit.session.clock = lambda: self.clock[0]
        thread = kit.start("Create one case in the service")
        thread.propose(changes=GOOD)
        thread.check("c1")
        self.assertEqual(thread.accept("c1").status, "succeeded")
        return thread

    def observations(self, thread, phase):
        return [e for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == "intervention"
                and e["body"]["value"].get("phase") == phase]

    def test_cases_total_settles_applied_after_a_case_is_created_through_the_service(self):
        thread = self.thread()
        start = self.service.metric("cases_total")[2]["value"]
        receipt = thread.publish("measure")
        self.assertEqual((receipt.status, receipt.reason), ("unknown", "effect_unknown"))
        claim, baseline, unknown = thread.history()[-3:]
        self.assertEqual((claim["kind"], baseline["kind"], unknown["body"]["error"]), ("effect_started", "observed", "window_open"))
        self.assertRegex(baseline["body"]["version"], r'^"[0-9a-f]{64}"$', "the recorded version is the quoted ETag")
        record = baseline["body"]["value"]
        self.assertEqual((record["metric"], record["value"], record["phase"]), ("cases_total", float(start), "baseline"))
        self.assertIsInstance(record["observed_at"], float)
        self.assertLess(abs(record["observed_at"] - self.clock[0]), 60, "observed_at is epoch seconds the freshness rule accepts")

        status, _, created = self.service.call("/api/cases", {"contract_id": "note", "version": 1, "inputs": {"name": "from the thread"}})
        self.assertEqual(status, 200, created)
        self.assertEqual(self.service.metric("cases_total")[2]["value"], start + 1)
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "absent_within_settle_window"))

        self.clock[0] += WINDOW + 1
        [settled] = thread.recover()
        self.assertEqual((settled.status, settled.reason, settled.ref), ("succeeded", "destination_state_matched", "measure"))
        [settlement] = self.observations(thread, "settlement")
        self.assertEqual(settled.external_id, settlement["body"]["version"])
        self.assertRegex(settled.external_id, r'^"[0-9a-f]{64}"$')
        self.assertNotEqual(settlement["body"]["version"], baseline["body"]["version"])
        value = settlement["body"]["value"]
        self.assertEqual((value["outcome"], value["baseline"], value["value"], value["delta"]), ("moved", float(start), float(start + 1), 1.0))
        self.assertEqual(thread.history()[-1]["kind"], "reconciled")
        self.assertEqual(thread.recover(), [])
        self.assertEqual(validate_events(thread.history(), "self_metrics"), [])


if __name__ == "__main__":
    unittest.main()
