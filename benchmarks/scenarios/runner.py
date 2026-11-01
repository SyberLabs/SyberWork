"""Run a scenario on one host and collect probes."""

from __future__ import annotations

from benchmarks.scenarios.probe import evaluate


def run_scenario(scenario, host) -> dict:
    world = scenario.world()
    try:
        host.bind_world(world)
        host.install(scenario.contract(), scenario.policy())
        scenario.play(host, world)
        report = host.report()
    finally:
        close = getattr(host, "close", None)
        if close is not None:
            close()
    report["scenario"] = scenario.id
    report["probes"] = [evaluate(probe, report) for probe in scenario.probes()]
    report["probe_failures"] = [item["id"] for item in report["probes"] if not item["passed"]]
    report["expected_failures"] = list(getattr(scenario, "expected_failures", []))
    return report

