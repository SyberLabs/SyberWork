# G5 Protected-text diff

**Gate:** FACTORY red-team rubric section 3, G5, for SyberWork. It blocks merge when a change alters protected requirement text without a linked approved change decision. Rubric R2: protected text stays byte-identical unless an explicit approved change decision references it. Modeled on attest #33, with the same hook format and the same decision format.

**Check:** `g5-protected-text / G5 Protected-text diff` (`.github/workflows/g5-protected-text.yml`). It runs `tests/test_g5_protected_text.py`, then `scripts/g5_protected_text.py --base <base> --head HEAD`. On a PR the base is the first parent of the merge commit; on a push to `main` it is the previous `main` tip. Python 3.12, standard library only, no installs.

**Status:** Proposed until a hosted run is recorded here. Update this line only from real run output. Not a required check; that is for an admin.

## What is protected

The checker reads protected text from `attest.policy.yaml` files, at both revisions. That is the file Attest's Maintain Docs reads beside a doc (`attest.policy.load_policy`), so the same rule also stops Attest from repairing that paragraph. No second list is kept: a PR can't drop protection by editing a list, because dropping a rule is itself a change to the base's protected text.

| Source | Protected unit |
|---|---|
| `docs/attest.policy.yaml`, `match:` rule | Every paragraph of a `docs/*.md` file that contains the match, case- and whitespace-folded. The rule is protected too: removing it, or the policy file, removes the paragraph's protection and fails the gate. |

The rules added with this gate cover seven paragraphs, one per rule:

| Doc | Paragraph | Area |
|---|---|---|
| `ATTEST_RECONCILIATION_MAP.md` | the boundary paragraph: an Attest receipt is observation data | bridge |
| `ATTEST_RECONCILIATION_MAP.md` | the artifact-to-event mapping table | bridge |
| `ATTEST_RECONCILIATION_MAP.md` | "Destination verification remains separate" | bridge |
| `RECONCILIATION.md` | the status lookup checks and the refusal of caller-provided fields | bridge |
| `RECONCILIATION.md` | a 404, transport failure or mismatch never licenses acceptance | bridge |
| `BUILD_THREAD.md` | the inspector: per-run token, loopback `Host`, GET only, text rendering | console |
| `DASHBOARD.md` | the operator page: self-contained, every value escaped | console |

A base unit passes only if the head has the same policy file, doc path, rule and bytes. Adding new protected text passes. Editing anything else passes. Strengthening a protected paragraph is a change too: the gate compares bytes, not meaning.

The policy is read with the standard library. The reader accepts the subset of YAML the hook uses (a `protected:` list of quoted `match:` or `claim:` items, and comments) and refuses anything else with exit 2. It is never more permissive than PyYAML: every input it accepts parses to the same rules there.

**Exit codes:** 0 pass, 1 protected text changed without a decision, 2 the gate could not run: a base with this gate and no protected text (a vacuous gate), a policy it cannot read, or a revision that can't be resolved.

**Installing the gate.** Attest's hooks were on its `main` before its G5 landed. SyberWork's hook is added by the same change as the gate, so on that change the base protects nothing. When the base has no `scripts/g5_protected_text.py`, the gate treats the change as its own install: it passes only if the head protects at least one unit, and the step summary says BOOTSTRAP and that nothing was compared. Once the script is on `main`, a base with no protected text fails with exit 2, as in attest.

## Changing protected text

1. Land an approved change decision in `docs/change-decisions/<id>.json` on `main` first, through its own reviewed PR.
2. Then open the PR that changes the text.

A decision added in the same PR as the edit doesn't count, so a PR can't approve its own change. The format is attest's, unchanged, so a decision means the same in both repositories:

```json
{
  "schema": "attest.change-decision/v0",
  "decision_id": "CD-0001",
  "status": "approved",
  "approved_by": "protected-requirement owner",
  "path": "docs/RECONCILIATION.md",
  "before_sha256": "<sha256 of the protected paragraph at base>",
  "after_sha256": "<sha256 of the replacement paragraph at head, or null to allow removal>",
  "reason": "Why the requirement changed."
}
```

A paragraph's digest is the SHA-256 of its exact bytes, from its first non-blank line through the newline of its last one. The failing run's step summary prints the first 12 hex digits of each changed paragraph's base digest. A decision whose status isn't `approved`, or that is missing a field, is listed under "ignored" in the step summary and approves nothing.

## Controls

`tests/test_g5_protected_text.py` builds every tree from the frozen copies in `tests/fixtures/g5_protected_text/` and never reads the live `docs/`, so a PR that edits protected text fails in the diff step, not in the controls. The fixture names keep the copies out of the live scan: only a file named `attest.policy.yaml` is a hook. The tests also run in `ci.yml`'s full suite.

- **Positive:** the unchanged tree compares 7 units and passes; a descriptive edit outside every protected paragraph passes; adding a protected paragraph passes.
- **Negative:** one weakening per rule, each failing alone; trailing whitespace; a strengthening; dropping a rule; deleting the policy; deleting a doc; moving a paragraph to another doc.
- **Decisions:** an approved decision on base allows exactly the bound change; one added by the same change doesn't count; a wrong status, approver, schema, before digest, after digest, path or digest case approves nothing; a null after digest allows removal.
- **Fail closed:** a vacuous base, the install rule, eight unreadable policies.
- **CLI:** exit codes 0, 1 and 2 on a real temporary git repository.

## Not covered

- Protected text outside `docs/attest.policy.yaml`'s rules. Which paragraphs are protected is a direction decision; the seven above are a proposal for the owner to confirm.
- `claim:` rules. They name compiler claim ids and can't be resolved without compiling the doc, as in attest.
- Secret or credential leaks in bridge or console output. G5 is a text diff, not a scanner.
- Code. A paragraph is protected, not the code that implements it; G6 lints the bridge mapping.
- Who may approve. The gate checks that a decision is on `main` and binds the digests. Review of the decision PR is the approval control, and only works if `main` is protected.
