"""An EvoGit-style evolutionary search provider.

This is an independent implementation of the method described in "EvoGit:
Decentralized Code Evolution via Git-Based Multi-Agent Collaboration"
(Huang et al., 2025). It contains no EvoGit code; EvoGit itself is AGPL-3.0.

The published ideas it follows:

- a population of individuals that are Git commits, with ancestry as lineage;
- mutation: an operator (in EvoGit, a language model) rewrites part of an
  individual, producing a child commit;
- crossover: a three-way merge of two individuals that are not ancestors of
  each other, with conflicting regions resolved by a seeded coin;
- pairwise selection: a mutated child replaces its parent only if it is
  better, and a merge child replaces both parents only if it is better than both.

What differs, on purpose, in SyberLabs:

- Fitness is the host's own evaluation of each child's exact tree, not a
  score the provider computes or a model's opinion of a diff. A child is
  evaluated even when both of its parents passed: lineage is provenance,
  not evidence of superiority.
- The base tree is evaluated once, before the population is filled, so the
  first mutations have the host's results to go on and not only a text. It
  costs one evaluation and no model call; the base is not an individual.
- Every individual is a provisional candidate on a non-authoritative ref.
  Selection here only decides what the search keeps exploring and what it
  recommends. Promotion is a person's decision through admission.
- The contract fixes the scope, the operators (``mutation`` and ``crossover``
  must both be listed), and the budget. The host enforces them.
- Human feedback is the objective, seed candidates a person picks (EvoGit's
  "human tags"), and the promotion gate.

The mutation operator is pluggable. ``CommandMutator`` is the model seam: any
model adapter that reads JSON on stdin and prints changed files.

Where the search branches is a seam too. Before every step the provider asks
a ``BranchPolicy`` (``syberlabs.search``) for the next action from a
``Situation``; ``FixedPolicy`` is the branching described above and the
default. The provider executes every action itself, keeps the selection rule,
records each decision in its trace and in the child's ``signal``, and refuses
anything outside the closed action set. Two learning policies sit behind the
same seam: ``EmpiricalPolicy``, the fixed schedule with the round kind chosen
by Thompson sampling over this search's own outcomes, and ``JevPolicy``, a
bounded choice among priced actions, one action per decision, that spends the
next unit of budget where the expected admission per unit is highest. A policy
with an ``audit()`` method has its account of each decision merged into that
decision's trace entry, so a reader can tell from the entry alone why it was
taken. Neither learning policy draws from the provider's seeded stream beyond
the one crossover-pairing draw the fixed schedule makes.
"""

from __future__ import annotations

import dataclasses
import random
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from syberlabs.economic import is_money
from syberlabs.errors import Rejected
from syberlabs.providers import model_usage, refuse, run_json
from syberlabs.search import (ACTION_KINDS, Action, BranchPolicy, Crossover, Evaluate, Individual, Migrate, Mutate,
                              OperatorStats, Score, SearchSpace, Situation, Stop)

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

# Stop reasons that end the search at a generation boundary: the last generation that ran is closed with a
# plain trace entry. Any other reason ends the search inside the round the stop labels, which is closed with
# a ``"stopped"`` entry, as the exhausted-mutator stop always was.
CLEAN_STOPS = ("budget", "passing", "empty", "generations")


class Mutator(Protocol):
    name: str

    def __call__(self, space: SearchSpace, parent: str, rng: random.Random) -> Mapping[str, str | None] | None:
        """File edits that make a child of ``parent`` (a candidate id or ``"base"``), or None."""


class CommandMutator:
    """Mutation by an external program, typically a model adapter.

    Request (stdin): ``{protocol, objective, scope, parent, files: {path: text}, context, seed}``,
    plus ``feedback`` when the host has evaluated the parent: a candidate it judged, or ``base``
    once the search evaluated the base tree. ``scope`` is the
    evolution scope's paths, the same list the search protocol's request carries
    (``providers.CommandProvider``), so an adapter that tells its model where it may write is
    told the same thing under both protocols. ``feedback`` is ``mutation_feedback(space,
    parent)``: the host's recorded check results for the parent, in the shape the repair arm
    sends under the search protocol, so a mutation has a direction and not only a parent.
    Response (stdout): ``{"changes": {path: text or null}}``.
    """

    PROTOCOL = "syberlabs.mutate/v0alpha1"

    def __init__(self, argv: Sequence[str], *, name: str = "command", timeout: int = 300,
                 cwd: str | Path | None = None, max_bytes: int = 65_536, max_calls: int | None = None):
        self.argv, self.name, self.timeout, self.cwd, self.max_bytes = list(argv), name, timeout, cwd, max_bytes
        self.max_calls, self.calls, self.usage, self.last_usage = max_calls, 0, [], None
        # Why the last call produced no edit: ``unparsable`` (no ``changes`` object), ``no_change`` (an empty one), or
        # None (the call was not made, or it produced an edit). The provider records it against the search.
        self.last_reason: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.max_calls is not None and self.calls >= self.max_calls

    def __call__(self, space: SearchSpace, parent: str, rng: random.Random) -> Mapping[str, str | None] | None:
        self.last_usage = self.last_reason = None
        if self.exhausted:
            return None
        paths = list(dict.fromkeys([*(item["path"] for item in space.context),
                                    *(space.candidate(parent).changed_paths if parent != "base" else ())]))
        files, spent = {}, 0
        for path in paths:
            text = space.read(parent, path)
            if text is None or spent + len(text) > self.max_bytes:
                continue
            files[path] = text
            spent += len(text)
        self.calls += 1
        request = {"protocol": self.PROTOCOL, "objective": space.objective, "scope": list(space.scope),
                   "parent": parent, "files": files, "context": [dict(item) for item in space.context],
                   "seed": rng.randrange(2**31)}
        feedback = mutation_feedback(space, parent)
        if feedback is not None:
            request["feedback"] = feedback
        response = run_json(self.argv, request, timeout=self.timeout, cwd=self.cwd)
        self.last_usage = model_usage(response)
        if self.last_usage:
            self.usage.append(self.last_usage)
        changes = response.get("changes") if isinstance(response, dict) else None
        if not isinstance(changes, dict):
            self.last_reason = "unparsable"
            return None
        found = {k: v for k, v in changes.items() if isinstance(k, str) and (v is None or isinstance(v, str))}
        if not found:
            self.last_reason = "no_change"
        return found


