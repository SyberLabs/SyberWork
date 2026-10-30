"""The factory's v0 slice on the fixture app: Space, harness, ledger, spec, attestation, and the CLI end to end."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from syberlabs.ed25519 import generate
from syberlabs.errors import Rejected
from syberlabs.factory import attest, harness, ledger, space
from syberlabs.factory import cli as factory_cli
from syberlabs.factory import spec as specmod

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "factory_app"
DECLARED = json.loads((FIXTURE / "architecture.json").read_text(encoding="utf-8"))
ACTOR = {"kind": "human", "id": "tester"}


def indexed(root=FIXTURE, revision="fixture"):
    graph = space.index(root, revision=revision)
    space.map_components(graph, DECLARED)
    return graph


def file_components(graph):
    return {path: record["component"] for path, record in graph["files"].items()}


class Scratch(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="factory-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def record(self, home, revision="fixture", quantity=2, replay_of=None, graph=None, root=FIXTURE,
               entry="fxapp.main:run"):
        """One recording in a child interpreter, logged in ``home``. Returns (run id, outcome, report)."""
        graph = graph or indexed(root, revision)
        run_id = harness.new_run_id()
        report = harness.record(root, home.run_path(run_id), entry, file_components=file_components(graph),
                                fixture={"part": "p-104", "quantity": quantity}, run_id=run_id, revision=revision,
                                replay_of=home.run_path(replay_of) if replay_of else None, blobs=home.home / "blobs")
        self.assertNotEqual(report["status"], "killed", report)
        home.record_run(run_id)
        return run_id, report["outcome"], report


class SpaceTests(unittest.TestCase):
    def test_index_finds_files_symbols_and_edges(self):
        graph = indexed()
        self.assertEqual(sorted(graph["files"]), ["fxapp/__init__.py", "fxapp/effects.py", "fxapp/ingest.py",
                                                  "fxapp/main.py", "fxapp/rules.py", "fxapp/variants.py"])
        self.assertIn("s:fxapp/rules.py::decide", graph["symbols"])
        self.assertEqual(graph["symbols"]["s:fxapp/rules.py::decide"]["kind"], "function")
        kinds = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}
        self.assertIn(("f:fxapp/main.py", "f:fxapp/rules.py", "IMPORTS"), kinds)
        self.assertIn(("s:fxapp/main.py::run", "s:fxapp/ingest.py::load", "CALLS"), kinds)
        self.assertNotIn(("f:fxapp/main.py", "f:fxapp/__init__.py", "IMPORTS"), kinds,
                         "from pkg import module points at the module, not the package")
        self.assertEqual(graph["revision"], "fixture")

    def test_mapping_and_reflexion(self):
        graph = indexed()
        self.assertEqual(graph["files"]["fxapp/effects.py"]["component"], "effects")
        self.assertEqual(graph["unmapped"], [])
        report = space.reflexion(graph)
        self.assertEqual([(e["from"], e["to"]) for e in report["divergence"]], [("effects", "rules")])
        self.assertEqual(report["absence"], [{"from": "ingest", "to": "effects"}])
        self.assertEqual(report["drift"]["effects"]["score"], 1.0)
        self.assertEqual(report["drift"]["main"]["score"], 0.0)
        self.assertEqual(report["max_drift"], 1.0)
        via = report["divergence"][0]["via"]
        self.assertTrue(any(v["kind"] == "CALLS" and v["to"] == "s:fxapp/rules.py::label" for v in via))

    def test_globs(self):
        self.assertTrue(space._glob_to_regex("fxapp").match("fxapp/deep/file.py"))
        self.assertTrue(space._glob_to_regex("fxapp/**").match("fxapp/deep/file.py"))
        self.assertTrue(space._glob_to_regex("**/rules.py").match("fxapp/rules.py"))
        self.assertTrue(space._glob_to_regex("fxapp/*.py").match("fxapp/rules.py"))
        self.assertFalse(space._glob_to_regex("fxapp/*.py").match("fxapp/deep/rules.py"))
        self.assertFalse(space._glob_to_regex("other").match("fxapp/rules.py"))

    def test_last_matching_component_wins_and_unmapped_is_reported(self):
        graph = space.index(FIXTURE, revision="fixture")
        declared = {"nodes": [{"id": "all", "label": "All", "paths": ["fxapp"]},
                              {"id": "rules", "label": "Rules", "paths": ["fxapp/rules.py"]}],
                    "edges": [{"from": "all", "to": "rules"}]}
        space.map_components(graph, declared)
        self.assertEqual(graph["files"]["fxapp/rules.py"]["component"], "rules")
        self.assertEqual(graph["files"]["fxapp/main.py"]["component"], "all")
        graph = space.index(FIXTURE, revision="fixture")
        space.map_components(graph, {"nodes": [{"id": "rules", "label": "Rules", "paths": ["fxapp/rules.py"]}], "edges": []})
        self.assertEqual(len(graph["unmapped"]), 5)

    def test_repository_layering_has_no_drift(self):
        """The repository's own declaration (docs/architecture.declared.json) says syberwork depends on syberlabs
        and never the reverse; the derived graph agrees. This is test_package's invariant, read from the code."""
        declared = json.loads((ROOT / "docs" / "architecture.declared.json").read_text(encoding="utf-8"))
        graph = space.index(ROOT, paths=["syberlabs", "syberwork"], revision="checkout")
        space.map_components(graph, declared)
        report = space.reflexion(graph)
        self.assertEqual(report["divergence"], [], [(e["from"], e["to"], e["via"][:2]) for e in report["divergence"]])
        self.assertEqual(report["unmapped"], [])
        self.assertEqual(report["max_drift"], 0.0)
        observed = {(e["from"], e["to"]) for e in graph["depends_on"]}
        self.assertIn(("syberwork", "syberlabs"), observed)
        self.assertNotIn(("syberlabs", "syberwork"), observed)

    def test_diff_between_revisions(self):
        before = indexed()
        scratch = Path(tempfile.mkdtemp(prefix="factory-space-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        shutil.copytree(FIXTURE, scratch / "tree")
        rules = scratch / "tree" / "fxapp" / "rules.py"
        rules.write_text(rules.read_text(encoding="utf-8") + "\n\ndef extra(record, flag):\n    return flag\n", encoding="utf-8")
        after = indexed(scratch / "tree", "changed")
        report = space.diff(before, after)
        self.assertEqual(report["files"]["changed"], ["fxapp/rules.py"])
        self.assertEqual(report["symbols"]["added"], ["s:fxapp/rules.py::extra"])
        self.assertEqual(report["components"], ["rules"])
        self.assertNotEqual(space.graph_digest(before), space.graph_digest(after))
        self.assertEqual(space.graph_digest(before), space.graph_digest(indexed()), "the digest is stable")


class HarnessTests(Scratch):
    def test_record_spans_worldline_and_active_at(self):
        home = ledger.Ledger(self.scratch / "ledger")
        run_id, outcome, report = self.record(home)
        self.assertEqual(outcome["decision"], "order")
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["counts"], {"call": 7, "effect": 1, "marker": 1})
        records = home.load_run(run_id)
        self.assertEqual(records[0]["t"], "run")
        self.assertEqual(records[-1]["t"], "end")
        self.assertEqual(records[-1]["status"], "ok")
        path = [(component, fn) for _, component, fn in harness.worldline(records)]
        self.assertEqual(path[:4], [("main", "run"), ("ingest", "load"), ("rules", "normalize"), ("rules", "decide")])
        self.assertEqual(path[-1][0], "effect:http.post_order")
        spans = harness.spans(records)
        self.assertEqual([s for s in spans if s["inside_effect"]][0]["fn"], "post_order")
        active = harness.active_at(records, 3)
        self.assertEqual([s["fn"] for s in active], ["run", "load", "normalize"])
        self.assertTrue(all(s["end"] is not None for s in spans))

    def test_replay_matches_and_runs_no_effect(self):
        home = ledger.Ledger(self.scratch / "ledger")
        run_a, outcome_a, _ = self.record(home)
        run_b, outcome_b, replayer = self.record(home, replay_of=run_a)
        self.assertEqual(outcome_a, outcome_b, "the replay returns the journaled effect and marker")
        self.assertEqual(replayer["status"], "ok", replayer)
        self.assertEqual(replayer["counts"]["call"], 5, "post_order and label did not run on replay")
        report = harness.compare(home.load_run(run_a), home.load_run(run_b))
        self.assertTrue(report["equal"], report)
        self.assertEqual(home.load_run(run_b)[0]["replay_of"], run_a)

    def test_replay_divergence_is_detected(self):
        home = ledger.Ledger(self.scratch / "ledger")
        run_a, _, _ = self.record(home)
        run_c, _, replayer = self.record(home, replay_of=run_a, quantity=0)
        self.assertEqual(replayer["status"], "diverged", "the effect the journal holds was never asked for")
        self.assertEqual(home.load_run(run_c)[-1]["unconsumed_effects"], 1)
        report = harness.compare(home.load_run(run_a), home.load_run(run_c))
        self.assertFalse(report["equal"])
        self.assertEqual(report["first_divergence"]["before"], ("effects", "submit"))

        run_d, _, other = self.record(home, replay_of=run_a, entry="fxapp.variants:other")
        self.assertEqual(other["status"], "diverged")
        self.assertIn("asked for effect 'effect:http.other'; the journal has 'marker:time'", other["error"])
        self.assertEqual(home.load_run(run_d)[-1]["status"], "diverged")

    def test_large_payload_goes_to_a_blob_and_replays(self):
        home = ledger.Ledger(self.scratch / "ledger")
        big = {"rows": ["x" * 100] * 1000}
        run_a, outcome, _ = self.record(home, entry="fxapp.variants:big")
        self.assertEqual(outcome, big)
        effect = [r for r in home.load_run(run_a) if r["t"] == "effect"][0]
        self.assertIn("blob", effect)
        self.assertTrue((home.home / "blobs" / effect["blob"]).exists())
        _, replayed, _ = self.record(home, replay_of=run_a, entry="fxapp.variants:big")
        self.assertEqual(replayed, big)

    def test_non_json_result_is_kept_as_repr(self):
        home = ledger.Ledger(self.scratch / "ledger")
        run_a, outcome, _ = self.record(home, entry="fxapp.variants:odd")
        self.assertEqual(outcome, "{1, 2}", "a non-JSON outcome comes back from the child as its repr")
        effect = [r for r in home.load_run(run_a) if r["t"] == "effect"][0]
        self.assertFalse(effect["json"])
        self.assertEqual(effect["payload"], "{1, 2}")

    def test_chrome_export_and_script_entry(self):
        home = ledger.Ledger(self.scratch / "ledger")
        run_id, _, _ = self.record(home)
        trace = harness.chrome_trace(home.load_run(run_id))
        self.assertEqual(trace["traceEvents"][0]["name"], "run")
        self.assertEqual(trace["traceEvents"][0]["cat"], "main")
        self.assertEqual(trace["metadata"]["run_id"], run_id)
        with self.assertRaises(Rejected):
            harness.current()
        script = self.scratch / "script.py"
        script.write_text("import sys\nfrom syberlabs.factory.harness import current\nctx = current()\n"
                          "print(ctx.marker('answer', 42))\nsys.exit(0)\n", encoding="utf-8")
        report = harness.record(self.scratch, self.scratch / "s.jsonl", str(script))
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["stdout"].strip(), "42")
        self.assertEqual(report["counts"]["marker"], 1)
        records = harness.read_journal(self.scratch / "s.jsonl")
        self.assertEqual([r["t"] for r in records], ["run", "call", "marker", "return", "end"])
        self.assertEqual(records[1]["fn"], "<module>", "a script's own body is its one span")
        with self.assertRaises(Rejected):
            harness.record(self.scratch, self.scratch / "t.jsonl", "nope.py")
        failing = self.scratch / "failing.py"
        failing.write_text("raise ValueError('boom')\n", encoding="utf-8")
        report = harness.record(self.scratch, self.scratch / "f.jsonl", str(failing))
        self.assertEqual(report["status"], "error")
        self.assertIn("ValueError: boom", report["error"])
        self.assertEqual(harness.read_journal(self.scratch / "f.jsonl")[-1]["status"], "error")


