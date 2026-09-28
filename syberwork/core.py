"""Immutable contracts, case histories, deterministic admission and effects."""

from __future__ import annotations

import json
import sqlite3
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
    def __init__(self, database: str | Path):
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.database, timeout=15, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=15000")
        self._db.executescript("""
                CREATE TABLE IF NOT EXISTS contracts (
                    id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(id, version));
                CREATE TABLE IF NOT EXISTS policies (
                    version INTEGER PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (
                    name TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                    name TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, contract_id TEXT NOT NULL,
                    contract_version INTEGER NOT NULL, inputs TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    case_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                    body TEXT NOT NULL, at REAL NOT NULL, previous TEXT NOT NULL,
                    hash TEXT NOT NULL, at_json TEXT, PRIMARY KEY(case_id,seq));
                CREATE TABLE IF NOT EXISTS economic_reservations (
                    proposal_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    budget_id TEXT NOT NULL, asset TEXT NOT NULL,
                    amount_units INTEGER NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS event_side (
                    case_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    jcs TEXT, rule TEXT, PRIMARY KEY(case_id, seq));
            """)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(events)")}
        if "at_json" not in columns:
            self._db.execute("ALTER TABLE events ADD COLUMN at_json TEXT")

    def close(self) -> None:
        self._db.close()

    def __del__(self):
        try:
            self._db.close()
        except Exception:
            pass

    @contextmanager
    def tx(self):
        """One connection for the life of this Work. Callers must not nest tx()."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass
                raise

    def install_contract(self, doc: dict) -> None:
        doc = prepare_contract(doc)
        with self.tx() as db:
            row = db.execute("SELECT body FROM contracts WHERE id=? AND version=?", (doc["id"], doc["version"])).fetchone()
            if row and row["body"] != canonical(doc):
                raise Rejected("immutable_contract", "publish a new version")
            db.execute("INSERT OR IGNORE INTO contracts VALUES (?,?,?)", (doc["id"], doc["version"], canonical(doc)))

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
            current = db.execute("SELECT max(version) AS v FROM policies").fetchone()["v"]
            prior = db.execute("SELECT body FROM policies WHERE version=?", (doc["version"],)).fetchone()
            if prior and prior["body"] != canonical(doc):
                raise Rejected("immutable_policy", "publish a new version")
            if current is not None and doc["version"] < current and not prior:
                raise Rejected("policy_version", "new policy version must advance")
            if not prior and policy_has_economic(doc):
                prior_docs = [json.loads(row["body"]) for row in db.execute("SELECT body FROM policies")]
                validate_policy_budgets(doc, prior_docs)
            db.execute("INSERT OR IGNORE INTO policies VALUES (?,?)", (doc["version"], canonical(doc)))

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
            prior = db.execute("SELECT body FROM actions WHERE name=?", (name,)).fetchone()
            if prior and prior["body"] != canonical(doc):
                raise Rejected("immutable_action", "action definitions cannot change while cases exist; use a new name")
            db.execute("INSERT OR IGNORE INTO actions VALUES (?,?)", (name, canonical(doc)))

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
            prior = db.execute("SELECT body FROM sources WHERE name=?", (name,)).fetchone()
            if prior and prior["body"] != canonical(doc):
                raise Rejected("immutable_source", "publish a new source name")
            db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (name, canonical(doc)))

    def create_case(self, contract_id: str, version: int, inputs: dict, actor: str) -> str:
        with self.tx() as db:
            contract = self._contract(db, contract_id, version)
            check_case_inputs(contract["inputs"], inputs)
            case_id = str(uuid.uuid4())
            db.execute("INSERT INTO cases VALUES (?,?,?,?,?)", (case_id, contract_id, version, canonical(inputs), time.time()))
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
                action = db.execute("SELECT body FROM actions WHERE name=?", (event["body"]["action"],)).fetchone()
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

    def record_evaluation(self, case_id: str, evaluation: dict, actor: str, roles: list[str]) -> dict:
        if "evaluator" not in roles or AUTOMATION_ROLES.intersection(roles):
            raise Rejected("evaluation_denied", "an evaluator credential without model, compiled, or search roles is required")
        with self.tx() as db:
            contract, history = self._open_evolution(db, case_id)
            body = evaluation_record(contract, history, evaluation, actor)
            if registered(history, body["candidate"])["actor"] == actor:
                raise Rejected("evaluation_denied", "the actor that registered a candidate cannot evaluate it")
            return self._append(db, case_id, "candidate_evaluated", body)

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
            row = db.execute("SELECT body FROM sources WHERE name=?", (source,)).fetchone()
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
            action = json.loads(db.execute("SELECT body FROM actions WHERE name=?", (proposal["action"],)).fetchone()["body"])
            extra = {}
            if action["kind"] == "economic_http":
                rule = policy["actions"][proposal["action"]]["economic"]
                amount = units(proposal["args"]["amount_units"])
                db.execute("INSERT INTO economic_reservations VALUES (?,?,?,?,?,?)", (
                    proposal_id, case_id, rule["budget_id"], action["asset"], amount, "reserved"))
                extra = {"policy_snapshot": digest(policy),
                         "evidence_snapshot": digest(proposal["args"]["evidence"]),
                         "intent_snapshot": digest(proposal["args"]),
                         "budget_id": rule["budget_id"]}
            claim = self._append(db, case_id, "effect_started", {"proposal_id": proposal_id, "action": proposal["action"], "actor": actor, "policy_version": policy["version"], "idempotency_key": proposal_id, **extra})
        try:
            output = self._execute(action, proposal["args"], proposal_id)
        except urllib.error.HTTPError as exc:
            if exc.code in action.get("no_write_statuses", []):
                with self.tx() as db:
                    settled = self._verified(db, case_id, proposal_id)
                    if settled:
                        return {"status": "succeeded", "event": settled}
                    if action["kind"] == "economic_http":
                        db.execute("UPDATE economic_reservations SET state='released' WHERE proposal_id=?", (proposal_id,))
                    event = self._append(db, case_id, "effect_rejected", {
                        "proposal_id": proposal_id, "action": proposal["action"],
                        "status": exc.code, "claim_hash": claim["hash"],
                    })
                return {"status": "rejected", "event": event}
            with self.tx() as db:
                settled = self._verified(db, case_id, proposal_id)
                if settled:
                    return {"status": "succeeded", "event": settled}
                self._append(db, case_id, "effect_unknown", {"proposal_id": proposal_id, "action": proposal["action"], "error": _destination_code(exc)})
            return {"status": "unknown", "proposal_id": proposal_id, "detail": "Check destination before reconciliation; execution might have succeeded"}
        except Exception as exc:
            with self.tx() as db:
                settled = self._verified(db, case_id, proposal_id)
                if settled:
                    return {"status": "succeeded", "event": settled}
                self._append(db, case_id, "effect_unknown", {"proposal_id": proposal_id, "action": proposal["action"], "error": _destination_code(exc)})
            return {"status": "unknown", "proposal_id": proposal_id, "detail": "Check destination before reconciliation; execution might have succeeded"}
        with self.tx() as db:
            settled = self._verified(db, case_id, proposal_id)
            if settled:
                return {"status": "succeeded", "event": settled}
            if action["kind"] == "economic_http":
                db.execute("UPDATE economic_reservations SET state='settled' WHERE proposal_id=?", (proposal_id,))
            result = self._append(db, case_id, "effect_succeeded", {"proposal_id": proposal_id, "action": proposal["action"], "output": output, "claim_hash": claim["hash"]})
        return {"status": "succeeded", "event": result}

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
            config = json.loads(db.execute("SELECT body FROM actions WHERE name=?", (proposal["action"],)).fetchone()["body"])
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
                    db.execute("UPDATE economic_reservations SET state='settled' WHERE proposal_id=? AND state='reserved'", (proposal_id,))
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
            return [dict(r) for r in db.execute("SELECT * FROM cases ORDER BY created DESC")]

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

    def verify_chain(self, case_id: str) -> bool:
        with self.tx() as db:
            events = self._events(db, case_id)
            if not verify_events(events):
                return False
            stored = {
                row["seq"]: row["jcs"]
                for row in db.execute("SELECT seq, jcs FROM event_side WHERE case_id=?", (case_id,))
            }
            for event in events:
                digest = stored.get(event["seq"])
                if isinstance(digest, str) and digest != envelope_jcs(event):
                    return False
            return True

    def side_channel(self, case_id: str) -> list[dict]:
        """JCS digest and deciding rule id for each new event. Neither is in the event hash.

        Events written before this side table existed have no row.
        """
        with self.tx() as db:
            self._case(db, case_id)
            rows = db.execute(
                "SELECT seq, jcs, rule FROM event_side WHERE case_id=? ORDER BY seq",
                (case_id,),
            ).fetchall()
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
        row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise Rejected("unknown_case", case_id)
        return row

    @staticmethod
    def _contract(db, contract_id, version):
        row = db.execute("SELECT body FROM contracts WHERE id=? AND version=?", (contract_id, version)).fetchone()
        if not row:
            raise Rejected("unknown_contract", f"{contract_id}@{version}")
        return json.loads(row["body"])

    @staticmethod
    def _policy(db, version=None):
        row = db.execute("SELECT body FROM policies WHERE version=?" if version is not None else "SELECT body FROM policies ORDER BY version DESC LIMIT 1", (version,) if version is not None else ()).fetchone()
        if not row:
            raise Rejected("unknown_policy", "install policy first")
        return json.loads(row["body"])

    @staticmethod
    def _events(db, case_id):
        events = []
        rows = db.execute(
            "SELECT case_id, seq, kind, body, at, previous, hash, at_json FROM events WHERE case_id=? ORDER BY seq",
            (case_id,),
        )
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
        previous = db.execute("SELECT seq,hash FROM events WHERE case_id=? ORDER BY seq DESC LIMIT 1", (case_id,)).fetchone()
        event = {"case_id": case_id, "seq": previous["seq"] + 1 if previous else 1, "kind": kind,
                 "body": body, "at": stamp(time.time()), "previous": previous["hash"] if previous else "0" * 64}
        event["hash"] = event_digest(event)
        db.execute(
            "INSERT INTO events (case_id, seq, kind, body, at, previous, hash, at_json) VALUES (?,?,?,?,?,?,?,?)",
            (case_id, event["seq"], kind, canonical(body), event["at"], event["previous"], event["hash"], json.dumps(event["at"])),
        )
        db.execute(
            "INSERT INTO event_side VALUES (?,?,?,?)",
            (case_id, event["seq"], envelope_jcs(event), rule if kind == "decision" else None),
        )
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

    def _context(self, contract, policy, history, proposal, now, db):
        installed = {row["name"] for row in db.execute("SELECT name FROM actions")}
        configs = {row["name"]: json.loads(row["body"]) for row in db.execute("SELECT name, body FROM actions")}

        def reserved(budget_id, asset):
            row = db.execute(
                "SELECT COALESCE(SUM(amount_units),0) AS total FROM economic_reservations "
                "WHERE budget_id=? AND asset=? AND state!='released'",
                (budget_id, asset),
            ).fetchone()
            return int(row["total"])

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
