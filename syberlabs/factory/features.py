"""Feature maps: a vector per symbol and per component, from code text, documentation and trace statistics.

The vectorizer is feature hashing (Weinberger et al.): every token of a symbol's source (identifiers split
on case and underscores, keywords, string literals, the docstring) hashes to a dimension with a sign, the
counts are TF-weighted and L2-normalized. Trace statistics from the ledger (how often the symbol ran, how
many effects and io it reached, across recorded runs) are appended as a small block of scaled scalars, so
two symbols that read alike but behave differently sit apart. There is no learned model here: the
repository has no runtime dependencies, and this is the shape a learned embedding would slot into
(``vectorize`` is the one function to replace). It is enough for the queries the plan asks of the map:
what is nearest to a spec clause, what moves when a component moves, and which regions changed between two
commits.

A component's vector is the reference-weighted mean of its symbols' vectors, so the symbols other code
points at dominate. Vectors live in the ledger's SQLite projection (``features`` table), keyed by symbol
and revision, and are rebuilt from the Space graph and the journals, never edited.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import tokenize
from io import StringIO
from pathlib import Path

from syberlabs.errors import Rejected

VECTORIZER = "hash-0.1"
DIM = 512
TRACE_DIM = 6
_SPLIT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_KEYWORDS = frozenset({"def", "class", "return", "if", "else", "elif", "for", "while", "import", "from", "as", "with",
                       "try", "except", "finally", "raise", "yield", "lambda", "and", "or", "not", "in", "is", "None",
                       "True", "False", "pass", "break", "continue", "global", "nonlocal", "assert", "del", "async", "await"})


def tokens(text: str) -> list[str]:
    """The feature tokens of a piece of Python source: identifier subwords (lowercased), keywords, the words of
    strings and comments, and bigrams of consecutive identifier subwords."""
    out: list[str] = []
    words: list[str] = []
    try:
        stream = list(tokenize.generate_tokens(StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        stream = []
    if not stream:
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
            words.extend(w.lower() for w in _SPLIT.findall(word))
    for item in stream:
        if item.type == tokenize.NAME:
            if item.string in _KEYWORDS:
                out.append("kw:" + item.string)
            else:
                words.extend(w.lower() for w in _SPLIT.findall(item.string))
        elif item.type == tokenize.STRING:
            words.extend(w.lower() for w in re.findall(r"[A-Za-z]{3,}", item.string))
        elif item.type == tokenize.COMMENT:
            words.extend(w.lower() for w in re.findall(r"[A-Za-z]{3,}", item.string))
        elif item.type == tokenize.OP and item.string in ("await", "yield"):
            out.append("kw:" + item.string)
    out.extend(words)
    out.extend(a + "_" + b for a, b in zip(words, words[1:]))
    return out


def _slot(token: str) -> tuple[int, int]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "big") % DIM
    sign = 1 if digest[4] & 1 else -1
    return index, sign


def vectorize(text: str, trace: dict | None = None) -> list[float]:
    """A unit vector of ``DIM`` hashed text features followed by ``TRACE_DIM`` scaled trace scalars."""
    counts: dict[int, float] = {}
    for token in tokens(text):
        index, sign = _slot(token)
        counts[index] = counts.get(index, 0.0) + sign
    vector = [0.0] * (DIM + TRACE_DIM)
    for index, count in counts.items():
        vector[index] = math.copysign(math.log1p(abs(count)), count) if count else 0.0
    norm = math.sqrt(sum(v * v for v in vector[:DIM])) or 1.0
    for index in range(DIM):
        vector[index] /= norm
    trace = trace or {}
    scaled = [math.log1p(trace.get("runs", 0)), math.log1p(trace.get("calls", 0)), math.log1p(trace.get("effects", 0)),
              math.log1p(trace.get("io", 0)), math.log1p(trace.get("callers", 0)), math.log1p(trace.get("callees", 0))]
    total = math.sqrt(sum(v * v for v in scaled)) or 1.0
    for offset, value in enumerate(scaled):
        vector[DIM + offset] = 0.5 * value / total  # the trace block weighs half a text block at most
    return vector


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def symbol_source(root: Path, symbol: dict) -> str:
    """The symbol's own lines from its file, or an empty string when the file is gone."""
    path = Path(root) / symbol["path"]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[max(symbol["line"] - 1, 0):symbol.get("end_line", symbol["line"])])


