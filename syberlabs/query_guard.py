"""The query guard: a Claude Code ``PreToolUse`` hook that stops a lookup whose query could carry private material.

GitHits and the web tools see only the agent's queries, never the repository. This hook is what keeps that true
for the queries themselves: before Claude Code runs a GitHits (``mcp__githits__*``) or web (``WebSearch``,
``WebFetch``) tool, it reads the tool call from standard input and blocks it when the outbound text

- ``secret``: matches a credential shape (``_patterns.SECRET``, shared with ``metrics``; this module adds a
  private-key header and a bearer token), or
- ``url_credentials``: holds a URL with a user or password before the host (``scheme://user:pass@host``), or
- ``email``: holds an email address, or
- ``too_long``: is longer than ``max_chars`` characters (default 400), or
- ``code_fence``: holds a fenced block (three backticks or three tildes), or
- ``code_lines``: has more than ``max_lines`` non-blank lines (default 3), or
- ``tree_line``: has a line that, whitespace-normalized and at least ``MIN_TREE_LINE`` characters long, occurs
  verbatim in a tracked file of the worktree the run started from (the host hashes those lines into
  ``tree_lines.txt`` beside the hook before the run; the hook compares hashes and never holds the tree's text).

Anything else passes unchanged. The outbound text is every string of the tool input, dictionary keys included, for
every matched tool (``WebFetch``'s ``prompt`` too: it is processed off this machine).

``Bash`` is matched only when the host runs the GitHits client through its launcher (``claude_code``, ``githits=True``;
``settings(..., bash=True)``), and then a command passes only when it is the launcher's absolute path
(``SYBERLABS_GUARD_LAUNCHER`` in the hook's environment) followed by arguments the hook can read exactly as the shell
will (``bash_arguments``): words of plain characters and single- or double-quoted strings, separated by spaces.

- ``not_launcher``: any other command, or a command with no launcher configured;
- ``shell_syntax``: a control operator, a redirection, an expansion or a substitution anywhere after the launcher
  (``; & | < > $ ` \\ ( ) { } !``, any control character such as a newline), a glob, tilde, caret or comment
  character outside quotes, an unquoted word that a shell reads differently (one that starts with ``=``, which zsh's
  default EQUALS option expands to a command's path, with ``NAME=``, which bash's ``set -k`` makes an assignment into
  the launcher's environment, or with ``@``, which PowerShell splats), an unterminated quote, ``run_in_background``,
  or a ``timeout`` shorter than the Bash tool's default (``MIN_BASH_TIMEOUT_MS``: Claude Code would end the launcher
  after its request left and before its client writes the record).

The arguments that pass the shape check are the outbound text: each one, and all of them joined by spaces, are
judged by the rules above, so a launcher query carrying a credential, an email, code or a tree line is blocked as a
GitHits tool call is. Each string is judged on its
own, as typed and folded (``fold``: NFKC, every format character such as a zero-width space removed, and the
Cyrillic and Greek letters that look like Latin ones mapped to them); a rule broken by either form blocks. The hook
blocks the documented way: exit code 2 with a reason on standard error, which Claude Code returns to the model as
the tool's error (``code.claude.com/docs/en/hooks``, read 2026-10-23 for Claude Code 2.1.309). The reason names
the rule and never repeats the query. A hook that cannot read its input, its key or its policy, whose policy or
tree file does not match the digest the host gave it, or whose input carries no ``tool_use_id`` blocks too
(``guard_error``): a guard that fails open is not a guard.

Every decision is written to ``decisions/`` beside the hook as ``{tool_use_id, tool, query_digest, rule, mac}``: no
query text. ``mac`` is HMAC-SHA256 over the other four fields under a random per-call key the host puts in Claude
Code's environment (``KEY_ENV``). No file holds it, but the hook is not its only holder: every child of Claude Code
inherits that environment, the hook, every stdio MCP server (the GitHits server process) and any code-running tool
the operator allows. So a verifying MAC shows that a process holding the key wrote the decision, which a model
limited to the file tools cannot do; it does not show that this hook wrote it, and an MCP server or a shell could
sign one (``claude_code`` refuses a code-running tool beside an MCP server unless the operator overrides). The host
(``syberlabs.claude_code``) reads the decisions after the run, keeps only those whose MAC verifies, and records each
lookup with its ``guard``: the rule, ``pass``, or ``unguarded`` when no verified decision carries the lookup's exact
tool use id, tool and query digest.

This is a filter over what the model typed, not a proof that no private material leaves the machine: a model can
paraphrase code, spread it (or one credential) over several short queries, encode it (base64, hex, URL or percent
encoding, ROT13, one character per word), or quote a line it wrote during the run (the tree hash set is taken
before the run). The operator tunes ``max_lines`` and ``max_chars`` (``SYBERLABS_GUARD_MAX_LINES``,
``SYBERLABS_GUARD_MAX_CHARS``, or the CLI); they are host policy, recorded in the candidate's signal, never
contract data.

    python -I <host checkout>/syberlabs/query_guard.py <guard directory>    # what the hook runs (exec form)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import sys
import unicodedata
from pathlib import Path

if __package__ in (None, ""):  # run by absolute path (``-I``): the hook process
    # Load the host's own two leaf modules from beside this file, by path, and not the ``syberlabs`` package,
    # whose ``__init__`` imports the whole SDK (most of a lookup's time). A worktree's ``syberlabs`` is never
    # on the import path either way (``-I``).
    import importlib.util

    def _beside(name: str):
        spec = importlib.util.spec_from_file_location(f"syberlabs.{name}", Path(__file__).resolve().parent / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    canonical = _beside("canonical").canonical
    CREDENTIAL = _beside("_patterns").SECRET
else:
    from syberlabs._patterns import SECRET as CREDENTIAL  # the one credential pattern set, shared, not copied
    from syberlabs.canonical import canonical

VERSION = "2"
DEFAULT_MAX_LINES = 3
DEFAULT_MAX_CHARS = 400
MIN_TREE_LINE = 16       # a normalized tree line shorter than this is too common to mean "copied from here"
TREE_DIGEST_CHARS = 32   # hex characters of SHA-256 kept per tree line
MAX_TREE_FILE_BYTES = 1024 * 1024
LINES_ENV, CHARS_ENV = "SYBERLABS_GUARD_MAX_LINES", "SYBERLABS_GUARD_MAX_CHARS"
MATCHER = "^(mcp__githits__.*|WebSearch|WebFetch)$"
MATCHER_BASH = "^(mcp__githits__.*|WebSearch|WebFetch|Bash)$"  # with the GitHits launcher (claude_code githits=True)
# With the launcher and the operator's --allow-code-tools-with-mcp: an allowed PowerShell is judged as Bash is.
MATCHER_SHELLS = "^(mcp__githits__.*|WebSearch|WebFetch|Bash|PowerShell)$"
SHELL_TOOLS = ("Bash", "PowerShell")  # judged by the launcher-only rule (``bash_arguments``) when matched
PASS, UNGUARDED, ERROR = "pass", "unguarded", "guard_error"
NOT_LAUNCHER, SHELL_SYNTAX = "not_launcher", "shell_syntax"
RULES = ("secret", "url_credentials", "email", "too_long", "code_fence", "code_lines", "tree_line", NOT_LAUNCHER,
         SHELL_SYNTAX, ERROR)
DECISIONS = "decisions"
DECISION_FIELDS = ("tool_use_id", "tool", "query_digest", "rule")  # what the MAC covers
POLICY_FILE, TREE_FILE, SETTINGS_FILE = "policy.json", "tree_lines.txt", "settings.json"
# What the host hands the hook through Claude Code's environment, which a command hook inherits (the hooks reference
# documents no per-hook ``env`` field): the per-call MAC key and the SHA-256 of the policy and tree files it wrote.
# Every other child of Claude Code inherits them too, stdio MCP servers included.
KEY_ENV = "SYBERLABS_GUARD_KEY"
LAUNCHER_ENV = "SYBERLABS_GUARD_LAUNCHER"  # the GitHits launcher's absolute path: the one Bash command allowed
POLICY_DIGEST_ENV, TREE_DIGEST_ENV = "SYBERLABS_GUARD_POLICY_DIGEST", "SYBERLABS_GUARD_TREE_DIGEST"
BLOCK_EXIT = 2  # Claude Code: a PreToolUse hook that exits 2 blocks the call; its stderr is the reason the model sees
# Claude Code's Bash tool ends a command after 2 minutes unless its ``timeout`` (milliseconds) says otherwise. A
# launcher call may lengthen that, never shorten it: an early end could come after the lookup left and before the
# client's record is written.
MIN_BASH_TIMEOUT_MS = 120000

_EXTRA_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")
_URL_CREDENTIALS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]*(:[^\s/@]*)?@")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_FENCE = re.compile(r"```|~~~")
# Cyrillic and Greek letters that NFKC leaves alone but that read as Latin letters (a Cyrillic shha in ``ghp_``, a
# Cyrillic dze in ``sk-``, a Cyrillic A in ``AKIA``): mapped to Latin, so a lookalike prefix is judged as it reads.
_LOOKALIKE = str.maketrans({
    "\u0430": "a", "\u0432": "b", "\u0435": "e", "\u043a": "k", "\u043c": "m", "\u043d": "h", "\u043e": "o",
    "\u0440": "p", "\u0441": "c", "\u0442": "t", "\u0443": "y", "\u0445": "x", "\u0456": "i", "\u0455": "s",
    "\u0458": "j", "\u04bb": "h", "\u0501": "d", "\u051b": "q", "\u051d": "w",
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H", "\u041e": "O",
    "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X", "\u0406": "I", "\u0405": "S", "\u0408": "J",
    "\u03b1": "a", "\u03bf": "o", "\u03c1": "p", "\u03ba": "k", "\u03b9": "i", "\u03c5": "u", "\u03bd": "v",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H", "\u0399": "I", "\u039a": "K",
    "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T", "\u03a7": "X", "\u03a5": "Y",
})
WHY = {
    "secret": "it looks like it contains a credential",
    "url_credentials": "it contains a URL with a user name or password",
    "email": "it contains an email address",
    "too_long": "it is longer than the host allows for one lookup",
    "code_fence": "it contains a fenced code block",
    "code_lines": "it has more lines than the host allows for one lookup",
    "tree_line": "it contains a line copied from this repository",
    NOT_LAUNCHER: "the only command allowed here is the GitHits launcher named in your instructions",
    SHELL_SYNTAX: "the launcher's arguments may hold only plain words and quoted strings, with no shell operators, "
                  "redirections, substitutions or expansions (quote a word that starts with =, @ or NAME=, or "
                  "holds ^), and the call may not run in the background or set a timeout under two minutes",
    ERROR: "the guard could not read the call or its policy",
}
# What may follow the launcher. Anywhere: none of these (control operators, redirections, expansions, substitution,
# escapes, grouping, history). Outside quotes: only these word characters (no glob, tilde, comment or brace; no caret,
# a glob operator under zsh's EXTENDED_GLOB).
_SHELL_FORBIDDEN = frozenset(";&|<>$`\\(){}!")
_PLAIN = re.compile(r"[A-Za-z0-9_./:@=,+%-]")
# The unquoted start of a word that a shell reads as something else: ``=cmd`` (zsh's EQUALS, on by default, expands
# it to the command's path), ``NAME=...`` (under bash's ``set -k`` an assignment anywhere on the line goes into the
# command's environment, so ``GITHITS_API_URL=...`` would redirect the client and its token) and ``@name``
# (PowerShell, judged by this rule under the operator's override, splats the variable ``$name`` into arguments).
_READ_DIFFERENTLY = re.compile(r"[=@]|[A-Za-z_][A-Za-z0-9_]*=")


def query_text(tool_input) -> str:
    """The query of a lookup as the host records it: ``query`` (WebSearch, GitHits search), else ``url``
    (WebFetch), else ``command`` (Bash: the GitHits launcher's command line), else the whole input as canonical
    JSON. Only its digest and length are ever recorded."""
    if isinstance(tool_input, dict):
        for key in ("query", "url", "command"):
            if isinstance(tool_input.get(key), str):
                return tool_input[key]
    return canonical(tool_input if tool_input is not None else {})


def utf8(text: str) -> bytes:
    """``text`` as UTF-8 with a lone surrogate (what a JSON escape of U+D800 to U+DFFF decodes to) kept as its
    three bytes (``surrogatepass``) instead of raising. The hook and the host digest with this one function, so
    their digests of the same query agree."""
    return text.encode("utf-8", "surrogatepass")


def query_digest(tool_input) -> str:
    return hashlib.sha256(utf8(query_text(tool_input))).hexdigest()


def outbound(tool_name: str, tool_input) -> list[str]:
    """The texts this call sends off the machine, each judged on its own: every string of the input, dictionary keys
    included, for every matched tool (``WebFetch``'s ``prompt``, ``WebSearch``'s domain lists, and any field a tool
    adds later)."""
    if not isinstance(tool_input, dict):
        return [query_text(tool_input)]
    found: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                found.append(key if isinstance(key, str) else str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif value is not None and not isinstance(value, bool):
            found.append(str(value))

    walk(tool_input)
    return found


def bash_arguments(command, launcher: str | None) -> tuple[str, list[str]]:
    """``(rule, arguments)`` for a Bash command: ``pass`` and the arguments the shell would pass to the launcher, or
    ``not_launcher`` / ``shell_syntax`` and none. The command must be ``launcher`` exactly, alone or followed by a
    space; what follows may hold plain words (``_PLAIN``), ``'single'`` and ``"double"`` quoted strings and spaces,
    and none of ``_SHELL_FORBIDDEN`` or a control character anywhere. A word is the concatenation of its adjacent
    plain and quoted parts, as the shell reads it; a word whose unquoted start a shell reads differently
    (``_READ_DIFFERENTLY``: ``=cmd``, ``NAME=value``, ``@name``) is refused, and passes quoted."""
    if not isinstance(command, str) or not launcher or not (command == launcher or command.startswith(launcher + " ")):
        return NOT_LAUNCHER, []
    rest = command[len(launcher):]
    if any(ch in _SHELL_FORBIDDEN or unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in rest):
        return SHELL_SYNTAX, []
    found: list[str] = []
    word: list[str] | None = None
    lead = ""            # the word's unquoted start, up to its first quote
    quoted = False
    index = 0
    while index < len(rest):
        ch = rest[index]
        if ch == " ":
            if word is not None:
                if _READ_DIFFERENTLY.match(lead):
                    return SHELL_SYNTAX, []
                found.append("".join(word))
                word, lead, quoted = None, "", False
            index += 1
            continue
        word = [] if word is None else word
        if ch in "'\"":
            end = rest.find(ch, index + 1)
            if end < 0:
                return SHELL_SYNTAX, []
            word.append(rest[index + 1:end])
            quoted = True
            index = end + 1
            continue
        if not _PLAIN.fullmatch(ch):
            return SHELL_SYNTAX, []
        word.append(ch)
        lead += "" if quoted else ch
        index += 1
    if word is not None:
        if _READ_DIFFERENTLY.match(lead):
            return SHELL_SYNTAX, []
        found.append("".join(word))
    return PASS, found


def fold(text: str) -> str:
    """``text`` as a reader sees it: NFKC (fullwidth and other compatibility forms to their plain characters), every
    format character (Unicode category ``Cf``: zero-width space and joiners, direction marks, soft hyphen) removed,
    and the Cyrillic and Greek lookalikes of Latin letters mapped to them."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")
    return plain.translate(_LOOKALIKE)


