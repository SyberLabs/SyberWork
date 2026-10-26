"""Bridge red-team floors for DS, SIM, SCH and COST (factory quota, SyberWork bridge row).

The boundary is the same as for the REC cases. Only SyberWork's own lookup of the
installed destination ``status_url`` may append a verified ``reconciled`` event, and
only when the response matches the state, idempotency key, request digest and a
nonempty external ID. These cases attack the inputs around that lookup:

- DS: the lookup URL, its origin and its identity
- SIM: simulated or sandbox records
- SCH: malformed records, retries and the worker lease
- COST: time, size and budget bounds

Every adversarial case must end pending, unverified, refused or in error. None may
end reconciled. This file does not count toward the REC floor; its two REC
cases cover only the theme of a terminal outcome racing the lookup.

``CASES`` tags each test with one primary category and its rubric lines.
``MISSING_HOOK`` lists the expected failures, each with the invariant the product
does not yet enforce. They run under ``unittest.expectedFailure``, so they start
failing as unexpected successes the day the product hook lands. All destinations
are loopback simulators; nothing leaves the machine.
"""

import hashlib
import itertools
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import syberwork.core as core
import tests.test_economic as economic
from syberlabs.evidence import verified_reconciliation
from syberwork.core import Rejected, Work, digest
from syberwork.storage import ActionRegistry, ReservationStore
from syberwork.worker import run_available, run_once

CONTRACT = {"id": "bridge-floors", "version": 1, "inputs": {"ticket": "string"},
            "actions": {"publish": {"approval_role": "manager"}},
            "acceptance": [{"id": "published", "kind": "effect", "action": "publish"}]}
POLICY = {"version": 1, "actions": {"publish": {"roles": ["operator"]}}}
ARGS = {"ticket": "T-1"}
TOKEN_ENV, TOKEN = "BRIDGE_FLOORS_DEST_TOKEN", "bridge-floors-dest-token"

# name -> (primary category, rubric lines). DS digest/identity swap, SIM simulated evidence,
# SCH malformed or hostile schema, COST cost/time bounds, REC reconciliation spoofing.
CASES = {
    "test_status_url_key_placeholder_abuse_is_refused_at_install": ("DS", ("R1", "R8")),
    "test_cross_origin_redirect_is_not_followed_and_credential_stays_home": ("DS", ("R1", "R8")),
    "test_request_digest_and_key_must_match_exactly": ("DS", ("R1", "R3", "R8")),
    "test_valid_record_of_a_sibling_proposal_cannot_settle_this_one": ("DS", ("R1", "R3", "R8")),
    "test_404_never_establishes_no_write": ("DS", ("R1", "R8")),
    "test_simulation_states_never_verify": ("SIM", ("R1", "R5", "R8")),
    "test_committed_record_labeled_simulated_does_not_verify": ("SIM", ("R1", "R5", "R8")),
    "test_testnet_settlement_record_neither_verifies_nor_releases_budget": ("SIM", ("R1", "R5", "R8")),
    "test_settlement_record_on_another_rail_does_not_verify": ("SIM", ("R1", "R5", "R8")),
    "test_malformed_status_bodies_fail_closed": ("SCH", ("R1", "R8")),
    "test_status_body_with_duplicate_keys_fails_closed": ("SCH", ("R1", "R8")),
    "test_hostile_adapter_timeout_fails_closed": ("SCH", ("R1", "R8")),
    "test_repeated_lookups_never_reissue_the_write": ("SCH", ("R1", "R7", "R8")),
    "test_expired_worker_lease_is_unknown_and_never_reexecuted": ("SCH", ("R1", "R7", "R8")),
    "test_status_lookup_is_bounded_in_time_and_size": ("COST", ("R1", "R7")),
    "test_budget_held_through_every_unresolved_path": ("COST", ("R1", "R7", "R8")),
    "test_worker_settlement_racing_the_lookup_is_rechecked": ("REC", ("R1", "R8")),
    "test_concurrent_reconciliation_appends_one_proof": ("REC", ("R1", "R8")),
}
# Expected failures: the invariant holds in the rubric, but no product hook enforces it yet.
MISSING_HOOK = {
    "test_committed_record_labeled_simulated_does_not_verify":
        "the status match ignores fields other than state, key, digest and external_id, "
        "so a record the destination itself labels simulated or sandbox still verifies",
    "test_settlement_record_on_another_rail_does_not_verify":
        "receipt_matches does not compare the record's rail with the installed rail, "
        "and the proof then claims the installed rail",
    "test_status_body_with_duplicate_keys_fails_closed":
        "the status body is parsed with json.loads, which keeps the last duplicate key, "
        "so a body that is both pending and committed verifies",
}
FLOORS = {"DS": 3, "SIM": 2, "SCH": 4, "COST": 1}


