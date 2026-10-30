"""Space: the architecture graph derived from a source tree, and its drift against a declared one.

The derived graph is files, symbols and the edges between them, read with ``ast``
from the Python sources under a root. A declared architecture is a list of
components, each owning path globs, and the edges the author intends between
them. It has the same shape as the builder protocol's ``ArchitectureSnapshot``
(``spec/builder/architecture-snapshot.schema.json``): ``nodes`` with ``paths``
and ``edges`` with ``from`` and ``to``.

Mapping files to components, rolling file edges up to component edges, and
comparing them with the declared edges is a reflexion model: an observed edge
that is also declared is a convergence, an observed edge nobody declared is a
divergence, a declared edge nothing observed is an absence. Drift per component
is divergent out-weight over total out-weight.

Ids are readable and stable across commits as long as a path and a qualified
name are stable: ``f:<posix relative path>`` for a file, ``s:<path>::<qualname>``
for a symbol, and the declared id for a component. A symbol also carries a
``signature`` digest so a later pass can follow a rename.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from syberlabs.canonical import canonical, digest

INDEXER_VERSION = "ast-0.1"
EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules", ".syberlabs", ".syberwork",
    "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
})
MAX_FILE_BYTES = 2_000_000


def _glob_to_regex(pattern: str) -> re.Pattern:
    """A path glob over posix relative paths. ``**`` crosses directories, ``*`` and ``?`` stay inside one.

    A pattern with no wildcard names a file or a directory; a directory matches everything under it."""
    pattern = pattern.strip().strip("/")
    if not any(c in pattern for c in "*?["):
        return re.compile("^" + re.escape(pattern) + r"(/.*)?$")
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _module_name(relpath: str) -> str:
    parts = PurePosixPath(relpath).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _signature(node: ast.AST) -> str:
    """A digest of a definition's shape: name, argument names, decorators, bases. Not its body."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        if args.vararg:
            names.append("*" + args.vararg.arg)
        if args.kwarg:
            names.append("**" + args.kwarg.arg)
        shape = {"kind": "function", "name": node.name, "args": names,
                 "decorators": [ast.dump(d) for d in node.decorator_list]}
    else:
        shape = {"kind": "class", "name": node.name, "bases": [ast.dump(b) for b in node.bases]}
    return digest(shape)[:16]


