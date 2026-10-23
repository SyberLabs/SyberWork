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
from syberlabs.canonical import canonical
from syberlabs.evolve import CommandMutator, EvolutionaryProvider, FixedPolicy, resolve_conflicts
from syberlabs.providers import USAGE_FIELDS
from syberlabs.search import ACTION_KINDS, BranchPolicy, Mutate, Score

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


# The branch-policy seam. FixedPolicy must be the loop that ran before it, decision for decision.

OPTIONS = ("population", "generations", "crossover_every", "stop_when_passing")

# Recorded on main d0bebb8 with the pre-seam provider (FACTORY/exec/r3-evolve-expect/record_expectation.py, run
# twice; only commit ids differed between the runs, so they are not compared). Keyed by (seed, *OPTIONS).
# Candidates: (id, operator, parents, evaluation, checks passed). Trace: the legacy-shaped entries only.
EXPECTED = {
    (7, 4, 10, 3, True): {
        "candidates": [("c1", "mutation", (), "failed", 1), ("c2", "mutation", (), "failed", 1), ("c3", "mutation", (), "failed", 0),
                       ("c4", "mutation", (), "failed", 0), ("c5", "mutation", ("c1",), "failed", 1), ("c6", "mutation", ("c4",), "failed", 1),
                       ("c7", "mutation", ("c1",), "failed", 1), ("c8", "mutation", ("c2",), "failed", 1), ("c9", "mutation", ("c6",), "failed", 1),
                       ("c10", "crossover", ("c3", "c6"), "failed", 1), ("c11", "crossover", ("c1", "c2"), "failed", 2),
                       ("c12", "mutation", ("c11",), "failed", 1), ("c13", "crossover", ("c11", "c6"), "failed", 2),
                       ("c14", "mutation", ("c11",), "failed", 2), ("c15", "mutation", ("c11",), "failed", 2),
                       ("c16", "mutation", ("c11",), "failed", 2), ("c17", "mutation", ("c11",), "passed", 3),
                       ("c18", "mutation", ("c3",), "failed", 0)],
        "trace": [{"generation": 0, "population": ["c1", "c2", "c3", "c4"]}, {"generation": 1, "population": ["c1", "c2", "c3", "c6"]},
                  {"generation": 2, "population": ["c1", "c2", "c3", "c6"]}, {"generation": 3, "population": ["c11", "c11", "c3", "c6"]},
                  {"generation": 4, "population": ["c11", "c11", "c3", "c6"]}, {"generation": 5, "population": ["c11", "c11", "c3", "c6"]},
                  {"generation": 6, "population": ["c11", "c11", "c3", "c6"]}, {"generation": 7, "population": ["c11", "c11", "c3", "c6"]},
                  {"generation": 8, "population": ["c11", "c17", "c3", "c6"]}],
        "recommended": ["c17"], "evaluations": 18},
    (3, 4, 50, 3, True): {
        "candidates": [("c1", "mutation", (), "failed", 0), ("c2", "mutation", (), "failed", 0), ("c3", "mutation", (), "failed", 0),
                       ("c4", "mutation", (), "failed", 1), ("c5", "mutation", ("c1",), "failed", 0), ("c6", "mutation", ("c2",), "failed", 1),
                       ("c7", "mutation", ("c3",), "failed", 1), ("c8", "mutation", ("c4",), "failed", 1), ("c9", "mutation", ("c1",), "failed", 1),
                       ("c10", "crossover", ("c7", "c9"), "failed", 2), ("c11", "crossover", ("c6", "c4"), "failed", 1),
                       ("c12", "mutation", ("c10",), "failed", 1), ("c13", "mutation", ("c10",), "passed", 3)],
        "trace": [{"generation": 0, "population": ["c1", "c2", "c3", "c4"]}, {"generation": 1, "population": ["c1", "c6", "c7", "c4"]},
                  {"generation": 2, "population": ["c9", "c6", "c7", "c4"]}, {"generation": 3, "population": ["c10", "c6", "c10", "c4"]},
                  {"generation": 4, "population": ["c10", "c6", "c13", "c4"]}],
        "recommended": ["c13"], "evaluations": 13},
    (2, 3, 6, 2, False): {
        "candidates": [("c1", "mutation", (), "failed", 1), ("c2", "mutation", (), "failed", 1), ("c3", "mutation", (), "failed", 1),
                       ("c4", "mutation", ("c1",), "failed", 1), ("c5", "mutation", ("c2",), "failed", 2), ("c6", "mutation", ("c3",), "failed", 1),
                       ("c7", "mutation", ("c5",), "failed", 1), ("c8", "mutation", ("c3",), "failed", 2), ("c9", "crossover", ("c8", "c1"), "passed", 3),
                       ("c10", "mutation", ("c5",), "passed", 3), ("c11", "mutation", ("c9",), "failed", 2)],
        "trace": [{"generation": 0, "population": ["c1", "c2", "c3"]}, {"generation": 1, "population": ["c1", "c5", "c3"]},
                  {"generation": 2, "population": ["c1", "c5", "c3"]}, {"generation": 3, "population": ["c1", "c5", "c8"]},
                  {"generation": 4, "population": ["c9", "c5", "c9"]}, {"generation": 5, "population": ["c9", "c10", "c9"]},
                  {"generation": 6, "population": ["c9", "c10", "c9"]}],
        "recommended": ["c9", "c10"], "evaluations": 11},
}