def mutation_feedback(space: SearchSpace, parent: str) -> dict | None:
    """The host's evaluation of ``parent`` as a mutate request's ``feedback``, or None.

    The same object the repair arm puts on a search request after a failed attempt
    (``benchmarks/model_vs_patch.py``, ``Patches.search``): ``{"candidate": parent,
    "checks": [{name, state, exit_code, duration_ms, output_tail}, ...]}``, the check results
    exactly as the host recorded them in ``candidate_evaluated`` (each ``output_tail`` already
    within the record's bound). ``files`` is left out because the mutate request's own
    ``files`` already carries the parent's texts. For ``"base"`` the checks are the space's
    ``base_evaluation``, the host's evaluation of the base tree that ``EvolutionaryProvider``
    asks for before the fill; None on a space that has none (the budget had no room for it, or
    the space does not offer one). None when the host has not evaluated a candidate parent, so
    a reader can tell "no evaluation" from "an evaluation that passed". Pure: it reads the space
    and runs nothing.
    """
    if parent == "base":
        found = getattr(space, "base_evaluation", None)
        checks = found.checks if found is not None else ()
    else:
        checks = space.candidate(parent).checks
    if not checks:
        return None
    return {"candidate": parent, "checks": [dataclasses.asdict(check) for check in checks]}


def resolve_conflicts(text: str, rng: random.Random, accept_ours: float) -> str:
    """Pick one side of each ``git merge-file`` conflict region with a seeded coin."""
    out, ours, theirs, state = [], [], [], None
    for line in text.splitlines(keepends=True):
        if state is None and line.startswith("<<<<<<< "):
            state, ours, theirs = "ours", [], []
        elif state == "ours" and line.startswith("=======") and line.rstrip("\r\n") == "=======":
            state = "theirs"
        elif state == "theirs" and line.startswith(">>>>>>> "):
            out.extend(ours if rng.random() < accept_ours else theirs)
            state = None
        elif state == "ours":
            ours.append(line)
        elif state == "theirs":
            theirs.append(line)
        else:
            out.append(line)
    if state is not None:  # unterminated region: keep the text as it was
        return text
    return "".join(out)


