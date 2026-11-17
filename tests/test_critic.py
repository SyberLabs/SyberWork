"""The critic station: an independent reviewer as a host check, its findings in the chain, and the rule that reads them."""

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from examples import critic_rules
from spec.validate import check as check_schema
from spec.validate import validate_events
from syberlabs import build, critic
from syberlabs import checks as runner
from syberlabs.canonical import canonical
from syberlabs.cli import main as cli
from syberlabs.providers import PatchProvider
from tests.test_build_thread import EXPORT, GOOD, REPORT, TEST_REPORT, RepoCase, default_contract

PY = sys.executable
RULES = Path(__file__).resolve().parents[1] / "examples" / "critic_rules.py"
MARKED = {**GOOD, "src/export.py": EXPORT + "# FIXME-HIGH: unchecked input\n"}

# Fixture critics. Each is deterministic, reads the host's request on stdin, and prints a document.
FLAG = '''\
    import json, sys
    name, revision = sys.argv[1], sys.argv[2]
    request = json.load(sys.stdin)
    findings = [{"severity": "high", "file": path, "line": 1, "claim": "FIXME-HIGH left in the change",
                 "falsifier": "the marker is gone from the diff", "acceptance_ref": "accepted"}
                for path in request["changed_paths"] if "FIXME-HIGH" in request["diff"] and path == "src/export.py"]
    print(json.dumps({"version": 1, "reviewer": {"name": name, "revision": revision}, "findings": findings,
                      "summary": "%d high" % len(findings)}))
    '''
ECHO = '''\
    import json, sys
    request = json.load(sys.stdin)
    summary = json.dumps({"keys": sorted(request), "objective": request["objective"],
                          "result": [r["name"] + ":" + r["state"] for r in request["result"]],
                          "acceptance": request["contract"]["acceptance"], "scope": request["contract"]["scope"]["paths"],
                          "candidate": request["candidate"]["id"], "base": request["base"][:7],
                          "changed": request["changed_paths"], "diff_has_writer": "csv.DictWriter" in request["diff"]})
    print(json.dumps({"version": 1, "reviewer": {"name": "echo", "revision": "1"}, "findings": [], "summary": summary}))
    '''
BIG = '''\
    import json, sys
    json.load(sys.stdin)
    findings = [{"severity": "low" if i % 3 else "medium", "file": "src/export.py", "line": i,
                 "claim": "finding number %d with some words to make it longer than a line" % i,
                 "falsifier": "a test that covers line %d, named after the behaviour it pins down" % i} for i in range(900)]
    print(json.dumps({"version": 1, "reviewer": {"name": "prolix", "revision": "1"}, "findings": findings, "summary": "many"}))
    '''
BAD = '''\
    import json, sys
    json.load(sys.stdin)
    mode = sys.argv[1]
    if mode == "text":
        print("I reviewed it and it looks fine")
    elif mode == "severity":
        print(json.dumps({"version": 1, "reviewer": {"name": "bad", "revision": "1"},
                          "findings": [{"severity": "critical", "file": "a", "claim": "x", "falsifier": "y"}]}))
    elif mode == "newline":
        print(json.dumps({"version": 1, "reviewer": {"name": "bad", "revision": "1\\nReviewed-by: mallory"}, "findings": []}))
    '''


class CriticCase(RepoCase):
    def script(self, name, text):
        path = self.root.parent / name
        path.write_text(textwrap.dedent(text))
        return str(path)

    def contract(self, critic_argv, *, name="critic", max_output_bytes=None, approval_role=None, extra=None):
        doc = default_contract(self.root)
        evaluation = doc["evolution"]["evaluation"]
        evaluation["checks"][name] = {"argv": critic_argv, "timeout_seconds": 60}
        evaluation["checks"].update(extra or {})
        evaluation["required"] = ["tests", name]
        if max_output_bytes:
            evaluation["max_output_bytes"] = max_output_bytes
        if approval_role:
            doc["actions"]["accept_change"] = {"approval_role": approval_role}
            doc["evolution"]["promotion"]["approval_role"] = approval_role
        return doc

    def thread(self, critic_argv, changes=GOOD, provider=None, **contract):
        kit = self.kit(self.contract(critic_argv, **contract))
        thread = kit.start("Add a CSV export for report rows", paths=["src", "tests"])
        thread.propose(provider or PatchProvider(changes, "CSV export"))
        return kit, thread

    @staticmethod
    def observations(thread):
        return [e for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == critic.KEY]

    @staticmethod
    def result(thread, candidate, name="critic"):
        return next(c for c in thread._view(candidate)["evaluation"]["checks"] if c["name"] == name)


