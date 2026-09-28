"""Read-only context selection by path and symbol. No embeddings, no index on disk.

Terms come from the objective. Files in the read scope are scored by term hits
in their path, symbol definitions (``def``, ``class``, ``function``, ...), and
text, using ``git grep`` on the base commit, so the working tree and unrelated
files are never read into memory. Top files contribute line windows around
their hits until a byte budget is spent. Short repository guidance files are
offered first.

The thread records what was selected (path, blob id, line range, bytes, why)
and a digest of that list. The text itself is read from Git when a provider
runs and is not stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from syberlabs.canonical import digest
from syberlabs.evolution import in_scope
from syberlabs.gitspace import Repo

GUIDANCE = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "README.md")
STOP = frozenset("""a an and are add as at be by for from get in into is it make new of on or set the this
to use when with without should would could must need needs want""".split())
SYMBOL = re.compile(r"^\s*(?:async\s+def|def|class|function|func|fn|interface|type|struct|const|let|var)\s+([A-Za-z_][\w]*)")
WINDOW = 12
MAX_FILE_BYTES = 262_144


@dataclass(frozen=True)
class Excerpt:
    path: str
    blob: str
    start: int
    end: int
    reason: str
    text: str

    def ref(self) -> dict:
        return {"path": self.path, "blob": self.blob, "lines": [self.start, self.end],
                "bytes": len(self.text.encode()), "reason": self.reason}


def terms(objective: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9]+", objective)
    found = []
    for word in words:
        for part in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", word) + [word]:
            lowered = part.lower()
            if len(lowered) >= 3 and lowered not in STOP and lowered not in found:
                found.append(lowered)
    return found[:12]


def _windows(lines: list[int], total: int) -> list[tuple[int, int]]:
    spans = []
    for line in sorted(lines):
        start, end = max(1, line - WINDOW), min(total, line + WINDOW)
        if spans and start <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
        else:
            spans.append((start, end))
    return spans


def select(repo: Repo, commit: str, scope: dict, objective: str, *, max_bytes: int = 24_000,
           max_files: int = 12, drop: tuple[str, ...] = ()) -> list[Excerpt]:
    """Excerpts for ``objective`` from files ``scope`` admits at ``commit``."""
    allowed = [path for path in repo.files(commit) if in_scope(scope, path) and path not in drop]
    allowed_set = set(allowed)
    wanted = terms(objective)
    hits: dict[str, dict] = {}
    if wanted:
        argv = ["grep", "-I", "-n", "-i", "--full-name", "-z"]
        for term in wanted:
            argv += ["-e", term]
        raw = repo.git(*argv, commit, "--", *scope["paths"], ok=(0, 1))
        for record in raw.decode(errors="replace").splitlines():
            parts = record.split("\0")
            if len(parts) < 3:
                continue
            path = parts[0].split(":", 1)[1] if parts[0].startswith(commit + ":") else parts[0]
            if path not in allowed_set:
                continue
            line, text = int(parts[1]), parts[2]
            entry = hits.setdefault(path, {"lines": [], "score": 0})
            entry["lines"].append(line)
            lowered = text.lower()
            symbol = SYMBOL.match(text)
            entry["score"] += 1 + sum(4 for term in wanted if symbol and term in symbol.group(1).lower())
            entry["score"] += sum(1 for term in wanted if term in lowered) - 1
        for path in allowed:
            bonus = sum(3 for term in wanted if term in path.lower())
            if bonus:
                hits.setdefault(path, {"lines": [], "score": 0})["score"] += bonus
    ranked = sorted(hits, key=lambda path: (-hits[path]["score"], path))[:max_files]
    excerpts, spent = [], 0
    guidance = [path for path in GUIDANCE if path in allowed_set]
    for path, reason in [(p, "guidance") for p in guidance[:2]] + [(p, "match") for p in ranked if p not in guidance]:
        found = repo.entry(commit, path)
        if found is None or found[2] > MAX_FILE_BYTES:
            continue
        content = repo.read(commit, path, MAX_FILE_BYTES)
        if content is None or b"\0" in content[:8000]:
            continue
        lines = content.decode(errors="replace").splitlines()
        if reason == "guidance":
            spans = [(1, min(len(lines), 40))]
        else:
            spans = _windows(hits[path]["lines"], len(lines)) or [(1, min(len(lines), 2 * WINDOW))]
        for start, end in spans:
            text = "\n".join(lines[start - 1:end])
            size = len(text.encode())
            if spent + size > max_bytes:
                remaining = max_bytes - spent
                if remaining < 400:
                    break
                text = text.encode()[:remaining].decode(errors="ignore")
                end = start + text.count("\n")
                size = len(text.encode())
            excerpts.append(Excerpt(path, found[1], start, end, reason, text))
            spent += size
        if spent >= max_bytes:
            break
    return excerpts


def context_digest(excerpts: list[Excerpt]) -> str:
    return digest([excerpt.ref() for excerpt in excerpts])
