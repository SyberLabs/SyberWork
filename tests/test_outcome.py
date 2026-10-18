"""The intervention effect: an admitted change settled by whether a loopback metric moved as predeclared."""

import json
import os
import time
from unittest.mock import patch

from examples.metric_source import MetricSource
from spec.validate import validate_events
from syberlabs import Rejected, publish
from syberlabs.build import HOST, Kit
from syberlabs.outcome import Intervention
from tests.test_build_thread import GOOD, Crash, RepoCase, default_contract, git

FACTS = [{"key": "accepted", "source": "git", "verified": True}]
BOUND = {"commit": "fact:accepted.commit"}
WINDOW = 600
TOKEN_ENV = "TEST_METRIC_TOKEN"


class Interventions(RepoCase):
    def setUp(self):
        super().setUp()
        self.bare = self.root.parent / "remote.git"
        git(self.root.parent, "init", "-q", "--bare", str(self.bare))
        git(self.root, "remote", "add", "origin", str(self.bare))
        git(self.root, "push", "-q", "origin", "main")
        self.source = MetricSource(port=0)
        self.addCleanup(self.source.close)
        self.source.token = "metric-token"
        os.environ[TOKEN_ENV] = "metric-token"
        self.addCleanup(os.environ.pop, TOKEN_ENV, None)
        self.clock = [time.time()]

    def definition(self, **changes):
        doc = {"kind": "local", "effect": "intervention", "metric_url": self.source.url, "metric_key": "conversion",
               "window_seconds": WINDOW, "expectation": {"direction": "up", "min_delta": 1},
               "max_age_seconds": 3600, "auth_env": TOKEN_ENV}
        doc.update(changes)
        return doc

    def open(self, definition=None, extra=None, push=True):
        """A kit on a fake clock, an accepted candidate, and (by default) its branch pushed to the bare remote."""
        doc = default_contract(self.root)
        measured = {"measure": definition or self.definition(), **(extra or {})}
        doc["actions"]["push_branch"] = {"requires_effect": "accept_change", "required_facts": FACTS, "arguments": BOUND}
        for name in measured:
            doc["actions"][name] = {"requires_effect": "push_branch", "required_facts": FACTS, "arguments": BOUND}
        policy = {"version": 1, "actions": {name: {"roles": ["developer"]} for name in doc["actions"]}}
        home = self.root / ".syberlabs"
        home.mkdir(exist_ok=True)
        actions = {"push_branch": {"kind": "local", "effect": "git_push", "remote": "origin", "settle_seconds": 0}, **measured}
        (home / "actions.json").write_text(json.dumps(actions))
        kit = self.kit(doc, policy)
        kit.session.clock = lambda: self.clock[0]
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        self.assertEqual(thread.accept("c1").status, "succeeded")
        if push:
            self.assertEqual(thread.publish("push_branch").status, "succeeded")
        return kit, thread

    def claim(self, value=10.0):
        kit, thread = self.open()
        self.source.set("conversion", value, observed_at=self.clock[0])
        return kit, thread, thread.publish("measure")

    def observations(self, thread, phase=None):
        return [e for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == "intervention"
                and (phase is None or e["body"]["value"].get("phase") == phase)]

    def settle(self, thread, value=None):
        """Move the metric (optionally), cross the window on the fake clock, and recover the one open claim."""
        if value is not None:
            self.source.set("conversion", value, observed_at=self.clock[0])
        self.clock[0] += WINDOW + 1
        [receipt] = thread.recover()
        return receipt

    def test_claim_records_the_baseline_and_stays_unknown(self):
        kit, thread, receipt = self.claim()
        self.assertEqual((receipt.status, receipt.reason), ("unknown", "effect_unknown"))
        self.assertIn("syberlabs recover", receipt.hint)
        claim, baseline, unknown = thread.history()[-3:]
        self.assertEqual((claim["kind"], baseline["kind"], unknown["kind"]), ("effect_started", "observed", "effect_unknown"))
        self.assertEqual(baseline["seq"], claim["seq"] + 1)
        body = baseline["body"]
        self.assertEqual((body["key"], body["source"], body["actor"], body["verified"], body["version"]),
                         ("intervention", "metric:conversion", HOST, True, "conversion-v1"))
        record = body["value"]
        self.assertEqual((record["phase"], record["metric"], record["value"], record["proposal_id"], record["commit"]),
                         ("baseline", "conversion", 10.0, claim["body"]["proposal_id"], thread.candidates()[0].commit))
        self.assertEqual(record["observed_at"], self.clock[0])
        self.assertIsInstance(record["read_at"], float)
        self.assertEqual((unknown["body"]["error"], unknown["body"]["proposal_id"]), ("window_open", claim["body"]["proposal_id"]))
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "absent_within_settle_window"))
        self.assertEqual(thread.history()[-1]["kind"], "reconciliation_checked")
        again = thread.publish("measure")
        self.assertEqual((again.status, again.reason), ("denied", "effect_unresolved:measure"))
        self.assertEqual(len(self.observations(thread)), 1)
        self.assertEqual(validate_events(thread.history(), "claim"), [])

    def test_applied_when_the_metric_moved_as_predeclared(self):
        kit, thread, _ = self.claim()
        receipt = self.settle(thread, 11.5)
        self.assertEqual((receipt.status, receipt.reason, receipt.external_id, receipt.ref),
                         ("succeeded", "destination_state_matched", "conversion-v2", "measure"))
        reconciled = thread.history()[-1]
        self.assertEqual(reconciled["kind"], "reconciled")
        proof = reconciled["body"]["proof"]
        self.assertEqual((proof["verified"], proof["external_id"]), (True, "conversion-v2"))
        self.assertTrue(proof["request_digest"] and proof["response_digest"])
        [settlement] = self.observations(thread, "settlement")
        self.assertEqual(settlement["seq"], reconciled["seq"] - 1)
        self.assertEqual((settlement["body"]["source"], settlement["body"]["version"], settlement["body"]["actor"],
                          settlement["body"]["verified"]), ("metric:conversion", "conversion-v2", HOST, True))
        record = settlement["body"]["value"]
        self.assertEqual((record["outcome"], record["delta"], record["baseline"], record["baseline_version"],
                          record["value"], record["version"]), ("moved", 1.5, 10.0, "conversion-v1", 11.5, "conversion-v2"))
        self.assertEqual((record["direction"], record["min_delta"], record["window_seconds"], record["commit"]),
                         ("up", 1, WINDOW, thread.candidates()[0].commit))
        status = thread.status()
        self.assertEqual(status["open"], [])
        self.assertNotIn("syberlabs publish measure", status["next"])
        again = thread.publish("measure")
        self.assertEqual((again.status, again.reason), ("denied", "action_already_completed"))
        self.assertEqual(thread.recover(), [])
        self.assertEqual(validate_events(thread.history(), "applied"), [])

    def test_not_applied_when_unchanged_after_the_window(self):
        kit, thread, _ = self.claim()
        receipt = self.settle(thread)
        self.assertEqual((receipt.status, receipt.reason), ("not_applied", "destination_state_unchanged"))
        rejected = thread.history()[-1]
        self.assertEqual((rejected["kind"], rejected["body"]["status"]), ("effect_rejected", "not_applied"))
        [settlement] = self.observations(thread, "settlement")
        self.assertEqual((settlement["body"]["value"]["outcome"], settlement["body"]["value"]["delta"]), ("unchanged", 0.0))
        self.assertIn("syberlabs publish measure", thread.status()["next"])
        fresh = thread.publish("measure")
        self.assertEqual((fresh.status, fresh.reason), ("unknown", "effect_unknown"))
        baselines = self.observations(thread, "baseline")
        self.assertEqual([b["body"]["version"] for b in baselines], ["conversion-v1", "conversion-v1"])
        self.assertNotEqual(baselines[0]["body"]["value"]["proposal_id"], baselines[1]["body"]["value"]["proposal_id"])
        self.assertEqual(validate_events(thread.history(), "unchanged"), [])

    def test_not_applied_when_moved_against(self):
        kit, thread, _ = self.claim()
        receipt = self.settle(thread, 8.0)
        self.assertEqual((receipt.status, receipt.reason), ("not_applied", "destination_state_unchanged"))
        self.assertEqual(thread.history()[-1]["body"]["status"], "not_applied")
        [settlement] = self.observations(thread, "settlement")
        self.assertEqual((settlement["body"]["value"]["outcome"], settlement["body"]["value"]["delta"]), ("moved_against", -2.0))
        self.assertEqual(thread.publish("measure").status, "unknown", "a fresh claim is admitted after moved_against")
        self.assertEqual(validate_events(thread.history(), "against"), [])

    def test_small_moves_are_unchanged_not_applied(self):
        down = self.definition(metric_key="latency", expectation={"direction": "down", "min_delta": 1})
        kit, thread = self.open(extra={"measure_down": down})
        self.source.set("conversion", 10.0, observed_at=self.clock[0])
        self.source.set("latency", 10.0, observed_at=self.clock[0])
        self.assertEqual(thread.publish("measure").status, "unknown")
        self.assertEqual(thread.publish("measure_down").status, "unknown")
        self.source.set("conversion", 10.9, observed_at=self.clock[0])
        self.source.set("latency", 9.1, observed_at=self.clock[0])
        self.clock[0] += WINDOW + 1
        receipts = thread.recover()
        self.assertEqual([(r.ref, r.status, r.reason) for r in receipts],
                         [("measure", "not_applied", "destination_state_unchanged"),
                          ("measure_down", "not_applied", "destination_state_unchanged")])
        self.assertEqual([(e["body"]["value"]["metric"], e["body"]["value"]["outcome"]) for e in self.observations(thread, "settlement")],
                         [("conversion", "unchanged"), ("latency", "unchanged")])
        with self.subTest("down direction, moved as predeclared"):
            self.assertEqual(thread.publish("measure_down").status, "unknown")
            self.source.set("latency", 8.0, observed_at=self.clock[0])
            self.clock[0] += WINDOW + 1
            [receipt] = thread.recover()
            self.assertEqual((receipt.status, receipt.external_id), ("succeeded", "latency-v3"))
            self.assertEqual(self.observations(thread, "settlement")[-1]["body"]["value"]["outcome"], "moved")
        self.assertEqual(validate_events(thread.history(), "small"), [])

    def test_unknown_when_the_source_is_unavailable_at_settlement(self):
        kit, thread, _ = self.claim()
        self.source.set("conversion", 11.5, observed_at=self.clock[0])
        self.source.fail = True
        self.clock[0] += WINDOW + 1
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "status_unavailable"))
        self.assertEqual(thread.history()[-1]["kind"], "reconciliation_checked")
        self.assertEqual(self.observations(thread, "settlement"), [])
        self.assertEqual(thread.publish("measure").reason, "effect_unresolved:measure")
        self.source.fail = False
        self.source.unversioned = True
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "status_unavailable"))
        self.source.unversioned = False
        [settled] = thread.recover()
        self.assertEqual((settled.status, settled.external_id), ("succeeded", "conversion-v2"))
        self.assertEqual(len(self.observations(thread, "settlement")), 1)
        self.assertEqual(validate_events(thread.history(), "unavailable"), [])

    def test_unknown_when_the_reading_is_stale(self):
        kit, thread, _ = self.claim()
        self.source.set("conversion", 12.0, observed_at=self.clock[0] - 7200)
        self.clock[0] += WINDOW + 1
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "status_unavailable"))
        self.assertEqual(self.observations(thread, "settlement"), [])
        self.source.set("conversion", 12.0, observed_at=self.clock[0])
        [settled] = thread.recover()
        self.assertEqual((settled.status, settled.external_id), ("succeeded", "conversion-v3"))
        self.assertEqual(validate_events(thread.history(), "stale"), [])

    def test_claim_is_refused_without_a_write_when_it_cannot_read_a_fresh_versioned_baseline(self):
        kit, thread = self.open()
        fresh = lambda: self.source.set("conversion", 10.0, observed_at=self.clock[0])
        fresh()
        cases = {
            "metric_unavailable": (lambda: setattr(self.source, "fail", True), lambda: setattr(self.source, "fail", False), 409),
            "source_unversioned": (lambda: setattr(self.source, "unversioned", True),
                                   lambda: setattr(self.source, "unversioned", False), 428),
            "baseline_stale": (lambda: self.source.set("conversion", 10.0, observed_at=self.clock[0] - 7200), fresh, 428),
            "metric_shape": (lambda: self.source.set("conversion", "ten"), fresh, 409),
            "metric_too_large": (lambda: self.source.set("conversion", "x" * 70_000), fresh, 409),
            "missing_credential": (lambda: os.environ.pop(TOKEN_ENV), lambda: os.environ.__setitem__(TOKEN_ENV, "metric-token"), 409),
        }
        for code, (arrange, restore, status) in cases.items():
            with self.subTest(code):
                arrange()
                try:
                    receipt = thread.publish("measure")
                finally:
                    restore()
                self.assertEqual((receipt.status, receipt.reason), ("rejected", code))
                claim, rejected = thread.history()[-2:]
                self.assertEqual((claim["kind"], rejected["kind"], rejected["body"]["status"]), ("effect_started", "effect_rejected", status))
                self.assertEqual(self.observations(thread), [])
        self.assertEqual(thread.publish("measure").status, "unknown")
        self.assertEqual(len(self.observations(thread, "baseline")), 1)
        self.assertEqual(validate_events(thread.history(), "refused"), [])

    def test_forged_values_do_not_change_the_verdict(self):
        kit, thread = self.open()
        self.source.set("conversion", 10.0, observed_at=self.clock[0])
        commit = thread.candidates()[0].commit
        thread._accepted()
        # (a) Extra proposal arguments pass admission (only commit is bound) and are never read.
        proposed = kit.session.propose(thread.id, "measure", {"commit": commit, "baseline": 0, "value": 999, "outcome": "moved"},
                                       kit.actor, ["developer"])
        self.assertEqual(proposed["decision"]["status"], "allowed")
        pid = proposed["proposal"]["id"]
        self.assertEqual(kit.session.commit(thread.id, pid, kit.actor)["status"], "unknown")
        [baseline] = self.observations(thread, "baseline")
        self.assertEqual(baseline["body"]["value"]["value"], 10.0)
        # (b, c) A baseline and a settlement appended after the claim are not at claim.seq + 1 and are ignored.
        forged = {"proposal_id": pid, "phase": "baseline", "metric": "conversion", "commit": commit, "value": -1000,
                  "version": "forged-v9", "observed_at": self.clock[0], "read_at": self.clock[0]}
        kit.session.observe(thread.id, "intervention", forged, "metric:conversion", "forged-v9", HOST, verified=True)
        kit.session.observe(thread.id, "intervention", {**forged, "phase": "settlement", "value": 999, "delta": 1999, "outcome": "moved"},
                            "metric:conversion", "forged-v10", HOST, verified=True)
        self.clock[0] += WINDOW + 1
        [receipt] = thread.recover()
        self.assertEqual((receipt.status, receipt.reason), ("not_applied", "destination_state_unchanged"))
        settlement = self.observations(thread, "settlement")[-1]["body"]
        self.assertEqual((settlement["version"], settlement["value"]["baseline"], settlement["value"]["outcome"]),
                         ("conversion-v1", 10.0, "unchanged"))
        self.assertEqual(validate_events(thread.history(), "forged"), [])

    def test_status_is_deterministic_on_replay(self):
        kit, thread, _ = self.claim()
        args = {"commit": thread.candidates()[0].commit}
        pid = thread.history()[-1]["body"]["proposal_id"]
        executor = publish.executor(kit, kit.session.actions["measure"])
        self.assertIsInstance(executor, Intervention)
        self.assertEqual(executor.settle_seconds, WINDOW)
        first = executor.status(thread.id, args, pid)
        self.assertEqual(first[0], "absent")
        self.assertEqual(executor.status(thread.id, args, pid), first)
        length = len(thread.history())
        reopened = Kit.local(self.root / ".syberlabs", self.root, actor=kit.actor)
        self.addCleanup(reopened.close)
        reopened.session.clock = lambda: self.clock[0]
        self.assertEqual(publish.executor(reopened, reopened.session.actions["measure"]).status(thread.id, args, pid), first)
        self.assertEqual(len(thread.history()), length, "status inside the window writes nothing")
        self.source.set("conversion", 11.5, observed_at=self.clock[0])
        self.clock[0] += WINDOW + 1
        self.assertEqual(thread.recover()[0].status, "succeeded")
        head = thread.history()[-1]["hash"]
        self.assertEqual(kit.session.replay(thread.id, 1, 1)["changed_decisions"], [])
        with self.assertRaises(Rejected) as caught:
            kit.session.reconcile(thread.id, pid, kit.actor, ["developer"])
        self.assertEqual(caught.exception.code, "reconciliation_denied")
        self.source.set("conversion", 0.0, observed_at=self.clock[0])
        self.assertEqual(thread.recover(), [])
        self.assertEqual(thread.history()[-1]["hash"], head)
        self.assertTrue(kit.session.verify_chain(thread.id))
        self.assertEqual(validate_events(thread.history(), "replay"), [])

    def test_crash_after_the_baseline_is_recovered(self):
        kit, thread = self.open()
        self.source.set("conversion", 10.0, observed_at=self.clock[0])
        original = Intervention.apply

        def dies(effect, case_id, args, key):
            try:
                original(effect, case_id, args, key)
            except Rejected as exc:
                if exc.code != "window_open":
                    raise
            raise Crash()

        with patch.object(Intervention, "apply", dies), self.assertRaises(Crash):
            thread.publish("measure")
        self.assertEqual([e["kind"] for e in thread.history()[-2:]], ["effect_started", "observed"])
        status = thread.status()
        self.assertIn("syberlabs recover", status["next"])
        self.assertNotIn("syberlabs publish measure", status["next"])
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "absent_within_settle_window"))
        receipt = self.settle(thread, 11.5)
        self.assertEqual((receipt.status, receipt.external_id), ("succeeded", "conversion-v2"))
        self.assertEqual(validate_events(thread.history(), "crash_after"), [])

    def test_crash_before_the_baseline_voids_the_claim_after_the_window(self):
        kit, thread = self.open()
        self.source.set("conversion", 10.0, observed_at=self.clock[0])

        def dies(effect, case_id, args, key):
            raise Crash()

        with patch.object(Intervention, "apply", dies), self.assertRaises(Crash):
            thread.publish("measure")
        self.assertEqual(thread.history()[-1]["kind"], "effect_started")
        self.assertEqual(self.observations(thread), [])
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "absent_within_settle_window"))
        self.assertEqual(thread.publish("measure").reason, "effect_unresolved:measure")
        self.clock[0] += WINDOW + 1
        [void] = thread.recover()
        self.assertEqual((void.status, void.reason), ("not_applied", "destination_state_unchanged"))
        self.assertEqual(self.observations(thread), [], "nothing is measured without a baseline")
        self.source.set("conversion", 10.0, observed_at=self.clock[0])
        self.assertEqual(thread.publish("measure").status, "unknown")
        self.assertEqual(validate_events(thread.history(), "crash_before"), [])

    def test_action_definitions_are_validated(self):
        good = self.definition()
        publish.validate(good)
        publish.validate({"kind": "local", "effect": "intervention",
                          "title": "Checkout conversion must rise by 0.5 points within a week",
                          "metric_url": "http://127.0.0.1:8998/metrics/{key}", "metric_key": "checkout_conversion_7d",
                          "window_seconds": 604800, "expectation": {"direction": "up", "min_delta": 0.5},
                          "max_age_seconds": 3600, "auth_env": "METRICS_TOKEN", "timeout_seconds": 10})
        publish.validate({**good, "metric_url": "https://metrics.example.com/v1/{key}"})
        loopback = f"http://127.0.0.1:{self.source.port}"
        bad = {
            "public http": {**good, "metric_url": "http://example.com/metrics/{key}"},
            "https localhost": {**good, "metric_url": "https://localhost/metrics/{key}"},
            "https private address": {**good, "metric_url": "https://10.0.0.1/metrics/{key}"},
            "loopback without a port": {**good, "metric_url": "http://127.0.0.1/metrics/{key}"},
            "credentials in the url": {**good, "metric_url": f"http://user:pw@127.0.0.1:{self.source.port}/metrics/{{key}}"},
            "two keys": {**good, "metric_url": loopback + "/{key}/{key}"},
            "key in the query": {**good, "metric_url": loopback + "/metrics?k={key}"},
            "fragment": {**good, "metric_url": loopback + "/metrics/{key}#x"},
            "other braces": {**good, "metric_url": loopback + "/{tenant}/{key}"},
            "no key": {**good, "metric_url": loopback + "/metrics/conversion"},
            "settle_seconds": {**good, "settle_seconds": 600},
            "min_delta zero": {**good, "expectation": {"direction": "up", "min_delta": 0}},
            "min_delta negative": {**good, "expectation": {"direction": "up", "min_delta": -1}},
            "min_delta bool": {**good, "expectation": {"direction": "up", "min_delta": True}},
            "min_delta nan": {**good, "expectation": {"direction": "up", "min_delta": float("nan")}},
            "direction sideways": {**good, "expectation": {"direction": "sideways", "min_delta": 1}},
            "expectation extra field": {**good, "expectation": {"direction": "up", "min_delta": 1, "tolerance": 0}},
            "expectation missing": {k: v for k, v in good.items() if k != "expectation"},
            "window zero": {**good, "window_seconds": 0},
            "window bool": {**good, "window_seconds": True},
            "window too long": {**good, "window_seconds": 2_592_001},
            "window missing": {k: v for k, v in good.items() if k != "window_seconds"},
            "max_age text": {**good, "max_age_seconds": "1h"},
            "max_age zero": {**good, "max_age_seconds": 0},
            "auth_env lower": {**good, "auth_env": "lower"},
            "timeout zero": {**good, "timeout_seconds": 0},
            "key slash": {**good, "metric_key": "a/b"},
            "key newline": {**good, "metric_key": "a\nb"},
            "key empty": {**good, "metric_key": ""},
            "key too long": {**good, "metric_key": "k" * 201},
            "title number": {**good, "title": 3},
            "unknown field": {**good, "url": "x"},
            "wrong kind": {**good, "kind": "http"},
        }
        for label, doc in bad.items():
            with self.subTest(label):
                with self.assertRaises(Rejected) as caught:
                    publish.validate(doc)
                self.assertEqual(caught.exception.code, "invalid_action")
        with self.assertRaises(Rejected) as caught:
            self.open(definition={**good, "settle_seconds": 600})
        self.assertIn("unknown field settle_seconds for intervention", caught.exception.detail)

    def test_status_lists_the_intervention_as_a_next_step(self):
        kit, thread = self.open(push=False)
        self.source.set("conversion", 10.0, observed_at=self.clock[0])
        early = thread.publish("measure")
        self.assertEqual((early.status, early.reason), ("denied", "required_prior_effect_missing"))
        self.assertEqual(thread.publish("push_branch").status, "succeeded")
        status = thread.status()
        self.assertIn("syberlabs publish measure", status["next"])
        self.assertNotIn("syberlabs publish push_branch", status["next"])
        self.assertEqual(thread.publish("measure").status, "unknown")
        status = thread.status()
        self.assertIn("syberlabs recover", status["next"])
        self.assertEqual([step for step in status["next"] if step.startswith("syberlabs publish")], [])
        self.assertEqual(validate_events(thread.history(), "status"), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
