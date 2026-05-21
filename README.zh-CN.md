# KogCat

> 给 Claude Code 接上你的本地知识库 — 知识不出本机。

[English](./README.md) | [中文](./README.zh-CN.md)

AI 给你快速的答案。KogCat 让这些答案值得信赖。

每一个 AI 都会给出自信的回复。它无法告诉你的是：这个回复是否根植于你自己多年积累的知识——还是恰恰绕开了它。**KogCat** 将每一条回复与你个人的知识库（你的笔记、研究、过去的结论）做一次校准，把模型遗漏的部分浮出来，既不替换它，也不拖慢你。

KogCat 没有自己的服务器。校准引擎作为本地进程运行在你的机器上，在 Claude Code 内部。你的知识库不会被任何远程服务读取——只有你在对话框里输入的内容才参与其中。大语言模型依然是你正在使用的那个；KogCat 读取它返回的完整回复，在本地对照你的笔记进行评估。整个过程不需要账号、订阅，也不需要把你的知识托付给任何第三方。

---

## 看看区别

你问：*"我每天读书 30 分钟，但感觉什么都记不住。我是不是应该在阅读时记更详细的笔记？"*

**没有 KogCat：**

> 记详细的笔记是提升记忆效果的好方法。可以试试康奈尔笔记法：把页面分成提示、笔记和总结三栏。阅读时标注重点段落，之后再用自己的话复述。Anki 等间隔重复工具也有助于巩固你记录的内容。

**有 KogCat — 经你的知识库校准后：**

> **KogCat 有一个补充视角 ›**
>
> 阅读时记更多笔记，很可能让问题更严重，而不是更好。瓶颈不在于捕捉信息，在于检索练习。你的知识库里有一条你标记为高置信度的 claim：*"重读时'感觉自己懂了'的峰值，恰恰是最不可靠的实际记忆指标。"* 学习研究持续表明：合上书本、写下你所记得的内容——哪怕不完整——比任何边读边记的方式都能产生更强的记忆留存。增加更详细的笔记，只会加深学习的"感觉"，而不会改变真正记住的量。
>
> 在调整笔记格式之前，先试一次：读完一节，关掉所有内容，凭记忆写一段粗略的总结。比较你实际记住了多少，和你以为自己记住了多少。

---

## 两种模式，一个插件

**环境模式**保持你正常的对话节奏。Claude 照常回答。KogCat 在后台将回复与你的知识库进行校准，只有当它的判断与模型的回复有实质差异时，才显示一条提示。原始回答不会被静默替换——由你决定接受什么。

**主动模式**将你的知识库置于首位。当你知道自己的笔记里有相关内容、希望它直接呈现而不是附在后面时，使用 `/kogcat:query`。KogCat 自己的结构化回答优先展示：一个清晰的结论、改变建议的前提条件、一个具体的下一步。适合用在"一个听起来有把握的错误答案代价很高"的问题上。

---

## 什么碰了你的数据

- **你的知识库**：永远不离开你的机器。KogCat 只读取你指向的目录，除非你自己把内容粘贴进对话，否则它不会访问其他文件。
- **你的对话**：发送给你正在使用的 Claude provider，和没有插件时一模一样。KogCat 读取的是完整返回的回复，不是消息在途中的内容。
- **校准过程**：完全在本地 MCP server 进程内运行。结果不会离开你的机器。
- **om-core 引擎二进制**：从固定的 GitHub Release 下载一次，在运行前对照 sha256 manifest 验证。不静默更新。

---

## 系统要求

- Claude Code
- macOS（Apple Silicon 或 Intel）、Linux x86_64 *(试验性)*、Windows x86_64 *(试验性)*
- Python 3.9+ —— macOS 与多数 Linux 发行版已自带；**Windows 需自行安装 Python 3**,并确保 `python3` 在 `PATH` 上（Microsoft Store 版会自动注册 `python3`；用 python.org 安装包则勾选 *Add python.exe to PATH*）。

---

## 安装

在 Claude Code 里跑：

