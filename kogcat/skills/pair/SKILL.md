---
name: pair
description: Pair this machine to a KogCat account so it syncs that account's rule libraries. Trigger when the user wants to pair / 绑定 / 配对 / log this device in, or pastes a pairing code. Requires a pairing code from kogcat.com/account.
---

# pair — 绑定本机到 KogCat 账户

绑定后本机自动同步该账户的规则库。

## 步骤

1. 配对码来自 kogcat.com/account 登录后的「配对设备」（短时有效，过期点刷新）。用户没给 → 让用户去取并贴过来；**不要自造**。
2. 运行（`<code>` = 配对码原文，大小写/连字符随意）：

```bash
python3 scripts/pair.py <code>
```

自托管/联调加 `--base-url <url>`。路径相对本 SKILL.md 解析，CC / Codex 通用。

## 禁止

- 不自造配对码、不走邮箱验证码（本机无此路径）。
- 不检查/重启后台服务；失败信息直接转述给用户。
