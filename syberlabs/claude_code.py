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
- ``signal.guard``: the query guard's policy for the run, ``{version, max_lines, max_chars, policy_digest}`` (host
  policy, never contract data; ``policy_digest`` is the SHA-256 of the read-only ``policy.json`` the hook checks);
- ``signal.harness.installed_version``: what ``claude --version`` printed when the provider was built (below
  ``MIN_VERSION``, 2.1.295, the provider is refused: ``onFailure: "block"`` needs it);
- one verified ``observed`` event per GitHits or web lookup, key ``lookup``, value ``{tool, query_digest,
  query_chars, at, ok, guard}``: the SHA-256 of the query string and its length, never the text, and the query
  guard's decision. It is written through ``space.observe`` (``SearchSpace.observe``; ``GitSearchSpace`` records it
  with ``Session.observe``), so a provider built in Python keeps its lookups. Nothing else from the stream
  (prompt, tool inputs, tool results, the model's text) enters the journal.

The query guard (``syberlabs.query_guard``). Every call runs with ``--settings FILE``, a temporary settings document
the host writes in its own directory outside the worktree that holds one ``PreToolUse`` hook and nothing else: the
host's own ``query_guard.py`` by absolute path, matched to ``mcp__githits__*``, ``WebSearch`` and ``WebFetch``. It
blocks a lookup whose query carries a credential, an email, a URL with credentials, code (more than ``max_lines``
lines, a fence, more than ``max_chars`` characters) or a line copied from the worktree's tracked files, and writes
its decision (rule name, never text) beside itself with an HMAC under a random per-call key that only the
environment of Claude Code (and so of the hook it starts) holds. A blocked lookup is still a lookup: its record has
``ok: false`` and ``guard: <rule>``; an allowed one ``guard: "pass"``. A decision counts only when its MAC verifies
and its ``tool_use_id``, tool and query digest equal the stream's lookup exactly; a lookup without one is recorded
``guard: "unguarded"`` and stops the search (``provider_guard_inactive``): the guard did not run, or its record was
not the hook's, so the operator must know before another call. A call that fails or times out journals the lookups
its stream showed before the failure is raised.

Confinement. Every call passes ``--restricted`` (Claude Code 2.1.248 and later: the built-in file tools are
confined to the working directories, only managed settings and ``--settings`` load, and the built-in tools that
run commands or code, and ``WebFetch``, are removed unless ``--tools`` names them) and ``--tools`` with the
built-in names of the allowed tools, so the guard's directory, which is outside the worktree, is out of the file
tools' reach, and a code-running tool that could read the MAC key from the environment exists only when the
operator allows it by name.

Credential lanes (bring your own credential: SyberLabs never stores, pools or forwards a credential; the operator's
own Claude Code runs under their own login, or ``--bare`` with their API key). ``api`` (``--bare``, the default
there) is ``billed``: the tokens are priced under the contract's ``budget.prices`` through the host's meter, as an
adapter's usage is. ``provider_3p`` (Bedrock, Vertex, Foundry credentials) is ``estimated``: priced the same way, but
the bill is the cloud provider's. ``subscription_local`` (an inherited login, the default without ``--bare``) and
``seat_team_enterprise`` are ``none``: no per-call bill exists, so nothing is priced and the dollar budget does not
apply (turn, second and token budgets for those lanes are not built: ``spec/GAPS.md``).

Environment. On the login lanes (``subscription_local``, ``seat_team_enterprise``) every variable that names an
Anthropic or cloud credential or routes Claude Code to another endpoint or provider (``LOGIN_LANE_DROPPED_ENV``)
is removed, so a run recorded as unbilled cannot be billed elsewhere. ``--bare`` with ``ANTHROPIC_BASE_URL`` or a
third-party switch (``THIRD_PARTY_SWITCHES``) set must be ``--lane provider_3p``; ``api`` is refused.

Trust. A non-bare ``claude -p`` runs the hooks in the tree's ``.claude/settings.json``, connects its ``.mcp.json``
servers and reads its memory files (``CLAUDE.md``, ``CLAUDE.local.md``) with no trust prompt, so this provider
refuses to run without ``--bare`` unless the operator declares the tree trusted. It also always passes
``--strict-mcp-config`` (only ``--mcp-config`` servers connect) and ``--setting-sources user`` (no project or local
settings from a tree a previous call may have written). Memory files are still read from the working directory, so
a repair call whose parent candidate changed an instruction file against the thread's base (``CLAUDE.md``,
``CLAUDE.local.md``, ``AGENTS.md``, ``.mcp.json``, a path named ``.claude`` or anything under one, at any depth, or
a symbolic link whose target resolves to one of those) is refused ``instruction_files_changed`` before it runs: a
model must not write the next call's instructions. The base's own files are the operator's and are allowed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from syberlabs import query_guard
from syberlabs.checks import _stop
from syberlabs.errors import Rejected
from syberlabs.gitspace import MAX_FILE_BYTES, Repo
from syberlabs.providers import (RepairProvider, USAGE_FIELDS, current_meter, model_usage, refuse, split_command,
                                 tool_usage)
