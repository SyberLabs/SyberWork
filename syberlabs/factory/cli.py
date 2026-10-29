"""``syberlabs factory``: the Space-Time Parsing slice from a terminal.

    syberlabs factory index --declared architecture.json           # derive Space, map components, log it
    syberlabs factory drift --declared architecture.json --max 0.2  # reflexion report; exit 1 over the threshold
    syberlabs factory record --entry app.main:run --fixture in.json  # record one run into the ledger
    syberlabs factory replay RUN --entry app.main:run --fixture in.json
    syberlabs factory show RUN                                       # the worldline, one line per step
    syberlabs factory at RUN --seq 12                                # what was active at a logical time
    syberlabs factory export RUN --chrome trace.json | --view run.html
    syberlabs factory spec validate|promote|canonize|retire spec.json
    syberlabs factory ticket spec.json --req R1 --out ticket.json
    syberlabs factory attest --baseline A --candidate B --ticket ticket.json --commit SHA
    syberlabs factory verify attestation.json --public KEY
    syberlabs factory status

The ledger lives under ``--home`` (default ``.syberlabs/factory``). Every command reads the ledger fresh.
``--root`` (default ``.``) is the repository being parsed; ``--declared`` is an ``ArchitectureSnapshot``-shaped
JSON file naming components and the edges the author intends between them.
"""

from __future__ import annotations

import argparse
import importlib
import json
import runpy
import sys
from pathlib import Path

from syberlabs import certificate
from syberlabs.canonical import digest
from syberlabs.ed25519 import public_from_seed
from syberlabs.errors import Rejected
from syberlabs.factory import attest, harness, ledger, space
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

    sp = steps.add_parser("spec", help="move a spec file through its lifecycle; each step is logged")
    sp.add_argument("operation", choices=sorted(specmod.TRANSITIONS))
    sp.add_argument("spec", help="the spec JSON file; rewritten in place")
    sp.add_argument("--actor", default="human:operator", help="kind:id (default human:operator)")
    sp.add_argument("--verdict", action="append", default=[], help="a review verdict id promotion rests on")
    sp.add_argument("--reason")
    sp.add_argument("--superseded-by")

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
    att.add_argument("--behavior-change", action="store_true", help="the ticket intends a behavior change")
    att.add_argument("--builder", default="human:operator", help="who produced the candidate: agent[:model[:session]]")
    att.add_argument("--seed-env", default=None, metavar="NAME",
                     help="sign with the seed in this environment variable (see `syberlabs certify`); unsigned otherwise")
    att.add_argument("--out", help="write the statement or envelope here (default: the ledger's attestations dir)")

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
        return 1 if args.max is not None and report["max_drift"] > args.max else 0

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
        print(f"verified: {document['subject'][0]['name']}  spec {predicate['spec']['spec_id']} v{predicate['spec']['version']}"
              f"  ticket {predicate.get('ticket')}  promote {predicate['policy']['promote']}")
        for reason in predicate["policy"]["reasons"]:
            print(f"  {reason}")
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


def _load_entry(entry: str, root: Path):
    """``module:function`` from the root, or a script path. Returns (callable or None, script path or None)."""
    if ":" in entry and not Path(entry).exists():
        module_name, function_name = entry.rsplit(":", 1)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise Rejected("entry_import", f"cannot import {module_name}: {exc}") from None
        function = getattr(module, function_name, None)
        if function is None:
            raise Rejected("entry_missing", f"{module_name} has no {function_name}")
        return function, None
    script = Path(entry)
    if not script.exists():
        raise Rejected("entry_missing", f"{entry} is neither module:function nor a file")
    return None, script


