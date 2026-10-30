"""Specs and tickets: a versioned set of requirements that claims a footprint of cells.

A spec is one capability's requirements, each with a stable clause id, EARS-style text, scenarios, and the
evidence kinds its tickets must produce. Its ``footprint_hint`` names components; ``resolve_footprint``
checks the hint against a Space graph at a commit and returns the real footprint, so a spec claims cells
that exist. States run draft, revised, mutated, candidate, promoted, canonical, retired; every operation is
a ledger event with actor, model and an input digest, never an in-place edit of history.

A ticket is a delta over a footprint between two commits. ``ticket_from_spec`` opens one per requirement of
a promoted spec with the footprint resolved, the acceptance criteria copied from the scenarios, and one exit
criterion per evidence kind. Agents are delegated to; a human stays the owner.

v0 implements the lifecycle a single spec needs: draft, revise, validate (to candidate), promote, canonize,
retire. Mutation (several models proposing variants of one parent) is the v1 addition; the fields are here.
"""

from __future__ import annotations

from syberlabs.canonical import digest
from syberlabs.errors import Rejected
from syberlabs.factory import PROTOCOL

STATES = ("draft", "revised", "mutated", "candidate", "promoted", "canonical", "retired")
EVIDENCE_KINDS = frozenset({"trace_diff", "drift", "tests", "mutation", "critic", "replay"})
TRANSITIONS = {
    "revise": ({"draft", "revised", "candidate"}, "revised"),
    "mutate": ({"candidate", "canonical"}, "mutated"),
    "validate": ({"draft", "revised", "mutated"}, "candidate"),
    "promote": ({"candidate"}, "promoted"),
    "canonize": ({"promoted"}, "canonical"),
    "retire": ({"canonical", "promoted", "candidate"}, "retired"),
}


def new_spec(spec_id: str, capability: str, requirements: list[dict], footprint_hint: list[str], *,
             author: dict | None = None, constitution_refs: list[str] | None = None) -> dict:
    """A draft spec. Each requirement needs ``req_id`` and ``text``; ``scenarios`` and ``evidence_kinds`` default."""
    if not spec_id or not capability:
        raise Rejected("spec_invalid", "a spec needs an id and a capability")
    seen = set()
    cleaned = []
    for requirement in requirements:
        req_id = requirement.get("req_id")
        if not req_id or req_id in seen:
            raise Rejected("spec_invalid", f"requirement ids must be present and unique; {req_id!r} is not")
        seen.add(req_id)
        kinds = list(requirement.get("evidence_kinds") or ["trace_diff"])
        unknown = sorted(set(kinds) - EVIDENCE_KINDS)
        if unknown:
            raise Rejected("spec_invalid", f"unknown evidence kinds {unknown}; known: {sorted(EVIDENCE_KINDS)}")
        cleaned.append({"req_id": req_id, "text": requirement.get("text", ""),
                        "scenarios": list(requirement.get("scenarios") or []), "evidence_kinds": kinds})
    return {"protocol": PROTOCOL, "kind": "spec", "spec_id": spec_id, "capability": capability, "version": 1,
            "parent_version": None, "supersedes": None, "state": "draft", "requirements": cleaned,
            "footprint_hint": list(footprint_hint), "footprint": None, "footprint_revision": None,
            "constitution_refs": list(constitution_refs or []),
            "author": author or {"kind": "human", "id": "unknown"}, "history": []}


def spec_digest(spec: dict) -> str:
    """The spec without its history, so two specs with the same content digest alike."""
    return digest({k: v for k, v in spec.items() if k != "history"})


