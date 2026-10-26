# Ed25519 public keys from outside the process

`syberlabs.ed25519.verify` takes any 32 bytes as a public key, as RFC 8032 does. Under the identity point
(`01 00 … 00`) the signature `R = B, s = 1` verifies every message. Under another point `A` of order `n`,
`R = s·B + j·A` verifies when `n` divides `j + k`, `k` the challenge: about one choice of `(s, j)` in `n` (a fixed
`R = B, s = 1` verified 36, 10 and 7 of 64 messages under the order 2, 4 and 8 keys in review). An earlier wording
here, "one of at most eight `j`", was too strong: with `s` fixed, no `j` need work. Whoever chooses such a key signs without a seed. #122 refused such keys in `serve`'s `SYBERWORK_CERTIFICATE_PUBLIC` with a
`usable_key` of its own and left three things open: the check belonged in `syberlabs.ed25519`, `syberlabs verify
--public` took any 32 bytes, and `SYBERWORK_WITNESS_PUBLIC` had no check. This change closes those three.

Claim ceiling: **implemented** and **tested** locally (Linux, Python 3.13.5). Not deployed; CI result is the PR's.

## What changed

- **`syberlabs.ed25519.key_problem(public)`** returns `None` for a key a seed derives (a point of the prime-order
  subgroup) and otherwise one word from `KEY_PROBLEMS`, which stays that word:
  - `not_32_bytes`: not a `bytes` of length 32 (a `str`, a `bytearray` and `None` included);
  - `not_a_point`: no curve point has this encoding, or not the canonical one (`y ≥ p`; `x = 0` with the sign bit);
  - `small_order`: `8·A` is the identity, the identity included;
  - `mixed_order`: `L·A` is not the identity, a prime-order point plus a small-order one.

  `usable_key(public)` is `key_problem(public) is None`. The `L·A` check costs about 60 ms in pure Python, so the
  answer is cached for the 32 most recent keys.
- **`syberlabs verify --public`** (`certificate.read_public`) refuses such a key as `public_key_invalid`, the word
  last in the detail, exit 2, before the certificate is read.
- **A witness key** enters through `TransparencyLog.open_readonly`, which `WitnessClient` and `serve`'s
  `SYBERWORK_WITNESS_PUBLIC` both use. It refuses such a key as `invalid_witness`, the word last.
- **`serve`'s witness parsing had a dead branch.** `Rejected` is a `ValueError`, so `_witness_log` reported every
  `Rejected` from the log, the case-database path included, as *must be the witness public key in hex*. The
  `Rejected` branch now comes first, and `serve` stops with `witness log: invalid_witness: … small_order`.

## Tests

`tests/test_ed25519_keys.py`, 9 tests:

- the small-order points are of the order their names say (1, 2, 4, 8);
- the forgery the check exists for: `R = B, s = 1` verifies two different messages under the identity;
- each weak key (identity, order 2, 4 and 8, mixed order, not a point, `y = p`, `x = 0` with the sign bit) gets its
  word, and wrong types and lengths get `not_32_bytes`; every key `generate` derives is usable;
- `read_public` refuses each weak key with its word. Through the CLI, a real certificate re-signed `R = B, s = 1`
  passes `verify_certificate`'s signature layer under the identity (the library trusts the key it is handed) and
  `syberlabs verify --public identity.pub` refuses it with exit 2 and no output;
- a chain head re-signed the same way verifies under the identity; `open_readonly` and `WitnessClient` refuse each
  weak key; `serve`'s `_witness_log` stops on each with the word.

**Mutations**, each killed by the new tests: dropping the check in `read_public`; dropping it in `open_readonly`;
dropping the `mixed_order` check; checking `4·A` instead of `8·A`; putting `except ValueError` back first in
`_witness_log`.

**Local runs:** `tests.test_ed25519_keys tests.test_certificate tests.test_witness tests.test_external_witness
tests.test_console tests.test_server tests.test_package tests.test_sdk_surface tests.test_dashboard`: 135 OK, 1 skip
(the browser test, no Playwright here). `PYTHONPATH=. python -m unittest discover -s tests`: 937 OK, 26 skips
(no PostgreSQL or Playwright in this environment). Both ran before the review's wording fixes to docstrings and
docs; `tests.test_ed25519_keys` ran again after them.

**Review:** a separate Claude session, not told these conclusions, re-ran the mutations, brute-forced the 38
non-canonical encodings, and checked that no clamped scalar gives a refused key. It found no code bug. Its three
findings are fixed: the forgery wording for small-order keys other than the identity, the unchecked
`SYBERWORK_CERTIFICATE_PUBLIC` on `main` (now stated above), and the `docs/CERTIFICATE.md` row's wording.

## After #126: the library verifiers refuse a weak key themselves

#126 checked a key where it enters from outside (`read_public`, `open_readonly`) and left the library verifiers taking
any 32 bytes. A library caller that forgot `key_problem` got the forgery through. This change, stacked on #126, closes
that for everything above the primitive.

Claim ceiling: **implemented** and **tested** locally (Linux, Python 3.13.5). Not deployed; CI result is the PR's.

- **`dsse.verified_payload`** returns `None` under a key `key_problem` names a problem for, before the envelope is
  read. `verify_chain_head`, `TransparencyLog`'s checkpoint and chain-head reads, and `serve`'s witness reads in
  `syberwork/core.py` all go through it, so they refuse as well (`False` or `None`, as before for a bad signature). A
  caller that needs the reason asks `key_problem`.
