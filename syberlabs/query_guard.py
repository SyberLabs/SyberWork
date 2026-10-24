"""The query guard: a Claude Code ``PreToolUse`` hook that stops a lookup whose query could carry private material.

GitHits and the web tools see only the agent's queries, never the repository. This hook is what keeps that true
for the queries themselves: before Claude Code runs a GitHits (``mcp__githits__*``) or web (``WebSearch``,
``WebFetch``) tool, it reads the tool call from standard input and blocks it when the outbound text

- ``secret``: matches a credential shape (``metrics`` owns the pattern set; this module adds a private-key header
  and a bearer token), or
- ``url_credentials``: holds a URL with a user or password before the host (``scheme://user:pass@host``), or
- ``email``: holds an email address, or
- ``too_long``: is longer than ``max_chars`` characters (default 400), or
- ``code_fence``: holds a fenced block (three backticks or three tildes), or
- ``code_lines``: has more than ``max_lines`` non-blank lines (default 3), or
- ``tree_line``: has a line that, whitespace-normalized and at least ``MIN_TREE_LINE`` characters long, occurs
  verbatim in a tracked file of the worktree the run started from (the host hashes those lines into
  ``tree_lines.txt`` beside the hook before the run; the hook compares hashes and never holds the tree's text).

Anything else passes unchanged. The outbound text is ``url`` for ``WebFetch`` (its ``prompt`` goes to Anthropic's
model, not to the site), ``query`` and the domain lists for ``WebSearch``, and every string in the input for a
GitHits tool; each string is judged on its own. The hook blocks the documented way: exit code 2 with a reason on
standard error, which Claude Code returns to the model as the tool's error (``code.claude.com/docs/en/hooks``,
read 2026-10-23 for Claude Code 2.1.309). The reason names the rule and never repeats the query. A hook that
cannot read its input or its policy blocks too (``guard_error``): a guard that fails open is not a guard.

Every decision is written to ``decisions/`` beside the hook as ``{tool_use_id, tool, query_digest, rule}``: no
query text. The host (``syberlabs.claude_code``) reads them after the run and records each lookup with its
``guard``: the rule, ``pass``, or ``unguarded`` when the stream shows a lookup the hook never decided on.

This is a filter over what the model typed, not a proof that no private material leaves the machine: a model can
paraphrase code, spread it over several short queries, or quote a line it wrote during the run (the tree hash set
is taken before the run). The operator tunes ``max_lines`` and ``max_chars`` (``SYBERLABS_GUARD_MAX_LINES``,
``SYBERLABS_GUARD_MAX_CHARS``, or the CLI); they are host policy, recorded in the candidate's signal, never
contract data.

    python -I <host checkout>/syberlabs/query_guard.py <guard directory>    # what the hook runs (exec form)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # run by absolute path (``-I``): import the host's own package beside this file
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs.canonical import canonical  # noqa: E402
from syberlabs.metrics import _SECRET as CREDENTIAL  # noqa: E402  the one credential pattern set, shared, not copied

VERSION = "1"
DEFAULT_MAX_LINES = 3
DEFAULT_MAX_CHARS = 400
MIN_TREE_LINE = 16       # a normalized tree line shorter than this is too common to mean "copied from here"
TREE_DIGEST_CHARS = 32   # hex characters of SHA-256 kept per tree line
MAX_TREE_FILE_BYTES = 1024 * 1024
LINES_ENV, CHARS_ENV = "SYBERLABS_GUARD_MAX_LINES", "SYBERLABS_GUARD_MAX_CHARS"
MATCHER = "^(mcp__githits__.*|WebSearch|WebFetch)$"
PASS, UNGUARDED, ERROR = "pass", "unguarded", "guard_error"
RULES = ("secret", "url_credentials", "email", "too_long", "code_fence", "code_lines", "tree_line", ERROR)
DECISIONS = "decisions"
POLICY_FILE, TREE_FILE, SETTINGS_FILE = "policy.json", "tree_lines.txt", "settings.json"
BLOCK_EXIT = 2  # Claude Code: a PreToolUse hook that exits 2 blocks the call; its stderr is the reason the model sees

_EXTRA_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")
_URL_CREDENTIALS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]*(:[^\s/@]*)?@")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_FENCE = re.compile(r"```|~~~")
WHY = {
    "secret": "it looks like it contains a credential",
    "url_credentials": "it contains a URL with a user name or password",
    "email": "it contains an email address",
    "too_long": "it is longer than the host allows for one lookup",
    "code_fence": "it contains a fenced code block",
    "code_lines": "it has more lines than the host allows for one lookup",
    "tree_line": "it contains a line copied from this repository",
    ERROR: "the guard could not read the call or its policy",
}


def query_text(tool_input) -> str:
    """The query of a lookup as the host records it: ``query`` (WebSearch, GitHits search), else ``url``
    (WebFetch), else the whole input as canonical JSON. Only its digest and length are ever recorded."""
    if isinstance(tool_input, dict):
        for key in ("query", "url"):
            if isinstance(tool_input.get(key), str):
                return tool_input[key]
    return canonical(tool_input if tool_input is not None else {})


def query_digest(tool_input) -> str:
    return hashlib.sha256(query_text(tool_input).encode()).hexdigest()


def outbound(tool_name: str, tool_input) -> list[str]:
    """The texts this call sends to a third party, each judged on its own."""
    if not isinstance(tool_input, dict):
        return [query_text(tool_input)]
    if tool_name == "WebFetch":
        return [tool_input["url"]] if isinstance(tool_input.get("url"), str) else [query_text(tool_input)]
    if tool_name == "WebSearch":
        texts = [tool_input["query"]] if isinstance(tool_input.get("query"), str) else [query_text(tool_input)]
        for key in ("allowed_domains", "blocked_domains"):
            texts += [d for d in tool_input.get(key) or [] if isinstance(d, str)]
        return texts
    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif value is not None and not isinstance(value, bool):
            found.append(str(value))

    walk(tool_input)
    return found


def normalized(line: str) -> str:
    return " ".join(line.split())


def line_digest(line: str) -> str:
    return hashlib.sha256(normalized(line).encode()).hexdigest()[:TREE_DIGEST_CHARS]


def tree_digests(root: Path, paths) -> set[str]:
    """The digests of every normalized line of at least ``MIN_TREE_LINE`` characters in ``paths`` under ``root``.
    Binary, non-UTF-8 and oversized files are skipped. The host writes these; the hook only compares."""
    found: set[str] = set()
    for path in paths:
        file = root / path
        try:
            if not file.is_file() or file.stat().st_size > MAX_TREE_FILE_BYTES:
                continue
            data = file.read_bytes()
            if b"\0" in data[:8000]:
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if len(normalized(line)) >= MIN_TREE_LINE:
                found.add(line_digest(line))
    return found


def _credential(text: str) -> bool:
    """``metrics``' credential shapes at the start of a word (so ``risk-assessment`` is not an ``sk-`` key), or a
    private-key header or bearer token."""
    if _EXTRA_SECRET.search(text):
        return True
    return any(m.start() == 0 or not (text[m.start() - 1].isalnum() or text[m.start() - 1] == "_")
               for m in CREDENTIAL.finditer(text))


def judge(text: str, *, max_lines: int = DEFAULT_MAX_LINES, max_chars: int = DEFAULT_MAX_CHARS,
          tree: set[str] | frozenset[str] = frozenset()) -> str:
    """``pass`` or the first rule ``text`` breaks, in the order of ``RULES``."""
    if _credential(text):
        return "secret"
    if _URL_CREDENTIALS.search(text):
        return "url_credentials"
    if _EMAIL.search(text):
        return "email"
    if len(text) > max_chars:
        return "too_long"
    if _FENCE.search(text):
        return "code_fence"
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > max_lines:
        return "code_lines"
    if tree and any(len(normalized(line)) >= MIN_TREE_LINE and line_digest(line) in tree for line in lines):
        return "tree_line"
    return PASS


def decide(call: dict, *, max_lines: int = DEFAULT_MAX_LINES, max_chars: int = DEFAULT_MAX_CHARS,
           tree: set[str] | frozenset[str] = frozenset()) -> str:
    """The rule for one ``PreToolUse`` input (``tool_name``, ``tool_input``): ``pass`` or the first rule broken."""
    name, tool_input = call.get("tool_name"), call.get("tool_input")
    if not isinstance(name, str):
        return ERROR
    for text in outbound(name, tool_input):
        rule = judge(text, max_lines=max_lines, max_chars=max_chars, tree=tree)
        if rule != PASS:
            return rule
    return PASS


def reason(rule: str) -> str:
    """What the model reads when a lookup is blocked. It names the rule and never repeats the query."""
    return (f"SyberLabs query guard blocked this lookup (rule: {rule}): {WHY.get(rule, 'it broke a host rule')}. "
            "Lookups leave this machine, so they may carry only a short question about a public package or API: "
            "no code, no text from this repository, no credentials, emails or private URLs. Ask again as a short "
            "question naming the public package, its version and the API, or continue without the lookup.")


# Host policy: thresholds from the environment or the caller, never from the contract.

def _limit(value, env: str, default: int) -> int:
    if value is None:
        text = os.environ.get(env, "").strip()
        value = int(text) if text.isdigit() else default
    if type(value) is not int or value < 1:
        raise ValueError(f"{env.lower()} must be a positive integer")
    return value


def policy(max_lines: int | None = None, max_chars: int | None = None) -> dict:
    """``{version, max_lines, max_chars}``: the arguments, else ``SYBERLABS_GUARD_MAX_LINES`` /
    ``SYBERLABS_GUARD_MAX_CHARS``, else the defaults. Recorded on every candidate as ``signal.guard``."""
    return {"version": VERSION, "max_lines": _limit(max_lines, LINES_ENV, DEFAULT_MAX_LINES),
            "max_chars": _limit(max_chars, CHARS_ENV, DEFAULT_MAX_CHARS)}


def hook_command(directory: Path, python: str | None = None) -> dict:
    """The hook handler: exec form (no shell), this file by absolute path under ``python -I`` (no current directory
    and no ``PYTHONPATH`` on the import path, so a worktree's own ``syberlabs`` is never imported), blocking when
    the hook itself fails or times out (``onFailure``, Claude Code 2.1.295 and later)."""
    return {"type": "command", "command": python or sys.executable,
            "args": ["-I", str(Path(__file__).resolve()), str(Path(directory).resolve())],
            "timeout": 30, "onFailure": "block"}


def settings(directory: Path, python: str | None = None) -> dict:
    """The temporary settings document passed with ``--settings``: this hook and nothing else."""
    return {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [hook_command(directory, python)]}]}}


def prepare(directory: Path, rules: dict, tree: set[str], python: str | None = None) -> Path:
    """Write the guard's directory (policy, tree digests, settings, an empty ``decisions/``); returns the settings
    file to pass with ``--settings``. ``directory`` is the host's own, outside any worktree."""
    directory = Path(directory)
    (directory / DECISIONS).mkdir(parents=True, exist_ok=True)
    (directory / POLICY_FILE).write_text(json.dumps(rules), encoding="utf-8")
    (directory / TREE_FILE).write_text("".join(d + "\n" for d in sorted(tree)), encoding="utf-8")
    path = directory / SETTINGS_FILE
    path.write_text(json.dumps(settings(directory, python), indent=2), encoding="utf-8")
    return path


def read_decisions(directory: Path) -> list[dict]:
    """The decisions the hook wrote, each ``{tool_use_id, tool, query_digest, rule}``; malformed files are skipped."""
    found = []
    folder = Path(directory) / DECISIONS
    for file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            item = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(item, dict) and item.get("rule") in RULES + (PASS,) and isinstance(item.get("tool"), str)
                and isinstance(item.get("query_digest"), str) and isinstance(item.get("tool_use_id"), (str, type(None)))):
            found.append({k: item[k] for k in ("tool_use_id", "tool", "query_digest", "rule")})
    return found


