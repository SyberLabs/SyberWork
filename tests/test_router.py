"""The lookup router (``syberlabs.router``, ``docs/ROUTER.md``): research, opt-in, never the default proposer path.

No model is called and no real GitHits is reached. The model is a stand-in command that answers the search protocol
as an adapter would and logs every request it is given (or, for the routed Claude Code provider, a stand-in
``claude`` that prints a minimal ``stream-json`` run). GitHits is the loopback ``FakeGitHits`` of
``tests/test_githits.py`` with a random token. The third-party library is ``fakelib``: a package written with its
``dist-info`` into a temporary ``site-packages`` that the fixture's own test puts on its path, so the host's checks
raise real exceptions from real frames and the router reads them as it would read any check's output.
"""

import contextlib
import io
import json
import os
import random
import secrets
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from adapters import anthropic_adapter
from spec.validate import validate_events
from syberlabs import Kit, Rejected, githits, metrics, query_guard, router
from syberlabs.build import PROVIDER_FACTS, default_contract
from syberlabs.claude_code import ClaudeCodeProvider
from syberlabs.cli import main as cli
from syberlabs.events import verify_events
from syberlabs.providers import FunctionProvider, RepairProvider
from syberlabs.search import CheckResult
from tests.test_build_thread import git
from tests.test_githits import FakeGitHits, read_code_response, search_hit, search_response

PY = sys.executable
FAKELIB = ('"""fakelib 2.0: fetch_all() became fetch(), and timeout is a keyword-only argument."""\n\n\n'
           'def fetch(url, *, timeout):\n    return f"fetched {url} in {timeout}s"\n\n\n'
           'def each(rows, callback):\n    return [callback(row) for row in rows]\n')
STUB = "def get(url):\n    raise NotImplementedError('fetch the url with fakelib')\n"
V1 = "from fakelib import fetch_all\n\n\ndef get(url):\n    return fetch_all(url, 5)\n"  # the 1.x API: ImportError
POSITIONAL = "import fakelib\n\n\ndef get(url):\n    return fakelib.fetch(url, 5)  # attempt {n}\n"  # TypeError
RIGHT = "import fakelib\n\n\ndef get(url):\n    return fakelib.fetch(url, timeout=5)\n"
MISSING = "import fakelib_two\n\n\ndef get(url):\n    return fakelib_two.fetch(url, timeout=5)\n"  # installed nowhere
SIGNATURE = "def fetch(url, *, timeout)"  # what GitHits returns, and what the stand-in needs to see to get it right
QUERY = "fakelib 2.0.0 fetch_all import name"  # the router's GitHits query for the V1 failure
USAGE = {"input_tokens": 100, "output_tokens": 20, "model": "stand-in", "simulated": True}
GITHITS_SETTINGS = (githits.EXCLUDE_ENV, githits.EXCLUDE_REPLACE_ENV, githits.LICENSES_ENV, githits.SESSION_ENV,
                    githits.AGENT_ENV, githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV)

# The stand-in model. Modes: ``lookup`` (the default) gets it right only when the request's evidence shows fakelib's
# signature, repairs without it the wrong way (a positional timeout) and starts from the 1.x API; ``positional``
# always calls fetch positionally; ``missing`` imports a module installed nowhere until a web result names fakelib;
# ``empty_first`` answers its first call with no candidate, then behaves as ``lookup``. Every request is logged, and
# beside it the names of the GitHits variables and the signing seed the command was given.
STAND_IN = r'''
import json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
log = Path(LOG)
calls = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(request) + "\n")
with Path(LOG + ".env").open("a", encoding="utf-8") as f:
    f.write(json.dumps(sorted(name for name in os.environ
                              if name.startswith(("GITHITS_", "SYBERLABS_GITHITS_", "SYBERLABS_CERT")))) + "\n")
evidence = "\n".join(item.get("text", "") for item in request.get("evidence") or [])
feedback = request.get("feedback")
if MODE == "empty_first" and calls == 0:
    print(json.dumps({"candidates": [], "usage": USAGE}))
    sys.exit(0)
if MODE == "positional":
    text = POSITIONAL.format(n=calls)
elif MODE == "stubborn":  # keeps the 1.x import, in a new tree each time, until the evidence shows the signature
    text = RIGHT if SIGNATURE in evidence else V1.replace("fetch_all(url, 5)", "fetch_all(url, 5)  # %d" % calls)
elif MODE == "missing":
    text = RIGHT if "fakelib provides" in evidence else MISSING.replace("timeout=5", "timeout=%d" % (5 + calls))
elif SIGNATURE in evidence:
    text = RIGHT
elif feedback:
    text = POSITIONAL.format(n=calls)
else:
    text = V1
print(json.dumps({"candidates": [{"changes": {"src/client.py": text}, "message": "attempt %d" % calls}],
                  "recommended": [0], "usage": USAGE}))
'''

# A stand-in ``claude`` for the routed Claude Code provider: it writes the change in its working directory from what
# the prompt shows, and prints one assistant message and a result as Claude Code's stream-json does.
CLAUDE = r'''
import json, sys
from pathlib import Path
if "--version" in sys.argv:
    print("2.1.309 (Claude Code)")
    sys.exit(0)
prompt = sys.stdin.read()
log = Path(LOG)
calls = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"prompt": prompt}) + "\n")
if SIGNATURE in prompt:
    text = RIGHT
elif "did not pass the host's checks" in prompt:
    text = POSITIONAL.format(n=calls)
else:
    text = V1
Path("src").mkdir(exist_ok=True)
Path("src/client.py").write_bytes(text.encode("utf-8"))  # LF on every platform
usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
for event in ({"type": "system", "subtype": "init", "claude_code_version": "2.1.309", "model": "claude-opus-5",
               "effort": "high", "apiKeySource": "none", "mcp_servers": []},
              {"type": "assistant", "message": {"id": "m%d" % calls, "model": "claude-opus-5", "usage": usage,
                                                "content": [{"type": "text", "text": "done"}]}},
              {"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": 0.0}):
    print(json.dumps(event), flush=True)
'''


def write_dist(site: Path, name: str, version: str, files: dict, *, top: str | None = None,
               local: bool = False) -> Path:
    """One installed distribution in ``site``: its files and a ``dist-info`` with ``RECORD`` (and ``top_level.txt``
    when ``top`` is given); ``local`` adds PEP 610's ``direct_url.json``, as an install from a local path does."""
    for path, text in files.items():
        (site / path).parent.mkdir(parents=True, exist_ok=True)
        (site / path).write_text(text, encoding="utf-8")
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    listed = list(files) + [f"{info.name}/METADATA", f"{info.name}/RECORD"]
    if top:
        (info / "top_level.txt").write_text(top + "\n", encoding="utf-8")
        listed.append(f"{info.name}/top_level.txt")
    if local:
        (info / "direct_url.json").write_text('{"url": "file:///work/acme-ledger", "dir_info": {"editable": true}}',
                                              encoding="utf-8")
    (info / "RECORD").write_text("".join(f"{path},,\n" for path in listed), encoding="utf-8")
    return site


def write_site(site: Path) -> Path:
    """A ``site-packages`` holding fakelib 2.0.0 and its ``dist-info``."""
    return write_dist(site, "fakelib", "2.0.0", {"fakelib/__init__.py": FAKELIB}, top="fakelib")


def fakelib_hit(repo_url: str = "https://github.com/example/fakelib") -> dict:
    hit = search_hit("fetch", repo_url, file_path="fakelib/__init__.py", start_line=4, end_line=5)
    hit["summary"] = "fetch(url, *, timeout) replaced fetch_all in fakelib 2.0"
    return hit


def check(name: str, output: str, state: str = "failed", exit_code: int | None = 1) -> CheckResult:
    return CheckResult(name, state, exit_code, 10, output)


class RouterCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.site = write_site(self.folder / "env" / "lib" / "python3" / "site-packages")
        self.root = self.folder / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "client.py").write_text(STUB, encoding="utf-8")
        (self.root / "tests" / "test_client.py").write_text(textwrap.dedent(f'''\
            import os, sys, unittest
            sys.path.insert(0, {str(self.site)!r})
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
            import client


            class Client(unittest.TestCase):
                def test_get(self):
                    self.assertEqual(client.get("u"), "fetched u in 5s")
            '''), encoding="utf-8")
        git(self.folder, "init", "-q", "-b", "main", str(self.root))
        self.commit("initial")
        self.fake = FakeGitHits()
        self.addCleanup(self.fake.close)
        self.fake.serve("/v1/searches", search_response([fakelib_hit()]))
        self.fake.serve("/v1/read", read_code_response("https://github.com/example/fakelib",
                                                       body=f"{SIGNATURE}:\n    return url\n"))
        self.token = "ghtest_" + secrets.token_hex(16)

    def commit(self, message: str) -> None:
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", message)

    def kit(self, contract=None) -> Kit:
        home = self.root / ".syberlabs"
        if contract is not None:
            (home / "contracts").mkdir(parents=True, exist_ok=True)
            (home / "contracts" / f"{contract['id']}.v{contract['version']}.json").write_text(json.dumps(contract))
        kit = Kit.local(home, self.root, actor="dev@example.test")
        self.addCleanup(kit.close)
        return kit

    def contract(self, **budget) -> dict:
        doc = default_contract(self.root)
        doc["evolution"]["budget"].update(budget)
        return doc

    def adapter(self, name: str, mode: str = "lookup") -> list[str]:
        script = self.folder / f"{name}.py"
        script.write_text("\n".join(f"{key} = {value!r}" for key, value in (
            ("LOG", str(self.folder / f"{name}.log")), ("MODE", mode), ("SIGNATURE", SIGNATURE), ("V1", V1),
            ("POSITIONAL", POSITIONAL), ("RIGHT", RIGHT), ("MISSING", MISSING), ("USAGE", USAGE)))
            + "\n" + STAND_IN, encoding="utf-8")
        return [PY, str(script)]

    def requests(self, name: str) -> list[dict]:
        log = self.folder / f"{name}.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def environments(self, name: str) -> list[list[str]]:
        """Per call of the stand-in ``name``: the GitHits variables and signing seed its environment held."""
        log = self.folder / f"{name}.log.env"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def environ(self) -> dict:
        return {githits.TOKEN_ENV: self.token, githits.URL_ENV: self.fake.url}

    def route(self, **kwargs) -> router.GitHitsRoute:
        return router.GitHitsRoute(self.environ(), **kwargs)

    def routed(self, name="model", mode="lookup", **config) -> router.RouterProvider:
        settings = {"githits": self.route(session="thread-router"), **config}
        return router.RouterProvider(self.adapter(name, mode), router=router.RouterConfig(**settings))

    def facts(self, thread, key: str) -> list[dict]:
        return [e["body"] for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == key]

    def host_sees_fakelib(self):
        """The host interpreter can import fakelib, as when the host and the checks share one environment."""
        return mock.patch.object(sys, "path", [str(self.site), *sys.path])

    def journal_bytes(self) -> bytes:
        return b"".join(p.read_bytes() for p in (self.root / ".syberlabs").rglob("*") if p.is_file())


