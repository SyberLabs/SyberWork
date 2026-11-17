"""The queue for the person who merges: every ticket whose thread holds an accepted change, in an order that says
why, with a reading order for each change.

A factory that builds faster than a person can read moves the wait to the merge. This module does not shorten the
reading; it orders it. Two things are computed, from the ledger, the Space graph and Git, and nothing is asked of a
model:

- **The order of the queue.** An ordered list of keys, the first difference decides (``ORDER``): a change that other
  tickets are waiting on comes first (their plans name a path this change touches, so merging it first is what lets
  them build on what will be there); then the change with fewer files; then the smaller diff; then the one that has
  waited longest. Small before large is the shortest-job-first rule: it is the order in which the changes behind
  wait least in total. The keys are printed with every row, so a person who orders differently can see what they
  are overruling.
- **The reading order of one change.** Its files grouped by component, a component after the ones it depends on
  (the Space graph's ``depends_on``), so the code that is called is read before the code that calls it. Each group
  is a layer. A change with several layers can be read, and if the person wants, split, layer by layer; this module
  splits nothing.

Read-only: ``queue`` appends no event, moves no ref and runs no command but ``git diff`` and ``git rev-parse``.
Accepting was a person's act on the thread, and pushing, opening a pull request and merging stay theirs.
"""

from __future__ import annotations

from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.factory import intake, work
from syberlabs.factory import plan as planmod
from syberlabs.gitspace import GitError, Repo

ORDER = ("blocks", "files", "diff_bytes", "waiting_since")  # most first, then fewest, smallest, oldest (``sort_key``)
WAITING = ("open", "plan_held", "plan_rejected", "planned", "claimed", "working")  # not ready, and may hold a plan
NO_COMPONENT = None


def reading_order(space: dict | None, paths: list[str]) -> list[dict]:
    """``[{"layer", "component", "paths"}]``: ``paths`` grouped by the component each belongs to
    (``plan.components_of``; the first by name when globs overlap), a component after every component it depends
    on. Components that depend on each other in a cycle, and ties, go by name. Paths with no component are the last
    layer."""
    groups: dict = {}
    for path in sorted(paths):
        owners = planmod.components_of(space, path)
        groups.setdefault(owners[0] if owners else NO_COMPONENT, []).append(path)
    touched = sorted(name for name in groups if name is not NO_COMPONENT)
    needs = {name: set() for name in touched}
    for edge in (space or {}).get("depends_on", []):
        if edge["from"] in needs and edge["to"] in needs and edge["from"] != edge["to"]:
            needs[edge["from"]].add(edge["to"])
    placed: list = []
    while len(placed) < len(touched):
        free = [name for name in touched if name not in placed and needs[name] <= set(placed)]
        placed.append(free[0] if free else next(name for name in touched if name not in placed))
    layers = [{"component": name, "paths": groups[name]} for name in placed]
    if NO_COMPONENT in groups:
        layers.append({"component": NO_COMPONENT, "paths": groups[NO_COMPONENT]})
    return [{"layer": number, **layer} for number, layer in enumerate(layers, 1)]


def _changed(repo: Repo, base: str | None, commit: str) -> tuple[list[str], int] | None:
    """The paths the accepted commit changes against the thread's base and the size of that diff in bytes, or None
    when either commit is not in this repository any more."""
    if not base:
        return None
    try:
        return repo.changed_paths(base, commit), repo.diff(base, commit, max_bytes=0)[1]
    except GitError:  # one of the two commits is not here
        return None


def sort_key(row: dict) -> tuple:
    """Where a row goes: more waiting tickets first, then fewer files, then the smaller diff, then the longer
    wait. A change whose commit is gone has no diff to measure and goes after every change that can be read."""
    order = row["order"]
    return (not row["found"], -order["blocks"], order["files"], order["diff_bytes"] or 0, order["waiting_since"])


