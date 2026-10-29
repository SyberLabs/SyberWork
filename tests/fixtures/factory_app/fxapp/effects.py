"""Effects: the one place the program reaches outside itself. Imports rules, which the declaration does not allow."""

from fxapp import rules


def post_order(record, decision):
    """Stands in for an HTTP call. Replaced by the journal on replay."""
    return {"status": 201, "order_id": f"ord-{record['part'].lower()}-{record['quantity']}", "label": rules.label(decision)}


def submit(ctx, record, decision):
    return ctx.run("http.post_order", post_order, record, decision)