class Destination:
    """Loopback destination. Writes are idempotent by key, and each case can script the replies."""

    def __init__(self):
        self.records, self.posts, self.gets, self.auth = {}, 0, 0, []
        self.drop_ack = False
        self.write = None   # optional callable(key) -> (code, body, headers) after the record is stored
        self.status = None  # optional callable(key) -> (code, body, headers); bytes bodies are sent raw
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code, body=None, headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
                self.send_response(code)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                owner.posts += 1
                owner.auth.append(self.headers.get("Authorization"))
                key = self.headers["Idempotency-Key"]
                args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.records[key] = {"state": "committed", "idempotency_key": key,
                                      "request_digest": digest(args), "external_id": "dest-" + key}
                if owner.drop_ack:
                    self.connection.shutdown(2)
                    return
                if owner.write is not None:
                    return self.reply(*owner.write(key))
                self.reply(201, owner.records[key])

            def do_GET(self):
                owner.gets += 1
                owner.auth.append(self.headers.get("Authorization"))
                key = self.path.rsplit("/", 1)[-1]
                if owner.status is not None:
                    return self.reply(*owner.status(key))
                record = owner.records.get(key)
                self.reply(200, record) if record else self.reply(404, {"error": "not_found"})

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def reply(record, **headers):
    return lambda _key: (200, record, headers)


