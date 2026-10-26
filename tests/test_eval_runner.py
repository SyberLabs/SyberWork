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
from syberlabs.eval import runner, spec, tasks
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


class HiddenChecks(Folder):
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

    def test_the_agent_never_holds_a_hidden_check_and_cannot_shadow_one(self):
        found = runner.run_eval(self.root, "hidden", require_preflight=False, only=",repair,",
                                work_root=self.work, keep_work=True, seed_env=TEST_SEED_ENV)
        [row] = found["runs"]
        bundle = self.bundles("hidden")[row["run_id"]]
        folder = self.root / "hidden" / "runs" / row["run_id"]
        work = Path(bundle["work"])
        repo, staged = work / "repo", work / "host" / "hidden"
        self.assertTrue(staged.is_dir())
        self.assertFalse(staged.resolve().is_relative_to(repo.resolve()), "the hidden copy is outside the repository")
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
                                                repo=Repo(repo), home=work / "kit")
        self.assertTrue(report.passed)
        self.assertEqual(report.payload["result"]["candidate"], "c2")

        # The model and effort reached the adapter; no seed and no GitHub token did.
        for n in (1, 2):
            env = json.loads((self.log / f"env-{n}.json").read_text(encoding="utf-8"))
            self.assertEqual(env, {"certificate_seed": False, "test_seed": False, "github_token": False,
                                   "model": "simulated", "effort": "low"})
        self.assertEqual(os.environ[TEST_SEED_ENV], TEST_SEED, "restored after the run")

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


if __name__ == "__main__":
    unittest.main()
