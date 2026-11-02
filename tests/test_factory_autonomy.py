"""The autonomy thin slice (AM1) on the fixture app: spec -> approved feature map -> tickets on the ledger -> Build
Threads driven by the runner with a stand-in proposer -> features ready for the owner's merge, inside an envelope.
Offline, no model, no network, no merge."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.factory import featuremap, ledger, loop, space
from syberlabs.factory import policy as policymod
from syberlabs.factory import spec as specmod

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "tests" / "fixtures" / "factory_app"
AUTONOMY = ROOT / "tests" / "fixtures" / "autonomy"
DECLARED = json.loads((APP / "architecture.json").read_text(encoding="utf-8"))
OWNER = {"kind": "human", "id": "tester"}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def promoted_spec(project: Path) -> dict:
    """The fixture spec as #154's object: from sectioned Markdown, footprint resolved against Space, validated, promoted."""
    document = featuremap.spec_from_markdown((AUTONOMY / "spec.md").read_text(encoding="utf-8"), "order-markers",
                                             "delivery markers on the order path", ["rules", "ingest", "main"], author=OWNER)
    graph = space.index(project, revision="base")
    space.map_components(graph, DECLARED)
    specmod.resolve_footprint(document, graph)
    specmod.transition(document, "validate", OWNER, space=graph)
    specmod.transition(document, "promote", OWNER, verdict_ids=["review-1"])
    return document


def fixture_map(document: dict, base: str, **overrides) -> dict:
    mapping = json.loads((AUTONOMY / "map.json").read_text(encoding="utf-8"))
    mapping["spec_ref"]["spec_digest"] = specmod.spec_digest(document)
    mapping["base"] = base
    mapping.update(overrides)
    return mapping


def hidden_catalogue() -> dict:
    raw = json.loads((AUTONOMY / "hidden_checks.json").read_text(encoding="utf-8"))
    return {name: [arg.replace("{python}", sys.executable) for arg in argv] for name, argv in raw.items() if not name.startswith("_")}


class Scratch(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="autonomy-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def project(self, name="project") -> tuple[Path, str]:
        """A copy of the fixture app as a Git repository with a deterministic base commit."""
        project = self.scratch / name
        shutil.copytree(APP, project)
        env = {**os.environ, **GIT_ENV}
        git = lambda *a: subprocess.run(["git", "-C", str(project), *a], check=True, capture_output=True, text=True, env=env).stdout.strip()
        git("init", "-q", "-b", "main")
        git("add", ".")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "base")
        return project, git("rev-parse", "HEAD")

    def make_loop(self, project: Path, mapping: dict, document: dict, *, calls_total=10, attempts=2, mode="", policy=None,
                  kill_switch=None, ledger_name="ledger") -> loop.Loop:
        home = ledger.Ledger(self.scratch / ledger_name)
        envelope = loop.new_envelope(featuremap.map_digest(mapping), dollars_total=0.0, calls_total=calls_total, seconds_total=600,
                                     attempts_per_feature=attempts, kill_switch=kill_switch)
        argv = [sys.executable, str(AUTONOMY / "standin_proposer.py"), str(project)] + ([mode] if mode else [])
        checks = {"tests": {"argv": [sys.executable, "-c", "import fxapp.main, fxapp.variants"], "timeout_seconds": 60}}
        return loop.Loop(repo_root=project, kit_home=project / ".syberlabs", ledger=home, map=mapping, spec=document, envelope=envelope,
                         policy=policy or policymod.OfflinePolicy(), proposer_argv=argv, checks=checks, hidden_catalogue=hidden_catalogue(),
                         human_owner="tester")


