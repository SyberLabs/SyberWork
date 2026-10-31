"""Claude Code as a proposer: the operator's own, unmodified ``claude -p`` inside the repair loop.

    syberlabs propose --claude-code --trusted-tree                      # the operator's login (subscription_local)
    syberlabs propose --claude-code --bare                              # an API key in the environment (api)
    syberlabs propose --claude-code --bare --mcp-config githits.json    # the same, with one more MCP server
    syberlabs propose --claude-code --lane api-hooked --trusted-tree    # an API key, without --bare: hooks run
    syberlabs propose --claude-code --lane api-hooked --trusted-tree --githits   # the same, with the GitHits launcher

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

GitHits through the API client (``githits=True``, ``--githits``; ``docs/CLAUDE_CODE.md``, GitHits through the
launcher). For each call the host writes, in the call's guard directory outside the worktree, a read-only launcher
(``write_launcher``) that runs the host checkout's ``syberlabs/githits_cli_entry.py`` under ``python -I``; adds
``Bash`` to ``--tools`` and ``Bash(<launcher>:*)`` to ``--allowedTools``; appends the adapted GitHits skill
(``skills/githits/SKILL.md``, its SHA-256 on ``signal.harness.skill``) to the harness note; matches ``Bash`` in the
query guard, which passes only the launcher with plain arguments and judges those (and ``PowerShell`` too, by the
same rule, under ``allow_code_tools_with_mcp``); and gives Claude Code's environment the client's settings
(``GITHITS_API_TOKEN`` passed through from the operator's, a per-call record directory and record key,
``GITHITS_SESSION_ID`` ``<thread>-<arm>``, ``GITHITS_AGENT`` ``syberfactory/<arm>``, the license and exclusion
policy). The launcher's entry runs only the five lookups (``githits.LOOKUPS``; never ``doctor``) and ``example``
only with ``license_mode: strict``. After the call the host reads the client's authentic records
(``githits.read_records``) and journals one ``lookup`` per record, tool ``githits.<op>``; ``signal.tool_usage.githits``
is their number, cross-checked against the stream's launcher calls (``signal.githits.mismatch``). Without
``githits=True`` the argv is what it was; and with no ``mcp_config`` either (arm A: no GitHits route at all) the
environment loses every GitHits variable (``githits_variable``: ``GITHITS_*`` and ``SYBERLABS_GITHITS_*``, the token
included), so arm A holds no GitHits credential. An ``mcp_config`` run keeps them: a GitHits MCP server, its child,
may read its token from that environment.

The query guard (``syberlabs.query_guard``). Every call runs with ``--settings FILE``, a temporary settings document
the host writes in its own directory outside the worktree that holds one ``PreToolUse`` hook and nothing else: the
host's own ``query_guard.py`` by absolute path, matched to ``mcp__githits__*``, ``WebSearch`` and ``WebFetch``. It
blocks a lookup whose query carries a credential, an email, a URL with credentials, code (more than ``max_lines``
lines, a fence, more than ``max_chars`` characters) or a line copied from the worktree's tracked files, and writes
its decision (rule name, never text) beside itself with an HMAC under a random per-call key that no file holds. The
key is in Claude Code's environment, and every child of Claude Code inherits it: the hook, every stdio MCP server
(the GitHits server process) and any code-running tool the operator allows. A verifying MAC therefore shows that a
process holding the key wrote the decision, which a model limited to the file tools cannot do; it does not show
that the hook wrote it. A blocked lookup is still a lookup: its record has
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
operator allows it by name. When ``--mcp-config`` names any MCP server, an allowed code-running built-in
(``CODE_TOOLS``: ``Bash`` and ``PowerShell``, the ones ``--restricted``'s help names) is refused when the provider
is built (``invalid_provider``): a shell beside a server that already holds the key leaves the MAC nothing to
prove. ``allow_code_tools_with_mcp`` (``--allow-code-tools-with-mcp``) overrides, and is recorded as
``signal.guard.allow_code_tools_with_mcp``.

Credential lanes (bring your own credential: SyberLabs never stores, pools or forwards a credential; the operator's
own Claude Code runs under their own login, or with their API key). ``api`` (``--bare``, the default
there) is ``billed``: the tokens are priced under the contract's ``budget.prices`` through the host's meter, as an
adapter's usage is. ``api-hooked`` (``--lane api-hooked``, never with ``--bare``) is the same API key, billed the
same way, with Claude Code not in bare mode, so the query guard's ``--settings`` hook runs: Claude Code 2.1.312 does
not run ``--settings`` ``PreToolUse`` hooks under ``--bare`` (live canary on a Linux VM, 2026-10-30), so ``githits``
refuses ``--bare`` and the GitHits treatment runs only on a lane where its guard runs. Ambient configuration is kept
out of it: each call gets a fresh, empty ``CLAUDE_CONFIG_DIR`` and ``HOME`` (``USERPROFILE`` on Windows) in the call's
guard directory (no user settings, memory, skills, plugins or login), ``--disable-slash-commands``, and the
``--restricted``, ``--setting-sources user`` and ``--strict-mcp-config`` every lane passes, so only managed settings
and the provider's ``--settings`` load; ``ANTHROPIC_API_KEY`` is kept (required) and every other credential, login
token, routing or proxy variable is removed (``LOGIN_LANE_DROPPED_ENV`` and ``CLAUDE_CODE_OAUTH_TOKEN``), while
``ANTHROPIC_BASE_URL`` or a third-party switch is refused, as on ``api``. A non-bare run reads the worktree's memory
files, so ``api-hooked`` needs ``trusted_tree`` like every non-bare lane. ``provider_3p`` (Bedrock, Vertex,
Foundry credentials) is ``estimated``: priced the same way, but the bill is the cloud provider's.
``subscription_local`` (an inherited login, the default without ``--bare``) and
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
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from syberlabs import githits as githits_client
from syberlabs import githits_pace, query_guard
from syberlabs.checks import _stop
from syberlabs.errors import Rejected
from syberlabs.gitspace import MAX_FILE_BYTES, Repo
from syberlabs.providers import (RepairProvider, USAGE_FIELDS, current_meter, model_usage, refuse, split_command,
                                 tool_usage)
from syberlabs.query_guard import query_text
from syberlabs.search import SearchSpace

HOOKED = "api-hooked"  # the API key without --bare, so --settings hooks run; ambient configuration kept out
LANES = ("api", HOOKED, "subscription_local", "seat_team_enterprise", "provider_3p")
COST_BASES = ("billed", "estimated", "none")
LANE_COST = {"api": "billed", HOOKED: "billed", "provider_3p": "estimated", "subscription_local": "none",
             "seat_team_enterprise": "none"}
API_KEY_ENV = "ANTHROPIC_API_KEY"
# What api-hooked removes besides LOGIN_LANE_DROPPED_ENV (minus the key it runs on): the claude.ai login token, so
# a run recorded as billed on the key cannot fall back to a subscription.
HOOKED_DROPPED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN",)
CONFIG_DIR, HOME_DIR = "claude-config", "home"  # api-hooked: a fresh, empty config directory and home per call
BARE_HOOKS = ("Claude Code 2.1.312 does not run --settings PreToolUse hooks under --bare (live canary on a Linux VM, "
              "2026-10-30: the launcher ran with no guard and its lookup was sent before the run stopped "
              "provider_guard_inactive)")
BIN_ENV = "SYBERLABS_CLAUDE_BIN"  # the Claude Code executable (a path, or a command line); default ``claude`` on PATH
DEFAULT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")  # no Bash: a shell could read the guard's key
# The built-in tools that run commands or code that ``--restricted``'s help names (2.1.309: "Bash, PowerShell and
# the other code-running tools"); refused beside an MCP server unless the operator overrides. Compared ignoring case.
CODE_TOOLS = ("Bash", "PowerShell")
# ``claude --version`` as Claude Code documents it, the whole output: anything else is refused, never searched.
VERSION_OUTPUT = re.compile(r"(\d+)\.(\d+)\.(\d+) \(Claude Code\)")
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
# GitHits through the API client (``githits=True``).
LAUNCHER_NAME = "githits"                 # the launcher's file name in the call's guard directory
RECORDS_DIR = "githits-records"           # the client's record directory, beside it
GITHITS_ENTRY = Path(__file__).resolve().parent / "githits_cli_entry.py"  # the host checkout's, by absolute path
SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "githits" / "SKILL.md"
SKILL_NAME = "skills/githits/SKILL.md"
LAUNCHER_PLACEHOLDER = "{launcher}"
CLIENT_TOOL = "githits."                  # a lookup fact from a client record: ``githits.<op>``
UNRECORDED_OP = "unrecorded"              # ``githits.unrecorded``: a failed call's launcher call that left no record
GITHITS_UNRECORDED = "githits_unrecorded"           # a launcher call passed the guard and left no authentic record
GITHITS_UNEXPECTED = "githits_unexpected_records"   # more authentic records than launcher calls the guard passed
GITHITS_VARIABLES = ("GITHITS_", "SYBERLABS_GITHITS_")  # prefixes arm A never passes (compared ignoring case)
_LAUNCHER_PATH = re.compile(r"[A-Za-z0-9_./:+-]+")  # typed unquoted by the model, and inside ``Bash(...:*)``
_ARM = re.compile(r"[A-Za-z0-9_-]{1,16}")


def _dropped(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in LOGIN_LANE_DROPPED_ENV)


def githits_variable(name: str) -> bool:
    """A GitHits variable (``GITHITS_API_TOKEN``, ``GITHITS_API_URL``, ``SYBERLABS_GITHITS_*`` ...): what a provider
    with no GitHits route (no ``githits=True`` and no ``mcp_config``: arm A) removes from Claude Code's environment."""
    return name.upper().startswith(GITHITS_VARIABLES)


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
    """``(X, Y, Z)`` when the output, stripped, is exactly ``X.Y.Z (Claude Code)``; None otherwise, so a banner or
    any other line before or after the version fails closed instead of lending its own number."""
    found = VERSION_OUTPUT.fullmatch(text.strip())
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def code_tools(tools: Sequence[str]) -> list[str]:
    """The ``CODE_TOOLS`` among ``--allowedTools`` entries (``Bash(git *)`` is ``Bash``), compared ignoring case."""
    named = {name.lower() for name in CODE_TOOLS}
    return [name for name in built_in_tools(tools) if name.lower() in named]