# ------------------------------------------------------------------------------------------------- the diagnosis

UNITTEST_IMPORT = textwrap.dedent('''\
    E
    ======================================================================
    ERROR: test_client (unittest.loader._FailedTest.test_client)
    ----------------------------------------------------------------------
    ImportError: Failed to import test module: test_client
    Traceback (most recent call last):
      File "/usr/lib/python3.13/unittest/loader.py", line 396, in _find_test_path
        module = self._get_module_from_name(name)
      File "/tmp/wt-1/tests/test_client.py", line 4, in <module>
        import client
      File "/tmp/wt-1/src/client.py", line 1, in <module>
        from fakelib import fetch_all
    ImportError: cannot import name 'fetch_all' from 'fakelib' ({site}/fakelib/__init__.py)

    ----------------------------------------------------------------------
    Ran 1 test in 0.001s

    FAILED (errors=1)
    ''')
FILES = ("src/client.py", "tests/test_client.py", "src/helpers.py")


def nowhere(_module, _sites):
    return None


class Diagnosis(unittest.TestCase):
    def diagnose(self, output, *, versions=nowhere, files=FILES, read=None, state="failed"):
        return router.diagnose([check("tests", output, state)], required=["tests"], files=files,
                               read=read or (lambda _path: None), versions=versions)

    def test_an_import_error_from_site_packages_is_a_third_party_failure_at_the_installed_version(self):
        with tempfile.TemporaryDirectory() as folder:
            site = write_site(Path(folder) / "site-packages")
            found = self.diagnose(UNITTEST_IMPORT.format(site=site), versions=router.resolve_version)
        self.assertEqual((found.check, found.kind, found.error, found.module, found.symbol, found.why, found.vouched),
                         ("tests", "third_party", "ImportError", "fakelib", "fetch_all", "message", "import"))
        self.assertEqual((found.distribution, found.version, found.version_source, found.pin, found.target),
                         ("fakelib", "2.0.0", "traceback_site", "installed", "pypi:fakelib@2.0.0"),
                         "the version is read from the dist-info beside the path the traceback printed")
        self.assertEqual(found.frames, {"repo": 2, "third_party": 0, "stdlib": 1, "unknown": 0})
        self.assertTrue(found.error_line.startswith("ImportError: cannot import name 'fetch_all'"))
        record = found.record()
        self.assertNotIn("fetch_all", json.dumps(record), "the symbol is journaled as a digest only")
        self.assertEqual((record["symbol_digest"], record["vouched"]), (router._digest("fetch_all"), "import"))

    def test_the_host_interpreters_metadata_is_the_fallback_and_says_so(self):
        output = ("Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 4, in get\n"
                  "    return fakelib.fetch(url, 5)\nTypeError: fetch() takes 1 positional argument but 2 were given\n")
        read = {"src/client.py": "import fakelib\n\n\ndef get(url):\n    return fakelib.fetch(url, 5)\n"}.get
        with tempfile.TemporaryDirectory() as folder:
            site = write_site(Path(folder) / "site-packages")
            with mock.patch.object(sys, "path", [str(site), *sys.path]):
                found = self.diagnose(output, versions=router.resolve_version, read=read)
        self.assertEqual((found.kind, found.module, found.symbol, found.why, found.version, found.version_source),
                         ("third_party", "fakelib", "fetch", "imported module in the failing line", "2.0.0",
                          "host_metadata"))
        unresolved = self.diagnose(output, read=read)
        self.assertEqual((unresolved.kind, unresolved.module, unresolved.version, unresolved.pin),
                         ("unknown", "fakelib", None, "unavailable"),
                         "an installed version is never guessed: unresolved, the failure is not a lookup's")

    def test_symbols_are_traced_to_their_module_through_the_failing_files_imports(self):
        versions = {"fakelib": ("fakelib", "2.0.0", "host_metadata")}.get
        resolve = lambda module, _sites: versions(module.split(".")[0])  # noqa: E731  (a dotted module comes in)
        read = {"src/client.py": "from fakelib import fetch as get_it\nfrom fakelib.api import Session\n"
                                 "from . import helpers\n"}.get
        frame = '  File "/tmp/w/src/client.py", line 9, in get\n    return get_it(url, retries=3)\n'
        cases = [
            ("TypeError: fetch() got an unexpected keyword argument 'retries'", "unknown", None, None),
            ("TypeError: get_it() got an unexpected keyword argument 'retries'", "third_party", "import binding",
             "fetch"),  # the library's own name, never the repository's alias for it
            ("AttributeError: type object 'Session' has no attribute 'mount_all'", "third_party", "import binding",
             "Session.mount_all"),
            ("AttributeError: 'Session' object has no attribute 'mount_all'", "third_party", "import binding",
             "Session.mount_all"),
            ("AttributeError: 'Response' object has no attribute 'jsonn'", "unknown", None, None),
            ("AttributeError: module 'helpers' has no attribute 'nope'", "repo", "message", None),
            ("AttributeError: module 'fakelib' has no attribute 'fetch_all'", "third_party", "message", "fetch_all"),
        ]
        for line, kind, why, symbol in cases:
            with self.subTest(line=line):
                found = self.diagnose("Traceback (most recent call last):\n" + frame + line + "\n", versions=resolve,
                                      read=read)
                self.assertEqual((found.kind, found.why, found.symbol), (kind, why, symbol))
        raised = ('Traceback (most recent call last):\n  File "/tmp/w/src/client.py", line 9, in get\n'
                  '    return fakelib.fetch(url, timeout="5")\n'
                  '  File "/venv/lib/python3.12/site-packages/fakelib/core.py", line 20, in fetch\n'
                  '    raise TypeError("timeout must be a number")\nTypeError: timeout() takes 1 positional argument\n')
        found = self.diagnose(raised, versions=resolve)
        self.assertEqual((found.kind, found.module, found.why, found.symbol),
                         ("third_party", "fakelib", "raised in a third-party frame", None),
                         "the library raised it: a name is sent only when the library's own files show it, and this "
                         "resolver gave no files to read")

    def test_repo_stdlib_missing_and_unknown_failures(self):
        cases = {
            "repo": 'Traceback (most recent call last):\n  File "/tmp/w/tests/test_client.py", line 9, in test_get\n'
                    "    self.assertEqual(client.get('u'), 'x')\nAssertionError: 'y' != 'x'\n",
            "stdlib": "Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 2, in <module>\n"
                      "    json.loadz('1')\nAttributeError: module 'json' has no attribute 'loadz'\n",
            "missing_dependency": "Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 1, "
                                  "in <module>\n    import fakelib_two\n"
                                  "ModuleNotFoundError: No module named 'fakelib_two'\n",
            "unknown": "npm ERR! code ELIFECYCLE\nnpm ERR! errno 1\nError: Cannot find module 'left-pad'\n",
        }
        for kind, output in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(self.diagnose(output).kind, kind)
        own = self.diagnose("Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 1, in <module>\n"
                            "    import helpers.extra\nModuleNotFoundError: No module named 'helpers.extra'\n")
        self.assertEqual((own.kind, own.module), ("repo", "helpers"), "a module the repository's own files make")
        syntax = self.diagnose('  File "/tmp/w/src/client.py", line 3\n    return fetch(\n           ^\n'
                               "SyntaxError: '(' was never closed\n")
        self.assertEqual((syntax.kind, syntax.error), ("repo", "SyntaxError"))
        timed = self.diagnose("", state="timed_out")
        self.assertEqual((timed.kind, timed.error, timed.why), ("unknown", None, "no Python exception in the output"))

    def test_the_last_traceback_counts_and_pytest_and_windows_paths_are_read(self):
        chained = ('Traceback (most recent call last):\n  File "/venv/lib/python3.12/site-packages/other/x.py", '
                   "line 1, in f\n    raise KeyError('a')\nKeyError: 'a'\n\nDuring handling of the above exception, "
                   'another exception occurred:\n\nTraceback (most recent call last):\n  File "/tmp/w/src/client.py", '
                   "line 4, in get\n    raise ValueError('b')\nValueError: b\n")
        found = router.parse_failure(chained)
        self.assertEqual((found.error, found.message, [frame[0] for frame in found.frames]),
                         ("ValueError", "b", ["/tmp/w/src/client.py"]))
        short = ("tests/test_client.py:9: in test_get\n    self.assertEqual(client.get('u'), 'x')\n"
                 "src/client.py:4: in get\n    return fakelib.fetch(url, 5)\n"
                 "E   TypeError: fetch() takes 1 positional argument but 2 were given\n")
        found = router.parse_failure(short)
        self.assertEqual((found.error, found.frames[-1][:2], found.frames[-1][3]),
                         ("TypeError", ("src/client.py", 4), "return fakelib.fetch(url, 5)"))
        long = ("    def get(url):\n>       return fakelib.fetch_all(url)\n"
                "E       AttributeError: module 'fakelib' has no attribute 'fetch_all'\n\n"
                "src/client.py:3: AttributeError\n========================= short test summary info ===\n"
                "FAILED tests/test_client.py::Client::test_get - AttributeError: module 'fakelib' has no attribute\n")
        found = router.parse_failure(long)
        self.assertEqual((found.error, found.frames), ("AttributeError", (("src/client.py", 3, "",
                                                                           "return fakelib.fetch_all(url)"),)))
        windows = ('Traceback (most recent call last):\n  File "C:\\work\\wt\\src\\client.py", line 1, in <module>\n'
                   "    from fakelib import fetch_all\nImportError: cannot import name 'fetch_all' from 'fakelib' "
                   "(C:\\Users\\dev\\venv\\Lib\\site-packages\\fakelib\\__init__.py)\n")
        seen = []
        found = self.diagnose(windows, versions=lambda module, sites: seen.append(list(sites)) or None)
        self.assertEqual(found.frames["repo"], 1)
        self.assertEqual(seen, [["C:\\Users\\dev\\venv\\Lib\\site-packages"]], "the site as printed, for the dist-info")
        frame = router.classify_frame("C:\\Users\\dev\\venv\\Lib\\site-packages\\fakelib\\api.py", 3, "f", "", set())
        self.assertEqual((frame.where, frame.package, frame.path), ("third_party", "fakelib", "fakelib/api.py"))
        pytest_windows = ("C:\\work\\wt\\src\\client.py:4: in get\n    return fakelib.fetch(url, 5)\n"
                          "C:\\Users\\dev\\venv\\Lib\\site-packages\\fakelib\\api.py:5: in fetch\n"
                          "    raise TypeError('timeout')\n"
                          "E   TypeError: fetch() takes 1 positional argument but 2 were given\n")
        found = router.parse_failure(pytest_windows)
        self.assertEqual([(path, line) for path, line, _function, _source in found.frames],
                         [("C:\\work\\wt\\src\\client.py", 4),
                          ("C:\\Users\\dev\\venv\\Lib\\site-packages\\fakelib\\api.py", 5)],
                         "pytest's frames with a drive letter are read as on POSIX")
        seen = []
        found = self.diagnose(pytest_windows, versions=lambda module, sites: seen.append((module, list(sites))))
        self.assertEqual((found.kind, found.why), ("third_party", "raised in a third-party frame"))
        self.assertEqual(seen, [("fakelib.api", ["C:\\Users\\dev\\venv\\Lib\\site-packages"])])

    def test_the_signature_ignores_paths_addresses_and_timings_and_not_the_failure(self):
        first = UNITTEST_IMPORT.format(site="/tmp/a/site-packages").replace("wt-1", "wt-1")
        second = UNITTEST_IMPORT.format(site="/opt/b/site-packages").replace("wt-1", "wt-9").replace("0.001s", "0.4s")
        other = first.replace("fetch_all", "fetch_many")
        signatures = [self.diagnose(text).signature for text in (first, second, other)]
        self.assertEqual(signatures[0], signatures[1])
        self.assertNotEqual(signatures[0], signatures[2])
        found = router.diagnose([check("lint", "E501 line too long"), check("tests", UNITTEST_IMPORT.format(
            site="/s/site-packages"))], required=["tests"], files=FILES, versions=nowhere)
        self.assertEqual(found.check, "tests", "the required check is read first")
        lint_only = router.diagnose([check("lint", "E501 line too long")], files=FILES, versions=nowhere)
        self.assertNotEqual(found.signature, lint_only.signature, "every failing check is in the signature")

    def test_imports_are_read_not_run(self):
        self.assertEqual(router.import_bindings("import a.b\nimport c.d as e\nfrom f.g import h as i, j\n"
                                                "from .k import l\nfrom . import m\nfrom n import *\n"),
                         {"a": "a", "e": "c", "i": "f", "j": "f", "l": ".k", "m": "."})
        self.assertEqual(router.import_bindings("import os\nraise SystemExit('never run')\n"), {"os": "os"})
        self.assertEqual(router.import_bindings("def broken(:\n"), {})
        origins = router.import_origins("import a.b\nimport c.d as e\nfrom f.g import h as i\nfrom . import m\n")
        self.assertEqual(origins, {"a": ("a", None), "e": ("c.d", None), "i": ("f.g", "h"), "m": (".", "m")})
        self.assertEqual(router.defined_names("class Ledger:\n    def post(self):\n        self.total = 1\n"
                                              "async def sync():\n    pass\nLIMIT: int = 3\nx, (y, z) = 1, (2, 3)\n"),
                         frozenset({"Ledger", "post", "total", "sync", "LIMIT", "x", "y", "z"}))
        deep = "x = " + "+".join(["1"] * 100000)  # ast.parse raises RecursionError on it (MemoryError for others)
        self.assertEqual((router.import_bindings(deep), router.import_origins(deep), router.defined_names(deep)),
                         ({}, {}, frozenset()), "text the parser cannot read is read as nothing, never a crash")
        self.assertEqual(router.repo_modules(["src/pkg/__init__.py", "src/tool.py", "vendor/site-packages/x/y.py",
                                              "docs/a-b.md", "fixtures/requests/data.json"]),
                         frozenset({"src", "pkg", "__init__", "tool"}),
                         "only Python files and the directories on their way make a module")


