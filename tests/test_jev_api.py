"""TypeSafeJevPolicy (``syberlabs.jev_api``): TypeSafe's Jev API behind the BranchPolicy seam, without a network.

Every response here was recorded from ``POST https://api.typesafe.ai/v1/systemone`` on
2026-10-28 (``tests/fixtures/jev_api/``, headers and request ids scrubbed) or is one of those
bodies with one field changed. No test opens a socket: the policy's transport is a recorded
replay, a stand-in that fails on purpose, or ``http_transport`` with a stand-in opener. What
is covered: the validation of an answer, each fallback and the switch to local after three
failures in a row, that the request carries ids and numbers and no free text, that the key
never reaches a record, a trace or an error, that the existing policies' traces are
byte-for-byte what they were before this module, and the ``evolve_jevapi`` benchmark arm.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import random
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path

from syberlabs import Rejected
from syberlabs.build import SpendBudget
from syberlabs.canonical import canonical, digest
from syberlabs.evolve import EvolutionaryProvider, JevPolicy
from syberlabs.jev_api import (CRITERIA, DETAILS, ENDPOINT, INSTRUCTIONS, KEY_ENV, MODEL, QUESTION, REASONS, SOURCES,
                               TOLERANCE, JevFailure, TypeSafeJevPolicy, http_transport, validate)
from syberlabs.search import BranchPolicy, Individual, OperatorStats, Situation
from tests.test_jev import GENERATIONS, POPULATION, PRICE, PROFILES, Profile, World, run

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jev_api"
RECORD_FIELDS = ("source", "model", "answers_digest", "request_digest", "latency_ms", "fallback", "failures",
                 "local_action")
KINDS3, KINDS4, KINDS2 = ("evaluate", "crossover", "mutate"), ("evaluate", "crossover", "mutate", "migrate"), ("crossover", "mutate")
CANARY_KEY = "ts-canary-7f3a91c2e5d84b06aa11"  # set as TYPESAFE_API_KEY in the tests that need a key; never a real one

# The traces of the existing policies on the in-memory pricing world (tests/test_jev.py), seeds 0 to 2, as
# computed on main at 0b03040 before this module existed: refactoring JevPolicy.decide changed none of them.
UNCHANGED = {
    ("mutation_rare", "fixed"): "0a39d5562a7f5963e9a7e7bfd3a94fed5beb1e3996d9714b5de74b00971cdeb0",
    ("mutation_rare", "empirical"): "626b5a92fd83c7aff5351c2a47230cfb0def9aaaa80c861dd4eb0f8a47b9eea9",
    ("mutation_rare", "jev"): "998507ece7d01e47e5bdf3dac2ae5ec0c1f77759d3c6a53bbb6937a47d5e0d78",
    ("mutation_common", "fixed"): "28e5966f06aac3b4ed1837597328fb12a7782916817985055620302e86e8cf56",
    ("mutation_common", "empirical"): "50cee6b5d74c4c7a718e033b94ed6817d5d42f22a7b30676eddfeedbf4dd1929",
    ("mutation_common", "jev"): "7d603eb2d10fe560e193f68c63f3ef44e3c779923a31d819c9ab739f07d30658",
}
UNCHANGED_UNPRICED_JEV = "efd800301bb6fa81debd2f9ced4cfd419b32f1c5a03aa3fcc18bdb2ec2ab012a"


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def response(name: str) -> dict:
    return recorded(name)["response"]


def raw(value) -> bytes:
    return json.dumps(value).encode("utf-8")


def changed(name: str, edit) -> bytes:
    """A recorded body with one change, made by ``edit(body)`` in place."""
    body = response(name)
    edit(body)
    return raw(body)


def answer(body: dict) -> dict:
    return body["answers"][QUESTION]


class Recorded:
    """Replays the recorded 200 responses, picked by the option set the request offers, in turn."""

    POOL = {frozenset(KINDS3): ["choice_three_options", "choice_repeat_a", "choice_repeat_b", "choice_low_confidence"],
            frozenset(KINDS4): ["choice_four_options"], frozenset(KINDS2): ["choice_two_options"]}

    def __init__(self):
        self.requests: list[dict] = []
        self.bodies: list[bytes] = []

    def __call__(self, body: bytes, timeout: float) -> tuple[int, bytes]:
        self.bodies.append(body)
        request = json.loads(body)
        self.requests.append(request)
        names = self.POOL[frozenset(request["questions"][QUESTION]["criteria"])]
        return 200, raw(response(names[(len(self.requests) - 1) % len(names)]))


class Failing:
    """A transport that fails the way it is told to, ``plan`` entries in turn (the last one repeats)."""

    def __init__(self, *plan):
        self.plan, self.calls = list(plan), 0

    def __call__(self, body: bytes, timeout: float):
        step = self.plan[min(self.calls, len(self.plan) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(body)
        return step


class KeyEnv:
    """``TYPESAFE_API_KEY`` set to ``value`` (or removed for None) inside the block, restored after it."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get(KEY_ENV)
        if self.value is None:
            os.environ.pop(KEY_ENV, None)
        else:
            os.environ[KEY_ENV] = self.value
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop(KEY_ENV, None)
        else:
            os.environ[KEY_ENV] = self.old
        return False


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status, self.body = status, body

    def read(self, n=-1):
        return self.body if n is None or n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Stands in for urllib's opener: records what would be sent, and answers or fails as told."""

    def __init__(self, mode: str, body: bytes = b"", key: str = CANARY_KEY):
        self.mode, self.body, self.key, self.seen = mode, body, key, []

    def open(self, request, timeout):
        self.seen.append({"url": request.full_url, "method": request.get_method(), "timeout": timeout,
                          "headers": dict(request.header_items()), "data": request.data})
        if self.mode == "ok":
            return FakeResponse(200, self.body)
        if self.mode == "http":
            raise urllib.error.HTTPError(ENDPOINT, 401, f"refused {self.key}", {},
                                         io.BytesIO(raw({"detail": {"message": f"bad key {self.key}"}})))
        if self.mode == "url":
            raise urllib.error.URLError(f"connection refused while sending {self.key}")
        raise TimeoutError(f"timed out sending {self.key}")


