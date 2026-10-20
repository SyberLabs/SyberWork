# Running a Build Thread on a Node, Rust or Go repository

A Build Thread does not care what language a repository is written in. It cares that the contract names checks that can start, that a fresh worktree can run them, and that their output fits the record. This page is what a consumer that is not a Python project needs to know, with the second consumer, Flyspace (a TypeScript repository, Windows, 2026-10-18), as the worked example. [BUILD_THREAD.md](BUILD_THREAD.md) is the general reference.

## What the kit does for you

- **Absolute tools.** `syberlabs init` resolves `npm`, `cargo` and `go` with `shutil.which` when it writes the contract and records the absolute path, as it already does for Python with `sys.executable`. On Windows that is `C:\Program Files\nodejs\npm.cmd`, not `npm`: checks run as argv without a shell, and `CreateProcess` cannot start a bare `npm`. A tool that is not on PATH gets no check and a note on stderr; name one yourself.
- **An install step.** A `package.json` gets two checks, `install` and `tests`. Every candidate is checked in a detached worktree the kit creates and removes, which has no `node_modules`, no `target/`, no build output. `install` is `npm ci --no-audit --no-fund` with a lockfile and `npm install --no-audit --no-fund` without one.
- **Name order, and `setup_failed`.** Checks run in sorted name order (`install` < `tests` < `typecheck`), because a contract is stored as canonical JSON and the runner sorts. A check named `setup` or `install` is the worktree's preparation: when it does not pass, the checks after it are not started and are recorded `error` with the tail `setup_failed`, so the verdict reads `candidate_check_failed:install` rather than three unrelated failures. For Rust and Go, `cargo test` and `go test` fetch what they need themselves; add a `setup` check only when something must be generated first.
- **A cap that fits a real runner.** `evaluation.max_output_bytes` defaults to 1,048,576, the ceiling. The earlier default, 65,536, was less than vitest prints for a medium suite (about 117 KB with colour and the per-file lines), so a passing suite was recorded `error` with `output exceeded 65536 bytes`. A contract may still declare a lower cap; the 4 KB tail in the record is unchanged.
- **Caches survive.** The checks' environment is scrubbed (`HOME`, `TMP` and `TEMP` are a temporary directory), but it keeps where package managers cache downloads: `APPDATA`, `LOCALAPPDATA` and `USERPROFILE` on Windows, `XDG_CACHE_HOME` on POSIX, and `npm_config_cache`, `CARGO_HOME`, `RUSTUP_HOME`, `GOPATH`, `GOCACHE` and `GOMODCACHE` whenever they are set. Set the explicit ones in the shell that runs `syberlabs` if your toolchain lives somewhere unusual: a rustup toolchain under a scrubbed `HOME` is found only through `RUSTUP_HOME` and `CARGO_HOME`. Nothing else crosses: no `NODE_AUTH_TOKEN`, no `SSH_AUTH_SOCK`, no `AWS_*`.
- **Timeouts end the tree.** A check over its `timeout_seconds` is `timed_out`, and on Windows the whole process tree is ended with `taskkill /T /F` (npm starts `node`, which starts the runner); on POSIX the process group is killed. Give `install` its own generous timeout; a cold `npm ci` on a large tree can take minutes.
- **The console survives Unicode.** `syberlabs` reconfigures its stdout and stderr to replace characters the console cannot encode, so the `✓` in a test runner's tail prints as `?` on a cp1252 console instead of raising after the verdict was already recorded.

## The worked example: Flyspace

Flyspace is a TypeScript repository with `vitest` and `tsc`. Its thread setup script writes the contract, the policy and the publish actions after `python -m syberlabs init`. This is the contract that worked, with the kit's own resolution standing in for the hand-resolved path the first attempt needed:

