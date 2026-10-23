"""The learning branch policies: EmpiricalPolicy as the control, JevPolicy choosing each action by expected admission per dollar.

Both sit behind the ``BranchPolicy`` seam and are measured against ``FixedPolicy`` on two
simulated operator profiles over the pricing fixture of ``examples/evolve.py``. The
comparison runs on an in-memory ``SearchSpace`` (``World``) that keeps the host's rules
the policies can feel (the ``no_change`` and ``duplicate`` refusals, evaluations counted
per judged tree, spend attributed to the next registration), so ten seeds of three policies
cost milliseconds rather than the seconds a Git worktree evaluation costs; the host-side
facts (host-observed dollars, the audit fields in a real search, the signal bound) are
checked on a real Build Thread. Nothing here is evidence about a model: the profiles are
chosen so a learning policy can be told from the fixed schedule, nothing more.
"""

from __future__ import annotations

import dataclasses
import json
import random
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from examples.evolve import PRICING, contract, make_repository
from syberlabs import Kit, Rejected
from syberlabs.build import SpendBudget
from syberlabs.canonical import canonical
from syberlabs.evolve import CommandMutator, EmpiricalPolicy, EvolutionaryProvider, FixedPolicy, JevPolicy
from syberlabs.search import (BranchPolicy, Budget, Candidate, CheckResult, Evaluation, MergeResult, OperatorStats,
                              SearchSpace)

PATH = "src/pricing.py"
RULES = ("discount", "tax", "shipping")
FIXED = {"discount": "return total * 0.9 if total > 100 else total", "tax": "return round(amount * 1.08, 2)",
         "shipping": "return weight * 5"}
ARGS = {"discount": "total", "tax": "amount", "shipping": "weight"}
RULE_LINE = {rule: re.compile(rf"(def {rule}\(\w+\):\n    )(return [^\n]*)") for rule in RULES}
AUDIT_FIELDS = ("expected_value", "expected_dollars", "chosen_by")


def body(text: str, rule: str) -> str:
    return RULE_LINE[rule].search(text).group(2)


def with_body(text: str, rule: str, line: str) -> str:
    return RULE_LINE[rule].sub(lambda m: m.group(1) + line, text, count=1)


def fixed_rules(text: str) -> set[str]:
    return {rule for rule in RULES if body(text, rule) == FIXED[rule]}


class Profile:
    """A stand-in model with a per-operator success profile.

    From ``base`` it always fixes one rule (a fresh file gets one rule right). From a child
    it fixes one missing rule with probability ``fix`` and otherwise writes a new wrong body
    for one missing rule; with ``rewrite_all`` it also rewrites every other missing rule
    with a wrong body, so two children conflict in every region a crossover would merge and
    recombination rarely helps. Each call costs ``price`` dollars, attributed by the world as
    the host attributes spend: to the next registered candidate.
    """

    name = "profile"

    def __init__(self, *, fix: float, rewrite_all: bool = False, price: float = 0.0):
        self.fix, self.rewrite_all, self.price, self.calls, self.tries = fix, rewrite_all, price, 0, 0

    def wrong(self, rule: str) -> str:
        self.tries += 1
        return f"return {ARGS[rule]} + {self.tries}"

    def __call__(self, space, parent: str, rng: random.Random):
        text = space.read(parent, PATH)
        self.calls += 1
        space.charge(self.price)
        missing = sorted(set(RULES) - fixed_rules(text))
        if not missing:
            return None
        rule = rng.choice(missing)
        if parent == "base" or rng.random() < self.fix:
            text = with_body(text, rule, FIXED[rule])
        else:
            text = with_body(text, rule, self.wrong(rule))
        if self.rewrite_all:
            for other in missing:
                if other != rule:
                    text = with_body(text, other, self.wrong(other))
        return {PATH: text}


