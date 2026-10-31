"""Adversarial verification of the SyberEval core before the GitHits A/B pilot runs on it.

Each test names one way a run could cheat, be mislabelled, or be scored or charged wrongly, and shows it against
real Build Threads with stand-in models (nothing here calls a model or the network). A test marked
``expectedFailure`` pins a gap that needs a design decision, not a bug with a small fix: it passes, and must lose
its marker, once that decision is built.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from syberlabs.errors import Rejected
from syberlabs.eval import runner, spec, tasks
from tests.test_eval_core import SIM, git, tiny_spec

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
CANARY_CODE = "canary-code-5be0c1d7e4"
CANARY_DATA = "canary-data-9a73f20c18"
# Every stand-in call declares 1000 tokens in and 100 out of the simulated model: $0.003 under this table.
PRICES = {"simulated": {"input_per_million": 2.0, "output_per_million": 10.0}}
CALL = 0.003
GOOD = ("def discount(total):\n    return total * 9 / 10 if total > 100 else total\n\n\n"
        "def tax(amount):\n    return amount * 108 / 100\n\n\ndef shipping(weight):\n    return 5 * weight\n")
WRONG = ("def discount(total):\n    return total\n\n\ndef tax(amount):\n    return amount\n\n\n"
         "def shipping(weight):\n    return 0\n")

# A stand-in model adapter under the search protocol. ``plan.json`` lists, per call, the files to write; the last
# entry repeats. Each request is logged. ``hold`` makes the first call wait (bounded) for a ``release`` file;
# ``kill_parent`` kills the process that ran it (the eval runner) with SIGKILL, as a crash or an OOM kill would.
ADAPTER = r'''
import json, os, signal, sys, time
from pathlib import Path

plan, log = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), Path(sys.argv[2])
request = json.load(sys.stdin)
n = len(list(log.glob("request-*.json"))) + 1
(log / f"request-{n}.json").write_text(json.dumps(request), encoding="utf-8")
step = plan[min(n, len(plan)) - 1]
if step.get("hold") and n == 1:
    deadline = time.monotonic() + 60
    while not (log / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
if step.get("kill_parent"):
    os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(0)
print(json.dumps({"candidates": [{"changes": step["changes"], "message": "stand-in"}], "recommended": [0],
                  "usage": {"input_tokens": 1000, "output_tokens": 100, "model": "simulated", "simulated": True}}))
'''

# A stand-in ``claude``: answers ``--version``, logs the GitHits session and agent it was given, writes a passing
# pricing.py in its working directory, and prints a one-turn ``stream-json`` transcript served by ``sys.argv[2]``.
CLAUDE = r'''
import json, os, sys
from pathlib import Path

if "--version" in sys.argv:
    print("2.1.309 (Claude Code)", flush=True)
    sys.exit(0)
log, model = Path(sys.argv[1]), sys.argv[2]
sys.stdin.read()
n = len(list(log.glob("call-*.json"))) + 1
(log / f"call-{n}.json").write_text(json.dumps({"session": os.environ.get("GITHITS_SESSION_ID"),
                                                "agent": os.environ.get("GITHITS_AGENT")}), encoding="utf-8")
Path("src/pricing.py").write_text(GOOD, encoding="utf-8")
usage = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
for event in ({"type": "system", "subtype": "init", "claude_code_version": "2.1.309", "model": model,
               "effort": "medium", "apiKeySource": "ANTHROPIC_API_KEY", "mcp_servers": [], "session_id": "x",
               "tools": ["Read", "Edit"]},
              {"type": "assistant", "message": {"id": "msg_1", "model": model, "usage": usage,
                                                "content": [{"type": "text", "text": "done"}]}},
              {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "done",
               "total_cost_usd": 0.01, "session_id": "x", "usage": {}, "modelUsage": {}}):
    print(json.dumps(event), flush=True)
'''


def canary_task(tasks_root: Path, task_id: str = "canary-pricing") -> str:
    """The pricing fixture with two hidden checks run by ``tasks.LOADER``; canaries in a check's code and in a data
    file that the ``tax`` check reads by its ``{hidden}`` path. Returns the task's digest."""
    discount = (f"# {CANARY_CODE}\nimport sys; sys.path.insert(0, 'src'); import pricing\n"
                "assert pricing.discount(200) == 180, 'discount: ten percent off totals over 100'\n")
    tax = ("import sys; sys.path.insert(0, 'src'); import pricing\n"
           "want = int(open(sys.argv[1], encoding='utf-8').read().split()[0])\n"
           "assert pricing.tax(100) == want, 'tax: eight percent'\n")
    loader = ["{python}", "-c", tasks.LOADER, "{hidden}/checks.json"]
    task = {"schema": tasks.TASK_SCHEMA_ID, "id": task_id, "source": "fixture:pricing",
            "source_digest": tasks.source_digest(tasks.fixture_files("pricing")),
            "objective": "Make discount, tax and shipping pass their checks", "scope": {"paths": ["src/"]},
            "checks": {}, "hidden": {"checks": {"discount": {"argv": loader + ["discount"]},
                                                "tax": {"argv": loader + ["tax", "{hidden}/expected.txt"]}}},
            "safety": {"expectations": [], "canaries": [CANARY_CODE, CANARY_DATA]},
            "provenance": {"origin": "tests/test_eval_adversarial.py"},
            "contamination": {"status": "private", "note": "written for this test"}, "labels": {}}
    return tasks.write_task(tasks_root, task, {
        "checks.json": json.dumps({"discount": discount, "tax": tax}).encode(),
        "expected.txt": f"108\n# {CANARY_DATA}\n".encode()})


