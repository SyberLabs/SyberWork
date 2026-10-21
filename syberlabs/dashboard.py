"""The operator page: one self-contained HTML file computed from the journals alone.

    python -m syberlabs.dashboard --home .syberlabs --out dashboard.html

Threads, candidates, checks, refusals, effects and their settlements, and any
admission certificates under ``<home>/certificates``, read through the same
session read API the terminal commands use (``Kit.open``, ``Thread.status``,
``Thread.history``, ``Session.candidates``). Nothing on the page is authored by
hand and nothing here appends an event or takes a write path. The page has no
script, no external stylesheet or font, and follows ``prefers-color-scheme``.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import time
from pathlib import Path

from syberlabs.build import Kit
from syberlabs.clock import stamp
from syberlabs.errors import Rejected
from syberlabs.evidence import verified_reconciliation
from syberlabs.refusals import thread_refusals, when

SETTLED = ("applied", "not_applied")
OBJECTIVE_CHARS = 140


def _escape(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _short(value, length: int = 10) -> str:
    return str(value)[:length] if value else ""


# Model: dictionaries computed from the journal, so a test can check them without parsing HTML.

def thread_effects(case_id: str, events: list[dict], views: list[dict]) -> list[dict]:
    """Every claimed effect of one thread with its settlement, in claim order."""
    proposals = {e["body"]["id"]: e["body"] for e in events if e["kind"] == "proposed"}
    by_commit = {view["commit"]: view["id"] for view in views}
    claims: dict[str, dict] = {}
    for event in events:
        body = event["body"]
        kind = event["kind"]
        if kind == "effect_started":
            proposal = proposals.get(body["proposal_id"], {})
            args = proposal.get("args") if isinstance(proposal.get("args"), dict) else {}
            candidate = args.get("candidate") if isinstance(args.get("candidate"), str) else by_commit.get(args.get("commit"))
            claims[body["proposal_id"]] = {
                "thread": case_id, "proposal_id": body["proposal_id"], "action": body.get("action"),
                "actor": body.get("actor"), "candidate": candidate, "claimed_at": event["at"], "claim_hash": event["hash"],
                "verdict": "pending", "detail": "claimed; no outcome recorded", "settled_at": None,
                "external_id": None, "destination": None, "url": None,
            }
            continue
        claim = claims.get(body.get("proposal_id")) if isinstance(body, dict) else None
        if claim is None or claim["verdict"] in SETTLED:
            continue
        if kind == "effect_succeeded":
            output = body.get("output") if isinstance(body.get("output"), dict) else {}
            claim.update(verdict="applied", detail="effect_succeeded", settled_at=event["at"],
                         external_id=output.get("external_id") or output.get("new"),
                         destination=output.get("destination") or output.get("ref"), url=output.get("url") or None)
        elif verified_reconciliation(event):
            proof = body["proof"]
            claim.update(verdict="applied", detail="reconciled: destination state matched", settled_at=event["at"],
                         external_id=proof.get("external_id"), destination=proof.get("destination"))
        elif kind == "effect_rejected":
            status = body.get("status")
            claim.update(verdict="not_applied", settled_at=event["at"],
                         detail="reconciled: destination unchanged" if status == "not_applied" else f"no write ({status})")
        elif kind == "effect_unknown":
            claim.update(detail=f"outcome unknown ({body.get('error')}); run syberlabs recover")
        elif kind == "reconciliation_checked":
            claim.update(detail=f"pending: {body.get('reason')}")
    return list(claims.values())


def thread_model(kit: Kit, case_id: str) -> dict:
    """One thread's state, candidates, effects and refusals, all from its journal."""
    try:
        thread = kit.open(case_id)
        status = thread.status()
    except Rejected as exc:
        first = kit.journal.first(case_id) or {}
        return {"id": case_id, "objective": (first.get("inputs") or {}).get("objective"), "act_class": None,
                "contract": None, "state": f"unreadable ({exc.code})", "error": exc.detail, "candidates": [],
                "accepted": None, "head": None, "last_at": None, "events": 0, "chain_valid": False,
                "effects": [], "refusals": [], "base": None, "target_ref": None, "target": None, "next": [], "open": []}
    events = thread.history()
    views = kit.session.candidates(case_id)
    candidates = []
    for view in views:
        candidates.append({
            "id": view["id"], "provider": f"{view['provider']['name']}@{view['provider']['revision']}",
            "operator": view["operator"], "parents": list(view["parents"]), "files": len(view["changed_paths"]),
            "diff_bytes": view["diff"]["bytes"], "commit": view["commit"],
            "in_scope": not view["scope_violations"] and not view["limit_violations"],
            "evaluation": view["evaluation"]["state"],
            "checks": [{"name": c["name"], "state": c["state"], "exit_code": c.get("exit_code"),
                        "duration_ms": c.get("duration_ms"), "output_digest": c.get("output_digest")}
                       for c in view["evaluation"]["checks"]],
            "promotion": view["promotion"]["state"], "reason": view["promotion"]["reason"],
            "authoritative": view["authoritative"], "note": view.get("note") or "",
        })
    return {
        "id": case_id, "objective": status["objective"], "act_class": status["act_class"], "contract": status["contract"],
        "state": status["status"], "error": None, "candidates": candidates, "accepted": status["accepted"],
        "head": events[-1]["hash"] if events else None, "last_at": events[-1]["at"] if events else None,
        "events": len(events), "chain_valid": status["chain_valid"], "base": status["base"],
        "target_ref": status["target_ref"], "target": status["target"], "next": status["next"], "open": status["open"],
        "effects": thread_effects(case_id, events, views), "refusals": thread_refusals(kit, case_id),
    }


