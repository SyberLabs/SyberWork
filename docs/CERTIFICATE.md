# Admission certificate (v0)

An admission certificate is one signed statement about one accepted candidate: *the host that ran this Build Thread states, under a signature a verifier can check with only the public key, which exact tree it accepted, under which contract and policy, with which host-run check results, by whose proposal and approvals, at which point of which hash chain.* `syberlabs certify` writes it from the thread's journal; `syberlabs verify` checks it anywhere the public key is, and re-derives whatever a repository and the journal can re-derive. By default the certificate travels as an in-toto Statement inside its DSSE envelope, so any in-toto, Sigstore or SLSA tool can carry and inspect it ([Interoperability](#interoperability)); `--bare` writes the payload under its own type, and `verify` reads both.

What it is not:

- **Not an event.** Nothing is appended to the thread's chain. `certify` reads the journal and signs. The admission rules, the event kinds, `spec/event.schema.json` and the golden traces are untouched.
- **Not a provider claim.** No `signal`, no `output_tail`, no model output enters the payload. The checks it names are the host's own `candidate_evaluated` record (`syberlabs/checks.py`, evaluator `syberlabs.checks/1`), with each check's output digest.
- **Not proof that `main` took the change.** It proves what the journal said at certification about the thread's target branch. Pushing and the pull request are later effects with their own events.
- **Not witnessed.** v0 signs in the certifying process with a seed read from the environment. The SyberFactory plan's thesis T2 asks for a certificate "signed by a key the producing process does not hold"; v0 does not meet that sentence, says so in the payload (`issuer.signing: "in-process"`), and the witness packet below is what closes it.

## Ten lines of shell

```sh
# once, by a person: a credential, kept out of Git and out of any agent's environment
python -c "import os,sys; open(sys.argv[1],'wb').write(os.urandom(32))" seed.bin

syberlabs check c1
syberlabs accept c1
SYBERLABS_CERTIFICATE_SEED=seed.bin syberlabs certify c1        # set the variable for this one command
#   wrote .syberlabs/certificates/<thread>-c1.json (an in-toto Statement in a DSSE envelope) and .syberlabs/certificates/<thread>-c1.pub
#   add --bare for the payload under its own payloadType; verify reads either form

syberlabs verify .syberlabs/certificates/<thread>-c1.json --public .syberlabs/certificates/<thread>-c1.pub          # signature and shape
syberlabs verify .syberlabs/certificates/<thread>-c1.json --public .syberlabs/certificates/<thread>-c1.pub --repo . # plus Git, plus the journal
```

