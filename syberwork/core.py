"""Immutable contracts, case histories, deterministic admission and effects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from urllib.parse import quote


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward action credentials or source tokens to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(NoRedirect)
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Rejected(ValueError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


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
        if doc["kind"] == "http" and (not doc.get("url", "").startswith(("https://", "http://127.0.0.1:")) or doc.get("method", "POST") not in ("POST", "PUT", "PATCH")):
            raise Rejected("invalid_action", "HTTP target must be HTTPS or local and method must write")
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
            if any(e["kind"] in ("effect_started", "effect_succeeded", "effect_unknown") and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("effect_claimed", "already started; inspect or reconcile")
            decision = self._admit(contract, policy, history, proposal, time.time(), db)
            if decision["status"] != "allowed":
                self._append(db, case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], "phase": "commit", **decision})
                return {"decision": decision}
            action = json.loads(db.execute("SELECT body FROM actions WHERE name=?", (proposal["action"],)).fetchone()["body"])
            claim = self._append(db, case_id, "effect_started", {"proposal_id": proposal_id, "action": proposal["action"], "actor": actor, "policy_version": policy["version"], "idempotency_key": proposal_id})
        try:
            output = self._execute(action, proposal["args"], proposal_id)
        except Exception as exc:
            with self.tx() as db:
                self._append(db, case_id, "effect_unknown", {"proposal_id": proposal_id, "error": str(exc)[:400]})
            return {"status": "unknown", "proposal_id": proposal_id, "detail": "Check destination before reconciliation; execution might have succeeded"}
        with self.tx() as db:
            result = self._append(db, case_id, "effect_succeeded", {"proposal_id": proposal_id, "action": proposal["action"], "output": output, "claim_hash": claim["hash"]})
        return {"status": "succeeded", "event": result}

    def reconcile(self, case_id: str, proposal_id: str, success: bool, evidence: str, actor: str, roles: list[str]) -> dict:
        if "manager" not in roles or not evidence:
            raise Rejected("reconciliation_denied", "manager and external evidence reference required")
        with self.tx() as db:
            history = self._events(db, case_id)
            proposal = self._proposal(history, proposal_id)
            if not any(e["kind"] == "effect_unknown" and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "no unknown effect")
            if any(e["kind"] == "reconciled" and e["body"]["proposal_id"] == proposal_id for e in history):
                raise Rejected("reconciliation_denied", "already reconciled")
            return self._append(db, case_id, "reconciled", {"proposal_id": proposal_id, "action": proposal["action"], "success": success, "evidence": evidence, "actor": actor})

    def signoff(self, case_id: str, actor: str, roles: list[str], role: str) -> dict:
        if role not in roles:
            raise Rejected("signoff_denied", "actor lacks role")
        with self.tx() as db:
            self._case(db, case_id)
            return self._append(db, case_id, "signed", {"actor": actor, "role": role})

    def inspect(self, case_id: str) -> dict:
        with self.tx() as db:
            row = self._case(db, case_id)
            contract = self._contract(db, row["contract_id"], row["contract_version"])
            history = self._events(db, case_id)
            clauses = self._acceptance(contract, history)
            return {"case": dict(row), "contract": contract, "events": history,
                    "acceptance": clauses, "complete": bool(clauses) and all(v["passed"] for v in clauses),
                    "next_compiled": self._next_path(contract, history)}

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
            previous = "0" * 64
            for event in self._events(db, case_id):
                if event["previous"] != previous or event["hash"] != digest({k: event[k] for k in ("case_id", "seq", "kind", "body", "at", "previous")}):
                    return False
                previous = event["hash"]
            return True

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
        event["hash"] = digest(event)
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
        roles = [policy["actions"].get(action, {}).get("approval_role"), contract["actions"].get(action, {}).get("approval_role")]
        return list(dict.fromkeys(r for r in roles if r))

    def _admit(self, contract, policy, history, proposal, now, db):
        action, args = proposal["action"], proposal["args"]
        def deny(reason):
            return {"status": "denied", "reason": reason}
        if not isinstance(args, dict) or not isinstance(proposal.get("roles"), list):
            return deny("invalid_proposal_shape")
        if action not in contract["actions"]:
            return deny("action_not_in_contract")
        if action not in policy["actions"]:
            return deny("action_not_in_global_policy")
        if not db.execute("SELECT 1 FROM actions WHERE name=?", (action,)).fetchone():
            return deny("action_not_installed")
        local, global_rule = contract["actions"][action], policy["actions"][action]
        if not set(global_rule.get("roles", [])).intersection(proposal["roles"]):
            return deny("actor_role_missing")
        for rule in (global_rule, local):
            if "max_amount" in rule:
                if type(args.get("amount")) not in (int, float):
                    return deny("amount_required")
                if args["amount"] > rule["max_amount"] or args["amount"] < 0:
                    return deny("amount_exceeds_limit")
        if local.get("requires_effect") and not any(e["kind"] == "effect_succeeded" and e["body"]["action"] == local["requires_effect"] or e["kind"] == "reconciled" and e["body"].get("success") and e["body"]["action"] == local["requires_effect"] for e in history):
            return deny("required_prior_effect_missing")
        for requirement in local.get("required_facts", []):
            fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == requirement["key"]), None)
            if not fact:
                return deny("missing_fact:" + requirement["key"])
            if fact["body"]["source"] != requirement["source"]:
                return deny("untrusted_fact_source:" + requirement["key"])
            if requirement.get("verified") and not fact["body"].get("verified"):
                return deny("source_verification_required:" + requirement["key"])
            if now - fact["at"] > requirement.get("max_age_seconds", 86400):
                return deny("stale_fact:" + requirement["key"])
        for param, binding in local.get("arguments", {}).items():
            if binding.startswith("version:"):
                name = binding[8:]
                fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == name), None)
                if not fact or args.get(param) != fact["body"]["version"]:
                    return deny("argument_provenance:" + param)
            elif binding.startswith("fact:"):
                name, *path = binding[5:].split(".")
                fact = next((e for e in reversed(history) if e["kind"] == "observed" and e["body"]["key"] == name), None)
                expected = fact["body"]["value"] if fact else None
                for component in path:
                    expected = expected.get(component) if isinstance(expected, dict) else None
                if not fact or expected is None or param not in args or args[param] != expected:
                    return deny("argument_provenance:" + param)
        for role in self._approval_roles(contract, policy, action):
            if not any(e["kind"] == "approved" and e["body"].get("proposal_id") == proposal["id"] and e["body"]["role"] == role and e["body"].get("args_hash") == digest(args) for e in history):
                return {"status": "needs_approval", "reason": "approval_required:" + role}
        return {"status": "allowed", "reason": "all_checks_passed"}

    @staticmethod
    def _acceptance(contract, history):
        results = []
        for clause in contract["acceptance"]:
            if clause["kind"] == "effect":
                passed = any(e["kind"] == "effect_succeeded" and e["body"]["action"] == clause["action"] or e["kind"] == "reconciled" and e["body"].get("success") and e["body"]["action"] == clause["action"] for e in history)
            elif clause["kind"] == "signoff":
                completed_seq = next((e["seq"] for e in history if e["kind"] == "effect_succeeded" and e["body"]["action"] == clause.get("after_action")), 0)
                passed = any(e["kind"] == "signed" and e["body"]["role"] == clause["role"] and (not clause.get("after_action") or e["seq"] > completed_seq > 0) for e in history)
            elif clause["kind"] == "fact":
                passed = any(e["kind"] == "observed" and e["body"]["key"] == clause["key"] and e["body"]["value"] == clause.get("equals") for e in history)
            else:
                passed = False
            results.append({"id": clause["id"], "passed": passed})
        return results

    @staticmethod
    def _next_path(contract, history):
        completed = {e["body"]["action"] for e in history if e["kind"] == "effect_succeeded"}
        return next((a for a in contract.get("compiled_path", []) if a not in completed), None)
