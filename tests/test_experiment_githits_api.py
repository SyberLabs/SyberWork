"""The A/B report on the GitHits pilot's API route: arm B is ``ClaudeCodeProvider(githits=True)`` (#129, #134), and
the host's record that a call had GitHits is the launcher ledger on its candidate, ``signal.githits``, not an MCP
server.

``tests/test_experiment.py`` covers the report on the MCP route; it is unchanged. Here the same checks are read off the
API route's record: first synthetic journals, ``tests.test_experiment.run`` threads given the signal and facts
``claude_code`` writes on that route, so each check can be read off the events that make it; then both arms of the pilot
end to end through the real ``ClaudeCodeProvider`` (the stand-in ``claude`` of ``tests/test_claude_code.py``, whose Bash
tool runs the real launcher behind the real query guard, and the host's own GitHits client against a loopback fake),
labelled with ``syberlabs experiment label`` and compared with ``syberlabs experiment report``; and last the rehearsal,
``examples/githits_ab.py``, on that route. No model is called and nothing reaches the real GitHits API.
"""

import contextlib
import copy
import importlib.util
import io
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spec.validate import validate_events
from syberlabs import experiment, githits
from syberlabs.claude_code import THIRD_PARTY_SWITCHES, ClaudeCodeProvider
from syberlabs.cli import main as cli
from tests.test_build_thread import GOOD, REPORT
from tests.test_claude_code import COST, PRICES, ClaudeCodeCase
from tests.test_experiment import NAME, run
from tests.test_githits import FakeGitHits
from tests.test_metrics import HOST, h

RUNNER, REVIEWER = "runner@example.test", "reviewer@example.test"
BROKEN = {**GOOD, "src/report.py": "def rows():\n    return []\n"}  # the host's tests fail: the report rows are gone
SKILL = {"path": "skills/githits/SKILL.md", "sha256": "2" * 64}
GITHITS_SETTINGS = (githits.EXCLUDE_ENV, githits.EXCLUDE_REPLACE_ENV, githits.LICENSES_ENV, githits.SESSION_ENV,
                    githits.AGENT_ENV, githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV)


def ledger(records=0, arm="b"):
    """``signal.githits`` as ``claude_code`` writes it on the candidate of a call it gave the launcher."""
    return {"arm": arm, "launcher_calls": records, "launcher_passed": records, "records": records,
            "records_ignored": 0, "bash_refused": 0, "license_policy": "1" * 64}


def api(events, case, launcher=(True,)):
    """``run()``'s thread ``case`` as the API route records it. Call n had the launcher when ``launcher[n]`` (the last
    entry repeated) is true: its candidate carries the ledger, whose ``records`` is the call's GitHits count, and the
    skill's digest. No call lists an MCP server."""
    out = copy.deepcopy(events)
    calls = [e for e in out if e["case_id"] == case and e["kind"] == "candidate_registered"]
    for n, event in enumerate(calls):
        signal = event["body"]["signal"]
        # Each call its own harness (``run()`` shares one object between a thread's calls).
        signal["harness"] = {k: v for k, v in signal["harness"].items() if k != "mcp_servers"}
        if launcher[min(n, len(launcher) - 1)]:
            signal["githits"] = ledger((signal.get("tool_usage") or {}).get("githits", 0))
            signal["harness"]["skill"] = dict(SKILL)
    return out


def ledger_says(events, case, **fields):
    """``events`` with ``fields`` set on the ledger of every call of thread ``case``."""
    for event in events:
        if event["case_id"] == case and event["kind"] == "candidate_registered":
            event["body"]["signal"]["githits"].update(fields)
    return events


def lookups_of(events, case):
    """The metrics station's ``githits_lookups`` for thread ``case``, as the report reads it."""
    return next(r for r in experiment.runs(events, NAME) if r.thread == case).work.githits_lookups


def looked_up(events, case, *tools, guard="pass"):
    """``events`` with one verified ``lookup`` fact per tool appended to thread ``case``, as ``space.observe`` writes
    them (a call that fails journals its lookups and registers nothing)."""
    out = list(events)
    last = max((e for e in out if e["case_id"] == case), key=lambda e: e["seq"])
    for n, tool in enumerate(tools, 1):
        seq = last["seq"] + n
        out.append({"case_id": case, "seq": seq, "kind": "observed", "at": last["at"] + n, "previous": "0" * 64,
                    "hash": h(case, seq), "body": {
                        "key": "lookup", "source": HOST, "version": f"s9.1.{n}", "actor": HOST, "verified": True,
                        "value": {"tool": tool, "query_digest": "3" * 64, "ok": guard == "pass", "guard": guard}}})
    return out


