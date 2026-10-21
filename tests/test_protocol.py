"""Schema checks for published examples and for events the golden run emits."""

import copy
import json
import unittest

from spec.validate import PRICED_CONTRACT, SPEND_SEARCH_FINISHED, SchemaError, check, validate_examples, validate_golden
from syberlabs.evolution import check_evolution, search_record


def _registered_with_spend() -> dict:
    return {
        "id": "c1", "commit": "a" * 40, "tree": "b" * 40, "base": "c" * 40, "parents": [],
        "operator": "patch", "provider": {"name": "evolve", "revision": "1"},
        "changed_paths": ["src/a.py"], "scope_violations": [], "limit_violations": [],
        "diff": {"digest": "d" * 64, "bytes": 12, "files": 1}, "signal": None, "note": "",
        "actor": "search", "spend": {"dollars": 0.0405, "seconds": 1.5, "host_observed": True},
    }


class ProtocolExamples(unittest.TestCase):
    def test_examples_match_v0alpha1(self):
        self.assertEqual(validate_examples(), [])

    def test_golden_run_events_match_v0alpha1(self):
        self.assertEqual(validate_golden(), [])


class AdditiveFields(unittest.TestCase):
    """2026-10-20: kind on a check, the dollar budget, and the two spend records (GAPS 30 and 31)."""

    def test_a_priced_critic_bearing_contract_validates(self):
        check(PRICED_CONTRACT, "contract.schema.json")
        check(PRICED_CONTRACT["evolution"], "evolution.schema.json")

    def test_the_runtime_accepts_the_priced_budget_but_not_kind_yet(self):
        # The budget fields are live in evolution.check_evolution; kind is schema-only until the
        # packet that owns evolution.py reads it. The name convention stays authoritative.
        without_kind = copy.deepcopy(PRICED_CONTRACT)
        for spec in without_kind["evolution"]["evaluation"]["checks"].values():
            spec.pop("kind")
        check_evolution(without_kind)
        with self.assertRaises(Exception):
            check_evolution(PRICED_CONTRACT)

    def test_spend_validates_on_both_records(self):
        check({"kind": "candidate_registered", "body": _registered_with_spend()}, "event-body.schema.json")
        check({"kind": "search_finished", "body": SPEND_SEARCH_FINISHED}, "event-body.schema.json")
        null_dollars = _registered_with_spend()
        null_dollars["spend"]["dollars"] = None
        check({"kind": "candidate_registered", "body": null_dollars}, "event-body.schema.json")

    def test_the_runtime_search_finished_record_validates(self):
        # The body evolution.search_record writes under a priced contract, not a transcription of it.
        contract = {k: v for k, v in PRICED_CONTRACT.items()}
        contract["evolution"] = copy.deepcopy(PRICED_CONTRACT["evolution"])
        for spec in contract["evolution"]["evaluation"]["checks"].values():
            spec.pop("kind")
        registered = _registered_with_spend()
        history = [
            {"kind": "search_started", "body": {"id": "s1", "provider": {"name": "evolve", "revision": "1"},
                                                "operators": ["patch"], "budget": {"max_candidates": 2, "max_evaluations": 2, "max_seconds": 60},
                                                "base": "c" * 40, "context_digest": None, "actor": "search"}},
            {"kind": "candidate_registered", "body": registered},
        ]
        raw = {key: value for key, value in SPEND_SEARCH_FINISHED.items() if key != "actor"}
        raw["candidates"], raw["recommended"] = ["c1"], ["c1"]
        body = search_record(contract, history, "finished", raw, "search")
        self.assertEqual(set(body["spend"]), {"dollars", "seconds", "evaluations", "candidates", "host_observed", "unknown_models"})
        check({"kind": "search_finished", "body": body}, "event-body.schema.json")

    def test_spend_fields_are_exact(self):
        extra = _registered_with_spend()
        extra["spend"]["evaluations"] = 1
        with self.assertRaises(SchemaError):
            check({"kind": "candidate_registered", "body": extra}, "event-body.schema.json")
        short = copy.deepcopy(SPEND_SEARCH_FINISHED)
        del short["spend"]["unknown_models"]
        with self.assertRaises(SchemaError):
            check({"kind": "search_finished", "body": short}, "event-body.schema.json")
        negative = copy.deepcopy(SPEND_SEARCH_FINISHED)
        negative["spend"]["dollars"] = -1
        with self.assertRaises(SchemaError):
            check({"kind": "search_finished", "body": negative}, "event-body.schema.json")

    def test_check_kind_is_command_or_critic(self):
        for kind in ("command", "critic"):
            check({"kind": kind, "argv": ["python", "x.py"]}, "evolution.schema.json#/$defs/check")
        with self.assertRaises(SchemaError):
            check({"kind": "other", "argv": ["python", "x.py"]}, "evolution.schema.json#/$defs/check")

    def test_a_negative_or_unknown_price_fails(self):
        budget = {"max_candidates": 1, "prices": {"m": {"input_per_million": -1, "output_per_million": 1}}}
        with self.assertRaises(SchemaError):
            check(budget, "evolution.schema.json#/properties/budget")
        budget["prices"]["m"] = {"input_per_million": 1, "output_per_million": 1, "per_call": 1}
        with self.assertRaises(SchemaError):
            check(budget, "evolution.schema.json#/properties/budget")
        budget["prices"]["m"] = {"input_per_million": 1}
        with self.assertRaises(SchemaError):
            check(budget, "evolution.schema.json#/properties/budget")

    def test_existing_required_lists_and_closure_are_kept(self):
        with open("spec/evolution.schema.json", encoding="utf-8") as handle:
            evolution = json.load(handle)
        with open("spec/candidate.schema.json", encoding="utf-8") as handle:
            candidate = json.load(handle)
        self.assertIs(evolution["additionalProperties"], False)
        self.assertIs(evolution["properties"]["budget"]["additionalProperties"], False)
        self.assertIs(evolution["$defs"]["check"]["additionalProperties"], False)
        self.assertIs(candidate["$defs"]["registered"]["additionalProperties"], False)
        self.assertIs(candidate["$defs"]["search_finished"]["additionalProperties"], False)
        self.assertEqual(evolution["properties"]["budget"]["required"], ["max_candidates"])
        self.assertEqual(evolution["$defs"]["check"]["required"], ["argv"])
        self.assertNotIn("spend", candidate["$defs"]["registered"]["required"])
        self.assertNotIn("spend", candidate["$defs"]["search_finished"]["required"])


if __name__ == "__main__":
    unittest.main()
