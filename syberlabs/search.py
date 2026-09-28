"""Provider-neutral search interface.

A ``SearchProvider`` explores changes and hands them to the host through a
``SearchSpace``. The provider never receives the session, the approval API, or
the promotion target. Everything it submits becomes a provisional candidate
that the host scopes, records, and evaluates itself. Replacing one provider
with another (a person's patch, a model adapter, an evolutionary search)
changes nothing on the authority side.

These are structural types. The Build Thread supplies the Git-backed space.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Budget:
    """Limits for one search. The host enforces them; the provider may read them."""

    max_candidates: int
    max_evaluations: int
    max_seconds: int


@dataclass(frozen=True)
class CheckResult:
    name: str
    state: str  # passed, failed, timed_out, error, or not_run
    exit_code: int | None
    duration_ms: int
    output_tail: str = ""


@dataclass(frozen=True)
class Evaluation:
    """The host's own run of the contract's checks on one candidate tree."""

    candidate: str
    checks: tuple[CheckResult, ...]
    required: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Every required check ran and passed. A check that did not run is not a pass."""
        states = {check.name: check.state for check in self.checks}
        return all(states.get(name) == "passed" for name in self.required)

    def score(self) -> tuple[int, int]:
        """Required checks passed, then all checks passed. For provider selection only."""
        states = {check.name: check.state for check in self.checks}
        return (sum(states.get(name) == "passed" for name in self.required),
                sum(state == "passed" for state in states.values()))


@dataclass(frozen=True)
class Candidate:
    """A provisional change with Git lineage. Lineage is provenance, not evidence of superiority."""

    id: str
    commit: str
    tree: str
    base: str
    parents: tuple[str, ...]
    operator: str
    provider: str
    provider_revision: str
    changed_paths: tuple[str, ...]
    scope_violations: tuple[str, ...] = ()
    limit_violations: tuple[str, ...] = ()
    diff_bytes: int = 0
    signal: Mapping[str, Any] | None = None
    note: str = ""
    evaluation: str = "none"  # none, passed, failed, stale
    promotion: str = "provisional"  # provisional, denied, needs_approval, admitted, unresolved, not_applied, promoted
    promotion_reason: str | None = None
    proposal_id: str | None = None
    checks: tuple[CheckResult, ...] = field(default=(), compare=False)

    @property
    def in_scope(self) -> bool:
        return not self.scope_violations and not self.limit_violations

    @property
    def authoritative(self) -> bool:
        return self.promotion == "promoted"

    @classmethod
    def from_view(cls, view: Mapping[str, Any]) -> "Candidate":
        return cls(
            id=view["id"], commit=view["commit"], tree=view["tree"], base=view["base"],
            parents=tuple(view["parents"]), operator=view["operator"],
            provider=view["provider"]["name"], provider_revision=view["provider"]["revision"],
            changed_paths=tuple(view["changed_paths"]), scope_violations=tuple(view["scope_violations"]),
            limit_violations=tuple(view["limit_violations"]), diff_bytes=view["diff"]["bytes"],
            signal=view["signal"], note=view["note"], evaluation=view["evaluation"]["state"],
            promotion=view["promotion"]["state"], promotion_reason=view["promotion"]["reason"],
            proposal_id=view["promotion"]["proposal_id"],
            checks=tuple(CheckResult(c["name"], c["state"], c["exit_code"], c["duration_ms"], c["output_tail"])
                         for c in view["evaluation"]["checks"]),
        )


@dataclass(frozen=True)
class MergeResult:
    """A three-way text merge. ``conflicts`` counts regions the provider must resolve."""

    text: str
    conflicts: int


@runtime_checkable
class SearchSpace(Protocol):
    """What a provider may do. Reads are limited to the contract's scope."""

    objective: str
    base: str
    scope: tuple[str, ...]
    budget: Budget
    context: tuple[Mapping[str, Any], ...]
    seed: int

    def files(self) -> list[str]:
        """In-scope file paths at the base commit."""

    def read(self, ref: str, path: str) -> str | None:
        """Text of ``path`` at ``"base"`` or a candidate id; None if absent or out of scope."""

    def merge_base(self, a: str, b: str) -> str:
        """The nearest common ancestor of two candidates, as a candidate id or ``"base"``."""

    def merge_text(self, base: str, ours: str, theirs: str) -> MergeResult:
        """Three-way merge of three texts, with conflict markers left in place."""

    def submit(self, changes: Mapping[str, str | None], *, parents: Sequence[str] = (), operator: str,
               message: str = "", signal: Mapping[str, Any] | None = None) -> Candidate:
        """Create a provisional candidate from file edits on ``parents[0]`` (or base).

        ``None`` deletes a file. The host writes the commit on a non-authoritative
        ref, computes changed paths against the base, and records the candidate.
        """

    def evaluate(self, candidate: str) -> Evaluation:
        """Run the contract's checks on the candidate. Counts against the budget."""

    def remaining(self) -> Budget:
        """Budget left in this search."""


@runtime_checkable
class SearchProvider(Protocol):
    """Anything that proposes candidate changes.

    ``search`` returns the candidate ids it recommends, or None. A recommendation
    is a signal recorded with the search; it is not evidence and cannot promote.
    """

    name: str
    revision: str
    operators: tuple[str, ...]

    def search(self, space: SearchSpace) -> Sequence[str] | None:
        ...