def queue(ledger, *, repo_root: str | Path, now: int | None = None) -> list[dict]:
    """Every ``ready`` ticket, first to read first. Each row: ``position``, ``ticket_id``, ``source``, ``title``
    (the request's visible title), ``kind``, ``size``, ``act_class``, ``human_owner``, ``thread``, ``target_ref``,
    ``commit``, ``paths`` and ``diff_bytes`` (of the accepted commit against the thread's base; the plan's paths
    and None when the commit is gone, with ``found`` False), ``blocks`` (tickets not ready yet whose plan names
    one of these paths), ``overlaps`` (other ready tickets that touch one), ``layers`` (``reading_order``) and
    ``order`` (the keys that placed it)."""
    events = ledger.events()
    space = ledger.latest_space()
    repo = Repo(Path(repo_root).resolve())
    states = work.tickets(ledger, now=now)
    waiting = {s["ticket_id"]: set(s["plan"]["files"] + s["plan"]["new_files"]) for s in states
               if s["status"] in WAITING and s["plan"] is not None and s["plan"]["plan_digest"] is not None}
    rows = []
    for current in states:
        if current["status"] != "ready":
            continue
        ready, started = current["ready"], current["work"]
        found = _changed(repo, (started or {}).get("base"), ready["commit"])
        paths, size = found if found is not None else (sorted(current["plan"]["files"] + current["plan"]["new_files"]), None)
        item = work.find(ledger, current["ticket_id"], events)
        try:
            title = intake.load_snapshot(ledger, item["snapshot_digest"])["visible"]["title"]
        except Rejected:  # a missing or edited snapshot costs the row its title, not the queue its row
            title = ""
        rows.append({"ticket_id": current["ticket_id"], "source": current["source"], "title": " ".join(title.split())[:120],
                     "kind": (item.get("work") or {}).get("kind"), "size": (item.get("work") or {}).get("size"),
                     "act_class": (started or {}).get("act_class"), "human_owner": current["human_owner"],
                     "thread": ready["thread"], "target_ref": ready["target_ref"], "commit": ready["commit"],
                     "found": found is not None, "paths": paths, "diff_bytes": size,
                     "blocks": sorted(tid for tid, planned in waiting.items() if planned & set(paths)),
                     "layers": reading_order(space, paths), "order": {"waiting_since": ready["seq"]}})
    for row in rows:
        row["overlaps"] = sorted(other["ticket_id"] for other in rows
                                 if other is not row and set(other["paths"]) & set(row["paths"]))
        row["order"] = {"blocks": len(row["blocks"]), "files": len(row["paths"]), "diff_bytes": row["diff_bytes"],
                        "waiting_since": row["order"]["waiting_since"]}
    return [{"position": number, **row} for number, row in enumerate(sorted(rows, key=sort_key), 1)]


def text(rows: list[dict]) -> str:
    """The queue as a person reads it in a terminal: one block per change, the keys that placed it, where it is,
    and its layers in reading order."""
    if not rows:
        return "nothing is ready: no ticket's thread holds an accepted change\n"
    lines = []
    for row in rows:
        source = row["source"]
        size = "size unknown" if row["diff_bytes"] is None else f"{row['diff_bytes']} bytes"
        lines.append(f"{row['position']}. {source['kind']}:{source['ref']}  {row['title'] or '(no title)'}")
        lines.append(f"   ticket {row['ticket_id']}  {row['kind'] or 'kind unknown'} {row['size'] or ''}".rstrip()
                     + f"  class {row['act_class'] or 'not recorded'}  owner {row['human_owner']}")
        lines.append(f"   placed by: blocks {len(row['blocks'])}, {len(row['paths'])} file(s), {size}, ready since event "
                     f"{row['order']['waiting_since']}")
        if row["blocks"]:
            lines.append(f"   waiting on it: {', '.join(row['blocks'])}")
        if row["overlaps"]:
            lines.append(f"   touches the same files as: {', '.join(row['overlaps'])} (whichever merges second is rebased)")
        where = f"{row['target_ref']} at {str(row['commit'])[:10]}"
        lines.append(f"   branch {where}; " + ("accepted, not pushed or merged here" if row["found"]
                                               else "that commit is not in this repository, so the plan's paths are listed"))
        many = len(row["layers"]) > 1
        lines.append(f"   read in {len(row['layers'])} layer(s)" + (", each after what it depends on:" if many else ":"))
        for layer in row["layers"]:
            lines.append(f"     {layer['layer']}. {layer['component'] or '(no component)'}: {', '.join(layer['paths'])}")
    lines.append(f"{len(rows)} change(s) ready. Nothing here pushes, splits or merges one.")
    return "\n".join(lines) + "\n"
