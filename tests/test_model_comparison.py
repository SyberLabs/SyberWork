"""The model adapter seam, cost accounting, the cost-matched comparison harness (simulated model only), and the v2 fixtures."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from adapters import anthropic_adapter as adapter
from benchmarks import model_vs_patch as harness
from benchmarks import simulated_model as simulated
from benchmarks.fixtures import FIXTURES, V2
from syberlabs.evolve import CommandMutator, EvolutionaryProvider
from syberlabs.providers import CommandProvider, model_usage
from tests.test_build_thread import GOOD, RepoCase

USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "model": "claude-opus-5", "stop_reason": "end_turn"}


class Adapter(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "report.py").write_text("def rows():\n    return []\n")

    def test_prompts_carry_objective_scope_and_files(self):
        prompt = adapter.build_prompt({"protocol": "syberlabs.search/v0alpha1", "objective": "Add CSV", "scope": ["src"],
                                       "context": [{"path": "src/report.py"}], "files": ["src/report.py", "../outside.txt"]},
                                      self.root)
        self.assertIn("Objective: Add CSV", prompt)
        self.assertIn('<file path="src/report.py">', prompt)
        self.assertNotIn("outside", prompt, "files outside the checkout are never read")
        repair = adapter.build_prompt({"protocol": "syberlabs.search/v0alpha1", "objective": "x", "scope": ["src"],
                                       "feedback": {"files": {"src/a.py": "A"}, "checks": [
                                           {"name": "tests", "state": "failed", "output_tail": "AssertionError: 1 != 2"},
                                           {"name": "lint", "state": "passed", "output_tail": ""}]}}, self.root)
        self.assertIn("AssertionError: 1 != 2", repair)
        self.assertNotIn("--- lint", repair)
        mutate = adapter.build_prompt({"protocol": "syberlabs.mutate/v0alpha1", "objective": "x", "scope": ["src"],
                                       "parent": "c3", "seed": 9, "files": {"src/a.py": "A"}}, self.root)
        self.assertIn("mutation of this candidate", mutate)
        with self.assertRaises(ValueError):
            adapter.build_prompt({"protocol": "other"}, self.root)

    def test_an_absent_or_empty_scope_is_never_stated_as_empty(self):
        """Before 2026-10-22 the mutate request carried no scope and the prompt read ``Mutable scope: `` (nothing);
        Sonnet 5.5, told to write only inside the stated scope, returned no changes on every evolve call."""
        for protocol, extra in (("syberlabs.mutate/v0alpha1", {"parent": "c3", "seed": 9, "files": {"src/a.py": "A"}}),
                                ("syberlabs.search/v0alpha1", {"context": [{"path": "src/report.py"}], "files": ["src/report.py"]})):
            for request in ({"protocol": protocol, "objective": "x", **extra},
                            {"protocol": protocol, "objective": "x", "scope": [], **extra},
                            {"protocol": protocol, "objective": "x", "scope": None, **extra}):
                with self.subTest(protocol=protocol, scope=request.get("scope", "absent")):
                    prompt = adapter.build_prompt(request, self.root)
                    self.assertIn("Mutable scope: the files shown below", prompt)
                    self.assertNotIn("Mutable scope: \n", prompt)
            with self.subTest(protocol=protocol, scope="stated"):
                prompt = adapter.build_prompt({"protocol": protocol, "objective": "x", "scope": ["src/", "docs"], **extra}, self.root)
                self.assertIn("Mutable scope: src/, docs", prompt)
                self.assertNotIn("the files shown below", prompt)
        self.assertIn("when no scope is stated, the files shown in the request", adapter.SYSTEM)

    def test_responses_follow_each_protocol_and_report_usage(self):
        parsed = {"message": "csv", "rationale": "reuse rows()",
                  "files": [{"path": "src/export.py", "content": "x", "delete": False},
                            {"path": "src/old.py", "content": "", "delete": True}]}
        search = adapter.respond({"protocol": "syberlabs.search/v0alpha1", "objective": "x"}, self.root, lambda p: (parsed, USAGE))
        self.assertEqual(search["candidates"][0]["changes"], {"src/export.py": "x", "src/old.py": None})
        self.assertEqual((search["recommended"], search["usage"]), ([0], USAGE))
        mutate = adapter.respond({"protocol": "syberlabs.mutate/v0alpha1", "files": {}}, self.root, lambda p: (parsed, USAGE))
        self.assertEqual(set(mutate), {"changes", "usage"})
        refused = adapter.respond({"protocol": "syberlabs.search/v0alpha1"}, self.root,
                                  lambda p: (None, {**USAGE, "stop_reason": "refusal"}))
        self.assertEqual((refused["candidates"], refused["usage"]["stop_reason"]), ([], "refusal"))

    def test_schema_is_strict(self):
        self.assertFalse(adapter.SCHEMA["additionalProperties"])
        self.assertFalse(adapter.SCHEMA["properties"]["files"]["items"]["additionalProperties"])


class Accounting(RepoCase):
    def test_usage_is_filtered_and_recorded_as_a_signal(self):
        self.assertEqual(model_usage({"usage": {"input_tokens": 5, "output_tokens": -1, "secret": "x", "model": "m"}}),
                         {"input_tokens": 5, "model": "m"})
        self.assertIsNone(model_usage({"usage": "lots"}))
        script = self.root.parent / "model.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            json.load(sys.stdin)
            print(json.dumps({{"candidates": [{{"changes": {json.dumps(GOOD)}, "message": "m"}}], "recommended": [0],
                              "usage": {json.dumps(USAGE)}}}))
            '''))
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        provider = CommandProvider([sys.executable, str(script)], name="model", revision="1")
        [candidate] = thread.propose(provider)
        self.assertEqual((provider.calls, provider.usage), (1, [USAGE]))
        self.assertEqual(candidate.signal["model_usage"]["output_tokens"], 300)

    def test_mutator_call_cap_and_duplicate_refusal(self):
        script = self.root.parent / "mutator.py"
        script.write_text("import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'changes': {'src/n.py': 'x = 1\\n'}}))\n")
        kit = self.kit(self.contract(**{"evolution.operators": ["patch", "mutation", "crossover"]}))
        thread = kit.start("Add a CSV export")
        mutator = CommandMutator([sys.executable, str(script)], max_calls=3)
        found = thread.propose(EvolutionaryProvider(mutator, population=3, generations=5), seed=1)
        self.assertEqual(mutator.calls, 3)
        self.assertTrue(mutator.exhausted)
        self.assertEqual(len(found), 1, "identical results are refused as duplicates, so only one is recorded")
        self.assertEqual(thread.propose(changes={"src/n.py": "x = 1\n"}), [])
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual((search["stopped"], search["error"]), ("error", "duplicate"))

    def test_dollars_use_the_price_table(self):
        self.assertAlmostEqual(harness.dollars([USAGE]), (1200 * 5 + 300 * 25) / 1e6)
        self.assertAlmostEqual(harness.dollars([{**USAGE, "cache_read_input_tokens": 1000}]), (1200 * 5 + 300 * 25 + 1000 * 0.5) / 1e6)
        self.assertIsNone(harness.dollars([{**USAGE, "model": "unknown-model"}]))


class Harness(unittest.TestCase):
    def test_every_arm_respects_the_call_budget_and_is_labelled_simulated(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [harness.run("slugify", arm, harness.SIMULATED, 3, seed, Path(folder)) for arm in harness.ARMS for seed in (1, 2)]
            again = harness.run("slugify", "repair", harness.SIMULATED, 3, 1, Path(folder) / "again")
        for row in rows:
            self.assertLessEqual(row["calls"], 1 if row["arm"] == "single" else 3)
            self.assertTrue(row["simulated"])
            self.assertIsNone(row["dollars"])
            self.assertGreater(row["input_tokens"], 0)
        first = next(r for r in rows if r["arm"] == "repair" and r["seed"] == 1)
        self.assertEqual({k: again[k] for k in ("solved", "calls", "candidates")}, {k: first[k] for k in ("solved", "calls", "candidates")})
        report = harness.summarize(rows, 3)
        self.assertTrue(report.startswith("SIMULATED MODEL"))
        self.assertIn("best_of_k", report)


REFERENCE_FIXES = {
    "crlf_export": {"src/fields.py": '''"""Field report export: render a table and compare it with the committed export."""


def render(rows):
    return "\\n".join(rows)


def load(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8").replace("\\r\\n", "\\n")


def lines(path):
    return load(path).splitlines()


def matches(path, rows):
    return render(rows) in load(path)
'''},
    "oauth_state": {"src/oauth.py": '''"""OAuth connect flow: the state round trip."""

import random
import string

ALPHABET = string.ascii_lowercase + string.digits


def make_state(seed):
    rng = random.Random(seed)
    return "".join(rng.choice(ALPHABET) for _ in range(24))


def tamper(state):
    last = "y" if state[-1] == "x" else "x"
    return state[:-1] + last


def finish_connect(expected, returned):
    return returned == expected
'''},
    "settlement": {"src/ledger.py": '''"""Settle a reading against a target, durably, as a settlement record and then a verdict."""

import json


def verdict(reading, target):
    return "moved" if reading >= target else "stayed"


def append(path, record):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\\n")


def records(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def settle(path, reading, target, die_between=None):
    append(path, {"kind": "settlement", "reading": reading, "target": target})
    if die_between is not None:
        die_between()
    append(path, {"kind": "verdict", "verdict": verdict(reading, target)})


def recover(path, reading, target):
    found = records(path)
    if not found:
        settle(path, reading, target)
        return True
    last = found[-1]
    if last["kind"] == "settlement":
        append(path, {"kind": "verdict", "verdict": verdict(last["reading"], last["target"])})
        return True
    return False
'''},
    "tool_path": {"src/launch.py": '''"""Launch a repository's tool by name from a list of directories."""

