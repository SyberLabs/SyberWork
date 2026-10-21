"""``syberlabs run``: every open thread's next safe step, from the journal, idempotent across restarts."""

import contextlib
import io
import json
import subprocess
import time
import unittest
from unittest.mock import patch

from spec.validate import validate_events
from syberlabs import Rejected
from syberlabs import build
from syberlabs.build import GitRefEffect, Runner, Thread
from syberlabs.cli import main as cli
from tests.test_build_thread import GOOD, Crash, RepoCase, died_after_case_created, git, git_ref

BAD = {"src/export.py": "def to_csv():\n    return None\n", "tests/test_export.py": GOOD["tests/test_export.py"]}
FACTS = [{"key": "accepted", "source": "git", "verified": True}]
BOUND = {"commit": "fact:accepted.commit"}


def kinds(thread, kind, **where):
    return [e for e in thread.history() if e["kind"] == kind and all(e["body"].get(k) == v for k, v in where.items())]


class RunnerCase(RepoCase):
    def runner(self, kit, **kwargs):
        lines = []
        return Runner(kit, log=lines.append, **kwargs), lines

    def started(self, kit, objective, *, unattended, changes=GOOD):
        thread = kit.start(objective, paths=["src/", "tests/"], unattended=unattended)
        thread.propose(changes=changes, message=objective)
        return thread

    def assertSound(self, thread, label):
        self.assertTrue(thread.kit.session.verify_chain(thread.id), label)
        self.assertEqual(validate_events(thread.history(), label), [], label)