class ApiRouteChecks(unittest.TestCase):
    """The checks of ``tests/test_experiment.py``'s ``Checks``, with the treatment's GitHits on the API route."""

    def pair(self, off=None, on=None, *, off_launcher=(False,), on_launcher=(True,)):
        events = run("x-off", "X", "off", **(off or {}))[0] + run("x-on", "X", "on", **{"githits": 1, **(on or {})})[0]
        return api(api(events, "x-off", off_launcher), "x-on", on_launcher)

    def row(self, events, **report):
        return experiment.report(events, NAME, **report)["rows"][0]

    def test_the_launcher_ledger_is_the_treatments_record_of_githits(self):
        events = self.pair()
        self.assertEqual(self.row(events)["flags"], [], "a clean pair: no MCP server anywhere, and no conflict")
        self.assertEqual({r.thread: r.server for r in experiment.runs(events, NAME)}, {"x-off": False, "x-on": True})
        bare = run("x-off", "X", "off")[0] + run("x-on", "X", "on", githits=1)[0]
        self.assertEqual(self.row(bare)["excluded"], ["arm_conflict"],
                         "the same pair without the ledger shows no GitHits in the treatment; before the report read "
                         "the ledger, every API-route treatment read like this")

    def test_arm_conflict_when_the_baseline_had_the_launcher_or_the_treatment_did_not(self):
        row = self.row(self.pair(on={"githits": 0}, on_launcher=(False,)))
        self.assertEqual((row["excluded"], row["flags"]), (["arm_conflict"], ["arm_conflict", "unused"]),
                         "a treatment run without --githits had no GitHits, and used none")
        self.assertEqual(self.row(self.pair(off_launcher=(True,)))["flags"], ["arm_conflict"],
                         "a baseline given the launcher is in conflict even if it looked nothing up")
        self.assertEqual(self.row(self.pair(off={"githits": 2}, off_launcher=(True,)))["excluded"],
                         ["arm_conflict", "contaminated"])
        text = experiment.table(experiment.report(self.pair(on_launcher=(False,)), NAME))
        self.assertIn("check arm_conflict (excluded): X: the record contradicts the label: the server was there in the "
                      "baseline (connected, or for GitHits the host's API launcher), or not in the treatment", text)

    def test_contaminated_counts_every_host_observed_githits_lookup_of_the_baseline(self):
        for tool in ("githits.search", "githits.example", "mcp__githits__search"):
            with self.subTest(tool=tool):
                events = looked_up(self.pair(), "x-off", tool)
                self.assertEqual(self.row(events)["excluded"], ["contaminated"],
                                 "no candidate of the baseline counted it, but the host journaled it")
        for tool, guard in (("WebSearch", "pass"), ("Bash", "not_launcher"), ("githits.doctor", "pass")):
            with self.subTest(tool=tool):
                self.assertEqual(self.row(looked_up(self.pair(), "x-off", tool, guard=guard))["flags"], [],
                                 "a web lookup, a shell command the guard refused, or a readiness check is not a "
                                 "GitHits lookup")

    def test_unused_when_the_treatment_made_no_githits_lookup(self):
        idle = self.pair(on={"githits": 0})
        self.assertEqual(self.row(idle)["flags"], ["unused"],
                         "the launcher was there and the model never used it: kept, and said, not a conflict")
        self.assertEqual(self.row(looked_up(idle, "x-on", "Bash", guard="shell_syntax"))["flags"], ["unused"],
                         "a launcher call the guard refused never reached GitHits")
        self.assertEqual(self.row(looked_up(idle, "x-on", "githits.doctor"))["flags"], ["unused"],
                         "a readiness check is not a lookup (metrics leaves it out of githits_lookups too)")
        self.assertEqual(self.row(looked_up(idle, "x-on", "mcp__githits__search"))["flags"], [],
                         "a lookup the host observed in a call that registered nothing is still a use")

    def test_an_unknown_lookup_count_is_never_read_as_zero(self):
        """``githits_lookups`` is unknown when a call declared no count and, where ``metrics`` reads it so (#139), when
        a launcher call or a client record cannot be vouched for (``signal.githits.mismatch``, ``unguarded``). The
        report never reads it as zero: a baseline that shows no lookup is ``contamination_unknown``, a treatment
        ``use_unknown``, never ``unused``; a lookup the host observed shows use whatever the count says. These hold on
        either reading of the count, so they assume neither."""
        silent = self.pair(on={"tools": False})  # unknown on every reading: no call declared a count
        row = self.row(silent)
        self.assertEqual(row["flags"], ["use_unknown"],
                         "the treatment can be shown neither to have used GitHits nor not")
        self.assertIsNone(row["values"]["on"]["githits_lookups"], "and its paired count is unknown, never 0")
        self.assertEqual(self.row(self.pair(off={"tools": False}))["flags"], ["contamination_unknown"])
        self.assertEqual(self.row(looked_up(silent, "x-on", "mcp__githits__search"))["flags"], [],
                         "a lookup the host observed shows use whatever the count says")
        self.assertEqual(self.row(looked_up(silent, "x-on", "githits.search"))["flags"], [],
                         "so does a client record a guarded launcher call stands behind")
        # A launcher call the guard passed that left no record: the count is 0 or unknown, as metrics reads it.
        gap = ledger_says(self.pair(on={"githits": 0}), "x-on", mismatch="githits_unrecorded", launcher_calls=1,
                          launcher_passed=1)
        self.assertEqual(self.row(gap)["flags"], ["unused"] if lookups_of(gap, "x-on").known else ["use_unknown"])
        self.assertEqual(self.row(looked_up(gap, "x-on", "githits.search"))["flags"], [])
        # A client record no guarded launcher call stands behind, in the baseline: counted, or unknown.
        doubtful = looked_up(self.pair(), "x-off", "githits.search", guard="unguarded")
        known = lookups_of(doubtful, "x-off").known
        self.assertEqual(self.row(doubtful)["flags"], ["contaminated"] if known else ["contamination_unknown"],
                         "never clean: contaminated if the count includes it, contamination_unknown if it cannot")

    def test_a_thread_with_the_launcher_in_some_calls_only_is_mixed(self):
        events = self.pair(on={"calls": 2}, on_launcher=(True, False))
        self.assertEqual({r.thread: r.server for r in experiment.runs(events, NAME)}["x-on"], "mixed")
        self.assertEqual(self.row(events)["flags"], ["server_mixed"], "a treatment call without it: kept, and said")
        baseline = self.pair(off={"calls": 2}, off_launcher=(False, True))
        self.assertEqual(self.row(baseline)["excluded"], ["arm_conflict"],
                         "a baseline with the launcher in one call of two")

    def test_only_the_hosts_ledger_says_the_launcher_was_there(self):
        for bad in (2, True, "githits", [], {"arm": "on"}, {"records": -1}, {"records": "1"}, {"records": True},
                    {"records": 1.0}):
            with self.subTest(githits=bad):
                events = self.pair(on_launcher=(False,))
                for event in events:
                    if event["case_id"] == "x-on" and event["kind"] == "candidate_registered":
                        event["body"]["signal"]["githits"] = bad
                self.assertEqual(self.row(events)["excluded"], ["arm_conflict"],
                                 "a count, a flag or an object without the host's record count is not the ledger")
        events = self.pair(on={"githits": 3}, on_launcher=(False,))
        for event in events:
            if event["case_id"] == "x-on" and event["kind"] == "candidate_registered":
                event["body"]["signal"]["harness"]["skill"] = dict(SKILL)
        self.assertEqual(self.row(events)["excluded"], ["arm_conflict"],
                         "lookup counts the model drove, and the skill text it was shown, do not say the launcher was "
                         "there")
        events = self.pair()
        for event in events:
            if event["kind"] == "candidate_registered" and event["case_id"] == "x-on":
                event["body"]["signal"]["githits"].update(arm="off", launcher_calls=0)
                event["body"]["signal"].pop("harness")
        self.assertEqual(self.row(events)["flags"], [],
                         "the operator's name for the arm and the model's calls are not read; the ledger alone "
                         "stands for the call when no harness was recorded")

    def test_either_route_counts_for_githits_and_only_for_githits(self):
        events = run("x-off", "X", "off")[0] + run("x-on", "X", "on", calls=2, githits=1, server="githits:connected")[0]
        events = api(events, "x-on", launcher=(False, True))
        for event in events:  # call 1 through the MCP server, call 2 through the launcher
            if event["case_id"] == "x-on" and event["body"].get("id") == "c1":
                event["body"]["signal"]["harness"]["mcp_servers"] = ["githits:connected"]
        self.assertEqual(self.row(events)["flags"], [], "GitHits in every call, by one route or the other")
        self.assertEqual(self.row(self.pair(), server="docs")["excluded"], ["arm_conflict"],
                         "the launcher is GitHits'; another server named on the command line is not it")
        self.assertEqual(self.row(self.pair(on_launcher=(False,)), server=None)["flags"], [],
                         "server None: an experiment that switches nothing")


