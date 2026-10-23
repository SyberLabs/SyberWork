"""The repair provider: one proposal, then repairs directed by the host's check results.

Every model call in these tests is a local script that answers the way an adapter would and logs the
request it was given. No model is called. ``CommandProvider``'s own tests are in ``test_build_thread.py``
and are untouched: the repair loop is a second provider beside it, not a change to it.
"""

import contextlib
import io
import json
import sys
import textwrap
import unittest

from adapters import anthropic_adapter
from spec.validate import validate_events
from syberlabs import Rejected
from syberlabs.cli import main as cli
from syberlabs.events import verify_events
from syberlabs.evolve import mutation_feedback
from syberlabs.providers import PROTOCOL, FunctionProvider, RepairProvider, model_usage, repair_feedback
from tests.test_build_thread import EXPORT, GOOD, REPORT, RepoCase

PY = sys.executable
BROKEN = {**GOOD, "src/report.py": "def rows():\n    return []\n"}  # tests fail: report rows are gone
BROKEN_AGAIN = {**BROKEN, "src/report.py": "def rows():\n    return []  # again\n"}
FIXED = {**GOOD, "src/report.py": REPORT}  # a repair edits the failed candidate, so it restores the rows too
PRICES = {"claude-opus-5": {"input_per_million": 15, "output_per_million": 75}}
USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "model": "claude-opus-5", "stop_reason": "end_turn"}
COST = round(1200 * 15 / 1_000_000 + 300 * 75 / 1_000_000, 6)  # 0.0405, the host's arithmetic
CHECK_FIELDS = ("name", "state", "exit_code", "duration_ms", "output_tail")


def item(changes, message="m"):
    return {"changes": changes, "message": message}


def search_of(thread):
    return [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]


def refusals_of(thread):
    return [(r["reason"], r["operator"], r["parents"], r["diff"] is not None, r["model_usage"])
            for r in (e["body"]["value"] for e in thread.history()
                      if e["kind"] == "observed" and e["body"]["key"] == "proposal_refused")]


