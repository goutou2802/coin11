# -*- coding: utf-8 -*-
"""把 coin11 打成「分发版」Windows 文件夹 + zip。

    python builder/build_student.py           # 全流程：PyInstaller -> 装 adb/启动器/说明 -> 打 zip
    python builder/build_student.py --no-zip  # 只要文件夹（自己调试用）

产物：
    dist/coin11/coin11.exe          双击 = 起面板（精简模式，自动开浏览器）
    dist/coin11/adb/adb.exe         随包带的 adb，对方不用装
    dist/coin11/一键启动.bat         对方双击这个
    dist/coin11/使用说明.txt
    dist/coin11-分发版-v<版本号>-YYYYMMDD.zip  发出去的就是它
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # coin11/（auto.py、dash.py 就在这一层）
COIN = ROOT
WS = os.path.dirname(ROOT)                         # 上一级工作区（android-platform-tools 在这）
#: 允许用 COIN11_DIST 指到别的输出目录（dist 被占用/锁定时的逃生口）
DIST = os.environ.get("COIN11_DIST") or os.path.join(ROOT, "dist")
WORK = os.path.join(ROOT, "build")
APP = os.path.join(DIST, "coin11")
NAME = "coin11"

#: 打进 resources/targets 的数据（规则库/设置）；人教出来的历史（manual_log/pending/shots）不发给对方
RES_FILES = ("targets.jsonl", "jump_policy.json", "panel_settings.json")
#: adb 只需要这三个（platform-tools 里其余的是 fastboot/mke2fs，对方用不上）
ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")
#: 离线自测（--selftest）断言里点名要的那几个帧；开发机的 evidence 有 900MB，只挑这几个带走
SELFTEST_FIXTURES = ("2026-11-02/scans/race_full_004436.json",
                     "2026-11-02/scans/coin_more_002517.json",
                     "2026-11-02/scans/coinspeed_tail_002348.json",
                     "2026-11-02/round-130/02_before_ocr.json",
                     "2026-11-03-auto/rounds/round-001/02_before_ocr.json")
#: 夹具暂存目录：放临时盘，别放进 build/（PyInstaller --clean 会清工作目录）
STAGE = os.path.join(tempfile.gettempdir(), "coin11-build-stage")
#: 明确不打的模块：老脚本那套 UI 自动化 / 大而无用的库，少 100MB 起步
#: 注意 onnxruntime.transformers / onnxruntime.quantization 会拐进 torch、tensorflow（实测 hook 链）：
#: 我们只用 InferenceSession，这两个子包连同它们拖来的大件一起砍掉（少 2GB 起步）
EXCLUDES = ("uiautomator2", "ddddocr", "hydra", "omegaconf", "matplotlib", "pandas",
            "scipy", "tkinter", "IPython", "pytest", "notebook", "PyQt5", "PySide2",
            "onnxruntime.transformers", "onnxruntime.quantization", "onnxruntime.tools",
            "torch", "torchvision", "tensorflow", "keras", "sklearn", "transformers",
            "h5py", "sympy", "numba", "tensorboard", "jax", "paddle")


def _find_adb():
    """找 adb.exe 的来源目录：环境变量 ADB → 仓库旁的 android-platform-tools → platform-tools → PATH。"""
    env = (os.environ.get("ADB") or "").strip()
    cands = []
    if env:
        cands.append(env)
    cands += [os.path.join(WS, "android-platform-tools", "adb.exe"),
              os.path.join(ROOT, "android-platform-tools", "adb.exe"),
              os.path.join(ROOT, "platform-tools", "adb.exe"),
              os.path.join(WS, "platform-tools", "adb.exe"),
              shutil.which("adb") or ""]
    for c in cands:
        c = os.path.abspath(c) if c else ""
        if c and os.path.isfile(c) and os.path.basename(c).lower() == "adb.exe":
            return os.path.dirname(c)
    return ""


def _run(cmd, cwd=None):
    print("[build] " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode:
        sys.exit("[build] 失败：%s（退出码 %d）" % (os.path.basename(cmd[0]), r.returncode))


def stage_selftest():
    """把夹具按 evidence/ 的原样结构摆到 build/stage/selftest 下（缺了就跳过，不阻塞打包）。"""
    dst_root = os.path.join(STAGE, "selftest")
    shutil.rmtree(STAGE, ignore_errors=True)
    got = 0
    for rel in SELFTEST_FIXTURES:
        src = os.path.join(WS, "evidence", *rel.split("/"))
        if not os.path.isfile(src):
            print("[build] 夹具缺：%s（对应的自测项会 FAIL，不影响对方用）" % rel)
            continue
        dst = os.path.join(dst_root, "evidence", *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        got += 1
    print("[build] 自测夹具 %d/%d" % (got, len(SELFTEST_FIXTURES)))
    return dst_root if got else ""


def pyinstaller():
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean", "--noupx", "--onedir", "--console", "--log-level", "WARN",
           "--name", NAME,
           "--distpath", DIST, "--workpath", WORK, "--specpath", WORK,
           "--paths", COIN]
    for f in RES_FILES:
        src = os.path.join(COIN, "targets", f)
        if os.path.isfile(src):
            cmd += ["--add-data", "%s%sresources%stargets" % (src, os.pathsep, os.sep)]
    # T11（源码卫生自测）要读自己的源码：打包版从这里读
    cmd += ["--add-data", "%s%ssrc" % (os.path.join(COIN, "auto.py"), os.pathsep)]
    stage = stage_selftest()
    if stage:
        cmd += ["--add-data", "%s%sresources%sselftest" % (stage, os.pathsep, os.sep)]
    # rapidocr 的模型（3 个 .onnx ≈ 15MB）和配置 yaml 是 data；子模块只要它自己那几个
    cmd += ["--collect-submodules", "rapidocr_onnxruntime",
            "--collect-data", "rapidocr_onnxruntime",
            "--collect-data", "onnxruntime"]
    for m in ("onnxruntime", "cv2", "adbutils", "yaml", "rapidocr_onnxruntime", "custom_script"):
        cmd += ["--hidden-import", m]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(os.path.join(COIN, "app_entry.py"))
    _run(cmd)


def assemble():
    adb_src = _find_adb()
    if not adb_src:
        sys.exit("[build] 找不到 adb.exe：设一个环境变量 ADB=<adb.exe 的完整路径> 再重试")
    adb_dst = os.path.join(APP, "adb")
    os.makedirs(adb_dst, exist_ok=True)
    for f in ADB_FILES:
        s = os.path.join(adb_src, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(adb_dst, f))
    print("[build] adb <- %s" % adb_src)
    _write_sources()
    _write_launcher()
    _write_readme()
    for sub in ("data/coin11/targets", "data/coin11/state", "data/evidence"):
        os.makedirs(os.path.join(APP, *sub.split("/")), exist_ok=True)


#: 随包带的源码：出问题时对方（或者他找的智能体）能直接看/直接改。
#: 注意：exe 是打包好的冻结版本，**改这里不会影响正在跑的 exe**，要重新打包才会生效。
SRC_FILES = ("auto.py", "dash.py", "teach.py", "custom_script.py", "discover_devices.py",
             "app_entry.py", "builder/build_student.py", "requirements.txt",
             "README.md", "AUTO_README.md")

_SRC_README = r"""# coin11 源码（随包附带）