FIXTURE_MODEL = textwrap.dedent('''\
    import json, sys
    request = json.load(sys.stdin)
    assert request["protocol"] == "syberlabs.mutate/v0alpha1"
    text = request["files"]["src/pricing.py"]
    print(json.dumps({"changes": {"src/pricing.py": text.replace("return 0", "return weight * 5")}}))
    ''')


class LegacyProvider(EvolutionaryProvider):
    """``search`` as it was on main d0bebb8 (syberlabs/evolve.py:236-277), verbatim: the oracle FixedPolicy is held to."""

    def search(self, space):
        rng = random.Random(space.seed)
        people: list[tuple[str, Score, bool]] = []
        for seed in self.seeds[: self.population]:
            if self._room(space):
                people.append(self._judge(space, seed))
        attempts = 0
        while len(people) < self.population and self._room(space) and attempts < 4 * self.population:
            attempts += 1
            child = self._mutation(space, "base", rng, 0)
            if child is not None:
                people.append(child)
        self.trace.append({"generation": 0, "population": [p[0] for p in people]})
        for generation in range(1, self.generations + 1):
            if not self._room(space) or (self.stop_when_passing and any(p[2] for p in people)) or not people:
                break
            exhausted = getattr(self.mutate, "exhausted", False)
            if generation % self.crossover_every == 0 or exhausted:
                improved = False
                order = rng.sample(range(len(people)), len(people))
                for i, j in zip(order[::2], order[1::2]):
                    child = self._crossover(space, people[i][0], people[j][0], rng, generation)
                    if child and child[1] > people[i][1] and child[1] > people[j][1]:
                        people[i] = people[j] = child
                        improved = True
                if exhausted and not improved:
                    # No model calls left and recombination found nothing better: stop spending evaluations.
                    self.trace.append({"generation": generation, "population": [p[0] for p in people], "stopped": "exhausted"})
                    break
            else:
                for index, (parent, score, _passed) in enumerate(list(people)):
                    child = self._mutation(space, parent, rng, generation)
                    if child and child[1] > score:
                        people[index] = child
            if self.exchange is not None and generation % self.migrate_every == 0 and self._room(space):
                self._migration(space, people, generation)
            self.trace.append({"generation": generation, "population": [p[0] for p in people]})
        if self.exchange is not None and people:
            self._publish(space, people)
        ranked = sorted(dict.fromkeys(people), key=lambda p: p[1], reverse=True)
        passing = [p[0] for p in ranked if p[2]]
        return passing or [p[0] for p in ranked[:1]]


def outcome(thread, provider):
    """What the equality tests compare. Commit ids are left out: gitspace commits carry no fixed date."""
    search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
    return {"candidates": [(c.id, c.operator, c.parents, c.evaluation, sum(ch.state == "passed" for ch in c.checks))
                           for c in thread.candidates()],
            "trace": [e for e in provider.trace if "decision" not in e],
            "recommended": search["recommended"], "evaluations": search["evaluations"]}


