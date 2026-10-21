"""The evaluation worktree is removed even when a file in it is briefly held (a Windows race)."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syberlabs.gitspace import GitError, Repo


def _repo(parent: Path) -> Repo:
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(["git", "-C", str(parent), "-c", "user.name=t", "-c", "user.email=t@example.test",
                    "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    return Repo(parent)


class WorktreeRemoval(unittest.TestCase):
    def test_a_briefly_held_worktree_is_removed_after_retries(self):
        with tempfile.TemporaryDirectory() as home:
            repo = _repo(Path(home))
            head = repo.git("rev-parse", "HEAD").decode().strip()
            real = repo.git
            calls = {"remove": 0}

            def flaky(*args, **kwargs):
                if args[:2] == ("worktree", "remove"):
                    calls["remove"] += 1
                    if calls["remove"] <= 2:
                        raise GitError("git worktree: error: failed to delete 'tree': Permission denied")
                return real(*args, **kwargs)

            with patch.object(repo, "git", flaky), patch("syberlabs.gitspace.time.sleep") as sleep:
                with repo.worktree(head) as tree:
                    self.assertTrue(tree.is_dir())
            self.assertEqual(calls["remove"], 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertFalse(tree.exists())
            self.assertNotIn(str(tree), repo.git("worktree", "list").decode())

    def test_a_worktree_that_never_releases_does_not_fail_the_evaluation(self):
        with tempfile.TemporaryDirectory() as home:
            repo = _repo(Path(home))
            head = repo.git("rev-parse", "HEAD").decode().strip()
            real = repo.git

            def stuck(*args, **kwargs):
                if args[:2] == ("worktree", "remove"):
                    raise GitError("git worktree remove: Permission denied")
                return real(*args, **kwargs)

            with patch.object(repo, "git", stuck), patch("syberlabs.gitspace.time.sleep"):
                with repo.worktree(head) as tree:
                    pass  # no exception escapes the context manager
            # Nothing held the tree in this test, so the directory removal after the last attempt
            # succeeded and the prune that follows it dropped the registration.
            self.assertFalse(tree.exists())
            self.assertNotIn(str(tree), repo.git("worktree", "list").decode())


if __name__ == "__main__":
    unittest.main()
