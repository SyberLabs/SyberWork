# GitHits API client

```bash
export GITHITS_API_TOKEN=...            # from the environment only; never in a file in this repository
syberlabs githits search "retry with backoff" --target pypi:urllib3@2.2.1 --source code
syberlabs githits example "read a JSON file in Rust" --language rust
syberlabs githits read pypi:requests@2.32.3 --path src/requests/api.py --start-line 40 --end-line 90
syberlabs githits list github:psf/requests --path "src/**" --recursive
syberlabs githits grep "Retry(" --target pypi:urllib3@2.2.1 --literal --max-matches 10
syberlabs githits search ... --json      # the filtered response and every count, as JSON
syberlabs githits doctor                 # one cheap authenticated request: ok, auth, rate_limited, no_token or error
```

`syberlabs/githits.py` is a standard-library client for five read-only operations of the GitHits public REST
API: `search` (`POST /v1/searches`), `example` (`POST /v1/examples/search`), `read` (`GET /v1/read`), `list`
(`POST /v1/list`) and `grep` (`POST /v1/grep`). They are the same five tools the MCP route allows
(`.claude/settings.json`), called directly. This gives the host three things the MCP route does not: it controls
the request, it keeps the response lean, and it keeps its own record of each call. It also gives the host a place
to filter results before an agent sees them.

`githits-client` built the client and its CLI; `githits-wiring` made it the GitHits route of the Claude Code
provider: `syberlabs propose --claude-code --githits` runs it through a per-call launcher as the agent's only
command, behind the query guard, and journals each of its records as a `lookup` (`docs/CLAUDE_CODE.md`, GitHits
through the launcher). No call to the real API was made while building either. Every test runs against a loopback
fake server, and no token was available.

## Exit codes

`0` a result (possibly with some results dropped and counted); `2` refused: a target names an excluded owner
(`excluded_target`) or the response cites one (`contamination_blocked`), with the reason on standard error;
`1` an error (no token, a bad setting, a non-2xx answer, a redirect, an oversize or malformed body, a transport
failure, a record that could not be written).

## Lean defaults

The defaults are chosen for an agent's context window. GitHits' own default is in parentheses.

| Operation | Default | Why |
|---|---|---|
| search | `limit` 5 (10); `fields=results` sent explicitly (`results`) | highlights, sources and progress are opt-in with `--fields` |
| read | lines 1 to 200 when no bound is given (whole file or page); from `--start-line`, 200 lines | `--all-lines` turns this off; a target with a `#fragment` gets no default bounds, because a line bound overrides the fragment |
| list | `limit` 50 (100) | `--after` pages on |
| grep | `max_matches` 20 (100); context lines 0 (0) | `-B`/`-A` add context |
| example | `license_mode` `strict` (`strict`) | sent explicitly; see the license policy |

Timeouts: 60 s, or the requested `wait_timeout_ms` plus 30 s when that is longer (search and read wait 20 s by
default on the server), and 120 s for an example. `--timeout` overrides. A response body over 4 MiB is refused,
not truncated. The compact text output is one header line, then one line per hit (two for a search hit:
location and title, then a 200-character summary). Request ids are not printed; they are in `--json` and in the
records.

## Contamination control

GitHits indexes public code, and SyberLabs' own repositories are public. An agent working on SyberLabs code must
not be shown SyberLabs code as if it were outside evidence. For the pilot, contamination is resolved on SyberLabs'
side: the work items are unpublished, both arms run locally on a pinned base, and this client is the backstop. An
exclusion at GitHits' index is optional, not something the pilot relies on.

The client works in three layers. The exclusion list is `SyberLabs` plus whatever `SYBERLABS_GITHITS_EXCLUDE` adds:
a comma list of owners or `owner/repo`, matched case-insensitively. The variable only adds; it never removes
`SyberLabs`, and an empty value keeps the default. `SYBERLABS_GITHITS_EXCLUDE_REPLACE=1` makes the variable's list
replace the default; it exists for tests only, and the Claude Code provider never passes it to the launcher's client.
Passing another list to `Client(exclude=...)` in code replaces the default too.

