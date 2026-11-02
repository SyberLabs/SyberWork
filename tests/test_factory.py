"""The factory's v0 slice on the fixture app: Space, harness, ledger, spec, attestation, and the CLI end to end."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from syberlabs.ed25519 import generate
from syberlabs.errors import Rejected
from syberlabs.factory import attest, features, harness, independence, ledger, space
from syberlabs.factory import cli as factory_cli
from syberlabs.factory import spec as specmod

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "factory_app"
DECLARED = json.loads((FIXTURE / "architecture.json").read_text(encoding="utf-8"))
ACTOR = {"kind": "human", "id": "tester"}
OWNER_SEED, OWNER_PUBLIC = generate()


def approved(document, operation, verdict_ids=(), approver=ACTOR, seed=OWNER_SEED, public=OWNER_PUBLIC):
    """An owner approval of ``operation`` on ``document`` as it is now, signed and verified: what ``transition``
    takes for a gated operation."""
    payload = specmod.approval_payload(document, operation, approver, verdict_ids=list(verdict_ids))
    envelope = specmod.sign_approval(payload, seed)
    return specmod.verify_approval(envelope, public, document, operation, verdict_ids=list(verdict_ids))


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
                                                  "fxapp/main.py", "fxapp/rules.py", "fxapp/variants.py", "proposers.py"])
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
        self.assertEqual(len(graph["unmapped"]), 6)

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
        self.assertIn(("syberwork", "syberlabs-admission"), observed)
        self.assertFalse([e for e in observed if e[0].startswith("syberlabs") and e[1] == "syberwork"],
                         "nothing in syberlabs depends on syberwork")
        self.assertGreaterEqual(len(graph["components"]), 11)
        self.assertNotIn(("syberlabs-primitives", "syberlabs-admission"), observed, "primitives stay at the bottom")

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
        self.assertEqual(report["counts"], {"call": 7, "effect": 1, "marker": 1, "io": 0})
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
        self.assertEqual(report["first_divergence"]["before"], ("effects", None), "component level by default")
        strict = harness.compare(home.load_run(run_a), home.load_run(run_c), level="function")
        self.assertEqual(strict["first_divergence"]["before"], ("effects", "submit"))
        with self.assertRaises(Rejected):
            harness.compare(home.load_run(run_a), home.load_run(run_c), level="line")

        run_d, _, other = self.record(home, replay_of=run_a, entry="fxapp.variants:other")
        self.assertEqual(other["status"], "diverged")
        self.assertIn("asked for effect 'effect:http.other'; the journal has 'marker:time'", other["error"])
        self.assertEqual(home.load_run(run_d)[-1]["status"], "diverged")

    def test_a_refactor_inside_a_component_is_not_a_change_at_component_level(self):
        home = ledger.Ledger(self.scratch / "ledger")
        run_a, _, _ = self.record(home)
        tree = self.scratch / "refactored"
        shutil.copytree(FIXTURE, tree)
        rules = tree / "fxapp" / "rules.py"
        rules.write_text(rules.read_text(encoding="utf-8").replace(
            'def decide(record):\n    if record["quantity"] <= 0:',
            'def _zero(record):\n    return record["quantity"] <= 0\n\n\ndef decide(record):\n    if _zero(record):'), encoding="utf-8")
        graph = indexed(tree, "refactor")
        self.assertIn("s:fxapp/rules.py::_zero", graph["symbols"])
        run_b, outcome, _ = self.record(home, root=tree, graph=graph, revision="refactor")
        self.assertEqual(outcome["decision"], "order")
        loose = harness.compare(home.load_run(run_a), home.load_run(run_b))
        strict = harness.compare(home.load_run(run_a), home.load_run(run_b), level="function")
        self.assertTrue(loose["equal"], loose)
        self.assertFalse(strict["equal"])
        self.assertEqual(strict["first_divergence"]["after"], ("rules", "_zero"))
        evidence = attest.trace_diff(home.load_run(run_a), home.load_run(run_b), None)
        self.assertTrue(evidence["diff_empty"])
        self.assertFalse(evidence["function_level_equal"])

    def test_a_subprocess_is_observed_as_io_and_counts_in_the_diff(self):
        home = ledger.Ledger(self.scratch / "ledger")
        script = self.scratch / "shell.py"
        script.write_text("import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
                          "subprocess.run([sys.executable, '-c', sys.argv[1] if len(sys.argv) > 1 else 'pass'], check=True)\n",
                          encoding="utf-8")
        run_a = harness.new_run_id()
        report = harness.record(self.scratch, home.run_path(run_a), str(script), run_id=run_a)
        self.assertEqual((report["status"], report["counts"]["io"]), ("ok", 2), report)
        home.record_run(run_a)
        records = home.load_run(run_a)
        io_records = [r for r in records if r["t"] == "io"]
        self.assertEqual(io_records[0]["name"], "subprocess")
        self.assertEqual(io_records[0]["payload"]["argv"][1:], ["-c", "pass"])
        path = harness.worldline(records)
        self.assertEqual([h for _, h, _ in path if str(h).startswith("io:")], ["io:subprocess", "io:subprocess"])
        run_b = harness.new_run_id()
        harness.record(self.scratch, home.run_path(run_b), str(script), run_id=run_b)
        self.assertTrue(harness.compare(records, harness.read_journal(home.run_path(run_b)))["equal"])
        quiet = harness.new_run_id()
        harness.record(self.scratch, home.run_path(quiet), str(script), run_id=quiet, observe=())
        self.assertEqual([r["t"] for r in harness.read_journal(home.run_path(quiet)) if r["t"] == "io"], [])
        home.record_run(run_b)
        self.assertEqual(home.project()["effects"], 4)

    def test_a_forging_candidate_is_refused_and_an_honest_one_passes(self):
        """S2: the candidate's process cannot pass by printing a report, by writing journal lines around the
        recorder, or by writing its own clean end; the honest program still passes. The child is never told the
        ledger's path, and the journal it wrote is sealed under a key the parent holds."""
        home = ledger.Ledger(self.scratch / "ledger")
        graph = indexed()
        honest, outcome, report = self.record(home, graph=graph)
        self.assertEqual((report["status"], outcome["decision"]), ("ok", "order"))
        records = home.load_run(honest)
        self.assertTrue(records[0]["sealed"])
        self.assertTrue(all("m" in r for r in records), "every record carries its seal")
        forged = harness.new_run_id()
        report = harness.record(FIXTURE, home.run_path(forged), "fxapp.variants:forge_report", file_components=file_components(graph),
                                fixture={"part": "p-104", "quantity": 2}, run_id=forged, revision="fixture", blobs=home.home / "blobs")
        self.assertEqual(report["status"], "error", report)
        self.assertTrue(any(s in report["error"] for s in ("no end record", "journal is empty", "torn")), report["error"])
        self.assertFalse(home.run_path(forged).exists(), "a journal the recorder never closed is not delivered")
        self.assertIsNone(report["outcome"], "the printed line is stdout, not a report")
        self.assertIn('"status": "ok"', report["stdout"])
        planted = harness.new_run_id()
        report = harness.record(FIXTURE, home.run_path(planted), "fxapp.variants:forge_journal", file_components=file_components(graph),
                                fixture={}, run_id=planted, revision="fixture", blobs=home.home / "blobs")
        self.assertEqual(report["status"], "error", report)
        self.assertIn("journal_unsealed", report["error"])
        self.assertFalse(home.run_path(planted).exists(), "an unsealed journal is not delivered to the ledger")
        false_end = harness.new_run_id()
        report = harness.record(FIXTURE, home.run_path(false_end), "fxapp.variants:forge_end", file_components=file_components(graph),
                                fixture={}, run_id=false_end, revision="fixture", blobs=home.home / "blobs")
        self.assertEqual(report["status"], "error", report)
        self.assertIn("journal_unsealed", report["error"])
        self.assertFalse(home.run_path(false_end).exists())
        # The seal itself, with a known key: intact verifies; an edited line, an unsealed line and a torn tail do not.
        sealed = self.scratch / "sealed.jsonl"
        with harness.Recorder(FIXTURE, sealed, key="ab" * 32) as recorder:
            recorder.io("x", {"a": 1})
            recorder.io("y", {"b": 2})
        self.assertIsNone(harness.verify_journal(sealed, "ab" * 32))
        self.assertIn("line 1", harness.verify_journal(sealed, "cd" * 32))
        lines = sealed.read_text(encoding="utf-8").splitlines()
        edited = lines[:1] + [lines[1].replace('"name":"x"', '"name":"z"', 1)] + lines[2:]
        sealed.write_text("\n".join(edited) + "\n", encoding="utf-8")
        self.assertIn("line 2", harness.verify_journal(sealed, "ab" * 32))
        sealed.write_text("\n".join(lines) + '\n{"t":"marker","seq":9}\n', encoding="utf-8")
        self.assertIn("carries no seal", harness.verify_journal(sealed, "ab" * 32))
        sealed.write_text("\n".join(lines) + "\n" + lines[-1][:20], encoding="utf-8")
        self.assertIn("torn", harness.verify_journal(sealed, "ab" * 32))

    def test_the_child_and_the_tests_runner_run_without_the_hosts_seed(self):
        """M2 of the #164 verification: the candidate's process and the suite get the Build Thread's scrubbed
        environment, never the host's whole one; the host's signing seed and a --seed-env variable are absent."""
        from unittest import mock
        root = self.scratch / "probe"
        root.mkdir()
        (root / "envprobe.py").write_text(
            "import os\n\ndef run(ctx, fixture):\n"
            "    return {'seed': len(os.environ.get('SYBERLABS_FACTORY_PROBE_SEED', '')),"
            " 'default': len(os.environ.get('SYBERLABS_CERTIFICATE_SEED', '')),"
            " 'path': bool(os.environ.get('PATH')), 'home': os.environ.get('HOME')}\n", encoding="utf-8")
        home = ledger.Ledger(self.scratch / "ledger")
        with mock.patch.dict(os.environ, {"SYBERLABS_FACTORY_PROBE_SEED": "a" * 64, "SYBERLABS_CERTIFICATE_SEED": "b" * 64}):
            run_id = harness.new_run_id()
            report = harness.record(root, home.run_path(run_id), "envprobe:run", run_id=run_id, revision="probe",
                                    strip_env=("SYBERLABS_FACTORY_PROBE_SEED",))
            self.assertEqual(report["status"], "ok", report)
            self.assertEqual((report["outcome"]["seed"], report["outcome"]["default"], report["outcome"]["path"]), (0, 0, True))
            self.assertNotEqual(report["outcome"]["home"], os.environ.get("HOME"), "HOME is the scratch home, not the host's")
            probe = json.dumps([sys.executable, "-c", "import os, sys; sys.exit(len(os.environ.get('SYBERLABS_FACTORY_PROBE_SEED', ''))"
                                                       " + len(os.environ.get('SYBERLABS_CERTIFICATE_SEED', '')))"])
            tests = factory_cli._run_tests(root, probe, 60, strip_env=("SYBERLABS_FACTORY_PROBE_SEED",))
            self.assertEqual((tests["exit_code"], tests["full_suite"]), (0, True), tests["output_tail"])

    def test_a_killed_run_delivers_no_journal(self):
        """L1 of the #164 verification: a run stopped at its timeout has no end record and its journal does not
        reach the ledger's runs/."""
        root = self.scratch / "sleeper"
        root.mkdir()
        (root / "sleep.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        home = ledger.Ledger(self.scratch / "ledger")
        run_id = harness.new_run_id()
        report = harness.record(root, home.run_path(run_id), str(root / "sleep.py"), run_id=run_id, revision="sleep", timeout=2)
        self.assertEqual(report["status"], "killed", report)
        self.assertIn("no end after", report["error"])
        self.assertFalse(home.run_path(run_id).exists(), "a killed run's journal is not delivered")
        self.assertEqual(list((home.home / "runs").iterdir()), [])

    def test_scrub(self):
        self.assertEqual(harness.scrub("ghp_abcdefghijklmnopqrstuvwxyz0123456789"), "<redacted>")
        self.assertEqual(harness.scrub("GITHUB_TOKEN=whatever"), "GITHUB_TOKEN=<redacted>")
        self.assertEqual(harness.scrub("http.extraheader=AUTHORIZATION: basic abc"), "http.extraheader=<redacted>")
        self.assertEqual(harness.scrub("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"), "Authorization: Bearer <redacted>")
        self.assertEqual(harness.scrub("core.hooksPath=.githooks"), "core.hooksPath=.githooks")
        self.assertEqual(harness.scrub("--format=%(refname)"), "--format=%(refname)")
        self.assertEqual(harness.scrub_argv(["git", "-c", "http.extraheader=AUTHORIZATION: basic abc", "fetch"]),
                         ["git", "-c", "http.extraheader=<redacted>", "fetch"])
        self.assertEqual(harness.scrub_argv(["curl", "-H", "Authorization: Bearer x", "--token=abc", "--password", "p", "https://h/"]),
                         ["curl", "-H", "<redacted>", "--token=<redacted>", "--password", "<redacted>", "https://h/"])
        self.assertEqual(harness.scrub_text("a\nkey sk-ant-abcdefghijklmnop\nb", 100), "a\nkey <redacted>\nb")
        self.assertEqual(len(harness.scrub_text("x" * 5000, 100)), 100)

    def test_a_subprocess_argv_is_scrubbed_before_it_is_journaled(self):
        home = ledger.Ledger(self.scratch / "ledger")
        script = self.scratch / "leaky.py"
        script.write_text("import subprocess, sys\n"
                          "subprocess.run([sys.executable, '-c', 'pass', '--token', 'ghp_abcdefghijklmnopqrstuvwxyz0123456789',"
                          " 'API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz'], check=True)\n", encoding="utf-8")
        run_id = harness.new_run_id()
        report = harness.record(self.scratch, home.run_path(run_id), str(script), run_id=run_id)
        self.assertEqual(report["status"], "ok", report)
        text = home.run_path(run_id).read_text(encoding="utf-8")
        self.assertNotIn("ghp_", text)
        self.assertNotIn("sk-ant-", text)
        home.record_run(run_id)
        [io_record] = [r for r in home.load_run(run_id) if r["t"] == "io"]
        self.assertEqual(io_record["payload"]["argv"][3:], ["--token", "<redacted>", "API_KEY=<redacted>"])

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
        # The child keeps the host's checkout off the program's path on purpose (a candidate's own syberlabs must
        # win on this repository), so a program that wants current() imports syberlabs from where it has it.
        script.write_text(f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom syberlabs.factory.harness import current\n"
                          "ctx = current()\nprint(ctx.marker('answer', 42))\nsys.exit(0)\n", encoding="utf-8")
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

    def test_artifacts_are_checked_against_their_recorded_digests(self):
        """S1: an edited golden journal fails verify and cannot be loaded, so it cannot become a baseline; an
        edited Space graph and an unlogged journal are refused the same way; a torn ledger tail is repaired."""
        home = ledger.Ledger(self.scratch / "ledger")
        graph = indexed()
        home.record_space(graph)
        run_id, _, _ = self.record(home, graph=graph)
        home.set_baseline(run_id)
        self.assertTrue(home.verify())
        path = home.run_path(run_id)
        original = path.read_bytes()
        self.assertIn(b'"status":201', original)
        path.write_bytes(original.replace(b'"status":201', b'"status":500', 1))  # one byte of the golden's effect result
        self.assertFalse(home.verify())
        self.assertTrue(any(f"runs/{run_id}" in p for p in home.problems()), home.problems())
        with self.assertRaises(Rejected) as refused:
            home.load_run(run_id)
        self.assertEqual(refused.exception.code, "artifact_tampered")
        with self.assertRaises(Rejected):
            home.set_baseline(run_id)
        self.assertEqual(home.project()["runs"], 0, "an edited journal projects nothing")
        path.write_bytes(original)
        self.assertTrue(home.verify())
        space_path = home.space_path(space.graph_digest(graph))
        edited = json.loads(space_path.read_text(encoding="utf-8"))
        edited["revision"] = "elsewhere"
        space_path.write_text(json.dumps(edited), encoding="utf-8")
        with self.assertRaises(Rejected) as refused:
            home.latest_space()
        self.assertEqual(refused.exception.code, "artifact_tampered")
        self.assertFalse(home.verify())
        space_path.write_text(space.dumps(graph), encoding="utf-8")
        stray = harness.new_run_id()
        shutil.copyfile(path, home.run_path(stray))
        with self.assertRaises(Rejected) as refused:
            home.load_run(stray)
        self.assertEqual(refused.exception.code, "run_unrecorded")
        with home.log.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 99, "torn')
        self.assertEqual(len(home.events()), 3, "the torn tail is truncated on the next locked read, as the journal does")
        home.append("note", {"text": "after the tear"})
        self.assertTrue(home.verify())
        self.assertEqual(home.describe()["problems"], 0)

    def test_projection_follows_the_log(self):
        home = ledger.Ledger(self.scratch / "ledger")
        graph = indexed()
        home.record_space(graph)
        run_a, _, _ = self.record(home, graph=graph)
        self.assertEqual(len(home.worldline(run_a)), 7)
        run_b, _, _ = self.record(home, graph=graph, revision="second")
        self.assertEqual(len(home.worldline(run_b)), 7, "a run appended after the projection was built is queryable")
        self.assertEqual({c["revision"] for c in home.cells_for("rules")}, {"fixture", "second"})

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
            specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"], approval=approved(document, "promote", ["v1"]))
        specmod.transition(document, "revise", ACTOR, delta={"added": [{"req_id": "R2", "text": "Reject zero quantity."}]})
        self.assertEqual(document["version"], 2)
        self.assertEqual([r["req_id"] for r in document["requirements"]], ["R1", "R2"])
        specmod.transition(document, "validate", ACTOR, space=graph)
        self.assertEqual(document["state"], "candidate")
        # The goal gate: a model actor is refused whatever it carries; a human without the owner's approval is
        # refused; a dictionary shaped like an approval is refused; an approval of another operation, another
        # version or other verdicts is refused; the approver must be the actor.
        approval = approved(document, "promote", ["v1"])
        with self.assertRaises(Rejected) as refused:
            specmod.transition(document, "promote", {"kind": "model", "id": "reviewer"}, verdict_ids=["v1"], approval=approval)
        self.assertEqual(refused.exception.code, "spec_actor")
        with self.assertRaises(Rejected) as refused:
            specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"])
        self.assertEqual(refused.exception.code, "spec_approval")
        with self.assertRaises(Rejected) as refused:
            specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"], approval=dict(approval))
        self.assertEqual(refused.exception.code, "spec_approval")
        with self.assertRaises(Rejected) as refused:
            specmod.transition(document, "promote", ACTOR, verdict_ids=["v2"], approval=approval)
        self.assertEqual(refused.exception.code, "spec_verdict")
        with self.assertRaises(Rejected) as refused:
            specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"], approval=approved(document, "canonize"))
        self.assertEqual(refused.exception.code, "approval_mismatch")
        with self.assertRaises(Rejected) as refused:
            specmod.transition(document, "promote", {"kind": "human", "id": "someone-else"}, verdict_ids=["v1"], approval=approval)
        self.assertEqual(refused.exception.code, "approval_actor")
        with self.assertRaises(Rejected):
            approved(document, "promote", ["v1"], approver={"kind": "model", "id": "reviewer"})
        with self.assertRaises(Rejected) as refused:
            approved(document, "promote", ["v1"], public=generate()[1])
        self.assertEqual(refused.exception.code, "approval_unverified")
        self.assertEqual(document["state"], "candidate", "nothing above moved the spec")
        record = specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"], approval=approval)
        self.assertEqual(record["approval"]["approver"], ACTOR)
        self.assertEqual(record["approval"]["keyid"], OWNER_PUBLIC.hex())
        with self.assertRaises(Rejected):
            specmod.transition(document, "canonize", ACTOR)
        specmod.transition(document, "canonize", ACTOR, approval=approved(document, "canonize"))
        specmod.transition(document, "retire", ACTOR, superseded_by="order-path:3", approval=approved(document, "retire"))
        self.assertEqual([h["operation"] for h in document["history"]],
                         ["revise", "validate", "promote", "canonize", "retire"])
        self.assertEqual(document["supersedes"], "order-path:3")
        with self.assertRaises(Rejected):
            specmod.transition(document, "bogus", ACTOR)

    def test_principal(self):
        self.assertEqual(specmod.principal("human:mateo"), {"kind": "human", "id": "mateo"})
        self.assertEqual(specmod.principal("model:claude-sonnet-4-5"), {"kind": "model", "id": "claude-sonnet-4-5"})
        for bad in ("operator", "owner:mateo", "human:", "human:has space", "model:" + "x" * 65, ""):
            with self.assertRaises(Rejected, msg=bad):
                specmod.principal(bad)

    def test_verdicts_resolve_against_the_ledger(self):
        """A promotion's verdict ids are not strings: each is a review on the ledger that accepted the very variant
        this spec is, and the approver may not have proposed or reviewed it."""
        document = self.spec()
        current = specmod.spec_digest(document)
        reviewer = {"kind": "model", "id": "gpt", "model": "gpt-5", "family": "openai"}
        events = [
            {"kind": "owner_key_set", "body": {"keyid": OWNER_PUBLIC.hex(), "previous": None, "rotation_digest": None}},
            {"kind": "spec_changed", "body": {"spec_id": "order-path", "operation": "mutate", "variant_id": "var1",
                                              "actor": {"kind": "model", "id": "claude"}}},
            {"kind": "spec_changed", "body": {"spec_id": "order-path", "operation": "review", "verdict_id": "ok1",
                                              "variant_id": "var1", "decision": "accept", "actor": reviewer}},
            {"kind": "spec_changed", "body": {"spec_id": "order-path", "operation": "review", "verdict_id": "no1",
                                              "variant_id": "var2", "decision": "reject", "actor": reviewer}},
            {"kind": "spec_changed", "body": {"spec_id": "order-path", "operation": "validate", "accepted_variant": "var1",
                                              "verdict_id": "ok1", "spec_digest": current}},
        ]
        [resolved] = specmod.resolve_verdicts(events, document, ["ok1"])
        self.assertEqual((resolved["variant_id"], resolved["proposers"]), ("var1", ["claude"]))
        for ids, why in (([], "none given"), (["ghost"], "not a review"), (["no1"], "did not accept")):
            with self.assertRaises(Rejected, msg=why) as refused:
                specmod.resolve_verdicts(events, document, ids)
            self.assertEqual(refused.exception.code, "spec_verdict")
        edited = dict(document, capability="something else")
        with self.assertRaises(Rejected):
            specmod.resolve_verdicts(events, edited, ["ok1"])
        envelope = specmod.sign_approval(specmod.approval_payload(document, "promote", ACTOR, verdict_ids=["ok1"]), OWNER_SEED)
        approval, found = specmod.gate(document, "promote", ACTOR, events, envelope, verdict_ids=["ok1"])
        self.assertIsInstance(approval, specmod.Approved)
        self.assertEqual(found[0]["verdict_id"], "ok1")
        proposer_as_owner = {"kind": "human", "id": "claude"}
        own = specmod.sign_approval(specmod.approval_payload(document, "promote", proposer_as_owner, verdict_ids=["ok1"]), OWNER_SEED)
        with self.assertRaises(Rejected) as refused:
            specmod.gate(document, "promote", proposer_as_owner, events, own, verdict_ids=["ok1"])
        self.assertEqual(refused.exception.code, "approval_not_independent")
        with self.assertRaises(Rejected) as refused:
            specmod.gate(document, "promote", {"kind": "model", "id": "gpt"}, events, envelope, verdict_ids=["ok1"])
        self.assertEqual(refused.exception.code, "spec_actor")
        # The key is the ledger's, not the caller's: an approval under another keypair is refused however the gate
        # is called, and a ledger that pins no key approves nothing.
        other_seed, _ = generate()
        under_another_key = specmod.sign_approval(specmod.approval_payload(document, "promote", ACTOR, verdict_ids=["ok1"]), other_seed)
        with self.assertRaises(Rejected) as refused:
            specmod.gate(document, "promote", ACTOR, events, under_another_key, verdict_ids=["ok1"])
        self.assertEqual(refused.exception.code, "owner_key_mismatch")
        with self.assertRaises(Rejected) as refused:
            specmod.gate(document, "promote", ACTOR, events[1:], envelope, verdict_ids=["ok1"])
        self.assertEqual(refused.exception.code, "owner_key_unset")
        self.assertEqual(specmod.pinned_owner_key(events[1:]), None)
        # ``Approved`` is a capability, not a marker: the constructor refuses without the module's token, and
        # ``transition`` refuses a plain dictionary and an instance made around the constructor alike.
        with self.assertRaises(Rejected) as refused:
            specmod.Approved(dict(approval))
        self.assertEqual(refused.exception.code, "approval_forged")
        candidate = json.loads(json.dumps(document))
        specmod.transition(candidate, "validate", ACTOR)
        counterfeit = specmod.Approved.__new__(specmod.Approved)
        dict.update(counterfeit, approval)
        for forged in (dict(approval), counterfeit):
            with self.assertRaises(Rejected) as refused:
                specmod.transition(json.loads(json.dumps(candidate)), "promote", ACTOR, approval=forged, verdict_ids=["ok1"])
            self.assertEqual(refused.exception.code, "spec_approval")
        self.assertEqual(json.loads(json.dumps(candidate))["state"], "candidate")

    def test_independence_is_by_provider_family(self):
        claude = {"kind": "model", "id": "a", "model": "claude-sonnet-4-5"}
        gpt = {"kind": "model", "id": "b", "model": "gpt-5"}
        self.assertEqual(independence.family(claude), "anthropic")
        self.assertEqual(independence.family({"signal": {"harness": {"model": "grok-4"}}}), "xai")
        self.assertEqual(independence.family({"provider": {"model": "openrouter/google/gemini-2.5-pro"}}), "google")
        self.assertEqual(independence.family({"family": "OpenAI"}), "openai")
        self.assertIsNone(independence.family({"kind": "model", "id": "mystery"}))
        self.assertIsNone(independence.family({"model": "frobnicator-9"}))
        self.assertTrue(independence.independence(claude, gpt)[0])
        self.assertFalse(independence.independence(claude, {"id": "other-name", "model": "claude-opus-4-1"})[0], "same family, other name")
        self.assertFalse(independence.independence(claude, {"id": "unknown"})[0], "an unknown family refuses")
        self.assertFalse(independence.independence(None, gpt)[0])

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
        specmod.transition(document, "promote", ACTOR, verdict_ids=["v1"], approval=approved(document, "promote", ["v1"]))
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
        self.assertEqual((changed["level"], tuple(changed["first_divergence"]["before"])), ("component", ("effects", None)))
        self.assertFalse(changed["function_level_equal"])
        self.assertTrue(same["function_level_equal"])
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
        event = home.record_attestation(envelope, public)
        self.assertEqual(event["body"]["signed"], True)
        self.assertEqual(event["body"]["keyid"], public.hex())
        self.assertEqual(event["body"]["promote"], True)
        self.assertEqual(event["body"]["subject"], ["git:fx@cand"])
        self.assertTrue((home.home / "attestations" / f"{event['body']['attestation_digest']}.json").exists())
        unchecked = home.record_attestation(envelope)
        self.assertEqual((unchecked["body"]["signed"], unchecked["body"]["envelope"]), (False, True),
                         "envelope shape alone is not a signature")
        self.assertFalse(home.record_attestation(envelope, generate()[1])["body"]["signed"], "the wrong key is not a signature")
        self.assertTrue(home.verify())
        # A signed statement whose written policy says more than its evidence: verify recomputes and refuses.
        forged = json.loads(json.dumps(refused))
        forged["predicate"]["policy"] = {"promote": True, "reasons": ["looks fine"]}
        self.assertFalse(attest.recompute(forged["predicate"])["promote"])
        self.assertTrue(attest.recompute(promoted["predicate"])["promote"])


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
        # Promotion is an owner act over a reviewed variant: a model actor is refused, a human without the owner's
        # approval is refused, a verdict that is not on the ledger is refused, and the reviewed, approved variant passes.
        seed, public = generate()
        os.environ["SYBERLABS_FACTORY_TEST_SEED"] = seed.hex()
        self.addCleanup(os.environ.pop, "SYBERLABS_FACTORY_TEST_SEED", None)
        (tree / "owner.pub").write_text(public.hex(), encoding="utf-8")
        code, out, err = self.run_cli("spec", "promote", "spec.json", "--verdict", "v1", "--actor", "model:reviewer", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("spec_actor", err)
        code, out, err = self.run_cli("spec", "promote", "spec.json", "--verdict", "v1", "--actor", "human:tester", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("spec_approval", err)
        code, out, err = self.run_cli("approve", "spec.json", "--verdict", "v1", "--actor", "human:tester",
                                      "--seed-env", "SYBERLABS_FACTORY_TEST_SEED", "--out", "approval.json", cwd=tree)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("spec", "promote", "spec.json", "--verdict", "v1", "--actor", "human:tester",
                                      "--approval", "approval.json", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("owner_key_unset", err, "a ledger that pins no owner key promotes nothing")
        code, out, err = self.run_cli("spec", "promote", "spec.json", "--verdict", "v1", "--actor", "human:tester",
                                      "--approval", "approval.json", "--owner-public", "owner.pub", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("pinned (bootstrap", out, "the first --owner-public pins the key")
        self.assertIn("spec_verdict", err, "v1 is a string, not a review on the ledger")
        self.assertEqual(json.loads(spec_path.read_text(encoding="utf-8"))["state"], "candidate")
        run = lambda role: f"{sys.executable} {tree / 'proposers.py'} {role}"
        code, out, err = self.run_cli("variants", "spec.json", "--proposer", f"claude={run('tighten')}",
                                      "--reviewer", f"gpt={run('review')}", cwd=tree)
        self.assertEqual(code, 0, err + out)
        self.assertNotIn("--actor model:", out, "the loop no longer hints at a model promoting")
        verdict_id = out.split("--verdict ")[1].split()[0]
        candidate_spec = "order-path.v2.json"
        code, out, err = self.run_cli("approve", candidate_spec, "--verdict", verdict_id, "--actor", "human:tester",
                                      "--seed-env", "SYBERLABS_FACTORY_TEST_SEED", "--out", "approval.json", cwd=tree)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("spec", "promote", candidate_spec, "--verdict", verdict_id, "--actor", "human:claude",
                                      "--approval", "approval.json", "--owner-public", "owner.pub", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("approval_actor", err, "the approval names tester, not the actor")
        (tree / "other.pub").write_text(generate()[1].hex(), encoding="utf-8")
        code, out, err = self.run_cli("spec", "promote", candidate_spec, "--verdict", verdict_id, "--actor", "human:tester",
                                      "--approval", "approval.json", "--owner-public", "other.pub", cwd=tree)
        self.assertEqual(code, 2)
        self.assertIn("owner_key_mismatch", err, "the ledger pins the owner key; the caller cannot name another")
        code, out, err = self.run_cli("spec", "promote", candidate_spec, "--verdict", verdict_id, "--actor", "human:tester",
                                      "--approval", "approval.json", "--tickets", "tickets", "--baseline", run_a, "--owner", "tester", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("candidate -> promoted", out)
        self.assertIn("ticket     ", out)
        promoted = json.loads((tree / candidate_spec).read_text(encoding="utf-8"))
        self.assertEqual(promoted["state"], "promoted")
        self.assertEqual(promoted["history"][-1]["approval"]["approver"], {"kind": "human", "id": "tester"})
        home = ledger.Ledger(tree / ".syberlabs" / "factory")
        approvals = [e for e in home.events() if e["kind"] == "approval_recorded"]
        self.assertEqual(len(approvals), 1)
        promotion = [e for e in home.events() if e["kind"] == "spec_changed" and e["body"].get("to") == "promoted"][-1]
        self.assertEqual(promotion["body"]["approval"]["event_hash"], approvals[0]["hash"], "the promotion cites the approval event")
        self.assertTrue(home.verify())
        code, out, err = self.run_cli("spec", "promote", candidate_spec, "--verdict", verdict_id, "--actor", "human:tester",
                                      "--approval", "approval.json", cwd=tree)
        self.assertEqual(code, 2, "a promoted spec is not promoted twice")
        [auto] = list((tree / "tickets").glob("*.json"))
        auto_ticket = json.loads(auto.read_text(encoding="utf-8"))
        self.assertEqual((auto_ticket["spec_ref"]["req_ids"], auto_ticket["repro"]["baseline_run"]), (["R1"], run_a))
        self.assertEqual(auto_ticket["footprint"]["components"], ["effects", "ingest", "rules"])
        code, out, err = self.run_cli("ticket", candidate_spec, "--req", "R1", "--baseline", run_a, "--owner", "tester",
                                      "--out", "ticket.json", cwd=tree)
        self.assertEqual(code, 0, err)
        ticket = json.loads((tree / "ticket.json").read_text(encoding="utf-8"))
        self.assertEqual(ticket["footprint"]["components"], ["effects", "ingest", "rules"])
        code, out, err = self.run_cli("record", "--entry", "fxapp.main:run", "--fixture", "fixture.json",
                                      "--revision", "cand", cwd=tree)
        self.assertEqual(code, 0, err)
        run_b = out.split("recorded run ")[1].split()[0]
        code, out, err = self.run_cli("attest", "--baseline", run_a, "--candidate", run_b, "--ticket", "ticket.json",
                                      "--commit", "cand", "--seed-env", "SYBERLABS_FACTORY_TEST_SEED",
                                      "--out", "attestation.json", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("promote True", out)
        attested = [e for e in ledger.Ledger(tree / ".syberlabs" / "factory").events() if e["kind"] == "attested"][-1]
        self.assertEqual((attested["body"]["signed"], attested["body"]["keyid"]), (True, public.hex()))
        (tree / "key.pub").write_text(public.hex(), encoding="utf-8")
        code, out, err = self.run_cli("verify", "attestation.json", "--public", "key.pub", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("verified: git:tree@cand", out)
        self.assertIn("recomputed from the evidence", out)
        (tree / "wrong.pub").write_text(generate()[1].hex(), encoding="utf-8")
        code, out, _ = self.run_cli("verify", "attestation.json", "--public", "wrong.pub", cwd=tree)
        self.assertEqual(code, 1)
        # A statement the signer graded itself: signed with the right key, but its policy says more than its evidence.
        self_graded = json.loads((tree / "attestation.json").read_text(encoding="utf-8"))
        statement = attest.unwrap(self_graded)
        statement["predicate"]["evidence"]["trace_diff"]["diff_empty"] = False
        (tree / "graded.json").write_text(json.dumps(attest.sign(statement, seed)), encoding="utf-8")
        code, out, _ = self.run_cli("verify", "graded.json", "--public", "key.pub", cwd=tree)
        self.assertEqual(code, 1)
        self.assertIn("policy mismatch", out)
        code, out, err = self.run_cli("export", run_a, "--chrome", "trace.json", "--view", "run.html", cwd=tree)
        self.assertEqual(code, 0, err)
        page = (tree / "run.html").read_text(encoding="utf-8")
        self.assertIn('"run_id": "' + run_a + '"', page)
        self.assertNotIn("/*__FACTORY_DATA__*/null", page)
        self.assertNotIn('"payload"', page, "the viewer references effects by digest; payloads stay in the journal")
        self.assertNotIn("innerHTML", page)
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

    def test_the_owner_key_is_pinned_and_rotates_only_under_the_old_key(self):
        """M1 of the #164 verification: the gate trusts the key the ledger pins, not the key the caller names. A
        second keypair's approval is refused with and without --owner-public, a rotation needs the pinned key's
        seed, and after a rotation the new key approves and the old one does not."""
        from unittest import mock
        tree = self.scratch / "tree"
        shutil.copytree(FIXTURE, tree)
        (tree / "spec.json").write_text(json.dumps(specmod.new_spec("order-path", "ordering", [
            {"req_id": "R1", "text": "When quantity is positive, the system shall post one order."}],
            ["ingest", "rules", "effects"], author=ACTOR)), encoding="utf-8")
        code, _, err = self.run_cli("spec", "validate", "spec.json", cwd=tree)
        self.assertEqual(code, 0, err)
        owner_seed, owner_public = generate()
        mine_seed, mine_public = generate()
        (tree / "owner.pub").write_text(owner_public.hex(), encoding="utf-8")
        (tree / "mine.pub").write_text(mine_public.hex(), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SYBERLABS_FACTORY_OWNER_SEED": owner_seed.hex(), "SYBERLABS_FACTORY_MINE_SEED": mine_seed.hex()}):
            retire = lambda *extra: self.run_cli("spec", "retire", "spec.json", "--actor", "human:mateo", *extra, cwd=tree)
            code, out, _ = self.run_cli("owner", cwd=tree)
            self.assertEqual((code, "(unset)" in out), (1, True))
            code, _, err = self.run_cli("approve", "spec.json", "--operation", "retire", "--actor", "human:mateo",
                                        "--seed-env", "SYBERLABS_FACTORY_OWNER_SEED", "--out", "owner.json", cwd=tree)
            self.assertEqual(code, 0, err)
            code, _, err = retire("--approval", "owner.json")
            self.assertEqual((code, "owner_key_unset" in err), (2, True), err)
            code, out, err = self.run_cli("owner", "--public", "owner.pub", cwd=tree)
            self.assertEqual(code, 0, err)
            self.assertIn("pinned (bootstrap)", out)
            code, out, _ = self.run_cli("owner", "--public", "owner.pub", cwd=tree)
            self.assertEqual((code, "already pinned" in out), (0, True))
            # The verifier's repro: my own keypair signs an approval, and I name my own key on argv.
            code, _, err = self.run_cli("approve", "spec.json", "--operation", "retire", "--actor", "human:mateo",
                                        "--seed-env", "SYBERLABS_FACTORY_MINE_SEED", "--out", "mine.json", cwd=tree)
            self.assertEqual(code, 0, err)
            for extra in (("--approval", "mine.json", "--owner-public", "mine.pub"), ("--approval", "mine.json")):
                code, _, err = retire(*extra)
                self.assertEqual(code, 2, err)
                self.assertIn("owner_key_mismatch", err)
            self.assertEqual(json.loads((tree / "spec.json").read_text(encoding="utf-8"))["state"], "candidate")
            # Rotation without the pinned key's signature is refused; with it, the ledger moves to the new key.
            code, _, err = self.run_cli("owner", "--public", "mine.pub", cwd=tree)
            self.assertEqual((code, "owner_key_rotation" in err), (2, True), err)
            code, _, err = self.run_cli("owner", "--public", "mine.pub", "--seed-env", "SYBERLABS_FACTORY_MINE_SEED", cwd=tree)
            self.assertEqual((code, "owner_key_rotation" in err), (2, True), err)
            code, out, err = self.run_cli("owner", "--public", "mine.pub", "--seed-env", "SYBERLABS_FACTORY_OWNER_SEED", cwd=tree)
            self.assertEqual(code, 0, err)
            self.assertIn("rotated from " + owner_public.hex()[:10], out)
            code, out, _ = self.run_cli("owner", cwd=tree)
            self.assertEqual((code, mine_public.hex() in out), (0, True))
            code, _, err = retire("--approval", "owner.json")
            self.assertEqual((code, "owner_key_mismatch" in err), (2, True), "the key that was rotated out no longer approves")
            code, out, err = retire("--approval", "mine.json")
            self.assertEqual(code, 0, err)
            self.assertIn("candidate -> retired", out)
        home = ledger.Ledger(tree / ".syberlabs" / "factory")
        pins = [e["body"] for e in home.events() if e["kind"] == "owner_key_set"]
        self.assertEqual([(p["keyid"], p["previous"]) for p in pins], [(owner_public.hex(), None), (mine_public.hex(), owner_public.hex())])
        self.assertTrue(home.verify())
        # In process, the same: ``set_owner_key`` refuses a seed that is not the pinned key's and a re-pin of the same key.
        with self.assertRaises(Rejected) as refused:
            home.set_owner_key(owner_public, seed=owner_seed)
        self.assertEqual(refused.exception.code, "owner_key_rotation")
        with self.assertRaises(Rejected) as refused:
            home.set_owner_key(mine_public, seed=mine_seed)
        self.assertEqual(refused.exception.code, "owner_key_pinned")
        # An edited rotation is named by verify as the artifact it is and as a break in the lineage; from then on
        # the ledger pins nothing, so neither key approves and no rotation is accepted (M3 of the delta verification).
        rotation = home.home / "approvals" / f"{pins[1]['rotation_digest']}.json"
        rotation.write_text(rotation.read_text(encoding="utf-8").replace(owner_public.hex(), mine_public.hex()), encoding="utf-8")
        problems = home.problems()
        self.assertTrue(any("approvals/" in problem for problem in problems), problems)
        self.assertTrue(any(problem.startswith("owner_key_lineage:") for problem in problems), problems)
        for public, seed in ((owner_public, owner_seed), (mine_public, mine_seed)):
            with self.assertRaises(Rejected) as refused:
                home.set_owner_key(public, seed=seed)
            self.assertEqual(refused.exception.code, "owner_key_lineage")

    def test_the_owner_key_is_the_verified_lineage_not_the_newest_event(self):
        """M3 of the #164 delta verification: the pin is the ledger's verified lineage, not its newest
        ``owner_key_set``. After an honest bootstrap and rotation, each of the verifier's four shapes, appended
        through the public ``append`` with the chain intact (a bare attacker pin, a rotation signed by the new key,
        a rotation with a stale ``previous``, a malformed keyid last), fails ``verify``, is named
        ``owner_key_lineage`` by ``problems``, refuses the gate at ``owner_key_lineage`` for the attacker's and
        the honest approval alike (no ``owner_key_unset``, no fallback to an earlier key), refuses a further
        rotation, and exits `spec retire` with 2. The honest ledger still retires."""
        from syberlabs.canonical import canonical, digest
        mateo = {"kind": "human", "id": "mateo"}
        tree = self.scratch / "tree"
        shutil.copytree(FIXTURE, tree)
        (tree / "spec.json").write_text(json.dumps(specmod.new_spec("order-path", "ordering", [
            {"req_id": "R1", "text": "When quantity is positive, the system shall post one order."}],
            ["ingest", "rules", "effects"], author=mateo)), encoding="utf-8")
        code, _, err = self.run_cli("spec", "validate", "spec.json", "--actor", "human:mateo", cwd=tree)
        self.assertEqual(code, 0, err)
        first_seed, first_public = generate()
        second_seed, second_public = generate()
        attacker_seed, attacker_public = generate()
        home = ledger.Ledger(tree / ".syberlabs" / "factory")
        home.set_owner_key(first_public)
        home.set_owner_key(second_public, seed=first_seed)
        self.assertEqual(home.owner_key(), second_public.hex())
        self.assertEqual(home.owner_key_lineage()[1], [])
        self.assertTrue(home.verify())
        document = json.loads((tree / "spec.json").read_text(encoding="utf-8"))
        honest = specmod.sign_approval(specmod.approval_payload(document, "retire", mateo), second_seed)
        forged = specmod.sign_approval(specmod.approval_payload(document, "retire", mateo), attacker_seed)
        (tree / "honest.json").write_text(json.dumps(honest), encoding="utf-8")
        (tree / "forged.json").write_text(json.dumps(forged), encoding="utf-8")

        def rotation(target, previous, next_key, signer_seed, claimed_previous=None):
            """An ``owner_key_set`` body with its envelope stored where ``verify`` looks, signed by whoever."""
            envelope = specmod.sign_rotation(specmod.rotation_payload(previous, next_key), signer_seed)
            sha = digest(envelope)
            (target.home / "approvals" / f"{sha}.json").write_text(canonical(envelope), encoding="utf-8")
            return {"keyid": next_key.hex(), "previous": (claimed_previous or previous).hex(), "rotation_digest": sha}

        shapes = [
            ("a bare appended pin", lambda t: {"keyid": attacker_public.hex(), "previous": None, "rotation_digest": None}),
            ("a rotation signed by the new key", lambda t: rotation(t, second_public, attacker_public, attacker_seed)),
            ("a rotation with a stale previous", lambda t: rotation(t, first_public, attacker_public, first_seed)),
            ("a malformed keyid last", lambda t: {"keyid": "not-a-key", "previous": second_public.hex(), "rotation_digest": None}),
        ]
        for number, (shape, body) in enumerate(shapes):
            copy = self.scratch / f"shape{number}"
            shutil.copytree(tree, copy)
            target = ledger.Ledger(copy / ".syberlabs" / "factory")
            target.append("owner_key_set", body(target))
            events = target.events()
            self.assertTrue(ledger._verify_chain(events), shape)
            self.assertFalse(target.verify(), shape)
            self.assertTrue(any(p.startswith("owner_key_lineage: owner_key_set at seq 4") for p in target.problems()), (shape, target.problems()))
            self.assertEqual(target.owner_key_lineage(events)[0], None, shape)
            with self.assertRaises(Rejected, msg=shape) as refused:
                target.owner_key()
            self.assertEqual(refused.exception.code, "owner_key_lineage", shape)
            for envelope in (forged, honest):
                with self.assertRaises(Rejected, msg=shape) as refused:
                    specmod.gate(document, "retire", mateo, events, envelope, load_rotation=target.load_rotation)
                self.assertEqual(refused.exception.code, "owner_key_lineage", shape)
            with self.assertRaises(Rejected, msg=shape) as refused:
                target.set_owner_key(attacker_public, seed=second_seed)
            self.assertEqual(refused.exception.code, "owner_key_lineage", shape)
            for approval in ("forged.json", "honest.json"):
                code, _, err = self.run_cli("spec", "retire", "spec.json", "--actor", "human:mateo", "--approval", approval, cwd=copy)
                self.assertEqual(code, 2, (shape, err))
                self.assertIn("owner_key_lineage", err, shape)
            code, _, err = self.run_cli("owner", cwd=copy)
            self.assertEqual((code, "owner_key_lineage" in err), (2, True), shape)
            self.assertEqual(json.loads((copy / "spec.json").read_text(encoding="utf-8"))["state"], "candidate", shape)
            self.assertEqual([e for e in target.events() if e["kind"] == "spec_changed" and e["body"].get("operation") == "retire"], [], shape)
        # In-memory events without a loader: a bootstrap alone pins; a rotation cannot be checked, so it is invalid.
        self.assertEqual(specmod.pinned_owner_key(home.events()[:2]), first_public.hex())
        with self.assertRaises(Rejected) as refused:
            specmod.pinned_owner_key(home.events())
        self.assertEqual(refused.exception.code, "owner_key_lineage")
        # The honest ledger: the forged approval is a mismatch, the honest one retires, and the lineage still verifies.
        code, _, err = self.run_cli("spec", "retire", "spec.json", "--actor", "human:mateo", "--approval", "forged.json", cwd=tree)
        self.assertEqual((code, "owner_key_mismatch" in err), (2, True), err)
        code, out, err = self.run_cli("spec", "retire", "spec.json", "--actor", "human:mateo", "--approval", "honest.json", cwd=tree)
        self.assertEqual(code, 0, err)
        self.assertIn("candidate -> retired", out)
        self.assertTrue(home.verify())
        self.assertEqual(home.describe()["owner_key"], second_public.hex())


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

    def kit(self, extra_argv=(), version=1):
        contract = self.default_contract(self.project)
        contract["version"] = version
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

    def test_an_edited_golden_refuses_the_check(self):
        """S1 through the seam: the golden journal is edited after the baseline was set; the check refuses before
        the candidate runs, and nothing is attested."""
        home, golden = self.golden()
        path = home.run_path(golden)
        original = path.read_bytes()
        self.assertIn(b'"status":201', original)
        path.write_bytes(original.replace(b'"status":201', b'"status":500', 1))
        self.assertFalse(home.verify())
        kit = self.kit()
        thread = kit.start("Tidy", paths=["fxapp/"])
        rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
        [candidate] = thread.propose(changes={"fxapp/rules.py": rules + "\n"})
        verdict = thread.check(candidate.id)
        self.assertFalse(verdict.acceptable, verdict.summary())
        self.assertEqual(verdict.reason, "candidate_check_failed:factory")
        self.assertIn("ledger_corrupt", verdict.checks[0].output_tail)
        self.assertIn(f"runs/{golden}", verdict.checks[0].output_tail)
        self.assertEqual([e for e in home.events() if e["kind"] == "attested"], [], "nothing was attested over an edited baseline")

    def test_a_ledger_that_changes_under_the_test_suite_refuses_the_check(self):
        """M3's window: a candidate that learned the ledger's path appends a line while its test suite runs, after
        the check's post-run verification. The check verifies the ledger again after the suite and before anything
        is attested, and refuses; the line is on the ledger, nothing is attested over it."""
        import sys as _sys
        home, _ = self.golden()
        reaching = json.dumps([_sys.executable, "-c",
                               f"import sys; sys.path.insert(0, {str(ROOT)!r}); from syberlabs.factory import ledger; "
                               f"ledger.Ledger({str(self.ledger_home)!r}).append('note', {{'text': 'from the suite'}})"])
        kit = self.kit(extra_argv=["--tests-argv", reaching])
        thread = kit.start("Reach", paths=["fxapp/"])
        rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
        [candidate] = thread.propose(changes={"fxapp/rules.py": rules + "\n"})
        verdict = thread.check(candidate.id)
        self.assertFalse(verdict.acceptable, verdict.summary())
        self.assertEqual(verdict.reason, "candidate_check_failed:factory")
        self.assertIn("ledger_corrupt", verdict.checks[0].output_tail)
        events = home.events()
        self.assertEqual(events[-1]["kind"], "note", "the suite's line did land, which is what the check saw")
        self.assertEqual([e for e in events if e["kind"] == "attested"], [], "nothing was attested over it")
        self.assertTrue(home.verify())

    def test_a_candidate_that_forges_its_report_is_refused(self):
        """S2 through the seam: the candidate rewrites the recorded entrypoint to print a clean report and exit. The
        check reads the journal's end record itself and takes the report only over the nonce-bound channel."""
        home, _ = self.golden()
        kit = self.kit()
        thread = kit.start("Make the gate happy", paths=["fxapp/"])
        main = (self.project / "fxapp" / "main.py").read_text(encoding="utf-8")
        forged = main.replace("from fxapp import rules\n", "from fxapp import rules\nfrom fxapp import variants\n") \
                     .replace("def run(ctx, payload):\n", "def run(ctx, payload):\n    return variants.forge_report(ctx, payload)\n")
        self.assertNotEqual(main, forged)
        [candidate] = thread.propose(changes={"fxapp/main.py": forged})
        verdict = thread.check(candidate.id)
        self.assertFalse(verdict.acceptable, verdict.summary())
        self.assertEqual(verdict.reason, "candidate_check_failed:factory")
        tail = verdict.checks[0].output_tail
        self.assertIn("no journal:", tail, "the forged line on stdout is not a report, and a journal without an end is not delivered")
        self.assertIn('"status": "ok"', tail, "what the candidate printed is in the log, as output, not as a verdict")
        self.assertEqual([e for e in home.events() if e["kind"] == "attested"], [], "nothing was attested")
        self.assertEqual([r for r in home.runs() if r["revision"] == candidate.commit], [], "the forged run is not on the ledger")
        self.assertTrue(home.verify())

    def test_tests_argv_is_recorded_as_evidence_and_required(self):
        import sys as _sys
        home, _ = self.golden()
        green = json.dumps([_sys.executable, "-c", "import fxapp.rules"])
        red = json.dumps([_sys.executable, "-c", "import sys; print('1 failed'); sys.exit(1)"])
        kit = self.kit(extra_argv=["--tests-argv", green])
        thread = kit.start("Tidy", paths=["fxapp/"])
        rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
        [candidate] = thread.propose(changes={"fxapp/rules.py": rules + "\n"})
        verdict = thread.check(candidate.id)
        self.assertTrue(verdict.acceptable, verdict.summary())
        self.assertIn("full test suite green", verdict.checks[0].output_tail)
        attested = [e for e in home.events() if e["kind"] == "attested"][-1]
        document = json.loads((home.home / "attestations" / f"{attested['body']['attestation_digest']}.json").read_text(encoding="utf-8"))
        tests = document["predicate"]["evidence"]["tests"]
        self.assertEqual((tests["exit_code"], tests["fail"], tests["full_suite"]), (0, 0, True))
        self.assertTrue(document["predicate"]["gate"]["require_tests"])
        kit = self.kit(extra_argv=["--tests-argv", red], version=2)
        thread = kit.start("Break", paths=["fxapp/"])
        [candidate] = thread.propose(changes={"fxapp/rules.py": rules + "\n\n"})
        verdict = thread.check(candidate.id)
        self.assertFalse(verdict.acceptable)
        self.assertIn("tests did not run the full suite green: refuse", verdict.checks[0].output_tail)
        # A suite that hangs is stopped at the contract's timeout, with its process tree, and is not a full suite.
        hanging = json.dumps([_sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"])
        kit = self.kit(extra_argv=["--tests-argv", hanging, "--tests-timeout", "2"], version=3)
        thread = kit.start("Hang", paths=["fxapp/"])
        [candidate] = thread.propose(changes={"fxapp/rules.py": rules + "\n\n\n"})
        import time as _time
        started = _time.monotonic()
        verdict = thread.check(candidate.id)
        self.assertLess(_time.monotonic() - started, 40)
        self.assertFalse(verdict.acceptable)
        attested = [e for e in home.events() if e["kind"] == "attested"][-1]
        document = json.loads((home.home / "attestations" / f"{attested['body']['attestation_digest']}.json").read_text(encoding="utf-8"))
        tests = document["predicate"]["evidence"]["tests"]
        self.assertEqual((tests["timed_out"], tests["full_suite"], tests["exit_code"]), (True, False, None))

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
        contract["evolution"]["evaluation"] = {"checks": {"tests": {"argv": [_sys.executable, "-c", "pass"], "timeout_seconds": 60}},
                                               "required": ["tests"], "max_age_seconds": 3600}
        contracts = self.project / ".syberlabs" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "repo-change.v1.json").write_text(json.dumps(contract), encoding="utf-8")
        # The published argv says `-m syberlabs.factory`; this checkout is not installed, so the test names a
        # launcher that puts it on sys.path first and otherwise behaves as the interpreter.
        launcher = self.scratch / "python-with-checkout.py"
        launcher.write_text(f"import runpy, sys\nsys.path.insert(0, {str(ROOT)!r})\n"
                            "assert sys.argv[1:3] == ['-P', '-m']\nsys.argv = sys.argv[3:]\nrunpy.run_module(sys.argv[0], run_name='__main__', alter_sys=True)\n",
                            encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(self.project)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = factory_cli.main(["--home", str(self.ledger_home), "bootstrap", "--declared", "architecture.json",
                                         "--entry", "fxapp.main:run", "--fixture", "fixture.json", "--max-drift", "1.0",
                                         "--python", _sys.executable, "--tests-argv", "contract"])
        finally:
            os.chdir(here)
        self.assertEqual(code, 0, err.getvalue() + out.getvalue())
        published = contracts / "repo-change.v2.json"
        self.assertTrue(published.exists(), out.getvalue())
        document = json.loads(published.read_text(encoding="utf-8"))
        self.assertEqual(document["evolution"]["evaluation"]["required"], ["factory"], "the tests check folded into factory")
        self.assertNotIn("tests", document["evolution"]["evaluation"]["checks"])
        argv = document["evolution"]["evaluation"]["checks"]["factory"]["argv"]
        self.assertEqual(argv[1:4], ["-P", "-m", "syberlabs.factory"], "the host's factory runs, not the candidate's")
        self.assertIn(str(self.ledger_home), argv)
        self.assertEqual(json.loads(argv[argv.index("--tests-argv") + 1])[1:], ["-c", "pass"])
        self.assertEqual(argv[argv.index("--tests-timeout") + 1], "60", "the contract's own tests timeout, not 1800")
        self.assertEqual(document["evolution"]["evaluation"]["checks"]["factory"]["timeout_seconds"], 660)
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
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("factory", "passed")])
        self.assertIn("full test suite green", verdict.checks[0].output_tail)
        self.assertEqual(thread.accept(candidate.id).status, "succeeded")


class SelfHosting(Scratch):
    """The factory on this repository: examples/build_thread.py recorded under the declared layering. A refactor
    passes; dropping the compare-and-swap from accept is caught by the io observation with no edit to the program."""

    def test_mask(self):
        self.assertEqual(harness.mask("a" * 40), "<hex>")
        self.assertEqual(harness.mask("/tmp/tmpab12cd_x/project"), "<path>/project")
        self.assertEqual(harness.mask("tmpab12cd_x"), "<tmp>")
        self.assertEqual(harness.mask("refs/heads/syberlabs/f6853a94"), "<path>/<hex>")
        self.assertEqual(harness.mask("accept c2 (bd57b9bf-53e8-4803-8c7c-ae22237e8bcb)"), "accept c2 (<hex>)")
        self.assertEqual(harness.mask("a3973731f2552abd581f2c7bdf5ee3b92ed7d466^{tree}"), "<hex>^{tree}")
        self.assertEqual(harness.mask("--format=%(refname)"), "--format=%(refname)")
        self.assertEqual(harness.mask("c1"), "c1")

    def test_the_self_check_program_sees_a_rerun_policy_change(self):
        """examples/factory_self_check.py checks a candidate three times (plain, plain again, rerun). Making
        Thread.check rerun by default adds check processes on the second call, which io observation sees."""
        home = ledger.Ledger(self.scratch / "ledger")
        declared = json.loads((ROOT / "docs" / "architecture.declared.json").read_text(encoding="utf-8"))
        graph = space.index(ROOT, paths=["syberlabs", "syberwork"], revision="checkout")
        space.map_components(graph, declared)
        golden = harness.new_run_id()
        report = harness.record(ROOT, home.run_path(golden), str(ROOT / "examples" / "factory_self_check.py"),
                                file_components=file_components(graph), run_id=golden, revision="checkout", blobs=home.home / "blobs")
        self.assertEqual(report["status"], "ok", report.get("error"))
        tree = self.scratch / "rerun"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", ".syberlabs", "*.sqlite3"))
        build = tree / "syberlabs" / "build.py"
        text = build.read_text(encoding="utf-8")
        changed = text.replace("def check(self, candidate: str, *, rerun: bool = False, run: bool = True,",
                               "def check(self, candidate: str, *, rerun: bool = True, run: bool = True,")
        self.assertNotEqual(text, changed)
        build.write_text(changed, encoding="utf-8")
        home.record_run(golden)
        candidate = harness.new_run_id()
        report = harness.record(tree, home.run_path(candidate), str(tree / "examples" / "factory_self_check.py"),
                                file_components=file_components(graph), run_id=candidate, revision="rerun", blobs=home.home / "blobs")
        self.assertEqual(report["status"], "ok", report.get("error"))
        home.record_run(candidate)
        self.assertGreater(report["counts"]["io"], [r for r in home.load_run(golden) if r["t"] == "io"].__len__(),
                           "rerunning by default starts more check processes")
        diff = attest.trace_diff(home.load_run(golden), home.load_run(candidate), None)
        self.assertFalse(diff["diff_empty"])

    def test_build_thread_example_records_and_a_weakened_accept_is_caught(self):
        import subprocess
        home = ledger.Ledger(self.scratch / "ledger")
        declared = json.loads((ROOT / "docs" / "architecture.declared.json").read_text(encoding="utf-8"))
        graph = space.index(ROOT, paths=["syberlabs", "syberwork"], revision="checkout")
        space.map_components(graph, declared)
        home.record_space(graph)
        golden = harness.new_run_id()
        report = harness.record(ROOT, home.run_path(golden), str(ROOT / "examples" / "build_thread.py"),
                                file_components=file_components(graph), run_id=golden, revision="checkout",
                                blobs=home.home / "blobs")
        self.assertEqual(report["status"], "ok", report.get("error"))
        self.assertGreater(report["counts"]["call"], 1000)
        self.assertGreater(report["counts"]["io"], 20, "git subprocesses are observed")
        home.record_run(golden)
        again = harness.new_run_id()
        harness.record(ROOT, home.run_path(again), str(ROOT / "examples" / "build_thread.py"),
                       file_components=file_components(graph), run_id=again, revision="checkout", blobs=home.home / "blobs")
        home.record_run(again)
        same = attest.trace_diff(home.load_run(golden), home.load_run(again), None)
        self.assertTrue(same["diff_empty"], same["divergence_detail"])
        self.assertTrue(same["function_level_equal"], "the example is deterministic once io keys are masked")

        tree = self.scratch / "weakened"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", ".syberlabs", "*.sqlite3"))
        gitspace = tree / "syberlabs" / "gitspace.py"
        text = gitspace.read_text(encoding="utf-8")
        weakened = text.replace('self.git("update-ref", "-m", message, ref, new, old or self.zero, env=WRITER)',
                                'self.git("update-ref", "-m", message, ref, new, env=WRITER)')
        self.assertNotEqual(text, weakened)
        gitspace.write_text(weakened, encoding="utf-8")
        candidate = harness.new_run_id()
        report = harness.record(tree, home.run_path(candidate), str(tree / "examples" / "build_thread.py"),
                                file_components=file_components(graph), run_id=candidate, revision="weakened",
                                blobs=home.home / "blobs")
        self.assertEqual(report["status"], "ok", report.get("error"))
        home.record_run(candidate)
        changed = attest.trace_diff(home.load_run(golden), home.load_run(candidate), None)
        self.assertFalse(changed["diff_empty"])
        detail = changed["divergence_detail"]
        self.assertEqual(detail["before"]["name"], "subprocess")
        self.assertIn("update-ref", detail["before"]["payload"]["argv"])
        self.assertEqual(len(detail["before"]["payload"]["argv"]) - len(detail["after"]["payload"]["argv"]), 1,
                         "the expected old value is the argument that went missing")
        statement = attest.statement(subject_commit="weakened", repository="syberwork", spec_ref={"spec_id": None},
                                     footprint=None, evidence={"trace_diff": changed}, builder={"agent": "test"})
        self.assertFalse(statement["predicate"]["policy"]["promote"])


class Variants(Scratch):
    """Several proposers mutate one parent spec, one reviewer judges, the accepted variant becomes the next version.
    Proposers and reviewer are commands; the one that is not independent is refused."""

    def setUp(self):
        super().setUp()
        import sys as _sys
        self.py = _sys.executable
        self.tree = self.scratch / "tree"
        shutil.copytree(FIXTURE, self.tree)
        self.proposers = str(self.tree / "proposers.py")
        self.home = ledger.Ledger(self.scratch / "ledger")
        graph = indexed(self.tree, "base")
        self.home.record_space(graph)
        spec = specmod.new_spec("order-path", "ordering", [
            {"req_id": "R1", "text": "When quantity is positive, the system shall post one order.",
             "scenarios": [{"given": "quantity 2", "when": "run", "then": "one http.post_order effect"}]}],
            ["ingest", "rules", "effects"], author=ACTOR)
        specmod.transition(spec, "validate", ACTOR, space=graph)
        self.spec_path = self.tree / "order-path.v1.json"
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = factory_cli.main(["--home", str(self.home.home), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_primitives(self):
        parent = json.loads(self.spec_path.read_text(encoding="utf-8"))
        graph = self.home.latest_space()
        a, b = specmod.party("model", "a", {"model": "claude-sonnet-4-5"}), specmod.party("model", "b", {"model": "grok-4"})
        self.assertEqual((a["family"], b["family"]), ("anthropic", "xai"))
        good = specmod.make_variant(parent, {"modified": [{"req_id": "R1", "text": "tighter"}], "rationale": "r"}, a, graph)
        bad = specmod.make_variant(parent, {"footprint_hint": ["ghost"]}, b, graph)
        self.assertEqual((good["state"], good["version"], good["findings"]), ("mutated", 2, []))
        self.assertTrue(bad["findings"])
        self.assertNotEqual(good["variant_id"], bad["variant_id"])
        self.assertEqual(parent["version"], 1, "the parent is untouched")
        with self.assertRaises(Rejected):
            specmod.make_variant(parent, {"rationale": "nothing"}, a, graph)
        reviewer = specmod.party("model", "r", {"signal": {"harness": {"model": "gpt-5"}}})
        self.assertEqual(reviewer["family"], "openai")
        review = specmod.judge([good, bad], [{"variant_id": good["variant_id"], "decision": "accept"},
                                             {"variant_id": bad["variant_id"], "decision": "reject"}], reviewer)
        self.assertEqual(review["accepted"], good["variant_id"])
        with self.assertRaises(Rejected):
            specmod.judge([good, bad], [{"variant_id": bad["variant_id"], "decision": "accept"},
                                        {"variant_id": good["variant_id"], "decision": "reject"}], reviewer)
        with self.assertRaises(Rejected):
            specmod.judge([good, bad], [{"variant_id": good["variant_id"], "decision": "accept"}], reviewer)
        # Independence is the provider family, not the name: another name on the proposer's family is refused, and
        # so is a reviewer whose family is not recorded.
        for not_independent in (specmod.party("model", "other", {"model": "claude-opus-4-1"}), specmod.party("model", "r", None)):
            with self.assertRaises(Rejected) as refused:
                specmod.judge([good], [{"variant_id": good["variant_id"], "decision": "accept"}], not_independent)
            self.assertEqual(refused.exception.code, "reviewer_not_independent")

    def test_loop_through_the_cli(self):
        run = lambda role: f"{self.py} {self.proposers} {role}"
        code, out, err = self.cli("variants", str(self.spec_path), "--proposer", f"claude={run('tighten')}",
                                  "--proposer", f"grok={run('overreach')}", "--proposer", f"cursor={run('broken')}",
                                  "--reviewer", f"gpt={run('review')}", "--objective", "make R1 testable")
        self.assertEqual(code, 0, err + out)
        self.assertIn("proposer cursor: provider_", err, "a proposer that returns no JSON is reported, not fatal")
        self.assertIn("variant ", out)
        self.assertIn("accept ", out)
        self.assertIn("reject ", out)
        target = self.tree / "order-path.v2.json"
        self.assertTrue(target.exists(), out)
        accepted = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(accepted["state"], "candidate")
        self.assertIn("exactly one order", accepted["requirements"][0]["text"])
        self.assertEqual([h["operation"] for h in accepted["history"]], ["validate", "mutate", "validate"])
        self.assertEqual(accepted["history"][1]["actor"], {"kind": "model", "id": "claude", "model": "claude-sonnet-4-5", "family": "anthropic"})
        review = json.loads((self.tree / "order-path.variants" / "review.json").read_text(encoding="utf-8"))
        self.assertEqual({v["decision"] for v in review["verdicts"]}, {"accept", "reject"})
        self.assertEqual(review["verdicts"][0]["reviewer"]["family"], "openai")
        kinds = [(e["body"]["operation"], e["body"].get("to"), e["body"]["actor"]["id"]) for e in self.home.events() if e["kind"] == "spec_changed"]
        self.assertIn(("mutate", "mutated", "claude"), kinds)
        self.assertIn(("mutate", "mutated", "grok"), kinds)
        self.assertIn(("mutate", None, "cursor"), kinds, "the failed proposer is on the record")
        self.assertIn(("review", None, "gpt"), kinds)
        self.assertEqual(kinds[-1][:2], ("validate", "candidate"))
        verdict_id = out.split("--verdict ")[1].split()[0]
        self.assertNotIn("--actor model:", out)
        code, out, err = self.cli("spec", "promote", str(target), "--verdict", verdict_id, "--actor", "model:gpt",
                                  "--tickets", str(self.tree / "tickets"))
        self.assertEqual(code, 2, "the reviewer cannot promote what it reviewed, nor can any model")
        self.assertIn("spec_actor", err)
        os.environ["SYBERLABS_FACTORY_TEST_SEED"] = OWNER_SEED.hex()
        self.addCleanup(os.environ.pop, "SYBERLABS_FACTORY_TEST_SEED", None)
        (self.tree / "owner.pub").write_text(OWNER_PUBLIC.hex(), encoding="utf-8")
        approval = self.tree / "approval.json"
        code, out, err = self.cli("approve", str(target), "--verdict", verdict_id, "--actor", "human:mateo",
                                  "--seed-env", "SYBERLABS_FACTORY_TEST_SEED", "--out", str(approval))
        self.assertEqual(code, 0, err)
        # The spec edited after review is not the variant that was reviewed.
        edited = json.loads(target.read_text(encoding="utf-8"))
        edited["requirements"][0]["text"] += " (and something the reviewer never saw)"
        target.write_text(json.dumps(edited), encoding="utf-8")
        code, out, err = self.cli("spec", "promote", str(target), "--verdict", verdict_id, "--actor", "human:mateo",
                                  "--approval", str(approval), "--owner-public", str(self.tree / "owner.pub"))
        self.assertEqual(code, 2)
        self.assertIn("spec_verdict", err)
        target.write_text(json.dumps(accepted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        code, out, err = self.cli("spec", "promote", str(target), "--verdict", verdict_id, "--actor", "human:mateo",
                                  "--approval", str(approval), "--owner-public", str(self.tree / "owner.pub"),
                                  "--tickets", str(self.tree / "tickets"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list((self.tree / "tickets").glob("*.json"))), 1)
        self.assertTrue(self.home.verify())

    def test_a_reviewer_of_the_proposers_family_is_refused_and_a_review_accepting_two_is_refused(self):
        run = lambda role, model="": f"{self.py} {self.proposers} {role} {model}".rstrip()
        code, out, err = self.cli("variants", str(self.spec_path), "--proposer", f"a={run('tighten', 'gpt-5')}",
                                  "--reviewer", f"b={run('review')}")
        self.assertEqual(code, 2)
        self.assertIn("reviewer_not_independent", err)
        self.assertIn("both served by openai", err, "same family under two names")
        code, out, err = self.cli("variants", str(self.spec_path), "--proposer", f"a={run('tighten')}",
                                  "--reviewer", f"r={run('review', 'none')}")
        self.assertEqual(code, 2)
        self.assertIn("reviewer_not_independent", err)
        self.assertIn("not recorded", err, "a reviewer that declares no model is refused, not trusted")
        code, out, err = self.cli("variants", str(self.spec_path), "--proposer", f"a={run('tighten')}",
                                  "--proposer", f"b={run('tighten')}", "--reviewer", f"r={run('review-all')}")
        self.assertEqual(code, 2)
        self.assertIn("at most one variant", err)
        self.assertFalse((self.tree / "order-path.v2.json").exists())


class FeatureMaps(Scratch):
    """Vectors per symbol and component from code, docs and trace statistics; nearest, moved, impact; the third
    ticket source."""

    def test_tokens_and_vectors(self):
        toks = features.tokens("def post_order(record, decision):\n    '''Stands in for an HTTP call.'''\n    return rules.label(decision)\n")
        self.assertIn("post", toks)
        self.assertIn("order", toks)
        self.assertIn("kw:def", toks)
        self.assertIn("http", toks)
        self.assertIn("post_order", toks, "subword bigrams keep the identifier together")
        a = features.vectorize("def post_order(record): return http.post(record)")
        b = features.vectorize("def post_order(record): return http.post(record)", {"runs": 3, "calls": 9, "io": 2})
        c = features.vectorize("def normalize(record): record['part'] = record['part'].upper()")
        self.assertEqual(len(a), features.DIM + features.TRACE_DIM)
        self.assertAlmostEqual(features.cosine(a[:features.DIM], a[:features.DIM]), 1.0, places=6)
        self.assertEqual(a[:features.DIM], b[:features.DIM], "text features do not depend on the trace")
        self.assertGreater(features.cosine(a, b), features.cosine(a, c), "trace statistics move a symbol a little, text moves it a lot")
        self.assertGreater(features.cosine(a, b), 0.85)

    def test_build_nearest_moved_impact_and_tickets(self):
        home = ledger.Ledger(self.scratch / "ledger")
        graph = indexed()
        home.record_space(graph)
        run_id, _, _ = self.record(home, graph=graph)
        counts = features.build(home, graph, FIXTURE)
        self.assertEqual((counts["revision"], counts["components"]), ("fixture", 4))
        self.assertGreaterEqual(counts["with_trace"], 5, "symbols the recorded run touched carry trace statistics")
        hits = features.nearest(home, "fixture", "post an order over http and return its id", 3)
        self.assertEqual({h[0] for h in hits[:2]}, {"s:fxapp/effects.py::post_order", "s:fxapp/effects.py::submit"}, hits)
        comps = features.nearest(home, "fixture", "decide normalize label quantity", 2, kind="component")
        self.assertEqual(comps[0][0], "rules", comps)
        tree = self.scratch / "changed"
        shutil.copytree(FIXTURE, tree)
        rules = tree / "fxapp" / "rules.py"
        rules.write_text(rules.read_text(encoding="utf-8").replace(
            'def decide(record):\n    if record["quantity"] <= 0:',
            'def decide(record):\n    """Route a record: reject, review or order, by quantity and a fraud flag."""\n'
            '    if record.get("fraud") or record["quantity"] <= 0:'), encoding="utf-8")
        after = indexed(tree, "changed")
        home.record_space(after)
        features.build(home, after, tree)
        self.assertEqual(features.revisions(home), ["fixture", "changed"])
        moved = features.moved(home, "fixture", "changed")
        self.assertEqual(moved[0]["id"], "s:fxapp/rules.py::decide", moved)
        self.assertTrue(all(e["id"] == "s:fxapp/rules.py::decide" for e in moved if e["distance"] > 0.001), moved)
        tickets = specmod.tickets_from_moved(moved, after, set(), threshold=0.05, before="fixture", after="changed")
        self.assertEqual([t["footprint"]["components"] for t in tickets], [["rules"]])
        self.assertEqual(tickets[0]["footprint"]["files"], ["fxapp/rules.py"])
        self.assertEqual(specmod.tickets_from_moved(moved, after, {"rules"}, threshold=0.05, before="fixture", after="changed"), [],
                         "a claimed component opens nothing")
        report = features.impact(graph, ["rules"])
        self.assertEqual(report["dependents"], ["effects", "ingest", "main"])
        self.assertIn("s:fxapp/main.py::run", report["referencing_symbols"])
        with self.assertRaises(Rejected):
            features.impact(graph, ["ghost"])
        with self.assertRaises(Rejected):
            features.nearest(home, "nowhere", "x")

    def test_map_cli(self):
        home = ledger.Ledger(self.scratch / "ledger")
        tree = self.scratch / "tree"
        shutil.copytree(FIXTURE, tree)
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(tree)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                codes = [factory_cli.main(["--home", str(home.home), "map", "build", "--declared", "architecture.json", "--revision", "a"])]
                (tree / "fxapp" / "rules.py").write_text((tree / "fxapp" / "rules.py").read_text(encoding="utf-8") +
                                                         "\n\ndef audit(record):\n    return {'audited': record['part']}\n", encoding="utf-8")
                codes.append(factory_cli.main(["--home", str(home.home), "map", "build", "--declared", "architecture.json", "--revision", "b"]))
                codes.append(factory_cli.main(["--home", str(home.home), "map", "nearest", "audit a record", "-k", "2"]))
                codes.append(factory_cli.main(["--home", str(home.home), "map", "moved", "--before", "a", "--after", "b",
                                               "--ticket", str(self.scratch / "tickets")]))
                codes.append(factory_cli.main(["--home", str(home.home), "map", "impact", "rules"]))
        finally:
            os.chdir(here)
        self.assertEqual(codes, [0, 0, 0, 0, 0], err.getvalue())
        text = out.getvalue()
        self.assertIn("s:fxapp/rules.py::audit", text.split("audit a record")[0] if "audit a record" in text else text)
        self.assertIn("added    s:fxapp/rules.py::audit", text)
        self.assertIn("ticket     ", text)
        self.assertEqual([e["body"]["source"] for e in home.events() if e["kind"] == "ticket_opened"], ["feature_map"])
        self.assertIn("dependents of ['rules']: ['effects', 'ingest', 'main']", text)
