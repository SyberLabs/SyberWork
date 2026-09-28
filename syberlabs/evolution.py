"""Candidate changes and the contract section that governs them.

A search provider (a person, a model adapter, an evolutionary search) produces
candidates. A candidate is provisional. Its lineage says where it came from:
parent candidates, the operator that produced it, and the provider revision.
Lineage is provenance, not evidence of superiority. Nothing in this module
reads a parent's evaluation, a provider's score, or a promoted ancestor when
deciding whether a candidate may be promoted.

Promotion is an ordinary contract action. Admission runs the same rule list as
any other action, plus ``candidate.promotable``, which needs the host's own
evaluation of the candidate's exact tree, every required check passed, the
change inside the contract's mutable scope, and a human-origin proposal from a
promotion role. Any approval the contract or policy requires still applies.

The records here are the host's claims, like ``observe(..., verified=True)``.
The host that computes changed paths and runs checks is trusted. The provider
is not: it never receives the session, and the host recomputes scope from the
changed paths it reports.
"""

from __future__ import annotations

import re
from typing import Any

from syberlabs.canonical import canonical
from syberlabs.clock import as_seconds
from syberlabs.errors import Rejected
from syberlabs.evidence import verified_reconciliation


# Roles that belong to automated proposers. An actor holding one cannot propose
# a promotion, even with origin "human".
AUTOMATION_ROLES = frozenset({"model", "compiled", "search"})

EVOLUTION_KEYS = {"scope", "operators", "budget", "evaluation", "promotion"}
CHECK_STATES = ("passed", "failed", "timed_out", "error")
PROMOTION_RANK = {"denied": 0, "not_applied": 1, "admitted": 2, "needs_approval": 2, "unresolved": 3, "promoted": 4}
SEARCH_STOPS = ("completed", "budget_exhausted", "deadline", "cancelled", "error")

DEFAULT_TARGET_REF = "refs/heads/syberlabs/{thread}"
DEFAULTS = {
    "max_files": 50,
    "max_diff_bytes": 1_000_000,
    "max_seconds": 600,
    "max_age_seconds": 3600,
    "max_output_bytes": 65_536,
}

# Hard ceilings. A contract can set lower limits, never higher ones.
MAX_PATHS = 1000
MAX_SCOPE_ENTRIES = 64
MAX_OPERATORS = 16
MAX_CHECKS = 32
MAX_ARGV = 64
MAX_PARENTS = 8
MAX_SIGNAL_BYTES = 4096
MAX_TAIL_CHARS = 4096
MAX_RECORD_BYTES = 65_536

# Paths the host keeps out of every candidate, whatever the scope says.
ALWAYS_EXCLUDED = (".git", ".syberlabs")

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OBJECT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _fail(detail: str) -> Rejected:
    return Rejected("invalid_contract", "evolution: " + detail)


