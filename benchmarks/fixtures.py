"""Small repositories for comparing search strategies. Each has several independently checked parts.

A fixture is files, an objective, and one check per part. ``slots`` describe,
for the simulated model only, which line of which function a part lives on and
which replacements are right; a real model never sees them.

Two generations. The first three (``pricing``, ``slugify``, ``intervals``) are
one-line slots a model solves in one call (the 2026-10-21 pilot). ``V2`` names
the four built from this program's own defects, where a passing change needs a
reading of the failure, not a line swap: their slots list only wrong surface
replacements (``correct: []``), so the simulated model fails them by
construction and a ``single`` solve on them can only come from a real model.
A file whose content is ``bytes`` is written verbatim (``.gitattributes`` keeps
Git from translating it), which is how a fixture carries CRLF on every platform.
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

# ---- v2: fixtures from this program's own defects -------------------------------------------

# (a) Flyspace fieldHarness (docs/CONSUMERS.md, second-consumer packet 2026-10-18): a report stored
# with LF read back as CRLF under core.autocrlf while the renderer joined with "\n". The export here
# is committed with CRLF bytes behind a .gitattributes rule, so every platform sees the same file.
FIELDS = '''"""Field report export: render a table and compare it with the committed export."""


def render(rows):
    return "\\n".join(rows)


def load(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def lines(path):
    return load(path).split("\\n")


def matches(path, rows):
    return render(rows) in load(path)
'''

EXPORT = b"name\r\nemail\r\nphone\r\n"

# (b) Flyspace oauthConnect (same packet): the "mismatched" state was state[:-1] + "x"; one state in
# N already ends in "x", so the tampered state equalled the real one and the assertion failed by
# chance. finish_connect also accepts any returned state that merely starts with the expected one.
OAUTH = '''"""OAuth connect flow: the state round trip."""

import random
import string

ALPHABET = string.ascii_lowercase + string.digits


def make_state(seed):
    rng = random.Random(seed)
    return "".join(rng.choice(ALPHABET) for _ in range(24))


def tamper(state):
    return state[:-1] + "x"


def finish_connect(expected, returned):
    return returned[:len(expected)] == expected
'''

# (c) The settlement window of #35, closed by #38: a settlement and its verdict are two appends, and
# the recovery path measured the input again instead of reading the durable record back.
LEDGER = '''"""Settle a reading against a target, durably, as a settlement record and then a verdict."""

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
    settled = [r for r in found if r["kind"] == "settlement"]
    verdicts = [r for r in found if r["kind"] == "verdict"]
    if len(settled) == len(verdicts):
        return False
    append(path, {"kind": "verdict", "verdict": verdict(reading, target)})
    return True
'''

# (d) The npm / npm.cmd finding of #51: a tool launched by bare name that only exists on disk with a
# platform suffix, and a hand-rolled search that never tries one. Checks run under a scrubbed PATH
# holding one fake tool directory.
LAUNCH = '''"""Launch a repository's tool by name from a list of directories."""

import os
import subprocess


class ToolNotFound(Exception):
    pass


def resolve(name, dirs):
    for folder in dirs:
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
    return name


def run_tool(name, args, dirs):
    done = subprocess.run([resolve(name, dirs), *args], capture_output=True, text=True,
                          env={**os.environ, "PATH": os.pathsep.join(dirs)})
    return done.stdout.strip()
'''

# The fake tool the tool_path checks place on their scrubbed PATH: a batch file on Windows, a shell
# script elsewhere, in both cases present under the suffixed name only.
FAKE_TOOL = '''
import os, stat, tempfile
folder = tempfile.mkdtemp()
tool = os.path.join(folder, "syber-tool.cmd")
if os.name == "nt":
    open(tool, "wb").write(b"@echo off\\r\\necho ok-%1\\r\\n")
else:
    open(tool, "w").write("#!/bin/sh\\necho ok-$1\\n")
    os.chmod(tool, os.stat(tool).st_mode | stat.S_IXUSR)
