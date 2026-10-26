"""G6 Bridge mapping lint: Attest output never maps to ``reconciled``.

Factory gate G6 blocks a merge when an Attest artifact type can map to a
``reconciled`` event, or when caller-supplied ``success``/``evidence`` input is
accepted as reconciliation proof. Only SyberWork's destination status lookup
may append ``reconciled`` (docs/RECONCILIATION.md).

This is a static check over the source tree. It reads Python files with
``ast`` and imports nothing from the repository, so a broken module cannot
make it pass. A missing hook (file, table, function, or route) is a finding:
the lint fails closed. Behavioral checks live in tests/test_g6_bridge_mapping.py.

Usage: python scripts/g6_bridge_mapping_lint.py [repo_root]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CLASSIFIER = "syberlabs/attest_reconcile_map.py"
CLASSIFIER_TABLE = "_CLASSIFICATIONS"
CLASSIFIER_FUNCTION = "classify_attest_artifact"
ALLOWED_CLASSES = {"observation", "handoff"}

# Product code scanned for ``reconciled`` emission. Tests and fixtures may
# build such events to exercise the predicates; product code may not.
PRODUCT_DIRS = ("syberwork", "syberlabs", "examples", "adapters")

# The only places that may append a ``reconciled`` event: each reads the
# destination itself. (file, class, function)
EMITTERS = {
    ("syberwork/core.py", "Work", "reconcile"),
    ("syberlabs/session.py", "Session", "reconcile"),
}
# Read-only uses of the literal that neither write nor map an event.
READ_ONLY_SITES = {
    ("syberlabs/protocol.py", None, None),  # the EventKind vocabulary list
    ("syberlabs/evolution.py", None, "candidate_views"),  # labels an already verified reconciliation
}
# Event kinds that claim an external outcome. A bridge module never names them.
OUTCOME_KINDS = {"reconciled", "effect_succeeded", "reconciliation_checked"}
CALLER_OUTCOME_PARAMS = ("success", "evidence")
REFUSAL = "manual_reconciliation_disabled"
SERVER = "syberwork/server.py"


def _parse(root: Path, rel: str, findings: list[str]) -> ast.Module | None:
    path = root / rel
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        findings.append(f"{rel}: cannot parse ({type(exc).__name__}); G6 hook missing")
        return None


def _const(node: ast.AST | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is type(value) and node.value == value


def _docstrings(tree: ast.AST) -> set[int]:
    """ids of string constants that are module, class, or function docstrings."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _scopes(tree: ast.Module):
    """Yield (class_name, function_name, node) for every function, nested ones included."""
    def visit(node, owner, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from visit(child, child.name, None)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield owner, child.name, child
                yield from visit(child, owner, child.name)
            else:
                yield from visit(child, owner, function)
    yield from visit(tree, None, None)


def _owner_map(tree: ast.Module) -> dict[int, tuple[str | None, str | None]]:
    """Map each node id to its innermost (class, top-level method) scope."""
    owners: dict[int, tuple[str | None, str | None]] = {}

    def visit(node, owner, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name, None)
                continue
            scope = (owner, function)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and function is None:
                scope = (owner, child.name)
            owners[id(child)] = scope
            visit(child, scope[0], scope[1])
    visit(tree, None, None)
    return owners


def check_classifier(root: Path, findings: list[str]) -> None:
    tree = _parse(root, CLASSIFIER, findings)
    if tree is None:
        return
    tables = [node for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == CLASSIFIER_TABLE for t in node.targets)]
    if len(tables) != 1 or not isinstance(tables[0].value, ast.Dict):
        findings.append(f"{CLASSIFIER}: {CLASSIFIER_TABLE} must be one literal dict; G6 hook missing")
    else:
        table = tables[0].value
        if not table.keys:
            findings.append(f"{CLASSIFIER}: {CLASSIFIER_TABLE} is empty")
        for key, value in zip(table.keys, table.values):
            label = ast.unparse(key) if key is not None else "**"
            if key is None or not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                findings.append(f"{CLASSIFIER}:{value.lineno}: {CLASSIFIER_TABLE} key {label} is not a string literal")
            if not (isinstance(value, ast.Constant) and value.value in ALLOWED_CLASSES):
                findings.append(f"{CLASSIFIER}:{value.lineno}: Attest kind {label} maps to "
                                f"{ast.unparse(value)}; only {sorted(ALLOWED_CLASSES)} are allowed")
    # Nothing else may write the table.
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            base = target.value if isinstance(target, ast.Subscript) else None
            if isinstance(base, ast.Name) and base.id == CLASSIFIER_TABLE:
                findings.append(f"{CLASSIFIER}:{node.lineno}: {CLASSIFIER_TABLE} is mutated")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == CLASSIFIER_TABLE \
                and node.func.attr in ("update", "setdefault", "__setitem__"):
            findings.append(f"{CLASSIFIER}:{node.lineno}: {CLASSIFIER_TABLE} is mutated")

    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == CLASSIFIER_FUNCTION]
    if len(functions) != 1:
        findings.append(f"{CLASSIFIER}: {CLASSIFIER_FUNCTION} not found; G6 hook missing")
        return
    returns = [node for node in ast.walk(functions[0]) if isinstance(node, ast.Return)]
    if not returns:
        findings.append(f"{CLASSIFIER}: {CLASSIFIER_FUNCTION} has no return")
    for node in returns:
        value = node.value
        keys = {k.value: v for k, v in zip(value.keys, value.values)
                if isinstance(k, ast.Constant)} if isinstance(value, ast.Dict) else {}
        if not isinstance(value, ast.Dict) or None in value.keys:
            findings.append(f"{CLASSIFIER}:{node.lineno}: {CLASSIFIER_FUNCTION} must return a literal dict")
        elif not _const(keys.get("verified_reconciliation"), False):
            findings.append(f"{CLASSIFIER}:{node.lineno}: verified_reconciliation must be the literal False")
        extra = set(keys) - ALLOWED_CLASSES - {"refuse", "verified_reconciliation"}
        if extra:
            findings.append(f"{CLASSIFIER}:{node.lineno}: unexpected classification keys {sorted(extra)}")


