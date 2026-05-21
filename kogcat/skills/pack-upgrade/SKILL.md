---
name: pack-upgrade
description: Upgrade an installed pack to a new .ompack version. MAJOR jumps rewrite user Markdown + kb.db edges per migrations.yaml; three-layer rollback guards the apply.
argument-hint: <path-to-new-.ompack> [dry_run|yes|force]
disable-model-invocation: true
---

# pack-upgrade — 升级已装 pack

工具名约定:本 skill 用裸名引用 kogcat plugin 的 MCP 工具;工具列表里实际名形如 `mcp__…__pack_upgrade`,按列表实际名调用。

## 入口(MCP tool 走 sidecar)

调用 `pack_upgrade` MCP 工具,入参 `{"archive_path": "<path>", "dry_run": false, "yes": false, "force": false}`

参数:
- `archive_path` **(必填)**:新 `.ompack` 的 **LOCAL** 路径;sidecar 从 manifest 读 pack name,自动定位本地已装条目作为 upgrade 目标
- `dry_run`(默认 `false`):只算 plan,不动 kb.db / 不改 .md / 不动 overlay
- `yes`(默认 `false`):plan 非空时**必须**显式 true 才会真正 apply
- `force`(默认 `false`):pack `pinned=true` 时绕过

返回 `{name, old_version, new_version, dry_run, applied, noop, stats?, plan_summary, plan_text?, warnings}`。

| 状态 | 行为 |
|---|---|
| pack 未装 | 404 `PACK_NOT_FOUND`(用 `/kogcat:pack-install` 而不是 upgrade) |
| `old_version == new_version` | `noop:true` |
| `pinned=true` 且 `force:false` | 409 `PACK_PINNED` |
| MAJOR 跨度违反 semver / migrations 覆盖不全 | 409 `PACK_SEMVER_VIOLATION`(`details` 列缺漏 stable_id) |
| ontology delta 冲突 | 409 `PACK_ONTOLOGY_CONFLICT` |
| plan 非空且 `yes:false` | 409 `PACK_USER_ABORTED`(用户必须看完 plan 再 yes) |
| apply 中途异常 | 500 `PACK_APPLY_FAILED`,自动三层回滚(MD 字节快照 + `.bak` 旧 pack + DB 事务) |

## 标准两步流(LLM MUST)

1. **先 dry_run**:`{"archive_path": "...", "dry_run": true}` → 拿 `plan_text` / `plan_summary` 回显给用户(改几个 .md、改几条 DB edge、几条 migration)
2. **再 apply**:用户明确"upgrade / 同意"后,`{"archive_path": "...", "yes": true}` 真改

`dry_run` 跳过等于把"会动用户 .md"的事情**自动**化,LLM **MUST NOT** 跳。

## 安全机制

- **MD 字节快照**:`plan.markdown_edits` 涉及的每个 .md 文件在改前读 bytes,失败时整文件恢复
- **`.bak` sidecar**:旧 pack 整个目录 rename 到 `<name>.bak`,失败时 rename 回来
- **DB 事务**:整段 DB mutation 在 `with conn:` 内,SQLite 自动 ROLLBACK
- 三层任何一层异常 → 自动尝试反向恢复,但**告警用户去看 git diff** 确认

## 升级跨 MAJOR 的额外要求

migrations.yaml(随新 pack 一起发)**MUST** 覆盖所有从老版本删除的 stable_id;否则 sidecar 拒绝。这是 author 责任,不是 user 责任 —— 升级失败时去 issue 上找作者。

## 相关

- 列出已装:`/kogcat:pack-list`
- 装新 pack(非升级):`/kogcat:pack-install`
- 卸载:`/kogcat:pack-uninstall @scope/name`
