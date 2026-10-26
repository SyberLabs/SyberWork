"""A deterministic, model-free critic for a Build Thread contract.

Declare it as a critic check (a check named ``critic`` or ``critic.<reviewer>``), by the absolute
path of this file in the host's own checkout, so a candidate cannot rewrite its reviewer:

    "critic.rules": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_rules.py"]}

It reads the host's request on stdin (``syberlabs.critic.request``: the candidate, the earlier
check results, the unified diff, the changed paths, the objective, and the contract's acceptance
clauses and mutable scope) and prints a findings document (``spec/finding.schema.json``). Three rules:

- high: a changed path outside the contract's mutable scope. The scope rule already refuses such
  a candidate, so this proves the plumbing rather than adding a judgment.
- medium: a changed source file when no test file changed in the same candidate.
- high, test integrity: a line the candidate adds to a Python file under ``tests/`` (or named
  ``test*.py``) that can end, signal or subvert the process the tests run in, or reach the runner
  that judges them (``TEST_INTEGRITY`` lists the patterns; ``docs/CRITIC.md`` says why each is
  there). Test code is candidate code, and it runs inside the process that reports on it.

Every finding names what would falsify it. No model is called. The integrity rule reads each changed
test file's content at the candidate commit and the lines the candidate added from Git
(``git show <commit>:<path>``, ``git diff -U0 <base> <commit>``, run in the check's working
directory, the candidate's worktree), which are content-addressed, so the same request always yields
the same document. Without a repository (a request built by hand), it rebuilds a new file from the
diff in the request, and reports a changed test file it cannot read as a high finding rather than
passing it unread. The file is parsed with ``ast``, so a pattern inside a string or a comment is not
code and does not match. A model-backed critic is a later packet; it must print the same document
shape and declare a ``reviewer`` that is not the candidate's provider.
"""

import ast
import json
import re
import subprocess
import sys

