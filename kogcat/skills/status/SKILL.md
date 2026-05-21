---
name: status
description: Show om-core local status — binary fetcher, embedding model warmup, sidecar service, KB binding. Use when first-install seems stuck, or to diagnose "下载/启动" issues.
---

# status — om-core 本地状态诊断

只读诊断,**不**修改任何状态。

运行:

```bash
python3 scripts/status.py
```

`scripts/status.py` 相对本 SKILL.md 目录解析。它从自身位置解析出 plugin root 后转跑 om-core 状态脚本 —— 不依赖任何 host 注入的环境变量,CC / Codex 通用。

输出涵盖:
- binary fetcher 进度(version / 已下载 / 状态)
- 嵌入模型状态(向 sidecar 查 `/v1/embedding/status`:下载中 / 已就绪 / 失败)
- sidecar 服务状态(pid / uptime / `/healthz`)
- 当前 KB 绑定(kb_root / schema 版本)

参数:`--json` 输出机器可读 JSON(给排障 / 支持流程用):

```bash
python3 scripts/status.py --json
```
