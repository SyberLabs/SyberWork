#!/usr/bin/env python3
"""A stand-in model for exercising the comparison harness without spending money. NOT a model.

It speaks the same two protocols as adapters/anthropic_adapter.py and edits the
fixture's known slots: a patch sets every slot (right with probability
``SIM_PATCH``), a repair re-rolls the slots whose checks failed (right with
``SIM_REPAIR``), a mutation re-rolls one slot (right with ``SIM_MUTATE``). Token
usage is estimated from text length and marked ``simulated``. Results produced
with it describe the harness and these probabilities, not any model.

Environment: ``SYBERLABS_FIXTURE`` (required), ``SIM_PATCH`` (0.45),
``SIM_REPAIR`` (0.6), ``SIM_MUTATE`` (0.35).
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.fixtures import FIXTURES  # noqa: E402


def roll(slot: dict, rng: random.Random, right: float) -> str:
    wrong = [v for i, v in enumerate(slot["variants"]) if i not in slot["correct"] and i != 0] or slot["variants"]
    if rng.random() < right:
        return slot["variants"][rng.choice(slot["correct"])]
    return rng.choice(wrong)


def put(text: str, slot: dict, line: str) -> str:
    pattern = re.compile(rf"(def {slot['function']}\([^)]*\):\n    )return [^\n]*")
    return pattern.sub(lambda m: m.group(1) + line, text, count=1)


def respond(request: dict, cwd: Path) -> dict:
    fixture = FIXTURES[os.environ["SYBERLABS_FIXTURE"]]
    rng = random.Random(json.dumps([request.get("seed"), request.get("parent"), request.get("protocol")], sort_keys=True)
                        if request.get("seed") is not None else None)
    if request["protocol"] == "syberlabs.mutate/v0alpha1":
        files = dict(request.get("files") or {})
        slot = rng.choice(fixture["slots"])
        text = files.get(slot["path"]) or (cwd / slot["path"]).read_text()
        changes = {slot["path"]: put(text, slot, roll(slot, rng, float(os.environ.get("SIM_MUTATE", "0.35"))))}
        body = {"changes": changes}
    else:
        feedback = request.get("feedback")
        failing = {c["name"] for c in (feedback or {}).get("checks", []) if c["state"] != "passed"}
        texts = dict((feedback or {}).get("files") or {})
        for slot in fixture["slots"]:
            texts.setdefault(slot["path"], (cwd / slot["path"]).read_text())
            if feedback is None:
                texts[slot["path"]] = put(texts[slot["path"]], slot, roll(slot, rng, float(os.environ.get("SIM_PATCH", "0.45"))))
            elif slot["check"] in failing:
                texts[slot["path"]] = put(texts[slot["path"]], slot, roll(slot, rng, float(os.environ.get("SIM_REPAIR", "0.6"))))
        body = {"candidates": [{"changes": texts, "message": "simulated patch", "signal": {"simulated": True}}], "recommended": [0]}
    body["usage"] = {"input_tokens": len(json.dumps(request)) // 4, "output_tokens": len(json.dumps(body)) // 4,
                     "model": "simulated", "stop_reason": "end_turn", "simulated": True}
    return body


if __name__ == "__main__":
    print(json.dumps(respond(json.load(sys.stdin), Path.cwd())))