REVIEWER = {"name": "rules", "revision": "1"}
TEST_MARKERS = ("tests/", "test/", "/test_", "_test.", "spec/")
SOURCE_SUFFIXES = (".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".c", ".h", ".cs", ".mjs")
MAX_INTEGRITY_FINDINGS = 40  # per candidate; the summary counts the rest

# The test-integrity rule: what added test code may not do. Each entry is (rule, claim).
TEST_INTEGRITY = {
    "process_exit": "ends or kills a process from test code (os._exit, os.abort, os.kill, signal.raise_signal, ...): "
                    "a test can end its shard with a chosen exit code, or kill another one",
    "signal_handler": "installs a signal handler or timer from test code (signal.signal, signal.alarm, ...)",
    "exit_at_import": "exits or raises SystemExit/KeyboardInterrupt at import or in a module or class fixture: "
                      "the module ends the process that discovers or runs it",
    "harness_override": "a TestCase defines a method that starts or reports a test (run, __call__, debug, id, ...): "
                        "it can report a test that never ran",
    "load_tests": "defines load_tests: the module chooses its own test set at discovery",
    "unittest_patch": "changes unittest's own objects (assigns, deletes or patches unittest attributes or a result's "
                      "callbacks, or reaches self._outcome / shouldStop)",
    "sys_modules": "writes sys.modules['unittest...'] or sys.modules['__main__']",
    "runner_reach": "reaches the process that runs the tests (__main__, gc, frames, raw file descriptors, the shard "
                    "runner's directories): it can forge what the runner reports",
    "dynamic_code": "runs code this review cannot read (exec, eval, compile, a computed import or attribute of "
                    "os, sys, signal or unittest)",
}
EXIT_CALLS = {"os._exit", "os.abort", "os.killpg", "signal.raise_signal", "signal.pthread_kill", "posix._exit",
              "nt._exit", "os.kill"}
SIGNAL_CALLS = {"signal.signal", "signal.alarm", "signal.setitimer", "signal.set_wakeup_fd", "signal.siginterrupt",
                "signal.pthread_sigmask"}
QUIT_CALLS = {"sys.exit", "exit", "quit", "os._exit", "unittest.main"}
QUIT_RAISES = {"SystemExit", "KeyboardInterrupt", "GeneratorExit", "BaseException"}
FIXTURES = {"setUpModule", "tearDownModule", "setUpClass", "tearDownClass"}
HARNESS = {"run", "__call__", "debug", "id", "_callTestMethod", "_callSetUp", "_callTearDown", "_callCleanup",
           "countTestCases", "__getattribute__"}
RESULT_CALLBACKS = {"startTest", "stopTest", "startTestRun", "stopTestRun", "addSuccess", "addError", "addFailure",
                    "addSkip", "addSubTest", "addExpectedFailure", "addUnexpectedSuccess", "addDuration",
                    "wasSuccessful", "testsRun", "shouldStop", "stop"}
UNITTEST_INTERNALS = {"_outcome", "_resultForDoCleanups", "shouldStop"}
IDENTITY_ATTRIBUTES = {"_testMethodName", "__qualname__", "__module__"}
REACH_CALLS = {"gc.get_objects", "gc.get_referrers", "gc.get_referents", "sys._getframe", "sys._current_frames",
               "inspect.currentframe", "inspect.stack", "inspect.getouterframes", "inspect.getinnerframes",
               "inspect.trace", "os.dup", "os.dup2", "os.write", "os.fdopen", "os.set_inheritable",
               "os.set_handle_inheritable", "sys.settrace", "sys.setprofile", "sys.addaudithook",
               "threading.settrace", "threading.setprofile"}
REACH_MODULES = {"__main__", "ctypes", "msvcrt", "_winapi", "gc"}
FRAME_ATTRIBUTES = {"f_back", "f_locals", "f_globals", "tb_frame", "gi_frame", "cr_frame", "ag_frame"}
RUNNER_NAMES = ("syberlabs-shard",)
SENSITIVE_MODULES = {"os", "sys", "signal", "unittest", "gc", "inspect", "posix", "nt", "__main__"}
PATCHERS = ("patch", "patch.object", "patch.dict", "patch.multiple", "mock.patch", "mock.patch.object",
            "mock.patch.dict", "mock.patch.multiple", "unittest.mock.patch", "unittest.mock.patch.object",
            "unittest.mock.patch.dict", "unittest.mock.patch.multiple")


def _in_scope(scope: dict, path: str) -> bool:
    def matches(entry: str) -> bool:
        return entry == "." or path == entry or path.startswith(entry.rstrip("/") + "/")
    if any(matches(entry) for entry in scope.get("exclude", [])):
        return False
    return any(matches(entry) for entry in scope.get("paths", ["."]))


def is_test(path: str) -> bool:
    marker = "/" + path
    return any(m in marker for m in TEST_MARKERS) or path.split("/")[-1].startswith("test_")


def is_test_code(path: str) -> bool:
    """A Python file the test runner may import: under a ``tests/`` directory, or named ``test*.py``."""
    return path.endswith(".py") and (("/" + path).find("/tests/") >= 0 or path.split("/")[-1].startswith("test"))


# --- reading the candidate's test files ------------------------------------------------------


def _git(args: list):
    try:
        proc = subprocess.run(["git", "-c", "core.quotepath=off", *args], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None


def _hunks(diff_text: str) -> dict:
    """``{path: (new file?, {added line number: text}, [new-side lines when the file is new])}`` from a unified diff."""
    files: dict = {}
    path = new_file = None
    number = 0
    header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            path, new_file, header, number = None, False, True, 0
        elif header and line.startswith("new file mode"):
            new_file = True
        elif header and line.startswith("+++ "):
            name = line[4:].rstrip("\t")
            path = name[2:] if name.startswith("b/") else None
            if path is not None:
                files.setdefault(path, [bool(new_file), {}])
        elif line.startswith("@@"):
            header = False
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            number = int(match.group(1)) if match and path is not None else 0
        elif not header and path is not None and number:
            if line.startswith("+"):
                files[path][1][number] = line[1:]
                number += 1
            elif line.startswith(" "):
                number += 1
    return {p: (new, added) for p, (new, added) in files.items()}


def _sources(request: dict, paths: list) -> dict:
    """``{path: (source text or None, added line numbers or None for "unknown")}`` for each changed test file.

    From Git when the candidate commit is in the repository the critic runs in: the content at the
    commit and the lines ``git diff -U0 <base> <commit>`` adds (all lines when that diff cannot be
    read); a path the commit does not have was deleted and adds nothing. Otherwise from the request's
    diff: a new file is rebuilt from it, a modified one cannot be read, and a file the diff does not
    show because it was truncated is unknown. A request with no diff and no repository adds nothing.
    """
    commit = (request.get("candidate") or {}).get("commit")
    base = request.get("base")
    diff_text = request.get("diff") or ""
    truncated = "[diff truncated" in diff_text
    from_request = _hunks(diff_text)
    in_repo = bool(commit) and _git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"]) is not None
    found = {}
    for path in paths:
        if in_repo:
            source = _git(["show", f"{commit}:{path}"])
            if source is None:
                continue  # deleted by the candidate
            diff = _git(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "-U0", base, commit,
                         "--", path]) if base else None
            found[path] = (source, set(_hunks(diff).get(path, (False, {}))[1]) if diff is not None else None)
        elif path in from_request:
            new, lines = from_request[path]
            source = None
            if new and lines and sorted(lines) == list(range(1, len(lines) + 1)):
                source = "\n".join(lines[n] for n in sorted(lines)) + "\n"  # a new file is all added lines
            found[path] = (source, set(lines))
        elif truncated:
            found[path] = (None, None)
    return found


# --- the test-integrity rule -----------------------------------------------------------------


class _Scan(ast.NodeVisitor):
    """Collect ``(line, rule, detail)`` for every construct of ``TEST_INTEGRITY`` in a module."""

    def __init__(self):
        self.aliases: dict = {}
        self.hits: list = []
        self.scopes: list = []  # ("def"|"class", name, node)

    # names --------------------------------------------------------------------------------
    def bind_imports(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else \
                        alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    if alias.name != "*":
                        self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def name(self, node) -> str | None:
        """The dotted name an expression refers to, through import aliases, or None."""
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            inner = self.name(node.value)
            return f"{inner}.{node.attr}" if inner else None
        if isinstance(node, ast.Call):
            func = self.name(node.func)
            first = node.args[0] if node.args else None
            constant = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
            if func in ("__import__", "importlib.import_module") and constant:
                return constant
            if func == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                    and isinstance(node.args[1].value, str):
                inner = self.name(node.args[0])
                return f"{inner}.{node.args[1].value}" if inner else None
        if isinstance(node, ast.Subscript):
            target = self.name(node.value)
            key = node.slice
            if target == "sys.modules" and isinstance(key, ast.Constant) and isinstance(key.value, str):
                return key.value
        return None

    def hit(self, node, rule, detail):
        self.hits.append((getattr(node, "lineno", 0), getattr(node, "end_lineno", None) or getattr(node, "lineno", 0),
                          rule, detail))

    @staticmethod
    def _rooted(name, roots) -> bool:
        return bool(name) and any(name == r or name.startswith(r + ".") for r in roots)

    # scopes -------------------------------------------------------------------------------
    def at_import_time(self) -> bool:
        return not any(kind == "def" for kind, _, _ in self.scopes)

    def in_fixture(self) -> bool:
        return any(kind == "def" and name in FIXTURES for kind, name, _ in self.scopes)

    def visit_If(self, node):
        test = node.test
        guarded = (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"
                   and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators))
        if guarded:  # the module run as a script, not imported by the runner
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        if node.name == "load_tests":
            self.hit(node, "load_tests", "def load_tests")
        if self.scopes and self.scopes[-1][0] == "class" and self._test_case_like(self.scopes[-1][2]) \
                and node.name in HARNESS:
            self.hit(node, "harness_override", f"def {node.name} in class {self.scopes[-1][1]}")
        self.scopes.append(("def", node.name, node))
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.scopes.append(("class", node.name, node))
        self.generic_visit(node)
        self.scopes.pop()

    def _test_case_like(self, cls: ast.ClassDef) -> bool:
        for base in cls.bases:
            name = self.name(base) or ""
            if name.endswith("TestCase") or self._rooted(name, ("unittest",)):
                return True
        return any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")
                   for item in cls.body)

    # statements ---------------------------------------------------------------------------
    def visit_Raise(self, node):
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = self.name(exc) if exc is not None else None
        if name and name.split(".")[-1] in QUIT_RAISES and (self.at_import_time() or self.in_fixture()):
            self.hit(node, "exit_at_import", f"raise {name}")
        self.generic_visit(node)

    def _targets(self, targets, verb):
        for target in targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Attribute):
                    root = self.name(node.value)
                    if self._rooted(root, ("unittest",)):
                        self.hit(node, "unittest_patch", f"{verb} {root}.{node.attr}")
                    elif node.attr in RESULT_CALLBACKS:
                        self.hit(node, "unittest_patch", f"{verb} .{node.attr}")
                    elif node.attr in IDENTITY_ATTRIBUTES:
                        self.hit(node, "unittest_patch", f"{verb} .{node.attr}")
                if isinstance(node, ast.Subscript) and self.name(node.value) == "sys.modules":
                    key = self.name(node)
                    if key and self._rooted(key, ("unittest", "__main__")):
                        self.hit(node, "sys_modules", f"{verb} sys.modules[{key!r}]")
                if isinstance(node, ast.Name) and node.id == "load_tests" and verb == "assigns":
                    self.hit(node, "load_tests", "assigns load_tests")

    def visit_Assign(self, node):
        self._targets(node.targets, "assigns")
        if self.scopes and self.scopes[-1][0] == "class" and self._test_case_like(self.scopes[-1][2]):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in HARNESS:
                    self.hit(node, "harness_override", f"{target.id} = ... in class {self.scopes[-1][1]}")
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._targets([node.target], "assigns")
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._targets([node.target], "assigns")
        self.generic_visit(node)

    def visit_Delete(self, node):
        self._targets(node.targets, "deletes")
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.split(".")[0] in REACH_MODULES:
                self.hit(node, "runner_reach", f"import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module.split(".")[0] in REACH_MODULES and not node.level:
            self.hit(node, "runner_reach", f"from {node.module} import ...")
        self.generic_visit(node)

    # expressions --------------------------------------------------------------------------
    def visit_Attribute(self, node):
        if node.attr in UNITTEST_INTERNALS:
            self.hit(node, "unittest_patch", f".{node.attr}")
        if node.attr in FRAME_ATTRIBUTES:
            self.hit(node, "runner_reach", f".{node.attr}")
        name = self.name(node)
        if name in EXIT_CALLS and name != "os.kill":
            self.hit(node, "process_exit", name)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        key = self.name(node)
        if self.name(node.value) == "sys.modules" and key == "__main__":
            self.hit(node, "runner_reach", "sys.modules['__main__']")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and any(marker in node.value for marker in RUNNER_NAMES):
            self.hit(node, "runner_reach", f"names the shard runner's directories ({node.value[:60]!r})")

    def visit_Call(self, node):
        name = self.name(node.func)
        args = node.args
        constant = args[0].value if args and isinstance(args[0], ast.Constant) else None
        if name == "os.kill" and not (len(args) >= 2 and isinstance(args[1], ast.Constant) and args[1].value == 0):
            self.hit(node, "process_exit", "os.kill")  # os.kill(pid, 0) only asks whether pid is alive
        elif name in EXIT_CALLS and name != "os.kill":  # reached without an attribute, e.g. getattr(os, "_exit")(0)
            self.hit(node, "process_exit", name)
        if name == "getattr":  # a reference by getattr with a constant name: getattr(os, "_exit")
            reference = self.name(node)
            for names, rule in ((EXIT_CALLS, "process_exit"), (SIGNAL_CALLS, "signal_handler"),
                                (REACH_CALLS, "runner_reach")):
                if reference in names:
                    self.hit(node, rule, f"getattr reference to {reference}")
        if name in SIGNAL_CALLS:
            self.hit(node, "signal_handler", name)
        if name in QUIT_CALLS and (self.at_import_time() or self.in_fixture()):
            self.hit(node, "exit_at_import", f"{name}()")
        if name in REACH_CALLS:
            self.hit(node, "runner_reach", f"{name}()")
        if name in ("exec", "eval", "compile"):
            self.hit(node, "dynamic_code", f"{name}()")
        if name in ("__import__", "importlib.import_module", "importlib.reload") and not isinstance(constant, str):
            self.hit(node, "dynamic_code", f"{name}() of a computed name")
        if name in ("getattr", "setattr", "delattr") and len(args) >= 2:
            target = self.name(args[0])
            if not (isinstance(args[1], ast.Constant) and isinstance(args[1].value, str)):
                if self._rooted(target, SENSITIVE_MODULES):
                    self.hit(node, "dynamic_code", f"{name}({target}, <computed>)")
            elif name != "getattr" and (self._rooted(target, ("unittest",)) or args[1].value in RESULT_CALLBACKS
                                        or args[1].value in HARNESS or args[1].value in IDENTITY_ATTRIBUTES):
                self.hit(node, "unittest_patch", f"{name}({target}, {args[1].value!r})")
        if name and name.split(".")[-1] in ("pop", "update", "setdefault", "clear", "__setitem__", "__delitem__") \
                and self.name(node.func.value if isinstance(node.func, ast.Attribute) else None) == "sys.modules":
            method = name.split(".")[-1]
            if method in ("update", "clear") or (isinstance(constant, str)
                                                 and self._rooted(constant, ("unittest", "__main__"))):
                self.hit(node, "sys_modules", f"sys.modules.{method}({constant!r})")
        if name == "sys.modules.get" and constant == "__main__":
            self.hit(node, "runner_reach", "sys.modules.get('__main__')")
        if name in PATCHERS or (name or "").endswith((".patch", ".patch.object", ".patch.dict", ".patch.multiple")):
            target = constant if isinstance(constant, str) else self.name(args[0]) if args else None
            if isinstance(target, str) and self._rooted(target, ("unittest", "__main__", "sys.modules")) \
                    and not target.startswith("unittest.mock"):
                self.hit(node, "unittest_patch", f"{name}({target!r})")
        self.generic_visit(node)


def integrity_findings(path: str, source: str | None, added: set) -> list:
    """High findings for the lines ``added`` to test file ``path`` (content ``source``; ``added`` None: all lines)."""
    if added is not None and not added:
        return []
    if source is None:
        return [{"severity": "high", "file": path, "line": None,
                 "claim": "a changed test file could not be read at the candidate commit, so the test-integrity "
                          "rule did not run on it",
                 "falsifier": "run the critic where `git show <commit>:<path>` works (the candidate's worktree)"}]
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # It cannot import under this interpreter either, but the rule did not read it, so it does not pass it.
        return [{"severity": "high", "file": path, "line": None,
                 "claim": "a changed test file does not parse, so the test-integrity rule could not read it",
                 "falsifier": "a test file that parses under the host's Python"}]
    scan = _Scan()
    scan.bind_imports(tree)
    scan.visit(tree)
    findings, seen = [], set()
    for first, last, rule, detail in sorted(scan.hits):
        line = first if added is None else next((n for n in range(first, last + 1) if n in added), None)
        if line is None or (line, rule) in seen:
            continue
        seen.add((line, rule))
        findings.append({"severity": "high", "file": path, "line": line,
                         "claim": f"test integrity ({rule}): {detail}: test code {TEST_INTEGRITY[rule]}",
                         "falsifier": "remove the construct, or show that it never runs inside the test process "
                                      "(a string, or a child process the test starts and checks)"})
    return findings


def review(request: dict) -> dict:
    changed = list(request.get("changed_paths", []))
    scope = (request.get("contract") or {}).get("scope") or {"paths": ["."], "exclude": []}
    findings = []
    for path in changed:
        if not _in_scope(scope, path):
            findings.append({"severity": "high", "file": path, "line": None,
                             "claim": "the candidate changes a path outside the contract's mutable scope",
                             "falsifier": "show the path under evolution.scope.paths and not under scope.exclude"})
    sources = [p for p in changed if p.endswith(SOURCE_SUFFIXES) and not is_test(p)]
    if sources and not any(is_test(p) for p in changed):
        for path in sources:
            findings.append({"severity": "medium", "file": path, "line": None,
                             "claim": "a source file changed and no test changed in the same candidate",
                             "falsifier": "a changed file under tests/ (or named test_*) that exercises this change"})
    integrity = []
    test_code = [p for p in changed if is_test_code(p)]
    for path, (source, added) in _sources(request, test_code).items():
        integrity += integrity_findings(path, source, added)
    findings += integrity[:MAX_INTEGRITY_FINDINGS]
    summary = (f"{len(findings)} finding(s) over {len(changed)} changed path(s)" if findings
               else f"no findings over {len(changed)} changed path(s)")
    if len(integrity) > MAX_INTEGRITY_FINDINGS:
        summary += f"; {len(integrity) - MAX_INTEGRITY_FINDINGS} more test-integrity finding(s) not listed"
    return {"version": 1, "reviewer": dict(REVIEWER), "findings": findings, "summary": summary}


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except ValueError:
        print("critic_rules: stdin is not a JSON request", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(review(request), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
