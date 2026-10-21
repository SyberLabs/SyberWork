"""The model-backed critic (examples/critic_model.py), offline: replayed responses, no key, no network."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from examples import critic_model
from syberlabs import critic
from syberlabs.providers import PatchProvider
from tests.test_build_thread import GOOD
from tests.test_critic import CriticCase

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "examples" / "critic_model.py"
FIXTURES = ROOT / "tests" / "fixtures" / "critic_model"
RECORDED = FIXTURES / "sonnet55-medium-critic-md.json"

REQUEST = {
    "protocol": "syberlabs.critic/1",
    "candidate": {"id": "c1", "commit": "a" * 40, "tree": "b" * 40, "provider": {"name": "claude", "revision": "fable-5.1"},
                  "operator": "patch", "parents": [], "note": "CSV export"},
    "base": "c" * 40,
    "result": [{"name": "conformance", "state": "passed", "exit_code": 0, "duration_ms": 10, "output_digest": "d" * 64,
                "output_tail": "matched 23 golden traces"}],
    "changed_paths": ["src/export.py", "tests/test_export.py"],
    "diff": "--- a/src/export.py\n+++ b/src/export.py\n@@ -1 +1,2 @@\n+import csv\n",
    "objective": "Add a CSV export for report rows",
    "contract": {"acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
                 "scope": {"paths": ["src", "tests"], "exclude": []}},
}


def recorded(findings, summary="two findings", *, stop_reason="end_turn", model="claude-sonnet-5-5", text=None, **extra):
    """A response as ``to_dict()`` of a live one would record it, with the fields the adapter reads."""
    body = text if text is not None else json.dumps({"findings": findings, "summary": summary})
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "stop_reason": stop_reason,
            "stop_sequence": None, "content": [{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": body}],
            "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, **extra}


def finding(**over):
    found = {"severity": "medium", "file": "src/export.py", "line": 2, "claim": "the writer never closes the buffer",
             "falsifier": "a test that calls to_csv twice and compares the outputs", "acceptance_ref": None}
    found.update(over)
    return found


def run_adapter(request, *args, env=None, replay=None):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("SYBERLABS_CRITIC") and k != "ANTHROPIC_API_KEY"}
    environment.update(env or {})
    argv = [PY, str(ADAPTER), *args] + (["--replay", replay] if replay else [])
    done = subprocess.run(argv, input=json.dumps(request), capture_output=True, text=True, env=environment, cwd=str(ROOT))
    return done.returncode, done.stdout, done.stderr


class Fixtures(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def record(self, name, response):
        path = self.folder / name
        path.write_text(json.dumps(response), encoding="utf-8")
        return str(path)


class Shape(unittest.TestCase):
    def test_reviewer_is_the_model_and_effort_and_never_the_proposers(self):
        self.assertEqual(critic_model.REVIEWER, {"name": "critic.model", "revision": f"{critic_model.MODEL}@{critic_model.EFFORT}@1"})
        self.assertRegex(critic_model.REVIEWER["name"], r"^[a-z][a-z0-9_.-]{0,63}$")
        self.assertTrue(critic.independent({"reviewer": critic_model.REVIEWER}, {"name": "claude", "revision": "fable-5.1"}))
        self.assertEqual(critic_model.MODEL, os.environ.get("SYBERLABS_CRITIC_MODEL", "claude-sonnet-5-5"))
        self.assertEqual(critic_model.EFFORT, os.environ.get("SYBERLABS_CRITIC_EFFORT", "medium"))

    def test_schema_uses_only_what_structured_outputs_accept_and_leaves_identity_to_the_adapter(self):
        text = json.dumps(critic_model.SCHEMA)
        for word in ("minLength", "maxLength", "minimum", "maximum", "pattern", "maxItems", "$ref"):
            self.assertNotIn(word, text)
        self.assertNotIn("reviewer", critic_model.SCHEMA["properties"])
        self.assertNotIn("version", critic_model.SCHEMA["properties"])
        for node in (critic_model.SCHEMA, critic_model.FINDING_SCHEMA):
            self.assertIs(node["additionalProperties"], False)
            self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
        self.assertEqual(critic_model.FINDING_SCHEMA["properties"]["severity"], {"enum": ["high", "medium", "low"]})
        self.assertEqual(critic_model.FINDING_SCHEMA["properties"]["line"], {"anyOf": [{"type": "integer"}, {"type": "null"}]})

    def test_prompt_carries_the_request_and_cuts_a_long_diff(self):
        prompt = critic_model.build_prompt(REQUEST)
        for text in ("Objective: Add a CSV export", '"id": "accepted"', '"paths": ["src", "tests"]', "- src/export.py",
                     "conformance (passed, exit 0)", "matched 23 golden traces", "<diff>\n--- a/src/export.py"):
            self.assertIn(text, prompt)
        self.assertNotIn("diff cut", prompt)
        long = dict(REQUEST, diff="x" * (critic_model.MAX_DIFF_CHARS + 10))
        cut = critic_model.build_prompt(long)
        self.assertIn(f"[diff cut at {critic_model.MAX_DIFF_CHARS} characters", cut)
        self.assertLess(len(cut), critic_model.MAX_DIFF_CHARS + 2000)
        self.assertIn("No check ran before this review.", critic_model.build_prompt(dict(REQUEST, result=[])))

    def test_system_prompt_states_the_job(self):
        for text in ("falsifier", "never restate", "high only", "acceptance", "required check", str(critic_model.MAX_FINDINGS)):
            self.assertIn(text, critic_model.SYSTEM)


class Normalize(unittest.TestCase):
    def test_stderr_is_silent_when_it_is_stdout(self):
        path = Path(tempfile.mkdtemp()) / "replay.json"
        path.write_text(json.dumps(recorded([finding()])), encoding="utf-8")
        with open(path.parent / "merged.log", "wb") as log:
            done = subprocess.run([PY, str(ADAPTER), "--replay", str(path)], input=json.dumps(REQUEST).encode(),
                                  stdout=log, stderr=subprocess.STDOUT)
        raw = (path.parent / "merged.log").read_bytes()
        self.assertEqual(done.returncode, 0)
        self.assertEqual(critic.counts(critic.parse(raw)), {"high": 0, "medium": 1, "low": 0})
        self.assertNotIn(b"usage:{", raw)

    def test_a_conforming_answer_becomes_the_host_document(self):
        document, notes = critic_model.normalize({"findings": [finding(), finding(severity="high", acceptance_ref="accepted", line=None)],
                                                  "summary": "two"}, REQUEST["changed_paths"])
        self.assertEqual(notes, [])
        parsed = critic.parse(json.dumps(document).encode())
        self.assertEqual(parsed["reviewer"], critic_model.REVIEWER)
        self.assertEqual(critic.counts(parsed), {"high": 1, "medium": 1, "low": 0})
        self.assertNotIn("acceptance_ref", parsed["findings"][0])
        self.assertEqual(parsed["findings"][1]["acceptance_ref"], "accepted")
        self.assertIsNone(parsed["findings"][1]["line"])

    def test_bounds_of_the_finding_schema_are_enforced_here(self):
        many = [finding(claim="c" * 5000, falsifier="f" * 5000, file="src/export.py") for _ in range(critic_model.MAX_FINDINGS + 5)]
        document, notes = critic_model.normalize({"findings": many, "summary": "s" * 9000}, REQUEST["changed_paths"])
        self.assertEqual(len(document["findings"]), critic_model.MAX_FINDINGS)
        self.assertEqual(len(document["findings"][0]["claim"]), 2000)
        self.assertEqual(len(document["findings"][0]["falsifier"]), 2000)
        self.assertEqual(len(document["summary"]), 4000)
        self.assertIn(f"5 finding(s) beyond {critic_model.MAX_FINDINGS} dropped", notes)
        critic.parse(json.dumps(document).encode())

    def test_malformed_findings_are_dropped_not_invented(self):
        document, notes = critic_model.normalize({"findings": [
            finding(severity="critical"), "not an object", finding(claim=""), finding(file="a\nb"),
            finding(line=-4), finding(line="7"), finding(file="docs/other.md"),
        ], "summary": ""}, REQUEST["changed_paths"])
        self.assertEqual(len(document["findings"]), 3)
        self.assertEqual([f["line"] for f in document["findings"]], [None, None, 2])
        self.assertEqual(document["summary"], "3 finding(s): 0 high, 3 medium, 0 low")
        self.assertEqual(notes[:4], ["finding 0 dropped: severity", "finding 1 dropped: severity",
                                     "finding 2 dropped: empty or unprintable field", "finding 3 dropped: empty or unprintable field"])
        self.assertIn("file(s) not among the changed paths: docs/other.md", notes)
        critic.parse(json.dumps(document).encode())

    def test_an_answer_without_findings_is_an_empty_document(self):
        document, notes = critic_model.normalize({"summary": "fine"})
        self.assertEqual((document["findings"], notes), ([], ["findings missing"]))


class Replay(Fixtures):
    def test_replayed_response_prints_the_document_and_the_usage_line(self):
        path = self.record("ok.json", recorded([finding(), finding(severity="low", line=None)]))
        code, out, err = run_adapter(REQUEST, replay=path)
        self.assertEqual(code, 0, err)
        document = critic.parse(out.encode())
        self.assertEqual(document["reviewer"], critic_model.REVIEWER)
        self.assertEqual(critic.counts(document), {"high": 0, "medium": 1, "low": 1})
        self.assertEqual(document["summary"], "two findings [critic.model usage: claude-sonnet-5-5 in=1200 out=300 cache_read=0]")
        usage_lines = [line for line in err.splitlines() if line.startswith("usage:")]
        self.assertEqual(len(usage_lines), 1)
        usage = json.loads(usage_lines[0][len("usage:"):])
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["model"], usage["stop_reason"]),
                         (1200, 300, "claude-sonnet-5-5", "end_turn"))
        # The environment form works too, and the model and effort name the reviewer.
        code, out, err = run_adapter(REQUEST, env={"SYBERLABS_CRITIC_REPLAY": path, "SYBERLABS_CRITIC_MODEL": "claude-haiku-5-5",
                                                   "SYBERLABS_CRITIC_EFFORT": "low"})
        self.assertEqual(code, 0, err)
        self.assertEqual(critic.parse(out.encode())["reviewer"], {"name": "critic.model", "revision": "claude-haiku-5-5@low@1"})

    def test_refusal_truncation_and_garbage_exit_non_zero_with_one_line_and_no_document(self):
        cases = {
            "refusal.json": (recorded([], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}),
                             "refusal after fallbacks (category cyber)"),
            "cut.json": (recorded([], stop_reason="max_tokens"), "answer truncated (max_tokens)"),
            "text.json": (recorded([], text="I reviewed it and it looks fine"), "model output is not JSON"),
            "list.json": (recorded([], text="[]"), "model output is not an object"),
            "nousage.json": ({k: v for k, v in recorded([]).items() if k != "usage"}, "response has no usage or content"),
            "nocontent.json": ({k: v for k, v in recorded([]).items() if k != "content"}, "response has no usage or content"),
        }
        for name, (response, message) in cases.items():
            with self.subTest(name):
                code, out, err = run_adapter(REQUEST, replay=self.record(name, response))
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err.strip().splitlines(), [f"critic_model: {message}"])

    def test_unreadable_replay_and_bad_stdin_are_errors(self):
        code, out, err = run_adapter(REQUEST, replay=str(self.folder / "missing.json"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("replay file unreadable", err)
        done = subprocess.run([PY, str(ADAPTER), "--replay", "x"], input="not json", capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertEqual(done.stderr.strip(), "critic_model: stdin is not a JSON request")


class Recorded(unittest.TestCase):
    """The one live call of 2026-10-21 (Claude Sonnet 5.5, medium), replayed: it reviewed this packet's own docs and adapter diff."""

    def test_the_recorded_response_replays_into_a_document_the_host_parses(self):
        raw = RECORDED.read_text(encoding="utf-8")
        self.assertNotIn("sk-ant", raw)
        fixture = json.loads(raw)
        self.assertEqual((fixture["model"], fixture["stop_reason"]), ("claude-sonnet-5-5", "end_turn"))
        code, out, err = run_adapter(REQUEST, replay=str(RECORDED))
        self.assertEqual(code, 0, err)
        document = critic.parse(out.encode())
        self.assertEqual(document["reviewer"], {"name": "critic.model", "revision": "claude-sonnet-5-5@medium@1"})
        self.assertEqual(critic.counts(document), {"high": 1, "medium": 2, "low": 1})
        self.assertEqual(document["findings"][0]["acceptance_ref"], "accepted")
        self.assertTrue(all(f["falsifier"] for f in document["findings"]))
        usage = json.loads(next(line for line in err.splitlines() if line.startswith("usage:"))[len("usage:"):])
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (fixture["usage"]["input_tokens"], fixture["usage"]["output_tokens"]))
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (9705, 937))
        self.assertTrue(document["summary"].endswith("[critic.model usage: claude-sonnet-5-5 in=9705 out=937 cache_read=0; 1 note(s)]"))
        # The findings name files the test request did not change; the adapter notes that and keeps them.
        self.assertIn("file(s) not among the changed paths: docs/CRITIC.md, examples/critic_model.py", err)