def certificates(home: Path) -> list[dict]:
    """Certificate envelopes under ``<home>/certificates``: listed and decoded, not verified here."""
    folder = home / "certificates"
    rows = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        row = {"file": path.name, "thread": None, "candidate": None, "issued_at": None, "readable": False}
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = json.loads(base64.b64decode(envelope["payload"]))
            row.update(thread=payload.get("thread"), candidate=(payload.get("result") or {}).get("candidate"),
                       issued_at=payload.get("issued_at"), readable=True)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        rows.append(row)
    return rows


def collect(kit: Kit, *, now: float | None = None) -> dict:
    threads = [thread_model(kit, case_id) for case_id in kit.journal.thread_ids()]
    effects = [effect for thread in threads for effect in thread["effects"]]
    refusals = [row for thread in threads for row in thread["refusals"]]
    candidates = [c for thread in threads for c in thread["candidates"]]
    by_state: dict[str, int] = {}
    for thread in threads:
        by_state[thread["state"]] = by_state.get(thread["state"], 0) + 1
    return {
        "generated_at": stamp(time.time() if now is None else now),
        "journal": str(kit.journal.home),
        "home": str(kit.home),
        "threads": threads,
        "effects": effects,
        "refusals": refusals,
        "certificates": certificates(kit.home),
        "summary": {
            "threads": {"total": len(threads), **by_state},
            "candidates": {"proposed": len(candidates),
                           "accepted": sum(1 for c in candidates if c["authoritative"]),
                           "refused": sum(1 for c in candidates if c["promotion"] == "denied")},
            "refusals": len(refusals),
            "effects": {"claimed": len(effects),
                        **{verdict: sum(1 for e in effects if e["verdict"] == verdict)
                           for verdict in ("applied", "not_applied", "pending")}},
        },
    }


# Rendering: strings only, every value escaped. Objectives, notes and provider names are free text.

