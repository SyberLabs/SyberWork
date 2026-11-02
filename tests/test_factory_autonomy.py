"""The autonomy thin slice (AM1) on the fixture app: spec -> approved feature map -> tickets on the ledger -> Build
Threads driven by the runner with a stand-in proposer -> features ready for the owner's merge, inside an envelope.
Offline, no model, no network, no merge. The spec is promoted through the goal gate's real path: review and
validate events on the ledger, the owner's signed approval verified and recorded, the promotion citing it."""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from syberlabs.canonical import canonical
from syberlabs.ed25519 import generate
from syberlabs.errors import Rejected
from syberlabs.factory import featuremap, ledger, loop, space
from syberlabs.factory import policy as policymod
from syberlabs.factory import spec as specmod

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "tests" / "fixtures" / "factory_app"
AUTONOMY = ROOT / "tests" / "fixtures" / "autonomy"
DECLARED = json.loads((APP / "architecture.json").read_text(encoding="utf-8"))
OWNER = {"kind": "human", "id": "tester"}
OWNER_SEED, OWNER_PUBLIC = generate()
VERDICT = "review-1"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def candidate_spec(project: Path) -> dict:
    """The fixture spec as #154's object: from sectioned Markdown, footprint resolved against Space, validated."""
    document = featuremap.spec_from_markdown((AUTONOMY / "spec.md").read_text(encoding="utf-8"), "order-markers",
                                             "delivery markers on the order path", ["rules", "ingest", "main"], author=OWNER)
    graph = space.index(project, revision="base")
    space.map_components(graph, DECLARED)
    specmod.resolve_footprint(document, graph)
    specmod.transition(document, "validate", OWNER, space=graph)
    return document


def promoted_spec(project: Path, home: ledger.Ledger, *, seed: bytes = OWNER_SEED, public: bytes = OWNER_PUBLIC) -> dict:
    """The candidate promoted the way `factory spec promote` does it (C1's goal gate): the ledger pins the owner
    key (bootstrap, once); a model's variant, an independent reviewer's accepting verdict and the validate that
    wrote the candidate are on the ledger; the owner signs an approval over the spec's digest, `gate` resolves the
    verdict and verifies the approval under the pinned key (read through the lineage with the ledger's stored
    rotations), `transition` promotes, the approval is recorded and
    the `spec_changed` cites it."""
    document = candidate_spec(project)
    if home.owner_key() is None:
        home.set_owner_key(public)
    before = specmod.spec_digest(document)
    reviewer = {"kind": "model", "id": "gpt", "model": "gpt-5", "family": "openai"}
    home.append("spec_changed", {"spec_id": document["spec_id"], "version": document["version"], "operation": "mutate",
                                 "variant_id": "var1", "actor": {"kind": "model", "id": "claude", "family": "anthropic"}})
    home.append("spec_changed", {"spec_id": document["spec_id"], "version": document["version"], "operation": "review",
                                 "verdict_id": VERDICT, "variant_id": "var1", "decision": "accept", "actor": reviewer})
    home.append("spec_changed", {"spec_id": document["spec_id"], "version": document["version"], "operation": "validate",
                                 "accepted_variant": "var1", "verdict_id": VERDICT, "spec_digest": before})
    envelope = specmod.sign_approval(specmod.approval_payload(document, "promote", OWNER, verdict_ids=[VERDICT]), seed)
    approval, resolved = specmod.gate(document, "promote", OWNER, home.events(), envelope, verdict_ids=[VERDICT], spec_digest_value=before,
                                       load_rotation=home.load_rotation)
    record = specmod.transition(document, "promote", OWNER, verdict_ids=[VERDICT], approval=approval, spec_digest=before,
                                resolved_verdicts=resolved)
    event = home.record_approval(envelope, approval)
    record["approval"] = {**record["approval"], "event_hash": event["hash"], "approval_digest": event["body"]["approval_digest"]}
    home.append("spec_changed", {"spec_id": document["spec_id"], "version": document["version"], **record,
                                 "spec_digest": specmod.spec_digest(document), "footprint": document["footprint"]["components"]})
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


