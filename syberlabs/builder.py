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
RUNNING_STATES = ("launched", "evaluating", "selecting")
LINKABLE_STATES = ("sealed", *RUNNING_STATES)
COORDINATION_ROLES = frozenset({"admin", "operator", "engineer", "manager", "decision"})

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
AGENT_AUTHORITY = {
    "implementer": "informative",
    "reviewer": "advisory",
    "operator": "informative",
    "search": "informative",
}
PROTOTYPE_STATES = ("building", "ready", "failed", "expired")
PROTOTYPE_TRANSITIONS = {
    "building": frozenset({"ready", "failed"}),
    "ready": frozenset({"expired", "failed"}),
    "failed": frozenset(),
    "expired": frozenset(),
}
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
    """Jaccard distance on structural tokens. 0 is identical. 1 is disjoint."""
    a, b = _tokens(left), _tokens(right)
    if not a and not b:
        return 0.0
    return round(1.0 - (len(a & b) / len(a | b)), 6)


def diversity_evidence(descriptor: dict, siblings: list[dict], threshold: float) -> dict:
    """Pairwise structural evidence against approaches already accepted in the generation."""
    pairs = []
    failure = None
    inputs = {
        "descriptor_digest": digest(descriptor),
        "threshold": float(threshold),
        "siblings": [{"id": sibling["id"], "descriptor_digest": digest(sibling["descriptor"])} for sibling in siblings],
    }
    for sibling in siblings:
        distance = structural_distance(descriptor, sibling["descriptor"])
        claims_equal = set(descriptor["distinguishing_claims"]) == set(sibling["descriptor"]["distinguishing_claims"])
        passed = (not claims_equal) and distance >= float(threshold)
        pairs.append({
            "sibling_id": sibling["id"],
            "structural_distance": distance,
            "claims_equal": claims_equal,
            "passed": passed,
        })
        if not passed and failure is None:
            failure = sibling["id"]
    return {
        "passed": failure is None,
        "sibling_id": failure,
        "evidence": {"method": "structural_jaccard", "authoritative": "structural", "inputs": inputs, "pairs": pairs},
    }


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
    if "stakeholder_authority" in document:
        raise Rejected("invalid_policy", "name grants per role and feedback kind; authority is not one rank")
    required = document.get("required_integrity", [])
    if not isinstance(required, list) or any(item not in INTEGRITY_CLASSES for item in required):
        raise Rejected("invalid_policy", "required integrity classes are not recognized")
    raw_grants = document.get("grants")
    if not isinstance(raw_grants, list) or not raw_grants:
        raise Rejected("invalid_policy", "selection policy needs grants")
    grants = []
    for grant in raw_grants:
        if not isinstance(grant, dict):
            raise Rejected("invalid_policy", "each grant is an object")
        role, kind, level = grant.get("role"), grant.get("kind"), grant.get("authority")
        if not isinstance(role, str) or not role.strip() or kind not in FEEDBACK_KINDS or level not in AUTHORITIES:
            raise Rejected("invalid_policy", "a grant names a role, a feedback kind, and an authority class")
        item = {"role": role.strip(), "kind": kind, "authority": level}
        if item not in grants:
            grants.append(item)
    raw_consensus = document.get("consensus_authorities", ["consensus"])
    if not isinstance(raw_consensus, list) or not raw_consensus or any(item not in AUTHORITIES for item in raw_consensus):
        raise Rejected("invalid_policy", "consensus_authorities must list authority classes")
    consensus_authorities = list(dict.fromkeys(raw_consensus))
    threshold = document.get("consensus_threshold", 1)
    if type(threshold) is not int or threshold < 1:
        raise Rejected("invalid_policy", "consensus threshold must be a positive integer")
    promotion = document.get("promotion_roles", [])
    if not isinstance(promotion, list) or any(not isinstance(item, str) or not item for item in promotion):
        raise Rejected("invalid_policy", "promotion roles must be a list of strings")
    return {
        "protocol": PROTOCOL,
        "id": identifier.strip(),
        "version": version,
        "required_integrity": list(required),
        "grants": grants,
        "consensus_authorities": consensus_authorities,
        "consensus_threshold": threshold,
        "promotion_roles": list(promotion),
    }