def site_with(folder: Path, **libraries) -> Path:
    """A ``site-packages`` with fakelib 2.0.0 whose ``__init__.py`` holds ``FAKELIB`` and the given text."""
    site = folder / "site-packages"
    write_dist(site, "fakelib", "2.0.0", {"fakelib/__init__.py": FAKELIB + libraries.get("extra", "")},
               top="fakelib")
    return site


class Privacy(unittest.TestCase):
    """A query names a published library's API and nothing of the repository's (``router.vouch``)."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def raised(self, site: Path, message: str) -> str:
        return ('Traceback (most recent call last):\n  File "/tmp/w/src/client.py", line 9, in get\n'
                '    return fakelib.each(rows, compute_acme_customer_ssn_hash)\n'
                f'  File "{site}/fakelib/__init__.py", line 9, in each\n    return [callback(row) for row in rows]\n'
                f"{message}\n")

    def diagnose(self, output, **kwargs):
        kwargs.setdefault("files", FILES)
        return router.diagnose([check("tests", output)], required=["tests"], versions=router.resolve_version,
                               **kwargs)

    def test_a_name_from_the_failure_is_sent_only_when_it_is_the_librarys(self):
        site = site_with(self.folder, extra="\n\ndef serialize(entry):\n    return entry.to_dict()\n")
        callback = self.diagnose(self.raised(site, "TypeError: compute_acme_customer_ssn_hash() takes 0 positional "
                                                   "arguments but 1 was given"))
        self.assertEqual((callback.kind, callback.distribution, callback.symbol, callback.why),
                         ("third_party", "fakelib", None, "raised in a third-party frame"),
                         "the library raised it on the repository's callback: the callback's name is not sent")
        entry = self.diagnose(self.raised(site, "AttributeError: 'AcmeInternalLedgerEntry' object has no attribute "
                                                "'to_dict'"))
        self.assertEqual((entry.symbol, entry.vouched), ("to_dict", "library"),
                         "the attribute the library's own code asks for is kept; the caller's class is not")
        imported = ("Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 1, in <module>\n"
                    "    from fakelib import compute_acme_customer_ssn_hash\nImportError: cannot import name "
                    f"'compute_acme_customer_ssn_hash' from 'fakelib' ({site}/fakelib/__init__.py)\n")
        self.assertEqual(self.diagnose(imported).symbol, "compute_acme_customer_ssn_hash",
                         "a name the code imports from the library is the library's by the code's own account")
        defined = self.diagnose(imported, defined={"compute_acme_customer_ssn_hash"})
        self.assertEqual((defined.kind, defined.symbol), ("third_party", None),
                         "unless the repository defines that name itself: then it is the repository's")
        for found in (callback, defined):
            self.assertEqual(found.target, "pypi:fakelib@2.0.0")

    def test_a_local_install_and_a_missing_module_are_never_named_outward(self):
        site = self.folder / "site-packages"
        write_dist(site, "acme-ledger", "0.3.0", {"acme_ledger/__init__.py": "def post():\n    pass\n"},
                   top="acme_ledger", local=True)
        local = self.diagnose("Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 1, in "
                              "<module>\n    from acme_ledger import post_all\nImportError: cannot import name "
                              f"'post_all' from 'acme_ledger' ({site}/acme_ledger/__init__.py)\n")
        self.assertEqual((local.kind, local.distribution, local.version, local.symbol, local.target),
                         ("local", None, None, None, None),
                         "an install from a local path or a URL is not a published release: possibly private")
        self.assertEqual(router.resolve_version("acme_ledger", [str(site)]).published, False)
        missing = self.diagnose("Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 1, in "
                                "<module>\n    import acme_internal_ledger_v2\n"
                                "ModuleNotFoundError: No module named 'acme_internal_ledger_v2'\n")
        self.assertEqual((missing.kind, missing.symbol, missing.distribution), ("missing_dependency", None, None))

    def test_the_distribution_that_installed_the_module_is_the_one_pinned(self):
        site = self.folder / "site-packages"
        write_dist(site, "acme-one", "1.0", {"acme/one/__init__.py": "ONE = 1\n"}, top="acme")
        write_dist(site, "acme-two", "2.0", {"acme/two/__init__.py": "def go():\n    pass\n"}, top="acme")
        write_dist(site, "fakelib", "2.0.0", {"fakelib/__init__.py": FAKELIB}, top="fakelib")
        resolved = {module: router.resolve_version(module, [str(site)]) for module in
                    ("acme.two", "acme.two.sub", "acme.one", "acme", "acme.three", "fakelib.missing_sub")}
        self.assertEqual({module: (found.distribution, found.version, found.source) if found else None
                          for module, found in resolved.items()},
                         {"acme.two": ("acme-two", "2.0", "traceback_site"),
                          "acme.two.sub": ("acme-two", "2.0", "traceback_site"),
                          "acme.one": ("acme-one", "1.0", "traceback_site"),
                          "acme": None, "acme.three": None,
                          "fakelib.missing_sub": ("fakelib", "2.0.0", "traceback_site")},
                         "a namespace package shared by two distributions is never guessed to be one of them")
        self.assertEqual(resolved["acme.two"].files, (str(site / "acme" / "two" / "__init__.py"),))

    def test_a_site_path_outranks_a_repository_module_of_the_same_name(self):
        files = FILES + ("src/celery.py",)
        resolve = lambda module, _sites: ("celery", "5.4.0", "traceback_site") \
            if module.split(".")[0] == "celery" else None  # noqa: E731
        output = ("Traceback (most recent call last):\n  File \"/tmp/w/src/client.py\", line 1, in <module>\n"
                  "    from celery import shared_taskz\nImportError: cannot import name 'shared_taskz' from 'celery' "
                  "(/venv/lib/python3.12/site-packages/celery/__init__.py)\n")
        found = router.diagnose([check("tests", output)], files=files, versions=resolve)
        self.assertEqual((found.kind, found.distribution, found.symbol), ("third_party", "celery", "shared_taskz"))
        bare = output.replace(" (/venv/lib/python3.12/site-packages/celery/__init__.py)", "")
        self.assertEqual(router.diagnose([check("tests", bare)], files=files, versions=resolve).kind, "repo",
                         "nothing places it in a site: the repository's own module wins, and it is repaired")


# --------------------------------------------------------------------------------------------------- the policy

def situation(kind="third_party", eligible=("repair", "lookup", "stop"), repeats=1, **diagnosis):
    found = router.Diagnosis("tests", "failed", kind, error=diagnosis.pop("error", "ImportError"),
                             module=diagnosis.pop("module", "fakelib"), version=diagnosis.pop("version", "2.0.0"),
                             **diagnosis)
    return router.Situation(decision=2, calls=1, calls_left=5, room=True, diagnosis=found, score=(0, 0), best=(0, 0),
                            repeats=repeats, eligible=tuple(eligible), lookups_used=0, max_lookups=2,
                            routes={"githits": "ready", "web": "none"})


class Policy(unittest.TestCase):
    def test_the_baseline_rules_in_order(self):
        policy = router.BaselinePolicy()
        cases = [
            (situation(repeats=3), "stop", "stop.no_progress"),
            (situation(repeats=2), "lookup", "lookup.third_party"),
            (situation(eligible=("repair", "lookup", "web", "stop")), "lookup", "lookup.third_party"),
            (situation("missing_dependency", eligible=("repair", "web", "stop"), error="ModuleNotFoundError"),
             "repair", "repair.default"),  # a module installed nowhere is never searched for: its name may be private
            (situation("local", eligible=("repair", "lookup", "web", "stop")), "repair", "repair.default"),
            (situation(eligible=("repair", "web", "stop")), "web", "web.third_party"),
            (situation(eligible=("repair", "stop")), "repair", "repair.default"),
            (situation("missing_dependency", eligible=("repair", "stop")), "repair", "repair.default"),
            (situation("repo", eligible=("repair", "stop"), error="AssertionError"), "repair", "repair.repo"),
            (situation("stdlib", eligible=("repair", "stop")), "repair", "repair.default"),
            (situation("unknown", eligible=("repair", "stop")), "repair", "repair.default"),
        ]
        for found, action, rule in cases:
            with self.subTest(rule=rule, kind=found.diagnosis.kind, eligible=found.eligible):
                choice = policy.decide(found)
                self.assertEqual((choice.action, choice.rule), (action, rule))
                self.assertTrue(choice.reason and len(choice.reason) <= router.MAX_REASON)
        self.assertEqual(router.BaselinePolicy(patience=1).decide(situation(repeats=2)).rule, "stop.no_progress")
        for bad in (0, -1, 1.5, True):
            with self.assertRaises(Rejected):
                router.BaselinePolicy(patience=bad)
        self.assertEqual(router.RULES[:2], ("stop.budget", "stop.calls"), "the router's own stops come first")

    def test_a_lookup_query_names_the_package_version_and_symbol_and_nothing_else(self):
        found = router.Diagnosis("tests", "failed", "third_party", error="ImportError", module="fakelib",
                                 symbol="fetch_all", distribution="fakelib", version="2.0.0",
                                 version_source="traceback_site")
        need = router.githits_need(found)
        self.assertEqual((need.query, need.target, need.route), (QUERY, "pypi:fakelib@2.0.0", "githits"))
        self.assertEqual(query_guard.judge(need.query), query_guard.PASS)
        unpinned = router.githits_need(router.Diagnosis("tests", "failed", "third_party", error="TypeError",
                                                        module="fakelib", symbol="Session.get", distribution="fakelib"))
        self.assertEqual((unpinned.query, unpinned.target, unpinned.record()["pin"]),
                         ("fakelib Session.get signature parameters", "pypi:fakelib", "unavailable"))
        namespaced = router.githits_need(router.Diagnosis("tests", "failed", "third_party", error="AttributeError",
                                                          module="acme", symbol="Blob.upload",
                                                          distribution="acme-two", version="2.0"))
        self.assertEqual(namespaced.query, "acme-two 2.0 Blob.upload attribute", "the distribution, not 'acme'")
        web = router.web_need(found)
        self.assertEqual((web.query, web.target, web.key), (f"python {QUERY}", None, found.need()),
                         "the web is asked the same question, which the holdout draws once for both routes")
        self.assertEqual(need.key, found.need())
        self.assertNotIn(QUERY, json.dumps(need.record()), "the record holds the query's digest and length only")
        self.assertEqual(need.record()["query_chars"], len(QUERY))


# -------------------------------------------------------------------------------------------- routing, end to end

class Routing(RouterCase):
    def test_a_third_party_failure_is_looked_up_at_its_installed_version_and_the_next_attempt_passes(self):
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed()
        found = thread.propose(provider, seed=3)
        self.assertEqual([(c.id, c.evaluation, c.parents) for c in found], [("c1", "failed", ()),
                                                                            ("c2", "passed", ("c1",))])
        self.assertEqual((found[0].provider, found[0].provider_revision), ("repair-router", "1"))
        first, second = self.requests("model")
        self.assertNotIn("evidence", first)
        self.assertNotIn("feedback", first)
        self.assertEqual(second["feedback"]["candidate"], "c1", "a lookup's call is still a directed repair")
        [evidence] = second["evidence"]
        self.assertEqual({key: evidence[key] for key in ("route", "decision", "target", "pin")},
                         {"route": "githits", "decision": 2, "target": "pypi:fakelib@2.0.0",
                          "pin": "2.0.0 (traceback_site)"})
        self.assertIn(SIGNATURE, evidence["text"], "the read of the top hit")
        self.assertIn("replaced fetch_all", evidence["text"], "the search hit's summary")
        # What reached GitHits: one pinned search, then one read of the top hit, with the router's own headers.
        search, read = self.fake.requests
        self.assertEqual((search["method"], search["path"], read["method"], read["path"]),
                         ("POST", "/v1/searches", "GET", "/v1/read"))
        self.assertEqual(json.loads(search["body"]), {"query": QUERY, "limit": 5, "targets": [
            {"registry": "pypi", "name": "fakelib", "version": "2.0.0"}]})
        self.assertEqual({k: v[0] for k, v in read["query"].items()},
                         {"target": "pypi:fakelib@2.0.0", "path": "fakelib/__init__.py", "start_line": "4",
                          "end_line": "5"})
        for request in (search, read):
            self.assertEqual((request["headers"]["authorization"], request["headers"]["x-githits-agent"],
                              request["headers"]["x-githits-session-id"]),
                             (f"Bearer {self.token}", "syberfactory/router", "thread-router"))
        # The journal: a decision before each call, the lookup's calls from the client's own records.
        routes = self.facts(thread, router.ROUTE)
        self.assertEqual([(b["version"], b["source"], b["actor"], b["verified"]) for b in routes],
                         [("s1.r1", "syberlabs.host", "syberlabs.host", True),
                          ("s1.r2", "syberlabs.host", "syberlabs.host", True)])
        propose, lookup = (b["value"] for b in routes)
        self.assertEqual((propose["action"], propose["rule"], propose["after"], propose["diagnosis"]),
                         ("propose", "propose.first", None, None))
        self.assertEqual((lookup["action"], lookup["rule"], lookup["after"], lookup["call"], lookup["eligible"]),
                         ("lookup", "lookup.third_party", "c1", 1, ["repair", "lookup", "stop"]))
        diagnosis = lookup["diagnosis"]
        self.assertEqual({key: diagnosis[key] for key in ("check", "kind", "error", "module", "distribution",
                                                          "version", "version_source", "pin", "why")},
                         {"check": "tests", "kind": "third_party", "error": "ImportError", "module": "fakelib",
                          "distribution": "fakelib", "version": "2.0.0", "version_source": "traceback_site",
                          "pin": "installed", "why": "message"})
        self.assertEqual(lookup["lookup"], {"route": "githits", "target": "pypi:fakelib@2.0.0", "pin": "installed",
                                            "query_digest": router._digest(QUERY), "query_chars": len(QUERY),
                                            "guard": "pass"})
        self.assertEqual((lookup["routes"], lookup["score"], lookup["repeats"], lookup["carried"]),
                         ({"githits": "ready", "web": "none"}, [0, 0], 1, []))
        self.assertEqual((propose["last_outcome"], lookup["last_outcome"]), (None, "failed"))
        self.assertEqual(lookup["diagnosis"]["vouched"], "import")
        calls = self.facts(thread, router.LOOKUP)
        self.assertEqual([(b["version"], b["value"]["tool"], b["value"]["status"], b["value"]["ok"],
                           b["value"]["guard"], b["value"]["results_returned"]) for b in calls],
                         [("s1.r2.1", "githits.search", "ok", True, "pass", 1),
                          ("s1.r2.2", "githits.read", "ok", True, "pass", 1)])
        history = thread.history()
        order = [(e["kind"], e["body"].get("key")) for e in history if e["kind"] in ("observed", "candidate_registered")
                 and e["body"].get("key") in (None, "route", "lookup")]
        self.assertEqual(order, [("observed", "route"), ("candidate_registered", None), ("observed", "route"),
                                 ("observed", "lookup"), ("observed", "lookup"), ("candidate_registered", None)],
                         "each decision is recorded before its lookup, and the lookup before the call it informs")
        record = self.journal_bytes()
        for text in (QUERY.encode(), SIGNATURE.encode(), b"replaced fetch_all", self.token.encode()):
            self.assertNotIn(text, record, "no query, result text or token in the journal")
        self.assertEqual(validate_events(history, "router"), [])
        self.assertTrue(verify_events(history))
        [item] = metrics.work_items(history)
        self.assertEqual((item.githits_lookups.value, item.lookups_blocked.value), (2, 0),
                         "metrics count the router's GitHits calls host-observed, as the launcher route's")
        self.assertEqual(provider.trace[-1], {"stopped": "passing", "calls": 2, "refused": {}, "decisions": 2,
                                              "lookups": 1})
        self.assertEqual([(e["decision"], e["action"], e["evidence"]) for e in provider.trace[:-1]],
                         [(1, "propose", 0), (2, "lookup", 1)])
        self.assertEqual([d["action"] for d in provider.decisions], ["propose", "lookup"])
        self.assertTrue(thread.check("c2").acceptable)
        # Attribution: did the attempt after the lookup pass, and at what cost?
        rows = router.attribution(history)
        self.assertEqual([(r["decision"], r["action"], r["after"]["outcome"], r["calls_to_pass"]) for r in rows],
                         [(1, "propose", "failed", 2), (2, "lookup", "passed", 1)])
        after = rows[1]["after"]
        self.assertEqual((after["candidate"], after["score_before"], after["score_after"], after["gained"],
                          after["trigger_fixed"], after["same_failure"], after["input_tokens"], after["output_tokens"]),
                         ("c2", [0, 0], [1, 1], 1, True, False, 100, 20))
        self.assertEqual((rows[1]["lookups"]["calls"], rows[1]["lookups"]["ok"], rows[1]["lookups"]["results"],
                          rows[1]["lookups"]["tools"]), (2, 2, 2, ["githits.read", "githits.search"]))
        self.assertIsInstance(after["evaluation_ms"], int)
        self.assertEqual((after["evidence_passed"], after["registered"], after["booked_dollars"], after["calls_booked"],
                          rows[1]["dollars_to_pass"]), (True, True, None, 1, None),
                         "an unpriced contract books no dollars")
        found = router.summary(rows)
        self.assertEqual((found["lookup"]["decisions"], found["lookup"]["passed"], found["lookup"]["pass_rate"],
                          found["lookup"]["calls_per_lookup"], found["lookup"]["with_results"],
                          found["lookup"]["evidence_passed"]), (1, 1, 1.0, 2.0, 1, 1))
        self.assertEqual((found["by_action"]["propose"]["passed"], found["holdout"], found["web"]["decisions"]),
                         (0, None, 0))

    def test_without_the_read_the_summary_alone_did_not_say_enough_and_the_record_shows_it(self):
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed(githits=self.route(read_top=False))
        provider.max_calls = 3
        found = thread.propose(provider, seed=1)
        self.assertEqual([c.evaluation for c in found], ["failed", "failed", "failed"])
        self.assertEqual([r["path"] for r in self.fake.requests], ["/v1/searches"], "one search, no read")
        rows = router.attribution(thread.history())
        self.assertEqual([(r["action"], r["rule"], r["after"]["outcome"]) for r in rows],
                         [("propose", "propose.first", "failed"), ("lookup", "lookup.third_party", "failed"),
                          ("repair", "repair.default", "failed"), ("stop", "stop.calls", None)],
                         "the second failure (a positional timeout) is not resolvable on this host: repair, then stop")
        self.assertEqual(rows[1]["after"]["trigger_fixed"], False)
        self.assertEqual([row["after"]["same_failure"] for row in rows], [None, False, True, None],
                         "the import failure became a call failure after the lookup, which then came back unchanged")
        self.assertEqual(self.requests("model")[2].get("evidence"), None,
                         "the lookup answered the import failure; it is not carried to a different failure")
        self.assertEqual(router.summary(rows)["lookup"]["pass_rate"], 0.0)

    def test_two_failures_two_lookups_when_the_host_sees_the_library(self):
        """The 1.x import, then a positional timeout: each a new question, each looked up once."""
        self.fake.script["/v1/read"] = lambda request: (
            200, {"Content-Type": "application/json", "X-Request-ID": "r"},
            json.dumps(read_code_response("https://github.com/example/fakelib", body="def fetch_all(url):\n")
                       if len(self.fake.requests) < 3 else
                       read_code_response("https://github.com/example/fakelib", body=f"{SIGNATURE}:\n")).encode())
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        with self.host_sees_fakelib():
            found = thread.propose(self.routed(), seed=2)
        self.assertEqual([c.evaluation for c in found], ["failed", "failed", "passed"])
        routes = [b["value"] for b in self.facts(thread, router.ROUTE)]
        self.assertEqual([(v["action"], (v["diagnosis"] or {}).get("error"),
                           (v["diagnosis"] or {}).get("version_source")) for v in routes],
                         [("propose", None, None), ("lookup", "ImportError", "traceback_site"),
                          ("lookup", "TypeError", "host_metadata")])
        searches = [json.loads(r["body"])["query"] for r in self.fake.requests if r["path"] == "/v1/searches"]
        self.assertEqual(searches, [QUERY, "fakelib 2.0.0 fetch signature parameters"])
        self.assertEqual(router.summary(router.attribution(thread.history()))["lookup"]["passed"], 1)

    def test_a_name_the_repository_defines_never_leaves_the_machine(self):
        """The library calls the repository's callback with the wrong arity: the failure names the callback, which is
        the repository's, so neither GitHits nor the web is asked about it."""
        script = self.folder / "callback.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            from pathlib import Path
            LOG = {str(self.folder / "callback.log")!r}
            json.load(sys.stdin)
            log = Path(LOG)
            calls = log.read_text().count("\\n") if log.exists() else 0
            with log.open("a") as f:
                f.write("x\\n")
            text = ("import fakelib\\n\\n\\ndef compute_acme_customer_ssn_hash():\\n    return 0\\n\\n\\n"
                    "def get(url):\\n    return fakelib.each([url], compute_acme_customer_ssn_hash)  # %d\\n" % calls)
            print(json.dumps({{"candidates": [{{"changes": {{"src/client.py": text}}}}], "usage": {{
                "input_tokens": 100, "output_tokens": 20, "model": "stand-in", "simulated": True}}}}))
            '''), encoding="utf-8")
        queries = []

        class Search:
            name = "web"

            def search(self, query, *, timeout):
                queries.append(query)
                return router.WebAnswer("ok", "anything", 1, "w")

        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = router.RouterProvider([PY, str(script)], max_calls=2,
                                         router=router.RouterConfig(githits=self.route(), web=Search()))
        thread.propose(provider, seed=1)
        second = [b["value"] for b in self.facts(thread, router.ROUTE)][1]
        diagnosis = second["diagnosis"]
        self.assertEqual((diagnosis["kind"], diagnosis["distribution"], diagnosis["error"], diagnosis["why"],
                          diagnosis["symbol_digest"], diagnosis["vouched"]),
                         ("third_party", "fakelib", "TypeError", "raised in a third-party frame", None, None))
        self.assertEqual((second["action"], second["rule"], second["eligible"]),
                         ("repair", "repair.default", ["repair", "stop"]))
        self.assertEqual((self.fake.requests, queries), ([], []))

    def test_refused_proposals_keep_their_decisions_and_attribution(self):
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        found = thread.propose(self.routed(mode="empty_first"), seed=1)
        self.assertEqual([c.evaluation for c in found], ["failed", "passed"])
        rows = router.attribution(thread.history())
        self.assertEqual([(r["rule"], r["after"]["outcome"], r["calls_to_pass"], r["after"]["calls_booked"])
                          for r in rows],
                         [("propose.first", "refused:no_change", 3, None), ("propose.again", "failed", 2, 2),
                          ("lookup.third_party", "passed", 1, 1)],
                         "the refused call's usage is booked at the next registration, which covers two calls")
        self.assertEqual(validate_events(thread.history(), "router-refused"), [])

    def test_the_attempt_is_the_first_items_and_its_cost_is_what_the_host_booked(self):
        """Two candidates in one answer: the second is refused ``limit`` before the first is submitted; the first is
        the attempt, here refused by the host as the parent's own tree. Each call costs $1.00 on a priced contract."""
        script = self.folder / "pair.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            from pathlib import Path
            V1, RIGHT, LOG = {V1!r}, {RIGHT!r}, {str(self.folder / "pair.log")!r}
            json.load(sys.stdin)
            log = Path(LOG)
            calls = log.read_text().count("\\n") if log.exists() else 0
            with log.open("a") as f:
                f.write("x\\n")
            items = ([{{"changes": {{"src/client.py": V1}}}}] if calls == 0 else
                     [{{"changes": {{"src/client.py": V1}}}}, {{"changes": {{"src/client.py": RIGHT}}}}] if calls == 1
                     else [{{"changes": {{"src/client.py": RIGHT}}}}])
            print(json.dumps({{"candidates": items, "usage": {{"input_tokens": 100, "output_tokens": 20,
                                                            "model": "stand-in", "simulated": True}}}}))
            '''), encoding="utf-8")
        prices = {"stand-in": {"input_per_million": 10000, "output_per_million": 0}}  # $1.00 per call
        kit = self.kit(self.contract(prices=prices))
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = router.RouterProvider([PY, str(script)], router=router.RouterConfig(githits=self.route()))
        thread.propose(provider, seed=1)
        refusals = [b["value"]["reason"] for b in self.facts(thread, "proposal_refused")]
        self.assertEqual(refusals, ["limit", "no_change"])
        rows = router.attribution(thread.history())
        self.assertEqual([(r["action"], r["after"]["outcome"], r["after"]["booked_dollars"], r["after"]["calls_booked"],
                           r["after"]["input_tokens"], r["calls_to_pass"], r["dollars_to_pass"]) for r in rows],
                         [("propose", "failed", 1.0, 1, 100, 3, 3.0),
                          ("lookup", "refused:no_change", None, None, None, 2, 2.0),
                          ("repair", "passed", 2.0, 2, 100, 1, 2.0)],
                         "the host's refusal, not limit; the refused call's dollar is booked at the next attempt")
        self.assertEqual(rows[1]["after"]["evidence_passed"], True, "the next attempt carried the lookup and passed")
        self.assertEqual(rows[2]["carried"], [2])
        self.assertEqual(router.summary(rows)["lookup"]["evidence_passed"], 1)

    def test_an_attempt_outside_the_scope_is_recorded_as_such(self):
        script = self.folder / "outside.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            from pathlib import Path
            V1, RIGHT, LOG = {V1!r}, {RIGHT!r}, {str(self.folder / "outside.log")!r}
            json.load(sys.stdin)
            log = Path(LOG)
            calls = log.read_text().count("\\n") if log.exists() else 0
            with log.open("a") as f:
                f.write("x\\n")
            changes = [{{"src/client.py": V1}}, {{"README.md": "out of scope\\n"}}, {{"src/client.py": RIGHT}}][calls]
            print(json.dumps({{"candidates": [{{"changes": changes}}], "usage": {{"input_tokens": 100,
                              "output_tokens": 20, "model": "stand-in", "simulated": True}}}}))
            '''), encoding="utf-8")
        contract = self.contract()
        contract["evolution"]["scope"]["paths"] = ["src/", "tests/"]
        kit = self.kit(contract)
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        thread.propose(router.RouterProvider([PY, str(script)], router=router.RouterConfig(githits=self.route())),
                       seed=1)
        rows = router.attribution(thread.history())
        self.assertEqual([(r["action"], r["after"]["outcome"]) for r in rows],
                         [("propose", "failed"), ("lookup", "out_of_scope"), ("repair", "passed")])

    def test_a_provider_error_is_the_attempts_outcome(self):
        script = self.folder / "crash.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            from pathlib import Path
            V1, LOG = {V1!r}, {str(self.folder / "crash.log")!r}
            json.load(sys.stdin)
            log = Path(LOG)
            calls = log.read_text().count("\\n") if log.exists() else 0
            with log.open("a") as f:
                f.write("x\\n")
            if calls:
                sys.exit(4)
            print(json.dumps({{"candidates": [{{"changes": {{"src/client.py": V1}}}}], "usage": {{
                "input_tokens": 100, "output_tokens": 20, "model": "stand-in", "simulated": True}}}}))
            '''), encoding="utf-8")
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        thread.propose(router.RouterProvider([PY, str(script)], router=router.RouterConfig(githits=self.route())),
                       seed=1)
        rows = router.attribution(thread.history())
        self.assertEqual([(r["action"], r["after"]["outcome"]) for r in rows],
                         [("propose", "failed"), ("lookup", "provider_error")])


