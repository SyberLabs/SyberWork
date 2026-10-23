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

``V3`` names four more, built after the 2026-10-21 pilots showed v1 and v2 solved
single-shot: the objective states the symptom at the level of an issue, never
the fix, and each check pins a convention this program's own history pinned
(exact line formats, canonical JSON, a call order, the right site among several
files) that the visible files do not determine. A check's assertion message
names the expectation, so an arm that reads check output can converge where a
one-call patch has to guess. Their slots, like v2's, hold no correct variant.

``V4`` names four more, built after the v3 runs of 2026-10-22 showed ``repair``
closing every v3 fixture in one or two calls: each is three to five files and
six to eight independently checked parts that live in different files under
different conventions; the objective states each area's symptom, never the
fix; the parts are separable (the reference fix of one part touches a region no
other part's fix touches, so a child that fixed some parts and a child that
fixed others recombine into one that passes both); and a failing check names
its part first, so a directed mutation of a partial child can target what is
left. Their slots, like v2's and v3's, hold no correct variant.
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

# ---- v3: fixtures whose checks pin conventions the visible files do not determine ---------------

# (a) #58 (db87875): spec/GAPS.md was renumbered so every entry has one number in file order, and
# the references to renumbered entries lived in docs, in two module docstrings (syberlabs/checks.py,
# syberlabs/critic.py) and in a test docstring (tests/test_protocol.py). The fixture holds a GAPS-like
# file with gaps in its numbering and references in three other files, two of them docstrings; the
# checks also hold that numbers which are not references (a plan step, a byte count, a date, a
# range) stay as they are, which a textual substitution of old numbers for new breaks.
GAPS_FILE = """# Known gaps

Each entry names one thing the kit does not do. The other files cite entries by number.

1. **The chain is per thread.** A journal's hash chain covers one thread's events, not the store.

2. **A refusal is not an event.** The kernel returns a refusal code; nothing is appended.

4. **A worktree is removed, then pruned.** The registration is deleted after the directory (added 2026-10-17).

5. **The output cap is 65,536 bytes.** A longer check output is digested whole and stored truncated.

7. **The lock is advisory on Windows.** `msvcrt.locking` holds one byte at offset 0.

8. **A setup step is a check by name.** A check named `setup` or `install` runs before the others.
"""

GAPS_NOTES = """# Working notes

Plan step 4 of 7: the worktree cleanup (see gaps.md entry 4) must land before the lock
work (entry 7). The cap in entry 5 stays at 65,536 bytes until 2026-10-24.

Entry 2 is unchanged by any of this; entries 1 and 2 keep their numbers.
"""

GAPS_CHECKS = '''"""Run a contract's checks in name order.

A check named setup or install runs first and gates the rest, which is why the step is a check by
name (gaps.md, 8). The output cap (gaps.md, 5) applies to every check's stdout and stderr together.
"""

SETUP_NAMES = ("setup", "install")


def entry():
    return 8


def gated(names):
    ordered = sorted(names)
    return [name for name in ordered if name not in SETUP_NAMES]
'''

GAPS_TEST = '''"""The worktree removal order and the Windows lock (gaps 4 and 7)."""

import unittest


class Removal(unittest.TestCase):
    def test_order(self):
        steps = ["remove", "rmtree", "prune"]
        self.assertEqual(len(steps[0]), 6)
        self.assertEqual(list(range(7))[4], 4)
'''

GAPS_TITLES = ["The chain is per thread.", "A refusal is not an event.", "A worktree is removed, then pruned.",
               "The output cap is 65,536 bytes.", "The lock is advisory on Windows.", "A setup step is a check by name."]

# (b) syberlabs/events.py and syberlabs/canonical.py: an event's hash is the SHA-256 of the canonical
# JSON (sorted keys, separators "," and ":", non-ASCII unescaped) of six envelope fields, the first
# event's previous is 64 zeros, and verify_chain recomputes every digest. The vectors below were
# produced by that rule from the kit's own functions; a candidate has to reproduce them byte for byte.
CHAIN = '''"""An append-only event journal whose events form a hash chain."""

import hashlib
import json


def canonical(value):
    return json.dumps(value)


def event_digest(event):
    return hashlib.sha256(canonical(event).encode("utf-8")).hexdigest()


def append(events, case_id, kind, body, at):
    event = {"case_id": case_id, "seq": len(events) + 1, "kind": kind, "body": body, "at": at,
             "previous": events[-1]["hash"] if events else None}
    event["hash"] = event_digest(event)
    events.append(event)
    return event


def verify(events):
    return all("hash" in event for event in events)
'''

CHAIN_EVENTS = [("case-7", "case_created", {"objective": "renumber", "inputs": {"b": 1, "a": 2}}, "2026-10-21T00:00:00Z"),
                ("case-7", "observed", {"key": "note", "value": "café", "verified": True}, "2026-10-21T00:00:01Z"),
                ("case-7", "decision", {"status": "accepted", "by": ["r", "q"]}, "2026-10-21T00:00:02Z")]
# The checks' messages quote non-ASCII; the host decodes check output as UTF-8 on every platform.
UTF8 = "sys.stderr.reconfigure(encoding='utf-8')\n"
CHAIN_HASHES = ["acc5fa69ba9773e0a1c347e1c16d77cd7923e03f4531c4c5b313ea4ccd8da74d",
                "15301547bd8d770081839b9cc89c99438ca28a93839885a6e9aae03db8bf5f56",
                "571a94ec18997af8204918a08ff372dca3ca1ac02a8fae016f03da6869011c41"]

# (c) #61 (e19c74b, superseding #59): on Windows a check's child can hold a file in the evaluation
# worktree for a moment, so `git worktree remove` failed with Permission denied and a passing check
# became an error; a stale registration remained when prune ran before the directory was gone. The
# fix retries the removal only (six attempts, 0.1 s doubling to a 1.0 s cap), then removes the
# directory, then prunes, and tolerates a removal or prune that still fails. A fake git records the
# order of calls.
CLEANUP = '''"""Remove an evaluation worktree: the git registration, then the directory that held it."""

import shutil
import time


class GitError(Exception):
    pass


def remove_worktree(git, target, parent, rmtree=shutil.rmtree, sleep=time.sleep):
    git("worktree", "prune")
    rmtree(parent, ignore_errors=True)
    git("worktree", "remove", "--force", str(target))
'''

FAKE_GIT = '''
calls, slept = [], []
class Git:
    def __init__(self, fail_removes=0, fail_prune=False):
        self.fail, self.fail_prune = fail_removes, fail_prune
    def __call__(self, *args):
        calls.append(args)
        if args[:2] == ("worktree", "remove") and self.fail > 0:
            self.fail -= 1
            raise cleanup.GitError("Permission denied")
        if args[:2] == ("worktree", "prune") and self.fail_prune:
            raise cleanup.GitError("exit 128")
def rmtree(path, **kw): calls.append(("rmtree", str(path)))
def sleep(seconds): slept.append(seconds)
REMOVE, RMTREE, PRUNE = ("worktree", "remove", "--force", "T"), ("rmtree", "P"), ("worktree", "prune")
def run(git):
    try:
        cleanup.remove_worktree(git, "T", "P", rmtree=rmtree, sleep=sleep)
    except Exception as exc:
        raise AssertionError("remove_worktree must never raise: a failed worktree remove is retried and then tolerated, a failed prune is tolerated; it raised %r after calls %r, sleeps %r" % (exc, calls, slept))
'''

# (d) syberlabs/certificate.py format_report: `syberlabs verify` prints one fixed-label line per
# layer in the order signature, repository, journal (label left-justified to 10 characters, then
# `ok   note`, `skipped  note` or `FAIL code  note` with trailing space stripped), stops at the first
# failing layer, and ends with `verdict    pass`, `verdict    pass (x and y not checked)` or
# `verdict    fail: reason`. Every packet receipt since 2026-10-20 quotes those lines.
REPORT = '''"""Print what verify found: one line per layer, then the verdict."""

LAYERS = ("signature", "repository", "journal")


def format_report(states, notes, reason=None):
    lines = []
    for label in LAYERS:
        lines.append(f"{label}: {states.get(label)} {notes.get(label, '')}")
    lines.append("verdict: " + ("pass" if reason is None else "fail"))
    return lines
'''


def _lines(name: str, expected: list, call: str) -> str:
    """Check body: ``call`` must return exactly ``expected``; the message shows both."""
    return (f"expected = {expected!r}\ngot = {call}\n"
            f"assert got == expected, {name!r} + ': expected lines %r, got %r' % (expected, got)\n")


