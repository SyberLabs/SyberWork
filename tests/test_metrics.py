"""The metrics suite: every number a pure function of the journal, with its basis, and unknown never coerced to zero.

The synthetic journal below is two threads written by hand, event by event, so each expected number can be read off
the events that make it. The facts the suite needs (tool_usage, human_intervention, review, critic_usage) are then
written by the real host paths against real Git repositories. No model is called.
"""

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

from spec.validate import check as check_schema
from spec.validate import validate_events
from syberlabs import Rejected, critic, metrics
from syberlabs.cli import main as cli
from syberlabs.evolve import CommandMutator, EvolutionaryProvider
from syberlabs.providers import CommandProvider, RepairProvider, tool_usage
from tests.test_build_thread import GOOD, REPORT, RepoCase
from tests.test_server import ServiceCase

HOST = "syberlabs.host"
T0 = 1_792_000_000_000_000  # integer microseconds, as new events store ``at``
HOUR = 3_600_000_000
U = [{"input_tokens": 1000 + n, "output_tokens": 200, "model": "claude-sonnet-5-5"} for n in range(5)]
CONTRACT = {"id": "repo-change", "version": 4,
            "evolution": {"evaluation": {"required": ["tests", "critic.rules"]}}}
D1 = "d1" * 32


def h(case, seq):
    return hashlib.sha256(f"{case}:{seq}".encode()).hexdigest()


class Journal:
    """Events for one thread, numbered and hashed in order (the suite reads hashes; it does not verify the chain)."""

    def __init__(self, case):
        self.case, self.events = case, []

    def add(self, kind, body, at=None):
        seq = len(self.events) + 1
        self.events.append({"case_id": self.case, "seq": seq, "kind": kind, "body": body,
                            "at": T0 + seq * 1_000_000 if at is None else at, "previous": "0" * 64, "hash": h(self.case, seq)})
        return h(self.case, seq)

    def fact(self, key, value, version="1", source=HOST):
        return self.add("observed", {"key": key, "value": value, "source": source, "version": version, "actor": HOST,
                                     "verified": True})


def check(name, state, digest="0" * 64):
    return {"name": name, "state": state, "exit_code": 0 if state == "passed" else 1, "duration_ms": 5,
            "output_digest": digest, "output_tail": ""}


def delivered_thread():
    """Delivered: three candidates, two fix rounds, a critic high finding, a failed review, one human approval."""
    j = Journal("thread-a")
    e = {}
    e["created"] = j.add("case_created", {"actor": "agent@example.test", "contract": ["repo-change", 4],
                                          "inputs": {"objective": "Add a CSV export", "act_class": "R"}}, at=T0)
    j.add("observed", {"key": "base", "value": {"commit": "a" * 40}, "source": "git", "version": "a" * 40,
                       "actor": HOST, "verified": True})
    j.fact("unattended", {"unattended": True, "by": "runner@example.test"})
    j.add("search_started", {"id": "s1", "actor": HOST})
    e["c1"] = j.add("candidate_registered", {"id": "c1", "operator": "patch", "actor": HOST, "signal": {
        "model_usage": U[0], "tool_usage": {"web_search": 2, "web_fetch": 1, "githits": 3}},
        "spend": {"dollars": 0.1, "seconds": 1.0, "host_observed": True}})
    e["eval1"] = j.add("candidate_evaluated", {"candidate": "c1", "actor": HOST, "checks": [
        check("tests", "failed"), check("critic.rules", "passed", D1)]})
    e["critic"] = j.fact("critic", {"candidate": "c1", "check": "critic.rules", "counts": {"high": 1, "medium": 0, "low": 0},
                                    "reviewer": {"name": "rules", "revision": "1"}}, version=D1, source="check:critic.rules")
    e["critic_usage"] = j.fact("critic_usage", {"candidate": "c1", "check": "critic.rules", "usage": U[1]},
                               version=D1, source="check:critic.rules")
    e["refused"] = j.fact("proposal_refused", {"search": "s1", "operator": "patch", "parents": ["c1"], "reason": "no_change",
                                               "diff": None, "model_usage": U[2],
                                               "spend": {"dollars": 0.05, "seconds": 2.0, "host_observed": True}}, version="s1")
    e["c2"] = j.add("candidate_registered", {"id": "c2", "operator": "patch", "actor": HOST, "signal": {
        "model_usage": U[3], "tool_usage": {"web_search": 0, "githits": 1}},
        "spend": {"dollars": 0.12, "seconds": 3.0, "host_observed": True}})
    e["eval2"] = j.add("candidate_evaluated", {"candidate": "c2", "actor": HOST, "checks": [
        check("tests", "failed"), check("critic.rules", "passed", "e2" * 32)]})
    e["c3"] = j.add("candidate_registered", {"id": "c3", "operator": "patch", "actor": HOST, "signal": {
        "model_usage": U[4], "tool_usage": {"web_fetch": 1, "githits": 0}},
        "spend": {"dollars": 0.08, "seconds": 4.0, "host_observed": True}})
    j.add("candidate_evaluated", {"candidate": "c3", "actor": HOST, "checks": [
        check("tests", "passed"), check("critic.rules", "passed", "e3" * 32)]})
    e["finished"] = j.add("search_finished", {"id": "s1", "actor": HOST, "candidates": ["c1", "c2", "c3"], "spend": {
        "dollars": 0.35, "seconds": 9.0, "evaluations": 3, "candidates": 3, "host_observed": True, "unknown_models": []}})
    j.add("proposed", {"id": "p1", "action": "accept_change", "actor": "runner@example.test", "args": {"candidate": "c3"}})
    j.add("decision", {"proposal_id": "p1", "status": "needs_approval", "reason": "approval_required"})
    e["approved"] = j.add("approved", {"proposal_id": "p1", "actor": "owner@example.test", "role": "maintainer"})
    j.add("effect_succeeded", {"proposal_id": "p1", "action": "accept_change", "output": {"ref": "refs/heads/x", "new": "c" * 40}})
    j.add("proposed", {"id": "p2", "action": "open_pull_request", "actor": "runner@example.test", "args": {}})
    e["pr"] = j.add("effect_succeeded", {"proposal_id": "p2", "action": "open_pull_request", "output": {
        "destination": "github:owner/repo", "external_id": "90", "url": "https://github.com/owner/repo/pull/90"}})
    e["review_fail"] = j.fact("review", {"verdict": "fail", "reviewer": "verifier", "revision": "c" * 40}, version="c" * 40)
    j.fact("review", {"verdict": "pass", "reviewer": "verifier", "revision": "d" * 40}, version="d" * 40)
    e["merged"] = j.fact("pr_merged", {"number": 90, "merged_at": "2026-10-19T08:00:00Z", "merge_commit": "e" * 40,
                                      "merged_by": "owner"}, version="90")
    return j, e


