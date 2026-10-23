"""``syberlabs``: the Build Thread from a terminal.

    syberlabs init
    syberlabs start "Add a CSV export" --paths src tests
    syberlabs context
    syberlabs propose --from-worktree          # or --command ./my_model_adapter
    syberlabs check c1
    syberlabs findings c1                      # what the contract's critic found
    syberlabs diff c1
    syberlabs accept c1
    syberlabs status
    syberlabs certify c1                         # sign what the journal says about the accepted candidate
    syberlabs verify CERT --public KEY --repo .  # check it with the public key; no Kit, no checks, no model
    syberlabs run --once                       # or a loop: every open thread's next safe step
    syberlabs metrics [--json|--csv] [--check]   # per-work-item metrics, every number from the journal
    syberlabs settle [--thread ID]             # record merges, closes and reviews of the pull requests opened
    syberlabs note --intervention message --note "asked the agent to split the PR"
    syberlabs review --record --verdict pass --reviewer NAME --revision COMMIT

The current thread is remembered in ``.syberlabs/current``. Every command
reads the journal fresh, so a thread can be resumed from any terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from syberlabs import certificate
from syberlabs import critic
from syberlabs import metrics
from syberlabs.build import ACT_CLASSES, Kit, hint
from syberlabs.ed25519 import public_from_seed
from syberlabs.errors import Rejected
from syberlabs.gitspace import Repo
from syberlabs.providers import CommandProvider, PatchProvider, RepairProvider, split_command


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
    if status.get("act_class"):
        print(f"class     {status['act_class']}")
    if status.get("unattended"):
        print("mode      unattended: syberlabs run may accept an acceptable candidate on this thread")
    print(f"contract  {status['contract']}   base {status['base'][:10]}   target {status['target_ref']}"
          + (f" -> {status['target'][:10]}" if status["target"] else " (not created)"))
    print(f"sources   {', '.join(status['sources'])}")
    if status["context"]:
        ctx = status["context"]
        print(f"context   {ctx['files']} files, {ctx['bytes']} bytes, digest {ctx['digest'][:12]}")
    used = status["budget"]
    print(f"budget    candidates {used['candidates'][0]}/{used['candidates'][1]}, "
          f"evaluations {used['evaluations'][0]}/{used['evaluations'][1]}")
    spend = used.get("spend")
    if spend:  # present only when the contract prices usage; dollars are the host's own observation
        cap = f" / ${spend['max_dollars']:.4f}" if spend["max_dollars"] is not None else " (no max_dollars)"
        print(f"spend     ${spend['dollars']:.4f}{cap}, {spend['seconds']:.1f}s over {spend['searches']} searches "
              f"(max {spend['max_seconds']}s each)"
              + ("" if spend["host_observed"] else "; no host-observed usage")
              + (f"; unpriced models: {', '.join(spend['unknown_models'])}" if spend["unknown_models"] else ""))
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
    if receipt.status == "succeeded" and receipt.external_id:
        line += f"\n{receipt.ref}: {receipt.external_id}" + (f" {receipt.url}" if receipt.url else "")
    elif receipt.status == "succeeded" and receipt.commit and receipt.ref and receipt.ref.startswith("refs/"):
        line += f"\n{receipt.ref} now points at {receipt.commit[:10]}. Nothing was pushed or merged."
    print(line)
    if receipt.hint:
        print("next: " + receipt.hint)


def _survive_unicode() -> None:
    """Never crash on a console that cannot encode a check's output (cp1252 and a vitest tick, say)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _survive_unicode()
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
    start.add_argument("--class", dest="act_class", choices=list(ACT_CLASSES),
                       help="act class: R reversible, C canonical, P public claim, X external effect "
                            "(default R when the contract records a class)")
    start.add_argument("--unattended", action="store_true",
                       help="record that syberlabs run may accept an acceptable candidate on this thread")
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
    source.add_argument("--repair", metavar="ADAPTER_COMMAND",
                        help="repair loop, the default proposer path: the command proposes one patch under the search "
                             "protocol, then is asked again with the host's check results and the failed candidate's "
                             "files until a candidate passes or the budget stops it")
    propose.add_argument("--max-calls", type=int,
                         help="for --repair: the most adapter calls in this search (default: the search's candidate budget)")
    propose.add_argument("--population", type=int, default=4)
    propose.add_argument("--generations", type=int, default=6)
    propose.add_argument("--crossover-every", type=int, default=3)
    propose.add_argument("--seed", type=int, default=0)
    propose.add_argument("--from", dest="seeds", nargs="+", default=[], help="candidates to seed the population with")
    propose.add_argument("--exchange", metavar="REMOTE", help="share the population with other hosts through this git remote")
    propose.add_argument("--host", help="this host's name in the exchange")
    propose.add_argument("--topic", help="what the hosts are working on; hosts with the same topic and base exchange")
    propose.add_argument("--migrate-every", type=int, default=2)
    propose.add_argument("--migrants", type=int, default=1)
    propose.add_argument("--policy", choices=("fixed", "empirical", "jev"), default="fixed",
                         help="branch policy for --evolve: fixed (the schedule), empirical (Thompson sampling over this "
                              "search's rounds) or jev (one action per decision, by expected admission per dollar)")
    propose.add_argument("--message", default="")
    propose.add_argument("--name", help="provider name, recorded as the acting agent platform; "
                                        "default patch for --from-worktree, repair for --repair, command otherwise")
    propose.add_argument("--revision", default="1")
    check = commands.add_parser("check", help="run the contract's checks and ask admission")
    check.add_argument("candidate")
    check.add_argument("--rerun", action="store_true")
    check.add_argument("--json", action="store_true")
    findings = commands.add_parser("findings", help="the critic's findings from a candidate's latest evaluation")
    findings.add_argument("candidate")
    diff = commands.add_parser("diff", help="show a candidate's diff against the thread base")
    diff.add_argument("candidate")
    accept = commands.add_parser("accept", help="accept a candidate: moves only the thread's target branch")
    accept.add_argument("candidate")
    approve = commands.add_parser("approve", help="independently approve a pending acceptance or publishing action")
    approve.add_argument("candidate", nargs="?")
    approve.add_argument("--action", help="approve a pending publishing action instead of an acceptance")
    publish = commands.add_parser("publish", help="run a publishing action after acceptance (push, pull request, command)")
    publish.add_argument("action")
    commands.add_parser("recover", help="settle an interrupted acceptance by reading the target branch")
    run = commands.add_parser("run", help="drive every open thread through its next safe step, from the journal; "
                                          "recovers first, never proposes")
    run.add_argument("--once", action="store_true", help="one pass, then exit: 0 nothing refused, 1 a step was refused, "
                                                         "2 unexpected error")
    run.add_argument("--interval", type=float, default=30.0, help="seconds between passes (default 30)")
    run.add_argument("--thread", dest="run_thread", help="only this thread (id or unique prefix)")
    run.add_argument("--publish", action="store_true",
                     help="also run the contract's publishing actions after acceptance (off by default)")
    commands.add_parser("log", help="the thread's events")
    export = commands.add_parser("export", help="the thread's full record as JSON")
    export.add_argument("--out")
    certify = commands.add_parser("certify", help="sign what the journal says about an accepted candidate (DSSE, Ed25519)")
    certify.add_argument("candidate")
    certify.add_argument("--out", help="certificate path (default .syberlabs/certificates/<thread>-<candidate>.json); "
                                       "the public key is written beside it as .pub")
    certify.add_argument("--seed-env", default=certificate.DEFAULT_SEED_ENV, metavar="NAME",
                         help="environment variable holding the signing seed, as the path of a 32-byte file or 64 hex "
                              "characters (default SYBERLABS_CERTIFICATE_SEED); only the name is recorded")
    certify.add_argument("--bare", action="store_true",
                         help="sign the payload under its own payloadType (application/vnd.syberlabs.admission-certificate+json) "
                              "instead of wrapping it in an in-toto Statement (application/vnd.in-toto+json); verify reads both")
    verify = commands.add_parser("verify", help="check a certificate with its public key; builds no Kit and runs nothing")
    verify.add_argument("certificate")
    verify.add_argument("--public", required=True, metavar="KEY", help="path to the signer's public key (the .pub file)")
    verify.add_argument("--repo", dest="verify_repo", nargs="?", const=".", default=None, metavar="PATH",
                        help="also re-derive commits, trees and the diff from this repository, and the certified journal "
                             "prefix under --home when the thread's journal is present (PATH defaults to .)")
    measure = commands.add_parser("metrics", help="per-work-item metrics computed from the journal (docs/METRICS.md)")
    measure.add_argument("--thread", dest="metrics_thread", help="only this thread (id or prefix)")
    measure.add_argument("--since", help="only threads started on or after this date (YYYY-MM-DD or ISO 8601)")
    shape = measure.add_mutually_exclusive_group()
    shape.add_argument("--json", action="store_true", help="items with every metric's basis, and the summary")
    shape.add_argument("--csv", action="store_true")
    measure.add_argument("--check", action="store_true",
                         help="exit 1 when a delivered item has an unknown metric (for CI over admitted work)")
    measure.add_argument("--from-export", dest="exports", nargs="+", metavar="FILE",
                         help="read thread records written by syberlabs export instead of this home's journal")
    settling = commands.add_parser("settle", help="poll GitHub once for the pull requests published threads opened; "
                                                  "record pr_merged, pr_closed and pr_review facts (docs/METRICS.md)")
    settling.add_argument("--thread", dest="settle_thread", help="only this thread (id or unique prefix)")
    note = commands.add_parser("note", help="record that a person acted on the thread (a human_intervention fact)")
    note.add_argument("--intervention", required=True, choices=list(metrics.INTERVENTION_KINDS))
    note.add_argument("--note", help=f"what was done, at most {metrics.MAX_NOTE} characters; never a secret")
    review = commands.add_parser("review", help="record an independent verifier's verdict (a review fact), or list them")
    review.add_argument("--record", action="store_true", help="record a verdict; without it, list the recorded ones")
    review.add_argument("--verdict", choices=list(metrics.VERDICTS))
    review.add_argument("--reviewer")
    review.add_argument("--revision", help="the commit or revision reviewed")
    review.add_argument("--findings-high", type=int)
    review.add_argument("--url", help="the verification comment (https)")
    commands.add_parser("memory", help="what is stored, why, and how to remove it")
    inspect = commands.add_parser("inspect", help="read-only local web inspector for threads, lineage, diffs, and history")
    inspect.add_argument("--port", type=int, default=0)
    forget = commands.add_parser("forget", help="delete a thread's history as the policy's retention rule allows")
    forget.add_argument("id")
    forget.add_argument("--reason", required=True)
    forget.add_argument("--export", help="write the full record to this file first")
    prune = commands.add_parser("prune", help="delete refs of unaccepted candidates in finished threads")
    prune.add_argument("id", nargs="?")
    compare = commands.add_parser("contract-diff", help="field differences between two contract versions")
    compare.add_argument("a")
    compare.add_argument("b")
    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            return _verify(args)  # no Kit: works outside any repository and has none of Kit.local's side effects
        if args.command == "metrics" and args.exports:
            return _metrics(args, None)  # exported records only: no Kit, no repository
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


