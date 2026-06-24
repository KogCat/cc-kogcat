# KogCat

> AI makes the answer smoother. KogCat makes the judgment sound.

[English](./README.md) | [中文](./README.zh-CN.md) · **Website:** <https://www.kogcat.com>

A local-first **judgment calibration layer** for Claude Code and Codex. Before you act on an AI answer, it surfaces the counterexamples, the boundaries, the blind spots — drawn from a knowledge base that lives on your machine. It won't replace your model. It won't slow you down. The call stays yours.

---

## See the difference

*"I read 30 minutes a day but nothing sticks. Should I take more detailed notes?"*

**Plain AI** — Try the Cornell method, highlight key passages, add Anki for spaced repetition.

**KogCat** — More notes will likely make it worse. The bottleneck isn't capture. It's retrieval. Your knowledge base holds a claim you marked high-confidence: *the "I get it" feeling while re-reading is the least reliable signal of real recall.* So try this once — finish a section, close the book, write what you remember. Then compare it to what you thought you had.

---

## When it speaks up

Built for judgment, not lookup. KogCat speaks up for the calls that cost you when they're wrong — decisions, tradeoffs, comparisons, critiques, strategy. It stays quiet for the rest: lookups, definitions, code, summaries, translation. And even on a judgment call, it adds a note only when it sees something the model didn't.

- **Automatic.** Ask a judgment question in conversation. A note appears only when there's something worth saying — and your original answer is never touched.
- **On demand.** `/kogcat:query <question>` puts your knowledge base first: a conclusion, the conditions that change it, a next step.

---

## Install

**Claude Code**

```
/plugin marketplace add KogCat/cc-kogcat
/plugin install kogcat
```

**Codex**

```
codex plugin marketplace add KogCat/cc-kogcat
codex plugin add kogcat@kogcat
```

**After installing, fully quit and reopen Claude Code (or restart Codex).** The first-run download only starts on the next fresh session — the install command alone won't begin it.

On first launch, KogCat quietly downloads its engine (~40 MB) and embedding model (~90 MB). Once. About a minute on a good connection. Keep working while it does — run `/kogcat:status` anytime to watch each piece come online.

---

## Use it from any MCP client

KogCat's calibration engine runs as a local sidecar; the Claude Code / Codex plugin is just one client of it. Any MCP-capable tool — Cursor, Cline, Zed, VS Code, Claude Desktop — can use the same engine through a standalone stdio MCP server.

Add this to your client's MCP config (field names vary slightly by client; most use an `mcpServers` map):

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

Requires [uv](https://docs.astral.sh/uv/), on macOS (Apple Silicon) or Windows x86_64 (same engine, same platforms as below). On first run it downloads the engine + embedding model and registers a background sidecar — the same one-time setup as the plugin, shared by every client on the machine. It exposes the knowledge-base tools (`search`, `node`, `edges`, `calibrate`, `calibrate_review`, and the `memory_*` family) for your model to call.

**What you give up vs. the plugin.** The Claude Code / Codex plugin adds two host conveniences a generic MCP client has no hook for: calibration that fires *automatically* on judgment questions, and a memory index injected into context at session start. With a standalone server your model reaches the same knowledge base, but it's the model that decides to call those tools — or you ask it to "use KogCat" — rather than a hook firing them for you. You never type the tool names (`search`, `calibrate_review`, `memory_*`); they're the model's to call.

---

## Privacy

- **Your knowledge base** stays on your machine. KogCat reads only the folder you point it at.
- **Your conversation** goes to the same Claude or Codex you already use. Nothing extra, nowhere else.
- **Calibration** happens in a local process. The results never leave.
- **The engine** comes from a public release channel, checked against a sha256 manifest before it ever runs.

No account. No subscription. No one else holding your knowledge.

---

## Commands

| Command | What it does |
|---|---|
| `/kogcat:query <question>` | A knowledge-base-first answer: conclusion, conditions, next step. |
| `/kogcat:status` | A read-only local check. Reach for it if first launch seems stuck. |
| `/kogcat:memory-consolidate` | Review and tidy saved memories — every change is yours to confirm. |

Automatic calibration needs no command.

---

## Requirements

- Claude Code or Codex
- macOS (Apple Silicon) or Windows x86_64 — Intel Mac and Linux not yet supported
- Python 3 on `PATH` — already there on macOS; on Windows, install it yourself (tick *Add python.exe to PATH*)

---

## License

FSL-1.1-MIT — see [LICENSE](./LICENSE). Converts to MIT two years after each release.
