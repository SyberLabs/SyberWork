"""The lookup router: between attempts, the factory decides what the next call gets (``docs/ROUTER.md``). Research.

    syberlabs propose --repair "python adapter.py" --router      # opt-in; the default proposer path is unchanged
    syberlabs propose --claude-code --bare --router              # the same, with Claude Code as the call

The repair loop (``providers.RepairProvider``, the factory's default proposer since 2026-10-23) answers every failed
attempt the same way: the next call gets the host's check results and the failed candidate's files. The router puts
one decision between attempts, read from the host's own evaluation of the attempt, and records it before it acts:

- ``repair``: the next call gets the failed check's output, as in the repair loop;
- ``lookup``: the host looks the library up in GitHits at its installed version through its own client
  (``syberlabs.githits``, used as a library: its exclusions, license policy and signed records), and the next call
  gets the failed check's output and what GitHits returned;
- ``web``: the host runs a configured web search command (a process seam; none is built in) and the next call gets
  what it returned;
- ``stop``: the search ends: no call or budget is left, or the same failure came back with no better score.

The first call of a search has nothing to route from and is recorded as ``propose``; every later call follows a
decision.

Who decides. ``BaselinePolicy`` is deterministic: an ordered list of named rules (``RULES``), the first that matches
decides, as admission is one ordered list of rules. ``stop.budget`` and ``stop.calls`` are the router's own and come
first. ``Decider`` is the seam for a small decider model: ``CommandDecider`` runs one with JSON on stdin and stdout
under the host's meter, like any model call, in an environment without the GitHits variables. A decider's choice
stands only when its action is eligible in the situation; otherwise the baseline's stands and the refusal is
recorded. ``holdout`` replaces a lookup the policy chose with a repair at a seeded random rate and records it, so
the value of a lookup can be read against its absence.

The diagnosis (``diagnose``) reads the failed checks' output tails as the host recorded them: the last Python
exception line and the traceback frames before it (CPython's format; pytest's short and long formats in part). A
frame is the repository's when its path ends with an in-scope file, third-party when it lies under a
``site-packages`` or ``dist-packages`` directory, the standard library's when it lies under the interpreter's
library. An ``ImportError``, ``ModuleNotFoundError``, ``AttributeError`` or ``TypeError`` names a module, or names a
symbol that a third-party frame raised or that the failing repository file binds with an import (read with ``ast``,
never run). That module is the repository's own (``repo``: repair), the standard library's (``stdlib``: repair), an
installed distribution (``third_party``: a GitHits lookup at its version), installed nowhere (``missing_dependency``:
a web search), or not known (``unknown``: repair). The installed version comes from the ``*.dist-info`` beside the
traceback's own ``site-packages`` frames (the environment the check ran in: ``traceback_site``), else from the host
interpreter's metadata (``host_metadata``), else it is unavailable and GitHits gets no lookup, since an unpinned
target would answer for another version; the evidence the model reads says where the version came from
(``CLAUDE.md``: never present another version as the installed one).

What is journaled, through ``SearchSpace.observe`` as verified host observations:

- ``route``: one per decision, written before anything it decides runs, versioned ``<search>.r<n>``: the action, the
  deciding rule, a reason built from a closed vocabulary and package names, the eligible actions, the decider's note,
  the holdout draw, the diagnosis as names, counts and digests (never output text, query text or file content), the
  scores and repeats it read, the lookup's target, query digest and length, the evidence the next call carries over
  from earlier lookups, and the budget left;
- ``lookup``: one per GitHits call, built from the client's own signed record as the Claude Code route builds it
  (``claude_code.record_lookup``: tool ``githits.<op>``), versioned ``<search>.r<n>.<k>``, so
  ``metrics.githits_lookups`` counts the router's lookups as host-observed; one per web search, tool ``router.web``.

``attribution(events)`` is a pure function of the journal, like ``metrics.work_items``: one row per decision with the
attempt that followed it (passed, the triggering check fixed, the same failure again, the score it gained), what that
attempt cost (host-observed dollars and tokens of its call, its evaluation time, the lookup's calls and latency) and
the calls from the decision to the search's first pass. ``summary`` reads the rows per action and per lookup, with
the randomized holdout comparison when one ran.

Claim ceiling: implemented and tested against stand-ins and a loopback fake GitHits. Nothing here has run against a
model or the real GitHits API, and nothing is measured.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import random
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from syberlabs import githits, metrics, query_guard
from syberlabs.canonical import canonical
from syberlabs.claude_code import DROPPED_ENV, githits_variable, record_lookup
from syberlabs.errors import Rejected
from syberlabs.providers import (RepairProvider, current_meter, model_usage, refuse, run_json, split_command,
                                 tool_usage, with_usage)
from syberlabs.search import CheckResult, SearchSpace

ROUTE = "route"            # observed: one router decision, versioned <search>.r<n>
LOOKUP = "lookup"          # observed: one lookup call (claude_code.LOOKUP), versioned <search>.r<n>.<k>
WEB_TOOL = "router.web"    # the tool of a lookup fact for the router's own web search
PROPOSE = "propose"        # the first call of a search: nothing evaluated yet to route from
ACTIONS = ("repair", "lookup", "web", "stop")
LOOKUP_ACTIONS = ("lookup", "web")
CALL_ACTIONS = (PROPOSE, "repair", "lookup", "web")  # each precedes exactly one model call
KINDS = ("third_party", "repo", "stdlib", "missing_dependency", "unknown")
LOOKUP_ERRORS = ("ImportError", "ModuleNotFoundError", "AttributeError", "TypeError")
# The order decisions are taken in: the router's two hard stops, then the baseline policy's rules.
RULES = ("stop.budget", "stop.calls", "stop.no_progress", "lookup.third_party", "web.missing_dependency",
         "web.third_party", "repair.repo", "repair.default")
PROTOCOL = "syberlabs.route/v0alpha1"     # a decider command's request
WEB_PROTOCOL = "syberlabs.web/v0alpha1"   # a web search command's request
AGENT = "syberfactory/router"             # X-GitHits-Agent of the router's lookups
DEFAULT_MAX_LOOKUPS = 2                   # lookup decisions (GitHits and web together) per search
DEFAULT_PATIENCE = 2                      # the same failure may come back this many times before stop.no_progress
MAX_EVIDENCE_CHARS = 6000                 # of one lookup's text in the next request
MAX_REASON, MAX_NAME, MAX_ERROR_LINE = 200, 64, 300
MAX_COMMAND_BYTES = 1024 * 1024
READ_SPAN = 60                            # lines read at the top hit when the hit gives no span
MAX_TREE_FILES = 500                      # in-scope base files the query guard's tree rule reads per search
HOLDOUT_SALT = "syberlabs.router.holdout"
# The few words a query adds after the package, its version and the symbol, by the failure's class.
PHRASE = {"ImportError": "import name", "ModuleNotFoundError": "module name", "AttributeError": "attribute",
          "TypeError": "signature parameters"}

# ------------------------------------------------------------------------------------------------------- the failure

_TRACEBACK = re.compile(r"^(?:E\s+)?Traceback \(most recent call last\):\s*$")
_CHAINED = ("During handling of the above exception", "The above exception was the direct cause")
_SEPARATOR = re.compile(r"^\s*(?:={5,}|-{5,}|_{5,})")  # unittest's and pytest's section rules
_FRAME = re.compile(r'^\s*(?:E\s+)?File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+?))?\s*$')
_PYTEST_FRAME = re.compile(r"^(?P<path>[^\s:][^:]*\.py):(?P<line>\d+):(?: in (?P<func>\S+))?\s*"
                           r"(?P<error>[A-Za-z_]\w*)?\s*$")
_PYTEST_SOURCE = re.compile(r"^>\s+(?P<source>\S.*)$")
_EXCEPTION = re.compile(r"^(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?::(?P<message>.*))?$")
_CARETS = re.compile(r"^\s*[~^\s]+$")
_ENDINGS = ("Error", "Exception", "Exit", "Interrupt", "Warning")
_BARE = ("StopIteration", "StopAsyncIteration", "GeneratorExit")
_NOT_A_CLASS = ("Error", "Warning")  # tool output says "Error: ..."; no Python exception has these bare names

_NO_MODULE = re.compile(r"^No module named '(?P<module>[A-Za-z_][\w.]*)'")
_CANNOT_IMPORT = re.compile(r"^cannot import name '(?P<name>\w+)' from (?:partially initialized module )?"
                            r"'(?P<module>[A-Za-z_][\w.]*)'(?: \((?P<path>[^)]*)\))?")
_MODULE_ATTR = re.compile(r"^(?:partially initialized )?module '(?P<module>[A-Za-z_][\w.]*)' has no attribute "
                          r"'(?P<name>\w+)'")
_OBJECT_ATTR = re.compile(r"^'(?P<owner>[A-Za-z_][\w.]*)' object has no attribute '(?P<name>\w+)'")
_TYPE_ATTR = re.compile(r"^type object '(?P<owner>[A-Za-z_][\w.]*)' has no attribute '(?P<name>\w+)'")
_CALL = re.compile(r"^(?P<callable>[A-Za-z_][\w.]*)\(\) (?:got an unexpected keyword argument|got multiple values"
                   r"|missing \d+ required|takes (?:from )?\d+|takes no|takes exactly|takes at (?:most|least))")

_SITE = re.compile(r"^(?P<site>.*/(?:site|dist)-packages)/(?P<rest>[^/]+)")
_STDLIB_DIRS = tuple(sorted({path.replace("\\", "/").rstrip("/") + "/"
                             for path in (sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib"))
                             if path}))
_STDLIB_PATH = re.compile(r"/lib/python3(?:\.\d+)?/|^[A-Za-z]:/python3\d*/lib/", re.IGNORECASE)
_HEX = re.compile(r"0x[0-9A-Fa-f]+")
_PATHISH = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s'\"()\[\]:,]+){2,}")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b")


@dataclass(frozen=True)
class Failure:
    """The last Python exception in a check's output and the frames of its traceback, innermost last."""

    error: str                                     # the class, last dotted component (``AttributeError``)
    message: str                                   # its message, first line
    frames: tuple[tuple[str, int, str, str], ...]  # (path as printed, line, function, source line)
    line: str                                      # the exception line as printed, bounded