from syberlabs.query_guard import query_text
from syberlabs.search import SearchSpace

LANES = ("api", "subscription_local", "seat_team_enterprise", "provider_3p")
COST_BASES = ("billed", "estimated", "none")
LANE_COST = {"api": "billed", "provider_3p": "estimated", "subscription_local": "none", "seat_team_enterprise": "none"}
BIN_ENV = "SYBERLABS_CLAUDE_BIN"  # the Claude Code executable (a path, or a command line); default ``claude`` on PATH
DEFAULT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")  # no Bash: a shell could read the guard's key
MIN_VERSION = (2, 1, 295)  # ``onFailure: "block"`` on a command hook (code.claude.com/docs/en/hooks)
LOOKUP = "lookup"  # observed: one GitHits or web lookup, versioned ``<search>.<call>.<n>``
WEB_TOOLS = {"WebSearch": "web_search", "web_search": "web_search", "WebFetch": "web_fetch", "web_fetch": "web_fetch"}
GITHITS = "mcp__githits__"
HARNESS_NOTE = ("Make the change by editing files in the current directory. The host reads the result from this "
                "working tree with git and runs its own checks. Do not commit.")
# A signing seed has no business in a model's process; a subscription run must not silently bill an API key.
DROPPED_ENV = ("SYBERLABS_CERTIFICATE_SEED",)
LOGIN_LANE_DROPPED_ENV = (
    # Anthropic credentials and endpoints other than the login itself
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_WORKSPACE_ID",
    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    # third-party platforms: switches, endpoints, projects, regions, credentials
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_SKIP_*_AUTH",
    "ANTHROPIC_BEDROCK_*", "ANTHROPIC_VERTEX_*", "ANTHROPIC_FOUNDRY_*", "ANTHROPIC_AWS_*",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION", "CLOUD_ML_REGION", "VERTEX_REGION_*",
    "AWS_*", "GOOGLE_*", "GCLOUD_*", "AZURE_*",
    # another configuration directory (another login, apiKeyHelper, settings), and where requests are sent
    "CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
)
"""What a login lane (``subscription_local``, ``seat_team_enterprise``) never passes to Claude Code: every variable
Claude Code's environment reference (``code.claude.com/docs/en/env-vars``, read 2026-10-23 for 2.1.309) documents as
an Anthropic credential or endpoint other than the claude.ai login (``CLAUDE_CODE_OAUTH_TOKEN`` stays: it is the
login), or as a switch, endpoint, project, region or credential of Bedrock, Vertex (Google Cloud Agent Platform),
Foundry, Mantle or Claude Platform on AWS, plus the cloud SDKs' own ``AWS_*``, ``GOOGLE_*``, ``GCLOUD_*`` and
``AZURE_*`` credentials; ``CLAUDE_CONFIG_DIR`` ("Override the configuration directory ... All settings, session
history, and plugins are stored under this path", so it can carry another login or ``apiKeyHelper``); and the proxy
variables the page lists (``HTTP_PROXY``, ``HTTPS_PROXY``, ``NO_PROXY``) with ``ALL_PROXY``, which the page does not
list but common HTTP clients read, in upper and lower case: not billing, but they change where requests and
lookups go. Names are compared case-sensitively (on Windows the environment's names are upper case). A trailing
``*`` matches a prefix; a ``*`` inside matches any run of characters. A run recorded as unbilled must not be
routed to a billed provider."""
THIRD_PARTY_SWITCHES = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
                        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST")