这套源码和同目录下的 coin11.exe **是同一份代码**，放这里是为了出问题时能查、能改。

> 注意：coin11.exe 是打好的冻结程序。**改这个目录里的 .py 不会影响 exe**，
> 想让改动生效，必须按下面「怎么重新打包」重做一次。

## 每个文件干什么

| 文件 | 作用 |
|---|---|
| auto.py | 干活的主体：一条命令跑完一轮（导航 / 扫描 / 执行 / 复核 / 报告）。赛车能量已下线，代码留档在 RACE_ENABLED = False |
| dash.py | 面板本体：HTTP 服务、实时投屏、进度、轮次、记录管理、关闭面板 |
| teach.py | 教学页服务（8777/8778），认不出界面时在那儿点位置存成规则 |
| custom_script.py | 自定义脚本回放器（--what scripts） |
| discover_devices.py | 重新发现手机、写 data/coin11/state/devices.json |
| app_entry.py | 打包入口：无参数=起面板，--run-auto ...=跑一轮 |
| build_student.py | 重新打包用（见下） |

## 常见的小改（改完都要重新打包）

| 想改什么 | 改哪里 |
|---|---|
| 版本号（面板右上角、报告） | dash.py 的 PANEL_VERSION |
| 赛车能量要不要跑 | auto.py 的 RACE_ENABLED（True=跑） |
| 面板端口 / 不自动开窗口 | 启动前设环境变量：COIN11_PORT=8899、COIN11_NO_BROWSER=1 |
| 数据放哪 | 默认在 exe 同目录的 data\\ 里，删掉会重建 |
| 任务黑名单 / 可做行判定 | auto.py 里的 BLACK_WORDS、ACTION_LABELS 等常量（都有注释说明来由） |

