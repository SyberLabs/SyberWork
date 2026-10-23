"""Settlement poll: what became of the pull requests Build Threads opened, as verified host observations.

    syberlabs settle [--thread ID]          # one pass over every published thread (or one)
    syberlabs run                           # also one pass per runner pass, when the action's token is set

A thread is *published* when its ``github_pull_request`` effect succeeded (or a verified reconciliation found
it): the effect's ``external_id`` is the pull request's number, and the installed action definition
(``.syberlabs/actions.json``) names the ``api``, the ``repository``, the ``branch`` and ``token_env``, the
environment variable that holds the token. For each published pull request one pass reads, with the standard
library only,

    GET {api}/repos/{repository}/pulls/{number}
    GET {api}/repos/{repository}/pulls/{number}/reviews?per_page=100&page=N

and writes what it found through ``Session.observe`` as ``observed`` events by ``syberlabs.host`` with
``verified: true``, the route ``unattended``, ``proposal_refused`` and ``human_intervention`` use; no event kind
is added:

- ``pr_merged`` ``{number, merged_at, merge_commit, merged_by}``, versioned by the merge commit: once. A thread
  whose merge is recorded is not polled again.
- ``pr_closed`` ``{number, closed_at}``, versioned by ``closed_at``: when the pull request is closed unmerged,
  once per closing.
- ``pr_review`` ``{number, review_id, state, reviewer, at}``, versioned by the review id: once per submitted
  review, ``state`` in GitHub's words lowercased (``approved``, ``changes_requested``, ``commented``,
  ``dismissed``), the shape ``syberlabs.metrics`` reads.

What GitHub answers is untrusted input: the pull request must carry the number asked for (an integer, not
``true``) and the thread's branch as its head, every value read from the answer is checked by type before it is
used (``head`` an object, ``ref`` a string, ``merged`` a boolean, ``user`` an object or null), and every
recorded field is checked (a login, a 40-hex commit, an ISO 8601 time), or nothing is recorded for that pull
request: any shape error is a ``PollFailed``, never another exception. A non-200 answer, a redirect, an
oversized body, a network failure, an unreadable body or a
missing token records nothing and is reported (``Settled.error``); an unknown stays unknown. The token is
read from the environment for the request and is never written, printed or put in an error.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping

from syberlabs import metrics
from syberlabs.errors import Rejected
from syberlabs.evidence import verified_reconciliation
from syberlabs.publish import HTTP, thread_branch
from syberlabs.targets import guard_request

HOST = metrics.HOST
EFFECT = "github_pull_request"
TIMEOUT_SECONDS = 30
MAX_BYTES = 1_048_576
PER_PAGE = 100
MAX_PAGES = 10
REVIEW_STATES = ("approved", "changes_requested", "commented", "dismissed")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


class PollFailed(Exception):
    """GitHub did not answer with what the poll needs. The message never carries the token or a header."""


@dataclass(frozen=True)
class Published:
    """A pull request a thread's ``github_pull_request`` effect opened, and where to ask about it."""

    thread: str
    number: int
    action: str
    api: str
    repository: str
    branch: str
    token_env: str


@dataclass
class Settled:
    """One pull request after one pass: its state and the facts this pass recorded, or why nothing was."""

    thread: str
    number: int
    state: str                      # open, merged, closed, or unknown when the poll failed
    recorded: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def line(self) -> str:
        head = f"{self.thread[:8]} #{self.number}"
        if self.error:
            return f"{head}: {self.error}; nothing recorded"
        found = ", ".join(self.recorded) if self.recorded else "nothing new"
        return f"{head} {self.state}: {found}"


