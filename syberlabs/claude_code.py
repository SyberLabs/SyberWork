"""Claude Code as a proposer: the operator's own, unmodified ``claude -p`` inside the repair loop.

    syberlabs propose --claude-code --trusted-tree                      # the operator's login (subscription_local)
    syberlabs propose --claude-code --bare                              # an API key in the environment (api)
    syberlabs propose --claude-code --bare --mcp-config githits.json    # the same, with one more MCP server

``ClaudeCodeProvider`` is ``providers.RepairProvider`` with one call replaced: instead of a JSON adapter, each call
checks out the parent (the base, or the candidate being repaired) in a detached temporary worktree, runs Claude Code
there with the prompt ``adapters/anthropic_adapter.py: build_prompt`` writes for the same request (objective, scope,
and on a repair the failed checks' output), and reads the change back from that worktree with git: the paths that
differ from the parent, tracked or untracked, with their texts. The model's prose is never read as a change. The
host then registers, scopes and evaluates the candidate exactly as for any provider.

What the host records per call, parsed from Claude Code's ``stream-json`` output and never taken from the model:

- ``signal.model_usage``: input, output and cache tokens summed over the run's assistant messages (one per message
  id), with ``model`` when one model served the run; ``signal.model_usage_by_model`` the same sums per model;
- ``signal.harness_turns``: the result's ``num_turns``; ``signal.harness``: Claude Code's version, model, effort,
  credential source name (``apiKeySource``, never a credential) and MCP servers with their status, from ``init``;
- ``signal.tool_usage``: ``{web_search, web_fetch, githits, other: {name: n}}`` counted from the stream's
  ``tool_use`` and ``server_tool_use`` blocks (GitHits is any ``mcp__githits__*`` tool; web is ``WebSearch``,
  ``WebFetch`` and the server tools ``web_search`` and ``web_fetch``), bounded by ``providers.tool_usage``;
- ``signal.lane`` and ``signal.cost_basis`` (the credential lane, below) and ``signal.estimated_usd``, Claude Code's
  own ``total_cost_usd``, a client-side estimate that is never billed spend;
- one verified ``observed`` event per GitHits or web lookup, key ``lookup``, value ``{tool, query_digest,
  query_chars, at, ok}``: the SHA-256 of the query string and its length, never the text. Nothing else from the
  stream (prompt, tool inputs, tool results, the model's text) enters the journal.

Credential lanes (bring your own credential: SyberLabs never stores, pools or forwards a credential; the operator's
own Claude Code runs under their own login, or ``--bare`` with their API key). ``api`` (``--bare``, the default
there) is ``billed``: the tokens are priced under the contract's ``budget.prices`` through the host's meter, as an
adapter's usage is. ``provider_3p`` (Bedrock, Vertex, Foundry credentials) is ``estimated``: priced the same way, but
the bill is the cloud provider's. ``subscription_local`` (an inherited login, the default without ``--bare``) and
``seat_team_enterprise`` are ``none``: no per-call bill exists, so nothing is priced and the dollar budget does not
apply (turn, second and token budgets for those lanes are not built: ``spec/GAPS.md``).

Trust. A non-bare ``claude -p`` runs the hooks in the tree's ``.claude/settings.json`` and connects its ``.mcp.json``
servers with no trust prompt, so this provider refuses to run without ``--bare`` unless the operator declares the
tree trusted. It also always passes ``--strict-mcp-config`` (only ``--mcp-config`` servers connect) and
``--setting-sources user`` (no project or local settings from a tree a previous call may have written).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from syberlabs.canonical import canonical
from syberlabs.errors import Rejected
from syberlabs.gitspace import MAX_FILE_BYTES, Repo
from syberlabs.providers import (RepairProvider, USAGE_FIELDS, current_meter, model_usage, split_command,
                                 tool_usage)
from syberlabs.search import SearchSpace

LANES = ("api", "subscription_local", "seat_team_enterprise", "provider_3p")
COST_BASES = ("billed", "estimated", "none")
LANE_COST = {"api": "billed", "provider_3p": "estimated", "subscription_local": "none", "seat_team_enterprise": "none"}
BIN_ENV = "SYBERLABS_CLAUDE_BIN"  # the Claude Code executable (a path, or a command line); default ``claude`` on PATH
DEFAULT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
LOOKUP = "lookup"  # observed: one GitHits or web lookup, versioned ``<search>.<call>.<n>``
WEB_TOOLS = {"WebSearch": "web_search", "web_search": "web_search", "WebFetch": "web_fetch", "web_fetch": "web_fetch"}
GITHITS = "mcp__githits__"
HARNESS_NOTE = ("Make the change by editing files in the current directory. The host reads the result from this "
                "working tree with git and runs its own checks. Do not commit.")
# A signing seed has no business in a model's process; a subscription run must not silently bill an API key.
DROPPED_ENV = ("SYBERLABS_CERTIFICATE_SEED",)
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_NAMES = 8


def _short(value, limit: int = 64) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() else None


def _count(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def claude_executable() -> list[str]:
    """``SYBERLABS_CLAUDE_BIN`` (an executable's path, or a command line such as ``python stand_in.py``), else
    ``claude`` on PATH. The binary is the operator's own; this module never downloads or modifies one."""
    named = os.environ.get(BIN_ENV, "").strip()
    if named:
        return [named] if Path(named).is_file() else split_command(named)
    found = shutil.which("claude")
    if not found:
        raise Rejected("provider_unavailable", f"Claude Code is not on PATH; install it or set {BIN_ENV}")
    return [found]


def prompt_builder() -> Callable[[dict, Path], str]:
    """``adapters/anthropic_adapter.py: build_prompt``, the one prompt the model adapters use. The adapter lives in
    the repository beside the package, not in the wheel, so it is imported from the checkout (``spec/GAPS.md``)."""
    try:
        from adapters.anthropic_adapter import build_prompt
        return build_prompt
    except ImportError:
        pass
    path = Path(__file__).resolve().parents[1] / "adapters" / "anthropic_adapter.py"
    spec = importlib.util.spec_from_file_location("syberlabs_anthropic_adapter", path) if path.is_file() else None
    if spec is None or spec.loader is None:
        raise Rejected("provider_unavailable", "adapters/anthropic_adapter.py (build_prompt) is not beside this package")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_prompt


def parse_tools(text: str) -> list[str]:
    """``--allowed-tools`` as a list: comma or space separated, as Claude Code accepts it."""
    return [part for part in text.replace(",", " ").split() if part]


def query_text(tool_input) -> str:
    """The query of a lookup: ``query`` (WebSearch, GitHits search), else ``url`` (WebFetch), else the whole input as
    canonical JSON. Only its digest and length are recorded."""
    if isinstance(tool_input, dict):
        for key in ("query", "url"):
            if isinstance(tool_input.get(key), str):
                return tool_input[key]
    return canonical(tool_input if tool_input is not None else {})


def is_lookup(name: str) -> bool:
    return name in WEB_TOOLS or name.startswith(GITHITS)


class Transcript:
    """What the host reads from one run's ``stream-json`` lines. ``lines`` are ``(at, text)``: ``at`` is the host's
    UTC time when the line arrived (the stream carries no time of its own)."""

    def __init__(self, lines: Iterable[tuple[str, str]]):
        import json

        self.init: dict = {}
        self.result: dict | None = None
        self.messages: dict[str, dict] = {}  # assistant message id -> {model, usage}
        self.uses: dict[str, dict] = {}      # tool_use id -> {name, input, at}
        self.order: list[str] = []
        self.results: dict[str, bool] = {}   # tool_use id -> ok
        anonymous = 0
        for at, text in lines:
            try:
                event = json.loads(text)
            except ValueError:
                continue  # not a stream line (a warning a wrapper printed); never an instruction
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                self.init = event
            elif kind == "result":
                self.result = event
            elif kind == "assistant" and isinstance(event.get("message"), dict):
                message = event["message"]
                key = message.get("id") if isinstance(message.get("id"), str) else None
                if key is None:
                    anonymous += 1
                    key = f"anonymous-{anonymous}"
                # Claude Code streams one event per content block, each repeating the message's usage: one per id.
                self.messages[key] = {"model": message.get("model"), "usage": message.get("usage")}
                for block in message.get("content") or []:
                    if (isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use")
                            and isinstance(block.get("name"), str) and isinstance(block.get("id"), str)
                            and block["id"] not in self.uses):
                        self.uses[block["id"]] = {"name": block["name"], "input": block.get("input"), "at": at}
                        self.order.append(block["id"])
                    elif isinstance(block, dict) and block.get("type", "").endswith("_tool_result"):
                        self._result(block)  # a server tool's result comes back inside the assistant message
            elif kind == "user" and isinstance(event.get("message"), dict):
                for block in event["message"].get("content") or []:
                    if isinstance(block, dict) and block.get("type", "").endswith("tool_result"):
                        self._result(block)

    def _result(self, block: dict) -> None:
        key = block.get("tool_use_id")
        if not isinstance(key, str):
            return
        content = block.get("content")
        failed = block.get("is_error") is True or (isinstance(content, dict) and str(content.get("type", "")).endswith("_error"))
        self.results[key] = not failed

    def usage_by_model(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for message in self.messages.values():
            usage = message["usage"]
            if not isinstance(usage, dict):
                continue
            model = _short(message.get("model")) or "unnamed"
            row = found.setdefault(model, {key: 0 for key in USAGE_FIELDS})
            for key in USAGE_FIELDS:
                row[key] += _count(usage.get(key))
        return dict(sorted(found.items())[:MAX_NAMES])

    def usage(self) -> dict | None:
        """The run's tokens summed over models, with ``model`` when one model served it; reduced as an adapter's."""
        by_model = self.usage_by_model()
        if not by_model:
            return None
        total = {key: sum(row[key] for row in by_model.values()) for key in USAGE_FIELDS}
        if len(by_model) == 1 and next(iter(by_model)) != "unnamed":
            total["model"] = next(iter(by_model))
        stop = _short((self.result or {}).get("subtype"))
        if stop:
            total["stop_reason"] = stop
        return model_usage({"usage": total})

    def tool_usage(self) -> dict | None:
        counts = {"web_search": 0, "web_fetch": 0, "githits": 0}
        other: dict[str, int] = {}
        for key in self.order:
            name = self.uses[key]["name"]
            if name in WEB_TOOLS:
                counts[WEB_TOOLS[name]] += 1
            elif name.startswith(GITHITS):
                counts["githits"] += 1
            else:
                other[name] = other.get(name, 0) + 1
        return tool_usage({"tool_usage": {**counts, "other": other}})

    def lookups(self) -> list[dict]:
        """``{tool, query_digest, query_chars, at, ok}`` per GitHits or web lookup, in stream order. ``ok`` is false
        when the stream carries no successful result for it."""
        found = []
        for key in self.order:
            use = self.uses[key]
            if not is_lookup(use["name"]):
                continue
            query = query_text(use["input"])
            found.append({"tool": use["name"][:64], "query_digest": hashlib.sha256(query.encode()).hexdigest(),
                          "query_chars": len(query), "at": use["at"], "ok": self.results.get(key) is True})
        return found

    def harness(self) -> dict:
        servers = [f"{s.get('name')}:{s.get('status')}" for s in self.init.get("mcp_servers") or []
                   if isinstance(s, dict) and _short(s.get("name"), 48) and _short(s.get("status"), 15)]
        found = {"name": "claude-code", "version": _short(self.init.get("claude_code_version")),
                 "model": _short(self.init.get("model")), "effort": _short(self.init.get("effort")),
                 "auth_source": _short(self.init.get("apiKeySource")), "mcp_servers": sorted(servers)[:MAX_NAMES]}
        return {k: v for k, v in found.items() if v not in (None, [])}


def read_changes(tree: Path, parent: str) -> tuple[dict[str, str | None], list[str]]:
    """The run's change: every path that differs from ``parent`` in the worktree (committed or not, tracked or
    untracked and not ignored), with its text; None for a deleted path. Binary, non-UTF-8 and oversized files are
    left out and named, never re-encoded."""
    repo = Repo(tree)
    changes, skipped = {}, []
    for path in repo.working_changes(parent):
        file = repo.root / path
        if not file.exists():
            changes[path] = None
            continue
        if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
            skipped.append(path)
            continue
        data = file.read_bytes()
        try:
            if b"\0" in data[:8000]:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "binary")
            changes[path] = data.decode()
        except UnicodeDecodeError:
            skipped.append(path)
    return changes, skipped


def run_stream(argv: Sequence[str], prompt: str, *, cwd: Path, env: dict, timeout: int) -> tuple[int, list, str]:
    """Run Claude Code with ``prompt`` on stdin (not in argv: no length limit, not in a process listing) and collect
    its stdout lines with the host's arrival time. Returns ``(exit code, lines, stderr head)``."""
    lines: list[tuple[str, str]] = []
    size = [0]
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=str(cwd),
                                    env=env, start_new_session=hasattr(os, "setsid"))
        except OSError as exc:
            raise Rejected("provider_unavailable", f"could not start {argv[0]!r}: {exc.strerror}") from None

        def read() -> None:
            for raw in proc.stdout:
                size[0] += len(raw)
                if size[0] > MAX_STREAM_BYTES:
                    break
                lines.append((now_utc(), raw.decode("utf-8", errors="replace").strip()))
            proc.stdout.close()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if time.monotonic() > deadline or size[0] > MAX_STREAM_BYTES:
                proc.kill()
                proc.wait()
                reader.join(5)
                raise Rejected("provider_timeout", "Claude Code exceeded its time or output limit")
            time.sleep(0.05)
        reader.join(30)
        err.seek(0)
        stderr = err.read(400).decode(errors="replace")
    return proc.returncode, lines, stderr


