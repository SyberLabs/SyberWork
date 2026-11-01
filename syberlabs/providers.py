"""Built-in search providers. Each only proposes; the host records and judges.

- ``PatchProvider`` submits one given edit: a person's patch, or the files a
  coding tool already changed in the working tree.
- ``FunctionProvider`` wraps a Python callable that receives the search space.
- ``CommandProvider`` runs an external program with a JSON request on stdin
  and reads JSON candidates from stdout. This is the model seam: an adapter
  for any model provider can be a command, and SyberLabs imports no model SDK.
- ``RepairProvider`` runs the same command in a loop: one proposal, then, while
  the host's evaluation of the last candidate did not pass, another request that
  carries that candidate's check results and file texts as ``feedback``. It is
  the factory's default proposer path (owner decision of 2026-10-23).
- ``syberlabs.claude_code.ClaudeCodeProvider`` is that loop with the operator's
  own Claude Code as the call: ``claude -p`` in a worktree of the parent, the
  change read back with git, usage and lookups parsed from its stream.

``run_json`` is also where the host observes spend. Inside ``metered(meter)``
every call asks the meter's ``permit()`` before the command runs and hands the
adapter's reported ``usage`` to ``measure()`` afterwards, stamped
``host_observed``. The Build Thread installs its ledger there, so only usage
that crossed this boundary is ever priced; a ``model_usage`` a provider writes
into a candidate's ``signal`` is the provider's declaration and is not.

An adapter's response may also carry ``tool_usage`` beside ``usage``: ``{web_search: n, web_fetch: n, githits: n,
other: {name: n}}``, the lookups the call made. ``tool_usage`` reduces it (integer counts, at most 16 other names
of at most 64 characters) and the command providers copy it into the candidate's ``signal.tool_usage``; an adapter
that declares none leaves it absent, so ``syberlabs.metrics`` reports the lookups as unknown, not as zero.
"""

from __future__ import annotations

import contextvars
import json
import os
import random
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from syberlabs.errors import Rejected
from syberlabs.search import SearchSpace

PROTOCOL = "syberlabs.search/v0alpha1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# The host's usage meter for the search running in this context, or None.
_METER: contextvars.ContextVar = contextvars.ContextVar("syberlabs.usage_meter", default=None)


@contextmanager
def metered(meter):
    """Route every ``run_json`` call in the block through ``meter``.

    ``meter.permit()`` runs before each command starts and may raise to refuse
    the purchase; ``meter.measure(usage)`` runs after each successful call with
    the adapter's reduced usage stamped ``host_observed: True``, or None when
    the adapter reported none.
    """
    token = _METER.set(meter)
    try:
        yield meter
    finally:
        _METER.reset(token)


def current_meter():
    """The usage meter of the search running in this context, or None. A provider that buys model calls without
    ``run_json`` (``syberlabs.claude_code``) asks it ``permit()`` before a call and hands it ``measure(usage)`` after."""
    return _METER.get()


def refuse(space, reason: str, operator: str, parents=(), usage=None) -> None:
    """Ask the space to record a proposal the provider discards itself. A space without ``refuse`` records nothing."""
    found = getattr(space, "refuse", None)
    if callable(found):
        found(reason, operator=operator, parents=list(parents), usage=usage)


def split_command(text: str) -> list[str]:
    """Split a command line. On Windows, backslashes in paths are kept."""
    return shlex.split(text, posix=(os.name != "nt"))


class PatchProvider:
    operators = ("patch",)

    def __init__(self, changes: Mapping[str, str | None], message: str = "", *, name: str = "patch",
                 revision: str = "1", signal: Mapping | None = None):
        self.changes, self.message, self.signal = dict(changes), message, signal
        self.name, self.revision = name, revision

    def search(self, space: SearchSpace) -> Sequence[str]:
        return [space.submit(self.changes, operator="patch", message=self.message, signal=self.signal).id]


class FunctionProvider:
    def __init__(self, fn: Callable[[SearchSpace], Sequence[str] | None], *, name: str, revision: str,
                 operators: Sequence[str] = ("patch",)):
        self.fn, self.name, self.revision, self.operators = fn, name, revision, tuple(operators)

    def search(self, space: SearchSpace) -> Sequence[str] | None:
        return self.fn(space)


