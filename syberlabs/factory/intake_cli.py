"""``syberlabs factory intake ...``: triage a request for work from a terminal or a workflow step.

    syberlabs factory intake triage --github-issue issue.json --owner mateo \\
        --decider "rules=python standin_decider.py" --comment comment.md   # screen, ask, route, record
    syberlabs factory intake triage --title "..." --body-file body.md --ref acme/app#12 --owner mateo
    syberlabs factory intake policy --out intake-policy.json                # the default policy, to edit
    syberlabs factory intake list --route needs_human                       # what waits for a person
    syberlabs factory intake show acme/app#12 --comment                     # one request's decision
    syberlabs factory intake resolve acme/app#12 --route agent_ready --by human:mateo --reason "read it; safe"

Registered through ``extend`` from one hook line at the end of ``cli.py``, as the autonomy slice is. ``--home`` is
the factory ledger. Nothing here fetches an issue, posts a comment or applies a label: ``--github-issue`` reads
the JSON the operator fetched, ``--comment`` writes the text to post, and ``--outputs`` appends ``route``,
``label``, ``rule``, ``reason`` and ``ticket`` lines in the form a workflow step reads (``$GITHUB_OUTPUT``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.factory import intake
from syberlabs.factory import spec as specmod
from syberlabs.providers import split_command

COMMAND = "intake"


def extend(populate, run):
    """Wrap ``cli.populate`` and ``cli.run`` so the factory parser gains ``intake`` and dispatches it here."""
    def populate_with_intake(factory):
        populate(factory)
        steps = next(a for a in factory._actions if isinstance(a, argparse._SubParsersAction))
        add_commands(steps)

    def run_with_intake(args):
        if getattr(args, "factory_command", None) != COMMAND:
            return run(args)
        try:
            return _run(args)
        except Rejected as exc:
            print(f"error: {exc.code}: {exc.detail}", file=sys.stderr)
            return 2

    return populate_with_intake, run_with_intake


def add_commands(steps) -> None:
    top = steps.add_parser(COMMAND, help="triage a request for work: screen its text, ask six closed questions, let code "
                                         "pick the route, record every answer (docs/FACTORY_INTAKE.md)")
    sub = top.add_subparsers(dest="intake_command", required=True)

    triage = sub.add_parser("triage", help="screen, ask, route and record one request; exit 0 once a decision is recorded")
    source = triage.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", metavar="FILE", help=f"a {intake.REQUEST_SCHEMA} JSON document")
    source.add_argument("--github-issue", metavar="FILE", help="the JSON GitHub's REST API returns for one issue")
    source.add_argument("--title", help="the request's title, with --ref (and --body-file, --source, --author)")
    triage.add_argument("--body-file", metavar="FILE", help="the request's body as a UTF-8 text file (with --title)")
    triage.add_argument("--ref", help="what names the request where it came from, for example owner/repository#12 (with --title)")
    triage.add_argument("--source", choices=intake.SOURCE_KINDS, default="manual", help="where a --title request came from")
    triage.add_argument("--author", help="who wrote a --title request")
    triage.add_argument("--owner", required=True, help="the accountable human on whatever ticket opens")
    triage.add_argument("--policy", metavar="FILE", help="an intake policy JSON (default: the built-in one, `intake policy`)")
    triage.add_argument("--decider", metavar="NAME=COMMAND", help="the command that answers the six questions; without "
                                                                  "one, a person decides every request the screen lets through")
    triage.add_argument("--model", help="the exact model id the decider must report; another one is refused")
    triage.add_argument("--shadow", action="append", default=[], metavar="NAME=COMMAND",
                        help="a second decider asked the same questions; recorded beside the decision, never acted on (repeatable)")
    triage.add_argument("--known", metavar="FILE", help="earlier requests for the duplicate menu: a JSON list of {ref, title}, "
                                                        "or of GitHub issue documents")
    triage.add_argument("--timeout", type=int, default=60, help="seconds a decider may take (default 60)")
    triage.add_argument("--again", action="store_true", help="decide again even though this exact text has a decision")
    _outputs(triage)

    policy = sub.add_parser("policy", help="print the built-in policy and its digest, or write it to a file to edit")
    policy.add_argument("--out", metavar="FILE")

    listing = sub.add_parser("list", help="every request intake knows, by its newest decision or resolution")
    listing.add_argument("--route", choices=intake.RESOLUTIONS)
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="one request's decision as JSON, or as the comment to post")
    show.add_argument("request", help="kind:ref, the ref alone, or the first characters of the request digest")
    show.add_argument("--comment", action="store_true", help="print the Markdown comment instead of JSON")

    resolve = sub.add_parser("resolve", help="a person's route for a request, whatever intake said (a human act)")
    resolve.add_argument("request", help="kind:ref, the ref alone, or the first characters of the request digest")
    resolve.add_argument("--route", required=True, choices=intake.RESOLUTIONS)
    resolve.add_argument("--by", required=True, help="the person resolving it, as human:<id>")
    resolve.add_argument("--reason", required=True, help="why, on one line")
    resolve.add_argument("--owner", help="the accountable human, when the route is agent_ready and triage named none")
    resolve.add_argument("--of", metavar="REF", help="the request this one repeats, when the route is duplicate")
    resolve.add_argument("--kind", choices=sorted(intake.KINDS))
    resolve.add_argument("--size", choices=sorted(intake.SIZES))
    resolve.add_argument("--area", help="the component the work is in")
    _outputs(resolve)


def _outputs(parser) -> None:
    parser.add_argument("--comment", metavar="FILE", help="write the Markdown comment for the request to FILE")
    parser.add_argument("--tickets", metavar="DIR", help="write the ticket JSON into DIR when one opens")
    parser.add_argument("--outputs", metavar="FILE", help="append route, label, rule, reason and ticket as name=value lines")
    parser.add_argument("--json", action="store_true", help="print the record as JSON")


def _read_json(path: str, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Rejected(f"{what}_unreadable", f"{path}: {type(exc).__name__}") from None


def _party(text: str, what: str) -> tuple[str, list[str]]:
    name, found, command = text.partition("=")
    if not found or not name or not command.strip():
        raise Rejected("intake_decider", f"{what} is NAME=COMMAND, not {text!r}")
    return name, split_command(command)


def _request(args) -> dict:
    if args.request:
        return intake.check_request(_read_json(args.request, "request"))
    if args.github_issue:
        return intake.from_github_issue(_read_json(args.github_issue, "issue"))
    if not args.ref:
        raise Rejected("intake_request", "--title needs --ref: the name of the request where it came from")
    body = ""
    if args.body_file:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise Rejected("request_unreadable", f"{args.body_file}: {type(exc).__name__}") from None
    return intake.new_request(args.source, args.ref, args.title, body, author=args.author)


def _known(path: str | None) -> list[dict]:
    if not path:
        return []
    entries = _read_json(path, "known")
    if not isinstance(entries, list):
        raise Rejected("intake_known", "--known is a JSON list")
    out = []
    for entry in entries:
        if isinstance(entry, dict) and "ref" not in entry and "number" in entry:
            if entry.get("pull_request") is not None:
                continue
            issue = intake.from_github_issue(entry)
            entry = {"ref": issue["source"]["ref"], "title": issue["title"]}
        out.append(entry)
    return out


def _write_outputs(args, item: dict, decision: dict, ticket: dict | None) -> None:
    if args.comment:
        Path(args.comment).write_text(intake.comment(decision), encoding="utf-8")
    if ticket is not None and args.tickets:
        folder = Path(args.tickets)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{ticket['ticket_id']}.json").write_text(json.dumps(ticket, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.outputs:
        lines = {"route": item["route"], "label": intake.label(item["route"]), "rule": item["rule"],
                 "reason": " ".join(str(item["reason"]).split()), "ticket": ticket["ticket_id"] if ticket else ""}
        with open(args.outputs, "a", encoding="utf-8", newline="\n") as handle:
            handle.writelines(f"{name}={value}\n" for name, value in lines.items())


def _answers_line(decision: dict) -> str | None:
    decider = decision.get("decider")
    if not decider or not decider.get("answers"):
        return None
    parts = []
    for name in intake.QUESTIONS:
        reading, p = intake._reading(name, decider["answers"][name], decision["floor"])
        parts.append(f"{name} {reading} {p:.2f}")
    return " | ".join(parts)


def _print_decision(decision: dict, item: dict, ticket: dict | None, already: bool) -> None:
    source = decision["source"]
    print(f"request   {source['kind']}:{source['ref']}  {decision['request_digest'][:12]}" + ("  (decided before; nothing asked)" if already else ""))
    removed = [f"{count} {intake.REMOVED[name]}{'' if count == 1 else 's'}" for name, count in decision["screen"]["hidden"].items() if count]
    flags = [flag["rule"] for flag in decision["screen"]["flags"]]
    print(f"screen    {', '.join(removed) + ' removed' if removed else 'nothing hidden'}"
          + (f"; findings: {', '.join(flags)}" if flags else "")
          + (f"; sensitive: {', '.join(decision['screen']['sensitive'])}" if decision["screen"]["sensitive"] else ""))
    decider = decision.get("decider")
    answers = _answers_line(decision)
    if answers:
        print(f"answers   {answers}  ({decider['name']}" + (f", {decider['model']}" if decider.get("model") else "") + ")")
    elif decider:
        print(f"answers   none usable from {decider['name']}: {decider['outcome']}")
    for shadow in decision.get("shadow") or []:
        said = f"would say {shadow['would_route']} ({'agrees' if shadow['agrees'] else 'differs'})" if shadow.get("answers") \
            else f"gave no usable answer ({shadow['outcome']})"
        print(f"shadow    {shadow['name']} {said}; recorded, not acted on")
    print(f"route     {decision['route']}  (rule {decision['rule']}: {decision['reason']})")
    if item["route"] != decision["route"] or item["by"].startswith("human:"):
        print(f"resolved  {item['route']} by {item['by']}: {item['reason']}")
    if ticket is not None:
        print(f"ticket    {ticket['ticket_id']}  {ticket['title']}  (owner {ticket['human_owner']})")


def _run(args) -> int:
    from syberlabs.factory import ledger as ledgermod

    command = args.intake_command
    if command == "policy":
        policy = intake.default_policy()
        text = json.dumps(policy, indent=2, sort_keys=True) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.out}: intake policy {intake.policy_digest(policy)[:12]}; edit it and pass it with --policy")
        else:
            print(text, end="")
        return 0
    home = ledgermod.Ledger(args.home)
    if command == "triage":
        policy = intake.check_policy(_read_json(args.policy, "policy")) if args.policy else None
        result = intake.triage(home, _request(args), human_owner=args.owner, policy=policy,
                               decider=_party(args.decider, "--decider") if args.decider else None,
                               shadows=[_party(entry, "--shadow") for entry in args.shadow], known=_known(args.known),
                               model=args.model, timeout=args.timeout, again=args.again)
        _write_outputs(args, result["item"], result["decision"], result["ticket"])
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_decision(result["decision"], result["item"], result["ticket"], result["already"])
        return 0
    if command == "list":
        rows = [item for _, item in sorted(intake.items(home).items()) if args.route in (None, item["route"])]
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        for item in rows:
            print(f"{item['route']:12} {item['by']:18} {item['source']['kind']}:{item['source']['ref']}  "
                  f"{item['ticket_id'] or '-':16}  {item['reason']}")
        print(f"{len(rows)} request(s)" + (f" routed {args.route}" if args.route else ""))
        return 0
    if command == "show":
        item = intake.find(home, args.request)
        decision = next(e["body"] for e in home.events() if e["seq"] == item["decided_seq"])
        if args.comment:
            print(intake.comment(decision), end="")
        else:
            print(json.dumps({"decision": decision, "item": item}, indent=2, sort_keys=True))
        return 0
    if command == "resolve":
        work = {"kind": args.kind, "size": args.size, "area": args.area}
        result = intake.resolve(home, args.request, args.route, specmod.principal(args.by), reason=args.reason,
                                human_owner=args.owner, duplicate_of=args.of, work=work)
        item = result["item"]
        decision = next(e["body"] for e in home.events() if e["seq"] == item["decided_seq"])
        _write_outputs(args, item, decision, result["ticket"])
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_decision(decision, item, result["ticket"], False)
        return 0
    raise Rejected("intake_command", f"unknown command {command!r}")
