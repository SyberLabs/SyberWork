"""Someone rewrites the actor on a stored event after a release is recorded."""

from __future__ import annotations

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.world import Ledger


class TamperedChain:
    id = "tampered_chain"
    expected_failures: list[str] = []

    def world(self):
        return Ledger()

    def contract(self):
        return {
            "id": "release-note",
            "version": 1,
            "inputs": {"version": "string"},
            "actions": {
                "record_checks": {
                    "required_facts": [{"key": "checks", "source": "ci", "verified": True}],
                    "arguments": {"version": "fact:checks.version"},
                },
            },
            "acceptance": [{"id": "recorded", "kind": "effect", "action": "record_checks"}],
        }

    def policy(self):
        return {"version": 1, "actions": {"record_checks": {"roles": ["engineer"]}}}

    def play(self, host, world):
        host.create_case("release-note", {"version": "1.4.2"}, "engineer")
        host.observe("checks", {"version": "1.4.2"}, "ci", "build-90", "engineer", verified=True)
        host.propose("record_checks", {"version": "1.4.2"}, "engineer", ["engineer"])
        host.ids["review"] = host.ids["record_checks"]
        host.commit("review", "engineer")
        host.finish()
        host.tamper()

    def probes(self):
        return [
            Probe("tamper_detected", code="tamper_detected"),
            Probe("original_chain_valid", chain_valid=True),
        ]


SCENARIO = TamperedChain()
