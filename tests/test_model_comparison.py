"""The model adapter seam, cost accounting, the cost-matched comparison harness (simulated model only), and the v2, v3 and v4 fixtures."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from adapters import anthropic_adapter as adapter
from benchmarks import model_vs_patch as harness
from benchmarks.fixtures import FIXTURES, V2, V3, V4
from syberlabs.evolve import CommandMutator, EvolutionaryProvider
from syberlabs.providers import CommandProvider, model_usage
from tests.test_build_thread import GOOD, RepoCase

USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "model": "claude-opus-5", "stop_reason": "end_turn"}


class Adapter(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "report.py").write_text("def rows():\n    return []\n")

    def test_prompts_carry_objective_scope_and_files(self):
        prompt = adapter.build_prompt({"protocol": "syberlabs.search/v0alpha1", "objective": "Add CSV", "scope": ["src"],
                                       "context": [{"path": "src/report.py"}], "files": ["src/report.py", "../outside.txt"]},
                                      self.root)
        self.assertIn("Objective: Add CSV", prompt)
        self.assertIn('<file path="src/report.py">', prompt)
        self.assertNotIn("outside", prompt, "files outside the checkout are never read")
        repair = adapter.build_prompt({"protocol": "syberlabs.search/v0alpha1", "objective": "x", "scope": ["src"],
                                       "feedback": {"files": {"src/a.py": "A"}, "checks": [
                                           {"name": "tests", "state": "failed", "output_tail": "AssertionError: 1 != 2"},
                                           {"name": "lint", "state": "passed", "output_tail": ""}]}}, self.root)
        self.assertIn("AssertionError: 1 != 2", repair)
        self.assertNotIn("--- lint", repair)
        mutate = adapter.build_prompt({"protocol": "syberlabs.mutate/v0alpha1", "objective": "x", "scope": ["src"],
                                       "parent": "c3", "seed": 9, "files": {"src/a.py": "A"}}, self.root)
        self.assertIn("mutation of this candidate", mutate)
        with self.assertRaises(ValueError):
            adapter.build_prompt({"protocol": "other"}, self.root)

    def test_responses_follow_each_protocol_and_report_usage(self):
        parsed = {"message": "csv", "rationale": "reuse rows()",
                  "files": [{"path": "src/export.py", "content": "x", "delete": False},
                            {"path": "src/old.py", "content": "", "delete": True}]}
        search = adapter.respond({"protocol": "syberlabs.search/v0alpha1", "objective": "x"}, self.root, lambda p: (parsed, USAGE))
        self.assertEqual(search["candidates"][0]["changes"], {"src/export.py": "x", "src/old.py": None})
        self.assertEqual((search["recommended"], search["usage"]), ([0], USAGE))
        mutate = adapter.respond({"protocol": "syberlabs.mutate/v0alpha1", "files": {}}, self.root, lambda p: (parsed, USAGE))
        self.assertEqual(set(mutate), {"changes", "usage"})
        refused = adapter.respond({"protocol": "syberlabs.search/v0alpha1"}, self.root,
                                  lambda p: (None, {**USAGE, "stop_reason": "refusal"}))
        self.assertEqual((refused["candidates"], refused["usage"]["stop_reason"]), ([], "refusal"))

    def test_schema_is_strict(self):
        self.assertFalse(adapter.SCHEMA["additionalProperties"])
        self.assertFalse(adapter.SCHEMA["properties"]["files"]["items"]["additionalProperties"])


class Accounting(RepoCase):
    def test_usage_is_filtered_and_recorded_as_a_signal(self):
        self.assertEqual(model_usage({"usage": {"input_tokens": 5, "output_tokens": -1, "secret": "x", "model": "m"}}),
                         {"input_tokens": 5, "model": "m"})
        self.assertIsNone(model_usage({"usage": "lots"}))
        script = self.root.parent / "model.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            json.load(sys.stdin)
            print(json.dumps({{"candidates": [{{"changes": {json.dumps(GOOD)}, "message": "m"}}], "recommended": [0],
                              "usage": {json.dumps(USAGE)}}}))
            '''))
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        provider = CommandProvider([sys.executable, str(script)], name="model", revision="1")
        [candidate] = thread.propose(provider)
        self.assertEqual((provider.calls, provider.usage), (1, [USAGE]))
        self.assertEqual(candidate.signal["model_usage"]["output_tokens"], 300)

    def test_mutator_call_cap_and_duplicate_refusal(self):
        script = self.root.parent / "mutator.py"
        script.write_text("import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'changes': {'src/n.py': 'x = 1\\n'}}))\n")
        kit = self.kit(self.contract(**{"evolution.operators": ["patch", "mutation", "crossover"]}))
        thread = kit.start("Add a CSV export")
        mutator = CommandMutator([sys.executable, str(script)], max_calls=3)
        found = thread.propose(EvolutionaryProvider(mutator, population=3, generations=5), seed=1)
        self.assertEqual(mutator.calls, 3)
        self.assertTrue(mutator.exhausted)
        self.assertEqual(len(found), 1, "identical results are refused as duplicates, so only one is recorded")
        self.assertEqual(thread.propose(changes={"src/n.py": "x = 1\n"}), [])
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual((search["stopped"], search["error"]), ("error", "duplicate"))

    def test_dollars_use_the_price_table(self):
        self.assertAlmostEqual(harness.dollars([USAGE]), (1200 * 5 + 300 * 25) / 1e6)
        self.assertAlmostEqual(harness.dollars([{**USAGE, "cache_read_input_tokens": 1000}]), (1200 * 5 + 300 * 25 + 1000 * 0.5) / 1e6)
        self.assertIsNone(harness.dollars([{**USAGE, "model": "unknown-model"}]))


class Harness(unittest.TestCase):
    def test_every_arm_respects_the_call_budget_and_is_labelled_simulated(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [harness.run("slugify", arm, harness.SIMULATED, 3, seed, Path(folder)) for arm in harness.ARMS for seed in (1, 2)]
            again = harness.run("slugify", "repair", harness.SIMULATED, 3, 1, Path(folder) / "again")
        for row in rows:
            self.assertLessEqual(row["calls"], 1 if row["arm"] == "single" else 3)
            self.assertTrue(row["simulated"])
            self.assertIsNone(row["dollars"])
            self.assertGreater(row["input_tokens"], 0)
        first = next(r for r in rows if r["arm"] == "repair" and r["seed"] == 1)
        self.assertEqual({k: again[k] for k in ("solved", "calls", "candidates")}, {k: first[k] for k in ("solved", "calls", "candidates")})
        report = harness.summarize(rows, 3)
        self.assertTrue(report.startswith("SIMULATED MODEL"))
        self.assertIn("best_of_k", report)


REFERENCE_FIXES = {
    "crlf_export": {"src/fields.py": '''"""Field report export: render a table and compare it with the committed export."""


def render(rows):
    return "\\n".join(rows)


def load(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8").replace("\\r\\n", "\\n")


def lines(path):
    return load(path).splitlines()


def matches(path, rows):
    return render(rows) in load(path)
'''},
    "oauth_state": {"src/oauth.py": '''"""OAuth connect flow: the state round trip."""

import random
import string

ALPHABET = string.ascii_lowercase + string.digits


def make_state(seed):
    rng = random.Random(seed)
    return "".join(rng.choice(ALPHABET) for _ in range(24))


def tamper(state):
    last = "y" if state[-1] == "x" else "x"
    return state[:-1] + last


def finish_connect(expected, returned):
    return returned == expected
'''},
    "settlement": {"src/ledger.py": '''"""Settle a reading against a target, durably, as a settlement record and then a verdict."""

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
    if not found:
        settle(path, reading, target)
        return True
    last = found[-1]
    if last["kind"] == "settlement":
        append(path, {"kind": "verdict", "verdict": verdict(last["reading"], last["target"])})
        return True
    return False
'''},
    "tool_path": {"src/launch.py": '''"""Launch a repository's tool by name from a list of directories."""

