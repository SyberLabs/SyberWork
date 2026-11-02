"""The spacetime ledger: an append-only event log, the artifacts it names, and a rebuildable SQLite projection.

Layout under the ledger home (``.syberlabs/factory`` by default)::

    ledger.jsonl            one hash-chained event per line: what was indexed, recorded, promoted, attested
    space/<digest>.json     Space graphs, named by their canonical digest
    runs/<run_id>.jsonl     harness journals
    blobs/<sha256>          effect payloads too large to inline
    attestations/<digest>.json
    approvals/<digest>.json the owner's signed approvals a promotion rests on, and signed owner-key rotations
    index.sqlite3           the projection: components, runs, steps, cells, edges; rebuilt from the above
    .journal/               the kernel journal's lock file for this ledger (``syberlabs.journal``)

The event log is the system of record. Every event carries ``seq``, ``kind``, ``body``, ``at`` (integer
microseconds), ``previous`` and ``hash``; ``verify`` walks the chain and then every artifact an event names,
comparing the file with the digest the event recorded. Bodies hold digests and ids, not payloads, so the log
stays small while artifacts stay content-addressed. An artifact is never read by name alone: ``load_run``
and ``load_space`` refuse a file whose digest is not the one on the ledger, so an edited golden journal cannot
become a gate's baseline.

The owner's public key is pinned here (``owner_key_set``): the first such event is the ledger's trust root and
every later one is a rotation signed by the key it replaces (``set_owner_key``), so the goal gate verifies
approvals under a key the ledger names rather than one the caller passes.

Appends go through the kernel journal's writer (``syberlabs.journal.Journal``): a cross-process lock (``flock``
or ``msvcrt.locking``), flush and fsync per record, and truncation of a torn final line before the next read.
Two factory commands on one home therefore serialize instead of forking the chain.

A cell is (component, step, commit): one span of one run, attributed to the component that owned its file at
that run's revision. The grid is sparse; only steps that ran become cells. ``cells_for`` slices by component
across commits, ``worldline`` slices by run, ``active_at`` slices by step. The projection is rebuilt when the
log it was built from has moved on (its ``meta`` row records the chain head it reflects); the ``features``
table ``features.build`` keeps in the same file survives a rebuild.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from syberlabs import journal as kernel_journal
from syberlabs.canonical import canonical, digest
from syberlabs.errors import Rejected
from syberlabs.factory import PROTOCOL, attest, harness, space
from syberlabs.factory import spec as specmod

GENESIS = "0" * 64
EVENT_FIELDS = ("seq", "kind", "body", "at", "previous")
RUN_KINDS = frozenset({"run_recorded", "run_replayed"})
KINDS = frozenset({"space_indexed", "run_recorded", "run_replayed", "spec_changed", "ticket_opened",
                   "attested", "drift_reported", "baseline_set", "approval_recorded", "owner_key_set", "note"})
PROJECTION_TABLES = ("components", "edges", "runs", "steps", "effects", "cells", "meta")


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


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Ledger:
    def __init__(self, home: str | Path = ".syberlabs/factory"):
        self.home = Path(home)
        for name in ("space", "runs", "blobs", "attestations", "approvals"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        self.log = self.home / "ledger.jsonl"
        self.database = self.home / "index.sqlite3"

    # -- the event log -----------------------------------------------------------------------------------------

    @contextmanager
    def _locked(self):
        """The kernel journal's lock over this ledger, held for one read or one read-then-append. A fresh
        ``Journal`` per operation: every command reads the ledger from its first byte anyway, and no lock
        handle outlives the call (a held handle would pin the directory on Windows)."""
        writer = kernel_journal.Journal(self.home / ".journal")
        try:
            with writer.locked():
                yield writer
        finally:
            writer.close()

    def _read(self, writer: kernel_journal.Journal) -> list[dict]:
        try:
            return writer.read_new(self.log)
        except Rejected as exc:
            raise Rejected("ledger_unreadable", f"{self.log}: {exc.detail}") from None

    def events(self) -> list[dict]:
        with self._locked() as writer:
            return self._read(writer)

    def head(self, events: list[dict] | None = None) -> str:
        events = self.events() if events is None else events
        return events[-1]["hash"] if events else GENESIS

    def problems(self, events: list[dict] | None = None) -> list[str]:
        """Why the ledger does not verify: the chain first, then every artifact an event names whose file is
        missing or whose digest is not the recorded one (Space graphs, journals and the blobs they reference,
        attestations, approvals). Empty when everything holds."""
        events = self.events() if events is None else events
        if not _verify_chain(events):
            return ["the event chain does not verify"]
        found = []
        for event in events:
            body, kind = event["body"], event["kind"]
            try:
                if kind == "space_indexed":
                    path = self.space_path(body["space_digest"])
                    if not path.exists():
                        found.append(f"space/{body['space_digest'][:12]} is missing")
                    elif space.graph_digest(json.loads(path.read_text(encoding="utf-8"))) != body["space_digest"]:
                        found.append(f"space/{body['space_digest'][:12]} does not match its digest")
                elif kind in RUN_KINDS:
                    path = self.run_path(body["run_id"])
                    if not path.exists():
                        found.append(f"runs/{body['run_id']} is missing")
                    elif _file_digest(path) != body["journal_digest"]:
                        found.append(f"runs/{body['run_id']} does not match the digest recorded at seq {event['seq']}")
                    else:
                        for record in harness.read_journal(path):
                            blob = record.get("blob")
                            if blob and (not (self.home / "blobs" / blob).exists() or _file_digest(self.home / "blobs" / blob) != blob):
                                found.append(f"blobs/{blob[:12]} of run {body['run_id']} is missing or does not match")
                elif kind in ("attested", "approval_recorded") or (kind == "owner_key_set" and body.get("rotation_digest")):
                    folder, key = {"attested": ("attestations", "attestation_digest"), "approval_recorded": ("approvals", "approval_digest"),
                                   "owner_key_set": ("approvals", "rotation_digest")}[kind]
                    path = self.home / folder / f"{body[key]}.json"
                    if not path.exists():
                        found.append(f"{folder}/{body[key][:12]} is missing")
                    elif digest(json.loads(path.read_text(encoding="utf-8"))) != body[key]:
                        found.append(f"{folder}/{body[key][:12]} does not match its digest")
            except (KeyError, TypeError, ValueError, OSError, Rejected) as exc:
                found.append(f"seq {event['seq']} ({kind}): {type(exc).__name__}")
        return found

    def verify(self, events: list[dict] | None = None) -> bool:
        """True when the chain verifies and every artifact the events name matches its recorded digest."""
        return not self.problems(events)

    def append(self, kind: str, body: dict) -> dict:
        if kind not in KINDS:
            raise Rejected("ledger_kind", f"unknown event kind {kind!r}")
        with self._locked() as writer:
            events = self._read(writer)
            if not _verify_chain(events):
                raise Rejected("ledger_corrupt", "the hash chain does not verify; refusing to append to an edited ledger")
            event = {"seq": len(events) + 1, "kind": kind, "body": body, "at": int(time.time() * 1_000_000),
                     "previous": events[-1]["hash"] if events else GENESIS}
            event["hash"] = event_digest(event)
            # The kernel journal's append: flushed, fsynced, and refused if another writer slipped in between.
            writer._write(self.log, event)
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
        if not sha or any(c not in "0123456789abcdef" for c in sha):
            raise Rejected("space_digest", f"a Space digest is lowercase hex, not {sha!r}")
        return self.home / "space" / f"{sha}.json"

    def load_space(self, sha: str) -> dict:
        """The Space graph named ``sha``, refused unless its content digests to that name."""
        path = self.space_path(sha)
        if not path.exists():
            raise Rejected("space_missing", f"no Space graph {sha[:12]} in {self.home}")
        graph = json.loads(path.read_text(encoding="utf-8"))
        if space.graph_digest(graph) != sha:
            raise Rejected("artifact_tampered", f"space/{sha[:12]}.json does not digest to its name")
        return graph

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
        """Log a journal the harness delivered into ``runs/``. Returns the event. The digest recorded here is
        what every later ``load_run`` checks the file against."""
        path = self.run_path(run_id)
        if not path.exists():
            raise Rejected("run_missing", f"no run {run_id} in {self.home}")
        records = harness.read_journal(path)
        if not records or records[0].get("t") != "run" or records[0].get("run_id") != run_id:
            raise Rejected("journal_unreadable", f"{path} is not the journal of run {run_id}")
        header, tail = records[0], records[-1]
        body = {"run_id": run_id, "journal_digest": _file_digest(path), "revision": header.get("revision"),
                "entrypoint": header.get("entrypoint"), "space_digest": header.get("space_digest"),
                "fixture_digest": header.get("fixture_digest"), "status": tail.get("status") if tail.get("t") == "end" else "open",
                "counts": tail.get("counts") if tail.get("t") == "end" else None, "replay_of": replay_of or header.get("replay_of"),
                "sealed": bool(header.get("sealed"))}
        return self.append("run_replayed" if body["replay_of"] else "run_recorded", body)

    def run_record(self, run_id: str, events: list[dict] | None = None) -> dict | None:
        """The ledger's record of ``run_id`` (the newest ``run_recorded`` or ``run_replayed`` body), or None."""
        for event in reversed(self.events() if events is None else events):
            if event["kind"] in RUN_KINDS and event["body"].get("run_id") == run_id:
                return event["body"]
        return None

    def load_run(self, run_id: str) -> list[dict]:
        """A recorded run's journal, refused when the ledger never logged it or the file no longer matches the
        digest the ledger recorded."""
        path = self.run_path(run_id)
        if not path.exists():
            raise Rejected("run_missing", f"no run {run_id} in {self.home}")
        recorded = self.run_record(run_id)
        if recorded is None:
            raise Rejected("run_unrecorded", f"run {run_id} is in runs/ but not on the ledger; refusing to read an unlogged journal")
        if _file_digest(path) != recorded["journal_digest"]:
            raise Rejected("artifact_tampered", f"runs/{run_id}.jsonl does not match the digest the ledger recorded")
        return harness.read_journal(path)

    def runs(self) -> list[dict]:
        return [e["body"] for e in self.events() if e["kind"] in RUN_KINDS]

    def set_baseline(self, run_id: str, label: str = "golden") -> dict:
        """Name a recorded run as the golden a later ``check`` diffs against. Labels are free text; the
        newest ``baseline_set`` for a label wins, so re-pointing is one more event, never an edit. The run
        must be on the ledger and intact (``load_run``)."""
        self.load_run(run_id)
        if not label or any(c in label for c in " /\\"):
            raise Rejected("baseline_label", f"a label is one word, not {label!r}")
        return self.append("baseline_set", {"label": label, "run_id": run_id})

    def baseline(self, label: str = "golden") -> str | None:
        """The run id the label points at, or None when it was never set."""
        for event in reversed(self.events()):
            if event["kind"] == "baseline_set" and event["body"]["label"] == label:
                return event["body"]["run_id"]
        return None

    def record_attestation(self, document: dict, public: bytes | None = None) -> dict:
        """Store an attestation (a statement or its DSSE envelope) by digest and log it. ``signed`` is true
        only when the envelope's signature verifies under ``public``; an envelope recorded without the key,
        or one that does not verify, is logged ``signed: false`` with ``envelope: true``."""
        sha = digest(document)
        (self.home / "attestations" / f"{sha}.json").write_text(canonical(document), encoding="utf-8")
        envelope = "payloadType" in document
        statement = attest.verify(document, public) if envelope and public is not None else None
        signed = statement is not None
        if statement is None:
            try:
                statement = attest.unwrap(document)
            except (ValueError, TypeError, KeyError):
                raise Rejected("attestation_unreadable", "the envelope's payload is not a statement") from None
        predicate = statement.get("predicate", {})
        body = {"attestation_digest": sha, "signed": signed, "envelope": envelope,
                "keyid": public.hex() if signed else None,
                "subject": [s.get("name") for s in statement.get("subject", [])],
                "spec": predicate.get("spec", {}).get("spec_id"), "ticket": predicate.get("ticket"),
                "diff_empty": (predicate.get("evidence", {}).get("trace_diff") or {}).get("diff_empty"),
                "promote": (predicate.get("policy") or {}).get("promote")}
        return self.append("attested", body)

    def record_approval(self, envelope: dict, approval: dict) -> dict:
        """Store a verified owner approval (its DSSE envelope) by digest and log it; ``approval`` is the
        payload :func:`spec.verify_approval` returned. The event's hash is what the ``spec_changed`` cites."""
        sha = digest(envelope)
        (self.home / "approvals" / f"{sha}.json").write_text(canonical(envelope), encoding="utf-8")
        body = {"approval_digest": sha, "keyid": approval["keyid"], "approver": approval["approver"],
                "operation": approval["operation"], "spec_id": approval["spec_id"], "version": approval["version"],
                "spec_digest": approval["spec_digest"], "verdict_ids": list(approval.get("verdict_ids") or [])}
        return self.append("approval_recorded", body)

    # -- the owner key -------------------------------------------------------------------------------------------

    def owner_key(self, events: list[dict] | None = None) -> str | None:
        """The owner's public key (hex) this ledger pins, or None before the first ``owner_key_set``."""
        return specmod.pinned_owner_key(self.events() if events is None else events)

    def set_owner_key(self, public: bytes, *, seed: bytes | None = None) -> dict:
        """Pin ``public`` as the owner key. On a ledger that pins none this is the bootstrap: one event, no
        signature, and from then on the goal gate trusts this key and no other. On a ledger that pins a key
        it is a rotation and needs ``seed``, the seed of the key pinned now: the handover is signed by that
        key (``spec.sign_rotation``), verified, stored under ``approvals/`` and cited by the event. Refused
        when the seed is not the pinned key's (``owner_key_rotation``) or the key is already pinned
        (``owner_key_pinned``). Returns the event."""
        from syberlabs.ed25519 import public_from_seed

        keyid = public.hex()
        current = self.owner_key()
        if current is None:
            return self.append("owner_key_set", {"keyid": keyid, "previous": None, "rotation_digest": None})
        if current == keyid:
            raise Rejected("owner_key_pinned", f"the ledger already pins {keyid[:10]}")
        if seed is None:
            raise Rejected("owner_key_rotation", f"the ledger pins {current[:10]}; rotating to {keyid[:10]} needs that key's seed (--seed-env)")
        if public_from_seed(seed).hex() != current:
            raise Rejected("owner_key_rotation", f"the seed given is not the pinned owner key's ({current[:10]}); only the pinned key rotates")
        envelope = specmod.sign_rotation(specmod.rotation_payload(bytes.fromhex(current), public), seed)
        specmod.verify_rotation(envelope, bytes.fromhex(current), public)
        sha = digest(envelope)
        (self.home / "approvals" / f"{sha}.json").write_text(canonical(envelope), encoding="utf-8")
        return self.append("owner_key_set", {"keyid": keyid, "previous": current, "rotation_digest": sha})

    # -- the projection -----------------------------------------------------------------------------------------

    def project(self, events: list[dict] | None = None) -> dict:
        """Rebuild the projection tables in ``index.sqlite3`` from the log and its artifacts. Returns row
        counts. Tables other modules keep in the same file (``features``) are left alone."""
        events = self.events() if events is None else events
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript("".join(f"DROP TABLE IF EXISTS {table};" for table in PROJECTION_TABLES) + """
                CREATE TABLE meta (head TEXT, count INTEGER);
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
            for event in events:
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
                elif event["kind"] in RUN_KINDS:
                    path = self.run_path(body["run_id"])
                    if not path.exists() or _file_digest(path) != body["journal_digest"]:
                        continue  # a missing or edited journal projects nothing; ``verify`` names it
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
                        if record.get("t") in ("effect", "marker", "io"):
                            connection.execute("INSERT INTO effects VALUES (?,?,?,?,?,?)",
                                               (body["run_id"], record["seq"], record.get("span"), record["t"],
                                                record["name"], record["digest"]))
                            counts["effects"] += 1
            connection.execute("INSERT INTO meta VALUES (?, ?)", (self.head(events), len(events)))
            connection.commit()
        finally:
            connection.close()
        return counts

    def _stale(self, events: list[dict]) -> bool:
        """True when the projection is missing or was built from a shorter or different log."""
        if not self.database.exists():
            return True
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute("SELECT head, count FROM meta").fetchone()
        except sqlite3.Error:
            return True
        finally:
            connection.close()
        return row is None or row[0] != self.head(events) or row[1] != len(events)

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        events = self.events()
        if self._stale(events):
            self.project(events)
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
        problems = self.problems(events)
        return {"protocol": PROTOCOL, "home": str(self.home), "events": len(events), "verified": not problems,
                "problems": len(problems),
                "spaces": sum(1 for e in events if e["kind"] == "space_indexed"),
                "runs": sum(1 for e in events if e["kind"] in RUN_KINDS),
                "attestations": sum(1 for e in events if e["kind"] == "attested"),
                "approvals": sum(1 for e in events if e["kind"] == "approval_recorded"),
                "owner_key": self.owner_key(events), "head": self.head(events)}
