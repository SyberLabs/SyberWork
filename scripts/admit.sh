#!/bin/bash
# Admit one tree through a Build Thread: the contract's checks are host-run on the exact
# head tree, journaled, and exported. Nothing is pushed, merged or commented.
#
#   bash scripts/admit.sh <base> <head>
#
# <base> is the commit the thread starts from (a pull request's base), <head> the tree to
# admit (its head). Both are resolved in ADMIT_REPO, then as origin/<name>. The thread runs
# in a temporary worktree of ADMIT_REPO, so the caller's HEAD and working tree are not moved.
# Runs on ubuntu and on Windows Git Bash; needs git and the interpreter that runs syberlabs.
#
# Environment, all optional:
#   ADMIT_REPO    the repository to admit (default: the current directory)
#   ADMIT_TITLE   one-line objective, normally the pull request title (default: "Admit <head>")
#   ADMIT_AUTHOR  the pull request author's login, recorded as the provider that proposed the tree
#   ADMIT_ACTOR   who runs the thread (default: GITHUB_ACTOR, then the repository's git user.email)
#   ADMIT_LABEL   first part of the export file names, normally the pull request number (default: local)
#   ADMIT_OUT     directory that receives the export and the journal copy (default: ./admission)
#   ADMIT_CHECKS  NAME=COMMAND lines, one per line, for scripts/admit_contract.py
#                 (default: SyberWork's unit tests and conformance; {python} is the interpreter)
#   ADMIT_PYTHON  the interpreter that runs syberlabs (default: python)
#   PYTHONPATH    one directory syberlabs imports from (default: this script's parent directory)
#
# Exit 0 when the candidate was accepted; 1 when it was not (a check failed, the head tree
# could not be reproduced, or there was nothing to admit); 2 on a usage error.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: bash scripts/admit.sh <base> <head>" >&2
  exit 2
fi

tool=$(cd "$(dirname "$0")/.." && pwd)
py=${ADMIT_PYTHON:-python}
repo=$(cd "${ADMIT_REPO:-.}" && pwd)
out=${ADMIT_OUT:-admission}
mkdir -p "$out"
out=$(cd "$out" && pwd)
label=${ADMIT_LABEL:-local}
author=${ADMIT_AUTHOR:-}
actor=${ADMIT_ACTOR:-${GITHUB_ACTOR:-}}

# syberlabs must import from an absolute path in the interpreter's own notation (the thread
# runs in a temporary worktree, and Git Bash paths are not Windows paths).
PYTHONPATH=$("$py" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "${PYTHONPATH:-$tool}")
export PYTHONPATH

resolve() {
  git -C "$repo" rev-parse --verify --quiet "$1^{commit}" 2>/dev/null && return 0
  git -C "$repo" rev-parse --verify --quiet "origin/$1^{commit}" 2>/dev/null && return 0
  echo "admit: cannot resolve $1 in $repo" >&2
  return 2
}
base_sha=$(resolve "$1") || exit 2
head_sha=$(resolve "$2") || exit 2
head10=${head_sha:0:10}

title=$(printf '%s' "${ADMIT_TITLE:-}" | tr '\r\n\t' '   ' | cut -c1-300)
[ -n "${title// /}" ] || title="Admit $head10"
# A provider name is lowercase, starts with a letter and has no brackets; the login itself
# is kept verbatim in the candidate's message.
provider=$(printf '%s' "${author:-author}" | tr 'A-Z' 'a-z' | sed -e 's/[^a-z0-9_.-]/-/g' -e 's/[-._]*$//' -e 's/^[^a-z]/gh-&/' | cut -c1-64)
[ -n "$provider" ] || provider=author

status=error
thread=""
candidate=""
export_path=""
journal_path=""
parent=""
work=""
notes=$(mktemp)

note() { printf '%s\n' "$*" | tee -a "$notes"; }

finish() {
  code=$?
  cd "$repo"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "status=$status"
      echo "thread=$thread"
      echo "candidate=$candidate"
      echo "export=$export_path"
      echo "base=$base_sha"
      echo "head=$head_sha"
    } >> "$GITHUB_OUTPUT"
  fi
  summary=$(mktemp)
  {
    echo "## Admission of ${head10} on base ${base_sha:0:10}"
    echo
    echo "- status: **${status}**"
    echo "- base: \`${base_sha}\`"
    echo "- head: \`${head_sha}\`"
    echo "- provider: \`${provider}\` (login: ${author:-not given})"
    [ -n "$thread" ] && echo "- thread: \`${thread}\`"
    [ -n "$candidate" ] && echo "- candidate: \`${candidate}\`"
    cat "$notes"
    echo
    echo "Host-run checks on the exact head tree, journaled and exported. Not a review, not a merge, not a certificate; the claim ceiling is tested."
  } > "$summary"
  echo
  cat "$summary"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat "$summary" >> "$GITHUB_STEP_SUMMARY"
  fi
  rm -f "$summary" "$notes"
  if [ -n "$work" ]; then
    git -C "$repo" worktree remove --force "$work" >/dev/null 2>&1 || true
    git -C "$repo" worktree prune >/dev/null 2>&1 || true
  fi
  [ -n "$parent" ] && rm -rf "$parent"
  exit "$code"
}
trap finish EXIT