import os
import subprocess

SUFFIXES = ("", ".cmd", ".bat", ".exe")


class ToolNotFound(Exception):
    pass


def resolve(name, dirs):
    for folder in dirs:
        for suffix in SUFFIXES:
            candidate = os.path.join(folder, name + suffix)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    raise ToolNotFound(name)


def run_tool(name, args, dirs):
    done = subprocess.run([resolve(name, dirs), *args], capture_output=True, text=True,
                          env={**os.environ, "PATH": os.pathsep.join(dirs)})
    return done.stdout.strip()
'''},
}


def materialise(name: str, root: Path, fix: dict | None = None) -> None:
    for path, text in FIXTURES[name]["files"].items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8", newline="\n")
    for path, text in (fix or {}).items():
        (root / path).write_text(text, encoding="utf-8", newline="\n")


def check_outputs(name: str, root: Path) -> dict:
    """Run a fixture's check commands in ``root`` the way the kit does: argv, no shell, worktree as cwd.

    Returns ``{check: (state, last line of stderr)}``; the last line is the assertion message a repair arm reads."""
    out = {}
    for check, spec in FIXTURES[name]["checks"].items():
        done = subprocess.run(spec["argv"], cwd=root, capture_output=True, timeout=spec["timeout_seconds"])
        tail = done.stderr.decode("utf-8", errors="replace").strip().splitlines()
        out[check] = ("passed" if done.returncode == 0 else "failed", tail[-1] if tail else "")
    return out


def check_states(name: str, root: Path) -> dict:
    return {check: state for check, (state, _) in check_outputs(name, root).items()}


class FixturesV2(unittest.TestCase):
    """The four fixtures built from this program's own defects (docs/E3_BENCHMARK_PLAN.md, Fixtures v2)."""

    def test_the_four_fixtures_exist_with_three_checks_and_no_correct_slot(self):
        self.assertEqual(sorted(V2), ["crlf_export", "oauth_state", "settlement", "tool_path"])
        for name in V2:
            self.assertEqual(len(FIXTURES[name]["checks"]), 3, name)
            self.assertEqual(sorted(harness.contract(name)["evolution"]["evaluation"]["required"]), sorted(FIXTURES[name]["checks"]))
            for slot in FIXTURES[name]["slots"]:
                self.assertEqual(slot["correct"], [], f"{name}: a v2 slot must hold no correct variant")
        self.assertIsInstance(FIXTURES["crlf_export"]["files"]["src/export.txt"], bytes)
        self.assertIn(b"\r\n", FIXTURES["crlf_export"]["files"]["src/export.txt"])

    def test_every_check_fails_on_the_broken_source_and_passes_on_the_reference_fix(self):
        for name in V2:
            with tempfile.TemporaryDirectory() as folder:
                broken, fixed = Path(folder) / "broken", Path(folder) / "fixed"
                broken.mkdir()
                fixed.mkdir()
                materialise(name, broken)
                materialise(name, fixed, REFERENCE_FIXES[name])
                before, after = check_states(name, broken), check_states(name, fixed)
            self.assertEqual(set(before.values()), {"failed"}, (name, before))
            self.assertEqual(set(after.values()), {"passed"}, (name, after))

    def test_the_flaky_fixture_has_a_seed_where_the_tampered_state_is_the_real_one(self):
        """One state in 36 ends in "x" (the alphabet is 36 characters), so the no-op tamper is deterministic in the harness."""
        import random
        import string
        alphabet = string.ascii_lowercase + string.digits
        ending_in_x = [seed for seed in range(1, 201)
                       if "".join(random.Random(seed).choice(alphabet) for _ in range(24)).endswith("x")]
        self.assertTrue(ending_in_x, "no seed in 1..200 ends in x; the fixture would not flake")


class HarnessV2(unittest.TestCase):
    def test_single_does_not_solve_the_v2_fixtures_with_the_simulated_model_and_journals_sit_beside_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            journals = Path(folder) / "out-journals"
            rows = [harness.run(name, "single", harness.SIMULATED, 6, 1, Path(folder), journals) for name in V2]
            for row in rows:
                self.assertFalse(row["solved"], row)
                self.assertEqual((row["calls"], row["candidates"], row["model"], row["provider_error"]),
                                 (1, 1, "simulated", None), row)
                self.assertEqual(row["journal"], f"out-journals/{row['fixture']}-single-1.json")
                exported = json.loads((Path(folder) / row["journal"]).read_text(encoding="utf-8"))
                self.assertTrue(exported["chain_valid"])
                evaluated = [e for e in exported["events"] if e["kind"] == "candidate_evaluated"]
                self.assertEqual(len(evaluated), 1)
                self.assertIn("failed", {c["state"] for c in evaluated[0]["body"]["checks"]}, row["fixture"])
            without = harness.run("tool_path", "single", harness.SIMULATED, 6, 2, Path(folder) / "none")
        self.assertIsNone(without["journal"], "no --write, no journal")
        report = harness.summarize(rows, 6)
        self.assertIn("simulated", report)
        self.assertIn("provider_error", report.splitlines()[1])
        for name in V2:
            self.assertRegex(report, rf"{name}\s+single\s+0/1")

    def test_rows_name_the_served_model_and_the_summary_shows_provider_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing_adapter.py"
            row = harness.run("pricing", "single", f"{sys.executable} {missing}", 3, 1, Path(folder))
        self.assertEqual((row["provider_error"], row["model"], row["candidates"], row["solved"]), ("provider_failed", None, 0, False))
        served = {**row, "seed": 2, "provider_error": None, "model": "claude-sonnet-5-5", "simulated": False,
                  "input_tokens": 700, "output_tokens": 300, "dollars": 0.0044, "solved": True, "candidates": 1}
        report = harness.summarize([row, served], 3)
        line = next(l for l in report.splitlines() if l.startswith("pricing"))
        self.assertIn("claude-sonnet-5-5", line)
        self.assertIn("1/2 provider_failed", line)
        clean = harness.summarize([served], 3)
        self.assertIn("none", next(l for l in clean.splitlines() if l.startswith("pricing")))


# ---- v3 ------------------------------------------------------------------------------------------

def _renumbered(text: str, *pairs: tuple[str, str]) -> str:
    for old, new in pairs:
        text = text.replace(old, new)
    return text


REFERENCE_FIXES_V3 = {
    "gaps_renumber": {
        "src/gaps.md": _renumbered(FIXTURES["gaps_renumber"]["files"]["src/gaps.md"],
                                   ("\n4. **", "\n3. **"), ("\n5. **", "\n4. **"), ("\n7. **", "\n5. **"), ("\n8. **", "\n6. **")),
        "src/notes.md": _renumbered(FIXTURES["gaps_renumber"]["files"]["src/notes.md"],
                                    ("(see gaps.md entry 4)", "(see gaps.md entry 3)"), ("work (entry 7)", "work (entry 5)"),
                                    ("The cap in entry 5 stays", "The cap in entry 4 stays")),
        "src/checks.py": _renumbered(FIXTURES["gaps_renumber"]["files"]["src/checks.py"],
                                     ("(gaps.md, 8)", "(gaps.md, 6)"), ("(gaps.md, 5)", "(gaps.md, 4)"), ("    return 8\n", "    return 6\n")),
        "src/test_gaps.py": _renumbered(FIXTURES["gaps_renumber"]["files"]["src/test_gaps.py"], ("(gaps 4 and 7)", "(gaps 3 and 5)")),
    },
    "journal_chain": {"src/chain.py": '''"""An append-only event journal whose events form a hash chain."""

