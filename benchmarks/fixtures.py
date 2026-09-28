"""Small repositories for comparing search strategies. Each has several independently checked parts.

A fixture is files, an objective, and one check per part. ``slots`` describe,
for the simulated model only, which line of which function a part lives on and
which replacements are right; a real model never sees them.
"""

from __future__ import annotations

import sys

PRICING = '''"""Pricing rules for the demo shop."""


def discount(total):
    return total


# Taxes are applied after discounts.


def tax(amount):
    return amount


# Shipping depends on weight.


def shipping(weight):
    return 0
'''

TEXT = '''"""Turn titles into URL slugs."""

import re


def lower(text):
    return text


# Runs of dashes become one dash.


def collapse(text):
    return text


# No dash at either end.


def trim(text):
    return text


# The pipeline.


def slugify(text):
    return trim(collapse(lower(text).replace(" ", "-")))
'''

INTERVALS = '''"""Merge closed integer intervals."""


def order(items):
    return items


# When do two sorted intervals overlap?


def overlaps(a, b):
    return False


# The union of two overlapping intervals.


def join(a, b):
    return a


# The merge loop.


def merge(items):
    out = []
    for item in order(items):
        if out and overlaps(out[-1], item):
            out[-1] = join(out[-1], item)
        else:
            out.append(item)
    return out
'''


def _check(module: str, expression: str) -> dict:
    return {"argv": [sys.executable, "-c", f"import sys; sys.path.insert(0, 'src'); import {module}; assert {expression}"],
            "timeout_seconds": 30}


FIXTURES = {
    "pricing": {
        "files": {"src/pricing.py": PRICING},
        "objective": "Make discount (10% off totals over 100), tax (8%), and shipping (5 per kg) pass their checks",
        "checks": {"discount": _check("pricing", "pricing.discount(200) == 180 and pricing.discount(50) == 50"),
                   "tax": _check("pricing", "pricing.tax(100) == 108"),
                   "shipping": _check("pricing", "pricing.shipping(3) == 15")},
        "slots": [
            {"path": "src/pricing.py", "function": "discount", "check": "discount",
             "variants": ["return total", "return total * 0.9", "return total * 0.9 if total > 100 else total", "return total - 10"],
             "correct": [2]},
            {"path": "src/pricing.py", "function": "tax", "check": "tax",
             "variants": ["return amount", "return amount * 1.08", "return round(amount * 1.08, 2)", "return amount + 8"],
             "correct": [2, 3]},
            {"path": "src/pricing.py", "function": "shipping", "check": "shipping",
             "variants": ["return 0", "return weight * 5", "return weight * 4", "return 5"], "correct": [1]},
        ],
    },
    "slugify": {
        "files": {"src/text.py": TEXT},
        "objective": "Make slugify lowercase text, collapse repeated dashes, and trim dashes from both ends",
        "checks": {"lower": _check("text", "text.slugify('Hello World') == 'hello-world'"),
                   "collapse": _check("text", "text.slugify('a  b') == 'a-b'"),
                   "trim": _check("text", "text.slugify(' a ') == 'a'")},
        "slots": [
            {"path": "src/text.py", "function": "lower", "check": "lower",
             "variants": ["return text", "return text.lower()", "return text.upper()", "return text.casefold()"], "correct": [1, 3]},
            {"path": "src/text.py", "function": "collapse", "check": "collapse",
             "variants": ["return text", "return re.sub('-+', '-', text)", "return text.replace('-', '')", "return text.replace('--', '-')"],
             "correct": [1, 3]},
            {"path": "src/text.py", "function": "trim", "check": "trim",
             "variants": ["return text", "return text.strip('-')", "return text.strip()", "return text[1:-1]"], "correct": [1]},
        ],
    },
    "intervals": {
        "files": {"src/intervals.py": INTERVALS},
        "objective": "Make merge sort its input and merge overlapping and nested intervals",
        "checks": {"sorted": _check("intervals", "intervals.merge([(5, 6), (1, 2)]) == [(1, 2), (5, 6)]"),
                   "overlap": _check("intervals", "intervals.merge([(1, 3), (2, 4)]) == [(1, 4)]"),
                   "nested": _check("intervals", "intervals.merge([(1, 10), (2, 3)]) == [(1, 10)]")},
        "slots": [
            {"path": "src/intervals.py", "function": "order", "check": "sorted",
             "variants": ["return items", "return sorted(items)", "return list(reversed(items))", "return sorted(items, reverse=True)"],
             "correct": [1]},
            {"path": "src/intervals.py", "function": "overlaps", "check": "overlap",
             "variants": ["return False", "return b[0] <= a[1]", "return b[0] < a[0]", "return True"], "correct": [1]},
            {"path": "src/intervals.py", "function": "join", "check": "nested",
             "variants": ["return a", "return (a[0], max(a[1], b[1]))", "return (a[0], b[1])", "return b"], "correct": [1]},
        ],
    },
}


def contract(name: str) -> dict:
    fixture = FIXTURES[name]
    return {
        "id": f"bench-{name}", "version": 1, "title": fixture["objective"],
        "inputs": {"objective": "string"},
        "actions": {"accept_change": {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
        "evolution": {
            "scope": {"paths": ["src/"], "max_files": 5, "max_diff_bytes": 50_000},
            "operators": ["patch", "mutation", "crossover"],
            "budget": {"max_candidates": 400, "max_evaluations": 400, "max_seconds": 3600},
            "evaluation": {"checks": fixture["checks"], "required": sorted(fixture["checks"])},
            "promotion": {"action": "accept_change", "roles": ["developer"]},
        },
    }