class ThroughTheHost(CriticCase):
    """The host runs the adapter as ``critic.model`` with a scrubbed environment; ``--replay`` names the fixture."""

    def record(self, response):
        path = self.root.parent / "replay.json"
        path.write_text(json.dumps(response), encoding="utf-8")
        return str(path)

    def test_findings_are_observed_under_the_models_identity(self):
        path = self.record(recorded([finding(), finding(severity="low", file="tests/test_export.py", line=1)], "replayed"))
        kit, thread = self.thread([PY, str(ADAPTER), "--replay", path], GOOD, name="critic.model")
        verdict = thread.check("c1")
        self.assertTrue(verdict.acceptable, verdict.summary())
        [event] = self.observations(thread)
        value = event["body"]["value"]
        self.assertEqual(value["reviewer"], critic_model.REVIEWER)
        self.assertEqual((value["check"], value["counts"]), ("critic.model", {"high": 0, "medium": 1, "low": 1}))
        self.assertEqual(value["summary"], "replayed [critic.model usage: claude-sonnet-5-5 in=1200 out=300 cache_read=0]")
        # Under the host stderr is stdout; the host parsed the whole log, so the adapter wrote only the document there.
        self.assertEqual(self.result(thread, "c1", "critic.model")["output_digest"], value["findings_digest"])

    def test_a_high_finding_blocks_and_a_replayed_refusal_is_a_check_error(self):
        path = self.record(recorded([finding(severity="high", acceptance_ref="accepted")], "one high"))
        kit, thread = self.thread([PY, str(ADAPTER), "--replay", path], GOOD, name="critic.model")
        verdict = thread.check("c1")
        self.assertEqual((verdict.status, verdict.reason), ("denied", "high_finding:critic.model:1"))
        Path(path).write_text(json.dumps(recorded([], stop_reason="refusal")), encoding="utf-8")
        thread.propose(changes={**GOOD, "README.md": "# Report tool\nRun tests with python -m unittest.\nCSV too.\n"}, message="again")
        verdict = thread.check("c2")
        self.assertFalse(verdict.acceptable)
        found = self.result(thread, "c2", "critic.model")
        self.assertEqual((found["state"], found["exit_code"]), ("failed", 2))
        self.assertIn("critic_model: refusal after fallbacks", found["output_tail"])

    def test_the_same_identity_as_the_provider_is_not_independent(self):
        path = self.record(recorded([], "nothing"))
        provider = PatchProvider(GOOD, "CSV export", name="critic.model", revision=critic_model.REVIEWER["revision"])
        kit, thread = self.thread([PY, str(ADAPTER), "--replay", path], GOOD, provider=provider, name="critic.model")
        verdict = thread.check("c1")
        self.assertFalse(verdict.acceptable)
        self.assertEqual(self.result(thread, "c1", "critic.model")["output_tail"], "reviewer_not_independent")


if __name__ == "__main__":
    unittest.main()