class _Visitor(ast.NodeVisitor):
    """Collects one file's symbols, imports and calls. Call edges are resolved later, once every file is read."""

    def __init__(self, relpath: str):
        self.relpath = relpath
        self.module = _module_name(relpath)
        self.symbols: list[dict] = []
        self.imports: list[dict] = []     # {"module": str, "names": [str] | None, "alias": {local: target}, "line": int}
        self.calls: list[dict] = []       # {"caller": symbol id or file id, "callee": ast target, "line": int}
        self._scope: list[str] = []
        self._classes: set[str] = set()
        self._aliases: dict[str, tuple[str, str | None]] = {}  # local name -> (module, attribute or None)

    def _qualname(self, name: str) -> str:
        return ".".join(self._scope + [name])

    def _current(self) -> str:
        if self._scope:
            return f"s:{self.relpath}::{'.'.join(self._scope)}"
        return f"f:{self.relpath}"

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self._aliases[local] = (alias.name, None)
            self.imports.append({"module": alias.name, "names": None, "level": 0, "line": node.lineno})

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        names = [a.name for a in node.names]
        for alias in node.names:
            self._aliases[alias.asname or alias.name] = (module, alias.name)
        self.imports.append({"module": module, "names": names, "level": node.level, "line": node.lineno})

    def _define(self, node, kind: str) -> None:
        qualname = self._qualname(node.name)
        self.symbols.append({
            "id": f"s:{self.relpath}::{qualname}", "path": self.relpath, "qualname": qualname, "kind": kind,
            "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno), "signature": _signature(node),
        })
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._define(node, "method" if self._in_class() else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._define(node, "method" if self._in_class() else "function")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._classes.add(".".join(self._scope + [node.name]))
        self._define(node, "class")

    def _in_class(self) -> bool:
        return bool(self._scope) and ".".join(self._scope) in self._classes

    def visit_Call(self, node: ast.Call) -> None:
        target = node.func
        if isinstance(target, ast.Name):
            self.calls.append({"caller": self._current(), "name": target.id, "attr": None, "line": node.lineno})
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            self.calls.append({"caller": self._current(), "name": target.value.id, "attr": target.attr, "line": node.lineno})
        self.generic_visit(node)


def _read_file(path: Path, relpath: str) -> tuple[_Visitor | None, str, int]:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        return None, hashlib.sha256(data).hexdigest(), 0
    try:
        tree = ast.parse(data, filename=relpath)
    except (SyntaxError, ValueError):
        return None, hashlib.sha256(data).hexdigest(), data.count(b"\n")
    visitor = _Visitor(relpath)
    visitor.visit(tree)
    return visitor, hashlib.sha256(data).hexdigest(), data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def git_revision(root: Path) -> str | None:
    """HEAD's commit, or None outside a repository or without git."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def git_repository_name(root: Path) -> str:
    """The repository's directory name, through a worktree: the parent of the common git dir, else ``root``'s name."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return Path(root).name
    if out.returncode != 0 or not out.stdout.strip():
        return Path(root).name
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = Path(root) / common
    return common.resolve().parent.name


def index(root: str | Path, paths: list[str] | None = None, revision: str | None = None) -> dict:
    """Derive the Space graph of the Python sources under ``root``.

    ``paths`` limits the walk to those relative directories or files. ``revision`` labels the graph; when
    omitted, git's HEAD is used if there is one. The result is a plain dict so it canonicalizes and digests."""
    root = Path(root).resolve()
    roots = [root / p for p in paths] if paths else [root]
    files: dict[str, dict] = {}
    visitors: dict[str, _Visitor] = {}
    for start in roots:
        if start.is_file():
            candidates = [start]
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(start):
                dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.endswith(".egg-info"))
                candidates.extend(Path(dirpath) / f for f in sorted(filenames) if f.endswith(".py"))
        for path in candidates:
            relpath = path.relative_to(root).as_posix()
            if relpath in files:
                continue
            visitor, sha, lines = _read_file(path, relpath)
            files[relpath] = {"id": f"f:{relpath}", "path": relpath, "hash": sha, "lines": lines,
                              "module": _module_name(relpath), "lang": "python", "parsed": visitor is not None}
            if visitor is not None:
                visitors[relpath] = visitor

    modules = {record["module"]: relpath for relpath, record in files.items()}
    symbols: dict[str, dict] = {}
    for relpath, visitor in visitors.items():
        for symbol in visitor.symbols:
            symbols[symbol["id"]] = symbol
    by_file_name: dict[str, dict[str, str]] = {}   # relpath -> top-level name -> symbol id
    for symbol in symbols.values():
        if "." not in symbol["qualname"]:
            by_file_name.setdefault(symbol["path"], {})[symbol["qualname"]] = symbol["id"]

    def resolve_module(module: str, level: int, importer: str) -> str | None:
        """The repository file a module name points at, or None when it is outside the tree."""
        if level:
            base = _module_name(importer).split(".")
            if not importer.endswith("__init__.py"):
                base = base[:-1]
            base = base[:len(base) - (level - 1)] if level > 1 else base
            module = ".".join(base + ([module] if module else []))
        parts = module.split(".") if module else []
        while parts:
            found = modules.get(".".join(parts))
            if found is not None:
                return found
            parts = parts[:-1]
        return None

    edges: list[dict] = []
    externals: dict[str, set] = {}
    unresolved_calls = 0
    for relpath, visitor in visitors.items():
        local_targets: dict[str, str] = {}   # local name -> symbol id or file id
        for record in visitor.imports:
            target = resolve_module(record["module"], record["level"], relpath)
            if target is None:
                externals.setdefault(relpath, set()).add(record["module"] or ".")
                continue
            if record["names"] is None:
                if target != relpath:
                    edges.append({"from": f"f:{relpath}", "to": f"f:{target}", "kind": "IMPORTS",
                                  "confidence": 1.0, "source": "ast", "line": record["line"]})
                continue
            for name in record["names"]:
                # ``from pkg import module`` points at the module, not at pkg's __init__; a name that is not a
                # submodule points at the module it was imported from.
                sub = resolve_module(f"{record['module']}.{name}" if record["module"] else name, record["level"], relpath)
                if sub is not None and sub != target:
                    if sub != relpath:
                        edges.append({"from": f"f:{relpath}", "to": f"f:{sub}", "kind": "IMPORTS",
                                      "confidence": 1.0, "source": "ast", "line": record["line"]})
                    local_targets[name] = f"f:{sub}"
                else:
                    if target != relpath:
                        edges.append({"from": f"f:{relpath}", "to": f"f:{target}", "kind": "IMPORTS",
                                      "confidence": 1.0, "source": "ast", "line": record["line"]})
                    symbol_id = by_file_name.get(target, {}).get(name)
                    local_targets[name] = symbol_id or f"f:{target}"
        for local, (module, attribute) in visitor._aliases.items():
            if local in local_targets:
                continue
            target = resolve_module(module, 0, relpath)
            if target is not None and attribute is None:
                local_targets[local] = f"f:{target}"
        for call in visitor.calls:
            callee = None
            confidence = 0.0
            if call["attr"] is None:
                callee = by_file_name.get(relpath, {}).get(call["name"])
                confidence = 0.9
                if callee is None:
                    callee = local_targets.get(call["name"])
                    confidence = 0.9 if callee and callee.startswith("s:") else 0.6
            else:
                base = local_targets.get(call["name"])
                if base and base.startswith("f:"):
                    callee = by_file_name.get(base[2:], {}).get(call["attr"])
                    confidence = 0.8
            if callee is None or callee == call["caller"]:
                unresolved_calls += 1 if callee is None else 0
                continue
            edges.append({"from": call["caller"], "to": callee, "kind": "CALLS",
                          "confidence": confidence, "source": "ast", "line": call["line"]})

    seen = set()
    unique = []
    for edge in edges:
        key = (edge["from"], edge["to"], edge["kind"], edge["line"])
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    unique.sort(key=lambda e: (e["from"], e["to"], e["kind"], e["line"]))
    graph = {
        "protocol": "syberlabs.space/factory/v0",
        "kind": "space",
        "indexer": INDEXER_VERSION,
        "root": root.name,
        "revision": revision if revision is not None else git_revision(root),
        "files": dict(sorted(files.items())),
        "symbols": dict(sorted(symbols.items())),
        "edges": unique,
        "externals": {k: sorted(v) for k, v in sorted(externals.items())},
        "unresolved_calls": unresolved_calls,
        "components": {},
        "depends_on": [],
        "unmapped": [],
    }
    return graph


