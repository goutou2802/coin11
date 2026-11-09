# 淘金币线复盘（只读调查，2026-11-01）

范围：只查「淘金币」线，赛车/能量线不纳入。证据标注：`会话id seq N`（均已用 session_event_read 核实原文）或 `文件:行号`（以仓库当前文件为准）。
设备：手机1 = V2505A / 淘宝 10.68.0 / 1440x3168；手机2 = iQOO 11 V2243A / 淘宝 10.68.10 / 1440x3200（`coin11/phone2/README_driver.md:3`、`session-2ed097be... seq 2713`）。
注意：`coin11/README.md` 的功能清单是宣传稿，与本文件冲突时以本文件为准。

## 已证伪清单（试过、确认不行，不要再试第二遍）

1. 做法：在淘宝 10.68 首页 feed 上直接用 description=领淘金币 / text=领淘金币 / textContains=淘金币 / TextView textMatches 找入口。
   实际现象：四种选择器全部找不到，日志「首页无入口，尝试切换到我的淘宝tab」；必须先点底部「我的淘宝」tab 才出现入口。
   证据：`session-c98a0572... seq 566, 682`；`coin11/2025淘宝双11.py:181-217`
   结论：首页 feed 没有该入口，to_11 的前四种选择器在该页永远空。

2. 做法：手机2 上点「我的淘宝」页里的「领淘金币」入口。
   实际现象：打开的是双11领券页，不是淘金币任务链路；正确入口是「淘金币抵￥227」。
   证据：`coin11/phone2/README_DAILY.md:22`；`coin11/phone2/README_driver.md:40-41`；`session-2ed097be... seq 2713`（“禁地”）
   结论：手机2 的「领淘金币」是陷阱入口，只能点「淘金币抵￥227」。

3. 做法：手机2 用 uiautomator2 控件树读淘金币任务列表。
   实际现象：整页是 android.webkit.WebView/UC WebView（content-desc=淘金币首页），不向无障碍树暴露任何子节点，整树 76 节点，任务文字一条都读不到。
   证据：`session-2ed097be... seq 1686`；`coin11/phone2/README_driver.md:4-6`
   结论：手机2 的淘金币页必须走「截图 -> OCR -> 坐标点击」，uiautomator2 选择器全部失效。

4. 做法：手机2 答题页用 OCR 读题/选项。
   实际现象：OCR 把选项字母认丢（如「A消费券、政府补贴」的 B 没了）；该页其实是原生控件，dump_hierarchy 0.22s 能拿到完整题干和 A/B/C/D bounds。
   证据：`session-f48d2bab... seq 452`；`coin11/phone2/README_fast.md:24-28`
   结论：答题页读题走无障碍（两次 dump 一致才采信），不要用 OCR。

5. 做法：「逛清单，每15秒30金币(0/2)」只打开清单页静止停留。
   实际现象：停 37.7s，计数仍 (0/2)、余额 +0；必须持续滑动才计数。
   证据：`session-cc995bc7... seq 403`；`coin11/phone2/README_fast.md:167`；`coin11/phone2/README_run2.md:8-13`
   结论：逛清单=持续滑动任务，静止停留永远 +0。

6. 做法：「点击3个商品立得」只点 1 个商品或只停留 8s。
   实际现象：余额 +0；必须进列表真点 3 个商品（进详情页再 back）才 +30。
   证据：`session-2ed097be... seq 1319`；`coin11/phone2/README_run2.md:52`；`coin11/phone2/README_run3.md:14`
   结论：这类任务按副标题执行 N 次点击，点一次就退=白做。

7. 做法：副标题只写「浏览」不给秒数的任务，停 10s。
   实际现象：+0；实测 16s 才计数（发现精选好物/看看#夜胶原 从 +0 翻正）。
   证据：`session-6b9d69fb... seq 37`；`coin11/phone2/README_run3.md:21-22`
   结论：无秒数「浏览」至少 16s，10s 不够。