class FixedPolicy:
    """Today's branching as a policy: the loop ``search()`` ran before the seam, replayed decision for decision.

    Seeds are evaluated while there is room; the population is filled from ``base``
    with at most four attempts per slot; then every generation is a mutation round
    over each slot in order, or a crossover round over a random pairing every
    ``crossover_every`` generations and whenever the mutator is exhausted, followed
    by a migration on its cadence. The search stops at the top of a generation when
    the generations are used up, there is no room, an individual passes (with
    ``stop_when_passing``) or the population is empty, and inside a crossover round
    when the mutator is exhausted and recombination improved nothing.

    Revision ``"2"`` (``stop_on_first_pass=True``) adds one rule before the fill is
    complete: the host already judges every seed and every child from ``base`` the
    moment it is made, so with ``stop_when_passing`` the first one that passes ends
    the search at generation 0 instead of after the population is full. Revision
    ``"1"`` (``stop_on_first_pass=False``, the default) is the loop as it was, in
    which the passing check runs only at the top of a generation.
    """

    name, revision = "fixed", "1"

    def __init__(self, *, population: int, generations: int, crossover_every: int, seeds: Sequence[str] = (),
                 stop_when_passing: bool = True, migrate_every: int = 0, stop_on_first_pass: bool = False):
        self.population, self.generations, self.crossover_every = population, generations, crossover_every
        self.seeds, self.stop_when_passing, self.migrate_every = tuple(seeds)[:population], stop_when_passing, migrate_every
        self.stop_on_first_pass = stop_on_first_pass
        self.revision = "2" if stop_on_first_pass else "1"
        self.reset()

    def reset(self) -> None:
        self._phase = "seeds"  # seeds -> fill -> top -> (a round) -> end_of_round -> top ...
        self._seed_index = 0
        self._fill_attempts = 0
        self._generation = 1
        self._pending: list[Action] = []  # the rest of the current round, in order
        self._round: tuple[str, bool, int] | None = None  # (kind, exhausted, improvements before the round)

    def _round_kind(self, situation: Situation, exhausted: bool) -> str:
        """``"crossover"`` or ``"mutation"`` for the round about to start. The hook a learning policy overrides."""
        return "crossover" if self._generation % self.crossover_every == 0 or exhausted else "mutation"

    def decide(self, s: Situation) -> Action:
        while True:
            if self._pending:
                return self._pending.pop(0)
            if (self._phase in ("seeds", "fill") and self.stop_on_first_pass and self.stop_when_passing
                    and any(p.passed for p in s.population)):
                return Stop("passing", 0)  # revision 2: a passing seed or first child ends the search before the fill
            if self._phase == "seeds":
                while self._seed_index < len(self.seeds):
                    seed = self.seeds[self._seed_index]
                    self._seed_index += 1
                    if s.room:
                        return Evaluate(seed, 0)
                self._phase = "fill"
            if self._phase == "fill":
                if len(s.population) < self.population and s.room and self._fill_attempts < 4 * self.population:
                    self._fill_attempts += 1
                    return Mutate("base", None, 0)
                self._phase = "top"
            if self._phase == "end_of_round":
                kind, exhausted, improved_before = self._round
                self._round, self._phase = None, "top"
                if kind == "crossover" and exhausted and s.history["crossover"].improved == improved_before:
                    return Stop("exhausted", self._generation)  # inside the round: the provider writes the "stopped" entry
                g, self._generation = self._generation, self._generation + 1
                if self.migrate_every and g % self.migrate_every == 0 and s.room:
                    return Migrate(g)
            g = self._generation
            if g > self.generations:
                return Stop("generations", g)
            if not s.room:
                return Stop("budget", g)
            if self.stop_when_passing and any(p.passed for p in s.population):
                return Stop("passing", g)
            if not s.population:
                return Stop("empty", g)
            exhausted = s.mutator_exhausted
            if self._round_kind(s, exhausted) == "crossover":
                n = len(s.population)
                order = s.rng.sample(range(n), n)  # the one draw a policy makes from the provider's stream
                self._pending = [Crossover(s.population[i].id, s.population[j].id, (i, j), g)
                                 for i, j in zip(order[::2], order[1::2])]
                self._round = ("crossover", exhausted, s.history["crossover"].improved)
            else:
                self._pending = [Mutate(p.id, slot, g) for slot, p in enumerate(s.population)]
                self._round = ("mutation", exhausted, s.history["mutate"].improved)
            self._phase = "end_of_round"
            # A crossover round over one slot has no pairs: the round ends at once and the loop closes it.


class EmpiricalPolicy(FixedPolicy):
    """The fixed schedule with the round kind learned: the control for a learning policy.

    Everything is ``FixedPolicy`` (seeds, the fill from ``base``, rounds over every
    slot, the migration cadence, the stops, both revisions) except ``_round_kind``,
    which chooses a mutation round or a crossover round by Thompson sampling: one
    Beta posterior per operator over ``improved / attempted`` in the rounds of this
    search (the fill is not a choice and is left out), a sample from each, the
    larger wins. The sampler is a private ``random.Random`` seeded from a constant,
    ``salt`` and ``situation.decisions``, so the one-draw rule holds: the only draw
    from the provider's stream is the crossover pairing the fixed schedule makes.
    An exhausted mutator still forces a crossover round and a population of one
    slot a mutation round, as the fixed schedule has them. ``audit()`` reports the
    posterior and the samples behind the first action of each round. Revision
    ``"1"`` and ``"2"`` mean what they mean for ``FixedPolicy``.
    """

    name = "empirical"
    KINDS = ("mutate", "crossover")

    def __init__(self, *, prior: tuple[float, float] = (1.0, 1.0), salt: int = 0, **options):
        if len(prior) != 2 or any(not is_money(x) or x <= 0 for x in prior) or type(salt) is not int:
            raise Rejected("invalid_provider", "empirical: prior must be two positive numbers and salt an integer")
        self.prior, self.salt = (float(prior[0]), float(prior[1])), salt
        super().__init__(**options)

    def reset(self) -> None:
        super().reset()
        self._baseline: dict[str, OperatorStats] | None = None
        self._last_audit: dict | None = None

    def posterior(self, s: Situation) -> dict[str, tuple[float, float]]:
        """``(alpha, beta)`` per operator: the prior plus improvements and failures in this search's rounds."""
        if self._baseline is None:
            self._baseline = {kind: s.history[kind] for kind in self.KINDS}
        found = {}
        for kind in self.KINDS:
            now, then = s.history[kind], self._baseline[kind]
            improved, attempted = now.improved - then.improved, now.attempted - then.attempted
            found[kind] = (self.prior[0] + improved, self.prior[1] + attempted - improved)
        return found

    def _round_kind(self, s: Situation, exhausted: bool) -> str:
        posterior = self.posterior(s)
        if exhausted:
            return "crossover"
        if len(s.population) < 2:
            return "mutation"
        rng = random.Random(f"syberlabs.empirical/{self.salt}/{s.decisions}")
        sampled = {kind: rng.betavariate(*posterior[kind]) for kind in self.KINDS}
        kind = max(self.KINDS, key=sampled.__getitem__)
        self._last_audit = {"chosen_by": "thompson", "posterior": {k: [a, b] for k, (a, b) in posterior.items()},
                            "sampled": {k: round(v, 6) for k, v in sampled.items()}}
        return "crossover" if kind == "crossover" else "mutation"

    def audit(self) -> dict | None:
        found, self._last_audit = self._last_audit, None
        return found


