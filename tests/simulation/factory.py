"""The factory campaign: Space-Time Parsing and the autonomy thin slice, driven end to end on the fixture app.

Part one goes through ``syberlabs factory`` as an operator would: index and drift against the declared
architecture, record a run, show and replay it, validate a spec, let two stand-in proposers and an independent
reviewer produce a variant, pin the owner key, approve and promote it into a ticket, record a candidate, attest the
pair under the owner's seed, verify the attestation, export the run, and build and query a feature map.

Part two compiles an approved feature map over a promoted spec into tickets and lets the loop build every feature
with the stand-in proposer inside an envelope, then walks a ticket back with ``why`` and runs ``autonomy check``.
Nothing leaves loopback; every step runs in a scratch folder.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from syberlabs import factory as factory_package
from syberlabs.ed25519 import generate
from syberlabs.factory import cli as factory_cli
from syberlabs.factory import featuremap, ledger, loop
from syberlabs.factory import policy as policymod
from syberlabs.factory import spec as specmod

from tests.simulation.catalog import ROOT

APP = ROOT / "tests" / "fixtures" / "factory_app"
SEED_ENV = "SYBERLABS_FACTORY_SIMULATION_SEED"


def _cli(cwd: Path, *argv: str) -> str:
    out, err = io.StringIO(), io.StringIO()
    here = os.getcwd()
    os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = factory_cli.main(list(argv))
    finally:
        os.chdir(here)
    if code != 0:
        raise AssertionError(f"factory {' '.join(argv)} exited {code}: {err.getvalue()[-600:]} {out.getvalue()[-300:]}")
    return out.getvalue()


def _operator_path(tree: Path) -> None:
    shutil.copytree(APP, tree)
    (tree / "fixture.json").write_text('{"part": "p-104", "quantity": 2}', encoding="utf-8")
    actor = {"kind": "human", "id": "tester"}
    (tree / "spec.json").write_text(json.dumps(specmod.new_spec("order-path", "ordering", [
        {"req_id": "R1", "text": "When quantity is positive, the system shall post one order.",
         "scenarios": [{"given": "quantity 2", "when": "run", "then": "one http.post_order effect"}]}],
        ["ingest", "rules", "effects"], author=actor)), encoding="utf-8")
    if "divergent" not in _cli(tree, "index", "--declared", "architecture.json", "--revision", "base"):
        raise AssertionError("the fixture's declared layering should diverge")
    if json.loads(_cli(tree, "drift", "--declared", "architecture.json", "--json"))["max_drift"] <= 0:
        raise AssertionError("drift reported none")
    run_a = _cli(tree, "record", "--entry", "fxapp.main:run", "--fixture", "fixture.json",
                 "--revision", "base").split("recorded run ")[1].split()[0]
    if "effect:http.post_order" not in _cli(tree, "show", run_a):
        raise AssertionError("the recorded worldline lost its effect")
    if "worldline matches the recording" not in _cli(tree, "replay", run_a, "--entry", "fxapp.main:run",
                                                     "--fixture", "fixture.json", "--revision", "base"):
        raise AssertionError("replay diverged from the recording")
    _cli(tree, "spec", "validate", "spec.json")
    proposer = lambda role: f"{sys.executable} {tree / 'proposers.py'} {role}"  # noqa: E731
    variants = _cli(tree, "variants", "spec.json", "--proposer", f"claude={proposer('tighten')}",
                    "--reviewer", f"gpt={proposer('review')}")
    verdict = variants.split("--verdict ")[1].split()[0]
    seed, public = generate()
    os.environ[SEED_ENV] = seed.hex()
    try:
        (tree / "owner.pub").write_text(public.hex(), encoding="utf-8")
        candidate = "order-path.v2.json"
        _cli(tree, "approve", candidate, "--verdict", verdict, "--actor", "human:tester",
             "--seed-env", SEED_ENV, "--out", "approval.json")
        promoted = _cli(tree, "spec", "promote", candidate, "--verdict", verdict, "--actor", "human:tester",
                        "--approval", "approval.json", "--owner-public", "owner.pub",
                        "--tickets", "tickets", "--baseline", run_a, "--owner", "tester")
        if "candidate -> promoted" not in promoted:
            raise AssertionError(promoted)
        _cli(tree, "ticket", candidate, "--req", "R1", "--baseline", run_a, "--owner", "tester", "--out", "ticket.json")
        run_b = _cli(tree, "record", "--entry", "fxapp.main:run", "--fixture", "fixture.json",
                     "--revision", "cand").split("recorded run ")[1].split()[0]
        _cli(tree, "attest", "--baseline", run_a, "--candidate", run_b, "--ticket", "ticket.json", "--commit", "cand",
             "--seed-env", SEED_ENV, "--out", "attestation.json")
    finally:
        os.environ.pop(SEED_ENV, None)
    if "verified" not in _cli(tree, "verify", "attestation.json", "--public", "owner.pub"):
        raise AssertionError("the attestation did not verify under the owner key")
    _cli(tree, "export", run_a, "--chrome", "trace.json", "--view", "run.html")
    if not (tree / "run.html").is_file():
        raise AssertionError("export wrote no viewer page")
    _cli(tree, "map", "build")
    _cli(tree, "map", "nearest", "post one order")
    _cli(tree, "map", "impact", "rules")
    status = _cli(tree, "status")
    if not ledger.Ledger(tree / ".syberlabs" / "factory").verify():
        raise AssertionError(f"the operator path left a ledger that does not verify: {status[-300:]}")


def _autonomy_path(scratch: Path) -> None:
    from tests.test_factory_autonomy import GIT_ENV, OWNER, AUTONOMY, fixture_map, hidden_catalogue, promoted_spec

    project = scratch / "project"
    shutil.copytree(APP, project)
    env = {**os.environ, **GIT_ENV}
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    home = ledger.Ledger(scratch / "ledger")
    document = promoted_spec(project, home)
    mapping = fixture_map(document, base)
    featuremap.approve(mapping, OWNER)
    envelope = loop.new_envelope(featuremap.map_digest(mapping), dollars_total=0.0, calls_total=10, seconds_total=600,
                                 attempts_per_feature=2)
    run = loop.Loop(repo_root=project, kit_home=project / ".syberlabs", ledger=home, map=mapping, spec=document,
                    envelope=envelope, policy=policymod.OfflinePolicy(),
                    proposer_argv=[sys.executable, str(AUTONOMY / "standin_proposer.py"), str(project)],
                    checks={"tests": {"argv": [sys.executable, "-c", "import fxapp.main, fxapp.variants"], "timeout_seconds": 60}},
                    hidden_catalogue=hidden_catalogue(), human_owner="tester")
    tickets = run.compile()
    summary = run.run()
    if sorted(summary["ready_for_owner_merge"]) != sorted(t["map_ref"]["feature_id"] for t in tickets):
        raise AssertionError(summary)
    if git("rev-parse", "main") != base:
        raise AssertionError("the loop moved main")
    if loop.why(home, tickets[0]["ticket_id"])["map"]["approved_by"] != OWNER:
        raise AssertionError("why lost the map's approval")
    if not home.verify():
        raise AssertionError("the autonomy ledger does not verify")
    (scratch / "map.json").write_text(json.dumps(mapping), encoding="utf-8")
    (scratch / "spec.json").write_text(json.dumps(document), encoding="utf-8")
    _cli(scratch, "autonomy", "check", "--map", "map.json", "--spec", "spec.json", "--json")


def run_factory() -> None:
    if not factory_package.__doc__:
        raise AssertionError("the factory package lost its docstring")
    with tempfile.TemporaryDirectory() as folder:
        scratch = Path(folder)
        _operator_path(scratch / "tree")
        _autonomy_path(scratch / "autonomy")