8. 做法：副标题「浏览5秒/浏览2秒」的任务用滑动来凑时间。
   实际现象：一滑就跳走目标页、计数不成立；必须静止停 N 秒。
   证据：`session-6b9d69fb... seq 37`；`coin11/phone2/README_run3.md:11`
   结论：短秒数浏览=静止停留，禁止滑动。

9. 做法：跳支付宝（神奇海洋/农场）、跳闪购后按 back 返回任务面板。
   实际现象：back 停在支付宝首页/搜索页，闪购被 triver 容器接住，回不到面板；正确做法是 am start Welcome + 重新导航。
   证据：`session-f48d2bab... seq 452`；`coin11/phone2/README_fast.md:74-90`；`coin11/phone2/README_DAILY.md:23`
   结论：外部 App/triver 落地页禁用 back 返回。

10. 做法：去蚂蚁庄园、去省钱卡「点开+停留」。
    实际现象：停 13.8s/22.3s、12s，行原样、余额 +0；它们不跳支付宝，留在淘宝 TMSActivity 中间页，需要页内真实操作。
    证据：`session-f48d2bab... seq 452`；`coin11/phone2/README_fast.md:82-83`
    结论：留在淘宝容器里的“点开停留”类任务不做完。

11. 做法：「浏览5秒」但落地是消消乐，静止 5s / 滑动 16s 都试。
    实际现象：两种都 +0。
    证据：`session-6b9d69fb... seq 37`；`coin11/phone2/README_run3.md:13`
    结论：消消乐落地页按浏览做不通，需真玩。

12. 做法：「从首页入口或搜索回访」按落地页停留。
    实际现象：落地页不是首页，停多久都不算；+0。
    证据：`session-2ed097be... seq 1319`；`coin11/phone2/README_run3.md:18`
    结论：回访任务必须真的「回首页->从首页入口进淘金币」，停留无效。

13. 做法：余额读取用小裁剪 OCR。
    实际现象：数字滚动动画时新旧值粘连，OCR 读成 236762、23576376；可靠做法是回顶+全屏 OCR（或取前 5 位+连读 2~3 次取小值）。
    证据：`session-cc995bc7... seq 403`；`coin11/phone2/README_run2.md:76-77`；`coin11/phone2/README_run3.md:40-41`
    结论：余额条不要用小裁剪 OCR。

14. 做法：把页面顶部「完成进度 20/20」当任务完成判据（check_task_progress 里 current>=target 就 return True）。
    实际现象：20/20 是每日累计奖励进度条，不代表下方任务做完；当判据会提前结束、漏掉剩余任务。已改为仅供参考。
    证据：`session-c98a0572... seq 884`；`coin11/2025淘宝双11.py:655-685`（676-677 注释）
    结论：顶部进度条不能做终止条件。

15. 做法：用 Activity 名 TMSActivity 判定“已在任务列表”并按固定次数多退一次。
    实际现象：淘金币主页与任务列表同为 TMSActivity，退过头会把任务列表踢掉。
    证据：`session-c98a0572... seq 884`；`coin11/2025淘宝双11.py:127-128, 566-576`
    结论：任务列表判定靠页面标志物，不靠 Activity 名。

16. 做法：按标题黑名单过滤任务（旧逻辑把“玩消消乐领金币(浏览5秒)”误杀）。
    实际现象：有浏览保底的任务被跳过；正确判定依据是副标题（真正的完成条件），不是标题。
    证据：`session-2ed097be... seq 1319`；`coin11/2025淘宝双11.py:749-771`
    结论：过滤/分流看副标题。

17. 做法：手机2 用 d.app_start(use_monkey=True) 启动淘宝。
    实际现象：vivo 启动管理拦截；一律改用 am start Welcome。
    证据：`coin11/phone2/README_driver.md:41`
    结论：vivo 上不要 use_monkey。