CSS = """
:root{color-scheme:light dark;--bg:#fbfbf9;--fg:#1d1d1b;--mute:#6b6b66;--line:#dcdcd6;--card:#ffffff;
--ok:#1f7a3a;--bad:#b3261e;--wait:#8a6d00;--accent:#2b5aa6}
@media (prefers-color-scheme:dark){:root{--bg:#141514;--fg:#e8e8e3;--mute:#9b9b94;--line:#33342f;--card:#1d1e1c;
--ok:#6fcf8a;--bad:#f28b82;--wait:#e3c15a;--accent:#8ab4f8}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 8px}h3{font-size:1rem;margin:16px 0 6px}
p.lead{margin:0 0 12px;color:var(--mute)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile b{display:block;font-size:1.5rem;font-weight:600}.tile span{color:var(--mute);font-size:.85rem}
.tile small{display:block;color:var(--mute);font-size:.8rem;margin-top:2px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
tr:last-child td{border-bottom:0}th{color:var(--mute);font-weight:600}
td.wrapcell{white-space:normal;min-width:16em;max-width:36em}
code{font:.85em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ok{color:var(--ok)}.bad{color:var(--bad)}.wait{color:var(--wait)}.mute{color:var(--mute)}
details{margin:8px 0;border:1px solid var(--line);border-radius:8px;background:var(--card)}
summary{cursor:pointer;padding:8px 12px;font-weight:600}summary span{font-weight:400;color:var(--mute)}
details .inner{padding:0 12px 12px}
details .wrap{border:0;border-top:1px solid var(--line);border-radius:0}
ul.plain{margin:4px 0;padding-left:18px}
footer{margin-top:32px;padding-top:12px;border-top:1px solid var(--line);color:var(--mute);font-size:.85rem}
footer code{word-break:break-all}
"""

TONE = {"passed": "ok", "applied": "ok", "promoted": "ok", "complete": "ok", "admitted": "ok",
        "failed": "bad", "timed_out": "bad", "error": "bad", "denied": "bad", "not_applied": "bad",
        "pending": "wait", "needs_approval": "wait", "unresolved": "wait", "stale": "wait", "in_progress": "wait",
        "none": "mute", "not_run": "mute", "provisional": "mute", "cancelled": "mute"}


def _tone(state) -> str:
    key = str(state or "")
    return f'<span class="{TONE.get(key, "")}">{_escape(key)}</span>' if key in TONE else _escape(key)


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="mute">{_escape(empty)}</p>'
    head = "".join(f"<th>{_escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(cell if cell.startswith("<td") else f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _cell(text: str, wrap: bool = False) -> str:
    return f'<td class="wrapcell">{text}</td>' if wrap else f"<td>{text}</td>"


def _objective(text, limit: int = OBJECTIVE_CHARS) -> str:
    line = " ".join(str(text or "").split())
    if len(line) <= limit:
        return _escape(line)
    return f'<span title="{_escape(line)}">{_escape(line[:limit - 1])}&hellip;</span>'


def _checks(checks: list[dict]) -> str:
    if not checks:
        return '<span class="mute">no result</span>'
    parts = []
    for check in checks:
        exit_code = "" if check.get("exit_code") in (None, 0) else f" exit {_escape(check['exit_code'])}"
        parts.append(f"{_escape(check['name'])} {_tone(check['state'])}{exit_code} ({_escape(check.get('duration_ms'))} ms)")
    return "<br>".join(parts)


def _tiles(summary: dict, certificates: list[dict]) -> str:
    threads = summary["threads"]
    candidates = summary["candidates"]
    effects = summary["effects"]
    states = ", ".join(f"{_escape(v)} {_escape(k)}" for k, v in threads.items() if k != "total") or "none"
    tiles = [
        ("Threads", threads["total"], states),
        ("Candidates proposed", candidates["proposed"],
         f"{candidates['accepted']} accepted, {candidates['refused']} refused"),
        ("Refusals", summary["refusals"], "denied decisions"),
        ("Effects claimed", effects["claimed"],
         f"{effects['applied']} applied, {effects['not_applied']} not applied, {effects['pending']} pending"),
    ]
    if certificates:
        tiles.append(("Certificates", len(certificates), f"{sum(1 for c in certificates if c['readable'])} readable"))
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><span>{_escape(label)}</span><b>{_escape(count)}</b><small>{detail}</small></div>'
        for label, count, detail in tiles) + "</div>"


