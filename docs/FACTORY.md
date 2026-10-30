# The factory: Space-Time Parsing on one ledger

The factory is a ledger indexed by component, execution step and commit, and a set of operations over
that grid. The architecture graph derived from the source tree (Space) gives the component axis. A
recorded execution (Time) gives the step axis. Git gives the commit axis. A cell is one component at one
step at one commit; a worldline is one run's path through cells. Specs claim footprints of cells, tickets
name a delta between two commits over a footprint, and an attestation is a trace diff over that
footprint, signed with the same Ed25519 and DSSE code the chain head uses.

The design, its prior art (record and replay, durable execution, trace standards, code graphs,
architecture conformance, spec-driven development, attestation, ledger storage) and the three-phase plan
are in the architecture plan this slice was built from. This page is what the repository does today.

Package: `syberlabs/factory/`. Standard library only. `syberlabs` still does not import `syberwork`.
Claim ceiling for this page: **implemented** and **tested** (`tests/test_factory.py`, 18 tests, and the
CLI run below). Nothing here is measured, deployed or externally validated.

## The v0 slice

| Layer | Module | What it does |
| --- | --- | --- |
| Space | `space.py` | Reads the Python sources under a root with `ast`: files, symbols (functions, classes, methods with a signature digest), `IMPORTS` edges resolved to repository files, `CALLS` edges resolved to symbols with a confidence. Maps files to declared components by path glob (last match wins), rolls edges up to `DEPENDS_ON` between components, and computes the reflexion report: convergence, divergence (observed, not declared, with the symbol-level `via` edges), absence (declared, not observed), unmapped files, and a drift score per component (divergent out-weight over total out-weight). `diff` compares two graphs by file hash and symbol signature. |
| Time | `harness.py` | `record` runs an entrypoint in a child interpreter whose bootstrap installs `Recorder.hook`, a profile function, and journals every call and return under the root as JSON lines with `seq`, a logical clock, and `ns` since the run started. The hook is installed only in that child: the repository's integrity rule (`examples/critic_rules.py`, `runner_reach`) refuses a trace or profile hook in any file a test can import, so the bootstrap is a string and the parent only reads the journal. `Context.run(name, fn, *args)` is the effect boundary: recording calls `fn` and journals its JSON result (large payloads go to a content-addressed blob); replay returns the journaled result without calling `fn` and raises `Diverged` when the program asks for a different effect, or ends with `status: diverged` when it asks for fewer. `Context.marker` does the same for time, random and environment reads. `spans`, `active_at`, `worldline`, `compare` and `chrome_trace` read a journal back. |
| Ledger | `ledger.py` | An append-only JSONL event log with a `previous`/`hash` chain (`verify` walks it; `append` refuses an edited log), content-addressed artifacts (Space graphs, run journals, blobs, attestations), and a rebuildable SQLite projection with `components`, `edges`, `runs`, `steps`, `effects` and `cells`. Queries: `worldline(run)`, `active_at(run, seq)`, `cells_for(component)` across revisions, `components_at(revision)`. |
| Spec | `spec.py` | A spec is one capability's requirements, each with a clause id, text, scenarios and evidence kinds, plus a footprint hint. Operations (revise, mutate, validate, promote, canonize, retire) are checked against a state table and recorded on the spec's history with actor and input digest. `resolve_footprint` pins the hint to a Space graph's revision and lists the files it owns. `ticket_from_spec` opens one ticket per requirement with the resolved footprint, the scenarios as acceptance criteria, and one exit criterion per evidence kind. |
| Attestation | `attest.py` | `trace_diff` compares two runs over a footprint's components, projecting each to (component, fn) per call and (effect, result digest) per effect, and reports whether they are equal and where they first part. `statement` wraps the evidence, spec reference, footprint, builder and verdicts in an in-toto Statement v1 with predicate type `https://syberlabs.space/attestation/spec-satisfied/v1`; `policy` is the gate (empty diff, or a non-empty diff with the behavior-change label, then optional full-suite tests and a mutation threshold); `sign` and `verify` are DSSE over the canonical statement. |
| CLI | `cli.py` | `syberlabs factory ...` or `python -m syberlabs.factory ...`; every command reads the ledger fresh. |
| Viewer | `static/factory.html` | A self-contained page `export --view` fills with one run: the component graph with declared, divergent and absent edges, a slider over `seq` that lights the active components and edges, the active span stack, and the effects so far. Play at 0.2x to 5x paces `seq`, not wall time. |

## Running it

The repository's own fixture (`tests/fixtures/factory_app`, four components with one planted undeclared
import and one declared edge nothing uses) is the worked example:

```sh
cd tests/fixtures/factory_app
echo '{"part": "p-104", "quantity": 2}' > fixture.json
python -m syberlabs.factory index --declared architecture.json           # Space; drift report; ledger seq 1
python -m syberlabs.factory drift --declared architecture.json --max 0.5  # exit 1: effects -> rules is undeclared
python -m syberlabs.factory record --entry fxapp.main:run --fixture fixture.json
python -m syberlabs.factory show RUN_A                                    # the worldline, one line per step
python -m syberlabs.factory at RUN_A --seq 10                             # main.run and effects.submit are open
python -m syberlabs.factory replay RUN_A --entry fxapp.main:run --fixture fixture.json   # effects come from the journal
python -m syberlabs.factory spec validate spec.json
python -m syberlabs.factory spec promote spec.json --verdict review-1 --actor model:reviewer
python -m syberlabs.factory ticket spec.json --req R1 --baseline RUN_A --owner mateo --out ticket.json
python -m syberlabs.factory record --entry fxapp.main:run --fixture fixture.json --revision candidate1
python -m syberlabs.factory attest --baseline RUN_A --candidate RUN_B --ticket ticket.json --commit candidate1 \
    --seed-env SYBERLABS_FACTORY_SEED --out attestation.json             # exit 0: diff empty, promote True
python -m syberlabs.factory verify attestation.json --public key.pub
python -m syberlabs.factory export RUN_A --chrome trace.json --view run.html
python -m syberlabs.factory status                                        # 9 events, chain verified
```

An entrypoint is `module:function`, imported from the root and called as `function(ctx, fixture)` with the
hook installed after the import so the import is not part of the run, or a script run as `__main__` whose
own body is its first span and which takes its context from `syberlabs.factory.harness.current()`. Effects
go through `ctx.run(name, fn, ...)`; that is the only instrumentation a program needs, and the only part
replay cannot do without. The child inherits the caller's environment, as provider commands do; it is not a
sandbox. The signing seed is read from the named environment variable the way `syberlabs certify` reads
it; only the name is recorded.

The repository checks itself the same way. `docs/architecture.declared.json` declares four components
(`syberlabs`, `syberlabs.eval`, `syberlabs.factory`, `syberwork`) and the edges allowed between them;
`tests/test_factory.py::test_repository_layering_has_no_drift` derives the graph from the checkout and
requires zero divergence, which is `tests/test_package.py`'s "syberlabs never imports syberwork" read from
the code rather than from `sys.modules`. The same check runs from a terminal:

```sh
python -m syberlabs.factory drift --declared docs/architecture.declared.json --paths syberlabs syberwork --max 0
```

Recording the repository's own Build Thread example under the harness (`python -m syberlabs.factory record
--entry examples/build_thread.py`) journals about 6,000 spans over 266 functions, all in `syberlabs`, in about a
second on the container the tests ran in, and `export --view` renders it. Indexing `syberlabs` and
`syberwork` (84 files, about 1,500 symbols, 3,200 edges) takes under a second there. Both numbers are single
observations, not measurements.

## The seam: `factory check` as a Build Thread check

The factory does not decide anything about a candidate; the Build Thread's admission does. The seam between
them is one host-run check. A contract names it like any other check:

```json
"checks": {"factory": {"argv": ["/abs/python", "-m", "syberlabs.factory", "--home", "/abs/.syberlabs/factory",
                                "check", "--declared", "architecture.json", "--entry", "app.main:run",
                                "--fixture", "fixture.json", "--max-drift", "0"], "timeout_seconds": 300}},