class ApiArmEndToEnd(ClaudeCodeCase):
    """Both arms of the pilot as #134 runs them, modelled on ``tests/test_experiment.py``'s ``WithClaudeCode`` (#83):
    arm A is ``ClaudeCodeProvider`` as before, arm B the same with ``githits=True``. The stand-in ``claude`` lists no
    MCP server; arm B's lookups go through the launcher, the real query guard and the host's GitHits client to a
    loopback fake. Each thread is labelled with ``syberlabs experiment label`` before its first candidate, accepted by
    the unattended runner, and reviewed blind; the report has to read the arm, the lookups and the price off that
    record, with no help."""

    def setUp(self):
        super().setUp()
        self.fake = FakeGitHits()
        self.addCleanup(self.fake.close)
        # The api lane as an operator's machine has it: a key, no gateway or provider switch inherited from the
        # environment the suite runs in, and GitHits settings from nowhere but this test.
        env = {k: v for k, v in os.environ.items() if k not in THIRD_PARTY_SWITCHES and k not in GITHITS_SETTINGS}
        env.update({"ANTHROPIC_API_KEY": "test-placeholder", githits.TOKEN_ENV: "ghtest_" + secrets.token_hex(16),
                    githits.URL_ENV: self.fake.url})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        budget = {"max_candidates": 6, "max_evaluations": 6, "max_seconds": 600, "prices": PRICES, "max_dollars": 1.0}
        self.kit_ = self.kit(self.contract(**{"evolution.budget": budget}))

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli(["--home", str(self.root / ".syberlabs"), "--repo", str(self.root), "--actor",
                        "owner@example.test", *args])
        return code, out.getvalue()

    @staticmethod
    def launcher(words: str) -> list:
        """A lookup through the launcher: the stand-in puts the path ``--allowedTools`` names in place of
        ``{launcher}``; without that rule its Bash tool denies the command, as Claude Code's ``dontAsk`` does."""
        return ["Bash", {"command": "{launcher} " + words, "description": "look up a public API"}]

    def run_arm(self, item, arm, tag, *, githits_on, name, first=None, repair=None, lookups=()):
        """One labelled thread through ``ClaudeCodeProvider``: labelled, proposed, accepted, reviewed blind."""
        kit = self.kit_
        thread = kit.start(f"Add a CSV export ({item})", paths=["src/", "tests/"], unattended=True, actor=RUNNER)
        (kit.home / "current").write_text(thread.id + "\n")
        code, text = self.cli("experiment", "label", "--name", NAME, "--item", item, "--arm", arm, "--tag", tag)
        self.assertEqual(code, 0, text)
        self.assertNotIn("labelled_late", text)
        claude = self.stand_in(name, first=first, repair=repair, lookups=[list(entry) for entry in lookups])
        provider = ClaudeCodeProvider(kit.repo, bare=True, executable=claude, max_calls=3, githits=githits_on,
                                      arm=arm, thread_label=thread.id)
        found = thread.propose(provider)
        receipt = thread.accept(found[-1].id, actor=RUNNER)
        self.assertEqual(receipt.status, "succeeded", receipt.reason)
        thread.record_review("pass", reviewer="blind", revision=receipt.commit, by=REVIEWER)
        self.assertEqual(validate_events(thread.history(), "thread"), [])
        return thread, found

    def events(self):
        return [e for case in self.kit_.journal.thread_ids() for e in self.kit_.session.history(case)]

    def test_both_arms_through_the_api_launcher_give_a_paired_result_and_a_verdict(self):
        web = ["WebSearch", {"query": "zq91 web query: python csv DictWriter header row"}]
        search = self.launcher('search "csv DictWriter writeheader zq92" --target pypi:requests@2.32.3')
        idle = self.launcher('search "title case names zq93" --target pypi:requests@2.32.3')
        # LIB-1 needs a lookup: without GitHits the first try fails the host's tests and a repair fixes it.
        _, off = self.run_arm("LIB-1", "off", "needs_lookup", githits_on=False, name="lib-off", first=BROKEN,
                              repair={"src/report.py": REPORT}, lookups=[web])
        thread, on = self.run_arm("LIB-1", "on", "needs_lookup", githits_on=True, name="lib-on", lookups=[search])
        self.assertEqual(([c.evaluation for c in off], [c.evaluation for c in on]), (["failed", "passed"], ["passed"]))
        self.assertEqual(on[0].signal["githits"]["records"], 1, "the host read one authentic client record")
        self.assertNotIn("mcp_servers", on[0].signal["harness"], "arm B connected no MCP server")
        self.assertEqual([e["body"]["value"]["tool"] for e in thread.history() if e["kind"] == "observed"
                          and e["body"]["key"] == "lookup"], ["githits.search"])
        # ROUTINE-1 needs none: both arms are right first time; arm B calls GitHits once and has nothing to add.
        self.run_arm("ROUTINE-1", "off", "routine", githits_on=False, name="routine-off")
        self.run_arm("ROUTINE-1", "on", "routine", githits_on=True, name="routine-on", lookups=[idle])
        self.assertEqual(len(self.fake.requests), 2, "only arm B's launcher calls reached GitHits")

        found = experiment.report(self.events(), NAME, dict(self.kit_.session.contracts), delivered_by="review")
        self.assertEqual(found["items"], {"labelled": 2, "compared": 2, "excluded": 0})
        self.assertEqual(found["checks"], {}, "the record agrees with the labels: the launcher in arm B only, GitHits "
                                              "used there only, the same base, objective, model and lane")
        self.assertEqual({(r.item, r.arm): r.server for r in experiment.runs(self.events(), NAME)},
                         {("LIB-1", "off"): False, ("LIB-1", "on"): True, ("ROUTINE-1", "off"): False,
                          ("ROUTINE-1", "on"): True})
        rows = {row["item"]: row for row in found["rows"]}
        lib, routine = rows["LIB-1"]["values"], rows["ROUTINE-1"]["values"]
        self.assertEqual((lib["off"]["cost"], lib["on"]["cost"]), (round(2 * COST, 6), COST),
                         "host-observed dollars on the api lane: two priced calls off, one on")
        self.assertEqual((lib["off"]["fix_rounds"], lib["on"]["fix_rounds"]), (1.0, 0.0))
        self.assertEqual((lib["off"]["turns"], lib["on"]["turns"]), (4.0, 2.0), "Claude Code's num_turns, per call")
        self.assertEqual((lib["off"]["web_lookups"], lib["on"]["web_lookups"]), (2.0, 0.0))
        self.assertEqual((lib["off"]["githits_lookups"], lib["on"]["githits_lookups"]), (0.0, 1.0),
                         "arm B's lookup is the host's fact from the client's record")
        self.assertEqual((routine["off"]["cost"], routine["on"]["cost"], routine["on"]["githits_lookups"]),
                         (COST, COST, 1.0))
        self.assertEqual({(arm, row["values"][arm]["reviewed"]) for row in found["rows"] for arm in ("off", "on")},
                         {("off", 1.0), ("on", 1.0)})
        rule = found["rule"]
        self.assertEqual((rule["delivered"], rule["pending"], rule["cost_pairs"]), ({"off": 2.0, "on": 2.0}, 0, 2))
        self.assertEqual(rule["median_cost_difference"], round(-COST / 2, 6), "LIB-1 saves a call, ROUTINE-1 ties")
        self.assertIs(rule["met"], True)
        self.assertEqual(found["per_arm"]["on"]["lookups_blocked"], 0, "every launcher call passed the guard")

        code, text = self.cli("experiment", "report", "--name", NAME, "--delivered", "review")
        self.assertEqual(code, 0, text)
        self.assertIn("2 items compared, 0 excluded, 4 threads", text)
        verdict = next(line for line in text.splitlines() if line.startswith("rule ("))
        self.assertTrue(verdict.startswith("rule (accepted and reviewed = delivered): met."), verdict)
        lines = ["", "GitHits pilot, API route, both arms through ClaudeCodeProvider (stand-in claude, loopback "
                     "GitHits):"]
        for row in found["rows"]:
            for arm in ("off", "on"):
                value = row["values"][arm]
                lines.append(f"  {row['item']:9} {arm:3}  cost ${value['cost']:.4f}  turns {value['turns']:g}  "
                             f"fix rounds {value['fix_rounds']:g}  web {value['web_lookups']:g}  "
                             f"githits {value['githits_lookups']:g}  reviewed {value['reviewed']:g}")
        print("\n".join(lines + ["  " + verdict]))

    def test_an_api_pilot_run_against_its_label_is_excluded(self):
        search = self.launcher('search "csv DictWriter writeheader zq94" --target pypi:requests@2.32.3')
        # X: the operator passed --githits in the baseline (with the arm name "off"), and the model used it.
        self.run_arm("X", "off", "needs_lookup", githits_on=True, name="x-off", lookups=[search])
        self.run_arm("X", "on", "needs_lookup", githits_on=True, name="x-on", lookups=[search])
        # Y: the operator forgot --githits in the treatment, so its Bash call was denied and nothing was looked up.
        self.run_arm("Y", "off", "routine", githits_on=False, name="y-off")
        self.run_arm("Y", "on", "routine", githits_on=False, name="y-on", lookups=[search])
        found = experiment.report(self.events(), NAME, dict(self.kit_.session.contracts))
        rows = {row["item"]: row for row in found["rows"]}
        self.assertEqual(rows["X"]["excluded"], ["arm_conflict", "contaminated"])
        self.assertEqual((rows["Y"]["excluded"], rows["Y"]["flags"]), (["arm_conflict"], ["arm_conflict", "unused"]))
        self.assertEqual(found["items"], {"labelled": 2, "compared": 0, "excluded": 2})
        code, text = self.cli("experiment", "report", "--name", NAME)
        self.assertEqual(code, 1)
        self.assertIn("check arm_conflict (excluded): X, Y", text)
        self.assertIn("nothing was compared", text)


