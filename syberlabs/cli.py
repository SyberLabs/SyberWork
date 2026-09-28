"""``syberlabs``: the Build Thread from a terminal.

    syberlabs init
    syberlabs start "Add a CSV export" --paths src tests
    syberlabs context
    syberlabs propose --from-worktree          # or --command ./my_model_adapter
    syberlabs check c1
    syberlabs diff c1
    syberlabs accept c1
    syberlabs status

The current thread is remembered in ``.syberlabs/current``. Every command
reads the journal fresh, so a thread can be resumed from any terminal.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from syberlabs.build import Kit, hint
from syberlabs.errors import Rejected
from syberlabs.providers import CommandProvider


def _kit(args) -> Kit:
    return Kit.local(args.home, args.repo, actor=args.actor, roles=args.role or ("developer",))


def _thread(kit: Kit, args):
    current = kit.home / "current"
    name = args.thread or (current.read_text().strip() if current.exists() else None)
    if not name:
        raise Rejected("no_thread", "start one with: syberlabs start \"what to change\"")
    return kit.open(name)


def _print_status(status: dict) -> None:
    print(f"thread    {status['thread'][:8]}  {status['status']}")
    print(f"objective {status['objective']}")
    print(f"contract  {status['contract']}   base {status['base'][:10]}   target {status['target_ref']}"
          + (f" -> {status['target'][:10]}" if status["target"] else " (not created)"))
    print(f"sources   {', '.join(status['sources'])}")
    if status["context"]:
        ctx = status["context"]
        print(f"context   {ctx['files']} files, {ctx['bytes']} bytes, digest {ctx['digest'][:12]}")
    used = status["budget"]
    print(f"budget    candidates {used['candidates'][0]}/{used['candidates'][1]}, "
          f"evaluations {used['evaluations'][0]}/{used['evaluations'][1]}")
    for item in status["candidates"]:
        lineage = ("from " + "+".join(item["parents"])) if item["parents"] else "from base"
        scope = "" if item["in_scope"] else "  OUT OF SCOPE"
        print(f"  {item['id']:5} {item['operator']:10} {lineage:14} {item['files']} files  "
              f"checks {item['evaluation']:7} {item['promotion']}{scope}  [{item['provider']}]")
    for line in status["open"]:
        print(f"open      {line}")
    for line in status["next"]:
        print(f"next      {line}")
    print(f"history   {status['events']} events, chain {'verified' if status['chain_valid'] else 'BROKEN'}")


def _print_receipt(receipt) -> None:
    line = f"{receipt.status}: {receipt.candidate or ''}"
    if receipt.reason:
        line += f" ({receipt.reason})"
    if receipt.status == "succeeded":
        line += f"\n{receipt.ref} now points at {receipt.commit[:10]}. Nothing was pushed or merged."
    print(line)
    if receipt.hint:
        print("next: " + receipt.hint)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syberlabs", description="Governed, Git-lineaged changes to this repository.")
    parser.add_argument("--home", default=".syberlabs")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--thread", help="thread id or unique prefix (default: the current thread)")
    parser.add_argument("--actor", help="who is acting (default: git user.email)")
    parser.add_argument("--role", action="append", help="role of the actor (repeatable; default developer)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create .syberlabs with a contract and policy for this repository")
    start = commands.add_parser("start", help="start a thread")
    start.add_argument("objective")
    start.add_argument("--contract")
    start.add_argument("--paths", nargs="+", help="repository paths providers may read")
    commands.add_parser("threads", help="list threads")
    use = commands.add_parser("use", help="make a thread current")
    use.add_argument("id")
    status = commands.add_parser("status", help="resume: objective, candidates, open items, next steps")
    status.add_argument("--json", action="store_true")
    context = commands.add_parser("context", help="select and show what a provider will read")
    context.add_argument("--query")
    context.add_argument("--drop", nargs="+", default=[], help="paths to leave out")
    context.add_argument("--show", action="store_true", help="print the excerpt text")
    propose = commands.add_parser("propose", help="run a provider and record its candidates")
    source = propose.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-worktree", action="store_true", help="snapshot in-scope working-tree edits")
    source.add_argument("--command", dest="provider_command", help="external provider command (JSON on stdin and stdout)")
    source.add_argument("--evolve", metavar="MUTATOR_COMMAND",
                        help="EvoGit-style search; the command mutates one candidate (JSON on stdin and stdout)")
    propose.add_argument("--population", type=int, default=4)
    propose.add_argument("--generations", type=int, default=6)
    propose.add_argument("--crossover-every", type=int, default=3)
    propose.add_argument("--seed", type=int, default=0)
    propose.add_argument("--from", dest="seeds", nargs="+", default=[], help="candidates to seed the population with")
    propose.add_argument("--message", default="")
    propose.add_argument("--name", default="command")
    propose.add_argument("--revision", default="1")
    check = commands.add_parser("check", help="run the contract's checks and ask admission")
    check.add_argument("candidate")
    check.add_argument("--rerun", action="store_true")
    check.add_argument("--json", action="store_true")
    diff = commands.add_parser("diff", help="show a candidate's diff against the thread base")
    diff.add_argument("candidate")
    accept = commands.add_parser("accept", help="accept a candidate: moves only the thread's target branch")
    accept.add_argument("candidate")
    approve = commands.add_parser("approve", help="independently approve a pending acceptance")
    approve.add_argument("candidate")
    commands.add_parser("recover", help="settle an interrupted acceptance by reading the target branch")
    commands.add_parser("log", help="the thread's events")
    export = commands.add_parser("export", help="the thread's full record as JSON")
    export.add_argument("--out")
    commands.add_parser("memory", help="what is stored, why, and how to remove it")
    prune = commands.add_parser("prune", help="delete refs of unaccepted candidates in finished threads")
    prune.add_argument("id", nargs="?")
    compare = commands.add_parser("contract-diff", help="field differences between two contract versions")
    compare.add_argument("a")
    compare.add_argument("b")
    args = parser.parse_args(argv)

    try:
        kit = _kit(args)
        try:
            return _run(kit, args)
        finally:
            kit.close()
    except Rejected as exc:
        print(f"error: {exc.code}: {exc.detail}", file=sys.stderr)
        found = hint(exc.code)
        if found:
            print("next: " + found, file=sys.stderr)
        return 2


def _run(kit: Kit, args) -> int:
    if args.command == "init":
        contracts = sorted(p.name for p in (kit.home / "contracts").glob("*.json"))
        print(f"{kit.home} is ready. Contracts: {', '.join(contracts)}. Policy: policy.json.")
        print("Review the checks in the contract, then: syberlabs start \"what to change\"")
        return 0
    if args.command == "start":
        thread = kit.start(args.objective, args.contract, paths=args.paths)
        (kit.home / "current").write_text(thread.id + "\n")
        print(f"started {thread.id[:8]} on {thread.contract[0]}.v{thread.contract[1]} at {thread.base[:10]}")
        print(f"accepting will move {thread.target_ref}; it never pushes or merges")
        return 0
    if args.command == "threads":
        current = (kit.home / "current").read_text().strip() if (kit.home / "current").exists() else ""
        for row in kit.threads():
            mark = "*" if row["id"] == current else " "
            print(f"{mark} {row['id'][:8]}  {row['contract']:16} {row['objective']}")
        return 0
    if args.command == "use":
        thread = kit.open(args.id)
        (kit.home / "current").write_text(thread.id + "\n")
        print(f"current thread {thread.id[:8]}: {thread.objective}")
        return 0
    if args.command == "memory":
        print(json.dumps(kit.memory(), indent=2))
        return 0
    if args.command == "prune":
        print(f"removed {kit.prune(args.id)} candidate refs")
        return 0
    if args.command == "contract-diff":
        for change in kit.contract_diff(args.a, args.b):
            print(f"{change['path']}: {json.dumps(change['before'])} -> {json.dumps(change['after'])}")
        return 0

    thread = _thread(kit, args)
    if args.command == "status":
        found = thread.status()
        print(json.dumps(found, indent=2)) if args.json else _print_status(found)
    elif args.command == "context":
        excerpts = thread.context(query=args.query, drop=tuple(args.drop))
        total = 0
        for excerpt in excerpts:
            total += len(excerpt.text.encode())
            print(f"{excerpt.path}:{excerpt.start}-{excerpt.end}  ({excerpt.reason}, {len(excerpt.text.encode())} bytes)")
            if args.show:
                print(excerpt.text + "\n")
        print(f"{len(excerpts)} excerpts, {total} bytes. Drop any with --drop PATH; this selection is recorded.")
    elif args.command == "propose":
        if args.from_worktree:
            changes, skipped = thread.worktree_changes()
            if skipped:
                print("left out (outside scope or binary): " + ", ".join(skipped))
            if not changes:
                raise Rejected("no_change", "no in-scope edits in the working tree")
            found = thread.propose(changes=changes, message=args.message or "working-tree edits")
        elif args.evolve:
            from syberlabs.evolve import CommandMutator, EvolutionaryProvider
            mutator = CommandMutator(shlex.split(args.evolve), name=args.name, cwd=kit.repo.root)
            provider = EvolutionaryProvider(mutator, population=args.population, generations=args.generations,
                                            crossover_every=args.crossover_every, seeds=args.seeds,
                                            revision=args.revision)
            found = thread.propose(provider, seed=args.seed)
        else:
            provider = CommandProvider(shlex.split(args.provider_command), name=args.name, revision=args.revision,
                                       cwd=kit.repo.root)
            found = thread.propose(provider)
        search = [e for e in thread.history() if e["kind"] == "search_finished"][-1]["body"]
        for candidate in found:
            scope = "" if candidate.in_scope else "  OUT OF SCOPE: " + ", ".join(candidate.scope_violations + candidate.limit_violations)
            print(f"{candidate.id}: {len(candidate.changed_paths)} files, {candidate.diff_bytes} diff bytes, provisional{scope}")
        print(f"search {search['id']} {search['stopped']}" + (f" ({search['error']})" if search["error"] else "")
              + (f"; provider recommends {', '.join(search['recommended'])} (a signal, not a verdict)" if search["recommended"] else ""))
        if found:
            print(f"next: syberlabs check {found[-1].id}")
    elif args.command == "check":
        verdict = thread.check(args.candidate, rerun=args.rerun)
        if args.json:
            print(json.dumps({"candidate": verdict.candidate, "acceptable": verdict.acceptable, "status": verdict.status,
                              "reason": verdict.reason, "rule": verdict.rule,
                              "checks": [c.__dict__ for c in verdict.checks], "untested": verdict.untested,
                              "base_current": verdict.base_current, "hint": verdict.hint}, indent=2))
        else:
            print(verdict.summary())
            for result in verdict.checks:
                if result.state not in ("passed", "not_run") and result.output_tail:
                    print(f"--- {result.name} output (tail) ---\n{result.output_tail[-1500:]}")
        return 0 if verdict.acceptable else 1
    elif args.command == "diff":
        sys.stdout.write(thread.diff(args.candidate))
    elif args.command == "accept":
        receipt = thread.accept(args.candidate)
        _print_receipt(receipt)
        return 0 if receipt.status == "succeeded" else 1
    elif args.command == "approve":
        _print_receipt(thread.approve(args.candidate, actor=kit.actor, roles=kit.roles))
    elif args.command == "recover":
        receipts = thread.recover()
        for receipt in receipts:
            _print_receipt(receipt)
        if not receipts:
            print("nothing to recover")
    elif args.command == "log":
        for event in thread.history():
            body = event["body"]
            detail = body.get("id") or body.get("candidate") or body.get("proposal_id") or body.get("key") or ""
            extra = body.get("reason") or body.get("stopped") or body.get("status") or ""
            print(f"{event['seq']:4} {event['kind']:22} {str(detail)[:40]:40} {extra}")
    elif args.command == "export":
        data = json.dumps(kit.export(thread.id), indent=2, sort_keys=True)
        if args.out:
            Path(args.out).write_text(data + "\n")
            print(f"wrote {args.out}")
        else:
            print(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
