"""Builder coordination contracts.

Protocol ``sdk.syberlabs.space/builder/v0alpha1``. These objects are not
SyberWork case events. They do not enter the hash chain, and nothing here
promotes Git state or calls admission.

Four planes stay separate:

* Authority is the existing case history. This module only names it.
* Coordination is generations, approaches, feedback, agents, prototypes, and selections.
* Integrity is attributed observations with an independence class.
* Projection reads those records into World, Generation, Candidate, Actor, and Integrity views.

A role lens changes a projection. It does not write a second set of facts.
Embeddings, model architecture, agent claims, comments, and votes are evidence
a person can read. They are not admission.
"""

from __future__ import annotations

import copy
import re
from typing import Protocol

from syberlabs.canonical import digest
from syberlabs.errors import Rejected


PROTOCOL = "sdk.syberlabs.space/builder/v0alpha1"

MODES = ("explore", "refine", "harden")
ISOLATIONS = ("independent", "aware", "collaborative")
GENERATION_STATES = ("drafting", "sealed", "launched", "evaluating", "selecting", "closed")
LINKABLE_STATES = ("sealed", "launched", "evaluating", "selecting")
APPROACH_STATES = ("active", "rejected", "frozen")

EVENT_KINDS = (
    "generation_created",
    "generation_sealed",
    "generation_launched",
    "generation_closed",
    "approach_registered",
    "approach_rejected",
    "approach_revised",
    "candidate_linked",
    "prototype_registered",
    "prototype_state_changed",
    "agent_registered",
    "agent_assignment_changed",
    "agent_state_changed",
    "agent_activity_recorded",
    "feedback_recorded",
    "integrity_observation_recorded",
    "selection_started",
    "selection_recorded",
    "architecture_snapshot_published",
)

DESCRIPTOR_FIELDS = (
    "intent",
    "interaction_model",
    "architecture",
    "state_model",
    "data_model",
    "primary_abstraction",
    "dependencies",
    "expected_strengths",
    "expected_weaknesses",
    "distinguishing_claims",
    "structural_traits",
)
_TEXT_FIELDS = (
    "intent",
    "interaction_model",
    "architecture",
    "state_model",
    "data_model",
    "primary_abstraction",
)
_LIST_FIELDS = (
    "dependencies",
    "expected_strengths",
    "expected_weaknesses",
    "distinguishing_claims",
    "structural_traits",
)
_STRUCTURAL_KEYS = (
    "architecture",
    "primary_abstraction",
    "interaction_model",
    "state_model",
    "data_model",
)

FEEDBACK_TARGETS = ("generation", "approach", "candidate", "architecture_node", "prototype")
FEEDBACK_KINDS = ("comment", "critique", "preference", "concern", "dissent", "trait_request")
AUTHORITIES = ("informative", "advisory", "veto", "consensus", "decision")
AUTHORITY_RANK = {name: index for index, name in enumerate(AUTHORITIES)}

INTEGRITY_CLASSES = ("internal", "host_verified", "human_reviewed", "external", "signed_external")
INDEPENDENT_CLASSES = ("host_verified", "human_reviewed", "external", "signed_external")
INTEGRITY_RESULTS = ("pass", "fail", "concern", "unknown")
PASSING_RESULTS = ("pass",)

ACTIVITIES = (
    "inspect",
    "read",
    "receive_context",
    "send_context",
    "claim_task",
    "edit_scope",
    "run_check",
    "produce_candidate",
    "request_help",
    "handoff",
    "complete",
)
COMMANDS = ("pause", "resume", "cancel", "send_context", "restrict_scope", "redirect")
AGENT_ROLES = ("implementer", "reviewer", "operator", "search")
AGENT_AUTHORITY = {
    "implementer": "informative",
    "reviewer": "advisory",
    "operator": "informative",
    "search": "informative",
}
AGENT_STATES = ("registered", "active", "paused", "cancelled", "complete")
PROTOTYPE_STATES = ("building", "ready", "failed", "expired")
HIDDEN_KEYS = {"chain_of_thought", "reasoning", "hidden_thought", "private_thought"}
REPLAY_LIMIT = 200


