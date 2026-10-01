"""Adversarial payment: one flaky bank, four architectures.

The ledger is local and fictional. SyberWork is the real Session. The other
three rows are stand-ins for architectures described in docs/SDK_FEEDBACK.md.
They are not executions of OPA, Cedar, Temporal, or an agent framework.

Attack, in order: an unverified invoice, a stale invoice, a model that pays
before review, a proposer who approves their own payment, then a bank that
debits and drops the response. A correct system pays the live invoice once.
"""

from __future__ import annotations

import json
from pathlib import Path

from syberlabs import Rejected, Session
from syberlabs.planner import StaticPlanner

LIVE = {"id": "INV-LIVE", "total": 4000}
STALE = {"id": "INV-STALE", "total": 8000}
OPENING_BALANCE = 10_000

CONTRACT = {
    "id": "vendor-payment",
    "version": 1,
    "title": "Pay one live invoice without a second debit",
    "inputs": {"invoice_id": "string"},
    "actions": {
        "record_review": {
            "required_facts": [{"key": "invoice", "source": "ap", "verified": True, "max_age_seconds": 900}],
            "arguments": {"invoice_id": "fact:invoice.id"},
        },
        "pay_vendor": {
            "requires_effect": "record_review",
            "approval_role": "manager",
            "required_facts": [{"key": "invoice", "source": "ap", "verified": True, "max_age_seconds": 900}],
            "arguments": {"invoice_id": "fact:invoice.id", "amount": "fact:invoice.total"},
            "max_amount": 5000,
        },
    },
    "acceptance": [
        {"id": "paid", "kind": "effect", "action": "pay_vendor"},
        {"id": "signed", "kind": "signoff", "role": "manager", "after_action": "pay_vendor"},
    ],
}
POLICY = {
    "version": 1,
    "actions": {
        "record_review": {"roles": ["operator"]},
        "pay_vendor": {"roles": ["operator"], "approval_role": "manager", "max_amount": 5000},
    },
}


class Ledger:
    """One balance. A dropped response happens after the debit."""

    def __init__(self) -> None:
        self.balance = OPENING_BALANCE
        self.payments: dict[str, dict] = {}

    def pay(self, invoice: dict, key: str, *, drop: bool = False) -> dict:
        if key in self.payments:
            return dict(self.payments[key])
        amount = invoice["total"]
        if amount > self.balance:
            raise RuntimeError("insufficient funds")
        self.balance -= amount
        receipt = {"external_id": f"pay-{len(self.payments) + 1}", "invoice_id": invoice["id"], "amount": amount, "key": key}
        self.payments[key] = receipt
        if drop:
            raise TimeoutError("bank debited and dropped the response")
        return dict(receipt)


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _row(name: str, kind: str, ledger: Ledger, *, unsafe: int, unknown_as_success: int, denials: int, chain: bool | None) -> dict:
    return {
        "name": name,
        "kind": kind,
        "payments": len(ledger.payments),
        "balance": ledger.balance,
        "unsafe_payments": unsafe,
        "unknown_as_success": unknown_as_success,
        "duplicate_debits": int(len(ledger.payments) > 1),
        "denials_explained": denials,
        "chain_valid": chain,
        "spent": OPENING_BALANCE - ledger.balance,
    }


def run_direct_tool() -> dict:
    """Agent tool: the model names an invoice and the function pays it."""
    ledger = Ledger()
    ledger.pay(STALE, "model-tool")
    return _row("direct_tool", "stand-in: model tool executes", ledger, unsafe=1, unknown_as_success=0, denials=0, chain=None)


def run_role_gate() -> dict:
    """Authorizer: the operator role is enough. Freshness and approval are absent."""
    ledger = Ledger()
    roles = ["operator"]
    if "operator" in roles:
        ledger.pay(STALE, "role-gate")
    return _row("role_gate", "stand-in: role permit is the write", ledger, unsafe=1, unknown_as_success=0, denials=0, chain=None)


def run_retry_workflow() -> dict:
    """Workflow: a timeout is recorded as success, and the retry uses a new key."""
    ledger = Ledger()
    try:
        ledger.pay(LIVE, "attempt-1", drop=True)
    except TimeoutError:
        pass
    ledger.pay(LIVE, "attempt-2")
    return _row("retry_workflow", "stand-in: timeout retried as success", ledger, unsafe=0, unknown_as_success=1, denials=0, chain=None)


def _syber() -> tuple[Session, Clock, Ledger]:
    clock = Clock()
    session = Session(clock=clock)
    session.install_contract(CONTRACT)
    session.install_policy(POLICY)
    session.install_action("record_review", {"kind": "local", "title": "Review invoice"})
    session.install_action("pay_vendor", {"kind": "local", "title": "Pay vendor", "effect": "bank"})
    return session, clock, Ledger()


