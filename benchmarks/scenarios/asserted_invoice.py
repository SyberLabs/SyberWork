"""The host marks an invoice verified. The accounts-payable world has no such invoice.

Session.observe(verified=True) is the host's own claim. This probe expects the
payment to be refused. On the current SDK it is not, and the probe fails.
"""

from __future__ import annotations

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.world import Ledger

GHOST = {"id": "INV-GHOST", "total": 4000}


class AssertedInvoice:
    id = "asserted_invoice"
    expected_failures = ["unknown_invoice_refused"]

    def world(self):
        return Ledger()

    def contract(self):
        return {
            "id": "ghost-invoice",
            "version": 1,
            "inputs": {"invoice_id": "string"},
            "actions": {
                "record_review": {
                    "required_facts": [{"key": "invoice", "source": "ap", "verified": True, "max_age_seconds": 900}],
                    "arguments": {"invoice_id": "fact:invoice.id"},
                },
                "send_wire": {
                    "requires_effect": "record_review",
                    "required_facts": [{"key": "invoice", "source": "ap", "verified": True, "max_age_seconds": 900}],
                    "arguments": {"invoice_id": "fact:invoice.id", "amount": "fact:invoice.total"},
                    "max_amount": 5000,
                    "executor": "bank",
                },
            },
            "acceptance": [{"id": "wired", "kind": "effect", "action": "send_wire"}],
        }

    def policy(self):
        return {"version": 1, "actions": {
            "record_review": {"roles": ["operator"]},
            "send_wire": {"roles": ["operator"], "max_amount": 5000},
        }}

    def play(self, host, world):
        host.create_case("ghost-invoice", {"invoice_id": GHOST["id"]}, "operator")
        host.observe("invoice", GHOST, "ap", "asserted", "operator", verified=True)

        def apply(ledger, args, key):
            ledger.pay({"id": args["invoice_id"], "total": args["amount"]}, key)

        host.bind_executor("send_wire", apply)
        host.propose("record_review", {"invoice_id": GHOST["id"]}, "operator", ["operator"])
        host.ids["review"] = host.ids["record_review"]
        host.commit("review", "operator")
        host.propose("send_wire", {"invoice_id": GHOST["id"], "amount": GHOST["total"]}, "operator", ["operator"])
        host.ids["pay"] = host.ids["send_wire"]
        host.commit("pay", "operator")
        host.finish()

    def probes(self):
        return [
            Probe("unknown_invoice_refused", payments=0, spent=0),
            Probe("chain_valid", chain_valid=True),
        ]


SCENARIO = AssertedInvoice()
