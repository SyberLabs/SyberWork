"""A deterministic, model-free critic for a Build Thread contract.

Declare it as a critic check (a check named ``critic`` or ``critic.<reviewer>``), by the absolute
path of this file in the host's own checkout, so a candidate cannot rewrite its reviewer:

    "critic.rules": {"argv": ["C:/Python312/python.exe", "D:/src/SyberWork/examples/critic_rules.py"]}

It reads the host's request on stdin (``syberlabs.critic.request``: the candidate, the earlier
check results, the unified diff, the changed paths, the objective, and the contract's acceptance
clauses and mutable scope) and prints a findings document (``spec/finding.schema.json``). Five rules:

- high: a changed path outside the contract's mutable scope. The scope rule already refuses such
  a candidate, so this proves the plumbing rather than adding a judgment.
- medium: a changed source file when no test file changed in the same candidate.
- high, test integrity: a line the candidate adds to a Python file under ``tests/`` (or named
  ``test*.py``) that can end, signal or subvert the process the tests run in, or reach the runner
  that judges them (``TEST_INTEGRITY`` lists the patterns; ``docs/CRITIC.md`` says why each is
  there). Test code is candidate code, and it runs inside the process that reports on it. Any other
  Python file the candidate adds or changes is held to the harness-tampering families
  (``HARNESS_RULES``): a test can import it, so the same code one ``import`` away from a test runs in
  the same process. Ending the process, signal handlers, exiting at import and ``load_tests`` stay
  test-scoped (``TEST_ONLY_RULES``): production code legitimately exits. A compiled module the
  candidate adds (``.pyc``, ``.pyd``, ``.so``) is a high finding: Python can import it and this rule
  cannot read it.
- high, secret added: a line the candidate adds to any file holds a value in the shape of a credential
  (a key prefix, a private-key header) that looks generated; low when it has the shape and looks typed
  by hand, as a fixture does. The finding names the shape and the line and never repeats the value.
- medium, test weakened: a test that was on the base is gone, a test gains a skip or an expected
  failure, or a test asserts less than it did on the base (``WEAKENED``). Each has honest uses, so by
  default it is for the person who accepts to read. With ``--block-weakened-tests`` after the file's
  path in the check's argv these findings are high and refuse acceptance; the reviewer then declares
  another revision, so a ``Reviewed-by`` line says which one ran.

Every finding names what would falsify it. No model is called. The integrity rule reads each changed
Python file's content at the candidate commit and the lines the candidate added from Git
(``git show <commit>:<path>``, ``git diff -U0 <base> <commit>``, run in the check's working
directory, the candidate's worktree), which are content-addressed, so the same request always yields
the same document. Without a repository (a request built by hand), it rebuilds a new file from the
diff in the request, and reports a changed test file it cannot read as a high finding rather than
passing it unread. The file is parsed with ``ast``, so a pattern inside a string or a comment is not
code and does not match. A model-backed critic is a later packet; it must print the same document
shape and declare a ``reviewer`` that is not the candidate's provider.
"""

import ast
import difflib
import json
import re
import subprocess
import sys

# Revision 2: the integrity rule (test files, then every changed Python file) changed what this reviewer
# refuses, so a Reviewed-by line naming revision 1 would misdescribe it. Revision 3: harness_override
# also names the two methods through which a test reports an expected failure or an unexpected success.
# Revision 4: harness_override also names the methods that register and run a test's cleanups and run its
# subtests, and unittest_patch an assignment of one of them on anything.
# Revision 5: test_replaced, an assignment, deletion or patch of a test method (an attribute named test...) on
# anything other than ``self``.
# Revision 6: secret_added (high) and the test-weakened rule (medium; high under ``--block-weakened-tests``, which
# declares revision ``6-block-weakened``).
REVIEWER = {"name": "rules", "revision": "6"}
BLOCK_WEAKENED = "--block-weakened-tests"
BLOCKING_REVISION = "6-block-weakened"
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
    "harness_override": "defines, in a TestCase, a method that starts or reports a test (run, __call__, debug, id, "
                        "...): it can report a test that never ran; or one that runs its cleanups or subtests "
                        "(doCleanups, subTest, ...): it can swallow its own failures",
    "load_tests": "defines load_tests: the module chooses its own test set at discovery",
    "test_replaced": "replaces, deletes or patches a test method (an attribute named test... on anything other than "
                     "self): the replaced test then reports whatever its replacement does",
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
CLEANUP_METHODS = {"addCleanup", "enterContext", "doCleanups", "addClassCleanup", "enterClassContext",
                   "doClassCleanups", "subTest"}