def _exception_line(line: str) -> tuple[str, str] | None:
    """``(class, message)`` for an exception line at column 0 (or after pytest's ``E``); None for anything else."""
    if not line or line[:1].isspace():
        return None
    text = line.rstrip()
    if re.match(r"^E\s", text):
        text = text[1:].lstrip()
    found = _EXCEPTION.match(text)
    if not found:
        return None
    last = found["name"].rsplit(".", 1)[-1]
    if last in _NOT_A_CLASS or not (last.endswith(_ENDINGS) or last in _BARE):
        return None
    return last, (found["message"] or "").strip()


def parse_failure(text: str) -> Failure | None:
    """The last exception in ``text`` with the traceback frames printed before it (the frames of an earlier,
    chained or unrelated traceback are not its own), or None when the output holds no Python exception."""
    lines = (text or "").splitlines()
    at = None
    for index in range(len(lines) - 1, -1, -1):
        found = _exception_line(lines[index])
        if found is not None:
            at, (error, message) = index, found
            break
    if at is None:
        return None
    frames: list[list] = []
    source = ""
    for line in lines[:at]:
        if _TRACEBACK.match(line) or _SEPARATOR.match(line) or line.startswith(_CHAINED):
            frames, source = [], ""
            continue
        pytest_source = _PYTEST_SOURCE.match(line)
        if pytest_source:
            source = pytest_source["source"].strip()  # pytest's long format prints the source before its location
            continue
        found = _FRAME.match(line) or _PYTEST_FRAME.match(line)
        if found:
            frames.append([found["path"], int(found["line"]), (found["func"] or "").strip(), source[:200]])
            source = ""
        elif frames and not frames[-1][3] and line[:1].isspace() and line.strip() and not _CARETS.match(line):
            frames[-1][3] = line.strip()[:200]
    for line in lines[at + 1:at + 12]:  # pytest's long format names the innermost frame after its E lines
        found = _PYTEST_FRAME.match(line)
        if found and found["error"] == error:
            frames.append([found["path"], int(found["line"]), "", source[:200]])
            break
    return Failure(error, message[:500], tuple(tuple(item) for item in frames[-32:]),
                   lines[at].strip()[:MAX_ERROR_LINE])


@dataclass(frozen=True)
class Frame:
    """One traceback frame, classified. ``path`` is the in-scope path of a repository frame, the path below the
    site directory of a third-party frame, else the path as printed."""

    where: str                 # repo | third_party | stdlib | unknown
    path: str
    line: int
    function: str
    source: str
    package: str | None = None  # third_party: its top-level package
    site: str | None = None     # third_party: its site-packages directory, as printed


def classify_frame(path: str, line: int, function: str, source: str, files: frozenset[str] | set[str]) -> Frame:
    norm = path.replace("\\", "/")
    found = _SITE.match(norm)
    if found:
        rest = found["rest"]
        package = rest[:-3] if rest.endswith(".py") else rest
        return Frame("third_party", norm[len(found["site"]) + 1:][:200], line, function, source,
                     package=package if package.isidentifier() else None, site=path[:len(found["site"])])
    if norm.startswith("<"):
        return Frame("stdlib", norm[:200], line, function, source)
    parts = norm.split("/")
    for start in range(len(parts)):
        suffix = "/".join(parts[start:])
        if suffix in files:
            return Frame("repo", suffix, line, function, source)
    if any(norm.startswith(prefix) for prefix in _STDLIB_DIRS) or _STDLIB_PATH.search(norm):
        return Frame("stdlib", norm[:200], line, function, source)
    return Frame("unknown", norm[:200], line, function, source)


def repo_modules(paths: Iterable[str]) -> frozenset[str]:
    """The top-level names the repository's own files make importable: every directory name and every ``.py``
    file's stem, outside ``site-packages`` and ``dist-packages`` (vendored distributions are third-party)."""
    names: set[str] = set()
    for path in paths:
        parts = [part for part in str(path).replace("\\", "/").split("/") if part]
        if not parts or any(part in ("site-packages", "dist-packages") for part in parts):
            continue
        names.update(part for part in parts[:-1] if part.isidentifier())
        if parts[-1].endswith(".py") and parts[-1][:-3].isidentifier():
            names.add(parts[-1][:-3])
    return frozenset(names)


def import_bindings(text: str | None) -> dict[str, str]:
    """The names a Python file binds by importing them, each to the top-level module it comes from (``"." + name``
    for a relative import). Parsed with ``ast``; nothing is run. Empty for text that does not parse."""
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return {}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                found[alias.asname or top] = top
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "").split(".")[0]
            top = "." * node.level + base if node.level else base
            for alias in node.names:
                if alias.name != "*" and top:
                    found[alias.asname or alias.name] = top
    return found


def _provides(distribution, module: str) -> bool:
    try:
        top = distribution.read_text("top_level.txt")
    except Exception:  # noqa: BLE001  a broken dist-info answers nothing
        top = None
    if top:
        return module in top.split()
    try:
        files = distribution.files or ()
    except Exception:  # noqa: BLE001
        files = ()
    return any(getattr(item, "parts", ()) and item.parts[0] in (module, module + ".py") for item in files)


def resolve_version(module: str, sites: Sequence[str] = ()) -> tuple[str, str, str] | None:
    """``(distribution, version, source)`` for the top-level ``module``: from the ``*.dist-info`` in a
    ``site-packages`` directory the traceback ran from (``traceback_site``: the environment the check ran in), else
    from the host interpreter's metadata (``host_metadata``), else None. Never a declared range, never a guess."""
    for site in list(dict.fromkeys(site for site in sites if isinstance(site, str)))[:4]:
        if not os.path.isdir(site):
            continue
        try:
            for distribution in importlib.metadata.distributions(path=[site]):
                name, version = distribution.metadata["Name"], distribution.version
                if (isinstance(name, str) and name and isinstance(version, str) and version
                        and _provides(distribution, module)):
                    return name[:MAX_NAME], version[:MAX_NAME], "traceback_site"
        except Exception:  # noqa: BLE001  an unreadable site is no answer
            continue
    try:
        names = importlib.metadata.packages_distributions().get(module) or []
        for name in sorted(set(names)):
            return name[:MAX_NAME], importlib.metadata.version(name)[:MAX_NAME], "host_metadata"
    except Exception:  # noqa: BLE001
        pass
    return None


def _digest(text: str | None) -> str | None:
    return hashlib.sha256(query_guard.utf8(text)).hexdigest() if isinstance(text, str) and text else None


def _normal(message: str) -> str:
    """A message as two runs of the same failure print it: addresses, paths and durations replaced."""
    text = _DURATION.sub("<time>", _PATHISH.sub("<path>", _HEX.sub("0x?", message or "")))
    return " ".join(text.split())[:500]


def _name(value) -> str | None:
    return value[:MAX_NAME] if isinstance(value, str) and value and value.isprintable() else None


@dataclass(frozen=True)
class Diagnosis:
    """What the router read from one failed evaluation: the primary failing check and the failure it shows."""

    check: str | None
    state: str | None
    kind: str                            # KINDS
    error: str | None = None             # the exception's class, last component
    module: str | None = None            # the top-level module the failure names
    symbol: str | None = None            # the dotted name it names; held in memory, only its digest is journaled
    distribution: str | None = None
    version: str | None = None
    version_source: str | None = None    # traceback_site | host_metadata
    why: str | None = None               # how the module was found (closed vocabulary)
    frames: Mapping[str, int] = field(default_factory=dict)
    message_digest: str | None = None
    signature: str = ""                  # every failing check's class and normalized message, digested
    error_line: str = ""                 # the exception line, bounded: a decider model may read it; never journaled

    @property
    def pin(self) -> str:
        return "installed" if self.version else "unavailable"

    @property
    def target(self) -> str | None:
        """The GitHits target: ``pypi:<distribution>@<version>``, unpinned when the version is unavailable."""
        if not self.distribution:
            return None
        return f"pypi:{self.distribution}@{self.version}" if self.version else f"pypi:{self.distribution}"

    def need(self) -> str:
        """What a lookup for this failure would answer, as a digest: the same failure is never looked up twice."""
        return hashlib.sha256(query_guard.utf8(canonical([self.module, self.distribution, self.version, self.error,
                                                          self.symbol]))).hexdigest()

    def record(self) -> dict:
        """The journaled form: names, counts and digests; never output text."""
        return {"check": self.check, "state": self.state, "kind": self.kind, "error": self.error,
                "module": self.module, "distribution": self.distribution, "version": self.version,
                "version_source": self.version_source, "pin": self.pin if self.module else None, "why": self.why,
                "frames": dict(self.frames), "symbol_digest": _digest(self.symbol),
                "message_digest": self.message_digest, "signature": self.signature}


