# Claude Code as a proposer

```bash
syberlabs propose --claude-code --trusted-tree                    # your own Claude Code login (subscription_local)
syberlabs propose --claude-code --bare                            # ANTHROPIC_API_KEY in your environment (api)
syberlabs propose --claude-code --bare --max-calls 1              # one shot instead of the repair loop
syberlabs propose --claude-code --bare --mcp-config githits.json --allowed-tools Read,Edit,Write,Glob,Grep,mcp__githits
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
       --no-session-persistence --append-system-prompt "<harness note>" [--max-turns N] [--mcp-config FILE]
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

Claude Code's own `total_cost_usd` is recorded as `signal.estimated_usd`. It is a client-side estimate and is never used as billed spend. On a lane without a per-call bill, `syberlabs metrics` keeps cost `unknown` and says why; turns, lookups and the other metrics are still counted. `--bare` with `subscription_local` or `seat_team_enterprise` is refused (bare mode never reads a login). On those two lanes the provider removes `ANTHROPIC_API_KEY` from Claude Code's environment, so a run labelled subscription cannot bill an API key; it never passes `SYBERLABS_CERTIFICATE_SEED` on any lane. Otherwise Claude Code inherits the caller's environment, as every provider command does.

## Trust

Without `--bare`, `claude -p` runs the hooks in the tree's `.claude/settings.json` and connects the servers in its `.mcp.json`, with no workspace trust dialog and no per-server prompt (headless page, above). The provider therefore **refuses to run non-bare unless the operator passes `--trusted-tree`**, and the refusal (`untrusted_tree`) says why. Trusting the tree means trusting the repository and every tree the model writes in it, because a repair call runs in the failed candidate's tree. Two flags narrow that in every mode: `--setting-sources user` (no project or local settings, so no project hooks) and `--strict-mcp-config` (no `.mcp.json` servers). `CLAUDE.md` is still read in non-bare mode. `--bare` needs no declaration. None of this is a sandbox: Claude Code runs with the operator's permissions, like every provider command.

## GitHits A/B

The owner-approved pilot (2026-10-23) compares the same factory, the same agent and the same work items with GitHits off and on. With this provider the only difference between the arms is `--mcp-config FILE`:

```bash
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,mcp__githits   # off
syberlabs propose --claude-code --bare --allowed-tools Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,mcp__githits \
    --mcp-config githits.json                                                                                       # on
```

Use the same `--allowed-tools` in both arms; the off arm has no GitHits server to call (`--strict-mcp-config` is always passed), so naming its tools there changes nothing. The two argv vectors differ in the last two items only (`tests/test_claude_code.py` pins this). The `init` event's MCP servers and their status are recorded in `signal.harness.mcp_servers`, so a reader can see that GitHits connected in the on arm.

## What is recorded, and what is not

Per call, on the candidate's `signal` (all parsed by the host from the stream, bounded, and never taken from the model's text):

- `model_usage`: input, output and cache-read and cache-creation tokens summed over the run's assistant messages, counted once per message id (Claude Code repeats a message's usage on every content block it streams), with `model` when one model served the run; `model_usage_by_model` when more than one did;
- `harness_turns`: the result's `num_turns`; `harness_stop`: the result's `subtype`;
- `tool_usage`: `{web_search, web_fetch, githits, other: {tool: n}}`. GitHits is any `mcp__githits__*` tool; web is `WebSearch`, `WebFetch` and the server tools `web_search` and `web_fetch`. It is bounded by `providers.tool_usage`: at most 16 other names;
- `harness`: Claude Code's version, model, effort, the credential source name (`apiKeySource`, never a credential) and the MCP servers with their status;
- `lane`, `cost_basis`, `estimated_usd`, and `skipped_paths` (how many changed files were binary, not UTF-8 or over 1 MiB, and so left out).

Per lookup (each GitHits or web tool call), one verified `observed` event, key `lookup`, source and actor `syberlabs.host`, version `<search>.<call>.<n>`, value `{tool, query_digest, query_chars, at, ok}`. `query_digest` is the SHA-256 of the query string: `query` for WebSearch and GitHits search, `url` for WebFetch, otherwise the tool input as canonical JSON. `at` is when the host read the line (the stream carries no time of its own). `ok` is false when the stream holds no successful result for that call. A call's lookups are recorded before its candidate.

**Not recorded anywhere in the journal:** the prompt, the query text, tool inputs and results, the model's text and Claude Code's session id. A reader can count and date lookups and tell whether two runs searched for the same thing; they cannot read what was searched. A call that changed nothing is refused `no_change` with its usage, but without its lane, turns or tool counts (`spec/GAPS.md` 51).

## Not built here

Budgets in turns, seconds and tokens for the lanes without a dollar bill, and pausing on Claude Code's `system/api_retry` rate-limit events, are `governor-lanes` (D3). Exposing the kernel as an MCP server (D4) and the Claude Code plugin (D5) come after. `benchmarks/model_vs_patch.py` has no `--adapter claude-code` arm yet. See `spec/GAPS.md` 51 and 52.
