"""The Build Thread: governed, Git-lineaged changes to one local repository.

    kit = Kit.local(".syberlabs")
    thread = kit.start("Add a CSV export", contract="repo-change.v1", paths=["src/", "tests/"])
    [candidate] = thread.propose(PatchProvider({"src/export.py": "..."}))
    verdict = thread.check(candidate.id)
    if verdict.acceptable:
        receipt = thread.accept(candidate.id)

A thread is a durable case pinned to a contract with an ``evolution`` section.
Providers submit provisional candidates; the host writes each as a commit under
``refs/syberlabs/candidates/`` without touching the working tree, runs the
contract's checks on it, and records everything in the thread's hash chain.
Accepting is an ordinary admitted action whose only effect is an atomic
compare-and-swap of the thread's target branch. It does not push, merge, or
publish anything. Reopening a thread rebuilds it from its journal.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from syberlabs import checks as runner
from syberlabs.errors import Rejected
from syberlabs.evidence import verified_reconciliation
from syberlabs.evolution import clean_path, in_scope, settings
from syberlabs.gitspace import CANDIDATE_REFS, MAX_FILE_BYTES, GitError, Repo
from syberlabs.journal import Journal
from syberlabs.providers import PatchProvider
from syberlabs.retrieval import Excerpt, context_digest, select
from syberlabs.search import Budget, Candidate, CheckResult, Evaluation, MergeResult, SearchProvider
from syberlabs.session import NoWrite, Session

HOST = "syberlabs.host"
TEMPLATE = "repo-change"
PROMOTE = "accept_change"
MAX_CHANGE_BYTES = 4 * 1024 * 1024
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

HINTS = {
    "candidate_not_evaluated": "Run its checks: syberlabs check {cid}",
    "candidate_evidence_stale": "Its evaluation is older than the contract allows. Run syberlabs check {cid} again.",
    "candidate_check_failed": "A required check failed on this exact tree. Read the output with syberlabs check {cid}, then propose a fix.",
    "candidate_check_missing": "A required check has no result. Run syberlabs check {cid} again.",
    "candidate_out_of_scope": "The change touches paths outside the contract's mutable scope or exceeds its size limits. Narrow it, or publish a new contract version.",
    "candidate_promotion_origin": "Only a person can accept a candidate. Models and search providers only propose.",
    "candidate_promotion_role": "Your roles do not include one of this contract's promotion roles.",
    "approval_required": "This contract needs an independent approver: syberlabs approve {cid} --actor NAME --role ROLE",
    "action_already_completed": "This thread already accepted a change. Start a new thread for the next one.",
    "effect_unresolved": "An earlier acceptance was interrupted. Run syberlabs recover first.",
    "action_not_in_global_policy": "The active policy does not allow this action.",
    "actor_role_missing": "The policy does not let your roles propose this action.",
    "target_moved": "The target branch no longer matches this thread's base. Start a new thread from the current commit.",
    "target_checked_out": "The target branch is checked out; SyberLabs will not move a branch under a working tree.",
    "budget_exhausted": "The thread used its contract budget. Start a new thread or publish a contract with a larger budget.",
}


def hint(reason: str | None, candidate: str | None = None) -> str | None:
    if not reason:
        return None
    found = HINTS.get(reason.split(":", 1)[0])
    return found.format(cid=candidate or "CANDIDATE") if found else None


@dataclass(frozen=True)
class Verdict:
    """Whether a candidate may be accepted now, and the evidence behind that answer."""

    candidate: str
    acceptable: bool
    status: str
    reason: str
    rule: str
    checks: tuple[CheckResult, ...]
    untested: tuple[str, ...]
    scope_violations: tuple[str, ...]
    limit_violations: tuple[str, ...]
    base_current: bool
    signal: Mapping[str, Any] | None
    hint: str | None

    def summary(self) -> str:
        marks = {"passed": "pass", "failed": "FAIL", "timed_out": "TIMEOUT", "error": "ERROR", "not_run": "not run"}
        lines = [f"{self.candidate}: {'acceptable' if self.acceptable else 'not acceptable'} "
                 f"({self.status}: {self.reason}; rule {self.rule})"]
        lines += [f"  {marks.get(c.state, c.state):8} {c.name}" + (f"  exit {c.exit_code}" if c.exit_code not in (None, 0) else "")
                  for c in self.checks]
        if self.untested:
            lines.append("  untested required checks: " + ", ".join(self.untested))
        if self.scope_violations or self.limit_violations:
            lines.append("  outside contract: " + ", ".join(self.scope_violations + self.limit_violations))
        if not self.base_current:
            lines.append("  target branch moved since the thread started")
        if self.signal:
            lines.append("  provider says (unverified): " + json.dumps(self.signal, sort_keys=True)[:200])
        if self.hint:
            lines.append("  next: " + self.hint)
        return "\n".join(lines)


@dataclass(frozen=True)
class Receipt:
    """What an accept, approve, or recover call actually did."""

    status: str
    candidate: str | None
    proposal_id: str | None = None
    reason: str | None = None
    ref: str | None = None
    commit: str | None = None
    event: str | None = None
    hint: str | None = None


class SearchStopped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GitRefEffect:
    """Compare-and-swap of a thread's target branch. The only authoritative write."""

    def __init__(self, kit: "Kit"):
        self.kit = kit

    def _target(self, case_id: str) -> tuple[str, bool]:
        return self.kit._target(case_id)

    def apply(self, case_id: str, args: dict, key: str) -> dict:
        repo = self.kit.repo
        ref, create = self._target(case_id)
        if repo.checked_out(ref):
            raise NoWrite(409, "target_checked_out")
        expected = None if create else args["base"]
        current = repo.read_ref(ref)
        if current != expected:
            raise NoWrite(412, "target_moved")
        try:
            repo.update_ref(ref, args["commit"], expected, f"syberlabs accept {args['candidate']} ({key})")
        except GitError:
            after = repo.read_ref(ref)
            if after != args["commit"]:
                if after == current:
                    raise NoWrite(412, "target_moved") from None
                raise
        return {"ref": ref, "old": expected, "new": args["commit"]}

    def status(self, case_id: str, args: dict, key: str) -> tuple[str, dict]:
        repo = self.kit.repo
        ref, _ = self._target(case_id)
        current = repo.read_ref(ref)
        if current and (current == args["commit"] or repo.is_ancestor(args["commit"], current)):
            return "applied", {"external_id": args["commit"], "ref": ref, "current": current}
        return "not_applied", {"ref": ref, "current": current}


