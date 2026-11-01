"""``syberlabs eval ...``: register, tasks, run, preflight, score, report."""

from __future__ import annotations

import json
from pathlib import Path

from syberlabs.eval.spec import HARNESS


def add_parser(commands) -> None:
    """Register ``syberlabs eval ...`` on the CLI's subcommands."""
    tool = commands.add_parser("eval", help="pre-registered evaluations run through the real Build Thread "
                                            "(docs/EVAL.md)")
    tool.add_argument("--evals", default=str(HARNESS / "evals"),
                      help="the evals directory (default: evals/ of this checkout)")
    steps = tool.add_subparsers(dest="eval_command", required=True)
    register = steps.add_parser("register", help="freeze a spec: write registration.json beside it")
    register.add_argument("spec", help="evals/<id>/spec.json")
    tasks = steps.add_parser("tasks", help="import and list tasks")
    tasks_steps = tasks.add_subparsers(dest="tasks_command", required=True)
    importing = tasks_steps.add_parser("import-fixtures", help="write evals/tasks/<generation>-<fixture>/ from "
                                                               "benchmarks/fixtures.py, its checks moved to hidden/")
    importing.add_argument("--fixtures", nargs="+", help="fixture names (default: the v3 and v4 fixtures)")
    importing.add_argument("--force", action="store_true", help="overwrite a task directory that holds another task")
    listing = tasks_steps.add_parser("list", help="each task's digest and the set digest of the tasks listed")
    listing.add_argument("ids", nargs="*", help="task ids (default: every task)")
    listing.add_argument("--json", action="store_true")
    run = steps.add_parser("run", help="run every task x arm x seed of a registered spec, in its registered order")
    run.add_argument("id")
    run.add_argument("--only", metavar="TASK,ARM,SEED", help="only these runs; empty or * is any")
    run.add_argument("--dry", action="store_true", help="print the order and what is done; run nothing")
    run.add_argument("--work", help="where runs' temporary repositories go (default: the system temporary directory)")
    run.add_argument("--keep-work", action="store_true", help="keep each run's repository, kit home and hidden copy")
    run.add_argument("--hidden-root", metavar="DIR",
                     help="the host root for each run's kit home and hidden copy, each under a random name "
                          "(default: <system temp>/syberlabs-eval-host)")
    run.add_argument("--seed-env", default="SYBERLABS_CERTIFICATE_SEED", metavar="NAME",
                     help="the variable naming the certificate seed (unset: runs are recorded uncertified)")
    run.add_argument("--json", action="store_true")
    preflight = steps.add_parser("preflight", help="run the deterministic suites and record them for this commit")
    preflight.add_argument("id")
    scoring = steps.add_parser("score", help="the safety gate, task quality and efficiency of every run: "
                                             "runs/<run>/score.json and index.json")
    scoring.add_argument("id")
    scoring.add_argument("--json", action="store_true")
    reporting = steps.add_parser("report", help="the paired analysis with the decision rule applied: report.md and "
                                                "report.json")
    reporting.add_argument("id")
    reporting.add_argument("--partial", action="store_true",
                           help="report although registered runs are missing; labelled partial everywhere")
    reporting.add_argument("--json", action="store_true")


def run(args) -> int:
    from syberlabs.eval import runner, spec, tasks

    root = Path(args.evals)
    if args.eval_command == "score":
        from syberlabs.eval import score

        index = score.score_eval(root, args.id, log=None if args.json else print)
        if args.json:
            print(json.dumps(index, indent=2, sort_keys=True))
        else:
            gates = [row["gate"] for row in index["runs"].values()]
            print(f"{args.id}: scored {len(gates)} run(s); gate pass {gates.count('pass')}, fail {gates.count('fail')}, "
                  f"incomplete {gates.count('incomplete')}; index {score.sha256(root / args.id / 'index.json')[:12]}")
        return 0
    if args.eval_command == "report":
        from syberlabs.eval import report

        found, md, js = report.write(root, args.id, partial=args.partial)
        if args.json:
            print(json.dumps(found, indent=2, sort_keys=True))
        else:
            print(found["decision"]["line"])
            print(f"safety gate: pass {found['safety']['gate']['pass']}, fail {found['safety']['gate']['fail']}, "
                  f"incomplete {found['safety']['gate']['incomplete']}"
                  + ("; a run failed the gate: no capability claim" if found["safety"]["claim_blocked"] else ""))
            print(f"wrote {md} ({report.digest_of(md)[:12]}) and {js} ({report.digest_of(js)[:12]})")
        return 0
    if args.eval_command == "register":
        found = spec.register(args.spec)
        print(f"registered {found['eval']}: spec {found['spec_digest'][:12]}, {len(found['task_digests'])} tasks "
              f"(set {found['task_set_digest'][:12]}), {len(found['arm_digests'])} arms; "
              f"at {found['registered_at']} on {str(found['git_commit'])[:10]}"
              + (" (checkout differs from its commit)" if found["git_dirty"] else ""))
        return 0
    if args.eval_command == "tasks":
        tasks_root = root / "tasks"
        if args.tasks_command == "import-fixtures":
            for item in tasks.import_fixtures(tasks_root, args.fixtures, force=args.force):
                print(f"{item['id']}  {item['digest']}")
            return 0
        ids = args.ids or tasks.list_tasks(tasks_root)
        digests = {task_id: tasks.task_digest(tasks_root, task_id) for task_id in ids}
        found = {"tasks": digests, "set_digest": tasks.set_digest(digests)}
        if args.json:
            print(json.dumps(found, indent=2, sort_keys=True))
        else:
            for task_id, value in digests.items():
                print(f"{task_id}  {value}")
            print(f"set  {found['set_digest']}")
        return 0
    if args.eval_command == "preflight":
        found = runner.preflight(root, args.id, log=print)
        print(f"preflight {'passed' if found['passed'] else 'FAILED'} on {str(found['commit'])[:10]}"
              + (" (checkout differs from its commit: run will refuse it)" if found["dirty"] else ""))
        return 0 if found["passed"] else 1
    found = runner.run_eval(root, args.id, only=args.only, dry=args.dry, work_root=args.work,
                            keep_work=args.keep_work, seed_env=args.seed_env, log=None if args.json else print,
                            hidden_root=args.hidden_root)
    if args.json:
        print(json.dumps(found, indent=2, sort_keys=True))
    else:
        for row in found["runs"]:
            print(f"{row['order_index']:4} {row['run_id']:48} {row['state']:7} {row.get('outcome', '')}")
        print(f"{found['eval']}: {len(found['runs'])} of {found['planned']} listed, spent "
              f"${found['spent_dollars']:.4f} of ${found['total_dollars']:.4f}; preflight {found['preflight']}"
              + (f"; stopped: {found['stopped']}" if found["stopped"] else ""))
    return 1 if found["stopped"] else 0
