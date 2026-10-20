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

| Section | Computed from |
| --- | --- |
| Summary tiles | threads by state (`in_progress`, `complete`, `cancelled`); candidates proposed, accepted (promoted), refused (newest promotion decision `denied`); refusals (denied decisions); effects claimed, `applied`, `not_applied`, `pending`; certificates, when any exist |
| Threads | id, objective on one line, class, contract, state, candidate count, accepted candidate, chain head, time of the last event |
| Candidates by thread | one expandable block per thread: provider `name@revision`, operator and lineage, files and diff bytes, each check's state and duration, evidence state, promotion state with the decision reason when refused, the commit; plus base, target ref, event count, chain verification, open items and next steps as `syberlabs status` prints them |
| Effects | every `effect_started` claim with its settlement: `applied` (`effect_succeeded`, or a verified `reconciled`), `not_applied` (`effect_rejected`, whether a `NoWrite` refusal or a reconciliation that found the destination unchanged), or `pending` (an `effect_unknown` or `reconciliation_checked` with no settlement yet, or a claim with no outcome at all); claim time, settlement time, destination, external id and link when the effect reported one |
| Refusals | the refusal report below, as a table |
| Certificates | files under `<home>/certificates/*.json`: the envelope is decoded to show thread, candidate and issue time. The page lists them; it does not verify signatures. |
| Footer | journal path, chain head per thread, generation time, event and thread counts |

A thread whose journal no longer verifies is shown as `unreadable (journal_corrupt)`
with its first-recorded objective, never dropped from the page.

The page is a projection of the record, so two renderings of the same journal
differ only in the generation time. `dashboard.collect(kit)` returns the model as
plain dictionaries for a program that wants the numbers without the HTML.

## The refusal report

A refusal is a `decision` event with status `denied`. `refusals.refusals(kit)`
returns one dictionary per refusal, oldest first, with:

- `thread`, `objective`, `act_class`, `seq`, `at`, `event_hash`
- `proposal_id`, `action`, `actor`, `origin`, `phase` (`propose`, or `commit` when
  the recheck at commit time refused)
- `rule`: the deciding rule id, read from the journal's side channel, where the
  kernel keeps it outside the hashed decision body (`syberlabs/admission.py`)
- `reason`: the reason code, for example `candidate_check_failed:tests`
- `candidate`, `commit`, `provider`: the candidate the proposal named (for a
  publishing action, the candidate whose commit it named)
- `checks`: name, state, exit code, duration and SHA-256 output digest of each
  check in the newest evaluation of that candidate that existed when the decision
  was made, and `evaluation_hash`, the hash of that evaluation event. Empty when
  the candidate had not been evaluated.

`refusals.render_text(rows)` is the terminal form. `needs_approval` is a hold,
not a refusal, and is not listed; it appears on the page as a candidate's
promotion state.

## Claim ceiling

Implemented and tested (`tests/test_dashboard.py`: a thread with one accepted and
one refused candidate, a certificate folder, a corrupt journal, an empty home;
each test asserts the journal is byte-identical before and after rendering).
Not measured against a large journal; the page reads every event of every thread
on each run.
