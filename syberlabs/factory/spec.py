"""Specs and tickets: a versioned set of requirements that claims a footprint of cells.

A spec is one capability's requirements, each with a stable clause id, EARS-style text, scenarios, and the
evidence kinds its tickets must produce. Its ``footprint_hint`` names components; ``resolve_footprint``
checks the hint against a Space graph at a commit and returns the real footprint, so a spec claims cells
that exist. States run draft, revised, mutated, candidate, promoted, canonical, retired; every operation is
a ledger event with actor, model and an input digest, never an in-place edit of history.

A ticket is a delta over a footprint between two commits. ``ticket_from_spec`` opens one per requirement of
a promoted spec with the footprint resolved, the acceptance criteria copied from the scenarios, and one exit
criterion per evidence kind. Agents are delegated to; a human stays the owner.

The goal gate. The factory may propose a spec at any level; it may promote none. ``promote``, ``canonize``
and ``retire`` (``GATED``) take a human actor and an approval that verifies: a DSSE envelope the owner signed
over exactly this spec (its content digest before the operation), this operation and, for a promotion, the
verdict ids it rests on, each resolved on the ledger to a ``review`` event that accepted this very variant.
The approver may not be a proposer or the reviewer of that variant. ``transition`` accepts only the
``Approved`` record :func:`verify_approval` returns, which nothing outside this module can construct, so a
caller cannot hand it a dictionary of its own.

The owner's public key is pinned on the ledger, not supplied by the caller: an ``owner_key_set`` event names
it, the first one is the trust root of that ledger (bootstrap, written once), and a later one is a rotation
the previous key signed (:func:`verify_rotation`). The pin is read through its lineage, never from the last
event alone: :func:`owner_key_lineage` walks every ``owner_key_set`` in order and verifies each handover under
the key pinned before it, so an appended pin, a rotation the pinned key did not sign, a stale ``previous`` or
a malformed key invalidates the lineage instead of re-rooting the gate (``owner_key_lineage``). :func:`gate`
verifies every approval under the lineage's key only; a ledger with no pinned key refuses (``owner_key_unset``),
an invalid lineage refuses (``owner_key_lineage``, never falling back to an earlier key) and an approval under
any other key refuses (``owner_key_mismatch``), whoever runs the host command. The signing seed never enters
this module. A kernel ``approved`` event on a goal case is the intended source of this approval once
the factory has a case to hang it on; until then the signature stands in, and ``spec/GAPS.md`` records the
gap.

v0 implements the lifecycle a single spec needs: draft, revise, validate (to candidate), promote, canonize,
retire, and the variant loop (several models proposing variants of one parent, one independent reviewer).
"""

from __future__ import annotations

import json
import re
import time

from syberlabs.canonical import canonical, digest
from syberlabs.dsse import sign_dsse, verified_payload
from syberlabs.errors import Rejected
from syberlabs.factory import PROTOCOL
from syberlabs.factory.independence import family, independence, model_of

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
GATED = frozenset({"promote", "canonize", "retire"})
ACTOR_KINDS = ("human", "model", "compiled")
APPROVAL_PROTOCOL = "syberlabs.space/factory/approval/v0"
APPROVAL_PAYLOAD_TYPE = "application/vnd.syberlabs.factory-approval+json"
OWNER_KEY_PROTOCOL = "syberlabs.space/factory/owner-key/v0"
OWNER_KEY_PAYLOAD_TYPE = "application/vnd.syberlabs.factory-owner-key+json"
_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")
# The module-private token ``Approved`` demands: only :func:`verify_approval` holds it.
_ISSUED = object()


def principal(text: str) -> dict:
    """``kind:id`` as a principal: ``kind`` is human, model or compiled and ``id`` is one token of at most 64
    characters. Free text is refused, so an actor on the ledger is always a resolvable identity."""
    kind, sep, actor_id = (text or "").partition(":")
    if not sep or kind not in ACTOR_KINDS or not _PRINCIPAL_ID.match(actor_id):
        raise Rejected("actor_invalid", f"an actor is kind:id with kind in {ACTOR_KINDS} and a one-token id, not {text!r}")
    return {"kind": kind, "id": actor_id}


