# Operator page and refusal report

Two read-only views over a kit's journal. Neither appends an event, moves a ref,
runs a check, or calls a provider; both read through the same session API the
terminal commands use (`Kit.open`, `Thread.status`, `Thread.history`,
`Session.candidates`, `Session.side_channel`). They need Python 3.11+ and `git`,
no dependency, no network, no account.

```sh
PYTHONPATH=. python -m syberlabs.dashboard --home .syberlabs --out dashboard.html
PYTHONPATH=. python -m syberlabs.refusals  --home .syberlabs            # text
PYTHONPATH=. python -m syberlabs.refusals  --home .syberlabs --json     # a list of objects
```

Both take `--repo` (default `.`); `refusals` also takes `--thread ID-OR-PREFIX`.
They are modules today. Wiring them as `syberlabs dashboard` and
`syberlabs refusals` subcommands in `syberlabs/cli.py` is a separate work package.

## The page

One self-contained HTML file: inline CSS, no script, no external stylesheet or
font, light and dark through `prefers-color-scheme`, tables that scroll
sideways on a phone. Every value is passed through `html.escape`, because
objectives, candidate notes and provider names are free text that a provider
or a person typed.
