"""Stand-in proposers and a reviewer for the factory's variant loop. Each role reads JSON on stdin and writes
JSON on stdout, the process boundary a real model adapter would sit behind, and declares the model that served
it (``model``), which is what the loop judges independence on. Usage: proposers.py ROLE [MODEL]; without MODEL
each role declares its own default, and ``MODEL`` may be ``none`` to declare nothing."""

import json
import sys

DEFAULT_MODELS = {"tighten": "claude-sonnet-4-5", "overreach": "grok-4", "review": "gpt-5", "review-all": "gpt-5"}


def main(role: str, model: str | None = None) -> int:
    request = json.load(sys.stdin)
    if role == "tighten":
        # A sound proposal: one requirement made testable, nothing else touched.
        out = {"modified": [{"req_id": "R1", "text": "When quantity is positive, the system shall post exactly one order "
                                                      "and record its id.",
                             "scenarios": [{"given": "quantity 2", "when": "run", "then": "one http.post_order effect with status 201"}]}],
               "rationale": "the original said 'one order' without naming the evidence"}
    elif role == "overreach":
        # Claims a component the Space graph does not have: validation finds it, the reviewer sees the finding.
        out = {"added": [{"req_id": "R9", "text": "The system shall email the buyer.", "evidence_kinds": ["trace_diff"]}],
               "footprint_hint": request["parent"]["footprint_hint"] + ["mailer"], "rationale": "buyers want mail"}
    elif role == "broken":
        print("not json at all")
        return 0
    elif role == "review":
        verdicts = []
        for variant in request["variants"]:
            if variant["findings"]:
                verdicts.append({"variant_id": variant["variant_id"], "decision": "reject",
                                 "rationale": "; ".join(variant["findings"])})
            else:
                verdicts.append({"variant_id": variant["variant_id"], "decision": "accept", "rationale": "testable and in scope"})
        out = {"verdicts": verdicts}
    elif role == "review-all":
        out = {"verdicts": [{"variant_id": v["variant_id"], "decision": "accept"} for v in request["variants"]]}
    else:
        return 2
    declared = DEFAULT_MODELS.get(role) if model is None else model
    if declared and declared != "none":
        out["model"] = declared
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else None))
