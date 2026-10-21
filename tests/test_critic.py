"""The critic station: an independent reviewer as a host check, its findings in the chain, and the rule that reads them."""

import contextlib
import hashlib
import io
import json
import sys
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


if __name__ == "__main__":
    unittest.main()
