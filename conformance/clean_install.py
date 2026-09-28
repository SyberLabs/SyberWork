"""Release gate: build the wheel, install it in a clean venv, run the public examples.

Unit tests run inside the checkout, where ``PYTHONPATH=.`` can hide a
packaging mistake. This script runs each example from a temporary directory
against the installed wheel only, and checks that the imported modules come
from the venv, not from this repository.

    python -m conformance.clean_install [--python python3.12] [--keep]

Needs ``pip`` and network access to fetch setuptools for the isolated build,
or ``--no-build-isolation`` to use the setuptools already installed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ("release_gate.py", "access_review.py", "build_thread.py", "evolve.py")


def run(argv: list[str], *, cwd: Path, env: dict | None = None) -> str:
    completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if completed.returncode != 0:
        raise SystemExit(f"failed: {' '.join(map(str, argv))}\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", default=sys.executable, help="interpreter for the clean venv")
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    options = parser.parse_args()

    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="syberlabs-clean-"))
    try:
        dist = work / "dist"
        build = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", str(dist), str(ROOT)]
        if options.no_build_isolation:
            build.insert(4, "--no-build-isolation")
        run(build, cwd=work)
        wheels = sorted(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"expected one wheel, found {wheels}")
        venv = work / "venv"
        run([options.python, "-m", "venv", str(venv)], cwd=work)
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", str(wheels[0])], cwd=work)

        outside = work / "outside"
        outside.mkdir()
        # The venv comes first; the rest of PATH is kept so the Build Thread example can find git.
        env = {"PATH": os.pathsep.join([str(python.parent), os.environ.get("PATH", "")]), "HOME": str(outside),
               "PYTHONNOUSERSITE": "1"}
        origin = run([str(python), "-c", "import syberlabs, syberwork; print(syberlabs.__file__); print(syberwork.__file__)"],
                     cwd=outside, env=env).split()
        if any(str(ROOT) in path or "site-packages" not in path for path in origin):
            raise SystemExit(f"imports did not come from the installed wheel: {origin}")
        for name in EXAMPLES:
            copy = outside / name
            shutil.copy(ROOT / "examples" / name, copy)
            print(run([str(python), str(copy)], cwd=outside, env=env).strip().splitlines()[-1])
        run([str(python), "-c", "import syberlabs.inspector as i; "
             "assert all((i.STATIC / name).is_file() for name in i.ASSETS), 'inspector assets missing from the wheel'"],
            cwd=outside, env=env)
        script = python.parent / ("syberlabs.exe" if sys.platform == "win32" else "syberlabs")
        print(run([str(script), "--help"], cwd=outside, env=env).splitlines()[0])
        print(f"wheel={wheels[0].name} python={run([str(python), '-V'], cwd=outside, env=env).strip()} "
              f"seconds={time.monotonic() - started:.1f}")
    finally:
        if options.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
