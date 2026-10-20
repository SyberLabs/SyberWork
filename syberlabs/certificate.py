"""Admission certificate v0: what the journal said about one accepted candidate, under a signature.

    syberlabs certify c1                         # reads the journal, signs, writes <out> and <out>.pub
    syberlabs verify CERT --public KEY --repo .  # signature and shape; commits, trees and the diff from Git;
                                                 # the certified journal prefix when it is present

The host that ran a Build Thread states, under a DSSE signature a verifier can check with only
the public key, which exact tree it accepted, under which contract and policy, with which
host-run check results, by whose proposal and approvals, at which point of which hash chain.

It is not an event: nothing is appended to the thread's chain, and the admission rules, the
event kinds and the golden traces are untouched. It is not a provider claim: no ``signal`` and
no ``output_tail`` enter the payload, only the host's own ``candidate_evaluated`` record. It is
not proof that ``main`` took the change: it proves what the journal said at certification about
the thread's target branch. And in v0 it is not witnessed: the certifying process holds the
seed, so a certificate proves possession of that seed plus read access to the journal, not an
independent party. ``issuer.signing`` says ``in-process`` for that reason.

One pure function, ``claims``, derives every journal-sourced field. ``certify`` runs it on the
live history and signs; ``verify_certificate`` runs it on the certified prefix of the journal
and compares leaf by leaf, so there is no second derivation to drift. The verifier never runs
a check, a provider or a model, and never writes to the repository or the journal: the journal
layer reads ``registry.jsonl`` and the thread file with a read-only loader of its own, takes no
lock, and leaves a torn final line in place where the session's reader would truncate it.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from syberlabs.canonical import canonical
from syberlabs.clock import stamp
from syberlabs.contracts import prepare_contract
from syberlabs.dsse import sign_dsse, verified_payload
from syberlabs.ed25519 import public_from_seed
from syberlabs.errors import Rejected
from syberlabs.events import event_digest, verify_events
from syberlabs.evidence import verified_reconciliation
from syberlabs.evolution import candidate_views, latest_evaluation, registered, settings
from syberlabs.gitspace import Repo
from syberlabs.jcs import JcsError, envelope_jcs, jcs_bytes
from syberlabs.journal import CASE_ID

PAYLOAD_TYPE = "application/vnd.syberlabs.admission-certificate+json"
CERTIFICATE_TYPE = PAYLOAD_TYPE
PROTOCOL = "sdk.syberlabs.space/certificate/v0alpha1"
VERSION = 0
ISSUER = "syberlabs.certificate/0"
HOST = "syberlabs.host"                      # the same string as build.HOST; a test asserts it
SIGNING = "in-process"
DEFAULT_SEED_ENV = "SYBERLABS_CERTIFICATE_SEED"
MAX_CERTIFICATE_BYTES = 1_048_576
ACT_CLASSES = ("R", "C", "P", "X")            # the same as build.ACT_CLASSES; a test asserts it
CHECK_STATES = ("passed", "failed", "timed_out", "error")
# The fields ``claims`` derives from the journal, in the order ``verify_certificate`` compares them.
JOURNAL_FIELDS = ("thread", "objective", "act_class", "contract", "policy_version", "base",
                  "result", "provider", "evaluation", "admission", "chain")
LAYERS = ("signature", "repository", "journal")

_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_OBJECT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REF = re.compile(r"^refs/heads/[A-Za-z0-9._/-]{1,200}$")
_INT_MAX = 2**53 - 1   # the JCS integer range; jcs_bytes refuses anything larger


# Derivation: one function for both sides.

def claims(row: dict, events: list[dict], contract: dict) -> dict:
    """Every journal-sourced field of the certificate, derived from one case history.

    Pure: no I/O and no clock. ``certify`` calls it on the live history and
    ``verify_certificate`` on the certified prefix, so both compute the same object or the
    verifier names the first leaf that differs. The evaluation it names is the newest host
    evaluation of the accepted candidate's exact commit and tree *before the acceptance claim*,
    which is what admission could see when it re-ran at commit. The decision is the last
    ``decision`` event of the promoting proposal: ``needs_approval`` when the contract required
    an approver, because a successful commit after approval appends no second decision.
    """
    config = settings(contract)
    if config is None:
        raise Rejected("not_a_thread", "the contract has no evolution section")
    view = next((v for v in candidate_views(contract, events, 0) if v["authoritative"]), None)
    if view is None:
        raise Rejected("not_accepted", "accept a candidate before certifying it")
    pid = view["promotion"]["proposal_id"]
    proposal = claim = effect = decision = None
    claim_index = 0
    approvers = []
    for index, event in enumerate(events):
        kind, body = event["kind"], event["body"]
        if kind == "proposed":
            if body.get("id") == pid:
                proposal = body
            continue
        if body.get("proposal_id") != pid:
            continue
        if kind == "effect_started" and claim is None:
            claim, claim_index = event, index
        elif kind == "decision":
            decision = body
        elif kind == "approved":
            approvers.append({"actor": body["actor"], "role": body["role"]})
        elif effect is None and (kind == "effect_succeeded" or verified_reconciliation(event)):
            effect = event
    if proposal is None or claim is None or effect is None or decision is None:
        raise Rejected("journal_inconsistent", "the accepted candidate lacks its proposal, decision, claim or effect")
    candidate = registered(events, view["id"])
    evaluation = latest_evaluation(events[:claim_index], candidate)
    if evaluation is None:
        raise Rejected("journal_inconsistent", "no evaluation before the acceptance claim")
    base = next((e["body"] for e in reversed(events) if e["kind"] == "observed" and e["body"].get("key") == "base"
                 and e["body"].get("source") == "git" and e["body"].get("verified") is True), None)
    if base is None or not isinstance(base.get("value"), dict) or base["value"].get("commit") != view["base"]:
        raise Rejected("journal_inconsistent", "the thread's base observation does not match the accepted candidate")
    output = effect["body"].get("output") if effect["kind"] == "effect_succeeded" else None
    if isinstance(output, dict) and isinstance(output.get("ref"), str):
        target_ref = output["ref"]
    else:
        # A reconciled acceptance recorded no ref; the kit asserts the one it moves (``Kit._target``).
        target_ref = config["promotion"]["target_ref"].replace("{thread}", row["id"][:8])
    required = set(config["evaluation"]["required"])
    return {
        "thread": row["id"],
        "objective": row["inputs"]["objective"],
        "act_class": row["inputs"].get("act_class"),
        "contract": {"id": row["contract_id"], "version": row["contract_version"]},
        "policy_version": claim["body"]["policy_version"],
        "base": {"commit": base["value"]["commit"], "tree": base["value"]["tree"], "branch": base["value"].get("branch")},
        "result": {
            "candidate": view["id"], "commit": view["commit"], "tree": view["tree"], "operator": view["operator"],
            "changed_paths": list(view["changed_paths"]), "diff_digest": view["diff"]["digest"],
            "diff_bytes": view["diff"]["bytes"], "files": view["diff"]["files"],
            "origin": dict(view["origin"]) if view.get("origin") else None,
        },
        "provider": {"name": view["provider"]["name"], "revision": view["provider"]["revision"]},
        "evaluation": {
            "evaluator": evaluation["body"]["evaluator"], "event_hash": evaluation["hash"], "at": evaluation["at"],
            "checks": [{"name": c["name"], "required": c["name"] in required, "state": c["state"],
                        "exit_code": c["exit_code"], "duration_ms": c["duration_ms"], "output_digest": c["output_digest"]}
                       for c in evaluation["body"]["checks"]],
        },
        "admission": {
            "proposal_id": pid, "actor": proposal["actor"], "roles": list(proposal.get("roles") or []),
            "origin": proposal["origin"], "approvers": approvers,
            "decision": {"status": decision["status"], "reason": decision["reason"]},
            "claim_hash": claim["hash"], "effect_hash": effect["hash"], "effect_kind": effect["kind"],
            "target_ref": target_ref,
        },
        "chain": {"head": events[-1]["hash"], "count": len(events)},
    }


def build_payload(row: dict, events: list[dict], contract: dict, *, seed_env: str, now: float) -> dict:
    """``claims`` plus the identity block and the issuer. ``seed_env`` is a variable name, never a value."""
    return {
        "protocol": PROTOCOL, "payload_type": PAYLOAD_TYPE, "version": VERSION,
        "issuer": {"tool": ISSUER, "host": HOST, "seed_env": seed_env, "signing": SIGNING},
        "issued_at": stamp(now),
        **claims(row, events, contract),
    }


# Shape: the strict form of spec/certificate.schema.json.

def _shape(path: str) -> Rejected:
    return Rejected("payload_shape", path)


def _at(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _object(value: Any, path: str, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        raise _shape(path or "$")
    for key in keys:
        if key not in value:
            raise _shape(_at(path, key))
    for key in value:
        if key not in keys:
            raise _shape(_at(path, key))
    return value


def _string(value: Any, path: str, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value or (pattern is not None and pattern.fullmatch(value) is None):
        raise _shape(path)
    return value


def _integer(value: Any, path: str, low: int = 0) -> int:
    if type(value) is not int or value < low or value > _INT_MAX:
        raise _shape(path)
    return value


def _const(value: Any, path: str, expected: str) -> None:
    if not isinstance(value, str) or value != expected:
        raise _shape(path)


def check_payload(payload: object) -> dict:
    """The strict shape of ``spec/certificate.schema.json``, plus three rules the schema cannot say.

    Every integer is an ``int`` that is not a ``bool`` and lies in the JCS safe range (the
    schema's ``const: 0`` would accept ``false``); a ``passed`` check has exit code 0;
    ``result.files`` equals the number of changed paths. ``protocol``, ``payload_type`` and
    ``version`` are checked for type only: their values are the verifier's
    ``certificate_version_unsupported`` step. Raises ``Rejected("payload_shape", <path>)`` with
    the first offending path and returns the payload otherwise.
    """
    top = _object(payload, "", ("protocol", "payload_type", "version", "issuer", "issued_at", "thread", "objective",
                                "act_class", "contract", "policy_version", "base", "result", "provider",
                                "evaluation", "admission", "chain"))
    _string(top["protocol"], "protocol")
    _string(top["payload_type"], "payload_type")
    _integer(top["version"], "version")
    issuer = _object(top["issuer"], "issuer", ("tool", "host", "seed_env", "signing"))
    _const(issuer["tool"], "issuer.tool", ISSUER)
    _const(issuer["host"], "issuer.host", HOST)
    _string(issuer["seed_env"], "issuer.seed_env", _ENV)
    _const(issuer["signing"], "issuer.signing", SIGNING)
    _integer(top["issued_at"], "issued_at")
    _string(top["thread"], "thread", CASE_ID)
    _string(top["objective"], "objective")
    if top["act_class"] is not None and top["act_class"] not in ACT_CLASSES:
        raise _shape("act_class")
    contract = _object(top["contract"], "contract", ("id", "version"))
    _string(contract["id"], "contract.id")
    _integer(contract["version"], "contract.version", 1)
    _integer(top["policy_version"], "policy_version", 1)
    base = _object(top["base"], "base", ("commit", "tree", "branch"))
    _string(base["commit"], "base.commit", _OBJECT)
    _string(base["tree"], "base.tree", _OBJECT)
    if base["branch"] is not None:
        _string(base["branch"], "base.branch")
    result = _object(top["result"], "result", ("candidate", "commit", "tree", "operator", "changed_paths",
                                                "diff_digest", "diff_bytes", "files", "origin"))
    _string(result["candidate"], "result.candidate", _ID)
    _string(result["commit"], "result.commit", _OBJECT)
    _string(result["tree"], "result.tree", _OBJECT)
    _string(result["operator"], "result.operator", _NAME)
    paths = result["changed_paths"]
    if not isinstance(paths, list) or len(paths) > 1000:
        raise _shape("result.changed_paths")
    for index, item in enumerate(paths):
        _string(item, f"result.changed_paths[{index}]")
    _string(result["diff_digest"], "result.diff_digest", _DIGEST)
    _integer(result["diff_bytes"], "result.diff_bytes")
    if _integer(result["files"], "result.files") != len(paths):
        raise _shape("result.files")
    if result["origin"] is not None:
        origin = _object(result["origin"], "result.origin", ("host", "candidate"))
        _string(origin["host"], "result.origin.host", _ID)
        _string(origin["candidate"], "result.origin.candidate", _ID)
    provider = _object(top["provider"], "provider", ("name", "revision"))
    _string(provider["name"], "provider.name", _NAME)
    _string(provider["revision"], "provider.revision")
    evaluation = _object(top["evaluation"], "evaluation", ("evaluator", "event_hash", "at", "checks"))
    _string(evaluation["evaluator"], "evaluation.evaluator")
    _string(evaluation["event_hash"], "evaluation.event_hash", _DIGEST)
    _integer(evaluation["at"], "evaluation.at")
    checks = evaluation["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 32:
        raise _shape("evaluation.checks")
    for index, item in enumerate(checks):
        where = f"evaluation.checks[{index}]"
        check = _object(item, where, ("name", "required", "state", "exit_code", "duration_ms", "output_digest"))
        _string(check["name"], where + ".name", _NAME)
        if type(check["required"]) is not bool:
            raise _shape(where + ".required")
        if check["state"] not in CHECK_STATES:
            raise _shape(where + ".state")
        if check["exit_code"] is not None:
            _integer(check["exit_code"], where + ".exit_code", -_INT_MAX)
        if check["state"] == "passed" and check["exit_code"] != 0:
            raise _shape(where + ".exit_code")
        _integer(check["duration_ms"], where + ".duration_ms")
        _string(check["output_digest"], where + ".output_digest", _DIGEST)
    admission = _object(top["admission"], "admission", ("proposal_id", "actor", "roles", "origin", "approvers",
                                                          "decision", "claim_hash", "effect_hash", "effect_kind",
                                                          "target_ref"))
    _string(admission["proposal_id"], "admission.proposal_id", CASE_ID)
    _string(admission["actor"], "admission.actor")
    roles = admission["roles"]
    if not isinstance(roles, list):
        raise _shape("admission.roles")
    for index, role in enumerate(roles):
        _string(role, f"admission.roles[{index}]")
    _const(admission["origin"], "admission.origin", "human")
    approvers = admission["approvers"]
    if not isinstance(approvers, list):
        raise _shape("admission.approvers")
    for index, item in enumerate(approvers):
        where = f"admission.approvers[{index}]"
        approver = _object(item, where, ("actor", "role"))
        _string(approver["actor"], where + ".actor")
        _string(approver["role"], where + ".role")
    decision = _object(admission["decision"], "admission.decision", ("status", "reason"))
    if decision["status"] not in ("allowed", "needs_approval"):
        raise _shape("admission.decision.status")
    _string(decision["reason"], "admission.decision.reason")
    _string(admission["claim_hash"], "admission.claim_hash", _DIGEST)
    _string(admission["effect_hash"], "admission.effect_hash", _DIGEST)
    if admission["effect_kind"] not in ("effect_succeeded", "reconciled"):
        raise _shape("admission.effect_kind")
    _string(admission["target_ref"], "admission.target_ref", _REF)
    chain = _object(top["chain"], "chain", ("head", "count"))
    _string(chain["head"], "chain.head", _DIGEST)
    _integer(chain["count"], "chain.count", 1)
    return top


# Keys in v0: a seed the certifying process reads from the environment, and a public key file.

def _hex32(text: str) -> bytes | None:
    return bytes.fromhex(text) if _HEX64.fullmatch(text) else None


def load_seed(env_name: str = DEFAULT_SEED_ENV) -> bytes:
    """The 32-byte signing seed named by an environment variable.

    The variable holds either 64 hex characters or the path of a file holding 32 raw bytes (or
    64 hex characters and whitespace). Neither the value nor the file's content reaches a
    ``Rejected.detail``; only the variable's name does.
    """
    if not isinstance(env_name, str) or _ENV.fullmatch(env_name) is None:
        raise Rejected("seed_env_invalid", "the seed variable must be an uppercase environment variable name")
    value = os.environ.get(env_name)
    if not value:
        raise Rejected("seed_missing", f"set ${env_name} to the path of a 32-byte seed file (or 64 hex characters)")
    seed = _hex32(value)
    if seed is None:
        try:
            data = Path(value).read_bytes()
        except (OSError, ValueError):
            raise Rejected("seed_invalid", f"${env_name} is neither 64 hex characters nor a readable seed file") from None
        seed = data if len(data) == 32 else _hex32(data.strip().decode("ascii", errors="replace"))
        if seed is None:
            raise Rejected("seed_invalid", "the seed must be 32 raw bytes or 64 hex characters")
    try:
        public_from_seed(seed)
    except ValueError:
        raise Rejected("seed_invalid", "the seed must be 32 raw bytes or 64 hex characters") from None
    return seed


def read_public(path: str | Path) -> bytes:
    """The 32-byte Ed25519 public key from a file of 64 hex characters (``<certificate>.pub``)."""
    try:
        text = Path(path).read_bytes().strip().decode("ascii", errors="replace")
    except (OSError, ValueError):
        raise Rejected("public_key_invalid", f"cannot read the public key file {path}") from None
    key = _hex32(text)
    if key is None:
        raise Rejected("public_key_invalid", f"{path} must hold 64 hex characters")
    return key


# Certify: read, sign, write. Nothing is appended.

def certify(kit, thread_id: str, candidate: str, *, seed: bytes, seed_env: str = DEFAULT_SEED_ENV,
            now: float | None = None) -> tuple[dict, dict]:
    """Sign what the journal says about ``candidate``. Returns ``(envelope, payload)``.

    Opens the thread, verifies its chain and side digests, and appends nothing: no event, no
    observation, no ref. ``seed_env`` is recorded by name; the seed itself reaches only
    ``sign_dsse``. Refuses ``unknown_candidate``, ``not_accepted`` (the candidate is not the
    thread's authoritative one, or the acceptance is unresolved), ``invalid_chain`` and
    ``seed_env_invalid``.
    """
    if not isinstance(seed_env, str) or _ENV.fullmatch(seed_env) is None:
        raise Rejected("seed_env_invalid", "the seed variable must be an uppercase environment variable name")
    thread = kit.open(thread_id)
    if not kit.session.verify_chain(thread.id):
        raise Rejected("invalid_chain", "case history does not verify")
    events = kit.session.history(thread.id)
    if registered(events, candidate) is None:
        raise Rejected("unknown_candidate", str(candidate)[:64])
    row = kit.session.cases[thread.id]
    contract = kit.session.contracts[thread.contract]
    payload = build_payload(row, events, contract, seed_env=seed_env, now=time.time() if now is None else now)
    if payload["result"]["candidate"] != candidate:
        raise Rejected("not_accepted", f"{candidate} is not the accepted candidate of this thread")
    return sign_dsse(PAYLOAD_TYPE, jcs_bytes(payload), seed), payload


def write_certificate(envelope: dict, public: bytes, out: str | Path) -> tuple[Path, Path]:
    """Write the envelope to ``out`` and the public key beside it, both as bytes with LF.

    The key file is ``out`` with its last suffix replaced by ``.pub``: ``c1.json`` gives
    ``c1.pub`` and ``c1.cert.json`` gives ``c1.cert.pub``.
    """
    cert = Path(out)
    cert.parent.mkdir(parents=True, exist_ok=True)
    pub = cert.with_suffix(".pub")
    cert.write_bytes(json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    pub.write_bytes(public.hex().encode("ascii") + b"\n")
    return cert, pub


def summary(payload: dict) -> str:
    """The one-line statement ``syberlabs certify`` prints."""
    result, contract = payload["result"], payload["contract"]
    checks = ", ".join(f"{c['name']} {c['state']}" for c in sorted(payload["evaluation"]["checks"], key=lambda c: c["name"]))
    klass = f", class {payload['act_class']}" if payload["act_class"] else ""
    return (f"certified {result['candidate']} of thread {payload['thread'][:8]}: result {result['commit'][:10]} "
            f"(tree {result['tree'][:10]}) on {contract['id']}.v{contract['version']}{klass}; checks {checks}; "
            f"chain head {payload['chain']['head'][:10]} at {payload['chain']['count']} events")


# Verify: three layers, the first failure stops the run.

@dataclass(frozen=True)
class Report:
    """What ``verify_certificate`` found, layer by layer.

    ``signature``, ``repository`` and ``journal`` are ``"ok"``, the reason code that failed
    there, or ``None`` for a layer that did not run (skipped, or after an earlier failure).
    ``notes`` holds one human line per layer in that order: what was checked, the target
    ref's current state, event counts and paths, or why the layer was skipped.
    """

    passed: bool
    reason: str | None
    signature: str
    repository: str | None
    journal: str | None
    payload: dict | None
    keyid: str | None
    notes: tuple[str, str, str]


class _Fail(Exception):
    def __init__(self, reason: str, note: str = ""):
        super().__init__(reason)
        self.reason, self.note = reason, note


def verify_certificate(envelope: object, public: bytes, *, repo: Repo | None = None,
                       home: Path | None = None) -> Report:
    """Check a certificate with only the public key, then with what the repository and journal can re-derive.

    Layer 1 always: the file size and that it is a JSON object (when ``envelope`` is bytes),
    the ``payloadType``, the signature before any parsing of the payload, the strict shape, the
    identity constants, and that the signed bytes are the JCS form of the object they parse to.
    Layer 2 with ``repo``: both commits exist, their trees match ``rev-parse ^{tree}``, and the
    changed paths, diff digest and diff size match; the target ref's current state is reported,
    never judged. Layer 3 with ``home`` (the kit's ``.syberlabs``) when the thread's journal file
    exists: the chain is not truncated, the head matches at the certified count, the prefix
    links, and ``claims`` recomputed on that prefix equals the certificate leaf by leaf. The
    first failure stops the run. Nothing is executed and nothing is written: the journal is read
    without its lock, and a torn final record is ignored and reported, never truncated.
    """
    keyid = public.hex()
    states: list[str | None] = [None, None, None]
    notes = ["", "", ""]
    payload = None
    layer = 0
    try:
        payload = _layer_signature(envelope, public)
        states[0], notes[0] = "ok", _describe(payload, keyid)
        layer = 1
        if repo is None:
            notes[1] = "(pass --repo PATH to re-derive commits, trees and the diff)"
        else:
            notes[1] = _layer_repository(repo, payload)
            states[1] = "ok"
        layer = 2
        if home is None:
            notes[2] = "(pass --repo PATH to re-derive the journal under --home)"
        else:
            path = Path(home) / "journal" / "threads" / f"{payload['thread']}.jsonl"
            if path.exists():
                notes[2] = _layer_journal(path, Path(home), payload)
                states[2] = "ok"
            else:
                notes[2] = f"(no journal for thread {payload['thread'][:8]} under {home})"
    except _Fail as failure:
        states[layer], notes[layer] = failure.reason, failure.note
        return Report(False, failure.reason, states[0] or failure.reason, states[1], states[2],
                      payload if layer > 0 else None, keyid, (notes[0], notes[1], notes[2]))
    return Report(True, None, "ok", states[1], states[2], payload, keyid, (notes[0], notes[1], notes[2]))


def _describe(payload: dict, keyid: str) -> str:
    contract = payload["contract"]
    klass = f", class {payload['act_class']}" if payload["act_class"] else ""
    return (f"ed25519 {keyid[:10]}  thread {payload['thread'][:8]} candidate {payload['result']['candidate']} "
            f"result {payload['result']['commit'][:10]} ({contract['id']}.v{contract['version']}{klass})")


def _layer_signature(envelope: object, public: bytes) -> dict:
    if isinstance(envelope, (bytes, bytearray)):
        if len(envelope) > MAX_CERTIFICATE_BYTES:
            raise _Fail("certificate_too_large", f"more than {MAX_CERTIFICATE_BYTES} bytes")
        try:
            envelope = json.loads(envelope)
        except ValueError:
            raise _Fail("certificate_unreadable", "the file is not JSON") from None
    if not isinstance(envelope, dict):
        raise _Fail("certificate_unreadable", "the file is not a JSON object")
    if envelope.get("payloadType") != PAYLOAD_TYPE:
        raise _Fail("payload_type_mismatch", f"payloadType is not {PAYLOAD_TYPE}")
    signed = verified_payload(envelope, public, PAYLOAD_TYPE)
    if signed is None:
        raise _Fail("signature_invalid", f"no signature verifies under ed25519 {public.hex()[:10]}")
    try:
        parsed = json.loads(signed)
    except ValueError:
        raise _Fail("payload_unreadable", "the signed bytes are not JSON") from None
    if not isinstance(parsed, dict):
        raise _Fail("payload_unreadable", "the signed bytes are not a JSON object")
    try:
        check_payload(parsed)
    except Rejected as exc:
        raise _Fail(f"payload_shape:{exc.detail}", "a field is missing, extra or malformed") from None
    if parsed["protocol"] != PROTOCOL or parsed["version"] != VERSION or parsed["payload_type"] != PAYLOAD_TYPE:
        raise _Fail("certificate_version_unsupported", f"this verifier reads {PROTOCOL} version {VERSION}")
    try:
        canonical = jcs_bytes(parsed)
    except JcsError:
        canonical = None
    if canonical != signed:
        raise _Fail("payload_not_canonical", "the signed bytes are not RFC 8785 JSON of the object they parse to")
    return parsed


def _layer_repository(repo: Repo, payload: dict) -> str:
    base, result, target_ref = payload["base"], payload["result"], payload["admission"]["target_ref"]
    for label, section in (("base", base), ("result", result)):
        if repo.rev(section["commit"]) is None:
            raise _Fail(f"commit_missing:{label}", f"{section['commit'][:10]} is not in this repository")
    for label, section in (("base", base), ("result", result)):
        actual = repo.tree(section["commit"])
        if actual != section["tree"]:
            raise _Fail(f"tree_mismatch:{label}", f"certified {section['tree'][:10]}, repository {actual[:10]}")
    paths = repo.changed_paths(base["commit"], result["commit"])
    if paths != result["changed_paths"]:
        raise _Fail("changed_paths_mismatch", f"certified {len(result['changed_paths'])} paths, repository {len(paths)}")
    _, size, sha = repo.diff(base["commit"], result["commit"], max_bytes=0)
    if sha != result["diff_digest"]:
        raise _Fail("diff_mismatch:digest", f"certified {result['diff_digest'][:10]}, repository {sha[:10]}")
    if size != result["diff_bytes"]:
        raise _Fail("diff_mismatch:bytes", f"certified {result['diff_bytes']}, repository {size}")
    current = repo.read_ref(target_ref)
    if current is None:
        state = "absent"
    elif current == result["commit"]:
        state = f"at {current[:10]}"
    else:
        relation = "is an ancestor" if repo.is_ancestor(result["commit"], current) else "is not an ancestor"
        state = f"moved to {current[:10]} (result {relation})"
    return (f"base {base['commit'][:10]} and result {result['commit'][:10]} present; trees match; "
            f"{len(paths)} changed paths and diff digest match; target {target_ref} {state}")


# The journal layer's loader. It mirrors ``Session._sync`` and ``Session._load`` check for check
# but opens ``registry.jsonl`` and the thread file ``rb`` only, takes no lock, creates nothing,
# and leaves a torn final line (a writer that died mid-record) in place where ``Journal.read_new``
# would truncate it. A verifier may therefore hold a read-only copy of a journal.

def _complete_records(path: Path) -> tuple[list[dict], int]:
    """The complete records of one journal file, and 1 when a torn final line was ignored."""
    with open(path, "rb") as handle:
        data = handle.read()
    complete = data.rfind(b"\n") + 1
    records, offset = [], 0
    for line in data[:complete].splitlines(keepends=True):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            raise Rejected("journal_corrupt", f"{path.name}: unreadable record after byte {offset}") from None
        offset += len(line)
    return records, int(complete < len(data))


def _read_contracts(registry: Path) -> tuple[dict[tuple[str, int], dict], int]:
    """Installed contracts from ``registry.jsonl``, validated as ``Session._sync`` validates them."""
    if not registry.exists():
        return {}, 0
    records, torn = _complete_records(registry)
    contracts: dict[tuple[str, int], dict] = {}
    for record in records:
        if record["t"] != "contract":
            continue
        doc = record["doc"]
        try:
            valid = json.loads(canonical(prepare_contract(doc))) == doc
        except Rejected:
            valid = False
        if not valid:
            raise Rejected("journal_corrupt", f"contract {doc.get('id')}@{doc.get('version')} does not validate")
        contracts[(doc["id"], doc["version"])] = doc
    return contracts, torn


def _read_thread(path: Path, thread: str) -> tuple[dict, list[dict], int]:
    """The case row and events of a thread file, linked as ``Session._load`` links them."""
    records, torn = _complete_records(path)
    row, events = None, []
    for record in records:
        if record["t"] == "case":
            row = record["row"]
            continue
        event = record["event"]
        previous = events[-1]["hash"] if events else "0" * 64
        if (event["previous"] != previous or event["hash"] != event_digest(event)
                or event["case_id"] != thread or event["seq"] != len(events) + 1):
            raise Rejected("journal_corrupt", f"thread {thread} breaks its hash chain at seq {event.get('seq')}")
        side = record.get("side") or {}
        if isinstance(side.get("jcs"), str) and side["jcs"] != envelope_jcs(event):
            raise Rejected("journal_corrupt", f"thread {thread} side record differs at seq {event['seq']}")
        events.append(event)
    if row is None:
        raise Rejected("unknown_case", thread)
    return row, events, torn


def _layer_journal(path: Path, home: Path, payload: dict) -> str:
    thread, chain = payload["thread"], payload["chain"]
    try:
        if not CASE_ID.match(thread):
            raise Rejected("unknown_case", thread[:64])
        contracts, torn_registry = _read_contracts(home / "journal" / "registry.jsonl")
        row, events, torn_thread = _read_thread(path, thread)
        contract = contracts.get((row["contract_id"], row["contract_version"]))
    except Rejected as exc:
        if exc.code == "journal_corrupt":
            raise _Fail("journal_corrupt", exc.detail) from None
        raise _Fail(f"journal_unreadable:{exc.code}", exc.detail) from None
    except OSError as exc:
        raise _Fail("journal_unreadable:io_error",
                    f"cannot read {Path(exc.filename or path).name}: {exc.strerror or exc}") from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _Fail("journal_corrupt", f"{path.name} does not read as a thread journal") from None
    if contract is None:
        raise _Fail("journal_unreadable:unknown_contract",
                    f"{row['contract_id']}@{row['contract_version']} is not in the journal's registry")
    if len(events) < chain["count"]:
        raise _Fail("chain_truncated", f"certified {chain['count']} events, journal has {len(events)}")
    prefix = events[:chain["count"]]
    if prefix[-1]["hash"] != chain["head"]:
        raise _Fail("chain_head_mismatch",
                    f"certified {chain['head'][:10]}, journal {prefix[-1]['hash'][:10]} at event {chain['count']}")
    if not verify_events(prefix):
        raise _Fail("chain_invalid", f"the first {chain['count']} events do not link")
    try:
        expected = claims(row, prefix, contract)
    except Rejected as exc:
        if exc.code == "not_accepted":
            raise _Fail("not_accepted_in_journal", f"the first {chain['count']} events accept no candidate") from None
        raise _Fail("journal_inconsistent", exc.detail) from None
    for field in JOURNAL_FIELDS:
        where = first_difference(expected[field], payload[field], field)
        if where is not None:
            raise _Fail(f"field_mismatch:{where}",
                        f"certified {_short(_dig(payload, where))}, journal {_short(_dig(expected, where))}")
    torn = torn_registry + torn_thread
    note = f"; {torn} torn trailing record{'s' if torn > 1 else ''} ignored" if torn else ""
    return (f"{path}: {chain['count']} of {len(events)} events certified, head {chain['head'][:10]}; "
            f"every certified field matches{note}")


def first_difference(expected: Any, actual: Any, path: str = "") -> str | None:
    """Dotted path of the first leaf where ``actual`` differs from ``expected``, or None.

    Objects are walked in ``expected``'s key order, then any extra key of ``actual``; lists of
    different length name the list, lists of equal length the first differing element.
    """
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in list(expected) + [key for key in actual if key not in expected]:
            where = f"{path}.{key}" if path else key
            if key not in expected or key not in actual:
                return where
            found = first_difference(expected[key], actual[key], where)
            if found is not None:
                return found
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return path or "$"
        for index, (left, right) in enumerate(zip(expected, actual)):
            found = first_difference(left, right, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if type(expected) is not type(actual) or expected != actual:
        return path or "$"
    return None


def _dig(value: Any, where: str) -> Any:
    for token in where.replace("[", ".[").split("."):
        if token.startswith("["):
            index = int(token[1:-1])
            value = value[index] if isinstance(value, list) and index < len(value) else None
        else:
            value = value.get(token) if isinstance(value, dict) else None
    return value


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 60 else text[:57] + "..."


def format_report(report: Report) -> list[str]:
    """The fixed-label lines ``syberlabs verify`` prints: each layer that ran or was skipped, then the verdict."""
    lines, skipped = [], []
    for label, state, note in zip(LAYERS, (report.signature, report.repository, report.journal), report.notes):
        if state is None:
            lines.append(f"{label:<10} skipped  {note}")
            skipped.append(label)
        elif state == "ok":
            lines.append(f"{label:<10} ok   {note}")
        else:
            lines.append(f"{label:<10} FAIL {state}  {note}".rstrip())
            break
    if report.passed:
        lines.append("verdict    pass" + (f" ({' and '.join(skipped)} not checked)" if skipped else ""))
    else:
        lines.append(f"verdict    fail: {report.reason}")
    return lines
