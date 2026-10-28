"""The eval spec: a JSON document checked against ``SCHEMA``, frozen by ``register``, refused by ``run`` once it moves.

``evals/<id>/spec.json`` states, before anything runs, what is asked and how it is answered: the question and the
hypothesis, the arms (``arms.py``), the task set by id and by digest (``tasks.py``), the primary and secondary
metrics, the per-run budget (calls, dollars, seconds) with the price table the governor applies and the dollar cap
of the whole eval, the seeds, the seed of the run order, the exclusions, a structured decision rule, what is
reported whatever the outcome, and ``hidden_feedback``: how much of a hidden check's result reaches the proposer
(``tails``: name, state, exit code, duration and output tail, as every check's result; ``status``: the name and
passed or failed only; ``none``: nothing of a hidden check, only the visible checks' results). The mode is part of
the spec and so of its digest.

``register(spec_path)`` validates the spec, recomputes every task's digest and the set digest the spec declares
(``task_set_mismatch`` when they differ), and writes ``evals/<id>/registration.json``: ``{spec_digest,
task_digests, task_set_digest, arm_digests, registered_at, git_commit, git_dirty}``. The spec digest is
``syberlabs.canonical.digest`` of the parsed document, so whitespace and line endings do not move it and any field
does. A registration is written once: registering the same spec again returns it, and registering a changed spec
over it is refused (``already_registered``); a changed question is a new eval id. ``registered(spec_path)`` is
what ``run`` asks first: ``spec_unregistered``, ``spec_modified`` (the spec's digest is not the registered one) and
``task_modified`` (a task's ``task.json`` or hidden file moved) are refusals, never warnings.

The schema is JSON Schema (draft 2020-12 vocabulary) held here as a Python value; ``validate`` implements the
subset it uses (``type``, ``const``, ``enum``, ``properties``, ``required``, ``additionalProperties: false``,
``items``, ``minItems``, ``maxItems``, ``uniqueItems``, ``minLength``, ``maxLength``, ``pattern``, ``minimum``,
``maximum``, ``exclusiveMinimum``; a number must also be finite, because ``NaN`` passes every comparison), standard
library only, then the cross-field rules a schema cannot state (``check``): arm names are distinct, the decision
rule names two of the spec's arms and its metric is the primary metric, each arm is valid for its kind, no two
cells share a run id (``<task>.<arm>.s<seed>``, where task ids and arm names may hold dots), no two ``githits``
arms share a GitHits agent label (``arms.githits_arm``, which shortens and rewrites names), the price table is one
``syberlabs.economic.check_prices`` accepts, and an evolve arm runs only under ``tails`` (its mutate requests are
built in ``syberlabs.evolve``, where no filter is set). ``read_json`` and ``loads`` are strict: a key repeated in
one object, ``NaN`` and ``Infinity`` are refused, so what a reader sees in the file is what the digest freezes.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from syberlabs.canonical import canonical, digest
from syberlabs.economic import check_prices
from syberlabs.errors import Rejected

SPEC_SCHEMA_ID = "syberlabs.eval/spec.v1"
REGISTRATION_SCHEMA_ID = "syberlabs.eval/registration.v1"
HARNESS = Path(__file__).resolve().parents[2]  # the SyberWork checkout this package runs from
NAME = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
HEX64 = r"^[0-9a-f]{64}$"
ARM_KINDS = ("single", "repair", "evolve", "evolve_jev", "claude-code")
HIDDEN_FEEDBACK = ("tails", "status", "none")  # what of a hidden check's result reaches the proposer (module docstring)
LANES = ("api", "subscription_local", "seat_team_enterprise", "provider_3p")
MAX_TOTAL_DOLLARS = 1000.0

_TEXT = {"type": "string", "minLength": 1, "maxLength": 4000}
_ARGV = {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string", "minLength": 1, "maxLength": 4096}}
_METRIC = {"type": "object", "additionalProperties": False, "required": ["name", "definition"],
           "properties": {"name": {"type": "string", "pattern": NAME}, "definition": _TEXT}}

ARM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "kind", "model", "effort"],
    "properties": {
        "name": {"type": "string", "pattern": NAME},
        "kind": {"enum": list(ARM_KINDS)},
        "model": {"type": "string", "minLength": 1, "maxLength": 64},
        "effort": {"type": "string", "minLength": 1, "maxLength": 32},
        "adapter": _ARGV,
        "max_calls": {"type": "integer", "minimum": 1, "maximum": 1000},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
        "population": {"type": "integer", "minimum": 2, "maximum": 64},
        "generations": {"type": "integer", "minimum": 1, "maximum": 1000},
        "crossover_every": {"type": "integer", "minimum": 1, "maximum": 1000},
        "lane": {"enum": list(LANES)},
        "bare": {"type": "boolean"},
        "trusted_tree": {"type": "boolean"},
        "tools": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                  "items": {"type": "string", "minLength": 1, "maxLength": 128}},
        "max_turns": {"type": "integer", "minimum": 1, "maximum": 1000},
        "githits": {"type": "boolean"},
        "mcp_config": {"type": "string", "minLength": 1, "maxLength": 4096},
        "executable": _ARGV,
        "notes": _TEXT,
    },
}

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SPEC_SCHEMA_ID,
    "type": "object", "additionalProperties": False,
    "required": ["schema", "id", "question", "hypothesis", "arms", "tasks", "primary_metric", "secondary_metrics",
                 "budget", "seeds", "order_seed", "exclusions", "decision_rule", "reporting", "hidden_feedback"],
    "properties": {
        "schema": {"const": SPEC_SCHEMA_ID},
        "id": {"type": "string", "pattern": NAME},
        "question": _TEXT,
        "hypothesis": _TEXT,
        "arms": {"type": "array", "minItems": 1, "maxItems": 16, "items": ARM_SCHEMA},
        "tasks": {"type": "object", "additionalProperties": False, "required": ["ids", "set_digest"],
                  "properties": {"ids": {"type": "array", "minItems": 1, "maxItems": 1000, "uniqueItems": True,
                                         "items": {"type": "string", "pattern": NAME}},
                                 "set_digest": {"type": "string", "pattern": HEX64}}},
        "primary_metric": _METRIC,
        "secondary_metrics": {"type": "array", "maxItems": 32, "items": _METRIC},
        "budget": {"type": "object", "additionalProperties": False,
                   "required": ["calls", "dollars", "seconds", "total_dollars", "prices"],
                   "properties": {"calls": {"type": "integer", "minimum": 1, "maximum": 1000},
                                  "dollars": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_TOTAL_DOLLARS},
                                  "seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                                  "total_dollars": {"type": "number", "exclusiveMinimum": 0,
                                                    "maximum": MAX_TOTAL_DOLLARS},
                                  "prices": {"type": "object"},
                                  "strict": {"type": "boolean"}}},
        "seeds": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True,
                  "items": {"type": "integer", "minimum": 0, "maximum": 2**31 - 1}},
        "order_seed": {"type": "integer", "minimum": 0, "maximum": 2**31 - 1},
        "exclusions": {"type": "array", "maxItems": 32,
                       "items": {"type": "object", "additionalProperties": False, "required": ["id", "rule"],
                                 "properties": {"id": {"type": "string", "pattern": NAME}, "rule": _TEXT}}},
        "decision_rule": {"type": "object", "additionalProperties": False,
                          "required": ["metric", "comparison", "treatment", "baseline", "operator", "threshold",
                                       "unit", "aggregation"],
                          "properties": {"metric": {"type": "string", "pattern": NAME},
                                         "comparison": {"enum": ["paired_difference"]},
                                         "treatment": {"type": "string", "pattern": NAME},
                                         "baseline": {"type": "string", "pattern": NAME},
                                         "operator": {"enum": [">=", ">", "<=", "<"]},
                                         "threshold": {"type": "number"},
                                         "unit": {"const": "task"},
                                         "aggregation": {"enum": ["mean_over_seeds"]},
                                         "test": {"enum": ["sign", "bootstrap", "none"]}}},
        "reporting": {"type": "array", "minItems": 1, "maxItems": 64, "items": _TEXT},
        "hidden_feedback": {"enum": list(HIDDEN_FEEDBACK)},
        "notes": _TEXT,
    },
}

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(value: Any, kind: str) -> bool:
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) in (int, float)
    return isinstance(value, _TYPES[kind])


def _walk(value: Any, schema: dict, path: str, errors: list[str]) -> None:
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_is(value, kind) for kind in kinds):
            errors.append(f"{path}: expected {' or '.join(kinds)}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
            errors.append(f"{path}: length out of range")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']}")
    if type(value) is float and not math.isfinite(value):
        errors.append(f"{path}: must be a finite number")  # NaN passes every comparison below
    if type(value) in (int, float):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must be above {schema['exclusiveMinimum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            errors.append(f"{path}: item count out of range")
        if schema.get("uniqueItems") and len({canonical(item) for item in value}) != len(value):
            errors.append(f"{path}: items must be distinct")
        if "items" in schema:
            for index, item in enumerate(value):
                _walk(item, schema["items"], f"{path}[{index}]", errors)
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _walk(item, properties[key], f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unknown field")


def validate_schema(value: Any, schema: dict, path: str = "$") -> list[str]:
    """The ways ``value`` breaks ``schema`` (the subset of JSON Schema this module uses); empty when it conforms."""
    errors: list[str] = []
    _walk(value, schema, path, errors)
    return errors


def check(doc: Any) -> list[str]:
    """Every reason ``doc`` is not a valid eval spec: the schema first, then the cross-field rules."""
    errors = validate_schema(doc, SCHEMA)
    if errors:
        return errors
    from syberlabs.eval import arms

    names = [arm["name"] for arm in doc["arms"]]
    if len(set(names)) != len(names):
        errors.append("$.arms: arm names must be distinct")
    for arm in doc["arms"]:
        errors += [f"$.arms[{arm['name']}]: {reason}" for reason in arms.check(arm, doc["budget"])]
    rule = doc["decision_rule"]
    for side in ("treatment", "baseline"):
        if rule[side] not in names:
            errors.append(f"$.decision_rule.{side}: {rule[side]!r} is not an arm of this spec")
    if rule["treatment"] == rule["baseline"]:
        errors.append("$.decision_rule: treatment and baseline must be different arms")
    if rule["metric"] != doc["primary_metric"]["name"]:
        errors.append("$.decision_rule.metric: the decision is taken on the primary metric")
    from syberlabs.eval.runner import run_id

    shared = sorted(found for found, count in Counter(run_id(task, arm, seed) for task in doc["tasks"]["ids"]
                                                      for arm in names for seed in doc["seeds"]).items() if count > 1)
    if shared:
        errors.append(f"$: two cells share the run id {shared[0]} (task ids and arm names may hold dots); "
                      "rename an arm or a task")
    labels = Counter(arms.githits_arm(arm["name"]) for arm in doc["arms"] if arm.get("githits") is True)
    errors += [f"$.arms: two githits arms share the GitHits agent label {label!r} (names are cut to 16 characters "
               "and every character outside A-Z, a-z, 0-9, _ and - becomes -); rename one"
               for label, count in sorted(labels.items()) if count > 1]
    reason = check_prices(doc["budget"]["prices"])
    if reason:
        errors.append("$.budget." + reason)
    if doc["budget"]["dollars"] > doc["budget"]["total_dollars"]:
        errors.append("$.budget.dollars: one run's dollars cannot exceed the eval's total_dollars")
    if doc["hidden_feedback"] != "tails":
        errors += [f"$.arms[{arm['name']}]: an evolve arm runs only under hidden_feedback tails (its mutate requests "
                   "carry feedback no eval filter reaches)" for arm in doc["arms"] if arm["kind"] in ("evolve", "evolve_jev")]
    return errors


def spec_digest(doc: dict) -> str:
    """The canonical digest of the parsed spec (``syberlabs.canonical.digest``)."""
    return digest(doc)


def _object(pairs: list) -> dict:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key appears twice in one object")  # json.loads would keep the last one silently
    return found


def _constant(name: str):
    raise ValueError(f"{name} is not a JSON number")


def loads(text: str) -> Any:
    """Strict JSON text: a key repeated in one object, ``NaN`` and ``Infinity`` raise ``ValueError``."""
    return json.loads(text, object_pairs_hook=_object, parse_constant=_constant)


def read_json(path: Path, code: str) -> Any:
    """Strict JSON (``loads``): a key repeated in one object, ``NaN`` and ``Infinity`` are refused with ``code``."""
    try:
        return loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise Rejected(code, f"{path}: {exc.strerror or type(exc).__name__}") from None
    except ValueError as exc:
        raise Rejected(code, f"{path}: not JSON ({exc})") from None


def write_json(path: Path, value: Any) -> None:
    """Sorted keys, two-space indent, LF, UTF-8: the same bytes on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def harness_state(root: Path = HARNESS) -> dict:
    """The checkout's ``HEAD`` commit and whether it differs from it outside ``evals/`` (where runs write)."""
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout if done.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude)evals")
    return {"commit": commit.strip() if commit else None, "dirty": None if status is None else bool(status.strip())}


