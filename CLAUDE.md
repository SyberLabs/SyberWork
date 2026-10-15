# CLAUDE.md

## Public dependency lookup (GitHits)

`.mcp.json` invokes the private MasterMind usage runtime. Install and load both
absolute runtime paths as described below before starting the agent. Headless
agents also set `GITHITS_API_TOKEN`; interactive OAuth uses the private bridge.
Never write a token to a file.

- Use it for the exact source and docs of the dependency version pinned in
  `pyproject.toml`, and for vulnerability, changelog, and upgrade checks before
  bumping a dependency.
- It indexes public open-source code only. Never send it private SyberLabs
  code, tokens, secrets, or personal data.


## Private GitHits usage runtime

Install the private MasterMind `tools/githits-usage` package and invoke its `install.mjs` with an absolute Node 24 executable. Load the two non-secret absolute paths from its ignored `data/environment.json` before starting the agent: `GITHITS_USAGE_NODE` (the executable) and `GITHITS_USAGE_HOME` (the tool directory). The tracked MCP entry invokes that private executable and `mcp.mjs` directly; it requires neither Node on PATH nor a repository-root working directory.

For headless authentication, also set `GITHITS_API_TOKEN` in the environment. Interactive OAuth is handled by the private bridge. Never put a token or usage ledger into this repository. Missing private runtime paths stop the tool; do not restore a direct unaccounted hosted fallback. The product runtime is unchanged.
