# Admitting a pull request in CI

Any pull request, from any agent or person, can be run through a Build Thread by the repository that receives it. The author needs nothing from SyberWork: no `init`, no contract, no kit on their machine. The workflow `admit` (`.github/workflows/admit.yml`) does this for this repository; what it runs is the composite action `.github/actions/admit` and `scripts/admit.sh`, and another repository can run the same two.

## What it does

For a pull request from base `B` to head `H`, the job:

1. checks out SyberWork at `H` as the tool, and the repository at `B`, with full history, as the subject;
2. starts a thread at `B` in a temporary worktree: `syberlabs --actor "$GITHUB_ACTOR" start "<pull request title>" --class R --paths .`, on a contract `scripts/admit_contract.py` writes for the run (one action, `accept_change`; the checks are this repository's unit tests and conformance, run by the interpreter's absolute path);
3. brings `H`'s tree into that worktree without moving HEAD (`git checkout H -- .`, then removes the files `H` deletes) and stops with `tree_mismatch` if the worktree does not equal `H`;
4. proposes the worktree as candidate `c1` with `--name <author login> --revision <H, short>`, so the record names the pull request's author, agent or person, as the provider; then compares `c1`'s tree id with `H`'s and stops with `tree_mismatch` if they differ;
5. `check c1`: the contract's checks run in a detached worktree of `c1` with a scrubbed environment; each result (state, exit code, duration, output digest, output tail) is a `candidate_evaluated` event in the thread's hash chain;
6. `accept c1` when the verdict is acceptable, under the same admission rules as a local accept; the only effect is `refs/heads/syberlabs/<thread>` in the job's checkout;
7. exports the thread to `admission/<pr>-<H, short>.json`, copies the journal directory beside it, writes the step summary, and uploads `admission/` as the artifact `admission-<pr>`.

The job exits 0 when `c1` was accepted and 1 otherwise. It is not a required check; making it one is a repository setting and an owner decision.

## What it proves

- **Host-run.** The checks were processes the host started on a tree the host wrote, not a claim the author made about their machine.
- **The exact tree.** `c1`'s tree id equals `H`'s tree id, checked before any check runs. Both ids are in the export.
- **Journaled.** The base commit, the contract and its check commands, the provider, the candidate's changed paths and diff digest, each check's state and output digest, and the admission decision with its rule are events in one hash chain; `chain_valid` is in the export.
- **Exported.** The record leaves the job as an artifact: the JSON export and a copy of the journal directory, which any `syberlabs` can reopen.

The claim ceiling is `tested`: the named checks passed on this tree, in this job. Nothing more.

## What it does not do

- **No review.** Nobody read the change. A green run says the checks passed, not that the change is right or wanted.
- **No merge, push or comment.** The job has `permissions: contents: read`, uses no secret, and the admit contract has no publishing action. `GITHUB_ACTOR`, recorded as the thread's actor, is whoever triggered the run, not a human key.
- **No certificate.** A certificate is signed with a seed, and the job must not hold one. Certification is a local, keyed act the admitting human performs on the exported journal (the certificate itself is the subject of pull request #36 and is not on `main` yet). That is the design, not a gap.
- **No claim beyond `tested`.** Not measured, not deployed, not validated by anyone outside.
- **No judgement of the checks.** The checks are the ones the contract names, on the head's tree. A pull request that changes the tests changes what runs.

## Running it locally

From a checkout, on ubuntu or Windows Git Bash:

```sh
PYTHONPATH=. bash scripts/admit.sh origin/main my-branch
ADMIT_CHECKS='quick={python} -m unittest discover -s tests -q -p test_package.py' bash scripts/admit.sh origin/main my-branch
```

| Variable | Meaning | Default |
| --- | --- | --- |
| `ADMIT_REPO` | the repository to admit | the current directory |
| `ADMIT_TITLE` | one-line objective, normally the pull request title | `Admit <head>` |
| `ADMIT_AUTHOR` | the author's login, recorded as the provider (lower-cased, with a letter first and only `[a-z0-9_.-]`; the login itself is kept in the candidate's message) | `author` |
| `ADMIT_ACTOR` | who runs the thread | `GITHUB_ACTOR`, then the repository's `user.email` |
| `ADMIT_LABEL` | first part of the export file names | `local` |
| `ADMIT_OUT` | directory that receives the export and the journal copy | `./admission` |
| `ADMIT_CHECKS` | `NAME=COMMAND` lines for `scripts/admit_contract.py`; `{python}` is the interpreter running `syberlabs` | SyberWork's unit tests and conformance |
| `ADMIT_PYTHON` | the interpreter that runs `syberlabs` | `python` |
| `PYTHONPATH` | one directory `syberlabs` imports from | the script's parent directory |

