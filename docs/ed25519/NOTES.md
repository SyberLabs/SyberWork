# Ed25519 public keys from outside the process

`syberlabs.ed25519.verify` takes any 32 bytes as a public key, as RFC 8032 does. Under the identity point
(`01 00 … 00`) the signature `R = B, s = 1` verifies every message. Under another point of small order, `R = B + j·A`
verifies for one of at most eight `j` (a fixed `R = B, s = 1` verified 36, 10 and 7 of 64 messages under the order 2,
4 and 8 keys in review). Whoever chooses such a key signs without a seed. #122 refused such keys in `serve`'s `SYBERWORK_CERTIFICATE_PUBLIC` with a
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

## Not done

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