class Passes(RunnerCase):
    def test_a_pass_checks_then_accepts_only_an_unattended_thread(self):
        kit = self.kit()
        auto = self.started(kit, "Add a CSV export", unattended=True)
        manual = self.started(kit, "Add a CSV export, attended", unattended=False)
        self.assertTrue(auto.unattended)
        self.assertFalse(manual.unattended)
        [marker] = [e for e in auto.history() if e["kind"] == "observed" and e["body"]["key"] == "unattended"]
        self.assertEqual((marker["body"]["source"], marker["body"]["verified"]), ("syberlabs.host", True))
        runner, lines = self.runner(kit)

        first = runner.once()
        self.assertEqual([(s.thread, s.step, s.candidate, s.outcome) for s in first],
                         [(auto.id, "check", "c1", "acceptable"), (manual.id, "check", "c1", "acceptable")])
        self.assertEqual(lines[0], f"{auto.id[:8]} check c1 -> acceptable")

        second = runner.once()
        self.assertEqual([(s.step, s.outcome) for s in second], [("accept", "succeeded"), ("idle", "nothing to do")])
        self.assertIn("syberlabs accept c1", second[1].reason)
        self.assertEqual(git_ref(self.root, auto.target_ref), auto.candidates()[0].commit)
        self.assertIsNone(git_ref(self.root, manual.target_ref), "an attended thread is never accepted by the runner")

        third = runner.once()
        self.assertEqual([s.step for s in third], ["idle", "idle"])
        self.assertEqual(third[0].reason, "accepted; publishing needs --publish")
        self.assertFalse(any(s.taken for s in third), "nothing left to do")
        for thread in (auto, manual):
            self.assertEqual(len(kinds(thread, "candidate_evaluated")), 1, "a checked tree is not checked again")
            self.assertSound(thread, "three passes")
        self.assertTrue(auto.status()["unattended"])

    def test_a_failed_check_is_refused_once_and_not_retried(self):
        kit = self.kit()
        thread = self.started(kit, "Break the export", unattended=True, changes=BAD)
        runner, _ = self.runner(kit)
        [step] = runner.once()
        self.assertEqual((step.step, step.outcome, step.reason), ("check", "refused", "candidate_check_failed:tests"))
        self.assertTrue(step.refused)
        [again] = runner.once()
        self.assertEqual((again.step, again.reason), ("idle", "candidate_check_failed"))
        self.assertEqual(len(kinds(thread, "candidate_evaluated")), 1)
        self.assertIsNone(git_ref(self.root, thread.target_ref))

    def test_recovery_comes_before_anything_new(self):
        kit = self.kit()
        thread = self.started(kit, "Add a CSV export", unattended=True)
        thread.check("c1")
        original = GitRefEffect.apply

        def dies(effect, case_id, args, key):
            original(effect, case_id, args, key)
            raise Crash()

        with patch.object(GitRefEffect, "apply", dies), self.assertRaises(Crash):
            thread.accept("c1")
        kit.close()
        restarted = self.kit()
        runner, lines = self.runner(restarted)
        steps = runner.once()
        self.assertEqual([(s.step, s.outcome, s.reason) for s in steps],
                         [("recover", "succeeded", "destination_state_matched"),
                          ("idle", "nothing to do", "accepted; publishing needs --publish")])
        reopened = restarted.open(thread.id)
        self.assertEqual(len(kinds(reopened, "effect_started")), 1, "the interrupted acceptance was settled, not repeated")
        self.assertEqual(reopened.candidates()[0].promotion, "promoted")
        self.assertSound(reopened, "recovered by the runner")

    def test_evidence_about_to_age_out_is_rechecked_not_accepted(self):
        kit = self.kit(self.contract(**{"evolution.evaluation.max_age_seconds": 120}))
        thread = self.started(kit, "Add a CSV export", unattended=True)
        runner, _ = self.runner(kit)
        [checked] = runner.once()
        self.assertEqual((checked.step, checked.outcome), ("check", "acceptable"))
        real = time.time
        with patch.object(build.time, "time", lambda: real() + 70):  # 50 seconds of life left: inside the margin
            [again] = runner.once()
        self.assertEqual((again.step, again.outcome), ("check", "acceptable"), "re-run, not accepted")
        self.assertEqual(len(kinds(thread, "candidate_evaluated")), 2)
        self.assertEqual(kinds(thread, "effect_started"), [], "no acceptance was proposed on aging evidence")
        [accepted] = runner.once()
        self.assertEqual((accepted.step, accepted.outcome), ("accept", "succeeded"))
        self.assertSound(thread, "rechecked")

    def test_waiting_states_are_idle_not_retried(self):
        contract = self.contract()
        contract["actions"]["accept_change"] = {"approval_role": "maintainer"}
        contract["evolution"]["promotion"]["approval_role"] = "maintainer"
        kit = self.kit(contract)
        thread = self.started(kit, "Add a CSV export", unattended=True)
        runner, _ = self.runner(kit)
        runner.once()
        [step] = runner.once()
        self.assertEqual((step.step, step.outcome), ("accept", "needs_approval"))
        self.assertFalse(step.refused)
        [waiting] = runner.once()
        self.assertEqual((waiting.step, waiting.reason), ("idle", "approval_required"))
        self.assertEqual(len(kinds(thread, "proposed")), 1, "one pending proposal, reused, never a second")
        thread.approve("c1", actor="reviewer@example.test", roles=["maintainer"])
        [done] = runner.once()
        self.assertEqual((done.step, done.outcome), ("accept", "succeeded"))
        self.assertEqual(len(kinds(thread, "proposed")), 1)
        self.assertSound(thread, "approved then run")

    def test_publish_runs_the_contract_actions_in_prerequisite_order_only_when_asked(self):
        bare = self.root.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        git(self.root, "remote", "add", "origin", str(bare))
        contract = self.contract()
        contract["actions"]["push_branch"] = {"requires_effect": "accept_change", "required_facts": FACTS, "arguments": BOUND}
        policy = {"version": 1, "actions": {name: {"roles": ["developer"]} for name in contract["actions"]}}
        home = self.root / ".syberlabs"
        home.mkdir(exist_ok=True)
        (home / "actions.json").write_text(json.dumps(
            {"push_branch": {"kind": "local", "effect": "git_push", "remote": "origin", "settle_seconds": 0}}))
        kit = self.kit(contract, policy)
        thread = self.started(kit, "Add a CSV export", unattended=True)
        quiet, _ = self.runner(kit)
        quiet.once()
        quiet.once()
        [idle] = quiet.once()
        self.assertEqual((idle.step, idle.reason), ("idle", "accepted; publishing needs --publish"))
        publishing, lines = self.runner(kit, publish=True)
        [pushed] = publishing.once()
        self.assertEqual((pushed.step, pushed.candidate, pushed.outcome), ("publish", "push_branch", "succeeded"))
        self.assertEqual(subprocess.run(["git", "--git-dir", str(bare), "rev-parse", thread.target_ref],
                                        capture_output=True, text=True).stdout.strip(), thread.candidates()[0].commit)
        [done] = publishing.once()
        self.assertEqual((done.step, done.reason), ("idle", "accepted and published"))
        self.assertSound(thread, "published by the runner")