class Approved(dict):
    """A verified approval: what :func:`verify_approval` returns and the only thing ``transition`` takes for a
    gated operation. The constructor demands a module-private token, so ``Approved({...})`` anywhere else
    raises ``approval_forged`` instead of producing something the gate would accept; ``transition`` checks
    the token again, so an instance made around the constructor is refused too."""

    __slots__ = ("_issued",)

    def __init__(self, payload: dict, *, _token: object = None):
        if _token is not _ISSUED:
            raise Rejected("approval_forged", "an Approved is made by verify_approval only; a verified approval is not a dictionary a caller writes")
        super().__init__(payload)
        self._issued = _ISSUED


def _is_issued(approval: object) -> bool:
    return isinstance(approval, Approved) and getattr(approval, "_issued", None) is _ISSUED


def owner_key_lineage(events: list[dict], load_rotation=None) -> tuple[str | None, list[str]]:
    """Walk every ``owner_key_set`` in ``events`` in order and return ``(pinned key, violations)``: the key the
    verified lineage ends on (None when the ledger pins none) and every way the lineage fails. The first event
    is the one unsigned bootstrap: ``previous`` None, no ``rotation_digest``. Every later one is a rotation:
    ``previous`` is the key pinned right before it, ``rotation_digest`` names a stored envelope
    (``load_rotation(digest)``, the ledger's ``approvals/``; None when missing or altered) that
    :func:`verify_rotation` accepts under that previous key, and every ``keyid`` is a 64-hex public key. A
    second bare bootstrap, a rotation signed by the new key, a stale ``previous`` and a malformed key are each
    a violation. With violations the lineage is invalid and the key returned is None: a broken lineage pins
    nothing rather than its last or an earlier key. Without a ``load_rotation`` no rotation can be checked, so
    a rotated ledger read that way is invalid, by design."""
    pinned, violations = None, []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "owner_key_set":
            continue
        where = f"owner_key_set at seq {event.get('seq', '?')}"
        body = event.get("body") if isinstance(event.get("body"), dict) else {}
        keyid, previous, rotation = body.get("keyid"), body.get("previous"), body.get("rotation_digest")
        if not (isinstance(keyid, str) and _HEX_KEY.match(keyid)):
            violations.append(f"{where}: keyid is not a 64-hex public key")
            continue
        if pinned is None:
            if previous is not None or rotation is not None:
                violations.append(f"{where}: the first pin is the bootstrap and carries no previous key or rotation")
        else:
            if previous != pinned:
                violations.append(f"{where}: previous {str(previous)[:10]!r} is not the key pinned before it ({pinned[:10]})")
            if not (isinstance(rotation, str) and _HEX_KEY.match(rotation)):  # a sha256 digest has the key's shape
                violations.append(f"{where}: a second pin is a rotation and names a rotation_digest; a bare pin does not re-root the ledger")
            else:
                envelope = load_rotation(rotation) if load_rotation is not None else None
                if envelope is None:
                    violations.append(f"{where}: rotation {rotation[:12]} is missing, altered or unavailable")
                else:
                    try:
                        verify_rotation(envelope, bytes.fromhex(pinned), bytes.fromhex(keyid))
                    except Rejected as exc:
                        violations.append(f"{where}: {exc.detail}")
        pinned = keyid
    return (None if violations else pinned), violations


def pinned_owner_key(events: list[dict], load_rotation=None) -> str | None:
    """The owner's public key (hex) the ledger pins, read through its verified lineage
    (:func:`owner_key_lineage`): None when the ledger has pinned none, refused (``owner_key_lineage``) when the
    lineage does not verify. Never the last event's key on its own, and never an earlier key as a fallback."""
    pinned, violations = owner_key_lineage(events, load_rotation)
    if violations:
        raise Rejected("owner_key_lineage", "the ledger's owner-key lineage does not verify: " + "; ".join(violations))
    return pinned


def rotation_payload(previous: bytes, next_key: bytes) -> dict:
    """What the current owner signs to hand the ledger to a new key: the key being replaced and the key that
    replaces it. Signed by ``previous``; nothing else can rotate."""
    return {"protocol": OWNER_KEY_PROTOCOL, "previous": previous.hex(), "next": next_key.hex(), "at": int(time.time() * 1_000_000)}


def sign_rotation(payload: dict, seed: bytes) -> dict:
    return sign_dsse(OWNER_KEY_PAYLOAD_TYPE, canonical(payload).encode("utf-8"), seed)


