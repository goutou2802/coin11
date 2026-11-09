#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coin11/auto.py — 淘宝「淘金币 / 赛车能量」任务面板：一条命令跑完全程。

一条命令（无交互、无人值守）：
    python coin11/auto.py --serial 192.168.1.4:5555 --what all --minutes 60

依赖只有四个 + 标准库，全部本地离线跑，**运行期不需要任何模型参与判断**：
    adbutils(adb) · rapidocr_onnxruntime(OCR) · PIL · numpy
刻意不 import coin11/v2/ 下的任何东西（那套和这里混用会互相拖坏），
需要的逻辑全部在本文件内重新实现了一遍。

产物（默认 evidence/<YYYY-MM-DD>-auto/，与别人的 evidence/<date>/ 分开，不抢写）：
    rounds/round-NNN/01_before.png        点击前截图
    rounds/round-NNN/02_before_ocr.json   点击前 OCR（含前台 focus）
    rounds/round-NNN/03_action.json       点击坐标 + 意图 + 设备时间
    rounds/round-NNN/04_after.png         点击后截图（快速路径：唯一一帧，只做像素指纹）
    rounds/round-NNN/05_after_ocr.json    点击后 OCR（含前台包名；快速路径不做 OCR，写空盒+原因）
    rounds/round-NNN/06_verdict.json      判定（ok / reason / 证据字段）
    rounds/round-NNN/07*_postcheck*.png/json      回到面板后的复核帧（1 帧；判不出结论才补 1 帧）
    ledger.jsonl                         一行一轮
    scans/*.json                         扫描存档（全表 full_scan / 一页一页做 paged）
    REPORT.md                            收尾报告      run.log  运行日志

=============================== 硬规则（关不掉）===============================
R1  按钮 x 必须落在 [1245,1270]（BTN_X_MIN/BTN_X_MAX）。实测 1252~1259。
    2026-11-01 曾算出 x=1078，「点」进了别的页面。越界 → GuardError：**不点**，
    并且整个 run 立刻停下（定位链已不可信，继续点就是在赌）。
R2  点击前必须证明任务面板在前台：当次截图里既有面板标题（今日速赚 / 做任务赚能量），
    又有 ≥3 行可识别行。缺一条就不点。
R3  支付宝类（点名只认「支付宝农场」「金豆夺宝」）：点「去完成 / 奖励芯片」是**直跳**，
    没有「仅打开一次」确认框（round-036 / 111 / 130 现证）。进去之后**只连滑，任何按钮
    都不点**；中途抽检到弹窗词（领取/授权/支付/签约…）立即 BACK 退出。
R4  搜一搜：只用「搜索发现 / 历史搜索」区块里的热词 chip 提交；页面标题「搜索有福利」
    绝不能当 chip（round-034 就是栽在它上面）。提交后要自证进了结果页再滑。
R5  不点 领取 / 立即领取 / 授权 / 开通 / 同意 / 支付 / 转账 / 去玩 / 邀请 / 助力 / 广告 /
    下单 / 买 / 折扣 / 签到 / 免费拿 / 答题；**永不点「始终打开」**。
R6  不安装任何东西、不改系统设置、不 git。拉回淘宝优先 am start 起欢迎页；
    force-stop 重启只在恢复阶梯里用（2026-11-02 用户授权：任务识别/完成识别两次都不行
    才重启，每轮最多 1 次）。
R7  长列表回滚到顶会自动收回 → 只能「展开后向下扫」，全程零上滚；
    面板驱动按「一页一页做」（drive_panel）：做完当前这一页才向下滚一屏，连续两屏内容
    一致 = 到底；要回到列表顶部只能 BACK 关面板再重新进（本文件的 reopen_panel）。
R8  循环体必须 try/except 包裹，异常也要落盘（证据链断掉等于白跑）。
R9  屏幕唤醒只认 keyevent 26；224/82 只改 wakefulness 状态不点亮屏幕
    （用它会在黑屏上截图，OCR 全是空）。
R10 弹窗分级处置（dismiss_popup_if_any）只挂在**非导航**路径（点后落地 / 复核）；
    2026-11-02 19:17 用户明确「弹窗先不考虑」，导航路径不再调它。分级仍是：
    关闭类文字 > 卡片几何候选点
    （底部中央下方 40~120px / 右上角内侧 60px / 卡片下方无字圆图标）> BACK 兜底
    （日志标 fallback=back，最多 2 次）。绝不点 去使用/立即领取/领取/开通/授权/支付/
    同意/去逛逛/抢。每次处置落盘：截图 + OCR 文本 + 用的哪级策略 + 结果。
R11 导航（2026-11-02 19:17 用户现场要求）：进面板走**写死坐标序列**，动作之间
    0 次截图 / 0 次 OCR，每条序列末尾 1 次 OCR 判 panel_ok（每个面板最多 2 次）；
    导航路径不查规则库、不做弹窗几何、不逐动作诊断。流程被打乱只在「任务识别 /
    完成识别」两处恢复：BACK×3（每次 BACK 后 1 次 OCR）→ force-stop 重开 + 写死序列
    重进（每轮最多 1 次）→ 仍不行就落盘待教学并停手（NeedTeaching / 退出码 3）。
    旧的「每轮先截图+OCR+弹窗几何+逐动作诊断」整段保留在 nav_until_panel_diagnostic()，
    **导航主路径不许调它**。
R12 盲点必须先证明「我在哪一屏」（2026-11-02 19:49 真机事故：am start 后固定 sleep(3)
    就盲点，冷启动还在欢迎页，几步后被点到 TTDetailActivity 商品下单页，只差一点就
    点「立即支付」）。规则：① 打开淘宝后不许固定 sleep，轮询 dumpsys focus 等到
    TBMainActivity / TMSActivity 才允许动手（最多 20s）；② 每一发盲点前读一次 focus，
    activity 不在该步预期清单（首页/我的淘宝页 = TBMainActivity，淘金币页 = TMSActivity）
    就绝不点；③ 焦点变成商品/下单/支付宝类（TTDetailActivity / NewDetailActivity /
    IrpActivity / *detail* / *order* / *alipay* …）立刻 BACK 退干净，期间零点击，
    退不掉交给恢复阶梯；④ 点完该到的页面 6 秒内没到，不许盲点下一发。
    读 focus 是 dumpsys（约 0.2s），不占 OCR 预算。
R13 进面板的第一步永远是「先重启淘宝」（2026-11-02 用户在事故后追加，优先级最高）：
    用户原话「不管怎么样，都是直接重启第一步，那不就 不用那个 OCR 了嘛」。
    顺序：am force-stop com.taobao.taobao -> sleep(1.0) -> am start .../welcome.Welcome ->
    轮询 focus 等 TBMainActivity（开屏/活动页连续 3 次轮询才 BACK 1 次、最多 2 次；
    20s 还没到首页 -> 一次点击都不发，直接走恢复阶梯）。
    只在"真的要进面板"时重启：被「今天已完成」跳过的面板不重启；多面板只有第一个面板冷启动
    （第二个已在淘金币页，直接点胶囊）；--what scripts / --script 单跑自定义脚本
    **绝不** force-stop / am start（用户选 3A：脚本只管当前屏幕）。
R14 写死坐标**按屏分辨率取表**（2026-11-03 第五批）：DEV_COORDS 的键是 "dev.w x dev.h"
    （手机1 V2505A 1440x3168 / 手机2 V2243A 1440x3200 各一组实测坐标）。
    未登记的分辨率**不许瞎猜**：一次点击都不发，日志写明「未登记的分辨率 WxH，写死导航没有
    坐标表」，直接走恢复阶梯 / 待教学。两台机器的首页版式不同（「领淘金币」手机1 在
    (159,938)、手机2 在 (721,890)），**不是整屏偏移**，绝不许用一台的坐标加减算另一台。
R17 全黑帧 = 息屏，**按设备问题处理**（2026-11-03 真机：手机息屏时 screencap 回来纯黑、
    input tap 石沉大海、focus 却仍报旧 activity，日志看着像「坐标错了 / 没落地」）。
    判面板 / 判落地 / 复核三处拿到黑帧：先 dev.wake() + 0.8s 重拍一次（复用同一帧，不额外截图）；
    还黑 -> status="screen-off"（frame_black=true, rewoke=true）：不算 skipped、不给该行
    give_up+1、不进恢复阶梯；连续 SCREEN_OFF_MAX_ROUNDS=3 轮全黑才 safety_stop 停手等人。
R16 多面板连跑换面板时，先退出上一枚胶囊再点下一枚（2026-11-03 22:09 真机）：
    coin 跑到「到底」停在的是**淘金币任务列表**，不是「双11 淘金币/赛车主页面」——
    两屏的 activity 都是 TMSActivity，于是写死序列第二拍 (151,1356)/(1299,1357) 直接落在
    任务列表的某一行上（没进赛车面板，白走 20 秒恢复阶梯）。
    规则：上一个面板**真的做过轮次**之后切下一个面板 → 先 dev.back() 一次（+0.8s）退回主页面，
    再按写死序列点胶囊；被跳过/没开做的面板不按 BACK（别把本来就在主页面退多了）。
R15 跳转类任务一律「不滑，直接返回」（2026-11-02 用户真机口径）：
    用户原话「跳转app去逛的不用滑的，如果非要滑也行，就是耽误点时间，其实跳转后直接返回到
    任务页就做完了，有时会要多返回几步，因为跳转可能会辗转多个窗口」。
    落地判定之后先读 focus：焦点**不在淘宝**（支付宝 / 天猫 / 任何别的 App）→ 一次 dwell
    都不调，直接 BACK 回任务页（最多 JUMP_BACK_MAX=6 次，每次读 focus，回到 TBMainActivity /
    TMSActivity 就停）；焦点在淘宝内 → 照旧 browse_dwell 滑满 secs（淘宝内靠滑动走计时）。
    跳转类**不因为"没滑"扣分**：完成与否只看「回到面板后该行消失 / 计数 +1」。
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

# 2026-11-02（真机事故）：Windows 控制台是 GBK，日志里只要出现一个 GBK 编不出来的符号
# （比如 ✔ U+2714），print 就抛 UnicodeEncodeError，**整轮直接崩掉**——真机上连崩两轮，
# 报告写了但面板没进。日志绝不能有能力杀死业务逻辑，所以这里把 stdout/stderr 的编码错误
# 一律降级成 'replace'：编不出的字符打成 ?，流程继续。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

# --------------------------------------------------------------------------- #
# 常量：坐标 / 设备
# --------------------------------------------------------------------------- #
TAOBAO_PKG = "com.taobao.taobao"
# R6：拉回淘宝只用 am start 欢迎页，绝不 force-stop
TAOBAO_WELCOME = "com.taobao.taobao/com.taobao.tao.welcome.Welcome"
ALIPAY_PKG = "com.eg.android.AlipayGphone"

# 面板导航的已知坐标（OCR 找不到文字时的兜底；都来自真实证据帧）
NAV_MY_TAOBAO = (1207, 3073)     # 底部 tab「我的淘宝」  logs/nav5.json y=3073 x=1208
NAV_COIN_ENTRY = (721, 890)      # 「领淘金币」           logs/nav5.json y=890 x=620
NAV_GO_EARN_COIN = (151, 1322)   # 「去赚金币」           round-001/02_before_ocr.json
NAV_GO_EARN_ENERGY = (1298, 1322)  # 「去赚能量」          round-001/02_before_ocr.json
NAV_EXPAND = (1253, 2491)        # 收起态的「展开」       round-002/02_before_ocr.json y=2492
NAV_RACE_TASKS = (1298, 1320)    # 赛车页「做任务赚能量」  p4/report.md

# R7：扫描/浏览都用这一条向下手势
SWIPE_DOWN = (720, 2400, 720, 1600, 300)

# --------------------------------------------------------------------------- #
# 2026-11-02 19:17 用户现场要求：导航写死、几乎不 OCR（第四批·导航改造）
# 每条写死坐标都必须能追到一帧真机证据；换手机 / 淘宝改版后要重新取证（见 REPORT 风险）
# --------------------------------------------------------------------------- #
#: 首页九宫格「领淘金币」图标中心
#: 源：targets/targets.jsonl intent=coin_entry（ts 20261102_145439，父会话按真机截图 OCR 建立）
HOME_COIN_ENTRY = (159, 938)
#: 底部 tab「我的淘宝」
#: 源：targets/targets.jsonl intent=my_taobao（ts 20261102_140505，x=1294 y=2996）
HOME_MY_TAOBAO = (1294, 2996)
#: 我的淘宝页「领淘金币」图标（图标下的那行字：OCR 盒 x 350-552 / y 1955 → 中心 x=451）
#: 源：evidence/2026-11-02-auto/navfail/141826_coin_navfail_ocr.txt 行「1955  350-552  领淘金币」
MY_TB_GET_COIN = (451, 1955)
#: 淘金币主页面底部黑色胶囊「去赚金币」（左）
#: 源：evidence/2026-11-02-auto/rounds/round-010/07_postcheck_ocr.json 盒 (54-248, y=1356)
TASK_GO_COIN = (151, 1356)
#: 淘金币主页面底部黑色胶囊「去赚能量」（右，与上面那条同一行）
#: 源：targets/targets.jsonl intent=go-earn-energy 的 (1303,1266)；
#:     同一枚胶囊的文字在 round-010/07_postcheck_ocr.json 盒 (1204-1394, y=1357)，
#: 2026-11-02 20:32 真机截图 OCR 复测：这枚胶囊的文字盒是 x=1204-1394 / y=1357（中心 1299,1357），
#: 原 (1303,1266) 高了 91px —— 会点到上方「抵¥9 去比赛」。已按实测改成 (1299,1357)。
TASK_GO_ENERGY = (1299, 1357)

#: 写死坐标表：按屏分辨率登记（2026-11-03 第五批：手机2 也写死）。
#: 键 = "dev.w x dev.h" —— Device.__init__ 已经读过 wm size（self.w/self.h），直接用它，
#: 不为这件事再起一次 adb shell。每个坐标都是真机截图 + rapidocr 盒中心实测，来源见行内注释。
#: 未登记的分辨率**不许瞎猜**（见 R14）：written_nav_to_panel 一次点击都不发。
DEV_COORDS = {
    # 手机1 V2505A / 1440x3168（实测 2026-11-02 20:32：真机截图 + rapidocr 盒中心）
    "1440x3168": {
        "device": "手机1 V2505A 1440x3168",
        "measured_at": "2026-11-02 20:32 真机（截图 + rapidocr 盒中心）",
        # 首页九宫格「领淘金币」图标中心：OCR 盒 x=54-264 / y=936
        "home_coin_entry": (159, 938),
        # 底部 tab「我的淘宝」：OCR 文字中心 (1298,3041)，tab 图标在文字上方
        "home_my_taobao": (1294, 2996),
        # 我的淘宝页「领淘金币」：OCR 盒 x=350-552 / y=1955 -> 中心 x=451
        "my_tb_get_coin": (451, 1955),
        # 淘金币页底部黑胶囊「去赚金币」：OCR 盒 x=54-250 / y=1356
        "task_go_coin": (719, 857),
        # 同一行右侧「去赚能量」：OCR 盒 x=1204-1394 / y=1357
        # （旧值 (1303,1266) 高了 91px，会点到上方「抵¥9 去比赛」，已按实测改）
        "task_go_energy": (1299, 1357),
        # 「点击商品领优惠红包」任务页（原商品页）上，一屏可见的 3 张商品卡的固定点击点。
        # 实测 2026-11-03 22:01 真机（截图 evidence/2026-11-03-observe-click-goods/s3b_landing_later.png）：
        # 进页面那一帧 rapidocr 读到 3 枚「点我得淘金币」按钮盒 x=762-1082，
        # 盒顶 y=1605 / 2211 / 2815（行距 606/604）-> 取盒中心 (922, y+23)：
        #   (922,1628) (922,2234) (922,2838)
        # 实点验证（同一天真机）：(922,1628) 在 (0/3) 与 (1/3) 两帧各打开一次商品详情
        # （焦点 -> TTDetailActivity），BACK 后页面进度各 +1；第三发在 (2/3) 帧后按 (922,2234)
        # 打开详情、进度到 (3/3)，BACK 后面板上这条行消失（= 做完）。
        # 点卡片而不是点「去购买」类按钮：这三发落在卡片主体上，详情页里一个按钮都不点。
        # **未证实（待下次真机复测）**：曾据此推断「做完一张卡后列表会把卡片上顶 ~192px」——
        # 用户 2026-11-03 指出那次上移**可能是他自己手动滚动造成的**，所以「上顶 192px」
        # 只是一个未经证实的推测，不是实测结论。实测办法见 product_click_flow 里的 band_y
        # 证据（每次回原商品页都记一次进度行的 y，复跑后看 y 变没变）。
        "product_cards": ((922, 1628), (922, 2234), (922, 2838)),
    },
    # 手机2 V2243A / 1440x3200（192.168.1.5:43059）；
    # 实测 2026-11-03 真机：用户逐点截图 + rapidocr 取盒中心（每点都实测过，不是推算）。
    # 注意：手机2 首页版式与手机1 不同 ——「领淘金币」在 (721,890)（手机1 在 (159,938)），
    # 不是整屏偏移，别拿手机1 的坐标加减换算。
    "1440x3200": {
        "device": "手机2 V2243A 1440x3200",
        "measured_at": "2026-11-03 真机（用户实测：截图 + rapidocr 盒中心）",
        "home_coin_entry": (721, 890),     # 首页「领淘金币」
        "home_my_taobao": (1298, 3073),    # 底部 tab「我的淘宝」
        "my_tb_get_coin": (453, 1917),     # 我的淘宝页「领淘金币」
        "task_go_coin": (721, 889),       # 淘金币页「去赚金币」（左胶囊）
        "task_go_energy": (1299, 1322),    # 淘金币页「去赚能量」（右胶囊）
        # 2026-11-04 真机实测补录（手机2 1440x3200）：行「点击商品领优惠红包(0/1)」的任务页 OCR ——
        #   标题「领补贴2.0-淘金币」y=227；进度「点3个商品，得30淘金币（0/3）」y=715；
        #   **四枚**「点我得淘金币」按钮，盒 x 758~1081、盒心 y = 1487 / 2014 / 2540 / 3066
        #   （行距约 527；手机1 是 3 枚、行距约 606）。取盒中心 x=920。
        #   **别拿手机1 的 (922,1628)/(922,2234)/(922,2838) 换算过来** —— 版式不同。
        #   手机2 原来没登这张表，导致这条任务每次都被「未登记分辨率 -> 一次点击都不发」白跑一轮。
        "product_cards": ((920, 1487), (920, 2014), (920, 2540), (920, 3066)),
        # 2026-11-04 用户拍板：淘宝内置小游戏（消消乐/关卡页）右上角那个白圈 ◎ = 关闭键，
        # 点它直接退出游戏回任务页。坐标从用户 17:02 的截图按 1440x3200 量取
        # （截图 480x1066 = 本机 1/3 缩放，右上角白圈中心约 (450,72) -> 乘 3）。
        # 手机1 没做过这类任务，**不登记**（没有这一项就照旧走 BACK 阶梯，一个字都不变）。
        "game_close": (1350, 215),
    },
}
#: 老常量（HOME_COIN_ENTRY …）属于这一组：只留给兼容调用点当默认值，导航内部一律走 DEV_COORDS
DEFAULT_DEV_KEY = "1440x3168"
# --------------------------------------------------------------------------- #
# 0 OCR 的「我现在在哪一屏」校验 —— 2026-11-02 19:49 真机事故后加的硬规则
# 事故：am start 后固定 sleep(3) 就盲点，冷启动 3 秒还在欢迎页，于是 (159,938) 点空、
#       后面几步全在**不认识的页面**上继续盲点，最后停在 TTDetailActivity 商品下单页，
#       只差一点就点到「立即支付」。结论：写死坐标没问题，缺的是「点之前先确认我在哪一屏」。
# 硬规则（不许绕过，也不消耗 OCR 预算 —— 读 focus 是 dumpsys，约 0.2s）：
#   1. am start 之后**不许固定 sleep 后盲点**：轮询 focus，等到淘宝自己的 activity 再往下走；
#   2. 每一发盲点之前都要读一次 focus，activity 不在该步的预期清单里就**绝不点**；
#   3. 焦点一旦变成商品/下单/支付宝类 activity，**立刻 BACK** 退干净（期间零点击），
#      退不掉就交给恢复阶梯；恢复阶梯 force-stop 之前也要先退货页面。
# --------------------------------------------------------------------------- #
#: 淘宝首页 / 「我的淘宝」tab 页（实测同一 activity：navfail/141826 focuse=TBMainActivity）
FOCUS_ACTS_HOME = ("TBMainActivity",)
#: 淘金币主页面 / 淘金币面板 / 赛车面板（三屏都是它 —— 这就是当初要靠像素指纹分辨的原因）
FOCUS_ACTS_TMS = ("TMSActivity",)
#: 站内自家人页面（我的淘宝 / 淘宝首页 / 浏览器容器页）
FOCUS_ACTS_VISIT = ("TBMainActivity", "TMSActivity", "BrowserActivity")
#: 商品 / 下单 / 看图 / 支付宝类：出现在这里一律先 BACK，绝不再点
FOCUS_BAD_ACTS = ("TTDetailActivity", "NewDetailActivity", "IrpActivity")
FOCUS_BAD_SUB = ("detail", "order", "cart", "alipay", "pay", "trade", "login")
#: focus 轮询间隔（每次 dumpsys 约 0.2s）
FOCUS_POLL_S = 0.4
#: am start 之后最多等多久（冷启动实测 3 秒不够）
FOCUS_LAUNCH_MAX_S = 20.0
#: 每一步点完之后，最多等多久让 activity 变成预期的那一个
FOCUS_STEP_MAX_S = 6.0
#: 危险页最多按几次 BACK 退出来
FOCUS_ESCAPE_BACKS = 3
#: 进面板前固定第一步：force-stop 之后等多久再 am start（用户 2026-11-02：不管怎样先重启）
WRITTEN_NAV_KILL_S = 1.0
#: 开屏/活动页（一直是 TMSActivity）连续几次轮询才按 1 次 BACK
FOCUS_TMS_BACK_AFTER = 3
#: 活动页最多按几次 BACK（不许循环，用户明确「最多 2 次」）
FOCUS_TMS_BACK_MAX = 2
#: 首页 activity 起来之后**再固定等这么久**才允许点（2026-11-02 20:15 真机实测：
#: 冷启动 0.9s 就聚焦到 TBMainActivity，但那时页面还没渲染完，(159,938) 点空；
#: 2s 那一帧已完整渲染（同一帧能读到 芭芭农场/百亿补贴/省钱卡/我的淘宝 tab）。
#: activity 就绪 ≠ 页面画完 —— 这段等待不截图、不 OCR，只 sleep 一次。）
HOME_SETTLE_S = 2.0
#: 页内换页（首页 -> 淘金币主页面）后，再等这么久才点下一发。
#: 2026-11-02 真机：TMSActivity 0.1s 就切过来了，但 H5 页面约 1s 时还没接管点击，
#: 那一拍直接点空、页面纹丝不动（日志看着像"点了"）。历史上成功那轮两次点击间隔 14s。
STEP_SETTLE_S = 2.5
#: 面板点开之后，**任务列表是异步加载的**：标题先出来，列表区域可能还是一片空白。
#: 2026-11-02 真机（手机2 赛车面板）就是栽在这：点完胶囊固定等 2.5s 就判，
#: 判定帧里只有标题（OCR 只读到 25 个文字块），于是被当成「没进面板」→ 白走恢复阶梯。
#: 解决办法：判之前先确认列表区域**有内容**（纯像素，不解码文字、不 OCR）：
#:   把列表区域缩成小图、算「深色像素占比」，占比够高就说明列表已经画出来了。
PANEL_READY_INK = 0.004        # 列表区域里深色像素占比超过 0.4% 就算画出来了（空白页≈0）
PANEL_READY_BOX = (0.05, 0.18, 0.95, 0.95)   # 相对屏幕：左/上/右/下（避开标题与底栏）
PANEL_READY_POLL_S = 0.7       # 每次重拍间隔
PANEL_READY_MAX_S = 8.0        # 最多等这么久（列表还空白就放弃，交给原判定）
#: 写死序列：每一步 = {xy 坐标 / wait 点完等几秒 / need 点之前必须是的 activity /
#:                   then 点完必须等到的 activity（None = 只 sleep(wait)）}
#: 主路径 2 步 / 兜底路径 3 步，动作之间 0 次 OCR。
#: 坐标由外面传进来（R14）：老常量那组 = 手机1；运行时**一律**调 written_nav_steps(dev)
#: 按当前分辨率从 DEV_COORDS 取表，绝不拿别的机型的坐标去点。
def _written_nav_of(home_coin_entry, home_my_taobao, my_tb_get_coin,
                    task_go_coin, task_go_energy):
    """按一组坐标装出写死序列（主路径 2 步 / 兜底路径 3 步，两个面板只差第二拍）。"""

    def _steps(go_xy):
        return {
            "primary": [
                {"xy": home_coin_entry, "wait": 0.0, "need": FOCUS_ACTS_HOME,
                 "then": FOCUS_ACTS_TMS},
                # 2026-11-07 日常版（真机实测）：币页中间那颗大红按钮 = 未签到「签到领金币」/
                # 签完「赚更多金币」，位置不变。这里只点一次（签到由 coin_signin_if_needed 先处理），
                # **绝不能盲点两次** —— 已进面板时 y≈857 正是「币+30/50/100」奖励芯片，会点开奖励页。
                # 坐标：手机1 (719,857)@1440x3168、手机2 (721,889)@1440x3200。
                {"xy": go_xy, "wait": 1.5, "need": FOCUS_ACTS_TMS, "then": None},
            ],
            "fallback": [
                {"xy": home_my_taobao, "wait": 0.0, "need": FOCUS_ACTS_HOME,
                 "then": FOCUS_ACTS_VISIT},
                {"xy": my_tb_get_coin, "wait": 0.0, "need": FOCUS_ACTS_HOME,
                 "then": FOCUS_ACTS_TMS},
                {"xy": go_xy, "wait": 1.5, "need": FOCUS_ACTS_TMS, "then": None},
            ],
        }

    return {"coin": _steps(task_go_coin), "race": _steps(task_go_energy)}


#: 兼容用：手机1 那组坐标装出来的写死序列（旧调用点可能读这个名字；导航运行时不看它）
WRITTEN_NAV = _written_nav_of(HOME_COIN_ENTRY, HOME_MY_TAOBAO, MY_TB_GET_COIN,
                              TASK_GO_COIN, TASK_GO_ENERGY)


def device_resolution(dev):
    """当前设备的 "WxH"（Device.__init__ 已读过 wm size，这里只拼字符串，不碰 adb）。"""
    try:
        return "%dx%d" % (int(dev.w), int(dev.h))
    except (AttributeError, TypeError, ValueError):
        return ""


def device_coords(dev):
    """按分辨率取写死坐标表。返回 (key, 表 or None)：None = 未登记 -> 一次点击都不许发（R14）。"""
    key = device_resolution(dev)
    return key, (DEV_COORDS.get(key) if key else None)


def written_nav_steps(dev):
    """这台设备自己的写死序列。返回 (key, {coin: …, race: …} 或 None)。"""
    key, table = device_coords(dev)
    if not table:
        return key, None
    return key, _written_nav_of(table["home_coin_entry"], table["home_my_taobao"],
                                table["my_tb_get_coin"], table["task_go_coin"],
                                table["task_go_energy"])
#: 恢复阶梯（只在「任务识别 / 完成识别」两处用）：BACK 最多 3 次，每次 BACK 后 1 次 OCR
RECOVER_BACKS = 3
#: 每轮最多重启淘宝 1 次（用户 2026-11-02 19:17：别无限重启）
RECOVER_RESTART_MAX = 1

OCR_SCALE = 0.5          # 全屏 OCR 的缩放；实测 1440x3200 在此比例下 2~4s/帧
DEFAULT_BROWSE_SECS = 15  # 行里读不到「浏览N秒」时的默认停留
#: 2026-11-04 用户实测（手机2）：「天天签到免费拿IP周边(0/1)」副标题只写「浏览最新礼盒得」、**不写秒数**，
#: 用户点开看过：只要浏览 5 秒。所以「写了浏览、没写秒数」的行按 5 秒处理 ——
#: **只在命中 RELAX_BROWSE_WORDS 的行上生效**（见 dwell_plan 与 classify 的浏览通道），别的行不受影响。
BROWSE_NO_SECS_S = 5
BROWSE_NO_SECS_HINTS = ("浏览",)

# --------------------------------------------------------------------------- #
# R1：按钮 x 守卫（事故：算出 1078 点错页）
# --------------------------------------------------------------------------- #
BTN_X_MIN = 1245
BTN_X_MAX = 1270

# --------------------------------------------------------------------------- #
# 规则常量：行尾动作 / 黑名单 / 外部 App / 支付宝授权通道
# --------------------------------------------------------------------------- #
#: 行尾可点的动作标签（parse_rows 靠它把一整屏切成「行」）
#: 2026-11-04 真机（手机1 14:01 用户截图）补录「看一看」：「酒店超抵日至高5%(0/1) 浏览5秒 +20」
#: 这一行的末端按钮就是「看一看」—— 明写浏览 5 秒、没有危险词、也不是领取类，完全合规；但它
#: 当初不在任何一张词表里 -> parse_rows 切不出这一行 -> 脚本**一条日志都没有**、从没做过它。
#: 它和「去逛逛 / 逛一逛」同级，是**浏览类**动作（点下去 = 进详情页逛，不领不发不跳支付）。
ACTION_LABELS = ("去完成", "去逛逛", "逛一逛", "去搜索", "去浏览",
                 "看一看",
                 "去领取", "立即领", "领取奖励", "已完成", "已领取", "已参加")
#: 允许点下去的动作（R5：领取类不在其中）
#: 「领取」三个字这一族的（去领取 / 立即领 / 领取奖励）只在 ACTION_LABELS 里，**永远不进 TAP_LABELS**。
TAP_LABELS = ("去完成", "去逛逛", "逛一逛", "去搜索", "去浏览", "看一看")
DONE_LABELS = ("已完成", "已领取", "已参加")
#: 有些行末端不是按钮而是一枚奖励芯片（+50），点它同样进任务（round-130 现证）
REWARD_CHIP_RE = re.compile(r"^[+＋]\s*\d{1,6}$")

#: R5 黑名单词：命中即跳过（行名 + 副标题一起扫）
BLACK_WORDS = ("邀请", "助力", "去玩", "游戏", "下单", "买", "折扣", "领取", "立即领",
               "领现金", "点击去逛", "点击商品", "授权", "开通", "同意", "支付", "转账",
               "广告", "始终打开", "签到", "免费拿", "答题", "考试", "安装", "下载",
               "卸载", "分享", "拉人",
               # 小游戏 / 抽奖 / 课堂类（"玩消消乐领金币" 曾因黑名单只有「游戏」两字被判成可做）
               "消消乐", "玩一玩", "小游戏", "玩游戏", "修仙", "课堂",
               "抢", "红包抢", "补贴")
#: 外部 App（未装 / 会跳走 / 与任务无关）
#: 2026-11-03：「百度极速版」必须排在「百度」前（更具体的名字先命中，见 VISIT_OK_APPS 的包名差异）
EXT_APPS = ("百度极速版", "百度", "头条极速版", "头条", "快手", "UC", "闲鱼", "京东", "拼多多", "美团", "抖音",
            "唯品会", "得物", "58同城", "知乎", "微博", "夸克",
            # 2026-11-04 真机事故（手机1 02:08「去淘宝特价版领红包(0/1)」被连做 4 轮才放弃）：
            # 名单制漏了这个 App -> card["ext"] 为空 -> 没有 ext_pkg -> one_round 的
            # ext_app_installed 闸根本不触发 -> 被当成普通站内任务点了 4 次，全程没跳出淘宝。
            # 注意：名字里带「淘宝」两个字，但它是**另一个 App**（com.taobao.lite），
            # 和淘宝站内（com.taobao.taobao）不是一回事，别把两者混成一个词。
            # 匹配按**名字最长**取（见 _classify_card 的 max(_ext_ok, key=len)），
            # 所以它排在哪个位置都不会被更短的「淘宝」类词吃掉。
            "淘宝特价版")
#: 2026-11-02 用户批准放开的一小口子：**已安装**的外部 App，且这行没有任何"危险动作词"时，
#: 允许「点进去 -> 停 5 秒 -> 返回」这类（原来只有微博/百度/头条/夸克）。用户原话：
#: 「有一些你黑名单的其实可以做，做完还能刷新出在白名单的任务……按你说的做」。
#: 2026-11-03 用户再拍板（--appcheck「App 自检」的由来）：**凡是自检查到已安装的外部 App，
#: 一律放行「点进去逛一下」**。用户原话：「我还想做个自检，像那个任务扫描一样，可以确定手机
#: 有哪些app，没下载的app脚本就不会做，如果哪天下载了再跑一遍自检就可以做」。
#: 所以这张表现在覆盖 EXT_APPS **全量 17 个** = 用户点名的 15 个 + 原本就在册的「夸克」
#: + 2026-11-04 事故后补录的「淘宝特价版」（未用 pm 实测，见表里的第三列说明），一个不漏。
#: 一致性：EXT_APPS 与 EXT_APP_PKGS 的名称集合必须**完全相等**（--selftest 里有断言），
#: 名单里多一个名字却没有包名 = 这一行的 ext_pkg 为空 = ext_app_installed 闸不触发（就是本次事故）。
#: 下面三件事一个都没放松：
#:   ① 仍然**绝对**不许：授权 / 开通 / 同意 / 支付 / 转账 / 下单 / 买 / 折扣 / 答题 / 考试 /
#:      邀请 / 助力 / 免费拿 / 签到 / 下载 / 安装 / 去玩 / 游戏（DANGER_HARD 按**任务名**判定，
#:      优先于本白名单；BLACK_WORDS / POPUP_HARD_FORBIDDEN 同样一个字都没改）；
#:   ② 每个 App 点进去**之前**都要**实时**查一次 pm list packages（ext_app_installed），
#:      没装 -> 日志「外部 App 未安装(...) -> 不点，换下一行」+ verdict ext-missing；
#:      state/apps.json 只给人看，**不参与**「做不做」的判定（清单过期也绝不跳过其实已装的 App）；
#:   ③ 支付宝**绝不进这张表**：它走 ALIPAY_OK / ALIPAY_PKG 那套授权通道，语义不同
#:      （只认「支付宝农场」「金豆夺宝」两类行，进去只连滑、零点击、弹窗立即 BACK）。
#: 2026-11-03 补：百度极速版是**另一个包名**（com.baidu.searchbox.lite），和正式版 com.baidu.searchbox 无关。
#: 不补的后果：行名「去百度极速版领现金」会去查正式版的包名 —— 正式版没装时日志报错包名（误报），
#: 正式版装了时又会真的点进极速版安装页（漏拦）。补上后：没装 -> ext_app_installed 拦成 ext-missing。
#:
#: 第三列 = 这个包名有没有用**真机** pm list packages 实测确认过（2026-11-03 两台 App 自检）：
#:   True  = 实测确认（手机1 10.241.163.217:5555 / 手机2 10.241.163.254:5555，pm 里真有这一行）；
#:   False = **未验证**（那两台都没装这个 App，pm 查不到，只能按公开包名填）。
#:     代价写清楚：万一包名写错，这一行会**永远**判成「未装」= 这条任务永远不做
#:     （保守、可接受，用户已确认）。绝不许把未验证的包名当事实：--appcheck 写 state/apps.json
#:     时带 verified:false，控制台表格的「原因」列也会标出来。
EXT_APP_PKGS = (
    # 未验证（2026-11-03 更正）：这一行原来标 True 是错的 —— 两台真机都没装这个包，
    # pm 里查不到 com.baidu.searchbox.lite，**没有**用 pm 复核过包名。沿用旧口径：两台均未安装，
    # 未用 pm 复核过；只把「已实测」这个假事实改掉，行为不变（没装就是不做）。
    ("百度极速版", "com.baidu.searchbox.lite", False),
    ("百度", "com.baidu.searchbox", True),
    # 未验证（2026-11-04 真机，手机2）：头条**正式版** com.ss.android.article.news 装着、
    # **极速版**没装；而行名「头条极速版刷视频(0/1)」只命中了名单里的「头条」→ 查的是正式版 →
    # 判成「已装」放行 → 点进去落不到极速版 → 不计数 → 试 2 次放弃（和百度极速版同一个坑）。
    # 包名 com.ss.android.article.lite 是按公开包名补录的，**没用 pm 实测**（两台都没装极速版）。
    # 写错的后果是「永远判未装 = 这条任务永远不做」，方向保守；装了之后跑一次 --appcheck 即可确认。
    ("头条极速版", "com.ss.android.article.lite", False),
    ("头条", "com.ss.android.article.news", True),
    # 未验证：两台都没装（手机1 的 pm 里只有 com.kuaishou.nebula，那是快手极速版，是另一个 App）
    ("快手", "com.smile.gifmaker", False),
    ("UC", "com.UCMobile", True),
    ("闲鱼", "com.taobao.idlefish", True),
    # 实测修正：用户稿里的 com.jingdong.app.market 在两台真机 pm 里**都不存在**，
    # 京东的真实包名是 com.jingdong.app.mall（两台手机都装了、都查到了这一行）
    ("京东", "com.jingdong.app.mall", True),
    ("拼多多", "com.xunmeng.pinduoduo", True),
    ("美团", "com.sankuai.meituan", True),
    ("抖音", "com.ss.android.ugc.aweme", True),
    # 未验证：两台都没装
    ("唯品会", "com.achievo.vipshop", False),
    # 未验证：两台都没装
    ("得物", "com.shizhuang.duapp", False),
    # 未验证：两台都没装
    ("58同城", "com.wuba", False),
    ("知乎", "com.zhihu.android", True),
    ("微博", "com.sina.weibo", True),
    ("夸克", "com.quark.browser", True),
    # 未验证（2026-11-04 事故后按公开包名补录）：未用 pm 实测；手机1 当前未安装该 App，无法复核。
    # 行为：没装 -> one_round 的既有 ext-missing 路径（「外部 App 未安装（淘宝特价版，com.taobao.lite）
    # -> 不点，换下一行」），一个点击都不发，也绝不引导安装。
    ("淘宝特价版", "com.taobao.lite", False),
)
#: 名称 -> 包名：分类（_classify_card）和执行（ext_app_installed）只认这张表。
#: 表里顺序无意义：同一行点名多个 App 时由 max(_ext_ok, key=len) 取**名字最长**的那条
#: （「百度极速版」比「百度」长，才不会把极速版的行错查成正式版包名）。
VISIT_OK_APPS = {name: pkg for name, pkg, _v in EXT_APP_PKGS}
DANGER_HARD = ("授权", "开通", "同意", "支付", "转账", "下单", "买", "折扣", "答题",
               "考试", "邀请", "助力", "免费拿", "下载", "安装", "去玩", "游戏",
               "消消乐", "小游戏", "抽奖", "课堂",
               # 刷视频/看直播要长时间观看，不是"点进去停 5 秒就返回"，仍然不做
               "视频", "直播", "听课",
               # 2026-11-03 用户口径 ③：金融类不碰
               "理财", "基金", "保险", "贷款", "借款", "分期", "信用卡")
#: 2026-11-03 用户三级口径（明确拍板）：
#:   ① 已装 App + 只是「点进去逛一下/浏览」→ 可以（进去停 5 秒就返回，不点里面任何按钮）
#:   ② 答题 / 考试 / 课堂 → 需要人工做，脚本不碰
#:   ③ 助力 / 邀请 / 理财 / 下单 / 买 / 支付 / 授权 → 绝不碰
#: ①里除了微博/百度/头条/夸克，**支付宝家族（蚂蚁庄园/芭芭农场/神奇海洋/蚂蚁森林）也算**：
#: 它们一样是「点进去逛一下」，而我们的跳转类流程在目标 App 里**一个按钮都不点**，只停留+返回。
VISIT_LIKE = ("去逛逛", "点击去逛", "逛一逛", "逛逛", "浏览", "去逛")

#: 2026-11-03 用户授权（交接文档「五、待办」第 2/4/5 条）——**词级放宽，只放这 5 个词**：
#:   视频 / 消消乐 / 下单 / 补贴 / 抢
#: 必须**同时**满足两条才不拦：
#:   (a) 这一行**明写浏览N秒**（card["secs_raw"] 有值，即 secs_from_text 读得到）；
#:   (b) 走的是**纯浏览流程**：kind 归为 browse（点进去只停留/只滑动，不点商品、不点任何按钮），
#:       而且末端动作必须是浏览类（TAP_LABELS）或奖励芯片 —— 领取类按钮不算纯浏览。
#: 其余 DANGER_HARD / BLACK_WORDS / POPUP_HARD_FORBIDDEN 的词一个都没动
#: （支付/转账/授权/开通/同意/理财/基金/保险/贷款/答题/考试/邀请/助力/免费拿/下载/安装/去玩…照旧拦）。
RELAX_BROWSE_WORDS = ("视频", "消消乐", "下单", "补贴", "抢", "签到", "免费拿")
#: 2026-11-04 用户拍板补「签到」「免费拿」这两个：真机行「天天签到免费拿IP周边(0/1) 浏览5秒 +200」
#: 同时踩了 BLACK_WORDS 的「签到」「免费拿」和 DANGER_HARD 的「免费拿」，被一刀切拦掉、一次都没做。
#: 它们在这类行里只是营销措辞（动作就是进去浏览 5 秒、零点击），所以并入同一个放宽口子；
#: **注意：不是从 BLACK_WORDS / DANGER_HARD 里删掉** —— 别的行照旧拦，只有「明写浏览N秒 + 纯浏览」才豁免。
#: 这条授权的日期：写进证据 authorized_by_user，审计时要能回答「这条为什么放开」
RELAX_USER_AUTH = "2026-11-03"

#: 2026-11-03 用户授权（交接文档「五、待办」第 3 条）——「点击商品领优惠红包(0/1)」。
#: 用户真机拍板的流程（原话：「我看他点进去只会滑动，直接点击首页可见的三个商品就行了，
#: 点完一个就返回到原商品页」）：
#:   点行尾「去完成」-> 任务页（= **原商品页**，一屏就能看到商品卡，不要滑屏去找）
#:   -> 在一屏可见范围内点 N 张商品卡（N 从副标题「点击N个商品」读，实测 3）
#:   -> 每点开一张只发 BACK，回到**原商品页**（不是回面板）-> 点完再 BACK 回面板。
#: 商品详情页内**零点击**：不点购买/加购/领券/收藏，只发 BACK。
#: 放宽范围只到这一条：行名含「点击商品」**且**行名/副标题含「点击N个商品」才走 product_click；
#: BLACK_WORDS 里「点击商品」对别的行照旧一票否决（一个字的含义都没改）。
PRODUCT_CLICK_WORD = "点击商品"
PRODUCT_CLICK_RE = re.compile(r"点击\s*(\d{1,2})\s*个商品")
PRODUCT_CLICK_AUTH = RELAX_USER_AUTH
#: 任务页（原商品页）的 activity —— 实测 2026-11-03 22:01 真机：BrowserActivity（H5 容器）。
#: 焦点守卫：只有焦点还停在这一页时才允许发商品卡点击；不在这页 = **一发都不发**。
PRODUCT_PAGE_ACTS = ("BrowserActivity",)
PRODUCT_TAP_SETTLE_S = 2.6      # 点完一张商品卡后等详情页拉起来
PRODUCT_BACK_SETTLE_S = 1.8     # BACK 回原商品页后等它重排
PRODUCT_PROGRESS_BAND = (690, 830)   # 任务页顶部「点3个商品，得30淘金币（x/3）」那一条带
PRODUCT_PROGRESS_RE = re.compile(r"[（(]\s*(\d{1,2})\s*/\s*(\d{1,2})\s*[)）]")
PRODUCT_MAX_TAPS = 6            # 保险丝：一次任务最多发几发商品卡点击（need+2 封顶）
#: 2026-11-03 缺陷 1 的修法（用户原话：「那就多等一下，刷新了再抓取」）。
#: 真机证据：round-095/096 的 08_product_click.json 里 page_progress[] 的 done/need/band_y
#: **全是 null** —— 进任务页**立刻**抓的那一帧页面还在半加载（白屏，只有标题和橙色头图），
#: product_page_progress() 读不到任何进度，流程只能靠 remaining -= 1 盲减，真实只到 2/3
#: 就记成 remaining_after=0。现在先轮询等页面就绪（读到 need）再抓 enter 帧。
PRODUCT_READY_POLL_S = 0.8      # 等页面就绪的抓帧间隔（≈0.8s）
PRODUCT_READY_MAX_TRIES = 6     # 上限 6 次 × 0.8s ≈ 4.8s（用户口径「最多等 4~5 秒」）
PRODUCT_RECHECK_TRIES = 2       # 每次 BACK 回原商品页后，读不到进度最多再等 2 次

#: 2026-11-03 用户**明确授权**（交接文档「五、待办」第 6、7 条）——这是对「R5 绝不点领取」的
#: **唯一例外**。用户原话：「任务到访得金币（直接点击领取，和累计任务领取奖励一样）」
#: 所以第 6、7 条实现成**同一个 kind**（claim_task）+ **同一条执行路径**（claim_card / run_claim）。
#:   第 6 条：行名/副标题命中「任务到访得金币」这类字样，且末端按钮**恰好**是「领取奖励」
#:            -> 只点**那一行**那一枚；
#:   第 7 条：面板顶部「今日累计奖励 / 累计任务奖励」**那一排**里的「立即领取」-> 点。
#: 除此以外**任何位置**的 领取 / 立即领取 / 领取奖励 照旧绝不点：BLACK_WORDS 与
#: POPUP_HARD_FORBIDDEN 里那几个词对其他行的含义**一个字都没改**（T23 断言照旧过）。
CLAIM_USER_AUTH = "2026-11-03"
CLAIM_KIND = "claim_task"
#: 第 6 条：行文字里必须出现这一串（OCR 常把「任务」二字读丢，所以认后半串「到访得金币」）。
CLAIM_ROW_WORDS = ("到访得金币",)
#: 第 6 条：末端按钮必须**恰好**是这四个字（「去领取」「立即领」「领取」等同族按钮照旧绝不点）。
CLAIM_ROW_BUTTON = "领取奖励"
#: 第 7 条：顶部那一排的锚点文字（标题在左，「币+N」与「立即领取」在同一/相邻 OCR 带）。
CLAIM_TOP_TITLES = ("今日累计奖励", "累计任务奖励")
#: 第 7 条：只认文字**恰好**是这四个字（精确相等，绝不做子串匹配）。
CLAIM_TOP_BUTTON = "立即领取"
#: 第 7 条「同一行 / 同一 OCR 带」的像素阈值（「立即领取」与锚点标题的 y 差上限）。
#: 真机实测：淘金币面板 今日累计奖励 y=857、三枚立即领取 y=943（差 86，
#:           见 evidence/2026-11-03-auto/rounds/round-001/02_before_ocr.json）；
#:           赛车面板   累计任务奖励 y=829、立即领取      y=845（差 16）。
CLAIM_TOP_BAND_DY = 140
#: 第 7 条还要求锚点标题在**面板顶部**：真机实测 y=829~857，上限给到 1500（约屏幕 47%）。
CLAIM_TOP_MAX_Y = 1500
#: 一次运行最多点几次顶部「立即领取」（失败/点空一律收手，绝不反复怼同一个按钮）。
CLAIM_TOP_MAX_TAPS = 1
#: 点完「领取奖励 / 立即领取」后等多久再取复核帧（领取走 toast / 计数刷新，实测 1 秒量级）。
CLAIM_SETTLE_S = 1.6
#: 领取万一被带离面板（或弹出奖励浮层）：最多按几次 BACK 回来（**只按 BACK，期间零点击**）。
CLAIM_BACK_MAX = 2


def danger_hits(txt):
    """硬危险词命中（带「支付 vs 支付宝」的排除）。

    坑：「去**支付宝**红包抢现金」包含"支付"两个字，朴素子串匹配会把整条任务误判成危险。
    这里对「支付」用 支付(?!宝) 的负向断言 —— 和文件里 DIALOG_TEXT/黑名单那处的口径一致。
    """
    t = str(txt or "")
    out = []
    for k in DANGER_HARD:
        if k == "支付":
            if re.search(r"支付(?!宝)", t):
                out.append(k)
            continue
        if k in t:
            out.append(k)
    return out
#: R3 授权通道：只认这两类支付宝任务（报告 9.2：仅滑动、零点击，且有到账证据）
ALIPAY_OK = ("支付宝农场", "金豆夺宝")
#: 其余带支付宝/蚂蚁字样的行一律跳过（未授权通道）
ALIPAY_HINT = ("支付宝", "蚂蚁", "金豆", "神奇海洋", "芭芭")

BROWSE_HINTS = ("浏览", "逛逛", "逛", "看", "沉浸", "好物", "清单")
SEARCH_HINTS = ("搜一搜", "搜索")

#: R4 搜索页的热词区块锚点
SEARCH_BLOCK_LABELS = ("搜索发现", "历史搜索", "猜你想搜")
#: R4 不能当 chip 的字符串（页面标题「搜索有福利」害过 round-034）
BAD_CHIP = ("搜索有福利", "搜索宝贝", "搜索发现", "历史搜索", "猜你想搜", "近一个月",
            "点击近一周", "行业", "趋势", "综合", "销量", "筛选", "超", "立得", "奖励")

#: R6 系统弹窗看门狗：包名 / Activity / 文案（fail 即停，不点不关）
DIALOG_PKG = ("packageinstaller", "permissioncontroller", "com.android.settings",
              "appfilter", "com.vivo.permissionmanager", "com.android.permission")
DIALOG_ACT = ("install", "permission", "appjumpprompt", "appfilter")
DIALOG_TEXT = ("继续安装", "未知来源", "安装未知应用", "允许安装", "应用权限", "是否允许",
               "USB调试", "将在外部应用打开", "即将跳转", "打开其他应用", "打开外部应用",
               "仅打开一次", "始终打开")
#: R3 支付宝内抽检的弹窗词（只用于「发现即 BACK」，绝不点它们）
ALIPAY_POPUP_WORDS = ("领取", "立即领", "授权", "开通", "同意", "确定", "去支付",
                      "签约", "立即使用", "立即参与", "去打开", "立即抢")

#: 缺陷 1 弹窗处置：第一级「关闭类文字」，命中就点它（用户选定的策略：点 X/关闭优先）
CLOSE_WORDS = ("关闭", "×", "X", "我知道了", "以后再说", "暂不", "忽略", "残忍离开")
CLOSE_GLYPHS = ("X", "x", "×", "✕", "✖", "╳")
#: 只做**子串**匹配的关闭词；单个字母/符号必须整串相等才算
#: （实测踩过：OCR 把一串「ZTOEXPRE」里的 X 当成关闭键 → 点到了页面别处）
CLOSE_WORDS_TEXT = tuple(w for w in CLOSE_WORDS if w not in ("×", "X"))
#: 用户 2026-11-02 明示：这些**绝对不许点**（交易 / 授权 / 抽奖类），弹窗处置一律避让。
POPUP_HARD_FORBIDDEN = ("立即领取", "领取", "去支付", "开通", "授权", "同意", "签约",
                        "抽奖", "去使用", "去玩", "抢")
#: 在用户清单之上再保守加两个（历史事故：点「支付」类会下单，「去逛逛」会跳走）
POPUP_FORBIDDEN = POPUP_HARD_FORBIDDEN + ("支付", "去逛逛")
#: 诊断词：页面还在加载 —— 不是弹窗，等一会儿再看，不许点
LOADING_WORDS = ("加载中", "正在加载", "请稍候", "正在跳转", "正在打开", "网络不给力")
POPUP_MAX_BACK = 2       # 第三级 BACK 兜底：最多 2 次
NAV_STEP_TRIES = 2       # 同一个导航动作最多试 2 次（2026-11-02 用户要求：卡住就重开淘宝，别穷举规则）
HARD_RESTART_MAX = 3     # 卡住时 force-stop 淘宝重开的最大次数（用户 2026-11-02 授权）
NAV_MAX_ACTIONS = 12     # 一次导航最多做多少个动作（保险丝）

def _home():
    """数据根：COIN11_ROOT 优先（打包版由 app_entry.py 注入），否则就是仓库根。

    源码模式两者逐字等价（coin11/auto.py 的上一级 = 仓库根），所以开发流程零回归。
    """
    env = (os.environ.get("COIN11_ROOT") or "").strip()
    if env:
        return os.path.normpath(env)
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _own_source():
    """本文件的源码：源码模式读 auto.py；打包版读随包带的 src/auto.py（T11 要读它）。"""
    p = os.path.abspath(__file__)
    if os.path.isfile(p):
        return p
    root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    cand = os.path.join(root, "src", "auto.py")
    if os.path.isfile(cand):
        return cand
    raise IOError("找不到源码副本（T11 源码卫生检查要用）：%s" % p)


#: 规则库 / 待教学 —— 和 teach.py 用的是同一份数据（自动化 ←→ 教学闭环）
TARGETS_DIR = os.path.join(_home(), "coin11", "targets")
TARGETS_PATH = os.path.join(TARGETS_DIR, "targets.jsonl")
PENDING_DIR = os.path.join(TARGETS_DIR, "pending")
EXIT_NEED_TEACH = 3      # 退出码 3 = 需要教学（不是崩溃）
RULE_RETRY_MAX = 2       # 同一意图用规则最多试 2 次，还是不成功才算「不会」
GENERIC_RECOVER_MAX = 1  # 通用兜底（label 就是「返回/关闭」的休闲规则）只试 1 次，点不动就问人
#: 导航时「规则优先」的步骤顺序（这一屏确实有这些字才用规则坐标，避免乱点）
NAV_RULE_STEPS = {"coin": ("去赚金币", "领淘金币"), "race": ("做任务赚能量", "去赚能量")}
#: 认不出入口 / 弹窗关不掉 时去规则库找哪些词（label 或 note 命中即可）
UNKNOWN_RULE_KEYS = ("导航", "返回", "关闭")
POPUP_RULE_KEYS = ("关闭", "我知道了", "关掉", "×")
WHAT_NAME = {"coin": "淘金币", "race": "赛车能量"}
TEACH_PORTS = (8777, 8778)   # 手机1 / 手机2 的教学页

#: 2026-11-09 用户定案：**赛车能量活动已下线** —— 点原来「去赚能量」那个位置，现在进的是
#: 「淘宝闪购」，不是任务面板（所以以前会一直找不到面板 -> 退出码 3 等教学）。
#: 相关代码一行没删（导航序列 / 面板判定 / 进度解析 / 测试都还在），只是默认不跑：
#:   - --what all / --what scan 只跑淘金币；
#:   - 活动哪天复出：把这里改成 True，或者显式写 --what race（会打一行警告，照跑）。
RACE_ENABLED = False

#: 面板标记与进度
PANEL_MARKERS = {
    # 2026-11-02 手机1 实测：淘金币面板标题已换成「赚金币抵钱」（原「今日速赚」那版也在用）
    "coin": ("今日速赚", "更多金币等你赚", "更多专享福利", "赚金币抵钱"),
    "race": ("做任务赚能量",),
}
PANEL_TITLE = {"coin": "今日速赚", "race": "做任务赚能量"}
RACE_PROGRESS_RE = re.compile(r"(\d{1,3})\s*/\s*50")

#: 计数 (0/1)  允许全角括号与空格
CPAT = re.compile(r"[（(]\s*(\d+)\s*/\s*(\d+)\s*[)）]")
CPAT_LOOSE = re.compile(r"(?<!\d)(\d{1,3})\s*/\s*(\d{1,3})(?!\d)")


# --------------------------------------------------------------------------- #
# 从归档脚本吸收的判据常量
#   每条注明来源（logs/ = 手机2 那轮驱动、_archive/v2 = 原 v2 体系、_archive/root_py = 手机1 旧实现）。
#   原则：只抄逻辑，不 import 那些目录里的任何模块（auto.py 必须单文件自包含）。
# --------------------------------------------------------------------------- #
#: 落地四分类的匹配词（logs/r2_expand.py classify()）。顺序有意义：天猫焦点串里也可能带 taobao
LANDING_NEEDLES = (("alipay", ("alipay", "com.eg.android")),
                   ("tmall", ("tmall", "jike")),
                   ("taobao", ("taobao",)))
#: 点完立刻读前台可能读到「还没切走」的旧 App（logs/r2_expand_coin.py 复核修正）
LANDING_SETTLE_S = 0.8

# --------------------------------------------------------------------------- #
# 息屏 = 全黑帧（2026-11-03 真机事故，别再用「坐标错了 / 没落地」解释它）
#   手机息屏时：screencap 回来是**纯黑**（两张黑帧 sha256 相同）、input tap 石沉大海，
#   但 dumpsys focus 仍报旧的 TBMainActivity —— 日志看上去像「坐标错了 / 没落地」，
#   其实是被息屏骗了。所以判面板 / 判落地 / 复核三处拿到黑帧时，一律先当**设备问题**：
#   唤醒 + 等 0.8s 重拍一次；还黑就记 status="screen-off"（独立状态：不算 skipped、
#   不给这一行 give_up+1、不进恢复阶梯），连续 SCREEN_OFF_MAX_ROUNDS 轮全黑才停手等人。
BLACK_FRAME_MAX = 4          # 16x16 灰度缩略图的 max() < 4 = 全黑（与 Device.shot 同一判据）
BLACK_REWAKE_S = 0.8         # 唤醒之后等多久再重拍
SCREEN_OFF_MAX_ROUNDS = 3    # 连续几轮拿全黑帧就停下等人（别在黑洞里空转）

#: 槽位判据（_archive/v2/percept/racing_rows.py）：按位置跟踪同一行，不按任务名
SLOT_TOL_FLOOR_PX = 24        # DEFAULT_TOL_PX：行距算不出来时的兜底阈值
SLOT_TOL_PITCH_RATIO = 0.5    # 阈值 = 行距的一半，保证阈值内最多一个候选
SLOT_MIN_PANEL_ROWS = 3       # MIN_PANEL_ROWS：不到 3 行 = 白屏 / 面板没打开
SLOT_PANEL_ROW_FLOOR = 0.5    # PANEL_ROW_FLOOR_RATIO：整盘读漏的下限

#: 视觉双证（_archive/v2/percept/vision.py）：整页 8x8 灰度指纹 + 平均 RGB
VISION_THUMB = (8, 8)
#: 细网格：浮层只盖住下半屏时，8x8 全局签名仍有 0.6 左右相似度（T16 实测 0.607），
#: 离阈值太近。细网格给出「坏格子占比」，把差异变成可定位的量。源 vision.PageFingerprint
VISION_GRID = (16, 32)
VISION_BAD_CELL = 0.20        # 单格灰度差超过它就记成「坏格子」
VISION_W_GLOBAL, VISION_W_GRID, VISION_W_BAD = 0.45, 0.25, 0.30
VISION_COLOR_MAX = 441.67     # sqrt(3)*255，RGB 欧氏距离的上限
VISION_W_STRUCT = 0.6
VISION_W_COLOR = 0.4
PAGE_MIN_SCORE = 0.72         # 「还是同一个页面」的阈值
PAGE_LOST_SCORE = 0.55        # 低于它：压根不是同一页 → 成功判据一律降级，不猜
VISION_LOST_BAD = 0.35        # 坏格子占比 ≥ 它就判不是同一页（半屏浮层实测 0.50）
PANEL_BOX = (0, 140, 1440, 3080)   # 比指纹的范围（去掉状态栏与导航栏）
#: 2026-11-02 用户要求（第 8 条）：点后那一帧与点前面板几乎一致 = 这一轮根本没落地。
#: 只比像素指纹（region_signature + signature_similarity，约 0.1s），绝不 OCR：
#: 面板与部分浏览落地页是同一个 TMSActivity，靠 focus 分辨不出来。
#: 阈值比「同一页」(PAGE_MIN_SCORE=0.72) 严得多：面板真没变化才拦，正在加载/换页不误拦。
LANDING_SAME_PANEL_SCORE = 0.93

#: 2026-11-02 20:53 真机误判（用户当场抓到，两轮都是「逛一逛支付宝农场(0/1)」）：
#: 旧口径是 tap 之后只 sleep(0.2) 就抓帧判「没落地」—— 淘宝点「去逛逛」拉起支付宝要 1 秒以上，
#: 0.2 秒那一帧跟面板逐像素相同（score=1.0）→ 判 skipped，白白丢一轮（其实后台已经跳转成功，
#: 返回任务页时行也没了）。现在改成：tap 后轮询焦点（dumpsys，零 OCR），焦点一变就算落地；
#: 焦点一直是面板那一屏才继续等，最多 3 秒，等满了才拿像素指纹下结论。
#: 用户原话：「可能是他跳转后无法识别是否成功，其实这个判定可以到返回到任务界面来判定，
#:           任务消失了大概率是完成了」—— 最终成败一律以复核帧的「行消失 / 计数 +1」为准。
LANDING_FOCUS_MAX_S = 3.0       # 焦点一直是同一屏时最多等多久
LANDING_FOCUS_POLL_S = 0.2      # 两次读 focus 之间睡多久（读一次本身约 0.2s，一轮约 0.4s）
LANDING_EARLY_PIXEL_S = 0.7     # 早于 3 秒先花一帧看屏幕有没有换（淘宝内落地不必每次白等 3 秒）
LANDING_BACK_SETTLE_S = 1.0     # 判成「可能没落地」后按 1 次 BACK，等页面稳住再抓帧
#: R15：跳转类任务（焦点不在淘宝）最多按几次 BACK 回任务页
#: 依据：用户 2026-11-02 真机观察「跳转后直接返回就做完了，有时会要多返回几步，
#:       因为跳转可能会辗转多个窗口」——所以给 6 次，且每次都读 focus，回到淘宝立刻停。
JUMP_BACK_MAX = 6
#: 每次 BACK 之后等多久再读 focus（切换窗口要时间）
JUMP_BACK_SETTLE_S = 0.8
#: C（2026-11-03 用户提议、已确认真机启动 Activity）：跳转类回淘宝**先 am start**，
#: BACK 只作兜底。等价于「点一下淘宝图标」：Android 把淘宝 Task 整体提到前台、栈内页面原样
#: 保留，不用一层层退栈（对比 BACK 最多 6 次 × 0.8s）；而且不按 BACK 就不会动到被切走那个
#: App 的页面状态。命令固定：am start -n com.taobao.taobao/com.taobao.tao.welcome.Welcome
AM_START_POLL_S = 0.2        # am start 之后每 0.2s 读一次 focus
AM_START_MAX_S = 3.0         # 最多轮询 3 秒（落不到淘宝页面就退回 BACK 阶梯，不许直接放弃）
#: R16：多面板连跑换面板时，按 1 次 BACK 退出上一个面板后等多久（让主页面回来）
PANEL_SWITCH_BACK_S = 0.8

#: 停留模式（_archive/root_py/run_energy.py plan()、do_energy.py act()）
#: 手机1 实测：浏览N秒必须持续滑动计时才走；玩N秒静止就计时；去逛进页面即算
#: 2026-11-02 用户要求（第 6 条余量）：浏览类余量由 4 砍到 1（用户拍板），
#: 命令行 --dwell-margin 可临时覆盖（默认取这个常量；T18 用符号断言，改值不会挂）。
DWELL_MARGIN_BROWSE_S = 1
DWELL_MARGIN_PLAY_S = 3
#: 「逛一逛 / 去逛逛」这类行：副标题不写秒数，但**进去以后有 5 秒计时**（2026-11-02 用户真机观察：
#: 手机2 的「好物沉浸看」子标题只写逛一逛，点进去能看到 5 秒倒计时）。所以按 5 秒静止停留，
#: 到时间再退出。原来这里是 12 秒——纯浪费，且没等够也没用（计时就是 5 秒）。
DWELL_VISIT_S = 5
#: 跳转类任务的停留秒数（2026-11-02 用户实测：进去有 5 秒倒计时，停满才算）。
#: **绝不用 DWELL_DEFAULT_S(18s)** —— 那是"读不到任何时长"的兜底滑动档，
#: 拿它去当跳转类的停留时间会白等 13 秒（本轮差点修坏「好物沉浸看」）。
JUMP_STAY_S = 5
DWELL_DEFAULT_S = 18
DWELL_BROWSE_RE = re.compile(r"浏览\s*(\d{1,3})\s*秒")
DWELL_STATIC_RE = re.compile(r"[玩看]\s*(\d{1,3})\s*秒")

#: 2026-11-03 用户真机观察后拍板（B）：停留时长**不再写死**，改成「阶梯 + 记忆」。
#: 用户原话：「第一次直接返回，没做完就加到5秒，再没做完再加到8秒」——第一档是起步值，
#: 「没做完」（行还在 / 计数没推进，见 dwell_escalation_decision 里的 not_done 判据）
#: 才升一档，到顶不再加：
#:   跳转类（只到访、不在目标 App 里浏览）   : 0  -> 5  -> 8  秒
#:   逛一逛（进去静止停留）                 : 5  -> 8  -> 8  秒
#:   浏览N秒（副标题明写秒数）               : N+1 -> N+3 -> N+6 秒
#:   玩N秒                                 : N+3 -> N+6 -> N+9 秒
#: 第 0 档的 N+1 / N+3 与 DWELL_MARGIN_BROWSE_S / DWELL_MARGIN_PLAY_S 是同一个值
#: （--dwell-margin 覆盖的正是第 0 档），改阶梯时第 0 档保持跟着那两个常数走。
#: 学到的高档位落进已有的 state/secs_cache.json（key 用 kind|n/m|名称族），
#: 下一轮同族任务直接从该档起步（见 dwell_level_get / dwell_level_put）。
DWELL_LADDER_JUMP_S = (0, 5, 10)    # 2026-11-04 用户：好物沉浸看 8 秒还是不够 -> 顶档 8 直接换成 10
DWELL_LADDER_VISIT_S = (5, 8, 8)
DWELL_LADDER_BROWSE_OFFSETS = (1, 3, 6)      # 浏览N秒：N+1 / N+3 / N+6
DWELL_LADDER_PLAY_OFFSETS = (3, 6, 9)        # 玩N秒  ：N+3 / N+6 / N+9
#: 阶梯最高档下标（3 档 = 0/1/2；到顶不再加）
DWELL_LADDER_TOP = 2
#: 保险丝：同一行一轮内最多升几档（阶梯 3 档本来只够升 2 次，这条把上限显式写死）
DWELL_LADDER_MAX_STEP = 2

#: 2026-11-04 真机事故（手机1 02:08「去淘宝特价版领红包(0/1)」被连做 4 轮才放弃）的修 2：
#: 同一行在同一次运行里被**真正点下去**的次数硬上限 —— 升档重试 / jump 立刻重试 / 普通失败
#: 全都算在内（记在 ctx.try_count，点击**之前**就 +1，不是失败之后）。
#: 到上限就 give_up[key] = 2（pick_target 要求 give_up < 2 才会再选这一行）并打一行 [cap]。
#: 为什么会需要这条：旧代码升档成功的那两轮「不动放弃额度」，于是 0->1、1->2 都不扣分，
#: 到顶才 +1，再一轮 +1 才被排除 —— 一行能被点 4 次，而其中前 3 次加起来毫无进展。
MAX_ROW_TRIES = 3

#: 修 3（同日事故）：落地判定落在这些**还在淘宝里**的值上 = 这一点击原地没动（没跳出淘宝）。
#: 只对行名点名了外部 App（card["ext_pkg"] 非空）的行生效；站内「点击去逛」类靠停留计时，
#: 必须维持现有升档逻辑（误伤即等于把它们全部改成不重试）。
DWELL_NO_LEAVE_LANDINGS = ("taobao", "taobao-search", "panel")

#: 滑动是否真的动了（p4/drv3.py dwell_swipe 的 moved 判据）
MIN_MOVE_PIX = 3.0

#: PNG 文件头（快速抓帧通道用它一眼判「adb 回来的确实是 PNG，而不是报错文本/空串」）
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: 自适应停留（用户 2026-11-02 现场反馈）：浏览任务不点进商品页，停在页面上时间就在走，
#: 甚至不用滑。所以落地后先不动，隔 DWELL_PROBE_S 取两帧看计时/进度有没有在变：
#:   在变 -> 只等待（省掉 10~17 秒，也少了误触）；没变 -> 才滑动，滑动中每 DWELL_PROBE_EVERY_S
#:   复查一次，一变就停下来改等待。dwell_plan 给的 +4/+3 秒余量已覆盖「预留 2 秒」。
DWELL_PROBE_S = 2.5            # 落地后先不动，隔这么久取第二帧
DWELL_PROBE_EVERY_S = 3.0      # 滑动期间每这么久复查一次
DWELL_PROBE_BAND = (0, 2400)   # 只 OCR 上半屏找计时/进度，采样不再等一次全屏 OCR
DWELL_PROBE_SCALE = 0.4

#: 搜索前置（_archive/v2/archetypes/prelude.py + logs/r2_search3.py）
#: 2026-11-04 真机（手机2）：搜索结果页**明明加载成功了**，但那个版式里没有「综合/销量/筛选」那排筛选栏，
#: 只剩价格 ￥ -> markers 只有 1 个 -> 被判 search-not-submitted（当天 0 成功 / 8 次误判）。
#: 所以补上结果页特有的词，并允许「价格条数 >= 3」作为等价证据（商品网格就是价格堆出来的）。
RESULT_MARKS = ("综合", "销量", "筛选", "商品", "旗舰店", "￥", "已售",
                "包邮", "官方立减", "金币已抵", "月销", "加购", "好评")
MIN_RESULT_MARKS = 2          # 只读到 1 个可能是巧合
MIN_PRICE_TOKENS = 3          # 价格条数 >= 3 = 商品网格出来了（可替代 markers 计数）
SEARCH_BOX_HINTS = ("搜索", "搜一搜")
SEARCH_BOX_YMIN = 100
SEARCH_BOX_YMAX = 700         # 搜索框只在顶部那一条，往下的是 chip 区不是输入框
SEARCH_BOX_XMAX = 1100
#: 商品类安全热词（logs/r2_search3.py SAFE）：优先选它们，避开「居家/数码」这类栏目名
SEARCH_SAFE_CHIP_WORDS = ("面霜", "精华", "手机", "电脑", "鞋", "包", "零食", "咖啡", "牛奶",
                          "茶", "洗发水", "香水", "口红", "面膜", "羽绒服", "连衣裙", "卫衣",
                          "裤", "外套", "袜", "帽", "箱", "灯", "椅", "杯", "锅", "米", "油",
                          "纸巾", "牙刷", "猫粮", "狗粮", "玩具", "手表", "耳机", "充电", "键盘")

#: 弹窗探针只看对话框那一条带（p4/x.py band()），且点前就在的静态文字不算弹窗
#: （logs/r2_corr212.py：round-212 把支付宝页面静态文字「开通」当成弹窗，误判失败）
POPUP_BAND = (1000, 2100)
POPUP_PROBE_WORDS = ("领取", "立即", "授权", "开通", "同意", "确定", "取消", "去支付",
                     "签约", "下载", "安装", "允许")

#: 挡住去路的浮层：允许点「把它关掉」的文案。
#: 来源：用户 2026-11-02 批准；已知案例：解锁新车轮弹窗 —— 那个弹窗**不能用 BACK**
#: （BACK 会掉回首页），必须点「确认选择」。这类文案只关浮层，不产生任何交易或授权。
OVERLAY_CLOSE_WORDS = ("确认选择", "我知道了", "取消", "以后再说", "暂不", "关闭", "×")
#: 短词必须**整串相等**才算（否则「取消订单」这类长文案会被当成关闭键）
OVERLAY_CLOSE_EXACT = ("确认选择", "取消")
#: 屏上出现这些字 = 可能有浮层挡路（先试点批准的关闭文案，读不到才退 BACK）
OVERLAY_BLOCK_WORDS = ("确认选择",)

#: 用户显式授权（2026-11-02 现场要求）：
#:   「那个如果选了始终打开后面就不会跳了，就是直接到软件了」
#: 系统「打开其他应用」确认框有两枚按钮：仅打开一次 / 始终打开。选了「始终打开」之后
#: 系统就不再弹这个框，任务也就能直达目标 App。面板通过 coin11/targets/jump_policy.json
#: 告诉我们用户的选择：{"mode": "once" | "always"}；文件不存在时默认 once（更保守）。
JUMP_POLICY_PATH = os.path.join(TARGETS_DIR, "jump_policy.json")
JUMP_MARKS = ("打开其他应用", "即将跳转", "将在外部应用打开", "打开外部应用",
              "仅打开一次", "始终打开")
JUMP_BTN = {"once": ("仅打开一次",), "always": ("始终打开",)}

#: 同一个弹窗连续判到几次就熔断（2026-11-02 用户批准的修法）：
#: 首页双11 大 banner 会被 find_popup_card 当成弹层 → 关掉→BACK→又回首页→再判弹层，
#: 一直循环到预算耗尽。连续 3 次同指纹就当「认不出入口」，落盘请人教，不再烧预算。
POPUP_BREAKER_LIMIT = 3

#: 看门狗加强（_archive/v2/core/guard.py）
DIALOG_PKG_GLOBS = ("com.vivo.*security",)
#: 整句命中即停（单条就足够判危险）
DIALOG_TEXT_STRONG = ("已了解应用的风险检测", "风险检测结果", "安全守护", "权限管理",
                      "风险应用", "高风险应用")
#: 成对命中才停（单独一个「允许」在正常页面里太常见，会误停整轮）
DIALOG_TEXT_PAIRS = (("允许", "安装"), ("允许", "权限"), ("允许", "打开"), ("允许", "访问"))


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class GuardError(RuntimeError):
    """R1 命中：按钮 x 越界。不点，并让整个 run 停下。"""


class StopRun(RuntimeError):
    """需要人来看一眼就停：系统弹窗、唤醒失败、面板找不回。"""


class NeedTeaching(StopRun):
    """这一步我不会：截图 + pending json 已经落盘，整轮停手等人教（退出码 3）。"""

    def __init__(self, msg, pending=None):
        super().__init__(msg)
        self.pending = pending or {}


# --------------------------------------------------------------------------- #
# 纯逻辑（无设备、无 OCR 也能跑，--selftest 全靠这一层）
# --------------------------------------------------------------------------- #
def norm(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def center_x(row) -> int:
    return (int(row["x"]) + int(row["x2"])) // 2


def text_of(rows, sep="|") -> str:
    return sep.join(str(r.get("t", "")) for r in rows)


#: 2026-11-04 事故的第二个根因：**无声丢行**。
#: 「酒店超抵日至高5%(0/1) 浏览5秒 +20」的末端按钮「看一看」不在 ACTION_LABELS 里 ->
#: parse_rows 认不出这一行 -> 整行直接消失，日志里一个字都没有，用户和我们都无从发现。
#: 下面两个东西只干一件事：把「像一行、末端文案却不在词表」的行**报一次**，判定一个字不改。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
#: 本次运行里已经报过的「末端文案」（同一个文案最多报一次，避免一屏刷几十行）。
_UNKNOWN_BTN_SEEN = set()


def _report_unknown_btn(name, btn):
    """打一行 [unknown-btn] 证据；同一个文案每次运行只打一次。返回打出来的行（没打= None）。

    只打日志、**不参与任何判定**：未识别的末端文案照旧不做这一行。
    走 stderr：这一行带 OCR 出来的汉字，GBK 控制台下 stderr 已被 reconfigure(errors="replace")
    兜住（见文件头的注释），符号全部用 ASCII（= -> [] ''）。
    """
    btn = norm(btn)
    if not btn or btn in _UNKNOWN_BTN_SEEN:
        return None
    _UNKNOWN_BTN_SEEN.add(btn)
    line = ("[unknown-btn] 行='%s' 末端文案='%s' -> 不在按钮词表，整行跳过"
            "(把它加进 ACTION_LABELS 即可支持)" % (str(name or "").strip()[:40], btn))
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    return line


def unknown_end_buttons(rows, ypad_top=80):
    """找「看起来像一行、末端文案却不在 ACTION_LABELS / TAP_LABELS」的按钮（**只用于打日志**）。

    判据（保守，宁可少报也不瞎报）：
      ① 在面板右半边（center_x > 900，和 ACTION_LABELS 认锚点用的是同一条线）；
      ② 文案 2~8 个字、含汉字（过滤掉「+50」「」「12345」这类非按钮文字）；
      ③ 不在 ACTION_LABELS / TAP_LABELS / DONE_LABELS，也不是奖励芯片；
      ④ 它上方 ypad_top 像素内、左半边（center_x < 1100）有行名文字 —— 即它确实挂在一行上。
    返回 [(行名, 末端文案), ...]（同一文案只保留第一条）。
    """
    out, seen = [], set()
    for r in (rows or []):
        lab = norm(r.get("t"))
        if not (2 <= len(lab) <= 8) or not _CJK_RE.search(lab):
            continue
        if center_x(r) <= 900:
            continue
        if lab in ACTION_LABELS or lab in TAP_LABELS or lab in DONE_LABELS:
            continue
        if REWARD_CHIP_RE.match(lab):
            continue
        by = int(r.get("y", 0))
        blk = [x for x in (rows or [])
               if by - ypad_top <= int(x.get("y", 0)) <= by - 15
               and center_x(x) < 1100 and str(x.get("t") or "").strip()]
        if not blk or lab in seen:
            continue
        seen.add(lab)
        out.append((" ".join(str(x.get("t")) for x in blk), lab))
    return out


def parse_rows(rows, ypad_top=80, ypad_bot=100, enrich=True):
    """把一屏 OCR 盒切成「行」。

    一行的锚点是行尾的动作（去完成 / 去逛逛 / 逛一逛 / 去搜索 / +50 芯片），
    锚点往上 ypad_top 像素是标题区（行名），往下 ypad_bot 是副标题区（浏览15秒 / 点击去逛）。
    """
    acts = []
    for r in rows:
        lab = norm(r.get("t"))
        if not lab or center_x(r) <= 900:
            continue
        if lab in ACTION_LABELS:
            acts.append(dict(r, button=lab, action="button"))
        elif REWARD_CHIP_RE.match(lab) and int(r.get("x", 0)) > 1100:
            # 同一行末端也可能只是「+50」这样的奖励芯片（round-130 点它同样进任务）
            acts.append(dict(r, button=lab, action="chip"))
    acts.sort(key=lambda r: int(r["y"]))

    cards = []
    for i, b in enumerate(acts):
        top = int(b["y"]) - ypad_top
        if i > 0:
            top = max(top, int(acts[i - 1]["y"]) + 40)
        blk = [r for r in rows if top <= int(r["y"]) <= int(b["y"]) + ypad_bot]
        name = " ".join(str(r["t"]) for r in blk
                        if int(r["y"]) <= int(b["y"]) - 15 and center_x(r) < 1100)
        sub = " ".join(str(r["t"]) for r in blk if int(r["y"]) > int(b["y"]) + 10)
        raw = "|".join(str(r["t"]) for r in blk)
        m = CPAT.search(raw) or CPAT_LOOSE.search(raw)
        n, mm = (int(m.group(1)), int(m.group(2))) if m else (None, None)
        card = {
            "y": int(b["y"]), "btn_y": int(b["y"]), "btn_x": center_x(b),
            "button": b["button"], "action": b["action"],
            "name": name[:160], "sub": sub[:200], "raw": raw[:400],
            "n": n, "m": mm,
            "done": b["button"] in DONE_LABELS or "已完成" in raw or "已参加" in raw,
            "doable": False, "kind": "unknown", "secs": None, "secs_raw": None, "why": "",
            "hits": [], "ext": [], "key": "",
        }
        card["key"] = row_key(card)
        cards.append(card)
    # 2026-11-04（无声丢行）：这一屏里有没有「像一行、末端按钮却不在词表」的行？
    # 有就报一条 [unknown-btn]（同一文案一次运行只报一条）。**只报证据，判定一个字不改**：
    # 这些行照旧一行都不产出、一个都不点 —— 想支持它们就把末端文案加进 ACTION_LABELS / TAP_LABELS。
    # 2026-11-04 用户批准恢复：搜索结果页/商品页也会被 OCR 出一堆「像一行、末端不是按钮」的东西
    # （真机噪音：'CUKTEOISSN' 末端文案='酷态科'、'宽4cm长6m' 末端文案='艺术漆'）。
    # 只在「这一帧确实像面板」（含面板标题词）时才报，免得把日志刷满。
    _un_txt = text_of(rows)
    _un_panel_like = any(k in _un_txt
                         for _m in PANEL_MARKERS.values() for k in _m)
    if _un_panel_like:
        for _un_name, _un_btn in unknown_end_buttons(rows, ypad_top):
            _report_unknown_btn(_un_name, _un_btn)
    if enrich:
        for c in cards:
            classify_card(c)
    return cards


def row_key(card) -> str:
    """行的稳定身份：去掉括号/标点/数字（计数 n/m 会变，不能进 key）。"""
    base = re.sub(r"[\s（）()【】\[\]#·、，,。.:：/\\!！?？+＋\d]+", "", str(card.get("name", "")))
    return base[:12] or norm(card.get("name"))[:12]


def secs_from_text(txt):
    """从行文字里读「浏览N秒 / N秒」。2026-11-02 用户要求（第 2 条）：扫描阶段就用它定死时长。"""
    t = str(txt or "")
    m = re.search(r"浏览\s*(\d{1,3})\s*秒", t) or re.search(r"(\d{1,3})\s*秒", t)
    return int(m.group(1)) if m else None


#: 「外部 App 白名单」的安装状态缓存：一次 pm list packages 拉全量（约 0.4s），进程内复用。
#: 语义：None = 还没查成功过（下次调用再查）；非空集合 = 查成功过的全量包名。
#: **空集合绝不是合法值**：查询抛异常或一个包都没返回时保持 None，不许把空集合缓存整轮
#: （旧写法缓存空集合 -> 之后所有外部 App 行都被判「未安装」而静默跳过。真机证据：
#:  2026-11-03 18:25 手机1 日志报「外部 App 未安装（百度，com.baidu.searchbox）」，
#:  而今天 pm 实测这个包是装着的）。
_INSTALLED_PKGS = None


#: 修 6（2026-11-04 用户真机拍板）：一个 App 名可能对应**一族包名**，装其中任意一个都能完成任务。
#: 真机证据：行 `去快手打卡领手机(0/1)` 里写的 com.smile.gifmaker 手机1 **没装**（pm 查不到），
#: 但 com.kuaishou.nebula（快手极速版）装着 —— 用户手动做这条**做成了**，原话：
#: 「虽然没有下载快手本体，但是快手极速版的跳转也算」。
#: 保守边界：只有**确实知道是同一族**的才登记；其余 App 一律单包名。
#: 特别是「百度」和「百度极速版」是**两个不同的行名**（正式版 com.baidu.searchbox /
#: 极速版 com.baidu.searchbox.lite），**不许**合并；淘宝特价版也保持单包名。
#: 表里第一项 = 主包名（任务文案上写的那个，仍进 VISIT_OK_APPS / 证据 ext_pkg），
#: 其余只是同族包名，**只**参与「装没装」的判定。
EXT_APP_FAMILY = {
    # 手机1（10.241.163.217:5555）pm 实测：com.smile.gifmaker 没有、com.kuaishou.nebula 有。
    "快手": ("com.smile.gifmaker", "com.kuaishou.nebula"),
}


def card_ext_pkgs(card):
    """这一行要查的**全部**包名（修 6）：同族 App 返回整族，其余就是 ext_pkg 那一个。

    只按 App 名查同族表 —— 表外的行（含支付宝那套 ALIPAY_PKG）行为与改动前**一模一样**。
    """
    fam = EXT_APP_FAMILY.get(str((card or {}).get("ext_visit") or ""))
    if fam:
        return tuple(fam)
    return (str((card or {}).get("ext_pkg") or ""),)


def _pkg_list(pkg):
    """把「单个包名 / 同族包名集合」统一成非空包名列表（修 6 的入参兼容层）。"""
    if isinstance(pkg, (list, tuple, set, frozenset)):
        items = [str(p).strip() for p in pkg]
    else:
        items = [str(pkg or "").strip()]
    return [p for p in items if p]


def ext_app_hit(dev, pkg, app=""):
    """在 pm 清单里**第一个命中**的包名（None = 一个都没装 / 这次查询失败）。

    pkg 可以是单个包名，也可以是同族包名集合（修 6）：任一命中即算已装。
    查询失败/为空时**本次按未安装处理且不写缓存**（保守方向：只少做，绝不因此多点），
    下一次调用会自然重试 —— 一次卡壳不再毁掉整轮（这条既有修复不许退化）。
    """
    global _INSTALLED_PKGS
    pkgs = _pkg_list(pkg)
    if not pkgs:
        return None
    if _INSTALLED_PKGS is None:
        installed = set()
        try:
            out = dev.shell("pm list packages") or ""
            installed = set(l.split(":", 1)[1].strip()
                            for l in out.splitlines() if l.startswith("package:"))
        except Exception:
            installed = set()
        if not installed:
            # ASCII 一行，走 stderr：GBK 控制台也绝不会 UnicodeEncodeError
            print("[ext] pm list packages failed/empty -> treat as NOT installed this time, "
                  "not cached, will retry (pkg=%s)" % ",".join(pkgs),
                  file=sys.stderr, flush=True)
            return None
        _INSTALLED_PKGS = installed
    hit = next((p for p in pkgs if p in _INSTALLED_PKGS), None)
    if hit and len(pkgs) > 1:
        # 同族命中（例如任务写的是快手本体、实际装的是极速版）：一行 ASCII 符号的日志
        print("[ext] %s 已装 <- %s (同族, %s)"
              % (app or "?", hit, ",".join(pkgs)), file=sys.stderr, flush=True)
    return hit


def ext_app_installed(dev, pkg):
    """这台机器上装了没？装了就允许「点进去逛一下」，没装就别浪费时间（也绝不安装）。

    签名不变（单个包名）；修 6 起也接受同族包名集合，任一命中即 True。
    """
    return ext_app_hit(dev, pkg) is not None


def _skip(card, kind, why):
    card["kind"] = kind
    card["doable"] = False
    card["why"] = why
    return card


#: ---- 修 3（2026-11-04 用户拍板）：领取型**入口**按钮的极小例外 ----
#: 真机行：`去闲鱼币领现金红包(0/1)` 副标题 `点击去逛 +20` 末端按钮 `立即领`。
#: 现行 R5 口径（领取类绝不点）把它拦掉了 —— 那是修越权口子后的正确行为，但这一行的语义其实是
#: 「去闲鱼逛一下」，「立即领」只是入口（真领取发生在目标 App 里，我们进去后**零点击**）。
#: 用户只放开这一种组合：**副标题写明「点击去逛」+ 行名点名了 EXT_APP_PKGS 里的外部 App**。
CLAIM_ENTRY_WORD = "点击去逛"
#: 修 3 允许忽略的黑名单命中：全部来自「领取型入口按钮」本身（立即领/去领取/领取奖励 以及行名里
#: 同义的「领取/领现金」营销词），外加 ① 通道本来就容忍的「点击去逛」。**别的黑名单词一个都不豁免**
#: （下载/安装/签到/抢/分享/拉人/免费拿…命中即照旧拦）。
CLAIM_ENTRY_TOLERATE = ("去领取", "立即领", "领取奖励", "领取", "领现金", CLAIM_ENTRY_WORD)
CLAIM_ENTRY_AUTH = RELAX_USER_AUTH
#: 修 5（2026-11-04 用户拍板）：副标题含「点击去逛」的行，**只**忽略 DANGER_HARD 里的这两个词
#: （真机行 `看淘宝视频领红包(0/1)`：这里的「视频」只是任务名，进去逛一下、零点击）。
#: 「听课」「直播以外的视频类」等一个都没跟着放宽 —— 表里其它词照旧全部照拦。
CLAIM_ENTRY_IGNORE_WORDS = ("视频", "直播")


def claim_entry_gate(card, txt, ext_ok=None, danger=None):
    """修 3 的放行闸（纯函数，离线/--selftest 可直接断言）：返回 (ok, why)。

    四条**同时**满足才放行，少一条就一个字都不放宽（照旧不点）：
      ① 副标题含「点击去逛」；
      ② 行名点名 EXT_APP_PKGS 里的外部 App（放行后必须带 ext_pkg 交给 one_round 的
         ext_app_installed 闸：没装 -> ext-missing，一个点击都不发）；
      ③ 末端是**领取型按钮**（在 ACTION_LABELS 里但**不在** TAP_LABELS 里，且不是奖励芯片）；
      ④ 没有硬危险词（DANGER_HARD 全量），且除 CLAIM_ENTRY_TOLERATE 以外没有别的黑名单命中。
    放行的效果只有一个：**允许点这一枚入口按钮**；进去之后仍然零点击（沿用跳转/停留流程）。
    """
    sub = norm(card.get("sub"))
    btn = norm(card.get("button"))
    ext_ok = list(ext_ok if ext_ok is not None
                  else [e for e in (card.get("ext") or []) if e in VISIT_OK_APPS])
    danger = list(danger if danger is not None else danger_hits(txt))
    if CLAIM_ENTRY_WORD not in sub:
        return False, "副标题没有「%s」" % CLAIM_ENTRY_WORD
    if not ext_ok:
        return False, "行名没点名表内的外部 App（拿不到 ext_pkg，交不了安装闸）"
    if not (btn in ACTION_LABELS and btn not in TAP_LABELS) or card.get("action") == "chip":
        return False, "末端不是领取型入口按钮：%s" % (btn or "-")
    if danger:
        return False, "有硬危险词：%s" % "/".join(danger)
    rest = [h for h in (card.get("hits") or []) if h not in CLAIM_ENTRY_TOLERATE]
    if rest:
        return False, "还有别的黑名单命中：%s" % "/".join(rest)
    return True, ""


#: ---- 修 4（2026-11-04 用户拍板）：副标题写「跳转得」的跳转类任务 ----
#: 真机行：`去抽最高1万淘金币(0/1)` 副标题 `跳转得 +400` 按钮 `去完成`。
#: 过去 BROWSE_HINTS 一个都不命中、也没写「浏览N秒」-> 落到 not-browse 被**无声丢掉**。
#: 用户口径：副标题含「跳转」就按跳转类做 —— 落地后**静止停留、不滑动**，秒数走 jump 阶梯
#: （DWELL_LADDER_JUMP_S = 0 -> 5 -> 8），到点 BACK 回面板，进去之后**零点击**。
JUMP_HINT_WORD = "跳转"


def jump_hint_row(card, danger=None):
    """修 4 的判据闸（纯函数，离线可直接断言）：这一行是不是「跳转得」类跳转任务。

    四条同时满足：
      ① **副标题**含「跳转」（行名里出现的不算：那多半是「不要跳转」这类文案，不是任务类型）；
      ② 没有任何黑名单命中；③ 没有硬危险词（DANGER_HARD 全量）；
      ④ 末端是浏览类动作（TAP_LABELS）或奖励芯片，且不是已完成/已领取。
    """
    sub = norm(card.get("sub"))
    danger = list(danger if danger is not None else danger_hits(
        "%s %s" % (card.get("name") or "", sub)))
    if JUMP_HINT_WORD not in sub:
        return False
    if card.get("hits"):
        return False
    if danger:
        return False
    if card.get("button") in DONE_LABELS:
        return False
    if not (card.get("button") in TAP_LABELS or card.get("action") == "chip"):
        return False
    return True


def _classify_card(card):
    """行分类：可做（浏览 / 搜一搜 / 支付宝浏览）vs 跳过（并给出**具体原因**）。"""
    name = str(card.get("name", ""))
    sub = str(card.get("sub", ""))
    txt = name + " " + sub
    raw = str(card.get("raw", ""))
    # 源 _archive/root_py/run_energy.py SKIP_RE=支付(?!宝)：单独的「支付」不能吃掉「支付宝」行
    card["hits"] = black_hits(txt, raw)
    card["ext"] = [w for w in EXT_APPS
                   if w in txt or w in raw or (w.isascii() and w.lower() in txt.lower())]
    # secs_raw = 副标题里真的读到的秒数（None=读不到 -> 本轮去查同族缓存 secs_cache）
    card["secs_raw"] = secs_from_text(txt)
    card["secs"] = card["secs_raw"]

    if card.get("done"):
        return _skip(card, "done", "已完成 / 已参加（行已结束）")
    # ---- 2026-11-03 用户**明确授权**（交接文档「五、待办」第 6 条）：任务到访得金币(0/1) ----
    # 授权范围**严格限定**在这一行：① 行名/副标题命中 CLAIM_ROW_WORDS（「到访得金币」）；
    # ② 末端按钮**恰好**是「领取奖励」（不是「去领取/立即领/领取」等同族按钮，也不是奖励芯片）；
    # ③ 除「领取」这个**来自被授权按钮本身**的黑名单词以外，没有任何别的黑名单词；
    # ④ 没有硬危险词（邀请/助力/下单/买/支付/授权/答题/理财/视频…），也不在支付宝家族里。
    # 四条**全部**满足才放行；少一条就一个字都不放宽，照旧走后面的黑名单判定。
    # 第 7 条（顶部累计奖励那一排）不在这里判 —— 它由 claim_top_candidates 在整帧层面认。
    _claim_rest = [h for h in card["hits"] if h != "领取"]
    if (card["action"] != "chip"
            and card["button"] == CLAIM_ROW_BUTTON
            and any(w in txt for w in CLAIM_ROW_WORDS)
            and not _claim_rest
            and not danger_hits(txt)
            and not any(k in txt for k in ALIPAY_HINT)):
        if card["m"] and card["n"] is not None and card["n"] >= card["m"]:
            return _skip(card, "quota-full", "额度已满 %d/%d" % (card["n"], card["m"]))
        card["kind"] = CLAIM_KIND
        card["doable"] = True
        card["why"] = ""
        card["hits"] = []
        card["secs"] = None                    # 领取不靠停留计时（点完就判到账）
        card["secs_raw"] = None
        card["claim_auth"] = CLAIM_USER_AUTH
        card["claim_item"] = "第6条"
        card["claim_slot"] = "row_button"
        card["claim_reason"] = (
            "用户%s授权（交接文档五、待办第6条）：行名命中「%s」且末端按钮恰好是「%s」"
            "-> 只点这一行这一枚" % (CLAIM_USER_AUTH, "/".join(CLAIM_ROW_WORDS), CLAIM_ROW_BUTTON))
        return card
    # ---- 2026-11-03 用户授权（交接文档「五、待办」第 3 条）：点击商品领优惠红包(0/1) ----
    # 行级精确放宽：必须**同时**命中「点击商品」和「点击N个商品」，并且除「点击商品」以外
    # 没有任何黑名单词、没有硬危险词、末端是浏览类按钮 —— 否则一个字都不放宽，照旧走后面的
    # 黑名单判定（BLACK_WORDS 里「点击商品」对其他行的含义完全没动）。
    _pc = PRODUCT_CLICK_RE.search(txt)
    _pc_rest = [h for h in card["hits"] if h != PRODUCT_CLICK_WORD]
    if (PRODUCT_CLICK_WORD in txt and _pc and not _pc_rest
            and not danger_hits(txt)
            and (card["button"] in TAP_LABELS or card["action"] == "chip")):
        if card["m"] and card["n"] is not None and card["n"] >= card["m"]:
            return _skip(card, "quota-full", "额度已满 %d/%d" % (card["n"], card["m"]))
        card["need"] = max(1, int(_pc.group(1)))     # 副标题写的「点击N个商品」
        card["kind"] = "product_click"
        card["doable"] = True
        card["why"] = ""
        card["hits"] = []                            # 「点击商品」这一条已被用户授权豁免
        card["secs"] = None                          # 这条不靠停留计时
        card["secs_raw"] = None
        card["click_auth"] = PRODUCT_CLICK_AUTH
        card["click_rule"] = ("点行尾按钮 -> 原商品页 -> 固定坐标点 %d 张商品卡"
                              "（每点开只发 BACK，详情页内零点击）" % card["need"])
        return card
    # R3 先于黑名单：授权支付宝行的副标题本来就写着「点击去逛」
    if any(k in name for k in ALIPAY_OK):
        if not (card["button"] in TAP_LABELS or card["action"] == "chip"):
            return _skip(card, "claim", "支付宝授权行但末端是领取类：%s" % card["button"])
        if card["m"] and card["n"] is not None and card["n"] >= card["m"]:
            return _skip(card, "quota-full", "额度已满 %d/%d" % (card["n"], card["m"]))
        card["kind"] = "alipay"
        card["doable"] = True
        card["why"] = ""
        card["secs"] = card["secs"] or DEFAULT_BROWSE_SECS
        return card
    # ---- 2026-11-03 真机：**副标题写明「浏览N秒」的，一律按浏览类处理** ----
    # 用户截图里「去签到领红包(浏览5秒)」「双11寻宝中国抢4999元(浏览5秒)」都被黑名单词误挡。
    # 用户的底线是"别乱点/别付钱"，不是"别出现某个字"。所以：明写浏览秒数 + 无硬危险词 => 可做。
    # 硬危险词（DANGER_HARD：助力/邀请/下单/买/支付/授权/答题/考试/理财/游戏/视频…）仍然优先拦截。
    _is_search = (any(k in name for k in SEARCH_HINTS) or card["button"] == "去搜索")
    # ---- 2026-11-03 用户授权（第 2/4/5 条）：这 5 个词只在「明写浏览N秒 + 纯浏览流程」时放宽 ----
    # _danger_left：除了这 5 个词以外的硬危险词（一个都没放宽，命中即照旧拦）；
    # _relaxed    ：这一行命中的放宽词（放行时留在 card 上当审计标记）。
    # ---- 修 5（2026-11-04 用户拍板）：副标题含「点击去逛」的行，**只忽略「视频」「直播」两个词** ----
    # 真机行：`看淘宝视频领红包(0/1)` 副标题 `点击去逛 +400` 按钮 `去完成` —— 名字里的「视频」
    # 让它在 relax-guard 上被挡掉。用户口径：这种行的「视频」只是任务名（点进去逛一下、零点击），
    # 不像「刷视频领金币」那样要长时间观看，所以**只看这一条占位词**。
    # 严格边界：① 只在**副标题**含「点击去逛」时生效；② 只忽略这两个词 ——
    # 支付/转账/授权/开通/同意/下单/买/折扣/答题/考试/邀请/助力/免费拿/下载/安装/去玩/游戏/
    # 理财/基金/保险… 以及「听课」照旧**全部照拦**，一个都不跟着放宽（DANGER_HARD 一个字没改）。
    _ignore_media = CLAIM_ENTRY_WORD in norm(sub)
    _ignored = [w for w in CLAIM_ENTRY_IGNORE_WORDS if _ignore_media and w in txt]
    _danger = [w for w in danger_hits(txt) if w not in _ignored]
    _relaxed = [w for w in RELAX_BROWSE_WORDS if w in txt and w not in _ignored]
    _danger_left = [w for w in _danger if w not in RELAX_BROWSE_WORDS]
    if _ignored:
        # 审计标记（只做证据，不参与判定）：这一行忽略了哪两个危险词、依据哪条口径
        card["ignore_words"] = list(_ignored)
        card["ignore_reason"] = ("副标题含「%s」-> 按用户 2026-11-04 口径忽略『%s』"
                                 % (CLAIM_ENTRY_WORD, "/".join(CLAIM_ENTRY_IGNORE_WORDS)))
    # 纯浏览流程 = 末端是浏览类动作（去完成/去逛逛/去浏览…）或奖励芯片；领取类按钮点下去就是「领」，不算
    _relax_btn_ok = (card["button"] in TAP_LABELS or card["action"] == "chip")
    # 行名点名的、我们能跳的外部 App（取名字最长的那个：百度极速版 > 百度）。
    # 提前算好，是因为下面「明写浏览N秒」这条通道**同样**要过「装了没」这道闸：
    # 否则「去逛UC送红包福利(浏览5秒)」会绕过 ext_app_installed，未装也照点（跳到安装页）。
    _ext_ok = [e for e in card["ext"] if e in VISIT_OK_APPS]
    # ---- R5 补齐到本通道（2026-11-04 审计，不是新权限）：明写浏览N秒的行，末端如果是
    # **领取类按钮**（立即领 / 去领取 / 领取奖励…），这条通道也绝不点 —— 和上面 ① 通道同一口径。
    # 事故证据：闲鱼行「去闲鱼币领现金红包(0/1)」副标题「浏览5秒 +20」末端「立即领」，曾被本通道
    # 判成 browse/doable 并真的点了那枚「立即领」（用户口径：除第 6/7 条授权外，任何位置的领取类都不点）。
    # 放行仍只认浏览类动作（TAP_LABELS）或奖励芯片（action=="chip"）；领取类不满足 -> 不进本通道，
    # 往下落到既有判定（claim_entry_gate / 黑名单 / 末端领取类闸），一个字都没新放宽。
    # 旧条件末尾的 (_relax_btn_ok or not _relaxed) 已被新增的 _relax_btn_ok 蕴含，原文保留不动。
    # 2026-11-04 用户实测（手机2）：「天天签到免费拿IP周边(0/1)」副标题只写「浏览最新礼盒得」，
    # 不写秒数 -> secs_raw=None -> 旧条件不成立 -> 仍被判黑名单（签到/免费拿），一次都没做。
    # 用户点开确认：这条只要浏览 5 秒，应该做。所以本通道从「明写浏览N秒」扩成
    # 「明写浏览N秒 **或** 副标题含 BROWSE_NO_SECS_HINTS（浏览）」；其余条件（无残留硬危险词、
    # 非搜索、末端是浏览类按钮/芯片）一个字都没放宽。
    _browse_word = [w for w in BROWSE_NO_SECS_HINTS if w in norm(sub)]
    _browse_ok = bool(card["secs_raw"]) or bool(_browse_word)
    if _browse_word and not card["secs_raw"]:
        card["browse_word"] = list(_browse_word)
    if (_browse_ok and not _danger_left and not _is_search
            and _relax_btn_ok and (_relax_btn_ok or not _relaxed)):
        if card["button"] in DONE_LABELS:
            return _skip(card, "done", "已完成（按钮=%s）" % card["button"])
        if card["m"] and card["n"] is not None and card["n"] >= card["m"]:
            return _skip(card, "quota-full", "额度已满 %d/%d" % (card["n"], card["m"]))
        card["kind"] = "browse"
        card["doable"] = True
        card["why"] = ""
        card["hits"] = []
        if _ext_ok:
            # 这一行点名了外部 App（UC / 百度极速版 / 微博…）：交给 one_round 的 ext_app_installed 闸，
            # 没装的在日志里写「外部 App 未安装（…）-> 不点，换下一行」，装了才点（点进去只停留+返回）
            card["ext_visit"] = max(_ext_ok, key=len)
            card["ext_pkg"] = VISIT_OK_APPS[card["ext_visit"]]
        if _relaxed:
            # 审计标记：哪个词被放宽了 + 依据哪条授权 + 依据哪一条规则（写进证据用）
            card["relaxed_words"] = _relaxed
            card["relax_auth"] = RELAX_USER_AUTH
            card["relax_rule"] = "明写浏览%s秒 + 纯浏览流程（不点商品/不点任何按钮）" % card["secs_raw"]
        card["secs"] = card["secs_raw"]
        return card
    # ---- 用户口径 ①：已装 App 的「点进去逛一下就返回」类（含支付宝家族）----
    # ③（DANGER_HARD）优先：只要带助力/邀请/答题/理财/下单/支付这些词，一律落到下面黑名单。
    # 这里用的是**全量** _danger（上面已算好）：① 类外部 App 不做任何词级放宽。
    _visit = any(k in txt for k in VISIT_LIKE)
    # R5 补齐（2026-11-03 审计发现，不是新权限）：末端是**领取类按钮**（领取奖励/立即领/去领取…）
    # 的行，① 类「点进去逛一下」通道也**绝不点** —— 只有浏览类动作（TAP_LABELS）或奖励芯片才放行。
    # 事故证据：闲鱼行「去闲鱼币领现金红包(0/1)」末端是「立即领」，曾被 ① 类通道放行并真的点了
    # （round-010/046/049，tap x≈1258）。用户口径是「除第 6/7 条授权外，任何位置的领取类都不点」，
    # 这里只是把 TAP_LABELS 那条既有口径补齐到 ① 类，词义/白名单一个字都没改。
    _tap_ok = (card["button"] in TAP_LABELS or card["action"] == "chip")
    if not _danger and card["button"] not in DONE_LABELS and _tap_ok:
        app, pkg = None, None
        if _ext_ok:
            # 已装的外部 App（微博/百度/头条/夸克）：行名不一定写「逛逛」，例如「去微博看看热搜」，
            # 所以这里**不要求** VISIT_LIKE —— 只要不带危险词就按「进去逛一下」处理。
            # 取**名字最长**的那个：同一 App 有极速版/正式版两个包名（百度 vs 百度极速版），
            # 取最长才不会把「去百度极速版领现金」错查成正式版包名（会漏拦到安装页）。
            app = max(_ext_ok, key=len)
            pkg = VISIT_OK_APPS[app]
        elif any(k in txt for k in ALIPAY_HINT):
            if _visit:                              # 支付宝家族：只在「逛逛/浏览」类才放
                app, pkg = "支付宝", ALIPAY_PKG
        elif "点击去逛" in txt and not card["ext"]:
            # 淘宝**内部**的「点击去逛」（来闪购逛逛/闪购兑免单卡这类不跳出淘宝的）：
            # 过去被「点击去逛」这个黑名单词误挡。它们是正常浏览任务，按 browse 处理。
            # **必须** not card["ext"]：否则「去快手打卡领手机」这种外部 App 也会被顺带放行。
            card["kind"] = "browse"
            card["doable"] = True
            card["why"] = ""
            card["hits"] = [h for h in card["hits"] if h != "点击去逛"]
            card["secs_raw"] = card["secs_raw"] or JUMP_STAY_S
            card["secs"] = card["secs_raw"]
            return card
        if pkg:
            card["kind"] = "browse"
            card["ext_visit"] = app                # 证据：这次是「跳去某个 App 逛一下」
            card["ext_pkg"] = pkg
            card["doable"] = True
            card["why"] = ""
            card["hits"] = []                      # 挂在这个 App 上的「领现金」不再单独挡
            card["secs_raw"] = card["secs_raw"] or JUMP_STAY_S
            card["secs"] = card["secs_raw"]
            return card
    # ---- 修 4（2026-11-04 用户拍板）：副标题写「跳转得」的任务也是可做行 ----
    # 真机行：去抽最高1万淘金币(0/1) 副标题「跳转得 +400」按钮「去完成」—— BROWSE_HINTS 一个都
    # 不命中、也没写「浏览N秒」-> 过去落到 not-browse 被**无声丢掉**（真机日志里这行一条都没有）。
    # 用户口径：副标题含「跳转」就按跳转类做（落地后静止停留 -> BACK 返回，全程零点击）。
    # **只在副标题里找「跳转」**：行名里出现的不算（避免把「不要跳转」这类文案当任务类型）。
    if jump_hint_row(card, _danger):
        card["kind"] = "browse"        # 沿用现有 kind；「跳转」是停留/落地语义，由 jump 阶梯表达
        card["jump_hint"] = True
        card["doable"] = True
        card["why"] = ""
        card["secs_raw"] = None        # 跳转类的秒数由 jump 阶梯给（0 -> 5 -> 8），不冒充「浏览N秒」
        card["secs"] = dwell_ladder_secs("jump", 0)
        return card
    # ---- 修 3（2026-11-04 用户拍板）：副标题写明「点击去逛」+ 行名点名已装外部 App 的行，
    #      末端是**领取型入口按钮**（立即领 / 去领取 / 领取奖励）也允许点下去 ----
    # 过去被 R5 的「领取类绝不点」拦在这里（真机：去闲鱼币领现金红包(0/1) 点击去逛 +20 立即领）。
    # 用户的判断：这一枚只是**去那个 App 逛一下**的入口 —— 进去之后我们仍然零点击。
    # 只放开这一种组合（四条同时满足见 claim_entry_gate），且放开后**照样**交给 one_round 的
    # ext_app_installed 闸：App 没装 -> ext-missing，一个点击都不发。
    _entry_ok, _entry_why = claim_entry_gate(card, txt, _ext_ok, _danger)
    if _entry_ok:
        if card["m"] and card["n"] is not None and card["n"] >= card["m"]:
            return _skip(card, "quota-full", "额度已满 %d/%d" % (card["n"], card["m"]))
        _entry_app = max(_ext_ok, key=len)
        card["kind"] = "browse"                 # 和 ① 通道里其它外部 App 行完全一致（不带新 kind）
        card["doable"] = True
        card["why"] = ""
        card["hits"] = []                       # 命中的全是入口按钮自带的领取类词（见 CLAIM_ENTRY_TOLERATE）
        card["ext_visit"] = _entry_app
        card["ext_pkg"] = VISIT_OK_APPS[_entry_app]
        card["secs_raw"] = card["secs_raw"] or JUMP_STAY_S
        card["secs"] = card["secs_raw"]
        card["authorized_by_user"] = CLAIM_ENTRY_AUTH
        card["authorized_reason"] = (
            "副标题『%s』+ 行名点名已装外部 App（%s）-> 允许点领取型入口按钮（%s）；"
            "进 App 后零点击" % (CLAIM_ENTRY_WORD, _entry_app, card["button"]))
        return card
    if not _entry_ok and CLAIM_ENTRY_WORD in norm(sub):
        # 副标题点名「点击去逛」却没被这条例外放行 -> 留一句为什么（只加证据，不影响判定）
        card["claim_entry_blocked"] = _entry_why
    if any(k in txt for k in ALIPAY_HINT):
        return _skip(card, "alipay-unapproved",
                     "支付宝/蚂蚁类，但不在授权通道（只认 %s）" % "/".join(ALIPAY_OK))
    # 外部 App 但不是「逛一下」类（例如需要安装/下单）→ 仍然跳过
    if card["ext"] and any(e in VISIT_OK_APPS for e in card["ext"]) and not _visit:
        card["hits"] = card["hits"] or []
    if card["hits"]:
        why = "黑名单词：" + ",".join(card["hits"])
        if card["ext"]:
            why += " / 外部App：" + ",".join(card["ext"])
        return _skip(card, "black", why)
    if card["ext"]:
        return _skip(card, "ext-app", "外部 App / 未装 App：" + ",".join(card["ext"]))
    if card["button"] in DONE_LABELS:
        return _skip(card, "done", "已完成（按钮=%s）" % card["button"])
    if card["button"] not in TAP_LABELS and card["action"] != "chip":
        return _skip(card, "claim", "末端是领取类按钮：%s" % card["button"])
    # 计数只用来判「额度是否做满」；有的行本来就不显示 n/m，不能因为读不到就不做
    if card["m"] and card["n"] is not None and card["n"] >= card["m"]:
        return _skip(card, "quota-full", "额度已满 %d/%d" % (card["n"], card["m"]))
    # ---- 2026-11-03 用户授权：放宽词一旦命中，就**只能**走上面那条「浏览N秒 + 纯浏览」通道 ----
    # 走到这里说明 (a) 没写浏览秒数 或 (b) 不是纯浏览 —— 这 5 个词照旧不点。
    # 必须放在 search/browse 兜底之前：那两处兜底不看硬危险词，「看视频得奖励」这类会被顺带放行。
    if _relaxed:
        return _skip(card, "relax-guard",
                     "命中放宽词 %s，但不是「明写浏览N秒 + 纯浏览流程」"
                     "（秒数=%s 搜索=%s 末端=%s 其余硬危险词=%s）-> 不放宽，照旧不点"
                     % ("/".join(_relaxed), card["secs_raw"], _is_search, card["button"],
                        "/".join(_danger_left) or "-"))
    if any(k in name for k in SEARCH_HINTS) or card["button"] == "去搜索":
        card["kind"] = "search"
        card["doable"] = True
        card["why"] = ""
        card["secs"] = card["secs"] or DEFAULT_BROWSE_SECS
        return card
    if any(k in txt for k in BROWSE_HINTS) or card["secs"]:
        card["kind"] = "browse"
        card["doable"] = True
        card["why"] = ""
        card["secs"] = card["secs"] or DEFAULT_BROWSE_SECS
        return card
    return _skip(card, "not-browse", "既不是浏览也不是搜索（名字/副标题无浏览字样）")


#: D（2026-11-03）：金融类词。用户已放宽口径③：「双11理财任务 浏览5秒这种也可以做」（只看不点）。
#: 这些词同时在 DANGER_HARD 里（拦截优先），所以它们眼下只能靠 BROWSE_HINTS 兜底被放行。
FINANCE_WORDS = ("理财", "基金", "保险", "贷款", "借款", "分期", "信用卡")


def finance_words_in(txt):
    """行名/副标题里的金融类词（D 的审计用，**不参与拦截判定**）。"""
    t = str(txt or "")
    return [w for w in FINANCE_WORDS if w in t]


def classify_card(card):
    """行分类入口：先按原判据分类，**被放行的金融类行**再补一条授权审计痕迹（D）。

    2026-11-03 用户放宽口径③：「双11理财任务 浏览5秒这种也可以做」（只看不点）。
    这类行原本走 BROWSE_HINTS 兜底被放行、不留任何授权痕迹；这里只补标记，
    拦截判定本身一个字都没改（现在能做就继续能做，现在拦的继续拦）。
    """
    card = _classify_card(card)
    words = finance_words_in("%s %s" % (card.get("name") or "", card.get("sub") or ""))
    if words and card.get("doable") and card.get("kind") in ("browse", "search", "alipay"):
        secs = card.get("secs_raw") or card.get("secs")
        card["finance_words"] = words
        card["finance_auth"] = RELAX_USER_AUTH
        card["finance_reason"] = (
            ("明写浏览%s秒的金融类任务" % secs if secs else "浏览类（未写秒数）的金融类任务")
            + "，用户%s授权只看不点（纯浏览：不点商品、不点任何按钮）" % RELAX_USER_AUTH)
    # 修 5（2026-11-04 用户拍板）：副标题含「点击去逛」的行忽略了「视频/直播」两个危险词 ——
    # 只有**真的因此被放行**才留审计痕迹（被别的闸拦下的行不留，免得 grep 时混进来）。
    if card.get("ignore_words") and card.get("doable"):
        card["authorized_by_user"] = card.get("authorized_by_user") or RELAX_USER_AUTH
        _why = ("副标题含「%s」-> 忽略『%s』两词，进去只看不点"
                % (CLAIM_ENTRY_WORD, "/".join(card["ignore_words"])))
        _old = str(card.get("authorized_reason") or "")
        card["authorized_reason"] = (_old + "；" + _why) if _old else _why
    return card


def priority(card) -> int:
    """越小越先做：领取 -> 浏览 -> 搜一搜 -> 支付宝；名字带「红包/领/省钱」的往后放。

    领取（claim_task，用户 2026-11-03 授权）排最前：它**不滑不白停**，点一枚就判到账，
    成本最低且是用户明确要的（面板顶部/行尾的奖励先落袋），不该排在一堆浏览任务后面。
    """
    base = {CLAIM_KIND: 5, "browse": 10, "search": 20, "alipay": 30}.get(card.get("kind"), 99)
    if any(w in str(card.get("name", "")) for w in ("红包", "领", "省钱")):
        base += 5
    return base


def pick_target(cards, give_up=None, prefer=None):
    """挑一个可做行；give_up[key] >= 2 的行本轮放弃（连续两轮无进展）。

    prefer：B（2026-11-03）上一轮「没做完 -> 停留升档」的那一行，优先重做它
    （否则优先级的排序会把它挤到后面，升了档却等不到重试）。
    """
    give_up = give_up or {}
    cands = [c for c in cards if c.get("doable") and give_up.get(c["key"], 0) < 2]
    if prefer:
        for c in cands:
            if c["key"] == prefer:
                return c
    # 同类里先收「已经开始计数」的行（n>0），再按时长短的先做，最后自上而下
    cands.sort(key=lambda c: (priority(c), 0 if (c.get("n") or 0) > 0 else 1,
                              c.get("secs") or DEFAULT_BROWSE_SECS, -int(c["y"])))
    return cands[0] if cands else None


# --------------------------------------------------------------------------- #
# 2026-11-03 用户明确授权（交接文档「五、待办」第 6、7 条）：领取类**唯一例外**
#   第 6 条 = 行名「任务到访得金币」+ 末端「领取奖励」（行级，见 _classify_card）
#   第 7 条 = 面板顶部「今日累计奖励/累计任务奖励」那一排的「立即领取」（整帧级，见下）
#   两条产出同一张「领取卡」（kind=CLAIM_KIND），执行走同一条 run_claim。
#   范围以外的 领取/立即领取/领取奖励 一个都不点。
# --------------------------------------------------------------------------- #
def claim_top_candidates(rows):
    """第 7 条：面板顶部累计奖励**那一排**里可以点的「立即领取」（找不到就返回 []）。

    认定口径**写死**（这是钱，越权就是事故，宁可不点）：
      ① 锚点：文字含「今日累计奖励 / 累计任务奖励」的盒子，且在面板顶部（y <= CLAIM_TOP_MAX_Y）；
      ② 按钮：文字**恰好**等于「立即领取」（精确相等，不做子串匹配）；
      ③ 同一 OCR 带：与锚点同一行 —— |按钮 y - 锚点 y| <= CLAIM_TOP_BAND_DY；
      ④ 在锚点右侧：按钮 x >= 锚点盒子的 x2（那一排是「标题 + 币+N + 立即领取」从左往右排）。
    四条全中才算。返回按 x 升序（先领最小的那一档），每项 {x,y,box,title,title_box,dx,dy}。
    """
    out = []
    for b in (rows or []):
        if norm(b.get("t")) != CLAIM_TOP_BUTTON:
            continue
        by, bx = int(b.get("y", 0)), int(b.get("x", 0))
        best = None
        for a in (rows or []):
            at = str(a.get("t") or "")
            title = next((w for w in CLAIM_TOP_TITLES if w in at), None)
            if not title:
                continue
            ay, ax2 = int(a.get("y", 0)), int(a.get("x2", 0))
            if ay > CLAIM_TOP_MAX_Y:            # 不在顶部 = 不是「那一排」
                continue
            dy = abs(by - ay)
            if dy > CLAIM_TOP_BAND_DY:          # 不在同一 OCR 带
                continue
            if bx < ax2:                        # 在标题左边 = 不是那一排的按钮
                continue
            if best is None or dy < best["dy"]:
                best = {"title": title, "title_box": [int(a.get("x", 0)), ay, ax2],
                        "dy": dy}
        if best is None:
            continue
        out.append(dict(best, x=(bx + int(b.get("x2", bx))) // 2, y=by,
                        box=[bx, by, int(b.get("x2", bx))]))
    out.sort(key=lambda c: int(c["x"]))
    return out


def claim_top_card(rows):
    """第 7 条：把那一排里的「立即领取」装成一张「领取卡」（与第 6 条同 kind / 同执行路径）。

    读不到那一排 -> None（= 面板其他位置的「立即领取」一次都不点）。
    """
    cands = claim_top_candidates(rows)
    if not cands:
        return None
    top = cands[0]
    return {"kind": CLAIM_KIND, "slot": "top_band",
            "x": int(top["x"]), "y": int(top["y"]), "button": CLAIM_TOP_BUTTON,
            "name": str(top.get("title") or ""), "key": "claim-top|%s" % (top.get("title") or ""),
            "item": "第7条", "n": None, "m": None, "candidates": cands,
            "reason": ("用户%s授权（交接文档五、待办第7条）：只点面板顶部「%s」那一排里的"
                       "「立即领取」（同一 OCR 带 |dy|<=%dpx、文字精确相等、且在标题右侧）；"
                       "面板其他位置的「立即领取」照旧绝不点"
                       % (CLAIM_USER_AUTH, top.get("title"), CLAIM_TOP_BAND_DY))}


def claim_row_card(card):
    """第 6 条：把「任务到访得金币 + 领取奖励」这一行装成同一张「领取卡」。"""
    return {"kind": CLAIM_KIND, "slot": "row_button",
            "x": int(card["btn_x"]), "y": int(card["btn_y"]), "button": CLAIM_ROW_BUTTON,
            "name": str(card.get("name") or ""), "key": str(card.get("key") or ""),
            "item": card.get("claim_item") or "第6条", "n": card.get("n"), "m": card.get("m"),
            "candidates": [],
            "reason": card.get("claim_reason") or (
                "用户%s授权（交接文档五、待办第6条）：行名命中「%s」且末端按钮恰好是「%s」"
                "-> 只点这一行这一枚"
                % (CLAIM_USER_AUTH, "/".join(CLAIM_ROW_WORDS), CLAIM_ROW_BUTTON))}


def guard_button_x(bx, label="", y=0):
    """R1：行尾动作的 x 必须落在 [1245,1270]，否则不点并让整个 run 停下。"""
    bx = int(bx)
    if not (BTN_X_MIN <= bx <= BTN_X_MAX):
        raise GuardError("R1 按钮 x 越界：%r x=%d y=%d（必须 %d~%d）→ 不点，停下"
                         % (label, bx, int(y), BTN_X_MIN, BTN_X_MAX))
    return bx


def panel_ok(rows, what):
    """R2：面板在前台 = 有面板标题 + ≥3 行。返回 (bool, info)。

    2026-11-04 真机（手机2）：用户把任务做得差不多之后，面板上的行全都变成 ✓（已完成的图形勾），
    parse_rows 认不出动作按钮 -> rows=2 / buttons=0 -> 旧判据说「不在面板」-> 走恢复阶梯
    -> 重启淘宝后还是满屏勾 -> 恢复阶梯用尽 -> 需要教学停手。其实那就是面板，只是**已经做完了**。
    补一条：标题命中 + 没有任何动作按钮 + 还解析出了至少 2 张卡（奖励芯片之类）= 面板在、但已做完。
    （首页那种真正不在面板的屏 rows 会是 0，不会误判。）
    """
    text = text_of(rows)
    title = any(k in text for k in PANEL_MARKERS[what])
    cards = parse_rows(rows)
    info = {"title": bool(title), "rows": len(cards),
            "buttons": sum(1 for c in cards if c["action"] == "button"),
            "ocr_n": len(rows)}
    if title and info["rows"] >= 3:
        return True, info
    info["all_done"] = bool(title and info["buttons"] == 0 and info["rows"] >= 2)
    return bool(title and (info["rows"] >= 3 or info["all_done"])), info


def progress_in_text(text, what):
    """面板总进度：赛车面板是「当前进度 47/50」。金币面板不靠它（用金币总数）。"""
    if what != "race":
        return None
    m = RACE_PROGRESS_RE.search(text or "")
    return int(m.group(1)) if m else None


def coin_total(rows, y_max=700):
    """金币面板左上角金币总数。OCR 会把「26001」和后面的字粘一起，必须用 search。"""
    vals = []
    for r in rows:
        if int(r.get("y", 0)) > y_max:
            continue
        for m in re.finditer(r"\d{4,7}", str(r.get("t", ""))):
            vals.append(int(m.group(0)))
    return max(vals) if vals else None


def dialog_hit(pkg, act, text):
    """R5/R6：系统安装 / 权限 / 跳转确认框。命中即停，绝不点、绝不关。"""
    p, a, t = str(pkg or "").lower(), str(act or "").lower(), str(text or "")
    hits = []
    for k in DIALOG_PKG:
        if k in p:
            hits.append("package:" + k)
    for k in DIALOG_ACT:
        if k in a:
            hits.append("activity:" + k)
    for k in DIALOG_TEXT:
        if k in t:
            hits.append("text:" + k)
    return hits


def chip_candidates(rows):
    """R4：在「搜索发现 / 历史搜索」区块下方挑热词 chip（绝不选中页面标题）。"""
    rows = sorted(rows, key=lambda r: int(r["y"]))
    anchors = [r for r in rows if any(k in str(r.get("t", "")) for k in SEARCH_BLOCK_LABELS)]
    if not anchors:
        return [], "no-block"
    a = anchors[0]
    ay = int(a["y"])
    out = []
    for r in rows:
        t = str(r.get("t", "")).strip()
        if not (2 <= len(t) <= 8):
            continue
        if any(w in t for w in BAD_CHIP):
            continue
        if not (ay + 40 < int(r["y"]) < ay + 260):
            continue
        if center_x(r) > 1000:
            continue
        out.append({"t": t, "x": center_x(r), "y": int(r["y"])})
    out.sort(key=lambda r: (r["y"], r["x"]))
    return out, ("ok" if out else "no-chip")


def merge_scan_rows(per_screen_cards):
    """把逐屏行表合并成一张完整表（按 key 去重，保留第一次出现的顺序与 y）。"""
    seen, order = {}, []
    for k, cards in enumerate(per_screen_cards):
        for c in cards:
            if c["key"] not in seen:
                seen[c["key"]] = dict(c, screen=k)
                order.append(c["key"])
            else:
                # 后来的屏幕里若读到更明确的计数，补一次（不回退已有读数）
                cur = seen[c["key"]]
                if cur.get("n") is None and c.get("n") is not None:
                    cur["n"], cur["m"] = c["n"], c["m"]
    return [seen[k] for k in order]


# --------------------------------------------------------------------------- #
# 落地分类（源: logs/r2_expand.py classify() + logs/r2_expand_coin.py 复核）
# --------------------------------------------------------------------------- #
def classify_landing(focus_text):
    """点完之后前台落在哪个 App：alipay / tmall / taobao / other。

    为什么必须分四类而不是「是不是淘宝」：落到支付宝/天猫是**同一家的授权通道**，
    只许滑不许点（点了等于替用户同意授权/下单）；落到别的 App 则要立刻退出，
    在别人的 App 里滑动既不计时也可能触发广告。源: logs/r2_expand.py
    """
    f = str(focus_text or "").lower()
    for mode, needles in LANDING_NEEDLES:
        if any(n in f for n in needles):
            return mode
    return "other"


def landing_after(dev, first_focus, settle_s=LANDING_SETTLE_S):
    """第一次读到的前台可能是「还没切走的旧 App」→ settle 后复核，以复核结果为准。

    源: logs/r2_expand_coin.py（没有这次复核时，跳到天猫的那一瞬会被读成 taobao，
    于是脚本在落地页上白滑了整段时长）。返回 {"mode","first_mode","rechecked",...}。
    """
    first = classify_landing(first_focus)
    try:
        time.sleep(max(0.0, float(settle_s)))
        again = str(dev.focus() or "")
    except Exception:
        again = ""
    mode = classify_landing(again) if again else first
    return {"mode": mode, "first_mode": first, "first_focus": str(first_focus or ""),
            "focus_after_settle": again, "rechecked": mode != first}


# --------------------------------------------------------------------------- #
# 槽位判据（源: _archive/v2/percept/racing_rows.py）
#   实测：任务名与计数不绑定，计数属于**槽位**。按名字匹配会同时判出假进展与假倒退
#   （好物随心逛 8/10 与 搜一搜你心仪的宝贝 0/10 会互相串号）。因此判据一律按位置。
# --------------------------------------------------------------------------- #
def row_pitch(cards):
    """相邻行的纵向间距（中位数）；行数 < 2 返回 None。"""
    ys = sorted({int(c["y"]) for c in cards})
    if len(ys) < 2:
        return None
    gaps = sorted(b - a for a, b in zip(ys, ys[1:]))
    return gaps[len(gaps) // 2]


def match_tolerance(cards, floor=None, ratio=None):
    """槽位匹配阈值：行距 * ratio，不小于 floor（阈值必须小于行距，否则会命中两行）。"""
    floor = SLOT_TOL_FLOOR_PX if floor is None else int(floor)
    ratio = SLOT_TOL_PITCH_RATIO if ratio is None else float(ratio)
    pitch = row_pitch(cards)
    if pitch is None:
        return int(floor)
    return max(int(floor), int(pitch * ratio))


def capture_slot(cards, index):
    """点之前：按行序记下目标槽位（几何 + 当时 (n,m) + 当时整盘行数）。越界返回 None。"""
    if not (0 <= int(index) < len(cards)):
        return None
    c = cards[int(index)]
    return {"slot_index": int(index), "x": int(c["btn_x"]), "y": int(c["y"]),
            "count": [c["n"], c["m"]] if c["n"] is not None else None,
            "label": str(c["name"]), "key": c["key"], "panel_rows": len(cards)}


def find_slot(anchor, cards, tolerance=None):
    """按槽位规则重新定位 anchor 那一行。

    规则（顺序）：中心点距离 ≤ 阈值取最近 → 都不在阈值内则退化为行序（页面滚动/换分辨率）
    → 阈值内多于一个候选 = 阈值配错，ambiguous 不判 → 行序越界 = 槽位丢失，不猜。
    """
    if not cards:
        return None
    tol = match_tolerance(cards) if tolerance is None else int(tolerance)
    ax, ay = int(anchor["x"]), int(anchor["y"])
    within = []
    for i, c in enumerate(cards):
        dx, dy = int(c["btn_x"]) - ax, int(c["y"]) - ay
        dist = (dx * dx + dy * dy) ** 0.5
        if dist <= tol:
            within.append((dist, abs(dy), i, c))
    if within:
        within.sort(key=lambda t: (t[0], t[2]))
        dist, ady, i, c = within[0]
        return {"card": c, "method": "bounds", "distance": round(dist, 1),
                "dy": int(ady), "candidates_within": len(within), "slot_index": i,
                "tolerance": tol}
    idx = int(anchor["slot_index"])
    if 0 <= idx < len(cards):
        c = cards[idx]
        dx, dy = int(c["btn_x"]) - ax, int(c["y"]) - ay
        return {"card": c, "method": "index",
                "distance": round((dx * dx + dy * dy) ** 0.5, 1), "dy": int(abs(dy)),
                "candidates_within": 0, "slot_index": idx, "tolerance": tol}
    return None


def slot_readability(anchor, cards_after, panel_title_ok=None, expected_rows=0):
    """面板本身读得到吗；读不到返回原因（空串 = 可读）。

    这道闸必须排在「行消失」之前：白屏 / 面板没打开 / 整盘读漏，与「一行做完被抽走」
    的外观完全一样，不设闸就是把读失败判成成功。源: racing_rows._readability
    """
    n = len(cards_after)
    tail = "；槽位丢失，但不判行消失"
    if n == 0:
        return "面板读不到：整屏一条带动作的行都没有（白屏/面板未打开/不在面板）" + tail
    if n < SLOT_MIN_PANEL_ROWS:
        return "面板只读到 %d 行（<%d），疑似白屏或面板没打开" % (n, SLOT_MIN_PANEL_ROWS) + tail
    if expected_rows > 0:
        floor = int(-((-int(expected_rows) * SLOT_PANEL_ROW_FLOOR) // 1))  # ceil
        if n < floor:
            return "面板只读到 %d 行，远少于正常 %d 行，疑似整盘没读到" % (n, expected_rows) + tail
    if int(anchor.get("panel_rows") or 0) > 0 and n < int(anchor["panel_rows"]) - 1:
        return ("整盘行数 %s -> %d：掉的不止一行，不是「一行完成」而是面板没读到"
                % (anchor["panel_rows"], n) + tail)
    if panel_title_ok is False:
        return "面板标题读不到：疑似不在面板或被遮挡" + tail
    return ""


def _same_task_alive(anchor, cards_after):
    """点前那条任务还在盘上、且计数没退吗（用来否掉「其实没消失」）。"""
    n0 = (anchor.get("count") or [None])[0]
    out = []
    for c in cards_after:
        if c["key"] != anchor["key"]:
            continue
        if n0 is None or c["n"] is None or int(c["n"]) >= int(n0):
            out.append(c)
    return out


def _completion_reasons(anchor, cards_after, text_after="", progress_delta=None,
                        coin_delta=None):
    """「这行消失是因为做完了」的辅助证据，逐条列出。源: racing_rows._completion_reasons

    任何一条都只是辅助（单条都有反例），但四条全空就是「说不清」，那时不判消失。
    """
    reasons = []
    cnt = anchor.get("count")
    if cnt and cnt[0] is not None and cnt[1]:
        if int(cnt[0]) == int(cnt[1]) - 1:
            reasons.append("count_was_last(点前 %d/%d，再推进一次就满)" % (cnt[0], cnt[1]))
    # 2026-11-04 用户口径（原话）：「如果他完成了，就算消失了那个槽位的计数也会增加，
    # 没增加就没有完成，只是任务消失了，不管怎么样就是没完成」。
    # 所以 label_absent **不能**当作「做完了」的证据：行都没了，label 当然读不到 ——
    # 那是同义反复（循环论证）。旧代码靠这一条就能把「任务被换掉/刷走」判成「做完了」，
    # 真机实例：搜一搜你心仪的宝贝(3/5) 整行消失、计数没到 4/5、金币 d=0，却被判 disappeared=完成。
    # 现在只把事实记进 evidence，**不进 reasons**：判「消失=完成」必须有一条独立证据
    # （计数已是最后一张 / 整盘少一行 / 面板进度涨 / 金币涨）。四条都空 -> 判 skipped，不判完成。
    label = str(anchor.get("label") or "")
    if label and not (label in str(text_after or "")
                      or any(c["key"] == anchor["key"] for c in cards_after)):
        pass    # label_absent：只说明「看不到这一行了」，不构成完成证据（见上面的注释）
    if int(anchor.get("panel_rows") or 0) > 0 and \
            len(cards_after) == int(anchor["panel_rows"]) - 1:
        reasons.append("panel_total_dropped(%s->%d)" % (anchor["panel_rows"], len(cards_after)))
    if (progress_delta or 0) > 0:
        reasons.append("panel_progress_up(+%s)" % progress_delta)
    if (coin_delta or 0) > 0:
        reasons.append("coin_total_up(+%s)" % coin_delta)
    return reasons


def _slot_disappear(anchor, cards_after, text_after, ev, progress_delta=None,
                      coin_delta=None):
    """行消失分支：先确认「同一任务真的不在了」，再要一条「这行做完了」的辅助证据。

    顺序不能反：读不到面板的闸在 slot_verdict 里已经过完了，这里只剩两件事 ——
    同一任务还在不在、消失是不是因为做完。源: racing_rows._judge_disappeared
    """
    alive = _same_task_alive(anchor, cards_after)
    if alive:
        ev["verdict"] = "failed"
        return {"verdict": "failed", "row": alive[0], "evidence": ev,
                "reason": "同一任务仍在面板（%s/%s），计数没推进：不是行消失"
                          % (alive[0]["n"], alive[0]["m"])}
    if not anchor.get("count"):
        ev["verdict"] = "skipped"
        return {"verdict": "skipped", "row": None, "evidence": ev,
                "reason": "点前没读到计数：分不清是做完消失还是根本没读到，不判"}
    reasons = _completion_reasons(anchor, cards_after, text_after, progress_delta,
                                  coin_delta)
    ev["disappear_reasons"] = reasons
    if not reasons:
        ev["verdict"] = "skipped"
        return {"verdict": "skipped", "row": None, "evidence": ev,
                "reason": "槽位 #%s 已不是同一任务，但没有一条「这行做完了」的辅助证据"
                          "（n 未到 m-1 / 名称仍在盘上 / 整盘没少行 / 总额没涨）：不判"
                          % anchor.get("slot_index")}
    ev["verdict"] = "disappeared"
    return {"verdict": "disappeared", "row": None, "evidence": ev,
            "reason": "槽位 #%s 的行 %r 已从面板消失（点前 %s/%s），理由：%s"
                      % (anchor.get("slot_index"), str(anchor.get("label"))[:20],
                         anchor["count"][0], anchor["count"][1], "、".join(reasons))}


def slot_verdict(anchor, cards_after, text_after="", panel_title_ok=None,
                 expected_rows=0, progress_delta=None, coin_delta=None):
    """槽位判据：按槽位重新定位同一行，判「计数推进 / 整行消失 / 说不清」。

    verdict 只有四种：
        advanced     计数推进（成功）
        disappeared  这一行消失，且有「做完了」的辅助证据（成功）
        skipped      读不到面板 / 匹配不唯一 / 说不清 → 不猜，不算成功
        failed       同一任务还在、计数没推进（真失败）
    两条口径（实测，别再用错）：
      1) (n,m) 只在**同一轮** before/after 之间比，跨轮不可比（名字与计数会整体重排）；
      2) m 变了（10<->5）说明盘面重排了，completed 一律降级为「说不清」。
    """
    ev = {"slot": anchor.get("slot_index"), "before_label": str(anchor.get("label"))[:40],
          "before": list(anchor["count"]) if anchor.get("count") else None,
          "anchor_y": anchor.get("y"), "anchor_panel_rows": anchor.get("panel_rows"),
          "panel_rows_after": len(cards_after), "title_checked": panel_title_ok is not None}
    blocked = slot_readability(anchor, cards_after, panel_title_ok, expected_rows)
    if blocked:
        ev["verdict"] = "skipped"
        return {"verdict": "skipped", "reason": blocked, "row": None, "evidence": ev}

    match = find_slot(anchor, cards_after)
    if match is None:
        return _slot_disappear(anchor, cards_after, text_after, ev, progress_delta,
                               coin_delta)

    row = match["card"]
    ev.update({"method": match["method"], "distance": match["distance"], "dy": match["dy"],
               "candidates_within": match["candidates_within"],
               "after_label": str(row["name"])[:40], "after": [row["n"], row["m"]],
               "name_changed": row["key"] != anchor.get("key")})
    if match["candidates_within"] > 1:
        ev["verdict"] = "skipped"
        return {"verdict": "skipped", "row": row, "evidence": ev,
                "reason": "阈值 %dpx 内命中 %d 行，无法确定是同一槽位：不判"
                          % (match["tolerance"], match["candidates_within"])}
    if not anchor.get("count"):
        ev["verdict"] = "skipped"
        return {"verdict": "skipped", "row": row, "evidence": ev,
                "reason": "点前没读到计数：无判据"}
    n0, m0 = anchor["count"]
    n1, m1 = row["n"], row["m"]
    if n1 is None:
        ev["verdict"] = "skipped"
        return {"verdict": "skipped", "row": row, "evidence": ev,
                "reason": "槽位对上了，但点后读不到计数：不判"}
    ev["advanced"] = int(n1) > int(n0)
    # 总数 m 本身会变（10 <-> 5）：只能记录，不能当判据；变了就把 completed 降级为 UNKNOWN
    ev["total_changed"] = (m0 is not None and m1 is not None and int(m0) != int(m1))
    if ev["total_changed"]:
        ev["completed"] = None
        ev["completed_note"] = ("总数 m %s->%s 变了：(n,m) 跨轮不可比，"
                                "completed 降级为 UNKNOWN" % (m0, m1))
    else:
        ev["completed"] = bool(m1 and int(n1) >= int(m1))
    delta = int(n1) - int(n0)
    ev["delta"] = delta
    if delta > 0:
        ev["verdict"] = "advanced"
        return {"verdict": "advanced", "row": row, "evidence": ev,
                "reason": "槽位 #%s 计数 %s/%s -> %s/%s（+%d）"
                          % (anchor.get("slot_index"), n0, m0, n1, m1, delta)}
    if delta < 0:
        # 同一任务的 n 不会回退：这一槽位已经被另一条任务占了 → 交给消失判据
        ev["slot_regressed"] = True
        return _slot_disappear(anchor, cards_after, text_after, ev, progress_delta,
                               coin_delta)
    label_gone = (row["key"] != anchor.get("key")
                  and str(anchor.get("label") or "") not in str(text_after or "")
                  and not any(c["key"] == anchor.get("key") for c in cards_after))
    if label_gone:
        # 计数持平但同坐标换了任务：可能整行消失后被同样计数的行顶上
        ev["slot_reassigned"] = True
        return _slot_disappear(anchor, cards_after, text_after, ev, progress_delta,
                               coin_delta)
    ev["verdict"] = "failed"
    return {"verdict": "failed", "row": row, "evidence": ev,
            "reason": "槽位 #%s 的同一行还在（%s/%s -> %s/%s），计数没推进"
                      % (anchor.get("slot_index"), n0, m0, n1, m1)}


# --------------------------------------------------------------------------- #
# 视觉双证（源: _archive/v2/percept/vision.py）
#   树/OCR 给位置，像素判身份。整页 8x8 灰度指纹 + 平均 RGB，0~1 可解释分数。
#   「同一页面」的像素证据与 OCR 面板标记同时成立，才认「已经回到面板」。
# --------------------------------------------------------------------------- #
def region_signature(path, box=PANEL_BOX, size=None):
    """一块矩形 → 可 JSON 化的签名：8x8 灰度缩略图 + 平均 RGB + 亮度方差。"""
    import numpy as np
    from PIL import Image
    size = tuple(size or VISION_THUMB)
    im = Image.open(path).convert("RGB")
    W, H = im.size
    l, t, r, b = [int(v) for v in box]
    l, t = max(0, min(W - 1, l)), max(0, min(H - 1, t))
    r, b = max(l + 1, min(W, r)), max(t + 1, min(H, b))
    crop = im.crop((l, t, r, b))
    g = np.asarray(crop.convert("L").resize(size, Image.BILINEAR),
                   dtype=np.float64).ravel() / 255.0
    grid = np.asarray(crop.convert("L").resize(VISION_GRID, Image.BILINEAR),
                      dtype=np.float64).ravel() / 255.0
    rgb = np.asarray(crop, dtype=np.float64).reshape(-1, 3).mean(axis=0)
    return {"thumb": [round(float(v), 4) for v in g],
            "grid": [round(float(v), 2) for v in grid],
            "mean_rgb": [round(float(v), 2) for v in rgb],
            "luma_var": round(float(np.var(g * 255.0)), 2),
            "bounds": [l, t, r, b], "thumb_size": [int(size[0]), int(size[1])]}


def signature_similarity(sig_a, sig_b):
    """两个签名的相似度（0~1）：0.6*结构 + 0.4*颜色，每一分都能解释。"""
    import numpy as np
    a = np.asarray(sig_a["thumb"], dtype=np.float64)
    b = np.asarray(sig_b["thumb"], dtype=np.float64)
    sa, sb = float(a.std()), float(b.std())
    if sa < 1e-6 and sb < 1e-6:
        corr = 1.0 - min(1.0, abs(float(a.mean()) - float(b.mean())))
    elif sa < 1e-6 or sb < 1e-6:
        corr = 0.0                     # 一边纯色：结构无从比较，交给颜色分
    else:
        corr = float(np.corrcoef(a, b)[0, 1])
    corr_score = max(0.0, min(1.0, (corr + 1.0) / 2.0))
    l1 = 1.0 - float(np.abs(a - b).mean())
    struct = 0.5 * corr_score + 0.5 * l1
    dist = float(np.linalg.norm(np.asarray(sig_a["mean_rgb"], dtype=np.float64)
                                - np.asarray(sig_b["mean_rgb"], dtype=np.float64)))
    color = max(0.0, 1.0 - dist / VISION_COLOR_MAX)
    glob = VISION_W_STRUCT * struct + VISION_W_COLOR * color
    # 细网格：坏格子占比把「半屏被浮层盖住」这类差异放大出来
    ga = np.asarray(sig_a.get("grid") or [], dtype=np.float64)
    gb = np.asarray(sig_b.get("grid") or [], dtype=np.float64)
    if ga.size and ga.size == gb.size:
        cell = np.abs(ga - gb)
        grid_l1 = 1.0 - float(cell.mean())
        bad_ratio = float((cell > VISION_BAD_CELL).mean())
    else:
        grid_l1, bad_ratio = 1.0, 0.0          # 老签名没有细网格：退回只比全局
    score = (VISION_W_GLOBAL * glob + VISION_W_GRID * grid_l1
             + VISION_W_BAD * (1.0 - bad_ratio))
    return {"score": round(max(0.0, min(1.0, score)), 4), "corr": round(corr, 4),
            "thumb_l1": round(l1, 4), "struct": round(struct, 4), "color": round(color, 4),
            "global": round(glob, 4), "grid_l1": round(grid_l1, 4),
            "bad_ratio": round(bad_ratio, 4), "rgb_dist": round(dist, 2)}


def page_similarity(path_a, path_b, box=PANEL_BOX, min_score=PAGE_MIN_SCORE):
    """两张整页截图是不是同一页。任一张读不到都算不通过（查不到 ≠ 安全）。"""
    try:
        a = region_signature(path_a, box)
        b = region_signature(path_b, box)
    except Exception as e:
        return {"ok": False, "score": None,
                "reason": "截图读不到，无法做视觉校验: %s: %s" % (type(e).__name__, e)}
    d = signature_similarity(a, b)
    d["ok"] = bool(d["score"] >= float(min_score)
                   and (d.get("bad_ratio") or 0) < VISION_LOST_BAD)
    d["min_score"] = float(min_score)
    d["luma_var"] = [a["luma_var"], b["luma_var"]]
    return d


def page_lost(detail):
    """这次比对算不算「压根不是同一页」。

    两个条件任一成立就算：整页分低于 PAGE_LOST_SCORE，或者坏格子占比 ≥ VISION_LOST_BAD
    （后者才是关键 —— 半屏被浮层盖住时整页分还有 0.58，光看分数会当它是同一页）。
    读不到也算（查不到 ≠ 安全）。
    """
    if not detail or detail.get("score") is None:
        return True
    return bool(detail["score"] < PAGE_LOST_SCORE
                or (detail.get("bad_ratio") or 0) >= VISION_LOST_BAD)


# --------------------------------------------------------------------------- #
# 弹窗探针：只看对话框那一条带，且点前就在的静态文字不算
#   （源: p4/x.py band() + logs/r2_corr212.py 的假阳性教训）
# --------------------------------------------------------------------------- #
def ocr_band(dev, path, y0, y1, x0=None, x1=None, scale=None):
    """只 OCR 一条带（坐标已换算回原图）；比全屏快，也不会把带外的静态文字当成弹窗词。"""
    from PIL import Image
    im = Image.open(path)
    x0 = 0 if x0 is None else int(x0)
    x1 = im.width if x1 is None else int(x1)
    rows = ocr_image(im.crop((x0, int(y0), x1, int(y1))),
                     scale=OCR_SCALE if scale is None else scale)
    for r in rows:
        r["y"] += int(y0)
        r["x"] += x0
        r["x2"] += x0
    return rows


def words_in_band(rows, words, band=POPUP_BAND):
    """这批 OCR 盒里，落在对话框带内的那几个词（点前基线，用来豁免静态文字）。"""
    lo, hi = int(band[0]), int(band[1])
    out = []
    for w in words:
        for r in rows:
            y = int(r.get("y", 0))
            if lo <= y <= hi and w in str(r.get("t", "")):
                out.append(w)
                break
    return out


def popup_probe(path, baseline_words=(), band=POPUP_BAND, words=None):
    """对话框带内的**新出现**弹窗词；点前就在的页面静态文字进 static_excluded，不算弹窗。"""
    words = tuple(words or POPUP_PROBE_WORDS)
    base = set(baseline_words or ())
    rows = ocr_band(None, path, band[0], band[1])
    txt = text_of(rows)
    fresh = [w for w in words if w in txt and w not in base]
    static = [w for w in words if w in txt and w in base]
    return {"words": fresh, "static_excluded": static, "band": [int(band[0]), int(band[1])],
            "ocr_n": len(rows), "text": txt[:200]}


# --------------------------------------------------------------------------- #
# 停留模式（源: _archive/root_py/run_energy.py plan() / do_energy.py act()）
#   手机1 实测：浏览N秒必须持续滑动计时才走（静止等于白等）；玩N秒静止就计时。
#   原来一律「连滑 N+1 秒」，于是「玩N秒」类任务被滑走页面，计时反而不走。
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 同族任务的浏览秒数缓存（2026-11-02 用户要求 第 3 条）
#   "很多任务是相似的，例如有 0/5 这种轮次的浏览时间都是一样的，这样点击进去就直接滑动"
#   进程内 dict + 落盘 state/secs_cache.json。两级 key：
#     精确 key = 种类 | 计数 n/m | 名称族；
#     族级 key = @family|种类|名称族（@family 前缀保证永远不会和精确 key 撞车）。
#   2026-11-03 真机证据：rounds 037/038/039/041/046 全是 secs_source=default（dwell 18.2~18.75s），
#   而 round-036 走 sub 只有 16.01s —— 因为精确 key 带 n/m，「好物随心逛(4/5)」查不到
#   自己家族「好物随心逛(0/5)」学到的 15 秒。所以精确 key 未命中必须回退族级 key。
# --------------------------------------------------------------------------- #
_SECS_CACHE = None        # 精确 key -> 秒数
_SECS_FAMILY = None       # 族级 key -> {"vals": {秒数: {"count": 次数, "last": 写入时刻}}}
#: B（2026-11-03）：停留阶梯学到的高档位。key 与秒数缓存同一套：
#:   精确 key = kind|n/m|名称族；族级 key = @family|kind|名称族。
#: value = {"level": 档位下标, "cat": jump/visit/browse/play, "at": 写入时刻}
_SECS_LEVEL = None
#: --selftest 里关掉落盘：离线自测不往 state/ 里塞任何东西（否则会污染第一次真机跑）
_SECS_CACHE_PERSIST = True
#: 一个名称族最多留几个候选秒数（超了丢「次数最少、写入最旧」的，防大表无限长大）
_SECS_FAMILY_MAX_VALS = 6


def name_family(name):
    """名称族：去掉括号里的轮次 (n/m) 与计数后剩下的名字前缀（相似任务共用一份时长）。"""
    base = re.sub(r"[（(][^）)]*[）)]", " ", str(name or ""))
    base = re.sub(r"\d+\s*/\s*\d+", " ", base)
    return norm(base)[:12]


def secs_cache_key(card):
    return "%s|%s/%s|%s" % (card.get("kind") or "unknown", card.get("n"), card.get("m"),
                            name_family(card.get("name")))


def secs_family_key(card):
    """族级 key：只带种类 + 名称族，不带 n/m（同族任意轮次共用）。

    名称族为空（名字全被剔掉，真机 state 里就有这种行）时返回空串 = 不参与族级缓存：
    否则所有无名行会共用同一个空族，那是「异族串号」而不是复用。
    """
    fam = name_family(card.get("name"))
    return "@family|%s|%s" % (card.get("kind") or "unknown", fam) if fam else ""


def secs_cache_path():
    return os.path.join(STATE_DIR, "secs_cache.json")


def _secs_cache_load():
    """第一次用时从 state/secs_cache.json 读一次（读不到就当空表，老文件只有 secs 也能读）。"""
    global _SECS_CACHE, _SECS_FAMILY, _SECS_LEVEL
    if _SECS_CACHE is not None and _SECS_FAMILY is not None and _SECS_LEVEL is not None:
        return
    if _SECS_CACHE is None:
        _SECS_CACHE = {}
    if _SECS_FAMILY is None:
        _SECS_FAMILY = {}
    if _SECS_LEVEL is None:
        _SECS_LEVEL = {}
    try:
        d = jload(secs_cache_path()) or {}
    except Exception:
        return
    # B：阶梯档位（老文件没有这一段，读不到就当全部第 0 档）
    for k, rec in (d.get("level") or {}).items():
        if isinstance(rec, dict) and str(rec.get("level", "")).lstrip("-").isdigit():
            _SECS_LEVEL[str(k)] = {"level": int(rec["level"]),
                                   "cat": str(rec.get("cat") or ""),
                                   "at": str(rec.get("at") or "")}
    for k, v in (d.get("secs") or {}).items():
        if str(v).isdigit():
            _SECS_CACHE[str(k)] = int(v)
    for k, rec in (d.get("family") or {}).items():
        vals = {}
        for vs, vr in ((rec or {}).get("vals") or {}).items():
            if str(vs).isdigit():
                vals[str(vs)] = {"count": int((vr or {}).get("count") or 1),
                                 "last": str((vr or {}).get("last") or "")}
        if vals:
            _SECS_FAMILY[str(k)] = {"vals": vals}
    # 旧落盘文件只有精确 key（真机 state/secs_cache.json 就是这种）：
    # 按「种类|n/m|名称族」反推族级 key 补一份，升级后第一次跑就能吃到同族已学到的秒数。
    for k, secs in list(_SECS_CACHE.items()):
        parts = str(k).split("|", 2)
        if len(parts) != 3:
            continue
        if not parts[2]:                  # 老数据里名称为空的行（如 browse|None/None|）不参与族级
            continue
        fam = "@family|%s|%s" % (parts[0], parts[2])
        vals = dict((_SECS_FAMILY.get(fam) or {}).get("vals") or {})
        if str(secs) in vals or len(vals) >= _SECS_FAMILY_MAX_VALS:
            continue
        vals[str(secs)] = {"count": 1, "last": ""}
        _SECS_FAMILY[fam] = {"vals": vals}


def secs_cache_all():
    """精确 key 的进程内表（兼容旧调用）。"""
    _secs_cache_load()
    return _SECS_CACHE


def secs_family_all():
    _secs_cache_load()
    return _SECS_FAMILY


def _secs_family_pick(vals):
    """族内有多个秒数候选时取「出现次数最多」的，并列取「最近写入」的；返回 (秒数, 记录)。"""
    best = None
    for secs, rec in (vals or {}).items():
        k = (int(rec.get("count") or 0), str(rec.get("last") or ""))
        if best is None or k > best[0]:
            best = (k, int(secs), rec)
    return (best[1], best[2]) if best else (None, None)


def secs_cache_get(card):
    """只查精确 key（旧行为）。"""
    return secs_cache_all().get(secs_cache_key(card))


def secs_cache_lookup(card):
    """先查精确 key，未命中回退族级 key。返回 (秒数, 来源, 命中的 key)。

    来源 cache = 精确 key 命中；cache_family = 族级回退命中（2026-11-03 用户要求）；
    None = 两级都没命中。命中的 key 一并返回，证据里能看出用的是哪一级。
    """
    exact = secs_cache_key(card)
    hit = secs_cache_all().get(exact)
    if hit:
        return hit, "cache", exact
    fam = secs_family_key(card)
    if fam:
        secs, _rec = _secs_family_pick((secs_family_all().get(fam) or {}).get("vals"))
        if secs:
            return secs, "cache_family", fam
    return None, None, exact


def secs_cache_put(card, secs):
    """写秒数缓存（2026-11-03：两台并行会同时改这个文件，锁内重载再合并，避免互相覆盖）。"""
    with _FileLock("secs_cache"):
        try:
            _secs_cache_load()        # 锁内重新读盘：拿到另一台刚写进去的记录
        except Exception:             # noqa: BLE001
            pass
        return _secs_cache_put_nolock(card, secs)


def _secs_cache_put_nolock(card, secs):
    """把这一行真读到的秒数**同时**记进精确 key 与族级 key 并落盘（写不进去也绝不打断主流程）。

    族级 key 必须一起写，否则「好物随心逛(0/5) 学到 15 秒、下一轮 (4/5) 查不到」的老问题还在。
    """
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    cache = secs_cache_all()
    fam_all = secs_family_all()
    key = secs_cache_key(card)
    fam = secs_family_key(card)
    if fam:                                   # 名称族为空的行不写族级（避免空族串号）
        vals = dict((fam_all.get(fam) or {}).get("vals") or {})
        prev = dict(vals.get(str(secs)) or {})
        vals[str(secs)] = {"count": int(prev.get("count") or 0) + 1, "last": now}
        if len(vals) > _SECS_FAMILY_MAX_VALS:
            dead = sorted(vals, key=lambda s: (int(vals[s].get("count") or 0),
                                               str(vals[s].get("last") or "")))
            for s in dead[:len(vals) - _SECS_FAMILY_MAX_VALS]:
                vals.pop(s, None)
        fam_all[fam] = {"vals": vals}
    cache[key] = secs
    if not _SECS_CACHE_PERSIST:
        return secs
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        jdump(secs_cache_path(), {"v": 3, "updated": now, "secs": cache, "family": fam_all,
                                  "level": secs_level_all()})
    except Exception:
        pass
    return secs


def secs_level_all():
    """阶梯档位表（和秒数缓存同一份文件，key 同一套；老文件里没有就是空表 = 全是第 0 档）。"""
    _secs_cache_load()
    return _SECS_LEVEL


def dwell_level_get(card):
    """B：读这一行（或同族）学到的停留档位。返回 (档位, 来源, 命中的 key)。

    来源 exact = kind|n/m|名称族 命中；family = @family|kind|名称族 回退命中；
    None = 从没学过，第 0 档起步。
    """
    lv = secs_level_all()
    exact = secs_cache_key(card)
    rec = lv.get(exact)
    if rec:
        return int(rec.get("level") or 0), "exact", exact
    fam = secs_family_key(card)
    if fam:
        rec = lv.get(fam)
        if rec:
            return int(rec.get("level") or 0), "family", fam
    return 0, None, exact


def dwell_level_put(card, level, cat=""):
    """B：把学到的高档位落盘（精确 key + 族级 key 一起写，下一轮同族直接从该档起步）。

    到顶不再加：写进来之前先夹到 [0, DWELL_LADDER_TOP]，绝不越界。
    """
    try:
        level = int(level)
    except (TypeError, ValueError):
        return None
    level = dwell_ladder_level(level, cat)   # 按类别夹断（jump 多一档到 10 秒）
    lv = secs_level_all()
    now = datetime.now().isoformat(timespec="seconds")
    keys = [secs_cache_key(card)]
    fam = secs_family_key(card)
    if fam:                                   # 名称族为空的行不写族级（避免空族串号）
        keys.append(fam)
    for k in keys:
        lv[k] = {"level": level, "cat": str(cat or ""), "at": now}
    if not _SECS_CACHE_PERSIST:
        return level
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        jdump(secs_cache_path(), {"v": 3, "updated": now, "secs": secs_cache_all(),
                                  "family": secs_family_all(), "level": lv})
    except Exception:
        pass
    return level


def dwell_level_decay(card, cat="", level=0):
    """S1（用户 2026-11-03 拍板）：任务真的做成**一次**就把档位降一档（最低 0 档）。

    问题：原来的阶梯**只升不降** —— 真机 state/secs_cache.json 里已有 3 个任务族停在最高档
    （level=2，也就是每次都白停 21 秒），一旦某次运气好做成了也永远不下来。
    口径：做成一次 -> 该 key 与 @family 的 level 各 -1（夹到 0，绝不为负）；失败照旧按
    one_round 里的升档逻辑升回去 —— 这就是自我纠偏。
    cat 只认 jump/visit/browse/play 四个真阶梯档（default/force/product-click 不进阶梯，也就不降）。
    返回 {"dwell_ladder_decayed": bool, "from": int, "to": int, "why": str}（直接进 06_verdict.json）。
    """
    from_lv = int(dwell_ladder_level(level, cat))
    out = {"dwell_ladder_decayed": False, "from": from_lv, "to": from_lv, "why": ""}
    if cat not in ("jump", "visit", "browse", "play"):
        out["why"] = "no-ladder（%s 档不使用阶梯）-> 不降档" % (cat or "?")
        return out
    if from_lv <= 0:
        out["why"] = "已经是第 0 档（最低档）-> 没得降"
        return out
    to_lv = max(0, from_lv - 1)
    dwell_level_put(card, to_lv, cat)
    out.update({"dwell_ladder_decayed": True, "to": to_lv,
                "why": "做成了一次 -> 按用户 %s 口径降一档（最低回到 0 档）" % CLAIM_USER_AUTH})
    return out


def set_dwell_margin(v):
    """--dwell-margin：命令行覆盖浏览类停留余量（默认取 DWELL_MARGIN_BROWSE_S）。"""
    global DWELL_MARGIN_BROWSE_S
    DWELL_MARGIN_BROWSE_S = max(0, int(v))
    return DWELL_MARGIN_BROWSE_S


def resolve_dwell_plan(card, force_secs=None):
    """扫描阶段就把这次停留的秒数定死（2026-11-02 用户要求 第 2/3 条）。

    返回 (plan, 来源, 取证明细)，来源 = force（--dwell）/ sub（副标题读到）
    / cache（精确 key 命中）/ cache_family（名称族回退命中）/ default（都没命中，
    走 dwell_plan 的默认档）。解析到秒数就把精确 key 与族级 key 一起写进缓存。
    取证明细里带 secs_cache_key / secs_cache_family_key / secs_cache_hit，
    落进 06_verdict.json 的 picked，事后能看出这一轮命中的是哪一级。
    """
    detail = {"secs_cache_key": secs_cache_key(card),
              "secs_cache_family_key": secs_family_key(card)}
    # B（2026-11-03）：先取这一行/这一族**学到的阶梯档位**，再按档位算秒数。
    # 从没学过 = 第 0 档起步（和以前的行为一致）；学到过高档位就直从那档起步。
    lvl, lsrc, lhit = dwell_level_get(card)
    detail.update({"dwell_ladder_level": int(lvl),
                   "dwell_ladder_src": lsrc or "none",
                   "dwell_ladder_key": lhit})
    raw = card.get("secs_raw")
    if raw is None:
        raw = secs_from_text("%s %s" % (card.get("name") or "", card.get("sub") or ""))
    if force_secs:
        plan = dwell_plan(card.get("sub"), card.get("name"), force_secs=force_secs, level=lvl)
        return (plan, "force", dict(detail, secs_cache_level="none"))
    if raw:
        secs_cache_put(card, raw)
        plan = dwell_plan(card.get("sub"), card.get("name"), level=lvl)
        return (plan, "sub",
                dict(detail, secs_cache_level="write", secs_cache_hit=detail["secs_cache_key"],
                     secs_cache_wrote=int(raw), dwell_ladder_cat=plan.get("cat")))
    cached, src, hit = secs_cache_lookup(card)
    if cached:
        plan = dwell_plan(card.get("sub"), card.get("name"), cache_secs=cached, level=lvl)
        return (plan, src,
                dict(detail, secs_cache_level=("exact" if src == "cache" else "family"),
                     secs_cache_hit=hit, secs_cache_secs=int(cached),
                     dwell_ladder_cat=plan.get("cat")))
    plan = dwell_plan(card.get("sub"), card.get("name"), level=lvl)
    return (plan, "default",
            dict(detail, secs_cache_level="none", dwell_ladder_cat=plan.get("cat")))


def dwell_browse_offsets():
    """浏览N秒阶梯的偏移量：第 0 档跟 DWELL_MARGIN_BROWSE_S 走（--dwell-margin 覆盖的就是它）。"""
    return (int(DWELL_MARGIN_BROWSE_S),) + tuple(DWELL_LADDER_BROWSE_OFFSETS[1:])


def dwell_play_offsets():
    """玩N秒阶梯的偏移量：第 0 档跟 DWELL_MARGIN_PLAY_S 走。"""
    return (int(DWELL_MARGIN_PLAY_S),) + tuple(DWELL_LADDER_PLAY_OFFSETS[1:])


def dwell_ladder_level(level, cat=None):
    """把档位夹进 [0, top]：到顶不再加（B 的硬口径）。

    2026-11-04 用户：好物沉浸看 8 秒还是不够，jump 档多一档到 10 秒。
    所以顶按**该类别自己表长**算；visit/browse/play 仍是 3 档（DWELL_LADDER_TOP）。
    """
    try:
        level = int(level or 0)
    except (TypeError, ValueError):
        level = 0
    top = DWELL_LADDER_TOP
    if cat == "jump":
        top = len(DWELL_LADDER_JUMP_S) - 1
    return max(0, min(level, top))


def dwell_ladder_secs(cat, level, n=None):
    """B：按阶梯表算这一档的秒数（第 0 档 = 起步值，到顶不再加）。

    cat=jump   -> DWELL_LADDER_JUMP_S  (0 -> 5 -> 10)
    cat=visit  -> DWELL_LADDER_VISIT_S (5 -> 8 -> 8)
    cat=browse -> n + 偏移             (N+1 -> N+3 -> N+6)
    cat=play   -> n + 偏移             (N+3 -> N+6 -> N+9)
    """
    lvl = dwell_ladder_level(level, cat)
    if cat == "jump":
        return int(DWELL_LADDER_JUMP_S[min(lvl, len(DWELL_LADDER_JUMP_S) - 1)])
    if cat == "visit":
        return int(DWELL_LADDER_VISIT_S[min(lvl, len(DWELL_LADDER_VISIT_S) - 1)])
    base = int(n) if n else int(DEFAULT_BROWSE_SECS)
    offs = dwell_play_offsets() if cat == "play" else dwell_browse_offsets()
    return int(base) + int(offs[lvl])


def row_never_left_taobao(card, landing, present, row_delta):
    """修 3（2026-11-04 真机事故）：行名点名了外部 App，这一轮却**压根没跳出淘宝**。

    真机证据（手机1 02:08，行「去淘宝特价版领红包(0/1)」）：landing=taobao、行还在、
    计数没推进 —— 这一次点击原地没动，给这一行从 8 秒加到 8 秒毫无意义，只会白烧一轮。

    只认「点名了外部 App」的行（card["ext_pkg"] 非空）：站内「点击去逛」类本来就在淘宝里、
    靠停留计时，必须维持现有升档逻辑（误伤即等于把它们全部改成不重试）。
    """
    if not card.get("ext_pkg"):
        return False
    if str(landing or "") not in DWELL_NO_LEAVE_LANDINGS:
        return False
    return bool(present and (row_delta is None or row_delta <= 0))


def dwell_escalation_decision(card, status, ok, landing, present, row_delta,
                              ladder_cat, ladder_level, steps_used=0, tries=0):
    """B 的升档决策（2026-11-04 从 one_round 抽成纯函数：一个副作用都不做，可离线断言）。

    「没做完」沿用原判据：**行还在（present）且计数没推进（row_delta 为空或 <= 0）**。
    升档之前先看两条 2026-11-04 新增的闸（两个真机事故的根因）：
      修 2：这一行本轮已经被真正点下去几次（tries）。到 MAX_ROW_TRIES 就不再升档 ——
            旧代码「升档成功不动放弃额度」，于是 0->1、1->2 都不扣分，一行能被点 4 次。
      修 3：点名了外部 App 的行这一轮没跳出淘宝（见 row_never_left_taobao）-> 加时治不了
            原地没动，直接按失败计（照样受修 2 的尝试上限约束）。
    其余分支与原来的行为**逐字一致**（ladder-top / max-step / no-ladder / done / 判不出）。

    返回 esc：cat/from/to/max_step/applied/why/steps_used/tries/max_tries，
    applied=True 时另有 steps_used（升完之后的累计档数）与 next_secs（下一轮该停几秒）。
    """
    ladder_cat = str(ladder_cat or "default")
    try:
        ladder_level = int(ladder_level or 0)
    except (TypeError, ValueError):
        ladder_level = 0
    steps_used = max(0, int(steps_used or 0))
    tries = max(0, int(tries or 0))
    esc = {"cat": ladder_cat, "from": ladder_level, "to": ladder_level,
           "max_step": DWELL_LADDER_MAX_STEP, "applied": False, "why": "",
           "steps_used": steps_used, "tries": tries, "max_tries": MAX_ROW_TRIES}
    not_done = bool(not ok and present and (row_delta is None or row_delta <= 0)
                    and str(card.get("kind") or "") != "product_click")
    ladder = ("jump", "visit", "browse", "play")
    if status == "disappeared":
        # 行消失 = 任务已被换人或被吃掉：记 skipped（不重试），绝不给它升档
        esc["why"] = "slot-disappeared"
        esc["retry"] = False
        esc["note"] = ("行消失=任务已被换人或被吃掉 -> 不升档、不重试（B 口径）；"
                       "完成判定仍沿用第 8 条：行消失=做完（status=disappeared / ok=True）")
    elif not_done and ladder_cat in ladder and tries >= MAX_ROW_TRIES:
        esc["capped"] = True
        esc["why"] = ("try-cap（这一行本轮已尝试 %d 次，上限 %d）-> 不升档、不重试"
                      % (tries, MAX_ROW_TRIES))
    elif not_done and ladder_cat in ladder and row_never_left_taobao(card, landing,
                                                                    present, row_delta):
        esc["no_leave"] = True
        esc["why"] = ("no-leave（落地=%s 还在淘宝里 + 行还在 + 计数没推进 -> 这一点击原地没动，"
                      "加时没意义）-> 不升档" % (landing,))
    elif not_done and ladder_cat in ladder:
        _next = dwell_ladder_level(ladder_level + 1, ladder_cat)
        if _next > ladder_level and steps_used < DWELL_LADDER_MAX_STEP:
            esc.update({"to": _next, "applied": True, "steps_used": steps_used + 1,
                        "next_secs": dwell_ladder_secs(ladder_cat, _next, card.get("secs_raw")),
                        "why": "没做完（行还在 / 计数没推进）-> 升一档，下一轮按该档再停一次"})
        else:
            esc["why"] = ("ladder-top（第 %d 档，到顶不再加）" % ladder_level
                          if _next <= ladder_level else
                          "max-step（本轮已升 %d 档，保险丝）" % steps_used)
    elif not_done and ladder_cat not in ladder:
        esc["why"] = ("no-ladder（%s 档：读不到时长的兜底档 / --dwell 强制档）-> 不升档"
                      % ladder_cat)
    elif ok:
        esc["why"] = "done"
    else:
        esc["why"] = "判不出没做完（不在面板 / 读不到计数）-> 不升档"
    return esc


def dwell_plan(sub="", name="", force_secs=None, cache_secs=None, level=0):
    """按行的副标题决定这次落地怎么停留：{"mode": "slide"|"static", "secs", "why", ...}。

    cache_secs：同族任务上次真读到的浏览秒数（2026-11-02 用户要求 第 3 条）——
    本行副标题读不到时长时用它，按「浏览N秒」那一档算。
    level：2026-11-03 用户拍板（B）的阶梯档位，0 = 起步档、1/2 = 没做完升上去的档；
    算出来的秒数进 secs，plan 里同时带 cat/level/why 三个审计字段。
    """
    txt = "%s %s" % (name or "", sub or "")
    lvl = dwell_ladder_level(level)
    if force_secs:
        return {"mode": "slide", "secs": int(force_secs), "cat": "force", "level": lvl,
                "why": "命令行 --dwell 强制滑动"}
    m = DWELL_BROWSE_RE.search(txt)
    if m:
        n = int(m.group(1))
        return {"mode": "slide", "secs": dwell_ladder_secs("browse", lvl, n),
                "cat": "browse", "level": lvl, "base_s": n,
                "why": "浏览N秒：必须持续滑动，计时才走（阶梯 %d 档 = N+%ds）"
                       % (lvl, dwell_browse_offsets()[lvl])}
    m = DWELL_STATIC_RE.search(txt)
    if m:
        n = int(m.group(1))
        return {"mode": "static", "secs": dwell_ladder_secs("play", lvl, n),
                "cat": "play", "level": lvl, "base_s": n,
                "why": "玩/看N秒：静止即计时，滑动会离开页面（阶梯 %d 档 = N+%ds）"
                       % (lvl, dwell_play_offsets()[lvl])}
    # 修 4（2026-11-04 用户拍板）：副标题写「跳转得」的跳转类任务 —— **静止停留、不滑动**，
    # 秒数走 jump 阶梯（DWELL_LADDER_JUMP_S：0 -> 5 -> 8）。放在缓存之前：跳转类的秒数由阶梯说了算，
    # 不能被同族「浏览N秒」的缓存顶掉（否则 0 秒档会被拖成十几秒的白等）。
    if JUMP_HINT_WORD in str(sub or ""):
        jlvl = dwell_ladder_level(level, "jump")   # jump 档比别的类多一档（到 10 秒）
        return {"mode": "static", "secs": dwell_ladder_secs("jump", jlvl), "cat": "jump",
                "level": jlvl,
                "why": "副标题含「跳转」：跳转类一跳就不滑，静止停留（jump 阶梯 %d 档：0->5->10）"
                       % jlvl}
    # 2026-11-04 用户实测：写「浏览」但不写秒数的行（「浏览最新礼盒得」）实际只要 5 秒；
    # 旧逻辑会掉进「读不到时长 -> 默认滑 18 秒」，既慢又容易把页面滑走。
    # **只对命中放宽词的行**按 5 秒，其它「读不到时长」的行行为完全不变。
    if any(k in txt for k in BROWSE_NO_SECS_HINTS) \
            and any(w in txt for w in RELAX_BROWSE_WORDS):
        _s5 = dwell_ladder_secs("browse", lvl, BROWSE_NO_SECS_S)
        return {"mode": "slide", "secs": _s5, "cat": "browse", "level": lvl,
                "base_s": BROWSE_NO_SECS_S,
                "why": "有「浏览」但没写秒数（命中放宽词）-> 按用户实测 %d 秒浏览（阶梯 %d 档 = %d+%ds）"
                       % (BROWSE_NO_SECS_S, lvl, BROWSE_NO_SECS_S, lvl)}
    if cache_secs:
        return {"mode": "slide", "secs": dwell_ladder_secs("browse", lvl, cache_secs),
                "cat": "browse", "level": lvl, "base_s": int(cache_secs),
                "why": "同族缓存：上次这族是浏览 %d 秒，照它滑（阶梯 %d 档）"
                       % (int(cache_secs), lvl)}
    # 2026-11-02 用户真机抓到：手机2 的「好物沉浸看」副标题只写「逛一逛」，而这里的判据只认
    # 「去逛 / 逛逛」—— 于是它掉进 DWELL_DEFAULT_S(18s) 的**滑动**档，既滑走了页面又白等 13 秒。
    # 「逛一逛」进去后有 5 秒计时，所以按静止停留 5 秒处理。
    if any(k in txt for k in ("去逛", "逛逛", "逛一逛", "去逛逛")):
        secs = dwell_ladder_secs("visit", lvl)
        return {"mode": "static", "secs": secs, "cat": "visit", "level": lvl,
                "why": "逛一逛/去逛：进页面即算，静止停留 %ds（阶梯 %d 档：5->8->8）"
                       % (secs, lvl)}
    # 读不到任何时长：兜底滑动档，**不参与阶梯**（这不是「做没做完」的问题，是读不到任务时长）
    return {"mode": "slide", "secs": DWELL_DEFAULT_S, "cat": "default", "level": lvl,
            "why": "读不到时长：按默认滑动"}


def static_dwell(dev, secs, shots_dir, tag, start_ts=None, start_basis=""):
    """静止停留：只等不滑（玩N秒 / 去逛）。证据字段与 browse_dwell 对齐。

    start_ts / start_basis：2026-11-03（A）锚点 = 落地确认那一刻（见 browse_dwell）。
    """
    a = os.path.join(shots_dir, "_static_%s_before.jpg" % tag)
    b = os.path.join(shots_dir, "_static_%s_after.jpg" % tag)
    dev.shot(a)
    t0 = time.time()
    anchor = float(start_ts) if start_ts else t0
    end = anchor + max(0.0, float(secs))
    waited = round(max(0.0, end - t0), 2)
    time.sleep(waited)
    dur = round(time.time() - anchor, 2)
    dev.shot(b)
    return {"mode": "static", "requested_s": round(float(secs), 1), "actual_s": dur,
            "swipes": 0, "pix_delta": pix_diff(a, b), "waited_s": waited,
            "start_basis": start_basis or "dwell_entry", "start_ts": round(anchor, 3),
            "before_png": os.path.basename(a), "after_png": os.path.basename(b)}


def dwell_anchor_ev(dwell, anchor_ts, basis, tap_ms):
    """A（2026-11-03 用户真机观察）：把停留计时的**锚点**写进证据。

    用户原话：「计时启动很慢，刚开始计时1秒就返回了」——目标页自己的倒计时是**落地之后**
    才启动的，所以停留必须从「落地确认」（焦点变到目标 App / 已离开面板）那一刻起算，
    而不是从 tap 起算。这里只加证据字段，不改任何判定：
      start_basis        = landing_focus / landing_px / landing_ocr（锚点是怎么确认的）
      start_ts           = 锚点的绝对时刻（epoch 秒，便于和 tap_ms 对账）
      tap_to_dwell_start_ms = tap -> 开始计时 的毫秒数（06_verdict.json 直接可读）
    """
    out = dict(dwell or {})
    try:
        out["start_basis"] = str(basis or out.get("start_basis") or "unknown")
        out["start_ts"] = round(float(anchor_ts), 3)
        if tap_ms:
            out["tap_to_dwell_start_ms"] = int(round(float(anchor_ts) * 1000)) - int(tap_ms)
    except (TypeError, ValueError):
        out.setdefault("start_basis", str(basis or "unknown"))
    return out


def dwell_signature(rows):
    """一帧里与「停留计时 / 任务进度」有关的读数（自适应停留的判据，可解释、可回放）。"""
    txt = text_of(rows)
    return {"text": txt,
            "nums": ",".join(re.findall(r"\d{1,4}", txt)),
            "countdown": ",".join(re.findall(r"(\d{1,3})\s*秒", txt)),
            "prog": ",".join("%s/%s" % (a, b) for a, b
                             in re.findall(r"(\d{1,3})\s*/\s*(\d{1,3})", txt))}


def _sig_changed(before, after):
    """两帧读数有没有在变：返回变了的字段名列表（空 = 没动）。"""
    if not before or not after:
        return []
    return [k for k in ("nums", "countdown", "prog") if before.get(k) != after.get(k)]


def adaptive_dwell(dev, secs, shots_dir, tag, want="slide", start_ts=None, start_basis=""):
    """自适应停留：先等一会儿看计时是不是自己在走，不走才滑。

    来源：用户 2026-11-02 现场反馈 ——「浏览任务不用点进商品页，在页面滑动就行了，
    进去时间就在减少，可能不滑动都行」。原实现一律连滑 N+1 秒，每轮白花 10~17 秒，
    而且滑动次数越多越容易误触到别的东西。

    判据：只 OCR 上半屏（DWELL_PROBE_BAND），比对倒计时数字 / 进度 n/m / 页面数字集合。
    返回值里的 mode 就是 verdict 记的 dwell_mode：
        wait   一开始就在走 -> 全程只等待，零滑动
        swipe  一直没看到变化 -> 按老办法滑满（滑到底也不变说明这页就是这样）
        mixed  先滑、滑到一半开始走 -> 立刻停止滑动改等待
    want="static"（玩N秒 / 去逛）时根本不许滑，只等待 + 取样取证。
    """
    t0 = time.time()
    need = float(secs)
    # A（2026-11-03）：截止时刻从**落地确认**那一刻起算（start_ts），不是本函数入口。
    anchor = float(start_ts) if start_ts else t0
    deadline = anchor + need
    a = os.path.join(shots_dir, "_dwell_%s_start.jpg" % tag)
    b = os.path.join(shots_dir, "_dwell_%s_end.jpg" % tag)
    dev.shot(a)

    def sample(name):
        p = os.path.join(shots_dir, "_dwell_%s_%s.jpg" % (tag, name))
        try:
            dev.shot(p)
            rows = ocr_band(dev, p, DWELL_PROBE_BAND[0], DWELL_PROBE_BAND[1],
                            scale=DWELL_PROBE_SCALE)
            return p, dwell_signature(rows)
        except Exception as e:            # 采样失败不该打断停留
            return p, {"error": "%s: %s" % (type(e).__name__, e), "nums": "",
                       "countdown": "", "prog": "", "text": ""}

    _, s0 = sample("p0")
    time.sleep(min(DWELL_PROBE_S, max(0.0, deadline - time.time())))
    _, s1 = sample("p1")
    moving = _sig_changed(s0, s1)
    mode, basis = "wait", "落地后两帧读数一致，未检测到计时在走"
    if want == "static":
        mode, basis = "wait", "玩N秒/去逛：静止即计时，全程不滑"
    elif moving:
        mode, basis = "wait", "落地后计时/进度在变（%s），只等待不滑动" % ",".join(moving)
    else:
        mode, basis = ("swipe", "落地后读数没变，改滑动试探；滑动中每 %ss 复查一次"
                              % DWELL_PROBE_EVERY_S)

    now = time.time()
    swipes = 0
    probes = [{"at": 0.0, "sig": s0.get("nums", "")[:120], "ts": t0},
              {"at": round(now - t0, 1), "sig": s1.get("nums", "")[:120], "ts": now}]
    if mode == "wait" and want != "static":
        # 等待中途再取一帧，证明「计时确实在走」而不是我们读错了
        mid_at = t0 + need * 0.6
        while time.time() < min(mid_at, deadline):
            time.sleep(min(0.5, max(0.05, mid_at - time.time())))
        if time.time() < deadline:
            _, sm = sample("p2")
            probes.append({"at": round(time.time() - t0, 1), "sig": sm.get("nums", "")[:120]})
            moving = moving or _sig_changed(s0, sm)
    last_probe = probes[-1]["ts"]
    first_swipe_ms = None
    while time.time() < deadline:
        if mode == "swipe":
            if first_swipe_ms is None:
                first_swipe_ms = int(time.time() * 1000)
            dev.swipe(*SWIPE_DOWN)
            swipes += 1
            time.sleep(0.35)
            if time.time() - last_probe >= DWELL_PROBE_EVERY_S:
                last_probe = time.time()
                _, sn = sample("s%d" % len(probes))
                probes.append({"at": round(last_probe - t0, 1),
                               "sig": sn.get("nums", "")[:120], "ts": last_probe})
                changed = _sig_changed(s0, sn)
                if changed:
                    mode = "mixed"
                    basis = ("滑动中复查到读数开始变化（%s）→ 停止滑动改等待"
                             % ",".join(changed))
        else:
            time.sleep(min(0.5, max(0.05, deadline - time.time())))
    dur = round(time.time() - anchor, 2)
    waited = round(max(0.0, deadline - t0), 2)
    dev.shot(b)
    delta = pix_diff(a, b)
    return {"mode": mode, "basis": basis, "requested_s": round(need, 1), "actual_s": dur,
            "swipes": swipes, "pix_delta": delta, "moved": bool(delta >= MIN_MOVE_PIX),
            "first_swipe_ms": first_swipe_ms,
            "probes": probes, "want": want, "waited_s": waited,
            "start_basis": start_basis or "dwell_entry", "start_ts": round(anchor, 3),
            "before_png": os.path.basename(a), "after_png": os.path.basename(b)}


# --------------------------------------------------------------------------- #
# 搜索前置（源: _archive/v2/archetypes/prelude.py + logs/r2_search3.py）
# --------------------------------------------------------------------------- #
def find_search_box(rows):
    """在顶部那一条里找搜索输入框（占位文字「搜索/搜一搜」），返回 (box, 候选)。

    prelude 的教训：落的是搜索页、不把关键词送进框里，怎么滑都不算完成。
    所以热词 chip 一个都选不出来时，还有「点搜索框 -> 再取 chip」这条路。
    """
    cands = []
    for r in rows:
        t = str(r.get("t", "")).strip()
        if not (2 <= len(t) <= 12):
            continue
        if not any(k in t for k in SEARCH_BOX_HINTS):
            continue
        if not (SEARCH_BOX_YMIN <= int(r.get("y", 0)) <= SEARCH_BOX_YMAX):
            continue
        if center_x(r) > SEARCH_BOX_XMAX:
            continue
        if any(w in t for w in BAD_CHIP):
            continue
        cands.append({"t": t, "x": center_x(r), "y": int(r["y"])})
    cands.sort(key=lambda r: (r["y"], r["x"]))
    return (cands[0] if cands else None), cands


# --------------------------------------------------------------------------- #
# 证据链补写（源: logs/r2_repair.py）
#   进程被 kill 时那一轮只落了 01/02，没有 verdict，报告里就出现「无判定的轮次」。
# --------------------------------------------------------------------------- #
def repair_missing_verdicts(rounds_dir, log=print):
    """给缺少 06_verdict.json 的轮次补一条诚实的 aborted 记录（不伪造成功）。"""
    fixed = []
    if not rounds_dir or not os.path.isdir(rounds_dir):
        return fixed
    for d in sorted(os.listdir(rounds_dir)):
        m = re.match(r"round-(\d+)$", d)
        if not m:
            continue
        dr = os.path.join(rounds_dir, d)
        if not os.path.isdir(dr) or os.path.exists(os.path.join(dr, "06_verdict.json")):
            continue
        files = sorted(os.listdir(dr))
        rec = {"round": int(m.group(1)), "kind": "unknown", "writer": "auto.py/repair",
               "target": "aborted", "status": "aborted", "ok": False, "repaired": True,
               "dir": d, "files": files, "duration_s": 0,
               "reason": "本轮在写 06_verdict.json 之前就断了（进程被杀 / 异常退出，"
                         "没有做任何新动作）；现存证据：" + ",".join(files)}
        jdump(os.path.join(dr, "06_verdict.json"), rec)
        fixed.append(rec["round"])
    if fixed and log:
        log("[repair] 补写 %d 个缺失的 verdict：%s" % (len(fixed), fixed))
    return fixed


# --------------------------------------------------------------------------- #
# 看门狗加强（源: _archive/v2/core/guard.py）：通配包名 + 强/成对关键词 + fail-closed
# --------------------------------------------------------------------------- #
def _glob_match(pattern, text):
    """fnmatch 的最小实现（T11 的 import 白名单里没有 fnmatch，所以用 re 自己写）。"""
    rx = "^" + re.escape(str(pattern)).replace(r"\*", ".*") + "$"
    return re.match(rx, str(text or "").lower()) is not None


def watchdog_hit(pkg, act, text, rows=None):
    """dialog_hit 的加强版：命中即停，绝不点、绝不关。

    1) 包名/Activity 黑名单（含 com.vivo.*security 通配）
    2) 整句命中即停的强关键词
    3) 必须成对出现的弱关键词（单独一个「允许」在正常页面里太常见）
    4) fail-closed：前台读不到、界面也读不到 —— 查不到 ≠ 安全
    """
    hits = list(dialog_hit(pkg, act, text))
    t = str(text or "")
    for k in DIALOG_TEXT_STRONG:
        if k in t:
            hits.append("text-strong:" + k)
    for a, b in DIALOG_TEXT_PAIRS:
        if a in t and b in t:
            hits.append("text-pair:%s+%s" % (a, b))
    for pat in DIALOG_PKG_GLOBS:
        if _glob_match(pat, str(pkg or "").lower()):
            hits.append("package-glob:" + pat)
    if not (pkg or act) and not (rows or t):
        hits.append("fail-closed: 读不到前台也读不到界面（查不到不等于安全）")
    return list(dict.fromkeys(hits))


# --------------------------------------------------------------------------- #
# 跳转确认框 + 导航弹窗熔断
# --------------------------------------------------------------------------- #
def load_jump_policy(path=None):
    """读用户显式授权：跳转确认框点「仅打开一次」还是「始终打开」。

    来源：用户 2026-11-02「那个如果选了始终打开后面就不会跳了，就是直接到软件了」。
    文件格式以 dash.py 实际写出的为准（它是**写方**，本函数只读）：
        {"policy": "once" | "always", "label": ..., "updated_at": ..., "by": "页面",
         "targets_for": ["192.168.1.8:5555", "192.168.1.5:43059"]}
    兼容键名 mode（旧写法）与中文值（始终打开 / 仅打开一次）；读不到或写坏一律按 once，
    这是更保守的一侧（多确认一次，不会替用户做出"以后都别再问"的决定）。
    """
    p = path or JUMP_POLICY_PATH
    try:
        with open(p, encoding="utf8") as f:
            obj = json.load(f) or {}
    except Exception:
        return {"mode": "once", "source": "default(no-file)", "path": p}
    raw = None
    if isinstance(obj, dict):
        for k in ("policy", "mode"):
            if k in obj:
                raw = obj.get(k)
                break
    val = str(raw or "").strip().lower()
    if val in ("always", "始终打开"):
        mode = "always"
    elif val in ("once", "仅打开一次"):
        mode = "once"
    else:
        return {"mode": "once", "source": "default(bad-value=%r)" % val, "path": p,
                "targets_for": obj.get("targets_for") if isinstance(obj, dict) else None}
    return {"mode": mode, "source": "file", "path": p,
            "label": obj.get("label") if isinstance(obj, dict) else None,
            "updated_at": obj.get("updated_at") if isinstance(obj, dict) else None,
            "targets_for": obj.get("targets_for") if isinstance(obj, dict) else None}


def jump_dialog_button(rows, mode):
    """在跳转确认框里找用户选的那枚按钮（once→仅打开一次 / always→始终打开）。"""
    labels = JUMP_BTN.get(str(mode or "once"), JUMP_BTN["once"])
    hits = []
    for r in rows:
        t = str(r.get("t", "")).strip()
        if not t or not any(k in t for k in labels):
            continue
        y = int(r.get("y", 0))
        if not (600 <= y <= 3050):
            continue
        hits.append({"t": t, "x": center_x(r), "y": y})
    hits.sort(key=lambda h: (h["y"], h["x"]))
    return (hits[0] if hits else None), hits


def handle_jump_dialog(dev, rows, log=print, path=None):
    """处理系统/淘宝的「打开其他应用」确认框 —— 只在用户显式授权范围内出手。

    绝不越界：只在确认框自己出现时动手，只点用户选的那一枚按钮；
    那枚按钮读不到就如实返回 not-handled（不改点「取消」，也不改点另一枚）。
    安装/权限类确认框**不在**这里处理，照样由看门狗拦下等人。
    """
    text = text_of(rows)
    if not any(k in text for k in JUMP_MARKS):
        return {"seen": False, "ok": False, "mode": None, "why": "no-jump-dialog"}
    pol = load_jump_policy(path)
    btn, cands = jump_dialog_button(rows, pol["mode"])
    if btn is None:
        log("[jump] 跳转确认框出现，但要点的「%s」读不到 → 不改点别的，交给看门狗/人"
            % "/".join(JUMP_BTN[pol["mode"]]))
        return {"seen": True, "ok": False, "mode": pol["mode"], "policy": pol,
                "why": "button-not-readable",
                "candidates": [c["t"] for c in cands[:4]]}
    log("[jump] 跳转确认框：按用户授权（%s，来源=%s）点「%s」(%d,%d)"
        % (pol["mode"], pol["source"], btn["t"], btn["x"], btn["y"]))
    dev.tap(btn["x"], btn["y"])
    time.sleep(2.0)
    return {"seen": True, "ok": True, "mode": pol["mode"], "policy": pol,
            "button": btn["t"], "tap": [btn["x"], btn["y"]]}


def popup_signature(pop, focus=""):
    """给「这一次判到的弹窗」一个稳定指纹：同一张卡反复出现时指纹不变。"""
    card = (pop or {}).get("card") or {}
    bounds = [int(card.get(k) or 0) for k in ("y0", "y1", "x0", "x1")]
    texts = ",".join(sorted(str(t) for t in ((pop or {}).get("close_texts") or []))[:4])
    return "%s|%s|%s" % (bounds, texts, str(focus or "")[-40:])


def popup_breaker(history, sig, limit=None):
    """连续判到同一张弹窗卡 → 返回 (熔断?, 连续第几次)。源: 2026-11-02 用户批准。

    history 是之前每次「判到并关掉」的指纹序列；只有**连续**相同才算，中间换过页面就清零。
    """
    limit = POPUP_BREAKER_LIMIT if limit is None else int(limit)
    run = 1
    for prev in reversed(list(history)):
        if prev != sig:
            break
        run += 1
    return bool(run >= limit), run


#: 黑名单命中：单独一个「支付」不能吃掉「支付宝」（源: run_energy.py SKIP_RE=支付(?!宝)）
PAYMENT_WORD, ALIPAY_WORD = "支付", "支付宝"


def black_hits(*texts):
    """返回命中的黑名单词；「支付」若只出现在「支付宝」里则不算命中。"""
    blob = " ".join(str(t or "") for t in texts)
    hits = [w for w in BLACK_WORDS if w in blob]
    if PAYMENT_WORD in hits:
        if blob.count(PAYMENT_WORD) <= blob.count(ALIPAY_WORD):
            hits = [w for w in hits if w != PAYMENT_WORD]
    return hits


# --------------------------------------------------------------------------- #
# 图像 / OCR 底座
# --------------------------------------------------------------------------- #
_ENGINE = None


def jdump(path, obj):
    with open(path, "w", encoding="utf8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


def jload(path):
    with open(path, encoding="utf8") as f:
        return json.load(f)


def ocr_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def ocr_image(img, scale=OCR_SCALE):
    """PIL.Image -> [{y,x,x2,t,s}]（坐标已换算回原图分辨率）。"""
    import numpy as np
    from PIL import Image
    im = img.convert("RGB")
    if scale != 1.0:
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    res, _ = ocr_engine()(np.array(im))
    out = []
    for box, txt, score in (res or []):
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        out.append({"y": int(sum(ys) / 4), "x": int(min(xs)), "x2": int(max(xs)),
                    "t": txt, "s": float(score)})
    out.sort(key=lambda r: (r["y"], r["x"]))
    return out


def image_signature(path, box=(0, 140, 1440, 3080), size=(40, 90)):
    """图片指纹：用来判「这一屏和上一屏一样 = 已到底」（R7 的终止条件）。"""
    from PIL import Image
    im = Image.open(path).convert("L").crop(box).resize(size)
    return im.tobytes()


def pix_diff(a, b):
    """两张图的内容差（0~255 均值）：滑动有没有真的动，靠它说话。"""
    from PIL import Image, ImageChops, ImageStat
    ia = Image.open(a).convert("L").resize((180, 400))
    ib = Image.open(b).convert("L").resize((180, 400))
    return round(ImageStat.Stat(ImageChops.difference(ia, ib)).mean[0], 2)


# --------------------------------------------------------------------------- #
# 设备层（只用 adbutils）
# --------------------------------------------------------------------------- #
class Device:
    def __init__(self, serial, log=print):
        import adbutils
        self.serial = serial
        self.log = log
        self._adb = adbutils.adb
        if ":" in serial:
            try:
                self._adb.connect(serial, timeout=10)
            except Exception as e:            # 已经连着会报错，不算失败
                log("[adb] connect 提示：%s" % e)
        self.d = self._adb.device(serial=serial)
        self.w, self.h = 1440, 3200
        try:
            ws = self.d.window_size()
            self.w, self.h = int(ws.width), int(ws.height)
        except Exception:
            pass
        self.shots = 0
        self.ocrs = 0
        #: 最近一帧是不是全黑（息屏）—— shot() 连续失败时也会把它落盘并置 True
        self.last_frame_black = False

    # ---- 底层 ----
    def shell(self, cmd, timeout=90):
        try:
            out = self.d.shell(cmd, timeout=timeout)
        except Exception as e:
            self.log("[adb] shell 失败 %r：%s" % (cmd, e))
            return ""
        if isinstance(out, str):
            return out
        return (out or b"").decode("utf8", "ignore")

    def tap(self, x, y):
        live_write("tap", "点击 @(%d,%d)" % (int(x), int(y)), self.serial)
        pause_wait(self.log, self.serial)
        self.shell("input tap %d %d" % (int(x), int(y)))

    def back(self):
        self.shell("input keyevent 4")

    def swipe(self, x1, y1, x2, y2, dur=300):
        live_write("swipe", "滑动 (%d,%d)->(%d,%d) %dms" % (x1, y1, x2, y2, dur), self.serial)
        pause_wait(self.log, self.serial)
        self.shell("input swipe %d %d %d %d %d" % (int(x1), int(y1), int(x2), int(y2), int(dur)))

    def focus(self):
        return self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'").strip()

    def focus_short(self):
        f = self.focus()
        m = re.search(r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", f)
        return m.group(1) if m else (f[:60] or "(空)")

    def app_current(self):
        try:
            info = self.d.app_current()
            return str(getattr(info, "package", "") or ""), str(getattr(info, "activity", "") or "")
        except Exception:
            f = self.focus()
            m = re.search(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", f)
            return (m.group(1), m.group(2)) if m else ("", "")

    def device_time(self):
        return self.shell("date +%Y-%m-%d_%H:%M:%S").strip()

    # ---- R9 唤醒：只有 keyevent 26 会真正点亮屏幕 ----
    def is_awake(self):
        out = self.shell("dumpsys power | grep mWakefulness=")
        # 本机输出形如 "  mWakefulness=Awake\nmWakefulness=1\n" —— 有**两处** mWakefulness=。
        # 必须取第一个（文字状态），取最后一个会解析成 "1" 从而永远判为未唤醒，
        # 导致 wake() 连按三次电源键把亮着的屏幕按灭（2026-11-02 实测事故）。
        m = re.search(r"mWakefulness=\s*([A-Za-z]+)", out or "")
        return bool(m) and m.group(1).lower().startswith("awake")

    def keyguard_showing(self):
        """是不是停在锁屏上（要密码的那种）。2026-11-02 用户提醒：唤醒了也进不去。

        所以「锁着」和「黑着」必须分开判：黑着还能按亮；锁着只能请人工解锁，
        脚本绝不能对着锁屏盲点（可能点出紧急拨号之类的）。
        """
        d = self.shell("dumpsys window") or ""
        return any(k in d for k in ("mDreamingLockscreen=true", "mShowingLockscreen=true",
                                    "isKeyguardShowing=true"))

    def _dismiss_keyguard(self):
        if self.keyguard_showing():
            self.shell("wm dismiss-keyguard")       # 无密码时会解锁；有密码时无效（正常）
            time.sleep(0.6)

    def wake(self, tries=3):
        if self.is_awake():
            self._dismiss_keyguard()
            return True
        for _ in range(tries):
            self.shell("input keyevent 26")     # R9
            time.sleep(0.9)
            if self.is_awake():
                self._dismiss_keyguard()
                return True
        return False

    def am_start_taobao(self):
        """R6：拉回淘宝只用 am start 欢迎页，不 force-stop。"""
        return self.shell("am start -n %s" % TAOBAO_WELCOME)

    # ---- 截图 / OCR ----
    def shot(self, path, tries=3):
        # dash 总控台：实时状态 + 暂停安全点（几乎每一步都要截图，这里最密）
        live_write("shot", "截图 %s" % os.path.basename(str(path)), self.serial)
        pause_wait(self.log, self.serial)
        last_black = None
        for _ in range(tries):
            if not self.is_awake() and not self.wake():
                raise StopRun("屏幕唤不醒（R9：224/82 不点亮，keyevent 26 也不行）→ 停下等人")
            try:
                img = self.d.screenshot(error_ok=False)
            except Exception as e:
                self.log("[shot] screencap 失败：%s" % e)
                time.sleep(1.0)
                self.wake()
                continue
            gray = img.convert("L").resize((16, 16))
            if max(gray.tobytes()) < 4:          # 全黑帧 = 还在锁屏 / 息屏
                last_black = img
                time.sleep(0.8)
                self.wake()
                continue
            img = img.convert("RGB")
            if path.lower().endswith((".jpg", ".jpeg")):
                img.save(path, format="JPEG", quality=82)   # 过程帧用 JPEG，控磁盘
            else:
                img.save(path)
            self.shots += 1
            self.last_frame_black = False
            return path
        # 2026-11-03：连续几次都是黑帧时**不再**直接 StopRun —— 息屏是设备问题，不是任务问题。
        # 帧照样落盘（证据链不断），把「全黑」交给调用方判：frame_black() 会认出来，
        # 判面板 / 判落地 / 复核三处统一记 status="screen-off"（不扣分、不进恢复阶梯）。
        self.last_frame_black = True
        if last_black is not None:
            last_black.convert("RGB").save(path)
            self.shots += 1
            self.log("[shot] 连续 %d 次拿到全黑帧（疑似息屏/锁屏）→ 帧已落盘，交由调用方按"
                     " screen-off 处理（不扣分、不进恢复阶梯）" % tries)
            return path
        raise StopRun("连续 %d 次截图失败（不是黑帧，是拿不到图）→ 停下等人" % tries)

    def shot_fast(self, path):
        """快速路径的「落地确认帧」：一次 adb 取 PNG 原始字节直接落盘（不 OCR、不要坐标）。

        2026-11-03 用户要求（修 3）。真机实测（空闲态各 3 次）：
            Device.shot()        1016/911/955 ms（= 本方法要替换的那条）
            device.screenshot()   746/769/743 ms
        这一帧只喂给 region_signature 做指纹比对，所以 shot() 里为「OCR/取证质量」花的钱
        在这里是纯浪费，一次省掉三笔：
          - is_awake() 的一次 dumpsys 往返；
          - 16x16 灰度查黑帧要先让 PIL 把整张图解码一遍；
          - convert("RGB") + 重新 PNG 编码，又是整张图一次编解码。
        这里只做「screencap -p 的字节 → 文件」，像素解码只留给指纹那一步（一次而不是两次）。
        拿不到合法 PNG 就退回 shot()（含黑帧检测与唤醒重试），绝不把坏帧当证据。
        返回 {"path","channel","bytes","ms"}；channel 会落进 06_verdict.json 的 landing_pixel。
        """
        live_write("shot", "快速抓帧 %s" % os.path.basename(str(path)), self.serial)
        pause_wait(self.log, self.serial)
        t0 = time.time()
        try:
            data = self.d.shell(["screencap", "-p"], encoding=None)
            blob = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
            if len(blob) > 1024 and blob[:8] == PNG_MAGIC:
                with open(path, "wb") as f:
                    f.write(blob)
                self.shots += 1
                return {"path": path, "channel": "raw_png", "bytes": len(blob),
                        "ms": int((time.time() - t0) * 1000)}
            self.log("[shot_fast] 回来的不是合法 PNG（%d 字节）→ 退回完整截图" % len(blob))
        except Exception as e:
            self.log("[shot_fast] 原始字节通道失败：%s → 退回完整截图" % e)
        self.shot(path)
        return {"path": path, "channel": "full_shot",
                "bytes": (os.path.getsize(path) if os.path.exists(path) else 0),
                "ms": int((time.time() - t0) * 1000)}

    def ocr(self, path, scale=OCR_SCALE):
        from PIL import Image
        rows = ocr_image(Image.open(path), scale)
        self.ocrs += 1
        return rows


def dev_pixel_frame(dev, path):
    """落地确认帧的统一入口：设备有快速通道就用快速的，没有（如离线假设备）退回 dev.shot。"""
    f = getattr(dev, "shot_fast", None)
    if callable(f):
        return f(path)
    dev.shot(path)
    return {"path": path, "channel": "full_shot"}


# --------------------------------------------------------------------------- #
# 定位小工具
# --------------------------------------------------------------------------- #
def find_text(rows, *keys, ymin=None, ymax=None, xmax=None, exact=False):
    out = []
    for r in rows:
        t = str(r.get("t", ""))
        ok = (t.strip() == keys[0]) if exact else all(k in t for k in keys)
        if not ok:
            continue
        if ymin is not None and int(r["y"]) < ymin:
            continue
        if ymax is not None and int(r["y"]) > ymax:
            continue
        if xmax is not None and center_x(r) > xmax:
            continue
        out.append(r)
    return out


def tap_row(dev, r, guard=False):
    cx, cy = center_x(r), int(r["y"])
    if guard:
        cx = guard_button_x(cx, str(r.get("t", "")), cy)
    dev.tap(cx, cy)
    return cx, cy


# --------------------------------------------------------------------------- #
# 弹窗处置（缺陷 1）：分级策略 —— 关闭文字 > 卡片几何候选点 > BACK 兜底
# 事故 2026-11-02 手机1：首页弹「双11品类周 · 2张消费券共45元 · 去使用」，关闭键是
# 一枚**无文字**的图标 X（在卡片下沿再往下约 190px）。旧脚本不认识这层遮罩，连点 8 次
# 「我的淘宝 tab」全被遮罩吃掉 → 导航必然失败。现在按三级策略处置，每次处置都落盘：
# 截图 + OCR 文本 + 用了哪一级策略 + 结果。**绝不点** POPUP_FORBIDDEN 里的任何按钮。
# --------------------------------------------------------------------------- #
def _popup_dir(outdir=None):
    d = os.path.join(outdir or os.path.join(_root(), "evidence",
                                            "%s-auto" % datetime.now().strftime("%Y-%m-%d")),
                     "popups")
    os.makedirs(d, exist_ok=True)
    return d


def _dump_rows_txt(path, rows):
    try:
        with open(path, "w", encoding="utf8") as f:
            for r in rows:
                f.write("%4d %4d-%-4d %s" % (int(r["y"]), int(r["x"]), int(r["x2"]),
                                             r.get("t", "")) + chr(10))
    except Exception:
        pass


def close_candidates(rows):
    """第一级：屏上的关闭类文字。文字关闭键优先于图标 X；同类里取最下面那个。"""
    out = []
    for r in rows:
        t = norm(r.get("t"))
        if not t or len(t) > 8:
            continue
        glyph = t in CLOSE_GLYPHS
        word = (not glyph) and any(w in t for w in CLOSE_WORDS_TEXT)
        # 用户批准的浮层关闭文案（确认选择 / 取消）按整串相等认，避免误伤长文案
        exact = (not glyph) and any(t == w for w in OVERLAY_CLOSE_EXACT)
        if not (glyph or word or exact):
            continue
        if any(f in t for f in POPUP_FORBIDDEN):      # 文字里带禁点词，不当关闭键
            continue
        if glyph and (int(r.get("x2", 0)) - int(r.get("x", 0))) > 160:
            continue                                  # 一串文字里的 x，不是图标
        out.append(dict(r, how=("glyph" if glyph else "word")))
    out.sort(key=lambda r: (0 if r["how"] == "word" else 1, -int(r["y"])))
    return out


def overlay_close_button(rows):
    """找那枚「把挡路浮层关掉」的按钮（用户 2026-11-02 批准的那几个文案）。

    已知案例：解锁新车轮弹窗用 BACK 会掉回首页，必须点「确认选择」。
    只认整串相等，且文字里带 POPUP_FORBIDDEN 的一律不认 —— 宁可不关，也绝不点
    立即领取 / 领取 / 去支付 / 开通 / 授权 / 同意 / 签约 / 抽奖 / 去使用 / 去玩 / 抢。
    """
    hits = []
    for r in rows:
        t = norm(r.get("t"))
        if not t or not any(t == w for w in OVERLAY_CLOSE_EXACT):
            continue
        if any(f in t for f in POPUP_FORBIDDEN):
            continue
        if int(r.get("x2", 0)) - int(r.get("x", 0)) > 420:      # 一整段句子不是按钮
            continue
        hits.append({"t": t, "x": center_x(r), "y": int(r["y"])})
    hits.sort(key=lambda h: -h["y"])                            # 越靠下的越像主按钮
    return (hits[0] if hits else None), hits


def _forbidden_boxes(rows):
    return [r for r in rows if any(f in norm(r.get("t")) for f in POPUP_FORBIDDEN)]


def _hits_forbidden(x, y, fboxes, pad_x=150, pad_y=120):
    """候选点离禁点按钮太近就不点（宁可不关，也绝不点「去使用/领取/支付…」）。"""
    for r in fboxes:
        if (int(r["x"]) - pad_x) <= x <= (int(r["x2"]) + pad_x) and \
                abs(int(r["y"]) - y) <= pad_y:
            return str(r.get("t", ""))
    return ""


def find_popup_card(path):
    """找「盖住屏幕的一层卡片」：上下各一条横贯屏幕的硬边界，卡片内部很平（没有文字行）。

    返回 {"x0","x1","y0","y1","cx","cy",...} 或 None。纯图形判据，不认颜色也不认文字。
    """
    try:
        import numpy as np
        from PIL import Image
        im = Image.open(path).convert("RGB")
        W, H = im.size
        a = np.asarray(im, dtype=np.int16)
        bx0, bx1 = int(W * 0.10), int(W * 0.90)
        band = a[:, bx0:bx1, :].astype(np.int32)
        d = np.abs(np.diff(band, axis=0)).max(axis=2)        # 相邻两行的逐列差
        drow, frac = d.mean(axis=1), (d > 40).mean(axis=1)
        edges = [y + 1 for y in range(len(drow)) if frac[y] >= 0.85 and drow[y] >= 55]

        def span(y, pct=(2, 98)):
            c = np.nonzero(d[y - 1] > 40)[0]
            if c.size < 10:
                return None
            return int(np.percentile(c, pct[0])) + bx0, int(np.percentile(c, pct[1])) + bx0

        best = None
        for i in range(len(edges) - 1):
            yt, yb = edges[i], edges[i + 1]      # 卡片 = 相邻两条强边界之间（中间不再有强边界）
            if yt < int(H * 0.06):
                continue
            h = yb - yt
            if h > 0.85 * H or h < 0.28 * H:
                continue
            inner = drow[yt + 30:yb - 30]
            if inner.size < 60 or float(np.median(inner)) > 8.0:
                continue                                      # 内部不平 → 不是一张卡片
            st, sb = span(yt), span(yb)
            if not st or not sb:
                continue
            lo, hi = max(st[0], sb[0]), min(st[1], sb[1])      # 上下边界要框在同一块上
            if hi <= lo:
                continue
            iou = float(hi - lo) / max(1, max(st[1], sb[1]) - min(st[0], sb[0]))
            if iou < 0.55:
                continue
            score = (float(drow[yt - 1]) + float(drow[yb - 1])) * (0.5 + 0.5 * h / H)
            if best is None or score > best[0]:
                best = (score, yt, yb)
        if best is None:
            return None
        _, y0, y1 = best
        sp = span(y0)
        if not sp:
            return None
        x0, x1 = sp
        w, h = x1 - x0, y1 - y0
        if not (0.45 * W <= w <= 0.96 * W):
            return None
        cy = (y0 + y1) // 2
        if not (0.20 * H <= cy <= 0.88 * H):
            return None
        # 「大面积纯色」自证：卡片内部颜色必须够纯，否则那是页面内容而不是盖上来的一层
        reg = a[y0 + 10:y1 - 10, x0 + 10:x1 - 10, :][::3, ::3, :]
        q = (reg >> 4).reshape(-1, 3).astype(np.int32)
        keys = (q[:, 0] << 8) | (q[:, 1] << 4) | q[:, 2]
        _, cnt = np.unique(keys, return_counts=True)
        tot = max(1, keys.size)
        dom = float(cnt.max()) / tot
        top3 = float(cnt[np.argsort(cnt)[::-1][:3]].sum()) / tot
        if dom < 0.30 or top3 < 0.55:
            return None
        # 「遮罩」自证：弹窗是盖在**变暗**的页面之上的，卡片外圈必须比卡片内部暗得多。
        # 没有这一条，普通页面里一张大白卡（例如菜鸟活动页）也会被当成弹窗 → 会点坏。
        gg = a.max(axis=2).astype(np.float32)
        inner = float(np.median(gg[y0 + 40:y1 - 40, x0 + 40:x1 - 40][::3, ::3]))
        outs = []
        for sl in (gg[max(0, y0 - 120):max(1, y0 - 20), int(0.10 * W):int(0.90 * W)],
                   gg[min(H - 2, y1 + 20):min(H - 1, y1 + 120), int(0.10 * W):int(0.90 * W)],
                   gg[y0:y1, 2:max(3, x0 - 20)],
                   gg[y0:y1, min(W - 2, x1 + 20):W - 2]):
            if sl.size:
                outs.append(float(np.median(sl[::3, ::3])))
        if inner <= 1 or not outs or max(outs) > 0.72 * inner:
            return None
        return {"x0": x0, "x1": x1, "y0": y0, "y1": y1, "w": w, "h": h,
                "dom_color": round(dom, 2), "top3_color": round(top3, 2),
                "cx": (x0 + x1) // 2, "cy": cy,
                "cover_h_pct": round(100.0 * h / H, 1), "screen": [W, H]}
    except Exception:
        return None


def _components(mask):
    """4 邻域连通域（不依赖 scipy）：返回 [(x0, x1, y0, y1, area)]。"""
    import numpy as np
    h, w = mask.shape
    seen = np.zeros((h, w), dtype=bool)
    out = []
    ys, xs = np.nonzero(mask)
    for k in range(len(ys)):
        y, x = int(ys[k]), int(xs[k])
        if seen[y, x]:
            continue
        seen[y, x] = True
        stack = [(y, x)]
        x0 = x1 = x
        y0 = y1 = y
        area = 0
        while stack:
            cy, cx = stack.pop()
            area += 1
            if cx < x0:
                x0 = cx
            elif cx > x1:
                x1 = cx
            if cy < y0:
                y0 = cy
            elif cy > y1:
                y1 = cy
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        out.append((x0, x1, y0, y1, area))
    return out


def find_close_icon(path, card):
    """卡片下沿正下方、卡片水平中线附近的横窗里找那枚**无文字**的圆形关闭图标。

    窗口 = x: 卡片中心 ±25% 卡宽（本弹窗的 X 就在正下方中央）, y: 卡片下沿 +30~+380px。
    判据：与窗口底色差 ≥40 的一整块，宽高 40~430px、接近正方、填充率像圆盘。
    """
    try:
        import numpy as np
        from PIL import Image
        im = Image.open(path).convert("RGB")
        W, H = im.size
        a = np.asarray(im, dtype=np.int16)
        g = a.max(axis=2).astype(np.float32)
        ya = min(H - 1, card["y1"] + 30)
        yb = min(H, card["y1"] + 380)
        if yb - ya < 80:
            return None
        half = max(120, int(0.25 * card["w"]))
        xa = max(0, card["cx"] - half)
        xb = min(W, card["cx"] + half)
        sub = g[ya:yb, xa:xb]
        if sub.size < 20000:
            return None
        bg = float(np.median(sub))
        for sign in (1.0, -1.0):                 # 先找比底色亮的，再找比底色暗的
            for th in (40.0, 90.0):              # 先按普通对比度找，找不到再提高对比度
                mask = ((sub - bg) * sign) > th
                if mask.sum() > 0.35 * mask.size:      # 半屏都在动 → 不是一枚图标
                    continue
                best = None
                for (x0, x1, y0, y1, area) in _components(mask):
                    w, hh = x1 - x0 + 1, y1 - y0 + 1
                    if not (40 <= w <= 430 and 40 <= hh <= 430):
                        continue
                    if not (0.70 <= float(w) / max(1, hh) <= 1.45):
                        continue                  # 关闭图标基本是方的；细长条/合并块都不是
                    if area < 1500 or area < 0.35 * w * hh:
                        continue                  # 太稀疏 → 不是一整枚图标
                    cx, cy = xa + (x0 + x1) // 2, ya + (y0 + y1) // 2
                    cand = {"x": cx, "y": cy, "w": w, "h": hh, "area": int(area),
                            "fill": round(float(area) / float(w * hh), 2),
                            "dx_from_card_cx": int(abs(cx - card["cx"])),
                            "thr": th,
                            "polarity": ("brighter" if sign > 0 else "darker")}
                    if best is None or cand["dx_from_card_cx"] < best["dx_from_card_cx"]:
                        best = cand
                if best is not None:
                    return best
        return None
    except Exception:
        return None


def _region_delta(before, after, box):
    """两帧在指定矩形里的平均灰度差。弹窗原封不动 → 接近 0；遮罩撤掉 → 几十。"""
    from PIL import Image, ImageChops, ImageStat
    ia = Image.open(before).convert("L").crop(box)
    ib = Image.open(after).convert("L").crop(box)
    if ia.size != ib.size:
        ib = ib.resize(ia.size)
    return round(float(ImageStat.Stat(ImageChops.difference(ia, ib)).mean[0]), 2)


def _popup_verify(dev, pdir, name, why, before_frame, card=None):
    """处置之后复核：**同一块区域**变没变。

    判据用「卡片矩形内的平均灰度差」而不是「还detect不detect得到卡片」——后者在普通
    页面上也会误命中（2026-11-02 实测：弹窗已被关掉，却把商品流当成卡片 → 多点了两下）。
    """
    time.sleep(0.9)
    p = os.path.join(pdir, "%s_after_%s.png" % (name, why))
    out = {"after": os.path.basename(p), "gone": False}
    try:
        dev.shot(p)
        if card:
            box = (max(0, int(card["x0"])), max(0, int(card["y0"])),
                   int(card["x1"]), int(card["y1"]))
        else:
            from PIL import Image as _Img
            W, H = _Img.open(p).size
            box = (int(0.10 * W), int(0.15 * H), int(0.90 * W), int(0.85 * H))
        out["box"] = [int(v) for v in box]
        out["region_delta"] = _region_delta(before_frame, p, box)
        out["card_after"] = find_popup_card(p)
        out["pix_delta"] = pix_diff(before_frame, p)
        out["gone"] = bool(out["region_delta"] >= 2.5)
        if out["gone"]:
            # 只在「确实关掉了」这一刻多做一次 OCR：证据要能证明关掉之后是哪一屏
            out["text_after"] = text_of(dev.ocr(p))[:300]
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def dismiss_popup_if_any(dev, log=print, outdir=None, tag="", rows=None, frame=None,
                         allow_back=True):
    """分级关弹窗，返回 {"detected","stage","tap","dismissed","fallback","steps",...}。

    第 1 级  关闭类文字（关闭 / × / X / 我知道了 / 以后再说 / 暂不 / 忽略 / 残忍离开）
    第 2 级  没有文字 → 定位弹窗卡片矩形，依次取三个候选点：
             底部中央下方 40~120px → 右上角内侧 60px → 卡片下方那枚无字圆图标
    第 3 级  仍关不掉 → BACK 兜底（日志标 fallback=back），最多 POPUP_MAX_BACK=2 次
    绝不点 POPUP_FORBIDDEN 里的任何按钮；每次处置都落盘（截图 + OCR + 策略 + 结果）。
    """
    ts = datetime.now().strftime("%H%M%S")
    pdir = _popup_dir(outdir)
    name = "%s_%s" % (ts, re.sub(r"[^0-9A-Za-z_-]+", "-", tag or "popup")[:24])
    rec = {"at": datetime.now().isoformat(timespec="seconds"),
           "device": getattr(dev, "serial", ""), "tag": tag,
           "detected": False, "stage": "none", "tap": None, "dismissed": False,
           "fallback": None, "steps": []}
    try:
        if not frame or not os.path.exists(str(frame)):
            frame = dev.shot(os.path.join(pdir, name + "_A.png"))
        frame = str(frame)
        if rows is None:
            rows = dev.ocr(frame)
        rec["frame"] = os.path.relpath(frame, os.path.dirname(pdir))
        rec["ocr_n"] = len(rows)
        rec["ocr_text"] = text_of(rows)
        _dump_rows_txt(os.path.join(pdir, name + "_ocr.txt"), rows)
        card = find_popup_card(frame)
        close = close_candidates(rows)
        fboxes = _forbidden_boxes(rows)
        rec["card"] = card
        rec["icon"] = find_close_icon(frame, card) if card else None
        rec["close_texts"] = [str(r.get("t")) for r in close]
        rec["forbidden_texts"] = [str(r.get("t")) for r in fboxes]
        if not card and not close:
            rec["result"] = "no-popup"
            jdump(os.path.join(pdir, name + ".json"), rec)
            return rec
        rec["detected"] = True

        # ---- 第 0 级：规则库优先（用户教过「关闭/×」在哪就点哪）----
        for kw in POPUP_RULE_KEYS:
            if rec["dismissed"]:
                break
            rr = find_rule(dev, (kw,))
            if not rr:
                continue
            x, y = int(rr["x"]), int(rr["y"])
            step = {"stage": "rule", "text": rr.get("label") or kw, "tap": [x, y]}
            hit = _hits_forbidden(x, y, fboxes)
            if hit:
                step["skip"] = "规则点离禁点按钮太近（%s）" % hit
                rec["steps"].append(step)
                continue
            log("[rule] 用规则「%s」点 (%d,%d)" % (rr.get("label") or kw, x, y))
            dev.tap(x, y)
            v = _popup_verify(dev, pdir, name, "rule%d" % len(rec["steps"]), frame, card)
            step.update(v)
            rec["steps"].append(step)
            if v["gone"]:
                rec.update(stage="rule", tap=[x, y], dismissed=True)

        # ---- 第 1 级：关闭类文字 ----
        for r in close[:3]:
            x, y = center_x(r), int(r["y"])
            step = {"stage": "text", "text": str(r.get("t")), "tap": [x, y]}
            hit = _hits_forbidden(x, y, fboxes)
            if hit:
                step["skip"] = "候选点离禁点按钮太近（%s）" % hit
                rec["steps"].append(step)
                continue
            dev.tap(x, y)
            v = _popup_verify(dev, pdir, name, "text%d" % len(rec["steps"]), frame, card)
            step.update(v)
            rec["steps"].append(step)
            if v["gone"]:
                rec.update(stage="text", tap=[x, y], dismissed=True)
                break

        # ---- 第 2 级：卡片几何候选点（底部中央优先）----
        if not rec["dismissed"] and card:
            cands = [("card-bottom", (card["cx"], card["y1"] + 80)),
                     ("card-topright", (card["x1"] - 60, card["y0"] + 60)),
                     ("card-bottom", (card["cx"], card["y1"] + 120))]
            if rec.get("icon"):
                cands.append(("card-icon", (rec["icon"]["x"], rec["icon"]["y"])))
            for stage_name, (x, y) in cands:
                if rec["dismissed"]:
                    break
                x = max(2, min(int(getattr(dev, "w", 1440)) - 3, int(x)))
                y = max(2, min(int(getattr(dev, "h", 3200)) - 3, int(y)))
                step = {"stage": stage_name, "tap": [x, y]}
                hit = _hits_forbidden(x, y, fboxes)
                if hit:
                    step["skip"] = "候选点离禁点按钮太近（%s）" % hit
                    rec["steps"].append(step)
                    continue
                dev.tap(x, y)
                v = _popup_verify(dev, pdir, name,
                                  "%s%d" % (stage_name, len(rec["steps"])), frame, card)
                step.update(v)
                rec["steps"].append(step)
                if v["gone"]:
                    rec.update(stage=stage_name, tap=[x, y], dismissed=True)

        # ---- 第 3 级：BACK 兜底（最多 2 次）----
        if not rec["dismissed"] and allow_back:
            for k in range(POPUP_MAX_BACK):
                dev.back()
                time.sleep(1.2)
                v = _popup_verify(dev, pdir, name, "back%d" % (k + 1), frame, card)
                v.update({"stage": "back", "fallback": "back", "n": k + 1})
                rec["steps"].append(v)
                if v["gone"]:
                    rec.update(stage="back", fallback="back", dismissed=True)
                    break
        rec["result"] = "dismissed" if rec["dismissed"] else "still-there"
    except Exception as e:
        rec["result"] = "exception"
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        rec["traceback"] = traceback.format_exc()[-1500:]
    try:
        jdump(os.path.join(pdir, name + ".json"), rec)
    except Exception:
        pass
    log("[popup] %s detected=%s 策略=%s%s 结果=%s 证据=%s.json"
        % (tag or "popup", rec["detected"], rec["stage"],
           (" fallback=back" if rec.get("fallback") == "back" else ""),
           rec.get("result"), name))
    return rec


# --------------------------------------------------------------------------- #
# 规则库（teach.py 教过的坐标） / 待教学（不会就问人，退出码 3）
# --------------------------------------------------------------------------- #
_RULES_CACHE = {"path": "", "key": None, "rows": []}


def load_rules(path=None):
    """读 targets.jsonl（按 mtime+size 缓存）。坏行跳过：规则库有问题也不能把跑批拖死。"""
    path = os.path.abspath(path or TARGETS_PATH)
    try:
        st = os.stat(path)
        key = (st.st_mtime, st.st_size)
    except OSError:
        return []
    if _RULES_CACHE["path"] == path and _RULES_CACHE["key"] == key:
        return _RULES_CACHE["rows"]
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                try:
                    r["x"], r["y"] = int(r["x"]), int(r["y"])
                except (KeyError, TypeError, ValueError):
                    continue
                rows.append(r)
    except OSError:
        return []
    rows.sort(key=lambda r: str(r.get("ts", "")))
    _RULES_CACHE.update(path=path, key=key, rows=rows)
    return rows


def _rule_text(r):
    return ("%s %s" % (r.get("label", ""), r.get("note", ""))).lower()


def find_rule(dev, keywords, path=None):
    """按 label/note 关键词找**最近一条**屏幕尺寸和当前设备一致的规则；没有就 None。"""
    kws = [str(k).strip().lower() for k in keywords if str(k).strip()]
    if not kws:
        return None
    w, h = int(getattr(dev, "w", 0) or 0), int(getattr(dev, "h", 0) or 0)
    best, best_kw = None, ""
    for r in load_rules(path):                 # 已按 ts 升序 → 后面覆盖前面 = 取最新
        try:
            if int(r.get("screen_w", 0)) != w or int(r.get("screen_h", 0)) != h:
                continue
        except (TypeError, ValueError):
            continue
        hit = next((k for k in kws if k in _rule_text(r)), "")
        if not hit:
            continue
        if not (0 <= r["x"] < w and 0 <= r["y"] < h):
            continue
        best, best_kw = r, hit
    if not best:
        return None
    out = dict(best)
    out["kw"] = best_kw
    return out


def click_by_rule(dev, keywords, log=print, why="", path=None):
    """按关键词查规则 → 直接 input tap → 打印 [rule] 命中日志；查不到返回 None（走 OCR 兜底）。"""
    r = find_rule(dev, keywords, path=path)
    if not r:
        return None
    log("[rule] 用规则「%s」点 (%d,%d)%s"
        % (r.get("label") or r.get("note") or "?", r["x"], r["y"],
           ("  ← %s" % why) if why else ""))
    dev.tap(r["x"], r["y"])
    return r


def pending_dir(path=None):
    return os.path.join(os.path.dirname(os.path.abspath(path or TARGETS_PATH)), "pending")


def _frame_bright(path):
    """粗看一张帧亮不亮（0~255）：加载中的黑底帧亮度极低，对人没有教学价值。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            gray = im.convert("L").resize((16, 16))
            data = gray.tobytes()
        return sum(data) / float(len(data))
    except Exception:
        return 255.0


def write_pending(dev, question, context, label_hint="", kind="unknown", log=print):
    """「我不会这一步」落盘：pending/<ts>.png + pending/<ts>.json（state=pending）。"""
    d = pending_dir()
    os.makedirs(d, exist_ok=True)
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S") + "_%03d" % (now.microsecond // 1000)
    shot = os.path.join(d, ts + ".png")
    ok_shot = True
    try:
        # 黑屏/加载中的帧对人没用：等两秒再拍，最多 3 次（拍不到亮的也照记最后一张）
        for attempt in range(3):
            dev.shot(shot)
            if _frame_bright(shot) >= 8:
                break
            log("[teach] 这张帧基本是黑的（锁屏/加载中？）-> 点亮屏幕再等 2s 重拍（第 %d/3 次）"
                % (attempt + 1))
            try:
                dev.shell("input keyevent 224")     # KEYCODE_WAKEUP：只点亮，不切换电源状态
            except Exception:
                pass
            time.sleep(2.0)
    except Exception as e:
        ok_shot = False
        log("[teach] 截图失败（问题照记）：%s" % e)
    try:
        focus = dev.focus_short()
    except Exception:
        focus = ""
    if isinstance(context, dict):
        context = dict(context)
        context.setdefault("focus", focus)
    rec = {
        "ts": ts,
        "serial": getattr(dev, "serial", ""),
        "screenshot": (ts + ".png") if ok_shot else "",
        "screenshot_path": shot if ok_shot else "",
        "question": question,
        "context": context,
        "screen_w": int(getattr(dev, "w", 0) or 0),
        "screen_h": int(getattr(dev, "h", 0) or 0),
        "state": "pending",
        "label_hint": label_hint,
        "kind": kind,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "focus": focus,
        "teach_urls": ["http://127.0.0.1:%d" % p for p in TEACH_PORTS],
    }
    jdump(os.path.join(d, ts + ".json"), rec)
    log("=" * 72)
    log("[teach] 这一步我不会，已经停手等你教：")
    log("[teach] 问题：%s" % question)
    log("[teach] 截图：%s" % (rec["screenshot_path"] or "(截图失败)"))
    log("[teach] 记录：%s" % os.path.join(d, ts + ".json"))
    log("[teach] 打开 http://127.0.0.1:8777 （手机1）或 :8778 （手机2）→「待教学」标签页")
    log("[teach] 在那张截图上点正确位置 → 保存 → 规则写进 targets.jsonl")
    log("[teach] 退出码 3 = 需要教学（不是崩溃）；教完重跑 auto.py 即可")
    log("=" * 72)
    return rec


def _nav_teach(dev, what, log, scans_dir, tries, diag, rows, text, why="",
               label_hint="导航", question="", extra_ctx=None):
    """导航卡住 → 截图 + pending json 落盘，然后停手（退出码 3），绝不盲点、不死循环。"""
    q = question or ("我想进入%s任务面板，但当前界面上找不到入口（%s），该点哪里？"
                     % (WHAT_NAME.get(what, what), why or "认不出这个界面"))
    rules = load_rules()
    ctxd = {"stage": "nav", "what": what, "why": why,
            "tries": dict(tries),
            "ocr_n": len(rows or []), "ocr_text": (text or "")[:400],
            "diagnostics": (diag or [])[-3:],
            "rules_n": len(rules),
            "rules_newest": [{"label": r.get("label"), "note": r.get("note"),
                              "x": r.get("x"), "y": r.get("y"),
                              "screen_w": r.get("screen_w"), "screen_h": r.get("screen_h")}
                             for r in rules[-5:]],
            "device": {"serial": getattr(dev, "serial", ""),
                       "screen_w": getattr(dev, "w", 0), "screen_h": getattr(dev, "h", 0)}}
    if extra_ctx:
        ctxd.update(extra_ctx)
    rec = write_pending(dev, q, ctxd, label_hint=label_hint, kind="nav-" + str(what), log=log)
    raise NeedTeaching("需要教学：%s（%s）" % (q, rec["ts"]), rec)


# --------------------------------------------------------------------------- #
# 面板导航 / 展开 / 重新进面板（R7）
# --------------------------------------------------------------------------- #
#: 导航步骤关键字 -> 规则用途（intent）。规则只有在 intent 一致时才可用，
#: 否则「名字里含这个词」的规则会被误用（2026-11-02 实测：我的淘宝规则被当成领淘金币用，原地打转）
NAV_KW_INTENT = {
    "我的淘宝": "my_taobao",
    "领淘金币": "coin_entry",
    "去赚金币": "go_earn_coin",
    "去赚能量": "go-earn-energy",
    "做任务赚能量": "race_tasks",
}


def find_rule_intent(dev, intent, path=None):
    """按 intent 精确取规则（同设备同分辨率、坐标合法），取最新一条；没有返回 None。"""
    if not intent:
        return None
    w, h = int(getattr(dev, "w", 0) or 0), int(getattr(dev, "h", 0) or 0)
    best = None
    for r in load_rules(path):
        try:
            if int(r.get("screen_w", 0)) != w or int(r.get("screen_h", 0)) != h:
                continue
            if int(r["x"]) < 0 or int(r["y"]) < 0 or int(r["x"]) >= w or int(r["y"]) >= h:
                continue
        except (TypeError, ValueError, KeyError):
            continue
        if (r.get("intent") or "") == intent:
            best = r
    return best


def _nav_action(dev, what, rows):
    """看这一屏能做什么动作：返回 (说明, 坐标或 None, 预算 key)。

    规则库优先：这一屏**确实有**入口文字，而且用户教过这个文字的坐标 → 用规则坐标，
    不再依赖 OCR 框（OCR 框在小字/图标上经常偏）。查不到再走原来的 OCR 兜底。
    """
    text = text_of(rows)
    for kw in NAV_RULE_STEPS.get(what, ()) + ("我的淘宝",):
        if kw not in text:
            continue
        # 只认「用途一致」的规则；名字里碰巧含这个词的不算（2026-11-02 实测 bug）
        rr = find_rule_intent(dev, NAV_KW_INTENT.get(kw))
        if rr:
            return ("用规则「%s」点 (%d,%d)" % (rr.get("label") or kw, rr["x"], rr["y"]),
                    (int(rr["x"]), int(rr["y"])), "rule-" + kw)
        # 没有对应规则时，直接用这个关键词在屏幕上的文字位置点下去。
        # 修 2026-11-02 实测 bug：原来会 continue 到下一个关键词（「我的淘宝」），
        # 于是在我的淘宝页上反复点自己，永远到不了「领淘金币」。
        hit = find_text(rows, kw)
        if hit:
            r0 = hit[0]
            return ("tap %s（OCR 文字位置）" % kw, (center_x(r0), int(r0["y"])), "tap-" + kw)
    if what == "race":
        if "做任务赚能量" in text:                      # 赛车页的入口按钮
            r = find_text(rows, "做任务赚能量")[0]
            return "tap 做任务赚能量", (center_x(r), int(r["y"])), "race-tasks"
        if "去赚能量" in text:
            r = find_text(rows, "去赚能量")[0]
            return "tap 去赚能量", (center_x(r), int(r["y"])), "go-earn-energy"
    if what == "coin" and "去赚金币" in text:
        cand = find_text(rows, "去赚金币")
        cand.sort(key=lambda r: center_x(r))            # 左侧那枚才是入口
        r = cand[0]
        return "tap 去赚金币", (center_x(r), int(r["y"])), "go-earn-coin"
    if "领淘金币" in text:
        r = find_text(rows, "领淘金币")[0]
        return "tap 领淘金币", (center_x(r), int(r["y"])), "coin-entry"
    tab = find_text(rows, "我的淘宝", ymin=int(getattr(dev, "h", 3200) * 0.85))
    if tab:
        r = tab[0]
        return "tap 我的淘宝 tab", (center_x(r), int(r["y"])), "my-taobao-tab"
    return "", None, ""


def hard_restart_taobao(dev, log=print):
    """用户 2026-11-02 授权：卡住 / 弹窗关不掉时，直接 force-stop 淘宝再重开。
    这是最快的恢复手段——卡住时那点现场状态本来也没用了。"""
    try:
        log("[recover] force-stop + 重新启动淘宝（用户授权）")
        dev.shell("am force-stop %s" % TAOBAO_PKG)
        time.sleep(1.5)
        dev.am_start_taobao()
        time.sleep(6.0)
        return True
    except Exception as e:
        log("[recover] 重启淘宝失败：%s" % e)
        return False


def _nav_fail(dev, what, log, scans_dir, tries, diag, step_tries):
    """同一个导航动作 3 次仍不成功 → 落盘「导航失败」（截图 + OCR + 每步次数），停该面板。"""
    ts = datetime.now().strftime("%H%M%S")
    d = os.path.join(os.path.dirname(os.path.abspath(scans_dir)), "navfail")
    os.makedirs(d, exist_ok=True)
    img = os.path.join(d, "%s_%s_navfail.png" % (ts, what))
    rows, text = [], ""
    try:
        dev.shot(img)
        rows = dev.ocr(img)
        text = text_of(rows)
    except Exception as e:
        log("[nav] 导航失败取证时出错：%s" % e)
    _dump_rows_txt(os.path.join(d, "%s_%s_navfail_ocr.txt" % (ts, what)), rows)
    try:
        jdump(os.path.join(d, "%s_%s_navfail.json" % (ts, what)), {
            "at": datetime.now().isoformat(timespec="seconds"), "what": what,
            "device": getattr(dev, "serial", ""), "verdict": "导航失败",
            "step_tries": step_tries, "tries": tries, "diagnostics": diag,
            "frame": os.path.basename(img), "focus": dev.focus(),
            "ocr_n": len(rows), "ocr_text": text,
        })
    except Exception:
        pass
    log("[nav] 导航失败 panel=%s（已落盘）各动作尝试次数=%s 上限=%d 次/动作"
        % (what, tries, step_tries))
    log("[nav] 截图=%s OCR前300字=%s" % (os.path.basename(img), text[:300]))
    # 同一个动作 3 次不成功 = 我已经不会了：落盘待教学 + 停手（退出码 3），不再「跳过面板」硬跑
    _nav_teach(dev, what, log, scans_dir, tries, diag, rows, text,
               why="同一个导航动作 %d 次仍未成功" % step_tries,
               extra_ctx={"navfail_frame": img})
    return None, "nav-failed", {"navfail_frame": img, "tries": dict(tries)}


def _f1(x):
    """把「可能是 None / 非数字」的耗时变成 float，专供日志里的 %.1f 用。

    2026-11-02 真机事故：导航里一条 log 的 %.1f 收到 None → TypeError 直接把整轮跑崩。
    日志不该有能力打断业务，所以所有 %.1f 的入参都过这一层（None -> 0.0）。
    """
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_focus_pkg_act(raw):
    """从 dumpsys 的两行 focus 里拆出 (包名, activity 短名)。纯函数（--selftest 用）。

    行的形态：
      mCurrentFocus=Window{3 u0 com.taobao.taobao/com.taobao.themis.container.app.TMSActivity}
      mFocusedApp=AppWindowToken{... ActivityRecord{... com.taobao.taobao/.TBMainActivity}}
    mCurrentFocus 优先（和以前一模一样）；它读不到（=null）时退回 mFocusedApp。
    """
    first = other = None
    for line in str(raw or "").splitlines():
        m = re.search(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", line)
        if not m:
            continue
        # com.taobao.tao.TBMainActivity -> TBMainActivity（预期清单用的是短名）
        pair = (m.group(1), m.group(2).rsplit(".", 1)[-1])
        if "mCurrentFocus" in line:
            first = pair
            break
        if other is None:
            other = pair
    return first or other or ("", "")


def focus_pkg_activity(dev):
    """读一次前台 (包名, activity 短名)。dumpsys，零 OCR，约 0.2s。读不到返回 ("", "")。

    S2（用户 2026-11-03 拍板）：am start 之后只要焦点**已经在 com.taobao.taobao 包里**就得
    立刻停止等待 —— 点进去落在淘宝自家页面（TaoLiveVideoActivity / FluidVideoActivity）时，
    再发一次 am start 不会换页，白等满 3 秒纯属浪费。光有 activity 短名不够（要认包名），
    所以单独给这一对；一次 dumpsys 同时拿包名 + 短名，不额外多花时间。
    """
    try:
        raw = str(dev.focus() or "")
    except Exception:
        return "", ""
    return parse_focus_pkg_act(raw)


def focus_activity(dev):
    """读一次前台 activity 短名（dumpsys，零 OCR，约 0.2s）。读不到返回 ""。

    2026-11-02 19:49 事故后新增：这是「我现在在哪一屏」的唯一判据。
    返回短名（TBMainActivity / TMSActivity / TTDetailActivity …），不带包名与前缀。
    包名那一半由 focus_pkg_activity 提供（S2 用它认「焦点已在淘宝包内」）；判定逻辑共用一份。
    """
    return focus_pkg_activity(dev)[1]


def focus_is_bad(act):
    """焦点是不是「商品 / 下单 / 看图 / 支付宝」这类绝对不该再点的页面。"""
    a = str(act or "")
    if not a:
        return False
    if any(b in a for b in FOCUS_BAD_ACTS):
        return True
    low = a.lower()
    return any(s in low for s in FOCUS_BAD_SUB)


def wait_focus(dev, allowed, timeout_s, log=None, tag="", why="", escape_bad=True):
    """轮询 focus 直到 activity ∈ allowed（零 OCR）。返回 (ok, act, 读了几次, 用时)。

    轮询途中一旦发现焦点落在商品/下单/支付宝类页面（硬规则 3），**立刻 BACK**（零点击），
    最多 FOCUS_ESCAPE_BACKS 次；退出来就继续等，退不出来就按超时返回 False。
    """
    t0, polls, act, backs = time.time(), 0, "", 0
    while True:
        act = focus_activity(dev)
        polls += 1
        if act and act in allowed:
            return True, act, polls, float(time.time() - t0)   # 一定给真实数字，绝不给 None
        if escape_bad and focus_is_bad(act) and backs < FOCUS_ESCAPE_BACKS:
            backs += 1
            if log:
                log("[focus] %s 焦点在危险页 %r（商品/下单/支付宝类）-> 只按 BACK"
                    "（第 %d/%d 次），期间不发任何点击" % (tag, act, backs, FOCUS_ESCAPE_BACKS))
            dev.back()
            time.sleep(1.2)
            continue
        if time.time() - t0 >= float(timeout_s):
            if log:
                log("[focus] %s 等了 %.1fs 仍是 activity=%r（期望 %s）%s"
                    % (tag, _f1(time.time() - t0), act, "/".join(allowed), why))
            return False, act, polls, float(time.time() - t0)
        time.sleep(FOCUS_POLL_S)


def escape_bad_page(dev, log, tag="", max_back=FOCUS_ESCAPE_BACKS, allowed=None):
    """焦点在商品/下单/支付宝类页面时：**只按 BACK**，期间一次点击都不发（硬规则 3）。

    返回 (是否已离开危险页, act, 按了几次 BACK, 读了几次 focus)。
    """
    allowed = tuple(allowed or (FOCUS_ACTS_HOME + FOCUS_ACTS_VISIT))
    polls, backs, act = 0, 0, ""
    while True:
        act = focus_activity(dev)
        polls += 1
        if not focus_is_bad(act):
            return True, act, backs, polls
        if backs >= int(max_back):
            log("[focus] %s 危险页 %r 按了 %d 次 BACK 还没退出去 -> 停手，交给恢复阶梯"
                % (tag, act, backs))
            return False, act, backs, polls
        backs += 1
        log("[focus] %s 焦点在危险页 %r（商品/下单/支付宝类）-> 只按 BACK（第 %d/%d 次），"
            "期间不发任何点击" % (tag, act, backs, max_back))
        dev.back()
        time.sleep(1.2)


def cold_start_taobao(dev, log=print):
    """进面板的固定第一步：force-stop -> sleep(1.0) -> am start 欢迎页（不截图、不 OCR）。

    用户 2026-11-02 原话：「你直接先重启淘宝嘛，不管怎么样，都是直接重启第一步」——
    起点确定，写死坐标才有意义；当前落在商品页/下单页/浏览器都不会再传染进来。
    """
    # 2026-11-02 真机事故：手机2 在导航途中息屏 → 之后每次截图都是全黑、每次盲点都石沉大海，
    # 而 focus 仍报着旧 activity，看上去像"坐标错了"。盲点之前必须先确认屏幕是亮的。
    # dev.wake() 内部先查 is_awake()，只有真的黑屏才会按 26（那是唤醒，不是按灭）。
    try:
        if not dev.is_awake():
            log("[nav] 屏幕是黑的 -> 先唤醒再冷启动（盲点必须有亮屏前提）")
        dev.wake()
        if dev.keyguard_showing():
            # 用户 2026-11-02：「你按亮了也没用吧，你又没有密码」。对——所以锁着就停手等人，
            # 绝不对着锁屏盲点（那可能点出紧急拨号/相机，还会把后续步骤全带偏）。
            raise StopRun("手机停在锁屏上（需要密码/图案解锁）：请先人工解锁再跑。"
                          "脚本不会对着锁屏盲点，也不会尝试猜密码。")
    except StopRun:
        raise
    except Exception as e:
        log("[nav] 唤醒检查失败（继续）：%s" % e)
    log("[nav] 冷启动第 1 步：force-stop %s（%s 用户要求：进面板前先重启）"
        % (TAOBAO_PKG, datetime.now().strftime("%H:%M:%S")))
    try:
        dev.shell("am force-stop %s" % TAOBAO_PKG)
    except Exception as e:
        log("[nav] force-stop 失败（继续 am start）：%s" % e)
    time.sleep(WRITTEN_NAV_KILL_S)
    try:
        dev.am_start_taobao()
        log("[nav] 冷启动第 2 步：am start %s" % TAOBAO_WELCOME)
    except Exception as e:
        log("[nav] am start 失败：%s" % e)


def wait_home(dev, log=print, tag="", timeout_s=None):
    """等淘宝首页（TBMainActivity）就绪：零 OCR，每 FOCUS_POLL_S 读一次 focus。

    用户 2026-11-02 补充规则：
      - 焦点是商品/下单/支付宝类 -> 立刻 BACK（最多 FOCUS_ESCAPE_BACKS 次），期间零点击；
      - 一直是 TMSActivity（开屏广告 / 活动页）-> **连续 FOCUS_TMS_BACK_AFTER 次**才按 1 次
        BACK，最多 FOCUS_TMS_BACK_MAX 次（不是每次轮询都按，更不许循环）；
      - 超时还没到首页 -> 返回 False，调用方**一次点击都不发**，直接走恢复阶梯。
    返回 (ok, act, focus 读次数, 用时, BACK 次数)。
    """
    timeout_s = FOCUS_LAUNCH_MAX_S if timeout_s is None else float(timeout_s)
    t0, polls, act = time.time(), 0, ""
    backs_bad = backs_tms = tms_run = 0
    while True:
        act = focus_activity(dev)
        polls += 1
        if act and act in FOCUS_ACTS_HOME:
            return True, act, polls, float(time.time() - t0), backs_bad + backs_tms
        tms_run = tms_run + 1 if act in FOCUS_ACTS_TMS else 0
        if focus_is_bad(act) and backs_bad < FOCUS_ESCAPE_BACKS:
            backs_bad += 1
            log("[focus] %s 焦点在危险页 %r（商品/下单/支付宝类）-> 只按 BACK（第 %d/%d 次），"
                "期间不发任何点击" % (tag, act, backs_bad, FOCUS_ESCAPE_BACKS))
            dev.back()
            time.sleep(1.2)
            continue
        if tms_run >= FOCUS_TMS_BACK_AFTER and backs_tms < FOCUS_TMS_BACK_MAX:
            backs_tms += 1
            tms_run = 0
            log("[focus] %s 连续 %d 次都停在活动页 %r -> 按 1 次 BACK 再看"
                "（第 %d/%d 次，之后仍按「连续 %d 次」才再按）"
                % (tag, FOCUS_TMS_BACK_AFTER, act, backs_tms, FOCUS_TMS_BACK_MAX,
                   FOCUS_TMS_BACK_AFTER))
            dev.back()
            time.sleep(1.2)
            continue
        if time.time() - t0 >= timeout_s:
            log("[focus] %s 等了 %.1fs 还没到淘宝首页（TBMainActivity），最后 focus=%r，"
                "已按 BACK %d 次" % (tag, _f1(time.time() - t0), act, backs_bad + backs_tms))
            return False, act, polls, float(time.time() - t0), backs_bad + backs_tms
        time.sleep(FOCUS_POLL_S)


#: 本次运行放「临时帧」的目录：由 main() 建在证据目录下（实测可写），空 = 退回系统 TEMP。
#: 2026-11-09 真机复现：面板起的子进程写 %TEMP% 会被拒（[Errno 13] Permission denied），
#: 而同一个进程写证据目录一直没问题 —— 所以临时帧就落在证据目录里。
FRAME_DIR = ""


def frame_path(stem, ext="png"):
    """一次运行里临时帧的落地路径：证据目录（或 TEMP）+ 本进程 pid。

    带 pid 是为了两个并发的 auto（面板起的 + 手敲命令起的）不抢同一个文件；
    放证据目录是因为 %TEMP% 在面板起的子进程里可能写不进去（见上面的真机复现）。
    """
    base = FRAME_DIR or os.environ.get("TEMP") or os.environ.get("TMP") or "."
    return os.path.join(base, "%s-%d.%s" % (stem, os.getpid(), ext))


def _rm_quiet(path):
    """删临时文件，删不掉就算了（Windows 上被别的进程占着是常态，不该影响主流程）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def wait_panel_list_ready(dev, log=print, tag=""):
    """点开面板后，等「任务列表」真的画出来 —— 纯像素判断，**0 次 OCR、0 次文字解码**。

    2026-11-02 真机（手机2 赛车面板）：标题「做任务赚能量」先出来，任务列表晚好几秒才渲染，
    而写死导航只固定等 STEP_SETTLE_S(2.5s) 就拍照判定 —— 判定帧里只有标题、列表一片空白
    （OCR 只读到 25 个文字块），于是被判成「没进面板」，白走一趟恢复阶梯（20 秒）。
    这里把列表区域缩小、算深色像素占比：空白页≈0，列表画出来后明显大于 0。
    返回 (ready, waited_s, ink)。
    """
    from PIL import Image
    # 临时帧文件名 = 设备后缀 + **本进程 pid**：两台并行时共用同一个文件会互相覆盖/抢锁
    # （2026-11-09 真机实测：面板起的 auto 和手敲命令起的 auto 撞在同一个 _panel_ready-<机型>.png，
    #  报 WinError 32 / Permission denied，整条「等列表渲染」退化成完整截图通道，白等 8 秒）。
    # 带 pid 之后各写各的，函数走的时候自己删掉，TEMP 里不留东西。
    tmp = frame_path("_panel_ready-%s" % (_dev_san(CUR_SERIAL) or "local"))
    t0 = time.time()
    best = 0.0
    while True:
        try:
            if callable(getattr(dev, "shot_fast", None)):
                dev.shot_fast(tmp)
            else:
                dev.shot(tmp)
            im = Image.open(tmp).convert("L")
            w, h = im.size
            l, t, r, b = PANEL_READY_BOX
            im = im.crop((int(w * l), int(h * t), int(w * r), int(h * b))).resize((90, 160))
            px = im.tobytes()
            ink = sum(1 for v in px if v < 200) / float(len(px) or 1)
            best = max(best, ink)
            if ink >= PANEL_READY_INK:
                _rm_quiet(tmp)
                return True, round(time.time() - t0, 2), round(ink, 4)
        except Exception as e:      # noqa: BLE001
            log("[nav] %s 等列表渲染时读帧失败（继续等）：%s" % (tag, e))
        if time.time() - t0 >= PANEL_READY_MAX_S:
            _rm_quiet(tmp)
            return False, round(time.time() - t0, 2), round(best, 4)
        time.sleep(PANEL_READY_POLL_S)


def nav_by_ocr(dev, what, log, scans_dir, cold=False, max_tries=6):
    """2026-11-09：**不知道分辨率也能进任务面板** —— 全靠 OCR 找文字再点，不用任何写死坐标。

    用户给同学做通用版的要求（原话）：「没有他们手机的坐标数据，就按 OCR 来吧，慢点也没事」。
    入口词按优先级：
        「领淘金币」   -> 从首页进淘金币页
        「签到领金币」 -> 点掉签到（日常版每天第一次必须点，否则进不了面板）
        「赚更多金币」 -> 进任务面板
    每轮 1 次 OCR：先判是不是已经在面板，不是就点认识的词，一个都不认识才 BACK 一次。
    返回 (rows, how, info)；rows 为 None = 没走通（调用方走恢复阶梯）。
    """
    rounds = []
    if cold:
        try:
            dev.am_start_taobao()
            time.sleep(3.0)
        except Exception as e:
            log("[navocr] %s am start 发不出去（%s: %s）-> 直接照当前屏找" % (what, type(e).__name__, e))
    words = ("签到领金币", "赚更多金币", "领淘金币")
    for i in range(max_tries):
        p = os.path.join(scans_dir, "_navocr_%s_%d.jpg" % (what, i))
        try:
            dev.shot(p)
            rows = dev.ocr(p)
        except Exception as e:
            log("[navocr] %s 第 %d 次截图/OCR 失败：%s: %s" % (what, i + 1, type(e).__name__, e))
            return None, "ocr-nav-io-fail", {"rounds": rounds, "error": str(e)[:120]}
        ok, info = panel_ok(rows, what)
        if ok:
            log("[navocr] %s 第 %d 次 OCR：已在面板 %s" % (what, i + 1, info))
            return rows, "ocr-nav", {"rounds": rounds, "info": info}
        hit = None
        for r in rows:
            txt = str(r.get("t") or "")
            for w in words:
                if w in txt:
                    hit = (w, r)
                    break
            if hit:
                break
        if hit is None:
            log("[navocr] %s 第 %d 次 OCR：入口词一个都没读到（%s）-> BACK 一次再试"
                % (what, i + 1, "/".join(words)))
            rounds.append({"ocr": i + 1, "action": "back",
                           "saw": text_of(rows)[:100]})
            try:
                dev.back()
            except Exception:
                pass
            time.sleep(1.6)
            continue
        w, r = hit
        try:
            x, y = tap_row(dev, r)
        except Exception as e:
            log("[navocr] %s 点到「%s」时失败：%s: %s" % (what, w, type(e).__name__, e))
            continue
        log("[navocr] %s 第 %d 次 OCR：读到「%s」-> tap (%d,%d)" % (what, i + 1, w, x, y))
        rounds.append({"ocr": i + 1, "action": "tap", "word": w, "xy": [x, y]})
        time.sleep(2.5)
    return None, "ocr-nav-exhausted", {"rounds": rounds}


def written_nav_to_panel(dev, what, log, scans_dir, outdir=None, cold=True, judge=True):
    """写死导航进面板：主路径 2 步 + 1 次 OCR，兜底路径再 3 步 + 1 次 OCR（最多 2 次）。

    为什么要先点「去赚金币 / 去赚能量」：首页「领淘金币」和我的淘宝「领淘金币」都只把
    **淘金币主页面**（双11赛车那屏，底部两枚黑胶囊）拉到前台，任务面板要再点一下才出来。
    证据：evidence/2026-11-02-auto/run.log 12:58:05→12:58:34
    「我的淘宝 tab → 领淘金币 → 去赚金币 → [panel] coin ready」；11:09:59 点 (1303,1266)
    「去赚能量」→ 11:10:26「[panel] race ready」。

    cold=True（默认）：第一步固定 force-stop + am start，然后等首页就绪——起点确定；
    cold=False：不重启（多面板第 2 个 / 恢复阶梯刚 force-stop 过），只确认当前在哪一屏。
    每一步盲点都被 focus 校验夹住（2026-11-02 19:49 真机事故后加，见常量区硬规则）：
      - 点之前：焦点是危险页 -> 只 BACK；activity 不在该步 need 里 -> 这一步不点；
      - 点之后：该步的 then 6 秒内没出现 -> 不许盲点下一发，交回调用方走恢复阶梯。
    导航主路径**不调用** dismiss_popup_if_any / find_rule / click_by_rule，也不逐步截图：
    用户原话「弹窗先不考虑…每一步都要识别太耽误时间」（弹窗由人在旁边手动点掉）。
    返回 (rows, how, info)；rows 为 None 表示这条写死路径没走通（调用方该走恢复阶梯）。
    """
    res_key, nav_all = written_nav_steps(dev)
    if nav_all is None:
        # R14（2026-11-03 第五批·任务一）：未登记的分辨率**不许瞎猜**。
        # 手机1/手机2 的首页版式本来就不同（「领淘金币」分别在 (159,938) / (721,890)），
        # 拿别的机型的坐标去点只会点到别处 —— 所以这一次调用一次点击都不发，
        # 直接返回 written-unregistered，交给调用方走恢复阶梯 / 待教学。
        # 2026-11-09 通用版：没有这台手机的坐标表 -> **改用 OCR 导航**（不点任何坐标）。
        # 用户口径：「没有他们手机的坐标数据，就按 OCR 来吧，慢点也没事」。
        log("[nav] 未登记的分辨率 %s（已登记：%s）-> 改走 OCR 导航（全程按文字找按钮）"
            % (res_key or "未知", "/".join(sorted(DEV_COORDS))))
        rows, how, info = nav_by_ocr(dev, what, log, scans_dir, cold=cold)
        if rows is not None:
            return rows, how, info
        return None, how, dict(info or {}, resolution=res_key,
                               registered=sorted(DEV_COORDS))
    steps_map = nav_all[what]
    ocr0 = int(getattr(dev, "ocrs", 0) or 0)
    fpolls = 0
    t0 = time.time()
    # judge=False：写死动作做完就返回，不做判面板 OCR（交给紧接着第一轮的抓帧判定）。

    def _judge(stage_key):
        p = os.path.join(scans_dir, "_nav_%s_%s.jpg" % (what, stage_key))
        dev.shot(p)
        if frame_black(p):
            # 息屏：这一帧判不了面板（focus 还会骗人）。唤醒重拍一次，还黑就交回调用方当 screen-off
            rewrap_screen(dev, log, "%s 判面板帧" % what)
            dev.shot(p)
            if frame_black(p):
                log("[nav] %s 判面板那一帧仍是全黑（息屏）-> 判定无效，不进恢复阶梯" % what)
                return (None, False, {"screen_off": True, "frame_black": True,
                                      "rewoke": True})
        rows = dev.ocr(p)                  # 唯一的一次判面板 OCR
        ok, info = panel_ok(rows, what)
        log("[nav] %s OCR 判面板（focus 读 %d 次）：%s %s（累计 OCR %d 次 / 用时 %.1fs）"
            % (what, fpolls, "在面板" if ok else "不在面板", info,
               int(getattr(dev, "ocrs", 0) or 0) - ocr0, _f1(time.time() - t0)))
        return rows, ok, info

    settled = []          # 是否已经为「首页画完」等过一拍（同一次调用只等一次）

    def _settle_home(why):
        if settled:
            return
        settled.append(1)
        log("[nav] %s %s -> 首页 activity 已就绪，再固定等 %.1fs 让页面画完"
            "（activity 起来 != 渲染完；真机 0.9s 时点击会落空、2s 时已完整）"
            % (what, why, HOME_SETTLE_S))
        time.sleep(HOME_SETTLE_S)

    if cold:
        # 用户 2026-11-02 最高优先：进面板的第一步永远是「先重启淘宝」，
        # 起点确定后写死坐标才有意义（当前落在商品页/下单页都不会再传染进来）。
        cold_start_taobao(dev, log)
        ok, act, n, spent, backs = wait_home(dev, log, what)
        fpolls += n
        log("[nav] %s 冷启动后等首页：focus=%r（等 %.1fs / 读 focus %d 次 / BACK %d 次）"
            % (what, act, _f1(spent), n, backs))
        if not ok:
            log("[nav] %s 冷启动后 %.1fs 还没到首页 -> 一次点击都不发，交给恢复阶梯"
                % (what, FOCUS_LAUNCH_MAX_S))
            return None, "written-home-timeout", {"focus_reads": fpolls, "activity": act}
        _settle_home("冷启动")
    else:
        # 不重启（多面板的第 2 个 / 刚由恢复阶梯 force-stop 过）：直接确认当前在哪一屏
        act = focus_activity(dev)
        fpolls += 1
        if act not in FOCUS_ACTS_HOME and act not in FOCUS_ACTS_TMS:
            ok, act, n, spent, backs = wait_home(dev, log, what)
            fpolls += n
            if not ok:
                log("[nav] %s 没等到首页（最后 focus=%r）-> 一次点击都不发，交给恢复阶梯"
                    % (what, act))
                return None, "written-home-timeout", {"focus_reads": fpolls, "activity": act}
            _settle_home("等首页")
        log("[nav] %s 不重启淘宝，当前 focus=%r（多面板连跑 / 恢复阶梯重开后走这条）"
            % (what, act))

    # 2026-11-07 日常版：币页那颗大红按钮未签到时是「签到领金币」，签完才变「赚更多金币」。
    # 进面板前先看一眼：还在签到那一屏就先点掉签到（同一次 OCR 顺便确认当前是币页）。
    if what == "coin" and judge:
        try:
            act0 = focus_activity(dev)
            fpolls += 1
            if act0 in FOCUS_ACTS_TMS:
                _p = os.path.join(scans_dir, "_nav_coin_signin.jpg")
                dev.shot(_p)
                _txt = text_of(dev.ocr(_p))
                if "签到领金币" in _txt:
                    _k, _tbl = device_coords(dev)
                    _xy = (_tbl or {}).get("task_go_coin")
                    if _xy:
                        log("[nav] %s 币页上是「签到领金币」（还没签到）-> 先点掉签到 (%d,%d)"
                            % (what, int(_xy[0]), int(_xy[1])))
                        dev.tap(int(_xy[0]), int(_xy[1]))
                        time.sleep(2.5)
        except Exception as _e:
            log("[nav] %s 签到预检失败（不影响后续）：%s: %s"
                % (what, type(_e).__name__, _e))
    for stage, key in (("主路径", "primary"), ("兜底", "fallback")):
        if key == "fallback":
            # 兜底序列第一步是首页「我的淘宝」tab：只有确认还在首页才允许走它
            act = focus_activity(dev)
            fpolls += 1
            if act not in FOCUS_ACTS_HOME:
                log("[nav] %s 主路径没走通，且 focus=%r 不在首页 -> 不走兜底（不许盲点），"
                    "交给恢复阶梯" % (what, act))
                break
            log("[nav] %s 主路径没走通，但 focus=%r 仍在首页 -> 走兜底写死序列" % (what, act))
            _settle_home("兜底前")     # 兜底第一发是首页「我的淘宝」tab：同样要等页面画完
        failed = False
        for i, step in enumerate(steps_map[key], 1):
            tag = "%s %s 第 %d/%d 步" % (what, stage, i, len(steps_map[key]))
            act = focus_activity(dev)
            fpolls += 1
            if focus_is_bad(act):
                left, act, backs, n2 = escape_bad_page(dev, log, tag)
                fpolls += n2
                if not left:
                    failed = True
                    break
            if act not in step["need"]:
                if act in FOCUS_ACTS_TMS and "TBMainActivity" in step["need"]:
                    log("[focus] %s 已经在淘金币页（TMSActivity）-> 跳过这一步"
                        "（它是给首页/我的淘宝页用的）" % tag)
                    continue
                log("[focus] %s activity=%r 不在预期清单 %s -> 这一步不点，交给恢复阶梯"
                    % (tag, act, "/".join(step["need"])))
                failed = True
                break
            xy = step["xy"]
            log("[nav] %s -> tap (%d,%d)（点前 focus=%r，动作之间不 OCR）"
                % (tag, int(xy[0]), int(xy[1]), act))
            dev.tap(int(xy[0]), int(xy[1]))
            if step.get("then"):
                ok2, act2, n3, spent2 = wait_focus(dev, step["then"], FOCUS_STEP_MAX_S,
                                                   log, tag, "（这一步点完要到的页面）")
                fpolls += n3
                if not ok2:
                    log("[focus] %s 点完 %.1fs 没到 %s（focus=%r）-> 不许盲点下一发，"
                        "交给恢复阶梯" % (tag, _f1(FOCUS_STEP_MAX_S),
                                          "/".join(step["then"]), act2))
                    failed = True
                    break
                log("[focus] %s 点完 %.1fs focus=%r OK（读 %d 次）"
                    % (tag, _f1(spent2), act2, n3))
                # 2026-11-02 真机事故：淘金币主页面是 H5，activity 切过来 ~1s 时
                # 还没接管点击 —— 这一拍会「点空」，页面纹丝不动（日志看着像成功）。
                # 历史上成功那轮两次点击间隔 14s。这里固定等 STEP_SETTLE_S 让 H5 起来，
                # 仍然 0 次 OCR、0 张截图。
                time.sleep(STEP_SETTLE_S)
            else:
                time.sleep(float(step.get("wait") or 0))
        if failed:
            continue
        # 判之前先等列表真的画出来（纯像素，不 OCR）：否则会读到「只有标题、列表空白」那种帧
        ready, waited, ink = wait_panel_list_ready(dev, log, "%s(%s)" % (what, key))
        log("[nav] %s 等任务列表渲染：%s（等了 %.1fs，列表区深色占比 %.4f）"
            % (what, "已画出" if ready else "8 秒仍空白，按原样判定", _f1(waited), ink))
        if not judge:
            # 2026-11-02 用户要求：「坐标都有了，还用 OCR 干嘛」。
            # 写完拍拍完就直接交回，判面板交给**紧接着第一轮自己的抓帧 OCR**（它本来就要读
            # 任务行，顺带就判了 panel_ok）——同一件事不再收两遍钱。
            # 万一没进去，第一轮会返回 no-panel，由 drive_panel 的兜底重新走这里。
            return None, "written-assumed", {
                "assumed": True, "stage": key, "resolution": res_key,
                "nav_ocr": int(getattr(dev, "ocrs", 0) or 0) - ocr0, "focus_reads": fpolls}
        rows, ok, info = _judge(key[:1])
        if (info or {}).get("screen_off"):
            return None, "screen-off", info
        if ok:
            return rows, "written-" + key, info
    return None, "written-failed", {"nav_ocr": int(getattr(dev, "ocrs", 0) or 0) - ocr0,
                                    "focus_reads": fpolls, "resolution": res_key}


def recover_to_panel(dev, what, log, scans_dir, budget=None):
    """恢复阶梯 —— 只用在「任务识别 / 完成识别」两处（2026-11-02 19:17 用户要求）。

    BACK -> 1 次 OCR -> 判面板；最多 3 次 BACK（RECOVER_BACKS）；
    都不行 -> force-stop 重开淘宝 + 按写死序列重进（**每轮最多 1 次重启**，budget 控）；
    重进仍不行 -> 落盘待教学并停手（NeedTeaching，退出码 3），绝不无限重启。

    budget 由调用方传 {"left": n} 并在同一轮内共享：one_round 每轮开头重置为 RECOVER_RESTART_MAX。
    成功返回 (True, rows, how)；失败不返回而是抛 NeedTeaching。
    """
    budget = budget if budget is not None else {"left": RECOVER_RESTART_MAX}
    ocr0 = int(getattr(dev, "ocrs", 0) or 0)
    rows = []
    for i in range(RECOVER_BACKS):
        log("[recover] %s 第 %d/%d 次 BACK -> 判面板（恢复阶梯每次 BACK 后 1 次 OCR）"
            % (what, i + 1, RECOVER_BACKS))
        dev.back()
        time.sleep(1.8)
        p = os.path.join(scans_dir, "_recover_%s_%d.jpg" % (what, i))
        dev.shot(p)
        rows = dev.ocr(p)
        if panel_ok(rows, what)[0]:
            log("[recover] %s BACK %d 次回到面板（本阶梯 OCR %d 次）"
                % (what, i + 1, int(getattr(dev, "ocrs", 0) or 0) - ocr0))
            return True, rows, "back-%d" % (i + 1)
    if int(budget.get("left", 0)) > 0:
        budget["left"] = int(budget.get("left", 0)) - 1
        log("[recover] %s BACK %d 次都不行 -> force-stop 重开淘宝 + 写死序列重进"
            "（本轮重启额度剩 %d 次）" % (what, RECOVER_BACKS, budget["left"]))
        # 2026-11-02 19:49 事故后补的：如果现在停在商品/下单/支付宝页，先按 BACK 退干净再重启，
        # 免得 force-stop 后「最近任务」把脚本又带回那个下单页。
        act_before = focus_activity(dev)
        if focus_is_bad(act_before):
            left, act_now, backs, _p = escape_bad_page(dev, log, "%s 重启前" % what)
            log("[recover] %s 重启前先退危险页 %r -> %r（BACK %d 次，%s）"
                % (what, act_before, act_now, backs, "已退出" if left else "没退出去"))
        hard_restart_taobao(dev, log)
        # hard_restart_taobao 里已经 am start + 等 6 秒，这里不再重复拉一次（省 4.5 秒）
        rows, how, info = written_nav_to_panel(dev, what, log, scans_dir, cold=False)
        if rows is not None:
            log("[recover] %s 重开后按写死序列重新进面板成功（%s，本阶梯累计 OCR %d 次）"
                % (what, how, int(getattr(dev, "ocrs", 0) or 0) - ocr0))
            return True, rows, how
        why = "force-stop 重开淘宝 + 写死序列重进后仍不在%s面板" % WHAT_NAME.get(what, what)
    else:
        why = "BACK %d 次 + 本轮重启额度已用完，仍不在%s面板" % (
            RECOVER_BACKS, WHAT_NAME.get(what, what))
    log("[recover] %s 恢复阶梯用尽（%s）-> 落盘待教学并停手" % (what, why))
    _nav_teach(dev, what, log, scans_dir, {}, [], rows or [],
               text_of(rows or []), why=why, label_hint="导航",
               question="%s面板找不回来了（%s）。当前这一屏该点哪里？"
                        % (WHAT_NAME.get(what, what), why))


def nav_until_panel(dev, what, log, scans_dir, outdir=None, cold=True,
                    step_tries=NAV_STEP_TRIES, max_actions=NAV_MAX_ACTIONS, judge=True):
    """把指定面板导航到前台 —— **写死序列版**（2026-11-02 19:17 用户现场要求）。

    与旧版（nav_until_panel_diagnostic）的差别，就是用户点名要摘掉的那一整套：
      - 动作之间 0 次截图、0 次 OCR；只在每条写死序列末尾 OCR 一次判 panel_ok（最多 2 次）；
      - 不调用 dismiss_popup_if_any / find_rule / click_by_rule，不做弹窗几何；
      - 弹窗这一批不处理（用户原话「弹窗先不考虑」，他在旁边手动点掉）。
    写死序列没走通（focus 校验拦下 / 页面没到位）就**直接进恢复阶梯**：
    BACK×3 -> force-stop + 写死序列重进（每轮最多 1 次）-> 仍不行落盘待教学停手。
    step_tries / max_actions 只为兼容旧调用点保留，写死版不再使用。
    """
    rows, how, info = written_nav_to_panel(dev, what, log, scans_dir, outdir,
                                          cold=cold, judge=judge)
    if rows is not None:
        return rows, how, info
    if how == "screen-off":
        # 判面板那两帧全黑（息屏）：这不是「没进去」，也不是流程被打乱 —— 进恢复阶梯只会白烧，
        # 而且息屏时 BACK/重启淘宝都没用。直接交回调用方（它按设备问题处理）。
        log("[nav] %s 判面板帧全黑（息屏）-> 判定无效，不进恢复阶梯" % what)
        return None, how, info
    if (info or {}).get("assumed"):
        # judge=False 的正常返回：写死动作都发出去了，判面板交给调用方的第一轮抓帧。
        # 这里**绝不能**当失败进恢复阶梯（那会白花 3 次 BACK + 一次重启 = 40 秒）。
        log("[nav] %s 写死序列已发出（%s），本轮不做判面板 OCR -> 交给第一轮抓帧判定"
            % (what, how))
        return None, how, info
    log("[nav] %s 写死序列没走通（%s，focus=%r）-> 进恢复阶梯"
        % (what, how, (info or {}).get("activity")))
    _ok, rows2, how2 = recover_to_panel(dev, what, log, scans_dir)
    return rows2, "nav-recovered:" + how2, {"recovered_from": how}


def nav_until_panel_diagnostic(dev, what, log, scans_dir, outdir=None,
                               step_tries=NAV_STEP_TRIES, max_actions=NAV_MAX_ACTIONS):
    """【旧版，导航主路径已不再调用】每轮先截图 + OCR + 弹窗几何 + 逐动作诊断的导航。

    2026-11-02 19:17 用户现场要求把这一整套从导航主路径上摘掉（太慢，且会在
    「rule-我的淘宝」这类规则上反复点同一坐标 → 第 7~9 轮死循环）。
    保留此函数仅供现场排障 / 别的脚本显式调用；**导航主路径不许调它**。
    """
    outdir = outdir or os.path.dirname(os.path.abspath(scans_dir))
    tries, diag = {}, []
    pop_hist = []      # 最近几次判到的弹窗指纹（熔断用，源: 2026-11-02 用户批准）
    for i in range(max_actions):
        p = os.path.join(scans_dir, "_nav_%s_%02d.jpg" % (what, i))
        dev.shot(p)
        rows = dev.ocr(p)
        ok, info = panel_ok(rows, what)
        if ok:
            return rows, ("already" if i == 0 else "nav-%d" % i), info

        # ---- 强制诊断：这一屏到底是什么（弹窗 / 加载中 / 别的页面）----
        text = text_of(rows)
        d = {"round": i, "frame": os.path.basename(p), "ocr_n": len(rows),
             "focus": dev.focus_short(),
             "loading": [w for w in LOADING_WORDS if w in text],
             "card": find_popup_card(p)}
        diag.append(d)

        # ---- 空帧/黑屏：页面还没出来，等着，不点也不问人（最多等 3 次）----
        if len(rows) < 3:
            k = "blank-%s" % what
            tries[k] = tries.get(k, 0) + 1
            if tries[k] <= 3:
                log("[nav] %s 第 %d 轮诊断=空帧/黑屏（OCR %d 行）-> 等 3s 再看，不点"
                    % (what, i, len(rows)))
                time.sleep(3.0)
                continue
            log("[nav] %s 仍然读不到字（OCR %d 行）-> 交给 am start/BACK 阶梯处置（不点坐标）"
                % (what, len(rows)))

        # ---- 弹窗/浮层：先处置再看屏（同一坐标绝不盲点第二次）----
        # 导航阶段禁止 BACK 兜底：淘宝首页的「双11」大横幅会被误判成弹窗，
        # 一旦按 BACK 去关它就会「判弹窗→BACK→又判弹窗」无限循环（2026-11-02 实测卡死 3 次）。
        # 真弹窗一定有可点的关闭控件；三级都关不掉，就说明它不是弹窗，继续找入口即可。
        pop = dismiss_popup_if_any(dev, log=log, outdir=outdir,
                                   tag="%s-nav%02d" % (what, i), rows=rows, frame=p,
                                   allow_back=False)
        if pop.get("detected"):
            d["popup"] = {"stage": pop.get("stage"), "dismissed": pop.get("dismissed"),
                          "tap": pop.get("tap"), "fallback": pop.get("fallback")}
            log("[nav] %s 第 %d 轮诊断=弹窗/浮层 -> 策略=%s%s 结果=%s"
                % (what, i, pop.get("stage"),
                   (" fallback=back" if pop.get("fallback") else ""),
                   "已关掉" if pop.get("dismissed") else "仍未关"))
            if pop.get("dismissed"):
                # 熔断：同一张卡连续关掉又回来（首页 banner 被当成弹层的典型样子），
                # 就不要再循环烧预算 —— 直接当「认不出入口」，落盘请人教。
                sig = popup_signature(pop, dev.focus_short())
                hot, run = popup_breaker(pop_hist, sig)
                pop_hist.append(sig)
                if hot:
                    # 用户 2026-11-02 授权：关不掉就先直接重启淘宝，比反复按返回有效
                    if tries.get("force-stop", 0) < 1:
                        tries["force-stop"] = 1
                        log("[nav] %s 同一张弹窗连续第 %d 次判到 -> 直接 force-stop 淘宝重开（用户授权）"
                            % (what, run))
                        hard_restart_taobao(dev, log)
                        pop_hist.clear()
                        continue
                    log("[nav] %s 同一张弹窗连续第 %d 次判到（%s）-> 重启淘宝后仍如此，"
                        "落盘请人教" % (what, run, sig))
                    return _nav_teach(
                        dev, what, log, scans_dir, tries, diag, rows, text,
                        why="同一张弹窗连续 %d 次关掉又回来（疑似把首页/活动 banner 当成弹层）"
                            % run,
                        label_hint="导航",
                        question="我一直把当前这张卡片当成弹窗关掉，它又回来（连续 %d 次）。"
                                 "%s面板的入口到底在哪？该点哪里？" % (run, WHAT_NAME.get(what, what)))
                time.sleep(1.2)
                continue
            # 关不掉：先 am start 洗一次状态（不点遮罩）；再关不掉就直接问人（退出码 3）
            key = "amstart-%s" % what
            tries[key] = tries.get(key, 0) + 1
            if tries[key] > 1:
                return _nav_teach(
                    dev, what, log, scans_dir, tries, diag, rows, text,
                    why="弹窗反复关不掉", label_hint="关闭",
                    question="当前有个弹窗挡住了%s面板，我点关闭和返回都关不掉，该点哪里？"
                             % WHAT_NAME.get(what, what))
            # 2026-11-02 用户实测：淘宝首页的「双11」大卡片会被误判成弹窗，
            # 于是在"关弹窗"上死循环，连查规则/找入口都轮不到。
            # 结论：导航阶段**不再因为"弹窗关不掉"而改变流程**——直接继续找入口，
            # 面板能不能出来，交给后面的面板判定说话。
            log("[nav] %s 弹窗关不掉 -> 忽略它，继续找入口（不再 am start / 不再问人）" % what)
            pop_hist = []
        pop_hist = []          # 这一轮没判到弹窗：连续计数清零
        if d["loading"]:
            log("[nav] %s 第 %d 轮诊断=页面加载中（%s）-> 等 2.5s 再看，不点"
                % (what, i, ",".join(d["loading"])))
            time.sleep(2.5)
            continue

        label, xy, key = _nav_action(dev, what, rows)
        if not key:
            # 认不出入口：绝不盲点。先按规则库试（含上次为这个意图教的坐标）；
            # 规则没有/试不动，再退回 am start + BACK 阶梯；阶梯也失败 → _nav_fail 落盘待教学。
            rr = find_rule(dev, UNKNOWN_RULE_KEYS)
            # 用户 2026-11-02 明确要求：认不出入口时**不要点教过的「返回」坐标**，
            # 直接用系统返回键（keyevent 4）就可以。教过的坐标只对"这一步专门教过的非返回类意图"生效。
            if (rr and (rr.get("label") or "") not in ("返回", "关闭", "×")
                    and tries.get("rule-unknown", 0) < RULE_RETRY_MAX):
                tries["rule-unknown"] = tries.get("rule-unknown", 0) + 1
                log("[rule] 用规则「%s」点 (%d,%d)  ← 这一步专门教过的坐标（第 %d/%d 次）"
                    % (rr.get("label") or "?", rr["x"], rr["y"],
                       tries["rule-unknown"], RULE_RETRY_MAX))
                dev.tap(rr["x"], rr["y"])
                time.sleep(2.6)
                continue
            key, label = "backout-%s" % what, "屏上认不出入口 -> 用系统返回键退一层再看"
        if tries.get(key, 0) >= step_tries:
            # 用户 2026-11-02：卡住就直接关掉淘宝重进，别一条条穷举规则。
            if tries.get("hard-restart", 0) < HARD_RESTART_MAX:
                tries["hard-restart"] = tries.get("hard-restart", 0) + 1
                n_hr = tries["hard-restart"]
                log("[nav] %s 卡住（%s 已 %d 次）-> force-stop 淘宝重开，第 %d/%d 次"
                    % (what, key, tries.get(key, 0), n_hr, HARD_RESTART_MAX))
                hard_restart_taobao(dev, log)
                tries.clear()
                tries["hard-restart"] = n_hr
                pop_hist.clear()
                continue
            return _nav_fail(dev, what, log, scans_dir, tries, diag, step_tries)
        tries[key] = tries.get(key, 0) + 1
        log("[nav] %s 第 %d 轮（%s 第 %d/%d 次，已诊断）-> %s"
            % (what, i, key, tries[key], step_tries, label))
        if xy:
            if key.startswith("rule-"):
                log("[rule] 用规则「%s」点 (%d,%d)" % (key[5:], xy[0], xy[1]))
            dev.tap(xy[0], xy[1])
            time.sleep(2.6)
            continue
        # 认不出入口：先 am start 把淘宝拉回前台，再 BACK 一层层退（实测 am start 有时无效，
        # 而 BACK 一定能退回淘宝主界面；R6 允许，不 force-stop、不盲点坐标）
        k = tries[key]
        if k == 1:
            log("[nav] 认不出入口 -> am start 拉回欢迎页（不 force-stop）")
            dev.am_start_taobao()
            time.sleep(3.5)
        elif k == 2:
            log("[nav] 认不出入口 -> BACK 退一层")
            dev.back()
            time.sleep(2.0)
        else:
            log("[nav] 认不出入口 -> BACK 连退两层")
            dev.back()
            time.sleep(1.2)
            dev.back()
            time.sleep(2.0)
    return _nav_fail(dev, what, log, scans_dir, tries, diag, step_tries)


def reopen_panel(dev, what, log, scans_dir):
    """R7 兼容入口：面板没了就按恢复阶梯重进（2026-11-02 19:17 用户要求）。

    旧实现是「截图判在不在面板 -> BACK -> 再来一遍 -> 最后 nav_until_panel」，
    每圈 1 次白白多花 3 秒 OCR；现在统一走 recover_to_panel()：
    BACK×3（每次 1 次 OCR）-> force-stop + 写死序列重进（每轮最多 1 次）-> 不行就待教学。
    """
    return recover_to_panel(dev, what, log, scans_dir)[1]


#: 2026-11-03 用户口径（手机2 真机）：面板顶部是「今日速赚」任务区，下面才是
#: 「更多金币等你赚 展开∨」；**先做速赚，再做展开里面的**；而且任务多了以后
#: 「展开」会被挤到屏幕下面 —— 不往下滑根本看不到，所以不能靠"进面板时按固定坐标点展开"。
#: 做法：折叠交给「一页一页做」的循环 —— 当前屏没有可做行、但这一帧里读到「展开」时才点它，
#: 坐标取**那一帧 OCR 盒的中心**（自适应、零盲点、零新增 OCR：复用这一轮的帧）。
FOLD_TEXTS = ("展开", "展开更多", "查看更多", "点击展开", "查看更多任务")
#: 同一屏最多点几次「展开」（OCR 抖动 / 点了没反应时的保险丝，防死循环）
MAX_EXPAND_TRIES = 3
#: 点完「展开」等多久让列表长出来
FOLD_SETTLE_S = 1.9


def fold_button(rows):
    """这一帧里有没有「展开」这类折叠标记？有就返回 {"t","x","y","box"}（x/y = 盒中心）。

    只认「整行文字就是折叠文案」（允许后面跟一个 ∨/▽ 这类符号）——任务行里带「展开」两个字
    （例如「去看看展开的活动(1/2)」）绝不当按钮，免得点错（R5 黑名单风险）。
    """
    for r in rows or []:
        t = str(r.get("t", "")).strip()
        bare = t.rstrip("∨∨vV▽▼⌄>›· 。")
        if bare in FOLD_TEXTS:
            return {"t": t, "x": center_x(r), "y": int(r.get("y") or 0),
                    "box": [int(r.get("x") or 0), int(r.get("y") or 0), int(r.get("x2") or 0)]}
    return None


def expand_panel(dev, rows, log, scans_dir):
    """收起态点「展开」(≈1253,2491)；展开后行数应当明显变多。

    注意（2026-11-03 用户口径）：折叠不是在"进面板"这一步解决的 —— 速赚在前、展开在后，
    任务多了「展开」还会被挤到下面看不见。真正处理折叠的是 drive_panel 的「一页一页做」循环
    （见到「展开」才点，坐标取自当帧 OCR 盒，见 fold_button）。这个函数只服务调用它的老路径。
    """
    cands = [r for r in rows if str(r.get("t", "")).strip() == "展开"]
    rr = find_rule(dev, ("展开",))
    if not cands and not rr:
        return rows, "no-expand-button"
    before = len(parse_rows(rows))
    if rr:                                   # 规则库优先：用户教过「展开」在哪就点哪
        cx, cy = int(rr["x"]), int(rr["y"])
        log("[rule] 用规则「%s」点 (%d,%d)" % (rr.get("label") or "展开", cx, cy))
    else:
        r = cands[0]
        cx, cy = center_x(r), int(r["y"])
    if not (900 < cx < 1400) or not (1500 < cy < 3100):
        cx, cy = NAV_EXPAND
    log("[expand] 点「展开」(%d,%d)，展开前行数=%d" % (cx, cy, before))
    dev.tap(cx, cy)
    time.sleep(1.9)
    p = os.path.join(scans_dir, "_expand_%s.jpg" % datetime.now().strftime("%H%M%S"))
    dev.shot(p)
    rows2 = dev.ocr(p)
    after = len(parse_rows(rows2))
    return rows2, ("expanded %d->%d rows" % (before, after))


# --------------------------------------------------------------------------- #
# 全表扫描（R7：展开后一次性向下扫到底，全程不回滚）
# --------------------------------------------------------------------------- #
def full_scan(dev, what, log, scans_dir, max_screens=14, settle=0.9):
    per, screens = [], []
    prev_sig, prev_text = None, None
    t0 = time.time()
    last_rows = []
    for k in range(max_screens):
        live_write("scan", "%s 扫第 %d/%d 屏" % (what, k + 1, max_screens),
                   getattr(dev, "serial", ""))
        p = os.path.join(scans_dir, "_scan_%s_%02d.jpg" % (what, k))
        dev.shot(p)
        rows = dev.ocr(p)
        last_rows = rows
        cards = parse_rows(rows)
        sig = image_signature(p)
        text = text_of(rows)
        screens.append({
            "screen": k, "frame": os.path.basename(p), "ocr_n": len(rows), "text": text[:2000],
            "boxes": rows,
            "cards": [{kk: c[kk] for kk in ("y", "btn_x", "button", "action", "name", "sub",
                                            "n", "m", "doable", "kind", "why", "key")}
                      for c in cards],
        })
        per.append(cards)
        if k > 0 and (sig == prev_sig or text == prev_text):
            log("[scan] %s 第 %d 屏与上一屏一致 → 到底（共 %d 屏）" % (what, k, k + 1))
            break
        prev_sig, prev_text = sig, text
        dev.swipe(*SWIPE_DOWN)
        time.sleep(settle)

    table = merge_scan_rows(per)
    doc = {
        "tag": "%s_full" % what, "device": dev.serial, "what": what,
        "started": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(time.time() - t0, 1), "screens": len(screens),
        "progress": progress_in_text("".join(s["text"] for s in screens), what),
        "coin_total": coin_total(last_rows) if what == "coin" else None,
        "rows": table, "doable": [t for t in table if t["doable"]],
        "screen_index": screens,
    }
    path = os.path.join(scans_dir, "%s_%s.json" % (doc["tag"], datetime.now().strftime("%H%M%S")))
    jdump(path, doc)
    log("[scan] %s 全表 %d 屏 → %d 行（可做 %d）：%s"
        % (what, len(screens), len(table), len(doc["doable"]), os.path.basename(path)))
    return doc, path


# --------------------------------------------------------------------------- #
# 落地动作：淘宝连滑 / 支付宝只滑 / 搜一搜热词 chip（R3 R4）
# --------------------------------------------------------------------------- #
def browse_dwell(dev, secs, shots_dir, tag, before=None, start_ts=None, start_basis=""):
    """淘宝内落地：一次调用连滑满 N 秒（正好匹配任务要求，中间不读屏）。
    2026-11-02 用户要求：滑动时长要和任务要求的浏览时间匹配，不要浪费时间；
    也不要为了"取证"多拍一张前置截图（那要多花 2-3 秒）。

    before：已经有落地确认帧时直接拿它当 before 证据，**绝不再拍一帧**（快速路径，第 6 条）。
    first_swipe_ms：第一次滑动发出前的时刻（第 6 条 tap->首次滑动 ≤2000ms 的取证口径）。
    start_ts / start_basis：2026-11-03（A）停留计时锚点 = 落地确认那一刻。给了 start_ts 就以
    「start_ts + secs」为截止时刻（锚点之后已经过去的等待不重复等），不再从本函数入口起算。
    """
    need = float(secs)          # 正好等于任务要求时长，不再 +1 秒
    a = before or os.path.join(shots_dir, "_dwell_%s_before.jpg" % tag)
    b = os.path.join(shots_dir, "_dwell_%s_after.jpg" % tag)
    if not before:
        dev.shot(a)
    t0 = time.time()
    anchor = float(start_ts) if start_ts else t0
    end = anchor + need
    k = 0
    first_swipe_ms = None
    while time.time() < end:
        if first_swipe_ms is None:
            first_swipe_ms = int(time.time() * 1000)
        dev.swipe(*SWIPE_DOWN)
        k += 1
        time.sleep(0.35)
    dur = round(time.time() - anchor, 2)
    waited = round(max(0.0, end - t0), 2)
    dev.shot(b)
    delta = pix_diff(a, b)
    # 源 p4/drv3.py dwell_swipe()：滑动到底/页面没动时 pix_delta 会掉到 3 以下，
    # 那说明这一段「滑动停留」其实没在翻页，证据里要留痕（判据照样按时间走）。
    return {"mode": "slide", "requested_s": round(need, 1), "actual_s": dur, "swipes": k,
            "pix_delta": delta, "moved": bool(delta >= MIN_MOVE_PIX),
            "first_swipe_ms": first_swipe_ms, "before_reused": bool(before),
            "start_basis": start_basis or "dwell_entry", "start_ts": round(anchor, 3),
            "waited_s": waited,
            "before_png": os.path.basename(a), "after_png": os.path.basename(b)}


def swipe_only_dwell(dev, secs, shots_dir, tag, mode="alipay", baseline_words=()):
    """只连滑、零点击，中途抽检一次弹窗（源: logs/r2_expand.py swipe_only()）。

    落到支付宝/天猫这类「同一家授权通道」时用：滑满 N+1 秒就算浏览，任何按钮都不点，
    一旦抽检到弹窗就立刻 BACK 退出，绝不带着遮罩继续滑。
    """
    need = float(secs) + 1.0
    t0 = time.time()
    k = 0
    mid = None
    first_swipe_ms = None
    probe = {"words": [], "static_excluded": [], "checked": False}
    while time.time() - t0 < need:
        if not probe["checked"] and (time.time() - t0) > need * 0.45:
            mid = os.path.join(shots_dir, "_%s_mid_%s.jpg" % (mode, tag))
            dev.shot(mid)
            got = popup_probe(mid, baseline_words=baseline_words)
            probe = dict(got, checked=True)
            if got["words"]:
                dev.back()                     # 弹窗立即退出，绝不点
                time.sleep(1.5)
                break
        if first_swipe_ms is None:
            first_swipe_ms = int(time.time() * 1000)
        dev.swipe(*SWIPE_DOWN)
        k += 1
        time.sleep(0.35)
    return {"mode": mode, "requested_s": round(need, 1),
            "actual_s": round(time.time() - t0, 2), "swipes": k,
            "first_swipe_ms": first_swipe_ms,
            "popup": probe["words"], "popup_static_excluded": probe["static_excluded"],
            "mid_png": os.path.basename(mid) if mid else None}


def alipay_dwell(dev, secs, shots_dir, tag, baseline_words=()):
    """R3：支付宝内**只连滑**，任何按钮都不点；中途抽检弹窗词，命中立即 BACK。

    弹窗探针已按 logs/r2_corr212.py 修正：只看对话框那一条带，而且点前就在的页面静态
    文字（「开通」这类）不算弹窗 —— 原来把静态文字判成弹窗，白废掉一整轮。
    """
    return swipe_only_dwell(dev, secs, shots_dir, tag, mode="alipay",
                            baseline_words=baseline_words)


def search_submit(dev, shots_dir, tag):
    """R4：提交一个热词 chip，并自证真的进了结果页。

    三步（每一步都是归档脚本里踩出来的）：
      1. 常规：只用「搜索发现 / 历史搜索」区块下的 chip（不选页面标题，round-034 事故）
      2. 兜底：一个 chip 都选不出来时，先点搜索输入框（源 prelude.py：落在搜索页
         却不把关键词送进框里，怎么滑都不算完成），再取一次 chip
      3. 优先商品类安全词（源 logs/r2_search3.py SAFE），避开「居家/数码」这类栏目名
    结果页自证：词被回显 + 结果页指纹 ≥ MIN_RESULT_MARKS 个。
    """
    p0 = os.path.join(shots_dir, "_search_%s_landing.jpg" % tag)
    dev.shot(p0)
    rows = dev.ocr(p0)
    chips, why = chip_candidates(rows)
    box_tapped = None
    if not chips:
        box, cands = find_search_box(rows)
        if box is not None:
            dev.tap(box["x"], box["y"])
            box_tapped = dict(box, candidates=[c["t"] for c in cands[:6]])
            time.sleep(2.0)
            p0 = os.path.join(shots_dir, "_search_%s_boxed.jpg" % tag)
            dev.shot(p0)
            rows = dev.ocr(p0)
            chips, why = chip_candidates(rows)
    if not chips:
        return {"submitted": False, "reason": "no usable hot-word chip (%s)" % why,
                "candidates": [], "search_box": box_tapped}
    safe = [c for c in chips if any(w in c["t"] for w in SEARCH_SAFE_CHIP_WORDS)]
    ch = (safe or chips)[0]
    dev.tap(ch["x"], ch["y"])
    time.sleep(2.4)
    p1 = os.path.join(shots_dir, "_search_%s_results.jpg" % tag)
    dev.shot(p1)
    rows1 = dev.ocr(p1)
    t1 = text_of(rows1)
    echoed = ch["t"] in t1
    markers = [w for w in RESULT_MARKS + ("购买",) if w in t1]
    # 2026-11-04：价格条数也是「结果页出来了」的硬证据（商品网格 = 一排排价格）。
    price_n = t1.count("￥") + t1.count("¥")
    ok_marks = (len(markers) >= MIN_RESULT_MARKS) or (price_n >= MIN_PRICE_TOKENS)
    # 2026-11-04 用户拍板：结果页顶上淘宝自己的横幅写着「浏览N秒可领 …」，那是这一页的**真实要求**。
    # 横幅就在这张已经 OCR 过的结果页里 -> 抠数字零额外开销；读不到就返回 None，调用方沿用原秒数。
    _mb = re.search("浏览\\s*(\\d{1,3})\\s*秒", t1)
    banner_secs = int(_mb.group(1)) if _mb else None
    return {"submitted": bool(echoed and ok_marks), "chip": ch["t"],
            "banner_secs": banner_secs,
            "chip_xy": [ch["x"], ch["y"]], "echoed": echoed, "markers": markers,
            "price_n": price_n,
            "safe_chip": bool(safe), "search_box": box_tapped,
            "landing_png": os.path.basename(p0), "results_png": os.path.basename(p1),
            "candidates": [c["t"] for c in chips[:6]]}


def game_page_hint(rows):
    """这一屏像不像淘宝内置小游戏（消消乐/关卡页）。返回命中的特征，空串 = 不像。

    2026-11-04 真机（手机2）：「玩消消乐领金币(0/1)」进去后有教程浮层（「滑这里」），
    它把系统 BACK 全吞了 —— 4 次 BACK 都没退出来，最后只能冷启动 force-stop 重启淘宝。
    """
    t = text_of(rows)
    m = re.search("第\\s*\\d+\\s*关", t)
    if m:
        return m.group(0)
    for w in ("滑这里", "再玩", "关卡", "得分"):
        if w in t:
            return w
    return ""


def back_to_panel(dev, what, log, scans_dir, maxback=4, settle=0.5, first_check=False):
    """滑完**直接**按系统返回键回面板，再截图判面板；不行才 am start 拉回（R6）。

    用户 2026-11-02 要求（原话）：「滑动完就直接返回，滑动可以延迟个 0.5 秒再返回，大概率就是
    任务页面，直接识图判断任务完成情况就行了，不是任务页面再考虑规则那些，如果是搜索页其实都
    可以再多返回一次，规则主要是来判断弹窗的」。

    所以这条路径**不再碰规则库**（旧实现在每次截图判面板之后先 click_by_rule(("返回",))）：
      - 教过的「返回」箭头坐标 (95,249) 在商品页/搜索页上不是返回键（点了也回不去，
        还会把真正的 keyevent BACK 一直挡住 —— 真机 4 次 BACK 全变成点那个坐标）；
      - 每轮省掉 1 次多余 OCR（约 3 秒）+ 1 次无效点击。
    规则库留着判弹窗（dismiss_popup_if_any / 教学），不再管「回面板」。

    顺序：sleep(settle=0.5) -> dev.back() -> shot + OCR 判 panel_ok；
    不在面板就**再**按一次 BACK（搜索页 / 图片搜索页多返一次就回面板），循环上限仍是 maxback。

    first_check=False：调用点都是「刚滑完停留」，人不可能已经在面板上（先查一帧纯属白花 3 秒）。
    将来若有「可能本来就在面板」的调用方，传 first_check=True 让它先看一眼再按返回。
    """
    seq = []
    rows = []
    backs = 0
    if first_check:
        p0 = os.path.join(scans_dir, "_back_%s_pre.jpg" % what)
        dev.shot(p0)
        rows = dev.ocr(p0)
        if panel_ok(rows, what)[0]:
            return True, 0, seq, rows
    # 2026-11-04 用户拍板（方案甲）：像小游戏页 + 这台机器登记了 game_close -> **先点一次关闭键**，
    # 比连按 4 次 BACK 快约 5 秒，也不会掉进「force-stop 重启淘宝」那条路（真机那次花了 ~75 秒）。
    # 没有坐标 / 不像游戏页 -> 一个字都不变，照旧走下面的 BACK 阶梯。
    try:
        _gp = os.path.join(scans_dir, "_back_%s_game.jpg" % what)
        dev.shot(_gp)
        _grows = dev.ocr(_gp)
        _ghint = game_page_hint(_grows)
        _gkey, _gtab = device_coords(dev)
        _gc = (_gtab or {}).get("game_close")
        if _ghint and _gc:
            log("[back] 这一屏像小游戏页（命中 %r）-> 点关闭键 (%d,%d)"
                % (_ghint, int(_gc[0]), int(_gc[1])))
            dev.tap(int(_gc[0]), int(_gc[1]))
            time.sleep(1.5)
            _gp2 = os.path.join(scans_dir, "_back_%s_gameclose.jpg" % what)
            dev.shot(_gp2)
            _grows2 = dev.ocr(_gp2)
            if panel_ok(_grows2, what)[0]:
                log("[back] 关闭键有效 -> 已回面板，不再按 BACK（省掉 force-stop 重启）")
                return True, 0, seq + ["game-close"], _grows2
            log("[back] 关闭键点了但仍不在面板 -> 照旧走 BACK 阶梯")
    except Exception as _ge:
        log("[back] 游戏页关闭键尝试失败（不影响原流程）：%s: %s" % (type(_ge).__name__, _ge))
    for i in range(maxback):
        if i == 0:
            # 用户要求：滑动完延 0.5 秒再返回（页面停稳，大概率已经是任务页）
            time.sleep(settle)
        dev.back()                       # 系统返回键：这条路上唯一的「回去」动作
        backs += 1
        time.sleep(1.8)                  # 每次 BACK 之间保留原来的等待
        p = os.path.join(scans_dir, "_back_%s_%d.jpg" % (what, i))
        dev.shot(p)
        rows = dev.ocr(p)
        if panel_ok(rows, what)[0]:
            return True, backs, seq, rows
        hit_words = [w for w in OVERLAY_BLOCK_WORDS if w in text_of(rows)]
        if hit_words:
            # 用户 2026-11-02 批准；已知案例：解锁新车轮弹窗 —— 用 BACK 会掉回首页，
            # 必须点「确认选择」把它关掉。所以先试点批准的关闭文案，读不到就继续 BACK。
            # 注意：这一支是**读屏判出来的浮层**，不是规则库，保留。
            btn, _cands = overlay_close_button(rows)
            if btn:
                log("[back] 浮层挡路（%s）→ 按用户批准点「%s」(%d,%d)"
                    % (",".join(hit_words), btn["t"], btn["x"], btn["y"]))
                dev.tap(btn["x"], btn["y"])
                time.sleep(1.5)
                continue
            log("[back] 命中挡路浮层（%s）但读不到可点的关闭文案 → 交给下一次 BACK"
                % ",".join(hit_words))
            continue
        seq.append(dev.focus_short())
    p = os.path.join(scans_dir, "_back_%s_last.jpg" % what)
    dev.shot(p)
    rows = dev.ocr(p)
    if panel_ok(rows, what)[0]:
        return True, maxback, seq, rows
    log("[back] %d 次 BACK 仍未回到面板 → am start 拉回（不 force-stop）" % maxback)
    dev.am_start_taobao()
    time.sleep(3.5)
    # 2026-11-04 真机（用户抓到的 bug）：am start 会把淘宝**原来那一页**拉到前台（保留栈内页面），
    # 所以拉回来时经常**已经在面板上了**。旧代码不看一眼就接着跑写死导航 —— 在面板上按首页坐标
    # 盲点「领淘金币」→「去赚金币」，反而把面板点掉，紧接着被判「拉不回」→ 冷启动 force-stop。
    # 现在：am start 之后**先判一次**，在面板就直接返回接着做；确实不在面板才走写死序列。
    p = os.path.join(scans_dir, "_back_%s_amstart.jpg" % what)
    dev.shot(p)
    rows = dev.ocr(p)
    if panel_ok(rows, what)[0]:
        try:
            _foc = dev.focus_short()
        except Exception:
            _foc = "?"
        log("[back] am start 之后已经在面板上（焦点=%r）-> 直接接着做，不再重跑写死导航" % _foc)
        return True, maxback + 1, seq + ["am-start"], rows
    nav_until_panel(dev, what, log, scans_dir)
    p2 = os.path.join(scans_dir, "_back_%s_amstart2.jpg" % what)
    dev.shot(p2)
    rows2 = dev.ocr(p2)
    return panel_ok(rows2, what)[0], maxback + 2, seq + ["am-start", "written-nav"], rows2


# --------------------------------------------------------------------------- #
# 运行上下文 / 证据轮
# --------------------------------------------------------------------------- #
MIN_ROUND_LEFT_S = 90        # 剩余预算不够跑一轮（含落地停留）就收尾
MAX_SCROLL_STEPS = 14        # 「一页一页做」时最多往下滚几屏（保险丝；与 full_scan 的 max_screens 同量级）
#: --minutes <= 0 = 不设时间上限（2026-11-03 用户要求：dash 传 --minutes 0 就是「不限时间」）。
#: 实现方式：把 deadline 推到这么远，ctx.left() 就是一个很大的值 —— MIN_ROUND_LEFT_S 的
#: 预算检查永不触发，只在「面板到底收工 / --max-rounds / 脚本队列跑完」时停。
#: 不设上限时 --max-rounds（默认 200）就是唯一保险丝，所以它的默认值一个字都不动。
UNLIMITED_LEFT_S = 10.0 ** 9     # 约 31 年


class Ctx:
    def __init__(self, outdir, dev, args):
        self.args = args
        self.dev = dev
        self.outdir = outdir
        self.rounds_dir = os.path.join(outdir, "rounds")
        self.scans_dir = os.path.join(outdir, "scans")
        self.ledger = os.path.join(outdir, "ledger.jsonl")
        self.log_path = os.path.join(outdir, "run.log")
        os.makedirs(self.rounds_dir, exist_ok=True)
        os.makedirs(self.scans_dir, exist_ok=True)
        self.t0 = time.time()
        # 2026-11-03 用户要求：--minutes 0（或负数）= 不设时间上限。
        # 注意：--minutes 的默认值仍是 60.0，只有显式传 0 才会不限时间。
        try:
            _m = float(getattr(args, "minutes", 60.0) or 0.0)
        except (TypeError, ValueError):
            _m = 60.0
        self.unlimited = bool(_m <= 0)
        self.deadline = self.t0 + (UNLIMITED_LEFT_S if self.unlimited else _m * 60.0)
        self.stop_reason = ""
        self.safety_stop = False
        #: 连续几轮拿到全黑帧（息屏）—— 见 frame_black / screen_off_verdict
        self.screen_off = 0
        #: 跳转类任务"没计数"的重试计数（用户 2026-11-03 口径：保持 5 秒但失败立刻重试一次）
        self.jump_retry = {}
        self.rounds = []
        self.scans = {"before": {}, "after": {}}
        self.give_up = {}
        #: 修 2（2026-11-04 真机事故）：同一行**本轮的尝试次数**，点击之前就 +1
        #: （升档重试 / jump 立刻重试 / 普通失败三条路径都算在内）。
        #: 到 MAX_ROW_TRIES 就不再点第 4 次，直接 give_up=2 换下一行。
        self.try_count = {}
        #: B（2026-11-03）：上一轮「没做完 -> 停留升档」留下的**一次性**提示 —— 下一轮优先重做
        #: 同一行，按更高一档再停一次（见 one_round 的 pick 与 dwell_escalation）
        self.retry_prefer = None
        #: B 的保险丝：这一行本轮已经升过几档（到 DWELL_LADDER_MAX_STEP=2 就不再升）
        self.dwell_steps = {}
        #: 2026-11-03 用户授权（第 7 条）：本次运行已经点过几次「顶部累计奖励」的「立即领取」
        #: （上限 CLAIM_TOP_MAX_TAPS；失败/点空一律收手，绝不反复怼同一个按钮）
        self.claim_top_taps = 0
        # 恢复阶梯的重启额度（每轮重置一次；RECOVER_RESTART_MAX=1）
        self.nav_budget = {"left": RECOVER_RESTART_MAX}
        self.n_round = self._existing_rounds()
        if self.unlimited:
            # 日志一口气写清「什么时候才会停」，免得看日志的人以为脚本挂了
            self.log("[budget] --minutes %s -> 本次不设时间上限（只在面板到底收工 / "
                     "--max-rounds=%s / 脚本队列跑完时停）"
                     % (getattr(args, "minutes", 0), getattr(args, "max_rounds", "?")))

    def log(self, msg):
        line = "%s %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        try:
            with open(self.log_path, "a", encoding="utf8") as f:
                f.write(line + chr(10))
        except Exception:
            pass
        if not getattr(self.args, "quiet", False):
            print(line, flush=True)

    def left(self):
        """剩余预算秒数。--minutes<=0 时 deadline 被推到 UNLIMITED_LEFT_S 之后 ->
        返回一个很大的值（MIN_ROUND_LEFT_S 的预算检查永不触发）。"""
        return self.deadline - time.time()

    def elapsed(self):
        return time.time() - self.t0

    def _existing_rounds(self):
        n = 0
        if os.path.isdir(self.rounds_dir):
            for d in os.listdir(self.rounds_dir):
                m = re.match(r"round-(\d+)$", d)
                if m:
                    n = max(n, int(m.group(1)))
        return n


class Round:
    """一轮 = 六件套落盘。任何时候出错都要能走到 verdict()（R8）。"""

    def __init__(self, ctx, what, kind="row"):
        ctx.n_round += 1
        self.n = ctx.n_round
        self.ctx = ctx
        self.what = what
        self.dir = os.path.join(ctx.rounds_dir, "round-%03d" % self.n)
        os.makedirs(self.dir, exist_ok=True)
        self.rec = {"round": self.n, "what": what, "kind": kind, "writer": "auto.py",
                    "device": getattr(ctx.dev, "serial", ""),
                    "device_time_start": ctx.dev.device_time() if ctx.dev else "",
                    "dir": os.path.relpath(self.dir, os.path.dirname(ctx.outdir) or ".")}
        self.t0 = time.time()

    def p(self, name):
        return os.path.join(self.dir, name)

    def grab(self, dev, img_name, ocr_name):
        """截图 + OCR 一并落盘，返回 (rows, package, activity)。"""
        p = self.p(img_name)
        dev.shot(p)
        rows = dev.ocr(p)
        pkg, act = dev.app_current()
        jdump(self.p(ocr_name), {"image": img_name, "n": len(rows), "focus": dev.focus(),
                                 "package": pkg, "activity": act, "boxes": rows})
        return rows, pkg, act

    def verdict(self, ok, reason, status, **extra):
        self.rec.update({"ok": bool(ok), "status": status, "reason": reason,
                         "duration_s": round(time.time() - self.t0, 1),
                         "device_time_end": self.ctx.dev.device_time() if self.ctx.dev else ""})
        self.rec.update(extra)
        jdump(self.p("06_verdict.json"), self.rec)
        try:
            with open(self.ctx.ledger, "a", encoding="utf8") as f:
                f.write(json.dumps(self.rec, ensure_ascii=False) + chr(10))
        except Exception:
            pass
        self.ctx.rounds.append(self.rec)
        self.ctx.log("[round-%03d] %s ok=%s status=%s | %s"
                     % (self.n, self.what, self.rec["ok"], status, reason))
        return self.rec


def leave_foreign_app(dev, ctx, landing_focus=""):
    """点后落到别的 App：立刻退出来，绝不在别人的 App 里滑动（源 logs/r2_expand.py）。"""
    seq = []
    for _ in range(3):
        dev.back()
        time.sleep(1.0)
        try:
            seq.append(str(dev.focus_short()))
        except Exception:
            seq.append("")
        if classify_landing(dev.focus()) == "taobao":
            break
    ctx.log("[landing] 点后落在 %s（既不是淘宝也不是支付宝/天猫）→ 已连按 BACK 退出"
            % str(landing_focus)[:40])
    return {"mode": "skip", "foreign_exit_seq": seq, "actual_s": 0, "swipes": 0,
            "first_swipe_ms": None,
            "basis": "落在 %s（既不是淘宝也不是支付宝/天猫）→ 立刻退出，本行不再重复"
                     % str(landing_focus)[:40]}


def frame_black(path):
    """这一帧是不是全黑（息屏的典型症状）。只看这一帧的 16x16 缩略图，**绝不额外截图**。

    读不到帧一律返回 False：读不到 ≠ 黑屏（各自的失败判据照旧说话）。
    """
    try:
        from PIL import Image
        im = Image.open(path)
        return max(im.convert("L").resize((16, 16)).tobytes()) < BLACK_FRAME_MAX
    except Exception:
        return False


def rewrap_screen(dev, log=print, tag=""):
    """疑似息屏：唤醒 + 等 BLACK_REWAKE_S（只把屏幕叫醒，不改任何业务判定）。"""
    ok = False
    try:
        ok = bool(dev.wake())
    except Exception as e:
        if callable(log):
            log("[screen] %s wake() 失败：%s" % (tag, e))
    time.sleep(BLACK_REWAKE_S)
    if callable(log):
        log("[screen] %s 拿到全黑帧（疑似息屏）-> 已唤醒（wake=%s）+ 等 %.1fs 后重拍一次"
            % (tag, ok, BLACK_REWAKE_S))
    return True


def screen_off_verdict(r, ctx, dev, what, where, extra=None, log=None):
    """全黑帧（息屏）的统一收尾：**设备问题 ≠ 任务问题**。

    - status="screen-off"：独立状态，不算 skipped、不算失败、不猜成败；
    - 不给这一行 give_up+1（不扣分）、不进恢复阶梯（BACK / 重启淘宝都治不了息屏）；
    - 连续 SCREEN_OFF_MAX_ROUNDS 轮全黑 -> safety_stop（main 退 2 = 停下等人）。
    """
    ctx.screen_off = int(getattr(ctx, "screen_off", 0) or 0) + 1
    fields = {"frame_black": True, "rewoke": True, "landing": "unknown", "on_panel": None,
              "row_after": [None, None], "row_present_after": None,
              "progress_after": None, "coin_after": None, "row_delta": None,
              "progress_delta": None, "coin_delta": None,
              "dwell": {"mode": "screen-off", "basis": "屏幕是黑的，判定无效",
                        "actual_s": 0, "swipes": 0, "first_swipe_ms": None}}
    fields.update(extra or {})
    r.verdict(False, "%s：屏幕是黑的，判定无效（frame_black=true, rewoke=true）" % where,
              "screen-off", **fields)
    if ctx.screen_off >= SCREEN_OFF_MAX_ROUNDS:
        ctx.safety_stop = True
        ctx.stop_reason = ("连续 %d 轮拿到全黑帧（息屏/锁屏）-> 判定无效，停下等人"
                           % ctx.screen_off)
        if callable(log):
            log("[STOP] %s" % ctx.stop_reason)
    return {"status": "screen-off", "ok": False}


def landing_pixel_gate(before_path, land_path, sig_before=None, source="full"):
    """2026-11-02 用户要求（第 8 条）：进入浏览滑动之前，必须先有「已离开面板」的像素证据。

    只比像素指纹（region_signature + signature_similarity，约 0.1s），绝不 OCR：
    面板与部分浏览落地页是同一个 TMSActivity（com.taobao.themis.container.app），
    靠 focus 分辨不出来，只能看像素。
    still_panel=True 时调用方**不许滑**（记 skipped）；读不到也算没证据（查不到 ≠ 安全）。
    sig_before 可传入点前已算好的 01_before.png 指纹（快速路径就是靠这个不占 2 秒预算）。
    source/frame_* 会写进 06_verdict.json 的 landing_pixel，说明用的是哪一帧、分数多少。
    """
    out = {"source": source,
           "before_frame": os.path.basename(before_path),
           "land_frame": os.path.basename(land_path),
           "same_panel_score": LANDING_SAME_PANEL_SCORE}
    try:
        d = signature_similarity(sig_before or region_signature(before_path),
                                 region_signature(land_path))
        out.update(d)
        out["still_panel"] = bool(d.get("score") is None
                                  or d["score"] >= LANDING_SAME_PANEL_SCORE)
        out["why"] = ("点后仍在面板，未落地（像素指纹 %s ≥ %s）"
                      % (d.get("score"), LANDING_SAME_PANEL_SCORE)
                      if d.get("score") is not None else
                      "点后那一帧读不到，证明不了已离开面板（查不到 ≠ 安全）")
    except Exception as e:
        out.update({"score": None, "bad_ratio": None, "still_panel": True,
                    "error": "%s: %s" % (type(e).__name__, e),
                    "why": "像素指纹算不出（%s: %s）：证明不了已离开面板"
                           % (type(e).__name__, e)})
    return out


def am_start_taobao_and_wait(dev, log, tag=""):
    """C（2026-11-03）：am start 回淘宝 + 轮询 focus（0.2s/次，上限 3 秒）。

    这等于「点一下淘宝图标」：Android 会把淘宝 Task 整体提到前台、**保留栈内页面**，
    不用一层层退栈（对比 BACK 最多 JUMP_BACK_MAX=6 次 × JUMP_BACK_SETTLE_S=0.8s ≈ 4.8s），
    而且不按 BACK 就不会动到被切走那个 App 的页面状态。
    实测启动 Activity：com.taobao.taobao/com.taobao.tao.welcome.Welcome（见 TAOBAO_WELCOME）。
    成功判据（用户 2026-11-03 口径）：焦点落到 **TMSActivity（淘金币/赛车面板）** = 成功，直接用。
    落不到就当没成：冷启到淘宝首页 TBMainActivity、停在欢迎页、还留在别人的 App 里 —— 一律
    由调用方**退回现有的 BACK 阶梯**（JUMP_BACK_MAX 那套），不许直接放弃。

    S2（用户 2026-11-03 拍板）：**焦点包名是 com.taobao.taobao 就立刻停止等待**。
      焦点 = TMSActivity          -> 已在面板，直接用它（上面那条，不用 BACK）；
      焦点 = 淘宝包下的**其他** activity（TBMainActivity / TaoLiveVideoActivity /
             FluidVideoActivity / H5 容器页）-> 立刻停止等待（am_early_stop=true），
             但仍由调用方走 BACK 阶梯回面板；**这里不会再发第二次 am start**。
    返回 {"cmd","am_start_ms","focus","ok","focus_seq","waited_ms",
          "am_early_stop","landed_activity","landed_pkg"}。
    """
    cmd = "am start -n %s" % TAOBAO_WELCOME
    t0 = time.time()
    seq, act, ok = [], "", False
    early, pkg_seen = False, ""
    try:
        dev.shell(cmd)
    except Exception as e:               # am start 发不出去也不许直接放弃：照旧退回 BACK 阶梯
        log("[jump] %s am start 发不出去（%s: %s）-> 退回 BACK 阶梯"
            % (tag, type(e).__name__, e))
    while True:
        pkg, act = focus_pkg_activity(dev)   # 一次 dumpsys 同时拿包名 + 短名（S2）
        pkg_seen = pkg or pkg_seen
        seq.append(act)
        if act and act in FOCUS_ACTS_TMS:        # 落到淘金币/赛车面板 = 成功，直接用
            ok = True
            break
        if pkg and pkg == TAOBAO_PKG:
            # S2：焦点已经在**淘宝包内**了 —— 不管它是 TBMainActivity、TaoLiveVideoActivity、
            # FluidVideoActivity 还是 H5 容器页，再发一次 am start 也不会换页，继续干等满 3 秒
            # 纯属浪费 -> 立刻停止等待。只有 TMSActivity 才算「已回到任务面板」（上面那一条）；
            # 淘宝包下的其它 activity 一律交回调用方走 BACK 阶梯（这里绝不再发 am start）。
            early = True
            break
        if act and act in FOCUS_ACTS_VISIT:  # 已回到淘宝但不是面板（如冷启到首页）-> 不再干等
            break
        if time.time() - t0 >= AM_START_MAX_S:
            break
        time.sleep(AM_START_POLL_S)
    ms = int((time.time() - t0) * 1000)
    landed = act or ""
    if ok:
        why = "落到淘金币/赛车面板，直接用"
    elif early:
        why = ("焦点已在淘宝包内（%s/%s）-> 立刻停止等待，交给 BACK 阶梯回面板"
               "（不再发第二次 am start）" % (pkg_seen or "?", landed or "?"))
    elif act and act in FOCUS_ACTS_VISIT:
        why = "落到淘宝别的页面（例如冷启到首页 TBMainActivity，不是任务面板）-> 退回 BACK 阶梯"
    else:
        why = "没落到淘宝 -> 退回 BACK 阶梯"
    log("[jump] %s 回淘宝优先 am start：cmd=%s -> %dms 后焦点=%r 包名=%r"
        "（%s；am_early_stop=%s；focus 序列 %s）"
        % (tag, cmd, ms, landed or "?", pkg_seen or "?", why, early,
           "/".join([s or "?" for s in seq[-5:]])))
    return {"cmd": cmd, "am_start_ms": ms, "focus": landed, "ok": ok,
            "focus_seq": seq[-8:], "waited_ms": ms,
            "am_early_stop": bool(early), "landed_activity": landed, "landed_pkg": pkg_seen}


def jump_back_to_taobao(dev, log, tag="", landing_app="", max_back=None, stay_s=0,
                        start_ts=None, start_basis=""):
    """R15：跳转类任务**一次都不滑**，回任务页（零 OCR，只看 focus）。

    用户 2026-11-02 真机口径：「跳转app去逛的不用滑的，如果非要滑也行，就是耽误点时间，
    其实跳转后直接返回到任务页就做完了，有时会要多返回几步，因为跳转可能会辗转多个窗口」。
    回到 TBMainActivity / TMSActivity 之一就立刻停，**不再多拍一帧**：紧接着的复核帧本来
    就要抓帧 + OCR，顺带判面板与行是否消失。

    2026-11-03 两处改动：
      C：先发 am start 回淘宝（= 点一下淘宝图标，见 am_start_taobao_and_wait），
         **落不到任务面板才退回 BACK 阶梯**（JUMP_BACK_MAX 那套），不许直接放弃；
      A：静止停留从「落地确认」那一刻起算（start_ts），不再从本函数入口/tap 起算；
      B：stay_s 由 one_round 按阶梯表给（跳转类 0 -> 5 -> 8 秒）。
    返回 dwell 证据（mode=jump-no-slide）：backs / back_ms / landing_app / back_ok / focus_seq
    / am_start_ms / am_start_cmd / am_start_ok / back_fallback_used / start_basis。
    back_ok=False 表示 am start + BACK 到上限还没回淘宝 -> 调用方交给恢复阶梯（别自己再瞎点）。
    """
    max_back = JUMP_BACK_MAX if max_back is None else int(max_back)
    allow = FOCUS_ACTS_HOME + FOCUS_ACTS_TMS
    t0 = time.time()
    act = focus_activity(dev)
    seq = [act]
    backs = 0
    back_ok = bool(act and act in allow)
    log("[jump] %s 跳转类任务（落地 activity=%r）：按 R15 一次都不滑；"
        "先在目标页静止停留 %.1fs（计时要走），再 am start 回淘宝（落不到才退回 BACK，最多 %d 次）"
        % (tag, landing_app or act or "?", float(stay_s or 0), max_back))
    stayed = 0.0
    if stay_s and float(stay_s) > 0:
        # 2026-11-02 用户：「逛一逛的时间改成停留5秒 —— 进去我看见有个5秒的计时」。
        # 跳转类不滑动，但计时要走：原地静止等满，再退出。
        # A（2026-11-03）：等满 secs 以**落地确认**（start_ts）为锚点，不是从现在起算。
        anchor = float(start_ts) if start_ts else time.time()
        end = anchor + float(stay_s)
        t_stay = time.time()
        time.sleep(max(0.0, end - t_stay))
        stayed = round(time.time() - anchor, 2)
        log("[jump] %s 已在目标页静止停留 %.2fs（从落地确认锚点算起，不滑动）-> 现在回任务页"
            % (tag, stayed))
    elif not stay_s:
        log("[jump] %s 阶梯第 0 档（停留 0 秒）：落地确认后直接回任务页，不等" % tag)
    am = {"cmd": "", "am_start_ms": 0, "focus": "", "ok": False, "focus_seq": [], "waited_ms": 0}
    back_fallback = False
    if not back_ok:
        am = am_start_taobao_and_wait(dev, log, tag)
        seq.extend(am.get("focus_seq") or [])
        act = am.get("focus") or act
        back_ok = bool(am.get("ok"))
        if not back_ok:
            back_fallback = True
            log("[jump] %s am start 没落到任务面板（焦点=%r）-> 退回 BACK 阶梯（最多 %d 次，"
                "不许直接放弃）" % (tag, act or "?", max_back))
    while not back_ok and backs < max_back:
        dev.back()
        backs += 1
        time.sleep(JUMP_BACK_SETTLE_S)
        act = focus_activity(dev)
        seq.append(act)
        back_ok = bool(act and act in allow)
        log("[jump] %s 第 %d/%d 次 BACK 后焦点=%r（落地 activity=%r）-> %s"
            % (tag, backs, max_back, act, landing_app or "?",
               "已回到淘宝，停" if back_ok else "还在别的 App，继续"))
    if not back_ok:
        log("[jump] %s am start + %d 次 BACK 还没回淘宝（最后焦点=%r）-> 交给恢复阶梯，不再自己瞎点"
            % (tag, backs, act))
    return {"mode": "jump-no-slide", "backs": backs,
            "back_ms": int((time.time() - t0) * 1000),
            "landing_app": str(landing_app or ""), "actual_s": stayed, "swipes": 0,
            "first_swipe_ms": None, "back_ok": back_ok, "focus_seq": seq[-8:],
            "requested_s": stay_s,
            "am_start_cmd": am.get("cmd") or "", "am_start_ms": am.get("am_start_ms") or 0,
            "am_start_ok": bool(am.get("ok")), "am_focus": am.get("focus") or "",
            # S2（用户 2026-11-03）：焦点已在淘宝包内 -> 立刻停止等待；落地的那个 activity
            # 一并留证（TaoLiveVideoActivity / FluidVideoActivity 这类淘宝自家页面）。
            "am_early_stop": bool(am.get("am_early_stop")),
            "landed_activity": am.get("landed_activity") or act or "",
            "landed_pkg": am.get("landed_pkg") or "",
            "back_fallback_used": back_fallback,
            "start_basis": start_basis or "jump_entry",
            "start_ts": round(float(start_ts), 3) if start_ts else 0.0}


def landing_watch(dev, log, before_path, sig_before, focus_before, tap_ms, grab, paths,
                  max_wait=None, early_s=None, tag=""):
    """tap 之后判「到底有没有落地」：先轮询焦点（零 OCR），焦点一直是同一屏才用像素兜底。

    2026-11-02 20:53 真机误判后改（详见 LANDING_FOCUS_MAX_S 上方）：旧口径 tap 后只
    sleep(0.2) 就抓帧，淘宝拉起支付宝要 1 秒以上，那一帧与面板逐像素相同 → 误判「没落地」。
    现在的顺序：
      1. 每次约 0.2s 读一次 focus：焦点变成**别的 activity**（尤其支付宝/别的 App）
         → 立刻认定「已落地」（抓一帧留证，不拿像素拦它）；
      2. 焦点仍是面板那一屏 → 继续等，最多 LANDING_FOCUS_MAX_S=3 秒；
      3. 等满 3 秒才用像素指纹下结论：≥ LANDING_SAME_PANEL_SCORE 才算「可能没落地」；
      4. 中途（约 0.7s）先花一帧看一眼：屏幕已经变了等于落地，不必每次白等 3 秒。
    只读 focus + 算指纹：**不 OCR、不点击、不滑动**。
    paths = {"early": …, "later": …, "changed": …}：三条分支各用一条帧路径。
    返回 dict：landed / why / gate / frame / frame_meta / focus_before / focus_after /
              focus_changed / focus_reads / waited_ms / judged_at_ms（相对 tap 的毫秒）。
    """
    max_wait = LANDING_FOCUS_MAX_S if max_wait is None else float(max_wait)
    early_s = LANDING_EARLY_PIXEL_S if early_s is None else float(early_s)
    t0 = time.time()
    out = {"landed": False, "why": "", "gate": None, "early": None, "frame": None,
           "frame_meta": None, "focus_before": str(focus_before or ""),
           "focus_after": str(focus_before or ""), "focus_changed": False,
           "focus_reads": 0, "waited_ms": 0, "judged_at_ms": 0, "landed_at_ms": 0,
           "frame_black": False, "rewoke": False, "screen_off": False}

    def _shot(path):
        out["frame"] = path
        out["frame_meta"] = grab(path) or {}
        if frame_black(path):
            # 息屏时这一帧是纯黑：既不能当「已落地」，也不能当「没落地」——
            # 先唤醒 + 重拍一次；还黑就整轮作废（status=screen-off，不扣分、不滑）。
            rewrap_screen(dev, log, "%s 落地帧" % (tag or "?"))
            out["frame_meta"] = grab(path) or {}
            out["frame_black"] = True
            out["rewoke"] = True
            if frame_black(path):
                out["screen_off"] = True
        return out["frame_meta"]

    while True:
        act = focus_activity(dev)
        out["focus_reads"] += 1
        if act:
            out["focus_after"] = act
        if act and act != str(focus_before or ""):
            out["focus_changed"] = True
            out["landed"] = True
            _shot(paths["changed"])
            if out["screen_off"]:
                out["landed"] = False
                out["why"] = ("tap 后 %.2fs 焦点已变（%r），但落地帧全黑（息屏）-> 判定无效"
                              % (time.time() - t0, act))
                log("[landing] %s %s" % (tag, out["why"]))
                break
            # 这一帧只是留证：焦点都变了，**绝不**再拿像素拦一次（那正是 20:53 的坑）
            out["gate"] = dict(landing_pixel_gate(before_path, paths["changed"],
                                                 sig_before=sig_before,
                                                 source="fast-focus-changed"),
                               informational_only=True)
            out["why"] = ("tap 后 %.2fs 焦点已变：%r -> %r -> 已落地（不看像素，直接开滑）"
                          % (time.time() - t0, focus_before, act))
            log("[landing] %s %s" % (tag, out["why"]))
            break
        waited = time.time() - t0
        if out["early"] is None and waited >= early_s and not out["screen_off"]:
            _shot(paths["early"])
            if out["screen_off"]:
                out["why"] = ("tap 后 %.2fs 帧全黑（息屏）-> 判定无效" % waited)
                log("[landing] %s %s" % (tag, out["why"]))
                break
            g = landing_pixel_gate(before_path, paths["early"], sig_before=sig_before,
                                   source="fast-early")
            out["early"] = g
            if not g["still_panel"]:
                out["landed"] = True
                out["gate"] = g
                out["why"] = ("tap 后 %.2fs 焦点仍是 %r，但屏幕已变（像素 %s < %s）-> 已落地"
                              % (waited, act or "?", g.get("score"),
                                 LANDING_SAME_PANEL_SCORE))
                log("[landing] %s %s" % (tag, out["why"]))
                break
        if waited >= max_wait:
            break
        time.sleep(LANDING_FOCUS_POLL_S)
    if not out["landed"] and not out["screen_off"]:
        # 焦点整整 3 秒没变：用**这一刻**的帧下结论（早先那一帧只作参考，不拿来定生死）
        _shot(paths["later"])
    if not out["landed"] and not out["screen_off"]:
        g = landing_pixel_gate(before_path, paths["later"], sig_before=sig_before,
                               source="fast")
        out["gate"] = g
        out["landed"] = not g["still_panel"]
        out["why"] = ("tap 后 %.1fs 焦点一直是 %r（未变），像素指纹 %s %s %s -> %s"
                      % (time.time() - t0, focus_before or "?", g.get("score"),
                         ">=" if g.get("still_panel") else "<", LANDING_SAME_PANEL_SCORE,
                         "可能没落地" if g.get("still_panel") else "已落地"))
        log("[landing] %s %s" % (tag, out["why"]))
    if out["landed"] and not out.get("landed_at_ms"):
        # A（2026-11-03）：记下「落地确认」那一刻（相对 tap 的毫秒）——停留计时从这里起算，
        # 不是从 tap 起算（用户原话：「计时启动很慢，刚开始计时1秒就返回了」）。
        out["landed_at_ms"] = int(time.time() * 1000) - int(tap_ms)
    out["waited_ms"] = int((time.time() - t0) * 1000)
    out["judged_at_ms"] = int(time.time() * 1000) - int(tap_ms)
    return out


def finish_not_landed(r, ctx, dev, what, card, action_doc, gate, tap_ms, secs, secs_src,
                      n0, m0, prog0, coin0, cards0, anchor):
    """点后 3 秒「焦点未变 + 像素未变」：先 BACK 一次再看行，仍不行才 skipped。

    用户 2026-11-02 追加的第二条（原话见 LANDING_FOCUS_MAX_S 上方）：即便判成「可能没落地」
    也不许立刻 skipped —— 先 BACK 一次看是否回到面板、行是否消失；**真没落地**才记 skipped，
    reason 里写全三项证据（焦点未变 / 像素未变 / BACK 一次仍不行）。
    成败口径一个字都不改：回到面板后「该行消失 / 计数 +1」才算完成（slot_verdict 直接用，
    绝不因为它"没滑"就扣成失败）。这里**永远不滑动**：没落地说明人还在面板上。
    """
    back_path = r.p("04b_after_back.png")
    t_back = time.time()
    dev.back()
    time.sleep(LANDING_BACK_SETTLE_S)
    meta = dev_pixel_frame(dev, back_path) or {}
    rows_b = dev.ocr(back_path)          # 唯一一次 OCR：只看「行还在不在」
    try:
        focus_now = dev.focus()
    except Exception:
        focus_now = ""
    jdump(r.p("05b_after_back_ocr.json"), {"image": os.path.basename(back_path),
                                          "n": len(rows_b), "focus": focus_now,
                                          "boxes": rows_b})
    gate_b = landing_pixel_gate(r.p("01_before.png"), back_path, source="fast-back")
    gate["after_back"] = dict(gate_b, frame=os.path.basename(back_path),
                              shot_channel=meta.get("channel"), shot_channel_ms=meta.get("ms"),
                              ms_since_tap=int(time.time() * 1000) - tap_ms,
                              back_ms=int((time.time() - t_back) * 1000))
    gate["focus_after_back"] = focus_activity(dev)
    why3 = ("点后 %.1f 秒焦点未变（%r）、像素未变（指纹 %s ≥ %s）、BACK 一次"
            % ((gate.get("judged_at_ms") or 0) / 1000.0, gate.get("focus_before") or "?",
               gate.get("score"), LANDING_SAME_PANEL_SCORE))
    timing = {"tap_ms": tap_ms, "first_swipe_ms": None, "tap_to_swipe_ms": None}
    action_doc.update(timing)
    jdump(r.p("03_action.json"), action_doc)
    text_b = text_of(rows_b)
    on_panel_b = panel_ok(rows_b, what)[0]
    base = {"landing": "panel", "on_panel": on_panel_b, "secs": secs, "secs_source": secs_src,
            "row_after": [None, None], "row_present_after": None, "progress_after": None,
            "coin_after": None, "row_delta": None, "progress_delta": None, "coin_delta": None,
            "dwell": {"mode": "skip", "basis": why3 + "（判成可能没落地）", "actual_s": 0,
                      "swipes": 0, "first_swipe_ms": None}}
    base.update(timing)
    if not on_panel_b:
        ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 1
        r.verdict(False, why3 + "，BACK 后那一帧也不是面板（焦点=%r）-> 真没落地：不滑"
                  % (gate.get("focus_after_back") or "?"), "skipped",
                  back_frame_on_panel=False, **base)
        return {"status": "skipped", "ok": False, "key": card["key"]}
    cards_b = parse_rows(rows_b)
    slot = slot_verdict(anchor, cards_b, text_after=text_b, panel_title_ok=True,
                        expected_rows=len(cards0))
    prog1 = progress_in_text(text_b, what)
    coin1 = coin_total(rows_b) if what == "coin" else None
    prog_delta = (prog1 - prog0) if (prog0 is not None and prog1 is not None) else None
    coin_delta = (coin1 - coin0) if (coin0 is not None and coin1 is not None) else None
    present = any(c["key"] == card["key"] for c in cards_b)
    srow = slot.get("row")
    n1, m1 = ((srow.get("n"), srow.get("m")) if srow else (None, None))
    row_delta = (n1 - n0) if (n0 is not None and n1 is not None) else None
    r.rec["slot"] = dict(slot["evidence"], verdict=slot["verdict"], reason=slot["reason"])
    base.update({"row_after": [n1, m1], "row_present_after": present,
                 "progress_after": prog1, "coin_after": coin1, "row_delta": row_delta,
                 "progress_delta": prog_delta, "coin_delta": coin_delta})
    done = bool(slot["verdict"] == "disappeared" or (row_delta or 0) > 0
                or (prog_delta or 0) > 0 or (coin_delta or 0) > 0)
    if done:
        # 用户口径：任务真做完了 —— 行消失 / 计数增加就算完成，绝不因为早先那次像素闸
        # 判过「没落地」就把它记成 skipped（2026-11-02 20:53 丢的就是这种轮次）。
        r.verdict(True, why3 + "，但回面板后行已消失/计数增加 -> 判定完成（没滑也算）；"
                  "row %r %s/%s -> %s/%s (d=%s)；slot=%s(%s)"
                  % (card["name"][:26], n0, m0, n1, m1, row_delta,
                     slot["verdict"], slot["reason"][:70]),
                  "disappeared", landed_after_back=True, **base)
        return {"status": "disappeared", "ok": True, "key": card["key"]}
    ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 1
    r.verdict(False, why3 + "仍不行（行 %r 还在：%s/%s -> %s/%s，slot=%s）：没落地，不滑"
              % (card["name"][:26], n0, m0, n1, m1, slot["verdict"]), "skipped", **base)
    return {"status": "skipped", "ok": False, "key": card["key"]}


def _skip_on_panel(r, ctx, card, action_doc, why, tap_ms, secs, secs_src,
                   n0, m0, prog0, coin0, extra=None):
    """第 8 条的统一收尾：没落地 -> 不滑、回面板、give_up+1、记 skipped。"""
    ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 1
    timing0 = {"tap_ms": tap_ms, "first_swipe_ms": None, "tap_to_swipe_ms": None}
    action_doc.update(timing0)
    jdump(r.p("03_action.json"), action_doc)
    fields = {"landing": "panel", "row_after": [n0, m0], "row_present_after": True,
              "on_panel": True, "progress_after": prog0, "coin_after": coin0,
              "row_delta": 0, "progress_delta": 0, "coin_delta": 0,
              "secs": secs, "secs_source": secs_src,
              "dwell": {"mode": "skip", "basis": why, "actual_s": 0, "swipes": 0,
                        "first_swipe_ms": None}}
    fields.update(extra or {})
    fields.update(timing0)
    r.verdict(False, why + "：不滑", "skipped", **fields)
    return {"status": "skipped", "ok": False, "key": card["key"]}


# --------------------------------------------------------------------------- #
# 一轮执行（R1 R2 R3 R4 R8）
# --------------------------------------------------------------------------- #
def product_card_points(dev):
    """这台设备的商品卡固定点击点。返回 (分辨率键, 点元组 or None)。

    None = 这台设备没登记（或登记了但没有 product_cards）-> **一次点击都不发**（R14 同一条铁律：
    未登记分辨率不许瞎猜，宁可这一行不做）。
    """
    key, table = device_coords(dev)
    if not table:
        return key, None
    pts = table.get("product_cards")
    if not pts:
        return key, None
    return key, tuple((int(p[0]), int(p[1])) for p in pts)


def product_page_progress(dev, path):
    """读任务页自己那行进度「点3个商品，得30淘金币（x/N）」。返回 (x|None, N|None)。"""
    x, n, _y = product_page_progress_pos(dev, path)
    return x, n


def product_page_progress_pos(dev, path):
    """同上，但**连进度那一行在帧里的 y 一起读**（E 的实测证据，见 product_click_flow）。

    返回 (x|None, N|None, y|None)：y 是那行文字盒的顶边（绝对坐标，0~3200）。
    用途：下次真机复跑时，用「每做完一张卡、BACK 回原商品页后 y 变没变、变了多少」
    来**用数据回答**「列表会不会把卡片上顶」，而不是靠谁回忆/推测。
    """
    try:
        rows = ocr_band(dev, path, PRODUCT_PROGRESS_BAND[0], PRODUCT_PROGRESS_BAND[1])
    except Exception:
        return None, None, None
    for row in rows:
        m = PRODUCT_PROGRESS_RE.search(str(row.get("t") or ""))
        if m:
            return int(m.group(1)), int(m.group(2)), int(row.get("y") or 0)
    return None, None, None


def product_progress_wait(dev, r, base, max_tries, tag="", label="enter", poll_s=None, log=None):
    """抓帧**直到**能读到任务页进度（need 非 None），最多 max_tries 次 —— 缺陷 1 的修法。

    用户原话（2026-11-03）：「那就多等一下，刷新了再抓取」。进任务页**立刻**抓的那一帧页面
    还在半加载（真机证据 round-095/096：08_product_page.png 只有标题 + 橙色头图，白屏，
    progress 那一行还没渲染），product_page_progress() 因此全程返回 None。
    这里改成轮询抓帧（间隔 PRODUCT_READY_POLL_S，上限 PRODUCT_READY_MAX_TRIES）：
    **读到 need 才算页面就绪**，读到了才记 enter 帧、才开始点。

    读不到也照常返回（不能因为读不到就不做）—— 调用方按 ready=False 走 B 规则
    （不许盲减，改成主动前进固定点）。
    返回 {"ready","tries","ms","done","need","band_y","frame"}；frame = 最后一次抓的帧文件名。
    """
    poll = PRODUCT_READY_POLL_S if poll_s is None else float(poll_s)
    tmax = max(1, int(max_tries))
    t0, tries, x, n, y = time.time(), 0, None, None, None
    path = r.p(base + ".png")
    for i in range(tmax):
        tries = i + 1
        path = r.p(base + ".png") if tries == 1 else r.p("%s_try%d.png" % (base, tries))
        try:
            dev.shot(path)
            x, n, y = product_page_progress_pos(dev, path)
        except Exception:
            x, n, y = None, None, None
        if n is not None:
            break
        if tries < tmax:
            time.sleep(poll)
    ms = int(round((time.time() - t0) * 1000))
    out = {"ready": n is not None, "tries": tries, "ms": ms,
           "done": x, "need": n, "band_y": y, "frame": os.path.basename(path)}
    if log:
        log("[product] %s %s 抓帧 %d 次 / %dms -> 页面进度 %s/%s（page_ready=%s）"
            % (tag, label, tries, ms, x, n, "true" if out["ready"] else "false"))
    return out


def product_click_flow(dev, ctx, r, card):
    """「点击商品领优惠红包(0/1)」专用流程（2026-11-03 用户真机拍板，见 PRODUCT_CLICK_WORD 注释）。

    进来时行尾「去完成」已经点过，人就在任务页（= 原商品页）上。这里做四件事：
      1. **先等页面就绪（读到进度行）再抓 enter 帧**，最多等 PRODUCT_READY_MAX_TRIES 次
         （2026-11-03 缺陷 1：以前立刻抓帧，页面还在半加载，进度全程读成 None）；
      2. 按**固定坐标**（DEV_COORDS[分辨率]["product_cards"]）点商品卡，不靠 OCR 找卡片；
         N = 副标题里的「点击N个商品」（读不到才退到页面上的进度分母）；
      3. 每点开一张：焦点变成商品详情页（TTDetailActivity 等）就**只发 BACK**（详情页内零点击），
         回到原商品页后再点下一张；
      4. 点完（或页面进度到 N/N）再 BACK 一次离开任务页，交给 one_round 的公共收尾（回面板 + 复核）。

    读不到进度时（B，2026-11-03 用户拍板）：**不许 blind 减**（remaining -= 1 那种）。每次
    BACK 回原商品页都重读一次进度，读不到就主动把固定点前进到下一个并继续点；只有「读到进度且
    remaining == 0」或「点满 need 发且每发都成功打开过详情页」才算做完，绝不谎报。

    三条硬约束（违反任何一条就不点）：
      * 未登记分辨率的设备：一发都不发（R14）；
      * 焦点不在 PRODUCT_PAGE_ACTS 上（例如没落到原商品页、或停在详情页/面板上）：先只按 BACK，
        退不出来就收手，绝不在别的页面上发商品卡点击；
      * 一辆车最多发 PRODUCT_MAX_TAPS 发（保险丝）。
    """
    tag = "r%03d-product" % r.n
    need = int(card.get("need") or 0)
    t0 = time.time()
    rec = {"mode": "product-click", "need": need, "taps": [], "backs": 0,
           "page_progress": [], "resolution": "", "points": []}
    key, pts = product_card_points(dev)
    rec["resolution"] = key
    if not pts:
        ctx.log("[product] %s 分辨率 %r 没登记商品卡坐标 -> 一次点击都不发（R14）："
                "这一行不做，换下一行" % (tag, key or "?"))
        rec["skipped"] = "no-coords"
        ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 2
        jdump(r.p("08_product_click.json"), rec)
        return rec
    rec["points"] = [list(p) for p in pts]
    # 1) **先等页面就绪、再抓 enter 帧**（2026-11-03 缺陷 1 的根因修法）。
    #    以前是进页面立刻抓帧：那时页面还在半加载（白屏 + 标题 + 橙色头图），读不到进度 ->
    #    round-095/096 的 done/need/band_y 全 null -> 只能靠 remaining -= 1 盲减。
    w0 = product_progress_wait(dev, r, "08_product_page", PRODUCT_READY_MAX_TRIES,
                               tag=tag, label="enter", log=ctx.log)
    x0, n0, y0 = w0["done"], w0["need"], w0["band_y"]
    rec["page_ready"] = bool(w0["ready"])
    rec["ready_tries"] = int(w0["tries"])
    rec["ready_ms"] = int(w0["ms"])
    # E（补充条）：band_y = 任务页进度那一行在帧里的 y。每次回原商品页都记一次，
    # 用来实测「做完一张卡之后列表会不会上移」——那条「上顶 192px」曾未证实，2026-11-03
    # 真机截图已证实**两次进入之间整张卡会往上移**（首卡 iQOO -> 苏泊尔，见缺陷 2）。
    rec["page_progress"].append({"at": "enter", "frame": w0["frame"],
                                 "done": x0, "need": n0, "band_y": y0,
                                 "ready": rec["page_ready"], "tries": rec["ready_tries"]})
    total = int(n0) if n0 else need
    done = int(x0) if x0 else 0
    remaining = max(0, total - done) if total else need
    progress_ok = bool(w0["ready"])
    if progress_ok:
        ctx.log("[product] %s 任务页已就绪（等了 %d 次 / %dms）：页面进度 %s/%s，"
                "需要点 %s 张商品卡（固定坐标 %s）"
                % (tag, w0["tries"], w0["ms"], x0, n0, remaining, list(pts)))
    else:
        ctx.log("[product] %s 等了 %d 次 / %dms 仍读不到任务页进度（page_ready=false）-> "
                "照常继续（读不到也得做，绝不因为读不到就不做），下面按「读不到就前进固定点」"
                "接管，不再用盲减下结论" % (tag, w0["tries"], w0["ms"]))
    tries, pi = 0, 0
    opened_ok, closed_by = 0, ""
    while remaining > 0 and opened_ok < need and tries < min(PRODUCT_MAX_TAPS, need + 2):
        tries += 1
        act = focus_activity(dev)
        if focus_is_bad(act):
            ok, act, backs, _ = escape_bad_page(dev, ctx.log, tag, max_back=2)
            rec["backs"] += backs
            rec["taps"].append({"step": tries, "tap": None, "focus_before": act,
                                "why": "焦点在商品/下单类页面 -> 只按 BACK，未发点击"})
            if not ok:
                ctx.log("[product] %s 焦点 %r 退不出来 -> 收手（不再发任何点击）" % (tag, act))
                break
            time.sleep(PRODUCT_BACK_SETTLE_S)
            continue
        if not any(a in act for a in PRODUCT_PAGE_ACTS):
            # 不是「原商品页」就不点：可能没落地/已经回到面板，交给 one_round 的收尾去判
            ctx.log("[product] %s 焦点 %r 不是原商品页（%s）-> 不发商品卡点击，收手"
                    % (tag, act or "?", "/".join(PRODUCT_PAGE_ACTS)))
            rec["taps"].append({"step": tries, "tap": None, "focus_before": act,
                                "why": "焦点不在原商品页 -> 未发点击"})
            break
        # 固定点按顺序取。换下一个点的两种情形：
        #   a) 这一发点空了（没打开详情页）—— 老的安全闸，不依赖任何前提；
        #   b) BACK 回来**读不到进度**（缺陷 2 的修法）—— 真机截图已证实两次进入之间列表
        #      整张卡上移（首卡 iQOO -> 苏泊尔），而 (922,2234)/(922,2838) 全天一次都没用过；
        #      这时原地不动重试同一个点就是在赌「列表没动」，所以主动前进。
        # 读到进度且 remaining > 0 时**保持原样**（成功就一直用同一个点：同一个点 (922,1628)
        # 在 (0/3) 与 (1/3) 两帧都还能点中下一张，真机实测）。
        xy = pts[min(pi, len(pts) - 1)]
        dev.tap(xy[0], xy[1])
        time.sleep(PRODUCT_TAP_SETTLE_S)
        act2 = focus_activity(dev)
        ent = {"step": tries, "tap": [int(xy[0]), int(xy[1])],
               "focus_after": act2, "opened_detail": bool(focus_is_bad(act2)), "back": 0}
        if not ent["opened_detail"]:
            # 这一发没打开商品页（可能是列表上移、卡片高度不同、或页面还没排好）
            # -> 换下一个固定点（安全闸，不依赖任何关于「列表上移多少」的假设）。
            ent["note"] = "点空了（焦点没变成商品详情页）-> 换下一个固定点"
            rec["taps"].append(ent)
            pi += 1
            ctx.log("[product] %s 第 %d 发 (%d,%d) 没打开商品页（焦点=%r）-> 换下一个固定点"
                    % (tag, tries, xy[0], xy[1], act2 or "?"))
            # 安全闸：点空之后先确认脚下这一页**还是**原商品页（顶部进度还能读到）。
            # 读不到 = 可能被广告页/别的页面顶替了 —— 这时再按固定坐标点就是盲点，立刻收手。
            p_m = r.p("08c_product_miss_%d.png" % tries)
            try:
                dev.shot(p_m)
                mx, mn, ym = product_page_progress_pos(dev, p_m)
            except Exception:
                mx, mn, ym = None, None, None
            rec["taps"][-1]["band_y"] = ym
            if mx is None and mn is None:
                ctx.log("[product] %s 点空后读不到任务页进度 -> 脚下可能已经不是原商品页，"
                        "收手，不再发任何商品卡点击" % tag)
                rec["taps"][-1]["stopped"] = "page-not-recognised"
                break
            continue
        dev.back()                       # 详情页内**零点击**：只 BACK 回原商品页
        ent["back"] = 1
        rec["backs"] += 1
        time.sleep(PRODUCT_BACK_SETTLE_S)
        ent["focus_after_back"] = focus_activity(dev)
        rec["taps"].append(ent)
        opened_ok += 1
        # 2) BACK 回原商品页后**重新读一次进度**（读不到就按 A 的方式再等一下，最多 2 次）
        wx = product_progress_wait(dev, r, "08b_product_%d" % tries, PRODUCT_RECHECK_TRIES,
                                   tag=tag, label="after tap %d" % tries, log=ctx.log)
        xx, nn, yx = wx["done"], wx["need"], wx["band_y"]
        rec["page_progress"].append({"at": "after tap %d" % tries,
                                     "frame": wx["frame"],
                                     "done": xx, "need": nn, "band_y": yx,
                                     "ready": bool(wx["ready"]), "tries": int(wx["tries"])})
        ctx.log("[product] %s 第 %d 发 (%d,%d)：打开商品详情 -> 只按 BACK 回原商品页"
                "（详情页内零点击）；页面进度 %s/%s"
                % (tag, tries, xy[0], xy[1], xx, nn))
        if xx is not None and nn:
            remaining = max(0, int(nn) - int(xx))
            progress_ok = True
            ent["progress"] = [int(xx), int(nn)]
        else:
            # B（2026-11-03 用户拍板）：读不到进度**不许盲减**。
            # round-095 就是靠 remaining -= 1 在真实 2/3 时记成 remaining_after=0 收手，
            # 第三发重复点在了已经点过的同一张卡上（金币 d=0、当轮判失败），真正第 3 张
            # 拖到下一轮 round-096 才点成 —— 单轮闭环当时并不成立。
            # 现在改成：主动把固定点前进到下一个（真机截图已证实列表整张卡会上移，
            # 而 (922,2234)/(922,2838) 这两个点全天一次都没被用过），并继续点；
            # 收尾交给「点满 need 发且每发都成功打开过详情页」这条硬条件 + PRODUCT_MAX_TAPS 保险丝。
            progress_ok = False
            pi += 1
            ent["why"] = "progress-unreadable-advance"
            ent["note"] = "BACK 回来读不到进度 -> 不盲减；固定点前进到下一个（列表整张卡上移）"
            ctx.log("[product] %s 第 %d 发 BACK 回来仍读不到进度 -> 不盲减（remaining 仍 %s），"
                    "固定点前进到下一个 %s"
                    % (tag, tries, remaining, pts[min(pi, len(pts) - 1)]))
    # 4) 点完再 BACK 一次回面板（用户口径第 3 步）。回没回到面板只看 focus（dumpsys，零 OCR）：
    #    回到了就在这里收手，让 one_round 跳过 back_to_panel 的那一次多余 BACK
    #    （在面板上多按一次 BACK 会把面板按掉）；没回到就交给 back_to_panel 去按、去 OCR 复核。
    act = focus_activity(dev)
    seq = [act]
    if act and not any(a in act for a in FOCUS_ACTS_TMS) and not focus_is_bad(act):
        dev.back()
        rec["backs"] += 1
        time.sleep(PRODUCT_BACK_SETTLE_S)
        act = focus_activity(dev)
        seq.append(act)
        ctx.log("[product] %s 商品卡点完 -> BACK 一次回面板（焦点=%r）" % (tag, act or "?"))
    rec["focus_seq"] = seq
    rec["exit_focus"] = act
    rec["back_home"] = bool(act and any(a in act for a in FOCUS_ACTS_TMS))
    # 收尾口径（B）：只有「读到进度且 remaining == 0」或「已点满 need 发且每次发都成功打开过
    # 详情页」才算做完；保险丝到顶 / 焦点跑掉 / 点空后脚下不是原商品页 = 没收完（不谎报做完）。
    if remaining == 0:
        closed_by = "progress-zero"
    elif need > 0 and opened_ok >= need:
        closed_by = "need-opened"
    elif tries >= min(PRODUCT_MAX_TAPS, need + 2):
        closed_by = "fuse"
    else:
        closed_by = "stopped"
    rec["closed_by"] = closed_by
    rec["opened_detail"] = opened_ok
    # 读不到进度时 remaining 是个**不可信的推断**（绝不再拿它当「做完了」的证据）：
    # 此时 remaining_after 记 null，看证据的人一眼就知道这一轮是看详情页次数收的尾。
    rec["remaining_after"] = remaining if progress_ok else None
    rec["remaining_source"] = "page-progress" if progress_ok else "unreadable"
    rec["actual_s"] = round(time.time() - t0, 2)
    rec["requested_s"] = 0
    rec["swipes"] = 0
    jdump(r.p("08_product_click.json"), rec)
    return rec


def run_claim(dev, ctx, r, what, claim, rows0):
    """第 6 / 第 7 条**共用**的领取路径（kind=claim_task：一条代码，绝不写两套）。

    只做一件事：点那一枚「领取奖励 / 立即领取」，再判「奖励是不是真的到账」。
      - 一次都不滑、不进 dwell 阶梯、点后**零点击**（只有找不回面板时才发 BACK）；
      - 证据：03_action.json 与 06_verdict.json 都写 authorized_by_user + authorized_reason
        （审计要能回答「这条为什么点了领取」）；
      - 失败或点空：give_up +2 -> 这一枚这次不点了（pick_target 要求 give_up < 2 才会再选它），
        绝不反复怼同一个按钮。
    返回 {"status","ok","key"}，与 one_round 其它分支同构。
    """
    tag = "r%03d" % r.n
    slot = str(claim.get("slot") or "")
    auth = {"authorized_by_user": CLAIM_USER_AUTH,
            "authorized_reason": str(claim.get("reason") or ""),
            "authorized_item": str(claim.get("item") or ""),
            "claim": {"item": claim.get("item"), "kind": CLAIM_KIND, "slot": slot,
                      "button": claim.get("button"), "name": claim.get("name")}}
    if slot == "top_band":
        auth["claim"]["candidates"] = [{"x": int(c["x"]), "y": int(c["y"]),
                                        "title": c.get("title")}
                                       for c in (claim.get("candidates") or [])][:4]
    # R1 守卫只管「行尾按钮」那一套 x（[1245,1270]）；顶部那一排的「立即领取」不在这个坐标域
    # （真机实测 x=612/941/1267），所以那里只做屏幕 bounds 校验：越界就一次都不发。
    tx, ty = int(claim["x"]), int(claim["y"])
    if slot == "row_button":
        tx = guard_button_x(tx, str(claim.get("button") or ""), ty)
    _w = int(getattr(dev, "w", 0) or 0)
    _h = int(getattr(dev, "h", 0) or 0)
    if _w and _h and not (0 < tx < _w and 0 < ty < _h):
        r.verdict(False, "领取点 (%d,%d) 越出屏幕 %dx%d -> 一次都不点" % (tx, ty, _w, _h),
                  "claim-guard", **auth)
        return {"status": "claim-guard", "ok": False, "key": claim.get("key") or ""}
    n0, m0 = claim.get("n"), claim.get("m")
    prog0 = progress_in_text(text_of(rows0), what)
    coin0 = coin_total(rows0) if what == "coin" else None
    left0 = len(claim.get("candidates") or [])
    action_doc = {
        "intent": "claim %r of %r (%s, kind=%s)"
                  % (claim.get("button"), str(claim.get("name"))[:40],
                     claim.get("item"), CLAIM_KIND),
        "tap": [tx, ty],
        "coord_source": "01_before.png OCR box centre (re-located this round, never cached)",
        "at": datetime.now().isoformat(timespec="seconds"),
        "device_time": dev.device_time(),
        "secs": 0, "secs_source": "claim",
    }
    action_doc.update(auth)
    jdump(r.p("03_action.json"), action_doc)
    r.rec.update({"picked": {"key": claim.get("key") or "",
                             "name": claim.get("name") or "",
                             "button": claim.get("button"), "action": "button",
                             "kind": CLAIM_KIND, "n": n0, "m": m0,
                             "secs": 0, "secs_source": "claim",
                             "dwell_plan": {"mode": "claim-tap", "secs": 0,
                                            "cat": "claim", "level": 0},
                             "dwell_ladder": {"cat": "claim", "level": 0,
                                              "top": DWELL_LADDER_TOP,
                                              "max_step": DWELL_LADDER_MAX_STEP}},
                     "progress_before": prog0, "coin_before": coin0, "fast_path": True})
    r.rec.update(auth)
    tap_ms = int(time.time() * 1000)
    dev.tap(tx, ty)
    ctx.log("[claim] %s %s 授权领取：点 %r (%d,%d)，行=%r -> 等 %.1fs 看奖励是否到账"
            % (tag, claim.get("item"), claim.get("button"), tx, ty,
               str(claim.get("name"))[:24], CLAIM_SETTLE_S))
    time.sleep(CLAIM_SETTLE_S)
    # 领取本身不该跳页。万一焦点被带离面板：**只按 BACK**，期间零点击。
    backs, act = 0, focus_activity(dev)
    while act and act not in (FOCUS_ACTS_HOME + FOCUS_ACTS_TMS) and backs < CLAIM_BACK_MAX:
        backs += 1
        ctx.log("[claim] %s 焦点被带到 %r（不在面板）-> 只按 BACK（第 %d/%d 次），期间零点击"
                % (tag, act, backs, CLAIM_BACK_MAX))
        dev.back()
        time.sleep(JUMP_BACK_SETTLE_S)
        act = focus_activity(dev)
    rows1 = r.grab(dev, "07_postcheck.jpg", "07_postcheck_ocr.json")[0]
    if frame_black(r.p("07_postcheck.jpg")):
        rewrap_screen(dev, ctx.log, "%s 领取复核帧" % tag)
        rows1 = r.grab(dev, "07_postcheck.jpg", "07_postcheck_ocr.json")[0]
        r.rec["frame_black"] = True
        r.rec["rewoke"] = True
        if frame_black(r.p("07_postcheck.jpg")):
            return screen_off_verdict(r, ctx, dev, what, "领取复核帧",
                                      extra={"tap_ms": tap_ms, "secs": 0,
                                             "secs_source": "claim"}, log=ctx.log)
    # 面板判据沿用 one_round 的粘性口径：标题被滚出屏幕时（领取类行常在列表底部），
    # 只要还能读到 >=1 行、而且本轮早先确实在过这个面板，就仍按「在面板」处理 ——
    # 否则会把「领完金币 +10」这种成功误判成失败（23:18 真机 round-060 就是这样）。
    def _on_panel(rows_x):
        okp, info_x = panel_ok(rows_x, what)
        if not okp and info_x["rows"] >= 1 and what in getattr(ctx, "panel_seen", ()):
            ctx.log("[claim] %s 标题被滚出屏幕，但还能读到 %d 行 -> 仍按在面板处理"
                    % (tag, info_x["rows"]))
            return True
        return okp

    on_panel = _on_panel(rows1)
    if not on_panel and backs < CLAIM_BACK_MAX:
        # 典型：领完弹了个「恭喜获得 x 金币」浮层。**浮层上的按钮一个都不点**，只按 BACK 关掉它。
        backs += 1
        ctx.log("[claim] %s 复核帧不是面板（可能弹了奖励浮层）-> 只按 BACK（第 %d/%d 次），"
                "浮层上的按钮一个都不点" % (tag, backs, CLAIM_BACK_MAX))
        dev.back()
        time.sleep(JUMP_BACK_SETTLE_S)
        rows1 = r.grab(dev, "07b_postcheck2.jpg", "07b_postcheck2_ocr.json")[0]
        if frame_black(r.p("07b_postcheck2.jpg")):
            return screen_off_verdict(r, ctx, dev, what, "领取复核帧",
                                      extra={"tap_ms": tap_ms, "secs": 0,
                                             "secs_source": "claim"}, log=ctx.log)
        on_panel = _on_panel(rows1)
    prog1 = progress_in_text(text_of(rows1), what)
    coin1 = coin_total(rows1) if what == "coin" else None
    prog_delta = (prog1 - prog0) if (prog0 is not None and prog1 is not None) else None
    coin_delta = (coin1 - coin0) if (coin0 is not None and coin1 is not None) else None
    cards1 = parse_rows(rows1)
    n1 = m1 = None
    row_gone = done_lbl = False
    left1 = len(claim_top_candidates(rows1))
    if slot == "row_button":
        same = [c for c in cards1 if c["key"] == claim.get("key")]
        row_gone = not same
        if same:
            n1, m1 = same[0]["n"], same[0]["m"]
            _raw = str(same[0].get("raw") or "")
            done_lbl = bool(same[0]["button"] in DONE_LABELS
                            or "已领取" in _raw or "已完成" in _raw)
    row_delta = (n1 - n0) if (n0 is not None and n1 is not None) else None
    if not on_panel:
        if (coin_delta or 0) > 0:
            # 复核帧不满足面板判据，但金币总数确实涨了 —— 奖励到账是硬证据，照实记成功。
            ok, how = True, "复核帧不是面板，但金币 +%s（奖励确实到账）" % coin_delta
        else:
            ok, how = False, "复核帧不是面板（读不到任何到账证据）"
    elif slot == "row_button":
        if row_gone:
            ok, how = True, "这一行已消失（领完被抽走）"
        elif done_lbl:
            ok, how = True, "按钮已变成 已完成/已领取"
        elif (row_delta or 0) > 0:
            ok, how = True, "计数推进 %s -> %s" % (n0, n1)
        elif (coin_delta or 0) > 0:
            ok, how = True, "金币 +%s（奖励到账）" % coin_delta
        else:
            ok, how = False, "行还在、计数没动、金币没涨"
    else:
        if (coin_delta or 0) > 0:
            ok, how = True, "金币 +%s（顶部累计奖励到账）" % coin_delta
        elif left1 < left0:
            ok, how = True, "那一排可点按钮少了一枚（%d -> %d）" % (left0, left1)
        else:
            ok, how = False, "那一排没变化、金币也没涨"
    dwell = {"mode": "claim-tap", "actual_s": 0, "swipes": 0, "backs": backs,
             "requested_s": 0, "first_swipe_ms": None,
             "item": claim.get("item"), "button": claim.get("button"),
             "on_panel_after": bool(on_panel), "focus_after": act or "",
             "left_before": left0, "left_after": left1}
    reason = ("%s 领取 %r（行=%r）：%s；金币 %s->%s（d=%s）；计数 %s/%s -> %s/%s（d=%s）；"
              "复核帧在面板=%s；尾部 BACK %d 次（零点击）"
              % (claim.get("item"), claim.get("button"), str(claim.get("name"))[:26], how,
                 coin0, coin1, coin_delta, n0, m0, n1, m1, row_delta, on_panel, backs))
    r.rec["dwell"] = dwell
    r.rec["dwell_mode"] = "claim-tap"
    r.verdict(ok, reason, "claimed" if ok else "claim-failed",
              row_after=[n1, m1], row_present_after=bool(slot == "row_button" and not row_gone),
              progress_after=prog1, coin_after=coin1, coin_delta=coin_delta,
              row_delta=row_delta, on_panel=on_panel, secs=0, secs_source="claim",
              secs_actual_s=0, tap_ms=tap_ms, **auth)
    if ok:
        ctx.log("[claim] %s 领取成功：%s" % (tag, how))
    else:
        _k = str(claim.get("key") or "")
        ctx.give_up[_k] = ctx.give_up.get(_k, 0) + 2
        ctx.log("[claim] %s 没读到到账证据（%s）-> 收手：这一枚这次不点了，绝不反复怼同一个按钮"
                % (tag, how))
    return {"status": "claimed" if ok else "claim-failed", "ok": ok,
            "key": claim.get("key") or ""}


def one_round(dev, ctx, what):
    r = Round(ctx, what)
    # 每轮重启额度重置：恢复阶梯最多 force-stop 1 次（2026-11-02 19:17 用户：别无限重启）
    ctx.nav_budget["left"] = RECOVER_RESTART_MAX
    live_write("round", "第 %d 轮开始（%s）" % (r.n, what), getattr(dev, "serial", ""))
    pause_wait(ctx.log, getattr(dev, "serial", ""))
    try:
        # ---- 1) 点击前：截图 + OCR + R2 面板校验 ----
        rows0, pkg0, act0 = r.grab(dev, "01_before.png", "02_before_ocr.json")
        # 2026-11-03 真机：息屏时这一帧是纯黑，focus 却仍报旧 activity —— 别把它当「不在面板」
        # （那会白进恢复阶梯）。先唤醒 + 重拍一次；还黑就记 screen-off，本行不扣分、不进阶梯。
        if frame_black(r.p("01_before.png")):
            rewrap_screen(dev, ctx.log, "round-%03d 判面板帧" % r.n)
            rows0, pkg0, act0 = r.grab(dev, "01_before.png", "02_before_ocr.json")
            r.rec["frame_black"] = True
            r.rec["rewoke"] = True
            if frame_black(r.p("01_before.png")):
                return screen_off_verdict(r, ctx, dev, what, "判面板", log=ctx.log)
        else:
            ctx.screen_off = 0        # 这一轮开头是亮屏：连续计数清零
        # 用户显式授权（targets/jump_policy.json）：先把「打开其他应用」确认框按用户选择点掉，
        # 再看面板（否则看门狗会把它当危险框拦下来，整轮白停）。
        jump0 = handle_jump_dialog(dev, rows0, log=ctx.log)
        if jump0.get("seen"):
            r.rec["jump_dialog_before"] = jump0
        if jump0.get("ok"):
            rows0, pkg0, act0 = r.grab(dev, "01_before.png", "02_before_ocr.json")
        ok_panel, info = panel_ok(rows0, what)
        # 全表扫描会把面板标题滚出屏幕。若此时还用「标题在不在」判面板，
        # 就会「扫到底 -> 判面板丢失 -> 退回重进 -> 再扫」无限循环（2026-11-02 实测卡死 3 分钟）。
        # 因此：标题缺失但行数>=3 且至少 1 个按钮时，仍按"在面板"处理。
        # 2026-11-03 真机（手机2 淘金币）：速赚区显示的是「已完成」任务（没有按钮），
        # 往下滚一屏后**标题也滚出屏幕** -> title=False/rows=3/buttons=0 -> 被判「不在面板」
        # -> 恢复阶梯 force-stop 重开 -> 又是那 3 行 -> 再滚 -> 死循环（01:02~01:11 实测）。
        # 规则：本来就在这个面板里（panel_seen），只要还能读到 ≥1 行，就继续当在面板。
        if not ok_panel and info["rows"] >= 1 and what in getattr(ctx, "panel_seen", ()): 
            ctx.log("[panel] %s 标题被滚出屏幕，但还能读到 %d 行 -> 仍按在面板处理（不回恢复阶梯）"
                    % (what, info["rows"]))
            ok_panel = True
        if not ok_panel and info["rows"] >= 3 and info["buttons"] >= 1:
            ctx.log("[panel] 标题已滚出屏幕，但行/按钮齐全（rows=%d buttons=%d）-> 仍按在面板处理"
                    % (info["rows"], info["buttons"]))
            ok_panel = True
        if not ok_panel:
            # 2026-11-03 用户口径（手机2 真机）：折叠态面板**不是**流程被打乱 —— 标题在、
            # 任务行 0，但屏幕上明明有「展开」。这时绝不进恢复阶梯（那会白烧 40 秒甚至落到
            # 待教学）；按「这一屏没有可做行」处理，交给 drive_panel 的循环去点开它再看。
            fold0 = fold_button(rows0)
            if fold0 is not None:
                ctx.log("[panel] %s 读到折叠标记「%s」(%d,%d) -> 面板在、只是折叠"
                        "（rows=%d buttons=%d）：本屏按「没有可做行」处理，"
                        "由 drive_panel 点开它再看" % (what, fold0["t"], fold0["x"], fold0["y"],
                                                      info["rows"], info["buttons"]))
                ok_panel = True
                r.rec["panel_folded"] = {"text": fold0["t"],
                                         "box": [fold0["x"], fold0["y"]],
                                         "rows": info["rows"], "buttons": info["buttons"]}
        if not ok_panel:
            # 2026-11-03 真机（手机2 淘金币，01:49 重启后仍复现）：刚往下滚过一屏，
            # 这一帧落在空白区（rows=0、标题也没了）——不是"面板丢了"，硬进恢复阶梯就会
            # BACK×3 + force-stop 重开，重进后还是同一屏，永远出不来。
            # 这种情况**交回 drive_panel**，让它按"这一页做空了"继续滚/点展开/判到底。
            if getattr(ctx, "just_scrolled", False) and what in getattr(ctx, "panel_seen", ()):
                ctx.log("[panel] %s 刚滚过一屏、这帧读不到面板（rows=%d buttons=%d）"
                        "-> 不当成面板丢失，交给 drive_panel 继续（不回恢复阶梯）"
                        % (what, info["rows"], info["buttons"]))
                return {"status": "no-doable", "scrolled_blank": True}
            # 【任务识别】读不到面板/读不到行 = 流程被弹窗之类打乱了。
            # 2026-11-02 19:17 用户要求的恢复阶梯：BACK×3（每次 1 次 OCR）-> force-stop +
            # 写死序列重进（本轮最多 1 次）-> 仍不行落盘待教学并停手。不再「盲重进 2 次」。
            _, rows_r, how_r = recover_to_panel(dev, what, ctx.log, ctx.scans_dir,
                                                budget=ctx.nav_budget)
            r.rec["recovered_before"] = how_r
            # 阶梯给的是它自己那一帧；本轮证据帧（01_before/02_before_ocr）照旧自己抓
            rows0, pkg0, act0 = r.grab(dev, "01_before.png", "02_before_ocr.json")
            ok_panel, info = panel_ok(rows0, what)
            if not ok_panel:
                r.verdict(False, "R2 面板不在前台（标题=%s 行数=%d）→ 不点"
                          % (info["title"], info["rows"]), "no-panel", panel=info)
                return {"status": "no-panel"}
        # 确认在面板：记住它（滚屏后标题会被滚出屏幕，判据要放宽，见上面的 sticky 分支）
        try:
            ctx.panel_seen.add(what)
        except AttributeError:
            ctx.panel_seen = {what}
        text0 = text_of(rows0)

        # 跳转确认框：按 dash 面板上的策略点「仅打开一次 / 始终打开」（默认仅打开一次）
        if jump_prompt_apply(dev, rows0, ctx.log):
            rows0, pkg0, act0 = r.grab(dev, "01_before.png", "02_before_ocr.json")
            text0 = text_of(rows0)

        # ---- R5/R6：点前系统弹窗看门狗（安装/权限/跳转确认框）----
        dh = watchdog_hit(pkg0, act0, text0, rows=rows0)   # 源 v2/core/guard.py（通配+强/成对+fail-closed）
        if dh:
            r.verdict(False, "系统安装/权限/跳转确认框命中 %s → 不点不关，停下等人" % dh,
                      "system-dialog", hits=dh)
            ctx.safety_stop = True
            raise StopRun("系统弹窗：%s" % dh)

        # ---- 2) 挑行 + R1 守卫 ----
        cards0 = parse_rows(rows0)
        # ---- 2026-11-03 用户**明确授权**（交接文档「五、待办」第 7 条）：顶部累计奖励 ----
        # 只认「与今日累计奖励/累计任务奖励**同一 OCR 带**、文字**恰好**是「立即领取」、
        # 且在标题右侧」的那几枚（见 claim_top_candidates）；找不到这一排 -> 一个都不点，
        # 绝不放宽到面板其他位置的「立即领取」。
        # 一次运行最多点 CLAIM_TOP_MAX_TAPS 次（失败/点空一律收手，绝不反复怼同一个按钮）。
        if int(getattr(ctx, "claim_top_taps", 0) or 0) < CLAIM_TOP_MAX_TAPS:
            _top_claim = claim_top_card(rows0)
            if _top_claim is not None:
                ctx.claim_top_taps = int(getattr(ctx, "claim_top_taps", 0) or 0) + 1
                ctx.log("[claim] 第7条授权：顶部「%s」那一排读到 %d 枚「立即领取」-> 点最左那枚 "
                        "(%d,%d)（同一 OCR 带 |dy|<=%dpx；用户 %s 授权）"
                        % (_top_claim["name"], len(_top_claim["candidates"]),
                           _top_claim["x"], _top_claim["y"], CLAIM_TOP_BAND_DY,
                           CLAIM_USER_AUTH))
                return run_claim(dev, ctx, r, what, _top_claim, rows0)
        # dash 面板【做这个】指定的行：once 语义，优先做它；找不到/不合格就回退自动选择
        # B（2026-11-03）：上一轮「没做完 -> 升档」留了个一次性提示，优先重做同一行
        # （否则优先级排序会把它挤到后面，升了档却等不到重试）。取一次就清掉。
        _prefer, ctx.retry_prefer = getattr(ctx, "retry_prefer", None), None
        card = (hint_card(what, dev, cards0, ctx.log)
                or pick_target(cards0, ctx.give_up, prefer=_prefer))
        if card is None:
            r.verdict(False, "当前视野没有可做行（共 %d 行）" % len(cards0), "no-doable",
                      visible=[{"name": c["name"][:40], "kind": c["kind"], "why": c["why"],
                                "btn": c["button"]} for c in cards0][:12])
            return {"status": "no-doable"}
        # 2026-11-03 用户口径 ①：只放行**已安装**的外部 App（微博/百度/头条/夸克/支付宝家族）。
        # 没装的话点下去只会跳安装页或空白页，白费一轮 —— 先查一次包名（进程内缓存），没装就跳过这一行。
        if card.get("ext_pkg") and not ext_app_installed(dev, card["ext_pkg"]):
            ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 2   # 别在同一行上反复试
            r.verdict(False, "外部 App 未安装（%s，%s）-> 不点，换下一行"
                      % (card.get("ext_visit") or "?", card["ext_pkg"]),
                      "ext-missing")
            return {"status": "ext-missing", "ok": False, "key": card["key"]}
        # 修 2（2026-11-04 真机事故的主修）：同一行本轮的尝试次数硬上限。
        # 升档重试（retry_prefer）/ jump 立刻重试 / 普通失败三条路径都会把这一行重新送回来，
        # 所以闸口放在**每一次真正点下去之前**，而不是放在某一条失败分支里。
        # 落在 pick_target 之外的行（dash 面板【做这个】指定的 hint_card）也照样拦，
        # 不许从侧门挤进第 4 次。
        _tries0 = int(ctx.try_count.get(card["key"], 0) or 0)
        if _tries0 >= MAX_ROW_TRIES:
            ctx.give_up[card["key"]] = max(int(ctx.give_up.get(card["key"], 0) or 0), 2)
            r.rec["try_cap"] = {"tries": _tries0, "max": MAX_ROW_TRIES,
                                "gave_up": int(ctx.give_up[card["key"]] or 0),
                                "gate": "before-tap"}
            r.verdict(False, "本轮已尝试 %d 次（上限 %d）-> 记放弃，换下一行"
                      % (_tries0, MAX_ROW_TRIES), "try-cap")
            ctx.log("[cap] %s 本轮已尝试 %d 次（上限 %d）-> 记放弃，换下一行"
                    % (str(card["name"])[:20], _tries0, MAX_ROW_TRIES))
            return {"status": "try-cap", "ok": False, "key": card["key"]}
        if card["kind"] == CLAIM_KIND:
            # 2026-11-03 用户明确授权（第 6 条）：这一行末端就是「领取奖励」->
            # 走和第 7 条**同一条**领取路径（claim_task 一个 kind，绝不写两套）。
            return run_claim(dev, ctx, r, what, claim_row_card(card), rows0)
        tx = guard_button_x(card["btn_x"], card["button"], card["btn_y"])   # R1，越界抛
        ty = card["btn_y"]
        n0, m0 = card["n"], card["m"]
        # 源 _archive/root_py/run_energy.py plan()：浏览N秒=持续滑动、玩N秒=静止、去逛=静止
        # （原来一律「连滑 N+1 秒」，于是「玩N秒」类任务被滑走页面，计时反而不走）
        # 2026-11-02 用户要求（第 2/3 条）：秒数在扫描阶段（parse_rows/classify_card）就定死，
        # 点进去直接用；副标题读不到秒数时才查同族缓存，并留下来源（sub/cache/default）。
        if card["kind"] == "product_click":
            # 2026-11-03 用户授权（第 3 条）：这条不靠停留计时（点完 N 张卡就回面板），
            # 所以不进 dwell_plan / 不动 secs_cache；证据里的 secs 写 0。
            plan = {"mode": "static", "secs": 0,
                    "why": "点击商品流程：不靠停留计时，只点 N 张商品卡"}
            secs_src, secs_detail = "product-click", {"secs_cache_level": "none"}
        else:
            plan, secs_src, secs_detail = resolve_dwell_plan(
                card, force_secs=int(getattr(ctx.args, "dwell", 0) or 0) or None)
        secs = plan["secs"]
        # B（2026-11-03 用户拍板）：这一行的阶梯档位（0 = 起步档；非 0 = 同族/memory 里学到的高档位）
        ladder_cat = str(plan.get("cat") or "default")
        ladder_level = dwell_ladder_level(plan.get("level") or 0, ladder_cat)
        # 点前对话框带内的弹窗词基线（源 logs/r2_corr212.py：页面静态文字不算弹窗）
        base_popup_words = words_in_band(rows0, POPUP_PROBE_WORDS)
        prog0 = progress_in_text(text0, what)
        coin0 = coin_total(rows0) if what == "coin" else None
        # 槽位锚点：点前按行序记下几何 + 计数 + 整盘行数
        # （源 _archive/v2/percept/racing_rows.py capture_slot）
        slot_index = next((i for i, c in enumerate(cards0) if c is card), 0)
        anchor = capture_slot(cards0, slot_index)
        # 2026-11-03 用户授权（第 8 条）：**只有**因为本次授权放宽了词才放行/点击的轮次，
        # 证据里才写 authorized_by_user + authorized_reason（审计要能回答「这条为什么放开」）。
        # 没放宽的轮次一个字段都不写（不留空串，免得 grep 时混进来）。
        auth_ev = {}
        if card.get("relaxed_words"):
            auth_ev = {
                "authorized_by_user": RELAX_USER_AUTH,
                "relaxed_words": list(card["relaxed_words"]),
                "authorized_reason": "明写浏览%d秒+纯浏览流程+用户%s授权放宽'%s'"
                                     % (secs, RELAX_USER_AUTH, "/".join(card["relaxed_words"])),
            }
            ctx.log("[relax] 词级放宽：放宽词=%s 行=%r -> 明写浏览%d秒 + 纯浏览流程，"
                    "不点商品/不点任何按钮（用户 %s 授权）"
                    % ("/".join(card["relaxed_words"]), card["name"][:30], secs, RELAX_USER_AUTH))
        # D（2026-11-03）：行名/副标题含 理财/基金/保险/贷款/借款/分期/信用卡 且被放行（走纯浏览）
        # 的轮次，补审计标记 —— 只补标记，**拦截判定一个字都没改**（现在能做就继续能做）。
        if card.get("finance_auth"):
            auth_ev.update({
                "authorized_by_user": card["finance_auth"],
                "authorized_reason": card.get("finance_reason") or "",
                "finance_words": list(card.get("finance_words") or []),
            })
            ctx.log("[finance] 金融类行放行审计（只看不点）：行=%r 命中=%s -> 用户 %s 授权"
                    % (card["name"][:30], "/".join(card.get("finance_words") or []),
                       card["finance_auth"]))
        # 修 3 / 修 5（2026-11-04 用户拍板）：领取型**入口**按钮 / 「点击去逛」忽略视频·直播 的行，
        # 证据里必须能回答「这一条为什么放开」—— 只补字段，拦截与点击判定一个字都没改。
        if card.get("authorized_by_user") and card.get("authorized_reason"):
            auth_ev.update({
                "authorized_by_user": card["authorized_by_user"],
                "authorized_reason": card["authorized_reason"],
            })
            if card.get("ignore_words"):
                auth_ev.update({"ignored_danger_words": list(card["ignore_words"]),
                                "ignore_rule": card.get("ignore_reason") or ""})
            if card.get("ext_pkg"):
                auth_ev.update({"claim_entry": {"ext_visit": card.get("ext_visit"),
                                                "ext_pkg": card["ext_pkg"]}})
            ctx.log("[auth] %s -> 授权（%s）"
                    % (str(card.get("name"))[:30], card["authorized_reason"][:120]))
        if card["kind"] == "product_click":
            # 2026-11-03 用户授权（第 3 条）：这条走的不是「纯浏览」，是「点 N 张商品卡」，
            # 证据里必须写清是哪一条授权、流程长什么样（审计要能回答「为什么这次点了商品」）。
            auth_ev = {
                "authorized_by_user": PRODUCT_CLICK_AUTH,
                "relaxed_words": [PRODUCT_CLICK_WORD],
                "authorized_reason": ("用户%s授权（交接文档五、待办第3条）：点击商品领优惠红包 —— "
                                      "点行尾按钮进原商品页，按固定坐标点 %d 张商品卡，"
                                      "每点开一张只发 BACK，商品详情页内零点击"
                                      % (PRODUCT_CLICK_AUTH, int(card.get("need") or 0))),
                "product_click": {"need": int(card.get("need") or 0),
                                  "rule": card.get("click_rule") or ""},
            }
            ctx.log("[product] 第3条授权：行 %r -> 点 %d 张商品卡（固定坐标、详情页内零点击；"
                    "用户 %s 授权）" % (card["name"][:24], int(card.get("need") or 0),
                                        PRODUCT_CLICK_AUTH))
        r.rec.update(dict({
            # secs_detail：命中精确 key 还是族级 key（secs_cache_level/hit），见 resolve_dwell_plan
            "picked": dict({"key": card["key"], "name": card["name"], "button": card["button"],
                            "action": card["action"], "kind": card["kind"], "n": n0, "m": m0,
                            # 修 4：跳转得 类任务在证据里留一枚显式标记（dwell_ladder.cat 也会是 jump）
                            "jump_hint": bool(card.get("jump_hint")),
                            "secs": secs, "secs_source": secs_src, "dwell_plan": plan,
                            "dwell_ladder": {"cat": ladder_cat, "level": ladder_level,
                                             "top": DWELL_LADDER_TOP,
                                             "max_step": DWELL_LADDER_MAX_STEP}},
                           **secs_detail),
            "slot_anchor": anchor,
            "panel_before": info, "progress_before": prog0, "coin_before": coin0,
        }, **auth_ev))
        action_doc = {
            "intent": "tap %r of row %r n=%s/%s kind=%s (R1: x=%d in [%d,%d])"
                      % (card["button"], card["name"][:40], n0, m0, card["kind"],
                         tx, BTN_X_MIN, BTN_X_MAX),
            "tap": [tx, ty],
            "coord_source": "01_before.png OCR box centre (re-located this round, never cached)",
            "at": datetime.now().isoformat(timespec="seconds"),
            "device_time": dev.device_time(),
        }
        action_doc.update(auth_ev)
        jdump(r.p("03_action.json"), action_doc)
        # 2026-11-02 用户要求（第 6 条，最高优先）：点后只允许「一帧截图 + 像素指纹」，绝不 OCR。
        # 只有两种例外才走原来的完整流程：搜一搜（必须先提交关键词）；这一行连续 2 轮没进展。
        fast_path = bool(card["kind"] != "search" and ctx.give_up.get(card["key"], 0) < 2)
        sig_before = None
        if fast_path:
            try:
                # 点前面板的指纹在点击之前算（不占点后那 2 秒预算）
                sig_before = region_signature(r.p("01_before.png"))
            except Exception as e:
                # 算不出指纹就证明不了「已离开面板」→ 退回完整流程（在面板上绝不滑动）
                fast_path = False
                ctx.log("[round-%03d] 面板指纹算不出（%s: %s）→ 本轮走完整流程"
                        % (r.n, type(e).__name__, e))
        r.rec["fast_path"] = fast_path
        # 点前读一次焦点（dumpsys，零 OCR，约 0.2s）：这是「点之前我在哪一屏」的基线。
        # 点后焦点只要和它不一样，就说明页面真的切走了 —— 比像素更早、更准（landing_watch）。
        focus_before = focus_activity(dev) if fast_path else ""
        # 修 2（2026-11-04 真机事故）：这一行本轮**真正点下去**的次数，点击之前就 +1
        # （不是失败之后）—— 升档重试、jump 立刻重试、普通失败三条路径都要被同一本账记上，
        # 上限 MAX_ROW_TRIES 才拦得住「升档成功不动放弃额度」那种无限打转。
        ctx.try_count[card["key"]] = int(ctx.try_count.get(card["key"], 0) or 0) + 1
        r.rec["try_no"] = ctx.try_count[card["key"]]
        tap_ms = int(time.time() * 1000)
        dev.tap(tx, ty)
        # 2026-11-02 用户要求：点完立刻滑，不要等页面加载完——
        # 早滑几下不影响（页面没加载完时滑动是无害的空滑），等 1.5 秒纯属浪费。
        # 2026-11-03 修正：这 0.2 秒**不再用来判「没落地」**（20:53 真机就栽在这里：
        # 淘宝拉起支付宝要 1 秒以上，0.2 秒那一帧与面板逐像素相同 → 误判 skipped 丢一轮）。
        # 现在只作「落地轮询」的第一步间隔，判定交给 landing_watch。
        time.sleep(LANDING_FOCUS_POLL_S)

        # ---- 3) 点击后：判落地并分派（R3 R4）----
        landing, dwell = "other", {}
        # A（2026-11-03 用户真机观察）：停留计时的**锚点**是「落地确认」那一刻，不是 tap 时刻。
        # 两个变量在下面各分支里被赋成真实锚点与依据（landing_focus / landing_px / landing_ocr）。
        dwell_anchor_ts, dwell_basis = 0.0, ""
        tag = "r%03d" % r.n
        if fast_path:
            # ---- 3F) 快速路径（2026-11-02 用户要求 第 6/8 条；2026-11-03 判落地改口径）----
            # 判落地不再看「tap 后 0.2 秒那一帧」：改用 landing_watch 轮询焦点（零 OCR），
            # 焦点变了立刻算落地；焦点一直是同一屏才最多等 3 秒、再用像素指纹兜底。
            # 帧只做 region_signature 指纹比对（不 OCR、不要坐标），走最快的抓帧通道。
            watch = landing_watch(
                dev, ctx.log, r.p("01_before.png"), sig_before,
                focus_before=focus_before, tap_ms=tap_ms,
                grab=lambda p: dev_pixel_frame(dev, p),
                paths={"early": r.p("04_after.png"), "later": r.p("04c_after_wait.png"),
                       "changed": r.p("04d_after_focus.png")},
                tag=tag)
            # 快速路径不 OCR：五号证据写「空盒 + 原因」，不冒充 OCR 结果
            jdump(r.p("05_after_ocr.json"), {
                "image": os.path.basename(watch.get("frame") or "04_after.png"),
                "n": 0, "fast_path": True,
                "note": "2026-11-02 用户要求（第 6 条）：点后不做 OCR，只做像素指纹比对",
                "landing_watch": {k: watch[k] for k in
                                  ("landed", "why", "focus_before", "focus_after",
                                   "focus_changed", "focus_reads", "waited_ms",
                                   "judged_at_ms")},
                "boxes": []})
            # 第 8 条 + 第 4 条证据字段：像素闸里补 focus_before / focus_after /
            # focus_changed / judged_at_ms（相对 tap 的毫秒），只加不改。
            gate = dict(watch.get("gate") or {})
            gate.update({"focus_before": watch["focus_before"],
                         "focus_after": watch["focus_after"],
                         "focus_changed": watch["focus_changed"],
                         "focus_reads": watch["focus_reads"],
                         "judged_at_ms": watch["judged_at_ms"],
                         "waited_ms": watch["waited_ms"],
                         "shot_done_ms": watch["judged_at_ms"],
                         "shot_channel": (watch.get("frame_meta") or {}).get("channel"),
                         "shot_channel_ms": (watch.get("frame_meta") or {}).get("ms"),
                         "landed": watch["landed"]})
            r.rec["landing_pixel"] = gate
            r.rec["landing_watch"] = {k: watch[k] for k in
                                      ("landed", "why", "focus_before", "focus_after",
                                       "focus_changed", "focus_reads", "waited_ms",
                                       "judged_at_ms")}
            if watch.get("screen_off"):
                # 落地帧全黑（息屏）：tap 根本不会生效 —— 设备问题，不扣分、不滑、不进阶梯
                return screen_off_verdict(r, ctx, dev, what, "落地判定",
                                          extra=dict(gate, tap_ms=tap_ms, secs=secs,
                                                     secs_source=secs_src), log=ctx.log)
            if not watch["landed"]:
                # 3 秒焦点未变 + 像素未变：**不是**立刻 skipped —— 先 BACK 一次再看行是否消失
                # （用户 2026-11-02：「这个判定可以到返回到任务界面来判定」）。
                return finish_not_landed(r, ctx, dev, what, card, action_doc, gate, tap_ms,
                                         secs, secs_src, n0, m0, prog0, coin0, cards0, anchor)
            # 已落地 -> 落地确认那一帧当 before 证据（第 6 条：不再拍前置帧）
            # A（2026-11-03）：锚点 = 落地确认那一刻（watch 里记的 landed_at_ms，相对 tap 的毫秒）。
            # 依据：焦点变了 = landing_focus；像素变了（焦点没变）= landing_px。
            dwell_anchor_ts = (int(tap_ms) + int(watch.get("landed_at_ms")
                                                 or watch.get("judged_at_ms") or 0)) / 1000.0
            dwell_basis = "landing_focus" if watch.get("focus_changed") else "landing_px"
            r.rec["dwell_start"] = {"basis": dwell_basis,
                                    "tap_to_dwell_start_ms": int(round(dwell_anchor_ts * 1000))
                                                              - int(tap_ms),
                                    "landed_at_ms": int(watch.get("landed_at_ms") or 0),
                                    "focus": str(watch.get("focus_after") or "")}
            land_path = watch.get("frame") or r.p("04_after.png")
            # 2026-11-03 性能修复：这里原来又调了 focus_short() + focus_activity()，
            # 两次 dumpsys 约 2 秒（真机实测 tap→首次滑动 3.9~4.7s，超了第 6 条 2 秒线）。
            # 落地判定里**已经读到过焦点**（watch["focus_after"]），直接复用，不再重读。
            lf = str(watch.get("focus_after") or focus_before or "")
            landing = classify_landing(lf)
            r.rec["landing_focus"] = lf
            landing_act = lf
            r.rec["landing_activity"] = landing_act
            if card["kind"] == "product_click":
                # 2026-11-03 用户授权（第 3 条）：这条**不滑**，走商品卡点击流程。
                # 落地页是 BrowserActivity（任务页=原商品页），既不是首页也不是面板页，
                # 所以必须在这条分类分支**之前**拦下，别让它掉进「跳转类」或「滑满 N 秒」。
                landing = "taobao-product"
                r.rec["landing_kind"] = "taobao-product"
                dwell = product_click_flow(dev, ctx, r, card)
            elif landing_act and landing_act in (FOCUS_ACTS_HOME + FOCUS_ACTS_TMS):
                # 淘宝内浏览：计时靠滑动走，照旧滑满扫描阶段定死的 secs（第 2 条，不变）
                landing = "taobao"
                r.rec["landing_kind"] = "taobao-in"
                # 2026-11-02 修：static_dwell 以前**从来没被调用过** —— 「玩N秒 / 去逛」这类
                # 本该静止等计时的任务，一直被拿去滑动（滑走页面 = 计时反而不走）。
                if plan["mode"] == "static":
                    dwell = static_dwell(dev, secs, ctx.scans_dir, tag,
                                         start_ts=dwell_anchor_ts, start_basis=dwell_basis)
                elif bool(getattr(ctx.args, "adaptive_dwell", False)):
                    dwell = adaptive_dwell(dev, secs, ctx.scans_dir, tag, want=plan["mode"],
                                           start_ts=dwell_anchor_ts, start_basis=dwell_basis)
                else:
                    dwell = browse_dwell(dev, secs, ctx.scans_dir, tag, before=land_path,
                                         start_ts=dwell_anchor_ts, start_basis=dwell_basis)
            else:
                # R15（2026-11-02 用户真机口径）：跳转类任务一律「不滑，直接返回」。
                # 支付宝 / 天猫 / 任何别的 App 收敛成**同一条**分支（原来三份）：一次 dwell
                # 都不调，直接 BACK 回任务页（可能辗转多个窗口，最多 JUMP_BACK_MAX 次）。
                if landing == "other":
                    r.rec["landing_other_package"] = lf
                r.rec["landing_kind"] = "jump"
                # 停留几秒？B（2026-11-03 用户拍板）走阶梯表：
                #   跳转类（行上没写时长）= jump 档 0 -> 5 -> 10 秒（第 0 档落地确认后直接回）；
                #   行上明写「浏览N秒 / 玩N秒」= 按那一档 N+1 -> N+3 -> N+6（同 browse 阶梯口径）。
                # 不能用 plan["secs"]：读不到时长时它是 18（默认滑动档），拿它当停留会白等 13 秒。
                _txt = "%s %s" % (card.get("name") or "", card.get("sub") or "")
                _m = DWELL_BROWSE_RE.search(_txt) or DWELL_STATIC_RE.search(_txt)
                if _m:
                    jump_cat = "browse"
                    jump_stay = dwell_ladder_secs("browse", ladder_level, int(_m.group(1)))
                else:
                    jump_cat = "jump"
                    jump_stay = dwell_ladder_secs("jump", ladder_level)
                ladder_cat = jump_cat
                r.rec["jump_stay_s"] = jump_stay
                r.rec["dwell_ladder_cat"] = jump_cat
                r.rec["dwell_ladder_level"] = ladder_level
                # 证据一致性：picked 里的阶梯种类在「跳转类」这一支会被改成 jump/browse，
                # 这里同步一份，免得 picked.dwell_ladder.cat 与 dwell_escalation.cat 对不上。
                try:
                    r.rec["picked"]["dwell_ladder"]["cat"] = jump_cat
                except Exception:
                    pass
                # C（2026-11-03）：回淘宝改成「先 am start（= 点一下淘宝图标），落不到才退回 BACK」；
                # A：静止停留从落地确认锚点起算。
                dwell = jump_back_to_taobao(dev, ctx.log, tag, lf or landing_act,
                                            stay_s=jump_stay, start_ts=dwell_anchor_ts,
                                            start_basis=dwell_basis)
        else:
            # ---- 3A) 完整流程（只有上面那两种例外才走）：点后 OCR 落地判定 + 跳转框 + 落地复核 ----
            rows1, pkg1, act1 = r.grab(dev, "04_after.png", "05_after_ocr.json")
            # 用户显式授权：点完行之后弹的「打开其他应用」确认框，按 jump_policy 选
            # 「仅打开一次 / 始终打开」（选了始终打开，系统以后就不再弹）。
            jump1 = handle_jump_dialog(dev, rows1, log=ctx.log)
            if jump1.get("seen"):
                r.rec["jump_dialog"] = jump1
            if jump1.get("ok"):
                rows1, pkg1, act1 = r.grab(dev, "04_after.png", "05_after_ocr.json")
                r.rec["jump_dialog"]["recheck_focus"] = "%s/%s" % (pkg1, act1)
            # 2026-11-02 用户反馈：浏览类点进去之后"不知道滑动"，任务做不完。
            # 因此默认改为**一直滑**（滑到底为止），不再靠"计时在不在走"来判断要不要滑。
            # 想回到自适应可传 --adaptive-dwell。
            force_swipe = not bool(getattr(ctx.args, "adaptive_dwell", False))
            # 源 logs/r2_expand.py classify()：四分类（alipay / tmall / taobao / 其他 App）
            # 源 logs/r2_expand_coin.py：首次读到的前台可能是「还没切走」，settle 后复核一次
            land = landing_after(dev, "%s/%s" % (pkg1, act1))
            landing = land["mode"]
            r.rec["landing_recheck"] = land
            r.rec["landing_focus"] = "%s/%s" % (pkg1, act1)
            # A（2026-11-03）：完整流程的停留锚点 = 这一轮落地确认（OCR + settle 复核完成）那一刻；
            # 这样「点后 OCR / 复核」花掉的那几秒不会从停留时长里被扣掉。
            dwell_anchor_ts, dwell_basis = time.time(), "landing_ocr"
            if landing in ("alipay", "tmall"):
                # R3：支付宝/天猫是同一家的授权通道 —— 只连滑、零点击，抽检到弹窗立即退
                dwell = swipe_only_dwell(dev, secs, ctx.scans_dir, tag, mode=landing,
                                         baseline_words=base_popup_words)
            elif landing == "taobao":
                land_text = text_of(rows1)
                want_search = (card["kind"] == "search") or any(
                    k in land_text for k in SEARCH_BLOCK_LABELS)
                if want_search:
                    landing = "taobao-search"
                    dwell = search_submit(dev, ctx.scans_dir, tag)       # R4：热词 chip
                    # 第 8 条：闸门落在「进入滑动之前的那一帧」——search_submit 最后拍的那一帧
                    # （结果页 _search_<tag>_results.jpg；一个 chip 都选不出时退到它拍的落地帧）。
                    # 绝不能用 04_after.png 来判，那会把搜索框那页当成面板拦掉。
                    frame_name = dwell.get("results_png") or dwell.get("landing_png") or ""
                    gate = landing_pixel_gate(r.p("01_before.png"),
                                              os.path.join(ctx.scans_dir,
                                                           frame_name or "_none_"),
                                              sig_before=sig_before, source="full-search")
                    gate["frame_role"] = ("results_png" if dwell.get("results_png") else
                                          ("landing_png" if dwell.get("landing_png") else "none"))
                    r.rec["landing_pixel"] = gate
                    if gate["still_panel"]:
                        # 结果页/落地页与点前面板几乎一致 = 根本没离开面板 -> 不滑
                        return _skip_on_panel(r, ctx, card, action_doc, gate["why"], tap_ms,
                                              secs, secs_src, n0, m0, prog0, coin0,
                                              extra={"search_result": dwell})
                    # 2026-11-04 用户拍板：按结果页横幅「浏览N秒可领」校准停留秒数。
                    # 横幅来自这张已经 OCR 过的结果页，**零额外识别开销**。
                    # 读不到就用原来的 secs（同族缓存/副标题）；万一等少了，阶梯会自动往上加。
                    _bn = dwell.get("banner_secs")
                    if _bn:
                        _new = int(_bn) + DWELL_MARGIN_BROWSE_S
                        if _new != secs:
                            ctx.log("[search] %s 结果页横幅「浏览 %s 秒」-> 本行停留 %d 秒（原计划 %d 秒）"
                                    % (str(card.get("name"))[:18], _bn, _new, secs))
                            r.rec["banner_secs"] = {"banner": int(_bn), "used": _new, "was": secs}
                            secs = _new
                    # 提交成功与否都要在结果页/落地页上把时长待满（搜一搜计的是浏览时长）
                    # 2026-11-04 真机（手机2，用户口径）：这一条是「搜索 + 滑动浏览」的复合任务，
                    # **计时是从结果页出现那一刻开始**的，搜索前置那 5~8 秒不该算进停留预算。
                    # 旧代码从「点完落地」起算：按横幅校准到 11 秒后，搜索前置就把预算吃光了 ——
                    # round-224 实测 requested_s=11、actual_s=18.49、**swipes=0 / moved=false**，
                    # 一次都没滑 -> 计时不走 -> 计数不涨 -> 任务永远做不成。
                    # 所以这里把锚点重置到「结果页已就绪」的此刻，再按计划秒数真滑。
                    dwell_anchor_ts, dwell_basis = time.time(), "search_results"
                    dwell["browse"] = browse_dwell(
                        dev, secs, ctx.scans_dir, tag, start_ts=dwell_anchor_ts,
                        start_basis=dwell_basis)
                else:
                    landing = "taobao"
                    # 第 8 条（普遍保证）：完整流程滑之前也要有「已离开面板」的像素证据。
                    # 直接复用点后那一帧 04_after.png（它已经做过 OCR），不再额外拍帧。
                    gate = landing_pixel_gate(r.p("01_before.png"), r.p("04_after.png"),
                                              sig_before=sig_before, source="full")
                    r.rec["landing_pixel"] = gate
                    if gate["still_panel"]:
                        return _skip_on_panel(r, ctx, card, action_doc, gate["why"], tap_ms,
                                              secs, secs_src, n0, m0, prog0, coin0)
                    dwell = (
                        browse_dwell(dev, secs, ctx.scans_dir, tag, start_ts=dwell_anchor_ts,
                                     start_basis=dwell_basis) if force_swipe else
                        adaptive_dwell(dev, secs, ctx.scans_dir, tag, want=plan["mode"],
                                       start_ts=dwell_anchor_ts, start_basis=dwell_basis))
            else:
                # 源 logs/r2_expand.py：落到别的 App 就立刻退出来，绝不在别人的 App 里滑动
                r.rec["landing_other_package"] = pkg1
                dwell = leave_foreign_app(dev, ctx, "%s/%s" % (pkg1, act1))
                ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 2
        # A（2026-11-03）：把「停留计时锚点」统一写进 dwell 证据（只加字段，不改任何判定）：
        #   start_basis / start_ts / tap_to_dwell_start_ms —— 06_verdict.json 的 dwell 段直接可读。
        # 只给「真的按锚点起算」的 dwell 加这三个字段（swipe_only_dwell / leave_foreign_app
        # 这类没接锚点的路径一个字段都不加，免得证据里冒充「已锚定」）。
        if isinstance(dwell, dict) and dwell and dwell_anchor_ts and dwell.get("start_ts"):
            dwell = dwell_anchor_ev(dwell, dwell_anchor_ts, dwell_basis, tap_ms)
        r.rec["landing"] = landing
        r.rec["dwell_mode"] = (dwell or {}).get("mode")

        # ---- 3e) 第 6 条取证：tap -> 首次滑动（≤2000ms 验收的唯一取证途径）----
        first_swipe_ms = (dwell or {}).get("first_swipe_ms")
        if first_swipe_ms is None and isinstance((dwell or {}).get("browse"), dict):
            first_swipe_ms = dwell["browse"].get("first_swipe_ms")
        timing = {"tap_ms": tap_ms, "first_swipe_ms": first_swipe_ms,
                  "tap_to_swipe_ms": (int(first_swipe_ms - tap_ms) if first_swipe_ms else None)}
        r.rec.update(timing)
        action_doc.update(timing)
        jdump(r.p("03_action.json"), action_doc)
        # 2026-11-03：只有「快速路径」（普通浏览类）才归第 6 条的 ≤2000ms 验收管；
        # 搜一搜必须先提交关键词再浏览，那一轮 tap→首次滑动 20 秒以上是流程本身决定的，
        # 拿它去撞 2000ms 验收线只会每天刷出假告警（真机 2026-11-02 实测 24545/23412ms）。
        if (fast_path and timing["tap_to_swipe_ms"] is not None
                and timing["tap_to_swipe_ms"] > 2000):
            ctx.log("[round-%03d] 警告：tap→首次滑动 %dms，超了第 6 条的 2000ms 验收线"
                    % (r.n, timing["tap_to_swipe_ms"]))

        # ---- 4) 回面板（R6：不 force-stop）----
        # R15：跳转类（jump-no-slide）在 dwell 里已经一路 BACK 回到淘宝了，**不再多按一次 BACK**
        # （在面板上再按一次会把面板按掉）；直接进复核帧，那一步本来就要抓帧 + OCR 判面板与行。
        # 6 次 BACK 还没回淘宝（back_ok=False）-> 照旧走 back_to_panel（它内含 am start +
        # 写死序列重进 = 恢复阶梯），别自己再瞎点。
        _dwell_mode = (dwell or {}).get("mode")
        # 2026-11-03：点击商品流程（product-click）自己已经按过一次 BACK 且读 focus 确认回到面板，
        # 和「跳转类」一样不能再让 back_to_panel 多按一次 BACK（那会把面板按掉）。
        jump_no_slide = (_dwell_mode == "jump-no-slide"
                         or (_dwell_mode == "product-click" and (dwell or {}).get("back_home")))
        if jump_no_slide and ((dwell or {}).get("back_ok") or (dwell or {}).get("back_home")):
            ctx.log("[panel] %s 已回到淘宝（am start %s / BACK %d 次，焦点=%r）-> 不再按 BACK，"
                    "直接抓复核帧判行是否消失"
                    % (what, "ok" if (dwell or {}).get("am_start_ok") else "未用/失败",
                       int((dwell or {}).get("backs") or 0),
                       ((dwell or {}).get("focus_seq") or [""])[-1]))
            r.rec.update({"back_ok": True, "back_tries": int((dwell or {}).get("backs") or 0),
                          "back_seq": ["jump-back"], "dwell": dwell})
        elif jump_no_slide:
            ctx.log("[panel] %s 跳转类 %d 次 BACK 还没回淘宝 -> 交给恢复阶梯（不再自己瞎点）"
                    % (what, int((dwell or {}).get("backs") or 0)))
            back_ok, nback, seq, _ = back_to_panel(dev, what, ctx.log, ctx.scans_dir)
            r.rec.update({"back_ok": back_ok, "back_tries": nback, "back_seq": seq,
                          "dwell": dwell})
        else:
            back_ok, nback, seq, _ = back_to_panel(dev, what, ctx.log, ctx.scans_dir)
            r.rec.update({"back_ok": back_ok, "back_tries": nback, "back_seq": seq,
                          "dwell": dwell})

        # ---- 5) 复核：1 帧 + 槽位判据 + 像素双证 + 进度/金币差 ----
        # 2026-11-02 用户要求（第 6 条）：复核帧从 07/07b 两帧降为 1 帧；
        # 只有这一帧判不出结论（on_panel 为假 / 槽位说不清）才补第 2 帧。
        post, post_paths = [], []
        rows7 = r.grab(dev, "07_postcheck.jpg", "07_postcheck_ocr.json")[0]
        if frame_black(r.p("07_postcheck.jpg")):
            # 复核帧全黑（息屏）：这一轮的证据里没有可用结论 —— screen-off，不扣分、不进阶梯
            rewrap_screen(dev, ctx.log, "round-%03d 复核帧" % r.n)
            rows7 = r.grab(dev, "07_postcheck.jpg", "07_postcheck_ocr.json")[0]
            r.rec["frame_black"] = True
            r.rec["rewoke"] = True
            if frame_black(r.p("07_postcheck.jpg")):
                return screen_off_verdict(r, ctx, dev, what, "复核帧",
                                          extra={"tap_ms": tap_ms, "secs": secs,
                                                 "secs_source": secs_src, "dwell": dwell},
                                          log=ctx.log)
        post.append(rows7)
        post_paths.append(r.p("07_postcheck.jpg"))
        panel7 = panel_ok(rows7, what)[0]
        on_panel = panel7
        probe = slot_verdict(anchor, parse_rows(rows7), text_after=text_of(rows7),
                             panel_title_ok=panel7, expected_rows=len(cards0))
        if (not panel7) or probe["verdict"] == "skipped":
            rows7b = r.grab(dev, "07b_postcheck2.jpg", "07b_postcheck2_ocr.json")[0]
            post.append(rows7b)
            post_paths.append(r.p("07b_postcheck2.jpg"))
            on_panel = panel7 or panel_ok(rows7b, what)[0]
            r.rec["postcheck_second_frame"] = {
                "frame": "07b_postcheck2.jpg",
                "why": ("07 不是面板" if not panel7
                        else "07 这一帧槽位判不出结论：%s" % probe["reason"][:80])}
        # 源 logs/r2_search3.py：POSTCHECK 这一帧本身也得是面板；不是就再 BACK 一轮再取证
        if not on_panel:
            # 【完成识别】复核帧不是面板 = 同样被流程打乱 -> 同一条恢复阶梯（共享本轮重启额度）
            _, rows_r, how_r = recover_to_panel(dev, what, ctx.log, ctx.scans_dir,
                                                budget=ctx.nav_budget)
            p3 = r.p("07c_postcheck3.jpg")
            dev.shot(p3)
            post.append(dev.ocr(p3))
            post_paths.append(p3)
            on_panel = panel_ok(post[-1], what)[0]
            r.rec["post_back_retry"] = {"how": how_r, "on_panel": on_panel}
        texts = [text_of(p) for p in post]
        prog1 = next((v for v in (progress_in_text(t, what) for t in texts) if v is not None), None)
        coin1 = next((v for v in (coin_total(p) for p in post) if v is not None), None)
        prog_delta = (prog1 - prog0) if (prog0 is not None and prog1 is not None) else None
        coin_delta = (coin1 - coin0) if (coin0 is not None and coin1 is not None) else None
        # 槽位判据优先（源 _archive/v2/percept/racing_rows.py）：按位置跟踪同一行；
        # 名字法只作辅助证据（实测会串号：好物随心逛 与 搜一搜 的计数会互换）
        cards_post = [parse_rows(p) for p in post]
        frame_i = next((i for i, p in enumerate(post) if panel_ok(p, what)[0]), len(post) - 1)
        slot = slot_verdict(anchor, cards_post[frame_i], text_after=texts[frame_i],
                            panel_title_ok=on_panel, expected_rows=len(cards0),
                            progress_delta=prog_delta, coin_delta=coin_delta)
        r.rec["slot"] = dict(slot["evidence"], verdict=slot["verdict"], reason=slot["reason"])
        hits = [next((c for c in cs if c["key"] == card["key"]), None) for cs in cards_post]
        present = any(h is not None for h in hits)
        srow = slot.get("row")
        if srow is not None and srow.get("n") is not None:
            n1, m1 = srow["n"], srow["m"]
        else:
            hs = [h for h in hits if h is not None]
            best = next((h for h in hs if h["n"] is not None), (hs[0] if hs else None))
            n1, m1 = (best["n"], best["m"]) if best else (None, None)
        row_delta = (n1 - n0) if (n0 is not None and n1 is not None) else None
        # 2026-11-03 用户真机观察（值得留痕）：做完「好物沉浸看(0/5)」后，同一槽位换成了
        # 「搜一搜你心仪的宝贝(1/5)」——槽位判据把新任务的计数记在了旧任务头上。
        # 判定结果没错（旧任务确实消失=完成），但报告里「0/5→1/5」会误导。
        # 这里**只加证据**，不改判定：槽位上的名字变了就记下来。
        _slot_name = str((srow or {}).get("name") or "")
        if _slot_name and _slot_name != str(card.get("name") or ""):
            r.rec["slot_name_changed"] = {
                "picked": str(card.get("name") or ""), "slot_now": _slot_name,
                "note": "同一槽位换成了别的任务：新计数不算原来那条的，原来那条是「已消失=做完」"}
            ctx.log("[round-%03d] 槽位换人了：%r -> %r（旧任务已消失=做完，新计数不算它的）"
                    % (r.n, str(card.get("name"))[:18], _slot_name[:18]))
        # 视觉双证（源 _archive/v2/percept/vision.py）：OCR 说在面板，像素也要说同一页
        vis = page_similarity(r.p("01_before.png"), post_paths[frame_i])
        r.rec["panel_visual"] = vis
        if on_panel and page_lost(vis):
            r.rec["panel_visual"]["downgraded"] = (
                "像素与点前面板对不上（分 %.3f<%s 或 坏格子 %.2f≥%s）：这一帧不是同一页，"
                "不拿它当复核帧" % (vis.get("score") or 0, PAGE_LOST_SCORE,
                                   vis.get("bad_ratio") or 0, VISION_LOST_BAD))
            on_panel = False
        # 2026-11-02 用户要求（第 8 条）：返回后若面板帧判定「行还在、计数没动」，
        # 就算进度/金币在动也不许记 advanced —— 没落地的东西不能算业绩。
        row_still = bool(on_panel and present and slot["verdict"] in ("failed", "skipped")
                         and row_delta is not None and row_delta <= 0)
        advanced = bool(not row_still and (slot["verdict"] == "advanced"
                                           or (row_delta or 0) > 0
                                           or (prog_delta or 0) > 0 or (coin_delta or 0) > 0))
        # 「行消失」必须过槽位闸：白屏/整盘读漏与「做完被抽走」外观一样，
        # 只按「看不见了」判成功等于把读失败算成业绩（racing_rows 的核心结论）
        disappeared = bool(on_panel and slot["verdict"] == "disappeared")

        popup = (dwell or {}).get("popup") or []
        # 2026-11-07 真机（手机1）：搜一搜的计数已经 2/5 -> 3/5 推进了（row-delta=+1），却因为
        # 「结果页自证」没过被判 search-not-submitted；连着几轮这样，这一行攒够放弃次数被踢出可做列表，
        # 面板就报「当前视野没有可做行」，任务永远停在 4/5。用户口径：**计数推进是唯一硬证据**。
        # 所以只有当确实没有任何进度证据时，才判「搜索没提交」。
        _any_progress = bool((row_delta or 0) > 0 or (prog_delta or 0) > 0
                             or (coin_delta or 0) > 0 or advanced)
        not_submitted = bool(dwell.get("chip") and not dwell.get("submitted")
                             and not _any_progress)
        if landing == "other" and not jump_no_slide:
            # 快速路径的「别的 App」现在是 R15 的跳转类（不滑、BACK 回任务页），成败只看复核帧；
            # 这一条只留给完整流程里 leave_foreign_app 那种「真落到不相干的 App」。
            status, ok = "foreign-app", False
        elif popup:
            status, ok = "alipay-popup", False
        elif not_submitted:
            status, ok = "search-not-submitted", False
        elif not on_panel:
            status, ok = "panel-lost", False
        elif advanced:
            status, ok = "advanced", True
        elif disappeared:
            status, ok = "disappeared", True
        else:
            status, ok = "skipped", False
        reason = ("row %r %s/%s -> %s/%s (row-delta=%s); progress %s->%s (d=%s);"
                  " coin %s->%s (d=%s); landing=%s; row_still_there=%s; panel_foreground=%s;"
                  " dwell=%ss/%s swipes/%s; slot=%s(%s)"
                  % (card["name"][:26], n0, m0, n1, m1, row_delta, prog0, prog1, prog_delta,
                     coin0, coin1, coin_delta, landing, present, on_panel,
                     (dwell or {}).get("actual_s"), (dwell or {}).get("swipes"),
                     (dwell or {}).get("mode"), slot["verdict"], slot["reason"][:90]))
        if popup:
            reason = "alipay popup words %s -> BACK immediately, counted not-done; " % popup + reason
        # 通用要求：点击后期待的元素没出现 → 先诊断（复核帧已在手）再决定下一步，
        # 判断是弹窗 / 浮层 / 加载中；是弹窗就先关掉，绝不带着遮罩继续点同一坐标。
        if not ok:
            last = post_paths[-1]
            diag = {"frame": os.path.basename(last),
                    "focus": dev.focus_short(),
                    "popup_card": find_popup_card(last),
                    "loading": [w for w in LOADING_WORDS if w in (texts[-1] if texts else "")]}
            if diag["popup_card"]:
                pop = dismiss_popup_if_any(dev, log=ctx.log, outdir=ctx.outdir,
                                           tag="r%03d-post" % r.n, frame=last, rows=post[-1])
                diag["popup_action"] = {k: pop.get(k) for k in
                                        ("stage", "dismissed", "tap", "fallback")}
                ctx.log("[round-%03d] 点击后期望未出现 -> 诊断为弹窗 -> 策略=%s%s 结果=%s"
                        % (r.n, pop.get("stage"),
                           " fallback=back" if pop.get("fallback") else "",
                           "已关掉" if pop.get("dismissed") else "仍未关"))
            elif diag["loading"]:
                ctx.log("[round-%03d] 点击后期望未出现 -> 诊断为加载中（%s）-> 不重复点同坐标"
                        % (r.n, ",".join(diag["loading"])))
            else:
                ctx.log("[round-%03d] 点击后期望未出现 -> 诊断为普通页面（非弹窗/非加载中）"
                        % r.n)
            r.rec["post_diag"] = diag
        # ---- B（2026-11-03 用户拍板）：没做完 -> 停留升一档再来；行消失 -> 不升档、不重试 ----
        # 「没做完」沿用现有判据：**行还在（present）且计数没推进（row_delta 为空或 <=0）**。
        # 学到的高档位落进 state/secs_cache.json（key 用 kind|n/m|名称族），
        # 下一轮同族任务直接从该档起步；保险丝：同一行一轮内最多升 DWELL_LADDER_MAX_STEP 档。
        # 2026-11-04：决策本身抽成纯函数 dwell_escalation_decision（离线可断言），
        # 这里只做副作用 + 打日志。修 2 / 修 3 两条闸都在那个函数里：
        #   修 2：这一行本轮已被真正点下去几次（ctx.try_count，点击之前就 +1）。到 MAX_ROW_TRIES
        #         就**不再升档**、也不再设 retry_prefer —— 旧代码升档成功不动放弃额度，
        #         于是 0->1、1->2 两轮都不扣分，一行能被点 4 次（本次事故）。
        #   修 3：点名了外部 App 的行这一轮压根没跳出淘宝 -> 加时治不了原地没动，直接按失败计。
        _steps = int(getattr(ctx, "dwell_steps", {}).get(card["key"], 0) or 0)
        _tries = int(getattr(ctx, "try_count", {}).get(card["key"], 0) or 0)
        esc = dwell_escalation_decision(card, status, ok, landing, present, row_delta,
                                        ladder_cat, ladder_level, _steps, _tries)
        if esc["applied"]:
            ctx.dwell_steps[card["key"]] = esc["steps_used"]
            dwell_level_put(card, esc["to"], ladder_cat)
            ctx.retry_prefer = card["key"]          # 下一轮优先重做同一行（一次性）
            ctx.log("[dwell] %s 没做完 -> 停留升档 %d->%d（%s 档 %ss；本轮已升 %d/%d 档）"
                    % (str(card["name"])[:20], ladder_level, esc["to"], ladder_cat,
                       esc["next_secs"], esc["steps_used"], DWELL_LADDER_MAX_STEP))
        elif esc.get("no_leave"):
            # 修 3：落地还在淘宝里 = 这一点击原地没动，加时毫无意义（不升档、不 retry_prefer）
            ctx.log("[no-leave] %s 落地=%s 没跳出淘宝 + 行还在 + 计数没推进 -> 不升档"
                    "（加时治不了原地没动；同一行本轮已尝试 %d/%d 次）"
                    % (str(card["name"])[:20], landing, _tries, MAX_ROW_TRIES))
        elif esc.get("capped"):
            ctx.log("[cap] %s 本轮已尝试 %d 次（上限 %d）-> 不升档"
                    % (str(card["name"])[:20], _tries, MAX_ROW_TRIES))
        if esc["why"] != "done":
            r.rec["dwell_escalation"] = esc
        # ---- S1（2026-11-03 用户拍板）：**做成一次就降一档**（最低回到 0 档），失败再升回去 ----
        # 判据沿用现有「计数推进 / 行消失且 ok」：这里只认 ok=True（advanced / disappeared）。
        # 原来只升不降，真机 state/secs_cache.json 里已有 3 个任务族停在最高档（level=2）白停 21 秒。
        if ok:
            dec = dwell_level_decay(card, ladder_cat, ladder_level)
            if dec["dwell_ladder_decayed"]:
                ctx.log("[dwell] %s 做成了（%s）-> 停留降档 %d->%d（最低 0 档，自我纠偏）"
                        % (str(card["name"])[:20], status, dec["from"], dec["to"]))
        else:
            dec = {"dwell_ladder_decayed": False, "from": int(ladder_level),
                   "to": int(ladder_level),
                   "why": "没做成 -> 不降档（没做完会走上面的升档）"}
        r.rec["dwell_ladder_decayed"] = bool(dec["dwell_ladder_decayed"])
        r.rec["dwell_decay"] = dec
        if ok:
            # 修 2 配套（2026-11-04）：这一行本轮**有进展** -> 尝试计数清零。
            # 上限只治「原地打转」，不治正常的多轮累计：row_key 把数字全去掉（计数 n/m 会变），
            # 所以「好物随心逛 0/10」这种一行要做 10 次的正常任务，key 从头到尾都是同一个 ——
            # 做成一次就清零，绝不会被「同一行最多 3 次」砍在第 3 次。
            ctx.try_count.pop(card["key"], None)
        if not ok and status in ("skipped", "alipay-popup", "search-not-submitted"):
            # 2026-11-03 用户口径：「保持 5 秒，但没成功就立刻重试一次」。
            # 跳转类任务（jump-no-slide）没计数时**不扣放弃额度**，下一轮立刻重试同一行
            # （drive_panel 收到非 no-doable 就 continue，不滚屏、不再导航，所以就是"立刻"）。
            # 同一行连续两次都没计数才算放弃（give_up +2），避免在它身上无限打转。
            if _tries >= MAX_ROW_TRIES:
                # 修 2（2026-11-04 事故主修的最后一道闸）：这一行本轮已经点到上限 -> 不管失败
                # 原因是什么（升档重试 / jump 立刻重试 / 普通失败），一律记放弃并换下一行。
                # 上面那道「点击前」的闸拦的是第 4 次点击，这里拦的是「记完就再也不选它」。
                ctx.give_up[card["key"]] = max(int(ctx.give_up.get(card["key"], 0) or 0), 2)
                r.rec["try_cap"] = {"tries": _tries, "max": MAX_ROW_TRIES,
                                    "gave_up": int(ctx.give_up[card["key"]] or 0),
                                    "escalation_why": str(esc["why"])[:120]}
                ctx.log("[cap] %s 本轮已尝试 %d 次（上限 %d）-> 记放弃，换下一行"
                        % (str(card["name"])[:20], _tries, MAX_ROW_TRIES))
            elif esc["applied"]:
                # B：已经升档 -> 下一轮优先重做同一行、按更高一档再停；不动放弃额度
                if (dwell or {}).get("mode") == "jump-no-slide":
                    r.rec["jump_retry"] = True
            elif (dwell or {}).get("mode") == "jump-no-slide" and status == "skipped":
                _try = ctx.jump_retry.get(card["key"], 0) + 1
                ctx.jump_retry[card["key"]] = _try
                if _try <= 1:
                    ctx.log("[jump] %s 这一次没计数 -> 立刻重试一次（不扣放弃额度）"
                            % str(card["name"])[:20])
                    r.rec["jump_retry"] = True
                else:
                    ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 2
                    ctx.log("[jump] %s 第 %d 次仍没计数 -> 记放弃，换下一行"
                            % (str(card["name"])[:20], _try))
            else:
                ctx.give_up[card["key"]] = ctx.give_up.get(card["key"], 0) + 1
        # 2026-11-03 用户要求（修 2）：实际用的秒数必须和 secs_source 成对写进 06_verdict.json。
        # dash 读的是 rec["secs"]，原来只落在 dwell.requested_s 里，于是 11 轮全是 null。
        dwell_browse = (dwell or {}).get("browse")
        secs_used = (dwell or {}).get("requested_s")
        if secs_used is None:
            secs_used = (dwell_browse or {}).get("requested_s")
        if secs_used is None:
            secs_used = secs
        secs_actual = (dwell or {}).get("actual_s")
        if secs_actual is None:
            secs_actual = (dwell_browse or {}).get("actual_s")
        r.verdict(ok, reason, status, row_after=[n1, m1], row_present_after=present,
                  progress_after=prog1, coin_after=coin1, row_delta=row_delta,
                  progress_delta=prog_delta, coin_delta=coin_delta, on_panel=on_panel,
                  secs=secs_used, secs_source=secs_src, secs_actual_s=secs_actual)
        return {"status": status, "ok": ok, "key": card["key"]}

    except GuardError as e:
        # R1：本轮证据（01/02 已落盘）保留，但绝不点击
        try:
            r.verdict(False, str(e), "guard-violation")
        except Exception:
            pass
        ctx.safety_stop = True
        raise
    except StopRun as e:
        # 停手也要当场留判定：否则报告里就出现「证据齐、没结论」的空轮，证据链断掉。
        # 源 logs/r2_repair.py 的同一教训（它事后补；这里当场补，下次启动的 repair 再兜底）。
        try:
            r.verdict(False, "本轮被停手中断：%s" % e, "stoprun")
        except Exception:
            pass
        raise
    except Exception as e:
        # R8：异常也要落盘，证据链不能断
        tb = traceback.format_exc()
        try:
            r.verdict(False, "exception %s: %s" % (type(e).__name__, e), "exception",
                      traceback=tb[-2000:])
        except Exception:
            pass
        return {"status": "exception", "ok": False}


# --------------------------------------------------------------------------- #
# 面板驱动
# --------------------------------------------------------------------------- #
def _page_snapshot(ctx, what):
    """这一屏的内容（原始 OCR 盒 + 文本签名）：直接复用刚结束那一轮的证据，不额外截图/OCR。

    2026-11-02 用户要求（第 4/5 条）：判断只在真正需要时做一次 ——「这一页还有没有可做行」
    由 one_round 自己抓帧判（它每轮都重读行），这里只在「这页做空了」那一刻取一份屏内容，
    成本就是读一次刚写下的 02_before_ocr.json。
    """
    n = ctx.n_round
    path = os.path.join(ctx.rounds_dir, "round-%03d" % n, "02_before_ocr.json")
    boxes, source = [], os.path.basename(path)
    try:
        boxes = (jload(path) or {}).get("boxes") or []
    except Exception:
        boxes = []
    if len(text_of(boxes)) < 8:            # 证据读不到 / 空帧（少见）：现场补一帧
        p = os.path.join(ctx.scans_dir, "_page_%s_%03d.jpg" % (what, n))
        ctx.dev.shot(p)
        boxes = ctx.dev.ocr(p)
        source = os.path.basename(p)
    return {"round": n, "source": source, "boxes": boxes, "text": text_of(boxes)}


def _paged_scan_doc(ctx, dev, what, pages, t0):
    """把「一页一页做」过程里采集到的逐屏数据拼成 full_scan() 兼容的收尾表。

    兼容口径 = write_report() 实际读的字段（REPORT.md 第 2 节「入场 vs 收尾」、第 3 节
    「仍然不可做的行」都靠它们）：progress / coin_total / screens /
    rows[].{key,name,button,n,m,doable,why}。
    """
    per = [parse_rows(p["boxes"]) for p in pages]
    table = merge_scan_rows(per)
    last_boxes = pages[-1]["boxes"] if pages else []
    doc = {
        "tag": "%s_paged" % what, "device": dev.serial, "what": what, "mode": "paged",
        "started": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(time.time() - t0, 1), "screens": len(pages),
        "progress": progress_in_text("".join(p["text"] for p in pages), what),
        "coin_total": coin_total(last_boxes) if what == "coin" else None,
        "rows": table, "doable": [t for t in table if t["doable"]],
        "screen_index": [{"screen": i, "round": p["round"], "source": p["source"],
                          "text": p["text"][:2000],
                          "cards": [{kk: c[kk] for kk in ("y", "btn_x", "button", "action",
                                                          "name", "sub", "n", "m", "doable",
                                                          "kind", "why", "key")}
                                    for c in per[i]]}
                         for i, p in enumerate(pages)],
    }
    path = os.path.join(ctx.scans_dir, "%s_%s.json"
                        % (doc["tag"], datetime.now().strftime("%H%M%S")))
    jdump(path, doc)
    ctx.log("[panel] %s 收尾表由「一页一页做」的 %d 屏过程数据生成 -> %d 行（可做 %d）：%s"
            % (what, len(pages), len(table), len(doc["doable"]), os.path.basename(path)))
    return doc


def drive_panel(dev, ctx, what, cold=True, leave_prev=False):
    """驱动一个面板，返回「这个面板到底有没有开做过」（True/False，main 用它决定下一个面板要不要先 BACK）。

    cold=True 时进面板第一步是 force-stop + am start（多面板只在第一个传 True）。
    leave_prev=True（R16，多面板连跑的第二个起）：上一个面板收工后停在**任务列表**里，
    activity 还是 TMSActivity —— 这里先 BACK 一次退回「双11 淘金币/赛车主页面」，
    否则写死序列第二拍会落在任务列表的某一行上（2026-11-03 22:09 手机1 真机）。
    BACK 之后不管焦点对不对都继续按写死序列走：不对的话第一轮会 no-panel / 写死没走通，
    照旧交给恢复阶梯（不在这里自己瞎点）。
    """
    if leave_prev:
        act0 = focus_activity(dev)
        dev.back()
        time.sleep(PANEL_SWITCH_BACK_S)
        act1 = focus_activity(dev)
        ctx.log("[nav] %s 换面板：上一个面板停在 %r -> 先 BACK 一次退回主页面"
                "（现在 focus=%r），再按写死序列点下一枚胶囊" % (what, act0, act1))
        if act1 and act1 not in (FOCUS_ACTS_HOME + FOCUS_ACTS_TMS):
            ctx.log("[nav] %s BACK 之后焦点=%r 不在首页/淘金币页 -> 照旧交给恢复阶梯"
                    % (what, act1))
    # 2026-11-02 用户要求：「坐标都有了，还用 OCR 干嘛」。
    # 写死点击做完**不再单独判一次面板**（那是 1 次约 3~10 秒的 OCR，和紧接着第一轮的
    # 抓帧 OCR 完全重复）。没进去的话第一轮会返回 no-panel，照旧进恢复阶梯兜住。
    rows, how, info = nav_until_panel(dev, what, ctx.log, ctx.scans_dir, cold=cold, judge=False)
    assumed = bool((info or {}).get("assumed"))
    if rows is None and not assumed:
        if how == "screen-off":
            # 息屏：跳过这个面板，但不算失败（不写 panel_done、不 panic）
            ctx.log("[panel] %s 判面板帧全黑（息屏）-> 这个面板这轮不做了，跳过（不是失败）"
                    % what)
            return False
        ctx.log("[panel] %s 导航失败 -> 跳过该面板" % what)
        return False
    if assumed:
        rows = []
        ctx.log("[panel] %s 写死点击已完成（%s，导航 OCR %s 次）-> 不做判面板 OCR，"
                "直接开做，由第一轮抓帧判定"
                % (what, how, (info or {}).get("nav_ocr", 0)))
        # 2026-11-03 用户口径：折叠**不在这里**展开（速赚在前、展开在后，任务多了「展开」
        # 还会被挤到屏幕下面）。折叠由下面「一页一页做」的循环遇到才点（见 fold_button）。
    else:
        rows, exp = expand_panel(dev, rows, ctx.log, ctx.scans_dir)
        ctx.log("[panel] %s ready: %s / %s / %s" % (what, how, exp, info))

    # 2026-11-02 用户要求（第 4 条）：一页一页做 —— 直接在这一屏开始做，不做全表扫描、不回顶部、不重进面板。
    #   「这一页还有没有可做行」= one_round 的返回（它每轮自己抓帧重读行，返回 no-doable 就是这一页空了）；
    #   「到底」= 连续两屏 OCR 文本签名完全一致（与 full_scan 的到底判据同一条），另加滚动次数保险丝。
    # 用户口径 3：收尾不再 reopen_panel + full_scan，after 表用下面过程里采集到的逐屏数据生成。
    ctx.scans["before"][what] = {"rows": [], "screens": 0, "lazy": True}
    t_panel0, start_n = time.time(), ctx.n_round
    scrolls, pages, prev_sig = 0, [], None
    soft_backs = 0        # 「判成不在面板」时的温和恢复次数（BACK 一次再看），见 no-panel 分支
    expands = 0          # 「展开」点了几次（MAX_EXPAND_TRIES 保险丝）
    while True:
        pause_wait(ctx.log, getattr(dev, "serial", ""))
        if ctx.left() < MIN_ROUND_LEFT_S:
            ctx.stop_reason = ctx.stop_reason or "预算用完（剩 %ds）" % int(ctx.left())
            break
        if ctx.n_round - start_n >= int(ctx.args.max_rounds):   # 保险丝按面板计
            ctx.stop_reason = ctx.stop_reason or "达到 --max-rounds=%d" % ctx.args.max_rounds
            break
        try:
            res = one_round(dev, ctx, what)
        except GuardError as e:
            ctx.stop_reason = "R1 按钮 x 越界，已停止：%s" % e
            ctx.log("[STOP] %s" % e)
            return
        except NeedTeaching:
            # 待教学已经落盘（_nav_teach 做的），这里是「停手」信号：别吞掉，
            # 交给 main 处理成退出码 3（保留现有 NeedTeaching 语义）
            raise
        except StopRun as e:
            ctx.stop_reason = ctx.stop_reason or str(e)
            ctx.log("[STOP] %s" % e)
            return

        st = res.get("status")
        ctx.just_scrolled = False     # 这一轮已经用掉「刚滚过屏」这个标记
        if st == "screen-off":
            # 全黑帧（息屏）：设备问题，不跳过、不扣分、不进恢复阶梯；连续几轮全黑才停下
            if ctx.safety_stop:
                ctx.stop_reason = ctx.stop_reason or ("连续 %d 轮全黑帧（息屏）-> 停下等人"
                                                      % int(getattr(ctx, "screen_off", 0) or 0))
                ctx.log("[STOP] %s" % ctx.stop_reason)
                break
            continue
        if st == "no-panel":
            # 2026-11-03 真机教训：手机2 滚屏后标题滚出屏幕会被判「面板丢了」，一进恢复阶梯就是
            # BACK×3 + force-stop（每次 2 分钟），而重进后还是同一屏 -> 死循环（01:02~01:11）。
            # 所以先做**温和恢复**：BACK 一次，再看下一帧；两次都不行才交给恢复阶梯。
            if soft_backs < 2:
                soft_backs += 1
                # 用**往回滚一屏**代替 BACK：BACK 会真的把面板退出去（退到主页面），
                # 往回滚只是把被滚出屏幕的标题找回来，风险低得多。
                x1, y1, x2, y2, dur = SWIPE_DOWN
                ctx.log("[panel] %s 判成不在面板 -> 温和恢复：往回滚一屏把标题找回来"
                        "（%d/2），不进恢复阶梯" % (what, soft_backs))
                dev.swipe(x2, y2, x1, y1, dur)
                time.sleep(0.9)
                continue
            # 面板丢了：同一条恢复阶梯（BACK×3 -> force-stop + 写死序列 -> 或待教学停手）
            _, rows, how3 = recover_to_panel(dev, what, ctx.log, ctx.scans_dir)
            ctx.log("[panel] %s 恢复阶梯救回面板（%s）-> 继续在这一页做" % (what, how3))
            soft_backs = 0
            continue
        if st != "no-doable":
            # 这一页还有可做行（做完的行会被新任务顶上来，刷新的那些照样做）-> 继续在同一屏做
            soft_backs = 0        # 这一轮正常，温和恢复的计数清零
            continue
        snap = _page_snapshot(ctx, what)
        # 2026-11-03 用户口径：这一屏做空了，但这一屏里读到「展开」-> 折叠着，先点开再看。
        # 坐标来自**这一帧的 OCR 盒**（不写死、不盲点），复用帧 = 零新增 OCR；
        # 点完 continue：不判到底、不滚屏，让下一轮 one_round 重读这一屏（速赚自然先做）。
        fold = fold_button(snap.get("boxes") or [])
        if fold is not None and expands < MAX_EXPAND_TRIES:
            expands += 1
            ctx.log("[panel] %s 这一屏有「%s」-> 点它的 OCR 盒中心 (%d,%d) 展开再看"
                    "（第 %d/%d 次；坐标来自本帧，不写死）"
                    % (what, fold["t"], fold["x"], fold["y"], expands, MAX_EXPAND_TRIES))
            jdump(os.path.join(ctx.scans_dir, "_expand_%s_%d.json" % (what, expands)),
                  {"what": what, "text": fold["t"], "tap": [fold["x"], fold["y"]],
                   "box": fold["box"], "source_frame": snap.get("source"),
                   "source_round": snap.get("round"),
                   "at": datetime.now().isoformat(timespec="seconds")})
            dev.tap(int(fold["x"]), int(fold["y"]))
            time.sleep(FOLD_SETTLE_S)
            continue
        sig = snap["text"].strip()
        pages.append(snap)
        if prev_sig is not None and sig == prev_sig:
            ctx.log("[panel] %s 第 %d 屏与上一屏内容一致 -> 到底，该面板收工（%d 轮 / 滚 %d 屏）"
                    % (what, len(pages), ctx.n_round - start_n, scrolls))
            # 契约 B：**只在这里**记「今天已完成」——真的走到「到底」收工才算。
            # 因预算（ctx.left()）/ --max-rounds / 滚动保险丝而 break 的都不算完成，绝不写，
            # 否则明天启动会把没做完的面板错误跳过。
            panel_done_mark(dev, what, ctx.log)
            break
        prev_sig = sig
        if scrolls >= MAX_SCROLL_STEPS:
            ctx.stop_reason = ctx.stop_reason or ("%s 向下滚了 %d 屏仍未到底（保险丝）"
                                                  % (what, scrolls))
            break
        ctx.log("[panel] %s 这一页做空了 -> 向下滚一屏找下面的任务（第 %d 屏）"
                % (what, scrolls + 1))
        ctx.just_scrolled = True      # 下一轮若读到空白帧，别判"面板丢了"（见 one_round 里那条）
        dev.swipe(*SWIPE_DOWN)
        time.sleep(0.9)
        scrolls += 1

    # 收尾（2026-11-02 用户口径 3）：不再 reopen_panel + full_scan，after 表用过程数据生成
    if pages:
        ctx.scans["after"][what] = _paged_scan_doc(ctx, dev, what, pages, t_panel0)
    return True


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _fmt_ts(sec):
    m, s = divmod(int(sec), 60)
    return "%d分%02d秒" % (m, s)


def write_report(ctx, dev, what_list, args):
    """REPORT.md：做了多少轮 / 每行 before→after / 剩余不可做行及原因 / 硬规则 / 何时停下等人。"""
    out = os.path.join(ctx.outdir, "REPORT.md")
    L = []
    ok_n = sum(1 for x in ctx.rounds if x.get("ok"))
    adv = sum(1 for x in ctx.rounds if x.get("status") == "advanced")
    dis = sum(1 for x in ctx.rounds if x.get("status") == "disappeared")
    L.append("# 淘金币 / 赛车能量 自动跑 — 报告")
    L.append("")
    L.append(("> 由 `coin11/auto.py` 自动生成：一条命令、无人值守，运行期无任何模型参与判断。"))
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(("| 设备 serial | `%s` |") % dev.serial)
    L.append("| 面板 | %s |" % " / ".join(what_list))
    L.append("| 预算 | %s |" % ("不设时间上限（--minutes %s：只在面板到底 / --max-rounds / "
                                "脚本队列跑完时停）" % args.minutes
                                if getattr(ctx, "unlimited", False)
                                else "%s 分钟" % args.minutes))
    L.append("| 开始 / 结束 | %s / %s |"
             % (datetime.fromtimestamp(ctx.t0).strftime("%Y-%m-%d %H:%M:%S"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    L.append("| 实际耗时 | %s |" % _fmt_ts(ctx.elapsed()))
    L.append("| 轮数 | %d（成功 %d = 有增量 %d + 行消失 %d） |"
             % (len(ctx.rounds), ok_n, adv, dis))
    L.append("| 停止原因 | %s |" % (ctx.stop_reason or "全部可做行已做完"))
    L.append(("| 证据目录 | `%s` |") % os.path.relpath(ctx.outdir).replace(chr(92), "/"))
    L.append("")

    L.append("## 1. 每轮 before → after")
    L.append("")
    L.append("| round | 面板 | 行 | 计数 before→after | 进度 | 落地 | 判定 |")
    L.append("|---|---|---|---|---|---|---|")
    for x in ctx.rounds:
        pk = x.get("picked") or {}
        ra = x.get("row_after") or [None, None]
        L.append("| %03d | %s | %s | %s/%s→%s/%s | %s→%s | %s | **%s** %s |"
                 % (x.get("round", 0), x.get("what", ""), str(pk.get("name", "-"))[:24],
                    pk.get("n"), pk.get("m"), ra[0], ra[1], x.get("progress_before"),
                    x.get("progress_after"), x.get("landing", "-"), x.get("status"),
                    "OK" if x.get("ok") else "--"))
    L.append("")

    L.append("## 2. 面板全表（入场扫描 vs 收尾扫描）")
    L.append("")
    for what in what_list:
        b = (ctx.scans["before"] or {}).get(what)
        a = (ctx.scans["after"] or {}).get(what)
        if not b:
            continue
        L.append("### %s" % what)
        L.append("")
        if what == "race":
            L.append("- 面板进度：%s → %s" % (b.get("progress"), (a or {}).get("progress")))
        else:
            L.append("- 金币总数：%s → %s" % (b.get("coin_total"), (a or {}).get("coin_total")))
        L.append("- 扫描屏数：%s / %s" % (b.get("screens"), (a or {}).get("screens")))
        if (a or {}).get("mode") == "paged":
            L.append("- 采集方式：一页一页做（%d 屏，边做边向下滚；2026-11-02 用户要求 第 4 条）"
                     % ((a or {}).get("screens") or 0))
        L.append("")
        # 2026-11-02 用户要求（第 4 条补）：入场是懒扫描（before 只有元信息、rows=[]），
        # 逐行数据全在收尾表里（「一页一页做」边做边采，见 _paged_scan_doc）——
        # 这时第 2 节就直接渲染收尾全表，并把列名换成只有「收尾」的一套，别让用户对着空表猜。
        arows = {r["key"]: r for r in ((a or {}).get("rows") or [])}
        brows = b.get("rows") or []
        if brows:
            L.append("| 行 | 按钮 | 入场 n/m | 收尾 n/m | 入场判定 | 收尾判定 |")
            L.append("|---|---|---|---|---|---|")
            for rw in brows:
                ra = arows.get(rw["key"])
                L.append("| %s | %s | %s/%s | %s | %s | %s |"
                         % (str(rw["name"])[:30], rw["button"], rw["n"], rw["m"],
                            ("%s/%s" % (ra["n"], ra["m"])) if ra else "（已不在表内）",
                            "可做" if rw["doable"] else "跳过：" + str(rw["why"])[:34],
                            ("可做" if ra["doable"] else "跳过：" + str(ra["why"])[:34])
                            if ra else "-"))
        elif (a or {}).get("rows"):
            L.append("> 入场未逐行扫描（懒扫描）：下表是**收尾全表** —— 「一页一页做」过程中"
                     "采集到的 %d 屏合并结果，共 %d 行。"
                     % ((a or {}).get("screens") or 0, len(a["rows"])))
            L.append("")
            L.append("| 行 | 按钮 | 收尾 n/m | 收尾判定 |")
            L.append("|---|---|---|---|")
            for rw in a["rows"]:
                L.append("| %s | %s | %s/%s | %s |"
                         % (str(rw["name"])[:30], rw["button"], rw["n"], rw["m"],
                            "可做" if rw.get("doable") else "跳过：" + str(rw["why"])[:34]))
        else:
            L.append("（没有行表：入场未逐行扫描，收尾也没采到屏。）")
        L.append("")

    L.append("## 3. 仍然不可做的行（以收尾扫描为准，逐行给原因）")
    L.append("")
    any_doc = False
    for what in what_list:
        a = (ctx.scans["after"] or {}).get(what)
        if not a:
            continue
        any_doc = True
        # 标题里的「N 行」必须是**仍然不可做**的行数（rest），不是收尾表总行数
        rest = [r for r in (a.get("rows") or []) if not r.get("doable")]
        L.append("### %s（%d 行）" % (what, len(rest)))
        L.append("")
        if rest:
            L.append("| 行 | 按钮 | n/m | 原因 |")
            L.append("|---|---|---|---|")
            for rw in rest:
                L.append("| %s | %s | %s/%s | %s |"
                         % (str(rw["name"])[:34], rw["button"], rw["n"], rw["m"],
                            str(rw["why"]) or "-"))
        else:
            L.append("（无）收尾表里没有「仍然不可做」的行。")
        L.append("")
    if not any_doc:
        L.append("（无：这一轮没有收尾表 —— 面板没走到「这一页做空」就结束了。）")
        L.append("")

    L.append("## 4. 硬规则清单（代码里逐条常量 + 注释，参数关不掉）")
    L.append("")
    L.append("| # | 规则 | 落地位置 |")
    L.append("|---|---|---|")
    L.append(("| R1 | 按钮 x 必须 ∈ [%d,%d]，越界不点并整轮停机 | `BTN_X_MIN/BTN_X_MAX` + `guard_button_x()` |")
             % (BTN_X_MIN, BTN_X_MAX))
    L.append(("| R2 | 点击前面板必须在前台（面板标题 + ≥3 行） | `panel_ok()`：不满足先重进面板，仍不满足则落盘不点 |"))
    L.append(("| R3 | 支付宝类点「去完成/奖励芯片」直跳；进去只连滑、任何按钮不点，弹窗立即 BACK | `alipay_dwell()` + `ALIPAY_OK` / `ALIPAY_POPUP_WORDS` |"))
    L.append(("| R4 | 搜一搜只用「搜索发现/历史搜索」热词 chip，并自证进结果页 | `chip_candidates()` + `search_submit()` + `BAD_CHIP` |"))
    L.append(("| R5 | 不点 领取/立即领取/授权/开通/同意/支付/转账/去玩/邀请/助力/广告/下单/买/折扣/签到/免费拿/答题；永不点「始终打开」 | `BLACK_WORDS` + `TAP_LABELS` + `DIALOG_TEXT` |"))
    L.append(("| R6 | 不 force-stop、不安装、不改系统设置；拉不回用 am start | `Device.am_start_taobao()` |"))
    L.append(("| R7 | 长列表展开后只能向下扫（零上滚）；面板驱动按「一页一页做」：做完这一页才滚一屏，"
              "连续两屏内容一致=到底 | `drive_panel()` / `full_scan()` |"))
    L.append(("| R8 | 循环体 try/except，异常也落 verdict | `one_round()` 的 `except Exception` |"))
    L.append(("| R9 | 只认 keyevent 26 唤醒（224/82 只改状态不亮屏） | `Device.wake()` |"))
    L.append("")

    L.append("## 5. 什么情况下它会主动停下等人（而不是硬着头皮点）")
    L.append("")
    L.append(("1. **R1 按钮 x 越界**（算出 1078 这类值）：本轮落 `06_verdict.json` = `guard-violation`，整个 run 立刻结束——定位链已不可信。"))
    L.append("2. **系统安装 / 权限 / 跳转确认框**：包名、Activity 或文案命中即停；不点、不按 back、不尝试关闭，只留证。")
    L.append(("3. **屏幕唤不醒**：`input keyevent 26` 三次后仍非 Awake（R9）。"))
    L.append(("4. **面板找不回**：BACK 4 次 + `am start` 拉回 + 重新导航全部失败。"))
    L.append("5. **支付宝内出现弹窗**：立即 BACK 退出，该行记未完成；同一行连续两轮无进展则放弃该行。")
    L.append("6. **遇到未安装 App 的行**：只记「外部 App / 未装 App」，绝不安装（R6）。")
    L.append(("7. 其余都是自己收尾：可做行做完、连续两轮全表扫描都没有可做行、或 `--minutes` 预算到点。"))
    L.append("")

    L.append("## 6. 证据清单")
    L.append("")
    L.append(chr(96) * 3)
    L.append("%s/" % os.path.basename(ctx.outdir))
    L.append("├── REPORT.md")
    L.append("├── run.log")
    L.append("├── ledger.jsonl            %d 行（一轮一行）" % len(ctx.rounds))
    rd = sorted(os.listdir(ctx.rounds_dir)) if os.path.isdir(ctx.rounds_dir) else []
    L.append("├── rounds/                 %d 个轮次目录" % len(rd))
    if rd:
        sample = os.path.join(ctx.rounds_dir, rd[0])
        L.append("│   └── %s/  %s" % (rd[0], " ".join(sorted(os.listdir(sample)))))
    sc = sorted(os.listdir(ctx.scans_dir)) if os.path.isdir(ctx.scans_dir) else []
    L.append("└── scans/                  %d 个文件" % len(sc))
    L.append(chr(96) * 3)
    L.append("")
    L.append(("> 六件套固定为 `01_before.png` `02_before_ocr.json` `03_action.json` `04_after.png` `05_after_ocr.json` `06_verdict.json`；`07*_postcheck*` 是回到面板后的复核帧（默认 1 帧，判不出结论才补第 2 帧；2026-11-02 用户要求）。"))
    L.append("")
    with open(out, "w", encoding="utf8") as f:
        f.write(chr(10).join(L))
    ctx.log("[report] %s" % out)
    return out


# --------------------------------------------------------------------------- #
# 离线自测：不连手机，用已落盘的 evidence 复算「解析 / 分类 / 守卫 / 面板判定」
# --------------------------------------------------------------------------- #
def _root():
    return _home()


def run_selftest(root=None):
    global _SECS_CACHE_PERSIST, STATE_DIR, PENDING_DIR, LIVE_JSON, PAUSE_FLAG, HINT_JSON
    _SECS_CACHE_PERSIST = False        # 离线自测不写 state/secs_cache.json
    # 2026-11-03 事故：离线自测**碰了真实状态文件** —— T12 的假设备走 live_write() 把
    # {"serial":"offline-fake","phase":"round"} 写进真实 state/live.json，dash 于是显示
    # 「正在执行第 1 轮」、保活读 _run_active() 以为脚本在跑一直让路。
    # 现在所有可写路径（live/pause/hint/panel_done/scripts 全在 STATE_DIR 下，待教学在
    # PENDING_DIR 下）一律先指到临时目录，跑完再换回来 + 删掉。
    import tempfile as _tf
    import shutil as _sh
    _real_paths = (STATE_DIR, PENDING_DIR, LIVE_JSON, PAUSE_FLAG, HINT_JSON)
    _owner = _tf.mkdtemp(prefix="autotest-paths-")
    STATE_DIR = os.path.join(_owner, "state")
    LIVE_JSON = os.path.join(STATE_DIR, "live.json")
    PAUSE_FLAG = os.path.join(STATE_DIR, "pause.flag")
    HINT_JSON = os.path.join(STATE_DIR, "task_hint.json")
    PENDING_DIR = os.path.join(_owner, "pending")
    # 打包版没有开发机的 evidence/：夹具随包带一份，路径由 app_entry.py 通过这个变量指过去
    root = root or os.environ.get("COIN11_SELFTEST_ROOT") or _root()
    ev = os.path.join(root, "evidence", "2026-11-02", "scans")
    checks = []

    def detail(name):
        return jload(os.path.join(ev, name))["detail"]

    def round_boxes(rel):
        return jload(os.path.join(root, rel))["boxes"]

    def by_name(cards, key):
        hit = [c for c in cards if key in c["name"]]
        assert hit, "找不到行：%s（有：%s）" % (key, [c["name"][:14] for c in cards][:12])
        return hit[0]

    # T1 真实赛车面板 OCR -> 行解析 + R1 守卫
    def t1():
        screens = detail("race_full_004436.json")
        assert len(screens) >= 3, "赛车面板应有 ≥3 屏"
        cards = parse_rows(screens[0]["boxes"])
        assert len(cards) >= 5, "首屏行数过少：%d" % len(cards)
        for c in cards:
            guard_button_x(c["btn_x"], c["button"], c["btn_y"])   # 越界即抛
        return "%d 屏 / 首屏 %d 行，全部按钮 x ∈ [%d,%d]" % (len(screens), len(cards),
                                                             BTN_X_MIN, BTN_X_MAX)

    # T2 可做行判定（浏览 / 搜一搜）
    def t2():
        cards = parse_rows(detail("race_full_004436.json")[0]["boxes"])
        a = by_name(cards, "好物随心逛")
        assert a["doable"] and a["kind"] == "browse" and a["secs"] == 15, \
            "好物随心逛 应为 browse/15s，实际 %s/%s/%s" % (a["kind"], a["doable"], a["secs"])
        b = by_name(cards, "搜一搜你心仪的宝贝")
        assert b["doable"] and b["kind"] == "search", "搜一搜 应为 search，实际 %s" % b["kind"]
        c = by_name(cards, "去省钱卡领红包")
        assert c["doable"] and c["kind"] == "browse", "去省钱卡领红包 应为可做浏览行"

        # 2026-11-03 用户授权（交接文档「五、待办」第 2/4/5 条）：词级放宽**只放 5 个词**，
        # 且必须同时满足 (a) 明写浏览N秒 (b) 纯浏览流程（kind=browse + 浏览类末端动作）。
        def _mk_row(name, sub, button="去完成"):
            return {"name": name, "sub": sub, "raw": name + " " + sub, "button": button,
                    "btn_x": 1250, "btn_y": 2000, "action": "tap", "n": 0, "m": 1,
                    "key": name, "done": False, "doable": False, "why": "",
                    "kind": "", "hits": [], "ext": []}
        for w in RELAX_BROWSE_WORDS:                      # (a)+(b) 齐 -> 放行，并留审计标记
            r = _mk_row("双11%s任务(0/1)" % w, "浏览5秒 得50金币")
            classify_card(r)
            assert r["doable"] and r["kind"] == "browse", \
                "%s + 明写浏览5秒 应放宽成 browse，实际 kind=%s why=%s" % (w, r["kind"], r["why"])
            assert r.get("relaxed_words") == [w] and r.get("relax_auth") == RELAX_USER_AUTH, \
                "%s 应留下 relaxed_words/relax_auth，实际 %r/%r" % (
                    w, r.get("relaxed_words"), r.get("relax_auth"))
        two = _mk_row("双11视频消消乐任务(0/1)", "浏览5秒 得50金币")
        classify_card(two)
        assert two.get("relaxed_words") == ["视频", "消消乐"], \
            "两个放宽词应都记下，实际 %r" % (two.get("relaxed_words"),)
        for w in RELAX_BROWSE_WORDS:                      # 少了 (a) 明写秒数 -> 照旧不点
            r = _mk_row("双11%s任务(0/1)" % w, "得50金币")
            classify_card(r)
            assert not r["doable"], "%s 但没写浏览秒数 必须仍然拦住，实际 %s/%s" % (
                w, r["kind"], r["why"])
            assert not r.get("relaxed_words"), "%s 没放行就不许留放宽标记" % w
        for w in ("支付", "授权", "助力", "答题"):        # 清单之外的危险词 + 秒数 -> 照旧拦
            r = _mk_row("双11%s任务(0/1)" % w, "浏览5秒 %s+50" % w)
            classify_card(r)
            assert not r["doable"], "%s 不在放宽清单里，必须仍然拦住，实际 %s/%s" % (
                w, r["kind"], r["why"])
        # D（2026-11-03 用户放宽口径③）：「双11理财任务 浏览5秒这种也可以做」（只看不点）。
        # 这类行本来就是走 BROWSE_HINTS 兜底被放行的 —— 现在只补审计痕迹，拦截判定一个字没改。
        fin = _mk_row("双11理财任务(0/1)", "浏览5秒 得50金币")
        classify_card(fin)
        assert fin["doable"] and fin["kind"] == "browse", \
            "理财+浏览5秒 现在就是放行的（口径③），实际 %s/%s" % (fin["kind"], fin["why"])
        assert fin.get("finance_words") == ["理财"], fin.get("finance_words")
        assert fin.get("finance_auth") == RELAX_USER_AUTH, \
            "被放行的金融类行必须留 authorized_by_user=%s，实际 %r" % (
                RELAX_USER_AUTH, fin.get("finance_auth"))
        assert "只看不点" in (fin.get("finance_reason") or ""), fin.get("finance_reason")
        for _n, _s, _b in (("理财任务(0/1)", "浏览5秒 领取50金币", "去完成"),   # 黑名单词照旧拦
                           ("基金任务(0/1)", "浏览5秒 得50金币", "领取奖励")):  # 末端领取类照旧拦
            r = _mk_row(_n, _s, button=_b)
            classify_card(r)
            assert not r["doable"], "%r 本来就该拦住，实际 %s/%s" % (_n, r["kind"], r["why"])
            assert not r.get("finance_auth"), "%r 被拦了就不许留金融授权标记" % (_n,)

        # 2026-11-03 用户授权（交接文档「五、待办」第 3 条）：点击商品领优惠红包(0/1)。
        # 这条是**行级**放宽：必须同时命中「点击商品」+「点击N个商品」，别的地方一个词都不放。
        pc = _mk_row("点击商品领优惠红包(0/1)", "点击3个商品立得币+30")
        classify_card(pc)
        assert pc["doable"] and pc["kind"] == "product_click", \
            "点击商品领优惠红包 应走 product_click，实际 %s/%s" % (pc["kind"], pc["why"])
        assert pc["need"] == 3 and pc["hits"] == [], \
            "应读出 need=3 且清掉「点击商品」这一条命中的黑名单词，实际 %r/%r" % (
                pc.get("need"), pc.get("hits"))
        assert pc.get("click_auth") == RELAX_USER_AUTH, \
            "应留下授权标记 authorized_by_user=%s，实际 %r" % (RELAX_USER_AUTH, pc.get("click_auth"))
        for name, sub, btn in (("点击商品领优惠红包(0/1)", "补贴天天领", "去完成"),   # 没有「点击N个商品」
                               ("点击商品领优惠红包(0/1)", "点击3个商品支付立得", "去完成"),  # 带硬危险词
                               ("点击商品领优惠红包(0/1)", "点击3个商品立得", "领取奖励")):  # 末端是领取类
            r = _mk_row(name, sub, button=btn)
            classify_card(r)
            assert not r["doable"] and r["kind"] != "product_click", \
                "%r/%r/%r 不该走 product_click，实际 %s/%s" % (name, sub, btn, r["kind"], r["why"])

        class _Dev(object):                                # 只喂 device_coords 要的 w/h
            def __init__(self, w, h):
                self.w, self.h = w, h
        key1, pts1 = product_card_points(_Dev(1440, 3168))
        assert key1 == "1440x3168" and pts1 and len(pts1) == 3, \
            "手机1 1440x3168 应登记 3 个商品卡点，实际 %r/%r" % (key1, pts1)
        key2, pts2 = product_card_points(_Dev(1080, 2400))
        assert pts2 is None, "未登记分辨率 %r 不许给商品卡坐标（一次点击都不发），实际 %r" % (
            key2, pts2)

        # ---- 2026-11-04 真机事故（手机1 02:08「去淘宝特价版领红包(0/1)」被连做 4 轮）的修 1 ----
        # 根因：EXT_APPS 是名单制，漏了这个 App -> card["ext"] 为空 -> card["ext_pkg"] 为空
        # -> one_round 的 ext_app_installed 闸根本不触发 -> 被当成普通站内任务点了 4 次。
        assert set(EXT_APPS) == set(VISIT_OK_APPS), \
            "EXT_APPS 与 EXT_APP_PKGS 的名称集合必须完全一致（多一个名字没包名 = 闸不触发）：" \
            "只在 EXT_APPS=%r / 只在 EXT_APP_PKGS=%r" % (
                sorted(set(EXT_APPS) - set(VISIT_OK_APPS)),
                sorted(set(VISIT_OK_APPS) - set(EXT_APPS)))
        assert len(list(EXT_APPS)) == len(set(EXT_APPS)), "EXT_APPS 不许有重名：%r" % (EXT_APPS,)
        assert len(list(EXT_APP_PKGS)) == len(set(n for n, _p, _v in EXT_APP_PKGS)), \
            "EXT_APP_PKGS 不许有重名：%r" % (EXT_APP_PKGS,)
        _lite_verified = [v for _n, _p, v in EXT_APP_PKGS if _n == "淘宝特价版"]
        assert _lite_verified == [False], \
            "淘宝特价版 必须进表且第三列 = False（未用 pm 实测），实际 %r" % (_lite_verified,)
        assert VISIT_OK_APPS.get("淘宝特价版") == "com.taobao.lite", \
            VISIT_OK_APPS.get("淘宝特价版")
        assert "淘宝" not in VISIT_OK_APPS, \
            "「淘宝」站内不是外部 App，绝不许进这张表（进了就等于把整个淘宝面板当跳转类）"
        _lite_row = _mk_row("去淘宝特价版领红包(0/1)", "")
        classify_card(_lite_row)
        assert _lite_row["ext"] == ["淘宝特价版"], \
            "行名含「淘宝特价版」必须识别成外部 App，实际 ext=%r" % (_lite_row["ext"],)
        assert _lite_row["doable"] and _lite_row["kind"] == "browse", \
            "识别后仍应判成可做（做不做由实时 pm 闸决定），实际 %s/%s" % (
                _lite_row["kind"], _lite_row["why"])
        assert _lite_row.get("ext_pkg") == "com.taobao.lite" and \
            _lite_row.get("ext_visit") == "淘宝特价版", \
            "必须带上包名交给 ext_app_installed 闸（没装就是 ext-missing），实际 %r/%r" % (
                _lite_row.get("ext_pkg"), _lite_row.get("ext_visit"))
        # 名字最长优先：同一行同时点名「淘宝特价版」与别的 App 时，不许查成别的包名
        _both = _mk_row("去淘宝特价版和UC领红包(0/1)", "")
        classify_card(_both)
        assert _both.get("ext_pkg") == VISIT_OK_APPS[max(_both["ext"], key=len)], _both.get("ext_pkg")
        return "好物随心逛=browse/15s · 搜一搜=search · 去省钱卡领红包=browse/5s · " \
               "5 个放宽词+浏览5秒=browse(带标记) · 无秒数/清单外危险词=仍拦 · " \
               "点击商品+点击3个商品=product_click(need=3/带授权) · 缺副标题/带支付/末端领取=仍拦 · " \
               "未登记分辨率=不给商品卡坐标 · " \
               "淘宝特价版=ext_pkg com.taobao.lite(未验证 False)/EXT_APPS≡EXT_APP_PKGS 全量 %d 个" % (
                   len(list(EXT_APPS)))

    # T3 跳过行必须带具体原因（用 4 屏合并后的全表，不只看首屏）
    def t3():
        table = merge_scan_rows([parse_rows(s["boxes"])
                                 for s in detail("race_full_004436.json")])
        exp = {"每拉1人": ("邀请", "助力"), "去玩趣味小游戏": ("去玩",),
               "去快手打卡领手机": ("快手",), "淘金币省钱等级考试": ("答题", "考试"),
               "双11来淘金币逛买更省": ("买", "点击去逛")}
        notes = []
        for key, words in exp.items():
            c = by_name(table, key)
            if key == "去快手打卡领手机":
                # 2026-11-03 App 自检（用户拍板）：已装的外部 App 一律放行「点进去逛一下」，
                # 快手因此也进了 VISIT_OK_APPS —— 这一行现在**可以**判成可做。这里断言的是
                # 那条安全链本身（而不是"一律不做"这个旧结论）：必须带 ext_pkg 交给
                # ext_app_installed 在点之前实时查 pm list packages，没装就 ext-missing 跳过。
                # 快手包名未实测确认 -> 两台 pm 都查不到 -> 两台都永远不做（保守一侧，用户已接受）。
                assert c.get("ext_pkg") == VISIT_OK_APPS["快手"], \
                    "快手 行必须带包名交给实时安装闸，实际 %r" % c.get("ext_pkg")
            else:
                assert not c["doable"], "%s 不应被判为可做" % key
            blob = c["why"] + "|".join(c["hits"]) + "|".join(c["ext"])
            assert any(w in blob for w in words), "%s 原因不含 %s，实际：%s" % (key, words, c["why"])
            notes.append("%s->%s" % (key[:8], c["kind"]))
        return " · ".join(notes)

    # T4 淘金币两份全表：展开长表 + 「+50 芯片」版
    def t4():
        long_tbl = merge_scan_rows([parse_rows(s["boxes"])
                                    for s in detail("coin_more_002517.json")])
        chip_tbl = merge_scan_rows([parse_rows(s["boxes"])
                                   for s in detail("coinspeed_tail_002348.json")])
        farm = by_name(long_tbl, "去支付宝农场逛逛哟")
        assert farm["doable"] and farm["kind"] == "alipay", \
            "支付宝农场 应走授权 alipay 通道，实际 %s/%s" % (farm["kind"], farm["why"])
        farm2 = by_name(chip_tbl, "去支付宝农场逛逛哟")
        assert farm2["doable"] and farm2["action"] == "chip", \
            "只有 +50 芯片的支付宝农场行也应可做（round-130 就是点芯片），实际 %s/%s" % (
                farm2["doable"], farm2["why"])
        ant = by_name(long_tbl, "去蚂蚁庄园逛逛哟")
        # 2026-11-03 用户口径 ①：支付宝家族里「点击去逛」这类也算"进去逛一下就返回"，
        # 所以蚂蚁庄园现在**应该可做**（跳转类：目标 App 里一个按钮都不点，只停留 5 秒再返回）。
        assert ant["doable"], "蚂蚁庄园 应属①类可做，实际 %s" % ant["why"]
        assert (ant.get("ext_pkg") or "").endswith("AlipayGphone"), \
            "蚂蚁庄园 应带支付宝包名，实际 %r" % ant.get("ext_pkg")
        # 修 5（2026-11-04 用户拍板）改写了这一行的结论：它的副标题写明「点击去逛」（零点击、
        # 只停留+返回），所以名字里的「视频」按用户口径忽略 -> 现在可做，但**必须**带 ext_pkg
        # 交给 one_round 的 ext_app_installed 闸（头条没装 -> ext-missing，一个点击都不发）。
        tiao = by_name(long_tbl, "头条极速版刷视频")
        assert "点击去逛" in norm(tiao["sub"]), "这条证据行的副标题本该写着「点击去逛」：%r" % tiao["sub"]
        assert tiao["doable"] and tiao.get("ignore_words") == ["视频"], \
            "副标题「点击去逛」+ 名字带「视频」-> 修 5 忽略该词后应可做，实际 %s/%s/%r" % (
                tiao["doable"], tiao["why"], tiao.get("ignore_words"))
        # 2026-11-04 修正期望值：这一行写的是「头条**极速版**」，原先只命中名单里的「头条」->
        # 查的是正式版包名 -> 手机上装了正式版就判「已装」放行 -> 点进去落不到极速版、不计数
        # （手机2 真机试了 2 次才放弃）。现在名单里有了「头条极速版」，最长匹配应取极速版包名。
        assert tiao.get("ext_pkg") == VISIT_OK_APPS["头条极速版"], \
            "这一行点名「头条极速版」-> 必须查极速版包名再交给实时安装闸，实际 %r" % (tiao.get("ext_pkg"),)
        assert tiao.get("authorized_by_user") == RELAX_USER_AUTH and \
            "忽略" in (tiao.get("authorized_reason") or ""), \
            "修 5 放行必须留授权证据，实际 %r/%r" % (
                tiao.get("authorized_by_user"), tiao.get("authorized_reason"))
        # 2026-11-02 用户批准：已装外部 App 的「点进去逛一下」类放开（微博/百度/头条/夸克）。
        # 注意：**不是**「点击去逛」的行仍然照旧挡（刷视频要长时间看，不是停 5 秒就返回）。
        def _mk_ext(name, sub, button="去完成"):
            return {"name": name, "sub": sub, "raw": name + " " + sub, "button": button,
                    "btn_x": 1250, "btn_y": 2000, "action": "tap", "n": 0, "m": 1,
                    "key": name, "done": False, "doable": False, "why": "",
                    "kind": "", "hits": [], "ext": []}
        for nm, sb in (("去微博看看热搜(0/1)", "跳转得奖励 +600"),
                       ("去百度App领现金(0/1)", "完成任务 +400"),
                       ("头条刷热点领现金(0/1)", "点击去逛 +400"),
                       ("去夸克浏览器得金币(0/1)", "点击去逛")):
            c = _mk_ext(nm, sb)
            classify_card(c)
            assert c["doable"], "%s 应已放开（已装外部 App 的逛一下类），实际 %s" % (nm, c["why"])
            assert c.get("ext_pkg"), "%s 应带 ext_pkg，实际 %r" % (nm, c.get("ext_pkg"))
        # 2026-11-03（第 1 条）：百度极速版是**另一个包名**，必须按「最具体的名字」查包。
        # 查错包的后果是对着正式版的安装状态放行/拦住极速版任务（漏拦到安装页 / 误报包名）。
        lite = _mk_ext("去百度极速版领现金(0/1)", "完成任务 +400")
        classify_card(lite)
        assert lite.get("ext_pkg") == VISIT_OK_APPS["百度极速版"], \
            "百度极速版 应查 %s，实际 %r" % (VISIT_OK_APPS["百度极速版"], lite.get("ext_pkg"))
        uc = _mk_ext("去逛UC送红包福利(0/1)", "浏览5秒 +600")
        classify_card(uc)
        assert uc.get("ext_pkg") == VISIT_OK_APPS["UC"], \
            "UC 行 应查 %s，实际 %r" % (VISIT_OK_APPS["UC"], uc.get("ext_pkg"))
        bad = _mk_ext("去京东买东西返金币(0/1)", "下单返 +800")
        classify_card(bad)
        assert not bad["doable"], "带「下单」的外部 App 行仍必须挡住"
        # R5 补齐（2026-11-03 审计发现）：① 类「点进去逛一下」通道也**绝不点领取类按钮**。
        # 真机事故：闲鱼行「去闲鱼币领现金红包(0/1)」末端是「立即领」，曾走 ① 类被真的点了
        # （round-010/046/049）。用户口径：除第 6/7 条授权外，任何位置的领取类都不点。
        # 2026-11-04 用户**再次拍板**（修 3）：只有「副标题写明『点击去逛』+ 行名点名已装外部 App」
        # 这一种行，末端「立即领」允许点（它只是去那个 App 逛一下的入口，进去零点击）。
        # 所以这里断言的是**更窄的新口径**：同一个「立即领」在别的行上照旧一次都不许点。
        xianyu = _mk_ext("去闲鱼币领现金红包(0/1)", "点击去逛 +50", button="立即领")
        classify_card(xianyu)
        assert xianyu["doable"] and xianyu["kind"] == "browse", \
            "修 3：副标题『点击去逛』+ 已装外部 App 的行，领取型入口按钮允许点，实际 %s/%s" % (
                xianyu["kind"], xianyu["why"])
        assert xianyu.get("ext_pkg") == VISIT_OK_APPS["闲鱼"] and xianyu.get("ext_visit") == "闲鱼", \
            "必须带 ext_pkg 交给 ext_app_installed 闸（没装就是 ext-missing），实际 %r/%r" % (
                xianyu.get("ext_pkg"), xianyu.get("ext_visit"))
        assert xianyu.get("authorized_by_user") == CLAIM_ENTRY_AUTH and \
            "点击去逛" in (xianyu.get("authorized_reason") or "") and \
            "零点击" in (xianyu.get("authorized_reason") or ""), \
            "必须留授权证据 authorized_by_user/authorized_reason，实际 %r/%r" % (
                xianyu.get("authorized_by_user"), xianyu.get("authorized_reason"))
        for _nm, _sb in (("去闲鱼币领现金红包(0/1)", "+50"),        # 副标题没有「点击去逛」
                         ("去逛一逛好物(0/1)", "点击去逛 +50")):    # 行名没点名任何外部 App
            _k = _mk_ext(_nm, _sb, button="立即领")
            classify_card(_k)
            assert not _k["doable"], \
                "「立即领」只在『点击去逛』+外部 App 的行上才放，%r/%r 仍必须拦住，实际 %s/%s" % (
                    _nm, _sb, _k["kind"], _k["why"])
        # 2026-11-03 用户**明确授权**（交接文档「五、待办」第 6/7 条）：对「绝不点领取」的唯一例外。
        # 第 6 条：行名「任务到访得金币」+ 末端按钮恰好「领取奖励」-> 同一个 kind（claim_task）。
        arrive = by_name(long_tbl, "任务到访得金币")
        assert arrive["doable"] and arrive["kind"] == CLAIM_KIND, \
            "任务到访得金币 已由用户 2026-11-03 授权（第6条），应判 claim_task，实际 %s/%s" % (
                arrive["kind"], arrive["why"])
        assert arrive["button"] == CLAIM_ROW_BUTTON, arrive["button"]
        assert arrive.get("claim_auth") == CLAIM_USER_AUTH and arrive.get("claim_item") == "第6条", \
            "必须留授权标记 authorized_by_user=%s/第6条，实际 %r/%r" % (
                CLAIM_USER_AUTH, arrive.get("claim_auth"), arrive.get("claim_item"))
        _rc = claim_row_card(arrive)
        assert (_rc["kind"], _rc["slot"], _rc["button"]) == (CLAIM_KIND, "row_button", "领取奖励"), _rc
        assert _rc["x"] == arrive["btn_x"] and _rc["key"] == arrive["key"], _rc
        # 授权**只**到这一处：同族但按钮是「去领取」/带硬危险词/不在白名单词的，照旧绝不点。
        for nm, sb, btn in (("任务到访得金币(0/1)", "每日来任务面板+10", "去领取"),
                            ("任务到访得金币(0/1)", "每日来任务面板+10", "立即领取"),
                            ("邀请好友到访得金币(0/1)", "每日来任务面板+10", "领取奖励"),
                            ("任务到访得金币(0/1)", "下单再得 +300", "领取奖励")):
            c = _mk_ext(nm, sb, button=btn)
            classify_card(c)
            assert not c["doable"] and c["kind"] != CLAIM_KIND, \
                "%r/%r 不在授权范围内，必须仍然拦住，实际 %s/%s" % (nm, btn, c["kind"], c["why"])

        # 第 7 条：只认「与今日累计奖励/累计任务奖励同一 OCR 带 + 文字恰好是立即领取 + 在标题右侧」。
        # 真机赛车帧：累计任务奖励 y=829 / 立即领取 y=845（差 16），只有 1 枚。
        race_top = claim_top_candidates(detail("race_full_004436.json")[0]["boxes"])
        assert len(race_top) == 1 and race_top[0]["title"] == "累计任务奖励", race_top
        assert (race_top[0]["x"], race_top[0]["y"]) == (552, 845), race_top[0]
        # 真机淘金币帧（round-001）：今日累计奖励 y=857 + 三枚立即领取 y=943，按 x 升序先领最小档
        coin_top = claim_top_candidates(
            round_boxes("evidence/2026-11-03-auto/rounds/round-001/02_before_ocr.json"))
        assert len(coin_top) == 3 and all(c["title"] == "今日累计奖励" for c in coin_top), coin_top
        assert [c["x"] for c in coin_top] == [612, 941, 1267], [c["x"] for c in coin_top]
        assert (coin_top[0]["y"], coin_top[0]["dy"]) == (943, 86), coin_top[0]
        # 不在那一排的「立即领取」一个都不点：离锚点太远 / 在标题左边 / 干脆没有锚点。
        assert claim_top_candidates([{"y": 2600, "x": 300, "x2": 520, "t": "立即领取", "s": .99},
                                     {"y": 300, "x": 60, "x2": 300, "t": "今日累计奖励", "s": .99}]) == [], \
            "离锚点太远的立即领取绝不点"
        assert claim_top_candidates([{"y": 860, "x": 600, "x2": 800, "t": "立即领取", "s": .99},
                                     {"y": 857, "x": 900, "x2": 1100, "t": "今日累计奖励", "s": .99}]) == [], \
            "标题左边的立即领取绝不点（不在那一排的右侧）"
        assert claim_top_candidates([{"y": 900, "x": 300, "x2": 500, "t": "立即领取", "s": .99}]) == [], \
            "没有累计奖励锚点时不点（绝不放宽到面板其他位置的立即领取）"
        assert claim_top_candidates([{"y": 1900, "x": 300, "x2": 500, "t": "立即领取", "s": .99},
                                     {"y": 1902, "x": 60, "x2": 200, "t": "今日累计奖励", "s": .99}]) == [], \
            "锚点不在面板顶部（y>%d）时不点" % CLAIM_TOP_MAX_Y
        assert claim_top_card([]) is None and claim_top_card(
            [{"y": 900, "x": 300, "x2": 500, "t": "立即领取", "s": .99}]) is None
        _tc = claim_top_card(coin_top and round_boxes(
            "evidence/2026-11-03-auto/rounds/round-001/02_before_ocr.json"))
        assert _tc and _tc["kind"] == CLAIM_KIND and _tc["slot"] == "top_band", _tc
        assert (_tc["x"], _tc["y"], _tc["button"]) == (612, 943, "立即领取"), _tc
        assert _tc["item"] == "第7条" and CLAIM_USER_AUTH in _tc["reason"], _tc["reason"]
        assert by_name(long_tbl, "发现精选好物")["doable"], "发现精选好物 应可做"
        assert by_name(long_tbl, "搜一搜你心仪的宝贝")["kind"] == "search", "搜一搜 应为 search"
        return ("支付宝农场(按钮)=alipay · 支付宝农场(+50 芯片)=alipay · 蚂蚁庄园=跳过 · 头条=ext-app"
                " · 任务到访得金币+领取奖励=claim_task(第6条，其他领取类仍拦)"
                " · 顶部累计奖励立即领取 赛车1枚/淘金币3枚(第7条，同一OCR带+标题右侧+顶部三条硬口径)")

    # T5 R1 守卫边界
    def t5():
        for ok_x in (BTN_X_MIN, 1255, BTN_X_MAX):
            guard_button_x(ok_x, "去完成", 100)
        bad = 0
        for bad_x in (BTN_X_MIN - 1, BTN_X_MAX + 1, 1078):
            try:
                guard_button_x(bad_x, "去完成", 100)
            except GuardError:
                bad += 1
        assert bad == 3, "越界值应立即抛 GuardError"
        return "[%d,%d] 放行；%d/%d/1078 全部抛 GuardError" % (BTN_X_MIN, BTN_X_MAX,
                                                             BTN_X_MIN - 1, BTN_X_MAX + 1)

    # T6 R2 面板判定
    def t6():
        ok, info = panel_ok(detail("race_full_004436.json")[0]["boxes"], "race")
        assert ok and info["rows"] >= 3, "赛车面板应判在前台：%s" % info
        ok2, info2 = panel_ok(detail("coin_more_002517.json")[0]["boxes"], "coin")
        assert ok2, "淘金币面板应判在前台：%s" % info2
        ok3, _ = panel_ok([{"y": 100, "x": 200, "x2": 400, "t": "这是一段无关文字", "s": 0.9}], "coin")
        assert not ok3, "无标题无行的屏幕不应判成面板"
        return "race=%s coin=%s 无关屏=否" % (info["rows"], info2["rows"])

    # T7 进度 / 金币总数（OCR 会把数字和后面的字粘一起）
    def t7():
        p = progress_in_text("当前进度 47/50 累计任务奖励", "race")
        assert p == 47, "进度解析失败：%s" % p
        v = coin_total([{"y": 80, "x": 0, "x2": 10, "t": "26001全场20亿抵", "s": 0.9}])
        assert v == 26001, "金币总数应做子串匹配（26001），实际 %s" % v
        real_gold = coin_total(round_boxes("evidence/2026-11-02/round-130/02_before_ocr.json"))
        assert real_gold and 1000 <= real_gold <= 9999999, "真实金币帧解析失败：%s" % real_gold
        return "47/50→47 · 粘连金币帧→26001 · round-130 真实帧→%s" % real_gold

    # T8 R4 热词 chip：不能选中页面标题「搜索有福利」
    def t8():
        rows = [
            {"y": 400, "x": 60, "x2": 520, "t": "搜索有福利", "s": 0.99},
            {"y": 560, "x": 100, "x2": 260, "t": "搜索发现", "s": 0.99},
            {"y": 640, "x": 110, "x2": 330, "t": "空气净化器", "s": 0.99},
            {"y": 640, "x": 380, "x2": 560, "t": "羽绒服", "s": 0.99},
            {"y": 760, "x": 110, "x2": 300, "t": "综合排序", "s": 0.99},
        ]
        chips, why = chip_candidates(rows)
        names = [c["t"] for c in chips]
        assert names, "应选出 chip：%s/%s" % (why, names)
        assert "搜索有福利" not in names, "页面标题被当成 chip 了（round-034 事故）"
        assert names[0] == "空气净化器", "首个 chip 应为 空气净化器，实际 %s" % names
        return "选中 %s（排除标题「搜索有福利」）" % names[0]

    # T9 全表合并去重
    def t9():
        screens = detail("race_full_004436.json")
        per = [parse_rows(s["boxes"]) for s in screens]
        table = merge_scan_rows(per)
        assert len(table) == len({r["key"] for r in table}), "合并后仍有重复 key"
        assert len(table) >= len(per[0]), "合并结果不应少于首屏"
        return "%d 屏 → %d 行，无重复 key" % (len(per), len(table))

    # T10 证据落盘：六件套目录 + ledger 一行
    def t10():
        import tempfile
        import shutil as _sh
        tmp = tempfile.mkdtemp(prefix="autotest-")
        try:
            class _Args:
                minutes = 1.0
                dwell = 0
                quiet = True
                max_rounds = 1

            class _Dev:
                serial = "offline-fake"

                def device_time(self):
                    return "offline"

            ctx = Ctx(os.path.join(tmp, "ev"), _Dev(), _Args())
            r = Round(ctx, "race")
            for name in ("01_before.png", "02_before_ocr.json", "03_action.json",
                         "04_after.png", "05_after_ocr.json"):
                with open(r.p(name), "w", encoding="utf8") as f:
                    f.write("{}")
            r.verdict(True, "offline", "advanced", row_after=[1, 5])
            files = sorted(os.listdir(r.dir))
            with open(ctx.ledger, encoding="utf8") as f:
                ledger = [json.loads(x) for x in f.read().splitlines() if x.strip()]
            assert len(files) == 6, "六件套不齐：%s" % files
            assert ledger and ledger[0]["round"] == 1 and ledger[0]["ok"] is True, "ledger 行不对"

            # 2026-11-03 第五批·任务三：--minutes 0（或负数）= 不设时间上限，
            # 不是「跑 0 分钟就收工」——left() 必须是很大的值，预算检查永不触发。
            class _ArgsU:
                minutes = 0
                dwell = 0
                quiet = True
                max_rounds = 1

            ctx_u = Ctx(os.path.join(tmp, "ev-u"), _Dev(), _ArgsU())
            assert ctx_u.unlimited is True, ctx_u.unlimited
            assert ctx_u.left() > 1e8 and ctx_u.left() >= MIN_ROUND_LEFT_S, ctx_u.left()
            ctx_n = Ctx(os.path.join(tmp, "ev-n"), _Dev(), _Args())
            assert ctx_n.unlimited is False and ctx_n.left() < 120, ctx_n.left()

            # 2026-11-03 第五批·任务B：panel_done.json v2 按设备分开记（v1 当无记录，不误跳过）。
            # STATE_DIR 临时改到 tmp 下，绝不碰仓库里的 coin11/state/
            global STATE_DIR
            _old_state = STATE_DIR
            STATE_DIR = os.path.join(tmp, "state")
            try:
                class _DevA:
                    serial = "192.168.1.8:5555"

                class _DevB:
                    serial = "192.168.1.5:43059"

                assert panel_done_mark(_DevA(), "coin", log=lambda *a: None) is True
                assert panel_done_done_today(_DevA(), "coin") == _today()
                assert panel_done_done_today(_DevB(), "coin") == "",                     "手机1 记过的面板不许把手机2 也跳过（这就是 2026-11-03 真机的 bug）"
                assert panel_done_mark(_DevB(), "race", log=lambda *a: None) is True
                doc = jload(_panel_done_path())
                assert doc.get("v") == 2, doc
                assert set(doc["panels"]) == {"192.168.1.8:5555", "192.168.1.5:43059"}, doc
                assert doc["panels"]["192.168.1.8:5555"] == {"coin": _today()}, doc
                assert doc["panels"]["192.168.1.5:43059"] == {"race": _today()}, doc
                with open(_panel_done_path(), "w", encoding="utf8") as f:
                    json.dump({"v": 1, "panels": {"coin": _today(), "race": _today()}}, f)
                assert panel_done_load() == {}, "v1 旧格式必须当「未知设备」忽略"
                assert panel_done_done_today(_DevA(), "coin") == "", "v1 不许造成误跳过"
            finally:
                STATE_DIR = _old_state
            return "round-001 六件套 %s + ledger %d 行" % (" ".join(files), len(ledger))
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    # T11 源码卫生：不 import v2/、不依赖任何模型/推理框架
    def t11():
        import ast
        src_text = open(_own_source(), encoding="utf8").read()
        mods = set()
        for node in ast.walk(ast.parse(src_text)):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        # custom_script = 本仓库自己的模块（coin11/custom_script.py，自定义脚本回放器）；
        # 它不是第三方依赖、更不是模型 SDK，登记进来让依赖可见（2026-11-02）。
        allowed = {"__future__", "argparse", "ast", "atexit", "datetime", "json", "numpy", "os",
                   "re", "shutil", "sys", "tempfile", "time", "traceback",
                   "adbutils", "rapidocr_onnxruntime", "PIL", "custom_script"}
        stray = sorted(m for m in mods if m not in allowed)
        assert not stray, "出现白名单外的 import：%s" % stray
        need = {"adbutils", "rapidocr_onnxruntime", "PIL", "numpy"}
        assert need <= mods, "缺少声明依赖：%s" % sorted(need - mods)
        return "import 白名单干净：%s（无 v2 / 无模型 SDK）" % ",".join(sorted(mods))

    # T12 端到端一轮（假设备，真 OCR 盒）：解析→守卫→点击→落地→BACK→复核→判定→六件套
    def t12():
        import tempfile
        import shutil as _sh
        from PIL import Image

        # 2026-11-03 第五批·任务一（R14）：写死坐标按屏分辨率取表，未登记的不许瞎猜。
        # 纯离线断言（假设备只有 w/h 两个属性，零 adb）。
        class _D1:
            w, h = 1440, 3168      # 手机1 V2505A

        class _D2:
            w, h = 1440, 3200      # 手机2 V2243A

        class _D3:
            w, h = 1080, 2400      # 没登记过的分辨率

        k1, s1 = written_nav_steps(_D1())
        k2, s2 = written_nav_steps(_D2())
        k3, s3 = written_nav_steps(_D3())
        assert k1 == "1440x3168" and s1, k1
        assert s1["coin"]["primary"][0]["xy"] == (159, 938), s1["coin"]["primary"][0]
        # 2026-11-07 日常版（真机 OCR 实测）：币页中间大红按钮 = 未签到「签到领金币」/
        # 签完「赚更多金币」，位置不变；同一坐标连点两次（签到 -> 进面板）。
        assert s1["coin"]["primary"][1]["xy"] == (719, 857), s1["coin"]["primary"][1]
        assert len(s1["coin"]["primary"]) == 2, s1["coin"]["primary"]   # 签到由预检单独处理，主路径仍是 2 拍
        assert s1["race"]["primary"][1]["xy"] == (1299, 1357), s1["race"]["primary"][1]
        assert k2 == "1440x3200" and s2, k2
        assert s2["coin"]["primary"][0]["xy"] == (721, 890), s2["coin"]["primary"][0]
        assert s2["coin"]["primary"][1]["xy"] == (721, 889), s2["coin"]["primary"][1]
        assert len(s2["coin"]["primary"]) == 2, s2["coin"]["primary"]
        assert s2["race"]["primary"][1]["xy"] == (1299, 1322), s2["race"]["primary"][1]
        assert s3 is None and k3 == "1080x2400", (k3, s3)
        # 2026-11-03 用户口径：折叠面板的「展开」不写死坐标 —— 只认当帧 OCR 读到的文字，
        # 坐标取那个盒子的中心；读不到就一次都不点（R5 黑名单风险）。
        fold_rows = [{"y": 397, "x": 120, "x2": 700, "t": "今日速赚800淘金币", "s": .99},
                     {"y": 2600, "x": 300, "x2": 420, "t": "展开∨", "s": .98}]
        fb = fold_button(fold_rows)
        assert fb and fb["y"] == 2600 and fb["x"] == 360, fb
        assert fold_button([{"y": 900, "x": 120, "x2": 700, "t": "去看看展开的活动(1/2)", "s": .9}]) is None, \
            "任务行里带「展开」两个字的不许当折叠按钮"
        assert fold_button([{"y": 1500, "x": 200, "x2": 400, "t": "查看更多", "s": .9}]), "查看更多 也算折叠"
        assert fold_button([]) is None

        panel_before = detail("race_full_004436.json")[0]["boxes"]
        target = pick_target(parse_rows(panel_before))
        assert target, "首屏应有可做行"
        panel_after = [dict(b) for b in panel_before]
        hit = False
        for b in panel_after:
            t = str(b["t"])
            if str(target["name"])[:6] in t and re.search(r"[(（]\s*\d+\s*/\s*\d+\s*[)）]", t):
                b["t"] = re.sub(r"[(（]\s*(\d+)\s*/\s*(\d+)\s*[)）]",
                                lambda m: "(%d/%s)" % (int(m.group(1)) + 1, m.group(2)), t)
                hit = True
        assert hit, "构造复核帧失败：%s" % target["name"]

        class _FakeDev:
            serial = "offline-fake"
            w, h = 1440, 3200

            def __init__(self, frames, pngs):
                self.frames, self.i = frames, 0
                # 2026-11-02 用户要求（第 8 条）：点后那一帧必须与点前面板**不一样**，
                # 否则快速路径会（正确地）判成「没落地」→ skipped。
                self.pngs, self.j = list(pngs), 0
                self.taps, self.backs, self.swipes = [], 0, 0

            def _next(self):
                f = self.frames[min(self.i, len(self.frames) - 1)]
                self.i += 1
                return f

            def device_time(self):
                return "offline"

            def shot(self, path):
                src = self.pngs[min(self.j, len(self.pngs) - 1)]
                self.j += 1
                _sh.copyfile(src, path)
                return path

            def ocr(self, path, scale=None):
                return self._next()

            def app_current(self):
                return ("com.taobao.taobao", "com.taobao.themis.container.app.TMSActivity")

            def focus(self):
                return "mCurrentFocus=com.taobao.taobao/TMSActivity"

            def focus_short(self):
                return "com.taobao.taobao/TMSActivity"

            def tap(self, x, y):
                self.taps.append((int(x), int(y)))

            def back(self):
                self.backs += 1

            def swipe(self, *a, **k):
                self.swipes += 1

            def is_awake(self):
                return True

            def wake(self):
                return True

            def shell(self, cmd, timeout=90):
                return ""

            def am_start_taobao(self):
                return ""

        tmp = tempfile.mkdtemp(prefix="autoround-")
        try:
            png_panel = os.path.join(tmp, "frame_panel.png")
            png_landing = os.path.join(tmp, "frame_landing.png")
            Image.new("RGB", (1440, 3200), (40, 40, 40)).save(png_panel)
            Image.new("RGB", (1440, 3200), (200, 200, 200)).save(png_landing)

            class _Args:
                minutes = 1.0
                dwell = 0
                quiet = True
                max_rounds = 5

            # 帧序：01_before(面板) → 04_after(落地页) → 停留后(落地页) → 07 复核(面板)
            dev = _FakeDev([panel_before, panel_after, panel_after, panel_after],
                           [png_panel, png_landing, png_landing, png_panel])
            ctx = Ctx(os.path.join(tmp, "ev"), dev, _Args())
            # 2026-11-03 第 7 条（顶部累计奖励「立即领取」）在上面的 T4 里有专项断言；
            # 本用例只验「浏览类端到端一轮」，所以把那一拍按上限关掉（真机不受影响）。
            ctx.claim_top_taps = CLAIM_TOP_MAX_TAPS
            res = one_round(dev, ctx, "race")
            assert res["status"] == "advanced" and res["ok"], "一轮应判 advanced，实际 %s" % res
            assert len(dev.taps) == 1, "一轮只应点一次，实际 %d" % len(dev.taps)
            tx, ty = dev.taps[0]
            guard_button_x(tx, "去完成", ty)                      # 真点出去的坐标也必须过守卫
            rec = ctx.rounds[0]
            assert rec["picked"]["kind"] == "browse" and rec["landing"] == "taobao", rec["landing"]
            files = sorted(os.listdir(os.path.join(ctx.rounds_dir, "round-001")))
            for need in ("01_before.png", "02_before_ocr.json", "03_action.json",
                         "04_after.png", "05_after_ocr.json", "06_verdict.json"):
                assert need in files, "缺证据文件 %s（有 %s）" % (need, files)
            want = [(target["n"] or 0) + 1, target["m"]]
            assert rec["row_after"] == want, "计数应 %s/%s -> %s/%s，实际 %s" % (
                target["n"], target["m"], want[0], want[1], rec["row_after"])

            # ---- 修 2（2026-11-04 真机事故的主修）：同一行本轮的尝试次数硬上限 ----
            # 旧事故：升档成功的两轮「不动放弃额度」，0->1、1->2 都不扣分，到顶才 +1、再一轮 +1
            # 才被排除 -> 一行被点 4 次（手机1 02:08）。现在点击**之前**就 +1 记在 ctx.try_count，
            # 到 MAX_ROW_TRIES 就拦在第 4 次之前，并 give_up=2 把它整行排除。
            assert MAX_ROW_TRIES == 3, "上限就是 3 次（事故那一行被连做 4 轮），实际 %r" % (
                MAX_ROW_TRIES,)
            assert rec.get("try_no") == 1, "点下去那一刻就要记第 1 次，实际 %r" % (rec.get("try_no"),)
            assert target["key"] not in ctx.try_count, \
                "做成一行必须把尝试计数清零（上限只治原地打转，不治正常的多轮累计）：%r" % (
                    ctx.try_count,)
            # 第 3 次还点得下去（上限不是 2）
            dev_k2 = _FakeDev([panel_before, panel_after, panel_after, panel_after],
                              [png_panel, png_landing, png_landing, png_panel])
            ctx_k2 = Ctx(os.path.join(tmp, "ev-cap2"), dev_k2, _Args())
            ctx_k2.claim_top_taps = CLAIM_TOP_MAX_TAPS
            ctx_k2.try_count[target["key"]] = MAX_ROW_TRIES - 1
            res_k2 = one_round(dev_k2, ctx_k2, "race")
            assert res_k2["ok"] is True and len(dev_k2.taps) == 1, \
                "已经点过 %d 次的那一行，第 %d 次仍要照点：%r / taps=%r" % (
                    MAX_ROW_TRIES - 1, MAX_ROW_TRIES, res_k2, dev_k2.taps)
            assert ctx_k2.rounds[0].get("try_no") == MAX_ROW_TRIES, ctx_k2.rounds[0].get("try_no")
            # 第 4 次：点之前就被拦住 + give_up>=2 + [cap] 日志
            dev_k3 = _FakeDev([panel_before, panel_after, panel_after, panel_after],
                              [png_panel, png_landing, png_landing, png_panel])
            ctx_k3 = Ctx(os.path.join(tmp, "ev-cap3"), dev_k3, _Args())
            ctx_k3.claim_top_taps = CLAIM_TOP_MAX_TAPS
            ctx_k3.try_count[target["key"]] = MAX_ROW_TRIES
            res_k3 = one_round(dev_k3, ctx_k3, "race")
            assert res_k3["status"] == "try-cap" and res_k3["ok"] is False, \
                "到上限必须判 try-cap（不是继续做），实际 %r" % (res_k3,)
            assert dev_k3.taps == [], "到上限还点就是漏拦（第 %d 次点出去了）：%r" % (
                MAX_ROW_TRIES + 1, dev_k3.taps)
            assert ctx_k3.give_up.get(target["key"], 0) >= 2, \
                "第 4 次之前必须已经 give_up>=2（pick_target 才会整行排除）：%r" % (ctx_k3.give_up,)
            _caplog = ""
            try:
                with open(ctx_k3.log_path, encoding="utf8") as f:
                    _caplog = f.read()
            except Exception:
                pass
            assert "[cap]" in _caplog and "上限 %d" % MAX_ROW_TRIES in _caplog, \
                "日志必须有 [cap] 那一行（ASCII 符号）：%r" % (_caplog[-400:],)

            # ---- 修 1（同日事故）：行名点名「淘宝特价版」+ 该 App 未装 -> ext-missing，不点 ----
            # 行名盒必须比按钮盒高 ≥15px（parse_rows 的 ypad_top 口径），所以行名 y = 按钮 y-40
            ext_panel = [
                {"y": 300, "x": 120, "x2": 700, "t": "做任务赚能量", "s": .99},
                {"y": 960, "x": 120, "x2": 700, "t": "邀请好友赚能量(0/1)", "s": .99},
                {"y": 1000, "x": 1240, "x2": 1300, "t": "去完成", "s": .99},
                {"y": 1360, "x": 120, "x2": 700, "t": "去玩趣味小游戏(0/1)", "s": .99},
                {"y": 1400, "x": 1240, "x2": 1300, "t": "去完成", "s": .99},
                {"y": 1760, "x": 120, "x2": 900, "t": "去淘宝特价版领红包(0/1)", "s": .99},
                {"y": 1800, "x": 1240, "x2": 1300, "t": "去完成", "s": .99},
            ]
            assert pick_target(parse_rows(ext_panel))["ext_pkg"] == "com.taobao.lite", \
                "这一屏只有淘宝特价版那一行可做（另两行是黑名单诱饵），实际 %r" % (
                    pick_target(parse_rows(ext_panel)),)

            class _PmDev(_FakeDev):
                """修 1 证明用：shell 的 pm list packages **不含** com.taobao.lite（手机1 没装它）。"""

                def __init__(self, frames, pngs, pkgs):
                    _FakeDev.__init__(self, frames, pngs)
                    self.pm = "".join("package:" + p + chr(10) for p in pkgs)

                def shell(self, cmd, timeout=90):
                    return self.pm if "pm list packages" in str(cmd) else ""

            _pkgs_no_lite = [p for _n, p, _v in EXT_APP_PKGS if p != "com.taobao.lite"]
            _g12 = globals()
            _saved_cache = _g12.get("_INSTALLED_PKGS")
            _g12["_INSTALLED_PKGS"] = None       # 不许用别的用例留下的安装缓存
            try:
                dev_ext = _PmDev([ext_panel], [png_panel], _pkgs_no_lite)
                ctx_ext = Ctx(os.path.join(tmp, "ev-ext"), dev_ext, _Args())
                ctx_ext.claim_top_taps = CLAIM_TOP_MAX_TAPS
                res_ext = one_round(dev_ext, ctx_ext, "race")
                assert res_ext["status"] == "ext-missing", \
                    "外部 App 没装必须判 ext-missing（复用既有路径，不新写分支），实际 %r" % (
                        res_ext,)
                assert res_ext["ok"] is False and dev_ext.taps == [], \
                    "没装还点 = 事故复现：taps=%r" % (dev_ext.taps,)
                _ext_key = "去淘宝特价版领红包"
                assert int(ctx_ext.give_up.get(_ext_key, 0)) >= 2, \
                    "ext-missing 必须顺手 give_up>=2（别在同一行反复试）：%r" % (ctx_ext.give_up,)
                _extrec = ctx_ext.rounds[0]
                assert "外部 App 未安装（淘宝特价版，com.taobao.lite）" in str(_extrec["reason"]), \
                    _extrec["reason"]
                assert "-> 不点，换下一行" in str(_extrec["reason"]), _extrec["reason"]
            finally:
                _g12["_INSTALLED_PKGS"] = _saved_cache

            # 反例一（2026-11-02 第 8 条 + 2026-11-03 新口径）：
            # 焦点 3 秒没变 + 像素指纹没变 + BACK 一次后行还在 = 这一轮**真没落地**
            # -> 必须 skipped、landing="panel"、一个滑动都不许发出去。
            # 复核帧必须是「同一屏、行还在、计数没动」（否则会（正确地）判 disappeared）。
            # 独立 dev / ctx / 子目录，避免污染上面那个正向用例。
            dev_neg = _FakeDev([panel_before, panel_before], [png_panel, png_panel])
            ctx_neg = Ctx(os.path.join(tmp, "ev-neg"), dev_neg, _Args())
            ctx_neg.claim_top_taps = CLAIM_TOP_MAX_TAPS   # 同上：本用例只验浏览类
            res_neg = one_round(dev_neg, ctx_neg, "race")
            assert res_neg["status"] == "skipped" and res_neg["ok"] is False, \
                "点后仍在面板必须判 skipped/ok=False，实际 %s" % res_neg
            assert dev_neg.swipes == 0, \
                "第 8 条：在面板上一个滑动都不许发，实际 %d 次" % dev_neg.swipes
            assert dev_neg.backs >= 1, "判成可能没落地后必须先 BACK 一次（实际 %d 次）" % dev_neg.backs
            rec_neg = ctx_neg.rounds[0]
            assert rec_neg["landing"] == "panel", "landing 应为 panel，实际 %s" % rec_neg["landing"]
            px_neg = rec_neg.get("landing_pixel") or {}
            assert (px_neg.get("score") or 0) >= LANDING_SAME_PANEL_SCORE, \
                "像素指纹应 ≥ %s，实际 %s" % (LANDING_SAME_PANEL_SCORE, px_neg.get("score"))
            assert px_neg.get("source") == "fast", "反例应走快速路径，实际 %s" % px_neg.get("source")
            # 证据字段（2026-11-03 第 4 条，只加不改）：焦点三件套 + 相对 tap 的毫秒
            assert px_neg.get("focus_changed") is False, px_neg
            assert px_neg.get("focus_before") and px_neg.get("focus_after"), px_neg
            assert int(px_neg.get("judged_at_ms") or 0) >= int(LANDING_FOCUS_MAX_S * 1000), \
                "焦点一直没变时必须等满 %ss 才下结论，实际 %sms" % (
                    LANDING_FOCUS_MAX_S, px_neg.get("judged_at_ms"))
            back_ev = px_neg.get("after_back") or {}
            assert back_ev.get("source") == "fast-back" and back_ev.get("still_panel") is True, \
                "BACK 后还必须留一帧证据（source=fast-back 且仍在面板）：%s" % back_ev
            assert (rec_neg.get("dwell") or {}).get("swipes") == 0, rec_neg.get("dwell")
            who = ["焦点未变", "像素未变", "BACK 一次"]
            assert all(w in (rec_neg.get("reason") or "") for w in who), \
                "skipped 的 reason 要写全三项证据（%s）：%s" % (who, rec_neg.get("reason"))
            neg_files = sorted(os.listdir(os.path.join(ctx_neg.rounds_dir, "round-001")))
            for need in ("01_before.png", "03_action.json", "04_after.png",
                         "05_after_ocr.json", "06_verdict.json"):
                assert need in neg_files, "反例缺证据文件 %s（有 %s）" % (need, neg_files)

            # 反例二（2026-11-03，20:53 真机误判的回归用例）：
            # 像素闸仍然说「没落地」（帧完全一样），但 BACK 回面板后**计数 +1** ——
            # 用户原话「任务消失了大概率是完成了」-> 这一轮必须记成功，绝不许 skipped。
            dev_gone = _FakeDev([panel_before, panel_after], [png_panel, png_panel])
            ctx_gone = Ctx(os.path.join(tmp, "ev-gone"), dev_gone, _Args())
            ctx_gone.claim_top_taps = CLAIM_TOP_MAX_TAPS  # 同上：本用例只验浏览类
            res_gone = one_round(dev_gone, ctx_gone, "race")
            rec_gone = ctx_gone.rounds[0]
            assert res_gone["ok"] is True and res_gone["status"] in ("advanced", "disappeared"), \
                "行已消失/计数已增加必须判完成，实际 %s" % res_gone
            assert dev_gone.swipes == 0, "跳转/没落地都不许在面板上滑动，实际 %d" % dev_gone.swipes
            assert (rec_gone.get("landing_pixel") or {}).get("after_back"), rec_gone

            # 2026-11-03 缺陷 1/2（用户拍板「那就多等一下，刷新了再抓取」）离线断言。
            # 真机证据：round-095/096 的 08_product_click.json 里 page_progress[].done/need/
            # band_y 全 null（立刻抓帧时页面还在半加载），流程只能 remaining -= 1 盲减 ->
            # 真实 2/3 就收手，第三发重复点在同一张已点过的卡上。这里用桩对象验三件事：
            #   a) 页面迟迟读不到进度 -> 会重试等待，且 page_ready=false 落进证据；
            #   b) 读不到进度 -> 固定点**会前进**（连续两发不落在同一个点上）；
            #   c) 读到 done=3/need=3 -> 正常收手，一发都不多点。
            _pg = globals()
            _save_pg = {k: _pg[k] for k in ("product_page_progress_pos", "PRODUCT_READY_POLL_S",
                                            "PRODUCT_TAP_SETTLE_S", "PRODUCT_BACK_SETTLE_S")}
            _pg["PRODUCT_READY_POLL_S"] = 0.0     # 离线不等 0.8s（真机照旧 0.8s）
            _pg["PRODUCT_TAP_SETTLE_S"] = 0.0     # 离线不等 2.6s
            _pg["PRODUCT_BACK_SETTLE_S"] = 0.0    # 离线不等 1.8s

            class _PDev:
                """只喂 product_click_flow 要的那几样（shot/focus/tap/back），零 adb。

                w/h 取手机1 1440x3168 —— 那是 DEV_COORDS 里唯一实测登记了 product_cards 的分辨率。
                """
                serial = "offline-product"
                w, h = 1440, 3168
                PAGE = ("mCurrentFocus=Window{1 u0 com.taobao.taobao/"
                        "com.taobao.themis.container.app.BrowserActivity}")
                DETAIL = ("mCurrentFocus=Window{2 u0 com.taobao.taobao/"
                          "com.taobao.taobao.TTDetailActivity}")

                def __init__(self):
                    self.taps, self.backs, self.frames = [], 0, 0
                    self.focus_now = self.PAGE

                def device_time(self):
                    return "offline"

                def shot(self, path):
                    self.frames += 1
                    open(path, "wb").close()      # 假 PNG 占位；进度由桩函数给

                def focus(self):
                    return self.focus_now

                def tap(self, x, y):
                    self.taps.append((int(x), int(y)))
                    self.focus_now = self.DETAIL     # 点卡片 -> 商品详情页（焦点变坏）

                def back(self):
                    self.backs += 1
                    self.focus_now = self.PAGE       # 详情页内零点击：只有 BACK

            _pc_n = [0]

            def _run_pflow(seq):
                """seq = 每抓一帧要读到的进度 (x,n,y)；None = 这一帧读不到（= 页面还在半加载）。"""
                _pc_n[0] += 1
                d = _PDev()
                frames = list(seq)

                def _stub(_dev, _path):
                    return frames.pop(0) if frames else (None, None, None)

                _pg["product_page_progress_pos"] = _stub
                c = Ctx(os.path.join(tmp, "ev-pflow%d" % _pc_n[0]), d, _Args())
                rp = Round(c, "race", kind="row")
                card = {"key": "点击商品领优惠红包(0/1)", "need": 3}
                return d, product_click_flow(d, c, rp, card)

            try:
                pc_key, pc_pts = product_card_points(_PDev())
                assert pc_key == "1440x3168" and pc_pts and len(pc_pts) == 3, (pc_key, pc_pts)
                # (a)+(b) 页面从头到尾读不到进度：进页面要重试等待，page_ready=false 落进证据；
                # 每一发 BACK 回来也读不到 -> 不盲减、固定点前进（三个实测点会被依次用到）。
                d_b, rec_b = _run_pflow([None] * PRODUCT_READY_MAX_TRIES)
                assert rec_b["page_ready"] is False,                     "读不到进度时 page_ready 必须落成 false，实际 %r" % (rec_b.get("page_ready"),)
                assert rec_b["ready_tries"] == PRODUCT_READY_MAX_TRIES,                     "进页面要先重试等待 %d 次，实际 %r 次" % (
                        PRODUCT_READY_MAX_TRIES, rec_b.get("ready_tries"))
                assert rec_b["ready_ms"] >= 0, rec_b.get("ready_ms")
                assert d_b.frames == PRODUCT_READY_MAX_TRIES + 3 * PRODUCT_RECHECK_TRIES,                     "抓帧次数应为「进页面 %d 次 + 每发回来各 %d 次」= %d，实际 %d" % (
                        PRODUCT_READY_MAX_TRIES, PRODUCT_RECHECK_TRIES,
                        PRODUCT_READY_MAX_TRIES + 3 * PRODUCT_RECHECK_TRIES, d_b.frames)
                assert [tuple(t["tap"]) for t in rec_b["taps"]] == list(pc_pts),                     "读不到进度时三发必须依次落到三个实测固定点（不许一直打同一个点），实际 %r" % (
                        [t.get("tap") for t in rec_b["taps"]],)
                ys = [t["tap"][1] for t in rec_b["taps"]]
                assert ys == sorted(set(ys)) and len(set(ys)) == 3,                     "连续两发不许落在同一个固定点上（缺陷 2），实际 y=%r" % (ys,)
                assert all(t.get("why") == "progress-unreadable-advance" for t in rec_b["taps"]),                     "读不到进度那一发要标 why=progress-unreadable-advance，实际 %r" % (
                        [t.get("why") for t in rec_b["taps"]],)
                assert rec_b["closed_by"] == "need-opened" and rec_b["opened_detail"] == 3,                     "读不到进度时应按「点满 need 发且每发都打开过详情页」收手，实际 %r/%r" % (
                        rec_b.get("closed_by"), rec_b.get("opened_detail"))
                assert rec_b["remaining_after"] is None and                     rec_b["remaining_source"] == "unreadable",                     "读不到进度时绝不许再拿 remaining 当结论，实际 %r/%r" % (
                        rec_b.get("remaining_after"), rec_b.get("remaining_source"))
                assert d_b.backs >= 3 and all(t.get("back") == 1 for t in rec_b["taps"]),                     "每打开一张详情页只能发 1 次 BACK（详情页内零点击），实际 backs=%d" % d_b.backs
                assert len(d_b.taps) == 3, "保险丝内只该点 3 发，实际 %d" % len(d_b.taps)
                # (c) 页面就绪且已 3/3：一发都不多点。
                d_c, rec_c = _run_pflow([(3, 3, 700)])
                assert rec_c["page_ready"] is True and rec_c["ready_tries"] == 1,                     "读到进度就该第一次就收手等待，实际 ready=%r tries=%r" % (
                        rec_c.get("page_ready"), rec_c.get("ready_tries"))
                assert d_c.taps == [], "已 3/3 还发点击就是多点，实际 %r" % (d_c.taps,)
                assert rec_c["remaining_after"] == 0 and rec_c["closed_by"] == "progress-zero",                     "读到 3/3 应以 progress-zero 收手，实际 %r/%r" % (
                        rec_c.get("remaining_after"), rec_c.get("closed_by"))
                # (c2) 恢复正常（进度逐步读得到）：仍然「成功就一直用同一个点」，3 发收手。
                d_d, rec_d = _run_pflow([(0, 3, 700), (1, 3, 700), (2, 3, 700), (3, 3, 700)])
                assert [tuple(t["tap"]) for t in rec_d["taps"]] == [pc_pts[0]] * 3,                     "读得到进度时沿用同一个点（老逻辑不变），实际 %r" % (
                        [t.get("tap") for t in rec_d["taps"]],)
                assert rec_d["remaining_after"] == 0 and rec_d["closed_by"] == "progress-zero",                     rec_d
                assert len(d_d.taps) == 3, "读到 3/3 就该停，实际点了 %d 发" % len(d_d.taps)
                pc_note = ("a) 读不到进度: page_ready=%s/tries=%d/frames=%d · "
                           "b) 固定点前进: %r (why=progress-unreadable-advance) · "
                           "c) 读到 3/3: 0 发收手 · 读得到时仍 3 发收手"
                           % (rec_b["page_ready"], rec_b["ready_tries"], d_b.frames,
                              [t["tap"] for t in rec_b["taps"]]))
            finally:
                _pg.update(_save_pg)
            return ("点 (%d,%d) · 行=%s · %s/%s→%s/%s · 六件套齐 · verdict=%s"
                    " · 反例一(真没落地): %s/ok=%s/滑动 %d/BACK %d/指纹 %.3f/focus_changed=%s"
                    " · 反例二(没落地但行已推进): %s/ok=%s/滑动 %d"
                    " · 商品卡流程: %s"
                    % (tx, ty, str(target["name"])[:12], target["n"], target["m"],
                       want[0], want[1], rec["status"],
                       res_neg["status"], res_neg["ok"], dev_neg.swipes, dev_neg.backs,
                       px_neg.get("score") or 0, px_neg.get("focus_changed"),
                       res_gone["status"], res_gone["ok"], dev_gone.swipes, pc_note))
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    # T13 行内「+NN 芯片」也是可点动作（源 logs/r2_coinrow.py）
    def t13():
        real = merge_scan_rows([parse_rows(s["boxes"])
                                for s in detail("coinspeed_tail_002348.json")])
        chip_rows = [c for c in real if c["action"] == "chip"]
        assert chip_rows, "真实帧里应有「行尾 +NN 芯片」的行"
        for c in chip_rows:
            assert REWARD_CHIP_RE.match(c["button"]), "芯片形态不对：%r" % c["button"]
            assert c["btn_x"] > 1100, "芯片应在行最右端（x>1100），实际 %d" % c["btn_x"]
            guard_button_x(c["btn_x"], c["button"], c["btn_y"])
        # r2_coinrow 的几何判据：芯片与行名的 y 差 ≤ 90，且芯片本身可点
        # 行名换成不带放宽词的（原帧是「看商品视频得奖励」）：2026-11-03 起「视频」这类词
        # 只在「明写浏览N秒 + 纯浏览」时才放行，留给下面那条带秒数的芯片行单独验。
        rows = [{"y": 1460, "x": 120, "x2": 900, "t": "看商品好物得奖励(2/10)", "s": 0.99},
                {"y": 1500, "x": 1240, "x2": 1300, "t": "+50", "s": 0.99}]
        cards = parse_rows(rows)
        assert len(cards) == 1 and cards[0]["action"] == "chip", cards
        c0 = cards[0]
        assert 0 < c0["y"] - 1460 <= 90, "芯片与行名 y 差应 ≤90：%d" % (c0["y"] - 1460)
        assert (c0["n"], c0["m"]) == (2, 10), (c0["n"], c0["m"])
        assert c0["doable"] and c0["kind"] == "browse", (c0["doable"], c0["kind"], c0["why"])
        guard_button_x(c0["btn_x"], c0["button"], c0["btn_y"])
        # 同样形态、但行名带「视频」+ 明写浏览 5 秒：芯片照点（放宽通道把词放掉，并留审计标记）
        rows_r = [{"y": 1460, "x": 120, "x2": 900, "t": "看商品视频得奖励(2/10) 浏览5秒", "s": 0.99},
                  {"y": 1500, "x": 1240, "x2": 1300, "t": "+50", "s": 0.99}]
        cr = parse_rows(rows_r)[0]
        assert cr["action"] == "chip" and cr["doable"] and cr["kind"] == "browse", \
            (cr["action"], cr["doable"], cr["kind"], cr["why"])
        assert cr.get("relaxed_words") == ["视频"] and cr["secs"] == 5, \
            (cr.get("relaxed_words"), cr["secs"])
        return "真实帧 %d 枚芯片可点；合成行 chip=%s 行名差=%d → %s；视频+浏览5秒芯片行也放行" % (
            len(chip_rows), c0["button"], c0["y"] - 1460, c0["kind"])

    # T14 落地四分类 + 二次复核（源 logs/r2_expand.py + logs/r2_expand_coin.py）
    def t14():
        cases = [
            ("mCurrentFocus=Window{1 u0 com.eg.android.AlipayGphone/com.alipay.mobile.onsitepay.XRiverActivity}", "alipay"),
            ("mCurrentFocus=Window{2 u0 com.tmall.wireless/com.tmall.wireless.MainActivity}", "tmall"),
            ("mCurrentFocus=Window{3 u0 com.taobao.taobao/com.taobao.themis.container.app.TMSActivity}", "taobao"),
            ("mCurrentFocus=Window{4 u0 com.yxcorp.gifshow.webview.KwaiYodaWebViewActivity}", "other"),
        ]
        for focus, want in cases:
            got = classify_landing(focus)
            assert got == want, "%s 应判 %s，实际 %s" % (focus[-46:], want, got)

        class _D:
            def __init__(self, seq):
                self.seq = list(seq)

            def focus(self):
                return self.seq.pop(0) if self.seq else ""

        d = _D(["mCurrentFocus=com.tmall.wireless/MainActivity"])
        got = landing_after(d, "mCurrentFocus=com.taobao.taobao/TMSActivity", settle_s=0)
        assert got["first_mode"] == "taobao" and got["mode"] == "tmall", got
        assert got["rechecked"], "首次读成旧前台时必须标 rechecked"

        # C（2026-11-03）：跳转类回淘宝**先 am start**（= 点一下淘宝图标），落不到才退回 BACK。
        # 这里用假设备验「命令对不对 / 判定对不对」；真机表现看 run.log 的 [jump] 行。
        class _Am:
            def __init__(self, seq):
                self.seq, self.cmds = list(seq), []

            def shell(self, cmd, timeout=90):
                self.cmds.append(cmd)
                return ""

            def focus(self):
                return self.seq.pop(0) if self.seq else \
                    "mCurrentFocus=Window{9 u0 com.android.launcher3/com.android.launcher3.Launcher}"

        _amdoc = _Am(["mCurrentFocus=Window{1 u0 com.taobao.taobao/"
                      "com.taobao.themis.container.app.TMSActivity}"])
        am = am_start_taobao_and_wait(_amdoc, lambda *a: None, "t14")
        assert am["cmd"] == "am start -n com.taobao.taobao/com.taobao.tao.welcome.Welcome", am["cmd"]
        assert _amdoc.cmds == [am["cmd"]], _amdoc.cmds
        assert am["ok"] and am["focus"] == "TMSActivity", am
        assert am["focus_seq"] and am["am_start_ms"] >= 0, am
        _g = globals()
        _save_max = _g["AM_START_MAX_S"]
        _g["AM_START_MAX_S"] = 0.4        # 不让离线自测真等 3 秒
        try:
            am_bad = am_start_taobao_and_wait(_Am([]), lambda *a: None, "t14")
            # 落不到淘宝 -> jump_back_to_taobao 必须真的退回 BACK 阶梯（不许直接放弃）
            _jb = _Am([])
            _jb.backs = 0

            def _jb_back():
                _jb.backs += 1
            _jb.back = _jb_back
            jb = jump_back_to_taobao(_jb, lambda *a: None, "t14", stay_s=0, max_back=1)
            # S2（2026-11-03 用户拍板）：am start 后焦点**已在淘宝包内**（TaoLiveVideoActivity /
            # FluidVideoActivity / TBMainActivity 这类淘宝自家页面）-> 立刻停止等待（不再干等满
            # 3 秒），但**仍然走 BACK 阶梯**回面板，而且绝不再发第二次 am start。
            # 同一条焦点串给两次调用用（am_start_taobao_and_wait 1 次 + jump_back_to_taobao 1 次）
            _s2 = _Am(["mCurrentFocus=Window{1 u0 com.taobao.taobao/"
                       "com.taobao.taobao.taolive.TaoLiveVideoActivity}"] * 4)
            _s2.backs = 0

            def _s2_back():
                _s2.backs += 1
            _s2.back = _s2_back
            _s2_am = am_start_taobao_and_wait(_s2, lambda *a: None, "t14s2")
            _s2_jb = jump_back_to_taobao(_s2, lambda *a: None, "t14s2", stay_s=0, max_back=1)
        finally:
            _g["AM_START_MAX_S"] = _save_max
        assert not am_bad["ok"], "落到 launcher 就该判「没落到淘宝」，交给 BACK 兜底：%r" % (am_bad,)
        assert jb["mode"] == "jump-no-slide" and jb["am_start_ok"] is False, jb
        assert jb["back_fallback_used"] is True and jb["backs"] >= 1, jb
        assert "am_start_ms" in jb and "focus_seq" in jb and "start_basis" in jb, sorted(jb)
        # S2 断言（数据在上面同一个 try 里取的）：焦点已在淘宝包内 -> 只读了 1 次 focus 就停等，
        # 落地 activity 记进证据，随后仍走 BACK 阶梯，且全程只发 1 次 am start。
        assert _s2_am["am_early_stop"] is True, _s2_am
        assert _s2_am["landed_activity"] == "TaoLiveVideoActivity", _s2_am
        assert _s2_am["ok"] is False, "淘宝包下的直播页不是任务面板，绝不算成功：%r" % (_s2_am,)
        assert len(_s2_am["focus_seq"]) == 1, \
            "焦点已在淘宝包内就必须立刻停等（只读 1 次 focus），实际读了 %d 次：%r" % (
                len(_s2_am["focus_seq"]), _s2_am["focus_seq"])
        assert int(_s2_am["am_start_ms"]) < 200, \
            "请停必须立刻发生（不等满 AM_START_MAX_S=%.1fs），实际 %sms" % (
                AM_START_MAX_S, _s2_am["am_start_ms"])
        assert _s2_jb["am_early_stop"] is True and \
            _s2_jb["landed_activity"] == "TaoLiveVideoActivity", _s2_jb
        assert _s2_jb["backs"] >= 1 and _s2_jb["back_fallback_used"] is True, _s2_jb
        assert len(_s2.cmds) == 2, \
            "am start 只该发 2 次（自身 1 次 + jump 1 次），停等之后绝不再发：%r" % (_s2.cmds,)
        return ("alipay/tmall/taobao/other 四分类；首读 taobao→复核 tmall（rechecked）；"
                "am start 命中 TMSActivity=成功 / 落不到→退回 BACK 阶梯（back_fallback_used）；"
                "S2 淘宝包内其它 activity(TaoLiveVideoActivity)→停等 1 次 focus + 仍走 BACK(%d 次)"
                % _s2_jb["backs"])

    # T15 槽位判据：按位置跟踪，名字变了也不串号（源 v2/percept/racing_rows.py）
    def t15():
        def frame(items):
            out = []
            for y, n, m, label in items:
                out.append({"y": y, "x": 120, "x2": 900, "s": 0.99,
                            "t": "%s(%d/%d)" % (label, n, m)})
                out.append({"y": y + 40, "x": 1240, "x2": 1300, "t": "去完成", "s": 0.99})
            return out

        before = frame([(1100, 9, 10, "好物随心逛"), (1600, 8, 10, "搜一搜你心仪的宝贝"),
                        (2100, 3, 5, "去支付宝农场逛逛")])
        cards0 = parse_rows(before)
        assert len(cards0) == 3, len(cards0)
        assert match_tolerance(cards0) == 250, "行距 500 时阈值应为 250，实际 %s" % \
            match_tolerance(cards0)
        anchor = capture_slot(cards0, 0)
        assert anchor["count"] == [9, 10] and anchor["panel_rows"] == 3, anchor
        adv = slot_verdict(anchor, parse_rows(frame(
            [(1100, 10, 10, "好物随心逛"), (1600, 8, 10, "搜一搜你心仪的宝贝"),
             (2100, 3, 5, "去支付宝农场逛逛")])), text_after="今日速赚")
        assert adv["verdict"] == "advanced", adv
        # 做完消失：槽位被别的任务顶上（计数回退），名称整盘读不到 + 点前 9/10 → disappeared
        gone = slot_verdict(anchor, parse_rows(frame(
            [(1100, 0, 5, "搜一搜你心仪的宝贝"), (1600, 2, 10, "去逛逛领金币"),
             (2100, 3, 5, "去支付宝农场逛逛")])), text_after="今日速赚")
        assert gone["verdict"] == "disappeared", gone
        assert gone["evidence"].get("disappear_reasons"), gone["evidence"]
        # 白屏 / 整盘读不到：绝不许判成功
        blank = slot_verdict(anchor, [], text_after="")
        assert blank["verdict"] == "skipped" and "面板读不到" in blank["reason"], blank
        # 读数没推进：failed（不是成功）
        flat = slot_verdict(anchor, parse_rows(before), text_after="今日速赚")
        assert flat["verdict"] == "failed", flat
        return "阈值 250 · 推进→advanced · 同槽换任务→disappeared · 白屏→skipped · 没动→failed"

    # T16 视觉双证：整页指纹判「还是不是同一页」（源 v2/percept/vision.py）
    def t16():
        import tempfile
        import shutil as _sh
        from PIL import Image, ImageDraw
        tmp = tempfile.mkdtemp(prefix="autovis-")
        try:
            def panel(path, cover=False):
                im = Image.new("RGB", (1440, 3200), (30, 30, 30))
                d = ImageDraw.Draw(im)
                for i in range(12):
                    d.rectangle([100, 400 + i * 200, 800, 520 + i * 200], fill=(200, 180, 40))
                if cover:
                    d.rectangle([0, 1600, 1440, 3199], fill=(250, 250, 250))
                im.save(path)

            a, b, c = (os.path.join(tmp, x) for x in ("a.png", "b.png", "c.png"))
            panel(a)
            panel(b)
            panel(c, cover=True)
            same = page_similarity(a, b)
            assert same["ok"] and same["score"] >= 0.99 and not page_lost(same), same
            half = page_similarity(a, c)
            # 半屏浮层：整页分还有 0.58（>PAGE_LOST_SCORE），靠「坏格子占比」才判得出来
            assert not half["ok"], half
            assert half["score"] < PAGE_MIN_SCORE, half
            assert half["bad_ratio"] >= VISION_LOST_BAD and page_lost(half), half
            assert half["score"] >= PAGE_LOST_SCORE, \
                "这条用例的意义就是：分数没低到 PAGE_LOST_SCORE，仍必须判不是同一页"
            miss = page_similarity(os.path.join(tmp, "nope.png"), b)
            assert not miss["ok"] and miss["score"] is None and page_lost(miss), miss
            # 2026-11-03：全黑帧（息屏）必须有独立判据 —— 黑就是黑，正常帧不许误判，
            # 读不到帧也不许当黑（读不到 ≠ 黑屏）
            black = os.path.join(tmp, "black.png")
            Image.new("RGB", (1440, 3200), (0, 0, 0)).save(black)
            assert frame_black(black) is True, "全黑帧必须判成黑（息屏）"
            assert frame_black(a) is False, "正常面板帧不许判成黑"
            assert frame_black(os.path.join(tmp, "nope.png")) is False, "读不到帧 ≠ 黑屏"
            return ("同帧 %.3f(坏格 %.2f) / 浮层 %.3f(坏格 %.2f≥%.2f 才判不同页) / 读不到=不通过"
                    % (same["score"], same["bad_ratio"], half["score"],
                       half["bad_ratio"], VISION_LOST_BAD))
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    # T17 看门狗加强：通配包名 / 强词 / 成对词 / fail-closed（源 v2/core/guard.py）
    def t17():
        assert watchdog_hit("com.vivo.app.security", "", "主页面"), "com.vivo.*security 应被拦"
        assert watchdog_hit("com.taobao.taobao", "", "已了解应用的风险检测"), "强关键词应被拦"
        assert watchdog_hit("com.taobao.taobao", "", "是否允许访问相册"), "成对（允许+访问）应拦"
        assert not watchdog_hit("com.taobao.taobao", "", "允许浏览即可获得奖励"), \
            "单个「允许」不许拦，否则整轮会误停"
        assert not watchdog_hit("com.taobao.taobao", "TMSActivity", "今日速赚 去完成"), \
            "正常面板不该被拦"
        assert watchdog_hit("", "", "", rows=None), "读不到前台也读不到界面 → fail-closed"
        assert dialog_hit("appfilter", "AppJumpPrompt", ""), "原有 R5/R6 判据必须还在"
        return "glob/强词/成对词/fail-closed 全覆盖；单个「允许」不误停"

    # T18 停留模式 + 自适应判据（源 root_py/run_energy.py + 用户 2026-11-02 反馈）
    def t18():
        b = dwell_plan("浏览15秒可领+400", "好物随心逛(2/10)")
        assert b["mode"] == "slide" and b["secs"] == 15 + DWELL_MARGIN_BROWSE_S, b
        p = dwell_plan("玩30秒得奖励")
        assert p["mode"] == "static" and p["secs"] == 30 + DWELL_MARGIN_PLAY_S, p
        v = dwell_plan("点击去逛")
        assert v["mode"] == "static" and v["secs"] == DWELL_VISIT_S, v
        assert dwell_plan("浏览15秒", force_secs=8)["secs"] == 8
        s1 = dwell_signature([{"y": 300, "x": 100, "x2": 300, "t": "浏览中 15秒", "s": .9}])
        s2 = dwell_signature([{"y": 300, "x": 100, "x2": 300, "t": "浏览中 11秒", "s": .9}])
        assert _sig_changed(s1, s2), "倒计时在变必须能识别（这才是「时间在走」的证据）"
        assert not _sig_changed(s2, s2), "同一帧不该被判成在走"

        # B（2026-11-03 用户拍板）：停留阶梯表 + 档位记忆（失败升一档，到顶不再加）。
        assert DWELL_LADDER_JUMP_S == (0, 5, 10), DWELL_LADDER_JUMP_S
        assert DWELL_LADDER_VISIT_S == (5, 8, 8), DWELL_LADDER_VISIT_S
        assert dwell_ladder_secs("jump", 0) == 0 and dwell_ladder_secs("jump", 1) == 5
        assert dwell_ladder_secs("jump", 2) == 10
        assert dwell_ladder_secs("jump", 9) == 10, "到顶不再加（夹到第 2 档）"
        assert dwell_ladder_secs("visit", 0) == DWELL_VISIT_S
        assert dwell_ladder_secs("visit", 2) == 8
        assert dwell_ladder_secs("browse", 0, 15) == 15 + DWELL_MARGIN_BROWSE_S
        assert dwell_ladder_secs("browse", 1, 15) == 18
        assert dwell_ladder_secs("browse", 2, 15) == 21
        assert dwell_ladder_secs("play", 0, 30) == 30 + DWELL_MARGIN_PLAY_S
        assert dwell_ladder_secs("play", 2, 30) == 39
        assert dwell_plan("浏览15秒", level=1)["secs"] == 18
        assert dwell_plan("浏览15秒", level=9)["secs"] == 21, "档位越界要夹到顶档"
        assert dwell_plan("没写时长的行")["cat"] == "default" and \
            dwell_plan("没写时长的行")["secs"] == DWELL_DEFAULT_S, "兜底档不参与阶梯"
        # 学到的高档位要被同族下一轮直接读到（key 与秒数缓存同一套：kind|n/m|名称族）
        _c0 = {"kind": "browse", "n": 0, "m": 5, "name": "好物随心逛(0/5)", "sub": "浏览15秒"}
        dwell_level_put(_c0, 2, "browse")
        _lv, _src, _hit = dwell_level_get(_c0)
        assert (_lv, _src) == (2, "exact"), (_lv, _src)
        _c3 = {"kind": "browse", "n": 3, "m": 5, "name": "好物随心逛(3/5)", "sub": "浏览15秒"}
        _lv2, _src2, _ = dwell_level_get(_c3)
        assert (_lv2, _src2) == (2, "family"), (_lv2, _src2)
        _plan, _psrc, _det = resolve_dwell_plan(_c3)
        assert _psrc == "sub" and _plan["secs"] == 21 and _det["dwell_ladder_level"] == 2, \
            (_psrc, _plan, _det.get("dwell_ladder_level"))
        assert DWELL_LADDER_MAX_STEP == 2, "保险丝：同一行一轮内最多升 2 档"

        # S1（2026-11-03 用户拍板）：**做成一次就降一档**（最低回到 0 档），失败再升回去。
        # 原来只升不降，真机 state/secs_cache.json 里已有 3 个族停在 level=2 白停 21 秒。
        _c5 = {"kind": "browse", "n": 2, "m": 5, "name": "好物沉浸看(2/5)", "sub": "浏览15秒"}
        dwell_level_put(_c5, 2, "browse")
        _dec = dwell_level_decay(_c5, "browse", 2)
        assert _dec["dwell_ladder_decayed"] is True and (_dec["from"], _dec["to"]) == (2, 1), _dec
        assert dwell_level_get(_c5)[0] == 1, "降档必须落盘（精确 key + @family 一起）"
        _fam5 = {"kind": "browse", "n": 4, "m": 5, "name": "好物沉浸看(4/5)", "sub": "浏览15秒"}
        assert dwell_level_get(_fam5)[0] == 1, "同族下一轮必须读到降后的档位"
        _dec1 = dwell_level_decay(_fam5, "browse", 1)
        assert _dec1["dwell_ladder_decayed"] is True and _dec1["to"] == 0, _dec1
        _dec0 = dwell_level_decay(_fam5, "browse", 0)
        assert _dec0["dwell_ladder_decayed"] is False and _dec0["to"] == 0, \
            "第 0 档没得降（夹到 0，绝不为负）：%r" % (_dec0,)
        _nod = dwell_level_decay(_c5, "default", 2)
        assert _nod["dwell_ladder_decayed"] is False, "default/force/product-click 不进阶梯，也就不降档"
        # 非阶梯档位的调用不改任何已降下来的 level（_c5 自己的精确 key 还停在 1）
        assert dwell_level_get(_c5)[0] == 1, "非阶梯档位的调用不许改坏已降下来的 level"

        # ---- 2026-11-04 真机事故的两个根因：升档决策已抽成纯函数，这里逐条钉住 ----
        # 修 3：点名了外部 App 的行这一轮**没跳出淘宝**（landing 还在 taobao 类）+ 行还在 +
        # 计数没推进 -> 不升档（也不该设 retry_prefer：那是 one_round 里 applied=True 才做的事）。
        _ext_row = {"key": "去淘宝特价版领红包", "kind": "browse",
                    "name": "去淘宝特价版领红包(0/1)", "secs_raw": None,
                    "ext_visit": "淘宝特价版", "ext_pkg": "com.taobao.lite"}
        _stay = dwell_escalation_decision(_ext_row, "skipped", False, "taobao", True, 0,
                                          "visit", 0, 0, 1)
        assert _stay["applied"] is False and _stay.get("no_leave") is True, _stay
        assert _stay["to"] == 0 and _stay["from"] == 0, "不升档时档位一个字都不许动：%r" % (_stay,)
        # 同样情况、但 ext_pkg 为空（站内「点击去逛」类：本来就在淘宝里、靠停留计时）-> 仍然升档
        _inside_row = dict(_ext_row)
        _inside_row.pop("ext_pkg")
        _inside_row.pop("ext_visit")
        _inside_row["name"] = "来闪购逛逛得红包(0/1)"
        _go = dwell_escalation_decision(_inside_row, "skipped", False, "taobao", True, 0,
                                        "visit", 0, 0, 1)
        assert _go["applied"] is True and not _go.get("no_leave") and _go["to"] == 1, _go
        assert _go["next_secs"] == dwell_ladder_secs("visit", 1), _go
        # 这一行真的跳出去了（landing=other）-> 加时也许有用，照旧升档（修 3 不许误伤跳转类）
        _jump = dwell_escalation_decision(_ext_row, "skipped", False, "other", True, 0,
                                          "jump", 0, 0, 1)
        assert _jump["applied"] is True and _jump["to"] == 1, _jump
        # 修 2 的升档闸：到尝试上限时即使还能升档也不再升（applied=False）；上限 -1 次照旧升
        _cap1 = dwell_escalation_decision(_inside_row, "skipped", False, "other", True, 0,
                                          "visit", 0, 0, MAX_ROW_TRIES)
        assert _cap1["applied"] is False and _cap1.get("capped") is True, _cap1
        _cap2 = dwell_escalation_decision(_inside_row, "skipped", False, "other", True, 0,
                                          "visit", 1, 0, MAX_ROW_TRIES - 1)
        assert _cap2["applied"] is True and _cap2["to"] == 2, \
            "没到上限时还该升档（闸口在 >= MAX_ROW_TRIES）：%r" % (_cap2,)
        # 计数真的推进了（row_delta > 0）-> 一律不升档（那就不叫「没做完」）
        _adv = dwell_escalation_decision(_ext_row, "advanced", True, "taobao", True, 1,
                                         "visit", 0, 0, 1)
        assert _adv["applied"] is False and _adv["why"] == "done", _adv
        # 兜底档（default / --dwell 强制档）不参与阶梯：老行为一个字没动
        _no_ladder = dwell_escalation_decision(_inside_row, "skipped", False, "other", True, 0,
                                               "default", 0, 0, 1)
        assert _no_ladder["applied"] is False and "no-ladder" in _no_ladder["why"], _no_ladder
        return "浏览→slide/%ds · 玩30秒→static/%ds · 去逛→static/%ds · 倒计时变化可识别 · " \
               "阶梯 jump 0->5->8 / visit 5->8->8 / 浏览N+1,N+3,N+6 / 玩N+3,N+6,N+9（到顶不再加） · " \
               "档位记忆 2 档被同族(3/5)读到 · 做成一档降一档 2->1->0（0 档没得降） · " \
               "修3 没跳出淘宝(ext_pkg 非空)=不升档 / ext_pkg 空(站内点击去逛)=照升 / " \
               "landing=other 照升 · 修2 到上限 %d 次=不升档(capped)，上限-1 次照升" % (
                   b["secs"], p["secs"], v["secs"], MAX_ROW_TRIES)

    # T19 证据链不断：缺 verdict 的轮次要补一条诚实的 aborted（源 logs/r2_repair.py）
    def t19():
        import tempfile
        import shutil as _sh
        tmp = tempfile.mkdtemp(prefix="autorepair-")
        try:
            rd = os.path.join(tmp, "rounds")
            okd = os.path.join(rd, "round-001")
            os.makedirs(okd)
            with open(os.path.join(okd, "06_verdict.json"), "w", encoding="utf8") as f:
                f.write("{}")
            badd = os.path.join(rd, "round-002")
            os.makedirs(badd)
            with open(os.path.join(badd, "01_before.png"), "w", encoding="utf8") as f:
                f.write("x")
            fixed = repair_missing_verdicts(rd, log=lambda *a: None)
            assert fixed == [2], fixed
            rec = jload(os.path.join(badd, "06_verdict.json"))
            assert rec["ok"] is False and rec["status"] == "aborted", rec
            assert "01_before.png" in rec["reason"], rec["reason"]
            assert repair_missing_verdicts(rd, log=lambda *a: None) == [], "不许重复补写"
            return "round-002 补成 aborted(ok=False)，round-001 不动，重复调用不重复写"
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    # T20 弹窗探针豁免静态文字 + 黑名单不吃「支付宝」（logs/r2_corr212.py、run_energy.py）
    def t20():
        base_rows = [{"y": 1500, "x": 100, "x2": 400, "t": "开通会员享折扣", "s": .9}]
        base = words_in_band(base_rows, POPUP_PROBE_WORDS)
        assert "开通" in base, base                      # 点前就在 = 静态文字
        later = base_rows + [{"y": 1200, "x": 300, "x2": 900, "t": "确定", "s": .9}]
        stat = [w for w in POPUP_PROBE_WORDS
                if w in base and any(w in str(r["t"]) for r in later)]
        fresh = [w for w in POPUP_PROBE_WORDS
                 if w not in base and any(w in str(r["t"]) for r in later)]
        assert "开通" in stat and "确定" in fresh, (stat, fresh)
        assert words_in_band([{"y": 300, "x": 1, "x2": 9, "t": "开通", "s": .9}],
                             POPUP_PROBE_WORDS) == [], "带外文字不算弹窗词"
        assert black_hits("去支付宝农场逛逛哟 浏览15秒") == [], \
            "「支付」不许吃掉「支付宝」行"
        assert PAYMENT_WORD in black_hits("支付红包"), black_hits("支付红包")
        return "静态「开通」豁免、新出现「确定」算弹窗、带外不算；「支付宝」不被误杀"

    # T21 导航弹窗熔断：同一张卡连续关掉又回来 → 当认不出入口，不再烧预算
    def t21():
        card = {"card": {"y0": 300, "y1": 2600, "x0": 40, "x1": 1400},
                "close_texts": ["关闭"]}
        same = popup_signature(card, "com.taobao.taobao/TBMainActivity")
        assert same == popup_signature(card, "com.taobao.taobao/TBMainActivity")
        other = popup_signature({"card": {"y0": 300, "y1": 1200, "x0": 40, "x1": 1400},
                                 "close_texts": ["我知道了"]},
                                "com.taobao.taobao/TBMainActivity")
        assert same != other, "两张不同的卡指纹不该一样"
        hist, runs = [], []
        for _ in range(POPUP_BREAKER_LIMIT):
            hot, run = popup_breaker(hist, same)
            hist.append(same)
            runs.append(run)
        assert runs == [1, 2, 3] and hot, runs
        assert not popup_breaker([], same)[0], "第一次就该放行"
        hot2, run2 = popup_breaker([same, other, same], same)
        assert run2 == 2 and not hot2, "中间换过卡必须清零（run=%s hot=%s）" % (run2, hot2)
        return "同指纹连判 1→2→3 次熔断（第 %d 次停手）；换过卡就清零（run=%d）" % (
            POPUP_BREAKER_LIMIT, run2)

    # T22 跳转确认框：按用户在面板里的显式授权点「仅打开一次/始终打开」
    def t22():
        import tempfile
        import shutil as _sh
        tmp = tempfile.mkdtemp(prefix="autojump-")
        try:
            rows = [{"y": 1500, "x": 200, "x2": 700, "t": "仅打开一次", "s": .98},
                    {"y": 1500, "x": 820, "x2": 1300, "t": "始终打开", "s": .98},
                    {"y": 1200, "x": 200, "x2": 1300, "t": "即将跳转第三方应用", "s": .98}]
            once, always = jump_dialog_button(rows, "once")[0], jump_dialog_button(rows, "always")[0]
            assert once["t"] == "仅打开一次" and always["t"] == "始终打开", (once, always)
            assert once["x"] == 450 and always["x"] == 1060, (once, always)
            assert load_jump_policy(os.path.join(tmp, "nope.json"))["mode"] == "once", \
                "文件不存在必须默认 once（保守一侧）"
            p = os.path.join(tmp, "jump_policy.json")
            for raw, want in (({"policy": "always", "label": "始终打开"}, "always"),
                              ({"policy": "始终打开"}, "always"),
                              ({"mode": "once"}, "once"),
                              ({"policy": "whatever"}, "once")):
                with open(p, "w", encoding="utf8") as f:
                    json.dump(raw, f, ensure_ascii=False)
                got = load_jump_policy(p)["mode"]
                assert got == want, "%s 应读成 %s，实际 %s" % (raw, want, got)

            class _D:
                def __init__(self):
                    self.taps = []

                def tap(self, x, y):
                    self.taps.append((int(x), int(y)))

            d = _D()
            with open(p, "w", encoding="utf8") as f:
                json.dump({"policy": "always"}, f)
            got = handle_jump_dialog(d, rows, log=lambda *a: None, path=p)
            assert got["ok"] and got["mode"] == "always", got
            assert d.taps == [(1060, 1500)], d.taps
            # 不是跳转框：绝不动手
            quiet = handle_jump_dialog(None, [{"y": 300, "x": 10, "x2": 90, "t": "今日速赚", "s": .9}],
                                       log=lambda *a: None, path=p)
            assert quiet["seen"] is False and quiet["ok"] is False, quiet
            return "once→仅打开一次 x=450 / always→始终打开 x=1060（真点）；缺文件→once；非跳转框不动手"
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    # T23 浮层关闭白名单：批准的文案可点，禁点文案绝不当关闭键（用户 2026-11-02 批准）
    def t23():
        rows = [{"y": 1800, "x": 300, "x2": 560, "t": "确认选择", "s": .98},
                {"y": 1950, "x": 700, "x2": 900, "t": "取消", "s": .98},
                {"y": 2100, "x": 200, "x2": 700, "t": "取消订单并退款", "s": .98},
                {"y": 2200, "x": 800, "x2": 1050, "t": "立即领取", "s": .98},
                {"y": 2300, "x": 200, "x2": 420, "t": "抽奖", "s": .98}]
        btn, hits = overlay_close_button(rows)
        assert btn and btn["t"] == "取消", btn              # 最靠下那枚才是主按钮
        assert [h["t"] for h in hits] == ["取消", "确认选择"], hits
        assert not any(h["t"] == "取消订单并退款" for h in hits), "长文案不许当关闭键"
        assert not any(h["t"] in ("立即领取", "抽奖") for h in hits), "禁点文案绝不当关闭键"
        cands = [c["t"] for c in close_candidates(rows)]
        assert "确认选择" in cands, cands                   # 解锁新车轮那类弹窗要能关
        assert "取消订单并退款" not in cands, cands
        for w in ("立即领取", "领取", "去支付", "开通", "授权", "同意", "签约",
                  "抽奖", "去使用", "去玩", "抢"):
            assert w in POPUP_FORBIDDEN, "用户明示的禁点词漏了：%s" % w
        for w in ("确认选择", "我知道了", "取消", "以后再说", "暂不", "关闭", "×"):
            assert w in OVERLAY_CLOSE_WORDS, "批准清单漏了：%s" % w
        assert "确认选择" in OVERLAY_BLOCK_WORDS
        return "确认选择/取消可点（取最下面那枚）；取消订单并退款、抽奖、立即领取绝不点；11 个禁点词全在册"

    tests = [("T1 赛车面板解析 + R1 守卫", t1), ("T2 可做行判定", t2), ("T3 跳过行原因", t3),
             ("T4 淘金币长表分类", t4), ("T5 R1 守卫边界", t5), ("T6 R2 面板判定", t6),
             ("T7 进度/金币解析", t7), ("T8 R4 热词 chip", t8), ("T9 全表合并", t9),
             ("T10 证据六件套", t10), ("T11 源码卫生", t11),
             ("T12 端到端一轮（假设备）", t12),
             ("T13 行内 +NN 芯片可点", t13), ("T14 落地四分类 + 复核", t14),
             ("T15 槽位判据", t15), ("T16 视觉双证", t16), ("T17 看门狗加强", t17),
             ("T18 停留模式 + 自适应判据", t18), ("T19 证据链补写", t19),
             ("T20 弹窗豁免 + 黑名单精确", t20),
             ("T21 导航弹窗熔断", t21), ("T22 跳转确认框授权", t22),
             ("T23 浮层关闭白名单", t23)]
    try:
        print("=== coin11/auto.py 离线自测（无需连手机）===")
        for name, fn in tests:
            try:
                checks.append((name, True, fn()))
            except Exception as e:
                checks.append((name, False, "%s: %s" % (type(e).__name__, e)))
        fails = 0
        for name, ok, msg in checks:
            print("%s %s\n    %s" % ("[PASS]" if ok else "[FAIL]", name, msg))
            if not ok:
                fails += 1
        print("\n结果：%d/%d 通过%s" % (len(checks) - fails, len(checks),
                                       "" if not fails else "，失败 %d 项" % fails))
        return 0 if not fails else 1
    finally:
        # 真实路径原样换回，自测期间产生的临时文件全删（绝不把 offline-fake 留在真实 state/）
        STATE_DIR, PENDING_DIR, LIVE_JSON, PAUSE_FLAG, HINT_JSON = _real_paths
        _sh.rmtree(_owner, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 前置检查 / 只扫描模式
# --------------------------------------------------------------------------- #
def preflight(dev, ctx):
    log = ctx.log
    t = dev.device_time()
    if not t:
        raise StopRun("读不到设备时间（serial=%s 不存在 / 离线 / 未授权）" % dev.serial)
    log("[pre] serial=%s 设备时间=%s" % (dev.serial, t))
    log("[pre] 型号=%s 分辨率=%dx%d"
        % (dev.shell("getprop ro.product.model").strip(), dev.w, dev.h))
    pkg, act = dev.app_current()
    log("[pre] 前台=%s/%s" % (pkg, act))
    dh = dialog_hit(pkg, act, "")
    if dh:
        raise StopRun("启动时就在系统弹窗上：%s → 交给人处理" % dh)
    if not dev.wake():
        raise StopRun("唤醒失败：input keyevent 26 三次后仍非 Awake（R9）")
    log("[pre] 屏幕已唤醒")
    if dev.keyguard_showing():
        raise StopRun("手机停在锁屏上（需要密码/图案解锁）：请先人工解锁再跑。"
                      "按亮没用——脚本没有密码，也绝不猜密码。")
    # 2026-11-02 真机事故：手机2 跑到一半息屏（dumpsys power 里 mLastSleepReason=timeout），
    # 之后每一帧全黑，整轮报废还被判成「没落地」。插着电时用系统级「保持亮屏」压住；
    # 跑完在 main 的 finally 里还原。没插电时这条不起作用（系统会忽略）。
    try:
        if "AC powered: true" in (dev.shell("dumpsys battery") or ""):
            dev.shell("svc power stayon true")
            ctx.keep_stayon = True
            log("[pre] 已开「插电保持亮屏」svc power stayon true（跑完还原）")
    except Exception as _e:  # noqa: BLE001
        log("[pre] 开保持亮屏失败（不影响继续跑）：%s" % _e)


def scan_only(dev, ctx, what, cold=True):
    rows, how, info = nav_until_panel(dev, what, ctx.log, ctx.scans_dir, cold=cold)
    if rows is None:
        ctx.log("[scan] %s 导航失败，跳过" % what)
        return
    rows, exp = expand_panel(dev, rows, ctx.log, ctx.scans_dir)
    doc, path = full_scan(dev, what, ctx.log, ctx.scans_dir)
    ctx.scans["before"][what] = doc
    ctx.scans["after"][what] = doc
    ctx.log("[scan] %s 就绪=%s / %s → 可做 %d 行，共 %d 行"
            % (what, how, exp, len(doc["doable"]), len(doc["rows"])))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
EXAMPLES = """
一条命令跑完全程（无交互，参数清晰）：
    python coin11/auto.py --serial 192.168.1.8:5555 --what all --minutes 60

只要某个面板（手机2 用 192.168.1.5:43059）：
    python coin11/auto.py --serial 192.168.1.8:5555 --what race --minutes 30
只扫描不出手（先看看表里有什么、哪些行会被跳过、为什么）：
    python coin11/auto.py --serial 192.168.1.8:5555 --what scan --minutes 10
只跑自定义脚本队列（coin11/scripts/<id>.json，按 order 排；enabled 且今天没成功过的才跑）：
    python coin11/auto.py --serial 192.168.1.8:5555 --what scripts --minutes 20
一键运行单个脚本（dash 的【一键运行】：不受 enabled / 今天已成功 限制）：
    python coin11/auto.py --serial 192.168.1.8:5555 --script jd_signin --minutes 10
离线自测（不连手机，用已存 evidence 复算解析/分类/守卫）：
    python coin11/auto.py --selftest
"""


def build_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="coin11/auto.py",
        description="淘宝 淘金币 / 赛车能量 任务面板：一条命令、无人值守跑完全程（运行期无模型参与）",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    # 默认与 coin11/.phone_serial、run.bat 保持一致（都是手机1）；手机2 = 192.168.1.5:43059
    ap.add_argument("--serial", default="192.168.1.8:5555", help="设备 serial（默认手机1）")
    # 2026-11-02 用户要求（第 7 条 / 7.3-1）：--what 支持逗号组合，all/coin/race/scan 语义不变。
    # 不能再挂 choices=（逗号组合过不了），非法值改由 parse_what() 用 SystemExit 拒绝。
    ap.add_argument("--what", default="all",
                    help="跑哪些面板：all=淘金币（赛车已下线，默认不带）/ coin / race / scan=只扫描不点击 / "
                         "scripts=只跑自定义脚本队列；可逗号组合（如 coin,race）")
    # 2026-11-02 用户要求（第 7 条 / 7.3-2/3/4）：面板可选 + 已完成记忆 + 自定义脚本
    ap.add_argument("--force-panels", action="store_true",
                    help="忽略 state/panel_done.json 的「今天已完成」，强制重跑这些面板")
    ap.add_argument("--no-scripts", action="store_true",
                    help="面板跑完不接着跑自定义脚本队列（默认跑：enabled 且今天没成功过的）")
    ap.add_argument("--script", default="",
                    help="只跑这一个脚本然后退出（dash 的【一键运行】；不受 enabled / 今天已跑过 限制）")
    ap.add_argument("--script-timeout", type=float, default=180.0,
                    help="单脚本超时秒数（默认 180；超时按 failed 收尾并立刻停）")
    # 2026-11-03 用户要求：dash 传 --minutes 0 = 不限时间（只在面板到底 / --max-rounds /
    # 脚本队列跑完时停）。默认值仍是 60.0，一个字没改。
    ap.add_argument("--minutes", type=float, default=60.0,
                    help="总预算（分钟），到点自动收尾；0（或负数）= 不设时间上限")
    ap.add_argument("--max-rounds", type=int, default=120,
                    help="最多跑多少轮（保险丝；2026-11-03 用户确认从 200 降到 120 —— "
                         "--minutes 0「不限时间」时它是唯一保险丝）")
    ap.add_argument("--dwell", type=int, default=0,
                    help="强制停留秒数（0=按行上的「浏览N秒 / 玩N秒 / 去逛」自己算）")
    ap.add_argument("--dwell-margin", type=int, default=None,
                    help="浏览类停留余量秒数（默认 %d；2026-11-02 用户要求从 4 砍到 1）"
                         % DWELL_MARGIN_BROWSE_S)
    ap.add_argument("--adaptive-dwell", action="store_true",
                    help="回到自适应停留（先看计时走不走再决定滑不滑）；默认是全程滑动")
    # 2026-11-02 用户要求（第 6 条）之后它的语义只剩一半：快速路径**已经**默认一律连滑
    # （只有 --adaptive-dwell 才自适应），这个开关现在只影响完整流程（搜一搜 / give_up≥2）。
    ap.add_argument("--force-swipe", action="store_true",
                    help="只对完整流程（搜一搜 / give_up≥2）生效：关掉自适应、一律连滑满；"
                         "快速路径不受它影响（默认就连滑）")
    ap.add_argument("--out", default="", help="证据输出目录（默认 evidence/<日期>-auto，不抢别人的目录）")
    ap.add_argument("--targets", default="",
                    help="规则库路径（默认 coin11/targets/targets.jsonl，和 teach.py 同一份）")
    ap.add_argument("--selftest", action="store_true", help="离线自测：不连手机")
    # 2026-11-03 用户需求（App 自检）：只读 pm list packages + 落盘 + 打印表格，
    # **不跑任务、不点屏幕、不滑动、不 am start**。不带 --serial = state/devices.json 里每台各查一份。
    ap.add_argument("--appcheck", action="store_true",
                    help="App 自检：只读 pm list packages，打印表格并写 state/apps.json；"
                         "不跑任务、不点屏幕、不滑动。不带 --serial 就查 devices.json 里每一台")
    ap.add_argument("--quiet", action="store_true", help="少打印")
    args = ap.parse_args(argv)
    # --appcheck 要分清「没传 --serial」和「传了默认值」：argparse 的 default 分不出来，
    # 所以直接看命令行原文。--serial 的默认值一个字都没改（正常跑任务的行为完全不变）。
    args.serial_given = any(str(a) == "--serial" or str(a).startswith("--serial=")
                            for a in (sys.argv[1:] if argv is None else argv))
    return args


#: --what 的合法 token（7.3-1）。scan 是独立模式，不能和别的组合。
WHAT_TOKENS = ("all", "coin", "race", "scan", "scripts")


def _panels_now(names):
    """按 RACE_ENABLED 过滤默认面板表：赛车下线时，all / scan 都不再带 race。

    显式写 --what race 不受影响（那是用户点名要跑，main() 里会打一行警告）。
    """
    return [p for p in names if p != "race" or RACE_ENABLED]


def parse_what(value):
    """--what 解析（7.3-1：支持逗号组合；all/coin/race/scan 的行为一个字都不变）。

    all        -> 面板 ["coin","race"]（旧行为，原样）
    coin/race  -> 单面板（旧行为，原样）
    coin,race  -> 两个面板（新增）
    scripts    -> 只跑脚本队列，不进任何面板（新增）
    scan       -> 只扫描不点击（旧行为；**不读也不写 panel_done.json**，也不跑脚本队列）

    返回 (panels, want_scripts, is_scan)。
    """
    raw = str(value or "").strip().lower() or "all"
    toks = [t.strip() for t in raw.split(",") if t.strip()]
    bad = [t for t in toks if t not in WHAT_TOKENS]
    if bad:
        raise SystemExit("--what 不支持：%s（可选 %s，可用逗号组合）"
                         % (",".join(bad), " / ".join(WHAT_TOKENS)))
    if "scan" in toks:
        if len(set(toks)) > 1:
            raise SystemExit("--what scan 不能和别的组合（scan 是只扫描不点击的独立模式）")
        return _panels_now(["coin", "race"]), False, True
    panels, want_scripts = [], "scripts" in toks
    for t in toks:
        names = _panels_now(["coin", "race"]) if t == "all" else (() if t == "scripts" else (t,))
        for p in names:
            if p not in panels:
                panels.append(p)
    return panels, want_scripts, False


HOME_ANCHORS = ("我的淘宝", "领淘金币", "搜索", "购物车")


def ensure_taobao_home(dev, log=print, tries=2):
    """确保淘宝停在主页面（有底部 tab）。若恢复到 H5/活动页，直接 force-stop 重开。
    用户 2026-11-02："卡了直接关闭重新进" —— 这比在活动页上按返回键有效得多。

    注意（2026-11-02 用户要求 第 1 条）：**已从启动必经路径上摘掉** —— 打开淘宝后不做
    主页面判断，am start 后直接 nav_until_panel() 进面板。这个函数保留给别处/别的脚本用。
    """
    for k in range(tries):
        log("[launch] am start 第 %d/%d 次%s"
            % (k + 1, tries, "（先 force-stop）" if k > 0 else ""))
        try:
            if k > 0:
                dev.shell("am force-stop %s" % TAOBAO_PKG)
                time.sleep(1.5)
            dev.am_start_taobao()
        except Exception as e:
            log("[launch] am start 失败：%s" % e)
            continue
        deadline = time.time() + 14
        while time.time() < deadline:
            time.sleep(1.0)
            # 同样走 frame_path：证据目录 + pid，别抢同一个临时文件
            p = frame_path("_home-%s" % str(dev.serial).replace(":", "-"), "jpg")
            try:
                dev.shot(p)
                rows = dev.ocr(p)
            except Exception:
                continue
            if any(a in text_of(rows) for a in HOME_ANCHORS):
                log("[launch] 主页面就绪")
                return True
        log("[launch] 第 %d 次没到主页面" % (k + 1))
    return False


def main(argv=None):
    args = build_args(argv)
    if args.selftest:
        return run_selftest()
    # 2026-11-03 用户需求：--appcheck 是**只读自检模式**，先于 --what 解析分派
    # （本模式不跑任务、不点屏幕、不滑动，也绝不进 any 面板/脚本阶段）。
    if getattr(args, "appcheck", False):
        return run_appcheck(args)
    # 2026-11-02 用户要求（第 7 条 / 7.3-1）：--what 支持逗号组合（all/coin/race/scan 语义不变）。
    # 放在最前面解析：非法值立刻退出，不建证据目录、不碰设备。
    what_list, want_scripts, is_scan = parse_what(args.what)
    # 2026-11-02 用户要求（第 6 条余量）：--dwell-margin 覆盖浏览类停留余量
    if getattr(args, "dwell_margin", None) is not None:
        log_margin = set_dwell_margin(args.dwell_margin)
        if not args.quiet:
            print("[dwell] 浏览类停留余量 = %ds（--dwell-margin 覆盖）" % log_margin, flush=True)

    global TARGETS_PATH, PENDING_DIR, CUR_SERIAL
    CUR_SERIAL = str(args.serial or "").strip()
    if args.targets:
        TARGETS_PATH = os.path.abspath(args.targets)
        PENDING_DIR = os.path.join(os.path.dirname(TARGETS_PATH), "pending")

    root = _root()
    # 2026-11-09 通用版（用户原话）：「如果是给我同学用的话就不需要这些记录了呀，把任务做完就行了」。
    # COIN11_NO_EVIDENCE=1 -> 证据全部写进临时目录，进程退出自动删掉（截图照拍照 OCR，只是不留档）。
    # 用环境变量而不是新参数：面板起子进程时直接塞环境变量，命令行一个字都不用改。
    # 2026-11-09 用户定案：「还是做成和我一样有轮次的」—— 分发版也照常留证据（每轮六件套 + 截图），
    # 靠面板的【记录管理】自己删旧轮次，不再走「同学版不留档」那条路。
    _no_evidence = os.environ.get("COIN11_NO_EVIDENCE") == "1" and not args.out
    if _no_evidence:
        import atexit
        import shutil
        import tempfile
        outdir = tempfile.mkdtemp(prefix="coin11-noev-")
        atexit.register(shutil.rmtree, outdir, True)
    else:
        outdir = args.out or os.path.join(root, "evidence", "%s-auto" % datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)
    # 临时帧落在证据目录下：%TEMP% 在「面板起的子进程」里写不进去（真机复现 WinError 32/5）
    global FRAME_DIR
    try:
        FRAME_DIR = os.path.join(outdir, "_tmp")
        os.makedirs(FRAME_DIR, exist_ok=True)
    except OSError:
        FRAME_DIR = ""

    def log(msg):
        line = "%s %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        if not args.quiet:
            print(line, flush=True)
        try:
            with open(os.path.join(outdir, "run.log"), "a", encoding="utf8") as f:
                f.write(line + chr(10))
        except Exception:
            pass

    log("=== coin11/auto.py 启动 ===")
    log("serial=%s what=%s minutes=%s%s out=%s"
        % (args.serial, args.what, args.minutes,
           "（不设时间上限）" if float(args.minutes or 0) <= 0 else "", outdir))
    log("[rules] 规则库=%s（%d 条） 待教学目录=%s"
        % (TARGETS_PATH, len(load_rules()), PENDING_DIR))
    try:
        dev = Device(args.serial, log)
    except Exception as e:
        log("[fatal] 连不上设备 %s：%s" % (args.serial, e))
        return 3
    ctx = Ctx(outdir, dev, args)
    ctx.log = log
    live_write("start", "auto.py 已启动 what=%s minutes=%s" % (args.what, args.minutes), args.serial)
    # 源 logs/r2_repair.py：上次被 kill 掉的轮次没有 verdict → 先补一条诚实的 aborted，
    # 否则报告里会出现「有证据、没判定」的空轮（证据链断掉）。
    try:
        repair_missing_verdicts(ctx.rounds_dir, log)
    except Exception:
        log("[repair] 补写失败：%s" % traceback.format_exc()[-400:])

    only_script = str(getattr(args, "script", "") or "").strip()
    ctx.log("[what] 面板=%s / 脚本队列=%s / 强制重跑=%s / 单跑脚本=%s"
            % ("/".join(what_list) or "（无）",
               "关掉（--no-scripts）" if args.no_scripts
               else ("不适用（scan）" if is_scan else "默认跑"),
               "是（--force-panels）" if args.force_panels else "否",
               only_script or "（无）"))
    if "race" in what_list and not RACE_ENABLED:
        ctx.log("[race] 赛车能量代码是**留档**状态（RACE_ENABLED=False）：这次是你显式要跑，照跑。"
                "提醒：现在点「去赚能量」进的是淘宝闪购，不是任务面板。")
    elif not RACE_ENABLED and not only_script:
        ctx.log("[race] 赛车能量活动已下线 -> 本趟只跑淘金币；"
                "赛车代码留档在 auto.py（RACE_ENABLED，复出时改 True 即可）")
    rc = 0
    try:
        preflight(dev, ctx)
        # 2026-11-02 用户要求（第 1 条）：打开淘宝后不做「主页面判断」，直接进面板。
        # 2026-11-02 19:17 起 nav_until_panel() 改成**写死序列**；19:5x 用户再追加最高优先的一条：
        # 「不管怎么样，第一步都直接重启淘宝」—— 起点确定，写死坐标才有意义。
        #   冷启动（force-stop + am start）-> 轮询 focus 等首页 -> tap(159,938)
        #   -> 等 focus 变 TMSActivity -> tap(去赚金币/去赚能量) -> 1 次 OCR 判面板。
        # 这里**不再自己 am start**（重启统一由 nav_until_panel 负责，免得启动两次）。
        # force-stop 只在「真的要进面板」之前做一次：被「今天已完成」跳过的面板不重启，
        # 多面板连跑只有第一个面板做冷启动（第二个直接从淘金币页点胶囊）。
        if only_script:
            # 7.3-4【一键运行】：绝不 am start 别人的 App（用户选 3A），直接在当前屏幕上回放
            ctx.log("[script] --script %s：只跑这一个脚本，不进任何面板、不 am start" % only_script)
        elif what_list:
            ctx.log("[launch] 不在启动阶段 am start（用户 2026-11-02）：进面板时由写死导航统一冷启动")
            cold_done = False
            prev_drove = False      # 上一个面板是不是**真的开做过**（R16：没开做就别多按 BACK）
            for what in what_list:
                # 7.3-2/6：今天该面板已走到「到底」-> 跳过（--force-panels 覆盖）。
                # 先判跳过再决定要不要冷启动：跳过的面板不白重启。
                done_day = (None if is_scan else
                            panel_done_done_today(dev, what, force=args.force_panels,
                                                  log=ctx.log))
                if done_day:
                    ctx.log("[panel] %s 今天（%s）已完成，跳过（用【强制重跑】/--force-panels 可覆盖）"
                            % (WHAT_NAME.get(what, what), done_day))
                    continue
                cold = not cold_done
                cold_done = True
                if is_scan:
                    scan_only(dev, ctx, what, cold=cold)   # scan：不读也不写 panel_done.json
                    continue
                # R16：上一个面板真的开做过（跑过轮次）-> 现在还停在它的**任务列表**里
                # （activity 同为 TMSActivity），先 BACK 一次退回主页面再点下一枚胶囊。
                n_before = ctx.n_round
                leave = bool(prev_drove and not cold)
                if leave:
                    ctx.log("[panel] 换面板（%s -> %s）：上一个面板跑过 %d 轮，先退出它的任务列表"
                            % ("/".join(what_list[:what_list.index(what)]) or "?",
                               what, n_before))
                drive_panel(dev, ctx, what, cold=cold, leave_prev=leave)
                prev_drove = bool(ctx.n_round > n_before)
        else:
            # --what scripts：没有面板要跑 -> 不 am start 别人的 App（3A），直接回放队列
            ctx.log("[script] --what %s：没有面板要跑 -> 不 am start，直接回放脚本队列" % args.what)
        # 7.3-3/4：脚本阶段（单跑 / 队列）。--no-scripts 只关「队列」，不挡 --script 手动单跑。
        # --what scan 是「只扫描不点击」的诊断模式：既不读/写 panel_done.json，也不跑脚本
        # （脚本是会动手的），保持旧行为一个字不变。
        if only_script and args.no_scripts:
            # dash 不会这么拼；真收到就按 --script 优先（手动单跑是用户的显式动作）
            ctx.log("[script] --no-scripts 与 --script 同时出现 -> 以 --script（手动单跑）为先，"
                    "--no-scripts 只关「队列」，不挡这一趟")
        if is_scan and not only_script:
            ctx.log("[script] --what scan：只扫描不点击，不跑脚本队列")
        elif args.no_scripts and not only_script:
            ctx.log("[script] --no-scripts：不跑脚本队列")
        else:
            try:
                run_script_stage(dev, ctx, args)
            except Exception:
                ctx.log("[script] 脚本阶段出错（面板结果不受影响）：%s"
                        % traceback.format_exc()[-600:])
    except NeedTeaching as e:
        # 已经是「不会就问人」：pending 已落盘，退出码 3（不是崩溃）
        rc = EXIT_NEED_TEACH
        ctx.stop_reason = "需要教学（退出码 3）：%s" % e
        ctx.safety_stop = True
        log("[STOP] 需要教学 -> 已停手，等你教：%s" % e)
    except GuardError as e:
        ctx.stop_reason = ctx.stop_reason or ("R1 按钮 x 越界，已停止：%s" % e)
        log("[STOP] %s" % e)
    except StopRun as e:
        ctx.stop_reason = ctx.stop_reason or str(e)
        ctx.safety_stop = True
        log("[STOP] %s" % e)
    except KeyboardInterrupt:
        ctx.stop_reason = "人工 Ctrl-C 中断"
        log("[STOP] 人工中断")
    except Exception:
        ctx.stop_reason = ctx.stop_reason or "未捕获异常（traceback 见 run.log）"
        log("[fatal] 未捕获异常：%s" % traceback.format_exc())
        # 循环里抛异常也要落盘：截图 + pending json，照样进「待教学」
        try:
            write_pending(dev, "自动脚本在这一步抛了异常，当前界面应该怎么点？",
                          {"stage": "exception", "why": "uncaught-exception",
                           "what_list": what_list,
                           "traceback": traceback.format_exc()[-1500:]},
                          label_hint="异常", kind="exception", log=log)
            rc = EXIT_NEED_TEACH
        except Exception:
            log("[teach] 异常落盘也失败了：%s" % traceback.format_exc()[-400:])
    finally:
        # 还原 preflight 开的「插电保持亮屏」（只在自己开过的前提下还原，别动用户原有设置）
        try:
            if getattr(ctx, "keep_stayon", False):
                dev.shell("svc power stayon false")
                ctx.keep_stayon = False
                log("[done] 已还原：svc power stayon false")
        except Exception:
            pass
        try:
            write_report(ctx, dev, what_list, args)
        except Exception:
            log("[fatal] 报告写失败：%s" % traceback.format_exc())
        live_write("done", "收尾：%s" % (ctx.stop_reason or "可做行已做完"), args.serial)

    ok_n = sum(1 for x in ctx.rounds if x.get("ok"))
    print("")
    print("=== 收尾 ===")
    print("轮数 %d / 成功 %d / 耗时 %s / 停止原因：%s"
          % (len(ctx.rounds), ok_n, _fmt_ts(ctx.elapsed()), ctx.stop_reason or "可做行已做完"))
    print("证据目录：%s" % outdir)
    print("报告：%s" % os.path.join(outdir, "REPORT.md"))
    if rc:
        print("退出码 %d = 需要教学：截图和问题已写进 %s" % (rc, pending_dir()))
        return rc
    return 2 if ctx.safety_stop else 0


# --------------------------------------------------------------------------- #
# 总控台（coin11/dash.py）配合：实时状态 / 暂停继续 / 跳转策略 / 用户指定任务
# 全部是新增能力，不改上面任何判定逻辑；dash 不在时这些近乎空转（一次文件探测）。
# --------------------------------------------------------------------------- #
STATE_DIR = os.path.join(_root(), "coin11", "state")
LIVE_JSON = os.path.join(STATE_DIR, "live.json")
PAUSE_FLAG = os.path.join(STATE_DIR, "pause.flag")

# 2026-11-02：两台手机要能**同时各跑一轮**（用户已确认）。单文件会互相覆盖/互相打断，所以：
#   - 暂停：state/pause-<设备>.flag 只停这一台；老的 pause.flag 变成「全部暂停」；
#   - 实时状态：state/live-<设备>.json 每台一份；live.json 继续写（老面板兼容）。
def _dev_san(serial):
    """serial -> 文件名安全的设备键（192.168.1.8:5555 -> 192-168-1-8-5555）。"""
    return re.sub(r"[^0-9A-Za-z_-]", "-", str(serial or "dev"))


def dev_state_path(prefix, serial, ext=".json"):
    return os.path.join(STATE_DIR, "%s-%s%s" % (prefix, _dev_san(serial), ext))


class _FileLock:
    """跨进程互斥（2026-11-03：两台同时跑会同时改 panel_done.json / secs_cache.json）。

    实现：`state/<名字>.lock` 用 O_EXCL 独占创建；拿不到就小睡重试，超时**照常继续**
    （宁可偶发丢一次更新，也绝不因为锁住把整轮卡死）。超过 20 秒的锁视为陈旧，直接删掉。
    """

    def __init__(self, name, timeout=5.0, stale=20.0):
        self.path = os.path.join(STATE_DIR, "%s.lock" % name)
        self.timeout = float(timeout)
        self.stale = float(stale)
        self.fd = None

    def __enter__(self):
        _state_dir()
        t0 = time.time()
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(self.fd, str(os.getpid()).encode())
                except OSError:
                    pass
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)          # 陈旧锁（进程被 kill 掉的残留）
                        continue
                except OSError:
                    pass
                if time.time() - t0 > self.timeout:
                    return self                        # 等太久就放弃加锁，照常干活
                time.sleep(0.05)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


#: 当前进程的设备（main 里按 --serial 设定；未设定时按 None 处理 = 只有全局语义）
CUR_SERIAL = ""
HINT_JSON = os.path.join(STATE_DIR, "task_hint.json")
JUMP_POLICY_JSON = os.path.join(_root(), "coin11", "targets", "jump_policy.json")

# --------------------------------------------------------------------------- #
# 面板「今天已完成」记忆（2026-11-02 用户要求 第 7 条 / 7.3-2/6）
# 契约 B（v2，2026-11-03 用户真机踩到）：**记录必须按设备分开**
#   v2: state/panel_done.json =
#       {"v":2,"panels":{"192.168.1.8:5555":{"coin":"2026-11-02"},
#                        "192.168.1.5:43059":{"race":"2026-11-02"}}}
#   v1（旧）: {"v":1,"panels":{"coin":"2026-11-02","race":"2026-11-02"}} —— **没有设备维度**，
#   两台手机共用一份：手机1 记了「今天已完成」，手机2 启动就被误跳过（用户 2026-11-03 只能
#   靠 --force-panels 才跑起来）。所以改成按设备分开记；读到 v1 一律当「未知设备的历史记录」
#   忽略掉（宁可重扫一遍，也不能误跳过）。
# 它专治用户原话那个坑：「淘金币任务做完了，重启又从头扫一遍，有点浪费时间」。
# --------------------------------------------------------------------------- #
PANEL_DONE_FILE = "panel_done.json"
PANEL_DONE_V = 2


def _panel_done_path():
    return os.path.join(STATE_DIR, PANEL_DONE_FILE)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def device_key(dev):
    """本设备在 state/panel_done.json 里的键：优先 serial（两台手机各不相同），其次分辨率。"""
    serial = str(getattr(dev, "serial", "") or "").strip()
    if serial:
        return serial
    return device_resolution(dev) or "unknown"


def panel_done_load():
    """按设备读：{device_key: {what: "YYYY-MM-DD"}}。

    v1（没有设备维度）一律返回 {} —— 不知道是哪台手机的记录，宁可重扫。
    """
    try:
        with open(_panel_done_path(), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return {}
    panels = d.get("panels") if isinstance(d, dict) else None
    if not isinstance(panels, dict) or not panels:
        return {}
    try:
        ver = int(d.get("v") or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver < PANEL_DONE_V or all(isinstance(v, str) for v in panels.values()):
        return {}          # v1 旧格式：当「未知设备的历史记录」，忽略
    out = {}
    for key, per in panels.items():
        if isinstance(per, dict):
            out[str(key)] = {str(k): str(v) for k, v in per.items()}
    return out


def panel_done_save(panels):
    try:
        _state_dir()
        p = _panel_done_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            json.dump({"v": PANEL_DONE_V,
                       "panels": {str(k): dict(v) for k, v in (panels or {}).items()}},
                      f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def panel_done_mark(dev, what, log=print):
    """记「**本设备**这个面板今天走到「到底」收工了」（v2：按设备分开记）。

    调用点只有一个：drive_panel 里「连续两屏内容一致 -> 到底」那个 break（见那里的注释）。
    预算用完 / --max-rounds / 滚动保险丝 结束的**都不算完成**，绝不写 —— 写了明天就会错误跳过。
    """
    if what not in WHAT_NAME:
        return False
    key = device_key(dev)
    day = _today()
    # 2026-11-03：两台并行会同时改这个文件（读-改-写），加跨进程锁并在**锁内重读**，
    # 否则一台的「今天已完成」会被另一台基于旧快照的写盘覆盖掉。
    with _FileLock("panel_done"):
        panels = panel_done_load()          # 锁内重读，别用内存里的旧快照
        mine = dict(panels.get(key) or {})
        if mine.get(what) == day:
            return True
        mine[what] = day
        panels[key] = mine
        ok = panel_done_save(panels)
    if callable(log):
        log("[done] %s 已走到「到底」收工 -> 记入 state/%s 的本设备（%s）（%s）%s"
            % (WHAT_NAME.get(what, what), PANEL_DONE_FILE, key, day,
               "" if ok else "（写盘失败，明天会重跑）"))
    return ok


def panel_done_done_today(dev, what, force=False, log=None):
    """今天**本设备**该面板是否已经收完工：返回当天日期字符串，'' = 没完成（要跑）。

    force=True（--force-panels）永远返回 ''，即强制重跑。
    记录是按设备分开的：别的设备今天做完了不影响本设备（只在日志里说明一句）。
    """
    if force or what not in WHAT_NAME:
        return ""
    key = device_key(dev)
    panels = panel_done_load()
    got = str((panels.get(key) or {}).get(what) or "")
    if callable(log):
        others = [k for k, per in panels.items()
                  if k != key and str((per or {}).get(what) or "") == _today()]
        if others and not got:
            log("[done] %s 今天在别的设备（%s）上已完成 -> 本设备（%s）照旧重扫"
                "（记录按设备分开，v%d）"
                % (WHAT_NAME.get(what, what), "/".join(others), key, PANEL_DONE_V))
    return got if got == _today() else ""


# --------------------------------------------------------------------------- #
# 自定义脚本队列（2026-11-02 用户要求 第 7 条 / 7.3-3/4）
# 契约 C/D 在 coin11/custom_script.py 里实现；本文件只管「什么时候跑、跳过谁」。
# --------------------------------------------------------------------------- #
def _custom_script():
    """按需加载 coin11/custom_script.py。

    2026-11-02：这里改成正经 import，并把 custom_script 登记进 T11 的依赖白名单。
    原先用 __import__("custom_script") 绕过了 T11 的 ast 扫描 —— 那等于骗过静态检查：
    T11 存在的意义是「谁 import 了什么必须看得见」，绕过它的人自己也没法被审计。
    custom_script 是本仓库的本地模块（不是第三方、不是模型 SDK），本来就该在白名单里。
    仍然只在函数内 import（不是模块级）：只有真要跑脚本时才加载，坏掉也拖不垮 --what coin。
    """
    import custom_script
    return custom_script


def scripts_outdir():
    """契约 D：evidence/<日期>-scripts[-<设备>]/<id>/<HHMMSS>/

    2026-11-02：两台并行跑时脚本证据也要分开，否则两个进程会往同一个 <id>/<HHMMSS> 里写。
    单机时保持原来的目录名（不带设备后缀），老证据不用动。
    """
    suffix = ("-scripts-" + _dev_san(CUR_SERIAL)) if CUR_SERIAL else "-scripts"
    return os.path.join(_root(), "evidence", "%s%s" % (_today(), suffix))


def run_script_once(cs, dev, script, timeout, log=print):
    """跑一个脚本；执行器自己抛异常也算 failed（绝不把「跑挂了」记成「跑过了」）。"""
    sid = str(script.get("id") or "")
    try:
        return cs.run_script(dev, script, scripts_outdir(), log=log, timeout=timeout)
    except Exception:
        log("[script] %s 执行器异常 -> 记 failed：%s" % (sid, traceback.format_exc()[-400:]))
        try:
            cs.record_run(sid, "failed", 0, "执行器异常")
        except Exception:
            pass
        return {"script_id": sid, "status": "failed", "steps_done": 0,
                "error": "执行器异常", "step_records": []}


def run_script_stage(dev, ctx, args):
    """脚本阶段（7.3-3/4）。

    --script <id>（一键运行）: 只跑这一个，**不受** enabled / 今天已成功 限制 —— 人手动点的必须真跑。
    否则按队列跑：order 小的先跑、同 order 按 id 排；下面两条跳过并写日志：
        enabled 为假  /  last_ok_date == 今天（每天只成功跑一次）。
    """
    log = ctx.log
    cs = _custom_script()
    try:
        timeout = float(getattr(args, "script_timeout", 0) or 0) or 180.0
    except (TypeError, ValueError):
        timeout = 180.0
    only = str(getattr(args, "script", "") or "").strip()
    if only:
        sc = cs.load_script(only)
        if not sc:
            log("[script] 找不到脚本 %s（%s）-> 什么都不跑"
                % (only, os.path.join(cs.SCRIPTS_DIR, only + ".json")))
            return []
        log("[script] 【一键运行】单跑 %s（%s）：%d 步，不受 enabled / 今天已成功 限制"
            % (sc.get("name"), only, len(sc.get("steps") or [])))
        return [run_script_once(cs, dev, sc, timeout, log)]
    scripts = cs.list_scripts()
    if not scripts:
        log("[script] 没有自定义脚本（%s）-> 跳过脚本队列" % cs.SCRIPTS_DIR)
        return []
    state = cs.load_state()
    log("[script] 脚本队列 %d 个（按 order,id 排）：%s / 证据 %s"
        % (len(scripts), " ".join(str(s.get("id")) for s in scripts), scripts_outdir()))
    done, skipped = [], 0
    for sc in scripts:
        why = cs.queue_skip_reason(sc, state)
        if why:
            skipped += 1
            log("[script] 跳过 %s（%s）：%s" % (sc.get("name"), sc.get("id"), why))
            continue
        done.append(run_script_once(cs, dev, sc, timeout, log))
    log("[script] 队列收工：跑 %d 个（成功 %d）/ 跳过 %d 个"
        % (len(done), sum(1 for r in done if r.get("status") == "ok"), skipped))
    return done


def _state_dir():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass


def live_write(phase, detail, serial=""):
    """把「正在做什么」写给 dash 面板（state/live.json）。写不进去也绝不打断主流程。"""
    try:
        _state_dir()
        payload = {"ts": datetime.now().isoformat(timespec="seconds"),
                   "serial": serial or "", "phase": phase, "detail": detail}
        text = json.dumps(payload, ensure_ascii=False)
        for path in ([LIVE_JSON] + ([dev_state_path("live", serial)] if serial else [])):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf8") as f:
                f.write(text)
            os.replace(tmp, path)
    except Exception:
        pass


def pause_now_running(serial=None):
    """暂停判据：全局 pause.flag（全部暂停）或本设备的 pause-<设备>.flag，任一存在即暂停。

    2026-11-02：多机并行后，暂停必须按设备独立 —— 停手机2 不能把手机1 也打断。
    """
    if os.path.exists(PAUSE_FLAG):
        return True
    serial = serial if serial is not None else CUR_SERIAL
    if serial:
        return os.path.exists(dev_state_path("pause", serial, ".flag"))
    return False


def pause_wait(log=print, serial=""):
    """安全点：dash 点【暂停】= 写 pause.flag。

    看到 flag 就**原地等**（不退出进程、不重新导航、不重扫面板）；flag 消失后从原处继续。
    返回 True 表示确实暂停过。
    """
    if not pause_now_running(serial):
        return False
    live_write("paused", "已暂停，等你点【继续】", serial)
    log("[pause] 已暂停，等待继续（%s 或 %s）"
        % (PAUSE_FLAG, dev_state_path("pause", serial, ".flag") if serial else "（仅全局）"))
    t0 = time.time()
    while pause_now_running(serial):
        time.sleep(1.0)
    log("[pause] 已继续（停了 %.1f 秒），从原处接着跑，不重新导航" % _f1(time.time() - t0))
    live_write("resumed", "已继续，从原处接着跑", serial)
    return True


def jump_policy():
    """dash 页面上的「跳转确认」策略：once=仅打开一次（默认），always=始终打开。"""
    try:
        with open(JUMP_POLICY_JSON, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return "always" if str((d or {}).get("policy") or "").lower() == "always" else "once"
    except Exception:
        return "once"


def jump_label():
    return "始终打开" if jump_policy() == "always" else "仅打开一次"


def jump_prompt_present(rows):
    """系统「跳转确认」窗口：出现「仅打开一次 / 始终打开」这两个按钮词才算。"""
    t = text_of(rows)
    return ("仅打开一次" in t) or ("始终打开" in t)


def jump_prompt_apply(dev, rows, log=print):
    """按面板策略点跳转确认框。没找到对应按钮文字就**不点**，交给原看门狗停手。"""
    if not jump_prompt_present(rows):
        return False
    label = jump_label()
    hits = find_text(rows, label)
    if not hits:
        log("[jump] 检测到跳转确认框，但没找到「%s」按钮 → 不点，交给看门狗" % label)
        return False
    x, y = tap_row(dev, hits[0])
    live_write("jump", "跳转确认：点「%s」@(%d,%d)" % (label, x, y), getattr(dev, "serial", ""))
    log("[jump] 策略=%s -> 点「%s」@(%d,%d)" % (jump_policy(), label, x, y))
    time.sleep(1.2)
    return True


def _hint_norm(s):
    return re.sub(r"[\s\W_]+", "", str(s or "")).lower()


def _hint_pick(want, cards):
    """先按 row_key 精确匹配；再规范化模糊匹配。

    理由：面板行的 OCR 每次都有抖动（"好物沉浸看" / "好物沉浸看(0/3)" / "物沉浸看"），
    用户点【做这个】时记下的 key 与下一轮 OCR 出来的 key 常常差一两个字 —— 精确匹配会白丢指定。
    """
    for c in cards:
        if str(c.get("key") or "") == want:
            return c, "exact"
    nw = _hint_norm(want)
    if not nw:
        return None, ""
    best, best_len = None, 0
    for c in cards:
        for txt in (c.get("key"), c.get("name")):
            n = _hint_norm(txt)
            if not n:
                continue
            if (nw in n or n in nw) and len(n) > best_len:
                best, best_len = c, len(n)
    return (best, "fuzzy") if best else (None, "")


def _hint_load():
    try:
        with open(HINT_JSON, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return None
    if isinstance(d, dict) and d.get("row_key"):
        return d
    return None


def _hint_clear(log=None, why=""):
    try:
        if os.path.exists(HINT_JSON):
            os.remove(HINT_JSON)
            if log:
                log("[hint] %s -> 已清除 task_hint.json" % (why or "用完"))
    except OSError:
        pass


def hint_card(what, dev, cards, log=print):
    """dash 面板【做这个】写的 task_hint.json：下一轮优先做这一行（mode=once）。

    找不到该行 / serial 不匹配 / 命中黑名单 → 记日志、清除、回退自动选择。
    """
    h = _hint_load()
    if not h:
        return None
    if h.get("panel") and h["panel"] != what:
        return None                       # 指定的不是当前面板：留着，等轮到它
    serial = getattr(dev, "serial", "")
    if h.get("serial") and h["serial"] != serial:
        log("[hint] 指定的是 %s，当前设备 %s -> 不适用，已清除" % (h.get("serial"), serial))
        _hint_clear(log, "serial 不匹配")
        return None
    want = str(h.get("row_key") or "")
    card, how = _hint_pick(want, cards)
    if card is not None:
        log("[hint] 命中（%s）：%s" % (how, card.get("key")))
    if card is None:
        log("[hint] 指定的行已不在面板，回退自动选择（row_key=%s）" % want[:40])
        _hint_clear(log, "行已不在面板")
        return None
    if not card.get("doable"):
        log("[hint] 你指定的行不可做（%s）-> 拒绝执行，回退自动选择；请在面板上改选一行"
            % (card.get("why") or "黑名单/不可做"))
        _hint_clear(log, "指定行被拒绝")
        return None
    name = card.get("name") or want
    log("[hint] 优先执行：%s（%s/%s，row_key=%s）" % (name, card.get("n"), card.get("m"), want[:30]))
    live_write("hint", "指定任务：%s（%s/%s）" % (name, card.get("n"), card.get("m")), serial)
    _hint_clear(log, "已消费（mode=once）")
    return card


# --------------------------------------------------------------------------- #
# App 自检（--appcheck）：只读 pm list packages -> 打印表格 + 写 state/apps.json
#   用户 2026-11-03 原话：「我还想做个自检，像那个任务扫描一样，可以确定手机有哪些app，
#   没下载的app脚本就不会做，如果哪天下载了再跑一遍自检就可以做」。
#   三条口径（用户已拍板，不扩大）：
#     ① 本模式**不跑任务、不点屏幕、不滑动、不 am start**：只发 pm list packages + getprop（全只读）；
#     ② 「做不做」永远以**实时** pm 为准（每轮 ext_app_installed 现查）；state/apps.json 只给人看，
#        绝不让过期清单挡住一个其实已装的 App；
#     ③ 支付宝单独列，**不进**通用白名单（它走 ALIPAY_OK 授权通道）。
# --------------------------------------------------------------------------- #
APPCHECK_V = 1              # state/apps.json 结构版本
APPCHECK_HISTORY_MAX = 20   # 每台最多留多少次历史快照（够看「这次比上次多了/少了哪些 App」）


def appcheck_json_path():
    """state/apps.json（和 live.json / panel_done.json 同一个 state 目录）。"""
    return os.path.join(STATE_DIR, "apps.json")


def pm_split_packages(out):
    """`pm list packages` 的输出 -> 包名集合（纯函数：自检和离线演示共用同一份解析）。"""
    return set(l.split(":", 1)[1].strip()
               for l in str(out or "").splitlines() if l.startswith("package:"))


def appcheck_probe(serial):
    """连一台手机只读 pm 清单。返回 {serial, model, pkgs, pm_ok, err}。

    刻意**不用** Device 类：那个会顺手做 window_size 之类；自检只允许只读 shell。
    """
    info = {"serial": str(serial or ""), "model": "", "pkgs": set(), "pm_ok": False, "err": ""}
    try:
        import adbutils
        adb = adbutils.adb
        if ":" in info["serial"]:
            try:
                adb.connect(info["serial"], timeout=10)
            except Exception:
                pass                          # 已经连着会报错，不算失败
        d = adb.device(serial=info["serial"])
        try:
            info["model"] = str(d.shell("getprop ro.product.model") or "").strip()
        except Exception:
            pass
        info["pkgs"] = pm_split_packages(d.shell("pm list packages") or "")
        info["pm_ok"] = bool(info["pkgs"])
        if not info["pm_ok"]:
            info["err"] = "pm list packages 一个包都没返回（离线 / 没授权调试？）"
    except Exception as e:
        info["err"] = "%s: %s" % (type(e).__name__, e)
    return info


def appcheck_app_rows(pkgs, prev=None, at=""):
    """包名集合 -> 每个外部 App 一行（纯函数，不连手机、不读文件）。

    prev = 上一次 apps.json 里这台设备的 apps 列表 -> 用来算 first_seen / last_seen（哪天装的）。
    """
    prev_map = {}
    for a in (prev or []):
        if isinstance(a, dict) and a.get("pkg"):
            prev_map[str(a["pkg"])] = a
    rows = []
    for name, pkg, verified in EXT_APP_PKGS:
        old = prev_map.get(pkg) or {}
        installed = pkg in (pkgs or set())
        if installed:
            why = "" if verified else "包名未实测确认(verified=false)"
            first_seen = old.get("first_seen") or at
            last_seen = at
        else:
            why = ("未安装；且包名未实测确认(verified=false)" if not verified
                   else "未安装（pm 里没有这个包名）")
            first_seen = old.get("first_seen") or None
            last_seen = old.get("last_seen") or None
        rows.append({"name": name, "pkg": pkg, "verified": bool(verified),
                     # 口径：装了 -> 能做（点进去逛一下）；没装 -> 不做（运行期 ext-missing 跳过）
                     "installed": bool(installed), "doable": bool(installed), "why": why,
                     "first_seen": first_seen, "last_seen": last_seen})
    return rows


def appcheck_alipay_row(pkgs):
    """支付宝单独一行：**不进** VISIT_OK_APPS，只说明它走 ALIPAY_OK 授权通道。"""
    installed = ALIPAY_PKG in (pkgs or set())
    why = ("走授权通道 ALIPAY_OK（只认 %s 这两类行；进去只连滑、零点击、弹窗立即 BACK），"
           "不进通用白名单" % "/".join(ALIPAY_OK))
    if not installed:
        why = "未装 %s；且%s" % (ALIPAY_PKG, why)
    return {"name": "支付宝", "pkg": ALIPAY_PKG, "channel": "ALIPAY_OK",
            "installed": bool(installed), "doable": False, "doable_label": "仅授权",
            "why": why}


def _disp_w(s):
    """终端显示宽度（全角/CJK 算 2 列）。不 import unicodedata：T11 的 import 白名单锁死。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def _disp_pad(s, w):
    s = str(s)
    return s + " " * max(0, w - _disp_w(s))


def appcheck_table(headers, body):
    """ASCII 边框表格（+ - | 全是 ASCII；中文本体走 GBK，不会 UnicodeEncodeError）。"""
    widths = [max([_disp_w(h)] + [_disp_w(r[i]) for r in body]) for i, h in enumerate(headers)]
    bar = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [bar,
           "| " + " | ".join(_disp_pad(h, widths[i]) for i, h in enumerate(headers)) + " |",
           bar]
    for r in body:
        out.append("| " + " | ".join(_disp_pad(r[i], widths[i]) for i in range(len(headers))) + " |")
    out.append(bar)
    return out


def _attached_device_serials():
    """adb 现在**真的连着**的 serial（state=device）。offline / unauthorized 的不算。"""
    try:
        import adbutils
        return {str(i.serial) for i in adbutils.adb.list()
                if str(getattr(i, "state", "device") or "").lower() == "device"}
    except Exception:  # noqa: BLE001
        return set()


def appcheck_targets(serial="", serial_given=False):
    """要查哪些设备：显式 --serial 就只查它；否则读 state/devices.json 里每一台。

    2026-11-09 修（用户反馈分发版「App 自检」点了像没反应，另外结果里还挂着
    「进程退出码 2（可能有一台设备没查成功）」）：设备表里会留着换网络前的旧端点，
    它们现在是 offline，pm 查询必然失败 -> 整趟被算成失败。这里只留**真正连着**的那些。
    """
    serial = str(serial or "").strip()
    if serial_given and serial:
        return [{"serial": serial, "label": appcheck_label_of(serial)}]
    live = _attached_device_serials()
    try:
        cfg = jload(os.path.join(STATE_DIR, "devices.json"))
        devs = [d for d in (cfg.get("devices") or [])
                if str((d or {}).get("serial") or "").strip()]
    except Exception:
        devs = []
    if devs:
        keep = [d for d in devs
                if not live or str(d["serial"]).strip() in live]
        if keep:
            return [{"serial": str(d["serial"]).strip(), "label": str(d.get("label") or "")}
                    for d in keep]
    # 没跑过 discover_devices.py（没有 devices.json）或表里那几台都不在线：
    # 退回「adb 现在连着的那几台」（同样只要 state=device 的）
    try:
        import adbutils
        return [{"serial": str(i.serial), "label": ""} for i in adbutils.adb.list()
                if str(getattr(i, "state", "device") or "").lower() == "device"]
    except Exception:  # noqa: BLE001
        return []


def appcheck_label_of(serial):
    """state/devices.json 里这台机器的标签（手机1 / 手机2）。查不到就空。"""
    try:
        for d in (jload(os.path.join(STATE_DIR, "devices.json")).get("devices") or []):
            if str((d or {}).get("serial") or "") == str(serial):
                return str(d.get("label") or "")
    except Exception:
        pass
    return ""


def appcheck_load_doc(path):
    """读上一次的 apps.json（坏文件 / 不存在一律当空文档：绝不因为旧文件坏了拦住新自检）。"""
    try:
        d = jload(path)
        if isinstance(d, dict) and isinstance(d.get("devices"), dict):
            d.setdefault("v", APPCHECK_V)
            d.setdefault("at", "")
            return d
    except Exception:
        pass
    return {"v": APPCHECK_V, "at": "", "devices": {}}


def appcheck_save_doc(doc, path):
    """原子写（.tmp + os.replace）：写盘被打断也不会留半个 json。"""
    doc["v"] = APPCHECK_V
    try:
        _state_dir()
        tmp = path + ".tmp"
        jdump(tmp, doc)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def run_appcheck(args, log=print):
    """--appcheck 主流程：每台一张表 + state/apps.json 各存一份（互不覆盖，保留历史）。"""
    devs = appcheck_targets(getattr(args, "serial", ""), getattr(args, "serial_given", False))
    if not devs:
        log("[appcheck] 没有可查的设备：既没给 --serial，也没读到 state/devices.json"
            "（先跑 python coin11/discover_devices.py 写一份）")
        return 2
    path = appcheck_json_path()
    doc = appcheck_load_doc(path)
    at = datetime.now().isoformat(timespec="seconds")
    doc["at"] = at
    log("[appcheck] 只读自检：只发 pm list packages（不跑任务 / 不点屏幕 / 不滑动 / 不 am start）")
    log("[appcheck] 设备 %d 台：%s" % (len(devs), ", ".join(d["serial"] for d in devs)))
    rc = 0
    for d in devs:
        serial = d["serial"]
        old = doc["devices"].get(serial) or {}
        label = d.get("label") or str(old.get("label") or "")
        info = appcheck_probe(serial)
        if not info["pm_ok"]:
            rc = 2
            log("[appcheck] %s%s: 查询失败（%s）-> 这台不打印、不覆盖旧记录"
                % (serial, (" / " + label) if label else "", info["err"] or "未知原因"))
            continue
        pkgs = info["pkgs"]
        rows = appcheck_app_rows(pkgs, prev=old.get("apps"), at=at)
        alipay = appcheck_alipay_row(pkgs)
        n_inst = sum(1 for r in rows if r["installed"])
        n_do = sum(1 for r in rows if r["doable"])
        n_miss = sum(1 for r in rows if not r["installed"])
        prev_pkgs = set(str(a.get("pkg")) for a in (old.get("apps") or [])
                        if isinstance(a, dict) and a.get("installed"))
        cur_pkgs = set(r["pkg"] for r in rows if r["installed"])
        # 「这次比上次多了哪些 App」只在**有上一次**时才成立；首次记录不算"新增"
        # （否则第一次跑完 added 会把全部已装 App 列一遍，后人容易误读成"刚装上的"）。
        first_run = not old.get("at")
        added = ([] if first_run else
                 [r["name"] for r in rows if r["installed"] and r["pkg"] not in prev_pkgs])
        removed = ([] if first_run else
                   [str(a.get("name") or a.get("pkg")) for a in (old.get("apps") or [])
                    if isinstance(a, dict) and a.get("installed") and a.get("pkg") not in cur_pkgs])
        model = info["model"] or str(old.get("model") or "?")
        print("")
        print("=== %s%s ===" % (serial, (" / " + label) if label else ""))
        print("[appcheck] 机型=%s  pm 包总数=%d  表内外部 App=%d"
              % (model, len(pkgs), len(rows)))
        body = [(str(i), r["name"], r["pkg"],
                 "是" if r["installed"] else "否",
                 "是" if r["doable"] else "否",
                 r["why"] or "-") for i, r in enumerate(rows, 1)]
        for line in appcheck_table(("#", "名称", "包名", "已装", "能做", "原因"), body):
            print(line)
        print("[appcheck] 支付宝（单独列：走授权通道，不进通用白名单）：")
        abody = [("1", alipay["name"], alipay["pkg"], "是" if alipay["installed"] else "否",
                  alipay["doable_label"], alipay["why"])]
        for line in appcheck_table(("#", "名称", "包名", "已装", "能做", "原因"), abody):
            print(line)
        print("[appcheck] 汇总：已装 %d 个 / 可做 %d 个 / 未装 %d 个" % (n_inst, n_do, n_miss))
        if old.get("at"):
            print("[appcheck] 与上次（%s）比：新增 %s / 消失 %s"
                  % (old.get("at"), added or "无", removed or "无"))
        else:
            print("[appcheck] 首次记录这台（没有上一次可比）")
        # 落盘：每台一个独立小节（互不覆盖），带 history（最多 APPCHECK_HISTORY_MAX 条）
        hist = [h for h in (old.get("history") or []) if isinstance(h, dict)]
        hist.append({"at": at, "installed": [r["name"] for r in rows if r["installed"]]})
        doc["devices"][serial] = {
            "label": label, "model": model, "serial": serial, "at": at,
            "pm_ok": True, "pm_total": len(pkgs),
            "counts": {"total": len(rows), "installed": n_inst, "doable": n_do, "missing": n_miss},
            "prev_at": old.get("at") or None,
            "first_run": first_run, "added": added, "removed": removed,
            "apps": rows, "alipay": alipay,
            "history": hist[-APPCHECK_HISTORY_MAX:],
        }
        # 2026-11-04 真机：手机2 的「无线调试」端口变了（5555 -> 39529），旧 serial 的小节留在文件里，
        # 面板就把它和新的画成「两个手机2」。这里按**机型标签**去重：同一个 label 只保留当前 serial 这一节
        # （历史地址仍在 history 里，不丢信息）。
        for _k in [k for k, v in list(doc["devices"].items())
                   if isinstance(v, dict) and v.get("label") == label and k != serial]:
            doc["devices"].pop(_k, None)
            print("[appcheck] 清掉同一台设备的旧地址小节：%s（label=%s）" % (_k, label))
        doc["at"] = at
        ok = appcheck_save_doc(doc, path)
        print("[appcheck] %s %s" % ("已写" if ok else "写盘失败（结论不受影响）", path))
    return rc


if __name__ == "__main__":
    sys.exit(main())
