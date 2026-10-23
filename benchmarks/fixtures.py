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

FIXTURES.update(V2)
FIXTURES.update(V3)


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