def cancelled_thread():
    """Cancelled: one working-tree candidate, no model call through the host, cancelled by a person."""
    j = Journal("thread-b")
    e = {"created": j.add("case_created", {"actor": "agent@example.test", "contract": ["repo-change", 4],
                                           "inputs": {"objective": "Try something", "act_class": "C"}}, at=T0 + 10 * HOUR)}
    j.add("search_started", {"id": "s1", "actor": HOST})
    e["c1"] = j.add("candidate_registered", {"id": "c1", "operator": "patch", "actor": HOST, "signal": None,
                                             "spend": {"dollars": 0, "seconds": 1.0, "host_observed": False}})
    e["finished"] = j.add("search_finished", {"id": "s1", "actor": HOST, "spend": {
        "dollars": 0, "seconds": 1.0, "evaluations": 0, "candidates": 1, "host_observed": False, "unknown_models": []}})
    e["cancel"] = j.add("case_cancelled", {"actor": "owner@example.test", "reason": "superseded"})
    return j, e


class Derivations(unittest.TestCase):
    def setUp(self):
        self.a, self.ea = delivered_thread()
        self.b, self.eb = cancelled_thread()
        contracts = {("repo-change", 4): CONTRACT}
        self.items = metrics.work_items(self.a.events + self.b.events, contracts)
        self.item_a, self.item_b = self.items

    def test_the_delivered_thread_yields_nine_known_metrics_each_with_its_events(self):
        a, e = self.item_a, self.ea
        self.assertEqual((a.thread, a.contract, a.act_class, a.started_at, a.unattended, a.cancelled),
                         ("thread-a", "repo-change.v4", "R", T0, True, False))
        self.assertEqual((a.cost_basis, a.cost.value, a.cost.basis), ("host_observed", 0.35, (e["finished"],)),
                         "dollars are the search's own spend record; registrations and refusals are not added again")
        self.assertEqual(a.turns.value, 5, "four proposer calls with declared usage and one critic call")
        self.assertEqual(a.turns.basis, (e["c1"], e["critic_usage"], e["refused"], e["c2"], e["c3"]))
        merged = metrics.parse_time("2026-10-19T08:00:00Z")
        self.assertEqual(a.agent_time.value, round((merged - T0 / 1_000_000) / 3600, 3))
        self.assertEqual(a.agent_time.basis, (e["created"], e["merged"]))
        self.assertEqual(a.fix_rounds.value, 2)
        self.assertEqual(a.fix_rounds.basis, (e["eval1"], e["c2"], e["eval2"], e["c3"]))
        self.assertEqual((a.web_lookups.value, a.githits_lookups.value), (4, 4))
        self.assertEqual(a.web_lookups.basis, (e["c1"], e["c2"], e["c3"]))
        self.assertEqual((a.opened.value, a.opened.basis, a.pull_request), (True, (e["pr"],), "90"))
        self.assertEqual((a.delivered.value, a.delivered.basis), (True, (e["merged"],)))
        self.assertEqual(a.review_rejections.value, 2, "the critic's high finding on c1 and the failed review")
        self.assertEqual(a.review_rejections.basis, (e["eval1"], e["review_fail"]))
        self.assertEqual((a.human_interventions.value, a.human_interventions.basis), (1, (e["approved"],)),
                         "the owner's approval; the runner's own proposals are the agent's")
        self.assertEqual(a.unknown(), [])

    def test_lookups_blocked_counts_guard_rules_and_is_unknown_without_a_decision(self):
        j = Journal("thread-g")
        j.add("case_created", {"actor": "agent@example.test", "contract": ["repo-change", 4], "inputs": {}}, at=T0)
        blocked = j.fact("lookup", {"tool": "mcp__githits__search", "ok": False, "guard": "tree_line"}, version="s1.1.1")
        j.fact("lookup", {"tool": "mcp__githits__search", "ok": True, "guard": "pass"}, version="s1.1.2")
        j.fact("lookup", {"tool": "WebSearch", "ok": False, "guard": "unguarded"}, version="s1.1.3")
        j.add("candidate_registered", {"id": "c1", "operator": "patch", "actor": HOST, "signal": {
            "model_usage": U[0], "tool_usage": {"web_search": 1, "web_fetch": 0, "githits": 2}}})
        [item] = metrics.work_items(j.events)
        self.assertEqual((item.githits_lookups.value, item.web_lookups.value), (2, 1), "attempted, blocked included")
        self.assertEqual((item.lookups_blocked.value, item.lookups_blocked.basis), (1, (blocked,)), "a rule, not pass or unguarded")
        self.assertEqual(item.to_dict()["lookups_blocked"]["value"], 1)
        self.assertEqual(metrics.provenance(item, "lookups_blocked")[0]["kind"], "observed")
        before = j.fact("lookup", {"tool": "WebSearch", "ok": True}, version="s1.1.4")  # recorded before the guard existed
        [item] = metrics.work_items(j.events)
        self.assertEqual((item.lookups_blocked.value, item.lookups_blocked.basis), (None, (before,)))
        self.assertEqual(self.item_a.lookups_blocked.value, None, "lookups counted, no lookup facts: unknown, not 0")
        self.assertEqual(self.item_a.to_dict()["lookups_blocked"]["value"], "unknown")

    def test_a_githits_doctor_fact_is_never_counted_as_a_lookup(self):
        """githits-pilot-hardening, item 1: a readiness check (``githits.doctor``) is the operator's, not a lookup,
        whether it sits beside client lookups or alone (no fallback to the declared count either)."""
        j = Journal("thread-d")
        j.add("case_created", {"actor": "agent@example.test", "contract": ["repo-change", 4], "inputs": {}}, at=T0)
        search = j.fact("lookup", {"tool": "githits.search", "ok": True, "guard": "pass"}, version="s1.1.1")
        j.fact("lookup", {"tool": "githits.doctor", "ok": True, "guard": "pass"}, version="s1.1.2")
        example = j.fact("lookup", {"tool": "githits.example", "ok": False, "guard": "pass"}, version="s1.1.3")
        j.add("candidate_registered", {"id": "c1", "operator": "patch", "actor": HOST, "signal": {
            "model_usage": U[0], "tool_usage": {"web_search": 0, "web_fetch": 0, "githits": 3}}})
        [item] = metrics.work_items(j.events)
        self.assertEqual((item.githits_lookups.value, item.githits_lookups.basis), (2, (search, example)))
        self.assertIn("doctor excluded", item.githits_lookups.note)
        alone = Journal("thread-e")
        alone.add("case_created", {"actor": "agent@example.test", "contract": ["repo-change", 4], "inputs": {}}, at=T0)
        alone.fact("lookup", {"tool": "githits.doctor", "ok": True, "guard": "pass"}, version="s1.1.1")
        alone.add("candidate_registered", {"id": "c1", "operator": "patch", "actor": HOST, "signal": {
            "model_usage": U[0], "tool_usage": {"web_search": 0, "web_fetch": 0, "githits": 1}}})
        [item] = metrics.work_items(alone.events)
        self.assertEqual((item.githits_lookups.value, item.githits_lookups.basis), (0, ()),
                         "a doctor record alone is still host-observed, and still not a lookup")

    def test_the_cancelled_thread_says_unknown_where_nothing_was_recorded(self):
        b, e = self.item_b, self.eb
        self.assertEqual((b.act_class, b.unattended, b.cancelled, b.pull_request), ("C", False, True, None))
        self.assertEqual((b.cost_basis, b.cost.value), ("none", None), "no host-metered call: unknown, not $0")
        self.assertEqual((b.turns.value, b.turns.basis), (None, (e["c1"],)), "a working-tree candidate's calls were elsewhere")
        self.assertEqual((b.agent_time.value, b.web_lookups.value, b.githits_lookups.value), (None, None, None))
        self.assertEqual((b.fix_rounds.value, b.review_rejections.value), (0, 0))
        self.assertEqual((b.opened.value, b.delivered.value, b.delivered.basis), (False, False, (e["cancel"],)))
        self.assertEqual((b.human_interventions.value, b.human_interventions.basis), (1, (e["cancel"],)))
        self.assertEqual(b.unknown(), ["cost", "turns", "agent_time", "web_lookups", "githits_lookups"])
        row = b.to_dict()
        self.assertEqual(row["cost"]["value"], "unknown")
        self.assertEqual(row["fix_rounds"]["value"], 0)
        self.assertEqual(row["started_at"], metrics.utc(T0 + 10 * HOUR))

    def test_an_open_pull_request_without_a_merge_fact_is_unknown_delivery(self):
        a, _ = delivered_thread()
        a.events = [x for x in a.events if x["body"].get("key") != "pr_merged"]
        [item] = metrics.work_items(a.events)
        self.assertEqual((item.opened.value, item.delivered.value, item.agent_time.value), (True, None, None))
        self.assertEqual(metrics.incomplete([item]), [], "--check judges delivered items only")

    def test_provenance_lists_the_events_behind_one_number(self):
        e = self.ea
        rows = metrics.provenance(self.item_a, "fix_rounds")
        self.assertEqual([r["hash"] for r in rows], [e["eval1"], e["c2"], e["eval2"], e["c3"]])
        self.assertEqual([r["kind"] for r in rows],
                         ["candidate_evaluated", "candidate_registered", "candidate_evaluated", "candidate_registered"])
        self.assertEqual([r["seq"] for r in rows], sorted(r["seq"] for r in rows))
        self.assertEqual([r["hash"] for r in metrics.provenance(self.item_a, "delivered")], [e["merged"]])
        self.assertEqual([r["hash"] for r in metrics.provenance(self.item_b, "web_lookups")], [self.eb["c1"]],
                         "an unknown cites the registration that declared nothing")
        self.assertEqual(metrics.provenance(self.item_b, "review_rejections"), [], "zero, and nothing to cite")
        with self.assertRaises(KeyError):
            metrics.provenance(self.item_a, "velocity")

    def test_summary_counts_unknowns_beside_means_and_medians(self):
        found = metrics.summary(self.items)
        self.assertEqual((found["items"], found["opened"], found["cancelled"]), (2, 1, 1))
        self.assertEqual(found["delivered"], {"yes": 1, "no": 1, "unknown": 0})
        self.assertEqual((found["delivered_rate"], found["delivered_rate_bounds"], found["opened_rate"]), (0.5, [0.5, 0.5], 0.5))
        self.assertEqual(found["metrics"]["fix_rounds"], {"known": 2, "unknown": 0, "total": 2.0, "mean": 1.0, "median": 1.0})
        self.assertEqual(found["metrics"]["cost"], {"known": 1, "unknown": 1, "total": 0.35, "mean": 0.35, "median": 0.35})
        self.assertEqual(found["cost_basis"], {"host_observed": 1, "declared": 0, "none": 1})
        self.assertEqual(metrics.summary(self.items, "2026-10-19")["items"], 0)
        self.assertEqual(metrics.summary(self.items, metrics.utc(T0 + 5 * HOUR))["items"], 1)
        empty = metrics.summary([])
        self.assertEqual((empty["delivered_rate"], empty["metrics"]["turns"]["mean"]), (None, None))
        with self.assertRaises(ValueError):
            metrics.since(self.items, "last tuesday")

    def test_required_checks_decide_a_fix_round_when_the_contract_is_known(self):
        loose = {("repo-change", 4): {"evolution": {"evaluation": {"required": ["critic.rules"]}}}}
        [item] = metrics.work_items(self.a.events, loose)
        self.assertEqual(item.fix_rounds.value, 0, "a failed check the contract does not require is not a failed attempt")
        [item] = metrics.work_items(self.a.events)
        self.assertEqual(item.fix_rounds.value, 2, "without the contract every recorded check counts")

    def test_declared_usage_without_a_host_meter_is_labelled_and_unknown(self):
        j = Journal("thread-c")
        j.add("case_created", {"actor": "a", "contract": ["repo-change", 1], "inputs": {"objective": "x"}})
        j.add("candidate_registered", {"id": "c1", "operator": "patch", "signal": {"model_usage": U[0]}})
        [item] = metrics.work_items(j.events)
        self.assertEqual((item.cost_basis, item.cost.value, item.turns.value), ("declared", None, 1))
        j.add("search_finished", {"id": "s1", "spend": {"dollars": None, "host_observed": True, "unknown_models": ["m"]}})
        [item] = metrics.work_items(j.events)
        self.assertEqual((item.cost_basis, item.cost.value), ("host_observed", None), "an unpriced model is not $0")

    def test_adjacent_records_of_one_call_are_one_turn(self):
        j = Journal("thread-d")
        j.add("case_created", {"actor": "a", "contract": ["repo-change", 4], "inputs": {"objective": "x"}})
        j.fact("proposal_refused", {"reason": "limit", "model_usage": U[0]})
        j.add("candidate_registered", {"id": "c1", "operator": "patch", "signal": {"model_usage": U[0]}})
        j.add("candidate_evaluated", {"candidate": "c1", "checks": [check("tests", "failed")]})
        j.add("candidate_registered", {"id": "c2", "operator": "patch", "signal": {"model_usage": U[0]}})
        [item] = metrics.work_items(j.events)
        self.assertEqual(item.turns.value, 2, "the limit refusal is the same call as c1; c2 is a new call")

    def test_tables_csv_and_the_check_render_unknown_as_unknown(self):
        text = metrics.table(self.items)
        self.assertIn("unknown (none)", text)
        self.assertIn("$0.3500 (host_observed)", text)
        self.assertIn("#90", text)
        rows = list(__import__("csv").reader(io.StringIO(metrics.to_csv(self.items))))
        self.assertEqual(rows[0][-9:], list(metrics.METRICS))
        self.assertEqual(rows[2][rows[0].index("cost")], "unknown")
        self.assertEqual(rows[1][rows[0].index("fix_rounds")], "2")
        self.assertEqual(metrics.incomplete(self.items), [])
        a, _ = delivered_thread()
        for event in a.events:
            if event["kind"] == "candidate_registered" and event["body"]["id"] == "c3":
                event["body"]["signal"].pop("tool_usage")
        self.assertEqual(metrics.incomplete(metrics.work_items(a.events)), [("thread-a", ["web_lookups", "githits_lookups"])])

    def test_fact_shapes_are_bounded(self):
        value = metrics.intervention_value("message", "owner@example.test", "2026-10-23T00:00:00Z", "asked to split the PR")
        self.assertEqual(value, {"kind": "message", "by": "owner@example.test", "at": "2026-10-23T00:00:00Z",
                                 "note": "asked to split the PR"})
        for bad in (("chat", "a", "t"), ("message", "", "t"), ("message", "a", "t", "x" * 501),
                    ("message", "a", "t", "token ghp_" + "A" * 30), ("message", "a", "t", "line\nbreak")):
            with self.subTest(bad), self.assertRaises(ValueError):
                metrics.intervention_value(*bad)
        review = metrics.review_value("fail", "verifier", "abc123", "owner", "t", 2, "https://github.com/o/r/pull/1#c")
        self.assertEqual((review["verdict"], review["findings_high"], review["comment_url"]),
                         ("fail", 2, "https://github.com/o/r/pull/1#c"))
        for bad in (("maybe", "v", "r"), ("pass", "bad name", "r"), ("pass", "v", "r r"), ("pass", "v", "r", -1),
                    ("pass", "v", "r", None, "http://plain")):
            with self.subTest(bad), self.assertRaises(ValueError):
                metrics.review_value(bad[0], bad[1], bad[2], "owner", "t", *bad[3:])