18. 做法：手机2 用 uiautomator2 d.screenshot() 截图（JSON-RPC takeScreenshot）。
    实际现象：12:54 实测 http.client.RemoteDisconnected 崩溃；且 uiautomator2 3.7 不支持 screenshot(format='raw')。改为 adb screencap 兜底 + PNG magic 校验。
    证据：`session-d0847e2f... seq 225, 283`；`coin11/phone2/driver_p2.py:64-68`
    结论：手机2 截图要有 adb screencap 兜底路径。

## 已验证清单（试过、确实可用，含连续成功次数）

1. 手机1 导航链路：Welcome -> 底部「我的淘宝」tab -> 领淘金币 -> 去赚金币 -> 任务列表。
   证据：`session-c98a0572... seq 556`（节点：我的淘宝 [1208,3008][1384,3067]；领淘金币 desc [322,1865][582,2128] / text [337,2061][567,2128]）、seq 682/792/806（多次运行同日志）。
   连续成功：4 次以上（seq 682、714、792、806 均见“✓ 成功进入任务列表界面”）。点击坐标 (452,1854)。
   结论：手机1 入口是「我的淘宝」tab 里的「领淘金币」，description/text 均可命中。

2. 手机1 原生任务列表用 TextView/Button 选择器执行任务，计数真实推进：好物沉浸看 1/5→3/5、搜一搜 2/5→4/5、发现精选好物 0/5→3/5，一轮 12 个任务点击。
   证据：`session-c98a0572... seq 884`
   连续成功：单轮 12 个任务计数递增（当天 20/20 已满的语境下仍有部分任务可做）。
   结论：手机1 的原生控件链路可用。

3. 手机2 OCR+坐标点击全链路（driver_p2）：冷启动 -> 我的淘宝tab -> 淘金币抵￥227 -> 去赚金币 -> 今日速赚面板 -> OCR 找任务行 -> 点任务名中心 -> 停留 -> back -> 前后截图+余额验证。
   证据：`session-2ed097be... seq 2713`（3 个任务全部确认：2 行消失+余额+30、1 计数变化+余额+20，rc=0）；`coin11/phone2/README_driver.md:79-94`
   连续成功：3/3（单次运行），余额 22946->22996(+50)。
   结论：手机2 主线可行，验证要「行消失/计数变化 + 余额旁证」。

4. 手机2 答题（淘金币趣味课堂）：dump_hierarchy 读题（两次一致才采信）+ 百炼多模态作答 -> 点选项 -> 我选好了，答对 +30。
   证据：`session-f48d2bab... seq 452`；`coin11/phone2/README_fast.md:22-67`
   连续成功：1/1（+30，截图「回答正确」）。注意该行做完不从面板消失，当天不可重答。
   结论：答题任务可行，读题走无障碍、作答走多模态。

5. 逛清单持续滑动：每 1.5~3s 滑一次、30%~45% 屏高、每 4 次 1 次反向、累计>=40s。
   证据：`session-cc995bc7... seq 403`；`coin11/phone2/README_run2.md:8-13, 27-31`
   连续成功：1 轮 (0/2)->(2/2)，+60。
   结论：逛清单=滑动 40s 一轮做满。

6. 展开区任务按副标题分流：浏览5秒静止 / 浏览16s+滑 / 点击3个商品 / 点击去逛跳支付宝 / 玩5秒游戏静止——多数 ✅。
   证据：`session-6b9d69fb... seq 37`；`coin11/phone2/README_run3.md:9-19`
   连续成功：同轮样本 好物沉浸看+50、夜胶原+50、点击3个商品+30、三国冰河+30、金豆夺宝+30、蚂蚁森林+50（当日 +1150，余额 23206->24356）。
   结论：副标题决定做法，第三轮验证有效。

