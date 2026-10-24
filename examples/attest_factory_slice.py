"""Offline fixture for admitting, observing Attest, approving, and reconciling."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.attest_observe import (
    ATTEST_RECEIPT_SCHEMA,
    receipt_file_to_observation_payload,
    receipt_json_to_observation_payload,
    record_to_observation_payload,
)
from syberlabs import Session
from syberlabs.attest_bridge import AuthorityDecision, HandoffPackage

CONTRACT = {
    "id": "attest-factory-slice",
    "version": 1,
    "title": "Offline Attest-to-SyberWork factory slice",
    "inputs": {"change_id": "string"},
    "actions": {"promote_change": {"approval_role": "manager"}},
    "acceptance": [{"id": "change_promoted", "kind": "effect", "action": "promote_change"}],
}
POLICY = {
    "version": 1,
    "actions": {"promote_change": {"roles": ["operator"], "approval_role": "manager"}},
}


class OfflineDestination:
    """Fictional destination that stores a record and loses its write acknowledgement."""

    def __init__(self):
        self.records = {}

    def apply(self, case_id, args, key):
        self.records[key] = {"external_id": "fixture-change-42", "args": dict(args)}
        raise ConnectionError("offline fixture: response lost after simulated write")

    def status(self, case_id, args, key):
        record = self.records.get(key)
        if record:
            return "applied", {"external_id": record["external_id"]}
        return "not_applied", {}


def run(receipt_path: Path | None = None) -> dict:
    session = Session()
    session.install_contract(CONTRACT)
    session.install_policy(POLICY)
    session.install_action("promote_change", {"kind": "local", "effect": "fictional_destination"})
    destination = OfflineDestination()
    session.bind_effect("promote_change", destination)
    case_id = session.create_case("attest-factory-slice", 1, {"change_id": "change-42"}, "operator")

    # Pass --receipt to import a file from an Attest run. Without it, use a
    # fictional receipt that keeps this example runnable offline.
    if receipt_path is None:
        receipt = receipt_json_to_observation_payload(
            {
                "schema": ATTEST_RECEIPT_SCHEMA,
                "run_id": "fictional-run-7",
                "status": "passed",
                "claim": "The candidate is ready to promote.",
                "external_write": {"status": "succeeded", "external_id": "attest-claim-only"},
            },
            source_version=ATTEST_RECEIPT_SCHEMA,
            actor="attest-importer",
        )
    else:
        receipt = receipt_file_to_observation_payload(receipt_path, actor="attest-importer")
    authority = record_to_observation_payload(
        AuthorityDecision({"decision": "approved", "basis": "fictional fixture only"}),
        source_version="attest-fixture-v1",
        actor="attest-importer",
    )
    handoff = record_to_observation_payload(
        HandoffPackage({"owner": "release-manager", "recommendation": "review change-42"}),
        source_version="attest-fixture-v1",
        actor="attest-importer",
    )
    for observation in (receipt, authority, handoff):
        session.observe(case_id, **observation)

    # Admission and approval are SyberWork decisions; Attest's "approved" text does not approve.
    proposed = session.propose(
        case_id, "promote_change", {"change_id": "change-42"}, "operator", ["operator"]
    )
    if proposed["decision"]["reason"] != "approval_required:manager":
        raise RuntimeError(proposed["decision"])
    proposal_id = proposed["proposal"]["id"]
    session.approve(case_id, proposal_id, "manager", ["manager"])

    # The fake destination stores locally, then simulates a lost acknowledgement.
    committed = session.commit(case_id, proposal_id, "operator")
    if committed["status"] != "unknown":
        raise RuntimeError(committed)
    before = session.inspect(case_id)
    if before["acceptance"][0]["passed"]:
        raise RuntimeError("Attest observation incorrectly satisfied effect acceptance")

    checked = session.reconcile(case_id, proposal_id, "manager", ["manager"])
    after = session.inspect(case_id)
    if checked["status"] != "verified" or not after["acceptance"][0]["passed"]:
        raise RuntimeError("destination status lookup did not verify the fixture effect")
    return {
        "admission": proposed["decision"]["status"],
        "independent_approval": "recorded",
        "attest_observations": len((receipt, authority, handoff)),
        "attest_receipt_source_version": receipt["version"],
        "attest_receipt_verified": receipt["verified"],
        "authority_and_handoff_verified": [authority["verified"], handoff["verified"]],
        "commit_after_lost_ack": committed["status"],
        "acceptance_before_reconcile": before["acceptance"][0]["passed"],
        "simulated_syberwork_destination_reconciliation": checked["status"],
        "acceptance_after_simulated_reconcile": after["acceptance"][0]["passed"],
        "case_chain_valid": after["chain_valid"],
        "receipt_fixture_only": receipt_path is None,
        "destination_is_simulated": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe an Attest receipt in an offline SyberWork fixture")
    parser.add_argument(
        "--receipt",
        type=Path,
        help="path to an <attest_home>/receipts/<case_id>.json file (attest.receipt/v0)",
    )
    args = parser.parse_args(argv)
    print(json.dumps(run(args.receipt), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