def normalized(line: str) -> str:
    return " ".join(line.split())


def line_digest(line: str) -> str:
    return hashlib.sha256(utf8(normalized(line))).hexdigest()[:TREE_DIGEST_CHARS]


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
    """The shared credential shapes (``_patterns.SECRET``) at the start of a word (so ``risk-assessment`` is not
    an ``sk-`` key), or a private-key header or bearer token."""
    if _EXTRA_SECRET.search(text):
        return True
    return any(m.start() == 0 or not (text[m.start() - 1].isalnum() or text[m.start() - 1] == "_")
               for m in CREDENTIAL.finditer(text))


def judge(text: str, *, max_lines: int = DEFAULT_MAX_LINES, max_chars: int = DEFAULT_MAX_CHARS,
          tree: set[str] | frozenset[str] = frozenset()) -> str:
    """``pass`` or the first rule ``text`` breaks, in the order of ``RULES``: as typed, then folded (``fold``). A
    rule broken by either form blocks."""
    rule = _judge(text, max_lines=max_lines, max_chars=max_chars, tree=tree)
    if rule == PASS:
        folded = fold(text)
        if folded != text:
            rule = _judge(folded, max_lines=max_lines, max_chars=max_chars, tree=tree)
    return rule


def _judge(text: str, *, max_lines: int, max_chars: int, tree: set[str] | frozenset[str]) -> str:
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
           tree: set[str] | frozenset[str] = frozenset(), launcher: str | None = None) -> str:
    """The rule for one ``PreToolUse`` input (``tool_name``, ``tool_input``): ``pass`` or the first rule broken.
    A ``Bash`` or ``PowerShell`` call (``SHELL_TOOLS``) must first be the GitHits ``launcher`` with plain arguments
    (``bash_arguments``); its arguments, each and joined, are then the outbound text."""
    name, tool_input = call.get("tool_name"), call.get("tool_input")
    if not isinstance(name, str):
        return ERROR
    if name in SHELL_TOOLS:
        if not isinstance(tool_input, dict):
            return NOT_LAUNCHER
        if tool_input.get("run_in_background"):
            return SHELL_SYNTAX  # a background run would outlive the call whose record the host reads
        timeout = tool_input.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not timeout >= MIN_BASH_TIMEOUT_MS):
            return SHELL_SYNTAX  # ended early, the launcher's lookup could leave with no record written
        rule, arguments = bash_arguments(tool_input.get("command"), launcher)
        if rule != PASS:
            return rule
        texts = arguments + ([" ".join(arguments)] if len(arguments) > 1 else [])
    else:
        texts = outbound(name, tool_input)
    for text in texts:
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