def transition(spec: dict, operation: str, actor: dict, **detail) -> dict:
    """Apply a lifecycle operation. The record of it (operation, actor, from, to, input digest) goes on
    ``history`` and is what the ledger logs as ``spec_changed``. Returns that record."""
    if operation not in TRANSITIONS:
        raise Rejected("spec_operation", f"unknown operation {operation!r}; one of {sorted(TRANSITIONS)}")
    allowed, target = TRANSITIONS[operation]
    if spec["state"] not in allowed:
        raise Rejected("spec_state", f"{operation} is not allowed from state {spec['state']!r} (needs one of {sorted(allowed)})")
    if operation == "revise":
        delta = detail.get("delta") or {}
        _apply_delta(spec, delta)
        spec["parent_version"] = spec["version"]
        spec["version"] += 1
    elif operation == "mutate":
        delta = detail.get("delta") or {}
        _apply_delta(spec, delta)
        spec["parent_version"] = spec["version"]
        spec["version"] += 1
    elif operation == "validate":
        findings = validate(spec, detail.get("space"))
        if findings:
            raise Rejected("spec_invalid", "; ".join(findings))
    elif operation == "promote":
        if not detail.get("verdict_ids"):
            raise Rejected("spec_verdict", "promotion records the review verdicts it rests on; none given")
    elif operation == "retire":
        spec["supersedes"] = detail.get("superseded_by")
    record = {"operation": operation, "actor": actor, "from": spec["state"], "to": target, "version": spec["version"],
              "input_digest": digest({k: v for k, v in detail.items() if k != "space"}),
              "reason": detail.get("reason")}
    spec["state"] = target
    spec["history"].append(record)
    return record


def _apply_delta(spec: dict, delta: dict) -> None:
    """Requirement-level add, modify, remove, and a footprint hint change. Unknown ids are refused."""
    by_id = {r["req_id"]: r for r in spec["requirements"]}
    for requirement in delta.get("added", []):
        if requirement["req_id"] in by_id:
            raise Rejected("spec_delta", f"requirement {requirement['req_id']!r} already exists")
        spec["requirements"].append({"req_id": requirement["req_id"], "text": requirement.get("text", ""),
                                     "scenarios": list(requirement.get("scenarios") or []),
                                     "evidence_kinds": list(requirement.get("evidence_kinds") or ["trace_diff"])})
    for requirement in delta.get("modified", []):
        if requirement["req_id"] not in by_id:
            raise Rejected("spec_delta", f"requirement {requirement['req_id']!r} does not exist")
        by_id[requirement["req_id"]].update({k: v for k, v in requirement.items() if k != "req_id"})
    for req_id in delta.get("removed", []):
        if req_id not in by_id:
            raise Rejected("spec_delta", f"requirement {req_id!r} does not exist")
        spec["requirements"] = [r for r in spec["requirements"] if r["req_id"] != req_id]
    if "footprint_hint" in delta:
        spec["footprint_hint"] = list(delta["footprint_hint"])
        spec["footprint"] = None
        spec["footprint_revision"] = None


def validate(spec: dict, space: dict | None = None) -> list[str]:
    """Structural and cross-artifact findings. Empty means the spec may become a candidate."""
    findings = []
    if not spec["requirements"]:
        findings.append("a spec needs at least one requirement")
    for requirement in spec["requirements"]:
        if not requirement["text"].strip():
            findings.append(f"{requirement['req_id']}: empty text")
        if not requirement["evidence_kinds"]:
            findings.append(f"{requirement['req_id']}: no evidence kind")
    if not spec["footprint_hint"]:
        findings.append("a spec needs a footprint hint naming at least one component")
    if space is not None:
        known = set(space.get("components", {}))
        missing = [c for c in spec["footprint_hint"] if c not in known]
        if missing:
            findings.append(f"footprint hint names components the Space graph does not have: {missing}")
    return findings


def resolve_footprint(spec: dict, space: dict) -> dict:
    """Check the hint against a Space graph and pin the footprint to that graph's revision.

    The footprint is the hinted components plus every file they own at this revision, so a ticket can list
    file pointers. Components the graph lacks are refused; a spec never claims cells that do not exist."""
    known = space.get("components", {})
    missing = [c for c in spec["footprint_hint"] if c not in known]
    if missing:
        raise Rejected("footprint_unresolved", f"components not in the Space graph at {space.get('revision')}: {missing}")
    files = sorted(f for c in spec["footprint_hint"] for f in known[c]["files"])
    spec["footprint"] = {"components": sorted(spec["footprint_hint"]), "files": files,
                         "revision": space.get("revision"), "space_digest": None}
    spec["footprint_revision"] = space.get("revision")
    return spec["footprint"]