class LedgerTests(Scratch):
    def test_chain_verifies_and_detects_tampering(self):
        home = ledger.Ledger(self.scratch / "ledger")
        self.assertTrue(home.verify())
        graph = indexed()
        home.record_space(graph)
        home.append("note", {"text": "hello"})
        self.assertTrue(home.verify())
        self.assertEqual([e["seq"] for e in home.events()], [1, 2])
        lines = home.log.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["body"]["files"] = 99
        home.log.write_text("\n".join([json.dumps(first, sort_keys=True, separators=(",", ":"))] + lines[1:]) + "\n",
                            encoding="utf-8")
        self.assertFalse(home.verify())
        with self.assertRaises(Rejected):
            home.append("note", {"text": "after tampering"})
        with self.assertRaises(Rejected):
            ledger.Ledger(self.scratch / "other").append("not_a_kind", {})

    def test_projection_and_queries(self):
        home = ledger.Ledger(self.scratch / "ledger")
        graph = indexed()
        home.record_space(graph)
        run_a, _, _ = self.record(home, graph=graph)
        run_b, _, _ = self.record(home, graph=graph, revision="second")
        counts = home.project()
        self.assertEqual(counts["runs"], 2)
        self.assertEqual(counts["components"], 4)
        self.assertEqual(counts["cells"], 14)
        self.assertEqual([s["component"] for s in home.worldline(run_a)][:3], ["main", "ingest", "rules"])
        self.assertEqual([s["fn"] for s in home.active_at(run_a, 10)], ["run", "submit"])
        cells = home.cells_for("rules")
        self.assertEqual({c["revision"] for c in cells}, {"fixture", "second"})
        self.assertEqual(sorted({c["fn"] for c in cells}), ["decide", "label", "normalize"])
        self.assertEqual(home.components_at("fixture"), ["effects", "ingest", "main", "rules"])
        self.assertEqual(home.latest_space()["revision"], "fixture")
        described = home.describe()
        self.assertEqual((described["spaces"], described["runs"], described["verified"]), (1, 2, True))
        with self.assertRaises(Rejected):
            home.load_run("ffff")
        with self.assertRaises(Rejected):
            home.run_path("../escape")