"""Set (non-empty and not ``0``/``false``/``no``/``off``), any of these sends a ``--bare`` run somewhere other than
Anthropic's API under the operator's key, so the lane is ``provider_3p`` and ``api`` is refused."""
INSTRUCTION_NAMES = ("claude.md", "claude.local.md", "agents.md", ".mcp.json")  # compared case-insensitively
INSTRUCTION_DIRS = (".claude",)
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_NAMES = 8


def _dropped(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in LOGIN_LANE_DROPPED_ENV)


def third_party(env) -> list[str]:
    """The ``THIRD_PARTY_SWITCHES`` set in ``env``."""
    return [name for name in THIRD_PARTY_SWITCHES
            if str(env.get(name, "")).strip().lower() not in ("", "0", "false", "no", "off")]


def instruction_file(path: str) -> bool:
    """A path Claude Code may read as instructions, settings or MCP servers from the working directory: an
    instruction file's name, a path named ``.claude`` (a file or a link standing in for the directory), or anything
    under a ``.claude`` directory, at any depth, compared case-insensitively."""
    parts = path.replace("\\", "/").lower().split("/")
    return parts[-1] in INSTRUCTION_NAMES or any(part in INSTRUCTION_DIRS for part in parts)


def instruction_link(path: str, target: str) -> bool:
    """A symbolic link at ``path`` whose ``target`` (the link's text, relative to its directory, or absolute)
    resolves to an instruction file or to or under a ``.claude`` directory."""
    target = target.strip().replace("\\", "/")
    absolute = target.startswith("/") or re.match(r"^[A-Za-z]:/", target) is not None
    resolved = posixpath.normpath(target if absolute else posixpath.join(posixpath.dirname(path), target))
    return instruction_file(resolved)


def version_tuple(text: str) -> tuple[int, int, int] | None:
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def built_in_tools(tools: Sequence[str]) -> list[str]:
    """The built-in tool names among ``--allowedTools`` entries, for ``--tools``: MCP tools left out (``--tools``
    does not affect them), a rule's parenthesised specifier dropped (``Bash(git *)`` is ``Bash``)."""
    found: list[str] = []
    for tool in tools:
        name = tool.split("(", 1)[0].strip()
        if name and not name.startswith("mcp__") and name not in found:
            found.append(name)
    return found


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

    def lookups(self, decisions: Sequence[dict] = (), key: str = "") -> list[dict]:
        """``{tool, query_digest, query_chars, at, ok, guard}`` per GitHits or web lookup, in stream order. ``ok`` is
        false when the stream carries no successful result for it, or the guard blocked it. ``guard`` is the query
        guard's decision from ``decisions`` (``query_guard.read_decisions``): only a decision whose MAC verifies under
        ``key`` and whose ``tool_use_id``, tool and query digest equal this lookup's counts; there is no fallback
        match. ``pass``, a rule name, or ``unguarded`` when no such decision exists."""
        found = []
        verified = [d for d in decisions if query_guard.authentic(d, key)]
        for use_id in self.order:
            use = self.uses[use_id]
            if not is_lookup(use["name"]):
                continue
            query = query_text(use["input"])
            digest = hashlib.sha256(query.encode()).hexdigest()
            match = next((d for d in verified if d["tool_use_id"] == use_id and d["tool"] == use["name"][:64]
                          and d["query_digest"] == digest), None)
            if match is not None:
                verified.remove(match)
            guard = match["rule"] if match is not None else query_guard.UNGUARDED
            found.append({"tool": use["name"][:64], "query_digest": digest, "query_chars": len(query), "at": use["at"],
                          "ok": self.results.get(use_id) is True and guard == query_guard.PASS, "guard": guard})
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


