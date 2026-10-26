"""Admission certificates for the console, read from a Build Thread home and verified by serve under one key.

``syberlabs certify`` writes a certificate for a Build Thread's accepted candidate as a file under
``<home>/certificates`` (docs/CERTIFICATE.md). A SyberWork case is not a Build Thread and has none. ``syberwork
serve`` lists that folder when ``SYBERWORK_CERTIFICATE_HOME`` and ``SYBERWORK_CERTIFICATE_PUBLIC`` name a home and the
key to trust, and runs ``verify_certificate`` on each file: the signature layer always, and the journal layer when the
thread's journal is under the same home. The repository layer needs Git and is not run here. The ``.pub`` file beside
each certificate is never read: whoever can write the folder can write that file too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
from collections import OrderedDict
from pathlib import Path

from syberlabs.certificate import (MAX_CERTIFICATE_BYTES, PAYLOAD_TYPE, STATEMENT_PAYLOAD_TYPE, Report,
                                   verify_certificate)
from syberlabs.errors import Rejected
from syberlabs.journal import CASE_ID

# The most files one read verifies, in name order. Each costs a pure-Python Ed25519 check the first time it is seen.
LIMIT = 50
# Verdicts kept per process. A verdict depends only on the bytes it was computed from, so it is never stale.
CACHE = 256
# The largest journal or registry file serve hashes and lets the verifier read. A larger one, or one that is not a
# regular file (a FIFO, a device), is refused rather than read, so one file under the home cannot hang the route.
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
NOTE = ("Verified by serve under the configured key: signature and shape, then the thread's journal under the same home "
        "when it is there. The repository layer is not run; syberlabs verify FILE --public KEY --repo PATH runs it. "
        "The .pub file beside each certificate is not read.")


def not_configured() -> dict:
    return {"configured": False,
            "note": "serve reads no certificates; set SYBERWORK_CERTIFICATE_HOME and SYBERWORK_CERTIFICATE_PUBLIC"}


def _state(path: Path) -> tuple:
    """``("absent",)``, ``("file", sha256)`` or ``("refused", reason)`` for a journal file the verifier would read.

    A missing file and an unreadable one are different states: the verifier skips the first and fails on the second.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return ("refused", "io_error")
    if not stat.S_ISREG(info.st_mode):
        return ("refused", "not_a_regular_file")
    if info.st_size > MAX_JOURNAL_BYTES:
        return ("refused", "too_large")
    digest, size = hashlib.sha256(), 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                size += len(chunk)
                if size > MAX_JOURNAL_BYTES:
                    return ("refused", "too_large")
                digest.update(chunk)
    except OSError:
        return ("refused", "io_error")
    return ("file", digest.hexdigest())


def _claimed_thread(data: bytes) -> str | None:
    """The thread an unverified file names, only to find which journal its verdict depends on; never answered."""
    try:
        envelope = json.loads(data)
        body = json.loads(base64.standard_b64decode(envelope["payload"]))
        if envelope.get("payloadType") == STATEMENT_PAYLOAD_TYPE:
            body = body["predicate"]
        thread = body["thread"]
    except Exception:  # RecursionError included: a deeply nested file is one unreadable row, not a failed listing
        return None
    return thread if isinstance(thread, str) and CASE_ID.match(thread) else None


def _statement(payload: dict, payload_type: str, keyid: str) -> dict:
    """What a signed certificate states, for the console. Only a payload whose signature verified gets here."""
    result, admission = payload["result"], payload["admission"]
    return {
        "form": "statement" if payload_type == STATEMENT_PAYLOAD_TYPE else "bare", "keyid": keyid,
        "thread": payload["thread"], "objective": payload["objective"], "act_class": payload["act_class"],
        "contract": payload["contract"], "policy_version": payload["policy_version"],
        "candidate": result["candidate"], "base": {"commit": payload["base"]["commit"], "tree": payload["base"]["tree"]},
        "result": {"commit": result["commit"], "tree": result["tree"], "files": result["files"]},
        "target_ref": admission["target_ref"], "actor": admission["actor"], "approvers": admission["approvers"],
        "decision": admission["decision"], "effect_kind": admission["effect_kind"],
        "checks": [{"name": c["name"], "required": c["required"], "state": c["state"]} for c in payload["evaluation"]["checks"]],
        "chain": payload["chain"], "issued_at": payload["issued_at"], "signing": payload["issuer"]["signing"],
    }


