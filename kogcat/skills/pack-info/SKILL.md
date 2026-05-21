---
name: pack-info
description: Show an installed pack's full manifest + lockfile entry (stats, ontology delta, dependencies, integrity).
argument-hint: <@scope/name>
---

# pack-info — 查看 pack 详情

工具名约定:本 skill 用裸名引用 kogcat plugin 的 MCP 工具;工具列表里实际名形如 `mcp__…__pack_info`,按列表实际名调用。

## 入口(MCP tool 走 sidecar)

调用 `pack_info` MCP 工具,入参 `{"name": "@scope/name"}`:

| 状态 | 返回 |
|------|------|
| 未安装 | `{"found": false, "name": "<name>"}` |
| 已安装 | `{"found": true, "name": "<name>", "lock": {...}, "manifest": {...}}` |

LLM **MUST** 用 `found` 字段判存在,**MUST NOT** 假设结构存在 / 假设字段非空。

输出合并的 `{lock, manifest}` JSON:
- `lock`:`version` / `integrity` / `installed_at` / `pinned` / `source`
- `manifest`:完整 `pack.json`(`verticals` / `stats` / `ontology.{base_version, delta_hash, introduces_*}` / `indexes.{embeddings, graph}` / `dependencies` / `excludes`)

## 常见用途

- 装前快扫已装内容:`/kogcat:pack-list` 拿 name → `/kogcat:pack-info @x/y` 看 stats / license
- 排查冲突:某 predicate 被 flagged → 看是哪个 pack 引入(`manifest.ontology.introduces_predicates`)
- 决定是否升级:对比作者 homepage / changelog 与本地 version