def load(spec_path: str | Path) -> dict:
    """The spec at ``spec_path``, validated; it must sit in a directory named by its id (``evals/<id>/spec.json``)."""
    path = Path(spec_path).resolve()
    doc = read_json(path, "spec_unreadable")
    errors = check(doc)
    if errors:
        raise Rejected("invalid_spec", f"{path.name}: " + "; ".join(errors[:8]) + (" ..." if len(errors) > 8 else ""))
    if path.parent.name != doc["id"]:
        raise Rejected("spec_location", f"spec {doc['id']!r} must be at evals/{doc['id']}/spec.json, not {path}")
    return doc


def _digests(doc: dict, evals_root: Path) -> tuple[dict, str, dict]:
    from syberlabs.eval import arms, tasks

    found = {task_id: tasks.task_digest(evals_root / "tasks", task_id) for task_id in doc["tasks"]["ids"]}
    return found, tasks.set_digest(found), {arm["name"]: arms.arm_digest(arm) for arm in doc["arms"]}


def register(spec_path: str | Path, *, harness: Path = HARNESS) -> dict:
    """Freeze ``spec_path``: write and return ``registration.json`` beside it (see the module docstring)."""
    path = Path(spec_path).resolve()
    doc = load(path)
    task_digests, set_found, arm_digests = _digests(doc, path.parent.parent)
    if set_found != doc["tasks"]["set_digest"]:
        raise Rejected("task_set_mismatch", f"the tasks as they are now digest to {set_found}; the spec declares "
                       f"{doc['tasks']['set_digest']} (syberlabs eval tasks list {' '.join(doc['tasks']['ids'])})")
    found = spec_digest(doc)
    target = path.parent / "registration.json"
    if target.exists():
        existing = read_json(target, "registration_unreadable")
        if isinstance(existing, dict) and existing.get("spec_digest") == found:
            return existing
        raise Rejected("already_registered", f"{doc['id']} was registered with another spec digest; a changed spec "
                       "is a new eval id")
    state = harness_state(harness)
    registration = {"schema": REGISTRATION_SCHEMA_ID, "eval": doc["id"], "spec_digest": found,
                    "task_digests": task_digests, "task_set_digest": set_found, "arm_digests": arm_digests,
                    "registered_at": now_utc(), "git_commit": state["commit"], "git_dirty": state["dirty"]}
    write_json(target, registration)
    return registration