class JevPolicy:
    """Bounded choice among priced actions: each decision buys the action with the highest expected admission per unit of budget.

    It decides per action, not per round. At each decision the available actions
    are: evaluate the next seed; cross the two best distinct individuals not yet
    crossed; mutate ``base`` while the population is below capacity, otherwise the
    best individual (ties: the slot mutated least); migrate once when an exchange
    is configured. Each is scored from ``Situation`` alone:

    - value: the posterior probability that the action yields a child that passes,
      ``(passed + a) / (attempted + a + b)`` with the Beta prior ``(a, b)`` over the
      ``OperatorStats`` of its kind (a seed evaluation counts under ``evaluate``);
    - price: its expected cost in the unit the host bounds. Priced (the host reports
      ``max_dollars``): the mean host-observed dollars of past actions of that kind,
      with ``prices[kind]`` as a prior worth one attempt, plus the evaluations it
      is expected to consume valued at the shadow price ``remaining dollars /
      remaining evaluations``, so a model-free action is not free when it spends
      an evaluation. Unpriced: evaluations only. The price is never below one
      micro-dollar, the host's rounding unit;
    - score: ``(value + bonus) / price`` with an exploration bonus
      ``explore / ((1 + decisions) * (1 + attempted))`` that decays with decisions.

    It picks the highest score (ties: evaluate, crossover, mutate, migrate); it
    returns ``Stop("passing")`` as soon as an individual passes, ``Stop("budget")``
    when the host reports no room or when the best action's expected passes within
    the remaining budget, ``value * remaining / price``, fall under ``floor``,
    ``Stop("generations")`` after ``generations`` actions beyond seeds and fill, and
    ``Stop("empty")`` when nothing is available. Each seed evaluation and each fill
    mutation keeps the current generation label; every other action is a generation
    of its own. It draws no randomness at all. ``audit()`` gives, for every decision
    including stops, ``expected_value``, ``expected_dollars`` (None unpriced) and
    ``chosen_by`` (``value_per_dollar``, ``exploration`` when the bonus decided,
    ``only_option``, or ``rule:<reason>`` for a stop), the price and its parts, the
    expected passes, and every alternative with its score. It never sees the
    session, the repository or file contents.
    """

    name, revision = "jev", "1"
    KINDS = ("evaluate", "crossover", "mutate", "migrate")  # tie order: a verdict, recombination, a model call, a fetch
    MIN_PRICE = 1e-6  # one micro-dollar, economic.DOLLAR_PLACES

    def __init__(self, *, generations: int, seeds: Sequence[str] = (), prices: Mapping[str, float] | None = None,
                 prior: tuple[float, float] = (1.0, 1.0), explore: float = 0.1, floor: float = 1.0,
                 stop_when_passing: bool = True):
        prices = dict(prices or {})
        if (type(generations) is not int or generations < 1 or len(prior) != 2
                or any(not is_money(x) or x <= 0 for x in prior) or not is_money(explore) or not is_money(floor)
                or any(k not in self.KINDS or not is_money(v) for k, v in prices.items())):
            raise Rejected("invalid_provider", "jev: generations >= 1, a positive Beta prior, explore and floor >= 0, "
                                               "and prices as non-negative dollars per action kind")
        self.generations, self.seeds, self.prices = generations, tuple(seeds), {k: float(v) for k, v in prices.items()}
        self.prior, self.explore, self.floor = (float(prior[0]), float(prior[1])), float(explore), float(floor)
        self.stop_when_passing = stop_when_passing
        self.reset()

    def reset(self) -> None:
        self._generation = 0
        self._seeds = list(self.seeds)
        self._crossed: set[frozenset] = set()
        self._mutated: dict[str, int] = {}
        self._last_audit: dict | None = None

    def _estimate(self, kind: str, s: Situation, priced: bool, shadow: float) -> dict:
        stats = s.history.get(kind, OperatorStats())
        a, b = self.prior
        value = (stats.passed + a) / (stats.attempted + a + b)
        evaluations = (stats.evaluated + 1.0) / (stats.attempted + 1.0)  # prior: one evaluation per attempt
        expected_dollars = None
        if priced:
            expected_dollars = round((self.prices.get(kind, 0.0) + (stats.dollars or 0.0)) / (stats.attempted + 1.0), 6)
        price = max((expected_dollars or 0.0) + evaluations * shadow, self.MIN_PRICE)
        bonus = self.explore / ((1 + s.decisions) * (1 + stats.attempted))
        return {"action": kind, "expected_value": round(value, 6), "expected_dollars": expected_dollars,
                "expected_evaluations": round(evaluations, 6), "price": round(price, 6),
                "exploit": round(value / price, 6), "score": round((value + bonus) / price, 6)}

    def _pair(self, s: Situation) -> tuple[int, int] | None:
        """The two best distinct individuals not yet crossed, as slots, or None."""
        best = None
        for i in range(len(s.population)):
            for j in range(i + 1, len(s.population)):
                a, b = s.population[i], s.population[j]
                if a.id == b.id or frozenset((a.id, b.id)) in self._crossed:
                    continue
                key = (a.score, b.score) if a.score >= b.score else (b.score, a.score)
                if best is None or key > best[0]:
                    best = (key, (i, j))
        return None if best is None else best[1]

    def _slot(self, s: Situation) -> int:
        top = max(p.score for p in s.population)
        return min((i for i, p in enumerate(s.population) if p.score == top),
                   key=lambda i: (self._mutated.get(s.population[i].id, 0), i))

    def _options(self, s: Situation) -> tuple[list[tuple[Action, dict]], dict]:
        priced = getattr(s.remaining, "max_dollars", None) is not None
        remaining = s.remaining.max_dollars if priced else float(s.remaining.max_evaluations)
        shadow = remaining / max(s.remaining.max_evaluations, 1) if priced else 1.0
        budget = {"unit": "dollars" if priced else "evaluations", "remaining": round(remaining, 6), "shadow": round(shadow, 6)}
        g, found = self._generation, []
        if self._seeds:
            found.append((Evaluate(self._seeds[0], g), self._estimate("evaluate", s, priced, shadow)))
        pair = self._pair(s)
        if pair is not None:
            i, j = pair
            found.append((Crossover(s.population[i].id, s.population[j].id, (i, j), g + 1),
                          self._estimate("crossover", s, priced, shadow)))
        if not s.mutator_exhausted:
            if len(s.population) < s.capacity:
                found.append((Mutate("base", None, g), self._estimate("mutate", s, priced, shadow)))
            else:
                slot = self._slot(s)
                found.append((Mutate(s.population[slot].id, slot, g + 1), self._estimate("mutate", s, priced, shadow)))
        if s.exchange and s.population:
            found.append((Migrate(g + 1), self._estimate("migrate", s, priced, shadow)))
        return found, budget

    def _stop(self, reason: str, generation: int, best: dict | None = None, **extra) -> Stop:
        self._last_audit = {"expected_value": best["expected_value"] if best else None,
                            "expected_dollars": best["expected_dollars"] if best else None,
                            "chosen_by": f"rule:{reason}", **extra}
        return Stop(reason, generation)

    def decide(self, s: Situation) -> Action:
        g = self._generation
        if self.stop_when_passing and any(p.passed for p in s.population):
            return self._stop("passing", g)
        if not s.room:
            return self._stop("budget", g)
        options, budget = self._options(s)
        if not options:
            return self._stop("empty", g)
        best = max(options, key=lambda o: o[1]["score"])  # the first of equal scores wins: KINDS order
        action, estimate = best
        expected_passes = round(estimate["expected_value"] * budget["remaining"] / estimate["price"], 6)
        alternatives = [dict(o[1]) for o in options]
        if expected_passes < self.floor:
            return self._stop("budget", g, estimate, expected_passes=expected_passes, floor=self.floor,
                              budget=budget, alternatives=alternatives)
        if action.generation > g and g >= self.generations:
            return self._stop("generations", g + 1, estimate, budget=budget, alternatives=alternatives)
        if len(options) == 1:
            chosen_by = "only_option"
        elif max(options, key=lambda o: o[1]["exploit"]) is best:
            chosen_by = "value_per_dollar"
        else:
            chosen_by = "exploration"
        if isinstance(action, Evaluate):
            self._seeds.pop(0)
        elif isinstance(action, Crossover):
            self._crossed.add(frozenset((action.a, action.b)))
        elif isinstance(action, Mutate) and action.slot is not None:
            self._mutated[action.parent] = self._mutated.get(action.parent, 0) + 1
        self._generation = action.generation
        self._last_audit = {"expected_value": estimate["expected_value"], "expected_dollars": estimate["expected_dollars"],
                            "chosen_by": chosen_by, "price": {k: estimate[k] for k in ("price", "expected_evaluations")},
                            "expected_passes": expected_passes, "budget": budget, "alternatives": alternatives}
        return action

    def audit(self) -> dict | None:
        found, self._last_audit = self._last_audit, None
        return found


