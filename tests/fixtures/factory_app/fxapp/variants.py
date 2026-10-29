"""Entrypoints the harness tests use beside main.run: a different effect, a large one, a non-JSON one."""

BIG = {"rows": ["x" * 100] * 1000}


def other(ctx, payload):
    return ctx.run("http.other", lambda: 1)


def big(ctx, payload):
    return ctx.run("big", lambda: BIG)


def odd(ctx, payload):
    return ctx.run("odd", lambda: {1, 2})


def marker_only(ctx, payload):
    return ctx.marker("answer", 42)