class World:
    """The pricing fixture as an in-memory SearchSpace, with the host's rules a policy can feel."""

    objective, base, scope, context = "Fix the pricing rules", "base", ("src/",), ()

    def __init__(self, *, seed: int, max_candidates: int = 60, max_evaluations: int = 60, max_dollars: float | None = None):
        self.seed, self.max_dollars = seed, max_dollars
        self.budget = Budget(max_candidates, max_evaluations, 300)
        self.texts: dict[str, str] = {"base": PRICING}
        self.views: dict[str, Candidate] = {}
        self.parents: dict[str, tuple[str, ...]] = {}
        self.spend: dict[str, float] = {}
        self.evaluations, self.dollars, self.pending = 0, 0.0, 0.0
        self.first_pass: dict | None = None  # calls, dollars and evaluations when the first candidate passed
        self.signal_bytes: list[int] = []
        self.calls = 0

    # The usage meter, as the Build Thread's ledger attributes it.
    def charge(self, price: float) -> None:
        self.calls += 1
        self.pending = round(self.pending + price, 6)

    def files(self):
        return [PATH]

    def read(self, ref, path):
        return self.texts.get(ref) if path == PATH else None

    def candidate(self, cid):
        return self.views[cid]

    def _ancestors(self, cid: str) -> set[str]:
        found, todo = set(), [cid]
        while todo:
            x = todo.pop()
            if x not in found:
                found.add(x)
                todo.extend(self.parents.get(x, ()))
        return found

    def merge_base(self, a, b):
        above, below = self._ancestors(a), self._ancestors(b)
        if a in below:
            return a
        if b in above:
            return b
        common = (above & below) - {"base"}
        return max(common, key=lambda c: int(c[1:])) if common else "base"

    def merge_text(self, base, ours, theirs):
        text, conflicts = ours, 0
        for rule in RULES:
            b, o, t = body(base, rule), body(ours, rule), body(theirs, rule)
            if o == b:
                text = with_body(text, rule, t)
            elif t == b or o == t:
                continue
            else:
                conflicts += 1
                region = f"<<<<<<< ours\n    {o}\n=======\n    {t}\n>>>>>>> theirs"
                text = RULE_LINE[rule].sub(lambda m: m.group(1)[:-4] + region, text, count=1)  # markers at column 0
        return MergeResult(text, conflicts)

    def submit(self, changes, *, parents=(), operator, message="", signal=None):
        if len(self.views) >= self.budget.max_candidates:
            raise Rejected("budget_exhausted", "no candidates left")
        parent = parents[0] if parents else "base"
        text = changes.get(PATH)
        if text is None or text == self.texts[parent]:
            raise Rejected("no_change", "the edit leaves its parent's tree unchanged")
        twin = next((cid for cid, known in self.texts.items() if known == text), None)
        if twin is not None:
            raise Rejected("duplicate", f"the result is identical to {twin}")
        self.signal_bytes.append(len(canonical(dict(signal or {})).encode()))
        cid = f"c{len(self.views) + 1}"
        self.texts[cid], self.parents[cid] = text, tuple(parents)
        self.spend[cid], self.dollars, self.pending = self.pending, round(self.dollars + self.pending, 6), 0.0
        self.views[cid] = Candidate(id=cid, commit=cid, tree=cid, base="base", parents=tuple(parents), operator=operator,
                                    provider="test", provider_revision="1", changed_paths=(PATH,), signal=signal)
        return self.views[cid]

    def migrate(self, commit, *, origin):
        raise Rejected("migration_unavailable", "no exchange in this world")

    def evaluate(self, candidate):
        if self.evaluations >= self.budget.max_evaluations:
            raise Rejected("budget_exhausted", "no evaluations left")
        self.evaluations += 1
        fixed = fixed_rules(self.texts[candidate])
        found = Evaluation(candidate, tuple(CheckResult(r, "passed" if r in fixed else "failed", 0, 0) for r in RULES), RULES)
        if found.passed and self.first_pass is None:
            self.first_pass = {"calls": self.calls, "dollars": self.dollars, "evaluations": self.evaluations}
        return found

    def remaining(self):
        left = None if self.max_dollars is None else max(0.0, round(self.max_dollars - self.dollars, 6))
        return SpendBudget(self.budget.max_candidates - len(self.views), self.budget.max_evaluations - self.evaluations, 300, left)