class SpecTests(unittest.TestCase):
    def spec(self):
        return specmod.new_spec("order-path", "ordering", [
            {"req_id": "R1", "text": "When quantity is positive, the system shall post one order.",
             "scenarios": [{"given": "quantity 2", "when": "run", "then": "one http.post_order effect"}]}],
            ["ingest", "rules", "effects"], author=ACTOR)

    def test_lifecycle(self):
        document = self.spec()
        graph = indexed()
        self.assertEqual(document["state"], "draft")
        with self.assertRaises(Rejected):
            specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"])
        specmod.transition(document, "revise", ACTOR, delta={"added": [{"req_id": "R2", "text": "Reject zero quantity."}]})
        self.assertEqual(document["version"], 2)
        self.assertEqual([r["req_id"] for r in document["requirements"]], ["R1", "R2"])
        specmod.transition(document, "validate", ACTOR, space=graph)
        self.assertEqual(document["state"], "candidate")
        with self.assertRaises(Rejected):
            specmod.transition(document, "promote", ACTOR)
        specmod.transition(document, "promote", {"kind": "model", "id": "reviewer"}, verdict_ids=["v1"])
        specmod.transition(document, "canonize", ACTOR)
        specmod.transition(document, "retire", ACTOR, superseded_by="order-path:3")
        self.assertEqual([h["operation"] for h in document["history"]],
                         ["revise", "validate", "promote", "canonize", "retire"])
        self.assertEqual(document["supersedes"], "order-path:3")
        with self.assertRaises(Rejected):
            specmod.transition(document, "bogus", ACTOR)

    def test_validation_findings(self):
        document = specmod.new_spec("x", "y", [{"req_id": "R1", "text": " "}], ["nowhere"])
        findings = specmod.validate(document, indexed())
        self.assertTrue(any("empty text" in f for f in findings))
        self.assertTrue(any("nowhere" in f for f in findings))
        with self.assertRaises(Rejected):
            specmod.new_spec("x", "y", [{"req_id": "R1", "text": "t", "evidence_kinds": ["vibes"]}], ["main"])
        with self.assertRaises(Rejected):
            specmod.new_spec("x", "y", [{"req_id": "R1", "text": "t"}, {"req_id": "R1", "text": "u"}], ["main"])

    def test_footprint_and_ticket(self):
        document = self.spec()
        graph = indexed()
        with self.assertRaises(Rejected):
            specmod.ticket_from_spec(document, "R1")
        specmod.transition(document, "validate", ACTOR, space=graph)
        specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"])
        with self.assertRaises(Rejected):
            specmod.ticket_from_spec(document, "R1")
        footprint = specmod.resolve_footprint(document, graph)
        self.assertEqual(footprint["files"], ["fxapp/effects.py", "fxapp/ingest.py", "fxapp/rules.py"])
        ticket = specmod.ticket_from_spec(document, "R1", baseline_run="abc", human_owner="tester")
        self.assertEqual(ticket["footprint"]["components"], ["effects", "ingest", "rules"])
        self.assertEqual(ticket["exit_criteria"], ["evidence:trace_diff"])
        self.assertEqual(ticket["acceptance_criteria"][0]["then"], "one http.post_order effect")
        with self.assertRaises(Rejected):
            specmod.ticket_from_spec(document, "R9")
        document["footprint_hint"] = ["ghost"]
        with self.assertRaises(Rejected):
            specmod.resolve_footprint(document, graph)