def situation(population=(("c1", (1, 1)), ("c2", (2, 2))), *, decisions=4, dollars=4.5) -> Situation:
    stats = {k: OperatorStats(dollars=0.0) for k in ("mutate", "crossover", "migrate", "evaluate")}
    return Situation(generation=0, population=tuple(Individual(i, score, False) for i, score in population), capacity=3,
                     remaining=SpendBudget(10, 20, 300, dollars), room=True, history=stats, mutator_exhausted=False,
                     exchange=False, decisions=decisions, rng=random.Random(0))


def search(transport, *, profile="mutation_rare", seed=0, policy=None, max_dollars=5.0):
    """One search on the in-memory pricing world of tests/test_jev.py, branching by TypeSafeJevPolicy."""
    world = World(seed=seed, max_dollars=max_dollars)
    policy = policy or TypeSafeJevPolicy(generations=GENERATIONS, prices={"mutate": PRICE}, transport=transport)
    provider = EvolutionaryProvider(Profile(price=PRICE, **PROFILES[profile]), population=POPULATION,
                                    generations=GENERATIONS, crossover_every=3, policy=policy)
    provider.search(world)
    return provider, world


def long_search(transport, *, seed=0):
    """A search that does not stop at the first pass, so it makes enough decisions to fail three times in a row."""
    return search(transport, seed=seed, policy=TypeSafeJevPolicy(generations=12, prices={"mutate": PRICE},
                                                                  transport=transport, stop_when_passing=False))


def decisions(trace):
    return [e for e in trace if "decision" in e]