PROFILES = {
    "mutation_rare": dict(fix=0.15, rewrite_all=False),  # a child's mutation rarely fixes another rule; crossover combines
    "mutation_common": dict(fix=0.85, rewrite_all=True),  # a child's mutation usually fixes one; children conflict everywhere
}
POLICIES = ("fixed", "empirical", "jev")
PRICE = 0.0135  # dollars per call: 2000 input at $3 and 500 output at $15 per million, as the Kit test below prices it
GENERATIONS, POPULATION, SEEDS = 60, 3, tuple(range(10))


def policy_for(name: str, *, prices=None) -> BranchPolicy | None:
    if name == "fixed":
        return None
    if name == "empirical":
        return EmpiricalPolicy(population=POPULATION, generations=GENERATIONS, crossover_every=3)
    return JevPolicy(generations=GENERATIONS, prices=prices)


def run(policy: str, profile: str, seed: int, *, max_dollars: float | None = 5.0, prices=None) -> dict:
    """One search of one policy on one profile. Returns calls, dollars and evaluations to the first pass, the trace and the world."""
    world = World(seed=seed, max_dollars=max_dollars)
    mutator = Profile(price=PRICE, **PROFILES[profile])
    provider = EvolutionaryProvider(mutator, population=POPULATION, generations=GENERATIONS, crossover_every=3,
                                    policy=policy_for(policy, prices=prices))
    recommended = provider.search(world)
    solved = world.first_pass is not None
    return {"policy": policy, "profile": profile, "seed": seed, "solved": solved,
            "calls": world.first_pass["calls"] if solved else mutator.calls,
            "dollars": world.first_pass["dollars"] if solved else world.dollars,
            "evaluations": world.first_pass["evaluations"] if solved else world.evaluations,
            "stop": provider.trace[-1]["reason"], "recommended": recommended, "trace": provider.trace, "world": world,
            "stats": dict(provider._stats)}


def compare(profile: str, seeds=SEEDS) -> dict[str, list[dict]]:
    return {policy: [run(policy, profile, seed) for seed in seeds] for policy in POLICIES}


def table(results: dict[str, dict[str, list[dict]]]) -> str:
    """The comparison as text: solved, calls and dollars to the first pass, summed and median over the seeds."""
    lines = [f"{'profile':16} {'policy':10} {'solved':>7} {'calls':>6} {'calls(med)':>10} {'dollars':>9} {'evals':>6}"]
    for profile, by_policy in results.items():
        for policy, rows in by_policy.items():
            calls = sorted(r["calls"] for r in rows)
            lines.append(f"{profile:16} {policy:10} {sum(r['solved'] for r in rows):>4}/{len(rows):<2} {sum(calls):>6} "
                         f"{calls[len(calls) // 2]:>10} {sum(r['dollars'] for r in rows):>9.4f} {sum(r['evaluations'] for r in rows):>6}")
    return "\n".join(lines)