class BranchPolicySeam(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def fixture(self, name):
        """A fresh repository, kit and thread of its own, so two searches never share a candidate."""
        root = self.folder / name
        make_repository(root)
        home = root / ".syberlabs"
        (home / "contracts").mkdir(parents=True)
        (home / "contracts" / "pricing-evolution.v1.json").write_text(json.dumps(contract()))
        kit = Kit.local(home, root, actor="dev@example.test")
        self.addCleanup(kit.close)
        return kit, kit.start("Fix the pricing rules", "pricing-evolution.v1")

    def side_by_side(self, name, seed, build, prepare=lambda thread: None):
        results = []
        for cls, label in ((LegacyProvider, "legacy"), (EvolutionaryProvider, "policy")):
            _kit, thread = self.fixture(f"{name}-{label}")
            prepare(thread)
            provider = build(cls)
            thread.propose(provider, seed=seed)
            results.append(outcome(thread, provider))
        return results

    def test_fixed_policy_reproduces_main(self):
        for case, want in EXPECTED.items():
            seed, options = case[0], dict(zip(OPTIONS, case[1:]))
            with self.subTest(seed=seed):
                _kit, thread = self.fixture(f"main-{seed}")
                provider = EvolutionaryProvider(RuleMutator(), **options, stop_on_first_pass=False)
                thread.propose(provider, seed=seed)
                self.assertEqual(outcome(thread, provider), want)
                self.assertEqual(provider.policy.revision, "1")
        # Revision 2 differs only when a seed or a fill child passes; no fill child does in the recordings.
        case = (3, 4, 50, 3, True)
        _kit, thread = self.fixture("main-3-rev2")
        provider = EvolutionaryProvider(RuleMutator(), **dict(zip(OPTIONS, case[1:])), stop_on_first_pass=True)
        thread.propose(provider, seed=case[0])
        self.assertEqual(outcome(thread, provider), EXPECTED[case])
        self.assertEqual(provider.policy.revision, "2")

    def test_fixed_policy_matches_the_legacy_loop_side_by_side(self):
        for case, want in EXPECTED.items():
            seed, options = case[0], dict(zip(OPTIONS, case[1:]))
            with self.subTest(seed=seed):
                legacy, policy = self.side_by_side(f"recorded-{seed}", seed,
                                                   lambda cls: cls(RuleMutator(), **options, stop_on_first_pass=False))
                self.assertEqual(legacy, policy)
                self.assertEqual(legacy, want, "the verbatim copy itself matches the recording from main")
        script = self.folder / "model.py"
        script.write_text(FIXTURE_MODEL)

        def model(max_calls):
            return CommandMutator([sys.executable, str(script)], name="fixture-model", max_calls=max_calls)

        def discount_and_tax(thread):
            thread.propose(changes={"src/pricing.py": fix(PRICING, "discount", "tax")})

        def shipping(thread):
            thread.propose(changes={"src/pricing.py": fix(PRICING, "shipping")})

        with self.subTest(case="exhausted"):  # three calls: the fill, one passing child, one no_change; then crossover finds nothing
            legacy, policy = self.side_by_side(
                "exhausted", 5, lambda cls: cls(model(3), population=2, generations=8, seeds=["c1"], stop_when_passing=False,
                                                stop_on_first_pass=False), discount_and_tax)
            self.assertEqual(legacy, policy)
            self.assertEqual(legacy["trace"][-1].get("stopped"), "exhausted", "the case drives the exhausted stop")
            self.assertEqual(len(legacy["trace"][-1]["population"]), 2)
        with self.subTest(case="exhausted-one-slot"):  # the one call duplicates the seed: a crossover round with no pair
            legacy, policy = self.side_by_side(
                "lone", 6, lambda cls: cls(model(1), population=2, generations=3, seeds=["c1"], stop_when_passing=False,
                                           stop_on_first_pass=False), shipping)
            self.assertEqual(legacy, policy)
            self.assertEqual([(e["generation"], e["population"], e.get("stopped")) for e in legacy["trace"]],
                             [(0, ["c1"], None), (1, ["c1"], "exhausted")])
        with self.subTest(case="empty-rounds"):  # a mutator that never produces: every round is a crossover over one slot
            legacy, policy = self.side_by_side(
                "idle", 1, lambda cls: cls(lambda space, parent, rng: None, population=2, generations=3, crossover_every=1,
                                           seeds=["c1"], stop_when_passing=False, stop_on_first_pass=False), shipping)
            self.assertEqual(legacy, policy)
            self.assertEqual([e["generation"] for e in legacy["trace"]], [0, 1, 2, 3])

    def test_a_passing_first_child_ends_the_search_under_revision_2(self):
        """Revision 2 stops the moment a seed or a child from base passes; revision 1 fills the population first."""
        class Solver:
            name = "solver"

            def __init__(self):
                self.calls = 0

            def __call__(self, space, parent, rng):
                self.calls += 1  # a distinct passing tree each call, so no child is refused as a duplicate
                return {"src/pricing.py": fix(PRICING, "discount", "tax", "shipping") + f"# {self.calls}\n"}

        def search(name, **options):
            _kit, thread = self.fixture(name)
            if options.pop("seeded", False):
                thread.propose(changes={"src/pricing.py": fix(PRICING, "discount", "tax", "shipping")})
                options["seeds"] = ["c1"]
            mutator = Solver()
            provider = EvolutionaryProvider(mutator, population=4, generations=6, crossover_every=3, **options)
            found = thread.propose(provider, seed=7)
            finished = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
            stop = provider.trace[-1]
            return mutator.calls, finished["evaluations"], finished["stopped"], finished["recommended"], stop, found

        calls, evaluations, stopped, recommended, stop, found = search("first-child-rev2", stop_on_first_pass=True)
        self.assertEqual((calls, evaluations, stopped, recommended), (1, 1, "completed", ["c1"]))
        self.assertEqual(stop, {"decision": 2, "generation": 0, "policy": {"name": "fixed", "revision": "2"},
                                "action": "stop", "reason": "passing"})
        self.assertEqual(found[0].signal["policy"], {"name": "fixed", "revision": "2", "decision": 1})

        calls, evaluations, stopped, recommended, stop, _found = search("first-child-rev1", stop_on_first_pass=False)
        self.assertEqual((calls, evaluations, stopped, recommended), (4, 4, "completed", ["c1", "c2", "c3", "c4"]))
        self.assertEqual((stop["generation"], stop["reason"], stop["policy"]["revision"]), (1, "passing", "1"))

        calls, evaluations, stopped, recommended, stop, found = search("seed-rev2", seeded=True, stop_on_first_pass=True)
        # The host records a recommendation only among the candidates this search submitted; a seed registers nothing.
        self.assertEqual((calls, evaluations, stopped, recommended, found), (0, 1, "completed", [], []))
        self.assertEqual((stop["decision"], stop["generation"], stop["reason"]), (2, 0, "passing"))

        calls, evaluations, stopped, recommended, stop, _found = search("seed-rev1", seeded=True, stop_on_first_pass=False)
        self.assertEqual((calls, evaluations, stopped), (3, 4, "completed"))
        self.assertEqual((stop["generation"], stop["reason"]), (1, "passing"))
        self.assertEqual(EvolutionaryProvider(Solver()).policy.revision, "1", "the default is the legacy rule")

    def test_every_decision_is_recorded_and_the_situation_counts_outcomes(self):
        class Watching(FixedPolicy):
            def __init__(self, **options):
                super().__init__(**options)
                self.seen = []

            def decide(self, situation):
                self.seen.append(situation)
                return super().decide(situation)

        watching = Watching(population=4, generations=10, crossover_every=3)
        _kit, thread = self.fixture("decisions")
        provider = EvolutionaryProvider(RuleMutator(), population=4, generations=10, crossover_every=3, policy=watching)
        found = thread.propose(provider, seed=7)
        decisions = [e for e in provider.trace if "decision" in e]
        self.assertEqual([e["decision"] for e in decisions], list(range(1, len(decisions) + 1)))
        self.assertTrue(all(e["policy"] == {"name": "fixed", "revision": "1"} and e["action"] in ACTION_KINDS for e in decisions))
        shapes = {"mutate": {"parent", "slot", "outcome"}, "crossover": {"pair", "slots", "outcome"}, "stop": {"reason"}}
        for entry in decisions:
            self.assertEqual(set(entry) - {"decision", "generation", "policy", "action"}, shapes[entry["action"]])
            if "outcome" in entry:
                self.assertEqual(set(entry["outcome"]), {"candidate", "score", "improved", "evaluated"})
        by_number = {e["decision"]: e for e in decisions}
        for candidate in found:
            policy = candidate.signal["policy"]
            self.assertEqual(set(policy), {"name", "revision", "decision"})
            entry = by_number[policy["decision"]]
            self.assertEqual((policy["name"], policy["revision"], entry["outcome"]["candidate"]), ("fixed", "1", candidate.id))
            self.assertEqual((entry["action"], entry["generation"]),
                             ({"mutation": "mutate", "crossover": "crossover"}[candidate.operator], candidate.signal["generation"]))
        stops = [e for e in decisions if e["action"] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertIs(provider.trace[-1], stops[0])
        self.assertEqual(stops[0]["reason"], "passing")
        self.assertEqual([e for e in provider.trace if "decision" not in e], EXPECTED[(7, 4, 10, 3, True)]["trace"])
        # The last situation the policy saw sums up the search; it never carries the space or the session.
        last = watching.seen[-1]
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual(set(last.history), {"mutate", "crossover", "migrate", "evaluate"})
        self.assertEqual(sum(s.attempted for s in last.history.values()), len(decisions) - 1)
        self.assertEqual(sum(s.produced for s in last.history.values()), len(found))
        self.assertEqual(sum(s.evaluated for s in last.history.values()), search["evaluations"])
        self.assertEqual(sum(s.passed for s in last.history.values()), sum(c.evaluation == "passed" for c in found))
        self.assertEqual((last.decisions, last.capacity, last.exchange, last.mutator_exhausted), (len(decisions) - 1, 4, False, False))
        self.assertEqual([p.id for p in last.population], EXPECTED[(7, 4, 10, 3, True)]["trace"][-1]["population"])
        self.assertTrue(all(s.rng is watching.seen[0].rng for s in watching.seen), "one seeded stream for the whole search")
        self.assertFalse(any(hasattr(last, name) for name in ("space", "session", "thread", "kit")))

    def test_the_signal_stays_small_with_maximal_fields(self):
        class Named:
            name = "m" * 64
            last_usage = {**{key: 2**31 for key in USAGE_FIELDS}, "model": "x" * 64, "stop_reason": "y" * 64, "simulated": True}

            def __call__(self, space, parent, rng):
                return {"src/pricing.py": fix(PRICING, rng.choice(("discount", "tax", "shipping")))}

        policy = FixedPolicy(population=2, generations=1, crossover_every=1)
        policy.revision = "r" * 128
        _kit, thread = self.fixture("signal")
        found = thread.propose(EvolutionaryProvider(Named(), population=2, generations=1, crossover_every=1, policy=policy), seed=1)
        self.assertEqual({c.operator for c in found}, {"mutation", "crossover"})
        for candidate in found:
            self.assertEqual(candidate.signal["policy"]["revision"], "r" * 128)
            self.assertLess(len(canonical(dict(candidate.signal)).encode()), 1024)
        self.assertTrue(all("model_usage" in c.signal and c.signal["mutator"] == "m" * 64
                            for c in found if c.operator == "mutation"))

    def priced_thread(self, name, doc):
        """Like ``fixture`` but under the given contract document."""
        root = self.folder / name
        make_repository(root)
        home = root / ".syberlabs"
        (home / "contracts").mkdir(parents=True)
        (home / "contracts" / "pricing-evolution.v1.json").write_text(json.dumps(doc))
        kit = Kit.local(home, root, actor="dev@example.test")
        self.addCleanup(kit.close)
        return kit.start("Fix the pricing rules", "pricing-evolution.v1")

    def test_a_priced_search_stops_after_the_first_priced_candidate(self):
        script = self.folder / "priced_model.py"
        script.write_text(textwrap.dedent('''            import json, sys
            request = json.load(sys.stdin)
            text = request["files"]["src/pricing.py"]
            print(json.dumps({"changes": {"src/pricing.py": text.replace("return 0", "return weight * 5")},
                              "usage": {"input_tokens": 2000, "output_tokens": 500, "model": "fixture-model"}}))
            '''))
        doc = contract()
        doc["evolution"]["budget"] = {"max_candidates": 8, "max_evaluations": 8, "max_seconds": 60, "max_dollars": 0.01,
                                      "prices": {"fixture-model": {"input_per_million": 3, "output_per_million": 15}}}
        cost = round(2000 * 3 / 1_000_000 + 500 * 15 / 1_000_000, 6)  # 0.0135 > max_dollars
        thread = self.priced_thread("priced", doc)
        mutator = CommandMutator([sys.executable, str(script)], name="fixture-model")
        provider = EvolutionaryProvider(mutator, population=3, generations=4)
        found = thread.propose(provider, seed=1)
        self.assertEqual([c.id for c in found], ["c1"], "the candidate that crossed the line is recorded; no more are bought")
        self.assertEqual(mutator.calls, 1)
        self.assertEqual(found[0].evaluation, "none", "no evaluation is bought over budget")
        registered = next(e["body"] for e in thread.history() if e["kind"] == "candidate_registered")
        self.assertEqual((registered["spend"]["dollars"], registered["spend"]["host_observed"]), (cost, True))
        self.assertEqual(registered["signal"]["model_usage"]["model"], "fixture-model")
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual(search["stopped"], "budget_exhausted")
        self.assertEqual((search["spend"]["dollars"], search["spend"]["evaluations"], search["spend"]["candidates"]), (cost, 0, 1))
        self.assertEqual(provider.trace[-1]["action"], "stop")
        self.assertEqual(provider.trace[-1]["reason"], "budget", "the policy saw no dollars left and stopped as it does for counts")

    def test_a_mutator_that_declares_usage_without_run_json_is_not_priced(self):
        class Declaring:
            name = "declaring"
            last_usage = {"input_tokens": 10**6, "output_tokens": 10**6, "model": "fixture-model"}

            def __call__(self, space, parent, rng):
                return {"src/pricing.py": fix(PRICING, rng.choice(("discount", "tax", "shipping")))}

        doc = contract()
        doc["evolution"]["budget"] = {"max_candidates": 8, "max_evaluations": 8, "max_seconds": 60, "max_dollars": 0.01,
                                      "prices": {"fixture-model": {"input_per_million": 3, "output_per_million": 15}}}
        thread = self.priced_thread("declaring", doc)
        found = thread.propose(EvolutionaryProvider(Declaring(), population=2, generations=1), seed=1)
        self.assertGreater(len(found), 1)
        self.assertTrue(all("model_usage" in c.signal for c in found if c.operator == "mutation"))
        for body in (e["body"] for e in thread.history() if e["kind"] == "candidate_registered"):
            self.assertEqual((body["spend"]["dollars"], body["spend"]["host_observed"]), (0.0, False))
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual((search["stopped"], search["spend"]["dollars"], search["spend"]["host_observed"]), ("completed", 0.0, False))

    def test_search_records_are_unchanged(self):
        _kit, thread = self.fixture("records")
        thread.propose(EvolutionaryProvider(RuleMutator(), population=2, generations=2), seed=1)
        history = thread.history()
        started = [e["body"] for e in history if e["kind"] == "search_started"][-1]
        finished = [e["body"] for e in history if e["kind"] == "search_finished"][-1]
        self.assertEqual(set(started), {"id", "provider", "operators", "budget", "base", "context_digest", "actor"})
        self.assertEqual(set(finished), {"id", "stopped", "candidates", "recommended", "evaluations", "elapsed_ms", "error", "actor"})
        self.assertEqual(validate_events(history, "evolution"), [])

    def test_the_provider_refuses_actions_outside_the_protocol(self):
        class Scripted:
            name, revision = "scripted", "1"

            def __init__(self, actions):
                self.actions = list(actions)

            def reset(self):
                pass

            def decide(self, situation):
                return self.actions.pop(0)

        self.assertIsInstance(Scripted([]), BranchPolicy)
        self.assertIsInstance(FixedPolicy(population=2, generations=1, crossover_every=1), BranchPolicy)
        cases = {
            "foreign": ["mutate"],
            "backwards": [Mutate("base", None, 1), Mutate("base", None, 0)],
            "far-ahead": [Mutate("base", None, 99)],
            "wrong-slot": [Mutate("base", None, 0), Mutate("nobody", 0, 1)],
        }
        for name, actions in cases.items():
            with self.subTest(case=name):
                _kit, thread = self.fixture(name)
                thread.propose(EvolutionaryProvider(RuleMutator(), policy=Scripted(actions)), seed=1)
                search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
                self.assertEqual((search["stopped"], search["error"]), ("error", "invalid_policy"))

        class Loud(Scripted):
            name = "Loud"

        with self.assertRaises(Rejected) as caught:
            EvolutionaryProvider(RuleMutator(), policy=Loud([]))
        self.assertEqual(caught.exception.code, "invalid_provider")
        with self.assertRaises(Rejected):
            EvolutionaryProvider(RuleMutator(), policy=object())


if __name__ == "__main__":
    unittest.main()
