"""The refusal report: every proposal the admission kernel refused, as a query over the journal.

    python -m syberlabs.refusals --home .syberlabs            # text
    python -m syberlabs.refusals --home .syberlabs --json     # one object per refusal

A refusal is a ``decision`` event whose status is ``denied``. Each row carries the
deciding rule (from the journal's side channel, which keeps the rule id outside the
hashed decision body), the reason code, the candidate the proposal named, and the
states and output digests of the newest evaluation of that candidate's exact tree
that existed when the decision was made. Everything is read through the session's
read API; nothing here appends an event or takes a write path.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from syberlabs.build import Kit
from syberlabs.clock import as_seconds

CHECK_FIELDS = ("name", "state", "exit_code", "duration_ms", "output_digest")


def when(at: int | float | None) -> str:
    """An event's ``at`` (integer microseconds, or legacy float seconds) as UTC text."""
    if at is None:
        return ""
    return datetime.fromtimestamp(as_seconds(at), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def thread_refusals(kit: Kit, case_id: str) -> list[dict]:
    """Refused proposals of one thread, oldest first."""
    session = kit.session
    events = session.history(case_id)
    rules = {row["seq"]: row.get("rule") for row in session.side_channel(case_id)}
    views = {view["id"]: view for view in session.candidates(case_id)}
    by_commit = {view["commit"]: view["id"] for view in views.values()}
    proposals = {e["body"]["id"]: e["body"] for e in events if e["kind"] == "proposed"}
    row = session.cases[case_id]
    rows = []
    for event in events:
        if event["kind"] != "decision" or event["body"].get("status") != "denied":
            continue
        body = event["body"]
        proposal = proposals.get(body.get("proposal_id"), {})
        args = proposal.get("args") if isinstance(proposal.get("args"), dict) else {}
        candidate = args.get("candidate")
        if not isinstance(candidate, str):
            candidate = by_commit.get(args.get("commit"))
        evaluation = None
        if candidate is not None:
            evaluation = next((e for e in reversed(events) if e["seq"] < event["seq"] and e["kind"] == "candidate_evaluated"
                               and e["body"].get("candidate") == candidate), None)
        view = views.get(candidate) if candidate is not None else None
        rows.append({
            "thread": case_id,
            "objective": row["inputs"].get("objective"),
            "act_class": row["inputs"].get("act_class"),
            "seq": event["seq"],
            "at": event["at"],
            "event_hash": event["hash"],
            "proposal_id": proposal.get("id") or body.get("proposal_id"),
            "action": proposal.get("action"),
            "actor": proposal.get("actor"),
            "origin": proposal.get("origin"),
            "phase": body.get("phase", "propose"),
            "rule": rules.get(event["seq"]),
            "reason": body.get("reason"),
            "candidate": candidate,
            "commit": view["commit"] if view else args.get("commit"),
            "provider": f"{view['provider']['name']}@{view['provider']['revision']}" if view else None,
            "evaluation_hash": evaluation["hash"] if evaluation else None,
            "checks": [{key: check.get(key) for key in CHECK_FIELDS} for check in evaluation["body"]["checks"]]
            if evaluation else [],
        })
    return rows


def refusals(kit: Kit, thread: str | None = None) -> list[dict]:
    """Every refusal in the journal (or in the threads whose id starts with ``thread``)."""
    rows: list[dict] = []
    for case_id in kit.journal.thread_ids():
        if thread and not case_id.startswith(thread):
            continue
        rows.extend(thread_refusals(kit, case_id))
    return rows


def render_text(rows: list[dict]) -> str:
    if not rows:
        return "no refusals recorded\n"
    lines = []
    for row in rows:
        target = row["candidate"] or (row["commit"][:10] if row["commit"] else "-")
        lines.append(f"{row['thread'][:8]} #{row['seq']:<4} {when(row['at'])}  {row['action']} {target}: "
                     f"{row['reason']}  (rule {row['rule'] or '?'}, {row['phase']}, actor {row['actor']})")
        for check in row["checks"]:
            exit_code = "" if check["exit_code"] in (None, 0) else f" exit {check['exit_code']}"
            digest = (check["output_digest"] or "")[:12]
            lines.append(f"    {check['state']:9} {check['name']}{exit_code}  {check['duration_ms']} ms  output {digest}")
    lines.append(f"{len(rows)} refusal{'s' if len(rows) != 1 else ''}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syberlabs.refusals", description="Every proposal the admission kernel refused.")
    parser.add_argument("--home", default=".syberlabs")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--thread", help="thread id or prefix (default: every thread)")
    parser.add_argument("--json", action="store_true", help="print one JSON object per refusal, as a list")
    args = parser.parse_args(argv)
    kit = Kit.local(args.home, args.repo)
    try:
        rows = refusals(kit, args.thread)
    finally:
        kit.close()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_text(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