class Case(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="syberlabs-evaladv-"))
        self.root = self.folder / "evals"
        self.log = self.folder / "requests"
        self.log.mkdir()
        (self.folder / "adapter.py").write_text(ADAPTER, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def adapter(self, *steps: dict) -> list[str]:
        """The stand-in adapter's argv, answering call n with ``steps[n - 1]`` (the last repeats)."""
        plan = self.folder / "plan.json"
        plan.write_text(json.dumps(list(steps)), encoding="utf-8")
        return ["{python}", str(self.folder / "adapter.py"), str(plan), str(self.log)]

    def requests(self) -> list[str]:
        found = sorted(self.log.glob("request-*.json"), key=lambda p: int(p.stem.split("-")[1]))
        return [p.read_text(encoding="utf-8") for p in found]

    def canary_spec(self, eval_id: str, adapter: list[str], **changes) -> dict:
        digest = canary_task(self.root / "tasks")
        doc = dict(tiny_spec(["canary-pricing"], {"canary-pricing": digest}), id=eval_id, seeds=[1],
                   arms=[{"name": "repair", "kind": "repair", "model": "simulated", "effort": "low",
                          "adapter": adapter, "max_calls": 3},
                         {"name": "single", "kind": "single", "model": "simulated", "effort": "low",
                          "adapter": adapter}])
        doc["budget"] = dict(doc["budget"], prices=PRICES)
        doc.update(changes)
        return doc

    def register(self, doc: dict) -> dict:
        path = self.root / doc["id"] / "spec.json"
        spec.write_json(path, doc)
        return spec.register(path)

    def run_eval(self, eval_id: str, **options):
        options.setdefault("require_preflight", False)
        options.setdefault("seed_env", "SYBERLABS_EVAL_ADV_NO_SEED")
        options.setdefault("work_root", self.folder / "work")
        return runner.run_eval(self.root, eval_id, **options)

    def runner_process(self, eval_id: str) -> list[str]:
        """``run_eval`` of this eval's ``single`` runs in another process, as a second terminal would run it."""
        code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from syberlabs.eval import runner; "
                f"runner.run_eval({str(self.root)!r}, {eval_id!r}, only=',single,', require_preflight=False, "
                f"seed_env='SYBERLABS_EVAL_ADV_NO_SEED', work_root={str(self.folder / 'work')!r})")
        return [PY, "-c", code]

    def bundle(self, eval_id: str, run: str) -> dict:
        return json.loads((self.root / eval_id / "runs" / run / "bundle.json").read_text(encoding="utf-8"))