```python
"""Configure the Flyspace clone's Build Thread. Run from the clone root after `python -m syberlabs init`."""
import json
import shutil
from pathlib import Path

home = Path(".syberlabs")
contracts = home / "contracts"
assert contracts.is_dir(), "run `python -m syberlabs init` first, from the clone root"
npm = shutil.which("npm.cmd") or shutil.which("npm")     # what detect_checks does for init's own contract
assert npm and Path(npm).is_absolute(), f"need an absolute npm, got {npm!r}"

contract = {
    "id": "repo-change",
    "version": 3,
    "title": "Change this repository under a recorded act class",
    "inputs": {"objective": "string", "act_class": "string"},
    "actions": {
        "accept_change": {},
        "push_branch": {"requires_effect": "accept_change",
                        "required_facts": [{"key": "accepted", "source": "git", "verified": True}],
                        "arguments": {"commit": "fact:accepted.commit"}},
        "open_pull_request": {"requires_effect": "push_branch",
                              "required_facts": [{"key": "accepted", "source": "git", "verified": True}],
                              "arguments": {"commit": "fact:accepted.commit"}},
    },
    "acceptance": [{"id": "accepted", "kind": "effect", "action": "accept_change"}],
    "evolution": {
        "scope": {"paths": ["src", "test", "docs"], "exclude": [], "max_files": 30, "max_diff_bytes": 400_000},
        "operators": ["patch"],
        "budget": {"max_candidates": 12, "max_evaluations": 24, "max_seconds": 7200},
        "evaluation": {
            "checks": {
                "install": {"argv": [npm, "ci", "--no-audit", "--no-fund"], "timeout_seconds": 1200},
                "tests": {"argv": [npm, "test"], "timeout_seconds": 1200},
                "typecheck": {"argv": [npm, "run", "typecheck"], "timeout_seconds": 1200},
            },
            "required": ["install", "tests", "typecheck"],
            "max_age_seconds": 10800,
            "max_output_bytes": 1_048_576,
        },
        "promotion": {"action": "accept_change", "roles": ["developer"]},
    },
}
policy = {"version": 2, "actions": {name: {"roles": ["developer"]} for name in contract["actions"]}}
actions = {
    "push_branch": {"kind": "local", "effect": "git_push", "remote": "origin"},
    "open_pull_request": {"kind": "local", "effect": "github_pull_request", "api": "https://api.github.com",
                          "repository": "SyberLabs/Flyspace", "base": "main", "token_env": "GITHUB_TOKEN"},
}
(contracts / "repo-change.v3.json").write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
(home / "policy.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
(home / "actions.json").write_text(json.dumps(actions, indent=2) + "\n", encoding="utf-8")
print("wrote repo-change.v3.json, policy.json (v2), actions.json; checks run with", npm)
```

Then, from the clone root, with the kit on `PYTHONPATH` (or installed):

```sh
python -m syberlabs init
python thread_setup.py
python -m syberlabs --actor you start "Add retry to the upload client" --contract repo-change.v3 --class R --paths src test docs
# make the change, then
python -m syberlabs --actor you propose --from-worktree --name claude --revision <model>
python -m syberlabs --actor you check c1            # install, tests, typecheck on c1's exact tree
python -m syberlabs --actor you accept c1
python -m syberlabs --actor you publish push_branch
GITHUB_TOKEN="$(gh auth token)" python -m syberlabs --actor you publish open_pull_request
```

What the Flyspace run taught, and where it now lives:

| Seen on Flyspace (SyberWork `6651714`) | Now |
| --- | --- |
| `npm` in the generated contract could not start on Windows (`npm.cmd`) | `init` writes the absolute path; an unresolved tool writes no check and says so |
| A fresh worktree had no `node_modules`; an `install` check had to be hand-written | `init` writes `install` for a `package.json`; `setup`/`install` is the documented first check and gates the rest |
| vitest's ~117 KB made a passing suite `error` under the 65,536 default | the default cap is 1,048,576; a declared cap still binds |
| The cp1252 console raised on `✓` after the verdict was recorded | stdout and stderr replace what they cannot encode |
| npm's cache was discarded per candidate (85 to 103 s reinstalls); a timeout left npm's children alive | the cache allowance in the environment; `taskkill /T /F` on Windows, the process group on POSIX |

The `max_output_bytes` line in the example is now the default and may be left out; it is kept because an explicit cap is what the record shows a reader.

## Rust and Go

A `Cargo.toml` gets `tests: [<cargo>, "test", "--quiet"]` and a `go.mod` gets `tests: [<go>, "test", "./..."]`, both by absolute path, 900 seconds. Cargo and Go download into their own caches, which the environment keeps when `CARGO_HOME` / `RUSTUP_HOME` and `GOPATH` / `GOMODCACHE` / `GOCACHE` are set in the shell that runs the kit; without them, `HOME` is temporary and a rustup-managed `cargo` cannot find its toolchain. Set them, or name a toolchain's own `cargo` binary in the contract. A `setup` check (a generator, `cargo fetch`, `go generate ./...`) runs first by name order and gates the rest exactly as `install` does for Node.

## Limits that stay

- The kit resolves tools when `init` writes the contract. A toolchain that moves afterwards needs a new contract version; a published version is immutable by design.
- The check environment is scrubbed, not sandboxed. The project's own commands run with your permissions against candidate code; a `postinstall` script is code.
- Output over the cap is still `error`, by design: a record that holds the whole of an unbounded log is not a record. Quiet your runner (`vitest run --reporter=dot`, `cargo test --quiet`) if 1 MB is not enough.
- The setup step is a naming convention, not a schema field (`spec/GAPS.md`, 31). A contract that calls its install step `deps` gets no `setup_failed` gating; the later checks fail on their own.