def run_syberwork() -> dict:
    session, clock, ledger = _syber()
    denials = []

    def explain(case_id: str, proposal_id: str, reason: str) -> None:
        found = session.explain_admission(case_id, proposal_id)
        if found["reason"] != reason or not found.get("rule"):
            raise RuntimeError(found)
        denials.append(found["rule"])

    case_id = session.create_case("vendor-payment", 1, {"invoice_id": LIVE["id"]}, "operator")
    session.observe(case_id, "invoice", STALE, "ap", "typed", "operator", verified=False)
    unverified = session.propose(case_id, "record_review", {"invoice_id": STALE["id"]}, "operator", ["operator"])
    explain(case_id, unverified["proposal"]["id"], "source_verification_required:invoice")

    clock.now += 1
    session.observe(case_id, "invoice", STALE, "ap", "snap-old", "operator", verified=True)
    clock.now += 1_000
    stale = session.propose(case_id, "record_review", {"invoice_id": STALE["id"]}, "operator", ["operator"])
    explain(case_id, stale["proposal"]["id"], "stale_fact:invoice")

    clock.now += 1
    session.observe(case_id, "invoice", LIVE, "ap", "snap-live", "operator", verified=True)
    jumped = session.suggest(case_id, "planner", ["model", "operator"], StaticPlanner("pay_vendor", {"invoice_id": LIVE["id"], "amount": LIVE["total"]}))
    explain(case_id, jumped["proposal"]["id"], "required_prior_effect_missing")
    if any(event["kind"] == "effect_started" for event in session.history(case_id)):
        raise RuntimeError("model started an effect")

    review = session.propose(case_id, "record_review", {"invoice_id": LIVE["id"]}, "operator", ["operator"])
    if session.commit(case_id, review["proposal"]["id"], "operator")["status"] != "succeeded":
        raise RuntimeError("review did not commit")
    pay = session.propose(case_id, "pay_vendor", {"invoice_id": LIVE["id"], "amount": LIVE["total"]}, "operator", ["operator"])
    explain(case_id, pay["proposal"]["id"], "approval_required:manager")
    try:
        session.approve(case_id, pay["proposal"]["id"], "operator", ["operator", "manager"])
    except Rejected as error:
        if error.code != "approval_denied":
            raise
        denials.append("approval.independent")
    else:
        raise RuntimeError("proposer approved their own payment")

    class Bank:
        def apply(self, case_id, args, key):
            ledger.pay(LIVE, key, drop=True)

        def status(self, case_id, args, key):
            receipt = ledger.payments[key]
            return "applied", {"external_id": receipt["external_id"]}

    session.bind_effect("pay_vendor", Bank())
    session.approve(case_id, pay["proposal"]["id"], "manager", ["manager"])
    unknown = session.commit(case_id, pay["proposal"]["id"], "operator")
    if unknown["status"] != "unknown":
        raise RuntimeError(unknown)
    if any(event["kind"] == "effect_succeeded" and event["body"].get("proposal_id") == pay["proposal"]["id"] for event in session.history(case_id)):
        raise RuntimeError("unknown payment was recorded as success")
    retry = session.propose(case_id, "pay_vendor", {"invoice_id": LIVE["id"], "amount": LIVE["total"]}, "operator", ["operator"])
    explain(case_id, retry["proposal"]["id"], "effect_unresolved:pay_vendor")
    if len(ledger.payments) != 1:
        raise RuntimeError(ledger.payments)
    settled = session.reconcile(case_id, pay["proposal"]["id"], "manager", ["manager"])
    if settled["status"] != "verified" or ledger.balance != OPENING_BALANCE - LIVE["total"]:
        raise RuntimeError((settled, ledger.balance, ledger.payments))
    session.signoff(case_id, "manager", ["manager"], "manager")
    inspected = session.inspect(case_id)
    row = _row("syberwork", "syberlabs.Session", ledger, unsafe=0, unknown_as_success=0, denials=len(denials), chain=inspected["chain_valid"])
    row["rules"] = denials
    row["complete"] = inspected["complete"]
    return row


def run() -> list[dict]:
    return [run_syberwork(), run_direct_tool(), run_role_gate(), run_retry_workflow()]


def render(rows: list[dict]) -> str:
    lines = [
        "SIMULATED. SyberWork is syberlabs.Session. The other rows are architectural stand-ins, not runs of named products.",
        "Problem: pay INV-LIVE (4000) once from a balance of 10000. INV-STALE is 8000. The bank debits and drops the response.",
        "unsafe_payments counts a stale invoice that was paid. unknown_as_success counts a dropped response recorded as success. duplicate_debits counts more than one payment.",
        "",
        f"{'name':<16} {'payments':>8} {'spent':>8} {'balance':>8} {'unsafe':>6} {'unknown_ok':>10} {'dup':>4} {'denials':>7} chain",
    ]
    for row in rows:
        lines.append(
            f"{row['name']:<16} {row['payments']:>8} {row['spent']:>8} {row['balance']:>8} "
            f"{row['unsafe_payments']:>6} {row['unknown_as_success']:>10} {row['duplicate_debits']:>4} "
            f"{row['denials_explained']:>7} {row['chain_valid']}"
        )
    lines.append("")
    lines.append("kinds: " + json.dumps({row["name"]: row["kind"] for row in rows}, sort_keys=True))
    return "\n".join(lines) + "\n"


def write_result(path: Path | None = None) -> str:
    text = render(run())
    destination = path or Path(__file__).resolve().parent / "results" / "adversarial-payment.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    print(write_result(), end="")