import hashlib
import json

GENESIS = "0" * 64
FIELDS = ("case_id", "seq", "kind", "body", "at", "previous")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_digest(event):
    return hashlib.sha256(canonical({k: event[k] for k in FIELDS}).encode("utf-8")).hexdigest()


def append(events, case_id, kind, body, at):
    event = {"case_id": case_id, "seq": len(events) + 1, "kind": kind, "body": body, "at": at,
             "previous": events[-1]["hash"] if events else GENESIS}
    event["hash"] = event_digest(event)
    events.append(event)
    return event


def verify(events):
    previous = GENESIS
    for event in events:
        if event.get("previous") != previous or event.get("hash") != event_digest(event):
            return False
        previous = event["hash"]
    return True
'''},
    "worktree_order": {"src/cleanup.py": '''"""Remove an evaluation worktree: the git registration, then the directory that held it."""

import shutil
import time


class GitError(Exception):
    pass


def remove_worktree(git, target, parent, rmtree=shutil.rmtree, sleep=time.sleep, attempts=6):
    delay = 0.1
    for attempt in range(attempts):
        try:
            git("worktree", "remove", "--force", str(target))
            break
        except GitError:
            if attempt + 1 == attempts:
                break
            sleep(delay)
            delay = min(delay * 2, 1.0)
    rmtree(parent, ignore_errors=True)
    try:
        git("worktree", "prune")
    except GitError:
        pass
'''},
    "verify_layers": {"src/report.py": '''"""Print what verify found: one line per layer, then the verdict."""

LAYERS = ("signature", "repository", "journal")


def format_report(states, notes, reason=None):
    lines, skipped = [], []
    for label in LAYERS:
        state, note = states.get(label), notes.get(label, "")
        if state is None:
            lines.append(f"{label:<10} skipped  {note}")
            skipped.append(label)
        elif state == "ok":
            lines.append(f"{label:<10} ok   {note}")
        else:
            lines.append(f"{label:<10} FAIL {state}  {note}".rstrip())
            break
    if reason is None:
        lines.append("verdict    pass" + (f" ({' and '.join(skipped)} not checked)" if skipped else ""))
    else:
        lines.append(f"verdict    fail: {reason}")
    return lines
