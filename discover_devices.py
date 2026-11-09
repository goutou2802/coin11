"""coin11/discover_devices.py — 自动找两台手机并写 state/devices.json。

为什么需要它：手机走无线调试，地址会随网络变（家里 wifi / 校园网 / 手机热点，
IP 会变、端口也会变：43059 变 5555 就很常见）。与其每次手改 dash.py，不如扫一遍网段。

用法：
    python coin11/discover_devices.py            # 扫描 + 写配置 + 打印结果
    python coin11/discover_devices.py --dry-run  # 只打印，不写文件

判定身份：按机型
    V2505A / 1440x3168 -> 手机1（端口 8777）
    V2243A / 1440x3200 -> 手机2（端口 8778）
其它机型：按发现顺序补成 手机N，并标注 model。
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adbutils import adb  # noqa: E402

# 同上：GBK 控制台下打不出特殊符号时降级成 '?'，不让编码错误打死脚本
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
OUT = os.path.join(STATE_DIR, "devices.json")
#: 单设备时代的遗留文件：dash.py 的 current_serial() 和 run.bat 都在读它。
#: 2026-11-03 修：以前这里只写 devices.json，没人刷新 .phone_serial —— 换网络后它一直躺在
#: 旧地址上（实测 192.168.1.8:5555），面板会多出一台幽灵设备，run.bat 还会拿死地址跑 auto.py。
SERIAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".phone_serial")

#: 机型 -> 标签（来自 auto.py 的 DEV_COORDS：写死坐标是按这两台机器测的）
MODEL_LABEL = {"V2505A": "手机1", "V2243A": "手机2"}
#: 只扫真正会用的两个端口（5037 是 adb server 自己，扫它纯浪费）
PORTS = (5555, 43059)


def local_ipv4s():
    """本机所有 IPv4 地址（含虚拟网卡，多扫几个网段无所谓）。"""
    try:
        r = subprocess.run(["ipconfig"], capture_output=True, text=True,
                           encoding="gbk", errors="replace")
        txt = r.stdout or ""
    except Exception:
        return []
    ips = re.findall(r"IPv4[^\d]*(\d+\.\d+\.\d+\.\d+)", txt)
    return sorted(set(ip for ip in ips if not ip.startswith("127.")))


async def _probe(host, port, timeout=0.9):
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        w.close()
        return host, port
    except Exception:
        return None


#: 并发上限：之前一次开 ~2000 个 socket，Windows 连接面直接糊掉，漏掉了在场的手机
#: （2026-11-03 真机连漏两次）。限制并发 + 只探 ARP 里活着的主机，才稳。
_SCAN_SEM = 60


async def scan(ips, ports=PORTS):
    sem = asyncio.Semaphore(_SCAN_SEM)

    async def one(host, port):
        async with sem:
            return await _probe(host, port, timeout=1.2)

    tasks = []
    for ip in ips:
        base = ip.rsplit(".", 1)[0]
        hosts = [h for h in arp_hosts() if h.startswith(base + ".")] or \
                ["%s.%d" % (base, i) for i in range(1, 255)]
        for host in hosts:
            for port in ports:
                tasks.append(asyncio.create_task(one(host, port)))
    done = await asyncio.gather(*tasks)
    return [x for x in done if x]


def arp_hosts():
    """ARP 表里已经在场的主机（比盲扫整段靠谱：在用的设备一般都有 ARP 记录）。"""
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                           encoding="gbk", errors="replace")
        return set(re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+[0-9a-fA-F-]{11,}", r.stdout or ""))
    except Exception:  # noqa: BLE001
        return set()


def read_device(serial):
    try:
        d = adb.device(serial)
        model = (d.shell("getprop ro.product.model") or "").strip()
        size = (d.shell("wm size") or "").strip().replace("\n", " ")
        return {"serial": serial, "model": model or "?", "size": size or "?"}
    except Exception as e:  # noqa: BLE001
        return {"serial": serial, "model": "?", "size": "?", "error": str(e)[:60]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写 state/devices.json")
    ap.add_argument("--timeout", type=float, default=0.4)
    args = ap.parse_args(argv)

    ips = local_ipv4s()
    if not ips:
        print("[discover] 读不到本机 IPv4（ipconfig 失败）")
        return 2
    print("[discover] 本机网段：%s" % ", ".join(i.rsplit(".", 1)[0] + ".0/24" for i in ips))
    # 先试已知地址（上次 devices.json 里的 + 已经连着的）：换网后 IP 常常没变，
    # 这一步几乎瞬时命中，避免全量扫网段（并发太多会漏，2026-11-03 真机漏过手机2）。
    hits, seen0 = [], set()
    known = []
    hist = {}
    try:
        with open(OUT, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        known = [str(d.get("serial") or "") for d in (cfg.get("devices") or [])]
        hist = {k: list(v) for k, v in (cfg.get("history") or {}).items() if isinstance(v, list)}
    except Exception:  # noqa: BLE001
        pass
    # 历史地址也先试一遍：换了网段后老 IP 常常还在（只是变了网段），命中就省一次全网扫描
    for lab, sers in hist.items():
        for ser in sers:
            if ser and ser not in known:
                known.append(ser)
    for d in adb.list():
        known.append(str(getattr(d, "serial", d)))
    # USB 有线设备（serial 里没有冒号，例如 ABCD1234）：直连、零网络占用，优先用
    for d in adb.list():
        ser = str(getattr(d, "serial", d))
        if ser and ":" not in ser:
            info = read_device(ser)
            print("[discover] USB 有线设备：%s（model=%s）" % (ser, info["model"]))
            known.append(ser)

    for serial in [k for k in known if k and ":" in k]:
        host, _, port = serial.rpartition(":")
        if not port.isdigit():
            continue
        got = asyncio.run(_probe(host, int(port)))
        if got and got not in seen0:
            seen0.add(got)
            hits.append(got)
            print("[discover] 已知地址直接命中：%s:%s" % got)
    more = [h for h in asyncio.run(scan(ips)) if h not in seen0]
    hits.extend(more)
    if not hits:
        print("[discover] 一个开放 adb 端口的设备都没扫到。")
        print("           检查：手机「开发者选项 → 无线调试」是否开着；手机和电脑是否在同一网络。")
        return 1
    print("[discover] 扫到：%s" % ", ".join("%s:%s" % h for h in hits))

    found, seen = [], set()
    # USB 设备（serial 无冒号）不需要扫网段：直接读
    for d in adb.list():
        ser = str(getattr(d, "serial", d))
        if ser and ":" not in ser and ser not in seen:
            seen.add(ser)
            found.append(read_device(ser))
    for host, port in hits:
        serial = "%s:%d" % (host, port)
        if serial in seen or port == 5037:      # 5037 是 adb server 自己，跳过
            continue
        seen.add(serial)
        try:
            adb.connect(serial, timeout=4)
        except Exception:
            pass
        info = read_device(serial)
        found.append(info)

    # 认身份：先按机型，机型不认识再按分辨率，最后按顺序补
    devices = []
    for info in found:
        label = MODEL_LABEL.get(info["model"])
        if label is None:
            label = "1440x3168" in info["size"] and "手机1" or \
                    ("1440x3200" in info["size"] and "手机2" or "")
        if label:
            devices.append({"serial": info["serial"], "label": label,
                            "model": info["model"], "size": info["size"]})
    known = {d["label"] for d in devices}
    n = 1
    for info in found:
        if any(d["serial"] == info["serial"] for d in devices):
            continue
        while ("手机%d" % n) in known:
            n += 1
        devices.append({"serial": info["serial"], "label": "手机%d" % n,
                        "model": info["model"], "size": info["size"]})
        known.add("手机%d" % n)

    # 同一台手机可能同时被 USB 和无线各枚举一次：按标签去重，**优先留 USB**（serial 无冒号）
    by_label = {}
    for d in devices:
        cur = by_label.get(d["label"])
        if cur is None:
            by_label[d["label"]] = d
            continue
        cur_is_usb = ":" not in cur["serial"]
        new_is_usb = ":" not in d["serial"]
        if new_is_usb and not cur_is_usb:
            print("[discover] %s 同时有 USB(%s) 和无线(%s) -> 保留 USB"
                  % (d["label"], d["serial"], cur["serial"]))
            by_label[d["label"]] = d
        else:
            print("[discover] %s 同时有 %s 和 %s -> 保留 %s"
                  % (d["label"], cur["serial"], d["serial"], cur["serial"]))
    devices = list(by_label.values())
    devices.sort(key=lambda d: d["label"])
    print("\n[discover] 识别结果：")
    for d in devices:
        print("   %-6s %-22s model=%-8s %s" % (d["label"], d["serial"], d["model"], d["size"]))
    if args.dry_run:
        print("\n[discover] --dry-run：不写文件")
        return 0
    os.makedirs(STATE_DIR, exist_ok=True)
    # 记历史地址（每台最近 5 个）：下次换网直接命中，不用扫网段
    for d in devices:
        lst = hist.setdefault(d["label"], [])
        if d["serial"] in lst:
            lst.remove(d["serial"])
        lst.insert(0, d["serial"])
        del lst[5:]
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"devices": devices, "history": hist,
                   "at": __import__("datetime").datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False, indent=1)
    # 同步刷新「主设备」文件（dash.py current_serial() 与 run.bat 都读它，见文件头注释）
    if devices:
        try:
            with open(SERIAL_FILE, "w", encoding="utf-8") as f:
                f.write(devices[0]["serial"])
            print("[discover] 已同步 %s = %s"
                  % (os.path.basename(SERIAL_FILE), devices[0]["serial"]))
        except OSError as e:
            print("[discover] 写 %s 失败（不影响 devices.json）：%s" % (SERIAL_FILE, e))
    print("\n[discover] 已写 %s —— 重启 dash（8899）后生效" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
