# KogCat

> Local knowledge calibration for Claude Code. Your notes never leave your machine.

[English](./README.md) | [中文](./README.zh-CN.md)

AI gives you fast answers. KogCat makes those answers worth trusting.

Every AI gives a confident reply. What it can't tell you is whether that reply is grounded in the knowledge you've already accumulated — or whether it sidesteps it entirely. **KogCat** calibrates every reply against your personal knowledge base (your notes, your research, your past conclusions), surfaces what the model missed, and doesn't replace the model or slow you down.

KogCat has no servers of its own. The calibration engine runs as a local process on your machine, inside Claude Code. Your knowledge base is never read by any remote service — only what you type into the chat is. The LLM is still the one you were already using; KogCat reads its complete reply and evaluates it locally against your notes. No account, no subscription, no third-party custody of your knowledge.

---

## See the difference

You ask: *"I read for 30 minutes every day but feel like nothing sticks. Should I take more detailed notes while reading?"*

**Without KogCat:**

> Taking detailed notes is a great way to retain more. Try the Cornell method — split each page into cue, notes, and summary columns. Highlight key passages and paraphrase them later. Spaced-repetition tools like Anki help too.

**With KogCat — calibrated against your KB:**

> **KogCat has an angle ›**
>
> Adding more notes while reading is likely to make this worse, not better. The bottleneck isn't capture — it's retrieval practice. Your KB has a claim you marked high-confidence: *"the 'feels like I get it' peak during re-reading is the least reliable indicator of actual recall."* Learning research consistently shows that closing the book and writing what you remember — even when it's incomplete — produces stronger retention than any read-along note format. Adding richer notes only deepens the *feeling* of learning, not what you actually remember.
>
> Before tweaking note format, try this once: finish a section, close everything, and write a rough summary from memory. Compare what you actually retained versus what you thought you had.

---

## Two modes, one plugin

**Ambient mode** keeps your normal conversation rhythm. Claude answers as usual. KogCat calibrates the reply against your KB in the background and only surfaces a note when its judgment differs meaningfully from the model's. The original answer is never silently replaced — you decide what to take.

**Active mode** puts your KB first. When you know your notes have something relevant and want it up front instead of as an aside, use `/kogcat:query`. KogCat's own structured answer comes first: a clear conclusion, the preconditions that change the recommendation, a concrete next step. Useful when a confident-sounding wrong answer would cost you something.

---

## What touches your data

- **Your knowledge base**: never leaves your machine. KogCat reads only the directory you point at, and only ever sees content you paste into the chat.
- **Your conversation**: goes to the Claude provider you're already using, identical to having no plugin. KogCat reads the complete reply, not messages in flight.
- **The calibration process**: runs entirely in a local MCP server process. Results never leave your machine.
- **The om-core engine binary**: downloaded once from a pinned GitHub Release, verified against a sha256 manifest before running. No silent updates.

---

## Requirements

- Claude Code
- macOS (Apple Silicon or Intel), Linux x86_64 *(experimental)*, or Windows x86_64 *(experimental)*
- Python 3.9+ — preinstalled on macOS and most Linux distributions. **On Windows, install Python 3 yourself** and make sure `python3` is on `PATH` (the Microsoft Store build registers `python3` automatically; with the python.org installer, tick *Add python.exe to PATH*).

---

## Install

Inside Claude Code:

```
/plugin marketplace add KogCat/cc-kogcat
/plugin install kogcat
```

You'll be prompted for:

- **`kb_root`** (required) — absolute path to a directory that will hold your KB (kb.db + your Markdown notes). Point it at an existing notes folder, or pick a fresh directory — KogCat will initialize it on first use.
- **`hf_endpoint`** (optional) — HuggingFace mirror URL. **Users in China should set this to `https://hf-mirror.com`** to avoid timeouts on the first-run embedding model download (~90 MB). Leave blank elsewhere. Has no effect once the model is cached.

**First-run downloads** (one-time, automatic on next session start):

| Artifact | Size | Purpose |
|---|---:|---|
| `om-core-bin` | ~40 MB | Local engine — query, KB storage, vector index |
| Embedding model | ~90 MB | For semantic search |

Total first-run footprint: ~130 MB. After this, sessions start in seconds.

A progress banner appears automatically in the system prompt during first run. You can also run **`/kogcat:status`** at any time to see live state:

```
om-core 状态
──────────────────────────────
[A] binary       ⏳ downloading   24.5MB / 47.9MB (51%), 已等 18s
[B] sidecar      · no_server_json sidecar 还未起来(binary ready 后 launchd 应自动起)
[C] 嵌入模型     ⏳ downloading   45.0MB / 90.0MB (50%), 已等 22s
[D] KB 绑定      ✅ ready        /Users/you/notes
```

When all four rows show `✅ ready`, you're done. Typical wait on a fast connection: ~2 min.

You can keep chatting during downloads — KogCat tools will report `OM_CORE_BIN_DOWNLOADING` / `EMBEDDING_MODEL_WARMING_UP` and auto-retry once each component is ready. Ambient calibration kicks in once everything is green.

---

## Commands

| Slash command | What it does |
|---|---|
| `/kogcat:query <question>` | Explicit lookup against your KB. Returns warn / enrich / reinforce / answer / gap. |
| `/kogcat:status` | Read-only local-state diagnostic (binary fetcher / embedding model / sidecar / KB binding). Use this if first-run seems stuck. |
| `/kogcat:pack-list` | List installed vendor knowledge packs. |
| `/kogcat:pack-info <pack-name>` | Show a pack's manifest, stats, and dependencies. |
| `/kogcat:pack-install <path.ompack>` | Install an `.ompack` file (read-only, namespaced under `packs/@scope/name/`). |
| `/kogcat:pack-uninstall <pack-name>` | Remove an installed pack. Your own notes are never touched. |
| `/kogcat:pack-upgrade <path.ompack>` | Upgrade an installed pack, with migrations applied atomically. |

Ambient mode needs no command — it's on as soon as the four `/kogcat:status` rows are green.

---

## Troubleshooting

`/kogcat:status` reads local state files only and never mutates anything. The JSON form (`/kogcat:status --json`) is for support tickets.

| Symptom | Where to look |
|---|---|
| Binary stuck at `pending` with no progress fields | Bootstrap hook may not have fired — fully quit and reopen Claude Code (not reload). |
| Binary stuck at `downloading` with no progress for 30+ s | `~/Library/Logs/om/om-core-fetch.log` (macOS) holds the fetcher's stderr. |
| Embedding model download times out (China users) | Set `hf_endpoint = https://hf-mirror.com` in plugin config, then restart Claude Code. |
| `sidecar unreachable` after binary ready | Supervisor may be respawning — wait 5 s and re-check. If persistent, restart Claude Code. |
| MCP tools return `OM_CORE_BIN_DOWNLOADING` / `EMBEDDING_MODEL_WARMING_UP` | First-run still in progress — wait for `/kogcat:status` to show all green. |

---

## License

MIT — see [LICENSE](./LICENSE).