class SimulatedProfiles(unittest.TestCase):
    """The three policies told apart on simulated operator profiles. Simulated: nothing here is about a model."""

    @classmethod
    def setUpClass(cls):
        cls.results = {profile: compare(profile) for profile in PROFILES}

    def totals(self, profile, key):
        return {policy: sum(r[key] for r in rows) for policy, rows in self.results[profile].items()}

    def test_every_policy_solves_every_seed_within_the_budget(self):
        for profile, by_policy in self.results.items():
            for policy, rows in by_policy.items():
                self.assertTrue(all(r["solved"] for r in rows), (profile, policy, [r["stop"] for r in rows]))
                self.assertTrue(all(r["stop"] == "passing" for r in rows), (profile, policy))

    def test_learning_policies_need_fewer_calls_than_fixed_where_crossover_pays(self):
        calls = self.totals("mutation_rare", "calls")
        self.assertLess(calls["empirical"], calls["fixed"], calls)
        self.assertLess(calls["jev"], calls["fixed"], calls)

    def test_jev_spends_fewer_dollars_than_empirical_when_crossover_is_cheaper(self):
        # A crossover makes no model call, so it costs no dollars; only its evaluation is priced, at the shadow price.
        for profile in PROFILES:
            dollars = self.totals(profile, "dollars")
            self.assertLess(dollars["jev"], dollars["empirical"], (profile, dollars))
        world = self.results["mutation_rare"]["jev"][0]["world"]
        self.assertTrue(all(world.spend[c.id] == 0.0 for c in world.views.values() if c.operator == "crossover"))

    def test_the_fixed_schedule_is_not_beaten_where_mutation_pays(self):
        # The reverse profile: mutation usually fixes a rule and crossover rarely combines. The learning policies
        # must not fall behind the fixed schedule by more than the exploration they spend on crossover.
        calls = self.totals("mutation_common", "calls")
        self.assertLessEqual(calls["jev"], calls["fixed"] + len(SEEDS), calls)
        self.assertLessEqual(calls["empirical"], calls["fixed"] + len(SEEDS), calls)

    def test_the_posterior_moves_and_each_decision_is_auditable(self):
        for row in self.results["mutation_rare"]["empirical"]:
            rounds = [e for e in row["trace"] if "posterior" in e]
            self.assertTrue(rounds, "a round decision carries the posterior")
            self.assertEqual({e["chosen_by"] for e in rounds}, {"thompson"})
            self.assertTrue(all(set(e["posterior"]) == {"mutate", "crossover"} and set(e["sampled"]) == set(e["posterior"])
                                for e in rounds))
            self.assertGreater(len({json.dumps(e["posterior"], sort_keys=True) for e in rounds}), 1, "the posterior moved")
        for row in self.results["mutation_rare"]["jev"]:
            decisions = [e for e in row["trace"] if "decision" in e]
            for entry in decisions:
                self.assertTrue(all(field in entry for field in AUDIT_FIELDS), entry)
                self.assertEqual(entry["policy"], {"name": "jev", "revision": "1"})
            values = {(e["action"], e["expected_value"]) for e in decisions if e["action"] != "stop"}
            self.assertGreater(len({v for _, v in values}), 1, "the expected value moved")
            chosen = [e for e in decisions if e["action"] != "stop"]
            self.assertTrue(all(e["chosen_by"] in ("value_per_dollar", "exploration", "only_option") for e in chosen))
            self.assertTrue(all(e["expected_dollars"] is not None and e["budget"]["unit"] == "dollars" for e in chosen))
            self.assertTrue(all({a["action"] for a in e["alternatives"]} <= set(JevPolicy.KINDS) for e in chosen))
            self.assertEqual(decisions[-1]["chosen_by"], "rule:passing")

    def test_the_signal_stays_within_the_bound_and_names_the_policy(self):
        for profile, by_policy in self.results.items():
            for policy, rows in by_policy.items():
                for row in rows:
                    self.assertTrue(all(n < 4096 for n in row["world"].signal_bytes), (profile, policy))
                    self.assertTrue(all(c.signal["policy"]["name"] == policy for c in row["world"].views.values()))

    def test_fixed_and_empirical_take_the_same_decisions_until_the_first_round(self):
        fixed, empirical = self.results["mutation_rare"]["fixed"][0]["trace"], self.results["mutation_rare"]["empirical"][0]["trace"]
        first_round = next(i for i, e in enumerate(empirical) if "posterior" in e)
        strip = lambda e: {k: v for k, v in e.items() if k != "policy"}  # noqa: E731
        self.assertEqual([strip(e) for e in fixed[:first_round]], [strip(e) for e in empirical[:first_round]])


