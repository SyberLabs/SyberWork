"""Row decoders for Builder coordination tables."""

from __future__ import annotations

import json

from syberlabs.builder import (
    PROTOCOL,
    feedback_dimensions,
    integrity_projection,
    nodes_for_paths,
)
from syberlabs.errors import Rejected


def _loads(value: str):
    return json.loads(value)


class BuilderRecords:
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
            "SELECT * FROM builder_approaches WHERE generation_id=? ORDER BY created_at, id",
            (generation_id,),
        ).fetchall()
        return [self._approach_row(row) for row in rows]

    def _approach(self, db, approach_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_approaches WHERE id=?", (approach_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_approach", approach_id)
        return self._approach_row(row)

    def _approach_row(self, row) -> dict:
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
        return self._agent_row(row)

    def _agent_row(self, row) -> dict:
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
        rows = db.execute("SELECT * FROM builder_agents WHERE generation_id=? ORDER BY id", (generation_id,)).fetchall()
        return [self._agent_row(row) for row in rows]

    def _link(self, db, link_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_candidate_links WHERE id=?", (link_id,)).fetchone()
        if row is None:
            raise Rejected("unknown_candidate", link_id)
        return self._link_row(row)

    def _link_row(self, row) -> dict:
        authority = _loads(row["authority"])
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "approach_id": row["approach_id"],
            "candidate_id": row["candidate_id"],
            "changed_paths": list(authority["changed_paths"]),
            "commit": authority["commit"],
            "tree": authority["tree"],
            "base": authority["base"],
            "operator": authority["operator"],
            "provider": authority["provider"],
            "link_state": "recorded",
            "selection_state": "eligible",
            "actor": row["actor"],
            "at": row["at"],
        }

    def _authority_candidate(self, db, case_id: str, candidate_id: str):
        for row in db.execute(
            "SELECT body FROM events WHERE case_id=? AND kind='candidate_registered'",
            (case_id,),
        ).fetchall():
            body = _loads(row["body"])
            if body.get("id") == candidate_id:
                return {
                    "id": body["id"],
                    "commit": body["commit"],
                    "tree": body["tree"],
                    "base": body["base"],
                    "operator": body["operator"],
                    "provider": body["provider"],
                    "changed_paths": list(body["changed_paths"]),
                }
        return None

    def _resolve_target(self, db, case_id: str, generation_id: str | None, target_kind: str, target_id: str) -> None:
        if target_kind == "case":
            if target_id != case_id:
                raise Rejected("unknown_target", "case target must be this case")
            return
        if target_kind == "generation":
            generation = self._generation(db, target_id)
            if generation["case_id"] != case_id or generation_id not in (None, target_id):
                raise Rejected("unknown_target", "generation is not in this case")
            return
        if generation_id is None:
            raise Rejected("unknown_target", "this target needs a generation")
        generation = self._generation(db, generation_id)
        if generation["case_id"] != case_id:
            raise Rejected("unknown_target", "generation is not in this case")
        if target_kind == "approach":
            approach = self._approach(db, target_id)
            if approach["generation_id"] != generation_id:
                raise Rejected("unknown_target", "approach is not in this generation")
            return
        if target_kind == "candidate":
            link = db.execute(
                "SELECT id FROM builder_candidate_links WHERE generation_id=? AND candidate_id=?",
                (generation_id, target_id),
            ).fetchone()
            if link is None or self._authority_candidate(db, case_id, target_id) is None:
                raise Rejected("unknown_target", "candidate is not linked in this generation")
            return
        if target_kind == "prototype":
            row = db.execute(
                "SELECT case_id, generation_id FROM builder_prototypes WHERE id=?",
                (target_id,),
            ).fetchone()
            if row is None or row["case_id"] != case_id or row["generation_id"] != generation_id:
                raise Rejected("unknown_target", "prototype is not in this generation")
            return
        if target_kind == "architecture_node":
            snapshot = self._latest_snapshot(db, case_id, generation_id)
            if snapshot is None or not any(node["id"] == target_id for node in snapshot["nodes"]):
                raise Rejected("unknown_target", "architecture node is not in the current snapshot")
            return
        raise Rejected("unknown_target", "target is not recognized")

    def _links(self, db, generation_id: str) -> list[dict]:
        rows = db.execute(
            "SELECT * FROM builder_candidate_links WHERE generation_id=? ORDER BY at, id",
            (generation_id,),
        ).fetchall()
        return [self._link_row(row) for row in rows]

    def _feedback_row(self, db, feedback_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_feedback WHERE id=?", (feedback_id,)).fetchone()
        return self._feedback_from_row(row)

    def _feedback_from_row(self, row) -> dict:
        return {
            "protocol": PROTOCOL,
            "id": row["id"],
            "case_id": row["case_id"],
            "generation_id": row["generation_id"],
            "target_kind": row["target_kind"],
            "target_id": row["target_id"],
            "kind": row["kind"],
            "authorities": _loads(row["authorities"]),
            "actor": row["actor"],
            "text": _loads(row["body"])["text"],
            "at": row["at"],
        }

    def _feedback(self, db, generation_id: str) -> list[dict]:
        rows = db.execute("SELECT * FROM builder_feedback WHERE generation_id=? ORDER BY at", (generation_id,)).fetchall()
        return [self._feedback_from_row(row) for row in rows]

    def _integrity_row(self, db, observation_id: str) -> dict:
        row = db.execute("SELECT * FROM builder_integrity WHERE id=?", (observation_id,)).fetchone()
        return self._integrity_from_row(row)

    def _integrity_from_row(self, row) -> dict:
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
            "independence_claim": row["independence_claim"],
            "verification_status": row["verification_status"],
            "verification_method": row["verification_method"],
            "verified_independence": row["verified_independence"],
            "evidence_refs": _loads(row["evidence_refs"]),
            "result": row["result"],
            "digest": row["digest"],
            "at": row["at"],
        }

    def _integrity(self, db, generation_id: str) -> list[dict]:
        rows = db.execute("SELECT * FROM builder_integrity WHERE generation_id=? ORDER BY at", (generation_id,)).fetchall()
        return [self._integrity_from_row(row) for row in rows]

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
        return self._prototype_row(row)

    def _prototype_row(self, row) -> dict:
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

    def _prototypes(self, db, generation_id: str) -> list[dict]:
        rows = db.execute("SELECT * FROM builder_prototypes WHERE generation_id=? ORDER BY created_at", (generation_id,)).fetchall()
        return [self._prototype_row(row) for row in rows]

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
            "commit": link["commit"],
            "tree": link["tree"],
            "base": link["base"],
            "operator": link["operator"],
            "provider": link["provider"],
            "link_state": link["link_state"],
            "selection_state": link["selection_state"],
            "architecture_nodes": nodes,
            "prototype": prototype,
            "feedback_dimensions": feedback_dimensions(notes),
            "integrity": integrity_projection(observations, link["candidate_id"]),
            "case_candidate_recorded": link["link_state"] == "recorded",
            "promotion_state": None,
        }
