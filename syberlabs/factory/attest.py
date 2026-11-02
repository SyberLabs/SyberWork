"""Attestation: a trace diff over a footprint, carried in an in-toto statement, signed as DSSE.

``trace_diff`` replays or reruns are someone else's job; here two recorded runs are compared over the
components a ticket claims, and the result is one typed evidence entry. ``statement`` wraps the evidence,
the spec reference, the footprint, the builder and the verdicts in an in-toto Statement v1 whose subject is
the candidate commit. ``sign`` and ``verify`` use the repository's own Ed25519 and DSSE code; the seed is the
caller's and is never written here. ``policy`` is the gate: it reads the predicate and says whether the
candidate may be promoted, with the reasons, so a reviewer reads a verdict rather than a diff.

Test passes are weak evidence on their own (PatchDiff measured about 30 percent of test-passing SWE-bench
patches diverging behaviorally from the oracle), so the policy accepts a trace diff that is empty, or a
non-empty one the ticket labels as an intended behavior change, before it looks at tests at all.
"""

from __future__ import annotations

import base64
import json

from syberlabs.canonical import canonical, digest
from syberlabs.dsse import sign_dsse, verified_payload
from syberlabs.errors import Rejected
from syberlabs.factory import FACTORY_VERSION, harness

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://syberlabs.space/attestation/spec-satisfied/v1"
PAYLOAD_TYPE = "application/vnd.in-toto+json"


def trace_diff(baseline: list[dict], candidate: list[dict], footprint: dict | None, level: str = "component") -> dict:
    """The evidence entry: did the candidate run cross the same component boundaries, in the same order, with
    the same effect results, over the footprint's components? ``footprint`` is a ticket's footprint
    (``components``) or None for the whole run. ``level`` is ``component`` (the default: a refactor inside a
    component is not a change) or ``function`` (every call must match). The other level's verdict is recorded
    beside the chosen one so a reviewer sees both."""
    components = set(footprint["components"]) if footprint else None
    report = harness.compare(baseline, candidate, components, level)
    other = "function" if level == "component" else "component"
    other_report = harness.compare(baseline, candidate, components, other)
    detail = None
    if report["first_divergence"]:
        detail = {"before": _describe(baseline, report["first_divergence"]["before"]),
                  "after": _describe(candidate, report["first_divergence"]["after"])}
    base_header = baseline[0] if baseline and baseline[0].get("t") == "run" else {}
    cand_header = candidate[0] if candidate and candidate[0].get("t") == "run" else {}
    return {
        "kind": "trace_diff",
        "baseline_run": base_header.get("run_id"), "baseline_revision": base_header.get("revision"),
        "candidate_run": cand_header.get("run_id"), "candidate_revision": cand_header.get("revision"),
        "footprint": sorted(components) if components else None,
        "level": level,
        "diff_empty": report["equal"],
        "steps_before": report["steps_before"], "steps_after": report["steps_after"],
        "first_divergence": report["first_divergence"],
        "divergence_detail": detail,
        f"{other}_level_equal": other_report["equal"],
        f"{other}_level_first_divergence": other_report["first_divergence"],
        "diff_digest": digest(report),
    }


def _describe(records: list[dict], entry) -> dict | None:
    """What a projected entry was: for an effect, marker or io, the record's name and payload (argv for a
    subprocess, masked); for a call, the component. None when a run was shorter."""
    if entry is None:
        return None
    head, tail = entry
    if isinstance(head, str) and ":" in head and head.split(":", 1)[0] in ("effect", "marker", "io"):
        kind, name = head.split(":", 1)
        for record in records:
            if record.get("t") == kind and record.get("name") == name and record.get("digest") == tail:
                payload = record.get("payload")
                if kind == "io" and isinstance(payload, dict) and "argv" in payload:
                    payload = {"argv": [harness.mask(a) for a in payload["argv"]]}
                return {"kind": kind, "name": name, "seq": record["seq"], "payload": payload}
        return {"kind": kind, "name": name, "payload": None}
    return {"kind": "call", "component": head, "fn": tail}


def drift_evidence(report: dict, revision: str | None) -> dict:
    """The evidence entry for architecture conformance at the candidate: the reflexion report's numbers."""
    return {"kind": "drift", "revision": revision, "max_drift": report["max_drift"],
            "divergent": [(e["from"], e["to"], e["count"]) for e in report["divergence"]],
            "absent": [(e["from"], e["to"]) for e in report["absence"]], "unmapped": len(report["unmapped"]),
            "drift": {k: v["score"] for k, v in report["drift"].items()}}