The script never moves your HEAD: the thread runs in a temporary worktree that is removed at the end. It leaves `refs/heads/syberlabs/<thread>` (when accepted) and `refs/syberlabs/candidates/<thread>/c1` in your repository; `git branch -D syberlabs/<thread>` and `git update-ref -d refs/syberlabs/candidates/<thread>/c1` remove them. The script is invoked with `bash`; it does not rely on an executable bit.

## Adopting it in another repository

Copy `.github/workflows/admit.yml`. Replace the first checkout (the tool) with SyberWork at a pinned commit, and point the contract writer at the repository's own checks through the action's `checks` input:

```yaml
      - uses: actions/checkout@<sha>
        with:
          repository: SyberLabs/SyberWork
          ref: <SyberWork commit>
          path: tool
          persist-credentials: false
      - uses: actions/checkout@<sha>
        with:
          ref: ${{ github.event.pull_request.base.sha || inputs.base }}
          fetch-depth: 0
          path: repo
      - uses: ./tool/.github/actions/admit
        with:
          repository: repo
          head: ${{ github.event.pull_request.head.sha || inputs.head }}
          fetch: ${{ github.event_name == 'pull_request' && format('refs/pull/{0}/head', github.event.pull_request.number) || inputs.head }}
          title: ${{ github.event.pull_request.title || inputs.title }}
          author: ${{ github.event.pull_request.user.login || github.actor }}
          label: ${{ github.event.pull_request.number || github.run_id }}
          checks: |
            tests={python} -m pytest -q
            lint={python} -m ruff check .
```

`uses: SyberLabs/SyberWork/.github/actions/admit@<commit>` works too and needs no tool checkout; the action imports `syberlabs` from the SyberWork tree it belongs to, so nothing is installed. Each `checks` line is `NAME=COMMAND`, split like a shell command line; `{python}` becomes the absolute path of the interpreter the action runs, which is what a check needs under the kit's scrubbed environment. Checks that are not Python run as written (`cargo test --quiet`). Pass the inputs through `with:`, never into a `run:` line: a pull request title is untrusted text, and the action reads every input from an environment variable.

## Limits

- **The journal lives in the job.** It is uploaded as an artifact and otherwise ends with the job; it is not persisted anywhere, and the artifact lasts as long as the repository's retention setting. Nothing links one run to the next beyond the pull request number in the file names.
- **One thread per run.** A new push is a new run and a new thread from the new base; the earlier thread is not resumed.
- **Text only.** The kit carries a tree as UTF-8 text, so a head with binary or non-UTF-8 files does not reproduce and the run stops with `tree_mismatch` before any check. A file over 1 MiB is left out of the snapshot, so the run also stops with `tree_mismatch`; a candidate over 4 MiB in total is refused by the kit, and the run stops with `not_reproducible`. Candidates are stored through Git's clean filter, so a repository whose checked-in bytes differ from their cleaned form would also mismatch.
- **The tool is the head.** For this repository the kit, the action and `scripts/admit.sh` come from `H`, so a pull request that breaks them breaks its own admission; that is the point of running the pull request's own version. An adopter pins a SyberWork commit instead.
- **The actor is the trigger.** `GITHUB_ACTOR` is recorded because the kit records who acts; it carries no authority here.
- **Forks.** The head is fetched from `refs/pull/<n>/head` of this repository with the default read-only token; no code from the pull request runs before the thread's checks, and those run on the head tree by design, as this repository's own `ci` workflow already does.
- **Time.** The checks are this repository's full unit suite and conformance; the `ci` workflow's `test` job is the reference for how long they take on ubuntu.