class Validation(unittest.TestCase):
    """What an answer must be before it can become an action, on recorded bodies."""

    def test_every_recorded_answer_validates_against_its_own_options(self):
        for name, kinds in (("choice_three_options", KINDS3), ("choice_repeat_a", KINDS3), ("choice_repeat_b", KINDS3),
                            ("choice_low_confidence", KINDS3), ("choice_four_options", KINDS4),
                            ("choice_two_options", KINDS2)):
            found = validate(200, raw(response(name)), kinds)
            body = response(name)
            self.assertEqual(found["choice"], answer(body)["choice"], name)
            self.assertEqual(found["model"], MODEL)
            self.assertEqual(found["answers_digest"], digest(body["answers"]))
            self.assertRegex(found["answers_digest"], r"^[0-9a-f]{64}$")
            # The live format: two-decimal probabilities whose sum is off 1 by at most 0.01, within the tolerance.
            self.assertLessEqual(abs(sum(found["probabilities"].values()) - 1), 0.0100001, name)
            self.assertGreater(found["usage"]["input_tokens"], 0)

    def test_the_two_recorded_repeats_of_one_request_differ(self):
        # Same request body, same model, two answers: Jev's probabilities are not a pure function of the request.
        a, b = recorded("choice_repeat_a"), recorded("choice_repeat_b")
        self.assertEqual(a["request"], b["request"])
        self.assertNotEqual(answer(a["response"])["probabilities"], answer(b["response"])["probabilities"])

    def test_the_served_model_must_be_the_pinned_one(self):
        for name in ("choice_served_by_1_14", "choice_alias_latest"):  # jev-latest is served as jev-1.14.0
            self.assertEqual(response(name)["model"], "jev-1.14.0")
            with self.assertRaises(JevFailure) as caught:
                validate(200, raw(response(name)), KINDS3)
            self.assertEqual((caught.exception.reason, caught.exception.detail), ("jev_invalid", "model"))
        self.assertEqual(validate(200, raw(response("choice_served_by_1_14")), KINDS3, model="jev-1.14.0")["model"],
                         "jev-1.14.0")

    def test_the_pin_is_an_exact_version(self):
        for model in ("jev-latest", "jev-preview", "jev-1.13", "jev-1.13.0-rc", "", None):
            with self.assertRaises(Rejected, msg=model):
                TypeSafeJevPolicy(generations=5, model=model)
        for bad in (dict(timeout=0), dict(timeout=121), dict(max_failures=0), dict(tolerance=0), dict(transport=1)):
            with self.assertRaises(Rejected, msg=bad):
                TypeSafeJevPolicy(generations=5, **bad)
        policy = TypeSafeJevPolicy(generations=5, model="jev-1.14.0")
        self.assertIsInstance(policy, BranchPolicy)
        self.assertEqual((policy.name, policy.revision, TypeSafeJevPolicy(generations=1).model), ("jev_api", "1", MODEL))
        self.assertIs(TypeSafeJevPolicy(generations=1).transport, http_transport)

    def test_each_broken_answer_is_refused_with_its_own_detail(self):
        base = "choice_three_options"
        cases = {
            "choice": [lambda b: answer(b).update(choice=None)],
            "probabilities": [lambda b: answer(b)["probabilities"].pop("crossover"),
                              lambda b: answer(b)["probabilities"].update(migrate=0.0),
                              lambda b: answer(b)["probabilities"].update(mutate=1.5),
                              lambda b: answer(b)["probabilities"].update(crossover=-0.01),
                              lambda b: answer(b)["probabilities"].update(crossover=True),
                              lambda b: answer(b)["probabilities"].update(crossover="0.03"),
                              lambda b: answer(b)["probabilities"].update(mutate=0.48),  # sums to 0.90
                              lambda b: answer(b).update(probabilities=[0.39, 0.03, 0.58])],
            "argmax": [lambda b: answer(b).update(choice="evaluate")],
            "confidence": [lambda b: answer(b).pop("confidence"), lambda b: answer(b).update(confidence=1.2),
                           lambda b: answer(b).update(confidence="high")],
            "usage": [lambda b: b.pop("usage"), lambda b: b["usage"].update(input_tokens=-1),
                      lambda b: b["usage"].update(output_tokens=1.5)],
            "type": [lambda b: answer(b).update(type="noul"), lambda b: b["answers"].update({QUESTION: [1]})],
            "answers": [lambda b: b.pop("answers"), lambda b: b["answers"].update(extra={"type": "noul", "noul": 0.5}),
                        lambda b: b.update(answers={"other": answer(b)})],
            "model": [lambda b: b.update(model="jev-latest"), lambda b: b.pop("model")],
        }
        for detail, edits in cases.items():
            for i, edit in enumerate(edits):
                with self.subTest(detail=detail, case=i):
                    with self.assertRaises(JevFailure) as caught:
                        validate(200, changed(base, edit), KINDS3)
                    self.assertEqual((caught.exception.reason, caught.exception.detail), ("jev_invalid", detail))
        with self.assertRaises(JevFailure) as caught:
            validate(200, changed(base, lambda b: answer(b).update(choice="migrate")), KINDS3)
        self.assertEqual(caught.exception.reason, "choice_outside_options")
        # A noul answered beside the choice is an answer to a question this policy did not ask.
        with self.assertRaises(JevFailure) as caught:
            validate(200, raw(response("choice_with_noul")), KINDS3)
        self.assertEqual(caught.exception.detail, "answers")

    def test_the_tolerance_is_stated_and_holds_at_its_edge(self):
        self.assertEqual(TOLERANCE, 0.02)
        inside = changed("choice_three_options", lambda b: answer(b)["probabilities"].update(mutate=0.565))  # 0.985
        self.assertEqual(validate(200, inside, KINDS3)["choice"], "mutate")
        outside = changed("choice_three_options", lambda b: answer(b)["probabilities"].update(mutate=0.555))  # 0.975
        with self.assertRaises(JevFailure):
            validate(200, outside, KINDS3)

    def test_a_body_that_is_not_one_strict_json_object_is_refused(self):
        text = raw(response("choice_three_options")).decode()
        for body, detail in ((text.replace("0.58", "NaN").encode(), "not_json"),
                             (text.replace("0.58", "Infinity").encode(), "not_json"),
                             (text.replace('{"model"', '{"model": "jev-1.13.0", "model"', 1).encode(), "not_json"),
                             (b"\xff\xfe{}", "not_json"), (b"[]", "not_object"), (b"", "not_json"),
                             (b" " * 70_000, "too_large")):
            with self.subTest(detail=detail, head=body[:30]):
                with self.assertRaises(JevFailure) as caught:
                    validate(200, body, KINDS3)
                self.assertEqual((caught.exception.reason, caught.exception.detail), ("jev_invalid", detail))

    def test_a_non_200_is_unavailable_and_names_only_its_status_class(self):
        for name, status, detail in (("error_401_bad_key", 401, "http_401"), ("error_403_no_key", 403, "http_403"),
                                     ("error_400_unknown_model", 400, "http_4xx"), ("error_400_short_pin", 400, "http_4xx"),
                                     ("error_400_nonfinite", 400, "http_4xx"), ("error_422_missing_criteria", 422, "http_4xx")):
            fixture = recorded(name)
            self.assertEqual(fixture["status"], status, name)
            with self.assertRaises(JevFailure) as caught:
                validate(fixture["status"], raw(fixture["response"]), KINDS3)
            self.assertEqual((caught.exception.reason, caught.exception.detail), ("jev_unavailable", detail))
            self.assertNotIn(json.dumps(fixture["response"])[:40], str(caught.exception))
        for status, detail in ((429, "http_429"), (529, "http_529"), (500, "http_5xx"), (302, "http_other")):
            with self.assertRaises(JevFailure) as caught:
                validate(status, b"{}", KINDS3)
            self.assertEqual(caught.exception.detail, detail)
        self.assertTrue(set(REASONS) >= {"jev_timeout", "jev_unavailable", "jev_invalid", "choice_outside_options"})
        self.assertIn("http_429", DETAILS)