class GitSearchSpace:
    """The ``SearchSpace`` a provider sees. Reads stay in the read scope; every write is a candidate."""

    def __init__(self, thread: "Thread", provider: SearchProvider, budget: Budget, excerpts: list[Excerpt], seed: int):
        self._thread, self._provider = thread, provider
        self.objective, self.base = thread.objective, thread.base
        self.scope = tuple(thread.read_scope()["paths"])
        self.budget, self.seed = budget, seed
        self.context = tuple({"path": e.path, "lines": [e.start, e.end], "reason": e.reason, "text": e.text} for e in excerpts)
        self._deadline = time.monotonic() + budget.max_seconds
        self.submitted: list[str] = []
        self.evaluations = 0
        self._commits = {view["id"]: view["commit"] for view in thread._views()}

    def _time(self) -> None:
        if time.monotonic() > self._deadline:
            raise SearchStopped("deadline")

    def _commit_of(self, ref: str) -> str:
        if ref == "base":
            return self.base
        if ref not in self._commits:
            self._commits[ref] = self._thread._view(ref)["commit"]
        return self._commits[ref]

    def files(self) -> list[str]:
        scope = self._thread.read_scope()
        return [path for path in self._thread.kit.repo.files(self.base) if in_scope(scope, path)]

    def read(self, ref: str, path: str) -> str | None:
        if not in_scope(self._thread.read_scope(), path):
            return None
        data = self._thread.kit.repo.read(self._commit_of(ref), path)
        if data is None or b"\0" in data[:8000]:
            return None
        try:
            return data.decode()
        except UnicodeDecodeError:
            return None  # a lossy decode would silently change the file if the provider wrote it back

    def candidate(self, candidate: str) -> Candidate:
        return Candidate.from_view(self._thread._view(candidate))

    def merge_base(self, a: str, b: str) -> str:
        found = self._thread.kit.repo.merge_base(self._commit_of(a), self._commit_of(b))
        by_commit = {commit: cid for cid, commit in self._commits.items()}
        return by_commit.get(found, "base")

    def merge_text(self, base: str, ours: str, theirs: str) -> MergeResult:
        text, conflicts = self._thread.kit.repo.merge_text(base, ours, theirs)
        return MergeResult(text, conflicts)

    def submit(self, changes: Mapping[str, str | None], *, parents: Sequence[str] = (), operator: str,
               message: str = "", signal: Mapping[str, Any] | None = None) -> Candidate:
        self._time()
        if len(self.submitted) >= self.budget.max_candidates:
            raise SearchStopped("budget_exhausted")
        if operator not in self._provider.operators:
            raise Rejected("operator_not_permitted", f"{self._provider.name} did not declare {operator!r}")
        found = self._thread._submit(changes, parents, operator, message, signal, self._provider, self.submitted)
        self._commits[found.id] = found.commit
        return found

    def evaluate(self, candidate: str) -> Evaluation:
        self._time()
        if self.evaluations >= self.budget.max_evaluations:
            raise SearchStopped("budget_exhausted")
        found, ran = self._thread._evaluate(candidate)
        self.evaluations += ran
        return found

    def remaining(self) -> Budget:
        return Budget(self.budget.max_candidates - len(self.submitted),
                      self.budget.max_evaluations - self.evaluations,
                      max(0, int(self._deadline - time.monotonic())))


