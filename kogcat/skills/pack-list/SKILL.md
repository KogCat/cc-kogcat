---
name: pack-list
description: List installed vendor packs from om.lock — name, version, pinned state, install time.
---

# pack-list — 列出已装 pack

工具名约定:本 skill 用裸名引用 kogcat plugin 的 MCP 工具;工具列表里实际名形如 `mcp__…__pack_list`,按列表实际名调用。

## 入口(MCP tool 走 sidecar)

调用 `pack_list` MCP 工具(只读,无入参)。返回 `{packs: {<@scope/name>: {version, integrity, installed_at, pinned, source}}}`。

- **未装任何 pack 时 `packs` 为空 dict `{}`**,**MUST NOT** 把空当错误
- `install` / `uninstall` / `upgrade` 也走 MCP(`pack_install` / `pack_uninstall` / `pack_upgrade`,见对应 skill),但 LLM **MUST NOT** 自动调用 —— 必须用户**明确触发**,且 upgrade 必须先 `dry_run` 再 `yes`

## 相关

- 单包详情:`/kogcat:pack-info @scope/name`