class Fallbacks(unittest.TestCase):
    """A timeout, an invalid answer or an unavailable API: the local JevPolicy decides, and the record says why."""

    def first_asked(self, transport):
        provider, _world = search(transport)
        asked = [e for e in decisions(provider.trace) if e.get("request_digest")]
        self.assertTrue(asked, "the search asked Jev at least once")
        return asked[0], provider

    def test_each_failure_falls_back_to_the_local_action_with_its_reason(self):
        invalid_model = (200, raw(response("choice_served_by_1_14")))
        outside = (200, changed("choice_two_options", lambda b: answer(b).update(choice="evaluate")))
        cases = {
            ("jev_timeout", "timeout"): TimeoutError("read timed out"),
            ("jev_unavailable", "transport"): OSError("connection reset"),
            ("jev_unavailable", "http_401"): (401, raw(response("error_401_bad_key"))),
            ("jev_unavailable", "http_429"): (429, b'{"detail": "slow down"}'),
            ("jev_invalid", "model"): invalid_model,
            ("jev_invalid", "not_json"): (200, b"<html>"),
            ("choice_outside_options", "choice"): outside,
        }
        for (reason, detail), step in cases.items():
            with self.subTest(reason=reason, detail=detail):
                entry, provider = self.first_asked(Failing(step))
                self.assertEqual(entry["source"], "local")
                self.assertEqual((entry["fallback"]["reason"], entry["fallback"]["detail"]), (reason, detail))
                self.assertEqual(entry["action"], entry["local_action"])
                self.assertEqual(entry["failures"], 1)
                self.assertIsNone(entry["answers_digest"])
                self.assertIsNotNone(entry["latency_ms"])
                self.assertEqual(provider.trace[-1]["action"], "stop")

    def test_no_key_falls_back_without_opening_a_socket(self):
        opener = Opener("ok", raw(response("choice_two_options")))
        with KeyEnv(None):
            entry, _ = self.first_asked(lambda body, timeout: http_transport(body, timeout, opener=opener))
        self.assertEqual((entry["fallback"]["reason"], entry["fallback"]["detail"]), ("jev_unavailable", "no_key"))
        self.assertEqual(opener.seen, [], "nothing was sent")

    @staticmethod
    def decide_in_turn(transport, n: int) -> tuple[TypeSafeJevPolicy, list[dict]]:
        """``n`` decisions, each over a fresh pair below capacity, so each offers a crossover and a mutation."""
        policy = TypeSafeJevPolicy(generations=60, transport=transport)
        policy.reset()
        entries = []
        for i in range(n):
            action = policy.decide(situation(((f"c{2 * i + 1}", (1, 1)), (f"c{2 * i + 2}", (2, 2))), decisions=i))
            entries.append({**policy.audit(), "action": action.kind})
        return policy, entries

    def test_three_failures_in_a_row_switch_to_local_for_the_rest_of_the_search(self):
        transport = Failing(TimeoutError("slow"))
        policy, entries = self.decide_in_turn(transport, 6)
        self.assertEqual(transport.calls, 3)
        self.assertEqual(policy.calls, 3)
        self.assertEqual([e["failures"] for e in entries[:3]], [1, 2, 3])
        self.assertEqual([e["fallback"]["reason"] for e in entries[:3]], ["jev_timeout"] * 3)
        self.assertNotIn("switched_to_local", entries[1]["fallback"])
        self.assertTrue(entries[2]["fallback"]["switched_to_local"])
        for entry in entries[3:]:
            self.assertEqual(entry["source"], "local")
            self.assertEqual(entry["fallback"], {"reason": "local_only", "detail": "switched"})
            self.assertIsNone(entry["request_digest"])
            self.assertEqual(len(entry["alternatives"]), 2, "a choice was offered and Jev was not asked")
            self.assertEqual(entry["action"], entry["local_action"])
        # The next search starts with Jev again: the switch lasts one search.
        policy.reset()
        self.assertEqual((policy._failures, policy._local_only, policy.calls), (0, False, 0))
        # The same in a whole search: three calls, then none.
        transport = Failing(TimeoutError("slow"))
        provider, _ = long_search(transport)
        asked = [e for e in decisions(provider.trace) if e.get("request_digest")]
        self.assertEqual((transport.calls, len(asked), asked[-1]["fallback"].get("switched_to_local")), (3, 3, True))

    def test_a_success_resets_the_count_of_failures_in_a_row(self):
        ok = (200, raw(response("choice_two_options")))
        transport = Failing(TimeoutError("a"), TimeoutError("b"), ok, TimeoutError("c"), TimeoutError("d"), ok)
        policy, entries = self.decide_in_turn(transport, 6)
        self.assertEqual(transport.calls, 6)
        self.assertEqual([e["failures"] for e in entries], [1, 2, 0, 1, 2, 0])
        self.assertEqual([e["source"] for e in entries], ["local", "local", "jev_api", "local", "local", "jev_api"])
        self.assertFalse(any((e.get("fallback") or {}).get("switched_to_local") for e in entries))
        self.assertEqual([e["action"] for e in entries if e["source"] == "jev_api"], ["mutate", "mutate"])

    def test_with_every_call_failing_the_search_is_the_local_policys_search(self):
        record = set(RECORD_FIELDS)
        strip = lambda e: {k: v for k, v in e.items() if k not in record and k != "policy"}  # noqa: E731
        for seed in (0, 1, 2):
            failed, failed_world = search(Failing(OSError("down")), seed=seed)
            local = EvolutionaryProvider(Profile(price=PRICE, **PROFILES["mutation_rare"]), population=POPULATION,
                                         generations=GENERATIONS, crossover_every=3,
                                         policy=JevPolicy(generations=GENERATIONS, prices={"mutate": PRICE}))
            local_world = World(seed=seed, max_dollars=5.0)
            local.search(local_world)
            self.assertEqual([strip(e) for e in failed.trace], [strip(e) for e in local.trace], seed)
            self.assertEqual(failed_world.dollars, local_world.dollars)


