"""The bank debited, then the status call says the write did not happen.

Reconciliation trusts that status. A second payment is then admitted. This
probe expects one debit. On the current SDK the second debit lands, and the
probe fails.
"""

from __future__ import annotations

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.world import Ledger

INVOICE = {"id": "INV-LIVE", "total": 4000}


class LyingReconciler:
    id = "lying_reconciler"
    expected_failures = ["single_debit"]

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

        def apply(ledger, args, key):
            ledger.pay({"id": args["invoice_id"], "total": args["amount"]}, key)
            raise TimeoutError("debited and dropped")

        def status(ledger, args, key):
            return "not_applied", {}

        host.bind_executor("send_wire", apply, status)
        host.propose("send_wire", {"invoice_id": INVOICE["id"], "amount": INVOICE["total"]}, "operator", ["operator"])
        host.ids["pay"] = host.ids["send_wire"]
        host.approve("pay", "treasurer", ["treasurer"])
        host.commit("pay", "operator")
        host.reconcile("pay", "manager", ["manager"])
        host.propose("send_wire", {"invoice_id": INVOICE["id"], "amount": INVOICE["total"]}, "operator", ["operator"])
        host.ids["pay"] = host.ids["send_wire"]
        host.approve("pay", "treasurer", ["treasurer"])
        host.commit("pay", "operator")
        host.finish()

    def probes(self):
        return [
            Probe("single_debit", payments=1, spent=INVOICE["total"]),
            Probe("chain_valid", chain_valid=True),
        ]


SCENARIO = LyingReconciler()
