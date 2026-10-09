"""The EvoGit-style provider behind SearchProvider. SyberWork keeps authority."""

import json
import random
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from examples.evolve import PRICING, RuleMutator, contract, git, make_repository
from spec.validate import validate_events
from syberlabs import Kit, Rejected
from syberlabs.evolve import CommandMutator, EvolutionaryProvider, resolve_conflicts

FIXED = {
    "discount": "return total * 0.9 if total > 100 else total",
    "tax": "return round(amount * 1.08, 2)",
    "shipping": "return weight * 5",
}


def fix(text, *rules):
    for rule in rules:
        lines = text.splitlines(keepends=True)
        at = next(i for i, line in enumerate(lines) if line.startswith(f"def {rule}("))
        lines[at + 1] = f"    {FIXED[rule]}\n"
        text = "".join(lines)
    return text


class Evolution(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "shop"
        make_repository(self.root)
        self.home = self.root / ".syberlabs"
        (self.home / "contracts").mkdir(parents=True)

    def kit(self, doc=None):
        doc = doc or contract()
        (self.home / "contracts" / f"{doc['id']}.v{doc['version']}.json").write_text(json.dumps(doc))
        kit = Kit.local(self.home, self.root, actor="dev@example.test")
        self.addCleanup(kit.close)
        return kit

    def test_crossover_combines_lineages_and_the_child_is_judged_on_its_own(self):
        kit = self.kit()
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
        [a] = thread.propose(changes={"src/pricing.py": fix(PRICING, "discount")}, message="person A")
        [b] = thread.propose(changes={"src/pricing.py": fix(PRICING, "tax", "shipping")}, message="person B")
        provider = EvolutionaryProvider(RuleMutator(), population=2, generations=1, crossover_every=1, seeds=[a.id, b.id])
        [child] = thread.propose(provider)
        self.assertEqual((child.operator, set(child.parents)), ("crossover", {a.id, b.id}))
        commits = {a.id: a.commit, b.id: b.commit}
        self.assertEqual(git(self.root, "rev-list", "--parents", "-n", "1", child.commit).split()[1:],
                         [commits[parent] for parent in child.parents], "recorded lineage matches the Git parents")
        evaluated = [e["body"]["candidate"] for e in thread.history() if e["kind"] == "candidate_evaluated"]
        self.assertEqual(evaluated, [a.id, b.id, child.id], "the child is evaluated even though lineage exists")
        views = {c.id: c for c in thread.candidates()}
        self.assertEqual([views[a.id].evaluation, views[b.id].evaluation, views[child.id].evaluation], ["failed", "failed", "passed"])
        self.assertFalse(views[child.id].authoritative)
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual(search["recommended"], [child.id])
        self.assertEqual(thread.accept(child.id).status, "succeeded")

    def test_evolved_candidates_stay_provisional_until_a_person_accepts(self):
        kit = self.kit()
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
        found = thread.propose(EvolutionaryProvider(RuleMutator(), population=4, generations=10, crossover_every=3), seed=7)
        self.assertGreater(len(found), 4)
        self.assertEqual(git(self.root, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines(), ["refs/heads/main"])
        self.assertEqual(len(git(self.root, "for-each-ref", "--format=%(refname)", "refs/syberlabs/candidates").splitlines()), len(found))
        self.assertTrue(all(c.promotion == "provisional" and not c.authoritative for c in thread.candidates()))
        self.assertTrue(any(c.operator == "crossover" for c in found))
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        best = search["recommended"][0]
        self.assertEqual(thread.accept(best, actor="planner", roles=["developer", "model"]).reason, "candidate_promotion_origin")
        self.assertEqual(thread.accept(best).status, "succeeded")
        self.assertEqual(validate_events(thread.history(), "evolution"), [])

    def test_operators_must_be_in_the_contract(self):
        doc = contract()
        doc["evolution"]["operators"] = ["patch"]
        kit = self.kit(doc)
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
        with self.assertRaises(Rejected) as caught:
            thread.propose(EvolutionaryProvider(RuleMutator()))
        self.assertEqual(caught.exception.code, "operator_not_permitted")
        self.assertFalse(any(e["kind"].startswith("search") for e in thread.history()))

    def test_budget_stops_the_search_cleanly(self):
        doc = contract()
        doc["evolution"]["budget"] = {"max_candidates": 5, "max_evaluations": 5, "max_seconds": 60}
        kit = self.kit(doc)
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
        found = thread.propose(EvolutionaryProvider(RuleMutator(), population=4, generations=50), seed=3)
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertLessEqual(len(found), 5)
        self.assertLessEqual(search["evaluations"], 5)
        self.assertEqual(search["stopped"], "completed")
        with self.assertRaises(Rejected):
            thread.propose(EvolutionaryProvider(RuleMutator()))

    def test_out_of_scope_mutations_are_recorded_not_run(self):
        kit = self.kit()
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")

        def escapes(space, parent, rng):
            return {"setup.py": "import os; os.system('echo pwned')\n"}

        found = thread.propose(EvolutionaryProvider(escapes, population=2, generations=1), seed=1)
        self.assertTrue(found)
        self.assertTrue(all(c.scope_violations == ("setup.py",) for c in found))
        self.assertFalse(any(e["kind"] == "candidate_evaluated" for e in thread.history()))

    def test_command_mutator_is_the_model_seam(self):
        script = self.root.parent / "model.py"
        script.write_text(textwrap.dedent('''\
            import json, sys
            request = json.load(sys.stdin)
            assert request["protocol"] == "syberlabs.mutate/v0alpha1"
            text = request["files"]["src/pricing.py"]
            print(json.dumps({"changes": {"src/pricing.py": text.replace("return 0", "return weight * 5")}}))
            '''))
        kit = self.kit()
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
        thread.propose(changes={"src/pricing.py": fix(PRICING, "discount", "tax")})
        mutator = CommandMutator([sys.executable, str(script)], name="fixture-model")
        found = thread.propose(EvolutionaryProvider(mutator, population=2, generations=1, seeds=["c1"]))
        self.assertTrue(any(c.evaluation == "passed" and c.signal["mutator"] == "fixture-model" for c in found))

    def test_evolve_from_the_terminal(self):
        import contextlib
        import io
        from syberlabs.cli import main as cli
        script = self.root.parent / "model.py"
        script.write_text(textwrap.dedent('''\
            import json, random, sys
            request = json.load(sys.stdin)
            rng = random.Random(request["seed"])
            text = request["files"].get("src/pricing.py") or open("src/pricing.py").read()
            fixes = [("return total\\n", "return total * 0.9 if total > 100 else total\\n"),
                     ("return amount\\n", "return round(amount * 1.08, 2)\\n"), ("return 0\\n", "return weight * 5\\n")]
            old, new = rng.choice(fixes)
            print(json.dumps({"changes": {"src/pricing.py": text.replace(old, new, 1)}}))
            '''))
        self.kit().close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli(["--repo", str(self.root), "--home", str(self.home), "--actor", "dev", "start", "Fix pricing",
                 "--contract", "pricing-evolution.v1"])
            code = cli(["--repo", str(self.root), "--home", str(self.home), "--actor", "dev", "propose",
                        "--evolve", f"{sys.executable} {script}", "--name", "fixture-model", "--generations", "8",
                        "--seed", "2"])
            cli(["--repo", str(self.root), "--home", str(self.home), "--actor", "dev", "status"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("[fixture-model@1]", out.getvalue())  # an evolved candidate carries --name
        self.assertNotIn("[evolutionary@", out.getvalue())
        self.assertIn("provider recommends", out.getvalue())
        self.assertIn("(a signal, not a verdict)", out.getvalue())

    def test_same_thread_api_with_another_provider(self):
        kit = self.kit()
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
        [manual] = thread.propose(changes={"src/pricing.py": fix(PRICING, "discount", "tax", "shipping")})
        self.assertTrue(thread.check(manual.id).acceptable)


class Conflicts(unittest.TestCase):
    def test_each_region_is_resolved_by_the_seeded_coin(self):
        text = "a\n<<<<<<< ours\nB\n=======\nb\n>>>>>>> theirs\nc\n<<<<<<< ours\nD\n=======\nd\n>>>>>>> theirs\n"
        self.assertEqual(resolve_conflicts(text, random.Random(0), 1.0), "a\nB\nc\nD\n")
        self.assertEqual(resolve_conflicts(text, random.Random(0), 0.0), "a\nb\nc\nd\n")
        self.assertEqual(resolve_conflicts("x\n<<<<<<< ours\ny\n", random.Random(0), 0.5), "x\n<<<<<<< ours\ny\n")


if __name__ == "__main__":
    unittest.main()