class Stops(RouterCase):
    def test_the_same_failure_three_times_stops_the_search(self):
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed(mode="positional")
        found = thread.propose(provider, seed=1)
        self.assertEqual(len(found), 3, "three calls, not the candidate budget's twelve")
        rows = router.attribution(thread.history())
        self.assertEqual([(r["action"], r["rule"]) for r in rows],
                         [("propose", "propose.first"), ("repair", "repair.default"), ("repair", "repair.default"),
                          ("stop", "stop.no_progress")])
        self.assertEqual([r["after"]["same_failure"] for r in rows], [None, True, True, None])
        last = [b["value"] for b in self.facts(thread, router.ROUTE)][-1]
        self.assertEqual((last["repeats"], last["diagnosis"]["error"], last["eligible"]), (3, "TypeError",
                                                                                            ["repair", "stop"]))
        self.assertEqual(provider.trace[-1]["stopped"], "stop.no_progress")
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual((search["stopped"], search["error"]), ("completed", None))
        self.assertEqual(self.fake.requests, [], "no lookup: the host cannot resolve the library here")

    def test_the_budget_and_the_call_limit_are_the_routers_own_stops(self):
        kit = self.kit(self.contract(max_candidates=2, max_evaluations=6))
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = router.RouterProvider(self.adapter("budget"))  # no GitHits route: repair only
        found = thread.propose(provider)
        self.assertEqual([(c.id, c.evaluation) for c in found], [("c1", "failed"), ("c2", "none")])
        values = [b["value"] for b in self.facts(thread, router.ROUTE)]
        self.assertEqual([(v["action"], v["rule"]) for v in values],
                         [("propose", "propose.first"), ("repair", "repair.default"), ("stop", "stop.budget")])
        self.assertEqual(values[1]["routes"], {"githits": "none", "web": "none"})
        self.assertEqual(values[2]["budget"]["candidates"], 0)
        self.assertEqual(router.attribution(thread.history())[1]["after"]["outcome"], "unevaluated")
        other = kit.start("Fetch a URL with fakelib, twice", paths=["src/", "tests/"])
        limited = router.RouterProvider(self.adapter("limited"), max_calls=1)
        other.propose(limited)
        self.assertEqual([v["value"]["rule"] for v in self.facts(other, router.ROUTE)], ["propose.first", "stop.calls"])

    def test_a_failing_lookup_never_ends_the_search(self):
        state = {"searches": 0}

        def flaky(request):
            state["searches"] += 1
            if state["searches"] == 1:
                return 503, {"Content-Type": "application/problem+json", "X-Request-ID": "r1"}, b'{"code":"BUSY"}'
            return 200, {"Content-Type": "application/json", "X-Request-ID": "r2"}, json.dumps(
                search_response([fakelib_hit()])).encode()

        self.fake.script["/v1/searches"] = flaky
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        with self.host_sees_fakelib():
            found = thread.propose(self.routed(), seed=1)
        self.assertEqual([c.evaluation for c in found], ["failed", "failed", "passed"])
        calls = [b["value"] for b in self.facts(thread, router.LOOKUP)]
        self.assertEqual([(v["tool"], v["status"], v["ok"]) for v in calls],
                         [("githits.search", "error", False), ("githits.search", "ok", True),
                          ("githits.read", "ok", True)])
        second = self.requests("model")[1]
        self.assertNotIn("evidence", second, "a failed lookup attaches nothing")
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual((search["stopped"], search["error"]), ("completed", None))

    def test_excluded_results_are_dropped_by_the_client_and_leave_no_evidence(self):
        self.fake.serve("/v1/searches", search_response([fakelib_hit("https://github.com/SyberLabs/fakelib")]))
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed()
        provider.max_calls = 2
        thread.propose(provider, seed=1)
        [call] = [b["value"] for b in self.facts(thread, router.LOOKUP)]
        self.assertEqual((call["results_returned"], call["results_dropped"]), (0, 1))
        self.assertEqual([r["path"] for r in self.fake.requests], ["/v1/searches"], "nothing to read")
        self.assertNotIn("evidence", self.requests("model")[1])

    def test_after_a_slow_lookup_the_budget_is_read_again_before_the_model_call(self):
        class Slow:
            name = "web"

            def search(self, query, *, timeout):
                time.sleep(4.2)  # past the search's whole budget, whatever timeout it was given
                return router.WebAnswer("ok", f"{SIGNATURE}: a keyword-only timeout", 1, "w-slow")

        kit = self.kit(self.contract(max_seconds=4))
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = router.RouterProvider(self.adapter("model"), router=router.RouterConfig(web=Slow()))
        thread.propose(provider, seed=1)
        values = [b["value"] for b in self.facts(thread, router.ROUTE)]
        self.assertEqual([(v["action"], v["rule"], v["last_outcome"]) for v in values],
                         [("propose", "propose.first", None), ("web", "web.third_party", "failed"),
                          ("stop", "stop.budget", "none")])
        self.assertEqual(len(self.requests("model")), 1, "no model call after the lookup spent the budget")
        self.assertEqual(router.attribution(thread.history())[1]["after"]["outcome"], "none")

    def test_a_malformed_answer_is_still_journaled_and_never_ends_the_search(self):
        hit = fakelib_hit()
        hit["locator"] = "fakelib/__init__.py"  # a string where the contract has an object
        self.fake.serve("/v1/searches", search_response([hit]))
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed()
        provider.max_calls = 2
        thread.propose(provider, seed=1)
        self.assertEqual([r["path"] for r in self.fake.requests], ["/v1/searches"])
        calls = [b["value"] for b in self.facts(thread, router.LOOKUP)]
        self.assertEqual([(v["tool"], v["status"]) for v in calls], [("githits.search", "ok")],
                         "the call reached GitHits, so it is journaled even though its answer could not be used")
        [item] = metrics.work_items(thread.history())
        self.assertEqual(item.githits_lookups.value, 1)
        self.assertEqual(len(self.requests("model")), 2, "the search went on")

    def test_a_query_the_guard_blocks_is_not_a_lookup(self):
        (self.root / "src" / "notes.txt").write_text(f"{QUERY}\n", encoding="utf-8")  # a line of the repository
        self.commit("notes")
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed()
        provider.max_calls = 2
        thread.propose(provider, seed=1)
        second = [b["value"] for b in self.facts(thread, router.ROUTE)][1]
        self.assertEqual((second["action"], second["eligible"], second["blocked"]),
                         ("repair", ["repair", "stop"], {"lookup": "tree_line"}))
        self.assertEqual(self.fake.requests, [])