def normalize_generation(document: dict) -> dict:
    if not isinstance(document, dict):
        raise Rejected("invalid_generation", "generation must be an object")
    case_id = document.get("case_id")
    objective = document.get("objective")
    revision = document.get("base_revision")
    threshold = document.get("diversity_threshold", 0.3)
    minimum = document.get("min_approaches", 2)
    policy_id = document.get("selection_policy_id")
    policy_version = document.get("selection_policy_version")
    if not isinstance(case_id, str) or not isinstance(objective, str) or not objective.strip():
        raise Rejected("invalid_generation", "case and objective required")
    if not isinstance(revision, str) or not revision.strip() or len(revision) > 200:
        raise Rejected("invalid_generation", "base revision required")
    if document.get("mode") not in MODES:
        raise Rejected("invalid_mode", "mode must be explore, refine, or harden")
    if document.get("isolation") not in ISOLATIONS:
        raise Rejected("invalid_isolation", "isolation must be independent, aware, or collaborative")
    if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
        raise Rejected("invalid_generation", "diversity threshold must be between 0 and 1")
    if type(minimum) is not int or minimum < 1:
        raise Rejected("invalid_generation", "min_approaches must be a positive integer")
    if not isinstance(policy_id, str) or type(policy_version) is not int:
        raise Rejected("invalid_generation", "selection policy reference required")
    return {
        "case_id": case_id,
        "objective": objective.strip(),
        "base_revision": revision.strip(),
        "mode": document["mode"],
        "isolation": document["isolation"],
        "diversity_threshold": float(threshold),
        "min_approaches": minimum,
        "selection_policy_id": policy_id,
        "selection_policy_version": policy_version,
    }