class AttestTests(Scratch):
    def test_trace_diff_statement_policy_and_signature(self):
        home = ledger.Ledger(self.scratch / "ledger")
        run_a, _, _ = self.record(home, revision="base")
        run_b, _, _ = self.record(home, revision="cand")
        run_c, _, _ = self.record(home, revision="cand", quantity=0)
        footprint = {"components": ["ingest", "rules", "effects"]}
        same = attest.trace_diff(home.load_run(run_a), home.load_run(run_b), footprint)
        self.assertTrue(same["diff_empty"])
        self.assertEqual((same["baseline_run"], same["candidate_run"]), (run_a, run_b))
        changed = attest.trace_diff(home.load_run(run_a), home.load_run(run_c), footprint)
        self.assertFalse(changed["diff_empty"])
        self.assertEqual(changed["first_divergence"]["before"], ["effects", "submit"] if isinstance(
            changed["first_divergence"]["before"], list) else ("effects", "submit"))
        spec_ref = {"spec_id": "order-path", "version": 1, "req_ids": ["R1"]}
        builder = {"agent": "test", "model": None, "session": None}
        promoted = attest.statement(subject_commit="cand", repository="fx", spec_ref=spec_ref, footprint=footprint,
                                    evidence={"trace_diff": same}, builder=builder, ticket="t1")
        self.assertTrue(promoted["predicate"]["policy"]["promote"])
        refused = attest.statement(subject_commit="cand", repository="fx", spec_ref=spec_ref, footprint=footprint,
                                   evidence={"trace_diff": changed}, builder=builder, ticket="t1")
        self.assertFalse(refused["predicate"]["policy"]["promote"])
        labeled = attest.statement(subject_commit="cand", repository="fx", spec_ref=spec_ref, footprint=footprint,
                                   evidence={"trace_diff": changed}, builder=builder, behavior_change_label=True)
        self.assertTrue(labeled["predicate"]["policy"]["promote"])
        strict = attest.policy(promoted["predicate"], require_tests=True)
        self.assertFalse(strict["promote"])
        with_tests = dict(promoted["predicate"], evidence={"trace_diff": same, "tests": {"full_suite": True, "fail": 0},
                                                            "mutation": {"score": 0.9}})
        self.assertTrue(attest.policy(with_tests, require_tests=True, mutation_threshold=0.8)["promote"])
        self.assertFalse(attest.policy(with_tests, mutation_threshold=0.95)["promote"])
        with self.assertRaises(Rejected):
            attest.statement(subject_commit="", repository="fx", spec_ref=spec_ref, footprint=None,
                             evidence={}, builder=builder)
        seed, public = generate()
        envelope = attest.sign(promoted, seed)
        self.assertEqual(attest.verify(envelope, public), promoted)
        self.assertIsNone(attest.verify(envelope, generate()[1]))
        self.assertEqual(attest.unwrap(envelope), promoted)
        self.assertEqual(attest.unwrap(promoted), promoted)
        event = home.record_attestation(envelope)
        self.assertEqual(event["body"]["signed"], True)
        self.assertEqual(event["body"]["promote"], True)
        self.assertEqual(event["body"]["subject"], ["git:fx@cand"])
        self.assertTrue((home.home / "attestations" / f"{event['body']['attestation_digest']}.json").exists())
        self.assertTrue(home.verify())


