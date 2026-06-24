# KogCat

> AI 让答案更顺，KogCat 让判断更稳。

[English](./README.md) | [中文](./README.zh-CN.md) · **官网：** <https://www.kogcat.com>

给 Claude Code 与 Codex 用的本地优先 **判断校准层**。在你照着 AI 的回答动手之前，它把反例、边界、和你漏看的那一面摆到你面前——全部来自跑在你本机的知识库。不替换你的模型。不拖慢你。判断，依然由你来下。

---

## 看看区别

*"我每天读书 30 分钟，但什么都记不住。要不要记更详细的笔记？"*

**普通 AI** —— 试试康奈尔笔记法，标重点，再用 Anki 做间隔重复。

**KogCat** —— 记更多笔记，多半只会更糟。瓶颈不在记录，在检索。你的知识库里有一条你亲手标过高置信度的判断：*重读时"感觉懂了"的那一刻，恰恰最靠不住。* 试一次——读完一节，合上书，凭记忆写下来。再和你以为记住的，对一对。

---

## 它什么时候介入

为判断而生，不为检索。决策、取舍、对比、批判、策略——这些一旦答错就代价高昂的问题，它才出手。事实查询、定义、写代码、总结、翻译，它都安静待着。即便是判断题，也只在它看到了模型没看到的，才开口。

- **自动。** 在对话里问一个判断题。只有真有话值得说，它才浮出一条提示——你的原始回答，分毫不动。
- **主动。** `/kogcat:query <问题>` 让知识库先开口：一个结论、会改变结论的前提、一个下一步。

---

## 安装

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

**安装后请完全退出并重新打开 Claude Code（Codex 则重启）。** 首装下载只在新会话开始 —— 仅执行安装命令不会触发它。

下次会话启动时，KogCat 悄悄下载本地引擎（~40 MB）和嵌入模型（~90 MB）。一次，就好。良好网络下约 1 分钟。下载时照常工作——想看进度，对 KogCat 说一句 `查看 kogcat 的状态`，每一项核心依赖的就绪情况一目了然。

---

## 在其他 MCP 客户端里用

KogCat 的校准引擎是一个跑在本机的 sidecar；Claude Code / Codex 插件只是它的一个客户端。任何支持 MCP 的工具 —— Cursor、Cline、Zed、VS Code、Claude Desktop —— 都能通过一个独立的 stdio MCP server 用上同一个引擎。

在你客户端的 MCP 配置里加上（字段名因客户端略有差异，多数用 `mcpServers` 映射）：

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

需要 [uv](https://docs.astral.sh/uv/)，平台同下方（macOS Apple Silicon 或 Windows x86_64，同一个引擎）。首次运行时它下载引擎 + 嵌入模型并注册后台 sidecar —— 和插件一样的一次性准备，本机所有客户端共享。它把知识库工具（`search`、`node`、`edges`、`calibrate`、`calibrate_review`，以及 `memory_*` 系列）暴露给你的模型调用。

**相比插件少了什么。** 插件多了两个通用 MCP 客户端没有钩子可挂的便利：判断题上*自动*触发的校准，以及会话开始时注入上下文的记忆索引。用独立 server，你的模型能拿到同一个知识库，但要由模型自己决定调用这些工具（或你让它「用 kogcat」），而不是 hook 替你触发。你从不敲这些工具名（`search`、`calibrate_review`、`memory_*`）——它们是给模型调的。

---

## 隐私

- **你的知识库** 留在你的机器上。KogCat 只读你指给它的那个目录。
- **你的对话** 仍然发往你本就在用的 Claude 或 Codex。没有额外去向，没有第二个收件人。
- **校准** 在本地进程里完成。结果，从不外流。
- **引擎** 来自公共发布 channel，运行前先过一遍 sha256 校验。

不要账号。不要订阅。没有第三方替你保管知识。

---

## 命令

| 命令 | 干什么 |
|---|---|
| `/kogcat:query <问题>` | 知识库优先的回答：结论、前提、下一步。 |
| `/kogcat:status` | 只读本地状态。首次启动卡住时，找它。 |
| `/kogcat:memory-consolidate` | 整理已存的记忆——每条改动，由你确认。 |

自动校准，无需任何命令。

---

## 系统要求

- Claude Code 或 Codex
- macOS（Apple Silicon）或 Windows x86_64 —— Intel Mac 与 Linux 暂未支持
- `PATH` 上有 Python 3 —— macOS 已自带；Windows 需自行安装（勾选 *Add python.exe to PATH*）

---

## License

FSL-1.1-MIT —— 见 [LICENSE](./LICENSE)。发布满两年后转为 MIT。