_WORLD_FIELDS = frozenset({
    "provider",
    "services",
    "seed_ref",
    "seed_digest",
    "environment_snapshot",
    "network_policy",
    "time_policy",
    "entropy_policy",
    "reproducibility",
    "parent_world_digest",
    "intent",
    "digest",
})
_WORLD_INTENTS = frozenset({"comparison", "mutated", "adversarial", "historical", "customer"})
_PHASE1_REPRODUCIBILITY = frozenset({"unknown", "externally_mutable"})
_SECRET_KEYS = frozenset({"token", "secret", "authorization", "password", "api_key", "credential", "auth_env", "bearer"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _optional_sha(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise Rejected("invalid_world", f"{field} must be a sha256 hex string or null")
    return value


def _refuse_secret(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                raise Rejected("world_secret", "a world definition cannot carry a credential")
            _refuse_secret(item)
    elif isinstance(value, list):
        for item in value:
            _refuse_secret(item)


def _name_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise Rejected("invalid_world", f"{field} must be a list of strings")
    return sorted({item.strip() for item in value})


def normalize_world_definition(document: dict) -> dict:
    """Canonical world definition. The digest is not part of this object.

    Explicit nulls stay in the result so two documents that differ by an
    absent snapshot do not hash as the same world. ``deterministic`` and
    ``snapshot_replayable`` are refused until a host has observed them.
    """
    if not isinstance(document, dict):
        raise Rejected("invalid_world", "world definition must be an object")
    _refuse_secret(document)
    if set(document) - _WORLD_FIELDS:
        raise Rejected("invalid_world", "unknown world field")
    provider = document.get("provider")
    if not isinstance(provider, dict) or set(provider) != {"name", "revision"}:
        raise Rejected("invalid_world", "provider needs a name and a revision")
    name, revision = provider["name"], provider["revision"]
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise Rejected("invalid_world", "provider name required")
    if not isinstance(revision, str) or not revision.strip() or len(revision) > 200:
        raise Rejected("invalid_world", "provider revision required")
    services = document.get("services")
    if not isinstance(services, list):
        raise Rejected("invalid_world", "services must be a list")
    cleaned_services = []
    seen = set()
    for service in services:
        if not isinstance(service, dict) or set(service) != {"name", "mode"}:
            raise Rejected("invalid_world", "service needs a name and a mode")
        if service["mode"] not in ("simulated", "real"):
            raise Rejected("invalid_world", "service mode must be simulated or real")
        if not isinstance(service["name"], str) or not service["name"].strip() or len(service["name"]) > 64:
            raise Rejected("invalid_world", "service name required")
        service_name = service["name"].strip()
        if service_name in seen:
            raise Rejected("invalid_world", "service names must be unique")
        seen.add(service_name)
        cleaned_services.append({"name": service_name, "mode": service["mode"]})
    snapshot = document.get("environment_snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {"snapshot_ref", "snapshot_digest"}:
        raise Rejected("invalid_world", "environment_snapshot needs a ref and a digest")
    ref = snapshot.get("snapshot_ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip() or len(ref) > 200):
        raise Rejected("invalid_world", "snapshot_ref must be a string or null")
    seed_ref = document.get("seed_ref")
    if seed_ref is not None and (not isinstance(seed_ref, str) or not seed_ref.strip() or len(seed_ref) > 200):
        raise Rejected("invalid_world", "seed_ref must be a string or null")
    policy = document.get("network_policy")
    if not isinstance(policy, dict) or set(policy) != {"allowed", "denied", "record_denied"}:
        raise Rejected("invalid_world", "network_policy needs allowed, denied, and record_denied")
    if type(policy.get("record_denied")) is not bool:
        raise Rejected("invalid_world", "record_denied must be a boolean")
    clock = document.get("time_policy")
    if not isinstance(clock, dict) or set(clock) != {"mode", "epoch", "timezone"}:
        raise Rejected("invalid_world", "time_policy needs mode, epoch, and timezone")
    if clock.get("mode") != "fixed":
        raise Rejected("invalid_world", "time_policy mode must be fixed")
    if type(clock.get("epoch")) is not int or clock["epoch"] < 0:
        raise Rejected("invalid_world", "epoch must be a non-negative integer")
    timezone = clock.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip() or len(timezone) > 64:
        raise Rejected("invalid_world", "timezone required")
    entropy = document.get("entropy_policy")
    if not isinstance(entropy, dict) or set(entropy) != {"mode", "seed_digest"}:
        raise Rejected("invalid_world", "entropy_policy needs mode and seed_digest")
    if entropy.get("mode") not in ("none", "fixed"):
        raise Rejected("invalid_world", "entropy mode must be none or fixed")
    entropy_digest = _optional_sha(entropy.get("seed_digest"), "seed_digest")
    if entropy["mode"] == "none" and entropy_digest is not None:
        raise Rejected("invalid_world", "entropy mode none has no seed")
    if entropy["mode"] == "fixed" and entropy_digest is None:
        raise Rejected("invalid_world", "fixed entropy needs a seed digest")
    reproducibility = document.get("reproducibility")
    if reproducibility not in ("unknown", "externally_mutable", "deterministic", "snapshot_replayable"):
        raise Rejected("invalid_world", "reproducibility is not recognized")
    if reproducibility not in _PHASE1_REPRODUCIBILITY:
        raise Rejected("world_reproducibility", "that reproducibility class is not established yet")
    intent = document.get("intent")
    if intent not in _WORLD_INTENTS:
        raise Rejected("invalid_world", "intent is not recognized")
    return {
        "provider": {"name": name.strip(), "revision": revision.strip()},
        "services": sorted(cleaned_services, key=lambda item: item["name"]),
        "seed_ref": None if seed_ref is None else seed_ref.strip(),
        "seed_digest": _optional_sha(document.get("seed_digest"), "seed_digest"),
        "environment_snapshot": {
            "snapshot_ref": None if ref is None else ref.strip(),
            "snapshot_digest": _optional_sha(snapshot.get("snapshot_digest"), "snapshot_digest"),
        },
        "network_policy": {
            "allowed": _name_list(policy.get("allowed"), "allowed"),
            "denied": _name_list(policy.get("denied"), "denied"),
            "record_denied": policy["record_denied"],
        },
        "time_policy": {"mode": "fixed", "epoch": clock["epoch"], "timezone": timezone.strip()},
        "entropy_policy": {"mode": entropy["mode"], "seed_digest": entropy_digest},
        "reproducibility": reproducibility,
        "parent_world_digest": _optional_sha(document.get("parent_world_digest"), "parent_world_digest"),
        "intent": intent,
    }


def world_definition(document: dict) -> dict:
    """Definition plus the digest of its canonical body. A supplied digest must match."""
    claimed = document.get("digest") if isinstance(document, dict) else None
    normalized = normalize_world_definition(document)
    artifact = digest(normalized)
    if claimed is not None and claimed != artifact:
        raise Rejected("world_digest_mismatch", "world digest does not match the definition")
    return {**normalized, "digest": artifact}


def instance_equivalence(reproducibility: str) -> str:
    """Phase 1 never returns ``verified``. A live service cannot share instance state."""
    if reproducibility == "externally_mutable":
        return "impossible"
    return "unverified"


def world_definition_matches(world_digest: str, candidate_ids: list[str], bindings: list[dict]) -> bool:
    """True only when every linked candidate has a binding to this definition."""
    if not candidate_ids:
        return False
    by_candidate = {item["candidate_id"]: item["world_digest"] for item in bindings}
    return all(by_candidate.get(identifier) == world_digest for identifier in candidate_ids)


def normalize_agent(document: dict) -> dict:
    if not isinstance(document, dict):
        raise Rejected("invalid_agent", "agent session must be an object")
    _refuse_hidden(document)
    generation_id = document.get("generation_id")
    principal = document.get("principal")
    role = document.get("role")
    if not isinstance(generation_id, str) or not isinstance(principal, str) or not principal.strip():
        raise Rejected("invalid_agent", "generation and principal required")
    if not isinstance(role, str):
        raise Rejected("invalid_agent", "agent role is not recognized")
    hypothesis = document.get("hypothesis_summary", "")
    if not isinstance(hypothesis, str) or len(hypothesis) > 2000:
        raise Rejected("invalid_agent", "hypothesis summary must be a short string")
    lists = {}
    for name in ("open_uncertainties", "evidence_refs", "working_set"):
        value = document.get(name, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise Rejected("invalid_agent", f"{name} must be a list of strings")
        lists[name] = list(value)
    approach_id = document.get("approach_id")
    if approach_id is not None and not isinstance(approach_id, str):
        raise Rejected("invalid_agent", "approach id must be a string")
    return {
        "generation_id": generation_id,
        "approach_id": approach_id,
        "principal": principal.strip(),
        "role": role,
        "authority": agent_authority(role),
        "hypothesis_summary": hypothesis,
        **lists,
    }


def derive_authorities(roles: list[str], kind: str, policy: dict) -> list[str]:
    """Every class this feedback kind grants. Roles do not collapse into one rank."""
    found = []
    for grant in policy["grants"]:
        if grant["role"] in roles and grant["kind"] == kind and grant["authority"] not in found:
            found.append(grant["authority"])
    if not found:
        raise Rejected("feedback_authority", "participant role has no configured authority for this feedback")
    return found


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
    if document.get("authority") is not None or document.get("authorities") is not None:
        raise Rejected("invalid_feedback", "authority is derived from the policy grants, not supplied")
    return {
        "target_kind": target,
        "target_id": identifier.strip(),
        "kind": kind,
        "text": text.strip(),
        "authorities": derive_authorities(roles, kind, policy),
    }


def normalize_integrity(document: dict) -> dict:
    """The caller's claim. This does not decide whether the class was verified."""
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
        "independence_claim": independence,
        "evidence_refs": [item.strip() for item in refs],
        "result": result,
        "digest": artifact,
    }


class IntegrityVerifier(Protocol):
    """Checks external or signed evidence. The HTTP body cannot implement this."""

    name: str

    def verify(self, observation: dict) -> dict: ...


def assess_integrity(observation: dict, *, principal_kind: str | None = None, host: bool = False, verifier: IntegrityVerifier | None = None) -> dict:
    """Separate the claimed class from the class a mechanism actually established.

    Selection reads ``verified_independence`` only. A digest, by itself, verifies nothing.
    """
    claim = observation["independence_claim"]
    status, method, verified = "unverified", None, None
    if host:
        if claim != "host_verified":
            raise Rejected("integrity_verifier", "the host mechanism records host_verified observations")
        status, method, verified = "verified", "host_mechanism", "host_verified"
    elif claim == "internal":
        status, method, verified = "verified", "self_report", "internal"
    elif claim == "human_reviewed" and principal_kind == "human":
        status, method, verified = "verified", "authenticated_human", "human_reviewed"
    elif claim in ("external", "signed_external") and verifier is not None:
        outcome = verifier.verify(observation)
        if not isinstance(outcome, dict) or outcome.get("status") not in ("verified", "failed"):
            raise Rejected("integrity_verifier", "verifier result is not recognized")
        if outcome.get("independence") != claim:
            raise Rejected("integrity_verifier", "a verifier cannot upgrade or relabel the claimed class")
        method = outcome.get("method") if isinstance(outcome.get("method"), str) and outcome["method"] else verifier.name
        if outcome["status"] == "verified":
            status, verified = "verified", claim
        else:
            status = "failed"
    return {
        **observation,
        "verification_status": status,
        "verification_method": method,
        "verified_independence": verified,
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
            "authorities": list(record["authorities"]),
            "actor": record["actor"],
            "text": record["text"],
            "at": record["at"],
        }
        items.append(item)
        by_kind[record["kind"]].append(item)
        for level in record["authorities"]:
            by_authority[level].append(item)
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
        "self_report": [row for row in rows if row.get("verified_independence") == "internal"],
        "independent_evidence": [row for row in rows if row.get("verified_independence") in INDEPENDENT_CLASSES],
        "unverified_claims": [row for row in rows if row.get("verification_status") != "verified"],
    }


def _integrity_dimension(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "id",
            "independence_claim",
            "verification_status",
            "verification_method",
            "verified_independence",
            "result",
            "claim",
            "verifier",
            "digest",
            "source",
        )
    }


def _verdict(policy: dict, notes: list[dict], observations: list[dict]) -> tuple[str, list[str]]:
    """Veto and dissent beat missing evidence, which beats advancement."""
    blockers = [
        f"veto:{item['actor']}" for item in notes
        if "veto" in item["authorities"] and item["kind"] in ("concern", "dissent", "critique")
    ] + [
        f"decision_dissent:{item['actor']}" for item in notes
        if "decision" in item["authorities"] and item["kind"] == "dissent"
    ]
    if blockers:
        return "rejected", blockers
    missing = [
        f"integrity_required:{name}" for name in policy["required_integrity"]
        if not any(
            item.get("verification_status") == "verified"
            and item.get("verified_independence") == name
            and item["result"] in PASSING_RESULTS
            for item in observations
        )
    ]
    if missing:
        return "unresolved", missing
    preferences = [item for item in notes if item["kind"] == "preference"]
    decided = [item for item in preferences if "decision" in item["authorities"]]
    levels = set(policy["consensus_authorities"])
    consensus = {item["actor"] for item in preferences if levels.intersection(item["authorities"])}
    reasons = []
    if decided:
        reasons.append(f"advanced:decision:{decided[0]['actor']}")
    if len(consensus) >= policy["consensus_threshold"]:
        reasons.append(f"advanced:consensus:{len(consensus)}")
    if reasons:
        return "advanced", reasons
    return "unresolved", ["selection_unresolved"]


def evaluate_selection(policy: dict, candidate_ids: list[str], feedback: list[dict], integrity: list[dict], actor_roles: list[str]) -> dict:
    """Say why each linked candidate advanced, stayed unresolved, or was rejected.

    ``promotes_git`` is always false. Promotion remains a SyberWork admission.
    """
    buckets = {"advanced": [], "unresolved": [], "rejected": []}
    dimensions = {}
    for identifier in candidate_ids:
        notes = [item for item in feedback if item["target_kind"] == "candidate" and item["target_id"] == identifier]
        observations = [item for item in integrity if item["target_kind"] == "candidate" and item["target_id"] == identifier]
        bucket, reasons = _verdict(policy, notes, observations)
        dimensions[identifier] = {
            "feedback": feedback_dimensions(notes),
            "integrity": [_integrity_dimension(item) for item in observations],
            "reasons": reasons,
        }
        buckets[bucket].append({"id": identifier, "reasons": reasons})
    return {
        "protocol": PROTOCOL,
        "advanced": buckets["advanced"],
        "unresolved": buckets["unresolved"],
        "rejected": buckets["rejected"],
        "dimensions": dimensions,
        "promotes_git": False,
        "promotion_authorized": bool(set(policy["promotion_roles"]) & set(actor_roles)),
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
        effect.update(state="paused", events=[("agent_state_changed", {"state": "paused"})])
    elif command == "resume":
        if agent["state"] != "paused":
            raise Rejected("invalid_command", "agent is not paused")
        effect.update(state="active", events=[("agent_state_changed", {"state": "active"})])
    elif command == "cancel":
        if agent["state"] == "cancelled":
            raise Rejected("invalid_command", "agent is already cancelled")
        effect.update(state="cancelled", events=[("agent_state_changed", {"state": "cancelled"})])
    elif command == "send_context":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise Rejected("invalid_command", "send_context needs text")
        effect.update(events=[("agent_activity_recorded", {"activity": "send_context", "text": text.strip(), "caused_by": []})])
    elif command == "restrict_scope":
        paths = body.get("paths")
        if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
            raise Rejected("invalid_command", "restrict_scope needs paths")
        effect.update(working_set=list(paths), events=[("agent_state_changed", {"state": agent["state"], "working_set": list(paths)})])
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
        effect.update(assignment=assignment, events=[("agent_assignment_changed", {"assignment": assignment})])
    return effect


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
        for identifier in sorted(base_nodes.keys() & cand_nodes.keys())
        if base_nodes[identifier] != cand_nodes[identifier]
    ]
    return {
        "protocol": PROTOCOL,
        "kind": "ArchitectureDiff",
        "authoritative": False,
        "baseline_id": baseline.get("id"),
        "candidate_id": candidate.get("id"),
        "added_nodes": [cand_nodes[identifier] for identifier in sorted(cand_nodes.keys() - base_nodes.keys())],
        "removed_nodes": [base_nodes[identifier] for identifier in sorted(base_nodes.keys() - cand_nodes.keys())],
        "changed_nodes": changed,
        "added_edges": [{"from": pair[0], "to": pair[1]} for pair in sorted(cand_edges - base_edges)],
        "removed_edges": [{"from": pair[0], "to": pair[1]} for pair in sorted(base_edges - cand_edges)],
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


_UNREDACTED_ROLES = {"engineer", "manager", "decision", "admin"}


def redacts(roles) -> bool:
    """The intended_user lens hides agent reasoning summaries and agent activity."""
    roles = set(roles or [])
    return "intended_user" in roles and not roles & _UNREDACTED_ROLES


def project(view: dict, principal: dict) -> dict:
    """Role lens. The stored records stay shared; this copy is filtered."""
    roles = principal.get("roles") or []
    framed = copy.deepcopy(view)
    framed["lens"] = sorted(set(roles))
    framed["authoritative"] = False
    if redacts(roles):
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
    return not (redacts(roles) and event["kind"] == "agent_activity_recorded")


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