def load_rehearsal():
    path = Path(__file__).resolve().parents[1] / "examples" / "githits_ab.py"
    spec = importlib.util.spec_from_file_location("githits_ab", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Rehearsal(unittest.TestCase):
    """``examples/githits_ab.py`` on its default route, the API launcher: seven items through both arms of the real
    ``ClaudeCodeProvider``, with its own stand-in ``claude`` and loopback GitHits."""

    def test_the_api_route_rehearsal_compares_six_items_and_excludes_the_misconfigured_one(self):
        rehearsal, said = load_rehearsal(), []
        with tempfile.TemporaryDirectory() as folder:
            before = dict(os.environ)
            events, contracts = rehearsal.rehearse(Path(folder), "api", log=said.append)
            self.assertEqual(dict(os.environ), before, "the rehearsal puts the environment back")
        found = experiment.report(events, rehearsal.NAME, contracts, delivered_by="review")
        self.assertEqual(found["items"], {"labelled": 7, "compared": 6, "excluded": 1})
        self.assertEqual(found["checks"], {"arm_conflict": ["LIB-4"], "unused": ["LIB-4"]},
                         "the treatment that ran without --githits, and nothing else")
        self.assertEqual((found["rule"]["met"], found["by_tag"]["needs_lookup"]["rule"]["met"]), (False, True),
                         "docs/EXPERIMENTS.md: the rehearsal's rule is not met overall and met on needs_lookup")
        self.assertIn("The loopback GitHits received 9 requests; the real API received none.", said)
        runs = experiment.runs(events, rehearsal.NAME)
        self.assertEqual({(r.arm, r.server) for r in runs if r.item != "LIB-4"}, {("off", False), ("on", True)})
        lookups = {r.item: r.value("githits_lookups") for r in runs if r.arm == "on" and r.item != "LIB-4"}
        self.assertEqual({item: metric.value for item, metric in lookups.items()},
                         {"LIB-1": 2, "LIB-2": 2, "LIB-3": 2, "ROUTINE-1": 1, "ROUTINE-2": 1, "ROUTINE-3": 1})
        self.assertTrue(all("host-observed" in metric.note for metric in lookups.values()),
                        "counted from the host's lookup facts, not from what the stand-in declared")
        with self.assertRaises(ValueError):
            rehearsal.rehearse(Path("unused"), "carrier-pigeon")


if __name__ == "__main__":
    unittest.main()
