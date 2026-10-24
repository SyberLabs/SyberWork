"""The A/B report: two arms of one experiment, paired by item, every number from the journal.

The synthetic journals below are written event by event, as in ``tests/test_metrics.py``, so each expected number can
be read off the events that make it. The label is then written by the real host path on a real thread, and an end-to-end
run puts two work items through both arms of the repair loop with a stand-in model command. No model is called.
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from spec.validate import validate_events
from syberlabs import experiment, metrics
from syberlabs.cli import main as cli
from syberlabs.providers import RepairProvider
from tests.test_build_thread import EXPORT, GOOD, TEST_EXPORT, RepoCase
from tests.test_metrics import HOUR, T0, Journal, check

NAME = "githits-pilot"
BASE = "a" * 40


def label(item, arm, tags=("needs_lookup",), name=NAME):
    return {"name": name, "item": item, "arm": arm, "tags": list(tags), "by": "owner@example.test",
            "at": "2026-10-24T00:00:00Z"}


def run(case, item, arm, *, calls=1, dollars=None, githits=0, web=0, server="", tags=("needs_lookup",), hours=1.0,
        base=BASE, objective=None, late=False, model="claude-opus-5-5", accepted=True, tools=True, contract=4,
        extra_label=None, review=None, review_revision=None):
    """One labelled thread: ``calls`` proposals, the first ``calls - 1`` failing the tests, then acceptance."""
    j = Journal(case)
    e = {"created": j.add("case_created", {"actor": "agent@example.test", "contract": ["repo-change", contract],
                                           "inputs": {"objective": objective or f"Work item {item}"}}, at=T0)}
    j.fact("base", {"commit": base})
    j.fact("unattended", {"unattended": True, "by": "runner@example.test"})
    if not late:
        e["label"] = j.fact(experiment.KEY, label(item, arm, tags), version=NAME)
    if extra_label is not None:
        j.fact(experiment.KEY, extra_label, version=NAME)
    j.add("search_started", {"id": "s1", "actor": "syberlabs.host"})
    harness = {"name": "claude-code", "model": model, **({"mcp_servers": [server]} if server else {})}
    for n in range(1, calls + 1):
        signal = {"model_usage": {"input_tokens": 1000 + n, "output_tokens": 200, "model": model}, "harness": harness}
        if tools:
            signal["tool_usage"] = {"githits": githits if n == 1 else 0, "web_search": web if n == 1 else 0}
        e[f"c{n}"] = j.add("candidate_registered", {"id": f"c{n}", "operator": "patch", "actor": "syberlabs.host",
                                                    "signal": signal})
        if late and n == 1:
            e["label"] = j.fact(experiment.KEY, label(item, arm, tags), version=NAME)
        j.add("candidate_evaluated", {"candidate": f"c{n}", "actor": "syberlabs.host",
                                      "checks": [check("tests", "passed" if n == calls else "failed")]})
    spend = {"dollars": dollars if dollars is not None else round(0.01 * calls, 6), "seconds": 1.0, "evaluations": calls,
             "candidates": calls, "host_observed": True, "unknown_models": []}
    j.add("search_finished", {"id": "s1", "actor": "syberlabs.host", "spend": spend})
    commit = hashlib.sha1(case.encode()).hexdigest()
    e["commit"] = commit
    if accepted:
        j.add("proposed", {"id": "p1", "action": "accept_change", "actor": "runner@example.test",
                           "args": {"candidate": f"c{calls}", "commit": commit, "base": base}})
        j.add("decision", {"proposal_id": "p1", "status": "allowed", "reason": "allowed"})
        e["accepted"] = j.add("effect_succeeded", {"proposal_id": "p1", "action": "accept_change", "output": {}},
                              at=T0 + int(hours * HOUR))
    if review is not None:
        revision = review_revision or commit[:12]
        e["review"] = j.fact("review", {"verdict": review, "reviewer": "blind", "revision": revision}, version=revision)
    return j.events, e


def pilot():
    """Three items run in both arms, the treatment better on two of them, plus one item run in the treatment only."""
    events = []
    for case, item, arm, kwargs in [
        ("a-off", "I1", "off", dict(calls=3, web=4, hours=3.0)),
        ("a-on", "I1", "on", dict(calls=1, githits=3, server="githits:connected", hours=1.0)),
        ("b-off", "I2", "off", dict(calls=2, web=2, hours=2.0)),
        ("b-on", "I2", "on", dict(calls=1, githits=1, web=1, server="githits:connected", hours=1.5)),
        ("c-off", "I3", "off", dict(calls=1, tags=("routine",), hours=0.5)),
        ("c-on", "I3", "on", dict(calls=1, githits=0, server="githits:connected", tags=("routine",), hours=0.5)),
        ("d-on", "I4", "on", dict(calls=1, githits=2, server="githits:connected")),
    ]:
        events += run(case, item, arm, **kwargs)[0]
    return events


class Statistics(unittest.TestCase):
    def test_the_sign_test_is_exact_and_two_sided(self):
        self.assertEqual(experiment.sign_test(6, 0), 0.03125, "six wins of six: the fewest pairs that reach p < 0.05")
        self.assertEqual(experiment.sign_test(5, 0), 0.0625, "five of five cannot")
        self.assertEqual(experiment.sign_test(0, 5), 0.0625, "two-sided: losses count the same")
        self.assertEqual(experiment.sign_test(3, 3), 1.0)
        self.assertEqual(experiment.sign_test(8, 2), 0.109375)
        self.assertIsNone(experiment.sign_test(0, 0), "every pair tied: no test")

    def test_the_bootstrap_is_seeded_and_brackets_the_mean(self):
        diffs = [-3.0, -1.0, -2.0, 0.0, -1.5, -0.5]
        interval = experiment.bootstrap(diffs)
        self.assertEqual(interval, experiment.bootstrap(diffs), "seed 0: two readers get the same interval")
        self.assertLessEqual(interval[0], sum(diffs) / len(diffs))
        self.assertGreaterEqual(interval[1], sum(diffs) / len(diffs))
        self.assertIsNone(experiment.bootstrap([-1.0]), "one pair has no interval")

    def test_compare_counts_wins_losses_and_ties_in_the_better_direction(self):
        found = experiment.compare([(2.0, 1.0), (1.0, 1.0), (1.0, 2.0), (3.0, 0.0)])
        self.assertEqual((found["wins"], found["losses"], found["ties"]), (2, 1, 1))
        self.assertEqual((found["baseline_mean"], found["treatment_mean"], found["mean_difference"]), (1.75, 1.0, -0.75))
        self.assertEqual(found["change"], round((4.0 - 7.0) / 7.0, 6))
        self.assertEqual(found["p_value"], 1.0)
        zero = experiment.compare([(0.0, 1.0), (0.0, 2.0)])
        self.assertIsNone(zero["change"], "a zero baseline total has no ratio")
        neutral = experiment.compare([(0.0, 1.0)], lower_is_better=None)
        self.assertEqual((neutral["wins"], neutral["p_value"]), (None, None), "use is shown, not judged")
        self.assertEqual(experiment.compare([])["pairs"], 0)


class Report(unittest.TestCase):
    def setUp(self):
        self.found = experiment.report(pilot(), NAME)
        self.rows = {row["item"]: row for row in self.found["rows"]}

    def test_items_pair_across_arms_and_one_armed_items_are_excluded(self):
        self.assertEqual(self.found["items"], {"labelled": 4, "compared": 3, "excluded": 1})
        self.assertEqual(self.rows["I4"]["excluded"], ["missing_arm"])
        self.assertEqual(self.rows["I1"]["threads"], {"off": ["a-off"], "on": ["a-on"]})
        self.assertEqual(self.found["threads"], 7)

    def test_the_paired_numbers_are_read_off_the_events(self):
        m = self.found["metrics"]
        self.assertEqual((m["fix_rounds"]["baseline_mean"], m["fix_rounds"]["treatment_mean"]), (1.0, 0.0),
                         "(2 + 1 + 0) / 3 retries off, none on")
        self.assertEqual((m["fix_rounds"]["wins"], m["fix_rounds"]["losses"], m["fix_rounds"]["ties"]), (2, 0, 1))
        self.assertEqual(m["fix_rounds"]["p_value"], 0.5)
        self.assertEqual((m["turns"]["baseline_mean"], m["turns"]["treatment_mean"]), (2.0, 1.0))
        self.assertEqual(m["cost"]["baseline_mean"], round((0.03 + 0.02 + 0.01) / 3, 6))
        self.assertEqual(m["cost"]["change"], round((0.03 - 0.06) / 0.06, 6))
        self.assertEqual((m["time_to_accept"]["baseline_mean"], m["time_to_accept"]["treatment_mean"]),
                         (round((3.0 + 2.0 + 0.5) / 3, 6), 1.0))
        self.assertEqual((m["web_lookups"]["baseline_mean"], m["web_lookups"]["treatment_mean"]), (2.0, round(1 / 3, 6)))
        self.assertEqual((m["githits_lookups"]["baseline_mean"], m["githits_lookups"]["treatment_mean"]),
                         (0.0, round(4 / 3, 6)))
        self.assertIsNone(m["githits_lookups"]["wins"], "GitHits use has no better direction")
        self.assertEqual(m["accepted"]["baseline_rate"], 1.0)
        self.assertEqual(self.rows["I1"]["values"]["off"]["fix_rounds"], 2.0)

    def test_tags_are_reported_separately(self):
        self.assertEqual(set(self.found["by_tag"]), {"needs_lookup", "routine"},
                         "(untagged) appears only when some items have no tag")
        lookup = self.found["by_tag"]["needs_lookup"]
        self.assertEqual(lookup["items"], 2)
        self.assertEqual(lookup["metrics"]["fix_rounds"]["mean_difference"], -1.5)
        self.assertEqual(self.found["by_tag"]["routine"]["metrics"]["fix_rounds"]["mean_difference"], 0.0)

    def test_checks_name_what_was_kept_and_what_was_excluded(self):
        self.assertEqual(self.found["checks"], {"missing_arm": ["I4"], "unused": ["I3"]},
                         "I3's treatment made no GitHits call: kept, and said")
        self.assertEqual(self.found["per_arm"]["on"]["threads"], 4)
        self.assertEqual(self.found["per_arm"]["off"]["accepted"], 3)

    def test_unknown_is_counted_and_never_paired(self):
        events = pilot()
        for event in events:
            if event["case_id"] == "b-off" and event["kind"] == "candidate_registered" and event["body"]["id"] == "c2":
                event["body"]["signal"].pop("tool_usage")
        m = experiment.report(events, NAME)["metrics"]
        self.assertEqual((m["web_lookups"]["pairs"], m["web_lookups"]["unknown"]), (2, 1),
                         "I2's baseline declared no lookups on one call: unknown, so I2 drops out of this metric only")
        self.assertEqual(m["fix_rounds"]["pairs"], 3)

    def test_repeats_are_averaged_and_known_only_when_every_repeat_is(self):
        events = pilot() + run("a-on-2", "I1", "on", calls=2, githits=1, server="githits:connected", hours=2.0)[0]
        found = experiment.report(events, NAME)
        row = next(r for r in found["rows"] if r["item"] == "I1")
        self.assertIn("repeats", row["flags"])
        self.assertEqual((row["values"]["on"]["fix_rounds"], row["values"]["on"]["time_to_accept"]), (0.5, 1.5))
        silent = run("a-on-3", "I1", "on", calls=1, server="githits:connected", tools=False)[0]
        row = next(r for r in experiment.report(events + silent, NAME)["rows"] if r["item"] == "I1")
        self.assertIsNone(row["values"]["on"]["web_lookups"], "one repeat unknown: the arm's value is unknown")


class Checks(unittest.TestCase):
    def pair(self, off=None, on=None, **report):
        events = run("x-off", "X", "off", **(off or {}))[0] + run("x-on", "X", "on", **{
            "githits": 1, "server": "githits:connected", **(on or {})})[0]
        found = experiment.report(events, NAME, **report)
        return found["rows"][0]

    def test_a_clean_pair_has_no_check(self):
        self.assertEqual(self.pair()["flags"], [])

    def test_different_starts_or_objectives_exclude_the_item(self):
        self.assertEqual(self.pair(on={"base": "b" * 40})["excluded"], ["base_mismatch"])
        self.assertEqual(self.pair(on={"objective": "Something else"})["excluded"], ["objective_mismatch"])

    def test_the_record_must_agree_with_the_label(self):
        row = self.pair(on={"server": "githits:failed"})
        self.assertEqual(row["excluded"], ["arm_conflict"], "a treatment whose GitHits never connected ran as a baseline")
        row = self.pair(off={"server": "githits:connected"})
        self.assertIn("arm_conflict", row["excluded"])
        row = self.pair(off={"githits": 2})
        self.assertEqual(row["excluded"], ["contaminated"])
        self.assertEqual(self.pair(on={"githits": 0})["flags"], ["unused"])

    def test_a_label_written_late_or_changed_is_kept_and_said(self):
        self.assertEqual(self.pair(on={"late": True})["flags"], ["labelled_late"])
        moved = label("Y", "on")
        self.assertEqual(self.pair(on={"extra_label": moved})["flags"][:1], ["missing_arm"],
                         "the latest label wins: the thread moved to item Y")
        same = label("X", "on")
        self.assertEqual(self.pair(on={"extra_label": same})["flags"], [], "the same label twice is not a change")
        renamed = label("X", "on", tags=("routine",))
        self.assertIn("relabelled", self.pair(on={"extra_label": renamed})["flags"])

    def test_models_lanes_and_tags_that_differ_are_said(self):
        self.assertEqual(self.pair(on={"model": "claude-haiku-5-5"})["flags"], ["model_mismatch"])
        self.assertEqual(self.pair(on={"tags": ("routine",)})["flags"], ["tag_mismatch"])

    def test_a_cost_on_another_basis_is_not_compared(self):
        events = run("x-off", "X", "off")[0] + run("x-on", "X", "on", githits=1, server="githits:connected")[0]
        for event in events:
            if event["case_id"] == "x-on" and event["kind"] == "search_finished":
                event["body"]["spend"]["host_observed"] = False
            if event["case_id"] == "x-on" and event["kind"] == "candidate_registered":
                event["body"]["signal"].pop("model_usage")
        found = experiment.report(events, NAME)
        self.assertIn("cost_basis_mismatch", found["rows"][0]["flags"])
        self.assertEqual((found["metrics"]["cost"]["pairs"], found["metrics"]["cost"]["unknown"]), (0, 1))

    def test_without_a_recorded_server_only_the_label_says_which_arm_ran(self):
        row = self.pair(off={"server": ""}, on={"server": ""}, server=None)
        self.assertEqual(row["flags"], [], "server=None: an experiment that does not switch a server")
        events = run("x-off", "X", "off")[0] + run("x-on", "X", "on", githits=1)[0]
        for event in events:
            if event["kind"] == "candidate_registered":
                event["body"]["signal"].pop("harness")
        self.assertEqual(experiment.report(events, NAME)["rows"][0]["flags"], ["arm_unverified"])

    def test_acceptance_reads_the_contracts_promotion_and_a_verified_reconciliation(self):
        events, e = run("x-off", "X", "off", hours=2.0)
        [found] = experiment.runs(events, NAME)
        self.assertEqual((found.accepted.value, found.time_to_accept.value), (True, 2.0))
        self.assertEqual(found.time_to_accept.basis, (e["created"], e["accepted"]))
        other = {("repo-change", 4): {"evolution": {"promotion": {"action": "ship_it"}}}}
        [found] = experiment.runs(events, NAME, other)
        self.assertEqual((found.accepted.value, found.time_to_accept.value), (False, None),
                         "this contract promotes with ship_it, which never ran")
        events, _ = run("y-off", "Y", "off", accepted=False)
        [found] = experiment.runs(events, NAME)
        self.assertEqual((found.accepted.value, found.time_to_accept.value), (False, None))
        j = Journal("y-off")
        j.events = events
        j.add("proposed", {"id": "p1", "action": "accept_change", "actor": "runner@example.test", "args": {}})
        j.add("reconciled", {"proposal_id": "p1", "success": True, "proof": {
            "verified": True, "external_id": "refs/heads/x", "response_digest": "f" * 64}}, at=T0 + 4 * HOUR)
        [found] = experiment.runs(j.events, NAME)
        self.assertEqual((found.accepted.value, found.time_to_accept.value), (True, 4.0))

    def test_the_same_arm_twice_is_refused(self):
        with self.assertRaises(ValueError):
            experiment.report(pilot(), NAME, baseline="on", treatment="on")

    def test_label_shapes_are_bounded(self):
        value = experiment.label_value(NAME, "ITEM-14", "on", "owner@example.test", "t", ["routine", "needs_lookup",
                                                                                          "routine"])
        self.assertEqual(value["tags"], ["needs_lookup", "routine"], "sorted, duplicates dropped")
        for bad in ((NAME, "has space", "on"), (NAME, "I", ""), ("", "I", "on"), (NAME, "I", "x" * 65)):
            with self.subTest(bad), self.assertRaises(ValueError):
                experiment.label_value(*bad, "owner@example.test", "t")
        with self.assertRaises(ValueError):
            experiment.label_value(NAME, "I", "on", "owner@example.test", "t", [f"t{n}" for n in range(9)])
        with self.assertRaises(ValueError):
            experiment.label_value(NAME, "I", "on", "", "t")
        events, _ = run("x-off", "X", "off")
        for event in events:
            if event["body"].get("key") == experiment.KEY:
                event["body"]["value"]["item"] = "not a name"
        self.assertEqual(experiment.runs(events, NAME), [], "a malformed label puts the thread in no experiment")


class Delivery(unittest.TestCase):
    """Delivered as the pilot defines it: accepted, and a blind review of the accepted commit passed."""

    def test_reviewed_reads_the_review_of_the_accepted_commit(self):
        for review, revision, expected in (("pass", None, True), ("fail", None, False), (None, None, None),
                                           ("pass", "0123456789ab", None)):
            with self.subTest(review=review, revision=revision):
                events, e = run("x-off", "X", "off", review=review, review_revision=revision)
                [found] = experiment.runs(events, NAME)
                self.assertEqual(found.reviewed.value, expected)
        events, e = run("x-off", "X", "off", review="pass")
        [found] = experiment.runs(events, NAME)
        self.assertEqual(found.reviewed.basis, (e["accepted"], e["review"]))
        events, _ = run("y-off", "Y", "off", accepted=False)
        self.assertIs(experiment.runs(events, NAME)[0].reviewed.value, False, "not accepted is not delivered")

    def items(self, reviews):
        """Two lookup items the treatment makes cheaper, two routine items it makes a little dearer."""
        events = []
        for item, tag, off_cost, on_cost in (("L1", "needs_lookup", 0.40, 0.10), ("L2", "needs_lookup", 0.50, 0.20),
                                             ("R1", "routine", 0.10, 0.12), ("R2", "routine", 0.10, 0.12),
                                             ("R3", "routine", 0.10, 0.12)):
            for arm, dollars, githits in (("off", off_cost, 0), ("on", on_cost, 1)):
                events += run(f"{item}-{arm}", item, arm, dollars=dollars, githits=githits, tags=(tag,),
                              server="githits:connected" if arm == "on" else "",
                              review=reviews.get((item, arm), "pass"))[0]
        return events

    def test_the_rule_counts_deliveries_and_compares_cost_over_items_delivered_in_both(self):
        found = experiment.report(self.items({}), NAME, delivered_by="review")
        rule = found["rule"]
        self.assertEqual((rule["delivered"], rule["pending"], rule["cost_pairs"]), ({"off": 5.0, "on": 5.0}, 0, 5))
        self.assertEqual(rule["median_cost_difference"], 0.02, "three routine items +0.02, two lookup items -0.30")
        self.assertEqual((rule["treatment_at_least_as_many"], rule["treatment_cheaper"], rule["met"]), (True, False, False))
        lookup, routine = found["by_tag"]["needs_lookup"]["rule"], found["by_tag"]["routine"]["rule"]
        self.assertEqual((lookup["met"], routine["met"]), (True, False),
                         "the median over a mix of items can go against the treatment where it helped")
        self.assertEqual(found["metrics"]["reviewed"]["baseline_rate"], 1.0)

    def test_a_pending_review_leaves_the_rule_undecided_and_a_failed_one_counts(self):
        rule = experiment.report(self.items({("L1", "on"): None}), NAME, delivered_by="review")["rule"]
        self.assertEqual((rule["pending"], rule["met"]), (1, None), "not yet reviewed is neither pass nor fail")
        rule = experiment.report(self.items({("L1", "on"): None}), NAME, delivered_by="accept")["rule"]
        self.assertEqual((rule["pending"], rule["met"]), (0, False), "on acceptance alone nothing is pending")
        rule = experiment.report(self.items({("R1", "on"): "fail"}), NAME, delivered_by="review")["rule"]
        self.assertEqual((rule["delivered"], rule["treatment_at_least_as_many"], rule["met"]),
                         ({"off": 5.0, "on": 4.0}, False, False))
        reviewed = experiment.report(self.items({("R1", "on"): None}), NAME, delivered_by="review")["metrics"]["reviewed"]
        self.assertEqual((reviewed["pairs"], reviewed["unknown"]), (4, 1))

    def test_no_item_delivered_in_both_arms_decides_nothing(self):
        rule = experiment.report(run("x-off", "X", "off", review="fail")[0]
                                 + run("x-on", "X", "on", githits=1, server="githits:connected", review="pass")[0],
                                 NAME, delivered_by="review")["rule"]
        self.assertEqual((rule["cost_pairs"], rule["met"]), (0, None))
        with self.assertRaises(ValueError):
            experiment.report([], NAME, delivered_by="merge")


class Rendering(unittest.TestCase):
    def test_table_markdown_and_csv(self):
        found = experiment.report(pilot(), NAME)
        text = experiment.table(found)
        self.assertIn("experiment githits-pilot: on against off, 3 items compared, 1 excluded, 7 threads", text)
        self.assertIn("check missing_arm (excluded): I4", text)
        self.assertIn("check unused (kept): I3", text)
        self.assertIn("2/0/1", text)
        md = experiment.markdown(found)
        self.assertIn("| Fix rounds | retries per item | 1 | 0 | -100% | 3 (2/0/1) | 0.5 |", md)
        self.assertIn("| GitHits lookups | calls per item | 0 | 1.33 | - | 3 | - |", md,
                      "a zero baseline has no ratio: shown as -, never as unknown")
        rows = list(__import__("csv").reader(io.StringIO(experiment.to_csv(found))))
        self.assertEqual(rows[0][:6], ["item", "tags", "flags", "excluded", "off_threads", "on_threads"])
        i4 = next(r for r in rows if r[0] == "I4")
        self.assertEqual((i4[2], i4[3], i4[rows[0].index("off_cost")]), ("missing_arm", "True", "unknown"))


class Terminal(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.exports = []
        threads = {}
        for event in pilot():
            threads.setdefault(event["case_id"], []).append(event)
        for case, events in threads.items():
            path = Path(folder.name) / f"{case}.json"
            path.write_text(json.dumps({"events": events, "contract": {"id": "repo-change", "version": 4}}),
                            encoding="utf-8")
            self.exports.append(str(path))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["experiment", "report", *args, "--from-export", *self.exports])
        return code, out.getvalue(), err.getvalue()

    def test_report_from_exported_records(self):
        code, text, _ = self.run_cli("--name", NAME)
        self.assertEqual(code, 0, text)
        self.assertIn("3 items compared", text)
        code, text, _ = self.run_cli("--name", NAME, "--json")
        found = json.loads(text)
        self.assertEqual(found["metrics"]["fix_rounds"]["wins"], 2)
        code, text, _ = self.run_cli("--name", NAME, "--markdown")
        self.assertTrue(text.startswith("| Metric | Unit | off (mean) | on (mean) |"))
        code, text, _ = self.run_cli("--name", NAME, "--csv")
        self.assertEqual(len(text.splitlines()), 5)
        code, text, _ = self.run_cli("--name", NAME, "--server", "none")
        self.assertNotIn("unused", text, "no server named: no use check")
        code, text, _ = self.run_cli("--name", NAME, "--delivered", "review")
        self.assertIn("rule (accepted and reviewed = delivered): not decided (3 items' delivery unknown)", text,
                      "accepted but never reviewed: the rule waits")
        code, text, _ = self.run_cli("--name", NAME)
        self.assertIn("rule (accepted = delivered): met", text)

    def test_an_unknown_experiment_and_one_arm_twice(self):
        code, text, _ = self.run_cli("--name", "other")
        self.assertEqual(code, 1)
        self.assertIn("no thread is labelled into experiment 'other'", text)
        code, _, err = self.run_cli("--name", NAME, "--baseline", "on")
        self.assertEqual(code, 2)
        self.assertIn("invalid_experiment", err)


CHANGED = {"src/export.py": EXPORT.replace('["name", "qty"]', '["qty", "name"]'), "tests/test_export.py": TEST_EXPORT}
PRICES = {"fixture-model": {"input_per_million": 3, "output_per_million": 15}}


class EndToEnd(RepoCase):
    """Two work items through both arms of the real repair loop, labelled, accepted, and compared."""

    def cli(self, *args, actor="owner@example.test"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--home", str(self.root / ".syberlabs"), "--repo", str(self.root), "--actor", actor, *args])
        return code, out.getvalue() + err.getvalue()

    def adapter(self):
        """A stand-in for the model: off the treatment it gets a lookup-dependent item wrong once, then repairs it."""
        script = self.root.parent / "stand_in.py"
        script.write_text(textwrap.dedent(f'''\
            import json, os, sys
            request = json.load(sys.stdin)
            arm, lookup = os.environ["FIXTURE_ARM"], "library" in request["objective"]
            wrong = arm == "off" and lookup and "feedback" not in request
            print(json.dumps({{
                "candidates": [{{"changes": {CHANGED!r} if wrong else {GOOD!r}, "message": "x"}}], "recommended": [0],
                "usage": {{"input_tokens": 1000, "output_tokens": 200, "model": "fixture-model"}},
                "tool_usage": {{"githits": 2 if arm == "on" else 0, "web_search": 2 if arm == "off" and lookup else 0}}}}))
            '''), encoding="utf-8")
        return [sys.executable, str(script)]

    def test_both_arms_of_two_items_end_to_end(self):
        doc = self.contract(**{"evolution.budget": {"max_candidates": 6, "max_evaluations": 6, "max_seconds": 120,
                                                    "prices": PRICES}})
        kit = self.kit(contract=doc)
        argv = self.adapter()
        items = {"LIB-1": ("Add a CSV export using the csv library", "needs_lookup"),
                 "ROUTINE-1": ("Add a CSV export with a header row", "routine")}
        for item, (objective, tag) in items.items():
            for arm in ("off", "on"):
                thread = kit.start(objective, "repo-change.v1", paths=["src/", "tests/"], unattended=True,
                                   actor="runner@example.test")
                (kit.home / "current").write_text(thread.id + "\n")
                code, text = self.cli("experiment", "label", "--name", NAME, "--item", item, "--arm", arm, "--tag", tag)
                self.assertEqual(code, 0, text)
                with patch.dict(os.environ, {"FIXTURE_ARM": arm}):
                    found = thread.propose(RepairProvider(argv, name="fixture-model"), seed=1)
                receipt = thread.accept(found[-1].id, actor="runner@example.test")
                self.assertEqual(receipt.status, "succeeded", receipt.reason)
                self.assertEqual(validate_events(thread.history(), "thread"), [])
        self.assertTrue(self.clean(), "four threads, four acceptances: the working tree and HEAD untouched")

        events = [e for case in kit.journal.thread_ids() for e in kit.session.history(case)]
        found = experiment.report(events, NAME, dict(kit.session.contracts))
        self.assertEqual(found["items"], {"labelled": 2, "compared": 2, "excluded": 0})
        self.assertEqual(found["checks"], {"arm_unverified": ["LIB-1", "ROUTINE-1"]},
                         "this stand-in records no MCP servers, so the label alone names the arm; every thread "
                         "started from the same HEAD, and no other check applies")
        lib = next(r for r in found["rows"] if r["item"] == "LIB-1")
        self.assertEqual((lib["values"]["off"]["fix_rounds"], lib["values"]["on"]["fix_rounds"]), (1.0, 0.0))
        self.assertEqual((lib["values"]["off"]["web_lookups"], lib["values"]["on"]["githits_lookups"]), (4.0, 2.0))
        call = round(1000 * 3 / 1_000_000 + 200 * 15 / 1_000_000, 6)
        self.assertEqual((lib["values"]["off"]["cost"], lib["values"]["on"]["cost"]), (round(2 * call, 6), call),
                         "host-observed dollars: two priced calls off, one on")
        m = found["metrics"]
        self.assertEqual((m["fix_rounds"]["wins"], m["fix_rounds"]["ties"]), (1, 1))
        self.assertEqual((m["turns"]["baseline_mean"], m["turns"]["treatment_mean"]), (1.5, 1.0))
        self.assertEqual(m["time_to_accept"]["pairs"], 2, "both arms were accepted, so both have a time")
        self.assertEqual(m["accepted"]["baseline_rate"], 1.0)
        self.assertEqual(m["human_interventions"]["mean_difference"], 0.0, "the runner accepted: no person acted")

        code, text = self.cli("experiment", "report", "--name", NAME)
        self.assertEqual(code, 0, text)
        self.assertIn("2 items compared, 0 excluded, 4 threads", text)

    def test_label_is_a_verified_host_fact_and_a_late_label_is_said(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        (kit.home / "current").write_text(thread.id + "\n")
        code, text = self.cli("experiment", "label", "--name", NAME, "--item", "ITEM-14", "--arm", "on")
        self.assertEqual(code, 0, text)
        self.assertNotIn("labelled_late", text)
        [value] = [metrics.fact(e, experiment.KEY) for e in thread.history() if metrics.fact(e, experiment.KEY)]
        self.assertEqual((value["name"], value["item"], value["arm"], value["tags"], value["by"]),
                         (NAME, "ITEM-14", "on", [], "owner@example.test"))
        self.assertEqual(validate_events(thread.history(), "thread"), [])
        thread.propose(changes=GOOD)
        code, text = self.cli("experiment", "label", "--name", NAME, "--item", "ITEM-14", "--arm", "off")
        self.assertIn("labelled_late", text)
        code, text = self.cli("experiment", "label", "--name", NAME, "--item", "has space", "--arm", "on")
        self.assertEqual(code, 2)
        self.assertIn("invalid_experiment", text)


if __name__ == "__main__":
    unittest.main()