def trace_statistics(ledger, graph: dict) -> dict[str, dict]:
    """Per symbol, over every recorded run in the ledger: runs it appeared in, calls, effects and io reached
    from its spans. Matched by (file, qualified name), which is how the harness names a span."""
    stats: dict[str, dict] = {}
    by_key = {(s["path"], s["qualname"]): sid for sid, s in graph["symbols"].items()}
    if not ledger.database.exists():
        ledger.project()
    connection = sqlite3.connect(ledger.database)
    try:
        rows = connection.execute("SELECT s.file, s.fn, COUNT(DISTINCT s.run_id), COUNT(*), "
                                  "(SELECT COUNT(*) FROM effects e WHERE e.run_id = s.run_id AND e.span = s.span AND e.kind = 'effect'), "
                                  "(SELECT COUNT(*) FROM effects e WHERE e.run_id = s.run_id AND e.span = s.span AND e.kind = 'io') "
                                  "FROM steps s GROUP BY s.file, s.fn").fetchall()
    finally:
        connection.close()
    for file, fn, runs, calls, effects, io in rows:
        sid = by_key.get((file, fn))
        if sid is not None:
            stats[sid] = {"runs": runs, "calls": calls, "effects": effects, "io": io}
    callers: dict[str, int] = {}
    callees: dict[str, int] = {}
    for edge in graph["edges"]:
        if edge["kind"] == "CALLS":
            callers[edge["to"]] = callers.get(edge["to"], 0) + 1
            callees[edge["from"]] = callees.get(edge["from"], 0) + 1
    for sid in graph["symbols"]:
        entry = stats.setdefault(sid, {"runs": 0, "calls": 0, "effects": 0, "io": 0})
        entry["callers"] = callers.get(sid, 0)
        entry["callees"] = callees.get(sid, 0)
    return stats


def build(ledger, graph: dict, root: str | Path) -> dict:
    """Vectors for every symbol and component of ``graph`` at its revision, written to the ledger's projection.
    Returns counts. Rebuilding for the same revision replaces that revision's rows."""
    root = Path(root)
    stats = trace_statistics(ledger, graph)
    revision = graph.get("revision") or "unknown"
    symbol_vectors: dict[str, list[float]] = {}
    for sid, symbol in graph["symbols"].items():
        text = symbol_source(root, symbol)
        if not text:
            continue
        symbol_vectors[sid] = vectorize(text, stats.get(sid))
    components: dict[str, list[list[float]]] = {}
    weights: dict[str, list[float]] = {}
    for sid, vector in symbol_vectors.items():
        component = graph["files"].get(graph["symbols"][sid]["path"], {}).get("component")
        if component is None:
            continue
        weight = 1.0 + stats.get(sid, {}).get("callers", 0)
        components.setdefault(component, []).append([v * weight for v in vector])
        weights.setdefault(component, []).append(weight)
    component_vectors = {}
    for component, vectors in components.items():
        total = sum(weights[component]) or 1.0
        component_vectors[component] = [sum(col) / total for col in zip(*vectors)]
    if not ledger.database.exists():
        ledger.project()
    connection = sqlite3.connect(ledger.database)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS features (revision TEXT, kind TEXT, id TEXT, vectorizer TEXT, "
                           "vector TEXT, trace TEXT, PRIMARY KEY (revision, kind, id))")
        connection.execute("DELETE FROM features WHERE revision = ?", (revision,))
        for sid, vector in symbol_vectors.items():
            connection.execute("INSERT INTO features VALUES (?,?,?,?,?,?)",
                               (revision, "symbol", sid, VECTORIZER, json.dumps([round(v, 6) for v in vector]),
                                json.dumps(stats.get(sid, {}), sort_keys=True)))
        for component, vector in component_vectors.items():
            connection.execute("INSERT INTO features VALUES (?,?,?,?,?,?)",
                               (revision, "component", component, VECTORIZER, json.dumps([round(v, 6) for v in vector]), "{}"))
        connection.commit()
    finally:
        connection.close()
    return {"revision": revision, "symbols": len(symbol_vectors), "components": len(component_vectors),
            "with_trace": sum(1 for s in stats.values() if s.get("runs"))}


