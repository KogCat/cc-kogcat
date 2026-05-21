---
name: pack-uninstall
description: Remove an installed vendor knowledge pack from the active KB. User Markdown is never touched; inbound user edges become dangling refs by design.
argument-hint: <@scope/name>
disable-model-invocation: true
---

# pack-uninstall — 卸载已装 pack

工具名约定:本 skill 用裸名引用 kogcat plugin 的 MCP 工具;工具列表里实际名形如 `mcp__…__pack_uninstall`,按列表实际名调用。

## 入口(MCP tool 走 sidecar)

调用 `pack_uninstall` MCP 工具,入参 `{"name": "@scope/name", "yes": false}`

参数:
- `name` **(必填)**:`@scope/name` 格式
- `yes`(默认 `false`):用户对"dangling edges"风险的显式确认

返回(成功)`{name, nodes_deleted, overlay_unmerged, dangling_edges, warnings}`。

| 状态 | 行为 |
|---|---|
| pack 未装 | 404 `PACK_NOT_FOUND` |
| 有 inbound 用户 edge **且** `yes:false` | 409 `PACK_USER_ABORTED`(`details.dangling_edges` 给数量) |
| 有 inbound 用户 edge **且** `yes:true` | 正常卸载;edges 保留在 kb.db,query 层 `dangling:true` 标记 |

## 副作用范围

| 改 | 不改 |
|---|---|
| `packs/@scope/name/` 整个目录 | 用户 `.md` 文件(永远不动) |
| `kb.db` 里 `source_pack=name` 的 nodes(CASCADE 删 outbound edges) | inbound user edges(故意保留 → dangling) |
| `om.lock` 的 `packs[name]` 入口 | 用户 `relationships[*].object` frontmatter |
| 本机 ontology overlay 中该 pack 的 block | base ontology |

## 标准两步流(LLM MUST)

1. 第一次调用 `yes:false`,**MUST** 把 `dangling_edges` 数量回显给用户
2. 用户**明确**回 "yes/确认" 后才 `yes:true` 重试 —— 单次 user prompt + 多次 tool call 不能跳过这步

## 相关

- 列出已装:`/kogcat:pack-list`
- 升级而非卸载:`/kogcat:pack-upgrade`
- 单包详情:`/kogcat:pack-info @scope/name`