def _verify(args) -> int:
    """``syberlabs verify``: the key and the file, then Git and the journal when ``--repo`` names a repository."""
    public = certificate.read_public(args.public)
    path = Path(args.certificate)
    try:
        with open(path, "rb") as handle:
            data = handle.read(certificate.MAX_CERTIFICATE_BYTES + 1)
    except OSError as exc:
        raise Rejected("certificate_unreadable", f"cannot read {path}: {exc.strerror or type(exc).__name__}") from None
    repo = home = None
    if args.verify_repo is not None:
        repo = Repo(args.verify_repo)
        home = Path(args.home)
        home = home if home.is_absolute() else repo.root / home
    report = certificate.verify_certificate(data, public, repo=repo, home=home)
    for line in certificate.format_report(report):
        print(line)
    return 0 if report.passed else 1


def _metrics(args, kit: Kit | None) -> int:
    """``syberlabs metrics``: fold the journal (or exported records) into work items; print, and check if asked."""
    events, contracts, seen = [], {}, set()
    if kit is None:
        for path in args.exports:
            try:
                record = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise Rejected("export_unreadable", f"{path}: {type(exc).__name__}") from None
            found = record.get("events") if isinstance(record, dict) else record
            if not isinstance(found, list) or not all(
                    isinstance(e, dict) and {"case_id", "seq", "kind", "body", "hash"} <= set(e) for e in found):
                raise Rejected("export_unreadable", f"{path}: no journal event list")
            contract = record.get("contract") if isinstance(record, dict) else None
            if isinstance(contract, dict) and "id" in contract and "version" in contract:
                contracts[(contract["id"], contract["version"])] = contract
            events += found
    else:
        for case_id in kit.journal.thread_ids():
            try:
                events += kit.session.history(case_id)
            except Rejected:
                continue  # an unreadable thread is the dashboard's to show, not a number here
        contracts = dict(kit.session.contracts)
    events = [e for e in events if e["hash"] not in seen and not seen.add(e["hash"])]
    items = metrics.work_items(events, contracts)
    wanted = args.metrics_thread or args.thread
    if wanted:
        items = [item for item in items if item.thread.startswith(wanted)]
    try:
        items = metrics.since(items, args.since)
    except ValueError as exc:
        raise Rejected("invalid_since", str(exc)) from None
    found = {**metrics.summary(items), "since": args.since}
    if args.json:
        print(json.dumps({"items": [item.to_dict() for item in items], "summary": found}, indent=2))
    elif args.csv:
        sys.stdout.write(metrics.to_csv(items))
    else:
        print(metrics.table(items))
        rate = found["delivered_rate"]
        print(f"{found['items']} items, {found['opened']} opened, delivered {found['delivered']['yes']} "
              f"(unknown {found['delivered']['unknown']}), rate "
              + (f"{rate:.2f}" if rate is not None else metrics.UNKNOWN)
              + "; unknown means the fact is not in the record, never zero")
    missing = metrics.incomplete(items) if args.check else []
    for thread, names in missing:
        print(f"incomplete: {thread[:8]} delivered with unknown {', '.join(names)}", file=sys.stderr)
    return 1 if missing else 0


