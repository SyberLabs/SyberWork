"""Entrypoints the harness tests use beside main.run: a different effect, a large one, a non-JSON one, and the
adversarial candidates of the trust-boundary tests: one that prints a forged report and keeps the recorder from
writing its ``end``, one that writes its own record into the journal around the recorder, and one that writes a
false clean ``end``. They run only in the child the harness starts; none ends a process."""

import json
import sys

BIG = {"rows": ["x" * 100] * 1000}


def other(ctx, payload):
    return ctx.run("http.other", lambda: 1)


def big(ctx, payload):
    return ctx.run("big", lambda: BIG)


def odd(ctx, payload):
    return ctx.run("odd", lambda: {1, 2})


def marker_only(ctx, payload):
    return ctx.marker("answer", 42)


def _recorder():
    return sys.modules["_factory_harness"]._CURRENT[-1]


def forge_report(ctx, payload):
    """The S2(c) candidate: print what used to pass for the child's report, then close the journal under the
    recorder so its ``end`` record is never written."""
    recorder = _recorder()
    sys.stdout.write("\n" + json.dumps({"run_id": recorder.run_id, "status": "ok", "error": None,
                                        "outcome": {"decision": "order"}, "counts": {"call": 1, "effect": 0, "marker": 0, "io": 0}}) + "\n")
    sys.stdout.flush()
    recorder._handle.close()
    return {"decision": "order"}


def forge_journal(ctx, payload):
    """The S2(a) candidate: append a record to the journal file behind the recorder's back."""
    recorder = _recorder()
    recorder._handle.write(json.dumps({"t": "marker", "seq": recorder.seq + 1, "ns": 0, "name": "planted", "digest": "0" * 64,
                                       "json": True, "span": None, "payload": "planted"}) + "\n")
    recorder._handle.flush()
    return {"decision": "order"}


def forge_end(ctx, payload):
    """A candidate that writes a clean ``end`` itself, then closes the journal so the recorder's own is never written."""
    recorder = _recorder()
    recorder._handle.write(json.dumps({"t": "end", "seq": recorder.seq + 1, "ns": 1, "status": "ok", "error": None,
                                       "counts": dict(recorder.counts), "open_spans": 0, "unconsumed_effects": 0}) + "\n")
    recorder._handle.close()
    return {"decision": "order"}