def _bridge_modules(root: Path) -> list[str]:
    found = {CLASSIFIER}
    for folder in PRODUCT_DIRS:
        base = root / folder
        if base.is_dir():
            found.update(p.relative_to(root).as_posix() for p in base.rglob("*attest*.py"))
    return sorted(found)


def check_bridge_modules(root: Path, findings: list[str]) -> None:
    """Attest bridge modules never name an outcome kind, reconcile, or mark data verified."""
    modules = _bridge_modules(root)
    if len(modules) < 2:
        findings.append("no Attest bridge modules found besides the classifier; G6 hook missing")
    for rel in modules:
        tree = _parse(root, rel, findings)
        if tree is None:
            continue
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in OUTCOME_KINDS and id(node) not in docs:
                findings.append(f"{rel}:{node.lineno}: Attest bridge names outcome event kind {node.value!r}")
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name in ("reconcile", "_append"):
                    findings.append(f"{rel}:{node.lineno}: Attest bridge calls {name}()")
                for keyword in node.keywords:
                    if keyword.arg == "verified" and not _const(keyword.value, False):
                        findings.append(f"{rel}:{node.lineno}: Attest bridge passes verified={ast.unparse(keyword.value)}")
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if _const(key, "verified") and not _const(value, False):
                        findings.append(f"{rel}:{node.lineno}: Attest bridge sets "
                                        f"'verified': {ast.unparse(value)}")


def _emits_reconciled(node: ast.AST) -> bool:
    """A call to an event writer whose arguments name ``reconciled``."""
    if not isinstance(node, ast.Call):
        return False
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    named = any(_const(inner, "reconciled") for argument in node.args + [k.value for k in node.keywords]
                for inner in ast.walk(argument))
    return named and "append" in name and len(node.args) + len(node.keywords) >= 2


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _in_compare(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """True when the literal is only compared against (``==``, ``in``, ``not in``)."""
    current = parents.get(id(node))
    while isinstance(current, (ast.Tuple, ast.List, ast.Set)):
        current = parents.get(id(current))
    return isinstance(current, ast.Compare)


def check_emitters(root: Path, findings: list[str]) -> None:
    """``reconciled`` is written only inside the destination-lookup functions.

    Every ``"reconciled"`` literal in product code must be a read-only
    comparison, sit in an allowed emitter, or sit in a listed read-only
    vocabulary site. Anything else (a new writer, a variable holding the kind,
    a mapping table) is a finding, so indirection cannot slip past.
    """
    seen = set()
    for folder in PRODUCT_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            tree = _parse(root, rel, findings)
            if tree is None:
                continue
            owners, parents, docs = _owner_map(tree), _parents(tree), _docstrings(tree)
            for node in ast.walk(tree):
                owner, function = owners.get(id(node), (None, None))
                site = (rel, owner, function)
                if site in EMITTERS and _emits_reconciled(node):
                    seen.add(site)
                if not _const(node, "reconciled") or id(node) in docs or _in_compare(node, parents):
                    continue
                if site in EMITTERS or site in READ_ONLY_SITES:
                    continue
                where = ".".join(x for x in (owner, function) if x) or "<module>"
                findings.append(f"{rel}:{node.lineno}: {where} uses 'reconciled' outside a comparison; only "
                                "the destination status lookup may emit it (G6)")
    for rel, owner, function in sorted(EMITTERS - seen):
        findings.append(f"{rel}: {owner}.{function} no longer emits 'reconciled'; update the G6 emitter list")


def _guard_refuses(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If) or len(statement.body) != 1 or statement.orelse:
        return False
    raised = statement.body[0]
    return (isinstance(raised, ast.Raise) and isinstance(raised.exc, ast.Call)
            and getattr(raised.exc.func, "id", None) == "Rejected"
            and bool(raised.exc.args) and _const(raised.exc.args[0], REFUSAL))


def _strict_none_guard(test: ast.expr, params: list[str]) -> bool:
    """``a is not None or b is not None`` over exactly ``params``: any non-None value is refused."""
    clauses = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) else [test]
    names = []
    for clause in clauses:
        if not (isinstance(clause, ast.Compare) and isinstance(clause.left, ast.Name)
                and len(clause.ops) == 1 and isinstance(clause.ops[0], ast.IsNot)
                and _const(clause.comparators[0], None)):
            return False
        names.append(clause.left.id)
    return sorted(names) == sorted(params)