def load(ledger, revision: str, kind: str | None = None) -> dict[str, list[float]]:
    if not ledger.database.exists():
        return {}
    connection = sqlite3.connect(ledger.database)
    try:
        try:
            rows = connection.execute("SELECT id, vector FROM features WHERE revision = ?" + (" AND kind = ?" if kind else ""),
                                      (revision, kind) if kind else (revision,)).fetchall()
        except sqlite3.OperationalError:
            return {}
    finally:
        connection.close()
    return {row[0]: json.loads(row[1]) for row in rows}


def revisions(ledger) -> list[str]:
    if not ledger.database.exists():
        return []
    connection = sqlite3.connect(ledger.database)
    try:
        try:
            return [r[0] for r in connection.execute("SELECT DISTINCT revision FROM features ORDER BY rowid").fetchall()]
        except sqlite3.OperationalError:
            return []
    finally:
        connection.close()


def nearest(ledger, revision: str, text: str, k: int = 10, kind: str = "symbol") -> list[tuple[str, float]]:
    """The ``k`` symbols (or components) at ``revision`` whose vectors lie closest to the text's. Text, not a
    symbol, is the query, so a spec clause can ask which cells it is about."""
    vectors = load(ledger, revision, kind)
    if not vectors:
        raise Rejected("features_missing", f"no feature map at revision {revision}; run `factory map build`")
    query = vectorize(text)
    scored = sorted(((sid, cosine(query[:DIM], v[:DIM])) for sid, v in vectors.items()), key=lambda kv: -kv[1])
    return [(sid, round(score, 4)) for sid, score in scored[:k]]


def moved(ledger, before: str, after: str, k: int = 20, kind: str = "symbol") -> list[dict]:
    """What changed most between two revisions' maps: distance per id, plus ids present on one side only.
    This is the plan's third ticket source (a region moved and no spec claims it)."""
    a, b = load(ledger, before, kind), load(ledger, after, kind)
    if not a or not b:
        raise Rejected("features_missing", f"feature maps are needed at both {before} and {after}")
    out = []
    for sid in sorted(set(a) | set(b)):
        if sid in a and sid in b:
            distance = 1.0 - cosine(a[sid], b[sid])
            if distance > 1e-9:
                out.append({"id": sid, "distance": round(distance, 4), "change": "moved"})
        else:
            out.append({"id": sid, "distance": 1.0, "change": "added" if sid in b else "removed"})
    out.sort(key=lambda e: -e["distance"])
    return out[:k]


def impact(graph: dict, components: list[str]) -> dict:
    """What else moves when these components move, from the Space graph: components that depend on them
    (transitively) and the symbols that reference their symbols. Change coupling from git history is the
    v2 addition; this is structure only."""
    known = set(graph.get("components", {}))
    unknown = [c for c in components if c not in known]
    if unknown:
        raise Rejected("component_unknown", f"not in the Space graph: {unknown}")
    dependents: dict[str, set] = {}
    for edge in graph.get("depends_on", []):
        dependents.setdefault(edge["to"], set()).add(edge["from"])
    reached = set(components)
    frontier = list(components)
    while frontier:
        current = frontier.pop()
        for dependent in dependents.get(current, ()):
            if dependent not in reached:
                reached.add(dependent)
                frontier.append(dependent)
    inside = {sid for sid, s in graph["symbols"].items() if graph["files"].get(s["path"], {}).get("component") in set(components)}
    referencing = sorted({e["from"] for e in graph["edges"] if e["to"] in inside and e["from"] not in inside})
    return {"components": sorted(components), "dependents": sorted(reached - set(components)),
            "referencing_symbols": referencing[:200], "referencing_count": len(referencing)}
