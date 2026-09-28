"""Case session for a project that does not use SyberWork's database.

In memory by default. With ``journal=Journal(path)`` every installed document
and event is fsynced to an append-only file before memory changes, threads load
lazily, and a restarted process resumes from the file after verifying each
chain. Local actions only. A local action may be bound to an executor (for
example a compare-and-swap Git ref update); its claim is durable before the
executor runs, and ``reconcile`` reads the destination to settle an interrupted
claim. Admission is the only path to an effect. The hash chain uses the same
digest as Work. This module does not import syberwork.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable

from syberlabs.admission import AdmissionContext, admit, approval_roles, explain, proposal_prefix
from syberlabs.bindings import bind_arguments, next_compiled
from syberlabs.canonical import canonical, digest
from syberlabs.clock import stamp
from syberlabs.contracts import check_case_inputs, prepare_contract
from syberlabs.economic import policy_has_economic, validate_policy_budgets
from syberlabs.errors import Rejected
from syberlabs.events import event_digest, verify_events
from syberlabs.evidence import acceptance_results, signer_is_effect_actor
from syberlabs.evolution import candidate_record, candidate_views, evaluation_record, search_record
from syberlabs.evidence import verified_reconciliation
from syberlabs.jcs import envelope_jcs
from syberlabs.planner import HttpPlanner, planning_context


class NoWrite(Exception):
    """Raised by a local executor that guarantees the destination was not written.

    ``status`` follows the HTTP no-write codes the protocol already uses:
    409 (conflict, e.g. the target is checked out) or 412 (the expected prior
    state no longer holds).
    """

    def __init__(self, status: int, detail: str):
        if status not in (409, 412, 428):
            raise ValueError("no-write status must be 409, 412 or 428")
        super().__init__(detail)
        self.status = status


def _side_matches(events: list[dict], sides: dict) -> bool:
    """A stored JCS digest must still describe the event. Missing digests are old history."""
    for event in events:
        row = sides.get(event["seq"])
        stored = row.get("jcs") if isinstance(row, dict) else None
        if isinstance(stored, str) and stored != envelope_jcs(event):
            return False
    return True


def _guard(method):
    """Serialize session operations. Re-entrant so compiled_propose can call propose.

    With a journal, also hold its cross-process lock and read what other
    processes appended before running the operation.
    """

    def wrapped(self, *args, **kwargs):
        with self._lock:
            if self._journal is None:
                return method(self, *args, **kwargs)
            with self._journal.locked():
                self._sync()
                return method(self, *args, **kwargs)

    wrapped.__name__ = method.__name__
    wrapped.__doc__ = method.__doc__
    return wrapped


class Session:
    def __init__(self, *, clock: Callable[[], float] | None = None, journal=None):
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self.contracts: dict[tuple[str, int], dict] = {}
        self.policies: dict[int, dict] = {}
        self.actions: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self._side: dict[str, dict[int, dict]] = {}
        self._executors: dict[str, Any] = {}
        self._journal = journal
        if journal is not None:
            with self._lock, journal.locked():
                self._sync()

    def _sync(self) -> None:
        """Apply registry records and events other processes appended."""
        for record in self._journal.read_new(self._journal.registry):
            if record["t"] == "contract":
                doc = record["doc"]
                try:
                    valid = json.loads(canonical(prepare_contract(doc))) == doc
                except Rejected:
                    valid = False
                if not valid:
                    raise Rejected("journal_corrupt", f"contract {doc.get('id')}@{doc.get('version')} does not validate")
                self.contracts[(doc["id"], doc["version"])] = doc
            elif record["t"] == "policy":
                self.policies[record["doc"]["version"]] = record["doc"]
            elif record["t"] == "action":
                self.actions[record["name"]] = record["doc"]
        for case_id in list(self.cases):
            self._load(case_id)

    def _load(self, case_id: str) -> None:
        """Read a thread's new records and check each event links to the chain."""
        records = self._journal.read_new(self._journal.thread_path(case_id))
        events = self.events.setdefault(case_id, [])
        for record in records:
            if record["t"] == "case":
                self.cases[case_id] = record["row"]
                continue
            event = record["event"]
            previous = events[-1]["hash"] if events else "0" * 64
            if (event["previous"] != previous or event["hash"] != event_digest(event)
                    or event["case_id"] != case_id or event["seq"] != len(events) + 1):
                raise Rejected("journal_corrupt", f"thread {case_id} breaks its hash chain at seq {event.get('seq')}")
            side = record.get("side") or {}
            if isinstance(side.get("jcs"), str) and side["jcs"] != envelope_jcs(event):
                raise Rejected("journal_corrupt", f"thread {case_id} side record differs at seq {event['seq']}")
            events.append(event)
            self._side.setdefault(case_id, {})[event["seq"]] = side
        if case_id not in self.cases:
            raise Rejected("unknown_case", case_id)
        self._journal.note_loaded(case_id, len(events))

    @_guard
    def bind_effect(self, action: str, executor) -> None:
        """Run ``executor.apply(case_id, args, key)`` for this action's effect; ``status`` settles a claim.

        Only an action whose definition names an ``effect`` can be bound, and
        such an action cannot commit without its executor.
        """
        found = self.actions.get(action)
        if not found or not isinstance(found.get("effect"), str):
            raise Rejected("invalid_action", "only an installed local action that names an effect can be bound")
        self._executors[action] = executor

    @_guard
    def install_contract(self, doc: dict) -> None:
        prepared = prepare_contract(doc)
        key = (prepared["id"], prepared["version"])
        stored = json.loads(canonical(prepared))
        prior = self.contracts.get(key)
        if prior is not None and prior != stored:
            raise Rejected("immutable_contract", "publish a new version")
        if prior is None and self._journal is not None:
            self._journal.add_registry({"t": "contract", "doc": stored})
        self.contracts[key] = stored

    @_guard
    def install_policy(self, doc: dict) -> None:
        if not isinstance(doc.get("version"), int) or not isinstance(doc.get("actions"), dict):
            raise Rejected("invalid_policy", "version and actions required")
        version = doc["version"]
        stored = json.loads(canonical(doc))
        prior = self.policies.get(version)
        if prior is not None and prior != stored:
            raise Rejected("immutable_policy", "publish a new version")
        if self.policies and version < max(self.policies) and prior is None:
            raise Rejected("policy_version", "new policy version must advance")
        if prior is None and policy_has_economic(doc):
            validate_policy_budgets(doc, list(self.policies.values()))
        if prior is None:
            if self._journal is not None:
                self._journal.add_registry({"t": "policy", "doc": stored})
            self.policies[version] = stored

    @_guard
    def install_action(self, name: str, doc: dict) -> None:
        if doc.get("kind") != "local":
            raise Rejected("invalid_action", "the in-memory session executes local actions")
        if "effect" in doc and (not isinstance(doc["effect"], str) or not doc["effect"]):
            raise Rejected("invalid_action", "effect must name the executor kind")
        stored = json.loads(canonical(doc))
        prior = self.actions.get(name)
        if prior is not None and prior != stored:
            raise Rejected("immutable_action", "action definitions cannot change while cases exist; use a new name")
        if prior is None:
            if self._journal is not None:
                self._journal.add_registry({"t": "action", "name": name, "doc": stored})
            self.actions[name] = stored

    @_guard
    def create_case(self, contract_id: str, version: int, inputs: dict, actor: str) -> str:
        contract = self._contract(contract_id, version)
        check_case_inputs(contract["inputs"], inputs)
        case_id = str(uuid.uuid4())
        row = {
            "id": case_id,
            "contract_id": contract_id,
            "contract_version": version,
            "inputs": inputs,
            "created": self.clock(),
        }
        if self._journal is not None:
            self._journal.create_thread(row)
        self.cases[case_id] = row
        self.events[case_id] = []
        self._append(case_id, "case_created", {"actor": actor, "inputs": inputs, "contract": [contract_id, version]})
        return case_id

    @_guard
    def observe(self, case_id: str, key: str, value: Any, source: str, version: str, actor: str, verified: bool = False) -> dict:
        if not all((key, source, version)):
            raise Rejected("invalid_observation", "key, source and source version are required")
        self._case(case_id)
        return self._append(case_id, "observed", {
            "key": key, "value": value, "source": source, "version": version, "actor": actor, "verified": verified,
        })

    @_guard
    def record_candidate(self, case_id: str, candidate: dict, actor: str) -> dict:
        """Register a provisional candidate. The host computes its commit, tree, and changed paths.

        Scope and size violations are recomputed from the contract. A candidate
        outside scope is still recorded, so the attempt is visible, and cannot be
        promoted. Registration is not evaluation and not promotion.
        """
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        self._open(case_id)
        body = candidate_record(contract, self.events[case_id], candidate, actor)
        return self._append(case_id, "candidate_registered", body)

    @_guard
    def record_evaluation(self, case_id: str, evaluation: dict, actor: str) -> dict:
        """Record the host's own check results for one candidate's exact commit and tree."""
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        self._open(case_id)
        body = evaluation_record(contract, self.events[case_id], evaluation, actor)
        return self._append(case_id, "candidate_evaluated", body)

    @_guard
    def record_search(self, case_id: str, phase: str, body: dict, actor: str) -> dict:
        """Record that a search provider started or finished, with its revision and budget use."""
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        self._open(case_id)
        record = search_record(contract, self.events[case_id], phase, body, actor)
        return self._append(case_id, "search_" + phase, record)

    @_guard
    def history(self, case_id: str) -> list[dict]:
        """The case's events, oldest first. Cheaper than inspect: no projections, no chain check."""
        self._case(case_id)
        return list(self.events[case_id])

    @_guard
    def candidates(self, case_id: str) -> list[dict]:
        """Candidates with evaluation and promotion state, derived from the case history."""
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        return candidate_views(contract, list(self.events[case_id]), self.clock())

    def _open(self, case_id: str) -> None:
        if any(event["kind"] == "case_cancelled" for event in self.events[case_id]):
            raise Rejected("case_cancelled", "case is closed")

    @_guard
    def propose(self, case_id: str, action: str, args: dict, actor: str, roles: list[str], origin: str = "human") -> dict:
        if origin not in ("human", "model", "compiled"):
            raise Rejected("invalid_origin", "origin must be human, model or compiled")
        if origin != "human" and origin not in roles:
            raise Rejected("origin_denied", "the actor cannot claim this proposer origin")
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        policy = self._policy()
        history = list(self.events[case_id])
        proposal_id = str(uuid.uuid4())
        proposal = {"id": proposal_id, "action": action, "args": args, "actor": actor, "roles": roles, "origin": origin}
        self._append(case_id, "proposed", proposal)
        full = admit(self._context(contract, policy, history, proposal, self.clock()))
        decision = {"status": full["status"], "reason": full["reason"]}
        self._append(case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], **decision}, rule=full["rule"])
        return {"proposal": proposal, "decision": decision}

    @_guard
    def suggest(self, case_id: str, actor: str, roles: list[str], planner) -> dict:
        if "model" not in roles:
            raise Rejected("origin_denied", "model proposer credential required")
        if isinstance(planner, HttpPlanner):
            planner.ensure_configured()
        case = self.inspect(case_id)
        suggestion = planner.propose(planning_context(case["contract"], case["acceptance"]))
        if not isinstance(suggestion, dict) or not isinstance(suggestion.get("action"), str) or not isinstance(suggestion.get("args"), dict):
            raise Rejected("planner_shape", "planner must return {action, args}")
        return self.propose(case_id, suggestion["action"], suggestion["args"], actor, roles, origin="model")

    @_guard
    def compiled_propose(self, case_id: str, actor: str, roles: list[str]) -> dict:
        if "compiled" not in roles:
            raise Rejected("origin_denied", "compiled proposer credential required")
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        history = self.events[case_id]
        action = next_compiled(contract, history)
        if not action:
            raise Rejected("path_complete", "no remaining compiled step")
        args = bind_arguments(contract, history, action)
        return self.propose(case_id, action, args, actor, roles, origin="compiled")

    @_guard
    def approve(self, case_id: str, proposal_id: str, actor: str, roles: list[str]) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        policy = self._policy()
        history = self.events[case_id]
        proposal = self._proposal(history, proposal_id)
        required_roles = approval_roles(contract, policy, proposal["action"])
        selected_role = next((role for role in required_roles if role in roles and not any(
            event["kind"] == "approved" and event["body"]["proposal_id"] == proposal_id and event["body"]["role"] == role
            for event in history
        )), None)
        if not selected_role or actor == proposal["actor"]:
            raise Rejected("approval_denied", "independent authorized approver required")
        return self._append(case_id, "approved", {
            "proposal_id": proposal_id, "actor": actor, "role": selected_role, "args_hash": digest(proposal["args"]),
        })

    @_guard
    def commit(self, case_id: str, proposal_id: str, actor: str) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        policy = self._policy()
        history = list(self.events[case_id])
        proposal = self._proposal(history, proposal_id)
        if actor != proposal["actor"]:
            raise Rejected("actor_mismatch", "only the original proposer may commit")
        if any(event["kind"] in ("effect_started", "effect_succeeded", "effect_unknown", "effect_rejected") and event["body"]["proposal_id"] == proposal_id for event in history):
            raise Rejected("effect_claimed", "already started; inspect or reconcile")
        full = admit(self._context(contract, policy, history, proposal, self.clock()))
        decision = {"status": full["status"], "reason": full["reason"]}
        if decision["status"] != "allowed":
            self._append(case_id, "decision", {"proposal_id": proposal_id, "policy_version": policy["version"], "phase": "commit", **decision}, rule=full["rule"])
            return {"decision": decision}
        action = self.actions.get(proposal["action"])
        if not action or action.get("kind") != "local":
            raise Rejected("unsupported_effect", "the in-memory session executes local actions")
        executor = self._executors.get(proposal["action"])
        if action.get("effect") and executor is None:
            raise Rejected("effect_unavailable", f"no executor is bound for {action['effect']}; nothing was claimed")
        claim = self._append(case_id, "effect_started", {
            "proposal_id": proposal_id, "action": proposal["action"], "actor": actor,
            "policy_version": policy["version"], "idempotency_key": proposal_id,
        })
        if executor is None:
            output = {"recorded": True, "args": proposal["args"]}
        else:
            try:
                output = executor.apply(case_id, proposal["args"], proposal_id)
            except NoWrite as exc:
                event = self._append(case_id, "effect_rejected", {
                    "proposal_id": proposal_id, "action": proposal["action"],
                    "status": exc.status, "claim_hash": claim["hash"],
                })
                return {"status": "rejected", "event": event, "detail": str(exc)}
            except Exception as exc:
                code = exc.code if isinstance(exc, Rejected) else "destination_error"
                self._append(case_id, "effect_unknown", {"proposal_id": proposal_id, "action": proposal["action"], "error": code})
                return {"status": "unknown", "proposal_id": proposal_id,
                        "detail": "Check the destination with reconcile; the write might have happened"}
        result = self._append(case_id, "effect_succeeded", {
            "proposal_id": proposal_id, "action": proposal["action"], "output": output, "claim_hash": claim["hash"],
        })
        return {"status": "succeeded", "event": result}

    @_guard
    def reconcile(self, case_id: str, proposal_id: str, actor: str, roles: list[str]) -> dict:
        """Settle an interrupted or unknown local effect by reading its destination.

        The executor's ``status`` reports ``applied`` (recorded as a verified
        reconciliation), ``not_applied`` (recorded as ``effect_rejected`` with
        status ``not_applied``, which frees the action for a fresh proposal), or
        anything else (recorded as pending). A caller cannot supply the outcome.
        No commit can be in flight here: commit holds the same lock for its
        whole claim, write, and outcome.
        """
        row = self._case(case_id)
        history = self.events[case_id]
        proposal = self._proposal(history, proposal_id)
        executor = self._executors.get(proposal["action"])
        if executor is None:
            raise Rejected("reconciliation_unavailable", "no executor is bound for this action")
        policy_roles = set(self._policy()["actions"].get(proposal["action"], {}).get("roles", []))
        if not (policy_roles | {"manager"}).intersection(roles):
            raise Rejected("reconciliation_denied", "a role that may propose this action, or manager, is required")
        claim = next((e for e in history if e["kind"] == "effect_started" and e["body"]["proposal_id"] == proposal_id), None)
        if claim is None or any(
                (e["kind"] in ("effect_succeeded", "effect_rejected") or verified_reconciliation(e))
                and e["body"]["proposal_id"] == proposal_id for e in history):
            raise Rejected("reconciliation_denied", "no unresolved effect")
        self._contract(row["contract_id"], row["contract_version"])
        try:
            state, proof = executor.status(case_id, proposal["args"], proposal_id)
        except Exception as exc:
            state, proof = "unknown", {"error": exc.code if isinstance(exc, Rejected) else "status_unavailable"}
        if state == "applied":
            event = self._append(case_id, "reconciled", {
                "proposal_id": proposal_id, "action": proposal["action"], "success": True, "actor": actor,
                "proof": {"verified": True, "external_id": proof["external_id"],
                          "request_digest": digest(proposal["args"]), "response_digest": digest(proof),
                          "idempotency_key": proposal_id},
            })
            return {"status": "verified", "reason": "destination_state_matched", "event": event}
        if state == "not_applied":
            event = self._append(case_id, "effect_rejected", {
                "proposal_id": proposal_id, "action": proposal["action"],
                "status": "not_applied", "claim_hash": claim["hash"],
            })
            return {"status": "not_applied", "reason": "destination_state_unchanged", "event": event}
        event = self._append(case_id, "reconciliation_checked", {
            "proposal_id": proposal_id, "action": proposal["action"],
            "status": "pending", "reason": "status_unavailable", "actor": actor,
        })
        return {"status": "pending", "reason": "status_unavailable", "event": event}

    @_guard
    def signoff(self, case_id: str, actor: str, roles: list[str], role: str) -> dict:
        if role not in roles:
            raise Rejected("signoff_denied", "actor lacks role")
        row = self._case(case_id)
        history = self.events[case_id]
        if any(event["kind"] == "case_cancelled" for event in history):
            raise Rejected("case_cancelled", "case is closed")
        contract = self._contract(row["contract_id"], row["contract_version"])
        if signer_is_effect_actor(contract, history, actor, role):
            raise Rejected("signoff_denied", "signer must differ from the effect actor")
        return self._append(case_id, "signed", {"actor": actor, "role": role})

    @_guard
    def preview(self, case_id: str, action: str, args: dict, actor: str, roles: list[str], origin: str = "human") -> dict:
        """What admission would decide for this proposal now, with the deciding rule. Nothing is recorded."""
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        proposal = {"id": "preview", "action": action, "args": args, "actor": actor, "roles": list(roles), "origin": origin}
        return explain(self._context(contract, self._policy(), list(self.events[case_id]), proposal, self.clock()))

    @_guard
    def explain_admission(self, case_id: str, proposal_id: str, *, when: str = "now") -> dict:
        """Admission for a stored proposal, plus the deciding rule.

        ``when="now"`` rechecks against the current history, which is what commit
        would do. ``when="recorded"`` uses the prefix and time of the original
        proposal, which is what the stored decision was based on.
        """
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        full = list(self.events[case_id])
        proposal = self._proposal(full, proposal_id)
        policy = self._policy()
        history = full
        now = self.clock()
        if when == "recorded":
            prefix = proposal_prefix(full, proposal_id)
            if prefix is None:
                raise Rejected("unknown_proposal", proposal_id)
            history, now = prefix
            recorded = next((event["body"] for event in full if event["kind"] == "decision" and event["body"].get("proposal_id") == proposal_id), None)
            if recorded and "policy_version" in recorded:
                policy = self._policy(recorded["policy_version"])
        elif when != "now":
            raise Rejected("invalid_explain", "when must be now or recorded")
        before = len(self.events[case_id])
        found = explain(self._context(contract, policy, history, proposal, now))
        if len(self.events[case_id]) != before:
            raise RuntimeError("explain_admission appended an event")
        return found

    @_guard
    def inspect(self, case_id: str) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], row["contract_version"])
        history = list(self.events[case_id])
        clauses = acceptance_results(contract, history)
        cancelled = any(event["kind"] == "case_cancelled" for event in history)
        complete = not cancelled and bool(clauses) and all(item["passed"] for item in clauses)
        found = {
            "case": dict(row),
            "contract": contract,
            "events": history,
            "acceptance": clauses,
            "complete": complete,
            "status": "cancelled" if cancelled else "complete" if complete else "in_progress",
            "chain_valid": verify_events(history),
        }
        if "evolution" in contract:
            found["candidates"] = candidate_views(contract, history, self.clock())
        return found

    @_guard
    def replay(self, case_id: str, contract_version: int, policy_version: int) -> dict:
        row = self._case(case_id)
        contract = self._contract(row["contract_id"], contract_version)
        policy = self._policy(policy_version)
        events = list(self.events[case_id])
        changed = []
        for index, event in enumerate(events):
            if event["kind"] != "proposed":
                continue
            proposal = event["body"]
            original = next((item["body"] for item in events[index + 1:] if item["kind"] == "decision" and item["body"]["proposal_id"] == proposal["id"]), None)
            updated = self._public_decision(contract, policy, events[:index], proposal, event["at"])
            if original and (original["status"], original.get("reason")) != (updated["status"], updated.get("reason")):
                changed.append({"proposal_id": proposal["id"], "action": proposal["action"], "before": original, "after": updated})
        original_contract = self._contract(row["contract_id"], row["contract_version"])
        return {
            "case_id": case_id,
            "contract_version": contract_version,
            "policy_version": policy_version,
            "changed_decisions": changed,
            "acceptance_before": acceptance_results(original_contract, events),
            "acceptance_after": acceptance_results(contract, events),
        }

    @_guard
    def verify_chain(self, case_id: str) -> bool:
        self._case(case_id)
        events = self.events[case_id]
        if not verify_events(events):
            return False
        return _side_matches(events, self._side.get(case_id, {}))

    def submit_witness(self, case_id: str, client) -> dict:
        """Ask an external witness to sign this case. This session has no signing seed."""
        with self._lock:
            if not self.verify_chain(case_id):
                raise Rejected("invalid_chain", "case history does not verify")
            events = json.loads(json.dumps(self.events[case_id]))
        return client.submit(events)

    @_guard
    def side_channel(self, case_id: str) -> list[dict]:
        """JCS digest and deciding rule id for each event. Neither is in the event hash."""
        self._case(case_id)
        rows = self._side.get(case_id, {})
        return [dict(rows[seq]) for seq in sorted(rows)]

    def _context(self, contract, policy, history, proposal, now) -> AdmissionContext:
        return AdmissionContext(
            contract, policy, history, proposal, now, set(self.actions),
            action_configs=dict(self.actions), budget_reserved=None,
        )

    def _public_decision(self, contract, policy, history, proposal, now) -> dict:
        decision = admit(self._context(contract, policy, history, proposal, now))
        return {"status": decision["status"], "reason": decision["reason"]}

    def _contract(self, contract_id: str, version: int) -> dict:
        found = self.contracts.get((contract_id, version))
        if not found:
            raise Rejected("unknown_contract", f"{contract_id}@{version}")
        return found

    def _policy(self, version: int | None = None) -> dict:
        if version is None:
            if not self.policies:
                raise Rejected("unknown_policy", "install policy first")
            version = max(self.policies)
        found = self.policies.get(version)
        if not found:
            raise Rejected("unknown_policy", "install policy first")
        return found

    def _case(self, case_id: str) -> dict:
        found = self.cases.get(case_id)
        if not found and self._journal is not None and isinstance(case_id, str):
            path = self._journal.thread_path(case_id)
            if path.exists():
                self._load(case_id)
                found = self.cases.get(case_id)
        if not found:
            raise Rejected("unknown_case", case_id)
        return found

    def _proposal(self, history, proposal_id: str) -> dict:
        for event in history:
            if event["kind"] == "proposed" and event["body"]["id"] == proposal_id:
                return event["body"]
        raise Rejected("unknown_proposal", proposal_id)

    def _append(self, case_id: str, kind: str, body: dict, *, rule: str | None = None) -> dict:
        previous = self.events[case_id][-1] if self.events[case_id] else None
        event = {
            "case_id": case_id,
            "seq": previous["seq"] + 1 if previous else 1,
            "kind": kind,
            "body": body,
            "at": stamp(self.clock()),
            "previous": previous["hash"] if previous else "0" * 64,
        }
        event["hash"] = event_digest(event)
        side = {"seq": event["seq"], "jcs": envelope_jcs(event), "rule": rule if kind == "decision" else None}
        if self._journal is not None:
            self._journal.add_event(case_id, event, side)
        self.events[case_id].append(event)
        self._side.setdefault(case_id, {})[event["seq"]] = side
        return event