def settings(directory: Path, python: str | None = None, bash: bool = False, powershell: bool = False) -> dict:
    """The temporary settings document passed with ``--settings``: this hook and nothing else. ``bash`` (the GitHits
    launcher is in use) adds ``Bash`` to the matcher, so every shell command is judged; ``powershell`` (with
    ``bash``, under the operator's code-tools override) adds ``PowerShell`` too, judged by the same rule."""
    matcher = (MATCHER_SHELLS if powershell else MATCHER_BASH) if bash else MATCHER
    return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [hook_command(directory, python)]}]}}


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_read_only(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))
    os.chmod(path, stat.S_IREAD)  # read-only for everyone; on Windows, the read-only attribute


def prepare(directory: Path, rules: dict, tree: set[str], python: str | None = None, bash: bool = False,
            powershell: bool = False) -> Path:
    """Write the guard's directory (policy, tree digests and settings, each read-only, and an empty
    ``decisions/``); returns the settings file to pass with ``--settings``. ``directory`` is the host's own,
    outside any worktree. ``hook_environment`` then gives the hook the files' digests and the MAC key."""
    directory = Path(directory)
    (directory / DECISIONS).mkdir(parents=True, exist_ok=True)
    _write_read_only(directory / POLICY_FILE, json.dumps(rules))
    _write_read_only(directory / TREE_FILE, "".join(d + "\n" for d in sorted(tree)))
    path = directory / SETTINGS_FILE
    _write_read_only(path, json.dumps(settings(directory, python, bash, powershell), indent=2))
    return path


