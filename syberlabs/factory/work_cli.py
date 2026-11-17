"""``syberlabs factory work ...``: plan a ticket, claim it, start its Build Thread, and see what waits.

    syberlabs factory work list                                               # every ticket and its status
    syberlabs factory work plan acme/app#12 --planner "claude=/abs/python /abs/planner.py" --floors policy.json
    syberlabs factory work plan acme/app#12 --plan-file plan.json --by human:mateo   # a plan a person wrote
    syberlabs factory work show acme/app#12 --text                            # the plan as a person reads it
    syberlabs factory work approve acme/app#12 --by human:mateo --reason "read it"   # or --reject
    syberlabs factory work claim acme/app#12 --by model:claude-1              # one agent per ticket
    syberlabs factory work start acme/app#12 --by model:claude-1 --check "tests=/abs/python -m unittest -q"
    syberlabs factory work settle acme/app#12                                 # record that its thread holds an accepted change
    syberlabs factory work release acme/app#12 --by model:claude-1 --reason "blocked on a decision"
    syberlabs factory work settle                                             # the same for every ticket that has a thread
    syberlabs factory work queue                                              # ready work, first to read first

Registered through ``extend`` from one hook line at the end of ``cli.py``. ``--home`` is the factory ledger and
``--root`` the repository, as for every ``factory`` command. A ticket is named by its id, by the first characters
of it, or by its request (``owner/repository#12``). See docs/FACTORY_WORK.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.factory import review_queue, work
from syberlabs.factory import plan as planmod
from syberlabs.factory import spec as specmod
from syberlabs.providers import split_command

COMMAND = "work"


def extend(populate, run):
    """Wrap ``cli.populate`` and ``cli.run`` so the factory parser gains ``work`` and dispatches it here."""
    def populate_with_work(factory):
        populate(factory)
        steps = next(a for a in factory._actions if isinstance(a, argparse._SubParsersAction))
        add_commands(steps)

    def run_with_work(args):
        if getattr(args, "factory_command", None) != COMMAND:
            return run(args)
        try:
            return _run(args)
        except Rejected as exc:
            print(f"error: {exc.code}: {exc.detail}", file=sys.stderr)
            return 2

    return populate_with_work, run_with_work


def add_commands(steps) -> None:
    top = steps.add_parser(COMMAND, help="plan a ticket from intake, claim it for one agent, start a Build Thread whose "
                                         "scope is the plan's files (docs/FACTORY_WORK.md)")
    sub = top.add_subparsers(dest="work_command", required=True)

    listing = sub.add_parser("list", help="every ticket intake opened, with its status")
    listing.add_argument("--status", choices=work.STATUSES)
    listing.add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="plan an agent_ready ticket: a planner command (or a person) names files, steps and "
                                       "risks; code checks them and routes the plan")
    plan.add_argument("ticket")
    plan.add_argument("--planner", metavar="NAME=COMMAND", help="the command that writes the plan; it runs in a temporary "
                                                                "checkout of HEAD")
    plan.add_argument("--plan-file", metavar="FILE", help="a plan a person wrote, as JSON (with --by human:<id>)")
    plan.add_argument("--by", help="the person who wrote --plan-file, as human:<id>")
    plan.add_argument("--floors", metavar="FILE", help="class floors by path, in policy.json's shape (default: built in)")
    plan.add_argument("--max-files", type=int, help=f"most paths a plan may name before a person reads it (default "
                                                    f"{planmod.DEFAULT_LIMITS['max_files']}, at most {planmod.MAX_FILES_CEILING})")
    plan.add_argument("--sensitive-area", action="append", default=[], metavar="COMPONENT",
                      help="a component a plan may not touch without a person (repeatable)")
    plan.add_argument("--timeout", type=int, default=300, help="seconds the planner may take (default 300)")
    plan.add_argument("--text", metavar="FILE", help="write the plan as Markdown to FILE")
    plan.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="one ticket's state as JSON, or its plan as Markdown")
    show.add_argument("ticket")
    show.add_argument("--text", action="store_true", help="print the plan as a person reads it")

    approve = sub.add_parser("approve", help="a person's decision on a ticket's plan (a human act)")
    approve.add_argument("ticket")
    approve.add_argument("--by", required=True, help="the person deciding, as human:<id>")
    approve.add_argument("--reason", required=True)
    approve.add_argument("--reject", action="store_true", help="reject the plan; the ticket is planned again")

    claim = sub.add_parser("claim", help="take a planned ticket for one agent, or renew that agent's claim")
    claim.add_argument("ticket")
    claim.add_argument("--by", required=True, help="who takes it, as kind:id (model:claude-1, human:mateo)")
    claim.add_argument("--ttl", type=int, default=work.DEFAULT_TTL, help=f"seconds the claim lasts (default {work.DEFAULT_TTL})")

    release = sub.add_parser("release", help="give a claim up; its holder releases its own, a person releases anyone's")
    release.add_argument("ticket")
    release.add_argument("--by", required=True)
    release.add_argument("--reason", required=True)

    start = sub.add_parser("start", help="start the Build Thread of a ticket its claimant holds; the scope is the plan's files")
    start.add_argument("ticket")
    start.add_argument("--by", required=True, help="the claimant, as kind:id")
    start.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND", help="a check SyberWork runs on every "
                                                                                             "proposed change (repeatable)")
    start.add_argument("--kit-home", default=".syberlabs", help="the Build Thread home under --root (default .syberlabs)")
    start.add_argument("--max-candidates", type=int, default=6)

    settle = sub.add_parser("settle", help="record, once, that a ticket's thread holds an accepted change; nothing is merged. "
                                           "Without a ticket: every ticket that has a thread")
    settle.add_argument("ticket", nargs="?")
    settle.add_argument("--kit-home", default=".syberlabs")

    queue = sub.add_parser("queue", help="every ready change, first to read first, with the reasons for the order and a "
                                         "reading order by component; nothing is pushed, split or merged")
    queue.add_argument("--json", action="store_true")


def _read_json(path: str, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Rejected(f"{what}_unreadable", f"{path}: {type(exc).__name__}") from None


def _named(text: str, what: str) -> tuple[str, list[str]]:
    name, found, command = text.partition("=")
    if not found or not name or not command.strip():
        raise Rejected("work_argument", f"{what} is NAME=COMMAND, not {text!r}")
    return name, split_command(command)


def _line(current: dict) -> str:
    source = current["source"]
    plan = current["plan"]
    detail = ""
    if current["status"] == "held":
        detail = f"request is {current['request_route']}"
    elif plan is not None and current["status"] in ("plan_held", "plan_rejected"):
        detail = plan["reason"]
    elif current["claim"] is not None:
        detail = f"{current['claim']['by']['kind']}:{current['claim']['by']['id']}"
        if current["work"] is not None:
            detail += f" on thread {current['work']['thread']}"
    elif current["ready"] is not None:
        detail = f"accepted on {current['ready']['target_ref']} at {str(current['ready']['commit'])[:10]}; not pushed or merged here"
    return f"{current['status']:13} {current['ticket_id']}  {source['kind']}:{source['ref']}  {detail}".rstrip()


def _plan_text(home, current: dict) -> str:
    plan = current["plan"]
    if plan is None or plan["plan_digest"] is None:
        raise Rejected("plan_missing", f"{current['ticket_id']} has no plan to show ({current['status']})")
    decided = {"route": plan["route"], "rule": plan["rule"], "reason": plan["reason"]}
    return planmod.text(work.load_plan(home, plan["plan_digest"]), plan["assessment"], decided)


def _run(args) -> int:
    from syberlabs.factory import ledger as ledgermod

    home = ledgermod.Ledger(args.home)
    command = args.work_command
    if command == "list":
        rows = [row for row in work.tickets(home) if args.status in (None, row["status"])]
        if args.json:
            print(json.dumps([{**row, "label": work.label(row)} for row in rows], indent=2, sort_keys=True))
            return 0
        for row in rows:
            print(_line(row))
        print(f"{len(rows)} ticket(s)" + (f" {args.status}" if args.status else ""))
        return 0
    if command == "plan":
        if bool(args.plan_file) != bool(args.by):
            raise Rejected("work_argument", "--plan-file and --by go together: a written plan names the person who wrote it")
        written = (specmod.principal(args.by), _read_json(args.plan_file, "plan")) if args.plan_file else None
        limits = {"max_files": args.max_files} if args.max_files is not None else None
        found = work.make_plan(home, args.ticket, repo_root=args.root, planner=_named(args.planner, "--planner") if args.planner else None,
                               written=written, floors=_read_json(args.floors, "floors") if args.floors else None, limits=limits,
                               sensitive_areas=args.sensitive_area, timeout=args.timeout)
        planned, current = found["planned"], found["state"]
        if args.text and found["plan"] is not None:
            Path(args.text).write_text(_plan_text(home, current), encoding="utf-8")
        if args.json:
            print(json.dumps(found, indent=2, sort_keys=True))
            return 0
        print(f"ticket    {current['ticket_id']}  {current['source']['kind']}:{current['source']['ref']}")
        if found["plan"] is not None:
            print(f"plan      {planned['plan_digest'][:12]}  {len(planned['files'])} file(s) to change, {len(planned['new_files'])} to add, "
                  f"{planned['steps']} step(s)")
            assessment = planned["assessment"]
            print(f"computed  class floor {assessment['class_floor']}; components {', '.join(assessment['components']) or 'none mapped'}; "
                  f"a test among the paths: {'yes' if assessment['tests_planned'] else 'no'}")
        else:
            print(f"plan      none usable: {planned['reason']}")
        print(f"route     {planned['route']}  (rule {planned['rule']}: {planned['reason']})")
        return 0
    if command == "show":
        current = work.state(home, args.ticket)
        if args.text:
            print(_plan_text(home, current), end="")
        else:
            print(json.dumps({**current, "label": work.label(current)}, indent=2, sort_keys=True))
        return 0
    if command == "approve":
        current = work.approve(home, args.ticket, specmod.principal(args.by), reason=args.reason, reject=args.reject)
        print(_line(current))
        return 0
    if command == "claim":
        current = work.claim(home, args.ticket, specmod.principal(args.by), ttl=args.ttl)
        print(_line(current))
        return 0
    if command == "release":
        current = work.release(home, args.ticket, specmod.principal(args.by), reason=args.reason)
        print(_line(current))
        return 0
    if command == "start":
        checks = {}
        for entry in args.check:
            name, argv = _named(entry, "--check")
            checks[name] = {"argv": argv, "timeout_seconds": 300}
        if not checks:
            raise Rejected("work_argument", "name at least one check with --check NAME=COMMAND")
        found = work.start(home, args.ticket, specmod.principal(args.by), repo_root=args.root, kit_home=args.kit_home, checks=checks,
                           max_candidates=args.max_candidates)
        print(f"thread    {found['thread']}" + ("  (started before)" if found["already"] else ""))
        print(f"contract  {found['contract']}")
        print(f"scope     {', '.join(found['scope'])}")
        print(f"class     {found['state']['work']['act_class']}  (the floor of the plan's paths)")
        if found["objective"]:
            print(f"objective {found['objective'].splitlines()[0]}")
        print(f"next      syberlabs use {found['thread']}; then propose, check, and a person accepts. Nothing was proposed or merged.")
        return 0
    if command == "queue":
        rows = review_queue.queue(home, repo_root=args.root)
        print(json.dumps(rows, indent=2, sort_keys=True) if args.json else review_queue.text(rows), end="" if not args.json else "\n")
        return 0
    if command == "settle" and args.ticket is None:
        looked = work.settle_all(home, repo_root=args.root, kit_home=args.kit_home)
        for current in looked:
            print(_line(current))
        print(f"{sum(1 for current in looked if current['status'] == 'ready')} of {len(looked)} thread(s) hold an accepted change")
        return 0
    if command == "settle":
        current = work.settle(home, args.ticket, repo_root=args.root, kit_home=args.kit_home)
        print(_line(current))
        return 0 if current["status"] == "ready" else 1
    raise Rejected("work_command", f"unknown command {command!r}")

