# 任意 App 自动操作范式 · 架构（v1，2026-11-01）

> 用途：新会话读这一份就能接手。目标不是"再写几个脚本"，而是把**两台手机 × 淘金币/赛车能量**的实测经验沉淀成**可复用的自动化范式**。

## 0. 一句话架构

**判据优先（verification-first）**：任何一次点击，都必须先知道"点完之后拿什么证据判定成功"；拿不到证据的任务，一律跳过并记录，绝不允许用"点了"冒充成功。

## 1. 分层（自下而上，上层不依赖下层实现细节）

```
            ┌─────────────────────────────────────────┐
  runner/   │ 单任务试跑 → 连续 N 次(默认3) → 写日志   │
            ├─────────────────────────────────────────┤
  tasks/    │ 任务单元 detect/execute/verify → VerifyResult │
            ├─────────────────────────────────────────┤
  knowledge/│ 每台机 × 每个 App 的实测知识（坐标/选择器/坑）│
            ├─────────────────────────────────────────┤
  percept/  │ 感知层：Dump感知 / OCR感知 → 统一 Scene    │
            ├─────────────────────────────────────────┤
  actions/  │ 动作层：click/swipe/back/launch + 后置条件 │
            ├─────────────────────────────────────────┤
  core/     │ 设备层：Device(explicit serial) / wait_for │
            └─────────────────────────────────────────┘
```

## 2. 目标目录

```
coin11/
  v2/                      # 影子重建区：新架构全部在这里长出来
    core/device.py         # 已有雏形：tasks/device.py 直接搬进来
    core/base.py           # 已有雏形：tasks/base.py
    percept/dump.py        # 原生控件感知（XML → Scene）
    percept/ocr.py         # 截图 + OCR → Scene（给 UC WebView 用）
    percept/scene.py       # 统一数据结构：Element{text,bounds,center,source,conf}
    actions/act.py         # click(Element|coord) / swipe / back / launch
    tasks/coin_p1.py       # 手机1 淘金币（先只做一个）
    tasks/racing.py        # 赛车能量
    knowledge/
      phone1_taobao.yaml   # 192.168.1.8 实测：导航链、坐标、选择器、坑
      phone2_taobao.yaml   # 192.168.1.4 实测：同上，OCR 路线
      _pitfalls.md         # 跨机通用坑（见第 4 节）
    runner/run.py          # 跑一个任务 N 次，统计连续成功率
  logs/verify/YYYYMMDD-HHMMSS/   # 每次运行：(n/m) 前后原文 + 截图 + dump
  docs/                    # 人类可读：架构、任务说明、复盘
  _frozen/                 # 旧代码原样冻结（见第 7 节，一行不改）
```

## 3. 核心协议（签名固定，子智能体不得擅改）

```python
# core/base.py
@dataclass
class VerifyResult:
    ok: bool
    evidence: dict          # 必须能原样打印：读到的 (n/m)、界面文本、bounds

class BaseTask(abc.ABC):
    name: str
    def detect(self, device) -> bool
    def execute(self, device) -> None      # 无判据时抛 TaskSkipped，不许静默返回
    def verify(self, device) -> VerifyResult
```

```python
# percept/scene.py —— 让"原生"和"OCR"两条路对上层长得一样
@dataclass
class Element:
    text: str; bounds: tuple; center: tuple
    source: str      # "dump" | "ocr"
    conf: float = 1.0
@dataclass
class Scene:
    elements: list[Element]; raw: str; shot: str | None
    def find(text, exact=False) -> Element | None
    def smallest(text) -> Element | None    # 避开铺满全屏的外层容器
```

## 4. 判据体系（这是整个范式的核心，比代码重要）

| 判据类型 | 成立条件 | 适用 |
|---|---|---|
| `counter_progress` | 行内 `(n/m)` 的 n 增大 | 有计数的任务行 |
| `row_disappear` | 该行的识别文本在下一屏消失 | 完成后行被移除 |
| `state_text` | 明确的终态文案（如"已完成"） | 少数任务 |

**硬规则**
1. 没有上述任一判据 = 无判据 = `VerifyResult.skipped` 并记录任务名，**永远不算成功**。
2. `(4/5) → (0/5)` 是**展开换批次的新实例**，既不是失败也不是完成 —— 必须识别并重新取基线。
3. 等待一律 `wait_for(predicate, timeout)`，**禁止固定 sleep 当判据**；超时必须抛出可解释异常。
4. **每次点击前重新 dump/截图取坐标**，坐标会漂移，禁止缓存坐标跨步骤复用。
5. 余额类旁证**不做**（用户已明确否掉）。

## 5. 两台手机的差异化（已知实测事实）

| | 手机1 `192.168.1.8` | 手机2 `192.168.1.4` |
|---|---|---|
| 机型 | vivo V2505A | iQOO 11 V2243A |
| 淘宝 | 10.68.0 | 10.68.10 |
| 淘金币页 | **原生控件**，`dump_hierarchy` 可读 `(n/m)` | **UC WebView**，只能截图 + OCR + 坐标 |
| 感知路线 | Tier A：dump | Tier B：OCR |
| 已验证坑 | `d.info` 抛 `ApplicationSharedMemory` 崩溃，禁用；`dump_hierarchy` 偶发 HTTP 失败，轮询要 `ignore_errors=True` | 已有三层判据范本 `phone2/README_run3.md:33-38`（计数推进→余额旁证→拉黑），已拉黑 11 个 |