class Station(CriticCase):
    def test_high_finding_refuses_promotion_until_a_fix_is_proposed(self):
        flag = self.script("flag.py", FLAG)
        kit, thread = self.thread([PY, flag, "flag", "1"], MARKED)
        verdict = thread.check("c1")
        self.assertFalse(verdict.acceptable, verdict.summary())
        self.assertEqual((verdict.status, verdict.reason, verdict.rule),
                         ("denied", "high_finding:critic:1", "critic.no_high_finding"))
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("critic", "passed"), ("tests", "passed")])
        receipt = thread.accept("c1")
        self.assertEqual((receipt.status, receipt.reason), ("denied", "high_finding:critic:1"))
        # The observation is the host's, verified, and names the same bytes the evaluation recorded.
        [event] = self.observations(thread)
        body = event["body"]
        self.assertEqual((body["source"], body["verified"], body["actor"]), ("check:critic", True, build.HOST))
        value = body["value"]
        self.assertEqual((value["candidate"], value["check"], value["counts"]), ("c1", "critic", {"high": 1, "medium": 0, "low": 0}))
        self.assertEqual(value["reviewer"], {"name": "flag", "revision": "1"})
        recorded = self.result(thread, "c1")
        self.assertEqual((body["version"], value["findings_digest"]), (recorded["output_digest"], recorded["output_digest"]))
        self.assertEqual(value["findings"][0]["file"], "src/export.py")
        self.assertEqual(value["findings"][0]["acceptance_ref"], "accepted")
        self.assertFalse(value["truncated"])
        # The same change without the marker is promotable; the critic ran again on the new tree.
        thread.propose(changes=GOOD, message="drop the marker")
        fixed = thread.check("c2")
        self.assertTrue(fixed.acceptable, fixed.summary())
        self.assertEqual(fixed.rule, "admission.passed")
        self.assertEqual(thread.accept("c2").status, "succeeded")
        self.assertEqual([e["body"]["value"]["candidate"] for e in self.observations(thread)], ["c1", "c2"])
        self.assertEqual(self.observations(thread)[1]["body"]["value"]["counts"]["high"], 0)
        self.assertEqual(validate_events(thread.history(), "critic"), [])
        self.assertTrue(kit.session.inspect(thread.id)["chain_valid"])

    def test_medium_findings_from_the_reference_critic_never_block(self):
        kit, thread = self.thread([PY, str(RULES)], {"src/export.py": EXPORT})
        verdict = thread.check("c1")
        self.assertTrue(verdict.acceptable, verdict.summary())
        [event] = self.observations(thread)
        value = event["body"]["value"]
        self.assertEqual(value["reviewer"], critic_rules.REVIEWER)
        self.assertEqual(value["counts"], {"high": 0, "medium": 1, "low": 0})
        self.assertEqual(value["findings"][0]["file"], "src/export.py")
        self.assertEqual(thread.accept("c1").status, "succeeded")

    def test_reviewer_equal_to_the_provider_is_not_independent(self):
        flag = self.script("flag.py", FLAG)
        provider = PatchProvider(GOOD, "CSV export", name="claude", revision="opus-5")
        kit, thread = self.thread([PY, flag, "claude", "opus-5"], provider=provider)
        verdict = thread.check("c1")
        self.assertFalse(verdict.acceptable)
        self.assertEqual((verdict.reason, verdict.rule), ("candidate_check_failed:critic", "candidate.promotable"))
        recorded = self.result(thread, "c1")
        self.assertEqual((recorded["state"], recorded["exit_code"], recorded["output_tail"]), ("error", 0, "reviewer_not_independent"))
        self.assertEqual(self.observations(thread), [])
        # A different revision of the same platform is independent by the recorded identity.
        other = PatchProvider({**GOOD, "src/export.py": EXPORT + "# v2\n"}, "again", name="claude", revision="opus-6")
        thread.propose(other)
        self.assertTrue(thread.check("c2").acceptable)

    def test_malformed_documents_are_check_errors_with_the_path(self):
        bad = self.script("bad.py", BAD)
        for mode, path in (("text", "json"), ("severity", "findings/0/severity"), ("newline", "reviewer/revision")):
            with self.subTest(mode):
                kit, thread = self.thread([PY, bad, mode])
                verdict = thread.check("c1")
                self.assertEqual(verdict.reason, "candidate_check_failed:critic")
                recorded = self.result(thread, "c1")
                self.assertEqual((recorded["state"], recorded["output_tail"]), ("error", "findings_invalid:" + path))
                self.assertEqual(self.observations(thread), [])
                kit.close()
                import shutil
                shutil.rmtree(self.root / ".syberlabs")

    def test_a_large_document_is_truncated_in_the_record_with_the_digest_intact(self):
        big = self.script("big.py", BIG)
        kit, thread = self.thread([PY, big], max_output_bytes=1_048_576)
        self.assertTrue(thread.check("c1").acceptable)
        [event] = self.observations(thread)
        value = event["body"]["value"]
        self.assertTrue(value["truncated"])
        self.assertEqual(value["counts"], {"high": 0, "medium": 300, "low": 600})
        self.assertLess(len(value["findings"]), 900)
        self.assertGreater(len(value["findings"]), 0)
        self.assertLessEqual(len(canonical(value).encode()), critic.MAX_VALUE_BYTES)
        recorded = self.result(thread, "c1")
        self.assertEqual(value["findings_digest"], recorded["output_digest"])
        self.assertEqual(validate_events(thread.history(), "big"), [])

    def test_the_request_carries_what_the_spec_names(self):
        echo = self.script("echo.py", ECHO)
        # Checks run in the stored contract's order, which is by name: a_lint, critic, tests.
        kit, thread = self.thread([PY, echo], extra={"a_lint": {"argv": [PY, "-c", "pass"]}})
        thread.check("c1")
        [event] = self.observations(thread)
        seen = json.loads(event["body"]["value"]["summary"])
        self.assertEqual(seen["keys"], ["base", "candidate", "changed_paths", "contract", "diff", "objective", "protocol", "result"])
        self.assertEqual(seen["objective"], "Add a CSV export for report rows")
        self.assertEqual(seen["result"], ["a_lint:passed"])
        self.assertEqual(seen["acceptance"], [{"id": "accepted", "kind": "effect", "action": "accept_change"}])
        self.assertEqual(seen["scope"], ["."])
        self.assertEqual((seen["candidate"], seen["base"]), ("c1", thread.base[:7]))
        self.assertEqual(seen["changed"], ["src/export.py", "tests/test_export.py"])
        self.assertTrue(seen["diff_has_writer"])

    def test_evaluate_without_the_new_arguments_still_records_the_critic_as_a_check(self):
        flag = self.script("flag.py", FLAG)
        kit, thread = self.thread([PY, flag, "flag", "1"], MARKED)
        view = thread._view("c1")
        body = runner.evaluate(kit.repo, view, thread.config["evaluation"])
        self.assertEqual([(c["name"], c["state"]) for c in body["checks"]], [("critic", "passed"), ("tests", "passed")])
        self.assertEqual(self.observations(thread), [])

    def test_a_contract_without_a_critic_is_unchanged(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        verdict = thread.check("c1")
        self.assertEqual((verdict.acceptable, verdict.rule), (True, "admission.passed"))
        self.assertEqual(self.observations(thread), [])
        self.assertEqual(critic.critic_checks(thread.config["evaluation"]["required"]), [])

    def test_findings_from_the_terminal(self):
        flag = self.script("flag.py", FLAG)
        self.kit(self.contract([PY, flag, "flag", "1"])).close()

        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"), "--actor", "dev@example.test", *args])
            return code, out.getvalue()

        self.assertEqual(run("start", "Add a CSV export", "--paths", "src", "tests")[0], 0)
        for path, text in MARKED.items():
            (self.root / path).write_text(text)
        self.assertEqual(run("propose", "--from-worktree")[0], 0)
        code, out = run("findings", "c1")
        self.assertEqual(code, 1)
        self.assertIn("no critic observation for c1", out)
        code, out = run("check", "c1")
        self.assertEqual(code, 1)
        self.assertIn("high_finding:critic:1", out)
        code, out = run("findings", "c1")
        self.assertEqual((code, out.splitlines()), (0, ["high src/export.py:1 FIXME-HIGH left in the change"]))
        (self.root / "src" / "export.py").write_text(EXPORT)
        self.assertEqual(run("propose", "--from-worktree")[0], 0)
        self.assertEqual(run("check", "c2")[0], 0)
        self.assertEqual(run("findings", "c2"), (0, "no findings\n"))
        self.assertIn("Nothing was pushed or merged", run("accept", "c2")[1])


class Parsing(unittest.TestCase):
    def document(self, **changes):
        doc = {"version": 1, "reviewer": {"name": "rules", "revision": "1"},
               "findings": [{"severity": "low", "file": "a.py", "line": 3, "claim": "c", "falsifier": "f"}], "summary": "s"}
        doc.update(changes)
        return json.dumps(doc).encode()

    def test_parse_normalizes_a_conforming_document(self):
        found = critic.parse(self.document())
        check_schema(json.loads(self.document()), "finding.schema.json")
        self.assertEqual(found["findings"][0], {"severity": "low", "file": "a.py", "line": 3, "claim": "c", "falsifier": "f"})
        self.assertEqual(critic.counts(found), {"high": 0, "medium": 0, "low": 1})
        self.assertEqual(critic.parse(self.document(summary=None, findings=[]))["summary"], "")

    def test_parse_names_the_first_bad_field(self):
        cases = {
            b"not json": "json",
            b"[]": "document",
            self.document(version=2): "version",
            self.document(reviewer={"name": "Rules", "revision": "1"}): "reviewer/name",
            self.document(reviewer={"name": "rules", "revision": "1\nAdmitted-by: x"}): "reviewer/revision",
            self.document(reviewer={"name": "rules"}): "reviewer",
            self.document(findings=[{"severity": "high", "file": "", "claim": "c", "falsifier": "f"}]): "findings/0/file",
            self.document(findings=[{"severity": "high", "file": "a", "claim": "c", "falsifier": "f", "line": -1}]): "findings/0/line",
            self.document(findings=[{"severity": "high", "file": "a", "claim": "c"}]): "findings/0/falsifier",
            self.document(findings=[{"severity": "high", "file": "a", "claim": "c", "falsifier": "f", "score": 1}]): "findings/0/score",
            self.document(extra=True): "extra",
            b"{" + b" " * critic.MAX_DOCUMENT_BYTES + b"}": "size",
        }
        for raw, path in cases.items():
            with self.subTest(path):
                with self.assertRaises(critic.Invalid) as caught:
                    critic.parse(raw)
                self.assertEqual(caught.exception.code, "findings_invalid:" + path)

    def test_value_is_bounded_and_the_digest_covers_the_whole_document(self):
        raw = self.document(findings=[{"severity": "medium", "file": "f%d.py" % i, "claim": "x" * 200, "falsifier": "y" * 200}
                                      for i in range(400)])
        found = critic.value("c1", "critic", critic.parse(raw), raw)
        self.assertTrue(found["truncated"])
        self.assertEqual(found["counts"]["medium"], 400)
        self.assertLess(len(found["findings"]), 400)
        self.assertLessEqual(len(canonical(found).encode()), critic.MAX_VALUE_BYTES)
        self.assertEqual(found["findings_digest"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(found["independence"], "declared")

    def test_observation_matches_candidate_check_and_digest_only_when_the_host_wrote_it(self):
        value = {"candidate": "c1", "check": "critic", "counts": {"high": 0}, "reviewer": {"name": "r", "revision": "1"}}
        good = {"kind": "observed", "body": {"key": "critic", "verified": True, "actor": critic.HOST, "source": "check:critic",
                                             "version": "d" * 64, "value": value}}
        self.assertEqual(critic.observation([good], "c1", "critic", "d" * 64), value)
        for label, bad in (("unverified", {**good["body"], "verified": False}),
                           ("another actor", {**good["body"], "actor": "dev@example.test"}),
                           ("other digest", {**good["body"], "version": "e" * 64}),
                           ("other check", {**good["body"], "source": "check:critic.two"}),
                           ("other candidate", {**good["body"], "value": {**value, "candidate": "c2"}})):
            with self.subTest(label):
                self.assertIsNone(critic.observation([{"kind": "observed", "body": bad}], "c1", "critic", "d" * 64))

    def test_host_actor_name_is_the_build_threads(self):
        self.assertEqual(critic.HOST, build.HOST)
        self.assertTrue(critic.is_critic("critic") and critic.is_critic("critic.rules"))
        self.assertFalse(critic.is_critic("critical") or critic.is_critic("tests") or critic.is_critic("my.critic"))


class ReferenceCritic(unittest.TestCase):
    def review(self, changed, scope=None):
        request = critic.request({"id": "c1", "changed_paths": changed, "base": "b" * 40, "provider": {"name": "p", "revision": "1"}},
                                 [], "", {"objective": "o", "acceptance": [], "scope": scope or {"paths": ["src/", "tests/"], "exclude": []}})
        document = critic_rules.review(request)
        check_schema(document, "finding.schema.json")
        return critic.parse(json.dumps(document).encode())

    def test_out_of_scope_path_is_high(self):
        found = self.review(["src/a.py", "setup.py", "tests/test_a.py"])
        self.assertEqual(critic.counts(found), {"high": 1, "medium": 0, "low": 0})
        self.assertEqual(found["findings"][0]["file"], "setup.py")

    def test_source_without_a_test_is_medium_and_with_one_is_clean(self):
        self.assertEqual(critic.counts(self.review(["src/a.py", "src/b.py"])), {"high": 0, "medium": 2, "low": 0})
        self.assertEqual(critic.counts(self.review(["src/a.py", "tests/test_a.py"])), {"high": 0, "medium": 0, "low": 0})
        self.assertEqual(critic.counts(self.review(["docs/README.md"], {"paths": ["."], "exclude": []})), {"high": 0, "medium": 0, "low": 0})

    def test_the_same_request_yields_the_same_document(self):
        request = critic.request({"id": "c1", "changed_paths": ["src/a.py"], "base": "b" * 40, "provider": {}}, [], "", None)
        self.assertEqual(critic_rules.review(request), critic_rules.review(json.loads(json.dumps(request))))



# --- the test-integrity rule ---------------------------------------------------------------------

def _new_file_diff(path, source):
    lines = source.splitlines()
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))


def _added(path, source, scope=None):
    """A request for a candidate that adds one file, with no repository behind it (the rule rebuilds the file)."""
    return critic.request({"id": "c1", "changed_paths": [path], "base": "b" * 40,
                           "provider": {"name": "p", "revision": "1"}}, [], _new_file_diff(path, source),
                          {"objective": "o", "acceptance": [], "scope": scope or {"paths": ["."], "exclude": []}})


HARNESS_CLASS = "import unittest\n\nclass T(unittest.TestCase):\n    def {name}(self, *args):\n        return None\n"
# rule -> added test code that trips it (each snippet is a whole new test module)
TRIPS = {
    "process_exit": [
        "import os\nos._exit(0)\n",
        "import os, signal\n\ndef helper():\n    os.kill(os.getppid(), signal.SIGTERM)\n",
        "import signal\n\ndef helper():\n    signal.raise_signal(signal.SIGINT)\n",
        "end = __import__('os')._exit\n",
        "from os import _exit\n\ndef helper():\n    _exit(0)\n",
        "import os\n\ndef helper():\n    getattr(os, '_exit')(0)\n",
        "import os\n\ndef helper():\n    os.abort()\n",
    ],
    "signal_handler": [
        "import signal\n\ndef helper():\n    signal.signal(signal.SIGTERM, lambda *a: None)\n",
        "import signal\n\ndef helper():\n    signal.alarm(5)\n",
    ],
    "exit_at_import": [
        "import sys\nsys.exit(0)\n",
        "raise SystemExit(0)\n",
        "import unittest\nunittest.main()\n",
        "import sys, unittest\n\nclass T(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        sys.exit(0)\n",
        "def setUpModule():\n    raise KeyboardInterrupt\n",
        "def tearDownModule():\n    quit()\n",
    ],
    "harness_override": [HARNESS_CLASS.format(name=name) for name in ("run", "__call__", "debug", "id", "_callTestMethod",
                                                                        "_addExpectedFailure", "_addUnexpectedSuccess",
                                                                        "addCleanup", "enterContext", "doCleanups",
                                                                        "addClassCleanup", "enterClassContext",
                                                                        "doClassCleanups", "subTest")]
    + ["from unittest import TestCase as Base\n\nclass T(Base):\n    def _callSetUp(self):\n        pass\n",
       "class T:\n    def test_a(self):\n        pass\n    run = None\n",
       "import contextlib, unittest\n\nclass T(unittest.TestCase):\n    subTest = contextlib.nullcontext\n"],
    "load_tests": ["def load_tests(loader, tests, pattern):\n    return tests\n", "load_tests = None\n"],
    "unittest_patch": [
        "import unittest\nunittest.TestCase.run = lambda self, result=None: result\n",
        "from unittest import TestResult\nTestResult.addSuccess = print\n",
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self._outcome.result.stop()\n",
        "from unittest import mock\nmock.patch('unittest.TestCase.run').start()\n",
        "import unittest\nsetattr(unittest.TestCase, 'run', None)\n",
        "import unittest.case\nunittest.case._addError = lambda result, test, exc_info: None\n",
        "def helper(test):\n    setattr(type(test), '_addUnexpectedSuccess', lambda self, result: result.addSuccess(self))\n",
        "def helper(result):\n    result.startTest = print\n",
        "def helper(result):\n    return result.shouldStop\n",
        "def helper(test):\n    test._testMethodName = 'test_other'\n",
        "import contextlib\n\ndef helper(test):\n    test.subTest = lambda **kw: contextlib.suppress(Exception)\n",
        "def helper(test):\n    test.doCleanups = lambda: True\n",
        "def helper(test):\n    setattr(test, 'addCleanup', print)\n",
    ],
    "sys_modules": [
        "import sys\nsys.modules['unittest'] = None\n",
        "import sys\nsys.modules.pop('unittest.case')\n",
        "import sys\ndel sys.modules['unittest']\n",
    ],
    "runner_reach": [
        "import __main__\n",
        "import gc\n",
        "import sys\nmain = sys.modules['__main__']\n",
        "import sys\n\ndef helper():\n    return sys._getframe(1)\n",
        "import inspect\n\ndef helper():\n    return inspect.stack()\n",
        "def helper(tb):\n    return tb.tb_frame.f_back.f_locals\n",
        "import os\n\ndef helper():\n    os.write(3, b'x')\n",
        "import ctypes\n",
        "LOGS = 'syberlabs-' 'shards-'\n",
    ],
    "test_replaced": [
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_0(self):\n        T.test_1 = lambda self: None\n"
        "    def test_1(self):\n        self.fail('x')\n",
        "from tests.test_other import Victim\nVictim.test_fails = lambda self: None\n",
        "def helper(victim):\n    setattr(victim, 'test_fails', None)\n",
        "def helper(test):\n    del type(test).test_fails\n",
        "def helper(test):\n    test.__class__.test_fails, x = None, 1\n",
        "import unittest\n\nclass T(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
        "        cls.test_a = lambda self: None\n",
        "from unittest import mock\n\ndef helper(Victim):\n    return mock.patch.object(Victim, 'test_fails')\n",
        "from unittest import mock\n\ndef helper(Victim):\n    return mock.patch.multiple(Victim, test_fails=None)\n",
        "from unittest.mock import patch\nSTOP = patch('tests.test_other.Victim.test_fails').start()\n",
    ],
    "dynamic_code": [
        "exec('import os')\n",
        "def helper(text):\n    return eval(text)\n",
        "import importlib\n\ndef helper(name):\n    return importlib.import_module(name)\n",
        "import os\n\ndef helper():\n    return getattr(os, '_' + 'exit')\n",
    ],
}
# Added test code that only looks like a pattern, or does it where the runner already copes.
CLEAN = [
    "TEXT = 'os._exit(0) and signal.signal and load_tests'  # os._exit(0) in a comment\n",
    "import unittest\n\nif __name__ == '__main__':\n    unittest.main()\n",
    "import os\n\ndef alive(pid):\n    os.kill(pid, 0)\n",
    "import sys, unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        sys.exit(1)\n",
    "import sys\nsys.modules['mymodule'] = object()\nsys.modules.pop('syberlabs', None)\n",
    "class Clock:\n    def __call__(self):\n        return 0\n\n    def run(self):\n        return 1\n",
    "from unittest import mock\nimport os\n\ndef helper():\n    return mock.patch.object(os, 'getcwd')\n",
    "import unittest\nraise unittest.SkipTest('needs a database')\n",
    "import re\nPATTERN = re.compile('x')\n\ndef helper():\n    return __import__('csv')\n",
    "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.addCleanup(print)\n"
    "        with self.subTest(i=0):\n            self.doCleanups()\n",
    "class Steps:\n    def subTest(self):\n        return None\n",
    # an instance's own attributes named test... (a test fetches its own method before its code runs)
    "import unittest\n\nclass T(unittest.TestCase):\n    def setUp(self):\n        self.test_dir = 'x'\n"
    "        setattr(self, 'testing', True)\n    def test_a(self):\n        self.test_dir = None\n",
    "from unittest import mock\n\ndef helper(obj):\n    obj.tests_dir.path = 'x'\n"
    "    return mock.patch.dict('os.environ', {'test_mode': '1'}), mock.patch.object(obj, 'path')\n",
    "import unittest\n\ndef make(n):\n    return lambda self: None\n\nclass T(unittest.TestCase):\n    pass\n\n"
    "for n in range(3):\n    setattr(T, f'test_{n}', make(n))\n",
]


class TestIntegrityRule(unittest.TestCase):
    """The reference critic's high finding for test code that can end, signal or subvert the process it runs in."""

    def rules(self, document):
        return {f["claim"].split("(")[1].split(")")[0] for f in document["findings"] if f["severity"] == "high"}

    def test_each_pattern_is_a_high_finding_in_a_test_file(self):
        for rule, snippets in TRIPS.items():
            for snippet in snippets:
                with self.subTest(rule=rule, snippet=snippet):
                    document = critic_rules.review(_added("tests/test_new.py", snippet))
                    check_schema(document, "finding.schema.json")
                    self.assertIn(rule, self.rules(document), document["findings"])

    def test_the_same_code_outside_test_files_and_code_that_only_mentions_a_pattern_are_not_flagged(self):
        for snippet in CLEAN:
            with self.subTest(snippet=snippet):
                self.assertEqual(critic_rules.review(_added("tests/test_new.py", snippet))["findings"], [])
        document = critic_rules.review(_added("src/app.py", "import os\nos._exit(0)\n"))
        self.assertEqual(critic.counts(critic.parse(json.dumps(document).encode()))["high"], 0)
        for path in ("tests/helpers.py", "tests/sub/conftest.py", "benchmarks/test_speed.py"):
            self.assertTrue(critic_rules.is_test_code(path), path)
        for path in ("src/attest.py", "tests/data.json", "docs/TESTING.md"):
            self.assertFalse(critic_rules.is_test_code(path), path)

    def test_the_current_suite_has_no_finding(self):
        """Every Python file in this checkout, test code or not, read as if it were added whole, trips nothing:
        zero false positives."""
        root = Path(__file__).resolve().parents[1]
        paths = [p.relative_to(root).as_posix() for p in root.rglob("*.py")
                 if not any(part.startswith(".") for part in p.relative_to(root).parts)]
        tests = [p for p in paths if critic_rules.is_test_code(p)]
        self.assertGreater(len(tests), 50)
        self.assertGreater(len(paths) - len(tests), 80)
        found = {p: critic_rules.integrity_findings(p, (root / p).read_text(encoding="utf-8"), None) for p in paths}
        self.assertEqual({p: f for p, f in found.items() if f}, {})

    def test_a_changed_test_file_the_rule_cannot_read_is_high_not_passed(self):
        modified = ("diff --git a/tests/test_a.py b/tests/test_a.py\nindex 1..2 100644\n--- a/tests/test_a.py\n"
                    "+++ b/tests/test_a.py\n@@ -3,0 +4 @@\n+    x = 1\n")
        truncated = "diff --git a/src/a.py b/src/a.py\n[diff truncated: 2000000 bytes total]\n"
        not_python = _new_file_diff("tests/test_a.py", "def broken(:\n")
        for diff, says in ((modified, "could not be read"), (truncated, "could not be read"), (not_python, "does not parse")):
            with self.subTest(says=says):
                request = critic.request({"id": "c1", "changed_paths": ["tests/test_a.py"], "base": "b" * 40,
                                          "provider": {}}, [], diff, None)
                [finding] = critic_rules.review(request)["findings"]
                self.assertEqual((finding["severity"], finding["file"]), ("high", "tests/test_a.py"))
                self.assertIn(says, finding["claim"])

    def test_in_a_repository_only_the_lines_the_candidate_adds_count(self):
        """Run as the host runs it: in the candidate's worktree, reading the commit and the diff from Git."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]
            body = "import os\nimport unittest\n\n\nclass T(unittest.TestCase):\n    def test_old(self):\n        os._exit(3)\n"
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text(body, encoding="utf-8")
            (root / "tests" / "test_gone.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
            subprocess.run([*git, "init", "-q"], cwd=root, check=True)
            subprocess.run([*git, "add", "-A"], cwd=root, check=True)
            subprocess.run([*git, "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run([*git, "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            (root / "tests" / "test_a.py").write_text(body + "\n    def test_new(self):\n        os.abort()\n", encoding="utf-8")
            (root / "tests" / "test_gone.py").unlink()
            subprocess.run([*git, "add", "-A"], cwd=root, check=True)
            subprocess.run([*git, "commit", "-qm", "candidate"], cwd=root, check=True)
            head = subprocess.run([*git, "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            request = critic.request({"id": "c1", "commit": head, "base": base, "provider": {},
                                      "changed_paths": ["tests/test_a.py", "tests/test_gone.py"]}, [], "", None)
            out = subprocess.run([PY, str(RULES)], cwd=root, input=json.dumps(request), capture_output=True, text=True,
                                 check=True).stdout
        [finding] = json.loads(out)["findings"]
        self.assertEqual((finding["file"], finding["line"], finding["severity"]), ("tests/test_a.py", 10, "high"))
        self.assertIn("(process_exit): os.abort", finding["claim"])


def _added_files(files: dict):
    """A request for a candidate that adds several files, with no repository behind it."""
    diff = "".join(_new_file_diff(path, source) for path, source in files.items())
    return critic.request({"id": "c1", "changed_paths": list(files), "base": "b" * 40,
                           "provider": {"name": "p", "revision": "1"}}, [], diff,
                          {"objective": "o", "acceptance": [], "scope": {"paths": ["."], "exclude": []}})


# The consistent same-process forgery of tests/test_run_shards.py, moved into a module outside tests/.
RELOCATED_HELPER = """\
import gc, os
import __main__


def forge(test):
    result = next(o for o in gc.get_objects() if isinstance(o, __main__._ShardResult))
    spec = next(o for o in gc.get_objects() if isinstance(o, dict) and "nonce" in o and "ids" in o)
    result.channel.send("outcome", id=test.id(), kind="success")
    os._exit(0)
"""
RELOCATED_CALLER = """\
import unittest
from forge_helper import forge


class T(unittest.TestCase):
    def test_a(self):
        forge(self)
"""


class IntegrityBeyondTests(unittest.TestCase):
    """Any changed Python file, not only test code: a test can import it, and then it runs in the test process."""

    def rules(self, document, path=None):
        return {f["claim"].split("(")[1].split(")")[0] for f in document["findings"]
                if f["severity"] == "high" and (path is None or f["file"] == path)}

    def test_the_relocated_forgery_is_high_on_the_helper(self):
        document = critic_rules.review(_added_files({"forge_helper.py": RELOCATED_HELPER,
                                                     "tests/test_forge.py": RELOCATED_CALLER}))
        check_schema(document, "finding.schema.json")
        self.assertEqual(self.rules(document, "forge_helper.py"), {"runner_reach"})
        self.assertEqual(self.rules(document, "tests/test_forge.py"), set())
        self.assertEqual(sorted(f["line"] for f in document["findings"]), [1, 2, 6, 7])
        self.assertTrue(all("a test can import this file" in f["claim"] for f in document["findings"]))
        self.assertEqual(document["reviewer"], {"name": "rules", "revision": "6"})

    def test_the_harness_families_apply_to_any_python_file(self):
        write_side = ("os.write", "syberlabs-")  # the runner_reach snippets that stay test-scoped (next test)
        for rule in critic_rules.HARNESS_RULES:
            for snippet in TRIPS[rule]:
                if rule == "runner_reach" and any(marker in snippet for marker in write_side):
                    continue
                with self.subTest(rule=rule, snippet=snippet):
                    for path in ("src/helper.py", "syberlabs/util.pyw", "conftest_like.py"):
                        self.assertIn(rule, self.rules(critic_rules.review(_added(path, snippet))), path)

    def test_ending_the_process_and_the_runner_write_side_stay_test_scoped(self):
        for rule in critic_rules.TEST_ONLY_RULES:
            for snippet in TRIPS[rule]:
                with self.subTest(rule=rule, snippet=snippet):
                    self.assertEqual(self.rules(critic_rules.review(_added("src/app.py", snippet))), set())
                    self.assertIn(rule, self.rules(critic_rules.review(_added("tests/test_app.py", snippet))))
        for snippet in ("import os\n\ndef helper():\n    os.write(3, b'x')\n", "import msvcrt\n", "import _winapi\n",
                        "import os\n\ndef reopen(fd):\n    return os.fdopen(os.dup(fd), 'wb')\n",
                        "LOGS = 'syberlabs-' 'shards-'\n"):
            with self.subTest(snippet=snippet):
                self.assertEqual(self.rules(critic_rules.review(_added("src/app.py", snippet))), set())
                self.assertIn("runner_reach", self.rules(critic_rules.review(_added("tests/test_app.py", snippet))))
        for snippet in ("import ctypes\n", "import gc\n", "import sys\n\ndef f():\n    return sys._getframe(1)\n",
                        "import sys\n\ndef f():\n    sys.settrace(None)\n"):
            with self.subTest(snippet=snippet):  # the read side, which finds the nonce, applies everywhere
                self.assertIn("runner_reach", self.rules(critic_rules.review(_added("src/app.py", snippet))))

    def test_a_computed_import_anchored_in_a_named_package_is_not_dynamic_code(self):
        anchored = ("import importlib\n\ndef load(name):\n    return importlib.import_module(f'benchmarks.scenarios.{name}')\n",
                    "from importlib import import_module\n\ndef load(name):\n    return import_module('syberlabs.' + name)\n")
        loose = ("import importlib\n\ndef load(name):\n    return importlib.import_module(name)\n",
                 "import importlib\n\ndef load(name):\n    return importlib.import_module(f'{name}.x')\n",
                 "import importlib\n\ndef load(name):\n    return importlib.import_module('o' + name)\n",
                 "import importlib\n\ndef load(name):\n    return importlib.import_module(f'os.{name}')\n",
                 "import importlib\n\ndef load(name):\n    return importlib.import_module(f'ctypes.{name}')\n")
        for path in ("src/app.py", "tests/test_app.py"):
            for snippet in anchored:
                with self.subTest(path=path, snippet=snippet):
                    self.assertEqual(self.rules(critic_rules.review(_added(path, snippet))), set())
            for snippet in loose:
                with self.subTest(path=path, snippet=snippet):
                    self.assertIn("dynamic_code", self.rules(critic_rules.review(_added(path, snippet))))

    def test_a_compiled_module_or_an_unreadable_python_file_is_high(self):
        request = critic.request({"id": "c1", "changed_paths": ["src/fast.pyd", "src/fast.so", "src/cached.pyc",
                                                                "data/blob.bin"], "base": "b" * 40, "provider": {}},
                                 [], "", None)
        found = critic_rules.review(request)["findings"]
        self.assertEqual(sorted(f["file"] for f in found), ["src/cached.pyc", "src/fast.pyd", "src/fast.so"])
        self.assertTrue(all(f["severity"] == "high" and "compiled module" in f["claim"] for f in found))
        modified = ("diff --git a/src/a.py b/src/a.py\nindex 1..2 100644\n--- a/src/a.py\n"
                    "+++ b/src/a.py\n@@ -3,0 +4 @@\n+    x = 1\n")
        request = critic.request({"id": "c1", "changed_paths": ["src/a.py"], "base": "b" * 40, "provider": {}},
                                 [], modified, None)
        [finding] = [f for f in critic_rules.review(request)["findings"] if f["severity"] == "high"]
        self.assertEqual(finding["file"], "src/a.py")
        self.assertIn("a changed Python file could not be read", finding["claim"])


# --- revision 6: a credential on an added line, and a test made weaker --------------------------------

# Built from parts, so that no line of this file holds the shape the rule looks for.
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0"       # twenty characters in both cases: it looks generated
TYPED = "sk-ant-" + "api03-abcdefgh1234"       # the shape, typed by hand: what a fixture looks like
PEM_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
KEY_LINE = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" + "VJTUt9Us8cKjMzEfYyjiWA4R4"

WEAK_BASE = """\
import unittest


class Orders(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([1, 2]), 3)
        self.assertEqual(total([]), 0)

    def test_empty(self):
        with self.assertRaises(ValueError):
            parse("")

    def test_rounding(self):
        value = total([0.1, 0.2])
        assert round(value, 2) == 0.3

    def test_legacy(self):
        self.assertTrue(legacy())

    @unittest.skipUnless(HAS_DB, "needs the database")
    def test_stored(self):
        self.assertTrue(stored())


def test_module_level():
    assert total([5]) == 5
"""
WEAK_CANDIDATE = """\
import contextlib
import unittest
from unittest import skip as later


class Orders(unittest.TestCase):
    @later("flaky")
    def test_total(self):
        self.assertEqual(total([1, 2]), 3)
        self.assertEqual(total([]), 0)

    def test_empty(self):
        try:
            with self.assertRaises(ValueError):
                parse("")
        except AssertionError:
            pass

    def test_rounding(self):
        value = total([0.1, 0.2])
        return
        assert round(value, 2) == 0.3

    @unittest.skipUnless(HAS_DB, "needs the database")
    def test_stored(self):
        with contextlib.suppress(Exception):
            self.assertTrue(stored())

    @unittest.expectedFailure
    def test_new_and_expected_to_fail(self):
        self.fail("a known defect, written down")

    @unittest.skip("later")
    def test_new_and_never_run(self):
        self.fail("not written yet")

    @unittest.skipIf(WINDOWS, "posix only")
    def test_new_with_a_condition(self):
        self.assertTrue(posix())


def test_module_level():
    if not READY:
        raise unittest.SkipTest("not ready")
    assert total([5]) == 5
"""


class SecretAdded(unittest.TestCase):
    """The reference critic's high finding for a credential shape on a line the candidate adds."""

    def test_the_pattern_is_the_one_the_package_shares(self):
        from syberlabs import _patterns
        self.assertEqual(critic_rules.SECRET.pattern, _patterns.SECRET.pattern)
        samples = (f"token {TOKEN}", f"task-{TOKEN}", "a task-list and a risk-assessment", "key_sk-ant-" + "api03-abcdefgh1234",
                   "xoxb-" + "1234567890-abc", "AKIA" + "ABCDEFGHIJKLMNOP", "sk-short", "", f"{TOKEN} and gho_" + "zyxwvutsrq")
        for text in samples:
            with self.subTest(text=text[:12]):
                self.assertEqual(list(critic_rules.secret_shapes(text)), sorted(set(_patterns.credential_prefixes(text))))
        self.assertEqual(critic_rules.secret_shapes(f"{PEM_HEADER} and {TOKEN}"), {"ghp_": True, "private_key": False})

    def test_a_value_that_looks_generated_is_told_from_one_typed_by_hand(self):
        generated = (TOKEN, "sk-ant-" + "api03-QwErTyUiOpAsDfGhJkLzXc", "xoxb-" + "123456789012-AbCdEfGhIjKl", "gho_" + "abcdefghijKLMNOPQRST",
                     f"{PEM_HEADER}\\n{KEY_LINE}")
        typed = (TYPED, "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789", "sk-" + "test-key-value", "AKIA" + "IOSFODNN7EXAMPLE",
                 "ghp_" + "A1b2C3d4E5", "sk-" + "FAKEFAKEFAKEFAKEFAKE1234", PEM_HEADER, f"{PEM_HEADER} MIIB",
                 "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----", "-----BEGIN OPENSSH " + "PRIVATE KEY-----")
        for text in generated:
            self.assertEqual(set(critic_rules.secret_shapes(text).values()), {True}, text[:8])
        for text in typed:
            self.assertEqual(set(critic_rules.secret_shapes(text).values()), {False}, text[:8])

    def test_a_generated_credential_on_an_added_line_is_high_and_never_quoted(self):
        files = {"docs/setup.md": f"# Setup\n\n    export GH_TOKEN={TOKEN}\n",
                 "deploy/id_rsa": f"{PEM_HEADER}\n{KEY_LINE}\n",
                 "tests/test_masking.py": f"FIXTURE = '{TYPED}'\nHEADER = '{PEM_HEADER}'\n",
                 "src/app.py": "API = 'https://api.example.test'\n"}
        document = critic_rules.review(_added_files(files))
        check_schema(document, "finding.schema.json")
        found = [(f["severity"], f["file"], f["line"], f["claim"].split(":")[0]) for f in document["findings"]]
        self.assertEqual(found, [("high", "deploy/id_rsa", 1, "secret added (private_key)"),
                                 ("high", "docs/setup.md", 3, "secret added (ghp_)"),
                                 ("low", "tests/test_masking.py", 1, "secret added (sk-ant-)"),
                                 ("low", "tests/test_masking.py", 2, "secret added (private_key)")])
        self.assertIn("looks generated", document["findings"][0]["claim"])
        self.assertIn("looks typed by hand, as a fixture does", document["findings"][2]["claim"])
        text = json.dumps(document)
        for value in (TOKEN, TOKEN[4:], TYPED, KEY_LINE):
            self.assertNotIn(value, text, "the document is stored in the chain, so no value is ever in it")

    def test_only_added_lines_of_changed_paths_count(self):
        diff = ("diff --git a/docs/a.md b/docs/a.md\nindex 1..2 100644\n--- a/docs/a.md\n+++ b/docs/a.md\n@@ -1,2 +1,2 @@\n"
                f"-old {TOKEN}\n+new text\n kept {TOKEN}\n"
                "diff --git a/docs/b.md b/docs/b.md\nindex 1..2 100644\n--- a/docs/b.md\n+++ b/docs/b.md\n@@ -1,0 +2 @@\n"
                f"+added {TOKEN}\n")
        request = critic.request({"id": "c1", "changed_paths": ["docs/a.md"], "base": "b" * 40, "provider": {}}, [], diff, None)
        self.assertEqual(critic_rules.review(request)["findings"], [], "a removed line, a kept line, and a path not in the candidate")
        request["changed_paths"].append("docs/b.md")
        [finding] = critic_rules.review(request)["findings"]
        self.assertEqual((finding["file"], finding["line"], finding["severity"]), ("docs/b.md", 2, "high"))

    def test_more_than_the_limit_is_counted_in_the_summary(self):
        lines = "".join(f"K{n} = '{TOKEN}'\n" for n in range(critic_rules.MAX_SECRET_FINDINGS + 3))
        document = critic_rules.review(_added_files({"src/keys.txt": lines}))
        self.assertEqual(len(document["findings"]), critic_rules.MAX_SECRET_FINDINGS)
        self.assertIn("3 more secret-added finding(s) not listed", document["summary"])

    def test_the_reviewer_and_its_tests_hold_no_such_line(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("examples/critic_rules.py", "tests/test_critic.py"):
            lines = (root / name).read_text(encoding="utf-8").splitlines()
            self.assertEqual([n for n, line in enumerate(lines, 1) if critic_rules.secret_shapes(line)], [], name)

    def test_this_checkout_holds_shapes_and_none_that_looks_generated(self):
        """Every tracked text file read as if it were added whole: the fixtures that have the shape of a key are
        low findings, and nothing in the checkout is a high one."""
        root = Path(__file__).resolve().parents[1]
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True).stdout.split("\0")
        added = {}
        for name in filter(None, listed):
            try:
                added[name] = dict(enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1))
            except (OSError, UnicodeDecodeError):
                continue
        found = critic_rules.secret_findings(added)
        self.assertTrue(found, "the checkout has fixtures in the shape of a key")
        self.assertEqual([(f["file"], f["line"]) for f in found if f["severity"] == "high"], [])