HARNESS = {"run", "__call__", "debug", "id", "_callTestMethod", "_callSetUp", "_callTearDown", "_callCleanup",
           "_addExpectedFailure", "_addUnexpectedSuccess", "countTestCases", "__getattribute__"} | CLEANUP_METHODS
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

# Where each family applies. Test code gets every family. Any other changed Python file gets the
# families that tamper with the harness from inside the test process: a test can import it.
HARNESS_RULES = ("harness_override", "unittest_patch", "sys_modules", "runner_reach", "dynamic_code", "test_replaced")
TEST_ONLY_RULES = ("process_exit", "signal_handler", "exit_at_import", "load_tests")
# Two parts of ``runner_reach`` stay test-scoped. A forged report needs the channel's nonce, and only
# reading the process's memory gets it: gc, frames, ``__main__``, ctypes, trace and audit hooks.
# Those apply everywhere. The write side applies to tests only: raw descriptors and handles
# (``os.write``, ``os.dup``, ``os.fdopen``, ``msvcrt``, ``_winapi``) are I/O that production code and
# the runner itself use. Without the nonce, a write through them is ``channel_tampered``. The
# runner's directory names lead only to its logs, now that results travel over the channel.
TEST_ONLY_REACH_CALLS = {"os.dup", "os.dup2", "os.write", "os.fdopen", "os.set_inheritable",
                         "os.set_handle_inheritable"}
TEST_ONLY_REACH_MODULES = {"msvcrt", "_winapi"}
# The prefix ``unittest``'s loader collects test methods by (``TestLoader.testMethodPrefix``).
TEST_PREFIX = "test"
PYTHON_SOURCE = (".py", ".pyw")
PYTHON_COMPILED = (".pyc", ".pyd", ".so")

# The test-weakened rule: what a candidate may do to a test so that a check passes without the work. Each entry is
# (rule, claim). Compared with the base, so what the base already had is not reported again.
WEAKENED = {
    "test_removed": "the candidate removes {count} test(s) that were on the base: {names}",
    "test_skipped": "{count} test(s) gain a skip or an expected failure in this candidate: {names}",
    "assertions_removed": "{count} test(s) that were on the base assert less than they did (assertions that can fail "
                          "the test, base to candidate): {names}",
    "test_skipped_by_text": "an added line skips or narrows a test by its text ({marker})",
}
WEAKENED_FALSIFIERS = {
    "test_removed": "the same tests under another name with their assertions, or an objective that removes what they covered",
    "test_skipped": "the marker removed, or the condition under which the test cannot run stated in the objective",
    "assertions_removed": "the assertions restored, or shown in a helper the test now calls",
    "test_skipped_by_text": "the marker removed, or shown to sit in a string or a comment",
}
# Decorators are matched by their last name, wherever they come from: unittest.skip, pytest.mark.skip, a team's own.
SKIP_ALWAYS = {"skip"}                                 # decorators that keep a test from running, whatever happens
SKIP_SOMETIMES = {"skipIf", "skipUnless", "skipif"}    # ... under a condition
EXPECTED_TO_FAIL = {"expectedFailure", "xfail"}        # the test runs, and passing is what fails it
SKIP_CALLS = {"pytest.skip", "pytest.xfail"}           # with ``.skipTest(...)`` on anything and ``raise SkipTest``
SKIP_ATTRIBUTES = {"__unittest_skip__", "__unittest_expecting_failure__"}
SKIPPING_FIXTURES = {"setUp", "asyncSetUp", "setUpClass"}  # a skip raised here skips every test of the class
SWALLOWED = {"AssertionError", "Exception", "BaseException"}
ASSERTING_CALLS = {"pytest.raises", "pytest.warns", "pytest.fail"}
SCRIPT_TEST_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
SCRIPT_SKIP = re.compile(r"\b(?:it|test|describe|suite|context)\.(?:skip|todo|only|fixme)\s*\(|\bx(?:it|describe|test)\s*\(")
RENAMED = 0.6                 # two test names in one file this alike (difflib ratio) are one test, renamed
MAX_NAMES = 8                 # test names listed in one finding; the claim counts the rest
MAX_WEAKENED_FINDINGS = 40
# The secret-added rule. The first pattern is ``syberlabs._patterns.SECRET``, copied because this file runs alone, by
# its path; tests/test_critic.py keeps the two equal.
SECRET = re.compile(r"(ghp_|gho_|ghs_|github_pat_|sk-ant-|sk-|xox[abprs]-|AKIA)[A-Za-z0-9_\-]{8,}")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")
KEY_MATERIAL = re.compile(r"[A-Za-z0-9+/]{40,}")  # a line of a key's base64 body
GENERATED_LENGTH = 20         # a generated key is at least this long after its prefix, and mixes the two cases
MAX_SECRET_FINDINGS = 20


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
    return path.endswith(PYTHON_SOURCE) and (("/" + path).find("/tests/") >= 0
                                            or path.split("/")[-1].startswith("test"))


