"""SyberEval core: spec registration and refusal, task digests and fixture import, arms, preflight gating, the CLI.

The runner, the hidden-check isolation and the bundles are in ``tests/test_eval_runner.py``.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from syberlabs.canonical import digest
from syberlabs.errors import Rejected
from syberlabs.eval import arms, runner, spec, tasks

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
SIM = ["{python}", "{harness}/benchmarks/simulated_model.py"]
PRICES = {"simulated": {"input_per_million": 2.0, "output_per_million": 10.0}}


def tiny_spec(ids, digests, **changes):
    doc = {"schema": spec.SPEC_SCHEMA_ID, "id": "tiny", "question": "Does repair pass more often?",
           "hypothesis": "Repair passes at least as often.",
           "arms": [{"name": "single", "kind": "single", "model": "simulated", "effort": "none", "adapter": SIM},
                    {"name": "repair", "kind": "repair", "model": "simulated", "effort": "none", "adapter": SIM,
                     "max_calls": 3}],
           "tasks": {"ids": list(ids), "set_digest": tasks.set_digest(digests)},
           "primary_metric": {"name": "hidden_pass_rate", "definition": "accepted and every hidden check passed"},
           "secondary_metrics": [],
           "budget": {"calls": 3, "dollars": 0.5, "seconds": 600, "total_dollars": 5.0, "prices": PRICES},
           "seeds": [1, 2], "order_seed": 7, "exclusions": [],
           "decision_rule": {"metric": "hidden_pass_rate", "comparison": "paired_difference", "treatment": "repair",
                             "baseline": "single", "operator": ">=", "threshold": 0.0, "unit": "task",
                             "aggregation": "mean_over_seeds"},
           "reporting": ["every run"], "hidden_feedback": "tails"}
    doc.update(changes)
    return doc


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.test"})
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


class Workspace(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="syberlabs-evaltest-"))
        self.root = self.folder / "evals"
        self.written = tasks.import_fixtures(self.root / "tasks", ["pricing", "slugify"])
        self.ids = [w["id"] for w in self.written]
        self.digests = {w["id"]: w["digest"] for w in self.written}

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def write_spec(self, doc):
        path = self.root / doc["id"] / "spec.json"
        spec.write_json(path, doc)
        return path


class SpecRegistration(Workspace):
    def test_register_freezes_the_digests_and_run_refuses_a_modified_spec(self):
        path = self.write_spec(tiny_spec(self.ids, self.digests))
        found = spec.register(path)
        self.assertEqual(found["spec_digest"], spec.spec_digest(json.loads(path.read_text(encoding="utf-8"))))
        self.assertEqual(found["task_digests"], self.digests)
        self.assertEqual(set(found["arm_digests"]), {"single", "repair"})
        self.assertEqual(found["schema"], spec.REGISTRATION_SCHEMA_ID)
        self.assertIn("git_commit", found)
        self.assertIn("registered_at", found)
        self.assertEqual(spec.register(path), found, "registering the same spec again returns the registration")
        doc, registration = spec.registered(path)
        self.assertEqual(registration, found)
        changed = dict(doc, question="Something else?")
        spec.write_json(path, changed)
        with self.assertRaises(Rejected) as caught:
            spec.registered(path)
        self.assertEqual(caught.exception.code, "spec_modified")
        with self.assertRaises(Rejected) as caught:
            runner.run_eval(self.root, "tiny", require_preflight=False)
        self.assertEqual(caught.exception.code, "spec_modified")
        with self.assertRaises(Rejected) as caught:
            spec.register(path)
        self.assertEqual(caught.exception.code, "already_registered")

    def test_the_digest_is_of_the_parsed_spec_not_its_bytes(self):
        doc = tiny_spec(self.ids, self.digests)
        path = self.write_spec(doc)
        spec.register(path)
        path.write_bytes(json.dumps(doc, indent=7).replace("\n", "\r\n").encode())
        spec.registered(path)  # whitespace and line endings do not move it

    def test_an_unregistered_spec_is_refused(self):
        path = self.write_spec(tiny_spec(self.ids, self.digests))
        with self.assertRaises(Rejected) as caught:
            spec.registered(path)
        self.assertEqual(caught.exception.code, "spec_unregistered")

    def test_a_task_changed_after_registration_is_refused(self):
        path = self.write_spec(tiny_spec(self.ids, self.digests))
        spec.register(path)
        hidden = self.root / "tasks" / self.ids[0] / "hidden" / "checks.json"
        hidden.write_bytes(hidden.read_bytes().replace(b"assert", b"assert  "))
        with self.assertRaises(Rejected) as caught:
            spec.registered(path)
        self.assertEqual(caught.exception.code, "task_modified")
        self.assertIn(self.ids[0], caught.exception.detail)

    def test_the_declared_set_digest_must_be_the_tasks_as_they_are(self):
        doc = tiny_spec(self.ids, self.digests)
        doc["tasks"]["set_digest"] = "0" * 64
        with self.assertRaises(Rejected) as caught:
            spec.register(self.write_spec(doc))
        self.assertEqual(caught.exception.code, "task_set_mismatch")

    def test_the_schema_and_the_cross_field_rules(self):
        good = tiny_spec(self.ids, self.digests)
        self.assertEqual(spec.check(good), [])
        cases = {
            "missing": ({k: v for k, v in good.items() if k != "seeds"}, "$.seeds: required"),
            "unknown": (dict(good, extra=1), "$.extra: unknown field"),
            "bool is not an integer": (dict(good, order_seed=True), "$.order_seed: expected integer"),
            "decision arm": (dict(good, decision_rule=dict(good["decision_rule"], treatment="evolve")), "not an arm"),
            "decision metric": (dict(good, decision_rule=dict(good["decision_rule"], metric="other")), "primary"),
            "single calls": (dict(good, arms=[dict(good["arms"][0], max_calls=3), good["arms"][1]]), "one call"),
            "claude option": (dict(good, arms=[dict(good["arms"][0], githits=True), good["arms"][1]]),
                              "only for claude-code"),
            "duplicate arms": (dict(good, arms=[good["arms"][0], good["arms"][0]]), "distinct"),
            "run over total": (dict(good, budget=dict(good["budget"], dollars=9.0)), "total_dollars"),
            "bad prices": (dict(good, budget=dict(good["budget"], prices={"m": {"input_per_million": -1}})), "prices"),
            "unit": (dict(good, decision_rule=dict(good["decision_rule"], unit="run")), "unit"),
            "hidden_feedback required": ({k: v for k, v in good.items() if k != "hidden_feedback"},
                                         "$.hidden_feedback: required"),
            "hidden_feedback mode": (dict(good, hidden_feedback="tail"), "$.hidden_feedback: must be one of"),
            "evolve under status": (dict(good, hidden_feedback="status",
                                         arms=[good["arms"][0], dict(good["arms"][1], kind="evolve")]),
                                    "an evolve arm runs only under hidden_feedback tails"),
        }
        for label, (doc, expected) in cases.items():
            with self.subTest(label):
                errors = spec.check(doc)
                self.assertTrue(any(expected in e for e in errors), errors)
        for mode in ("tails", "status", "none"):
            self.assertEqual(spec.check(dict(good, hidden_feedback=mode)), [])
        self.assertNotEqual(spec.spec_digest(good), spec.spec_digest(dict(good, hidden_feedback="none")),
                            "the mode is part of the spec's digest")
        with self.assertRaises(Rejected) as caught:
            spec.register(self.write_spec(dict(good, seeds=[])))
        self.assertEqual(caught.exception.code, "invalid_spec")


class Tasks(Workspace):
    def test_the_digest_moves_with_a_hidden_file_and_task_json_but_not_with_line_endings(self):
        task_id = self.ids[0]
        hidden = self.root / "tasks" / task_id / "hidden" / "checks.json"
        before = tasks.task_digest(self.root / "tasks", task_id)
        original = hidden.read_bytes()
        hidden.write_bytes(original.replace(b"\n", b"\r\n"))
        self.assertEqual(tasks.task_digest(self.root / "tasks", task_id), before, "CRLF is normalized")
        hidden.write_bytes(original.replace(b"100", b"101", 1))
        self.assertNotEqual(tasks.task_digest(self.root / "tasks", task_id), before, "one byte of a hidden check")
        hidden.write_bytes(original)
        (hidden.parent / "data.bin").write_bytes(b"\x00\r\n")
        self.assertNotEqual(tasks.task_digest(self.root / "tasks", task_id), before, "a new hidden file")
        (hidden.parent / "data.bin").unlink()
        self.assertEqual(tasks.task_digest(self.root / "tasks", task_id), before)
        task_json = self.root / "tasks" / task_id / "task.json"
        doc = json.loads(task_json.read_text(encoding="utf-8"))
        doc["objective"] += "."
        task_json.write_text(json.dumps(doc), encoding="utf-8")
        self.assertNotEqual(tasks.task_digest(self.root / "tasks", task_id), before, "task.json")

    def test_fixture_import_moves_every_check_to_hidden_and_none_into_the_repository(self):
        from benchmarks.fixtures import FIXTURES

        for name, task_id in (("pricing", "v1-pricing"), ("slugify", "v1-slugify")):
            folder = self.root / "tasks" / task_id
            task = tasks.load_task(self.root / "tasks", task_id)
            bodies = json.loads((folder / "hidden" / "checks.json").read_text(encoding="utf-8"))
            self.assertEqual(bodies, {n: c["argv"][2] for n, c in FIXTURES[name]["checks"].items()})
            self.assertEqual(task["checks"], {}, "the fixtures have no visible check")
            self.assertEqual(set(task["hidden"]["checks"]), set(FIXTURES[name]["checks"]))
            task_text = (folder / "task.json").read_text(encoding="utf-8")
            for body in bodies.values():
                self.assertNotIn(body, task_text, "a check's code is in hidden/, never in task.json")
            repo = self.folder / f"repo-{name}"
            base = tasks.materialize(task, repo)
            self.assertEqual(sorted(git(repo, "ls-files").splitlines()), sorted(FIXTURES[name]["files"]))
            blobs = git(repo, "rev-list", "--all", "--objects").splitlines()
            content = b"".join(subprocess.run(["git", "-C", str(repo), "cat-file", "-p", line.split()[0]],
                                              capture_output=True, check=True).stdout for line in blobs)
            for body in bodies.values():
                self.assertNotIn(body.encode(), content)
            on_disk = b"".join(p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts)
            for check_name in bodies:
                self.assertNotIn(bodies[check_name].encode(), on_disk)
            self.assertEqual(tasks.materialize(task, self.folder / f"again-{name}"), base,
                             "the same base commit on every run")

    def test_import_is_deterministic_and_refuses_to_overwrite_another_task(self):
        again = tasks.import_fixtures(self.root / "tasks", ["pricing", "slugify"])
        self.assertEqual(again, self.written)
        hidden = self.root / "tasks" / "v1-pricing" / "hidden" / "checks.json"
        hidden.write_text("{}", encoding="utf-8")
        with self.assertRaises(Rejected) as caught:
            tasks.import_fixtures(self.root / "tasks", ["pricing"])
        self.assertEqual(caught.exception.code, "task_exists")
        self.assertEqual(tasks.import_fixtures(self.root / "tasks", ["pricing"], force=True), self.written[:1])

    def test_a_fixture_whose_files_changed_is_refused(self):
        task = tasks.load_task(self.root / "tasks", "v1-pricing")
        task["source_digest"] = "0" * 64
        with self.assertRaises(Rejected) as caught:
            tasks.materialize(task, self.folder / "repo")
        self.assertEqual(caught.exception.code, "task_source_changed")

    def test_hidden_checks_are_staged_outside_and_named_by_absolute_path(self):
        task = tasks.load_task(self.root / "tasks", "v1-pricing")
        with self.assertRaises(Rejected) as caught:
            tasks.stage_hidden(self.root / "tasks", "v1-pricing", self.folder / "bad", "0" * 64)
        self.assertEqual(caught.exception.code, "task_modified")
        staged = tasks.stage_hidden(self.root / "tasks", "v1-pricing", self.folder / "host" / "hidden",
                                    self.digests["v1-pricing"])
        self.addCleanup(tasks.release, staged)
        files = tasks.tree_digests(staged)
        self.assertEqual(digest({"task": task, "hidden": files}), self.digests["v1-pricing"],
                         "the copy re-hashes to the task")
        for path in staged.rglob("*"):
            if path.is_file():
                self.assertFalse(os.access(path, os.W_OK), f"{path.name} is read-only")
        checks, required = tasks.contract_checks(task, staged)
        self.assertEqual(sorted(checks), ["hidden.discount", "hidden.shipping", "hidden.tax"])
        self.assertEqual(required, sorted(checks))
        for check in checks.values():
            self.assertEqual(check["argv"][0], sys.executable)
            self.assertEqual(check["argv"][2], tasks.LOADER)
            self.assertTrue(Path(check["argv"][3]).is_absolute())
            self.assertEqual(Path(check["argv"][3]).parent, staged)

    def test_a_visible_check_may_not_name_the_hidden_directory(self):
        task = tasks.load_task(self.root / "tasks", "v1-pricing")
        bad = dict(task, checks={"peek": {"argv": ["{python}", "{hidden}/checks.json"]}})
        self.assertTrue(any("may not name the hidden" in e for e in tasks.check_task(bad)))
        bad = dict(task, checks={"hidden.x": {"argv": ["{python}", "-V"]}})
        self.assertTrue(tasks.check_task(bad))
        self.assertTrue(tasks.check_task(dict(task, hidden={"checks": {}})))

    def test_the_loader_shows_the_assertion_but_no_source_line_or_path(self):
        staged = self.folder / "host"
        staged.mkdir()
        body = "# secret-comment-4f1c\nimport sys\nassert sys.argv[1:] == ['x'], 'the message is the feedback'\n"
        (staged / "checks.json").write_text(json.dumps({"c": body}), encoding="utf-8")
        done = subprocess.run([sys.executable, "-c", tasks.LOADER, str(staged / "checks.json"), "c", "y"],
                              capture_output=True, text=True, cwd=self.folder)
        self.assertEqual(done.returncode, 1)
        self.assertIn("the message is the feedback", done.stderr)
        self.assertIn("<hidden:c>", done.stderr)
        self.assertNotIn("secret-comment", done.stderr)
        self.assertNotIn("assert sys.argv", done.stderr)
        self.assertNotIn(str(staged), done.stderr)
        ok = subprocess.run([sys.executable, "-c", tasks.LOADER, str(staged / "checks.json"), "c", "x"],
                            capture_output=True, text=True, cwd=self.folder)
        self.assertEqual(ok.returncode, 0, ok.stderr)


class CommittedEvals(unittest.TestCase):
    def test_the_committed_tasks_are_what_the_importer_writes(self):
        from benchmarks.fixtures import V3, V4

        committed = sorted(p.name for p in (EVALS / "tasks").iterdir() if (p / "task.json").is_file())
        self.assertEqual(committed, sorted([f"v3-{name}" for name in V3] + [f"v4-{name}" for name in V4]))
        with tempfile.TemporaryDirectory() as folder:
            fresh = tasks.import_fixtures(Path(folder))
            self.assertEqual({w["id"]: w["digest"] for w in fresh},
                             {task_id: tasks.task_digest(EVALS / "tasks", task_id) for task_id in committed})

    def test_e5_is_registered_from_clean_main_and_not_run(self):
        # eval-score added the required hidden_feedback field, so E5's digest moved and its stale registration was
        # deleted (register returns an existing registration with the same digest unchanged). E5 was registered again
        # on a clean checkout of main (e5-register). registered() refuses a spec, task or arm that moved since.
        path = EVALS / "e5-single-vs-repair" / "spec.json"
        doc, registration = spec.registered(path)
        self.assertEqual(doc["hidden_feedback"], "tails", "E5 matches E3: hidden checks' tails reach repair")
        self.assertEqual(registration["spec_digest"], spec.spec_digest(doc))
        self.assertEqual(registration["task_set_digest"], doc["tasks"]["set_digest"])
        self.assertIs(registration["git_dirty"], False, "registered from a clean tree")
        self.assertRegex(registration["git_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual([a["name"] for a in doc["arms"]], ["single", "repair"])
        self.assertEqual({a["model"] for a in doc["arms"]}, {"claude-sonnet-5-5"})
        self.assertEqual({a["effort"] for a in doc["arms"]}, {"medium"})
        self.assertEqual(doc["arms"][1]["max_calls"], 6)
        self.assertEqual(len(doc["tasks"]["ids"]), 8)
        self.assertEqual(doc["seeds"], [1, 2, 3])
        self.assertEqual((doc["decision_rule"]["treatment"], doc["decision_rule"]["baseline"],
                          doc["decision_rule"]["operator"]), ("repair", "single", ">="))
        self.assertEqual(len(runner.plan(doc)), 48)
        self.assertFalse((EVALS / "e5-single-vs-repair" / "runs").exists(), "E5 is not run")
        self.assertFalse((EVALS / "e5-single-vs-repair" / "spend.jsonl").exists(), "no E5 run was attempted")


class Arms(unittest.TestCase):
    BUDGET = {"calls": 6, "dollars": 1.0, "seconds": 60, "total_dollars": 2.0, "prices": PRICES}

    def test_each_kind_builds_the_kernel_provider(self):
        from syberlabs.evolve import EvolutionaryProvider, FixedPolicy, JevPolicy
        from syberlabs.providers import CommandProvider, RepairProvider

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        base = {"model": "simulated", "effort": "none", "adapter": SIM}
        single = arms.build(dict(base, name="single", kind="single"), repo=repo, budget=self.BUDGET, prices=PRICES)
        self.assertIsInstance(single, CommandProvider)
        self.assertEqual(single.argv[0], sys.executable)
        self.assertTrue(single.argv[1].endswith("simulated_model.py") and Path(single.argv[1]).is_file())
        repair = arms.build(dict(base, name="repair", kind="repair", max_calls=4), repo=repo, budget=self.BUDGET,
                            prices=PRICES)
        self.assertIsInstance(repair, RepairProvider)
        self.assertEqual(repair.max_calls, 4)
        self.assertEqual(arms.calls(dict(base, name="r", kind="repair"), self.BUDGET), 6)
        evolve = arms.build(dict(base, name="evolve", kind="evolve"), repo=repo, budget=self.BUDGET, prices=PRICES)
        self.assertIsInstance(evolve, EvolutionaryProvider)
        self.assertIsInstance(evolve.policy, FixedPolicy)
        self.assertEqual((evolve.mutate.max_calls, evolve.population), (6, 4))
        jev = arms.build(dict(base, name="jev", kind="evolve_jev"), repo=repo, budget=self.BUDGET, prices=PRICES)
        self.assertIsInstance(jev.policy, JevPolicy)
        self.assertGreater(jev.policy.prices["mutate"], 0)

    def test_claude_code_passes_its_options_and_githits_by_name(self):
        from syberlabs.claude_code import ClaudeCodeProvider

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        git(repo, "init", "-q")  # the provider reads its repository with git
        arm = {"name": "cc", "kind": "claude-code", "model": "claude-sonnet-5-5", "effort": "medium",
               "lane": "subscription_local", "trusted_tree": True, "tools": ["Read", "Edit"], "max_turns": 8,
               "githits": True, "executable": ["{python}", "-c", "print('2.1.309 (Claude Code)')"]}  # a stand-in
        self.assertEqual(arms.check(arm, self.BUDGET), [])
        from unittest import mock

        from syberlabs import githits

        # Construction only: the provider wants the token's variable set; no request is made.
        with mock.patch.dict(os.environ, {githits.TOKEN_ENV: "test-not-a-token"}):
            provider = arms.build(arm, repo=repo, budget=self.BUDGET, prices=PRICES)
        self.assertIsInstance(provider, ClaudeCodeProvider)
        self.assertTrue(provider.githits)
        self.assertEqual(provider.max_calls, 6)
        self.assertNotEqual(arms.arm_digest(arm), arms.arm_digest(dict(arm, githits=False)))
        self.assertTrue(arms.check(dict(arm, adapter=SIM), self.BUDGET))
        self.assertTrue(arms.check(dict(arm, bare=True), self.BUDGET), "--bare with a login lane")

    def test_the_environment_names_the_model_and_withholds_the_seed_and_tokens(self):
        arm = {"name": "single", "kind": "single", "model": "simulated", "effort": "low", "adapter": SIM}
        task = {"source": "fixture:pricing"}
        saved = {k: os.environ.get(k) for k in ("SYBERLABS_EVAL_TEST_ONLY_SEED", "GITHUB_TOKEN", "SYBERLABS_MODEL")}
        os.environ["SYBERLABS_EVAL_TEST_ONLY_SEED"] = "not-a-seed"
        os.environ["GITHUB_TOKEN"] = "not-a-token"
        try:
            with arms.environment(arm, task, withheld=("SYBERLABS_EVAL_TEST_ONLY_SEED",)):
                self.assertEqual((os.environ["SYBERLABS_MODEL"], os.environ["SYBERLABS_EFFORT"]), ("simulated", "low"))
                self.assertEqual(os.environ["SYBERLABS_FIXTURE"], "pricing")
                self.assertNotIn("SYBERLABS_EVAL_TEST_ONLY_SEED", os.environ)
                self.assertNotIn("GITHUB_TOKEN", os.environ)
            with arms.environment(dict(arm, model="claude-sonnet-5-5"), task):
                self.assertNotIn("SYBERLABS_FIXTURE", os.environ, "only the simulated model reads the fixture name")
            self.assertEqual(os.environ["GITHUB_TOKEN"], "not-a-token")
            self.assertEqual(os.environ.get("SYBERLABS_MODEL"), saved["SYBERLABS_MODEL"])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class Preflight(Workspace):
    PASS = (("ok", ["{python}", "-c", "print('fine')"]),)
    FAIL = (("ok", ["{python}", "-c", "print('fine')"]), ("bad", ["{python}", "-c", "raise SystemExit(3)"]))

    def setUp(self):
        super().setUp()
        self.harness = self.folder
        git(self.harness, "init", "-q")
        (self.harness / "README").write_text("harness\n", encoding="utf-8")
        git(self.harness, "add", "README")
        git(self.harness, "commit", "-qm", "one")
        spec.register(self.write_spec(tiny_spec(self.ids, self.digests)))

    def gate(self):
        with self.assertRaises(Rejected) as caught:
            runner.preflight_gate(self.root, "tiny", self.harness)
        return caught.exception.code

    def test_run_is_refused_until_a_preflight_passed_on_this_commit(self):
        self.assertEqual(self.gate(), "preflight_missing")
        with self.assertRaises(Rejected) as caught:
            runner.run_eval(self.root, "tiny", harness=self.harness)
        self.assertEqual(caught.exception.code, "preflight_missing")
        self.assertFalse((self.root / "tiny" / "runs").exists(), "nothing ran")
        dry = runner.run_eval(self.root, "tiny", harness=self.harness, dry=True)
        self.assertEqual((dry["preflight"], len(dry["runs"])), ("preflight_missing", 8))
        self.assertEqual({row["state"] for row in dry["runs"]}, {"pending"})

        found = runner.preflight(self.root, "tiny", harness=self.harness, commands=self.FAIL)
        self.assertFalse(found["passed"])
        self.assertEqual([r["exit_code"] for r in found["results"]], [0, 3])
        self.assertEqual(self.gate(), "preflight_failed")

        found = runner.preflight(self.root, "tiny", harness=self.harness, commands=self.PASS)
        self.assertTrue(found["passed"])
        self.assertEqual(found["commit"], git(self.harness, "rev-parse", "HEAD"))
        self.assertIn("fine", found["results"][0]["tail"])
        self.assertEqual(found["results"][0]["output_digest"],
                         hashlib.sha256(found["results"][0]["tail"].encode()).hexdigest())
        self.assertEqual(runner.preflight_gate(self.root, "tiny", self.harness)["commit"], found["commit"])
        self.assertEqual(runner.run_eval(self.root, "tiny", harness=self.harness, dry=True)["preflight"], "passed")

        (self.harness / "README").write_text("changed\n", encoding="utf-8")
        self.assertEqual(self.gate(), "preflight_dirty")
        git(self.harness, "commit", "-qam", "two")
        self.assertEqual(self.gate(), "preflight_stale")
        runner.preflight(self.root, "tiny", harness=self.harness, commands=self.PASS)
        runner.preflight_gate(self.root, "tiny", self.harness)
        (self.root / "tiny" / "runs").mkdir(parents=True)
        (self.root / "tiny" / "runs" / "note.txt").write_text("evals/ is where runs write\n", encoding="utf-8")
        runner.preflight_gate(self.root, "tiny", self.harness)  # untracked files under evals/ are not dirt

    def test_the_default_preflight_names_the_four_suites(self):
        argv = {name: " ".join(argv) for name, argv in runner.PREFLIGHT}
        self.assertEqual(set(argv), {"tests", "conformance", "scenarios", "eval_tests"})
        self.assertIn("scripts/run_shards.py", argv["tests"])
        self.assertIn("conformance.run", argv["conformance"])
        self.assertIn("benchmarks/run_scenarios.py", argv["scenarios"])
        self.assertIn("test_eval_*.py", argv["eval_tests"])


class Terminal(unittest.TestCase):
    def cli(self, *args):
        from syberlabs.cli import main

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(list(args))
        return code, out.getvalue()

    def cli_err(self, *args):
        from syberlabs.cli import main

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_score_and_report_of_e5_unregistered_and_registered_with_no_runs(self):
        # Every score and report runs on a copy of evals/: score writes index.json beside the spec, and no test may
        # write into the checkout (preflight records whether it differs from its commit).
        folder_e5 = EVALS / "e5-single-vs-repair"
        before = sorted(p.name for p in folder_e5.iterdir())
        artifacts = ("runs", "spend.jsonl", "index.json", "preflight.json", "report*")
        with tempfile.TemporaryDirectory() as folder:
            evals = Path(folder) / "evals"
            shutil.copytree(EVALS, evals, ignore=shutil.ignore_patterns(*artifacts, "registration.json"))
            for name in ("score", "report"):
                code, out, err = self.cli_err("eval", "--evals", str(evals), name, "e5-single-vs-repair")
                self.assertEqual(code, 2, out)
                self.assertIn("spec_unregistered", err)
        with tempfile.TemporaryDirectory() as folder:
            evals = Path(folder) / "evals"
            shutil.copytree(EVALS, evals, ignore=shutil.ignore_patterns(*artifacts))
            code, out, err = self.cli_err("eval", "--evals", str(evals), "score", "e5-single-vs-repair", "--json")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["runs"], {}, "a registered eval with no runs scores none")
            code, out, err = self.cli_err("eval", "--evals", str(evals), "report", "e5-single-vs-repair")
            self.assertEqual(code, 2, out)
            self.assertIn("runs_missing", err)
            self.assertIn("48 of 48", err)
        self.assertEqual(sorted(p.name for p in folder_e5.iterdir()), before, "nothing written into the checkout")

    def test_tasks_list_and_a_dry_run_of_e5(self):
        code, out = self.cli("eval", "tasks", "list", "--json")
        self.assertEqual(code, 0)
        found = json.loads(out)
        doc = json.loads((EVALS / "e5-single-vs-repair" / "spec.json").read_text(encoding="utf-8"))
        self.assertEqual(found["set_digest"], doc["tasks"]["set_digest"])
        with tempfile.TemporaryDirectory() as folder:  # the committed registration, on a copy: nothing written here
            evals = Path(folder) / "evals"
            shutil.copytree(EVALS, evals, ignore=shutil.ignore_patterns("runs", "spend.jsonl", "index.json"))
            code, out = self.cli("eval", "--evals", str(evals), "run", "e5-single-vs-repair", "--dry", "--json")
            found = json.loads(out)
            self.assertEqual((code, found["planned"], found["dry"]), (0, 48, True))
            self.assertEqual([row["order_index"] for row in found["runs"]], list(range(48)))
            self.assertEqual([row["run_id"] for row in found["runs"]], [p.run_id for p in runner.plan(doc)])
            code, _ = self.cli("eval", "--evals", str(evals), "run", "e5-single-vs-repair", "--dry", "--only",
                               "v3-journal_chain,repair,")
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
