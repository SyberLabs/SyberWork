# The critic station

An independent reviewer runs as a host check on every candidate, its findings are recorded in the thread's hash chain, an admission rule refuses acceptance while a high finding stands, and the pull request's `Reviewed-by` line is written from that record. The review is evidence a verifier can re-read, not a comment someone remembers.

## A critic is a check with a schema

Declare it like any other check in the contract's `evolution.evaluation.checks`, under the name `critic` or `critic.<reviewer>`, and list it in `evaluation.required` if it is to block:

```json
"evaluation": {
  "checks": {
    "tests":  {"argv": ["C:/Python312/python.exe", "-m", "unittest", "discover", "-s", "tests", "-q"]},
    "critic": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_rules.py"], "timeout_seconds": 120}
  },
  "required": ["tests", "critic"]
}
```

The name is the marker. The contract validator accepts no `kind` field on a check yet (`spec/GAPS.md` 30), so a check is a critic when its name is `critic` or starts with `critic.`; `critical` is an ordinary check. Name the critic program by an absolute path in the host's own checkout, not a path inside the candidate: the check runs in a temporary worktree of the candidate, and a candidate that could rewrite its own reviewer would review itself.

The host runs the command in the candidate's worktree with the usual scrubbed environment and a JSON request on stdin:

```
{"protocol": "syberlabs.critic/1",
 "candidate": {"id", "commit", "tree", "provider", "operator", "parents", "note"},
 "base": "<thread base commit>",
 "result": [<the checks that ran before this one, with state, exit code, digest and tail>],
 "changed_paths": [...], "diff": "<unified diff against the base, up to 1 MB>",
 "objective": "...", "contract": {"acceptance": [...], "scope": {"paths": [...], "exclude": [...]}}}
```

Checks run in the order the stored contract lists them, which is by name because a published contract is kept as canonical JSON; `critic` therefore runs before `tests` and sees `result: []`, while a check named to sort before it (`a_lint`) is in `result` with its state, exit code, digest and tail. The command must print a findings document (`spec/finding.schema.json`, identity `sdk.syberlabs.space/critic/1`):

```json
{"version": 1, "reviewer": {"name": "rules", "revision": "1"},
 "findings": [{"severity": "high|medium|low", "file": "src/export.py", "line": 12,
               "claim": "what is wrong", "falsifier": "what would show the claim false", "acceptance_ref": "accepted"}],
 "summary": "optional, 4000 characters at most"}
```

Exit code 0 means the document is the result. Any other exit code is a check `error`, as for every check, with the same record: state, exit code, duration, a SHA-256 of all output, and a 4 KB tail. Nothing in the critic's prose is read by admission.

## What the host checks itself

- **The document conforms.** `syberlabs.critic.parse` refuses anything else and the check becomes `error` with output `findings_invalid:<path>` (`json`, `version`, `reviewer/revision`, `findings/3/severity`, ...). The reviewer's name is a lowercase name and its revision is printable, so neither can carry a line break into a pull request body.
- **The reviewer is independent.** The reviewer's `name@revision` must differ from the candidate's provider `name@revision` as the candidate record stores it; a match is `error` with output `reviewer_not_independent`. The host cannot see which program a provider ran, so this is checked on the recorded identities and the observation says `independence: "declared"`. A model reviewing a model: use a different revision, or a different reviewer.
- **The findings are in the chain.** After the evaluation record is written, the host stores each accepted document as an `observed` event: key `critic`, source `check:<name>`, actor `syberlabs.host`, `verified: true`, version = the check's `output_digest`, and value `{candidate, check, reviewer, counts: {high, medium, low}, findings_digest, independence, truncated, summary, findings}`. `findings_digest` is the SHA-256 of the critic's full stdout, which is the digest the evaluation record keeps for that check, so the two events name the same bytes. The value is bounded to 64 KiB: a longer document keeps its counts and digest and drops findings from the end with `truncated: true`. The check's `max_output_bytes` (1 MiB by default) still bounds what the critic may print at all.

## The rule

`critic.no_high_finding` is one admission rule, ordered after `candidate.promotable` and before `approval.required`. It is a no-op unless the promotion's contract lists a critic check in `evaluation.required`, so a contract without one, and every golden trace, is unchanged. `candidate.promotable` has already required that every required check, the critic included, passed on the candidate's exact tree. Then, for each required critic, the rule reads the host's observation for that check result (matched on candidate, check and output digest) and denies with `high_finding:<check>:<count>` while `counts.high > 0`. Medium and low findings never block. A passed critic check with no observation is denied with `critic_observation_missing:<check>`: a review that is not in the record is not evidence.

The verdict from `syberlabs check` says so (`denied: high_finding:critic:1; rule critic.no_high_finding`), `syberlabs findings c1` prints each finding as `severity file:line claim`, and the fix is a new candidate: the critic runs again on the new tree and the new evaluation carries its own observation.

Whether a medium finding should block when the thread's class is C, P or X is an open owner question; the default is no, because class enforcement is a separate decision.

