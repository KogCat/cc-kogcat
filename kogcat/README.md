<!-- mcp-name: com.kogcat/kogcat-mcp -->

# kogcat-mcp

> AI makes the answer smoother. KogCat makes the judgment sound.

A client-agnostic **stdio MCP server** for [KogCat](https://www.kogcat.com) — a local-first judgment-calibration layer. After your AI answers, it surfaces counterexamples, edge cases, and blind spots drawn from a knowledge base that lives on your own machine. It won't replace your model and it won't slow you down — the call stays yours.

This package exposes KogCat over the Model Context Protocol, so any MCP client (Cursor, Cline, Zed, VS Code, Claude Desktop, …) can use it. The Claude Code and Codex plugins ship the same capability natively.

## Install

Run directly with [uv](https://docs.astral.sh/uv/):

```bash
uvx kogcat-mcp
```

Then point your MCP client at the `kogcat-mcp` command. Example client config:

```json
{
  "mcpServers": {
    "kogcat": {
      "command": "uvx",
      "args": ["kogcat-mcp"]
    }
  }
}
```

On first run the local `om-core` sidecar is fetched and supervised automatically; a small embedding model is downloaded once. Everything runs locally over a Unix domain socket — no account, nothing leaves your machine.

## Tools

Search the knowledge base, fetch nodes and edges, run a calibration pass, and read/write durable memory — surfaced as MCP tools for the calling client.

## Links

- Website: <https://www.kogcat.com>
- Claude Code / Codex plugin: <https://github.com/KogCat/cc-kogcat>

## License

FSL-1.1-MIT — see [LICENSE](../LICENSE). Converts to MIT two years after each release.
