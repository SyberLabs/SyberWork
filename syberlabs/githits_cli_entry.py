"""The GitHits launcher's entry: run the host checkout's ``syberlabs githits`` CLI, never a worktree's.

    <host python> -I <host checkout>/syberlabs/githits_cli_entry.py search "..." --target pypi:requests@2.32.3

``ClaudeCodeProvider(githits=True)`` writes a per-call launcher outside the candidate's worktree that runs exactly
this file by absolute path under ``-I`` (no current directory, no ``PYTHONPATH``, no user site on the import path).
This module puts the checkout that holds it first on ``sys.path``, imports ``syberlabs`` from there, refuses to run
if the package came from anywhere else, and hands its arguments to the host's GitHits client. So the code that sends
a lookup, filters its results and signs its record is the host's, whatever the candidate's tree holds.

It accepts only the five lookups (``githits.LOOKUPS``: search, example, read, list, grep), parsed by
``githits.launcher_parser``. ``doctor``, ``--help`` and anything else exit 2 with a usage message before a client
exists, so they write no record; ``doctor`` is the operator's, through ``syberlabs githits doctor``. ``example`` has
no ``--license-mode`` here and always sends ``license_mode: strict``.

Before it prints anything it runs ``cli.survive_unicode()``, as ``syberlabs`` itself does: under ``-I`` on Windows a
piped stdout is cp1252, and a result holding a character outside it (a box-drawing line, an emoji) must print with
that character replaced, not raise. The client delivers the output before it writes the call's record, so a print
that still fails is recorded ``error`` and never follows an ``ok`` record (``githits.Client.call``, ``deliver``).

A ``SIGTERM`` (how a Bash tool ends a command past its timeout) becomes ``SystemExit`` once a lookup is parsed, so
a launcher ended after its request left still writes that call's record (``githits.Client.call`` records any
interruption, during the delivery of its output too) before it exits. A ``SIGKILL`` cannot be caught: that call
stays unrecorded, and the host flags ``githits_unrecorded``.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # <host checkout>/syberlabs


def _terminated(signum, frame):
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    root = str(HERE.parent)
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    import syberlabs
    from syberlabs import cli, githits

    if any(Path(module.__file__).resolve().parent != HERE for module in (syberlabs, cli, githits)):
        sys.stderr.write("error: githits_launcher: syberlabs was not imported from the host checkout\n")
        return 1
    cli.survive_unicode()  # before any output: a cp1252 pipe replaces what it cannot encode instead of raising
    words = list(sys.argv[1:] if argv is None else argv)
    parser = githits.launcher_parser()
    if not words or words[0] not in githits.LOOKUPS:
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"{parser.prog}: error: the launcher runs only {', '.join(githits.LOOKUPS)}\n")
        return 2
    try:
        args = parser.parse_args(words)
    except SystemExit as exc:  # argparse: a usage error (2), or an operation's --help (0), which is no lookup either
        return 2 if exc.code in (0, None) else exc.code
    try:
        signal.signal(signal.SIGTERM, _terminated)
    except (AttributeError, ValueError, OSError):  # no SIGTERM here, or not the main thread: run as before
        pass
    return githits.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