class RecordedSearch(unittest.TestCase):
    """Whole searches with recorded answers: every decision recorded, Jev only ever choosing an offered action."""

    @classmethod
    def setUpClass(cls):
        cls.runs = []
        for seed in (0, 1, 2):
            transport = Recorded()
            provider, world = search(transport, seed=seed)
            cls.runs.append((transport, provider, world))

    def test_every_decision_carries_the_record(self):
        for _transport, provider, _world in self.runs:
            for entry in decisions(provider.trace):
                self.assertTrue(all(field in entry for field in RECORD_FIELDS), entry)
                self.assertIn(entry["source"], SOURCES)
                self.assertEqual(entry["policy"], {"name": "jev_api", "revision": "1"})
                self.assertEqual(entry["model"], MODEL)
                if entry["source"] == "jev_api":
                    self.assertRegex(entry["answers_digest"], r"^[0-9a-f]{64}$")
                    self.assertRegex(entry["request_digest"], r"^[0-9a-f]{64}$")
                    self.assertEqual(entry["action"], entry["jev"]["choice"])
                    self.assertEqual(entry["chosen_by"], "jev_api")
                    self.assertIn(entry["action"], {a["action"] for a in entry["alternatives"]})
                    self.assertIsNone(entry["fallback"])
                    self.assertEqual(set(entry["jev"]["probabilities"]), {a["action"] for a in entry["alternatives"]})
                elif entry["source"] == "rule":
                    self.assertEqual(entry["action"], "stop")
                    self.assertTrue(entry["chosen_by"].startswith("rule:"))
                    self.assertIsNone(entry["request_digest"])
                else:
                    self.assertIsNone(entry["request_digest"], "local without a call: only one action was offered")
            self.assertEqual(decisions(provider.trace)[-1]["source"], "rule", "only the local rules stop a search")

    def test_jev_was_asked_and_its_digests_match_what_was_sent_and_returned(self):
        for transport, provider, _world in self.runs:
            asked = [e for e in decisions(provider.trace) if e["source"] == "jev_api"]
            self.assertEqual(len(asked), len(transport.requests))
            self.assertEqual(provider.policy.calls, len(transport.requests))
            self.assertGreater(len(asked), 0)
            for entry, body in zip(asked, transport.bodies):
                self.assertEqual(entry["request_digest"], hashlib.sha256(body).hexdigest())
            self.assertGreater(provider.policy.input_tokens, 0)
            self.assertAlmostEqual(provider.policy.jev_dollars, provider.policy.input_tokens * 0.042e-6, places=9)

    def test_the_signal_stays_within_the_bound_and_names_the_policy(self):
        for _transport, _provider, world in self.runs:
            self.assertTrue(all(n < 4096 for n in world.signal_bytes))
            self.assertTrue(all(c.signal["policy"]["name"] == "jev_api" for c in world.views.values()))


