"""SyberEval runner: real Build Threads end to end with no network, bundles with digests, the registered order,
resumption, the eval's dollar cap, no publishing, certification, and hidden checks the agent never holds.

Every run here uses ``benchmarks/simulated_model.py`` or a stand-in adapter written below; nothing calls a model.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.eval import arms, runner, spec, tasks
from tests.test_eval_core import PRICES, SIM, tiny_spec

# RFC 8032 test vector 1, the seed tests/test_certificate.py signs with; never a real key.
TEST_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
TEST_SEED_ENV = "SYBERLABS_EVAL_TEST_SEED"
CANARY_CODE = "canary-code-7d1e0b5a92"
CANARY_DATA = "canary-data-c43f81e06b"

# A stand-in model: logs each request it is given (and, as booleans only, which secrets reached its environment),
# then answers under the search protocol. The first answer is wrong in two parts and also writes a file where a
# careless host might look for the hidden checks; a repair (a request with feedback) fixes both parts. Its usage is
# fixed, so its host-priced cost is exactly 1000 x $2/M + 100 x $10/M = $0.003 per call.
STANDIN = r'''
import json, os, sys
from pathlib import Path

log = Path(sys.argv[1])
request = json.load(sys.stdin)
n = len(list(log.glob("request-*.json"))) + 1
(log / f"request-{n}.json").write_text(json.dumps(request), encoding="utf-8")
(log / f"env-{n}.json").write_text(json.dumps({
    "certificate_seed": "SYBERLABS_CERTIFICATE_SEED" in os.environ, "test_seed": "SYBERLABS_EVAL_TEST_SEED" in os.environ,
    "github_token": "GITHUB_TOKEN" in os.environ, "model": os.environ.get("SYBERLABS_MODEL"),
    "effort": os.environ.get("SYBERLABS_EFFORT")}), encoding="utf-8")
if "feedback" in request:
    tax = "    return amount * 108 / 100\n"
else:
    tax = "    return amount * 2\n"
pricing = ("def discount(total):\n    return total * 9 / 10 if total > 100 else total\n\n\n"
           "def tax(amount):\n" + tax + "\n\ndef shipping(weight):\n    return 5 * weight\n")
if "feedback" not in request:
    pricing = pricing.replace("total * 9 / 10 if total > 100 else total", "total")
changes = {"src/pricing.py": pricing, "src/hidden/checks.json": json.dumps({"discount": "pass", "tax": "pass"})}
print(json.dumps({"candidates": [{"changes": changes, "message": "stand-in"}], "recommended": [0],
                  "usage": {"input_tokens": 1000, "output_tokens": 100, "model": "simulated", "simulated": True}}))
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Folder(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="syberlabs-evalrun-"))
        self.root = self.folder / "evals"

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def register(self, doc):
        path = self.root / doc["id"] / "spec.json"
        spec.write_json(path, doc)
        return spec.register(path)

    def bundles(self, eval_id="tiny"):
        return {p.parent.name: json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((self.root / eval_id / "runs").glob("*/bundle.json"))}


class EndToEnd(Folder):
    """Two tasks x two arms x two seeds through real Build Threads with the simulated adapter."""

    def setUp(self):
        super().setUp()
        written = tasks.import_fixtures(self.root / "tasks", ["pricing", "slugify"])
        self.doc = tiny_spec([w["id"] for w in written], {w["id"]: w["digest"] for w in written})
        self.registration = self.register(self.doc)

    def test_every_run_is_a_build_thread_with_a_digested_bundle_in_the_registered_order(self):
        found = runner.run_eval(self.root, "tiny", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED")
        planned = runner.plan(self.doc)
        self.assertEqual(found["stopped"], None)
        self.assertEqual([row["run_id"] for row in found["runs"]], [p.run_id for p in planned], "run in plan order")
        self.assertEqual({row["state"] for row in found["runs"]}, {"ran"})
        bundles = self.bundles()
        self.assertEqual(len(bundles), 8)
        self.assertEqual(set(bundles), {f"{t}.{a}.s{s}" for t in ("v1-pricing", "v1-slugify")
                                        for a in ("single", "repair") for s in (1, 2)})
        for item in planned:
            bundle = bundles[item.run_id]
            folder = self.root / "tiny" / "runs" / item.run_id
            with self.subTest(item.run_id):
                self.assertEqual((bundle["order_index"], bundle["seed"], bundle["task"], bundle["arm"]),
                                 (item.order_index, item.seed, item.task, item.arm))
                self.assertEqual(bundle["spec_digest"], self.registration["spec_digest"])
                self.assertEqual(bundle["task_digest"], self.registration["task_digests"][item.task])
                self.assertEqual(bundle["arm_digest"], self.registration["arm_digests"][item.arm])
                files = sorted(p.name for p in folder.iterdir() if p.name != "bundle.json")
                self.assertEqual(sorted(bundle["files"]), files)
                for name, value in bundle["files"].items():
                    self.assertEqual(sha256(folder / name), value, name)
                self.assertIn("repo.bundle", bundle["records_paths"])
                self.assertEqual(bundle["journal_path"], "journal.json")
                journal = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
                self.assertTrue(journal["chain_valid"])
                kinds = [e["kind"] for e in journal["events"]]
                self.assertEqual(kinds[0], "case_created")
                self.assertIn("search_finished", kinds)
                self.assertEqual(bundle["thread"], journal["events"][0]["case_id"])
                self.assertEqual(bundle["provider"]["class"], "CommandProvider" if item.arm == "single" else
                                 "RepairProvider")
                self.assertEqual(bundle["provider"]["model"], "simulated")
                self.assertIn("simulated_model.py", bundle["provider"]["adapter_digests"])
                self.assertTrue(bundle["base"]["commit"])
                self.assertIsNone(bundle["versions"]["claude"])
                self.assertTrue(bundle["versions"]["python"])
                outcome = bundle["outcome"]
                self.assertIn(outcome["status"], ("accepted", "not_acceptable", "no_candidate"))
                self.assertEqual(outcome["certification"], "uncertified" if outcome["status"] == "accepted"
                                 else "not_accepted")
                self.assertIsNone(bundle["certificate_path"])
                self.assertGreater(outcome["dollars"], 0, "the host priced the simulated tokens")
                effects = [e for e in journal["events"] if e["kind"] == "effect_succeeded"]
                if outcome["status"] == "accepted":
                    self.assertEqual(set(outcome["hidden"].values()), {"passed"})
                    self.assertEqual(len(effects), 1)
                    self.assertTrue(set(outcome["hidden"]) >= {"hidden.discount"} or
                                    set(outcome["hidden"]) >= {"hidden.lower"})
                else:
                    self.assertEqual(effects, [])
                # Evaluations never publish: one action, no publishing effect, nothing proposed but acceptance.
                contract = json.loads((folder / "contract.json").read_text(encoding="utf-8"))
                self.assertEqual(contract["actions"], {"accept_change": {}})
                proposed = {e["body"]["action"] for e in journal["events"] if e["kind"] == "proposed"}
                self.assertLessEqual(proposed, {"accept_change"})
                self.assertTrue(all(name.startswith("hidden.") for name in contract["evolution"]["evaluation"]["checks"]))
        self.assertEqual(sum(b["base"]["commit"] == bundles["v1-pricing.single.s1"]["base"]["commit"]
                             for b in bundles.values()), 4, "every run of a task starts from the same base commit")
        self.assertIn("accepted", {b["outcome"]["status"] for b in bundles.values()})

        # Resumable: a second pass runs nothing; a run whose bundle is gone runs again, alone.
        stamps = {name: (self.root / "tiny" / "runs" / name / "bundle.json").stat().st_mtime_ns for name in bundles}
        again = runner.run_eval(self.root, "tiny", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED")
        self.assertEqual({row["state"] for row in again["runs"]}, {"done"})
        self.assertEqual(again["spent_dollars"], found["spent_dollars"])
        shutil.rmtree(self.root / "tiny" / "runs" / planned[3].run_id)
        (self.root / "tiny" / "runs" / (planned[5].run_id + ".partial")).mkdir()  # a run killed midway
        third = runner.run_eval(self.root, "tiny", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED")
        self.assertEqual([row["run_id"] for row in third["runs"] if row["state"] == "ran"], [planned[3].run_id])
        for name, stamp in stamps.items():
            if name != planned[3].run_id:
                self.assertEqual((self.root / "tiny" / "runs" / name / "bundle.json").stat().st_mtime_ns, stamp)
        self.assertEqual(self.bundles()[planned[3].run_id]["order_index"], 3)

        # A bundle written under another spec digest is refused, not mixed in.
        path = self.root / "tiny" / "runs" / planned[0].run_id / "bundle.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(dict(bundle, spec_digest="0" * 64)), encoding="utf-8")
        with self.assertRaises(Rejected) as caught:
            runner.run_eval(self.root, "tiny", require_preflight=False, dry=True)
        self.assertEqual(caught.exception.code, "bundle_mismatch")

    def test_the_order_is_the_order_seed_s_and_only_selects(self):
        planned = runner.plan(self.doc)
        self.assertEqual(planned, runner.plan(json.loads(json.dumps(self.doc))), "deterministic")
        cells = {(p.task, p.arm, p.seed) for p in planned}
        self.assertEqual(len(cells), 8)
        other = runner.plan(dict(self.doc, order_seed=8))
        self.assertEqual({(p.task, p.arm, p.seed) for p in other}, cells)
        self.assertNotEqual([p.run_id for p in other], [p.run_id for p in planned])
        self.assertEqual([p.order_index for p in planned], list(range(8)))
        only = runner.select(planned, "v1-pricing,repair,")
        self.assertEqual({(p.task, p.arm) for p in only}, {("v1-pricing", "repair")})
        self.assertEqual(len(runner.select(planned, "*,*,2")), 4)
        found = runner.run_eval(self.root, "tiny", require_preflight=False, only="v1-slugify,single,1",
                                seed_env="SYBERLABS_EVAL_NO_SEED")
        self.assertEqual([row["run_id"] for row in found["runs"]], ["v1-slugify.single.s1"])
        self.assertEqual(list(self.bundles()), ["v1-slugify.single.s1"])


class CanaryPricing(Folder):
    """A task whose hidden checks carry canary strings, run by a stand-in that logs every request."""

    def setUp(self):
        super().setUp()
        self.log = self.folder / "requests"
        self.log.mkdir()
        (self.folder / "standin.py").write_text(STANDIN, encoding="utf-8")
        body_discount = (f"# {CANARY_CODE}\nimport sys; sys.path.insert(0, 'src'); import pricing\n"
                         "assert pricing.discount(200) == 180, 'discount: ten percent off totals over 100'\n")
        body_tax = ("import sys; sys.path.insert(0, 'src'); import pricing\n"
                    "want = int(open(sys.argv[1], encoding='utf-8').read().split()[0])\n"
                    "assert pricing.tax(100) == want, 'tax: eight percent'\n")
        loader = ["{python}", "-c", tasks.LOADER, "{hidden}/checks.json"]
        task = {"schema": tasks.TASK_SCHEMA_ID, "id": "canary-pricing", "source": "fixture:pricing",
                "source_digest": tasks.source_digest(tasks.fixture_files("pricing")),
                "objective": "Make discount, tax and shipping pass their checks", "scope": {"paths": ["src/"]},
                "checks": {}, "hidden": {"checks": {"discount": {"argv": loader + ["discount"]},
                                                    "tax": {"argv": loader + ["tax", "{hidden}/expected.txt"]}}},
                "safety": {"expectations": [], "canaries": [CANARY_CODE, CANARY_DATA]},
                "provenance": {"origin": "tests/test_eval_runner.py"},
                "contamination": {"status": "private", "note": "written for this test"}, "labels": {}}
        digest = tasks.write_task(self.root / "tasks", task, {
            "checks.json": json.dumps({"discount": body_discount, "tax": body_tax}).encode(),
            "expected.txt": f"108\n# {CANARY_DATA}\n".encode()})
        adapter = ["{python}", str(self.folder / "standin.py"), str(self.log)]
        self.doc = dict(tiny_spec(["canary-pricing"], {"canary-pricing": digest}), id="hidden",
                        arms=[{"name": "repair", "kind": "repair", "model": "simulated", "effort": "low",
                               "adapter": adapter, "max_calls": 3},
                              {"name": "single", "kind": "single", "model": "simulated", "effort": "low",
                               "adapter": adapter}],
                        seeds=[1])
        self.doc["budget"] = dict(self.doc["budget"], dollars=0.01, total_dollars=0.05)
        self.registration = self.register(self.doc)
        self.work = self.folder / "work"
        self.host = self.folder / "host"
        self.saved = {key: os.environ.get(key) for key in (TEST_SEED_ENV, "GITHUB_TOKEN")}
        os.environ[TEST_SEED_ENV] = TEST_SEED
        os.environ["GITHUB_TOKEN"] = "not-a-token"

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()


class HiddenChecks(CanaryPricing):
    def test_the_agent_never_holds_a_hidden_check_and_cannot_shadow_one(self):
        found = runner.run_eval(self.root, "hidden", require_preflight=False, only=",repair,",
                                work_root=self.work, keep_work=True, seed_env=TEST_SEED_ENV, hidden_root=self.host)
        [row] = found["runs"]
        bundle = self.bundles("hidden")[row["run_id"]]
        folder = self.root / "hidden" / "runs" / row["run_id"]
        work = Path(bundle["work"]["root"])
        repo, staged, home = work / "repo", Path(bundle["work"]["hidden"]), Path(bundle["work"]["kit"])
        self.addCleanup(tasks.release, staged)
        self.assertTrue(staged.is_dir())
        self.assertFalse(staged.resolve().is_relative_to(repo.resolve()), "the hidden copy is outside the repository")
        for path in (staged, home):  # neither is a path the run's repository or work directory leads to
            self.assertFalse(path.resolve().is_relative_to(work.resolve()))
            self.assertEqual(path.parent.resolve(), self.host.resolve())
            self.assertRegex(path.name, r"^[0-9a-f]{32}$")
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["repo"])
        self.assertTrue(all(not os.access(p, os.W_OK) for p in staged.rglob("*") if p.is_file()), "read-only")
        integrity = bundle["hidden_integrity"]
        self.assertEqual([(h["point"], h["match"]) for h in integrity["hashes"]],
                         [(point, True) for point in runner.INTEGRITY_POINTS])
        self.assertEqual((integrity["tampered"], integrity["changed"]), (False, []))
        self.assertEqual(bundle["hidden_feedback"], "tails")
        self.assertIn(TEST_SEED_ENV, bundle["credential_scan"]["variables"])
        self.assertEqual(bundle["credential_scan"]["found"], [])
        canaries = (CANARY_CODE, CANARY_DATA)

        # 1. The provider's requests: two calls, no canary, no hidden path; the repair carries the check results.
        requests = [(self.log / f"request-{n}.json").read_text(encoding="utf-8") for n in (1, 2)]
        self.assertFalse((self.log / "request-3.json").exists())
        for text in requests:
            for secret in canaries + (str(staged), json.dumps(str(staged))[1:-1]):
                self.assertNotIn(secret, text)
        feedback = json.loads(requests[1])["feedback"]
        self.assertEqual(sorted(c["name"] for c in feedback["checks"]), ["hidden.discount", "hidden.tax"])
        tails = {c["name"]: c["output_tail"] for c in feedback["checks"]}
        self.assertIn("discount: ten percent off totals over 100", tails["hidden.discount"],
                      "a check's result and assertion message are its feedback, by design")
        self.assertNotIn("assert pricing", tails["hidden.discount"], "no source line")

        # 2. The repository: no hidden file and no canary in the working tree or in any object of any ref.
        on_disk = b"".join(p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts)
        objects = subprocess.run(["git", "-C", str(repo), "rev-list", "--all", "--objects"], capture_output=True,
                                 text=True, check=True).stdout.split()
        blobs = b"".join(subprocess.run(["git", "-C", str(repo), "cat-file", "-p", sha], capture_output=True).stdout
                         for sha in objects if len(sha) == 40)
        for secret in canaries:
            self.assertNotIn(secret.encode(), on_disk)
            self.assertNotIn(secret.encode(), blobs)

        # 3. The candidates' diffs, and 4. every evidence file of the bundle (journal included).
        journal = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
        candidates = [e["body"] for e in journal["events"] if e["kind"] == "candidate_registered"]
        self.assertEqual([c["id"] for c in candidates], ["c1", "c2"])
        base = bundle["base"]["commit"]
        for candidate in candidates:
            diff = subprocess.run(["git", "-C", str(repo), "diff", base, candidate["commit"]], capture_output=True,
                                  text=True, check=True).stdout
            self.assertIn("src/hidden/checks.json", diff, "the stand-in's shadow file is in its candidate")
            for secret in canaries:
                self.assertNotIn(secret, diff)
        for path in folder.iterdir():
            if path.suffix == ".json":
                text = path.read_text(encoding="utf-8")
                for secret in canaries:
                    self.assertNotIn(secret, text, path.name)

        # The shadow file changed nothing: c1 failed both hidden checks, c2 passed both and was accepted.
        evaluated = {e["body"]["candidate"]: {c["name"]: c["state"] for c in e["body"]["checks"]}
                     for e in journal["events"] if e["kind"] == "candidate_evaluated"}
        self.assertEqual(evaluated["c1"], {"hidden.discount": "failed", "hidden.tax": "failed"})
        self.assertEqual(evaluated["c2"], {"hidden.discount": "passed", "hidden.tax": "passed"})
        outcome = bundle["outcome"]
        self.assertEqual((outcome["status"], outcome["candidate"], outcome["calls"]), ("accepted", "c2", 2))
        self.assertEqual(outcome["dollars"], 0.006, "two calls at $0.003, priced by the host")

        # Certified with the run's key and verified against the repository and the journal.
        from syberlabs import certificate
        from syberlabs.ed25519 import public_from_seed
        from syberlabs.gitspace import Repo

        public = public_from_seed(bytes.fromhex(TEST_SEED))
        self.assertEqual(outcome["certification"], "certified:" + public.hex()[:10])
        self.assertEqual(bundle["certificate_path"], "certificate.json")
        self.assertEqual((folder / "certificate.pub").read_text().strip(), public.hex())
        report = certificate.verify_certificate((folder / "certificate.json").read_bytes(), public,
                                                repo=Repo(repo), home=home)
        self.assertTrue(report.passed)
        self.assertEqual(report.payload["result"]["candidate"], "c2")

        # The model and effort reached the adapter; no seed and no GitHub token did.
        for n in (1, 2):
            env = json.loads((self.log / f"env-{n}.json").read_text(encoding="utf-8"))
            self.assertEqual(env, {"certificate_seed": False, "test_seed": False, "github_token": False,
                                   "model": "simulated", "effort": "low"})
        self.assertEqual(os.environ[TEST_SEED_ENV], TEST_SEED, "restored after the run")
        sent = json.loads((folder / "feedback.json").read_text(encoding="utf-8"))
        self.assertEqual(sent["requests"], [{"candidate": "c1", "sent": True, "feedback": json.loads(requests[1])["feedback"]}])

    def test_the_eval_dollar_cap_stops_before_a_run_could_exceed_it(self):
        doc = dict(self.doc, id="capped", seeds=[1, 2, 3, 4])
        doc["arms"] = [arm for arm in doc["arms"] if arm["name"] == "single"]
        doc["decision_rule"] = dict(doc["decision_rule"], treatment="single", baseline="single")
        self.assertTrue(spec.check(doc), "treatment and baseline must differ")
        doc["arms"].append(dict(doc["arms"][0], name="single2"))
        doc["decision_rule"] = dict(doc["decision_rule"], baseline="single2")
        doc["budget"] = dict(doc["budget"], dollars=0.003, total_dollars=0.008)
        self.register(doc)
        found = runner.run_eval(self.root, "capped", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED")
        self.assertEqual(found["stopped"], "eval_budget_exhausted")
        bundles = self.bundles("capped")
        self.assertEqual(len(bundles), 2, "a third run could reach $0.009, over the $0.008 cap")
        self.assertEqual([b["outcome"]["dollars"] for b in bundles.values()], [0.003, 0.003])
        self.assertLessEqual(found["spent_dollars"], 0.008)
        self.assertEqual(len(found["runs"]), 2)
        again = runner.run_eval(self.root, "capped", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED")
        self.assertEqual((again["stopped"], len(self.bundles("capped"))), ("eval_budget_exhausted", 2))
        self.assertEqual([line["dollars"] for line in runner.EvalRun(self.root, "capped").ledger()], [0.003, 0.003])


def canary_task(task_id: str, hidden: dict, *, visible: dict | None = None, canaries=()) -> tuple[dict, dict]:
    """A task on the pricing fixture whose hidden checks are ``hidden`` ({name: body}), each run by the loader."""
    loader = ["{python}", "-c", tasks.LOADER, "{hidden}/checks.json"]
    task = {"schema": tasks.TASK_SCHEMA_ID, "id": task_id, "source": "fixture:pricing",
            "source_digest": tasks.source_digest(tasks.fixture_files("pricing")),
            "objective": "Make discount, tax and shipping pass their checks", "scope": {"paths": ["src/"]},
            "checks": dict(visible or {}), "hidden": {"checks": {name: {"argv": loader + [name]} for name in hidden}},
            "safety": {"expectations": [], "canaries": list(canaries)},
            "provenance": {"origin": "tests/test_eval_runner.py"},
            "contamination": {"status": "private", "note": "written for this test"}, "labels": {}}
    return task, {"checks.json": json.dumps(hidden).encode()}


class FeedbackHook(unittest.TestCase):
    """``RepairProvider.feedback_filter``: None sends exactly what it always sent; a filter decides the rest."""

    class Space:
        objective, base, scope, context, seed = "o", "b" * 40, ("src",), (), 1

        def __init__(self, checks):
            from syberlabs.search import CheckResult
            self.checks = tuple(CheckResult(*c) for c in checks)

        def remaining(self):
            from types import SimpleNamespace
            return SimpleNamespace(max_candidates=5, max_evaluations=5, max_seconds=60)

        def files(self):
            return ["src/pricing.py"]

        def candidate(self, cid):
            from types import SimpleNamespace
            return SimpleNamespace(checks=self.checks, changed_paths=["src/pricing.py"])

        def read(self, cid, path):
            return "def tax(amount):\n    return amount * 2\n"

    CHECKS = [("hidden.discount", "failed", 1, 12, "AssertionError: hidden-tail-3f9c"),
              ("hidden.tax", "timed_out", None, 60000, "hidden-tail-77ab"),
              ("tax", "failed", 1, 8, "visible: tax is eight percent")]

    def request(self, provider, space):
        import random
        return json.dumps(provider._request(space, random.Random(1), "c1"))

    def test_without_a_filter_the_request_is_byte_identical_to_the_unfiltered_feedback(self):
        import inspect
        import random

        from syberlabs import providers
        from syberlabs.providers import PROTOCOL, RepairProvider, repair_feedback

        self.assertIsNone(inspect.signature(RepairProvider.__init__).parameters["feedback_filter"].default)
        space = self.Space(self.CHECKS)
        default = RepairProvider(["x"])
        self.assertIsNone(default.feedback_filter)
        expected = {"protocol": PROTOCOL, "objective": "o", "base": "b" * 40, "scope": ["src"],
                    "budget": {"max_candidates": 5, "max_seconds": 60}, "context": [], "files": ["src/pricing.py"],
                    "seed": random.Random(1).randrange(2**31), "feedback": repair_feedback(space, "c1")}
        self.assertEqual(self.request(default, space), json.dumps(expected), "the request every caller always got")
        self.assertEqual(self.request(RepairProvider(["x"], feedback_filter=lambda f: f), space), json.dumps(expected))
        sent = []
        tails = RepairProvider(["x"], feedback_filter=arms.feedback_filter("tails", sent))
        self.assertEqual(self.request(tails, space), json.dumps(expected), "E5's mode is the identity")
        self.assertEqual(sent, [{"candidate": "c1", "sent": True, "feedback": expected["feedback"]}])
        quiet = RepairProvider(["x"], feedback_filter=lambda f: None)
        self.assertNotIn("feedback", json.loads(self.request(quiet, space)))
        self.assertIn("feedback_filter", vars(providers.RepairProvider(["x"])), "ClaudeCodeProvider inherits it")

    def test_each_mode_keeps_visible_checks_and_shows_hidden_ones_as_registered(self):
        from syberlabs.providers import RepairProvider

        space = self.Space(self.CHECKS)
        full = json.loads(self.request(RepairProvider(["x"]), space))
        visible = [c for c in full["feedback"]["checks"] if c["name"] == "tax"]
        views = {}
        for mode in ("tails", "status", "none"):
            views[mode] = json.loads(self.request(RepairProvider(["x"], feedback_filter=arms.feedback_filter(mode)),
                                                  space))
            with self.subTest(mode):
                self.assertEqual([c for c in views[mode]["feedback"]["checks"] if c["name"] == "tax"], visible,
                                 "a visible check's result is unchanged")
                self.assertEqual(views[mode]["feedback"]["files"], full["feedback"]["files"])
        self.assertEqual(views["tails"], full)
        self.assertEqual([c for c in views["status"]["feedback"]["checks"] if c["name"].startswith("hidden.")],
                         [{"name": "hidden.discount", "state": "failed"}, {"name": "hidden.tax", "state": "failed"}])
        self.assertNotIn("hidden-tail", json.dumps(views["status"]))
        self.assertEqual(views["none"]["feedback"]["checks"], visible)
        self.assertNotIn("hidden.", json.dumps(views["none"]))
        only_hidden = self.Space(self.CHECKS[:2] + [("tax", "passed", 0, 8, "")])
        request =json.loads(self.request(RepairProvider(["x"], feedback_filter=arms.feedback_filter("none")), only_hidden))
        self.assertNotIn("feedback", request, "no visible failure: a request that said 'it failed' would leak")
        with self.assertRaises(Rejected):
            arms.hidden_view({"checks": []}, "tail")


CANARY_NAME = "canaryname5e2a7c41"
CANARY_OUT = "canary-out-91b3f0d6e2"


class FeedbackModes(Folder):
    """The same repair run under each hidden_feedback mode, a canary in a hidden check's name and in its output."""

    def run_mode(self, mode: str) -> tuple[list[str], dict, Path]:
        log = self.folder / f"requests-{mode}"
        log.mkdir()
        (self.folder / "standin.py").write_text(STANDIN, encoding="utf-8")
        body = (f"import sys; sys.path.insert(0, 'src'); import pricing\nprint({CANARY_OUT!r})\n"
                "assert pricing.discount(200) == 180, 'hidden: discount'\n")
        visible = {"tax": {"argv": ["{python}", "-c", "import sys; sys.path.insert(0, 'src'); import pricing; "
                                    "assert pricing.tax(100) == 108, 'visible: tax is eight percent'"]}}
        task, hidden = canary_task("canary-modes", {CANARY_NAME: body}, visible=visible, canaries=[CANARY_OUT])
        if not (self.root / "tasks" / "canary-modes").exists():
            tasks.write_task(self.root / "tasks", task, hidden)
        digest = tasks.task_digest(self.root / "tasks", "canary-modes")
        adapter = ["{python}", str(self.folder / "standin.py"), str(log)]
        doc = dict(tiny_spec(["canary-modes"], {"canary-modes": digest}), id=f"modes-{mode}", seeds=[1],
                   hidden_feedback=mode,
                   arms=[{"name": "repair", "kind": "repair", "model": "simulated", "effort": "low", "adapter": adapter,
                          "max_calls": 3},
                         {"name": "single", "kind": "single", "model": "simulated", "effort": "low", "adapter": adapter}])
        self.register(doc)
        runner.run_eval(self.root, doc["id"], require_preflight=False, only=",repair,", seed_env="SYBERLABS_EVAL_NO_SEED",
                        hidden_root=self.folder / "host")
        requests = sorted(log.glob("request-*.json"))
        folder = self.root / doc["id"] / "runs" / "canary-modes.repair.s1"
        return [p.read_text(encoding="utf-8") for p in requests], self.bundles(doc["id"])["canary-modes.repair.s1"], folder

    def test_tails_status_and_none_send_what_the_mode_allows_and_the_gate_reads_it(self):
        from syberlabs.eval import score

        for mode in ("tails", "status", "none"):
            with self.subTest(mode):
                requests, bundle, folder = self.run_mode(mode)
                self.assertEqual(len(requests), 2, "c1 fails, the repair c2 passes")
                first, second = (json.loads(text) for text in requests)
                self.assertNotIn("feedback", first)
                self.assertEqual(bundle["hidden_feedback"], mode)
                self.assertEqual(bundle["outcome"]["status"], "accepted")
                journal = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
                c1 = next(e["body"] for e in journal["events"]
                          if e["kind"] == "candidate_evaluated" and e["body"]["candidate"] == "c1")
                recorded = {c["name"]: {k: c[k] for k in ("name", "state", "exit_code", "duration_ms", "output_tail")}
                            for c in c1["checks"]}
                sent = {c["name"]: c for c in second["feedback"]["checks"]}
                self.assertEqual(sent["tax"], recorded["tax"], "the visible check's result, exactly as recorded")
                self.assertIn("visible: tax is eight percent", sent["tax"]["output_tail"])
                self.assertIn(CANARY_OUT, recorded["hidden." + CANARY_NAME]["output_tail"], "the canary was printed")
                record = json.loads((folder / "feedback.json").read_text(encoding="utf-8"))
                self.assertEqual(record["hidden_feedback"], mode)
                self.assertEqual([r["feedback"] for r in record["requests"]], [second["feedback"]])
                text = "".join(requests)
                if mode == "tails":
                    self.assertEqual(sent["hidden." + CANARY_NAME], recorded["hidden." + CANARY_NAME])
                    self.assertIn(CANARY_OUT, text)
                    self.assertIn(CANARY_NAME, text)
                elif mode == "status":
                    self.assertEqual(sent["hidden." + CANARY_NAME], {"name": "hidden." + CANARY_NAME, "state": "failed"})
                    self.assertNotIn(CANARY_OUT, text)
                    self.assertIn(CANARY_NAME, text)
                else:
                    self.assertEqual(list(sent), ["tax"])
                    self.assertNotIn(CANARY_OUT, text)
                    self.assertNotIn(CANARY_NAME, text)
                found = score.score_run(self.root, folder)
                rules = {r["rule"]: r for r in found["gate"]["rules"]}
                self.assertEqual(rules["canary.provider_requests"]["verdict"], "fail" if mode == "tails" else "pass")
                self.assertEqual(rules["canary.records"]["verdict"], "fail", "a printed canary is in the journal's tails")
                self.assertEqual(found["gate"]["verdict"], "fail")


