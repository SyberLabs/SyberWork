# Preregistration v2.1: coverage amendment before model trials

Owner: Seth Carlson. Acting agent: Codex. This is an explicit amendment to
[v2](PREREGISTRATION-v2.md), originally pushed at `291a7a1` before any trials.
The original registration and its JSON remain unchanged and visible.

## Observation before amendment

The authenticated preflight at source `291a7a1` obtained both planned excerpts
for T01, T02, T03, T05 and T06. T04's first request for `pydantic-core 2.27.2`
returned `VERSION_NOT_FOUND`. The v2 all-context gate correctly stopped with
**zero model cells**, not a failed candidate. The complete 65-file receipt
manifest, six baseline/reference validations, source identities and interrupted
record are retained in `results/hard-v2-access-001`.

No model outcome on these six tasks exists or has been inspected when making
this amendment. The existing original three-task 3/3-versus-3/3 tie stays visible
and separate. This amendment does not change any task, objective, starting code,
reference, check, dependency pin, source range, model, effort, call cap, ordering
seed or blinding policy. Fixture digests and configuration must match v2 exactly.

## Changed availability procedure

Run all six planned baseline cells and the five available treatment cells.
Keep T04's treatment explicitly unavailable, with null solved and zero model
calls. Its baseline is a standalone observation and cannot become a matched
win/loss or enter the primary matched denominator. Do not drop or replace T04,
substitute another version or source, or infer its missing treatment outcome.
There remain six planned pairs; at most five can complete using this snapshot.

Use the exact authenticated receipts already saved by v2. This is frozen-context
reuse, not another live retrieval or an unaccounted fallback. Verify the entire
preflight manifest, each original request target/path/range, HTTP status and
authenticated accounted transport, served package/version/repository/commit,
and exact derived excerpts before any model call. Pin that manifest digest in
`PREREGISTRATION-v2.1.json` before committing and pushing this amendment. No new
token, API lookup or provider indexing attempt is required for the model matrix.
Any availability other than the registered T04 `VERSION_NOT_FOUND` stops models.

## Unchanged scoring and limits

Use the same `gpt-6.1-sol` requested alias, high effort, two-call cap per arm,
one repeat and arm-unaware host checks. At most 24 calls remains the declared
ceiling; the frozen unavailable cell makes the actual maximum 22. Keep opaque
cells and do not inspect model outcomes until all 12 planned cells finish.
The unavailable cell still has a row in that matrix. Reference fixes and
acceptance-check code stay hidden from the model, with the same bounded repair
feedback policy. This remains partial blinding; the operator is unblinded.

Primary reporting remains matched host solves plus GitHits-only/baseline-only
solves, with both the completed and planned denominators. Publish all six rows
per arm and show the coverage gap alongside the matched result. Matched results
are conditional on this preflight's available context and cannot establish how
GitHits would perform on T04 or on a representative dependency workload. Do not
pool either this attempt or the earlier three-task tie into a larger success rate.