def check_emitter_inputs(root: Path, findings: list[str]) -> None:
    """Caller success/evidence is refused before any work and never read afterwards."""
    for rel, owner, function in sorted(EMITTERS):
        tree = _parse(root, rel, findings)
        if tree is None:
            continue
        matches = [node for cls, name, node in _scopes(tree) if (cls, name) == (owner, function)]
        if len(matches) != 1:
            findings.append(f"{rel}: {owner}.{function} not found; G6 hook missing")
            continue
        node = matches[0]
        arguments = node.args
        if arguments.vararg or arguments.kwarg:
            findings.append(f"{rel}:{node.lineno}: {owner}.{function} accepts *args/**kwargs; "
                            "caller outcome fields could pass unchecked")
        names = [a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
        outcome = [p for p in CALLER_OUTCOME_PARAMS if p in names]
        if not outcome:
            continue
        positional = [a.arg for a in arguments.posonlyargs + arguments.args]
        if any(p in positional for p in outcome):
            findings.append(f"{rel}:{node.lineno}: {owner}.{function} takes {outcome} positionally")
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        guard = body[0] if body else None
        if guard is None or not _guard_refuses(guard) or not _strict_none_guard(guard.test, outcome):
            findings.append(f"{rel}:{node.lineno}: {owner}.{function} must first refuse any non-None "
                            f"{'/'.join(outcome)} with Rejected({REFUSAL!r})")
            guard = None
        allowed = {id(n) for n in ast.walk(guard.test)} if guard is not None else set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id in outcome and id(inner) not in allowed:
                findings.append(f"{rel}:{inner.lineno}: {owner}.{function} reads caller {inner.id!r} "
                                "outside the refusal guard")


def check_server_route(root: Path, findings: list[str]) -> None:
    """The HTTP reconcile route refuses before calling reconcile and forwards only proposal_id."""
    tree = _parse(root, SERVER, findings)
    if tree is None:
        return
    branches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "operation" and len(node.test.comparators) == 1
                and _const(node.test.comparators[0], "reconcile")]
    if len(branches) != 1:
        findings.append(f"{SERVER}: expected one `operation == \"reconcile\"` route, found {len(branches)}; "
                        "G6 hook missing")
        return
    body = branches[0].body
    calls = [(i, n) for i, stmt in enumerate(body) for n in ast.walk(stmt)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "reconcile"]
    if len(calls) != 1:
        findings.append(f"{SERVER}:{branches[0].lineno}: reconcile route must make exactly one reconcile call")
        return
    index, call = calls[0]
    if not any(_guard_refuses(stmt) for stmt in body[:index]):
        findings.append(f"{SERVER}:{call.lineno}: reconcile route calls reconcile without first raising "
                        f"Rejected({REFUSAL!r}) for caller outcome fields")
    for keyword in call.keywords:
        findings.append(f"{SERVER}:{call.lineno}: reconcile route forwards keyword "
                        f"{keyword.arg or '**' + ast.unparse(keyword.value)}")
    for argument in call.args:
        for inner in ast.walk(argument):
            if isinstance(inner, ast.Starred):
                findings.append(f"{SERVER}:{call.lineno}: reconcile route forwards *{ast.unparse(inner.value)}")
            if isinstance(inner, ast.Name) and inner.id == "data" and not (
                    isinstance(argument, ast.Subscript) and _const(argument.slice, "proposal_id")):
                findings.append(f"{SERVER}:{call.lineno}: reconcile route forwards request data "
                                f"{ast.unparse(argument)}; only data['proposal_id'] is allowed")


CHECKS = (check_classifier, check_bridge_modules, check_emitters, check_emitter_inputs, check_server_route)


def lint(root: Path) -> list[str]:
    findings: list[str] = []
    for check in CHECKS:
        check(Path(root), findings)
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path(__file__).resolve().parents[1]
    findings = lint(root)
    for finding in findings:
        print("G6 FAIL " + finding)
    if findings:
        print(f"G6 Bridge mapping lint: {len(findings)} finding(s)")
        return 1
    print("G6 Bridge mapping lint: ok (" + ", ".join(check.__name__ for check in CHECKS) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