ALLOWED_KEYS = {"decision", "generation", "generations_left", "capacity", "population", "slot", "score", "passed",
                "remaining", "candidates", "evaluations", "seconds", "dollars", "history", "attempted", "produced",
                "evaluated", "improved", "mutator_exhausted", "options", "parent_slot", "slots", "seeds_left",
                "expected_value", "expected_dollars", "expected_evaluations", "price", *JevPolicy.KINDS}


class Request(unittest.TestCase):
    """What leaves the process: ids and numbers in the state, and only this module's fixed text in the question."""

    def capture(self, s, **options):
        transport = Recorded()
        policy = TypeSafeJevPolicy(generations=10, transport=transport, **options)
        policy.reset()
        action = policy.decide(s)
        return transport, policy, action

    def walk(self, value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertIn(key, ALLOWED_KEYS, f"{path}: key {key!r}")
                self.walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self.walk(item, f"{path}[{i}]")
        else:
            self.assertTrue(value is None or type(value) in (int, float, bool), f"{path}: {value!r} is not a number")

    def test_the_state_holds_no_text_and_the_question_only_fixed_text(self):
        canary = "CANARY src/secret_path.py objective prompt"
        s = situation(((canary, (1, 1)), ("c2-" + canary, (2, 2))))
        transport, _policy, action = self.capture(s, seeds=["seed-" + canary])
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(set(request), {"model", "state", "questions"})
        self.assertEqual(request["model"], MODEL)
        self.walk(request["state"])
        self.assertEqual(set(request["questions"]), {QUESTION})
        question = request["questions"][QUESTION]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(question["instructions"], INSTRUCTIONS)
        self.assertEqual(question["criteria"], {k: CRITERIA[k] for k in KINDS3})
        self.assertNotIn("CANARY", transport.bodies[0].decode())
        self.assertNotIn("c2", transport.bodies[0].decode(), "population members are slots, not candidate ids")
        self.assertIn(action.kind, KINDS3)

    def test_a_single_offered_action_is_taken_without_a_call(self):
        s = situation((("c1", (1, 1)),))  # one individual: no pair; below capacity: mutate base only
        transport, policy, action = self.capture(s)
        self.assertEqual(transport.requests, [])
        self.assertEqual((action.kind, action.parent), ("mutate", "base"))
        self.assertEqual(policy.audit()["source"], "local")

    def test_a_stop_is_the_local_rule_and_asks_nothing(self):
        s = dataclasses.replace(situation((("c1", (3, 3)),)), population=(Individual("c1", (3, 3), True),))
        transport, policy, action = self.capture(s)
        self.assertEqual((action.kind, action.reason), ("stop", "passing"))
        self.assertEqual(transport.requests, [])
        self.assertEqual(policy.audit()["source"], "rule")


class Key(unittest.TestCase):
    """The key goes to TypeSafe in one header and nowhere else: not a record, a trace, an error or the policy."""

    def run_with(self, opener):
        with KeyEnv(CANARY_KEY):
            provider, world = long_search(lambda body, timeout: http_transport(body, timeout, opener=opener))
        return provider, world

    def assert_absent(self, provider, world):
        self.assertNotIn(CANARY_KEY, json.dumps(provider.trace, default=str))
        self.assertNotIn(CANARY_KEY, repr(vars(provider.policy)))
        self.assertNotIn(CANARY_KEY, json.dumps([c.signal for c in world.views.values()], default=str))

    def test_the_key_is_sent_as_the_bearer_header_only(self):
        opener = Opener("ok", raw(response("choice_two_options")))
        provider, world = self.run_with(opener)
        self.assertTrue(opener.seen)
        for sent in opener.seen:
            self.assertEqual((sent["url"], sent["method"]), (ENDPOINT, "POST"))
            self.assertEqual(sent["headers"]["Authorization"], "Bearer " + CANARY_KEY)
            self.assertNotIn(CANARY_KEY, sent["data"].decode())
            self.assertEqual(sent["timeout"], 30.0)
        self.assertTrue(any(e["source"] == "jev_api" for e in decisions(provider.trace)))
        self.assert_absent(provider, world)

    def test_the_key_in_a_failure_never_reaches_the_record_or_the_error(self):
        for mode, detail in (("http", "http_401"), ("url", "transport"), ("timeout", "timeout")):
            with self.subTest(mode=mode):
                opener = Opener(mode)
                provider, world = self.run_with(opener)
                asked = [e for e in decisions(provider.trace) if e.get("request_digest")]
                self.assertEqual([e["fallback"]["detail"] for e in asked], [detail] * 3)
                self.assert_absent(provider, world)
                with KeyEnv(CANARY_KEY):
                    try:
                        status, body = http_transport(b"{}", 1.0, opener=opener)
                    except JevFailure as failure:
                        self.assertNotIn(CANARY_KEY, str(failure))
                        self.assertNotIn(CANARY_KEY, repr(vars(failure)))
                    else:
                        with self.assertRaises(JevFailure) as caught:
                            validate(status, body, KINDS3)
                        self.assertNotIn(CANARY_KEY, str(caught.exception))
                        self.assertNotIn(CANARY_KEY, repr(vars(caught.exception)))

    def test_a_key_with_a_line_break_is_not_sent(self):
        opener = Opener("ok", raw(response("choice_two_options")))
        with KeyEnv(CANARY_KEY + "\r\nX-Other: 1"):
            with self.assertRaises(JevFailure) as caught:
                http_transport(b"{}", 1.0, opener=opener)
        self.assertEqual(caught.exception.detail, "no_key")
        self.assertEqual(opener.seen, [])

    def test_the_fixtures_hold_no_key_request_id_or_cookie(self):
        for path in sorted(FIXTURES.glob("*.json")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"Bearer|req_[0-9a-f]{8}|__cf_bm|ts-[A-Za-z0-9]{20}", text), path.name)