def _threads_table(threads: list[dict]) -> str:
    rows = []
    for thread in threads:
        rows.append([
            f"<code>{_escape(thread['id'][:8])}</code>", _cell(_objective(thread["objective"]), wrap=True),
            _escape(thread["act_class"] or ""), _escape(thread["contract"] or ""), _tone(thread["state"]),
            _escape(len(thread["candidates"])), _escape(thread["accepted"] or ""),
            f"<code>{_escape(_short(thread['head'], 12))}</code>", _escape(when(thread["last_at"])),
        ])
    return _table(["thread", "objective", "class", "contract", "state", "candidates", "accepted", "head", "last event"],
                  rows, "No threads in this journal.")


def _thread_details(thread: dict) -> str:
    rows = []
    for candidate in thread["candidates"]:
        lineage = "+".join(candidate["parents"]) if candidate["parents"] else "base"
        promotion = _tone(candidate["promotion"])
        if candidate["promotion"] in ("denied", "not_applied", "needs_approval") and candidate["reason"]:
            promotion += f"<br><code>{_escape(candidate['reason'])}</code>"
        if not candidate["in_scope"]:
            promotion += '<br><span class="bad">out of scope</span>'
        rows.append([
            f"<code>{_escape(candidate['id'])}</code>", _escape(candidate["provider"]),
            f"{_escape(candidate['operator'])} from {_escape(lineage)}",
            f"{_escape(candidate['files'])} files, {_escape(candidate['diff_bytes'])} B",
            _cell(_checks(candidate["checks"]), wrap=True), _tone(candidate["evaluation"]), _cell(promotion, wrap=True),
            f"<code>{_escape(_short(candidate['commit']))}</code>",
        ])
    table = _table(["candidate", "provider", "lineage", "change", "checks", "evidence", "promotion", "commit"],
                   rows, "No candidates proposed.")
    facts = [f"base <code>{_escape(_short(thread['base']))}</code>",
             f"target <code>{_escape(thread['target_ref'])}</code>"
             + (f" &rarr; <code>{_escape(_short(thread['target']))}</code>" if thread["target"] else " (not created)"),
             f"{_escape(thread['events'])} events, chain "
             + ('<span class="ok">verified</span>' if thread["chain_valid"] else '<span class="bad">BROKEN</span>')]
    if thread["error"]:
        facts.append(f'<span class="bad">{_escape(thread["error"])}</span>')
    items = "".join(f"<li>{_escape(line)}</li>" for line in thread["open"]) \
        + "".join(f"<li>next: <code>{_escape(line)}</code></li>" for line in thread["next"])
    return (f"<details><summary><code>{_escape(thread['id'][:8])}</code> <span>{_objective(thread['objective'], 90)}"
            f" &middot; {_tone(thread['state'])}</span></summary><div class=\"inner\">"
            f"<p class=\"mute\">{' &middot; '.join(facts)}</p>{table}"
            + (f'<ul class="plain">{items}</ul>' if items else "") + "</div></details>")


def _effects_table(effects: list[dict]) -> str:
    rows = []
    for effect in effects:
        external = _escape(effect["external_id"] or "")
        if effect["url"]:
            external = f'<a href="{_escape(effect["url"])}">{external or _escape(effect["url"])}</a>'
        rows.append([
            f"<code>{_escape(effect['thread'][:8])}</code>", _escape(effect["action"]), _escape(effect["candidate"] or ""),
            _escape(when(effect["claimed_at"])), _tone(effect["verdict"]), _cell(_escape(effect["detail"]), wrap=True),
            _escape(when(effect["settled_at"])), _cell(_escape(effect["destination"] or ""), wrap=True),
            _cell(external, wrap=True),
        ])
    return _table(["thread", "action", "candidate", "claimed", "settlement", "detail", "settled", "destination", "external id"],
                  rows, "No effects claimed.")