'''},
}

# The most plausible wrong reading of each v3 objective: the files it produces, the part that must fail,
# and a phrase the failing check's message must contain so a second attempt can converge from it.
WRONG_READINGS_V3 = {
    # every old number replaced by its new one, textually, in every file
    "gaps_renumber": ({path: _renumbered(text, ("4", "3"), ("5", "4"), ("7", "5"), ("8", "6"))
                       for path, text in FIXTURES["gaps_renumber"]["files"].items()},
                      "references", "is not a reference to a renumbered entry and must stay exactly as it was"),
    # sorted keys with json's default separators and escapes, and no genesis value
    "journal_chain": ({"src/chain.py": _renumbered(REFERENCE_FIXES_V3["journal_chain"]["src/chain.py"],
                                                   (', separators=(",", ":"), ensure_ascii=False', ""), ('GENESIS = "0" * 64', "GENESIS = None"))},
                      "canonical", 'use the separators "," and ":" with no spaces'),
    # the right order, but the retry wrapped around all three operations: when the removal keeps failing
    # the directory is never removed and the registration never pruned (the #59 defect itself)
    "worktree_order": ({"src/cleanup.py": _renumbered(REFERENCE_FIXES_V3["worktree_order"]["src/cleanup.py"],
                                                      ('            git("worktree", "remove", "--force", str(target))\n            break\n',
                                                       '            git("worktree", "remove", "--force", str(target))\n            rmtree(parent, ignore_errors=True)\n'
                                                       '            git("worktree", "prune")\n            return\n'),
                                                      ('            if attempt + 1 == attempts:\n                break\n',
                                                       '            if attempt + 1 == attempts:\n                return\n'),
                                                      ('    rmtree(parent, ignore_errors=True)\n    try:\n        git("worktree", "prune")\n    except GitError:\n        pass\n', ""))},
                       "tolerated", "then rmtree and prune run once each"),
    # the right words with single spaces, and every layer printed after a failure
    "verify_layers": ({"src/report.py": _renumbered(REFERENCE_FIXES_V3["verify_layers"]["src/report.py"],
                                                    ('skipped  {note}', 'skipped {note}'), ('ok   {note}', 'ok {note}'),
                                                    ('FAIL {state}  {note}', 'FAIL {state} {note}'), ('            break\n', ""),
                                                    ('"verdict    pass"', '"verdict pass"'), ('f"verdict    fail: {reason}"', 'f"verdict fail: {reason}"'))},
                      "passing", "expected lines"),
}


class FixturesV3(unittest.TestCase):
    """The four fixtures whose checks pin conventions the visible files do not determine (docs/MODEL_COMPARISON.md, v3)."""

    def test_the_four_fixtures_exist_with_several_checks_and_no_correct_slot(self):
        self.assertEqual(sorted(V3), ["gaps_renumber", "journal_chain", "verify_layers", "worktree_order"])
        for name in V3:
            self.assertGreaterEqual(len(FIXTURES[name]["checks"]), 3, name)
            self.assertEqual(sorted(harness.contract(name)["evolution"]["evaluation"]["required"]), sorted(FIXTURES[name]["checks"]))
            self.assertTrue(all(path.startswith("src/") for path in FIXTURES[name]["files"]), name)
            for slot in FIXTURES[name]["slots"]:
                self.assertEqual(slot["correct"], [], f"{name}: a v3 slot must hold no correct variant")
                self.assertIn(slot["check"], FIXTURES[name]["checks"])
        self.assertEqual(len(FIXTURES["gaps_renumber"]["checks"]), 4)

    def test_every_check_fails_on_the_broken_source_and_passes_on_the_reference_fix(self):
        for name in V3:
            with tempfile.TemporaryDirectory() as folder:
                broken, fixed = Path(folder) / "broken", Path(folder) / "fixed"
                broken.mkdir()
                fixed.mkdir()
                materialise(name, broken)
                materialise(name, fixed, REFERENCE_FIXES_V3[name])
                before, after = check_outputs(name, broken), check_outputs(name, fixed)
            self.assertEqual({state for state, _ in before.values()}, {"failed"}, (name, before))
            self.assertEqual({state for state, _ in after.values()}, {"passed"}, (name, after))
            for check, (_, message) in before.items():
                self.assertTrue(message.startswith("AssertionError: ") and len(message) > 40,
                                f"{name}/{check}: a failing check must end with an assertion message that names the expectation; got {message!r}")

    def test_the_plausible_wrong_reading_fails_the_named_part_with_a_message_naming_the_expectation(self):
        for name, (files, part, phrase) in WRONG_READINGS_V3.items():
            with tempfile.TemporaryDirectory() as folder:
                materialise(name, Path(folder), files)
                found = check_outputs(name, Path(folder))
            state, message = found[part]
            self.assertEqual(state, "failed", (name, part, found))
            self.assertIn(phrase, message, (name, part, message))

    def test_the_chain_vectors_and_the_verify_lines_are_the_kits_own(self):
        """The pinned conventions are this program's: the hashes come from syberlabs.events, the lines from syberlabs.certificate."""
        from benchmarks.fixtures import CHAIN_EVENTS, CHAIN_HASHES
        from syberlabs import certificate
        from syberlabs.events import GENESIS, event_digest
        previous, hashes = GENESIS, []
        for seq, (case_id, kind, body, at) in enumerate(CHAIN_EVENTS, 1):
            previous = event_digest({"case_id": case_id, "seq": seq, "kind": kind, "body": body, "at": at, "previous": previous})
            hashes.append(previous)
        self.assertEqual(hashes, CHAIN_HASHES)
        report = certificate.Report(payload={}, keyid="", passed=False, reason="tree_mismatch", signature="ok", repository="tree_mismatch", journal=None,
                                    notes=["ed25519 4b6ae2deea", "trees differ", ""])
        self.assertEqual(certificate.format_report(report),
                         ["signature  ok   ed25519 4b6ae2deea", "repository FAIL tree_mismatch  trees differ", "verdict    fail: tree_mismatch"])
        skipped = certificate.Report(payload={}, keyid="", passed=True, reason=None, signature="ok", repository=None, journal=None,
                                     notes=["ed25519 4b6ae2deea", "no --repo", "no journal under --home"])
        self.assertEqual(certificate.format_report(skipped)[-1], "verdict    pass (repository and journal not checked)")


class HarnessV3(unittest.TestCase):
    def test_single_does_not_solve_the_v3_fixtures_with_the_simulated_model(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [harness.run(name, "single", harness.SIMULATED, 6, 1, Path(folder)) for name in V3]
        for row in rows:
            self.assertFalse(row["solved"], row)
            self.assertEqual((row["calls"], row["candidates"], row["evaluations"], row["model"], row["provider_error"]),
                             (1, 1, 1, "simulated", None), row)
        report = harness.summarize(rows, 6)
        for name in V3:
            self.assertRegex(report, rf"{name}\s+single\s+0/1")



# ---- v4 ------------------------------------------------------------------------------------------

# Each v4 part's reference fix is a list of (path, old text, new text) replacements on the broken tree,
# so the halves, their union and a wrong reading are compositions of parts, not hand-written trees.
# The regions of two parts never overlap (the oracle asserts it), which is what lets two children that
# fixed disjoint parts recombine.
PARTS_V4 = {
    "stack_landing": {
        "closed_entry": [("src/gaps.md",
                          "3. **A flaky test is a timing patch.** Two tests patch time.sleep process-wide.\n",
                          "3. **A flaky test is a timing patch.** Two tests patch time.sleep process-wide.\n"
                          "    *2026-10-22, closed.* The two patches are narrowed to the module under test (#70).\n")],
        "narrowed_entry": [("src/gaps.md",
                            "4. **A credential reaches a check through the contract.** The contract names the variable and the value.\n",
                            "4. **A credential reaches a check through the contract.** The contract names the variable and the value.\n"
                            "    *2026-10-22, closed in part.* The host passes a named credential; the contract still names the variable (#65).\n")],
        "table_rows": [("src/status.md",
                        "| #63 | syberlabs/d9a1cc66 | open | - |\n| #64 | syberlabs/afc19144 | open | - |\n| #65 | syberlabs/b2e9bf61 | open | - |\n"
                        "| #66 | syberlabs/522ca1aa | open | - |\n| #70 | syberlabs/17f5d831 | open | - |\n",
                        "| #70 | syberlabs/17f5d831 | merged | 5e1c9a0 |\n| #63 | syberlabs/d9a1cc66 | merged | 9b7d2e4 |\n"
                        "| #65 | syberlabs/b2e9bf61 | merged | c3a5e7f |\n| #64 | syberlabs/afc19144 | merged | 1f3e5d7 |\n"
                        "| #66 | syberlabs/522ca1aa | open | - |\n")],
        "head_line": [("src/status.md", "Chain head: main at e19c74b (2026-10-21).", "Chain head: main at 1f3e5d7 (2026-10-22).")],
        "test_doc": [("src/test_landing.py",
                      '"""The landing record of the chain of 2026-10-21: #61 merged, #59 superseded."""',
                      '"""The landing record of the chain of 2026-10-22: #63, #64, #65 and #70 merged, #66 not landed."""')],
        "landed_order": [("src/release.py", 'CHAIN = "2026-10-21"\nLANDED = (61,)\n', 'CHAIN = "2026-10-22"\nLANDED = (70, 63, 65, 64)\n')],
        "head_constant": [("src/release.py", 'HEAD = "e19c74b"\n', 'HEAD = "1f3e5d7"\n')],
    },
    "certificate_layers": {
        "pae": [("src/envelope.py",
                 '    return payload_type.encode("ascii") + b"." + payload\n',
                 '    kind = payload_type.encode("ascii")\n'
                 '    return b"DSSEv1 " + str(len(kind)).encode("ascii") + b" " + kind + b" " + str(len(payload)).encode("ascii") + b" " + payload\n')],
        "envelope_fields": [("src/envelope.py", "import hashlib\nimport hmac\n", "import base64\nimport hashlib\nimport hmac\n"),
                            ("src/envelope.py",
                             '    return {"payloadType": payload_type, "payload": payload.hex(),\n'
                             '            "signatures": [{"sig": hmac.new(key, pae(payload_type, payload), hashlib.sha256).hexdigest()}]}\n',
                             '    signature = hmac.new(key, pae(payload_type, payload), hashlib.sha256).digest()\n'
                             '    return {"payloadType": payload_type, "payload": base64.standard_b64encode(payload).decode("ascii"),\n'
                             '            "signatures": [{"keyid": key.hex(), "sig": base64.standard_b64encode(signature).decode("ascii")}]}\n')],
        "statement_shape": [("src/statement.py",
                             'STATEMENT_TYPE = "https://in-toto.io/Statement/v0.1"\nPREDICATE_TYPE = "sdk.syberlabs.space/certificate"\n',
                             'STATEMENT_TYPE = "https://in-toto.io/Statement/v1"\nPREDICATE_TYPE = "https://sdk.syberlabs.space/certificate/v0alpha1"\n'),
                            ("src/statement.py",
                             '    return {"_type": STATEMENT_TYPE, "predicateType": PREDICATE_TYPE, "predicate": payload,\n'
                             '            "subject": subject(payload), "version": 1}\n',
                             '    return {"_type": STATEMENT_TYPE, "subject": subject(payload), "predicateType": PREDICATE_TYPE, "predicate": payload}\n')],
        "subject": [("src/statement.py",
                     '    return {"name": payload["thread"], "sha1": payload["result"]["commit"]}\n',
                     '    result = payload["result"]\n'
                     '    return [{"name": payload["admission"]["target_ref"], "digest": {"gitCommit": result["commit"], "gitTree": result["tree"]}}]\n')],
        "unwrap_codes": [("src/statement.py",
                          '    if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != PREDICATE_TYPE:\n'
                          '        raise Fail("invalid_statement")\n'
                          '    return statement["predicate"]\n',
                          '    if statement.get("_type") != STATEMENT_TYPE:\n'
                          '        raise Fail("statement_type_mismatch", f"_type is not {STATEMENT_TYPE}")\n'
                          '    if statement.get("predicateType") != PREDICATE_TYPE:\n'
                          '        raise Fail("predicate_type_mismatch", f"predicateType is not {PREDICATE_TYPE}")\n'
                          '    if statement.get("subject") != subject(statement["predicate"]):\n'
                          '        raise Fail("subject_mismatch", "the subject is not the predicate\'s target ref, result commit and tree")\n'
                          '    return statement["predicate"]\n')],
        "identity": [("src/payload.py",
                      '    return {"protocol": PROTOCOL, "payload_type": PAYLOAD_TYPE, "version": "0",\n'
                      '            "issuer": {"tool": "syberlabs.certificate", "seed": os.environ.get(seed_env, ""), "signing": "ed25519"}}\n',
                      '    return {"protocol": PROTOCOL, "payload_type": PAYLOAD_TYPE, "version": 0,\n'
                      '            "issuer": {"tool": "syberlabs.certificate/0", "host": "syberlabs.host", "seed_env": seed_env, "signing": "in-process"}}\n')],
        "target_ref": [("src/payload.py", '    return "syberlabs/" + thread\n', '    return "refs/heads/syberlabs/" + thread[:8]\n')],
        "layer_order": [("src/layers.py",
                         '    states, reason = [], None\n'
                         '    for layer in (repository, journal, signature):\n'
                         '        if layer is None:\n'
                         '            states.append("skipped")\n'
                         '            continue\n'
                         '        code = layer()\n'
                         '        states.append("ok" if code is None else code)\n'
                         '        if code is not None:\n'
                         '            reason = code\n',
                         '    states, reason = [None, None, None], None\n'
                         '    for index, layer in enumerate((signature, repository, journal)):\n'
                         '        if layer is None:\n'
                         '            continue\n'
                         '        code = layer()\n'
                         '        states[index] = "ok" if code is None else code\n'
                         '        if code is not None:\n'
                         '            reason = code\n'
                         '            break\n')],
    },
    "cell_metrics": {
        "cases_open": [("src/counters.py",
                        '    return sum(1 for events in cases.values() if not any(e["kind"] == "case_cancelled" for e in events))\n',
                        '    count = 0\n'
                        '    for events in cases.values():\n'
                        '        cancelled = any(e["kind"] == "case_cancelled" for e in events)\n'
                        '        complete = any(e["kind"] == "effect_succeeded" and e["body"].get("action") == "accept_change" for e in events)\n'
                        '        count += int(not cancelled and not complete)\n'
                        '    return count\n')],
        "proposals": [("src/counters.py", 'sum(1 for s in decided if s != "denied")', 'sum(1 for s in decided if s == "allowed")')],
        "effects": [("src/counters.py",
                     '    applied = {e["body"]["proposal_id"] for e in events if e["kind"] == "effect_succeeded"}\n'
                     '    rejected = {e["body"]["proposal_id"] for e in events if e["kind"] == "effect_rejected"}\n'
                     '    return {"effects_settled_applied_total": len(applied), "effects_settled_not_applied_total": len(rejected),\n'
                     '            "effects_pending": len(claimed) - len(applied)}\n',
                     '    def verified(body):\n'
                     '        proof = body.get("proof")\n'
                     '        return (body.get("success") is True and isinstance(proof, dict) and proof.get("verified") is True\n'
                     '                and bool(proof.get("external_id")) and bool(proof.get("response_digest")))\n'
                     '    applied = {e["body"]["proposal_id"] for e in events\n'
                     '               if e["kind"] == "effect_succeeded" or (e["kind"] == "reconciled" and verified(e["body"]))}\n'
                     '    rejected = {e["body"]["proposal_id"] for e in events if e["kind"] == "effect_rejected"}\n'
                     '    return {"effects_settled_applied_total": len(applied & claimed), "effects_settled_not_applied_total": len(rejected & claimed),\n'
                     '            "effects_pending": len(claimed - applied - rejected)}\n')],
        "version": [("src/reading.py", "import hashlib\n", "import hashlib\nimport json\n"),
                    ("src/reading.py",
                     '    return hashlib.sha256(f"{key}={value}".encode()).hexdigest()[:12]\n',
                     '    document = {"key": key, "value": value, "observed_at": observed_at}\n'
                     '    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()\n')],
        "etag": [("src/reading.py",
                  '    return {"ETag": "W/" + doc["version"], "Content-Type": "application/json", "Cache-Control": "no-store"}\n',
                  '    return {"ETag": \'"\' + doc["version"] + \'"\', "X-Record-Version": doc["version"],\n'
                  '            "Content-Type": "application/json", "Cache-Control": "no-store"}\n')],
        "incomplete_row": [("src/rows.py",
                            '    for row in threads:\n        status = open_thread(row["id"])\n',
                            '    for row in threads:\n'
                            '        if row["state"] == "incomplete":\n'
                            '            rows.append({**row, "status": "incomplete", "accepted": None, "candidates": 0,\n'
                            '                         "chain_valid": chain_valid(row["id"]), "remedy": "start again with the same objective, or forget"})\n'
                            '            continue\n'
                            '        status = open_thread(row["id"])\n')],
        "refusal_status": [("src/rows.py",
                            'def response(exc):\n    return 500, {"error": "internal_error"}\n',
                            'def response(exc):\n'
                            '    if isinstance(exc, Refused):\n'
                            '        return (404 if exc.code.startswith("unknown") else 409), {"error": exc.code, "detail": exc.detail}\n'
                            '    return 500, {"error": "internal_error"}\n')],
        "tone": [("src/tone.py",
                  '        "failed": "bad", "timed_out": "bad", "error": "bad", "needs_approval": "bad", "not_applied": "bad",\n'
                  '        "pending": "wait", "denied": "wait", "unresolved": "wait", "stale": "wait", "provisional": "wait",\n'
                  '        "none": "mute", "not_run": "mute", "in_progress": "mute", "cancelled": "mute"}\n',
                  '        "failed": "bad", "timed_out": "bad", "error": "bad", "denied": "bad", "not_applied": "bad",\n'
                  '        "pending": "wait", "needs_approval": "wait", "unresolved": "wait", "stale": "wait", "in_progress": "wait",\n'
                  '        "none": "mute", "not_run": "mute", "provisional": "mute", "cancelled": "mute"}\n'),
                 ("src/tone.py",
                  '    return f\'<span class="{TONE.get(key, "mute")}">{html.escape(key)}</span>\'\n',
                  '    return f\'<span class="{TONE[key]}">{html.escape(key)}</span>\' if key in TONE else html.escape(key)\n')],
    },
    "thread_stations": {
        "registered": [("src/register.py",
                        '    return {"id": candidate["id"], "provider": provider["name"], "files": len(candidate["changed_paths"]),\n'
                        '            "in_scope": not candidate["scope_violations"]}\n',
                        '    return {"id": candidate["id"], "provider": f"{provider[\'name\']}@{provider[\'revision\']}", "files": len(candidate["changed_paths"]),\n'
                        '            "in_scope": not candidate["scope_violations"] and not candidate["limit_violations"]}\n')],
        "check_order": [("src/evaluate.py", "    return list(checks)\n", "    return sorted(checks)\n")],
        "setup_gate": [("src/evaluate.py",
                        '    results = []\n    for name in ordered(checks):\n        results.append(run(name))\n    return results\n',
                        '    results, gated = [], False\n'
                        '    for name in ordered(checks):\n'
                        '        if gated:\n'
                        '            results.append({"name": name, "state": "error", "exit_code": None, "duration_ms": 0,\n'
                        '                            "output_digest": hashlib.sha256(b"").hexdigest(), "output_tail": "setup_failed"})\n'
                        '            continue\n'
                        '        found = run(name)\n'
                        '        results.append(found)\n'
                        '        if name in SETUP_NAMES and found["state"] != "passed":\n'
                        '            gated = True\n'
                        '    return results\n')],
        "score": [("src/evaluate.py",
                   '    return sum(1 for r in results if r["state"] == "passed")\n',
                   '    states = {r["name"]: r["state"] for r in results}\n'
                   '    return (sum(states.get(name) == "passed" for name in required), sum(state == "passed" for state in states.values()))\n')],
        "promotion_state": [("src/promote.py",
                             '    if decision == "allowed":\n        return "admitted"\n    if decision == "denied":\n        return "denied"\n    return "provisional"\n',
                             '    if "effect_succeeded" in outcomes or "reconciled" in outcomes:\n        return "promoted"\n'
                             '    if "effect_rejected" in outcomes:\n        return "not_applied"\n'
                             '    if outcomes:\n        return "unresolved"\n'
                             '    if decision is None:\n        return "provisional"\n'
                             '    return {"allowed": "admitted", "needs_approval": "needs_approval"}.get(decision, "denied")\n')],
        "acceptable": [("src/promote.py", '    return decision == "allowed"\n', '    return decision in ("allowed", "needs_approval") and bool(current)\n')],
        "publish_lines": [("src/publish.py",
                           '    return {"remote": remote, "ref": ref, "commit": commit}\n',
                           '    return {"destination": f"git:{remote}:{ref}", "external_id": commit}\n'),
                          ("src/publish.py",
                           '    return {"repository": repository, "number": number, "url": url}\n',
                           '    return {"destination": "github:" + repository, "external_id": str(number), "url": url}\n'),
                          ("src/publish.py",
                           '    return " ".join(str(v) for v in output.values())\n',
                           '    return f"{output[\'destination\']}: {output[\'external_id\']}" + (f" {output[\'url\']}" if output.get("url") else "")\n')],
        "certify_summary": [("src/summary.py",
                             '    result = payload["result"]\n'
                             '    return f"certified {result[\'candidate\']} of thread {payload[\'thread\']}: result {result[\'commit\']}"\n',
                             '    result, contract = payload["result"], payload["contract"]\n'
                             '    checks = ", ".join(f"{c[\'name\']} {c[\'state\']}" for c in sorted(payload["evaluation"]["checks"], key=lambda c: c["name"]))\n'
                             '    klass = f", class {payload[\'act_class\']}" if payload["act_class"] else ""\n'
                             '    return (f"certified {result[\'candidate\']} of thread {payload[\'thread\'][:8]}: result {result[\'commit\'][:10]} "\n'
                             '            f"(tree {result[\'tree\'][:10]}) on {contract[\'id\']}.v{contract[\'version\']}{klass}; checks {checks}; "\n'
                             '            f"chain head {payload[\'chain\'][\'head\'][:10]} at {payload[\'chain\'][\'count\']} events")\n')],
    },
}

# A plausible wrong reading of one part, as a replacement applied over that part's reference fix: the
# part it must fail and a phrase the message must contain. Two per fixture.
WRONG_READINGS_V4 = {
    "stack_landing": {
        "narrowed_entry": (("src/gaps.md", "*2026-10-22, closed in part.*", "*2026-10-22, narrowed.*"), "a narrowed entry is closed in part"),
        "landed_order": (("src/release.py", "LANDED = (70, 63, 65, 64)", "LANDED = (63, 64, 65, 70)"), "in the order the chain merged them"),
    },
    "certificate_layers": {
        "pae": (("src/envelope.py", 'b"DSSEv1 " + str(len(kind)).encode("ascii") + b" " + kind + b" " + str(len(payload)).encode("ascii") + b" " + payload',
                 'b"DSSEv1" + len(kind).to_bytes(8, "little") + kind + len(payload).to_bytes(8, "little") + payload'), "ASCII decimal"),
        "identity": (("src/payload.py", '"seed_env": seed_env', '"seed_env": __import__("os").environ.get(seed_env, "")'), "the seed value must never appear"),
    },
    "cell_metrics": {
        "etag": (("src/reading.py", '"ETag": \'"\' + doc["version"] + \'"\'', '"ETag": \'W/"\' + doc["version"] + \'"\''), "strong validator"),
        "effects": (("src/counters.py", 'e["kind"] == "effect_succeeded" or (e["kind"] == "reconciled" and verified(e["body"]))',
                     'e["kind"] in ("effect_succeeded", "reconciled")'), "including an unverified reconciliation"),
    },
    "thread_stations": {
        "setup_gate": (("src/evaluate.py", '"state": "error", "exit_code": None', '"state": "skipped", "exit_code": None'), "state error"),
        "promotion_state": (("src/promote.py", '    if outcomes:\n        return "unresolved"\n', ""), "any other effect event is unresolved"),
    },
}


def apply_parts(name: str, parts: list[str], extra: tuple | None = None) -> dict:
    """The files of fixture ``name`` with the reference fix of each part in ``parts`` applied, then ``extra``."""
    files = {path: text for path, text in FIXTURES[name]["files"].items()}
    for part in parts:
        for path, old, new in PARTS_V4[name][part]:
            assert files[path].count(old) == 1, (name, part, path, old)
            files[path] = files[path].replace(old, new)
    if extra is not None:
        path, old, new = extra
        assert files[path].count(old) == 1, (name, extra)
        files[path] = files[path].replace(old, new)
    return files


def halves(name: str) -> tuple[list[str], list[str]]:
    """Two disjoint halves of a fixture's parts, interleaved so that most files hold a part of each."""
    parts = list(PARTS_V4[name])
    return parts[0::2], parts[1::2]


class FixturesV4(unittest.TestCase):
    """Four multipart fixtures: several files, six to eight separable parts (docs/MODEL_COMPARISON.md, v4)."""

    def test_the_four_fixtures_have_several_files_and_six_to_eight_parts_and_no_correct_slot(self):
        self.assertEqual(sorted(V4), ["cell_metrics", "certificate_layers", "stack_landing", "thread_stations"])
        for name in V4:
            self.assertTrue(3 <= len(FIXTURES[name]["files"]) <= 5, (name, len(FIXTURES[name]["files"])))
            self.assertTrue(6 <= len(FIXTURES[name]["checks"]) <= 8, (name, len(FIXTURES[name]["checks"])))
            self.assertEqual(sorted(PARTS_V4[name]), sorted(FIXTURES[name]["checks"]), name)
            self.assertEqual(sorted(harness.contract(name)["evolution"]["evaluation"]["required"]), sorted(FIXTURES[name]["checks"]))
            self.assertTrue(all(path.startswith("src/") for path in FIXTURES[name]["files"]), name)
            self.assertLessEqual(len(FIXTURES[name]["files"]), harness.contract(name)["evolution"]["scope"]["max_files"])
            for slot in FIXTURES[name]["slots"]:
                self.assertEqual(slot["correct"], [], f"{name}: a v4 slot must hold no correct variant")
                self.assertIn(slot["check"], FIXTURES[name]["checks"])

    def test_the_parts_regions_are_disjoint_and_the_wrong_readings_are_well_formed(self):
        """Every part's old text occurs once in the broken file and survives every other part's fix: the regions do not overlap."""
        for name in V4:
            for part, edits in PARTS_V4[name].items():
                others = apply_parts(name, [p for p in PARTS_V4[name] if p != part])
                for path, old, _ in edits:
                    self.assertEqual(FIXTURES[name]["files"][path].count(old), 1, (name, part, path))
                    self.assertEqual(others[path].count(old), 1, f"{name}/{part}: its region is touched by another part's fix in {path}")
            self.assertGreaterEqual(len(WRONG_READINGS_V4[name]), 2, name)
            for part, (extra, _) in WRONG_READINGS_V4[name].items():
                self.assertIn(part, PARTS_V4[name])
                apply_parts(name, list(PARTS_V4[name]), extra)

    def test_broken_fails_every_part_by_name_and_the_reference_passes_every_part(self):
        for name in V4:
            with tempfile.TemporaryDirectory() as folder:
                broken, fixed = Path(folder) / "broken", Path(folder) / "fixed"
                broken.mkdir()
                fixed.mkdir()
                materialise(name, broken)
                materialise(name, fixed, apply_parts(name, list(PARTS_V4[name])))
                before, after = check_outputs(name, broken), check_outputs(name, fixed)
            self.assertEqual({state for state, _ in before.values()}, {"failed"}, (name, before))
            for part, (_, message) in before.items():
                self.assertTrue(message.startswith(f"AssertionError: {part}: "),
                                f"{name}/{part}: a failing check must end with an assertion message naming the part; got {message!r}")
            self.assertEqual({state for state, _ in after.values()}, {"passed"}, (name, after))

    def test_two_disjoint_half_fixes_pass_their_halves_fail_the_rest_by_name_and_their_union_passes(self):
        """Recombination by construction: a child that fixed half A and one that fixed half B merge into a child that passes all."""
        for name in V4:
            first, second = halves(name)
            self.assertFalse(set(first) & set(second))
            self.assertEqual(sorted(first + second), sorted(PARTS_V4[name]))
            with tempfile.TemporaryDirectory() as folder:
                found = {}
                for label, parts in (("A", first), ("B", second), ("union", first + second)):
                    root = Path(folder) / label
                    root.mkdir()
                    materialise(name, root, apply_parts(name, parts))
                    found[label] = check_outputs(name, root)
            for label, own, rest in (("A", first, second), ("B", second, first)):
                for part in own:
                    self.assertEqual(found[label][part][0], "passed", (name, label, part, found[label][part]))
                for part in rest:
                    state, message = found[label][part]
                    self.assertEqual(state, "failed", (name, label, part, found[label][part]))
                    self.assertTrue(message.startswith(f"AssertionError: {part}: "), (name, label, part, message))
            self.assertEqual({state for state, _ in found["union"].values()}, {"passed"}, (name, found["union"]))

    def test_a_plausible_wrong_reading_fails_only_the_named_part_with_a_message_naming_the_expectation(self):
        for name in V4:
            for part, (extra, phrase) in WRONG_READINGS_V4[name].items():
                with tempfile.TemporaryDirectory() as folder:
                    materialise(name, Path(folder), apply_parts(name, list(PARTS_V4[name]), extra))
                    found = check_outputs(name, Path(folder))
                state, message = found[part]
                self.assertEqual(state, "failed", (name, part, found))
                self.assertTrue(message.startswith(f"AssertionError: {part}: "), (name, part, message))
                self.assertIn(phrase, message, (name, part, message))
                self.assertEqual({s for p, (s, _) in found.items() if p != part}, {"passed"}, (name, part, found))

    def test_the_pinned_conventions_are_the_kits_own(self):
        """The v4 pins come from the program: the PAE, the statement constants, the issuer, the target ref, the certify line, the checks' gate."""
        from benchmarks.fixtures import STATION_PAYLOAD, STATION_SUMMARY_LINE, TONE_EXPECTED
        from syberlabs import certificate, checks, dashboard, dsse, inspector
        from syberlabs.evolution import DEFAULT_TARGET_REF, PROMOTION_RANK
        self.assertEqual(dsse.pae("application/vnd.in-toto+json", b"{}"), b"DSSEv1 28 application/vnd.in-toto+json 2 {}")
        self.assertEqual((certificate.STATEMENT_TYPE, certificate.PREDICATE_TYPE, certificate.STATEMENT_FIELDS),
                         ("https://in-toto.io/Statement/v1", "https://sdk.syberlabs.space/certificate/v0alpha1",
                          ("_type", "subject", "predicateType", "predicate")))
        self.assertEqual((certificate.ISSUER, certificate.HOST, certificate.SIGNING, certificate.VERSION, certificate.LAYERS),
                         ("syberlabs.certificate/0", "syberlabs.host", "in-process", 0, ("signature", "repository", "journal")))
        self.assertEqual(DEFAULT_TARGET_REF.replace("{thread}", "8525ff4c-0cf2"[:8]), "refs/heads/syberlabs/8525ff4c")
        self.assertEqual(certificate.summary(STATION_PAYLOAD), STATION_SUMMARY_LINE)
        self.assertEqual((checks.SETUP_NAMES, checks.EMPTY_DIGEST), (("setup", "install"), hashlib.sha256(b"").hexdigest()))
        self.assertEqual(PROMOTION_RANK, {"denied": 0, "not_applied": 1, "admitted": 2, "needs_approval": 2, "unresolved": 3, "promoted": 4})
        self.assertEqual(dashboard.TONE, TONE_EXPECTED)
        self.assertEqual(inspector.INCOMPLETE_REMEDY, "start again with the same objective, or forget")


class HarnessV4(unittest.TestCase):
    def test_single_does_not_solve_the_v4_fixtures_with_the_simulated_model(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [harness.run(name, "single", harness.SIMULATED, 6, 1, Path(folder)) for name in V4]
        for row in rows:
            self.assertFalse(row["solved"], row)
            self.assertEqual((row["calls"], row["candidates"], row["evaluations"], row["model"], row["provider_error"]),
                             (1, 1, 1, "simulated", None), row)
        report = harness.summarize(rows, 6)
        for name in V4:
            self.assertRegex(report, rf"{name}\s+single\s+0/1")


if __name__ == "__main__":
    unittest.main()