def is_python_code(path: str) -> bool:
    """Any Python source file: a test can import it, so it runs in the test process."""
    return path.endswith(PYTHON_SOURCE)


def is_script_test(path: str) -> bool:
    """A JavaScript or TypeScript test file, by its place or its name (``tests/``, ``x.test.ts``, ``x.spec.js``)."""
    name = path.split("/")[-1]
    return path.endswith(SCRIPT_TEST_SUFFIXES) and (is_test(path) or ".test." in name or ".spec." in name
                                                    or "/__tests__/" in "/" + path)


# --- reading the candidate's Python files ----------------------------------------------------


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
    """``{path: (source text or None, added line numbers or None for "unknown")}`` for each changed Python file.

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
        self.hits: list = []  # (first line, last line, rule, detail, test-scoped only?)
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

    def hit(self, node, rule, detail, test_only=False):
        """Record a construct; ``test_only`` marks the part of a family that applies to test code only."""
        self.hits.append((getattr(node, "lineno", 0), getattr(node, "end_lineno", None) or getattr(node, "lineno", 0),
                          rule, detail, test_only or rule in TEST_ONLY_RULES))

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

    @staticmethod
    def _anchored(node) -> bool:
        """A computed module name whose package is fixed in the source: ``f"pkg.{name}"`` or ``"pkg." + name``.

        It imports a submodule of ``pkg``, so it cannot be ``os``, ``gc`` or ``ctypes``. A sensitive or
        reaching package (``os.``, ``ctypes.``, ...) does not anchor it.
        """
        if isinstance(node, ast.JoinedStr) and node.values:
            first = node.values[0]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            first = node.left
        else:
            return False
        prefix = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""
        root = prefix.split(".")[0]
        return "." in prefix and root.isidentifier() and root not in SENSITIVE_MODULES | REACH_MODULES

    def _test_case_like(self, cls: ast.ClassDef) -> bool:
        for base in cls.bases:
            name = self.name(base) or ""
            if name.endswith("TestCase") or self._rooted(name, ("unittest",)):
                return True
        return any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")
                   for item in cls.body)

    def _patched_attributes(self, name: str, node: ast.Call) -> list:
        """The attribute names a ``mock.patch`` call replaces, when the source names them (none on ``self``).

        ``patch("pkg.Cls.attr")`` replaces ``attr``; ``patch.object(obj, "attr")`` and ``patch.multiple(obj,
        attr=...)`` replace theirs on ``obj``; ``patch.dict`` replaces keys, not attributes.
        """
        args = node.args
        if name.endswith("patch.dict"):
            return []
        if name.endswith("patch.object"):
            if len(args) >= 2 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str) \
                    and not self._on_self(args[0]):
                return [args[1].value]
            return []
        if name.endswith("patch.multiple"):
            return [] if args and self._on_self(args[0]) else [k.arg for k in node.keywords if k.arg]
        first = args[0] if args else next((k.value for k in node.keywords if k.arg == "target"), None)
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and "." in first.value:
            return [first.value.rsplit(".", 1)[1]]
        return []

    # statements ---------------------------------------------------------------------------
    def visit_Raise(self, node):
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = self.name(exc) if exc is not None else None
        if name and name.split(".")[-1] in QUIT_RAISES and (self.at_import_time() or self.in_fixture()):
            self.hit(node, "exit_at_import", f"raise {name}")
        self.generic_visit(node)

    @staticmethod
    def _leaves(target):
        """The names a target binds: the target itself, or each element of a tuple, list or starred target."""
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                yield from _Scan._leaves(element)
        elif isinstance(target, ast.Starred):
            yield from _Scan._leaves(target.value)
        else:
            yield target

    @staticmethod
    def _on_self(node) -> bool:
        return isinstance(node, ast.Name) and node.id == "self"

    def _test_method_targets(self, targets, verb):
        """``X.test_... = ...`` or ``del X.test_...`` when X is not ``self``: a test's method replaced at run time.

        ``self.test_...`` is left out: a test instance has already fetched its own method when its code runs,
        and the attribute cannot reach another test's instance.
        """
        for target in targets:
            for leaf in self._leaves(target):
                if isinstance(leaf, ast.Attribute) and leaf.attr.startswith(TEST_PREFIX) and not self._on_self(leaf.value):
                    owner = self.name(leaf.value) or ast.unparse(leaf.value)
                    self.hit(leaf, "test_replaced", f"{verb} {owner}.{leaf.attr}")

    def _targets(self, targets, verb):
        self._test_method_targets(targets, verb)
        for target in targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Attribute):
                    root = self.name(node.value)
                    if self._rooted(root, ("unittest",)):
                        self.hit(node, "unittest_patch", f"{verb} {root}.{node.attr}")
                    elif node.attr in RESULT_CALLBACKS:
                        self.hit(node, "unittest_patch", f"{verb} .{node.attr}")
                    elif node.attr in IDENTITY_ATTRIBUTES or node.attr in CLEANUP_METHODS:
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
            root = alias.name.split(".")[0]
            if root in REACH_MODULES:
                self.hit(node, "runner_reach", f"import {alias.name}", root in TEST_ONLY_REACH_MODULES)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        root = (node.module or "").split(".")[0]
        if root in REACH_MODULES and not node.level:
            self.hit(node, "runner_reach", f"from {node.module} import ...", root in TEST_ONLY_REACH_MODULES)
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
            self.hit(node, "runner_reach", f"names the shard runner's directories ({node.value[:60]!r})", True)

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
                    self.hit(node, rule, f"getattr reference to {reference}", reference in TEST_ONLY_REACH_CALLS)
        if name in SIGNAL_CALLS:
            self.hit(node, "signal_handler", name)
        if name in QUIT_CALLS and (self.at_import_time() or self.in_fixture()):
            self.hit(node, "exit_at_import", f"{name}()")
        if name in REACH_CALLS:
            self.hit(node, "runner_reach", f"{name}()", name in TEST_ONLY_REACH_CALLS)
        if name in ("exec", "eval", "compile"):
            self.hit(node, "dynamic_code", f"{name}()")
        if name in ("__import__", "importlib.import_module", "importlib.reload") and not isinstance(constant, str) \
                and not self._anchored(args[0] if args else None):
            self.hit(node, "dynamic_code", f"{name}() of a computed name")
        if name in ("getattr", "setattr", "delattr") and len(args) >= 2:
            target = self.name(args[0])
            if not (isinstance(args[1], ast.Constant) and isinstance(args[1].value, str)):
                if self._rooted(target, SENSITIVE_MODULES):
                    self.hit(node, "dynamic_code", f"{name}({target}, <computed>)")
            elif name != "getattr" and (self._rooted(target, ("unittest",)) or args[1].value in RESULT_CALLBACKS
                                        or args[1].value in HARNESS or args[1].value in IDENTITY_ATTRIBUTES):
                self.hit(node, "unittest_patch", f"{name}({target}, {args[1].value!r})")
            if name != "getattr" and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str) \
                    and args[1].value.startswith(TEST_PREFIX) and not self._on_self(args[0]):
                self.hit(node, "test_replaced", f"{name}({target or ast.unparse(args[0])}, {args[1].value!r})")
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
            for attribute in self._patched_attributes(name, node):
                if attribute.startswith(TEST_PREFIX):
                    self.hit(node, "test_replaced", f"{name}(..., {attribute!r})")
        self.generic_visit(node)


def integrity_findings(path: str, source: str | None, added: set) -> list:
    """High findings for the lines ``added`` to Python file ``path`` (content ``source``; ``added`` None: all lines).

    Test code is held to every family. Any other Python
    file is held to ``HARNESS_RULES``, without the test-only parts of ``runner_reach``.
    """
    test = is_test_code(path)
    what = "test file" if test else "Python file"
    if added is not None and not added:
        return []
    if source is None:
        return [{"severity": "high", "file": path, "line": None,
                 "claim": f"a changed {what} could not be read at the candidate commit, so the test-integrity "
                          "rule did not run on it",
                 "falsifier": "run the critic where `git show <commit>:<path>` works (the candidate's worktree)"}]
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # It cannot import under this interpreter either, but the rule did not read it, so it does not pass it.
        return [{"severity": "high", "file": path, "line": None,
                 "claim": f"a changed {what} does not parse, so the test-integrity rule could not read it",
                 "falsifier": f"a {what} that parses under the host's Python"}]
    scan = _Scan()
    scan.bind_imports(tree)
    scan.visit(tree)
    findings, seen = [], set()
    for first, last, rule, detail, test_only in sorted(scan.hits):
        if test_only and not test:
            continue
        line = first if added is None else next((n for n in range(first, last + 1) if n in added), None)
        if line is None or (line, rule) in seen:
            continue
        seen.add((line, rule))
        if test:
            claim = f"test integrity ({rule}): {detail}: test code {TEST_INTEGRITY[rule]}"
            falsifier = ("remove the construct, or show that it never runs inside the test process "
                         "(a string, or a child process the test starts and checks)")
        else:
            claim = (f"test integrity ({rule}): {detail}: a test can import this file, and its code "
                     f"{TEST_INTEGRITY[rule]}")
            falsifier = ("remove the construct, or show that it never runs inside the test process "
                         "(a string, or code only a child process runs)")
        findings.append({"severity": "high", "file": path, "line": line, "claim": claim, "falsifier": falsifier})
    return findings


def compiled_findings(request: dict, paths: list) -> list:
    """High findings for compiled modules the candidate adds or changes: Python imports them, this rule cannot read them."""
    commit = (request.get("candidate") or {}).get("commit")
    in_repo = bool(commit) and _git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"]) is not None
    found = []
    for path in paths:
        if in_repo and _git(["cat-file", "-e", f"{commit}:{path}"]) is None:
            continue  # deleted by the candidate
        found.append({"severity": "high", "file": path, "line": None,
                      "claim": "the candidate adds or changes a compiled module that Python can import and the "
                               "test-integrity rule cannot read",
                      "falsifier": "the module's source instead, or show that nothing in the test process imports it"})
    return found


# --- added lines of any file -------------------------------------------------------------------


def _in_repository(request: dict) -> bool:
    commit = (request.get("candidate") or {}).get("commit")
    return bool(commit) and _git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"]) is not None


def _added_lines(request: dict) -> dict:
    """``{path: {line number: text}}`` for every line the candidate adds, to any file: from Git when the candidate
    commit is in the repository the critic runs in (``git diff -U0 <base> <commit>``), else from the request's diff."""
    commit, base = (request.get("candidate") or {}).get("commit"), request.get("base")
    if base and _in_repository(request):
        diff = _git(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "-U0", base, commit])
        if diff is not None:
            return {path: lines for path, (_, lines) in _hunks(diff).items()}
    return {path: lines for path, (_, lines) in _hunks(request.get("diff") or "").items()}