def verify_rotation(envelope: dict, previous: bytes, next_key: bytes) -> dict:
    """The rotation payload when ``envelope`` verifies under ``previous`` (the pinned key) and names exactly
    this handover; refused (``owner_key_rotation``) otherwise."""
    payload = verified_payload(envelope, previous, OWNER_KEY_PAYLOAD_TYPE)
    if payload is None:
        raise Rejected("owner_key_rotation", "a rotation is signed by the key the ledger pins now; this one does not verify under it")
    try:
        rotation = json.loads(payload)
    except ValueError:
        raise Rejected("owner_key_rotation", "the rotation payload is not JSON") from None
    if (not isinstance(rotation, dict) or rotation.get("protocol") != OWNER_KEY_PROTOCOL
            or rotation.get("previous") != previous.hex() or rotation.get("next") != next_key.hex()):
        raise Rejected("owner_key_rotation", "the rotation does not name this handover (previous and next key)")
    return rotation


def approval_payload(spec: dict, operation: str, approver: dict, *, verdict_ids: list[str] | None = None,
                     spec_digest_value: str | None = None) -> dict:
    """What the owner signs: this spec (by id, version and digest), this operation, the verdicts a promotion
    rests on, and who approves. The digest is the spec's content digest (:func:`spec_digest`: the parsed
    document without ``history``) before the operation, so a re-serialized file is the same spec and an
    edit to the file's ``history`` is not covered; the ledger's ``spec_changed`` events are the history."""
    if operation not in GATED:
        raise Rejected("spec_operation", f"an approval covers one of {sorted(GATED)}, not {operation!r}")
    if approver.get("kind") != "human":
        raise Rejected("approval_actor", "an approval is a human act; the approver's kind is human")
    return {"protocol": APPROVAL_PROTOCOL, "operation": operation, "spec_id": spec["spec_id"], "version": spec["version"],
            "spec_digest": spec_digest_value or spec_digest(spec), "verdict_ids": sorted(verdict_ids or []),
            "approver": {"kind": "human", "id": approver["id"]}, "at": int(time.time() * 1_000_000)}


def sign_approval(payload: dict, seed: bytes) -> dict:
    """The owner's DSSE envelope over the canonical approval payload (the certificate module's Ed25519)."""
    return sign_dsse(APPROVAL_PAYLOAD_TYPE, canonical(payload).encode("utf-8"), seed)


def verify_approval(envelope: dict, public: bytes, spec: dict, operation: str, *, verdict_ids: list[str] | None = None,
                    spec_digest_value: str | None = None) -> Approved:
    """The approval inside ``envelope`` when its signature verifies under the owner's ``public`` key and it
    covers exactly this spec, this operation and these verdicts. Anything else is refused."""
    payload = verified_payload(envelope, public, APPROVAL_PAYLOAD_TYPE)
    if payload is None:
        raise Rejected("approval_unverified", "the approval does not verify under the owner's public key")
    try:
        approval = json.loads(payload)
    except ValueError:
        raise Rejected("approval_unverified", "the approval payload is not JSON") from None
    if not isinstance(approval, dict) or approval.get("protocol") != APPROVAL_PROTOCOL:
        raise Rejected("approval_unverified", "the approval payload is not a factory approval")
    expected = {"operation": operation, "spec_id": spec["spec_id"], "version": spec["version"],
                "spec_digest": spec_digest_value or spec_digest(spec), "verdict_ids": sorted(verdict_ids or [])}
    for key, value in expected.items():
        if approval.get(key) != value:
            raise Rejected("approval_mismatch", f"the approval's {key} is {approval.get(key)!r}; this {operation} has {value!r}")
    approver = approval.get("approver")
    if not isinstance(approver, dict) or approver.get("kind") != "human" or not _PRINCIPAL_ID.match(str(approver.get("id"))):
        raise Rejected("approval_actor", "the approval names no human approver")
    return Approved({**approval, "keyid": public.hex()}, _token=_ISSUED)


