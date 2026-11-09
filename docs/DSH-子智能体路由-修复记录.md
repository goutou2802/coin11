# DSH 子智能体路由修复记录（2026-11-01）

## 症状
派子智能体时，账单落到 `deepseek-account`（收费账户），而主对话走 `bailian`（cost 0）。
用户明确要求：子智能体也不许碰 deepseek-account。

## 证据（`~/.dsh/dsh-usage/usage-ledger.json`，2026-11-01）
| 路由 | 调用 | 花费 |
|---|---|---|
| bailian/deepseek-v4.1-flash-1017 | 668 → 678 | 0 |
| deepseek-account/deepseek-v4-pro | 124 | 1.1256 |

124 通 / ¥1.13 全部来自当天派出的子智能体。

## 根因（两段代码）
1. `@deepseek-ai/dsh-tool-subagent/lib/index.js`
   ```js
   function recordSubagentModelSelection(projections, session, allowedModels) {
     if (subagentModelSelectionPolicy(projections, session) !== undefined) return; // 每会话只写一次
     session.append("subagent/model-selection-policy", { allowedModels });
   }
   ```
   可路由清单在每个会话开头钉死一条事件，之后会话内无法更改。
2. 同一文件里的放行校验：
   ```js
   if (policy === void 0 || !hasDelegationModelRequest(request)) return;  // 不显式指定路由 → 直接放行
   ... throw new Error(`child LLM route "provider/model" is not allowed for this Session`);
   ```
   只有**显式传 provider/model** 时才查白名单；不传则子智能体继承父路由（bailian，cost 0）。

实测验证：一次不带路由的子智能体调用后，bailian 668→678（cost 仍 0），deepseek-account 124 未变。

## 已做的修改
文件：`~/.dsh/profiles/desktop/cordis.patch.yml`
备份：`cordis.patch.yml.bak-20261101-before-bailian-fix`
改动：`subagent-model-selection-settings.config.allowedModels` 删除两条 deepseek-account 路由，
只保留 bailian 两条。改后用 js-yaml 校验：PARSE_OK，42 条目，allowlist 仅 bailian。

## 重启后如何验证（一条命令）
在新会话里：
```
list_subagent_models({})          → 应列出 bailian（两个模型），不应出现 deepseek-account
list_subagent_models({provider:"bailian"}) → 应返回 deepseek-v4.1-flash-1017 / qwen-plus
```
再查账本确认：`~/.dsh/dsh-usage/usage-ledger.json` 当天 deepseek-account 的 calls 不再增长。

## 若重启后仍是 deepseek-account
说明生效值来自内存/Host 侧的设置覆盖（磁盘上找不到该存储），配置文件不是最终权威。
第二步：把该条目改成 `enabled: false`（或整条删除）——此时不会记录任何白名单，
子智能体一律继承父路由（bailian，实测 cost 0），这是已验证的零成本通道。

## 铁律
**任何情况下都不要显式给 subagent 传 provider/model**，除非先确认白名单里只有免费/指定路由。
不传 = 继承父路由 = 零成本，实测有效。