| Layer | What it catches | What it does | Limit |
|---|---|---|---|
| (a) target | a search, read, list or grep target that names an excluded owner: `github:SyberLabs/x`, `https://github.com/SyberLabs/x`, `git@github.com:SyberLabs/x.git`, a bare `SyberLabs/x`, `npm:@syberlabs/x`, `pypi:syberlabs`, `syberlabs.github.io`, GitLab, Codeberg, raw and API hosts, URL-encoded forms | refuses with `excluded_target`; **no request is sent**; the refusal is recorded | a package whose name does not name the owner (for example a mirror under another name) passes; layer (b) sees its served repository afterwards |
| (b) result identity | a search hit whose `locator.repo_url`, `source_url` or `docs_read_target` is under an excluded owner; a grep hit's `repo_url`, `page_url` or read target; a list whose served repository or `canonical_target` is excluded (every entry dropped) or an entry whose read or browse target is; a read whose served repository or documentation source is excluded | drops the result and counts it (`results_dropped`); the call is still `ok` | the request was sent and GitHits returned the content; this only keeps it from the agent |
| (c) free text | an example's Markdown and any structured field, or a read body, that mentions an excluded owner on a code host (`github.com/<owner>/`, `<owner>.github.io` and the other hosts, as typed or URL-decoded) | drops the **whole** response: `contamination_blocked`, exit 2 | **detection, not prevention**. A generated example can blend code from an excluded repository without citing it, and nothing on this side can see that. A mention spelled in a form the patterns do not list (split across lines, zero-width characters, a link shortener) passes. This is why the pilot resolves contamination on its own side (above); index-side exclusion at GitHits is optional. |

## License policy