## Reviewed-by

`publish.attribution` adds the critic of the promoting evaluation to the pull request body, from the observation:

```
Reviewed-by: rules@1 (critic check critic: high 0, medium 1, low 0)
```

An independent approver under an `approval_role` still renders as `actor (role)`, and both can appear on the one line, comma-separated. Every part is rendered on one line, so a reviewer name or revision cannot add a field, and the parser refuses a revision with a line break before it can be observed at all. Without either kind of reviewer the line still reads `Reviewed-by: none recorded`.

## The reference critic

`examples/critic_rules.py` is deterministic and model-free: high for a changed path outside the contract's mutable scope (the scope rule already refuses such a candidate, so this proves the plumbing), medium for a changed source file when no test changed in the same candidate. Every finding names its falsifier. It lets the suite and the golden traces run without a model. The model-backed critic below prints the same document and declares a reviewer that is not the candidate's provider. Whether the rule stays blocking or becomes advisory is for E3 to measure against human review; nothing here claims precision.

## Model-backed critic

`examples/critic_model.py` is the same shape as the reference critic with a model behind it: it reads the host's request on stdin, asks one Claude model for findings as structured JSON, and prints the findings document. Declare it beside or instead of `critic.rules`, by an absolute path in the host's own checkout:

```json
"critic.model": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_model.py"], "timeout_seconds": 300}
```

- **Identity and independence.** The reviewer is `{"name": "critic.model", "revision": "<model>@<effort>@1"}`, for example `critic.model@claude-sonnet-5-5@medium@1`. It equals a proposer's `name@revision` only if the two are configured identically, and then `reviewer_not_independent` fires, which is correct. The model does not choose its identity: the schema it fills has `findings` and `summary` only; `version` and `reviewer` are written by the adapter.
- **Model and effort** come from the environment of the process that runs the check, never from the request: `SYBERLABS_CRITIC_MODEL` (default `claude-sonnet-5-5`) and `SYBERLABS_CRITIC_EFFORT` (default `medium`). The call uses the official SDK, structured output under a strict schema (severity `high|medium|low`, `line` integer or null, closed objects), and the server-side refusal fallback, as `adapters/anthropic_adapter.py` does. Structured outputs accept no string-length constraints, so the caps of `spec/finding.schema.json` (claim and falsifier 2000, file 4096, summary 4000) are enforced in the adapter after parsing; a finding without a usable severity, file, claim or falsifier is dropped, not invented; at most 20 findings are kept. The host's parser remains the authority.
- **Errors are errors.** An SDK error, a refusal that survives the fallback, or an answer cut at `max_tokens` exits 2 with one line on stderr, so the host records the check as `failed` with that line in its tail, never as a pass.
- **Cost.** The adapter appends `[critic.model usage: <served model> in=<tokens> out=<tokens> cache_read=<tokens>]` to the document's `summary`, so the cost of every review is in the chain, and prints the same as `usage:{...}` on stderr when stderr is not stdout (under the host it is: `run_check` merges them and parses everything as the document, so there the adapter writes only the document). The diff sent to the model is cut at `SYBERLABS_CRITIC_MAX_DIFF_BYTES` characters (default 200000, about 50k tokens), which bounds one review at about $0.26 at Sonnet 5.5 prices ($2 / $10 per million) with the 16000-token answer cap. The one recorded call (`tests/fixtures/critic_model/sonnet55-medium-critic-md.json`, 2026-10-21, Claude Sonnet 5.5 at `medium`, reviewing this section's and the adapter's own 22 KB diff) used 9,705 input and 937 output tokens: $0.029. At the E3 pilot's sizes (a toy fixture's diff of a few kilobytes; the proposer's own calls were about 700 input tokens) a review is a few thousand input tokens, about a cent. The reviewer's `revision` names the configured model; the served model, which the fallback may change, is the one in the summary's usage note.
- **Replay.** `--replay PATH` (or `SYBERLABS_CRITIC_REPLAY=PATH`) reads a recorded response instead of calling the model; `--record PATH` writes one from a live call. `tests/test_critic_model.py` runs the adapter through the host with a replayed response and no key or network. The argument form exists because the host runs checks with a scrubbed environment (`syberlabs.checks.PASS_ENV`) that passes no `SYBERLABS_*` variable.
- **What is not solved.** That same scrubbed environment carries no `ANTHROPIC_API_KEY` and replaces `HOME`, so under today's host the live adapter has no credential: running `critic.model` for real needs the host to pass a credential through, which is a `syberlabs/checks.py` decision (`spec/GAPS.md` 39). The reviewer's precision is unmeasured until the E3 addendum runs; nothing here says whether its high findings should block or advise.

## Trust boundary

The critic is a program the contract names, run with the developer's permissions against candidate code in a temporary worktree; it is not a sandbox. Its output is a claim until the host has parsed it, checked the reviewer against the provider, and stored it in the chain; only then does admission read it, and only the counts.