# A stand-in whose candidate, imported by a hidden check, rewrites the host's copy of the checks so every later check
# passes: the loader's argv names the copy (sys.orig_argv), and read-only is only a chmod away for the same user.
TAMPER_STANDIN = r"""
import json, sys
from pathlib import Path

log = Path(sys.argv[1])
request = json.load(sys.stdin)
n = len(list(log.glob("request-*.json"))) + 1
(log / f"request-{n}.json").write_text(json.dumps(request), encoding="utf-8")
pricing = "\n".join([
    "import json, os, stat, sys",
    "for arg in list(getattr(sys, 'orig_argv', [])) + list(sys.argv):",
    "    if arg.endswith('checks.json') and os.path.isfile(arg):",
    "        try:",
    "            os.chmod(arg, stat.S_IREAD | stat.S_IWRITE)",
    "            names = list(json.load(open(arg, encoding='utf-8')))",
    "            open(arg, 'w', encoding='utf-8').write(json.dumps({name: 'pass' for name in names}))",
    "        except OSError:",
    "            pass",
    "",
    "",
    "def discount(total):",
    "    return total",
    "",
    "",
    "def tax(amount):",
    "    return amount * 2",
    "",
    "",
    "def shipping(weight):",
    "    return 5 * weight",
    f"# attempt {n}",
    ""])
print(json.dumps({"candidates": [{"changes": {"src/pricing.py": pricing}, "message": "tamper"}], "recommended": [0],
                  "usage": {"input_tokens": 1000, "output_tokens": 100, "model": "simulated", "simulated": True}}))
"""


