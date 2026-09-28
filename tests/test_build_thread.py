"""The Build Thread against real Git repositories in temporary directories."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from spec.validate import validate_events
from syberlabs import Rejected
from syberlabs.build import GitRefEffect, Kit, default_contract
from syberlabs.cli import main as cli
from syberlabs.providers import CommandProvider, FunctionProvider

PY = sys.executable
GIT_ENV = {"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}

REPORT = 'def rows():\n    return [{"name": "a", "qty": 2}, {"name": "b", "qty": 3}]\n'
TEST_REPORT = textwrap.dedent('''\
    import os, sys, unittest
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import report

    class Report(unittest.TestCase):
        def test_rows(self):
            self.assertEqual(len(report.rows()), 2)
    ''')
EXPORT = textwrap.dedent('''\
    import csv, io
    from report import rows

    def to_csv():
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=["name", "qty"])
        writer.writeheader()
        writer.writerows(rows())
        return out.getvalue()
    ''')
TEST_EXPORT = textwrap.dedent('''\
    import os, sys, unittest
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import export

    class Export(unittest.TestCase):
        def test_header(self):
            self.assertEqual(export.to_csv().splitlines()[0], "name,qty")
    ''')
GOOD = {"src/export.py": EXPORT, "tests/test_export.py": TEST_EXPORT}


class Crash(BaseException):
    """Stands in for the process dying: nothing after it runs."""


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class RepoCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "report.py").write_text(REPORT)
        (self.root / "tests" / "test_report.py").write_text(TEST_REPORT)
        (self.root / "README.md").write_text("# Report tool\nRun tests with python -m unittest.\n")
        git(self.root.parent, "init", "-q", "-b", "main", str(self.root))
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "initial")
        self.head = git(self.root, "rev-parse", "HEAD")

    def kit(self, contract=None, policy=None, **kwargs):
        home = self.root / ".syberlabs"
        if contract is not None:
            (home / "contracts").mkdir(parents=True, exist_ok=True)
            (home / "contracts" / f"{contract['id']}.v{contract['version']}.json").write_text(json.dumps(contract))
        if policy is not None:
            home.mkdir(exist_ok=True)
            (home / "policy.json").write_text(json.dumps(policy))
        kit = Kit.local(home, self.root, actor=kwargs.pop("actor", "dev@example.test"), **kwargs)
        self.addCleanup(kit.close)
        return kit

    def contract(self, **changes):
        doc = default_contract(self.root)
        for path, value in changes.items():
            node = doc
            *parents, last = path.split(".")
            for part in parents:
                node = node[part]
            node[last] = value
        return doc

    def clean(self):
        return git(self.root, "status", "--porcelain") == "" and git(self.root, "rev-parse", "HEAD") == self.head


class Accept(RepoCase):
    def test_accept_moves_only_the_thread_branch(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export for report rows", paths=["src/", "tests/"])
        [candidate] = thread.propose(changes=GOOD, message="CSV export")
        self.assertEqual((candidate.id, candidate.promotion, candidate.authoritative), ("c1", "provisional", False))
        self.assertEqual(candidate.changed_paths, ("src/export.py", "tests/test_export.py"))
        self.assertTrue(self.clean(), "proposing must not touch the working tree")
        self.assertIn("csv.DictWriter", thread.diff("c1"))
        verdict = thread.check("c1")
        self.assertTrue(verdict.acceptable, verdict.summary())
        self.assertEqual([(c.name, c.state) for c in verdict.checks], [("tests", "passed")])
        receipt = thread.accept("c1")
        self.assertEqual(receipt.status, "succeeded")
        self.assertEqual(git(self.root, "rev-parse", thread.target_ref), candidate.commit)
        self.assertEqual(git(self.root, "rev-parse", "main"), self.head)
        self.assertTrue(self.clean(), "accepting must not touch the working tree or main")
        heads = git(self.root, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
        self.assertEqual(sorted(heads), sorted(["refs/heads/main", thread.target_ref]))
        status = thread.status()
        self.assertEqual((status["status"], status["accepted"], status["chain_valid"]), ("complete", "c1", True))
        self.assertEqual(validate_events(thread.history(), "thread"), [])
        again = thread.accept("c1")
        self.assertEqual((again.status, again.reason), ("denied", "action_already_completed"))

    def test_failing_candidate_is_refused_with_its_evidence(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        broken = {**GOOD, "src/report.py": 'def rows():\n    return []\n'}
        [candidate] = thread.propose(changes=broken)
        verdict = thread.check(candidate.id)
        self.assertFalse(verdict.acceptable)
        self.assertEqual(verdict.reason, "candidate_check_failed:tests")
        self.assertIn("FAIL", verdict.checks[0].output_tail)
        self.assertIn("syberlabs check", verdict.hint)
        receipt = thread.accept(candidate.id)
        self.assertEqual((receipt.status, receipt.reason), ("denied", "candidate_check_failed:tests"))
        self.assertIsNone(git_ref(self.root, thread.target_ref))

    def test_an_untested_claim_is_not_a_pass(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        claims = FunctionProvider(
            lambda space: [space.submit(GOOD, operator="patch", signal={"tests": "passed", "confidence": 0.99}).id],
            name="confident-model", revision="7")
        [candidate] = thread.propose(claims)
        self.assertEqual(candidate.signal["tests"], "passed")
        receipt = thread.accept(candidate.id)
        self.assertEqual((receipt.status, receipt.reason), ("denied", "candidate_not_evaluated"))
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual(search["recommended"], ["c1"])

    def test_out_of_scope_candidate_is_recorded_but_never_executed(self):
        marker = self.root.parent / "marker"
        contract = self.contract(**{
            "evolution.scope": {"paths": ["src/"]},
            "evolution.evaluation.checks": {"tests": {"argv": [PY, "-c", f"open({str(marker)!r}, 'w').write('ran')"]}},
        })
        kit = self.kit(contract)
        thread = kit.start("Add a CSV export")
        [candidate] = thread.propose(changes=GOOD)
        self.assertEqual(candidate.scope_violations, ("tests/test_export.py",))
        verdict = thread.check(candidate.id)
        self.assertFalse(marker.exists(), "an out-of-scope candidate's code must not run")
        self.assertEqual((verdict.reason, verdict.checks[0].state), ("candidate_out_of_scope", "not_run"))
        self.assertEqual(thread.accept(candidate.id).reason, "candidate_out_of_scope")

    def test_no_model_or_provider_can_accept(self):
        kit = self.kit(policy={"version": 1, "actions": {"accept_change": {"roles": ["developer", "model"]}}})
        thread = kit.start("Add a CSV export")
        seen = {}

        def provider(space):
            seen["api"] = {name for name in dir(space) if not name.startswith("_")}
            return [space.submit(GOOD, operator="patch").id]

        thread.propose(FunctionProvider(provider, name="model-adapter", revision="1"))
        self.assertFalse(seen["api"] & {"accept", "approve", "commit", "session", "kit", "propose"})
        thread.check("c1")
        receipt = thread.accept("c1", actor="planner", roles=["model"])
        self.assertEqual(receipt.reason, "candidate_promotion_origin")
        receipt = thread.accept("c1", actor="planner", roles=["developer", "model"])
        self.assertEqual(receipt.reason, "candidate_promotion_origin")
        self.assertIsNone(git_ref(self.root, thread.target_ref))
        self.assertEqual(thread.accept("c1").status, "succeeded")

    def test_independent_approval(self):
        contract = self.contract(**{"actions.accept_change": {"approval_role": "maintainer"},
                                    "evolution.promotion.approval_role": "maintainer"})
        kit = self.kit(contract)
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        self.assertEqual(thread.check("c1").status, "needs_approval")
        pending = thread.accept("c1")
        self.assertEqual((pending.status, pending.reason), ("needs_approval", "approval_required:maintainer"))
        self.assertIn("c1 waits for approval (approval_required:maintainer)", thread.status()["open"])
        with self.assertRaises(Rejected):
            thread.approve("c1", actor="dev@example.test", roles=["maintainer"])
        self.assertEqual(thread.approve("c1", actor="lead@example.test", roles=["maintainer"]).status, "approved")
        self.assertEqual(thread.accept("c1").status, "succeeded")

    def test_target_that_moved_is_a_no_write_rejection(self):
        git(self.root, "branch", "release")
        kit = self.kit(self.contract(**{"evolution.promotion.target_ref": "refs/heads/release"}))
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        self.assertTrue(thread.check("c1").acceptable)
        (self.root / "hotfix.txt").write_text("hotfix\n")
        git(self.root, "add", "hotfix.txt")
        git(self.root, "commit", "-qm", "hotfix")
        git(self.root, "branch", "-f", "release", "HEAD")
        moved = git(self.root, "rev-parse", "release")
        verdict = thread.check("c1")
        self.assertFalse(verdict.acceptable)
        self.assertFalse(verdict.base_current)
        receipt = thread.accept("c1")
        self.assertEqual((receipt.status, receipt.reason), ("rejected", "target_moved"))
        self.assertEqual(git(self.root, "rev-parse", "release"), moved)
        self.assertEqual(thread.candidates()[0].promotion, "not_applied")

    def test_checked_out_target_is_refused(self):
        kit = self.kit(self.contract(**{"evolution.promotion.target_ref": "refs/heads/main"}))
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        receipt = thread.accept("c1")
        self.assertEqual((receipt.status, receipt.reason), ("rejected", "target_checked_out"))
        self.assertTrue(self.clean())


def git_ref(root, ref):
    found = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", ref], capture_output=True, text=True)
    return found.stdout.strip() or None


class Recovery(RepoCase):
    def interrupted(self, after_write: bool):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.check("c1")
        original = GitRefEffect.apply

        def dies(effect, case_id, args, key):
            if after_write:
                original(effect, case_id, args, key)
            raise Crash()

        with patch.object(GitRefEffect, "apply", dies), self.assertRaises(Crash):
            thread.accept("c1")
        kit.close()
        restarted = self.kit()
        return restarted, restarted.open(thread.id[:6])

    def test_crash_after_the_write_is_verified_from_the_branch(self):
        kit, thread = self.interrupted(after_write=True)
        status = thread.status()
        self.assertEqual(status["next"], ["syberlabs recover"])
        self.assertEqual(thread.accept("c1").reason, "effect_unresolved:accept_change")
        [receipt] = thread.recover()
        self.assertEqual(receipt.status, "succeeded")
        self.assertEqual(thread.status()["status"], "complete")
        self.assertEqual(thread.candidates()[0].promotion, "promoted")
        self.assertEqual(validate_events(thread.history(), "recovered"), [])

    def test_crash_before_the_write_frees_the_action(self):
        kit, thread = self.interrupted(after_write=False)
        [receipt] = thread.recover()
        self.assertEqual((receipt.status, receipt.reason), ("not_applied", "destination_state_unchanged"))
        self.assertIsNone(git_ref(self.root, thread.target_ref))
        self.assertEqual(thread.accept("c1").status, "succeeded")
        self.assertEqual(validate_events(thread.history(), "retried"), [])

    def test_restart_resumes_from_the_journal_and_detects_tampering(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src", "tests"])
        thread.context()
        thread.propose(changes=GOOD)
        before = thread.status()
        kit.close()
        resumed = self.kit().open(thread.id)
        self.assertEqual(resumed.status(), before)
        path = self.root / ".syberlabs" / "journal" / "threads" / f"{thread.id}.jsonl"
        lines = path.read_text().splitlines()
        lines[1] = lines[1].replace("Add a CSV export", "Delete the database")
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(Rejected) as caught:
            self.kit().open(thread.id)
        self.assertEqual(caught.exception.code, "journal_corrupt")


class Search(RepoCase):
    def test_budget_bounds_candidates(self):
        kit = self.kit(self.contract(**{"evolution.budget": {"max_candidates": 2, "max_evaluations": 2}}))
        thread = kit.start("Add a CSV export")

        def greedy(space):
            for n in range(10):
                space.submit({"src/export.py": EXPORT + f"# variant {n}\n"}, operator="patch")

        found = thread.propose(FunctionProvider(greedy, name="greedy", revision="1"))
        self.assertEqual([c.id for c in found], ["c1", "c2"])
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual(search["stopped"], "budget_exhausted")
        with self.assertRaises(Rejected) as caught:
            thread.propose(changes=GOOD)
        self.assertEqual(caught.exception.code, "budget_exhausted")

    def test_provider_failure_is_recorded_and_the_thread_continues(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")

        def broken(space):
            space.submit(GOOD, operator="patch")
            raise RuntimeError("model returned garbage")

        found = thread.propose(FunctionProvider(broken, name="flaky", revision="2"))
        search = [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]
        self.assertEqual((search["stopped"], search["error"]), ("error", "provider_error"))
        self.assertEqual([c.id for c in found], ["c1"])
        with self.assertRaises(Rejected):
            thread.propose(FunctionProvider(lambda s: [s.submit({"src/x.py": "x"}, operator="mutation").id],
                                            name="sneaky", revision="1", operators=("mutation",)))

    def test_command_provider_is_the_model_seam(self):
        script = self.root.parent / "adapter.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            request = json.load(sys.stdin)
            assert request["protocol"] == "syberlabs.search/v0alpha1"
            assert any(item["path"] == "src/report.py" for item in request["context"])
            print(json.dumps({{"candidates": [{{"changes": {json.dumps(GOOD)}, "message": "csv",
                                "signal": {{"model": "fixture", "rationale": "reuse rows()"}}}}], "recommended": [0]}}))
            '''))
        kit = self.kit()
        thread = kit.start("Add a CSV export for report rows")
        [candidate] = thread.propose(CommandProvider([PY, str(script)], name="fixture-model", revision="2026-09"))
        self.assertEqual(candidate.provider, "fixture-model")
        self.assertEqual(candidate.signal["rationale"], "reuse rows()")
        started = [e["body"] for e in thread.history() if e["kind"] == "search_started"][-1]
        self.assertEqual(started["provider"], {"name": "fixture-model", "revision": "2026-09"})
        self.assertTrue(thread.check(candidate.id).acceptable)

    def test_context_records_references_not_text(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export for report rows", paths=["src", "tests", "README.md"])
        excerpts = thread.context()
        self.assertIn("README.md", [e.path for e in excerpts])
        self.assertIn("src/report.py", [e.path for e in excerpts])
        recorded = [e["body"]["value"] for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == "context"][-1]
        self.assertNotIn("def rows", json.dumps(recorded))
        dropped = thread.context(drop=("README.md",))
        self.assertNotIn("README.md", [e.path for e in dropped])

    def test_checks_are_bounded(self):
        contract = self.contract(**{"evolution.evaluation.checks": {
            "slow": {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout_seconds": 1},
            "loud": {"argv": [PY, "-c", "import sys; sys.stdout.write('x' * 5_000_000)"]}},
            "evolution.evaluation.required": ["slow", "loud"],
            "evolution.evaluation.max_output_bytes": 4096})
        kit = self.kit(contract)
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        verdict = thread.check("c1")
        states = {c.name: c.state for c in verdict.checks}
        self.assertEqual(states, {"slow": "timed_out", "loud": "error"})
        self.assertLessEqual(len(verdict.checks[1].output_tail), 4096)
        self.assertEqual(verdict.reason, "candidate_check_failed:slow")

    def test_worktree_snapshot_takes_only_attached_sources(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src", "tests"])
        for path, text in GOOD.items():
            (self.root / path).write_text(text)
        (self.root / "notes.txt").write_text("scratch\n")
        (self.root / "src" / "legacy.txt").write_bytes("caf\xe9\n".encode("latin-1"))
        changes, skipped = thread.worktree_changes()
        self.assertEqual(sorted(changes), sorted(GOOD))
        self.assertEqual(skipped, ["notes.txt", "src/legacy.txt"])


class Contracts(RepoCase):
    def test_published_contract_file_is_immutable(self):
        kit = self.kit()
        kit.close()
        path = self.root / ".syberlabs" / "contracts" / "repo-change.v1.json"
        doc = json.loads(path.read_text())
        doc["evolution"]["budget"]["max_candidates"] = 500
        path.write_text(json.dumps(doc))
        with self.assertRaises(Rejected) as caught:
            self.kit()
        self.assertIn("repo-change.v2.json", caught.exception.detail)
        doc["version"] = 2
        path.write_text(json.dumps({**doc, "version": 1, "evolution": default_contract(self.root)["evolution"]}))
        (path.parent / "repo-change.v2.json").write_text(json.dumps(doc))
        kit = self.kit()
        self.assertEqual(kit.contract_diff("repo-change.v1", "repo-change.v2"),
                         [{"path": "evolution.budget.max_candidates", "before": 12, "after": 500}])
        self.assertEqual(kit.start("x").contract, ("repo-change", 2))

    def test_memory_prune_and_export(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export")
        thread.propose(changes=GOOD)
        thread.propose(changes={"src/export.py": EXPORT + "# second\n"})
        thread.check("c1")
        thread.accept("c1")
        self.assertEqual(kit.memory()["candidate_refs"]["count"], 2)
        self.assertEqual(kit.prune(), 1)
        self.assertEqual(kit.memory()["candidate_refs"]["count"], 1)
        exported = kit.export(thread.id[:8])
        self.assertTrue(exported["chain_valid"])
        self.assertEqual(exported["status"]["accepted"], "c1")


class CommandLine(RepoCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"),
                        "--actor", "dev@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def test_quickstart_from_the_terminal(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        code, out, _ = self.run_cli("start", "Add a CSV export for report rows", "--paths", "src", "tests")
        self.assertIn("never pushes or merges", out)
        self.assertIn("src/report.py", self.run_cli("context")[1])
        for path, text in GOOD.items():
            (self.root / path).write_text(text)
        code, out, _ = self.run_cli("propose", "--from-worktree")
        self.assertIn("c1: 2 files", out)
        code, out, _ = self.run_cli("check", "c1")
        self.assertEqual(code, 0, out)
        self.assertIn("pass     tests", out)
        code, out, _ = self.run_cli("accept", "c1")
        self.assertIn("Nothing was pushed or merged", out)
        code, out, _ = self.run_cli("status")
        self.assertIn("promoted", out)
        self.assertIn("chain verified", out)
        code, _, err = self.run_cli("accept", "c9")
        self.assertEqual(code, 2)
        self.assertIn("unknown_candidate", err)


if __name__ == "__main__":
    unittest.main()