V3 = {
    "gaps_renumber": {
        "files": {"src/gaps.md": GAPS_FILE, "src/notes.md": GAPS_NOTES, "src/checks.py": GAPS_CHECKS,
                  "src/test_gaps.py": GAPS_TEST},
        "objective": ("Renumber the entries of src/gaps.md so every entry has one number in file order, starting at 1, "
                      "and make every cross reference to a renumbered entry in the other files follow the renumbering; "
                      "nothing else in those files may change"),
        "checks": {
            "sequence": _script("checks", "import re\n"
                                          "text = open('src/gaps.md', encoding='utf-8').read()\n"
                                          "found = re.findall(r'^(\\d+)\\. \\*\\*(.+?)\\*\\*', text, re.M)\n"
                                          f"titles = {GAPS_TITLES!r}\n"
                                          "assert [int(n) for n, _ in found] == [1, 2, 3, 4, 5, 6], 'gaps.md entries must be numbered 1 to 6 in file order (old 4, 5, 7, 8 become 3, 4, 5, 6); got %r' % [n for n, _ in found]\n"
                                          "assert [t for _, t in found] == titles, 'gaps.md titles must stay as they were, in order; got %r' % [t for _, t in found]\n"),
            "references": _script("checks", "text = open('src/notes.md', encoding='utf-8').read()\n"
                                            "for phrase, why in [('(see gaps.md entry 3)', 'old entry 4 is now entry 3'), ('work (entry 5)', 'old entry 7 is now entry 5'),\n"
                                            "                    ('The cap in entry 4 stays', 'old entry 5 is now entry 4')]:\n"
                                            "    assert phrase in text, 'notes.md must read %r (%s); got %r' % (phrase, why, text)\n"
                                            "for phrase in ['Plan step 4 of 7:', '65,536 bytes until 2026-10-24', 'Entry 2 is unchanged', 'entries 1 and 2 keep their numbers']:\n"
                                            "    assert phrase in text, 'notes.md: %r is not a reference to a renumbered entry and must stay exactly as it was; got %r' % (phrase, text)\n"),
            "code": _script("checks", "doc = checks.__doc__ or ''\n"
                                      "assert '(gaps.md, 6)' in doc and '(gaps.md, 8)' not in doc, 'the checks.py docstring cites the setup rule, old entry 8, now entry 6: expected (gaps.md, 6); got %r' % doc\n"
                                      "assert '(gaps.md, 4)' in doc and '(gaps.md, 5)' not in doc, 'the checks.py docstring cites the output cap, old entry 5, now entry 4: expected (gaps.md, 4); got %r' % doc\n"
                                      "assert checks.entry() == 6, 'checks.entry() names the setup rule entry and must return 6 (old 8); got %r' % checks.entry()\n"
                                      "assert checks.gated(['tests', 'setup']) == ['tests'] and checks.SETUP_NAMES == ('setup', 'install'), 'gated and SETUP_NAMES must not change'\n"),
            "testdoc": _script("checks", "import unittest, test_gaps\n"
                                         "doc = test_gaps.__doc__ or ''\n"
                                         "assert '(gaps 3 and 5)' in doc, 'the test_gaps.py docstring cites old entries 4 and 7, now 3 and 5: expected (gaps 3 and 5); got %r' % doc\n"
                                         "src = open('src/test_gaps.py', encoding='utf-8').read()\n"
                                         "for phrase in ['len(steps[0]), 6', 'list(range(7))[4], 4']:\n"
                                         "    assert phrase in src, 'test_gaps.py: %r is test data, not a reference, and must stay exactly as it was; got %r' % (phrase, src)\n"
                                         "assert unittest.main(module=test_gaps, argv=['x'], exit=False).result.wasSuccessful(), 'test_gaps must still pass'\n"),
        },
        "slots": [
            {"path": "src/checks.py", "function": "entry", "check": "code",
             "variants": ["return 8", "return 7", "return 9", "return 5"], "correct": []},
        ],
    },
    "journal_chain": {
        "files": {"src/chain.py": CHAIN},
        "objective": ("Make the journal a verifiable hash chain: an event's hash must be the SHA-256 hex digest of the "
                      "canonical JSON of its envelope, its previous must be the hash of the event before it, the first "
                      "event must chain from a fixed genesis value, and verify must accept the recorded journals and "
                      "reject one whose body, order or links were altered. The recorded journals' hashes are fixed and "
                      "must be reproduced byte for byte; keep the signatures of canonical, event_digest, append and verify"),
        "checks": {
            "canonical": _script("chain", UTF8 + "value = {'b': 1, 'a': [1, {'d': 2, 'c': 'caf\\u00e9'}], 's': 'x y'}\n"
                                          "expected = '{\"a\":[1,{\"c\":\"caf\\u00e9\",\"d\":2}],\"b\":1,\"s\":\"x y\"}'\n"
                                          "got = chain.canonical(value)\n"
                                          "assert got == expected, 'canonical JSON must sort keys at every level, use the separators \",\" and \":\" with no spaces, and keep non-ASCII characters unescaped (ensure_ascii=False); expected %r, got %r' % (expected, got)\n"),
            "genesis": _script("chain", UTF8 + f"events = []\nfirst = chain.append(events, *{CHAIN_EVENTS[0]!r})\n"
                                        "assert first['previous'] == '0' * 64, 'the first event must chain from the genesis value, a string of 64 zero characters; got previous %r' % first['previous']\n"
                                        f"assert first['hash'] == {CHAIN_HASHES[0]!r}, 'the first event hash must be {CHAIN_HASHES[0]}: the SHA-256 hex digest of the canonical JSON of the six fields case_id, seq (counting from 1), kind, body, at, previous, and nothing else; got %r from %r' % (first['hash'], first)\n"),
            "chain": _script("chain", UTF8 + f"import copy\nevents = []\n"
                                      f"for args in {CHAIN_EVENTS!r}:\n    chain.append(events, *args)\n"
                                      f"expected = {CHAIN_HASHES!r}\n"
                                      "for n, (event, want) in enumerate(zip(events, expected), 1):\n"
                                      "    assert event['hash'] == want, 'event %d hash must be %s: the SHA-256 hex digest of the canonical JSON (sorted keys, separators \",\" and \":\", non-ASCII unescaped) of case_id, seq, kind, body, at and previous, where previous is the hash of the event before it; got %r from %r' % (n, want, event['hash'], event)\n"
                                      "assert chain.verify(events) is True, 'verify must accept the recorded journal'\n"
                                      "tampered = copy.deepcopy(events); tampered[1]['body']['value'] = 'tea'\n"
                                      "assert chain.verify(tampered) is False, 'verify must reject a journal whose body was altered, by recomputing every hash'\n"
                                      "swapped = [events[0], events[2], events[1]]\n"
                                      "assert chain.verify(swapped) is False, 'verify must reject a journal whose events were reordered: each previous must equal the hash before it'\n"
                                      "relinked = copy.deepcopy(events); relinked[0]['previous'] = 'f' * 64; relinked[0]['hash'] = chain.event_digest(relinked[0])\n"
                                      "assert chain.verify(relinked) is False, 'verify must reject a first event whose previous is not the genesis value'\n"),
        },
        "slots": [
            {"path": "src/chain.py", "function": "canonical", "check": "canonical",
             "variants": ["return json.dumps(value)", "return json.dumps(value, sort_keys=True)",
                          "return json.dumps(value, separators=(\",\", \":\"))", "return json.dumps(value, ensure_ascii=False)"],
             "correct": []},
        ],
    },
    "worktree_order": {
        "files": {"src/cleanup.py": CLEANUP},
        "objective": ("On Windows a check's child process can still hold a file in the evaluation worktree for a moment "
                      "after the check ends, so git worktree remove fails with Permission denied, a passing check is "
                      "reported as an error, and a stale worktree registration is left behind. Make remove_worktree "
                      "retry the removal briefly before giving up, make sure the registration never outlives the "
                      "directory, tolerate a removal or prune that still fails, and never raise; keep the signature"),
        "checks": {
            "order": _script("cleanup", FAKE_GIT + "run(Git())\n"
                                                   "assert calls == [REMOVE, RMTREE, PRUNE] and slept == [], 'the call order must be worktree remove --force <target>, then rmtree(<parent>), then worktree prune, each once, with no sleep when the removal succeeds; got calls %r, sleeps %r' % (calls, slept)\n"),
            "retry": _script("cleanup", FAKE_GIT + "run(Git(fail_removes=2))\n"
                                                   "assert calls == [REMOVE, REMOVE, REMOVE, RMTREE, PRUNE] and slept == [0.1, 0.2], 'only the removal is retried: after a failed worktree remove, sleep 0.1 s, retry, sleep 0.2 s (doubling each time, capped at 1.0 s), retry; once it succeeds rmtree and prune run once each, with no sleep around them; got calls %r, sleeps %r' % (calls, slept)\n"),
            "tolerated": _script("cleanup", FAKE_GIT + "run(Git(fail_removes=99, fail_prune=True))\n"
                                                       "assert calls == [REMOVE] * 6 + [RMTREE, PRUNE] and slept == [0.1, 0.2, 0.4, 0.8, 1.0], 'a removal that keeps failing is attempted exactly 6 times with sleeps 0.1, 0.2, 0.4, 0.8, 1.0 s between attempts and none after the last; then rmtree and prune run once each and a prune that fails (exit 128) is tolerated, so nothing raises; got calls %r, sleeps %r' % (calls, slept)\n"),
        },
        "slots": [
            {"path": "src/cleanup.py", "function": "remove_worktree", "check": "order",
             "variants": ["git(\"worktree\", \"prune\")", "git(\"worktree\", \"prune\", \"--expire\", \"now\")",
                          "git(\"worktree\", \"remove\", \"--force\", str(target))", "git(\"worktree\", \"list\")"],
             "correct": []},
        ],
    },
    "verify_layers": {
        "files": {"src/report.py": REPORT},
        "objective": ("Make format_report print the verify transcript the operators' log parser already reads: one line "
                      "per layer in the order signature, repository, journal, each starting with the layer's name padded "
                      "to a fixed width and followed by its state and note; a layer that was skipped says so; a failing "
                      "layer names its code and ends the layer lines; the last line is the verdict, naming the reason on "
                      "a failure and the skipped layers on a pass. The parser's recorded transcripts fix the exact "
                      "spacing; keep the signature"),
        "checks": {
            "passing": _script("report", _lines("passing", ["signature  ok   ed25519 4b6ae2deea", "repository ok   trees match", "journal    ok   14 of 19 events certified", "verdict    pass"],
                                                "report.format_report({'signature': 'ok', 'repository': 'ok', 'journal': 'ok'}, {'signature': 'ed25519 4b6ae2deea', 'repository': 'trees match', 'journal': '14 of 19 events certified'})")
                               + "# a layer line is the label left-justified to 10 characters, a space, ok, three spaces, the note; the verdict line is verdict, four spaces, pass\n"),
            "failing": _script("report", _lines("failing", ["signature  ok   ed25519 4b6ae2deea", "repository FAIL tree_mismatch  trees differ", "verdict    fail: tree_mismatch"],
                                                "report.format_report({'signature': 'ok', 'repository': 'tree_mismatch', 'journal': 'ok'}, {'signature': 'ed25519 4b6ae2deea', 'repository': 'trees differ', 'journal': 'unused'}, reason='tree_mismatch')")
                               + _lines("failing (empty note)", ["signature  FAIL signature_invalid", "verdict    fail: signature_invalid"],
                                        "report.format_report({'signature': 'signature_invalid', 'repository': 'ok', 'journal': 'ok'}, {'signature': '', 'repository': 'x', 'journal': 'y'}, reason='signature_invalid')")
                               + "# a failing layer: label padded to 10, space, FAIL, space, the code, two spaces, the note, trailing space stripped; no later layer is printed; the verdict is verdict, four spaces, fail: <reason>\n"),
            "skipped": _script("report", _lines("skipped", ["signature  ok   ed25519 4b6ae2deea", "repository skipped  no --repo", "journal    skipped  no journal under --home", "verdict    pass (repository and journal not checked)"],
                                                "report.format_report({'signature': 'ok', 'repository': None, 'journal': None}, {'signature': 'ed25519 4b6ae2deea', 'repository': 'no --repo', 'journal': 'no journal under --home'})")
                               + _lines("skipped (one layer)", ["signature  ok   k", "repository ok   r", "journal    skipped  j", "verdict    pass (journal not checked)"],
                                        "report.format_report({'signature': 'ok', 'repository': 'ok', 'journal': None}, {'signature': 'k', 'repository': 'r', 'journal': 'j'})")
                               + "# a skipped layer (state None): label padded to 10, space, skipped, two spaces, the note; the verdict names the skipped labels joined by ' and ' in parentheses\n"),
        },
        "slots": [
            {"path": "src/report.py", "function": "format_report", "check": "passing",
             "variants": ["lines = []", "lines = [\"verify\"]", "lines = [\"\"]", "lines = [\"verdict\"]"], "correct": []},
        ],
    },
}

