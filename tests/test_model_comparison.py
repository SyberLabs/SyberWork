"""The model adapter seam, cost accounting, and the cost-matched comparison harness (simulated model only)."""

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from adapters import anthropic_adapter as adapter
from benchmarks import model_vs_patch as harness
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


if __name__ == "__main__":
    unittest.main()