# ----------------------------------------------------------------------------------------- holdout, decider, web

class Holdout(RouterCase):
    def test_a_held_out_lookup_is_a_recorded_repair(self):
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed(holdout=1.0)
        provider.max_calls = 2
        thread.propose(provider, seed=1)
        second = [b["value"] for b in self.facts(thread, router.ROUTE)][1]
        self.assertEqual((second["action"], second["rule"], second["lookup"]), ("repair", "holdout", None))
        expected = random.Random(f"{router.HOLDOUT_SALT}:{thread.id}:s1:1").random()
        self.assertEqual(second["holdout"], {"p": 1.0, "u": round(expected, 6), "chosen": "lookup",
                                             "rule": "lookup.third_party", "held_out": True})
        self.assertEqual(self.fake.requests, [], "a held-out lookup reaches nobody")
        found = router.summary(router.attribution(thread.history()))["holdout"]["all"]
        self.assertEqual((found["held_out"]["decisions"], found["held_out"]["evaluated"],
                          found["looked_up"]["decisions"], found["difference"]["pass_rate"]), (1, 1, 0, None))

    def test_a_held_out_question_stays_unasked_on_every_route(self):
        queries = []

        class Search:
            name = "web"

            def search(self, query, *, timeout):
                queries.append(query)
                return router.WebAnswer("ok", SIGNATURE, 1, "w")

        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        thread.propose(self.routed(mode="stubborn", holdout=1.0, web=Search()), seed=1)
        values = [b["value"] for b in self.facts(thread, router.ROUTE)]
        self.assertEqual([(v["action"], v["rule"], bool(v["holdout"]), v["blocked"]) for v in values],
                         [("propose", "propose.first", False, None), ("repair", "holdout", True, None),
                          ("repair", "repair.default", False, {"lookup": "holdout", "web": "holdout"}),
                          ("stop", "stop.no_progress", False, {"lookup": "holdout", "web": "holdout"})],
                         "one draw for the question; after it, neither GitHits nor the web is asked it")
        self.assertEqual((self.fake.requests, queries), ([], []))

    def test_each_work_item_draws_its_own_and_a_search_can_be_repeated(self):
        """``--seed`` is 0 for every item by default: the draw still differs from item to item, because the stream
        is seeded by the thread and the search too, and the same thread, search and seed draw the same again."""
        kit = self.kit()
        drawn = []
        for item in range(4):
            thread = kit.start(f"Fetch a URL with fakelib, item {item}", paths=["src/", "tests/"])
            provider = self.routed(name=f"model{item}", holdout=0.5)
            provider.max_calls = 2
            thread.propose(provider, seed=0)
            [value] = [b["value"] for b in self.facts(thread, router.ROUTE) if b["value"]["holdout"]]
            expected = random.Random(f"{router.HOLDOUT_SALT}:{thread.id}:s1:0").random()
            self.assertEqual((value["holdout"]["u"], value["holdout"]["held_out"]),
                             (round(expected, 6), expected < 0.5))
            self.assertEqual(value["action"], "repair" if expected < 0.5 else "lookup")
            drawn.append(value["holdout"]["u"])
        self.assertEqual(len(set(drawn)), len(drawn), "every item drew its own number")
        rows = router.attribution(e for t in kit.journal.thread_ids() for e in kit.session.history(t))
        found = router.summary(rows)["holdout"]
        self.assertEqual(found["all"]["looked_up"]["decisions"] + found["all"]["held_out"]["decisions"], 4)
        self.assertEqual(found["first_per_thread"], found["all"], "one draw per item here")
        self.assertEqual(found["all"]["looked_up"]["passed"], found["all"]["looked_up"]["decisions"],
                         "every lookup here is followed by a pass; no held-out repair is")
        self.assertEqual(found["all"]["held_out"]["passed"], 0)

    def test_the_comparison_counts_every_drawn_decision(self):
        """Intention to treat: a refused or unevaluated attempt after a draw is a decision that did not pass, never a
        row dropped from its group (the reviewer's case: four of ten passed against six of ten, not 1.0 against 0.6)."""
        def row(thread, held, outcome, decision=2):
            return {"thread": thread, "decision": decision, "action": "repair" if held else "lookup",
                    "holdout": {"p": 0.5, "u": 0.1, "chosen": "lookup", "rule": "lookup.third_party", "held_out": held},
                    "calls_to_pass": 1 if outcome == "passed" else None, "dollars_to_pass": None,
                    "lookups": {"calls": 0 if held else 2, "results": 0 if held else 1, "latency_ms": 0},
                    "after": {"outcome": outcome, "trigger_fixed": outcome == "passed", "same_failure": None,
                              "booked_dollars": None, "evidence_passed": None}}
        rows = ([row(f"a{i}", False, "passed") for i in range(4)] + [row(f"b{i}", False, "refused:no_change")
                                                                    for i in range(6)]
                + [row(f"c{i}", True, "passed") for i in range(6)] + [row(f"d{i}", True, "failed") for i in range(4)]
                + [row("a0", False, "failed", decision=4)])  # a second draw in one item: not independent
        found = router.summary(rows)["holdout"]
        self.assertEqual((found["all"]["looked_up"]["pass_rate"], found["all"]["held_out"]["pass_rate"],
                          found["all"]["difference"]["pass_rate"]), (round(4 / 11, 6), 0.6, round(4 / 11 - 0.6, 6)))
        self.assertEqual((found["first_per_thread"]["looked_up"]["pass_rate"],
                          found["first_per_thread"]["difference"]["pass_rate"]), (0.4, -0.2))
        self.assertEqual(found["first_per_thread"]["looked_up"]["threads"], 10)