def git(project: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


class Scratch(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="autonomy-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def project(self, name="project") -> tuple[Path, str]:
        """A copy of the fixture app as a Git repository with a deterministic base commit."""
        project = self.scratch / name
        shutil.copytree(APP, project)
        git(project, "init", "-q", "-b", "main")
        git(project, "add", ".")
        git(project, "-c", "commit.gpgsign=false", "commit", "-qm", "base")
        return project, git(project, "rev-parse", "HEAD")

    def home(self, name="ledger") -> ledger.Ledger:
        return ledger.Ledger(self.scratch / name)

    def make_loop(self, project: Path, mapping: dict, document: dict, home: ledger.Ledger, *, calls_total=10, attempts=2, mode="",
                  policy=None, kill_switch=None, arms=None) -> loop.Loop:
        envelope = loop.new_envelope(featuremap.map_digest(mapping), dollars_total=0.0, calls_total=calls_total, seconds_total=600,
                                     attempts_per_feature=attempts, kill_switch=kill_switch, arms=arms)
        argv = [sys.executable, str(AUTONOMY / "standin_proposer.py"), str(project)] + ([mode] if mode else [])
        checks = {"tests": {"argv": [sys.executable, "-c", "import fxapp.main, fxapp.variants"], "timeout_seconds": 60}}
        return loop.Loop(repo_root=project, kit_home=project / ".syberlabs", ledger=home, map=mapping, spec=document, envelope=envelope,
                         policy=policy or policymod.OfflinePolicy(), proposer_argv=argv, checks=checks, hidden_catalogue=hidden_catalogue(),
                         human_owner="tester")

    def ready_loop(self, name="project", ledger_name="ledger", **overrides) -> tuple[loop.Loop, Path, str, dict]:
        """A project, a promoted spec on a fresh ledger and an approved fixture map: the loop ready to compile."""
        project, base = self.project(name)
        home = self.home(ledger_name)
        document = promoted_spec(project, home)
        mapping = fixture_map(document, base, **overrides.pop("map", {}))
        featuremap.approve(mapping, OWNER)
        return self.make_loop(project, mapping, document, home, **overrides), project, base, mapping


def notes(events, note):
    return [e for e in events if e["kind"] == "note" and e["body"]["note"] == note]


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
        document = promoted_spec(project, self.home())
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
        document = promoted_spec(project, self.home())
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
        # Scope entries are compared normalised: case, leading "./" segments, backslashes and a trailing slash.
        cased = fixture_map(document, base)
        cased["features"][2]["scope"]["paths"] = ["./FXAPP/Rules.py"]
        self.assertTrue(any(f.startswith("scope conflict: F1 and F3") for f in featuremap.mapcheck(cased, document)),
                        "FXAPP/Rules.py is fxapp/rules.py on a case-insensitive filesystem")
        self.assertTrue(featuremap.path_intersects("./fxapp\\rules.py", "FXAPP/"))
        self.assertFalse(featuremap.path_intersects(".github/x", "github/x"), "a leading dot is part of a name, not a './' prefix")
        self.assertFalse(featuremap.path_intersects("fxapp/rules.py", "fxapp/rules"))

    def test_risk_plan_keeps_the_floor_and_allocates_softmax_units(self):
        project, base = self.project()
        document = promoted_spec(project, self.home())
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
        # Weights are validated: exactly the known inputs, each in [0, 1], summing to 1, so r stays in [0, 1].
        for bad in ({**featuremap.WEIGHTS, "scope": 0.5}, {k: v for k, v in featuremap.WEIGHTS.items() if k != "scope"},
                    {**featuremap.WEIGHTS, "extra": 0.0}, {**featuremap.WEIGHTS, "scope": -0.15, "act_class": 0.45},
                    {**featuremap.WEIGHTS, "scope": float("nan")}):
            with self.assertRaises(Rejected, msg=str(bad)) as refused:
                featuremap.risk_plan(mapping, weights=bad)
            self.assertEqual(refused.exception.code, "risk_weights")
        flat = fixture_map(document, base)
        for item in flat["features"]:
            item["depends_on"], item["size"], item["criteria"] = [], "S", [{"id": f"{item['id']}.c1", "text": "x", "kind": "test"}]
            item["risk_inputs"]["disagreement"] = 0.0
        plan = featuremap.risk_plan(flat, budget=3)
        self.assertFalse(plan["standardized"])
        self.assertEqual(plan["units"], {"F1": 1, "F2": 1, "F3": 1}, "zero spread allocates uniformly")

    def test_a_feature_compiles_to_one_ticket_in_the_spec_ticket_shape(self):
        project, base = self.project()
        document = promoted_spec(project, self.home())
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


class Envelope(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="envelope-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.envelope = loop.new_envelope("m" * 64, dollars_total=1.0, calls_total=3, seconds_total=10.0)
        self.spend = loop.EnvelopeLedger(loop.envelope_path(self.scratch, self.envelope), self.envelope)

    def raw(self, **fields) -> None:
        line = {"schema": loop.ENVELOPE_LINE_SCHEMA, "envelope_digest": self.spend.digest, **fields}
        with self.spend.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(line) + "\n")

    def refuses(self, code, call, *args, **kwargs):
        with self.assertRaises(Rejected, msg=f"{args} {kwargs}") as refused:
            call(*args, **kwargs)
        self.assertEqual(refused.exception.code, code, refused.exception.detail)

    def test_the_kill_switch_path_stays_out_of_the_digest_and_the_file_is_keyed_by_it(self):
        with_switch = loop.new_envelope("m" * 64, dollars_total=1.0, calls_total=3, seconds_total=10.0, kill_switch=str(self.scratch / "stop"))
        self.assertEqual(loop.envelope_digest(with_switch), self.spend.digest, "a host path is not a term of the grant")
        self.assertEqual(self.spend.path, self.scratch / loop.ENVELOPE_DIR / f"{self.spend.digest}.jsonl")
        other = loop.new_envelope("m" * 64, dollars_total=1.0, calls_total=4, seconds_total=10.0)
        self.assertNotEqual(loop.envelope_path(self.scratch, other), self.spend.path, "another grant, another file")

    def test_reserve_and_settle_are_strict(self):
        self.refuses("envelope_conflict", self.spend.settle, "F1", 1, dollars=0.0, calls=1, seconds=1.0, unknown_models=[])
        self.spend.reserve("F1", 1, "S", "standin")
        self.refuses("envelope_conflict", self.spend.reserve, "F1", 1, "S", "standin")
        for bad in ({"calls": -1}, {"calls": 1.0}, {"seconds": -0.5}, {"seconds": float("nan")}, {"dollars": float("inf")},
                    {"dollars": -0.01}, {"calls": True}):
            fields = {"dollars": 0.0, "calls": 1, "seconds": 1.0, **bad}
            self.refuses("envelope_unpriced", self.spend.settle, "F1", 1, unknown_models=[], **fields)
        self.assertEqual([line["kind"] for line in self.spend.lines()], ["reserve"], "a refused settle wrote nothing")
        self.spend.settle("F1", 1, dollars=0.25, calls=1, seconds=2.0, unknown_models=[])
        self.refuses("envelope_conflict", self.spend.settle, "F1", 1, dollars=0.0, calls=1, seconds=1.0, unknown_models=[])
        totals = self.spend.totals()
        self.assertEqual((totals["calls"], totals["dollars"], totals["seconds"], totals["unsettled"]), (1, 0.25, 2.0, 0))
        self.assertEqual((totals["calls_left"], totals["dollars_left"]), (2, 0.75))

    def test_a_file_line_that_is_not_a_priced_envelope_line_refuses_the_file(self):
        self.spend.reserve("F1", 1, "S", "standin")
        cases = [
            ("envelope_unreadable", {"feature": "F1", "attempt": 1, "dollars": 0.0, "calls": 1, "seconds": 0.0}),  # no kind: not a KeyError
            ("envelope_unreadable", {"kind": "refund", "feature": "F1", "attempt": 1, "dollars": 0.0, "calls": 1, "seconds": 0.0}),
            ("envelope_unpriced", {"kind": "settle", "feature": "F1", "attempt": 1, "dollars": -1.0, "calls": 1, "seconds": 0.0}),
            ("envelope_unpriced", {"kind": "settle", "feature": "F1", "attempt": 1, "dollars": 0.0, "calls": -2, "seconds": 0.0}),
            ("envelope_unpriced", {"kind": "settle", "feature": "F1", "attempt": 1, "dollars": 0.0, "calls": 1, "seconds": -3.0}),
            ("envelope_unpriced", {"kind": "reserve", "feature": "F1", "attempt": 2, "dollars": 0.0, "calls": "1", "seconds": 0.0}),
            ("envelope_unreadable", {"kind": "reserve", "attempt": 2, "dollars": 0.0, "calls": 1, "seconds": 0.0}),  # no feature
            ("envelope_unreadable", {"kind": "stop"}),  # no reason
            ("envelope_conflict", {"kind": "reserve", "feature": "F1", "attempt": 1, "dollars": 0.0, "calls": 1, "seconds": 0.0}),
            ("envelope_conflict", {"kind": "settle", "feature": "F2", "attempt": 1, "dollars": 0.0, "calls": 1, "seconds": 0.0}),
        ]
        good = self.spend.path.read_text(encoding="utf-8")
        for code, fields in cases:
            self.raw(**fields)
            self.refuses(code, self.spend.totals)
            self.refuses(code, self.spend.room, "S")
            self.spend.path.write_text(good, encoding="utf-8")
        self.raw(kind="stop", reason="x", envelope_digest="0" * 64)
        self.refuses("envelope_unreadable", self.spend.totals)
        self.spend.path.write_text(good, encoding="utf-8")
        self.raw(kind="settle", feature="F1", attempt=1, dollars=0.0, calls=1, seconds=0.0)
        self.raw(kind="settle", feature="F1", attempt=1, dollars=0.0, calls=1, seconds=0.0)
        self.refuses("envelope_conflict", self.spend.totals)
        self.spend.path.write_text(good, encoding="utf-8")
        self.assertEqual(self.spend.totals()["calls"], 1, "the hold counts until it settles")


class EndToEnd(Scratch):
    def test_the_thin_slice_builds_three_features_to_ready_for_owner_merge(self):
        project, base = self.project()
        home = self.home()
        document = promoted_spec(project, home)
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        recorded = policymod.recorded_answers({"next_feature": "F3", "budget_tier": "S", "proposer_arm": "standin"})
        shadow = policymod.ShadowPolicy(policymod.OfflinePolicy(), recorded, shadow_name="recorded-jev")
        run = self.make_loop(project, mapping, document, home, policy=shadow)
        tickets = run.compile()
        self.assertEqual([t["map_ref"]["feature_id"] for t in tickets], ["F1", "F2", "F3"])
        self.assertEqual(run.promotion["keyid"], OWNER_PUBLIC.hex(), "the approval was re-verified under the key the ledger pins")
        opened = [e for e in home.events() if e["kind"] == "ticket_opened"]
        self.assertEqual([(e["body"]["feature_id"], e["body"]["source"], e["body"]["human_owner"]) for e in opened],
                         [("F1", "map", "tester"), ("F2", "map", "tester"), ("F3", "map", "tester")])
        frozen = notes(home.events(), "map_frozen")
        self.assertEqual(len(frozen), 1)
        self.assertEqual(frozen[0]["body"]["approved_by"], OWNER)
        self.assertEqual(frozen[0]["body"]["promoted_by"], OWNER)
        self.assertEqual(frozen[0]["body"]["refs"]["promotion"]["approval_digest"], run.promotion["approval_digest"])
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
        picks = [e["body"]["answer"]["value"] for e in notes(events, "policy_proposal") if e["body"]["question"] == "next_feature"]
        self.assertEqual(picks, ["F1", "F3", "F2"], "critical path first: F1 (bottom level 2, a dependent), then F3 (M), then F2")
        shadows = [e["body"]["answer"]["shadow"] for e in notes(events, "policy_proposal") if e["body"]["question"] == "next_feature"]
        self.assertEqual([s["agrees"] for s in shadows], [False, True, None], "the shadow disagreed on the first pick and was out of options on the last")
        self.assertEqual(shadows[2]["reason"], "policy_invalid")
        steps = [(e["body"]["feature"], e["body"]["step"], e["body"]["outcome"]) for e in notes(events, "thread_step")]
        self.assertEqual(steps, [("F1", "check", "acceptable"), ("F1", "accept", "succeeded"),
                                 ("F3", "check", "acceptable"), ("F3", "accept", "succeeded"),
                                 ("F2", "check", "acceptable"), ("F2", "accept", "succeeded")])
        self.assertEqual(kinds[:4], ["map_frozen", "ticket_opened", "ticket_opened", "ticket_opened"])
        self.assertEqual(kinds[-1], "loop_stopped")
        self.assertEqual(kinds.count("feature_ready"), 3)
        self.assertEqual(kinds.count("feature_failed"), 0)
        self.assertTrue(home.verify())
        for event in notes(events, "feature_ready"):
            self.assertTrue(event["body"]["refs"]["target_ref"].startswith("refs/heads/syberlabs/"))
            self.assertEqual(event["body"]["next"], "owner merges; nothing here merges")
        self.assertEqual(git(project, "rev-parse", "main"), base, "main never moved")
        # The owner's checkout is as it was: HEAD on main at the base commit, a clean tree, no scratch branch left.
        self.assertEqual(git(project, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(git(project, "rev-parse", "HEAD"), base)
        self.assertEqual(git(project, "status", "--porcelain"), "")
        self.assertEqual([b for b in git(project, "branch", "--list", "syberfactory/*").split() if b], [])
        f2 = summary["features"]["F2"]["commit"]
        shown = git(project, "show", f"{f2}:fxapp/rules.py")
        self.assertIn("# F1 delivered", shown, "F2 stacked on F1's accepted commit")
        walk = loop.why(home, tickets[1]["ticket_id"])
        self.assertEqual((walk["feature"], walk["requirements"], walk["map"]["approved_by"]), ("F2", ["R2"], OWNER))
        self.assertEqual(walk["spec"]["digest"], specmod.spec_digest(document))
        self.assertEqual(walk["spec"]["promoted_by"], OWNER)
        self.assertEqual(walk["spec"]["promotion"]["seq"], run.promotion["seq"])
        self.assertEqual(walk["ready_for_owner_merge"]["commit"], f2)
        self.assertEqual(run.spend.totals()["stopped"], "nothing_eligible")
        self.assertEqual(run.spend.path.parent.name, loop.ENVELOPE_DIR)
        # The same list, for the report: every autonomy event in order.
        print("\nledger events:", " ".join(kinds))

    def test_same_fixture_gives_the_same_event_digests(self):
        digests = []
        for name in ("one", "two"):
            run, _, _, _ = self.ready_loop(name, f"ledger-{name}")
            run.run()
            digests.append(loop.event_digests(run.ledger))
        self.assertEqual(len(digests[0]), len(digests[1]))
        self.assertEqual(digests[0], digests[1], "no timestamp, thread id, commit or approval digest enters an event digest")
        self.assertGreater(len(digests[0]), 20)

    def test_compile_needs_the_ledgers_promotion_record_not_the_files_state(self):
        """The file's ``state`` decides nothing: the ledger must hold a promote of exactly this spec, citing a
        recorded approval by a human, whose envelope is stored and verifies under the owner key the ledger pinned."""
        project, base = self.project()
        home = self.home()
        in_memory = candidate_spec(project)
        payload = specmod.approval_payload(in_memory, "promote", OWNER, verdict_ids=[VERDICT])
        approval = specmod.verify_approval(specmod.sign_approval(payload, OWNER_SEED), OWNER_PUBLIC, in_memory, "promote", verdict_ids=[VERDICT])
        specmod.transition(in_memory, "promote", OWNER, verdict_ids=[VERDICT], approval=approval)
        self.assertEqual(in_memory["state"], "promoted", "the file says promoted")
        mapping = fixture_map(in_memory, base)
        featuremap.approve(mapping, OWNER)
        run = self.make_loop(project, mapping, in_memory, home)
        with self.assertRaises(Rejected) as refused:
            run.compile()
        self.assertEqual(refused.exception.code, "spec_unpromoted", "promoted in memory, never on the ledger")
        self.assertEqual(loop.autonomy_events(home), [], "nothing was logged")

        document = promoted_spec(project, home)
        edited = json.loads(json.dumps(document))
        edited["capability"] = "edited after promotion"
        mapping = fixture_map(edited, base)
        featuremap.approve(mapping, OWNER)
        with self.assertRaises(Rejected) as refused:
            self.make_loop(project, mapping, edited, home).compile()
        self.assertEqual(refused.exception.code, "spec_unpromoted", "the file is not the spec the ledger promoted")

        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        # A promotion whose approval was signed under a key the ledger never pinned, or on a ledger that pins no
        # key, is a record without a trust root: refused whatever the file and the events say.
        other_seed, other_public = generate()
        for name, pin, code in (("unpinned", False, "owner_key_unset"), ("other-key", True, "owner_key_mismatch")):
            forged_home = self.home(name)
            if pin:
                forged_home.set_owner_key(OWNER_PUBLIC)
            forged = candidate_spec(project)
            envelope = specmod.sign_approval(specmod.approval_payload(forged, "promote", OWNER, verdict_ids=[VERDICT]), other_seed)
            approval = specmod.verify_approval(envelope, other_public, forged, "promote", verdict_ids=[VERDICT])
            record = specmod.transition(forged, "promote", OWNER, verdict_ids=[VERDICT], approval=approval)
            event = forged_home.record_approval(envelope, approval)
            record["approval"] = {**record["approval"], "event_hash": event["hash"], "approval_digest": event["body"]["approval_digest"]}
            forged_home.append("spec_changed", {"spec_id": forged["spec_id"], "version": forged["version"], **record,
                                                "spec_digest": specmod.spec_digest(forged), "footprint": forged["footprint"]["components"]})
            self.assertEqual(specmod.spec_digest(forged), specmod.spec_digest(document), "the same spec, differently approved")
            with self.assertRaises(Rejected, msg=name) as refused:
                self.make_loop(project, mapping, forged, forged_home).compile()
            self.assertEqual(refused.exception.code, code, name)

        approvals = next(p for p in (home.home / "approvals").glob("*.json"))
        kept = approvals.read_text(encoding="utf-8")
        approvals.write_text(kept.replace('"', "'", 1), encoding="utf-8")
        with self.assertRaises(Rejected) as refused:
            self.make_loop(project, mapping, document, home).compile()
        self.assertEqual(refused.exception.code, "spec_unpromoted", "a damaged approval envelope refuses")
        approvals.write_text(kept, encoding="utf-8")

        run = self.make_loop(project, mapping, document, home)
        run.compile()
        self.assertEqual(run.promotion["approver"], OWNER)
        self.assertEqual(run.promotion["verdict_ids"], [VERDICT])
        self.assertEqual(run.promotion["keyid"], OWNER_PUBLIC.hex())
        self.assertEqual(loop.promotion_record(home, document)["seq"], run.promotion["seq"])

    def test_compile_reads_the_owner_key_through_the_ledgers_lineage(self):
        """M3 of the #164 verification, on the autonomy side: the owner key is the ledger's verified lineage, read
        with the ledger's stored rotations. A spec promoted after an honest rotation (bootstrap -> rotation signed
        by the first key, the approval signed by the second) compiles under the second key; that lineage cannot be
        read without the loader. A bare second ``owner_key_set`` appended through the public ``append`` after the
        promotion (the hash chain intact) leaves the ledger pinning nothing: ``compile`` refuses
        ``owner_key_lineage``, not ``owner_key_unset`` and not the earlier key, and writes nothing."""
        project, base = self.project()
        home = self.home()
        second_seed, second_public = generate()
        home.set_owner_key(OWNER_PUBLIC)
        home.set_owner_key(second_public, seed=OWNER_SEED)
        self.assertEqual(home.owner_key(), second_public.hex())
        document = promoted_spec(project, home, seed=second_seed, public=second_public)
        with self.assertRaises(Rejected) as refused:
            specmod.pinned_owner_key(home.events())
        self.assertEqual(refused.exception.code, "owner_key_lineage", "a rotated ledger is unreadable without the loader")
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        run = self.make_loop(project, mapping, document, home)
        run.compile()
        self.assertEqual(run.promotion["keyid"], second_public.hex(), "the key pinned before the approval, through the rotation")
        self.assertTrue(home.verify())
        self.assertEqual(len(notes(home.events(), "map_frozen")), 1)

        # The M3 repro: an honest bootstrap and promotion, then a bare owner_key_set appended with the chain intact.
        project, base = self.project("tampered")
        tampered = self.home("tampered-ledger")
        document = promoted_spec(project, tampered)
        _, attacker_public = generate()
        tampered.append("owner_key_set", {"keyid": attacker_public.hex(), "previous": None, "rotation_digest": None})
        self.assertTrue(ledger._verify_chain(tampered.events()), "the chain is intact; only the lineage is broken")
        self.assertFalse(tampered.verify())
        mapping = fixture_map(document, base)
        featuremap.approve(mapping, OWNER)
        before = len(tampered.events())
        with self.assertRaises(Rejected) as refused:
            self.make_loop(project, mapping, document, tampered).compile()
        self.assertEqual(refused.exception.code, "owner_key_lineage")
        self.assertIn("a bare pin does not re-root the ledger", refused.exception.detail)
        self.assertEqual(loop.autonomy_events(tampered), [], "nothing was logged")
        self.assertEqual(len(tampered.events()), before, "the ledger was not written")
        self.assertFalse((project / ".syberlabs" / "contracts").exists(), "no contract was written")

    def test_a_map_without_owner_approval_is_refused(self):
        project, base = self.project()
        home = self.home()
        document = promoted_spec(project, home)
        mapping = fixture_map(document, base)
        run = self.make_loop(project, mapping, document, home)
        with self.assertRaises(Rejected) as refused:
            run.compile()
        self.assertEqual(refused.exception.code, "map_unapproved")
        self.assertEqual(loop.autonomy_events(home), [], "nothing was logged")
        featuremap.approve(mapping, OWNER)
        mapping["features"][0]["depends_on"] = ["F2"]
        run = self.make_loop(project, mapping, document, home)
        with self.assertRaises(Rejected) as refused:
            run.compile()
        self.assertEqual(refused.exception.code, "map_unapproved", "an edit after approval leaves the approval stale")
        featuremap.approve(mapping, OWNER)
        run = self.make_loop(project, mapping, document, home)
        with self.assertRaises(Rejected) as refused:
            run.compile()
        self.assertEqual(refused.exception.code, "map_invalid")
        self.assertIn("cycle", refused.exception.detail)

    def test_the_envelope_stops_the_loop(self):
        run, _, _, _ = self.ready_loop(calls_total=2)
        summary = run.run()
        self.assertEqual(summary["stopped"], "envelope_exhausted")
        self.assertEqual(summary["ready_for_owner_merge"], ["F1", "F3"])
        self.assertEqual(summary["features"]["F2"]["state"], "open", "F2 was eligible and waits for a new envelope")
        self.assertEqual(summary["envelope"]["calls_left"], 0)
        self.assertEqual(run.spend.totals()["stopped"], "envelope_exhausted")
        self.assertEqual([line["kind"] for line in run.spend.lines()], ["reserve", "settle", "reserve", "settle", "stop"])

    def test_two_maps_in_one_home_keep_their_own_envelopes(self):
        """One file per envelope digest: a second map (or a second grant) in the same factory home starts
        with its own spend and no inherited stop; the same spent grant is refused, not silently re-run."""
        project_a, base = self.project("a")
        home = self.home()
        document = promoted_spec(project_a, home)
        first = fixture_map(document, base)
        featuremap.approve(first, OWNER)
        run_a = self.make_loop(project_a, first, document, home)
        self.assertEqual(run_a.run()["stopped"], "nothing_eligible")
        project_b, base_b = self.project("b")
        self.assertEqual(base_b, base, "the fixture's base commit is deterministic")
        second = fixture_map(document, base, id="order-markers.m2")
        featuremap.approve(second, OWNER)
        run_b = self.make_loop(project_b, second, document, home)
        self.assertNotEqual(run_b.spend.path, run_a.spend.path)
        summary = run_b.run()
        self.assertEqual(summary["stopped"], "nothing_eligible")
        self.assertEqual(summary["ready_for_owner_merge"], ["F1", "F2", "F3"], "the second map built; it inherited no stop and no spend")
        self.assertEqual(summary["envelope"]["calls"], 3)
        self.assertEqual([line["kind"] for line in run_b.spend.lines()], ["reserve", "settle"] * 3 + ["stop"])
        self.assertEqual(len(notes(home.events(), "loop_stopped")), 2)
        again = self.make_loop(project_a, first, document, home)
        with self.assertRaises(Rejected) as refused:
            again.run()
        self.assertEqual(refused.exception.code, "envelope_stopped", "the first grant is spent; a new run needs a new envelope")
        self.assertEqual(len(notes(home.events(), "loop_stopped")), 2, "a refused re-run writes nothing")
        self.assertEqual([line["kind"] for line in run_a.spend.lines()].count("stop"), 1)

    def test_await_merge_leaves_a_dependent_waiting_for_the_owner(self):
        run, _, _, _ = self.ready_loop(map={"integration": "await_merge"})
        summary = run.run()
        self.assertEqual(summary["ready_for_owner_merge"], ["F1", "F3"])
        self.assertEqual(summary["features"]["F2"]["state"], "waiting_owner_merge")
        self.assertEqual(summary["stopped"], "nothing_eligible")

    def test_a_failing_candidate_is_refused_by_the_kernel_and_the_feature_is_parked(self):
        run, _, _, _ = self.ready_loop(attempts=1, mode="--break")
        summary = run.run()
        self.assertEqual(summary["ready_for_owner_merge"], [])
        self.assertEqual({row["state"] for row in summary["features"].values()} - {"open"}, {"parked"})
        self.assertTrue(summary["features"]["F1"]["last_reason"].startswith("candidate_check_failed:"), summary["features"]["F1"])
        self.assertIn(summary["features"]["F1"]["last_reason"].split(":")[1], ("tests", "hidden.f1.marker"), "both checks fail; one is named")
        self.assertEqual(summary["features"]["F2"]["state"], "open", "F2 waits on F1 and never started")
        events = loop.autonomy_events(run.ledger)
        questions = [(e["body"]["question"], e["body"]["answer"]["value"]) for e in notes(events, "policy_proposal") if e["body"]["feature"] == "F1"]
        self.assertEqual(questions[-2:], [("worth_continuing", "stop"), ("after_failure", "park")])
        self.assertEqual(summary["envelope"]["calls"], 2, "one call per parked feature; the envelope counted both")

    def test_retry_escalate_arm_runs_the_stronger_arm_on_the_next_attempt(self):
        """The recorded decision is the one that happens: after ``retry_escalate_arm`` the next ``proposer_arm``
        offers only the arms ranked above the one that failed, and the attempt runs on that arm."""
        arms = [{"id": "cheap", "tier_rank": 0, "revision": "1"}, {"id": "strong", "tier_rank": 1, "revision": "1"}]
        run, _, _, _ = self.ready_loop(attempts=3, mode="--break", arms=arms)
        summary = run.run()
        self.assertEqual(summary["features"]["F1"]["state"], "parked")
        self.assertEqual(summary["features"]["F1"]["attempts"], 3)
        events = loop.autonomy_events(run.ledger)
        f1 = lambda note: [e["body"] for e in notes(events, note) if e["body"].get("feature") == "F1"]
        self.assertEqual([b["arm"] for b in f1("attempt_started")], ["cheap", "cheap", "strong"])
        self.assertEqual([b["arm"] for b in f1("envelope_reserved")], ["cheap", "cheap", "strong"])
        self.assertEqual([b["answer"]["value"] for b in f1("policy_proposal") if b["question"] == "after_failure"],
                         ["retry_same", "retry_escalate_arm", "park"])
        offered = [b["options"] for b in f1("policy_proposal") if b["question"] == "proposer_arm"]
        self.assertEqual(offered, [["cheap", "strong"], ["cheap", "strong"], ["strong"]], "after the escalation only the stronger arm is an option")
        choices = [b["options"] for b in f1("policy_proposal") if b["question"] == "after_failure"]
        self.assertNotIn("retry_escalate_arm", choices[2], "on the strongest arm there is nothing to escalate to")
        self.assertEqual([line["arm"] for line in run.spend.lines() if line["kind"] == "reserve" and line["feature"] == "F1"],
                         ["cheap", "cheap", "strong"])

    def test_a_candidate_outside_its_scope_never_reaches_accept(self):
        run, _, _, _ = self.ready_loop(attempts=2, mode="--outside")
        summary = run.run()
        self.assertEqual(summary["ready_for_owner_merge"], [])
        self.assertEqual(summary["features"]["F1"]["last_reason"], "candidate_out_of_scope")
        self.assertEqual(summary["features"]["F1"]["attempts"], 2, "retry_same once, then parked")
        self.assertEqual(summary["features"]["F1"]["state"], "parked")

    def test_the_kill_switch_stops_before_the_next_decision(self):
        switch = self.scratch / "stop-now"
        switch.write_text("")
        run, _, _, _ = self.ready_loop(kill_switch=str(switch))
        summary = run.run()
        self.assertEqual(summary["stopped"], "kill_switch")
        self.assertEqual(summary["ready_for_owner_merge"], [])
        self.assertEqual(summary["envelope"]["calls"], 0)

    def test_a_rejection_inside_run_still_ends_the_record(self):
        """A ``Rejected`` while running (here an answer outside the options) propagates, but not before the
        envelope has its ``stop`` line and the ledger its ``loop_stopped`` with the refusal's code."""
        class Rogue:
            name = "rogue"

            def decide(self, state, question):
                return policymod.Answer(question.key, "F9", self.name, "off the menu")

        run, _, _, _ = self.ready_loop(policy=Rogue())
        with self.assertRaises(Rejected) as refused:
            run.run()
        self.assertEqual(refused.exception.code, "policy_invalid")
        events = loop.autonomy_events(run.ledger)
        last = events[-1]["body"]
        self.assertEqual((last["note"], last["reason"]), ("loop_stopped", "policy_invalid"))
        self.assertIn("rogue answered 'F9'", last["refs"]["detail"])
        self.assertEqual([(line["kind"], line["reason"]) for line in run.spend.lines()], [("stop", "policy_invalid")])
        self.assertEqual(run.spend.totals()["stopped"], "policy_invalid")
        self.assertTrue(run.ledger.verify())

    def test_a_dirty_tree_refuses_the_stacked_start_and_leaves_the_owners_checkout_alone(self):
        """Stacking moves HEAD only for the kernel's start and puts it back; a tree with uncommitted tracked
        changes is refused before any checkout (``repository_dirty``), the hold stays counted, the record ends."""
        run, project, base, _ = self.ready_loop()
        (project / "fxapp" / "main.py").write_text((project / "fxapp" / "main.py").read_text(encoding="utf-8") + "# owner's unsaved work\n",
                                                   encoding="utf-8")
        with self.assertRaises(Rejected) as refused:
            run.run()
        self.assertEqual(refused.exception.code, "repository_dirty")
        self.assertEqual([fid for fid, row in run.state.items() if row["state"] == "ready_for_owner_merge"], ["F1", "F3"],
                         "the two unstacked features built; F2's stacked start was refused")
        self.assertEqual(git(project, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(git(project, "rev-parse", "HEAD"), base)
        self.assertIn("# owner's unsaved work", (project / "fxapp" / "main.py").read_text(encoding="utf-8"), "nothing was lost")
        self.assertEqual(git(project, "status", "--porcelain", "--untracked-files=no").split(), ["M", "fxapp/main.py"])
        totals = run.spend.totals()
        self.assertEqual((totals["stopped"], totals["attempts"], totals["unsettled"]), ("repository_dirty", 3, 1), "the refused attempt's hold counts")
        last = loop.autonomy_events(run.ledger)[-1]["body"]
        self.assertEqual((last["note"], last["reason"], last["states"]["F2"]), ("loop_stopped", "repository_dirty", "open"),
                         "refused before its thread started, F2 is still open for a clean tree and a new envelope")

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