**Permissive only by default.** Code an agent takes from a result can land in this repository, which is licensed
Apache-2.0. A copyleft snippet would carry obligations this repository cannot meet: licensing the whole work
under the same terms, or keeping file-level source terms. The allow list (`PERMISSIVE`) is `MIT`, `Apache-2.0`,
`BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `0BSD`, `Unlicense`, `Zlib`, `PSF-2.0` and `CC0-1.0`. Each of these asks
at most that a notice be kept, which an Apache-2.0 project can do. It is an allow list, so everything else is
dropped: GPL, LGPL, AGPL, MPL, EPL, EUPL, CDDL, SSPL, `NOASSERTION`, a license the list does not name, and a
missing license.

An SPDX expression is parsed as SPDX defines it (`githits.spdx_parse`): `WITH` binds tighter than `AND`, `AND`
tighter than `OR`, parentheses group, and operators and ids are matched ignoring case. It passes when it can be
satisfied with licenses on the list: an `AND` needs both sides, an `OR` either; the exception after `WITH` is
checked for form and not judged. So `MIT OR GPL-2.0-only` and `(MIT AND GPL-3.0) OR Apache-2.0` pass, and
`MIT AND GPL-2.0-only` and `GPL-3.0-only AND (MIT OR Apache-2.0)` do not. An expression that does not parse (an
unbalanced parenthesis, a dangling operator, a stray character, more than 64 tokens) is not admitted.

- **Server side, preferred where it exists.** Of the five operations, only `example` has a license parameter
  (`license_mode`: `strict`, `yolo`, `custom`). The client always sends `strict` unless the operator's own
  `syberlabs githits example --license-mode` says otherwise; the launcher a model runs (`--githits`, below) has
  no such option and always sends `strict`. The OpenAPI document does not say which licenses `strict` admits. `custom` applies an account-level policy that this API cannot set, so the
  client cannot ask the server for this exact list.
- **Client side, wherever a result carries a license.** A result that is not admitted is dropped and counted in
  `results_dropped_license`, apart from the contamination count. Contamination is judged first. In contract
  0.1.0 only an example carries licenses: the `(SPDX)` after each source link of its Markdown
  (`[Source 1](https://github.com/o/r/blob/sha/path) (MIT)`), a `License:` line, or a structured `references`
  list if one is ever returned. An example whose sources include a license that is not admitted, a source link
  with no license, or no source link at all, is dropped whole. Exit 0, with a one-line notice.
- **It depends on license data being present.** No search, grep, list or read result in 0.1.0 has a license
  field, so those results pass this check unjudged. The client would read `license_spdx_id`, `spdx_id`,
  `license_id` or `license` on a result (or a search hit's locator) if GitHits added one.
  `SpecPin.test_only_the_example_request_carries_a_license_and_no_result_schema_has_one` fails when the pinned
  contract gains such a field, so that the field gets read by name.

`SYBERLABS_GITHITS_LICENSES` replaces the list (a comma list of SPDX ids, case-insensitive) or turns the
client-side check off (`any`; the request still asks for `strict`). An empty value keeps the default, and a
malformed value is an error. Every record carries `license_policy`, the SHA-256 of the canonical policy
(`{"allow": [sorted ids]}` or `{"allow": "any"}`).

## Records

When `SYBERLABS_GITHITS_RECORD_DIR` and `SYBERLABS_GITHITS_RECORD_KEY` are both set, every call appends one line
to `githits-records.jsonl` in that directory. Refused calls and failed calls are recorded too. Setting only one of
the two is an error, so a run never believes it is recording when it is not.

```json
{"op": "search", "query_digest": "<sha256>", "targets_count": 1, "status": "ok", "http_status": 200,
 "request_id": "<X-Request-ID>", "results_returned": 3, "results_dropped": 1, "results_dropped_license": 0,
 "license_policy": "<sha256>", "contamination_blocked": false, "latency_ms": 412, "rate_limit_remaining": 97,
 "at": "2026-10-26T17:01:16.123Z", "mac": "<hmac-sha256>"}
```

- `status` is `ok`, `excluded_target`, `contamination_blocked` or `error`.
- `query_digest` is the SHA-256 of the canonical JSON request: the body of a POST, the query parameters of a
  read.
- `rate_limit_remaining` is `X-RateLimit-Remaining` when the answer has it. The spec documents it for every
  operation here except `example`.
- `mac` is HMAC-SHA256 under the record key over the canonical JSON of every other field. It is
  `query_guard.sign`, the same canonical form and MAC the query guard's decisions use.
- `read_records(directory, key)` returns the records whose MAC verifies, and counts every other line (bad MAC,
  extra or missing field, not JSON) as ignored.

No query text, no result and no token is ever stored. As with the guard's key, the MAC shows that a process
holding the key wrote the line. The key is in the environment of whatever runs the client, so it does not show
which process that was.

## Token and transport

- The token is read only from `GITHITS_API_TOKEN`, and is sent only in `Authorization: Bearer`. It is not in any
  URL, record, `repr`, exception or CLI output. If the server echoes it in a problem document, it is replaced by
  `[token]`. Tokens holding whitespace or control characters are refused.
- `GITHITS_API_URL` sets the base (default `https://api.githits.dev/v1`). It must be `https://`. `http://` is
  accepted only for a loopback host (the tests). A base with credentials, a query or a fragment is refused.
  Proxies from the environment apply, except to loopback.
- Redirects are refused (`redirect_refused`) rather than followed, so the `Authorization` header never reaches
  another URL.
- Every request carries `X-GitHits-Client-Name: syberlabs` and `X-GitHits-Client-Version` (the installed
  `syberwork` version, or `source`). `X-GitHits-Session-ID` (`GITHITS_SESSION_ID`, `[A-Za-z0-9_-]{1,64}`) and
  `X-GitHits-Agent` (`GITHITS_AGENT`, printable ASCII up to 160 bytes) are sent only when set. A pilot uses
  them to separate its arms. Invalid values are refused before any request.
- A non-2xx answer raises `GitHitsError` with code `http_<status>`. Its message holds the problem's stable `code`
  and `title`, never `detail`. Errors are recorded with their HTTP status and request id.
- The token is removed from server text **before** the text is cut to length (120 characters per problem field,
  200 for a transport reason), so a token across the cut cannot leave its first characters behind; `X-Request-ID`
  is scrubbed the same way before it is cut to 128 characters and reaches an exception, a record or `--json`.

## Readiness: `syberlabs githits doctor`

One cheap authenticated request, the lightest operation in the document: `GET /v1/languages?query=python&limit=1`
(no body, no target, no repository content; at most one language name back). It prints one line, for example
`githits doctor: ok (HTTP 200, request <id>, 212 ms, base https://api.githits.dev/v1, token set)`, or a JSON object
with `--json`, and exits 0 only when the status is `ok`:

| Status | When |
|---|---|
| `ok` | 200 and a JSON array |
| `auth` | 401 or 403 (a missing, rejected or inactive credential, or terms not accepted); the problem's code is shown, for example `AUTHENTICATION_REQUIRED` or `TERMS_ACCEPTANCE_REQUIRED` |
| `rate_limited` | 429, with `Retry-After` when sent |
| `no_token` | `GITHITS_API_TOKEN` is not set; nothing is sent |
| `error` | anything else: another status, a malformed body, a transport failure |

The token is reported as `set` or `unset`, never shown. The operation sends no rate-limit headers in the spec, so
`rate limit remaining` appears only if GitHits adds them. With records configured, the check is recorded like any
call (`op: doctor`), and such a record is never counted as a lookup (`githits_lookups` leaves `githits.doctor`
facts out). Its operation is pinned apart from the five, in
`tests/fixtures/githits_openapi_v1_languages.json` (15,285 bytes, the same closed-subset form from the same
document), so the larger fixture stays byte-identical. This is what a first live canary runs before any lookup.
`doctor` is the operator's command only: the launcher a model can run does not accept it (next section).

## The launcher's entry (`--githits`)

`syberlabs/githits_cli_entry.py` is what the per-call launcher of `syberlabs propose --claude-code --githits`
runs (`docs/CLAUDE_CODE.md`, GitHits through the launcher), the one command a model can run. It accepts only the
five lookups, `search`, `example`, `read`, `list` and `grep` (`githits.LOOKUPS`, parsed by
`githits.launcher_parser`), with the same options as `syberlabs githits`, except that `example` has no
`--license-mode`: through the launcher an example always asks for `license_mode: strict`. `doctor`, `--help`, an
unknown operation and a usage error exit 2 with a usage message before a client exists, so nothing is sent and
nothing is recorded. The operator keeps `syberlabs githits doctor` and `--license-mode`.

## The pinned contract

`tests/fixtures/githits_openapi_v1.json` pins the part of the public OpenAPI document that this client uses. The
document was fetched once, without a token, from `https://api.githits.dev/v1/openapi.json` on 2026-10-26:
"GitHits Public API" 0.1.0, 1,364,570 bytes, SHA-256
`e58048e3e56d7b0d6346e24722614d2d8907748960f18f18204895a354ab8a6a`. The whole document is over this repository's
change-size limit, so the fixture holds only the five operations, with every component they reference
(transitively), values unchanged. It is written as compact, sorted ASCII JSON (`tests/test_githits.py:
fixture_bytes`): 215,618 bytes, SHA-256 `c388ab7e53a25fdfbd914c10634d799ae923b01ab8880750c750e775c21f1b9c`.

`SpecPin` checks five things:
- the fixture's digest;
- that the fixture is exactly that subset of itself;
- each operation's method, path and schema names;
- that every request the client builds, all options set, is valid under the request schema (the request
  schemas reject unknown keys);
- that every response field the client reads (`githits.READS`) and every header it sends or reads exists.

It also checks that the fake server's responses are valid under the response schemas. A contract change
therefore fails a test.

To compare a newer upstream document, run this from the repository root:

```bash
PYTHONPATH=. python -c "import hashlib, json, sys; from tests.test_githits import fixture_bytes; \
print(hashlib.sha256(fixture_bytes(json.load(open(sys.argv[1], encoding='utf-8')))).hexdigest())" openapi.json
```

The same digest means the five operations are unchanged.

## Environment

| Variable | Meaning |
|---|---|
| `GITHITS_API_TOKEN` | the bearer token (required for a call; a refused target needs none) |
| `GITHITS_API_URL` | base URL, default `https://api.githits.dev/v1` |
| `GITHITS_SESSION_ID`, `GITHITS_AGENT` | optional `X-GitHits-Session-ID` and `X-GitHits-Agent` |
| `SYBERLABS_GITHITS_EXCLUDE` | owners or `owner/repo` to exclude besides `SyberLabs`, comma list (it only adds) |
| `SYBERLABS_GITHITS_EXCLUDE_REPLACE` | `1`: the list above replaces `SyberLabs` (tests only) |
| `SYBERLABS_GITHITS_LICENSES` | allowed SPDX ids, comma list, or `any` (default: the permissive list) |
| `SYBERLABS_GITHITS_RECORD_DIR`, `SYBERLABS_GITHITS_RECORD_KEY` | where records go and their MAC key (both or neither) |

Claim ceiling: **implemented and tested** against a loopback fake that follows the pinned schemas. It has not
been run against the real API, so the real server's behaviour is not observed: whether `strict` already excludes
copyleft, how example references look in practice, and the rate-limit headers. Nothing is measured or deployed.
