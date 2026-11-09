# coin11/auto.py —— 一条命令跑完全程

「烧了这么多 token，还是不能做出一个脚本可以独立完成任务吗」 → 这个脚本就是答案：
**一条命令、无人干预、跑完出报告**。运行期不需要任何模型参与判断，全部逻辑在 Python 里跑完。

## 1. 一条命令

```
python coin11/auto.py --serial 192.168.1.4:5555 --what all --minutes 60
```

其它用法：

| 命令 | 作用 |
|---|---|
| `python coin11/auto.py --what race --minutes 30` | 只跑赛车面板 |
| `python coin11/auto.py --what coin --minutes 30` | 只跑淘金币面板 |
| `python coin11/auto.py --what scan --minutes 10` | **只扫描不出手**：把全表 + 每行判定/原因列出来，一次点击都不做 |
| `python coin11/auto.py --selftest` | 离线自测：不连手机，用已存 evidence 复算解析/分类/守卫 |

参数：`--serial`（设备）`--what`（all/coin/race/scan）`--minutes`（预算，到点自己收尾）
`--max-rounds`（每个面板的轮数保险丝，默认 200）`--dwell`（强制浏览秒数，0=按行上的「浏览N秒」）
`--out`（证据目录）`--quiet`、`--selftest`。**没有任何交互提问**。

依赖只有四个 + 标准库：`adbutils`（adb）、`rapidocr_onnxruntime`（OCR）、`PIL`、`numpy`。
**没有 import coin11/v2/ 里的任何东西**（需要的逻辑在本文件里重写了一遍）。

## 2. 它做了什么

1. **前置**：读设备时间、确认 serial（读不到时间就直接停）、`input keyevent 26` 唤醒屏幕
   （224/82 只改状态不点亮，所以只认 26），必要时 `wm dismiss-keyguard`。
2. **启动淘宝**：`am start -n com.taobao.taobao/com.taobao.tao.welcome.Welcome`（不 force-stop）。
3. **面板导航**（每轮只做一个动作，做完重新看屏，认不出就 `am start` 拉回）：
   - 淘金币：我的淘宝 tab → 领淘金币 → 去赚金币 → 今日速赚面板 →（收起态点「展开」≈(1253,2491)）
   - 赛车：去赚能量（≈(1298,1322)）→ 赛车页「做任务赚能量」→ 做任务赚能量面板
4. **全表扫描**：展开后**一次性向下滚到底**（回滚到顶会自动收回，所以全程零上滚），
   逐屏 OCR 汇总成完整行表，画面两屏一致即判到底；存 `scans/*.json`。
5. **行分类**：以行尾动作（去完成/去逛逛/逛一逛/去搜索/浏览芯片 +50）为锚点切行，
   「可做」= 浏览 / 搜一搜 / 授权支付宝浏览；其余全部跳过，并**逐行写出具体原因**
   （黑名单词、外部 App、领取类按钮、额度已满、答题、支付宝未授权……）。
6. **执行循环**：现拍现定位（不缓存坐标）→ R1 守卫校验按钮 x → R2 校验面板在前台 →
   点击 → 按落地包名分派（淘宝连滑 N+1 秒 / 搜索页用热词 chip 提交并自证 / 支付宝只滑不点）→
   BACK 循环回面板（最多 4 次，不行才 am start）→ 两帧复核（计数差 / 面板进度差 / 金币总数差 / 行消失）。
7. **证据**：每轮 `rounds/round-NNN/` 六件套 + 过程帧；`ledger.jsonl` 一行一轮。
8. **报告**：`REPORT.md` — 做了多少轮/成功多少/每行 before→after/剩余不可做行及原因/耗时/硬规则/何时停下等人。

## 3. 硬规则（代码里是常量 + 注释，参数关不掉）

