"""Immutable contracts, case histories, deterministic admission and effects."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from syberlabs.admission import AdmissionContext, admit, approval_roles, explain, proposal_prefix
from syberlabs.bindings import bind_arguments, next_compiled
from syberlabs.canonical import canonical, digest
from syberlabs.clock import stamp
from syberlabs.contracts import check_case_inputs, compile_path, prepare_contract
from syberlabs.economic import policy_has_economic, receipt_matches, units, validate_policy_budgets
from syberlabs.errors import Rejected
from syberlabs.events import event_digest, verify_events
from syberlabs.evidence import acceptance_results, signer_is_effect_actor, verified_reconciliation
from syberlabs.evolution import (AUTOMATION_ROLES, candidate_record, candidate_views, evaluation_record, registered,
                                 search_record)
from syberlabs.jcs import envelope_jcs
from syberlabs.planner import HttpPlanner, planning_context
from syberlabs.targets import guard_request, trusted_origin
from syberlabs.values import at_path
from syberwork.storage import (
    ActionRegistry,
    CaseStore,
    ContractStore,
    EventStore,
    PolicyStore,
    ReservationStore,
    SourceRegistry,
    open_store,
)

__all__ = ["Rejected", "Work", "canonical", "digest", "trusted_origin"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward action credentials or source tokens to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(NoRedirect)


def _destination_code(exc: BaseException) -> str:
    """Stable code for a destination failure. The exception text is not stored."""
    if isinstance(exc, Rejected):
        return exc.code
    if isinstance(exc, urllib.error.HTTPError):
        return "destination_http"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "destination_unreachable"
    return "destination_error"


class Work:
    def __init__(self, database: str | Path, *, effects: str = "inline", trace=None):
        if effects not in ("inline", "worker"):
            raise Rejected("invalid_runtime", "effects must be inline or worker")
        self.database = str(database)
        self.effects = effects
        self.trace = trace
        self.metrics = {"admission": 0, "effect_claim": 0, "effect_settlement": 0, "effect_unknown": 0, "reconciliation": 0}
        self._lock = threading.Lock()
        self._db = open_store(database)

    def close(self) -> None:
        self._db.close()

    def __del__(self):
        try:
            self._db.close()
        except Exception:
            pass

    def _emit(self, name: str, **fields) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + 1
        if self.trace is not None:
            self.trace.record(name, **fields)

    @contextmanager
    def tx(self):
        """One connection for the life of this Work. Callers must not nest tx()."""
        with self._lock:
            self._db.begin()
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def install_contract(self, doc: dict) -> None:
        doc = prepare_contract(doc)
        with self.tx() as db:
            contracts = ContractStore(db)
            row = contracts.get(doc["id"], doc["version"])
            if row and row["body"] != canonical(doc):
                raise Rejected("immutable_contract", "publish a new version")
            contracts.insert_new(doc["id"], doc["version"], canonical(doc))

    @staticmethod
    def _compile_path(doc: dict) -> list[str]:
        return compile_path(doc)

    def artifacts(self, contract_id: str, version: int) -> dict:
        with self.tx() as db:
            contract = self._contract(db, contract_id, version)
            return {"contract": [contract_id, version], "path": contract["compiled_path"],
                    "input_form": contract["inputs"], "acceptance_queries": contract["acceptance"],
                    "action_gates": {name: {"required_facts": rule.get("required_facts", []),
                                            "arguments": rule.get("arguments", {}),
                                            "requires_effect": rule.get("requires_effect"),
                                            "approval_role": rule.get("approval_role")}
                                     for name, rule in contract["actions"].items()},
                    "note": "All artifacts were derived from the immutable stored contract; global policy remains independent"}

    def install_policy(self, doc: dict) -> None:
        if not isinstance(doc.get("version"), int) or not isinstance(doc.get("actions"), dict):
            raise Rejected("invalid_policy", "version and actions required")
        with self.tx() as db:
            policies = PolicyStore(db)
            current = policies.max_version()
            prior = policies.get(doc["version"])
            if prior and prior["body"] != canonical(doc):
                raise Rejected("immutable_policy", "publish a new version")
            if current is not None and doc["version"] < current and not prior:
                raise Rejected("policy_version", "new policy version must advance")
            if not prior and policy_has_economic(doc):
                prior_docs = [json.loads(row["body"]) for row in policies.bodies()]
                validate_policy_budgets(doc, prior_docs)
            policies.insert_new(doc["version"], canonical(doc))

    def install_action(self, name: str, doc: dict) -> None:
        if doc.get("kind") not in ("local", "http", "economic_http"):
            raise Rejected("invalid_action", "action kind must be local, http or economic_http")
        if doc["kind"] in ("http", "economic_http"):
            origin = trusted_origin(doc.get("url", ""))
            allowed_methods = ("POST",) if doc["kind"] == "economic_http" else ("POST", "PUT", "PATCH")
            if doc.get("method", "POST") not in allowed_methods:
                raise Rejected("invalid_action", "HTTP method must write")
        if doc["kind"] == "economic_http":
            if (not isinstance(doc.get("asset"), str) or not doc["asset"] or
                    not isinstance(doc.get("rail"), str) or not doc["rail"] or
                    not isinstance(doc.get("counterparty"), str) or not doc["counterparty"] or
                    doc.get("operation") not in ("purchase_capability", "transfer") or
                    not doc.get("status_url") or
                    (origin[0] == "https" and not doc.get("auth_env"))):
                raise Rejected("invalid_action", "economic adapter needs asset, rail, counterparty, credential and status lookup")
        status_url = doc.get("status_url")
        if status_url is not None:
            if (doc["kind"] not in ("http", "economic_http") or trusted_origin(status_url) != origin or
                    status_url.count("{key}") != 1 or
                    urlsplit(status_url).path.count("{key}") != 1 or
                    any(c in status_url.replace("{key}", "") for c in "{}")):
                raise Rejected("invalid_action", "status lookup needs one path key on the action origin")
        no_write = doc.get("no_write_statuses", [])
        if not isinstance(no_write, list) or any(type(code) is not int or code not in (409, 412, 428) for code in no_write):
            raise Rejected("invalid_action", "no-write statuses must be explicit precondition rejections")
        with self.tx() as db:
            actions = ActionRegistry(db)
            prior = actions.get(name)
            if prior and prior["body"] != canonical(doc):
                raise Rejected("immutable_action", "action definitions cannot change while cases exist; use a new name")
            actions.insert_new(name, canonical(doc))

    def install_source(self, name: str, doc: dict) -> None:
        url = doc.get("url") if isinstance(doc.get("url"), str) else ""
        try:
            trusted_origin(url)
            trusted = True
        except Rejected:
            trusted = False
        if doc.get("kind") != "http" or not trusted or "{key}" not in url:
            raise Rejected("invalid_source", "source needs a fixed HTTPS or local URL with {key}")
        with self.tx() as db:
            sources = SourceRegistry(db)
            prior = sources.get(name)
            if prior and prior["body"] != canonical(doc):
                raise Rejected("immutable_source", "publish a new source name")
            sources.insert_new(name, canonical(doc))

    def create_case(self, contract_id: str, version: int, inputs: dict, actor: str) -> str:
        with self.tx() as db:
            contract = self._contract(db, contract_id, version)
            check_case_inputs(contract["inputs"], inputs)
            case_id = str(uuid.uuid4())
            CaseStore(db).insert(case_id, contract_id, version, canonical(inputs), time.time())
            self._append(db, case_id, "case_created", {"actor": actor, "inputs": inputs, "contract": [contract_id, version]})
            return case_id

    @staticmethod
    def _task(history: list[dict], task_id: str) -> dict:
        task = next((event for event in history if event["kind"] == "resolution_requested" and event["body"]["id"] == task_id), None)
        if not task:
            raise Rejected("unknown_resolution", task_id)
        if any(event["kind"] == "resolution_completed" and event["body"]["task_id"] == task_id for event in history):
            raise Rejected("resolution_complete", "this task is already closed")
        return task

    def request_resolution(self, case_id: str, key: str, actor: str, roles: list[str]) -> dict:
        if "operator" not in roles:
            raise Rejected("resolution_denied", "operator role required to request clarification")
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            spec = contract.get("resolutions", {}).get(key)
            if not spec:
                raise Rejected("unknown_resolution", key)
            history = self._events(db, case_id)
            if any(e["kind"] == "case_cancelled" for e in history):
                raise Rejected("case_cancelled", "case is closed")
            if any(e["kind"] == "resolution_requested" and e["body"]["key"] == key for e in history):
                raise Rejected("resolution_exists", "a task for this decision already exists")

            def latest(descriptor):
                event = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == descriptor["key"]), None)
                if not event or event["body"]["source"] != descriptor["source"] or not event["body"].get("verified"):
                    raise Rejected("resolution_evidence_missing", descriptor["key"])
                return event

            trigger, choices = latest(spec["trigger"]), latest(spec["choices"])
            record_key = json.loads(row["inputs"])[spec["record_key_input"]]
            if any(at_path(event["body"]["value"], spec[field]["identity_path"]) != record_key
                   for event, field in ((trigger, "trigger"), (choices, "choices"))):
                raise Rejected("resolution_evidence_mismatch", "trigger and choices must belong to the case input")
            if at_path(trigger["body"]["value"], spec["trigger"]["missing_path"]) is not None:
                raise Rejected("resolution_unnecessary", "the authoritative source already has a decision")
            candidates = at_path(choices["body"]["value"], spec["choices"]["list_path"])
            if (not isinstance(candidates, list) or not candidates or
                    any(not isinstance(c, str) or not c for c in candidates) or len(set(candidates)) != len(candidates)):
                raise Rejected("resolution_evidence_missing", "choices must be distinct source-backed identifiers")
            now = time.time()
            return self._append(db, case_id, "resolution_requested", {
                "id": str(uuid.uuid4()), "key": key, "actor": actor, "owner_role": spec["owner_role"],
                "escalate_role": spec["escalate_role"], "due_at": now + spec["due_seconds"],
                "choices": candidates, "record_key": record_key,
                "trigger_version": trigger["body"]["version"], "trigger_hash": trigger["hash"],
                "choices_version": choices["body"]["version"], "choices_hash": choices["hash"],
            })

    def resolve_resolution(self, case_id: str, task_id: str, actor: str, roles: list[str]) -> dict:
        with self.tx() as db:
            row = self._case(db, case_id)
            history = self._events(db, case_id)
            task = self._task(history, task_id)["body"]
            if any(e["kind"] == "case_cancelled" for e in history):
                raise Rejected("case_cancelled", "case is closed")
            if task["owner_role"] not in roles:
                raise Rejected("resolution_denied", "assigned role required")
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            spec = contract["resolutions"][task["key"]]
        try:
            result = self.refresh_fact(case_id, spec["result"]["source"], spec["result"]["key"],
                                       task["record_key"], actor, roles)
            confirmation = None
            if "confirmation" in spec:
                check = spec["confirmation"]
                confirmation = self.refresh_fact(case_id, check["source"], check["key"],
                                                 task["record_key"], actor, roles)
        except Rejected as error:
            if error.code not in ("source_shape", "source_unavailable", "source_unversioned"):
                raise
            with self.tx() as db:
                self._task(self._events(db, case_id), task_id)
                self._append(db, case_id, "resolution_checked", {"task_id": task_id, "status": "pending", "reason": error.code, "actor": actor})
            return {"status": "pending", "reason": error.code}

        with self.tx() as db:
            history = self._events(db, case_id)
            self._task(history, task_id)
            if any(e["kind"] == "case_cancelled" for e in history):
                raise Rejected("case_cancelled", "case is closed")
            selected = at_path(result["body"]["value"], spec["result"]["value_path"])
            identity = at_path(result["body"]["value"], spec["result"]["identity_path"])
            confirmed = (not confirmation or at_path(confirmation["body"]["value"],
                         spec["confirmation"]["value_path"]) == selected)
            changed = (spec["trigger"]["key"] != spec["result"]["key"] or
                       result["body"]["version"] != task["trigger_version"])
            if (selected not in task["choices"] or identity != task["record_key"] or
                    not changed or not confirmed):
                self._append(db, case_id, "resolution_checked", {
                    "task_id": task_id, "status": "pending", "reason": "source_decision_unverified", "actor": actor,
                    "observation_hash": result["hash"],
                })
                return {"status": "pending", "reason": "source_decision_unverified"}
            event = self._append(db, case_id, "resolution_completed", {
                "task_id": task_id, "key": task["key"], "choice": selected, "actor": actor,
                "source": spec["result"]["source"], "version": result["body"]["version"],
                "observation_hash": result["hash"],
                "confirmation_hash": confirmation["hash"] if confirmation else None,
            })
            return {"status": "completed", "choice": selected, "event": event}

    def escalate_resolution(self, case_id: str, task_id: str, actor: str, roles: list[str]) -> dict:
        with self.tx() as db:
            history = self._events(db, case_id)
            task = self._task(history, task_id)["body"]
            if any(e["kind"] == "case_cancelled" for e in history):
                raise Rejected("case_cancelled", "case is closed")
            if task["escalate_role"] not in roles or time.time() < task["due_at"]:
                raise Rejected("resolution_escalation_denied", "escalation role and elapsed deadline required")
            if any(e["kind"] == "resolution_escalated" and e["body"]["task_id"] == task_id for e in history):
                raise Rejected("resolution_escalation_denied", "already escalated")
            return self._append(db, case_id, "resolution_escalated", {"task_id": task_id, "actor": actor, "role": task["escalate_role"]})

    def cancel_case(self, case_id: str, reason: str, actor: str, roles: list[str]) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise Rejected("cancellation_denied", "reason required")
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            if contract.get("cancel_role", "manager") not in roles:
                raise Rejected("cancellation_denied", "authorized cancellation role required")
            history = self._events(db, case_id)
            if any(e["kind"] == "case_cancelled" for e in history):
                raise Rejected("case_cancelled", "case is already closed")
            rejected = {e["body"]["proposal_id"] for e in history if e["kind"] == "effect_rejected"}
            for event in history:
                if event["kind"] != "effect_started" or event["body"]["proposal_id"] in rejected:
                    continue
                action = ActionRegistry(db).get(event["body"]["action"])
                if action and json.loads(action["body"])["kind"] in ("http", "economic_http"):
                    raise Rejected("cancellation_denied", "an external effect was claimed; verify its outcome first")
            return self._append(db, case_id, "case_cancelled", {"actor": actor, "role": contract.get("cancel_role", "manager"), "reason": reason.strip()})

    # Candidates. The caller is a search host (role "search" or "operator"); evidence comes
    # only from a separate "evaluator" credential that holds no automation role and did not
    # register the candidate. Validation is shared with syberlabs.Session.

    def _open_evolution(self, db, case_id):
        row = self._case(db, case_id)
        contract = self._contract(db, row["contract_id"], row["contract_version"])
        history = self._events(db, case_id)
        if any(e["kind"] == "case_cancelled" for e in history):
            raise Rejected("case_cancelled", "case is closed")
        return contract, history

    def record_candidate(self, case_id: str, candidate: dict, actor: str, roles: list[str]) -> dict:
        if not {"search", "operator"}.intersection(roles):
            raise Rejected("candidate_denied", "search or operator role required to register a candidate")
        with self.tx() as db:
            contract, history = self._open_evolution(db, case_id)
            return self._append(db, case_id, "candidate_registered", candidate_record(contract, history, candidate, actor))

    def _require_evaluator(self, roles: list[str]) -> None:
        if "evaluator" not in roles or AUTOMATION_ROLES.intersection(roles):
            raise Rejected("evaluation_denied", "an evaluator credential without model, compiled, or search roles is required")

    def _record_evaluation(self, db, case_id: str, evaluation: dict, actor: str, roles: list[str]) -> dict:
        """Append one evaluation on an open transaction. The caller commits."""
        self._require_evaluator(roles)
        contract, history = self._open_evolution(db, case_id)
        body = evaluation_record(contract, history, evaluation, actor)
        if registered(history, body["candidate"])["actor"] == actor:
            raise Rejected("evaluation_denied", "the actor that registered a candidate cannot evaluate it")
        return self._append(db, case_id, "candidate_evaluated", body)

    def record_evaluation(self, case_id: str, evaluation: dict, actor: str, roles: list[str]) -> dict:
        with self.tx() as db:
            return self._record_evaluation(db, case_id, evaluation, actor, roles)

    def record_search(self, case_id: str, phase: str, body: dict, actor: str, roles: list[str]) -> dict:
        if not {"search", "operator"}.intersection(roles):
            raise Rejected("candidate_denied", "search or operator role required to record a search")
        with self.tx() as db:
            contract, history = self._open_evolution(db, case_id)
            return self._append(db, case_id, "search_" + phase, search_record(contract, history, phase, body, actor))

    def candidates(self, case_id: str) -> list[dict]:
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            return candidate_views(contract, self._events(db, case_id), time.time())

    def observe(self, case_id: str, key: str, value: Any, source: str, version: str, actor: str, verified: bool = False) -> dict:
        if not all((key, source, version)):
            raise Rejected("invalid_observation", "key, source and source version are required")
        with self.tx() as db:
            self._case(db, case_id)
            return self._append(db, case_id, "observed", {"key": key, "value": value, "source": source, "version": version, "actor": actor, "verified": verified})

    def refresh_fact(self, case_id: str, source: str, key: str, record_key: str, actor: str, roles: list[str]) -> dict:
        with self.tx() as db:
            self._case(db, case_id)
            row = SourceRegistry(db).get(source)
            if not row:
                raise Rejected("unknown_source", source)
            config = json.loads(row["body"])
        if not set(config.get("roles", ["operator"])).intersection(roles):
            raise Rejected("source_denied", "actor lacks source read role")
        if not record_key or len(record_key) > 200:
            raise Rejected("invalid_record_key", "record key required, maximum 200 characters")
        import os
        headers = {"Accept": "application/json"}
        if config.get("auth_env"):
            token = os.getenv(config["auth_env"])
            if not token:
                raise Rejected("missing_credential", "source credential unavailable")
            headers["Authorization"] = "Bearer " + token
        target = config["url"].replace("{key}", quote(record_key, safe=""))
        guard_request(target)
        request = urllib.request.Request(target, headers=headers)
        try:
            with HTTP.open(request, timeout=min(30, config.get("timeout_seconds", 10))) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise Rejected("source_size", "source response exceeds 1 MB")
                document = json.loads(raw)
                version = response.headers.get("ETag") or response.headers.get("X-Record-Version")
        except urllib.error.HTTPError:
            raise Rejected("source_unavailable", "destination_http") from None
        except urllib.error.URLError:
            raise Rejected("source_unavailable", "destination_unreachable") from None
        except json.JSONDecodeError:
            raise Rejected("source_unavailable", "destination_error") from None
        if not version:
            raise Rejected("source_unversioned", "source must return ETag or X-Record-Version")
        value = document
        for field in config.get("value_path", []):
            if not isinstance(value, dict) or field not in value:
                raise Rejected("source_shape", "configured value path absent")
            value = value[field]
        return self.observe(case_id, key, value, source, version, actor, verified=True)

    def propose(self, case_id: str, action: str, args: dict, actor: str, roles: list[str], origin: str = "human") -> dict:
        if origin not in ("human", "model", "compiled"):
            raise Rejected("invalid_origin", "origin must be human, model or compiled")
        if origin != "human" and origin not in roles:
            raise Rejected("origin_denied", "the actor cannot claim this proposer origin")
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            policy = self._policy(db)
            history = self._events(db, case_id)
            proposal_id = str(uuid.uuid4())
            proposal = {"id": proposal_id, "action": action, "args": args, "actor": actor, "roles": roles, "origin": origin}
            self._append(db, case_id, "proposed", proposal)
            full = self._decision(contract, policy, history, proposal, time.time(), db)
            result = {"status": full["status"], "reason": full["reason"]}
            self._append(db, case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], **result}, rule=full["rule"])
            self._emit("admission", case_id=case_id, proposal_id=proposal_id, status=result["status"], reason=result["reason"], rule=full["rule"])
            return {"proposal": proposal, "decision": result}

    def compiled_propose(self, case_id: str, actor: str, roles: list[str]) -> dict:
        if "compiled" not in roles:
            raise Rejected("origin_denied", "compiled proposer credential required")
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            history = self._events(db, case_id)
            action = next_compiled(contract, history)
            if not action:
                raise Rejected("path_complete", "no remaining compiled step")
            args = bind_arguments(contract, history, action)
        return self.propose(case_id, action, args, actor, roles, origin="compiled")

    def model_propose(self, case_id: str, actor: str, roles: list[str], *, planner=None) -> dict:
        """Ask a planner for JSON; all output still passes through admission."""
        if "model" not in roles:
            raise Rejected("origin_denied", "model proposer credential required")
        if planner is None:
            planner = HttpPlanner()
        if isinstance(planner, HttpPlanner):
            planner.ensure_configured()
        case = self.inspect(case_id)
        suggestion = planner.propose(planning_context(case["contract"], case["acceptance"]))
        if not isinstance(suggestion, dict) or not isinstance(suggestion.get("action"), str) or not isinstance(suggestion.get("args"), dict):
            raise Rejected("planner_shape", "planner must return {action, args}")
        return self.propose(case_id, suggestion["action"], suggestion["args"], actor, roles, origin="model")

    def approve(self, case_id: str, proposal_id: str, actor: str, roles: list[str]) -> dict:
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            policy = self._policy(db)
            history = self._events(db, case_id)
            proposal = self._proposal(history, proposal_id)
            required_roles = self._approval_roles(contract, policy, proposal["action"])
            selected_role = next((r for r in required_roles if r in roles and not any(e["kind"] == "approved" and e["body"]["proposal_id"] == proposal_id and e["body"]["role"] == r for e in history)), None)
            if not selected_role or actor == proposal["actor"]:
                raise Rejected("approval_denied", "independent authorized approver required")
            result = self._append(db, case_id, "approved", {"proposal_id": proposal_id, "actor": actor, "role": selected_role, "args_hash": digest(proposal["args"])})
            return result

    def commit(self, case_id: str, proposal_id: str, actor: str) -> dict:
        # Claim one effect atomically. External I/O is performed only after the claim is durable.
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            policy = self._policy(db)
            history = self._events(db, case_id)
            proposal = self._proposal(history, proposal_id)
            if actor != proposal["actor"]:
                raise Rejected("actor_mismatch", "only the original proposer may commit")
            if any(e["kind"] in ("effect_started", "effect_succeeded", "effect_unknown", "effect_rejected") and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("effect_claimed", "already started; inspect or reconcile")
            full = self._decision(contract, policy, history, proposal, time.time(), db)
            decision = {"status": full["status"], "reason": full["reason"]}
            if decision["status"] != "allowed":
                self._append(db, case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], "phase": "commit", **decision}, rule=full["rule"])
                return {"decision": decision}
            action = json.loads(ActionRegistry(db).get(proposal["action"])["body"])
            extra = {}
            if action["kind"] == "economic_http":
                rule = policy["actions"][proposal["action"]]["economic"]
                amount = units(proposal["args"]["amount_units"])
                ReservationStore(db).reserve(proposal_id, case_id, rule["budget_id"], action["asset"], amount)
                extra = {"policy_snapshot": digest(policy),
                         "evidence_snapshot": digest(proposal["args"]["evidence"]),
                         "intent_snapshot": digest(proposal["args"]),
                         "budget_id": rule["budget_id"]}
            claim = self._append(db, case_id, "effect_started", {"proposal_id": proposal_id, "action": proposal["action"], "actor": actor, "policy_version": policy["version"], "idempotency_key": proposal_id, **extra})
            self._emit("effect_claim", case_id=case_id, proposal_id=proposal_id, mode=self.effects)
            if self.effects == "worker":
                db.execute(
                    "INSERT INTO effect_obligations (proposal_id, case_id, state, lease_until, attempts) VALUES (?,?,?,?,?)",
                    (proposal_id, case_id, "queued", None, 0),
                )
                return {"status": "queued", "proposal_id": proposal_id, "event": claim}
        return self.finish_claimed(case_id, proposal_id, proposal, action, claim)

    def finish_claimed(self, case_id: str, proposal_id: str, proposal: dict, action: dict, claim: dict) -> dict:
        """Run a claimed effect and settle it. The claim is already durable."""
        try:
            self._emit("effect_invocation", case_id=case_id, proposal_id=proposal_id, action=proposal["action"])
            output = self._execute(action, proposal["args"], proposal_id)
        except urllib.error.HTTPError as exc:
            if exc.code in action.get("no_write_statuses", []):
                with self.tx() as db:
                    settled = self._verified(db, case_id, proposal_id)
                    if settled:
                        self._close_obligation(db, proposal_id, "settled")
                        return {"status": "succeeded", "event": settled}
                    if action["kind"] == "economic_http":
                        ReservationStore(db).set_state(proposal_id, "released")
                    event = self._append(db, case_id, "effect_rejected", {
                        "proposal_id": proposal_id, "action": proposal["action"],
                        "status": exc.code, "claim_hash": claim["hash"],
                    })
                    self._close_obligation(db, proposal_id, "rejected")
                self._emit("effect_settlement", case_id=case_id, proposal_id=proposal_id, status="rejected")
                return {"status": "rejected", "event": event}
            return self._unknown_effect(case_id, proposal_id, proposal, exc)
        except Exception as exc:
            return self._unknown_effect(case_id, proposal_id, proposal, exc)
        with self.tx() as db:
            settled = self._verified(db, case_id, proposal_id)
            if settled:
                self._close_obligation(db, proposal_id, "settled")
                return {"status": "succeeded", "event": settled}
            if action["kind"] == "economic_http":
                ReservationStore(db).set_state(proposal_id, "settled")
            result = self._append(db, case_id, "effect_succeeded", {"proposal_id": proposal_id, "action": proposal["action"], "output": output, "claim_hash": claim["hash"]})
            self._close_obligation(db, proposal_id, "settled")
        self._emit("effect_settlement", case_id=case_id, proposal_id=proposal_id, status="succeeded")
        return {"status": "succeeded", "event": result}

    def _unknown_effect(self, case_id: str, proposal_id: str, proposal: dict, exc: BaseException) -> dict:
        with self.tx() as db:
            settled = self._verified(db, case_id, proposal_id)
            if settled:
                self._close_obligation(db, proposal_id, "settled")
                return {"status": "succeeded", "event": settled}
            self._append(db, case_id, "effect_unknown", {"proposal_id": proposal_id, "action": proposal["action"], "error": _destination_code(exc)})
            self._close_obligation(db, proposal_id, "unknown")
        self._emit("effect_unknown", case_id=case_id, proposal_id=proposal_id, error=_destination_code(exc))
        self._emit("effect_settlement", case_id=case_id, proposal_id=proposal_id, status="unknown")
        return {"status": "unknown", "proposal_id": proposal_id, "detail": "Check destination before reconciliation; execution might have succeeded"}

    def lease_obligation(self, *, now: float | None = None) -> dict | None:
        """Take one queued obligation, or report a lease that expired during execution."""
        now = time.time() if now is None else now
        with self.tx() as db:
            row = db.execute(
                "SELECT proposal_id, case_id, state FROM effect_obligations "
                "WHERE state='queued' OR (state='leased' AND lease_until IS NOT NULL AND lease_until <= ?) "
                "ORDER BY proposal_id LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            if row["state"] == "leased":
                return {"state": "interrupted", "case_id": row["case_id"], "proposal_id": row["proposal_id"]}
            db.execute(
                "UPDATE effect_obligations SET state='leased', lease_until=?, attempts=attempts+1 "
                "WHERE proposal_id=? AND state='queued'",
                (now + 30, row["proposal_id"]),
            )
            history = self._events(db, row["case_id"])
            proposal = self._proposal(history, row["proposal_id"])
            action = json.loads(ActionRegistry(db).get(proposal["action"])["body"])
            claim = next(event for event in history if event["kind"] == "effect_started" and event["body"]["proposal_id"] == row["proposal_id"])
            return {
                "state": "leased",
                "case_id": row["case_id"],
                "proposal_id": row["proposal_id"],
                "proposal": proposal,
                "action": action,
                "claim": claim,
            }

    def settle_interrupted(self, job: dict) -> dict:
        """A worker died during the destination call. The outcome stays unknown."""
        case_id, proposal_id = job["case_id"], job["proposal_id"]
        with self.tx() as db:
            history = self._events(db, case_id)
            if any(event["kind"] in ("effect_succeeded", "effect_rejected", "effect_unknown") and event["body"]["proposal_id"] == proposal_id for event in history):
                self._close_obligation(db, proposal_id, "settled")
                return {"status": "settled", "proposal_id": proposal_id}
            action = next(event["body"]["action"] for event in history if event["kind"] == "effect_started" and event["body"]["proposal_id"] == proposal_id)
            self._append(db, case_id, "effect_unknown", {"proposal_id": proposal_id, "action": action, "error": "worker_interrupted"})
            self._close_obligation(db, proposal_id, "unknown")
        self._emit("effect_unknown", case_id=case_id, proposal_id=proposal_id, error="worker_interrupted")
        self._emit("effect_settlement", case_id=case_id, proposal_id=proposal_id, status="unknown")
        return {"status": "unknown", "proposal_id": proposal_id, "detail": "worker interrupted during the destination call"}

    @staticmethod
    def _close_obligation(db, proposal_id: str, state: str) -> None:
        db.execute(
            "UPDATE effect_obligations SET state=?, lease_until=NULL WHERE proposal_id=?",
            (state, proposal_id),
        )

    def reconcile(self, case_id: str, proposal_id: str, actor: str, roles: list[str], *, success=None, evidence=None) -> dict:
        """Check the installed destination's authoritative status; caller supplies no outcome."""
        if success is not None or evidence is not None:
            raise Rejected("manual_reconciliation_disabled", "the destination must report the outcome")
        if "manager" not in roles:
            raise Rejected("reconciliation_denied", "manager role required")
        with self.tx() as db:
            history = self._events(db, case_id)
            proposal = self._proposal(history, proposal_id)
            if not any(e["kind"] in ("effect_started", "effect_unknown") and e["body"]["proposal_id"] == proposal_id for e in history) or any(
                    e["kind"] in ("effect_succeeded", "effect_rejected") and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "no unresolved effect")
            if any(verified_reconciliation(e) and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "already reconciled")
            config = json.loads(ActionRegistry(db).get(proposal["action"])["body"])
            status_url = config.get("status_url")
            if not status_url:
                raise Rejected("reconciliation_unavailable", "action has no installed destination status lookup")
        import os
        headers = {"Accept": "application/json"}
        if config.get("auth_env"):
            token = os.getenv(config["auth_env"])
            if not token:
                raise Rejected("missing_credential", "destination status credential unavailable")
            headers["Authorization"] = "Bearer " + token
        target = status_url.replace("{key}", quote(proposal_id, safe=""))
        guard_request(target)
        request = urllib.request.Request(target, headers=headers)
        try:
            with HTTP.open(request, timeout=min(30, config.get("timeout_seconds", 10))) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("status response exceeds 64 KB")
                record = json.loads(raw)
        except urllib.error.HTTPError as exc:
            status, reason = ("pending", "not_found_not_proof") if exc.code == 404 else ("pending", "status_unavailable")
            record = None
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            status, reason, record = "pending", "status_unavailable", None
        else:
            matched = False
            if config.get("kind") == "economic_http":
                matched = receipt_matches(record, proposal_id, proposal["args"], digest(proposal["args"]))
            elif (isinstance(record, dict) and record.get("state") == "committed"
                    and record.get("idempotency_key") == proposal_id
                    and record.get("request_digest") == digest(proposal["args"])
                    and isinstance(record.get("external_id"), str) and record["external_id"]):
                matched = True
            if matched:
                status, reason = "verified", "destination_record_matched"
            else:
                status, reason = "unverified", "destination_record_mismatch"
        with self.tx() as db:
            history = self._events(db, case_id)
            if any(verified_reconciliation(e) and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "already reconciled")
            if any(e["kind"] in ("effect_succeeded", "effect_rejected") and
                   e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "effect resolved during status lookup")
            if status == "verified":
                if config.get("kind") == "economic_http":
                    ReservationStore(db).set_state(proposal_id, "settled", only_reserved=True)
                proof = {"verified": True, "external_id": record["external_id"],
                         "request_digest": record["request_digest"], "response_digest": digest(record),
                         "idempotency_key": proposal_id}
                if config.get("kind") == "economic_http":
                    proof.update({"amount_units": record["amount_units"], "asset": record["asset"],
                                  "counterparty": record["counterparty"], "rail": config["rail"]})
                event = self._append(db, case_id, "reconciled", {
                    "proposal_id": proposal_id, "action": proposal["action"],
                    "success": True, "proof": proof, "actor": actor,
                })
            else:
                event = self._append(db, case_id, "reconciliation_checked", {
                    "proposal_id": proposal_id, "action": proposal["action"],
                    "status": status, "reason": reason, "actor": actor,
                })
            self._emit("reconciliation", case_id=case_id, proposal_id=proposal_id, status=status, reason=reason)
            return {"status": status, "reason": reason, "event": event}

    def signoff(self, case_id: str, actor: str, roles: list[str], role: str) -> dict:
        if role not in roles:
            raise Rejected("signoff_denied", "actor lacks role")
        with self.tx() as db:
            row = self._case(db, case_id)
            history = self._events(db, case_id)
            if any(e["kind"] == "case_cancelled" for e in history):
                raise Rejected("case_cancelled", "case is closed")
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            if signer_is_effect_actor(contract, history, actor, role):
                raise Rejected("signoff_denied", "signer must differ from the effect actor")
            return self._append(db, case_id, "signed", {"actor": actor, "role": role})

    def inspect(self, case_id: str) -> dict:
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            history = self._events(db, case_id)
            clauses = self._acceptance(contract, history)
            cancelled = any(e["kind"] == "case_cancelled" for e in history)
            resolutions = []
            for event in history:
                if event["kind"] != "resolution_requested":
                    continue
                task = event["body"]
                closed = next((e for e in history if e["kind"] == "resolution_completed" and e["body"]["task_id"] == task["id"]), None)
                escalated = any(e["kind"] == "resolution_escalated" and e["body"]["task_id"] == task["id"] for e in history)
                resolutions.append({**task, "status": "completed" if closed else "cancelled" if cancelled else "escalated" if escalated else "overdue" if time.time() >= task["due_at"] else "open",
                                    "choice": closed["body"]["choice"] if closed else None})
            found = {"case": dict(row), "contract": contract, "events": history,
                     "acceptance": clauses, "complete": not cancelled and bool(clauses) and all(v["passed"] for v in clauses),
                     "status": "cancelled" if cancelled else "complete" if bool(clauses) and all(v["passed"] for v in clauses) else "in_progress",
                     "resolutions": resolutions,
                     "next_compiled": None if cancelled else self._next_path(contract, history)}
            if "evolution" in contract:
                found["candidates"] = candidate_views(contract, history, time.time())
            return found

    def list_cases(self) -> list[dict]:
        with self.tx() as db:
            return [dict(row) for row in CaseStore(db).list_all()]

    def replay(self, case_id: str, contract_version: int, policy_version: int) -> dict:
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], contract_version)
            policy = self._policy(db, policy_version)
            events = self._events(db, case_id)
            changed = []
            for index, event in enumerate(events):
                if event["kind"] != "proposed":
                    continue
                proposal = event["body"]
                original = next((e["body"] for e in events[index+1:] if e["kind"] == "decision" and e["body"]["proposal_id"] == proposal["id"]), None)
                updated = self._admit(contract, policy, events[:index], proposal, event["at"], db)
                if original and (original["status"], original.get("reason")) != (updated["status"], updated.get("reason")):
                    changed.append({"proposal_id": proposal["id"], "action": proposal["action"], "before": original, "after": updated})
            original_contract = self._contract(db, row["contract_id"], row["contract_version"])
            return {"case_id": case_id, "contract_version": contract_version, "policy_version": policy_version,
                    "changed_decisions": changed, "acceptance_before": self._acceptance(original_contract, events),
                    "acceptance_after": self._acceptance(contract, events),
                    "note": "Counterfactual against captured case events; does not re-read or mutate external systems"}

    def submit_witness(self, case_id: str, client) -> dict:
        """Ask an external witness to sign this case. This store has no signing seed."""
        if not self.verify_chain(case_id):
            raise Rejected("invalid_chain", "case history does not verify")
        with self.tx() as db:
            events = self._events(db, case_id)
        return client.submit(events)

    def chain_ok(self, db, case_id: str) -> bool:
        """Check one case on a connection the caller already holds."""
        events = self._events(db, case_id)
        if not verify_events(events):
            return False
        stored = EventStore(db).side_digests(case_id)
        for event in events:
            digest = stored.get(event["seq"])
            if isinstance(digest, str) and digest != envelope_jcs(event):
                return False
        return True

    def verify_chain(self, case_id: str) -> bool:
        with self.tx() as db:
            return self.chain_ok(db, case_id)

    def side_channel(self, case_id: str) -> list[dict]:
        """JCS digest and deciding rule id for each new event. Neither is in the event hash.

        Events written before this side table existed have no row.
        """
        with self.tx() as db:
            self._case(db, case_id)
            rows = EventStore(db).side(case_id)
            return [{"seq": row["seq"], "jcs": row["jcs"], "rule": row["rule"]} for row in rows]

    @staticmethod
    def _execute(action: dict, args: dict, key: str) -> dict:
        if action["kind"] == "local":
            return {"recorded": True, "args": args}
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        if "auth_env" in action:
            import os
            token = os.getenv(action["auth_env"])
            if not token:
                raise Rejected("missing_credential", "configured action credential unavailable")
            headers["Authorization"] = f"Bearer {token}"
        if "version_arg" in action and action["version_arg"] in args:
            headers["If-Match"] = str(args[action["version_arg"]])
        guard_request(action["url"])
        req = urllib.request.Request(action["url"], data=canonical(args).encode(), headers=headers, method=action.get("method", "POST"))
        with HTTP.open(req, timeout=min(30, action.get("timeout_seconds", 10))) as response:
            raw = response.read(32768)
            if action["kind"] == "economic_http":
                record = json.loads(raw)
                if not receipt_matches(record, key, args, digest(args)):
                    raise ValueError("settlement receipt does not match the authorized intent")
                return {"settlement": record}
            return {"status": response.status, "body": raw.decode("utf-8", errors="replace")}

    @staticmethod
    def _case(db, case_id):
        row = CaseStore(db).get(case_id)
        if not row:
            raise Rejected("unknown_case", case_id)
        return row

    @staticmethod
    def _contract(db, contract_id, version):
        row = ContractStore(db).get(contract_id, version)
        if not row:
            raise Rejected("unknown_contract", f"{contract_id}@{version}")
        return json.loads(row["body"])

    @staticmethod
    def _policy(db, version=None):
        policies = PolicyStore(db)
        row = policies.get(version) if version is not None else policies.latest()
        if not row:
            raise Rejected("unknown_policy", "install policy first")
        return json.loads(row["body"])

    @staticmethod
    def _events(db, case_id):
        events = []
        rows = EventStore(db).read(case_id)
        for row in rows:
            at = json.loads(row["at_json"]) if row["at_json"] is not None else row["at"]
            events.append({
                "case_id": row["case_id"],
                "seq": row["seq"],
                "kind": row["kind"],
                "body": json.loads(row["body"]),
                "at": at,
                "previous": row["previous"],
                "hash": row["hash"],
            })
        return events

    @staticmethod
    def _append(db, case_id, kind, body, *, rule=None):
        events = EventStore(db)
        previous = events.previous(case_id)
        event = {"case_id": case_id, "seq": previous["seq"] + 1 if previous else 1, "kind": kind,
                 "body": body, "at": stamp(time.time()), "previous": previous["hash"] if previous else "0" * 64}
        event["hash"] = event_digest(event)
        events.append(case_id, event["seq"], kind, canonical(body), event["at"], event["previous"], event["hash"], json.dumps(event["at"]))
        events.append_side(case_id, event["seq"], envelope_jcs(event), rule if kind == "decision" else None)
        return event

    def _verified(self, db, case_id, proposal_id):
        """A verified reconciliation already recorded for this proposal, if any."""
        return next((e for e in self._events(db, case_id) if verified_reconciliation(e) and
                     e["body"]["proposal_id"] == proposal_id), None)

    @staticmethod
    def _proposal(history, proposal_id):
        for e in history:
            if e["kind"] == "proposed" and e["body"]["id"] == proposal_id:
                return e["body"]
        raise Rejected("unknown_proposal", proposal_id)

    @staticmethod
    def _approval_roles(contract, policy, action):
        return approval_roles(contract, policy, action)

    def explain_admission(self, case_id: str, proposal_id: str, *, when: str = "now") -> dict:
        """Admission for a stored proposal, including the deciding rule.

        ``when="now"`` rechecks the current history. ``when="recorded"`` uses the
        prefix and policy version of the original decision. Neither appends an event.
        """
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            full = self._events(db, case_id)
            proposal = self._proposal(full, proposal_id)
            policy = self._policy(db)
            history = full
            now = time.time()
            if when == "recorded":
                prefix = proposal_prefix(full, proposal_id)
                if prefix is None:
                    raise Rejected("unknown_proposal", proposal_id)
                history, now = prefix
                recorded = next((event["body"] for event in full if event["kind"] == "decision" and event["body"].get("proposal_id") == proposal_id), None)
                if recorded and "policy_version" in recorded:
                    policy = self._policy(db, recorded["policy_version"])
            elif when != "now":
                raise Rejected("invalid_explain", "when must be now or recorded")
            return explain(self._context(contract, policy, history, proposal, now, db))

    def bind_organization(self, organization: str) -> dict:
        """Bind this database to one organization. A second organization is refused."""
        if not isinstance(organization, str) or not organization.strip():
            raise Rejected("invalid_cell", "organization name required")
        name = organization.strip()
        with self.tx() as db:
            prior = db.execute("SELECT organization, cell_version FROM cell WHERE id=1").fetchone()
            if prior and prior["organization"] != name:
                raise Rejected("cell_bound", "this database already belongs to another organization")
            if not prior:
                db.execute("INSERT INTO cell VALUES (1, ?, ?)", (name, "0.1"))
            row = db.execute("SELECT organization, cell_version FROM cell WHERE id=1").fetchone()
        return {"organization": row["organization"], "cell_version": row["cell_version"], "isolation": "cell"}

    def register_principal(self, principal_id: str, kind: str, name: str, organization: str, roles: list[str], *, token: str | None = None, delegation: str | None = None) -> dict:
        """Record who can act in this cell. The token is stored only as a hash."""
        if kind not in ("human", "service", "agent"):
            raise Rejected("invalid_principal", "principal kind must be human, service, or agent")
        if not isinstance(principal_id, str) or not principal_id or not isinstance(name, str) or not name:
            raise Rejected("invalid_principal", "principal id and name are required")
        if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
            raise Rejected("invalid_principal", "roles must be a list of strings")
        cell = self.bind_organization(organization)
        token_hash = hashlib.sha256(token.encode()).hexdigest() if token else None
        with self.tx() as db:
            prior = db.execute("SELECT id FROM principals WHERE id=?", (principal_id,)).fetchone()
            if prior:
                raise Rejected("principal_exists", principal_id)
            db.execute(
                "INSERT INTO principals VALUES (?,?,?,?,?,?,?)",
                (principal_id, kind, name, cell["organization"], token_hash, json.dumps(roles), delegation),
            )
        return {"id": principal_id, "kind": kind, "name": name, "organization": cell["organization"], "roles": roles, "delegation": delegation}

    def principal_for_token(self, token: str) -> dict | None:
        if not isinstance(token, str) or not token:
            return None
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self.tx() as db:
            row = db.execute(
                "SELECT id, kind, name, organization, roles, delegation FROM principals WHERE token_hash=?",
                (hashed,),
            ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "roles": json.loads(row["roles"]),
            "sources": [],
            "principal_id": row["id"],
            "kind": row["kind"],
            "organization": row["organization"],
            "delegation": row["delegation"],
        }

    def _context(self, contract, policy, history, proposal, now, db):
        actions = ActionRegistry(db)
        installed = actions.names()
        configs = {name: json.loads(body) for name, body in actions.documents().items()}

        def reserved(budget_id, asset):
            return ReservationStore(db).reserved_total(budget_id, asset)

        return AdmissionContext(contract, policy, history, proposal, now, installed, configs, reserved)

    def _decision(self, contract, policy, history, proposal, now, db):
        return admit(self._context(contract, policy, history, proposal, now, db))

    def _admit(self, contract, policy, history, proposal, now, db):
        decision = self._decision(contract, policy, history, proposal, now, db)
        return {"status": decision["status"], "reason": decision["reason"]}

    @staticmethod
    def _acceptance(contract, history):
        return acceptance_results(contract, history)

    @staticmethod
    def _next_path(contract, history):
        return next_compiled(contract, history)