class Unchanged(unittest.TestCase):
    """The existing policies, after JevPolicy.decide was split into _plan and _take, give the same traces as before."""

    def test_the_traces_of_fixed_empirical_and_jev_are_byte_identical(self):
        for (profile, policy), expected in UNCHANGED.items():
            with self.subTest(profile=profile, policy=policy):
                self.assertEqual(digest([[run(policy, profile, s)["trace"] for s in (0, 1, 2)]]), expected)
        self.assertEqual(digest([run("jev", "mutation_rare", s, max_dollars=None)["trace"] for s in (0, 1, 2)]),
                         UNCHANGED_UNPRICED_JEV)
        self.assertEqual((JevPolicy.name, JevPolicy.revision), ("jev", "1"))

    def test_jevs_plan_takes_nothing(self):
        policy = JevPolicy(generations=10)
        policy.reset()
        policy._crossed.add(frozenset(("c7", "c8")))
        before = canonical({"g": policy._generation, "seeds": policy._seeds, "crossed": sorted(map(sorted, policy._crossed)),
                            "mutated": policy._mutated})
        empty = dataclasses.replace(situation(()), mutator_exhausted=True)  # nothing offered: the "empty" stop
        passing = dataclasses.replace(situation(), population=(Individual("c1", (3, 3), True),))
        broke = dataclasses.replace(situation(), room=False)
        for s in (situation(), empty, passing, broke, situation(), empty):
            policy._plan(s)
        self.assertEqual([policy._plan(s)[0].reason for s in (empty, passing, broke)], ["empty", "passing", "budget"])
        after = canonical({"g": policy._generation, "seeds": policy._seeds, "crossed": sorted(map(sorted, policy._crossed)),
                           "mutated": policy._mutated})
        self.assertEqual(before, after)


