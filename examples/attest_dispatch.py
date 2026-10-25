"""Dispatch an Attest ticket to a configured worker and print only its diff.

The default configuration selects the OpenAI Responses API. Select
``--worker command`` to opt into an offline command worker. Command workers
receive the original ticket JSON on stdin and must emit a Git-format unified
diff on stdout. OpenAI workers return complete file contents through
Structured Outputs; this program builds the diff locally.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

TICKET_SCHEMA = "attest.ticket/v0"
OBSERVATION_KEY = "attest_ticket"
OBSERVATION_SOURCE = "attest"
OBSERVATION_ACTOR = "attest-dispatch"
DEFAULT_WORKERS = Path(__file__).resolve().parent / "fixtures" / "workers.example.json"
TICKET_FIELDS = {
    "schema",
    "case_id",
    "title",
    "spec",
    "acceptance",
    "observed",
    "app",
    "output",
}


class DispatchError(ValueError):
    """An invalid ticket, worker configuration, or worker result."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DispatchError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} must be a nonempty string")
    return value


def _relative_file_path(value: Any, field: str) -> str:
    path = _nonempty_string(value, field)
    if "\\" in path or "\x00" in path or "\n" in path or "\r" in path:
        raise DispatchError(f"{field} must be a safe path relative to app.root")
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    parts = path.split("/")
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise DispatchError(f"{field} must be a safe path relative to app.root")
    return path


def validate_ticket(ticket: Any) -> dict[str, Any]:
    """Validate the v0 ticket fields required by the worker contract."""
    if not isinstance(ticket, dict):
        raise DispatchError("ticket must be a JSON object")
    if ticket.get("schema") != TICKET_SCHEMA:
        raise DispatchError(f"ticket schema must be {TICKET_SCHEMA}")

    missing = TICKET_FIELDS - ticket.keys()
    if missing:
        raise DispatchError("ticket is missing required fields: " + ", ".join(sorted(missing)))
    unexpected = ticket.keys() - TICKET_FIELDS
    if unexpected:
        raise DispatchError("ticket has unexpected fields: " + ", ".join(sorted(unexpected)))

    _nonempty_string(ticket["case_id"], "case_id")
    _nonempty_string(ticket["title"], "title")
    _nonempty_string(ticket["acceptance"], "acceptance")
    _nonempty_string(ticket["output"], "output")

    spec = ticket["spec"]
    if not isinstance(spec, dict):
        raise DispatchError("spec must be a JSON object")
    _nonempty_string(spec.get("source"), "spec.source")
    _nonempty_string(spec.get("text"), "spec.text")
    lines = spec.get("lines")
    if (
        not isinstance(lines, list)
        or len(lines) != 2
        or any(type(line) is not int or line < 1 for line in lines)
        or lines[1] < lines[0]
    ):
        raise DispatchError("spec.lines must be a two-item positive line range")

    observed = ticket["observed"]
    if not isinstance(observed, dict):
        raise DispatchError("observed must be a JSON object")
    _nonempty_string(observed.get("status"), "observed.status")

    app = ticket["app"]
    if not isinstance(app, dict):
        raise DispatchError("app must be a JSON object")
    _nonempty_string(app.get("root"), "app.root")
    files = app.get("files")
    if not isinstance(files, dict) or not files:
        raise DispatchError("app.files must be a nonempty object")
    for path, contents in files.items():
        _relative_file_path(path, "app.files path")
        if not isinstance(contents, str):
            raise DispatchError(f"app.files[{path!r}] must contain text")
    return ticket


def parse_ticket(raw: str) -> dict[str, Any]:
    try:
        ticket = json.loads(raw, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, DispatchError) as exc:
        raise DispatchError(f"invalid ticket JSON: {exc}") from exc
    return validate_ticket(ticket)


def ticket_observation(ticket: dict[str, Any]) -> dict[str, Any]:
    """Return the same unverified observation envelope used by the receipt bridge."""
    return {
        "key": OBSERVATION_KEY,
        "value": {
            "case_id": ticket["case_id"],
            "title": ticket["title"],
            "observed": ticket["observed"],
        },
        "source": OBSERVATION_SOURCE,
        "version": TICKET_SCHEMA,
        "actor": OBSERVATION_ACTOR,
        "verified": False,
    }


def _git_path(path: str) -> str:
    """Quote a Git patch path when whitespace or quoting needs it."""
    if not any(char.isspace() or char in '"\\' for char in path):
        return path
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(f"\\{ord(char):03o}" if ord(char) < 32 else char for char in escaped)
    return f'"{escaped}"'