def _owner(error: str, message: str) -> tuple[str | None, str | None]:
    """For an ``AttributeError`` or ``TypeError`` that names no module: the name whose module decides (a class or a
    callable's first part) and the dotted symbol the message names."""
    for pattern in (_TYPE_ATTR, _OBJECT_ATTR):
        found = pattern.match(message) if error == "AttributeError" else None
        if found:
            return found["owner"].split(".")[0], f"{found['owner']}.{found['name']}"
    found = _CALL.match(message) if error == "TypeError" else None
    if found:
        return found["callable"].split(".")[0], found["callable"]
    return None, None


def _check_diagnosis(check: CheckResult, *, files: frozenset[str], modules: frozenset[str],
                     read: Callable[[str], str | None], versions: Callable) -> Diagnosis:
    failure = parse_failure(check.output_tail) if check.state in ("failed", "error", "timed_out") else None
    if failure is None:
        return Diagnosis(check.name, check.state, "unknown", why="no Python exception in the output")
    frames = tuple(classify_frame(*item, files) for item in failure.frames)
    counts = {where: sum(1 for item in frames if item.where == where) for where in ("repo", "third_party", "stdlib",
                                                                                     "unknown")}
    error, message = failure.error, failure.message
    sites = [item.site for item in reversed(frames) if item.site]
    module = symbol = why = None
    if error in ("ImportError", "ModuleNotFoundError"):
        found = _NO_MODULE.match(message)
        if found:
            module, symbol, why = found["module"], found["module"], "message"
        else:
            found = _CANNOT_IMPORT.match(message)
            if found:
                module, symbol, why = found["module"], f"{found['module']}.{found['name']}", "message"
                site = _SITE.match((found["path"] or "").replace("\\", "/"))
                if site:
                    sites.insert(0, found["path"][:len(site["site"])])
    elif error == "AttributeError" and _MODULE_ATTR.match(message):
        found = _MODULE_ATTR.match(message)
        module, symbol, why = found["module"], f"{found['module']}.{found['name']}", "message"
    elif error in ("AttributeError", "TypeError"):
        owner, symbol = _owner(error, message)
        if owner:
            raised = frames[-1] if frames else None
            if raised is not None and raised.where == "third_party" and raised.package:
                module, why = raised.package, "raised in a third-party frame"
            else:
                at = next((item for item in reversed(frames) if item.where == "repo"), None)
                bindings = import_bindings(read(at.path)) if at is not None else {}
                attribute = symbol.rsplit(".", 1)[-1] if symbol else ""
                used = re.search(rf"(?<![\w.])([A-Za-z_]\w*)\.{re.escape(attribute)}\s*\(", at.source) \
                    if at is not None and attribute else None
                if owner in bindings:
                    module, why = bindings[owner], "import binding"
                elif used and used[1] in bindings:
                    module, symbol = bindings[used[1]], f"{used[1]}.{attribute}"
                    why = "imported module in the failing line"
    distribution = version = source = None
    if module is None:
        # A lookup-class error whose module could not be traced may be a library's or the repository's: unknown.
        kind = "repo" if counts["repo"] and error not in LOOKUP_ERRORS else "unknown"
    elif module.startswith("."):
        kind = "repo"
    else:
        top = module.split(".")[0]
        module = top
        if top in modules:
            kind = "repo"
        elif top in getattr(sys, "stdlib_module_names", ()):
            kind = "stdlib"
        else:
            resolved = versions(top, sites)
            if resolved:
                kind = "third_party"
                distribution, version, source = (_name(value) for value in resolved)
            elif any(item.where == "third_party" and item.package == top for item in frames):
                kind = "third_party"
            elif error == "ModuleNotFoundError":
                kind = "missing_dependency"
            else:
                kind = "unknown"
    return Diagnosis(check.name, check.state, kind, error=error, module=_name(module), symbol=symbol,
                     distribution=distribution, version=version, version_source=source, why=why, frames=counts,
                     message_digest=_digest(_normal(message)), error_line=failure.line)


def failure_signature(checks: Sequence[CheckResult], found: Sequence[Diagnosis]) -> str:
    """Every failing check's name, state, exit code, exception class and normalized message, digested: equal for
    two attempts that failed the same way, whatever their worktree paths, addresses or timings."""
    items = sorted([check.name, check.state, check.exit_code, item.error, item.message_digest]
                   for check, item in zip(checks, found))
    return hashlib.sha256(query_guard.utf8(canonical(items))).hexdigest()


def diagnose(checks: Sequence[CheckResult], *, required: Sequence[str] = (), files: Iterable[str] = (),
             read: Callable[[str], str | None] | None = None, versions: Callable = resolve_version) -> Diagnosis:
    """The router's reading of one evaluation. Every failing check is read (required checks first, then by name);
    the primary one is the first whose failure is a third-party library's, else the first with a Python exception,
    else the first. ``files`` are the in-scope repository paths (frames are matched against them), ``read`` gives a
    repository file's text (for its imports), ``versions`` resolves a module's installed distribution."""
    failing = sorted((check for check in checks if check.state != "passed"),
                     key=lambda check: (check.name not in required, check.name))
    if not failing:
        return Diagnosis(None, None, "unknown", signature=failure_signature([], []))
    paths = frozenset(str(path) for path in files)
    modules = repo_modules(paths)
    found = [_check_diagnosis(check, files=paths, modules=modules, read=read or (lambda _path: None),
                              versions=versions) for check in failing]
    primary = next((item for item in found if item.kind == "third_party"), None) \
        or next((item for item in found if item.error), None) or found[0]
    return replace(primary, signature=failure_signature(failing, found))


# ------------------------------------------------------------------------------------------------------ the decision

@dataclass(frozen=True)
class Choice:
    action: str   # propose, or one of ACTIONS
    rule: str     # one of RULES, propose.first or propose.again, decider, or holdout
    reason: str


@dataclass(frozen=True)
class Need:
    """One lookup the router could make: the route, what it answers, what leaves the machine, and the target."""

    route: str           # githits | web
    key: str             # Diagnosis.need(): the same failure is not looked up twice on one route
    query: str           # judged by the query guard before it can be chosen
    target: str | None   # the GitHits target, pinned when the version is known
    version: str | None
    version_source: str | None

    def record(self) -> dict:
        return {"route": self.route, "target": self.target, "pin": "installed" if self.version else "unavailable",
                "query_digest": _digest(self.query), "query_chars": len(self.query), "guard": query_guard.PASS}


@dataclass(frozen=True)
class Situation:
    """What a policy or a decider reads before one decision. No file content and no output text, except the failing
    exception's line (``diagnosis.error_line``, bounded), which a decider model may read and the journal never holds."""

    decision: int                     # this decision's number in the search
    calls: int                        # model calls made in this search
    calls_left: int
    room: bool
    diagnosis: Diagnosis | None
    score: tuple[int, int] | None     # the failed attempt's Evaluation.score()
    best: tuple[int, int] | None      # the best score of the search's failed attempts
    repeats: int                      # trailing evaluated attempts with this failure and this score
    eligible: tuple[str, ...]         # in ACTIONS order
    lookups_used: int
    max_lookups: int
    routes: Mapping[str, str]         # {"githits": "ready" or why not, "web": "ready" or "none"}
    history: tuple[Mapping[str, Any], ...] = ()
    needs: Mapping[str, Need] = field(default_factory=dict)
    blocked: Mapping[str, str] = field(default_factory=dict)  # route -> the query guard's rule
    baseline: Choice | None = None    # the baseline policy's choice, for a decider

    def to_json(self) -> dict:
        diagnosis = None
        if self.diagnosis is not None:
            diagnosis = {**self.diagnosis.record(), "error_line": self.diagnosis.error_line}
        return {"decision": self.decision, "calls": self.calls, "calls_left": self.calls_left, "room": self.room,
                "diagnosis": diagnosis, "score": list(self.score) if self.score else None,
                "best": list(self.best) if self.best else None, "repeats": self.repeats,
                "eligible": list(self.eligible), "lookups_used": self.lookups_used, "max_lookups": self.max_lookups,
                "routes": dict(self.routes), "history": [dict(item) for item in self.history],
                "lookups": {route: need.record() for route, need in self.needs.items()},
                "baseline": {"action": self.baseline.action, "rule": self.baseline.rule} if self.baseline else None}


@runtime_checkable
class Decider(Protocol):
    """Anything that chooses the next action from a ``Situation``: the baseline policy, or a decider model."""

    name: str
    revision: str

    def decide(self, situation: Situation) -> Choice | Mapping[str, Any]:
        ...


