"""One operator recipe around pilot_dry_run; preparation is offline by default.

python scripts/githits_engineering_smoke.py --work DIR --factory-commit SHA --run-approved --claude PATH
Mateo executes --run-approved only after explicit provider/run/spend approval.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__:
    from scripts import pilot_dry_run as pilot
else:
    import pilot_dry_run as pilot

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "examples" / "githits-engineering-smoke"
MODEL = "claude-sonnet-5-5"
CLI_VERSION = "2.1.312"  # exact: the host and the pilot VM run it; recorded in plan.json and manifest.json


def prepare(work):
    work.mkdir(parents=True, exist_ok=False)
    repo = work / "fixture"
    shutil.copytree(PACK / "fixture", repo)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Engineering smoke", "GIT_COMMITTER_NAME": "Engineering smoke",
           "GIT_AUTHOR_EMAIL": "smoke@syberlabs.invalid", "GIT_COMMITTER_EMAIL": "smoke@syberlabs.invalid",
           "GIT_AUTHOR_DATE": "2026-10-26T00:00:00Z", "GIT_COMMITTER_DATE": "2026-10-26T00:00:00Z"}
    for args in (["init", "-q", "-b", "main"], ["config", "core.autocrlf", "false"],
                 ["config", "commit.gpgsign", "false"], ["add", "."],
                 ["commit", "-qm", "Freeze synthetic engineering smoke base"]):
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)
    plan = json.loads((PACK / "plan.json").read_text(encoding="utf-8"))
    plan["checks"] = {"engineering-smoke": {"argv": [sys.executable, str(PACK / "check.py")], "timeout_seconds": 30}}
    pilot.validate_plan(plan)
    path = work / "plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    manifest = {"purpose": "ENGINEERING SMOKE, synthetic readiness only; formal pack unchanged",
                "factory": pilot.host_checkout(), "fixture_base": pilot.git(repo, "rev-parse", "HEAD"),
                "plan_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "check_sha256": hashlib.sha256((PACK / "check.py").read_bytes()).hexdigest(),
                "skill_sha256": pilot.claude_code.githits_skill()[1], "cli_version": CLI_VERSION}
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return repo, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="new private directory; never reused")
    parser.add_argument("--factory-commit", required=True, help="exact approved factory SHA")
    parser.add_argument("--run-approved", action="store_true", help="Mateo only, after explicit live/spend approval")
    parser.add_argument("--claude", help=f"Claude Code {CLI_VERSION} executable (else the existing configured command)")
    args = parser.parse_args(argv)
    factory = pilot.host_checkout()
    if args.factory_commit != factory["commit"] or (args.run_approved and factory["dirty"]):
        print("REFUSED: factory commit differs from the requested pin")
        return 2
    repo, plan = prepare(args.work.resolve())
    print("ENGINEERING SMOKE prepared: synthetic tasks, no formal pilot data;", plan)
    if not args.run_approved:
        print("No network or provider call was made. Live execution requires Mateo's explicit approval.")
        return 0
    if args.claude:
        os.environ[pilot.claude_code.BIN_ENV] = str(Path(args.claude).resolve())
    command = pilot.claude_code.claude_executable()
    version = subprocess.run([*command, "--version"], capture_output=True, timeout=30)
    found = pilot.claude_code.version_tuple(version.stdout.decode("utf-8", "replace")) if not version.returncode else None
    if found != tuple(int(part) for part in CLI_VERSION.split(".")):
        print("REFUSED: the frozen smoke plan requires Claude Code", CLI_VERSION, "exactly; found",
              ".".join(map(str, found)) if found else "no readable version")
        return 2
    print("Claude Code", CLI_VERSION, "(the exact pin)")
    if "--model" in command:
        print("REFUSED: remove the command's model override; the smoke recipe pins ANTHROPIC_MODEL")
        return 2
    os.environ.update(ANTHROPIC_MODEL=MODEL, CLAUDE_CODE_EFFORT_LEVEL="medium",
                      CLAUDE_CODE_PROMPT_CACHE_TTL="5m", CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL="5m",
                      FORCE_PROMPT_CACHING_5M="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return pilot.main(["--repo", str(repo), "--plan", str(plan), "--out", str(args.work.resolve() / "receipts"),
                       "--budget", "8", "--seed", "7", "--actor", "mateo", "--reviewer", "blind-engineering"])


if __name__ == "__main__":
    raise SystemExit(main())
