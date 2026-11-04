"""FeaturePolicy: the five typed questions a loop over a feature map asks, with closed answer sets.

A policy decides nothing. It proposes: ``decide(state, question)`` returns an ``Answer`` whose value is one of
the options code built before the question was asked, and the loop records the answer on the factory ledger
as a proposal before it acts on it. An answer outside the options is refused (``policy_invalid``) and never
acted on. The state a policy reads is ids and numbers (owner decision 1): feature ids, graph positions, risk
scores, attempts, dollars and calls. No titles, criteria, diffs, check output or paths.

The questions (autonomous design, section 4.2), each with the option set code builds:

- ``next_feature`` (choice): the eligible features (dependencies satisfied, no conflicting scope running, act
  class allowed, room in the envelope, attempts left). With no eligible feature the loop stops; no question.
- ``proposer_arm`` (choice): the envelope's arms; after ``retry_escalate_arm`` only the arms ranked above the one
  that failed, so the cheapest answer is then the cheapest stronger arm.
- ``budget_tier`` (choice): the tiers whose ceiling fits the remaining dollars (``S``, ``M``, ``L``).
- ``after_failure`` (choice): ``retry_same``, ``retry_escalate_arm``, ``escalate_owner``, ``park``; an option is
  removed when the caps forbid it (no attempts left removes the retries).
- ``worth_continuing`` (choice over ``continue`` and ``stop``): may only stop early, never extend a cap.

``OfflinePolicy`` is the default (owner decision 7): critical path first (highest bottom level, then higher
risk, then map order) for ``next_feature``, the cheapest arm and tier that fit, retry then escalate the arm
then park after a failure, stop when the envelope has no room. ``ShadowPolicy`` wraps an acting policy and a
second one that only answers: what it would have said is recorded beside the acting answer and never acted
on (Jev stays in shadow until E7). The shadow is an injected callable; nothing here imports or calls a
network client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from syberlabs.errors import Rejected

QUESTIONS = ("next_feature", "proposer_arm", "budget_tier", "after_failure", "worth_continuing")
TIERS = ("S", "M", "L")
AFTER_FAILURE = ("retry_same", "retry_escalate_arm", "escalate_owner", "park")
WORTH = ("continue", "stop")
SOURCES = ("offline", "shadow", "rule")


@dataclass(frozen=True)
class Question:
    key: str
    feature: str | None
    options: tuple[str, ...]
    numbers: dict = field(default_factory=dict)  # ids and numbers only; what a remote policy would be sent

    def __post_init__(self):
        if self.key not in QUESTIONS:
            raise Rejected("policy_question", f"unknown question {self.key!r}; one of {QUESTIONS}")
        if not self.options or len(set(self.options)) != len(self.options):
            raise Rejected("policy_question", f"{self.key}: options must be a non-empty list without repeats")
        closed = {"budget_tier": TIERS, "after_failure": AFTER_FAILURE, "worth_continuing": WORTH}.get(self.key)
        if closed is not None and any(o not in closed for o in self.options):
            raise Rejected("policy_question", f"{self.key}: options must come from {closed}")


@dataclass
class Answer:
    key: str
    value: str
    source: str
    reason: str
    probabilities: dict | None = None
    shadow: dict | None = None

    def record(self) -> dict:
        return {"key": self.key, "value": self.value, "source": self.source, "reason": self.reason,
                "probabilities": self.probabilities, "shadow": self.shadow}


class FeaturePolicy(Protocol):
    name: str

    def decide(self, state: dict, question: Question) -> Answer: ...


def validate(question: Question, answer: Any, source: str) -> Answer:
    """An answer is an ``Answer`` or a plain value; either way its value must be one of the options sent."""
    if isinstance(answer, Answer):
        value, reason, probabilities = answer.value, answer.reason, answer.probabilities
    elif isinstance(answer, dict):
        value, reason, probabilities = answer.get("value"), answer.get("reason", "cleared"), answer.get("probabilities")
    else:
        value, reason, probabilities = answer, "cleared", None
    if value not in question.options:
        raise Rejected("policy_invalid", f"{source} answered {value!r} to {question.key}; the options were {list(question.options)}")
    if probabilities is not None:
        if not isinstance(probabilities, dict) or set(probabilities) != set(question.options):
            raise Rejected("policy_invalid", f"{source}: probabilities must cover exactly the options sent")
        for option, p in probabilities.items():
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 1 or p != p:
                raise Rejected("policy_invalid", f"{source}: probability for {option!r} is not a number in [0, 1]")
        if abs(sum(probabilities.values()) - 1) > 0.02:
            raise Rejected("policy_invalid", f"{source}: probabilities sum to {sum(probabilities.values()):.3f}, not 1")
    return Answer(question.key, value, source, str(reason), probabilities)


class OfflinePolicy:
    """Deterministic rules over the state's numbers. The default."""
    name = "offline"

    def decide(self, state: dict, question: Question) -> Answer:
        features = state["features"]
        if question.key == "next_feature":
            pick = max(question.options, key=lambda fid: (features[fid]["bottom_level"], features[fid]["risk"], -features[fid]["map_order"]))
            return Answer(question.key, pick, self.name, "rule:critical_path_first")
        if question.key == "proposer_arm":
            arms = {arm["id"]: arm for arm in state["envelope"]["arms"]}
            pick = min(question.options, key=lambda a: (arms[a].get("tier_rank", 0), question.options.index(a)))
            return Answer(question.key, pick, self.name, "rule:cheapest_arm")
        if question.key == "budget_tier":
            attempts = features[question.feature]["attempts"]
            wanted = TIERS[min(attempts, len(TIERS) - 1)]
            pick = wanted if wanted in question.options else question.options[0]
            return Answer(question.key, pick, self.name, "rule:cheapest_tier_that_fits")
        if question.key == "after_failure":
            # First failure: the same arm again. Later failures with attempts left: a stronger arm when the envelope
            # has one, else the owner. No retry offered (the cap is spent): park; the ledger holds the reason.
            attempts = features[question.feature]["attempts"]
            retry = [o for o in question.options if o.startswith("retry_")]
            if attempts <= 1 and "retry_same" in retry:
                return Answer(question.key, "retry_same", self.name, "rule:first_failure_retry")
            if "retry_escalate_arm" in retry:
                return Answer(question.key, "retry_escalate_arm", self.name, "rule:escalate_arm")
            if retry and "escalate_owner" in question.options:
                return Answer(question.key, "escalate_owner", self.name, "rule:no_stronger_arm")
            pick = "park" if "park" in question.options else question.options[-1]
            return Answer(question.key, pick, self.name, "rule:attempts_spent")
        if question.key == "worth_continuing":
            envelope = state["envelope"]
            room = envelope["dollars_left"] >= envelope["smallest_tier_dollars"] and envelope["calls_left"] > 0
            left = features[question.feature]["attempts_left"] > 0
            pick = "continue" if (room and left and "continue" in question.options) else "stop"
            return Answer(question.key, pick, self.name, "rule:room_and_attempts" if pick == "continue" else "rule:stop_on_envelope")
        raise Rejected("policy_question", f"offline policy has no rule for {question.key}")