| # | 规则 | 代码位置 |
|---|---|---|
| R1 | 按钮 x 必须 ∈ [1245,1270]（实测 1252~1259）；越界**不点**并整个 run 停机（曾算出 1078 点进别的页） | `BTN_X_MIN/BTN_X_MAX` + `guard_button_x()` |
| R2 | 点击前必须证明面板在前台（面板标题 + ≥3 行），否则重进面板，仍不行就落盘不点 | `panel_ok()` |
| R3 | 支付宝类点「去完成/奖励芯片」是直跳、无确认弹窗；进去**只连滑、任何按钮不点**，抽检到弹窗词立即 BACK | `alipay_dwell()`、`ALIPAY_OK`、`ALIPAY_POPUP_WORDS` |
| R4 | 搜一搜只用「搜索发现/历史搜索」热词 chip；页面标题「搜索有福利」绝不当 chip；提交后自证进结果页 | `chip_candidates()`、`search_submit()`、`BAD_CHIP` |
| R5 | 不点 领取/立即领取/授权/开通/同意/支付/转账/去玩/邀请/助力/广告/下单/买/折扣/签到/免费拿/答题；永不点「始终打开」 | `BLACK_WORDS`、`TAP_LABELS`、`DIALOG_TEXT` |
| R6 | 不 force-stop、不安装、不改系统设置、不 git；拉不回用 am start | `Device.am_start_taobao()` |
| R7 | 长列表只能展开后一次性向下扫到底；回顶部靠「BACK 关掉面板再重进」 | `full_scan()`、`reopen_panel()` |
| R8 | 循环体 try/except，异常也落 verdict（证据链不能断） | `one_round()` 的 `except Exception` |
| R9 | 只认 keyevent 26 唤醒（224/82 只改状态不亮屏） | `Device.wake()` |

## 4. 什么情况下它会主动停下等人

1. **R1 按钮 x 越界** → 本轮落 `guard-violation`，整个 run 立即结束（定位链已不可信）。
2. **系统安装/权限/跳转确认框**（包名、Activity 或整句文案命中）→ 不点、不按 back、不尝试关闭，停机留证。
3. **屏幕唤不醒**（`keyevent 26` 三次仍非 Awake）或读不到设备时间（serial 不在/离线）。
4. **面板找不回**：BACK 4 次 + `am start` 拉回 + 重新导航全部失败。
5. **支付宝内出现弹窗** → 立即 BACK 退出、该行记未完成；同一行连续两轮无进展就放弃该行。
6. **遇到未安装 App 的行** → 只记「外部 App / 未装 App」，绝不安装。
7. 其余都是自己收尾：可做行做完、连续两轮全表都没有可做行、或 `--minutes` 到点。

## 5. 预期输出样例

stdout（节选）：

```
02:31:05 === coin11/auto.py 启动 ===
02:31:05 serial=192.168.1.4:5555 what=all minutes=60 out=...\evidence\2026-11-02-auto
02:31:09 [pre] serial=192.168.1.4:5555 设备时间=2026-11-02_02:31:09
02:31:09 [pre] 型号= 分辨率=1440x3200
02:31:09 [pre] 前台=com.taobao.taobao/com.taobao.themis.container.app.TMSActivity
02:31:10 [pre] 屏幕已唤醒
02:31:14 [nav] coin -> tap 领淘金币
02:31:19 [nav] coin -> tap 去赚金币
02:31:24 [expand] 点「展开」(1253,2492)，展开前行数=6
02:31:29 [scan] coin 第 5 屏与上一屏一致 → 到底（共 5 屏）
02:31:30 [scan] coin 全表 5 屏 → 21 行（可做 8）：coin_full_023130.json
02:31:31 [round-001] coin ok=True status=advanced | row '发现精选好物(0/4)' 0/4 -> 1/4 ...
02:32:12 [round-002] coin ok=True status=advanced | row '逛清单，每15秒30金币(0/2)' 0/2 -> 1/2 ...
...
02:44:02 [round-031] race ok=True status=advanced | row '好物随心逛(7/10)' 7/10 -> 8/10 ...
02:44:30 [report] ...\evidence\2026-11-02-auto\REPORT.md

=== 收尾 ===
轮数 31 / 成功 26 / 耗时 13分25秒 / 停止原因：预算用完（剩 12s）
证据目录：...\evidence\2026-11-02-auto
报告：...\evidence\2026-11-02-auto\REPORT.md
```

