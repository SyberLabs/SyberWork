"""The model adapter seam, cost accounting, the cost-matched comparison harness (simulated model only), and the v2 and v3 fixtures."""

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
from benchmarks.fixtures import FIXTURES, V2, V3
from benchmarks import simulated_model as simulated
from benchmarks.fixtures import FIXTURES, V2
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

    def test_an_absent_or_empty_scope_is_never_stated_as_empty(self):
        """Before 2026-10-22 the mutate request carried no scope and the prompt read ``Mutable scope: `` (nothing);
        Sonnet 5.5, told to write only inside the stated scope, returned no changes on every evolve call."""
        for protocol, extra in (("syberlabs.mutate/v0alpha1", {"parent": "c3", "seed": 9, "files": {"src/a.py": "A"}}),
                                ("syberlabs.search/v0alpha1", {"context": [{"path": "src/report.py"}], "files": ["src/report.py"]})):
            for request in ({"protocol": protocol, "objective": "x", **extra},
                            {"protocol": protocol, "objective": "x", "scope": [], **extra},
                            {"protocol": protocol, "objective": "x", "scope": None, **extra}):
                with self.subTest(protocol=protocol, scope=request.get("scope", "absent")):
                    prompt = adapter.build_prompt(request, self.root)
                    self.assertIn("Mutable scope: the files shown below", prompt)
                    self.assertNotIn("Mutable scope: \n", prompt)
            with self.subTest(protocol=protocol, scope="stated"):
                prompt = adapter.build_prompt({"protocol": protocol, "objective": "x", "scope": ["src/", "docs"], **extra}, self.root)
                self.assertIn("Mutable scope: src/, docs", prompt)
                self.assertNotIn("the files shown below", prompt)
        self.assertIn("when no scope is stated, the files shown in the request", adapter.SYSTEM)

    def test_a_mutate_request_with_failing_feedback_asks_to_fix_the_failing_checks(self):
        """With the parent's failing checks a mutation has a direction; without them, or when every check passed, the
        wording is the blind re-sample it was. The failing block is the one the repair branch quotes, bounded alike."""
        request = {"protocol": "syberlabs.mutate/v0alpha1", "objective": "x", "scope": ["src"], "parent": "c3", "seed": 9,
                   "files": {"src/a.py": "A"}}
        tail = "".join(f"line {n}\n" for n in range(400))  # longer than the 2000-character bound
        failing = {"candidate": "c3", "checks": [
            {"name": "tests", "state": "failed", "exit_code": 1, "duration_ms": 12, "output_tail": tail},
            {"name": "lint", "state": "passed", "exit_code": 0, "duration_ms": 3, "output_tail": "ok"}]}
        plain = adapter.build_prompt(request, self.root)
        fed = adapter.build_prompt({**request, "feedback": failing}, self.root)
        self.assertIn("Vary your approach; seed 9", plain)
        self.assertNotIn("did not pass", plain)
        self.assertIn("This candidate did not pass the host's checks. Fix it. Failing checks:\n--- tests (failed)\n", fed)
        self.assertIn("Make one focused change toward the objective that fixes them, as a mutation of this candidate. "
                      "Candidate: c3; seed 9.", fed)
        self.assertNotIn("Vary your approach", fed)
        self.assertNotIn("--- lint", fed, "a check that passed is not quoted")
        self.assertIn(tail[-2000:], fed)
        self.assertNotIn(tail[:200], fed, "an output tail is quoted to its last 2000 characters, as repair quotes it")
        self.assertIn('<file path="src/a.py">\nA\n</file>', fed, "the request's own files are shown")
        self.assertIn("Mutable scope: src", fed)
        block = adapter.failing_checks(failing)
        self.assertTrue(block.startswith("--- tests (failed)\n") and block.endswith(tail[-2000:]))
        repair = adapter.build_prompt({"protocol": "syberlabs.search/v0alpha1", "objective": "x", "scope": ["src"],
                                       "feedback": {**failing, "files": {"src/a.py": "A"}}}, self.root)
        for prompt in (repair, fed):
            self.assertIn("did not pass the host's checks. Fix it. Failing checks:\n" + block, prompt)
        passed = {"candidate": "c3", "checks": [{"name": "tests", "state": "passed", "output_tail": ""}]}
        for feedback in (passed, {"candidate": "c3", "checks": []}, None):
            with self.subTest(feedback=feedback):
                self.assertEqual(adapter.build_prompt({**request, "feedback": feedback}, self.root), plain)
        self.assertEqual(adapter.failing_checks(None), "")

    def test_a_mutation_of_base_with_the_base_evaluation_as_feedback_is_directed(self):
        """The fill's requests name ``base`` as parent and, since the host evaluates the base tree first
        (base-evaluation, 2026-10-22), carry its checks; the adapter directs them exactly as a mutation of a failing child."""
        request = {"protocol": "syberlabs.mutate/v0alpha1", "objective": "x", "scope": ["src"], "parent": "base", "seed": 4,
                   "files": {"src/a.py": "A"}}
        feedback = {"candidate": "base", "checks": [
            {"name": "tests", "state": "failed", "exit_code": 1, "duration_ms": 12, "output_tail": "E  assert 1 == 2"},
            {"name": "lint", "state": "passed", "exit_code": 0, "duration_ms": 3, "output_tail": "ok"}]}
        plain = adapter.build_prompt(request, self.root)
        fed = adapter.build_prompt({**request, "feedback": feedback}, self.root)
        self.assertIn("Candidate: base. Vary your approach; seed 4.", plain)
        self.assertIn("This candidate did not pass the host's checks. Fix it. Failing checks:\n--- tests (failed)\nE  assert 1 == 2", fed)
        self.assertIn("Make one focused change toward the objective that fixes them, as a mutation of this candidate. "
                      "Candidate: base; seed 4.", fed)
        self.assertNotIn("Vary your approach", fed)
        self.assertNotIn("--- lint", fed)
        child = adapter.build_prompt({**request, "parent": "c3", "feedback": {**feedback, "candidate": "c3"}}, self.root)
        self.assertEqual(fed.replace("Candidate: base", "Candidate: c3"), child, "the same wording as for a child")

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
POLICY_ARMS = ("evolve", "evolve_empirical", "evolve_jev")