def resolve_verdicts(events: list[dict], spec: dict, verdict_ids: list[str], spec_digest_value: str | None = None) -> list[dict]:
    """Each verdict id resolved on the ledger: a ``spec_changed`` ``review`` event that accepted a variant, and
    the ``validate`` event that wrote that variant as the candidate whose digest is this spec's. Returns one
    record per verdict (``verdict_id``, ``variant_id``, ``reviewer``, ``proposers``); refuses an unknown,
    rejecting or mismatched verdict, and refuses when no verdict is given."""
    if not verdict_ids:
        raise Rejected("spec_verdict", "promotion records the review verdicts it rests on; none given")
    current = spec_digest_value or spec_digest(spec)
    changes = [e["body"] for e in events if e["kind"] == "spec_changed" and e["body"].get("spec_id") == spec["spec_id"]]
    resolved = []
    for verdict_id in verdict_ids:
        review = next((c for c in changes if c.get("operation") == "review" and c.get("verdict_id") == verdict_id), None)
        if review is None:
            raise Rejected("spec_verdict", f"verdict {verdict_id!r} is not a review on the ledger for spec {spec['spec_id']}")
        if review.get("decision") != "accept":
            raise Rejected("spec_verdict", f"verdict {verdict_id!r} did not accept (decision {review.get('decision')!r})")
        variant_id = review.get("variant_id")
        validated = next((c for c in changes if c.get("operation") == "validate" and c.get("accepted_variant") == variant_id
                          and c.get("verdict_id") == verdict_id), None)
        if validated is None or validated.get("spec_digest") != current:
            raise Rejected("spec_verdict", f"verdict {verdict_id!r} accepted variant {variant_id}, which is not this spec "
                                           f"(digest {current[:12]}); the file was edited after review or never written by the loop")
        proposers = sorted({c["actor"].get("id") for c in changes if c.get("operation") == "mutate"
                            and c.get("variant_id") == variant_id and isinstance(c.get("actor"), dict)})
        resolved.append({"verdict_id": verdict_id, "variant_id": variant_id, "reviewer": review.get("actor"),
                         "proposers": proposers})
    return resolved


def gate(spec: dict, operation: str, actor: dict, events: list[dict], envelope: dict, *,
         verdict_ids: list[str] | None = None, spec_digest_value: str | None = None, load_rotation=None) -> tuple[Approved, list[dict]]:
    """The goal gate in one call: the actor is human and is the approver, the ledger pins an owner key whose
    lineage verifies and the approval verifies under that key (no other) and covers this spec and operation,
    and (on promote) every verdict resolves to an accepting review of this variant whose proposers and
    reviewer are not the approver. The key is read from ``events`` through :func:`pinned_owner_key`, never
    taken from the caller; ``load_rotation`` (the ledger's ``approvals/`` reader) only supplies the stored
    rotation envelopes, each of which must still verify under the key the chain pinned before it. An invalid
    lineage refuses (``owner_key_lineage``). Returns the approval and the resolved verdicts."""
    if actor.get("kind") != "human":
        raise Rejected("spec_actor", f"{operation} is an owner act: the actor's kind is human, not {actor.get('kind')!r}")
    pinned = pinned_owner_key(events, load_rotation)
    if pinned is None:
        raise Rejected("owner_key_unset", "the ledger pins no owner key; `factory owner --public KEY` (or the first `--owner-public`) sets it")
    declared = [s.get("keyid") for s in (envelope.get("signatures") or []) if isinstance(s, dict)] if isinstance(envelope, dict) else []
    if declared and pinned not in declared:
        raise Rejected("owner_key_mismatch", f"the approval is signed under key {str(declared[0])[:10]}, not the owner key the ledger pins ({pinned[:10]})")
    resolved = resolve_verdicts(events, spec, list(verdict_ids or []), spec_digest_value) if operation == "promote" else []
    approval = verify_approval(envelope, bytes.fromhex(pinned), spec, operation, verdict_ids=verdict_ids, spec_digest_value=spec_digest_value)
    if approval["approver"]["id"] != actor.get("id"):
        raise Rejected("approval_actor", f"the approval was signed for {approval['approver']['id']!r}, not the actor {actor.get('id')!r}")
    for record in resolved:
        parties = set(record["proposers"]) | {(record.get("reviewer") or {}).get("id")}
        if actor["id"] in parties:
            raise Rejected("approval_not_independent", f"{actor['id']} proposed or reviewed variant {record['variant_id']} and cannot approve it")
    return approval, resolved


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
    """The content digest: the parsed spec without its ``history``, so two files with the same content digest
    alike however they are serialized. The ledger's events are the history; the file's copy is not covered."""
    return digest({k: v for k, v in spec.items() if k != "history"})