def detect_checks(root: Path) -> dict:
    """Guess the project's test command. Unknown projects get a check that fails until named."""
    python = "python3" if shutil.which("python3") else "python"
    tests = root / "tests"
    if tests.is_dir() and any(tests.glob("test*.py")):
        return {"tests": {"argv": [python, "-m", "unittest", "discover", "-s", "tests", "-q"], "timeout_seconds": 600}}
    package = root / "package.json"
    if package.exists():
        try:
            if "test" in json.loads(package.read_text()).get("scripts", {}):
                return {"tests": {"argv": ["npm", "test", "--silent"], "timeout_seconds": 600}}
        except (ValueError, AttributeError):
            pass
    if (root / "Cargo.toml").exists():
        return {"tests": {"argv": ["cargo", "test", "--quiet"], "timeout_seconds": 900}}
    if (root / "go.mod").exists():
        return {"tests": {"argv": ["go", "test", "./..."], "timeout_seconds": 900}}
    return {"configure": {"argv": [python, "-c", "import sys; sys.exit('name your checks in .syberlabs/contracts/repo-change.v1.json')"]}}


def default_contract(root: Path) -> dict:
    found = detect_checks(root)
    return {
        "id": TEMPLATE, "version": 1, "title": "Change this repository",
        "inputs": {"objective": "string"},
        "actions": {PROMOTE: {}},
        "acceptance": [{"id": "accepted", "kind": "effect", "action": PROMOTE}],
        "evolution": {
            "scope": {"paths": ["."], "exclude": [], "max_files": 20, "max_diff_bytes": 200_000},
            "operators": ["patch"],
            "budget": {"max_candidates": 12, "max_evaluations": 24, "max_seconds": 900},
            "evaluation": {"checks": found, "required": sorted(found), "max_age_seconds": 3600},
            "promotion": {"action": PROMOTE, "roles": ["developer"]},
        },
    }


DEFAULT_POLICY = {"version": 1, "actions": {PROMOTE: {"roles": ["developer"]}}}


def parse_contract(name: str) -> tuple[str, int | None]:
    for separator in (".v", "@"):
        head, found, tail = name.rpartition(separator)
        if found and head and tail.isdigit():
            return head, int(tail)
    return name, None


