"""Persistent Builder coordination for one cell.

Rows and the coordination event log live in the cell database. They are not
case events. ``Work.tx`` supplies the same SQLite and PostgreSQL statements
the rest of the cell uses. A generation, a comment, a heartbeat, or a
selection does not append to the case hash chain.
"""

from __future__ import annotations

import json
import time
import uuid

from syberlabs.builder import (
    LINKABLE_STATES,
    PROTOCOL,
    REPLAY_LIMIT,
    DisconnectedRuntime,
    agent_authority,
    architecture_diff,
    command_effect,
    context_manifest,
    diversity_evidence,
    evaluate_selection,
    feedback_dimensions,
    generation_summary,
    integrity_projection,
    nodes_for_paths,
    normalize_activity,
    normalize_descriptor,
    normalize_feedback,
    normalize_integrity,
    normalize_policy,
    normalize_prototype,
    project,
    promotion_reference,
    validate_snapshot,
)
from syberlabs.canonical import canonical, digest
from syberlabs.clock import stamp
from syberlabs.errors import Rejected


def _loads(value: str):
    return json.loads(value)


def _now() -> int:
    return stamp(time.time())


def _id() -> str:
    return str(uuid.uuid4())


class BuilderStore:
    def __init__(self, work):
        self.work = work

    def open_work(self, case_id: str, objective: str, actor: str) -> dict:
        if not isinstance(objective, str) or not objective.strip():
            raise Rejected("invalid_work", "work objective required")
        with self.work.tx() as db:
            self._require_case(db, case_id)
            row = db.execute("SELECT objective, actor, created_at FROM builder_works WHERE case_id=?", (case_id,)).fetchone()
            if row:
                if row["objective"] != objective.strip():
                    raise Rejected("work_objective_set", "this case already has a work objective")
                return {"case_id": case_id, "objective": row["objective"], "actor": row["actor"], "created_at": row["created_at"]}
            created = _now()
            db.execute(
                "INSERT INTO builder_works (case_id, objective, actor, created_at) VALUES (?,?,?,?)",
                (case_id, objective.strip(), actor, created),
            )
            return {"case_id": case_id, "objective": objective.strip(), "actor": actor, "created_at": created}

    def install_policy(self, document: dict, actor: str) -> dict:
        policy = normalize_policy(document)
        body = canonical(policy)
        with self.work.tx() as db:
            prior = db.execute(
                "SELECT body FROM builder_policies WHERE id=? AND version=?",
                (policy["id"], policy["version"]),
            ).fetchone()
            if prior and prior["body"] != body:
                raise Rejected("immutable_policy", "publish a new selection policy version")
            if not prior:
                db.execute(
                    "INSERT INTO builder_policies (id, version, body) VALUES (?,?,?)",
                    (policy["id"], policy["version"], body),
                )
        policy["actor"] = actor
        return policy

    def create_generation(self, document: dict, actor: str) -> dict:
        if not isinstance(document, dict):
            raise Rejected("invalid_generation", "generation must be an object")
        case_id = document.get("case_id")
        objective = document.get("objective")
        revision = document.get("base_revision")
        mode = document.get("mode")
        isolation = document.get("isolation")
        threshold = document.get("diversity_threshold", 0.3)
        minimum = document.get("min_approaches", 2)
        policy_id = document.get("selection_policy_id")
        policy_version = document.get("selection_policy_version")
        if not isinstance(case_id, str) or not isinstance(objective, str) or not objective.strip():
            raise Rejected("invalid_generation", "case and objective required")
        if not isinstance(revision, str) or not revision.strip() or len(revision) > 200:
            raise Rejected("invalid_generation", "base revision required")
        if mode not in ("explore", "refine", "harden"):
            raise Rejected("invalid_mode", "mode must be explore, refine, or harden")
        if isolation not in ("independent", "aware", "collaborative"):
            raise Rejected("invalid_isolation", "isolation must be independent, aware, or collaborative")
        if type(threshold) not in (int, float) or isinstance(threshold, bool) or not 0 <= float(threshold) <= 1:
            raise Rejected("invalid_generation", "diversity threshold must be between 0 and 1")
        if type(minimum) is not int or minimum < 1:
            raise Rejected("invalid_generation", "min_approaches must be a positive integer")
        if not isinstance(policy_id, str) or type(policy_version) is not int:
            raise Rejected("invalid_generation", "selection policy reference required")
        with self.work.tx() as db:
            self._require_case(db, case_id)
            self._policy(db, policy_id, policy_version)
            current = db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) AS n FROM builder_generations WHERE case_id=?",
                (case_id,),
            ).fetchone()
            ordinal = int(current["n"]) + 1
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_generations (id, case_id, ordinal, objective, base_revision, mode, isolation, "
                "diversity_threshold, min_approaches, selection_policy_id, selection_policy_version, state, actor, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, case_id, ordinal, objective.strip(), revision.strip(), mode, isolation,
                 float(threshold), minimum, policy_id, policy_version, "drafting", actor, created),
            )
            generation = self._generation(db, identifier)
            self._append(db, case_id, identifier, "generation_created", actor, {
                "generation_id": identifier,
                "ordinal": ordinal,
                "objective": generation["objective"],
                "base_revision": generation["base_revision"],
                "mode": mode,
                "isolation": isolation,
                "min_approaches": minimum,
                "selection_policy_id": policy_id,
                "selection_policy_version": policy_version,
            })
            return generation

    def register_approach(self, generation_id: str, descriptor: dict, actor: str, semantic=None) -> dict:
        normalized = normalize_descriptor(descriptor)
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] != "drafting":
                raise Rejected("approach_frozen", "approaches are frozen once the generation leaves drafting")
            siblings = [item for item in self._approaches(db, generation_id) if item["state"] == "active"]
            report = diversity_evidence(normalized, siblings, generation["diversity_policy"]["threshold"], semantic)
            state = "active" if report["passed"] else "rejected"
            kind = "approach_registered" if report["passed"] else "approach_rejected"
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_approaches (id, generation_id, state, descriptor, evidence, created_at) VALUES (?,?,?,?,?,?)",
                (identifier, generation_id, state, canonical(normalized), canonical(report["evidence"]), created),
            )
            body = {"approach_id": identifier, "state": state, "evidence": report["evidence"]}
            if not report["passed"]:
                body["reason"] = f"approach_too_similar:{report['sibling_id']}"
            self._append(db, generation["case_id"], generation_id, kind, actor, body)
            stored = self._approach(db, identifier)
            reason = None if report["passed"] else f"approach_too_similar:{report['sibling_id']}"
        if reason:
            raise Rejected(reason, stored["id"])
        return stored

    def revise_approach(self, approach_id: str, descriptor: dict, actor: str, semantic=None) -> dict:
        normalized = normalize_descriptor(descriptor)
        with self.work.tx() as db:
            approach = self._approach(db, approach_id)
            generation = self._generation(db, approach["generation_id"])
            if generation["state"] != "drafting" or approach["state"] != "active":
                raise Rejected("approach_frozen", "sealed approach descriptors are immutable")
            siblings = [
                item for item in self._approaches(db, generation["id"])
                if item["state"] == "active" and item["id"] != approach_id
            ]
            report = diversity_evidence(normalized, siblings, generation["diversity_policy"]["threshold"], semantic)
            if not report["passed"]:
                raise Rejected(f"approach_too_similar:{report['sibling_id']}", "revision failed the diversity gate")
            db.execute(
                "UPDATE builder_approaches SET descriptor=?, evidence=? WHERE id=?",
                (canonical(normalized), canonical(report["evidence"]), approach_id),
            )
            self._append(db, generation["case_id"], generation["id"], "approach_revised", actor, {
                "approach_id": approach_id,
                "evidence": report["evidence"],
            })
            return self._approach(db, approach_id)

    def seal(self, generation_id: str, actor: str) -> dict:
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] != "drafting":
                raise Rejected("generation_state", "only a drafting generation can be sealed")
            active = [item for item in self._approaches(db, generation_id) if item["state"] == "active"]
            minimum = generation["diversity_policy"]["min_approaches"]
            if len(active) < minimum:
                raise Rejected("diversity_short", f"generation needs {minimum} approaches before it can seal")
            pairs = []
            for index, approach in enumerate(active):
                report = diversity_evidence(
                    approach["descriptor"],
                    active[:index],
                    generation["diversity_policy"]["threshold"],
                )
                if not report["passed"]:
                    raise Rejected(f"approach_too_similar:{report['sibling_id']}", "diversity gate failed at seal")
                pairs.extend(report["evidence"]["pairs"])
            db.execute("UPDATE builder_approaches SET state='frozen' WHERE generation_id=? AND state='active'", (generation_id,))
            db.execute("UPDATE builder_generations SET state='sealed' WHERE id=?", (generation_id,))
            self._append(db, generation["case_id"], generation_id, "generation_sealed", actor, {
                "approach_ids": [item["id"] for item in active],
                "pairwise": pairs,
                "evidence_digest": digest(pairs),
            })
            return self._generation(db, generation_id)

    def launch(self, generation_id: str, actor: str) -> dict:
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] != "sealed":
                raise Rejected("generation_unsealed", "generation must be sealed before launch")
            db.execute("UPDATE builder_generations SET state='launched' WHERE id=?", (generation_id,))
            self._append(db, generation["case_id"], generation_id, "generation_launched", actor, {
                "generation_id": generation_id,
            })
            return self._generation(db, generation_id)

    def close_generation(self, generation_id: str, actor: str) -> dict:
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] not in ("sealed", "launched", "evaluating", "selecting"):
                raise Rejected("generation_state", "generation cannot close from this state")
            db.execute("UPDATE builder_generations SET state='closed' WHERE id=?", (generation_id,))
            self._append(db, generation["case_id"], generation_id, "generation_closed", actor, {
                "generation_id": generation_id,
                "prior_state": generation["state"],
            })
            return self._generation(db, generation_id)

    def register_agent(self, document: dict, actor: str) -> dict:
        if not isinstance(document, dict):
            raise Rejected("invalid_agent", "agent session must be an object")
        generation_id = document.get("generation_id")
        approach_id = document.get("approach_id")
        principal = document.get("principal")
        role = document.get("role")
        if not isinstance(generation_id, str) or not isinstance(principal, str) or not principal.strip():
            raise Rejected("invalid_agent", "generation and principal required")
        if not isinstance(role, str):
            raise Rejected("invalid_agent", "agent role is not recognized")
        authority = agent_authority(role)
        hypothesis = document.get("hypothesis_summary", "")
        if not isinstance(hypothesis, str) or len(hypothesis) > 2000:
            raise Rejected("invalid_agent", "hypothesis summary must be a short string")
        if "chain_of_thought" in document or "reasoning" in document:
            raise Rejected("chain_of_thought_refused", "agent sessions do not store private reasoning")
        uncertainties = document.get("open_uncertainties", [])
        evidence_refs = document.get("evidence_refs", [])
        working_set = document.get("working_set", [])
        for name, value in (("open_uncertainties", uncertainties), ("evidence_refs", evidence_refs), ("working_set", working_set)):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise Rejected("invalid_agent", f"{name} must be a list of strings")
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] not in LINKABLE_STATES:
                raise Rejected("generation_unsealed", "agents are assigned after the generation is sealed")
            if approach_id is not None:
                approach = self._approach(db, approach_id)
                if approach["generation_id"] != generation_id or approach["state"] != "frozen":
                    raise Rejected("candidate_approach", "agent approach must be a sealed descriptor in this generation")
            identifier = _id()
            assignment = {"approach_id": approach_id, "objective": generation["objective"]}
            db.execute(
                "INSERT INTO builder_agents (id, case_id, generation_id, approach_id, candidate_id, principal, role, "
                "authority, assignment, working_set, state, hypothesis_summary, evidence_refs, open_uncertainties) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, generation["case_id"], generation_id, approach_id, None, principal.strip(), role, authority,
                 canonical(assignment), canonical(working_set), "registered", hypothesis, canonical(evidence_refs),
                 canonical(uncertainties)),
            )
            self._append(db, generation["case_id"], generation_id, "agent_registered", actor, {
                "agent_id": identifier,
                "principal": principal.strip(),
                "role": role,
                "authority": authority,
                "approach_id": approach_id,
            })
            return self._agent(db, identifier)

    def record_activity(self, agent_id: str, document: dict, actor: str) -> dict:
        activity = normalize_activity(document)
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            generation = self._generation(db, agent["generation_id"])
            if generation["state"] not in ("launched", "evaluating", "selecting"):
                raise Rejected("generation_state", "agent activity starts after launch")
            if activity["caused_by"]:
                for event_id in activity["caused_by"]:
                    found = db.execute(
                        "SELECT id FROM coordination_events WHERE id=? AND case_id=?",
                        (event_id, agent["case_id"]),
                    ).fetchone()
                    if not found:
                        raise Rejected("unknown_cause", event_id)
            if generation["state"] == "launched":
                db.execute("UPDATE builder_generations SET state='evaluating' WHERE id=?", (generation["id"],))
            if activity["activity"] == "edit_scope":
                db.execute(
                    "UPDATE builder_agents SET working_set=? WHERE id=?",
                    (canonical(activity["paths"]), agent_id),
                )
            if activity["activity"] == "complete":
                db.execute("UPDATE builder_agents SET state='complete' WHERE id=?", (agent_id,))
            elif agent["state"] == "registered":
                db.execute("UPDATE builder_agents SET state='active' WHERE id=?", (agent_id,))
            event = self._append(db, agent["case_id"], agent["generation_id"], "agent_activity_recorded", actor, {
                "agent_id": agent_id,
                **activity,
            })
            if activity["activity"] == "complete" or agent["state"] == "registered":
                self._append(db, agent["case_id"], agent["generation_id"], "agent_state_changed", actor, {
                    "agent_id": agent_id,
                    "state": "complete" if activity["activity"] == "complete" else "active",
                    "caused_by": [event["id"]],
                })
            return event

    def command(self, agent_id: str, command: str, body: dict, actor: str, runtime=None) -> dict:
        runtime = runtime or DisconnectedRuntime()
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            effect = command_effect(agent, command, body or {})
            prepared = {
                "protocol": PROTOCOL,
                "agent_id": agent_id,
                "command": command,
                "body": body or {},
                "actor": actor,
            }
        try:
            result = runtime.apply(prepared)
        except Exception:
            result = {"applied": False, "reason": "runtime_error"}
        if not isinstance(result, dict) or type(result.get("applied")) is not bool or not isinstance(result.get("reason"), str):
            result = {"applied": False, "reason": "runtime_error"}
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            command_effect(agent, command, body or {})
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_commands (id, case_id, agent_id, command, body, result, actor, at) VALUES (?,?,?,?,?,?,?,?)",
                (identifier, agent["case_id"], agent_id, command, canonical(body or {}), canonical(result), actor, created),
            )
            if result["applied"]:
                if "state" in effect:
                    db.execute("UPDATE builder_agents SET state=? WHERE id=?", (effect["state"], agent_id))
                if "working_set" in effect:
                    db.execute("UPDATE builder_agents SET working_set=? WHERE id=?", (canonical(effect["working_set"]), agent_id))
                if "assignment" in effect:
                    approach_id = effect["assignment"].get("approach_id")
                    db.execute(
                        "UPDATE builder_agents SET assignment=?, approach_id=? WHERE id=?",
                        (canonical(effect["assignment"]), approach_id, agent_id),
                    )
                for kind in effect["events"]:
                    payload = {"agent_id": agent_id, "command": command, "command_id": identifier}
                    if kind == "agent_activity_recorded":
                        payload.update(activity=effect.get("activity", command), text=effect.get("text"), caused_by=[])
                    if kind == "agent_state_changed" and "state" in effect:
                        payload["state"] = effect["state"]
                    if kind == "agent_assignment_changed":
                        payload["assignment"] = effect.get("assignment")
                    self._append(db, agent["case_id"], agent["generation_id"], kind, actor, payload)
            stored = {
                "id": identifier,
                "agent_id": agent_id,
                "command": command,
                "body": body or {},
                "result": result,
                "actor": actor,
                "at": created,
            }
        return stored

    def link_candidate(self, generation_id: str, approach_id: str, candidate_id: str, changed_paths: list, actor: str) -> dict:
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise Rejected("invalid_candidate", "candidate id required")
        if not isinstance(changed_paths, list) or any(not isinstance(item, str) or not item.strip() for item in changed_paths):
            raise Rejected("invalid_candidate", "changed paths must be a list of strings")
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] not in LINKABLE_STATES:
                raise Rejected("generation_unsealed", "a candidate cannot link before the generation is sealed")
            approach = self._approach(db, approach_id)
            if approach["generation_id"] != generation_id or approach["state"] != "frozen":
                raise Rejected("candidate_approach", "candidate must belong to a sealed approach in this generation")
            existing = db.execute(
                "SELECT id FROM builder_candidate_links WHERE generation_id=? AND candidate_id=?",
                (generation_id, candidate_id),
            ).fetchone()
            if existing:
                raise Rejected("candidate_linked", "this candidate is already linked")
            identifier = _id()
            created = _now()
            paths = [item.strip() for item in changed_paths]
            db.execute(
                "INSERT INTO builder_candidate_links (id, case_id, generation_id, approach_id, candidate_id, changed_paths, actor, at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (identifier, generation["case_id"], generation_id, approach_id, candidate_id.strip(), canonical(paths), actor, created),
            )
            self._append(db, generation["case_id"], generation_id, "candidate_linked", actor, {
                "link_id": identifier,
                "approach_id": approach_id,
                "candidate_id": candidate_id.strip(),
                "changed_paths": paths,
            })
            return self._link(db, identifier)

    def record_feedback(self, document: dict, actor: str, roles: list[str]) -> dict:
        if not isinstance(document, dict) or not isinstance(document.get("case_id"), str):
            raise Rejected("invalid_feedback", "case_id required")
        with self.work.tx() as db:
            self._require_case(db, document["case_id"])
            generation_id = document.get("generation_id")
            if generation_id is not None:
                generation = self._generation(db, generation_id)
                if generation["case_id"] != document["case_id"]:
                    raise Rejected("invalid_feedback", "generation is not in this case")
                policy = self._policy(db, generation["selection_policy"]["id"], generation["selection_policy"]["version"])
            else:
                policy = self._latest_policy(db, document.get("selection_policy_id"))
            feedback = normalize_feedback(document, roles, policy)
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_feedback (id, case_id, generation_id, target_kind, target_id, kind, authority, actor, body, at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, document["case_id"], generation_id, feedback["target_kind"], feedback["target_id"],
                 feedback["kind"], feedback["authority"], actor, canonical({"text": feedback["text"]}), created),
            )
            stored = self._feedback_row(db, identifier)
            self._append(db, document["case_id"], generation_id, "feedback_recorded", actor, {
                "feedback_id": identifier,
                "target_kind": stored["target_kind"],
                "target_id": stored["target_id"],
                "kind": stored["kind"],
                "authority": stored["authority"],
            })
            return stored

    def record_integrity(self, document: dict, actor: str) -> dict:
        observation = normalize_integrity(document)
        if observation["verifier"] != actor:
            raise Rejected("verifier_mismatch", "verifier principal is the caller")
        case_id = document.get("case_id") if isinstance(document, dict) else None
        if not isinstance(case_id, str):
            raise Rejected("invalid_integrity", "case_id required")
        generation_id = document.get("generation_id")
        with self.work.tx() as db:
            self._require_case(db, case_id)
            if generation_id is not None:
                generation = self._generation(db, generation_id)
                if generation["case_id"] != case_id:
                    raise Rejected("invalid_integrity", "generation is not in this case")
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_integrity (id, case_id, generation_id, target_kind, target_id, claim, source, verifier, "
                "independence, evidence_refs, result, digest, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, case_id, generation_id, observation["target_kind"], observation["target_id"], observation["claim"],
                 observation["source"], observation["verifier"], observation["independence"], canonical(observation["evidence_refs"]),
                 observation["result"], observation["digest"], created),
            )
            stored = self._integrity_row(db, identifier)
            self._append(db, case_id, generation_id, "integrity_observation_recorded", actor, {
                "observation_id": identifier,
                "target_kind": stored["target_kind"],
                "target_id": stored["target_id"],
                "independence": stored["independence"],
                "result": stored["result"],
                "digest": stored["digest"],
            })
            return stored

    def select(self, generation_id: str, actor: str, roles: list[str]) -> dict:
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] not in ("launched", "evaluating", "selecting"):
                raise Rejected("generation_state", "selection starts after launch")
            policy = self._policy(db, generation["selection_policy"]["id"], generation["selection_policy"]["version"])
            links = self._links(db, generation_id)
            feedback = [item for item in self._feedback(db, generation["case_id"]) if item["generation_id"] == generation_id]
            integrity = [item for item in self._integrity(db, generation["case_id"]) if item["generation_id"] == generation_id]
            candidates = [{"id": item["candidate_id"]} for item in links]
            evaluation = evaluate_selection(policy, candidates, feedback, integrity, roles)
            evaluation["evidence_digest"] = digest({
                "advanced": evaluation["advanced"],
                "unresolved": evaluation["unresolved"],
                "rejected": evaluation["rejected"],
                "dimensions": evaluation["dimensions"],
            })
            self._append(db, generation["case_id"], generation_id, "selection_started", actor, {
                "policy_id": policy["id"],
                "policy_version": policy["version"],
            })
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_selections (id, case_id, generation_id, policy_id, policy_version, actor, body, at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (identifier, generation["case_id"], generation_id, policy["id"], policy["version"], actor, canonical(evaluation), created),
            )
            db.execute("UPDATE builder_generations SET state='selecting' WHERE id=?", (generation_id,))
            stored = self._selection(db, identifier)
            self._append(db, generation["case_id"], generation_id, "selection_recorded", actor, {
                "selection_id": identifier,
                "evidence_digest": stored["evidence_digest"],
                "promotes_git": False,
                "advanced": [item["id"] for item in stored["advanced"]],
                "unresolved": [item["id"] for item in stored["unresolved"]],
                "rejected": [item["id"] for item in stored["rejected"]],
            })
            return stored

    def publish_architecture(self, document: dict, actor: str, provider=None) -> dict:
        if not isinstance(document, dict) or not isinstance(document.get("case_id"), str):
            raise Rejected("invalid_architecture", "case_id required")
        case_id = document["case_id"]
        generation_id = document.get("generation_id")
        if provider is not None:
            built = provider.build(document["repository"], document.get("revision", ""))
            if document.get("id"):
                built["id"] = document["id"]
            if document.get("baseline_id"):
                built["baseline_id"] = document["baseline_id"]
            snapshot = validate_snapshot(built)
        else:
            snapshot = validate_snapshot(document)
        with self.work.tx() as db:
            self._require_case(db, case_id)
            if generation_id is not None:
                generation = self._generation(db, generation_id)
                if generation["case_id"] != case_id:
                    raise Rejected("invalid_architecture", "generation is not in this case")
            identifier = snapshot["id"] or _id()
            snapshot["id"] = identifier
            created = _now()
            db.execute(
                "INSERT INTO builder_architecture (id, case_id, generation_id, repository, revision, provider, body, at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (identifier, case_id, generation_id, snapshot["repository"], snapshot["revision"], snapshot["provider"],
                 canonical(snapshot), created),
            )
            self._append(db, case_id, generation_id, "architecture_snapshot_published", actor, {
                "snapshot_id": identifier,
                "repository": snapshot["repository"],
                "revision": snapshot["revision"],
                "provider": snapshot["provider"],
            })
            return self._snapshot(db, identifier)

    def register_prototype(self, document: dict, actor: str, provider=None) -> dict:
        prototype = normalize_prototype(document)
        if not isinstance(document, dict) or not isinstance(document.get("case_id"), str):
            raise Rejected("invalid_prototype", "case_id required")
        generation_id = document.get("generation_id")
        described = (provider or None).describe({**prototype, "provider": prototype["provider"]}) if provider else None
        if described and described.get("endpoint") and not prototype["endpoint"]:
            prototype["endpoint"] = described["endpoint"]
        with self.work.tx() as db:
            self._require_case(db, document["case_id"])
            if generation_id is not None:
                generation = self._generation(db, generation_id)
                if generation["case_id"] != document["case_id"]:
                    raise Rejected("invalid_prototype", "generation is not in this case")
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_prototypes (id, case_id, generation_id, candidate_id, artifact_ref, environment, provider, "
                "endpoint, state, created_by, created_at, expires_at, digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, document["case_id"], generation_id, prototype["candidate_id"], prototype["artifact_ref"],
                 prototype["environment"], prototype["provider"], prototype["endpoint"], prototype["state"], actor, created,
                 prototype["expires_at"], prototype["digest"]),
            )
            self._append(db, document["case_id"], generation_id, "prototype_registered", actor, {
                "prototype_id": identifier,
                "candidate_id": prototype["candidate_id"],
                "state": prototype["state"],
                "provider": prototype["provider"],
            })
            return self._prototype(db, identifier)

    def set_prototype_state(self, prototype_id: str, state: str, actor: str) -> dict:
        if state not in ("building", "ready", "failed", "expired"):
            raise Rejected("invalid_prototype", "prototype state is not recognized")
        with self.work.tx() as db:
            current = self._prototype(db, prototype_id)
            db.execute("UPDATE builder_prototypes SET state=? WHERE id=?", (state, prototype_id))
            self._append(db, current["case_id"], current["generation_id"], "prototype_state_changed", actor, {
                "prototype_id": prototype_id,
                "prior_state": current["state"],
                "state": state,
            })
            return self._prototype(db, prototype_id)

    def manifest(self, agent_id: str) -> dict:
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            generation = self._generation(db, agent["generation_id"])
            approach = self._approach(db, agent["approach_id"]) if agent["approach_id"] else None
            if approach is None:
                raise Rejected("candidate_approach", "agent has no approach")
            siblings = self._approaches(db, generation["id"])
            implementations = self._links(db, generation["id"])
            return context_manifest(generation, approach, siblings, implementations)

    def replay(self, case_id: str, after_seq: int = 0, limit: int = REPLAY_LIMIT) -> tuple[list[dict], bool]:
        limit = max(1, min(int(limit), REPLAY_LIMIT))
        with self.work.tx() as db:
            self._require_case(db, case_id)
            if after_seq:
                rows = db.execute(
                    "SELECT id, seq, case_id, generation_id, kind, actor, at, body FROM coordination_events "
                    "WHERE case_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (case_id, int(after_seq), limit + 1),
                ).fetchall()
                truncated = len(rows) > limit
                rows = rows[:limit]
            else:
                rows = db.execute(
                    "SELECT id, seq, case_id, generation_id, kind, actor, at, body FROM coordination_events "
                    "WHERE case_id=? ORDER BY seq DESC LIMIT ?",
                    (case_id, limit + 1),
                ).fetchall()
                truncated = len(rows) > limit
                rows = list(reversed(rows[:limit]))
        return [self._event(row) for row in rows], truncated

    def world(self, case_id: str) -> dict:
        with self.work.tx() as db:
            self._require_case(db, case_id)
            work = db.execute("SELECT objective FROM builder_works WHERE case_id=?", (case_id,)).fetchone()
            generations = []
            for row in db.execute(
                "SELECT id FROM builder_generations WHERE case_id=? ORDER BY ordinal",
                (case_id,),
            ).fetchall():
                generation = self._generation(db, row["id"])
                count = len([item for item in self._approaches(db, generation["id"]) if item["state"] != "rejected"])
                generations.append(generation_summary(generation, count))
            kinds = [row["kind"] for row in db.execute(
                "SELECT kind FROM events WHERE case_id=? ORDER BY seq",
                (case_id,),
            ).fetchall()]
        return {
            "protocol": PROTOCOL,
            "kind": "WorldProjection",
            "authoritative": False,
            "case_id": case_id,
            "objective": None if work is None else work["objective"],
            "generations": generations,
            "case_authority": {"event_kinds": kinds, "event_count": len(kinds)},
        }

    def generation_view(self, generation_id: str) -> dict:
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            approaches = self._approaches(db, generation_id)
            agents = self._agents(db, generation_id)
            links = self._links(db, generation_id)
            feedback = [item for item in self._feedback(db, generation["case_id"]) if item["generation_id"] == generation_id]
            integrity = [item for item in self._integrity(db, generation["case_id"]) if item["generation_id"] == generation_id]
            prototypes = [item for item in self._prototypes(db, generation["case_id"]) if item["generation_id"] == generation_id]
            selections = self._selections(db, generation_id)
            snapshot = self._latest_snapshot(db, generation["case_id"], generation_id)
            candidates = [
                self._candidate_projection(db, generation, link, snapshot, feedback, integrity, prototypes)
                for link in links
            ]
        return {
            "protocol": PROTOCOL,
            "kind": "GenerationProjection",
            "authoritative": False,
            "generation": generation,
            "approaches": approaches,
            "agents": agents,
            "candidates": candidates,
            "feedback_dimensions": feedback_dimensions(feedback),
            "integrity": integrity_projection(integrity),
            "prototypes": prototypes,
            "selection": selections[-1] if selections else None,
            "architecture_snapshot_id": None if snapshot is None else snapshot["id"],
        }

    def candidate_views(self, generation_id: str) -> list[dict]:
        return self.generation_view(generation_id)["candidates"]

    def actor_view(self, agent_id: str) -> dict:
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            events = [
                self._event(row)
                for row in db.execute(
                    "SELECT id, seq, case_id, generation_id, kind, actor, at, body FROM coordination_events "
                    "WHERE case_id=? AND kind IN ('agent_activity_recorded', 'agent_state_changed', 'agent_assignment_changed') "
                    "ORDER BY seq",
                    (agent["case_id"],),
                ).fetchall()
                if _loads(row["body"]).get("agent_id") == agent_id
            ]
            commands = []
            for row in db.execute(
                "SELECT id, command, body, result, actor, at FROM builder_commands WHERE agent_id=? ORDER BY at",
                (agent_id,),
            ).fetchall():
                commands.append({
                    "id": row["id"],
                    "command": row["command"],
                    "body": _loads(row["body"]),
                    "result": _loads(row["result"]),
                    "actor": row["actor"],
                    "at": row["at"],
                })
        manifest = self.manifest(agent_id) if agent["approach_id"] else None
        causality = []
        for event in events:
            for cause in event["body"].get("caused_by") or []:
                causality.append({"from": cause, "to": event["id"], "activity": event["body"].get("activity"), "kind": event["kind"]})
        return {
            "protocol": PROTOCOL,
            "kind": "ActorProjection",
            "authoritative": False,
            "agent": agent,
            "activity": events,
            "commands": commands,
            "context_manifest": manifest,
            "causality": causality,
        }

    def architecture_view(self, snapshot_id: str, baseline_id: str | None = None) -> dict:
        with self.work.tx() as db:
            snapshot = self._snapshot(db, snapshot_id)
            baseline = self._snapshot(db, baseline_id) if baseline_id else None
        view = {"protocol": PROTOCOL, "kind": "ArchitectureProjection", "authoritative": False, "snapshot": snapshot}
        if baseline is not None:
            view["diff"] = architecture_diff(baseline, snapshot)
        return view

    def integrity_view(self, target: str) -> dict:
        with self.work.tx() as db:
            rows = db.execute(
                "SELECT id FROM builder_integrity WHERE target_id=? ORDER BY at",
                (target,),
            ).fetchall()
            observations = [self._integrity_row(db, row["id"]) for row in rows]
        return integrity_projection(observations, target)

    def project(self, view: dict, principal: dict) -> dict:
        return project(view, principal)

    def promotion_reference(self, selection: dict) -> dict:
        return promotion_reference(selection)

    def map_paths(self, snapshot_id: str, paths: list[str]) -> list[dict]:
        with self.work.tx() as db:
            snapshot = self._snapshot(db, snapshot_id)
        return nodes_for_paths(snapshot, paths)

    def _require_case(self, db, case_id: str) -> None:
        if db.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone() is None:
            raise Rejected("unknown_case", case_id)

    def _policy(self, db, policy_id: str, version: int) -> dict:
        row = db.execute(
            "SELECT body FROM builder_policies WHERE id=? AND version=?",
            (policy_id, version),
        ).fetchone()
        if row is None:
            raise Rejected("unknown_policy", policy_id)
        return _loads(row["body"])

    def _latest_policy(self, db, policy_id: str | None) -> dict:
        if not policy_id:
            row = db.execute("SELECT body FROM builder_policies ORDER BY version DESC LIMIT 1").fetchone()
        else:
            row = db.execute(
                "SELECT body FROM builder_policies WHERE id=? ORDER BY version DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
        if row is None:
            raise Rejected("unknown_policy", policy_id or "")
        return _loads(row["body"])

    def _append(self, db, case_id: str, generation_id: str | None, kind: str, actor: str, body: dict) -> dict:
        current = db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM coordination_events WHERE case_id=?",
            (case_id,),
        ).fetchone()
        seq = int(current["s"]) + 1
        identifier = _id()
        created = _now()
        db.execute(
            "INSERT INTO coordination_events (id, seq, case_id, generation_id, kind, actor, at, body) VALUES (?,?,?,?,?,?,?,?)",
            (identifier, seq, case_id, generation_id, kind, actor, created, canonical(body)),
        )
        return {
            "protocol": PROTOCOL,
            "id": identifier,
            "seq": seq,
            "case_id": case_id,
            "generation_id": generation_id,
            "kind": kind,
            "actor": actor,
            "at": created,
            "body": body,
        }

    def _generation(self, db, generation_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_generations WHERE id=?", (generation_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_generation", generation_id)
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "case_id": row["case_id"],
            "ordinal": row["ordinal"],
            "objective": row["objective"],
            "base_revision": row["base_revision"],
            "mode": row["mode"],
            "isolation": row["isolation"],
            "diversity_policy": {
                "method": "structural_jaccard",
                "threshold": float(row["diversity_threshold"]),
                "min_approaches": row["min_approaches"],
            },
            "selection_policy": {"id": row["selection_policy_id"], "version": row["selection_policy_version"]},
            "state": row["state"],
            "actor": row["actor"],
            "created_at": row["created_at"],
        }

    def _approaches(self, db, generation_id: str) -> list[dict]:
        rows = db.execute(
            "SELECT id FROM builder_approaches WHERE generation_id=? ORDER BY created_at, id",
            (generation_id,),
        ).fetchall()
        return [self._approach(db, row["id"]) for row in rows]

    def _approach(self, db, approach_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_approaches WHERE id=?", (approach_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_approach", approach_id)
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "generation_id": row["generation_id"],
            "state": row["state"],
            "descriptor": _loads(row["descriptor"]),
            "evidence": _loads(row["evidence"]),
            "created_at": row["created_at"],
        }

    def _agent(self, db, agent_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_agent", agent_id)
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "approach_id": row["approach_id"],
            "candidate_id": row["candidate_id"],
            "principal": row["principal"],
            "role": row["role"],
            "authority": row["authority"],
            "assignment": _loads(row["assignment"]),
            "working_set": _loads(row["working_set"]),
            "state": row["state"],
            "hypothesis_summary": row["hypothesis_summary"],
            "evidence_refs": _loads(row["evidence_refs"]),
            "open_uncertainties": _loads(row["open_uncertainties"]),
        }

    def _agents(self, db, generation_id: str) -> list[dict]:
        rows = db.execute("SELECT id FROM builder_agents WHERE generation_id=? ORDER BY id", (generation_id,)).fetchall()
        return [self._agent(db, row["id"]) for row in rows]

    def _link(self, db, link_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_candidate_links WHERE id=?", (link_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_candidate", link_id)
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "approach_id": row["approach_id"],
            "candidate_id": row["candidate_id"],
            "changed_paths": _loads(row["changed_paths"]),
            "actor": row["actor"],
            "at": row["at"],
        }

    def _links(self, db, generation_id: str) -> list[dict]:
        rows = db.execute(
            "SELECT id FROM builder_candidate_links WHERE generation_id=? ORDER BY at, id",
            (generation_id,),
        ).fetchall()
        return [self._link(db, row["id"]) for row in rows]

    def _feedback_row(self, db, feedback_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_feedback WHERE id=?", (feedback_id,)).fetchone()
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "target_kind": row["target_kind"],
            "target_id": row["target_id"],
            "kind": row["kind"],
            "authority": row["authority"],
            "actor": row["actor"],
            "text": _loads(row["body"])["text"],
            "at": row["at"],
        }

    def _feedback(self, db, case_id: str) -> list[dict]:
        rows = db.execute("SELECT id FROM builder_feedback WHERE case_id=? ORDER BY at", (case_id,)).fetchall()
        return [self._feedback_row(db, row["id"]) for row in rows]

    def _integrity_row(self, db, observation_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_integrity WHERE id=?", (observation_id,)).fetchone()
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "target_kind": row["target_kind"],
            "target_id": row["target_id"],
            "claim": row["claim"],
            "source": row["source"],
            "verifier": row["verifier"],
            "independence": row["independence"],
            "evidence_refs": _loads(row["evidence_refs"]),
            "result": row["result"],
            "digest": row["digest"],
            "runtime_verified": False,
            "at": row["at"],
        }

    def _integrity(self, db, case_id: str) -> list[dict]:
        rows = db.execute("SELECT id FROM builder_integrity WHERE case_id=? ORDER BY at", (case_id,)).fetchall()
        return [self._integrity_row(db, row["id"]) for row in rows]

    def _selection(self, db, selection_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_selections WHERE id=?", (selection_id,)).fetchone()
        body = _loads(row["body"])
        body.update({
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "policy_id": row["policy_id"],
            "policy_version": row["policy_version"],
            "actor": row["actor"],
            "at": row["at"],
        })
        return body

    def _selections(self, db, generation_id: str) -> list[dict]:
        rows = db.execute(
            "SELECT id FROM builder_selections WHERE generation_id=? ORDER BY at",
            (generation_id,),
        ).fetchall()
        return [self._selection(db, row["id"]) for row in rows]

    def _snapshot(self, db, snapshot_id: str) -> dict:
        row = db.execute("SELECT body FROM builder_architecture WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_snapshot", snapshot_id)
        return _loads(row["body"])

    def _latest_snapshot(self, db, case_id: str, generation_id: str):
        row = db.execute(
            "SELECT id FROM builder_architecture WHERE case_id=? AND (generation_id=? OR generation_id IS NULL) ORDER BY at DESC LIMIT 1",
            (case_id, generation_id),
        ).fetchone()
        if row is None:
            return None
        return self._snapshot(db, row["id"])

    def _prototype(self, db, prototype_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_prototypes WHERE id=?", (prototype_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_prototype", prototype_id)
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "candidate_id": row["candidate_id"],
            "artifact_ref": row["artifact_ref"],
            "environment": row["environment"],
            "provider": row["provider"],
            "endpoint": row["endpoint"],
            "state": row["state"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "digest": row["digest"],
        }

    def _prototypes(self, db, case_id: str) -> list[dict]:
        rows = db.execute("SELECT id FROM builder_prototypes WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
        return [self._prototype(db, row["id"]) for row in rows]

    def _event(self, row) -> dict:
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "seq": row["seq"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "kind": row["kind"],
            "actor": row["actor"],
            "at": row["at"],
            "body": _loads(row["body"]),
        }

    def _candidate_projection(self, db, generation, link, snapshot, feedback, integrity, prototypes) -> dict:
        nodes = [] if snapshot is None else nodes_for_paths(snapshot, link["changed_paths"])
        recorded = False
        for row in db.execute(
            "SELECT body FROM events WHERE case_id=? AND kind='candidate_registered'",
            (generation["case_id"],),
        ).fetchall():
            if _loads(row["body"]).get("id") == link["candidate_id"]:
                recorded = True
        prototype = next((item for item in prototypes if item["candidate_id"] == link["candidate_id"]), None)
        notes = [item for item in feedback if item["target_kind"] == "candidate" and item["target_id"] == link["candidate_id"]]
        observations = [item for item in integrity if item["target_kind"] == "candidate" and item["target_id"] == link["candidate_id"]]
        return {
            "protocol": PROTOCOL,
            "kind": "CandidateProjection",
            "authoritative": False,
            "candidate_id": link["candidate_id"],
            "generation_id": link["generation_id"],
            "approach_id": link["approach_id"],
            "changed_paths": link["changed_paths"],
            "architecture_nodes": nodes,
            "prototype": prototype,
            "feedback_dimensions": feedback_dimensions(notes),
            "integrity": integrity_projection(observations, link["candidate_id"]),
            "case_candidate_recorded": recorded,
            "promotion_state": None,
        }