class CommandProvider:
    """Run ``argv`` once. It inherits the caller's environment, so it can read its own API keys.

    Request (stdin): ``{protocol, objective, base, scope, budget, context, files}``.
    Response (stdout): ``{"candidates": [{"changes": {path: text or null}, "message": str,
    "signal": object or null}], "recommended": [index, ...]}``.
    """

    operators = ("patch",)

    def __init__(self, argv: Sequence[str], *, name: str = "command", revision: str = "1", timeout: int = 300,
                 cwd: str | Path | None = None):
        if not argv or any(not isinstance(arg, str) for arg in argv):
            raise Rejected("invalid_provider", "argv must be a non-empty list of strings")
        self.argv, self.name, self.revision, self.timeout, self.cwd = list(argv), name, revision, timeout, cwd
        self.calls, self.usage = 0, []

    def search(self, space: SearchSpace) -> Sequence[str]:
        budget = space.remaining()
        request = {
            "protocol": PROTOCOL, "objective": space.objective, "base": space.base, "scope": list(space.scope),
            "budget": {"max_candidates": budget.max_candidates, "max_seconds": budget.max_seconds},
            "context": [dict(item) for item in space.context],
            "files": space.files()[:2000],
        }
        self.calls += 1
        response = run_json(self.argv, request, timeout=self.timeout, cwd=self.cwd)
        usage, tools = model_usage(response), tool_usage(response)
        if usage:
            self.usage.append(usage)
        if not isinstance(response, dict) or not isinstance(response.get("candidates"), list):
            raise Rejected("provider_shape", "the command must print {\"candidates\": [...]}")
        submitted = []
        for index, item in enumerate(response["candidates"]):
            if index >= budget.max_candidates:
                refuse(space, "limit", "patch", ["base"], usage=usage)  # beyond this search's candidate budget: never submitted
                continue
            if (not isinstance(item, dict) or not isinstance(item.get("changes"), dict)
                    or any(not isinstance(k, str) or not (v is None or isinstance(v, str)) for k, v in item["changes"].items())):
                refuse(space, "unparsable", "patch", ["base"], usage=usage)
                raise Rejected("provider_shape", "each candidate needs changes: {path: text or null}")
            message = item.get("message") if isinstance(item.get("message"), str) else ""
            signal = with_usage(item.get("signal"), usage, tools)
            submitted.append(space.submit(item["changes"], operator="patch", message=message[:500], signal=signal).id)
        chosen = response.get("recommended", [])
        if not isinstance(chosen, list):
            return []
        return [submitted[i] for i in chosen if type(i) is int and 0 <= i < len(submitted)]


USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
TOOL_FIELDS = ("web_search", "web_fetch", "githits")
MAX_TOOL_NAMES = 16
MAX_TOOL_COUNT = 1_000_000


def model_usage(response) -> dict | None:
    """The adapter's reported token usage for one call, reduced to known fields. A claim, not a measurement."""
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    found = {key: usage[key] for key in USAGE_FIELDS if type(usage.get(key)) is int and usage[key] >= 0}
    for key in ("model", "stop_reason"):
        if isinstance(usage.get(key), str) and len(usage[key]) <= 64:
            found[key] = usage[key]
    if usage.get("simulated") is True:
        found["simulated"] = True
    return found or None


def tool_usage(response) -> dict | None:
    """The adapter's declared lookups for one call, bounded: integer counts, at most 16 ``other`` names of at most
    64 characters (sorted, the rest dropped). None when the response declares none. A claim, not a measurement."""
    tools = response.get("tool_usage") if isinstance(response, dict) else None
    if not isinstance(tools, dict):
        return None

    def count(value) -> bool:
        return type(value) is int and 0 <= value <= MAX_TOOL_COUNT

    found = {key: tools[key] for key in TOOL_FIELDS if count(tools.get(key))}
    other = tools.get("other")
    if isinstance(other, dict):
        names = sorted((k, v) for k, v in other.items() if isinstance(k, str) and 0 < len(k) <= 64 and k.isprintable()
                       and count(v))
        if names:
            found["other"] = dict(names[:MAX_TOOL_NAMES])
    return found or None


