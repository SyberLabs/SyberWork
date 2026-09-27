"""Append-only Merkle log for signed chain heads.

The log is a different file from the case database. It stores DSSE envelopes,
not event bodies, and it signs each new size and root with the caller's key.
An operator who holds that key can rebuild the file. This is not a public
witness and it is not Sigstore Rekor.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Mapping

from syberlabs.canonical import canonical
from syberlabs.dsse import CHECKPOINT_TYPE, sign_dsse, verified_payload, verify_chain_head
from syberlabs.ed25519 import public_from_seed
from syberlabs.errors import Rejected


def leaf_hash(entry: bytes) -> bytes:
    """RFC 6962 leaf: SHA-256 of ``0x00 || entry``."""
    return hashlib.sha256(b"\x00" + entry).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def merkle_root(entries: list[bytes]) -> bytes:
    """RFC 6962 root. The empty tree is SHA-256 of the empty byte string."""
    if not entries:
        return hashlib.sha256(b"").digest()
    leaves = [leaf_hash(item) for item in entries]
    return _subtree(leaves, 0, len(leaves))


def _subtree(leaves: list[bytes], start: int, end: int) -> bytes:
    count = end - start
    if count == 1:
        return leaves[start]
    split = 1 << ((count - 1).bit_length() - 1)
    return _node(_subtree(leaves, start, start + split), _subtree(leaves, start + split, end))


def inclusion_proof(leaves: list[bytes], index: int) -> list[bytes]:
    """Sibling hashes from the leaf toward the root. ``leaves`` are leaf hashes."""

    def walk(start: int, end: int) -> list[bytes]:
        count = end - start
        if count == 1:
            return []
        split = 1 << ((count - 1).bit_length() - 1)
        if index < start + split:
            return walk(start, start + split) + [_subtree(leaves, start + split, end)]
        return walk(start + split, end) + [_subtree(leaves, start, start + split)]

    return walk(0, len(leaves))


def verify_inclusion(index: int, size: int, leaf: bytes, proof: list[bytes], root: bytes) -> bool:
    """RFC 6962 inclusion check. ``leaf`` is the leaf hash, not the entry bytes."""
    if type(index) is not int or type(size) is not int or index < 0 or index >= size:
        return False
    fn = index
    sn = size - 1
    result = leaf
    for sibling in proof:
        if sn == 0 or not isinstance(sibling, bytes) or len(sibling) != 32:
            return False
        if fn % 2 == 1 or fn == sn:
            result = hashlib.sha256(b"\x01" + sibling + result).digest()
            if fn % 2 == 0:
                while fn % 2 == 0 and sn > 0:
                    fn >>= 1
                    sn >>= 1
        else:
            result = hashlib.sha256(b"\x01" + result + sibling).digest()
        fn >>= 1
        sn >>= 1
    return sn == 0 and result == root


class TransparencyLog:
    """Append-only log. There is no update or delete method."""

    def __init__(self, path: str | Path, seed: bytes):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seed = seed
        self.public = public_from_seed(seed)
        self._db = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            existing = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cases'"
            ).fetchone()
            if existing:
                raise Rejected("log_path", "transparency log must be a different file from the case database")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS leaves (
                    idx INTEGER PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    size INTEGER PRIMARY KEY, envelope TEXT NOT NULL);
            """)
        except Exception:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def __del__(self):
        try:
            self._db.close()
        except Exception:
            pass

    def append(self, envelope: Mapping) -> int:
        """Append a chain-head envelope that verifies under this log's key. Returns the index."""
        if verified_payload(envelope, self.public, "application/vnd.syberlabs.chain-head+json") is None:
            raise Rejected("invalid_witness", "chain-head signature does not verify")
        body = canonical(dict(envelope))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                count = self._db.execute("SELECT COUNT(*) AS n FROM leaves").fetchone()["n"]
                self._db.execute("INSERT INTO leaves (idx, body) VALUES (?, ?)", (count, body))
                entries = [row["body"].encode() for row in self._db.execute("SELECT body FROM leaves ORDER BY idx")]
                payload = canonical({"root": merkle_root(entries).hex(), "size": count + 1}).encode("ascii")
                checkpoint = sign_dsse(CHECKPOINT_TYPE, payload, self._seed)
                self._db.execute(
                    "INSERT INTO checkpoints (size, envelope) VALUES (?, ?)",
                    (count + 1, canonical(checkpoint)),
                )
                self._db.commit()
            except BaseException:
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass
                raise
            return count

    def verify_witnessed(self, envelope: Mapping, events: list) -> bool:
        """True when the envelope matches the events, is in the log, and the signed checkpoint includes it."""
        if not verify_chain_head(envelope, events, self.public):
            return False
        body = canonical(dict(envelope))
        with self._lock:
            rows = list(self._db.execute("SELECT idx, body FROM leaves ORDER BY idx"))
            match = next((row for row in rows if row["body"] == body), None)
            checkpoint = self._db.execute(
                "SELECT envelope FROM checkpoints ORDER BY size DESC LIMIT 1"
            ).fetchone()
            if match is None or checkpoint is None:
                return False
            try:
                signed = json.loads(checkpoint["envelope"])
            except json.JSONDecodeError:
                return False
            payload = verified_payload(signed, self.public, CHECKPOINT_TYPE)
            if payload is None:
                return False
            try:
                document = json.loads(payload)
            except json.JSONDecodeError:
                return False
            if not isinstance(document, dict) or type(document.get("size")) is not int:
                return False
            if document["size"] != len(rows) or not isinstance(document.get("root"), str):
                return False
            try:
                root = bytes.fromhex(document["root"])
            except ValueError:
                return False
            entries = [row["body"].encode() for row in rows]
            if merkle_root(entries) != root or len(root) != 32:
                return False
            leaves = [leaf_hash(entry) for entry in entries]
            proof = inclusion_proof(leaves, match["idx"])
            return verify_inclusion(match["idx"], document["size"], leaves[match["idx"]], proof, root)
