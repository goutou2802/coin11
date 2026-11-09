# coin11 现状地图（复盘，只读盘点）

> 生成方式：只读检查代码 + git diff，未运行 adb/python/手机。所有代码结论带 文件:行号。
> 仓库当前状态：git 只跟踪 12 个文件（.gitignore、2025淘宝双11.py、README.md、builder/*、conf/config.yaml、requirements.txt、utils/config_manager.py、utils/utils.py），其余全部是未跟踪文件。也就是说"真正在用的那批新脚本"一个都没进版本库。

## 1. 入口点地图

### 1.1 手机1（根目录，用 uiautomator2 + utils.fastui 无障碍树 / rapidocr OCR）

| 脚本 | 实际做什么 | 关系与状态 |
|---|---|---|
| 2025淘宝双11.py (1155行) | 主脚本：连接设备→启动淘宝→watcher 弹窗→亮度压暗→to_11 导航→金币任务循环→（体力/跳一跳已禁用）→可选赛车充能 | 被 一键运行.bat/启动.bat 间接调用；实际入口是 run_daily.py 或 启动.bat 直接跑它。体力、跳一跳两阶段代码仍在但 config 已关（conf/config.yaml:24-28） |
| run_daily.py (170行) | 日常入口：选设备(adb devices/.phone_serial)→尝试无线调试→写 .phone_serial→以子进程跑 2025淘宝双11.py→存 logs/时间戳.log | 一键运行.bat 调用它（一键运行.bat:13）。它是"包装器"，不做任务 |
| run_all.py (247行) | 手机1 金币任务最终驱动：进面板一次，循环 OCR 扫任务行→按副标题算秒数→现读现点→back→验证该行是否消失，没消失写 skip_tasks.txt 拉黑 | 独立入口，python run_all.py --run。和 2025淘宝双11.py 是两条平行实现，互不调用 |
| run_tasks.py (187行) | 手机1 金币任务：先整屏扫清单再逐项做；dwell_seconds/classify 决定可做性 | 无逐任务成功验证（run_tasks.py:180-184 只 done+=1）；与 run_all.py 重复实现 |
| run_ocr_tasks.py (131行) | OCR 驱动执行器：滚动找指定任务行→点→停留→back→验证行是否消失 | 默认只做两个硬编码任务名（run_ocr_tasks.py:98-99），是过渡探针 |
| run_energy.py (245行) | 赛车能量：打开能量面板→做能量任务（验证计数推进）→回赛车页烧能量（boost 循环） | 被 _rw.py 重写过 main()（见 §5）。独立入口 |
| do_energy.py (163行) | 能量任务最简版：当前屏读任务→挑未满→做→back→对比该行 cur/tot 是否推进 | run_energy.py 的简化平行版，最新时间戳（13:13） |
| preflight.py (98行) | 运行前自检：依赖/ADB/设备/淘宝版本，不改手机 | 被 启动.bat 调用（启动.bat:20） |
| probe_nav / probe_tasks / probe_final / probe_energy / probe_en / probe_phone2 / probe_top / scan_panel / scan2 / ocr_tasks / dbg / final_check / try_stay / fin / fixdlg / look / test_final / test_ocr_energy | 一次性调试探针：只验证导航/OCR/控件树/赛车闭环等单一问题 | 判断依据：①无任何文件 import 它们（grep 0 命中）；②docstring/内容为单步调试（如 probe_nav.py:2 "只验证导航，不执行任何任务点击"，fixdlg.py 只按 back 看按钮）；③mtime 集中在 10-31 23:00~11-01 04:00 的试错期，随后被正式 run_*.py 取代 |
| _rw.py (119行) | 一次性"自改脚本"：读取 run_energy.py，把 main() 整段替换成带 close_unlock_dialog 的新版 | 纯工具，跑完即废；见 §5 |

### 1.2 手机2（phone2/，全链路"截图→OCR→坐标点击"，因淘金币页是 WebView、无障碍树读不到）

| 脚本 | 实际做什么 | 关系与状态 |
|---|---|---|
| driver_p2.py (446行) | 手机2 主驱动：导航到任务面板→OCR 任务行→挑任务→做→verify_task 对比 (n/m) 计数变化→无变化拉黑 | 手机2一键运行.bat 直接调它。含完整的逐任务成功判定（driver_p2.py:277-314） |
| fast_loop.py (201行) | 展开区连做循环：展开一次后上下扫着做，完成判定=计数推进/行消失/余额涨，失败写 skip_tasks.txt | 比 driver_p2 更贴近"连续做"需求，但设备序列号走 nav2.SERIAL |
| do_tasks.py (243行) | 展开区识别（rows_expanded）+ 按副标题执行（停留N秒/点击N个商品），用余额+计数验证 | 被 fast_loop.py、run4.py、p2.py、tap4.py import 的函数库+独立入口 |
| browse_list.py (180行) | 「逛清单」单任务验证：持续滑动 40s 推进 (0/2) 计数 | 被 driver_p2/do_tasks/fast_loop import（browse/parse_cnt） |
| tap_bottom.py (162行) | 「更多金币等你赚」底部任务：滑到底→展开→点目标→回来复验计数 | 被 do_tasks/fast_loop import（back_fast/to_top/on_panel） |
| quiz2.py (267行) | 趣味课堂答题：无障碍树读题→百炼 deepseek-v4.1-flash-1017 作答→点选项→提交→下一题 | 独立入口，配套 llm.py/a11y.py |
| run4.py (198行) | 名为"共用工具"，实为函数库（无 __main__） | 无任何文件 import run4（grep 0 命中）→ 死模块，被 p2.py 取代 |
| p2.py (189行) | "run4 工具层"函数库（无 __main__），复用 driver_p2 导航 | 被 tap4/check4/game4/search4/topread/probe4 import——这些全是交互式探针，所以 p2 随它们一起归档 |
| tap4.py (163行) | 交互式命令行（cmd_see/find/wait/sweep/back/panel/bal/shot） | 调试探针 |
| check4 / game4 / search4 / topread / probe4 / scan_bottom / run_bottom / peek / tap_one / qd / qd2 / browse_run / fast_one / ls2 / ls3 / state / hier / a11ytest / bench* / qprobe / crop_ocr / ocr_dump / nav_experiment / one_task / probe_browse / probe_tree / ocr_shots / test_* | 一次性调试/基准测试 | 判断依据同上：互不 import 进主链路；内容为单点验证（bench* 是 OCR 速度对比，qd/qd2 是签到实验）；mtime 集中在 11-01 01:00~03:30 |

### 1.3 支持库

- utils/fastui.py: Snapshot 类，一次 dump_hierarchy 本地解析（手机1 无障碍树路径）。被 2025淘宝双11.py、run_all.py、run_tasks.py、racing.py 等引用。
- utils/taobao_nav.py: goto_task_list（手机1 导航，被 run_all/run_ocr_tasks/probe_final/scan2/fin 等引用）。
- utils/device.py: connect()，读 .phone_serial 决定连哪台（被所有根目录 run_*.py 引用）。
- utils/racing.py: 赛车充能函数库（见 §3）。
- utils/quiz.py / quiz_ocr.py: 手机1 答题（百炼）；racing.handle_energy_popup 调 quiz_ocr。
- phone2/nav2.py: 手机2 共享件（OCR 引擎、ensure_list 导航、read_balance）；被 driver_p2 之外几乎所有 phone2 脚本 import。
- phone2/detect_tasks.py: OCR→任务行解析原型；driver_p2.rows_from_items 复用其 _load_items/_same_line_candidates/_text_below。
- phone2/fast_ocr.py: 快速 OCR（区域裁剪+缓存），仅 qprobe/state 少量引用，nav2.py 另实现了一套自己的 OCR。

## 2. 淘金币线现状

### 2.1 手机1 主脚本 2025淘宝双11.py

- 选择器：无障碍树为主。导航入口 "领淘金币"/"我的淘宝"/"赚金币"（to_11，:146-360）；任务卡片用快照本地解析 snap.tasks()（utils/fastui.py:96-127，认 Button 文案 去完成/去逛逛/逛一逛 + 任务名带 (n/m) 计数 + 副标题）；"立即领取"用 snap.center（:733-736）。
- 等待条件：固定 sleep（启动 wait 3s、导航间 4-7s、任务间 wait_between_tasks 2s）；无元素级 wait_for，靠 d(selector).exists 轮询。
- 任务过滤（:740-763）：副标题含"浏览/点击去逛/回访"才放行黑名单；副标题含"下单/购买/支付"且无"浏览"跳过；EXTERNAL_APPS 未安装跳过（:81-93）。
- 执行（operate_task :483-653）：按副标题分流——"点击N个"→click_n_items(:382)、"答题"→answer_quiz、 "回访/首页入口"→visit_by_search(:426)；其余按 browse_duration 随机滑动；返回逻辑靠 Activity 名 + check_in_task 标志物。
- **成功判据**：
  - 全局：check_task_progress(:655-700) 找含"进度"的 TextView 且 n/n，n>=target_count(40) 才 True；找到第一条进度文本但未达标就立刻 return False（:694-697），且注释自认 "20/20 是每日累计奖励进度条，不能当终止条件"（:690-691）。
  - 逐任务：**不存在**。finish_count 在"挑中任务"时就 +1（:778、:987），不是做完验证后才加；operate_task 返回后不检查该行 (n/m) 是否推进。
  - 终止：进度达标，或连续 3 次无任务→重启淘宝→重启 2 次仍无可做即"判定金币任务已完成"（:805-812）。
- 结论：手机1 金币线没有单任务级成功判据，只有全局进度条 + "无可做任务"兜底。

### 2.2 手机1 平行实现（run_all.py / run_tasks.py / run_ocr_tasks.py）

- run_all.py：OCR 读行（scan_panel :51-84 解析名称(n/m)），现读现点（locate_now :98-113），back_once(:156) 后验证"该行是否还在"，还在则写 skip_tasks.txt 拉黑（:226-233）。这是**有**逐任务验证的版本，但判据是"行消失"，不是计数推进。
- run_tasks.py：先 scan_all 整屏清单（:82-108），逐项做，back_to_list(:120) 只确认回到列表，**不验证任务是否完成**（:180-184 done+=1 无条件）。
- run_ocr_tasks.py：scroll_find(:48) 找指定行→点→浏览→back_to_panel(:78)→验证行是否还在（:120-124）。过渡版，默认任务名硬编码。
- 三个脚本各自重写了 OCR 行解析、按钮就近匹配、dwell 秒数规则，互不复用（run_all.py:33-113 vs run_tasks.py:62-108 vs run_ocr_tasks.py:19-57）。

### 2.3 手机2（phone2/，OCR 全链路）

- 选择器/坐标：无控件树可用，detect_tasks.py:103 把 OCR 结果组装成任务行（奖励 +N 左侧最宽文字=任务名，正下方=副标题）；driver_p2.rows_from_items(:78-102) 复用同一逻辑。坐标全部现截现算，不写死。
- 导航：goto_task_list（driver_p2.py:173-216）/ nav2.ensure_list（nav2.py:101-144）：OCR 找"去赚金币"/"淘金币抵"/"我的淘宝"，循环点+冷启动兜底，标记物 "今日速赚/任务到访得金币"。
- 弹窗：POPUP_CLOSE 文案点掉（driver_p2.py:145-153）；nav2 同款（nav2.py:15）。
- 等待：固定 sleep + 每次 OCR 即状态。
- **成功判据（这条线做得最完整）**：
  - driver_p2.verify_task(:277-314)：做完回面板找该行，名字 (n/m) 变了或行消失=成功；无变化→no_change 拉黑本次运行（:431-436）；余额变化作旁证（:439-444）。
  - fast_loop.py:191-213：计数推进/行消失=成功，否则回顶读余额，余额涨也算成功；仍失败写 skip_tasks.txt 拉黑。
  - do_tasks.py:229-238：余额涨了算成功，没涨打 ✗（只按余额，弱判据）。
- 注意：手机2 内部有两套序列号——driver_p2._load_serial 读 phone2/.serial（=192.168.1.4:5555，driver_p2.py:36-44），而 nav2.SERIAL 硬编码 "10AD4U0UHS0025A"（nav2.py:12）。所以 driver_p2.py 连的设备和 quiz2/fast_loop/do_tasks 连的**不是同一台**（除非 .serial 被改回）。phone2/README_fast.md:186 声称"所有脚本硬校验 10AD4U0UHS0025A"已不属实。

## 3. 赛车线现状（utils/racing.py 400行 + 两个执行器）

- 核心函数：read_energy(:28) 优先按钮文案"剩余 N 能量"、退回 OCR 左上角（read_energy_ocr :226）；can_boost(:42) 看文案有没有"长按可加速升级"；find_boost_btn(:54)；boost(:119) 长按 seconds 秒返回 (消耗前,消耗后) 能量；goto_racing(:92) back 兜底回赛车页；on_racing_page(:79) 靠"升级赛车拿钱/松手开始加速升级/组队赢"判定，避免 OCR 计数器误判。
- 充能：open_energy_tasks(:150) 点"去赚能量"；run_energy_tasks(:170) 优先浏览类任务、跳过 _SKIP_WORDS(:165)，每个浏览 browse 秒滑动，back 回赛车页。
- 弹窗/领取：handle_energy_popup(:290) OCR 答题弹窗（+1000 能量）；claim_energy_reward(:261) OCR 找"立即领取"；refresh_racing(:325) 离开再进刷新滞后能量值。
- 主流程 run_racing(:350)：能量<800 先做最多 max_energy_tasks 个能量任务并领取奖励→刷新→回赛车页循环 boost，最多 max_boost_rounds 轮。
- **成功判据**：
  - 单次 boost 成功 = res[1] < res[0]（能量确实减少，:384-386）；能量没降/读不到就停。
  - 能量任务是否做成：run_energy_tasks 只 got+=1（:215），**不验证**任务计数是否推进。
  - "产出金币"只有 read_summary(:65) 读"第N笔/升级已得"打日志，**不参与判定**——能量烧了但金币没涨不会被发现。
- 执行器：
  - run_energy.py：先做能量任务（做完对比该行是否还在，:216-220），再阶段B 烧能量（:232-240）；被 _rw.py 注入过 close_unlock_dialog。
  - do_energy.py：当前屏读任务→做→对比该行 cur/tot 是否推进（:150-157），判据更严。
  - 2025淘宝双11.py 第五阶段（:1110-1130）：config racing.enabled=false 默认关闭，开则调 run_racing。
- 结论：赛车线有"能量下降"这个单步判据，但缺"金币产出/任务计数"闭环；能量任务部分两套实现（racing.run_energy_tasks 不验证，do_energy.py 验证）。

## 4. 两任务耦合点

1. 设备初始化：
   - 手机1 全部走 utils/device.py:19 connect()，读 .phone_serial；run_daily.py:74 ensure_device 负责选设备并写 .phone_serial。
   - 手机2 不走 utils/device，driver_p2 读 phone2/.serial（:36-44），其余脚本硬编码 nav2.SERIAL=10AD4U0UHS0025A（nav2.py:12）→ 同一目录两个序列号。
   - 赛车/金币两线在手机1 共享 connect() 出来的同一个 d（2025淘宝双11.py:28）。
2. 导航：
   - 淘金币线：手机1 有 utils/taobao_nav.goto_task_list 与 2025淘宝双11.py 自带的 to_11 两套；手机2 有 driver_p2.goto_task_list 与 nav2.ensure_list 两套。
   - 赛车线自己一套 goto_racing/refresh_racing（racing.py:92/325），入口"去赚能量"与淘金币面板入口是同一屏的两个不同按钮（racing 从赛车页进能量面板，金币线进"今日速赚"面板）。
3. 弹窗处理：主脚本 watcher（:87-104）+close_all_dialog(:117)；racing 有 handle_energy_popup(:290) OCR 答题弹窗；手机2 有 dismiss_popup。互不共享。
4. 进度/计数解析：(n/m) 计数被至少 6 处独立实现——utils/fastui._CNT_RE、run_all.in_panel、run_energy.scan_energy、do_energy.rows、phone2/browse_list.parse_cnt、phone2/do_tasks.rows_expanded、driver_p2.COUNT_RE。余额读取 3 处独立实现（nav2.read_balance、driver_p2.read_balance、p2.balance、do_tasks.bal）。这是最大的耦合点：**规则复制粘贴，没有共享**。
5. 真正的交叉只有一处：2025淘宝双11.py 第五阶段 import utils.racing.run_racing（:1117），使金币主脚本尾部接上赛车充能；其余 racing 调用方（run_energy/do_energy/probe_energy/test_*）都是独立的能量脚本，不碰金币任务。

## 5. 未提交改动（git diff）与两个 .phone 文件

- 2025淘宝双11.py（+387/-167，最核心）：
  - 设备连接改走 utils/device.connect（读 .phone_serial）。
  - d.info 异常回退 window_size（Android 17 兼容）。
  - 新增 EXTERNAL_APPS 未安装探测（:81-93），避免每轮 RPC。
  - watcher 增加 vivo "始终打开/仅打开一次"放行（:91-95）。
  - 新增亮度压暗+恢复（:103-113、:1133-1143）。
  - check_in_task 从 Activity 判定改为页面标志物判定（:124-144）。
  - to_11 增加"我的淘宝 tab"兜底、赚能量/赚体力双文案（:199-217、:229-263）。
  - 新增 click_n_items（点N个商品）与 visit_by_search（首页回访），operate_task 按副标题分流（:368-516）。
  - 返回流程：最高优先"任务列表可见就停"、vivo 跳转框放行、删除"跳转过外部页强制多退一次"（:557-585）。
  - check_task_progress 改为快照本地找进度、避免无条件滑动（:656-700）。
  - 金币循环：snap.tasks() 本地过滤替代连环 RPC + 重启上限 2 次防无限重启（:726-812）。
  - 追加第五阶段赛车（:1110-1130）。
  - 意图一句话：把"每判定一次就发 RPC"的旧写法换成一次 dump 本地解析，并补上外部 App 跳转、回访、点N个商品等新任务类型的处理；体力/跳一跳暂时关闭。
- conf/config.yaml（+14/-3）：
  - 新增 racing 配置段（enabled:false、boost_seconds:5、max_boost_rounds:8、max_energy_tasks:6）。
  - physical.enabled 与 jump.enabled 由 true 改 false（理由注释：淘宝 10.68 入口改为"去赚能量"、跳一跳依赖旧控件）。
  - browse_duration 16→17。
- utils/utils.py（+7/-5）：
  - check_chars_exist 黑名单追加 "下单"（:12-13）。
  - get_current_app 正则改为截断 activity 名后可能带的 " type=N"（:23-24，修 Android 13+ 状态机永不匹配）。
- .phone_serial（内容 192.168.1.8:5555）：utils/device.py:8 和 run_daily.py:15 都读它，是手机1 的无线调试地址/序列号。
- .phone_ip（内容 10.5.19.92）：**全仓库无任何代码引用**（grep 0 命中），是早期调试遗留的 IP 备忘，死文件。
- 另注意：phone2/.serial=192.168.1.4:5555（driver_p2 用），而 nav2.SERIAL 硬编码 10AD4U0UHS0025A——手机2 存在两个序列号来源。
- _rw.py 是未跟踪的"自改脚本"：读 run_energy.py 并把 main() 整体替换成带 close_unlock_dialog 的新版（_rw.py:1-119），解释了 run_energy.py 为什么长这样；用完即废。
- 未定论：requirements.txt 未列 rapidocr_onnxruntime，但所有 OCR 脚本 import 它——无法确认用户是否另行安装，列为环境风险。

## 6. 结论表

### 可直接复用
- utils/fastui.py（Snapshot 一次 dump 本地解析）：被 6+ 文件引用，接口干净。
- utils/racing.py 的底层原语 read_energy/can_boost/find_boost_btn/boost/goto_racing/on_racing_page/read_energy_ocr：各自独立、有返回值可判据。
- phone2/nav2.py（OCR 引擎+ensure_list+read_balance）、phone2/detect_tasks.py（任务行解析）、phone2/browse_list.py 的 browse/parse_cnt（滑动计数实测结论）。
- phone2/quiz2.py + llm.py + a11y.py（答题全链路，含两路读题与模型作答）。
- utils/device.py、run_daily.py（设备选择与日志包装）。
- utils/quiz.py / quiz_ocr.py（百炼答题与 OCR 弹窗答题）。

### 必须重写（拆成带成功判据的独立函数）
- 2025淘宝双11.py 主循环：finish_count 按"挑中"计数而非"完成"；check_task_progress 找到第一条进度文本就 return；金币/体力/跳一跳/赛车四阶段塞在一个 1155 行脚本里。
- run_tasks.py：完全无逐任务成功验证。
- run_energy.py / do_energy.py / run_ocr_tasks.py：重复实现 OCR 行解析与判据，应统一。
- run_all.py：判据只有"行是否消失"，无计数对比；扫描与执行耦合。
- phone2/driver_p2.py、fast_loop.py、do_tasks.py：判据已有但混杂设备序列号、skip_tasks.txt 落盘、扫描/执行/验证三种职责，应拆成 任务识别→执行→验证 三个独立函数。
- utils/utils.py 的 check_chars_exist（巨型关键词黑名单）与 task_loop/close_xy_dialog（旧代码，基本无人调用）。

### 建议归档/删除
- 全部 probe_* / scan_panel / scan2 / ocr_tasks / dbg / final_check / try_stay / fin / fixdlg / look / test_*（根目录探针，无 import、内容为单步调试）。
- _rw.py（一次性自改工具）。
- phone2/run4.py（死模块，0 引用）；phone2/p2.py 及其全部调用方 tap4/check4/game4/search4/topread/probe4（交互式探针群）。
- phone2 其余探针/基准：bench*、qprobe、crop_ocr、ocr_dump、nav_experiment、one_task、probe_browse、probe_tree、ocr_shots、ls2、ls3、state、hier、a11ytest、qd、qd2、peek、tap_one、scan_bottom、run_bottom、browse_run、fast_one、test_*。
- 111 张 .png 截图与 logs/、phone2/shots_*、*.png、energy_run.log 等调试产物。
- .phone_ip（0 引用）；README.md 功能清单（声称体力/跳一跳自动完成，实际 config 已关且 README 未同步）。