class SpecAndMap(Scratch):
    def test_sectioned_markdown_becomes_the_spec_object(self):
        document = featuremap.spec_from_markdown((AUTONOMY / "spec.md").read_text(encoding="utf-8"), "order-markers", "cap", ["rules"])
        self.assertEqual([r["req_id"] for r in document["requirements"]], ["R1", "R2", "R3"])
        self.assertEqual(document["requirements"][0]["scenarios"][0]["given"], "fxapp/rules.py")
        self.assertEqual(document["requirements"][0]["scenarios"][0]["then"], "the module ends with the delivery marker for F1")
        self.assertEqual(document["state"], "draft")
        with self.assertRaises(Rejected):
            featuremap.spec_from_markdown("no sections here", "x", "y", ["rules"])

    def test_fixture_map_validates_and_the_task_graph_measures(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        self.assertEqual(featuremap.mapcheck(mapping, document), [])
        graph = featuremap.taskgraph(mapping)
        self.assertEqual(graph["critical_path"], ["F1", "F2"])
        self.assertEqual(graph["critical_path_length"], 2)
        self.assertEqual(graph["total_work"], 4, "S + S + M")
        self.assertEqual(graph["work_critical_ratio"], 2.0)
        self.assertEqual(graph["width"], 2)
        self.assertIn(sorted(graph["max_antichain"]), (["F1", "F3"], ["F2", "F3"]), "either pair is a maximum antichain")
        self.assertEqual(graph["bottom_level"], {"F1": 2, "F2": 1, "F3": 2})
        self.assertEqual(graph["conflicts"], [])
        featuremap.approve(mapping, OWNER)
        self.assertTrue(featuremap.approved(mapping))
        self.assertEqual(featuremap.mapcheck(mapping, document), [])
        mapping["features"][0]["title"] = "edited after approval"
        self.assertFalse(featuremap.approved(mapping))
        self.assertIn("approval is present but stale", featuremap.mapcheck(mapping, document)[0])
        with self.assertRaises(Rejected) as refused:
            featuremap.approve(mapping, {"kind": "model", "id": "claude"})
        self.assertEqual(refused.exception.code, "approval_actor")

    def test_mapcheck_refuses_a_cycle_flags_a_scope_conflict_and_checks_coverage(self):
        project, base = self.project()
        document = promoted_spec(project)
        cyclic = fixture_map(document, base)
        cyclic["features"][0]["depends_on"] = ["F2"]
        findings = featuremap.mapcheck(cyclic, document)
        self.assertTrue(any("cycle" in f for f in findings), findings)
        with self.assertRaises(Rejected) as refused:
            featuremap.taskgraph(cyclic)
        self.assertEqual(refused.exception.code, "map_cyclic")
        conflict = fixture_map(document, base)
        conflict["features"][2]["scope"]["paths"] = ["fxapp/"]
        findings = featuremap.mapcheck(conflict, document)
        self.assertTrue(any(f.startswith("scope conflict: F1 and F3") for f in findings), findings)
        self.assertTrue(any(f.startswith("scope conflict: F2 and F3") for f in findings), findings)
        declared = fixture_map(document, base)
        declared["features"][2]["scope"]["paths"] = ["fxapp/"]
        declared["features"][2]["overlaps"] = [{"with": "F1", "paths": ["fxapp/rules.py"], "order": "F1_first"},
                                               {"with": "F2", "paths": ["fxapp/ingest.py"], "order": "F2_first"}]
        self.assertEqual(featuremap.mapcheck(declared, document), [], "a declared overlap with an order is not a finding")
        declared["features"][2]["depends_on"] = ["F1"]
        declared["features"][2]["overlaps"][0]["order"] = "F3_first"
        self.assertTrue(any("contradicts depends_on" in f for f in featuremap.mapcheck(declared, document)))
        uncovered = fixture_map(document, base)
        uncovered["features"][2]["req_ids"] = ["R1"]
        findings = featuremap.mapcheck(uncovered, document)
        self.assertIn("requirement R3 is covered by no feature and is not listed as uncovered", findings)
        uncovered["uncovered"] = [{"req_id": "R3", "reason": "deferred to the next map"}]
        self.assertEqual(featuremap.mapcheck(uncovered, document), [])
        unknown = fixture_map(document, base)
        unknown["features"][0]["req_ids"] = ["R9"]
        self.assertIn("F1: req_ids names unknown requirement 'R9'", featuremap.mapcheck(unknown, document))
        stale = fixture_map(document, base)
        stale["spec_ref"]["spec_digest"] = "0" * 64
        self.assertTrue(any("spec changed after the map" in f for f in featuremap.mapcheck(stale, document)))
        shared = fixture_map(document, base)
        shared["features"][1]["hidden_checks"] = ["hidden.f1.marker"]
        self.assertTrue(any("also named by F1" in f for f in featuremap.mapcheck(shared, document)))

    def test_risk_plan_keeps_the_floor_and_allocates_softmax_units(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        plan = featuremap.risk_plan(mapping, budget=4, tau=1.0)
        self.assertEqual(sum(plan["units"].values()), 4)
        self.assertTrue(plan["standardized"])
        self.assertAlmostEqual(sum(plan["share"].values()), 1.0, places=3)
        for fid, buys in plan["buys"].items():
            self.assertEqual(buys[: len(featuremap.FLOOR)], list(featuremap.FLOOR), f"{fid} keeps the floor")
        self.assertGreater(plan["r"]["F1"], plan["r"]["F2"], "F1 has a dependent and disagreement; F2 has neither")
        self.assertEqual(plan["inputs"]["F3"]["uncovered"], 0.5, "one of F3's two criteria has no mechanical check")
        sharp = featuremap.risk_plan(mapping, budget=4, tau=0.1)
        self.assertGreaterEqual(max(sharp["share"].values()), max(plan["share"].values()), "a lower tau concentrates")
        with self.assertRaises(Rejected):
            featuremap.risk_plan(mapping, tau=0.01)
        flat = fixture_map(document, base)
        for item in flat["features"]:
            item["depends_on"], item["size"], item["criteria"] = [], "S", [{"id": f"{item['id']}.c1", "text": "x", "kind": "test"}]
            item["risk_inputs"]["disagreement"] = 0.0
        plan = featuremap.risk_plan(flat, budget=3)
        self.assertFalse(plan["standardized"])
        self.assertEqual(plan["units"], {"F1": 1, "F2": 1, "F3": 1}, "zero spread allocates uniformly")

    def test_a_feature_compiles_to_one_ticket_in_the_spec_ticket_shape(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        graph = space.index(project, revision="base")
        space.map_components(graph, DECLARED)
        tickets = featuremap.compile_tickets(mapping, document, human_owner="tester", baseline_run=None, space=graph)
        self.assertEqual(len(tickets), 3)
        ticket = tickets[1]
        reference = specmod.ticket_from_spec(document, "R2", human_owner="tester")
        self.assertEqual(set(reference) - set(ticket), set(), "every key of ticket_from_spec's shape is present")
        self.assertEqual(ticket["ticket_id"], featuremap.digest({"map": featuremap.map_digest(mapping), "feature": "F2"})[:16])
        self.assertEqual(ticket["footprint"]["files"], ["fxapp/ingest.py"])
        self.assertEqual(ticket["footprint"]["components"], ["ingest"], "the Space graph's owner of the scope's files")
        bare = featuremap.compile_ticket(mapping, mapping["features"][1], document, human_owner="tester")
        self.assertEqual(bare["footprint"]["components"], [], "no Space graph, no component claim")
        self.assertEqual(ticket["exit_criteria"], ["evidence:tests", "hidden:hidden.f2.marker"])
        self.assertEqual(ticket["rubric_digest"], featuremap.digest(["hidden.f2.marker"]))
        self.assertEqual(ticket["map_ref"], {"map_id": "order-markers.m1", "map_digest": featuremap.map_digest(mapping), "feature_id": "F2"})
        self.assertEqual(ticket["acceptance_criteria"], mapping["features"][1]["criteria"])
        self.assertEqual(ticket["depends_on"], ["F1"])
        with self.assertRaises(Rejected) as refused:
            featuremap.compile_ticket(mapping, mapping["features"][0], document, human_owner="")
        self.assertEqual(refused.exception.code, "ticket_owner")
        degenerate = featuremap.degenerate_map(document, base=base, author=OWNER)
        self.assertEqual(featuremap.mapcheck(degenerate, document), [], "one feature per requirement, overlaps declared")
        self.assertEqual([f["req_ids"] for f in degenerate["features"]], [["R1"], ["R2"], ["R3"]])


class Policies(unittest.TestCase):
    STATE = {"features": {"F1": {"bottom_level": 2, "risk": 0.2, "map_order": 0, "attempts": 0, "attempts_left": 2},
                          "F3": {"bottom_level": 2, "risk": 0.1, "map_order": 2, "attempts": 0, "attempts_left": 2}},
             "envelope": {"dollars_left": 0.0, "calls_left": 1, "seconds_left": 10, "smallest_tier_dollars": 0.0,
                          "arms": [{"id": "cheap", "tier_rank": 0}, {"id": "strong", "tier_rank": 1}], "tiers": {"S": 0, "M": 0, "L": 0}}}

    def test_offline_rules(self):
        offline = policymod.OfflinePolicy()
        answer = policymod.ask(offline, self.STATE, policymod.Question("next_feature", None, ("F1", "F3")))
        self.assertEqual((answer.value, answer.reason), ("F1", "rule:critical_path_first"))
        self.assertEqual(policymod.ask(offline, self.STATE, policymod.Question("proposer_arm", "F1", ("strong", "cheap"))).value, "cheap")
        self.assertEqual(policymod.ask(offline, self.STATE, policymod.Question("budget_tier", "F1", ("S", "M"))).value, "S")
        self.assertEqual(policymod.ask(offline, self.STATE, policymod.Question("worth_continuing", "F1", ("continue", "stop"))).value, "continue")
        state = json.loads(json.dumps(self.STATE))
        state["envelope"]["calls_left"] = 0
        self.assertEqual(policymod.ask(offline, state, policymod.Question("worth_continuing", "F1", ("continue", "stop"))).value, "stop")
        state["features"]["F1"]["attempts"] = 1
        self.assertEqual(policymod.ask(offline, state, policymod.Question("after_failure", "F1", policymod.AFTER_FAILURE)).value, "retry_same")
        state["features"]["F1"]["attempts"] = 2
        self.assertEqual(policymod.ask(offline, state, policymod.Question("after_failure", "F1", policymod.AFTER_FAILURE)).value, "retry_escalate_arm")
        self.assertEqual(policymod.ask(offline, state, policymod.Question("after_failure", "F1", ("retry_same", "escalate_owner", "park"))).value,
                         "escalate_owner", "attempts left but no stronger arm: the owner decides")
        self.assertEqual(policymod.ask(offline, state, policymod.Question("after_failure", "F1", ("escalate_owner", "park"))).value, "park",
                         "no retry offered: the cap is spent")

    def test_closed_option_sets_are_enforced(self):
        with self.assertRaises(Rejected):
            policymod.Question("budget_tier", "F1", ("XL",))
        with self.assertRaises(Rejected):
            policymod.Question("replan", "F1", ("a",))
        question = policymod.Question("next_feature", None, ("F1", "F3"))
        with self.assertRaises(Rejected) as refused:
            policymod.validate(question, "F2", "test")
        self.assertEqual(refused.exception.code, "policy_invalid")
        with self.assertRaises(Rejected):
            policymod.validate(question, {"value": "F1", "probabilities": {"F1": 0.9, "F3": 0.3}}, "test")
        good = policymod.validate(question, {"value": "F1", "probabilities": {"F1": 0.7, "F3": 0.3}}, "test")
        self.assertEqual(good.probabilities["F3"], 0.3)

    def test_shadow_records_disagreement_and_never_acts(self):
        recorded = policymod.recorded_answers({"next_feature": {"value": "F3", "probabilities": {"F1": 0.4, "F3": 0.6}},
                                               "budget_tier:F1": "M", "after_failure": "F9"})
        shadow = policymod.ShadowPolicy(policymod.OfflinePolicy(), recorded, shadow_name="recorded-jev")
        answer = policymod.ask(shadow, self.STATE, policymod.Question("next_feature", None, ("F1", "F3")))
        self.assertEqual(answer.value, "F1", "the acting policy's answer stands")
        self.assertEqual(answer.shadow, {"policy": "recorded-jev", "value": "F3", "probabilities": {"F1": 0.4, "F3": 0.6},
                                         "agrees": False, "reason": "recorded"})
        self.assertEqual(shadow.disagreements, 1)
        answer = policymod.ask(shadow, self.STATE, policymod.Question("budget_tier", "F1", ("S", "M")))
        self.assertEqual((answer.value, answer.shadow["value"], answer.shadow["agrees"]), ("S", "M", False))
        answer = policymod.ask(shadow, self.STATE, policymod.Question("proposer_arm", "F1", ("cheap",)))
        self.assertEqual(answer.shadow["reason"], "shadow_unanswered")
        self.assertIsNone(answer.shadow["agrees"])
        state = json.loads(json.dumps(self.STATE))
        state["features"]["F1"]["attempts"] = 1
        answer = policymod.ask(shadow, state, policymod.Question("after_failure", "F1", ("retry_same", "park")))
        self.assertEqual((answer.value, answer.shadow["reason"]), ("retry_same", "policy_invalid"), "an answer outside the options is recorded, not acted on")
        self.assertEqual(shadow.disagreements, 2)


class EndToEnd(Scratch):
    def test_the_thin_slice_builds_three_features_to_ready_for_owner_merge(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        recorded = policymod.recorded_answers({"next_feature": "F3", "budget_tier": "S", "proposer_arm": "standin"})
        shadow = policymod.ShadowPolicy(policymod.OfflinePolicy(), recorded, shadow_name="recorded-jev")
        run = self.make_loop(project, mapping, document, policy=shadow)
        tickets = run.compile()
        home = run.ledger
        self.assertEqual([t["map_ref"]["feature_id"] for t in tickets], ["F1", "F2", "F3"])
        opened = [e for e in home.events() if e["kind"] == "ticket_opened"]
        self.assertEqual([(e["body"]["feature_id"], e["body"]["source"], e["body"]["human_owner"]) for e in opened],
                         [("F1", "map", "tester"), ("F2", "map", "tester"), ("F3", "map", "tester")])
        frozen = [e for e in home.events() if e["kind"] == "note" and e["body"]["note"] == "map_frozen"]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(frozen[0]["body"]["approved_by"], OWNER)
        self.assertEqual(frozen[0]["body"]["critical_path"], ["F1", "F2"])
        contracts = sorted(p.name for p in (project / ".syberlabs" / "contracts").glob("*.json"))
        self.assertEqual(contracts, sorted(f"syberfactory-{t['ticket_id']}.v1.json" for t in tickets))
        contract = json.loads((project / ".syberlabs" / "contracts" / contracts[0]).read_text(encoding="utf-8"))
        self.assertEqual(contract["evolution"]["scope"]["paths"], [tickets[[f"syberfactory-{t['ticket_id']}.v1.json" for t in tickets].index(contracts[0])]["footprint"]["files"][0]])
        self.assertEqual(len(contract["evolution"]["evaluation"]["required"]), 2, "the visible check and the hidden check")

        summary = run.run()
        self.assertEqual(summary["stopped"], "nothing_eligible")
        self.assertEqual(summary["ready_for_owner_merge"], ["F1", "F2", "F3"], summary)
        self.assertEqual(summary["envelope"]["calls"], 3)
        self.assertEqual(summary["envelope"]["dollars"], 0.0)
        self.assertEqual(summary["envelope"]["unsettled"], 0)
        events = loop.autonomy_events(home)
        kinds = [(e["kind"] if e["kind"] != "note" else e["body"]["note"]) for e in events]
        picks = [e["body"]["answer"]["value"] for e in events if e["kind"] == "note" and e["body"]["note"] == "policy_proposal"
                 and e["body"]["question"] == "next_feature"]
        self.assertEqual(picks, ["F1", "F3", "F2"], "critical path first: F1 (bottom level 2, a dependent), then F3 (M), then F2")
        shadows = [e["body"]["answer"]["shadow"] for e in events if e["kind"] == "note" and e["body"]["note"] == "policy_proposal"
                   and e["body"]["question"] == "next_feature"]
        self.assertEqual([s["agrees"] for s in shadows], [False, True, None], "the shadow disagreed on the first pick and was out of options on the last")
        self.assertEqual(shadows[2]["reason"], "policy_invalid")
        steps = [(e["body"]["feature"], e["body"]["step"], e["body"]["outcome"]) for e in events if e["kind"] == "note" and e["body"]["note"] == "thread_step"]
        self.assertEqual(steps, [("F1", "check", "acceptable"), ("F1", "accept", "succeeded"),
                                 ("F3", "check", "acceptable"), ("F3", "accept", "succeeded"),
                                 ("F2", "check", "acceptable"), ("F2", "accept", "succeeded")])
        self.assertEqual(kinds[:4], ["map_frozen", "ticket_opened", "ticket_opened", "ticket_opened"])
        self.assertEqual(kinds[-1], "loop_stopped")
        self.assertEqual(kinds.count("feature_ready"), 3)
        self.assertEqual(kinds.count("feature_failed"), 0)
        self.assertTrue(home.verify())
        ready = [e for e in events if e["kind"] == "note" and e["body"]["note"] == "feature_ready"]
        for event in ready:
            self.assertTrue(event["body"]["refs"]["target_ref"].startswith("refs/heads/syberlabs/"))
            self.assertEqual(event["body"]["next"], "owner merges; nothing here merges")
        main = subprocess.run(["git", "-C", str(project), "rev-parse", "main"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(main, base, "main never moved")
        f2 = summary["features"]["F2"]["commit"]
        shown = subprocess.run(["git", "-C", str(project), "show", f"{f2}:fxapp/rules.py"], capture_output=True, text=True, check=True).stdout
        self.assertIn("# F1 delivered", shown, "F2 stacked on F1's accepted commit")
        walk = loop.why(home, tickets[1]["ticket_id"])
        self.assertEqual((walk["feature"], walk["requirements"], walk["map"]["approved_by"]), ("F2", ["R2"], OWNER))
        self.assertEqual(walk["spec"]["digest"], specmod.spec_digest(document))
        self.assertEqual(walk["ready_for_owner_merge"]["commit"], f2)
        self.assertEqual(run.spend.totals()["stopped"], "nothing_eligible")
        # The same list, for the report: every autonomy event in order.
        print("\nledger events:", " ".join(kinds))

    def test_same_fixture_gives_the_same_event_digests(self):
        digests = []
        for name in ("one", "two"):
            project, base = self.project(name)
            document = promoted_spec(project)
            mapping = fixture_map(document, base)
            featuremap.approve(mapping, OWNER)
            run = self.make_loop(project, mapping, document, ledger_name=f"ledger-{name}")
            run.run()
            digests.append(loop.event_digests(run.ledger))
        self.assertEqual(len(digests[0]), len(digests[1]))
        self.assertEqual(digests[0], digests[1], "no timestamp, thread id or commit enters an event digest")
        self.assertGreater(len(digests[0]), 20)

    def test_a_map_without_owner_approval_is_refused(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        run = self.make_loop(project, mapping, document)
        with self.assertRaises(Rejected) as refused:
            run.compile()
        self.assertEqual(refused.exception.code, "map_unapproved")
        self.assertEqual(run.ledger.events(), [], "nothing was logged")
        featuremap.approve(mapping, OWNER)
        mapping["features"][0]["depends_on"] = ["F2"]
        run = self.make_loop(project, mapping, document, ledger_name="ledger2")
        with self.assertRaises(Rejected) as refused:
            run.compile()
        self.assertEqual(refused.exception.code, "map_unapproved", "an edit after approval leaves the approval stale")
        featuremap.approve(mapping, OWNER)
        run = self.make_loop(project, mapping, document, ledger_name="ledger3")
        with self.assertRaises(Rejected) as refused:
            run.compile()
        self.assertEqual(refused.exception.code, "map_invalid")
        self.assertIn("cycle", refused.exception.detail)

    def test_the_envelope_stops_the_loop(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        run = self.make_loop(project, mapping, document, calls_total=2)
        summary = run.run()
        self.assertEqual(summary["stopped"], "envelope_exhausted")
        self.assertEqual(summary["ready_for_owner_merge"], ["F1", "F3"])
        self.assertEqual(summary["features"]["F2"]["state"], "open", "F2 was eligible and waits for a new envelope")
        self.assertEqual(summary["envelope"]["calls_left"], 0)
        self.assertEqual(run.spend.totals()["stopped"], "envelope_exhausted")
        lines = run.spend.lines()
        self.assertEqual([line["kind"] for line in lines], ["reserve", "settle", "reserve", "settle", "stop"])

    def test_await_merge_leaves_a_dependent_waiting_for_the_owner(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base, integration="await_merge")
        featuremap.approve(mapping, OWNER)
        summary = self.make_loop(project, mapping, document).run()
        self.assertEqual(summary["ready_for_owner_merge"], ["F1", "F3"])
        self.assertEqual(summary["features"]["F2"]["state"], "waiting_owner_merge")
        self.assertEqual(summary["stopped"], "nothing_eligible")

    def test_a_failing_candidate_is_refused_by_the_kernel_and_the_feature_is_parked(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        run = self.make_loop(project, mapping, document, attempts=1, mode="--break")
        summary = run.run()
        self.assertEqual(summary["ready_for_owner_merge"], [])
        self.assertEqual({row["state"] for row in summary["features"].values()} - {"open"}, {"parked"})
        self.assertTrue(summary["features"]["F1"]["last_reason"].startswith("candidate_check_failed:"), summary["features"]["F1"])
        self.assertIn(summary["features"]["F1"]["last_reason"].split(":")[1], ("tests", "hidden.f1.marker"), "both checks fail; one is named")
        self.assertEqual(summary["features"]["F2"]["state"], "open", "F2 waits on F1 and never started")
        events = loop.autonomy_events(run.ledger)
        questions = [(e["body"]["question"], e["body"]["answer"]["value"]) for e in events
                     if e["kind"] == "note" and e["body"]["note"] == "policy_proposal" and e["body"]["feature"] == "F1"]
        self.assertEqual(questions[-2:], [("worth_continuing", "stop"), ("after_failure", "park")])
        self.assertEqual(summary["envelope"]["calls"], 2, "one call per parked feature; the envelope counted both")

    def test_a_candidate_outside_its_scope_never_reaches_accept(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        run = self.make_loop(project, mapping, document, attempts=2, mode="--outside")
        summary = run.run()
        self.assertEqual(summary["ready_for_owner_merge"], [])
        self.assertEqual(summary["features"]["F1"]["last_reason"], "candidate_out_of_scope")
        self.assertEqual(summary["features"]["F1"]["attempts"], 2, "retry_same once, then parked")
        self.assertEqual(summary["features"]["F1"]["state"], "parked")

    def test_the_kill_switch_stops_before_the_next_decision(self):
        project, base = self.project()
        document = promoted_spec(project)
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        switch = self.scratch / "stop-now"
        switch.write_text("")
        run = self.make_loop(project, mapping, document, kill_switch=str(switch))
        summary = run.run()
        self.assertEqual(summary["stopped"], "kill_switch")
        self.assertEqual(summary["ready_for_owner_merge"], [])
        self.assertEqual(summary["envelope"]["calls"], 0)

    def test_the_slice_holds_no_merge_and_no_network(self):
        for name in ("featuremap", "policy", "loop", "autonomy_cli"):
            text = (ROOT / "syberlabs" / "factory" / f"{name}.py").read_text(encoding="utf-8")
            for forbidden in ('"merge"', "merge_pull", "git_push", "github_pull_request", "urllib", "http.client", "socket", "requests"):
                self.assertNotIn(forbidden, text, f"{name}.py holds {forbidden}")
        with self.assertRaises(Rejected):
            loop.record(ledger.Ledger(self.scratch / "l"), "merged", "x", {})
        self.assertNotIn("merge", loop.ACTIONS)
        self.assertNotIn("publish", " ".join(loop.ACTIONS))


if __name__ == "__main__":
    unittest.main()