def _text(value, field: str, *, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Rejected("invalid_approach", f"{field} must be a non-empty string")
    return value.strip()


def _strings(value, field: str, *, minimum: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum or len(value) > 32:
        raise Rejected("invalid_approach", f"{field} must be a list of strings")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 500:
            raise Rejected("invalid_approach", f"{field} must be a list of strings")
        items.append(item.strip())
    return items


def _refuse_hidden(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in HIDDEN_KEYS or key in ("fitness", "score"):
                code = "fitness_not_authoritative" if key in ("fitness", "score") else "chain_of_thought_refused"
                raise Rejected(code, "that field is not part of the coordination record")
            _refuse_hidden(item)
    elif isinstance(value, list):
        for item in value:
            _refuse_hidden(item)


def normalize_descriptor(document: dict) -> dict:
    """Approach descriptor. Present before any Git candidate exists."""
    if not isinstance(document, dict):
        raise Rejected("invalid_approach", "approach descriptor must be an object")
    _refuse_hidden(document)
    unknown = set(document) - set(DESCRIPTOR_FIELDS)
    if unknown:
        raise Rejected("invalid_approach", "unknown approach field")
    normalized = {field: _text(document.get(field), field) for field in _TEXT_FIELDS}
    for field in _LIST_FIELDS:
        minimum = 1 if field in ("distinguishing_claims", "structural_traits") else 0
        normalized[field] = _strings(document.get(field, []), field, minimum=minimum)
    return normalized


def _tokens(descriptor: dict) -> set[str]:
    parts = [descriptor[key] for key in _STRUCTURAL_KEYS]
    parts.extend(descriptor["distinguishing_claims"])
    parts.extend(descriptor["structural_traits"])
    text = " ".join(parts).lower().replace("-", "_")
    return set(re.findall(r"[a-z0-9_]+", text))


def structural_distance(left: dict, right: dict) -> float:
    """Jaccard distance on structural tokens. 0 is identical. 1 is disjoint.

    This comparison is deterministic. A semantic provider may be stored beside
    it and cannot change the result.
    """
    a, b = _tokens(left), _tokens(right)
    if not a and not b:
        return 0.0
    return round(1.0 - (len(a & b) / len(a | b)), 6)


class SemanticDistance(Protocol):
    """Optional, replaceable signal. Never the diversity gate."""

    def distance(self, left: dict, right: dict) -> float: ...


def diversity_evidence(descriptor: dict, siblings: list[dict], threshold: float, semantic: SemanticDistance | None = None) -> dict:
    """Pairwise evidence against the approaches already accepted in the generation."""
    if type(threshold) not in (int, float) or isinstance(threshold, bool) or not 0 <= float(threshold) <= 1:
        raise Rejected("invalid_generation", "diversity threshold must be between 0 and 1")
    pairs = []
    failure = None
    inputs = {
        "descriptor_digest": digest(descriptor),
        "threshold": float(threshold),
        "semantic": semantic is not None,
        "siblings": [{"id": sibling["id"], "descriptor_digest": digest(sibling["descriptor"])} for sibling in siblings],
    }
    for sibling in siblings:
        distance = structural_distance(descriptor, sibling["descriptor"])
        claims_equal = set(descriptor["distinguishing_claims"]) == set(sibling["descriptor"]["distinguishing_claims"])
        semantic_distance = None
        if semantic is not None:
            semantic_distance = semantic.distance(descriptor, sibling["descriptor"])
            if type(semantic_distance) not in (int, float) or isinstance(semantic_distance, bool):
                raise Rejected("invalid_approach", "semantic distance must be a number")
        passed = (not claims_equal) and distance >= float(threshold)
        pair = {
            "sibling_id": sibling["id"],
            "structural_distance": distance,
            "claims_equal": claims_equal,
            "semantic_distance": semantic_distance,
            "passed": passed,
        }
        pairs.append(pair)
        if not passed and failure is None:
            failure = sibling["id"]
    evidence = {
        "method": "structural_jaccard",
        "authoritative": "structural",
        "inputs": inputs,
        "pairs": pairs,
    }
    return {"passed": failure is None, "sibling_id": failure, "evidence": evidence}


def normalize_policy(document: dict) -> dict:
    if not isinstance(document, dict):
        raise Rejected("invalid_policy", "selection policy must be an object")
    _refuse_hidden(document)
    identifier = document.get("id")
    version = document.get("version")
    if not isinstance(identifier, str) or not identifier.strip():
        raise Rejected("invalid_policy", "selection policy id required")
    if type(version) is not int or version < 1:
        raise Rejected("invalid_policy", "selection policy version must advance from 1")
    required = document.get("required_integrity", [])
    if not isinstance(required, list) or any(item not in INTEGRITY_CLASSES for item in required):
        raise Rejected("invalid_policy", "required integrity classes are not recognized")
    mapping = document.get("stakeholder_authority")
    if not isinstance(mapping, dict) or not mapping:
        raise Rejected("invalid_policy", "stakeholder authority map required")
    authority = {}
    for role, level in mapping.items():
        if not isinstance(role, str) or not role.strip() or level not in AUTHORITIES:
            raise Rejected("invalid_policy", "stakeholder authority must name a known class")
        authority[role.strip()] = level
    dimensions = document.get("advisory_dimensions", list(FEEDBACK_KINDS))
    if not isinstance(dimensions, list) or any(item not in FEEDBACK_KINDS for item in dimensions):
        raise Rejected("invalid_policy", "advisory dimensions must be feedback kinds")
    threshold = document.get("consensus_threshold", 1)
    if type(threshold) is not int or threshold < 1:
        raise Rejected("invalid_policy", "consensus threshold must be a positive integer")
    promotion = document.get("promotion_roles", [])
    if not isinstance(promotion, list) or any(not isinstance(item, str) or not item for item in promotion):
        raise Rejected("invalid_policy", "promotion roles must be a list of strings")
    weights = document.get("weights")
    if weights is not None:
        if not isinstance(weights, dict):
            raise Rejected("invalid_policy", "weights must be an object of feedback kinds")
        cleaned = {}
        for kind, weight in weights.items():
            if kind not in FEEDBACK_KINDS or type(weight) not in (int, float) or isinstance(weight, bool):
                raise Rejected("invalid_policy", "weights must be numbers on feedback kinds")
            cleaned[kind] = weight
        weights = cleaned
    return {
        "protocol": PROTOCOL,
        "id": identifier.strip(),
        "version": version,
        "required_integrity": list(required),
        "stakeholder_authority": authority,
        "advisory_dimensions": list(dimensions),
        "consensus_threshold": threshold,
        "promotion_roles": list(promotion),
        "weights": weights,
    }


def derive_authority(roles: list[str], policy: dict) -> str:
    """Highest class granted to the participant's roles. The caller cannot choose it."""
    mapping = policy["stakeholder_authority"]
    found = [mapping[role] for role in roles if role in mapping]
    if not found:
        raise Rejected("feedback_authority", "participant role has no configured authority")
    return max(found, key=lambda item: AUTHORITY_RANK[item])


def normalize_feedback(document: dict, roles: list[str], policy: dict) -> dict:
    if not isinstance(document, dict):
        raise Rejected("invalid_feedback", "feedback must be an object")
    _refuse_hidden(document)
    target = document.get("target_kind")
    kind = document.get("kind")
    if target not in FEEDBACK_TARGETS:
        raise Rejected("feedback_target", "feedback target is not recognized")
    if kind not in FEEDBACK_KINDS:
        raise Rejected("invalid_feedback", "feedback kind is not recognized")
    identifier = document.get("target_id")
    text = document.get("text")
    if not isinstance(identifier, str) or not identifier.strip():
        raise Rejected("invalid_feedback", "feedback target id required")
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise Rejected("invalid_feedback", "feedback text required")
    if "authority" in document:
        # Present only so a caller cannot smuggle it. The stored class is derived.
        if document["authority"] not in AUTHORITIES and document["authority"] is not None:
            raise Rejected("invalid_feedback", "unknown authority field")
    return {
        "target_kind": target,
        "target_id": identifier.strip(),
        "kind": kind,
        "text": text.strip(),
        "authority": derive_authority(roles, policy),
    }


def normalize_integrity(document: dict) -> dict:
    if not isinstance(document, dict):
        raise Rejected("invalid_integrity", "integrity observation must be an object")
    _refuse_hidden(document)
    target = document.get("target_kind")
    independence = document.get("independence")
    result = document.get("result")
    if target not in FEEDBACK_TARGETS and target != "case":
        raise Rejected("invalid_integrity", "integrity target is not recognized")
    if independence not in INTEGRITY_CLASSES:
        raise Rejected("invalid_integrity", "independence class is not recognized")
    if result not in INTEGRITY_RESULTS:
        raise Rejected("invalid_integrity", "integrity result is not recognized")
    for field in ("target_id", "claim", "source", "verifier"):
        if not isinstance(document.get(field), str) or not document[field].strip():
            raise Rejected("invalid_integrity", f"{field} required")
    refs = document.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(item, str) or not item.strip() for item in refs):
        raise Rejected("invalid_integrity", "evidence refs must be a list of strings")
    artifact = document.get("digest")
    if artifact is not None and (not isinstance(artifact, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact)):
        raise Rejected("invalid_integrity", "digest must be a sha256 hex string")
    if independence == "signed_external" and artifact is None:
        raise Rejected("integrity_digest_required", "signed external evidence needs a digest")
    return {
        "target_kind": target,
        "target_id": document["target_id"].strip(),
        "claim": document["claim"].strip(),
        "source": document["source"].strip(),
        "verifier": document["verifier"].strip(),
        "independence": independence,
        "evidence_refs": [item.strip() for item in refs],
        "result": result,
        "digest": artifact,
        "runtime_verified": False,
    }


def feedback_dimensions(records: list[dict]) -> dict:
    """Each kind and authority stays visible. There is no fitness total."""
    by_kind = {kind: [] for kind in FEEDBACK_KINDS}
    by_authority = {level: [] for level in AUTHORITIES}
    items = []
    for record in records:
        item = {
            "id": record["id"],
            "target_kind": record["target_kind"],
            "target_id": record["target_id"],
            "kind": record["kind"],
            "authority": record["authority"],
            "actor": record["actor"],
            "text": record["text"],
            "at": record["at"],
        }
        items.append(item)
        by_kind[record["kind"]].append(item)
        by_authority[record["authority"]].append(item)
    return {"records": items, "by_kind": by_kind, "by_authority": by_authority}


def integrity_projection(observations: list[dict], target: str | None = None) -> dict:
    rows = observations
    if target is not None:
        rows = [row for row in observations if row["target_id"] == target]
    return {
        "protocol": PROTOCOL,
        "kind": "IntegrityProjection",
        "authoritative": False,
        "target": target,
        "observations": rows,
        "by_class": {name: [row for row in rows if row["independence"] == name] for name in INTEGRITY_CLASSES},
        "self_report": [row for row in rows if row["independence"] == "internal"],
        "independent_evidence": [row for row in rows if row["independence"] in INDEPENDENT_CLASSES],
    }


def evaluate_selection(policy: dict, candidates: list[dict], feedback: list[dict], integrity: list[dict], actor_roles: list[str]) -> dict:
    """Say why each candidate advanced, stayed unresolved, or was rejected.

    An optional weight may add an aggregate. The lists above are the decision.
    ``promotes_git`` is always false. Promotion remains a SyberWork admission.
    """
    advanced, unresolved, rejected = [], [], []
    dimensions = {}
    for candidate in candidates:
        identifier = candidate["id"]
        notes = [item for item in feedback if item["target_kind"] == "candidate" and item["target_id"] == identifier]
        observations = [item for item in integrity if item["target_kind"] == "candidate" and item["target_id"] == identifier]
        reasons = []
        vetoes = [item for item in notes if item["authority"] == "veto" and item["kind"] in ("concern", "dissent", "critique")]
        dissents = [item for item in notes if item["authority"] == "decision" and item["kind"] == "dissent"]
        missing = [
            name for name in policy["required_integrity"]
            if not any(item["independence"] == name and item["result"] in PASSING_RESULTS for item in observations)
        ]
        preferences = [item for item in notes if item["kind"] == "preference"]
        decision_preferences = [item for item in preferences if item["authority"] == "decision"]
        consensus_actors = {item["actor"] for item in preferences if item["authority"] in ("advisory", "consensus", "decision")}
        consensus = len(consensus_actors) >= policy["consensus_threshold"]
        entry = {
            "feedback": feedback_dimensions(notes),
            "integrity": [
                {key: item[key] for key in ("id", "independence", "result", "claim", "verifier", "digest", "source")}
                for item in observations
            ],
            "reasons": [],
        }
        if policy.get("weights"):
            entry["aggregate"] = sum(policy["weights"].get(item["kind"], 0) for item in notes)
        if vetoes or dissents:
            entry["reasons"] = [f"veto:{item['actor']}" for item in vetoes] + [f"decision_dissent:{item['actor']}" for item in dissents]
            rejected.append({"id": identifier, "reasons": entry["reasons"]})
        elif missing:
            entry["reasons"] = [f"integrity_required:{name}" for name in missing]
            unresolved.append({"id": identifier, "reasons": entry["reasons"]})
        elif decision_preferences or consensus:
            if decision_preferences:
                reasons.append(f"advanced:decision:{decision_preferences[0]['actor']}")
            if consensus:
                reasons.append(f"advanced:consensus:{len(consensus_actors)}")
            entry["reasons"] = reasons
            advanced.append({"id": identifier, "reasons": reasons})
        else:
            entry["reasons"] = ["selection_unresolved"]
            unresolved.append({"id": identifier, "reasons": entry["reasons"]})
        dimensions[identifier] = entry
    return {
        "protocol": PROTOCOL,
        "advanced": advanced,
        "unresolved": unresolved,
        "rejected": rejected,
        "dimensions": dimensions,
        "promotes_git": False,
        "promotion_authorized": bool(set(policy["promotion_roles"]) & set(actor_roles)),
    }


def promotion_reference(selection: dict) -> dict:
    """Digest a later SyberWork proposal may cite. This does not admit or promote."""
    return {
        "protocol": PROTOCOL,
        "selection_id": selection["id"],
        "evidence_digest": selection["evidence_digest"],
        "promotes_git": False,
    }


def context_manifest(generation: dict, approach: dict, siblings: list[dict], implementations: list[dict] | None = None) -> dict:
    """Information an agent is permitted to receive. Isolation is this object, not a prompt."""
    isolation = generation["isolation"]
    if isolation not in ISOLATIONS:
        raise Rejected("invalid_isolation", "isolation policy is not recognized")
    manifest = {
        "protocol": PROTOCOL,
        "generation_id": generation["id"],
        "objective": generation["objective"],
        "base_revision": generation["base_revision"],
        "mode": generation["mode"],
        "isolation": isolation,
        "approach": {
            "id": approach["id"],
            "descriptor": approach["descriptor"],
        },
        "siblings": [],
        "implementations": [],
    }
    if isolation == "independent":
        return manifest
    manifest["siblings"] = [
        {"id": item["id"], "descriptor": item["descriptor"]}
        for item in siblings
        if item["id"] != approach["id"] and item.get("state") != "rejected"
    ]
    if isolation == "collaborative":
        manifest["implementations"] = [
            {
                "candidate_id": item["candidate_id"],
                "approach_id": item["approach_id"],
                "changed_paths": list(item.get("changed_paths", [])),
            }
            for item in (implementations or [])
            if item["approach_id"] != approach["id"]
        ]
    return manifest


def agent_authority(role: str) -> str:
    if role not in AGENT_AUTHORITY:
        raise Rejected("invalid_agent", "agent role is not recognized")
    return AGENT_AUTHORITY[role]


def normalize_activity(document: dict) -> dict:
    if not isinstance(document, dict):
        raise Rejected("invalid_activity", "activity must be an object")
    _refuse_hidden(document)
    activity = document.get("activity")
    if activity not in ACTIVITIES:
        raise Rejected("invalid_activity", "activity is not recognized")
    caused = document.get("caused_by", [])
    if not isinstance(caused, list) or any(not isinstance(item, str) or not item for item in caused):
        raise Rejected("invalid_activity", "caused_by must be a list of event ids")
    body = {"activity": activity, "caused_by": list(caused)}
    for field in ("artifact", "finding", "candidate_id", "target_agent"):
        if field in document and document[field] is not None:
            if not isinstance(document[field], str) or not document[field].strip():
                raise Rejected("invalid_activity", f"{field} must be a string")
            body[field] = document[field].strip()
    if activity == "edit_scope":
        paths = document.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(item, str) or not item for item in paths):
            raise Rejected("invalid_activity", "edit_scope needs paths")
        body["paths"] = list(paths)
    return body


def command_effect(agent: dict, command: str, body: dict) -> dict:
    """What a command would change if a runtime accepts it.

    ``send_context`` delivers information. ``redirect`` changes the assignment.
    Neither function mutates a process.
    """
    if command not in COMMANDS:
        raise Rejected("invalid_command", "command is not recognized")
    if not isinstance(body, dict):
        raise Rejected("invalid_command", "command body must be an object")
    _refuse_hidden(body)
    effect = {"command": command, "events": []}
    if command == "pause":
        if agent["state"] not in ("registered", "active"):
            raise Rejected("invalid_command", "agent cannot pause from this state")
        effect.update(state="paused", events=["agent_state_changed"])
    elif command == "resume":
        if agent["state"] != "paused":
            raise Rejected("invalid_command", "agent is not paused")
        effect.update(state="active", events=["agent_state_changed"])
    elif command == "cancel":
        if agent["state"] == "cancelled":
            raise Rejected("invalid_command", "agent is already cancelled")
        effect.update(state="cancelled", events=["agent_state_changed"])
    elif command == "send_context":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise Rejected("invalid_command", "send_context needs text")
        effect.update(activity="send_context", text=text.strip(), events=["agent_activity_recorded"])
    elif command == "restrict_scope":
        paths = body.get("paths")
        if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
            raise Rejected("invalid_command", "restrict_scope needs paths")
        effect.update(working_set=list(paths), events=["agent_state_changed"])
    elif command == "redirect":
        objective = body.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise Rejected("invalid_command", "redirect needs an objective")
        assignment = dict(agent["assignment"])
        assignment["objective"] = objective.strip()
        if body.get("approach_id"):
            if not isinstance(body["approach_id"], str):
                raise Rejected("invalid_command", "approach id must be a string")
            assignment["approach_id"] = body["approach_id"]
        effect.update(assignment=assignment, events=["agent_assignment_changed"])
    return effect


class AgentRuntime(Protocol):
    """A future agent runtime. This package does not attach one to a process."""

    def apply(self, command: dict) -> dict: ...


class DisconnectedRuntime:
    """Default adapter. The command is durable; nothing is signaled."""

    def apply(self, command: dict) -> dict:
        return {"applied": False, "reason": "runtime_not_connected"}


def validate_snapshot(document: dict) -> dict:
    """Graph AST. Mermaid is not a stored architecture."""
    if not isinstance(document, dict):
        raise Rejected("invalid_architecture", "architecture snapshot must be an object")
    if "mermaid" in document:
        raise Rejected("invalid_architecture", "architecture is stored as a graph, not Mermaid")
    for field in ("repository", "revision", "provider"):
        if not isinstance(document.get(field), str) or not document[field].strip():
            raise Rejected("invalid_architecture", f"{field} required")
    groups = document.get("groups", [])
    nodes = document.get("nodes")
    edges = document.get("edges")
    if not isinstance(groups, list) or not isinstance(nodes, list) or not isinstance(edges, list):
        raise Rejected("invalid_architecture", "groups, nodes, and edges must be lists")
    if not nodes:
        raise Rejected("invalid_architecture", "architecture needs at least one node")
    cleaned_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not isinstance(group.get("label"), str):
            raise Rejected("invalid_architecture", "group needs an id and a label")
        cleaned_groups.append({"id": group["id"], "label": group["label"]})
    cleaned_nodes = []
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or "mermaid" in node:
            raise Rejected("invalid_architecture", "node must be an object")
        if not isinstance(node.get("id"), str) or not node["id"] or not isinstance(node.get("label"), str) or not node["label"]:
            raise Rejected("invalid_architecture", "node needs an id and a label")
        paths = node.get("paths")
        if not isinstance(paths, list) or not paths or any(not isinstance(item, str) or not item.strip() for item in paths):
            raise Rejected("invalid_architecture", "node needs one or more repository paths")
        if node["id"] in ids:
            raise Rejected("invalid_architecture", "node ids must be unique")
        ids.add(node["id"])
        cleaned = {"id": node["id"], "label": node["label"], "paths": [item.strip() for item in paths]}
        if "group" in node:
            if not isinstance(node["group"], str):
                raise Rejected("invalid_architecture", "node group must be a string")
            cleaned["group"] = node["group"]
        cleaned_nodes.append(cleaned)
    cleaned_edges = []
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("from") not in ids or edge.get("to") not in ids:
            raise Rejected("invalid_architecture", "edge must connect known nodes")
        cleaned_edges.append({"from": edge["from"], "to": edge["to"]})
    identifier = document.get("id")
    if identifier is not None and (not isinstance(identifier, str) or not identifier.strip()):
        raise Rejected("invalid_architecture", "snapshot id must be a string")
    return {
        "protocol": PROTOCOL,
        "id": identifier.strip() if isinstance(identifier, str) else None,
        "repository": document["repository"].strip(),
        "revision": document["revision"].strip(),
        "provider": document["provider"].strip(),
        "baseline_id": document.get("baseline_id") if isinstance(document.get("baseline_id"), str) else None,
        "groups": cleaned_groups,
        "nodes": cleaned_nodes,
        "edges": cleaned_edges,
    }


def _path_hit(node_path: str, changed: str) -> bool:
    left = node_path.strip("/")
    right = changed.strip("/")
    if not left or not right:
        return False
    return left == right or right.startswith(left + "/") or left.startswith(right + "/")


def nodes_for_paths(snapshot: dict, paths: list[str]) -> list[dict]:
    """Map changed paths or an agent working set onto architecture nodes."""
    if not isinstance(paths, list):
        raise Rejected("invalid_architecture", "paths must be a list")
    matched = []
    for node in snapshot["nodes"]:
        if any(_path_hit(node_path, changed) for node_path in node["paths"] for changed in paths if isinstance(changed, str)):
            matched.append({"id": node["id"], "label": node["label"], "paths": list(node["paths"])})
    return matched


def architecture_diff(baseline: dict, candidate: dict) -> dict:
    base_nodes = {node["id"]: node for node in baseline["nodes"]}
    cand_nodes = {node["id"]: node for node in candidate["nodes"]}
    base_edges = {(edge["from"], edge["to"]) for edge in baseline["edges"]}
    cand_edges = {(edge["from"], edge["to"]) for edge in candidate["edges"]}
    changed = [
        {"id": identifier, "baseline": base_nodes[identifier], "candidate": cand_nodes[identifier]}
        for identifier in base_nodes.keys() & cand_nodes.keys()
        if base_nodes[identifier] != cand_nodes[identifier]
    ]
    return {
        "protocol": PROTOCOL,
        "kind": "ArchitectureDiff",
        "authoritative": False,
        "baseline_id": baseline.get("id"),
        "candidate_id": candidate.get("id"),
        "added_nodes": [cand_nodes[identifier] for identifier in cand_nodes.keys() - base_nodes.keys()],
        "removed_nodes": [base_nodes[identifier] for identifier in base_nodes.keys() - cand_nodes.keys()],
        "changed_nodes": changed,
        "added_edges": [{"from": pair[0], "to": pair[1]} for pair in sorted(cand_edges - base_edges)],
        "removed_edges": [{"from": pair[0], "to": pair[1]} for pair in sorted(base_edges - cand_edges)],
    }


class ArchitectureProvider(Protocol):
    """Builds a snapshot document. A future GitDiagramProvider can implement this.

    This module does not import a diagram vendor.
    """

    name: str

    def build(self, repository: str, revision: str) -> dict: ...


class StaticArchitectureProvider:
    def __init__(self, document: dict):
        self.name = "static"
        self.document = document

    def build(self, repository: str, revision: str) -> dict:
        document = dict(self.document)
        document["repository"] = repository
        document["revision"] = revision
        document["provider"] = self.name
        return validate_snapshot(document)


class PrototypeProvider(Protocol):
    """Fills a launch reference. Reading a prototype does not call this."""

    name: str

    def describe(self, record: dict) -> dict: ...


class RegistryPrototypeProvider:
    name = "registry"

    def describe(self, record: dict) -> dict:
        return {
            "endpoint": record.get("endpoint"),
            "state": record.get("state", "building"),
            "provider": record.get("provider", self.name),
        }


def normalize_prototype(document: dict) -> dict:
    if not isinstance(document, dict):
        raise Rejected("invalid_prototype", "prototype must be an object")
    for field in ("candidate_id", "artifact_ref", "environment", "provider"):
        if not isinstance(document.get(field), str) or not document[field].strip():
            raise Rejected("invalid_prototype", f"{field} required")
    state = document.get("state", "building")
    if state not in PROTOTYPE_STATES:
        raise Rejected("invalid_prototype", "prototype state is not recognized")
    endpoint = document.get("endpoint")
    if endpoint is not None and (not isinstance(endpoint, str) or not endpoint.strip()):
        raise Rejected("invalid_prototype", "endpoint must be a string")
    artifact = document.get("digest")
    if artifact is not None and (not isinstance(artifact, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact)):
        raise Rejected("invalid_prototype", "digest must be a sha256 hex string")
    expires = document.get("expires_at")
    if expires is not None and type(expires) is not int:
        raise Rejected("invalid_prototype", "expires_at must be integer microseconds")
    return {
        "candidate_id": document["candidate_id"].strip(),
        "artifact_ref": document["artifact_ref"].strip(),
        "environment": document["environment"].strip(),
        "provider": document["provider"].strip(),
        "endpoint": endpoint.strip() if isinstance(endpoint, str) else None,
        "state": state,
        "digest": artifact,
        "expires_at": expires,
    }


def project(view: dict, principal: dict) -> dict:
    """Role lens. The stored records stay shared; this copy is filtered."""
    roles = set(principal.get("roles") or [])
    framed = copy.deepcopy(view)
    framed["lens"] = sorted(roles)
    framed["authoritative"] = False
    hide = "intended_user" in roles and not roles.intersection({"engineer", "manager", "decision", "admin"})
    if hide:
        framed["redacted"] = ["hypothesis_summary", "open_uncertainties"]
        _redact(framed)
    return framed


def _redact(value) -> None:
    if isinstance(value, dict):
        value.pop("hypothesis_summary", None)
        value.pop("open_uncertainties", None)
        for item in value.values():
            _redact(item)
    elif isinstance(value, list):
        for item in value:
            _redact(item)


def visible_event(event: dict, roles: list[str] | set[str]) -> bool:
    """Stream filter. Omitted events keep their sequence numbers, so ids may gap."""
    role_set = set(roles or [])
    hide = "intended_user" in role_set and not role_set.intersection({"engineer", "manager", "decision", "admin"})
    if hide and event["kind"] == "agent_activity_recorded":
        return False
    return True


def generation_summary(generation: dict, approach_count: int) -> dict:
    return {
        "id": generation["id"],
        "ordinal": generation["ordinal"],
        "state": generation["state"],
        "mode": generation["mode"],
        "isolation": generation["isolation"],
        "objective": generation["objective"],
        "approach_count": approach_count,
    }
