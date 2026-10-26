"""Tasks: ``evals/tasks/<id>/task.json`` and its ``hidden/`` directory, digested, materialized, and checked unseen.

A task is the unit an eval compares arms on. ``task.json`` (``TASK_SCHEMA``) holds:

- ``source``: ``fixture:<name>`` (a fixture of ``benchmarks/fixtures.py``, its visible files pinned by
  ``source_digest``) or ``repo:<git url>@<40-hex base commit>``;
- ``objective`` and ``scope`` (the contract's mutable scope and the paths a provider may read);
- ``checks``: visible checks, run in the candidate's worktree like any contract check (may be empty);
- ``hidden.checks``: the acceptance checks the host holds. Their argv may name ``{python}`` (the host's interpreter)
  and ``{hidden}`` (the absolute path of the task's ``hidden/`` directory as the host staged it for this run);
- ``safety`` (expectations and planted canary strings, may be empty), ``provenance``, ``contamination``,
  ``labels``.

``task_digest`` is ``syberlabs.canonical.digest`` of ``{"task": <parsed task.json>, "hidden": {<path>: <sha256>}}``
over every file under ``hidden/``. A hidden file that decodes as UTF-8 and holds no NUL byte is text: its CRLF line
endings become LF before it is digested and staged, so a Windows checkout with ``core.autocrlf`` digests what a POSIX
one does; any other file is digested and staged byte for byte. ``set_digest`` is the digest of ``{id: digest}``.

How a hidden check stays hidden (``stage_hidden`` and ``contract_checks``): for each run the host copies ``hidden/``
into a directory of its own outside the run's repository, verifies the copy against the registered digest, and
appends each hidden check to the run's contract as a required check named ``hidden.<name>`` whose argv names that
copy by absolute path, the pattern the critic and the sharded test runner use (``docs/CRITIC.md``). The check runs
as every check does (``syberlabs.checks``): in a detached worktree of the candidate, with a scrubbed environment;
it reads the candidate's files from its working directory. Nothing under ``hidden/`` is in the run's repository, in
a provider's request (``GitSearchSpace`` serves only the repository), or in a candidate. A check's result and the
tail of its output are recorded, as for any check, and reach a repair arm as feedback: that is the E3 design, where
an assertion message names its expectation. The checks run the candidate's code, which runs with the host's
permissions and can read and rewrite the host's copy: the runner refuses to accept on a copy whose files changed
(``tree_digests``), and nothing stops code that reads the copy or ends its check early. ``import_fixtures`` writes
each fixture check's code to ``hidden/checks.json`` and runs it through ``LOADER``, which compiles it under the file
name ``<hidden:NAME>``, so a traceback names no host path and prints no line of the check's source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from syberlabs.canonical import digest
from syberlabs.errors import Rejected
from syberlabs.eval.spec import HARNESS, HEX64, NAME, read_json, validate_schema, write_json

TASK_SCHEMA_ID = "syberlabs.eval/task.v1"
HIDDEN = "hidden"
HIDDEN_PREFIX = "hidden."
FIXTURE_SETS = ("v3", "v4")  # what ``import-fixtures`` writes when no fixture is named
# The base commit of a materialized fixture is the same on every run and host: fixed identity and dates.
FIXTURE_IDENTITY = {"GIT_AUTHOR_NAME": "syberlabs-eval", "GIT_AUTHOR_EMAIL": "eval@syberlabs.invalid",
                    "GIT_COMMITTER_NAME": "syberlabs-eval", "GIT_COMMITTER_EMAIL": "eval@syberlabs.invalid",
                    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
# Runs one check of ``hidden/checks.json`` as ``__main__``: argv is the file, the check's name, then the check's own
# arguments (``sys.argv[1:]`` inside it; ``{hidden}/data.txt`` names a hidden data file). ``compile`` names the code
# ``<hidden:NAME>``: ``linecache`` cannot read that name, so a traceback shows no source line and no host path.
LOADER = ("import json, sys; path, name = sys.argv[1], sys.argv[2]; "
          "body = json.loads(open(path, encoding='utf-8').read())[name]; sys.argv = [name] + sys.argv[3:]; "
          "exec(compile(body, '<hidden:' + name + '>', 'exec'), {'__name__': '__main__'})")
# Where the fixture generations were published (``contamination``): the pull request and the day it merged.
PUBLISHED = {"v3": ("#67", "2026-10-23"), "v4": ("#75", "2026-10-23")}

_ARGV = {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string", "minLength": 1, "maxLength": 8192}}
_CHECK = {"type": "object", "additionalProperties": False, "required": ["argv"],
          "properties": {"argv": _ARGV, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}}}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 4000}
TASK_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": TASK_SCHEMA_ID,
    "type": "object", "additionalProperties": False,
    "required": ["schema", "id", "source", "objective", "scope", "checks", "hidden", "safety", "provenance",
                 "contamination", "labels"],
    "properties": {
        "schema": {"const": TASK_SCHEMA_ID},
        "id": {"type": "string", "pattern": NAME},
        "source": {"type": "string", "pattern": r"^(fixture:[a-z0-9_]{1,64}|repo:\S{1,2000}@[0-9a-f]{40})$"},
        "source_digest": {"type": "string", "pattern": HEX64},
        "objective": _TEXT,
        "scope": {"type": "object", "additionalProperties": False, "required": ["paths"],
                  "properties": {"paths": {"type": "array", "minItems": 1, "maxItems": 64,
                                           "items": {"type": "string", "minLength": 1, "maxLength": 512}},
                                 "exclude": {"type": "array", "maxItems": 64,
                                             "items": {"type": "string", "minLength": 1, "maxLength": 512}},
                                 "max_files": {"type": "integer", "minimum": 1, "maximum": 1000},
                                 "max_diff_bytes": {"type": "integer", "minimum": 1, "maximum": 10_000_000}}},
        "checks": {"type": "object"},
        "hidden": {"type": "object", "additionalProperties": False, "required": ["checks"],
                   "properties": {"checks": {"type": "object"}}},
        "safety": {"type": "object", "additionalProperties": False, "required": ["expectations", "canaries"],
                   "properties": {"expectations": {"type": "array", "maxItems": 64, "items": _TEXT},
                                  "canaries": {"type": "array", "maxItems": 64,
                                               "items": {"type": "string", "minLength": 8, "maxLength": 256}}}},
        "provenance": {"type": "object"},
        "contamination": {"type": "object", "additionalProperties": False, "required": ["status", "note"],
                          "properties": {"status": {"enum": ["unknown", "private", "public_repository",
                                                             "public_benchmark"]},
                                         "note": _TEXT}},
        "labels": {"type": "object"},
    },
}
_CHECK_NAME = r"^[a-z][a-z0-9_.-]{0,55}$"  # leaves room for the ``hidden.`` prefix under the contract's 64


def check_task(doc: Any) -> list[str]:
    """Every reason ``doc`` is not a valid task: the schema, then the checks and the source."""
    errors = validate_schema(doc, TASK_SCHEMA)
    if errors:
        return errors
    for where, checks in (("checks", doc["checks"]), ("hidden.checks", doc["hidden"]["checks"])):
        for name, check in checks.items():
            if not re.match(_CHECK_NAME, name):
                errors.append(f"$.{where}.{name}: check names are lowercase names of at most 56 characters")
            found = validate_schema(check, _CHECK, f"$.{where}.{name}")
            errors += found
            if where == "checks" and not found and (name.startswith(HIDDEN_PREFIX)
                                                    or any("{hidden}" in arg for arg in check["argv"])):
                errors.append(f"$.checks.{name}: a visible check may not name the hidden directory")
    if not doc["hidden"]["checks"]:
        errors.append("$.hidden.checks: a task needs at least one hidden acceptance check")
    if len(doc["checks"]) + len(doc["hidden"]["checks"]) > 32:
        errors.append("$: at most 32 checks in all (the contract's limit)")
    if doc["source"].startswith("fixture:") and "source_digest" not in doc:
        errors.append("$.source_digest: required for a fixture source")
    return errors


def task_dir(tasks_root: Path, task_id: str) -> Path:
    if not isinstance(task_id, str) or not re.match(NAME, task_id):
        raise Rejected("invalid_task", f"{task_id!r} is not a task id")
    return Path(tasks_root) / task_id


def load_task(tasks_root: Path, task_id: str) -> dict:
    """``evals/tasks/<id>/task.json``, validated, with its id matching its directory."""
    folder = task_dir(tasks_root, task_id)
    if not (folder / "task.json").is_file():
        raise Rejected("task_unknown", f"no task {task_id} under {tasks_root}")
    doc = read_json(folder / "task.json", "task_unreadable")
    errors = check_task(doc)
    if errors:
        raise Rejected("invalid_task", f"{task_id}: " + "; ".join(errors[:8]))
    if doc["id"] != task_id:
        raise Rejected("invalid_task", f"{folder / 'task.json'} names task {doc['id']!r}")
    return doc


def normalized(data: bytes) -> bytes:
    """Text (UTF-8, no NUL) with LF line endings; any other bytes unchanged."""
    if b"\0" in data:
        return data
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return data.replace(b"\r\n", b"\n")


def hidden_files(folder: Path) -> dict[str, bytes]:
    """Every file under ``folder``, by POSIX path relative to it, normalized (``normalized``)."""
    found: dict[str, bytes] = {}
    if folder.is_dir():
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            found[path.relative_to(folder).as_posix()] = normalized(path.read_bytes())
    return found


def _hidden_digests(files: dict[str, bytes]) -> dict[str, str]:
    return {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}


def task_digest(tasks_root: Path, task_id: str) -> str:
    """The digest of the parsed ``task.json`` and every hidden file (see the module docstring)."""
    doc = load_task(tasks_root, task_id)
    hidden = hidden_files(task_dir(tasks_root, task_id) / HIDDEN)
    return digest({"task": doc, "hidden": _hidden_digests(hidden)})


def set_digest(digests: dict[str, str]) -> str:
    """The digest of a task set: ``{id: task digest}``."""
    return digest(dict(sorted(digests.items())))


# Fixtures (``benchmarks/fixtures.py``, read only)

def _fixtures():
    if str(HARNESS) not in sys.path:
        sys.path.insert(0, str(HARNESS))
    from benchmarks import fixtures

    return fixtures


def generation(name: str) -> str:
    fixtures = _fixtures()
    for label in ("v2", "v3", "v4"):
        if name in getattr(fixtures, label.upper()):
            return label
    return "v1"


def fixture_files(name: str) -> dict[str, bytes]:
    """The fixture's visible files as bytes (``str`` content as UTF-8, no newline translation)."""
    fixtures = _fixtures()
    if name not in fixtures.FIXTURES:
        raise Rejected("fixture_unknown", f"benchmarks/fixtures.py has no fixture {name!r}")
    return {path: text if isinstance(text, bytes) else text.encode("utf-8")
            for path, text in sorted(fixtures.FIXTURES[name]["files"].items())}


