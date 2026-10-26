"""``syberlabs eval ...``: register, tasks, run, preflight; ``score`` and ``report`` wait for the eval-score packet."""

from __future__ import annotations

import json
from pathlib import Path

from syberlabs.eval.spec import HARNESS

NOT_YET = "not yet: eval-score packet"


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
    run.add_argument("--keep-work", action="store_true", help="keep each run's repository and kit home")
    run.add_argument("--seed-env", default="SYBERLABS_CERTIFICATE_SEED", metavar="NAME",
                     help="the variable naming the certificate seed (unset: runs are recorded uncertified)")
    run.add_argument("--json", action="store_true")
    preflight = steps.add_parser("preflight", help="run the deterministic suites and record them for this commit")
    preflight.add_argument("id")
    for name in ("score", "report"):
        stub = steps.add_parser(name, help=NOT_YET)
        stub.add_argument("id", nargs="?")


def run(args) -> int:
    from syberlabs.eval import runner, spec, tasks

    root = Path(args.evals)
    if args.eval_command in ("score", "report"):
        print(NOT_YET)
        return 2
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
                            keep_work=args.keep_work, seed_env=args.seed_env, log=None if args.json else print)
    if args.json:
        print(json.dumps(found, indent=2, sort_keys=True))
    else:
        for row in found["runs"]:
            print(f"{row['order_index']:4} {row['run_id']:48} {row['state']:7} {row.get('outcome', '')}")
        print(f"{found['eval']}: {len(found['runs'])} of {found['planned']} listed, spent "
              f"${found['spent_dollars']:.4f} of ${found['total_dollars']:.4f}; preflight {found['preflight']}"
              + (f"; stopped: {found['stopped']}" if found["stopped"] else ""))
    return 1 if found["stopped"] else 0
