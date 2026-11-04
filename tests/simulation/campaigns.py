"""Three campaigns. Together they name every production module once.

``cell`` speaks to a real SyberWork HTTP server and its worker.
``kernel`` drives the SDK through Git, a journal, a witness log, and the
same loopback origin. ``libraries`` calls the remaining modules and checks
what they return. A name that is not a module, or a module named twice,
fails ``tests.test_simulation``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from tests.simulation.catalog import ROOT
from tests.simulation.cell import Cell
from tests.simulation.peer import Origin


CAMPAIGNS = {
    "cell": (
        "syberwork",
        "syberwork.server",
        "syberwork.core",
        "syberwork.storage",
        "syberwork.worker",
        "syberwork.backup",
        "syberwork.trace",
        "syberwork.cli",
        "syberwork.smoke",
        "syberwork.certificates",
        "syberwork.builder_http",
        "syberwork.coordination",
        "syberwork.builder_rows",
        "syberlabs.admission",
        "syberlabs.bindings",
        "syberlabs.canonical",
        "syberlabs.clock",
        "syberlabs.contracts",
        "syberlabs.errors",
        "syberlabs.events",
        "syberlabs.evidence",
        "syberlabs.evolution",
        "syberlabs.jcs",
        "syberlabs.metrics",
        "syberlabs.planner",
        "syberlabs.targets",
        "syberlabs.values",
    ),
    "kernel": (
        "syberlabs",
        "syberlabs.session",
        "syberlabs.journal",
        "syberlabs.witness",
        "syberlabs.tlog",
        "syberlabs.dsse",
        "syberlabs.ed25519",
        "syberlabs.interop",
        "syberlabs.economic",
        "syberlabs.checks",
        "syberlabs.providers",
        "syberlabs.gitspace",
        "syberlabs.retrieval",
        "syberlabs.exchange",
        "syberlabs.build",
        "syberlabs.githits",
        "syberlabs.githits_pace",
        "syberlabs.githits_cli_entry",
        "syberlabs.query_guard",
        "syberlabs.publish",
        "syberlabs.outcome",
    ),
    "libraries": (
        "syberlabs.mappings",
        "syberlabs.protocol",
        "syberlabs.attest_reconcile_map",
        "syberlabs.retention",
        "syberlabs.refusals",
        "syberlabs.search",
        "syberlabs.evolve",
        "syberlabs.settle",
        "syberlabs.inspector",
        "syberlabs.critic",
        "syberlabs.dashboard",
        "syberlabs.builder",
        "syberlabs.certificate",
        "syberlabs.claude_code",
        "syberlabs.cli",
        "syberlabs.experiment",
        "syberlabs.eval",
        "syberlabs.eval.gate",
        "syberlabs.eval.report",
        "syberlabs.eval.runner",
        "syberlabs.eval.score",
        "syberlabs.eval.spec",
        "syberlabs.eval.tasks",
        "syberlabs.eval.arms",
        "syberlabs.eval.cli",
    ),
}


def _expect(cell: Cell, method: str, path: str, token: str | None, data, status: int):
    code, body = cell.call(method, path, token, data)
    if code != status:
        raise AssertionError(f"{method} {path} returned {code} {body}, wanted {status}")
    return body


def _purchase(origin: Origin) -> tuple[dict, dict, dict, dict]:
    base = origin.base
    contract = {
        "id": "purchase-order",
        "version": 1,
        "title": "Source-backed purchase order",
        "inputs": {"part_number": "string", "quantity": "integer"},
        "actions": {
            "record_review": {
                "required_facts": [{"key": "part_number", "source": "inventory", "verified": True, "max_age_seconds": 86400}],
                "arguments": {"part_number": "fact:part_number"},
            },
            "issue_order": {
                "required_facts": [
                    {"key": "part_number", "source": "inventory", "verified": True, "max_age_seconds": 86400},
                    {"key": "quote", "source": "supplier", "verified": True, "max_age_seconds": 3600},
                ],
                "arguments": {
                    "part_number": "fact:part_number",
                    "quote": "fact:quote",
                    "quote_id": "fact:quote.id",
                    "amount": "fact:quote.price",
                    "quote_version": "version:quote",
                },
                "requires_effect": "record_review",
                "approval_role": "manager",
                "max_amount": 10000,
            },
        },
        "acceptance": [
            {"id": "order_sent", "kind": "effect", "action": "issue_order"},
            {"id": "manager_signed", "kind": "signoff", "role": "manager", "after_action": "issue_order"},
        ],
        "compiled_path": ["record_review", "issue_order"],
    }
    policy = {
        "version": 1,
        "actions": {
            "record_review": {"roles": ["operator", "manager"]},
            "issue_order": {"roles": ["operator"], "approval_role": "manager", "max_amount": 25000},
        },
    }
    sources = {
        "inventory": {"kind": "http", "url": base + "/inventory/{key}", "value_path": ["part_number"], "roles": ["operator"]},
        "supplier": {"kind": "http", "url": base + "/quotes/{key}", "value_path": ["quote"], "roles": ["operator"]},
    }
    actions = {
        "record_review": {"kind": "local", "title": "Record review"},
        "issue_order": {
            "kind": "http",
            "title": "Issue purchase order",
            "method": "POST",
            "url": base + "/orders",
            "status_url": base + "/orders/by-key/{key}",
            "no_write_statuses": [412],
            "version_arg": "quote_version",
            "timeout_seconds": 10,
        },
    }
    return contract, policy, sources, actions


def run_cell() -> None:
    """One cell, one origin: purchase, planner, lost write, backup, smoke."""
    from syberwork.backup import export_cell, restore_cell
    from syberwork.core import Work
    from syberwork.worker import run_once
    import syberwork.smoke as smoke

    home = tempfile.TemporaryDirectory(prefix="syber-sim-cell-")
    origin = Origin()
    cell = Cell(Path(home.name))
    try:
        contract, policy, sources, actions = _purchase(origin)
        admin, operator, scheduler, manager, planner = (
            cell.tokens["admin"], cell.tokens["operator"], cell.tokens["scheduler"],
            cell.tokens["manager"], cell.tokens["planner"],
        )
        _expect(cell, "GET", "/api/cases", None, None, 401)
        _expect(cell, "POST", "/api/contracts", admin, contract, 200)
        _expect(cell, "POST", "/api/policies", admin, policy, 200)
        for name, document in sources.items():
            _expect(cell, "POST", f"/api/sources/{name}", admin, document, 200)
        for name, document in actions.items():
            _expect(cell, "POST", f"/api/actions/{name}", admin, document, 200)

        created = _expect(cell, "POST", "/api/cases", operator, {
            "contract_id": "purchase-order", "version": 1,
            "inputs": {"part_number": "P-104", "quantity": 2},
        }, 200)
        case_id = created["id"]
        inventory = _expect(cell, "POST", f"/api/cases/{case_id}/refresh", operator, {
            "source": "inventory", "key": "part_number", "record_key": "P-104",
        }, 200)
        quote = _expect(cell, "POST", f"/api/cases/{case_id}/refresh", operator, {
            "source": "supplier", "key": "quote", "record_key": "Q-7",
        }, 200)
        if inventory["body"]["version"] != "inv:7" or quote["body"]["version"] != "quote:1":
            raise AssertionError(f"source versions {inventory['body']['version']!r} {quote['body']['version']!r}")

        review = _expect(cell, "POST", f"/api/cases/{case_id}/compiled", scheduler, {}, 200)
        if review["decision"]["status"] != "allowed" or review["proposal"]["args"] != {"part_number": "P-104"}:
            raise AssertionError(f"compiled review {review}")
        queued = _expect(cell, "POST", f"/api/cases/{case_id}/commit", scheduler, {
            "proposal_id": review["proposal"]["id"],
        }, 200)
        if queued["status"] != "queued":
            raise AssertionError(queued)
        settled = run_once(cell.work)
        if settled is None or settled["status"] != "succeeded":
            raise AssertionError(settled)

        order = _expect(cell, "POST", f"/api/cases/{case_id}/compiled", scheduler, {}, 200)
        args = order["proposal"]["args"]
        if order["decision"]["status"] != "needs_approval" or args.get("amount") != 250 or args.get("quote_version") != "quote:1":
            raise AssertionError(f"compiled order {order}")
        _expect(cell, "POST", f"/api/cases/{case_id}/approve", operator, {
            "proposal_id": order["proposal"]["id"],
        }, 409)
        _expect(cell, "POST", f"/api/cases/{case_id}/approve", manager, {
            "proposal_id": order["proposal"]["id"],
        }, 200)
        queued = _expect(cell, "POST", f"/api/cases/{case_id}/commit", scheduler, {
            "proposal_id": order["proposal"]["id"],
        }, 200)
        if queued["status"] != "queued":
            raise AssertionError(queued)
        settled = run_once(cell.work)
        if settled is None or settled["status"] != "succeeded":
            raise AssertionError(settled)
        if order["proposal"]["id"] not in origin.orders:
            raise AssertionError("the destination has no order for the committed proposal")

        state = _expect(cell, "GET", f"/api/cases/{case_id}", operator, None, 200)
        if state["complete"]:
            raise AssertionError("the purchase completed before the manager signed")
        _expect(cell, "POST", f"/api/cases/{case_id}/signoff", manager, {"role": "manager"}, 200)
        state = _expect(cell, "GET", f"/api/cases/{case_id}", operator, None, 200)
        if not state["complete"]:
            raise AssertionError("manager signoff did not complete the purchase")
        verified = _expect(cell, "GET", f"/api/cases/{case_id}/verify", operator, None, 200)
        if verified["valid"] is not True:
            raise AssertionError(verified)
        replay = _expect(cell, "POST", f"/api/cases/{case_id}/replay", operator, {
            "contract_version": 1, "policy_version": 1,
        }, 200)
        if replay["changed_decisions"]:
            raise AssertionError(replay["changed_decisions"])
        _expect(cell, "GET", "/metrics/queue_depth", operator, None, 200)
        depth = cell.work.metric("queue_depth")
        if depth != 0:
            raise AssertionError(f"queue_depth {depth}")
        _expect(cell, "GET", "/metrics/work_items", operator, None, 200)
        if "admission" not in (cell.home / "traces.jsonl").read_text(encoding="utf-8"):
            raise AssertionError("the trace log has no admission line")

        os.environ["SYBERWORK_PLANNER_URL"] = origin.base + "/suggest"
        try:
            bare = _expect(cell, "POST", "/api/cases", operator, {
                "contract_id": "purchase-order", "version": 1,
                "inputs": {"part_number": "P-104", "quantity": 1},
            }, 200)
            suggested = _expect(cell, "POST", f"/api/cases/{bare['id']}/suggest", planner, {}, 200)
        finally:
            os.environ.pop("SYBERWORK_PLANNER_URL", None)
        if suggested["decision"]["status"] != "denied" or "missing_fact" not in suggested["decision"]["reason"]:
            raise AssertionError(suggested)
        if origin.planner_hits < 1:
            raise AssertionError("the planner was not called")
        suggested_case = _expect(cell, "GET", f"/api/cases/{bare['id']}", operator, None, 200)
        if any(event["kind"] == "effect_started" for event in suggested_case["events"]):
            raise AssertionError("a denied suggestion started an effect")

        _expect(cell, "POST", "/api/sources/bounce", admin, {
            "kind": "http", "url": origin.base + "/redirect/{key}", "roles": ["operator"],
        }, 200)
        bounced = cell.call("POST", f"/api/cases/{case_id}/refresh", operator, {
            "source": "bounce", "key": "part_number", "record_key": "P-104",
        })
        if bounced[0] < 400 or origin.secret_hits != 0 or origin.redirect_hits < 1:
            raise AssertionError(f"redirect follow {bounced} secret={origin.secret_hits} redirects={origin.redirect_hits}")
        _expect(cell, "POST", "/api/actions/userinfo", admin, {
            "kind": "http", "method": "POST", "url": "https://user:sekrit@payments.example/charge",
        }, 409)
        _expect(cell, "POST", "/api/actions/metadata", admin, {
            "kind": "http", "method": "POST", "url": "https://169.254.169.254/latest",
        }, 409)

        certificates = _expect(cell, "GET", "/api/certificates", operator, None, 200)
        if certificates.get("configured") is not False:
            raise AssertionError(certificates)
        builder = cell.call("GET", "/api/builder/work/no-such", operator)
        if builder[0] == 200:
            raise AssertionError(builder)

        ping_policy = {
            "version": 2,
            "actions": {
                **policy["actions"],
                "ping": {"roles": ["operator"]},
            },
        }
        _expect(cell, "POST", "/api/policies", admin, ping_policy, 200)
        _expect(cell, "POST", "/api/contracts", admin, {
            "id": "ping", "version": 1, "inputs": {"name": "string"},
            "actions": {"ping": {}},
            "acceptance": [{"id": "sent", "kind": "effect", "action": "ping"}],
        }, 200)
        _expect(cell, "POST", "/api/actions/ping", admin, {
            "kind": "http", "method": "POST", "url": origin.base + "/drop",
            "status_url": origin.base + "/drop/by-key/{key}", "timeout_seconds": 10,
        }, 200)
        ping = _expect(cell, "POST", "/api/cases", operator, {
            "contract_id": "ping", "version": 1, "inputs": {"name": "lost"},
        }, 200)
        proposed = _expect(cell, "POST", f"/api/cases/{ping['id']}/proposals", operator, {
            "action": "ping", "args": {},
        }, 200)
        if proposed["decision"]["status"] != "allowed":
            raise AssertionError(proposed)
        queued = _expect(cell, "POST", f"/api/cases/{ping['id']}/commit", operator, {
            "proposal_id": proposed["proposal"]["id"],
        }, 200)
        if queued["status"] != "queued":
            raise AssertionError(queued)
        lost = run_once(cell.work)
        if lost is None or lost["status"] != "unknown":
            raise AssertionError(lost)
        reconciled = _expect(cell, "POST", f"/api/cases/{ping['id']}/reconcile", manager, {
            "proposal_id": proposed["proposal"]["id"],
        }, 200)
        if reconciled["status"] != "verified":
            raise AssertionError(reconciled)
        ping_state = _expect(cell, "GET", f"/api/cases/{ping['id']}", operator, None, 200)
        if not ping_state["complete"]:
            raise AssertionError("verified reconciliation did not complete the ping")

        snapshot = Path(home.name) / "snapshot"
        export_cell(cell.work, snapshot)
        restored_path = Path(home.name) / "restored.sqlite3"
        restored = Work(restored_path)
        try:
            restore_cell(restored, snapshot)
            if not restored.verify_chain(case_id) or not restored.verify_chain(ping["id"]):
                raise AssertionError("restored chains do not verify")
        finally:
            restored.close()

        inspected = subprocess.run(
            [sys.executable, "-m", "syberwork.cli", "--home", str(cell.home), "inspect", case_id],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, timeout=30, check=False,
        )
        if inspected.returncode != 0:
            raise AssertionError(inspected.stderr[-500:])
        if json.loads(inspected.stdout)["case"]["id"] != case_id:
            raise AssertionError("cli inspect returned another case")

        smoke_home = Path(home.name) / "smoke"
        smoke_db = smoke_home / "smoke.sqlite3"
        smoke_home.mkdir()
        saved = {name: os.environ.get(name) for name in ("SYBERWORK_DATABASE", "SYBERWORK_HOME")}
        os.environ["SYBERWORK_DATABASE"] = str(smoke_db)
        os.environ["SYBERWORK_HOME"] = str(smoke_home)
        captured = io.StringIO()
        try:
            with redirect_stdout(captured):
                smoke.queue()
            line = next(line for line in captured.getvalue().splitlines() if line.startswith("CASE "))
            smoke_case = line.split(" ", 1)[1].strip()
            smoke_work = Work(smoke_db, effects="worker")
            try:
                if run_once(smoke_work) is None:
                    raise AssertionError("smoke left nothing for the worker")
            finally:
                smoke_work.close()
            with redirect_stdout(io.StringIO()):
                smoke.verify(smoke_case)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    finally:
        cell.stop()
        origin.stop()
        home.cleanup()


def run_kernel() -> None:
    """SDK boundaries: journal, witness, settlement, Git, publish, GitHits."""
    import time

    from syberlabs.build import default_contract
    from syberlabs.economic import receipt_matches
    from syberlabs.ed25519 import public_from_seed, sign, verify
    from syberlabs.githits import Client
    from syberlabs.githits_cli_entry import main as githits_main
    from syberlabs.githits_pace import wait
    from syberlabs.query_guard import decide, judge

    home = tempfile.TemporaryDirectory(prefix="syber-sim-kernel-")
    root = Path(home.name)
    origin = Origin()
    try:
        _journal_and_witness(root)
        _economic(root, origin, receipt_matches)
        _resolution(root)
        _git(root)
        _commands(root)
        report = Client(token="sim-token", base_url=origin.base).doctor()
        if report["status"] != "ok" or report["token"] != "set" or "sim-token" in json.dumps(report):
            raise AssertionError("doctor did not report a set token against the loopback stand-in")
        held = wait(root / "pace", interval=0, clock=time.time, sleep=lambda _seconds: None)
        if held < 0:
            raise AssertionError(held)
        with redirect_stderr(io.StringIO()):
            launcher = githits_main([])
        if launcher != 2:
            raise AssertionError("the launcher accepted an empty argument list")
        if judge("which public package documents this function") != "pass":
            raise AssertionError("a short question was refused")
        secret = "ghp_" + ("a" * 30)
        if judge(secret) != "secret":
            raise AssertionError("a credential-shaped token was allowed through")
        if decide({"tool_name": "WebSearch", "tool_input": {"query": "requests library changelog"}}) != "pass":
            raise AssertionError("a public lookup was refused")
        seed = bytes(range(32))
        public = public_from_seed(seed)
        message = b"simulation"
        if not verify(public, message, sign(seed, message)):
            raise AssertionError("ed25519 round trip failed")
        contract = default_contract(root)
        if contract.get("version") != 1 or "actions" not in contract:
            raise AssertionError(contract)
        del secret
    finally:
        origin.stop()
        home.cleanup()


def _journal_and_witness(root: Path) -> None:
    from syberlabs import Session
    from syberlabs.errors import Rejected
    from syberlabs.interop import cloudevent, witness as hmac_witness
    from syberlabs.journal import Journal
    from syberlabs.tlog import TransparencyLog
    from syberlabs.witness import Witness
    from syberwork.core import Work

    note = {
        "id": "note", "version": 1, "inputs": {"name": "string"},
        "actions": {"note": {}},
        "acceptance": [{"id": "noted", "kind": "effect", "action": "note"}],
    }
    journal_home = root / "journal"
    journal = Journal(journal_home)
    session = Session(journal=journal)
    session.install_contract(note)
    session.install_policy({"version": 1, "actions": {"note": {"roles": ["operator"]}}})
    session.install_action("note", {"kind": "local"})
    case_id = session.create_case("note", 1, {"name": "a"}, "operator")
    proposed = session.propose(case_id, "note", {}, "operator", ["operator"])
    committed = session.commit(case_id, proposed["proposal"]["id"], "operator")
    if committed["status"] != "succeeded" or not session.verify_chain(case_id):
        raise AssertionError(committed)
    events = session.history(case_id)
    journal.close()
    reloaded = Session(journal=Journal(journal_home))
    try:
        if not reloaded.verify_chain(case_id) or len(reloaded.history(case_id)) != len(events):
            raise AssertionError("the reloaded journal does not match the session that wrote it")
    finally:
        reloaded._journal.close()

    envelope = cloudevent(events[-1])
    if envelope["type"] != "space.syberlabs.sdk.case." + events[-1]["kind"]:
        raise AssertionError(envelope)
    mac = hmac_witness(case_id, events, b"simulation-hmac-key")
    if not mac.get("mac"):
        raise AssertionError(mac)

    case_db = root / "cases.sqlite3"
    work = Work(case_db)
    try:
        try:
            TransparencyLog(case_db, bytes(range(32)))
        except Rejected as exc:
            if exc.code != "log_path":
                raise
        else:
            raise AssertionError("a transparency log opened the case database")
    finally:
        work.close()
    seed = bytes(range(32))
    witness = Witness(str(root / "witness.sqlite"), seed)
    try:
        signed = witness.attest(events)
        if not witness._log.verify_witnessed(signed, events):
            raise AssertionError("the witness log does not include the head it signed")
    finally:
        witness.close()
    if seed in (root / "witness.sqlite").read_bytes():
        raise AssertionError("the witness seed is stored in the log file")


def _economic(root: Path, origin: Origin, receipt_matches) -> None:
    import time

    from syberwork.core import Work

    work = Work(root / "economic.sqlite3")
    try:
        work.install_action("buy_data", {
            "kind": "economic_http",
            "url": origin.base + "/settle",
            "status_url": origin.base + "/status/{key}",
            "rail": "usdc_service", "asset": "USDC:base", "counterparty": "vendor-A",
            "operation": "purchase_capability", "no_write_statuses": [412],
        })
        work.install_contract({
            "id": "research", "version": 1, "inputs": {"subject": "string"},
            "actions": {"buy_data": {
                "required_facts": [{"key": "quote", "source": "vendor", "verified": True, "max_age_seconds": 3600}],
                "arguments": {"amount_units": "fact:quote.units"},
                "approval_role": "manager", "max_amount_units": "1000000",
            }},
            "acceptance": [{"id": "data", "kind": "effect", "action": "buy_data"}],
        })
        work.install_policy({"version": 1, "actions": {"buy_data": {
            "roles": ["operator"], "economic": {
                "budget_id": "research-q3", "budget_units": "1500000", "max_amount_units": "1000000",
                "asset": "USDC:base", "rail": "usdc_service", "counterparties": ["vendor-A"],
            },
        }}})
        case_id = work.create_case("research", 1, {"subject": "chips"}, "operator")
        observed = work.observe(case_id, "quote", {"units": "900000"}, "vendor", "q1", "vendor", verified=True)
        proposed = work.propose(case_id, "buy_data", {
            "operation": "purchase_capability", "amount_units": "900000",
            "asset": "USDC:base", "counterparty": "vendor-A",
            "purpose": "shipment evidence", "evidence": [observed["hash"]],
            "expires_at": time.time() + 600,
        }, "operator", ["operator"])
        if proposed["decision"]["status"] != "needs_approval":
            raise AssertionError(proposed)
        work.approve(case_id, proposed["proposal"]["id"], "manager", ["manager"])
        result = work.commit(case_id, proposed["proposal"]["id"], "operator")
        if result["status"] != "succeeded" or not work.inspect(case_id)["complete"]:
            raise AssertionError(result)
        record = origin.receipts[proposed["proposal"]["id"]]
        if not receipt_matches(record, proposed["proposal"]["id"], proposed["proposal"]["args"],
                               record["request_digest"], "usdc_service"):
            raise AssertionError(record)
    finally:
        work.close()


def _resolution(root: Path) -> None:
    from case_studies.enterprise_procurement import SimulatedERP, configure
    from syberwork.core import Work

    erp = SimulatedERP(root / "erp.sqlite3")
    work = Work(root / "resolution.sqlite3")
    try:
        configure(work, erp)
        case_id = work.create_case("northstar-spare-procurement", 1, {"request_id": "REQ-4813"}, "analyst")
        work.refresh_fact(case_id, "requisitions", "request", "REQ-4813", "analyst", ["operator"])
        work.refresh_fact(case_id, "site_options", "site_options", "REQ-4813", "analyst", ["operator"])
        task = work.request_resolution(case_id, "site", "analyst", ["operator"])
        pending = work.resolve_resolution(case_id, task["body"]["id"], "logistics", ["logistics"])
        if pending["status"] != "pending":
            raise AssertionError(pending)
        erp.select_site("logistics", "REQ-4813", "DC-WEST-4")
        done = work.resolve_resolution(case_id, task["body"]["id"], "l.chen", ["logistics"])
        if done["status"] != "completed" or done["choice"] != "DC-WEST-4":
            raise AssertionError(done)
        if not work.verify_chain(case_id):
            raise AssertionError("resolution broke the chain")
    finally:
        work.close()
        erp.close()


def _git(root: Path) -> None:
    from syberlabs.exchange import Exchange
    from syberlabs.gitspace import Repo
    from syberlabs.retrieval import select

    repo_path = root / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo_path), "-c", "user.name=simulation", "-c", "user.email=sim@example.test",
         "commit", "-q", "--allow-empty", "-m", "base"],
        check=True, timeout=30,
    )
    repo = Repo(repo_path)
    parent = repo.git("rev-parse", "HEAD").decode().strip()
    base = repo.commit([parent], {"anchor.txt": b"simulation anchor\n"}, "anchor")
    bare = root / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=30)
    repo.git("remote", "add", "origin", str(bare))
    Exchange(repo, "origin", "host-a", "topic").publish(base, [])
    excerpts = select(repo, base, {"paths": ["."], "exclude": []}, "simulation anchor")
    if not any(excerpt.path == "anchor.txt" and "simulation anchor" in excerpt.text for excerpt in excerpts):
        raise AssertionError([excerpt.path for excerpt in excerpts])


def _commands(root: Path) -> None:
    from syberlabs.checks import run_check
    from syberlabs import outcome, publish
    from syberlabs.providers import run_json
    from syberlabs.publish import Command

    log = root / "check.log"
    checked = run_check(
        [sys.executable, "-c", "raise SystemExit(0)"],
        root, {}, 30, 1000, log,
    )
    if checked["state"] != "passed":
        raise AssertionError(checked)
    answered = run_json(
        [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print(json.dumps({'ok': True}))"],
        {"n": 1}, timeout=10,
    )
    if answered != {"ok": True}:
        raise AssertionError(answered)

    class _Repo:
        @contextmanager
        def worktree(self, commit):
            folder = tempfile.TemporaryDirectory(prefix="syber-sim-tree-")
            try:
                yield Path(folder.name)
            finally:
                folder.cleanup()

    class _Kit:
        repo = _Repo()

    published = Command(_Kit(), {
        "argv": [sys.executable, "-c", "import json; print(json.dumps({'external_id': 'sim-1'}))"],
        "timeout_seconds": 30,
    }).apply("case", {"commit": "abc"}, "key")
    if published.get("external_id") != "sim-1":
        raise AssertionError(published)
    intervention = {
        "kind": "local", "effect": "intervention",
        "metric_url": "http://127.0.0.1:9/metrics/{key}",
        "metric_key": "queue_depth", "window_seconds": 60,
        "expectation": {"direction": "up", "min_delta": 1},
    }
    outcome.validate(intervention)
    publish.validate(intervention)


def run_libraries() -> None:
    """Every module the cell and the kernel do not already drive, with a real result."""
    import hashlib

    from syberlabs.attest_reconcile_map import classify_attest_artifact
    from syberlabs.builder import normalize_descriptor, project, structural_distance
    from syberlabs.certificate import check_payload
    from syberlabs.claude_code import instruction_file, third_party
    from syberlabs.critic import Invalid, is_critic, parse
    from syberlabs.dashboard import render_html
    from syberlabs.errors import Rejected
    from syberlabs import eval as eval_package
    from syberlabs.eval.arms import arm_digest
    from syberlabs.eval.cli import add_parser
    from syberlabs.eval.gate import Evidence, effect_without_admission
    from syberlabs.eval.report import sign_test as report_sign_test
    from syberlabs.eval.runner import run_id
    from syberlabs.eval.score import sha256
    from syberlabs.eval.spec import loads
    from syberlabs.eval.tasks import set_digest
    from syberlabs.evolve import resolve_conflicts
    from syberlabs.experiment import sign_test
    from syberlabs.inspector import make_inspector
    from syberlabs.mappings import translate_stabilize
    from syberlabs.protocol import AdmissionDecision
    from syberlabs.refusals import when
    from syberlabs.retention import check, decide
    from syberlabs.search import CheckResult, Evaluation
    from syberlabs.settle import describe

    mapped = translate_stabilize({"event": "chain_mismatch"})
    if mapped != {"mapped": False, "reason": "integrity_failure"}:
        raise AssertionError(mapped)
    if set(AdmissionDecision.__annotations__) != {"status", "reason"}:
        raise AssertionError(AdmissionDecision.__annotations__)
    classified = classify_attest_artifact("unknown")
    if not classified["refuse"] or classified["verified_reconciliation"]:
        raise AssertionError(classified)
    rules = check({})
    forgotten = decide([], 0.0, rules, 0.0)
    if not forgotten["allowed"] or forgotten["reason"] != "no_effects":
        raise AssertionError(forgotten)
    if when(None) != "":
        raise AssertionError(when(None))
    evaluation = Evaluation("candidate", (CheckResult("unit", "passed", 0, 1),), ("unit",))
    if not evaluation.passed:
        raise AssertionError(evaluation)
    conflict = "<<<<<<< ours\nkeep\n=======\ndrop\n>>>>>>> theirs\n"
    if resolve_conflicts(conflict, random.Random(0), 1.0) != "keep\n":
        raise AssertionError("conflict resolution dropped the kept side")

    os.environ["SIM_TOKEN"] = "super-secret-value"
    try:
        class _Actions:
            actions = {"publish": {"token_env": "SIM_TOKEN"}}

        class _Session:
            session = _Actions()

        text = describe(RuntimeError("saw super-secret-value"), _Session())
    finally:
        os.environ.pop("SIM_TOKEN", None)
    if "super-secret-value" in text or "***" not in text:
        raise AssertionError("settle.describe left a token in the error line")

    server = make_inspector(object(), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/x")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        if code != 401:
            raise AssertionError(f"inspector returned {code} without a bearer")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)

    if not is_critic("critic"):
        raise AssertionError("critic is not recognized")
    try:
        parse(b"not json")
    except Invalid:
        pass
    else:
        raise AssertionError("parse accepted a non-document")

    page = render_html({
        "threads": [],
        "journal": "<script>alert(1)</script>",
        "generated_at": 0,
        "summary": {
            "threads": {"total": 0},
            "candidates": {"proposed": 0, "accepted": 0, "refused": 0},
            "refusals": 0,
            "effects": {"claimed": 0, "applied": 0, "not_applied": 0, "pending": 0},
        },
        "certificates": [],
        "effects": [],
        "refusals": [],
    })
    if "<script>alert" in page or "&lt;script&gt;" not in page:
        raise AssertionError("the operator page did not escape the journal path")

    descriptor = normalize_descriptor({
        "intent": "record a review",
        "interaction_model": "form",
        "architecture": "cell",
        "state_model": "case",
        "data_model": "events",
        "primary_abstraction": "proposal",
        "distinguishing_claims": ["one chain"],
        "structural_traits": ["hash"],
    })
    if structural_distance(descriptor, descriptor) != 0.0:
        raise AssertionError("a descriptor is not identical to itself")
    projected = project({"hypothesis_summary": "hidden reasoning"}, {"roles": ["intended_user"]})
    if "hypothesis_summary" in projected:
        raise AssertionError(projected)
    try:
        check_payload({})
    except Rejected as exc:
        if exc.code != "payload_shape":
            raise
    else:
        raise AssertionError("an empty certificate payload was accepted")
    if not instruction_file(".claude/settings.json") or third_party({}) != []:
        raise AssertionError("claude instruction classification changed")

    helped = subprocess.run(
        [sys.executable, "-m", "syberlabs.cli", "--help"],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=30, check=False,
    )
    if helped.returncode != 0 or "usage" not in helped.stdout.lower():
        raise AssertionError(helped.stderr[-400:])
    if sign_test(0, 0) is not None:
        raise AssertionError("a tied sign test produced a probability")
    if eval_package.VERSION != "2":
        raise AssertionError(eval_package.VERSION)
    with tempfile.TemporaryDirectory() as folder:
        verdict = effect_without_admission(Evidence(bundle={}, folder=Path(folder), events=[], contract={}))
    if verdict["verdict"] != "pass":
        raise AssertionError(verdict)
    if report_sign_test([])["n"] != 0:
        raise AssertionError(report_sign_test([]))
    if run_id("task", "arm", 1) != "task.arm.s1":
        raise AssertionError(run_id("task", "arm", 1))
    # A file in a temporary folder, not NamedTemporaryFile: Windows refuses a second open of a file still held open.
    with tempfile.TemporaryDirectory() as folder:
        sample = Path(folder) / "sample.bin"
        sample.write_bytes(b"simulation")
        digest = sha256(sample)
    if digest != hashlib.sha256(b"simulation").hexdigest():
        raise AssertionError(digest)
    if loads('{"ok": true}') != {"ok": True}:
        raise AssertionError("strict JSON loads changed")
    if len(set_digest({})) != 64 or len(arm_digest({"kind": "single"})) != 64:
        raise AssertionError("eval digests are not 64 hex characters")
    parser = argparse.ArgumentParser(prog="syberlabs")
    add_parser(parser.add_subparsers(dest="command"))