"required": ["factory"]
```

Before the thread starts, the operator records the golden run on the base commit and names it. One
command does all of it and publishes the contract:

```sh
python -m syberlabs.factory bootstrap --declared architecture.json --entry app.main:run --fixture fixture.json --max-drift 0
```

It indexes, records the golden, sets the `golden` baseline, and writes `repo-change.v<N+1>.json` with
`factory` added to `evaluation.checks` and `required`; threads started afterwards use the newest version.
By hand it is `factory record ...` then `factory baseline RUN`. For each candidate, `syberlabs check` runs the argv in the
candidate's detached worktree under the scrubbed environment, and the check: indexes the worktree and logs
the Space graph; records the fixture run at the candidate's commit; diffs it against the baseline over the
ticket's footprint (or the whole run); computes the reflexion report; writes an attestation whose predicate
carries the trace diff, the drift evidence, the gate parameters and the policy's reasons; and exits 0 only
when the policy promotes and the program ended cleanly. The evaluation record then carries the check's
output digest and tail, which name the attestation digest, so `candidate_evaluated` cites the trace diff
without any change to the frozen event vocabulary. `candidate.promotable` still requires a human-origin
proposal from a promotion role; the factory's verdict is the host's own evaluation of the exact tree, which
is what invariant 2 asks for, and nothing more.

`tests/test_factory.py::BuildThreadCheck` runs this through the Kit: a comment-only change to `rules.py`
passes (trace diff empty, drift within bound) and is accepted; a change to a threshold in `decide` is
refused with `candidate_check_failed:factory` and the attestation says where the worldlines part; with
`--max-drift 0` the fixture's planted undeclared import refuses every candidate until the declaration or the
code changes. The ledger names everything by absolute path on argv, since the check's working directory is
the worktree and its environment is scrubbed; it refuses a relative `--home`.

## Drift opens tickets

`factory drift --declared arch.json --max 0 --ticket tickets/` writes one ticket per component over the
threshold (footprint: the component and the targets of its undeclared edges; acceptance: a clean reflexion
report; exit criterion: drift evidence; `repro` lists the symbol-level edges) and one for unmapped files,
and logs each as `ticket_opened` with `source: drift`. This is the first of the plan's three automatic
ticket sources; spec promotion and feature-map drift are v1.

## Where this departs from the plan, and why

- **`ast`, not tree-sitter and SCIP.** The plan chose tree-sitter for structure and SCIP for precise
  references. This repository has no runtime dependencies and keeps it that way, so v0 reads Python with
  the standard library. Calls resolve to module-level symbols and to names imported from repository
  modules; attribute chains deeper than one level and dynamic dispatch are counted as unresolved. A
  TypeScript indexer is a second module behind the same graph shape, not a change to the ledger.
- **A profile hook in a child, not `ctx.run` everywhere.** The plan bound every component boundary with
  `ctx.run`. The recorder uses a profile hook for the Space-Time binding (which component ran at which step)
  so an existing program records with no edits, and reserves `ctx.run` for effects, which is the only place
  replay needs a journal. The hook lives in the child the harness starts, never in the test process, which
  is what the integrity rule requires. The cost is that the hook sees one thread and nothing from threads the
  program starts, and that every recording is a process start.
- **SQLite and JSONL, not Neo4j, Parquet and LanceDB.** The plan's storage table stands; v0 ships the
  event log and the one projection the queries need. The log is the system of record and the projection is
  rebuilt from it, so a later Neo4j or DuckDB projection is an addition. Feature maps (v1) are the first
  thing that would need a vector store.
- **Declared architecture in the builder's shape.** The plan proposed Structurizr DSL. The repository
  already has an `ArchitectureSnapshot` schema (`spec/builder/architecture-snapshot.schema.json`: nodes
  with `paths`, edges with `from` and `to`), so the declaration is that JSON, and a Structurizr export can
  be converted to it later.
- **Spec mutation is a state, not a loop.** `mutate` is a legal transition with the delta semantics the
  plan describes, but nothing in v0 fans one parent out to several models and reviews the variants; that
  is SyberWork's provider loop and the v1 work.

## What a cell is, exactly

A step is one span of one run: a Python function that ran under the recorded root. Its component is the
one owning the function's file in the Space graph the run was recorded against (`space_digest` in the run
header). A cell is that span, keyed by `(component, run_id, span)` and carrying the run's revision, so
`cells_for("rules")` returns every time anything in `rules` ran, across commits. The grid is sparse: a
component nothing called at a revision has no cells there, and that absence is itself a query.

## Limits

- One thread. Threads, `multiprocessing` and subprocesses are not recorded; a subprocess's output comes back
  through `ctx.run` as an effect result, which is the right boundary anyway.
- Python only. A function call is a span; a module's import runs as one `<module>` span; statements
  outside any function in an already-imported module are not visible.
- Replay re-executes the program's own code. It is not a sandbox and nothing here is.
- One writer per ledger at a time. The ledger refuses to append to a log whose chain does not verify, but
  it has no cross-process lock; the harness journal is written by the process that records.
- The trace diff compares component and function names and effect result digests. It does not compare
  arguments, return values of ordinary calls, or timing, so two runs that differ only in what a pure
  function returned diff empty. The spec's scenarios and the tests evidence kind are where those claims
  go.
- A `CALLS` edge carries a confidence (0.6 to 0.9) and `source: ast`. The reflexion report counts edges
  without weighting by confidence.

## Next work package

v1 closes the rest of the loop: promoted specs open tickets on their own (`spec_changed` is already logged),
three providers propose variants from one parent spec and one reviews, the attestation gate reads a
`tests` evidence entry from the thread's other checks, and this repository's own contract names
`factory check` with `docs/architecture.declared.json` and `examples/build_thread.py` as the recorded
program, so the factory inspects itself on every candidate. The open decision upstream of all of it is
whether a host-run trace diff over a declared footprint counts as independent review for MasterMind's auto
class; if it does, the loop closes without changing who promotes.
