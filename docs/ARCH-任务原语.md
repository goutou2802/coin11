# 任务原语架构 · 形态优先（v2，2026-11-01 修订）

> 推翻 v1 的「按任务组织」（coin_p1 / racing）。正确切分是**按任务形态**：
> 浏览N秒 / 点击去逛 / 搜索回访 / 答题 / 领取 —— 这些在**淘金币↔赛车、手机1↔手机2** 之间是同一套动作。
> v1 文档保留，但其 §2 目录树以平台分文件的设计作废。

## 1. 一句话

**动作序列按「形态」复用，判据按「实例」挂载。**
同一段 `browse_seconds` 代码，在淘金币里判 `(n/m)` 推进，在赛车能量里判能量/余额上涨——
**动作通用，判据可插拔**。

## 2. 形态从副标题读，不从任务名读

实测依据：手机1 淘金币面板每行固定 5 件（icon / name / **sub** / reward / button），
`sub` 才是完成条件，`name` 只是文案。见 `docs/evidence-coin-p1.md` §3 行内结构。

| 形态 ID | 副标题特征 | 淘金币实例 | 赛车实例 | 动作序列 |
|---|---|---|---|---|
| `BROWSE_SECONDS` | `浏览N秒` | 养小羊免费赚话费 | 好物随心逛 | 点入口 → **持续滑动**满 N 秒 → 返回 |
| `VISIT_JUMP` | `点击去逛` / `去看` | 头条极速版刷视频、去淘宝特价版领红包 | 蚂蚁庄园/蚂蚁森林/闪购/抽最高1万/蚂蚁新村/双11寻宝 | 点 → 等落地(可能跨App) → 停留 → 返回 |
| `SEARCH_RETURN` | `从首页入口或搜索回访` | 从首页回访 | — | 回首页 → 搜索关键词 → 返回 |
| `QUIZ` | `参与答题` / 弹窗问答 | 淘金币趣味课堂 | 弹窗「送你能量 最高1000」 | 读题 → 选项 → 提交 |
| `CLAIM` | `立即领取` / `领取` | 每日来任务面板奖励 | 累计奖励「立即领取 +1000」 | 点领取 → 等状态变 |
| `NEVER` | `下单得` | 买限时折扣好物 ×2 | — | **永不执行**（花钱），记录并跳过 |

## 3. 形态里的「坑」是通用的（跨平台实测）

| 坑 | 证据 | 写进哪个原语 |
|---|---|---|
| 浏览类**静止白等**，必须持续滑动 | RETRO-racing 证伪3：好物随心逛静止 18s 计数不变 | `BROWSE_SECONDS.motion` |
| 跳转类**计数推进但能量不到账** | RETRO-racing 证伪1：6 个该类任务做完按钮仍 46 | `VISIT_JUMP` 的判据策略（平台相关） |
| 弹窗/浮层**不进无障碍树**，dump 读不到 | RETRO-racing 证伪6：解锁新车轮弹窗 buttons=[] | 感知层降级到 OCR |
| `back` 关弹窗会**把页面带走** | RETRO-racing 证伪6：back 直接掉回首页 | 动作层 `dismiss` 策略 |
| 活动页闲置 1-2 分钟**自动漂走** | RETRO-racing 时序不稳2 | 每次动作前重新定位入口 |
| 坐标为**相对某页出现**，同页不同时刻 y 漂移 1000px | evidence-coin-p1 §2 STEP3 | 动作层禁止缓存坐标 |

## 4. 分层（改：原语层独立，不依赖平台）

```
tasks/ 实例   coin_p1(养小羊)  racing(好物随心逛)   ← 只是「原语 + 平台判据 + 入口坐标」的绑定
                    ↓ 组合
archetypes/   BROWSE_SECONDS  VISIT_JUMP  SEARCH_RETURN  QUIZ  CLAIM  NEVER
                    ↓ 只用
actions/      click / swipe_loop / back / dismiss / launch
                    ↓ 只用
percept/      Scene（dump 实现 | OCR 实现）    ← 手机1 用 dump，手机2 用 OCR
                    ↓ 只用
core/         Device(显式 serial) / wait_for(timeout)
```

**关键约束**：`archetypes/` 里**不许出现**任何 `(n/m)`、能量、余额字样——判据一律由调用方注入。

## 5. 原语契约

```python
@dataclass
class TaskSpec:
    shape: str              # BROWSE_SECONDS / VISIT_JUMP / ...
    entry: Locator          # 怎么点到入口（文本/bounds/OCR 锚点）
    sub_text: str           # 副标题原文，形态判定的依据
    dwell: float = 0        # 停留秒数（BROWSE 用 N）
    motion: str = "none"    # "swipe_loop" | "none"
    verify: Callable        # 判据注入点，返回 VerifyResult

class Archetype(abc.ABC):
    shape: str
    def run(self, device, scene, spec: TaskSpec) -> VerifyResult
```

## 6. 判据仍然只有三种（不变）

`counter_progress` / `row_disappear` / `state_text` —— 没有就 `skipped` 并记录。
赛车线的「余额上涨」属于 `state_text` 的平台特例，只允许在赛车实例里用，不许污染原语层。

## 7. 手机2（OCR）怎么复用同一套

手机2 淘金币是 UC WebView，dump 读不到。但**只换 percept 实现**：

```python
scene = DumpScene(device)      # 手机1
scene = OcrScene(device, anchors)  # 手机2：截图 → OCR → Element{text,bounds,conf}
```

原语、动作、判据全部不改。OCR 的 `conf` 低于阈值 → 当作没读到 → `skipped`，不许猜。

## 8. 验收方式必须改（今天的现实）

**淘金币任务今天已耗尽**，真机连跑 3 次不可能今天完成。改为双轨：

- **A 轨（今天）**：`replay` —— 把 `logs/recon/*.xml` 喂给 `detect()/verify()`，
  证明判据逻辑正确；再对仍可做的行做**单次**真机验证。
- **B 轨（明天重置后）**：真机连跑 3 次，任何一次失败清零重来。

A 轨跑通才能证明「原语是对的」，B 轨只证明「今天网络/页面听话」。两者都要。

## 9. 注意：今天首屏其实还有 4 行是 (0/1)

`docs/evidence-coin-p1.md` §4（采集于 2026-11-01 14:07）显示：

| 行 | 任务 | 计数 | 副标题 | 可做 |
|---|---|---|---|---|
| 3 | 头条极速版刷视频 | (0/1) | 点击去逛 | `VISIT_JUMP` |
| 5 | 从首页回访 | (0/1) | 从首页入口或搜索回访 | `SEARCH_RETURN` |
| 7 | 去淘宝特价版领红包 | (0/1) | 点击去逛 | `VISIT_JUMP` |
| 8 | 养小羊免费赚话费 | (0/1) | 浏览5秒 | `BROWSE_SECONDS` |

只有 1、4 两行是 `(1/1)` 已完成；2、6 是「下单得」永久跳过。
**若昨晚确实做过这 4 个却仍是 (0/1)，那正好是「判据缺失」的直接后果**——
点击未生效、或生效未登记，旧代码两种都不区分。这是 A 轨要先弄清楚的第一件事。