class BaselinePolicy:
    """The deterministic baseline. In order, the first rule that holds decides (the router's ``stop.budget`` and
    ``stop.calls`` come before it):

    1. ``stop.no_progress``: the same failure with the same score came back more than ``patience`` times in a row;
    2. ``lookup.third_party``: the failure is a third-party library's and a GitHits lookup is eligible;
    3. ``web.missing_dependency``: the failure names a module installed nowhere and a web search is eligible;
    4. ``web.third_party``: the failure is a third-party library's, GitHits cannot answer it here (no route, no
       installed distribution, already asked, or the guard blocked the query) and a web search is eligible;
    5. ``repair.repo``: the failure is in the repository's own code;
    6. ``repair.default``: anything else (the standard library, an unknown failure, no lookup left).
    """

    name, revision = "baseline", "1"

    def __init__(self, patience: int = DEFAULT_PATIENCE):
        if type(patience) is not int or patience < 1:
            raise Rejected("invalid_provider", "patience must be a positive integer")
        self.patience = patience

    def decide(self, situation: Situation) -> Choice:
        found = situation.diagnosis
        if situation.repeats > self.patience:
            return Choice("stop", "stop.no_progress",
                          f"the same failure came back {situation.repeats} times in a row with no better score")
        if found is None:
            return Choice("repair", "repair.default", "no evaluation to read: repair")
        error = found.error or found.state or "failure"
        if "lookup" in situation.eligible and found.kind == "third_party":
            return Choice("lookup", "lookup.third_party",
                          f"{error} on {found.module} {found.version or '(version unavailable)'}, a third-party "
                          "library: look it up in GitHits at the installed version")
        if "web" in situation.eligible and found.kind == "missing_dependency":
            return Choice("web", "web.missing_dependency",
                          f"{error}: {found.module} is neither installed nor in the repository: search the web")
        if "web" in situation.eligible and found.kind == "third_party":
            return Choice("web", "web.third_party", f"{error} on {found.module}, a third-party library GitHits "
                                                    "cannot answer here: search the web")
        if found.kind == "repo":
            return Choice("repair", "repair.repo",
                          f"{error} in the repository's own code: repair from the check's output")
        return Choice("repair", "repair.default", f"{found.kind} failure ({error}): repair from the check's output")


class FunctionDecider:
    """A Python callable as a decider (for tests and in-process models): ``fn(situation)`` returns a ``Choice`` or
    ``{action, reason}``."""

    def __init__(self, fn: Callable[[Situation], Choice | Mapping[str, Any]], *, name: str, revision: str = "1"):
        self.fn, self.name, self.revision = fn, name, revision
        self.last_usage: dict | None = None

    def decide(self, situation: Situation):
        return self.fn(situation)


def command_environment() -> dict:
    """What a decider or web search command receives: the caller's environment without the GitHits variables
    (``claude_code.githits_variable``: the router holds the GitHits route, the command does not need the token) and
    without the certificate signing seed (``claude_code.DROPPED_ENV``). Not a sandbox: the rest is inherited, as a
    provider command inherits it."""
    return {key: value for key, value in os.environ.items() if not githits_variable(key) and key not in DROPPED_ENV}