# ---- v4: multipart fixtures, several files and six to eight separable parts each ----------------

# (a) The merge chains of 2026-10-20 and 2026-10-22 (the owner's chain script): one landing touched
# the GAPS closure notes, a packets table, a test docstring and a module constant, each in its own
# convention (merge order in the table and the constant, numeric order in the docstring, 7-character
# ids, the italic dated closure note with "closed." or "closed in part."). The log is the data.
LANDING_LOG = """# Merge chain of 2026-10-22, bottom-up; one line per step, in the order the steps ran.
[02:14:07] #70 syberlabs/17f5d831 merged 5e1c9a0b2f7d4c6e8a1b3d5f7e9c0a2b4d6f8e1a (closes gap 3)
[02:31:52] #63 syberlabs/d9a1cc66 merged 9b7d2e4f6a8c0e1b3d5f7a9c2e4b6d8f0a1c3e5b
[02:49:10] #65 syberlabs/b2e9bf61 merged c3a5e7f9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9a2 (narrows gap 4)
[03:05:44] #64 syberlabs/afc19144 merged 1f3e5d7c9b1a3f5e7d9c1b3a5f7e9d1c3b5a7f9e
[03:22:19] #66 syberlabs/522ca1aa STOP: not clean (expected head 7a9c1e3b)
"""

LANDING_GAPS = """# Known gaps

Each entry names one thing the kit does not do. A closed entry keeps its number and gains a dated note.

1. **The chain is per thread.** A journal's hash chain covers one thread's events, not the store.

2. **A refusal is not an event.** The kernel returns a refusal code; nothing is appended.
    *2026-10-19, closed.* Refusals are recorded as observations (#57).

3. **A flaky test is a timing patch.** Two tests patch time.sleep process-wide.

4. **A credential reaches a check through the contract.** The contract names the variable and the value.

5. **A fixture's difficulty is claimed against a model.** The stand-in cannot fail it by construction.
"""

LANDING_STATUS = """# Packets

| PR | branch | state | commit |
| --- | --- | --- | --- |
| #59 | syberlabs/5e3688b2 | superseded | - |
| #61 | syberlabs/e19f606b | merged | e19c74b |
| #63 | syberlabs/d9a1cc66 | open | - |
| #64 | syberlabs/afc19144 | open | - |
| #65 | syberlabs/b2e9bf61 | open | - |
| #66 | syberlabs/522ca1aa | open | - |
| #70 | syberlabs/17f5d831 | open | - |

Chain head: main at e19c74b (2026-10-21).
"""

LANDING_TEST = '''"""The landing record of the chain of 2026-10-21: #61 merged, #59 superseded."""

import unittest


class Landing(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(len("#66"), 3)
        self.assertEqual("e19c74b"[:2], "e1")
'''

LANDING_RELEASE = '''"""What the last chain landed, for the release note."""

# The chain's date and what it landed, in the order it landed them.
CHAIN = "2026-10-21"
LANDED = (61,)

# The commit main was at when the chain stopped.
HEAD = "e19c74b"


def note():
    return f"chain {CHAIN}: {len(LANDED)} landed, head {HEAD}"
'''

LANDING_ROWS = ["| #59 | syberlabs/5e3688b2 | superseded | - |", "| #61 | syberlabs/e19f606b | merged | e19c74b |",
                "| #70 | syberlabs/17f5d831 | merged | 5e1c9a0 |", "| #63 | syberlabs/d9a1cc66 | merged | 9b7d2e4 |",
                "| #65 | syberlabs/b2e9bf61 | merged | c3a5e7f |", "| #64 | syberlabs/afc19144 | merged | 1f3e5d7 |",
                "| #66 | syberlabs/522ca1aa | open | - |"]

# (b) syberlabs/dsse.py, syberlabs/certificate.py and spec/GAPS.md entries 26 and 28: the envelope's
# pre-authentication encoding and fields, the in-toto statement and its one subject, the unwrap codes,
# the identity block whose issuer names the seed variable and never its value, the target ref the
# acceptance moves, and the three verify layers that stop at the first failure.
CERT_ENVELOPE = '''"""A one-signature DSSE envelope over payload bytes, keyed by a shared secret in this toy."""

import hashlib
import hmac


def pae(payload_type, payload):
    return payload_type.encode("ascii") + b"." + payload


def sign(payload_type, payload, key):
    return {"payloadType": payload_type, "payload": payload.hex(),
            "signatures": [{"sig": hmac.new(key, pae(payload_type, payload), hashlib.sha256).hexdigest()}]}
'''

