"""A buyer returns to a quote after the 15-minute freshness window."""

from __future__ import annotations

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.world import Ledger

QUOTE = {"id": "Q-881", "total": 2500}


class StaleQuote:
    id = "stale_quote"
    expected_failures: list[str] = []

    def world(self):
        return Ledger()

    def contract(self):
        return {
            "id": "spare-purchase",
            "version": 1,
            "inputs": {"request_id": "string"},
            "actions": {
                "issue_order": {
                    "required_facts": [{"key": "quote", "source": "supplier", "verified": True, "max_age_seconds": 900}],
                    "arguments": {"quote_id": "fact:quote.id", "amount": "fact:quote.total"},
                    "max_amount": 5000,
                },
            },
            "acceptance": [{"id": "ordered", "kind": "effect", "action": "issue_order"}],
        }

    def policy(self):
        return {"version": 1, "actions": {"issue_order": {"roles": ["buyer"], "max_amount": 5000}}}

    def play(self, host, world):
        host.create_case("spare-purchase", {"request_id": "REQ-4812"}, "buyer")
        host.observe("quote", QUOTE, "supplier", "q-v3", "buyer", verified=True)
        host.jump(1_000)
        host.propose("issue_order", {"quote_id": QUOTE["id"], "amount": QUOTE["total"]}, "buyer", ["buyer"])
        host.explain_last("stale_fact:quote")
        host.finish()

    def probes(self):
        return [
            Probe("stale_quote_refused", reason="stale_fact:quote"),
            Probe("no_debit", payments=0, spent=0),
            Probe("chain_valid", chain_valid=True),
        ]


SCENARIO = StaleQuote()