def published(kit, case_id: str, events: list[dict] | None = None) -> list[Published]:
    """The pull requests this thread opened through an installed ``github_pull_request`` action, by number."""
    events = kit.session.history(case_id) if events is None else events
    found: dict[int, Published] = {}
    for event in events:
        body = event["body"]
        if event["kind"] == "effect_succeeded":
            output = body.get("output")
        elif verified_reconciliation(event):
            output = body.get("proof")
        else:
            continue
        doc = kit.session.actions.get(body.get("action")) or {}
        external = str((output or {}).get("external_id") or "") if isinstance(output, dict) else ""
        if doc.get("effect") != EFFECT or not (external.isascii() and external.isdigit()):
            continue
        number = int(external)
        found[number] = Published(case_id, number, body["action"], doc["api"].rstrip("/"), doc["repository"],
                                  thread_branch(doc, case_id), doc["token_env"])
    return list(found.values())


def tokens_available(kit, environ: Mapping[str, str] | None = None) -> bool:
    """True when an installed ``github_pull_request`` action's ``token_env`` is set: the runner polls only then."""
    environ = os.environ if environ is None else environ
    return any(doc.get("effect") == EFFECT and environ.get(doc.get("token_env") or "")
               for doc in kit.session.actions.values())


def describe(exc: BaseException, kit, environ: Mapping[str, str] | None = None) -> str:
    """One stderr line for an unexpected error in a pass: ``settle: <Type>: <message>``, with the value of every
    installed action's token variable removed, on one line and bounded. Used where a pass must not raise."""
    environ = os.environ if environ is None else environ
    message = " ".join(str(exc).split())
    try:
        names = {doc.get("token_env") for doc in kit.session.actions.values() if isinstance(doc, dict)}
    except Exception:
        names = set()
    for name in sorted(n for n in names if isinstance(n, str)):
        token = environ.get(name)
        if token:
            message = message.replace(token, "***")
    return f"settle: {type(exc).__name__}: {message[:300]}"


