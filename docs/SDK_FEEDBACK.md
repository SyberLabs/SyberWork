# Development feedback and framework comparison

This note is from building a second project, `examples/access_review.py`, on `syberlabs.Session` and from the numbers in `benchmarks/results/dev-session.txt`. It is not a market survey. The comparisons are capability comparisons, not speed claims against those projects.

## What a new project actually does

The access review is not the procurement case and not the release gate. A reviewer certifies a directory roster, a manager approves revocation of extra access, and an auditor signs. The host application supplies the roster. The SDK admits the actions and records the case.

`PYTHONPATH=. python examples/access_review.py` runs the development paths: complete case, planner that skips a required prior effect, unverified roster, a contract rejected at publish, policy replay, and a tampered chain.

## Feedback

What worked on the first integration:

- The public surface is small. `Session`, `Rejected`, and `StaticPlanner` were enough to finish a case.
- Admission is the same function the procurement app uses, so a denial reason from the new project is a real reason, not a demo stub.
- `compiled_propose` binds arguments from the contract through one function, `bind_arguments`, used by both `Session` and `Work`.
- `explain_admission(..., when="recorded")` still says `all_checks_passed` after the case is complete. `when="now"` says `action_already_completed`, because commit would refuse a second effect. Both are useful, and they answer different questions. The default remains `now`.
- A bad `required_facts` item fails at `install_contract` with `invalid_contract`, before a case exists.
- The planner can propose `revoke_extra` first. The decision is `required_prior_effect_missing` and no `effect_started` event is written.

What got in the way:

- `Session` executes `local` actions only. A project that needs an HTTP write has to move the case onto `syberwork.Work`. The admission rules, compiled binding, and hash function are shared. The stores are not.
- Reason codes are strings. A caller branches on `approval_required:manager` rather than a typed result.
- `explain_admission(when="now")` after a successful commit does not explain the recorded decision. Callers who want that must pass `when="recorded"`. The rule id for that decision is also on the side channel, outside the hash.
- The in-memory session has no source connector. The new project records a verified observation itself. That is the host's claim. The HTTP API still sets `verified` only from a source read.
- There is still no second language SDK. `conformance/jcs_vectors.json` publishes the JCS bytes and SHA-256 another language can check. The chain link itself is still this package's canonical JSON.
- Non-finite amounts are `amount_required`. An input kind other than `string` or `integer` is rejected at publish.

## Benchmarks

Command: `PYTHONPATH=. python benchmarks/run_bench.py`.

**MEASURED** on this development machine, one process, CPython 3.12, after one SQLite connection per `Work`, integer microsecond timestamps on new events, and a cache for rule provenance. The same run wrote `benchmarks/results/dev-session.txt`.

| Operation | n | Median | p95 |
| --- | ---: | ---: | ---: |
| `session.complete_case` | 100 | 0.54 ms | 0.60 ms |
| `work.complete_case` | 40 | 37.4 ms | 49.7 ms |
| `admit.allowed` | 2000 | 3.5 µs | 4.1 µs |
| `explain.allowed` | 1000 | 3.8 µs | 4.2 µs |
| `session.verify_chain` (500 observations) | 20 | 10.2 ms | 10.5 ms |

The unit test runs a shorter pass and checks that a session case stays under 50 ms, a Work case under 100 ms, an allow under 1 ms, and that the session median beats Work on this machine.

`explain` was about 106 µs before the provenance cache. It is now in the same band as `admit`. Admission itself is not the cost of a case.

`Work` is about 70× the session median on a full access-review case in this run. An earlier measurement of an empty transaction on the reused connection was about 7 µs, and opening a fresh connection for the same statement was about 29 µs. That difference does not account for the case. A separate measurement, not the default, completed one case in about 1.3 ms with WAL and `synchronous=NORMAL`, and stayed near 24 ms with the default FULL sync. `NORMAL` can drop the latest commits after power loss, so the library stays on FULL. The remaining cost is a sync per commit. It is not a claim about SQLite's maximum throughput, and it is not a reason to avoid `Work` when the case has to survive a process restart.

`admit` on an empty history is the floor. A case with facts, resolutions, and economic checks costs more. The benchmark does not include those. `verify_events` on 501 events is about 2.6 ms, almost all of it the digest loop. `session.verify_chain` is about 10.2 ms on the same history because it also recomputes the JCS side digest.

## How this compares to existing SDK frameworks

These systems are the ones a team would actually put next to this package. They do not solve the same problem.

| Framework | What the caller asks | What it will not do | Where SyberLabs is different |
| --- | --- | --- | --- |
| OPA and Cedar | May this principal take this action on this resource? | Record a case, run the effect, or pin a contract version to a history | Admission is one answer. The case still has a proposal, an effect, and an acceptance check. A planner cannot turn a permit into a write. |
| Casbin and SpiceDB | Does this ACL or relationship allow it? | Separate "legal" from "the write happened" | `allowed` is not `succeeded`. An unknown HTTP result stays unknown until reconciliation. |
| in-toto | Did the layout's steps happen, and were the links signed? | Decide a live business action against a policy that can change later | Closest cousin for the hash chain and the idea that a later step needs an earlier one. SyberLabs cases are mutable histories of one piece of work, not a software-supply-chain layout. |
| Temporal, Restate, Step Functions | Run this workflow durably and retry the activities | Keep a contract from widening a global policy, or keep a model from holding the executor credential | Those SDKs own execution. This one owns the license to execute. `Session` is in-process and dies with the process. Temporal survives workers. |
| LangGraph and other agent SDKs | Let the model pick a tool and run it | Stop the tool when a fact is stale, a prior effect is missing, or a person has not approved | A planner here returns `{action, args}` only. The same admission function used for a human proposal decides. The model token is not an effect credential. |

What this SDK is for: a host application that already has its own data, and needs a versioned contract, a global policy, a recorded proposal, and a hash-chained history before a local or HTTP effect.

What it is not for: general authorization (use Cedar or OPA), durable workflow execution (use Temporal or Restate), relationship tuples (use SpiceDB), or a multi-language client generated from these schemas. Those do not exist here.

The access-review project is the kind of fit that works today: a few named actions, facts from a system the host already trusts, one approval, and a signature. A project that needs retries, worker failover, or a policy language with its own tooling should not start here.

The standards gap and what this pass closed are in [SDK_REDTEAM.md](SDK_REDTEAM.md). New events hash an integer microsecond `at`. A chain head can be signed with Ed25519 and appended to a transparency log that is not the case database. The HMAC witness remains for callers that already have one.
