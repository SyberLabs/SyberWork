"""Replay the recorded scenarios against the code under evaluation.

Usage: python benchmarks/run_scenarios.py [--scenario NAME] [--json]
                                          [--expected PATH] [--record]

Every module under ``benchmarks/scenarios`` that publishes ``SCENARIO`` is run
in-process on ``SessionHost`` (``syberlabs.Session``; two scenarios also open a
``syberwork.Work`` on a temporary SQLite file). The report each run returns is
compared, field by field, with the record in ``benchmarks/scenarios/expected.json``
and with the module's own ``expected_failures``.

Exit codes: 0 when every recorded expectation holds; 1 on the first divergence,
with the scenario name and the differing field printed; 2 on a malformed scenario
or a malformed record. Standard library only. No server, no network, no clock.

``--record`` rewrites the record from this run. It is for a reviewed change of
expectations on a developer machine, never for a check.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.scenarios.host import SessionHost  # noqa: E402
from benchmarks.scenarios.probe import Probe  # noqa: E402
from benchmarks.scenarios.runner import run_scenario  # noqa: E402

EXPECTED_PATH = ROOT / "benchmarks" / "scenarios" / "expected.json"
SKIP = frozenset({"probe", "runner", "host", "world", "support"})
REPORT_FIELDS = (
    "payments", "spent", "balance", "invoices", "duplicate_debits", "unsafe_payments",
    "effect_started_before_review", "unknown_status", "unknown_succeeded", "unknown_as_success",
    "chain_valid", "complete", "rules", "decisions",
)
OK, DIVERGED, MALFORMED = 0, 1, 2


class Malformed(Exception):
    """A scenario module or the record cannot be replayed as written."""


class Divergence(Exception):
    def __init__(self, scenario: str, field: str, expected, observed) -> None:
        super().__init__(f"{scenario}: {field}: expected {expected!r}, observed {observed!r}")
        self.scenario, self.field, self.expected, self.observed = scenario, field, expected, observed


def validate_scenario(scenario, origin: str = "scenario"):
    """Return the scenario when it has the shape the runner replays; raise Malformed otherwise."""
    identifier = getattr(scenario, "id", None)
    if not isinstance(identifier, str) or not identifier:
        raise Malformed(f"{origin}: SCENARIO.id must be a non-empty string")
    for name in ("world", "contract", "policy", "play", "probes"):
        if not callable(getattr(scenario, name, None)):
            raise Malformed(f"{identifier}: SCENARIO.{name} is not callable")
    failures = getattr(scenario, "expected_failures", [])
    if not isinstance(failures, list) or not all(isinstance(item, str) for item in failures):
        raise Malformed(f"{identifier}: expected_failures must be a list of probe ids")
    try:
        probes = list(scenario.probes())
    except Exception as error:  # a probe with an unsupported field raises ValueError
        raise Malformed(f"{identifier}: probes(): {error}") from error
    if not probes or not all(isinstance(item, Probe) for item in probes):
        raise Malformed(f"{identifier}: probes() must return at least one Probe")
    ids = [item.id for item in probes]
    if len(set(ids)) != len(ids):
        raise Malformed(f"{identifier}: duplicate probe id")
    unknown = sorted(set(failures) - set(ids))
    if unknown:
        raise Malformed(f"{identifier}: expected_failures names unknown probe(s): {', '.join(unknown)}")
    return scenario


def load_scenarios() -> list:
    import benchmarks.scenarios as package

    found = []
    for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        if module.name in SKIP or module.name.startswith("_"):
            continue
        try:
            loaded = importlib.import_module(f"benchmarks.scenarios.{module.name}")
        except Exception as error:
            raise Malformed(f"{module.name}: import failed: {error}") from error
        scenario = getattr(loaded, "SCENARIO", None)
        if scenario is None:
            continue
        found.append(validate_scenario(scenario, module.name))
    ids = [item.id for item in found]
    if len(set(ids)) != len(ids):
        raise Malformed("two modules publish the same scenario id")
    return found


def replay(scenario) -> dict:
    """Run one scenario and reduce its report to the recorded shape."""
    try:
        report = run_scenario(scenario, SessionHost())
    except Exception as error:
        raise Malformed(f"{scenario.id}: play raised {type(error).__name__}: {error}") from error
    return {
        "expected_failures": list(report["expected_failures"]),
        "probe_failures": list(report["probe_failures"]),
        "probes": [
            {"id": item["id"], "passed": item["passed"], "expected": item["expected"], "observed": item["observed"]}
            for item in report["probes"]
        ],
        "report": {name: report.get(name) for name in REPORT_FIELDS},
    }


def load_record(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Malformed(f"record {path}: {error}") from error
    if not isinstance(record, dict) or not isinstance(record.get("scenarios"), dict):
        raise Malformed(f"record {path}: expected an object with a 'scenarios' object")
    for name, entry in record["scenarios"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("probes"), list) \
                or not isinstance(entry.get("report"), dict) or not isinstance(entry.get("probe_failures"), list):
            raise Malformed(f"record {path}: scenario {name!r} lacks probes, probe_failures or report")
    return record


def compare(name: str, recorded: dict, observed: dict) -> None:
    """Raise Divergence at the first field whose recorded value does not hold."""
    if observed["probe_failures"] != observed["expected_failures"]:
        raise Divergence(name, "probe_failures", observed["expected_failures"], observed["probe_failures"])
    if recorded["probe_failures"] != observed["probe_failures"]:
        raise Divergence(name, "probe_failures", recorded["probe_failures"], observed["probe_failures"])
    want_ids = [item.get("id") for item in recorded["probes"]]
    have_ids = [item["id"] for item in observed["probes"]]
    if want_ids != have_ids:
        raise Divergence(name, "probes", want_ids, have_ids)
    for want, have in zip(recorded["probes"], observed["probes"]):
        prefix = f"probes[{have['id']}]"
        if want.get("passed") != have["passed"]:
            raise Divergence(name, prefix + ".passed", want.get("passed"), have["passed"])
        if want.get("expected") != have["expected"]:
            raise Divergence(name, prefix + ".expected", want.get("expected"), have["expected"])
        want_observed = want.get("observed") or {}
        for key in sorted(set(want_observed) | set(have["observed"])):
            if want_observed.get(key) != have["observed"].get(key):
                raise Divergence(name, f"{prefix}.observed.{key}", want_observed.get(key), have["observed"].get(key))
    for key in REPORT_FIELDS:
        if recorded["report"].get(key) != observed["report"].get(key):
            raise Divergence(name, "report." + key, recorded["report"].get(key), observed["report"].get(key))


def digest(results: dict) -> str:
    return hashlib.sha256(json.dumps(results, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", help="replay one scenario by id")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of lines")
    parser.add_argument("--expected", type=Path, default=EXPECTED_PATH, help="path of the record")
    parser.add_argument("--record", action="store_true", help="rewrite the record from this run (not a check)")
    args = parser.parse_args(argv)

    outcome = {"scenarios": [], "digest": None, "exit_code": OK, "divergence": None, "error": None}

    def emit() -> int:
        if args.json:
            print(json.dumps(outcome, indent=2, sort_keys=True))
            return outcome["exit_code"]
        if outcome["error"]:
            print("malformed:", outcome["error"])
        elif outcome["divergence"]:
            print("divergence:", outcome["divergence"])
        print(f"scenarios: {len(outcome['scenarios'])} replayed, exit {outcome['exit_code']}, "
              f"digest {outcome['digest']}")
        return outcome["exit_code"]

    try:
        scenarios = load_scenarios()
        if args.scenario:
            scenarios = [item for item in scenarios if item.id == args.scenario]
            if not scenarios:
                raise Malformed(f"no scenario with id {args.scenario!r}")
        observed = {scenario.id: replay(scenario) for scenario in scenarios}
        outcome["digest"] = digest(observed)
        if args.record:
            record = load_record(args.expected) if args.expected.exists() else {"scenarios": {}}
            record["host"] = SessionHost.kind
            record["scenarios"].update(observed)
            record["scenarios"] = dict(sorted(record["scenarios"].items()))
            with open(args.expected, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, indent=1, sort_keys=True) + "\n")
            for name in observed:
                outcome["scenarios"].append({"id": name, "status": "recorded"})
                if not args.json:
                    print(f"recorded  {name}")
            return emit()
        record = load_record(args.expected)
        for name, result in observed.items():
            if name not in record["scenarios"]:
                raise Malformed(f"record has no entry for scenario {name!r}; run --record after review")
            try:
                compare(name, record["scenarios"][name], result)
            except Divergence as error:
                outcome["scenarios"].append({"id": name, "status": "diverged", "field": error.field,
                                             "expected": error.expected, "observed": error.observed,
                                             "probe_failures": result["probe_failures"]})
                outcome["divergence"] = str(error)
                outcome["exit_code"] = DIVERGED
                if not args.json:
                    print(f"diverged  {name}  {error.field}")
                return emit()
            outcome["scenarios"].append({"id": name, "status": "held", "probe_failures": result["probe_failures"],
                                         "probes": len(result["probes"])})
            if not args.json:
                failures = ",".join(result["probe_failures"]) or "-"
                print(f"held      {name}  probes={len(result['probes'])}  expected_failures={failures}")
        if not args.scenario:
            missing = sorted(set(record["scenarios"]) - set(observed))
            if missing:
                raise Malformed("record names scenario(s) no module publishes: " + ", ".join(missing))
    except Malformed as error:
        outcome["error"] = str(error)
        outcome["exit_code"] = MALFORMED
    return emit()


if __name__ == "__main__":
    sys.exit(main())
