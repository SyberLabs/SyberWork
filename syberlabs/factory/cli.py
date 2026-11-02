"""``syberlabs factory``: the Space-Time Parsing slice from a terminal.

    syberlabs factory index --declared architecture.json           # derive Space, map components, log it
    syberlabs factory drift --declared architecture.json --max 0.2  # reflexion report; exit 1 over the threshold
    syberlabs factory record --entry app.main:run --fixture in.json  # record one run into the ledger
    syberlabs factory replay RUN --entry app.main:run --fixture in.json
    syberlabs factory show RUN                                       # the worldline, one line per step
    syberlabs factory at RUN --seq 12                                # what was active at a logical time
    syberlabs factory export RUN --chrome trace.json | --view run.html
    syberlabs factory spec validate spec.json
    syberlabs factory owner --public owner.pub                       # pin the owner key (once); --seed-env OLD rotates
    syberlabs factory approve spec.json --verdict V --actor human:mateo --seed-env NAME --out approval.json
    syberlabs factory spec promote|canonize|retire spec.json --actor human:mateo --approval approval.json
    syberlabs factory ticket spec.json --req R1 --out ticket.json
    syberlabs factory attest --baseline A --candidate B --ticket ticket.json --commit SHA
    syberlabs factory verify attestation.json --public KEY
    syberlabs factory bootstrap --declared arch.json --entry app.main:run --fixture in.json --max-drift 0
                                                                     # golden, baseline, contract version with the check
    syberlabs factory baseline RUN                                   # name the golden run
    syberlabs factory check --declared arch.json --entry app.main:run --fixture in.json --max-drift 0
                                                                     # the Build Thread check, run in a worktree
    syberlabs factory drift --declared arch.json --max 0 --ticket tickets/   # open tickets for drift
    syberlabs factory status

The ledger lives under ``--home`` (default ``.syberlabs/factory``). Every command reads the ledger fresh.
``--root`` (default ``.``) is the repository being parsed; ``--declared`` is an ``ArchitectureSnapshot``-shaped
JSON file naming components and the edges the author intends between them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from syberlabs import certificate
from syberlabs.canonical import digest
from syberlabs.ed25519 import public_from_seed
from syberlabs.errors import Rejected
from syberlabs.factory import attest, features, harness, ledger, space
from syberlabs.factory import spec as specmod


def add_parser(commands) -> None:
    """Attach ``factory`` to the ``syberlabs`` command set."""
    populate(commands.add_parser("factory", help="Space-Time Parsing: index a repository, record and replay runs, "
                                                 "attest tickets on one ledger (docs/FACTORY.md)"))


def populate(factory) -> None:
    factory.add_argument("--home", default=".syberlabs/factory", help="the ledger directory (default .syberlabs/factory)")
    factory.add_argument("--root", default=".", help="the repository to parse and record (default .)")
    steps = factory.add_subparsers(dest="factory_command", required=True)

    index = steps.add_parser("index", help="derive the Space graph, map it to declared components, log it")
    index.add_argument("--declared", help="ArchitectureSnapshot JSON: components with path globs and intended edges")
    index.add_argument("--paths", nargs="*", help="only these relative directories or files")
    index.add_argument("--revision", help="label the graph with this commit instead of git HEAD")
    index.add_argument("--json", action="store_true", help="print the reflexion report as JSON")

    drift = steps.add_parser("drift", help="the reflexion report for the current tree; exit 1 over --max")
    drift.add_argument("--declared", required=True)
    drift.add_argument("--paths", nargs="*")
    drift.add_argument("--max", type=float, default=None, help="fail when any component's drift exceeds this")
    drift.add_argument("--json", action="store_true")
    drift.add_argument("--ticket", metavar="DIR", help="open one ticket per component over --max (and one for unmapped "
                                                       "files) as JSON files in DIR; each is logged as ticket_opened")
    drift.add_argument("--owner", help="the accountable human on tickets --ticket opens")

    base = steps.add_parser("baseline", help="name a recorded run as the golden that `check` diffs against")
    base.add_argument("run", nargs="?", help="the run id; omit to print the current baseline")
    base.add_argument("--label", default="golden")

    check = steps.add_parser("check", help="the Build Thread check: in a candidate's worktree, index, record the fixture, "
                                           "diff against the baseline, attest; exit 0 when the policy promotes")
    check.add_argument("--declared", required=True, help="ArchitectureSnapshot JSON (a path in the worktree or absolute)")
    check.add_argument("--entry", required=True)
    check.add_argument("--fixture", help="JSON fixture (a path in the worktree or absolute)")
    check.add_argument("--paths", nargs="*", help="limit the Space index to these relative paths")
    check.add_argument("--baseline", default="golden", metavar="RUN_OR_LABEL", help="a run id or a baseline label (default golden)")
    check.add_argument("--ticket", help="ticket JSON; its footprint scopes the diff (default: the whole run)")
    check.add_argument("--commit", help="the candidate commit (default: the worktree's HEAD)")
    check.add_argument("--max-drift", type=float, default=None, help="refuse when architecture drift exceeds this")
    check.add_argument("--tests-argv", metavar="JSON", help="a JSON argv list to run in the worktree as the test suite; its "
                                                            "outcome is recorded as tests evidence and the gate requires it green")
    check.add_argument("--tests-timeout", type=int, default=1800)
    check.add_argument("--level", choices=harness.LEVELS, default="component",
                       help="component (default): refactors inside a component pass; function: every call must match")
    check.add_argument("--behavior-change", action="store_true")
    check.add_argument("--builder", default=None, help="agent[:model[:session]] (default: $SYBERLABS_CANDIDATE or operator)")
    check.add_argument("--repository", default=None)
    check.add_argument("--seed-env", default=None, metavar="NAME")

    record = steps.add_parser("record", help="run an entrypoint under the recorder; the journal goes in the ledger")
    record.add_argument("--entry", required=True, help="module:function called as function(ctx, fixture), or a script "
                                                       "path run as __main__ (it takes the context from harness.current())")
    record.add_argument("--fixture", help="JSON file passed to the entrypoint (default {})")
    record.add_argument("--declared", help="map files to components with this declaration (default: the ledger's latest Space)")
    record.add_argument("--revision")
    record.add_argument("--replay", metavar="RUN", help="replay this run's journal instead of running effects")

    replay = steps.add_parser("replay", help="re-execute an entrypoint against a recorded run's journal")
    replay.add_argument("run")
    replay.add_argument("--entry", required=True)
    replay.add_argument("--fixture")
    replay.add_argument("--revision")

    show = steps.add_parser("show", help="a run's worldline")
    show.add_argument("run")
    show.add_argument("--json", action="store_true")

    at = steps.add_parser("at", help="the spans active at a logical time")
    at.add_argument("run")
    at.add_argument("--seq", type=int, required=True)

    export = steps.add_parser("export", help="a run as Chrome trace JSON or a self-contained viewer page")
    export.add_argument("run")
    export.add_argument("--chrome", metavar="FILE")
    export.add_argument("--view", metavar="FILE", help="write an HTML page that scrubs the run over its Space graph")

    sp = steps.add_parser("spec", help="move a spec file through its lifecycle; each step is logged. promote, canonize and "
                                       "retire are owner acts: a human --actor plus --approval, verified under the owner key "
                                       "the ledger pins (see `owner`)")
    sp.add_argument("operation", choices=sorted(specmod.TRANSITIONS))
    sp.add_argument("spec", help="the spec JSON file; rewritten in place")
    sp.add_argument("--actor", default="human:operator", help="kind:id, kind in human|model|compiled (default human:operator)")
    sp.add_argument("--verdict", action="append", default=[], help="a review verdict id promotion rests on; resolved on the ledger")
    sp.add_argument("--approval", metavar="FILE", help="the owner's signed approval from `factory approve` (promote, canonize, retire)")
    sp.add_argument("--owner-public", metavar="KEY", help="bootstrap only: pin this .pub as the owner key when the ledger pins "
                                                           "none (the same as `factory owner --public KEY`); a pinned ledger "
                                                           "refuses a different key here, rotation goes through `owner`")
    sp.add_argument("--reason")
    sp.add_argument("--superseded-by")
    sp.add_argument("--tickets", metavar="DIR", help="on promote: open one ticket per requirement into DIR, footprint "
                                                     "resolved against the ledger's latest Space, baseline from --baseline")
    sp.add_argument("--baseline", default="golden", help="the golden run or label the tickets diff against (default golden)")
    sp.add_argument("--owner", help="the accountable human on the tickets")

    own = steps.add_parser("owner", help="the owner key the ledger pins: print it; --public KEY pins it once (bootstrap); "
                                         "--public NEW --seed-env OLD rotates, signed by the key pinned now")
    own.add_argument("--public", metavar="KEY", help="the owner's .pub file (64 hex characters)")
    own.add_argument("--seed-env", metavar="NAME", help="on rotation: the variable holding the seed of the key pinned now")

    ap = steps.add_parser("approve", help="sign the owner's approval of a promote, canonize or retire of a spec by its content "
                                          "digest; the seed is read from an environment variable and never written")
    ap.add_argument("spec")
    ap.add_argument("--operation", choices=sorted(specmod.GATED), default="promote")
    ap.add_argument("--verdict", action="append", default=[], help="the verdict ids the promotion rests on (must match `spec promote`)")
    ap.add_argument("--actor", default="human:operator", help="the approving human, human:id (default human:operator)")
    ap.add_argument("--seed-env", required=True, metavar="NAME", help="the owner's signing seed, as `syberlabs certify` reads it")
    ap.add_argument("--out", required=True, help="where to write the approval envelope")

    var = steps.add_parser("variants", help="several proposers each mutate a parent spec; one reviewer judges; the accepted "
                                            "variant is written as the spec's next version. Proposers and the reviewer are "
                                            "commands reading JSON on stdin and writing JSON on stdout (process boundary, no SDK)")
    var.add_argument("spec", help="the parent spec JSON file (state candidate or canonical)")
    var.add_argument("--proposer", action="append", default=[], metavar="NAME=COMMAND",
                     help="a proposer: its name and the command that returns a delta; repeatable")
    var.add_argument("--reviewer", metavar="NAME=COMMAND", help="the reviewer command; omit to write the variants and stop")
    var.add_argument("--objective", default="")
    var.add_argument("--out", default=None, help="directory for the variants and the review (default: beside the spec)")
    var.add_argument("--timeout", type=int, default=600)

    ticket = steps.add_parser("ticket", help="open a ticket for one requirement of a promoted spec")
    ticket.add_argument("spec")
    ticket.add_argument("--req", required=True)
    ticket.add_argument("--baseline", metavar="RUN", help="the golden run the ticket's attestation diffs against")
    ticket.add_argument("--owner", help="the accountable human")
    ticket.add_argument("--out", help="write the ticket here (default: print)")

    att = steps.add_parser("attest", help="diff two runs over a ticket's footprint and write the attestation")
    att.add_argument("--baseline", required=True, metavar="RUN")
    att.add_argument("--candidate", required=True, metavar="RUN")
    att.add_argument("--ticket", required=True, help="ticket JSON from `factory ticket`")
    att.add_argument("--commit", required=True, help="the candidate commit the attestation is about")
    att.add_argument("--repository", default=None, help="repository name in the subject (default: the root's name)")
    att.add_argument("--level", choices=harness.LEVELS, default="component")
    att.add_argument("--behavior-change", action="store_true", help="the ticket intends a behavior change")
    att.add_argument("--builder", default="human:operator", help="who produced the candidate: agent[:model[:session]]")
    att.add_argument("--seed-env", default=None, metavar="NAME",
                     help="sign with the seed in this environment variable (see `syberlabs certify`); unsigned otherwise")
    att.add_argument("--out", help="write the statement or envelope here (default: the ledger's attestations dir)")

    boot = steps.add_parser("bootstrap", help="one command to the seam: index, record the golden run, set the baseline, and "
                                              "publish a Build Thread contract version that names `factory check`")
    boot.add_argument("--declared", required=True)
    boot.add_argument("--entry", required=True)
    boot.add_argument("--fixture")
    boot.add_argument("--paths", nargs="*")
    boot.add_argument("--max-drift", type=float, default=None)
    boot.add_argument("--level", choices=harness.LEVELS, default="component")
    boot.add_argument("--tests-argv", metavar="JSON", help="the suite the check runs in the worktree (tests evidence); "
                                                           "'contract' copies the existing contract's tests check and its timeout")
    boot.add_argument("--tests-timeout", type=int, default=1800, help="seconds for a --tests-argv suite (default 1800; "
                                                                      "'contract' takes the contract's own)")
    boot.add_argument("--thread-home", default=".syberlabs", help="the Build Thread home holding contracts/ (default .syberlabs)")
    boot.add_argument("--contract", default="repo-change", help="the contract id to extend (default repo-change)")
    boot.add_argument("--python", default=None, help="the interpreter the check argv names (default: this one)")
    boot.add_argument("--no-contract", action="store_true", help="golden and baseline only; print the argv instead")

    fmap = steps.add_parser("map", help="feature maps: a vector per symbol and component from code, docs and trace statistics")
    fsteps = fmap.add_subparsers(dest="map_command", required=True)
    fb = fsteps.add_parser("build", help="vectorize the ledger's latest Space graph (or --declared re-indexed now) at its revision")
    fb.add_argument("--declared")
    fb.add_argument("--paths", nargs="*")
    fb.add_argument("--revision")
    fn = fsteps.add_parser("nearest", help="the symbols nearest to a text, such as a spec clause")
    fn.add_argument("text")
    fn.add_argument("--revision", help="default: the newest map")
    fn.add_argument("-k", type=int, default=10)
    fn.add_argument("--components", action="store_true", help="rank components instead of symbols")
    fm = fsteps.add_parser("moved", help="what moved between two revisions' maps; --ticket opens tickets for unclaimed regions")
    fm.add_argument("--before", required=True)
    fm.add_argument("--after", required=True)
    fm.add_argument("-k", type=int, default=20)
    fm.add_argument("--ticket", metavar="DIR")
    fm.add_argument("--threshold", type=float, default=0.05, help="minimum cosine distance to count as moved (default 0.05)")
    fm.add_argument("--owner")
    fi = fsteps.add_parser("impact", help="what depends on these components, from the Space graph")
    fi.add_argument("components", nargs="+")

    ver = steps.add_parser("verify", help="check a signed attestation with its public key")
    ver.add_argument("attestation")
    ver.add_argument("--public", required=True, metavar="KEY", help="the .pub file (64 hex characters)")

    steps.add_parser("status", help="the ledger: events, chain head, counts")


def run(args) -> int:
    try:
        return _run(args)
    except Rejected as exc:
        print(f"error: {exc.code}: {exc.detail}", file=sys.stderr)
        return 2


def _declared(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Rejected("declared_unreadable", f"{path}: {type(exc).__name__}") from None


def _indexed(args, declared: dict | None, revision: str | None = None) -> dict:
    graph = space.index(args.root, getattr(args, "paths", None), revision)
    if declared is not None:
        space.map_components(graph, declared)
    return graph


def _print_reflexion(report: dict) -> None:
    for edge in report["divergence"]:
        via = ", ".join(f"{v['from']} -> {v['to']} ({v['kind']} line {v['line']})" for v in edge["via"][:3])
        print(f"divergent  {edge['from']} -> {edge['to']}  x{edge['count']}  via {via}")
    for edge in report["absence"]:
        print(f"absent     {edge['from']} -> {edge['to']}  (declared, never observed)")
    for relpath in report["unmapped"][:20]:
        print(f"unmapped   {relpath}")
    for component, score in report["drift"].items():
        print(f"drift      {component:24} {score['score']:.3f}  ({score['divergent_weight']}/{score['out_weight']})")
    print(f"max drift  {report['max_drift']:.3f}; convergent edges {len(report['convergence'])}")


def _run(args) -> int:
    home = ledger.Ledger(args.home)
    command = args.factory_command

    if command == "index":
        declared = _declared(args.declared)
        graph = _indexed(args, declared, args.revision)
        event = home.record_space(graph, digest(declared) if declared else None)
        print(f"indexed {len(graph['files'])} files, {len(graph['symbols'])} symbols, {len(graph['edges'])} edges "
              f"at {str(graph.get('revision'))[:10]}; space {event['body']['space_digest'][:12]} (ledger seq {event['seq']})")
        if declared is not None:
            report = space.reflexion(graph)
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else "", end="")
            if not args.json:
                _print_reflexion(report)
        return 0

    if command == "drift":
        graph = _indexed(args, _declared(args.declared))
        report = space.reflexion(graph)
        home.append("drift_reported", {"revision": graph.get("revision"), "max_drift": report["max_drift"],
                                       "divergent": len(report["divergence"]), "absent": len(report["absence"]),
                                       "unmapped": len(report["unmapped"]),
                                       "drift": {k: v["score"] for k, v in report["drift"].items()}})
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            _print_reflexion(report)
        if args.ticket:
            if args.max is None:
                raise Rejected("drift_threshold", "--ticket needs --max: tickets open for components over it")
            opened = specmod.tickets_from_drift(report, graph.get("revision"), threshold=args.max, human_owner=args.owner)
            folder = Path(args.ticket)
            folder.mkdir(parents=True, exist_ok=True)
            for ticket in opened:
                (folder / f"{ticket['ticket_id']}.json").write_text(json.dumps(ticket, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                home.append("ticket_opened", {"ticket_id": ticket["ticket_id"], "spec_id": "architecture", "version": None,
                                              "req_id": ticket["spec_ref"]["req_ids"][0], "source": "drift",
                                              "footprint": ticket["footprint"]["components"], "revision": graph.get("revision")})
                print(f"ticket     {ticket['ticket_id']}  {ticket['title']}")
            if not opened:
                print("no ticket: nothing over the threshold and nothing unmapped")
        return 1 if args.max is not None and report["max_drift"] > args.max else 0

    if command == "baseline":
        if not args.run:
            current = home.baseline(args.label)
            print(f"{args.label}: {current or '(unset)'}")
            return 0 if current else 1
        event = home.set_baseline(args.run, args.label)
        print(f"{args.label} -> {args.run} (ledger seq {event['seq']})")
        return 0

    if command == "check":
        return _check(args, home)

    if command == "bootstrap":
        return _bootstrap(args, home)

    if command in ("record", "replay"):
        return _record(args, home, replay=command == "replay" or bool(getattr(args, "replay", None)))

    if command == "show":
        records = home.load_run(args.run)
        if args.json:
            print(json.dumps(harness.spans(records), indent=2))
            return 0
        header = records[0]
        print(f"run {header['run_id']}  revision {str(header.get('revision'))[:10]}  entry {header.get('entrypoint')}")
        for seq, head, tail in harness.worldline(records, skip_effect_bodies=False):
            if str(head).startswith(("effect:", "marker:")):
                print(f"{seq:6}  {head:36} {str(tail)[:16]}")
            else:
                print(f"{seq:6}  {str(head):24} {tail}")
        tail = records[-1]
        if tail.get("t") == "end":
            print(f"end    {tail['status']}  {tail.get('counts')}")
        return 0

    if command == "at":
        for span in harness.active_at(home.load_run(args.run), args.seq):
            print(f"{span['component'] or '(unmapped)':24} {span['fn']:32} seq {span['start']}..{span['end']}  {span['file']}:{span['line']}")
        return 0

    if command == "export":
        records = home.load_run(args.run)
        if args.chrome:
            Path(args.chrome).write_text(json.dumps(harness.chrome_trace(records)), encoding="utf-8")
            print(f"wrote {args.chrome} (open it in Perfetto or chrome://tracing)")
        if args.view:
            graph = home.load_space(records[0]["space_digest"]) if records[0].get("space_digest") else home.latest_space()
            Path(args.view).write_text(viewer_page(records, graph), encoding="utf-8")
            print(f"wrote {args.view}")
        if not args.chrome and not args.view:
            print(json.dumps(harness.chrome_trace(records)))
        return 0

    if command == "spec":
        return _spec(args, home)

    if command == "owner":
        return _owner(args, home)

    if command == "approve":
        return _approve(args)

    if command == "variants":
        return _variants(args, home)

    if command == "map":
        return _map(args, home)

    if command == "ticket":
        document = _read_json(args.spec, "spec")
        graph = home.latest_space()
        if graph is None:
            raise Rejected("space_missing", "index the repository first: syberlabs factory index --declared ...")
        specmod.resolve_footprint(document, graph)
        ticket = specmod.ticket_from_spec(document, args.req, baseline_run=args.baseline, human_owner=args.owner)
        home.append("ticket_opened", {"ticket_id": ticket["ticket_id"], "spec_id": document["spec_id"],
                                      "version": document["version"], "req_id": args.req,
                                      "footprint": ticket["footprint"]["components"], "baseline_run": args.baseline})
        text = json.dumps(ticket, indent=2, sort_keys=True)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {args.out}: ticket {ticket['ticket_id']} over {ticket['footprint']['components']}")
        else:
            print(text)
        return 0

    if command == "attest":
        return _attest(args, home)

    if command == "verify":
        envelope = _read_json(args.attestation, "attestation")
        public = certificate.read_public(args.public)
        document = attest.verify(envelope, public)
        if document is None:
            print("signature does not verify under that key, or the payload is not a statement")
            return 1
        predicate = document["predicate"]
        # The signer's own policy line is not the verdict: compute it again from the evidence and the gate the
        # statement carries, and refuse a statement whose written policy says more than its evidence does.
        recomputed = attest.recompute(predicate)
        written = predicate.get("policy") or {}
        print(f"verified: {document['subject'][0]['name']}  spec {predicate['spec']['spec_id']} v{predicate['spec']['version']}"
              f"  ticket {predicate.get('ticket')}  promote {recomputed['promote']} (recomputed from the evidence)")
        for reason in recomputed["reasons"]:
            print(f"  {reason}")
        if written.get("promote") != recomputed["promote"] or list(written.get("reasons") or []) != recomputed["reasons"]:
            print(f"policy mismatch: the statement says promote {written.get('promote')}; the evidence says {recomputed['promote']}")
            return 1
        return 0

    if command == "status":
        for key, value in home.describe().items():
            print(f"{key:14} {value}")
        return 0
    raise Rejected("factory_command", f"unknown command {command!r}")


def _read_json(path: str, what: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Rejected(f"{what}_unreadable", f"{path}: {type(exc).__name__}") from None


def _record(args, home: ledger.Ledger, replay: bool) -> int:
    root = Path(args.root).resolve()
    declared = _declared(getattr(args, "declared", None))
    graph = home.latest_space() if declared is None else _indexed(args, declared, args.revision)
    file_components = {p: r.get("component") for p, r in graph["files"].items()} if graph else {}
    space_digest = space.graph_digest(graph) if graph else None
    fixture = _read_json(args.fixture, "fixture") if args.fixture else {}
    run_id = harness.new_run_id()
    recorded = args.run if replay and hasattr(args, "run") else getattr(args, "replay", None)
    replay_of = home.run_path(recorded) if replay else None
    revision = args.revision or (graph.get("revision") if graph else None) or space.git_revision(root)
    report = harness.record(root, home.run_path(run_id), args.entry, file_components=file_components, fixture=fixture,
                            run_id=run_id, revision=revision, space_digest=space_digest, replay_of=replay_of,
                            blobs=home.home / "blobs")
    if report.get("stdout"):
        print(report["stdout"])
    if report.get("error"):
        print(f"the program: {report['error']}", file=sys.stderr)
    if report["status"] == "killed" or not home.run_path(run_id).exists():
        print(f"no journal for run {run_id}: {report.get('error')}", file=sys.stderr)
        if report.get("stderr"):
            print(report["stderr"], file=sys.stderr)
        return 1
    event = home.record_run(run_id)
    body = event["body"]
    print(f"{'replayed' if replay else 'recorded'} run {run_id}  status {body['status']}  steps {body['counts']}  "
          f"revision {str(revision)[:10]}  (ledger seq {event['seq']})")
    if replay:
        comparison = harness.compare(home.load_run(recorded), home.load_run(run_id))
        if comparison["equal"]:
            print(f"worldline matches the recording over {comparison['steps_before']} projected steps")
        else:
            first = comparison["first_divergence"]
            print(f"worldline parts at projected step {first['index']}: recorded {first['before']}, replayed {first['after']}")
        return 0 if body["status"] == "ok" and comparison["equal"] else 1
    if report.get("outcome") is not None:
        print("outcome", json.dumps(report["outcome"], sort_keys=True, default=str)[:400])
    return 0 if body["status"] == "ok" else 1


def _check(args, home: ledger.Ledger) -> int:
    """One host-run check over a candidate's worktree. Everything it reads outside the worktree is named by an
    absolute path on argv (the ledger home, or the declaration and fixture when they are not in the tree), and
    everything it writes goes to the ledger. Its exit code is the policy's verdict, so a Build Thread contract
    can name it like any other check and the evaluation record carries its output digest and tail."""
    if not Path(args.home).is_absolute():
        raise Rejected("check_home", f"`check` runs in a candidate's worktree; name the ledger by an absolute --home, not {args.home!r}")
    root = Path(args.root).resolve()
    declared = _declared(args.declared)
    graph = _indexed(args, declared, args.commit)
    commit = args.commit or graph.get("revision")
    if not commit:
        raise Rejected("check_commit", "no commit: the worktree has no git HEAD; pass --commit")
    space_event = home.record_space(graph, digest(declared))
    report = space.reflexion(graph)
    is_run_id = args.baseline and all(c in "0123456789abcdef" for c in args.baseline) and home.run_path(args.baseline).exists()
    baseline_id = args.baseline if is_run_id else home.baseline(args.baseline)
    if baseline_id is None:
        raise Rejected("baseline_missing", f"no baseline {args.baseline!r}: record the golden run, then `factory baseline RUN`")
    # The ledger is verified, chain and artifacts, before the candidate runs, and the baseline is read then; the
    # candidate's process never learns the ledger's path, but the ledger is checked again after it exits so
    # that anything that did reach it in the meantime refuses the check rather than becoming its baseline.
    before = home.events()
    problems = home.problems(before)
    if problems:
        raise Rejected("ledger_corrupt", f"the ledger does not verify before the check: {problems[0]}")
    baseline_records = home.load_run(baseline_id)
    fixture = _read_json(args.fixture, "fixture") if args.fixture else {}
    run_id = harness.new_run_id()
    strip_env = (args.seed_env,) if args.seed_env else ()  # the check's own seed never reaches the candidate or its suite
    recorded = harness.record(root, home.run_path(run_id), args.entry,
                              file_components={p: r.get("component") for p, r in graph["files"].items()}, fixture=fixture,
                              run_id=run_id, revision=commit, space_digest=space_event["body"]["space_digest"],
                              blobs=home.home / "blobs", strip_env=strip_env)
    if recorded.get("stdout"):
        print(recorded["stdout"])
    after = home.events()
    if home.head(after) != home.head(before) or len(after) != len(before) or home.problems(after):
        print("the ledger changed while the candidate ran: refuse", file=sys.stderr)
        return 1
    if recorded["status"] == "killed" or not home.run_path(run_id).exists():
        print(f"no journal: {recorded.get('error')}", file=sys.stderr)
        return 1
    run_event = home.record_run(run_id)
    ticket = _read_json(args.ticket, "ticket") if args.ticket else None
    footprint = ticket.get("footprint") if ticket else None
    evidence = {"trace_diff": attest.trace_diff(baseline_records, home.load_run(run_id), footprint, args.level),
                "drift": attest.drift_evidence(report, commit)}
    if recorded.get("error"):
        evidence["run"] = {"kind": "run", "status": recorded["status"], "error": recorded["error"]}
    if args.tests_argv:
        evidence["tests"] = _run_tests(root, args.tests_argv, args.tests_timeout, strip_env=strip_env)
        print(f"tests: {' '.join(harness.mask(a) for a in evidence['tests']['argv'])} -> exit {evidence['tests']['exit_code']}"
              f" in {evidence['tests']['seconds']}s")
    builder_text = args.builder or os.environ.get("SYBERLABS_CANDIDATE") or "operator"
    parts = builder_text.split(":")
    builder = {"agent": parts[0], "model": parts[1] if len(parts) > 1 else None, "session": parts[2] if len(parts) > 2 else None,
               "fixture_digest": digest(fixture), "space_digest": space_event["body"]["space_digest"],
               "baseline_run": baseline_id, "candidate_run": run_id}
    gate = {"max_drift": args.max_drift} if args.max_drift is not None else {}
    gate["level"] = args.level
    if args.tests_argv:
        gate["require_tests"] = True
    spec_ref = ticket["spec_ref"] if ticket else {"spec_id": None, "version": None, "req_ids": []}
    document = attest.statement(subject_commit=commit, repository=args.repository or space.git_repository_name(root), spec_ref=spec_ref,
                                footprint=footprint, evidence=evidence, builder=builder,
                                ticket=ticket["ticket_id"] if ticket else None, behavior_change_label=args.behavior_change,
                                gate=gate)
    written, public = document, None
    if args.seed_env:
        seed = certificate.load_seed(args.seed_env)
        written, public = attest.sign(document, seed), public_from_seed(seed)
    event = home.record_attestation(written, public)
    policy = document["predicate"]["policy"]
    promote = policy["promote"] and recorded["status"] == "ok"
    diff = evidence["trace_diff"]
    other = "function" if args.level == "component" else "component"
    print(f"factory check {commit[:10]}: run {run_id} {recorded['status']}; trace diff {'empty' if diff['diff_empty'] else 'NOT empty'}"
          f" at {args.level} level ({other} level {'equal' if diff[f'{other}_level_equal'] else 'differs'})"
          f" over {diff['footprint'] or 'the whole run'} against {baseline_id}; drift {report['max_drift']:.3f}; "
          f"promote {promote}")
    for reason in policy["reasons"]:
        print(f"  {reason}")
    if not diff["diff_empty"] and diff.get("divergence_detail"):
        for side in ("before", "after"):
            print(f"  {side:7}{_describe_side(diff['divergence_detail'][side])}")
    if recorded["status"] != "ok":
        print(f"  the program did not end cleanly ({recorded['status']}): refuse")
    print(f"attestation {event['body']['attestation_digest']} (ledger seq {event['seq']}; run seq {run_event['seq']})")
    return 0 if promote else 1


def _bootstrap(args, home: ledger.Ledger) -> int:
    """Golden, baseline, contract: the operator's setup for the seam, from the base commit."""
    root = Path(args.root).resolve()
    declared = _declared(args.declared)
    graph = _indexed(args, declared)
    space_event = home.record_space(graph, digest(declared))
    report = space.reflexion(graph)
    fixture = _read_json(args.fixture, "fixture") if args.fixture else {}
    run_id = harness.new_run_id()
    recorded = harness.record(root, home.run_path(run_id), args.entry,
                              file_components={p: r.get("component") for p, r in graph["files"].items()}, fixture=fixture,
                              run_id=run_id, revision=graph.get("revision"), space_digest=space_event["body"]["space_digest"],
                              blobs=home.home / "blobs")
    if recorded["status"] != "ok":
        print(f"the golden run did not end cleanly ({recorded['status']}): {recorded.get('error')}", file=sys.stderr)
        if recorded.get("stderr"):
            print(recorded["stderr"], file=sys.stderr)
        return 1
    home.record_run(run_id)
    home.set_baseline(run_id)
    print(f"golden run {run_id} at {str(graph.get('revision'))[:10]}: {recorded['counts']}; drift {report['max_drift']:.3f}")
    if args.max_drift is not None and report["max_drift"] > args.max_drift:
        print(f"note: the base tree already drifts {report['max_drift']:.3f} over --max-drift {args.max_drift}; "
              "every candidate will be refused until the declaration or the code changes", file=sys.stderr)
    ledger_home = home.home.resolve()
    declared_arg = args.declared if not Path(args.declared).is_absolute() else str(Path(args.declared).resolve())
    # ``-P`` keeps the worktree off sys.path: the host's installed factory judges the candidate, never the
    # candidate's own copy of it. A check without it on this repository would run whatever the candidate wrote.
    argv = [args.python or sys.executable, "-P", "-m", "syberlabs.factory", "--home", str(ledger_home), "check",
            "--declared", declared_arg, "--entry", args.entry]
    if args.fixture:
        argv += ["--fixture", args.fixture if not Path(args.fixture).is_absolute() else str(Path(args.fixture).resolve())]
    if args.paths:
        argv += ["--paths", *args.paths]
    if args.max_drift is not None:
        argv += ["--max-drift", str(args.max_drift)]
    if args.level != "component":
        argv += ["--level", args.level]
    tests_argv = args.tests_argv
    tests_timeout = 0
    if tests_argv == "contract":
        tests_argv = None  # resolved from the contract below, with its own timeout
    elif tests_argv:
        tests_timeout = args.tests_timeout
        argv += ["--tests-argv", tests_argv, "--tests-timeout", str(tests_timeout)]
    if args.no_contract:
        print("check argv: " + json.dumps(argv))
        return 0
    contracts = Path(args.thread_home) / "contracts"
    if not contracts.is_absolute():
        contracts = root / contracts
    versions = sorted(contracts.glob(f"{args.contract}.v*.json"), key=lambda p: int(p.stem.rsplit(".v", 1)[1]))
    if not versions:
        raise Rejected("contract_missing", f"no {args.contract} contract under {contracts}; run `syberlabs init` first, or pass --no-contract")
    document = json.loads(versions[-1].read_text(encoding="utf-8"))
    evaluation = document.setdefault("evolution", {}).setdefault("evaluation", {})
    checks = evaluation.setdefault("checks", {})
    if args.tests_argv == "contract":
        if "tests" not in checks:
            raise Rejected("tests_missing", f"{versions[-1].name} has no tests check to copy")
        # The contract's own tests check moves into the factory check with its own timeout; no second run.
        tests_timeout = int(checks["tests"].get("timeout_seconds") or args.tests_timeout)
        argv += ["--tests-argv", json.dumps(checks["tests"]["argv"]), "--tests-timeout", str(tests_timeout)]
        checks.pop("tests")
    checks["factory"] = {"argv": argv, "timeout_seconds": 600 + tests_timeout}
    required = [name for name in evaluation.get("required", []) if name in checks]
    if "factory" not in required:
        required.append("factory")
    evaluation["required"] = sorted(required)
    document["version"] = int(document["version"]) + 1
    target = contracts / f"{args.contract}.v{document['version']}.json"
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    home.append("note", {"text": f"bootstrap published {target.name} naming factory check", "baseline": run_id})
    print(f"published {target}: the factory check is required; threads started with "
          f"`syberlabs start ... --contract {args.contract}` (or the newest version by default) now run it")
    return 0