```
/plugin marketplace add KogCat/cc-kogcat
/plugin install kogcat
```

会让你填：

- **`kb_root`**（必填）——知识库根目录的绝对路径，里面会有 `kb.db` 和你的 Markdown 笔记。可以指向已有笔记目录，也可以新建空目录——KogCat 首次使用时会初始化它。
- **`hf_endpoint`**（可选）——HuggingFace 镜像地址。**国内用户建议填 `https://hf-mirror.com`**，否则首次嵌入模型下载（~90MB）容易超时。海外用户留空。模型缓存命中后此设置不再起作用。

**首次启动会自动下载**（一次性）：

| 文件 | 大小 | 干什么的 |
|---|---:|---|
| `om-core-bin` | ~40 MB | 本地引擎 — 查询 / KB 存储 / 向量索引 |
| 嵌入模型 | ~90 MB | 语义检索用 |

首次总计：~130 MB。装完后启动只要几秒。

首次启动时系统提示里会自动显示进度横幅。你也可以随时跑 **`/kogcat:status`** 查看实时状态：

```
om-core 状态
──────────────────────────────
[A] binary       ⏳ downloading   24.5MB / 47.9MB (51%), 已等 18s
[B] sidecar      · no_server_json sidecar 还未起来(binary ready 后 launchd 应自动起)
[C] 嵌入模型     ⏳ downloading   45.0MB / 90.0MB (50%), 已等 22s
[D] KB 绑定      ✅ ready        /Users/you/notes
```

四行都是 `✅ ready` 就完事了。良好网络下全程 ~2 分钟。

下载期间你可以正常聊天 —— KogCat 工具会返回 `OM_CORE_BIN_DOWNLOADING` / `EMBEDDING_MODEL_WARMING_UP` 并在对应组件就绪后自动重试。环境模式在四行全绿后自动启用。

---

## 命令

| Slash command | 干什么 |
|---|---|
| `/kogcat:query <问题>` | 显式查 KB，返回 warn / enrich / reinforce / answer / gap。 |
| `/kogcat:status` | 看本地组件状态（binary / 嵌入模型 / sidecar / KB）。只读诊断，首次启动卡住时第一时间用这个。 |
| `/kogcat:pack-list` | 列出已装的第三方知识包。 |
| `/kogcat:pack-info <pack 名>` | 看一个 pack 的清单 / 统计 / 依赖。 |
| `/kogcat:pack-install <path.ompack>` | 把 `.ompack` 文件装进 KB（只读 / 命名空间隔离）。 |
| `/kogcat:pack-uninstall <pack 名>` | 卸载，你自己的笔记永远不会被动。 |
| `/kogcat:pack-upgrade <path.ompack>` | 升级已装 pack，自动跑迁移，原子提交。 |

环境模式无需任何命令 —— `/kogcat:status` 四行全绿后自动启用。

---

## 故障排查

`/kogcat:status` 只读本地状态文件，不修改任何东西。JSON 形式（`/kogcat:status --json`）给排障工单用。

| 症状 | 看哪里 |
|---|---|
| binary 卡在 `pending` 且无 progress 字段 | bootstrap hook 可能没跑 — 完全关掉 Claude Code 再开（不是 reload）。 |
| binary 卡在 `downloading` 但 30+ 秒无新进度 | 看 `~/Library/Logs/om/om-core-fetch.log`（macOS）里 fetcher 的 stderr。 |
| 嵌入模型下载超时（国内常见） | 插件配置里填 `hf_endpoint = https://hf-mirror.com`，重启 Claude Code。 |
| binary ready 但 `sidecar unreachable` | supervisor 可能正在 respawn — 等 5 秒再看；持续不通就重启 Claude Code。 |
| MCP 工具返回 `OM_CORE_BIN_DOWNLOADING` / `EMBEDDING_MODEL_WARMING_UP` | 首次启动还在跑 — 等 `/kogcat:status` 四行全绿。 |

---

## License

MIT — 见 [LICENSE](./LICENSE)。
