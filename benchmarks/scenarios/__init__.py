"""Programmable scenarios for the SDK.

A scenario installs a contract, plays the public host API against a world, and
returns probes. A failed probe is a finding. It is not a score, and it is not
written into a case.
"""

from benchmarks.scenarios.probe import Probe
from benchmarks.scenarios.runner import load_scenarios, run_scenario

__all__ = ["Probe", "load_scenarios", "run_scenario"]