def transition(spec: dict, operation: str, actor: dict, **detail) -> dict:
    """Apply a lifecycle operation. The record of it (operation, actor, from, to, input digest) goes on
    ``history`` and is what the ledger logs as ``spec_changed``. Returns that record."""
    if operation not in TRANSITIONS:
        raise Rejected("spec_operation", f"unknown operation {operation!r}; one of {sorted(TRANSITIONS)}")
    allowed, target = TRANSITIONS[operation]
    if spec["state"] not in allowed:
        raise Rejected("spec_state", f"{operation} is not allowed from state {spec['state']!r} (needs one of {sorted(allowed)})")
    if operation in GATED:
        # The goal gate: a human actor and the approval ``verify_approval`` (or ``gate``) returned for this very
        # operation on this very spec. A model actor, a missing approval, or a dictionary that is not an
        # ``Approved`` is refused here, whatever the caller said on argv.
        if actor.get("kind") != "human":
            raise Rejected("spec_actor", f"{operation} is an owner act: the actor's kind is human, not {actor.get('kind')!r}")
        approval = detail.get("approval")
        if not _is_issued(approval):
            raise Rejected("spec_approval", f"{operation} needs the owner's verified approval (see `factory approve`); none given")
        if approval["operation"] != operation or approval["spec_id"] != spec["spec_id"] or approval["version"] != spec["version"]:
            raise Rejected("approval_mismatch", f"the approval covers {approval['operation']} of {approval['spec_id']} v{approval['version']}")
        if approval["approver"]["id"] != actor.get("id"):
            raise Rejected("approval_actor", f"the approval was signed for {approval['approver']['id']!r}, not {actor.get('id')!r}")
        if operation == "promote" and sorted(detail.get("verdict_ids") or []) != approval["verdict_ids"]:
            raise Rejected("spec_verdict", "promotion records the review verdicts the approval covers; they differ or none were given")
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
    elif operation == "retire":
        spec["supersedes"] = detail.get("superseded_by")
    record = {"operation": operation, "actor": actor, "from": spec["state"], "to": target, "version": spec["version"],
              "input_digest": digest({k: v for k, v in detail.items() if k not in ("space", "approval")}),
              "reason": detail.get("reason")}
    if operation in GATED:
        record["approval"] = {"keyid": detail["approval"]["keyid"], "approver": detail["approval"]["approver"],
                              "spec_digest": detail["approval"]["spec_digest"], "verdict_ids": detail["approval"]["verdict_ids"]}
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


# -- variants: several proposers, one reviewer, at a process boundary ---------------------------------------

VARIANT_PROTOCOL = "syberlabs.space/factory/spec-variant/v0"


def variant_request(parent: dict, space: dict | None, objective: str | None) -> dict:
    """What a proposer reads on stdin: the parent spec, the components it may claim, and the objective. It
    answers with one JSON delta (``added``, ``modified``, ``removed``, optional ``footprint_hint`` and
    ``rationale``), the shape :func:`_apply_delta` takes."""
    return {"protocol": VARIANT_PROTOCOL, "role": "proposer", "parent": {k: v for k, v in parent.items() if k != "history"},
            "components": sorted((space or {}).get("components", {})), "objective": objective or ""}


def make_variant(parent: dict, delta: dict, proposer: dict, space: dict | None) -> dict:
    """A variant: the parent with ``delta`` applied through ``mutate``, validated, and marked with its
    proposer. Validation findings stay on the variant (``findings``) rather than refusing it, so a reviewer
    sees an invalid proposal as invalid instead of never seeing it."""
    if not isinstance(delta, dict) or not any(k in delta for k in ("added", "modified", "removed", "footprint_hint")):
        raise Rejected("variant_delta", f"a variant is a delta with added, modified, removed or footprint_hint, not {type(delta).__name__}")
    variant = json.loads(json.dumps(parent))
    transition(variant, "mutate", proposer, delta={k: v for k, v in delta.items() if k != "rationale"},
               reason=delta.get("rationale"))
    variant["proposer"] = proposer
    variant["rationale"] = delta.get("rationale")
    variant["findings"] = validate(variant, space)
    variant["variant_id"] = spec_digest({k: v for k, v in variant.items() if k not in ("findings", "proposer", "rationale")})[:16]
    return variant


def review_request(parent: dict, variants: list[dict], objective: str | None) -> dict:
    """What a reviewer reads on stdin: the parent, every variant with its findings, the objective. It answers
    ``{"verdicts": [{"variant_id", "decision": accept|reject|revise, "rationale"}]}``."""
    return {"protocol": VARIANT_PROTOCOL, "role": "reviewer", "objective": objective or "",
            "parent": {k: v for k, v in parent.items() if k != "history"},
            "variants": [{k: v for k, v in variant.items() if k != "history"} for variant in variants]}


