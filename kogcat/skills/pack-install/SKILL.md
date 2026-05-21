---
name: pack-install
description: Install a vendor knowledge pack from a local .ompack file. Stages content + ontology delta into the active KB via the om-core sidecar.
argument-hint: <path-to-.ompack>
disable-model-invocation: true
---

# pack-install — 安装预制知识包

工具名约定:本 skill 用裸名引用 kogcat plugin 的 MCP 工具;工具列表里实际名形如 `mcp__…__pack_install`,按列表实际名调用。

## 入口(MCP tool 走 sidecar)

调用 `pack_install` MCP 工具,入参 `{"archive_path": "<absolute or kb_root-relative path>"}`

参数:
- `archive_path` **(必填)**:**LOCAL** filesystem 路径,sidecar **不**联网。URL 下载是 caller 责任 —— 自己 `curl` / `wget` / `gh release download` 到本地路径,再传给本 tool

返回(成功)`{name, version, installed_at, integrity, stats: {nodes, edges, vectors, skipped_edges}, noop, warnings}`。

| 状态 | 行为 |
|---|---|
| 同 name+version 已装 | `noop:true`,DB 无变更,返回 0 |
| 同 name 不同 version | 409 `PACK_VERSION_CONFLICT` → 改用 `/kogcat:pack-upgrade` |
| spec_version 不对 | 400 `PACK_INVALID_SPEC`(v1 旧格式不支持) |
| integrity 不匹配 | 400 `PACK_INTEGRITY_MISMATCH` |
| min_db_schema 高于本地 | 400 `PACK_SCHEMA_TOO_OLD`(先升 om-core / 重启 sidecar 跑 migration) |
| ontology 冲突 | 409 `PACK_ONTOLOGY_CONFLICT`(`details.message` 给冲突 predicate) |

LLM **MUST** 把 `warnings: [...]` 原样回显给用户(包含 ontology base_version 不一致 / 依赖未装的提示)。

## 用户触发约定

LLM **MUST NOT** 自动安装 pack。本 skill 只在用户**明确说**"装这个 pack" / "install" / 显式调用 `/kogcat:pack-install` 时执行。

## 相关

- 列出已装:`/kogcat:pack-list`
- 单包详情:`/kogcat:pack-info @scope/name`
- 升级:`/kogcat:pack-upgrade <新.ompack 路径>`
- 卸载:`/kogcat:pack-uninstall @scope/name`
