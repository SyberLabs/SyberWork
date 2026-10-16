# Upstream-v3: preregistered; paired run awaiting authentication

Draft status report — updated October 16, 2026, Pacific time.
Owner: Seth Carlson. Acting agent: Codex.

**No new model comparison has run.** The four-case registration was committed
and pushed at [`0ab8736`](https://github.com/SyberLabs/SyberWork/commit/0ab87364cbe5c8043b9343803a7c6fe2f75a248b)
before any model trials. This host has no configured GitHits token or callable
authenticated GitHits connection. The unused masked prompt was closed without
starting preflight. No new GitHits requests, treatment contexts, baseline model
calls or paired outcomes exist. This is a local credential blocker, not a
measured GitHits package-coverage failure or a model failure.

The two earlier results remain unchanged and separate: **original 3/3 versus
3/3**, three matched pairs; **hard-v2 5/5 versus 5/5**, five of six planned
pairs with T04 context unavailable. Neither establishes general parity or
uplift. See [the main brief](REPORT.md) and [phone brief](phone.html).

## What is ready

- Four real upstream-confirmed Werkzeug bugs: multipart chunk framing
  ([3065](https://github.com/pallets/werkzeug/issues/3065)), empty URL components
  ([3189](https://github.com/pallets/werkzeug/issues/3189)), routing capture order
  ([3254](https://github.com/pallets/werkzeug/pull/3254)), and bounded-stream
  short reads ([3253](https://github.com/pallets/werkzeug/pull/3253)).
- Complete, unmodified upstream starting modules at Werkzeug 3.1.3, with
  blob/SHA-256 identities, source/fix links and BSD license. Checks load each
  module under its original package identity against the installed dependency.
- Four broken starts that fail behavior with correct pins; four reference
  repairs that pass all 20 authored host test methods. Reference success is
  fixture validation, not model success. U01's oracle explicitly extends an
  upstream repair to retain a partial closing-delimiter regression case.
- A [pushed registration](PREREGISTRATION-upstream-v3.md) with 150 source/data
  hashes, eight planned cells, requested `gpt-6.1-sol` at high effort, two calls
  per arm, one repeat and a 16-call maximum. A fresh remote checkout verified
  all hashes before any trial. No model pilots were used for case selection.
- Hidden check/reference source, opaque cells, unbranded treatment context
  and arm-unaware deterministic host checks. The operator is unblinded and a
  model can notice extra context: this is partial blinding.
- Model-free preflight, frozen-context matrix and exact-tree replay modes
  using the existing benchmark/host seams. Only benchmark files changed;
  there is no new core runtime dependency or protocol change.

## Remaining action

On October 16, all five CI jobs for handoff commit `c189956` were confirmed
successful. The [launcher](run-upstream-v3.ps1) now defaults to no prompting and
can reuse a saved authenticated preflight with `-Access`. Six offline launcher
tests passed, including credential clearing, cache reuse and stopping after
failure. A check with the real pinned interpreter stopped without a prompt
when credentials were absent. All 150 original frozen hashes still match.
These are workflow checks, not new model results; there is still no upstream-v3
authenticated preflight or matrix on this host.

On a machine with approved private accounting configured and an available
GitHits token, follow [README.md](README.md) using the frozen source. The
authenticated preflight makes five preregistered reads, then the matrix reuses
the validated receipts after clearing the token. No alternate source/version
or direct unaccounted fallback is permitted. The predeclared rule retains all
four baselines, marks unavailable treatments null/zero-call, and scores only
completed pairs out of four planned. Do not start model pilots to fill the wait.

After the complete eight-cell matrix, inspect outcomes and patches, replay
all preflight and model candidate trees, publish every row and push only the
benchmark branch. Do not overwrite this registration, change tasks after
outcomes, or pool a rerun into either prior tie. Historical fixes may be in
training data; this one-package convenience sample is not representative.