class RepairCase(RepoCase):
    def adapter(self, name, *, answers=None, by_feedback=None, usage=None):
        """A stand-in adapter. It logs every request to ``<name>.log`` (outside the repository) and answers the
        ``answers`` entry for its call number (the last one repeated; ``"exit"`` fails the command), or, with
        ``by_feedback``, the ``with``/``without`` entry by whether the request carries ``feedback``."""
        log = self.root.parent / f"{name}.log"
        script = self.root.parent / f"{name}.py"
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            from pathlib import Path
            request = json.load(sys.stdin)
            log = Path({str(log)!r})
            calls = log.read_text(encoding="utf-8").count("\\n") if log.exists() else 0
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(request) + "\\n")
            answers, by_feedback, usage = {answers!r}, {by_feedback!r}, {usage!r}
            if by_feedback is not None:
                items = by_feedback["with" if request.get("feedback") else "without"]
            else:
                items = answers[min(calls, len(answers) - 1)]
            if items == "exit":
                sys.exit(1)
            body = {{"candidates": items, "recommended": [0]}}
            if usage is not None:
                body["usage"] = usage
            print(json.dumps(body))
            '''), encoding="utf-8")
        return [PY, str(script)]

    def requests(self, name):
        log = self.root.parent / f"{name}.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


class Repair(RepairCase):
    def test_a_failed_attempt_is_repaired_with_the_hosts_check_results(self):
        """The second request carries ``feedback``: the first candidate's recorded checks and its file texts."""
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        argv = self.adapter("once", by_feedback={"without": [item(BROKEN)], "with": [item(FIXED)]}, usage=USAGE)
        provider = RepairProvider(argv, name="fixture-model", revision="2026-10")
        found = thread.propose(provider, seed=3)
        self.assertEqual([c.id for c in found], ["c1", "c2"])
        first, second = found
        self.assertEqual((first.evaluation, second.evaluation, second.parents), ("failed", "passed", ("c1",)))
        self.assertEqual((first.provider, first.provider_revision), ("fixture-model", "2026-10"))
        requests = self.requests("once")
        self.assertEqual(len(requests), 2)
        self.assertEqual(set(requests[0]), {"protocol", "objective", "base", "scope", "budget", "context", "files", "seed"},
                         "the first request is the one CommandProvider sends, plus the seed")
        self.assertEqual((requests[0]["protocol"], requests[0]["base"]), (PROTOCOL, thread.base))
        self.assertIn("src/report.py", requests[0]["files"])
        self.assertNotEqual(requests[0]["seed"], requests[1]["seed"], "one seeded stream, a fresh seed per call")
        feedback = requests[1]["feedback"]
        self.assertEqual(list(feedback), ["candidate", "files", "checks"], "the harness's object, key for key")
        self.assertEqual(feedback["candidate"], "c1")
        self.assertEqual(feedback["files"], BROKEN, "the failed candidate's texts, read from its tree")
        history = thread.history()
        recorded = [e["body"] for e in history if e["kind"] == "candidate_evaluated" and e["body"]["candidate"] == "c1"][-1]
        self.assertEqual(feedback["checks"], [{k: c[k] for k in CHECK_FIELDS} for c in recorded["checks"]],
                         "the checks are the host's record of c1, not the provider's reading")
        self.assertEqual(feedback["checks"][0]["state"], "failed")
        self.assertIn("did not pass the host's checks", anthropic_adapter.build_prompt(requests[1], self.root))
        search = search_of(thread)
        self.assertEqual((search["stopped"], search["recommended"], search["candidates"], search["evaluations"], search["error"]),
                         ("completed", ["c2"], ["c1", "c2"], 2, None))
        for body in (e["body"] for e in history if e["kind"] == "candidate_registered"):
            self.assertEqual(body["signal"]["model_usage"], model_usage({"usage": USAGE}), "the adapter's declaration stays in the signal")
        self.assertEqual(provider.trace, [
            {"call": 1, "parent": "base", "feedback": False, "outcome": "failed", "candidate": "c1", "score": [0, 0]},
            {"call": 2, "parent": "c1", "feedback": True, "outcome": "passed", "candidate": "c2", "score": [1, 1]},
            {"stopped": "passing", "calls": 2, "refused": {}}])
        self.assertEqual(provider.calls, 2)
        self.assertEqual(validate_events(history, "repair"), [])
        self.assertTrue(verify_events(history))
        self.assertTrue(thread.check("c2").acceptable)

    def test_an_adapter_that_never_passes_stops_at_max_calls_with_the_best_partial(self):
        doc = self.contract()
        doc["evolution"]["evaluation"]["checks"]["importable"] = {
            "argv": [PY, "-c", "import sys; sys.path.insert(0, 'src'); import export"], "timeout_seconds": 60}
        kit = self.kit(doc)
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        worse = {"src/report.py": BROKEN["src/report.py"]}  # tests fail, nothing to import: (0, 0)
        partial = {**worse, "src/export.py": EXPORT}  # tests still fail, export imports: (0, 1)
        worse_again = {"src/report.py": BROKEN_AGAIN["src/report.py"], "src/export.py": None}  # a child of c2: drop its export
        argv = self.adapter("never", answers=[[item(worse)], [item(partial)], [item(worse_again)]])
        provider = RepairProvider(argv, max_calls=3)
        found = thread.propose(provider)
        self.assertEqual([(c.id, c.parents, c.evaluation) for c in found],
                         [("c1", (), "failed"), ("c2", ("c1",), "failed"), ("c3", ("c2",), "failed")])
        requests = self.requests("never")
        self.assertEqual([r.get("feedback", {}).get("candidate") for r in requests], [None, "c1", "c2"],
                         "every call after the first repairs the last evaluated candidate")
        search = search_of(thread)
        self.assertEqual((search["stopped"], search["recommended"], search["evaluations"]), ("completed", ["c2"], 3),
                         "no candidate passed; the best partial by score is recommended, a signal and not a verdict")
        self.assertEqual([e["score"] for e in provider.trace[:-1]], [[0, 0], [0, 1], [0, 0]])
        self.assertEqual(provider.trace[-1], {"stopped": "calls", "calls": 3, "refused": {}})
        self.assertFalse(thread.check("c2").acceptable)

    def test_the_default_call_budget_is_the_searchs_candidate_budget(self):
        kit = self.kit(self.contract(**{"evolution.budget": {"max_candidates": 3, "max_evaluations": 6}}))
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        provider = RepairProvider(self.adapter("empty", answers=[[]]))
        self.assertEqual(thread.propose(provider), [])
        self.assertEqual(provider.calls, 3, "an adapter that proposes nothing is still bounded by the candidate budget")
        self.assertEqual(provider.trace[-1], {"stopped": "calls", "calls": 3, "refused": {"no_change": 3}})
        self.assertEqual(refusals_of(thread), [("no_change", "patch", ["base"], False, None)] * 3,
                         "an empty answer is a proposal the provider refused itself, one record each")
        search = search_of(thread)
        self.assertEqual((search["stopped"], search["candidates"], search["recommended"]), ("completed", [], []))
        with self.assertRaises(Rejected) as caught:
            RepairProvider(self.adapter("empty"), max_calls=0)
        self.assertEqual(caught.exception.code, "invalid_provider")

    def test_a_priced_thread_stops_at_max_dollars_with_the_governors_refusal(self):
        """One call fits, two do not: the second is bought and registered, nothing more is evaluated or bought, the
        provider stops cleanly, the host writes budget_exhausted, and admission refuses by the governor's rule."""
        budget = {"max_candidates": 6, "max_evaluations": 6, "max_seconds": 120, "prices": PRICES, "max_dollars": 0.05}
        kit = self.kit(self.contract(**{"evolution.budget": budget}))
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        argv = self.adapter("priced", answers=[[item(BROKEN)], [item(FIXED)]], usage=USAGE)
        provider = RepairProvider(argv, name="model")
        found = thread.propose(provider)
        self.assertEqual([(c.id, c.evaluation) for c in found], [("c1", "failed"), ("c2", "none")],
                         "the call that crossed the line is registered; no evaluation is bought over budget")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(self.requests("priced")[1]["feedback"]["candidate"], "c1", "the second call was a directed repair")
        history = thread.history()
        for body in (e["body"] for e in history if e["kind"] == "candidate_registered"):
            self.assertEqual((body["spend"]["dollars"], body["spend"]["host_observed"]), (COST, True))
        search = search_of(thread)
        self.assertEqual((search["stopped"], search["recommended"], search["evaluations"]), ("budget_exhausted", ["c1"], 1))
        self.assertEqual((search["spend"]["dollars"], search["spend"]["candidates"], search["spend"]["host_observed"]),
                         (round(2 * COST, 6), 2, True))
        self.assertEqual(provider.trace[1]["outcome"], "unevaluated")
        self.assertEqual(provider.trace[-1], {"stopped": "budget", "calls": 2, "refused": {}})
        self.assertEqual(thread.check("c1").reason, "candidate_check_failed:tests", "the failed first attempt is refused on its evidence")
        verdict = thread.check("c2")  # the repair passes its checks when a person runs them; the governor still refuses it
        self.assertEqual([c.state for c in verdict.checks], ["passed"])
        self.assertEqual((verdict.acceptable, verdict.reason, verdict.rule), (False, "over_budget:dollars", "budget.promotion"))
        receipt = thread.accept("c2")
        self.assertEqual((receipt.status, receipt.reason), ("denied", "over_budget:dollars"))
        decision = [e["body"] for e in thread.history() if e["kind"] == "decision"][-1]
        self.assertEqual((decision["status"], decision["reason"]), ("denied", "over_budget:dollars"), "the refusal is in the journal")
        # Nothing more is bought: the next repair search stops before its first call.
        again = RepairProvider(self.adapter("again", answers=[[item(GOOD)]], usage=USAGE), name="model")
        self.assertEqual(thread.propose(again), [])
        self.assertEqual((again.calls, again.trace), (0, [{"stopped": "budget", "calls": 0, "refused": {}}]))
        self.assertEqual(search_of(thread)["stopped"], "budget_exhausted")
        self.assertEqual(validate_events(thread.history(), "repair-priced"), [])

    def test_refused_proposals_leave_records_and_the_loop_goes_on(self):
        """An empty answer (the provider's refusal), an unchanged tree (the host's) and a second item in one answer
        (``limit``) each leave one record; the loop continues to the candidate that passes."""
        kit = self.kit(self.contract(**{"evolution.budget": {"max_candidates": 3, "max_evaluations": 6}}))
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        answers = [[], [item({"src/report.py": REPORT})], [item(GOOD), item({"src/report.py": "x = 1\n"})]]
        provider = RepairProvider(self.adapter("refused", answers=answers, usage=USAGE))
        found = thread.propose(provider)
        self.assertEqual([(c.id, c.parents, c.evaluation) for c in found], [("c1", (), "passed")])
        declared = model_usage({"usage": USAGE})
        self.assertEqual(refusals_of(thread), [
            ("no_change", "patch", ["base"], False, declared),  # the provider's: an empty answer, no diff to name
            ("no_change", "patch", ["base"], True, declared),  # the host's: the tree is the base's, diff named
            ("limit", "patch", ["base"], False, declared)])  # the provider's: one candidate per call
        self.assertEqual([e["outcome"] for e in provider.trace[:-1]], ["refused:no_change", "refused:no_change", "passed"])
        self.assertEqual(provider.trace[-1], {"stopped": "passing", "calls": 3, "refused": {"no_change": 2, "limit": 1}})
        self.assertTrue(all("feedback" not in r for r in self.requests("refused")), "nothing was evaluated before c1")
        search = search_of(thread)
        self.assertEqual((search["stopped"], search["recommended"], search["candidates"]), ("completed", ["c1"], ["c1"]))
        self.assertEqual(validate_events(thread.history(), "repair-refused"), [])

    def test_a_malformed_answer_is_refused_and_a_failing_command_ends_the_search(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        answers = [{"not": "a list"}, [{"changes": "not an object"}], [item(BROKEN)], "exit"]
        provider = RepairProvider(self.adapter("broken", answers=answers), max_calls=6)
        found = thread.propose(provider)
        self.assertEqual([c.id for c in found], ["c1"])
        self.assertEqual([r[:3] for r in refusals_of(thread)],
                         [("unparsable", "patch", ["base"]), ("unparsable", "patch", ["base"]), ("provider_error", "patch", ["c1"])])
        self.assertEqual([e["outcome"] for e in provider.trace], ["refused:unparsable", "refused:unparsable", "failed", "provider_error"])
        search = search_of(thread)
        self.assertEqual((search["stopped"], search["error"], search["recommended"]), ("error", "provider_failed", []))
        self.assertEqual(validate_events(thread.history(), "repair-broken"), [])

    def test_repair_feedback_is_the_harness_object_and_none_before_evaluation(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        seen = {}

        def probe(space):
            first = space.submit(BROKEN, operator="patch")
            seen["before"] = repair_feedback(space, first.id)
            space.evaluate(first.id)
            seen["after"] = repair_feedback(space, first.id)
            view = space.candidate(first.id)
            seen["harness"] = {"candidate": first.id,  # Patches.search, benchmarks/model_vs_patch.py
                               "files": {p: space.read(first.id, p) for p in view.changed_paths if space.read(first.id, p) is not None},
                               "checks": [c.__dict__ for c in view.checks]}
            seen["mutation"] = mutation_feedback(space, first.id)
            return [first.id]

        thread.propose(FunctionProvider(probe, name="probe", revision="1"))
        self.assertIsNone(seen["before"], "no evaluation is not the same as an evaluation that passed")
        self.assertEqual(seen["after"], seen["harness"])
        self.assertEqual(list(seen["after"]), ["candidate", "files", "checks"])
        self.assertEqual(seen["after"]["checks"], seen["mutation"]["checks"], "one check shape, from evolve.mutation_feedback")
        self.assertEqual(seen["after"]["files"], BROKEN)
        json.dumps(seen["after"])


class CommandLine(RepairCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"),
                        "--actor", "dev@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def test_repair_from_the_terminal(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        code, out, _ = self.run_cli("start", "Add a CSV export for report rows", "--paths", "src", "tests")
        self.assertEqual(code, 0, out)
        command = " ".join(self.adapter("cli", by_feedback={"without": [item(BROKEN)], "with": [item(FIXED)]}))
        code, out, err = self.run_cli("propose", "--repair", command, "--max-calls", "3", "--seed", "5")
        self.assertEqual(code, 0, out + err)
        self.assertIn("c1: 3 files", out)
        self.assertIn("c2: 2 files", out)
        self.assertIn("search s1 completed; provider recommends c2", out)
        self.assertIn("next: syberlabs check c2", out)
        self.assertEqual([r.get("feedback", {}).get("candidate") for r in self.requests("cli")], [None, "c1"])
        status = self.run_cli("status")[1]
        self.assertIn("[repair@1]", status, "the default provider name for --repair")
        code, _, err = self.run_cli("propose", "--repair", command, "--max-calls", "0")
        self.assertEqual(code, 2)
        self.assertIn("invalid_provider", err)
        code, out, _ = self.run_cli("check", "c2")
        self.assertEqual(code, 0, out)
        self.assertIn("acceptable", out)


if __name__ == "__main__":
    unittest.main()