def _file_diff(path: str, old: str | None, new: str) -> str:
    old_text = old if old is not None else ""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    from_path = f"a/{path}"
    to_path = f"b/{path}"
    git_from = _git_path(from_path)
    git_to = _git_path(to_path)
    lines = [f"diff --git {git_from} {git_to}\n"]
    if old is None:
        lines.append("new file mode 100644\n")
        diff_from = "/dev/null"
    else:
        diff_from = from_path
    diff_to = to_path
    unified = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=_git_path(diff_from),
            tofile=_git_path(diff_to),
            lineterm="\n",
        )
    )
    if not unified and old is None:
        # An empty new file has no hunks, but still needs unified file headers.
        lines.extend(["--- /dev/null\n", f"+++ {_git_path(to_path)}\n"])
        return "".join(lines)

    # difflib preserves missing final newlines but does not add Git's marker.
    for index, line in enumerate(unified):
        lines.append(line)
        if index >= 2 and line and line[0] in " +-" and not line.endswith(("\n", "\r")):
            lines.append("\n\\ No newline at end of file\n")
    return "".join(lines)


def files_to_diff(ticket: dict[str, Any], files: Any) -> str:
    """Build a Git-format patch from complete returned file contents."""
    if not isinstance(files, list) or not files:
        raise DispatchError("OpenAI worker returned no files")
    original = ticket["app"]["files"]
    seen: set[str] = set()
    patch: list[str] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise DispatchError("each returned file must contain only path and content")
        path = _relative_file_path(item["path"], "returned file path")
        content = item["content"]
        if not isinstance(content, str):
            raise DispatchError(f"returned file {path!r} content must be text")
        if path in seen:
            raise DispatchError(f"OpenAI worker returned duplicate file: {path}")
        seen.add(path)
        old = original.get(path)
        if old != content:
            patch.append(_file_diff(path, old, content))
    if not patch:
        raise DispatchError("worker returned no file changes")
    return "".join(patch)


def _openai_instructions(ticket: dict[str, Any]) -> str:
    files = ", ".join(sorted(ticket["app"]["files"]))
    return f"""Implement the capability with the smallest change, following the existing code style, markup, and CSS conventions.
Do not change seed data, existing rows, names, or unrelated behavior. Do not add data that makes the spec pass by itself; the user performs the spec's steps in the browser.
Return only the complete new content of each file you change, as objects with path and content in the structured output schema. Do not return partial snippets or prose outside that schema. Paths must be relative to app.root ({ticket['app']['root']}). For an existing file, provide its complete replacement contents. Include only files that need to change. Existing files: {files}."""


def _response_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise DispatchError("OpenAI refused the ticket")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    if not chunks:
        raise DispatchError("OpenAI response contained no structured output")
    return "".join(chunks)


def openai_api_key() -> str | None:
    """Read the standard key name, with a Reason-safe configurable secret fallback."""
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("SYBERWORK_OPENAI_API_KEY")