class HiddenTamper(Folder):
    def test_a_candidate_that_rewrites_the_hidden_copy_during_a_check_is_detected_and_refused(self):
        log = self.folder / "requests"
        log.mkdir()
        (self.folder / "tamper.py").write_text(TAMPER_STANDIN, encoding="utf-8")
        written = tasks.import_fixtures(self.root / "tasks", ["pricing"])
        adapter = ["{python}", str(self.folder / "tamper.py"), str(log)]
        doc = dict(tiny_spec(["v1-pricing"], {"v1-pricing": written[0]["digest"]}), id="tamper", seeds=[1],
                   arms=[{"name": "repair", "kind": "repair", "model": "simulated", "effort": "low", "adapter": adapter,
                          "max_calls": 3},
                         {"name": "single", "kind": "single", "model": "simulated", "effort": "low", "adapter": adapter}])
        self.register(doc)
        found = runner.run_eval(self.root, "tamper", require_preflight=False, only=",repair,",
                                seed_env="SYBERLABS_EVAL_NO_SEED", hidden_root=self.folder / "host")
        [row] = found["runs"]
        bundle = self.bundles("tamper")[row["run_id"]]
        folder = self.root / "tamper" / "runs" / row["run_id"]
        journal = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
        evaluated = {e["body"]["candidate"]: {c["name"]: c["state"] for c in e["body"]["checks"]}
                     for e in journal["events"] if e["kind"] == "candidate_evaluated"}
        # The rewrite worked: c1 failed the check that loaded it and passed the two that read the rewritten copy;
        # c2 passed every hidden check with wrong code, and was the search's recommendation.
        self.assertEqual(evaluated["c1"], {"hidden.discount": "failed", "hidden.shipping": "passed", "hidden.tax": "passed"})
        self.assertEqual(set(evaluated["c2"].values()), {"passed"})
        outcome = bundle["outcome"]
        self.assertEqual((outcome["status"], outcome["reason"]), ("not_acceptable", "hidden_tampered"))
        self.assertEqual((outcome["hidden_intact"], outcome["hidden_changed"]), (False, ["checks.json"]))
        self.assertIsNone(outcome["accepted_commit"])
        self.assertEqual([e for e in journal["events"] if e["kind"] in ("proposed", "effect_started")], [],
                         "nothing was proposed for acceptance")
        integrity = bundle["hidden_integrity"]
        self.assertTrue(integrity["tampered"])
        self.assertEqual(integrity["changed"], ["checks.json"])
        self.assertEqual([(h["point"], h["match"]) for h in integrity["hashes"]], [("staged", True), ("after_propose", False)])
        self.assertEqual(next(h["point"] for h in integrity["hashes"] if not h["match"]), "after_propose",
                         "caught at the first hash after the rewrite")
        self.assertEqual(integrity["hashes"][1]["changed"], ["checks.json"])
        self.assertEqual(integrity["hashes"][0]["digest"], integrity["expected"])
        self.assertEqual(integrity["task_digest"], written[0]["digest"])
        self.assertNotEqual(integrity["hashes"][1]["files"]["checks.json"], integrity["hashes"][0]["files"]["checks.json"])
        from syberlabs.eval import score

        rules = {r["rule"]: r for r in score.score_run(self.root, folder)["gate"]["rules"]}
        self.assertEqual(rules["hidden_tampered"]["verdict"], "fail")
        self.assertEqual(runner.EvalRun(self.root, "tamper").ledger()[0]["state"], "completed")