class Restarts(RunnerCase):
    """Kill the runner at every step boundary: the chain stays valid and no step repeats."""

    def test_interrupts_and_refusals_between_steps_repeat_nothing(self):
        kit = self.kit()
        first = self.started(kit, "Add a CSV export", unattended=True)
        second = self.started(kit, "Add a CSV export, second thread", unattended=True)
        ids = (first.id, second.id)
        kit.close()
        check, accept = Thread.check, Thread.accept

        def check_then_die(thread, candidate, **kwargs):
            verdict = check(thread, candidate, **kwargs)
            raise KeyboardInterrupt  # the step completed; the process died before the next one

        def refuse_accept(thread, candidate, **kwargs):
            raise Rejected("io_error", "journal unavailable")  # refused before anything was written

        def accept_then_die(thread, candidate, **kwargs):
            receipt = accept(thread, candidate, **kwargs)
            raise KeyboardInterrupt

        injected = [("check", check_then_die), ("check", check_then_die), ("accept", refuse_accept),
                    ("accept", accept_then_die), (None, None), ("accept", accept_then_die)]
        passes = 0
        for name, fake in injected + [(None, None)] * 4:
            restarted = self.kit()
            runner, _ = self.runner(restarted)
            try:
                if name is None:
                    steps = runner.once()
                else:
                    with patch.object(Thread, name, fake):
                        steps = runner.once()
            except KeyboardInterrupt:
                steps = None
            finally:
                restarted.close()
            passes += 1
            if steps is not None and not any(s.taken for s in steps):
                break
        self.assertLessEqual(passes, len(injected) + 2, "every restart made progress")
        kit = self.kit()
        for case_id in ids:
            thread = kit.open(case_id)
            self.assertEqual(len(kinds(thread, "candidate_evaluated")), 1, "one check per candidate across all restarts")
            self.assertEqual(len(kinds(thread, "effect_succeeded", action="accept_change")), 1, "one acceptance")
            self.assertEqual(len(kinds(thread, "effect_started")), 1, "no second claim")
            self.assertEqual(thread.status()["status"], "complete")
            self.assertEqual(git_ref(self.root, thread.target_ref), thread.candidates()[0].commit)
            self.assertSound(thread, "after interrupts")
        runner, lines = self.runner(kit)
        self.assertFalse(any(s.taken for s in runner.once()), "nothing left")


class Terminal(RunnerCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"),
                        "--actor", "dev@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def test_run_once_from_the_terminal(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        code, out, _ = self.run_cli("start", "Add a CSV export", "--paths", "src", "tests", "--unattended")
        self.assertEqual(code, 0, out)
        self.assertIn("unattended: syberlabs run may accept", out)
        self.assertIn("mode      unattended", self.run_cli("status")[1])
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, 0)
        self.assertIn("idle - -> nothing to do (no candidate; the runner never proposes)", out)
        self.assertIn("pass: 0 steps, 0 refused; nothing left to do", out)
        for path, text in GOOD.items():
            (self.root / path).write_text(text)
        self.assertEqual(self.run_cli("propose", "--from-worktree")[0], 0)
        thread = self.run_cli("status", "--json")[1]
        thread8 = json.loads(thread)["thread"][:8]
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, 0, out)
        self.assertIn(f"{thread8} check c1 -> acceptable", out)
        self.assertIn("pass: 1 steps, 0 refused", out)
        code, out, _ = self.run_cli("run", "--once", "--thread", thread8[:6])
        self.assertEqual(code, 0, out)
        self.assertIn(f"{thread8} accept c1 -> succeeded", out)
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, 0)
        self.assertIn("nothing left to do", out)
        self.assertIn("promoted", self.run_cli("status")[1])

    def test_a_refused_step_exits_one_and_an_incomplete_start_is_skipped(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        code, _, _ = self.run_cli("start", "Break the export", "--paths", "src", "tests", "--unattended")
        for path, text in BAD.items():
            (self.root / path).write_text(text)
        self.assertEqual(self.run_cli("propose", "--from-worktree")[0], 0)
        kit = self.kit()
        partial = died_after_case_created(kit, "Something else")
        kit.close()
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, 1, out)
        self.assertIn("check c1 -> refused (candidate_check_failed:tests)", out)
        self.assertIn(f"{partial[:8]} skip - -> incomplete start", out)
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, 0, out)
        self.assertIn("idle c1 -> nothing to do (candidate_check_failed)", out)
        code, _, err = self.run_cli("run", "--once", "--thread", "nosuch")
        self.assertEqual(code, 2)
        self.assertIn("unknown_thread", err)

    def test_a_loop_stops_cleanly_on_interrupt_and_an_unexpected_error_exits_two(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.run_cli("start", "Add a CSV export", "--paths", "src", "tests")
        with patch.object(build.time, "sleep", side_effect=KeyboardInterrupt):
            code, out, _ = self.run_cli("run", "--interval", "0")
        self.assertEqual(code, 0)
        self.assertIn("nothing left to do", out)
        self.assertTrue(out.rstrip().endswith("stopped"), out)
        with patch.object(Thread, "recover", side_effect=OSError("disk gone")):
            code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, 2)
        self.assertIn("error: OSError: disk gone", out)


if __name__ == "__main__":
    unittest.main()
