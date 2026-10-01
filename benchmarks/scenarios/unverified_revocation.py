"""A reviewer types a roster instead of reading it from the directory."""

from __future__ import annotations

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.world import Ledger


class UnverifiedRevocation:
    id = "unverified_revocation"
    expected_failures: list[str] = []

    def world(self):
        return Ledger()

    def contract(self):
        return {
            "id": "access-review",
            "version": 1,
            "inputs": {"review_id": "string"},
            "actions": {
                "revoke_extra": {
                    "required_facts": [{"key": "roster", "source": "directory", "verified": True, "max_age_seconds": 86400}],
                    "arguments": {"review_id": "fact:roster.review_id"},
                },
            },
            "acceptance": [{"id": "revoked", "kind": "effect", "action": "revoke_extra"}],
        }

    def policy(self):
        return {"version": 1, "actions": {"revoke_extra": {"roles": ["reviewer"]}}}

    def play(self, host, world):
        host.create_case("access-review", {"review_id": "AR-19"}, "reviewer")
        host.observe("roster", {"review_id": "AR-19", "account_count": 12}, "directory", "typed", "reviewer", verified=False)
        host.propose("revoke_extra", {"review_id": "AR-19"}, "reviewer", ["reviewer"])
        host.explain_last("source_verification_required:roster")
        host.finish()

    def probes(self):
        return [
            Probe("unverified_roster_refused", reason="source_verification_required:roster"),
            Probe("no_debit", payments=0, spent=0),
            Probe("chain_valid", chain_valid=True),
        ]


SCENARIO = UnverifiedRevocation()