TESTS_TAIL_CHARS = 2000


def _run_tests(root: Path, argv_json: str, timeout: int, strip_env: tuple = ()) -> dict:
    """Run the suite the contract names, in the worktree, and reduce it to a tests evidence entry. The exit code
    is the verdict (a runner's own counts are not parsed); the output digest lets a reviewer find the log, and
    the tail that enters the attestation is capped and scrubbed of credential shapes. ``full_suite`` is true
    only when the suite ran to completion: a timed-out or unstartable run is not a full suite. On timeout the
    whole process tree is stopped, as the Build Thread's own checks do. The suite runs under the Build
    Thread's scrubbed environment (``checks.scrubbed_environment``, with ``SYBERLABS_CANDIDATE`` and
    ``SYBERLABS_BASE`` as the host has them, so a floor runner still sees its base) without the host's signing
    seed or any variable in ``strip_env``."""
    import hashlib
    import subprocess
    import tempfile
    import time
    from syberlabs.certificate import DEFAULT_SEED_ENV
    from syberlabs.checks import scrubbed_environment, stop_tree
    try:
        argv = json.loads(argv_json)
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or not argv:
            raise ValueError
    except ValueError:
        raise Rejected("tests_argv", "--tests-argv is a JSON list of strings") from None
    started = time.monotonic()
    exit_code, timed_out = None, False
    with tempfile.TemporaryDirectory(prefix="syberlabs-factory-tests-") as home, tempfile.TemporaryFile() as log:
        env = scrubbed_environment(Path(home), os.environ.get("SYBERLABS_CANDIDATE") or "operator", os.environ.get("SYBERLABS_BASE"))
        for name in (DEFAULT_SEED_ENV, *strip_env):
            env.pop(name, None)
        try:
            proc = subprocess.Popen(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=hasattr(os, "setsid"))
        except OSError as exc:
            log.write(f"could not start {argv[0]!r}: {exc.strerror or exc}\n".encode())
        else:
            deadline = started + timeout
            while True:
                try:
                    exit_code = proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() > deadline:
                    stop_tree(proc)
                    timed_out = True
                    break
        log.seek(0)
        output = log.read()
    return {"kind": "tests", "argv": harness.scrub_argv(argv), "exit_code": exit_code, "full_suite": exit_code is not None,
            "fail": 0 if exit_code == 0 else 1, "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 1), "output_digest": hashlib.sha256(output).hexdigest(),
            "output_tail": harness.scrub_text(output[-TESTS_TAIL_CHARS * 4:].decode(errors="replace"), TESTS_TAIL_CHARS)}


