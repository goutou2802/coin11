# coin11 —— 淘金币自动跑（2026-11 现状）

一条命令：起面板 → 打开浏览器 → 让面板自己挑一台在线手机跑一轮。
赛车能量活动已下线：相关代码留档在 `auto.py` 的 `RACE_ENABLED = False`（复出改 True 即可），
详见 `_archive/赛车能量-留档说明.md`。

## 目录里现在有什么

| 文件 / 目录 | 作用 |
|---|---|
| `run.bat` | **唯一入口**：起面板（8899）+ 开浏览器 + 让面板跑一轮 |
| `dash.py` | 面板本体：实时投屏 / 进度 / 轮次 / 日志 / 记录管理 / 关闭面板 |
| `auto.py` | 执行引擎：一条命令跑完全程，证据写 `../evidence/<日期>-auto-<机型>/` |
| `app_entry.py` | 打包入口（exe 用它：无参=面板，`--run-auto`=跑一轮） |
| `teach.py` | 教学页服务（8777/8778，面板上那个【教学页】按钮） |
| `custom_script.py` | 自定义脚本回放器（`--what scripts`） |
| `discover_devices.py` | 重新发现设备并写 `state/devices.json` |
| `builder/build_student.py` | 打分发版 exe + zip（产物在 `dist/`） |
| `targets/` | 规则库 `targets.jsonl` + 待教学 `pending/` + 手动记录截图 |
| `state/` | 运行时状态（设备表 / 暂停标志 / live.json / 面板日志） |
| `dist/` | 分发版产物（`coin11-分发版-v<版本>-<日期>.zip`） |
| `_archive/` | 归档：老的探针脚本、旧截图、2025 版脚本与 README、赛车留档 |

## 常用命令

```bat
run.bat                                   :: 面板 + 一轮（设备由面板自动发现）
python coin11\auto.py --serial <机型/IP:端口> --what coin --minutes 30
python coin11\auto.py --what scan --minutes 2      :: 只扫描不点击（看全表）
python coin11\auto.py --selftest                  :: 离线自测（不连手机）
python coin11\builder\build_student.py            :: 打分发版
```

## 铁律（代码里是常量，参数关不掉）

只做「浏览 / 搜一搜 / 授权支付宝浏览」这三类；领取、授权、支付、下单、答题、玩游戏、
安装 App 一律跳过并写明原因；不 force-stop 淘宝、不改手机设置。详见 `AUTO_README.md`。

## 换机器 / 排障

- 手机上要开「USB 调试」（或用无线调试同网段）；面板【刷新设备地址】重新认人。
- 面板起来但看不到手机：`python coin11\discover_devices.py`，再刷新面板。
- 打包版：`dist\coin11\一键启动.bat`；用完点面板上的【⏻ 关闭面板】安全退出。
