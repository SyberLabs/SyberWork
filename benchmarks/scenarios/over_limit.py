"""A wire above the contract ceiling."""

from __future__ import annotations

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.world import Ledger


class OverLimit:
    id = "over_limit"
    expected_failures: list[str] = []

    def world(self):
        return Ledger()

    def contract(self):
        return {
            "id": "capped-wire",
            "version": 1,
            "inputs": {"invoice_id": "string"},
            "actions": {
                "send_wire": {
                    "arguments": {"invoice_id": "fact:invoice.id", "amount": "fact:invoice.total"},
                    "max_amount": 5000,
                },
            },
            "acceptance": [{"id": "wired", "kind": "effect", "action": "send_wire"}],
        }

    def policy(self):
        return {"version": 1, "actions": {"send_wire": {"roles": ["operator"], "max_amount": 5000}}}

    def play(self, host, world):
        host.create_case("capped-wire", {"invoice_id": "INV-BIG"}, "operator")
        host.propose("send_wire", {"invoice_id": "INV-BIG", "amount": 9000}, "operator", ["operator"])
        host.explain_last("amount_exceeds_limit")
        host.finish()

    def probes(self):
        return [
            Probe("ceiling_holds", reason="amount_exceeds_limit"),
            Probe("no_debit", payments=0, spent=0),
            Probe("chain_valid", chain_valid=True),
        ]


SCENARIO = OverLimit()