def _refusals_table(refusals: list[dict]) -> str:
    rows = []
    for row in refusals:
        checks = "<br>".join(f"{_escape(c['name'])} {_tone(c['state'])} <code>{_escape(_short(c['output_digest'], 12))}</code>"
                             for c in row["checks"]) or '<span class="mute">no evaluation at decision time</span>'
        rows.append([
            f"<code>{_escape(row['thread'][:8])}</code>", _escape(when(row["at"])), _escape(row["action"]),
            _escape(row["candidate"] or _short(row["commit"])), f"<code>{_escape(row['rule'] or '?')}</code>",
            f"<code>{_escape(row['reason'])}</code>", _cell(checks, wrap=True), _escape(row["phase"]), _escape(row["actor"]),
        ])
    return _table(["thread", "when", "action", "candidate", "rule", "reason", "checks at decision", "phase", "actor"],
                  rows, "The kernel refused nothing.")


def _certificates_table(rows: list[dict]) -> str:
    return _table(["file", "thread", "candidate", "issued"], [[
        _escape(row["file"]), f"<code>{_escape(_short(row['thread'], 8))}</code>", _escape(row["candidate"] or ""),
        _escape(when(row["issued_at"]) if row["readable"] else "unreadable envelope"),
    ] for row in rows], "No certificates.")


def render_html(model: dict) -> str:
    threads = model["threads"]
    generated = when(model["generated_at"])
    heads = "".join(f"<li><code>{_escape(t['id'])}</code> &rarr; <code>{_escape(t['head'] or 'no events')}</code></li>"
                    for t in threads)
    parts = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>SyberLabs operator page</title>", f"<style>{CSS}</style></head><body><main>",
        "<h1>SyberLabs operator page</h1>",
        f'<p class="lead">Computed from the journal at <code>{_escape(model["journal"])}</code> on {_escape(generated)}. '
        "Nothing on this page was written by hand.</p>",
        "<h2>Summary</h2>", _tiles(model["summary"], model["certificates"]),
        "<h2>Threads</h2>", _threads_table(threads),
        "<h2>Candidates by thread</h2>",
        "".join(_thread_details(t) for t in threads) or '<p class="mute">No threads.</p>',
        "<h2>Effects</h2>", _effects_table(model["effects"]),
        "<h2>Refusals</h2>", _refusals_table(model["refusals"]),
    ]
    if model["certificates"]:
        parts += ["<h2>Certificates</h2>",
                  '<p class="mute">Envelopes found under the home\'s <code>certificates</code> folder; listed, not verified by this page.</p>',
                  _certificates_table(model["certificates"])]
    parts += [
        f"<footer><p>Journal <code>{_escape(model['journal'])}</code></p>",
        f"<p>Chain head per thread:</p><ul class=\"plain\">{heads or '<li>none</li>'}</ul>",
        f"<p>Generated {_escape(generated)} by <code>syberlabs.dashboard</code> from "
        f"{_escape(sum(t['events'] for t in threads))} events in {_escape(len(threads))} threads. "
        "Reading this journal recorded nothing.</p></footer></main></body></html>",
    ]
    return "\n".join(parts) + "\n"


def render(kit: Kit) -> str:
    return render_html(collect(kit))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syberlabs.dashboard", description="Write the operator page computed from the journals.")
    parser.add_argument("--home", default=".syberlabs")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out", default="dashboard.html", help="where to write the page (default dashboard.html)")
    args = parser.parse_args(argv)
    kit = Kit.local(args.home, args.repo)
    try:
        model = collect(kit)
    finally:
        kit.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render_html(model).encode("utf-8"))
    summary = model["summary"]
    print(f"wrote {out}: {summary['threads']['total']} threads, {summary['candidates']['proposed']} candidates, "
          f"{summary['refusals']} refusals, {summary['effects']['claimed']} effects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