class CliTests(Scratch):
    def run_cli(self, *argv, cwd):
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(cwd)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = factory_cli.main(list(argv))
        finally:
            os.chdir(here)
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end(self):
        tree = self.scratch / "tree"
        shutil.copytree(FIXTURE, tree)
        (tree / "fixture.json").write_text('{"part": "p-104", "quantity": 2}', encoding="utf-8")
        spec_path = tree / "spec.json"
        spec_path.write_text(json.dumps(specmod.new_spec("order-path", "ordering", [
            {"req_id": "R1", "text": "When quantity is positive, the system shall post one order.",
             "scenarios": [{"given": "quantity 2", "when": "run", "then": "one http.post_order effect"}]}],
            ["ingest", "rules", "effects"], author=ACTOR)), encoding="utf-8")
        code, out, err = self.run_cli("index", "--declared", "architecture.json", "--revision", "base", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("divergent  effects -> rules", out)
        code, out, _ = self.run_cli("drift", "--declared", "architecture.json", "--max", "0.5", cwd=tree)
        self.assertEqual(code, 1, "drift over the threshold fails the command")
        code, out, _ = self.run_cli("drift", "--declared", "architecture.json", "--json", cwd=tree)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["max_drift"], 1.0)
        code, out, err = self.run_cli("record", "--entry", "fxapp.main:run", "--fixture", "fixture.json",
                                      "--revision", "base", cwd=tree)
        self.assertEqual(code, 0, err)
        run_a = out.split("recorded run ")[1].split()[0]
        code, out, _ = self.run_cli("show", run_a, cwd=tree)
        self.assertEqual(code, 0)
        self.assertIn("effect:http.post_order", out)
        code, out, _ = self.run_cli("at", run_a, "--seq", "10", cwd=tree)
        self.assertIn("submit", out)
        code, out, err = self.run_cli("replay", run_a, "--entry", "fxapp.main:run", "--fixture", "fixture.json",
                                      "--revision", "base", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("worldline matches the recording", out)
        code, out, err = self.run_cli("spec", "validate", "spec.json", cwd=tree)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("spec", "promote", "spec.json", "--verdict", "v1", "--actor", "model:reviewer", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("candidate -> promoted", out)
        code, out, err = self.run_cli("ticket", "spec.json", "--req", "R1", "--baseline", run_a, "--owner", "tester",
                                      "--out", "ticket.json", cwd=tree)
        self.assertEqual(code, 0, err)
        ticket = json.loads((tree / "ticket.json").read_text(encoding="utf-8"))
        self.assertEqual(ticket["footprint"]["components"], ["effects", "ingest", "rules"])
        code, out, err = self.run_cli("record", "--entry", "fxapp.main:run", "--fixture", "fixture.json",
                                      "--revision", "cand", cwd=tree)
        self.assertEqual(code, 0, err)
        run_b = out.split("recorded run ")[1].split()[0]
        seed, public = generate()
        os.environ["SYBERLABS_FACTORY_TEST_SEED"] = seed.hex()
        self.addCleanup(os.environ.pop, "SYBERLABS_FACTORY_TEST_SEED", None)
        code, out, err = self.run_cli("attest", "--baseline", run_a, "--candidate", run_b, "--ticket", "ticket.json",
                                      "--commit", "cand", "--seed-env", "SYBERLABS_FACTORY_TEST_SEED",
                                      "--out", "attestation.json", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("promote True", out)
        (tree / "key.pub").write_text(public.hex(), encoding="utf-8")
        code, out, err = self.run_cli("verify", "attestation.json", "--public", "key.pub", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("verified: git:tree@cand", out)
        (tree / "wrong.pub").write_text(generate()[1].hex(), encoding="utf-8")
        code, out, _ = self.run_cli("verify", "attestation.json", "--public", "wrong.pub", cwd=tree)
        self.assertEqual(code, 1)
        code, out, err = self.run_cli("export", run_a, "--chrome", "trace.json", "--view", "run.html", cwd=tree)
        self.assertEqual(code, 0, err)
        page = (tree / "run.html").read_text(encoding="utf-8")
        self.assertIn('"run_id": "' + run_a + '"', page)
        self.assertNotIn("/*__FACTORY_DATA__*/null", page)
        self.assertEqual(json.loads((tree / "trace.json").read_text(encoding="utf-8"))["metadata"]["run_id"], run_a)
        code, out, _ = self.run_cli("status", cwd=tree)
        self.assertIn("verified       True", out)
        self.assertIn("attestations   1", out)
        code, _, err = self.run_cli("show", "deadbeef", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("run_missing", err)
        code, _, err = self.run_cli("show", "../escape", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("run_id", err)


if __name__ == "__main__":
    unittest.main()


class BuildThreadCheck(Scratch):
    """The seam: a Build Thread contract names `factory check` as a host-run check. The candidate's evaluation then
    carries the trace diff's verdict and the attestation digest, under the existing admission rules."""

    def setUp(self):
        super().setUp()
        import subprocess
        import sys as _sys
        from syberlabs.build import Kit, default_contract
        self.Kit, self.default_contract = Kit, default_contract
        self.project = self.scratch / "project"
        shutil.copytree(FIXTURE, self.project)
        (self.project / "fixture.json").write_text('{"part": "p-104", "quantity": 2}', encoding="utf-8")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
        self.git = lambda *a: subprocess.run(["git", "-C", str(self.project), *a], check=True, capture_output=True,
                                             text=True, env=env).stdout.strip()
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "base")
        self.ledger_home = (self.scratch / "ledger").resolve()
        # The check is argv without a shell under a scrubbed environment, so it names the interpreter and this
        # checkout explicitly; an installed wheel would use `-m syberlabs.factory` instead.
        self.check_argv = [_sys.executable, "-c",
                           f"import sys; sys.path.insert(0, {str(ROOT)!r}); from syberlabs.factory.cli import main; "
                           f"sys.exit(main(['--home', {str(self.ledger_home)!r}, 'check', '--declared', 'architecture.json', "
                           f"'--entry', 'fxapp.main:run', '--fixture', 'fixture.json', '--max-drift', '1.0'] + sys.argv[1:]))"]

    def golden(self):
        home = ledger.Ledger(self.ledger_home)
        graph = space.index(self.project, revision=self.git("rev-parse", "HEAD"))
        space.map_components(graph, DECLARED)
        home.record_space(graph)
        run_id = harness.new_run_id()
        report = harness.record(self.project, home.run_path(run_id), "fxapp.main:run", file_components=file_components(graph),
                                fixture={"part": "p-104", "quantity": 2}, run_id=run_id, revision=graph["revision"])
        self.assertEqual(report["status"], "ok", report)
        home.record_run(run_id)
        home.set_baseline(run_id)
        return home, run_id

    def kit(self, extra_argv=()):
        contract = self.default_contract(self.project)
        contract["evolution"]["scope"] = {"paths": ["fxapp/"], "exclude": [], "max_files": 20, "max_diff_bytes": 200_000}
        contract["evolution"]["evaluation"] = {"checks": {"factory": {"argv": self.check_argv + list(extra_argv), "timeout_seconds": 300}},
                                               "required": ["factory"], "max_age_seconds": 3600, "max_output_bytes": 1_048_576}
        home = self.project / ".syberlabs"
        (home / "contracts").mkdir(parents=True, exist_ok=True)
        (home / "contracts" / f"{contract['id']}.v{contract['version']}.json").write_text(json.dumps(contract), encoding="utf-8")
        kit = self.Kit.local(home, self.project, actor="dev@example.test")
        self.addCleanup(kit.close)
        return kit

    def test_a_behavior_preserving_candidate_passes_and_a_changed_one_is_refused(self):
        home, golden = self.golden()
        kit = self.kit()
        thread = kit.start("Tidy the rules module", paths=["fxapp/"])
        rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
        [same] = thread.propose(changes={"fxapp/rules.py": rules.replace('"""Rules: pure decisions over a record."""',
                                                                           '"""Rules: pure decisions over one record."""')})
        verdict = thread.check(same.id)
        self.assertTrue(verdict.acceptable, verdict.summary())
        [check] = verdict.checks
        self.assertEqual((check.name, check.state), ("factory", "passed"))
        self.assertIn("trace diff empty", check.output_tail)
        self.assertIn("promote True", check.output_tail)
        attested = [e for e in home.events() if e["kind"] == "attested"]
        self.assertEqual(len(attested), 1)
        self.assertEqual(attested[0]["body"]["subject"], [f"git:project@{same.commit}"])
        self.assertTrue(attested[0]["body"]["promote"])
        self.assertIn(attested[0]["body"]["attestation_digest"][:16], check.output_tail)
        self.assertEqual(home.load_run(home.runs()[-1]["run_id"])[0]["revision"], same.commit,
                         "the check recorded the candidate's own commit")
        receipt = thread.accept(same.id)
        self.assertEqual(receipt.status, "succeeded", receipt.reason)

        other = kit.start("Lower the review threshold", paths=["fxapp/"])
        [changed] = other.propose(changes={"fxapp/rules.py": rules.replace('if record["quantity"] > 100:', 'if record["quantity"] > 1:')})
        verdict = other.check(changed.id)
        self.assertFalse(verdict.acceptable, verdict.summary())
        self.assertEqual(verdict.reason, "candidate_check_failed:factory")
        self.assertIn("NOT empty", verdict.checks[0].output_tail)
        self.assertIn("no behavior change was declared: refuse", verdict.checks[0].output_tail)
        receipt = other.accept(changed.id)
        self.assertEqual((receipt.status, receipt.reason), ("denied", "candidate_check_failed:factory"))
        last = [e for e in home.events() if e["kind"] == "attested"][-1]
        self.assertFalse(last["body"]["promote"])
        self.assertFalse(last["body"]["diff_empty"])
        self.assertTrue(home.verify())

    def test_drift_bound_refuses_an_undeclared_dependency(self):
        self.golden()
        kit = self.kit(extra_argv=["--max-drift", "0"])
        thread = kit.start("Tighten the layering", paths=["fxapp/"])
        rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
        [candidate] = thread.propose(changes={"fxapp/rules.py": rules + "\n"})
        verdict = thread.check(candidate.id)
        self.assertFalse(verdict.acceptable)
        self.assertIn("architecture drift 1.0 exceeds 0.0 (worst: effects): refuse", verdict.checks[0].output_tail)

    def test_drift_opens_tickets(self):
        home = ledger.Ledger(self.ledger_home)
        graph = space.index(self.project, revision="r1")
        space.map_components(graph, DECLARED)
        report = space.reflexion(graph)
        tickets = specmod.tickets_from_drift(report, "r1", threshold=0.0, human_owner="tester")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["footprint"]["components"], ["effects", "rules"])
        self.assertEqual(tickets[0]["exit_criteria"], ["evidence:drift"])
        self.assertEqual(tickets[0]["repro"]["divergent_edges"][0]["via"][0]["kind"], "IMPORTS")
        self.assertEqual(specmod.tickets_from_drift(report, "r1", threshold=1.0), [])
        report["unmapped"] = ["stray.py"]
        self.assertEqual(len(specmod.tickets_from_drift(report, "r1", threshold=1.0)), 1)
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(self.project)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = factory_cli.main(["--home", str(self.ledger_home), "drift", "--declared", "architecture.json",
                                         "--max", "0", "--ticket", str(self.scratch / "tickets")])
        finally:
            os.chdir(here)
        self.assertEqual(code, 1, err.getvalue())
        self.assertIn("ticket     ", out.getvalue())
        written = list((self.scratch / "tickets").glob("*.json"))
        self.assertEqual(len(written), 1)
        opened = [e for e in home.events() if e["kind"] == "ticket_opened"]
        self.assertEqual(opened[0]["body"]["source"], "drift")
        self.assertEqual(home.baseline(), None)
        with self.assertRaises(Rejected):
            home.set_baseline("deadbeef")

    def test_bootstrap_publishes_the_contract_and_a_thread_runs_the_check(self):
        """`factory bootstrap` on the base commit: golden, baseline, and a new contract version naming the check;
        a thread started afterwards runs it with no further setup."""
        import sys as _sys
        contract = self.default_contract(self.project)
        contract["evolution"]["scope"] = {"paths": ["fxapp/"], "exclude": [], "max_files": 20, "max_diff_bytes": 200_000}
        contract["evolution"]["evaluation"] = {"checks": {"noop": {"argv": [_sys.executable, "-c", "pass"]}},
                                               "required": ["noop"], "max_age_seconds": 3600}
        contracts = self.project / ".syberlabs" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "repo-change.v1.json").write_text(json.dumps(contract), encoding="utf-8")
        # The published argv says `-m syberlabs.factory`; this checkout is not installed, so the test names a
        # launcher that puts it on sys.path first and otherwise behaves as the interpreter.
        launcher = self.scratch / "python-with-checkout.py"
        launcher.write_text(f"import runpy, sys\nsys.path.insert(0, {str(ROOT)!r})\n"
                            "assert sys.argv[1] == '-m'\nsys.argv = sys.argv[2:]\nrunpy.run_module(sys.argv[0], run_name='__main__', alter_sys=True)\n",
                            encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(self.project)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = factory_cli.main(["--home", str(self.ledger_home), "bootstrap", "--declared", "architecture.json",
                                         "--entry", "fxapp.main:run", "--fixture", "fixture.json", "--max-drift", "1.0",
                                         "--python", _sys.executable])
        finally:
            os.chdir(here)
        self.assertEqual(code, 0, err.getvalue() + out.getvalue())
        published = contracts / "repo-change.v2.json"
        self.assertTrue(published.exists(), out.getvalue())
        document = json.loads(published.read_text(encoding="utf-8"))
        self.assertEqual(document["evolution"]["evaluation"]["required"], ["factory", "noop"])
        argv = document["evolution"]["evaluation"]["checks"]["factory"]["argv"]
        self.assertEqual(argv[1:3], ["-m", "syberlabs.factory"])
        self.assertIn(str(self.ledger_home), argv)
        home = ledger.Ledger(self.ledger_home)
        self.assertIsNotNone(home.baseline())
        # Swap the interpreter for the launcher so the check finds this checkout, then run a thread on v2.
        argv[0:1] = [_sys.executable, str(launcher)]
        published.write_text(json.dumps(document), encoding="utf-8")
        kit = self.Kit.local(self.project / ".syberlabs", self.project, actor="dev@example.test")
        self.addCleanup(kit.close)
        thread = kit.start("Tidy rules", paths=["fxapp/"])
        self.assertEqual(thread.status()["contract"], "repo-change.v2")
        rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
        [candidate] = thread.propose(changes={"fxapp/rules.py": rules + "\n"})
        verdict = thread.check(candidate.id)
        self.assertTrue(verdict.acceptable, verdict.summary())
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("factory", "passed"), ("noop", "passed")])
        self.assertEqual(thread.accept(candidate.id).status, "succeeded")