def statement(*, subject_commit: str, repository: str, spec_ref: dict, footprint: dict | None, evidence: dict,
              builder: dict, ticket: str | None = None, verdicts: list[dict] | None = None,
              behavior_change_label: bool = False, gate: dict | None = None) -> dict:
    """An in-toto Statement v1 with the spec-satisfied predicate. ``evidence`` maps kind to entry
    (``trace_diff`` from :func:`trace_diff`; ``drift`` from :func:`drift_evidence`; ``tests``, ``mutation``,
    ``critic`` from their producers). ``gate`` holds the policy's parameters (``require_tests``,
    ``mutation_threshold``, ``max_drift``) so the statement says what it was judged against."""
    if not subject_commit:
        raise Rejected("attestation_subject", "an attestation's subject is the candidate commit")
    gate = dict(gate or {})
    predicate = {
        "factory": FACTORY_VERSION,
        "spec": {"spec_id": spec_ref.get("spec_id"), "version": spec_ref.get("version"),
                 "req_ids": list(spec_ref.get("req_ids", []))},
        "ticket": ticket,
        "footprint": footprint,
        "evidence": evidence,
        "behavior_change_label": bool(behavior_change_label),
        "builder": builder,
        "verdicts": list(verdicts or []),
        "gate": gate,
    }
    predicate["policy"] = policy(predicate, **gate)
    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": f"git:{repository}@{subject_commit}", "digest": {"gitCommit": subject_commit}}],
        "predicateType": PREDICATE_TYPE,
        "predicate": predicate,
    }


def policy(predicate: dict, *, require_tests: bool = False, mutation_threshold: float | None = None,
           max_drift: float | None = None, level: str | None = None) -> dict:
    """The gate. Promote when the trace diff over the footprint is empty, or is non-empty and the ticket
    carries the behavior-change label; then when tests ran the full suite (if required), the mutation
    score meets the threshold (if set), and architecture drift at the candidate stays at or under
    ``max_drift`` (if set). Every reason is listed, pass or fail."""
    reasons = []
    promote = True
    evidence = predicate.get("evidence", {})
    diff = evidence.get("trace_diff")
    if diff is None:
        reasons.append("no trace diff: refuse")
        promote = False
    elif diff.get("diff_empty"):
        reasons.append(f"trace diff over the footprint is empty at {diff.get('level', level or 'component')} level")
    elif predicate.get("behavior_change_label"):
        reasons.append("trace diff is not empty and the ticket is labeled as a behavior change")
    else:
        first = diff.get("first_divergence") or {}
        reasons.append(f"trace diff is not empty at projected step {first.get('index')} and no behavior change was declared: refuse")
        promote = False
    tests = evidence.get("tests")
    if require_tests:
        if not tests:
            reasons.append("tests required and none recorded: refuse")
            promote = False
        elif not tests.get("full_suite") or tests.get("fail", 0):
            reasons.append("tests did not run the full suite green: refuse")
            promote = False
        else:
            reasons.append("full test suite green")
    elif tests:
        reasons.append("tests recorded: " + ("green" if not tests.get("fail", 0) else f"{tests.get('fail')} failing"))
    mutation = evidence.get("mutation")
    if mutation_threshold is not None:
        score = (mutation or {}).get("score")
        if score is None or score < mutation_threshold:
            reasons.append(f"mutation score {score} below {mutation_threshold}: refuse")
            promote = False
        else:
            reasons.append(f"mutation score {score} meets {mutation_threshold}")
    drift = evidence.get("drift")
    if max_drift is not None:
        if drift is None:
            reasons.append("drift bound set and no drift evidence: refuse")
            promote = False
        elif drift.get("max_drift", 1.0) > max_drift:
            worst = max(drift.get("drift", {}).items(), key=lambda kv: kv[1], default=("?", None))
            reasons.append(f"architecture drift {drift['max_drift']} exceeds {max_drift} (worst: {worst[0]}): refuse")
            promote = False
        else:
            reasons.append(f"architecture drift {drift['max_drift']} within {max_drift}")
    return {"promote": promote, "reasons": reasons}


GATE_PARAMETERS = ("require_tests", "mutation_threshold", "max_drift", "level")


def recompute(predicate: dict) -> dict:
    """The policy's result computed again from the predicate's evidence and gate, so a verifier compares
    what the signer wrote under ``policy`` with what the evidence says. Unknown gate keys are ignored."""
    gate = {k: v for k, v in (predicate.get("gate") or {}).items() if k in GATE_PARAMETERS}
    return policy(predicate, **gate)


def sign(document: dict, seed: bytes) -> dict:
    """A one-signature DSSE envelope over the canonical statement. ``keyid`` is the public key, hex."""
    return sign_dsse(PAYLOAD_TYPE, canonical(document).encode("utf-8"), seed)


def verify(envelope: dict, public: bytes) -> dict | None:
    """The statement, when the signature verifies under ``public`` and the payload is a statement. Else None."""
    payload = verified_payload(envelope, public, PAYLOAD_TYPE)
    if payload is None:
        return None
    try:
        document = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(document, dict) or document.get("_type") != STATEMENT_TYPE:
        return None
    return document


def unwrap(document: dict) -> dict:
    """The statement inside an envelope, unverified, or the document itself when it is a bare statement."""
    if "payloadType" in document:
        return json.loads(base64.standard_b64decode(document["payload"]))
    return document