# --- the secret-added rule -----------------------------------------------------------------------


def _generated(body: str) -> bool:
    """A value a key generator writes and a person rarely types: twenty or more characters after the prefix, with
    both lower-case and upper-case letters. (An ``AKIA`` id is upper case throughout and never counts: it is an
    identifier, and the secret that goes with it has no prefix to match.)"""
    return len(body) >= GENERATED_LENGTH and any(c.islower() for c in body) and any(c.isupper() for c in body)


def secret_shapes(text: str) -> dict:
    """``{shape: looks generated?}`` for the credential shapes in ``text``, named and never quoted: a key prefix
    that is not glued to a preceding letter or digit (the rule of ``syberlabs._patterns.credential_prefixes``),
    and ``private_key`` for a PEM header, which looks generated when key material follows it on the line."""
    found: dict = {}
    position = 0
    while True:
        match = SECRET.search(text, position)
        if match is None:
            break
        if match.start() == 0 or not text[match.start() - 1].isalnum():
            prefix = match.group(1)
            found[prefix] = found.get(prefix, False) or _generated(match.group(0)[len(prefix):])
            position = match.end()
        else:
            position = match.start() + 1
    header = PRIVATE_KEY.search(text)
    if header:
        found["private_key"] = bool(KEY_MATERIAL.search(text, header.end()))
    return dict(sorted(found.items()))


