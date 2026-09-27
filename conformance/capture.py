"""Capture comparable admission traces from the current executor.

Each golden file is one scenario: an outcome label plus the ordered
``(event kind, decision status, decision reason)`` sequence. UUIDs,
timestamps, hashes, and ports are omitted so two runs match.

Run from the repository root:

    PYTHONPATH=. python -m conformance.capture
    PYTHONPATH=. python -m conformance.capture --fixture conformance/fixtures
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from case_studies.enterprise_procurement import run_study
from syberwork.core import Work


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
GOLDEN = ROOT / "conformance" / "golden"


def tuples_from_events(events: list[dict]) -> list[list]:
    rows = []
    for event in events:
        if event["kind"] == "decision":
            rows.append([event["kind"], event["body"]["status"], event["body"]["reason"]])
        else:
            rows.append([event["kind"], None, None])
    return rows


def _load_examples(work: Work) -> tuple[dict, dict]:
    contract = json.loads((EXAMPLES / "contract.json").read_text())
    policy = json.loads((EXAMPLES / "policy.json").read_text())
    work.install_contract(contract)
    work.install_policy(policy)
    work.install_action("record_review", {"kind": "local"})
    work.install_action("issue_order", {"kind": "local"})
    return contract, policy


def _facts(work: Work, case: str) -> None:
    work.observe(case, "part_number", "P-104", "inventory", "inv:7", "inventory", verified=True)
    work.observe(case, "quote", {"id": "Q-7", "price": 250}, "supplier", "quote:1", "supplier", verified=True)


def _trace(scenario_id: str, outcome: str, events: list[dict] | None, *, source: str) -> dict:
    return {
        "events": tuples_from_events(events or []),
        "id": scenario_id,
        "outcome": outcome,
        "raw_events": events or [],
        "source": source,
    }


def _case_study_traces() -> list[dict]:
    traces = []
    with tempfile.TemporaryDirectory() as folder:
        report = run_study(Path(folder))
    for item in report["scenarios"]:
        traces.append(_trace(item["id"], item["outcome"], item.get("case_events") or [], source="case_study"))
    return traces


def _admission_traces() -> list[dict]:
    traces = []
    with tempfile.TemporaryDirectory() as folder:
        work = Work(Path(folder) / "work.sqlite3")
        contract, policy = _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        _facts(work, case)
        review = work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        work.commit(case, review["proposal"]["id"], "operator")
        order = work.propose(
            case, "issue_order",
            {"part_number": "P-104", "quote": {"id": "Q-7", "price": 250}, "quote_id": "Q-7",
             "amount": 250, "quote_version": "quote:1"},
            "operator", ["operator", "model"], "model",
        )
        work.approve(case, order["proposal"]["id"], "manager", ["manager"])
        work.commit(case, order["proposal"]["id"], "operator")
        work.signoff(case, "manager", ["manager"], "manager")
        state = work.inspect(case)
        traces.append(_trace("admit_complete", "complete" if state["complete"] else state["status"], state["events"], source="admission"))

        work = Work(Path(folder) / "untrusted.sqlite3")
        _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        work.observe(case, "part_number", "P-104", "user", "asserted", "operator")
        work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        work.observe(case, "part_number", "P-104", "inventory", "v1", "inventory", verified=True)
        work.propose(case, "record_review", {"part_number": "P-999"}, "operator", ["operator"])
        state = work.inspect(case)
        traces.append(_trace("admit_untrusted_then_provenance", state["events"][-1]["body"]["reason"], state["events"], source="admission"))

        bound = json.loads(json.dumps(contract))
        bound["version"] = 2
        bound["input_bindings"] = {"part_number": "fact:part_number"}
        work.install_contract(bound)
        case = work.create_case("purchase-order", 2, {"part_number": "P-104", "quantity": 2}, "operator")
        work.observe(case, "part_number", "P-999", "inventory", "inventory:9", "inventory", verified=True)
        work.propose(case, "record_review", {"part_number": "P-999"}, "operator", ["operator"])
        state = work.inspect(case)
        traces.append(_trace("admit_input_provenance", state["events"][-1]["body"]["reason"], state["events"], source="admission"))

        work = Work(Path(folder) / "policy.sqlite3")
        _load_examples(work)
        restrictive = json.loads(json.dumps(policy))
        restrictive["version"] = 2
        restrictive["actions"].pop("record_review")
        work.install_policy(restrictive)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        _facts(work, case)
        work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        state = work.inspect(case)
        traces.append(_trace("admit_global_policy", state["events"][-1]["body"]["reason"], state["events"], source="admission"))

        work = Work(Path(folder) / "unverified.sqlite3")
        _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        work.observe(case, "part_number", "P-104", "inventory", "invented", "inventory")
        work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        state = work.inspect(case)
        traces.append(_trace("admit_unverified_fact", state["events"][-1]["body"]["reason"], state["events"], source="admission"))

        work = Work(Path(folder) / "approval.sqlite3")
        _load_examples(work)
        revised = json.loads(json.dumps(policy))
        revised["version"] = 2
        revised["actions"]["record_review"]["approval_role"] = "compliance"
        work.install_policy(revised)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        _facts(work, case)
        proposed = work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        work.approve(case, proposed["proposal"]["id"], "compliance", ["compliance"])
        work.commit(case, proposed["proposal"]["id"], "operator")
        state = work.inspect(case)
        traces.append(_trace("admit_global_approval", "succeeded", state["events"], source="admission"))

        work = Work(Path(folder) / "recheck.sqlite3")
        _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        _facts(work, case)
        proposed = work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        work.observe(case, "part_number", "P-105", "inventory", "inv:8", "inventory", verified=True)
        work.commit(case, proposed["proposal"]["id"], "operator")
        state = work.inspect(case)
        traces.append(_trace("admit_commit_recheck", state["events"][-1]["body"]["reason"], state["events"], source="admission"))

        work = Work(Path(folder) / "replay.sqlite3")
        contract, _policy = _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        _facts(work, case)
        work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        revised = json.loads(json.dumps(contract))
        revised["version"] = 2
        revised["actions"].pop("record_review")
        revised["actions"]["issue_order"].pop("requires_effect")
        revised["compiled_path"].remove("record_review")
        work.install_contract(revised)
        diff = work.replay(case, 2, 1)
        state = work.inspect(case)
        traces.append(_trace("admit_replay_removed_action", diff["changed_decisions"][0]["after"]["reason"], state["events"], source="admission"))

        work = Work(Path(folder) / "cancelled.sqlite3")
        _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        work.cancel_case(case, "withdrawn", "manager", ["manager"])
        work.propose(case, "record_review", {}, "operator", ["operator"])
        state = work.inspect(case)
        traces.append(_trace("admit_cancelled", state["events"][-1]["body"]["reason"], state["events"], source="admission"))

        traces.append(_planner_trace(Path(folder)))
    return traces


def _planner_trace(folder: Path) -> dict:
    class Planner(BaseHTTPRequestHandler):
        def do_POST(self):
            _request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"action": "issue_order", "args": {"amount": 10}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Planner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        work = Work(folder / "planner.sqlite3")
        _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        with patch.dict(os.environ, {"SYBERWORK_PLANNER_URL": f"http://127.0.0.1:{server.server_address[1]}/suggest"}):
            work.model_propose(case, "planner", ["model", "operator"])
        state = work.inspect(case)
    finally:
        server.shutdown()
        server.server_close()
    return _trace("admit_planner_denied", state["events"][-1]["body"]["reason"], state["events"], source="admission")


def collect() -> list[dict]:
    """Case-study scenarios first, then admission paths, each sorted by id later."""
    traces = _case_study_traces() + _admission_traces()
    traces.sort(key=lambda item: item["id"])
    return traces


def golden_document(trace: dict) -> dict:
    return {"events": trace["events"], "id": trace["id"], "outcome": trace["outcome"], "source": trace["source"]}


def write_golden(directory: Path | None = None) -> list[Path]:
    directory = directory or GOLDEN
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for trace in collect():
        path = directory / f"{trace['id']}.json"
        path.write_text(json.dumps(golden_document(trace), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def write_fixture(directory: Path) -> None:
    """SQLite history produced by this process, for later verify/replay."""
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "work.sqlite3"
        work = Work(source)
        _load_examples(work)
        case = work.create_case("purchase-order", 1, {"part_number": "P-104", "quantity": 2}, "operator")
        _facts(work, case)
        review = work.propose(case, "record_review", {"part_number": "P-104"}, "operator", ["operator"])
        work.commit(case, review["proposal"]["id"], "operator")
        order = work.propose(
            case, "issue_order",
            {"part_number": "P-104", "quote": {"id": "Q-7", "price": 250}, "quote_id": "Q-7",
             "amount": 250, "quote_version": "quote:1"},
            "operator", ["operator"],
        )
        work.approve(case, order["proposal"]["id"], "manager", ["manager"])
        work.commit(case, order["proposal"]["id"], "operator")
        work.signoff(case, "manager", ["manager"], "manager")
        state = work.inspect(case)
        destination = directory / "main_history.sqlite"
        if destination.exists():
            destination.unlink()
        with sqlite3.connect(source) as origin, sqlite3.connect(destination) as copy:
            origin.backup(copy)
        meta = {
            "case_id": case,
            "chain_valid": work.verify_chain(case),
            "complete": state["complete"],
            "contract_id": "purchase-order",
            "contract_version": 1,
            "decision_events": [
                [event["kind"], event["body"]["status"], event["body"]["reason"]]
                for event in state["events"] if event["kind"] == "decision"
            ],
            "event_kinds": [event["kind"] for event in state["events"]],
            "policy_version": 1,
            "source_commit": "a2f909bc4c638e07c9ae14cb8dc63f7c3347c06d",
        }
        (directory / "main_history.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("--fixture", type=Path, help="Also write a SQLite history fixture")
    args = parser.parse_args()
    paths = write_golden(args.golden)
    print(f"wrote {len(paths)} golden traces to {args.golden}")
    if args.fixture:
        write_fixture(args.fixture)
        print(f"wrote fixture to {args.fixture}")


if __name__ == "__main__":
    main()
