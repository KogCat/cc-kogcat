---
name: memory-consolidate
description: Review and clean up the user's persistent memory store. Use ONLY when the user explicitly asks to consolidate / review / clean up memory — e.g. "整理一下 memory", "review my memories", "清一下知识库的 memory", "/kogcat:memory-consolidate". Never run automatically. All mutations require user approval.
disable-model-invocation: true
---

# memory-consolidate — Memory consolidation protocol

工具名约定:本 skill 用裸名引用 kogcat plugin 的 MCP 工具(`memory_list` / `memory_get` / `memory_save` / `memory_delete`);工具列表里实际名形如 `mcp__…__memory_list`(CC 与 Codex 前缀不同),按列表实际名调用。

User-driven review pass over om memory store. All mutations via the `memory_*` MCP tools; LLM MUST NOT `Read` memory files directly or hand-edit `MEMORY.md`.

## MUST NOT

1. Auto-trigger or schedule this skill — consolidation is human-driven
2. Bulk-approve / bulk-delete
3. Delete a memory just because it hasn't been read in months — rare-access ≠ stale
4. Invent new memories during consolidation — exception: the `user_lens` synthesis in §7, which only distills already-approved entries and introduces no new facts
5. Read or modify KB nodes / edges / sources (use `query` instead)

## Steps

### §1 List

`memory_list` (no params). If `items` is empty, tell the user there's nothing to consolidate and **stop**.

### §2 Pull bodies for ambiguous ones

`memory_get {"name": "<name>"}` only for memories where `description` doesn't make the call clear. MUST NOT fetch all upfront.

### §3 Categorize

Place each memory in **exactly one bucket** with a one-sentence reason:

| Bucket | Meaning |
|--------|---------|
| **keep** | Still accurate, still relevant. No change. |
| **revise** | Substantively right but description / body stale. Propose new text. |
| **merge** | Overlaps with another memory by ≥80%. Propose which `name` survives + merged body. |
| **delete** | Superseded, contradicted, or no longer relevant. Reason MUST say what made it obsolete. |

### §4 Render review card

```
## keep (N)
- name — reason

## revise (N)
- name — reason
  proposed description: ...
  proposed body: ...

## merge (N)
- name_a + name_b → name_a
  merged body: ...

## delete (N)
- name — reason
```

### §5 Confirm per item

Ask the user to approve / skip / edit **each non-`keep` item**. MUST NOT ask in bulk.

### §6 Execute

| Action | Tool |
|--------|------|
| `revise` / `merge survivor` | `memory_save {"name", "description", "type", "source": "user_explicit", "body"}` |
| `merge non-survivor` | `memory_delete {"name"}` (after survivor saved) |
| `delete` | `memory_delete {"name"}` |
| `keep` | no API call |

`memory_save` for `revise` / `merge survivor` MUST use `source: "user_explicit"`.

Type changes: MUST `memory_delete` + `memory_save` new (single `memory_save` does NOT re-classify).

### §7 Lens synthesis(`user_lens`,可选)

`user_lens` 是单条 `type=lens` 的合成画像 —— kogcat 校准时据它把校正内容调到用户视角。它不是原子记忆,是对已批准的 `user` / `feedback`(必要时 `project`)条目的提炼。`user_lens` 是唯一的 `lens` 条目,重复合成即覆盖。

1. 完成 §1–§6 后,问用户是否要(重新)合成 `user_lens`。不要 → 结束。
2. `memory_get` 读齐相关 `user` / `feedback` 条目的 body。
3. 综合出 body,四段固定结构:
   - `## 身份与方向` — 用户是谁、所在领域 / 角色、在往哪走
   - `## 思考侧重` — 评估问题时最先权衡什么、默认视角
   - `## 有效论述角度` — 什么切入角度 / 用词 / 框架能讲到用户心里
   - `## 需补偿盲区` — kogcat 应主动补的盲点
4. 把 body 渲染给用户,逐段确认 / 编辑。MUST NOT 整体一次批准。
5. 批准后:`memory_save {"name": "user_lens", "description": "<≤200 字一句话画像>", "type": "lens", "source": "user_explicit", "body": "<合成 body>"}`。

**MUST**:lens body 只能来自已批准记忆条目的提炼,不得引入任何新事实。body ≤4KB utf-8。

## Failure modes

| Code | Action |
|------|--------|
| `409 memory.duplicate_candidate` | Surface conflict — user probably wants a `merge` |
| `400 memory.rejected_by_policy` | Rephrase body and retry, or escalate to `source: "user_explicit"` |
| `400 memory.body_too_long` | Split or trim (limit: 4KB utf-8) |
| `400 memory.body_has_path` | Remove path, retry |
