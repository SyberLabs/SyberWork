"""Publishing effects for an accepted Build Thread: push, pull request, and a publish command.

Each is a separate contract action with its own authority, admitted like any
other action, and each runs only after acceptance (the contract says so with
``requires_effect`` and an ``arguments`` binding to the ``accepted`` fact the
host records from the target branch). Destinations are fixed in admin-owned
action definitions (``.syberlabs/actions.json``); a proposal only names the
accepted commit.

Every executor is idempotent under the proposal id and has a status lookup:

- ``git_push`` pushes with ``--force-with-lease``, so a remote that moved is a
  guaranteed no-write (412). Status is ``git ls-remote``.
- ``github_pull_request`` looks for an open or closed pull request from the
  thread branch at that commit before creating one. Status is the same lookup.
- ``command`` runs an admin-configured argv with ``SYBERLABS_IDEMPOTENCY_KEY``
  in its environment, and a status argv that reports ``applied``, ``absent``,
  or ``unknown``.

A remote destination that does not show the write is ``absent``, which only
becomes ``not_applied`` after ``settle_seconds`` (see ``Session.reconcile``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from syberlabs.errors import Rejected
from syberlabs.session import NoWrite
from syberlabs.targets import guard_request, trusted_origin

EFFECTS = ("git_push", "github_pull_request", "command")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_OUTPUT = 65_536


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(_NoRedirect)


def _bad(detail: str) -> Rejected:
    return Rejected("invalid_action", detail)


def _branch_ok(template: str) -> bool:
    body = template.replace("{thread}", "t")
    return (isinstance(template, str) and template.count("{thread}") <= 1 and bool(_BRANCH.match(body))
            and ".." not in body and "//" not in body and "@{" not in body
            and not body.endswith((".", "/", ".lock")) and "{" not in body and "}" not in body)


def validate(doc: dict) -> None:
    """Check a publishing action definition before it is installed."""
    effect = doc.get("effect")
    if doc.get("kind") != "local" or effect not in EFFECTS:
        raise _bad("publishing actions are kind local with effect " + ", ".join(EFFECTS))
    allowed = {"kind", "effect", "title", "settle_seconds", "timeout_seconds"}
    if "settle_seconds" in doc and (type(doc["settle_seconds"]) is not int or not 0 <= doc["settle_seconds"] <= 86_400):
        raise _bad("settle_seconds must be 0 to 86400")
    if "timeout_seconds" in doc and (type(doc["timeout_seconds"]) is not int or not 1 <= doc["timeout_seconds"] <= 3600):
        raise _bad("timeout_seconds must be 1 to 3600")
    if effect == "git_push":
        allowed |= {"remote", "branch"}
        if not isinstance(doc.get("remote"), str) or not _REMOTE.match(doc["remote"]):
            raise _bad("git_push needs the name of a configured git remote")
        if not _branch_ok(doc.get("branch", "syberlabs/{thread}")):
            raise _bad("git_push branch must be a plain branch name, optionally with {thread}")
    elif effect == "github_pull_request":
        allowed |= {"api", "repository", "base", "branch", "token_env"}
        api = doc.get("api")
        if not isinstance(api, str) or "?" in api or "#" in api:
            raise _bad("api must be an HTTPS base URL")
        trusted_origin(api.rstrip("/") + "/")
        if not isinstance(doc.get("repository"), str) or not _REPOSITORY.match(doc["repository"]):
            raise _bad("repository must be owner/name")
        if not isinstance(doc.get("base"), str) or not _branch_ok(doc["base"]) or "{thread}" in doc["base"]:
            raise _bad("base must be a branch name")
        if not _branch_ok(doc.get("branch", "syberlabs/{thread}")):
            raise _bad("branch must be a plain branch name, optionally with {thread}")
        if not isinstance(doc.get("token_env"), str) or not _ENV.match(doc["token_env"]):
            raise _bad("token_env must name an environment variable")
    else:
        allowed |= {"argv", "status_argv", "no_write_exit_codes"}
        for field in ("argv", "status_argv"):
            argv = doc.get(field)
            if (not isinstance(argv, list) or not argv or len(argv) > 64
                    or any(not isinstance(a, str) or "\x00" in a for a in argv) or not argv[0]):
                raise _bad(f"command {field} must be a non-empty list of strings; no shell is used")
        codes = doc.get("no_write_exit_codes", [])
        if not isinstance(codes, list) or any(type(c) is not int or not 1 <= c <= 125 for c in codes):
            raise _bad("no_write_exit_codes must be exit codes 1 to 125 that the command guarantees mean no write")
    unknown = set(doc) - allowed
    if unknown:
        raise _bad(f"unknown field {sorted(unknown)[0]} for {effect}")


class _Publisher:
    """Shared plumbing: the thread a proposal belongs to, and bounded subprocesses."""

    def __init__(self, kit, doc: dict):
        self.kit, self.doc = kit, doc
        self.settle_seconds = doc.get("settle_seconds", 120)
        self.timeout = doc.get("timeout_seconds", 300)

    def _thread(self, case_id: str):
        from syberlabs.build import Thread
        return Thread(self.kit, case_id)

    def _branch(self, case_id: str) -> str:
        return self.doc.get("branch", "syberlabs/{thread}").replace("{thread}", case_id[:8])


class GitPush(_Publisher):
    def _lease(self, case_id: str) -> str:
        """Expected remote value: absent for a per-thread branch, the thread base otherwise."""
        return "" if "{thread}" in self.doc.get("branch", "syberlabs/{thread}") else self._thread(case_id).base

    def _remote_head(self, ref: str) -> str | None:
        raw = self.kit.repo.git("ls-remote", self.doc["remote"], ref, timeout=self.timeout)
        for line in raw.decode().splitlines():
            commit, _, name = line.partition("\t")
            if name == ref:
                return commit
        return None

    def apply(self, case_id: str, args: dict, key: str) -> dict:
        ref = "refs/heads/" + self._branch(case_id)
        argv = ["git", "-C", str(self.kit.repo.root), "push", "--porcelain",
                f"--force-with-lease={ref}:{self._lease(case_id)}", self.doc["remote"], f"{args['commit']}:{ref}"]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        flags = [line.split("\t") for line in done.stdout.splitlines() if "\t" in line]
        mine = next((f for f in flags if len(f) >= 2 and f[1].endswith(":" + ref)), None)
        if mine and mine[0] in ("*", " ", "="):
            return {"destination": f"git:{self.doc['remote']}:{ref}", "external_id": args["commit"]}
        if mine and mine[0] == "!":
            # The remote refused this ref update; it did not write it.
            raise NoWrite(412, "remote_moved")
        raise RuntimeError("push outcome unknown")

    def status(self, case_id: str, args: dict, key: str) -> tuple[str, dict]:
        ref = "refs/heads/" + self._branch(case_id)
        head = self._remote_head(ref)
        if head == args["commit"]:
            return "applied", {"external_id": args["commit"], "destination": f"git:{self.doc['remote']}:{ref}"}
        return "absent", {"remote_head": head}


class GitHubPullRequest(_Publisher):
    def _request(self, method: str, path: str, body: dict | None = None):
        token = os.getenv(self.doc["token_env"])
        if not token:
            raise NoWrite(428, "missing_credential")
        url = self.doc["api"].rstrip("/") + path
        guard_request(url)
        request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                         headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                                  "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        with HTTP.open(request, timeout=self.timeout) as response:
            raw = response.read(MAX_OUTPUT + 1)
        if len(raw) > MAX_OUTPUT:
            raise ValueError("response too large")
        return json.loads(raw)

    def _find(self, case_id: str, commit: str) -> dict | None:
        owner = self.doc["repository"].split("/")[0]
        head = quote(f"{owner}:{self._branch(case_id)}", safe=":")
        pulls = self._request("GET", f"/repos/{self.doc['repository']}/pulls?head={head}&state=all&per_page=100")
        if not isinstance(pulls, list):
            raise ValueError("unexpected pull request listing")
        return next((p for p in pulls if isinstance(p, dict) and (p.get("head") or {}).get("sha") == commit), None)

    @staticmethod
    def _output(doc, pull: dict) -> dict:
        return {"destination": "github:" + doc["repository"], "external_id": str(pull["number"]),
                "url": str(pull.get("html_url", ""))}

    def apply(self, case_id: str, args: dict, key: str) -> dict:
        found = self._find(case_id, args["commit"])
        if found:
            return self._output(self.doc, found)
        thread = self._thread(case_id)
        view = next(v for v in thread._views() if v["authoritative"])
        checks = ", ".join(f"{c['name']} {c['state']}" for c in view["evaluation"]["checks"])
        body = (f"{thread.objective}\n\nAccepted in SyberLabs thread `{case_id}` as candidate `{view['id']}` "
                f"(commit `{args['commit']}`).\nHost checks on that tree: {checks}.\n")
        try:
            created = self._request("POST", f"/repos/{self.doc['repository']}/pulls",
                                    {"title": thread.objective[:200], "head": self._branch(case_id),
                                     "base": self.doc["base"], "body": body})
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404, 422):
                # GitHub validates and authorizes before creating a pull request.
                raise NoWrite(409, f"github_{exc.code}") from None
            raise
        return self._output(self.doc, created)

    def status(self, case_id: str, args: dict, key: str) -> tuple[str, dict]:
        found = self._find(case_id, args["commit"])
        if found:
            return "applied", self._output(self.doc, found)
        return "absent", {}


class Command(_Publisher):
    def _run(self, argv: list[str], case_id: str, args: dict, key: str) -> tuple[int | None, str]:
        env = {**os.environ, "SYBERLABS_IDEMPOTENCY_KEY": key, "SYBERLABS_COMMIT": args["commit"],
               "SYBERLABS_THREAD": case_id}
        with self.kit.repo.worktree(args["commit"]) as tree, tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(argv, cwd=tree, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL, start_new_session=hasattr(os, "setsid"))
            deadline = time.monotonic() + self.timeout
            while proc.poll() is None:
                if time.monotonic() > deadline or os.fstat(out.fileno()).st_size > MAX_OUTPUT:
                    proc.kill()
                    proc.wait()
                    return None, ""
                time.sleep(0.02)
            out.seek(0)
            return proc.returncode, out.read(MAX_OUTPUT).decode(errors="replace")

    @staticmethod
    def _last_json(text: str) -> dict:
        lines = [line for line in text.strip().splitlines() if line.strip()]
        found = json.loads(lines[-1]) if lines else None
        if not isinstance(found, dict):
            raise ValueError("the command must print a JSON object as its last line")
        return found

    def apply(self, case_id: str, args: dict, key: str) -> dict:
        code, text = self._run(self.doc["argv"], case_id, args, key)
        if code in self.doc.get("no_write_exit_codes", []):
            raise NoWrite(409, f"command_exit_{code}")
        if code != 0:
            raise RuntimeError("publish command outcome unknown")
        found = self._last_json(text)
        if not isinstance(found.get("external_id"), str) or not found["external_id"]:
            raise ValueError("publish command must report external_id")
        return {"destination": "command:" + Path(self.doc["argv"][0]).name, "external_id": found["external_id"],
                "url": str(found.get("url", ""))}

    def status(self, case_id: str, args: dict, key: str) -> tuple[str, dict]:
        code, text = self._run(self.doc["status_argv"], case_id, args, key)
        if code != 0:
            return "unknown", {}
        found = self._last_json(text)
        if found.get("state") == "applied" and isinstance(found.get("external_id"), str) and found["external_id"]:
            return "applied", {"external_id": found["external_id"]}
        return ("absent", {}) if found.get("state") == "absent" else ("unknown", {})


def executor(kit, doc: dict):
    return {"git_push": GitPush, "github_pull_request": GitHubPullRequest, "command": Command}[doc["effect"]](kit, doc)