def run_stream(argv: Sequence[str], prompt: str, *, cwd: Path, env: dict, timeout: int,
               lines: list | None = None) -> tuple[int, list, str]:
    """Run Claude Code with ``prompt`` on stdin (not in argv: no length limit, not in a process listing) and collect
    its stdout lines with the host's arrival time into ``lines`` (a caller's list, so what was read before a
    timeout is still the caller's). Returns ``(exit code, lines, stderr head)``."""
    lines = [] if lines is None else lines
    size = [0]
    with tempfile.TemporaryFile() as err:
        try:
            # Its own session (POSIX) or process group (Windows), so a timeout ends Claude Code and what it started.
            proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=str(cwd),
                                    env=env, start_new_session=hasattr(os, "setsid"),
                                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
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
                _stop(proc)  # checks._stop: the session on POSIX, the process tree on Windows
                reader.join(5)
                raise Rejected("provider_timeout", "Claude Code exceeded its time or output limit")
            time.sleep(0.05)
        reader.join(30)
        err.seek(0)
        stderr = err.read(400).decode(errors="replace")
    return proc.returncode, lines, stderr


def _third_party_reason(names: list[str]) -> str:
    return (", ".join(names) + " is set: this --bare run would go to another endpoint or provider, not Anthropic's API "
            "under your key, so its lane is provider_3p (pass --lane provider_3p) and api is refused")


