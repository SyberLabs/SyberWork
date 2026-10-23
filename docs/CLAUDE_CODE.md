# Claude Code as a proposer

```bash
syberlabs propose --claude-code --trusted-tree                    # your own Claude Code login (subscription_local)
syberlabs propose --claude-code --bare                            # ANTHROPIC_API_KEY in your environment (api)
syberlabs propose --claude-code --bare --max-calls 1              # one shot instead of the repair loop
syberlabs propose --claude-code --bare --mcp-config githits.json --allowed-tools Read,Edit,Write,Glob,Grep,mcp__githits
syberlabs propose --claude-code --bare --guard-max-lines 3 --guard-max-chars 400   # the query guard's thresholds (defaults)
```

`syberlabs.claude_code.ClaudeCodeProvider` is the repair loop (`providers.RepairProvider`, the factory's default proposer path) with one call replaced. Each call:

1. checks out the parent, the thread's base on the first call and the failed candidate on a repair, in a detached temporary worktree (`gitspace.Repo.worktree`; the developer's working tree is never touched);
2. builds the prompt with `adapters/anthropic_adapter.py: build_prompt` for the same request the JSON adapters get (objective, scope, and on a repair the failed checks' output and the failed candidate's files), and writes it to Claude Code's standard input;
3. runs `claude -p` in that worktree under the operator's own credential;
4. reads the change back from the worktree with git: every path that differs from the parent, committed or not, tracked or untracked and not ignored, with its text. The model's prose is never read as a change;
5. hands that change to the host, which registers, scopes and evaluates it like any candidate. While it fails, the next call repairs it, until one passes, `--max-calls` is reached or the budget stops it.

Nothing here makes a provider's claim into evidence: only the host's own checks on the candidate's tree decide.

## The command it runs