class SimulatedProfiles(unittest.TestCase):
    """The simulated adapter's per-operator profiles. Simulated: nothing here is about a model."""

    def mutate(self, profile, parent, text=None):
        os.environ["SYBERLABS_FIXTURE"] = "pricing"
        if profile is None:
            os.environ.pop("SIM_PROFILE", None)
        else:
            os.environ["SIM_PROFILE"] = profile
        self.addCleanup(os.environ.pop, "SIM_PROFILE", None)
        text = text or FIXTURES["pricing"]["files"]["src/pricing.py"]
        with tempfile.TemporaryDirectory() as folder:
            out = {}
            for seed in range(1, 21):
                request = {"protocol": "syberlabs.mutate/v0alpha1", "seed": seed, "parent": parent, "objective": "x",
                           "context": [], "files": {"src/pricing.py": text}}
                found = simulated.respond(request, Path(folder))["changes"]["src/pricing.py"]
                out[seed] = [simulated.is_right(found, slot) for slot in FIXTURES["pricing"]["slots"]]
            return out

    def test_mutation_rare_fixes_one_slot_from_base_and_rarely_from_a_child(self):
        from_base = self.mutate("mutation_rare", "base")
        self.assertTrue(all(sum(right) == 1 for right in from_base.values()), from_base)
        self.assertEqual(len({tuple(r) for r in from_base.values()}), 3, "children from base fix different slots")
        from_child = self.mutate("mutation_rare", "c1")
        fixes = sum(sum(right) for right in from_child.values())
        self.assertTrue(0 < fixes < 10, (fixes, from_child))

    def test_mutation_common_rewrites_every_remaining_slot(self):
        rows = self.mutate("mutation_common", "c1")
        self.assertTrue(any(all(right) for right in rows.values()), rows)
        text = FIXTURES["pricing"]["files"]["src/pricing.py"]
        os.environ["SIM_PROFILE"] = "mutation_common"
        with tempfile.TemporaryDirectory() as folder:
            found = simulated.respond({"protocol": "syberlabs.mutate/v0alpha1", "seed": 3, "parent": "c1", "objective": "x",
                                       "context": [], "files": {"src/pricing.py": text}}, Path(folder))["changes"]["src/pricing.py"]
        for slot in FIXTURES["pricing"]["slots"]:
            self.assertNotEqual(simulated.current(found, slot), simulated.current(text, slot), slot["function"])

    def test_feedback_in_a_mutate_request_changes_nothing_simulated(self):
        """The stand-in never reads ``feedback``: the same seed gives the same child with and without it, under every profile."""
        text = FIXTURES["pricing"]["files"]["src/pricing.py"]
        os.environ["SYBERLABS_FIXTURE"] = "pricing"
        self.addCleanup(os.environ.pop, "SIM_PROFILE", None)
        feedback = {"candidate": "c1", "checks": [{"name": "discount", "state": "failed", "exit_code": 1, "duration_ms": 2,
                                                   "output_tail": "AssertionError"}]}
        with tempfile.TemporaryDirectory() as folder:
            for profile in (None, *sorted(simulated.PROFILES)):
                if profile is None:
                    os.environ.pop("SIM_PROFILE", None)
                else:
                    os.environ["SIM_PROFILE"] = profile
                for seed in range(1, 6):
                    request = {"protocol": "syberlabs.mutate/v0alpha1", "seed": seed, "parent": "c1", "objective": "x",
                               "context": [], "files": {"src/pricing.py": text}}
                    with self.subTest(profile=profile, seed=seed):
                        self.assertEqual(simulated.respond({**request, "feedback": feedback}, Path(folder))["changes"],
                                         simulated.respond(request, Path(folder))["changes"])

    def test_no_profile_keeps_the_single_slot_reroll(self):
        rows = self.mutate(None, "base")
        self.assertTrue(all(sum(right) <= 1 for right in rows.values()), rows)
        self.assertNotIn("SIM_PROFILE", os.environ)
        with self.assertRaises(ValueError):
            harness.run("pricing", "single", harness.SIMULATED, 1, 1, Path(tempfile.gettempdir()), profile="no_such_profile")