class WeakenedTests(unittest.TestCase):
    """The reference critic's finding for a test the candidate removed, newly skipped, or left asserting less."""

    PATH = "tests/test_orders.py"

    def compare(self, base, candidate, **kwargs):
        before = {self.PATH: critic_rules.tests_of(base)} if base is not None else {}
        after = {self.PATH: critic_rules.tests_of(candidate)} if candidate is not None else {}
        return critic_rules.weakened_findings(before, after, **kwargs)

    def rules(self, findings):
        return [(f["claim"].split("(")[1].split(")")[0], f["line"]) for f in findings]

    def test_what_counts_for_each_test(self):
        found = critic_rules.tests_of(WEAK_CANDIDATE)
        self.assertEqual({name: (t["skips"], t["always"], t["assertions"]) for name, t in found.items()}, {
            "Orders.test_total": (1, True, 2), "Orders.test_empty": (0, False, 0), "Orders.test_rounding": (0, False, 0),
            "Orders.test_stored": (1, False, 0), "Orders.test_new_and_expected_to_fail": (1, False, 1),
            "Orders.test_new_and_never_run": (1, True, 1), "Orders.test_new_with_a_condition": (1, False, 1),
            "test_module_level": (1, False, 1)})
        self.assertEqual({name: (t["skips"], t["assertions"]) for name, t in critic_rules.tests_of(WEAK_BASE).items()}, {
            "Orders.test_total": (0, 2), "Orders.test_empty": (0, 1), "Orders.test_rounding": (0, 1), "Orders.test_legacy": (0, 1),
            "Orders.test_stored": (1, 1), "test_module_level": (0, 1)})
        self.assertIsNone(critic_rules.tests_of("def broken(:\n"))
        nested = ("def test_outer():\n    class Inner:\n        def test_inner(self):\n            assert True\n    assert Inner\n\n\n"
                  "def make():\n    class Built:\n        def test_built(self):\n            assert True\n    return Built\n")
        self.assertEqual(list(critic_rules.tests_of(nested)), ["test_outer"], "a test defined inside a function is a fixture")
        named = ("import unittest\nimport team.marks as marks\n\n\nclass T(unittest.TestCase):\n    @marks.skip('later')\n    def test_a(self):\n        pass\n\n"
                 "    @marks.slow\n    @unittest.mock.patch('x.y')\n    def test_b(self, y):\n        pass\n")
        self.assertEqual({name: t["skips"] for name, t in critic_rules.tests_of(named).items()}, {"T.test_a": 1, "T.test_b": 0},
                         "a marker is told by its last name, wherever it is imported from")

    def test_a_removed_test_a_new_skip_and_fewer_assertions_are_each_one_finding(self):
        findings = self.compare(WEAK_BASE, WEAK_CANDIDATE)
        self.assertEqual(self.rules(findings), [("test_removed", None), ("test_skipped", 8), ("assertions_removed", 12)])
        self.assertTrue(all(f["severity"] == "medium" and f["file"] == self.PATH and f["falsifier"] for f in findings))
        removed, skipped, fewer = (f["claim"] for f in findings)
        self.assertIn("removes 1 test(s) that were on the base: Orders.test_legacy", removed)
        self.assertIn("3 test(s) gain a skip or an expected failure in this candidate: Orders.test_total, "
                      "Orders.test_new_and_never_run (new), test_module_level", skipped)
        self.assertNotIn("test_new_with_a_condition", skipped, "a new test that runs under a condition is a test")
        self.assertNotIn("test_new_and_expected_to_fail", skipped, "a new test that must fail runs: it pins a known defect")
        self.assertIn("3 test(s) that were on the base assert less than they did", fewer)
        self.assertIn("Orders.test_empty (1 to 0), Orders.test_rounding (1 to 0), Orders.test_stored (1 to 0)", fewer)
        self.assertEqual([f["severity"] for f in self.compare(WEAK_BASE, WEAK_CANDIDATE, severity="high")], ["high"] * 3)

    def test_what_the_base_already_had_and_what_only_grew_is_not_reported(self):
        self.assertEqual(self.compare(WEAK_BASE, WEAK_BASE), [])
        self.assertEqual(self.compare(WEAK_CANDIDATE, WEAK_CANDIDATE), [], "skips and swallowed assertions the base already had")
        grown = WEAK_BASE.replace("        self.assertTrue(legacy())\n", "        self.assertTrue(legacy())\n        self.assertIsNone(extra())\n")
        self.assertEqual(self.compare(WEAK_BASE, grown), [])
        self.assertEqual(self.rules(self.compare(WEAK_CANDIDATE, WEAK_BASE)), [("test_removed", None)],
                         "taking a skip away or adding an assertion is not a finding; the tests that left are")
        self.assertEqual(self.compare(None, WEAK_BASE), [], "a new file with ordinary tests")

    def test_a_handler_that_fails_the_test_and_a_cleanup_do_not_swallow(self):
        base = "class T:\n    def test_a(self):\n        self.assertTrue(run())\n"
        kept = ("class T:\n    def test_a(self):\n        try:\n            self.assertTrue(run())\n"
                "        except Exception:\n            cleanup()\n            raise\n        finally:\n            close()\n")
        typed = ("class T:\n    def test_a(self):\n        try:\n            self.assertTrue(run())\n"
                 "        except OSError:\n            pass\n")
        retold = ("class T:\n    def test_a(self):\n        try:\n            self.assertTrue(run())\n"
                  "        except AssertionError as exc:\n            self.fail(str(exc))\n")
        for candidate in (kept, typed, retold):
            self.assertEqual(self.compare(base, candidate), [], candidate)
        for candidate in ("class T:\n    def test_a(self):\n        try:\n            self.assertTrue(run())\n        except:\n            pass\n",
                          "class T:\n    def test_a(self):\n        try:\n            self.assertTrue(run())\n"
                          "        except (OSError, Exception):\n            log()\n",
                          "class T:\n    def test_a(self):\n        if False:\n            self.assertTrue(run())\n",
                          "class T:\n    def test_a(self):\n        pass\n"):
            self.assertEqual(self.rules(self.compare(base, candidate)), [("assertions_removed", 2)], candidate)

    def test_a_skip_on_the_class_or_in_a_fixture_reaches_every_test_of_it(self):
        base = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n\n    def test_b(self):\n        pass\n"
        for candidate in (base.replace("class T", "@unittest.skip('later')\nclass T"),
                          base.replace("    def test_a", "    def setUp(self):\n        self.skipTest('later')\n\n    def test_a"),
                          base.replace("    def test_a", "    __unittest_skip__ = True\n\n    def test_a"),
                          base + "\n\ndef setUpModule():\n    raise unittest.SkipTest('later')\n"):
            [finding] = self.compare(base, candidate)
            self.assertIn("2 test(s) gain a skip or an expected failure in this candidate: T.test_a, T.test_b", finding["claim"])
        pytest_style = "import pytest\n\n\n@pytest.mark.xfail\ndef test_a():\n    assert run()\n\n\n@pytest.mark.parametrize('n', [1])\ndef test_b(n):\n    assert n\n"
        self.assertEqual(self.rules(self.compare("def test_a():\n    assert run()\n", pytest_style)), [("test_skipped", 5)],
                         "a test that had to pass and now has to fail")

    def test_a_test_that_moved_is_compared_where_it_went(self):
        old = {"tests/test_old.py": critic_rules.tests_of(WEAK_BASE)}
        renamed_class = {"tests/test_old.py": critic_rules.tests_of(WEAK_BASE.replace("class Orders", "class OrderTotals"))}
        self.assertEqual(critic_rules.weakened_findings(old, renamed_class), [], "the same tests under a renamed class")
        moved = {"tests/test_new.py": critic_rules.tests_of(WEAK_BASE)}
        self.assertEqual(critic_rules.weakened_findings(old, moved), [], "the same tests in another changed file")
        gutted = {"tests/test_new.py": critic_rules.tests_of(WEAK_BASE.replace("        self.assertTrue(legacy())\n", "        legacy()\n"))}
        [finding] = critic_rules.weakened_findings(old, gutted)
        self.assertEqual((finding["file"], finding["claim"].split(":")[0]), ("tests/test_new.py", "test weakened (assertions_removed)"))
        self.assertIn("Orders.test_legacy (1 to 0)", finding["claim"])
        [finding] = critic_rules.weakened_findings(old, {})
        self.assertEqual((finding["file"], finding["line"]), ("tests/test_old.py", None))
        self.assertIn("removes 6 test(s)", finding["claim"])

    def test_a_test_renamed_in_place_is_compared_with_what_it_became(self):
        base = ("class Study:\n    def test_seven_scenarios_keep_the_write_boundary(self):\n        assert run(7)\n        assert boundary()\n\n"
                "    def test_signoff_completes_the_case(self):\n        assert signoff()\n")
        renamed = base.replace("test_seven_scenarios", "test_nine_scenarios").replace("run(7)", "run(9)")
        self.assertEqual(self.compare(base, renamed), [], "the same test under a name this alike")
        gutted = renamed.replace("        assert boundary()\n", "")
        [finding] = self.compare(base, gutted)
        self.assertIn("Study.test_nine_scenarios_keep_the_write_boundary (2 to 1)", finding["claim"])
        replaced = base.replace("    def test_signoff_completes_the_case(self):\n        assert signoff()\n",
                                "    def test_refunds_are_idempotent(self):\n        assert refund()\n")
        [finding] = self.compare(base, replaced)
        self.assertIn("removes 1 test(s) that were on the base: Study.test_signoff_completes_the_case", finding["claim"])

    def test_many_names_are_counted_not_listed(self):
        base = "class T:\n" + "".join(f"    def test_{n:02}(self):\n        assert run({n})\n" for n in range(12))
        [finding] = self.compare(base, "class T:\n    pass\n")
        self.assertIn("removes 12 test(s) that were on the base: T.test_00, ", finding["claim"])
        self.assertTrue(finding["claim"].endswith("T.test_07 and 4 more"), finding["claim"])

    def test_script_skip_markers_are_matched_as_text_on_added_lines(self):
        added = {"packages/agent/test/decision.test.js": {3: "  it.skip('rejects', () => {})", 4: "describe.only ('totals', () => {", 5: "xit('later')",
                                                          6: "it('runs', () => {})", 7: "test.todo('write this')"},
                 "src/skipper.js": {1: "it.skip('not a test file')"},
                 "web/__tests__/page.tsx": {9: "  test.only('one', async () => {"},
                 "tests/test_a.py": {2: "it.skip('python is parsed, not matched')"}}
        found = critic_rules.script_skip_findings(added)
        self.assertEqual([(f["file"], f["line"], f["claim"].rsplit("(", 1)[1].rstrip(")")) for f in found],
                         [("packages/agent/test/decision.test.js", 3, "it.skip"), ("packages/agent/test/decision.test.js", 4, "describe.only"),
                          ("packages/agent/test/decision.test.js", 5, "xit"), ("packages/agent/test/decision.test.js", 7, "test.todo"),
                          ("web/__tests__/page.tsx", 9, "test.only")])
        self.assertTrue(all(f["severity"] == "medium" and "by its text" in f["claim"] for f in found))

    def test_review_reports_them_at_medium_and_the_option_makes_them_high(self):
        files = {"tests/test_new.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    @unittest.skip('later')\n    def test_a(self):\n        self.fail()\n",
                 "web/app.test.ts": "it.skip('x', () => {})\n"}
        document = critic_rules.review(_added_files(files))
        check_schema(document, "finding.schema.json")
        self.assertEqual([(f["severity"], f["file"], f["line"]) for f in document["findings"]],
                         [("medium", "tests/test_new.py", 6), ("medium", "web/app.test.ts", 1)])
        self.assertEqual(document["reviewer"], {"name": "rules", "revision": "6"})
        blocking = critic_rules.review(_added_files(files), block_weakened=True)
        check_schema(blocking, "finding.schema.json")
        self.assertEqual([f["severity"] for f in blocking["findings"]], ["high", "high"])
        self.assertEqual(blocking["reviewer"], {"name": "rules", "revision": "6-block-weakened"})
        self.assertNotEqual(blocking["reviewer"], document["reviewer"], "a Reviewed-by line says which one ran")

    def test_the_command_takes_one_option_and_refuses_any_other(self):
        request = json.dumps(_added_files({"tests/test_new.py": "import unittest\n\n\n@unittest.skip('x')\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"}))
        plain = subprocess.run([PY, str(RULES)], input=request, capture_output=True, text=True)
        strict = subprocess.run([PY, str(RULES), "--block-weakened-tests"], input=request, capture_output=True, text=True)
        unknown = subprocess.run([PY, str(RULES), "--block-everything"], input=request, capture_output=True, text=True)
        self.assertEqual((plain.returncode, strict.returncode, unknown.returncode), (0, 0, 2))
        self.assertEqual([f["severity"] for f in json.loads(plain.stdout)["findings"]], ["medium"])
        self.assertEqual([f["severity"] for f in json.loads(strict.stdout)["findings"]], ["high"])
        self.assertIn("the only option is --block-weakened-tests", unknown.stderr)

    def test_this_checkout_s_tests_parse_and_compare_clean_with_themselves(self):
        root = Path(__file__).resolve().parents[1]
        paths = [p.relative_to(root).as_posix() for p in root.rglob("*.py")
                 if not any(part.startswith(".") for part in p.relative_to(root).parts)]
        sides = {p: critic_rules.tests_of((root / p).read_text(encoding="utf-8")) for p in paths if critic_rules.is_test_code(p)}
        self.assertGreater(sum(len(tests) for tests in sides.values()), 1000)
        self.assertNotIn(None, sides.values())
        self.assertEqual(critic_rules.weakened_findings(sides, sides), [])

    def test_in_a_repository_both_sides_are_read_from_git(self):
        """Run as a check runs it: in the candidate's worktree, reading the base and the candidate from Git."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]

            def commit(message):
                subprocess.run([*git, "add", "-A"], cwd=root, check=True)
                subprocess.run([*git, "commit", "-qm", message], cwd=root, check=True)
                return subprocess.run([*git, "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            (root / "tests").mkdir()
            (root / "docs").mkdir()
            (root / "tests" / "test_orders.py").write_text(WEAK_BASE, encoding="utf-8")
            (root / "tests" / "test_gone.py").write_text("def test_one():\n    assert 1\n\n\ndef test_two():\n    assert 2\n", encoding="utf-8")
            (root / "tests" / "test_kept.py").write_text("def test_kept():\n    assert 1\n", encoding="utf-8")
            (root / "tests" / "test_broken.py").write_text("def test_was_fine():\n    assert 1\n", encoding="utf-8")
            (root / "docs" / "setup.md").write_text(f"old {TOKEN}\n", encoding="utf-8")
            subprocess.run([*git, "init", "-q"], cwd=root, check=True)
            base = commit("base")
            (root / "tests" / "test_orders.py").write_text(WEAK_CANDIDATE, encoding="utf-8")
            (root / "tests" / "test_gone.py").unlink()
            (root / "tests" / "test_moved.py").write_text("def test_two():\n    assert 2\n", encoding="utf-8")
            (root / "tests" / "test_broken.py").write_text("def test_was_fine(:\n    assert 1\n", encoding="utf-8")
            (root / "docs" / "setup.md").write_text(f"old {TOKEN}\nnew {TOKEN}\n", encoding="utf-8")
            head = commit("candidate")
            changed = ["docs/setup.md", "tests/test_broken.py", "tests/test_gone.py", "tests/test_moved.py", "tests/test_orders.py"]
            request = critic.request({"id": "c1", "commit": head, "base": base, "provider": {}, "changed_paths": changed}, [], "", None)
            out = subprocess.run([PY, str(RULES)], cwd=root, input=json.dumps(request), capture_output=True, text=True, check=True).stdout
            request["base"] = "0" * 40
            unreadable = json.loads(subprocess.run([PY, str(RULES)], cwd=root, input=json.dumps(request), capture_output=True,
                                                   text=True, check=True).stdout)
        document = json.loads(out)
        check_schema(document, "finding.schema.json")
        self.assertEqual([(f["severity"], f["file"], f["line"], f["claim"].split(":")[0]) for f in document["findings"]], [
            ("high", "tests/test_broken.py", None, "a changed test file does not parse, so the test-integrity rule could not read it"),
            ("high", "docs/setup.md", 2, "secret added (ghp_)"),
            ("medium", "tests/test_gone.py", None, "test weakened (test_removed)"),
            ("medium", "tests/test_orders.py", None, "test weakened (test_removed)"),
            ("medium", "tests/test_orders.py", 8, "test weakened (test_skipped)"),
            ("medium", "tests/test_orders.py", 12, "test weakened (assertions_removed)")])
        self.assertIn("removes 1 test(s) that were on the base: test_one", document["findings"][2]["claim"], "test_two moved")
        self.assertNotIn("test_was_fine", out, "a file that does not parse is the integrity rule's finding, not a removed test")
        self.assertNotIn(TOKEN, out)
        self.assertIn("the base commit could not be read: no test was compared", unreadable["summary"])
        self.assertEqual([f["claim"].split(":")[0] for f in unreadable["findings"] if f["severity"] != "high"], [])


class WeakenedThroughTheThread(CriticCase):
    """A candidate that makes the `tests` check pass by skipping the test that would fail it."""

    CHANGES = {"src/report.py": REPORT.replace('{"name": "b", "qty": 3}', '{"name": "b", "qty": 3}, {"name": "c", "qty": 1}'),
               "tests/test_report.py": TEST_REPORT.replace("    def test_rows(self):\n", "    @unittest.skip('later')\n    def test_rows(self):\n")}

    def test_by_default_the_finding_is_in_the_chain_and_a_person_decides(self):
        kit, thread = self.thread([PY, str(RULES)], self.CHANGES)
        verdict = thread.check("c1")
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("critic", "passed"), ("tests", "passed")])
        self.assertTrue(verdict.acceptable, verdict.summary())
        [event] = self.observations(thread)
        value = event["body"]["value"]
        self.assertEqual((value["reviewer"], value["counts"]), ({"name": "rules", "revision": "6"}, {"high": 0, "medium": 1, "low": 0}))
        self.assertIn("test weakened (test_skipped): 1 test(s) gain a skip or an expected failure in this candidate: Report.test_rows",
                      value["findings"][0]["claim"])

    def test_with_the_option_the_same_candidate_is_refused(self):
        kit, thread = self.thread([PY, str(RULES), "--block-weakened-tests"], self.CHANGES)
        verdict = thread.check("c1")
        self.assertEqual((verdict.status, verdict.reason, verdict.rule), ("denied", "high_finding:critic:1", "critic.no_high_finding"))
        self.assertEqual(self.observations(thread)[0]["body"]["value"]["reviewer"], {"name": "rules", "revision": "6-block-weakened"})
        thread.propose(changes={"src/report.py": self.CHANGES["src/report.py"],
                                "tests/test_report.py": TEST_REPORT.replace("rows()), 2)", "rows()), 3)")}, message="fix the test instead")
        fixed = thread.check("c2")
        self.assertTrue(fixed.acceptable, fixed.summary())


class IntegrityThroughTheHost(CriticCase):
    def test_a_test_that_exits_at_import_passes_unittest_and_the_critic_refuses_it(self):
        """The `tests` check of this fixture is serial ``unittest discover``, which exits 0 when a module calls
        ``os._exit(0)`` at import; the reference critic's high finding is what refuses the candidate."""
        changes = {**GOOD, "tests/test_zz_exit.py": "import os\nos._exit(0)\n"}
        kit, thread = self.thread([PY, str(RULES)], changes)
        verdict = thread.check("c1")
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("critic", "passed"), ("tests", "passed")])
        self.assertEqual((verdict.status, verdict.rule), ("denied", "critic.no_high_finding"))
        self.assertTrue(verdict.reason.startswith("high_finding:critic:"), verdict.reason)
        [event] = self.observations(thread)
        claims = [f["claim"] for f in event["body"]["value"]["findings"]]
        self.assertTrue(any("(process_exit): os._exit" in c for c in claims), claims)
        self.assertTrue(all(f["file"] == "tests/test_zz_exit.py" for f in event["body"]["value"]["findings"]))


if __name__ == "__main__":
    unittest.main()