def _record(directory: Path, call: dict, rule: str) -> None:
    tool_use_id = call.get("tool_use_id") if isinstance(call.get("tool_use_id"), str) else None
    tool = call.get("tool_name") if isinstance(call.get("tool_name"), str) else ""
    digest = query_digest(call.get("tool_input"))
    entry = {"tool_use_id": tool_use_id, "tool": tool[:64], "query_digest": digest, "rule": rule}
    name = hashlib.sha256(canonical([tool_use_id, tool, digest, os.getpid(), os.urandom(8).hex()]).encode()).hexdigest()
    folder = directory / DECISIONS
    temporary = folder / f".{name}.tmp"
    temporary.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(temporary, folder / f"{name}.json")


def run(directory: Path, stdin, stderr) -> int:
    """The hook process: read the call, decide, record the decision, and exit 0 (pass) or 2 (blocked)."""
    directory = Path(directory)
    call: dict = {}
    try:
        raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
        call = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)  # Claude Code writes UTF-8
        if not isinstance(call, dict):
            raise ValueError("not an object")
        rules = json.loads((directory / POLICY_FILE).read_text(encoding="utf-8"))
        tree = set((directory / TREE_FILE).read_text(encoding="utf-8").split())
        rule = decide(call, max_lines=rules["max_lines"], max_chars=rules["max_chars"], tree=tree)
    except Exception:  # noqa: BLE001  a guard that cannot judge blocks
        rule = ERROR
    try:
        _record(directory, call if isinstance(call, dict) else {}, rule)
    except Exception:  # noqa: BLE001  an unrecorded pass would read as unguarded; an unrecorded block still blocks
        if rule == PASS:
            rule = ERROR
    if rule == PASS:
        return 0
    stderr.write(reason(rule) + "\n")
    stderr.flush()
    return BLOCK_EXIT


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write(reason(ERROR) + "\n")
        return BLOCK_EXIT
    return run(Path(args[0]), sys.stdin, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