`certify` takes the candidate id, `--out PATH` (default `.syberlabs/certificates/<thread8>-<candidate>.json`, inside the directory `init` already excludes from Git), `--seed-env NAME` (default `SYBERLABS_CERTIFICATE_SEED`) and `--bare` (sign the payload under `application/vnd.syberlabs.admission-certificate+json` instead of wrapping it in an in-toto Statement under `application/vnd.in-toto+json`; the payload, the key, the summary line and `verify`'s lines are the same either way). It goes through the usual `--home`, `--repo`, `--thread` and `--actor` options. It refuses with `not_accepted` when the candidate is not the thread's accepted one (nothing accepted yet, another candidate accepted, or an acceptance still unresolved: run `syberlabs recover`), `unknown_candidate`, `invalid_chain`, `seed_missing`, `seed_invalid` and `seed_env_invalid`. It prints three lines: what was certified, where it was written and under which key, and the `verify` command to run next.

`verify` takes the certificate path and `--public KEY` (the `.pub` file). It is dispatched before any Kit is built, so it works outside any repository and performs none of `Kit.local`'s side effects (no contract or policy file written, no `.git/info/exclude` line). `--repo [PATH]` (PATH defaults to the current directory) enables the repository layer and, when the thread's journal exists under `--home` (default `.syberlabs`, resolved against that repository's root), the journal layer. The global `--repo` means nothing to `verify`; only its own `--repo` does.

## What a verifier learns, layer by layer

**Signature and shape, always.** The file is at most 1 MiB and a JSON object; `payloadType` is `application/vnd.in-toto+json` (the default, an in-toto Statement) or `application/vnd.syberlabs.admission-certificate+json` (`--bare`); a signature in `signatures[]` verifies under the given key over the DSSE pre-authentication encoding of that `payloadType`, before the payload is parsed; for a statement, `_type` is `https://in-toto.io/Statement/v1`, the fields are exactly `_type`, `subject`, `predicateType` and `predicate`, and `predicateType` is `https://sdk.syberlabs.space/certificate/v0alpha1`; the payload (the predicate, for a statement) is an object of exactly the shape in `spec/certificate.schema.json` (every integer an integer, a `passed` check with exit code 0, `result.files` equal to the number of changed paths); for a statement, the one subject is the payload's `admission.target_ref` with `result.commit` as `gitCommit` and `result.tree` as `gitTree`; `protocol`, `version` and `payload_type` inside the payload are this verifier's constants; and the signed bytes are the RFC 8785 form of the object they parse to (the whole statement, for a statement), so two readers cannot see two different certificates in one file. The lines `verify` prints are the same for both forms. A pass here means: whoever held the seed signed exactly this statement.

**Repository, with `--repo`.** Both commits exist; `rev-parse <commit>^{tree}` matches `base.tree` and `result.tree`; `git diff --name-only` between them equals `result.changed_paths`; the diff's SHA-256 and size equal `result.diff_digest` and `result.diff_bytes`. The target ref's current state is reported, never judged: `at <commit>` when it still points at the result, `absent` when it is gone (after a merge the thread branch is usually deleted), or `moved to <commit>` with whether the result is an ancestor. A pass here means: the trees the certificate names are the trees in this repository, and the change between them is the change it describes.

**Journal, with `--repo` and the thread's journal present.** The journal loads cleanly (every link and side digest, checked as the session's loader checks them, by a read-only loader of the verifier's own); it has at least `chain.count` events; the event at that count has hash `chain.head`; the prefix links; and `claims()` recomputed on that prefix, the same function `certify` used, equals every journal-sourced field of the certificate leaf by leaf. Events appended after certification do not matter: the certificate is a statement about a prefix, and the line says `12 of 22 events certified`. A pass here means: this journal still says what the certificate says it said.

The verdict is the last line. `verdict    pass` when every layer that ran passed and none was skipped; `verdict    pass (repository and journal not checked)` or `(journal not checked)` when a layer was skipped, so a signature-only pass is never mistaken for a full one; `verdict    fail: <reason>` at the first failing field, with that layer's line naming the certified and the actual value. Exit status 0, 1 for fail, 2 for a `Rejected` (an unreadable key or certificate file, a `--repo` that is not a repository).

```
signature  ok   ed25519 d75a980182  thread a5a7b724 candidate c1 result cc6e0c8e65 (repo-change.v2, class C)
repository ok   base 541a5193f1 and result cc6e0c8e65 present; trees match; 7 changed paths and diff digest match; target refs/heads/syberlabs/a5a7b724 at cc6e0c8e65
journal    ok   .syberlabs/journal/threads/a5a7b724-....jsonl: 12 of 22 events certified, head 27635b9177; every certified field matches
verdict    pass
```

## The payload

The complete shape is `spec/certificate.schema.json` (`$id` `https://sdk.syberlabs.space/certificate/v0alpha1/certificate.schema.json`), a separate identity beside the frozen `sdk.syberlabs.space/v0alpha1`, in the way the Builder got `sdk.syberlabs.space/builder/v0alpha1`. The schema describes both forms under one top-level `oneOf`: the payload object itself (`--bare`), or the in-toto Statement wrapper whose `predicate` is that object (the default). The payload bytes are `jcs_bytes(payload)` (RFC 8785), the cross-language form the red team chose, not the chain's `canonical`; in the statement form they appear unchanged as the JCS bytes of `predicate`, and the predicate's own `payload_type` field still says `application/vnd.syberlabs.admission-certificate+json`, the predicate's identity rather than the envelope's. Each field has one of three sources, which is also what `verify` checks it against:

| Source | Fields | Re-derived by |
| --- | --- | --- |
| repository | `base.commit`, `base.tree`, `result.commit`, `result.tree`, `result.changed_paths`, `result.diff_digest`, `result.diff_bytes` | `--repo`, from Git |
| journal | `thread`, `objective`, `act_class`, `contract`, `policy_version`, `base.branch`, the rest of `result`, `provider`, `evaluation` (evaluator, event hash, time, every check as `{name, required, state, exit_code, duration_ms, output_digest}`), `admission` (proposal id, actor, roles, origin `human`, approvers, the last decision, claim and effect hashes, `effect_kind`, `target_ref`), `chain` | the journal layer, through `claims()` |
| kit | `protocol`, `payload_type`, `version`, `issuer` (`tool`, `host`, `seed_env`, `signing`), `issued_at`, and `target_ref` for a reconciled acceptance | the signature only |

Three derivations are worth knowing. The evaluation named is the newest host evaluation of the accepted candidate's exact commit and tree *before the acceptance claim*, which is what admission could see when it re-ran at commit. `admission.decision` is the last `decision` event of the promoting proposal: with an `approval_role` it reads `needs_approval` / `approval_required:<role>`, because a successful commit after approval appends no second decision; the approver is in `approvers`. `act_class` is `null` when the contract has no `act_class` input. `admission.roles` are the roles the proposer asserted on the command line; they are recorded, not verified.

## Keys in v0

The seed is 32 bytes. `certify` reads it from the environment variable named by `--seed-env` (default `SYBERLABS_CERTIFICATE_SEED`): the value is either the path of a file holding 32 raw bytes (or 64 hex characters), or 64 hex characters itself. The variable's *name* is recorded as `issuer.seed_env`; its value never reaches the journal, the payload, the certificate file, stdout, stderr or a refusal's text. The public key is written beside the certificate, under the certificate's name with its last suffix replaced by `.pub` (`Path(out).with_suffix(".pub")`: `c1.json` gives `c1.pub`, `c1.cert.json` gives `c1.cert.pub`), 64 lowercase hex characters and a newline, and `verify --public` reads that file. DSSE's `keyid` is the public key hex; the verifier uses it only to narrow which signatures to try, so a genuine signature carrying a foreign `keyid` is not tried and fails as `signature_invalid`, and a signature with no `keyid` is tried.

Generating and keeping the seed is a credential act, a key under the charter, not an agent act; the one-liner above is the whole tool. `syberlabs.ed25519` is pure Python and **not constant-time**, as `docs/SDK_REDTEAM.md` records; it is fine for a certificate signed once on a developer's machine and wrong for a service that signs on request.

**Environment inheritance.** Checks run with a scrubbed environment, but provider commands (`propose --command`, `--evolve`) and publish commands inherit the caller's full environment (`AGENTS.md`, invariant 8). A seed exported in a shell that also runs those commands is visible to those processes. Set the variable for the single `certify` command, prefer the file form over the hex form, and never export it in an agent's shell.

## Interoperability

The default certificate file is a DSSE envelope whose payload is an in-toto Statement v1, the exported statement `docs/SDK_REDTEAM.md` asked for, so the tools that already carry DSSE envelopes (in-toto, Sigstore's `cosign`, SLSA verifiers, Rekor) can carry and inspect it without knowing SyberWork:

```json
{"payloadType": "application/vnd.in-toto+json",
 "payload": "<base64 of the RFC 8785 JSON below>",
 "signatures": [{"keyid": "<public key, 64 hex characters>", "sig": "<base64 Ed25519 signature>"}]}
```
```json
{"_type": "https://in-toto.io/Statement/v1",
 "subject": [{"name": "refs/heads/syberlabs/a5a7b724",
              "digest": {"gitCommit": "<result commit>", "gitTree": "<result tree>"}}],
 "predicateType": "https://sdk.syberlabs.space/certificate/v0alpha1",
 "predicate": {"protocol": "sdk.syberlabs.space/certificate/v0alpha1", "payload_type": "application/vnd.syberlabs.admission-certificate+json", "version": 0, "...": "the v0 payload, unchanged"}}
```

Any DSSE verifier with the `.pub` key can check the signature: Ed25519 over `DSSEv1 28 application/vnd.in-toto+json <payload length> <payload>`, with `keyid` the public key hex. That is the check a `cosign verify-blob-attestation --key <public key> --type https://sdk.syberlabs.space/certificate/v0alpha1 <certificate>` performs (cosign takes the key as PEM, so the 64 hex characters need wrapping as a PKIX Ed25519 key first; running cosign is not part of this repository's tests). The test suite performs the same verification in pure Python, with `syberlabs.dsse.verified_payload(envelope, public, "application/vnd.in-toto+json")` and the pre-authentication encoding spelled out, and reads the statement back without any certificate code. What such a tool learns is the signature layer: whoever held the seed signed this statement about this commit and tree. The repository layer (the trees and the diff from Git) and the journal layer (the certified prefix of the thread's journal) remain SyberWork's own, and only `syberlabs verify` checks that the subject is the predicate's result; a generic verifier reports the subject and trusts it.

The two forms are the same statement under two `payloadType`s, not two certificates. `jcs_bytes(predicate)` is exactly the `--bare` payload, so a reader that has either form has the payload the other signs; but the signatures are not interchangeable, since each covers the pre-authentication encoding of its own `payloadType`, and `verify` refuses an envelope whose `payloadType` was swapped (`signature_invalid`). Rekor publication and a second `signatures[]` entry from the witness are later packets, below.

## Reason codes

| Code | Layer | Meaning |
| --- | --- | --- |
| `certificate_too_large`, `certificate_unreadable` | signature | the file is over 1 MiB, or not a JSON object |
| `payload_type_mismatch` | signature | the envelope's `payloadType` is neither `application/vnd.in-toto+json` nor `application/vnd.syberlabs.admission-certificate+json` |
| `signature_invalid` | signature | no signature verifies under the given public key (forged key, changed bytes, foreign `keyid`, a swapped `payloadType`) |
| `payload_unreadable` | signature | the signed bytes are not a JSON object |
| `statement_type_mismatch` | signature | a statement's `_type` is not `https://in-toto.io/Statement/v1` |
| `predicate_type_mismatch` | signature | a statement's `predicateType` is not `https://sdk.syberlabs.space/certificate/v0alpha1` |
| `payload_shape:<path>` | signature | a field of the payload (the predicate, for a statement), or one of the statement's own four fields, is missing, extra or malformed; `<path>` is the first one |
| `subject_mismatch` | signature | a statement does not have exactly one subject, or that subject is not the predicate's `admission.target_ref` with `result.commit` as `gitCommit` and `result.tree` as `gitTree` |
| `certificate_version_unsupported` | signature | `protocol`, `version` or `payload_type` inside the payload differ from this verifier's |
| `payload_not_canonical` | signature | the signed bytes are not the RFC 8785 form of the object they parse to |
| `commit_missing:<base or result>` | repository | the repository has no such commit |
| `tree_mismatch:<base or result>` | repository | `rev-parse <commit>^{tree}` differs |
| `changed_paths_mismatch`, `diff_mismatch:<digest or bytes>` | repository | the diff between base and result differs from the certified one |
| `journal_corrupt`, `journal_unreadable:<code>` | journal | the journal does not load cleanly |
| `chain_truncated` | journal | fewer events than certified |
| `chain_head_mismatch` | journal | the event at the certified count has a different hash |
| `chain_invalid` | journal | the certified prefix does not link |
| `not_accepted_in_journal`, `journal_inconsistent` | journal | the prefix names no accepted candidate, or lacks a record the claim requires |
| `field_mismatch:<path>` | journal | a certified journal-sourced value differs from the journal; `<path>` is the first leaf |

## Limits

- **In-process signing.** The certifying process holds the seed, so a certificate proves that someone with the seed and read access to the journal produced it, not that an independent party checked anything. This is weaker than T2's sentence and is the first thing the next packet changes.
- **Signature-only verification cannot see a changed tree.** A re-signed certificate naming another tree passes the signature layer, and so does a re-signed statement whose subject and predicate were forged together, because the subject is checked against the predicate, not against Git; only `--repo` catches either. The qualified verdict exists so a reader does not mistake one for the other.
- **The certificate proves the journal's statement, not the merge.** `main` taking the change is a later effect; the target ref's state is reported for the reader, not judged.
- **One key, no rotation, no revocation.** A compromised seed cannot be revoked; a verifier trusts whichever public key it was handed.
- **`keyid` narrowing.** A genuine signature under a foreign `keyid` is rejected; this follows `syberlabs/dsse.py` and DSSE's rule that `keyid` only narrows the keys to try.
- **The journal layer never writes to or locks the journal.** `verify` opens `registry.jsonl` and the thread file read-only, takes no lock, and leaves a torn final line left by a writer that died mid-record in place: the line is ignored and the journal line says `1 torn trailing record ignored`, where the session's own reader would truncate it. A verifier can therefore work from a read-only copy of a journal, and reads whatever complete records are on disk at that moment. It never runs a check, a provider or a model, and never updates a ref.

## What comes next

Each is a later packet; none changes the v0 payload.

1. **Witness co-signature.** `syberlabs/witness.py` learns a second request, recomputes `claims()` from the events it is sent, compares like the journal layer, and appends its own `{keyid, sig}` as a second entry in the statement envelope's `signatures[]`; `issuer.signing` becomes `witnessed` and `verify` gains `--witness-public`. The producing process then needs no seed.
2. **Transparency-log inclusion.** `TransparencyLog.append` accepts the certificate type beside the chain-head type, the witness appends after co-signing, and `verify` gains an inclusion check.
3. **Rekor publication.** The statement form is already what Rekor's DSSE and in-toto entry kinds store. Publishing needs a Rekor identity, a key under the charter, and runs `cosign` beside the kit; the kit gains a documented command, not a dependency.
4. **Attaching the certificate to the pull request.** The `github_pull_request` action gains an optional `certificate` field and the pull request body carries the certificate's digest and path after the host-check line.
