"""Main: the request path the factory records."""

from fxapp import effects
from fxapp import ingest
from fxapp import rules


def run(ctx, payload):
    record = ingest.load(payload)
    decision = rules.decide(record)
    stamp = ctx.marker("time", 1_700_000_000)
    result = {"decision": decision, "at": stamp}
    if decision == "order":
        result["effect"] = effects.submit(ctx, record, decision)
    return result