def openai_worker(
    ticket: dict[str, Any],
    model: str,
    api_key: str,
    service_tier: str | None = None,
    reasoning: dict[str, str] | None = None,
) -> str:
    """Call the Responses API for structured file contents and create a diff."""
    schema = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["files"],
        "additionalProperties": False,
    }
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": _openai_instructions(ticket)},
            {"role": "user", "content": json.dumps(ticket, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "attest_file_contents",
                "strict": True,
                "schema": schema,
            }
        },
    }
    if service_tier is not None:
        payload["service_tier"] = service_tier
    if reasoning is not None:
        payload["reasoning"] = reasoning
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DispatchError(f"OpenAI Responses API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DispatchError(f"OpenAI Responses API request failed: {exc.__class__.__name__}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchError("OpenAI Responses API returned invalid JSON") from exc

    if not isinstance(response_data, dict):
        raise DispatchError("OpenAI Responses API returned an unexpected response")
    try:
        output = json.loads(_response_text(response_data), object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, DispatchError) as exc:
        raise DispatchError(f"OpenAI structured output was invalid: {exc}") from exc
    if not isinstance(output, dict) or set(output) != {"files"}:
        raise DispatchError("OpenAI structured output must contain only files")
    return files_to_diff(ticket, output["files"])


def validate_git_diff(patch: str) -> None:
    """Reject worker stdout that is empty, prose, or not Git-format diff data."""
    if not patch or not patch.strip():
        raise DispatchError("worker produced an empty diff")
    lines = patch.splitlines()
    if not lines or not lines[0].startswith("diff --git a/"):
        raise DispatchError("worker stdout must start with a Git-format diff")
    if "*** Begin Patch" in patch or "```" in patch:
        raise DispatchError("worker stdout must not contain patch wrappers or Markdown fences")
    diff_headers = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    for index, start in enumerate(diff_headers):
        end = diff_headers[index + 1] if index + 1 < len(diff_headers) else len(lines)
        section = lines[start:end]
        has_old = any(
            line.startswith("--- a/") or line.startswith('--- "a/') or line == "--- /dev/null"
            for line in section
        )
        has_new = any(line.startswith("+++ b/") or line.startswith('+++ "b/') for line in section)
        if not has_old or not has_new:
            raise DispatchError("worker stdout contains an incomplete Git-format diff")
        for line in section[1:]:
            if line.startswith((
                "index ",
                "new file mode ",
                "deleted file mode ",
                "old mode ",
                "new mode ",
                "similarity index ",
                "dissimilarity index ",
                "rename from ",
                "rename to ",
                "copy from ",
                "copy to ",
                "--- ",
                "+++ ",
                "@@",
                " ",
                "+",
                "-",
                "\\ No newline at end of file",
            )):
                continue
            raise DispatchError("worker stdout contains text outside the Git-format diff")


def load_worker_config(path: Path, worker_name: str | None = None) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DispatchError) as exc:
        raise DispatchError(f"cannot read worker configuration from {path}") from exc
    if not isinstance(config, dict):
        raise DispatchError("worker configuration must be a JSON object")

    if "workers" in config:
        workers = config["workers"]
        if not isinstance(workers, dict):
            raise DispatchError("workers must be an object of named worker configurations")
        default_name = _nonempty_string(config.get("default"), "default")
        selected_name = worker_name or default_name
        if selected_name not in workers:
            raise DispatchError(f"worker {selected_name!r} is not configured")
        worker = workers[selected_name]
        worker_label = f"workers.{selected_name}"
    else:
        if worker_name not in (None, "default"):
            raise DispatchError("named worker selection requires a workers object")
        worker = config.get("default")
        worker_label = "default"

    if not isinstance(worker, dict):
        raise DispatchError("selected worker configuration must be an object")
    worker_type = worker.get("type")
    if worker_type == "command":
        command = worker.get("cmd")
        try:
            if isinstance(command, str):
                argv = shlex.split(_nonempty_string(command, f"{worker_label}.cmd"))
            elif isinstance(command, list) and command and all(isinstance(arg, str) and arg for arg in command):
                argv = command
            else:
                raise DispatchError(f"{worker_label}.cmd must be a command string or nonempty argument list")
        except ValueError as exc:
            raise DispatchError(f"invalid {worker_label}.cmd: {exc}") from exc
        if not argv:
            raise DispatchError(f"{worker_label}.cmd must contain a command")
        config_dir = str(path.resolve().parent)
        argv = [arg.replace("{python}", sys.executable).replace("{config_dir}", config_dir) for arg in argv]
        return {"type": "command", "argv": argv}
    if worker_type == "openai":
        config: dict[str, Any] = {
            "type": "openai",
            "model": _nonempty_string(worker.get("model"), f"{worker_label}.model"),
        }
        if "service_tier" in worker:
            config["service_tier"] = _nonempty_string(
                worker["service_tier"], f"{worker_label}.service_tier"
            )
        if "reasoning" in worker:
            reasoning = worker["reasoning"]
            if not isinstance(reasoning, dict):
                raise DispatchError(f"{worker_label}.reasoning must be an object")
            if set(reasoning) != {"effort"}:
                raise DispatchError(f"{worker_label}.reasoning must contain only effort")
            config["reasoning"] = {
                "effort": _nonempty_string(reasoning["effort"], f"{worker_label}.reasoning.effort")
            }
        return config
    raise DispatchError(f"{worker_label}.type must be 'command' or 'openai'")


def command_worker(ticket_json: str, argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv,
            input=ticket_json.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise DispatchError(f"cannot start command worker: {exc.__class__.__name__}") from exc
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.buffer.flush()
    if result.returncode:
        raise DispatchError(f"command worker exited with status {result.returncode}")
    try:
        patch = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DispatchError("command worker stdout must be UTF-8 text") from exc
    # Command workers often run through Windows text-mode stdout, which uses CRLF.
    # Normalize before emitting so the dispatcher never doubles those newlines.
    patch = patch.replace("\r\n", "\n")
    validate_git_diff(patch)
    return patch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch one attest.ticket/v0 to a worker")
    parser.add_argument(
        "--workers",
        type=Path,
        default=DEFAULT_WORKERS,
        help=f"worker configuration JSON (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument("--worker", help="select a named worker from the configuration")
    args = parser.parse_args(argv)

    try:
        ticket_json = sys.stdin.read()
        ticket = parse_ticket(ticket_json)
        observation = ticket_observation(ticket)
        print(f"attest_dispatch: admitted case {ticket['case_id']}", file=sys.stderr)
        print(json.dumps(observation, ensure_ascii=False, sort_keys=True), file=sys.stderr)

        worker = load_worker_config(args.workers, args.worker)
        if worker["type"] == "command":
            patch = command_worker(ticket_json, worker["argv"])
        else:
            api_key = openai_api_key()
            if not api_key:
                raise DispatchError("OPENAI_API_KEY is not set (or SYBERWORK_OPENAI_API_KEY)")
            patch = openai_worker(
                ticket,
                worker["model"],
                api_key,
                worker.get("service_tier"),
                worker.get("reasoning"),
            )
            validate_git_diff(patch)
        # Bypass platform text-mode newline conversion to preserve Git diff bytes.
        sys.stdout.buffer.write(patch.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    except (DispatchError, UnicodeDecodeError) as exc:
        print(f"attest_dispatch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
