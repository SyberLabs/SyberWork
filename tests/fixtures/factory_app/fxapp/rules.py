"""Rules: pure decisions over a record."""


def normalize(record):
    record["part"] = record["part"].upper()
    return record


def decide(record):
    if record["quantity"] <= 0:
        return "reject"
    if record["quantity"] > 100:
        return "review"
    return "order"


def label(decision):
    return f"decision:{decision}"