def _map(args, home: ledger.Ledger) -> int:
    sub = args.map_command
    if sub == "build":
        declared = _declared(args.declared)
        graph = _indexed(args, declared, args.revision) if declared is not None else home.latest_space()
        if graph is None:
            raise Rejected("space_missing", "index the repository first, or pass --declared")
        if declared is not None:
            home.record_space(graph, digest(declared))
        counts = features.build(home, graph, args.root)
        home.append("note", {"text": "feature map built", **counts, "vectorizer": features.VECTORIZER})
        print(f"map at {str(counts['revision'])[:10]}: {counts['symbols']} symbols, {counts['components']} components, "
              f"{counts['with_trace']} symbols with trace statistics ({features.VECTORIZER})")
        return 0
    if sub == "nearest":
        revision = args.revision or (features.revisions(home) or [None])[-1]
        if revision is None:
            raise Rejected("features_missing", "no feature map yet; run `factory map build`")
        for sid, score in features.nearest(home, revision, args.text, args.k, "component" if args.components else "symbol"):
            print(f"{score:.4f}  {sid}")
        return 0
    if sub == "moved":
        entries = features.moved(home, args.before, args.after, args.k)
        for entry in entries:
            print(f"{entry['distance']:.4f}  {entry['change']:8} {entry['id']}")
        if args.ticket:
            graph = home.latest_space(args.after) or home.latest_space()
            claimed = set()
            for event in home.events():
                if event["kind"] == "ticket_opened":
                    claimed.update(event["body"].get("footprint") or [])
                if event["kind"] == "spec_changed" and event["body"].get("to") in ("promoted", "canonical"):
                    claimed.update(event["body"].get("footprint") or [])
            opened = specmod.tickets_from_moved(entries, graph, claimed, threshold=args.threshold, before=args.before,
                                                after=args.after, human_owner=args.owner)
            folder = Path(args.ticket)
            folder.mkdir(parents=True, exist_ok=True)
            for ticket in opened:
                (folder / f"{ticket['ticket_id']}.json").write_text(json.dumps(ticket, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                home.append("ticket_opened", {"ticket_id": ticket["ticket_id"], "spec_id": None, "version": None, "req_id": None,
                                              "source": "feature_map", "footprint": ticket["footprint"]["components"],
                                              "revision": args.after})
                print(f"ticket     {ticket['ticket_id']}  {ticket['title']}")
            if not opened:
                print("no ticket: every moved region is claimed, or nothing moved past the threshold")
        return 0
    if sub == "impact":
        graph = home.latest_space()
        if graph is None:
            raise Rejected("space_missing", "index the repository first")
        report = features.impact(graph, args.components)
        print(f"dependents of {report['components']}: {report['dependents'] or 'none'}")
        print(f"referencing symbols outside: {report['referencing_count']}")
        for sid in report["referencing_symbols"][:20]:
            print(f"  {sid}")
        return 0
    raise Rejected("factory_command", f"unknown map command {sub!r}")


def _variants(args, home: ledger.Ledger) -> int:
    """The variant loop: each proposer command mutates the parent, every variant is validated and logged, the
    reviewer command judges them all at once, and the accepted one becomes the spec's next version file."""
    from syberlabs.providers import run_json, split_command

    parent = _read_json(args.spec, "spec")
    if parent["state"] not in ("candidate", "canonical"):
        raise Rejected("spec_state", f"variants fork from a candidate or canonical spec, not state {parent['state']!r}")
    if not args.proposer:
        raise Rejected("variants_proposers", "name at least one --proposer NAME=COMMAND")
    graph = home.latest_space()
    out = Path(args.out) if args.out else Path(args.spec).resolve().parent / f"{parent['spec_id']}.variants"
    out.mkdir(parents=True, exist_ok=True)
    request = specmod.variant_request(parent, graph, args.objective)
    variants = []
    for entry in args.proposer:
        name, _, command = entry.partition("=")
        if not name or not command:
            raise Rejected("variants_proposers", f"a proposer is NAME=COMMAND, not {entry!r}")
        proposer = specmod.party("model", name, None)
        try:
            delta = run_json(split_command(command), request, timeout=args.timeout)
        except Rejected as exc:
            print(f"proposer {name}: {exc.code}: {exc.detail}", file=sys.stderr)
            home.append("spec_changed", {"spec_id": parent["spec_id"], "version": parent["version"], "operation": "mutate",
                                         "actor": proposer, "from": parent["state"], "to": None, "failed": exc.code})
            continue
        # The command declares the model that served it (``model`` or ``signal.harness.model``); the family it
        # belongs to is what independence is judged on. The declaration is the adapter's, as ``model_usage`` is.
        proposer = specmod.party("model", name, delta)
        if isinstance(delta, dict):
            delta = {k: v for k, v in delta.items() if k not in ("model", "signal", "provider")}
        try:
            variant = specmod.make_variant(parent, delta, proposer, graph)
        except Rejected as exc:
            print(f"proposer {name}: {exc.code}: {exc.detail}", file=sys.stderr)
            continue
        path = out / f"{parent['spec_id']}.v{variant['version']}.{name}.json"
        path.write_text(json.dumps(variant, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        home.append("spec_changed", {"spec_id": parent["spec_id"], "version": variant["version"], "operation": "mutate",
                                     "actor": proposer, "from": parent["state"], "to": "mutated", "variant_id": variant["variant_id"],
                                     "spec_digest": specmod.spec_digest(variant), "findings": len(variant["findings"])})
        variants.append(variant)
        print(f"variant {variant['variant_id']} by {name}: {len(variant['requirements'])} requirements, "
              f"{'valid' if not variant['findings'] else str(len(variant['findings'])) + ' finding(s)'}  -> {path.name}")
    if not variants:
        raise Rejected("variants_none", "no proposer returned a usable delta")
    if not args.reviewer:
        print(f"{len(variants)} variant(s) written to {out}; review with --reviewer, or pick one and `spec validate` it")
        return 0
    name, _, command = args.reviewer.partition("=")
    answer = run_json(split_command(command), specmod.review_request(parent, variants, args.objective), timeout=args.timeout)
    if not isinstance(answer, dict) or not isinstance(answer.get("verdicts"), list):
        raise Rejected("review_invalid", "the reviewer answers {\"verdicts\": [...]}")
    reviewer = specmod.party("model", name, answer)
    review = specmod.judge(variants, answer["verdicts"], reviewer)
    (out / "review.json").write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for verdict in review["verdicts"]:
        home.append("spec_changed", {"spec_id": parent["spec_id"], "version": parent["version"], "operation": "review",
                                     "actor": reviewer, "variant_id": verdict["variant_id"], "decision": verdict["decision"],
                                     "verdict_id": verdict["verdict_id"]})
        print(f"verdict {verdict['verdict_id']} {verdict['decision']:7} {verdict['variant_id']}  {(verdict.get('rationale') or '')[:80]}")
    if review["accepted"] is None:
        print("no variant accepted; the parent stands")
        return 1
    chosen = next(v for v in variants if v["variant_id"] == review["accepted"])
    verdict_id = next(v["verdict_id"] for v in review["verdicts"] if v["variant_id"] == review["accepted"])
    specmod.transition(chosen, "validate", reviewer, space=graph)
    for key in ("proposer", "rationale", "findings", "variant_id"):
        chosen.pop(key, None)
    target = Path(args.spec).resolve().parent / f"{parent['spec_id']}.v{chosen['version']}.json"
    target.write_text(json.dumps(chosen, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    home.append("spec_changed", {"spec_id": chosen["spec_id"], "version": chosen["version"], "operation": "validate",
                                 "actor": reviewer, "from": "mutated", "to": "candidate", "spec_digest": specmod.spec_digest(chosen),
                                 "accepted_variant": review["accepted"], "verdict_id": verdict_id})
    print(f"accepted {review['accepted']} -> {target.name} (candidate); the owner promotes it: "
          f"factory approve {target.name} --verdict {verdict_id} --actor human:OWNER --seed-env NAME --out approval.json, then "
          f"factory spec promote {target.name} --verdict {verdict_id} --actor human:OWNER --approval approval.json "
          f"--tickets DIR")
    return 0


def _describe_side(entry: dict | None) -> str:
    if entry is None:
        return "(the run ended)"
    if entry.get("kind") == "call":
        return f"call in {entry.get('component') or '(unmapped)'}" + (f" {entry['fn']}" if entry.get("fn") else "")
    payload = entry.get("payload")
    if isinstance(payload, dict) and "argv" in payload:
        return f"{entry['kind']} {entry['name']}: {' '.join(payload['argv'])[:160]}"
    return f"{entry['kind']} {entry['name']}: {json.dumps(payload, sort_keys=True, default=str)[:160]}"


def _owner(args, home: ledger.Ledger) -> int:
    """The owner key the ledger pins. Without ``--public``: print it. With it on a ledger that pins none: the
    bootstrap, pinned once and from then on the only key the gate trusts (a Class C act: whoever runs this
    first on an empty ledger is its owner, which is why it is one explicit command). With it on a pinned
    ledger: a rotation, signed by the seed of the key pinned now (``--seed-env``) or refused."""
    current = home.owner_key()
    if not args.public:
        print(f"owner key: {current or '(unset)'}")
        return 0 if current else 1
    public = certificate.read_public(args.public)
    if current == public.hex():
        print(f"owner key: {current} (already pinned)")
        return 0
    seed = certificate.load_seed(args.seed_env) if args.seed_env else None
    event = home.set_owner_key(public, seed=seed)
    body = event["body"]
    what = f"rotated from {body['previous'][:10]} (rotation {body['rotation_digest'][:12]})" if body["previous"] else "pinned (bootstrap)"
    print(f"owner key: {body['keyid']} {what} (ledger seq {event['seq']})")
    return 0


def _approve(args) -> int:
    """The owner signs an approval of one gated operation on a spec by its content digest. Nothing touches the
    ledger here; `spec promote` verifies it under the key the ledger pins and records it."""
    document = _read_json(args.spec, "spec")
    actor = specmod.principal(args.actor)
    payload = specmod.approval_payload(document, args.operation, actor, verdict_ids=args.verdict)
    seed = certificate.load_seed(args.seed_env)
    envelope = specmod.sign_approval(payload, seed)
    Path(args.out).write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"approved {args.operation} of {document['spec_id']} v{document['version']} (digest {payload['spec_digest'][:12]}) "
          f"by {actor['id']}, key {public_from_seed(seed).hex()[:10]}, seed from ${args.seed_env}; wrote {args.out}")
    return 0


def _spec(args, home: ledger.Ledger) -> int:
    document = _read_json(args.spec, "spec")
    actor = specmod.principal(args.actor)
    before_digest = specmod.spec_digest(document)  # the spec as reviewed and as approved, before the footprint moves
    detail = {}
    if args.operation == "validate":
        detail["space"] = home.latest_space()
    if args.operation == "promote":
        detail["verdict_ids"] = args.verdict
    if args.reason:
        detail["reason"] = args.reason
    if args.superseded_by:
        detail["superseded_by"] = args.superseded_by
    approval_event = None
    if args.operation in specmod.GATED:
        if actor["kind"] != "human":
            raise Rejected("spec_actor", f"{args.operation} is an owner act: the actor's kind is human, not {actor['kind']!r}")
        if not args.approval:
            raise Rejected("spec_approval", f"{args.operation} is an owner act: pass --approval FILE (from `factory approve`)")
        envelope = _read_json(args.approval, "approval")
        if args.owner_public:
            # Bootstrap only: the first key a ledger sees is pinned. A pinned ledger takes the same key as a no-op
            # and refuses any other here; rotation is `factory owner --public NEW --seed-env OLD`, never this flag.
            public = certificate.read_public(args.owner_public)
            pinned = home.owner_key()
            if pinned is None:
                event = home.set_owner_key(public)
                print(f"owner key {public.hex()[:10]} pinned (bootstrap; ledger seq {event['seq']})")
            elif pinned != public.hex():
                raise Rejected("owner_key_mismatch", f"the ledger pins owner key {pinned[:10]}, not {public.hex()[:10]}; "
                                                     "rotate with `factory owner --public NEW --seed-env OLD`")
        approval, resolved = specmod.gate(document, args.operation, actor, home.events(), envelope,
                                          verdict_ids=args.verdict, spec_digest_value=before_digest)
        detail["approval"] = approval
        detail["spec_digest"] = before_digest
        detail["resolved_verdicts"] = resolved
    graph = None
    if args.operation == "promote":
        graph = home.latest_space()
        if graph is None:
            raise Rejected("space_missing", "promotion resolves the footprint; index the repository first")
        specmod.resolve_footprint(document, graph)
    record = specmod.transition(document, args.operation, actor, **detail)
    if args.operation in specmod.GATED:
        approval_event = home.record_approval(envelope, detail["approval"])
        record["approval"] = {**record["approval"], "event_hash": approval_event["hash"], "approval_digest": approval_event["body"]["approval_digest"]}
    Path(args.spec).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    home.append("spec_changed", {"spec_id": document["spec_id"], "version": document["version"], **record,
                                 "spec_digest": specmod.spec_digest(document),
                                 "footprint": (document.get("footprint") or {}).get("components")})
    print(f"{document['spec_id']} v{document['version']}: {record['from']} -> {record['to']} ({args.operation})")
    if args.operation == "promote" and args.tickets:
        is_run = args.baseline and all(c in "0123456789abcdef" for c in args.baseline) and home.run_path(args.baseline).exists()
        baseline = args.baseline if is_run else home.baseline(args.baseline)
        folder = Path(args.tickets)
        folder.mkdir(parents=True, exist_ok=True)
        for requirement in document["requirements"]:
            ticket = specmod.ticket_from_spec(document, requirement["req_id"], baseline_run=baseline, human_owner=args.owner)
            (folder / f"{ticket['ticket_id']}.json").write_text(json.dumps(ticket, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            home.append("ticket_opened", {"ticket_id": ticket["ticket_id"], "spec_id": document["spec_id"],
                                          "version": document["version"], "req_id": requirement["req_id"], "source": "promotion",
                                          "footprint": ticket["footprint"]["components"], "baseline_run": baseline})
            print(f"ticket     {ticket['ticket_id']}  {requirement['req_id']}: {ticket['title'][:80]}")
        if baseline is None:
            print("note: no baseline is set; the tickets' attestations need `factory baseline RUN` first")
    return 0


def _attest(args, home: ledger.Ledger) -> int:
    ticket = _read_json(args.ticket, "ticket")
    baseline = home.load_run(args.baseline)
    candidate = home.load_run(args.candidate)
    evidence = {"trace_diff": attest.trace_diff(baseline, candidate, ticket.get("footprint"), args.level)}
    parts = args.builder.split(":")
    builder = {"agent": parts[0], "model": parts[1] if len(parts) > 1 else None, "session": parts[2] if len(parts) > 2 else None,
               "fixture_digest": candidate[0].get("fixture_digest")}
    document = attest.statement(subject_commit=args.commit, repository=args.repository or Path(args.root).resolve().name,
                                spec_ref=ticket["spec_ref"], footprint=ticket.get("footprint"), evidence=evidence,
                                builder=builder, ticket=ticket["ticket_id"], behavior_change_label=args.behavior_change)
    written, public = document, None
    note = "unsigned"
    if args.seed_env:
        seed = certificate.load_seed(args.seed_env)
        written, public = attest.sign(document, seed), public_from_seed(seed)
        note = f"signed, key {public.hex()[:10]}, seed from ${args.seed_env}"
    event = home.record_attestation(written, public)
    out = Path(args.out) if args.out else home.home / "attestations" / f"{event['body']['attestation_digest']}.json"
    if args.out:
        out.write_text(json.dumps(written, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    policy = document["predicate"]["policy"]
    diff = evidence["trace_diff"]
    print(f"attested {args.commit[:10]} for ticket {ticket['ticket_id']} over {diff['footprint']}: "
          f"diff {'empty' if diff['diff_empty'] else 'NOT empty'}; promote {policy['promote']} ({note})")
    for reason in policy["reasons"]:
        print(f"  {reason}")
    print(f"wrote {out} (ledger seq {event['seq']})")
    return 0 if policy["promote"] else 1


def viewer_page(records: list[dict], graph: dict | None) -> str:
    """A self-contained HTML page: the Space graph with components lit as a slider scrubs the run. Effects are
    referenced (seq, name, digest), never embedded: an io record shows its masked argv and nothing else, so a
    payload that reached the journal does not travel with the page."""
    template = Path(__file__).resolve().parents[1] / "static" / "factory.html"
    effects = []
    for record in records:
        if record.get("t") not in ("effect", "marker", "io"):
            continue
        entry = {"t": record["t"], "seq": record["seq"], "span": record.get("span"), "name": record["name"],
                 "digest": record["digest"], "replayed": bool(record.get("replayed"))}
        payload = record.get("payload")
        if record["t"] == "io" and isinstance(payload, dict) and isinstance(payload.get("argv"), list):
            entry["argv"] = [harness.mask(harness.scrub(str(a))) for a in payload["argv"][:8]]
        effects.append(entry)
    data = {"run": records[0] if records else {}, "spans": harness.spans(records), "effects": effects,
            "end": records[-1] if records and records[-1].get("t") == "end" else None,
            "components": [{"id": c["id"], "label": c["label"], "files": len(c["files"])}
                           for c in (graph or {}).get("components", {}).values()],
            "edges": [{"from": e["from"], "to": e["to"], "count": e["count"],
                       "declared": [e["from"], e["to"]] in [[d["from"], d["to"]] for d in (graph or {}).get("declared_edges", [])]}
                      for e in (graph or {}).get("depends_on", [])],
            "absent": [e for e in (graph or {}).get("declared_edges", [])
                       if not any(d["from"] == e["from"] and d["to"] == e["to"] for d in (graph or {}).get("depends_on", []))]}
    payload = json.dumps(data).replace("</", "<\\/")
    return template.read_text(encoding="utf-8").replace("/*__FACTORY_DATA__*/null", payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m syberlabs.factory", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    populate(parser)
    return run(parser.parse_args(argv))