class ClaudeCodeProvider(RepairProvider):
    """The repair loop with Claude Code as the call. ``max_calls=1`` is one shot. See the module docstring."""

    operators = ("patch",)

    def __init__(self, repo: Repo | str | Path, *, bare: bool = False, trusted_tree: bool = False,
                 mcp_config: str | Path | None = None, allowed_tools: Sequence[str] | None = None,
                 max_turns: int | None = None, lane: str | None = None, executable: Sequence[str] | None = None,
                 name: str = "claude-code", revision: str = "1", timeout: int = 1800, max_calls: int | None = None,
                 guard_max_lines: int | None = None, guard_max_chars: int | None = None):
        lane = lane or ("api" if bare else "subscription_local")
        if lane not in LANES:
            raise Rejected("invalid_provider", "lane must be one of " + ", ".join(LANES))
        if bare and lane in ("subscription_local", "seat_team_enterprise"):
            raise Rejected("invalid_provider", "--bare never reads a Claude login; its lane is api or provider_3p")
        if not bare and not trusted_tree:
            raise Rejected("untrusted_tree",
                           "refusing to run Claude Code without --bare on a tree not declared trusted: a non-bare "
                           "`claude -p` runs the project's hooks, connects its .mcp.json servers and reads its memory "
                           "files (CLAUDE.md, CLAUDE.local.md) as instructions, with no trust prompt. Pass "
                           "--trusted-tree if you trust this repository and every tree the model will write in it "
                           "(a repair whose parent changed an instruction file is refused), or use --bare (an API "
                           "key in the environment)")
        tools = list(DEFAULT_TOOLS if allowed_tools is None else allowed_tools)
        if not tools or any(not isinstance(t, str) or not t.strip() or "," in t for t in tools):
            raise Rejected("invalid_provider", "allowed tools must be a non-empty list of tool names")
        if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
            raise Rejected("invalid_provider", "max_turns must be a positive integer")
        if bare and lane == "api" and third_party(os.environ):
            raise Rejected("invalid_provider", _third_party_reason(third_party(os.environ)))
        if mcp_config is not None:
            mcp_config = Path(mcp_config).resolve()
            if not mcp_config.is_file():
                raise Rejected("invalid_provider", f"--mcp-config {mcp_config} is not a file")
        try:
            guard = query_guard.policy(guard_max_lines, guard_max_chars)
        except ValueError as exc:
            raise Rejected("invalid_provider", str(exc)) from None
        claude = list(executable) if executable else claude_executable()
        super().__init__(claude, name=name, revision=revision, timeout=timeout, max_calls=max_calls)
        self.repo = repo if isinstance(repo, Repo) else Repo(repo)
        self.bare, self.trusted_tree, self.lane, self.cost_basis = bare, trusted_tree, lane, LANE_COST[lane]
        self.mcp_config, self.allowed_tools, self.max_turns = mcp_config, tools, max_turns
        self.guard = guard  # host policy: {version, max_lines, max_chars}
        self.lookups: list[dict] = []  # every lookup of every call, as recorded (digests, never query text)
        self.version = self._installed_version()

    def _installed_version(self) -> str:
        """``claude --version``, refused below ``MIN_VERSION``: an older Claude Code ignores ``onFailure`` and lets
        a lookup through when the hook crashes or times out."""
        floor = ".".join(map(str, MIN_VERSION))
        try:
            done = subprocess.run([*self.argv, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=60, env=self.environment())
        except OSError as exc:
            raise Rejected("provider_unavailable", f"could not start {self.argv[0]!r}: {exc.strerror}") from None
        except subprocess.TimeoutExpired:
            raise Rejected("invalid_provider", "`claude --version` did not answer within 60 seconds") from None
        found = version_tuple(done.stdout.decode("utf-8", errors="replace")) if done.returncode == 0 else None
        if found is None:
            raise Rejected("invalid_provider", f"could not read Claude Code's version from `claude --version`; "
                                               f"{floor} or later is required")
        if found < MIN_VERSION:
            raise Rejected("invalid_provider", f"Claude Code {'.'.join(map(str, found))} is older than {floor}, the "
                                               "version that honours onFailure: \"block\" on the query guard's hook")
        return ".".join(map(str, found))

    def command(self, settings: str | Path | None = None) -> list[str]:
        """The argv of a call; ``settings`` is the query guard's temporary settings file (``--settings``). Two
        providers that differ only in ``mcp_config`` differ only in the last two items."""
        argv = [*self.argv, "-p"] + (["--bare"] if self.bare else [])
        argv += ["--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk", "--restricted",
                 "--tools", ",".join(built_in_tools(self.allowed_tools)),
                 "--allowedTools", ",".join(self.allowed_tools), "--setting-sources", "user", "--strict-mcp-config",
                 "--no-session-persistence", "--append-system-prompt", HARNESS_NOTE]
        if settings is not None:
            argv += ["--settings", str(settings)]
        if self.max_turns is not None:
            argv += ["--max-turns", str(self.max_turns)]
        if self.mcp_config is not None:
            argv += ["--mcp-config", str(self.mcp_config)]
        return argv

    def environment(self) -> dict:
        """The caller's environment, narrowed: no signing seed on any lane; on a login lane nothing in
        ``LOGIN_LANE_DROPPED_ENV``. A ``--bare`` api run whose environment routes elsewhere is refused here too, in
        case the environment changed after the provider was built."""
        env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
        if self.lane in ("subscription_local", "seat_team_enterprise"):
            env = {k: v for k, v in env.items() if not _dropped(k)}
        elif self.bare and self.lane == "api" and third_party(env):
            raise Rejected("provider_lane_mismatch", _third_party_reason(third_party(env)))
        return env

    def _instructions_changed(self, space: SearchSpace, parents: list[str]) -> list[str]:
        """Instruction files the parent candidate changed against the thread's base (none on a first call): an
        instruction path, or a symbolic link (mode 120000) whose target resolves to one."""
        if not parents:
            return []
        parent = space.candidate(parents[0])
        found = []
        for path in parent.changed_paths:
            if instruction_file(path):
                found.append(path)
                continue
            entry = self.repo.entry(parent.commit, path)
            if entry is not None and entry[0] == "120000":
                target = self.repo.git("cat-file", "blob", entry[1]).decode("utf-8", errors="replace")
                if instruction_link(path, target):
                    found.append(path)
        return sorted(found)

    def _journal(self, space: SearchSpace, found: list[dict]) -> list[dict]:
        """Record each lookup of this call: on the provider, and as a verified fact through ``space.observe``."""
        version = getattr(space, "search_id", "s0")
        record = getattr(space, "observe", None)
        for number, lookup in enumerate(found, 1):
            self.lookups.append(lookup)
            if callable(record):
                record(LOOKUP, lookup, f"{version}.{self.calls}.{number}")
        return found

    def _call(self, space: SearchSpace, request: dict, parents: list[str]):
        changed = self._instructions_changed(space, parents)
        if changed:
            refuse(space, "instruction_files_changed", "patch", parents)
            raise Rejected("instruction_files_changed",
                           f"the parent {parents[0]} changed {len(changed)} instruction file(s) against the base "
                           "(CLAUDE.md, CLAUDE.local.md, AGENTS.md, .mcp.json or .claude/): Claude Code would read "
                           "them as the next call's instructions, so no call is made on it")
        meter = current_meter()
        if meter is not None:
            meter.permit()
        commit = space.candidate(parents[0]).commit if parents else space.base
        build_prompt = prompt_builder()
        guard_dir = Path(tempfile.mkdtemp(prefix="syberlabs-guard-"))  # the host's, outside every worktree
        key = secrets.token_hex(32)  # this call's MAC key: in Claude Code's environment only, never in a file
        lines: list[tuple[str, str]] = []
        failure: Rejected | None = None
        try:
            with self.repo.worktree(commit) as tree:
                prompt = build_prompt(request, tree)
                settings = query_guard.prepare(guard_dir, self.guard,
                                               query_guard.tree_digests(tree, self.repo.files(commit)))
                hook_env = query_guard.hook_environment(guard_dir, key)
                env = {**self.environment(), **hook_env}
                try:
                    code, _, stderr = run_stream(self.command(settings), prompt, cwd=tree, env=env,
                                                 timeout=self.timeout, lines=lines)
                except Rejected as exc:
                    if exc.code != "provider_timeout":
                        raise  # nothing ran
                    failure = exc
                transcript = Transcript(lines)
                decisions = query_guard.read_decisions(guard_dir)
                subtype = (transcript.result or {}).get("subtype")
                if failure is None and (transcript.result is None or (code != 0 and subtype != "error_max_turns")):
                    failure = Rejected("provider_failed", f"exit {code}: " + (_short(subtype) or stderr[:300]))
                if failure is None:
                    changes, skipped = read_changes(tree, commit)
        finally:
            query_guard.remove(guard_dir)
        found = self._journal(space, transcript.lookups(decisions, key))
        if failure is not None:
            raise failure  # after the lookups it made are journaled: they may have left the machine
        usage, by_model = transcript.usage(), transcript.usage_by_model()
        if meter is not None:
            if self.cost_basis in ("billed", "estimated") and by_model:
                for model, row in by_model.items():  # each model priced at its own rate
                    meter.measure({**row, **({"model": model} if model != "unnamed" else {}), "host_observed": True})
            else:
                meter.measure(None)  # a call was made; under this lane it has no per-call bill to price
        unguarded = sum(1 for lookup in found if lookup["guard"] == query_guard.UNGUARDED)
        if unguarded:
            raise Rejected("provider_guard_inactive",
                           f"{unguarded} lookup(s) ran with no verified decision from the query guard: the hook "
                           "passed with --settings did not run (a version, a setting or a mode that skips it), or "
                           "its record was missing, altered or not the hook's; no further call is made. See "
                           "docs/CLAUDE_CODE.md, the query guard")
        signal = {"lane": self.lane, "cost_basis": self.cost_basis,
                  "harness": {**transcript.harness(), "installed_version": self.version},
                  "guard": {**self.guard, "policy_digest": hook_env[query_guard.POLICY_DIGEST_ENV]}}
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