class CertificateShelf:
    """The certificates under ``<home>/certificates``, each with the verdict ``verify_certificate`` gives it now."""

    def __init__(self, home: str | Path, public: bytes):
        home = Path(home)
        if not home.is_dir():
            raise Rejected("invalid_runtime", "the certificate home must be a directory (a Build Thread home such as .syberlabs)")
        if not isinstance(public, bytes) or len(public) != 32:
            raise Rejected("invalid_runtime", "the certificate public key must be 32 bytes")
        self.home, self.public = home, public
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._lock = threading.Lock()

    def listing(self) -> dict:
        folder = self.home / "certificates"
        files = sorted((p for p in folder.glob("*.json")), key=lambda p: p.name) if folder.is_dir() else []
        # One read at a time: two console tabs opening Studio at once verify each file once, not twice.
        with self._lock:
            # The registry and a thread's journal are hashed once per listing, however many rows name them.
            seen: dict[Path, tuple] = {}
            rows = [self._row(path, seen) for path in files[:LIMIT]]
        return {"configured": True, "public_key": self.public.hex(), "total": len(files), "shown": len(rows),
                "checked": ["signature", "journal"], "not_checked": ["repository"], "certificates": rows, "note": NOTE}

    def _inputs(self, journal: Path | None, seen: dict | None = None) -> tuple:
        """The state of the registry and the thread's journal, which with the file and the key decide a verdict."""
        def state(path):
            if seen is None:
                return _state(path)
            if path not in seen:
                seen[path] = _state(path)
            return seen[path]
        return (state(self.home / "journal" / "registry.jsonl"), state(journal) if journal else ("absent",))

    def _row(self, path: Path, seen: dict) -> dict:
        refused = {"file": path.name, "verdict": "fail", "signature": None, "journal": None, "journal_present": None,
                   "repository": None, "statement": None}
        if path.is_symlink() or not path.is_file():
            return {**refused, "reason": "not_a_regular_file"}
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_CERTIFICATE_BYTES + 1)
        except OSError:
            return {**refused, "reason": "certificate_unreadable"}
        thread = _claimed_thread(data)
        journal = self.home / "journal" / "threads" / f"{thread}.jsonl" if thread else None
        inputs = self._inputs(journal, seen)
        key = (hashlib.sha256(data).hexdigest(), *inputs)
        if key in self._cache:
            self._cache.move_to_end(key)
            return {"file": path.name, **self._cache[key]}
        verdict = self._verdict(data, inputs)
        # Kept only when nothing was refused and the inputs read the same after verifying: an append in between would
        # otherwise file the new content's verdict under the old content's key.
        if all(part[0] != "refused" for part in inputs) and self._inputs(journal) == inputs:
            self._cache[key] = verdict
            while len(self._cache) > CACHE:
                self._cache.popitem(last=False)
        return {"file": path.name, **verdict}

    def _verdict(self, data: bytes, inputs: tuple) -> dict:
        registry, journal = inputs
        # The journal layer reads the registry only when the thread's journal is there.
        blocked = None if journal[0] == "absent" else next((part[1] for part in (journal, registry) if part[0] == "refused"), None)
        try:
            report: Report = verify_certificate(data, self.public, home=None if blocked else self.home)
        except Exception:  # an envelope verify_certificate cannot read is refused, never drawn
            return {"verdict": "fail", "reason": "unverifiable", "signature": None, "journal": None,
                    "journal_present": None, "repository": None, "statement": None}
        statement = None
        if report.signature == "ok" and report.payload is not None:
            payload_type = json.loads(data).get("payloadType", PAYLOAD_TYPE)
            statement = _statement(report.payload, payload_type, report.keyid)
        if blocked and statement is not None:
            # Signed, but the journal could not be read safely: never a pass, and named as unreadable, not as changed.
            reason = f"journal_unreadable:{blocked}"
            return {"verdict": "fail", "reason": reason, "signature": "ok", "journal": reason, "journal_present": True,
                    "repository": None, "statement": statement}
        return {"verdict": "pass" if report.passed else "fail", "reason": report.reason, "signature": report.signature,
                "journal": report.journal, "journal_present": None if statement is None else journal[0] != "absent",
                "repository": None, "statement": statement}