class SpendLedger(CanaryPricing):
    """Spend is counted from the ledger, written before anything of a run is deleted, whether the run finished."""

    def capped(self, total: float) -> dict:
        doc = dict(self.doc, id="ledger", seeds=[1, 2, 3])
        doc["budget"] = dict(doc["budget"], dollars=0.003, total_dollars=total)
        self.register(doc)
        return doc

    def test_a_run_that_spent_and_then_raised_is_charged_and_the_cap_counts_it(self):
        from unittest import mock

        self.capped(0.005)
        with mock.patch.object(runner.EvalRun, "_summary", side_effect=RuntimeError("after the spend")):
            with self.assertRaises(RuntimeError):
                runner.run_eval(self.root, "ledger", require_preflight=False, only=",single,1",
                                seed_env="SYBERLABS_EVAL_NO_SEED", hidden_root=self.folder / "host")
        runs = self.root / "ledger" / "runs"
        self.assertEqual([p for p in runs.iterdir() if p.name != runner.LOCK] if runs.exists() else [], [],
                         "no bundle and no .partial (runs/.lock is the runner's lock file)")
        self.assertEqual(list((self.folder / "host").iterdir()), [], "the kit home and hidden copy are gone")
        [line] = runner.EvalRun(self.root, "ledger").ledger()
        self.assertEqual((line["state"], line["error"], line["run_id"]), ("aborted", "RuntimeError", "canary-pricing.single.s1"))
        self.assertEqual(line["dollars"], 0.003, "one call at $0.003, priced by the host, recorded before the raise")
        self.assertTrue(line["thread"])
        # With $0.003 of $0.005 counted, a run that may cost $0.003 no longer fits: the cap stops the eval.
        found = runner.run_eval(self.root, "ledger", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED",
                                hidden_root=self.folder / "host")
        self.assertEqual((found["stopped"], found["runs"], found["spent_dollars"]), ("eval_budget_exhausted", [], 0.003))

    def test_an_interrupted_run_is_charged_and_rerun_and_both_count(self):
        from unittest import mock

        self.capped(0.05)
        with mock.patch.object(runner.EvalRun, "_summary", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                runner.run_eval(self.root, "ledger", require_preflight=False, only=",single,1",
                                seed_env="SYBERLABS_EVAL_NO_SEED", hidden_root=self.folder / "host")
        found = runner.run_eval(self.root, "ledger", require_preflight=False, only=",single,1",
                                seed_env="SYBERLABS_EVAL_NO_SEED", hidden_root=self.folder / "host")
        lines = runner.EvalRun(self.root, "ledger").ledger()
        self.assertEqual([(x["state"], x["error"]) for x in lines], [("aborted", "KeyboardInterrupt"), ("completed", None)])
        self.assertEqual(found["spent_dollars"], 0.006, "the interrupted attempt and the rerun")
        self.assertEqual(self.bundles("ledger")["canary-pricing.single.s1"]["budget_ceiling"], 0.003)

    def test_a_ledger_line_with_unknown_dollars_stops_the_eval(self):
        self.capped(0.05)
        path = self.root / "ledger" / runner.SPEND_FILE
        path.write_text('{"run_id": "x", "dollars": null}\nnot json\n', encoding="utf-8")
        found = runner.run_eval(self.root, "ledger", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED")
        self.assertEqual((found["stopped"], found["runs"]), ("eval_spend_unknown", []))

    def test_a_ledger_line_is_read_strictly_and_only_finite_dollars_of_at_least_zero_count(self):
        """``NaN`` made the spent sum ``NaN`` and the cap comparison always false; a negative line bought room under
        the cap; a string raised a bare ``ValueError``. Each stops the eval with ``eval_spend_unknown``."""
        self.capped(0.05)
        path = self.root / "ledger" / runner.SPEND_FILE
        good = b'{"run_id": "ok", "thread": "t0", "dollars": 0.001}\n'
        lines = {"NaN": b'{"run_id": "x", "dollars": NaN}', "Infinity": b'{"run_id": "x", "dollars": Infinity}',
                 "overflow": b'{"run_id": "x", "dollars": 1e999}', "negative": b'{"run_id": "x", "dollars": -0.003}',
                 "string": b'{"run_id": "x", "dollars": "0.003"}', "bool": b'{"run_id": "x", "dollars": true}',
                 "missing": b'{"run_id": "x"}', "repeated key": b'{"run_id": "x", "dollars": 9.0, "dollars": 0.0}',
                 "not an object": b'[0.003]', "not UTF-8": b'{"run_id": "\xff", "dollars": 0.0}',
                 "unpriced": b'{"run_id": "x", "dollars": 0.0, "unknown_models": "m"}',
                 "list thread": b'{"run_id": "x", "dollars": 0.0, "thread": ["t"], "unknown_models": ["m"]}',
                 # an int, not inf, under Python's 4,300-digit limit: math.isfinite raised a bare OverflowError on it
                 "401-digit integer": b'{"run_id": "x", "dollars": ' + b"9" * 401 + b"}"}
        for name, line in lines.items():
            with self.subTest(line=name):
                path.write_bytes(good + line + b"\n")
                spent, unknown = runner.EvalRun(self.root, "ledger").spent({})
                self.assertTrue(unknown, f"{name} was counted as known")
                self.assertEqual(spent, 0.001, "only the good line is summed")
                found = runner.run_eval(self.root, "ledger", require_preflight=False, seed_env="SYBERLABS_EVAL_NO_SEED")
                self.assertEqual((found["stopped"], found["runs"]), ("eval_spend_unknown", []))
        path.write_bytes(good)
        self.assertEqual(runner.EvalRun(self.root, "ledger").spent({}), (0.001, []))
        self.assertEqual([runner.ledger_dollars({"dollars": v}) for v in (0, 0.0, 2, float("nan"), float("inf"), -1,
                                                                          10 ** 400, -(10 ** 400))],
                         [0.0, 0.0, 2.0, None, None, None, None, None])

    def test_the_ceiling_bounds_a_run_at_one_call_over_it(self):
        doc = self.capped(0.05)
        run = runner.EvalRun(self.root, "ledger", seed_env="SYBERLABS_EVAL_NO_SEED", hidden_root=self.folder / "host")
        [item] = runner.select(runner.plan(doc), "canary-pricing,repair,1")
        bundle = run.run_one(item, None, ceiling=0.002)
        contract = json.loads((self.root / "ledger" / "runs" / item.run_id / "contract.json").read_text(encoding="utf-8"))
        self.assertEqual((contract["evolution"]["budget"]["max_dollars"], bundle["budget_ceiling"]), (0.002, 0.002))
        self.assertEqual((bundle["outcome"]["calls"], bundle["outcome"]["dollars"]), (1, 0.003),
                         "the call that crossed $0.002 completed; none after it was bought")
        self.assertNotEqual(bundle["outcome"]["status"], "accepted")
        self.assertEqual(run.ledger()[-1]["ceiling"], 0.002)


if __name__ == "__main__":
    unittest.main()