7. 支付宝「点击去逛」类（神奇海洋/农场/蚂蚁森林/金豆夺宝）：跳支付宝即算，进页面停 6~9s，am start Welcome 拉回。
   证据：`session-f48d2bab... seq 452`；`coin11/phone2/README_fast.md:79-81`；`coin11/phone2/README_run3.md:15`
   连续成功：神奇海洋+30、农场+50（第二轮）、金豆夺宝+30、蚂蚁森林+50（第三轮），多轮重复成功。
   结论：跳支付宝的“点击去逛”自动完成，用 am start 返回。

8. 任务面板同屏连做（fast_loop）：开头导航一次，之后每任务 back 一次接着做。
   证据：`coin11/phone2/README_run3.md:26-31`
   连续成功：5 任务 198s、8 任务 293s。
   结论：面板上连做比每任务冷启动快 2~3 倍。

9. OCR 提速方案：整屏 1.8s（首次 4~5s），小裁剪（名字条带 0.08s/余额条 0.11s/行条带 0.43s）+ det_limit 960；旧 6 次全图 15.4s -> 新 1 全屏+2 裁剪 1.5~2.0s。
   证据：`session-f48d2bab... seq 452`；`coin11/phone2/README_fast.md:96-113`
   连续成功：bench 各 2 样本一致。
   结论：验证只裁「名字那一行」，别裁 640px 高条带。

10. 完成识别三层（已实现）：计数推进 -> 余额旁证 -> 拉黑。
    证据：`coin11/phone2/README_run3.md:33-38`（已拉黑 11 个）
    结论：手机2 第三轮才真正有单任务级完成判据，这是修「点了就算过」的正确形态。

## 时序不稳点（等待不足、竞态、偶发失败）

1. to_11 全程固定 sleep：sleep(2) 等首页、sleep(4) 等 tab 切换、sleep(4) 等入口、sleep(3)+sleep(5) 等按钮、sleep(5) 等任务列表——无 wait-for-element，页面慢就脱靶（`coin11/2025淘宝双11.py:176,207,227,235,276`）。
2. check_in_task 一次 dump 判定，dump 慢 => 每任务 40~60s（`session-c98a0572... seq 884`）。
3. UC WebView 无障碍树不稳定：同一屏有时 110 节点/21 条文本、有时 153 节点/全部任务行，还会“撒谎”（a11y 报「淘金币首页/去赚金币」，同刻截图 OCR 是任务列表）——手机2 一律以 OCR 判状态（`coin11/phone2/README_fast.md:137-141`）。
4. 任务面板做完会重排：同一行可能换屏/换批次，「行消失」既有真做完也有被重排掉，必须结合余额（`coin11/phone2/README_driver.md:68-70`）。
5. back 回面板不稳定：落地页在 TMSActivity 里 back 无效；面板滚到下半部时「今日速赚」标题不在屏上，on_list 误判 -> 白走一次冷启动 ~40s（`coin11/phone2/README_run2.md:78-80`）。
6. 余额数字滚动粘连（见已证伪13）。
7. 天天签到 +10 延迟到账（10 分钟后），当场读数 +0 会误判失败（`coin11/phone2/README_run2.md:51`）。
8. 展开区每次展开轮换一批任务；(4/5)->(0/5) 是新实例不是失败（`coin11/phone2/README_run3.md:35-36`）。
9. OCR 单次全图 1.8~4s，不宜高频轮询；页面滚动后所有坐标作废，每次点击前必须重截图+重 detect（`coin11/phone2/README_driver.md:67,76`）。
10. 底栏被截断的任务会漏检，需要先下滑再 detect（`coin11/phone2/README.md:49`）。
11. poplayer 浮层在无障碍树无节点，只能靠关闭文案/back；back 可能把赛车页退掉，之后要从「我的淘宝」重新导航（`coin11/phone2/README_driver.md:46,74-75`）。
12. vivo 系统跳转确认框（com.vivo.appfilter）不点「始终打开」会取消跳转，闲鱼类任务因此做不成；已加放行逻辑（`session-2ed097be... seq 1319`；`coin11/2025淘宝双11.py:558-564`）。

