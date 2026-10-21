"""The check runner on a repository that is not Python: setup, caps, consoles, caches, process trees.

These are the Flyspace findings (a Node consumer on Windows, 2026-10-18): a check named by a
bare ``npm`` cannot start, a fresh worktree has no ``node_modules``, a 64 KB cap turns a
passing vitest run into ``error``, a cp1252 console crashes on a tick after the verdict is
recorded, and a timeout leaves npm's children alive. ``docs/CONSUMERS.md`` is the user's side.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from syberlabs import checks as runner
from syberlabs.build import Kit, default_contract, detect_checks
from syberlabs.cli import main as cli
from syberlabs.evolution import DEFAULTS

PY = sys.executable
GIT_ENV = {"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}
# A test script that writes more than the old 65,536-byte default and succeeds, like vitest does.
LOUD_PACKAGE = {"name": "loud", "version": "1.0.0", "private": True,
                "scripts": {"test": "node -e \"process.stdout.write('x'.repeat(120000))\""}}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def alive(pid: int) -> bool:
    if runner.WINDOWS:
        # ``os.kill(pid, 0)`` terminates on Windows; ask the task list instead.
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Fixture(unittest.TestCase):
    """A one-file repository with no ``tests/`` directory, so detection is not Python's."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "project"
        self.root.mkdir()
        (self.root / "README.md").write_text("# fixture\n")

    def commit(self):
        git(self.root.parent, "init", "-q", "-b", "main", str(self.root))
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "initial")

    def kit(self, contract, actor="dev@example.test"):
        home = self.root / ".syberlabs"
        (home / "contracts").mkdir(parents=True, exist_ok=True)
        (home / "contracts" / f"{contract['id']}.v{contract['version']}.json").write_text(json.dumps(contract))
        kit = Kit.local(home, self.root, actor=actor)
        self.addCleanup(kit.close)
        return kit

    def contract(self, checks, **evaluation):
        doc = default_contract(self.root)
        doc["evolution"]["evaluation"] = {"checks": checks, "required": sorted(checks), **evaluation}
        return doc

    def verdict(self, contract, version=1):
        contract["version"] = version
        thread = self.kit(contract).start("Touch the README", contract=f"{contract['id']}.v{version}")
        thread.propose(changes={"README.md": f"# fixture\n\nedit {version}\n"})
        return thread.check("c1")


class NodeRepository(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "package.json").write_text(json.dumps(LOUD_PACKAGE))
        self.commit()
        self.found = detect_checks(self.root)
        if "install" not in self.found:
            self.skipTest("no npm on PATH; detect_checks wrote no Node check")

    def test_the_default_cap_fits_a_real_test_runner_and_a_declared_cap_still_binds(self):
        """120 KB of passing output is ``passed`` by default and ``error`` under an explicit 65,536."""
        self.assertEqual(DEFAULTS["max_output_bytes"], 1_048_576)
        self.assertTrue(Path(self.found["tests"]["argv"][0]).is_absolute())
        self.assertEqual(self.found["install"]["argv"][1], "install")  # no lockfile, so not ``npm ci``
        verdict = self.verdict(self.contract(self.found))
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("install", "passed"), ("tests", "passed")],
                         [c.output_tail for c in verdict.checks])
        self.assertTrue(verdict.acceptable)
        strict = self.verdict(self.contract(self.found, max_output_bytes=65_536), version=2)
        states = {c.name: c.state for c in strict.checks}
        self.assertEqual(states["tests"], "error")
        self.assertIn("output exceeded 65536 bytes", [c for c in strict.checks if c.name == "tests"][0].output_tail)


