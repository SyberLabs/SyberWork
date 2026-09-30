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

from .builder_rows import BuilderRecords
from syberlabs.builder import (
    COORDINATION_ROLES,
    LINKABLE_STATES,
    PROTOCOL,
    PROTOTYPE_STATES,
    PROTOTYPE_TRANSITIONS,
    REPLAY_LIMIT,
    RUNNING_STATES,
    DisconnectedRuntime,
    architecture_diff,
    assess_integrity,
    command_effect,
    context_manifest,
    diversity_evidence,
    evaluate_selection,
    feedback_dimensions,
    generation_summary,
    integrity_projection,
    nodes_for_paths,
    normalize_activity,
    normalize_agent,
    normalize_descriptor,
    normalize_feedback,
    normalize_generation,
    normalize_integrity,
    normalize_policy,
    normalize_prototype,
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


class BuilderStore(BuilderRecords):
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
        return policy

    def create_generation(self, document: dict, actor: str) -> dict:
        spec = normalize_generation(document)
        case_id = spec["case_id"]
        with self.work.tx() as db:
            self._require_case(db, case_id)
            self._policy(db, spec["selection_policy_id"], spec["selection_policy_version"])
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
                (identifier, case_id, ordinal, spec["objective"], spec["base_revision"], spec["mode"], spec["isolation"],
                 spec["diversity_threshold"], spec["min_approaches"], spec["selection_policy_id"],
                 spec["selection_policy_version"], "drafting", actor, created),
            )
            generation = self._generation(db, identifier)
            self._append(db, case_id, identifier, "generation_created", actor, {
                "generation_id": identifier,
                "ordinal": ordinal,
                "objective": spec["objective"],
                "base_revision": spec["base_revision"],
                "mode": spec["mode"],
                "isolation": spec["isolation"],
                "min_approaches": spec["min_approaches"],
                "selection_policy_id": spec["selection_policy_id"],
                "selection_policy_version": spec["selection_policy_version"],
            })
            return generation

    def register_approach(self, generation_id: str, descriptor: dict, actor: str) -> dict:
        normalized = normalize_descriptor(descriptor)
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] != "drafting":
                raise Rejected("approach_frozen", "approaches are frozen once the generation leaves drafting")
            siblings = [item for item in self._approaches(db, generation_id) if item["state"] == "active"]
            report = diversity_evidence(normalized, siblings, generation["diversity_policy"]["threshold"])
            state = "active" if report["passed"] else "rejected"
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_approaches (id, generation_id, state, descriptor, evidence, created_at) VALUES (?,?,?,?,?,?)",
                (identifier, generation_id, state, canonical(normalized), canonical(report["evidence"]), created),
            )
            reason = None if report["passed"] else f"approach_too_similar:{report['sibling_id']}"
            body = {"approach_id": identifier, "state": state, "evidence": report["evidence"]}
            if reason:
                body["reason"] = reason
            self._append(db, generation["case_id"], generation_id, "approach_rejected" if reason else "approach_registered", actor, body)
            stored = self._approach(db, identifier)
        if reason:
            raise Rejected(reason, stored["id"])
        return stored

    def revise_approach(self, approach_id: str, descriptor: dict, actor: str) -> dict:
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
            report = diversity_evidence(normalized, siblings, generation["diversity_policy"]["threshold"])
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
        spec = normalize_agent(document)
        with self.work.tx() as db:
            generation = self._generation(db, spec["generation_id"])
            if generation["state"] not in LINKABLE_STATES:
                raise Rejected("generation_unsealed", "agents are assigned after the generation is sealed")
            self._require_frozen_approach(db, generation["id"], spec["approach_id"])
            identifier = _id()
            assignment = {"approach_id": spec["approach_id"], "objective": generation["objective"]}
            db.execute(
                "INSERT INTO builder_agents (id, case_id, generation_id, approach_id, candidate_id, principal, role, "
                "authority, assignment, working_set, state, hypothesis_summary, evidence_refs, open_uncertainties) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, generation["case_id"], spec["generation_id"], spec["approach_id"], None, spec["principal"],
                 spec["role"], spec["authority"], canonical(assignment), canonical(spec["working_set"]), "registered",
                 spec["hypothesis_summary"], canonical(spec["evidence_refs"]), canonical(spec["open_uncertainties"])),
            )
            self._append(db, generation["case_id"], spec["generation_id"], "agent_registered", actor, {
                "agent_id": identifier,
                "principal": spec["principal"],
                "role": spec["role"],
                "authority": spec["authority"],
                "approach_id": spec["approach_id"],
            })
            return self._agent(db, identifier)

    def record_activity(self, agent_id: str, document: dict, actor: str, roles: list[str] | None = None) -> dict:
        activity = normalize_activity(document)
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            if roles is not None and not (set(roles) & COORDINATION_ROLES) and actor != agent["principal"]:
                raise Rejected("forbidden", "an agent may record only its own activity")
            generation = self._generation(db, agent["generation_id"])
            if generation["state"] not in RUNNING_STATES:
                raise Rejected("generation_state", "agent activity starts after launch")
            for event_id in activity["caused_by"]:
                if not db.execute(
                    "SELECT 1 FROM coordination_events WHERE id=? AND case_id=?",
                    (event_id, agent["case_id"]),
                ).fetchone():
                    raise Rejected("unknown_cause", event_id)
            if generation["state"] == "launched":
                db.execute("UPDATE builder_generations SET state='evaluating' WHERE id=?", (generation["id"],))
            if activity["activity"] == "edit_scope":
                db.execute("UPDATE builder_agents SET working_set=? WHERE id=?", (canonical(activity["paths"]), agent_id))
            state = "complete" if activity["activity"] == "complete" else "active" if agent["state"] == "registered" else None
            if state:
                db.execute("UPDATE builder_agents SET state=? WHERE id=?", (state, agent_id))
            event = self._append(db, agent["case_id"], agent["generation_id"], "agent_activity_recorded", actor, {
                "agent_id": agent_id,
                **activity,
            })
            if state:
                self._append(db, agent["case_id"], agent["generation_id"], "agent_state_changed", actor, {
                    "agent_id": agent_id,
                    "state": state,
                    "caused_by": [event["id"]],
                })
            return event

    def command(self, agent_id: str, command: str, body: dict | None, actor: str, runtime=None) -> dict:
        runtime = runtime or DisconnectedRuntime()
        body = body or {}
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            self._prepare_command(db, agent, command, body)
        prepared = {"protocol": PROTOCOL, "agent_id": agent_id, "command": command, "body": body, "actor": actor}
        try:
            result = runtime.apply(prepared)
        except Exception:
            result = {"applied": False, "reason": "runtime_error"}
        if not isinstance(result, dict) or type(result.get("applied")) is not bool or not isinstance(result.get("reason"), str):
            result = {"applied": False, "reason": "runtime_error"}
        with self.work.tx() as db:
            agent = self._agent(db, agent_id)
            effect = self._prepare_command(db, agent, command, body)
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_commands (id, case_id, agent_id, command, body, result, actor, at) VALUES (?,?,?,?,?,?,?,?)",
                (identifier, agent["case_id"], agent_id, command, canonical(body), canonical(result), actor, created),
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
                for kind, payload in effect["events"]:
                    self._append(db, agent["case_id"], agent["generation_id"], kind, actor, {
                        "agent_id": agent_id, "command": command, "command_id": identifier, **payload,
                    })
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

    def link_candidate(self, generation_id: str, approach_id: str, candidate_id: str, actor: str, changed_paths: list | None = None) -> dict:
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise Rejected("invalid_candidate", "candidate id required")
        if changed_paths is not None and (
            not isinstance(changed_paths, list) or any(not isinstance(item, str) or not item.strip() for item in changed_paths)
        ):
            raise Rejected("invalid_candidate", "changed paths must be a list of strings")
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] not in LINKABLE_STATES:
                raise Rejected("generation_unsealed", "a candidate cannot link before the generation is sealed")
            approach = self._approach(db, approach_id)
            if approach["generation_id"] != generation_id or approach["state"] != "frozen":
                raise Rejected("candidate_approach", "candidate must belong to a sealed approach in this generation")
            authority = self._authority_candidate(db, generation["case_id"], candidate_id.strip())
            if authority is None:
                raise Rejected("candidate_unrecorded", "candidate is not in the case history")
            paths = list(authority["changed_paths"])
            if changed_paths is not None and sorted(item.strip() for item in changed_paths) != sorted(paths):
                raise Rejected("candidate_paths_mismatch", "changed paths come from the case candidate")
            existing = db.execute(
                "SELECT id FROM builder_candidate_links WHERE generation_id=? AND candidate_id=?",
                (generation_id, candidate_id.strip()),
            ).fetchone()
            if existing:
                raise Rejected("candidate_linked", "this candidate is already linked")
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_candidate_links (id, case_id, generation_id, approach_id, candidate_id, changed_paths, actor, at, authority) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (identifier, generation["case_id"], generation_id, approach_id, candidate_id.strip(), canonical(paths), actor, created,
                 canonical(authority)),
            )
            self._append(db, generation["case_id"], generation_id, "candidate_linked", actor, {
                "link_id": identifier,
                "approach_id": approach_id,
                "candidate_id": candidate_id.strip(),
                "changed_paths": paths,
                "commit": authority["commit"],
                "tree": authority["tree"],
                "link_state": "recorded",
                "selection_state": "eligible",
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
            self._resolve_target(db, document["case_id"], generation_id, feedback["target_kind"], feedback["target_id"])
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_feedback (id, case_id, generation_id, target_kind, target_id, kind, authorities, actor, body, at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, document["case_id"], generation_id, feedback["target_kind"], feedback["target_id"],
                 feedback["kind"], canonical(feedback["authorities"]), actor, canonical({"text": feedback["text"]}), created),
            )
            stored = self._feedback_row(db, identifier)
            self._append(db, document["case_id"], generation_id, "feedback_recorded", actor, {
                "feedback_id": identifier,
                "target_kind": stored["target_kind"],
                "target_id": stored["target_id"],
                "kind": stored["kind"],
                "authorities": stored["authorities"],
            })
            return stored

    def record_integrity(self, document: dict, actor: str, *, principal_kind: str | None = None, host: bool = False, verifier=None) -> dict:
        observation = assess_integrity(
            normalize_integrity(document),
            principal_kind=principal_kind,
            host=host,
            verifier=verifier,
        )
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
            self._resolve_target(db, case_id, generation_id, observation["target_kind"], observation["target_id"])
            identifier = _id()
            created = _now()
            db.execute(
                "INSERT INTO builder_integrity (id, case_id, generation_id, target_kind, target_id, claim, source, verifier, "
                "independence_claim, verification_status, verification_method, verified_independence, evidence_refs, result, digest, at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, case_id, generation_id, observation["target_kind"], observation["target_id"], observation["claim"],
                 observation["source"], observation["verifier"], observation["independence_claim"], observation["verification_status"],
                 observation["verification_method"], observation["verified_independence"], canonical(observation["evidence_refs"]),
                 observation["result"], observation["digest"], created),
            )
            stored = self._integrity_row(db, identifier)
            self._append(db, case_id, generation_id, "integrity_observation_recorded", actor, {
                "observation_id": identifier,
                "target_kind": stored["target_kind"],
                "target_id": stored["target_id"],
                "independence_claim": stored["independence_claim"],
                "verification_status": stored["verification_status"],
                "verified_independence": stored["verified_independence"],
                "result": stored["result"],
                "digest": stored["digest"],
            })
            return stored

    def record_host_integrity(self, document: dict, actor: str) -> dict:
        """Host mechanism only. The HTTP API does not call this."""
        return self.record_integrity(document, actor, host=True)

    def select(self, generation_id: str, actor: str, roles: list[str]) -> dict:
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            if generation["state"] not in RUNNING_STATES:
                raise Rejected("generation_state", "selection starts after launch")
            policy = self._policy(db, generation["selection_policy"]["id"], generation["selection_policy"]["version"])
            links = self._links(db, generation_id)
            feedback = self._feedback(db, generation_id)
            integrity = self._integrity(db, generation_id)
            evaluation = evaluate_selection(policy, [item["candidate_id"] for item in links], feedback, integrity, roles)
            evaluation["evidence_digest"] = digest({
                "advanced": evaluation["advanced"],
                "unresolved": evaluation["unresolved"],
                "rejected": evaluation["rejected"],
                "dimensions": evaluation["dimensions"],
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
                "policy_id": policy["id"],
                "policy_version": policy["version"],
                "evidence_digest": stored["evidence_digest"],
                "promotes_git": False,
                "advanced": [item["id"] for item in stored["advanced"]],
                "unresolved": [item["id"] for item in stored["unresolved"]],
                "rejected": [item["id"] for item in stored["rejected"]],
            })
            return stored

    def publish_architecture(self, document: dict, actor: str) -> dict:
        if not isinstance(document, dict) or not isinstance(document.get("case_id"), str):
            raise Rejected("invalid_architecture", "case_id required")
        case_id = document["case_id"]
        generation_id = document.get("generation_id")
        supplied = {key: value for key, value in document.items() if key != "id"}
        snapshot = validate_snapshot(supplied)
        with self.work.tx() as db:
            self._require_case(db, case_id)
            if generation_id is not None:
                generation = self._generation(db, generation_id)
                if generation["case_id"] != case_id:
                    raise Rejected("invalid_architecture", "generation is not in this case")
            identifier = _id()
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

    def register_prototype(self, document: dict, actor: str) -> dict:
        prototype = normalize_prototype(document)
        if not isinstance(document.get("case_id"), str):
            raise Rejected("invalid_prototype", "case_id required")
        generation_id = document.get("generation_id")
        with self.work.tx() as db:
            self._require_case(db, document["case_id"])
            if generation_id is not None:
                generation = self._generation(db, generation_id)
                if generation["case_id"] != document["case_id"]:
                    raise Rejected("invalid_prototype", "generation is not in this case")
            self._resolve_target(db, document["case_id"], generation_id, "candidate", prototype["candidate_id"])
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
        if state not in PROTOTYPE_STATES:
            raise Rejected("invalid_prototype", "prototype state is not recognized")
        with self.work.tx() as db:
            current = self._prototype(db, prototype_id)
            if state not in PROTOTYPE_TRANSITIONS[current["state"]]:
                raise Rejected("invalid_prototype", f"prototype cannot move from {current['state']} to {state}")
            db.execute("UPDATE builder_prototypes SET state=? WHERE id=?", (state, prototype_id))
            self._append(db, current["case_id"], current["generation_id"], "prototype_state_changed", actor, {
                "prototype_id": prototype_id,
                "prior_state": current["state"],
                "state": state,
            })
            return self._prototype(db, prototype_id)

    def manifest(self, agent_id: str) -> dict:
        with self.work.tx() as db:
            return self._manifest(db, self._agent(db, agent_id))

    def _manifest(self, db, agent: dict) -> dict:
        if agent["approach_id"] is None:
            raise Rejected("candidate_approach", "agent has no approach")
        generation = self._generation(db, agent["generation_id"])
        return context_manifest(
            generation,
            self._approach(db, agent["approach_id"]),
            self._approaches(db, generation["id"]),
            self._links(db, generation["id"]),
        )

    def _require_frozen_approach(self, db, generation_id: str, approach_id: str | None) -> None:
        if approach_id is None:
            return
        approach = self._approach(db, approach_id)
        if approach["generation_id"] != generation_id or approach["state"] != "frozen":
            raise Rejected("candidate_approach", "approach must be a sealed descriptor in this generation")

    def _prepare_command(self, db, agent: dict, command: str, body: dict) -> dict:
        if command == "redirect":
            self._require_frozen_approach(db, agent["generation_id"], body.get("approach_id"))
        return command_effect(agent, command, body)

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
                count = db.execute(
                    "SELECT COUNT(*) AS n FROM builder_approaches WHERE generation_id=? AND state!='rejected'",
                    (generation["id"],),
                ).fetchone()["n"]
                generations.append(generation_summary(generation, int(count)))
            event_count = db.execute("SELECT COUNT(*) AS n FROM events WHERE case_id=?", (case_id,)).fetchone()["n"]
        return {
            "protocol": PROTOCOL,
            "kind": "WorldProjection",
            "authoritative": False,
            "case_id": case_id,
            "objective": None if work is None else work["objective"],
            "generations": generations,
            "case_authority": {"event_count": int(event_count)},
        }

    def generation_view(self, generation_id: str) -> dict:
        with self.work.tx() as db:
            generation = self._generation(db, generation_id)
            approaches = self._approaches(db, generation_id)
            agents = self._agents(db, generation_id)
            links = self._links(db, generation_id)
            feedback = self._feedback(db, generation_id)
            integrity = self._integrity(db, generation_id)
            prototypes = self._prototypes(db, generation_id)
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
                    "WHERE case_id=? AND generation_id=? AND kind IN ('agent_activity_recorded', 'agent_state_changed', 'agent_assignment_changed') "
                    "ORDER BY seq",
                    (agent["case_id"], agent["generation_id"]),
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
            manifest = self._manifest(db, agent) if agent["approach_id"] else None
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

    def integrity_view(self, target: str, *, case_id: str, target_kind: str, generation_id: str | None = None) -> dict:
        if not isinstance(case_id, str) or not case_id or target_kind not in ("generation", "approach", "candidate", "architecture_node", "prototype", "case"):
            raise Rejected("invalid_integrity", "case_id and target_kind are required")
        with self.work.tx() as db:
            sql = "SELECT * FROM builder_integrity WHERE case_id=? AND target_kind=? AND target_id=?"
            params: list = [case_id, target_kind, target]
            if generation_id is not None:
                sql += " AND generation_id=?"
                params.append(generation_id)
            observations = [self._integrity_from_row(row) for row in db.execute(sql + " ORDER BY at", tuple(params)).fetchall()]
        return integrity_projection(observations, target)

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
        if not isinstance(policy_id, str) or not policy_id:
            raise Rejected("unknown_policy", "generation-less feedback must name its selection policy")
        row = db.execute(
            "SELECT body FROM builder_policies WHERE id=? ORDER BY version DESC LIMIT 1",
            (policy_id,),
        ).fetchone()
        if row is None:
            raise Rejected("unknown_policy", policy_id)
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