def secret_findings(added: dict) -> list:
    """Findings for added lines that hold the shape of a credential: high when the value looks generated (or a
    private-key header is followed by key material, on its line or the next added one), low when it has the shape
    and looks typed by hand, which is what a fixture looks like. The claim names the shape; the value is never
    copied into the document, which is stored in the thread's chain."""
    found = []
    for path in sorted(added):
        for number in sorted(added[path]):
            shapes = secret_shapes(added[path][number])
            if not shapes:
                continue
            if shapes.get("private_key") is False and KEY_MATERIAL.search(added[path].get(number + 1, "")):
                shapes["private_key"] = True
            if any(shapes.values()):
                severity, looks = "high", "a value in the shape of a credential that looks generated"
                falsifier = ("the value removed from the candidate (and revoked, if it was real), or a fixture that "
                             "is not a credential: one built from parts, so that no line holds the shape")
            else:
                severity, looks = "low", "the shape of a credential and looks typed by hand, as a fixture does"
                falsifier = "nothing to do if it is a fixture; a real value is removed and revoked"
            found.append({"severity": severity, "file": path, "line": number,
                          "claim": f"secret added ({', '.join(shapes)}): a line the candidate adds holds {looks}; "
                                   "the value is not repeated here", "falsifier": falsifier})
    return found


