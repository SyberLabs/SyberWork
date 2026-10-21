"""The governor: spend the host measured itself, a dollar budget in the contract, and the two budget rules.

Every model call in these tests is a local script that reports a ``usage`` the way an adapter
would. No model is called. The host prices only what crossed ``providers.run_json``.
"""

import contextlib
import io
import json
import sys
import textwrap
import unittest

from spec.validate import validate_events
from syberlabs import Rejected
from syberlabs.build import SpendBudget
from syberlabs.cli import _print_status
from syberlabs.economic import check_prices, price_usage
from syberlabs.evolution import candidate_record, check_evolution, search_record, settings, spend_total
from syberlabs.providers import CommandProvider, FunctionProvider, run_json
from tests.test_build_thread import EXPORT, GOOD, TEST_EXPORT, RepoCase

PRICES = {"claude-opus-5": {"input_per_million": 15, "output_per_million": 75}}
USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "model": "claude-opus-5", "stop_reason": "end_turn"}
COST = round(1200 * 15 / 1_000_000 + 300 * 75 / 1_000_000, 6)  # 0.0405, the host's arithmetic
OTHER = {"src/export.py": EXPORT + "\n# a second, equally passing, export\n", "tests/test_export.py": TEST_EXPORT}


def budget(**extra):
    return {"max_candidates": 6, "max_evaluations": 6, "max_seconds": 120, "prices": PRICES, **extra}


class Prices(RepoCase):
    def test_the_price_table_and_dollar_budget_are_validated_fail_closed(self):
        bad = {
            "max_dollars without prices": {"max_candidates": 2, "max_dollars": 1},
            "strict without prices": {"max_candidates": 2, "strict": True},
            "unknown rate field": budget(prices={"m": {"input_per_million": 1, "output_per_million": 1, "per_call": 1}}),
            "missing output rate": budget(prices={"m": {"input_per_million": 1}}),
            "negative rate": budget(prices={"m": {"input_per_million": -1, "output_per_million": 1}}),
            "boolean rate": budget(prices={"m": {"input_per_million": True, "output_per_million": 1}}),
            "empty table": budget(prices={}),
            "zero dollars": budget(max_dollars=0),
            "nan dollars": budget(max_dollars=float("nan")),
            "strict as text": budget(strict="yes"),
        }
        for name, value in bad.items():
            with self.subTest(case=name), self.assertRaises(Rejected) as caught:
                check_evolution(self.contract(**{"evolution.budget": value}))
            self.assertEqual(caught.exception.code, "invalid_contract")
        self.assertIsNone(check_prices(PRICES))
        doc = self.contract(**{"evolution.budget": budget(max_dollars=2.5, strict=True)})
        check_evolution(doc)
        found = settings(doc)["budget"]
        self.assertEqual((found["prices"], found["max_dollars"], found["strict"]), (PRICES, 2.5, True))
        plain = settings(self.contract())["budget"]
        self.assertEqual((plain["prices"], plain["max_dollars"], plain["strict"]), (None, None, False))
        self.assertEqual(set(plain), {"max_candidates", "max_evaluations", "max_seconds", "prices", "max_dollars", "strict"})

    def test_pricing_is_the_host_arithmetic(self):
        self.assertEqual(price_usage(USAGE, PRICES), COST)
        self.assertIsNone(price_usage({**USAGE, "model": "mystery"}, PRICES))
        self.assertIsNone(price_usage({"input_tokens": 5}, PRICES), "usage without a model name is unpriced")
        cached = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000, "output_tokens": 0, "model": "m"}
        self.assertEqual(price_usage(cached, {"m": {"input_per_million": 2, "output_per_million": 9}}), 4.0,
                         "cache tokens without a rate of their own are priced at the input rate")
        self.assertEqual(price_usage(cached, {"m": {"input_per_million": 2, "output_per_million": 9, "cache_read_per_million": 0.5}}), 2.5)