def party(kind: str, name: str, answer: dict | None) -> dict:
    """A proposer or reviewer as the ledger records it: the caller's name, and the model and provider family
    the command declared in its answer (``model``, or ``signal.harness.model``). The family is what
    independence is judged on; a command that declares no model has none and cannot be judged independent."""
    recorded = {"kind": kind, "id": name, "model": model_of(answer) if isinstance(answer, dict) else None}
    recorded["family"] = family(recorded)
    return recorded


def judge(variants: list[dict], verdicts: list[dict], reviewer: dict) -> dict:
    """Apply a reviewer's verdicts. The reviewer is independent of every proposer by provider family
    (``independence``: both families recorded and different; an unknown family refuses), a variant with
    findings cannot be accepted, and at most one variant is accepted. Returns ``{"accepted": variant_id or
    None, "verdicts": [...]}`` with each verdict stamped by the reviewer."""
    by_id = {v["variant_id"]: v for v in variants}
    for variant in variants:
        independent, reason = independence(variant.get("proposer"), reviewer)
        if not independent:
            raise Rejected("reviewer_not_independent", f"{reviewer.get('id')} cannot review variant {variant['variant_id']} "
                                                       f"by {variant.get('proposer', {}).get('id')}: {reason}")
    accepted = None
    stamped = []
    for verdict in verdicts:
        variant_id = verdict.get("variant_id")
        decision = verdict.get("decision")
        if variant_id not in by_id or decision not in ("accept", "reject", "revise"):
            raise Rejected("verdict_invalid", f"a verdict names a variant and decides accept, reject or revise: {verdict!r}")
        if decision == "accept":
            if by_id[variant_id]["findings"]:
                raise Rejected("verdict_invalid", f"variant {variant_id} has validation findings and cannot be accepted: {by_id[variant_id]['findings']}")
            if accepted is not None:
                raise Rejected("verdict_invalid", "a review accepts at most one variant")
            accepted = variant_id
        stamped.append({"verdict_id": digest({"variant": variant_id, "reviewer": reviewer, "decision": decision})[:16],
                        "variant_id": variant_id, "decision": decision, "rationale": verdict.get("rationale"), "reviewer": reviewer})
    missing = sorted(set(by_id) - {v["variant_id"] for v in stamped})
    if missing:
        raise Rejected("verdict_incomplete", f"no verdict for variants {missing}")
    return {"accepted": accepted, "verdicts": stamped}


def tickets_from_moved(moved: list[dict], graph: dict, claimed: set, *, threshold: float, before: str, after: str,
                       human_owner: str | None = None) -> list[dict]:
    """The third automatic source: regions whose feature vectors moved more than ``threshold`` between two
    revisions, grouped by component, where no open ticket or promoted spec claims the component. One ticket
    per unclaimed component, asking for a spec or a revert."""
    by_component: dict[str, list[dict]] = {}
    for entry in moved:
        if entry["distance"] < threshold:
            continue
        sid = entry["id"]
        symbol = graph["symbols"].get(sid)
        component = graph["files"].get(symbol["path"], {}).get("component") if symbol else None
        if component is None or component in claimed:
            continue
        by_component.setdefault(component, []).append(entry)
    tickets = []
    for component, entries in sorted(by_component.items()):
        tickets.append({
            "protocol": PROTOCOL, "kind": "ticket",
            "ticket_id": digest({"moved": component, "before": before, "after": after, "ids": sorted(e["id"] for e in entries)})[:16],
            "title": f"{component} changed between {str(before)[:10]} and {str(after)[:10]} with no spec claiming it",
            "spec_ref": {"spec_id": None, "version": None, "req_ids": []},
            "acceptance_criteria": [{"given": f"the feature map at {str(after)[:10]}", "when": "a spec claims the component or the change is reverted",
                                     "then": "the region is claimed or unmoved"}],
            "footprint": {"components": [component], "files": sorted({graph["symbols"][e["id"]]["path"] for e in entries if e["id"] in graph["symbols"]}),
                          "revision": after, "space_digest": None},
            "repro": {"baseline_run": None, "moved": entries[:20]},
            "exit_criteria": ["spec:claims-footprint"], "delegate": None, "human_owner": human_owner,
            "state": "open", "candidates": [], "verdicts": [], "evidence_links": [],
        })
    return tickets
