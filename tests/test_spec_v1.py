"""sdk.syberlabs.space/v1alpha1 (seam S0): fixtures behave as labelled, and nothing v0alpha1 recorded changes shape."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from spec.v1.check import (
    FIXTURES,
    V1,
    canonical,
    capability_digest,
    check_decision_call,
    check_fixtures,
    check_golden,
    check_v0alpha1_lock,
    check_v1,
)
from spec.validate import SchemaError
from syberlabs.canonical import canonical as runtime_canonical


class FixtureTests(unittest.TestCase):
    def test_every_fixture_behaves_as_labelled(self):
        errors, count = check_fixtures()
        self.assertEqual(errors, [])
        self.assertGreater(count, 40)

    def test_every_schema_has_fixtures(self):
        schemas = {path.name for path in V1.glob("*.schema.json")} - {"event.schema.json", "port.schema.json"}
        covered = {json.loads(path.read_text(encoding="utf-8"))["schema"] for path in FIXTURES.glob("*.json")}
        self.assertEqual(schemas - covered, set())

    def test_every_fixture_file_has_both_polarities(self):
        for path in FIXTURES.glob("*.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(doc.get("valid"), path.name)
            self.assertTrue(doc.get("invalid"), path.name)


class DigestTests(unittest.TestCase):
    def test_canonical_encoding_matches_the_runtime(self):
        value = {"b": [1, {"z": "é", "a": None}], "a": True}
        self.assertEqual(canonical(value), runtime_canonical(value))

    def test_approval_is_outside_the_digest(self):
        doc = json.loads((FIXTURES / "capability.json").read_text(encoding="utf-8"))
        manifest = next(case["instance"] for case in doc["valid"] if case["instance"].get("approval"))
        approved = dict(manifest, approval="approved")
        self.assertEqual(capability_digest(manifest), capability_digest(approved))

    def test_changing_the_effect_changes_the_digest(self):
        doc = json.loads((FIXTURES / "capability.json").read_text(encoding="utf-8"))
        manifest = doc["valid"][0]["instance"]
        self.assertNotEqual(capability_digest(manifest), capability_digest(dict(manifest, effect="write")))


class RuleTests(unittest.TestCase):
    """The invariants the design states, each as one refusal."""

    def _capability(self):
        doc = json.loads((FIXTURES / "capability.json").read_text(encoding="utf-8"))
        return json.loads(json.dumps(doc["valid"][3]["instance"]))

    def test_destructive_capability_cannot_be_auto_approved(self):
        manifest = self._capability()
        manifest.update(effect="destructive", approval="auto")
        with self.assertRaises(SchemaError):
            check_v1(manifest, "capability.schema.json")

    def test_read_capability_may_be_auto_approved(self):
        manifest = self._capability()
        manifest.update(effect="read", approval="auto")
        check_v1(manifest, "capability.schema.json")

    def test_approve_ask_never_defaults_but_keys_ask_may_default_to_inaction(self):
        approve = {
            "id": "ask_c-1_seq-3", "key": "approve", "owner": "sykosyber", "opened": "2026-11-02",
            "expires": "2026-11-03", "question": "Publish the wheel?", "options": ["publish", "hold"],
            "binds_to": "0" * 64,
        }
        check_v1(approve, "ask.schema.json")
        with self.assertRaises(SchemaError):
            check_v1(dict(approve, default_on_silence="hold"), "ask.schema.json")
        keys = {key: value for key, value in approve.items() if key != "binds_to"}
        check_v1(dict(keys, id="ASK-099", key="keys", default_on_silence="hold"), "ask.schema.json")

    def test_jev_needs_no_revision_but_kev_does(self):
        call = json.loads((FIXTURES / "decision-call.json").read_text(encoding="utf-8"))["valid"][1]["instance"]
        check_v1(call, "decision-call.schema.json")
        kev = dict(call, provider={"name": "Kev", "model": "kev-latest"})
        with self.assertRaises(SchemaError):
            check_v1(kev, "decision-call.schema.json")

    def test_http_method_fixes_the_lowest_effect(self):
        manifest = self._capability()
        manifest.update(transport={"kind": "http", "mode": "server_broker", "method": "DELETE", "origin": "https://x.test"},
                        effect="write", approval="pending")
        with self.assertRaises(SchemaError):
            check_v1(manifest, "capability.schema.json")
        check_v1(dict(manifest, effect="destructive"), "capability.schema.json")

    def test_every_valid_decision_call_passes_the_count_rules(self):
        for case in json.loads((FIXTURES / "decision-call.json").read_text(encoding="utf-8"))["valid"]:
            check_decision_call(case["instance"])


class CompatibilityTests(unittest.TestCase):
    def test_v0alpha1_golden_events_validate_under_v1(self):
        errors, count = check_golden()
        self.assertEqual(errors, [])
        self.assertGreater(count, 0)

    def test_v1_event_is_the_v0alpha1_envelope(self):
        v1 = json.loads((V1 / "event.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(v1["$ref"], "../event.schema.json")
        self.assertNotIn("properties", v1)

    def test_v0alpha1_schema_files_match_their_lock(self):
        self.assertEqual(check_v0alpha1_lock(), [])

    def test_every_v1_schema_has_a_v1alpha1_id(self):
        for path in V1.glob("*.schema.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(schema["$id"].startswith("https://sdk.syberlabs.space/v1alpha1/"), path.name)


if __name__ == "__main__":
    unittest.main()