class Terminal(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        a, _ = delivered_thread()
        b, _ = cancelled_thread()
        self.exports = []
        for j in (a, b):
            path = self.folder / f"{j.case}.json"
            path.write_text(json.dumps({"events": j.events, "contract": CONTRACT, "status": {}, "side": [],
                                        "chain_valid": True}), encoding="utf-8")
            self.exports.append(str(path))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["metrics", "--from-export", *self.exports, *args])
        return code, out.getvalue(), err.getvalue()

    def test_table_json_csv_from_exported_records(self):
        code, text, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("thread-a", text)
        self.assertIn("unknown (none)", text)
        self.assertIn("2 items, 1 opened, delivered 1 (unknown 0), rate 0.50", text)
        code, text, _ = self.run_cli("--json")
        found = json.loads(text)
        self.assertEqual([i["thread"] for i in found["items"]], ["thread-a", "thread-b"])
        self.assertEqual(found["items"][0]["fix_rounds"]["value"], 2)
        self.assertEqual(found["items"][1]["turns"]["value"], "unknown")
        self.assertEqual(found["summary"]["delivered_rate"], 0.5)
        code, text, _ = self.run_cli("--csv", "--thread", "thread-b")
        self.assertEqual(len(text.splitlines()), 2)
        code, text, _ = self.run_cli("--json", "--since", "2026-10-19")
        self.assertEqual(json.loads(text)["items"], [])
        code, _, err = self.run_cli("--since", "someday")
        self.assertEqual(code, 2)
        self.assertIn("invalid_since", err)

    def test_check_fails_on_a_delivered_item_with_an_unknown_fact(self):
        self.assertEqual(self.run_cli("--check")[0], 0)
        a, _ = delivered_thread()
        for event in a.events:
            if event["kind"] == "candidate_registered" and event["body"]["id"] == "c2":
                event["body"]["signal"].pop("model_usage")
        Path(self.exports[0]).write_text(json.dumps({"events": a.events, "contract": CONTRACT}), encoding="utf-8")
        code, _, err = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("incomplete: thread-a delivered with unknown turns", err)

    def test_an_export_without_an_event_list_is_refused(self):
        bad = self.folder / "bad.json"
        bad.write_text(json.dumps({"events": [{"kind": "x"}]}), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["metrics", "--from-export", str(bad)]), 2)
        self.assertIn("export_unreadable", err.getvalue())