def _settle(kit: Kit, args) -> int:
    """``syberlabs settle``: one settlement pass. Exit 0 when every pull request was read, 1 when one was not."""
    from syberlabs import settle
    try:
        found = settle.settle(kit, thread=args.settle_thread or args.thread)
    except Rejected:
        raise
    except Exception as exc:
        print(settle.describe(exc, kit), file=sys.stderr)
        return 1
    for item in found:
        print(item.line, file=sys.stderr if item.error else sys.stdout)
    if not found:
        print("no published pull request to settle")
    return 1 if any(item.error for item in found) else 0


def _intervened(thread, kit: Kit, kind: str, proposal: str | None = None) -> None:
    """A thread command run by a person (not the host, not the unattended runner) leaves a human_intervention fact."""
    if thread.is_person(kit.actor):
        thread.record_intervention(kind, by=kit.actor, proposal=proposal)


def _run(kit: Kit, args) -> int:
    if args.command == "init":
        contracts = sorted(p.name for p in (kit.home / "contracts").glob("*.json"))
        print(f"{kit.home} is ready. Contracts: {', '.join(contracts)}. Policy: policy.json.")
        print("Review the checks in the contract, then: syberlabs start \"what to change\"")
        return 0
    if args.command == "start":
        interrupted = {row["id"] for row in kit.threads() if row["state"] == "incomplete"}
        thread = kit.start(args.objective, args.contract, paths=args.paths, act_class=args.act_class,
                           unattended=args.unattended)
        (kit.home / "current").write_text(thread.id + "\n")
        verb = "completed interrupted start of" if thread.id in interrupted else "started"
        print(f"{verb} {thread.id[:8]} on {thread.contract[0]}.v{thread.contract[1]} at {thread.base[:10]}")
        if thread.act_class:
            print(f"act class {thread.act_class}; a publishing effect states it with the admitting key")
        if thread.unattended:
            print("unattended: syberlabs run may accept an acceptable candidate on this thread")
        print(f"accepting will move {thread.target_ref}; it never pushes or merges")
        return 0
    if args.command == "threads":
        current = (kit.home / "current").read_text().strip() if (kit.home / "current").exists() else ""
        for row in kit.threads():
            mark = "*" if row["id"] == current else " "
            note = "  [incomplete: start again with the same objective, or forget]" if row["state"] == "incomplete" else ""
            print(f"{mark} {row['id'][:8]}  {row['contract']:16} {row['objective']}{note}")
        return 0
    if args.command == "use":
        thread = kit.open(args.id)
        (kit.home / "current").write_text(thread.id + "\n")
        print(f"current thread {thread.id[:8]}: {thread.objective}")
        return 0
    if args.command == "inspect":
        from syberlabs.inspector import make_inspector
        server = make_inspector(kit, port=args.port)
        print(f"inspector (read-only): {server.url}")
        print("The token in the address is this run's access key. Ctrl-C stops the inspector.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if args.command == "memory":
        print(json.dumps(kit.memory(), indent=2))
        return 0
    if args.command == "forget":
        if args.export:
            Path(args.export).write_text(json.dumps(kit.export(args.id), indent=2, sort_keys=True) + "\n")
            print(f"wrote {args.export}")
        found = kit.forget(args.id, reason=args.reason)
        print(f"forgot {found['forgotten'][:8]}: history deleted, {found['candidate_refs_removed']} candidate refs removed; "
              f"{found['kept']} and anything published are unchanged")
        if found["receipt"]:
            print(f"receipt kept: {len(found['receipt']['effects'])} effects, head {found['receipt']['head'][:12]}")
        return 0
    if args.command == "prune":
        print(f"removed {kit.prune(args.id)} candidate refs")
        return 0
    if args.command == "contract-diff":
        for change in kit.contract_diff(args.a, args.b):
            print(f"{change['path']}: {json.dumps(change['before'])} -> {json.dumps(change['after'])}")
        return 0
    if args.command == "metrics":
        return _metrics(args, kit)
    if args.command == "settle":
        return _settle(kit, args)
    if args.command == "run":
        if args.interval < 0:
            raise Rejected("invalid_interval", "--interval is seconds, zero or more")
        return kit.run(once=args.once, interval=args.interval, thread=args.run_thread or args.thread,
                       publish=args.publish, log=print)

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
            found = thread.propose(PatchProvider(changes, args.message or "working-tree edits",
                                                 name=args.name or "patch", revision=args.revision))
        elif args.evolve:
            from syberlabs.evolve import CommandMutator, EvolutionaryProvider
            mutator = CommandMutator(split_command(args.evolve), name=args.name or "command", cwd=kit.repo.root)
            exchange = None
            if args.exchange:
                if not args.host or not args.topic:
                    raise Rejected("invalid_exchange", "--exchange needs --host and --topic")
                exchange = kit.exchange(args.exchange, host=args.host, topic=args.topic)
            policy = None  # FixedPolicy, built by the provider
            if args.policy == "empirical":
                from syberlabs.evolve import EmpiricalPolicy
                policy = EmpiricalPolicy(population=args.population, generations=args.generations,
                                         crossover_every=args.crossover_every, seeds=args.seeds,
                                         migrate_every=args.migrate_every if exchange else 0)
            elif args.policy == "jev":
                from syberlabs.evolve import JevPolicy
                policy = JevPolicy(generations=args.generations, seeds=args.seeds)
            provider = EvolutionaryProvider(mutator, population=args.population, generations=args.generations,
                                            crossover_every=args.crossover_every, seeds=args.seeds,
                                            name=args.name or "command", revision=args.revision, exchange=exchange,
                                            migrate_every=args.migrate_every if exchange else 0,
                                            migrants=args.migrants, policy=policy)
            found = thread.propose(provider, seed=args.seed)
        elif args.repair:
            provider = RepairProvider(split_command(args.repair), name=args.name or "repair", revision=args.revision,
                                      cwd=kit.repo.root, max_calls=args.max_calls)
            found = thread.propose(provider, seed=args.seed)
        else:
            provider = CommandProvider(split_command(args.provider_command), name=args.name or "command", revision=args.revision,
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
        if args.rerun:
            _intervened(thread, kit, "override")
        if args.json:
            print(json.dumps({"candidate": verdict.candidate, "acceptable": verdict.acceptable, "status": verdict.status,
                              "reason": verdict.reason, "rule": verdict.rule,
                              "checks": [c.__dict__ for c in verdict.checks], "untested": verdict.untested,
                              "base_current": verdict.base_current, "hint": verdict.hint}, indent=2))
        else:
            print(verdict.summary())
            for result in verdict.checks:
                if result.state not in ("passed", "not_run") and result.output_tail:
                    tail = result.output_tail[-1500:]
                    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
                    # The stream was reconfigured at entry when it could be; a stream that could
                    # not be is still given text it can encode.
                    tail = tail.encode(encoding, errors="replace").decode(encoding, errors="replace")
                    print(f"--- {result.name} output (tail) ---\n{tail}")
        return 0 if verdict.acceptable else 1
    elif args.command == "findings":
        view = thread._view(args.candidate)
        if not critic.critic_checks(thread.config["evaluation"]["checks"]):
            print("this contract has no critic check (a check named critic or critic.<reviewer>)")
            return 0
        reviews = critic.reviews(thread.history(), view)
        if not reviews:
            print(f"no critic observation for {args.candidate}; run: syberlabs check {args.candidate}")
            return 1
        for name, found in reviews:
            for finding in found["findings"]:
                line = "" if finding.get("line") is None else str(finding["line"])
                print(f"{finding['severity']} {finding['file']}:{line} {finding['claim']}")
            if found.get("truncated"):
                print(f"[{name}: findings truncated in the record; full document digest {found['findings_digest']}]")
        if not any(found["findings"] for _name, found in reviews):
            print("no findings")
    elif args.command == "diff":
        sys.stdout.write(thread.diff(args.candidate))
    elif args.command == "accept":
        receipt = thread.accept(args.candidate)
        if receipt.proposal_id:
            _intervened(thread, kit, "approve", receipt.proposal_id)
        _print_receipt(receipt)
        return 0 if receipt.status == "succeeded" else 1
    elif args.command == "approve":
        if args.action:
            receipt = thread.approve_action(args.action, actor=kit.actor, roles=kit.roles)
        elif args.candidate:
            receipt = thread.approve(args.candidate, actor=kit.actor, roles=kit.roles)
        else:
            raise Rejected("nothing_to_approve", "name a candidate or --action")
        _intervened(thread, kit, "approve", receipt.proposal_id)
        _print_receipt(receipt)
    elif args.command == "note":
        event = thread.record_intervention(args.intervention, by=kit.actor, note=args.note)
        print(f"recorded human_intervention {args.intervention} by {kit.actor} ({event['hash'][:12]})")
    elif args.command == "review":
        if args.record:
            if not (args.verdict and args.reviewer and args.revision):
                raise Rejected("invalid_review", "--record needs --verdict, --reviewer and --revision")
            event = thread.record_review(args.verdict, reviewer=args.reviewer, revision=args.revision, by=kit.actor,
                                         findings_high=args.findings_high, url=args.url)
            print(f"recorded review {args.verdict} by {args.reviewer} on {args.revision[:12]} ({event['hash'][:12]})")
        else:
            found = [metrics.fact(e, metrics.REVIEW) for e in thread.history()]
            found = [value for value in found if value is not None]
            for value in found:
                print(f"{value['verdict']:4} {value['reviewer']} {value['revision'][:12]} {value['at']}"
                      + (f" {value['comment_url']}" if value.get("comment_url") else ""))
            if not found:
                print("no review recorded; record one with: syberlabs review --record --verdict pass|fail "
                      "--reviewer NAME --revision COMMIT")
    elif args.command == "publish":
        receipt = thread.publish(args.action)
        _print_receipt(receipt)
        return 0 if receipt.status == "succeeded" else 1
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
    elif args.command == "certify":
        seed = certificate.load_seed(args.seed_env)
        envelope, payload = certificate.certify(kit, thread.id, args.candidate, seed=seed, seed_env=args.seed_env,
                                                bare=args.bare)
        out = Path(args.out) if args.out else kit.home / "certificates" / f"{thread.id[:8]}-{args.candidate}.json"
        cert, pub = certificate.write_certificate(envelope, public_from_seed(seed), out)
        print(certificate.summary(payload))
        print(f"wrote {cert} and {pub} (ed25519 key {envelope['signatures'][0]['keyid'][:10]}, "
              f"seed from ${args.seed_env}, signed in this process)")
        print(f"next: syberlabs verify {cert} --public {pub} --repo .")
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