def hook_environment(directory: Path, key: str, launcher: str | None = None) -> dict:
    """What the host adds to Claude Code's environment for the hook, which inherits it: the per-call MAC key and
    the SHA-256 of the policy and tree files ``prepare`` wrote; with the GitHits launcher, its absolute path."""
    directory = Path(directory)
    found = {KEY_ENV: key, POLICY_DIGEST_ENV: file_digest(directory / POLICY_FILE),
             TREE_DIGEST_ENV: file_digest(directory / TREE_FILE)}
    if launcher:
        found[LAUNCHER_ENV] = launcher
    return found


def remove(directory: Path) -> None:
    """Remove a guard directory, its read-only files included."""
    directory = Path(directory)
    for folder, _, names in os.walk(directory):
        for name in names:
            try:
                os.chmod(os.path.join(folder, name), stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(directory, ignore_errors=True)


def sign(value, key: str) -> str:
    """HMAC-SHA256 of ``value`` as canonical JSON (``utf8``) under ``key``: the MAC of a decision here and of a
    GitHits call record (``syberlabs.githits``)."""
    return hmac.new(key.encode("utf-8"), utf8(canonical(value)), hashlib.sha256).hexdigest()


def verify(value, given, key: str) -> bool:
    """True only when ``given`` is ``sign(value, key)`` under a non-empty key; a missing or wrong MAC is not."""
    return bool(key) and isinstance(given, str) and hmac.compare_digest(given, sign(value, key))


def mac(entry: dict, key: str) -> str:
    """HMAC-SHA256 of a decision's four fields (``DECISION_FIELDS``, as canonical JSON) under the per-call key."""
    return sign({field: entry.get(field) for field in DECISION_FIELDS}, key)


def authentic(entry: dict, key: str) -> bool:
    """True only when ``entry`` carries a MAC that verifies under ``key``; a missing or wrong MAC is not."""
    return verify({field: entry.get(field) for field in DECISION_FIELDS}, entry.get("mac"), key)


def read_decisions(directory: Path) -> list[dict]:
    """The decisions in ``decisions/``, each ``{tool_use_id, tool, query_digest, rule, mac}`` (``mac`` None when
    absent); malformed files are skipped. None is trusted until ``authentic`` verifies it."""
    found = []
    folder = Path(directory) / DECISIONS
    for file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            item = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(item, dict) and item.get("rule") in RULES + (PASS,) and isinstance(item.get("tool"), str)
                and isinstance(item.get("query_digest"), str) and isinstance(item.get("tool_use_id"), (str, type(None)))):
            found.append({**{k: item[k] for k in DECISION_FIELDS},
                          "mac": item["mac"] if isinstance(item.get("mac"), str) else None})
    return found


