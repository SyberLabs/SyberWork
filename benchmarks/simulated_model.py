#!/usr/bin/env python3
"""A stand-in model for exercising the comparison harness without spending money. NOT a model.

It speaks the same two protocols as adapters/anthropic_adapter.py and edits the
fixture's known slots: a patch sets every slot (right with probability
``SIM_PATCH``), a repair re-rolls the slots whose checks failed (right with
``SIM_REPAIR``), a mutation re-rolls one slot (right with ``SIM_MUTATE``). A slot
replaces the first body line of its function. A slot with no correct variant
(``correct: []``, the v2 fixtures) is always rolled wrong: the stand-in knows
only line swaps, and on those fixtures no line swap passes, so it fails them by
construction and no arm can solve them with it. Token usage is estimated from
text length and marked ``simulated``. Results produced with it describe the
harness and these probabilities, not any model.

Environment: ``SYBERLABS_FIXTURE`` (required), ``SIM_PATCH`` (0.45),
``SIM_REPAIR`` (0.6), ``SIM_MUTATE`` (0.35), ``SIM_PROFILE`` (unset).

``SIM_PROFILE`` names a per-operator success profile for the mutate protocol only
(``PROFILES``); the patch and repair protocols and an unnamed profile behave exactly
as above, draw for draw. A profile makes the branch policies of ``syberlabs.evolve``
distinguishable: ``mutation_rare`` is a world where a mutation of ``base`` fixes one
slot (a fresh look gets one thing right), a mutation of a child fixes one of its
remaining slots one time in five and otherwise rewrites one remaining slot wrong,
and two children fix different slots, so a crossover combines them;
``mutation_common`` is the reverse, a mutation rewrites every remaining slot, each
right three times in five, so children conflict in every slot and a crossover
combines nothing a mutation would not have. A crossover never calls this program:
its success in a profile is structural (whether the children's edits are disjoint),
not a rolled probability. A profile fixes nothing on a slot with no correct variant.
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
    if slot["correct"] and rng.random() < right:
        return slot["variants"][rng.choice(slot["correct"])]
    return rng.choice(wrong)


PROFILES = {
    # fix: the probability that a mutation of a child fixes a slot it touches (a mutation of base always fixes one);
    # touch: "one" remaining slot per mutation, or "all" of them.
    "mutation_rare": {"fix": 0.2, "touch": "one"},
    "mutation_common": {"fix": 0.6, "touch": "all"},
}


def current(text: str, slot: dict) -> str | None:
    """The first body line of the slot's function as it stands, or None when the function is not found."""
    found = re.search(r"def " + re.escape(slot["function"]) + r"\([^)]*\):\n    ([^\n]*)", text)
    return found.group(1) if found else None


def is_right(text: str, slot: dict) -> bool:
    line = current(text, slot)
    return line is not None and line in {slot["variants"][i] for i in slot["correct"]}


def mutate_with_profile(fixture: dict, files: dict, cwd: Path, rng: random.Random, profile: dict, from_base: bool) -> dict:
    """One mutation under a profile: the changed files. Deterministic from ``rng``."""
    texts = {}
    for slot in fixture["slots"]:
        texts.setdefault(slot["path"], files.get(slot["path"]) or (cwd / slot["path"]).read_text())
    remaining = [slot for slot in fixture["slots"] if not is_right(texts[slot["path"]], slot)]
    if not remaining:
        return {}
    chosen = remaining if profile["touch"] == "all" else [rng.choice(remaining)]
    for slot in chosen:
        before = current(texts[slot["path"]], slot)
        if slot["correct"] and (from_base and profile["touch"] == "one" or rng.random() < profile["fix"]):
            line = slot["variants"][rng.choice(slot["correct"])]
        else:
            wrong = [v for i, v in enumerate(slot["variants"]) if i not in slot["correct"] and v != before] or slot["variants"]
            line = rng.choice(wrong)
        texts[slot["path"]] = put(texts[slot["path"]], slot, line)
    return texts


def put(text: str, slot: dict, line: str) -> str:
    pattern = re.compile(rf"(def {slot['function']}\([^)]*\):\n    )[^\n]*")
    return pattern.sub(lambda m: m.group(1) + line, text, count=1)


def respond(request: dict, cwd: Path) -> dict:
    fixture = FIXTURES[os.environ["SYBERLABS_FIXTURE"]]
    rng = random.Random(json.dumps([request.get("seed"), request.get("parent"), request.get("protocol")], sort_keys=True)
                        if request.get("seed") is not None else None)
    profile = os.environ.get("SIM_PROFILE")
    if request["protocol"] == "syberlabs.mutate/v0alpha1" and profile:
        if profile not in PROFILES:
            raise SystemExit(f"SIM_PROFILE must be one of {sorted(PROFILES)}, not {profile!r}")
        body = {"changes": mutate_with_profile(fixture, dict(request.get("files") or {}), cwd, rng, PROFILES[profile],
                                               request.get("parent") == "base")}
    elif request["protocol"] == "syberlabs.mutate/v0alpha1":
        # ``feedback`` (the parent's check results, since 2026-10-22) is ignored on purpose, under every
        # profile: the stand-in rolls one slot, it does not read; nothing simulated is about a model.
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
