# CLAUDE.md

## Public dependency lookup (GitHits)

`.mcp.json` registers the hosted GitHits MCP server (`https://mcp.githits.com`,
OAuth on first use via `/mcp`). Headless agents set `GITHITS_API_TOKEN` in the
environment; never write a token to a file.

- Use it for the exact source and docs of the dependency version pinned in
  `pyproject.toml`, and for vulnerability, changelog, and upgrade checks before
  bumping a dependency.
- It indexes public open-source code only. Never send it private SyberLabs
  code, tokens, secrets, or personal data.