def _get(target: Published, token: str, path: str, timeout: float):
    url = target.api + path
    try:
        guard_request(url)
    except Rejected as exc:
        raise PollFailed(exc.code) from None
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with HTTP.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise PollFailed(f"GitHub answered {response.status}")
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PollFailed(f"GitHub answered {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PollFailed(f"network failure ({type(reason).__name__})") from None
    except http.client.HTTPException as exc:
        raise PollFailed(f"network failure ({type(exc).__name__})") from None
    if len(raw) > MAX_BYTES:
        raise PollFailed("response too large")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise PollFailed("unreadable response") from None


def _login(user) -> str | None:
    """The login of a user object; None for a null user (a deleted account). Anything else is a shape error."""
    if user is None:
        return None
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not _LOGIN.match(login):
        raise PollFailed("unexpected login in the response")
    return login


def _int(value) -> bool:
    return type(value) is int


def _optional_time(value) -> str | None:
    """A time GitHub may leave null; a present one is checked like any other."""
    return None if value is None else _time(value)


def _time(value) -> str:
    if not isinstance(value, str) or not _TIME.match(value) or metrics.parse_time(value) is None:
        raise PollFailed("unexpected time in the response")
    return value


def _reviews(target: Published, token: str, timeout: float) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        found = _get(target, token, f"/repos/{target.repository}/pulls/{target.number}/reviews"
                                    f"?per_page={PER_PAGE}&page={page}", timeout)
        if not isinstance(found, list):
            raise PollFailed("unexpected review listing")
        rows += found
        if len(found) < PER_PAGE:
            return rows
    raise PollFailed(f"more than {PER_PAGE * MAX_PAGES} reviews")


def _review_value(target: Published, review) -> dict | None:
    """``pr_review`` for one submitted review; None for a pending one (not submitted, not GitHub's record yet)."""
    if not isinstance(review, dict) or not _int(review.get("id")) or review["id"] <= 0:
        raise PollFailed("unexpected review in the response")
    if not isinstance(review.get("state"), str):
        raise PollFailed("unexpected review state in the response")
    state = review["state"].lower()
    if state == "pending":
        return None
    if state not in REVIEW_STATES:
        raise PollFailed("unexpected review state in the response")
    return {"number": target.number, "review_id": review["id"], "state": state,
            "reviewer": _login(review.get("user")), "at": _time(review.get("submitted_at"))}


def _recorded(events: list[dict], key: str, number: int) -> list[dict]:
    return [value for value in (metrics.fact(e, key) for e in events)
            if value is not None and str(value.get("number")) == str(number)]


def poll(kit, target: Published, *, environ: Mapping[str, str] | None = None,
         timeout: float = TIMEOUT_SECONDS) -> Settled:
    """One pull request: read GitHub, then record what is new. Reads everything before writing anything."""
    events = kit.session.history(target.thread)
    if _recorded(events, metrics.PR_MERGED, target.number):
        return Settled(target.thread, target.number, "merged")
    environ = os.environ if environ is None else environ
    token = environ.get(target.token_env)
    if not token:
        return Settled(target.thread, target.number, "unknown", error=f"${target.token_env} is not set")
    try:
        pull = _get(target, token, f"/repos/{target.repository}/pulls/{target.number}", timeout)
        if not isinstance(pull, dict) or not _int(pull.get("number")) or pull["number"] != target.number:
            raise PollFailed("the response is not the pull request asked for")
        head = pull.get("head")
        if not isinstance(head, dict) or not isinstance(head.get("ref"), str):
            raise PollFailed("unexpected head in the response")
        if head["ref"] != target.branch:
            raise PollFailed(f"the pull request's head is not {target.branch}")
        if type(pull.get("merged")) not in (bool, type(None)) or not isinstance(pull.get("state"), str):
            raise PollFailed("unexpected pull request state in the response")
        merged_at = _optional_time(pull.get("merged_at"))
        reviews = [v for v in (_review_value(target, r) for r in _reviews(target, token, timeout)) if v is not None]
        merged = closed = None
        if pull.get("merged") is True or merged_at:
            commit = pull.get("merge_commit_sha")
            if not isinstance(commit, str) or not _SHA.match(commit):
                raise PollFailed("unexpected merge commit in the response")
            merged = {"number": target.number, "merged_at": _time(merged_at), "merge_commit": commit,
                      "merged_by": _login(pull.get("merged_by"))}
        elif pull.get("state") == "closed":
            closed = {"number": target.number, "closed_at": _time(pull.get("closed_at"))}
        elif pull.get("state") != "open":
            raise PollFailed("unexpected pull request state in the response")
    except PollFailed as exc:
        return Settled(target.thread, target.number, "unknown", error=str(exc))

    seen = {value.get("review_id") for value in _recorded(events, metrics.PR_REVIEW, target.number)}
    done = Settled(target.thread, target.number, "merged" if merged else "closed" if closed else "open")
    observe = kit.session.observe
    for value in reviews:
        if value["review_id"] in seen:
            continue
        seen.add(value["review_id"])
        observe(target.thread, metrics.PR_REVIEW, value, "github:" + target.repository, str(value["review_id"]),
                HOST, verified=True)
        done.recorded.append(f"{metrics.PR_REVIEW} {value['review_id']} {value['state']}")
    if merged is not None:
        observe(target.thread, metrics.PR_MERGED, merged, "github:" + target.repository, merged["merge_commit"],
                HOST, verified=True)
        done.recorded.append(f"{metrics.PR_MERGED} {merged['merged_at']}")
    if closed is not None and closed["closed_at"] not in {v.get("closed_at")
                                                          for v in _recorded(events, metrics.PR_CLOSED, target.number)}:
        observe(target.thread, metrics.PR_CLOSED, closed, "github:" + target.repository, closed["closed_at"],
                HOST, verified=True)
        done.recorded.append(f"{metrics.PR_CLOSED} {closed['closed_at']}")
    return done


def settle(kit, *, thread: str | None = None, environ: Mapping[str, str] | None = None,
           timeout: float = TIMEOUT_SECONDS) -> list[Settled]:
    """One pass: every published thread (or the one named by id or unique prefix), each pull request once."""
    if thread:
        ids = [kit._find(thread)]
    else:
        ids = [row["id"] for row in kit.threads() if row["state"] != "incomplete"]
    results = []
    for case_id in ids:
        for target in published(kit, case_id):
            results.append(poll(kit, target, environ=environ, timeout=timeout))
    return results
