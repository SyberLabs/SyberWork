"""Contract publication checks shared by Work and the in-memory session."""

from __future__ import annotations

from syberlabs.errors import Rejected


def _text(value) -> bool:
    return isinstance(value, str) and bool(value)


def require_input_kinds(doc: dict) -> None:
    """Only string and integer inputs. Other kinds are not a case schema."""
    inputs = doc.get("inputs")
    if not isinstance(inputs, dict):
        raise Rejected("invalid_contract", "inputs must be an object")
    for key, kind in inputs.items():
        if kind not in ("string", "integer"):
            raise Rejected("invalid_contract", f"input {key} must be string or integer")


def check_case_inputs(schema: dict, inputs: dict) -> None:
    """Reject a case whose keys or value kinds do not match the contract."""
    if set(inputs) != set(schema):
        raise Rejected("input_schema", "input keys must exactly match contract")
    for key, kind in schema.items():
        value = inputs[key]
        if kind == "string":
            ok = isinstance(value, str)
        elif kind == "integer":
            ok = type(value) is int
        else:
            ok = False
        if not ok:
            raise Rejected("input_schema", f"invalid {key}: expected {kind}")


def require_indexed_fields(doc: dict) -> None:
    """Reject acceptance clauses and fact requirements admission later indexes."""
    for clause in doc["acceptance"]:
        kind = clause.get("kind")
        if kind == "effect" and not _text(clause.get("action")):
            raise Rejected("invalid_contract", "effect acceptance needs an action")
        if kind == "signoff" and not _text(clause.get("role")):
            raise Rejected("invalid_contract", "signoff acceptance needs a role")
        if kind == "fact" and not _text(clause.get("key")):
            raise Rejected("invalid_contract", "fact acceptance needs a key")
    for spec in doc["actions"].values():
        if not isinstance(spec, dict):
            continue
        facts = spec.get("required_facts", [])
        if not isinstance(facts, list):
            raise Rejected("invalid_contract", "required_facts must be a list")
        for item in facts:
            if not isinstance(item, dict) or not _text(item.get("key")) or not _text(item.get("source")):
                raise Rejected("invalid_contract", "required fact needs key and source")


def compile_path(doc: dict) -> list[str]:
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


def prepare_contract(doc: dict) -> dict:
    """Return the document to store. Raises Rejected with the same codes as Work."""
    required = ("id", "version", "inputs", "actions", "acceptance")
    if any(k not in doc for k in required) or not isinstance(doc["version"], int):
        raise Rejected("invalid_contract", "id, integer version, inputs, actions, acceptance are required")
    require_input_kinds(doc)
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
    require_indexed_fields(doc)
    return {**doc, "compiled_path": compile_path(doc)}