if [ "$base_sha" = "$head_sha" ]; then
  status=no_change
  note "- the head is the base: there is nothing to admit"
  exit 1
fi

parent=$(mktemp -d "${TMPDIR:-/tmp}/admit.XXXXXX")
work=$parent/tree
git -C "$repo" worktree add --detach --quiet "$work" "$base_sha"
cd "$work"

check_args=()
if [ -n "${ADMIT_CHECKS:-}" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    [ -n "$line" ] && check_args+=(--check "$line")
  done <<< "$ADMIT_CHECKS"
fi
actor_args=()
[ -n "$actor" ] && actor_args+=(--actor "$actor")
sl() { "$py" -m syberlabs --home .syberlabs --repo . ${actor_args[@]+"${actor_args[@]}"} "$@"; }

"$py" -m syberlabs --home .syberlabs --repo . init >/dev/null
contract=$("$py" "$tool/scripts/admit_contract.py" --home .syberlabs ${check_args[@]+"${check_args[@]}"})
sl start "$title" --contract "$contract" --class R --paths .
thread=$(tr -d '\r\n' < .syberlabs/current)
thread8=${thread:0:8}

# The head's tree, in the worktree, without moving HEAD: files the head adds or changes are
# checked out from it; files it deletes are removed. Renames are a delete and an add.
git checkout --quiet "$head_sha" -- .
git diff --name-only -z --no-renames --diff-filter=D "$base_sha" "$head_sha" > "$parent/deleted"
if [ -s "$parent/deleted" ]; then
  xargs -0 git rm --quiet --force -- < "$parent/deleted"
fi
if ! git diff --quiet --no-ext-diff "$head_sha" --; then
  status=tree_mismatch
  note "- the worktree does not match the head's tree after checkout; nothing was proposed"
  exit 1
fi

if ! sl propose --from-worktree --name "$provider" --revision "$head10" \
     --message "pull request $label by ${author:-an unnamed author}: head $head_sha" > "$parent/propose.log" 2>&1; then
  cat "$parent/propose.log"
  status=not_reproducible
  note "- the kit could not propose the head's tree as a candidate (see the propose output above)"
  exit 1
fi
cat "$parent/propose.log"
candidate=$(sed -n 's/^\(c[0-9][0-9]*\):.*/\1/p' "$parent/propose.log" | head -n 1)
if grep -q '^left out' "$parent/propose.log"; then
  note "- $(grep '^left out' "$parent/propose.log" | head -n 1)"
fi
candidate_tree=$(git rev-parse --verify "refs/syberlabs/candidates/$thread8/$candidate^{tree}")
head_tree=$(git rev-parse --verify "$head_sha^{tree}")
if [ "$candidate_tree" != "$head_tree" ]; then
  status=tree_mismatch
  note "- candidate $candidate has tree ${candidate_tree:0:10}, the head has ${head_tree:0:10}: the kit cannot carry this tree (binary or non-UTF-8 files?); its checks were not run"
  exit 1
fi
note "- candidate $candidate is the head's exact tree (\`$head_tree\`)"

set +e
sl check "$candidate" --json > "$parent/check.json"
check_code=$?
# Render the verdict for the log and the summary: exit 0 acceptable, 1 not, 3 no verdict.
"$py" - "$parent/check.json" "$notes" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        verdict = json.load(handle)
except ValueError:
    sys.exit(3)
lines = []
for check in verdict["checks"]:
    extra = f", exit {check['exit_code']}" if check["exit_code"] not in (None, 0) else ""
    lines.append(f"- check `{check['name']}`: **{check['state']}** ({check['duration_ms']} ms{extra})")
    if check["state"] != "passed" and check.get("output_tail"):
        tail = check["output_tail"][-3000:].replace("```", "'''")
        lines.append(f"\n<details><summary>{check['name']} output tail</summary>\n\n```\n{tail}\n```\n\n</details>\n")
word = "acceptable" if verdict["acceptable"] else "not acceptable"
lines.append(f"- verdict: **{word}** ({verdict['status']}: {verdict['reason']}; rule {verdict['rule']})")
text = "\n".join(lines) + "\n"
sys.stdout.write(text)
with open(sys.argv[2], "a", encoding="utf-8") as handle:
    handle.write(text)
sys.exit(0 if verdict["acceptable"] else 1)
PY
rendered=$?
set -e
if [ "$rendered" -eq 0 ]; then
  if sl accept "$candidate"; then
    status=accepted
    note "- accepted: \`refs/heads/syberlabs/$thread8\` in the job's checkout points at the candidate; nothing was pushed or merged"
  else
    status=not_applied
    note "- the candidate was acceptable but accept did not succeed (see above)"
  fi
elif [ "$rendered" -eq 1 ]; then
  status=not_acceptable
else
  status=error
  note "- check $candidate did not return a verdict (exit $check_code)"
  exit 1
fi

export_path="$out/$label-$head10.json"
journal_path="$out/$label-$head10-journal"
sl export --out "$export_path"
rm -rf "$journal_path"
cp -R .syberlabs/journal "$journal_path"
rm -f "$journal_path/.lock"
thread_status=$(sl status --json | "$py" -c 'import json, sys; print(json.load(sys.stdin)["status"])')
note "- thread status: $thread_status; export: \`admission/$label-$head10.json\`; journal copy: \`admission/$label-$head10-journal/\`"

[ "$status" = accepted ]
