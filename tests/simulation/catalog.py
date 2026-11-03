"""The production modules and the one campaign that must drive each of them."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKIP_FILES = {"__main__.py", "_patterns.py"}


def production_modules() -> set[str]:
    """Importable modules under ``syberlabs`` and ``syberwork``.

    ``__main__`` is a one-line trampoline. ``_patterns`` is private pattern
    data, exercised by the modules that import it.
    """
    found = set()
    for package in ("syberlabs", "syberwork"):
        for path in (ROOT / package).rglob("*.py"):
            if path.name in SKIP_FILES or any(part == "__pycache__" for part in path.parts):
                continue
            relative = path.relative_to(ROOT).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            found.add(".".join(parts))
    return found


def assignments() -> dict[str, str]:
    """Map each production module to the campaign that drives it."""
    from tests.simulation.campaigns import CAMPAIGNS

    assigned: dict[str, str] = {}
    for name, modules in CAMPAIGNS.items():
        for module in modules:
            if module in assigned:
                raise AssertionError(f"{module} is claimed by {assigned[module]} and {name}")
            assigned[module] = name
    return assigned


def gaps() -> tuple[set[str], set[str]]:
    """Modules with no campaign, and campaign names that are not modules."""
    assigned = assignments()
    discovered = production_modules()
    return discovered - set(assigned), set(assigned) - discovered