class Policies(unittest.TestCase):
    """Construction, the one-draw rule, the stats, and the stops, without a search host."""

    def test_both_are_branch_policies_with_the_spec_names(self):
        empirical = EmpiricalPolicy(population=2, generations=1, crossover_every=1)
        jev = JevPolicy(generations=1)
        self.assertIsInstance(empirical, BranchPolicy)
        self.assertIsInstance(jev, BranchPolicy)
        self.assertEqual((empirical.name, empirical.revision, jev.name, jev.revision), ("empirical", "1", "jev", "1"))
        self.assertEqual(EmpiricalPolicy(population=2, generations=1, crossover_every=1, stop_on_first_pass=True).revision, "2")
        for bad in (dict(generations=0), dict(generations=1, prior=(0, 1)), dict(generations=1, prices={"patch": 1}),
                    dict(generations=1, prices={"mutate": -1}), dict(generations=1, explore=-1)):
            with self.assertRaises(Rejected):
                JevPolicy(**bad)
        with self.assertRaises(Rejected):
            EmpiricalPolicy(population=2, generations=1, crossover_every=1, prior=(1, 0))

    def test_neither_learning_policy_draws_from_the_provider_stream_beyond_the_pairing(self):
        class Counting(random.Random):
            """Counts draws. A subclass that overrides random() makes sample() draw through it, so those are not double-counted."""

            draws, sampling = 0, False

            def random(self):
                self.draws += not self.sampling
                return super().random()

            def sample(self, population, k, **kw):
                self.draws, self.sampling = self.draws + 1, True
                try:
                    return super().sample(population, k, **kw)
                finally:
                    self.sampling = False

        for policy in ("empirical", "jev"):
            world = World(seed=1, max_dollars=5.0)
            provider = EvolutionaryProvider(Profile(fix=0.15, price=PRICE), population=POPULATION, generations=GENERATIONS,
                                            crossover_every=3, policy=policy_for(policy))
            stream, inside, original = Counting(1), [], provider.policy.decide

            def decide(s, _stream=stream, _inside=inside, _original=original):
                s = dataclasses.replace(s, rng=_stream)
                before = _stream.draws
                action = _original(s)
                _inside.append((action.kind, _stream.draws - before))
                return action

            provider.policy.decide = decide
            provider.search(world)
            crossover_rounds = sum(1 for e in provider.trace if "posterior" in e and e["action"] == "crossover")
            draws = sum(n for _, n in inside)
            self.assertEqual(draws, crossover_rounds if policy == "empirical" else 0, (policy, inside))
            self.assertTrue(world.first_pass, policy)

    def test_operator_stats_carry_dollars_on_a_priced_host_only(self):
        priced = run("fixed", "mutation_rare", 0, max_dollars=5.0)
        unpriced = run("fixed", "mutation_rare", 0, max_dollars=None)
        self.assertEqual(priced["stats"]["crossover"].dollars, 0.0)
        self.assertGreater(priced["stats"]["mutate"].dollars, 0.0)
        self.assertAlmostEqual(priced["stats"]["mutate"].dollars + priced["stats"]["crossover"].dollars, priced["world"].dollars, places=6)
        self.assertTrue(all(s.dollars is None for s in unpriced["stats"].values()))
        self.assertEqual(OperatorStats().dollars, None)
        self.assertTrue(all("dollars" in e["outcome"] for e in priced["trace"] if "outcome" in e))
        self.assertTrue(all("dollars" not in e["outcome"] for e in unpriced["trace"] if "outcome" in e), "an unpriced trace is as it was")
        # The same seed, priced or not, takes the same decisions: the fixed schedule does not read dollars.
        self.assertEqual([c for c in priced["trace"] if "decision" not in c], [c for c in unpriced["trace"] if "decision" not in c])

    def test_jev_prices_a_crossover_below_a_mutation_and_stops_on_the_floor(self):
        row = run("jev", "mutation_rare", 3, prices={"mutate": PRICE})
        chosen = [e for e in row["trace"] if "decision" in e and e["action"] in ("mutate", "crossover")]
        both = next(e for e in chosen if {a["action"] for a in e["alternatives"]} >= {"mutate", "crossover"})
        prices = {a["action"]: a["expected_dollars"] for a in both["alternatives"]}
        self.assertEqual(prices["crossover"], 0.0)
        self.assertGreater(prices["mutate"], 0.0)
        self.assertEqual(chosen[0]["expected_dollars"], PRICE, "the prior price counts as one attempt before any is observed")
        # Jev stops for budget when the remaining dollars cannot be expected to buy one pass, and says so.
        world = World(seed=3, max_dollars=0.02)
        provider = EvolutionaryProvider(Profile(fix=0.0, price=PRICE), population=POPULATION, generations=GENERATIONS,
                                        policy=JevPolicy(generations=GENERATIONS, prices={"mutate": PRICE}, floor=1.0))
        provider.search(world)
        stop = provider.trace[-1]
        self.assertEqual((stop["action"], stop["reason"], stop["chosen_by"]), ("stop", "budget", "rule:budget"))
        self.assertTrue(stop.get("expected_passes", 0.0) < 1.0 or stop["expected_value"] is None)
        self.assertLess(world.dollars, 0.05)

    def test_jev_without_a_dollar_bound_prices_in_evaluations(self):
        row = run("jev", "mutation_rare", 2, max_dollars=None)
        chosen = [e for e in row["trace"] if "decision" in e and e["action"] != "stop"]
        self.assertTrue(chosen and all(e["expected_dollars"] is None and e["budget"]["unit"] == "evaluations" for e in chosen))
        self.assertTrue(row["solved"])

    def test_jev_evaluates_seeds_first_and_stops_on_a_passing_seed(self):
        world = World(seed=1, max_dollars=5.0)
        world.texts["c1"] = PRICING
        for rule in RULES:
            world.texts["c1"] = with_body(world.texts["c1"], rule, FIXED[rule])
        world.parents["c1"] = ()
        world.views["c1"] = Candidate(id="c1", commit="c1", tree="c1", base="base", parents=(), operator="patch",
                                      provider="person", provider_revision="1", changed_paths=(PATH,))
        provider = EvolutionaryProvider(Profile(fix=0.15, price=PRICE), population=POPULATION, generations=GENERATIONS,
                                        policy=JevPolicy(generations=GENERATIONS, seeds=["c1"]))
        self.assertEqual(provider.search(world), ["c1"])
        actions = [(e["action"], e.get("candidate"), e.get("reason")) for e in provider.trace if "decision" in e]
        self.assertEqual(actions, [("evaluate", "c1", None), ("stop", None, "passing")])
        self.assertEqual(world.calls, 0)

    def test_an_audit_that_shadows_a_provider_field_is_refused(self):
        class Shadowing(JevPolicy):
            def audit(self):
                return {"action": "mutate"}

        world = World(seed=1, max_dollars=5.0)
        provider = EvolutionaryProvider(Profile(fix=0.15), population=POPULATION, generations=GENERATIONS,
                                        policy=Shadowing(generations=GENERATIONS))
        with self.assertRaises(Rejected) as caught:
            provider.search(world)
        self.assertEqual(caught.exception.code, "invalid_policy")