def with_usage(signal, usage: dict | None, tools: dict | None) -> dict | None:
    """A candidate's ``signal``: the item's own object, with the call's declared ``model_usage`` and ``tool_usage``.

    The two keys are the call's, not the item's: an item's own ``tool_usage`` is dropped, so only a bounded
    declaration from the response reaches the record, and absent stays absent."""
    found = {k: v for k, v in signal.items() if k != "tool_usage"} if isinstance(signal, dict) else {}
    if usage:
        found["model_usage"] = usage
    if tools:
        found["tool_usage"] = tools
    return found or (None if not isinstance(signal, dict) else found)


def run_json(argv: Sequence[str], payload: dict, *, timeout: int, cwd: str | Path | None = None):
    """Run a command with ``payload`` as JSON on stdin; return its stdout parsed as JSON.

    Bounded by ``timeout`` seconds and 4 MB of output. The command inherits the
    caller's environment. Failures are ``Rejected`` with a ``provider_*`` code.
    """
    meter = _METER.get()
    if meter is not None:
        meter.permit()
    request = json.dumps(payload).encode()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=cwd,
                                    env=dict(os.environ), start_new_session=hasattr(os, "setsid"))
        except OSError as exc:
            raise Rejected("provider_unavailable", f"could not start {argv[0]!r}: {exc.strerror}") from None
        try:
            proc.stdin.write(request)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if time.monotonic() > deadline or os.fstat(out.fileno()).st_size > MAX_RESPONSE_BYTES:
                proc.kill()
                proc.wait()
                raise Rejected("provider_timeout", "the provider command exceeded its time or output limit")
            time.sleep(0.02)
        if proc.returncode != 0:
            err.seek(0)
            raise Rejected("provider_failed", f"exit {proc.returncode}: {err.read(400).decode(errors='replace')}")
        out.seek(0)
        data = out.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise Rejected("provider_timeout", "the provider response exceeded its output limit")
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        raise Rejected("provider_shape", "the command did not print JSON") from None
    if meter is not None:
        usage = model_usage(parsed)
        meter.measure({**usage, "host_observed": True} if usage else None)
    return parsed


def repair_feedback(space: SearchSpace, candidate: str) -> dict | None:
    """The host's evaluation of ``candidate`` and its current file texts as a search request's ``feedback``, or None.

    ``{"candidate": id, "files": {path: text}, "checks": [{name, state, exit_code, duration_ms, output_tail}, ...]}``:
    the object the repair arm of ``benchmarks/model_vs_patch.py`` (``Patches.search``) puts on its next request after
    a failed attempt and ``adapters/anthropic_adapter.py: build_prompt`` reads under the search protocol. ``checks``
    is ``syberlabs.evolve.mutation_feedback``'s: the check results exactly as the host recorded them in
    ``candidate_evaluated``. ``files`` is the text of every path the candidate changed against the base, read from the
    candidate's tree; a deleted or unreadable path is left out. None when the host has not evaluated the candidate,
    so a reader can tell "no evaluation" from "an evaluation that passed". Pure: it reads the space and runs nothing.
    """
    from syberlabs.evolve import mutation_feedback  # evolve imports this module; the shared check shape lives there

    found = mutation_feedback(space, candidate)
    if found is None:
        return None
    files = {}
    for path in space.candidate(candidate).changed_paths:
        text = space.read(candidate, path)
        if text is not None:
            files[path] = text
    return {"candidate": found["candidate"], "files": files, "checks": found["checks"]}