CERT_STATEMENT = '''"""The statement around a certificate payload, and the way back to the payload."""

STATEMENT_TYPE = "https://in-toto.io/Statement/v0.1"
PREDICATE_TYPE = "sdk.syberlabs.space/certificate"


class Fail(Exception):
    def __init__(self, code, note=""):
        super().__init__(code)
        self.code, self.note = code, note


def wrap(payload):
    return {"_type": STATEMENT_TYPE, "predicateType": PREDICATE_TYPE, "predicate": payload,
            "subject": subject(payload), "version": 1}


def subject(payload):
    return {"name": payload["thread"], "sha1": payload["result"]["commit"]}


def unwrap(statement):
    if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != PREDICATE_TYPE:
        raise Fail("invalid_statement")
    return statement["predicate"]
'''

CERT_PAYLOAD = '''"""The certificate payload: its identity block, the issuer, and the ref it certifies."""

import os

PROTOCOL = "sdk.syberlabs.space/certificate/v0alpha1"
PAYLOAD_TYPE = "application/vnd.syberlabs.admission-certificate+json"


def identity(seed_env):
    return {"protocol": PROTOCOL, "payload_type": PAYLOAD_TYPE, "version": "0",
            "issuer": {"tool": "syberlabs.certificate", "seed": os.environ.get(seed_env, ""), "signing": "ed25519"}}


def target_ref(thread):
    return "syberlabs/" + thread


def build(thread, candidate, commit, tree, seed_env="SYBERLABS_CERTIFICATE_SEED"):
    return {**identity(seed_env), "thread": thread, "result": {"candidate": candidate, "commit": commit, "tree": tree},
            "admission": {"target_ref": target_ref(thread)}}
'''

CERT_LAYERS = '''"""Run verify's layers and report what each found."""

LAYERS = ("signature", "repository", "journal")


def run_layers(signature, repository=None, journal=None):
    states, reason = [], None
    for layer in (repository, journal, signature):
        if layer is None:
            states.append("skipped")
            continue
        code = layer()
        states.append("ok" if code is None else code)
        if code is not None:
            reason = code
    return {"passed": reason is None, "reason": reason, "states": tuple(states)}
'''

CERT_PAYLOAD_VECTOR = {"thread": "8525ff4c-0cf2-4bb0-a556-e2f7029342e1",
                       "result": {"candidate": "c1", "commit": "8dae2e4a00d0d3617d569f6084623616ca2ae3ee",
                                  "tree": "62933f98490b7a2e1c4d5f6a7b8c9d0e1f2a3b4c"},
                       "admission": {"target_ref": "refs/heads/syberlabs/8525ff4c"}}

# (c) syberwork/core.py Work.metric and syberwork/server.py (spec/GAPS.md 29), syberlabs/inspector.py
# (entry 33's incomplete rows) and syberlabs/dashboard.py TONE: the counters' rules, the reading's
# version and ETag, the listing that survives an interrupted thread, the refusal status codes, and the
# dashboard's colour of each state.
CELL_COUNTERS = '''"""Counters a cell reports about itself, counted from its recorded events. Read only."""

METRIC_KEYS = ("cases_open", "proposals_admitted_total", "proposals_denied_total",
               "effects_settled_applied_total", "effects_settled_not_applied_total", "effects_pending")


def cases_open(cases):
    """``cases`` maps a case id to its events; a case's acceptance is the action accept_change."""
    return sum(1 for events in cases.values() if not any(e["kind"] == "case_cancelled" for e in events))


def proposals(events):
    decided = [e["body"]["status"] for e in events if e["kind"] == "decision"]
    return {"proposals_admitted_total": sum(1 for s in decided if s != "denied"),
            "proposals_denied_total": sum(1 for s in decided if s == "denied")}


def effects(events):
    claimed = {e["body"]["proposal_id"] for e in events if e["kind"] == "effect_started"}
    applied = {e["body"]["proposal_id"] for e in events if e["kind"] == "effect_succeeded"}
    rejected = {e["body"]["proposal_id"] for e in events if e["kind"] == "effect_rejected"}
    return {"effects_settled_applied_total": len(applied), "effects_settled_not_applied_total": len(rejected),
            "effects_pending": len(claimed) - len(applied)}
'''

CELL_READING = '''"""One metric as a reading: its value, when it was read, a version, and the headers that carry it."""

import hashlib


def version(key, value, observed_at):
    return hashlib.sha256(f"{key}={value}".encode()).hexdigest()[:12]


def document(key, value, observed_at):
    return {"key": key, "value": value, "observed_at": observed_at, "version": version(key, value, observed_at)}


def headers(doc):
    return {"ETag": "W/" + doc["version"], "Content-Type": "application/json", "Cache-Control": "no-store"}
'''

CELL_ROWS = '''"""Thread rows for the inspector's listing, and the status a refusal is answered with."""


class Refused(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


def listing(threads, open_thread, chain_valid):
    """``threads`` are the kit's rows (id, state); ``open_thread(id)`` is the thread's status or raises Refused."""
    rows = []
    for row in threads:
        status = open_thread(row["id"])
        rows.append({**row, "status": status["status"], "accepted": status["accepted"],
                     "candidates": len(status["candidates"]), "chain_valid": status["chain_valid"]})
    return rows


def response(exc):
    return 500, {"error": "internal_error"}
'''

CELL_TONE = '''"""The dashboard's colour for a state: ok, bad, wait or mute; an unknown state is plain text."""

import html

TONE = {"passed": "ok", "applied": "ok", "promoted": "ok", "complete": "ok", "admitted": "ok",
        "failed": "bad", "timed_out": "bad", "error": "bad", "needs_approval": "bad", "not_applied": "bad",
        "pending": "wait", "denied": "wait", "unresolved": "wait", "stale": "wait", "provisional": "wait",
        "none": "mute", "not_run": "mute", "in_progress": "mute", "cancelled": "mute"}


def tone(state):
    key = str(state or "")
    return f'<span class="{TONE.get(key, "mute")}">{html.escape(key)}</span>'
'''

TONE_EXPECTED = {"passed": "ok", "applied": "ok", "promoted": "ok", "complete": "ok", "admitted": "ok",
                 "failed": "bad", "timed_out": "bad", "error": "bad", "denied": "bad", "not_applied": "bad",
                 "pending": "wait", "needs_approval": "wait", "unresolved": "wait", "stale": "wait", "in_progress": "wait",
                 "none": "mute", "not_run": "mute", "provisional": "mute", "cancelled": "mute"}

# (d) The Build Thread's stations and what each records: syberlabs/build.py status rows, checks.py
# evaluate (sorted order, the setup gate of entry 32), search.py Evaluation.score, evolution.py's
# promotion states, publish.py's destinations and certificate.summary's line.
STATION_REGISTER = '''"""The propose station: the row a registered candidate shows in status."""


def row(candidate, provider):
    return {"id": candidate["id"], "provider": provider["name"], "files": len(candidate["changed_paths"]),
            "in_scope": not candidate["scope_violations"]}
'''

STATION_EVALUATE = '''"""The check station: run a contract's checks in a worktree and record each one."""

import hashlib

SETUP_NAMES = ("setup", "install")


def ordered(checks):
    """The order the checks run in."""
    return list(checks)


def evaluate(checks, run):
    """``checks`` maps names to specs; ``run(name)`` returns the record of one check."""
    results = []
    for name in ordered(checks):
        results.append(run(name))
    return results


def score(results, required):
    return sum(1 for r in results if r["state"] == "passed")
'''

STATION_PROMOTE = '''"""The accept station: a candidate's promotion state and whether it may be accepted."""

RANK = {"denied": 0, "not_applied": 1, "admitted": 2, "needs_approval": 2, "unresolved": 3, "promoted": 4}


def promotion(decision, outcomes):
    """``decision`` is the admission status or None; ``outcomes`` are the effect event kinds of the proposal."""
    if decision == "allowed":
        return "admitted"
    if decision == "denied":
        return "denied"
    return "provisional"


def acceptable(decision, current):
    return decision == "allowed"
'''

STATION_PUBLISH = '''"""The publish station: what an executor says about where it wrote."""


def push_output(remote, ref, commit):
    return {"remote": remote, "ref": ref, "commit": commit}


def pull_request_output(repository, number, url):
    return {"repository": repository, "number": number, "url": url}


def line(output):
    return " ".join(str(v) for v in output.values())
'''

STATION_SUMMARY = '''"""The certify station: the one line certify prints."""


def summary(payload):
    result = payload["result"]
    return f"certified {result['candidate']} of thread {payload['thread']}: result {result['commit']}"
'''