def map_components(graph: dict, declared: dict) -> dict:
    """Assign every file to the last declared component whose path glob matches it, then roll edges up.

    ``declared`` has ``nodes`` (``id``, ``label``, ``paths``) and ``edges`` (``from``, ``to``), the
    ``ArchitectureSnapshot`` shape. Mutates and returns ``graph``."""
    rules = []
    components: dict[str, dict] = {}
    for node in declared.get("nodes", []):
        components[node["id"]] = {"id": node["id"], "label": node.get("label", node["id"]),
                                  "paths": list(node.get("paths", [])), "group": node.get("group"), "files": []}
        for pattern in node.get("paths", []):
            rules.append((_glob_to_regex(pattern), node["id"]))
    unmapped = []
    for relpath, record in graph["files"].items():
        owner = None
        for regex, component in rules:
            if regex.match(relpath):
                owner = component
        record["component"] = owner
        if owner is None:
            unmapped.append(relpath)
        else:
            components[owner]["files"].append(relpath)
    graph["components"] = components
    graph["unmapped"] = unmapped
    graph["declared_edges"] = sorted({(e["from"], e["to"]) for e in declared.get("edges", [])})
    graph["declared_edges"] = [{"from": a, "to": b} for a, b in graph["declared_edges"]]
    rollup: dict[tuple[str, str], dict] = {}
    for edge in graph["edges"]:
        source = component_of(graph, edge["from"])
        target = component_of(graph, edge["to"])
        if source is None or target is None or source == target:
            continue
        entry = rollup.setdefault((source, target), {"from": source, "to": target, "count": 0, "via": []})
        entry["count"] += 1
        if len(entry["via"]) < 20:
            entry["via"].append({"from": edge["from"], "to": edge["to"], "kind": edge["kind"], "line": edge["line"]})
    graph["depends_on"] = [rollup[k] for k in sorted(rollup)]
    return graph