def _record(directory: Path, call: dict, rule: str, key: str) -> None:
    tool_use_id = call.get("tool_use_id") if isinstance(call.get("tool_use_id"), str) else None
    tool = call.get("tool_name") if isinstance(call.get("tool_name"), str) else ""
    digest = query_digest(call.get("tool_input"))
    entry = {"tool_use_id": tool_use_id, "tool": tool[:64], "query_digest": digest, "rule": rule}
    if key:
        entry["mac"] = mac(entry, key)
    name = hashlib.sha256(utf8(canonical([tool_use_id, tool, digest, os.getpid(), os.urandom(8).hex()]))).hexdigest()
    folder = directory / DECISIONS
    temporary = folder / f".{name}.tmp"
    temporary.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(temporary, folder / f"{name}.json")


def _matching(path: Path, expected: str) -> bytes:
    """The file's bytes when their SHA-256 is the digest the host gave; otherwise the guard refuses to judge."""
    data = path.read_bytes()
    if not expected or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        raise ValueError(f"{path.name} does not match the host's digest")
    return data


def run(directory: Path, stdin, stderr, environ=None) -> int:
    """The hook process: read the call, check the policy and tree files against the host's digests, decide, record
    the decision with its MAC, and exit 0 (pass) or 2 (blocked)."""
    directory = Path(directory)
    environ = os.environ if environ is None else environ
    key = environ.get(KEY_ENV, "")
    call: dict = {}
    try:
        raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
        call = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)  # Claude Code writes UTF-8
        if not isinstance(call, dict):
            raise ValueError("not an object")
        if not key:
            raise ValueError("no key: the host did not start this hook")
        if not isinstance(call.get("tool_use_id"), str) or not call["tool_use_id"]:
            raise ValueError("no tool_use_id: no decision could be matched to this call")
        rules = json.loads(_matching(directory / POLICY_FILE, environ.get(POLICY_DIGEST_ENV, "")).decode("utf-8"))
        tree = set(_matching(directory / TREE_FILE, environ.get(TREE_DIGEST_ENV, "")).decode("utf-8").split())
        rule = decide(call, max_lines=rules["max_lines"], max_chars=rules["max_chars"], tree=tree,
                      launcher=environ.get(LAUNCHER_ENV) or None)
    except Exception:  # noqa: BLE001  a guard that cannot judge blocks
        rule = ERROR
    try:
        _record(directory, call if isinstance(call, dict) else {}, rule, key)
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