STATION_PAYLOAD = {"thread": "afc19144-275c-45c6-9a49-ff9f24e7bdf3", "act_class": "R",
                   "result": {"candidate": "c1", "commit": "f9674877cf37e09b87e75f1d5f2ca5a296f8ea51",
                              "tree": "62933f9849d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5"},
                   "contract": {"id": "repo-change", "version": 4},
                   "evaluation": {"checks": [{"name": "tests", "state": "passed"}, {"name": "critic.rules", "state": "passed"},
                                             {"name": "scenarios", "state": "passed"}, {"name": "conformance", "state": "passed"}]},
                   "chain": {"head": "9a4f4d1123aa55bb66cc77dd88ee99ff00112233445566778899aabbccddeeff", "count": 14}}
STATION_SUMMARY_LINE = ("certified c1 of thread afc19144: result f9674877cf (tree 62933f9849) on repo-change.v4, class R; "
                        "checks conformance passed, critic.rules passed, scenarios passed, tests passed; chain head 9a4f4d1123 at 14 events")


def _read(path: str) -> str:
    return f"open({path!r}, encoding='utf-8').read()"


V4 = {
    "stack_landing": {
        "files": {"src/chain.log": LANDING_LOG, "src/gaps.md": LANDING_GAPS, "src/status.md": LANDING_STATUS,
                  "src/test_landing.py": LANDING_TEST, "src/release.py": LANDING_RELEASE},
        "objective": ("The merge chain recorded in src/chain.log landed and the other records under src were not brought up "
                      "to date: src/gaps.md does not say which entries the landed pull requests closed or narrowed; the table "
                      "in src/status.md still shows those pull requests as open, in the order before the chain, and its chain "
                      "head line is the old head; the docstring of src/test_landing.py still describes the previous chain; and "
                      "src/release.py still names the previous chain, what it landed and its head. Bring each record into "
                      "agreement with the log in that record's own convention; test data, entry numbers, entry titles, the "
                      "rows of earlier chains and the log itself must not change"),
        "checks": {
            "closed_entry": _script("release", "import re\ntext = " + _read("src/gaps.md") + "\n"
                                               "entries = re.split(r'\\n(?=\\d+\\. \\*\\*)', text)\n"
                                               "third = next((e for e in entries if e.startswith('3. **')), '')\n"
                                               "assert '\\n    *2026-10-22, closed.* ' in third and '#70' in third, 'closed_entry: entry 3 was closed by #70 on 2026-10-22 and must gain, on its own line indented four spaces, the note *2026-10-22, closed.* followed by a sentence naming #70, the form entry 2 already uses; got entry 3 as %r' % third\n"
                                               "assert third.startswith('3. **A flaky test is a timing patch.** Two tests patch time.sleep process-wide.\\n'), 'closed_entry: the title and text of entry 3 must stay as they were; got %r' % third\n"
                                               "assert re.findall(r'^(\\d+)\\. ', text, re.M) == ['1', '2', '3', '4', '5'], 'closed_entry: entries keep their numbers 1 to 5; got %r' % re.findall(r'^(\\d+)\\. ', text, re.M)\n"),
            "narrowed_entry": _script("release", "import re\ntext = " + _read("src/gaps.md") + "\n"
                                                 "entries = re.split(r'\\n(?=\\d+\\. \\*\\*)', text)\n"
                                                 "fourth = next((e for e in entries if e.startswith('4. **')), '')\n"
                                                 "assert '\\n    *2026-10-22, closed in part.* ' in fourth and '#65' in fourth, 'narrowed_entry: entry 4 was narrowed, not closed, by #65 on 2026-10-22: it must gain, on its own line indented four spaces, the note *2026-10-22, closed in part.* followed by a sentence naming #65 (a narrowed entry is closed in part, in the dated italic form entry 2 uses); got entry 4 as %r' % fourth\n"
                                                 "assert fourth.startswith('4. **A credential reaches a check through the contract.** The contract names the variable and the value.\\n'), 'narrowed_entry: the title and text of entry 4 must stay as they were; got %r' % fourth\n"
                                                 "fifth = next((e for e in entries if e.startswith('5. **')), '')\n"
                                                 "assert fifth.strip() == '5. **A fixture\\'s difficulty is claimed against a model.** The stand-in cannot fail it by construction.', 'narrowed_entry: entry 5 was not touched by the chain (#66 did not land) and must stay exactly as it was; got %r' % fifth\n"),
            "table_rows": _script("release", "text = " + _read("src/status.md") + "\n"
                                             "rows = [l for l in text.splitlines() if l.startswith('| #')]\n"
                                             f"expected = {LANDING_ROWS!r}\n"
                                             "assert rows == expected, 'table_rows: the packets table must list the two earlier rows unchanged, then the four pull requests the chain merged as merged with the 7-character id of their merge commit, in the order the chain merged them (the log order, not the number order), then #66 still open; expected rows %r, got %r' % (expected, rows)\n"),
            "head_line": _script("release", "text = " + _read("src/status.md") + "\n"
                                            "assert 'Chain head: main at 1f3e5d7 (2026-10-22).' in text, 'head_line: the chain head line must read exactly Chain head: main at 1f3e5d7 (2026-10-22). (the 7-character id of the last merge the chain made, and the chain date); got %r' % [l for l in text.splitlines() if l.startswith('Chain head')]\n"
                                            "assert 'e19c74b (2026-10-21)' not in text, 'head_line: the old chain head line must not remain'\n"),
            "test_doc": _script("release", "import unittest, test_landing\ndoc = test_landing.__doc__ or ''\n"
                                           "assert doc == 'The landing record of the chain of 2026-10-22: #63, #64, #65 and #70 merged, #66 not landed.', 'test_doc: the module docstring must read exactly: The landing record of the chain of 2026-10-22: #63, #64, #65 and #70 merged, #66 not landed. (the merged pull requests in ascending number order, then the one that did not land); got %r' % doc\n"
                                           "src = " + _read("src/test_landing.py") + "\n"
                                           "for phrase in ['len(\"#66\"), 3', '\"e19c74b\"[:2], \"e1\"']:\n"
                                           "    assert phrase in src, 'test_doc: %r is test data, not a record of the chain, and must stay; got %r' % (phrase, src)\n"
                                           "assert unittest.main(module=test_landing, argv=['x'], exit=False).result.wasSuccessful(), 'test_doc: test_landing must still pass'\n"),
            "landed_order": _script("release", "assert release.CHAIN == '2026-10-22', 'landed_order: release.CHAIN must be the date of the chain in the log, 2026-10-22; got %r' % release.CHAIN\n"
                                               "assert release.LANDED == (70, 63, 65, 64), 'landed_order: release.LANDED must be the tuple of the pull request numbers the chain merged, as integers, in the order the chain merged them (70, 63, 65, 64); got %r' % (release.LANDED,)\n"),
            "head_constant": _script("release", "assert release.HEAD == '1f3e5d7', 'head_constant: release.HEAD must be the 7-character id of the last merge commit the chain made, 1f3e5d7; got %r' % release.HEAD\n"
                                                "assert callable(release.note) and '1f3e5d7' in release.note(), 'head_constant: note() must still exist and name the head; got %r' % release.note()\n"),
        },
        "slots": [
            {"path": "src/release.py", "function": "note", "check": "head_constant",
             "variants": ["return f\"chain {CHAIN}: {len(LANDED)} landed, head {HEAD}\"", "return HEAD", "return CHAIN", "return str(LANDED)"],
             "correct": []},
        ],
    },
    "certificate_layers": {
        "files": {"src/envelope.py": CERT_ENVELOPE, "src/statement.py": CERT_STATEMENT, "src/payload.py": CERT_PAYLOAD,
                  "src/layers.py": CERT_LAYERS},
        "objective": ("The toy certificate pipeline under src does not interoperate with the DSSE and in-toto tools the "
                      "receipts are checked with: a generic DSSE verifier rejects every envelope src/envelope.py signs, "
                      "because neither its pre-authentication encoding nor its field encoding is the standard's; what "
                      "src/statement.py wraps is not an in-toto Statement v1, its subject is not the certified ref with "
                      "its commit and tree, and unwrap answers every defect with one code instead of saying which of the "
                      "statement's type, predicate type or subject is wrong; src/payload.py's identity block does not carry "
                      "the issuer fields the certificate specification names and puts the signing seed's value where the "
                      "verifier expects the variable's name, and its target ref is not the branch a thread's acceptance "
                      "moves; and src/layers.py runs the three layers in the wrong order, does not stop at the first failure, "
                      "and records a skipped layer as a word. Make each module behave as the specification does; keep every "
                      "signature"),
        "checks": {
            "pae": _script("envelope", "got = envelope.pae('application/vnd.in-toto+json', b'{}')\n"
                                       "expected = b'DSSEv1 28 application/vnd.in-toto+json 2 {}'\n"
                                       "assert got == expected, 'pae: the DSSE pre-authentication encoding is DSSEv1, a space, the type length in ASCII decimal, a space, the type, a space, the payload length in ASCII decimal, a space, the payload; expected %r, got %r' % (expected, got)\n"
                                       "assert envelope.pae('t', b'') == b'DSSEv1 1 t 0 ', 'pae: an empty payload encodes as DSSEv1 1 t 0 followed by a space; got %r' % envelope.pae('t', b'')\n"),
            "envelope_fields": _script("envelope", "import base64, hashlib, hmac\n"
                                                   "def b64(text):\n    try:\n        return base64.standard_b64decode(text)\n    except Exception:\n        return None\n"
                                                   "env = envelope.sign('t', b'{\"a\":1}', b'k')\n"
                                                   "assert set(env) == {'payloadType', 'payload', 'signatures'} and env['payloadType'] == 't', 'envelope_fields: an envelope has exactly payloadType, payload and signatures; got %r' % env\n"
                                                   "assert b64(env['payload']) == b'{\"a\":1}', 'envelope_fields: payload is the standard base64 of the payload bytes; got %r' % env['payload']\n"
                                                   "sigs = env['signatures']\n"
                                                   "assert isinstance(sigs, list) and len(sigs) == 1 and set(sigs[0]) == {'keyid', 'sig'}, 'envelope_fields: signatures is a list of one object with exactly keyid and sig; got %r' % sigs\n"
                                                   "assert sigs[0]['keyid'] == b'k'.hex(), 'envelope_fields: keyid is the key, hex-encoded; got %r' % sigs[0]['keyid']\n"
                                                   "want = hmac.new(b'k', envelope.pae('t', b'{\"a\":1}'), hashlib.sha256).digest()\n"
                                                   "assert b64(sigs[0]['sig']) == want, 'envelope_fields: sig is the standard base64 of the HMAC-SHA256 of the pre-authentication encoding under the key; got %r' % sigs[0]['sig']\n"),
            "statement_shape": _script("statement", f"payload = {CERT_PAYLOAD_VECTOR!r}\n"
                                                    "st = statement.wrap(payload)\n"
                                                    "assert st.get('_type') == 'https://in-toto.io/Statement/v1', 'statement_shape: _type must be https://in-toto.io/Statement/v1 (an in-toto Statement v1); got %r' % st.get('_type')\n"
                                                    "assert set(st) == {'_type', 'subject', 'predicateType', 'predicate'}, 'statement_shape: a statement has exactly the four fields _type, subject, predicateType and predicate; got %r' % sorted(st)\n"
                                                    "assert st['predicateType'] == 'https://sdk.syberlabs.space/certificate/v0alpha1', 'statement_shape: predicateType must be https://sdk.syberlabs.space/certificate/v0alpha1; got %r' % st['predicateType']\n"
                                                    "assert st['predicate'] is payload, 'statement_shape: the predicate is the payload object itself, unchanged, so its canonical bytes inside the statement are the bytes the bare form signs; got %r' % (st['predicate'],)\n"),
            "subject": _script("statement", f"payload = {CERT_PAYLOAD_VECTOR!r}\n"
                                            "got = statement.subject(payload)\n"
                                            "expected = [{'name': 'refs/heads/syberlabs/8525ff4c', 'digest': {'gitCommit': payload['result']['commit'], 'gitTree': payload['result']['tree']}}]\n"
                                            "assert got == expected, 'subject: exactly one subject, named by the certified target ref (admission.target_ref), whose digest carries the result commit as gitCommit and the result tree as gitTree; expected %r, got %r' % (expected, got)\n"),
            "unwrap_codes": _script("statement", f"payload = {CERT_PAYLOAD_VECTOR!r}\n"
                                                 "def good():\n    return {'_type': statement.STATEMENT_TYPE, 'subject': statement.subject(payload), 'predicateType': statement.PREDICATE_TYPE, 'predicate': payload}\n"
                                                 "assert statement.unwrap(good()) is payload, 'unwrap_codes: unwrap returns the predicate of a statement whose type, predicate type and subject are right'\n"
                                                 "def code(st):\n    try:\n        statement.unwrap(st)\n    except statement.Fail as exc:\n        return exc.code\n    return None\n"
                                                 "bad_type = good(); bad_type['_type'] = 'https://in-toto.io/Statement/v0.1'\n"
                                                 "assert code(bad_type) == 'statement_type_mismatch', 'unwrap_codes: a wrong _type is refused with Fail(\"statement_type_mismatch\"); got %r' % code(bad_type)\n"
                                                 "bad_pred = good(); bad_pred['predicateType'] = 'x'\n"
                                                 "assert code(bad_pred) == 'predicate_type_mismatch', 'unwrap_codes: a wrong predicateType is refused with Fail(\"predicate_type_mismatch\"); got %r' % code(bad_pred)\n"
                                                 "bad_subject = good(); bad_subject['subject'] = []\n"
                                                 "assert code(bad_subject) == 'subject_mismatch', 'unwrap_codes: a subject that is not subject(predicate) is refused with Fail(\"subject_mismatch\"), compared with the predicate, not with Git; got %r' % code(bad_subject)\n"
                                                 "both = good(); both['_type'] = 'x'; both['subject'] = []\n"
                                                 "assert code(both) == 'statement_type_mismatch', 'unwrap_codes: the type is checked before the predicate type, and both before the subject; got %r' % code(both)\n"),
            "identity": _script("payload", "import json, os\nos.environ['DEMO_SEED'] = 'secret-seed-value-7f3a'\n"
                                           "got = payload.identity('DEMO_SEED')\n"
                                           "expected = {'protocol': 'sdk.syberlabs.space/certificate/v0alpha1', 'payload_type': 'application/vnd.syberlabs.admission-certificate+json', 'version': 0, 'issuer': {'tool': 'syberlabs.certificate/0', 'host': 'syberlabs.host', 'seed_env': 'DEMO_SEED', 'signing': 'in-process'}}\n"
                                           "assert 'secret-seed-value-7f3a' not in json.dumps(got), 'identity: the seed value must never appear in the payload; the issuer records the name of the variable under seed_env'\n"
                                           "assert got == expected, 'identity: the identity block is protocol, payload_type, version 0 (an integer) and issuer {tool syberlabs.certificate/0, host syberlabs.host, seed_env <the variable name>, signing in-process}; expected %r, got %r' % (expected, got)\n"),
            "target_ref": _script("payload", "got = payload.target_ref('8525ff4c-0cf2-4bb0-a556-e2f7029342e1')\n"
                                             "assert got == 'refs/heads/syberlabs/8525ff4c', 'target_ref: the certified ref is the branch the acceptance moves, refs/heads/syberlabs/ followed by the first 8 characters of the thread id; expected %r, got %r' % ('refs/heads/syberlabs/8525ff4c', got)\n"),
            "layer_order": _script("layers", "called = []\n"
                                             "def ok(name):\n    def layer():\n        called.append(name)\n    return layer\n"
                                             "def fail(name, code):\n    def layer():\n        called.append(name)\n        return code\n    return layer\n"
                                             "r = layers.run_layers(ok('s'), ok('r'), ok('j'))\n"
                                             "assert (r['passed'], r['reason'], r['states'], called) == (True, None, ('ok', 'ok', 'ok'), ['s', 'r', 'j']), 'layer_order: the layers run in the order signature, repository, journal and each ok layer records the state ok; got %r after calls %r' % (r, called)\n"
                                             "called.clear(); r = layers.run_layers(ok('s'), fail('r', 'tree_mismatch'), fail('j', 'chain_head_mismatch'))\n"
                                             "assert (r['passed'], r['reason'], r['states'], called) == (False, 'tree_mismatch', ('ok', 'tree_mismatch', None), ['s', 'r']), 'layer_order: the first failing layer stops the run: its code is the reason and the state of its layer, and the layers after it do not run and record None; got %r after calls %r' % (r, called)\n"
                                             "called.clear(); r = layers.run_layers(ok('s'), None, ok('j'))\n"
                                             "assert (r['passed'], r['reason'], r['states'], called) == (True, None, ('ok', None, 'ok'), ['s', 'j']), 'layer_order: a layer given as None is skipped and recorded None, and the layers after it still run; got %r after calls %r' % (r, called)\n"
                                             "called.clear(); r = layers.run_layers(fail('s', 'signature_invalid'), ok('r'), ok('j'))\n"
                                             "assert (r['passed'], r['reason'], r['states'], called) == (False, 'signature_invalid', ('signature_invalid', None, None), ['s']), 'layer_order: a failing signature layer ends the run before the repository and journal layers; got %r after calls %r' % (r, called)\n"),
        },
        "slots": [
            {"path": "src/payload.py", "function": "target_ref", "check": "target_ref",
             "variants": ["return \"syberlabs/\" + thread", "return thread", "return \"refs/heads/\" + thread", "return \"refs/heads/syberlabs/\" + thread"],
             "correct": []},
        ],
    },
    "cell_metrics": {
        "files": {"src/counters.py": CELL_COUNTERS, "src/reading.py": CELL_READING, "src/rows.py": CELL_ROWS, "src/tone.py": CELL_TONE},
        "objective": ("The cell's self-metrics and the inspector's listing under src disagree with the operator service and "
                      "the dashboard: src/counters.py counts a completed case as open, a proposal that still needs approval "
                      "as admitted, and a verified reconciliation as pending; src/reading.py gives two reads of the same value "
                      "the same version and sends a weak ETag; src/rows.py cannot list a journal that holds an interrupted "
                      "thread and answers every refusal with 500; and src/tone.py colours a pending approval as a failure, a "
                      "denial as waiting and a provisional candidate as waiting, and gives an unknown state a colour. Make each "
                      "module agree with the service's and the dashboard's rules; keep every signature"),
        "checks": {
            "cases_open": _script("counters", "cancelled = [{'kind': 'case_created', 'body': {}}, {'kind': 'case_cancelled', 'body': {}}]\n"
                                              "complete = [{'kind': 'case_created', 'body': {}}, {'kind': 'effect_succeeded', 'body': {'action': 'accept_change'}}]\n"
                                              "other = [{'kind': 'case_created', 'body': {}}, {'kind': 'effect_succeeded', 'body': {'action': 'push_branch'}}]\n"
                                              "fresh = [{'kind': 'case_created', 'body': {}}]\n"
                                              "got = counters.cases_open({'a': cancelled, 'b': complete, 'c': other, 'd': fresh})\n"
                                              "assert got == 2, 'cases_open: a case is open when it is neither cancelled (a case_cancelled event) nor complete (its acceptance clause passed: an effect_succeeded for the action accept_change); of a cancelled, a complete, one with another effect and a fresh case, 2 are open; got %r' % got\n"),
            "proposals": _script("counters", "events = [{'kind': 'decision', 'body': {'status': s}} for s in ['allowed', 'needs_approval', 'denied', 'allowed', 'needs_approval']]\n"
                                             "got = counters.proposals(events)\n"
                                             "assert got == {'proposals_admitted_total': 2, 'proposals_denied_total': 1}, 'proposals: the counters count decision events by status, allowed as admitted and denied as denied; needs_approval is neither; expected 2 admitted and 1 denied, got %r' % got\n"),
            "effects": _script("counters", "def ev(kind, pid, **body):\n    return {'kind': kind, 'body': {'proposal_id': pid, **body}}\n"
                                           "events = [ev('effect_started', 'p1'), ev('effect_succeeded', 'p1'),\n"
                                           "          ev('effect_started', 'p2'), ev('effect_rejected', 'p2'),\n"
                                           "          ev('effect_started', 'p3'), ev('reconciled', 'p3', success=True, proof={'verified': True, 'external_id': 'x', 'response_digest': 'd'}),\n"
                                           "          ev('effect_started', 'p4'), ev('reconciled', 'p4', success=True, proof={'verified': False}),\n"
                                           "          ev('effect_started', 'p5')]\n"
                                           "got = counters.effects(events)\n"
                                           "expected = {'effects_settled_applied_total': 2, 'effects_settled_not_applied_total': 1, 'effects_pending': 2}\n"
                                           "assert got == expected, 'effects: every claimed effect (effect_started) is in exactly one of applied (effect_succeeded, or a reconciled whose success is true and whose proof is verified with an external_id and a response_digest), not applied (effect_rejected) or pending (everything else, including an unverified reconciliation); expected %r, got %r' % (expected, got)\n"),
            "version": _script("reading", "import hashlib, json\n"
                                          "got = reading.version('cases_open', 3, 1792651912.25)\n"
                                          "expected = hashlib.sha256(json.dumps({'key': 'cases_open', 'value': 3, 'observed_at': 1792651912.25}, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()\n"
                                          "assert got == expected, 'version: the version is the full SHA-256 hex digest of the canonical JSON (sorted keys, separators \",\" and \":\", non-ASCII unescaped) of {key, value, observed_at}, so a repeated read of the same value carries a new version; expected %r, got %r' % (expected, got)\n"
                                          "assert reading.version('cases_open', 3, 1792651912.25) != reading.version('cases_open', 3, 1792651913.0), 'version: two reads at different times must differ'\n"),
            "etag": _script("reading", "doc = {'key': 'cases_open', 'value': 3, 'observed_at': 1.0, 'version': 'abc123'}\n"
                                       "got = reading.headers(doc)\n"
                                       "assert got.get('ETag') == '\"abc123\"', 'etag: the ETag is the version in double quotes, a strong validator, so an intervention that prefers the ETag header records the quoted form; got %r' % got.get('ETag')\n"
                                       "assert got.get('X-Record-Version') == 'abc123', 'etag: the version is also sent unquoted as X-Record-Version; got %r' % got.get('X-Record-Version')\n"
                                       "assert got.get('Cache-Control') == 'no-store' and got.get('Content-Type') == 'application/json', 'etag: Content-Type application/json and Cache-Control no-store stay; got %r' % got\n"),
            "incomplete_row": _script("rows", "threads = [{'id': 'aaaa1111-x', 'state': 'open'}, {'id': 'bbbb2222-x', 'state': 'incomplete'}]\n"
                                              "opened = []\n"
                                              "def open_thread(tid):\n    opened.append(tid)\n    if tid == 'bbbb2222-x':\n        raise rows.Refused('thread_incomplete', 'start was interrupted')\n    return {'status': 'complete', 'accepted': 'c1', 'candidates': ['c1', 'c2'], 'chain_valid': True}\n"
                                              "try:\n    got = rows.listing(threads, open_thread, lambda tid: tid == 'aaaa1111-x')\nexcept Exception as exc:\n    got = ('raised', repr(exc))\n"
                                              "expected = [{'id': 'aaaa1111-x', 'state': 'open', 'status': 'complete', 'accepted': 'c1', 'candidates': 2, 'chain_valid': True},\n"
                                              "            {'id': 'bbbb2222-x', 'state': 'incomplete', 'status': 'incomplete', 'accepted': None, 'candidates': 0, 'chain_valid': False, 'remedy': 'start again with the same objective, or forget'}]\n"
                                              "assert got == expected and opened == ['aaaa1111-x'], 'incomplete_row: a thread whose state is incomplete is not opened; its row carries status incomplete, accepted None, candidates 0, chain_valid from chain_valid(id) and the remedy \"start again with the same objective, or forget\"; the other rows are unchanged; expected %r, got %r (opened %r)' % (expected, got, opened)\n"),
            "refusal_status": _script("rows", "got = rows.response(rows.Refused('thread_incomplete', 'start was interrupted'))\n"
                                              "assert got == (409, {'error': 'thread_incomplete', 'detail': 'start was interrupted'}), 'refusal_status: a refusal is answered with its code and detail as {error, detail}, status 409; got %r' % (got,)\n"
                                              "got = rows.response(rows.Refused('unknown_thread', 'zzz'))\n"
                                              "assert got == (404, {'error': 'unknown_thread', 'detail': 'zzz'}), 'refusal_status: a refusal whose code starts with unknown is 404; got %r' % (got,)\n"
                                              "got = rows.response(ValueError('boom'))\n"
                                              "assert got == (500, {'error': 'internal_error'}), 'refusal_status: any other exception is 500 {error: internal_error} with no detail; got %r' % (got,)\n"),
            "tone": _script("tone", f"expected = {TONE_EXPECTED!r}\n"
                                    "assert tone.TONE == expected, 'tone: the dashboard colours passed, applied, promoted, complete and admitted ok; failed, timed_out, error, denied and not_applied bad; pending, needs_approval, unresolved, stale and in_progress wait; none, not_run, provisional and cancelled mute; expected %r, got %r' % (expected, tone.TONE)\n"
                                    "assert tone.tone('needs_approval') == '<span class=\"wait\">needs_approval</span>', 'tone: a known state is a span of its colour; got %r' % tone.tone('needs_approval')\n"
                                    "assert tone.tone('<odd>') == '&lt;odd&gt;' and tone.tone(None) == '', 'tone: an unknown state is escaped plain text with no span, and None is empty; got %r and %r' % (tone.tone('<odd>'), tone.tone(None))\n"),
        },
        "slots": [
            {"path": "src/reading.py", "function": "version", "check": "version",
             "variants": ["return hashlib.sha256(f\"{key}={value}\".encode()).hexdigest()[:12]", "return hashlib.sha256(f\"{key}={value}@{observed_at}\".encode()).hexdigest()",
                          "return str(observed_at)", "return hashlib.sha256(repr((key, value, observed_at)).encode()).hexdigest()"],
             "correct": []},
        ],
    },
    "thread_stations": {
        "files": {"src/register.py": STATION_REGISTER, "src/evaluate.py": STATION_EVALUATE, "src/promote.py": STATION_PROMOTE,
                  "src/publish.py": STATION_PUBLISH, "src/summary.py": STATION_SUMMARY},
        "objective": ("The five station records under src disagree with what the Build Thread records: src/register.py's "
                      "candidate row names the provider without its revision and calls a candidate in scope when it only "
                      "broke a size limit; src/evaluate.py runs the checks in the order the contract happened to list them, "
                      "keeps running checks after the setup step failed and records nothing about why they did not run, and "
                      "scores a candidate by counting passes with no regard to which checks were required; src/promote.py's "
                      "promotion state ignores what happened at the destination and its acceptability ignores stale evidence; "
                      "src/publish.py reports a destination in a shape no receipt has quoted; and src/summary.py's certify "
                      "line is not the line the receipts quote. Make each station record what the kit records; keep every "
                      "signature"),
        "checks": {
            "registered": _script("register", "c = {'id': 'c1', 'changed_paths': ('a', 'b', 'c'), 'scope_violations': (), 'limit_violations': ('max_diff_bytes',)}\n"
                                              "got = register.row(c, {'name': 'claude', 'revision': 'fable-5.1'})\n"
                                              "expected = {'id': 'c1', 'provider': 'claude@fable-5.1', 'files': 3, 'in_scope': False}\n"
                                              "assert got == expected, 'registered: the row names the provider as name@revision and is in scope only when the candidate has neither scope violations nor limit violations; expected %r, got %r' % (expected, got)\n"
                                              "ok = register.row({**c, 'limit_violations': ()}, {'name': 'patch', 'revision': '1'})\n"
                                              "assert ok == {'id': 'c1', 'provider': 'patch@1', 'files': 3, 'in_scope': True}, 'registered: a candidate with no violations is in scope; got %r' % ok\n"),
            "check_order": _script("evaluate", "ran = []\n"
                                               "def run(name):\n    ran.append(name)\n    return {'name': name, 'state': 'passed'}\n"
                                               "got = evaluate.evaluate({'tests': {}, 'critic.rules': {}, 'scenarios': {}, 'conformance': {}}, run)\n"
                                               "assert ran == ['conformance', 'critic.rules', 'scenarios', 'tests'] and [r['name'] for r in got] == ran, 'check_order: checks run and are recorded in sorted name order, whatever order the contract lists them in; expected conformance, critic.rules, scenarios, tests, got %r' % ran\n"),
            "setup_gate": _script("evaluate", "import hashlib\nran = []\n"
                                              "def run(name):\n    ran.append(name)\n    return {'name': name, 'state': 'failed' if name == 'setup' else 'passed', 'exit_code': 1 if name == 'setup' else 0, 'duration_ms': 5, 'output_digest': 'x', 'output_tail': 'npm ERR!' if name == 'setup' else ''}\n"
                                              "got = evaluate.evaluate({'setup': {}, 'tests': {}, 'typecheck': {}}, run)\n"
                                              "gated = {'state': 'error', 'exit_code': None, 'duration_ms': 0, 'output_digest': hashlib.sha256(b'').hexdigest(), 'output_tail': 'setup_failed'}\n"
                                              "expected = [run('setup'), {'name': 'tests', **gated}, {'name': 'typecheck', **gated}]\n"
                                              "ran.clear()\n"
                                              "assert got == expected, 'setup_gate: when a check named setup or install does not pass, the checks after it are not started and each is recorded with state error, exit_code None, duration_ms 0, output_digest the SHA-256 of zero bytes and output_tail setup_failed; expected %r, got %r' % (expected, got)\n"
                                              "evaluate.evaluate({'setup': {}, 'tests': {}}, run)\n"
                                              "assert ran == ['setup'], 'setup_gate: the gated checks must not be run at all; ran %r' % ran\n"),
            "score": _script("evaluate", "results = [{'name': 'a', 'state': 'passed'}, {'name': 'b', 'state': 'failed'}, {'name': 'c', 'state': 'passed'}]\n"
                                         "got = evaluate.score(results, ['a', 'b', 'd'])\n"
                                         "assert got == (1, 2), 'score: the score is the pair (required checks passed, all checks passed), where a required check that did not run is not a pass; expected (1, 2), got %r' % (got,)\n"
                                         "assert evaluate.score([], ['a']) == (0, 0), 'score: no results is (0, 0); got %r' % (evaluate.score([], ['a']),)\n"),
            "promotion_state": _script("promote", "cases = [(None, [], 'provisional'), ('allowed', [], 'admitted'), ('needs_approval', [], 'needs_approval'), ('denied', [], 'denied'),\n"
                                                  "         ('allowed', ['effect_started', 'effect_succeeded'], 'promoted'), ('allowed', ['effect_started', 'reconciled'], 'promoted'),\n"
                                                  "         ('allowed', ['effect_started', 'effect_rejected'], 'not_applied'), ('allowed', ['effect_started'], 'unresolved'), ('allowed', ['effect_unknown'], 'unresolved')]\n"
                                                  "for decision, outcomes, want in cases:\n"
                                                  "    got = promote.promotion(decision, outcomes)\n"
                                                  "    assert got == want, 'promotion_state: an outcome at the destination outranks the decision: effect_succeeded or a reconciled outcome is promoted, effect_rejected is not_applied, any other effect event is unresolved; with no effect the decision gives admitted (allowed), needs_approval or denied, and no decision is provisional; for decision %r and outcomes %r expected %r, got %r' % (decision, outcomes, want, got)\n"
                                                  "assert promote.RANK == {'denied': 0, 'not_applied': 1, 'admitted': 2, 'needs_approval': 2, 'unresolved': 3, 'promoted': 4}, 'promotion_state: RANK must not change'\n"),
            "acceptable": _script("promote", "for decision, current, want in [('allowed', True, True), ('needs_approval', True, True), ('denied', True, False), ('allowed', False, False), ('needs_approval', False, False), (None, True, False)]:\n"
                                             "    got = promote.acceptable(decision, current)\n"
                                             "    assert got is want, 'acceptable: a candidate is acceptable when its decision is allowed or needs_approval and its evidence is current (a stale evaluation is never acceptable); for %r, current=%r expected %r, got %r' % (decision, current, want, got)\n"),
            "publish_lines": _script("publish", "sha = 'c0e3e89b08168a0c3c5ba2e88339bd9ea73dff69'\n"
                                                "push = publish.push_output('origin', 'refs/heads/syberlabs/9986c840', sha)\n"
                                                "assert push == {'destination': 'git:origin:refs/heads/syberlabs/9986c840', 'external_id': sha}, 'publish_lines: a push reports destination git:<remote>:<ref> and external_id the commit; got %r' % push\n"
                                                "pr = publish.pull_request_output('SyberLabs/SyberWork', 74, 'https://github.com/SyberLabs/SyberWork/pull/74')\n"
                                                "assert pr == {'destination': 'github:SyberLabs/SyberWork', 'external_id': '74', 'url': 'https://github.com/SyberLabs/SyberWork/pull/74'}, 'publish_lines: a pull request reports destination github:<repository>, external_id the number as a string, and url; got %r' % pr\n"
                                                "assert publish.line(push) == 'git:origin:refs/heads/syberlabs/9986c840: ' + sha, 'publish_lines: the line is the destination, a colon, a space, the external id; got %r' % publish.line(push)\n"
                                                "assert publish.line(pr) == 'github:SyberLabs/SyberWork: 74 https://github.com/SyberLabs/SyberWork/pull/74', 'publish_lines: with a url the line continues with a space and the url; got %r' % publish.line(pr)\n"),
            "certify_summary": _script("summary", f"payload = {STATION_PAYLOAD!r}\n"
                                                  f"expected = {STATION_SUMMARY_LINE!r}\n"
                                                  "got = summary.summary(payload)\n"
                                                  "assert got == expected, 'certify_summary: the certify line is: certified <candidate> of thread <first 8 of thread>: result <first 10 of commit> (tree <first 10 of tree>) on <contract id>.v<version>, class <act_class>; checks <name state, ... in name order>; chain head <first 10 of head> at <count> events; expected %r, got %r' % (expected, got)\n"
                                                  "noclass = summary.summary({**payload, 'act_class': None})\n"
                                                  "assert ', class ' not in noclass and noclass.startswith('certified c1 of thread afc19144: result f9674877cf (tree 62933f9849) on repo-change.v4; checks'), 'certify_summary: without an act class the class clause is omitted; got %r' % noclass\n"),
        },
        "slots": [
            {"path": "src/summary.py", "function": "summary", "check": "certify_summary",
             "variants": ["result = payload[\"result\"]", "result = payload", "result = payload[\"chain\"]", "result = payload[\"contract\"]"],
             "correct": []},
        ],
    },
}

FIXTURES.update(V2)
FIXTURES.update(V3)
FIXTURES.update(V4)


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