## 跳走分支（会跑到别的页面/被弹窗带走/中途切页）

1. 手机2「我的淘宝」页点「领淘金币」-> 双11领券页（`coin11/phone2/README_DAILY.md:22`）。
2. 去神奇海洋/支付宝农场 -> 跳支付宝 AlipayGphone，back 停在支付宝（`coin11/phone2/README_fast.md:79-80`）。
3. 来闪购逛逛 -> 淘宝内换 triver 容器 TriverMainActivity，am start Welcome 也被接住（`coin11/phone2/README_fast.md:81`）。
4. 去蚂蚁庄园 -> 淘宝 TMSActivity 中间页（不是真蚂蚁庄园），停留不算（`coin11/phone2/README_fast.md:82`）。
5. poplayer 浮层遮挡任务列表，点击落到浮层上（`coin11/phone2/README_driver.md:46`）。
6. 「浏览5秒」任务一滑就跳走目标页（`coin11/phone2/README_run3.md:11`）。
7. 淘金币导航必经「升级赛车页」，coin 线和赛车线在导航上物理耦合；back 过头会退掉赛车页（`coin11/phone2/README_driver.md:36-38,74-75`）。
8. 手机1 后退过头会到 Welcome/Launcher，脚本按固定次数多退时反复触发 to_11 重新导航（`coin11/2025淘宝双11.py:595-624`）。

## 未定论（试过但次数不够，说不清好坏）

1. iQOO 11 应用锁关闭后 WebView 是否恢复无障碍暴露：关锁后未复核出明确结论，原 76 节点现象“可能与应用锁叠加有关，也可能无关”（`session-2ed097be... seq 1750`）。
2. 「天天签到免费拿IP周边」+10 延迟到账归属未 100% 确认（`coin11/phone2/README_run2.md:51`）。
3. 「看看你关注的新品动态/店铺优惠」文案每次刷新变、无 (n/m) 计数，做没做看不出来（`coin11/phone2/README_fast.md:172`）。
4. 「搜一搜你心仪的宝贝(4/5)」那一次没给，未复测（`coin11/phone2/README_run3.md:59`）。
5. 逛清单“点进商品详情页”方案：网格坐标点两次未进详情页，卡片点击区在 WebView 未暴露，方案未解决、未定论（`coin11/phone2/README_fast.md:167`）。
6. 手机1 的 xpath poplayer 关闭 watcher 被注释未启用（`session-c98a0572... seq 1126`），未验证是否可行。
7. 手机1 to_11 的盲坐标兜底 d.click(screen_width//2, screen_height*0.7/0.5)（`coin11/2025淘宝双11.py:313,340`）无实机命中记录，属猜坐标，未定论。

## 附：关于「成功判据」的核心结论（本次要修的点）

1. 父会话在复盘指令里已明确要求：每个任务必须有自己的成功判据（跑完了 or 没跑完），不能“看起来点了”；单任务学到连续成功 N>=3 再串；淘金币与赛车分开做（`session-fb52a2bc... seq 39`）。
2. 手机1 主脚本现状 = “点了就算过”：finish_count 在点击前就 +1，无任何单任务验证（`coin11/2025淘宝双11.py:773-780`）；循环终止靠顶部进度条（已证伪14）和连续无任务重启 2 次（`coin11/2025淘宝双11.py:801-814`）。
3. 手机2 是反例也是范本：driver_p2 起才引入「行消失/计数变化 + 余额旁证」的单任务验证（`coin11/phone2/README_driver.md:58-63`），第三轮升级为「计数推进 -> 余额旁证 -> 拉黑」三层（`coin11/phone2/README_run3.md:33-38`）。重写时按手机2 第三轮的判据标准回灌手机1。