class ShadowPolicy:
    """Act on ``acting``; ask ``shadow`` the same question and record what it would have answered.

    ``shadow(state, question)`` is any callable, injected by the caller: a recorded-answer selector, a test
    stub, or later an adapter that reuses ``jev_api``'s request and validation code. Its answer is validated
    against the same options; an invalid or failing shadow is recorded as such (``shadow.reason``) and changes
    nothing. The acting answer is returned unchanged, with ``shadow`` filled in."""
    name = "shadow"

    def __init__(self, acting: FeaturePolicy, shadow: Callable[[dict, Question], Any], *, shadow_name: str = "shadow"):
        self.acting, self.shadow, self.shadow_name = acting, shadow, shadow_name
        self.disagreements = 0

    def decide(self, state: dict, question: Question) -> Answer:
        answer = validate(question, self.acting.decide(state, question), self.acting.name)
        try:
            would = validate(question, self.shadow(state, question), self.shadow_name)
            record = {"policy": self.shadow_name, "value": would.value, "probabilities": would.probabilities,
                      "agrees": would.value == answer.value, "reason": "recorded"}
        except Rejected as exc:
            record = {"policy": self.shadow_name, "value": None, "probabilities": None, "agrees": None, "reason": exc.code}
        except Exception as exc:  # a shadow that breaks never breaks the loop
            record = {"policy": self.shadow_name, "value": None, "probabilities": None, "agrees": None,
                      "reason": f"shadow_error:{type(exc).__name__}"}
        if record["agrees"] is False:
            self.disagreements += 1
        answer.shadow = record
        return answer


def recorded_answers(answers: dict[str, Any]) -> Callable[[dict, Question], Any]:
    """A shadow over a fixture: ``answers`` maps ``"<key>"`` or ``"<key>:<feature>"`` to a value or
    ``{"value", "probabilities"}``. A question with no recorded answer raises, which the shadow wrapper
    records as ``shadow_unanswered``."""
    def shadow(state: dict, question: Question):
        for key in (f"{question.key}:{question.feature}", question.key):
            if key in answers:
                return answers[key]
        raise Rejected("shadow_unanswered", f"no recorded answer for {question.key}")
    return shadow


def ask(policy: FeaturePolicy, state: dict, question: Question) -> Answer:
    """One validated proposal. The caller records it before acting."""
    answer = policy.decide(state, question)
    source = answer.source if isinstance(answer, Answer) else getattr(policy, "name", "policy")
    checked = validate(question, answer, source)
    checked.shadow = answer.shadow if isinstance(answer, Answer) else None
    return checked
