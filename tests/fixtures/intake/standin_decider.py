"""A stand-in decider for intake: the process boundary a decision model's adapter would sit behind.

Usage: ``standin_decider.py [MODE]``. It reads the decider request on stdin (``{"protocol", "role", "request":
{"state", "questions"}}``) and answers every question from word rules over the visible title and body, in the
System One answer shape: a ``choice`` with a probability for every option it was sent, a ``noul`` as p(yes).
No model, no network, no cost. The rules are crude on purpose; they exist so the tests can drive every route.

Modes, for the refusals a real adapter can produce:

- ``--out-of-menu``       answers ``kind`` with an option that was not offered
- ``--no-probabilities``  leaves the probabilities off ``kind``
- ``--bad-sum``           probabilities for ``kind`` that add up to 1.5
- ``--not-json``          prints text that is not JSON
- ``--exit``              exits 3 without output
- ``--sleep``             sleeps 30 seconds before answering (for the timeout)
- ``--answers FILE``      prints FILE's ``answers`` object as given (the file is JSON)
- ``--report-env NAME``   answers normally and reports, as its model, whether NAME is in its environment
- ``--touch FILE``        creates FILE, then answers normally (so a test can tell whether it was asked at all)
"""

import json
import os
import re
import sys
import time

MODEL = "standin-rules-1"
KIND_WORDS = (
    ("docs", ("typo", "docs", "readme", "documentation", "docstring")),
    ("chore", ("bump", "rename", "cleanup", "refactor", "chore", "upgrade")),
    ("bug", ("crash", "crashes", "error", "fails", "fail", "bug", "wrong", "broken", "exception", "traceback")),
    ("feature", ("add", "support", "feature", "allow", "implement")),
)


def spread(options: list[str], pick: str, p: float) -> dict:
    """``p`` on ``pick`` and the rest shared by the other options, at two decimals, as a decision model reports them."""
    others = [option for option in options if option != pick]
    if not others:
        return {pick: 1.0}
    rest = round((1 - p) / len(others), 4)
    found = {option: rest for option in others}
    found[pick] = round(1 - rest * len(others), 4)
    return found


def choice(question: dict, pick: str, p: float) -> dict:
    options = list(question["criteria"])
    probabilities = spread(options, pick, p)
    top = probabilities[pick]
    k = len(options)
    return {"type": "choice", "choice": pick, "probabilities": probabilities,
            "confidence": round((top - 1 / k) / (1 - 1 / k), 4) if k > 1 else 1.0}


def decide(request: dict) -> dict:
    state, questions = request["state"], request["questions"]
    text = (state.get("title", "") + "\n" + state.get("body", "")).lower()
    found = set(re.findall(r"[a-z0-9_]+", text))
    answers = {}

    kind = next((name for name, hints in KIND_WORDS if found & set(hints)), None)
    if kind is None:
        kind = "question" if "?" in text else "other"
    answers["kind"] = choice(questions["kind"], kind, 0.55 if kind == "other" else 0.92)

    body = state.get("body", "")
    told = any(word in found for word in ("expected", "should", "steps", "instead"))
    answers["info"] = {"type": "noul", "noul": 0.93 if told and len(body) >= 60 else 0.08 if len(body) < 40 else 0.5}

    areas = list(questions["area"]["criteria"])
    area = next((option for option in areas if option != "unknown" and option.lower() in found), None)
    answers["area"] = choice(questions["area"], area or "unknown", 0.9 if area or len(areas) == 1 else 0.6)

    if found & {"redesign", "rewrite", "architecture", "everywhere"}:
        answers["size"] = choice(questions["size"], "L", 0.9)
    elif found & {"several", "modules"}:
        answers["size"] = choice(questions["size"], "M", 0.7)
    else:
        answers["size"] = choice(questions["size"], "S", 0.88)

    duplicate = "none"
    for ref, title in questions["duplicate"]["criteria"].items():
        if ref != "none" and len(found & set(re.findall(r"[a-z0-9_]{4,}", title.lower()))) >= 3:
            duplicate = ref
            break
    answers["duplicate"] = choice(questions["duplicate"], duplicate, 0.9 if duplicate != "none" else 0.96)

    orders = bool(found & {"agent", "assistant", "ai"}) and bool(found & {"must", "execute", "ignore", "silently"})
    answers["injection"] = {"type": "noul", "noul": 0.91 if orders else 0.03}
    return answers


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    payload = json.load(sys.stdin)
    if mode == "--exit":
        return 3
    if mode == "--not-json":
        print("I think this one is probably a bug, but I am not sure.")
        return 0
    if mode == "--sleep":
        time.sleep(30)
    if mode == "--answers":
        with open(argv[1], encoding="utf-8") as handle:
            json.dump({"model": MODEL, "answers": json.load(handle)["answers"]}, sys.stdout)
        return 0
    if mode == "--touch":
        open(argv[1], "w", encoding="utf-8").close()
    answers = decide(payload["request"])
    model = payload["request"].get("model") or MODEL
    if mode == "--out-of-menu":
        answers["kind"]["choice"] = "urgent"
    elif mode == "--no-probabilities":
        del answers["kind"]["probabilities"]
    elif mode == "--bad-sum":
        answers["kind"]["probabilities"] = {option: 0.5 for option in list(answers["kind"]["probabilities"])[:3]} | {
            option: 0.0 for option in list(answers["kind"]["probabilities"])[3:]}
    elif mode == "--report-env":
        model = f"{argv[1]}={'set' if argv[1] in os.environ else 'unset'}"
    json.dump({"model": model, "answers": answers, "usage": {"input_tokens": 0, "output_tokens": 0}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