class Deciders(RouterCase):
    def test_a_decider_is_followed_only_where_its_action_is_open(self):
        seen = []

        def choose(found):
            seen.append(found)
            return router.Choice("web", "x", "try the web")

        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed(decider=router.FunctionDecider(choose, name="fn", revision="7"))
        found = thread.propose(provider, seed=1)
        values = [b["value"] for b in self.facts(thread, router.ROUTE)]
        self.assertEqual(values[1]["decider"], {"name": "fn@7", "proposed": "web", "refused": "not_eligible"})
        self.assertEqual((values[1]["action"], values[1]["rule"]), ("lookup", "lookup.third_party"),
                         "the web is not open here (no route), so the baseline's choice stands")
        self.assertEqual(seen[0].baseline, router.Choice("lookup", "lookup.third_party", seen[0].baseline.reason))
        self.assertEqual([c.evaluation for c in found], ["failed", "passed"], "no second decision was needed")
        for number, error in enumerate((ValueError("model offline"), RuntimeError("anything at all"))):
            broken = router.FunctionDecider(lambda _found, error=error: (_ for _ in ()).throw(error), name="b")
            other = kit.start(f"Fetch a URL with fakelib (broken decider {number})", paths=["src/", "tests/"])
            other.propose(self.routed(name=f"broken{number}", decider=broken), seed=1)
            note = [b["value"] for b in self.facts(other, router.ROUTE)][1]
            self.assertEqual((note["decider"], note["rule"]), ({"name": "b@1", "error": "decider_error"},
                                                               "lookup.third_party"),
                             "a decider that fails, however, never ends the search: the baseline decides")
        failing_policy = type("Failing", (), {"name": "failing", "revision": "1",
                                              "decide": lambda self, situation: 1 / 0})()
        third = kit.start("Fetch a URL with fakelib (failing policy)", paths=["src/", "tests/"])
        third.propose(self.routed(name="policy", policy=failing_policy), seed=1)
        self.assertEqual([b["value"]["rule"] for b in self.facts(third, router.ROUTE)][1], "lookup.third_party")

    def test_a_command_decider_reads_the_situation_and_its_usage_is_journaled(self):
        log = self.folder / "decider.log"
        script = self.folder / "decider.py"
        script.write_text(textwrap.dedent(f'''\
            import json, os, sys
            request = json.load(sys.stdin)
            request["environment"] = sorted(name for name in os.environ
                                            if name.startswith(("GITHITS_", "SYBERLABS_GITHITS_", "SYBERLABS_CERT")))
            open({str(log)!r}, "a", encoding="utf-8").write(json.dumps(request) + "\\n")
            print(json.dumps({{"action": "stop", "reason": "one lookup is not worth it here",
                              "usage": {{"input_tokens": 40, "output_tokens": 5, "model": "small-decider",
                                        "simulated": True}}}}))
            '''), encoding="utf-8")
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed(decider=router.CommandDecider([PY, str(script)], name="small", revision="0"))
        operator = {githits.TOKEN_ENV: self.token, githits.LICENSES_ENV: "MIT",
                    "SYBERLABS_CERTIFICATE_SEED": "00" * 32}
        with mock.patch.dict(os.environ, operator):
            found = thread.propose(provider, seed=1)
        self.assertEqual(len(found), 1)
        second = [b["value"] for b in self.facts(thread, router.ROUTE)][1]
        self.assertEqual((second["action"], second["rule"], second["reason"]),
                         ("stop", "decider", "one lookup is not worth it here"))
        self.assertEqual(second["decider"], {"name": "small@0", "proposed": "stop", "usage": {
            "input_tokens": 40, "output_tokens": 5, "model": "small-decider", "simulated": True}})
        [request] = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(request["environment"], [], "no GitHits variable and no signing seed reach the decider")
        self.assertEqual(request["protocol"], router.PROTOCOL)
        situation_json = request["situation"]
        self.assertEqual((situation_json["eligible"], situation_json["baseline"], situation_json["calls"]),
                         (["repair", "lookup", "stop"], {"action": "lookup", "rule": "lookup.third_party"}, 1))
        self.assertTrue(situation_json["diagnosis"]["error_line"].startswith("ImportError: cannot import name"),
                        "a decider may read the exception line")
        self.assertEqual(situation_json["lookups"]["lookup"]["query_digest"], router._digest(QUERY))
        routes = json.dumps([b["value"] for b in self.facts(thread, router.ROUTE)])
        self.assertNotIn("cannot import name", routes, "the router's facts never hold the exception line; the "
                                                       "host's own evaluation record keeps the check's output tail")
        self.assertNotIn("error_line", routes)
        self.assertEqual(provider.trace[-1]["stopped"], "decider")


