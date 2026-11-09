# 新会话接力单（2026-11-01）

> 用法：新会话第一句话就说「读 coin11/docs/NEXT-SESSION-接力.md 并照它执行」。

## 0. 先验证子智能体白名单（约 30 秒，别跳过）

在程序里调这两条：

```
list_subagent_models({})
list_subagent_models({ provider: "bailian" })
```

**通过判据**：第一条要列出 `bailian`（不再只有 `deepseek-account`）；
第二条要返回 `deepseek-v4.1-flash-1017` / `deepseek-v4-pro` / `deepseek-v4-flash-0731` / `qwen-plus`。

**仍然只有 deepseek-account 怎么办**：说明生效值来自 Host 侧的内存覆盖。
按 `coin11/docs/DSH-子智能体路由-修复记录.md` 的「第二步」，
把 `~/.dsh/profiles/desktop/cordis.patch.yml` 里
`subagent-model-selection-settings` 的 `enabled` 改成 `false`（或整条删掉），
重启 Host 再开新会话——那样不会记录白名单，子会话一律继承父路由（已验证 cost 0）。

参考背景：旧会话（2026-11-01 13:24 之前创建）永远验不出来，因为白名单是
**会话级记录一次**（`@deepseek-ai/dsh-tool-subagent/lib/index.js` 的
`recordSubagentModelSelection`，`if (policy !== undefined) return;`），
会话恢复时回放旧快照。只有**新建会话**才会按新配置重新记录。

## 1. 钱的红线（最高优先级）

- 父会话在 `bailian`（阿里云百炼，cost 0）→ 子会话**只能**用 `bailian/*`。
- **禁止** `deepseek-account/*`：那是用户账户里的真金白银。已经因为疏忽烧掉 ¥1.13。
- 最稳妥写法：**不传 `provider`/`model`**，让子会话继承父路由。已实测：bailian 668→678 通，cost 仍 0。
- 每次派活后顺手查账：`~/.dsh/dsh-usage/usage-ledger.json` 当天 `deepseek-account` 的 calls 不许涨。
- 全局 `~/.dsh/AGENTS.md` 也写了同一套规则。

## 2. 已经定下的决策（不要再问用户第二遍）

1. **两台手机都要完成淘金币 + 赛车能量任务**，后期还要接其它平台签到（架构要能加任务，但现在不实现）。
   - 手机1 = `192.168.1.8`（vivo V2505A，淘宝 10.68.0，淘金币页是**原生控件**，`(n/m)` 计数可直接读）
   - 手机2 = `192.168.1.4`（iQOO 11 V2243A，淘宝 10.68.10，淘金币页是 **UC WebView**，只能截图+OCR+坐标）
2. **成功判据 = 行内 `(n/m)` 计数推进或整行消失**；**不做余额旁证**（用户明确否掉）。
   - 推论（已与用户确认）：**没有 `(n/m)` 计数的任务 = 无判据 = 跳过并记录**，绝不许用"点了"冒充成功。
   - 坑：`(4/5)→(0/5)` 是展开换批次的**新实例**，不是失败也不是完成。
3. **淘金币两条线都稳了才碰赛车**；每改一版**先单任务跑**，再合流。
4. 连续成功 `N` 的定义：**连续 3 次**，中途任何一次失败清零重来。

## 3. 复盘产出（已核实，别再重挖）

`coin11/docs/` 下三份，共 415 行，每条都带证据：

- `RETRO-coin.md` —— 淘金币线：已证伪 18 / 已验证 10 / 时序不稳 12 / 跳走分支 8 / 未定论 7
- `RETRO-racing.md` —— 赛车线：已证伪 7 / 已验证 8 / 时序不稳 7 / 跳走 6 / 未定论 8
- `RETRO-code.md` —— 代码现状：可直接复用 6 类 / 必须重写 6 类 / 建议归档 6 类

三条最重要的：

1. 「点了就算过」坐实：`2025淘宝双11.py:778` 的 `finish_count += 1` 在 `d.click()` **之前**执行。
2. 手机2 已有正确的三层判据范本（计数推进→余额旁证→拉黑，`phone2/README_run3.md:33-38`，已拉黑 11 个）。
3. 赛车唯一被验证过的产出信号是「淘金币余额上涨 / 升级已得金额上涨」，
   而代码现在只判「能量没下降」（`utils/racing.py:394-395`）。

## 4. 下一步（唯一要做的一件事）

**只做手机1 的淘金币，只做一个任务，做到连续成功 3 次。**

- 新建 `coin11/tasks/`：`device.py`（DeviceProfile，所有 adb/u2 调用必须显式带 serial）、
  `base.py`（任务单元协议：detect / execute / verify → `VerifyResult{ok, evidence}`）、
  `coin_p1.py`（本批只实现一个任务）。
- 选任务按证据选：面板首屏可见 **且** 行文本里有 `(n/m)` 计数。
- 等待条件一律 wait-for-condition + 明确超时，**不许固定 sleep 当判据**；
  每次点击前重新 dump 取坐标。
- 导航链（已验证 4 次以上）：`am start` Welcome → 底部「我的淘宝」tab →
  「领淘金币」（点击坐标约 (452,1854)）→「去赚金币」→ 任务列表。
  **首页 feed 上没有入口，别在首页试选择器**（已证伪 1）。
- **只许新增文件**；`2025淘宝双11.py`、`utils/*`、`conf/*`、`phone2/*` 一行都不许改；
  不许 git add/commit。只碰 `192.168.1.8`，全程不许碰 `192.168.1.4`。

## 5. 验收（做完拿这五条对）

1. `python -m tasks.verify_coin_p1 --serial 192.168.1.8` 能独立跑，输出 PASS/FAIL + evidence。
2. 连续 3 次 PASS，`coin11/logs/verify/` 下有 3 个日志，每份能看到改动前后的 `(n/m)` 原始文本。
3. `coin11/docs/TASK-coin-p1.md` 记录：命中的真实选择器、真实坐标、每步等待条件+超时值、失败判定路径。
4. `git status --porcelain` 只有 `??` 新增，没有被跟踪文件被改。
5. 「无判据任务清单」已列（本机 dump 里读不到 `(n/m)` 的任务名）。
