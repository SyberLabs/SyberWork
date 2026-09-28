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
- Every individual is a provisional candidate on a non-authoritative ref.
  Selection here only decides what the search keeps exploring and what it
  recommends. Promotion is a person's decision through admission.
- The contract fixes the scope, the operators (``mutation`` and ``crossover``
  must both be listed), and the budget. The host enforces them.
- Human feedback is the objective, seed candidates a person picks (EvoGit's
  "human tags"), and the promotion gate.

The mutation operator is pluggable. ``CommandMutator`` is the model seam: any
model adapter that reads JSON on stdin and prints changed files.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from syberlabs.errors import Rejected
from syberlabs.providers import run_json
from syberlabs.search import SearchSpace

Score = tuple[int, int]


class Mutator(Protocol):
    name: str

    def __call__(self, space: SearchSpace, parent: str, rng: random.Random) -> Mapping[str, str | None] | None:
        """File edits that make a child of ``parent`` (a candidate id or ``"base"``), or None."""


class CommandMutator:
    """Mutation by an external program, typically a model adapter.

    Request (stdin): ``{protocol, objective, parent, files: {path: text}, context, seed}``.
    Response (stdout): ``{"changes": {path: text or null}}``.
    """

    PROTOCOL = "syberlabs.mutate/v0alpha1"

    def __init__(self, argv: Sequence[str], *, name: str = "command", timeout: int = 300,
                 cwd: str | Path | None = None, max_bytes: int = 65_536):
        self.argv, self.name, self.timeout, self.cwd, self.max_bytes = list(argv), name, timeout, cwd, max_bytes

    def __call__(self, space: SearchSpace, parent: str, rng: random.Random) -> Mapping[str, str | None] | None:
        paths = list(dict.fromkeys([*(item["path"] for item in space.context),
                                    *(space.candidate(parent).changed_paths if parent != "base" else ())]))
        files, spent = {}, 0
        for path in paths:
            text = space.read(parent, path)
            if text is None or spent + len(text) > self.max_bytes:
                continue
            files[path] = text
            spent += len(text)
        response = run_json(self.argv, {"protocol": self.PROTOCOL, "objective": space.objective, "parent": parent,
                                        "files": files, "context": [dict(item) for item in space.context],
                                        "seed": rng.randrange(2**31)}, timeout=self.timeout, cwd=self.cwd)
        changes = response.get("changes") if isinstance(response, dict) else None
        if not isinstance(changes, dict):
            return None
        return {k: v for k, v in changes.items() if isinstance(k, str) and (v is None or isinstance(v, str))}


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


class EvolutionaryProvider:
    """Mutation, crossover, and pairwise selection over Git-lineaged candidates."""

    operators = ("mutation", "crossover")

    def __init__(self, mutate: Mutator | Callable, *, population: int = 4, generations: int = 6,
                 crossover_every: int = 3, accept_ours: float = 0.5, seeds: Sequence[str] = (),
                 stop_when_passing: bool = True, name: str = "evolutionary", revision: str = "1"):
        if population < 2 or generations < 1 or crossover_every < 1 or not 0 <= accept_ours <= 1:
            raise Rejected("invalid_provider", "population >= 2, generations >= 1, crossover_every >= 1, 0 <= accept_ours <= 1")
        self.mutate, self.population, self.generations = mutate, population, generations
        self.crossover_every, self.accept_ours, self.seeds = crossover_every, accept_ours, tuple(seeds)
        self.stop_when_passing, self.name, self.revision = stop_when_passing, name, revision
        self.trace: list[dict] = []

    # Budget: stop cleanly before the host has to refuse.

    @staticmethod
    def _room(space: SearchSpace) -> bool:
        left = space.remaining()
        return left.max_candidates > 0 and left.max_evaluations > 0 and left.max_seconds > 0

    def _judge(self, space: SearchSpace, candidate: str) -> tuple[str, Score, bool]:
        found = space.evaluate(candidate)
        return candidate, found.score(), found.passed

    def _child(self, space, changes, parents, operator, generation) -> tuple[str, Score, bool] | None:
        if not changes or not self._room(space):
            return None
        try:
            made = space.submit(changes, parents=[p for p in parents if p != "base"], operator=operator,
                                message=f"{operator} g{generation}",
                                signal={"generation": generation, "mutator": getattr(self.mutate, "name", "function")}
                                if operator == "mutation" else {"generation": generation})
        except Rejected as exc:
            if exc.code in ("no_change", "invalid_change"):
                return None
            raise
        if not made.in_scope or not self._room(space):
            return made.id, (0, 0), False
        return self._judge(space, made.id)

    def _mutation(self, space, parent, rng, generation):
        return self._child(space, self.mutate(space, parent, rng), [parent], "mutation", generation)

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

    def search(self, space: SearchSpace) -> Sequence[str]:
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
            if not self._room(space) or (self.stop_when_passing and any(p[2] for p in people)) or len(people) < 2:
                break
            if generation % self.crossover_every == 0:
                order = rng.sample(range(len(people)), len(people))
                for i, j in zip(order[::2], order[1::2]):
                    child = self._crossover(space, people[i][0], people[j][0], rng, generation)
                    if child and child[1] > people[i][1] and child[1] > people[j][1]:
                        people[i] = people[j] = child
            else:
                for index, (parent, score, _passed) in enumerate(list(people)):
                    child = self._mutation(space, parent, rng, generation)
                    if child and child[1] > score:
                        people[index] = child
            self.trace.append({"generation": generation, "population": [p[0] for p in people]})
        ranked = sorted(dict.fromkeys(people), key=lambda p: p[1], reverse=True)
        passing = [p[0] for p in ranked if p[2]]
        return passing or [p[0] for p in ranked[:1]]