# --- the test-weakened rule ----------------------------------------------------------------------


def _marker(names: _Scan, decorator) -> str | None:
    """``always``, ``sometimes`` or ``expected`` for a decorator that changes what a test's result means
    (``unittest.skip``, ``pytest.mark.skipif``, ``unittest.expectedFailure``, ...), None for any other."""
    last = (names.name(decorator.func if isinstance(decorator, ast.Call) else decorator) or "").rsplit(".", 1)[-1]
    return "always" if last in SKIP_ALWAYS else "sometimes" if last in SKIP_SOMETIMES else \
        "expected" if last in EXPECTED_TO_FAIL else None


def _skip_calls(names: _Scan, node) -> int:
    """How many places under ``node`` skip the running test: ``.skipTest(...)``, ``pytest.skip(...)``, ``raise SkipTest``."""
    count = 0
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if (isinstance(func, ast.Attribute) and func.attr == "skipTest") or names.name(func) in SKIP_CALLS:
                count += 1
        elif isinstance(child, ast.Raise) and child.exc is not None:
            raised = child.exc.func if isinstance(child.exc, ast.Call) else child.exc
            if (names.name(raised) or "").rsplit(".", 1)[-1] == "SkipTest":
                count += 1
    return count


def _is_assertion(names: _Scan, node) -> bool:
    """A statement or call that can fail a test: ``assert``, ``.assert...(...)``, ``self.fail(...)``, a function
    named ``assert...``, ``pytest.raises`` and its kin."""
    if isinstance(node, ast.Assert):
        return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and (func.attr.startswith("assert") or (func.attr == "fail" and _Scan._on_self(func.value))):
        return True
    if isinstance(func, ast.Name) and func.id.startswith("assert"):
        return True
    return names.name(func) in ASSERTING_CALLS


def _swallows(names: _Scan, handler: ast.ExceptHandler) -> bool:
    """A handler that catches a failed assertion and lets the test go on: bare, or for ``AssertionError``,
    ``Exception`` or ``BaseException``, with no ``raise`` and no assertion of its own in its body."""
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    if handler.type is not None and not any((names.name(kind) or "").rsplit(".", 1)[-1] in SWALLOWED for kind in kinds):
        return False
    return not any(isinstance(node, ast.Raise) or _is_assertion(names, node) for part in handler.body for node in ast.walk(part))


def _suppresses(names: _Scan, expression) -> bool:
    """``contextlib.suppress(AssertionError)`` and wider: the block's failed assertions are dropped."""
    if not isinstance(expression, ast.Call) or (names.name(expression.func) or "").rsplit(".", 1)[-1] != "suppress":
        return False
    return any((names.name(arg) or "").rsplit(".", 1)[-1] in SWALLOWED for arg in expression.args)


def _assertions(names: _Scan, node) -> int:
    """The assertions under ``node`` that can fail the test. Not counted: those in a ``try`` whose handler
    swallows them, under ``contextlib.suppress`` of the same, or under a constant-false ``if``."""
    if isinstance(node, ast.Assert):
        return 1
    if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
        kept = [] if any(_swallows(names, handler) for handler in node.handlers) else list(node.body)
        parts = kept + [part for handler in node.handlers for part in handler.body] + node.orelse + node.finalbody
        return sum(_assertions(names, part) for part in parts)
    if isinstance(node, ast.If) and isinstance(node.test, ast.Constant) and not node.test.value:
        return sum(_assertions(names, part) for part in node.orelse)
    if isinstance(node, (ast.With, ast.AsyncWith)) and any(_suppresses(names, item.context_expr) for item in node.items):
        return 0
    own = 1 if _is_assertion(names, node) else 0
    return own + sum(_assertions(names, child) for child in ast.iter_child_nodes(node))


def _test_assertions(names: _Scan, test) -> int:
    """The assertions of one test function. Nothing after a ``return`` at the top of its body runs, so nothing
    after one counts."""
    total = 0
    for statement in test.body:
        if isinstance(statement, ast.Return):
            break
        total += _assertions(names, statement)
    return total


