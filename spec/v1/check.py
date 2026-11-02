"""Check the sdk.syberlabs.space/v1alpha1 fixtures and the v0alpha1 compatibility promise.

Stdlib only; reuses the v0alpha1 checker in spec/validate.py rather than adding a second
validator. Three checks:

1. Every fixture under spec/v1/fixtures/ behaves as labelled: each ``valid`` instance
   validates, each ``invalid`` instance is refused by its schema, and each
   ``digest_mismatch`` capability validates but fails the digest rule.
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
                check_v1(case["instance"], schema_name)
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


def main() -> None:
    errors, fixtures = check_fixtures()
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
