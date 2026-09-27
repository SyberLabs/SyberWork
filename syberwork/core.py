"""Immutable contracts, case histories, deterministic admission and effects."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from syberlabs.admission import AdmissionContext, admit, approval_roles
from syberlabs.canonical import canonical, digest
from syberlabs.errors import Rejected
from syberlabs.events import event_digest, verify_events
from syberlabs.evidence import verified_reconciliation
from syberlabs.values import at_path

__all__ = ["Rejected", "Work", "canonical", "digest", "trusted_origin"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward action credentials or source tokens to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(NoRedirect)


def trusted_origin(url: str) -> tuple[str, str, int]:
    """Reject userinfo and non-loopback HTTP before a credential-bearing request."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (not isinstance(url, str) or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or not parsed.path.startswith("/")):
            raise ValueError("invalid URL")
        if parsed.scheme == "https":
            return parsed.scheme, parsed.hostname, port or 443
        if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and port:
            return parsed.scheme, parsed.hostname, port
    except (ValueError, AttributeError, TypeError):
        pass
    raise Rejected("invalid_target", "target must be HTTPS or loopback HTTP without URL credentials")


class Work:
    def __init__(self, database: str | Path):
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript("""
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
                    hash TEXT NOT NULL, PRIMARY KEY(case_id,seq));
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def install_contract(self, doc: dict) -> None:
        required = ("id", "version", "inputs", "actions", "acceptance")
        if any(k not in doc for k in required) or not isinstance(doc["version"], int):
            raise Rejected("invalid_contract", "id, integer version, inputs, actions, acceptance are required")
        if not isinstance(doc["actions"], dict) or not isinstance(doc["acceptance"], list):
            raise Rejected("invalid_contract", "actions must be an object and acceptance a list")
        if any(not isinstance(a, dict) or "id" not in a or a.get("kind") not in ("effect", "signoff", "fact") for a in doc["acceptance"]):
            raise Rejected("invalid_contract", "acceptance clauses need IDs and known kinds")
        if len({a["id"] for a in doc["acceptance"]}) != len(doc["acceptance"]):
            raise Rejected("invalid_contract", "acceptance clause IDs must be unique")
        bindings = doc.get("input_bindings", {})
        if not isinstance(bindings, dict) or any(
            key not in doc["inputs"] or not isinstance(binding, str) or not binding.startswith("fact:")
            for key, binding in bindings.items()
        ):
            raise Rejected("invalid_contract", "input bindings must map declared input keys to fact paths")
        resolutions = doc.get("resolutions", {})
        if not isinstance(resolutions, dict):
            raise Rejected("invalid_contract", "resolutions must be an object")
        for name, spec in resolutions.items():
            if (not isinstance(name, str) or not isinstance(spec, dict) or
                    spec.get("record_key_input") not in doc["inputs"] or
                    not isinstance(spec.get("due_seconds"), int) or not 0 < spec["due_seconds"] <= 2592000 or
                    not isinstance(spec.get("blocks_actions"), list) or
                    any(action not in doc["actions"] for action in spec["blocks_actions"]) or
                    any(not isinstance(spec.get(field), str) or not spec[field] for field in ("owner_role", "escalate_role"))):
                raise Rejected("invalid_contract", "resolution needs owner, escalation, due time, and input key")
            for field, required_fields in (("trigger", ("key", "source", "missing_path", "identity_path")),
                                           ("choices", ("key", "source", "list_path", "identity_path")),
                                           ("result", ("key", "source", "value_path", "identity_path"))):
                item = spec.get(field)
                if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k] for k in required_fields):
                    raise Rejected("invalid_contract", "resolution requires a source-backed " + field)
            if "confirmation" in spec and (not isinstance(spec["confirmation"], dict) or
                    any(not isinstance(spec["confirmation"].get(k), str) or not spec["confirmation"][k]
                        for k in ("key", "source", "value_path"))):
                raise Rejected("invalid_contract", "invalid resolution confirmation")
        path = self._compile_path(doc)
        doc = {**doc, "compiled_path": path}
        with self.tx() as db:
            row = db.execute("SELECT body FROM contracts WHERE id=? AND version=?", (doc["id"], doc["version"])).fetchone()
            if row and row["body"] != canonical(doc):
                raise Rejected("immutable_contract", "publish a new version")
            db.execute("INSERT OR IGNORE INTO contracts VALUES (?,?,?)", (doc["id"], doc["version"], canonical(doc)))

    @staticmethod
    def _compile_path(doc: dict) -> list[str]:
        actions = doc["actions"]
        if any(not isinstance(spec, dict) for spec in actions.values()):
            raise Rejected("invalid_contract", "action rules must be objects")
        for name, spec in actions.items():
            dep = spec.get("requires_effect")
            if dep and (dep not in actions or dep == name):
                raise Rejected("invalid_contract", "unknown or self-referencing dependency: " + name)
        path = []
        pending = set(actions)
        while pending:
            ready = sorted(name for name in pending if not actions[name].get("requires_effect") or actions[name]["requires_effect"] in path)
            if not ready:
                raise Rejected("invalid_contract", "action dependency cycle")
            path.extend(ready)
            pending.difference_update(ready)
        authored = doc.get("compiled_path")
        if authored is not None:
            if not isinstance(authored, list) or len(authored) != len(set(authored)) or any(a not in actions for a in authored):
                raise Rejected("invalid_contract", "invalid compiled path")
            visited = set()
            for action in authored:
                dep = actions[action].get("requires_effect")
                if dep and dep not in visited:
                    raise Rejected("invalid_contract", "compiled path violates dependency: " + action)
                visited.add(action)
            return authored
        return path

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
            db.execute("INSERT OR IGNORE INTO policies VALUES (?,?)", (doc["version"], canonical(doc)))

    def install_action(self, name: str, doc: dict) -> None:
        if doc.get("kind") not in ("local", "http"):
            raise Rejected("invalid_action", "action kind must be local or http")
        if doc["kind"] == "http":
            origin = trusted_origin(doc.get("url", ""))
            if doc.get("method", "POST") not in ("POST", "PUT", "PATCH"):
                raise Rejected("invalid_action", "HTTP method must write")
        status_url = doc.get("status_url")
        if status_url is not None:
            if (doc["kind"] != "http" or trusted_origin(status_url) != origin or
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
        if doc.get("kind") != "http" or not doc.get("url", "").startswith(("https://", "http://127.0.0.1:")) or "{key}" not in doc["url"]:
            raise Rejected("invalid_source", "source needs a fixed HTTPS or local URL with {key}")
        with self.tx() as db:
            prior = db.execute("SELECT body FROM sources WHERE name=?", (name,)).fetchone()
            if prior and prior["body"] != canonical(doc):
                raise Rejected("immutable_source", "publish a new source name")
            db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (name, canonical(doc)))

    def create_case(self, contract_id: str, version: int, inputs: dict, actor: str) -> str:
        with self.tx() as db:
            contract = self._contract(db, contract_id, version)
            if set(inputs) != set(contract["inputs"]):
                raise Rejected("input_schema", "input keys must exactly match contract")
            for key, kind in contract["inputs"].items():
                value = inputs[key]
                if kind == "string" and not isinstance(value, str) or kind == "integer" and (type(value) is not int):
                    raise Rejected("input_schema", f"invalid {key}: expected {kind}")
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
                if action and json.loads(action["body"])["kind"] == "http":
                    raise Rejected("cancellation_denied", "an external effect was claimed; verify its outcome first")
            return self._append(db, case_id, "case_cancelled", {"actor": actor, "role": contract.get("cancel_role", "manager"), "reason": reason.strip()})

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
        request = urllib.request.Request(config["url"].replace("{key}", quote(record_key, safe="")), headers=headers)
        try:
            with HTTP.open(request, timeout=min(30, config.get("timeout_seconds", 10))) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise Rejected("source_size", "source response exceeds 1 MB")
                document = json.loads(raw)
                version = response.headers.get("ETag") or response.headers.get("X-Record-Version")
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise Rejected("source_unavailable", str(exc)[:200]) from exc
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
            result = self._admit(contract, policy, history, proposal, time.time(), db)
            self._append(db, case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], **result})
            return {"proposal": proposal, "decision": result}

    def compiled_propose(self, case_id: str, actor: str, roles: list[str]) -> dict:
        if "compiled" not in roles:
            raise Rejected("origin_denied", "compiled proposer credential required")
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            history = self._events(db, case_id)
            action = self._next_path(contract, history)
            if not action:
                raise Rejected("path_complete", "no remaining compiled step")
            args = {}
            for param, binding in contract["actions"][action].get("arguments", {}).items():
                name = binding.removeprefix("version:").removeprefix("fact:").split(".")[0]
                fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == name), None)
                if not fact:
                    raise Rejected("missing_fact", name)
                value = fact["body"]["version"] if binding.startswith("version:") else fact["body"]["value"]
                if binding.startswith("fact:"):
                    for field in binding[5:].split(".")[1:]:
                        if not isinstance(value, dict) or field not in value:
                            raise Rejected("missing_fact_field", binding)
                        value = value[field]
                args[param] = value
        return self.propose(case_id, action, args, actor, roles, origin="compiled")

    def model_propose(self, case_id: str, actor: str, roles: list[str]) -> dict:
        """Ask a configured planner for JSON; all output still passes through admission."""
        import os
        if "model" not in roles:
            raise Rejected("origin_denied", "model proposer credential required")
        target = os.getenv("SYBERWORK_PLANNER_URL", "")
        if not target.startswith(("https://", "http://127.0.0.1:")):
            raise Rejected("planner_unconfigured", "configure an HTTPS or local planner endpoint")
        case = self.inspect(case_id)
        request_body = {"objective": case["contract"].get("title", case["contract"]["id"]),
                        "allowed_actions": list(case["contract"]["actions"]),
                        "contract": case["contract"], "events": case["events"],
                        "acceptance": case["acceptance"]}
        headers = {"Content-Type": "application/json"}
        if os.getenv("SYBERWORK_PLANNER_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["SYBERWORK_PLANNER_TOKEN"]
        request = urllib.request.Request(target, data=canonical(request_body).encode(), headers=headers, method="POST")
        try:
            with HTTP.open(request, timeout=30) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise Rejected("planner_size", "planner response exceeds 1 MB")
            suggestion = json.loads(raw)
            if not isinstance(suggestion, dict) or not isinstance(suggestion.get("action"), str) or not isinstance(suggestion.get("args"), dict):
                raise Rejected("planner_shape", "planner must return {action, args}")
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise Rejected("planner_unavailable", str(exc)[:200]) from exc
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
            decision = self._admit(contract, policy, history, proposal, time.time(), db)
            if decision["status"] != "allowed":
                self._append(db, case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], "phase": "commit", **decision})
                return {"decision": decision}
            action = json.loads(db.execute("SELECT body FROM actions WHERE name=?", (proposal["action"],)).fetchone()["body"])
            claim = self._append(db, case_id, "effect_started", {"proposal_id": proposal_id, "action": proposal["action"], "actor": actor, "policy_version": policy["version"], "idempotency_key": proposal_id})
        try:
            output = self._execute(action, proposal["args"], proposal_id)
        except urllib.error.HTTPError as exc:
            if exc.code in action.get("no_write_statuses", []):
                with self.tx() as db:
                    event = self._append(db, case_id, "effect_rejected", {
                        "proposal_id": proposal_id, "action": proposal["action"],
                        "status": exc.code, "claim_hash": claim["hash"],
                    })
                return {"status": "rejected", "event": event}
            with self.tx() as db:
                self._append(db, case_id, "effect_unknown", {"proposal_id": proposal_id, "action": proposal["action"], "error": str(exc)[:400]})
            return {"status": "unknown", "proposal_id": proposal_id, "detail": "Check destination before reconciliation; execution might have succeeded"}
        except Exception as exc:
            with self.tx() as db:
                self._append(db, case_id, "effect_unknown", {"proposal_id": proposal_id, "action": proposal["action"], "error": str(exc)[:400]})
            return {"status": "unknown", "proposal_id": proposal_id, "detail": "Check destination before reconciliation; execution might have succeeded"}
        with self.tx() as db:
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
            if not any(e["kind"] == "effect_unknown" and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "no unknown effect")
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
        request = urllib.request.Request(status_url.replace("{key}", quote(proposal_id, safe="")), headers=headers)
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
            if (isinstance(record, dict) and record.get("state") == "committed"
                    and record.get("idempotency_key") == proposal_id
                    and record.get("request_digest") == digest(proposal["args"])
                    and isinstance(record.get("external_id"), str) and record["external_id"]):
                status, reason = "verified", "destination_record_matched"
            else:
                status, reason = "unverified", "destination_record_mismatch"
        with self.tx() as db:
            history = self._events(db, case_id)
            if any(verified_reconciliation(e) and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "already reconciled")
            if status == "verified":
                proof = {"verified": True, "external_id": record["external_id"],
                         "request_digest": record["request_digest"], "response_digest": digest(record),
                         "idempotency_key": proposal_id}
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
            self._case(db, case_id)
            if any(e["kind"] == "case_cancelled" for e in self._events(db, case_id)):
                raise Rejected("case_cancelled", "case is closed")
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
            return {"case": dict(row), "contract": contract, "events": history,
                    "acceptance": clauses, "complete": not cancelled and bool(clauses) and all(v["passed"] for v in clauses),
                    "status": "cancelled" if cancelled else "complete" if bool(clauses) and all(v["passed"] for v in clauses) else "in_progress",
                    "resolutions": resolutions,
                    "next_compiled": None if cancelled else self._next_path(contract, history)}

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

    def verify_chain(self, case_id: str) -> bool:
        with self.tx() as db:
            return verify_events(self._events(db, case_id))

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
        req = urllib.request.Request(action["url"], data=canonical(args).encode(), headers=headers, method=action.get("method", "POST"))
        with HTTP.open(req, timeout=min(30, action.get("timeout_seconds", 10))) as response:
            raw = response.read(32768)
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
        return [{**dict(r), "body": json.loads(r["body"])} for r in db.execute("SELECT * FROM events WHERE case_id=? ORDER BY seq", (case_id,))]

    @staticmethod
    def _append(db, case_id, kind, body):
        previous = db.execute("SELECT seq,hash FROM events WHERE case_id=? ORDER BY seq DESC LIMIT 1", (case_id,)).fetchone()
        event = {"case_id": case_id, "seq": previous["seq"] + 1 if previous else 1, "kind": kind,
                 "body": body, "at": time.time(), "previous": previous["hash"] if previous else "0" * 64}
        event["hash"] = event_digest(event)
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (case_id, event["seq"], kind, canonical(body), event["at"], event["previous"], event["hash"]))
        return event

    @staticmethod
    def _proposal(history, proposal_id):
        for e in history:
            if e["kind"] == "proposed" and e["body"]["id"] == proposal_id:
                return e["body"]
        raise Rejected("unknown_proposal", proposal_id)

    @staticmethod
    def _approval_roles(contract, policy, action):
        return approval_roles(contract, policy, action)

    def _admit(self, contract, policy, history, proposal, now, db):
        installed = {row["name"] for row in db.execute("SELECT name FROM actions")}
        decision = admit(AdmissionContext(contract, policy, history, proposal, now, installed))
        return {"status": decision["status"], "reason": decision["reason"]}

    @staticmethod
    def _acceptance(contract, history):
        results = []
        for clause in contract["acceptance"]:
            if clause["kind"] == "effect":
                passed = any(e["kind"] == "effect_succeeded" and e["body"]["action"] == clause["action"] or verified_reconciliation(e) and e["body"]["action"] == clause["action"] for e in history)
            elif clause["kind"] == "signoff":
                completed_seq = next((e["seq"] for e in history if
                                      (e["kind"] == "effect_succeeded" or verified_reconciliation(e))
                                      and e["body"]["action"] == clause.get("after_action")), 0)
                passed = any(e["kind"] == "signed" and e["body"]["role"] == clause["role"] and (not clause.get("after_action") or e["seq"] > completed_seq > 0) for e in history)
            elif clause["kind"] == "fact":
                passed = any(e["kind"] == "observed" and e["body"]["key"] == clause["key"] and e["body"]["value"] == clause.get("equals") for e in history)
            else:
                passed = False
            results.append({"id": clause["id"], "passed": passed})
        return results

    @staticmethod
    def _next_path(contract, history):
        completed = {e["body"]["action"] for e in history if e["kind"] == "effect_succeeded" or verified_reconciliation(e)}
        return next((a for a in contract.get("compiled_path", []) if a not in completed), None)
