---
name: query
description: Query om ONLY on explicit triggers — explicit invocation of this skill (/kogcat:query), or user phrase contains 知识库 / KB / 结合笔记 / 根据笔记 / 我笔记里 / 从笔记看 / 查一下笔记. Without an explicit trigger, om does not participate. MUST NOT trigger on general questions, opinions, comparisons, assertions, domain keywords alone, or timeliness questions without one of the explicit phrases above.
---

# query — 知识库查询协议

工具名约定:om 能力**唯一通道** = kogcat plugin 注册的 MCP 工具。本 skill 用裸名引用(`calibrate` / `node` / `edges` / `search`);工具列表里实际名形如 `mcp__…__calibrate`(CC 与 Codex 的 host 前缀不同),按列表里实际名调用。

## 触发(MUST 显式)

| 信号 | `source_kind` |
|---|---|
| 显式调用本 skill(`/kogcat:query`) | `cc.parallel_query.kb_first` |
| 用户问句含:知识库 / KB / 结合笔记 / 根据笔记 / 我笔记里 / 从笔记看 / 查一下笔记 | `cc.parallel_query` |

无显式触发 → om 不参与,本协议不加载。

## 双路并行

| 路 | 工具 | 何时发 |
|----|------|------|
| 主路 | `WebSearch`(必要时叠加 LLM 内部知识);产出 `web_draft` | 先发 |
| 校准路 | `calibrate` MCP 工具 | 主路 `web_draft` 完成后发,`text` = 整段 `web_draft`(< 8KB) |

校准入参:

```json
{
  "text": "<web_draft 整段, <8KB>",
  "top_k": 5,
  "source_kind": "<见触发表>"
}
```

校准返回 `{directive, debug?}`。skill **MUST** 只消费 `directive`,**MUST NOT** 检查任何其他字段。

## directive 消费规则(唯一权威)

`directive` 形如:

```json
{
  "should_emit": true,
  "placement": "front | inline | suffix | none",
  "phrasing": "<服务端合成的措辞片段>",
  "inline_refs": [{"title": "<concept-name>", "stable_id": null}],
  "user_facing_note": null,
  "extras": {}
}
```

渲染规则(**全部 6 条**):

1. `should_emit=false` → 主答纯 web,不提 Kogcat,结束
2. `placement="front"` → 把 `phrasing` 整段单独前置一段
3. `placement="inline"` → 把 `phrasing` 衔接进主答正文
4. `placement="suffix"` → 把 `phrasing` 放主答末尾
5. `inline_refs` 非空 → 在主答里相应位置插 `[Kogcat:<title>]`
6. `user_facing_note` 非空 → 主答末尾追加该注释一行

`extras.primary_mode="kb"` 表示服务端已升档到"以 KB 为主答"——这时 skill **MUST** 先调一次 `node` MCP 工具(`{"title": inline_refs[0].title}`)拿 `body_md`,并将其作为主答正文起头。其余 `extras.*` 字段一律忽略(forward-compat)。

## lens framing(用户视角适配)

渲染 directive 前,加载用户画像:`memory_get {"name": "user_lens"}`。

- 404 / 无 `user_lens` → 跳过本节,directive 按上面 6 条原样渲染,行为与升级前完全一致。
- 命中 → `user_lens` body 描述用户的「身份与方向 / 思考侧重 / 有效论述角度 / 需补偿盲区」。渲染 `phrasing` 时据此重组**进入角度、用词、侧重** —— 从用户惯用的论述角度切入,用户默认先权衡的维度先行。

**硬边界(MUST):lens 只换"怎么说",不换"说什么"。**

1. 判定不变 —— warn 仍是 warn,KB 反对 / 校准 / 印证 / 主答的结论一字不动。
2. `placement` / `inline_refs` / `[Kogcat:<title>]` 引用 / `user_facing_note` / `extras.primary_mode` 行为全不变。
3. 不新增 directive 或 KB 未给出的结论 / 事实;`primary_mode="kb"` 时 KB 节点正文按原义呈现,lens 只调引入措辞。
4. `phrasing` 仅为覆盖度 / 时效提示(如「Kogcat 未覆盖该主题」)时无需重组,原样渲染。
5. `user_lens` 的存在 / 内容 / 名字,以及 directive 内部字段,一律 MUST NOT 外显。

## 工具

- `calibrate` — 主校准路(read-only,必走)
- `memory_get` — 渲染前取 `user_lens` 做视角适配;404 即跳过,非必走
- `node` / `edges` — 仅在 `extras.primary_mode="kb"` 或需要精读时
- `search` — 按需,主路径走 calibrate

## 执行风格

- 工具调用前一句话过渡,不复述参数
- `directive` 内部字段(`placement` / `phrasing` 等)**MUST NOT** 外显
- web 来源 MUST 显式标;LLM 内部知识 MUST 标"基于 LLM 内部知识"

## MUST NOT

1. 跳过校准路直接 `Glob` / `Grep` / `find` 扫 om
2. 跳过主路 web 仅用 Kogcat 输出主答(`extras.primary_mode="kb"` 除外)
3. 任何 SQL client / DB 工具直查 KB 持久化文件
4. `Read` KB 内部归档目录下的快照
5. 读取或转述 `debug` 字段(仅 ops 用)
6. 时效性问题跳过 web 主路
7. 用 `Bash`(`curl` / `httpx` / `python -c`) 直访 om sidecar(UDS / TCP / `server.json` / Bearer token);只走注册的 MCP 工具
8. 读取或拼装 `~/Library/Application Support/om/server.json` / `om.sock` / `~/.claude/om-core-cache/**` 任意路径