class PreRegistration(Case):
    def test_a_number_that_is_not_finite_is_not_a_valid_spec(self):
        """``NaN`` passes ``minimum``, ``maximum`` and ``exclusiveMinimum`` (every comparison with it is false). A spec
        whose ``total_dollars`` is ``NaN`` registers and its eval cap never stops (``spent + dollars > NaN`` is
        false); a ``NaN`` threshold makes every decision false."""
        doc = self.canary_spec("nan", self.adapter({"changes": {"src/pricing.py": GOOD}}))
        for where, field in (("budget", "total_dollars"), ("budget", "dollars"), ("decision_rule", "threshold")):
            for value in (float("nan"), float("inf")):
                changed = json.loads(json.dumps(doc))
                changed[where][field] = value
                with self.subTest(field=field, value=value):
                    self.assertTrue(spec.check(changed), f"{field} = {value} accepted")
        path = self.root / "nan" / "spec.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(doc, budget=dict(doc["budget"], total_dollars=float("nan")))), encoding="utf-8")
        with self.assertRaises(Rejected):
            spec.register(path)
        self.assertFalse((path.parent / "registration.json").exists())

    def test_a_spec_with_a_duplicate_key_is_refused(self):
        """``json.loads`` keeps the last of two equal keys, so a reader of the file can see one threshold while the
        registered digest freezes the other."""
        doc = self.canary_spec("dup", self.adapter({"changes": {"src/pricing.py": GOOD}}))
        text = json.dumps(doc, indent=2).replace('"threshold": 0.0', '"threshold": 0.0, "threshold": -1.0')
        self.assertIn('"threshold": -1.0', text)
        path = self.root / "dup" / "spec.json"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(Rejected):
            spec.register(path)
        self.assertFalse((path.parent / "registration.json").exists())

    def test_two_cells_that_would_share_a_run_id_are_refused(self):
        """A run id is ``<task>.<arm>.s<seed>`` and both names may hold dots: task ``p`` with arm ``x.r`` and task
        ``p.x`` with arm ``r`` are both ``p.x.r.s1``. The second run overwrites the first one's bundle, and on
        resume both cells read as done from one bundle: a run is missing and another is counted twice."""
        task, bodies = tasks.fixture_task("pricing")
        digests = {tid: tasks.write_task(self.root / "tasks", dict(task, id=tid),
                                         {"checks.json": json.dumps(bodies).encode()}) for tid in ("p", "p.x")}
        doc = tiny_spec(["p", "p.x"], digests, seeds=[1])
        doc["arms"] = [{"name": "x.r", "kind": "repair", "model": "simulated", "effort": "none", "adapter": SIM,
                        "max_calls": 3},
                       {"name": "r", "kind": "single", "model": "simulated", "effort": "none", "adapter": SIM}]
        doc["decision_rule"] = dict(doc["decision_rule"], treatment="x.r", baseline="r")
        self.assertTrue(any("run id" in error for error in spec.check(doc)), spec.check(doc))
        with self.assertRaises(Rejected):
            self.register(doc)


class TaskFiles(Case):
    def test_write_task_keeps_hidden_files_inside_the_task(self):
        """``write_task`` is the API for importing tasks (the pilot's 21 items will come through an adapter); a hidden
        file named ``../..`` or by an absolute path is written outside the task, over a registration or anywhere."""
        task = dict(tasks.fixture_task("pricing")[0], id="escape")
        bodies = json.dumps(tasks.fixture_task("pricing")[1]).encode()
        for name in ("../../outside.txt", str(self.folder / "outside.txt"), "sub/../../../outside.txt", "",
                     "..", "a\\..\\..\\outside.txt"):
            with self.subTest(name=name):
                with self.assertRaises(Rejected):
                    tasks.write_task(self.root / "tasks", task, {"checks.json": bodies, name: b"written"})
                self.assertEqual(list(self.folder.rglob("outside.txt")), [])

    def test_a_fixture_s_base_holds_every_fixture_file_whatever_the_host_ignores(self):
        """``materialize`` committed a fixture with ``git add -A``, which honours the operator's global ignore file
        (``~/.config/git/ignore``, ``core.excludesFile``): a host that ignores ``*.py`` committed an empty base, and
        every arm on that host worked on a repository without the task. ``source_digest`` pins the fixture's
        files, not the commit made of them, so nothing noticed."""
        home = self.folder / "home"
        (home / ".config" / "git").mkdir(parents=True)
        (home / ".config" / "git" / "ignore").write_text("*.py\n", encoding="utf-8")
        task = tasks.fixture_task("pricing")[0]
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            base = tasks.materialize(task, self.folder / "repo")
        self.assertEqual(git(self.folder / "repo", "ls-tree", "-r", "--name-only", base).split(),
                         sorted(tasks.fixture_files("pricing")))
        self.assertEqual(base, tasks.materialize(task, self.folder / "again"), "the same base on every host")