class ClaudeCodeProvider(RepairProvider):
    """The repair loop with Claude Code as the call. ``max_calls=1`` is one shot. See the module docstring."""

    operators = ("patch",)

    def __init__(self, repo: Repo | str | Path, *, bare: bool = False, trusted_tree: bool = False,
                 mcp_config: str | Path | None = None, allowed_tools: Sequence[str] | None = None,
                 max_turns: int | None = None, lane: str | None = None, executable: Sequence[str] | None = None,
                 name: str = "claude-code", revision: str = "1", timeout: int = 1800, max_calls: int | None = None,
                 observe: Callable[[str, dict, str], object] | None = None):
        lane = lane or ("api" if bare else "subscription_local")
        if lane not in LANES:
            raise Rejected("invalid_provider", "lane must be one of " + ", ".join(LANES))
        if bare and lane in ("subscription_local", "seat_team_enterprise"):
            raise Rejected("invalid_provider", "--bare never reads a Claude login; its lane is api or provider_3p")
        if not bare and not trusted_tree:
            raise Rejected("untrusted_tree",
                           "refusing to run Claude Code without --bare on a tree not declared trusted: a non-bare "
                           "`claude -p` runs the project's hooks and connects its .mcp.json servers with no trust "
                           "prompt. Pass --trusted-tree if you trust this repository and every tree the model will "
                           "write in it, or use --bare (an API key in the environment)")
        tools = list(DEFAULT_TOOLS if allowed_tools is None else allowed_tools)
        if not tools or any(not isinstance(t, str) or not t.strip() or "," in t for t in tools):
            raise Rejected("invalid_provider", "allowed tools must be a non-empty list of tool names")
        if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
            raise Rejected("invalid_provider", "max_turns must be a positive integer")
        if mcp_config is not None:
            mcp_config = Path(mcp_config).resolve()
            if not mcp_config.is_file():
                raise Rejected("invalid_provider", f"--mcp-config {mcp_config} is not a file")
        claude = list(executable) if executable else claude_executable()
        super().__init__(claude, name=name, revision=revision, timeout=timeout, max_calls=max_calls)
        self.repo = repo if isinstance(repo, Repo) else Repo(repo)
        self.bare, self.trusted_tree, self.lane, self.cost_basis = bare, trusted_tree, lane, LANE_COST[lane]
        self.mcp_config, self.allowed_tools, self.max_turns = mcp_config, tools, max_turns
        self.observe = observe
        self.lookups: list[dict] = []  # every lookup of every call, as recorded (digests, never query text)

    def command(self) -> list[str]:
        """The argv of every call. Two providers that differ only in ``mcp_config`` differ only in the last two items."""
        argv = [*self.argv, "-p"] + (["--bare"] if self.bare else [])
        argv += ["--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
                 "--allowedTools", ",".join(self.allowed_tools), "--setting-sources", "user", "--strict-mcp-config",
                 "--no-session-persistence", "--append-system-prompt", HARNESS_NOTE]
        if self.max_turns is not None:
            argv += ["--max-turns", str(self.max_turns)]
        if self.mcp_config is not None:
            argv += ["--mcp-config", str(self.mcp_config)]
        return argv

    def environment(self) -> dict:
        """The caller's environment, narrowed: no signing seed, and no API key on a lane that is not billed by key."""
        env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
        if self.lane in ("subscription_local", "seat_team_enterprise"):
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def _call(self, space: SearchSpace, request: dict, parents: list[str]):
        meter = current_meter()
        if meter is not None:
            meter.permit()
        commit = space.candidate(parents[0]).commit if parents else space.base
        build_prompt = prompt_builder()
        with self.repo.worktree(commit) as tree:
            prompt = build_prompt(request, tree)
            code, lines, stderr = run_stream(self.command(), prompt, cwd=tree, env=self.environment(),
                                             timeout=self.timeout)
            transcript = Transcript(lines)
            subtype = (transcript.result or {}).get("subtype")
            if transcript.result is None or (code != 0 and subtype != "error_max_turns"):
                raise Rejected("provider_failed", f"exit {code}: " + (_short(subtype) or stderr[:300]))
            changes, skipped = read_changes(tree, commit)
        usage, by_model = transcript.usage(), transcript.usage_by_model()
        if meter is not None:
            if self.cost_basis in ("billed", "estimated") and by_model:
                for model, row in by_model.items():  # each model priced at its own rate
                    meter.observe({**row, **({"model": model} if model != "unnamed" else {}), "host_observed": True})
            else:
                meter.observe(None)  # a call was made; under this lane it has no per-call bill to price
        version = getattr(space, "search_id", "s0")
        for number, lookup in enumerate(transcript.lookups(), 1):
            self.lookups.append(lookup)
            if self.observe is not None:
                self.observe(LOOKUP, lookup, f"{version}.{self.calls}.{number}")
        signal = {"lane": self.lane, "cost_basis": self.cost_basis, "harness": transcript.harness()}
        turns = transcript.result.get("num_turns")
        if type(turns) is int and turns >= 0:
            signal["harness_turns"] = turns
        if _short(subtype):
            signal["harness_stop"] = subtype
        estimate = transcript.result.get("total_cost_usd")
        if isinstance(estimate, (int, float)) and not isinstance(estimate, bool) and estimate >= 0:
            signal["estimated_usd"] = round(float(estimate), 6)
        if len(by_model) > 1:
            signal["model_usage_by_model"] = by_model
        if skipped:
            signal["skipped_paths"] = len(skipped)
        response = {"usage": usage, "tool_usage": transcript.tool_usage()}
        response["candidates"] = [{"changes": changes, "message": "", "signal": signal}] if changes else []
        return response