class PolicyArms(unittest.TestCase):
    """The three evolve arms told apart on the ``mutation_rare`` profile, K = 12, five seeds, through the real harness.

    Simulated, so a statement about the harness and the profile, not about a model: where a
    crossover combines what two mutations fixed, Jev buys crossovers early and both learning
    policies reach a passing candidate in fewer model calls than the fixed schedule.
    """

    CALLS, SEEDS, PROFILE = 12, (1, 2, 3, 4, 5), "mutation_rare"

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.journals = Path(cls.folder.name) / "rows-journals"
        cls.rows = [harness.run("pricing", arm, harness.SIMULATED, cls.CALLS, seed, Path(cls.folder.name), cls.journals, cls.PROFILE)
                    for arm in POLICY_ARMS for seed in cls.SEEDS]

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def totals(self, key):
        return {arm: sum(r[key] for r in self.rows if r["arm"] == arm) for arm in POLICY_ARMS}

    def test_every_row_names_its_policy_and_the_profile_and_stays_within_the_budget(self):
        expected = {"evolve": ("fixed", "2"), "evolve_empirical": ("empirical", "2"), "evolve_jev": ("jev", "1")}
        for row in self.rows:
            self.assertEqual((row["policy"]["name"], row["policy"]["revision"]), expected[row["arm"]], row)
            self.assertEqual((row["profile"], row["simulated"], row["dollars"], row["model"]), (self.PROFILE, True, None, "simulated"))
            self.assertLessEqual(row["calls"], self.CALLS)
            self.assertGreater(row["host_dollars"], 0.0, "the priced contract makes the host observe the simulated usage")
            exported = json.loads((Path(self.folder.name) / row["journal"]).read_text(encoding="utf-8"))
            signals = [e["body"]["signal"]["policy"] for e in exported["events"] if e["kind"] == "candidate_registered"]
            self.assertTrue(signals and all((s["name"], s["revision"]) == expected[row["arm"]] for s in signals), row["arm"])
            spend = [e["body"]["spend"]["dollars"] for e in exported["events"] if e["kind"] == "candidate_registered"]
            self.assertTrue(all(d is not None for d in spend), row["arm"])

    def test_jev_needs_fewer_calls_and_fewer_simulated_dollars_than_the_fixed_schedule(self):
        calls, dollars, solved = self.totals("calls"), self.totals("host_dollars"), self.totals("solved")
        self.assertLess(calls["evolve_jev"], calls["evolve"], calls)
        self.assertLess(dollars["evolve_jev"], dollars["evolve"], dollars)
        self.assertGreaterEqual(solved["evolve_jev"], solved["evolve"], solved)

    def test_empirical_needs_fewer_calls_than_the_fixed_schedule_where_crossover_pays(self):
        calls = self.totals("calls")
        self.assertLess(calls["evolve_empirical"], calls["evolve"], calls)

    def test_the_summary_names_the_profile_and_the_policies(self):
        report = harness.summarize(self.rows, self.CALLS)
        self.assertIn("Simulated profile: mutation_rare.", report.splitlines()[0])
        self.assertIn("policy", report.splitlines()[1])
        for label in ("fixed/2", "empirical/2", "jev/1"):
            self.assertIn(label, report)


class JevPrices(unittest.TestCase):
    def test_the_prior_comes_from_the_contract_or_is_none(self):
        doc = harness.contract("pricing")
        self.assertIsNone(harness.jev_prices(doc))
        doc["evolution"]["budget"] = {**doc["evolution"]["budget"], "prices": harness.price_table(), "max_dollars": harness.MAX_DOLLARS}
        prices = harness.jev_prices(doc)
        self.assertEqual((prices["crossover"], prices["evaluate"]), (0.0, 0.0))
        self.assertGreater(prices["mutate"], 0.0)
        self.assertIn("simulated", harness.price_table())
        self.assertEqual(set(harness.price_table()) - {"simulated"}, set(harness.PRICES))


if __name__ == "__main__":
    unittest.main()