- **`certificate.verify_certificate`** fails the signature layer as **`public_key_invalid`**, the code `read_public`
  already uses, with the word last in the note (`… sign under: small_order`), before the envelope is read. A `str`, a
  `bytearray` or a wrong length is `not_32_bytes`, and the report's `keyid` is then `None`. The CLI still refuses
  first in `read_public` (exit 2); this is the layer under it.
- **`ed25519.verify` is unchanged.** It stays RFC 8032's check, the one primitive that still takes a small-order key.
  It has no product caller other than `verified_payload`, and the forgery tests call it to show the signature is
  valid mathematically and the refusal comes from the key. Refusing inside `verify` would break #122's
  `test_a_key_anyone_could_sign_under_is_refused`, which asserts `ed25519.verify(identity, …, R = B, s = 1)`: checked
  by merging this branch into #125's head (7c8f0d4, which carries #122), where that test and the rest of
  `tests.test_console` pass.
- **Cost.** `key_problem`'s cache (32 keys) now serves every `verified_payload`; a key's first check costs one `L·A`
  (about 60 ms in pure Python), about a third of a `verify`. Every later check under a cached key is a lookup. That
  includes a `TransparencyLog`'s own key, derived from its seed, which is checked once though it needs no check.
  `tests.test_witness`'s `test_a_log_checkpoint_is_signed_as_before` counts `_mul` calls in an `append`. It saw 5
  instead of 3 when run alone (the cache cold) and 3 in the full suite (warm), so the result depended on test order. It
  now clears the cache, asserts the key check is 2 multiplications once and none the second time, then asserts 3
  for the append as before.

**Tests** (`tests/test_ed25519_keys.py`, 12 now, 3 new in `LibraryVerifiers`, 2 changed):

- under each small-order key (identity, order 2, 4, 8) a certificate is re-signed without a seed, `R = s·B + j·A`
  searched until `ed25519.verify` takes it; `verified_payload` gives `None` and `verify_certificate` fails as
  `public_key_invalid`, every other layer unrun, the note ending `small_order`;
- under the mixed-order key (`PUBLIC` plus a point of order 4) the holder of `PUBLIC`'s seed writes a signature
  `ed25519.verify` takes; both verifiers refuse it, `mixed_order`;
- each weak key gets its word from `verify_certificate` for an envelope that is not even JSON, and wrong types get
  `not_32_bytes` with `keyid` `None`;
- the two #126 tests that showed `verify_certificate` and `verify_chain_head` taking the forgery now show
  `ed25519.verify` taking it and the verifier refusing it.

**Mutations**, each killed: dropping the check in `verified_payload` (5 failures); dropping it in
`verify_certificate` (26 failures: the report says `signature_invalid` instead, and a `str` key raises); letting `mixed_order` through
`verified_payload` (1 failure, after the mixed-order test was rebuilt with a signature that really verifies; its
first version used one that did not and the mutation survived).

**Local runs:** `PYTHONPATH=. python3 -m unittest discover -s tests` on the final tree: 940 OK, 26 skips
(no PostgreSQL or Playwright here), 664 s. An earlier full run hit a 30-minute `timeout` while a second test run
shared the machine; run alone before the `test_witness` fix it gave 940 OK, 26 skips. `tests.test_witness
tests.test_certificate tests.test_external_witness tests.test_ed25519_keys` together: OK. **Against #122:** this
branch merged without conflict into #125's head (7c8f0d4, which carries #122). There, `tests.test_ed25519_keys
tests.test_console tests.test_certificate tests.test_witness tests.test_external_witness tests.test_server` ran
137 OK, 1 skip (the browser test). That merge was local only and was not pushed. It ran before the `test_witness`
fix, and was not rerun after it; the fixed test passes on this branch.

## Not done (after #126)

- **`ed25519.verify` still takes any 32 bytes that decode**, on purpose (above). A new caller of it that takes a key
  from outside must call `key_problem` first; nothing enforces that.
- `syberwork/certificates.py:usable_key` from #122 is still its own copy; replacing it with
  `from syberlabs.ed25519 import usable_key` is a change to #122's file after both merge, not made here.
- `syberlabs verify` still takes one `--public` key; `bytes.fromhex` in `_witness_log` still accepts spaces.
- `spec/`, `.github/` and the required checks are unchanged.

## Not done (as #126 left it)

- **`ed25519.verify`, `dsse.verified_payload`, `verify_chain_head` and `certificate.verify_certificate` still take
  any 32 bytes.** A library caller that hands them a key from outside must call `key_problem` first. `read_public` and
  `open_readonly` now do.
- **On `main` with this change alone, `serve`'s `SYBERWORK_CERTIFICATE_PUBLIC` is still unchecked.** Set to the
  identity, the console would read a certificate re-signed `R = B, s = 1` as certified. #122 closes that and is open;
  this change does not touch `_certificates`, so the two do not conflict. Refusing inside
  `verify` would also break #122's test that performs the forgery with `ed25519.verify`, so it is a separate change.
- `syberwork/certificates.py:usable_key` from #122 is not replaced here; #122 is open and is not amended. Once both
  are merged it can become `from syberlabs.ed25519 import usable_key`: checked against #122's function (copied from
  its head) on 74 keys, this branch's weak keys, two real keys and the first 64 `y` values, the verdicts agree.
- `syberlabs verify` still takes one `--public` key. `bytes.fromhex` in `_witness_log` still accepts spaces between
  the hex pairs, unlike #122's certificate-key parsing.
- The arithmetic is still not constant-time; a public-key check needs no secret, so that is unchanged here.
- `spec/`, `.github/` and the required checks are unchanged.
