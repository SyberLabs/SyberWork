"""Share populations between hosts through a Git remote (EvoGit's multi-host mode).

Each host writes only its own namespace on the shared remote::

    refs/syberlabs/exchange/<topic>-<base12>/<host>/manifest   a commit holding manifest.json
    refs/syberlabs/exchange/<topic>-<base12>/<host>/<cN>       that host's candidate commits

and reads everyone else's into ``refs/syberlabs/remote/``. A manifest lists
candidates with their commit, operator, local parents, and the host's own
score. Everything read from another host is untrusted input: the receiving
thread checks that a migrant descends from its own base, recomputes scope,
records the origin as provenance, and evaluates the migrant itself. A
foreign score only decides which migrants are worth trying.

Integrity of the namespaces depends on the remote's access control; a
migrant cannot gain authority either way, because it is judged locally and
promoted only by a person.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from syberlabs.errors import Rejected
from syberlabs.gitspace import Repo

PROTOCOL = "syberlabs.exchange/v0alpha1"
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OBJECT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
MAX_MANIFEST = 1_048_576
MAX_ENTRIES = 256


@dataclass(frozen=True)
class Offer:
    host: str
    candidate: str
    commit: str
    score: tuple[int, int] | None


class Exchange:
    def __init__(self, repo: Repo, remote: str, host: str, topic: str, *, timeout: int = 120):
        if not _REMOTE.match(remote):
            raise Rejected("invalid_exchange", "remote must name a configured git remote")
        if not _NAME.match(host):
            raise Rejected("invalid_exchange", "host must be 1 to 32 lowercase letters, digits, or dashes")
        if not _TOPIC.match(topic):
            raise Rejected("invalid_exchange", "topic must be lowercase letters, digits, or dashes")
        self.repo, self.remote, self.host, self.topic, self.timeout = repo, remote, host, topic, timeout

    def _key(self, base: str) -> str:
        return f"{self.topic}-{base[:12]}"

    def publish(self, base: str, entries: list[dict]) -> None:
        """Push this host's candidates and a manifest into its own namespace on the remote."""
        entries = entries[:MAX_ENTRIES]
        manifest = {"protocol": PROTOCOL, "host": self.host, "topic": self.topic, "base": base, "candidates": entries}
        blob = self.repo.git("hash-object", "-w", "--stdin", input=json.dumps(manifest, sort_keys=True).encode()).decode().strip()
        tree = self.repo.git("mktree", input=f"100644 blob {blob}\tmanifest.json\n".encode()).decode().strip()
        commit = self.repo.git("commit-tree", tree, "-m", f"syberlabs exchange manifest from {self.host}",
                               env={"GIT_AUTHOR_NAME": "SyberLabs exchange", "GIT_AUTHOR_EMAIL": "exchange@syberlabs.invalid",
                                    "GIT_COMMITTER_NAME": "SyberLabs exchange",
                                    "GIT_COMMITTER_EMAIL": "exchange@syberlabs.invalid"}).decode().strip()
        prefix = f"refs/syberlabs/exchange/{self._key(base)}/{self.host}"
        refspecs = [f"+{commit}:{prefix}/manifest"] + [f"+{e['commit']}:{prefix}/{e['candidate']}" for e in entries]
        self.repo.git("push", "--quiet", self.remote, *refspecs, timeout=self.timeout)

    def offers(self, base: str) -> list[Offer]:
        """Fetch other hosts' namespaces and return their well-formed entries for this base."""
        key = self._key(base)
        self.repo.git("fetch", "--quiet", "--no-tags", self.remote,
                      f"+refs/syberlabs/exchange/{key}/*:refs/syberlabs/remote/{key}/*", timeout=self.timeout)
        found = []
        for ref, commit in self.repo.refs(f"refs/syberlabs/remote/{key}/").items():
            parts = ref.split("/")
            if len(parts) != 6 or parts[5] != "manifest" or parts[4] == self.host or not _NAME.match(parts[4]):
                continue
            manifest = self._manifest(commit)
            if manifest is None or manifest.get("base") != base or manifest.get("host") != parts[4]:
                continue
            for entry in manifest.get("candidates", [])[:MAX_ENTRIES]:
                offer = self._offer(parts[4], entry, key)
                if offer is not None:
                    found.append(offer)
        return found

    def _manifest(self, commit: str) -> dict | None:
        data = self.repo.read(commit, "manifest.json", max_bytes=MAX_MANIFEST)
        try:
            found = json.loads(data) if data is not None else None
        except (ValueError, UnicodeDecodeError):
            return None
        return found if isinstance(found, dict) and found.get("protocol") == PROTOCOL else None

    def _offer(self, host: str, entry, key: str) -> Offer | None:
        if not isinstance(entry, dict) or not isinstance(entry.get("candidate"), str) or not _ID.match(entry["candidate"]):
            return None
        commit = entry.get("commit")
        if not isinstance(commit, str) or not _OBJECT.match(commit):
            return None
        # The commit must be the one that host published under that name, not just any id in its manifest.
        if self.repo.read_ref(f"refs/syberlabs/remote/{key}/{host}/{entry['candidate']}") != commit:
            return None
        score = entry.get("score")
        if not (isinstance(score, list) and len(score) == 2 and all(type(x) is int for x in score)):
            score = None
        return Offer(host, entry["candidate"], commit, tuple(score) if score else None)