class BridgeFloor(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder, self.cells = Path(folder.name), itertools.count()
        environment = patch.dict(os.environ, {TOKEN_ENV: TOKEN})
        environment.start()
        self.addCleanup(environment.stop)
        self.dest = self.destination()
        self.work, self.case = self.cell(self.action())

    def destination(self) -> Destination:
        dest = Destination()
        self.addCleanup(dest.close)
        return dest

    def action(self, **changes) -> dict:
        return {"kind": "http", "url": self.dest.base + "/write", "status_url": self.dest.base + "/status/{key}",
                "auth_env": TOKEN_ENV, "no_write_statuses": [409], **changes}

    def cell(self, action: dict, *, effects: str = "inline") -> tuple[Work, str]:
        work = Work(self.folder / f"work-{next(self.cells)}.sqlite3", effects=effects)
        self.addCleanup(work.close)
        work.install_contract(CONTRACT)
        work.install_policy(POLICY)
        work.install_action("publish", action)
        return work, work.create_case(CONTRACT["id"], 1, {"ticket": "T-1"}, "operator")

    def approved(self, work=None, case=None) -> str:
        work, case = work or self.work, case or self.case
        result = work.propose(case, "publish", dict(ARGS), "operator", ["operator"])
        self.assertEqual(result["decision"]["status"], "needs_approval")
        key = result["proposal"]["id"]
        work.approve(case, key, "manager", ["manager"])
        return key

    def unknown(self, work=None, case=None) -> str:
        """An approved effect that the destination wrote but whose acknowledgement was lost."""
        work, case = work or self.work, case or self.case
        key = self.approved(work, case)
        self.dest.drop_ack = True
        self.assertEqual(work.commit(case, key, "operator")["status"], "unknown")
        self.dest.drop_ack = False
        self.assertIn(key, self.dest.records)
        return key

    def reconcile(self, key, work=None, case=None) -> dict:
        return (work or self.work).reconcile(case or self.case, key, "manager", ["manager"])

    def assert_not_verified(self, result: dict, status: str, reason: str | None = None) -> None:
        self.assertEqual(result["status"], status)
        if reason is not None:
            self.assertEqual(result["reason"], reason)
        self.assertEqual(result["event"]["kind"], "reconciliation_checked")
        self.assertFalse(verified_reconciliation(result["event"]))

    def assert_unresolved(self, key, work=None, case=None) -> None:
        """No terminal outcome, acceptance unmet, and neither a retry nor a fresh effect can start."""
        work, case = work or self.work, case or self.case
        events = work.inspect(case)["events"]
        self.assertFalse(any(verified_reconciliation(e) for e in events))
        self.assertFalse(any(e["kind"] in ("reconciled", "effect_succeeded", "effect_rejected") for e in events))
        self.assertFalse(work.inspect(case)["complete"])
        self.assertEqual(work.metric("effects_settled_applied_total"), 0)
        with self.assertRaisesRegex(Rejected, "effect_claimed"):
            work.commit(case, key, "operator")
        retry = work.propose(case, "publish", dict(ARGS), "operator", ["operator"])
        self.assertEqual(retry["decision"]["reason"], "effect_unresolved:publish")
        with self.assertRaisesRegex(Rejected, "cancellation_denied"):
            work.cancel_case(case, "give up", "manager", ["manager"])


class DigestAndIdentity(BridgeFloor):
    def test_status_url_key_placeholder_abuse_is_refused_at_install(self):
        base, port = self.dest.base, self.dest.server.server_port
        hostile = {
            "two_keys": base + "/status/{key}/{key}",
            "key_only_in_query": base + "/status?id={key}",
            "key_in_path_and_query": base + "/status/{key}?again={key}",
            "key_in_userinfo": f"http://{{key}}@127.0.0.1:{port}/status/{{key}}",
            "no_key": base + "/status/",
            "other_case_key": base + "/status/{KEY}",
            "stray_braces": base + "/status/{key}{}",
            "fragment": base + "/status/{key}#x",
            "other_port": f"http://127.0.0.1:{port + 1 if port < 65535 else port - 1}/status/{{key}}",
            "other_scheme": f"https://127.0.0.1:{port}/status/{{key}}",
            "other_host_name": f"http://localhost:{port}/status/{{key}}",
            "not_a_string": [base + "/status/{key}"],
        }
        for name, url in hostile.items():
            with self.subTest(status_url=name):
                with self.assertRaises(Rejected):
                    self.work.install_action("lookup-" + name, self.action(status_url=url))
                with self.work.tx() as db:
                    self.assertIsNone(ActionRegistry(db).get("lookup-" + name))
        with self.assertRaisesRegex(Rejected, "invalid_action"):
            self.work.install_action("local-lookup", {"kind": "local", "status_url": base + "/status/{key}"})

    def test_cross_origin_redirect_is_not_followed_and_credential_stays_home(self):
        foreign = self.destination()
        key = self.unknown()
        foreign.records[key] = self.dest.records[key]
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.dest.status = lambda k, code=code: (code, {"error": "moved"},
                                                         {"Location": f"{foreign.base}/status/{k}"})
                self.assert_not_verified(self.reconcile(key), "pending", "status_unavailable")
        self.assertEqual((foreign.gets, foreign.posts, foreign.auth), (0, 0, []))
        self.assert_unresolved(key)

        # A redirected write is unknown, not followed and not a no-write rejection.
        second, case = self.cell(self.action())
        self.dest.write = lambda k: (307, {"error": "moved"}, {"Location": f"{foreign.base}/write"})
        other = self.approved(second, case)
        self.assertEqual(second.commit(case, other, "operator")["status"], "unknown")
        self.assertEqual((foreign.gets, foreign.posts, foreign.auth), (0, 0, []))
        self.assertEqual(set(self.dest.auth), {"Bearer " + TOKEN})
        self.assert_unresolved(other, second, case)

    def test_request_digest_and_key_must_match_exactly(self):
        key = self.unknown()
        record = self.dest.records[key]
        exact = digest(ARGS)
        pretty = hashlib.sha256(json.dumps(ARGS, indent=2).encode()).hexdigest()
        self.assertNotEqual(pretty, exact)
        near_misses = {
            "digest_uppercase": {"request_digest": exact.upper()},
            "digest_prefixed": {"request_digest": "sha256:" + exact},
            "digest_padded": {"request_digest": exact + " "},
            "digest_truncated": {"request_digest": exact[:32]},
            "digest_noncanonical_json": {"request_digest": pretty},
            "digest_extra_field": {"request_digest": digest({**ARGS, "priority": "low"})},
            "digest_type_changed": {"request_digest": digest({"ticket": ["T-1"]})},
            "digest_of_the_key": {"request_digest": digest(key)},
            "digest_in_list": {"request_digest": [exact]},
            "digest_missing": {"request_digest": None},
            "key_uppercase": {"idempotency_key": key.upper()},
            "key_padded": {"idempotency_key": key + "\n"},
            "key_without_dashes": {"idempotency_key": key.replace("-", "")},
            "key_in_list": {"idempotency_key": [key]},
            "key_empty": {"idempotency_key": ""},
        }
        for name, change in near_misses.items():
            with self.subTest(near_miss=name):
                self.dest.status = reply({**record, **change})
                self.assert_not_verified(self.reconcile(key), "unverified", "destination_record_mismatch")
        self.assert_unresolved(key)

    def test_valid_record_of_a_sibling_proposal_cannot_settle_this_one(self):
        # Two cases with identical arguments share a request digest; only the key tells them apart.
        key_a = self.unknown()
        work_b, case_b = self.cell(self.action())
        key_b = self.unknown(work_b, case_b)
        record_a, record_b = self.dest.records[key_a], self.dest.records[key_b]
        self.assertEqual(record_a["request_digest"], record_b["request_digest"])
        sibling = {key_a: record_b, key_b: record_a}
        self.dest.status = lambda k: (200, sibling[k], {})
        self.assert_not_verified(self.reconcile(key_a), "unverified", "destination_record_mismatch")
        self.assert_not_verified(self.reconcile(key_b, work_b, case_b), "unverified", "destination_record_mismatch")
        self.assert_unresolved(key_a)
        self.assert_unresolved(key_b, work_b, case_b)

    def test_404_never_establishes_no_write(self):
        for statuses in ([404], [410], [200], [500], ["409"], [True], 409):
            with self.subTest(no_write_statuses=statuses), self.assertRaisesRegex(Rejected, "invalid_action"):
                self.work.install_action("no-write-" + json.dumps(statuses), self.action(no_write_statuses=statuses))
        # The destination stores the write but answers 404, as a lagging replica might.
        self.dest.write = lambda _k: (404, {"error": "not_found"}, {})
        key = self.approved()
        self.assertEqual(self.work.commit(self.case, key, "operator")["status"], "unknown")
        self.assertIn(key, self.dest.records)
        self.dest.status = lambda _k: (404, {"error": "not_found"}, {})
        self.assert_not_verified(self.reconcile(key), "pending", "not_found_not_proof")
        self.dest.status = lambda _k: (410, {"error": "gone"}, {})
        self.assert_not_verified(self.reconcile(key), "pending", "status_unavailable")
        self.assert_unresolved(key)
        self.assertEqual(self.dest.posts, 1)


class SimulatedEvidence(BridgeFloor):
    def test_simulation_states_never_verify(self):
        key = self.unknown()
        record = self.dest.records[key]
        states = ("simulated", "dry_run", "dry-run", "sandbox", "preview", "test", "would_commit",
                  "Committed", "COMMITTED", "committed ", "committed​", True, None, ["committed"])
        for state in states:
            with self.subTest(state=state):
                self.dest.status = reply({**record, "state": state})
                self.assert_not_verified(self.reconcile(key), "unverified", "destination_record_mismatch")
        for wrapper in ("simulated", "preview", "dry_run", "record"):
            with self.subTest(wrapper=wrapper):
                self.dest.status = reply({wrapper: record})
                self.assert_not_verified(self.reconcile(key), "unverified", "destination_record_mismatch")
        self.assert_unresolved(key)

    @unittest.expectedFailure
    def test_committed_record_labeled_simulated_does_not_verify(self):
        key = self.unknown()
        labeled = {**self.dest.records[key], "simulated": True, "mode": "simulation", "environment": "sandbox"}
        self.dest.status = reply(labeled)
        self.assert_not_verified(self.reconcile(key), "unverified")
        self.assert_unresolved(key)


class Schema(BridgeFloor):
    def test_malformed_status_bodies_fail_closed(self):
        key = self.unknown()
        record = self.dest.records[key]
        raw = {
            "empty": b"",
            "not_json": b"committed",
            "truncated": json.dumps(record).encode()[:-5],
            "null": b"null",
            "number": b"42",
            "string": b'"committed"',
            "array": json.dumps([record]).encode(),
            "nan": b"NaN",
            "html": b"<html>committed</html>",
            "oversized_valid_record": json.dumps({**record, "pad": "x" * 70000}).encode(),
        }
        typed = {
            "external_id_number": {"external_id": 1},
            "external_id_list": {"external_id": ["dest-" + key]},
            "external_id_object": {"external_id": {"id": "dest-" + key}},
            "external_id_true": {"external_id": True},
            "state_object": {"state": {"value": "committed"}},
            "digest_object": {"request_digest": {"sha256": record["request_digest"]}},
        }
        cases = {**raw, **{name: json.dumps({**record, **change}).encode() for name, change in typed.items()}}
        for name, body in cases.items():
            with self.subTest(body=name):
                self.dest.status = lambda _k, body=body: (200, body, {"Content-Type": "application/json"})
                result = self.reconcile(key)
                self.assertIn(result["status"], ("pending", "unverified"))
                self.assert_not_verified(result, result["status"])
        self.assert_unresolved(key)

    @unittest.expectedFailure
    def test_status_body_with_duplicate_keys_fails_closed(self):
        key = self.unknown()
        record = self.dest.records[key]
        body = json.dumps({**record, "state": "pending"})[:-1] + ', "state": "committed"}'
        self.dest.status = lambda _k: (200, body.encode(), {})
        self.assert_not_verified(self.reconcile(key), "unverified")
        self.assert_unresolved(key)

    def test_hostile_adapter_timeout_fails_closed(self):
        # The adapter installs, but a hostile timeout must never become a settled effect.
        for timeout in ("10", None, [], -1):
            with self.subTest(timeout_seconds=timeout):
                posts = self.dest.posts
                try:
                    work, case = self.cell(self.action(timeout_seconds=timeout))
                except Rejected:
                    continue
                key = self.approved(work, case)
                self.assertEqual(work.commit(case, key, "operator")["status"], "unknown")
                self.assertEqual(self.dest.posts, posts)
                before = len(work.inspect(case)["events"])
                try:
                    result = self.reconcile(key, work, case)
                except (Rejected, TypeError):
                    self.assertEqual(len(work.inspect(case)["events"]), before)
                else:
                    self.assert_not_verified(result, "pending")
                self.assert_unresolved(key, work, case)

    def test_repeated_lookups_never_reissue_the_write(self):
        key = self.unknown()
        before = len(self.work.inspect(self.case)["events"])
        self.dest.status = lambda _k: (503, {"error": "unavailable"}, {})
        for _ in range(20):
            self.assert_not_verified(self.reconcile(key), "pending", "status_unavailable")
        added = self.work.inspect(self.case)["events"][before:]
        self.assertEqual([e["kind"] for e in added], ["reconciliation_checked"] * 20)
        self.assertEqual((self.dest.posts, self.dest.gets), (1, 20))
        self.assert_unresolved(key)
        self.assertEqual(self.dest.posts, 1)

    def test_expired_worker_lease_is_unknown_and_never_reexecuted(self):
        work, case = self.cell(self.action(), effects="worker")
        key = self.approved(work, case)
        self.assertEqual(work.commit(case, key, "operator")["status"], "queued")
        start = time.time()
        self.assertEqual(work.lease_obligation(now=start)["state"], "leased")  # the worker then dies
        self.assertIsNone(run_once(work, now=start + 1))
        self.assertEqual(run_once(work, now=start + 31)["status"], "unknown")
        self.assertEqual(run_available(work), [])
        with work.tx() as db:
            row = db.execute("SELECT state, attempts FROM effect_obligations WHERE proposal_id=?", (key,)).fetchone()
        self.assertEqual((row["state"], row["attempts"]), ("unknown", 1))
        self.assertEqual(self.dest.posts, 0)
        # Nothing was written, but a 404 still cannot say so: the dead worker might have written.
        self.assert_not_verified(self.reconcile(key, work, case), "pending", "not_found_not_proof")
        self.assert_unresolved(key, work, case)
        self.assertEqual(run_available(work), [])
        self.assertEqual(self.dest.posts, 0)


class LookupRace(BridgeFloor):
    def test_worker_settlement_racing_the_lookup_is_rechecked(self):
        work, case = self.cell(self.action(), effects="worker")
        key = self.approved(work, case)
        self.assertEqual(work.commit(case, key, "operator")["status"], "queued")
        settled = []

        def settle_during_lookup(k):
            settled.append(run_once(work))  # the worker settles while the manager's lookup is in flight
            return 200, self.dest.records[k], {}

        self.dest.status = settle_during_lookup
        with self.assertRaisesRegex(Rejected, "effect resolved during status lookup"):
            self.reconcile(key, work, case)
        self.assertEqual(settled[0]["status"], "succeeded")
        events = work.inspect(case)["events"]
        self.assertEqual([e["kind"] for e in events if e["kind"] in ("effect_succeeded", "reconciled")],
                         ["effect_succeeded"])
        self.assertFalse(any(e["kind"] == "reconciliation_checked" for e in events))
        self.assertEqual(self.dest.posts, 1)

    def test_concurrent_reconciliation_appends_one_proof(self):
        key = self.unknown()
        inner = []

        def second_manager(k):
            if not inner:
                inner.append(None)
                inner[0] = self.reconcile(k)  # its own lookup reaches the else branch below
            return 200, self.dest.records[k], {}

        self.dest.status = second_manager
        with self.assertRaisesRegex(Rejected, "already reconciled"):
            self.reconcile(key)
        self.assertEqual(inner[0]["status"], "verified")
        events = self.work.inspect(self.case)["events"]
        self.assertEqual(sum(e["kind"] == "reconciled" for e in events), 1)
        self.assertEqual(sum(verified_reconciliation(e) for e in events), 1)
        self.assertEqual(self.dest.posts, 1)


class Cost(BridgeFloor):
    def test_status_lookup_is_bounded_in_time_and_size(self):
        work, case = self.cell(self.action(timeout_seconds=0.5))
        key = self.unknown(work, case)
        release = threading.Event()
        self.addCleanup(release.set)

        def stall(k):
            release.wait(10)
            return 200, self.dest.records[k], {}

        self.dest.status = stall
        start = time.monotonic()
        self.assert_not_verified(self.reconcile(key, work, case), "pending", "status_unavailable")
        self.assertLess(time.monotonic() - start, 5)
        release.set()

        oversized = json.dumps({**self.dest.records[key], "pad": "x" * 65536}).encode()
        self.dest.status = lambda _k: (200, oversized, {})
        self.assert_not_verified(self.reconcile(key, work, case), "pending", "status_unavailable")
        self.assert_unresolved(key, work, case)

        # A configured hour is capped at the 30-second ceiling.
        slow, slow_case = self.cell(self.action(timeout_seconds=3600))
        slow_key = self.unknown(slow, slow_case)
        self.dest.status = None
        with patch.object(core.HTTP, "open", wraps=core.HTTP.open) as opened:
            self.assertEqual(self.reconcile(slow_key, slow, slow_case)["status"], "verified")
        self.assertEqual(opened.call_args.kwargs["timeout"], 30)


class EconomicFloors(unittest.TestCase):
    """Reuses the economic settlement simulator; nothing here moves funds."""

    setUp = economic.EconomicActions.setUp
    case = economic.EconomicActions.case
    propose = economic.EconomicActions.propose
    approved = economic.EconomicActions.approved

    def reservation(self, key):
        with self.work.tx() as db:
            return db.execute("SELECT state FROM economic_reservations WHERE proposal_id=?", (key,)).fetchone()[0]

    def reserved_total(self):
        with self.work.tx() as db:
            return ReservationStore(db).reserved_total("research-q3", "USDC:base")

    def lost(self):
        case, evidence = self.case()
        key = self.approved(case, evidence)
        self.mode = "lost"
        self.assertEqual(self.work.commit(case, key, "operator")["status"], "unknown")
        self.mode = "success"
        return case, evidence, key

    def assert_budget_held(self, case, evidence, key):
        self.assertEqual(self.reservation(key), "reserved")
        self.assertEqual(self.reserved_total(), 900000)
        self.assertFalse(self.work.inspect(case)["complete"])
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["reason"], "economic_budget_exceeded")
        self.assertEqual(self.propose(case, evidence)["decision"]["reason"], "effect_unresolved:buy_data")

    def test_testnet_settlement_record_neither_verifies_nor_releases_budget(self):
        case, evidence, key = self.lost()
        record = self.records[key]
        simulated = {"asset": "USDC:base-sepolia", "state": "simulated", "counterparty": "vendor-A-sandbox",
                     "amount_units": "0", "external_id": ""}
        for field, value in simulated.items():
            with self.subTest(field=field):
                self.records[key] = {**record, field: value}
                result = self.work.reconcile(case, key, "manager", ["manager"])
                self.assertEqual((result["status"], result["reason"]), ("unverified", "destination_record_mismatch"))
                self.assertFalse(verified_reconciliation(result["event"]))
                self.assert_budget_held(case, evidence, key)

    @unittest.expectedFailure
    def test_settlement_record_on_another_rail_does_not_verify(self):
        case, evidence, key = self.lost()
        self.records[key] = {**self.records[key], "rail": "simulated"}
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "unverified")
        self.assert_budget_held(case, evidence, key)

    def test_budget_held_through_every_unresolved_path(self):
        case, evidence, key = self.lost()
        record = self.records.pop(key)
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["reason"], "not_found_not_proof")
        self.assert_budget_held(case, evidence, key)
        self.records[key] = {**record, "request_digest": digest({"amount_units": "900000"})}
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "unverified")
        self.assert_budget_held(case, evidence, key)
        with self.assertRaisesRegex(Rejected, "effect_claimed"):
            self.work.commit(case, key, "operator")
        with self.assertRaisesRegex(Rejected, "cancellation_denied"):
            self.work.cancel_case(case, "abandon", "manager", ["manager"])
        self.assert_budget_held(case, evidence, key)
        # A verified lookup settles the reservation; it never releases it back to the budget.
        self.records[key] = record
        self.assertEqual(self.work.reconcile(case, key, "manager", ["manager"])["status"], "verified")
        self.assertEqual(self.reservation(key), "settled")
        self.assertEqual(self.reserved_total(), 900000)
        other, other_evidence = self.case()
        self.assertEqual(self.propose(other, other_evidence)["decision"]["reason"], "economic_budget_exceeded")