class Facts(RepoCase):
    """The facts, written by the host's own paths on a real thread, and read back by the suite."""

    def cli(self, *args, actor="dev@example.test"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli(["--home", str(self.root / ".syberlabs"), "--repo", str(self.root), "--actor", actor, *args])
        return code, out.getvalue()

    def facts(self, thread, key):
        return [metrics.fact(e, key) for e in thread.history() if metrics.fact(e, key) is not None]

    def item(self, kit, thread):
        [found] = [i for i in metrics.work_items(kit.session.history(thread.id)) if i.thread == thread.id]
        return found

    def test_accept_by_a_person_records_an_intervention_and_the_host_or_runner_does_not(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        (kit.home / "current").write_text(thread.id + "\n")
        thread.propose(changes=GOOD)
        self.assertEqual(self.cli("check", "c1")[0], 0)
        self.assertEqual(self.facts(thread, metrics.HUMAN_INTERVENTION), [], "a first check is not an intervention")
        self.assertEqual(self.cli("accept", "c1")[0], 0)
        [fact] = self.facts(thread, metrics.HUMAN_INTERVENTION)
        proposal = next(e["body"]["id"] for e in thread.history() if e["kind"] == "proposed")
        self.assertEqual((fact["kind"], fact["by"], fact["proposal"], fact["note"]), ("approve", "dev@example.test", proposal, ""))
        item = self.item(kit, thread)
        self.assertEqual(item.human_interventions.value, 1, "the fact and the proposal it names are one act")
        self.assertEqual(validate_events(thread.history(), "thread"), [])

        unattended = kit.start("Add a CSV export again", paths=["src/", "tests/"], unattended=True,
                               actor="runner@example.test")
        self.assertEqual(unattended.runner, "runner@example.test")
        (kit.home / "current").write_text(unattended.id + "\n")
        self.assertEqual(self.cli("check", "c1", actor="runner@example.test")[0], 2, "no candidate yet")
        unattended.propose(changes=GOOD)
        self.cli("check", "c1", "--rerun", actor=HOST)
        self.cli("check", "c1", "--rerun", actor="runner@example.test")
        self.assertEqual(self.facts(unattended, metrics.HUMAN_INTERVENTION), [], "the host and the runner are not people")
        self.cli("check", "c1", "--rerun", actor="owner@example.test")
        [fact] = self.facts(unattended, metrics.HUMAN_INTERVENTION)
        self.assertEqual((fact["kind"], fact["by"]), ("override", "owner@example.test"))

    def test_note_and_review_are_recorded_and_read_back(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        (kit.home / "current").write_text(thread.id + "\n")
        code, text = self.cli("note", "--intervention", "message", "--note", "asked the agent to split the change")
        self.assertEqual(code, 0, text)
        [fact] = self.facts(thread, metrics.HUMAN_INTERVENTION)
        self.assertEqual((fact["kind"], fact["note"]), ("message", "asked the agent to split the change"))
        self.assertEqual(self.cli("note", "--intervention", "message", "--note", "key sk-ant-" + "x" * 20)[0], 2,
                         "a note that looks like a credential is refused, not stored")
        self.assertEqual(self.cli("review")[1].splitlines()[0][:19], "no review recorded;")
        code, text = self.cli("review", "--record", "--verdict", "fail", "--reviewer", "verifier", "--revision", "abc1234",
                              "--findings-high", "1", "--url", "https://github.com/o/r/pull/9#issuecomment-1")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.cli("review", "--record", "--verdict", "pass")[0], 2)
        [review] = self.facts(thread, metrics.REVIEW)
        self.assertEqual((review["verdict"], review["reviewer"], review["revision"], review["findings_high"], review["by"]),
                         ("fail", "verifier", "abc1234", 1, "dev@example.test"))
        self.assertIn("fail verifier abc1234", self.cli("review")[1])
        item = self.item(kit, thread)
        self.assertEqual((item.review_rejections.value, item.human_interventions.value), (1, 1))
        self.assertEqual(validate_events(thread.history(), "thread"), [])
        code, text = self.cli("metrics", "--json")
        self.assertEqual(code, 0)
        [row] = json.loads(text)["items"]
        self.assertEqual(row["review_rejections"]["value"], 1)


class ToolUsage(RepoCase):
    def adapter(self, name, body):
        script = self.root.parent / f"{name}.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            request = json.load(sys.stdin)
            print(json.dumps({body!r}))
            '''), encoding="utf-8")
        return [sys.executable, str(script)]

    def test_bounds(self):
        many = {f"tool{n:02d}": n for n in range(20)}
        found = tool_usage({"tool_usage": {"web_search": 3, "web_fetch": -1, "githits": True, "extra": 4,
                                           "other": {**many, "x" * 65: 1, "bad": "2"}}})
        self.assertEqual(set(found), {"web_search", "other"})
        self.assertEqual(found["web_search"], 3)
        self.assertEqual(list(found["other"]), [f"tool{n:02d}" for n in range(16)], "at most 16 names, sorted")
        self.assertIsNone(tool_usage({"tool_usage": {"web_search": "3"}}))
        self.assertIsNone(tool_usage({"usage": {}}))
        self.assertIsNone(tool_usage(None))

    def test_command_and_repair_providers_copy_it_into_the_signal_and_absent_stays_absent(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        body = {"candidates": [{"changes": GOOD, "signal": {"tool_usage": {"web_search": 99}}}], "recommended": [0],
                "usage": {"input_tokens": 10, "output_tokens": 5, "model": "m"},
                "tool_usage": {"web_search": 2, "githits": 1, "other": {"grep": 4}}}
        [c1] = thread.propose(CommandProvider(self.adapter("with", body), name="fixture-model"))
        self.assertEqual(c1.signal["tool_usage"], {"web_search": 2, "githits": 1, "other": {"grep": 4}})
        self.assertEqual(c1.signal["model_usage"]["input_tokens"], 10)
        plain = {"candidates": [{"changes": {**GOOD, "README.md": "# r\n"}, "signal": {"tool_usage": {"web_search": 99}}}]}
        [c2] = thread.propose(CommandProvider(self.adapter("without", plain), name="fixture-model"))
        self.assertNotIn("tool_usage", c2.signal or {}, "an item's own tool_usage is not the call's declaration")
        repair = {"candidates": [{"changes": {**GOOD, "README.md": "# repaired\n"}}],
                  "usage": {"input_tokens": 11, "output_tokens": 5, "model": "m"}, "tool_usage": {"web_fetch": 1}}
        [c3] = thread.propose(RepairProvider(self.adapter("repair", repair), name="fixture-model", max_calls=1))
        self.assertEqual(c3.signal["tool_usage"], {"web_fetch": 1})
        item = [i for i in metrics.work_items(thread.history())][0]
        self.assertIsNone(item.web_lookups.value, "c2 declared nothing, so the thread's lookups are unknown")

    def test_the_mutator_copies_it_into_the_childs_signal(self):
        mutator = CommandMutator(self.adapter("mutate", {"changes": {"src/report.py": REPORT + "# x\n"},
                                                         "tool_usage": {"githits": 2}}), name="fixture-model")
        space = types.SimpleNamespace(context=(), scope=("src",), objective="x", read=lambda ref, path: None,
                                      base_evaluation=None)
        import random
        self.assertIn("src/report.py", mutator(space, "base", random.Random(1)))
        self.assertEqual(mutator.last_tools, {"githits": 2})
        provider = EvolutionaryProvider(mutator, population=2, generations=1)
        self.assertEqual(provider._signal("mutation", 1)["tool_usage"], {"githits": 2})
        silent = CommandMutator(self.adapter("silent", {"changes": {}}), name="fixture-model")
        silent(space, "base", random.Random(1))
        self.assertIsNone(silent.last_tools)
        self.assertNotIn("tool_usage", EvolutionaryProvider(silent, population=2, generations=1)._signal("mutation", 1))


class CriticUsage(unittest.TestCase):
    DOC = {"version": 1, "reviewer": {"name": "model", "revision": "1"}, "findings": [], "summary": "ok",
           "usage": {"input_tokens": 300, "output_tokens": 40, "model": "claude-haiku-5-5", "junk": "x"}}

    def test_the_document_may_declare_usage_and_the_host_records_it_beside_the_critic(self):
        check_schema({k: v for k, v in self.DOC.items() if k != "usage"} | {"usage": {"input_tokens": 1}}, "finding.schema.json")
        raw = json.dumps(self.DOC).encode()
        parsed = critic.parse(raw)
        self.assertEqual(parsed["usage"], {"input_tokens": 300, "output_tokens": 40, "model": "claude-haiku-5-5"})
        with self.assertRaises(critic.Invalid) as caught:
            critic.parse(json.dumps({**self.DOC, "usage": 5}).encode())
        self.assertEqual(caught.exception.code, "findings_invalid:usage")
        self.assertNotIn("usage", critic.parse(json.dumps({**self.DOC, "usage": {}}).encode()))
        written = []
        session = types.SimpleNamespace(observe=lambda *args, **kw: written.append((args, kw)) or {"hash": str(len(written))})
        body = {"candidate": "c1", "checks": [{"name": "critic.model", "state": "passed",
                                               "output_digest": critic.digest(raw)}]}
        critic.observe(session, "t1", body, [("critic.model", raw, parsed)])
        self.assertEqual([args[1] for args, _ in written], ["critic", "critic_usage"])
        args, kw = written[1]
        self.assertEqual(args[2], {"candidate": "c1", "check": "critic.model", "usage": parsed["usage"]})
        self.assertEqual((args[3], args[4], args[5], kw), ("check:critic.model", critic.digest(raw), HOST, {"verified": True}))
        written.clear()
        plain = critic.parse(json.dumps({k: v for k, v in self.DOC.items() if k != "usage"}).encode())
        critic.observe(session, "t1", {**body, "checks": [{**body["checks"][0], "output_digest": critic.digest(
            json.dumps({k: v for k, v in self.DOC.items() if k != "usage"}).encode())}]},
            [("critic.model", json.dumps({k: v for k, v in self.DOC.items() if k != "usage"}).encode(), plain)])
        self.assertEqual([args[1] for args, _ in written], ["critic"], "no usage, no second observation")


class Service(ServiceCase):
    def test_work_items_are_served_beside_the_cells_metrics(self):
        self.service.install()
        work = self.service.work
        first = work.create_case("note", 1, {"name": "a"}, "operator")
        work.observe(first, "human_intervention", {"kind": "message", "by": "owner", "at": "t", "note": ""}, HOST, "1",
                     HOST, verified=True)
        status, _, body = self.service.call("/metrics/work_items")
        self.assertEqual(status, 200, body)
        [row] = body["items"]
        self.assertEqual((row["thread"], row["contract"], row["human_interventions"]["value"]), (first, "note.v1", 1))
        self.assertEqual(row["cost"]["value"], "unknown")
        self.assertEqual(body["summary"]["items"], 1)
        self.assertEqual(body, work.work_items(), "the route serves the same fold the terminal prints")
        status, _, one = self.service.call("/metrics/work_items/" + first)
        self.assertEqual((status, [r["thread"] for r in one["items"]]), (200, [first]))
        status, _, missing = self.service.call("/metrics/work_items/no-such-thread")
        self.assertEqual((status, missing["error"]), (404, "unknown_case"))
        self.assertEqual(self.service.call("/metrics/work_items", token=None)[0], 401)
        self.assertEqual(self.service.call("/metrics/cases_total")[2]["value"], 1, "the cell's own metrics are unchanged")
        self.assertEqual(self.service.call("/metrics/work_items/a/b")[0], 404)


if __name__ == "__main__":
    unittest.main()
