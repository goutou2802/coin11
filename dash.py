#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coin11/dash.py — 总控台：监控 + 实时投屏 + 直接操作手机 + 保活 / 暂停继续 / 指定任务。

    python coin11/dash.py        然后手机/电脑浏览器打开 http://127.0.0.1:8899

只读采集（绝不改 evidence）：
    evidence/<最新>*-auto/rounds/round-*/06_verdict.json   每轮判定
    evidence/<最新>*-auto/scans/*.json                     全表快照（累计进度）
    evidence/<最新>*-auto/navfail/*.json                   导航失败
    evidence/<最新>*-auto/run.log                          运行日志
    coin11/targets/pending/*.json                          待教学问题
面板自己启的 auto.py 只由面板杀（按 pid + 进程树，不碰别人的进程）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
#: 8899 被占用时（同学电脑上什么都可能占着）用 COIN11_PORT 换一个端口，不用改代码
try:
    PORT = int((os.environ.get("COIN11_PORT") or "").strip() or 8899)
except ValueError:
    PORT = 8899
REFRESH_MS = 3000
LOG_MAX = 400
#: 2026-11-04 用户要求（原话：「这个不是实时的吗，我想要实时的」）：新增 SSE 推送 /api/stream，
#: 日志 / 运行状态 / 顶部状态条 500ms 一帧。REFRESH_MS 的 3 秒轮询**保留**：
#: rounds / scans / scripts 这些重数据按轮次变，不需要实时，继续走 /api/state（也是 EventSource
#: 不可用时的降级路径）。改这个值就是改推送频率。
SSE_MS = 500
#: dash 已经在跑着的时候，别人（或别的会话）用命令行直接起的 auto.py 也得接管 ——
#: 但 auto_procs() 每查一次就 spawn 一个 powershell（自带 3 秒缓存），绝不能跟着 500ms 的帧走。
ADOPT_EVERY_S = 5.0

#: 数据根：COIN11_ROOT 优先（打包版 app_entry.py 注入 exe 同级的 data/），否则仓库根。
#: 源码模式两者逐字等价，所以开发流程零回归。
ROOT = os.path.normpath(os.environ.get("COIN11_ROOT") or
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
COIN = os.path.join(ROOT, "coin11")
EVIDENCE = os.path.join(ROOT, "evidence")
AUTO_PY = os.path.join(COIN, "auto.py")
SERIAL_FILE = os.path.join(COIN, ".phone_serial")

#: 2026-11-03 修：dash 若被**无控制台**的方式启动（例如 DETACHED_PROCESS + 重定向），
#: 它每 spawn 一个控制台子进程（adb / powershell / auto.py），Windows 都会给**子进程新开一个
#: 黑窗口** —— 用户看到的就是「终端反复开启关闭」，而且约每 3 秒一次（auto_procs 的缓存周期）。
#: 给所有子进程加 CREATE_NO_WINDOW（0x08000000）：进程照常跑，只是不许它开窗口。
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
#: 教学页端口按**设备标签**映射（2026-11-03：地址随网络变、标签不变）
TEACH_PORTS_BY_LABEL = {"手机1": 8777, "手机2": 8778}
TEACH_PORTS = {"192.168.1.8:5555": 8777, "192.168.1.5:43059": 8778}   # 下面按 DEVICES 重建
DEFAULT_SERIAL = "192.168.1.8:5555"                                    # 下面按 DEVICES 重建
#: 2026-11-09：赛车能量活动下线（点原入口进的是「淘宝闪购」），auto.py 那边 RACE_ENABLED=False。
#: 面板上同步只留淘金币；活动复出时把 auto.py 的开关打开、这里把 race 加回元组即可。
RACE_ON = os.environ.get("COIN11_RACE") == "1"
PANELS = ((("coin", "淘金币"), ("race", "赛车能量")) if RACE_ON else (("coin", "淘金币"),))
PANEL_MAX = 50.0

#: 2026-11-09 用户要求「最好帮我做做版本更新，这样我后面可以给他们加功能方便分发」。
#: 每次发布改这里，程序标题和 /api/state 都会带上版本号，同学报问题时一眼能对上是哪版。
PANEL_VERSION = "2.0.0-dev"

#: 2026-11-09 给同学用的「精简/不留档」模式：COIN11_STUDENT=1 时启用。
#: - 子进程自动继承 COIN11_NO_EVIDENCE=1 -> auto.py 把证据写进临时目录、退出即删；
#: - 前端隐藏轮次表等重信息，只留 连接状态 / 开始-停止 / App 自检。
STUDENT_MODE = os.environ.get("COIN11_STUDENT") == "1"
if STUDENT_MODE:
    os.environ["COIN11_NO_EVIDENCE"] = "1"     # 子进程直接继承，不用改 Popen 的 env

# ---------------------------------------------------------------- 总控台：路径 / 设备 / 参数
STATE_DIR = os.path.join(COIN, "state")
LIVE_JSON = os.path.join(STATE_DIR, "live.json")        # auto.py 写，面板读
PAUSE_FLAG = os.path.join(STATE_DIR, "pause.flag")      # 存在 = 暂停（同时也是「不许自动重启」的锁）
#: 存在 = 关闭实时投屏（2026-11-02 用户要求：一直抓帧会让手机发烫，也可能干扰 auto.py 自己截图）
SCREEN_OFF_FLAG = os.path.join(STATE_DIR, "screen_off.flag")
CTL_LOG = os.path.join(STATE_DIR, "ctl.jsonl")          # STOP / PAUSE / RESUME 审计
KEEP_LOG = os.path.join(STATE_DIR, "keepalive.log")     # 每一次 adb 保活动作
KEEP_JSON = os.path.join(STATE_DIR, "keepalive.json")   # 保活开关：跨 dash 重启保留用户的选择
TARGETS_DIR = os.path.join(COIN, "targets")
JUMP_POLICY = os.path.join(TARGETS_DIR, "jump_policy.json")
MANUAL_LOG = os.path.join(TARGETS_DIR, "manual_log.jsonl")
SNAP_DIR = os.path.join(TARGETS_DIR, "shots")
PANEL_CFG = os.path.join(TARGETS_DIR, "panel_settings.json")
TARGETS_JSONL = os.path.join(TARGETS_DIR, "targets.jsonl")   # 教过的规则（规则管理只动这个文件）

#: 2026-11-03：设备地址随网络变（家里 wifi / 校园网 + 热点），改成**可配置**。
#: 优先级：state/devices.json（跑 `python coin11/discover_devices.py` 自动生成）> 下面这组默认值。
DEVICES_DEFAULT = (("192.168.1.8:5555", "手机1"), ("192.168.1.5:43059", "手机2"))


def _load_devices():
    """读 state/devices.json：{"devices":[{"serial","label","model"}]}；没有/坏了就用默认。"""
    p = os.path.join(STATE_DIR, "devices.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        rows = d.get("devices") if isinstance(d, dict) else None
        out = [(str(it.get("serial") or "").strip(), str(it.get("label") or "").strip())
               for it in (rows or []) if isinstance(it, dict)]
        out = [(s, l) for s, l in out if s and l]
        if out:
            return tuple(out)
    except Exception:  # noqa: BLE001
        pass
    auto_rows = _auto_mode_devices()
    if auto_rows:
        return auto_rows
    return DEVICES_DEFAULT


#: 机型 -> 标签（和 discover_devices.py 的 MODEL_LABEL 一致）
DEV_MODEL_LABEL = {"V2505A": "手机1", "V2243A": "手机2"}
DEV_FALLBACK_PORTS = (5555, 43059)


def _attached_devices():
    """adb devices 里当前真的连着的设备（USB 线 / 无线都算）-> [{"serial","label","model"}]。

    打包版（COIN11_DEVICE_AUTO=1）才用：同学的手机型号不可能预登记在 DEV_MODEL_LABEL 里，
    靠「扫 IP + 认机型」的发现流程对他们无效 —— 直接问 adb「现在连着谁」。
    """
    out, err = _adb_run(["devices"], timeout=15)
    if err or not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        ser = parts[0]
        if any(r["serial"] == ser for r in rows):
            continue
        m_out, _e = _adb_run(["-s", ser, "shell", "getprop", "ro.product.model"], timeout=15)
        model = (m_out or "").strip().splitlines()[-1].strip() if (m_out or "").strip() else ""
        label = DEV_MODEL_LABEL.get(model) or ("我的手机" if not rows else "我的手机%d" % (len(rows) + 1))
        rows.append({"serial": ser, "label": label, "model": model})
    return rows


#: adb_path() 在本文件后半段才定义，而 _load_devices() 在导入期就被调用了 ——
#: 那一次问不了 adb（会 NameError），所以先置位 False，等 adb 层就绪后再补一次读表。
_ADB_READY = False


def _auto_mode_devices():
    """COIN11_DEVICE_AUTO=1 时：设备表 = 现在连着的那些；一台都没连就交回调用方的默认表。"""
    if not _ADB_READY or os.environ.get("COIN11_DEVICE_AUTO") != "1":
        return ()
    return tuple((r["serial"], r["label"]) for r in _attached_devices())


def reload_devices():
    """devices.json 变了之后重绑模块全局（/api/state 与设备下拉都读这几个）。

    2026-11-04 真机事故（用户点【刷新设备地址】后网络面板白屏）：这里原来只重绑了 DEVICES 等四个变量，
    没重建 _KEEP —— 而 build_state() 里 `keep_state()[serial]` 是**直接下标**，新端口不在 _KEEP 里
    就 KeyError -> /api/state 500 -> 面板整个白掉（只能重启 dash 才能恢复）。所以 _KEEP 必须一起重建。
    """
    global DEVICES, DEVICE_LABELS, DEFAULT_SERIAL, TEACH_PORTS
    DEVICES = _load_devices()
    DEVICE_LABELS = dict(DEVICES)
    DEFAULT_SERIAL = DEVICES[0][0]
    TEACH_PORTS = {ser: TEACH_PORTS_BY_LABEL.get(lab, 8777) for ser, lab in DEVICES}
    # 重建保活状态：新地址补一条默认；地址没变的**保留原状态**（开关/计数/日志都不丢）；
    # 已从设备表消失的留在内存里不删（免得正在跑的保活线程被打断），只是不再显示。
    with _KEEP_LOCK:
        for _ser, _lab in DEVICES:
            if _ser not in _KEEP:
                _KEEP[_ser] = {"serial": _ser, "label": _lab, "on": False,
                               "interval": KEEP_DEFAULT_MIN, "thread": None, "stop": None,
                               "last_at": None, "last_result": "", "hits": 0, "skips": 0,
                               "wakes": 0, "log": deque(maxlen=80), "next_at": None}
            else:
                _KEEP[_ser]["label"] = _lab


def _adb_run(args, timeout=18):
    """跑一次 adb（带 _NO_WINDOW，别弹黑窗）。返回 (输出, 错误)。"""
    ap = adb_path()
    if not ap:
        return None, "找不到 adb 可执行文件"
    try:
        r = subprocess.run([ap] + list(args), capture_output=True, text=True,
                           errors="replace", timeout=timeout, creationflags=_NO_WINDOW)
        return ((r.stdout or "") + (r.stderr or "")), None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _dev_mdns_candidates():
    """从 adb mdns services 里挖无线调试的 ip:port —— 端口每次重启手机都会变。"""
    out, err = _adb_run(["mdns", "services"], timeout=12)
    cands = []
    for line in (out or "").splitlines():
        s = line.strip()
        if "_adb-tls-connect._tcp" not in s and "._adb" not in s:
            continue
        for tok in s.split():            # split() 自动吃掉 \t，不写反斜杠
            if ":" in tok and tok.split(":")[0].count(".") == 3:
                cands.append(tok)
    return cands, err


def _dev_all_candidates():
    """mDNS + 当前表里的地址 + 历史地址 + 已知 IP 的老端口，全部去重。"""
    out = list(_dev_mdns_candidates()[0])
    ser_list = []
    try:
        with open(os.path.join(STATE_DIR, "devices.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        ser_list += [str(x.get("serial") or "") for x in (doc.get("devices") or [])]
        for lst in (doc.get("history") or {}).values():
            ser_list += [str(x) for x in (lst or [])]
    except Exception:  # noqa: BLE001
        pass
    for _ser, _lab in DEVICES:
        ser_list.append(_ser)
    ips = []
    for s in ser_list:
        s = (s or "").strip()
        if not s:
            continue
        out.append(s)
        ip = s.split(":")[0]
        if ip and ip not in ips:
            ips.append(ip)
    for ip in ips:
        for port in DEV_FALLBACK_PORTS:
            out.append("%s:%d" % (ip, port))
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _persist_found(found, tried=None, errs=None):
    """把认出来的设备写回 state/devices.json 并重绑面板全局（两个发现路径共用）。

    found: {label: {serial,label,model,size}}；只更新认出来的，没认出来的**保留原地址**（不删表）。
    """
    tried = list(tried or [])
    errs = list(errs or [])
    p = os.path.join(STATE_DIR, "devices.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:  # noqa: BLE001
        doc = {"devices": [{"serial": s, "label": l} for s, l in DEVICES]}
    rows = doc.setdefault("devices", [])
    by_label = {str(x.get("label") or ""): x for x in rows if isinstance(x, dict)}
    for lab, info in found.items():
        row = by_label.get(lab)
        if row is None:
            row = {"label": lab}
            rows.append(row)
        row.update(info)
    doc["at"] = datetime.now().isoformat(timespec="seconds")
    hist = doc.setdefault("history", {})
    for lab, info in found.items():
        lst = hist.setdefault(lab, [])
        if info["serial"] in lst:
            lst.remove(info["serial"])
        lst.insert(0, info["serial"])
        del lst[5:]
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    except OSError as e:
        return {"ok": False, "error": "写 devices.json 失败：%s" % e, "tried": tried[:12]}
    reload_devices()
    summary = " · ".join("%s %s" % (lab, found[lab]["serial"]) for lab in sorted(found))
    return {"ok": True, "at": doc["at"], "summary": summary,
            "devices": [found[k] for k in sorted(found)],
            "tried": tried[:12], "errors": errs[:6]}


# ---------------------------------------------------------------- 自动找手机
#: 2026-11-04 用户要求（原话：「端口还只能输一个IP，而且手动输入太麻烦了」）。
#: 安卓「无线调试」的连接端口是**随机高位端口**（Linux ephemeral range），只试 5555 永远找不到，
#: 所以这里扫端口段；手机则靠「热点主机=默认网关」+ ARP 邻居 + ping 扫网段推出来。
#: 实测（Windows / iQOO 热点，两台手机）：13.7 秒、零输入、一次认出两台。
#: 注意：Windows 版 adb 的 mDNS 用不了（adb mdns check 报 daemon unavailable），所以不走 mDNS。
FIND_PORT_LO, FIND_PORT_HI = 32768, 60999
FIND_COMMON_PORTS = (5555, 43059)
FIND_SOCK_THREADS = 900
FIND_SOCK_T = 0.20
FIND_SCAN_BUDGET_S = 180
FIND_MAX_HOSTS = 6          # 最多扫 6 台主机（每台 2.8 万个端口，再多跑不完）
FIND_RETRY_THREADS = 250    # 重试时降并发（本地临时端口池被打满时，高并发全是瞬间失败）
FIND_RETRY2_THREADS = 300   # 补扫「还没认出来那台」时的并发
FIND_RETRY2_SOCK_T = 0.5    # 补扫时单次探测超时放宽：手机在跑任务时 0.2 秒容易超时被漏掉


def _ps(cmd, timeout=20):
    """跑一段 PowerShell（UTF-8 前缀 + 不弹黑窗），返回文本。"""
    ps = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + cmd
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return ""
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return r.stdout.decode("gbk", "replace")


def _find_nets():
    """本机所有可扫的 /24 网段前缀（如 "10.86.111."）；排除回环与 169.254 链路本地。"""
    txt = _ps("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.IPAddress + '/' + $_.PrefixLength }")
    allnets = []
    for line in (txt or "").splitlines():
        line = line.strip()
        if "/" not in line:
            continue
        ip, _, plen = line.partition("/")
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        try:
            n = int(plen)
        except ValueError:
            continue
        if n < 24:
            continue
        base = ip.rsplit(".", 1)[0] + "."
        if base not in allnets:
            allnets.append(base)
    # 只保留**有默认网关**的网段：VMware 的虚拟网卡（192.168.x.1/24）没有网关，
    # 扫它们纯属浪费（每台主机 2.8 万个端口）。一个都没有就退回全部。
    gws = _gateways()
    nets = [n for n in allnets if any(g.startswith(n) for g in gws)]
    return nets or allnets


def _gateways():
    """默认网关（开热点的手机就是它）。排除 169.254 链路本地。"""
    out = []
    for line in (_ps("Get-NetRoute -DestinationPrefix 0.0.0.0/0 | ForEach-Object { $_.NextHop }") or "").splitlines():
        line = line.strip()
        if line and not line.startswith("169.254.") and line not in out:
            out.append(line)
    return out


def _find_hint_ips(nets):
    """网关 + ARP 邻居 = 最可能的手机地址（开热点的手机就是默认网关）。"""
    ips = list(_gateways())
    # Get-NetNeighbor 会把整个 /24 的所有地址都列出来（绝大多数是 Unreachable/Incomplete 占位项）。
    # 不过滤就会去扫 200 多台主机 × 2.8 万个端口 = 永远跑不完，所以只留状态正常的邻居。
    nb = _ps("Get-NetNeighbor -AddressFamily IPv4 | ForEach-Object { $_.IPAddress + ' ' + $_.State }")
    for line in (nb or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ip, state = parts[0], parts[1].lower()
        if state in ("unreachable", "incomplete"):
            continue
        if ip.endswith(".0") or ip.endswith(".255"):     # 网络地址/广播地址，不是主机
            continue
        if any(ip.startswith(b) for b in nets):
            ips.append(ip)
    out = []
    for ip in ips:
        if ip and ip not in out:
            out.append(ip)
    return out


def _sweep_hosts(nets):
    """ping 扫一遍网段把 ARP 表填满（TCP 探端口探不到「只开了随机 adb 端口」的主机）。"""
    import concurrent.futures as _cf

    def ping(ip):
        try:
            subprocess.run(["ping", "-n", "1", "-w", "300", ip], capture_output=True,
                           timeout=3, creationflags=_NO_WINDOW)
        except Exception:  # noqa: BLE001
            pass

    targets = ["%s%d" % (n, i) for n in nets for i in range(1, 255)]
    with _cf.ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(ping, targets))


def _scan_ports(hosts, budget_s=FIND_SCAN_BUDGET_S, threads=None, sock_t=None):
    """在给定主机上扫 ephemeral 端口段，返回 {ip: [port, ...]}。"""
    import queue as _q
    q = _q.Queue()
    for ip in hosts:
        for p in FIND_COMMON_PORTS:
            q.put((ip, p))
        for p in range(FIND_PORT_LO, FIND_PORT_HI + 1):
            q.put((ip, p))
    found, lock, t0 = {}, threading.Lock(), time.time()

    def worker():
        while True:
            if time.time() - t0 > budget_s:
                return
            try:
                ip, port = q.get_nowait()
            except _q.Empty:
                return
            try:
                s = socket.create_connection((ip, port), timeout=(sock_t or FIND_SOCK_T))
                s.close()
                with lock:
                    found.setdefault(ip, []).append(port)
            except OSError:      # 只吞「连不上/超时」；NameError 这类代码错必须暴露（本次事故就是被它吞掉的）
                pass

    ths = [threading.Thread(target=worker, daemon=True)
           for _ in range(int(threads or FIND_SOCK_THREADS))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return found


def _scan_ports_robust(hosts):
    """扫端口，带一次自适应重试。

    2026-11-04 真机踩到：连续扫几次之后 Windows 的本地临时端口池被打满，
    这时每个 connect 都是**瞬间失败**（不是超时），整轮 0.2 秒就「扫完」且一个端口都找不到。
    判据：空结果 + 快得离谱（<8 秒）-> 等 3 秒让端口回收，降并发再扫一次。
    """
    for i, nth in enumerate((FIND_SOCK_THREADS, FIND_RETRY_THREADS)):
        t0 = time.time()
        out = _scan_ports(hosts, threads=nth)
        if out:
            return out
        dt = time.time() - t0
        if dt > 8:            # 扫得慢 = 真的没人应答，别再折腾
            return out
        if i == 0:
            time.sleep(3.0)
    return {}


def _identify_ports(openp, tried, found, errs):
    """对扫到的开放端口逐个 adb connect + 读机型认人。就地更新 tried / found / errs。"""
    for ip, ports in openp.items():
        for port in sorted(ports):
            cand = "%s:%d" % (ip, port)
            if cand in tried:
                continue
            tried.append(cand)
            out, err = _adb_run(["connect", cand], timeout=10)
            if err:
                errs.append("%s: %s" % (cand, err))
                continue
            low = (out or "").lower()
            if "cannot connect" in low or "failed to connect" in low:
                continue
            m_out, _e2 = _adb_run(["-s", cand, "shell", "getprop", "ro.product.model"], timeout=10)
            txt = (m_out or "").strip()
            model = txt.splitlines()[-1].strip() if txt else ""
            label = DEV_MODEL_LABEL.get(model)
            if not label or label in found:
                continue
            size = ""
            s_out, _e3 = _adb_run(["-s", cand, "shell", "wm", "size"], timeout=10)
            if s_out and "Physical size:" in s_out:
                size = s_out[s_out.index("Physical size:"):].splitlines()[0].strip()
            found[label] = {"serial": cand, "label": label, "model": model, "size": size}


def _usb_serials():
    """有线（USB）连接的设备序号。

    2026-11-09 通用版：USB 设备的序号是纯序列号（如 R5CT1234ABC），**没有 ip:端口**，
    所以网络扫描永远找不到它们；自动找手机要先把这一批认出来。
    """
    out, err = _adb_run(["devices"], timeout=10)
    if err:
        return []
    ser = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        if state != "device":
            continue
        if ":" in name:      # ip:port = 无线，交给网络扫描那条路
            continue
        ser.append(name)
    return ser


def _usb_identify(tried, found, errs):
    """认有线设备：读机型 -> 对 DEV_MODEL_LABEL 认人。返回认出来的台数。"""
    for ser in _usb_serials():
        if ser in tried:
            continue
        tried.append(ser)
        m_out, err = _adb_run(["-s", ser, "shell", "getprop", "ro.product.model"], timeout=10)
        if err:
            errs.append("%s: %s" % (ser, err))
            continue
        txt = (m_out or "").strip()
        model = txt.splitlines()[-1].strip() if txt else ""
        label = DEV_MODEL_LABEL.get(model)
        if not label or label in found:
            continue
        size = ""
        s_out, _e = _adb_run(["-s", ser, "shell", "wm", "size"], timeout=10)
        if s_out and "Physical size:" in s_out:
            size = s_out[s_out.index("Physical size:"):].splitlines()[0].strip()
        found[label] = {"serial": ser, "label": label, "model": model, "size": size}
    return len(found)


def find_devices_by_scan():
    """自动找手机：推网段 -> 找活跃主机 -> 扫随机端口 -> adb connect + 读机型认人 -> 写回设备表。"""
    # 2026-11-09 通用版（用户要求「连接手机可以选择有线连接或者无线连接」）：
    # **先认有线设备** —— 同学第一次用通常插着数据线，这时候根本不需要扫网段。
    _t, _f, _e = [], {}, []
    _usb_identify(_t, _f, _e)
    if _f:
        _res = _persist_found(_f, tried=_t, errs=_e)
        _res["source"] = "usb"
        return _res
    nets = _find_nets()
    if not nets:
        return {"ok": False, "error": "没找到可扫的 /24 网段（本机没有这类 IPv4 地址）"}
    hosts = _find_hint_ips(nets)
    if len(hosts) < 2:
        _sweep_hosts(nets)
        hosts = _find_hint_ips(nets)
    hosts = [h for h in hosts if any(h.startswith(n) for n in nets)][:FIND_MAX_HOSTS]
    if not hosts:
        return {"ok": False, "nets": nets, "error": "网段 %s 里没找到任何主机" % "、".join(nets)}
    openp = _scan_ports_robust(hosts)
    if not openp:
        return {"ok": False, "nets": nets, "tried": hosts[:12],
                "error": "主机 %s 上没扫到 adb 端口（手机的「无线调试」打开了么？）" % "、".join(hosts[:6])}
    tried, found, errs = [], {}, []
    _identify_ports(openp, tried, found, errs)
    # 2026-11-07 真机：手机在跑任务时（截图 + 网络都忙），0.2 秒的单次探测会超时被漏掉，
    # 于是只认出手机1（网关永远最先响应）。对**还没认出机型的主机**补扫一次：
    # 超时放宽到 0.5 秒、并发降到 300；只在缺设备时发生，约多花 10 秒。
    if len(found) < 2:
        _used = set(str(_i.get("serial") or "").split(":")[0] for _i in found.values())
        _retry = [h for h in hosts if h not in _used]
        if _retry:
            _op2 = _scan_ports(_retry, threads=FIND_RETRY2_THREADS, sock_t=FIND_RETRY2_SOCK_T)
            if _op2:
                _identify_ports(_op2, tried, found, errs)
    if not found:
        return {"ok": False, "nets": nets, "tried": tried[:12], "errors": errs[:6],
                "error": "扫到开放端口，但一台都没认出机型（%s）" % "、".join(hosts[:6])}
    out = _persist_found(found, tried=tried, errs=errs)
    out["nets"] = nets
    out["hosts"] = hosts[:12]
    return out


def discover_devices_now(extra=None):
    """重新发现两台手机并写回 state/devices.json（面板【刷新设备地址】调用的）。

    手机重启后「无线调试」端口会变（5555 -> 随机端口），所以：
      1) 先 adb mdns services 拿它当前广播的 ip:port；
      2) 再退回历史地址、以及已知 IP 的老端口逐个 adb connect + 读机型认人。
    认出几台更新几台；没认出来的**保留原地址**（不删表）。
    """
    if os.environ.get("COIN11_DEVICE_AUTO") == "1":
        rows = _attached_devices()
        if not rows:
            return {"ok": False,
                    "error": "没看到手机：数据线插好了么？手机上「USB 调试」打开了么？"
                             "（第一次插会弹授权框，点「允许」）"}
        return _persist_found({r["label"]: dict(r, size="") for r in rows})
    tried, found, errs = [], {}, []
    cands = []
    for c in ([extra] if extra else []) + _dev_all_candidates():
        if c and c not in cands:
            cands.append(c)
    for cand in cands:
        tried.append(cand)
        out, err = _adb_run(["connect", cand], timeout=10)
        if err:
            errs.append("%s: %s" % (cand, err))
            continue
        low = (out or "").lower()
        if "cannot connect" in low or "failed to connect" in low:
            continue
        m_out, m_err = _adb_run(["-s", cand, "shell", "getprop", "ro.product.model"], timeout=10)
        if m_err:
            errs.append("%s: %s" % (cand, m_err))
            continue
        txt = (m_out or "").strip()
        model = txt.splitlines()[-1].strip() if txt else ""
        label = DEV_MODEL_LABEL.get(model)
        if not label or label in found:
            continue
        size = ""
        s_out, _e = _adb_run(["-s", cand, "shell", "wm", "size"], timeout=10)
        if s_out and "Physical size:" in s_out:
            tail = s_out[s_out.index("Physical size:"):]
            size = tail.splitlines()[0].strip()
        found[label] = {"serial": cand, "label": label, "model": model, "size": size}
        if len(found) >= 2:
            break

    if not found:
        return {"ok": False, "error": "一台都没认出来（手机上的「无线调试」打开了么？）",
                "tried": tried[:12], "errors": errs[:6]}

    return _persist_found(found, tried=tried, errs=errs)


DEVICES = _load_devices()
DEVICE_LABELS = dict(DEVICES)
DEFAULT_SERIAL = DEVICES[0][0]
TEACH_PORTS = {ser: TEACH_PORTS_BY_LABEL.get(lab, 8777) for ser, lab in DEVICES}
KEEP_INTERVALS = (2.0, 4.0, 6.0)
KEEP_DEFAULT_MIN = 2.0      # 2026-11-02：默认 2 分钟（一次 224 只要 0.2s，几乎没有成本）
#: 保活动作（2026-11-02 用户定案）：**input keyevent 224（KEYCODE_WAKEUP）**。
#:
#: 走过的三个坑，一次说清：
#:   1) 最早的「屏幕中间 8 像素 swipe」：注入触摸，会在可滚动列表上真的滚一点，
#:      而且 auto.py 正在 OCR/滑动时插一脚会让页面错位；
#:   2) 换成 `cmd statusbar expand/collapse`（系统服务）：不碰页面了，但**系统不认它是用户活动**，
#:      不会重置息屏计时器 —— 真机实测保活每 2 分钟都"成功执行"，手机照样按 10 分钟准时黑屏；
#:   3) 现在这条：`input keyevent 224` 走**输入管道**，系统算它是用户活动，会重置息屏计时器；
#:      同时它只"点亮"，**不点击、不滑动、不改任何界面**，也绝不会像 26 那样把亮屏按灭。
#: 实测（手机1，未插电，超时故意设成 20 秒）：每 8 秒发一次 224，**50 秒后仍然 Awake**。
KEEP_ACTIVITY_CMD = ("input", "keyevent", "224")
#: 默认间隔改 2 分钟：熄屏超时最短也是 2 分钟起，2 分钟一次既稳妥又几乎无成本（一次 0.2s）。
KEEP_DEFAULT_MIN = 2.0
#: 保活开启期间把「熄屏时间」临时拉长，关掉时**还原成你原来的值**（双保险）。
KEEP_SCREEN_ON_MS = 30 * 60 * 1000
_KEEP_SCREEN_ORIG = {}                          # serial -> 开启前的 screen_off_timeout（毫秒串）


def _keep_screen_guard(serial, on):
    """保活开启 -> 临时拉长熄屏时间 + 插电时保持亮屏；关闭 -> 全部还原（绝不永久改你的设置）。

    为什么两条都要（2026-11-02 真机）：
      - 只改 screen_off_timeout 不够：vivo 实测把超时设成 30 分钟后，手机仍在 5 分钟内
        `mLastSleepReason=timeout` 睡了（系统还有自己的智能息屏策略在管事）；
      - `svc power stayon true` 是系统级的「插电保持亮屏」，只对插着充电的手机生效，
        但它最硬 —— 手机2 当时正插着充电器（AC powered=true），这条能真正压住息屏。
    """
    try:
        if on:
            cur, _ = _adb(serial, ["shell", "settings", "get", "system", "screen_off_timeout"], timeout=20)
            cur = (cur or "").strip()
            if cur.lstrip("-").isdigit():
                _KEEP_SCREEN_ORIG.setdefault(serial, cur)
            _adb(serial, ["shell", "settings", "put", "system", "screen_off_timeout",
                          str(KEEP_SCREEN_ON_MS)], timeout=20)
            _keep_note(serial, "[keep] 熄屏时间临时改为 %d 分钟（原值 %s，关掉保活会还原）"
                       % (KEEP_SCREEN_ON_MS // 60000, _KEEP_SCREEN_ORIG.get(serial, "未知")))
            # 插电时保持亮屏（没插电的系统会忽略，不影响）
            _adb(serial, ["shell", "svc", "power", "stayon", "true"], timeout=20)
            _keep_note(serial, "[keep] 已开 svc power stayon true（插电时保持亮屏）")
        else:
            _adb(serial, ["shell", "svc", "power", "stayon", "false"], timeout=20)
            orig = _KEEP_SCREEN_ORIG.pop(serial, None)
            if orig is not None:
                _adb(serial, ["shell", "settings", "put", "system", "screen_off_timeout", orig], timeout=20)
                _keep_note(serial, "[keep] 熄屏时间已还原为 %s 毫秒、stayon 已关（回到你原来的设置）" % orig)
    except Exception as exc:  # noqa: BLE001
        _keep_note(serial, "[keep] 改熄屏时间失败（不影响保活本身）：%s" % exc)
#: 兼容：老配置/别处若还引用 KEEP_SWIPE，保留旧值（新保活不再用它）
#: 铁律不变：永不发 keyevent 26 / 电源键（曾把亮屏按灭）
KEEP_SWIPE = (720, 1560, 720, 1552, 60)
LIVE_STALE_S = 45.0
AUTO_PROC_CACHE_S = 3.0


# ---------------------------------------------------------------- 只读采集

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return None


def _dt(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d_%H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _hms(s):
    d = _dt(s)
    return d.strftime("%H:%M:%S") if d else (str(s or "")[-8:] or "-")


def current_serial():
    """当前「主」设备：.phone_serial 里记的那台。

    2026-11-03 修：这个文件是单设备时代的遗留物，换网络后没人刷新它 ——
    真机实测里面还躺着 192.168.1.8:5555，于是 /api/state 的 session.serial 指向一台
    不存在的手机、runs 里多出一台「幽灵设备」。以 devices.json（discover_devices.py 的
    产物）为准：记着的地址不在设备表里，就退回设备表第一台。
    """
    try:
        with open(SERIAL_FILE, "r", encoding="utf-8-sig") as fh:
            s = fh.read().strip()
    except Exception:
        s = ""
    known = [x[0] for x in DEVICES]
    if s and s in known:
        return s
    if known:
        return known[0]
    return s or DEFAULT_SERIAL


def teach_url(serial):
    return "http://127.0.0.1:%d" % TEACH_PORTS.get((serial or "").strip(), 8777)


# ---------------------------------------------------------------- 每台设备一套文件
# 2026-11-02 用户要求（两台手机同时各跑一轮）：
#   每台一个证据目录 evidence/<日期>-auto-<设备名>、一个 live-<san>.json、一个 pause-<san>.flag；
#   只有 state/pause.flag 保留成「全部暂停」。
# <san> 有两种写法，两种都认（读哪个都能读到，写就两个都写）：
#   1) 面板自己的 _san()：':'→'_'、'.'→'-' → 192-168-1-8_5555
#   2) 「':' 和 '.' 都换成 '-'」→ 192-168-1-8-5555
#      —— 这是 auto.py 的 _dev_san() 的写法，也就是 auto.py 真正读/写的那一份。
# 用户 2026-11-02 的口径写的是「':'/'.' 都换成 '-'」；面板多处已有 _san()（文件名、
# 投屏帧名）沿用不改，于是两种都支持：谁按哪种写，另一头都能读。

def dev_label(serial):
    s = str(serial or "").strip()
    return DEVICE_LABELS.get(s) or _san(s)


def _san_variants(serial):
    s = str(serial or "").strip()
    a = _san(s)
    b = s.replace(":", "-").replace(".", "-")
    return [a] if a == b else [a, b]


def pause_paths(serial):
    """这台设备的暂停标志（第 0 个是主写法，dash 两个都写，auto.py 读哪个都行）。"""
    return [os.path.join(STATE_DIR, "pause-%s.flag" % v) for v in _san_variants(serial)]


def live_paths(serial):
    """这台设备的 live.json：设备自己的那份优先，老的全局 state/live.json 兜底。"""
    return ([os.path.join(STATE_DIR, "live-%s.json" % v) for v in _san_variants(serial)]
            + [LIVE_JSON])


def out_dir(serial):
    """这台设备的证据目录：evidence/<日期>-auto-<设备名>（各写各的 REPORT，互不覆盖）。"""
    return os.path.join(EVIDENCE, "%s-auto-%s" % (_today(), dev_label(serial)))


_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def session_dirs():
    """evidence/ 下所有 *-auto* 目录（含 -手机1 / -手机2 后缀），旧的在前。

    2026-11-02 用户要求（多机并发）：每台设备各写一个证据目录
    （evidence/<日期>-auto-手机1 / -手机2），面板必须把两边的轮次**合并**显示，
    所以这里返回全部，而不是「最新一个」。
    2026-11-02 用户要求（第 7 条）：每次调用都重新 listdir，结果不许存成模块级常量，
    否则面板会冻结在启动那一刻的目录上。排序先按目录名里的日期，再按 mtime，
    免得「名字碰巧更大」的旧目录压住新目录。
    """
    try:
        names = [n for n in os.listdir(EVIDENCE)
                 if "-auto" in n and os.path.isdir(os.path.join(EVIDENCE, n))]
    except OSError:
        return []
    if not names:
        return []

    def key(n):
        m = _DATE_PREFIX.match(n)
        try:
            mt = os.path.getmtime(os.path.join(EVIDENCE, n))
        except OSError:
            mt = 0
        return ((m.group(1) if m else ""), mt, n)

    return [os.path.join(EVIDENCE, n) for n in sorted(names, key=key)]


def _dir_stats(path):
    """一个证据目录：轮次数、占用字节、最后写入时间戳。"""
    rounds = 0
    rdir = os.path.join(path, "rounds")
    if os.path.isdir(rdir):
        try:
            rounds = len([n for n in os.listdir(rdir) if os.path.isdir(os.path.join(rdir, n))])
        except OSError:
            rounds = 0
    nbytes = 0
    newest = 0.0
    for cur, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(cur, f))
            except OSError:
                continue
            nbytes += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
    return rounds, nbytes, newest


def _running_evidence_dirs():
    """正在跑的那几台设备各自的证据目录（规范化的绝对路径）—— 删记录时不许动它们。

    判活一律走 _run_snapshot：它先 _reap_locked() 收尸，再以 proc 句柄为准。
    别自己去看 st["pid"] —— 进程退出后 _reap_locked 只清 proc 不清 pid，
    照 pid 判会把「刚跑完」的目录一直当成「正在跑」，那个目录就永远删不掉。
    """
    out = set()
    for serial, _label in DEVICES:
        try:
            snap = _run_snapshot(serial)
        except Exception:  # noqa: BLE001
            continue
        if not snap.get("running"):
            continue
        d = snap.get("out") or out_dir(serial)
        if d:
            out.add(os.path.normcase(os.path.abspath(d)))
    return out


def records_overview():
    """GET /api/records：【记录管理】页的清单（只读，不动任何文件）。"""
    running = _running_evidence_dirs()
    dirs = []
    now = time.time()
    for d in session_dirs():
        rounds, nbytes, newest = _dir_stats(d)
        dirs.append({
            "name": os.path.basename(d),
            "rounds": rounds,
            "bytes": nbytes,
            "mtime": datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M") if newest else "-",
            "age_days": round((now - newest) / 86400.0, 2) if newest else None,
            "running": os.path.normcase(os.path.abspath(d)) in running,
        })
    dirs.sort(key=lambda x: x["name"])
    return {"ok": True, "root": _rel(EVIDENCE), "dirs": dirs,
            "total_bytes": sum(x["bytes"] for x in dirs)}


def records_delete(names=None, keep_days=None, dry_run=False):
    """POST /api/records_delete：删整轮的证据目录（截图 / OCR / 判定都在里面）。

    三条安全边界，缺一不可：
      1) 只删 EVIDENCE 的**直接子目录**、名字必须命中 *-auto*（和 session_dirs 同一套判定）；
      2) 正在跑的那台设备的目录不删（别把正在写的证据抽掉）；
      3) keep_days 只对「最后写入时间」早于 N 天的目录生效，宁少不多。
    返回 (dict, http_code)。
    """
    running = _running_evidence_dirs()
    all_dirs = session_dirs()
    targets = []
    if names:
        want = set(str(n).strip() for n in names if str(n).strip())
        targets = [d for d in all_dirs if os.path.basename(d) in want]
    elif keep_days is not None:
        try:
            days = float(keep_days)
        except (TypeError, ValueError):
            return {"ok": False, "error": "keep_days 要是数字"}, 400
        if days < 0:
            return {"ok": False, "error": "keep_days 不能是负数"}, 400
        now = time.time()
        for d in all_dirs:
            _r, _b, newest = _dir_stats(d)
            age_days = (now - newest) / 86400.0 if newest else 1e9
            if age_days >= days:
                targets.append(d)
    else:
        return {"ok": False, "error": "要么给 dirs（选中的目录），要么给 keep_days（只保留最近 N 天）"}, 400

    deleted, skipped, freed = [], [], 0
    root = os.path.abspath(EVIDENCE)
    for d in targets:
        name = os.path.basename(d)
        real = os.path.abspath(d)
        if os.path.dirname(real) != root or "-auto" not in name:
            skipped.append({"name": name, "why": "不在证据根下 / 名字不含 -auto，不删"})
            continue
        if os.path.normcase(real) in running:
            skipped.append({"name": name, "why": "这台正在跑，先【停止本台】再删"})
            continue
        _r, nbytes, _n = _dir_stats(d)
        if not dry_run:
            try:
                shutil.rmtree(real)
            except OSError as exc:
                skipped.append({"name": name, "why": "删不掉：%s" % exc})
                continue
        deleted.append({"name": name, "bytes": nbytes})
        freed += nbytes
    out = {"ok": True, "dry_run": bool(dry_run), "deleted": deleted, "skipped": skipped,
           "freed_bytes": freed}
    out.update(records_overview())
    out["deleted"] = deleted
    out["skipped"] = skipped
    out["dry_run"] = bool(dry_run)
    return out, 200


def session_dir():
    """evidence/ 下最新的 *-auto* 目录（老接口，保留：取 session_dirs() 的最后一个）。

    单设备看证据 / tail_log / 老调用点仍然用它；多设备请用 session_dirs()。
    """
    dirs = session_dirs()
    return dirs[-1] if dirs else None


def _count(n, m):
    if n is None and m is None:
        return None
    return "%s/%s" % ("?" if n is None else n, "?" if m is None else m)


def _pair(v, key):
    lst = v.get(key)
    if isinstance(lst, list) and len(lst) >= 2:
        return _count(lst[0], lst[1])
    return None


# 2026-11-02 用户要求（第 7 条）：「最近轮次」不更新。
# 真因：下面 rows.sort 用的是 str(round) —— '9' > '35'，第 10 轮以后永远排在单个数字
# 轮次后面，页面 slice(0, 8) 拿到的一直是 round-9 那一屏。改按轮次数字倒序，并给出
# 数据来源（sdir 绝对路径）与最后更新时间（最新一个 06_verdict.json 的 mtime）。
_ROUNDS_LOCK = threading.Lock()
_ROUNDS_CACHE = {"sig": None, "rows": [], "meta": {}}
_ROUNDS_NEWEST_STAT = 3    # 轻量签名只 stat 最新 3 个轮次目录，绝不每轮重读全部 verdict


def _round_num(*vals):
    """'round-035' / 35 / '35' → 35；解析不到给 -1（排序垫底）。"""
    for v in vals:
        s = str(v if v is not None else "").strip()
        m = re.search(r"(\d+)\s*$", s)
        if m:
            return int(m.group(1))
    return -1


def _rounds_sig(rdir, names):
    """轻量签名：目录名集合 + rounds/ 的 mtime + 最新 3 个轮次目录（含 verdict mtime）。

    新轮次目录出现、或最近几轮的 verdict/mtime/大小变了 → 签名变，立刻重扫；
    否则复用上一次解析结果（磁盘上只做几次 stat，不递归读所有 verdict）。
    """
    stats = []
    for name in names[-_ROUNDS_NEWEST_STAT:]:
        d = os.path.join(rdir, name)
        try:
            dmt = round(os.path.getmtime(d), 3)
        except OSError:
            dmt = 0.0
        try:
            st = os.stat(os.path.join(d, "06_verdict.json"))
            vmt, vsz = round(st.st_mtime, 3), st.st_size
        except OSError:
            vmt, vsz = -1.0, -1
        stats.append((name, dmt, vmt, vsz))
    try:
        rmt = round(os.path.getmtime(rdir), 3)
    except OSError:
        rmt = 0.0
    return (rdir, tuple(names), rmt, tuple(stats))


def _newest_verdict_mtime(rdir, names):
    """最新一个 06_verdict.json 的 mtime（只 stat，不读内容）。"""
    for name in reversed(names):
        try:
            return os.path.getmtime(os.path.join(rdir, name, "06_verdict.json"))
        except OSError:
            continue
    return None


_NO_TASK_LABEL = {
    "no-doable": "（本页没有可做行）",
    "no-panel": "（面板不在前台）",
    "aborted": "（进程中断，本轮没做完）",
    "stoprun": "（被停手中断）",
    "guard-violation": "（守卫拦截，没点）",
    "exception": "（异常，本轮没做完）",
    "foreign-app": "（落到别的 App 被退出）",
    "system-dialog": "（系统弹窗拦住）",
    "alipay-popup": "（支付宝弹窗，已退回）",
    "search-not-submitted": "（搜一搜关键词没提交上）",
}


def _no_task_label(status):
    """没选中任务的轮次（如 no-doable 的探测轮）不该显示成「未知任务」。

    2026-11-02 用户提问：「为什么这么多未知任务」—— 那些轮次本来就只是「滚一屏看看还有没有
    可做行」的探测轮，没有任务可做，而不是任务名丢了。按判定给出人话说明。
    """
    return _NO_TASK_LABEL.get((status or "").strip().lower(), "（本轮没选中任务）")


def _as_dirs(sdir):
    """session_dir() 给一个目录，session_dirs() 给一串；两种都吃。"""
    if not sdir:
        return []
    if isinstance(sdir, (list, tuple)):
        return [d for d in sdir if d]
    return [sdir]


def _rows_by_time(rows):
    """按设备时间倒序排（2026-11-02 用户要求）。

    多机并发时两个证据目录里都有 round-001，按 round 数字跨目录排一定会串号；
    一律用 device_time_start/end 解析出来的 _start 排，没有时间的垫底。
    """
    rows.sort(key=lambda r: (r.get("_start") or datetime.min,
                             str(r.get("dir") or ""),
                             str(r.get("round_dir") or "")), reverse=True)
    return rows


def _parse_rounds(rdir, names, src=""):
    """解析一个证据目录的 rounds/。每行带 dir（来源目录）+ round_dir（轮次目录名）+ device。"""
    rows = []
    for name in names:
        v = _read_json(os.path.join(rdir, name, "06_verdict.json"))
        if not isinstance(v, dict):
            continue
        picked = v.get("picked") or {}
        what = (v.get("what") or "").strip().lower()
        status = (v.get("status") or "").strip().lower()
        if not status:
            status = "advanced" if v.get("ok") else "failed"
        reason = v.get("reason") or ""
        rows.append({
            "round": v.get("round") or name,
            "time": _hms(v.get("device_time_end") or v.get("device_time_start")),
            "time_full": v.get("device_time_end") or v.get("device_time_start") or "",
            "panel": what,
            "panel_label": dict(PANELS).get(what, what or "?"),
            "task": (picked.get("name") or picked.get("key")
                     or _no_task_label(status)),
            "before": _count(picked.get("n"), picked.get("m")) or _count(v.get("progress_before"), None),
            "after": _pair(v, "row_after") or _count(v.get("progress_after"), None),
            "status": status,
            "duration_s": v.get("duration_s"),
            "reason": reason,
            "reason_short": (reason[:160] + "…") if len(reason) > 160 else reason,
            "coin": [v.get("coin_before"), v.get("coin_after")],
            "device": v.get("device") or "",
            "dir": src,            # 来源证据目录（evidence/<日期>-auto-手机1 / -手机2）
            "round_dir": name,     # rounds/round-0NN（两个目录会同名，所以不能当来源用）
            "_start": _dt(v.get("device_time_start")),
            "_end": _dt(v.get("device_time_end")),
        })
    return rows


def collect_rounds(sdir, meta=None):
    """轮次行：多个证据目录合并后按设备时间倒序（最新在前）。meta 回填来源与更新时间。

    sdir 可以是 session_dir() 的一个目录，也可以是 session_dirs() 的一串目录 —— 后者就是
    2026-11-02 用户要求的「两台手机的轮次合并显示」：行数 = 各目录之和，每行带自己的
    dir/device，排序只看时间，不看 round 数字（两个目录都有 round-001，按数字排会串号）。
    """
    dirs = _as_dirs(sdir)
    if meta is not None:
        meta.clear()
        meta.update({"dir": None, "dirs": [], "updated": None, "updated_mtime": None,
                     "count": 0, "round_dirs": 0, "by_dir": {}})
    if not dirs:
        return []
    per = []
    for d in dirs:
        rdir = os.path.join(d, "rounds")
        try:
            names = sorted(n for n in os.listdir(rdir) if n.startswith("round-"))
        except OSError:
            names = []
        per.append((d, rdir, names))
    sig = tuple(_rounds_sig(rdir, names) for _d, rdir, names in per)
    with _ROUNDS_LOCK:
        cached = dict(_ROUNDS_CACHE["meta"]) if sig == _ROUNDS_CACHE["sig"] else None
        if cached is not None:
            rows = [dict(r) for r in _ROUNDS_CACHE["rows"]]
    if cached is None:
        rows, by_dir, vmts = [], {}, []
        for d, rdir, names in per:
            base = os.path.basename(d)
            one = _parse_rounds(rdir, names, base) if names else []
            rows += one
            by_dir[base] = len(one)
            mt = _newest_verdict_mtime(rdir, names) if names else None
            if mt:
                vmts.append(mt)
        _rows_by_time(rows)
        vmt = max(vmts) if vmts else None
        cached = {
            "dir": (os.path.relpath(dirs[-1], ROOT) if len(dirs) == 1
                    else " + ".join(os.path.basename(d) for d in dirs)),
            "dirs": [os.path.basename(d) for d in dirs],
            "updated": (datetime.fromtimestamp(vmt).strftime("%Y-%m-%d %H:%M:%S")
                        if vmt else None),
            "updated_mtime": vmt,
            "count": len(rows),
            "round_dirs": sum(len(n) for _d, _r, n in per),
            "by_dir": by_dir,
        }
        with _ROUNDS_LOCK:
            _ROUNDS_CACHE.update({"sig": sig, "rows": [dict(r) for r in rows],
                                  "meta": dict(cached)})
    if meta is not None:
        meta.update(cached)
    return rows


def collect_scans(sdir):
    """每个面板取最新一次全表扫描：progress 就是「累计进度 x/50」。

    多设备（2026-11-02）：按目录（= 每台设备）分别扫，同一个面板只取**时间最新**的那一次，
    绝不把两台的可做行 / 进度加在一起（加一起进度会假装翻倍）。
    每台各自的行数、可做数留在 by_dir 里，想分开看有数据。
    """
    out = {}
    by_dir = {}
    for d in _as_dirs(sdir):
        base = os.path.basename(d)
        sdir2 = os.path.join(d, "scans")
        try:
            names = [n for n in os.listdir(sdir2) if n.lower().endswith(".json")]
        except OSError:
            continue
        best = {}
        for n in names:
            p = os.path.join(sdir2, n)
            d2 = _read_json(p)
            if not isinstance(d2, dict):
                continue
            what = (d2.get("what") or ("coin" if n.startswith("coin") else "race")).strip().lower()
            t = _dt(d2.get("started"))
            if t is None:
                try:
                    t = datetime.fromtimestamp(os.path.getmtime(p))
                except OSError:
                    t = datetime(1970, 1, 1)
            cur = best.get(what)
            if cur is None or t > cur[0]:
                best[what] = (t, d2, n)
        for what, (t, d2, n) in best.items():
            by_dir.setdefault(base, {})[what] = {
                "progress": d2.get("progress"),
                "rows": len(d2.get("rows") or []),
                "doable": len(d2.get("doable") or []),
            }
            cur = out.get(what)
            if cur is not None and (cur.get("_t") or datetime(1970, 1, 1)) >= t:
                continue
            out[what] = {
                "progress": d2.get("progress"),
                "coin_total": d2.get("coin_total"),
                "at": t.strftime("%m-%d %H:%M"),
                "file": n,
                "dir": base,
                "rows": len(d2.get("rows") or []),
                "doable": len(d2.get("doable") or []),
                "_t": t,
            }
    for what in list(out):
        out[what].pop("_t", None)
        out[what]["by_dir"] = by_dir
    return out


def collect_pending():
    pdir = os.path.join(COIN, "targets", "pending")
    items = []
    try:
        names = [n for n in os.listdir(pdir) if n.lower().endswith(".json")]
    except OSError:
        return items
    for n in names:
        d = _read_json(os.path.join(pdir, n))
        if not isinstance(d, dict):
            continue
        shot = d.get("screenshot_path") or ""
        if not shot:
            name = d.get("screenshot") or os.path.splitext(n)[0] + ".png"
            shot = os.path.join(pdir, name)
        state = (d.get("state") or "pending").strip().lower()
        items.append({
            "ts": d.get("ts") or os.path.splitext(n)[0],
            "created_at": d.get("created_at") or "",
            "question": d.get("question") or "(没有问题文本)",
            "state": state,
            "answered": state == "answered",
            "kind": d.get("kind") or "",
            "serial": d.get("serial") or current_serial(),
            "teach_url": teach_url(d.get("serial") or current_serial()),
            "shot_url": ("/shot?p=" + urllib.parse.quote(shot)) if os.path.exists(shot) else None,
            "file": n,
        })
    items.sort(key=lambda x: (x["created_at"] or "", x["ts"]), reverse=True)
    return items


def collect_navfail(sdir):
    """导航失败记录：多个证据目录合并（每台各自写自己那份）。"""
    items = []
    for sdir1 in _as_dirs(sdir):
        items += _collect_navfail_one(sdir1)
    items.sort(key=lambda x: x["at"], reverse=True)
    return items


def _collect_navfail_one(sdir):
    items = []
    ndir = os.path.join(sdir, "navfail")
    try:
        names = [n for n in os.listdir(ndir) if n.lower().endswith(".json")]
    except OSError:
        return items
    for n in names:
        d = _read_json(os.path.join(ndir, n))
        if not isinstance(d, dict):
            continue
        tries = d.get("tries") or {}
        steps = "、".join("%s ×%s" % (k, v) for k, v in tries.items())
        if not steps:
            steps = "%s 次" % (d.get("step_tries") or "?")
        frame = d.get("frame") or (os.path.splitext(n)[0] + ".png")
        shot = os.path.join(ndir, frame)
        if not os.path.exists(shot):
            shot = os.path.join(ndir, os.path.splitext(n)[0] + ".png")
        items.append({
            "at": d.get("at") or "",
            "panel_label": dict(PANELS).get((d.get("what") or "").strip().lower(), d.get("what") or "?"),
            "verdict": d.get("verdict") or "导航失败",
            "steps": steps,
            "step_tries": d.get("step_tries"),
            "device": d.get("device") or "",
            "teach_url": teach_url(d.get("device")),
            "shot_url": ("/shot?p=" + urllib.parse.quote(shot)) if os.path.exists(shot) else None,
            "file": n,
            "dir": os.path.basename(sdir),
        })
    return items


#: 2026-11-04 用户要求（SSE 500ms 一帧）：日志尾巴加一层进程内缓存 ——
#: (path, mtime_ns, size) 没变就**不碰磁盘**，直接返回上次那几行；变了才重读尾部。
#: 两台手机 × 2 帧/秒 = 每秒 4 次读；没有这层缓存，空闲时也一直在读盘。
_TAIL_CACHE = {}
_TAIL_LOCK = threading.Lock()
_TAIL_CACHE_MAX = 64


def _tail_lines(path, n=120, max_bytes=200000):
    """只读文件**尾部**（run.log 会长到几 MB，每次轮询全读会把 /api/state 拖垮 —
    2026-11-03 实测：两台各读一个上兆的 run.log，轮询超时、页面拿不到状态）。

    2026-11-04：加了 (mtime_ns, size) 缓存。返回的是缓存里的**同一个 list 对象**，
    调用方只读（拼接 JSON / 切片），别原地改它。
    """
    key = (path, n, max_bytes)
    try:
        stt = os.stat(path)
    except OSError:
        with _TAIL_LOCK:
            _TAIL_CACHE.pop(key, None)
        return []
    sig = (stt.st_mtime_ns, stt.st_size)
    with _TAIL_LOCK:
        hit = _TAIL_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            if sig[1] > max_bytes:
                fh.seek(sig[1] - max_bytes)
                fh.readline()          # 丢掉被截断的半行
            blob = fh.read()
        lines = blob.decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        with _TAIL_LOCK:
            _TAIL_CACHE.pop(key, None)
        return []
    with _TAIL_LOCK:
        if len(_TAIL_CACHE) >= _TAIL_CACHE_MAX:
            _TAIL_CACHE.pop(next(iter(_TAIL_CACHE)), None)   # 最早的先丢（够用了）
        _TAIL_CACHE[key] = (sig, lines)
    return lines


def tail_log(sdir, n=80):
    """run.log 尾巴：多个证据目录时取最新那个（每台各写自己的 run.log）。"""
    dirs = _as_dirs(sdir)
    if not dirs:
        return []
    try:
        with open(os.path.join(dirs[-1], "run.log"), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-n:]
    except OSError:
        return []


# ---------------------------------------------------------------- 运行控制

_STATS = {"state_hits": 0, "stream_clients": 0}
_LOCK = threading.Lock()
#: 2026-11-02 用户要求：**每台设备各自一个进程** —— 所以这里是 serial -> 运行状态，
#: 不再是单例。同一台重复启动才 409；另一台可以同时起（两台手机并行，互不打断）。
_RUNS = {}


class _AdoptedProc:
    """接管 dash 启动**之前**就已经在跑的 auto.py 进程。

    只实现 poll()（_reap_locked 需要），liveness 用 auto_procs() 的结果（3 秒缓存）。
    2026-11-03：不接管的话，面板一重启就把在途 run 当"空闲"，再点一次就在同一台起第二个进程。
    """

    def __init__(self, pid):
        try:
            self.pid = int(pid)
        except (TypeError, ValueError):
            self.pid = 0
        self._code = None

    def poll(self):
        """活着 = 进程还在 auto_procs() 里；但**这一帧不许 spawn 任何东西**。

        2026-11-04：auto_procs() 缓存过期时会 spawn 一个 powershell（实测 0.3~2s）。
        原来 poll() 直接调它，于是「有一轮是接管来的」时，SSE 每 500ms 就可能被拖到 1~2s
        （实测出现过 1915ms 的帧延迟）。现在只在缓存**还新鲜**时下结论，缓存过期就先当它活着，
        刷新交给 adopt 线程（5 秒一次）和 /api/state 的轮询 —— 推送链路永远不碰外部进程。
        """
        if self._code is not None or not self.pid:
            return self._code
        rows = _procs_fresh()
        if rows is None:
            return None            # 缓存过期：这一帧不下结论（别为了它去 spawn powershell）
        try:
            alive = any(int(r.get("pid") or 0) == self.pid for r in rows)
        except Exception:  # noqa: BLE001
            alive = True
        if not alive:
            self._code = 0
        return self._code

    def terminate(self):
        """接管来的进程没有 Popen 句柄，只能按 pid 杀（stop_run 自己会发 taskkill，这里兜底）。"""
        if not self.pid:
            return
        try:
            subprocess.run(["taskkill", "/PID", str(self.pid), "/T", "/F"],
                           capture_output=True, timeout=20, creationflags=_NO_WINDOW)
        except Exception:  # noqa: BLE001
            pass

    def wait(self, timeout=None):
        """stop_run 会 wait()：接管来的进程没法真等，量一下 taskkill 有没有落地就返回。"""
        time.sleep(min(0.3, float(timeout or 0.3)))
        return self._code


def _guess_out_dir(serial, max_age_s=120.0):
    """命令行**没带 --out** 时，认出这轮 auto.py 实际写到哪个证据目录。

    2026-11-04 真机实测（用户踩的同一个坑）：别的会话用命令行起 auto.py 时通常不给 --out，
    于是 auto.py 写的是它自己的默认目录 evidence/<日期>-auto（不带设备名）；dash 按设备算的
    out_dir() = evidence/<日期>-auto-手机N 那里**根本没有 run.log** —— 认领了、状态是「运行中」，
    日志却一直是「还没有」，用户看到的就是「那台没日志」。

    认法：evidence/*-auto* 里 run.log 在最近 max_age_s 秒内还在写、且内容里出现过
    serial=<这台> 的那个目录（取 mtime 最新的）。只在**认领那一刻**跑一次，不进轮询。
    """
    ser = str(serial or "").strip()
    if not ser:
        return None
    now = time.time()
    best, best_mt = None, -1.0
    for d in reversed(session_dirs()[-8:]):          # 最新的先看，最多看 8 个
        p = os.path.join(d, "run.log")
        try:
            stt = os.stat(p)
        except OSError:
            continue
        if now - stt.st_mtime > max_age_s:
            continue                                  # 早就不写了的目录，不是这一轮
        try:
            with open(p, "rb") as fh:
                if stt.st_size > 65536:
                    fh.seek(stt.st_size - 65536)
                    fh.readline()
                blob = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        if ("serial=%s" % ser) in blob and stt.st_mtime > best_mt:
            best, best_mt = d, stt.st_mtime
    return best


def adopt_existing_runs(log=print):
    """dash 启动时把已经在跑的 auto.py 认领回来（按 --serial 归到各自设备）。"""
    n = 0
    try:
        procs = auto_procs()
    except Exception:  # noqa: BLE001
        return 0
    for row in procs:
        cmd = str(row.get("cmd") or "")
        ms = re.search(r"--serial\s+(\S+)", cmd)
        serial = (ms.group(1) if ms else "").strip()
        if not serial or serial not in DEVICE_LABELS:
            continue
        mw = re.search(r"--what\s+(\S+)", cmd)
        mm = re.search(r"--minutes\s+([\d.]+)", cmd)
        mo = re.search(r"--out\s+(\S+)", cmd)
        out = mo.group(1).strip('"') if mo else None
        # 命令行没 --out（命令行手起的那种）：从证据目录反查它写到哪，否则面板一直是「还没有日志」
        if not out:
            out = _guess_out_dir(serial)
        with _LOCK:
            st = _run_of(serial)
            if st.get("proc") is not None and st["proc"].poll() is None:
                continue
            # started/ended/code 都重置：认领的是**新的一轮**，继承上一轮的时间会让
            # 面板显示「已跑 15 分钟」这种离谱数字（接管变成周期性之后这条尤其要紧）。
            st.update(proc=_AdoptedProc(row.get("pid")), pid=row.get("pid"),
                      what=(mw.group(1) if mw else "?"),
                      minutes=(float(mm.group(1)) if mm else None),
                      started=datetime.now(), ended=None, code=None,
                      out=out)
            st["lines"].append("[dash] 接管已在跑的进程 pid=%s（--what %s），不重复启动"
                               % (row.get("pid"), st["what"]))
            if out:
                st["lines"].append("[dash] 这轮的日志目录：%s" % out)
        n += 1
    if n:
        log("[dash] 已接管 %d 个在跑的 auto.py 进程（不会把它们当成空闲）" % n)
    return n


def _adopt_loop():
    """后台线程：dash **已经跑着**的时候，也要认领别人新起的 auto.py（每 ADOPT_EVERY_S 一次）。

    2026-11-04 用户要求（用户实际踩到的坑）：adopt_existing_runs() 原来只在启动时调一次，
    于是 dash 跑着的时候，别的会话 / 手敲命令起的 auto.py **不会被接管** —— 面板上那台一直
    显示「还没有日志」，必须重启 dash 才能认领。

    为什么是后台线程而不是塞进 SSE 的 500ms 帧里：
      - auto_procs() 每查一次就 spawn 一个 powershell（自带 3 秒缓存）。跟着 500ms 的帧走 =
        每秒 2 个 powershell 进程，机器会一直抖；
      - 放在线程里 = 不管开几个标签页、有没有人看页面，都是每 5 秒一次，成本恒定。
    """
    while True:
        time.sleep(ADOPT_EVERY_S)
        try:
            adopt_existing_runs(print)
        except Exception:  # noqa: BLE001
            pass          # 查不到进程 / 没有控制台可打 —— 都不该打死这个线程


def _run_of(serial, create=True):
    """这台设备的运行状态（没有就建一个空壳）。"""
    s = str(serial or "").strip() or current_serial()
    st = _RUNS.get(s)
    if st is None and create:
        st = {"serial": s, "proc": None, "pid": None, "what": None, "minutes": None,
              "started": None, "ended": None, "code": None, "out": None,
              "lines": deque(maxlen=LOG_MAX)}
        _RUNS[s] = st
    return st


def run_serials():
    """跑过（或正在跑）的设备，最近启动的排前面。"""
    def key(s):
        st = _RUNS.get(s) or {}
        return (st.get("started") or datetime.min, s)
    return sorted(_RUNS, key=key, reverse=True)


def _reap_locked(serial=None):
    """把已经退出的进程状态收回来（调用方持 _LOCK）。"""
    for s in ([serial] if serial else list(_RUNS)):
        st = _RUNS.get(s)
        if not st:
            continue
        proc = st.get("proc")
        if proc is not None and proc.poll() is not None:
            st["ended"] = st.get("ended") or datetime.now()
            st["code"] = proc.poll()
            st["proc"] = None


def _run_snapshot(serial=None):
    """一台设备的运行状态。不给 serial：最新的在跑的那台；全没在跑就 current_serial()。"""
    with _LOCK:
        _reap_locked()
        s = str(serial or "").strip()
        if not s:
            live = [x for x in run_serials() if (_RUNS.get(x) or {}).get("proc") is not None]
            s = live[0] if live else current_serial()
        st = _run_of(s)
        running = st["proc"] is not None
        return {
            "serial": s, "label": dev_label(s), "running": running,
            "pid": st["pid"] if running else None,
            "what": st["what"], "minutes": st["minutes"],
            "minutes_label": minutes_label(st["minutes"]),
            "out": st.get("out"),
            "started_at": st["started"].strftime("%H:%M:%S") if st["started"] else None,
            "elapsed_s": round((datetime.now() - st["started"]).total_seconds(), 1)
            if running and st["started"] else None,
            "code": st["code"],
        }


def runs_snapshot():
    """所有设备的运行状态（列表）—— 页面的「设备状态」和顶部状态条都读它。

    顺序：先按 DEVICES（手机1、手机2），再按最近启动排（未知设备垫后），
    免得每 3 秒刷新时设备行跳来跳去。
    """
    with _LOCK:
        _reap_locked()
        serials = list(run_serials())
    known = [s for s, _l in DEVICES]
    serials = known + [s for s in serials if s not in known]     # 两台手机总在列表里
    out = []
    for s in serials:
        snap = _run_snapshot(s)
        snap["paused"] = paused_for(s)
        if snap["running"]:
            snap["state_txt"], snap["state_cls"] = "运行中 pid %s" % snap["pid"], "running"
        elif snap["paused"]:
            snap["state_txt"], snap["state_cls"] = "已暂停", "paused"
        else:
            snap["state_txt"], snap["state_cls"] = "空闲", "idle"
        out.append(snap)
    return out


def _console_tail(st, n=200):
    """dash 自己 spawn 的那份 stdout 落盘文件（真·实时；且 dash 重启也不受影响）。"""
    p = st.get("console")
    if not p:
        return []
    try:
        return _tail_lines(p, n)
    except Exception:  # noqa: BLE001
        return []


def log_lines(serial=None):
    """面板日志那一段：优先选中那台，其次最近有输出的那台。

    2026-11-03 改：日志源从「内存里的管道缓冲」换成「stdout 落盘文件」（见 start_run）。
    接管来的老进程没有这个文件，退回内存缓冲 —— 那种缓冲通常只有 1 行，
    由 collect_state 里的 logs_by_dev 按「谁更完整」再择优。
    """
    with _LOCK:
        _reap_locked()
        if serial:
            st = _RUNS.get(str(serial).strip())
            if not st:
                return []
            return _console_tail(st) or list(st["lines"])[-200:]
        for s in run_serials():
            st = _RUNS.get(s) or {}
            t = _console_tail(st)
            if t:
                return t
            if st.get("lines"):
                return list(st["lines"])[-200:]
    return []


_GUESS = {"at": 0.0, "by": {}}


def _guess_out_dir_display(serial, ttl_s=10.0):
    """显示用：这台今天到底写到哪个证据目录（10 秒缓存，绝不每帧去扫）。

    只在「按设备算出来的目录里没有 run.log」时兜底：命令行起的 auto.py 不带 --out，
    写的是 evidence/<日期>-auto（不带设备名）。重启 dash 之后内存里已经没有 --out 了 ——
    有这层兜底，面板才不会把那台显示成「还没有日志」（这正是用户报的坑）。
    """
    now = time.time()
    if now - _GUESS["at"] > ttl_s:
        _GUESS.update(at=now, by={})
    key = str(serial or "").strip()
    if key in _GUESS["by"]:
        return _GUESS["by"][key]
    d = _guess_out_dir(key, max_age_s=8 * 3600.0)
    _GUESS["by"][key] = d
    return d


def logs_by_dev_map():
    """每台设备各自的日志（面板实时 > 盘上 run.log > 内存缓冲）。

    2026-11-04：从 build_state() 里原样抽出来，给 SSE 帧复用 —— **优先级只有一份实现**，
    否则推送和轮询会给出不一样的日志（用户看到的就是"一会儿有一会儿没有"）。

    2026-11-03 修：**接管来的进程没有 stdout 管道**（_pump 只对 dash 自己 spawn 的进程有效），
    它的 lines 永远停在「[dash] 接管已在跑的进程…」那一行、不会再长。旧逻辑只要 _live 非空
    就直接拿它当日志，于是「明明在跑、却只看到 1 行」——用户报的「刷新后看不到手机2的日志」。
    判据换成「谁更完整」而不是「有没有」：盘上的 run.log 更长就盘上说了算。
    （不进程内状态判断，因为进程结束后 _reap_locked 会把 proc 清掉，那种判据会失效。）
    """
    out = {}
    for _ser, _lab in DEVICES:
        _st = _RUNS.get(_ser) or {}
        _live = _console_tail(_st)
        # 在跑/刚跑过的那台优先用它自己的 --out（跨零点时目录名里是启动那天的日期）
        _d = _st.get("out") or out_dir(_ser)
        _p = os.path.join(_d, "run.log")
        _disk = _tail_lines(_p, 120)
        if not _disk and not _st.get("out"):
            # 命令行起的 auto.py 不带 --out -> 写在 evidence/<日期>-auto；dash 一重启，
            # 内存里的 --out 就没了。这里反查一下，别让那台永远显示「还没有日志」。
            _alt = _guess_out_dir_display(_ser)
            if _alt and _alt != _d:
                _disk = _tail_lines(os.path.join(_alt, "run.log"), 120)
                if _disk:
                    _d = _alt
        if _live and len(_live) >= len(_disk):
            out[_ser] = {"lines": _live[-120:], "source": "面板实时（%s）" % _lab}
        elif _disk:
            _src = "%s/run.log" % os.path.basename(_d)
            if _live:
                _src += "（面板实时已断，读盘上日志）"
            out[_ser] = {"lines": _disk, "source": _src}
        elif _live:
            out[_ser] = {"lines": _live[-120:], "source": "面板实时（%s）" % _lab}
        else:
            out[_ser] = {"lines": [],
                         "source": "%s/run.log（还没有）" % os.path.basename(_d)}
    return out


def build_stream_frame(sent=None):
    """/api/stream 的一帧（**轻量**）：只装实时性重要的东西。

    {now, refresh_ms, runs:[{...}], logs:{serial:{lines,source}}, pill_state}
    rounds / scans / scripts 这些重数据按轮次变，不进 SSE —— 继续走 /api/state 的 3 秒轮询。

    sent：这条连接上一帧发过的 {serial: 日志文本}。某台的日志一个字都没变，就只发
    lines=None（前端保留原内容）：539 字节的帧没必要每秒发两遍。
    """
    runs = runs_snapshot()
    logs = logs_by_dev_map()
    out_logs = {}
    for _ser, _blob in logs.items():
        _txt = "\n".join(_blob.get("lines") or [])
        if sent is not None and sent.get(_ser) == _txt:
            out_logs[_ser] = {"lines": None, "source": _blob.get("source")}
        else:
            out_logs[_ser] = _blob
            if sent is not None:
                sent[_ser] = _txt
    running_now = [r for r in runs if r.get("running")]
    if running_now:
        pill = "运行中 · " + " + ".join("%s(pid %s)" % (r.get("label"), r.get("pid"))
                                       for r in running_now)
    elif any(r.get("paused") for r in runs):
        pill = "已暂停"
    else:
        pill = "空闲"
    return {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_ms": SSE_MS,
        "runs": runs,
        "logs": out_logs,
        "pill_state": pill,
    }


def _append_lines(path, lines):
    """往日志文件追加几行（拿不到就静默跳过，日志坏了不该能打死面板）。"""
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            for ln in lines:
                f.write(ln + "\n")
    except OSError:
        pass


def _watch_run(proc, st, cpath):
    """等 dash 自己 spawn 的进程结束，把退出码写进日志文件并记进 st。

    2026-11-03：stdout 已重定向到文件，这里不再需要 _pump 那套读管道逻辑。
    """
    try:
        code = proc.wait()
    except Exception:  # noqa: BLE001
        code = None
    line = "[dash] 进程结束，退出码 %s" % code
    _append_lines(cpath, [line])
    with _LOCK:
        st["lines"].append(line)


def _pump(proc, serial, st):
    """读进程输出进这台设备自己的日志缓冲（两台同时跑也不会串）。"""
    try:
        for line in proc.stdout:
            st["lines"].append(line.rstrip("\r\n"))
    except Exception as exc:  # noqa: BLE001
        st["lines"].append("[dash] 读取输出中断：%s" % exc)
    finally:
        try:
            code = proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            code = None
        st["lines"].append("[dash] 进程结束，退出码 %s" % code)


MINUTES_DEFAULT = 30.0
MINUTES_MAX = 1440.0


def norm_minutes(raw):
    """把请求里的 minutes 归一：0 = 不限时间（允许），0~1440 之间照给，
    非法 / 缺省 → 30，负数一律当 30（用户原话：可以设 30 分钟、一小时，或者不限时间）。"""
    if isinstance(raw, bool) or raw is None or raw == "":
        return MINUTES_DEFAULT
    try:
        m = float(raw)
    except (TypeError, ValueError):
        return MINUTES_DEFAULT
    if m != m or m in (float("inf"), float("-inf")):   # NaN / inf
        return MINUTES_DEFAULT
    if m < 0:
        return MINUTES_DEFAULT
    return min(MINUTES_MAX, m)


def minutes_label(minutes):
    """界面 / 日志里的人话时长：30 分钟 / 1 小时 / 不限。"""
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return "-"
    if m <= 0:
        return "不限"
    if m >= 60 and abs(m % 60.0) < 1e-9:
        return "%g 小时" % (m / 60.0)
    return "%g 分钟" % m


def _argv_minutes(minutes):
    """--minutes 的值：0（不限时间）要落成字面 0，别写成 0.0；
    其余照旧 str()，所以默认 30 分钟的命令行跟改造前逐字一样（--minutes 30.0）。"""
    try:
        if float(minutes) == 0:
            return "0"
    except (TypeError, ValueError):
        pass
    return str(minutes)


MAXROUNDS_DEFAULT = 120      # 与 auto.py --max-rounds 的默认值逐字一致
MAXROUNDS_MAX = 100000


def norm_max_rounds(raw):
    """--max-rounds 的值：空 / 非法 / <=0 一律按 120（auto.py 的默认值）。

    2026-11-04 用户要求（轮数保险丝）：输入框里填什么都不许把面板搞挂 ——
    这里把所有脏输入都吃掉；bool 单列出来（True 不该被当成 1 轮）。
    """
    if isinstance(raw, bool) or raw is None or raw == 0 or raw == "":
        return MAXROUNDS_DEFAULT
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        try:
            n = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return MAXROUNDS_DEFAULT
    if n <= 0:
        return MAXROUNDS_DEFAULT
    return min(MAXROUNDS_MAX, n)


def auto_cmd_prefix():
    """auto.py 子进程的启动前缀：源码模式 = python -u auto.py；打包版 = exe --run-auto。

    打包后既没有 python.exe 也没有 auto.py 文件（都在 exe 里），所以让同一个 exe 带
    --run-auto 再起一份当 auto 用（见 coin11/app_entry.py）。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-auto"]
    return [sys.executable, "-u", AUTO_PY]


def build_auto_argv(what, minutes, serial, force_panels=False, scripts=True, script="",
                    out=None, max_rounds=None):
    """auto.py 的命令行。flag 约定见 7.3：--force-panels / --no-scripts / --script <id>。
    默认（不给 flag）与改造前逐字一致 → 老的 {what:"coin"} 请求行为不变。
    时长照旧只走 --minutes（0 就是 --minutes 0，不换成别的 flag）。

    2026-11-02 用户要求（多机并发）唯一新增的参数：`--out <本设备的证据目录>`
    （evidence/<日期>-auto-手机1 | -手机2）。auto.py 本来就支持 --out，不用改它。
    """
    cmd = auto_cmd_prefix() + ["--serial", serial,
                               "--what", what, "--minutes", _argv_minutes(minutes)]
    out = out if out is not None else out_dir(serial)
    if out:
        cmd += ["--out", out]
    if force_panels:
        cmd.append("--force-panels")
    if not scripts:
        cmd.append("--no-scripts")
    if script:
        cmd += ["--script", str(script)]
    # 2026-11-04 用户要求（轮数保险丝）：永远显式给 --max-rounds。
    # 给 120 时与 auto.py 自己的默认值逐字等价，所以老的调用方行为不变。
    cmd += ["--max-rounds", str(norm_max_rounds(max_rounds))]
    return cmd


def start_run(what="all", minutes=30.0, serial=None, force_panels=False, scripts=True, script="",
              max_rounds=None):
    """启动**这台设备**的一轮 auto.py。

    2026-11-02 用户要求（两台同时各跑一轮）：只检查「这台设备」有没有在跑 ——
    同一台重复启动才 409；另一台可以同时启动（各写各的证据目录，互不覆盖）。
    """
    serial = str(serial or "").strip() or current_serial()
    # 2026-11-03 真机事故：页面把**写死的老 IP**（192.168.1.8:5555）发过来，
    # auto.py 拿去连一个不存在的地址 -> 「device not found」，用户看到的是"两台都跑不了"。
    # 这里先按设备表校验：带冒号（ip:port）的必须是配置里登记过的地址；USB（无冒号）放行。
    if ":" in serial and serial not in DEVICE_LABELS:
        return {"ok": False, "serial": serial,
                "error": "地址不在设备表里：%s（当前登记：%s）。换网络后请先跑 "
                         "`python coin11/discover_devices.py`，再刷新本页面"
                         % (serial, "、".join("%s=%s" % (l, s) for s, l in DEVICES))}, 400
    label = dev_label(serial)
    with _LOCK:
        _reap_locked(serial)
        st = _run_of(serial)
        if st["proc"] is not None:
            return {"ok": False, "serial": serial,
                    "error": "%s 已有一轮在跑（pid %s），不能并发" % (label, st["pid"])}, 409
    if paused_for(serial):
        # 暂停就是锁：暂停状态下不许自动/误触重启（先点【继续】）。
        # 按设备判断：停/暂停手机2 不影响手机1。
        return {"ok": False, "serial": serial,
                "error": "%s 当前是暂停状态（%s），先点【▶ 继续】再启动"
                         % (label, os.path.relpath(PAUSE_FLAG, ROOT))}, 409
    out = out_dir(serial)
    cmd = build_auto_argv(what, minutes, serial, force_panels, scripts, script, out, max_rounds)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    # 2026-11-03 修（「重启 dash 后日志丢失」的真根因）：原来把 auto.py 的 stdout 挂在 dash 的
    # **管道**上。dash 一重启/一崩，读端消失，auto.py 里 print(line, flush=True) 就阻塞或抛错 ——
    # 真机实测后果：run.log 从那一刻起不再增长、整轮任务提前收尾
    # （证据：手机2 那轮 run.log 冻在 21:21:18，正好是杀掉旧 dash 的 21:21:22）。
    # 现在 stdout 直接**重定向到文件**，父子之间没有管道：dash 随便重启，任务照跑、日志照写。
    cpath = os.path.join(out, "console.log")
    cf = None
    sink = subprocess.DEVNULL
    try:
        # 2026-11-04 真机回归抓到：第一次为这台设备建证据目录时 out 还不存在（目录是 auto.py 自己建的），
        # open() 失败 -> 兜底成 DEVNULL -> 整轮的实时日志全丢。必须先建目录再开文件。
        os.makedirs(out, exist_ok=True)
        cf = open(cpath, "w", encoding="utf-8", errors="replace")
        sink = cf
    except OSError:
        cpath = ""
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=sink, stderr=subprocess.STDOUT,
                                env=env, creationflags=_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "serial": serial, "error": "启动失败：%s" % exc}, 500
    finally:
        if cf is not None:
            cf.close()
    rounds_n = norm_max_rounds(max_rounds)
    head = ["[dash] 已启动 pid=%s（%s）" % (proc.pid, label),
            "[dash] 命令：" + " ".join(cmd),
            "[dash] 本次运行时长：" + minutes_label(minutes),
            "[dash] 轮数保险丝：%d 轮" % rounds_n,
            "[dash] 证据目录：" + out]
    if cpath:
        _append_lines(cpath, head)
    with _LOCK:
        st.update(proc=proc, pid=proc.pid, what=what, minutes=float(minutes), serial=serial,
                  started=datetime.now(), ended=None, code=None, out=out, console=cpath)
        st["lines"].clear()
        st["lines"].extend(head)
    threading.Thread(target=_watch_run, args=(proc, st, cpath), daemon=True).start()
    flags = []
    if force_panels:
        flags.append("--force-panels")
    if not scripts:
        flags.append("--no-scripts")
    if script:
        flags += ["--script", str(script)]
    flags.append("--max-rounds %d" % rounds_n)
    return {"ok": True, "pid": proc.pid, "what": what, "minutes": float(minutes),
            "minutes_label": minutes_label(minutes), "max_rounds": rounds_n,
            "serial": serial, "label": label, "out": out, "script": str(script or ""),
            "flags": flags, "argv": cmd, "cmd": " ".join(cmd)}, 200


def stop_run(serial=None):
    """只杀**这台设备**的进程（2026-11-02 用户要求：停手机2 不许打断手机1）。"""
    serial = str(serial or "").strip() or current_serial()
    with _LOCK:
        _reap_locked(serial)
        st = _run_of(serial)
        proc = st["proc"]
        if proc is None:
            return {"ok": False, "serial": serial,
                    "error": "%s 上没有面板启的进程" % dev_label(serial)}, 400
        pid = proc.pid
    out = ""
    try:
        if os.name == "nt":
            # taskkill 的提示是 GBK：必须 errors="replace"，否则读管道线程会抛 UnicodeDecodeError
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=25,
                               encoding="utf-8", errors="replace",
                               creationflags=_NO_WINDOW)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
        else:
            proc.terminate()
        proc.wait(timeout=20)
    except Exception as exc:  # noqa: BLE001
        out = (out + " / " + str(exc)).strip(" /")
    with _LOCK:
        st["ended"] = datetime.now()
        st["code"] = proc.poll()
        st["lines"].append("[dash] 已停止 pid=%s %s" % (pid, out))
        st["proc"] = None
    return {"ok": True, "serial": serial, "pid": pid, "out": out}, 200


# ---------------------------------------------------------------- 总控台后端
# 保活 / 暂停-继续 / 强制结束 / 占用探测 / 跳转策略 / 手动记录 / 实时状态

def _ensure_dir(p):
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass


_ADB = {"path": None, "tried": False}
_ADB_LOCK = threading.Lock()


def adb_path():
    """找 adb：环境变量 ADB → PATH → adbutils 自带 → site-packages 兜底。

    加锁：投屏线程和主线程会同时问，先前的「tried 先置位」写法会读到 None（实测）。
    """
    with _ADB_LOCK:
        return _adb_path_locked()


def _adb_path_locked():
    if _ADB["tried"]:
        return _ADB["path"]
    cands = [os.environ.get("ADB") or "", os.environ.get("ANDROID_SDK_ROOT") or ""]
    cands = [c for c in cands if c]
    w = shutil.which("adb")
    if w:
        cands.append(w)
    try:
        import adbutils
        fn = getattr(adbutils, "adb_path", None)
        if callable(fn):
            cands.append(fn())
    except Exception:  # noqa: BLE001
        pass
    cands.append(os.path.join(sys.prefix, "Lib", "site-packages", "adbutils", "binaries", "adb.exe"))
    _ADB["tried"] = True
    for c in cands:
        if c and os.path.isfile(c):
            _ADB["path"] = c
            return c
    _ADB["path"] = None
    return None


_ADB_READY = True


def _adb(serial, args, timeout=30, binary=False):
    """直接调 adb 二进制（面板只用标准库，不 import adbutils 跑设备）。"""
    adb = adb_path()
    if not adb:
        return None, "找不到 adb 二进制（可用环境变量 ADB 指定）"
    cmd = [adb]
    if serial:
        cmd += ["-s", str(serial)]
    cmd += [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return None, "adb 调用失败：%s" % exc
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    if binary:
        return (r.stdout if r.returncode == 0 else None), err
    out = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0 and not out:
        return None, err or "adb 返回码 %s" % r.returncode
    return out, err


# ---- 保活（每台设备一个后台线程；默认全关）--------------------

_KEEP_LOCK = threading.Lock()
_KEEP = {}
for _s, _l in DEVICES:
    _KEEP[_s] = {"serial": _s, "label": _l, "on": False, "interval": KEEP_DEFAULT_MIN,
                 "thread": None, "stop": None, "last_at": None, "last_result": "",
                 "hits": 0, "skips": 0, "wakes": 0, "log": deque(maxlen=80), "next_at": None}


def _keep_note(serial, msg):
    line = "%s %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    st = _KEEP.get(serial)
    if st is not None:
        st["log"].append(line)
    _ensure_dir(STATE_DIR)
    try:
        with open(KEEP_LOG, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (serial or "-", line))
    except OSError:
        pass


def _run_active(serial=None):
    """**这台设备**的脚本是不是正在跑 —— 决定保活要不要让路（2026-11-02 用户要求）。

    两条判据都看，因为脚本可能是面板按钮拉起的，也可能是 .bat/命令行在外面起的：
      1) dash 自己 start_run() 拉起的这台设备的进程还活着；
      2) auto.py 写的 state/live-<san>.json（老版本只写 state/live.json）里
         phase 不是 done/idle，且文件 10 分钟内更新过。
    2026-11-02（多机并发）：按设备判断 —— 手机2 在跑不该让手机1 的保活让路。
    不用太精确：宁可少保活一次，也不要在脚本读屏/滑动时插一脚。
    """
    s = str(serial or "").strip() or current_serial()
    try:
        with _LOCK:
            _reap_locked(s)
            proc = (_RUNS.get(s) or {}).get("proc")
        if proc is not None and proc.poll() is None:
            return True
    except (NameError, AttributeError, OSError):
        pass
    for path in live_paths(s):
        d = _read_json(path)
        if not isinstance(d, dict):
            continue
        phase = str(d.get("phase") or "").strip().lower()
        if phase in ("", "done", "idle", "stop", "stopped"):
            continue
        if path == LIVE_JSON and str(d.get("serial") or "").strip() not in ("", s):
            continue        # 老的单份 live.json：serial 对不上就不是这台设备的
        try:
            if (time.time() - os.path.getmtime(path)) < 600:
                return True
        except OSError:
            pass
    return False


def _keep_tick(serial):
    """一次保活：脚本在跑就让路；屏幕醒着才拉通知栏再收起；Asleep 只记录，绝不按键。"""
    st = _KEEP[serial]
    if _run_active(serial):  # 这台设备的脚本在跑就让路（别台的脚本不影响它）
        # 2026-11-02 用户要求：保活会和脚本抢设备（它注入触摸/改焦点），
        # 脚本运行时一律跳过；脚本结束后自动恢复（开关保持 on 不动，用户不用再点一次）。
        st["skips"] += 1
        st["last_result"] = "脚本正在运行，本次让路（跑完自动恢复）"
        _keep_note(serial, "[keep] 检测到脚本正在运行 -> 本次不动作（跑完自动恢复）")
        return
    if paused_for(serial):
        st["skips"] += 1
        st["last_result"] = "已暂停，本次不动作"
        _keep_note(serial, "[keep] %s 处于暂停状态 -> 本次不动作" % dev_label(serial))
        return
    out, err = _adb(serial, ["shell", "dumpsys power"], timeout=25)
    if out is None:
        st["last_result"] = "adb 不可用：%s" % err
        _keep_note(serial, "[keep] %s" % st["last_result"])
        return
    m = re.search(r"mWakefulness=\s*([A-Za-z]+)", out or "")
    wake = (m.group(1) if m else "unknown").lower()
    if not wake.startswith("awake"):
        # 2026-11-02 用户反馈「手机刚息屏了」：原来这里只记账、一个键都不发，于是睡下去就再也不醒。
        # 改成发 **KEYCODE_WAKEUP(224)** —— 它只会点亮屏幕，绝不会把亮屏按灭；
        # 会按灭的是 26（POWER 是切换），那条铁律继续执行：永不发 26。
        st["wakes"] += 1
        _keep_note(serial, "[keep] 屏幕 %s -> 发 keyevent 224 唤醒（只点亮，不按灭）" % wake)
        _adb(serial, ["shell", "input", "keyevent", "224"], timeout=20)
        time.sleep(0.9)
        out, err = _adb(serial, ["shell", "dumpsys power"], timeout=25)
        m = re.search(r"mWakefulness=\s*([A-Za-z]+)", out or "")
        wake = (m.group(1) if m else "unknown").lower()
        if not wake.startswith("awake"):
            st["skips"] += 1
            st["last_result"] = "唤醒失败（屏幕仍 %s），可能设了锁屏密码，需要人工" % wake
            _keep_note(serial, "[keep] 224 之后仍 %s -> 需人工解锁（绝不发 26）" % wake)
            return
        kd, _ke = _adb(serial, ["shell", "dumpsys", "window"], timeout=25)
        if kd and any(k in kd for k in ("mDreamingLockscreen=true", "mShowingLockscreen=true",
                                        "isKeyguardShowing=true")):
            st["last_result"] = "已唤醒但停在锁屏——需要人工解锁（脚本没有密码，也不会猜）"
            _keep_note(serial, "[keep] 224 唤醒成功，但停在锁屏 -> 需要人工解锁；"
                               "保活只能防它睡，防不了它锁")
            return
        st["last_result"] = "已用 keyevent 224 唤醒屏幕"
        _keep_note(serial, "[keep] 已唤醒（keyevent 224）")
    # 这一拍：input keyevent 224 —— 输入管道会把它算成「用户活动」，重置息屏计时器；
    # 同时它只点亮、不点击、不滑动，界面一动不动（2026-11-02 实测：20 秒超时下每 8 秒一次，50 秒仍亮）。
    cmd = list(KEEP_ACTIVITY_CMD)
    out2, err2 = _adb(serial, ["shell"] + cmd, timeout=25)
    if out2 is None:
        st["last_result"] = "保活指令失败：%s" % err2
        _keep_note(serial, "[keep] %s" % st["last_result"])
        return
    st["hits"] += 1
    st["last_at"] = datetime.now()
    # 2026-11-03 真机：auto.py 跑完会在 finally 里 svc power stayon false（它以为是它开的），
    # 把保活设的「插电常亮」一起冲掉 —— 之后手机就没人管、照常息屏。
    # 所以保活每 N 拍重新声明一次（很便宜：两条 shell）。
    last_guard = st.get("guard_at")
    if last_guard is None or (datetime.now() - last_guard).total_seconds() > 240:
        _keep_screen_guard(serial, True)
        st["guard_at"] = datetime.now()
    st["last_result"] = "已发 %s（第 %d 次，算用户活动、界面不动）" % (" ".join(cmd), st["hits"])
    _keep_note(serial, "[keep] adb -s %s shell %s -> ok（第 %d 次，重置息屏计时器）"
               % (serial, " ".join(cmd), st["hits"]))


def _keep_loop(serial, stop_evt):
    st = _KEEP[serial]
    first = True
    while not stop_evt.is_set():
        if not first:
            if stop_evt.wait(max(0.5, float(st["interval"])) * 60.0):
                break
        first = False
        if not st["on"]:
            break
        try:
            _keep_tick(serial)
        except Exception as exc:  # noqa: BLE001
            _keep_note(serial, "[keep] 异常：%s" % exc)
        st["next_at"] = datetime.now() + timedelta(seconds=max(0.5, float(st["interval"])) * 60.0)


def keep_set(serial, on, interval=None):
    with _KEEP_LOCK:
        st = _KEEP.get(serial)
        if st is None:
            return {"ok": False, "error": "未知设备 %s（只有 %s）" % (serial, "/".join(DEVICE_LABELS))}, 400
        if interval is not None:
            try:
                iv = float(interval)
            except (TypeError, ValueError):
                iv = st["interval"]
            if iv not in KEEP_INTERVALS:
                return {"ok": False, "error": "间隔只能是 %s 分钟"
                        % "/".join("%g" % x for x in KEEP_INTERVALS)}, 400
            if float(iv) != float(st["interval"]):
                _keep_note(serial, "[keep] 间隔改为 %g 分钟" % float(iv))
            st["interval"] = iv
        if on:
            if not st["on"]:
                ev = threading.Event()
                st.update(on=True, stop=ev, last_result="", next_at=None)
                st["log"].clear()
                th = threading.Thread(target=_keep_loop, args=(serial, ev), daemon=True,
                                      name="keep-%s" % serial)
                st["thread"] = th
                th.start()
                _keep_screen_guard(serial, True)      # 先把熄屏时间拉长，再开始保活
                _keep_note(serial,
                           "[keep] 已开启：每 %g 分钟一次「shell %s」"
                           "（走输入管道 = 系统认作用户活动，会重置息屏计时；"
                           "只点亮、不点击不滑动、界面不动；脚本运行时自动让路，跑完自动恢复）"
                           % (st["interval"], " ".join(KEEP_ACTIVITY_CMD)))
        else:
            ev = st.get("stop")
            if st["on"]:
                _keep_note(serial, "[keep] 已关闭，不再保活")
            st["on"] = False
            st["thread"] = None
            st["next_at"] = None
            if ev is not None:
                ev.set()
            _keep_screen_guard(serial, False)     # 关掉就还原你原来的熄屏时间
        _keep_save()
        return {"ok": True, "serial": serial, "on": st["on"], "interval": st["interval"]}, 200


def _keep_save():
    """把开关与间隔落盘：dash 重启后按你上次的选择恢复（2026-11-02 用户要求）。"""
    snap = {s: {"on": bool(_KEEP[s]["on"]), "interval": float(_KEEP[s]["interval"])}
            for s in _KEEP}
    try:
        _ensure_dir(STATE_DIR)
        with open(KEEP_JSON, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _keep_load():
    """启动时恢复保活开关（只恢复上次真的开着的那几台）。"""
    d = _read_json(KEEP_JSON)
    if not isinstance(d, dict):
        return
    for s, v in d.items():
        if s not in _KEEP or not isinstance(v, dict):
            continue
        iv = v.get("interval")
        if iv in KEEP_INTERVALS:
            _KEEP[s]["interval"] = float(iv)
        if v.get("on"):
            keep_set(s, True)


def open_panel_window(url):
    """用 Chrome/Edge 的 --app 模式开一个**独立窗口**（没有地址栏/标签页）。

    2026-11-09 用户要求「关闭面板要能把浏览器那个页面一起关掉」：
    普通标签页浏览器**不允许**脚本关窗口（安全限制），app 模式窗口允许 —— 所以面板一律用
    app 模式开；点【⏻ 关闭面板】时页面里那句 window.close() 才真能把它关掉。
    找不到 Chrome/Edge 就退回默认浏览器（那时窗口只能手动关，页面上会写明）。
    """
    cands = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env) or ""
        if not root:
            continue
        cands.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
        cands.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
    for exe in cands:
        if not os.path.isfile(exe):
            continue
        try:
            subprocess.Popen([exe, "--app=" + url], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return exe
        except OSError:
            continue
    try:
        import webbrowser
        webbrowser.open(url)          # 兜底：默认浏览器（关不掉窗口，页面会提示手动关）
    except Exception:  # noqa: BLE001
        pass
    return ""


#: 面板自己的 HTTP server + 关停状态（【⏻ 关闭面板】要用它优雅退出）
_SRV = None
_SHUTDOWN = {"at": None}


def shutdown_panel(reason="面板【关闭面板】"):
    """停掉所有面板起的 auto → 还原保活改过的屏幕设置 → 关掉 HTTP 服务（进程随之退出）。

    2026-11-09 用户要求：「用完了点这个就可以全部关闭安全退出」。
    顺序不能反：先杀进程（别留孤儿 auto 在后台继续点手机），再还原屏幕设置，最后才关服务。
    """
    killed = []
    try:
        with _LOCK:
            _reap_locked()
            serials = [s for s in _RUNS if (_RUNS.get(s) or {}).get("proc") is not None]
        for s in serials:
            try:
                out, _code = force_stop(reason, s)
                killed.append({"serial": s, "ok": bool((out or {}).get("ok")),
                               "pid": (out or {}).get("pid")})
            except Exception as exc:  # noqa: BLE001
                killed.append({"serial": s, "ok": False, "error": str(exc)[:120]})
    finally:
        try:
            keep_all_off(reason)          # 保活改过的 screen_off_timeout 还原
        except Exception:  # noqa: BLE001
            pass
        try:
            screen_live_set(False)        # 关实时投屏（还在抓帧的话 adb 会忙，手机也烫）
        except Exception:  # noqa: BLE001
            pass
    _SHUTDOWN["at"] = datetime.now().strftime("%H:%M:%S")

    def _later():
        time.sleep(0.5)                   # 先把响应发回去，再关服务
        try:
            srv = _SRV
            if srv is not None:
                srv.shutdown()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_later, name="shutdown", daemon=True).start()
    return {"ok": True, "stopped": killed, "at": _SHUTDOWN["at"]}


def keep_all_off(who="面板"):
    for s in list(_KEEP):
        if _KEEP[s]["on"]:
            _keep_note(s, "[keep] %s 触发 -> 关闭保活" % who)
            keep_set(s, False)


def keep_state():
    out = {}
    for s, st in _KEEP.items():
        out[s] = {
            "serial": s, "label": st["label"], "on": bool(st["on"]),
            "interval": st["interval"], "hits": st["hits"], "skips": st["skips"],
            "wakes": st.get("wakes", 0), "screen_guard_ms": KEEP_SCREEN_ON_MS,
            "last_at": st["last_at"].strftime("%H:%M:%S") if st["last_at"] else None,
            "next_at": st["next_at"].strftime("%H:%M:%S") if st["next_at"] else None,
            "last_result": st["last_result"],
            "log": list(st["log"])[-12:],
        }
    return out


# ---- 暂停 / 继续 / 强制结束 ------------------------------------

_CTL = {"last": None, "paused_at": None, "resumed_at": None, "stopped_at": None}


def _ctl_log(kind, who, extra=""):
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
           "who": who, "extra": extra}
    _ensure_dir(STATE_DIR)
    try:
        with open(CTL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    _CTL["last"] = rec
    return rec


def paused():
    """全局「全部暂停」：state/pause.flag 存在。老含义保留，现在多解释成「全部暂停」。"""
    return os.path.exists(PAUSE_FLAG)


def paused_flags():
    """state 下所有 pause-*.flag（每台设备一个）。"""
    try:
        return [os.path.join(STATE_DIR, n) for n in os.listdir(STATE_DIR)
                if n.startswith("pause-") and n.endswith(".flag")]
    except OSError:
        return []


def paused_for(serial):
    """**这台设备**是否暂停：自己的 pause-<san>.flag，或全局「全部暂停」。

    2026-11-02 用户要求：暂停/继续按设备独立（停手机2 不许打断手机1）；
    state/pause.flag 保留，含义变成「全部暂停」。
    """
    if paused():
        return True
    return any(os.path.exists(p) for p in pause_paths(serial))


def paused_any():
    return paused() or bool(paused_flags())


def _write_flags(paths, who, serial=None):
    """写一组暂停标志（两种 san 写法都写，auto.py 读哪个都行）。返回错误字符串。"""
    _ensure_dir(STATE_DIR)
    body = json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "who": who, "serial": serial or ""}, ensure_ascii=False)
    try:
        for p in paths:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(body)
    except OSError as exc:
        return "写暂停标志失败：%s" % exc
    return ""


def pause_now(who="页面", serial=None):
    """serial 给了 → 只暂停这台；没给 → 全部暂停（全局 pause.flag + 每台各自的 flag）。"""
    s = str(serial or "").strip()
    if s:
        paths = pause_paths(s)
        err = _write_flags(paths, who, s)
        if err:
            return {"ok": False, "error": err}, 500
        _CTL["paused_at"] = datetime.now()
        _CTL["stopped_at"] = None
        rec = _ctl_log("PAUSE", who, "%s 暂停（%s）"
                       % (dev_label(s), os.path.relpath(paths[0], ROOT)))
        return {"ok": True, "paused": True, "serial": s, "label": dev_label(s),
                "at": rec["ts"], "pause_flag": os.path.relpath(paths[0], ROOT)}, 200
    targets = [x for x, _l in DEVICES] + [x for x in run_serials() if x not in DEVICE_LABELS]
    paths = [PAUSE_FLAG]
    for x in targets:
        paths += pause_paths(x)
    err = _write_flags(paths, who, None)
    if err:
        return {"ok": False, "error": err}, 500
    _CTL["paused_at"] = datetime.now()
    _CTL["stopped_at"] = None
    rec = _ctl_log("PAUSE_ALL", who, "全部暂停（写了 %d 个标志）" % len(paths))
    return {"ok": True, "paused": True, "all": True, "at": rec["ts"],
            "pause_flag": os.path.relpath(PAUSE_FLAG, ROOT)}, 200


def resume_now(who="页面", serial=None):
    """serial 给了 → 只继续这台（顺带撤掉「全部暂停」这个全局锁，别的设备仍按各自的 flag 停着）；
    没给 → 全部继续（撤掉全局 + 所有设备标志）。"""
    s = str(serial or "").strip()
    paths = (pause_paths(s) + [PAUSE_FLAG]) if s else ([PAUSE_FLAG] + paused_flags())
    try:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)
    except OSError as exc:
        return {"ok": False, "error": "删暂停标志失败：%s" % exc}, 500
    _CTL["paused_at"] = None
    _CTL["resumed_at"] = datetime.now()
    if s:
        rec = _ctl_log("RESUME", who, "%s 继续（不影响别的设备）" % dev_label(s))
        return {"ok": True, "paused": False, "serial": s, "label": dev_label(s),
                "at": rec["ts"]}, 200
    rec = _ctl_log("RESUME_ALL", who, "全部继续")
    return {"ok": True, "paused": False, "all": True, "at": rec["ts"]}, 200


def _rm_pause_flags(paths):
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def force_stop(who="页面", serial=None):
    """真杀进程（只在 pause 也叫不醒时用）+ 关保活 + 落一条 STOP。

    2026-11-02 用户要求：serial 给了就只动那台（停手机2 不许打断手机1）；
    没给就全部停（老 /api/stop 的语义，等于 /api/stop_all）。
    """
    s = str(serial or "").strip()
    if s:
        with _LOCK:
            _reap_locked(s)
            running = (_RUNS.get(s) or {}).get("proc") is not None
            pid = (_RUNS.get(s) or {}).get("pid")
        detail = ""
        if running:
            res, _code = stop_run(s)
            detail = "已杀 pid=%s %s" % (pid, (res or {}).get("out") or "")
        if (_KEEP.get(s) or {}).get("on"):
            keep_set(s, False)
            detail = (detail + " · 已关这台的保活").strip(" ·")
        _rm_pause_flags(pause_paths(s))     # 停掉的这台不需要暂停锁了
        _CTL["stopped_at"] = datetime.now()
        rec = _ctl_log("STOP", who, "%s：%s" % (dev_label(s), detail or "没有面板启的进程"))
        return {"ok": True, "serial": s, "label": dev_label(s), "at": rec["ts"],
                "killed": running, "pid": pid, "detail": detail}, 200
    with _LOCK:
        _reap_locked()
        targets = list(_RUNS)
    details, pids = [], []
    for x in targets:
        with _LOCK:
            running = (_RUNS.get(x) or {}).get("proc") is not None
        if not running:
            continue
        pid = (_RUNS.get(x) or {}).get("pid")
        res, _code = stop_run(x)
        pids.append(pid)
        details.append("%s pid=%s %s" % (dev_label(x), pid, (res or {}).get("out") or ""))
    keep_all_off("强制结束")
    _rm_pause_flags([PAUSE_FLAG] + paused_flags())
    _CTL["paused_at"] = None
    _CTL["stopped_at"] = datetime.now()
    rec = _ctl_log("STOP_ALL", who, "; ".join(details) or "没有面板启的进程，只关了保活/锁")
    return {"ok": True, "all": True, "at": rec["ts"], "killed": bool(pids),
            "pid": pids[0] if pids else None, "pids": pids,
            "detail": "; ".join(details)}, 200


# ---- 谁在占用手机（读本机进程命令行）---------------------------

_PROC = {"at": 0.0, "rows": []}
#: 只认「python 解释器直接跑 auto.py」这种命令行；同样的文字出现在别处（bash -c 的引号串、
#: 编辑器、harness 自己的进程）统统不算占用 —— 实测放过一次会把 5 个进程误判成 auto.py。
_PY_RE = re.compile(r"^python(w)?[0-9.]*(\.exe)?$", re.I)


def _procs_fresh():
    """auto_procs() 的**只读**版本：缓存还新鲜就返回，过期返回 None（绝不在这里 spawn）。

    SSE 每 500ms 一帧，谁在推送线程里调 auto_procs()，谁就可能被 powershell 拖住 1~2 秒。
    """
    if time.time() - _PROC["at"] < AUTO_PROC_CACHE_S:
        return _PROC["rows"]
    return None


def _looks_like_auto_py(cmd):
    """第一个 token（可执行文件）必须是 python，且参数里真有一个 auto.py。
    只看「命令行里出现 auto.py」会误判：bash -c 的引号串、harness 自己的进程都会带上这个词。"""
    c = (cmd or "").strip()
    if not c:
        return False
    if c.startswith('"'):
        end = c.find('"', 1)
        if end < 0:
            return False
        exe, rest = c[1:end], c[end + 1:]
    else:
        parts = c.split(None, 1)
        exe, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    if not _PY_RE.match(os.path.basename(exe)):
        return False
    return bool(re.search(r'(?:^|[\s"])[^\s"]*auto\.py(?:[\s"]|$)', rest))


def auto_procs():
    now = time.time()
    if now - _PROC["at"] < AUTO_PROC_CACHE_S:
        return _PROC["rows"]
    rows = []
    if os.name == "nt":
        # 2026-11-04 真机回归抓到：PowerShell 默认按 GBK 吐字节，旧代码按 UTF-8 + errors="replace" 解，
        # 于是命令行里的中文路径（evidence\2026-11-04-auto-手机2）被解成 U+FFFD，
        # 接管来的 run 记了个不存在的 --out -> 面板日志永远 0 行。先让 PS 吐 UTF-8，再严格解码。
        ps = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
              "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'auto\\.py' } "
              "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, timeout=25, creationflags=_NO_WINDOW)
            # 严格 UTF-8 解，解不了才退回 GBK —— 不再用 errors="replace" 把中文打成 U+FFFD
            _raw = r.stdout or b""
            try:
                txt = _raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                txt = _raw.decode("gbk", "replace").strip()
            if txt:
                data = json.loads(txt)
                if isinstance(data, dict):
                    data = [data]
                for it in data:
                    cmd = str(it.get("CommandLine") or "")
                    if _looks_like_auto_py(cmd):
                        rows.append({"pid": it.get("ProcessId"), "cmd": cmd})
        except Exception:  # noqa: BLE001
            rows = []
    else:
        try:
            r = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, timeout=15)
            for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
                if _looks_like_auto_py(line):
                    parts = line.strip().split(None, 1)
                    rows.append({"pid": parts[0], "cmd": parts[1] if len(parts) > 1 else ""})
        except Exception:  # noqa: BLE001
            rows = []
    _PROC.update(at=now, rows=rows)
    return rows


def _port_open(port, host=HOST):
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.settimeout(0.25)
    try:
        return sk.connect_ex((host, int(port))) == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        sk.close()


# ---- 跳转确认策略（auto.py 读 jump_policy.json 决定点哪个按钮）----

def jump_get():
    d = _read_json(JUMP_POLICY)
    pol = "once"
    if isinstance(d, dict):
        raw = str(d.get("policy") or d.get("mode") or "").lower()
        if raw == "always":
            pol = "always"
    return {"policy": pol,
            "label": "始终打开" if pol == "always" else "仅打开一次",
            "updated_at": (d or {}).get("updated_at", "") if isinstance(d, dict) else "",
            "by": (d or {}).get("by", "") if isinstance(d, dict) else "",
            "path": os.path.relpath(JUMP_POLICY, ROOT)}


def jump_set(policy, who="页面"):
    pol = "always" if str(policy or "").strip().lower() in ("always", "始终打开") else "once"
    rec = {
        "policy": pol,
        "mode": pol,          # 兼容读 "mode" 的那一路实现（auto.py 的 load_jump_policy）
        "label": "始终打开" if pol == "always" else "仅打开一次",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "by": who,
        "note": "auto.py 读本文件决定跳转确认框点哪个按钮。"
                "once=仅打开一次（每次弹框都确认）；"
                "always=始终打开（系统不再弹确认框，直接进入目标 App —— 用户已知悉）。",
        "targets_for": list(DEVICE_LABELS),
    }
    _ensure_dir(TARGETS_DIR)
    try:
        with open(JUMP_POLICY, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        return {"ok": False, "error": "写 %s 失败：%s" % (JUMP_POLICY, exc)}, 500
    _ensure_dir(STATE_DIR)
    _ctl_log("JUMP_POLICY", who, "%s -> %s" % (rec["label"], rec["policy"]))
    return {"ok": True, **jump_get()}, 200


# ---- 手动/自动记录（用户教我什么）------------------------------

def manual_list(n=20):
    rows = []
    try:
        with open(MANUAL_LOG, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        return []
    rows = rows[-int(n):]
    rows.reverse()
    return rows


def manual_add(serial, action, screenshot="", note="", source="manual"):
    action = (action or "").strip()
    if not action:
        return {"ok": False, "error": "请先填「我做了什么」"}, 400
    shots = [s for s in str(screenshot or "").replace(";", ",").split(",") if s.strip()]
    urls = []
    for s in shots:
        p = s.strip() if os.path.isabs(s.strip()) else os.path.join(ROOT, s.strip())
        urls.append("/shot?p=" + urllib.parse.quote(p))
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "serial": serial or current_serial(),
           "device": DEVICE_LABELS.get(serial or "", ""),
           "action": action,
           "screenshot": ",".join(s.strip() for s in shots),
           "shot_urls": urls,
           "note": (note or "").strip(),
           "source": source}
    _ensure_dir(TARGETS_DIR)
    try:
        with open(MANUAL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        return {"ok": False, "error": "写入失败：%s" % exc}, 500
    return {"ok": True, "record": rec}, 200


def snap(serial):
    """截取当前屏幕到 coin11/targets/shots/（给手动记录挂图用）。"""
    _ensure_dir(SNAP_DIR)
    tag = (serial or "dev").replace(":", "_").replace(".", "-")
    name = "%s_%s.png" % (datetime.now().strftime("%Y%m%d-%H%M%S"), tag)
    p = os.path.join(SNAP_DIR, name)
    data, err = _adb(serial, ["exec-out", "screencap", "-p"], timeout=45, binary=True)
    if not data:
        return {"ok": False, "error": "截图失败：%s" % (err or "adb 没有返回数据")}, 500
    try:
        with open(p, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        return {"ok": False, "error": "存图失败：%s" % exc}, 500
    return {"ok": True, "path": os.path.relpath(p, ROOT), "bytes": len(data),
            "url": "/shot?p=" + urllib.parse.quote(p)}, 200


def try_tap(serial, x, y):
    """【试点击】：面板上一次单点（用户点了才算数，绝不自己连点）；同样存前后两张图 + 记录。"""
    res, code = panel_op(serial, "tap", x, y, note="面板【试点击】")
    if not res.get("ok"):
        return res, code
    return {"ok": True, "serial": serial, "x": int(float(x)), "y": int(float(y)),
            "at": datetime.now().strftime("%H:%M:%S"), "record": res.get("record")}, 200


# ---- 面板设置（自动记录开关）-----------------------------------

_CFG = {"auto_record": False, "last_touch": {}}


def load_cfg():
    d = _read_json(PANEL_CFG)
    if isinstance(d, dict):
        _CFG["auto_record"] = bool(d.get("auto_record"))


def set_auto_record(on):
    _CFG["auto_record"] = bool(on)
    _ensure_dir(TARGETS_DIR)
    try:
        with open(PANEL_CFG, "w", encoding="utf-8") as fh:
            json.dump({"auto_record": _CFG["auto_record"],
                       "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return {"ok": True, "auto_record": _CFG["auto_record"]}, 200


def auto_record(serial, action, note=""):
    """自动记录开关打开时，把面板发起的一次动作写进 manual_log.jsonl（source=auto）。"""
    if not _CFG["auto_record"]:
        return None
    out, _code = manual_add(serial, action, "", note, source="auto")
    return out


# ---- 规则管理（targets.jsonl：查看 / 编辑 / 删除 / 合并相似）-----
#
# 用户原话：「那些规则我要可以编辑删除」「相似的教学可以清除了」。
# 铁律：每次写 targets.jsonl 之前先备份成 targets.jsonl.bak-YYYYmmdd-HHMMSS。

RULE_DEDUPE_PX = 40.0
_RULES_LOCK = threading.Lock()


def _rules_read():
    """读 targets.jsonl → [{...原字段, "idx": 序号}]；坏行跳过，未知字段原样保留。"""
    rows = []
    try:
        with open(TARGETS_JSONL, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(d, dict):
                    continue
                d["idx"] = len(rows)
                rows.append(d)
    except OSError:
        pass
    return rows


def _rules_write(rows):
    """整表回写：先写临时文件再 os.replace，绝不留下半截规则文件。"""
    _ensure_dir(TARGETS_DIR)
    tmp = "%s.tmp-%d" % (TARGETS_JSONL, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for d in rows:
                fh.write(json.dumps({k: v for k, v in d.items() if k != "idx"},
                                    ensure_ascii=False) + "\n")
        os.replace(tmp, TARGETS_JSONL)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _rules_backup():
    """写前备份 → 返回备份文件绝对路径（同一秒重复就加 -2/-3，绝不覆盖旧备份）。"""
    _ensure_dir(TARGETS_DIR)
    base = TARGETS_JSONL + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = "%s-%d" % (base, n)
    shutil.copy2(TARGETS_JSONL, path)
    return path


def _rules_public(d):
    serial = str(d.get("serial") or "")
    return {"idx": d.get("idx"), "label": str(d.get("label") or ""),
            "note": str(d.get("note") or ""), "x": d.get("x"), "y": d.get("y"),
            "screen_w": d.get("screen_w"), "screen_h": d.get("screen_h"),
            "serial": serial, "device": DEVICE_LABELS.get(serial, serial or "?"),
            "ts": str(d.get("ts") or ""),
            "screenshot_name": str(d.get("screenshot_name") or "")}


def _rules_payload(extra=None):
    rows = _rules_read()
    out = {"ok": True, "count": len(rows), "dedupe_px": RULE_DEDUPE_PX,
           "path": os.path.relpath(TARGETS_JSONL, ROOT),
           "rules": [_rules_public(d) for d in rows]}
    if extra:
        out.update(extra)
    return out


def _rule_int(v, field):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        raise ValueError("%s 必须是整数（收到 %r）" % (field, v))


def _rules_check(rec, label, x, y):
    """校验 label/x/y，返回 (label, x, y)；不合格抛 ValueError（调用方转 400）。"""
    label = str(rec.get("label") or "") if label is None else str(label)
    label = label.strip()
    if not label:
        raise ValueError("规则名不能为空")
    xv = x if (x is not None and str(x).strip() != "") else rec.get("x")
    yv = y if (y is not None and str(y).strip() != "") else rec.get("y")
    nx, ny = _rule_int(xv, "x"), _rule_int(yv, "y")
    w = _rule_int(rec.get("screen_w") or 0, "screen_w")
    h = _rule_int(rec.get("screen_h") or 0, "screen_h")
    if w <= 0 or h <= 0:
        raise ValueError("这条规则没记屏幕尺寸，无法校验坐标")
    if not (0 <= nx < w):
        raise ValueError("x=%d 超出屏幕范围 0~%d" % (nx, w - 1))
    if not (0 <= ny < h):
        raise ValueError("y=%d 超出屏幕范围 0~%d" % (ny, h - 1))
    return label, nx, ny


def rules_list():
    return _rules_payload(), 200


def rules_update(idx, label=None, note=None, x=None, y=None, who="页面"):
    """改第 idx 条（label/note/x/y）；写前备份。"""
    with _RULES_LOCK:
        rows = _rules_read()
        try:
            i = _rule_int(idx, "idx")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        if not (0 <= i < len(rows)):
            return {"ok": False, "error": "序号 %d 不存在（当前共 %d 条）" % (i, len(rows))}, 404
        rec = rows[i]
        try:
            lbl, nx, ny = _rules_check(rec, label, x, y)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        rec["label"], rec["x"], rec["y"] = lbl, nx, ny
        if note is not None:
            rec["note"] = str(note)
        try:
            bak = _rules_backup()
        except OSError as exc:
            return {"ok": False, "error": "备份失败，文件未改动：%s" % exc}, 500
        try:
            _rules_write(rows)
        except OSError as exc:
            return {"ok": False, "error": "写 %s 失败（备份在 %s）：%s"
                    % (TARGETS_JSONL, os.path.basename(bak), exc)}, 500
        _ctl_log("RULES_UPDATE", who, "idx=%d -> %s @(%d,%d) bak=%s"
                 % (i, lbl, nx, ny, os.path.basename(bak)))
        return _rules_payload({"idx": i, "backup": bak, "rule": _rules_public(rec)}), 200


def rules_delete(idx=None, idxs=None, who="页面"):
    """删一条或多条（idx 或 idxs）；写前备份。"""
    with _RULES_LOCK:
        rows = _rules_read()
        raw = idxs if idxs not in (None, "", []) else ([idx] if idx not in (None, "") else [])
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        want = []
        for v in raw:
            try:
                i = _rule_int(v, "idx")
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}, 400
            if not (0 <= i < len(rows)):
                return {"ok": False, "error": "序号 %d 不存在（当前共 %d 条）" % (i, len(rows))}, 404
            if i not in want:
                want.append(i)
        if not want:
            return {"ok": False, "error": "没给要删的 idx / idxs"}, 400
        removed = [_rules_public(rows[k]) for k in want]
        keep = [r for k, r in enumerate(rows) if k not in want]
        try:
            bak = _rules_backup()
        except OSError as exc:
            return {"ok": False, "error": "备份失败，文件未改动：%s" % exc}, 500
        try:
            _rules_write(keep)
        except OSError as exc:
            return {"ok": False, "error": "写 %s 失败（备份在 %s）：%s"
                    % (TARGETS_JSONL, os.path.basename(bak), exc)}, 500
        _ctl_log("RULES_DELETE", who, "idx=%s 删 %d 条：%d -> %d bak=%s"
                 % (want, len(removed), len(rows), len(keep), os.path.basename(bak)))
        return _rules_payload({"removed": removed, "before": len(rows),
                               "after": len(keep), "backup": bak}), 200


def _rule_xy(d):
    return _rule_int(d.get("x"), "x"), _rule_int(d.get("y"), "y")


def rules_dedupe(who="页面"):
    """同一 serial 且坐标距离 < RULE_DEDUPE_PX 视为同一条：保留最新 ts 的那条，
    label/note 取组内更长的那个。写前备份；删掉的是被合并掉的旧条。"""
    with _RULES_LOCK:
        rows = _rules_read()
        before = len(rows)
        used = [False] * before
        drop, removed, groups = set(), [], 0
        for i in range(before):
            if used[i]:
                continue
            used[i] = True
            try:
                xi, yi = _rule_xy(rows[i])
            except ValueError:
                continue
            grp = [i]
            for j in range(i + 1, before):
                if used[j]:
                    continue
                if str(rows[j].get("serial") or "") != str(rows[i].get("serial") or ""):
                    continue
                try:
                    xj, yj = _rule_xy(rows[j])
                except ValueError:
                    continue
                if (xj - xi) ** 2 + (yj - yi) ** 2 < RULE_DEDUPE_PX ** 2:
                    used[j] = True
                    grp.append(j)
            if len(grp) < 2:
                continue
            best = max(grp, key=lambda k: (str(rows[k].get("ts") or ""), k))
            lbl = max((str(rows[k].get("label") or "") for k in grp), key=len)
            nte = max((str(rows[k].get("note") or "") for k in grp), key=len)
            if lbl:
                rows[best]["label"] = lbl
            if nte:
                rows[best]["note"] = nte
            for k in grp:
                if k != best:
                    drop.add(k)
                    removed.append(_rules_public(rows[k]))
            groups += 1
        keep = [r for k, r in enumerate(rows) if k not in drop]
        out = {"ok": True, "before": before, "after": len(keep),
               "removed": removed, "merged_groups": groups, "backup": None,
               "path": os.path.relpath(TARGETS_JSONL, ROOT)}
        if not drop:
            out["note"] = "没有相距 < %gpx 的同设备规则，文件未改动" % RULE_DEDUPE_PX
            return _rules_payload({k: out[k] for k in ("before", "after", "removed",
                                                       "merged_groups", "backup", "note")}), 200
        try:
            bak = _rules_backup()
        except OSError as exc:
            return {"ok": False, "error": "备份失败，文件未改动：%s" % exc}, 500
        try:
            _rules_write(keep)
        except OSError as exc:
            return {"ok": False, "error": "写 %s 失败（备份在 %s）：%s"
                    % (TARGETS_JSONL, os.path.basename(bak), exc)}, 500
        out["backup"] = bak
        _ctl_log("RULES_DEDUPE", who, "合并 %d 组：%d -> %d bak=%s"
                 % (groups, before, len(keep), os.path.basename(bak)))
        return _rules_payload({k: out[k] for k in ("before", "after", "removed",
                                                   "merged_groups", "backup")}), 200


def rules_backup_now(who="页面"):
    """【全部备份并重载】：原地做一次完整备份，然后重读列表给前端。"""
    with _RULES_LOCK:
        if not os.path.exists(TARGETS_JSONL):
            return {"ok": False, "error": "找不到 %s"
                    % os.path.relpath(TARGETS_JSONL, ROOT)}, 404
        try:
            bak = _rules_backup()
        except OSError as exc:
            return {"ok": False, "error": "备份失败：%s" % exc}, 500
        _ctl_log("RULES_BACKUP", who, os.path.basename(bak))
        return _rules_payload({"backup": bak}), 200


# ---- 实时状态（auto.py 写的 state/live.json）-------------------

def read_live(serial=None):
    """「正在执行」卡片：**先读这台设备自己的 state/live-<san>.json**，
    读不到再退回老的全局 state/live.json（兼容 auto.py 现在只写一份）。

    2026-11-02 用户要求：两台一起跑时单份 live.json 会互相覆盖，所以 auto.py 随后会
    按设备各写一份（<san> 同 _san()）；dash 这边两种都读，老证据照常显示。
    """
    s = str(serial or "").strip() or current_serial()
    for path in live_paths(s):
        d = _read_json(path)
        if not isinstance(d, dict):
            continue
        if path == LIVE_JSON and str(d.get("serial") or "").strip() not in ("", s):
            continue        # 老的单份 live.json 写着别台设备 -> 不算这台的
        dt = _dt(d.get("ts"))
        if dt is None:
            try:
                dt = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                dt = None
        age = None if dt is None else round((datetime.now() - dt).total_seconds(), 1)
        return {"ts": d.get("ts") or "", "serial": d.get("serial") or s,
                "phase": d.get("phase") or "", "detail": d.get("detail") or "",
                "age_s": age, "stale": bool(age is not None and age > LIVE_STALE_S),
                "file": os.path.relpath(path, ROOT)}
    return None


# ---- 实时投屏（约 2 帧/秒，480px 宽 JPEG）---------------------

_LIVE = {"want": DEFAULT_SERIAL, "frames": {}, "err": {}, "fps": 2.0,
         "thread": None, "stop": None, "started": None, "grabs": 0, "fails": 0,
         "enabled": not os.path.exists(SCREEN_OFF_FLAG)}
LIVE_OFF_MSG = "实时投屏已关闭（不抓帧）"


def screen_live_set(on):
    """开关实时投屏：关掉后连 adb screencap 都不再发（手机不发热，也不占 adb 传输通道）。

    2026-11-02 用户要求。开关状态落盘到 state/screen_off.flag（存在 = 关），
    这样重启 dash 也会记住你的选择，而不是一重启又开始抓。
    """
    on = bool(on)
    _LIVE["enabled"] = on
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if on:
            if os.path.exists(SCREEN_OFF_FLAG):
                os.remove(SCREEN_OFF_FLAG)
        else:
            with open(SCREEN_OFF_FLAG, "w", encoding="utf-8") as f:
                f.write("%s 页面关闭实时投屏\n"
                        % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except OSError as exc:
        return {"ok": False, "on": _LIVE["enabled"], "error": "写开关失败：%s" % exc}
    return {"ok": True, "on": _LIVE["enabled"]}


def _san(serial):
    return (serial or "dev").replace(":", "_").replace(".", "-")


def _to_jpeg(png, width=480, quality=60):
    """PNG 帧 -> 宽 480 的 JPEG（page 上够看清；没有 PIL 就原样回 PNG）。"""
    try:
        import io as _io
        from PIL import Image
        im = Image.open(_io.BytesIO(png))
        w, h = int(im.width), int(im.height)
        if w > width:
            im = im.resize((width, max(1, int(h * width / w))), Image.BILINEAR)
        buf = _io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=quality)
        return buf.getvalue(), w, h
    except Exception:  # noqa: BLE001
        return png, None, None


def _grab_jpeg(serial):
    data, err = _adb(serial, ["exec-out", "screencap", "-p"], timeout=12, binary=True)
    if not data or len(data) < 2000:          # 黑帧 / 设备忙
        return None, err or "空帧", None, None
    jpg, w, h = _to_jpeg(data)
    return jpg, err, w, h


def _live_loop(stop_evt):
    while not stop_evt.is_set():
        t0 = time.time()
        if not _LIVE["enabled"]:       # 关掉投屏：一次 screencap 都不发（用户 2026-11-02 要求）
            time.sleep(0.3)
            continue
        serial = _LIVE["want"] or DEFAULT_SERIAL
        jpg, err, w, h = _grab_jpeg(serial)
        now = time.time()
        if jpg:
            _LIVE["frames"][serial] = {"bytes": jpg, "at": now, "n": len(jpg), "w": w, "h": h}
            _LIVE["grabs"] += 1
            _LIVE["err"].pop(serial, None)
        else:
            _LIVE["fails"] += 1
            _LIVE["err"][serial] = err or "抓帧失败"
        time.sleep(max(0.0, 0.5 - (time.time() - t0)))


def live_start():
    if _LIVE["thread"] is not None:
        return
    ev = threading.Event()
    _LIVE["stop"] = ev
    _LIVE["started"] = datetime.now()
    th = threading.Thread(target=_live_loop, args=(ev,), daemon=True, name="live-screen")
    _LIVE["thread"] = th
    th.start()


def live_should_show(serial):
    _LIVE["want"] = serial or DEFAULT_SERIAL


def live_frame(serial):
    """取一帧；还没有当前设备的帧就同步抓一次（有界等待），失败返回 (None, 原因)。"""
    serial = serial or DEFAULT_SERIAL
    _LIVE["want"] = serial
    if not _LIVE["enabled"]:
        # 关掉时不要把「上一次的旧帧」当成实时画面，也不要为了这张图去抓帧
        return None, LIVE_OFF_MSG
    fr = _LIVE["frames"].get(serial)
    if fr and time.time() - fr["at"] < 5.0:
        return fr, ""
    for _ in range(12):
        time.sleep(0.15)
        fr = _LIVE["frames"].get(serial)
        if fr and time.time() - fr["at"] < 3.0:
            return fr, ""
    jpg, err, w, h = _grab_jpeg(serial)
    if not jpg:
        return None, (err or _LIVE["err"].get(serial) or "抓帧失败（设备忙或黑屏）")
    fr = {"bytes": jpg, "at": time.time(), "n": len(jpg), "w": w, "h": h}
    _LIVE["frames"][serial] = fr
    return fr, ""


def live_state():
    now = time.time()
    out = {"want": _LIVE["want"], "grabs": _LIVE["grabs"], "fails": _LIVE["fails"],
           "enabled": bool(_LIVE["enabled"]), "fps": _LIVE["fps"],
           "since": _LIVE["started"].strftime("%H:%M:%S") if _LIVE["started"] else None,
           "frames": {}}
    for s in DEVICE_LABELS:
        fr = _LIVE["frames"].get(s)
        out["frames"][s] = {"age_s": None if not fr else round(now - fr["at"], 1),
                            "bytes": None if not fr else fr["n"],
                            "size": None if not fr or not fr.get("w") else "%sx%s" % (fr["w"], fr["h"]),
                            "err": _LIVE["err"].get(s) or ""}
    return out


# ---- 直接操作手机（只在页面点击时发生；只读投屏不动设备）--------

_KEY_OPS = {"back": "4", "home": "3", "recents": "187"}     # 白名单：绝无电源键/音量键
_SWIPES = {"swipe_down": (720, 900, 720, 1900, 250),
           "swipe_up": (720, 1900, 720, 900, 250)}
OP_LABELS = {"tap": "点击", "back": "返回", "home": "主页", "recents": "最近任务",
             "swipe_up": "上滑一下", "swipe_down": "下滑一下"}
SHOT_KEEP_MAX = 500
_DEV_SIZE = {}


def dev_size(serial):
    """设备像素尺寸（点图换算用）。"""
    if serial in _DEV_SIZE:
        return _DEV_SIZE[serial]
    out, _err = _adb(serial, ["shell", "wm", "size"], timeout=15)
    m = re.search(r"(\d+)x(\d+)", out or "")
    size = (int(m.group(1)), int(m.group(2))) if m else (1440, 3200)
    _DEV_SIZE[serial] = size
    return size


def _grab_shot(serial, kind):
    """存一张 PNG 到 coin11/targets/shots/<YYYYMMDD>/，返回相对路径（失败返回空串）。"""
    day = datetime.now().strftime("%Y%m%d")
    d = os.path.join(SNAP_DIR, day)
    _ensure_dir(d)
    name = "%s_%s_%s.png" % (datetime.now().strftime("%H%M%S_%f"), _san(serial), kind)
    p = os.path.join(d, name)
    data, _err = _adb(serial, ["exec-out", "screencap", "-p"], timeout=20, binary=True)
    if not data or len(data) < 2000:
        return ""
    try:
        with open(p, "wb") as fh:
            fh.write(data)
    except OSError:
        return ""
    return os.path.relpath(p, ROOT)


def _prune_shots(keep=SHOT_KEEP_MAX):
    files = []
    for root, _dirs, names in os.walk(SNAP_DIR):
        for n in names:
            if n.lower().endswith((".png", ".jpg", ".jpeg")):
                p = os.path.join(root, n)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    if len(files) <= keep:
        return 0
    files.sort()
    removed = 0
    for _mt, p in files[:len(files) - keep]:
        try:
            os.remove(p)
            removed += 1
        except OSError:
            pass
    return removed


def _shot_url(rel):
    return "/shot?p=" + urllib.parse.quote(os.path.join(ROOT, rel)) if rel else ""


# ---------------------------------------------------------------- 自定义脚本 / 录制
# 契约：coin11/docs/改造计划-20261102.md 第 7.2 节（字段名以文档为准，两边不许各写一套）。
# 面板只读 panel_done.json / scripts_state.json（auto.py 写），只写 record_draft.json 与 scripts/*.json。

SCRIPTS_DIR = os.path.join(COIN, "scripts")
SCRIPTS_DEL_DIR = os.path.join(SCRIPTS_DIR, "_deleted")
SCRIPT_STATE_JSON = os.path.join(STATE_DIR, "scripts_state.json")   # auto.py 写，面板只读
PANEL_DONE_JSON = os.path.join(STATE_DIR, "panel_done.json")        # auto.py 写，面板只读
RECORD_DRAFT_JSON = os.path.join(STATE_DIR, "record_draft.json")    # 录制草稿：面板自己写

#: 7.2 契约：steps 里只允许这 7 种 op
SCRIPT_OPS = ("tap", "back", "home", "recents", "swipe_up", "swipe_down", "wait")
_SCRIPT_ID_RE = re.compile(r"^[a-z0-9_]{1,48}$")
#: 与 coin11/custom_script.py 的 MAX_WAIT_S 对齐（那边超了会被静默截断，面板这边先拦住）
_MAX_WAIT_S = 60.0
_REC_LOCK = threading.Lock()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _now_s():
    """脚本/草稿里的时间戳：与 7.2 契约样例一致（2026-11-02T18:00:00）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _rel(p):
    return os.path.relpath(p, ROOT)


def _write_json_atomic(path, obj):
    """先写临时文件再 replace：dash 被强杀也不会留下半个 JSON。"""
    _ensure_dir(os.path.dirname(path) or ".")
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


# ---- 面板完成记忆（auto.py 写；面板只用它显示「今天已完成」，绝不写它）----

def panel_done_read(serial=None):
    """读「面板今天已完成」记录，**v1 老格式与 v2 按设备格式都认**（2026-11-02）。

    v1: {"v":1,"panels":{"coin":"2026-11-02"}}                     ← 没有设备维度
    v2: {"v":2,"panels":{"192.168.1.8:5555":{"coin":"2026-11-02"}}} ← auto.py 现在写这个
    serial 为空时按「当前选中设备」；v1 的记录只能当"这台设备也做过"（老数据没有设备信息）。
    """
    d = _read_json(PANEL_DONE_JSON)
    d = d if isinstance(d, dict) else {}
    raw = d.get("panels") if isinstance(d.get("panels"), dict) else {}
    ver = d.get("v") or 1
    today = _today()
    me = (serial or "").strip()
    per_dev = {k: v for k, v in raw.items() if isinstance(v, dict)}
    flat = {k: v for k, v in raw.items() if isinstance(v, str)}
    mine = per_dev.get(me) or {}
    rows = []
    for key, label in PANELS:
        date = str(mine.get(key) or "").strip()
        src = "本设备"
        if not date and flat:          # v1 老数据：没有设备信息，按"做过"显示但标注来源
            date = str(flat.get(key) or "").strip()
            src = "旧格式(无设备)"
        rows.append({"key": key, "label": label, "date": date or None,
                     "done": date == today, "source": src if date else ""})
    return {"file": _rel(PANEL_DONE_JSON), "v": ver, "today": today,
            "device": me or None, "panels": rows, "devices": sorted(per_dev.keys())}


# ---- 脚本运行状态（auto.py 写；面板只读）----

def scripts_state_runs():
    d = _read_json(SCRIPT_STATE_JSON)
    d = d if isinstance(d, dict) else {}
    runs = d.get("runs")
    return runs if isinstance(runs, dict) else {}


# ---- steps 校验：只认契约 7.2 的字段，不另造 ----

def _script_steps(raw):
    """把 steps 规整成契约字段；返回 (steps, error)。"""
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return None, "steps 必须是数组"
    out = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            return None, "第 %d 步不是对象" % i
        op = str(s.get("op") or "").strip().lower()
        if op not in SCRIPT_OPS:
            return None, ("第 %d 步 op=%r 不在白名单（只允许 %s）"
                          % (i, op, "/".join(SCRIPT_OPS)))
        st = {"op": op}
        if op == "tap":
            try:
                st["x"], st["y"] = int(float(s.get("x"))), int(float(s.get("y")))
            except (TypeError, ValueError):
                return None, "第 %d 步 tap 需要数字 x/y" % i
            if st["x"] < 0 or st["y"] < 0:
                return None, "第 %d 步 tap 坐标不能是负数" % i
        elif op in _SWIPES:
            dflt = dict(zip(("x1", "y1", "x2", "y2", "dur"), _SWIPES[op]))
            for k, dv in dflt.items():
                try:
                    st[k] = int(float(s.get(k))) if s.get(k) is not None else dv
                except (TypeError, ValueError):
                    return None, "第 %d 步 %s 的 %s 需要数字" % (i, op, k)
        elif op == "wait":
            try:
                secs = float(s.get("secs"))
            except (TypeError, ValueError):
                return None, "第 %d 步 wait 需要数字 secs" % i
            if not (0.1 <= secs <= _MAX_WAIT_S):
                return None, "第 %d 步 wait 的 secs 要在 0.1~%g 之间" % (i, _MAX_WAIT_S)
            st["secs"] = round(secs, 2)
        note = str(s.get("note") or "").strip()
        if note:
            st["note"] = note
        out.append(st)
    return out, ""


def script_norm(rec, sid=""):
    """把一个脚本文件规整成契约字段（只说 7.2 A 那 8 个字段）。"""
    rec = rec if isinstance(rec, dict) else {}
    steps = []
    for s in (rec.get("steps") or []):
        one, _err = _script_steps([s])
        if one:
            steps.extend(one)
    try:
        order = int(float(rec.get("order")))
    except (TypeError, ValueError):
        order = None
    return {"id": str(rec.get("id") or sid).strip(),
            "name": str(rec.get("name") or rec.get("id") or sid or "").strip(),
            "created": str(rec.get("created") or "").strip(),
            "updated": str(rec.get("updated") or "").strip(),
            "order": order,
            "enabled": bool(rec.get("enabled", True)),
            "note": str(rec.get("note") or ""),
            "steps": steps}


def _script_path(sid):
    return os.path.join(SCRIPTS_DIR, "%s.json" % sid)


def _script_files():
    try:
        names = os.listdir(SCRIPTS_DIR)
    except OSError:
        return []
    return sorted(n for n in names
                  if n.lower().endswith(".json") and os.path.isfile(os.path.join(SCRIPTS_DIR, n)))


def _script_order_next():
    mx = 0
    for n in _script_files():
        rec = _read_json(os.path.join(SCRIPTS_DIR, n)) or {}
        try:
            mx = max(mx, int(float(rec.get("order"))))
        except (TypeError, ValueError):
            pass
    return mx + 10


def scripts_list(include_steps=True, serial=None):
    """GET /api/scripts 的载荷：脚本列表 + 面板完成状态 + 录制草稿（全部只读 state）。

    serial：面板「今天已完成」要按设备看（panel_done.json 是 v2 按设备记的）。
    """
    today = _today()
    runs = scripts_state_runs()
    items = []
    for n in _script_files():
        rec = script_norm(_read_json(os.path.join(SCRIPTS_DIR, n)), os.path.splitext(n)[0])
        sid = rec["id"] or os.path.splitext(n)[0]
        run = runs.get(sid) if isinstance(runs.get(sid), dict) else {}
        item = dict(rec)
        item["id"] = sid
        if not include_steps:
            item.pop("steps", None)
        item["steps_count"] = len(rec["steps"])
        item["file"] = _rel(_script_path(sid))
        item["order"] = rec["order"] if rec["order"] is not None else 9999
        item["last"] = {"last_ok_date": str(run.get("last_ok_date") or ""),
                        "last_try": str(run.get("last_try") or ""),
                        "last_status": str(run.get("last_status") or ""),
                        "last_error": str(run.get("last_error") or ""),
                        "steps_done": run.get("steps_done")}
        item["ok_today"] = item["last"]["last_ok_date"] == today
        items.append(item)
    items.sort(key=lambda r: (r["order"], r["id"]))
    return {"ok": True, "dir": _rel(SCRIPTS_DIR), "state_file": _rel(SCRIPT_STATE_JSON),
            "today": today, "count": len(items), "ops": list(SCRIPT_OPS),
            "queue_hint": "排队只跑 enabled 且今天没成功过的（按 order、id 排）；单跑不受限",
            "scripts": items, "panel_done": panel_done_read(serial),
            "draft": record_draft_read()}


def script_save(body):
    """写/改一个脚本（存在就合并：只覆盖传进来的字段）。"""
    body = body if isinstance(body, dict) else {}
    sid = str(body.get("id") or "").strip().lower()
    if not _SCRIPT_ID_RE.match(sid):
        return {"ok": False, "error": "id 只许小写字母/数字/下划线（a-z0-9_），1~48 个字符"}, 400
    path = _script_path(sid)
    cur = _read_json(path)
    exists = isinstance(cur, dict)
    rec = script_norm(cur, sid) if exists else {"id": sid, "name": sid, "created": _now_s(),
                                                "updated": "", "order": None, "enabled": True,
                                                "note": "", "steps": []}
    rec["id"] = sid
    if str(body.get("name") or "").strip():
        rec["name"] = str(body["name"]).strip()
    if not rec["name"]:
        rec["name"] = sid
    if "note" in body:
        rec["note"] = str(body.get("note") or "")
    if "enabled" in body:
        rec["enabled"] = bool(body["enabled"])
    if "order" in body and body.get("order") is not None:
        try:
            rec["order"] = int(float(body["order"]))
        except (TypeError, ValueError):
            return {"ok": False, "error": "order 需要数字"}, 400
    if "steps" in body:
        steps, err = _script_steps(body.get("steps"))
        if err:
            return {"ok": False, "error": err}, 400
        rec["steps"] = steps
    if rec["order"] is None:
        rec["order"] = _script_order_next()
    rec["updated"] = _now_s()
    if not rec["created"]:
        rec["created"] = rec["updated"]
    try:
        _write_json_atomic(path, rec)
    except OSError as exc:
        return {"ok": False, "error": "写脚本失败：%s" % exc}, 500
    saved = dict(rec)
    saved["steps_count"] = len(rec["steps"])
    return {"ok": True, "script": saved, "file": _rel(path), "created": not exists}, 200


def script_delete(sid):
    sid = str(sid or "").strip().lower()
    if not _SCRIPT_ID_RE.match(sid):
        return {"ok": False, "error": "id 不合法（只许 a-z0-9_）"}, 400
    path = _script_path(sid)
    if not os.path.exists(path):
        return {"ok": False, "error": "没有这个脚本：%s" % _rel(path)}, 404
    _ensure_dir(SCRIPTS_DEL_DIR)
    bak = os.path.join(SCRIPTS_DEL_DIR, "%s.%s.json" % (sid, datetime.now().strftime("%Y%m%d-%H%M%S")))
    try:
        shutil.copy2(path, bak)
        os.remove(path)
    except OSError as exc:
        return {"ok": False, "error": "删除失败：%s" % exc}, 500
    return {"ok": True, "id": sid, "backup": _rel(bak), "count": len(_script_files())}, 200


def script_order(sid, delta=None, order=None):
    """调 order：delta=±1 与邻居换位；给了 order 就按数值重排。步长恒为 10。"""
    sid = str(sid or "").strip().lower()
    if not _SCRIPT_ID_RE.match(sid):
        return {"ok": False, "error": "id 不合法（只许 a-z0-9_）"}, 400
    recs = [script_norm(_read_json(os.path.join(SCRIPTS_DIR, n)), os.path.splitext(n)[0])
            for n in _script_files()]
    recs = [r for r in recs if r["id"]]
    if sid not in [r["id"] for r in recs]:
        return {"ok": False, "error": "没有这个脚本：%s" % sid}, 404
    recs.sort(key=lambda r: (r["order"] if r["order"] is not None else 9999, r["id"]))
    ids = [r["id"] for r in recs]
    if order is not None:
        try:
            newv = int(float(order))
        except (TypeError, ValueError):
            return {"ok": False, "error": "order 需要数字"}, 400
        for r in recs:
            if r["id"] == sid:
                r["order"] = newv
        recs.sort(key=lambda r: (r["order"] if r["order"] is not None else 9999, r["id"]))
        ids = [r["id"] for r in recs]
        pos_txt = "按数值 %d 重排" % newv
    else:
        try:
            d = int(delta or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "delta 需要数字"}, 400
        i = ids.index(sid)
        j = max(0, min(len(ids) - 1, i + (1 if d > 0 else (-1 if d < 0 else 0))))
        if j == i:
            return {"ok": True, "id": sid, "order": ids, "changed": [],
                    "note": "已经在%s了" % ("最后" if d > 0 else "最前")}, 200
        ids[i], ids[j] = ids[j], ids[i]
        pos_txt = "上移一位" if d < 0 else "下移一位"
    by_id = dict((r["id"], r) for r in recs)
    now = _now_s()
    changed = []
    for k, one in enumerate(ids, 1):
        rec = by_id[one]
        want = k * 10
        if rec["order"] == want:
            continue
        rec["order"] = want
        rec["updated"] = now
        try:
            _write_json_atomic(_script_path(one), rec)
        except OSError as exc:
            return {"ok": False, "error": "调序写盘失败：%s" % exc}, 500
        changed.append(one)
    return {"ok": True, "id": sid, "order": ids, "changed": changed, "note": pos_txt}, 200


# ---- 录制草稿（面板自己写；steps 字段与 7.2 完全一致）----

def record_draft_read():
    d = _read_json(RECORD_DRAFT_JSON)
    d = d if isinstance(d, dict) else {}
    steps = []
    for s in (d.get("steps") or []):
        one, _err = _script_steps([s])
        if one:
            steps.extend(one)
    return {"file": _rel(RECORD_DRAFT_JSON), "v": d.get("v") or 1,
            "active": bool(d.get("active")), "started": str(d.get("started") or ""),
            "updated": str(d.get("updated") or ""), "serial": str(d.get("serial") or ""),
            "steps": steps, "count": len(steps)}


def _draft_write(draft):
    draft["v"] = 1
    draft["updated"] = _now_s()
    _write_json_atomic(RECORD_DRAFT_JSON, draft)
    return record_draft_read()


def _record_step_of(op, x=None, y=None, note=""):
    """把一次成功的 panel_op 变成契约里的一步（滑动坐标直接取 _SWIPES）。"""
    st = {"op": op}
    if op == "tap":
        st["x"], st["y"] = int(x), int(y)
    elif op in _SWIPES:
        st.update(dict(zip(("x1", "y1", "x2", "y2", "dur"), _SWIPES[op])))
    st["note"] = (note or OP_LABELS.get(op, op)).strip()
    return st


def record_append_op(op, x=None, y=None, note=""):
    """panel_op 成功后调用：录制中就往草稿里追加一步；没在录制返回 None。"""
    with _REC_LOCK:
        d = record_draft_read()
        if not d["active"]:
            return None
        raw = _read_json(RECORD_DRAFT_JSON) or {}
        steps = list(raw.get("steps") or [])
        steps.append(_record_step_of(op, x, y, note))
        return _draft_write({"active": True,
                             "started": str(raw.get("started") or _now_s()),
                             "serial": str(raw.get("serial") or ""), "steps": steps})


def record_start(serial="", clear=False):
    with _REC_LOCK:
        d = record_draft_read()
        steps = [] if clear else list(d["steps"])
        started = d["started"] if (steps and d["started"]) else _now_s()
        out = _draft_write({"active": True, "started": started,
                            "serial": serial or d["serial"] or current_serial(), "steps": steps})
    return {"ok": True, "draft": out,
            "note": "录制中：在「操作」模式点画面 / 点返回·主页·滑动，每成功一次记一步"}, 200


def record_undo():
    with _REC_LOCK:
        d = record_draft_read()
        if not d["steps"]:
            return {"ok": False, "error": "草稿里没有步骤可以撤销", "draft": d}, 400
        raw = _read_json(RECORD_DRAFT_JSON) or {}
        steps = list(raw.get("steps") or [])[:-1]
        out = _draft_write({"active": bool(d["active"]), "started": d["started"],
                            "serial": d["serial"], "steps": steps})
    return {"ok": True, "draft": out, "undo": d["steps"][-1]}, 200


def record_wait(secs):
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        return {"ok": False, "error": "等待秒数需要数字"}, 400
    if not (0.1 <= secs <= _MAX_WAIT_S):
        return {"ok": False, "error": "等待秒数要在 0.1~%g 之间" % _MAX_WAIT_S}, 400
    with _REC_LOCK:
        d = record_draft_read()
        if not d["active"]:
            return {"ok": False, "error": "先点【● 开始录制】，再插入等待", "draft": d}, 400
        raw = _read_json(RECORD_DRAFT_JSON) or {}
        steps = list(raw.get("steps") or [])
        steps.append({"op": "wait", "secs": round(secs, 2), "note": "等 %g 秒" % secs})
        out = _draft_write({"active": True, "started": d["started"],
                            "serial": d["serial"], "steps": steps})
    return {"ok": True, "draft": out}, 200


def record_clear():
    with _REC_LOCK:
        out = _draft_write({"active": False, "started": "", "serial": "", "steps": []})
    return {"ok": True, "draft": out}, 200


def _slug_id(name):
    s = re.sub(r"[^a-z0-9_]+", "_", str(name or "").strip().lower()).strip("_")
    return s[:48]


def record_stop(name="", sid="", note="", overwrite=False, discard=False):
    """结束录制：把草稿存成 coin11/scripts/<id>.json（契约 7.2 A）。"""
    with _REC_LOCK:
        d = record_draft_read()
        if discard or not d["steps"]:
            out = _draft_write({"active": False, "started": d["started"],
                                "serial": d["serial"], "steps": []})
            return {"ok": True, "saved": False, "draft": out,
                    "note": "已丢弃草稿，没存脚本" if discard else "草稿是空的，没存脚本"}, 200
        name = str(name or "").strip()
        want = str(sid or "").strip().lower() or _slug_id(name)
        if not want:
            want = "rec_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        if not _SCRIPT_ID_RE.match(want):
            return {"ok": False, "id": want, "draft": d,
                    "error": "id 只许小写字母/数字/下划线（a-z0-9_），1~48 个字符：%r" % want}, 400
        if os.path.exists(_script_path(want)) and not overwrite:
            return {"ok": False, "id": want, "draft": d,
                    "error": "已经有 %s.json 了（换个名字，或覆盖保存）" % want}, 409
        cur = _read_json(_script_path(want))
        cur = script_norm(cur, want) if isinstance(cur, dict) else None
        now = _now_s()
        rec = {"id": want,
               "name": name or (cur["name"] if cur else want),
               "created": (cur["created"] if cur and cur["created"] else now),
               "updated": now,
               "order": (cur["order"] if cur and cur["order"] is not None else _script_order_next()),
               "enabled": (cur["enabled"] if cur else True),
               "note": str(note or (cur["note"] if cur else "")),
               "steps": d["steps"]}
        try:
            _write_json_atomic(_script_path(want), rec)
        except OSError as exc:
            return {"ok": False, "error": "写脚本失败：%s" % exc, "draft": d}, 500
        out = _draft_write({"active": False, "started": d["started"],
                            "serial": d["serial"], "steps": []})
    saved = dict(rec)
    saved["steps_count"] = len(rec["steps"])
    return {"ok": True, "saved": True, "script": saved, "file": _rel(_script_path(want)),
            "draft": out}, 200


def panel_op(serial, op, x=None, y=None, note=""):
    """页面上的一次真实操作：前后各存一张图 + 写 manual_log.jsonl（source=panel）。"""
    serial = serial or current_serial()
    op = str(op or "").strip().lower()
    if op not in ("tap", "back", "home", "recents", "swipe_up", "swipe_down"):
        return {"ok": False, "error": "不支持的操作 %r（只允许 tap/back/home/recents/swipe_up/swipe_down）"
                                      "；本面板没有电源键、音量键、文本注入" % op}, 400
    if op == "tap":
        try:
            x, y = int(float(x)), int(float(y))
        except (TypeError, ValueError):
            return {"ok": False, "error": "点击需要数字 x/y"}, 400
        dw, dh = dev_size(serial)
        if not (0 <= x < dw and 0 <= y < dh):
            return {"ok": False, "error": "坐标 (%s,%s) 超出设备 %dx%d" % (x, y, dw, dh)}, 400
    before = _grab_shot(serial, "before")
    if op == "tap":
        out, err = _adb(serial, ["shell", "input", "tap", x, y], timeout=25)
    elif op in _KEY_OPS:
        out, err = _adb(serial, ["shell", "input", "keyevent", _KEY_OPS[op]], timeout=25)
    else:
        x1, y1, x2, y2, dur = _SWIPES[op]
        out, err = _adb(serial, ["shell", "input", "swipe", x1, y1, x2, y2, dur], timeout=30)
    time.sleep(0.6)
    after = _grab_shot(serial, "after")
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "serial": serial,
           "device": DEVICE_LABELS.get(serial, ""), "source": "panel", "action": op,
           "x": x if op == "tap" else None, "y": y if op == "tap" else None,
           "note": note or OP_LABELS.get(op, op),
           "shot_before": before, "shot_after": after,
           "shot_urls": [u for u in (_shot_url(before), _shot_url(after)) if u]}
    _ensure_dir(SNAP_DIR)
    try:
        with open(MANUAL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        return {"ok": False, "error": "写操作记录失败：%s" % exc}, 500
    _CFG["last_touch"][serial] = datetime.now()
    _prune_shots()
    if out is None:
        return {"ok": False, "error": "设备没有响应：%s（记录已写）" % err, "record": rec}, 500
    # 2026-11-02 用户要求（4A 录制）：这一下在手机上真的做成了 → 录制中就往草稿追加一步
    draft = record_append_op(op, x, y, rec.get("note") or "")
    return {"ok": True, "record": rec, "label": OP_LABELS.get(op, op),
            "before": before, "after": after,
            "record_draft": {"active": bool(draft and draft.get("active")),
                             "count": (draft or {}).get("count") or 0}}, 200


# ---- 任务清单（最新扫描）+ 指定下一轮做哪个 -------------------

HINT_JSON = os.path.join(STATE_DIR, "task_hint.json")


def collect_tasks():
    """读每个面板最新一次全表扫描（*_full_*.json），列出所有行供用户挑。

    多设备（2026-11-02）：按目录（= 每台设备）分别找最新，同一个面板取**时间最新**
    的那一台那份 —— 不把两台的扫描行混成一张表（混了「做这个」会指错设备）。
    """
    dirs = session_dirs()
    out = {"rows": [], "panels": {}, "error": "",
           "dirs": [os.path.basename(d) for d in dirs]}
    if not dirs:
        out["error"] = "没有 evidence/*-auto 目录（还没跑过）"
        return out
    best = {}
    for sdir in dirs:
        sd = os.path.join(sdir, "scans")
        try:
            names = [n for n in os.listdir(sd) if n.lower().endswith(".json") and "_full_" in n]
        except OSError:
            continue
        for n in names:
            p = os.path.join(sd, n)
            d = _read_json(p)
            if not isinstance(d, dict):
                continue
            what = (d.get("what") or ("coin" if n.startswith("coin") else "race")).strip().lower()
            t = _dt(d.get("started"))
            if t is None:
                try:
                    t = datetime.fromtimestamp(os.path.getmtime(p))
                except OSError:
                    t = datetime(1970, 1, 1)
            cur = best.get(what)
            if cur is None or t > cur[0]:
                best[what] = (t, d, n, os.path.basename(sdir))
    if not best:
        out["error"] = "还没有扫描记录（evidence/*-auto/scans）"
        return out
    for key, label in PANELS:
        if key not in best:
            continue
        t, d, n, src = best[key]
        out["panels"][key] = {"label": label, "file": n, "at": t.strftime("%m-%d %H:%M"),
                              "device": d.get("device") or "", "dir": src,
                              "progress": d.get("progress"), "screens": d.get("screens")}
        for i, r in enumerate(d.get("rows") or []):
            out["rows"].append({
                "panel": key, "panel_label": label,
                "key": r.get("key") or r.get("name") or "",
                "name": r.get("name") or "(未识别行)",
                "sub": r.get("sub") or "",
                "n": r.get("n"), "m": r.get("m"),
                "kind": r.get("kind") or "?",
                "secs": r.get("secs"),
                "doable": bool(r.get("doable")),
                "why": (r.get("why") or "").strip(),
                "button": r.get("button") or "",
                "scan_at": t.strftime("%m-%d %H:%M"),
                "scan_file": n,
                "idx": i,
            })
    out["rows"].sort(key=lambda x: (0 if x["doable"] else 1,
                                    [k for k, _ in PANELS].index(x["panel"]) if x["panel"] in
                                    [k for k, _ in PANELS] else 9, x["idx"]))
    return out


def hint_get():
    d = _read_json(HINT_JSON)
    if not isinstance(d, dict) or not d.get("row_key"):
        return None
    return {"serial": d.get("serial") or "", "panel": d.get("panel") or "",
            "panel_label": dict(PANELS).get(d.get("panel") or "", d.get("panel") or ""),
            "row_key": d.get("row_key") or "", "name": d.get("name") or "",
            "n": d.get("n"), "m": d.get("m"), "mode": d.get("mode") or "once",
            "ts": d.get("ts") or "", "by": d.get("by") or ""}


def hint_set(panel, row_key, name="", n=None, m=None, serial="", who="页面"):
    panel = str(panel or "").strip().lower()
    if panel not in [k for k, _ in PANELS]:
        return {"ok": False, "error": "panel 只能是 coin/race"}, 400
    if not str(row_key or "").strip():
        return {"ok": False, "error": "缺少 row_key"}, 400
    rec = {"serial": str(serial or current_serial()), "panel": panel,
           "row_key": str(row_key).strip(), "name": str(name or "").strip(),
           "n": n, "m": m, "mode": "once",
           "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "by": "user"}
    _ensure_dir(STATE_DIR)
    try:
        with open(HINT_JSON, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        return {"ok": False, "error": "写 %s 失败：%s" % (HINT_JSON, exc)}, 500
    _ctl_log("HINT", who, "指定下一轮：%s/%s" % (panel, rec["name"] or rec["row_key"]))
    return {"ok": True, "hint": rec}, 200


def hint_clear(who="页面"):
    gone = False
    try:
        if os.path.exists(HINT_JSON):
            os.remove(HINT_JSON)
            gone = True
    except OSError as exc:
        return {"ok": False, "error": "删 %s 失败：%s" % (HINT_JSON, exc)}, 500
    _ctl_log("HINT_CLEAR", who, "恢复自动选择" if gone else "本来就没有指定")
    return {"ok": True, "cleared": gone}, 200


def console_state(rounds, running):
    """总控台那一大块（暂停/停止、保活、占用、策略、记录、实时状态）。

    2026-11-02 用户要求（两台同时各跑一轮）：每台设备各自一行 —— 谁在跑、pid 多少、
    谁被暂停、各自的 live 是哪份，全部按设备分开算；按钮状态由页面按设备分别显示。
    """
    procs = auto_procs()
    # 2026-11-07 用户反馈（面板多出两个 IP）：历史运行记录里旧端口（如 10-86-111-254_40535）的条目
    # 没按当前设备表过滤，被当成设备卡画了出来。这里只保留当前设备表里的 serial；
    # 旧记录**不删**（文件还在，历史可查），只是不再显示。
    runs = [r for r in runs_snapshot() if r.get("serial") in DEVICE_LABELS]
    run_map = {r["serial"]: r for r in runs}
    _ver = PANEL_VERSION
    lives = {s: read_live(s) for s, _l in DEVICES}
    devs = []
    for serial, label in DEVICES:
        mine = [p for p in procs if serial in (p.get("cmd") or "")]
        pids = [p.get("pid") for p in mine if p.get("pid")]
        kst = keep_state()[serial]
        run = run_map.get(serial) or _run_snapshot(serial)
        paused_dev = bool(run.get("paused"))
        teach_busy = _port_open(TEACH_PORTS.get(serial, 0)) if TEACH_PORTS.get(serial) else False
        if run.get("running") or pids:
            occ = "auto.py 在跑（pid %s）" % (run.get("pid")
                                             or ",".join(str(p) for p in pids))
            cls = "busy"
        elif paused_dev:
            occ, cls = "已暂停", "paused"
        elif kst["on"]:
            occ, cls = "保活开启", "keep"
        elif teach_busy:
            occ, cls = "教学页只读", "teach"
        else:
            occ, cls = "空闲", "idle"
        # 注意：kst["last_at"] 已经是字符串，这里要取 _KEEP 里的原始 datetime，否则 max() 混类型
        touches = [t for t in (_KEEP[serial]["last_at"], _CFG["last_touch"].get(serial)) if t]
        lv = lives.get(serial)
        if lv and lv.get("age_s") is not None:
            try:
                touches.append(datetime.fromtimestamp(
                    time.time() - float(lv["age_s"])))
            except (TypeError, ValueError):
                pass
        last_touch = max(touches).strftime("%H:%M:%S") if touches else None
        dw, dh = dev_size(serial)
        devs.append({"serial": serial, "label": label, "occupy": occ, "occupy_cls": cls,
                     "auto_pids": pids, "keep": kst, "teach_busy": teach_busy,
                     "w": dw, "h": dh, "size": "%dx%d" % (dw, dh),
                     "last_action_at": last_touch,
                     "running": bool(run.get("running")), "pid": run.get("pid"),
                     "paused": paused_dev,
                     "state_txt": run.get("state_txt") or occ,
                     "state_cls": run.get("state_cls") or ("running" if run.get("running")
                                                          else ("paused" if paused_dev else "idle")),
                     "what": run.get("what"), "minutes_label": run.get("minutes_label"),
                     "started_at": run.get("started_at"), "elapsed_s": run.get("elapsed_s"),
                     "out": run.get("out"), "live": lv,
                     "live_detail": (lv or {}).get("detail") or ""})
    running_now = [r for r in runs if r.get("running")]
    paused_devs = [r["label"] for r in runs if r.get("paused") and not r.get("running")]
    if running_now:
        # 顶部状态条：运行中 · 手机1(pid 123) + 手机2(pid 456)（两台都列出来，不只显示一台）
        status = "运行中 · " + " + ".join("%s(pid %s)" % (r["label"], r["pid"])
                                          for r in running_now)
        cls = "running"
        if paused_devs:
            status += " · 已暂停 " + "、".join(paused_devs)
    elif paused_devs:
        status, cls = "已暂停 · " + "、".join(paused_devs), "paused"
    elif _CTL["stopped_at"] and not running:
        status = "已全部停止（%s）" % _CTL["stopped_at"].strftime("%H:%M:%S")
        cls = "stopped"
    elif running:
        status, cls = "运行中", "running"
    else:
        status, cls = "空闲", "idle"
    return {
        "control": {
            "paused": paused_any(), "paused_all": paused(), "running": bool(running),
            "status": status, "cls": cls,
            "running_labels": [r["label"] for r in running_now],
            "paused_labels": paused_devs,
            "stopped_at": _CTL["stopped_at"].strftime("%H:%M:%S") if _CTL["stopped_at"] else None,
            "paused_at": _CTL["paused_at"].strftime("%H:%M:%S") if _CTL["paused_at"] else None,
            "rounds_done": len(rounds),
            "last_ctl": _CTL["last"],
            "pause_flag": os.path.relpath(PAUSE_FLAG, ROOT),
            "adb": adb_path() or "(没找到 adb)",
        },
        "devices": devs,
        "runs": runs,
        "lives": lives,
        "keepalive": keep_state(),
        "keep_intervals": [float(x) for x in KEEP_INTERVALS],
        "jump_policy": jump_get(),
        "manual": manual_list(20),
        "manual_count": len(manual_list(400)),
        "live": lives.get(current_serial()),
        "auto_record": bool(_CFG["auto_record"]),
        "screen": live_state(),
        "tasks": collect_tasks(),
        "hint": hint_get(),
        "mode": ({"kind": "hint", "label": "已指定：%s（下一轮）" % (hint_get() or {}).get("name", ""),
                  "hint": hint_get()} if hint_get() else {"kind": "auto", "label": "自动选择", "hint": None}),
    }


# ---------------------------------------------------------------- 组装状态


def build_state():
    # 2026-11-02 用户要求（多机并发）：run = 兼容老字段的「主」运行状态（有设备在跑就是
    # 最近启动的那台），runs/console.runs = 每台设备各自一条；running 是「有没有任何一台在跑」。
    runs = runs_snapshot()
    running = any(r.get("running") for r in runs)
    run = _run_snapshot()
    run["running"] = bool(run.get("running"))
    live_lines = log_lines(run.get("serial") if run.get("running") else None)
    with _LOCK:
        _reap_locked()
        started_map = {s: (_RUNS.get(s) or {}).get("started") for s in _RUNS}
        live_map = {s: (_RUNS.get(s) or {}).get("proc") is not None for s in _RUNS}
    started_dt = min([started_map[s] for s in started_map
                      if live_map.get(s) and started_map.get(s)] or [None])

    sdirs = session_dirs()            # 所有 *-auto*（含 -手机1 / -手机2），合并显示
    rmeta = {}
    rounds = collect_rounds(sdirs, rmeta)
    scans = collect_scans(sdirs)
    pending = collect_pending()
    navfail = collect_navfail(sdirs)

    counts = {"success": 0, "skipped": 0, "failed": 0, "total": len(rounds)}
    counts_by_dir = {}
    for r in rounds:
        st = r["status"]
        bucket = ("success" if st in ("advanced", "disappeared", "ok")
                  else ("skipped" if st == "skipped" else "failed"))
        counts[bucket] += 1
        # 多设备：先按来源目录（= 每台设备）分开统计，再给总表；不把两台算成一堆
        one = counts_by_dir.setdefault(r.get("dir") or "-",
                                       {"success": 0, "skipped": 0, "failed": 0, "total": 0,
                                        "device": r.get("device") or ""})
        one[bucket] += 1
        one["total"] += 1
        if not one["device"]:
            one["device"] = r.get("device") or ""

    # 「今天」的成败数：面板原来给的是**全部历史**（3036 轮、失败 1018），一点都不直观。
    today_s = datetime.now().strftime("%Y-%m-%d")
    today_counts = {"success": 0, "skipped": 0, "failed": 0, "total": 0}
    for r in rounds:
        when = r.get("_end") or r.get("_start")
        if not when or when.strftime("%Y-%m-%d") != today_s:
            continue
        st = r["status"]
        bucket = ("success" if st in ("advanced", "disappeared", "ok")
                  else ("skipped" if st == "skipped" else "failed"))
        today_counts[bucket] += 1
        today_counts["total"] += 1

    # 最近一轮读到的「行进度 / 金币总数」：进度块用它，不再依赖扫描 JSON（扫描已经不进面板了）
    coin_live = None
    if rounds:
        # 取「最近一轮**真的读到了数**的」：最后那轮常常是 no-doable / aborted，什么都不带，
        # 直接拿它显示就成了「? → ?」，用户看不懂。
        def _has_num(r):
            pair = r.get("coin") or []
            return bool(r.get("before") or r.get("after")
                        or (len(pair) > 0 and pair[0] is not None)
                        or (len(pair) > 1 and pair[1] is not None))

        pick = next((r for r in rounds if _has_num(r)), None)
        r0 = pick or rounds[0]
        pair = r0.get("coin") or []
        when = r0.get("_end") or r0.get("_start")
        coin_live = {
            "has_data": bool(pick),
            "device": r0.get("device") or "",
            "device_label": dev_label(r0.get("device") or ""),
            "at": when.strftime("%m-%d %H:%M") if when else (r0.get("time") or ""),
            "round": r0.get("round"),
            "task": r0.get("task"),
            "before": r0.get("before"),
            "after": r0.get("after"),
            "status": r0.get("status"),
            "coin_before": pair[0] if len(pair) > 0 else None,
            "coin_after": pair[1] if len(pair) > 1 else None,
        }

    panels = []
    for key, label in PANELS:
        sc = scans.get(key) or {}
        prog = sc.get("progress")
        try:
            prog = None if prog is None else float(prog)
        except (TypeError, ValueError):
            prog = None
        pct = None if prog is None else max(0.0, min(100.0, prog / PANEL_MAX * 100.0))
        panels.append({
            "key": key,
            "label": label,
            "progress": prog,
            "progress_txt": "未知" if prog is None else ("%g/%g" % (prog, PANEL_MAX)),
            "pct": None if pct is None else round(pct, 1),
            "coin_total": sc.get("coin_total"),
            "scan_at": sc.get("at"),
            "scan_file": sc.get("file"),
            "rows": sc.get("rows"),
            "doable": sc.get("doable"),
        })

    known = [p for p in panels if p["progress"] is not None]
    if known:
        done = sum(p["progress"] for p in known)
        total = PANEL_MAX * len(known)
        overall = {"done": done, "total": total, "pct": round(done / total * 100.0, 1),
                   "known": len(known), "txt": "%g/%g" % (done, total)}
    else:
        overall = {"done": 0, "total": 0, "pct": None, "known": 0, "txt": "未知"}

    starts = [r["_start"] for r in rounds if r.get("_start")]
    ends = [r["_end"] for r in rounds if r.get("_end")]
    sess_start = min(starts) if starts else None
    if running and started_dt and (sess_start is None or started_dt < sess_start):
        sess_start = started_dt
    if running:
        sess_end = datetime.now()
    else:
        sess_end = max(ends) if ends else (max(starts) if starts else None)
    elapsed_s = None
    if sess_start and sess_end and sess_end >= sess_start:
        elapsed_s = round((sess_end - sess_start).total_seconds(), 1)

    for r in rounds:
        r.pop("_start", None)
        r.pop("_end", None)

    if live_lines:
        lines = live_lines
        src = "面板实时（%s）" % (run.get("label") or run.get("serial") or "?")
    else:
        lines, src = tail_log(sdirs), "run.log"

    # 每台设备各自一份日志：面板实时优先，其次各自的 run.log（每台一个证据目录）
    # 2026-11-04：这段优先级逻辑抽成 logs_by_dev_map()，SSE 推送复用**同一份**实现
    # （推送和轮询给出不一样日志的话，用户看到的就是"一会儿有一会儿没有"）
    logs_by_dev = logs_by_dev_map()
    serial = current_serial()
    log_age_s = None
    log_dir = None
    for _d in reversed(sdirs):
        try:
            log_age_s = round(time.time() - os.path.getmtime(os.path.join(_d, "run.log")), 1)
            log_dir = os.path.basename(_d)
            break
        except OSError:
            continue

    return {
        "ok": True,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_ms": REFRESH_MS,
        "version": PANEL_VERSION,
        "student": STUDENT_MODE,
        # 教学页是单独的 teach.py 服务：打包版没带 -> app_entry.py 设 COIN11_NO_TEACH=1，按钮藏掉
        "teach_page": os.environ.get("COIN11_NO_TEACH") != "1",
        "session": {
            "name": (" + ".join(os.path.basename(d) for d in sdirs) if sdirs else None),
            "dir": rmeta.get("dir"),
            "dirs": [os.path.basename(d) for d in sdirs],
            "rounds": len(rounds),
            "elapsed_s": elapsed_s,
            "serial": serial,
            "log_age_s": log_age_s,
            "log_dir": log_dir,
        },
        "rounds_meta": {
            "dir": rmeta.get("dir"),
            "dirs": rmeta.get("dirs") or [],
            "by_dir": rmeta.get("by_dir") or {},
            "updated": rmeta.get("updated"),
            "updated_age_s": (round(time.time() - rmeta["updated_mtime"], 1)
                              if rmeta.get("updated_mtime") else None),
            "round_dirs": rmeta.get("round_dirs"),
        },
        "counts": counts,
        "counts_by_dir": counts_by_dir,
        "panels": panels,
        "today": today_counts,
        "coin_live": coin_live,
        "overall": overall,
        "rounds": rounds,
        "pending": pending,
        "pending_open": sum(1 for p in pending if not p["answered"]),
        "navfail": navfail,
        "log": {"lines": lines, "source": src},
        # 2026-11-03 用户要求：两台同时跑时，日志与「正在执行」都要能分别看
        # logs: {serial: {lines, source}}；lives 已在 console.lives 里（每台一份）
        "logs": logs_by_dev,
        "run": run,
        "runs": runs,
        "teach_url": teach_url(serial),
        "teach_ports": TEACH_PORTS,
        "state_hits": _STATS["state_hits"],
        # 2026-11-04 新增字段（老的字段一个没动）：实时推送的在场情况，排查 / 验收用
        "stream": {"clients": _STATS.get("stream_clients", 0), "interval_ms": SSE_MS},
        "console": console_state(rounds, running),
        # 2026-11-02 用户要求（4A）：脚本列表 + 面板完成状态 + 录制草稿，随 /api/state 一起刷
        "scripts": scripts_list(),
    }


HTML_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>coin11 总控台</title>
<style>
:root{--bg:#0e1116;--card:#161b22;--line:#232b36;--fg:#e6edf3;--dim:#8b98a8;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}
html,body{height:100vh;margin:0;overflow:hidden;background:var(--bg);color:var(--fg);font:13px/1.45 system-ui,"Segoe UI","Microsoft YaHei",sans-serif}
.app{height:100vh;display:grid;grid-template-columns:var(--leftw,58%) 6px minmax(0,1fr);gap:10px;padding:10px;overflow:hidden}
/* 2026-11-09 用户要求：面板每个模块能自己拖大小/换顺序，且下次打开不用重调。
   · 左右两栏之间加一条可拖的分隔条（宽度记在 localStorage）
   · 右栏每个 .box 显示右下角 resize 手柄（Chromium 原生），顺序靠拖标题 */
.splitter{width:6px;cursor:col-resize;border-radius:3px;background:transparent;transition:background .12s}
.splitter:hover,.splitter.on{background:#58a6ff66}
.col{min-height:0;min-width:0;display:grid;gap:8px;overflow:hidden}
.left{grid-template-rows:minmax(0,1fr) auto}
/* 2026-11-04 用户要求：右栏自己滚 —— 窗口不够高就滚右栏，绝不靠把某个区块压扁来塞下。
   左栏（投屏）是独立一列，一行没动。 */
.right{display:flex;flex-direction:column;gap:0;overflow:hidden;padding-right:2px}
/* 2026-11-09 用户定案（第二版）：不要「每块自己拉大小/拖动排序」了 —— 那样窗口一缩放就错位。
   改成**分界条**模型：所有块用 flex 权重**一起填满右栏**，只在相邻两块的缝里拖分界来分配比例。
   权重存 localStorage；窗口缩放时按比例自适应，永远是占满的。 */
.right>.box{flex:1 1 0;min-height:28px;overflow:hidden}
.right>.box.status{flex:0 0 auto}                       /* 控件区按内容高，固定 */
.right>.box.appcheck,.right>.box.coll{flex:0 0 auto;min-height:0}   /* 收起态只有标题一行 */
.right>.box.coll.open{flex:1.2 1 0}
.right>.box.progress{flex:1.7 1 0}
.right>.box.devstat{flex:1.9 1 0}
.right>.box.now{flex:1.2 1 0}
.right>.box.rounds{flex:2.6 1 0}
/* 相邻两块之间的分界条：上下拖 = 重新分配这两块的高度 */
.vsplit{height:10px;flex:0 0 auto;cursor:row-resize;position:relative;margin:-2px 0}
.vsplit::after{content:"";position:absolute;left:8px;right:8px;top:4px;height:2px;border-radius:2px;background:var(--line);transition:background .12s}
.vsplit:hover::after,.vsplit.on::after{background:#58a6ff99}
/* 轮次的高度由权重决定（见下面的 .right>.box 一组），内容在框内自己滚 */
/* 正在执行：同样按权重分高度，块内自己滚 */
/* 次要区块（App 自检 / 待教 / 导航失败 / 操作记录）：点标题展开收起。
   收起 = 只剩标题 + 条数；展开 = 最多 160px，超出在框内自己滚 */
.box.coll>h3{cursor:pointer;user-select:none}
.box.coll>h3 .chev{display:inline-block;flex:none;color:var(--dim);font-family:ui-monospace,Consolas,monospace;transition:transform .12s}
.box.coll.open>h3 .chev{transform:rotate(90deg)}
.box.coll>h3 .hd-txt{flex:none}
.box.coll>h3 .hd-meta{flex:1;min-width:0;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.box.coll>.body{display:none}
.box.coll.open>.body{display:block;flex:0 1 auto;min-height:0;max-height:160px;overflow:auto}
.box.appcheck.open>.body{max-height:none}
/* 轮次的标题元信息（共 N 轮/来源/更新时间）压成一行，别让它把表格挤走；想看全就悬停 */
.box.rounds>h3 #rounds-meta{flex:1;min-width:0;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-height:0;display:flex;flex-direction:column;overflow:hidden}
.box>h3{margin:0 0 6px;font-size:12px;font-weight:600;color:var(--dim);display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.box>.body{min-height:0;flex:1;overflow:auto}
.srow{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.spacer{flex:1}
.pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);background:#0b0f14;white-space:nowrap}
.pill.on{border-color:#2ea04366;color:var(--ok);background:#12261a}
.pill.off{color:var(--dim)}
.pill.err{color:var(--bad);border-color:#f8514966}
.ctl-status{font-weight:700;font-size:13px;white-space:nowrap}
.ctl-status.paused{color:var(--warn)}
.ctl-status.stopped{color:var(--bad)}
.ctl-status.running{color:var(--ok)}
.ctl-status.idle{color:var(--dim)}
.btn{font:inherit;font-size:13px;padding:5px 10px;border-radius:8px;border:1px solid var(--line);background:#1b222c;color:var(--fg);cursor:pointer;white-space:nowrap}
.btn:disabled{opacity:.42;cursor:not-allowed}
.btn.xs{font-size:12px;padding:4px 8px}
.btn.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
.btn.pause{background:#8a2020;border-color:#f85149;color:#fff;font-weight:700}
.btn.resume{background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:700}
.btn.kill{background:#2a1214;border-color:#f8514966;color:#ffb3ad;font-size:12px;padding:4px 8px}
.btn.ghost{background:transparent;color:var(--dim);font-size:12px;padding:4px 8px}
.btn.small{padding:5px 9px;font-size:12px;text-decoration:none;display:inline-block;color:var(--fg)}
.btn.on{background:#12261a;border-color:#2ea043;color:var(--ok)}
.hint{min-height:15px;color:var(--warn);font-size:12px}
.tiny{font-size:11px}
.dim{color:var(--dim)}
.mono{font-family:ui-monospace,Consolas,monospace}
.live{font-size:15px;font-weight:700;color:var(--fg)}
.live b{color:var(--acc)}
.live.stale{color:var(--dim);font-weight:400}
.tag{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);white-space:nowrap;display:inline-block}
.tag.ok{color:var(--ok);border-color:#3fb95055;background:#0f2417}
.tag.warn{color:var(--warn);border-color:#d2992255;background:#241d0f}
.tag.bad{color:var(--bad);border-color:#f8514955;background:#2a1214}
.tag.acc{color:var(--acc);border-color:#1f6feb66;background:#0d1b2c}
.occ{display:flex;gap:8px;flex-wrap:wrap;font-size:11px}
.bar{height:8px;border-radius:6px;background:#0b0f14;border:1px solid var(--line);overflow:hidden;margin:3px 0}
.bar .fill{height:100%;width:0;background:linear-gradient(90deg,#1f6feb,#58a6ff);transition:width .4s}
.bar .fill.coin{background:linear-gradient(90deg,#b8860b,#f0b429)}
.bar .fill.race{background:linear-gradient(90deg,#2ea043,#7ee787)}
.bar.unknown{opacity:.45}
.nums{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px}
.screenstage{position:relative;flex:1;min-height:0;display:flex;align-items:center;justify-content:center;background:#05070a;border-radius:8px;overflow:hidden}
.screenstage img.screen{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;cursor:crosshair;display:block}
.screenstage.full{position:fixed;inset:0;z-index:150;background:#000;border-radius:0}
.screenstage.full img.screen{max-height:100vh;max-width:100vw}
.screenstage.off img.screen{opacity:.18;filter:grayscale(1)}    /* 投屏关闭：旧画面压暗，别误当实时 */
.screen-msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:13px;background:#05070acc;pointer-events:none}
.screen-msg.hide{display:none}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:3px 5px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:11px;position:sticky;top:0;background:var(--card);z-index:1}
tr:last-child td{border-bottom:none}
.nowrap{white-space:nowrap}
.reason{color:var(--dim);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item{display:flex;gap:8px;justify-content:space-between;align-items:flex-start;padding:5px 6px;border:1px solid var(--line);border-radius:8px;background:#0d1219;margin-bottom:5px}
.q{font-size:12px;margin-bottom:2px}
.meta{color:var(--dim);font-size:11px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.item-side{display:flex;gap:4px;align-items:center;flex-shrink:0}
.thumb{width:34px;height:60px;object-fit:cover;object-position:top center;border-radius:4px;border:1px solid var(--line);display:block;background:#0b0f14}
select,input[type=text],input[type=number]{font:inherit;font-size:12px;padding:4px 7px;border-radius:7px;border:1px solid var(--line);background:#0b0f14;color:var(--fg)}
.radio{display:inline-flex;gap:4px;align-items:center;font-size:12px;margin-right:8px}
#log{background:#080b0f;border:1px solid var(--line);border-radius:8px;padding:8px;font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:1.45;height:100%;overflow:auto;white-space:pre-wrap;word-break:break-all;color:#c9d1d9;margin:0}
.modal{position:fixed;inset:0;background:#000000aa;display:none;z-index:200;align-items:center;justify-content:center}
.modal.open{display:flex}
.modal-inner{width:min(1180px,94vw);height:86vh;background:var(--card);border:1px solid var(--line);border-radius:12px;display:flex;flex-direction:column;overflow:hidden}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px}
.modal-body{flex:1;min-height:0;overflow:auto;padding:10px 14px}
.pane{display:none}
.pane.on{display:block}
.pane h4{margin:10px 0 6px;font-size:13px;color:var(--dim)}
.dev{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px}
.devbox{background:#0d1219;border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.devbox .top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.devbox .nm{font-size:13px;font-weight:600}
.devbox .sn{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--dim)}
.liverow{padding:4px 6px;border-radius:7px;margin-bottom:4px;background:#0d1219;border:1px solid var(--line)}
.liverow.sel{border-color:#1f6feb66;background:#0d1b2c}
/* 正在执行：每台一块（设备名 / 正在做哪一步 / 上一轮第几轮+计数）。原来 15px 粗体只够塞一行 */
.box.now .live{font-size:12px;font-weight:400}
.box.now .liverow{padding:3px 6px;margin-bottom:3px;display:flex;gap:6px;align-items:center;flex-wrap:nowrap;overflow:hidden}
.box.now .liverow:last-child{margin-bottom:0}
.box.now .liverow>.tag,.box.now .liverow>.mono{flex:none}
.box.now .lv-det{flex:1;min-width:0;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.devstat .devrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:5px 0;border-bottom:1px dashed var(--line)}
.devstat .devrow:last-child{border-bottom:0}
.keep-log{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--dim);white-space:pre-wrap;max-height:90px;overflow:auto;margin-top:5px}
.note{border-left:3px solid var(--warn);padding-left:8px;color:var(--dim);font-size:12px;margin:6px 0}
.appcheck .devblk{padding:6px 0;border-bottom:1px dashed var(--line)}
.appcheck .devblk:last-child{border-bottom:0}
.appcheck .hd{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:3px}
.appcheck .tblwrap{max-height:240px;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:4px}
.appcheck table{margin:0}
.appcheck .ali{margin-top:6px;border-left:3px solid var(--acc);padding-left:6px}
@media (max-height:820px){.box{padding:6px 8px}.box>h3{margin-bottom:4px;font-size:11px}.btn{font-size:12px;padding:4px 8px}.btn.pause,.btn.resume{font-size:13px;padding:5px 10px}th,td{padding:2px 4px}#c-elapsed{font-size:15px!important}.live{font-size:14px}}
</style>
</head>
<body>
<div class="app">
  <div class="col left">
    <div class="box screen">
      <h3>手机屏幕 · 实时投屏 <span id="screen-meta" class="dim tiny"></span>
        <button id="live-toggle" class="btn xs" title="关掉就不再抓帧：手机不发热，也不占 auto.py 截图要用的 adb 通道">投屏：开</button>
      </h3>
      <div class="screenstage" id="screenbox">
        <img id="screen" class="screen" alt="手机实时画面" src="">
        <div id="screen-msg" class="screen-msg">画面加载中…</div>
      </div>
    </div>
    <div class="box ctrl">
      <div class="srow">
        <select id="s-serial"></select>
        <label class="radio"><input type="radio" name="omode" value="view" id="om-view" checked> 看图</label>
        <label class="radio"><input type="radio" name="omode" value="op" id="om-op"> 操作</label>
        <span id="quicks" class="srow">
          <button class="btn xs" data-op="back">← 返回</button>
          <button class="btn xs" data-op="home">🏠 主页</button>
          <button class="btn xs" data-op="recents">▤ 最近任务</button>
          <button class="btn xs" data-op="swipe_down">↓ 下滑</button>
          <button class="btn xs" data-op="swipe_up">↑ 上滑</button>
        </span>
        <button id="btn-full" class="btn xs">⛶ 全屏</button>
        <span class="spacer"></span>
        <button id="btn-open-settings" class="btn xs ghost">⚙ 设置/保活</button>
        <button id="btn-open-log" class="btn xs ghost">📜 日志</button>
      </div>
      <div id="op-hint" class="dim tiny">看图模式下点画面不会操作手机。切到「操作」后点画面 = 在手机上点同一位置（点前会确认）。</div>
    </div>
  </div>

  <div class="splitter" id="splitter" title="拖动这条调整左右宽度（会记住）"></div>

  <div class="col right">
    <div class="box status" data-k="status">
      <div class="srow">
        <span id="pill" class="pill off">连接中…</span>
        <span id="ctl-status" class="ctl-status idle">状态加载中…</span>
        <span id="mode" class="tag acc">自动选择</span>
        <button id="btn-auto" class="btn xs ghost">恢复自动</button>
        <span class="spacer"></span>
        <span class="dim tiny">本轮</span><span id="c-elapsed" class="mono" style="font-size:17px">—</span>
        <span id="c-ver" class="tag tiny" title="版本号：报问题时报这一串就行">v?</span>
        <span id="clock" class="dim tiny">-</span>
      </div>
      <div class="srow" style="margin-top:6px">
        <button id="btn-pause" class="btn pause" title="只暂停下面「设备」里选中的那台（别的设备照跑）">⏸ 暂停本台</button>
        <button id="btn-resume" class="btn resume" disabled title="只继续选中的那台">▶ 继续本台</button>
        <button id="btn-kill" class="btn kill" title="只杀选中那台的 auto.py（另一台继续跑）">⏹ 停止本台</button>
        <button id="btn-stop-all" class="btn kill" title="两台手机上的 auto.py 全部杀掉">■ 全部停止</button>
        <button id="btn-start" class="btn primary xs">▶ 开始跑一轮</button>
        <button id="btn-appcheck" class="btn ghost" title="只读 pm list packages：一次查两台手机装了哪些外部 App（不点屏幕、不跑任务、不改手机上的东西）">App 自检</button>
        <button id="btn-shutdown" class="btn ghost" style="margin-left:auto" title="用完点这里：停掉正在跑的一轮 → 关掉面板进程 → 这个页面就不能再刷新了（可以关掉标签页）">⏻ 关闭面板</button>
        <button id="btn-teach" class="btn ghost">教学页</button>
        <button id="btn-records" class="btn ghost" title="记录管理：看每个证据目录占多大、有多少轮，删掉不要的旧记录（正在跑的那台不会被动）">记录管理</button>
        <button id="btn-reset-layout" class="btn ghost" title="模块顺序 / 大小 / 左右分栏 恢复默认（拖动标题排序、拖右下角改大小、拖中间那条分隔条改左右宽度，全部自动记住）">重置布局</button>
        <span class="spacer"></span>
        <span id="occupy" class="occ"></span>
      </div>
      <div class="srow" style="margin-top:6px">
        <label class="radio" title="这一轮在哪台手机上跑（选项由后端设备表 state/devices.json 动态生成，地址变了不用改代码）">设备
          <select id="pw-serial"></select></label>
        <input id="pw-devaddr" placeholder="可选 IP:端口" style="width:150px" title="自动发现不了时，把手机上「无线调试」显示的那一串粘进来，再点右边的【刷新设备地址】">
        <button id="btn-find-dev" class="btn xs ghost" title="零输入找手机：自动推网段 → 找活跃主机 → 扫随机端口 → 读机型认人 → 写回设备表并刷新（IP/端口/网段变了都能找到，约 15~40 秒）">自动找手机</button>
        <button id="btn-refresh-dev" class="btn xs ghost" title="手机重启后「无线调试」的端口会变。点这里自动重新发现两台手机并写回 state/devices.json（不用你手工读端口、也不用重启面板）">刷新设备地址</button>
        <span class="dim tiny">面板：</span>
        <label class="radio"><input type="checkbox" id="pw-coin" checked> 淘金币<span id="pw-coin-tag" class="tag tiny"></span></label>
        <!-- 2026-11-09 用户定案：赛车能量已下线（点原来的入口进的是「淘宝闪购」，不是任务面板）。
             auto.py 里 RACE_ENABLED=False，默认跳过；代码留档，活动复出时打开那个开关就能跑。 -->
        <label class="radio" title="活动已下线：默认不跑（勾了也会被 auto.py 的 RACE_ENABLED=False 跳过；要真跑就改 auto.py 里那个开关）"><input type="checkbox" id="pw-race"> 赛车能量（已下线）<span id="pw-race-tag" class="tag tiny"></span></label>
        <label class="radio" title="今天已完成的面板也要再跑一遍（= 忽略 coin11/state/panel_done.json）"><input type="checkbox" id="pw-force"> 强制重跑</label>
        <label class="radio" title="面板跑完接着按顺序跑自定义脚本（今天已经成功过的脚本会跳过）"><input type="checkbox" id="pw-scripts"> 跑完接着跑自定义脚本</label>
        <label class="radio" title="这一轮最多跑多久。不限时间 = 不设时间上限（auto.py 收到 --minutes 0），跑完为止">时长
          <select id="pw-minutes">
            <option value="30">30 分钟</option>
            <option value="60">1 小时</option>
            <option value="120">2 小时</option>
            <option value="0" selected>不限时间（默认）</option>
          </select></label>
        <label class="radio" title="轮数保险丝：这一轮最多做几轮（auto.py --max-rounds）。空 / 0 / 非法值一律按 120 处理">轮数
          <input type="number" id="pw-maxrounds" value="120" min="1" max="9999" step="1" style="width:66px"></label>
        <span class="dim tiny">轮数保险丝（仅在「不限时」时是唯一保险）</span>
        <button id="btn-open-scripts" class="btn xs ghost">🎬 脚本/录制</button>
      </div>
      <div id="hint" class="hint"></div>
      <div id="mode-hint" class="dim tiny"></div>
    </div>

    <div class="box progress" data-k="progress">
      <h3>进度 <span id="c-session" class="dim tiny"></span></h3>
      <div class="body">
        <div class="nums">
          <span id="c-ok" class="tag ok">成功 0</span>
          <span id="c-skip" class="tag warn">跳过 0</span>
          <span id="c-fail" class="tag bad">失败 0</span>
          <span id="c-today" class="dim tiny"></span>
        </div>
        <div class="srow" style="margin-top:8px">
          <span class="dim tiny" style="min-width:64px">淘金币</span>
          <span id="c-coin" class="small">还没跑过</span>
        </div>
        <div class="srow" style="margin-top:4px">
          <span class="dim tiny" style="min-width:64px">最近一次</span>
          <span id="c-runinfo" class="small dim"></span>
        </div>
      </div>
    </div>

    <div class="box devstat" data-k="devstat">
      <h3>设备状态 <span id="devstat-meta" class="dim tiny"></span></h3>
      <div class="body"><div id="devstat"><span class="dim">加载中…</span></div></div>
    </div>

    <div class="box appcheck coll" id="box-appcheck" data-k="appcheck">
      <h3 title="点这里展开/收起：后台跑 python auto.py --appcheck（只读 pm 包列表，两台一起查）"><span class="hd-txt">App 自检</span>
        <span id="appcheck-meta" class="hd-meta dim tiny">加载中…</span>
        <button id="appcheck-toggle" class="btn xs ghost">展开</button><span class="chev">></span>
      </h3>
      <div class="body">
        <div class="dim tiny">只读：后台跑 python auto.py --appcheck（只发 pm list packages，两台一起查，不点屏幕、不跑任务）。支付宝单独一栏：走授权通道，不进通用白名单。</div>
        <div id="appcheck-body" style="display:none"></div>
      </div>
    </div>

    <div class="box now" data-k="now">
      <h3 title="每台设备一行：数据来自 GET /api/state 的 console.lives（auto.py 写的 coin11/state/live-&lt;san&gt;.json，读不到退回 state/live.json），跟着 3 秒轮询刷新">正在执行 <span id="live-meta" class="dim tiny"></span></h3>
      <div class="body">
        <div id="live" class="live">正在：<b>—</b></div>
      </div>
    </div>

    <div class="box rounds" id="box-rounds" data-k="rounds">
      <h3>轮次（全部，可滚动） <span id="rounds-meta" class="dim tiny"></span></h3>
      <div class="body">
        <table>
          <thead><tr><th>时间</th><th>设备</th><th>面板</th><th>任务</th><th>计数</th><th>判定</th><th>耗时</th><th>理由</th></tr></thead>
          <tbody id="rounds"><tr><td colspan="8" class="dim">加载中…</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="box coll" id="box-pending" data-k="pending">
      <h3 title="点这里展开/收起：待教学的问题（最多 3 条，展开后在框内自己滚）"><span class="hd-txt">待教的地方</span>
        <span id="pending-meta" class="hd-meta dim tiny"></span><span class="chev">></span></h3>
      <div class="body">
        <div class="dim tiny" style="margin-bottom:4px">待教学的问题（最多 3 条）</div>
        <div id="pending"><div class="dim">加载中…</div></div>
      </div>
    </div>

    <div class="box coll" id="box-navfail" data-k="navfail">
      <h3 title="点这里展开/收起：导航失败（最多 3 条，展开后在框内自己滚）"><span class="hd-txt">导航失败</span>
        <span id="navfail-meta" class="hd-meta dim tiny"></span><span class="chev">></span></h3>
      <div class="body">
        <div class="dim tiny" style="margin-bottom:4px">导航失败（最多 3 条）</div>
        <div id="navfail"><div class="dim">加载中…</div></div>
      </div>
    </div>

    <div class="box coll" id="box-manual" data-k="manual">
      <h3 title="点这里展开/收起：我的操作记录（最近 5 条，展开后在框内自己滚）"><span class="hd-txt">我的操作记录</span>
        <span id="manual-meta" class="hd-meta dim tiny"></span><span class="chev">></span></h3>
      <div class="body"><div id="manual-list">加载中…</div></div>
    </div>
  </div>
</div>

<div id="modal" class="modal">
  <div class="modal-inner">
    <div class="modal-head">
      <b id="modal-title">设置</b>
      <span class="srow">
        <button class="btn xs ghost" data-pane="settings">⚙ 设置/保活</button>
        <button class="btn xs ghost" data-pane="scripts">🎬 脚本/录制</button>
        <button class="btn xs ghost" data-pane="log">📜 日志</button>
        <button class="btn xs ghost" data-pane="records">🗂 记录管理</button>
        <button id="modal-close" class="btn xs">关闭 ✕</button>
      </span>
    </div>
    <div class="modal-body">
      <div id="pane-records" class="pane">
        <h4>证据记录 —— 每一轮都在下面这些目录里（截图 + OCR + 判定）
          <span id="rec-meta" class="dim tiny"></span></h4>
        <div class="devbox">
          <div class="srow">
            <button class="btn xs ghost" id="rec-refresh">🔄 刷新</button>
            <button class="btn xs kill" id="rec-del-sel">🗑 删除选中</button>
            <span class="spacer"></span>
            <span class="dim tiny">只保留最近</span>
            <input type="number" id="rec-days" value="3" min="0" step="1" style="width:70px">
            <span class="dim tiny">天，删更旧的</span>
            <button class="btn xs kill" id="rec-prune">🧹 清理旧记录</button>
          </div>
          <div class="note" id="rec-hint">正在跑的那台设备，它的目录不会被动。删除不可恢复 —— 要留档先自己拷走。</div>
          <table>
            <thead><tr><th style="width:34px"><input type="checkbox" id="rec-all" title="全选（正在跑的目录不可选）"></th>
              <th>证据目录</th><th style="width:70px">轮次</th><th style="width:90px">占用</th>
              <th style="width:130px">最后写入</th><th style="width:80px">操作</th></tr></thead>
            <tbody id="rec-list"><tr><td colspan="6" class="dim">加载中…</td></tr></tbody>
          </table>
        </div>
      </div>
      <div id="pane-settings" class="pane on">
        <h4>保活（默认关；Asleep 只记录「待人工解锁」，永不发 keyevent 26 / 电源键）</h4>
        <div id="devices" class="dev"><div class="dim">加载中…</div></div>
        <h4>跳转确认策略 <span id="jp-meta" class="dim tiny"></span></h4>
        <div class="devbox">
          <label class="radio"><input type="radio" name="jp" value="once" id="jp-once"> 仅打开一次（默认）</label>
          <label class="radio"><input type="radio" name="jp" value="always" id="jp-always"> 始终打开</label>
          <div class="note">选「始终打开」后，系统不再弹跳转确认框，直接进入目标 App（用户已知悉）。
            写入 <span class="mono" id="jp-path">coin11/targets/jump_policy.json</span>，auto.py 照它点按钮。</div>
        </div>
        <h4>手动记录：我做了什么</h4>
        <div class="devbox">
          <div class="srow">
            <select id="m-serial"></select>
            <input type="text" id="m-action" placeholder="我做了什么（一句话）" style="flex:1;min-width:220px">
          </div>
          <div class="srow" style="margin-top:6px">
            <input type="text" id="m-note" placeholder="备注（可选）" style="flex:1;min-width:160px">
            <button class="btn xs ghost" id="m-snap">📷 截取当前屏幕</button>
            <button class="btn xs primary" id="m-save">💾 保存记录</button>
            <label class="radio"><input type="checkbox" id="m-auto"> 自动记录（开始跑一轮）</label>
          </div>
          <div class="srow" id="m-shots" style="margin-top:6px"></div>
          <div class="srow" style="margin-top:6px">
            <span class="dim tiny">试点击：</span>
            <select id="t-serial"></select>
            x <input type="number" id="t-x" value="1258" style="width:80px">
            y <input type="number" id="t-y" value="1813" style="width:80px">
            <button class="btn xs" id="t-go">试点击</button>
          </div>
        </div>
        <h4>我教过的规则 <span id="rules-meta" class="dim tiny"></span></h4>
        <div class="devbox">
          <div class="srow">
            <button class="btn xs ghost" id="rules-dedupe">🧹 合并相似规则</button>
            <button class="btn xs ghost" id="rules-backup">💾 全部备份并重载</button>
            <span id="rules-hint" class="dim tiny"></span>
          </div>
          <table>
            <thead><tr><th>序号</th><th>规则名</th><th>坐标</th><th>设备</th><th>时间</th><th>操作</th></tr></thead>
            <tbody id="rules"><tr><td colspan="6" class="dim">加载中…</td></tr></tbody>
          </table>
          <div class="note">规则名与坐标可就地改，改完点【保存】。删除会 confirm 二次确认。
            每次写文件前都先备份成 <span class="mono">targets.jsonl.bak-YYYYmmdd-HHMMSS</span>。
            【合并相似规则】= 同一设备且坐标距离 &lt; 40px 视为同一条，保留最新 ts 的那条（规则名/备注取更长的）。</div>
        </div>

      </div>

      <div id="pane-scripts" class="pane" style="height:72vh">
        <h4>录制一个自定义脚本 <span id="rec-meta" class="dim tiny"></span></h4>
        <div class="devbox">
          <div class="srow">
            <button class="btn xs primary" id="rec-start">● 开始录制</button>
            <button class="btn xs" id="rec-undo">↶ 撤销上一步</button>
            <span class="dim tiny">插入等待</span>
            <input type="number" id="rec-secs" value="2" min="0.2" max="60" step="0.5" style="width:70px">
            <span class="dim tiny">秒</span>
            <button class="btn xs" id="rec-wait">＋ 等待</button>
            <span class="spacer"></span>
            <button class="btn xs" id="rec-clear">🗑 清空草稿</button>
            <button class="btn xs primary" id="rec-save">✔ 结束并保存</button>
          </div>
          <div class="srow" style="margin-top:6px">
            <span class="dim tiny">名字</span>
            <input type="text" id="rec-name" placeholder="例如：京东每日签到" style="flex:1;min-width:180px">
            <span class="dim tiny">id</span>
            <input type="text" id="rec-id" placeholder="可空，自动生成（只许 a-z0-9_）" style="width:200px">
          </div>
          <div class="note">录制只记坐标步骤（不认 App 内容）：点【● 开始录制】→ 到左边切「操作」模式在手机上点一遍 →
            回到这里【✔ 结束并保存】。每次操作真的在手机上做成了才记一步。
            草稿落盘 <span class="mono">coin11/state/record_draft.json</span>，dash 重启也不丢。</div>
          <div id="rec-steps" class="keep-log" style="max-height:170px">还没有步骤</div>
        </div>

        <h4>脚本列表 <span id="scripts-meta" class="dim tiny"></span></h4>
        <table>
          <thead><tr><th>id</th><th>名字</th><th>步数</th><th>要做</th><th>顺序</th><th>上次结果</th><th>操作</th></tr></thead>
          <tbody id="scripts"><tr><td colspan="7" class="dim">加载中…</td></tr></tbody>
        </table>
        <div class="note">排队只跑打了「要做」且今天还没成功过的（按顺序；每步按脚本里的等待走）。【▶ 一键运行】只跑这一个、不受今天限制。
          运行状态是 auto.py 写的 <span class="mono">coin11/state/scripts_state.json</span>，面板只读。</div>

        <h4>面板完成记忆 <span class="dim tiny">（coin11/state/panel_done.json：auto.py 写，面板只读）</span></h4>
        <div id="pdone" class="srow"><span class="dim">加载中…</span></div>
      </div>

      <div id="pane-log" class="pane" style="height:72vh">
        <h4>运行日志 <span id="log-src" class="dim tiny"></span></h4>
        <div class="srow" id="log-tabs" style="margin-bottom:6px">
          <span class="dim tiny">看哪台的日志：</span>
          <span id="log-tab-btns"></span>
          <span class="dim tiny">（默认跟着上面「设备」下拉；两台并行时这里可以分开看）</span>
        </div>
        <pre id="log">（还没有日志）</pre>
      </div>
    </div>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let TEACH = "http://127.0.0.1:8777";
let SHOTS = [];
let LAST = null;

function pctOf(v){ return v == null ? null : Math.max(0, Math.min(100, v)); }
function setBar(wrapId, fillId, pct){
  const wrap = $(wrapId), fill = $(fillId);
  if (!wrap || !fill) return;
  if (pct == null){ wrap.classList.add("unknown"); fill.style.width = "0%"; }
  else { wrap.classList.remove("unknown"); fill.style.width = pctOf(pct) + "%"; }
}
function human(sec){
  if (sec == null) return "—";
  sec = Math.round(sec);
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  if (h) return h + " 小时 " + m + " 分";
  if (m) return m + " 分 " + s + " 秒";
  return s + " 秒";
}
function hint(t){ $("#hint").textContent = t || ""; }

function runOf(s, serial){
  return (s.runs || []).find(r => r.serial === serial) || {};
}

function renderControl(s){
  const c = s.console.control;
  const el = $("#ctl-status");
  el.className = "ctl-status " + (c.cls || "idle");
  el.textContent = c.status;
  // 2026-11-02：暂停/继续/停止都只作用于「设备」下拉里选中的那台（按钮状态按设备分别显示）
  const sel = deviceSel();
  const r = runOf(s, sel);
  const selPaused = !!r.paused;
  $("#btn-pause").disabled = selPaused;
  $("#btn-resume").disabled = !selPaused;
  $("#btn-kill").disabled = !(r.running || selPaused);
  // 2026-11-03 修：以前【开始跑一轮】被点过一次就永久 disabled（onclick 里置 true，render 从不还原），
  // 而且只要**任何一台**在跑也会连带禁用。现在按「设备下拉里选中的那台」判断：
  // 这台没在跑就能点 —— 这样手机2 在跑的时候照样可以给手机1 起一轮。
  $("#btn-start").disabled = !!r.running;
  $("#m-auto").checked = !!s.console.auto_record;
  const OCC = {busy: "bad", keep: "warn", teach: "acc", idle: "ok", paused: "warn"};
  $("#occupy").innerHTML = s.console.devices.map(d =>
    '<span class="tag ' + (OCC[d.occupy_cls] || "ok") + '">' + esc(d.label) + "：" + esc(d.occupy)
    + (d.last_action_at ? " · " + esc(d.last_action_at) : "") + '</span>').join("");
}

function renderRuns(s){
  // 2026-11-02 用户要求：每台一行 = 名称 + 序列号 + 状态（运行中 pid/空闲/已暂停）+ 三个按钮
  const runs = s.runs || [];
  const sel = deviceSel();
  const box = $("#devstat");
  box.innerHTML = runs.map(r => {
    const cls = r.running ? "running" : (r.paused ? "paused" : "idle");
    const isSel = r.serial === sel;
    let extra;
    if (r.running){
      extra = (r.what || "") + " · 时长 " + (r.minutes_label || "?") + " · 已跑 " + human(r.elapsed_s)
            + (r.started_at ? " · 起于 " + r.started_at : "");
    } else if (r.paused){
      extra = "暂停标志在：auto.py 会停在下一个安全点，先点【继续】";
    } else {
      extra = "没有面板启的进程";
    }
    const out = r.out ? String(r.out).replace(/^.*evidence[\\/]/, "evidence/") : "";
    return '<div class="devrow">'
      + '<span class="tag ' + (isSel ? "acc" : "") + '" title="这一轮在哪台跑，由上面的「设备」下拉决定">'
      + esc(r.label || r.serial) + (isSel ? " · 选中" : "") + '</span>'
      + '<span class="mono dim small">' + esc(r.serial) + '</span>'
      + '<span class="ctl-status ' + cls + '">' + esc(r.state_txt || "?") + '</span>'
      + '<button class="btn small" data-run="pause" data-serial="' + esc(r.serial) + '"'
      + (r.paused ? " disabled" : "") + '>⏸ 暂停</button>'
      + '<button class="btn small" data-run="resume" data-serial="' + esc(r.serial) + '"'
      + (r.paused ? "" : " disabled") + '>▶ 继续</button>'
      + '<button class="btn small" data-run="stop" data-serial="' + esc(r.serial) + '"'
      + ((r.running || r.paused) ? "" : " disabled") + '>⏹ 停止</button>'
      + '<span class="dim small">' + esc(extra) + '</span>'
      + (out ? '<span class="dim small mono">→ ' + esc(out) + '</span>' : "")
      + '</div>';
  }).join("") || '<span class="dim">还没有设备</span>';
  const n = runs.filter(r => r.running).length;
  $("#devstat-meta").textContent = "（" + runs.length + " 台 · 运行中 " + n
    + " 台 · 每台各自一个进程、各自的证据目录，互不打断）";
}

// phase 的中文说法（不认识的 phase 原样显示，别乱翻译）
const PHASE_LBL = {"start": "开局", "swipe": "滑动", "tap": "点击", "wait": "等待",
  "read": "读数", "scan": "扫描", "back": "返回", "home": "回桌面", "done": "收尾",
  "pause": "暂停", "shot": "截图", "open": "打开", "type": "输入", "check": "核对"};
function phaseLbl(p){
  const k = String(p || "").toLowerCase();
  return PHASE_LBL[k] || (p || "—");
}
// 新鲜度：<=5 秒 = 绿「实时」；stale 或 >30 秒 = 灰「已过期 Ns」；其余「Ns 前」
function liveFresh(lv){
  if (!lv) return '<span class="tag">没有状态</span>';
  const a = lv.age_s == null ? null : Math.round(lv.age_s);
  if (a == null) return '<span class="tag">时间未知</span>';
  if (!lv.stale && a <= 5) return '<span class="tag ok">实时</span>';
  if (lv.stale || a > 30) return '<span class="tag">已过期 ' + a + 's</span>';
  return '<span class="tag">' + a + 's 前</span>';
}
function renderLive(s){
  // 2026-11-04 用户要求：这块原来只显示一行「正在：—」（还只显示选中那台），标题里那段
  // live-<san>.json 的说明被当成正文，看起来就是一串路径 -> 现在标题只留一句摘要，
  // 正文每台设备一行：设备名 | phase 中文 | detail（截断，title 给全文）| 新鲜度。
  // 数据只用已有的 console.lives / runs（3 秒 /api/state 轮询），后端一个字没改。
  const box = $("#live");
  const meta = $("#live-meta");
  const sel = deviceSel();
  const lives = s.console.lives || {};
  const runs = s.runs || [];
  const devs = s.console.devices || [];
  const rows = devs.map(d => {
    const ser = d.serial;
    const nm = d.label || deviceLabelOf(ser) || ser;
    const run = runs.find(x => x.serial === ser) || {};
    const lv = lives[ser] || null;
    const isSel = ser === sel;
    const detail = lv ? String(lv.detail || "") : "";
    const ph = lv ? phaseLbl(lv.phase) : (run.running ? "—" : "空闲");
    const full = detail || (lv ? "（这次还没有详细动作）" : "（这台还没有 state/live 文件：没跑过，或 auto.py 还没写）");
    return '<div class="liverow' + (isSel ? " sel" : "") + '">'
      + '<span class="tag ' + (run.running ? "ok" : (run.paused ? "warn" : "")) + '">' + esc(nm) + (isSel ? " · 选中" : "") + '</span>'
      + '<span class="mono dim small">' + esc(ph) + '</span>'
      + '<span class="lv-det" title="' + esc(full) + '">' + esc(detail || "—") + '</span>'
      + liveFresh(lv) + '</div>';
  });
  const running = devs.filter(d => (runs.find(x => x.serial === d.serial) || {}).running);
  if (meta){
    const fresh = devs.filter(d => {
      const lv = lives[d.serial];
      return lv && !lv.stale && lv.age_s != null && lv.age_s <= 30;
    });
    meta.textContent = "（" + (running.length ? running.map(d => d.label || d.serial).join("、") + " 运行中" : "没有在跑的")
      + (fresh.length ? " · " + fresh.map(d => d.label || d.serial).join("、") + " 实时" : "") + "）";
  }
  box.className = "live" + (running.length ? "" : " stale");
  box.innerHTML = rows.join("") || '<span class="dim">还没有设备</span>';
}

function renderDevices(s){
  const ivs = s.console.keep_intervals;
  $("#devices").innerHTML = s.console.devices.map(d => {
    const k = d.keep;
    const cls = ({busy:"bad", keep:"warn", teach:"acc", idle:"ok"})[d.occupy_cls] || "ok";
    const opts = ivs.map(v => '<option value="' + v + '"' + (Number(k.interval) === Number(v) ? " selected" : "") + ">" + v + " 分钟</option>").join("");
    return '<div class="devbox">'
      + '<div class="top"><span class="nm">' + esc(d.label) + '</span>'
      + '<span class="tag ' + cls + '">' + esc(d.occupy) + '</span></div>'
      + '<div class="sn">' + esc(d.serial) + '</div>'
      + '<div class="row"><span class="dim small">保活</span>'
      + '<button class="btn small' + (k.on ? " on" : "") + '" data-act="keep" data-serial="' + esc(d.serial) + '" data-on="' + (k.on ? 0 : 1) + '">[保活 ' + (k.on ? "开" : "关") + ']</button>'
      + '<select data-act="iv" data-serial="' + esc(d.serial) + '">' + opts + '</select>'
      + '<span class="dim small">最近屏幕动作：' + esc(d.last_action_at || "无") + '</span></div>'
      + '<div class="dim small">已滑 ' + k.hits + ' 次 · 跳过 ' + k.skips + ' 次 · 上次：' + esc(k.last_result || "—")
      + (k.next_at ? (' · 下次 ' + esc(k.next_at)) : "") + '</div>'
      + (k.log && k.log.length ? '<div class="keep-log">' + esc(k.log.join("\n")) + '</div>' : "")
      + '</div>';
  }).join("");
  const sel = s.console.devices.map(d => '<option value="' + esc(d.serial) + '">' + esc(d.label + " " + d.serial) + '</option>').join("");
  if ($("#m-serial").dataset.opts !== sel){ $("#m-serial").innerHTML = sel; $("#m-serial").dataset.opts = sel; }
  if ($("#t-serial").dataset.opts !== sel){ $("#t-serial").innerHTML = sel; $("#t-serial").dataset.opts = sel; }
}

function renderJump(s){
  const jp = s.console.jump_policy;
  $("#jp-once").checked = jp.policy !== "always";
  $("#jp-always").checked = jp.policy === "always";
  $("#jp-meta").textContent = "（当前：" + jp.label + (jp.updated_at ? " · " + jp.updated_at : "") + "）";
  $("#jp-path").textContent = jp.path;
}

let OMODE = "view";
let SCREEN_SERIAL = "";
let SCR = {w: 1440, h: 3200};
const OP_LBL = {tap: "点击", back: "返回", home: "主页", recents: "最近任务",
                swipe_up: "上滑一下", swipe_down: "下滑一下"};

let SCREEN_LIVE = true;      // 实时投屏开关（后端 state/screen_off.flag 为准）
function screenTick(){
  if (!SCREEN_LIVE) return;   // 关掉时连 /live.jpg 都不请求（否则等于没关）
  const sel = $("#s-serial");
  const serial = (sel && sel.value) || SCREEN_SERIAL;
  if (!serial) return;
  SCREEN_SERIAL = serial;
  const img = $("#screen");
  img.src = "/live.jpg?serial=" + encodeURIComponent(serial) + "&t=" + Date.now();
}
setInterval(screenTick, 500);
$("#live-toggle").addEventListener("click", () => setScreenLive(!SCREEN_LIVE));

async function setScreenLive(on){
  const r = await fetch("/api/screen_live", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({on: on})
  });
  let out = {};
  try { out = await r.json(); } catch (e) { out = {}; }
  if (!r.ok || out.ok === false) { hint("切换投屏失败：" + (out.error || r.status)); return; }
  SCREEN_LIVE = !!out.on;
  paintScreenLive();
  hint(SCREEN_LIVE ? "实时投屏已打开（约 2 帧/秒）" : "实时投屏已关闭：不再抓帧，手机不会因为它发烫");
}

function paintScreenLive(){
  const b = $("#live-toggle");
  if (!b) return;
  b.textContent = SCREEN_LIVE ? "投屏：开" : "投屏：关";
  b.classList.toggle("acc", !SCREEN_LIVE);
  const box = $("#screenbox");
  if (box) box.classList.toggle("off", !SCREEN_LIVE);
}

async function panelOp(op, x, y, note){
  hint("正在执行「" + (OP_LBL[op] || op) + "」…（改手机前先存一张图，改完再存一张）");
  const r = await post("/api/panel_op", {serial: SCREEN_SERIAL, op: op, x: x, y: y, note: note || ""});
  if (r.ok){
    const rr = r.record || {};
    const rd = r.record_draft || {};
    hint((r.label || OP_LBL[op] || op) + " 完成" + (rr.x != null ? (" @(" + rr.x + "," + rr.y + ")") : "")
      + " · 已记录 · before=" + (rr.shot_before || "无") + " after=" + (rr.shot_after || "无")
      + (rd.active ? (" · 录制中已记 " + rd.count + " 步") : ""));
    poll();
  } else {
    hint("操作失败：" + (r.error || "未知"));
  }
  return r;
}

function renderScreen(s){
  const sc = s.console.screen || {frames: {}};
  if (!SCREEN_SERIAL || !s.console.devices.some(d => d.serial === SCREEN_SERIAL)) {
    SCREEN_SERIAL = (s.console.devices[0] || {}).serial || "";
  }
  const dev = s.console.devices.find(d => d.serial === SCREEN_SERIAL) || {};
  if (dev.w){ SCR = {w: dev.w, h: dev.h}; }
  const sel = $("#s-serial");
  const opts = s.console.devices.map(d => '<option value="' + esc(d.serial) + '"' + (d.serial === SCREEN_SERIAL ? " selected" : "") + '>' + esc(d.label + " " + d.serial) + '</option>').join("");
  if (sel.dataset.opts !== opts){ sel.innerHTML = opts; sel.dataset.opts = opts; }
  const liveOn = (sc.enabled !== false);      // 后端 state/screen_off.flag 是唯一真相
  if (liveOn !== SCREEN_LIVE) { SCREEN_LIVE = liveOn; paintScreenLive(); }
  const fr = (sc.frames || {})[SCREEN_SERIAL] || {};
  const cnt = "累计抓到 " + sc.grabs + " 帧 / 失败 " + sc.fails + " 次";
  $("#screen-meta").textContent = liveOn
    ? ("（" + (fr.age_s == null ? "还没抓到帧" : (fr.age_s + " 秒前的帧 · " + (fr.size || "?") + " · " + (fr.bytes || 0) + " 字节"))
       + " · " + cnt + (fr.err ? (" · 错误：" + fr.err) : "") + "）")
    : ("（投屏已关闭 · 不再抓帧，手机不会因为投屏发烫，也不占 auto.py 截图要用的 adb 通道；"
       + cnt + "。要恢复点右上角开关）");
  $("#op-hint").textContent = OMODE === "op"
    ? "操作模式：点画面 = 在手机上点同一位置（点前会确认），每个操作都存前后两张图。"
    : "看图模式：点画面不会操作手机。要操作请切到「操作模式」。";
}

// 2026-11-09 用户定案：任务清单是「最近一次扫描」的快照，跑一轮就覆盖，已从面板删除。
// 这个函数只剩「当前是谁在挑活」这一件事（顶部那枚标签 + 【恢复自动】按钮）。
function renderMode(s){
  const hint = s.console.hint;
  const mode = s.console.mode || {kind: "auto", label: "自动选择"};
  $("#mode").className = "tag " + (mode.kind === "hint" ? "warn" : "acc");
  $("#mode").textContent = mode.label;
  $("#btn-auto").disabled = !hint;
  $("#mode-hint").textContent = hint
    ? (hint.panel_label + " / " + (hint.name || hint.row_key) + " · 指定于 " + hint.ts + " · 做完自动清除")
    : "（auto.py 自己挑可做行；你可以点下面的【做这个】干预下一轮）";
}

function renderManual(s){
  const rows = s.console.manual || [];
  $("#manual-meta").textContent = "（最近 " + rows.length + " 条，共 " + s.console.manual_count + " 条）";
  $("#m-shots").innerHTML = SHOTS.length
    ? SHOTS.map((x, i) => '<span class="dim small">' + esc(x.path) + ' <a href="#" data-drop="' + i + '" class="dim small">移除</a></span>').join(" ")
    : '<span class="dim small">还没有挂截图</span>';
  const MS = rows.slice(0, 5);
  $("#manual-list").innerHTML = MS.length ? MS.map(r => {
    const tag = r.source === "auto" ? '<span class="tag warn">自动</span>'
      : (r.source === "panel" ? '<span class="tag bad">面板操作</span>' : '<span class="tag acc">手动</span>');
    const urls = r.shot_urls || [];
    const label = OP_LBL[r.action] || r.action;
    const xy = (r.x != null && r.y != null) ? ' @(' + r.x + ',' + r.y + ')' : '';
    return '<div class="item"><div class="item-main">'
      + '<div class="q">' + esc(label) + esc(xy) + '</div>'
      + '<div class="meta">' + tag + '<span class="mono">' + esc(r.ts) + '</span>'
      + '<span>' + esc(r.device || "") + ' ' + esc(r.serial || "") + '</span>'
      + (r.note ? '<span>备注：' + esc(r.note) + '</span>' : "") + '</div></div>'
      + '<div class="item-side">' + (urls.length ? urls.map(u => '<a href="' + esc(u) + '" target="_blank" rel="noopener"><img class="thumb sm" loading="lazy" src="' + esc(u) + '" alt="截图"></a>').join("") : '<span class="dim small">无截图</span>') + '</div></div>';
  }).join("") : '<div class="dim">还没有记录（手动保存或打开自动记录后才有）</div>';
}

function paintLogBox(blob, ld){
  // 日志框只有一个画法（轮询和 SSE 共用）：
  //   stick = 本来就在底部才跟着滚到底；往上翻着看历史时不抢滚动位置（这是老行为，别破）。
  const pre = $("#log");
  if (!pre) return;
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;
  const txt = (blob.lines || []).join("\n");
  if (pre.textContent !== txt){ pre.textContent = txt || "（这台还没有日志）"; }
  if (stick) pre.scrollTop = pre.scrollHeight;
  $("#log-src").textContent = "（" + (deviceLabelOf(ld) || ld) + " · 来源：" + (blob.source || "?") + "）";
}

function renderOld(s){
  const coin = s.panels.find(p => p.key === "coin") || {};
  const race = s.panels.find(p => p.key === "race") || {};
  // 2026-11-09 用户要求「进度对不上、看不懂、还有未知」：改成
  //   · 三个数只算**今天**（以前是全历史：3036 轮 / 失败 1018，跟今天干了啥没关系）
  //   · 淘金币进度用**最近一轮真的读到的**行进度和金币总数（不再依赖扫描 JSON，所以不会再是「未知」）
  //   · 赛车能量已下线，这一栏整个不再出现
  const td = s.today || {success: 0, skipped: 0, failed: 0, total: 0};
  $("#c-ok").textContent = "成功 " + td.success;
  $("#c-skip").textContent = "跳过 " + td.skipped;
  $("#c-fail").textContent = "失败 " + td.failed;
  var _sess = (s.session.dirs || []).join(" + ") || s.session.name || "未找到证据目录";
  var _el = $("#c-session");
  _el.textContent = "今天 · " + td.total + " 轮 · 待教 " + s.pending_open;
  _el.title = "证据目录：" + _sess + "（历史合计 " + s.counts.total + " 轮）";
  // 版本号单一来源 = 后端 PANEL_VERSION（改版本只改一处）
  if (s.version) {
    window.LAST_VERSION = s.version;
    document.title = "coin11 总控台 v" + s.version;
    var _v = $("#c-ver");
    if (_v) _v.textContent = "v" + s.version;
  }
  // 2026-11-09 同学版：隐藏轮次等重信息，只留连接状态 / 开始-停止 / App 自检
  if (s.student) {
    document.body.classList.add("student");
  }
  // 2026-11-09 用户定案：分发版也要看得到轮次（和自用版一样），旧轮次靠【记录管理】自己删。
  // 唯一藏掉的是教学页按钮 —— 那是单独的 teach.py 服务，打包版没带，留着就是死链。
  if (s.teach_page === false) {
    document.querySelectorAll("#btn-teach").forEach(function (el) { el.style.display = "none"; });
  }
  // 「本轮」只在真有进程在跑时才有意义：以前空闲时给的是「最早一轮到现在」的跨度（170 小时），纯噪音
  const _rn = (s.runs || []).filter(r => r.running);
  $("#c-elapsed").textContent = _rn.length ? human(s.session.elapsed_s) : "—";
  const runNow = (s.runs || []).filter(r => r.running);
  let info;
  if (runNow.length) {
    info = runNow.map(r => (r.label || r.serial) + " 正在跑（已 " + human(r.elapsed_s)
      + " / 时长 " + (r.minutes_label || "?") + "）").join("；");
  } else if (s.coin_live) {
    // 没在跑的进程：用「最近一轮」的时间 + 那台设备的上次退出码（跑过几千轮了，不能写「还没跑过」）
    const cl2 = s.coin_live;
    const rr = (s.runs || []).find(x => x.serial === cl2.device) || {};
    const code = (rr.code == null) ? null : rr.code;
    const why = code == null ? "" : (code === 0 ? "正常跑完" : (code === 3 ? "停下来等教学" : "异常退出"));
    info = (cl2.device_label || cl2.device || "设备") + " · 第 " + cl2.round + " 轮 · " + cl2.at
      + (code == null ? "" : " · 退出码 " + code + "（" + why + "）");
  } else if (s.run.code != null) {
    const code = s.run.code;
    info = "退出码 " + code;
  } else {
    info = "还没跑过";
  }
  $("#c-runinfo").textContent = info;
  // 淘金币：最近一轮读到的「行进度 before->after」和「金币 before->after」，一个都不猜
  const cl = s.coin_live;
  if (!cl) {
    $("#c-coin").textContent = "还没有跑过一轮（点【开始跑一轮】）";
  } else {
    const parts = [];
    if (cl.task) {
      const rowTxt = cl.after ? ((cl.before || "?") + " → " + cl.after)
                              : (cl.before ? (cl.before + "（没等到 after 读数）") : "");
      parts.push((rowTxt ? rowTxt + "　" : "") + cl.task);
    }
    if (cl.coin_before != null || cl.coin_after != null) {
      const d = (cl.coin_before != null && cl.coin_after != null) ? (cl.coin_after - cl.coin_before) : null;
      parts.push("金币 " + (cl.coin_before == null ? "?" : cl.coin_before) + " → "
        + (cl.coin_after == null ? "?" : cl.coin_after)
        + (d ? ("（" + (d > 0 ? "+" : "") + d + "）") : ""));
    }
    if (!cl.has_data || !parts.length) {
      $("#c-coin").textContent = "最近一轮没读到进度（状态 " + (cl.status || "?") + "）　第 "
        + cl.round + " 轮 · " + cl.at;
    } else {
      $("#c-coin").textContent = parts.join(" · ") + "　第 " + cl.round + " 轮 · " + cl.at
        + (cl.has_data && cl.at !== (s.coin_live_all_at || cl.at) ? "" : "");
    }
  }

  // 2026-11-02 用户要求：轮次区要像下面那些区一样能滚动看全部，不要只给 8 条。
  const RS = s.rounds;
  const rmeta = s.rounds_meta || {};
  let rmetaTxt = "（共 " + s.rounds.length + " 轮，全部列出，可滚动）";
  if (rmeta.dir) rmetaTxt += " · 来源 " + rmeta.dir;
  // 2026-11-02：多设备各自一个证据目录 -> 按目录分开统计，别只给一个总数
  const byDir = rmeta.by_dir || {};
  const dirKeys = Object.keys(byDir);
  if (dirKeys.length > 1) {
    rmetaTxt += " · " + dirKeys.map(k => k.replace(/^\d{4}-\d{2}-\d{2}-/, "") + " " + byDir[k] + " 轮").join(" / ");
  }
  if (rmeta.updated) {
    rmetaTxt += " · 最后更新 " + rmeta.updated;
    if (rmeta.updated_age_s != null) rmetaTxt += "（" + Math.round(rmeta.updated_age_s) + " 秒前）";
  } else if (rmeta.dir) {
    rmetaTxt += " · 还没有 06_verdict.json";
  }
  $("#rounds-meta").textContent = rmetaTxt;
  $("#rounds-meta").title = rmetaTxt;   // 2026-11-04：标题行只占一行（省略号），悬停看全
  const rbody = $("#rounds").closest(".body");
  const rsig = s.rounds.length + "|" + (s.rounds[0] ? (s.rounds[0].round + ":" + s.rounds[0].status
      + ":" + (s.rounds[0].dir || "")) : "") + "|" + (rmeta.updated || "") + "|" + dirKeys.join(",");
  if ($("#rounds").dataset.sig !== rsig) {
    // 只在内容真的变了时重建 DOM，并保住用户的滚动位置（否则每 3 秒轮询都会把列表顶回顶部）
    const keep = rbody ? rbody.scrollTop : 0;
    $("#rounds").innerHTML = RS.length ? RS.map(r => {
      const st = r.status || "?";
      const cls = (st === "advanced" || st === "disappeared" || st === "ok") ? "ok" : (st === "skipped" ? "warn" : "bad");
      // 没选中任务的轮次（no-doable 等）没有计数读数，别渲染成 "? → ?"
      const cnt = (r.before && r.after) ? (r.before + " → " + r.after) : (r.before || r.after || "—");
      // 2026-11-02：新增「设备」列；老证据没有 device 字段就显示 —，来源目录用小字标出来
      const devName = r.device ? (deviceLabelOf(r.device) || r.device) : "—";
      const srcDir = r.dir ? String(r.dir).replace(/^\d{4}-\d{2}-\d{2}-/, "") : "";
      return '<tr><td class="mono nowrap">' + esc(r.time) + '</td>'
        + '<td class="nowrap"><span class="tag">' + esc(devName) + '</span>'
        + (srcDir ? '<div class="dim small mono" title="' + esc(r.dir) + '">' + esc(srcDir) + '</div>' : '')
        + '</td>'
        + '<td class="nowrap">' + esc(r.panel_label) + '</td>'
        + '<td>' + esc(r.task) + '</td>'
        + '<td class="mono nowrap">' + esc(cnt) + '</td>'
        + '<td><span class="tag ' + cls + '">' + esc(st) + '</span></td>'
        + '<td class="mono nowrap">' + (r.duration_s == null ? "—" : esc(r.duration_s)) + 's</td>'
        + '<td class="reason" title="' + esc(r.reason) + '">' + esc(r.reason_short || "") + '</td></tr>';
    }).join("") : '<tr><td colspan="8" class="dim">还没有轮次记录</td></tr>';
    $("#rounds").dataset.sig = rsig;
    if (rbody) rbody.scrollTop = keep;
  }

  // 2026-11-04 用户要求：待教 / 导航失败 拆成两块、各自可收起；收起后标题上只留条数
  setBoxMeta("#pending-meta", "（待回答 " + s.pending_open + " 条）");
  setBoxMeta("#navfail-meta", "（" + s.navfail.length + " 次）");
  const PS = s.pending.slice(0, 3);
  $("#pending").innerHTML = PS.length ? PS.map(p =>
    '<div class="item' + (p.answered ? ' answered' : '') + '">'
    + '<div class="item-main"><div class="q">' + esc(p.question) + '</div>'
    + '<div class="meta"><span class="tag ' + (p.answered ? "warn" : "bad") + '">' + (p.answered ? "已回答" : "待回答") + '</span>'
    + '<span class="mono">' + esc(p.ts) + '</span>'
    + '<span>' + esc(p.created_at || "") + '</span>'
    + '<span>设备 ' + esc(p.serial || "?") + '</span></div></div>'
    + '<div class="item-side">'
    + (p.shot_url ? '<a href="' + esc(p.shot_url) + '" target="_blank" rel="noopener"><img class="thumb" loading="lazy" src="' + esc(p.shot_url) + '" alt="截图"></a>' : '<span class="dim small">无截图</span>')
    + '<a class="btn small" href="' + esc(p.teach_url) + '" target="_blank" rel="noopener">去教它 →</a>'
    + '</div></div>').join("") : '<div class="dim">没有待回答的问题</div>';

  const NS = s.navfail.slice(0, 3);
  $("#navfail").innerHTML = NS.length ? NS.map(n =>
    '<div class="item"><div class="item-main">'
    + '<div class="q">' + esc(n.panel_label) + ' · ' + esc(n.verdict) + '</div>'
    + '<div class="meta"><span class="tag bad">失败步骤</span><span class="mono">' + esc(n.steps) + '</span>'
    + '<span class="mono">' + esc(n.at) + '</span><span>设备 ' + esc(n.device || "?") + '</span></div></div>'
    + '<div class="item-side">'
    + (n.shot_url ? '<a class="btn small" href="' + esc(n.shot_url) + '" target="_blank" rel="noopener">看截图</a>' : "")
    + '<a class="btn small" href="' + esc(n.teach_url) + '" target="_blank" rel="noopener">去教它 →</a>'
    + '</div></div>').join("") : '<div class="dim">没有导航失败记录</div>';

  // 2026-11-03：日志按设备分开看（点上面的按钮切换，默认跟着「设备」下拉）
  const ld = logDev();
  const blob = (s.logs || {})[ld] || (ld === deviceSel() ? s.log : null)
    || {lines: [], source: "（这台还没有日志）"};
  paintLogBox(blob, ld);
  // 2026-11-03 修（与交接文档「血泪坑 7」同一类坑）：这两个按钮原来是**写死旧 IP** 的，
  // 换网络后 data-logdev 全是死地址 -> 点一下就把日志切到不存在的设备 -> 整片空白。
  // 现在和「设备」下拉一样，由后端设备表动态生成。
  const box = $("#log-tab-btns");
  if (box) {
    const bsig = deviceList().map(d => d.serial + "|" + d.label).join(",");
    if (box.dataset.sig !== bsig) {
      box.innerHTML = deviceList().map(d =>
        '<button class="btn xs" data-logdev="' + esc(d.serial) + '">' + esc(d.label) + '</button>'
      ).join(" ");
      box.dataset.sig = bsig;
    }
  }
  const tabs = $("#log-tabs");
  if (tabs) tabs.querySelectorAll("[data-logdev]").forEach(b => {
    b.classList.toggle("primary", b.dataset.logdev === ld);
  });
}

function render(s){
  LAST = s;
  if (STREAM_ON){
    // SSE 帧（<=500ms）比这次轮询（<=3s）新：日志和运行状态以 SSE 的为准，
    // 否则每 3 秒轮询回来一次就把日志框往回倒一行 —— 用户会觉得"还是在卡"。
    const lg = s.logs || (s.logs = {});
    Object.keys(SSE_LOGS).forEach(k => { lg[k] = SSE_LOGS[k]; });
    if (SSE_RUNS) s.runs = SSE_RUNS;
  }
  syncDeviceOptions();   // 设备下拉由后端设备表动态生成（换网络后地址变了也能跟上）
  // 2026-11-02：顶部状态条把**每台**在跑的都列出来（别只显示一台）
  const runNow = (s.runs || []).filter(r => r.running);
  $("#pill").className = "pill " + (runNow.length ? "on" : "off");
  $("#pill").textContent = runNow.length
    ? ("运行中 · " + runNow.map(r => (r.label || r.serial) + "(pid " + r.pid + ")").join(" + "))
    : (s.console.control.paused ? "已暂停" : "空闲");
  $("#clock").textContent = s.now;
  TEACH = s.teach_url;
  renderControl(s);
  renderRuns(s);
  renderLive(s);
  renderScreen(s);
  renderDevices(s);
  renderJump(s);
  renderMode(s);
  renderManual(s);
  renderOld(s);
  const ssc = s.scripts || {};
  renderPaneDone(ssc.panel_done);
  renderDraft(ssc.draft);
  renderScripts(ssc);
}

async function poll(){
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    render(await r.json());
  } catch (e) {
    $("#pill").className = "pill err";
    $("#pill").textContent = "面板连接断开";
  }
  // 2026-11-04 用户要求：App 自检结果只在「跑完那一刻」变，没必要每 3 秒重读一次 12KB 的 apps.json。
  // 现在：打开面板读一次；点按钮后只在「正在跑」期间每秒跟一次，跑完自动停（见 acFollowStart）。
}

// ---------------------------------------------------------------- 实时推送（SSE）
// 2026-11-04 用户要求（原话：「这个不是实时的吗，我想要实时的，监控尽量都做成实时的」）：
// 3 秒轮询最坏晚 3 秒，现场没法判断。这里接 /api/stream：500ms 一帧，只更新
// 日志框 / 顶部状态条 / 运行状态；rounds、scans、scripts 这些重数据继续走 3 秒轮询。
// EventSource 不可用或连不上 -> 乖乖退回 3 秒轮询（不白屏）；断线重连交给浏览器自己的
// EventSource（默认 3 秒重试一次），这里绝不自己写重连风暴。
let STREAM_ON = false;
const SSE_LOGS = {};      // serial -> {lines, source}：最近一帧带来的日志（lines=null = 这帧没变）
let SSE_RUNS = null;
let SSE_RSIG = "";

function streamApply(f){
  if (!f || !LAST) return;   // 第一帧可能比 /api/state 先到：等下一帧（500ms 后）再画
  STREAM_ON = true;
  LAST.now = f.now || LAST.now;
  if (f.runs && f.runs.length !== undefined){ LAST.runs = f.runs; SSE_RUNS = f.runs; }
  const lg = LAST.logs || (LAST.logs = {});
  Object.keys(f.logs || {}).forEach(k => {
    const b = f.logs[k] || {};
    const prev = SSE_LOGS[k] || lg[k] || {lines: [], source: ""};
    // lines 是 null = 这台的日志这一帧一个字都没改：保留原来的内容（别把日志框刷空）
    const cur = {lines: (b.lines == null ? (prev.lines || []) : b.lines),
                 source: b.source || prev.source};
    SSE_LOGS[k] = cur;
    lg[k] = cur;
  });
  const runNow = (LAST.runs || []).filter(r => r.running);
  $("#pill").className = "pill " + (runNow.length ? "on" : "off");
  $("#pill").textContent = f.pill_state || (runNow.length ? "运行中" : "空闲");
  $("#clock").textContent = LAST.now;
  // 设备行只在「状态真的变了」时重画（elapsed 按 5 秒粗算）：500ms 就把按钮连根重建，
  // 用户点【停止】时容易点在一个刚被换掉的节点上。
  const rsig = (LAST.runs || []).map(r => [r.serial, r.running, r.pid, r.paused, r.state_txt,
    r.what, r.minutes_label, Math.round((r.elapsed_s || 0) / 5)].join(":")).join("|");
  if (rsig !== SSE_RSIG){ SSE_RSIG = rsig; renderRuns(LAST); }
  const ld = logDev();
  const blob = lg[ld] || (ld === deviceSel() ? LAST.log : null);
  if (blob) paintLogBox(blob, ld);
}

function startStream(){
  if (!window.EventSource) return;          // 老浏览器：直接走 3 秒轮询，一样能用
  let es = null;
  try { es = new EventSource("/api/stream"); } catch (e) { return; }
  es.onopen = () => { STREAM_ON = true; };
  es.onmessage = (ev) => {
    let f = null;
    try { f = JSON.parse(ev.data); } catch (e) { return; }
    streamApply(f);
  };
  // 连不上 / 断了：退回轮询（3 秒一刷，页面照旧有数据）；重连由浏览器自己按 3 秒重试
  es.onerror = () => { STREAM_ON = false; };
}

// 2026-11-03 修：下拉框原来是**写死两个老 IP** 的，换了网络（校园网/热点）之后浏览器照样
// 把 192.168.1.8:5555 发给后端 -> auto.py 直接「device not found」（两台都跑不了）。
// 现在：选项由 /api/state 的 console.devices 动态生成；**记的是「手机1/手机2」这个标签**，
// 地址怎么变都跟得上。
const DEVICE_LABEL_KEY = "coin11.dash.targetLabel";

let LOG_DEV = "";            // "" = 跟着「设备」下拉；否则固定看某台的日志
function logDev(){
  // 2026-11-03 修：LOG_DEV 记的是**上次点过的那个地址**。换网络/换端口后它就不在设备表里了，
  // 旧代码照样拿它去 s.logs[ld] 查 -> 查不到 -> 显示「这台还没有日志」（用户报的「切换手机就丢日志」）。
  // 这里自愈：钉住的地址已不在当前设备表 -> 作废，回到跟随「设备」下拉。
  if (LOG_DEV && !deviceList().some(d => d.serial === LOG_DEV)) LOG_DEV = "";
  return LOG_DEV || deviceSel();
}

function deviceList(){
  return ((LAST && LAST.console && LAST.console.devices) || []).map(d => ({
    serial: String(d.serial || ""), label: String(d.label || d.serial || "")
  })).filter(d => d.serial);
}
function deviceLabelOf(serial){
  const hit = deviceList().find(d => d.serial === serial);
  return hit ? hit.label : String(serial || "");
}
function deviceSel(){
  const el = $("#pw-serial");
  if (el && el.value) return el.value;
  const list = deviceList();
  return list.length ? list[0].serial : "";
}
function deviceLabel(){
  return deviceLabelOf(deviceSel());
}
function syncDeviceOptions(){
  const el = $("#pw-serial");
  if (!el) return;
  const list = deviceList();
  if (!list.length) return;
  const sig = list.map(d => d.serial).join(",");
  if (el.dataset.sig === sig) return;
  let wantLabel = "";
  try { wantLabel = localStorage.getItem(DEVICE_LABEL_KEY) || ""; } catch (e) { wantLabel = ""; }
  const prevLabel = deviceLabelOf(el.value) || wantLabel;
  el.innerHTML = list.map(d => '<option value="' + esc(d.serial) + '">' + esc(d.label) + '</option>').join("");
  const hit = list.find(d => d.label === prevLabel) || list[0];
  el.value = hit.serial;
  el.dataset.sig = sig;
}
function deviceSelInit(){
  const el = $("#pw-serial");
  if (!el) return;
  el.onchange = () => {
    // 存**标签**（手机1/手机2）而不是 IP：换网络后地址变了，标签不变
    try { localStorage.setItem(DEVICE_LABEL_KEY, deviceLabel()); } catch (e) {}
    LOG_DEV = "";                      // 日志默认跟着选中的设备
    hint("这一轮将在「" + deviceLabel() + "」上跑（" + el.value + "）· 暂停/继续/停止也作用于这台");
    if (LAST) { renderControl(LAST); renderRuns(LAST); renderLive(LAST); render(LAST); }
  };
}

// 2026-11-02 用户要求：每轮时长可选（30 分钟 / 1 小时 / 2 小时 / 不限时间），选择记在本机
const MINUTES_KEY = "coin11.dash.minutes";

function minuteSel(){
  const el = $("#pw-minutes");
  const v = el ? String(el.value) : "0";       // 2026-11-09：默认不限时间
  return ["30", "60", "120", "0"].indexOf(v) >= 0 ? Number(v) : 0;
}
function minuteSelInit(){
  const el = $("#pw-minutes");
  if (!el) return;
  let v = "";
  try { v = localStorage.getItem(MINUTES_KEY) || ""; } catch (e) { v = ""; }
  if (v && el.querySelector('option[value="' + v + '"]')) el.value = v;
  el.onchange = () => {
    try { localStorage.setItem(MINUTES_KEY, el.value); } catch (e) {}
    hint("本轮时长：" + minuteSelLabel());
  };
}
// 2026-11-09：跑完接不接自定义脚本也记在本机（默认不勾）
const SCRIPTS_KEY = "coin11.dash.scripts";
function scriptsInit(){
  const el = $("#pw-scripts");
  if (!el) return;
  let v = null;
  try { v = localStorage.getItem(SCRIPTS_KEY); } catch (e) { v = null; }
  if (v === "1" || v === "0") el.checked = (v === "1");
  el.onchange = () => {
    try { localStorage.setItem(SCRIPTS_KEY, el.checked ? "1" : "0"); } catch (e) {}
  };
}

function minuteSelLabel(){
  const el = $("#pw-minutes");
  if (!el) return "30 分钟";
  const m = minuteSel();
  if (m === 0) return "不限时间（跑完为止）";
  const o = el.querySelector('option[value="' + String(m) + '"]');
  return o ? o.textContent : (m + " 分钟");
}

// 2026-11-04 用户要求（轮数保险丝）：不限时间时 --max-rounds 是唯一的刹车，必须能在面板上改。
// 空 / 0 / 非法值一律回落到 120（auto.py 的默认值），绝不把坏值发给后端。
const MAXROUNDS_KEY = "coin11.dash.maxRounds";
const MAXROUNDS_DEFAULT = 120;

function maxRoundsVal(){
  const el = $("#pw-maxrounds");
  const v = el ? String(el.value).trim() : "";
  const n = parseInt(v, 10);
  if (!isFinite(n) || n <= 0) return MAXROUNDS_DEFAULT;
  return Math.min(9999, n);
}
function maxRoundsInit(){
  const el = $("#pw-maxrounds");
  if (!el) return;
  let v = "";
  try { v = localStorage.getItem(MAXROUNDS_KEY) || ""; } catch (e) { v = ""; }
  const n = parseInt(v, 10);
  if (isFinite(n) && n > 0) el.value = String(Math.min(9999, n));
  el.onchange = () => {
    const cur = maxRoundsVal();
    el.value = String(cur);                       // 非法 / 0 -> 立刻回填 120，界面上看得到
    try { localStorage.setItem(MAXROUNDS_KEY, el.value); } catch (e) {}
    hint("轮数保险丝 = " + cur + " 轮"
      + (minuteSel() === 0 ? "（不限时间时它就是唯一的刹车）" : "（到点收工，不写成完成）"));
  };
}

// ---------------------------------------------------------------- App 自检
// 2026-11-04 用户要求：用户完全不用命令行，所以【App 自检】按钮 + 结果表格都做进面板。
// 结果来自 GET /api/appcheck（doc = state/apps.json 原文，auto.py 写、面板只读）。
// 刷新节奏复用上面那条 3 秒轮询（poll()），不另起定时器。
let AC_OPEN = false;      // 表格区展开状态：默认收起，别把主界面撑爆
let AC_LAST = null;
let AC_BUSY = false;      // 按钮刚点下、POST 还没回：防连点

function acAt(s){
  return String(s == null ? "" : s).replace("T", " ");
}

function acRow(a){
  a = a || {};
  const inst = a.installed ? '<span class="tag ok">已装</span>' : '<span class="tag bad">未装</span>';
  const able = a.doable
    ? '<span class="tag ok">' + esc(a.doable_label || "能做") + '</span>'
    : '<span class="tag">不做</span>';
  return '<tr><td>' + esc(a.name || "?") + '</td>'
    + '<td class="mono tiny">' + esc(a.pkg || "?") + '</td>'
    + '<td>' + inst + '</td>'
    + '<td>' + able + '</td>'
    + '<td class="tiny dim">' + esc(a.why || "") + '</td></tr>';
}

function acDevBlock(k, v){
  v = v || {};
  const c = v.counts || {};
  const apps = v.apps || [];
  const added = v.added || [];
  const removed = v.removed || [];
  const diff = v.first_run
    ? "与上次比：首次记录（没有上次可比）"
    : ("与上次" + (v.prev_at ? "（" + acAt(v.prev_at) + "）" : "") + "比：新增 " + added.length
       + "、消失 " + removed.length
       + (added.length ? "（新增：" + added.join("、") + "）" : "")
       + (removed.length ? "（消失：" + removed.join("、") + "）" : ""));
  const rows = apps.length
    ? apps.map(acRow).join("")
    : '<tr><td colspan="5" class="dim">这台没有查到外部 App 名单</td></tr>';
  const ali = v.alipay;
  const hd = '<thead><tr><th>名称</th><th>包名</th><th>已装</th><th>能做</th><th>原因</th></tr></thead>';
  return '<div class="devblk">'
    + '<div class="hd"><b>' + esc(v.label || k) + '</b>'
    + '<span class="mono tiny dim">' + esc(v.serial || k) + '</span>'
    + '<span class="tag">机型 ' + esc(v.model || "?") + '</span>'
    + '<span class="tag">pm 包总数 ' + esc(v.pm_total == null ? "?" : v.pm_total) + '</span>'
    + '<span class="tag ok">已装 ' + esc(c.installed == null ? "?" : c.installed) + '</span>'
    + '<span class="tag">可做 ' + esc(c.doable == null ? "?" : c.doable) + '</span>'
    + '<span class="tag bad">未装 ' + esc(c.missing == null ? "?" : c.missing) + '</span>'
    + '</div>'
    + '<div class="dim tiny">' + esc(diff)
    + (v.pm_ok === false ? ' · 注意：这台 pm 查询失败过，上面的数是上次留下的' : '') + '</div>'
    + '<div class="tblwrap"><table>' + hd + '<tbody>' + rows + '</tbody></table></div>'
    + (ali ? ('<div class="ali"><div class="tiny">支付宝（单独一栏：走授权通道，不进通用白名单）</div>'
              + '<table>' + hd + '<tbody>' + acRow(ali) + '</tbody></table></div>') : "")
    + '</div>';
}

function renderAppcheck(d){
  if (!d) return;
  AC_LAST = d;
  const body = $("#appcheck-body"), meta = $("#appcheck-meta"), btn = $("#btn-appcheck");
  if (!body || !meta) return;
  const doc = d.doc || null;
  const devs = (doc && doc.devices) || {};
  const keys = Object.keys(devs);
  const running = d.state === "running";
  if (btn){
    btn.disabled = running;
    btn.textContent = running ? "自检中…" : "App 自检";
  }
  if (running){
    meta.textContent = "正在查两台手机的 pm 包列表（只读，不点屏幕）…"
      + (keys.length ? "（下面是上一次的结果）" : "");
  } else if (!keys.length){
    meta.textContent = d.state === "error"
      ? ("上次自检失败：" + (d.error || "未知"))
      : "还没跑过（点【App 自检】，两台一起查）";
  } else {
    meta.textContent = "上次 " + (acAt(d.at) || acAt(doc.at) || "?") + " · "
      + keys.map(k => {
          const v = devs[k] || {}, c = v.counts || {};
          return (v.label || v.serial || k) + " 已装 "
            + (c.installed == null ? "?" : c.installed) + "/" + (c.total == null ? "?" : c.total);
        }).join(" · ")
      + (d.error ? " · " + d.error : "");
  }
  if (!keys.length){
    const msg = d.state === "error"
      ? ("自检失败：" + (d.error || "未知") + "（再点一次【App 自检】重跑）")
      : (running
         ? "第一次自检在跑，跑完这里按设备列出：名称 / 包名 / 已装 / 能做 / 原因。"
         : "点【App 自检】会在后台跑 python auto.py --appcheck：只读 pm 包列表，两台一起查；跑完这里出表。");
    body.innerHTML = '<div class="' + (d.state === "error" ? "note" : "dim tiny") + '">' + esc(msg) + '</div>';
  } else {
    body.innerHTML = keys.map(k => acDevBlock(k, devs[k])).join("");
  }
  acApplyOpen();   // 展开状态只有 AC_OPEN 一个来源（收起时 body 不占位，标题一行就够）
}

// App 自检：**只在「正在跑」期间**每秒跟一次；一旦 state != running 就停（不是常驻定时器）。
let AC_FOLLOW = null;
function acFollowStart(){
  if (AC_FOLLOW) return;
  const tick = async () => {
    AC_FOLLOW = null;
    let d = null;
    try {
      const r = await fetch("/api/appcheck", {cache: "no-store"});
      if (r.ok) d = await r.json();
    } catch (e) { return; }
    if (d) renderAppcheck(d);
    if (d && d.state === "running") AC_FOLLOW = setTimeout(tick, 1000);
  };
  AC_FOLLOW = setTimeout(tick, 800);
}

async function refreshAppcheck(force){
  if (AC_BUSY && !force) return;   // 自己刚点下按钮：请求还没回，别插队
  try {
    const r = await fetch("/api/appcheck", {cache: "no-store"});
    if (!r.ok) return;
    renderAppcheck(await r.json());
  } catch (e) { /* 面板断开时 poll() 已经在顶部提示，这里静默 */ }
}

async function post(url, body){
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  return await r.json();
}

async function act(url, body, okMsg){
  const r = await post(url, body);
  hint(r.ok ? (okMsg || "已执行") : ("失败：" + (r.error || "未知")));
  poll();
  return r;
}

$("#btn-pause").onclick = async () => {
  $("#btn-pause").disabled = true;
  const nm = deviceLabel();
  hint("正在写「" + nm + "」的暂停标志 … auto.py 会在下一个安全点停下（不丢上下文，点继续从原处接着跑）");
  const r = await post("/api/pause", {serial: deviceSel()});
  hint(r.ok ? ("已暂停「" + (r.label || nm) + "」" + r.at + "：auto.py 做完当前这一步就原地等【继续】"
              + "（另一台不受影响）") : ("暂停失败：" + (r.error || "未知")));
  poll();
};

$("#btn-resume").onclick = async () => {
  $("#btn-resume").disabled = true;
  const nm = deviceLabel();
  hint("正在恢复「" + nm + "」…");
  const r = await post("/api/resume", {serial: deviceSel()});
  hint(r.ok ? ("已继续「" + (r.label || nm) + "」" + r.at + "：从原处接着跑，不重新导航、不重扫面板")
            : ("继续失败：" + (r.error || "未知")));
  poll();
};

$("#btn-kill").onclick = async () => {
  const nm = deviceLabel();
  if (!confirm("停止「" + nm + "」= 真的杀掉这台的 auto.py 进程（证据不会丢，但下次要重新导航）。另一台继续跑。确定？")) return;
  $("#btn-kill").disabled = true;
  hint("正在停止「" + nm + "」…");
  const r = await post("/api/stop", {serial: deviceSel()});
  hint(r.ok ? ("已停止「" + (r.label || nm) + "」" + r.at + "（杀进程："
              + (r.killed ? ("pid " + r.pid) : "无进程在跑") + "，这台的保活已关）")
            : ("失败：" + (r.error || "未知")));
  poll();
};

$("#btn-stop-all").onclick = async () => {
  if (!confirm("全部停止 = 把两台手机上的 auto.py 进程都杀掉（证据不会丢，下次要重新导航）。确定？")) return;
  $("#btn-stop-all").disabled = true;
  hint("正在全部停止 …");
  const r = await post("/api/stop_all", {});
  hint(r.ok ? ("已全部停止 " + r.at + "（杀进程："
              + ((r.pids && r.pids.length) ? r.pids.join(",") : "无进程在跑") + "，保活已关）")
            : ("失败：" + (r.error || "未知")));
  poll();
};

// 设备状态那一块：每台各自的【暂停】【继续】【停止】（2026-11-02 用户要求按设备独立）
$("#devstat").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-run]");
  if (!b) return;
  const op = b.dataset.run, serial = b.dataset.serial;
  const nm = deviceLabelOf(serial) || serial;
  if (op === "stop" && !confirm("只停「" + nm + "」的 auto.py（另一台继续跑）。确定？")) return;
  b.disabled = true;
  const url = op === "stop" ? "/api/stop" : (op === "pause" ? "/api/pause" : "/api/resume");
  hint("正在对「" + nm + "」执行：" + (op === "stop" ? "停止" : (op === "pause" ? "暂停" : "继续")) + " …");
  const r = await post(url, {serial: serial});
  hint(r.ok ? (nm + "：" + (op === "pause" ? "已暂停（做完当前这一步就原地等【继续】）"
                     : op === "resume" ? "已继续（从原处接着跑）"
                     : ("已停止 pid " + (r.pid == null ? "—" : r.pid))))
            : ("失败：" + (r.error || "未知")));
  poll();
});

$("#btn-start").onclick = async () => {
  // 2026-11-02 用户要求（1A）：启动时自己勾面板 + 是否强制重跑 + 跑完接不接脚本
  const whats = [];
  if ($("#pw-coin").checked) whats.push("coin");
  if ($("#pw-race").checked) whats.push("race");
  if (!whats.length){
    hint("至少要勾一个面板（淘金币 / 赛车能量）。只想跑脚本 → 去【🎬 脚本/录制】点【▶ 一键运行】");
    return;
  }
  $("#btn-start").disabled = true;
  const minutes = minuteSel();
  const rounds = maxRoundsVal();
  hint("正在启动 auto.py（" + deviceLabel() + " · 面板 " + whats.join("+") + ($("#pw-force").checked ? " · 强制重跑" : "")
       + ($("#pw-scripts").checked ? " · 跑完接着跑脚本" : " · 不跑脚本")
       + " · 时长 " + minuteSelLabel() + " · 最多 " + rounds + " 轮）…");
  const r = await post("/api/start", {whats: whats, force_panels: $("#pw-force").checked,
                                    scripts: $("#pw-scripts").checked, minutes: minutes,
                                    max_rounds: rounds, serial: deviceSel()});
  hint(r.ok ? ("已启动 pid " + r.pid + " · 命令：" + (r.cmd || (r.what + " " + r.minutes + " 分钟")))
            : ("启动失败：" + (r.error || "未知")));
  poll();
};

// 2026-11-09 用户要求：【⏻ 关闭面板】= 停掉正在跑的一轮 + 关保活 + 关面板进程，一次收工。
async function panelShutdown(skipConfirm) {
  const running = ((LAST && LAST.runs) || []).filter(r => r.running);
  const msg = running.length
    ? ("现在 " + running.map(r => r.label || r.serial).join("、") + " 上有 auto.py 在跑，"
       + "关闭会把这一轮直接停掉（下次从下一轮重新开始）。\n\n确定关闭面板并退出吗？")
    : "关闭面板并退出？关掉之后这个页面就不能再刷新了。";
  if (!skipConfirm && !confirm(msg)) return;
  const b = $("#btn-shutdown");
  b.disabled = true;
  b.textContent = "正在关闭…";
  let r = null;
  try { r = await post("/api/shutdown", {}); } catch (e) { r = {ok: false, error: String(e)}; }
  if (!r || !r.ok) {
    b.disabled = false;
    b.textContent = "⏻ 关闭面板";
    hint("关闭失败：" + ((r && r.error) || "未知"));
    return;
  }
  const stopped = (r.stopped || []).length;
  document.body.innerHTML = '<div style="padding:40px;font:16px/1.9 system-ui,sans-serif;color:#ddd">'
    + '<h2 style="margin:0 0 12px">面板已关闭</h2>'
    + '<div>停掉了 ' + stopped + ' 个正在跑的进程 · ' + esc(r.at || "") + ' 退出</div>'
    + '<div style="margin-top:12px;color:#888">这个窗口应该会自己关掉；没关掉就按 Ctrl+W 或点叉。'
    + '要再跑就重新双击启动程序。</div></div>';
  // app 模式窗口允许脚本关自己（用户手开的标签页会被浏览器拒绝，那就留在上面这行提示）
  try { window.close(); } catch (e) {}
}

$("#btn-shutdown").onclick = () => panelShutdown(false);
// 也可以在地址后面加 ?shutdown=1 直接关（做成书签：点一下就整台收工，不问）
if (/[?&]shutdown=1/.test(location.search || "")) {
  setTimeout(function () { panelShutdown(true); }, 400);
}

// 2026-11-04 用户要求（App 自检）：按钮只管「起后台线程」，HTTP 立刻返回；结果按 3 秒轮询回来。
$("#btn-appcheck").onclick = async () => {
  if (AC_BUSY) return;                    // 连点保护：一次只发一个请求
  AC_BUSY = true;
  AC_OPEN = true;                         // 点完就展开结果区：跑完直接看表
  acApplyOpen();
  const btn = $("#btn-appcheck");
  btn.disabled = true;
  btn.textContent = "自检中…";
  hint("正在后台跑 App 自检（两台一起查 pm 包列表：只读、不点屏幕、不跑任务；每台大约十几秒）…");
  let r = null;
  try { r = await post("/api/appcheck", {}); } catch (e) { r = {ok: false, error: String(e)}; }
  AC_BUSY = false;
  if (r && r.ok){
    hint(r.already ? "App 自检已经在跑（上一轮还没结束，这次没有重复启动），查完自动出表"
                   : "App 自检已开始（后台线程在跑，页面不卡）：查完这里自动出表");
    acFollowStart();          // 只在跑的过程中跟，跑完自动停
  } else {
    hint("App 自检启动失败：" + ((r && r.error) || "未知"));
    if (AC_LAST) renderAppcheck(AC_LAST);
  }
  poll();
};

// ---------------------------------------------------------------- 收起 / 展开
// 2026-11-04 用户要求：待教的地方 / 导航失败 / 我的操作记录 默认收起（收起时只留标题 + 条数），
// 点标题展开；展开后最多 160px，超出在框内自己滚。App 自检同样默认收起。
// 收起状态只体现在 .box.coll 上的 open 类，CSS 负责隐藏 body（不占位 = 右栏不被挤）。
function setBoxMeta(sel, txt){
  const el = document.querySelector(sel);
  if (!el) return;
  el.textContent = txt;
  el.title = txt;          // 条数被省略号截断时，鼠标悬停能看全
}
// App 自检：AC_OPEN 是唯一开关（renderAppcheck 每次都按它同步一次，别让两处状态打架）
function acApplyOpen(){
  const box = document.getElementById("box-appcheck");
  if (box) box.classList.toggle("open", !!AC_OPEN);
  const b = document.getElementById("appcheck-body");
  if (b) b.style.display = AC_OPEN ? "block" : "none";
  const t = document.getElementById("appcheck-toggle");
  if (t) t.textContent = AC_OPEN ? "收起" : "展开";
}
function bindCollapse(id){
  const box = document.getElementById(id);
  const hd = box && box.querySelector("h3");
  if (!hd) return;
  hd.onclick = () => {
    const open = !box.classList.contains("open");
    if (id === "box-appcheck"){ AC_OPEN = open; acApplyOpen(); }
    else box.classList.toggle("open", open);
    setTimeout(refreshSplitters, 0);      // 展开/收起后：可伸缩的块变了，分界线跟着重画
  };
}
["box-appcheck", "box-pending", "box-navfail", "box-manual"].forEach(bindCollapse);

$("#btn-teach").onclick = () => window.open(TEACH, "_blank");


$("#btn-find-dev").onclick = async () => {
  const b = $("#btn-find-dev");
  const old = b.textContent;
  b.disabled = true; b.textContent = "扫描中…";
  hint("正在扫局域网找手机（推网段 → 找主机 → 扫随机端口 → 读机型），约 15~40 秒，别关页面…");
  try {
    const r = await post("/api/find_devices", {});
    hint(r.ok ? ("找到：" + (r.summary || "")) : ("没找到：" + (r.error || "未知")));
    poll();
  } catch (e) {
    hint("扫描失败：" + e);
  } finally {
    b.disabled = false; b.textContent = old;
  }
};

$("#btn-refresh-dev").onclick = async () => {
  const b = $("#btn-refresh-dev");
  const old = b.textContent;
  const el = $("#pw-devaddr");
  const addr = el ? String(el.value || "").trim() : "";
  b.disabled = true; b.textContent = "查找中…";
  try {
    const r = await post("/api/refresh_devices", {addr: addr});
    hint(r.ok ? ("设备地址已刷新：" + (r.summary || "")) : ("刷新失败：" + (r.error || "未知")));
    if (r.ok && el) el.value = "";
    poll();
  } catch (e) {
    hint("刷新失败：" + e);
  } finally {
    b.disabled = false; b.textContent = old;
  }
};

$("#devices").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act=keep]");
  if (!b) return;
  const on = b.dataset.on === "1";
  const sel = document.querySelector('select[data-act=iv][data-serial="' + b.dataset.serial + '"]');
  b.disabled = true;
  const r = await post("/api/keepalive", {serial: b.dataset.serial, on: on, interval: sel ? Number(sel.value) : null});
  hint(r.ok ? ("[" + b.dataset.serial + "] 保活 " + (r.on
    ? ("已开：每 " + r.interval + " 分钟发一次 keyevent 224（系统认作「用户活动」会重置息屏计时；只点亮、不点不滑、界面不动；脚本运行时自动让路）")
    : "已关：不再保活")) : ("保活失败：" + (r.error || "未知")));
  poll();
});

$("#devices").addEventListener("change", async (e) => {
  const s = e.target.closest("select[data-act=iv]");
  if (!s) return;
  const btn = document.querySelector('[data-act=keep][data-serial="' + s.dataset.serial + '"]');
  const on = btn ? btn.dataset.on !== "1" : false;
  const r = await post("/api/keepalive", {serial: s.dataset.serial, on: on, interval: Number(s.value)});
  hint(r.ok ? ("[" + s.dataset.serial + "] 间隔改为 " + r.interval + " 分钟（保活 " + (r.on ? "开" : "关") + "）") : ("设置失败：" + (r.error || "未知")));
  poll();
});

document.querySelectorAll("input[name=jp]").forEach(el => {
  el.onchange = async () => {
    const r = await post("/api/jump_policy", {policy: el.value});
    hint(r.ok ? ("跳转确认策略 = " + r.label + "（已写入 " + r.path + "）") : ("写入失败：" + (r.error || "未知")));
    poll();
  };
});

$("#screen").onload = () => { $("#screen-msg").classList.add("hide"); };
$("#screen").onerror = () => {
  $("#screen-msg").classList.remove("hide");
  $("#screen-msg").textContent = "画面暂不可用（抓帧失败 / 设备忙）";
};

$("#screen").addEventListener("click", async (e) => {
  if (OMODE !== "op"){ hint("现在是看图模式：要操作手机请先切到「操作模式」（防误点）"); return; }
  const img = e.currentTarget;
  const rect = img.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const x = Math.round((e.clientX - rect.left) / rect.width * SCR.w);
  const y = Math.round((e.clientY - rect.top) / rect.height * SCR.h);
  if (x < 0 || y < 0 || x >= SCR.w || y >= SCR.h){ hint("点到画面外了"); return; }
  if (!confirm("在 (" + x + ", " + y + ") 点击？（设备 " + SCREEN_SERIAL + " " + SCR.w + "x" + SCR.h + "）")) return;
  await panelOp("tap", x, y, "面板投屏点击");
});

document.querySelectorAll("input[name=omode]").forEach(el => {
  el.onchange = () => { OMODE = el.value; if (LAST) renderScreen(LAST); };
});

$("#s-serial").onchange = () => {
  SCREEN_SERIAL = $("#s-serial").value;
  hint("投屏已切到 " + SCREEN_SERIAL);
  screenTick();
  poll();
};

$("#btn-full").onclick = () => {
  const box = $("#screenbox");
  box.classList.toggle("full");
  $("#btn-full").textContent = box.classList.contains("full") ? "✕ 退出全屏" : "⛶ 全屏";
};

$("#quicks").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-op]");
  if (!b) return;
  if (!SCREEN_SERIAL){ hint("还没选设备"); return; }
  b.disabled = true;
  await panelOp(b.dataset.op, null, null, "面板快捷：" + (b.textContent || "").trim());
  b.disabled = false;
});

$("#btn-auto").onclick = async () => {
  $("#btn-auto").disabled = true;
  const out = await post("/api/task_hint_clear", {});
  hint(out.ok ? ("已恢复自动选择（" + (out.cleared ? "task_hint.json 已清除" : "本来就没有指定") + "）") : ("清除失败：" + (out.error || "未知")));
  poll();
};

$("#m-auto").onchange = async () => {
  const on = $("#m-auto").checked;
  const r = await post("/api/settings", {auto_record: on});
  hint(r.ok ? ("自动记录已" + (r.auto_record ? "打开（试点击/开始跑一轮会自动写一条）" : "关闭")) : ("设置失败：" + (r.error || "未知")));
};

$("#m-snap").onclick = async () => {
  const serial = $("#m-serial").value;
  $("#m-snap").disabled = true; hint("正在截取 " + serial + " 的屏幕 …");
  const r = await post("/api/snap", {serial: serial});
  $("#m-snap").disabled = false;
  if (r.ok){ SHOTS.push({path: r.path, url: r.url}); hint("已截屏：" + r.path + "（" + r.bytes + " 字节）"); }
  else hint("截屏失败：" + (r.error || "未知"));
  poll();
};

$("#m-shots").addEventListener("click", (e) => {
  const a = e.target.closest("[data-drop]");
  if (!a) return;
  e.preventDefault();
  SHOTS.splice(Number(a.dataset.drop), 1);
  if (LAST) renderManual(LAST);
});

$("#m-save").onclick = async () => {
  const serial = $("#m-serial").value;
  const action = ($("#m-action").value || "").trim();
  if (!action){ hint("请先写一句「我做了什么」"); return; }
  $("#m-save").disabled = true;
  const r = await post("/api/manual", {serial: serial, action: action, note: $("#m-note").value || "", screenshot: SHOTS.map(x => x.path).join(",")});
  $("#m-save").disabled = false;
  if (r.ok){ hint("已记录：" + r.record.ts + " · " + r.record.action); $("#m-action").value = ""; $("#m-note").value = ""; SHOTS = []; poll(); }
  else hint("保存失败：" + (r.error || "未知"));
};

$("#t-go").onclick = async () => {
  const serial = $("#t-serial").value;
  if (!confirm("真的在 " + serial + " 上点一次 (" + $("#t-x").value + ", " + $("#t-y").value + ")？")) return;
  $("#t-go").disabled = true; hint("正在点击 …");
  const r = await post("/api/tap", {serial: serial, x: Number($("#t-x").value), y: Number($("#t-y").value)});
  $("#t-go").disabled = false;
  hint(r.ok ? ("已点击 " + serial + " @(" + r.x + "," + r.y + ") " + r.at) : ("点击失败：" + (r.error || "未知")));
  poll();
};

// ---- 自定义脚本 / 录制（2026-11-02 用户要求 4A：可排队 + 每个脚本一个「是否要做」+ 每天只成功跑一次 + 也能单跑）----
let SCRIPTS = [];
let DRAFT = {steps: [], active: false, count: 0};

function stepText(st){
  const lb = OP_LBL[st.op] || st.op;
  if (st.op === "tap") return lb + "(" + st.x + "," + st.y + ")";
  if (st.op === "wait") return "等" + st.secs + "秒";
  return lb;
}

function renderPaneDone(pd){
  pd = pd || {};
  const rows = pd.panels || [];
  const box = $("#pdone");
  if (box) box.innerHTML = rows.length
    ? rows.map(p => {
        const txt = p.done
          ? "今天已完成（启动会跳过，勾【强制重跑】可覆盖）"
          : (p.date ? ("今天没完成 · 上次 " + esc(p.date)) : "今天没完成");
        return '<span class="tag ' + (p.done ? "ok" : "dim") + '">' + esc(p.label) + "：" + txt + "</span>";
      }).join(" ")
    : ('<span class="dim tiny">读不到 ' + esc(pd.file || "coin11/state/panel_done.json") + "（auto.py 还没写过）</span>");
  rows.forEach(p => {
    const t = $("#pw-" + p.key + "-tag");
    if (!t) return;
    t.textContent = p.done ? "今天已完成" : "";
    t.className = "tag tiny " + (p.done ? "ok" : "dim");
    t.style.display = p.done ? "" : "none";
  });
}

function renderDraft(d){
  if (d) DRAFT = d;
  const n = (DRAFT.steps || []).length;
  const meta = $("#rec-meta");
  if (meta) meta.textContent = DRAFT.active
    ? ("（● 正在录制 · 已记 " + n + " 步 · 开始于 " + (DRAFT.started || "?") + "）")
    : (n ? ("（没在录制 · 草稿里还有 " + n + " 步：点【● 开始录制】接着记，或直接【✔ 结束并保存】）")
         : "（没在录制 · 草稿是空的）");
  const box = $("#rec-steps");
  if (box) box.textContent = n
    ? (DRAFT.steps.map((st, i) => (i + 1) + ". " + stepText(st) + (st.note ? ("   // " + st.note) : "")).join("\n")
       + "\n\n草稿文件：" + (DRAFT.file || "coin11/state/record_draft.json"))
    : "还没有步骤：点【● 开始录制】，然后在左边「操作」模式点手机。";
  const b = $("#rec-start");
  if (b){ b.textContent = DRAFT.active ? "● 录制中…" : "● 开始录制"; b.classList.toggle("on", !!DRAFT.active); }
}

function renderScripts(p){
  p = p || {};
  const list = p.scripts || [];
  SCRIPTS = list;
  const meta = $("#scripts-meta");
  if (meta) meta.textContent = "（" + list.length + " 个 · " + (p.dir || "coin11/scripts") + " · 今天 " + (p.today || "?") + "）";
  const tb = $("#scripts");
  if (!tb) return;
  const sig = list.map(it => [it.id, it.enabled ? 1 : 0, it.order, it.steps_count,
    (it.last || {}).last_status || "", it.ok_today ? 1 : 0].join(":")).join("|");
  if (tb.dataset.sig === sig) return;
  tb.dataset.sig = sig;
  tb.innerHTML = list.length ? list.map(it => {
    const last = it.last || {};
    const cls = last.last_status === "ok" ? "ok" : (last.last_status ? "bad" : "dim");
    const lastTxt = last.last_status
      ? ('<span class="tag ' + cls + '">' + esc(last.last_status) + '</span><div class="dim tiny">'
         + esc(last.last_try || "") + (last.last_ok_date ? (" · 成功 " + esc(last.last_ok_date)) : "")
         + (last.last_error ? (" · " + esc(last.last_error)) : "") + "</div>")
      : '<span class="dim tiny">还没跑过</span>';
    const stepsTxt = (it.steps || []).map(stepText).join(" → ");
    return '<tr data-id="' + esc(it.id) + '">'
      + '<td class="mono tiny">' + esc(it.id) + '</td>'
      + '<td>' + esc(it.name) + (it.note ? '<div class="dim tiny">' + esc(it.note) + '</div>' : "") + '</td>'
      + '<td class="mono" title="' + esc(stepsTxt) + '">' + it.steps_count + '</td>'
      + '<td><label class="radio"><input type="checkbox" data-sct="en" data-id="' + esc(it.id) + '"'
      + (it.enabled ? " checked" : "") + '> 要做</label>'
      + (it.ok_today ? '<div class="dim tiny">今天成功过（排队会跳过）</div>' : "") + '</td>'
      + '<td class="nowrap"><span class="mono tiny">' + it.order + '</span> '
      + '<button class="btn xs ghost" data-sct="up" data-id="' + esc(it.id) + '">↑</button>'
      + '<button class="btn xs ghost" data-sct="down" data-id="' + esc(it.id) + '">↓</button></td>'
      + '<td>' + lastTxt + '</td>'
      + '<td class="nowrap"><button class="btn xs primary" data-sct="run" data-id="' + esc(it.id) + '">▶ 一键运行</button> '
      + '<button class="btn xs" data-sct="del" data-id="' + esc(it.id) + '">删除</button></td></tr>';
  }).join("") : '<tr><td colspan="7" class="dim">还没有脚本：点【● 开始录制】在手机上点一遍，再【✔ 结束并保存】</td></tr>';
}

async function loadScripts(){
  let r = null;
  try { r = await (await fetch("/api/scripts?serial=" + encodeURIComponent(deviceSel()), {cache: "no-store"})).json(); }
  catch (e){ hint("读取脚本失败：" + e); return; }
  if (!r || !r.ok){ hint("读取脚本失败：" + ((r && r.error) || "未知")); return; }
  renderScripts(r); renderDraft(r.draft); renderPaneDone(r.panel_done);
}

$("#btn-open-scripts").onclick = () => openPane("scripts");

$("#rec-start").onclick = async () => {
  const r = await post("/api/record_start", {serial: SCREEN_SERIAL});
  hint(r.ok ? ("开始录制（草稿 " + ((r.draft || {}).count || 0) + " 步）：到左边切「操作」模式，在手机上点一遍")
            : ("开始录制失败：" + (r.error || "未知")));
  renderDraft(r.draft); poll();
};

$("#rec-undo").onclick = async () => {
  const r = await post("/api/record_undo", {});
  hint(r.ok ? ("已撤销一步（" + (r.undo ? stepText(r.undo) : "") + "），草稿还剩 "
              + ((r.draft || {}).count || 0) + " 步")
            : ("撤销失败：" + (r.error || "未知")));
  renderDraft(r.draft); poll();
};

$("#rec-wait").onclick = async () => {
  const secs = Number($("#rec-secs").value);
  const r = await post("/api/record_wait", {secs: secs});
  hint(r.ok ? ("已插入等待 " + secs + " 秒（草稿 " + ((r.draft || {}).count || 0) + " 步）")
            : ("插入等待失败：" + (r.error || "未知")));
  renderDraft(r.draft); poll();
};

$("#rec-clear").onclick = async () => {
  if (!confirm("清空录制草稿？（已经保存过的脚本不受影响）")) return;
  const r = await post("/api/record_clear", {});
  hint(r.ok ? "草稿已清空" : ("清空失败：" + (r.error || "未知")));
  renderDraft(r.draft); poll();
};

$("#rec-save").onclick = async () => {
  const name = ($("#rec-name").value || "").trim();
  const id = ($("#rec-id").value || "").trim();
  if (!name && !id){ hint("先填个名字（例如「京东每日签到」）再保存"); $("#rec-name").focus(); return; }
  let r = await post("/api/record_stop", {name: name, id: id});
  if (!r.ok && r.id && confirm("已经有脚本「" + r.id + "」了。\n覆盖它？"))
    r = await post("/api/record_stop", {name: name, id: r.id, overwrite: true});
  if (r.ok && r.saved){
    const n = ((r.script || {}).steps || []).length;
    hint("已保存脚本 " + r.script.id + "（" + n + " 步）→ " + r.file);
    $("#rec-name").value = ""; $("#rec-id").value = "";
    renderDraft(r.draft); loadScripts(); poll();
  } else if (r.ok){
    hint(r.note || "已结束录制（没存脚本）"); renderDraft(r.draft); poll();
  } else hint("保存失败：" + (r.error || "未知"));
};

$("#scripts").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-sct]");
  if (!b) return;
  const id = b.dataset.id;
  const act = b.dataset.sct;
  if (act === "run"){
    if (!confirm("一键运行脚本「" + id + "」？\n只跑这一个，跑完 auto.py 会退出；手机上请先把这个 App 开到起始页面。")) return;
    b.disabled = true;
    // 2026-11-03 修：同样漏传 serial（会无视下拉、固定在手机1 上跑），补上
    const r = await post("/api/start", {script: id, minutes: 30, serial: deviceSel()});
    b.disabled = false;
    hint(r.ok ? ("已启动 pid " + r.pid + " · 命令：" + (r.cmd || r.what))
              : ("启动失败：" + (r.error || "未知")));
    poll(); loadScripts(); return;
  }
  if (act === "del"){
    const it = SCRIPTS.filter(x => x.id === id)[0] || {};
    if (!confirm("删除脚本「" + (it.name || id) + "」（" + (it.steps_count || 0) + " 步）？\n会先备份到 coin11/scripts/_deleted/。")) return;
    b.disabled = true;
    const r = await post("/api/script_delete", {id: id});
    b.disabled = false;
    hint(r.ok ? ("已删除 " + id + " · 备份 " + fname(r.backup)) : ("删除失败：" + (r.error || "未知")));
    loadScripts(); poll(); return;
  }
  if (act === "up" || act === "down"){
    b.disabled = true;
    const r = await post("/api/script_order", {id: id, delta: act === "up" ? -1 : 1});
    b.disabled = false;
    hint(r.ok ? ("顺序：" + (r.order || []).join(" → ")) : ("调序失败：" + (r.error || "未知")));
    loadScripts(); return;
  }
});

$("#scripts").addEventListener("change", async (e) => {
  const c = e.target.closest("input[data-sct=en]");
  if (!c) return;
  const r = await post("/api/script_save", {id: c.dataset.id, enabled: c.checked});
  hint(r.ok ? ("脚本 " + c.dataset.id + " 要做 = " + (c.checked ? "是（排队会跑）" : "否（排队跳过）"))
            : ("保存失败：" + (r.error || "未知")));
  loadScripts(); poll();
});

// ---- 设置弹窗：我教过的规则（查看 / 就地编辑 / 删除 / 合并相似）----
let RULES = [];
function fname(p){ return String(p || "").split(/[\\/]/).pop(); }
function rulesHint(t){
  const el = $("#rules-hint");
  if (el) el.textContent = t || "";
  hint(t);
}

async function loadRules(){
  const tb = $("#rules");
  if (!tb) return;
  let r = null;
  try { r = await (await fetch("/api/rules", {cache: "no-store"})).json(); }
  catch (e){ tb.innerHTML = '<tr><td colspan="6" class="dim">读取失败：' + esc(e) + '</td></tr>'; return; }
  if (!r || !r.ok){
    tb.innerHTML = '<tr><td colspan="6" class="dim">读取失败：' + esc((r && r.error) || "未知") + '</td></tr>';
    return;
  }
  RULES = r.rules || [];
  renderRules(r);
}

function renderRules(r){
  const tb = $("#rules"), meta = $("#rules-meta");
  if (meta) meta.textContent = "（共 " + RULES.length + " 条 · 合并阈值 " + (r.dedupe_px || 40)
    + "px · " + (r.path || "coin11/targets/targets.jsonl") + "）";
  if (!RULES.length){ tb.innerHTML = '<tr><td colspan="6" class="dim">还没教过任何规则</td></tr>'; return; }
  tb.innerHTML = RULES.map(it => '<tr data-idx="' + it.idx + '">'
    + '<td class="mono">' + it.idx + '</td>'
    + '<td><input type="text" class="rule-label" value="' + esc(it.label) + '" placeholder="规则名（不能为空）" title="'
    + esc(it.note || "（无备注）") + '" style="width:100%;min-width:130px"></td>'
    + '<td class="nowrap"><span class="dim tiny">x</span> <input type="number" class="rule-x" value="' + esc(it.x)
    + '" style="width:66px"> <span class="dim tiny">y</span> <input type="number" class="rule-y" value="' + esc(it.y)
    + '" style="width:66px"></td>'
    + '<td class="dim small">' + esc(it.device || it.serial || "?") + '<div class="dim tiny">'
    + esc(it.screen_w) + '×' + esc(it.screen_h) + '</div></td>'
    + '<td class="dim small nowrap">' + esc(it.ts) + '</td>'
    + '<td class="nowrap"><button class="btn xs primary" data-act="save" data-idx="' + it.idx + '">保存</button> '
    + '<button class="btn xs" data-act="del" data-idx="' + it.idx + '">删除</button></td>'
    + '</tr>').join("");
}

function ruleOf(idx){ return RULES.filter(r => Number(r.idx) === Number(idx))[0] || {}; }

$("#rules").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const tr = b.closest("tr");
  const idx = Number(b.dataset.idx);
  const cur = ruleOf(idx);
  if (b.dataset.act === "save"){
    const body = {idx: idx, label: tr.querySelector(".rule-label").value,
                  note: cur.note == null ? "" : cur.note,
                  x: tr.querySelector(".rule-x").value, y: tr.querySelector(".rule-y").value};
    b.disabled = true;
    const r = await post("/api/rules_update", body);
    b.disabled = false;
    if (r.ok){
      rulesHint("已保存第 " + idx + " 条「" + r.rule.label + "」@(" + r.rule.x + "," + r.rule.y
                + ") · 备份 " + fname(r.backup));
      loadRules();
    } else rulesHint("保存失败：" + (r.error || "未知"));
    return;
  }
  if (b.dataset.act === "del"){
    if (!confirm("删除第 " + idx + " 条规则「" + (cur.label || "") + "」？\n删除前会自动备份 targets.jsonl。")) return;
    b.disabled = true;
    const r = await post("/api/rules_delete", {idx: idx});
    b.disabled = false;
    if (r.ok){
      rulesHint("已删除第 " + idx + " 条（" + r.before + " → " + r.after + " 条）· 备份 " + fname(r.backup));
      loadRules();
    } else rulesHint("删除失败：" + (r.error || "未知"));
  }
});

$("#rules-dedupe").onclick = async () => {
  if (!confirm("合并相似规则？\n同一设备且坐标距离 < 40px 视为同一条，保留最新 ts 的那条（规则名/备注取更长的）。\n写之前会先备份。")) return;
  const b = $("#rules-dedupe");
  b.disabled = true; rulesHint("正在合并相似规则 …");
  const r = await post("/api/rules_dedupe", {});
  b.disabled = false;
  if (!r.ok){ rulesHint("合并失败：" + (r.error || "未知")); return; }
  rulesHint(r.backup
    ? ("合并完成：" + r.before + " → " + r.after + " 条，删掉 " + (r.removed || []).length + " 条 · 备份 " + fname(r.backup))
    : ("没有相似规则：" + r.before + " 条未改动（" + (r.note || "") + "）"));
  loadRules();
};

$("#rules-backup").onclick = async () => {
  const b = $("#rules-backup");
  b.disabled = true;
  const r = await post("/api/rules_backup", {});
  b.disabled = false;
  if (r.ok){ rulesHint("已把 " + r.count + " 条规则备份到 " + fname(r.backup) + "，列表已重载"); loadRules(); }
  else rulesHint("备份失败：" + (r.error || "未知"));
};

// ---- 记录管理（证据目录：看体积 / 删旧轮次）----------------------------
function fmtBytes(n){
  n = Number(n) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

async function recLoad(){
  let r = null;
  try { r = await (await fetch("/api/records", {cache: "no-store"})).json(); } catch (e) { r = null; }
  const tb = $("#rec-list");
  if (!r || !r.ok){
    tb.innerHTML = '<tr><td colspan="6" class="dim">读不到证据目录：' + esc((r && r.error) || "面板没连上") + '</td></tr>';
    return;
  }
  const dirs = r.dirs || [];
  $("#rec-meta").textContent = "共 " + dirs.length + " 个目录 · 占用 " + fmtBytes(r.total_bytes) + " · " + (r.root || "evidence");
  tb.innerHTML = dirs.length ? dirs.map(function (d) {
    const pick = d.running ? "" : '<input type="checkbox" class="rec-pick" value="' + esc(d.name) + '">';
    const tag = d.running ? ' <span class="tag tiny">正在跑</span>' : "";
    const btn = '<button class="btn xs kill" data-del="' + esc(d.name) + '"' +
                (d.running ? ' disabled title="这台正在跑，先【停止本台】"' : "") + ">删除</button>";
    return "<tr><td>" + pick + '</td><td class="mono">' + esc(d.name) + tag + "</td><td>" + d.rounds +
           "</td><td>" + fmtBytes(d.bytes) + '</td><td class="dim">' + esc(d.mtime) + "</td><td>" + btn + "</td></tr>";
  }).join("") : '<tr><td colspan="6" class="dim">还没有证据目录（跑一轮才会有）</td></tr>';
  tb.querySelectorAll("[data-del]").forEach(function (b) {
    b.onclick = function () { recDelete([b.dataset.del]); };
  });
}

async function recDelete(names){
  names = (names || []).filter(Boolean);
  if (!names.length){ $("#rec-hint").textContent = "先在左边勾要删的目录（或者单个点【删除】）。"; return; }
  if (!confirm("删掉这 " + names.length + " 个证据目录？里面的截图/OCR 一起没，删了不可恢复。\n\n" + names.join("\n"))) return;
  const r = await post("/api/records_delete", {dirs: names});
  if (r.ok){
    $("#rec-hint").textContent = "已删 " + (r.deleted || []).length + " 个，回收 " + fmtBytes(r.freed_bytes) +
      ((r.skipped || []).length ? "；跳过 " + r.skipped.length + " 个（" + r.skipped.map(function (s) { return s.name + "：" + s.why; }).join("；") + "）" : "");
  } else {
    $("#rec-hint").textContent = "删除失败：" + (r.error || "未知");
  }
  recLoad();
}

async function recPrune(){
  const days = Number($("#rec-days").value);
  if (!isFinite(days) || days < 0){ $("#rec-hint").textContent = "天数要写 0 或正数（0 = 今天写的以外的都删）。"; return; }
  if (!confirm("删掉「最后写入超过 " + days + " 天」的全部证据目录？删了不可恢复。")) return;
  const r = await post("/api/records_delete", {keep_days: days});
  if (r.ok){
    $("#rec-hint").textContent = "清理完成：删了 " + (r.deleted || []).length + " 个，回收 " + fmtBytes(r.freed_bytes) +
      ((r.skipped || []).length ? "；跳过 " + r.skipped.length + " 个（正在跑的不动）" : "");
  } else {
    $("#rec-hint").textContent = "清理失败：" + (r.error || "未知");
  }
  recLoad();
}

function openPane(name){
  document.querySelectorAll(".pane").forEach(p => p.classList.remove("on"));
  const el = $("#pane-" + name); if (el) el.classList.add("on");
  const t = {settings: "设置 / 保活 / 手动记录",
             scripts: "自定义脚本 / 录制", log: "运行日志",
             records: "记录管理（证据目录 / 删旧轮次）"}[name] || name;
  $("#modal-title").textContent = t;
  $("#modal").classList.add("open");
  if (LAST){ renderDevices(LAST); renderJump(LAST); renderMode(LAST); renderManual(LAST); }
  if (name === "settings") loadRules();
  if (name === "scripts") loadScripts();
  if (name === "records") recLoad();
}
document.querySelectorAll("[data-pane]").forEach(b => { b.onclick = () => openPane(b.dataset.pane); });
$("#btn-open-settings").onclick = () => openPane("settings");
$("#btn-open-log").onclick = () => openPane("log");
$("#btn-records").onclick = () => openPane("records");
$("#rec-refresh").onclick = recLoad;
$("#rec-prune").onclick = recPrune;
$("#rec-del-sel").onclick = () => recDelete(Array.from(document.querySelectorAll(".rec-pick:checked")).map(function (c) { return c.value; }));
$("#rec-all").onchange = function () {
  const on = $("#rec-all").checked;
  document.querySelectorAll(".rec-pick").forEach(function (c) { c.checked = on; });
};
$("#modal-close").onclick = () => $("#modal").classList.remove("open");
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.remove("open"); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").classList.remove("open"); });

// ---------------------------------------------------------------- 布局：分界可调 / 按比例填满 / 记住
// 2026-11-09 用户定案（第二版）：不要「每块自己拉大小、拖动排序」—— 窗口一缩放就错位。
// 改成和「投屏 / 右栏」一样的分界条模型：右栏所有块用 flex 权重一起**填满**，只在缝里拖分界分配比例；
// 权重和左右栏宽度存 localStorage，窗口怎么缩放都还是占满的。
const LKEY = "coin11.layout.v2";
function lget(){ try { return JSON.parse(localStorage.getItem(LKEY) || "{}") || {}; } catch (e) { return {}; } }
function lset(o){ try { localStorage.setItem(LKEY, JSON.stringify(o)); } catch (e) {} }
function rightBoxes(){
  const r = document.querySelector(".right");
  return r ? Array.prototype.slice.call(r.querySelectorAll(":scope > .box")) : [];
}
// 权重 = flex-grow：所有块按权重一起填满右栏。
// 0 = 固定高（控件区 / 收起的次要块），不参与分配，也不能被拖着改大改小。
function growOf(el){
  const cs = window.getComputedStyle(el);
  const g = parseFloat(cs.flexGrow || "");
  return (isFinite(g) && g > 0) ? g : 0;
}
function weightOf(el){
  const inline = parseFloat(el.style.flexGrow || "");
  if (isFinite(inline) && inline > 0) return inline;
  return growOf(el);
}
function setWeight(el, w){ el.style.flex = Math.max(0.05, w).toFixed(3) + " 1 0"; }
function saveLayout(){
  const lo = lget();
  const w = {};
  rightBoxes().forEach(b => {
    const g = weightOf(b);
    if (b.dataset.k && g > 0) w[b.dataset.k] = Number(g.toFixed(3));   // 固定高块不记
  });
  lo.weights = w;
  const app = document.querySelector(".app");
  const lw = app && app.style.getPropertyValue("--leftw");
  if (lw) lo.leftw = lw.trim();
  delete lo.size; delete lo.order;         // 老版本（自由拉大小 / 拖动排序）的记录不再用
  lset(lo);
}
function applyLayout(){
  const lo = lget();
  const w = lo.weights || {};
  rightBoxes().forEach(b => {
    const k = b.dataset.k;
    if (k && w[k] && growOf(b) > 0) setWeight(b, w[k]);   // 只还给「本来就是可伸缩」的块
  });
  if (lo.leftw) {
    const app = document.querySelector(".app");
    if (app) app.style.setProperty("--leftw", lo.leftw);
  }
}
// 分界条只在「上下两块**都能伸缩**」之间出现：固定高的块（控件区 / 收起的次要块）旁边不给假手柄。
function bindSplitterDrag(sp){
  sp.addEventListener("mousedown", (ev) => {
    const up = sp.previousElementSibling, down = sp.nextElementSibling;
    if (!up || !down || !up.classList.contains("box") || !down.classList.contains("box")) return;
    if (growOf(up) <= 0 || growOf(down) <= 0) return;
    ev.preventDefault();
    sp.classList.add("on");
    const h0 = up.getBoundingClientRect().height;
    const h1 = down.getBoundingClientRect().height;
    const total = h0 + h1;
    if (total <= 0) return;
    const startY = ev.clientY;
    const wsum = weightOf(up) + weightOf(down);
    const move = (e2) => {
      const a = Math.max(48, Math.min(total - 48, h0 + (e2.clientY - startY)));
      if (wsum <= 0) return;
      setWeight(up, wsum * (a / total));
      setWeight(down, wsum * ((total - a) / total));
    };
    const up2 = () => {
      sp.classList.remove("on");
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up2);
      saveLayout();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up2);
  });
}
function refreshSplitters(){
  const r = document.querySelector(".right");
  if (!r) return;
  Array.prototype.slice.call(r.querySelectorAll(":scope > .vsplit")).forEach(x => x.remove());
  const boxes = rightBoxes();
  for (let i = 1; i < boxes.length; i++) {
    const prev = boxes[i - 1], cur = boxes[i];
    if (growOf(prev) <= 0 || growOf(cur) <= 0) continue;   // 中间隔着的固定块旁边不画线
    const sp = document.createElement("div");
    sp.className = "vsplit";
    sp.title = "上下拖动：调整上面那块和下面这块的分界（窗口缩放按比例自适应，会记住）";
    r.insertBefore(sp, cur);
    bindSplitterDrag(sp);
  }
}
function initLayout(){
  const r = document.querySelector(".right");
  applyLayout();
  if (!r) return;
  refreshSplitters();
  // 左右两栏的分界（投屏 / 右栏）
  const sp = $("#splitter"), app = document.querySelector(".app");
  if (sp && app) {
    sp.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      sp.classList.add("on");
      const move = (e2) => {
        const rt = app.getBoundingClientRect();
        const usable = rt.width - 32;
        const x = e2.clientX - rt.left - 10;
        const pct = Math.max(25, Math.min(80, (x / usable) * 100));
        app.style.setProperty("--leftw", pct.toFixed(2) + "%");
      };
      const up = () => {
        sp.classList.remove("on");
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
        saveLayout();
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    });
    sp.addEventListener("dblclick", () => { app.style.removeProperty("--leftw"); saveLayout(); });
  }
  const rb = $("#btn-reset-layout");
  if (rb) rb.onclick = () => {
    if (!confirm("把分界（右栏各块的比例 + 左右两栏宽度）恢复默认？")) return;
    try { localStorage.removeItem(LKEY); } catch (e) {}
    location.reload();
  };
}

function openFromHash(){
  const h = (location.hash || "").replace("#", "");
  if (h === "settings" || h === "tasks" || h === "scripts" || h === "log") openPane(h);
}
openFromHash();
window.addEventListener("hashchange", openFromHash);
// 2026-11-09：布局记歪了（比如某个框被拖成 0 高、看不见了）时的逃生口
if (/[?&]reset-layout=1/.test(location.search || "")) {
  try { localStorage.removeItem(LKEY); } catch (e) {}
  try { history.replaceState(null, "", location.pathname); } catch (e) {}
}
minuteSelInit();
scriptsInit();
maxRoundsInit();
deviceSelInit();
initLayout();
// 日志切换按钮：点一下固定看那台，再点同一台取消（回到跟随「设备」下拉）
(function initLogTabs(){
  const box = $("#log-tabs");
  if (!box) return;
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-logdev]");
    if (!b) return;
    LOG_DEV = (LOG_DEV === b.dataset.logdev) ? "" : b.dataset.logdev;
    if (LAST) render(LAST);
  });
})();
poll();
setInterval(poll, 3000);   // 重数据 + EventSource 不可用时的降级路径，两条腿都留着
startStream();            // 2026-11-04：实时推送（日志/运行状态 500ms）
refreshAppcheck();        // 打开面板先显示上一次的 App 自检结果（之后只在自检跑的时候才跟，不再每 3 秒读一次文件）
// 2026-11-09 修（用户反馈「页面名称跳来跳去」）：这段调试用的标题每 800ms 把正式标题
// （coin11 总控台 v版本号）盖掉，标签页名字就在「版本号」和「页面无滚动」之间来回跳。
// 现在只有地址栏带 #debug 才启用，平时不碰 document.title。
if ((location.hash || "").indexOf("debug") >= 0) {
  setInterval(() => {
    const sh = document.documentElement.scrollHeight, ih = window.innerHeight;
    document.title = "coin11 总控台 v" + ((window.LAST_VERSION) || "")
      + " " + window.innerWidth + "x" + ih + " 页面" + (sh > ih + 1 ? "有滚动" : "无滚动");
  }, 800);
}
</script>
</body>
</html>
'''


# ---------------------------------------------------------------- App 自检（--appcheck）

#: 2026-11-04 用户要求：面板上加【App 自检】按钮，点一下就查两台手机装了哪些外部 App。
#: 只读（auto.py --appcheck 只发 pm list packages，不点屏幕、不跑任务、不写 apps.json 之外的东西）。
#: 关键约束：pm 查询会**卡几十秒**（设备慢/超时），所以 HTTP 必须立刻返回，真正的活交给后台线程。
APPCHECK_JSON = os.path.join(STATE_DIR, "apps.json")     # auto.py 写，面板只读、绝不改结构
APPCHECK_LOG = os.path.join(STATE_DIR, "appcheck.log")   # 面板自己记的 stdout（排查用）

_APPCHECK_LOCK = threading.Lock()
_APPCHECK = {"proc": None, "pid": None, "state": "idle", "started": None,
             "ended": None, "code": None, "error": ""}


def _appcheck_reap_locked():
    """进程收尸：**只在已经退出时**改状态；还在跑就原样保持 running。

    调用方必须持 _APPCHECK_LOCK。退出码 0 -> done；退出码非 0 但 apps.json 已写
    （可能只有一台设备 pm 超时）-> 也算 done，只把 error 留成一句说明。
    """
    st = _APPCHECK
    proc = st.get("proc")
    if proc is None:
        return
    code = proc.poll()
    if code is None:
        return
    st["proc"] = None
    st["code"] = code
    st["ended"] = datetime.now()
    if code == 0:
        st["state"] = "done"
    elif os.path.exists(APPCHECK_JSON):
        st["state"] = "done"
        st["error"] = "进程退出码 %s（结果已写盘，可能有一台设备没查成功）" % code
    else:
        st["state"] = "error"
        st["error"] = _appcheck_log_tail(code)


def _appcheck_log_tail(code, n=6):
    """失败时给一句人话：退出码 + appcheck.log 末尾几行（没有就直说没有）。"""
    lines = []
    try:
        with open(APPCHECK_LOG, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh.readlines()[-n:]]
    except OSError:
        lines = []
    tail = " | ".join([ln for ln in lines if ln])
    if not tail:
        tail = "（没有日志输出：可能 adb 不可用 / 两台都连不上）"
    return ("appcheck 失败（退出码 %s）：%s" % (code, tail))[-700:]


def _appcheck_worker():
    """后台线程：跑 python auto.py --appcheck（**故意不带 --serial** = 一次跑两台）。

    stdout 重定向到 state/appcheck.log（和 start_run 一样不用管道：dash 重启不牵连进程）。
    """
    # 2026-11-09 修（用户反馈「分发版点 App 自检没反应」）：这里漏了 auto_cmd_prefix()。
    # 打包后 sys.executable 是 coin11.exe、AUTO_PY 根本不存在，于是子进程被当成「再起一个面板」，
    # 一撞端口就退出 0 —— 面板看到退出码 0 以为成功，页面上永远是「还没跑过」，点了像没反应。
    cmd = auto_cmd_prefix() + ["--appcheck"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    fh = None
    sink = subprocess.DEVNULL
    code = None
    err = ""
    try:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            fh = open(APPCHECK_LOG, "w", encoding="utf-8", errors="replace")
            sink = fh
        except OSError:
            sink = subprocess.DEVNULL
        _t0 = time.time()          # 起点：用来判「这次到底有没有真写出 apps.json」
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=sink, stderr=subprocess.STDOUT,
                                env=env, creationflags=_NO_WINDOW)
        with _APPCHECK_LOCK:
            _APPCHECK["proc"] = proc
            _APPCHECK["pid"] = proc.pid
        code = proc.wait()
    except Exception as exc:  # noqa: BLE001
        err = "启动失败：%s" % exc
        code = -1
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
    with _APPCHECK_LOCK:
        _APPCHECK["proc"] = None
        _APPCHECK["pid"] = None
        _APPCHECK["code"] = code
        _APPCHECK["ended"] = datetime.now()
        if err:
            _APPCHECK["state"] = "error"
            _APPCHECK["error"] = err
        elif code == 0:
            # 退出码 0 也可能是「什么都没干」（比如子进程根本跑错地方）：要求这次真的写出了
            # apps.json，否则按失败报出来 —— 宁可报错，不要再出现「点了没反应」。
            try:
                _wrote = os.path.getmtime(APPCHECK_JSON) >= _t0 - 1
            except (OSError, NameError):
                _wrote = False
            if _wrote:
                _APPCHECK["state"] = "done"
            else:
                _APPCHECK["state"] = "error"
                _APPCHECK["error"] = (_appcheck_log_tail(code)
                                      or "进程正常退出，但没写出 apps.json（多半没认出手机）")
        elif os.path.exists(APPCHECK_JSON):
            _APPCHECK["state"] = "done"
            _APPCHECK["error"] = "进程退出码 %s（结果已写盘，可能有一台设备没查成功）" % code
        else:
            _APPCHECK["state"] = "error"
            _APPCHECK["error"] = _appcheck_log_tail(code)


def appcheck_start():
    """POST /api/appcheck：立刻回 {ok:true, state:'running'}，真正的 pm 查询在后台线程里。

    连点时**不重复起进程**：已经在跑就返回 running + already:true（幂等，用户点几次都一样）。
    """
    with _APPCHECK_LOCK:
        _appcheck_reap_locked()
        if _APPCHECK["state"] == "running":
            return {"ok": True, "state": "running", "already": True,
                    "pid": _APPCHECK.get("pid"), "error": "",
                    "note": "上一轮自检还在跑，本次没有重复启动"}
        _APPCHECK.update(proc=None, pid=None, state="running", started=datetime.now(),
                         ended=None, code=None, error="")
    threading.Thread(target=_appcheck_worker, name="appcheck", daemon=True).start()
    return {"ok": True, "state": "running", "already": False,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "error": ""}


def appcheck_status():
    """GET /api/appcheck —— {state, at, error, doc}。

    state：running（后台线程在跑）/ done（有 apps.json）/ error（跑挂了且没有 apps.json）/ idle（还没跑过）。
    硬要求：**没跑过但 apps.json 已经在 -> done**（dash 重启后刷新页面照样能看上次结果）。
    at 一律取 apps.json 的 mtime（文件真写出来的那一刻），文件不在才退回内存里的时间。
    """
    with _APPCHECK_LOCK:
        _appcheck_reap_locked()
        mem = dict(_APPCHECK)
    doc = None
    at = ""
    try:
        mt = os.path.getmtime(APPCHECK_JSON)
    except OSError:
        mt = 0.0
    if mt:
        doc = _read_json(APPCHECK_JSON)          # 坏文件当没有：页面显示「还没跑过」，绝不抛
        if doc is not None:
            at = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")
    if mem["state"] == "running":
        return {"ok": True, "state": "running", "at": at, "error": "",
                "doc": doc, "pid": mem.get("pid"), "log": _rel(APPCHECK_LOG)}
    if doc is not None:
        return {"ok": True, "state": "done", "at": at, "error": mem.get("error") or "",
                "doc": doc, "code": mem.get("code"), "log": _rel(APPCHECK_LOG)}
    if mem["state"] == "error":
        return {"ok": True, "state": "error", "at": "", "error": mem.get("error") or "自检失败",
                "doc": None, "code": mem.get("code"), "log": _rel(APPCHECK_LOG)}
    return {"ok": True, "state": "idle", "at": "", "error": "", "doc": None,
            "log": _rel(APPCHECK_LOG)}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "coin11-dash/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        # 浏览器每 500ms 换一次 /live.jpg，旧连接常被直接掐断 → WinError 10053 刷爆 stderr。
        # 这只是 keep-alive 正常抖动，安静吞掉即可。
        try:
            return super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True


    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _sse(self):
        """GET /api/stream —— 实时推送（Server-Sent Events）。

        2026-11-04 用户要求（原话：「这个不是实时的吗，我想要实时的，监控尽量都做成实时的」）：
        原来前端 3 秒轮询，日志最坏晚 3 秒，现场根本没法判断。这里每 SSE_MS（500ms）推一帧，
        只推日志 / 运行状态 / 顶部状态条；重数据（rounds / scans / scripts）继续走 /api/state。

        两条硬规矩：
        - 写失败（浏览器切页 / 关窗 / 断网）= 立刻 return，绝不把线程挂着；
        - 这条连接不许 keep-alive 复用：HTTP/1.1 下没有 Content-Length 的流靠关闭结束，
          不置 close_connection 的话，同一个 socket 上的下一个请求会读到一个永远不结束的响应。
        """
        self.close_connection = True
        try:
            # 写阻塞的超时：手机浏览器切到后台后 TCP 窗口可能填满，写会卡住整个线程；
            # 给个上限，卡住的连接自己烂掉退出，别越积越多。
            self.connection.settimeout(20)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            return
        _STATS["stream_clients"] = _STATS.get("stream_clients", 0) + 1
        sent = {}
        try:
            while True:
                t0 = time.time()
                try:
                    frame = build_stream_frame(sent)
                    blob = ("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n")
                    self.wfile.write(blob.encode("utf-8"))
                    self.wfile.flush()
                except (OSError, ValueError):
                    break            # 浏览器断开 / 切页 / 超时 —— 收工，线程归还
                dt = SSE_MS / 1000.0 - (time.time() - t0)
                if dt > 0:
                    time.sleep(dt)
        finally:
            _STATS["stream_clients"] = max(0, _STATS.get("stream_clients", 1) - 1)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            return self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            _STATS["state_hits"] += 1
            try:
                return self._json(200, build_state())
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"ok": False, "error": "采集失败：%s" % exc})
        if path == "/api/stream":
            return self._sse()
        if path == "/api/records":
            # 【记录管理】：只看体积/轮次，不动文件
            return self._json(200, records_overview())
        if path == "/api/rules":
            out, code = rules_list()
            return self._json(code, out)
        if path == "/api/scripts":
            # 契约 7.2：脚本定义 A + 面板完成记忆 B + 运行状态 C（B/C 只读，A 由面板写）
            # ?serial= 用来按设备显示「今天已完成」（panel_done.json 是 v2 按设备记的）
            try:
                qser = (qs.get("serial") or [""])[0]
                return self._json(200, scripts_list(serial=qser))
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"ok": False, "error": "读取脚本失败：%s" % exc})
        if path == "/api/appcheck":
            # 只读：apps.json 是 auto.py --appcheck 写的，面板不改它的结构
            try:
                return self._json(200, appcheck_status())
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"ok": False, "state": "error", "at": "",
                                        "error": "读取自检结果失败：%s" % exc, "doc": None})
        if path == "/live.jpg":
            return self._live_jpg(qs)
        if path == "/shot":
            return self._shot(qs)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._json(404, {"ok": False, "error": "not found"})

    def _live_jpg(self, qs):
        """实时投屏帧（只读）。抓不到就 503，让页面显示「画面暂不可用」而不是崩。"""
        serial = (qs.get("serial") or [DEFAULT_SERIAL])[0]
        fr, err = live_frame(serial)
        if not fr:
            return self._json(503, {"ok": False, "error": err or "画面暂不可用"})
        return self._send(200, fr["bytes"], "image/jpeg")

    def _shot(self, qs):
        p = (qs.get("p") or [""])[0]
        if not p:
            return self._json(400, {"ok": False, "error": "缺少 p 参数"})
        real = os.path.realpath(p)
        root = os.path.realpath(ROOT)
        ext = os.path.splitext(real)[1].lower()
        if not real.startswith(root + os.sep) or ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return self._json(403, {"ok": False, "error": "只允许工作区内的图片"})
        try:
            with open(real, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return self._json(404, {"ok": False, "error": str(exc)})
        ctype = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/" + ext[1:]
        return self._send(200, data, ctype)

    def _body(self):
        raw = b""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                raw = self.rfile.read(n)
        except Exception:  # noqa: BLE001
            raw = b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8")) or {}
            except Exception:  # noqa: BLE001
                body = {}
        return body if isinstance(body, dict) else {}

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        body = self._body()
        path = parsed.path
        try:
            if path == "/api/start":
                return self._api_start(body)
            if path == "/api/records_delete":
                # 删证据目录：只动 evidence/ 下名字含 -auto 的目录，正在跑的那台不动
                out, code = records_delete(body.get("dirs"), body.get("keep_days"),
                                           bool(body.get("dry_run")))
                return self._json(code, out)
            if path == "/api/find_devices":
                # 自动找手机：扫网段 + 扫随机端口 + 读机型认人（同步，实测十几秒）。
                return self._json(200, find_devices_by_scan())
            if path == "/api/refresh_devices":
                # 手机重启后无线调试端口会变（5555 -> 随机端口）：重新发现并写回 devices.json。
                # 同步执行（几条 adb，几秒），ThreadingHTTPServer 不会拖住别的请求。
                addr = str(body.get("addr") or "").strip()
                return self._json(200, discover_devices_now(addr or None))
            if path == "/api/appcheck":
                # 立刻返回 {ok:true, state:'running'}：pm 查询在后台线程，绝不在请求里同步等
                return self._json(200, appcheck_start())
            if path == "/api/screen_live":
                out = screen_live_set(bool(body.get("on")))
                return self._json(200 if out.get("ok") else 400, out)
            if path == "/api/pause":
                # 带 {serial} = 只暂停这台；不带 = 全部暂停（老行为：写全局 state/pause.flag）
                out, code = pause_now("页面【暂停】", body.get("serial"))
                return self._json(code, out)
            if path == "/api/resume":
                out, code = resume_now("页面【继续】", body.get("serial"))
                return self._json(code, out)
            if path in ("/api/stop", "/api/kill"):
                # 带 {serial} = 只停这台（2026-11-02：停手机2 不许打断手机1）；不带 = 全停
                out, code = force_stop("页面【强制结束】", body.get("serial"))
                return self._json(code, out)
            if path == "/api/shutdown":
                # 【⏻ 关闭面板】：停掉正在跑的一轮 -> 还原保活 -> 关掉服务（进程退出）
                return self._json(200, shutdown_panel(body.get("reason") or "面板【关闭面板】"))
            if path in ("/api/stop_all", "/api/kill_all"):
                out, code = force_stop("页面【全部停止】", None)
                return self._json(code, out)
            if path == "/api/pause_all":
                out, code = pause_now("页面【全部暂停】", None)
                return self._json(code, out)
            if path == "/api/keepalive":
                serial = str(body.get("serial") or "").strip()
                on = bool(body.get("on"))
                iv = body.get("interval")
                out, code = keep_set(serial, on, iv)
                return self._json(code, out)
            if path == "/api/jump_policy":
                out, code = jump_set(body.get("policy"), "页面")
                return self._json(code, out)
            if path == "/api/manual":
                out, code = manual_add(str(body.get("serial") or current_serial()),
                                       body.get("action"), body.get("screenshot", ""),
                                       body.get("note", ""), source="manual")
                return self._json(code, out)
            if path == "/api/snap":
                out, code = snap(str(body.get("serial") or current_serial()))
                return self._json(code, out)
            if path == "/api/tap":
                serial = str(body.get("serial") or current_serial())
                out, code = try_tap(serial, body.get("x"), body.get("y"))
                return self._json(code, out)
            if path == "/api/panel_op":
                serial = str(body.get("serial") or current_serial())
                out, code = panel_op(serial, body.get("op"), body.get("x"), body.get("y"),
                                     body.get("note") or "")
                return self._json(code, out)
            if path == "/api/settings":
                out, code = set_auto_record(bool(body.get("auto_record")))
                return self._json(code, out)
            if path == "/api/task_hint":
                out, code = hint_set(body.get("panel"), body.get("row_key"), body.get("name"),
                                     body.get("n"), body.get("m"), body.get("serial"))
                return self._json(code, out)
            if path == "/api/task_hint_clear":
                out, code = hint_clear("页面【恢复自动】")
                return self._json(code, out)
            if path == "/api/rules_update":
                out, code = rules_update(body.get("idx"), body.get("label"),
                                         body.get("note"), body.get("x"), body.get("y"),
                                         who="页面【规则管理·保存】")
                return self._json(code, out)
            if path == "/api/rules_delete":
                out, code = rules_delete(body.get("idx"), body.get("idxs"),
                                         who="页面【规则管理·删除】")
                return self._json(code, out)
            if path == "/api/rules_dedupe":
                out, code = rules_dedupe(who="页面【合并相似规则】")
                return self._json(code, out)
            if path == "/api/rules_backup":
                out, code = rules_backup_now(who="页面【全部备份并重载】")
                return self._json(code, out)
            if path == "/api/script_save":
                out, code = script_save(body)
                return self._json(code, out)
            if path == "/api/script_delete":
                out, code = script_delete(body.get("id"))
                return self._json(code, out)
            if path == "/api/script_order":
                out, code = script_order(body.get("id"), body.get("delta"), body.get("order"))
                return self._json(code, out)
            if path == "/api/record_start":
                out, code = record_start(str(body.get("serial") or ""), bool(body.get("clear")))
                return self._json(code, out)
            if path == "/api/record_stop":
                out, code = record_stop(body.get("name") or "", body.get("id") or "",
                                        body.get("note") or "", bool(body.get("overwrite")),
                                        bool(body.get("discard")))
                return self._json(code, out)
            if path == "/api/record_undo":
                out, code = record_undo()
                return self._json(code, out)
            if path == "/api/record_wait":
                out, code = record_wait(body.get("secs"))
                return self._json(code, out)
            if path == "/api/record_clear":
                out, code = record_clear()
                return self._json(code, out)
            return self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"ok": False, "error": "接口异常：%s" % exc})

    def _api_start(self, body):
        """POST /api/start

        老写法 {what:"coin"} 照旧（一条 flag 都不加，命令行与改造前逐字一致）；
        新写法 {whats:["coin","race"], force_panels:false, scripts:true} 与 {script:"<id>"} 单跑。
        """
        # 0 = 不限时间（透传 --minutes 0）；0~1440 照给；非法 / 缺省 / 负数 → 30
        minutes = norm_minutes(body.get("minutes"))
        # 轮数保险丝：空 / 非法 / <=0 → 120（auto.py 的默认值），绝不因为输入框出错而起不来
        max_rounds = norm_max_rounds(body.get("max_rounds"))
        serial = str(body.get("serial") or "").strip() or None
        force_panels = bool(body.get("force_panels"))
        scripts_opt = body.get("scripts")
        scripts_opt = True if scripts_opt is None else bool(scripts_opt)

        script = str(body.get("script") or "").strip().lower()
        if script:
            if not _SCRIPT_ID_RE.match(script):
                return self._json(400, {"ok": False, "error": "script 只许小写字母/数字/下划线"})
            if not os.path.exists(_script_path(script)):
                return self._json(404, {"ok": False,
                                        "error": "没有这个脚本：%s" % _rel(_script_path(script))})
            what = "scripts"
            scripts_opt = True        # 单跑就是跑脚本，别拼出 --what scripts --no-scripts 这种自相矛盾的命令
        else:
            whats = body.get("whats")
            if whats is not None:
                if isinstance(whats, str):
                    whats = whats.split(",")
                if not isinstance(whats, list):
                    return self._json(400, {"ok": False, "error": "whats 要是数组，例如 [\"coin\",\"race\"]"})
                order, seen = [], set()
                for one in whats:
                    k = str(one or "").strip().lower()
                    if not k:
                        continue
                    if k not in ("all", "coin", "race", "scan", "scripts"):
                        return self._json(400, {"ok": False,
                                                "error": "whats 里只允许 all/coin/race/scan/scripts：%r" % k})
                    if k not in seen:
                        seen.add(k)
                        order.append(k)
                if not order:
                    return self._json(400, {"ok": False, "error": "whats 是空的：至少勾一个面板"})
                if "all" in seen:
                    order = ["all"]          # all 就是 coin,race，别拼成 all,coin 让 auto.py 犯难
                what = ",".join(order)
            else:
                what = str(body.get("what") or "all").strip().lower()
                if what not in ("all", "coin", "race", "scan"):
                    return self._json(400, {"ok": False, "error": "what 只能是 all/coin/race/scan"})
        out, code = start_run(what, minutes, serial, force_panels=force_panels,
                              scripts=scripts_opt, script=script, max_rounds=max_rounds)
        if out.get("ok"):
            auto_record(out.get("serial") or current_serial(),
                        "开始跑一轮 what=%s minutes=%g（时长：%s · 轮数保险丝 %d）"
                        % (what, minutes, minutes_label(minutes), max_rounds),
                        note="面板按钮，pid=%s%s" % (out.get("pid"),
                                                   (" · " + " ".join(out.get("flags") or []))
                                                   if out.get("flags") else ""))
        return self._json(code, out)


def _port_in_use():
    """Windows 上 SO_REUSEADDR 允许两个进程同时绑同一端口（会一半请求打到旧实例），
    所以绑之前先探测一下，避免悄悄起第二份面板。"""
    return _port_open(PORT)


if os.environ.get("COIN11_DEVICE_AUTO") == "1":
    # 导入期读表时 adb 层还没就绪（见 _ADB_READY）：这里补一次，同学的手机才会出现在设备下拉里
    reload_devices()


def main():
    load_cfg()
    _keep_load()          # 2026-11-02：按上次的选择恢复保活开关（重启不丢）
    if _port_in_use():
        print("[dash] 已经有面板在 http://%s:%d/ 跑着了，本次不再启动（直接开浏览器即可）"
              % (HOST, PORT))
        return 0
    if not getattr(sys, "frozen", False) and not os.path.exists(AUTO_PY):
        print("[dash] 警告：找不到 coin11/auto.py，按钮会启动失败（只读监控仍可用）")
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print("[dash] 端口 %d 被占用（面板可能已在运行）：%s" % (PORT, exc))
        return 0
    srv.daemon_threads = True
    global _SRV
    _SRV = srv                   # 【关闭面板】要先拿到这个句柄才能优雅退出
    if os.environ.get("COIN11_NO_BROWSER") != "1":
        _opened = open_panel_window("http://%s:%d/" % (HOST, PORT))
        if _opened:
            print("[dash] 面板窗口：%s（--app 模式，关面板时会一起关掉）" % os.path.basename(_opened))
    adb_path()
    live_start()
    adopt_existing_runs(print)   # 接管 dash 启动前就在跑的 auto.py（别再起第二个）
    # 2026-11-04：启动之后新起的 auto.py 也要接管（每 ADOPT_EVERY_S 秒一次，5 秒节流）
    threading.Thread(target=_adopt_loop, name="adopt-loop", daemon=True).start()
    print("[dash] 总控台：http://%s:%d/" % (HOST, PORT))
    print("[dash] 实时推送：/api/stream（SSE，%dms 一帧，只推日志/运行状态）" % SSE_MS)
    print("[dash] 自动接管：每 %gs 查一次新起的 auto.py" % ADOPT_EVERY_S)
    print("[dash] 投屏：/live.jpg?serial=<serial>（约 2 帧/秒，480px JPEG）")
    print("[dash] 证据目录：%s"
          % (" + ".join(os.path.basename(d) for d in session_dirs()) or "(没找到 *-auto)"))
    print("[dash] 当前设备：%s  教学页：%s" % (current_serial(), teach_url(current_serial())))
    print("[dash] 设备：%s" % "，".join("%s=%s" % (l, s) for s, l in DEVICES))
    print("[dash] adb：%s" % (adb_path() or "(没找到：保活/试点击/截图会失败，其余照常)"))
    print("[dash] 暂停标志：%s（= 全部暂停）· 每台：state/pause-<san>.flag"
          % os.path.relpath(PAUSE_FLAG, ROOT))
    print("[dash] live：state/live-<san>.json 优先，state/live.json 兜底")
    print("[dash] 跳转策略：%s" % os.path.relpath(JUMP_POLICY, ROOT))
    print("[dash] 手动记录：%s" % os.path.relpath(MANUAL_LOG, ROOT))
    print("[dash] 保活默认关闭 · 铁律：永不发 keyevent 26 / 电源键")
    print("[dash] Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dash] 已退出")
    finally:
        keep_all_off("面板退出")
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
