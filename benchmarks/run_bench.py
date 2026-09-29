"""Development benchmarks for a new project on syberlabs.Session.

Stdlib only. Compares the in-memory session with Work's SQLite path on the
same access-review contract. Prints microseconds. Writes a text report when
run as a script.
"""

from __future__ import annotations

import importlib.util
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from syberlabs.admission import AdmissionContext, admit, explain
from syberlabs.events import verify_events
from syberwork.core import Work


def _load_project():
    path = ROOT / "examples" / "access_review.py"
    spec = importlib.util.spec_from_file_location("access_review_bench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _samples(fn, repeats: int, warmup: int = 2) -> list[float]:
    for _ in range(warmup):
        fn()
    taken = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        taken.append((time.perf_counter() - start) * 1_000_000)
    return taken


def _row(name: str, samples: list[float], note: str = "") -> dict:
    ordered = sorted(samples)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    return {
        "name": name,
        "n": len(samples),
        "median_us": statistics.median(samples),
        "p95_us": p95,
        "note": note,
    }


def _admit_context():
    return AdmissionContext(
        contract={"actions": {"certify_scope": {}}, "resolutions": {}, "input_bindings": {}},
        policy={"actions": {"certify_scope": {"roles": ["reviewer"]}}},
        history=[],
        proposal={
            "id": "p",
            "action": "certify_scope",
            "args": {},
            "actor": "reviewer",
            "roles": ["reviewer"],
            "origin": "human",
        },
        now=1_000_000.0,
        installed_actions={"certify_scope"},
    )


def run_benchmark(*, session_cases: int = 100, work_cases: int = 40, observations: int = 500) -> list[dict]:
    project = _load_project()
    rows = []

    session = project.open_session()
    counter = {"n": 0}

    def one_session_case():
        counter["n"] += 1
        done = project.complete_review(session, f"AR-S-{counter['n']}")
        if not done["chain_valid"]:
            raise RuntimeError("session chain broke")

    rows.append(_row("session.complete_case", _samples(one_session_case, session_cases), "steady session, full review"))

    work_dir = tempfile.TemporaryDirectory()
    work = Work(Path(work_dir.name) / "bench.sqlite3")
    work.install_contract(project.CONTRACT)
    work.install_policy(project.POLICY)
    work.install_action("certify_scope", {"kind": "local", "title": "Certify"})
    work.install_action("revoke_extra", {"kind": "local", "title": "Revoke"})
    work_counter = {"n": 0}

    def one_work_case():
        work_counter["n"] += 1
        review_id = f"AR-W-{work_counter['n']}"
        case_id = work.create_case("access-review", 1, {"review_id": review_id}, "reviewer")
        work.observe(case_id, "roster", project.roster(review_id), "directory", "snap", "reviewer", verified=True)
        certified = work.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
        if certified["decision"]["status"] != "allowed":
            raise RuntimeError(certified["decision"])
        work.commit(case_id, certified["proposal"]["id"], "reviewer")
        revoke = work.compiled_propose(case_id, "reviewer", ["reviewer", "compiled"])
        work.approve(case_id, revoke["proposal"]["id"], "manager", ["manager"])
        committed = work.commit(case_id, revoke["proposal"]["id"], "reviewer")
        if committed["status"] != "succeeded":
            raise RuntimeError(committed)
        work.signoff(case_id, "auditor", ["auditor"], "auditor")
        if not work.verify_chain(case_id):
            raise RuntimeError("work chain broke")

    rows.append(_row("work.complete_case", _samples(one_work_case, work_cases), "one SQLite connection, same contract"))

    ctx = _admit_context()

    def one_admit():
        decision = admit(ctx)
        if decision["reason"] != "all_checks_passed":
            raise RuntimeError(decision)

    rows.append(_row("admit.allowed", _samples(one_admit, 2000, warmup=20), "one allowed decision, empty history"))

    def one_explain():
        found = explain(ctx)
        if found["rule"] != "admission.passed":
            raise RuntimeError(found)

    rows.append(_row("explain.allowed", _samples(one_explain, 1000, warmup=10), "admit plus cached provenance"))

    long_session = project.open_session()
    long_case = long_session.create_case("access-review", 1, {"review_id": "AR-LONG"}, "reviewer")
    for index in range(observations):
        long_session.observe(long_case, "roster", project.roster("AR-LONG", index + 1), "directory", f"v{index}", "reviewer", verified=True)

    def verify_long():
        if not long_session.verify_chain(long_case):
            raise RuntimeError("long chain broke")

    rows.append(_row("session.verify_chain", _samples(verify_long, 20, warmup=1), f"{observations} observations"))

    events = long_session.inspect(long_case)["events"]

    def verify_direct():
        if not verify_events(events):
            raise RuntimeError("direct verify failed")

    rows.append(_row("verify_events", _samples(verify_direct, 20, warmup=1), f"{len(events)} events, no session lookup"))
    work.close()
    work_dir.cleanup()
    return rows


def format_report(rows: list[dict]) -> str:
    lines = [
        "SyberLabs SDK development benchmark",
        "unit: microseconds per call, median and p95",
        "",
        f"{'operation':<24} {'n':>6} {'median_us':>12} {'p95_us':>12}  note",
    ]
    for row in rows:
        lines.append(
            f"{row['name']:<24} {row['n']:>6} {row['median_us']:>12.1f} {row['p95_us']:>12.1f}  {row['note']}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    rows = run_benchmark()
    report = format_report(rows)
    out = Path(__file__).resolve().parent / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "dev-session.txt").write_text(report)
    print(report, end="")


if __name__ == "__main__":
    main()