def registered(spec_path: str | Path) -> tuple[dict, dict]:
    """``(spec, registration)`` when the spec, its tasks and its arms are exactly as registered; else ``Rejected``."""
    path = Path(spec_path).resolve()
    target = path.parent / "registration.json"
    if not target.exists():
        raise Rejected("spec_unregistered", f"register it first: syberlabs eval register {path}")
    registration = read_json(target, "registration_unreadable")
    if not isinstance(registration, dict) or registration.get("schema") != REGISTRATION_SCHEMA_ID:
        raise Rejected("registration_unreadable", f"{target} is not a registration")
    raw = read_json(path, "spec_unreadable")
    if not isinstance(raw, dict) or spec_digest(raw) != registration.get("spec_digest"):
        raise Rejected("spec_modified", f"{path.name} no longer digests to the registered "
                       f"{str(registration.get('spec_digest'))[:12]}; a changed spec is a new eval id")
    doc = load(path)
    task_digests, set_found, arm_digests = _digests(doc, path.parent.parent)
    moved = sorted(t for t, d in task_digests.items() if registration.get("task_digests", {}).get(t) != d)
    if moved or set_found != registration.get("task_set_digest"):
        raise Rejected("task_modified", "tasks changed since registration: " + (", ".join(moved) or "the set"))
    if arm_digests != registration.get("arm_digests"):
        raise Rejected("spec_modified", "an arm's digest differs from its registration")
    return doc, registration
