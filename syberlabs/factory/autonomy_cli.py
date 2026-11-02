"""``syberlabs factory autonomy ...``: the thin slice from a terminal.

    syberlabs factory autonomy check --map map.json --spec spec.json        # mapcheck, task graph, risk plan; exit 1 on findings
    syberlabs factory autonomy approve --map map.json --by mateo            # the owner's approval over the map's digest (G1)
    syberlabs factory autonomy envelope --map map.json --calls 6 --out env.json
    syberlabs factory autonomy run --map map.json --spec spec.json --envelope env.json --owner mateo \\
        --proposer "python standin.py ." --check "tests=python -m unittest -q" --hidden "hidden.f1.marker=python -c ..."
    syberlabs factory autonomy why TICKET                                   # ticket -> feature -> map -> spec, from the ledger

Registered through ``extend`` from one hook line at the end of ``cli.py``, so that file takes no other edit for
this packet. ``--home`` is the factory ledger, ``--root`` the repository the features change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from syberlabs import certificate
from syberlabs.errors import Rejected
from syberlabs.factory import featuremap, loop
from syberlabs.factory import policy as policymod
from syberlabs.providers import split_command

COMMAND = "autonomy"


def extend(populate, run):
    """Wrap ``cli.populate`` and ``cli.run`` so the factory parser gains ``autonomy`` and dispatches it here."""
    def populate_with_autonomy(factory):
        populate(factory)
        steps = next(a for a in factory._actions if isinstance(a, argparse._SubParsersAction))
        add_commands(steps)

    def run_with_autonomy(args):
        if getattr(args, "factory_command", None) != COMMAND:
            return run(args)
        try:
            return _run(args)
        except Rejected as exc:
            print(f"error: {exc.code}: {exc.detail}", file=sys.stderr)
            return 2

    return populate_with_autonomy, run_with_autonomy


def add_commands(steps) -> None:
    auto = steps.add_parser(COMMAND, help="the autonomous typed mode's thin slice: approved feature map -> tickets -> "
                                         "Build Threads -> ready for owner merge, inside an envelope (docs/FACTORY_AUTONOMY.md)")
    sub = auto.add_subparsers(dest="autonomy_command", required=True)
    check = sub.add_parser("check", help="mapcheck findings, the task graph and the risk plan")
    check.add_argument("--map", required=True)
    check.add_argument("--spec", required=True, help="the promoted spec JSON (syberlabs factory spec ...)")
    check.add_argument("--tau", type=float, default=1.0)
    check.add_argument("--units", type=int, default=None, help="extra verification units to allocate (default: one per feature)")
    check.add_argument("--json", action="store_true")
    approve = sub.add_parser("approve", help="record the owner's approval over the map's current digest (a human act)")
    approve.add_argument("--map", required=True)
    approve.add_argument("--by", required=True, help="the approving person's id")
    env = sub.add_parser("envelope", help="write an envelope for a map: dollars, calls, seconds, attempts")
    env.add_argument("--map", required=True)
    env.add_argument("--dollars", type=float, default=0.0)
    env.add_argument("--calls", type=int, required=True)
    env.add_argument("--seconds", type=float, default=3600.0)
    env.add_argument("--attempts", type=int, default=2)
    env.add_argument("--kill-switch", help="absolute path; when the file exists the loop stops before its next decision")
    env.add_argument("--out", required=True)
    runp = sub.add_parser("run", help="compile the approved map to tickets and drive each feature through a Build Thread")
    runp.add_argument("--map", required=True)
    runp.add_argument("--spec", required=True)
    runp.add_argument("--envelope", required=True)
    runp.add_argument("--owner", required=True, help="the accountable human on every ticket")
    runp.add_argument("--kit-home", default=".syberlabs", help="the Build Thread home under --root (default .syberlabs)")
    runp.add_argument("--proposer", required=True, help="the proposer command (a stand-in or a model adapter), as one string")
    runp.add_argument("--check", action="append", default=[], metavar="NAME=CMD", help="a visible check (repeatable)")
    runp.add_argument("--hidden", action="append", default=[], metavar="NAME=CMD",
                      help="a hidden check the host holds under the name the map uses (repeatable; never in a worktree)")
    runp.add_argument("--baseline", help="the golden run id to name on tickets")
    runp.add_argument("--max-steps", type=int, default=50)
    runp.add_argument("--shadow", help="recorded answers JSON for a shadow policy; recorded beside each decision, never acted on")
    runp.add_argument("--owner-public", metavar="KEY", help="the owner's .pub file: re-verify the spec's promotion approval under it "
                                                            "(without it the ledger's hash-chained record and stored envelope are checked)")
    why = sub.add_parser("why", help="walk a ticket back to its feature, map, approval, spec and its promotion")
    why.add_argument("ticket")


def _parse_checks(items: list[str], what: str) -> dict:
    out = {}
    for item in items:
        name, found, command = item.partition("=")
        if not found or not name or not command.strip():
            raise Rejected("check_argument", f"{what} must be NAME=COMMAND, not {item!r}")
        out[name] = split_command(command)
    return out


def _run(args) -> int:
    from syberlabs.factory import ledger as ledgermod
    command = args.autonomy_command
    if command == "check":
        document = loop.load_json(args.map, "map")
        spec = loop.load_json(args.spec, "spec")
        findings = featuremap.mapcheck(document, spec)
        if findings:
            for finding in findings:
                print(f"finding    {finding}")
            print(f"{len(findings)} finding(s); the map is not compiled")
            return 1
        graph = featuremap.taskgraph(document)
        plan = featuremap.risk_plan(document, graph, budget=args.units, tau=args.tau)
        if args.json:
            print(json.dumps({"graph": graph, "plan": plan, "approved": featuremap.approved(document)}, indent=2, sort_keys=True))
            return 0
        print(f"map {document['id']} {featuremap.map_digest(document)[:12]}: {graph['features']} features, {graph['edges']} edges; "
              f"approved {featuremap.approved(document)}")
        print(f"critical path {' -> '.join(graph['critical_path'])} (length {graph['critical_path_length']}); total work "
              f"{graph['total_work']}; work/critical {graph['work_critical_ratio']}; width {graph['width']} {graph['max_antichain']}")
        for conflict in graph["conflicts"]:
            print(f"conflict   {conflict['a']} {conflict['b']} on {conflict['paths']}" + (" (ordered by the DAG)" if conflict["ordered_by_dag"] else ""))
        for fid in plan["r"]:
            print(f"risk       {fid:5} r {plan['r'][fid]:.3f}  z {plan['z'][fid]:+.2f}  share {plan['share'][fid]:.3f}  "
                  f"units {plan['units'][fid]}  buys {', '.join(plan['buys'][fid][len(featuremap.FLOOR):]) or '-'}")
        print(f"floor      {', '.join(plan['floor'])} (every feature, never loosened); tau {plan['tau']} on standardized scores")
        return 0
    if command == "approve":
        document = loop.load_json(args.map, "map")
        approval = featuremap.approve(document, {"kind": "human", "id": args.by})
        Path(args.map).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"approved {document['id']} at {approval['digest'][:12]} by {args.by}; any edit of the map leaves this stale")
        return 0
    if command == "envelope":
        document = loop.load_json(args.map, "map")
        envelope = loop.new_envelope(featuremap.map_digest(document), dollars_total=args.dollars, calls_total=args.calls,
                                     seconds_total=args.seconds, attempts_per_feature=args.attempts, kill_switch=args.kill_switch)
        Path(args.out).write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {args.out}: envelope {loop.envelope_digest(envelope)[:12]} for map {document['id']}")
        return 0
    if command == "run":
        document = loop.load_json(args.map, "map")
        spec = loop.load_json(args.spec, "spec")
        envelope = loop.load_json(args.envelope, "envelope")
        policy = policymod.OfflinePolicy()
        if args.shadow:
            policy = policymod.ShadowPolicy(policy, policymod.recorded_answers(loop.load_json(args.shadow, "shadow")), shadow_name="recorded")
        checks = {name: {"argv": argv, "timeout_seconds": 300} for name, argv in _parse_checks(args.check, "--check").items()}
        if not checks:
            raise Rejected("check_argument", "name at least one visible check with --check NAME=COMMAND")
        root = Path(args.root).resolve()
        kit_home = Path(args.kit_home)
        owner_public = certificate.read_public(args.owner_public) if args.owner_public else None
        driver = loop.Loop(repo_root=root, kit_home=kit_home if kit_home.is_absolute() else root / kit_home,
                           ledger=ledgermod.Ledger(args.home), map=document, spec=spec, envelope=envelope, policy=policy,
                           proposer_argv=split_command(args.proposer), checks=checks, hidden_catalogue=_parse_checks(args.hidden, "--hidden"),
                           human_owner=args.owner, baseline_run=args.baseline, log=print, owner_public=owner_public)
        tickets = driver.compile()
        for ticket in tickets:
            print(f"ticket     {ticket['ticket_id']}  {ticket['map_ref']['feature_id']}  {ticket['title']}")
        summary = driver.run(max_steps=args.max_steps)
        for fid, row in summary["features"].items():
            print(f"feature    {fid:5} {row['state']:22} attempts {row['attempts']}" + (f"  {row['last_reason']}" if row["last_reason"] else ""))
        totals = summary["envelope"]
        print(f"stopped: {summary['stopped']}; envelope ${totals['dollars']:.4f}, {totals['calls']} calls, {totals['seconds']}s; "
              f"ready for owner merge: {', '.join(summary['ready_for_owner_merge']) or 'none'}. Nothing was merged or pushed.")
        return 0 if summary["ready_for_owner_merge"] else 1
    if command == "why":
        print(json.dumps(loop.why(ledgermod.Ledger(args.home), args.ticket), indent=2, sort_keys=True))
        return 0
    raise Rejected("autonomy_command", f"unknown command {command!r}")