class RepairProvider:
    """One proposal, then repairs directed by the host's check results, until one passes or the budget stops it.

    The repair loop of ``benchmarks/model_vs_patch.py`` (``Patches.search``) as a kernel provider, and the factory's
    default proposer path since the owner's decision of 2026-10-23 (E3 v3 and v4, Claude Sonnet 5.5, K = 6 and 12:
    it beat every evolve arm on solves and on parts per dollar at every budget tested, and ``single`` on solves).

    Each call runs ``argv`` under the search protocol with the request ``CommandProvider`` sends (``protocol``,
    ``objective``, ``base``, ``scope``, ``budget``, ``context``, ``files``) plus ``seed``, and, once a candidate has
    been evaluated and did not pass, ``feedback`` = ``repair_feedback(space, candidate)``: that candidate's recorded
    check results and its current file texts. The answer is the search protocol's; one candidate is taken per call
    (the first item; the rest are refused ``limit``), registered with the usage the adapter declared, and evaluated
    by the host at once. The loop ends on the first candidate that passes (``passing``), after ``max_calls`` calls
    (``calls``; the default is the search's candidate budget, ``evolution.budget.max_candidates`` less the thread's
    candidates), or when the budget has no room for another call or evaluation (``budget``: candidates, evaluations,
    seconds, and dollars under a priced contract), a clean stop with the same meaning as the evolutionary provider's
    ``Stop("budget")``; the host writes ``stopped: "budget_exhausted"`` when the dollars ran out, and ``budget.search``
    and ``budget.promotion`` judge the candidates as they do for every provider. The recommendation is the passing
    candidate, else the best partial by ``Evaluation.score`` (an unevaluated candidate scores ``(0, 0)``; one outside
    the contract's scope is registered but never recommended and never a parent).

    Every proposal leaves a record: the host refuses an unchanged tree, a duplicate, an unreadable edit or a
    zero-usage answer itself (``proposal_refused``) and the loop goes on with the same parent; this provider refuses
    an answer with no ``candidates`` list (``unparsable``), an empty one (``no_change``), a malformed first item
    (``unparsable``) and the items after the first (``limit``), and goes on; a command that fails is ``provider_error``
    and ends the search with its code, as for ``CommandProvider``. ``trace`` holds one entry per call,
    ``{call, parent, feedback, outcome, candidate, score}`` (``outcome``: ``passed``, ``failed``, ``out_of_scope``,
    ``unevaluated``, ``refused:<reason>`` or ``provider_error``), then ``{stopped, calls, refused}``.

    ``feedback_filter`` is an optional host hook: a callable that receives each ``feedback`` object before it is
    put on a request and returns the object to send, or None to send no ``feedback`` at all. The default, None,
    sends ``repair_feedback`` unchanged, so every caller that does not set it gets the requests it always got.
    ``syberlabs.eval`` sets it to enforce an eval's ``hidden_feedback`` mode (``docs/EVAL.md``).
    """

    operators = ("patch",)

    def __init__(self, argv: Sequence[str], *, name: str = "repair", revision: str = "1", timeout: int = 300,
                 cwd: str | Path | None = None, max_calls: int | None = None,
                 feedback_filter: Callable[[dict], dict | None] | None = None):
        if not argv or any(not isinstance(arg, str) for arg in argv):
            raise Rejected("invalid_provider", "argv must be a non-empty list of strings")
        if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
            raise Rejected("invalid_provider", "max_calls must be a positive integer, or None for the candidate budget")
        self.argv, self.name, self.revision, self.timeout, self.cwd = list(argv), name, revision, timeout, cwd
        self.max_calls = max_calls
        self.feedback_filter = feedback_filter
        self.calls, self.usage = 0, []
        self.trace: list[dict] = []

    @staticmethod
    def _room(space: SearchSpace) -> bool:
        """Every budget dimension is above zero, dollars included on a priced host, and nothing pending is over."""
        left = space.remaining()
        max_dollars = getattr(left, "max_dollars", None)
        return (left.max_candidates > 0 and left.max_evaluations > 0 and left.max_seconds > 0
                and (max_dollars is None or max_dollars > 0) and not getattr(space, "over_budget", False))

    def _call(self, space: SearchSpace, request: dict, parents: list[str]):
        """One model call: the search-protocol ``request`` in, the protocol's response out. ``parents`` is the
        candidate being repaired (empty on the first call). A subclass may run something other than ``argv``
        (``syberlabs.claude_code.ClaudeCodeProvider``); it raises ``Rejected`` with a ``provider_*`` code on failure."""
        return run_json(self.argv, request, timeout=self.timeout, cwd=self.cwd)

    def _request(self, space: SearchSpace, rng: random.Random, previous: str | None) -> dict:
        budget = space.remaining()
        request = {
            "protocol": PROTOCOL, "objective": space.objective, "base": space.base, "scope": list(space.scope),
            "budget": {"max_candidates": budget.max_candidates, "max_seconds": budget.max_seconds},
            "context": [dict(item) for item in space.context],
            "files": space.files()[:2000],
            "seed": rng.randrange(2**31),
        }
        feedback = repair_feedback(space, previous) if previous is not None else None
        if feedback is not None and self.feedback_filter is not None:
            feedback = self.feedback_filter(feedback)
        if feedback is not None:
            request["feedback"] = feedback
        return request

    @staticmethod
    def _parsed(item) -> bool:
        return (isinstance(item, dict) and isinstance(item.get("changes"), dict)
                and all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in item["changes"].items()))

    def search(self, space: SearchSpace) -> Sequence[str]:
        rng = random.Random(space.seed)
        limit = space.budget.max_candidates if self.max_calls is None else self.max_calls
        self.trace = []
        previous: str | None = None  # the last evaluated candidate: the parent of the next repair
        best: tuple[str, tuple[int, int]] | None = None
        stopped, made_calls = "calls", 0
        for call in range(1, limit + 1):
            if not self._room(space):
                stopped = "budget"
                break
            request = self._request(space, rng, previous)
            parents = [previous] if previous is not None else []
            entry = {"call": call, "parent": previous or "base", "feedback": "feedback" in request,
                     "outcome": None, "candidate": None, "score": None}
            self.trace.append(entry)
            self.calls, made_calls = self.calls + 1, made_calls + 1
            try:
                response = self._call(space, request, parents)
            except Rejected as exc:
                if exc.code.startswith("provider_"):
                    refuse(space, "provider_error", "patch", parents or ["base"])
                    entry["outcome"] = "provider_error"
                raise
            usage, tools = model_usage(response), tool_usage(response)
            if usage:
                self.usage.append(usage)
            items = response.get("candidates") if isinstance(response, dict) else None
            if not isinstance(items, list) or (items and not self._parsed(items[0])):
                refuse(space, "unparsable", "patch", parents or ["base"], usage=usage)
                entry["outcome"] = "refused:unparsable"
                continue
            if not items:
                refuse(space, "no_change", "patch", parents or ["base"], usage=usage)
                entry["outcome"] = "refused:no_change"
                continue
            for _ in items[1:]:
                refuse(space, "limit", "patch", parents or ["base"], usage=usage)  # one candidate per call: never submitted
            item = items[0]
            message = item.get("message") if isinstance(item.get("message"), str) else ""
            signal = with_usage(item.get("signal"), usage, tools)
            try:
                made = space.submit(item["changes"], parents=parents, operator="patch", message=message[:500], signal=signal)
            except Rejected as exc:
                if exc.code in ("no_change", "invalid_change", "duplicate", "zero_usage"):
                    entry["outcome"] = "refused:" + ("unparsable" if exc.code == "invalid_change" else exc.code)
                    continue  # the host recorded the refusal; the next call repairs the same parent
                raise
            entry["candidate"] = made.id
            if not made.in_scope:
                entry["outcome"], entry["score"] = "out_of_scope", [0, 0]
                continue
            if not self._room(space):
                entry["outcome"], entry["score"] = "unevaluated", [0, 0]
                best, stopped = best or (made.id, (0, 0)), "budget"
                break
            found = space.evaluate(made.id)
            score = found.score()
            entry["outcome"], entry["score"] = ("passed" if found.passed else "failed"), list(score)
            if best is None or score > best[1]:
                best = (made.id, score)
            if found.passed:
                stopped = "passing"
                break
            previous = made.id
        self.trace.append({"stopped": stopped, "calls": made_calls, "refused": dict(getattr(space, "refused", None) or {})})
        return [best[0]] if best else []
