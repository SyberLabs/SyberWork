"""A stand-in proposer for the autonomy thin slice: the process boundary a model adapter would sit behind.

Usage: ``standin_proposer.py REPO_ROOT [--break | --outside]``. It reads the search request on stdin (the
thread's objective, its scope and the file list), takes the first scope path, reads that file from REPO_ROOT's
working tree, and answers one candidate that appends the feature's delivery marker (``# F1 delivered``) to it.
``--break`` appends a line that is not Python instead, so the visible check fails; ``--outside`` edits a path
outside the scope, which the host refuses as out of scope. No model, no network, no cost.
"""

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    root = Path(argv[0])
    mode = argv[1] if len(argv) > 1 else ""
    request = json.load(sys.stdin)
    feature = request["objective"].split(":", 1)[0].strip()
    path = request["scope"][0]
    text = (root / path).read_text(encoding="utf-8")
    if mode == "--break":
        changes = {path: text.rstrip("\n") + "\nthis line is not python\n"}
    elif mode == "--outside":
        changes = {"fxapp/main.py": "# edited outside the feature's scope\n"}
    else:
        changes = {path: text.rstrip("\n") + f"\n\n# {feature} delivered\n"}
    json.dump({"candidates": [{"changes": changes, "message": f"{feature}: delivery marker", "signal": None}],
               "recommended": [0]}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