class SetupConvention(Fixture):
    GENERATE = [PY, "-c", "open('generated.txt', 'w').write('made by setup')"]
    NEEDS = [PY, "-c", "import sys; sys.exit(0 if open('generated.txt').read() == 'made by setup' else 3)"]

    def setUp(self):
        super().setUp()
        self.commit()

    def test_a_check_named_setup_runs_first_whatever_the_declaration_order(self):
        """Contracts are stored canonical, and the runner sorts: ``setup`` precedes ``tests``."""
        verdict = self.verdict(self.contract({"tests": {"argv": self.NEEDS}, "setup": {"argv": self.GENERATE}}))
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("setup", "passed"), ("tests", "passed")])
        self.assertTrue(verdict.acceptable)

    def test_without_setup_the_dependent_check_fails_on_its_own(self):
        verdict = self.verdict(self.contract({"tests": {"argv": self.NEEDS}}))
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("tests", "failed")])

    def test_a_failing_setup_marks_every_later_check_setup_failed_without_running_it(self):
        ran = self.root.parent / "ran.txt"
        checks = {"install": {"argv": [PY, "-c", "import sys; print('no lockfile'); sys.exit(1)"]},
                  "tests": {"argv": [PY, "-c", f"open({str(ran)!r}, 'w').write('ran')"]},
                  "typecheck": {"argv": [PY, "-c", "pass"]}}
        verdict = self.verdict(self.contract(checks))
        self.assertEqual([(c.name, c.state) for c in verdict.checks],
                         [("install", "failed"), ("tests", "error"), ("typecheck", "error")])
        self.assertEqual([c.output_tail for c in verdict.checks[1:]], ["setup_failed", "setup_failed"])
        self.assertEqual([c.duration_ms for c in verdict.checks[1:]], [0, 0])
        self.assertFalse(ran.exists(), "a check after a failed setup must not start")
        self.assertFalse(verdict.acceptable)
        self.assertEqual(verdict.reason, "candidate_check_failed:install")


class Runner(unittest.TestCase):
    def test_timeout_ends_the_whole_process_tree(self):
        """A check that spawned a grandchild leaves nothing behind after ``timed_out``."""
        script = textwrap.dedent("""\
            import subprocess, sys
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print(child.pid, flush=True)
            child.wait()
            """)
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "check.log"
            result = runner.run_check([PY, "-c", script], Path(folder), dict(os.environ), 1, 1_000_000, log)
            self.assertEqual(result["state"], "timed_out")
            grandchild = int(log.read_text().split()[0])
        deadline = time.monotonic() + 10
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(alive(grandchild), f"grandchild {grandchild} survived the timeout")

    def test_the_scrubbed_environment_keeps_the_package_caches_and_nothing_else(self):
        fake = {"PATH": "p", "APPDATA": "a", "LOCALAPPDATA": "l", "USERPROFILE": "u", "XDG_CACHE_HOME": "x",
                "GOPATH": "g", "CARGO_HOME": "c", "npm_config_cache": "n", "HOME": "/real/home",
                "AWS_SECRET_ACCESS_KEY": "no", "NODE_AUTH_TOKEN": "no", "SSH_AUTH_SOCK": "no"}
        with patch.dict(os.environ, fake, clear=True):
            env = runner._environment(Path("/tmp/home"), "c1")
        self.assertEqual(env["HOME"], str(Path("/tmp/home")))
        self.assertEqual((env["GOPATH"], env["CARGO_HOME"], env["npm_config_cache"]), ("g", "c", "n"))
        if runner.WINDOWS:
            self.assertEqual((env["APPDATA"], env["LOCALAPPDATA"], env["USERPROFILE"]), ("a", "l", "u"))
            self.assertNotIn("XDG_CACHE_HOME", env)
        else:
            self.assertEqual(env["XDG_CACHE_HOME"], "x")
            self.assertTrue({"APPDATA", "LOCALAPPDATA", "USERPROFILE"}.isdisjoint(env))
        self.assertTrue({"AWS_SECRET_ACCESS_KEY", "NODE_AUTH_TOKEN", "SSH_AUTH_SOCK"}.isdisjoint(env))


class Console(Fixture):
    def test_a_check_tail_with_a_tick_prints_on_a_cp1252_console(self):
        """The verdict is recorded before the tail is printed; printing must not raise."""
        self.commit()
        tick = [PY, "-c", "import sys; sys.stdout.buffer.write('\\u2713 2 passed, 1 failed\\n'.encode()); sys.exit(1)"]
        contract = self.contract({"tests": {"argv": tick}})
        kit = self.kit(contract)
        thread = kit.start("Touch the README", contract="repo-change.v1")
        thread.propose(changes={"README.md": "# fixture\n\nedit\n"})
        kit.close()
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(console):
            code = cli(["--home", str(self.root / ".syberlabs"), "--repo", str(self.root), "--actor", "dev@example.test",
                        "--thread", thread.id, "check", "c1"])
        console.flush()
        shown = console.buffer.getvalue().decode("cp1252")
        self.assertEqual(code, 1)
        self.assertIn("tests output (tail)", shown)
        self.assertIn("? 2 passed, 1 failed", shown)


if __name__ == "__main__":
    unittest.main()