def _describe(action: Action) -> dict:
    if isinstance(action, Mutate):
        return {"parent": action.parent, "slot": action.slot}
    if isinstance(action, Crossover):
        return {"pair": [action.a, action.b], "slots": list(action.slots)}
    if isinstance(action, Evaluate):
        return {"candidate": action.candidate}
    if isinstance(action, Stop):
        return {"reason": action.reason}
    return {}


class EvolutionaryProvider:
    """Mutation, crossover, and pairwise selection over Git-lineaged candidates."""

    def __init__(self, mutate: Mutator | Callable, *, population: int = 4, generations: int = 6,
                 crossover_every: int = 3, accept_ours: float = 0.5, seeds: Sequence[str] = (),
                 stop_when_passing: bool = True, name: str = "evolutionary", revision: str = "1",
                 exchange=None, migrate_every: int = 0, migrants: int = 1, policy: BranchPolicy | None = None,
                 stop_on_first_pass: bool = False):
        if population < 2 or generations < 1 or crossover_every < 1 or not 0 <= accept_ours <= 1:
            raise Rejected("invalid_provider", "population >= 2, generations >= 1, crossover_every >= 1, 0 <= accept_ours <= 1")
        if exchange is not None and (migrate_every < 1 or migrants < 1):
            raise Rejected("invalid_provider", "with an exchange, migrate_every and migrants must be at least 1")
        self.operators = ("mutation", "crossover") + (("migration",) if exchange is not None else ())
        self.exchange, self.migrate_every, self.migrants = exchange, migrate_every, migrants
        self.mutate, self.population, self.generations = mutate, population, generations
        self.crossover_every, self.accept_ours, self.seeds = crossover_every, accept_ours, tuple(seeds)
        self.stop_when_passing, self.name, self.revision = stop_when_passing, name, revision
        self.stop_on_first_pass = stop_on_first_pass  # read by the default FixedPolicy only; a given policy keeps its own rule
        self.trace: list[dict] = []
        self.policy = policy if policy is not None else FixedPolicy(
            population=population, generations=generations, crossover_every=crossover_every, seeds=self.seeds,
            stop_when_passing=stop_when_passing, migrate_every=migrate_every if exchange is not None else 0,
            stop_on_first_pass=stop_on_first_pass)
        if (not isinstance(self.policy, BranchPolicy) or not isinstance(self.policy.name, str)
                or not _NAME.match(self.policy.name) or not isinstance(self.policy.revision, str)
                or not 0 < len(self.policy.revision) <= 128):
            raise Rejected("invalid_provider", "a branch policy needs a lowercase name and a revision of at most 128 characters")
        self._decision = 0
        self._stats: dict[str, OperatorStats] = {}

    # Budget: stop cleanly before the host has to refuse.

    def _base(self, space: SearchSpace) -> None:
        """Evaluate the base tree once, before the fill, so a mutation of ``base`` has feedback and not only a parent.

        An ordinary host evaluation (``space.evaluate("base")``): one evaluation of the budget, no model call,
        recorded by the host as its records allow. Skipped, and said so in the trace, when the budget has no
        room; a host that already holds a fresh evaluation of this base (a restart of the search on the same
        thread) answers from its record and the trace shows ``evaluated: 0``. The result is not an individual:
        the population, the branch policies, their ``Situation`` and the selection rule never see it.
        """
        entry: dict = {"generation": 0, "base": {}}
        if not self._room(space):
            entry["base"]["skipped"] = "budget"
        else:
            before = space.remaining().max_evaluations
            found = space.evaluate("base")
            entry["base"].update(score=list(found.score()), passed=found.passed,
                                 evaluated=before - space.remaining().max_evaluations)
        self.trace.append(entry)

    @staticmethod
    def _room(space: SearchSpace) -> bool:
        """Every budget dimension is above zero. ``max_dollars`` is present only on a host that prices usage;
        None means the contract sets no dollar bound. When the dollars run out this returns False and
        ``FixedPolicy`` answers ``Stop("budget")`` as it does for candidates, evaluations and seconds."""
        left = space.remaining()
        max_dollars = getattr(left, "max_dollars", None)
        return (left.max_candidates > 0 and left.max_evaluations > 0 and left.max_seconds > 0
                and (max_dollars is None or max_dollars > 0))

    def _judge(self, space: SearchSpace, candidate: str) -> tuple[str, Score, bool]:
        found = space.evaluate(candidate)
        return candidate, found.score(), found.passed

    def _child(self, space, changes, parents, operator, generation) -> tuple[str, Score, bool] | None:
        # A proposal discarded here is recorded by the space (``refuse``), one record each: an empty edit as
        # ``no_change``, one the budget has no room for as ``limit``. The host records its own refusals itself
        # when ``submit`` raises them; this provider only goes on.
        if not changes:
            refuse(space, "no_change", operator, parents, usage=self._usage(operator))
            return None
        if not self._room(space):
            refuse(space, "limit", operator, parents, usage=self._usage(operator))
            return None
        try:
            made = space.submit(changes, parents=[p for p in parents if p != "base"], operator=operator,
                                message=f"{operator} g{generation}",
                                signal=self._signal(operator, generation))
        except Rejected as exc:
            if exc.code in ("no_change", "invalid_change", "duplicate", "zero_usage"):
                return None
            raise
        if not made.in_scope or not self._room(space):
            return made.id, (0, 0), False
        return self._judge(space, made.id)

    def _signal(self, operator: str, generation: int) -> dict:
        found = {"generation": generation,
                 "policy": {"name": self.policy.name, "revision": self.policy.revision, "decision": self._decision}}
        if operator == "mutation":
            found["mutator"] = getattr(self.mutate, "name", "function")
            usage = getattr(self.mutate, "last_usage", None)
            if usage:
                found["model_usage"] = usage
        return found

    def _usage(self, operator: str) -> dict | None:
        return getattr(self.mutate, "last_usage", None) if operator == "mutation" else None

    def _mutation(self, space, parent, rng, generation):
        try:
            changes = self.mutate(space, parent, rng)
        except Rejected as exc:
            if exc.code.startswith("provider_"):
                refuse(space, "provider_error", "mutation", [parent])  # the call failed; the search ends with its code
            raise
        if not changes:
            reason = getattr(self.mutate, "last_reason", "no_change")
            if reason is None:
                return None  # the mutator made no call (exhausted): there was no proposal to refuse
            refuse(space, reason, "mutation", [parent], usage=self._usage("mutation"))
            return None
        return self._child(space, changes, [parent], "mutation", generation)

    def _crossover(self, space, a: str, b: str, rng, generation):
        ancestor = space.merge_base(a, b)
        if ancestor in (a, b):
            return None  # one contains the other: a merge would add nothing new
        left, right = space.candidate(a), space.candidate(b)
        changes = {}
        for path in sorted(set(left.changed_paths) | set(right.changed_paths)):
            ours, theirs, base = space.read(a, path), space.read(b, path), space.read(ancestor, path)
            if ours == theirs:
                continue
            if ours is None or theirs is None:
                merged = ours if rng.random() < self.accept_ours else theirs
            elif base == ours:
                merged = theirs
            elif base == theirs:
                continue
            else:
                result = space.merge_text(base or "", ours, theirs)
                merged = resolve_conflicts(result.text, rng, self.accept_ours) if result.conflicts else result.text
            if merged != ours:
                changes[path] = merged
        return self._child(space, changes, [a, b], "crossover", generation)

    def _migration(self, space: SearchSpace, people: list, generation: int) -> None:
        """Publish this population, then try the best-reported migrants from other hosts.

        A migrant replaces the worst individual only if its local evaluation is better.
        """
        self._publish(space, people)
        offers = sorted(self.exchange.offers(space.base), key=lambda o: o.score or (0, 0), reverse=True)
        tried = 0
        for offer in offers:
            if tried >= self.migrants or not self._room(space):
                break
            try:
                made = space.migrate(offer.commit, origin={"host": offer.host, "candidate": offer.candidate})
            except Rejected as exc:
                if exc.code in ("already_imported", "migration_base_mismatch", "migration_unavailable"):
                    continue
                raise
            tried += 1
            judged = self._judge(space, made.id) if made.in_scope and self._room(space) else (made.id, (0, 0), False)
            if len(people) < self.population:
                people.append(judged)  # an unfilled slot takes the migrant; selection starts next generation
            else:
                worst = min(range(len(people)), key=lambda i: people[i][1])
                if judged[1] > people[worst][1]:
                    people[worst] = judged
            self.trace.append({"generation": generation, "migrant": made.id, "from": offer.host, "score": list(judged[1])})

    def _publish(self, space: SearchSpace, people: list) -> None:
        entries = []
        for cid, score, _passed in dict.fromkeys(people):
            found = space.candidate(cid)
            entries.append({"candidate": cid, "commit": found.commit, "operator": found.operator,
                            "parents": list(found.parents), "score": list(score)})
        self.exchange.publish(space.base, entries)

    # The policy seam: the policy decides, the provider performs, records and counts.

    def _situation(self, space: SearchSpace, people: list, generation: int, rng: random.Random) -> Situation:
        return Situation(generation=generation,
                         population=tuple(Individual(cid, score, passed) for cid, score, passed in people),
                         capacity=self.population, remaining=space.remaining(), room=self._room(space),
                         history=dict(self._stats), mutator_exhausted=bool(getattr(self.mutate, "exhausted", False)),
                         exchange=self.exchange is not None, decisions=self._decision, rng=rng)

    @staticmethod
    def _holds(people: list, slot, candidate: str) -> bool:
        return type(slot) is int and 0 <= slot < len(people) and people[slot][0] == candidate

    def _perform(self, space: SearchSpace, people: list, rng: random.Random, action: Action) -> dict:
        """Execute one action with the mechanics above and apply the replacement rule. Draws nothing itself."""
        left = space.remaining()
        before, before_dollars = left.max_evaluations, getattr(left, "max_dollars", None)
        child, produced, improved, passed = None, False, False, False
        if isinstance(action, Evaluate):
            judged = self._judge(space, action.candidate)
            slot = next((i for i, p in enumerate(people) if p[0] == action.candidate), None)
            if slot is None:
                people.append(judged)
            else:
                people[slot] = judged
            child, improved, passed = judged, slot is None, judged[2]
        elif isinstance(action, Mutate):
            if action.slot is not None and not self._holds(people, action.slot, action.parent):
                raise Rejected("invalid_policy", f"{self.policy.name}: mutate names a slot that does not hold its parent")
            child = self._mutation(space, action.parent, rng, action.generation)
            if child is not None:
                produced, passed = True, child[2]
                if action.slot is None:
                    people.append(child)
                    improved = True
                elif child[1] > people[action.slot][1]:
                    people[action.slot] = child
                    improved = True
        elif isinstance(action, Crossover):
            i, j = action.slots
            if not self._holds(people, i, action.a) or not self._holds(people, j, action.b):
                raise Rejected("invalid_policy", f"{self.policy.name}: crossover names slots that do not hold its parents")
            child = self._crossover(space, people[i][0], people[j][0], rng, action.generation)
            if child is not None:
                produced, passed = True, child[2]
                if child[1] > people[i][1] and child[1] > people[j][1]:
                    people[i] = people[j] = child
                    improved = True
        else:
            snapshot, entries = list(people), len(self.trace)
            self._migration(space, people, action.generation)
            produced, improved = len(self.trace) > entries, people != snapshot
        left = space.remaining()
        evaluated = before - left.max_evaluations
        # Dollars the host attributed while this action ran: what ``remaining().max_dollars`` lost. None without
        # a dollar bound; floored at zero, so spend past the bound is not seen (the host stops the search then).
        spent = None if before_dollars is None else max(0.0, round(before_dollars - left.max_dollars, 6))
        self._count(action.kind, produced, evaluated, improved, passed, spent)
        outcome = {"candidate": child[0] if child else None, "score": list(child[1]) if child else None,
                   "improved": improved, "evaluated": evaluated}
        if spent is not None:
            outcome["dollars"] = spent  # only a priced host's trace mentions spend, as its records do
        return outcome

    def _count(self, kind: str, produced: bool, evaluated: int, improved: bool, passed: bool,
               dollars: float | None = None) -> None:
        stats = self._stats[kind]
        spent = stats.dollars if dollars is None else round((stats.dollars or 0.0) + dollars, 6)
        self._stats[kind] = dataclasses.replace(
            stats, attempted=stats.attempted + 1, produced=stats.produced + produced, evaluated=stats.evaluated + evaluated,
            improved=stats.improved + improved, passed=stats.passed + passed, dollars=spent)

    _RESERVED = frozenset({"decision", "generation", "policy", "action", "outcome", "parent", "slot", "pair", "slots",
                           "candidate", "reason"})

    def _audited(self, found) -> dict:
        """A policy's account of its last decision, merged into the decision entry. It may not shadow the provider's fields."""
        if found is None:
            return {}
        if not isinstance(found, Mapping) or any(not isinstance(k, str) or k in self._RESERVED for k in found):
            raise Rejected("invalid_policy", f"{self.policy.name}: audit must map non-reserved string keys")
        return dict(found)

    def search(self, space: SearchSpace) -> Sequence[str]:
        rng = random.Random(space.seed)
        people: list[tuple[str, Score, bool]] = []
        priced = getattr(space.remaining(), "max_dollars", None) is not None
        self._stats = {kind: OperatorStats(dollars=0.0 if priced else None) for kind in ACTION_KINDS if kind != "stop"}
        self._decision = 0
        self.policy.reset()
        self._base(space)
        generation, ran = 0, True  # generation 0, the initial population, is always recorded
        while True:
            action = self.policy.decide(self._situation(space, people, generation, rng))
            if (not isinstance(action, (Mutate, Crossover, Migrate, Evaluate, Stop)) or type(action.generation) is not int
                    or action.generation < generation or action.generation > self.generations + 1):
                raise Rejected("invalid_policy", f"{self.policy.name} returned an action outside the closed set "
                                                 "or a generation label out of order")
            while action.generation > generation:  # close every generation up to the label; a skipped one ran empty
                self.trace.append({"generation": generation, "population": [p[0] for p in people]})
                generation, ran = generation + 1, False
            self._decision += 1
            entry = {"decision": self._decision, "generation": generation,
                     "policy": {"name": self.policy.name, "revision": self.policy.revision},
                     "action": action.kind, **_describe(action)}
            audit = getattr(self.policy, "audit", None)
            if callable(audit):
                entry.update(self._audited(audit()))
            if isinstance(action, Stop):
                if action.reason not in CLEAN_STOPS:
                    self.trace.append({"generation": generation, "population": [p[0] for p in people], "stopped": action.reason})
                elif ran:
                    self.trace.append({"generation": generation, "population": [p[0] for p in people]})
                # The proposals this search refused, counted by reason, as ``search_finished.refused`` would carry
                # them if that frozen record could grow: the entry before the stop decision carries them instead,
                # so the stop decision stays the last entry and keeps its shape. {} on a space that keeps no count.
                self.trace.append({"generation": generation, "refused": dict(getattr(space, "refused", None) or {})})
                self.trace.append(entry)
                break
            self.trace.append(entry)
            entry["outcome"] = self._perform(space, people, rng, action)
            ran = True
        if self.exchange is not None and people:
            self._publish(space, people)
        ranked = sorted(dict.fromkeys(people), key=lambda p: p[1], reverse=True)
        passing = [p[0] for p in ranked if p[2]]
        return passing or [p[0] for p in ranked[:1]]