class Web(RouterCase):
    def test_without_githits_the_web_is_asked_the_same_question(self):
        queries = []

        class Search:
            name = "web"

            def search(self, query, *, timeout):
                queries.append((query, timeout))
                return router.WebAnswer("ok", f"1. fakelib 2.0 changelog: fetch_all became {SIGNATURE}", 1, "w-1")

        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = router.RouterProvider(self.adapter("model"), router=router.RouterConfig(web=Search()))
        found = thread.propose(provider, seed=1)
        self.assertEqual([c.evaluation for c in found], ["failed", "passed"])
        self.assertEqual([q for q, _t in queries], [f"python {QUERY}"])
        second = [b["value"] for b in self.facts(thread, router.ROUTE)][1]
        self.assertEqual((second["action"], second["rule"], second["diagnosis"]["kind"], second["eligible"],
                          second["routes"]), ("web", "web.third_party", "third_party", ["repair", "web", "stop"],
                                              {"githits": "none", "web": "ready"}))
        [call] = self.facts(thread, router.LOOKUP)
        self.assertEqual((call["version"], call["value"]["tool"], call["value"]["ok"],
                          call["value"]["results_returned"], call["value"]["request_id"], call["value"]["guard"]),
                         ("s1.r2.1", router.WEB_TOOL, True, 1, "w-1", "pass"))
        [evidence] = self.requests("model")[1]["evidence"]
        self.assertEqual((evidence["route"], evidence["target"]), ("web", None))
        [item] = metrics.work_items(thread.history())
        self.assertEqual(item.githits_lookups.value, None, "a web search is not a GitHits lookup")
        self.assertEqual(router.summary(router.attribution(thread.history()))["web"]["evidence_passed"], 1)

    def test_a_module_installed_nowhere_is_repaired_and_its_name_never_leaves(self):
        queries = []

        class Search:
            name = "web"

            def search(self, query, *, timeout):
                queries.append(query)
                return router.WebAnswer("ok", "anything", 1, "w")

        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = self.routed(mode="missing", web=Search())
        provider.max_calls = 2
        thread.propose(provider, seed=1)
        second = [b["value"] for b in self.facts(thread, router.ROUTE)][1]
        self.assertEqual((second["action"], second["rule"], second["diagnosis"]["kind"], second["eligible"]),
                         ("repair", "repair.default", "missing_dependency", ["repair", "stop"]))
        self.assertEqual((queries, self.fake.requests), ([], []))

    def test_the_web_command_seam(self):
        log = self.folder / "web.log"
        script = self.folder / "web.py"
        script.write_text(textwrap.dedent(f'''\
            import json, os, sys
            request = json.load(sys.stdin)
            request["token"] = "GITHITS_API_TOKEN" in os.environ
            open({str(log)!r}, "w", encoding="utf-8").write(json.dumps(request))
            print(json.dumps({{"request_id": "w-9", "results": [
                {{"title": "fakelib", "url": "https://example.test/fakelib", "snippet": "fakelib provides fetch"}},
                {{"title": "other", "url": "https://example.test/other"}}, "not a result"]}}))
            '''), encoding="utf-8")
        with mock.patch.dict(os.environ, {githits.TOKEN_ENV: self.token}):
            answer = router.CommandWeb([PY, str(script)], limit=5).search("python fakelib", timeout=30)
        self.assertEqual((answer.status, answer.results, answer.request_id), ("ok", 2, "w-9"))
        self.assertEqual(answer.text, "1. fakelib https://example.test/fakelib\n   fakelib provides fetch\n"
                                      "2. other https://example.test/other")
        self.assertEqual(json.loads(log.read_text(encoding="utf-8")),
                         {"protocol": router.WEB_PROTOCOL, "query": "python fakelib", "limit": 5, "token": False})
        slow = router.CommandWeb([PY, "-c", "import time; time.sleep(30)"]).search("q", timeout=0.5)
        self.assertEqual(slow.status, "error", "a command over its time limit is stopped and answers nothing")
        failing = router.CommandWeb([PY, "-c", "import sys; sys.exit(3)"]).search("q", timeout=30)
        self.assertEqual((failing.status, failing.text), ("error", ""))


# ------------------------------------------------------------------------------------- the default is unchanged