def tests_of(source: str) -> dict | None:
    """``{qualified name: {"line", "skips", "always", "assertions"}}`` for the tests a Python source defines
    (functions named ``test...`` at module level or in a class, not those nested in a function); None when the
    source does not parse.

    ``skips`` counts every marker in force for the test: its own decorators and those of the classes around it
    (a skip, a conditional skip, an expected failure), skip calls in its body, in its class's
    ``setUp``/``setUpClass`` and in ``setUpModule``, and a class that sets ``__unittest_skip__``. ``always`` says
    an unconditional skip is among them: the test never runs."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    names = _Scan()
    names.bind_imports(tree)
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    found: dict = {}

    def walk(body, prefix, inherited, always):
        for node in body:
            if isinstance(node, ast.ClassDef):
                marks = [_marker(names, decorator) for decorator in node.decorator_list]
                fixtures = sum(_skip_calls(names, item) for item in node.body
                               if isinstance(item, functions) and item.name in SKIPPING_FIXTURES)
                flags = sum(1 for item in node.body if isinstance(item, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id in SKIP_ATTRIBUTES for target in item.targets)
                            and not (isinstance(item.value, ast.Constant) and not item.value.value))
                walk(node.body, f"{prefix}{node.name}.", inherited + sum(1 for mark in marks if mark) + fixtures + flags,
                     always or "always" in marks or bool(flags))
            elif isinstance(node, functions) and node.name.startswith(TEST_PREFIX):
                marks = [_marker(names, decorator) for decorator in node.decorator_list]
                found[prefix + node.name] = {"line": node.lineno, "always": always or "always" in marks,
                                             "skips": inherited + sum(1 for mark in marks if mark) + _skip_calls(names, node),
                                             "assertions": _test_assertions(names, node)}

    module_fixture = sum(_skip_calls(names, node) for node in tree.body if isinstance(node, functions) and node.name == "setUpModule")
    walk(tree.body, "", module_fixture, False)
    return found


def _test_sides(request: dict, paths: list) -> tuple:
    """``(base, candidate, compared)``: the tests of each changed Python test file on both sides, ``{path:
    tests_of(...)}``. From Git when the candidate commit is in the repository (``git show <base>:<path>`` and
    ``git show <commit>:<path>``); a path one side lacks is absent on that side. Without a repository only a new
    file can be rebuilt from the request's diff, and it has no base side. ``compared`` is False when the base
    commit could not be read, so nothing was compared. A candidate file that does not parse is left out: the
    integrity rule reports it."""
    commit, base = (request.get("candidate") or {}).get("commit"), request.get("base")
    before, after = {}, {}
    if not _in_repository(request):
        for path, (source, _) in _sources(request, paths).items():
            tests = tests_of(source) if source is not None else None
            if tests is not None:
                after[path] = tests
        return before, after, True
    if not base or _git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"]) is None:
        return before, after, False
    for path in paths:
        new, old = _git(["show", f"{commit}:{path}"]), _git(["show", f"{base}:{path}"])
        tests = tests_of(new) if new is not None else {}
        if tests is None:
            continue
        if new is not None:
            after[path] = tests
        if old is not None:
            before[path] = tests_of(old) or {}
    return before, after, True


def _listed(names: list) -> str:
    return ", ".join(names[:MAX_NAMES]) + (f" and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else "")


def weakened_findings(before: dict, after: dict, severity: str = "medium") -> list:
    """Findings for tests the candidate removed, newly skipped, or left asserting less. ``before`` and ``after``
    are ``{path: tests_of(...)}`` for the changed test files on the base and in the candidate.

    A test is the same test when its file and qualified name are. One that kept its method name under another
    class or in another changed file moved, and one whose name in the same file is ``RENAMED`` alike was
    renamed; both are compared with what they became. One finding per file and rule."""
    missing, new, pairs = [], [], []
    for path in sorted(set(before) | set(after)):
        old, now = before.get(path) or {}, after.get(path) or {}
        for name in old:
            if name in now:
                pairs.append((path, name, old[name], now[name]))
            else:
                missing.append((path, name))
        new += [(path, name) for name in now if name not in old]
    for entry in list(missing):
        path, name = entry
        method = name.rsplit(".", 1)[-1]
        moved = next((item for item in sorted(new, key=lambda item: (item[0] != path, item))
                      if item[1].rsplit(".", 1)[-1] == method), None)
        if moved is None:  # renamed in place: the most alike new name in the same file, if it is alike enough
            alike = [(difflib.SequenceMatcher(None, method, item[1].rsplit(".", 1)[-1]).ratio(), item)
                     for item in new if item[0] == path]
            ratio, moved = max(alike, key=lambda pair: (pair[0], pair[1]), default=(0.0, None))
            moved = moved if ratio >= RENAMED else None
        if moved is not None:
            missing.remove(entry)
            new.remove(moved)
            pairs.append((moved[0], moved[1], before[path][name], after[moved[0]][moved[1]]))
    grouped: dict = {}
    for path, name in missing:
        grouped.setdefault(("test_removed", path), []).append((0, name))
    for path, name, old, now in pairs:
        if now["skips"] > old["skips"]:
            grouped.setdefault(("test_skipped", path), []).append((now["line"], name))
        if now["assertions"] < old["assertions"]:
            grouped.setdefault(("assertions_removed", path), []).append((now["line"], f"{name} ({old['assertions']} to {now['assertions']})"))
    for path, name in new:
        if after[path][name]["always"]:
            grouped.setdefault(("test_skipped", path), []).append((after[path][name]["line"], f"{name} (new)"))
    found = []
    for (rule, path), entries in sorted(grouped.items(), key=lambda pair: (pair[0][1], list(WEAKENED).index(pair[0][0]))):
        entries.sort()
        claim = WEAKENED[rule].format(count=len(entries), names=_listed([name for _, name in entries]))
        found.append({"severity": severity, "file": path, "line": entries[0][0] or None,
                      "claim": f"test weakened ({rule}): {claim}", "falsifier": WEAKENED_FALSIFIERS[rule]})
    return found


def script_skip_findings(added: dict, severity: str = "medium") -> list:
    """Findings for a skip marker on a line added to a JavaScript or TypeScript test file (``it.skip(``,
    ``describe.only(``, ``xit(``, ...). Matched as text: this file parses Python only, so a marker inside a string
    or a comment matches too, and the falsifier says so."""
    found = []
    for path in sorted(added):
        if not is_script_test(path):
            continue
        for number in sorted(added[path]):
            match = SCRIPT_SKIP.search(added[path][number])
            if match:
                marker = re.sub(r"\s+", "", match.group(0)).rstrip("(")
                found.append({"severity": severity, "file": path, "line": number,
                              "claim": "test weakened (test_skipped_by_text): "
                                       + WEAKENED["test_skipped_by_text"].format(marker=marker),
                              "falsifier": WEAKENED_FALSIFIERS["test_skipped_by_text"]})
    return found


def review(request: dict, *, block_weakened: bool = False) -> dict:
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
    python_code = [p for p in changed if is_python_code(p)]
    for path, (source, added) in _sources(request, python_code).items():
        integrity += integrity_findings(path, source, added)
    integrity += compiled_findings(request, [p for p in changed if p.endswith(PYTHON_COMPILED)])
    findings += integrity[:MAX_INTEGRITY_FINDINGS]
    added = _added_lines(request)
    secrets = secret_findings({path: lines for path, lines in added.items() if path in changed})
    findings += secrets[:MAX_SECRET_FINDINGS]
    severity = "high" if block_weakened else "medium"
    before, after, compared = _test_sides(request, [p for p in changed if is_test_code(p)])
    weakened = weakened_findings(before, after, severity)
    weakened += script_skip_findings({path: lines for path, lines in added.items() if path in changed}, severity)
    findings += weakened[:MAX_WEAKENED_FINDINGS]
    summary = (f"{len(findings)} finding(s) over {len(changed)} changed path(s)" if findings
               else f"no findings over {len(changed)} changed path(s)")
    if len(integrity) > MAX_INTEGRITY_FINDINGS:
        summary += f"; {len(integrity) - MAX_INTEGRITY_FINDINGS} more test-integrity finding(s) not listed"
    if len(secrets) > MAX_SECRET_FINDINGS:
        summary += f"; {len(secrets) - MAX_SECRET_FINDINGS} more secret-added finding(s) not listed"
    if len(weakened) > MAX_WEAKENED_FINDINGS:
        summary += f"; {len(weakened) - MAX_WEAKENED_FINDINGS} more test-weakened finding(s) not listed"
    if not compared:
        summary += ("; the base commit could not be read: no test was compared with its base version, and added lines "
                    "were taken from the request's diff")
    reviewer = {**REVIEWER, "revision": BLOCKING_REVISION} if block_weakened else dict(REVIEWER)
    return {"version": 1, "reviewer": reviewer, "findings": findings, "summary": summary}


def main(argv: list | None = None) -> int:
    options = sys.argv[1:] if argv is None else argv
    if any(option != BLOCK_WEAKENED for option in options):
        print(f"critic_rules: the only option is {BLOCK_WEAKENED}", file=sys.stderr)
        return 2
    try:
        request = json.load(sys.stdin)
    except ValueError:
        print("critic_rules: stdin is not a JSON request", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(review(request, block_weakened=BLOCK_WEAKENED in options), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
