"""Development measurements for the Build Thread on a pinned fixture.

Machine time only. It does not measure how long a person takes to read the
docs or decide; the roadmap's first-use test needs people for that.

    PYTHONPATH=. python benchmarks/build_thread_dx.py [--write]
"""

from __future__ import annotations

import inspect
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import syberlabs  # noqa: E402
from examples.build_thread import EXPORT, TEST_EXPORT, make_repository  # noqa: E402
from syberlabs.build import Kit, Thread  # noqa: E402
from syberlabs.providers import FunctionProvider  # noqa: E402


def timed(fn):
    started = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - started) * 1000


def quickstart(folder: Path) -> dict:
    root = folder / "quickstart"
    make_repository(root)
    steps = {}
    kit, steps["init"] = timed(lambda: Kit.local(root / ".syberlabs", root, actor="dev"))
    thread, steps["start"] = timed(lambda: kit.start("Add a CSV export for report rows", paths=["src", "tests"]))
    _, steps["context"] = timed(thread.context)
    found, steps["propose"] = timed(lambda: thread.propose(changes={"src/export.py": EXPORT, "tests/test_export.py": TEST_EXPORT}))
    verdict, steps["check (runs tests)"] = timed(lambda: thread.check(found[0].id))
    evaluations = sum(1 for e in thread.history() if e["kind"] == "candidate_evaluated")
    _, steps["check again (no change)"] = timed(lambda: thread.check(found[0].id))
    repeat_ran = sum(1 for e in thread.history() if e["kind"] == "candidate_evaluated") - evaluations
    receipt, steps["accept"] = timed(lambda: thread.accept(found[0].id))
    kit.close()
    first_verdict = sum(steps[k] for k in ("init", "start", "context", "propose", "check (runs tests)"))
    return {"steps": steps, "first_verdict_ms": first_verdict, "repeat_evaluations": repeat_ran,
            "accepted": receipt.status, "acceptable": verdict.acceptable}


def scale(folder: Path, candidates: int) -> dict:
    root = folder / f"scale{candidates}"
    make_repository(root)
    kit = Kit.local(root / ".syberlabs", root, actor="dev")
    doc = kit.session.contracts[("repo-change", 1)]
    import copy
    import json
    bigger = copy.deepcopy(doc)
    bigger["version"] = 2
    bigger["evolution"]["budget"] = {"max_candidates": 1000, "max_evaluations": 1000, "max_seconds": 3600}
    (root / ".syberlabs" / "contracts" / "repo-change.v2.json").write_text(json.dumps(bigger))
    kit.close()
    kit = Kit.local(root / ".syberlabs", root, actor="dev")
    thread = kit.start("Add a CSV export", "repo-change.v2")
    payload = "x = 1\n" * 16_000  # about 96 KB per candidate file

    def many(space):
        for n in range(candidates):
            space.submit({"src/export.py": EXPORT + f"# {n}\n" + payload}, operator="patch")

    tracemalloc.start()
    _, submit_ms = timed(lambda: thread.propose(FunctionProvider(many, name="bulk", revision="1")))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    samples = []
    for _ in range(200):
        _, ms = timed(lambda: kit.session.preview(thread.id, "accept_change",
                                                  {"candidate": "c1", "commit": "0" * 40, "base": thread.base},
                                                  "dev", ["developer"]))
        samples.append(ms)
    events = len(thread.history())
    kit.close()
    reopened, reopen_ms = timed(lambda: Kit.local(root / ".syberlabs", root, actor="dev").open(thread.id))
    journal_bytes = reopened.kit.journal.size()
    reopened.kit.close()
    samples.sort()
    return {"candidates": candidates, "events": events, "submit_ms_each": submit_ms / candidates,
            "peak_python_mb": peak / 1e6, "preview_p50_ms": statistics.median(samples),
            "preview_p95_ms": samples[int(len(samples) * 0.95)], "reopen_ms": reopen_ms, "journal_kb": journal_bytes / 1024}


def surface() -> dict:
    public = [name for name in ("Kit", "Thread", "Verdict", "Receipt", "Candidate", "SearchProvider") if hasattr(syberlabs, name)]
    methods = [name for name, _ in inspect.getmembers(Thread, inspect.isfunction) if not name.startswith("_")]
    kit_methods = [name for name, _ in inspect.getmembers(Kit) if not name.startswith("_") and callable(getattr(Kit, name))]
    return {"primitives": public, "thread_methods": methods, "kit_methods": kit_methods}


def main() -> str:
    lines = [f"Build Thread development measurements. {platform.platform()}, CPython {platform.python_version()}, "
             f"{os.cpu_count()} CPUs. Journal fsync on every record. Machine time only."]
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        found = quickstart(folder)
        lines.append("")
        lines.append("Quickstart on the pinned fixture (examples/build_thread.py repository):")
        for step, ms in found["steps"].items():
            lines.append(f"  {step:24} {ms:8.1f} ms")
        lines.append(f"  first verdict (machine)  {found['first_verdict_ms']:8.1f} ms   acceptable={found['acceptable']} "
                     f"accepted={found['accepted']}")
        lines.append(f"  a repeat check of an unchanged tree ran {found['repeat_evaluations']} evaluations")
        lines.append("")
        lines.append("Scale: one search submitting N candidates, each carrying a ~96 KB file:")
        lines.append("  N     events  submit/cand  peak Python  preview p50/p95   reopen   journal")
        for n in (20, 100):
            row = scale(folder, n)
            lines.append(f"  {row['candidates']:<5} {row['events']:6}  {row['submit_ms_each']:8.1f} ms  {row['peak_python_mb']:7.2f} MB"
                         f"  {row['preview_p50_ms']:5.2f}/{row['preview_p95_ms']:5.2f} ms  {row['reopen_ms']:6.1f} ms"
                         f"  {row['journal_kb']:6.1f} KB")
    api = surface()
    lines.append("")
    lines.append(f"Public Build Thread primitives: {', '.join(api['primitives'])} ({len(api['primitives'])})")
    lines.append(f"Thread methods ({len(api['thread_methods'])}): {', '.join(api['thread_methods'])}")
    lines.append(f"Kit methods ({len(api['kit_methods'])}): {', '.join(api['kit_methods'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    report = main()
    print(report)
    if "--write" in sys.argv:
        out = ROOT / "benchmarks" / "results" / "build-thread-dev.txt"
        out.write_text(report + "\n")
        print(f"wrote {out}")