def component_of(graph: dict, node_id: str) -> str | None:
    """The component owning a file or symbol id, from the mapping ``map_components`` wrote."""
    if node_id.startswith("f:"):
        record = graph["files"].get(node_id[2:])
    elif node_id.startswith("s:"):
        record = graph["files"].get(node_id[2:].split("::", 1)[0])
    else:
        return node_id if node_id in graph.get("components", {}) else None
    return record.get("component") if record else None


def reflexion(graph: dict) -> dict:
    """Convergence, divergence and absence between observed and declared component edges, and drift per component."""
    observed = {(e["from"], e["to"]): e for e in graph.get("depends_on", [])}
    declared = {(e["from"], e["to"]) for e in graph.get("declared_edges", [])}
    convergence = [observed[k] for k in sorted(observed) if k in declared]
    divergence = [observed[k] for k in sorted(observed) if k not in declared]
    absence = [{"from": a, "to": b} for a, b in sorted(declared) if (a, b) not in observed]
    drift = {}
    for component in sorted(graph.get("components", {})):
        total = sum(e["count"] for e in observed.values() if e["from"] == component)
        bad = sum(e["count"] for k, e in observed.items() if e["from"] == component and k not in declared)
        drift[component] = {"out_weight": total, "divergent_weight": bad,
                            "score": round(bad / total, 4) if total else 0.0}
    return {"convergence": convergence, "divergence": divergence, "absence": absence,
            "unmapped": list(graph.get("unmapped", [])), "drift": drift,
            "max_drift": max((d["score"] for d in drift.values()), default=0.0)}


def diff(before: dict, after: dict) -> dict:
    """What changed between two Space graphs: files and symbols added, removed, or changed, and the components touched."""
    report: dict = {"files": {"added": [], "removed": [], "changed": []},
                    "symbols": {"added": [], "removed": [], "changed": []}, "components": set()}
    for relpath in sorted(set(before["files"]) | set(after["files"])):
        a, b = before["files"].get(relpath), after["files"].get(relpath)
        if a is None:
            report["files"]["added"].append(relpath)
        elif b is None:
            report["files"]["removed"].append(relpath)
        elif a["hash"] != b["hash"]:
            report["files"]["changed"].append(relpath)
        else:
            continue
        for record in (a, b):
            if record and record.get("component"):
                report["components"].add(record["component"])
    for symbol_id in sorted(set(before["symbols"]) | set(after["symbols"])):
        a, b = before["symbols"].get(symbol_id), after["symbols"].get(symbol_id)
        if a is None:
            report["symbols"]["added"].append(symbol_id)
        elif b is None:
            report["symbols"]["removed"].append(symbol_id)
        elif a["signature"] != b["signature"]:
            report["symbols"]["changed"].append(symbol_id)
    report["components"] = sorted(report["components"])
    return report


def graph_digest(graph: dict) -> str:
    """SHA-256 of the canonical graph. Stable across machines for the same tree and declaration."""
    return digest(graph)


def dumps(graph: dict) -> str:
    return canonical(graph)