证据目录：

```
evidence/<日期>-auto/
├── REPORT.md
├── run.log
├── ledger.jsonl            ← 一行一轮
├── rounds/round-001..NNN/  ← 六件套 + 过程帧
│   ├── 01_before.png  02_before_ocr.json  03_action.json
│   ├── 04_after.png   05_after_ocr.json   06_verdict.json
│   └── 07_postcheck.jpg  07b_postcheck2.jpg（+ 各自 OCR json）
└── scans/*.json           ← 全表扫描（含每屏 OCR 盒）
```

## 6. 离线自测（`--selftest`，不连手机，实测 12/12 通过）

自测用**今晚已经落盘的真实证据**复算，不是假想数据：
`evidence/2026-11-02/scans/*.json`（赛车 4 屏 / 淘金币展开 5 屏 / 芯片版 6 屏）、
`evidence/2026-11-02/round-103,round-130/02_before_ocr.json`。

```
$ python coin11/auto.py --selftest
=== coin11/auto.py 离线自测（无需连手机）===
[PASS] T1 赛车面板解析 + R1 守卫        4 屏 / 首屏 8 行，全部按钮 x ∈ [1245,1270]
[PASS] T2 可做行判定                    好物随心逛=browse/15s · 搜一搜=search · 去省钱卡领红包=browse/5s
[PASS] T3 跳过行原因                    每拉1人->black · 去玩趣味小游戏->black · 去快手打卡领手机->black ...
[PASS] T4 淘金币长表分类                支付宝农场(按钮)=alipay · 支付宝农场(+50 芯片)=alipay · 蚂蚁庄园=跳过 · 头条=ext-app
[PASS] T5 R1 守卫边界                   [1245,1270] 放行；1244/1271/1078 全部抛 GuardError
[PASS] T6 R2 面板判定                   race=8 coin=3 无关屏=否
[PASS] T7 进度/金币解析                 47/50→47 · 粘连金币帧→26001 · round-130 真实帧→25951
[PASS] T8 R4 热词 chip                  选中 空气净化器（排除标题「搜索有福利」）
[PASS] T9 全表合并                      4 屏 → 13 行，无重复 key
[PASS] T10 证据六件套                   round-001 六件套齐 + ledger 1 行
[PASS] T11 源码卫生                     import 白名单干净（无 v2 / 无模型 SDK）
[PASS] T12 端到端一轮（假设备）          点 (1253,2219) · 2/10→3/10 · 六件套齐 · verdict=advanced

结果：12/12 通过
```

另外用两个「状态机假设备」把整条链子跑通过（不连手机）：
- 赛车面板：导航（淘金币页→去赚能量→做任务赚能量）→ 全表 2 屏 → 2 轮 advanced → REPORT.md（含 before/after 表、剩余不可做行原因、硬规则表）；
- 淘金币面板：我的淘宝→领淘金币→去赚金币→展开→扫描 → **支付宝「只滑」通道**（金币 25861→25911，+50 记成 advanced，支付宝内零点击）
  → **搜一搜热词 chip**（选中「空气净化器」，自证结果页，行 0/5→1/5）。

## 7. 已知边界（诚实说明）

- 本轮**没有连手机实跑**（手机2 被另一个执行者占用）：所有结论都来自真实证据帧的离线复算 + 两套假设备全链路模拟。
  真机上第一次跑建议先用 `--what scan --minutes 10` 看一眼全表与判定，再跑 `--what all`。
- 坐标常量是兜底：导航优先按 OCR 文字点，认不出才用 `NAV_*` 兜底坐标（展开 (1253,2491)、去赚能量 (1298,1322)、我的淘宝 (1207,3073)）。
- 行识别依赖 OCR：分辨率/字体变化可能让某行读不到计数，这时照样会执行，只是判定只能靠「进度差/金币差/行消失」。
- `--minutes` 到点前留下的剩余可做行，会在 REPORT.md 第 3 节列清原因（预算用完 vs 规则跳过）。
