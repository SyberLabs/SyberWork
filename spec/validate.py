"""Validate examples and captured event bodies against the v0alpha1 schemas.

Stdlib only. Understands the draft 2020-12 keywords these schemas use:
type, enum, const, properties, required, additionalProperties, items,
prefixItems, minimum, maximum, minLength, minItems, maxItems, pattern,
allOf, anyOf, oneOf, if/then, and $ref (local fragment or sibling file).

Integer means a Python int that is not bool, matching SyberWork's type checks.
JSON Schema would also treat 1.0 as an integer; this checker does not.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = Path(__file__).resolve().parent
_CACHE: dict[Path, dict] = {}


class SchemaError(Exception):
    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def load_schema(path: Path) -> dict:
    path = path.resolve()
    if path not in _CACHE:
        _CACHE[path] = json.loads(path.read_text())
    return _CACHE[path]


def resolve_ref(ref: str, base: Path, root: dict) -> tuple[dict, Path, dict]:
    if ref.startswith("#"):
        node = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            node = node[part]
        return node, base, root
    file_part, _, fragment = ref.partition("#")
    target = (base.parent / file_part).resolve()
    document = load_schema(target)
    if not fragment:
        return document, target, document
    node = document
    for part in fragment[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node, target, document


def _type_ok(instance, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return type(instance) is int
    if expected == "number":
        return type(instance) in (int, float)
    if expected == "boolean":
        return type(instance) is bool
    if expected == "null":
        return instance is None
    raise SchemaError("$", f"unsupported type {expected}")


def validate(instance, schema, *, base: Path, root: dict | None = None, path: str = "$") -> None:
    if schema is True:
        return
    if schema is False:
        raise SchemaError(path, "schema is false")
    root = schema if root is None else root
    if "$ref" in schema:
        target, target_base, target_root = resolve_ref(schema["$ref"], base, root)
        validate(instance, target, base=target_base, root=target_root, path=path)
        rest = {key: value for key, value in schema.items() if key != "$ref"}
        if rest:
            validate(instance, rest, base=base, root=root, path=path)
        return
    if "allOf" in schema:
        for sub in schema["allOf"]:
            validate(instance, sub, base=base, root=root, path=path)
    if "anyOf" in schema:
        errors = []
        for sub in schema["anyOf"]:
            try:
                validate(instance, sub, base=base, root=root, path=path)
                break
            except SchemaError as error:
                errors.append(error)
        else:
            raise SchemaError(path, "no anyOf branch matched: " + "; ".join(str(e) for e in errors))
    if "oneOf" in schema:
        matched = 0
        errors = []
        for sub in schema["oneOf"]:
            try:
                validate(instance, sub, base=base, root=root, path=path)
                matched += 1
            except SchemaError as error:
                errors.append(error)
        if matched != 1:
            raise SchemaError(path, f"oneOf matched {matched}: " + "; ".join(str(e) for e in errors))
    if "if" in schema:
        try:
            validate(instance, schema["if"], base=base, root=root, path=path)
            matched = True
        except SchemaError:
            matched = False
        branch = "then" if matched else "else"
        if branch in schema:
            validate(instance, schema[branch], base=base, root=root, path=path)
    if "const" in schema and instance != schema["const"]:
        raise SchemaError(path, f"expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(path, f"{instance!r} not in enum")
    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, item) for item in options):
            raise SchemaError(path, f"expected {expected}, got {type(instance).__name__}")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in instance:
                validate(instance[key], sub, base=base, root=root, path=f"{path}.{key}")
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                raise SchemaError(path, f"missing {key}")
        if "additionalProperties" in schema:
            extra = set(instance) - set(properties)
            additional = schema["additionalProperties"]
            if additional is False and extra:
                raise SchemaError(path, "unexpected properties " + ", ".join(sorted(extra)))
            if isinstance(additional, dict):
                for key in extra:
                    validate(instance[key], additional, base=base, root=root, path=f"{path}.{key}")
    if isinstance(instance, list):
        if "prefixItems" in schema:
            for index, sub in enumerate(schema["prefixItems"]):
                if index < len(instance):
                    validate(instance[index], sub, base=base, root=root, path=f"{path}[{index}]")
            rest = instance[len(schema["prefixItems"]):]
            item_schema = schema.get("items", True)
            if item_schema is False and rest:
                raise SchemaError(path, "array longer than prefixItems")
            if item_schema is not False:
                for offset, item in enumerate(rest):
                    index = offset + len(schema["prefixItems"])
                    validate(item, item_schema, base=base, root=root, path=f"{path}[{index}]")
        elif "items" in schema and schema["items"] is not True:
            for index, item in enumerate(instance):
                validate(item, schema["items"], base=base, root=root, path=f"{path}[{index}]")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(path, "minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(path, "maxItems")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(path, "minLength")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise SchemaError(path, f"pattern {schema['pattern']}")
    if type(instance) in (int, float) and type(instance) is not bool:
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(path, "minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(path, "maximum")


def check(instance, schema_name: str) -> None:
    path = SPEC / schema_name
    schema = load_schema(path)
    validate(instance, schema, base=path, root=schema)


def validate_examples() -> list[str]:
    errors = []
    pairs = [
        (ROOT / "examples" / "contract.json", "contract.schema.json"),
        (ROOT / "examples" / "policy.json", "policy.schema.json"),
        (ROOT / "syberwork" / "examples" / "contract.json", "contract.schema.json"),
        (ROOT / "syberwork" / "examples" / "policy.json", "policy.schema.json"),
    ]
    for path, schema_name in pairs:
        try:
            check(json.loads(path.read_text()), schema_name)
        except SchemaError as error:
            errors.append(f"{path}: {error}")
    for directory in (ROOT / "examples", ROOT / "syberwork" / "examples"):
        actions = json.loads((directory / "actions.json").read_text())
        sources = json.loads((directory / "sources.json").read_text())
        for name, doc in actions.items():
            try:
                check(doc, "action-definition.schema.json")
            except SchemaError as error:
                errors.append(f"{directory / 'actions.json'}#{name}: {error}")
        for name, doc in sources.items():
            try:
                check(doc, "source-definition.schema.json")
            except SchemaError as error:
                errors.append(f"{directory / 'sources.json'}#{name}: {error}")
    sys.path.insert(0, str(ROOT))
    from case_studies.enterprise_procurement import CONTRACT, POLICY
    try:
        check(CONTRACT, "contract.schema.json")
    except SchemaError as error:
        errors.append(f"enterprise contract: {error}")
    try:
        check(POLICY, "policy.schema.json")
    except SchemaError as error:
        errors.append(f"enterprise policy: {error}")
    return errors


def project_effect(event: dict) -> dict | None:
    kind = event["kind"]
    body = event["body"]
    if kind == "effect_succeeded":
        return {"state": "succeeded", **body}
    if kind == "effect_rejected":
        projected = {key: value for key, value in body.items() if key != "status"}
        projected["state"] = "rejected"
        if body["status"] == "not_applied":
            projected["local_status"] = "not_applied"
        else:
            projected["http_status"] = body["status"]
        return projected
    if kind == "effect_unknown":
        return {"state": "unknown", **body}
    return None


def validate_events(events: list[dict], label: str) -> list[str]:
    errors = []
    for index, event in enumerate(events):
        where = f"{label}[{index}] {event.get('kind')}"
        try:
            check(event, "event.schema.json")
            check({"kind": event["kind"], "body": event["body"]}, "event-body.schema.json")
            if event["kind"] == "decision":
                check({"status": event["body"]["status"], "reason": event["body"]["reason"]}, "admission-decision.schema.json")
            projected = project_effect(event)
            if projected is not None:
                check(projected, "effect-outcome.schema.json")
        except SchemaError as error:
            errors.append(f"{where}: {error}")
    return errors


def validate_golden() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from conformance.capture import collect
    errors = []
    for trace in collect():
        errors.extend(validate_events(trace["raw_events"], trace["id"]))
    return errors


def main() -> None:
    errors = validate_examples()
    if "--golden" in sys.argv:
        errors.extend(validate_golden())
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    scope = "examples and golden events" if "--golden" in sys.argv else "examples"
    print(f"validated {scope} against sdk.syberlabs.space/v0alpha1")


if __name__ == "__main__":
    main()