def _record(args, home: ledger.Ledger, replay: bool) -> int:
    root = Path(args.root).resolve()
    declared = _declared(getattr(args, "declared", None))
    graph = home.latest_space() if declared is None else _indexed(args, declared, args.revision)
    file_components = {p: r.get("component") for p, r in graph["files"].items()} if graph else {}
    space_digest = space.graph_digest(graph) if graph else None
    fixture = _read_json(args.fixture, "fixture") if args.fixture else {}
    function, script = _load_entry(args.entry, root)
    run_id = harness.new_run_id()
    replay_of = home.run_path(args.run if replay and hasattr(args, "run") else args.replay) if replay else None
    revision = args.revision or (graph.get("revision") if graph else None) or space.git_revision(root)
    recorder = harness.Recorder(root, home.run_path(run_id), file_components, run_id=run_id, revision=revision,
                                entrypoint=args.entry, fixture_digest=digest(fixture), space_digest=space_digest,
                                replay_of=replay_of, blobs=home.home / "blobs")
    outcome = None
    try:
        with recorder as rec:
            if function is not None:
                outcome = function(rec.context, fixture)
            else:
                runpy.run_path(str(script), run_name="__main__")
    except harness.Diverged as exc:
        print(f"diverged: {exc.detail}", file=sys.stderr)
    except SystemExit as exc:  # a script's own exit: clean when the code is 0, recorded as an error otherwise
        if exc.code not in (None, 0):
            print(f"the program exited with {exc.code}", file=sys.stderr)
    except Exception as exc:  # the program's own failure is recorded, then reported
        print(f"the program raised {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
    event = home.record_run(run_id)
    body = event["body"]
    print(f"{'replayed' if replay else 'recorded'} run {run_id}  status {body['status']}  steps {body['counts']}  "
          f"revision {str(revision)[:10]}  (ledger seq {event['seq']})")
    if replay:
        before = home.load_run(args.run if hasattr(args, "run") else args.replay)
        report = harness.compare(before, home.load_run(run_id))
        if report["equal"]:
            print(f"worldline matches the recording over {report['steps_before']} projected steps")
        else:
            first = report["first_divergence"]
            print(f"worldline parts at projected step {first['index']}: recorded {first['before']}, replayed {first['after']}")
        return 0 if body["status"] == "ok" and report["equal"] else 1
    if outcome is not None:
        try:
            print("outcome", json.dumps(outcome, sort_keys=True, default=str)[:400])
        except (TypeError, ValueError):
            pass
    return 0 if body["status"] == "ok" else 1


def _spec(args, home: ledger.Ledger) -> int:
    document = _read_json(args.spec, "spec")
    kind, _, actor_id = args.actor.partition(":")
    actor = {"kind": kind or "human", "id": actor_id or "operator"}
    detail = {}
    if args.operation == "validate":
        detail["space"] = home.latest_space()
    if args.operation == "promote":
        detail["verdict_ids"] = args.verdict
    if args.reason:
        detail["reason"] = args.reason
    if args.superseded_by:
        detail["superseded_by"] = args.superseded_by
    record = specmod.transition(document, args.operation, actor, **detail)
    Path(args.spec).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    home.append("spec_changed", {"spec_id": document["spec_id"], "version": document["version"], **record,
                                 "spec_digest": specmod.spec_digest(document)})
    print(f"{document['spec_id']} v{document['version']}: {record['from']} -> {record['to']} ({args.operation})")
    return 0


def _attest(args, home: ledger.Ledger) -> int:
    ticket = _read_json(args.ticket, "ticket")
    baseline = home.load_run(args.baseline)
    candidate = home.load_run(args.candidate)
    evidence = {"trace_diff": attest.trace_diff(baseline, candidate, ticket.get("footprint"))}
    parts = args.builder.split(":")
    builder = {"agent": parts[0], "model": parts[1] if len(parts) > 1 else None, "session": parts[2] if len(parts) > 2 else None,
               "fixture_digest": candidate[0].get("fixture_digest")}
    document = attest.statement(subject_commit=args.commit, repository=args.repository or Path(args.root).resolve().name,
                                spec_ref=ticket["spec_ref"], footprint=ticket.get("footprint"), evidence=evidence,
                                builder=builder, ticket=ticket["ticket_id"], behavior_change_label=args.behavior_change)
    written = document
    note = "unsigned"
    if args.seed_env:
        seed = certificate.load_seed(args.seed_env)
        written = attest.sign(document, seed)
        note = f"signed, key {public_from_seed(seed).hex()[:10]}, seed from ${args.seed_env}"
    event = home.record_attestation(written)
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
    """A self-contained HTML page: the Space graph with components lit as a slider scrubs the run."""
    template = Path(__file__).resolve().parents[1] / "static" / "factory.html"
    data = {"run": records[0] if records else {}, "spans": harness.spans(records),
            "effects": [r for r in records if r.get("t") in ("effect", "marker")],
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