def ticket_from_spec(spec: dict, requirement_id: str, *, baseline_run: str | None = None, human_owner: str | None = None,
                     delegate: dict | None = None) -> dict:
    """One ticket for one requirement of a promoted spec, with the resolved footprint and the exit criteria."""
    if spec["state"] not in ("promoted", "canonical"):
        raise Rejected("spec_state", f"tickets open from a promoted spec, not one in state {spec['state']!r}")
    if not spec.get("footprint"):
        raise Rejected("footprint_unresolved", "resolve the footprint against a Space graph before opening tickets")
    requirement = next((r for r in spec["requirements"] if r["req_id"] == requirement_id), None)
    if requirement is None:
        raise Rejected("spec_requirement", f"no requirement {requirement_id!r} in spec {spec['spec_id']}")
    ticket = {
        "protocol": PROTOCOL, "kind": "ticket",
        "ticket_id": digest({"spec": spec["spec_id"], "version": spec["version"], "req": requirement_id})[:16],
        "title": requirement["text"][:120],
        "spec_ref": {"spec_id": spec["spec_id"], "version": spec["version"], "req_ids": [requirement_id]},
        "acceptance_criteria": list(requirement["scenarios"]),
        "footprint": dict(spec["footprint"]),
        "repro": {"baseline_run": baseline_run},
        "exit_criteria": [f"evidence:{kind}" for kind in requirement["evidence_kinds"]],
        "delegate": delegate, "human_owner": human_owner,
        "state": "open", "candidates": [], "verdicts": [], "evidence_links": [],
    }
    return ticket


def tickets_from_drift(report: dict, revision: str | None, *, threshold: float, human_owner: str | None = None) -> list[dict]:
    """One ticket per component whose drift exceeds ``threshold``: its footprint is the component and the
    targets of its undeclared edges, its acceptance criterion is a clean reflexion report, and its exit
    criterion is drift evidence. Unmapped files get one ticket of their own."""
    tickets = []
    divergent_by = {}
    for edge in report.get("divergence", []):
        divergent_by.setdefault(edge["from"], []).append(edge)
    for component, score in sorted(report.get("drift", {}).items()):
        if score["score"] <= threshold or component not in divergent_by:
            continue
        edges = divergent_by[component]
        targets = sorted({e["to"] for e in edges})
        tickets.append({
            "protocol": PROTOCOL, "kind": "ticket",
            "ticket_id": digest({"drift": component, "revision": revision, "edges": [(e["from"], e["to"]) for e in edges]})[:16],
            "title": f"{component} depends on {', '.join(targets)} without a declared edge",
            "spec_ref": {"spec_id": "architecture", "version": None, "req_ids": [f"declared-edges:{component}"]},
            "acceptance_criteria": [{"given": f"the Space graph at a commit after {revision}", "when": "reflexion runs",
                                     "then": f"{component} has no undeclared outgoing edge (or the declaration gains it)"}],
            "footprint": {"components": sorted({component, *targets}), "files": [], "revision": revision, "space_digest": None},
            "repro": {"baseline_run": None, "divergent_edges": [{"from": e["from"], "to": e["to"], "count": e["count"],
                                                                 "via": e["via"][:5]} for e in edges]},
            "exit_criteria": ["evidence:drift"], "delegate": None, "human_owner": human_owner,
            "state": "open", "candidates": [], "verdicts": [], "evidence_links": [],
        })
    if report.get("unmapped"):
        files = list(report["unmapped"])
        tickets.append({
            "protocol": PROTOCOL, "kind": "ticket", "ticket_id": digest({"unmapped": files, "revision": revision})[:16],
            "title": f"{len(files)} file(s) belong to no declared component",
            "spec_ref": {"spec_id": "architecture", "version": None, "req_ids": ["declared-paths"]},
            "acceptance_criteria": [{"given": "the declaration", "when": "every file is mapped", "then": "unmapped is empty"}],
            "footprint": {"components": [], "files": files, "revision": revision, "space_digest": None},
            "repro": {"baseline_run": None}, "exit_criteria": ["evidence:drift"], "delegate": None,
            "human_owner": human_owner, "state": "open", "candidates": [], "verdicts": [], "evidence_links": [],
        })
    return tickets