class DefaultUnchanged(RouterCase):
    def test_the_repair_loop_writes_no_route_fact_and_sends_no_evidence(self):
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        thread.propose(RepairProvider(self.adapter("plain"), max_calls=2), seed=1)
        self.assertEqual(self.facts(thread, router.ROUTE), [])
        self.assertEqual(self.facts(thread, router.LOOKUP), [])
        first, second = self.requests("plain")
        self.assertEqual(set(first), {"protocol", "objective", "base", "scope", "budget", "context", "files", "seed"})
        self.assertEqual(set(second), set(first) | {"feedback"})
        self.assertEqual(self.fake.requests, [])

    def test_a_request_without_evidence_gets_the_prompt_it_always_got(self):
        request = {"protocol": "syberlabs.search/v0alpha1", "objective": "Fix it", "scope": ["src"],
                   "feedback": {"candidate": "c1", "files": {"src/a.py": "x = 1\n"},
                                "checks": [{"name": "tests", "state": "failed", "output_tail": "boom"}]}}
        expected = ("Objective: Fix it\n\nMutable scope: src\n\nThe previous attempt below did not pass the host's "
                    "checks. Fix it. Failing checks:\n--- tests (failed)\nboom\n\n<file path=\"src/a.py\">\nx = 1\n\n"
                    "</file>")
        self.assertEqual(anthropic_adapter.build_prompt(request, self.root), expected)
        for evidence in ([], [{"text": "  "}], "not a list", [{"route": "githits"}]):
            self.assertEqual(anthropic_adapter.build_prompt({**request, "evidence": evidence}, self.root), expected)
        mutate = {"protocol": "syberlabs.mutate/v0alpha1", "objective": "o", "parent": "c1", "seed": 1,
                  "files": {"src/a.py": "x"}}
        self.assertEqual(anthropic_adapter.build_prompt({**mutate, "evidence": [{"text": "t"}]}, self.root),
                         anthropic_adapter.build_prompt(mutate, self.root), "only the search protocol carries it")
        shown = anthropic_adapter.build_prompt({**request, "evidence": [
            {"route": "githits", "target": "pypi:fakelib@2.0.0", "pin": "2.0.0 (traceback_site)", "text": SIGNATURE}]},
            self.root)
        self.assertIn("untrusted data", shown)
        self.assertIn(f'<reference route="githits" target="pypi:fakelib@2.0.0" pin="2.0.0 (traceback_site)">\n'
                      f"{SIGNATURE}\n</reference>", shown)
        self.assertLess(shown.index("Failing checks"), shown.index("<reference"))
        self.assertLess(shown.index("</reference>"), shown.index('<file path="src/a.py">'))

    def test_a_provider_still_records_only_the_closed_set_of_keys(self):
        self.assertEqual(PROVIDER_FACTS, ("lookup", "route"))
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        refused = []

        def probe(space):
            for key in ("experiment", "review", "proposal_refused"):
                try:
                    space.observe(key, {"x": 1}, "v1")
                except Rejected as exc:
                    refused.append((key, exc.code))
            space.observe("route", {"x": 1}, "s1.r1")
            return []

        thread.propose(FunctionProvider(probe, name="probe", revision="1"))
        self.assertEqual(refused, [("experiment", "invalid_fact"), ("review", "invalid_fact"),
                                   ("proposal_refused", "invalid_fact")])
        self.assertEqual(len(self.facts(thread, router.ROUTE)), 1)


class Construction(RouterCase):
    def test_settings_are_checked_and_the_githits_route_keeps_the_exclusion(self):
        for bad in ({"holdout": 1.5}, {"holdout": -0.1}, {"holdout": True}, {"max_lookups": -1},
                    {"max_lookups": 1.0}, {"policy": object()}, {"decider": object()}):
            with self.subTest(bad=bad), self.assertRaises(Rejected) as caught:
                router.RouterProvider([PY, "-c", "pass"], router=router.RouterConfig(**bad))
            self.assertEqual(caught.exception.code, "invalid_provider")
        with self.assertRaises(Rejected):
            router.RouterProvider([PY, "-c", "pass"], router={"holdout": 0.5})
        self.assertIs(router.routed(RepairProvider), router.RouterProvider)
        self.assertIs(router.routed(router.RouterProvider), router.RouterProvider)
        self.assertIs(router.routed(ClaudeCodeProvider), router.routed(ClaudeCodeProvider))
        with self.assertRaises(Rejected):
            router.routed(FunctionProvider)
        route = router.GitHitsRoute({**self.environ(), githits.EXCLUDE_ENV: "Other",
                                     githits.EXCLUDE_REPLACE_ENV: "1", githits.RECORD_DIR_ENV: "/nowhere"})
        self.assertIsNone(route.ready(), "a half-set record setting is the router's to replace, not an error")
        self.assertEqual(set(route.client().exclude.entries), {("syberlabs",), ("other",)},
                         "the replace switch is never honoured: SyberLabs stays excluded")
        self.assertEqual(router.GitHitsRoute({}).ready(), "no_token")
        self.assertEqual(router.GitHitsRoute({githits.TOKEN_ENV: "t", githits.URL_ENV: "http://example.test/v1"})
                         .ready(), "bad_base_url")
        self.assertEqual(router.session_id("8b1c-uuid/x"), "8b1c-uuid-x-router")

    def test_a_routed_claude_code_provider_has_no_githits_route_of_its_own(self):
        claude = [PY, "-c", "print('2.1.309 (Claude Code)')"]
        with mock.patch.dict(os.environ, {githits.TOKEN_ENV: self.token}):
            with self.assertRaises(Rejected) as caught:
                router.routed(ClaudeCodeProvider)(self.root, bare=True, executable=claude, githits=True,
                                                  lane="provider_3p")
        self.assertEqual(caught.exception.code, "invalid_provider")
        self.assertIn("--router or --githits", caught.exception.detail)
        config = self.folder / "mcp.json"
        config.write_text('{"mcpServers": {}}', encoding="utf-8")
        with self.assertRaises(Rejected) as caught:  # an MCP configuration keeps the GitHits variables for Claude Code
            router.routed(ClaudeCodeProvider)(self.root, bare=True, executable=claude, mcp_config=config,
                                              lane="provider_3p")
        self.assertEqual(caught.exception.code, "invalid_provider")
        self.assertIn("--mcp-config", caught.exception.detail)
        self.assertIsNotNone(ClaudeCodeProvider(self.root, bare=True, executable=claude, mcp_config=config,
                                                lane="provider_3p"), "the unrouted provider takes it, as before")

    def test_under_the_router_the_models_command_holds_no_githits_variable(self):
        operator = {githits.TOKEN_ENV: self.token, "SYBERLABS_GITHITS_LICENSES": "MIT",
                    "SYBERLABS_CERTIFICATE_SEED": "00" * 32}
        kit = self.kit()
        with mock.patch.dict(os.environ, operator):
            routed = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
            self.assertEqual([c.evaluation for c in routed.propose(self.routed("routed"), seed=1)],
                             ["failed", "passed"])
            plain = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
            plain.propose(RepairProvider(self.adapter("plain"), max_calls=1), seed=1)
        self.assertEqual(self.environments("routed"), [[], []], "the factory holds the GitHits route, the model not")
        [inherited] = self.environments("plain")
        self.assertLessEqual(set(operator), set(inherited), "the plain repair loop's command inherits all, as before")


class ClaudeCodeRouted(RouterCase):
    def test_the_routed_claude_code_call_reads_the_lookup_in_its_prompt(self):
        script = self.folder / "claude.py"
        script.write_text("\n".join(f"{key} = {value!r}" for key, value in (
            ("LOG", str(self.folder / "claude.log")), ("SIGNATURE", SIGNATURE), ("V1", V1),
            ("POSITIONAL", POSITIONAL), ("RIGHT", RIGHT))) + "\n" + CLAUDE, encoding="utf-8")
        kit = self.kit()
        thread = kit.start("Fetch a URL with fakelib", paths=["src/", "tests/"])
        provider = router.routed(ClaudeCodeProvider)(kit.repo, trusted_tree=True, executable=[PY, str(script)],
                                                     max_calls=3, name="claude-code-router",
                                                     router=router.RouterConfig(githits=self.route()))
        found = thread.propose(provider, seed=1)
        self.assertEqual([c.evaluation for c in found], ["failed", "passed"])
        prompts = [json.loads(line)["prompt"] for line in (self.folder / "claude.log").read_text().splitlines()]
        self.assertNotIn("<reference", prompts[0])
        self.assertIn('<reference route="githits" target="pypi:fakelib@2.0.0"', prompts[1])
        self.assertIn(SIGNATURE, prompts[1])
        self.assertEqual([b["version"] for b in self.facts(thread, router.LOOKUP)], ["s1.r2.1", "s1.r2.2"])
        self.assertEqual([c.signal["harness"]["name"] for c in found], ["claude-code", "claude-code"])
        self.assertEqual(found[0].provider, "claude-code-router")


class CommandLine(RouterCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"), "--actor",
                        "dev@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def test_propose_repair_with_the_router_from_the_terminal(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("start", "Fetch a URL with fakelib", "--paths", "src", "tests")[0], 0)
        command = " ".join(self.adapter("cli"))
        clean = {name: "" for name in GITHITS_SETTINGS}
        with mock.patch.dict(os.environ, {**self.environ(), **clean}):
            for name in GITHITS_SETTINGS:
                os.environ.pop(name, None)
            code, out, err = self.run_cli("propose", "--repair", command, "--router", "--seed", "4")
        self.assertEqual(code, 0, out + err)
        self.assertIn("c2: 1 files", out)
        self.assertIn("search s1 completed; provider recommends c2", out)
        self.assertNotIn("router:", err)
        status = self.run_cli("status")[1]
        self.assertIn("[repair-router@1]", status)
        kit = self.kit()
        thread = kit.open(kit.threads()[0]["id"])
        values = [b["value"] for b in self.facts(thread, router.ROUTE)]
        self.assertEqual([v["action"] for v in values], ["propose", "lookup"])
        self.assertEqual(self.fake.requests[0]["headers"]["x-githits-session-id"], router.session_id(thread.id))

    def test_router_options_are_refused_where_they_would_do_nothing(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("start", "Fetch a URL with fakelib", "--paths", "src", "tests")[0], 0)
        command = " ".join(self.adapter("cli"))
        cases = [
            (("--command", command, "--router"), "--router routes the repair loop"),
            (("--repair", command, "--router-holdout", "0.2"), "--router-holdout needs --router"),
            (("--repair", command, "--router-web", "x", "--router-decider", "y"),
             "--router-web, --router-decider needs --router"),
            (("--repair", command, "--router", "--router-holdout", "1.5"), "a probability from 0 to 1"),
            (("--repair", command, "--router", "--router-max-lookups", "-1"), "zero or more"),
            (("--claude-code", "--bare", "--githits", "--router"), "pick one"),
            (("--claude-code", "--bare", "--mcp-config", "mcp.json", "--router"), "takes no --mcp-config"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                code, _out, err = self.run_cli("propose", *args)
                self.assertEqual(code, 2)
                self.assertIn("invalid_provider", err)
                self.assertIn(message, err)
        with mock.patch.dict(os.environ, {githits.TOKEN_ENV: ""}):
            code, out, err = self.run_cli("propose", "--repair", command, "--router", "--max-calls", "1")
        self.assertEqual(code, 0, out + err)
        self.assertIn("router: no GitHits lookups in this search (no_token); it repairs, or stops", err)
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
