"""TypeSafe's Jev API as a branch policy: ``TypeSafeJevPolicy``, name ``jev_api``.

The local ``JevPolicy`` (``syberlabs.evolve``) builds the offered actions and the stops; Jev,
TypeSafe AI's decision API, chooses one offered action with a typed ``choice`` question; the
answer is validated before it can become an action, and anything else falls back to the
local policy's own choice. Jev never authorizes anything: it picks among actions code has
already built, it cannot stop, extend or widen a search, and the host judges every candidate.

What is sent (``POST https://api.typesafe.ai/v1/systemone``): the pinned model, a ``state``
of ids and numbers projected from ``Situation`` (slots, scores, the remaining budget, the
operator statistics, the local estimates of each offered action), and one question,
``next_action``, whose ``instructions`` and ``criteria`` are this module's constants and
whose option keys are action kinds. Never candidate ids, file contents, prompts, paths,
objective or check text. The key is read from ``TYPESAFE_API_KEY`` at call time, sent only
as the ``Authorization`` header, and never stored, journaled or put in an error.

What is checked before an answer is used: the HTTP status is 200; the body is one strict
JSON object (no duplicate keys, no NaN); ``model`` is exactly the pinned model; ``answers``
holds exactly ``next_action``, of type ``choice``; the choice is one of the options sent;
the probabilities cover exactly the options sent, each finite in [0, 1], summing to 1 within
``TOLERANCE``; the choice has the highest probability; ``confidence`` is finite in [0, 1];
``usage`` has non-negative integer token counts. The shape was taken from live calls of
2026-10-28 (``tests/fixtures/jev_api/``): probabilities come at two decimals, so their sum is
off 1 by up to 0.01 on three options; 0.02 covers four.

What happens otherwise: a timeout (``jev_timeout``), no key, a non-200 or a transport failure
(``jev_unavailable``), a body that fails a check (``jev_invalid``), or a choice outside the
options (``choice_outside_options``) is recorded as the decision's ``fallback`` and the local
policy's action is taken. Three failures in a row switch the policy to local for the rest of
the search (``local_only``); a failure is never retried. Every decision's trace entry carries
``source`` (``jev_api``, ``local`` or ``rule``), ``model``, ``answers_digest``,
``request_digest``, ``latency_ms``, ``fallback`` and ``local_action``, the action the local
policy would have taken, so the two can be compared decision by decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence

from syberlabs.canonical import canonical, digest
from syberlabs.economic import is_money
from syberlabs.errors import Rejected
from syberlabs.evolve import JevPolicy
from syberlabs.search import Action, Crossover, Evaluate, Mutate, OperatorStats, Situation, Stop

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
KEY_ENV = "TYPESAFE_API_KEY"
MODEL = "jev-1.13.0"  # the pin docs/AUTOSKILL.md names; jev-1.14.0 (2026-10-14) is jev-latest since then
PIN = re.compile(r"^jev-\d+\.\d+\.\d+$")  # an exact version; the aliases jev-latest and jev-preview are refused
TIMEOUT = 30.0  # seconds; the 2026-10-28 calls took 0.14 to 0.34 s for about 750 input tokens
MAX_TIMEOUT = 120.0  # docs/AUTOSKILL.md's adapter timeout, the most this policy accepts
TOLERANCE = 0.02  # |sum of probabilities - 1|; two-decimal probabilities on up to four options
MAX_FAILURES = 3  # failures in a row before the policy is local for the rest of the search
MAX_REQUEST = 65_536  # bytes; a request is about 2.5 KB
MAX_RESPONSE = 65_536  # bytes read from a response; one answered choice is under 1 KB
INPUT_DOLLARS_PER_TOKEN = 0.042 / 1_000_000  # docs.typesafe.ai/models, 2026-10-28: input only, output free
QUESTION = "next_action"
INSTRUCTIONS = ("Choose the next action of a bounded evolutionary code search from the numbers in the state. "
                "Pick the offered action most likely to produce a candidate that passes every check, "
                "per dollar of the remaining budget.")
CRITERIA = {
    "evaluate": "Evaluate the next seed candidate with the host's checks: no model call, one evaluation.",
    "crossover": "Merge the two population slots named in the state: no model call, one evaluation.",
    "mutate": "Ask the model for a child of the parent slot named in the state (none: the base): one model call, "
              "one evaluation.",
    "migrate": "Exchange individuals with another host: no model call.",
}
SOURCES = ("jev_api", "local", "rule")
REASONS = ("jev_timeout", "jev_unavailable", "jev_invalid", "choice_outside_options", "local_only")
# Why a call failed, as a closed word: never response text, a header or an exception message.
DETAILS = ("no_key", "timeout", "transport", "http_401", "http_403", "http_429", "http_529", "http_4xx", "http_5xx",
           "http_other", "too_large", "not_json", "not_object", "model", "answers", "type", "choice", "probabilities",
           "argmax", "confidence", "usage", "request_too_large", "switched")


class JevFailure(Exception):
    """A call that did not yield a usable answer. ``reason`` and ``detail`` are closed words; nothing else is kept."""

    def __init__(self, reason: str, detail: str, *, latency_ms: float | None = None, response_digest: str | None = None):
        assert reason in REASONS and detail in DETAILS, (reason, detail)
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail, self.latency_ms, self.response_digest = reason, detail, latency_ms, response_digest


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect is a non-200, never a second send of the key


def http_transport(body: bytes, timeout: float, *, opener=None) -> tuple[int, bytes]:
    """One POST to ``ENDPOINT``. Returns ``(status, body)``; raises ``JevFailure`` for no key, a timeout or transport.

    The key is read here, used for this request's header and dropped. No retry.
    """
    key = os.environ.get(KEY_ENV, "")
    if not key or any(c in key for c in "\r\n"):
        raise JevFailure("jev_unavailable", "no_key")
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"})
    del key
    opener = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(MAX_RESPONSE + 1)
        except Exception:  # noqa: BLE001 - an unreadable error body is still the status
            data = b""
        return exc.code, data
    except TimeoutError:
        raise JevFailure("jev_timeout", "timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise JevFailure("jev_timeout", "timeout") from None
        raise JevFailure("jev_unavailable", "transport") from None
    except OSError:
        raise JevFailure("jev_unavailable", "transport") from None


def _pairs(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("duplicate key")
        found[key] = value
    return found


def _nonfinite(_value):
    raise ValueError("non-finite number")


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _status_detail(status: int) -> str:
    if status in (401, 403, 429, 529):
        return f"http_{status}"
    if 400 <= status < 500:
        return "http_4xx"
    if 500 <= status < 600:
        return "http_5xx"
    return "http_other"


def validate(status: int, raw: bytes, options: Sequence[str], *, model: str = MODEL,
             tolerance: float = TOLERANCE) -> dict:
    """Check one ``systemone`` response for one ``choice`` question over ``options``. Returns the parsed answer.

    ``{"model", "choice", "probabilities", "confidence", "usage", "answers_digest"}``. Raises
    ``JevFailure`` with a closed detail; the response's text never reaches the exception.
    """
    if status != 200:
        raise JevFailure("jev_unavailable", _status_detail(status))
    if len(raw) > MAX_RESPONSE:
        raise JevFailure("jev_invalid", "too_large")
    try:
        found = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (UnicodeDecodeError, ValueError):
        raise JevFailure("jev_invalid", "not_json") from None
    if type(found) is not dict:
        raise JevFailure("jev_invalid", "not_object")
    if found.get("model") != model:
        raise JevFailure("jev_invalid", "model")
    answers = found.get("answers")
    if type(answers) is not dict or set(answers) != {QUESTION}:
        raise JevFailure("jev_invalid", "answers")
    answer = answers[QUESTION]
    if type(answer) is not dict or answer.get("type") != "choice":
        raise JevFailure("jev_invalid", "type")
    choice, probabilities, confidence = answer.get("choice"), answer.get("probabilities"), answer.get("confidence")
    if type(choice) is not str:
        raise JevFailure("jev_invalid", "choice")
    if choice not in options:
        raise JevFailure("choice_outside_options", "choice")
    if (type(probabilities) is not dict or set(probabilities) != set(options)
            or any(not _number(v) or not 0 <= v <= 1 for v in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > tolerance):
        raise JevFailure("jev_invalid", "probabilities")
    if probabilities[choice] < max(probabilities.values()):
        raise JevFailure("jev_invalid", "argmax")
    if not _number(confidence) or not 0 <= confidence <= 1:
        raise JevFailure("jev_invalid", "confidence")
    usage = found.get("usage")
    if (type(usage) is not dict
            or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens"))):
        raise JevFailure("jev_invalid", "usage")
    return {"model": found["model"], "choice": choice,
            "probabilities": {k: probabilities[k] for k in options}, "confidence": confidence,
            "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]},
            "answers_digest": digest(answers)}


def _stats(stats: OperatorStats) -> dict:
    return {"attempted": stats.attempted, "produced": stats.produced, "evaluated": stats.evaluated,
            "improved": stats.improved, "passed": stats.passed, "dollars": stats.dollars}


def _option(action: Action, estimate: Mapping, seeds_left: int) -> dict:
    """One offered action as numbers: which slots it uses, and the local policy's estimate of it."""
    found: dict = {"generation": action.generation}
    if isinstance(action, Mutate):
        found["parent_slot"] = action.slot  # None: the base
    elif isinstance(action, Crossover):
        found["slots"] = list(action.slots)
    elif isinstance(action, Evaluate):
        found["seeds_left"] = seeds_left
    for key in ("expected_value", "expected_dollars", "expected_evaluations", "price"):
        found[key] = estimate[key]
    return found


