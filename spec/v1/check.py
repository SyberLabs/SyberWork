"""Check the sdk.syberlabs.space/v1alpha1 fixtures and the v0alpha1 compatibility promise.

Stdlib only; reuses the v0alpha1 checker in spec/validate.py rather than adding a second
validator. Three checks:

1. Every fixture under spec/v1/fixtures/ behaves as labelled: each ``valid`` instance
   validates, each ``invalid`` instance is refused by its schema, each
   ``procedural_invalid`` instance passes its schema but fails a count rule in
   ``check_decision_call``, and each ``digest_mismatch`` capability validates but fails
   the digest rule. The v0alpha1 schema files match the digests in v0alpha1.lock.json.
2. Every valid capability's ``digest`` equals SHA-256 over canonical JSON of the manifest
   with ``digest`` and ``approval`` removed. Canonical JSON is the v0alpha1 encoding:
   sorted keys, compact separators, non-ASCII preserved.
3. With ``--golden``: every raw event behind the 23 v0alpha1 golden traces validates
   against spec/v1/event.schema.json, i.e. v1alpha1 changes no recorded envelope.

Run: ``PYTHONPATH=. python3 -m spec.v1.check [--golden]``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from spec.validate import ROOT, SchemaError, load_schema, validate

V1 = Path(__file__).resolve().parent
FIXTURES = V1 / "fixtures"


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def capability_digest(manifest: dict) -> str:
    body = {key: value for key, value in manifest.items() if key not in ("digest", "approval")}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def check_v1(instance, schema_name: str) -> None:
    path = V1 / schema_name
    schema = load_schema(path)
    validate(instance, schema, base=path, root=schema)


MAX_OPTIONS = 255


def check_decision_call(instance: dict) -> None:
    """Schema plus the counts JSON Schema could say but spec/validate.py does not support (minProperties)."""
    check_v1(instance, "decision-call.schema.json")
    request = instance["request"]
    if not request["questions"]:
        raise SchemaError("$.request.questions", "at least one question")
    if request["model"] != instance["provider"]["model"]:
        raise SchemaError("$.request.model", "must equal provider.model")
    for name, question in request["questions"].items():
        where = f"$.request.questions.{name}.criteria"
        if question["type"] == "choice" and not 1 <= len(question["criteria"]) <= MAX_OPTIONS:
            raise SchemaError(where, f"a choice offers 1 to {MAX_OPTIONS} options")
        if question["type"] == "score" and len(question["criteria"]) > MAX_OPTIONS:
            raise SchemaError(where, f"at most {MAX_OPTIONS} score levels")


CHECKS = {"decision-call.schema.json": check_decision_call}


def check_instance(instance, schema_name: str) -> None:
    CHECKS.get(schema_name, lambda value: check_v1(value, schema_name))(instance)


V0_LOCK = V1 / "v0alpha1.lock.json"


def v0alpha1_digests() -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((V1.parent).glob("*.schema.json"))}


def load_fixtures() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) | {"_file": path.name}
            for path in sorted(FIXTURES.glob("*.json"))]


def check_fixtures() -> tuple[list[str], int]:
    errors: list[str] = []
    count = 0
    for doc in load_fixtures():
        schema_name = doc["schema"]
        is_capability = schema_name == "capability.schema.json"
        for case in doc.get("valid", []):
            count += 1
            where = f"{doc['_file']} valid: {case['name']}"
            try:
                check_instance(case["instance"], schema_name)
            except SchemaError as error:
                errors.append(f"{where}: {error}")
                continue
            if is_capability and case["instance"]["digest"] != capability_digest(case["instance"]):
                errors.append(f"{where}: digest does not match the manifest")
        for case in doc.get("invalid", []):
            count += 1
            try:
                check_v1(case["instance"], schema_name)
            except SchemaError:
                continue
            errors.append(f"{doc['_file']} invalid: {case['name']}: accepted, expected refusal")
        for case in doc.get("procedural_invalid", []):
            count += 1
            where = f"{doc['_file']} procedural_invalid: {case['name']}"
            try:
                check_v1(case["instance"], schema_name)
            except SchemaError as error:
                errors.append(f"{where}: refused by schema, expected only a procedural refusal: {error}")
                continue
            try:
                check_instance(case["instance"], schema_name)
            except SchemaError:
                continue
            errors.append(f"{where}: accepted, expected refusal")
        for case in doc.get("digest_mismatch", []):
            count += 1
            where = f"{doc['_file']} digest_mismatch: {case['name']}"
            try:
                check_v1(case["instance"], schema_name)
            except SchemaError as error:
                errors.append(f"{where}: refused by schema, expected only a digest failure: {error}")
                continue
            if case["instance"]["digest"] == capability_digest(case["instance"]):
                errors.append(f"{where}: digest matches, expected a mismatch")
    return errors, count


def check_golden() -> tuple[list[str], int]:
    sys.path.insert(0, str(ROOT))
    from conformance.capture import collect

    errors: list[str] = []
    count = 0
    for trace in collect():
        for index, event in enumerate(trace["raw_events"]):
            count += 1
            try:
                check_v1(event, "event.schema.json")
            except SchemaError as error:
                errors.append(f"{trace['id']}[{index}] {event.get('kind')}: {error}")
    return errors, count


def check_v0alpha1_lock() -> list[str]:
    locked = json.loads(V0_LOCK.read_text(encoding="utf-8"))["files"]
    current = v0alpha1_digests()
    errors = [f"spec/{name}: changed since v1alpha1 was drafted" for name in locked if current.get(name) != locked[name]]
    errors += [f"spec/{name}: new v0alpha1 schema not in the lock" for name in current if name not in locked]
    return errors


def main() -> None:
    errors, fixtures = check_fixtures()
    errors.extend(check_v0alpha1_lock())
    summary = f"{fixtures} v1alpha1 fixture cases"
    if "--golden" in sys.argv:
        golden_errors, events = check_golden()
        errors.extend(golden_errors)
        summary += f" and {events} v0alpha1 golden events"
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print(f"checked {summary} against sdk.syberlabs.space/v1alpha1")


if __name__ == "__main__":
    main()
