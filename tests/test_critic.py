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
from tests.test_build_thread import EXPORT, GOOD, RepoCase, default_contract

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
                                                                        "_addExpectedFailure", "_addUnexpectedSuccess")]
    + ["from unittest import TestCase as Base\n\nclass T(Base):\n    def _callSetUp(self):\n        pass\n",
       "class T:\n    def test_a(self):\n        pass\n    run = None\n"],
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
        self.assertEqual(document["reviewer"], {"name": "rules", "revision": "3"})

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