def _int(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _strict(section: Any, name: str, allowed: set, required: set = frozenset()) -> dict:
    if not isinstance(section, dict):
        raise _fail(f"{name} must be an object")
    unknown = set(section) - allowed
    if unknown:
        raise _fail(f"unknown {name} field {sorted(unknown)[0]}")
    missing = set(required) - set(section)
    if missing:
        raise _fail(f"{name} needs {sorted(missing)[0]}")
    return section


def clean_path(value: Any) -> str | None:
    """A relative POSIX path without traversal, or None."""
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value or "\x00" in value:
        return None
    if value == ".":
        return "."
    stripped = value[:-1] if value.endswith("/") else value
    parts = stripped.split("/")
    if value.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return None
    return stripped


def _path_list(values: Any, name: str, *, required: bool) -> list[str]:
    if values is None and not required:
        return []
    if not isinstance(values, list) or (required and not values) or len(values) > MAX_SCOPE_ENTRIES:
        raise _fail(f"{name} must be a list of 1 to {MAX_SCOPE_ENTRIES} paths")
    cleaned = [clean_path(item) for item in values]
    if any(item is None for item in cleaned) or len(set(cleaned)) != len(cleaned):
        raise _fail(f"{name} entries must be distinct relative paths without '..'")
    return cleaned


def _matches(path: str, entry: str) -> bool:
    return entry == "." or path == entry or path.startswith(entry + "/")


def in_scope(scope: dict, path: str) -> bool:
    """True when the contract's mutable scope admits this repository path."""
    cleaned = clean_path(path)
    if cleaned is None or cleaned == ".":
        return False
    if any(_matches(cleaned, entry) for entry in ALWAYS_EXCLUDED):
        return False
    if any(_matches(cleaned, entry) for entry in scope.get("exclude", [])):
        return False
    return any(_matches(cleaned, entry) for entry in scope["paths"])


def _check_ref(value: Any) -> None:
    if not isinstance(value, str) or not value.startswith("refs/heads/") or len(value) > 200:
        raise _fail("promotion target_ref must start with refs/heads/")
    body = value.replace("{thread}", "t")
    if (value.count("{thread}") > 1 or "{" in body or "}" in body or ".." in body or "//" in body or "@{" in body
            or body.endswith(("/", ".", ".lock")) or any(c in body for c in " ~^:?*[\\\x7f") or any(ord(c) < 32 for c in body)
            or any(part.startswith(".") for part in body.split("/"))):
        raise _fail("promotion target_ref is not a valid branch ref")


def check_evolution(doc: dict) -> None:
    """Validate an optional ``evolution`` section. Unknown fields fail closed."""
    if "evolution" not in doc:
        return
    evolution = _strict(doc["evolution"], "section", EVOLUTION_KEYS, EVOLUTION_KEYS)

    scope = _strict(evolution["scope"], "scope", {"paths", "exclude", "max_files", "max_diff_bytes"}, {"paths"})
    _path_list(scope["paths"], "scope.paths", required=True)
    _path_list(scope.get("exclude"), "scope.exclude", required=False)
    if "max_files" in scope and not _int(scope["max_files"], 1, MAX_PATHS):
        raise _fail(f"scope.max_files must be 1 to {MAX_PATHS}")
    if "max_diff_bytes" in scope and not _int(scope["max_diff_bytes"], 1, 10_000_000):
        raise _fail("scope.max_diff_bytes must be 1 to 10000000")

    operators = evolution["operators"]
    if (not isinstance(operators, list) or not operators or len(operators) > MAX_OPERATORS
            or any(not isinstance(op, str) or not _NAME.match(op) for op in operators) or len(set(operators)) != len(operators)):
        raise _fail("operators must be distinct lowercase names")

    budget = _strict(evolution["budget"], "budget", {"max_candidates", "max_evaluations", "max_seconds"}, {"max_candidates"})
    if not _int(budget["max_candidates"], 1, MAX_PATHS):
        raise _fail(f"budget.max_candidates must be 1 to {MAX_PATHS}")
    if "max_evaluations" in budget and not _int(budget["max_evaluations"], 0, MAX_PATHS):
        raise _fail(f"budget.max_evaluations must be 0 to {MAX_PATHS}")
    if "max_seconds" in budget and not _int(budget["max_seconds"], 1, 86_400):
        raise _fail("budget.max_seconds must be 1 to 86400")

    evaluation = _strict(evolution["evaluation"], "evaluation",
                         {"checks", "required", "max_age_seconds", "max_output_bytes"}, {"checks", "required"})
    checks = evaluation["checks"]
    if not isinstance(checks, dict) or not checks or len(checks) > MAX_CHECKS:
        raise _fail(f"evaluation.checks must name 1 to {MAX_CHECKS} checks")
    for name, check in checks.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise _fail("check names must be lowercase names")
        _strict(check, f"check {name}", {"argv", "timeout_seconds"}, {"argv"})
        argv = check["argv"]
        if (not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV
                or any(not isinstance(arg, str) or len(arg) > 4096 or "\x00" in arg for arg in argv) or not argv[0]):
            raise _fail(f"check {name} argv must be a list of strings; no shell is used")
        if "timeout_seconds" in check and not _int(check["timeout_seconds"], 1, 3600):
            raise _fail(f"check {name} timeout_seconds must be 1 to 3600")
    required = evaluation["required"]
    if (not isinstance(required, list) or not required or len(set(required)) != len(required)
            or any(name not in checks for name in required)):
        raise _fail("evaluation.required must list declared checks")
    if "max_age_seconds" in evaluation and not _int(evaluation["max_age_seconds"], 1, 604_800):
        raise _fail("evaluation.max_age_seconds must be 1 to 604800")
    if "max_output_bytes" in evaluation and not _int(evaluation["max_output_bytes"], 1024, 1_048_576):
        raise _fail("evaluation.max_output_bytes must be 1024 to 1048576")

    promotion = _strict(evolution["promotion"], "promotion", {"action", "roles", "approval_role", "target_ref"}, {"action", "roles"})
    action = promotion["action"]
    if not isinstance(action, str) or not isinstance(doc["actions"].get(action), dict):
        raise _fail("promotion.action must be a contract action")
    roles = promotion["roles"]
    if (not isinstance(roles, list) or not roles or any(not isinstance(role, str) or not role for role in roles)
            or AUTOMATION_ROLES.intersection(roles)):
        raise _fail("promotion.roles must be human roles, not " + ", ".join(sorted(AUTOMATION_ROLES)))
    if "approval_role" in promotion:
        approval = promotion["approval_role"]
        if not isinstance(approval, str) or not approval or approval in AUTOMATION_ROLES:
            raise _fail("promotion.approval_role must be a human role")
        if doc["actions"][action].get("approval_role") != approval:
            raise _fail("promotion.approval_role must also be the promotion action's approval_role")
    if "target_ref" in promotion:
        _check_ref(promotion["target_ref"])


def settings(contract: dict) -> dict | None:
    """The evolution section with defaults filled in, or None."""
    evolution = contract.get("evolution")
    if not isinstance(evolution, dict):
        return None
    scope, budget, evaluation, promotion = (evolution["scope"], evolution["budget"],
                                            evolution["evaluation"], evolution["promotion"])
    return {
        "scope": {"paths": [clean_path(p) for p in scope["paths"]],
                  "exclude": [clean_path(p) for p in scope.get("exclude", [])],
                  "max_files": scope.get("max_files", DEFAULTS["max_files"]),
                  "max_diff_bytes": scope.get("max_diff_bytes", DEFAULTS["max_diff_bytes"])},
        "operators": list(evolution["operators"]),
        "budget": {"max_candidates": budget["max_candidates"],
                   "max_evaluations": budget.get("max_evaluations", budget["max_candidates"]),
                   "max_seconds": budget.get("max_seconds", DEFAULTS["max_seconds"])},
        "evaluation": {"checks": {name: {"argv": list(check["argv"]),
                                         "timeout_seconds": check.get("timeout_seconds", 300)}
                                  for name, check in evaluation["checks"].items()},
                       "required": list(evaluation["required"]),
                       "max_age_seconds": evaluation.get("max_age_seconds", DEFAULTS["max_age_seconds"]),
                       "max_output_bytes": evaluation.get("max_output_bytes", DEFAULTS["max_output_bytes"])},
        "promotion": {"action": promotion["action"], "roles": list(promotion["roles"]),
                      "approval_role": promotion.get("approval_role"),
                      "target_ref": promotion.get("target_ref", DEFAULT_TARGET_REF)},
    }


# Record bodies. Each returns the body to append or raises Rejected.

def _record_error(detail: str) -> Rejected:
    return Rejected("invalid_candidate", detail)


def _bounded(body: dict) -> dict:
    if len(canonical(body).encode()) > MAX_RECORD_BYTES:
        raise _record_error(f"record exceeds {MAX_RECORD_BYTES} bytes")
    return body


def _provider(value: Any) -> dict:
    if (not isinstance(value, dict) or set(value) != {"name", "revision"}
            or not isinstance(value["name"], str) or not _NAME.match(value["name"])
            or not isinstance(value["revision"], str) or not 0 < len(value["revision"]) <= 128):
        raise _record_error("provider needs a lowercase name and a revision")
    return {"name": value["name"], "revision": value["revision"]}


def _signal(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(canonical(value).encode()) > MAX_SIGNAL_BYTES:
        raise _record_error(f"signal must be an object of at most {MAX_SIGNAL_BYTES} bytes")
    return value


def registered(history: list, candidate_id: str) -> dict | None:
    return next((event["body"] for event in history
                 if event["kind"] == "candidate_registered" and event["body"].get("id") == candidate_id), None)


def candidate_record(contract: dict, history: list, raw: dict, actor: str) -> dict:
    """Validate a host's candidate record. Scope and limits are recomputed here."""
    config = settings(contract)
    if config is None:
        raise Rejected("evolution_not_enabled", "the contract has no evolution section")
    if not isinstance(raw, dict):
        raise _record_error("candidate must be an object")
    allowed = {"id", "commit", "tree", "base", "parents", "operator", "provider", "changed_paths", "diff", "signal", "note"}
    if set(raw) - allowed or {"id", "commit", "tree", "base", "parents", "operator", "provider", "changed_paths", "diff"} - set(raw):
        raise _record_error("candidate fields are " + ", ".join(sorted(allowed)))
    candidate_id = raw["id"]
    if not isinstance(candidate_id, str) or not _ID.match(candidate_id):
        raise _record_error("candidate id must be 1 to 64 letters, digits, '.', '_' or '-'")
    if registered(history, candidate_id):
        raise _record_error("candidate id already registered")
    for field in ("commit", "tree", "base"):
        if not isinstance(raw[field], str) or not _OBJECT.match(raw[field]):
            raise _record_error(f"{field} must be a full Git object id")
    parents = raw["parents"]
    if (not isinstance(parents, list) or len(parents) > MAX_PARENTS or len(set(parents)) != len(parents)
            or any(not isinstance(p, str) or registered(history, p) is None for p in parents)):
        raise _record_error("parents must be distinct candidates already registered in this thread")
    operator = raw["operator"]
    if operator not in config["operators"]:
        raise Rejected("operator_not_permitted", f"operator {operator!r} is not in the contract")
    count = sum(1 for event in history if event["kind"] == "candidate_registered")
    if count >= config["budget"]["max_candidates"]:
        raise Rejected("budget_exhausted", "max_candidates")
    paths = raw["changed_paths"]
    if not isinstance(paths, list) or len(paths) > MAX_PATHS or any(not isinstance(p, str) for p in paths):
        raise _record_error(f"changed_paths must be at most {MAX_PATHS} strings")
    paths = sorted(set(paths))
    diff = raw["diff"]
    if (not isinstance(diff, dict) or set(diff) != {"digest", "bytes", "files"}
            or not isinstance(diff["digest"], str) or not _DIGEST.match(diff["digest"])
            or not _int(diff["bytes"], 0, 2**53) or not _int(diff["files"], 0, 2**31)):
        raise _record_error("diff needs digest, bytes and files")
    note = raw.get("note", "")
    if not isinstance(note, str) or len(note) > 500:
        raise _record_error("note must be at most 500 characters")
    scope = config["scope"]
    limits = []
    if len(paths) > scope["max_files"] or diff["files"] > scope["max_files"]:
        limits.append("max_files")
    if diff["bytes"] > scope["max_diff_bytes"]:
        limits.append("max_diff_bytes")
    return _bounded({
        "id": candidate_id, "commit": raw["commit"], "tree": raw["tree"], "base": raw["base"],
        "parents": list(parents), "operator": operator, "provider": _provider(raw["provider"]),
        "changed_paths": paths, "scope_violations": [p for p in paths if not in_scope(scope, p)],
        "limit_violations": limits, "diff": dict(diff), "signal": _signal(raw.get("signal")),
        "note": note, "actor": actor,
    })


def evaluation_record(contract: dict, history: list, raw: dict, actor: str) -> dict:
    """Validate the host's evaluation of one candidate's exact commit and tree."""
    config = settings(contract)
    if config is None:
        raise Rejected("evolution_not_enabled", "the contract has no evolution section")
    if not isinstance(raw, dict) or set(raw) != {"candidate", "commit", "tree", "evaluator", "checks"}:
        raise _record_error("evaluation fields are candidate, commit, tree, evaluator, checks")
    candidate = registered(history, raw["candidate"]) if isinstance(raw["candidate"], str) else None
    if candidate is None:
        raise Rejected("unknown_candidate", str(raw["candidate"])[:64])
    if raw["commit"] != candidate["commit"] or raw["tree"] != candidate["tree"]:
        raise Rejected("evaluation_mismatch", "evaluation must name the candidate's registered commit and tree")
    if not isinstance(raw["evaluator"], str) or not 0 < len(raw["evaluator"]) <= 64:
        raise _record_error("evaluator must be a short name")
    count = sum(1 for event in history if event["kind"] == "candidate_evaluated")
    if count >= config["budget"]["max_evaluations"]:
        raise Rejected("budget_exhausted", "max_evaluations")
    checks = raw["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > MAX_CHECKS:
        raise _record_error(f"checks must list 1 to {MAX_CHECKS} results")
    declared = config["evaluation"]["checks"]
    results, names = [], set()
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "state", "exit_code", "duration_ms", "output_digest", "output_tail"}:
            raise _record_error("check result fields are name, state, exit_code, duration_ms, output_digest, output_tail")
        if item["name"] not in declared or item["name"] in names:
            raise _record_error("check results must name distinct declared checks")
        if (item["state"] not in CHECK_STATES or not (item["exit_code"] is None or type(item["exit_code"]) is int)
                or not _int(item["duration_ms"], 0, 2**53) or not isinstance(item["output_digest"], str)
                or not _DIGEST.match(item["output_digest"]) or not isinstance(item["output_tail"], str)
                or len(item["output_tail"]) > MAX_TAIL_CHARS):
            raise _record_error("invalid check result for " + str(item["name"]))
        if item["state"] == "passed" and item["exit_code"] != 0:
            raise _record_error("a passed check must have exit code 0")
        names.add(item["name"])
        results.append(dict(item))
    return _bounded({"candidate": candidate["id"], "commit": candidate["commit"], "tree": candidate["tree"],
                     "evaluator": raw["evaluator"], "checks": results, "actor": actor})


def search_record(contract: dict, history: list, phase: str, raw: dict, actor: str) -> dict:
    """Validate a search_started or search_finished body."""
    config = settings(contract)
    if config is None:
        raise Rejected("evolution_not_enabled", "the contract has no evolution section")
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not _ID.match(raw["id"]):
        raise _record_error("search needs an id")
    started = next((e["body"] for e in history if e["kind"] == "search_started" and e["body"]["id"] == raw["id"]), None)
    finished = any(e["kind"] == "search_finished" and e["body"]["id"] == raw["id"] for e in history)
    if phase == "started":
        if set(raw) != {"id", "provider", "operators", "budget", "base", "context_digest"}:
            raise _record_error("search_started fields are id, provider, operators, budget, base, context_digest")
        if started:
            raise _record_error("search id already used")
        operators = raw["operators"]
        if (not isinstance(operators, list) or not operators or len(set(operators)) != len(operators)
                or any(op not in config["operators"] for op in operators)):
            raise Rejected("operator_not_permitted", "search operators must be contract operators")
        budget = raw["budget"]
        if not isinstance(budget, dict) or set(budget) != set(config["budget"]) or any(
                type(budget[key]) is not int or not 0 <= budget[key] <= config["budget"][key] for key in budget):
            raise _record_error("search budget must not exceed the contract budget")
        if not isinstance(raw["base"], str) or not _OBJECT.match(raw["base"]):
            raise _record_error("base must be a full Git object id")
        if raw["context_digest"] is not None and (not isinstance(raw["context_digest"], str) or not _DIGEST.match(raw["context_digest"])):
            raise _record_error("context_digest must be a SHA-256 hex digest or null")
        return _bounded({**raw, "provider": _provider(raw["provider"]), "operators": list(operators),
                         "budget": dict(budget), "actor": actor})
    if phase == "finished":
        if set(raw) != {"id", "stopped", "candidates", "recommended", "evaluations", "elapsed_ms", "error"}:
            raise _record_error("search_finished fields are id, stopped, candidates, recommended, evaluations, elapsed_ms, error")
        if not started or finished:
            raise _record_error("search_finished needs one open search_started")
        if raw["stopped"] not in SEARCH_STOPS:
            raise _record_error("stopped must be one of " + ", ".join(SEARCH_STOPS))
        for field in ("candidates", "recommended"):
            ids = raw[field]
            if (not isinstance(ids, list) or len(ids) > MAX_PATHS or len(set(ids)) != len(ids)
                    or any(not isinstance(i, str) or registered(history, i) is None for i in ids)):
                raise _record_error(f"{field} must list registered candidates")
        if not set(raw["recommended"]) <= set(raw["candidates"]):
            raise _record_error("recommended candidates must come from this search")
        if not _int(raw["evaluations"], 0, MAX_PATHS) or not _int(raw["elapsed_ms"], 0, 2**53):
            raise _record_error("evaluations and elapsed_ms must be counts")
        if raw["error"] is not None and (not isinstance(raw["error"], str) or not _NAME.match(raw["error"])):
            raise _record_error("error must be a short code or null")
        return _bounded({**raw, "actor": actor})
    raise _record_error("phase must be started or finished")


# Admission and projection.

def latest_evaluation(history: list, candidate: dict) -> dict | None:
    """The newest host evaluation of this candidate's registered commit and tree."""
    return next((event for event in reversed(history) if event["kind"] == "candidate_evaluated"
                 and event["body"]["candidate"] == candidate["id"]
                 and event["body"]["commit"] == candidate["commit"]
                 and event["body"]["tree"] == candidate["tree"]), None)


def evaluation_denial(config: dict, evaluation: dict | None, now) -> str | None:
    if evaluation is None:
        return "candidate_not_evaluated"
    states = {item["name"]: item["state"] for item in evaluation["body"]["checks"]}
    for name in config["evaluation"]["required"]:
        if name not in states:
            return "candidate_check_missing:" + name
        if states[name] != "passed":
            return "candidate_check_failed:" + name
    if as_seconds(now) - as_seconds(evaluation["at"]) > config["evaluation"]["max_age_seconds"]:
        return "candidate_evidence_stale"
    return None


def promotion_denial(contract: dict, history: list, proposal: dict, now) -> str | None:
    """Reason a proposal may not promote its candidate, or None.

    Returns None for any action that is not the contract's promotion action.
    Lineage, parent evaluations, and provider signals are deliberately unused.
    """
    config = settings(contract)
    if config is None or proposal["action"] != config["promotion"]["action"]:
        return None
    roles = proposal.get("roles") or []
    if proposal.get("origin") != "human" or AUTOMATION_ROLES.intersection(roles):
        return "candidate_promotion_origin"
    if not set(config["promotion"]["roles"]).intersection(roles):
        return "candidate_promotion_role"
    args = proposal["args"]
    if set(args) != {"candidate", "commit", "base"} or any(not isinstance(value, str) for value in args.values()):
        return "candidate_args_invalid"
    candidate = registered(history, args["candidate"])
    if candidate is None:
        return "candidate_unknown"
    if candidate["commit"] != args["commit"] or candidate["base"] != args["base"]:
        return "candidate_mismatch"
    if candidate["scope_violations"] or candidate["limit_violations"]:
        return "candidate_out_of_scope"
    return evaluation_denial(config, latest_evaluation(history, candidate), now)


def candidate_views(contract: dict, history: list, now) -> list[dict]:
    """Each candidate with its evaluation and promotion state, derived from events."""
    config = settings(contract)
    if config is None:
        return []
    action = config["promotion"]["action"]
    proposals: dict[str, list[str]] = {}
    for event in history:
        body = event["body"]
        if event["kind"] == "proposed" and body.get("action") == action and isinstance(body.get("args"), dict):
            candidate_id = body["args"].get("candidate")
            if isinstance(candidate_id, str):
                proposals.setdefault(candidate_id, []).append(body["id"])
    decisions, outcomes = {}, {}
    for event in history:
        body = event["body"]
        if event["kind"] == "decision":
            decisions[body["proposal_id"]] = body
        elif event["kind"] in ("effect_started", "effect_unknown", "effect_rejected", "effect_succeeded"):
            outcomes.setdefault(body["proposal_id"], []).append(event["kind"])
        elif verified_reconciliation(event):
            outcomes.setdefault(body["proposal_id"], []).append("reconciled")
    views = []
    for event in history:
        if event["kind"] != "candidate_registered":
            continue
        candidate = event["body"]
        evaluation = latest_evaluation(history, candidate)
        if evaluation is None:
            evaluated = "none"
        else:
            reason = evaluation_denial(config, evaluation, now)
            evaluated = "passed" if reason is None else "stale" if reason == "candidate_evidence_stale" else "failed"
        # An outcome at the destination outranks a pending decision, which outranks
        # a refusal. Among equals the latest proposal wins.
        state, proposal_id, reason, rank = "provisional", None, None, -1
        for pid in proposals.get(candidate["id"], []):
            kinds = outcomes.get(pid, [])
            decision = decisions.get(pid)
            if "effect_succeeded" in kinds or "reconciled" in kinds:
                found = ("promoted", None)
            elif "effect_rejected" in kinds:
                found = ("not_applied", None)
            elif kinds:
                found = ("unresolved", None)
            elif decision is not None:
                found = ({"allowed": "admitted", "needs_approval": "needs_approval"}.get(decision["status"], "denied"),
                         decision["reason"])
            else:
                continue
            order = PROMOTION_RANK[found[0]]
            if order >= rank:
                (state, reason), proposal_id, rank = found, pid, order
        views.append({
            **{key: candidate[key] for key in ("id", "commit", "tree", "base", "parents", "operator", "provider",
                                               "changed_paths", "scope_violations", "limit_violations", "diff",
                                               "signal", "note")},
            "registered_hash": event["hash"],
            "evaluation": {"state": evaluated,
                           "checks": evaluation["body"]["checks"] if evaluation else [],
                           "hash": evaluation["hash"] if evaluation else None},
            "promotion": {"state": state, "proposal_id": proposal_id, "reason": reason},
            "authoritative": state == "promoted",
        })
    return views