def names_mcp_server(path: Path) -> bool:
    """False only for a JSON object whose ``mcpServers`` is absent or an empty object; a file that cannot be read
    or parsed, or any other shape, counts as naming a server (fail closed)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("mcpServers", {}) == {})


def built_in_tools(tools: Sequence[str]) -> list[str]:
    """The built-in tool names among ``--allowedTools`` entries, for ``--tools``: MCP tools left out (``--tools``
    does not affect them), a rule's parenthesised specifier dropped (``Bash(git *)`` is ``Bash``)."""
    found: list[str] = []
    for tool in tools:
        name = tool.split("(", 1)[0].strip()
        if name and not name.startswith("mcp__") and name not in found:
            found.append(name)
    return found


def githits_skill() -> tuple[str, str]:
    """``(prompt template, SHA-256 of SKILL.md)``: the adapted GitHits skill from the host checkout, its YAML front
    matter (name, license, source, changes) left out of the prompt. Like the adapter's prompt, it lives in the
    repository beside the package, not in the wheel. The digest is of the file with CRLF normalized to LF, so a
    Windows checkout with ``core.autocrlf`` records the committed blob's digest, as a POSIX one does
    (``.gitattributes`` also checks ``skills/**`` out with LF)."""
    try:
        data = SKILL_PATH.read_bytes()
    except OSError:
        raise Rejected("provider_unavailable", f"{SKILL_NAME} is not beside this package; --githits needs it") from None
    text = data.decode("utf-8").replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        text = text[end + 5:] if end >= 0 else text
    return text.strip() + "\n", hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def launcher_text(python: str, entry: Path) -> str:
    """The launcher: a POSIX shell script (Claude Code's Bash tool runs bash, Git Bash on Windows) that replaces
    itself with the host's Python running the host checkout's entry under ``-I``, with the call's arguments."""
    def quoted(path) -> str:
        text = Path(path).as_posix()
        if "'" in text or "\n" in text:
            raise Rejected("provider_unavailable", "the host's Python or checkout path holds a quote or a newline")
        return "'" + text + "'"
    return ("#!/bin/sh\n# SyberLabs GitHits launcher for one Claude Code call: written by the host, read-only, removed "
            "after the call.\n# It runs the host checkout's GitHits client, never the candidate worktree's.\n"
            f"exec {quoted(python)} -I {quoted(entry)} \"$@\"\n")