class HostObservedSpend(RepoCase):
    def adapter(self, name="model.py", usage=USAGE, candidates=(GOOD,)):
        script = self.root.parent / name
        script.write_text(textwrap.dedent(f'''\
            import json, sys
            json.load(sys.stdin)
            print(json.dumps({{"candidates": [{{"changes": c, "message": "m"}} for c in {json.dumps(list(candidates))}],
                              "recommended": [0], "usage": {json.dumps(usage)}}}))
            '''))
        return [sys.executable, str(script)]

    def priced(self, **extra):
        return self.kit(self.contract(**{"evolution.budget": budget(**extra)}))

    @staticmethod
    def search(thread):
        return [e["body"] for e in thread.history() if e["kind"] == "search_finished"][-1]

    def test_the_search_stops_after_the_candidate_that_crossed_the_dollar_budget(self):
        kit = self.priced(max_dollars=0.01)  # one call costs 0.0405
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        provider = CommandProvider(self.adapter(candidates=(GOOD, OTHER)), name="model", revision="1")
        found = thread.propose(provider)
        self.assertEqual([c.id for c in found], ["c1"], "the second candidate of the same response is refused")
        [candidate] = found
        self.assertEqual(candidate.signal["model_usage"]["output_tokens"], 300, "the provider's declaration stays in the signal")
        registered = next(e["body"] for e in thread.history() if e["kind"] == "candidate_registered")
        self.assertEqual(registered["spend"]["dollars"], price_usage(USAGE, PRICES))
        self.assertEqual(registered["spend"]["dollars"], COST)
        self.assertTrue(registered["spend"]["host_observed"])
        self.assertGreaterEqual(registered["spend"]["seconds"], 0)
        search = self.search(thread)
        self.assertEqual(search["stopped"], "budget_exhausted")
        self.assertEqual({k: search["spend"][k] for k in ("dollars", "evaluations", "candidates", "host_observed", "unknown_models")},
                         {"dollars": COST, "evaluations": 0, "candidates": 1, "host_observed": True, "unknown_models": []})
        self.assertGreaterEqual(search["spend"]["seconds"], registered["spend"]["seconds"])
        verdict = thread.check("c1")
        self.assertEqual((verdict.acceptable, verdict.reason, verdict.rule), (False, "search_over_budget:dollars", "budget.search"))
        self.assertIn("by the host's own measurement", verdict.hint)
        # Nothing more is bought: the next search is refused before its command runs.
        again = thread.propose(CommandProvider(self.adapter("again.py", candidates=(OTHER,)), name="model", revision="1"))
        self.assertEqual(again, [])
        search = self.search(thread)
        self.assertEqual((search["stopped"], search["candidates"], search["spend"]["dollars"], search["spend"]["host_observed"]),
                         ("budget_exhausted", [], 0.0, False))
        status = thread.status()
        self.assertEqual(status["budget"]["spend"]["dollars"], COST)
        self.assertEqual((status["budget"]["spend"]["max_dollars"], status["budget"]["spend"]["searches"]), (0.01, 2))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_status(status)
        self.assertIn("spend     $0.0405 / $0.0100", out.getvalue())
        self.assertEqual(kit.export(thread.id)["status"]["budget"]["spend"], status["budget"]["spend"])

    def test_a_declared_model_usage_is_not_priced(self):
        kit = self.priced(max_dollars=0.001)
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])

        def declares(space):
            return [space.submit(GOOD, operator="patch", signal={"model_usage": USAGE}).id]

        [candidate] = thread.propose(FunctionProvider(declares, name="inprocess", revision="1"))
        self.assertEqual(candidate.signal["model_usage"], USAGE)
        registered = next(e["body"] for e in thread.history() if e["kind"] == "candidate_registered")
        self.assertEqual((registered["spend"]["dollars"], registered["spend"]["host_observed"]), (0.0, False))
        search = self.search(thread)
        self.assertEqual((search["stopped"], search["spend"]["dollars"], search["spend"]["host_observed"]), ("completed", 0.0, False))
        self.assertTrue(thread.check("c1").acceptable, "nothing the host observed was spent")

    def test_promotion_is_refused_when_the_thread_is_over_budget(self):
        kit = self.priced(max_dollars=0.05)  # one call fits, two do not
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        [first] = thread.propose(CommandProvider(self.adapter("one.py", candidates=(GOOD,)), name="model", revision="1"))
        self.assertEqual(self.search(thread)["stopped"], "completed")
        [second] = thread.propose(CommandProvider(self.adapter("two.py", candidates=(OTHER,)), name="model", revision="1"))
        self.assertEqual(self.search(thread)["stopped"], "budget_exhausted")
        self.assertEqual(spend_total(thread.history())["dollars"], round(2 * COST, 6))
        for cid in (first.id, second.id):
            verdict = thread.check(cid)
            self.assertEqual([(c.name, c.state) for c in verdict.checks], [("tests", "passed")])
            self.assertEqual((verdict.acceptable, verdict.reason, verdict.rule), (False, "over_budget:dollars", "budget.promotion"))
            self.assertIn("max_dollars", verdict.hint)
        receipt = thread.accept(first.id)
        self.assertEqual((receipt.status, receipt.reason), ("denied", "over_budget:dollars"))
        self.assertEqual(thread.status()["candidates"][0]["promotion"], "denied")

    def test_strict_refuses_an_unpriced_model_and_a_lenient_contract_records_it(self):
        strict = self.contract(**{"evolution.budget": budget(max_dollars=1, strict=True)})
        lenient = self.contract(**{"evolution.budget": budget(max_dollars=1), "version": 2})
        kit = self.kit(strict)
        kit.close()
        kit = self.kit(lenient)
        argv = self.adapter(usage={**USAGE, "model": "mystery"})
        for name, acceptable, reason in (("repo-change.v1", False, "unknown_spend:mystery"), ("repo-change.v2", True, "all_checks_passed")):
            with self.subTest(contract=name):
                thread = kit.start("Add a CSV export", contract=name, paths=["src/", "tests/"])
                [candidate] = thread.propose(CommandProvider(argv, name="model", revision="1"))
                registered = next(e["body"] for e in thread.history() if e["kind"] == "candidate_registered")
                self.assertEqual((registered["spend"]["dollars"], registered["spend"]["host_observed"]), (None, True))
                search = self.search(thread)
                self.assertEqual((search["stopped"], search["spend"]["dollars"], search["spend"]["unknown_models"]),
                                 ("completed", 0.0, ["mystery"]))
                verdict = thread.check(candidate.id)
                self.assertEqual((verdict.acceptable, verdict.reason), (acceptable, reason))
                self.assertEqual(thread.status()["budget"]["spend"]["unknown_models"], ["mystery"])

    def test_a_runaway_provider_is_refused_before_its_next_purchase(self):
        kit = self.priced(max_dollars=0.01)
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        argv = self.adapter()
        calls = []

        def runaway(space):
            for _ in range(3):
                calls.append(run_json(argv, {"n": len(calls)}, timeout=60))
            return []

        self.assertEqual(thread.propose(FunctionProvider(runaway, name="runaway", revision="1")), [])
        self.assertEqual(len(calls), 1, "the second command never started")
        search = self.search(thread)
        self.assertEqual((search["stopped"], search["candidates"], search["spend"]["dollars"], search["spend"]["host_observed"]),
                         ("budget_exhausted", [], COST, True))

    def test_without_prices_nothing_changes_even_when_usage_flows(self):
        kit = self.kit()
        thread = kit.start("Add a CSV export", paths=["src/", "tests/"])
        seen = []

        def looks(space):
            seen.append(space.remaining())
            return [space.submit(OTHER, operator="patch").id]

        thread.propose(FunctionProvider(looks, name="looker", revision="1"))
        self.assertIsInstance(seen[0], SpendBudget)
        self.assertIsNone(seen[0].max_dollars)
        [candidate] = thread.propose(CommandProvider(self.adapter(), name="model", revision="1"))
        self.assertEqual(candidate.signal["model_usage"]["model"], "claude-opus-5")
        history = thread.history()
        for body in (e["body"] for e in history if e["kind"] == "candidate_registered"):
            self.assertNotIn("spend", body)
        finished = [e["body"] for e in history if e["kind"] == "search_finished"][-1]
        self.assertEqual(set(finished), {"id", "stopped", "candidates", "recommended", "evaluations", "elapsed_ms", "error", "actor"})
        self.assertEqual(validate_events(history, "governor"), [])
        self.assertNotIn("spend", thread.status()["budget"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_status(thread.status())
        self.assertNotIn("spend", out.getvalue())
        self.assertTrue(thread.check("c2").acceptable)

    def test_a_spend_record_needs_a_priced_contract_and_exact_fields(self):
        priced = self.contract(**{"evolution.budget": budget(max_dollars=1)})
        plain = self.contract()
        base = {"id": "x1", "commit": "a" * 40, "tree": "b" * 40, "base": "c" * 40, "parents": [], "operator": "patch",
                "provider": {"name": "p", "revision": "1"}, "changed_paths": ["src/a.py"],
                "diff": {"digest": "0" * 64, "bytes": 1, "files": 1}}
        good = {"dollars": 0.5, "seconds": 1.25, "host_observed": True}
        self.assertEqual(candidate_record(priced, [], {**base, "spend": good}, "host")["spend"], good)
        self.assertNotIn("spend", candidate_record(priced, [], base, "host"), "a host that measured nothing records nothing")
        for name, spend, doc in (("missing field", {"dollars": 1, "seconds": 1}, priced),
                                 ("negative dollars", {**good, "dollars": -1}, priced),
                                 ("text seconds", {**good, "seconds": "1"}, priced),
                                 ("unpriced contract", good, plain)):
            with self.subTest(case=name), self.assertRaises(Rejected) as caught:
                candidate_record(doc, [], {**base, "spend": spend}, "host")
            self.assertEqual(caught.exception.code, "invalid_candidate")
        started = {"kind": "search_started", "body": {"id": "s1"}}
        finished = {"id": "s1", "stopped": "completed", "candidates": [], "recommended": [], "evaluations": 0, "elapsed_ms": 1,
                    "error": None}
        spend = {"dollars": 0.0, "seconds": 0.001, "evaluations": 0, "candidates": 0, "host_observed": False, "unknown_models": []}
        self.assertEqual(search_record(priced, [started], "finished", {**finished, "spend": spend}, "host")["spend"], spend)
        self.assertNotIn("spend", search_record(plain, [started], "finished", finished, "host"))
        for name, bad, doc in (("unpriced contract", spend, plain),
                               ("null dollars", {**spend, "dollars": None}, priced),
                               ("duplicate unknown models", {**spend, "unknown_models": ["m", "m"]}, priced),
                               ("extra field", {**spend, "calls": 1}, priced)):
            with self.subTest(case=name), self.assertRaises(Rejected):
                search_record(doc, [started], "finished", {**finished, "spend": bad}, "host")


if __name__ == "__main__":
    unittest.main()