class TypeSafeJevPolicy(JevPolicy):
    """``JevPolicy``'s offered actions, chosen by TypeSafe's Jev API; ``JevPolicy`` decides whenever Jev cannot.

    Stops are the local policy's: a passing individual, no room, nothing offered, the floor and
    the generation bound end the search without a call, as they do for ``JevPolicy``. Otherwise
    the actions ``JevPolicy`` offers (less any that would cross the generation bound) are sent
    as a ``choice``; one offered action is taken without a call. ``transport(body, timeout)``
    returns ``(status, body)`` (``http_transport`` by default); tests pass a recorded one.
    """

    name, revision = "jev_api", "1"

    def __init__(self, *, generations: int, seeds: Sequence[str] = (), prices: Mapping[str, float] | None = None,
                 prior: tuple[float, float] = (1.0, 1.0), explore: float = 0.1, floor: float = 1.0,
                 stop_when_passing: bool = True, model: str = MODEL, timeout: float = TIMEOUT,
                 transport: Callable[[bytes, float], tuple[int, bytes]] | None = None,
                 max_failures: int = MAX_FAILURES, tolerance: float = TOLERANCE):
        super().__init__(generations=generations, seeds=seeds, prices=prices, prior=prior, explore=explore,
                         floor=floor, stop_when_passing=stop_when_passing)
        if (not isinstance(model, str) or not PIN.match(model) or not is_money(timeout) or not 0 < timeout <= MAX_TIMEOUT
                or type(max_failures) is not int or max_failures < 1 or not is_money(tolerance)
                or not 0 < tolerance <= 0.1 or (transport is not None and not callable(transport))):
            raise Rejected("invalid_provider", "jev_api: an exact model version (jev-X.Y.Z), 0 < timeout <= 120 s, "
                                               "max_failures >= 1, 0 < tolerance <= 0.1 and a callable transport")
        self.model, self.timeout, self.max_failures, self.tolerance = model, float(timeout), max_failures, float(tolerance)
        self.transport = transport or http_transport

    def reset(self) -> None:
        super().reset()
        self._failures, self._local_only = 0, False
        self.calls, self.input_tokens, self.jev_dollars = 0, 0, 0.0  # this search's Jev calls, read after it

    def state(self, s: Situation, offered: Sequence[tuple[Action, dict]]) -> dict:
        """The ``state`` Jev sees: ids and numbers only. Population members are slots, never candidate ids."""
        left = s.remaining
        return {
            "decision": s.decisions, "generation": self._generation,
            "generations_left": max(0, self.generations - self._generation), "capacity": s.capacity,
            "population": [{"slot": i, "score": list(p.score), "passed": bool(p.passed)} for i, p in enumerate(s.population)],
            "remaining": {"candidates": left.max_candidates, "evaluations": left.max_evaluations,
                          "seconds": left.max_seconds, "dollars": getattr(left, "max_dollars", None)},
            "history": {kind: _stats(s.history.get(kind, OperatorStats())) for kind in self.KINDS},
            "mutator_exhausted": bool(s.mutator_exhausted),
            "options": {action.kind: _option(action, estimate, len(self._seeds)) for action, estimate in offered},
        }

    def request(self, s: Situation, offered: Sequence[tuple[Action, dict]]) -> dict:
        kinds = [action.kind for action, _ in offered]
        return {"model": self.model, "state": self.state(s, offered),
                "questions": {QUESTION: {"type": "choice", "instructions": INSTRUCTIONS,
                                         "criteria": {kind: CRITERIA[kind] for kind in kinds}}}}

    def _ask(self, body: bytes, kinds: Sequence[str]) -> tuple[dict, float]:
        started = time.perf_counter()
        try:
            status, raw = self.transport(body, self.timeout)
        except JevFailure as failure:
            failure.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            raise
        except TimeoutError:
            raise JevFailure("jev_timeout", "timeout", latency_ms=round((time.perf_counter() - started) * 1000, 1)) from None
        except Exception:  # noqa: BLE001 - any transport failure falls back; its text is never kept
            raise JevFailure("jev_unavailable", "transport",
                             latency_ms=round((time.perf_counter() - started) * 1000, 1)) from None
        latency = round((time.perf_counter() - started) * 1000, 1)
        if not isinstance(status, int) or not isinstance(raw, (bytes, bytearray)):
            raise JevFailure("jev_unavailable", "transport", latency_ms=latency)
        raw = bytes(raw)
        try:
            return validate(status, raw, kinds, model=self.model, tolerance=self.tolerance), latency
        except JevFailure as failure:
            failure.latency_ms = latency
            failure.response_digest = hashlib.sha256(raw).hexdigest() if raw else None
            raise

    @staticmethod
    def _record(source: str, model: str, *, local_action: str | None, answers_digest: str | None = None,
                request_digest: str | None = None, latency_ms: float | None = None, fallback: dict | None = None,
                failures: int = 0) -> dict:
        return {"source": source, "model": model, "answers_digest": answers_digest, "request_digest": request_digest,
                "latency_ms": latency_ms, "fallback": fallback, "failures": failures, "local_action": local_action}

    def decide(self, s: Situation) -> Action:
        local, audit, options, _budget = self._plan(s)
        if isinstance(local, Stop):  # the local policy's rules end the search; Jev is not asked
            self._last_audit = {**audit, **self._record("rule", self.model, local_action="stop",
                                                        failures=self._failures)}
            return local
        g = self._generation
        offered = [(a, e) for a, e in options if not (a.generation > g and g >= self.generations)]
        if len(offered) == 1:
            self._take(local)
            self._last_audit = {**audit, **self._record("local", self.model, local_action=local.kind,
                                                        failures=self._failures)}
            return local
        if self._local_only:
            self._take(local)
            self._last_audit = {**audit, **self._record(
                "local", self.model, local_action=local.kind, failures=self._failures,
                fallback={"reason": "local_only", "detail": "switched"})}
            return local
        request = self.request(s, offered)
        body = canonical(request).encode("utf-8")
        request_digest = hashlib.sha256(body).hexdigest()
        kinds = [a.kind for a, _ in offered]
        try:
            if len(body) > MAX_REQUEST:
                raise JevFailure("jev_invalid", "request_too_large", latency_ms=0.0)
            self.calls += 1
            answer, latency = self._ask(body, kinds)
        except JevFailure as failure:
            self._failures += 1
            switched = self._failures >= self.max_failures
            self._local_only = self._local_only or switched
            self._take(local)
            fallback = {"reason": failure.reason, "detail": failure.detail}
            if failure.response_digest:
                fallback["response_digest"] = failure.response_digest
            if switched:
                fallback["switched_to_local"] = True
            self._last_audit = {**audit, **self._record(
                "local", self.model, local_action=local.kind, request_digest=request_digest,
                latency_ms=failure.latency_ms, fallback=fallback, failures=self._failures)}
            return local
        self._failures = 0
        self.input_tokens += answer["usage"]["input_tokens"]
        dollars = round(answer["usage"]["input_tokens"] * INPUT_DOLLARS_PER_TOKEN, 9)
        self.jev_dollars = round(self.jev_dollars + dollars, 9)
        action, estimate = next((a, e) for a, e in offered if a.kind == answer["choice"])
        self._take(action)
        budget = audit["budget"]
        self._last_audit = {
            **audit, "expected_value": estimate["expected_value"], "expected_dollars": estimate["expected_dollars"],
            "chosen_by": "jev_api", "price": {k: estimate[k] for k in ("price", "expected_evaluations")},
            "expected_passes": round(estimate["expected_value"] * budget["remaining"] / estimate["price"], 6),
            **self._record("jev_api", answer["model"], local_action=local.kind, answers_digest=answer["answers_digest"],
                           request_digest=request_digest, latency_ms=latency),
            "jev": {"choice": answer["choice"], "probabilities": answer["probabilities"],
                    "confidence": answer["confidence"], "usage": answer["usage"], "dollars": dollars},
        }
        return action