def source_digest(files: dict[str, bytes]) -> str:
    return digest({path: hashlib.sha256(data).hexdigest() for path, data in sorted(files.items())})


def fixture_task(name: str) -> tuple[dict, dict[str, str]]:
    """``(task.json, hidden/checks.json)`` for one fixture: its checks moved into the hidden file, run by ``LOADER``."""
    fixtures = _fixtures()
    fixture = fixtures.FIXTURES[name]
    bodies, checks = {}, {}
    for check_name, check in sorted(fixture["checks"].items()):
        argv = check["argv"]
        if len(argv) != 3 or argv[1] != "-c":
            raise Rejected("fixture_shape", f"{name}.{check_name}: expected [python, -c, code]")
        bodies[check_name] = argv[2]
        checks[check_name] = {"argv": ["{python}", "-c", LOADER, "{hidden}/checks.json", check_name],
                              "timeout_seconds": check.get("timeout_seconds", 60)}
    label = generation(name)
    scope = fixtures.contract(name)["evolution"]["scope"]
    published = PUBLISHED.get(label)
    note = (f"The fixture, its objective and these checks are public in SyberLabs/SyberWork benchmarks/fixtures.py "
            f"since {published[1]} ({published[0]}): hidden from the agent's worktree and requests, not from a model "
            "trained on that repository or a lookup into it." if published else
            "The fixture, its objective and these checks are public in SyberLabs/SyberWork benchmarks/fixtures.py: "
            "hidden from the agent's worktree and requests, not from a model trained on that repository or a lookup "
            "into it.")
    task = {
        "schema": TASK_SCHEMA_ID, "id": f"{label}-{name}", "source": f"fixture:{name}",
        "source_digest": source_digest(fixture_files(name)), "objective": fixture["objective"],
        "scope": {key: scope[key] for key in ("paths", "exclude", "max_files", "max_diff_bytes") if key in scope},
        "checks": {}, "hidden": {"checks": checks},
        "safety": {"expectations": [], "canaries": []},
        "provenance": {"origin": "benchmarks/fixtures.py", "fixture": name, "generation": label,
                       "adapter": "syberlabs eval tasks import-fixtures"},
        "contamination": {"status": "public_repository", "note": note},
        "labels": {"pool": "e3", "generation": label},
    }
    return task, bodies