Read from the installed `claude --help` (Claude Code **2.1.309**, the VS Code extension's native binary, 2026-10-23) and Anthropic's CLI reference and headless pages (`code.claude.com/docs/en/cli-reference`, `code.claude.com/docs/en/headless`, read the same day):

```
claude -p [--bare] --output-format stream-json --verbose --permission-mode dontAsk
       --allowedTools Read,Edit,Write,Glob,Grep --setting-sources user --strict-mcp-config
       --no-session-persistence --append-system-prompt "<harness note>" --settings <guard settings file>
       [--max-turns N] [--mcp-config FILE]
```

| Flag | Why |
|---|---|
| `-p`, prompt on stdin | Non-interactive. The prompt goes on standard input (capped at 10 MB by Claude Code), not in argv: no Windows command-line limit and no prompt in a process listing. |
| `--output-format stream-json --verbose` | The only format that carries every turn: each assistant message with its `usage` and `model`, every `tool_use` and `server_tool_use` block, every tool result, the `system/init` event and the final `result` (`num_turns`, `total_cost_usd`, `subtype`). `json` gives the totals but not the tool calls. |
| `--permission-mode dontAsk` | Denies every call that would otherwise prompt; nobody is there to answer. |
| `--allowedTools LIST` | The explicit tool list (`--allowed-tools` on our CLI; default `Read,Edit,Write,Glob,Grep`: no shell, no web). |
| `--setting-sources user` | Loads no project or local settings from the tree, which a previous call's model may have written. |
| `--strict-mcp-config` | Only servers named with `--mcp-config` connect; the tree's `.mcp.json` never does. Always passed, so the off arm of an A/B run has no MCP server at all. |
| `--no-session-persistence` | Per-candidate runs in temporary worktrees are not saved as resumable sessions. |
| `--append-system-prompt` | One fixed harness note: edit files in the current directory, the host reads the change with git, do not commit. The task prompt itself is the adapter's. |
| `--settings FILE` | The query guard (below): a temporary settings document the host writes per call, in its own directory outside the worktree, holding one `PreToolUse` hook and nothing else. `--settings` sits above user, project and local settings and below managed settings (`code.claude.com/docs/en/settings`, read 2026-10-23); `--setting-sources` names only `user`, `project` and `local`, so it does not filter it. |
| `--max-turns N` | Optional cap on agentic turns (listed in the CLI reference; not in 2.1.309's `--help` text). A run that reaches it still yields whatever it changed. |
| `--bare` | The API-key lane: skips hooks, plugins, MCP auto-discovery, auto memory and `CLAUDE.md`, and never reads a subscription login or the keychain. |
| `--mcp-config FILE` | One more MCP server, for example GitHits. |

The executable is `claude` on `PATH`, or `SYBERLABS_CLAUDE_BIN` (a path, or a command line; the tests use it to run a stand-in). It is the operator's own, unmodified binary; SyberLabs never ships, patches or downloads one.

## Credential lanes: bring your own credential

Anthropic's Claude Code legal and compliance page (`code.claude.com/docs/en/legal-and-compliance`, read 2026-10-20; see the officer report `2026-10-20-syberfactory-claude-native-operability.md`) allows an end user to sign in to the unmodified Claude Code with their own subscription, and a customer to give its own users API keys; it does not allow a third party to collect, store or intermediate claude.ai credentials or to route requests through someone's plan on behalf of others. So **SyberLabs never stores, pools or forwards a credential.** The operator's own Claude Code runs under the operator's own login, or with `--bare` and the operator's own API key. The provider holds no credential and writes none to the record; it records which lane ran.

Every candidate this provider registers carries `signal.lane` and `signal.cost_basis`:

| Lane | When | `cost_basis` | Cost in the record |
|---|---|---|---|
| `api` | `--bare` (default there) | `billed` | Tokens × the contract's `budget.prices`, through the host's meter, so the dollar budget applies |
| `provider_3p` | `--bare --lane provider_3p` (Bedrock, Vertex or Foundry credentials) | `estimated` | Priced the same way; the bill is the cloud provider's |
| `subscription_local` | No `--bare` (default there): an inherited login | `none` | Not priced: there is no per-call bill; the dollar budget does not apply |
| `seat_team_enterprise` | `--lane seat_team_enterprise` | `none` | As above |

Claude Code's own `total_cost_usd` is recorded as `signal.estimated_usd`. It is a client-side estimate and is never used as billed spend. On a lane without a per-call bill, `syberlabs metrics` keeps cost `unknown` and says why; turns, lookups and the other metrics are still counted. `--bare` with `subscription_local` or `seat_team_enterprise` is refused (bare mode never reads a login). On those two lanes the provider removes from Claude Code's environment every variable in `claude_code.LOGIN_LANE_DROPPED_ENV`: each one Claude Code's environment reference (`code.claude.com/docs/en/env-vars`, read 2026-10-23) documents as an Anthropic credential or endpoint other than the login (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, workload-identity and mTLS settings) or as a third-party switch, endpoint, project, region or credential (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`, `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`, `CLAUDE_CODE_SKIP_*_AUTH`, `ANTHROPIC_BEDROCK_*`, `ANTHROPIC_VERTEX_*`, `ANTHROPIC_FOUNDRY_*`, `ANTHROPIC_AWS_*`, `CLOUD_ML_REGION`, `VERTEX_REGION_*`), and the cloud SDKs' `AWS_*`, `GOOGLE_*`, `GCLOUD_*` and `AZURE_*`. `CLAUDE_CODE_OAUTH_TOKEN` stays: it is the login. So a run labelled subscription cannot be billed by a key or routed to a billed provider. On `--bare`, a set `ANTHROPIC_BASE_URL` or third-party switch (`claude_code.THIRD_PARTY_SWITCHES`; `0`, `false`, `no` and `off` count as unset) means the run does not go to Anthropic's API under the operator's key: the lane must be `provider_3p`, and `api` is refused when the provider is built (`invalid_provider`) and again at each call (`provider_lane_mismatch`) in case the environment changed. The provider never passes `SYBERLABS_CERTIFICATE_SEED` on any lane. Otherwise Claude Code inherits the caller's environment, as every provider command does.

## Trust

Without `--bare`, `claude -p` runs the hooks in the tree's `.claude/settings.json`, connects the servers in its `.mcp.json` and reads its memory files (`CLAUDE.md`, `CLAUDE.local.md`) as instructions, with no workspace trust dialog and no per-server prompt (headless page, above). The provider therefore **refuses to run non-bare unless the operator passes `--trusted-tree`**, and the refusal (`untrusted_tree`) says why. Trusting the tree means trusting the repository and every tree the model writes in it, because a repair call runs in the failed candidate's tree. Two flags narrow that in every mode: `--setting-sources user` (no project or local settings, so no project hooks) and `--strict-mcp-config` (no `.mcp.json` servers). Memory files are still read from the working directory in non-bare mode, and a repair call runs in the failed candidate's tree, so a model could write the next call's instructions. Before each repair call the provider therefore compares the parent candidate's changed paths against the thread's base, and refuses the call when any is an instruction file: `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` or `.mcp.json` at any depth, or anything under a `.claude/` directory (settings, rules, skills, agents, commands), compared case-insensitively. The refusal is journaled as `proposal_refused` with reason `instruction_files_changed` and the search ends with that error; no call is made on that parent. The base's own files are the operator's and are allowed, so the first call is never refused. The check runs in bare mode too (bare mode skips memory discovery, but skills still resolve), which means an objective that edits these files gets one shot, not a repair loop. `--bare` needs no declaration. None of this is a sandbox: Claude Code runs with the operator's permissions, like every provider command.

**Pilot condition.** This repository's own tracked `CLAUDE.md` describes the GitHits runtime. A pilot run on a SyberWork worktree reads it in both arms of the A/B (non-bare), which is equal across arms but is part of what the model was told; record it with the pilot's conditions.

**Timeouts.** Claude Code runs in its own session (POSIX) or process group (Windows, `CREATE_NEW_PROCESS_GROUP`); when it exceeds its time or output limit the provider ends the whole group or process tree with `checks._stop`, the routine a check's timeout uses (`killpg` on POSIX, `taskkill /T /F` on Windows), so an MCP server or a shell it started does not outlive the call.

## The query guard

GitHits' privacy line is that it sees only the agents' queries, never code, and that every lookup is in the factory's record. The record half was #80 (`lookup` facts with a digest of each query). The guard is the other half: a query that carries a secret, an email, a private URL or a block of the repository's code is stopped on this machine, before Claude Code sends it.

**The hook.** `syberlabs/query_guard.py` is a Claude Code `PreToolUse` hook. For every call the provider creates a temporary directory of its own (outside the worktree and the repository), writes there the guard's policy (`policy.json`), the digests of the worktree's lines (`tree_lines.txt`) and a settings document (`settings.json`) that holds exactly this:

```json
{"hooks": {"PreToolUse": [{"matcher": "^(mcp__githits__.*|WebSearch|WebFetch)$", "hooks": [{
  "type": "command", "command": "<the host's python>", "args": ["-I", "<host checkout>/syberlabs/query_guard.py", "<guard directory>"],
  "timeout": 30, "onFailure": "block"}]}]}}
```

and passes it with `--settings`. The script is the host's own file by absolute path, as a critic program is (`docs/CRITIC.md`), so a candidate cannot rewrite its guard; `-I` keeps the worktree's directory and `PYTHONPATH` off the import path, so the worktree's own `syberlabs` is never imported. The directory is removed after the call.

**The contract it relies on**, read from Anthropic's hooks reference (`code.claude.com/docs/en/hooks`, 2026-10-23) and the installed `claude --help` (2.1.309): a `PreToolUse` hook receives `tool_name`, `tool_input` and `tool_use_id` (with the common `session_id`, `cwd`, `hook_event_name` fields) as JSON on standard input; exit code 2 blocks the tool call and Claude Code hands the hook's standard error to the model as the denial reason; exit 0 lets the call go ahead, and the model never sees a passing hook's standard error; any other exit code is a non-blocking error unless `onFailure: "block"` is set (Claude Code 2.1.295 and later), which also blocks on a timeout or a hook that cannot start. A matcher with characters other than letters, digits, `_`, `-`, `,`, `|` and spaces is an unanchored JavaScript regular expression, hence the anchors; `mcp__githits__.*` matches every GitHits tool. `args` makes the hook exec form (no shell), which matters on Windows, where a shell-form hook runs in Git Bash or PowerShell.

**The rules.** Each outbound text is judged on its own: `url` for `WebFetch` (its `prompt` goes to Anthropic's model, not to the site), `query` and the domain lists for `WebSearch`, every string in the input for a GitHits tool. The first rule broken wins:

| Rule | Blocks |
|---|---|
| `secret` | A credential shape: the pattern set `syberlabs.metrics` refuses in a note (`ghp_`, `gho_`, `ghs_`, `github_pat_`, `sk-ant-`, `sk-`, `xox?-`, `AKIA`, followed by 8 or more key characters; the guard requires a word start, so `risk-assessment` is not an `sk-` key), a PEM private-key header, or a bearer token. |
| `url_credentials` | A URL with a user name or password before the host (`scheme://user:pass@host`). |
| `email` | An email address. |
| `too_long` | More than `max_chars` characters (default 400). |
| `code_fence` | A fenced block (three backticks or three tildes). |
| `code_lines` | More than `max_lines` non-blank lines (default 3). |
| `tree_line` | A line that, whitespace-normalized and at least 16 characters long, occurs verbatim in a tracked file of the worktree the call started from. The host hashes every such line of the parent's tracked files (SHA-256, 32 hex characters kept) before the run; the hook compares digests and never holds the tree's text. |
| `guard_error` | The hook could not read its input or its policy. A guard that cannot judge blocks. |

An allowed query passes unchanged and silently (exit 0, no output). A blocked one gets one line on standard error naming the rule and asking for a short question about a public package; it never repeats the query. Every decision is written beside the hook as `{tool_use_id, tool, query_digest, rule}`: no query text.

**What is recorded.** The provider reads those decisions after the run and matches each to the stream's lookup by tool use id (by tool and query digest, in order, when the hook input carried none). A blocked lookup is still a lookup: its `lookup` record has `ok: false` and `guard: "<rule>"`; an allowed one `guard: "pass"`. The thresholds are recorded on every candidate as `signal.guard: {version, max_lines, max_chars}`. A lookup the stream shows but the hook never decided on is recorded `guard: "unguarded"`, and the call fails `provider_guard_inactive`: the search stops with that error and no further call is made, because the guard did not run (a Claude Code that skipped the hook, a managed policy that allows only managed hooks, `disableAllHooks`, a server-side tool the hook cannot see) and the operator must find out before the next lookup, not after a pilot.

**What the operator tunes.** `--guard-max-lines N` and `--guard-max-chars M` on `syberlabs propose --claude-code`, or `SYBERLABS_GUARD_MAX_LINES` and `SYBERLABS_GUARD_MAX_CHARS` in the host's environment (the flags win), or `guard_max_lines` and `guard_max_chars` on `ClaudeCodeProvider`. They are host policy, never contract data. The rules themselves and the 16-character tree-line floor are fixed in `query_guard.py` (`VERSION` 1).

**Limits, stated plainly.** The guard is a filter over what the model typed, not a proof that no code leaves the machine. A model can paraphrase code, split it into several short queries, put it inside one line of a longer query (the tree rule compares whole lines), or quote a line it wrote during the run (the digests are taken from the parent's tracked files before the run). The identifier rule the packet sketched (a path or identifier from the tree that is in no public dependency name) is not built. A false positive costs a rephrased query: a query that happens to equal a 16-character line of the tree, an `user@host` that is not an address, a long but public question. Whether Claude Code 2.1.309 runs a `--settings` hook under `--bare` is not settled by what was read: the headless page says bare mode skips the *auto-discovery* of hooks and lists `--settings` as the way to pass settings, while the installed `--help` says bare mode skips "hooks (those defined in settings and by installed plugins)". No real Claude Code ran in this packet. The fail-closed rule above is what makes either answer safe: if the hook does not run, the first lookup stops the search and the record says `unguarded`. The first live run of either lane must check that its lookups read `pass` or a rule.

## GitHits A/B

The owner-approved pilot (2026-10-23) compares the same factory, the same agent and the same work items with GitHits off and on. With this provider the only difference between the arms is `--mcp-config FILE`:

```bash
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,mcp__githits   # off
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,mcp__githits \
    --mcp-config githits.json                                                                                       # on
```

Use the same `--allowed-tools` in both arms; the off arm has no GitHits server to call (`--strict-mcp-config` is always passed), so naming its tools there changes nothing. The two argv vectors differ in the last two items only, apart from the path of each call's own temporary guard settings file; the guard and its policy are the same in both arms (`tests/test_claude_code.py` pins this). The `init` event's MCP servers and their status are recorded in `signal.harness.mcp_servers`, so a reader can see that GitHits connected in the on arm.

## What is recorded, and what is not

Per call, on the candidate's `signal` (all parsed by the host from the stream, bounded, and never taken from the model's text):

- `model_usage`: input, output and cache-read and cache-creation tokens summed over the run's assistant messages, counted once per message id (Claude Code repeats a message's usage on every content block it streams), with `model` when one model served the run; `model_usage_by_model` when more than one did;
- `harness_turns`: the result's `num_turns`; `harness_stop`: the result's `subtype`;
- `tool_usage`: `{web_search, web_fetch, githits, other: {tool: n}}`. GitHits is any `mcp__githits__*` tool; web is `WebSearch`, `WebFetch` and the server tools `web_search` and `web_fetch`. It is bounded by `providers.tool_usage`: at most 16 other names;
- `harness`: Claude Code's version, model, effort, the credential source name (`apiKeySource`, never a credential) and the MCP servers with their status;
- `lane`, `cost_basis`, `estimated_usd`, and `skipped_paths` (how many changed files were binary, not UTF-8 or over 1 MiB, and so left out);
- `guard`: the query guard's policy for the call, `{version, max_lines, max_chars}`.

Per lookup (each GitHits or web tool call), one verified `observed` event, key `lookup`, source and actor `syberlabs.host`, version `<search>.<call>.<n>`, value `{tool, query_digest, query_chars, at, ok, guard}`. The provider writes it through the search space (`SearchSpace.observe`; `GitSearchSpace.observe` records it with `Session.observe` and accepts only the key `lookup`, a value of at most 4096 bytes and a short version), so a provider built in Python keeps its lookups in the journal; there is no callback from the CLI any more. `query_digest` is the SHA-256 of the query string: `query` for WebSearch and GitHits search, `url` for WebFetch, otherwise the tool input as canonical JSON. `at` is when the host read the line (the stream carries no time of its own). `ok` is false when the stream holds no successful result for that call, or the guard blocked it. `guard` is `pass`, a rule name or `unguarded` (the query guard, above). A call's lookups are recorded before its candidate.

**Not recorded anywhere in the journal:** the prompt, the query text, tool inputs and results, the model's text and Claude Code's session id. A reader can count and date lookups and tell whether two runs searched for the same thing; they cannot read what was searched. A call that changed nothing is refused `no_change` with its usage, but without its lane, turns or tool counts (`spec/GAPS.md` 51).

## Not built here

Budgets in turns, seconds and tokens for the lanes without a dollar bill, and pausing on Claude Code's `system/api_retry` rate-limit events, are `governor-lanes` (D3). Exposing the kernel as an MCP server (D4) and the Claude Code plugin (D5) come after. `benchmarks/model_vs_patch.py` has no `--adapter claude-code` arm yet. See `spec/GAPS.md` 51, 52 and 53.