**Tier B（OCR）额外要求**
- 坐标必须来自**锚点标定**（同一元素在截图里定位），不能硬编码一次就长期复用。
- OCR 结果的 `conf` 低于阈值 → 当作"没读到"，走 skip，不许猜。
- 每次判定都要存截图，日志里能把"看到的"和"判的"对上。

## 6. 环境事实（已实测，别再挖）

- Python 3.11.11，命令 `python`；uiautomator2 已装。
- adb **不在 PATH**：`D:\Users\28024\miniconda3\Lib\site-packages\adbutils\binaries\adb.exe`，代码用 `adbutils.adb_path()` 解析。
- 屏幕 1440×3168。
- 导航链（手机1，已验证 4 次以上）：
  `am start` Welcome → 底部「我的淘宝」tab →「领淘金币」≈(452,1854) →「去赚金币」→ 任务列表。
  **首页 feed 上没有入口，别在首页试选择器**（已证伪）。
- 手机1 首屏任务列表实测：只有 `(0/1)` / `(1/1)` 两种计数；**滚动后 `(n/m)` 归零**。

## 7. 现有 86 个 py 的处置（新会话按此分类，不要一个个读）

| 类别 | 文件 | 处置 |
|---|---|---|
| **冻结保命** | `2025淘宝双11.py`(1038) `utils/*`(8个) `conf/*` | 一行不改。它是现在唯一能跑的东西，重构期间必须保持可用 |
| **抽知识** | `utils/racing.py`(345) `utils/quiz*.py` `phone2/driver_p2.py`(379) `phone2/p2.py` `tap4.py` `search4.py` `game4.py` `check4.py` `do_energy.py` `run_energy.py` | 只读，把其中的**坐标/选择器/坑**抄进 `knowledge/*.yaml`，代码本身丢弃 |
| **归档** | `phone2/` 的 `bench1-4 / bench_ocr / bench_compare / probe* / test* / ls2 / ls3 / qd2 / fast_one / hie` 等约 30 个；根目录 `dbg/look/fin/fixdlg/final_check/scan*/probe*/try_stay/test_final` | 移到 `_archive/`，不删（保留证据） |
| **复用** | `tasks/device.py` `tasks/base.py` | 搬进 `v2/core/`，这是今天已验证过的新地基 |

## 8. 迁移方式：影子重建 + 一次性切换（推荐）

1. 新架构全部长在 `v2/` 下，**旧文件冻结不动**。
2. 手机1 淘金币先在 `v2/` 打通，跑到**连续 3 次 PASS**。
3. 再搬赛车能量，同样连续 3 次 PASS。
4. 手机2 走 OCR 路线，复用同一套 `Scene` 接口，只换 percept 实现。
5. 两条线都稳了，才把 `_frozen/` 之外的东西一次性切换、清理。

**为什么不用就地重构**：旧代码是唯一能跑的东西，边改边跑等于同时动地基和动楼。

## 9. 红线（任何会话都必须遵守）

- 子智能体禁止走 `deepseek-account`（花用户真金白银，已因疏忽烧掉 ¥1.13）。当前会话级实测：不传 provider/model → 继承父路由 → bailian → cost 0。
- 只碰 `192.168.1.8` 时绝不碰 `192.168.1.4`，反之亦然；所有 adb/u2 调用必须显式带 serial。
- 扫描类清理只许移动/新建，删除前必须核对绝对路径。

---

## 10. 新会话开场就贴这一句

```
读 coin11/docs/ARCH-自动化范式.md 和 coin11/docs/evidence-coin-p1.md，
按第 8 节「影子重建」执行。已定决策（不要再问）：
1. 旧代码冻结不动，新架构全部长在 coin11/v2/ 下；
2. 第一优先 = 手机1 淘金币，只做一个任务，做到连续成功 3 次；
3. 判据只有三种：counter_progress / row_disappear / state_text，其余一律 skip 并记录；
4. 子智能体不许走 deepseek-account（花真钱），不传 provider/model 让它继承父路由。
自己实现交给子智能体，你负责拆解、复核、验收。
```

## 11. 为什么"昨晚看着做完了，今天还在做"（必读，别重蹈）

`2025淘宝双11.py:776-780` 现存代码：

```python
if task_name not in have_clicked:
    have_clicked.append(task_name)
    finish_count += 1        # 尚未点击就记账
d.click(_click_x + ..., _click_y + ...)
time.sleep(4)                # 固定睡眠，之后无任何复查
```

**动作链是通的（能导航、能定位、能点、能读计数），但成功判定是伪造的。**
"跑完了"只代表"点过了"，点击有没有生效、`(n/m)` 有没有推进，全程无人核对。
v2 要补的不是功能，是**可信度**：`verify()` 拿不到判据就只能返回 skipped/failed。

## 12. 手机1 首屏判据实况（已实测，别再假设是 (3/5)）

- 首屏任务列表 `(n/m)` 去重后只有两种：`(0/1)`、`(1/1)`（8 处匹配）。
- **向下滚动后 `(n/m)` 匹配数 = 0**；且连续 8 次滚动 dump 字节数完全一致（196876），
  说明页面根本没滚动 —— 属于"滚不动"分支，必须识别并降级，不能当成"任务都做完了"。
- 因此候选任务只能从**首屏、行内带 (0/1) 的行**里选。