def run_command(argv: Sequence[str], payload: dict, *, timeout: float, code: str, meter=None):
    """Run ``argv`` with ``payload`` as JSON on stdin and parse its stdout, as ``providers.run_json`` does (bounded by
    ``timeout`` seconds and ``MAX_COMMAND_BYTES``), in ``command_environment()``. With ``meter`` (a decider model's
    call inside a search) the meter permits the call before it runs and measures its declared usage after, stamped
    ``host_observed``; a web search is not a model call and runs unmetered. Failures are ``Rejected`` with
    ``<code>_unavailable``, ``<code>_timeout``, ``<code>_failed`` or ``<code>_shape``."""
    if meter is not None:
        meter.permit()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    env=command_environment(), start_new_session=hasattr(os, "setsid"))
        except OSError as exc:
            raise Rejected(f"{code}_unavailable", f"could not start {argv[0]!r}: {exc.strerror}") from None
        try:
            proc.stdin.write(json.dumps(payload).encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if time.monotonic() > deadline or os.fstat(out.fileno()).st_size > MAX_COMMAND_BYTES:
                proc.kill()
                proc.wait()
                raise Rejected(f"{code}_timeout", "the command exceeded its time or output limit")
            time.sleep(0.02)
        if proc.returncode != 0:
            raise Rejected(f"{code}_failed", f"exit {proc.returncode}")
        out.seek(0)
        data = out.read(MAX_COMMAND_BYTES + 1)
    if len(data) > MAX_COMMAND_BYTES:
        raise Rejected(f"{code}_shape", "the command's output exceeded its limit")
    try:
        parsed = json.loads(data)
    except ValueError:
        raise Rejected(f"{code}_shape", "the command did not print JSON") from None
    if meter is not None:
        usage = model_usage(parsed)
        meter.measure({**usage, "host_observed": True} if usage else None)
    return parsed


class CommandDecider:
    """A decider model behind the process seam. ``argv`` reads ``{protocol, situation}`` on stdin (``situation`` is
    ``Situation.to_json``: the diagnosis as names and digests plus the exception line, the eligible actions, the
    baseline's choice) and prints ``{"action": ..., "reason": ...}``, with ``usage`` as an adapter reports it. Inside
    a search the host's meter (``providers.current_meter``) permits and prices it like any model call, and its
    declared usage is journaled on the decision. It runs in ``command_environment()``."""

    def __init__(self, argv: Sequence[str], *, name: str = "command", revision: str = "1", timeout: int = 60):
        if not argv or any(not isinstance(arg, str) for arg in argv):
            raise Rejected("invalid_provider", "the decider's argv must be a non-empty list of strings")
        self.argv, self.name, self.revision, self.timeout = list(argv), name, revision, timeout
        self.last_usage: dict | None = None

    def decide(self, situation: Situation) -> Choice:
        self.last_usage = None
        response = run_command(self.argv, {"protocol": PROTOCOL, "situation": situation.to_json()},
                               timeout=self.timeout, code="decider", meter=current_meter())
        self.last_usage = model_usage(response)
        if not isinstance(response, dict):
            raise Rejected("decider_shape", "the decider must print {\"action\": ..., \"reason\": ...}")
        return Choice(str(response.get("action"))[:16], "decider", str(response.get("reason") or "")[:MAX_REASON])


# --------------------------------------------------------------------------------------------------------- the routes

@dataclass(frozen=True)
class Looked:
    text: str           # what the next call reads; empty when nothing usable came back
    records: list       # the client's authentic records of this lookup's calls
    ignored: int        # record lines whose MAC did not verify
    error: str | None   # the client's error code, or a refusal's status


class GitHitsRoute:
    """The router's GitHits lookups: the host's own client (``syberlabs.githits``) in this process. Each lookup
    searches the pinned target and, with ``read_top``, reads the top hit's lines; every call is recorded by the
    client in a fresh record directory under a fresh key and journaled from those records. The exclusion list always
    starts with ``SyberLabs`` (``SYBERLABS_GITHITS_EXCLUDE_REPLACE`` is never honoured here, as the Claude Code route
    never passes it), and the router's own record directory replaces any the environment names."""

    name = "githits"

    def __init__(self, environ: Mapping[str, str], *, session: str | None = None, agent: str = AGENT,
                 read_top: bool = True, limit: int = githits.SEARCH_LIMIT):
        dropped = (githits.EXCLUDE_REPLACE_ENV, githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV)
        self.environ = {key: value for key, value in environ.items() if key not in dropped}
        self.session, self.agent, self.read_top, self.limit = session, agent, read_top, limit

    @classmethod
    def from_env(cls, **kwargs) -> "GitHitsRoute":
        return cls(dict(os.environ), **kwargs)

    def client(self, record_dir=None, record_key: str | None = None, timeout: float | None = None) -> githits.Client:
        return githits.Client.from_env(self.environ, exclude=githits.Exclusions.from_env(self.environ),
                                       record_dir=record_dir, record_key=record_key, session_id=self.session,
                                       agent=self.agent, timeout=timeout)

    def ready(self) -> str | None:
        """None when a lookup can be made; else why not (``no_token``, or the client's setting code)."""
        if not self.environ.get(githits.TOKEN_ENV):
            return "no_token"
        try:
            self.client()
        except githits.GitHitsError as exc:
            return exc.code
        return None

    def lookup(self, need: Need, directory: Path, *, timeout: float) -> Looked:
        directory.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        texts, error = [], None
        try:
            client = self.client(directory, key, timeout)
            found = client.search(need.query, [need.target], limit=self.limit)
            if found.refused:
                error = found.status
            elif found.results_returned:
                texts.append(githits.render(found))
                spot = _top_hit(found, need.target) if self.read_top else None
                if spot is not None:
                    read = client.read(spot[0], **spot[1])
                    if read.refused:
                        error = read.status
                    elif read.data:
                        texts.append(githits.render(read))
        except githits.GitHitsError as exc:
            error = exc.code
        records = githits.read_records(directory, key)
        return Looked("\n\n".join(text for text in texts if text.strip()), records.records, records.ignored, error)


def _top_hit(found: githits.Result, target: str) -> tuple[str, dict] | None:
    """Where to read the top search hit: its documentation page, or its file's lines under the pinned target."""
    hits = ((found.data or {}).get("result") or {}).get("results") or []
    locator = hits[0].get("locator") if hits and isinstance(hits[0], dict) else None
    if not isinstance(locator, dict):
        return None
    if isinstance(locator.get("docs_read_target"), str) and locator["docs_read_target"]:
        return locator["docs_read_target"], {}
    path = locator.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    start = locator.get("start_line") if type(locator.get("start_line")) is int and locator["start_line"] > 0 else 1
    end = locator.get("end_line") if type(locator.get("end_line")) is int else None
    end = start + READ_SPAN - 1 if end is None or end < start else min(end, start + 2 * READ_SPAN)
    return target, {"path": path, "start_line": start, "end_line": end}


@dataclass(frozen=True)
class WebAnswer:
    status: str                 # ok or error
    text: str = ""              # what the next call reads
    results: int = 0
    request_id: str | None = None


@runtime_checkable
class WebRoute(Protocol):
    """A web search the router may run. None is built in: the host has no web client of its own."""

    name: str

    def search(self, query: str, *, timeout: float) -> WebAnswer:
        ...


class CommandWeb:
    """A web search behind the process seam (``--router-web``): ``argv`` reads ``{protocol, query, limit}`` on stdin
    and prints ``{"results": [{"title", "url", "snippet"}], "request_id"}``, in ``command_environment()``, unmetered.
    The query is the router's own, short, and judged by the query guard before it can be chosen; results are
    untrusted text for the next call."""

    name = "web"

    def __init__(self, argv: Sequence[str], *, limit: int = 5):
        if not argv or any(not isinstance(arg, str) for arg in argv):
            raise Rejected("invalid_provider", "the web search argv must be a non-empty list of strings")
        self.argv, self.limit = list(argv), limit

    def search(self, query: str, *, timeout: float) -> WebAnswer:
        try:
            data = run_command(self.argv, {"protocol": WEB_PROTOCOL, "query": query, "limit": self.limit},
                               timeout=timeout, code="web")
        except Rejected:
            return WebAnswer("error")
        results = data.get("results") if isinstance(data, dict) and isinstance(data.get("results"), list) else []
        lines, count = [], 0
        for item in results:
            if not isinstance(item, dict) or count >= self.limit:
                continue
            count += 1
            title, url, snippet = (" ".join(str(item.get(key) or "").split()) for key in ("title", "url", "snippet"))
            lines.append(f"{count}. {title[:200]} {url[:300]}".rstrip())
            if snippet:
                lines.append("   " + snippet[:300])
        request_id = data.get("request_id") if isinstance(data, dict) else None
        return WebAnswer("ok", "\n".join(lines), count, request_id[:128] if isinstance(request_id, str) else None)


def githits_need(found: Diagnosis) -> Need:
    """The GitHits lookup for a third-party failure: a few words naming the package, its version and the symbol, as
    the GitHits skill asks (``skills/githits/SKILL.md``), against the pinned target."""
    name = found.symbol or ""
    if found.module and name.startswith(found.module + "."):
        name = name[len(found.module) + 1:]
    words = [found.module or ""] + ([found.version] if found.version else []) + ([name] if name else []) \
        + [PHRASE.get(found.error or "", "API")]
    return Need("githits", found.need(), " ".join(word for word in words if word), found.target, found.version,
                found.version_source)


def web_need(found: Diagnosis) -> Need:
    """The web search for a failure that names a module: which package provides it, or what its API is."""
    if found.kind == "missing_dependency":
        query = f"python package that provides module {found.module}"
    else:
        name = found.symbol or ""
        if found.module and name.startswith(found.module + "."):
            name = name[len(found.module) + 1:]
        words = ["python", found.module or ""] + ([found.version] if found.version else []) + ([name] if name else []) \
            + [PHRASE.get(found.error or "", "API")]
        query = " ".join(word for word in words if word)
    return Need("web", found.need(), query, None, found.version, found.version_source)


# ---------------------------------------------------------------------------------------------------------- the loop

@dataclass
class RouterConfig:
    """What the router may do. The defaults route nothing outward: no GitHits and no web route unless one is given
    (``GitHitsRoute.from_env()``, a ``WebRoute``), no decider model, two lookups per search, no holdout."""

    githits: GitHitsRoute | None = None
    web: WebRoute | None = None
    decider: Decider | None = None
    policy: Decider = field(default_factory=BaselinePolicy)
    max_lookups: int = DEFAULT_MAX_LOOKUPS
    holdout: float = 0.0
    versions: Callable[[str, Sequence[str]], tuple[str, str, str] | None] = resolve_version


class _State:
    """One search's routing state."""

    def __init__(self, search: str, routes: dict):
        self.search, self.routes = search, routes
        self.decisions = self.lookups = 0
        self.looked_up: set[str] = set()
        self.searched: set[str] = set()
        self.current: Diagnosis | None = None
        self.attempts: list[tuple[str, tuple[int, int]]] = []   # (signature, score) of each failed evaluation
        self.history: list[dict] = []
        self.evidence: list[dict] = []
        self.lines: dict[str, set[str]] = {}  # the query guard's tree digests, by "base" or candidate id

    def observe(self, found: Diagnosis, score: tuple[int, int]) -> None:
        self.current = found
        self.attempts.append((found.signature, tuple(score)))

    @property
    def score(self) -> tuple[int, int] | None:
        return self.attempts[-1][1] if self.attempts else None

    @property
    def best(self) -> tuple[int, int] | None:
        return max(score for _signature, score in self.attempts) if self.attempts else None

    def repeats(self) -> int:
        if not self.attempts:
            return 0
        last, count = self.attempts[-1], 0
        for item in reversed(self.attempts):
            if item != last:
                break
            count += 1
        return count

    def carried(self) -> list[dict]:
        """Evidence from earlier lookups for the failure being repaired: it rides on every call while that failure
        (its signature) is the current one, and is dropped once the failure changes."""
        if self.current is None:
            return []
        return [item for item in self.evidence if item["signature"] == self.current.signature]

    def outcome(self, value: str) -> None:
        if self.history:
            self.history[-1]["outcome"] = value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[truncated by the host]"


class RouterMixin:
    """The repair loop with the router between attempts. Put it before ``RepairProvider`` or a subclass of it in the
    bases (``RouterProvider``, ``routed``): the loop is this class's, the call is the base's (``_call``: a JSON
    adapter, or Claude Code), the request and feedback are the repair loop's, and a lookup's evidence rides on the
    request as ``evidence``, which ``adapters/anthropic_adapter.py: build_prompt`` shows the model as untrusted
    reference text. Every decision and lookup is journaled (see the module docstring); the in-memory ``decisions``
    holds the route facts' values and ``trace`` the repair loop's entries with the decision each call followed."""

    def __init__(self, *args, router: RouterConfig | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        config = RouterConfig() if router is None else router
        if not isinstance(config, RouterConfig):
            raise Rejected("invalid_provider", "router must be a RouterConfig")
        if type(config.max_lookups) is not int or config.max_lookups < 0:
            raise Rejected("invalid_provider", "max_lookups must be zero or a positive integer")
        if isinstance(config.holdout, bool) or not isinstance(config.holdout, (int, float)) \
                or not 0 <= config.holdout <= 1:
            raise Rejected("invalid_provider", "holdout is a probability from 0 to 1")
        for part in (config.policy, config.decider):
            if part is not None and not (callable(getattr(part, "decide", None))
                                         and isinstance(getattr(part, "name", None), str)
                                         and isinstance(getattr(part, "revision", None), str)):
                raise Rejected("invalid_provider", "a policy or decider needs name, revision and decide(situation)")
        if config.policy is None:
            raise Rejected("invalid_provider", "the router needs a policy")
        if getattr(self, "githits", False) is True:
            raise Rejected("invalid_provider", "a routed provider has no GitHits route of its own: with the router the "
                                               "factory looks up, the model does not (use --router or --githits)")
        if getattr(self, "mcp_config", None) is not None:
            raise Rejected("invalid_provider", "a routed provider takes no --mcp-config: an MCP server could give the "
                                               "model a GitHits route of its own, and with the router only the factory "
                                               "looks up")
        self.router = config
        self.decisions: list[dict] = []

    # -------------------------------------------------------------------------------------------------- the search

    def search(self, space: SearchSpace) -> Sequence[str]:
        rng = random.Random(space.seed)  # the repair loop's request seeds, draw for draw
        draws = random.Random(f"{HOLDOUT_SALT}:{space.seed}")  # the holdout's own stream
        limit = space.budget.max_candidates if self.max_calls is None else self.max_calls
        self.trace, self.decisions = [], []
        state = _State(getattr(space, "search_id", "s0"), self._routes())
        previous: str | None = None  # the last evaluated candidate: the parent of the next repair
        best: tuple[str, tuple[int, int]] | None = None
        stopped, made_calls = "calls", 0
        folder = Path(tempfile.mkdtemp(prefix="syberlabs-router-"))
        try:
            while True:
                situation, note, holdout = None, None, None
                if not self._room(space):
                    choice = Choice("stop", "stop.budget", "the search budget has no room for another call and its "
                                                           "evaluation")
                elif made_calls >= limit:
                    choice = Choice("stop", "stop.calls", f"the search's {limit} model calls are spent")
                elif state.current is None:
                    choice = Choice(PROPOSE, "propose.first" if made_calls == 0 else "propose.again",
                                    "nothing evaluated yet: propose")
                else:
                    situation = self._situation(space, state, made_calls, limit, previous)
                    choice, note, holdout = self._choose(situation, draws)
                number = self._decide(space, state, choice, situation, note, holdout, previous, made_calls, limit)
                if choice.action == "stop":
                    stopped = choice.rule
                    break
                if choice.action in LOOKUP_ACTIONS:
                    self._look(space, state, number, situation.needs[choice.action], folder)
                request = self._request(space, rng, previous)
                attached = [{key: value for key, value in item.items() if key != "signature"}
                            for item in state.carried()]
                if attached:
                    request["evidence"] = attached
                parents = [previous] if previous is not None else []
                entry = {"call": made_calls + 1, "parent": previous or "base", "feedback": "feedback" in request,
                         "outcome": None, "candidate": None, "score": None, "decision": number,
                         "action": choice.action, "rule": choice.rule, "evidence": len(attached)}
                self.trace.append(entry)
                self.calls, made_calls = self.calls + 1, made_calls + 1
                try:
                    response = self._call(space, request, parents)
                except Rejected as exc:
                    if exc.code.startswith("provider_"):
                        refuse(space, "provider_error", "patch", parents or ["base"])
                        entry["outcome"] = "provider_error"
                    state.outcome(entry["outcome"] or "error")
                    raise
                usage, tools = model_usage(response), tool_usage(response)
                if usage:
                    self.usage.append(usage)
                items = response.get("candidates") if isinstance(response, dict) else None
                if not isinstance(items, list) or (items and not self._parsed(items[0])):
                    refuse(space, "unparsable", "patch", parents or ["base"], usage=usage)
                    entry["outcome"] = "refused:unparsable"
                    state.outcome(entry["outcome"])
                    continue
                if not items:
                    refuse(space, "no_change", "patch", parents or ["base"], usage=usage)
                    entry["outcome"] = "refused:no_change"
                    state.outcome(entry["outcome"])
                    continue
                for _ in items[1:]:
                    refuse(space, "limit", "patch", parents or ["base"], usage=usage)
                item = items[0]
                message = item.get("message") if isinstance(item.get("message"), str) else ""
                signal = with_usage(item.get("signal"), usage, tools)
                try:
                    made = space.submit(item["changes"], parents=parents, operator="patch", message=message[:500],
                                        signal=signal)
                except Rejected as exc:
                    if exc.code in ("no_change", "invalid_change", "duplicate", "zero_usage"):
                        entry["outcome"] = "refused:" + ("unparsable" if exc.code == "invalid_change" else exc.code)
                        state.outcome(entry["outcome"])
                        continue
                    raise
                entry["candidate"] = made.id
                if not made.in_scope:
                    entry["outcome"], entry["score"] = "out_of_scope", [0, 0]
                    state.outcome(entry["outcome"])
                    continue
                if not self._room(space):
                    entry["outcome"], entry["score"] = "unevaluated", [0, 0]
                    state.outcome(entry["outcome"])
                    best = best or (made.id, (0, 0))
                    continue  # the next decision is stop.budget
                found = space.evaluate(made.id)
                score = found.score()
                entry["outcome"], entry["score"] = ("passed" if found.passed else "failed"), list(score)
                state.outcome(entry["outcome"])
                if best is None or score > best[1]:
                    best = (made.id, score)
                if found.passed:
                    stopped = "passing"
                    break
                previous = made.id
                state.observe(self._diagnose(space, made.id, found), score)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self.trace.append({"stopped": stopped, "calls": made_calls,
                           "refused": dict(getattr(space, "refused", None) or {}), "decisions": state.decisions,
                           "lookups": state.lookups})
        return [best[0]] if best else []

    # ------------------------------------------------------------------------------------------------ the decision

    def _routes(self) -> dict:
        route, web = self.router.githits, self.router.web
        return {"githits": "none" if route is None else (route.ready() or "ready"),
                "web": "none" if web is None else "ready"}

    def _diagnose(self, space: SearchSpace, candidate: str, found) -> Diagnosis:
        view = space.candidate(candidate)
        files = set(space.files()) | set(view.changed_paths)
        return diagnose(found.checks, required=found.required, files=files,
                        read=lambda path: space.read(candidate, path), versions=self.router.versions)

    def _tree_lines(self, space: SearchSpace, state: _State, candidate: str | None) -> set[str]:
        """The query guard's tree rule over the failed candidate's changed files and the first ``MAX_TREE_FILES``
        in-scope files at the base: the digests of their normalized lines of at least ``query_guard.MIN_TREE_LINE``
        characters, as the hook holds them for a Claude Code run (``query_guard.tree_digests``), read through the
        space once per search. The router's query holds no repository text but the failing symbol's name, so the
        rule is a backstop here, and the bound keeps a large scope from costing a read per file."""
        def lines(ref: str, paths: Iterable[str]) -> set[str]:
            found: set[str] = set()
            for path in list(paths)[:MAX_TREE_FILES]:
                for line in (space.read(ref, path) or "").splitlines():
                    if len(query_guard.normalized(line)) >= query_guard.MIN_TREE_LINE:
                        found.add(query_guard.line_digest(line))
            return found
        if "base" not in state.lines:
            state.lines["base"] = lines("base", space.files())
        if candidate is not None and candidate not in state.lines:
            state.lines[candidate] = lines(candidate, space.candidate(candidate).changed_paths)
        return state.lines["base"] | state.lines.get(candidate, set())

    def _eligible(self, space: SearchSpace, state: _State, candidate: str | None) -> tuple[tuple[str, ...], dict, dict]:
        """The actions open after this failure, the lookups they would make, and the routes whose query the guard
        blocked. ``repair`` and ``stop`` are always open; a lookup needs a ready route, a failure it can answer (a
        GitHits lookup an installed distribution), the search's lookup budget, a question not asked before on that
        route, and a query the guard passes."""
        found, config = state.current, self.router
        eligible, needs, blocked = {"repair", "stop"}, {}, {}
        options = []
        if found is not None and state.lookups < config.max_lookups:
            if (config.githits is not None and state.routes.get("githits") == "ready" and found.kind == "third_party"
                    and found.distribution):
                options.append(("lookup", githits_need(found), state.looked_up))
            if config.web is not None and found.kind in ("third_party", "missing_dependency") and found.module:
                options.append(("web", web_need(found), state.searched))
        options = [(action, need, done) for action, need, done in options if need.key not in done]
        tree = self._tree_lines(space, state, candidate) if options else set()
        for action, need, _done in options:
            rule = query_guard.judge(need.query, tree=tree)
            if rule == query_guard.PASS:
                eligible.add(action)
                needs[action] = need
            else:
                blocked[action] = rule
        return tuple(action for action in ACTIONS if action in eligible), needs, blocked

    def _situation(self, space: SearchSpace, state: _State, made_calls: int, limit: int,
                   candidate: str | None) -> Situation:
        eligible, needs, blocked = self._eligible(space, state, candidate)
        return Situation(decision=state.decisions + 1, calls=made_calls, calls_left=limit - made_calls, room=True,
                         diagnosis=state.current, score=state.score, best=state.best, repeats=state.repeats(),
                         eligible=eligible, lookups_used=state.lookups, max_lookups=self.router.max_lookups,
                         routes=dict(state.routes), history=tuple(dict(item) for item in state.history[-8:]),
                         needs=needs, blocked=blocked)

    @staticmethod
    def _valid(proposed, situation: Situation) -> tuple[Choice | None, str | None]:
        """``(choice, None)`` when ``proposed`` (a ``Choice`` or ``{action, rule, reason}``) names an eligible
        action; ``(None, why)`` otherwise: ``unknown_action`` or ``not_eligible``."""
        if isinstance(proposed, Mapping):
            proposed = Choice(str(proposed.get("action"))[:16], str(proposed.get("rule") or "custom"),
                              str(proposed.get("reason") or ""))
        if not isinstance(proposed, Choice) or proposed.action not in ACTIONS:
            return None, "unknown_action"
        if proposed.action not in situation.eligible:
            return None, "not_eligible"
        reason = proposed.reason if isinstance(proposed.reason, str) and proposed.reason.isprintable() else ""
        rule = proposed.rule if isinstance(proposed.rule, str) and proposed.rule.isprintable() else "decider"
        return Choice(proposed.action, rule[:MAX_NAME], reason[:MAX_REASON] or f"chose {proposed.action}"), None

    def _choose(self, situation: Situation, draws: random.Random) -> tuple[Choice, dict | None, dict | None]:
        """The policy's choice (the baseline's when a custom policy answers with an action that is not eligible), a
        decider's when it is eligible, then the holdout draw."""
        config = self.router
        choice, _why = self._valid(config.policy.decide(situation), situation)
        if choice is None:
            choice = BaselinePolicy().decide(situation)
        note = None
        if config.decider is not None:
            decider = config.decider
            note = {"name": f"{decider.name}@{decider.revision}"[:MAX_NAME]}
            try:
                proposed = decider.decide(replace(situation, baseline=choice))
                if isinstance(proposed, Choice):
                    proposed = replace(proposed, rule="decider")
                elif isinstance(proposed, Mapping):
                    proposed = {**proposed, "rule": "decider"}
                action = proposed.action if isinstance(proposed, Choice) else (
                    proposed.get("action") if isinstance(proposed, Mapping) else None)
                note["proposed"] = str(action)[:16] if action is not None else None
                accepted, why = self._valid(proposed, situation)
                if accepted is None:
                    note["refused"] = why
                else:
                    choice = accepted
            # A decider that fails never ends the search: the baseline decides. A budget stop is not a failure and
            # is not caught here: it ends the search as it would at the model call.
            except Rejected as exc:
                note["error"] = exc.code[:MAX_NAME]
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                note["error"] = "decider_error"
            usage = getattr(decider, "last_usage", None)
            if isinstance(usage, dict) and usage:
                note["usage"] = usage
        holdout = None
        if choice.action in LOOKUP_ACTIONS and config.holdout > 0:
            held = draws.random() < config.holdout
            holdout = {"p": float(config.holdout), "chosen": choice.action, "rule": choice.rule, "held_out": held}
            if held:
                choice = Choice("repair", "holdout", f"held out: {choice.action} was chosen ({choice.rule}); "
                                                     "repaired instead, so the lookup's value can be measured")
        return choice, note, holdout

    def _decide(self, space: SearchSpace, state: _State, choice: Choice, situation: Situation | None,
                note: dict | None, holdout: dict | None, previous: str | None, made_calls: int, limit: int) -> int:
        """Journal one decision before anything it decides runs."""
        state.decisions += 1
        number = state.decisions
        need = situation.needs.get(choice.action) if situation is not None and choice.action in LOOKUP_ACTIONS else None
        left = space.remaining()
        policy = self.router.policy
        value = {
            "router": f"{policy.name}@{policy.revision}"[:MAX_NAME], "search": state.search, "decision": number,
            "call": made_calls, "after": previous, "action": choice.action, "rule": choice.rule[:MAX_NAME],
            "reason": choice.reason[:MAX_REASON], "eligible": list(situation.eligible) if situation else None,
            "decider": note, "holdout": holdout,
            # The failure being repaired, read on every decision after one (a budget stop included), so the attempt
            # before it can be told apart from a new failure.
            "diagnosis": state.current.record() if state.current else None,
            "score": list(state.score) if state.score else None,
            "best": list(state.best) if state.best else None,
            "repeats": state.repeats(),
            "lookup": need.record() if need else None,
            "blocked": dict(situation.blocked) if situation and situation.blocked else None,
            "carried": [item["decision"] for item in state.carried()] if choice.action in CALL_ACTIONS else [],
            "routes": dict(state.routes),
            "budget": {"calls": limit - made_calls, "candidates": left.max_candidates,
                       "evaluations": left.max_evaluations, "seconds": left.max_seconds,
                       "dollars": getattr(left, "max_dollars", None)},
        }
        self.decisions.append(value)
        state.history.append({"decision": number, "action": choice.action, "rule": choice.rule, "outcome": None})
        self._observe(space, ROUTE, value, f"{state.search}.r{number}")
        return number

    def _look(self, space: SearchSpace, state: _State, number: int, need: Need, folder: Path) -> None:
        """Run the lookup the decision chose, journal each call, and keep what came back for the next calls."""
        state.lookups += 1
        version = f"{state.search}.r{number}"
        timeout = float(max(1, min(githits.DEFAULT_TIMEOUT, space.remaining().max_seconds)))
        text = ""
        if need.route == "githits":
            state.looked_up.add(need.key)
            try:
                looked = self.router.githits.lookup(need, folder / f"r{number}", timeout=timeout)
            except Exception:  # noqa: BLE001  a lookup never ends the search
                looked = Looked("", [], 0, "router_lookup_error")
            for count, record in enumerate(looked.records, 1):
                self._observe(space, LOOKUP, record_lookup(record, query_guard.PASS), f"{version}.{count}")
            text = looked.text
        else:
            state.searched.add(need.key)
            started = time.monotonic()
            try:
                answer = self.router.web.search(need.query, timeout=timeout)
            except Exception:  # noqa: BLE001
                answer = WebAnswer("error")
            if not isinstance(answer, WebAnswer):
                answer = WebAnswer("error")
            self._observe(space, LOOKUP, {
                "tool": WEB_TOOL, "query_digest": _digest(need.query), "query_chars": len(need.query), "at": _now(),
                "ok": answer.status == "ok", "guard": query_guard.PASS, "status": answer.status,
                "results_returned": answer.results if type(answer.results) is int and answer.results >= 0 else 0,
                "request_id": answer.request_id, "latency_ms": int((time.monotonic() - started) * 1000)},
                f"{version}.1")
            text = answer.text if answer.status == "ok" and isinstance(answer.text, str) else ""
        if text.strip() and state.current is not None:
            pin = (f"{need.version} ({need.version_source})" if need.version
                   else "unavailable: the results may describe another version than the one installed")
            state.evidence.append({"route": need.route, "decision": number, "target": need.target, "pin": pin,
                                   "signature": state.current.signature, "text": _clip(text)})

    @staticmethod
    def _observe(space: SearchSpace, key: str, value: dict, version: str) -> None:
        record = getattr(space, "observe", None)
        if callable(record):
            record(key, value, version)


class RouterProvider(RouterMixin, RepairProvider):
    """The repair loop (a JSON adapter as the call) with the router between attempts. Research, not the default:
    ``syberlabs propose --repair ADAPTER --router``."""

    def __init__(self, argv: Sequence[str], *, name: str = "repair-router", router: RouterConfig | None = None,
                 **kwargs):
        super().__init__(argv, name=name, router=router, **kwargs)

    def _call(self, space: SearchSpace, request: dict, parents: list[str]):
        """``RepairProvider._call`` in ``command_environment()``: the model's command gets no GitHits variable and no
        signing seed, as Claude Code's environment has none in arm A, so under the router only the factory can look
        up. The plain repair loop's command still inherits the caller's whole environment (unchanged)."""
        return run_json(self.argv, request, timeout=self.timeout, cwd=self.cwd, env=command_environment())


_ROUTED: dict[type, type] = {RepairProvider: RouterProvider}


def routed(base: type) -> type:
    """``base`` (``RepairProvider`` or a subclass, such as ``ClaudeCodeProvider``) with the router's loop: a class
    whose bases are ``RouterMixin`` and ``base``. Its constructor takes ``base``'s arguments plus ``router``."""
    if not isinstance(base, type) or not issubclass(base, RepairProvider):
        raise Rejected("invalid_provider", "the router routes a RepairProvider's calls")
    if issubclass(base, RouterMixin):
        return base
    if base not in _ROUTED:
        _ROUTED[base] = type(f"Routed{base.__name__}", (RouterMixin, base),
                             {"__module__": __name__, "__doc__": f"{base.__name__} with the router between attempts."})
    return _ROUTED[base]


def session_id(thread: str) -> str:
    """``X-GitHits-Session-ID`` for a thread's router lookups: ``<thread>-router`` (GitHits allows only
    ``[A-Za-z0-9_-]``)."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"{thread}-router")[:64]


def cli_config(args, thread: str) -> RouterConfig:
    """The router a ``syberlabs propose ... --router`` builds: GitHits from the operator's environment, a web search
    and a decider model when named, and the lookup budget and holdout given."""
    holdout = 0.0 if args.router_holdout is None else args.router_holdout
    if not 0 <= holdout <= 1:
        raise Rejected("invalid_provider", "--router-holdout is a probability from 0 to 1")
    lookups = DEFAULT_MAX_LOOKUPS if args.router_max_lookups is None else args.router_max_lookups
    if lookups < 0:
        raise Rejected("invalid_provider", "--router-max-lookups is zero or more")
    return RouterConfig(githits=GitHitsRoute.from_env(session=session_id(thread)),
                        web=CommandWeb(split_command(args.router_web)) if args.router_web else None,
                        decider=CommandDecider(split_command(args.router_decider)) if args.router_decider else None,
                        max_lookups=lookups, holdout=float(holdout))


# ------------------------------------------------------------------------------------------------------- attribution

def _route_value(value) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("search"), str) and type(value.get("decision")) is int
            and isinstance(value.get("action"), str))


def _required(events: list[dict], contracts) -> list[str] | None:
    created = next((event for event in events if event["kind"] == "case_created"), None)
    contract = (created or {}).get("body", {}).get("contract")
    if not contracts or not isinstance(contract, (list, tuple)) or len(contract) != 2:
        return None
    doc = contracts.get(tuple(contract)) or contracts.get(f"{contract[0]}.v{contract[1]}") or {}
    found = ((doc.get("evolution") or {}).get("evaluation") or {}).get("required")
    return list(found) if isinstance(found, list) else None


def _score(body: dict | None, required: list[str] | None) -> tuple[tuple[int, int] | None, bool | None]:
    """``(Evaluation.score(), passed)`` from a ``candidate_evaluated`` body; with no contract, every recorded check
    counts as required, as in ``metrics``."""
    if not isinstance(body, dict):
        return None, None
    states = {c.get("name"): c.get("state") for c in body.get("checks") or [] if isinstance(c, dict)}
    names = required if required is not None else list(states)
    score = (sum(states.get(name) == "passed" for name in names), sum(state == "passed" for state in states.values()))
    return score, all(states.get(name) == "passed" for name in names)


def attribution(events: Iterable[Mapping], contracts: Mapping | None = None) -> list[dict]:
    """One row per router decision in ``events``, in journal order. Pure: it reads events and nothing else.

    A decision's window is its events up to the next decision of the same search, or that search's end. In it: the
    lookup facts it made (versions ``<search>.r<n>.*``), the registration of the call that followed, its evaluation,
    or the refusal that took its place. ``after.outcome`` is ``passed``, ``failed``, ``unevaluated``,
    ``refused:<reason>``, ``none`` (no attempt is recorded: the call failed), or None for a ``stop``.
    ``trigger_fixed`` is whether the check the decision read passed in that evaluation; ``same_failure`` whether the
    next decision read the same failure signature; ``calls_to_pass`` the model calls from this decision to the
    search's first passing candidate, this decision's own call included (None when none passed after it)."""
    threads: dict[str, list[dict]] = {}
    for event in events:
        threads.setdefault(event["case_id"], []).append(dict(event))
    rows: list[dict] = []
    for case_id, found in threads.items():
        found.sort(key=lambda event: event["seq"])
        required = _required(found, contracts)
        routes = [(index, value) for index, value in ((i, metrics.fact(e, ROUTE)) for i, e in enumerate(found))
                  if _route_value(value)]
        thread_rows: list[dict] = []
        for position, (index, value) in enumerate(routes):
            search, number = value["search"], value["decision"]
            later = [i for i, v in routes[position + 1:] if v["search"] == search]
            end = later[0] if later else next(
                (i for i in range(index + 1, len(found)) if found[i]["kind"] == "search_finished"
                 and found[i]["body"].get("id") == search), len(found))
            window = found[index + 1:end]
            before = next((e["body"] for e in reversed(found[:index]) if e["kind"] == "candidate_evaluated"
                           and e["body"].get("candidate") == value.get("after")), None)
            score_before, _ = _score(before, required)
            lookups = [fact for fact in (metrics.fact(e, LOOKUP) for e in window
                                         if str(e["body"].get("version", "")).startswith(f"{search}.r{number}."))
                       if fact is not None]
            registration = next((e["body"] for e in window if e["kind"] == "candidate_registered"), None)
            refused = [fact for fact in (metrics.fact(e, "proposal_refused") for e in window)
                       if fact is not None and fact.get("search") == search]
            evaluation = None
            if registration is not None:
                evaluation = next((e["body"] for e in window if e["kind"] == "candidate_evaluated"
                                   and e["body"].get("candidate") == registration.get("id")), None)
            score_after, passed = _score(evaluation, required)
            if value["action"] == "stop":
                outcome = None
            elif evaluation is not None:
                outcome = "passed" if passed else "failed"
            elif registration is not None:
                outcome = "unevaluated"
            elif refused:
                outcome = "refused:" + str(refused[0].get("reason"))
            else:
                outcome = "none"
            diagnosis = value.get("diagnosis") if isinstance(value.get("diagnosis"), dict) else {}
            trigger = diagnosis.get("check")
            states = {c.get("name"): c.get("state") for c in (evaluation or {}).get("checks") or []
                      if isinstance(c, dict)}
            signal = (registration or {}).get("signal") if isinstance((registration or {}).get("signal"), dict) else {}
            usage = signal.get("model_usage") if isinstance(signal.get("model_usage"), dict) else {}
            spend = (registration or {}).get("spend") if isinstance((registration or {}).get("spend"), dict) else {}
            dollars = spend.get("dollars") if isinstance(spend.get("dollars"), (int, float)) else None
            thread_rows.append({
                "thread": case_id, "search": search, "decision": number, "action": value["action"],
                "rule": value.get("rule"), "router": value.get("router"), "decider": value.get("decider"),
                "holdout": value.get("holdout"), "eligible": value.get("eligible"),
                "kind": diagnosis.get("kind"), "error": diagnosis.get("error"), "module": diagnosis.get("module"),
                "distribution": diagnosis.get("distribution"), "version": diagnosis.get("version"),
                "pin": diagnosis.get("pin"), "check": trigger, "signature": diagnosis.get("signature"),
                "carried": value.get("carried") or [],
                "lookups": {"calls": len(lookups), "ok": sum(1 for v in lookups if v.get("ok") is True),
                            "results": sum(v.get("results_returned") or 0 for v in lookups
                                           if type(v.get("results_returned")) is int),
                            "latency_ms": sum(v.get("latency_ms") or 0 for v in lookups
                                              if type(v.get("latency_ms")) is int),
                            "tools": sorted({str(v.get("tool")) for v in lookups})},
                "after": {"candidate": (registration or {}).get("id"), "outcome": outcome,
                          "score_before": list(score_before) if score_before else None,
                          "score_after": list(score_after) if score_after else None,
                          "gained": (score_after[0] - score_before[0]) if score_after and score_before else None,
                          "trigger_fixed": (states.get(trigger) == "passed")
                          if evaluation is not None and trigger else None,
                          "same_failure": None, "dollars": dollars,
                          "input_tokens": _count(usage.get("input_tokens")),
                          "output_tokens": _count(usage.get("output_tokens")),
                          "evaluation_ms": sum(c.get("duration_ms") or 0 for c in (evaluation or {}).get("checks") or []
                                               if isinstance(c, dict) and type(c.get("duration_ms")) is int)
                          if evaluation is not None else None},
                "calls_to_pass": None,
            })
        for position, row in enumerate(thread_rows):
            same = [other for other in thread_rows[position + 1:] if other["search"] == row["search"]]
            if row["after"]["outcome"] == "passed":
                row["after"]["same_failure"] = False
            elif row["after"]["outcome"] == "failed" and same and same[0]["signature"] and row["signature"]:
                row["after"]["same_failure"] = same[0]["signature"] == row["signature"]
            if row["action"] in CALL_ACTIONS:
                count = 0
                for other in [row] + same:
                    count += 1 if other["action"] in CALL_ACTIONS else 0
                    if other["after"]["outcome"] == "passed":
                        row["calls_to_pass"] = count
                        break
        rows += thread_rows
    return rows


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 6) if whole else None


def _mean(values: Iterable) -> float | None:
    found = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return round(statistics.fmean(found), 6) if found else None


def _group(rows: Sequence[Mapping]) -> dict:
    evaluated = [row for row in rows if row["after"]["outcome"] in ("passed", "failed")]
    passed = sum(1 for row in evaluated if row["after"]["outcome"] == "passed")
    return {"decisions": len(rows), "evaluated": len(evaluated), "passed": passed,
            "pass_rate": _rate(passed, len(evaluated)),
            "trigger_fixed": sum(1 for row in evaluated if row["after"]["trigger_fixed"] is True),
            "same_failure": sum(1 for row in evaluated if row["after"]["same_failure"] is True),
            "calls_to_pass_mean": _mean(row["calls_to_pass"] for row in rows),
            "dollars_mean": _mean(row["after"]["dollars"] for row in evaluated)}


def summary(rows: Sequence[Mapping]) -> dict:
    """The rows read per action, per lookup, and, when the holdout ran, as a randomized comparison.

    ``by_action``: decisions, the attempts that followed and were evaluated, how many passed, fixed the triggering
    check or failed the same way, the mean calls to the first pass and the mean dollars of the attempt. ``lookup``:
    the same for GitHits lookups, with the GitHits calls, results and latency per lookup. ``holdout`` (None when no
    decision drew one): the lookups the policy chose that ran against those it chose and the draw held out, each
    group's next-attempt pass rate, and the difference (looked up minus held out). Only the holdout comparison is free
    of the policy's own selection: ``by_action`` compares lookups with repairs of other failures."""
    by_action = {action: _group([row for row in rows if row["action"] == action]) for action in (PROPOSE,) + ACTIONS}
    looked = [row for row in rows if row["action"] == "lookup"]
    lookup = {**_group(looked),
              "githits_calls": sum(row["lookups"]["calls"] for row in looked),
              "githits_calls_per_lookup": _rate(sum(row["lookups"]["calls"] for row in looked), len(looked)),
              "with_results": sum(1 for row in looked if row["lookups"]["results"] > 0),
              "latency_ms_mean": _mean(row["lookups"]["latency_ms"] for row in looked)}
    drawn = [row for row in rows if isinstance(row.get("holdout"), dict) and row["holdout"].get("chosen") == "lookup"]
    holdout = None
    if drawn:
        ran = [row for row in drawn if row["holdout"].get("held_out") is False]
        held = [row for row in drawn if row["holdout"].get("held_out") is True]
        a, b = _group(ran), _group(held)
        holdout = {"looked_up": a, "held_out": b,
                   "difference": round(a["pass_rate"] - b["pass_rate"], 6)
                   if a["pass_rate"] is not None and b["pass_rate"] is not None else None}
    return {"decisions": len(rows), "by_action": by_action, "lookup": lookup, "holdout": holdout}
