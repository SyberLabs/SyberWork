"""External worlds. SyberWork does not own their rows."""

from __future__ import annotations


OPENING_BALANCE = 10_000


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def jump(self, seconds: float) -> None:
        self.now += seconds


class Ledger:
    """One balance. A listed fault applies to the first new debit, then stays spent."""

    def __init__(self, balance: int = OPENING_BALANCE, faults: list[str] | None = None) -> None:
        self.balance = balance
        self.opening = balance
        self.faults = list(faults or [])
        self.spent_faults: list[str] = []
        self.payments: dict[str, dict] = {}

    def pay(self, invoice: dict, key: str) -> dict:
        if key in self.payments:
            return dict(self.payments[key])
        amount = invoice["total"]
        if amount > self.balance:
            raise RuntimeError("insufficient funds")
        self.balance -= amount
        receipt = {
            "external_id": f"pay-{len(self.payments) + 1}",
            "invoice_id": invoice["id"],
            "amount": amount,
            "key": key,
        }
        self.payments[key] = receipt
        if "drop_after_debit" in self.faults and "drop_after_debit" not in self.spent_faults:
            self.spent_faults.append("drop_after_debit")
            raise TimeoutError("bank debited and dropped the response")
        return dict(receipt)

    def snapshot(self) -> dict:
        return {
            "payments": len(self.payments),
            "spent": self.opening - self.balance,
            "balance": self.balance,
            "invoices": [item["invoice_id"] for item in self.payments.values()],
            "duplicate_debits": int(len(self.payments) > 1),
        }
