# Preregistration: four upstream-derived dependency bugs

Owner: Seth Carlson. Acting agent: Codex. October 15, 2026, Pacific time.
Status: freeze and push this document, JSON, source and checks before any model
trial. No model pilots were used to select or prepare these four cases.
Prior results remain separate and visible: original **3/3 versus 3/3**;
hard-v2 **5/5 versus 5/5**, five of six planned pairs, T04 context unavailable.

## Question and selection

Does adding exact-version GitHits context change host-verified solves on four
real, upstream-confirmed bugs, given the same bounded model and call budget?
This extends the existing runner, CommandProvider repair loop, host evaluator
and evidence format. It does not test unrestricted agents or interactive MCP.

The sampling frame is Werkzeug's public release history and linked issues or
merged fixes, inspected before any model trials. Select four reproducible,
network-free, pure-Python cases affecting the same installable version 3.1.3:
multipart framing, URL conversion, routing and bounded streams. This is an
operator-selected convenience sample in one dependency, not a random or
representative sample. Feasibility and module size drove selection; model
success did not. Do not claim these are harder than earlier tasks by solve rate.

- **U01:** [issue 3065](https://github.com/pallets/werkzeug/issues/3065),
  CR/LF split across a multipart chunk, with regression protection for payload
  CR bytes and partial closing delimiters. Related upstream repair history:
  [77bde333](https://github.com/pallets/werkzeug/commit/77bde3337286c405280bef1870442be7f235393c),
  [1049dd6b](https://github.com/pallets/werkzeug/commit/1049dd6b2a363e1ef302b4161c340fb8582f627a),
  [f282943d](https://github.com/pallets/werkzeug/commit/f282943ddc687e613b7ee79b4677e4961c32ae15).
- **U02:** [issue 3189](https://github.com/pallets/werkzeug/issues/3189),
  empty URL username/password, plus explicit port zero from the same upstream
  [repair 4da3786f](https://github.com/pallets/werkzeug/commit/4da3786f1443cbd6efd3f4107bcd5b4ab121f122).
- **U03:** [merged fix 3254](https://github.com/pallets/werkzeug/pull/3254),
  incorrect converter capture ordering with ten or more converters in one
  segment; [repair 12abecb2](https://github.com/pallets/werkzeug/commit/12abecb2de7ddf4767570c67dc3a5a1fc5b9c81d).
- **U04:** [merged fix 3253](https://github.com/pallets/werkzeug/pull/3253),
  LimitedStream short reads resize a caller bytearray;
  [repair 05ff3fe4](https://github.com/pallets/werkzeug/commit/05ff3fe46845c8650e74c71b34f3f881392c478b).

Each broken file is the **complete, unmodified upstream module**, with its
original imports, from tag 3.1.3, commit
`6389612fd1ee1bd93579eed5026e8fd471d04abd`. Blob identities and SHA-256 hashes
are frozen in `upstream-v3/U01.json` through `U04.json`. Host loading restores
the original module name inside installed Werkzeug, initializing its package
first. Only this module, materialized at `src/integration.py`, is mutable.
This is a module-level upstream repair experiment, not a full upstream-suite
run or a submitted upstream fix. The BSD license accompanies copied source.

Checks are authored reproductions and regression extensions of the cited
bugs, not claimed to be verbatim upstream tests. Five test methods per case
cover the reported failure and normal neighboring behavior. For U01, the
upstream fixed module still failed a valid chunk split after the first closing
dash. Before freezing, extend that reference's lookbehind by two bytes and
retain that test. Its reference is explicitly an authored extension of the
upstream fix, not a claim that the published upstream fix passes all our checks.
The other references backport the cited code fixes to 3.1.3. Reference fixes
are validation oracles only and never supplied to either arm.

## Frozen procedure and budget

Four tasks, two arms, one repeat: eight planned cells. Same requested model
`gpt-6.1-sol`, high effort, existing Codex CLI adapter and login, **two model
calls per arm**, stop at first host-verified acceptable candidate. Maximum 16
calls, no seed-controlled model sampling or immutable served-model guarantee.
Use order seed 20261015 to shuffle arms within each task. Each call uses a
fresh session, no shell/web/filesystem tools, and the existing JSON patch
schema. No task may be added, dropped, retuned or replaced after model outcomes.

Both arms share each task's base commit, objective, dependency manifest,
mutable scope, max diff size, checks and feedback rule. Initial source must
fail behavior with correct installed versions; reference must pass all host
checks and admission preview. Freeze every Python source file, fixtures,
requirements and this document by normalized-LF digest in the registration.
Require a clean committed checkout for preflight and matrix. Freeze again only
under a new named registration, never by overwriting this one after outcomes.

The treatment receives only the preregistered source ranges from **Werkzeug
3.1.3**, served by authenticated GitHits reads through private accounting. The
five planned reads are fixed in fixture JSON: multipart's caller, URL internal
helpers, routing compiler/converters and request stream caller. They add
surrounding pinned implementation; the target module is already given to both
arms. Do not supply later fixed code or issue/PR content to treatment. Verify
served repository, full tag commit, version and exact excerpt line content
against the independently frozen upstream snapshot. No alternate versions,
ordinary-doc substitutes or unaccounted fallback. Calls are matched; input
tokens and dollars are not. No equal-size ordinary-doc control is included.

Run an authenticated, model-free preflight after publication. Preserve its
entire receipt manifest and availability before the matrix. Then clear the
token and reuse these exact contexts without new GitHits reads. Predeclare
availability handling now: **run all four baselines**, and each treatment only
if its complete context passes validation. Keep null solved and zero calls for
unavailable treatments; publish coverage and standalone baselines. Score only
completed matched pairs, with denominator out of four planned. Any provider
error is missing data, not an evaluated failed patch. No context-based task
replacement or post-preflight amendment is needed or allowed under this rule.

## Blinded checks and inspection boundary

First calls receive opaque U01-U04 objectives, complete mutable module and
pins. Acceptance-check source, reference repairs, issue identifiers and fixed
commits are withheld. Opaque cell directories and an unbranded context preamble
hide arm names in prompts. A model can notice added context; supplier identity
cannot be guaranteed hidden from all source text. Each model session is fresh.

Host check programs are deterministic and receive **no arm label**. They use
the same code and actual installed dependency for both candidates. The host
runs checks itself on the exact proposed Git tree; a model's claims never
count. Repair feedback contains the same bounded failed-check output policy
in both arms. No subjective reviewer chooses a winner. The author/operator
knows the task and arm mapping: this is **partial blinding**, not double-blind
or independent authoring. The operator may watch completed-cell counts but
must not inspect outcomes or patches until the entire eight-cell matrix ends.

## Reporting and stopping rules

Primary outcome: host-verified solves within the two-call cap for completed
matched pairs, plus GitHits-only and baseline-only solves. Publish all eight
rows, context availability, first/second-call counts, raw prompts/output,
host events/checks, base/candidate/tree identities, bundles and hash manifests.
Replay every saved tree without models, checking scope, pins, unchanged tests,
event chains and recorded pass/fail states. Retain failed candidates too.

If the run interrupts, preserve partial evidence; do not silently restart or
pool a second attempt. No interim inspection or success-driven stopping. If
the baseline again saturates, report that ceiling without reducing the call
budget or adding tasks. Keep both earlier ties prominent and unpooled. These
historical public fixes may be in model training data; memorization is unknown.
The result is same-host measurement/replay, not external validation, a security
sandbox, an estimate of general parity/uplift, or performance on all upstream
tests. Publish only to `bench/githits-verified`; no PR, merge or deployment.