## 怎么重新打包

1. 装 Python 3.11（64 位）
2. pip install -r requirements.txt，再 pip install pyinstaller
3. 把手机工具目录放成 adb\\adb.exe（或设环境变量 ADB=<adb.exe 完整路径>）
4. 在项目根目录跑：python builder\\build_student.py
5. 产物在 dist\\coin11\\，再压成 zip 发出去

## 出问题时先做这两件事

1. 面板上打开「日志」页签，把报错那几行截下来；
2. 没有手机也能自检：coin11.exe --run-auto --selftest（23 项，不连手机、不点屏幕）。
"""


def _write_sources():
    """把源码抄进 dist/coin11/源码/（含一份「怎么改」说明）。"""
    dst = os.path.join(APP, "源码")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for rel in SRC_FILES:
        s = os.path.join(ROOT, *rel.split("/"))
        if not os.path.isfile(s):
            print("[build] 源码缺：%s" % rel)
            continue
        shutil.copy2(s, os.path.join(dst, os.path.basename(rel)))
        n += 1
    with open(os.path.join(dst, "怎么改.md"), "w", encoding="utf-8-sig", newline="") as f:
        f.write(_SRC_README)
    print("[build] 源码 %d 个 -> %s" % (n, dst))


def _write_launcher():
    """一键启动.bat：GBK 存盘（cmd 的默认代码页就是 GBK，中文才不乱码）。"""
    bat = "\r\n".join([
        "@echo off",
        "setlocal",
        "chcp 936 >nul",
        "title coin11 面板（关掉这个窗口 = 面板停止）",
        'cd /d "%~dp0"',
        "set PYTHONIOENCODING=utf-8",
        "set PYTHONUTF8=1",
        "echo ============================================",
        "echo   coin11  淘金币 / 赛车能量 自动跑",
        "echo ============================================",
        "echo   浏览器会自动打开面板；没开就把下面这个地址粘进去：",
        "echo   http://127.0.0.1:8899/",
        "echo   关掉这个黑窗口 = 面板停止（手机上正在跑的那一轮也会断）。",
        "echo.",
        '"%~dp0coin11.exe" %*',
        "if errorlevel 1 (",
        "  echo.",
        "  echo 程序异常退出。把这一屏截图发给我。",
        "  pause",
        ")",
        "",
    ])
    with open(os.path.join(APP, "一键启动.bat"), "w", encoding="gbk", newline="") as f:
        f.write(bat)


def _write_readme():
    readme = "\n".join([
        "coin11 · 淘金币自动跑（分发版 v%s）" % _version(),
        "打包于 %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "",
        "========== 你需要什么 ==========",
        "1. 一台 Windows 电脑（64 位）",
        "2. 一根能给手机充电的数据线（不是只能充电的劣质线）",
        "3. 一台安卓手机，装着淘宝 App，并且已经登录",
        "",
        "========== 怎么用 ==========",
        "1. 把压缩包解压到电脑上（别在压缩包里直接双击，解压到桌面或 D 盘都行）",
        "2. 手机上打开 USB 调试：设置 → 关于手机 → 连续点 7 次「版本号」",
        "   → 回到 设置 → 开发者选项 → 打开「USB 调试」",
        "3. 数据线插上电脑。手机会弹「允许 USB 调试吗？」→ 点「允许」（勾上「一律允许」更省事）",
        "4. 双击「一键启动.bat」",
        "5. 浏览器会自动打开 http://127.0.0.1:8899/ ，页面上应该能看到「我的手机」",
        "   —— 看不到就点一下页面上的【刷新设备地址】",
        "6. 点【开始跑一轮】。手机放平别碰，屏幕保持亮着，跑到它自己收尾为止",
        "",
        "========== 想停 ==========",
        "点面板上的【停止】；或者直接关掉那个黑窗口（面板和这一轮都会停）。",
        "",
        "========== 它自己会做、也绝不会做的事 ==========",
        "- 会做：淘金币面板里「浏览 N 秒」「逛一逛」这类任务，做完自动回来接着下一个",
        "  （赛车能量那个活动已经下线了：点原来的入口现在进的是淘宝闪购，所以面板上它是灰的，",
        "    代码留着没删，等哪天活动复出改一个开关就能再跑）",
        "- 不会做：领取 / 授权 / 开通 / 支付 / 下单 / 答题 / 玩游戏 / 安装 App —— 这些行一律跳过",
        "- 不会改你手机的任何设置，也不会 force-stop 你的淘宝（只是把它切到前台）",
        "",
        "========== 出问题怎么办 ==========",
        "- 页面上看不到手机：数据线换根线/换个 USB 口；手机上重新点一次「允许 USB 调试」",
        "- 一轮跑完啥也没做：多半是淘宝版本变了或今天的任务页版式变了，面板上会写原因，截图发我",
        "- 杀毒软件报毒：PyInstaller 打包的程序经常被误报，加信任即可（程序不联网、不上传任何东西）",
        "",
        "========== 包里有什么 ==========",
        "coin11.exe        主程序（面板）",
        "adb\\              随包带的 adb，不用你自己装",
        "data\\             运行时数据（设备表、日志、规则库都在这里，删了会重建）",
        "_internal\\        Python 运行时和依赖，别删别动",
        "",
        "说明：程序只在本机跑（127.0.0.1），不联网、不上传截图。",
        "",
    ])
    with open(os.path.join(APP, "使用说明.txt"), "w", encoding="utf-8-sig", newline="") as f:
        f.write(readme)


def _version():
    try:
        with open(os.path.join(COIN, "dash.py"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("PANEL_VERSION"):
                    return line.split("=")[1].strip().strip('"\'')
    except OSError:
        pass
    return "?"


def zip_it():
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    base = os.path.join(DIST, "coin11-分发版-v%s-%s" % (_version(), stamp))
    if os.path.exists(base + ".zip"):
        os.remove(base + ".zip")
    out = shutil.make_archive(base, "zip", root_dir=DIST, base_dir=NAME)
    print("[build] zip: %s（%.1f MB）" % (out, os.path.getsize(out) / 1048576.0))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true", help="只出 dist/coin11 文件夹")
    ap.add_argument("--skip-build", action="store_true", help="跳过 PyInstaller，只重新装配+打包")
    a = ap.parse_args()
    if not a.skip_build:
        if os.path.isdir(APP):
            shutil.rmtree(APP, ignore_errors=True)
        pyinstaller()
    if not os.path.isfile(os.path.join(APP, NAME + ".exe")):
        sys.exit("[build] 没产出 %s.exe，先看上面 PyInstaller 的报错" % NAME)
    assemble()
    total = sum(os.path.getsize(os.path.join(d, f))
                for d, _s, fs in os.walk(APP) for f in fs)
    print("[build] 文件夹 %.1f MB -> %s" % (total / 1048576.0, APP))
    if not a.no_zip:
        zip_it()


if __name__ == "__main__":
    main()
