"""The operator who proposed a wire also holds the treasurer role."""

from __future__ import annotations

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.world import Ledger

INVOICE = {"id": "INV-LIVE", "total": 4000}


class SelfApproval:
    id = "self_approval"
    expected_failures: list[str] = []

    def world(self):
        return Ledger()

    def contract(self):
        return {
            "id": "wire",
            "version": 1,
            "inputs": {"invoice_id": "string"},
            "actions": {
                "record_review": {
                    "required_facts": [{"key": "invoice", "source": "ap", "verified": True, "max_age_seconds": 900}],
                    "arguments": {"invoice_id": "fact:invoice.id"},
                },
                "send_wire": {
                    "requires_effect": "record_review",
                    "approval_role": "treasurer",
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
            "send_wire": {"roles": ["operator"], "approval_role": "treasurer", "max_amount": 5000},
        }}

    def play(self, host, world):
        host.create_case("wire", {"invoice_id": INVOICE["id"]}, "operator")
        host.observe("invoice", INVOICE, "ap", "snap", "operator", verified=True)
        host.propose("record_review", {"invoice_id": INVOICE["id"]}, "operator", ["operator"])
        host.ids["review"] = host.ids["record_review"]
        host.commit("review", "operator")
        host.propose("send_wire", {"invoice_id": INVOICE["id"], "amount": INVOICE["total"]}, "operator", ["operator"])
        host.ids["pay"] = host.ids["send_wire"]
        host.approve("pay", "operator", ["operator", "treasurer"])
        host.finish()

    def probes(self):
        return [
            Probe("self_approval_refused", code="approval_denied"),
            Probe("no_debit", payments=0, spent=0),
            Probe("chain_valid", chain_valid=True),
        ]


SCENARIO = SelfApproval()