class PositiveControl(BridgeFloor):
    def test_matching_destination_lookup_reconciles(self):
        """Guards against refuse-everything: the same fixtures still verify a real matching record."""
        key = self.unknown()
        result = self.reconcile(key)
        self.assertEqual((result["status"], result["event"]["kind"]), ("verified", "reconciled"))
        proof = result["event"]["body"]["proof"]
        self.assertEqual(proof["idempotency_key"], key)
        self.assertEqual(proof["request_digest"], digest(ARGS))
        self.assertEqual(proof["external_id"], "dest-" + key)
        self.assertEqual(proof["response_digest"], digest(self.dest.records[key]))
        self.assertTrue(verified_reconciliation(result["event"]))
        self.assertTrue(self.work.inspect(self.case)["complete"])
        self.assertEqual(self.dest.auth, ["Bearer " + TOKEN] * 2)
        self.assertEqual(self.dest.posts, 1)


class QuotaFloor(unittest.TestCase):
    ADVERSARIAL = (DigestAndIdentity, SimulatedEvidence, Schema, LookupRace, Cost, EconomicFloors)

    def test_tagged_cases_exist_and_meet_floors(self):
        tests = {name: getattr(cls, name) for cls in self.ADVERSARIAL for name in dir(cls) if name.startswith("test_")}
        self.assertEqual(set(CASES), set(tests))
        self.assertLessEqual(set(MISSING_HOOK), set(CASES))
        for name, method in tests.items():
            with self.subTest(case=name):
                self.assertEqual(getattr(method, "__unittest_expecting_failure__", False), name in MISSING_HOOK)
        for category, floor in FLOORS.items():
            with self.subTest(category=category):
                executable = sum(c == category and name not in MISSING_HOOK for name, (c, _) in CASES.items())
                self.assertGreaterEqual(executable, floor)


if __name__ == "__main__":
    unittest.main()