class Kit:
    """A local SyberLabs home for one repository: journal, contracts, policy, and threads."""

    def __init__(self, home: Path, repo: Repo, session: Session, journal: Journal, actor: str, roles: Sequence[str]):
        self.home, self.repo, self.session, self.journal = home, repo, session, journal
        self.actor, self.roles = actor, list(roles)
        self._effect = GitRefEffect(self)

    @classmethod
    def local(cls, home: str | Path = ".syberlabs", repo: str | Path = ".", *, actor: str | None = None,
              roles: Sequence[str] = ("developer",)) -> "Kit":
        repo = Repo(repo)
        home = Path(home)
        home = home if home.is_absolute() else repo.root / home
        home.mkdir(parents=True, exist_ok=True)
        cls._exclude(repo, home)
        journal = Journal(home / "journal")
        session = Session(journal=journal)
        if actor is None:
            actor = repo.git("config", "user.email", ok=(0, 1)).decode().strip() or "developer"
        kit = cls(home, repo, session, journal, actor, roles)
        kit._install_files()
        return kit

    def close(self) -> None:
        self.journal.close()

    @staticmethod
    def _exclude(repo: Repo, home: Path) -> None:
        try:
            relative = home.resolve().relative_to(repo.root)
        except ValueError:
            return
        common = Path(repo.git("rev-parse", "--git-common-dir").decode().strip())
        common = common if common.is_absolute() else repo.root / common
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        line = "/" + relative.as_posix().rstrip("/") + "/"
        existing = exclude.read_text().splitlines() if exclude.exists() else []
        if line not in existing:
            with open(exclude, "a") as handle:
                handle.write(("\n" if existing and existing[-1] else "") + line + "\n")

    def _install_files(self) -> None:
        contracts = self.home / "contracts"
        contracts.mkdir(exist_ok=True)
        if not any(contracts.glob("*.json")):
            (contracts / f"{TEMPLATE}.v1.json").write_text(json.dumps(default_contract(self.repo.root), indent=2) + "\n")
        policy = self.home / "policy.json"
        if not policy.exists():
            policy.write_text(json.dumps(DEFAULT_POLICY, indent=2) + "\n")
        for path in sorted(contracts.glob("*.json")):
            doc = json.loads(path.read_text())
            try:
                self.session.install_contract(doc)
            except Rejected as exc:
                if exc.code == "immutable_contract":
                    raise Rejected("immutable_contract", f"{path.name} changed after {doc['id']}@{doc['version']} was "
                                   f"published. Save it as {doc['id']}.v{doc['version'] + 1}.json with version "
                                   f"{doc['version'] + 1} instead.") from None
                raise Rejected(exc.code, f"{path.name}: {exc.detail}") from None
            config = settings(self.session.contracts[(doc["id"], doc["version"])])
            if config and config["promotion"]["action"] not in self.session.actions:
                self.session.install_action(config["promotion"]["action"], {
                    "kind": "local", "effect": "git_ref",
                    "title": "Move the thread's target branch to the accepted candidate (compare-and-swap)"})
        doc = json.loads(policy.read_text())
        known = self.session.policies.get(doc.get("version"))
        if known is None or known != doc:
            try:
                self.session.install_policy(doc)
            except Rejected as exc:
                raise Rejected(exc.code, f"policy.json: {exc.detail}; bump its version to publish a change") from None
        for name, action in self.session.actions.items():
            if action.get("effect") == "git_ref":
                self.session.bind_effect(name, self._effect)

    def _target(self, case_id: str) -> tuple[str, bool]:
        row = self.session.cases[case_id]
        template = settings(self.session.contracts[(row["contract_id"], row["contract_version"])])["promotion"]["target_ref"]
        return template.replace("{thread}", case_id[:8]), "{thread}" in template

    def _resolve(self, name: str | None) -> tuple[str, int]:
        cid, version = parse_contract(name or TEMPLATE)
        versions = sorted(v for c, v in self.session.contracts if c == cid)
        if not versions or (version is not None and version not in versions):
            raise Rejected("unknown_contract", f"{name}: installed are " +
                           ", ".join(f"{c}.v{v}" for c, v in sorted(self.session.contracts)))
        return cid, version if version is not None else versions[-1]

    def start(self, objective: str, contract: str | None = None, *, paths: Sequence[str] | None = None,
              actor: str | None = None) -> "Thread":
        if not isinstance(objective, str) or not objective.strip():
            raise Rejected("invalid_objective", "describe the change in a sentence")
        cid, version = self._resolve(contract)
        doc = self.session.contracts[(cid, version)]
        if "evolution" not in doc or doc["inputs"] != {"objective": "string"}:
            raise Rejected("invalid_contract", f"{cid}.v{version} needs an evolution section and one objective input")
        base = self.repo.rev("HEAD")
        if base is None:
            raise Rejected("empty_repository", "commit something first; a thread starts from HEAD")
        case = self.session.create_case(cid, version, {"objective": objective.strip()}, actor or self.actor)
        self.session.observe(case, "base", {"commit": base, "tree": self.repo.tree(base), "branch": self.repo.current_branch()},
                             "git", base, HOST, verified=True)
        thread = Thread(self, case)
        if paths:
            thread.attach_source("repository", paths=paths)
        return thread

    def open(self, thread_id: str) -> "Thread":
        matches = [t for t in self.journal.thread_ids() if t.startswith(thread_id)] if thread_id else []
        if len(matches) != 1:
            raise Rejected("unknown_thread", f"{thread_id!r} matches {len(matches)} threads")
        self.session.inspect(matches[0])
        return Thread(self, matches[0])

    def threads(self) -> list[dict]:
        rows = []
        for case_id in self.journal.thread_ids():
            row = self.journal.first(case_id)
            if row:
                rows.append({"id": case_id, "objective": row["inputs"].get("objective"),
                             "contract": f"{row['contract_id']}.v{row['contract_version']}", "created": row["created"]})
        return sorted(rows, key=lambda r: r["created"])

    def contract_diff(self, a: str, b: str) -> list[dict]:
        """Field-level differences between two installed contract versions."""
        left = self.session.contracts[self._resolve(a)]
        right = self.session.contracts[self._resolve(b)]
        changes: list[dict] = []

        def walk(x, y, path):
            if isinstance(x, dict) and isinstance(y, dict):
                for key in sorted(set(x) | set(y)):
                    walk(x.get(key, _MISSING), y.get(key, _MISSING), f"{path}.{key}" if path else key)
            elif x != y:
                changes.append({"path": path, "before": None if x is _MISSING else x, "after": None if y is _MISSING else y})

        walk(left, right, "")
        return [c for c in changes if c["path"] not in ("version",)]

    def memory(self) -> dict:
        """What this kit keeps, why, and how it goes away."""
        refs = self.repo.refs(CANDIDATE_REFS)
        return {
            "thread_records": {"threads": len(self.journal.thread_ids()), "bytes": self.journal.size(),
                               "where": str(self.journal.home),
                               "why": "hash-chained history of decisions, evidence, and effects; needed to verify an accepted change",
                               "removal": "delete the journal directory; history that proves an effect goes with it"},
            "context": {"stored": "paths, blob ids, line ranges, and a digest; excerpt text is re-read from Git for a provider call and not kept"},
            "candidate_refs": {"count": len(refs), "where": CANDIDATE_REFS,
                               "removal": "syberlabs prune deletes refs of unaccepted candidates in finished threads"},
            "project_knowledge": "none: not implemented",
            "personal_preferences": "none: not implemented",
        }

    def prune(self, thread_id: str | None = None) -> int:
        """Delete candidate refs that were not accepted, for finished threads (or one named thread)."""
        removed = 0
        for case_id in ([self.open(thread_id).id] if thread_id else self.journal.thread_ids()):
            inspected = self.session.inspect(case_id)
            if thread_id is None and inspected["status"] == "in_progress":
                continue
            keep = {view["commit"] for view in inspected.get("candidates", []) if view["authoritative"]}
            for ref, commit in self.repo.refs(f"{CANDIDATE_REFS}/{case_id[:8]}/").items():
                if commit not in keep:
                    self.repo.delete_ref(ref)
                    removed += 1
        return removed

    def export(self, thread_id: str) -> dict:
        thread = self.open(thread_id)
        inspected = self.session.inspect(thread.id)
        return {"status": thread.status(), "contract": inspected["contract"], "events": inspected["events"],
                "side": self.session.side_channel(thread.id), "chain_valid": inspected["chain_valid"]}