import os
import subprocess

SUFFIXES = ("", ".cmd", ".bat", ".exe")


class ToolNotFound(Exception):
    pass


def resolve(name, dirs):
    for folder in dirs:
        for suffix in SUFFIXES:
            candidate = os.path.join(folder, name + suffix)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    raise ToolNotFound(name)


def run_tool(name, args, dirs):
    done = subprocess.run([resolve(name, dirs), *args], capture_output=True, text=True,
                          env={**os.environ, "PATH": os.pathsep.join(dirs)})
    return done.stdout.strip()
'''},
}


def materialise(name: str, root: Path, fix: dict | None = None) -> None:
    for path, text in FIXTURES[name]["files"].items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8", newline="\n")
    for path, text in (fix or {}).items():
        (root / path).write_text(text, encoding="utf-8", newline="\n")


def check_states(name: str, root: Path) -> dict:
    """Run a fixture's check commands in ``root`` the way the kit does: argv, no shell, worktree as cwd."""
    out = {}
    for check, spec in FIXTURES[name]["checks"].items():
        done = subprocess.run(spec["argv"], cwd=root, capture_output=True, text=True, timeout=spec["timeout_seconds"])
        out[check] = "passed" if done.returncode == 0 else "failed"
    return out


class FixturesV2(unittest.TestCase):
    """The four fixtures built from this program's own defects (docs/E3_BENCHMARK_PLAN.md, Fixtures v2)."""

    def test_the_four_fixtures_exist_with_three_checks_and_no_correct_slot(self):
        self.assertEqual(sorted(V2), ["crlf_export", "oauth_state", "settlement", "tool_path"])
        for name in V2:
            self.assertEqual(len(FIXTURES[name]["checks"]), 3, name)
            self.assertEqual(sorted(harness.contract(name)["evolution"]["evaluation"]["required"]), sorted(FIXTURES[name]["checks"]))
            for slot in FIXTURES[name]["slots"]:
                self.assertEqual(slot["correct"], [], f"{name}: a v2 slot must hold no correct variant")
        self.assertIsInstance(FIXTURES["crlf_export"]["files"]["src/export.txt"], bytes)
        self.assertIn(b"\r\n", FIXTURES["crlf_export"]["files"]["src/export.txt"])

    def test_every_check_fails_on_the_broken_source_and_passes_on_the_reference_fix(self):
        for name in V2:
            with tempfile.TemporaryDirectory() as folder:
                broken, fixed = Path(folder) / "broken", Path(folder) / "fixed"
                broken.mkdir()
                fixed.mkdir()
                materialise(name, broken)
                materialise(name, fixed, REFERENCE_FIXES[name])
                before, after = check_states(name, broken), check_states(name, fixed)
            self.assertEqual(set(before.values()), {"failed"}, (name, before))
            self.assertEqual(set(after.values()), {"passed"}, (name, after))

    def test_the_flaky_fixture_has_a_seed_where_the_tampered_state_is_the_real_one(self):
        """One state in 36 ends in "x" (the alphabet is 36 characters), so the no-op tamper is deterministic in the harness."""
        import random
        import string
        alphabet = string.ascii_lowercase + string.digits
        ending_in_x = [seed for seed in range(1, 201)
                       if "".join(random.Random(seed).choice(alphabet) for _ in range(24)).endswith("x")]
        self.assertTrue(ending_in_x, "no seed in 1..200 ends in x; the fixture would not flake")


class HarnessV2(unittest.TestCase):
    def test_single_does_not_solve_the_v2_fixtures_with_the_simulated_model_and_journals_sit_beside_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            journals = Path(folder) / "out-journals"
            rows = [harness.run(name, "single", harness.SIMULATED, 6, 1, Path(folder), journals) for name in V2]
            for row in rows:
                self.assertFalse(row["solved"], row)
                self.assertEqual((row["calls"], row["candidates"], row["model"], row["provider_error"]),
                                 (1, 1, "simulated", None), row)
                self.assertEqual(row["journal"], f"out-journals/{row['fixture']}-single-1.json")
                exported = json.loads((Path(folder) / row["journal"]).read_text(encoding="utf-8"))
                self.assertTrue(exported["chain_valid"])
                evaluated = [e for e in exported["events"] if e["kind"] == "candidate_evaluated"]
                self.assertEqual(len(evaluated), 1)
                self.assertIn("failed", {c["state"] for c in evaluated[0]["body"]["checks"]}, row["fixture"])
            without = harness.run("tool_path", "single", harness.SIMULATED, 6, 2, Path(folder) / "none")
        self.assertIsNone(without["journal"], "no --write, no journal")
        report = harness.summarize(rows, 6)
        self.assertIn("simulated", report)
        self.assertIn("provider_error", report.splitlines()[1])
        for name in V2:
            self.assertRegex(report, rf"{name}\s+single\s+0/1")

    def test_rows_name_the_served_model_and_the_summary_shows_provider_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing_adapter.py"
            row = harness.run("pricing", "single", f"{sys.executable} {missing}", 3, 1, Path(folder))
        self.assertEqual((row["provider_error"], row["model"], row["candidates"], row["solved"]), ("provider_failed", None, 0, False))
        served = {**row, "seed": 2, "provider_error": None, "model": "claude-sonnet-5-5", "simulated": False,
                  "input_tokens": 700, "output_tokens": 300, "dollars": 0.0044, "solved": True, "candidates": 1}
        report = harness.summarize([row, served], 3)
        line = next(l for l in report.splitlines() if l.startswith("pricing"))
        self.assertIn("claude-sonnet-5-5", line)
        self.assertIn("1/2 provider_failed", line)
        clean = harness.summarize([served], 3)
        self.assertIn("none", next(l for l in clean.splitlines() if l.startswith("pricing")))


POLICY_ARMS = ("evolve", "evolve_empirical", "evolve_jev")


class SimulatedProfiles(unittest.TestCase):
    """The simulated adapter's per-operator profiles. Simulated: nothing here is about a model."""

    def mutate(self, profile, parent, text=None):
        os.environ["SYBERLABS_FIXTURE"] = "pricing"
        if profile is None:
            os.environ.pop("SIM_PROFILE", None)
        else:
            os.environ["SIM_PROFILE"] = profile
        self.addCleanup(os.environ.pop, "SIM_PROFILE", None)
        text = text or FIXTURES["pricing"]["files"]["src/pricing.py"]
        with tempfile.TemporaryDirectory() as folder:
            out = {}
            for seed in range(1, 21):
                request = {"protocol": "syberlabs.mutate/v0alpha1", "seed": seed, "parent": parent, "objective": "x",
                           "context": [], "files": {"src/pricing.py": text}}
                found = simulated.respond(request, Path(folder))["changes"]["src/pricing.py"]
                out[seed] = [simulated.is_right(found, slot) for slot in FIXTURES["pricing"]["slots"]]
            return out

    def test_mutation_rare_fixes_one_slot_from_base_and_rarely_from_a_child(self):
        from_base = self.mutate("mutation_rare", "base")
        self.assertTrue(all(sum(right) == 1 for right in from_base.values()), from_base)
        self.assertEqual(len({tuple(r) for r in from_base.values()}), 3, "children from base fix different slots")
        from_child = self.mutate("mutation_rare", "c1")
        fixes = sum(sum(right) for right in from_child.values())
        self.assertTrue(0 < fixes < 10, (fixes, from_child))

    def test_mutation_common_rewrites_every_remaining_slot(self):
        rows = self.mutate("mutation_common", "c1")
        self.assertTrue(any(all(right) for right in rows.values()), rows)
        text = FIXTURES["pricing"]["files"]["src/pricing.py"]
        os.environ["SIM_PROFILE"] = "mutation_common"
        with tempfile.TemporaryDirectory() as folder:
            found = simulated.respond({"protocol": "syberlabs.mutate/v0alpha1", "seed": 3, "parent": "c1", "objective": "x",
                                       "context": [], "files": {"src/pricing.py": text}}, Path(folder))["changes"]["src/pricing.py"]
        for slot in FIXTURES["pricing"]["slots"]:
            self.assertNotEqual(simulated.current(found, slot), simulated.current(text, slot), slot["function"])

    def test_no_profile_keeps_the_single_slot_reroll(self):
        rows = self.mutate(None, "base")
        self.assertTrue(all(sum(right) <= 1 for right in rows.values()), rows)
        self.assertNotIn("SIM_PROFILE", os.environ)
        with self.assertRaises(ValueError):
            harness.run("pricing", "single", harness.SIMULATED, 1, 1, Path(tempfile.gettempdir()), profile="no_such_profile")


class PolicyArms(unittest.TestCase):
    """The three evolve arms told apart on the ``mutation_rare`` profile, K = 12, five seeds, through the real harness.

    Simulated, so a statement about the harness and the profile, not about a model: where a
    crossover combines what two mutations fixed, Jev buys crossovers early and both learning
    policies reach a passing candidate in fewer model calls than the fixed schedule.
    """

    CALLS, SEEDS, PROFILE = 12, (1, 2, 3, 4, 5), "mutation_rare"

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.journals = Path(cls.folder.name) / "rows-journals"
        cls.rows = [harness.run("pricing", arm, harness.SIMULATED, cls.CALLS, seed, Path(cls.folder.name), cls.journals, cls.PROFILE)
                    for arm in POLICY_ARMS for seed in cls.SEEDS]

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def totals(self, key):
        return {arm: sum(r[key] for r in self.rows if r["arm"] == arm) for arm in POLICY_ARMS}

    def test_every_row_names_its_policy_and_the_profile_and_stays_within_the_budget(self):
        expected = {"evolve": ("fixed", "2"), "evolve_empirical": ("empirical", "2"), "evolve_jev": ("jev", "1")}
        for row in self.rows:
            self.assertEqual((row["policy"]["name"], row["policy"]["revision"]), expected[row["arm"]], row)
            self.assertEqual((row["profile"], row["simulated"], row["dollars"], row["model"]), (self.PROFILE, True, None, "simulated"))
            self.assertLessEqual(row["calls"], self.CALLS)
            self.assertGreater(row["host_dollars"], 0.0, "the priced contract makes the host observe the simulated usage")
            exported = json.loads((Path(self.folder.name) / row["journal"]).read_text(encoding="utf-8"))
            signals = [e["body"]["signal"]["policy"] for e in exported["events"] if e["kind"] == "candidate_registered"]
            self.assertTrue(signals and all((s["name"], s["revision"]) == expected[row["arm"]] for s in signals), row["arm"])
            spend = [e["body"]["spend"]["dollars"] for e in exported["events"] if e["kind"] == "candidate_registered"]
            self.assertTrue(all(d is not None for d in spend), row["arm"])

    def test_jev_needs_fewer_calls_and_fewer_simulated_dollars_than_the_fixed_schedule(self):
        calls, dollars, solved = self.totals("calls"), self.totals("host_dollars"), self.totals("solved")
        self.assertLess(calls["evolve_jev"], calls["evolve"], calls)
        self.assertLess(dollars["evolve_jev"], dollars["evolve"], dollars)
        self.assertGreaterEqual(solved["evolve_jev"], solved["evolve"], solved)

    def test_empirical_needs_fewer_calls_than_the_fixed_schedule_where_crossover_pays(self):
        calls = self.totals("calls")
        self.assertLess(calls["evolve_empirical"], calls["evolve"], calls)

    def test_the_summary_names_the_profile_and_the_policies(self):
        report = harness.summarize(self.rows, self.CALLS)
        self.assertIn("Simulated profile: mutation_rare.", report.splitlines()[0])
        self.assertIn("policy", report.splitlines()[1])
        for label in ("fixed/2", "empirical/2", "jev/1"):
            self.assertIn(label, report)


class JevPrices(unittest.TestCase):
    def test_the_prior_comes_from_the_contract_or_is_none(self):
        doc = harness.contract("pricing")
        self.assertIsNone(harness.jev_prices(doc))
        doc["evolution"]["budget"] = {**doc["evolution"]["budget"], "prices": harness.price_table(), "max_dollars": harness.MAX_DOLLARS}
        prices = harness.jev_prices(doc)
        self.assertEqual((prices["crossover"], prices["evaluate"]), (0.0, 0.0))
        self.assertGreater(prices["mutate"], 0.0)
        self.assertIn("simulated", harness.price_table())
        self.assertEqual(set(harness.price_table()) - {"simulated"}, set(harness.PRICES))


if __name__ == "__main__":
    unittest.main()