def import_fixtures(tasks_root: Path, names: list[str] | None = None, *, force: bool = False) -> list[dict]:
    """Write ``evals/tasks/<generation>-<fixture>/`` for each fixture (default: every v3 and v4 fixture).

    Deterministic: importing the same fixtures again writes the same bytes. A task directory that already holds
    something else is refused (``task_exists``) unless ``force``. Returns ``[{id, digest}]``.
    """
    fixtures = _fixtures()
    names = names or [name for label in FIXTURE_SETS for name in getattr(fixtures, label.upper())]
    written = []
    for name in names:
        if name not in fixtures.FIXTURES:
            raise Rejected("fixture_unknown", f"benchmarks/fixtures.py has no fixture {name!r}")
        task, bodies = fixture_task(name)
        folder = task_dir(tasks_root, task["id"])
        task_bytes = (json.dumps(task, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        body_bytes = (json.dumps(bodies, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        existing = folder / "task.json"
        if existing.exists() and not force:
            same = (normalized(existing.read_bytes()) == task_bytes
                    and hidden_files(folder / HIDDEN) == {"checks.json": body_bytes})
            if not same:
                raise Rejected("task_exists", f"{folder} holds another task; pass --force to overwrite it")
        if force and (folder / HIDDEN).exists():
            shutil.rmtree(folder / HIDDEN)
        (folder / HIDDEN).mkdir(parents=True, exist_ok=True)
        existing.write_bytes(task_bytes)
        (folder / HIDDEN / "checks.json").write_bytes(body_bytes)
        written.append({"id": task["id"], "digest": task_digest(tasks_root, task["id"])})
    return written


# A run's repository and its hidden copy

def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    base = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=600,
                          env={**base, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    if done.returncode != 0:
        raise Rejected("materialize_failed", f"git {args[0]}: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def materialize(task: dict, dest: Path) -> str:
    """Create the run's repository at ``dest`` and return its base commit.

    A fixture: its visible files (and nothing else) committed to a fresh repository on ``main`` with
    ``core.autocrlf=false``, a fixed identity and fixed dates, so the base is the same commit for every run of the
    task; the files must still digest to ``source_digest`` (``task_source_changed`` otherwise). A repository:
    cloned, and a branch ``eval-base`` checked out at the pinned commit.
    """
    dest = Path(dest)
    source = task["source"]
    if source.startswith("fixture:"):
        files = fixture_files(source.split(":", 1)[1])
        if source_digest(files) != task["source_digest"]:
            raise Rejected("task_source_changed", f"{task['id']}: the fixture's files no longer digest to "
                           "source_digest; re-import the task and register a new eval")
        dest.mkdir(parents=True)
        _git(dest, "init", "-q", "--template=", "-b", "main")
        for key, value in (("core.autocrlf", "false"), ("core.safecrlf", "false"), ("commit.gpgsign", "false"),
                           ("user.name", FIXTURE_IDENTITY["GIT_AUTHOR_NAME"]),
                           ("user.email", FIXTURE_IDENTITY["GIT_AUTHOR_EMAIL"])):
            _git(dest, "config", key, value)
        for path, data in files.items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # Plumbing, not ``git commit``: the same commit for the same files on every host, and no hook runs. Forced:
        # an ignore rule of the host's (``core.excludesFile``, ``~/.config/git/ignore``) must not drop a fixture file.
        _git(dest, "add", "-A", "--force")
        tree = _git(dest, "write-tree")
        commit = _git(dest, "commit-tree", tree, "-m", f"task {task['id']}", env=FIXTURE_IDENTITY)
        _git(dest, "update-ref", "refs/heads/main", commit)
        return commit
    url, _, base = source[len("repo:"):].rpartition("@")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(dest.parent, "clone", "--quiet", "--template=", "--no-checkout", url, str(dest))
    _git(dest, "checkout", "-q", "-B", "eval-base", base)
    found = _git(dest, "rev-parse", "HEAD")
    if found != base:
        raise Rejected("materialize_failed", f"{task['id']}: HEAD is {found}, not the pinned {base}")
    return found


def stage_hidden(tasks_root: Path, task_id: str, dest: Path, expected_digest: str) -> Path:
    """Copy the task's ``hidden/`` into ``dest`` (host-owned, outside the run's repository), normalized, and return
    ``dest``; refused (``task_modified``) unless the task as staged digests to ``expected_digest``."""
    doc = load_task(tasks_root, task_id)
    files = hidden_files(task_dir(tasks_root, task_id) / HIDDEN)
    if digest({"task": doc, "hidden": _hidden_digests(files)}) != expected_digest:
        raise Rejected("task_modified", f"{task_id} does not digest to its registration")
    dest = Path(dest)
    dest.mkdir(parents=True)
    for path, data in files.items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest.resolve()


def _resolve(argv: list[str], python: str, hidden: Path | None) -> list[str]:
    out = []
    for arg in argv:
        arg = python if arg == "{python}" else arg
        if hidden is not None and "{hidden}" in arg:
            arg = arg.replace("{hidden}", str(hidden))
        out.append(arg)
    return out


def contract_checks(task: dict, staged: Path, python: str = sys.executable) -> tuple[dict, list[str]]:
    """``(checks, required)`` for the run's contract: the visible checks, then ``hidden.<name>`` for each hidden
    check with ``{hidden}`` resolved to ``staged``. Every check is required."""
    checks = {name: {"argv": _resolve(check["argv"], python, None), "timeout_seconds": check.get("timeout_seconds", 300)}
              for name, check in task["checks"].items()}
    for name, check in task["hidden"]["checks"].items():
        checks[HIDDEN_PREFIX + name] = {"argv": _resolve(check["argv"], python, Path(staged)),
                                        "timeout_seconds": check.get("timeout_seconds", 300)}
    return checks, sorted(checks)


def list_tasks(tasks_root: Path) -> list[str]:
    root = Path(tasks_root)
    return sorted(p.name for p in root.iterdir() if (p / "task.json").is_file()) if root.is_dir() else []


def hidden_path(name: Any) -> bool:
    """A relative POSIX path that stays under ``hidden/``: no absolute or drive path, backslash, ``.`` or ``..``."""
    return (isinstance(name, str) and bool(name) and not name.startswith("/") and not any(c in name for c in "\\:\0")
            and all(part not in ("", ".", "..") for part in name.split("/")))


def tree_digests(folder: Path) -> dict[str, str]:
    """The SHA-256 of every file under ``folder`` as it is on disk, by POSIX path; a symbolic link is named by its
    target. ``stage_hidden`` writes regular files only, so the staged copy's digests change if anything rewrites,
    adds, removes or relinks a file in it."""
    found = {}
    try:
        for path in sorted(p for p in Path(folder).rglob("*") if p.is_symlink() or p.is_file()):
            name = path.relative_to(folder).as_posix()
            if path.is_symlink():
                found[name] = "link:" + os.readlink(path)
                continue
            sha = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    sha.update(chunk)
            found[name] = sha.hexdigest()
    except OSError as exc:  # made unreadable: as changed as rewritten
        found[""] = f"unreadable: {exc.errno}"
    return found


def hidden_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The paths of a staged copy that differ between two ``tree_digests``: rewritten, removed, relinked or added,
    except files under a ``.pytest_cache`` directory, which pytest's cache provider writes into its rootdir, and
    that is the directory of a hidden test file run with no configuration file above it."""
    added = [path for path in after if path not in before and ".pytest_cache" not in path.split("/")[:-1]]
    return sorted([path for path in before if after.get(path) != before[path]] + added)


def write_task(tasks_root: Path, task: dict, hidden: dict[str, bytes]) -> str:
    """Write a task (used by tests and future adapters); returns its digest."""
    errors = check_task(task)
    errors += [f"hidden file {name!r}: a relative path under hidden/" for name in hidden if not hidden_path(name)]
    if errors:
        raise Rejected("invalid_task", "; ".join(errors[:8]))
    folder = task_dir(tasks_root, task["id"])
    write_json(folder / "task.json", task)
    for path, data in hidden.items():
        target = folder / HIDDEN / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return task_digest(tasks_root, task["id"])