os.environ["PATH"] = folder
'''


def _script(module: str, body: str) -> dict:
    """A check that runs ``body`` with ``module`` imported from ``src`` in the fixture worktree."""
    code = f"import sys; sys.path.insert(0, 'src'); import {module}\n" + body
    return {"argv": [sys.executable, "-c", code], "timeout_seconds": 60}


SEEDS = "range(1, 201)"

V2 = {
    "crlf_export": {
        "files": {"src/fields.py": FIELDS, "src/export.txt": EXPORT, ".gitattributes": "src/export.txt -text\n"},
        "objective": ("Make the field report comparison hold for an export written with Windows line endings: "
                      "matches must accept the committed src/export.txt as it is, lines must give the same list "
                      "for CRLF and LF files with no empty trailing entry, and src/export.txt itself must not change"),
        "checks": {
            "report": _script("fields", "assert fields.matches('src/export.txt', ['name', 'email', 'phone'])\n"
                                        "assert open('src/export.txt', 'rb').read() == b'name\\r\\nemail\\r\\nphone\\r\\n', 'export.txt was edited'\n"),
            "lines": _script("fields", "assert fields.lines('src/export.txt') == ['name', 'email', 'phone'], fields.lines('src/export.txt')\n"),
            "lf": _script("fields", "import tempfile, os\n"
                                    "f = tempfile.NamedTemporaryFile('wb', delete=False); f.write(b'name\\nemail\\nphone\\n'); f.close()\n"
                                    "assert fields.lines(f.name) == ['name', 'email', 'phone'], fields.lines(f.name)\n"
                                    "assert fields.matches(f.name, ['name', 'email', 'phone'])\n"),
        },
        "slots": [
            {"path": "src/fields.py", "function": "lines", "check": "lines",
             "variants": ["return load(path).split(\"\\n\")", "return load(path).split(\"\\n\")[:-1]", "return load(path).strip().split(\"\\n\")"],
             "correct": []},
        ],
    },
    "oauth_state": {
        "files": {"src/oauth.py": OAUTH},
        "objective": ("Make the connect flow's state check sound: finish_connect accepts exactly the expected state "
                      "and nothing longer or shorter, and tamper always yields a different state of the same length "
                      "over the same alphabet, for every seed"),
        "checks": {
            "rejects_tampered": _script("oauth", f"for seed in {SEEDS}:\n"
                                                 "    s = oauth.make_state(seed)\n"
                                                 "    assert not oauth.finish_connect(s, oauth.tamper(s)), (seed, s)\n"),
            "rejects_extended": _script("oauth", f"for seed in {SEEDS}:\n"
                                                 "    s = oauth.make_state(seed)\n"
                                                 "    assert oauth.finish_connect(s, s), seed\n"
                                                 "    assert not oauth.finish_connect(s, s + '0'), seed\n"
                                                 "    assert not oauth.finish_connect(s, s[:-1]), seed\n"),
            "tamper_differs": _script("oauth", f"for seed in {SEEDS}:\n"
                                               "    s = oauth.make_state(seed); t = oauth.tamper(s)\n"
                                               "    assert t != s and len(t) == len(s) and set(t) <= set(oauth.ALPHABET), (seed, s, t)\n"),
        },
        "slots": [
            {"path": "src/oauth.py", "function": "tamper", "check": "tamper_differs",
             "variants": ["return state[:-1] + \"x\"", "return state[:-1] + \"z\"", "return state[:-1] + \"0\""], "correct": []},
            {"path": "src/oauth.py", "function": "finish_connect", "check": "rejects_extended",
             "variants": ["return returned[:len(expected)] == expected", "return returned.startswith(expected)", "return expected in returned"],
             "correct": []},
        ],
    },
    "settlement": {
        "files": {"src/ledger.py": LEDGER},
        "objective": ("Make recovery after a death between the settlement record and its verdict settle exactly once "
                      "from the durable record: the verdict must come from the recorded reading, not the reading "
                      "recover is handed; a death before any record settles once from that reading; a second "
                      "recover changes nothing"),
        "checks": {
            "between": _script("ledger", "import tempfile, os\n"
                                         "p = os.path.join(tempfile.mkdtemp(), 'ledger.jsonl')\n"
                                         "class Death(BaseException): pass\n"
                                         "def die(): raise Death()\n"
                                         "try:\n    ledger.settle(p, 7, 5, die_between=die)\nexcept Death:\n    pass\n"
                                         "assert ledger.recover(p, 1, 5) is True\n"
                                         "found = ledger.records(p)\n"
                                         "assert [r['kind'] for r in found] == ['settlement', 'verdict'], found\n"
                                         "assert found[1]['verdict'] == 'moved', found\n"),
            "before": _script("ledger", "import tempfile, os\n"
                                        "p = os.path.join(tempfile.mkdtemp(), 'ledger.jsonl')\n"
                                        "assert ledger.recover(p, 2, 5) is True\n"
                                        "found = ledger.records(p)\n"
                                        "assert [r['kind'] for r in found] == ['settlement', 'verdict'], found\n"
                                        "assert found[0]['reading'] == 2 and found[1]['verdict'] == 'stayed', found\n"),
            "twice": _script("ledger", "import tempfile, os\n"
                                       "p = os.path.join(tempfile.mkdtemp(), 'ledger.jsonl')\n"
                                       "class Death(BaseException): pass\n"
                                       "def die(): raise Death()\n"
                                       "try:\n    ledger.settle(p, 3, 5, die_between=die)\nexcept Death:\n    pass\n"
                                       "assert ledger.recover(p, 9, 5) is True\n"
                                       "assert ledger.recover(p, 9, 5) is False\n"
                                       "found = ledger.records(p)\n"
                                       "assert [r['kind'] for r in found] == ['settlement', 'verdict'], found\n"
                                       "assert found[1]['verdict'] == 'stayed', found\n"),
        },
        "slots": [
            {"path": "src/ledger.py", "function": "recover", "check": "between",
             "variants": ["found = records(path)", "found = records(path)[-2:]", "found = records(path)[-1:]"], "correct": []},
        ],
    },
    "tool_path": {
        "files": {"src/launch.py": LAUNCH},
        "objective": ("Make resolve find a tool that exists on disk only with a platform suffix such as .cmd, "
                      "return its absolute path, raise ToolNotFound when no directory holds it, and make run_tool "
                      "launch the resolved file so the tool runs under a PATH that holds only its own directory"),
        "checks": {
            "suffixed": _script("launch", FAKE_TOOL + "found = launch.resolve('syber-tool', [folder])\n"
                                                      "assert os.path.isabs(found) and os.path.samefile(found, tool), found\n"),
            "missing": _script("launch", FAKE_TOOL + "empty = tempfile.mkdtemp()\n"
                                                     "try:\n    launch.resolve('syber-tool', [empty])\n"
                                                     "except launch.ToolNotFound:\n    pass\n"
                                                     "else:\n    raise AssertionError('resolve returned instead of raising ToolNotFound')\n"),
            "launch": _script("launch", FAKE_TOOL + "out = launch.run_tool('syber-tool', ['build'], [folder])\n"
                                                    "assert out == 'ok-build', repr(out)\n"),
        },
        "slots": [
            {"path": "src/launch.py", "function": "resolve", "check": "suffixed",
             "variants": ["for folder in dirs:", "for folder in reversed(dirs):", "for folder in sorted(dirs):"], "correct": []},
        ],
    },
}

FIXTURES.update(V2)


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