class Arm(unittest.TestCase):
    """The ``evolve_jevapi`` arm of benchmarks/model_vs_patch.py, with the simulated model and recorded Jev answers."""

    def test_the_arm_is_named_only_on_request_and_records_jev(self):
        from benchmarks import model_vs_patch as harness
        self.assertIn("evolve_jevapi", harness.ALL_ARMS)
        self.assertNotIn("evolve_jevapi", harness.ARMS, "a default run makes no TypeSafe call")
        self.assertEqual(harness.EVOLVE_ARMS["evolve_jevapi"], "jev_api")
        with tempfile.TemporaryDirectory() as folder:
            journals = Path(folder) / "rows-journals"
            row = harness.run("pricing", "evolve_jevapi", harness.SIMULATED, 3, 1, Path(folder) / "runs", journals,
                              jev_transport=Recorded())
            self.assertEqual(row["policy"], {"name": "jev_api", "revision": "1"})
            jev = row["jev_api"]
            self.assertEqual(jev["model"], MODEL)
            self.assertEqual(sum(jev["by_source"].values()), len(json.loads(
                (journals / "pricing-evolve_jevapi-1.decisions.json").read_text(encoding="utf-8"))))
            self.assertEqual((jev["calls"], jev["fallbacks"], jev["switched_to_local"]), (jev["asked"], {}, False))
            self.assertGreater(jev["calls"], 0)
            self.assertGreater(jev["input_tokens"], 0)
            self.assertEqual(jev["decisions"], "rows-journals/pricing-evolve_jevapi-1.decisions.json")
            self.assertTrue((journals / "pricing-evolve_jevapi-1.json").is_file())
            report = harness.summarize([row], 3)
            self.assertIn("evolve_jevapi", report)
            self.assertIn("jev model", report)


if __name__ == "__main__":
    unittest.main()