def write_launcher(directory: Path, python: str | None = None, entry: Path = GITHITS_ENTRY) -> str:
    """Write the call's launcher in the host's guard ``directory`` (read and execute only) and return its absolute
    path with forward slashes, the exact text the model types and ``Bash(...:*)`` names. A path the model could not
    type unquoted (a space, a quote) is refused."""
    path = Path(directory).resolve() / LAUNCHER_NAME
    name = path.as_posix()
    if not _LAUNCHER_PATH.fullmatch(name):
        raise Rejected("provider_unavailable", f"the launcher path {name!r} holds characters a shell command cannot "
                                               "carry unquoted; set TMP or TEMP to a plain directory")
    path.write_bytes(launcher_text(python or sys.executable, entry).encode("utf-8"))
    os.chmod(path, stat.S_IREAD | stat.S_IEXEC | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return name


def is_launcher_call(use: dict, launcher: str | None) -> bool:
    """A ``Bash`` (or ``PowerShell``) tool use whose command names the launcher (``query_guard.bash_arguments``:
    anything but ``not_launcher``), whatever its arguments."""
    command = use["input"].get("command") if isinstance(use.get("input"), dict) else None
    return (use.get("name") in query_guard.SHELL_TOOLS and launcher is not None
            and query_guard.bash_arguments(command, launcher)[0] != query_guard.NOT_LAUNCHER)


def record_lookup(record: dict, guard: str) -> dict:
    """A ``lookup`` fact from one authentic client record: ``githits.<op>`` and the record's counts, never text.
    ``sent`` is the record's: whether the request was written to GitHits (null: not known). ``paced_ms``, only when
    the record carries it, is how long the host's launcher held the call to keep GitHits' example rate
    (``githits_pace``); the experiment report leaves it out of the arm's agent time."""
    op = record.get("op") if isinstance(record.get("op"), str) and re.fullmatch(r"[a-z]{1,16}", record["op"]) else "unknown"
    found = {"tool": CLIENT_TOOL + op}
    for field in ("query_digest", "request_id", "status", "at"):
        found[field] = record[field][:128] if isinstance(record.get(field), str) else None
    for field in ("results_returned", "results_dropped", "results_dropped_license", "latency_ms"):
        found[field] = _count(record.get(field))
    found["contamination_blocked"] = record.get("contamination_blocked") is True
    found["sent"] = record.get("sent") if isinstance(record.get("sent"), bool) else None
    if "paced_ms" in record:
        found["paced_ms"] = _count(record.get("paced_ms"))
    found["ok"] = found["status"] == githits_client.OK and guard == query_guard.PASS
    found["guard"] = guard
    return found


def unrecorded_lookup(call: dict) -> dict:
    """A ``lookup`` fact standing for a launcher call the guard passed that left no client record, in a call that
    registers no candidate to carry ``signal.githits.mismatch`` (it failed or timed out): tool ``githits.unrecorded``,
    the digest of the command line, ``guard: unguarded`` (the host cannot reconcile it), never text."""
    return record_lookup({"op": UNRECORDED_OP, "query_digest": call.get("query_digest"), "at": call.get("at"),
                          "sent": None}, query_guard.UNGUARDED)


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
            digest = query_guard.query_digest(use["input"])  # the hook's own function: a lone surrogate agrees
            match = next((d for d in verified if d["tool_use_id"] == use_id and d["tool"] == use["name"][:64]
                          and d["query_digest"] == digest), None)
            if match is not None:
                verified.remove(match)
            guard = match["rule"] if match is not None else query_guard.UNGUARDED
            found.append({"tool": use["name"][:64], "query_digest": digest, "query_chars": len(query), "at": use["at"],
                          "ok": self.results.get(use_id) is True and guard == query_guard.PASS, "guard": guard})
        return found

    def bash_calls(self, decisions: Sequence[dict] = (), key: str = "", launcher: str | None = None) -> list[dict]:
        """``{tool, query_digest, query_chars, at, ok, guard, launcher}`` per ``Bash`` or ``PowerShell`` tool use
        (``query_guard.SHELL_TOOLS``), in stream order, matched to the query guard's verified decision as ``lookups``
        matches one (exact tool use id, tool and digest of the command line); ``launcher`` is whether the command
        names the GitHits launcher."""
        found = []
        verified = [d for d in decisions if query_guard.authentic(d, key)]
        for use_id in self.order:
            use = self.uses[use_id]
            if use["name"] not in query_guard.SHELL_TOOLS:
                continue
            digest = query_guard.query_digest(use["input"])
            match = next((d for d in verified if d["tool_use_id"] == use_id and d["tool"] == use["name"]
                          and d["query_digest"] == digest), None)
            if match is not None:
                verified.remove(match)
            guard = match["rule"] if match is not None else query_guard.UNGUARDED
            found.append({"tool": use["name"], "query_digest": digest, "query_chars": len(query_text(use["input"])),
                          "at": use["at"], "ok": self.results.get(use_id) is True and guard == query_guard.PASS,
                          "guard": guard, "launcher": is_launcher_call(use, launcher)})
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


def _third_party_reason(names: list[str], lane: str = "api") -> str:
    if lane == HOOKED:
        return (", ".join(names) + f" is set: this {HOOKED} run would go to another endpoint or provider, not "
                "Anthropic's API under your key; unset it (provider_3p runs with --bare)")
    return (", ".join(names) + " is set: this --bare run would go to another endpoint or provider, not Anthropic's API "
            "under your key, so its lane is provider_3p (pass --lane provider_3p) and api is refused")


class ClaudeCodeProvider(RepairProvider):
    """The repair loop with Claude Code as the call. ``max_calls=1`` is one shot. See the module docstring."""

    operators = ("patch",)

    def __init__(self, repo: Repo | str | Path, *, bare: bool = False, trusted_tree: bool = False,
                 mcp_config: str | Path | None = None, allowed_tools: Sequence[str] | None = None,
                 max_turns: int | None = None, lane: str | None = None, executable: Sequence[str] | None = None,
                 name: str = "claude-code", revision: str = "1", timeout: int = 1800, max_calls: int | None = None,
                 guard_max_lines: int | None = None, guard_max_chars: int | None = None,
                 allow_code_tools_with_mcp: bool = False, githits: bool = False, arm: str = "b",
                 thread_label: str | None = None):
        lane = lane or ("api" if bare else "subscription_local")
        if lane not in LANES:
            raise Rejected("invalid_provider", "lane must be one of " + ", ".join(LANES))
        if bare and lane in ("subscription_local", "seat_team_enterprise"):
            raise Rejected("invalid_provider", "--bare never reads a Claude login; its lane is api or provider_3p")
        if bare and lane == HOOKED:
            raise Rejected("invalid_provider", f"{HOOKED} is the API-key lane without --bare, so the query guard's "
                                               "--settings hook runs; drop --bare")
        if not bare and lane == "api":
            raise Rejected("invalid_provider", f"the api lane is --bare; for the API key without --bare (hooks run) "
                                               f"use --lane {HOOKED}")
        if githits and bare:
            raise Rejected("invalid_provider", f"refusing --githits with --bare: {BARE_HOOKS}. Run the treatment where "
                                               f"its guard runs: --lane {HOOKED} --trusted-tree (the API key), or a "
                                               "login lane; arm A runs the same lane without --githits")
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
        if lane in ("api", HOOKED) and third_party(os.environ):
            raise Rejected("invalid_provider", _third_party_reason(third_party(os.environ), lane))
        if lane == HOOKED and not str(os.environ.get(API_KEY_ENV, "")).strip():
            raise Rejected("invalid_provider", f"{HOOKED} runs on {API_KEY_ENV} from the operator's environment "
                                               "(its config directory is empty: no login, no apiKeyHelper); set it")
        self.githits, self.arm, self.thread_label = bool(githits), arm, thread_label
        self.skill: tuple[str, str] | None = None
        if self.githits:
            risky = code_tools(tools)
            if risky and not allow_code_tools_with_mcp:
                raise Rejected("invalid_provider",
                               f"refusing {', '.join(risky)} with --githits: the only command-running entry allowed "
                               "beside GitHits is the launcher the provider adds for each call (Bash(<launcher>:*)). "
                               "The query guard's decision-signing key and the GitHits record key are in Claude "
                               "Code's environment, which every child inherits, and a code-running tool can read "
                               "them and sign a forged decision or record. Drop the tool, or pass "
                               "--allow-code-tools-with-mcp to accept that (recorded in signal.guard; the guard "
                               "still blocks every Bash and PowerShell command but the launcher)")
            if not isinstance(arm, str) or not _ARM.fullmatch(arm):
                raise Rejected("invalid_provider", "the GitHits arm is 1 to 16 letters, digits, '_' or '-'")
            if thread_label is not None and (not isinstance(thread_label, str)
                                             or not re.fullmatch(r"[A-Za-z0-9_-]{1,47}", thread_label)):
                raise Rejected("invalid_provider", "the thread label is 1 to 47 letters, digits, '_' or '-'")
            if not os.environ.get(githits_client.TOKEN_ENV):
                raise Rejected("invalid_provider", f"--githits needs {githits_client.TOKEN_ENV} in the operator's "
                                                   "environment; it is passed to the launcher's client, never written")
            try:
                githits_client.LicensePolicy.from_env(os.environ)
                githits_client.Exclusions.from_env(os.environ)
                githits_pace.directory(os.environ)
            except (githits_client.GitHitsError, githits_pace.PaceError) as exc:
                raise Rejected("invalid_provider", str(exc)) from None
            self.skill = githits_skill()
        if mcp_config is not None:
            mcp_config = Path(mcp_config).resolve()
            if not mcp_config.is_file():
                raise Rejected("invalid_provider", f"--mcp-config {mcp_config} is not a file")
            risky = code_tools(tools)
            if risky and not allow_code_tools_with_mcp and names_mcp_server(mcp_config):
                raise Rejected("invalid_provider",
                               f"refusing {', '.join(risky)} with --mcp-config naming an MCP server: the query "
                               "guard's decision-signing key is in Claude Code's environment, which every child "
                               "inherits (the MCP server holds it already), and a code-running tool can read it and "
                               "sign a forged decision. Drop the tool, or pass --allow-code-tools-with-mcp to "
                               "accept that (recorded in signal.guard)")
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
        self.allow_code_tools_with_mcp = bool(allow_code_tools_with_mcp)  # the operator's override, on the signal
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

    def skill_prompt(self, launcher: str) -> str:
        """The skill text arm B's harness note carries, with this call's launcher path in place of the placeholder."""
        return self.skill[0].replace(LAUNCHER_PLACEHOLDER, launcher) if self.skill else ""

    def command(self, settings: str | Path | None = None, launcher: str | None = None) -> list[str]:
        """The argv of a call; ``settings`` is the query guard's temporary settings file (``--settings``). Two
        providers that differ only in ``mcp_config`` differ only in the last two items. With the GitHits
        ``launcher`` (``githits=True``) three items differ: ``--tools`` gains ``Bash``, ``--allowedTools`` gains
        ``Bash(<launcher>:*)``, and the appended system prompt gains the skill after the harness note."""
        tools, note = list(self.allowed_tools), HARNESS_NOTE
        if launcher is not None:
            tools.append(f"Bash({launcher}:*)")
            note = HARNESS_NOTE + "\n\n" + self.skill_prompt(launcher)
        argv = [*self.argv, "-p"] + (["--bare"] if self.bare else [])
        if self.lane == HOOKED:
            argv.append("--disable-slash-commands")  # no skill or command loads, whatever the machine holds
        argv += ["--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk", "--restricted",
                 "--tools", ",".join(built_in_tools(tools)),
                 "--allowedTools", ",".join(tools), "--setting-sources", "user", "--strict-mcp-config",
                 "--no-session-persistence", "--append-system-prompt", note]
        if settings is not None:
            argv += ["--settings", str(settings)]
        if self.max_turns is not None:
            argv += ["--max-turns", str(self.max_turns)]
        if self.mcp_config is not None:
            argv += ["--mcp-config", str(self.mcp_config)]
        return argv

    def environment(self) -> dict:
        """The caller's environment, narrowed: no signing seed on any lane; on a login lane nothing in
        ``LOGIN_LANE_DROPPED_ENV``; on ``api-hooked`` nothing in it either but ``ANTHROPIC_API_KEY``, and no
        ``CLAUDE_CODE_OAUTH_TOKEN`` (each call then adds a fresh config directory and home: ``isolation``); with no
        GitHits route (neither ``githits=True`` nor ``mcp_config``: arm A) no GitHits variable (``githits_variable``:
        the token, the base URL and every ``SYBERLABS_GITHITS_*``), so arm A holds no GitHits credential. An api or
        api-hooked run whose environment routes elsewhere, or an api-hooked run with no key, is refused here too, in
        case the environment changed after the provider was built."""
        route = self.githits or self.mcp_config is not None
        env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV and (route or not githits_variable(k))}
        if self.lane in ("subscription_local", "seat_team_enterprise"):
            env = {k: v for k, v in env.items() if not _dropped(k)}
        elif self.lane in ("api", HOOKED) and third_party(env):
            raise Rejected("provider_lane_mismatch", _third_party_reason(third_party(env), self.lane))
        if self.lane == HOOKED:
            if not str(env.get(API_KEY_ENV, "")).strip():
                raise Rejected("provider_lane_mismatch", f"{API_KEY_ENV} is no longer set; {HOOKED} runs on it")
            env = {k: v for k, v in env.items()
                   if k == API_KEY_ENV or not (_dropped(k) or k in HOOKED_DROPPED_ENV)}
        return env

    def isolation(self, directory: Path) -> dict:
        """``api-hooked``: what keeps the machine's own Claude Code configuration out of one call. A fresh, empty
        ``CLAUDE_CONFIG_DIR`` ("All settings, session history, and plugins are stored under this path", Claude Code's
        environment reference) and ``HOME`` (and ``USERPROFILE`` on Windows, where Node reads the home from it), both
        created in the call's guard ``directory``, which is removed after the call. Empty on every other lane."""
        if self.lane != HOOKED:
            return {}
        config, home = Path(directory) / CONFIG_DIR, Path(directory) / HOME_DIR
        config.mkdir()
        home.mkdir()
        found = {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(home)}
        if os.name == "nt":
            found["USERPROFILE"] = str(home)
        return found

    def githits_environment(self, records: Path, record_key: str, session: str) -> dict:
        """What arm B adds to Claude Code's environment for the launcher's client: the record directory and key
        (fresh per call), the host's example pacing directory (``SYBERLABS_GITHITS_PACE_DIR``: the operator's,
        else the host's temporary-directory default, resolved here, so a launcher whose ``TMPDIR`` or home differs
        still shares one pacing slot), the session and agent headers, and the license and exclusion policy as
        explicit lists (the operator's, else the defaults; ``SYBERLABS_GITHITS_EXCLUDE`` only adds to ``SyberLabs``).
        The token is the operator's own ``GITHITS_API_TOKEN``, inherited, never written."""
        licenses = githits_client.LicensePolicy.from_env(os.environ)
        exclude = githits_client.Exclusions.from_env(
            {k: v for k, v in os.environ.items() if k != githits_client.EXCLUDE_REPLACE_ENV})
        return {githits_client.RECORD_DIR_ENV: str(records), githits_client.RECORD_KEY_ENV: record_key,
                githits_pace.PACE_DIR_ENV: str(githits_pace.directory(os.environ)),
                githits_client.SESSION_ENV: session, githits_client.AGENT_ENV: f"syberfactory/{self.arm}",
                githits_client.LICENSES_ENV: ",".join(licenses.allowed) if licenses.allowed else githits_client.ANY_LICENSE,
                githits_client.EXCLUDE_ENV: ",".join("/".join(entry) for entry in exclude.entries)}

    def _session(self, space: SearchSpace) -> str:
        """``X-GitHits-Session-ID``: ``<thread>-<arm>`` (GitHits allows ``[A-Za-z0-9_-]{1,64}``, so no ``/``). The
        thread is ``thread_label`` (the CLI passes the thread's id), else the space's thread: ``GitSearchSpace`` keeps
        it as ``_thread``, so a provider built in Python names its thread too, and two threads never share ``s1-b``."""
        thread = getattr(space, "_thread", None)
        label = (self.thread_label or getattr(space, "thread_id", None) or getattr(thread, "id", None)
                 or getattr(space, "search_id", None) or "s0")
        return re.sub(r"[^A-Za-z0-9_-]", "-", f"{label}-{self.arm}")[:64]

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
        launcher = records_dir = record_key = None
        records = githits_client.RecordRead([], 0)
        try:
            with self.repo.worktree(commit) as tree:
                prompt = build_prompt(request, tree)
                settings = query_guard.prepare(guard_dir, self.guard,
                                               query_guard.tree_digests(tree, self.repo.files(commit)),
                                               bash=self.githits,
                                               powershell=self.githits and self.allow_code_tools_with_mcp)
                extra: dict = {}
                if self.githits:
                    launcher = write_launcher(guard_dir)
                    records_dir, record_key = guard_dir / RECORDS_DIR, secrets.token_hex(32)
                    records_dir.mkdir()
                    extra = self.githits_environment(records_dir, record_key, self._session(space))
                hook_env = query_guard.hook_environment(guard_dir, key, launcher)
                env = {k: v for k, v in self.environment().items()
                       if not (self.githits and k == githits_client.EXCLUDE_REPLACE_ENV)}
                env = {**env, **hook_env, **extra, **self.isolation(guard_dir)}
                try:
                    code, _, stderr = run_stream(self.command(settings, launcher), prompt, cwd=tree, env=env,
                                                 timeout=self.timeout, lines=lines)
                except Rejected as exc:
                    if exc.code != "provider_timeout":
                        raise  # nothing ran
                    failure = exc
                transcript = Transcript(lines)
                decisions = query_guard.read_decisions(guard_dir)
                if self.githits:
                    records = githits_client.read_records(records_dir, record_key)
                subtype = (transcript.result or {}).get("subtype")
                if failure is None and (transcript.result is None or (code != 0 and subtype != "error_max_turns")):
                    failure = Rejected("provider_failed", f"exit {code}: " + (_short(subtype) or stderr[:300]))
                if failure is None:
                    changes, skipped = read_changes(tree, commit)
        finally:
            query_guard.remove(guard_dir)
        observed = transcript.lookups(decisions, key)
        githits_signal = None
        if self.githits:
            bash = transcript.bash_calls(decisions, key, launcher)
            passed = sum(1 for call in bash if call["launcher"] and call["guard"] == query_guard.PASS)
            mismatch = (None if len(records.records) == passed else GITHITS_UNRECORDED
                        if len(records.records) < passed else GITHITS_UNEXPECTED)
            # Every Bash call the guard did not pass is journaled with its rule; a passing launcher call is
            # journaled through its client record, which is the host's evidence that a lookup left.
            observed += [{k: v for k, v in call.items() if k != "launcher"} for call in bash
                         if call["guard"] != query_guard.PASS]
            # A failed, timed-out or no_change call registers no candidate to carry signal.githits.mismatch: a
            # launcher call in flight when the host ended Claude Code (the session is killed, so its client never
            # records) would leave no trace. Its records are then journaled unguarded (not reconciled), and when it
            # has none, one githits.unrecorded fact stands for each launcher call the guard passed.
            lost = mismatch == GITHITS_UNRECORDED and failure is not None
            guard = query_guard.UNGUARDED if mismatch == GITHITS_UNEXPECTED or lost else query_guard.PASS
            observed += [record_lookup(record, guard) for record in records.records]
            if lost and not records.records:
                observed += [unrecorded_lookup(call) for call in bash
                             if call["launcher"] and call["guard"] == query_guard.PASS]
            githits_signal = {"arm": self.arm, "launcher_calls": sum(1 for call in bash if call["launcher"]),
                              "launcher_passed": passed, "records": len(records.records),
                              "records_ignored": records.ignored, "bash_refused": sum(
                                  1 for call in bash if call["guard"] != query_guard.PASS),
                              "license_policy": githits_client.LicensePolicy.from_env(os.environ).digest}
            if mismatch:
                githits_signal["mismatch"] = mismatch
        found = self._journal(space, observed)
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
        if self.githits and mismatch == GITHITS_UNRECORDED and not changes:
            # no_change cannot carry the candidate's mismatch signal. Persist a gap,
            # without assigning a missing record to a particular concurrent command.
            self._journal(space, [unrecorded_lookup({})])
        signal = {"lane": self.lane, "cost_basis": self.cost_basis,
                  "harness": {**transcript.harness(), "installed_version": self.version, "lane": self.lane},
                  "guard": {**self.guard, "policy_digest": hook_env[query_guard.POLICY_DIGEST_ENV],
                            **({"allow_code_tools_with_mcp": True} if self.allow_code_tools_with_mcp else {})}}
        if githits_signal is not None:
            signal["harness"]["skill"] = {"path": SKILL_NAME, "sha256": self.skill[1]}
            signal["githits"] = githits_signal
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
        tools = transcript.tool_usage()
        if self.githits and isinstance(tools, dict):
            # Host-observed: the authentic client records (plus any GitHits MCP tool the stream shows).
            tools["githits"] = tools.get("githits", 0) + len(records.records)
        response = {"usage": usage, "tool_usage": tools}
        response["candidates"] = [{"changes": changes, "message": "", "signal": signal}] if changes else []
        return response