class OnTheHost(unittest.TestCase):
    """The host-side facts: host-observed dollars reach the policy, the audit lands in a real search, the signal bound holds."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.script = self.folder / "profile_model.py"
        # The ``mutation_rare`` profile as a model adapter: a fix from base, a fix from a child one time in seven,
        # a new wrong body otherwise; it reports usage so the host prices every call.
        self.script.write_text(textwrap.dedent(f'''\
            import json, random, re, sys
            request = json.load(sys.stdin)
            text = request["files"]["src/pricing.py"]
            rng = random.Random(request["seed"])
            fixed = {json.dumps(FIXED)}
            args = {json.dumps(ARGS)}
            lines = {{rule: re.compile(r"(def " + rule + r"\\(\\w+\\):\\n    )(return [^\\n]*)") for rule in fixed}}
            missing = sorted(rule for rule in fixed if lines[rule].search(text).group(2) != fixed[rule])
            if missing:
                rule = rng.choice(missing)
                body = fixed[rule] if request["parent"] == "base" or rng.random() < 1 / 7 else f"return {{args[rule]}} + {{request['seed']}}"
                text = lines[rule].sub(lambda m: m.group(1) + body, text, count=1)
            print(json.dumps({{"changes": {{"src/pricing.py": text}},
                              "usage": {{"input_tokens": 2000, "output_tokens": 500, "model": "fixture-model"}}}}))
            '''))

    def thread(self, name):
        root = self.folder / name
        make_repository(root)
        home = root / ".syberlabs"
        (home / "contracts").mkdir(parents=True)
        doc = contract()
        doc["evolution"]["budget"] = {"max_candidates": 40, "max_evaluations": 40, "max_seconds": 300, "max_dollars": 5.0,
                                      "prices": {"fixture-model": {"input_per_million": 3, "output_per_million": 15}}}
        (home / "contracts" / "pricing-evolution.v1.json").write_text(json.dumps(doc))
        kit = Kit.local(home, root, actor="dev@example.test")
        self.addCleanup(kit.close)
        return kit.start("Fix the pricing rules", "pricing-evolution.v1")

    def search(self, policy, seed):
        thread = self.thread(f"{policy}-{seed}")
        mutator = CommandMutator([sys.executable, str(self.script)], name="fixture-model")
        provider = EvolutionaryProvider(mutator, population=POPULATION, generations=GENERATIONS, crossover_every=3,
                                        policy=policy_for(policy))
        found = thread.propose(provider, seed=seed)
        history = thread.history()
        spend = {e["body"]["id"]: e["body"]["spend"] for e in history if e["kind"] == "candidate_registered"}
        finished = [e["body"] for e in history if e["kind"] == "search_finished"][-1]
        return {"found": found, "trace": provider.trace, "spend": spend, "finished": finished, "calls": mutator.calls,
                "stats": dict(provider._stats)}

    def test_host_observed_dollars_reach_the_policy_and_jev_spends_less_than_empirical(self):
        rows = {policy: [self.search(policy, seed) for seed in (1, 2)] for policy in ("empirical", "jev")}
        for policy, runs in rows.items():
            for row in runs:
                self.assertEqual(row["finished"]["stopped"], "completed")
                self.assertTrue(any(c.evaluation == "passed" for c in row["found"]), (policy, row["finished"]))
                # A mutation's usage crossed run_json, so the host observed it; a crossover registers with nothing pending.
                for candidate in row["found"]:
                    body = row["spend"][candidate.id]
                    if candidate.operator == "crossover":
                        self.assertEqual((body["dollars"], body["host_observed"]), (0.0, False))
                    else:
                        self.assertTrue(body["host_observed"] and body["dollars"] > 0.0, (policy, candidate.id, body))
                # The provider's per-kind dollars are the host's own attributions, summed by the operator that registered.
                by_kind = {"mutate": 0.0, "crossover": 0.0}
                for candidate in row["found"]:
                    by_kind[{"mutation": "mutate", "crossover": "crossover"}[candidate.operator]] += row["spend"][candidate.id]["dollars"]
                for kind, total in by_kind.items():
                    self.assertAlmostEqual(row["stats"][kind].dollars, total, places=6, msg=(policy, kind))
                self.assertAlmostEqual(sum(by_kind.values()), row["finished"]["spend"]["dollars"], places=6)
                for candidate in row["found"]:
                    self.assertLess(len(canonical(dict(candidate.signal)).encode()), 4096)
                    self.assertEqual(candidate.signal["policy"]["name"], policy)
        dollars = {policy: sum(r["finished"]["spend"]["dollars"] for r in runs) for policy, runs in rows.items()}
        self.assertLess(dollars["jev"], dollars["empirical"], dollars)
        for entry in (e for e in rows["jev"][0]["trace"] if "decision" in e):
            self.assertTrue(all(field in entry for field in AUDIT_FIELDS), entry)
        # From the second mutation on, its price is learned from the host's attribution of the first.
        later = [e for row in rows["jev"] for e in row["trace"] if "decision" in e and e["action"] == "mutate" and e["decision"] > 1]
        self.assertTrue(later and all(e["expected_dollars"] > 0.0 for e in later), later)

    def test_fixed_policy_decisions_are_unchanged_by_the_measurement(self):
        # The fixed schedule neither reads dollars nor audits: its decision entries gain only the outcome's dollars.
        row = self.search("fixed", 1)
        decisions = [e for e in row["trace"] if "decision" in e]
        self.assertTrue(all(set(e) <= {"decision", "generation", "policy", "action", "parent", "slot", "pair", "slots",
                                        "candidate", "reason", "outcome"} for e in decisions))
        self.assertTrue(all(set(e["outcome"]) == {"candidate", "score", "improved", "evaluated", "dollars"}
                            for e in decisions if "outcome" in e))
        self.assertEqual(row["found"][0].signal["policy"], {"name": "fixed", "revision": "1", "decision": 1})


if __name__ == "__main__":
    results = {profile: compare(profile) for profile in PROFILES}
    print(table(results))
