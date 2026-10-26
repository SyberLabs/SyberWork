"""The GitHits launcher's entry: run the host checkout's ``syberlabs githits`` CLI, never a worktree's.

    <host python> -I <host checkout>/syberlabs/githits_cli_entry.py search "..." --target pypi:requests@2.32.3

``ClaudeCodeProvider(githits=True)`` writes a per-call launcher outside the candidate's worktree that runs exactly
this file by absolute path under ``-I`` (no current directory, no ``PYTHONPATH``, no user site on the import path).
This module puts the checkout that holds it first on ``sys.path``, imports ``syberlabs`` from there, refuses to run
if the package came from anywhere else, and hands its arguments to ``syberlabs githits``. So the code that sends a
lookup, filters its results and signs its record is the host's, whatever the candidate's tree holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # <host checkout>/syberlabs


def main(argv: list[str] | None = None) -> int:
    root = str(HERE.parent)
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    import syberlabs
    from syberlabs import cli

    if Path(syberlabs.__file__).resolve().parent != HERE:
        sys.stderr.write("error: githits_launcher: syberlabs was not imported from the host checkout\n")
        return 1
    return cli.main(["githits", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