class Budget(Case):
    def capped(self, eval_id: str, adapter: list[str]) -> dict:
        doc = self.canary_spec(eval_id, adapter)
        doc["budget"] = dict(doc["budget"], dollars=CALL, total_dollars=0.008)
        self.register(doc)
        return doc

    def test_a_run_that_fails_after_it_spent_is_charged_against_the_cap(self):
        """A run that raises after its model calls (an interrupt, a harness error, a certificate that does not
        verify) leaves no bundle, and only bundles were counted: each retry spent again under a cap that saw
        nothing. Two failed attempts spend $0.006 of the $0.008 cap; a third run could reach $0.009."""
        self.capped("capfail", self.adapter({"changes": {"src/pricing.py": GOOD}}))
        with mock.patch.object(runner, "_chosen", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_eval("capfail", only=",single,")
        with mock.patch.object(runner, "_chosen", side_effect=RuntimeError("a harness error after the search")):
            with self.assertRaises(RuntimeError):
                self.run_eval("capfail", only=",single,")
        self.assertEqual(len(self.requests()), 2, "each failed attempt made its call")
        found = self.run_eval("capfail", only=",single,")
        self.assertEqual(len(self.requests()), 2, "a third call could take the eval over its cap")
        self.assertEqual(found["stopped"], "eval_budget_exhausted")
        self.assertAlmostEqual(found["spent_dollars"], 2 * CALL)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL is POSIX only; on Windows the test would fail early")
    @unittest.expectedFailure
    def test_a_run_killed_midway_is_charged_against_the_cap(self):
        """Design gap: a runner killed during a run (SIGKILL, an OOM kill, a power cut) runs no handler; its
        ``.partial`` is deleted on resume and what its calls cost is counted nowhere. Bounding it needs a policy
        (reserve the per-run dollars when a run starts, or stop the eval until the operator reconciles)."""
        self.capped("capkill", self.adapter({"changes": {"src/pricing.py": GOOD}, "kill_parent": True},
                                            {"changes": {"src/pricing.py": GOOD}}))
        done = subprocess.run(self.runner_process("capkill"), capture_output=True, timeout=300)
        self.assertEqual(done.returncode, -signal.SIGKILL, done.stderr)
        self.assertEqual(len(self.requests()), 1)
        found = self.run_eval("capkill", dry=True)
        self.assertGreaterEqual(found["spent_dollars"], CALL, "the killed run's call is counted somewhere")

    def test_a_second_runner_cannot_run_the_same_eval_at_once(self):
        """Two ``eval run`` processes on one eval each count only the bundles they found at start, so both pass
        the cap check, and both start the same first run (the second deletes the first one's ``.partial``)."""
        self.capped("twice", self.adapter({"changes": {"src/pricing.py": GOOD}, "hold": True},
                                          {"changes": {"src/pricing.py": GOOD}}))
        first = subprocess.Popen(self.runner_process("twice"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 120
            while not (self.log / "request-1.json").exists():
                self.assertIsNone(first.poll(), "the first runner ended before its call")
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            with self.assertRaises(Rejected) as caught:
                self.run_eval("twice", only=",single,")
            self.assertEqual(caught.exception.code, "eval_running")
            self.assertEqual(len(self.requests()), 1, "the second runner made no call")
        finally:
            (self.log / "release").write_text("go", encoding="utf-8")
            _, err = first.communicate(timeout=300)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(self.bundle("twice", "canary-pricing.single.s1")["outcome"]["status"], "accepted")


class Preflight(Case):
    PASS = (("ok", ["{python}", "-c", "print('fine')"]),)

    def test_the_preflight_gate_holds_for_every_run_not_only_the_first(self):
        """The gate was read once, when ``run`` started. An eval runs for hours; a checkout that moves or is edited
        after the first run (``git pull``, an edited adapter, which every call reads from disk again) ran every
        later run under a preflight that no longer describes it."""
        harness = self.folder / "harness"
        harness.mkdir()
        git(harness, "init", "-q")
        (harness / "README").write_text("harness\n", encoding="utf-8")
        git(harness, "add", "README")
        git(harness, "commit", "-qm", "one")
        self.root = harness / "evals"
        self.register(self.canary_spec("gate", self.adapter({"changes": {"src/pricing.py": GOOD}}), seeds=[1, 2]))
        self.assertTrue(runner.preflight(self.root, "gate", harness=harness, commands=self.PASS)["passed"])

        def log(line):
            if line.startswith("run ") and not (harness / "edited.py").exists():
                (harness / "edited.py").write_text("# changed after the gate\n", encoding="utf-8")

        found = runner.run_eval(self.root, "gate", only=",single,", harness=harness, log=log,
                                seed_env="SYBERLABS_EVAL_ADV_NO_SEED", work_root=self.folder / "work")
        self.assertEqual([row["state"] for row in found["runs"]], ["ran"])
        self.assertEqual(found["stopped"], "preflight_dirty")


class Bundles(Case):
    def setUp(self):
        super().setUp()
        self.register(self.canary_spec("ev", self.adapter({"changes": {"src/pricing.py": WRONG}},
                                                          {"changes": {"src/pricing.py": GOOD}}), seeds=[1, 2]))
        self.run_eval("ev", only=",repair,")
        self.runs = self.root / "ev" / "runs"
        self.assertEqual(sorted(p.name for p in self.runs.iterdir() if p.is_dir()),
                         ["canary-pricing.repair.s1", "canary-pricing.repair.s2"])

    def refused(self) -> str:
        with self.assertRaises(Rejected) as caught:
            self.run_eval("ev", dry=True)
        return caught.exception.code

    def test_a_bundle_whose_evidence_changed_is_refused(self):
        """``files`` holds a SHA-256 for every evidence file, and nothing compared them: an edited journal, trace or
        repository bundle was read as the run's evidence."""
        trace = self.runs / "canary-pricing.repair.s1" / "trace.json"
        trace.write_text(trace.read_text(encoding="utf-8").replace('"failed"', '"passed"'), encoding="utf-8")
        self.assertEqual(self.refused(), "bundle_tampered")

    def test_a_bundle_whose_dollars_were_edited_is_refused(self):
        """The cap sums ``outcome.dollars`` from bundle.json, which no digest covers: setting it to 0 bought room
        under the cap that the journal beside it shows was spent."""
        path = self.runs / "canary-pricing.repair.s1" / "bundle.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreater(bundle["outcome"]["dollars"], 0)
        bundle["outcome"]["dollars"] = 0.0
        spec.write_json(path, bundle)
        self.assertEqual(self.refused(), "bundle_tampered")

    def test_a_bundle_in_another_run_s_directory_is_refused(self):
        """``bundles`` keyed a bundle by the run id inside it, not by where it lies: a bundle copied over another
        run's directory made that run read as done."""
        first, second = self.runs / "canary-pricing.repair.s1", self.runs / "canary-pricing.repair.s2"
        shutil.rmtree(second)
        shutil.copytree(first, second)
        self.assertEqual(self.refused(), "bundle_tampered")

    def test_a_partial_run_is_never_done(self):
        """A run killed after it wrote ``bundle.json`` in ``<run>.partial`` and before the rename was read as done
        (the glob matched the partial directory), and its run never ran again."""
        done = self.runs / "canary-pricing.repair.s2"
        done.rename(self.runs / "canary-pricing.repair.s2.partial")
        found = self.run_eval("ev", dry=True)
        states = {row["run_id"]: row["state"] for row in found["runs"]}
        self.assertEqual(states["canary-pricing.repair.s2"], "pending")


class HiddenChecks(Case):
    """What candidate code can do while the host's hidden checks run it, in the host's process tree."""

    def test_a_candidate_that_rewrites_the_host_copy_of_a_check_is_not_accepted(self):
        """The host's copy is an ordinary directory, and its path is in the check's own process (the loader's
        ``__main__``, ``/proc/self/cmdline``). c1's code rewrites every hidden check to ``pass``; c2 changes
        nothing that matters and passes. ``Not editable`` held only for the candidate's own tree."""
        tamper = ("import __main__, json, os, stat\n"
                  "if getattr(__main__, 'path', None):\n"
                  "    passing = json.dumps({'discount': 'pass', 'tax': 'pass'})\n"
                  "    os.chmod(__main__.path, stat.S_IREAD | stat.S_IWRITE)  # the staged copy is read-only\n"
                  "    open(__main__.path, 'w', encoding='utf-8').write(passing)\n" + WRONG)
        self.register(self.canary_spec("tamper", self.adapter({"changes": {"src/pricing.py": tamper}},
                                                              {"changes": {"src/pricing.py": WRONG + "# two\n"}})))
        self.run_eval("tamper", only=",repair,")
        outcome = self.bundle("tamper", "canary-pricing.repair.s1")["outcome"]
        self.assertNotEqual(outcome["status"], "accepted", outcome)
        self.assertIsNone(outcome["accepted_commit"])
        self.assertEqual((outcome["reason"], outcome["hidden_intact"], outcome["hidden_changed"]),
                         ("hidden_tampered", False, ["checks.json"]))

    def test_only_pytest_s_own_cache_may_appear_in_the_host_copy(self):
        """A hidden pytest file with no configuration above it makes ``{hidden}`` pytest's rootdir, and its cache
        provider writes ``.pytest_cache`` there; anything else added, changed or removed is a change, a planted
        ``conftest.py`` or ``__pycache__`` (read even under ``PYTHONDONTWRITEBYTECODE``) above all."""
        before = {"checks.json": "a", "data/x.txt": "b"}
        cache = {".pytest_cache/v/cache/lastfailed": "c", "data/.pytest_cache/README.md": "d"}
        self.assertEqual(tasks.hidden_changes(before, {**before, **cache}), [])
        for after, changed in (({**before, "conftest.py": "e"}, ["conftest.py"]),
                               ({**before, "__pycache__/m.cpython-313.pyc": "f"}, ["__pycache__/m.cpython-313.pyc"]),
                               ({**before, ".pytest_cache": "g"}, [".pytest_cache"]),
                               ({"checks.json": "a"}, ["data/x.txt"]),
                               ({**before, "checks.json": "z"}, ["checks.json"])):
            with self.subTest(changed=changed):
                self.assertEqual(tasks.hidden_changes(before, after), changed)

    @unittest.expectedFailure
    def test_a_candidate_that_exits_at_import_does_not_pass_the_hidden_checks(self):
        """Design gap (a sandbox, or a loader that sees the check finish): every hidden check imports the
        candidate's module, and a module that ends the process with status 0 passes every check unread. The
        checks are judged by exit status alone (``syberlabs/checks.py``); ``sys.exit(0)`` and ``os._exit(0)`` both
        work. ``spec/GAPS.md`` 58 answers this for the ``tests`` check, not for hidden checks."""
        exits = {"changes": {"src/pricing.py": "import os\nos._exit(0)\n"}}
        self.register(self.canary_spec("exit", self.adapter(exits)))
        self.run_eval("exit", only=",single,")
        outcome = self.bundle("exit", "canary-pricing.single.s1")["outcome"]
        self.assertNotEqual(outcome["status"], "accepted", outcome)

    def leak(self, eval_id: str, mode: str) -> str:
        """A repair run under ``hidden_feedback`` ``mode`` whose c1 prints its hidden check's source and data from
        inside the check; returns the second request (the repair), after showing the leak reached the check's tail."""
        shutil.rmtree(self.log)
        self.log.mkdir()
        leak = ("import __main__, sys\nprint(getattr(__main__, 'body', ''))\n"
                "print(''.join(open(arg, encoding='utf-8').read() for arg in sys.argv[1:]))\n" + WRONG)
        self.register(self.canary_spec(eval_id, self.adapter({"changes": {"src/pricing.py": leak}},
                                                             {"changes": {"src/pricing.py": GOOD}}),
                                       hidden_feedback=mode))
        self.run_eval(eval_id, only=",repair,")
        journal = (self.root / eval_id / "runs" / "canary-pricing.repair.s1" / "journal.json").read_text(encoding="utf-8")
        self.assertIn(CANARY_CODE, journal, "the candidate printed the hidden check's source into its tail")
        self.assertIn(CANARY_DATA, journal, "the candidate printed the hidden check's data into its tail")
        self.assertGreaterEqual(len(self.requests()), 2, "the repair arm asked again after c1 failed")
        return self.requests()[1]

    @unittest.expectedFailure
    def test_a_candidate_cannot_carry_a_hidden_check_into_its_next_request(self):
        """Design gap under ``hidden_feedback: tails`` (a sandbox, or feedback the candidate cannot write): candidate
        code run by a hidden check reads the check's source from the loader's ``__main__.body`` and its data from
        ``sys.argv``, and prints both; under ``tails`` the output tail is the repair arm's feedback, so the next
        request holds the hidden check. EVAL.md's ``Not in a request`` holds only for a candidate that does not look,
        or under ``status`` and ``none`` (the next test)."""
        second = self.leak("leak", "tails")
        self.assertNotIn(CANARY_CODE, second)
        self.assertNotIn(CANARY_DATA, second)

    def test_under_status_or_none_a_candidate_cannot_carry_a_hidden_check_into_its_next_request(self):
        """``hidden_feedback`` ``status`` sends a hidden check's name and state only and ``none`` sends nothing of
        it, so what the candidate printed inside the check stays in the journal and out of the next request."""
        for mode in ("status", "none"):
            with self.subTest(mode=mode):
                second = self.leak(f"leak-{mode}", mode)
                self.assertNotIn(CANARY_CODE, second)
                self.assertNotIn(CANARY_DATA, second)


class ClaudeCodeArms(Case):
    """Two ``claude-code`` arms that differ only in ``githits``, the pilot's shape, with a stand-in ``claude``."""

    def setUp(self):
        super().setUp()
        self.claude = self.folder / "claude.py"
        self.claude.write_text(f"GOOD = {GOOD!r}\n" + textwrap.dedent(CLAUDE), encoding="utf-8")
        removed = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                   "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST")
        patcher = mock.patch.dict(os.environ, {"GITHITS_API_TOKEN": "test-not-a-token",
                                               "ANTHROPIC_API_KEY": "test-not-a-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in removed:
            os.environ.pop(name, None)

    def pilot(self, eval_id: str, *, declared: str, served: str) -> dict:
        written = tasks.import_fixtures(self.root / "tasks", ["pricing"])
        executable = ["{python}", str(self.claude), str(self.log), served]
        base = {"kind": "claude-code", "model": declared, "effort": "medium", "lane": "api-hooked",
                "trusted_tree": True, "max_calls": 1, "executable": executable}  # --githits refuses --bare
        doc = tiny_spec([w["id"] for w in written], {w["id"]: w["digest"] for w in written}, id=eval_id,
                        arms=[dict(base, name="off", githits=False), dict(base, name="on", githits=True)])
        doc["budget"] = dict(doc["budget"], prices={served: {"input_per_million": 2.0, "output_per_million": 10.0}})
        doc["decision_rule"] = dict(doc["decision_rule"], treatment="on", baseline="off")
        self.assertEqual(spec.check(doc), [])
        self.register(doc)
        return doc

    def calls(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.log.glob("call-*.json"))]

    def test_each_run_of_the_githits_arm_has_its_own_githits_session(self):
        """``arms.build`` passed the provider neither a thread label nor an arm, so every run of every task and seed
        sent ``X-GitHits-Session-ID: s1-b`` (the search id, then the default arm) and ``X-GitHits-Agent:
        syberfactory/b``: GitHits saw the whole pilot arm as one session, and nothing tied a lookup to its run."""
        self.pilot("sessions", declared="claude-opus-5", served="claude-opus-5")
        self.run_eval("sessions", only=",on,")
        bundles = [self.bundle("sessions", f"v1-pricing.on.s{seed}") for seed in (1, 2)]
        self.assertEqual([b["outcome"]["status"] for b in bundles], ["accepted", "accepted"])
        sessions = [call["session"] for call in self.calls()]
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len(set(sessions)), 2, sessions)
        for bundle, session in zip(bundles, sessions):
            self.assertIn(bundle["thread"], session)
        self.assertEqual({call["agent"] for call in self.calls()}, {"syberfactory/on"})

    def test_two_githits_arms_whose_agent_labels_collide_are_refused(self):
        """``githits_arm`` rewrites every character outside ``[A-Za-z0-9_-]`` and cuts the name to 16 characters, so
        ``a.b`` and ``a-b``, or two names that differ only after 16 characters, send one ``X-GitHits-Agent``."""
        doc = self.pilot("labels", declared="claude-opus-5", served="claude-opus-5")
        self.assertEqual(spec.check(doc), [])
        for first, second in (("a.b", "a-b"), ("githits-arm-long-1", "githits-arm-long-2")):
            with self.subTest(names=(first, second)):
                changed = json.loads(json.dumps(doc))
                changed["arms"][0].update(name=first, githits=True)
                changed["arms"][1].update(name=second, githits=True)
                changed["decision_rule"] = dict(changed["decision_rule"], treatment=second, baseline=first)
                errors = spec.check(changed)
                self.assertTrue(any("GitHits agent label" in e for e in errors), errors)
                changed["arms"][0]["githits"] = False  # only a githits arm sends the label
                self.assertEqual(spec.check(changed), [])

    @unittest.expectedFailure
    def test_a_run_served_by_another_model_than_its_arm_declares_is_not_reported_as_that_arm(self):
        """Design gap: the runner does not set Claude Code's model, and nothing compares the model that served
        (``signal.harness.model``) with the arm's registered ``model``. A Claude Code whose default moved (an update,
        an operator's ``/model``) runs both pilot arms on another model, and the bundle still says the declared one."""
        self.pilot("model", declared="claude-sonnet-5-5", served="claude-opus-5")
        self.run_eval("model", only=",off,1")
        bundle = self.bundle("model", "v1-pricing.off.s1")
        served = {e["body"]["signal"]["harness"]["model"]
                  for e in json.loads((self.root / "model" / "runs" / "v1-pricing.off.s1" / "journal.json")
                                      .read_text(encoding="utf-8"))["events"] if e["kind"] == "candidate_registered"}
        self.assertEqual(served, {"claude-opus-5"})
        self.assertFalse(bundle["outcome"]["status"] == "accepted" and bundle["provider"]["model"] not in served,
                         "an accepted run is reported as a model that did not serve it")


class RepoTasks(Case):
    @unittest.expectedFailure
    def test_a_repo_task_s_repository_holds_nothing_after_its_base(self):
        """Design gap: ``materialize`` clones every ref of the source and checks out the base, so a pilot item's run
        repository holds the upstream commits after its base, the fix itself when it was merged. Claude Code's
        file tools under ``--restricted`` cannot read ``.git`` outside the worktree, but candidate code run by the
        checks can (``git log --all``), any arm allowed ``Bash`` can, and ``repo.bundle`` exports all of it."""
        upstream = self.folder / "upstream"
        upstream.mkdir()
        git(upstream, "init", "-q", "-b", "main")
        (upstream / "src").mkdir()
        (upstream / "src" / "pricing.py").write_text(WRONG, encoding="utf-8")
        git(upstream, "add", "-A")
        git(upstream, "commit", "-qm", "base")
        base = git(upstream, "rev-parse", "HEAD")
        (upstream / "src" / "pricing.py").write_text(GOOD, encoding="utf-8")
        git(upstream, "commit", "-qam", "the fix")
        fix = git(upstream, "rev-parse", "HEAD")
        task = {"id": "repo-task", "source": f"repo:{upstream}@{base}"}
        repo = self.folder / "run" / "repo"
        self.assertEqual(tasks.materialize(task, repo), base)
        reachable = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", fix + "^{commit}"], capture_output=True)
        self.assertNotEqual(reachable.returncode, 0, "the fix commit is in the run's repository")


if __name__ == "__main__":
    unittest.main()
