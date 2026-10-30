"""The spacetime ledger: an append-only event log, the artifacts it names, and a rebuildable SQLite projection.

Layout under the ledger home (``.syberlabs/factory`` by default)::

    ledger.jsonl            one hash-chained event per line: what was indexed, recorded, promoted, attested
    space/<digest>.json     Space graphs, named by their canonical digest
    runs/<run_id>.jsonl     harness journals
    blobs/<sha256>          effect payloads too large to inline
    attestations/<digest>.json
    index.sqlite3           the projection: components, runs, steps, cells, edges; rebuilt from the above

The event log is the system of record. Every event carries ``seq``, ``kind``, ``body``, ``at`` (integer
microseconds), ``previous`` and ``hash``; ``verify`` walks the chain. Bodies hold digests and ids, not
payloads, so the log stays small while artifacts stay content-addressed.

A cell is (component, step, commit): one span of one run, attributed to the component that owned its file at
that run's revision. The grid is sparse; only steps that ran become cells. ``cells_for`` slices by component
across commits, ``worldline`` slices by run, ``active_at`` slices by step.

One process writes a ledger at a time. There is no cross-process lock here, by design: the factory runs as a
CLI or a test, not a service, and a lock that works on Windows and POSIX alike is the journal's job, not this
file's.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from syberlabs.canonical import canonical, digest
from syberlabs.errors import Rejected
from syberlabs.factory import PROTOCOL, harness, space

GENESIS = "0" * 64
EVENT_FIELDS = ("seq", "kind", "body", "at", "previous")
KINDS = frozenset({"space_indexed", "run_recorded", "run_replayed", "spec_changed", "ticket_opened",
                   "attested", "drift_reported", "baseline_set", "note"})


def event_digest(event: dict) -> str:
    return digest({key: event[key] for key in EVENT_FIELDS})


def _verify_chain(events: list[dict]) -> bool:
    previous = GENESIS
    for number, event in enumerate(events, 1):
        try:
            if event["seq"] != number or event["previous"] != previous or event["hash"] != event_digest(event):
                return False
        except (KeyError, TypeError):
            return False
        previous = event["hash"]
    return True


class Ledger:
    def __init__(self, home: str | Path = ".syberlabs/factory"):
        self.home = Path(home)
        for name in ("space", "runs", "blobs", "attestations"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        self.log = self.home / "ledger.jsonl"
        self.database = self.home / "index.sqlite3"

    # -- the event log -----------------------------------------------------------------------------------------

    def events(self) -> list[dict]:
        if not self.log.exists():
            return []
        out = []
        for number, line in enumerate(self.log.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                raise Rejected("ledger_unreadable", f"{self.log}: line {number} is not JSON") from None
        return out

    def verify(self) -> bool:
        return _verify_chain(self.events())

    def append(self, kind: str, body: dict) -> dict:
        if kind not in KINDS:
            raise Rejected("ledger_kind", f"unknown event kind {kind!r}")
        events = self.events()
        previous = events[-1]["hash"] if events else GENESIS
        if not _verify_chain(events):
            raise Rejected("ledger_corrupt", "the hash chain does not verify; refusing to append to an edited ledger")
        event = {"seq": len(events) + 1, "kind": kind, "body": body, "at": int(time.time() * 1_000_000),
                 "previous": previous}
        event["hash"] = event_digest(event)
        with self.log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(event) + "\n")
        return event

    # -- artifacts -----------------------------------------------------------------------------------------------

    def record_space(self, graph: dict, declared_digest: str | None = None) -> dict:
        """Store a Space graph by digest and log it. Returns the event."""
        sha = space.graph_digest(graph)
        path = self.home / "space" / f"{sha}.json"
        if not path.exists():
            path.write_text(space.dumps(graph), encoding="utf-8")
        body = {"space_digest": sha, "revision": graph.get("revision"), "files": len(graph["files"]),
                "symbols": len(graph["symbols"]), "edges": len(graph["edges"]),
                "components": sorted(graph.get("components", {})), "declared_digest": declared_digest,
                "unmapped": len(graph.get("unmapped", []))}
        return self.append("space_indexed", body)

    def space_path(self, sha: str) -> Path:
        return self.home / "space" / f"{sha}.json"

    def load_space(self, sha: str) -> dict:
        path = self.space_path(sha)
        if not path.exists():
            raise Rejected("space_missing", f"no Space graph {sha[:12]} in {self.home}")
        return json.loads(path.read_text(encoding="utf-8"))

    def latest_space(self, revision: str | None = None) -> dict | None:
        """The most recently logged Space graph, or the most recent one for ``revision``."""
        for event in reversed(self.events()):
            if event["kind"] == "space_indexed" and (revision is None or event["body"]["revision"] == revision):
                return self.load_space(event["body"]["space_digest"])
        return None

    def run_path(self, run_id: str) -> Path:
        if not run_id or any(c not in "0123456789abcdef" for c in run_id):
            raise Rejected("run_id", f"a run id is lowercase hex, not {run_id!r}")
        return self.home / "runs" / f"{run_id}.jsonl"

    def record_run(self, run_id: str, replay_of: str | None = None) -> dict:
        """Log a journal the harness wrote into ``runs/``. Returns the event."""
        path = self.run_path(run_id)
        records = harness.read_journal(path)
        if not records or records[0].get("t") != "run" or records[0].get("run_id") != run_id:
            raise Rejected("journal_unreadable", f"{path} is not the journal of run {run_id}")
        header, tail = records[0], records[-1]
        body = {"run_id": run_id, "journal_digest": harness.journal_digest(path), "revision": header.get("revision"),
                "entrypoint": header.get("entrypoint"), "space_digest": header.get("space_digest"),
                "fixture_digest": header.get("fixture_digest"), "status": tail.get("status") if tail.get("t") == "end" else "open",
                "counts": tail.get("counts") if tail.get("t") == "end" else None, "replay_of": replay_of or header.get("replay_of")}
        return self.append("run_replayed" if body["replay_of"] else "run_recorded", body)

    def load_run(self, run_id: str) -> list[dict]:
        path = self.run_path(run_id)
        if not path.exists():
            raise Rejected("run_missing", f"no run {run_id} in {self.home}")
        return harness.read_journal(path)

    def runs(self) -> list[dict]:
        return [e["body"] for e in self.events() if e["kind"] in ("run_recorded", "run_replayed")]

    def set_baseline(self, run_id: str, label: str = "golden") -> dict:
        """Name a recorded run as the golden a later ``check`` diffs against. Labels are free text; the
        newest ``baseline_set`` for a label wins, so re-pointing is one more event, never an edit."""
        if not self.run_path(run_id).exists():
            raise Rejected("run_missing", f"no run {run_id} in {self.home}")
        if not label or any(c in label for c in " /\\"):
            raise Rejected("baseline_label", f"a label is one word, not {label!r}")
        return self.append("baseline_set", {"label": label, "run_id": run_id})

    def baseline(self, label: str = "golden") -> str | None:
        """The run id the label points at, or None when it was never set."""
        for event in reversed(self.events()):
            if event["kind"] == "baseline_set" and event["body"]["label"] == label:
                return event["body"]["run_id"]
        return None

    def record_attestation(self, document: dict) -> dict:
        """Store an attestation (a statement or its DSSE envelope) by digest and log it."""
        sha = digest(document)
        (self.home / "attestations" / f"{sha}.json").write_text(canonical(document), encoding="utf-8")
        statement = document
        if "payloadType" in document:
            import base64
            statement = json.loads(base64.standard_b64decode(document["payload"]))
        predicate = statement.get("predicate", {})
        body = {"attestation_digest": sha, "signed": "payloadType" in document,
                "subject": [s.get("name") for s in statement.get("subject", [])],
                "spec": predicate.get("spec", {}).get("spec_id"), "ticket": predicate.get("ticket"),
                "diff_empty": (predicate.get("evidence", {}).get("trace_diff") or {}).get("diff_empty"),
                "promote": (predicate.get("policy") or {}).get("promote")}
        return self.append("attested", body)

    # -- the projection -----------------------------------------------------------------------------------------

    def project(self) -> dict:
        """Rebuild ``index.sqlite3`` from the log and its artifacts. Returns row counts."""
        if self.database.exists():
            self.database.unlink()
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript("""
                CREATE TABLE components (space_digest TEXT, revision TEXT, id TEXT, label TEXT, files INTEGER);
                CREATE TABLE edges (space_digest TEXT, src TEXT, dst TEXT, count INTEGER, declared INTEGER);
                CREATE TABLE runs (run_id TEXT PRIMARY KEY, revision TEXT, entrypoint TEXT, space_digest TEXT,
                                   status TEXT, replay_of TEXT, journal_digest TEXT);
                CREATE TABLE steps (run_id TEXT, span INTEGER, parent INTEGER, seq_start INTEGER, seq_end INTEGER,
                                    ns_start INTEGER, ns_end INTEGER, component TEXT, file TEXT, line INTEGER,
                                    fn TEXT, inside_effect INTEGER);
                CREATE TABLE effects (run_id TEXT, seq INTEGER, span INTEGER, kind TEXT, name TEXT, digest TEXT);
                CREATE TABLE cells (component TEXT, run_id TEXT, span INTEGER, revision TEXT);
                CREATE INDEX cells_component ON cells (component, revision);
                CREATE INDEX steps_run ON steps (run_id, seq_start);
            """)
            counts = {"components": 0, "edges": 0, "runs": 0, "steps": 0, "effects": 0, "cells": 0}
            for event in self.events():
                body = event["body"]
                if event["kind"] == "space_indexed":
                    graph = self.load_space(body["space_digest"])
                    declared = {(e["from"], e["to"]) for e in graph.get("declared_edges", [])}
                    for component in graph.get("components", {}).values():
                        connection.execute("INSERT INTO components VALUES (?,?,?,?,?)",
                                           (body["space_digest"], graph.get("revision"), component["id"],
                                            component["label"], len(component["files"])))
                        counts["components"] += 1
                    for edge in graph.get("depends_on", []):
                        connection.execute("INSERT INTO edges VALUES (?,?,?,?,?)",
                                           (body["space_digest"], edge["from"], edge["to"], edge["count"],
                                            int((edge["from"], edge["to"]) in declared)))
                        counts["edges"] += 1
                elif event["kind"] in ("run_recorded", "run_replayed"):
                    path = self.run_path(body["run_id"])
                    if not path.exists():
                        continue
                    records = harness.read_journal(path)
                    connection.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                                       (body["run_id"], body.get("revision"), body.get("entrypoint"),
                                        body.get("space_digest"), body.get("status"), body.get("replay_of"),
                                        body.get("journal_digest")))
                    counts["runs"] += 1
                    for span in harness.spans(records):
                        connection.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                           (body["run_id"], span["span"], span["parent"], span["start"], span["end"],
                                            span["start_ns"], span["end_ns"], span["component"], span["file"],
                                            span["line"], span["fn"], int(span["inside_effect"])))
                        counts["steps"] += 1
                        if span["component"] is not None:
                            connection.execute("INSERT INTO cells VALUES (?,?,?,?)",
                                               (span["component"], body["run_id"], span["span"], body.get("revision")))
                            counts["cells"] += 1
                    for record in records:
                        if record.get("t") in ("effect", "marker"):
                            connection.execute("INSERT INTO effects VALUES (?,?,?,?,?,?)",
                                               (body["run_id"], record["seq"], record.get("span"), record["t"],
                                                record["name"], record["digest"]))
                            counts["effects"] += 1
            connection.commit()
        finally:
            connection.close()
        return counts

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.database.exists():
            self.project()
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def cells_for(self, component: str) -> list[dict]:
        """Every step that ever ran in ``component``, across runs and revisions."""
        rows = self._query("SELECT c.run_id, c.revision, c.span, s.fn, s.seq_start, s.seq_end FROM cells c "
                           "JOIN steps s ON s.run_id = c.run_id AND s.span = c.span WHERE c.component = ? "
                           "ORDER BY c.run_id, c.span", (component,))
        return [{"run_id": r[0], "revision": r[1], "span": r[2], "fn": r[3], "start": r[4], "end": r[5]} for r in rows]

    def worldline(self, run_id: str) -> list[dict]:
        rows = self._query("SELECT span, parent, component, fn, seq_start, seq_end, inside_effect FROM steps "
                           "WHERE run_id = ? ORDER BY seq_start", (run_id,))
        return [{"span": r[0], "parent": r[1], "component": r[2], "fn": r[3], "start": r[4], "end": r[5],
                 "inside_effect": bool(r[6])} for r in rows]

    def active_at(self, run_id: str, seq: int) -> list[dict]:
        rows = self._query("SELECT span, component, fn, seq_start, seq_end FROM steps WHERE run_id = ? AND "
                           "seq_start <= ? AND (seq_end IS NULL OR seq_end > ?) ORDER BY seq_start", (run_id, seq, seq))
        return [{"span": r[0], "component": r[1], "fn": r[2], "start": r[3], "end": r[4]} for r in rows]

    def components_at(self, revision: str) -> list[str]:
        rows = self._query("SELECT DISTINCT id FROM components WHERE revision = ? ORDER BY id", (revision,))
        return [r[0] for r in rows]

    def describe(self) -> dict:
        events = self.events()
        return {"protocol": PROTOCOL, "home": str(self.home), "events": len(events), "verified": self.verify(),
                "spaces": sum(1 for e in events if e["kind"] == "space_indexed"),
                "runs": sum(1 for e in events if e["kind"] in ("run_recorded", "run_replayed")),
                "attestations": sum(1 for e in events if e["kind"] == "attested"),
                "head": events[-1]["hash"] if events else GENESIS}
