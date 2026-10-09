"""Git plumbing for candidates. Standard library and the ``git`` executable only.

Candidates are written with a temporary index and ``commit-tree``. Nothing here
changes the developer's working tree, index, or checked-out branch. Candidate
commits live under ``refs/syberlabs/candidates/``, outside ``refs/heads``, so they
are not branches and are not pushed by default refspecs. The only authoritative
write is ``update_ref`` with an expected old value, which Git applies atomically
or not at all.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from syberlabs.errors import Rejected

CANDIDATE_REFS = "refs/syberlabs/candidates"
IDENTITY = {
    "GIT_AUTHOR_NAME": "SyberLabs candidate",
    "GIT_AUTHOR_EMAIL": "candidate@syberlabs.invalid",
    "GIT_COMMITTER_NAME": "SyberLabs candidate",
    "GIT_COMMITTER_EMAIL": "candidate@syberlabs.invalid",
}
WRITER = {"GIT_COMMITTER_NAME": "SyberLabs", "GIT_COMMITTER_EMAIL": "accept@syberlabs.invalid"}
MAX_FILE_BYTES = 1_048_576


class GitError(Rejected):
    def __init__(self, detail: str):
        super().__init__("git_failed", detail[:300])


class Repo:
    """One local repository, addressed by its top-level directory."""

    def __init__(self, path: str | Path = "."):
        path = Path(path).resolve()
        try:
            top = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=30, env=self._env())
        except FileNotFoundError:
            raise Rejected("git_missing", "the git executable is not on PATH") from None
        if top.returncode != 0:
            raise Rejected("not_a_repository", f"{path} is not inside a Git working tree")
        self.root = Path(top.stdout.strip())
        object_format = self.git("rev-parse", "--show-object-format", ok=(0, 128)).decode().strip()
        self.zero = "0" * (64 if object_format == "sha256" else 40)

    @staticmethod
    def _env(extra: dict | None = None) -> dict:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
        env.update(extra or {})
        return env

    def git(self, *args: str, input: bytes | None = None, env: dict | None = None, ok=(0,), timeout=120) -> bytes:
        argv = ["git", "-C", str(self.root), "-c", "core.fsmonitor=false", *args]
        done = subprocess.run(argv, input=input, capture_output=True, env=self._env(env), timeout=timeout)
        if done.returncode not in ok:
            raise GitError(f"git {args[0]}: {done.stderr.decode(errors='replace').strip()}")
        return done.stdout

    def _text(self, *args: str, **kwargs) -> str:
        return self.git(*args, **kwargs).decode().strip()

    # Reads

    def rev(self, name: str) -> str | None:
        found = self.git("rev-parse", "--verify", "--quiet", "--end-of-options", name + "^{commit}", ok=(0, 1))
        return found.decode().strip() or None

    def tree(self, commit: str) -> str:
        return self._text("rev-parse", "--verify", "--end-of-options", commit + "^{tree}")

    def current_branch(self) -> str | None:
        name = self.git("symbolic-ref", "--quiet", "HEAD", ok=(0, 1)).decode().strip()
        return name or None

    def read_ref(self, ref: str) -> str | None:
        found = self.git("rev-parse", "--verify", "--quiet", "--end-of-options", ref, ok=(0, 1))
        return found.decode().strip() or None

    def files(self, commit: str) -> list[str]:
        raw = self.git("ls-tree", "-r", "-z", "--name-only", "--full-tree", commit)
        return [item.decode() for item in raw.split(b"\0") if item]

    def entry(self, commit: str, path: str) -> tuple[str, str, int] | None:
        """(mode, blob id, size) of one path, or None."""
        raw = self.git("ls-tree", "-z", "--long", "--full-tree", commit, "--", path)
        for item in raw.split(b"\0"):
            if not item:
                continue
            meta, _, name = item.partition(b"\t")
            mode, kind, blob, size = meta.split()
            if name.decode() == path and kind == b"blob":
                return mode.decode(), blob.decode(), int(size)
        return None

    def read(self, commit: str, path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes | None:
        found = self.entry(commit, path)
        if found is None or found[2] > max_bytes or found[0] == "120000":
            return None
        return self.git("cat-file", "blob", found[1])

    def is_ancestor(self, older: str, newer: str) -> bool:
        done = subprocess.run(["git", "-C", str(self.root), "merge-base", "--is-ancestor", older, newer],
                              capture_output=True, env=self._env(), timeout=60)
        return done.returncode == 0

    def merge_base(self, a: str, b: str) -> str | None:
        found = self.git("merge-base", a, b, ok=(0, 1)).decode().strip()
        return found or None

    def changed_paths(self, a: str, b: str) -> list[str]:
        raw = self.git("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", a, b)
        return sorted(item.decode() for item in raw.split(b"\0") if item)

    def diff(self, a: str, b: str, max_bytes: int = 200_000) -> tuple[str, int, str]:
        """Unified diff text up to ``max_bytes``, total size, and SHA-256 of the whole diff."""
        argv = ["git", "-C", str(self.root), "diff", "--no-color", "--no-ext-diff", "--no-renames", "--binary", a, b]
        sha, total, kept = hashlib.sha256(), 0, bytearray()
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self._env()) as proc:
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                sha.update(chunk)
                total += len(chunk)
                if len(kept) < max_bytes:
                    kept.extend(chunk[: max_bytes - len(kept)])
        if proc.returncode != 0:
            raise GitError("git diff failed")
        return kept.decode(errors="replace"), total, sha.hexdigest()

    def checked_out(self, ref: str) -> bool:
        raw = self.git("worktree", "list", "--porcelain")
        return any(line == f"branch {ref}" for line in raw.decode().splitlines())

    def working_changes(self, base: str) -> list[str]:
        """Paths that differ between ``base`` and the working tree, plus untracked files."""
        tracked = self.git("diff", "--name-only", "-z", "--no-renames", base)
        untracked = self.git("ls-files", "--others", "--exclude-standard", "-z")
        return sorted({item.decode() for item in (tracked + b"\0" + untracked).split(b"\0") if item})

    # Writes that never touch the working tree

    def commit(self, parents: list[str], changes: dict[str, bytes | None], message: str) -> str:
        """Write a commit whose tree is ``parents[0]`` plus ``changes``. None deletes a path."""
        with tempfile.TemporaryDirectory(prefix="syberlabs-index-") as folder:
            env = {"GIT_INDEX_FILE": str(Path(folder) / "index"), **IDENTITY}
            self.git("read-tree", parents[0], env=env)
            for path, data in sorted(changes.items()):
                if data is None:
                    self.git("update-index", "--force-remove", "--", path, env=env)
                    continue
                # ``--path`` applies the clean filters this path would get from ``git add``
                # (``core.autocrlf``, ``.gitattributes``). Without it, a Windows working tree's
                # CRLF bytes are stored verbatim: every line of every touched file reads as
                # changed, the diff exceeds the contract's size limits, and an accepted
                # candidate would commit CRLF into a repository that stores LF.
                blob = self._text("hash-object", "-w", "--path", path, "--stdin", input=data)
                existing = self.entry(parents[0], path)
                mode = existing[0] if existing and existing[0] in ("100644", "100755") else "100644"
                self.git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
            tree = self._text("write-tree", env=env)
        argv = ["commit-tree", tree]
        for parent in parents:
            argv += ["-p", parent]
        return self._text(*argv, input=message.encode(), env=IDENTITY)

    def update_ref(self, ref: str, new: str, old: str | None, message: str) -> None:
        """Atomic compare-and-swap. ``old=None`` means the ref must not exist yet."""
        self.git("update-ref", "-m", message, ref, new, old or self.zero, env=WRITER)

    def delete_ref(self, ref: str) -> None:
        self.git("update-ref", "-d", ref)

    def refs(self, prefix: str) -> dict[str, str]:
        raw = self.git("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        return dict(line.split(" ", 1) for line in raw.decode().splitlines() if line)

    def merge_text(self, base: str, ours: str, theirs: str) -> tuple[str, int]:
        """Three-way merge with ``git merge-file``. Returns text and conflict count."""
        with tempfile.TemporaryDirectory(prefix="syberlabs-merge-") as folder:
            paths = []
            for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
                path = Path(folder) / name
                path.write_bytes(text.encode())
                paths.append(str(path))
            done = subprocess.run(["git", "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "theirs", *paths],
                                  capture_output=True, env=self._env(), timeout=60)
        if done.returncode < 0 or done.returncode > 127:
            raise GitError("git merge-file failed")
        return done.stdout.decode(errors="replace"), done.returncode

    @contextmanager
    def worktree(self, commit: str):
        """A detached temporary checkout of ``commit`` outside the repository. Hooks are disabled."""
        parent = Path(tempfile.mkdtemp(prefix="syberlabs-eval-"))
        hooks = parent / "no-hooks"
        hooks.mkdir()
        target = parent / "tree"
        try:
            self.git("-c", f"core.hooksPath={hooks}", "worktree", "add", "--detach", "--quiet", str(target), commit)
            yield target
        finally:
            self.git("worktree", "remove", "--force", str(target), ok=(0, 128))
            self.git("worktree", "prune", ok=(0, 128))
            shutil.rmtree(parent, ignore_errors=True)