_MISSING = object()


class Thread:
    """One unit of work on the repository. All state lives in the session journal."""

    def __init__(self, kit: Kit, case_id: str):
        self.kit, self.id = kit, case_id
        row = kit.session.cases[case_id]
        self.objective = row["inputs"]["objective"]
        self.contract = (row["contract_id"], row["contract_version"])
        self.config = settings(kit.session.contracts[self.contract])
        self.base = self._latest("base")["value"]["commit"]
        self.target_ref, self._create = kit._target(case_id)

    # Views over the history

    def _events(self) -> list[dict]:
        return self.kit.session.history(self.id)

    def _latest(self, key: str) -> dict | None:
        events = self._events()
        found = next((e for e in reversed(events) if e["kind"] == "observed" and e["body"]["key"] == key
                      and e["body"]["source"] in ("git", HOST) and e["body"].get("verified")), None)
        return found["body"] if found else None

    def _views(self) -> list[dict]:
        return self.kit.session.candidates(self.id)

    def _view(self, candidate: str) -> dict:
        found = next((view for view in self._views() if view["id"] == candidate), None)
        if found is None:
            raise Rejected("unknown_candidate", str(candidate)[:64])
        return found

    def candidates(self) -> list[Candidate]:
        return [Candidate.from_view(view) for view in self._views()]

    def history(self) -> list[dict]:
        return self._events()

    def read_scope(self) -> dict:
        attached = self._latest("sources")
        paths = attached["value"]["repository"]["paths"] if attached else self.config["scope"]["paths"]
        return {"paths": paths, "exclude": self.config["scope"]["exclude"]}

    # Sources and context

    def attach_source(self, name: str = "repository", *, paths: Sequence[str]) -> dict:
        """Limit what providers may read to these repository paths. The latest attachment wins."""
        if name != "repository":
            raise Rejected("unknown_source", "the local kit reads one source: repository")
        cleaned = [clean_path(path) for path in paths]
        if not cleaned or any(p is None for p in cleaned) or any(p.split("/")[0] in (".git", ".syberlabs") for p in cleaned):
            raise Rejected("invalid_source", "paths must be relative repository paths outside .git and .syberlabs")
        return self.kit.session.observe(self.id, "sources", {"repository": {"paths": sorted(set(cleaned))}},
                                        HOST, self.base, HOST, verified=True)

    def context(self, *, query: str | None = None, drop: Sequence[str] = (), max_bytes: int = 24_000) -> list[Excerpt]:
        """Select excerpts for a provider and record what was chosen (not the text)."""
        excerpts = select(self.kit.repo, self.base, self.read_scope(), query or self.objective,
                          max_bytes=max_bytes, drop=tuple(drop))
        self.kit.session.observe(self.id, "context", {
            "query": query or self.objective, "digest": context_digest(excerpts),
            "entries": [excerpt.ref() for excerpt in excerpts]}, HOST, self.base, HOST, verified=True)
        return excerpts

    def _recorded_context(self) -> list[Excerpt]:
        recorded = self._latest("context")
        if recorded is None:
            return self.context()
        excerpts = []
        for entry in recorded["value"]["entries"]:
            data = self.kit.repo.git("cat-file", "blob", entry["blob"]).decode(errors="replace").splitlines()
            start, end = entry["lines"]
            excerpts.append(Excerpt(entry["path"], entry["blob"], start, end, entry["reason"],
                                    "\n".join(data[start - 1:end]).encode()[: entry["bytes"]].decode(errors="ignore")))
        return excerpts

    # Proposals

    def propose(self, provider: SearchProvider | None = None, *, changes: Mapping[str, str | None] | None = None,
                message: str = "", seed: int = 0, max_seconds: int | None = None) -> list[Candidate]:
        """Run one search. Returns the candidates it submitted, all provisional."""
        if provider is None:
            if changes is None:
                raise Rejected("invalid_proposal", "pass a provider or changes")
            provider = PatchProvider(changes, message)
        name = getattr(provider, "name", None)
        revision = getattr(provider, "revision", None)
        operators = tuple(getattr(provider, "operators", ()))
        if not isinstance(name, str) or not _NAME.match(name) or not isinstance(revision, str) or not revision:
            raise Rejected("invalid_provider", "a provider needs a lowercase name and a revision")
        if not operators or any(op not in self.config["operators"] for op in operators):
            raise Rejected("operator_not_permitted", f"{name} uses {list(operators)}; this contract allows {self.config['operators']}")
        views = self._views()
        used_evaluations = sum(1 for e in self._events() if e["kind"] == "candidate_evaluated")
        budget = Budget(self.config["budget"]["max_candidates"] - len(views),
                        self.config["budget"]["max_evaluations"] - used_evaluations,
                        min(max_seconds or self.config["budget"]["max_seconds"], self.config["budget"]["max_seconds"]))
        if budget.max_candidates <= 0:
            raise Rejected("budget_exhausted", "max_candidates")
        excerpts = self._recorded_context()
        search_id = f"s{sum(1 for e in self._events() if e['kind'] == 'search_started') + 1}"
        self.kit.session.record_search(self.id, "started", {
            "id": search_id, "provider": {"name": name, "revision": revision}, "operators": list(operators),
            "budget": {"max_candidates": budget.max_candidates, "max_evaluations": budget.max_evaluations,
                       "max_seconds": budget.max_seconds},
            "base": self.base, "context_digest": context_digest(excerpts)}, HOST)
        space = GitSearchSpace(self, provider, budget, excerpts, seed)
        started, stopped, error, recommended = time.monotonic(), "completed", None, []
        try:
            chosen = provider.search(space)
            recommended = [c for c in (chosen or []) if c in space.submitted]
        except SearchStopped as exc:
            stopped = exc.reason
        except KeyboardInterrupt:
            self._finish(search_id, "cancelled", space, [], started, None)
            raise
        except Rejected as exc:
            stopped, error = "error", exc.code if _NAME.match(exc.code) else "provider_error"
        except Exception:
            stopped, error = "error", "provider_error"
        self._finish(search_id, stopped, space, recommended, started, error)
        return [Candidate.from_view(self._view(c)) for c in space.submitted]

    def _finish(self, search_id, stopped, space, recommended, started, error) -> None:
        self.kit.session.record_search(self.id, "finished", {
            "id": search_id, "stopped": stopped, "candidates": list(space.submitted),
            "recommended": list(dict.fromkeys(recommended)), "evaluations": space.evaluations,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "error": error}, HOST)

    def _submit(self, changes, parents, operator, message, signal, provider, submitted) -> Candidate:
        if not isinstance(changes, Mapping) or not changes:
            raise Rejected("invalid_change", "changes must map at least one path to text or None")
        encoded, total = {}, 0
        for path, text in changes.items():
            cleaned = clean_path(path)
            if cleaned is None or cleaned == "." or not (text is None or isinstance(text, str)):
                raise Rejected("invalid_change", f"bad path or content for {str(path)[:80]!r}")
            data = None if text is None else text.encode()
            if data is not None and len(data) > MAX_FILE_BYTES:
                raise Rejected("invalid_change", f"{cleaned} exceeds {MAX_FILE_BYTES} bytes")
            total += len(data or b"")
            encoded[cleaned] = data
        if total > MAX_CHANGE_BYTES:
            raise Rejected("invalid_change", f"changes exceed {MAX_CHANGE_BYTES} bytes")
        views = {view["id"]: view for view in self._views()}
        if len(parents) > 8 or any(p not in views for p in parents):
            raise Rejected("invalid_change", "parents must be candidates of this thread")
        parent_commits = [views[p]["commit"] for p in parents] or [self.base]
        number = len(views) + 1
        cid = f"c{number}"
        body = (f"{operator}: {message or self.objective}"[:200] + "\n\n"
                f"SyberLabs-Thread: {self.id}\nSyberLabs-Candidate: {cid}\n"
                f"SyberLabs-Provider: {provider.name}@{provider.revision}\n")
        repo = self.kit.repo
        commit = repo.commit(parent_commits, encoded, body)
        tree = repo.tree(commit)
        if len(parent_commits) == 1 and tree == repo.tree(parent_commits[0]):
            raise Rejected("no_change", "the edit leaves its parent's tree unchanged")
        repo.git("update-ref", f"{CANDIDATE_REFS}/{self.id[:8]}/{cid}", commit)
        changed = repo.changed_paths(self.base, commit)
        _, size, sha = repo.diff(self.base, commit, max_bytes=0)
        self.kit.session.record_candidate(self.id, {
            "id": cid, "commit": commit, "tree": tree, "base": self.base, "parents": list(parents),
            "operator": operator, "provider": {"name": provider.name, "revision": provider.revision},
            "changed_paths": changed, "diff": {"digest": sha, "bytes": size, "files": len(changed)},
            "signal": dict(signal) if signal is not None else None, "note": (message or "")[:500]}, HOST)
        submitted.append(cid)
        return Candidate.from_view(self._view(cid))

    def worktree_changes(self) -> tuple[dict[str, str | None], list[str]]:
        """Working-tree edits relative to the thread base, and paths left out.

        Only paths inside both the attached sources and the contract's mutable
        scope are taken, so scratch files elsewhere are not swept into a candidate.
        """
        changes, skipped = {}, []
        scope = {"paths": self.config["scope"]["paths"], "exclude": self.config["scope"]["exclude"]}
        sources = self.read_scope()
        for path in self.kit.repo.working_changes(self.base):
            if not in_scope(scope, path) or not in_scope(sources, path):
                skipped.append(path)
                continue
            file = self.kit.repo.root / path
            if not file.exists():
                changes[path] = None
            elif file.is_file() and file.stat().st_size <= MAX_FILE_BYTES:
                data = file.read_bytes()
                try:
                    if b"\0" in data[:8000]:
                        raise UnicodeDecodeError("utf-8", data, 0, 1, "binary")
                    changes[path] = data.decode()
                except UnicodeDecodeError:
                    skipped.append(path)  # binary or not UTF-8: never re-encode it lossily
            else:
                skipped.append(path)
        return changes, skipped

    # Evidence

    def _evaluate(self, candidate: str) -> tuple[Evaluation, int]:
        view = self._view(candidate)
        required = tuple(self.config["evaluation"]["required"])
        if view["scope_violations"] or view["limit_violations"]:
            skipped = tuple(CheckResult(name, "not_run", None, 0, "outside the contract's scope; not executed")
                            for name in self.config["evaluation"]["checks"])
            return Evaluation(candidate, skipped, required), 0
        body = runner.evaluate(self.kit.repo, view, self.config["evaluation"])
        self.kit.session.record_evaluation(self.id, body, HOST)
        results = tuple(CheckResult(c["name"], c["state"], c["exit_code"], c["duration_ms"], c["output_tail"])
                        for c in body["checks"])
        return Evaluation(candidate, results, required), 1

    def check(self, candidate: str, *, rerun: bool = False, actor: str | None = None,
              roles: Sequence[str] | None = None) -> Verdict:
        """Run the contract's checks if this tree has no fresh result, then ask admission."""
        view = self._view(candidate)
        in_scope_ = not view["scope_violations"] and not view["limit_violations"]
        if in_scope_ and (rerun or view["evaluation"]["state"] in ("none", "stale")):
            try:
                self._evaluate(candidate)
            except Rejected as exc:
                if exc.code != "budget_exhausted":
                    raise
            view = self._view(candidate)
        preview = self.kit.session.preview(self.id, self.config["promotion"]["action"], self._args(view),
                                           actor or self.kit.actor, list(roles or self.kit.roles))
        results = {c["name"]: c for c in view["evaluation"]["checks"]}
        checks = tuple(CheckResult(name, results[name]["state"], results[name]["exit_code"], results[name]["duration_ms"],
                                   results[name]["output_tail"]) if name in results else
                       CheckResult(name, "not_run", None, 0, "") for name in self.config["evaluation"]["checks"])
        untested = tuple(name for name in self.config["evaluation"]["required"] if name not in results)
        current = self._target_current()
        acceptable = preview["status"] in ("allowed", "needs_approval") and current
        reason = preview["reason"] if preview["status"] != "allowed" or current else "target_moved"
        return Verdict(candidate, acceptable, preview["status"], preview["reason"], preview["rule"], checks, untested,
                       tuple(view["scope_violations"]), tuple(view["limit_violations"]), current, view["signal"],
                       hint(reason if not acceptable or preview["status"] != "allowed" else None, candidate))

    def diff(self, candidate: str, *, max_bytes: int = 200_000) -> str:
        view = self._view(candidate)
        text, size, _ = self.kit.repo.diff(self.base, view["commit"], max_bytes=max_bytes)
        return text if size <= max_bytes else text + f"\n[diff truncated: {size} bytes total]\n"

    # Authority

    def _args(self, view: dict) -> dict:
        return {"candidate": view["id"], "commit": view["commit"], "base": view["base"]}

    def _target_current(self) -> bool:
        current = self.kit.repo.read_ref(self.target_ref)
        return current is None if self._create else current == self.base

    def _pending(self, candidate: str, actor: str | None = None) -> str | None:
        """Newest promotion proposal for this candidate that has no effect and was not denied."""
        events = self._events()
        action = self.config["promotion"]["action"]
        touched = {e["body"]["proposal_id"] for e in events
                   if e["kind"] in ("effect_started", "effect_unknown", "effect_rejected", "effect_succeeded")}
        decisions = {e["body"]["proposal_id"]: e["body"] for e in events if e["kind"] == "decision"}
        for event in reversed(events):
            body = event["body"]
            if (event["kind"] == "proposed" and body["action"] == action and isinstance(body["args"], dict)
                    and body["args"].get("candidate") == candidate and (actor is None or body["actor"] == actor)
                    and body["id"] not in touched
                    and decisions.get(body["id"], {}).get("status") in ("allowed", "needs_approval")):
                return body["id"]
        return None

    def accept(self, candidate: str, *, actor: str | None = None, roles: Sequence[str] | None = None) -> Receipt:
        """Propose and, if admitted, apply the promotion. Never pushes or merges anything."""
        actor, roles = actor or self.kit.actor, list(roles or self.kit.roles)
        view = self._view(candidate)
        session, action = self.kit.session, self.config["promotion"]["action"]
        proposal_id = self._pending(candidate, actor)
        if proposal_id is None:
            proposed = session.propose(self.id, action, self._args(view), actor, roles)
            proposal_id, decision = proposed["proposal"]["id"], proposed["decision"]
            if decision["status"] != "allowed":
                return Receipt(decision["status"], candidate, proposal_id, decision["reason"],
                               hint=hint(decision["reason"], candidate))
        result = session.commit(self.id, proposal_id, actor)
        if "decision" in result:
            reason = result["decision"]["reason"]
            return Receipt(result["decision"]["status"], candidate, proposal_id, reason, hint=hint(reason, candidate))
        if result["status"] == "succeeded":
            output = result["event"]["body"]["output"]
            return Receipt("succeeded", candidate, proposal_id, None, output["ref"], output["new"], result["event"]["hash"])
        if result["status"] == "rejected":
            reason = result.get("detail")
            return Receipt("rejected", candidate, proposal_id, reason, self.target_ref, None, result["event"]["hash"],
                           hint(reason, candidate))
        return Receipt("unknown", candidate, proposal_id, "effect_unknown", self.target_ref,
                       hint=hint("effect_unresolved", candidate))

    def approve(self, candidate: str, *, actor: str, roles: Sequence[str]) -> Receipt:
        """Independent approval of the pending promotion proposal for this candidate."""
        proposal_id = self._pending(candidate)
        if proposal_id is None:
            raise Rejected("nothing_to_approve", f"no pending acceptance for {candidate}")
        event = self.kit.session.approve(self.id, proposal_id, actor, list(roles))
        return Receipt("approved", candidate, proposal_id, event["body"]["role"], event=event["hash"],
                       hint=f"The proposer can now run syberlabs accept {candidate}")

    def recover(self, *, actor: str | None = None, roles: Sequence[str] | None = None) -> list[Receipt]:
        """Settle interrupted acceptances by reading the target branch."""
        actor, roles = actor or self.kit.actor, list(roles or self.kit.roles)
        events = self._events()
        done = {e["body"]["proposal_id"] for e in events
                if e["kind"] in ("effect_succeeded", "effect_rejected") or verified_reconciliation(e)}
        receipts = []
        for event in events:
            body = event["body"]
            if event["kind"] == "effect_started" and body["proposal_id"] not in done:
                proposal = next(e["body"] for e in events if e["kind"] == "proposed" and e["body"]["id"] == body["proposal_id"])
                found = self.kit.session.reconcile(self.id, body["proposal_id"], actor, roles)
                status = {"verified": "succeeded"}.get(found["status"], found["status"])
                receipts.append(Receipt(status, proposal["args"].get("candidate"), body["proposal_id"], found["reason"],
                                        self.target_ref, proposal["args"].get("commit") if status == "succeeded" else None,
                                        found["event"]["hash"]))
        return receipts

    # Resume

    def status(self) -> dict:
        """Everything needed to resume: no chat transcript, only the recorded state."""
        inspected = self.kit.session.inspect(self.id)
        events = inspected["events"]
        views = inspected.get("candidates", [])
        started = {e["body"]["proposal_id"] for e in events if e["kind"] == "effect_started"}
        done = {e["body"]["proposal_id"] for e in events
                if e["kind"] in ("effect_succeeded", "effect_rejected") or verified_reconciliation(e)}
        open_items, next_steps = [], []
        if started - done:
            open_items.append("an acceptance was interrupted; its outcome is unknown until recovered")
            next_steps.append("syberlabs recover")
        for view in views:
            state = view["promotion"]["state"]
            if state == "needs_approval":
                open_items.append(f"{view['id']} waits for approval ({view['promotion']['reason']})")
        accepted = next((v for v in views if v["authoritative"]), None)
        if inspected["status"] == "in_progress" and not (started - done):
            if not views:
                next_steps.append("syberlabs propose")
            for view in views:
                if view["evaluation"]["state"] in ("none", "stale") and not view["scope_violations"] and not view["limit_violations"]:
                    next_steps.append(f"syberlabs check {view['id']}")
                elif view["evaluation"]["state"] == "passed" and view["promotion"]["state"] in ("provisional", "denied", "not_applied", "admitted"):
                    next_steps.append(f"syberlabs accept {view['id']}")
        context = self._latest("context")
        return {
            "thread": self.id, "objective": self.objective, "contract": f"{self.contract[0]}.v{self.contract[1]}",
            "status": inspected["status"], "base": self.base, "target_ref": self.target_ref,
            "target": self.kit.repo.read_ref(self.target_ref), "accepted": accepted["id"] if accepted else None,
            "sources": self.read_scope()["paths"],
            "context": ({"digest": context["value"]["digest"], "files": len({e["path"] for e in context["value"]["entries"]}),
                         "bytes": sum(e["bytes"] for e in context["value"]["entries"])} if context else None),
            "candidates": [{"id": v["id"], "operator": v["operator"], "parents": v["parents"],
                            "provider": f"{v['provider']['name']}@{v['provider']['revision']}",
                            "files": len(v["changed_paths"]), "in_scope": not v["scope_violations"] and not v["limit_violations"],
                            "evaluation": v["evaluation"]["state"], "promotion": v["promotion"]["state"]} for v in views],
            "budget": {"candidates": [len(views), self.config["budget"]["max_candidates"]],
                       "evaluations": [sum(1 for e in events if e["kind"] == "candidate_evaluated"),
                                       self.config["budget"]["max_evaluations"]]},
            "open": open_items, "next": next_steps, "events": len(events), "chain_valid": inspected["chain_valid"],
        }
