#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coin11/custom_script.py — 自定义脚本：读写（list/load/save/delete）+ 回放执行器 run_script。

2026-11-02 用户拍板 2A/3A/4A。契约见 docs/改造计划-20261102.md 7.2，**字段名一个字都不许改**：

    脚本定义   coin11/scripts/<id>.json（一脚本一文件）
        {"id":"jd_signin","name":"京东每日签到","created":"2026-11-02T18:00:00",
         "updated":"2026-11-02T18:05:00","order":10,"enabled":true,
         "note":"先手动把京东打开到首页",
         "steps":[{"op":"wait","secs":2.0,"note":"等首页加载"},
                  {"op":"tap","x":720,"y":1500,"note":"点签到"},
                  {"op":"back","note":"返回"},
                  {"op":"swipe_down","x1":720,"y1":900,"x2":720,"y2":1900,"dur":250},
                  {"op":"home"}]}
        op 只许 tap / back / home / recents / swipe_up / swipe_down / wait；
        id 只许 [a-z0-9_]；order 小的先跑，同 order 按 id 排。
    运行状态   coin11/state/scripts_state.json
        {"v":1,"runs":{"jd_signin":{"last_ok_date":"2026-11-02","last_try":"2026-11-02T18:10:03",
                                   "last_status":"ok","last_error":"","steps_done":5}}}
        「每天只成功跑一次」= last_ok_date == 今天 的脚本在排队时跳过（手动单跑不受限）。
    证据       evidence/<日期>-scripts/<id>/<HHMMSS>/step-02_before.jpg / step-02_after.jpg
        run.json {script_id,started,ended,status,steps_done,error,
                  step_records:[{i,op,ok,ms,before,after}]}

=========================== 执行器安全底线（关不掉）===========================
3A：脚本**只在当前屏幕上操作**，App 由人先打开，本模块绝不自己启动谁的 App。
  1. 坐标必须落在设备屏内（0<=x<w、0<=y<h），否则该步**立刻失败**——不裁剪、不猜、不点；
  2. 任何一步失败/异常立刻停并记 failed，**绝不「跳过继续」**；
  3. 单脚本超时默认 180 秒（auto.py --script-timeout），超时按 failed 收尾；
  4. **绝不 force-stop / am start 别人的 App**：本模块根本不发这两条命令；
  5. 绝不点任何用户没录过的坐标——每个坐标都只来自脚本文件里的 steps；
  6. 每一步前检查屏幕唤醒（R9：只认 keyevent 26，交给 dev.wake()）。
============================================================================

依赖：标准库 only。**不 import dash**（dash.py 并行开发，只读同样的 json 文件，不 import 本模块）；
auto.py 按需 __import__("custom_script") 使用本模块，两边没有循环 import。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))

#: 三个目录都做成**模块级可变全局**（不是直接算进常量里）：离线测试可以把它们指到 %TEMP%，
#: 绝不往仓库里写测试脚本/测试状态。
SCRIPTS_DIR = os.path.join(_HERE, "scripts")
STATE_DIR = os.path.join(_HERE, "state")
EVIDENCE_ROOT = os.path.join(_ROOT, "evidence")

STATE_FILE = "scripts_state.json"
STATE_V = 1

#: 7.2 A：合法 op 白名单。多一个都不许（执行器只认这七种）。
OPS = ("tap", "back", "home", "recents", "swipe_up", "swipe_down", "wait")
#: 键值必须跟 dash.py 的 _KEY_OPS 一致（白名单：绝无电源键 / 音量键）。
KEY_OPS = {"back": "4", "home": "3", "recents": "187"}
#: 与 dash.py 的 _SWIPES 默认值一致；脚本里可用 x1/y1/x2/y2/dur 覆盖。
SWIPES = {"swipe_down": (720, 900, 720, 1900, 250),
          "swipe_up": (720, 1900, 720, 900, 250)}

DEFAULT_TIMEOUT_S = 180.0
MAX_WAIT_S = 60.0
#: 与 dash.py 逐字一致：id 只许 [a-z0-9_]{1,48}
ID_RE = re.compile(r"^[a-z0-9_]{1,48}$")


class ScriptError(RuntimeError):
    """脚本定义非法 / 某一步失败（执行器见到它就立刻停）。"""


# --------------------------------------------------------------------------- #
# 路径 / 日期
# --------------------------------------------------------------------------- #
def _ensure(d):
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def scripts_dir():
    return _ensure(SCRIPTS_DIR)


def state_path():
    return os.path.join(_ensure(STATE_DIR), STATE_FILE)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def _now():
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 脚本读写
# --------------------------------------------------------------------------- #
def _as_int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _clean_steps(steps):
    """把 steps 归一化/校验成契约里的样子。非法就抛 ScriptError（保存和回放都走这里）。"""
    if not isinstance(steps, list):
        raise ScriptError("steps 必须是数组")
    out = []
    for i, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise ScriptError("第 %d 步不是对象：%r" % (i, raw))
        op = str(raw.get("op") or "").strip()
        if op not in OPS:
            raise ScriptError("第 %d 步 op 非法：%r（只许 %s）" % (i, op, "/".join(OPS)))
        step = {"op": op}
        if op == "tap":
            x, y = _as_int(raw.get("x")), _as_int(raw.get("y"))
            if x is None or y is None:
                raise ScriptError("第 %d 步 tap 缺 x/y" % i)
            step["x"], step["y"] = x, y
        elif op in SWIPES:
            dx1, dy1, dx2, dy2, ddur = SWIPES[op]
            for k, dv in (("x1", dx1), ("y1", dy1), ("x2", dx2), ("y2", dy2), ("dur", ddur)):
                step[k] = _as_int(raw.get(k), dv)
        elif op == "wait":
            secs = raw.get("secs")
            try:
                secs = float(secs)
            except (TypeError, ValueError):
                raise ScriptError("第 %d 步 wait 缺 secs 或不是数字" % i)
            # 与 dash.py 一致：< 0.1 视为非法；> MAX_WAIT_S(60) 截断到 60
            if secs < 0.1:
                raise ScriptError("第 %d 步 wait 的 secs 必须 >= 0.1（收到 %r）" % (i, secs))
            step["secs"] = min(secs, MAX_WAIT_S)
        if raw.get("note"):
            step["note"] = str(raw["note"])
        out.append(step)
    return out


def _norm_script(raw, fallback_id=""):
    """把一个 dict 归一化成脚本定义（读文件 / dash 存盘都过这里）。"""
    sid = str(raw.get("id") or fallback_id or "").strip()
    if not ID_RE.match(sid):
        raise ScriptError("id 非法：%r（只许 [a-z0-9_]）" % sid)
    return {"id": sid,
            "name": str(raw.get("name") or sid),
            "created": str(raw.get("created") or raw.get("updated") or _now()),
            "updated": str(raw.get("updated") or _now()),
            "order": _as_int(raw.get("order"), 0) or 0,
            "enabled": bool(raw.get("enabled", True)),
            "note": str(raw.get("note") or ""),
            "steps": _clean_steps(raw.get("steps") or [])}


def script_path(sid):
    return os.path.join(SCRIPTS_DIR, "%s.json" % sid)


def load_script(sid):
    """读一个脚本；不存在或坏文件返回 None（绝不抛给调用方炸掉整个队列）。"""
    p = script_path(str(sid))
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return _norm_script(raw, fallback_id=str(sid))
    except ScriptError:
        return None


def list_scripts():
    """所有脚本，按 (order, id) 排序 —— 这就是排队要的执行顺序。"""
    if not os.path.isdir(SCRIPTS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(SCRIPTS_DIR)):
        if not name.lower().endswith(".json"):
            continue
        sc = load_script(name[:-5])
        if sc:
            out.append(sc)
    out.sort(key=lambda s: (s.get("order") or 0, s.get("id") or ""))
    return out


def save_script(script):
    """写一个脚本（dash 录制保存走它）。返回落盘路径；非法就抛 ScriptError。"""
    sc = _norm_script(script)
    sc["updated"] = _now()
    if not sc.get("created"):
        sc["created"] = sc["updated"]
    p = script_path(sc["id"])
    _ensure(SCRIPTS_DIR)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(sc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def delete_script(sid):
    try:
        os.remove(script_path(str(sid)))
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 运行状态（scripts_state.json）
# --------------------------------------------------------------------------- #
def load_state():
    try:
        with open(state_path(), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        d = None
    if not isinstance(d, dict):
        d = {}
    runs = d.get("runs")
    return {"v": STATE_V, "runs": dict(runs) if isinstance(runs, dict) else {}}


def save_state(state):
    try:
        p = state_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            json.dump({"v": STATE_V, "runs": (state or {}).get("runs") or {}},
                      f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def record_run(script_id, status, steps_done, error=""):
    """跑完一次就记一次（成功才更新 last_ok_date）。"""
    st = load_state()
    rec = dict(st["runs"].get(str(script_id)) or {})
    now = datetime.now()
    rec["last_try"] = now.isoformat(timespec="seconds")
    rec["last_status"] = str(status)
    rec["last_error"] = str(error or "")[:400]
    rec["steps_done"] = int(steps_done or 0)
    if status == "ok":
        rec["last_ok_date"] = now.strftime("%Y-%m-%d")
    st["runs"][str(script_id)] = rec
    save_state(st)
    return rec


def queue_skip_reason(script, state=None, day=None):
    """排队时该不该跳过：返回 '' 表示要跑，否则返回给人看的跳过原因。

    4A 的两条（手动单跑用 run_script 直接跑，不走这里）：
      - enabled 为假 -> 跳过；
      - last_ok_date == 今天 -> 跳过（每天只成功跑一次）。
    """
    if not script.get("enabled", True):
        return "已关掉【是否要做】"
    runs = (state if state is not None else load_state()).get("runs") or {}
    ok_date = (runs.get(str(script.get("id"))) or {}).get("last_ok_date") or ""
    if ok_date and ok_date == (day or today()):
        return "今天（%s）已经成功跑过一次" % ok_date
    return ""


# --------------------------------------------------------------------------- #
# 回放执行器
# --------------------------------------------------------------------------- #
def evidence_dir(script_id, outdir=None, stamp=None):
    """evidence/<日期>-scripts/<id>/<HHMMSS>/ —— 同一天同一秒跑两次就加 -2/-3 后缀。"""
    base = outdir or os.path.join(EVIDENCE_ROOT, "%s-scripts" % today())
    sid = str(script_id)
    stamp = stamp or datetime.now().strftime("%H%M%S")
    d = os.path.join(base, sid, stamp)
    n = 1
    while os.path.isdir(d):
        n += 1
        d = os.path.join(base, sid, "%s-%d" % (stamp, n))
    _ensure(d)
    return d


def check_point(x, y, w, h, label=""):
    """坐标必须落在屏内，否则该步立刻失败（不裁剪、不猜、不点）。"""
    x, y = _as_int(x), _as_int(y)
    if x is None or y is None:
        raise ScriptError("%s 坐标不是数字：x=%r y=%r" % (label, x, y))
    if not (0 <= x < int(w) and 0 <= y < int(h)):
        raise ScriptError("%s 坐标越界：(%d,%d) 不在 %dx%d 屏内" % (label, x, y, int(w), int(h)))
    return x, y


def _wake_ok(dev, log):
    """每步前查一次屏幕唤醒（R9：只认 keyevent 26，逻辑在 Device.wake 里）。"""
    awake = getattr(dev, "is_awake", None)
    wake = getattr(dev, "wake", None)
    if not callable(wake):
        return
    if callable(awake) and awake():
        return
    if not wake():
        raise ScriptError("屏幕唤不醒（keyevent 26 也不行）")
    log("[script] 屏幕原来是灭的 -> 已唤醒")


def _shot(dev, sdir, i, tag):
    """一步两张图（契约 D）；截不出来就抛——证据不全会让排错没有依据。"""
    p = os.path.join(sdir, "step-%02d_%s.jpg" % (i, tag))
    dev.shot(p)
    return p


def _key(dev, keycode, op):
    """按键：back 优先用 dev.back()（离线假设备只有它），其余直接 keyevent。"""
    if op == "back":
        fn = getattr(dev, "back", None)
        if callable(fn):
            fn()
            return
    dev.shell("input keyevent %s" % keycode)


def _do_op(dev, step, w, h):
    """执行一步。本函数里**没有** am start / force-stop —— 这是 3A 的底线，别加。"""
    op = step["op"]
    if op == "tap":
        x, y = check_point(step["x"], step["y"], w, h, "tap")
        dev.tap(x, y)
    elif op in KEY_OPS:
        _key(dev, KEY_OPS[op], op)
    elif op in SWIPES:
        pts = [(step["x1"], step["y1"]), (step["x2"], step["y2"])]
        for (x, y) in pts:
            check_point(x, y, w, h, op)
        dev.swipe(int(step["x1"]), int(step["y1"]), int(step["x2"]),
                  int(step["y2"]), int(step["dur"]))
    elif op == "wait":
        time.sleep(float(step["secs"]))
    else:
        raise ScriptError("不认识的 op：%r" % op)


def run_script(dev, script, outdir, log=print, timeout=None):
    """回放一个脚本；返回 run.json 的内容（同时已落盘）。

    status: "ok" = 全部步都成功；"failed" = 中途失败/超时（**立刻停**，后面的步一步都不跑）。
    steps_done = 真正成功完成的步数；step_records 里也含失败那一步（ok=False）。
    """
    bad = ""
    try:
        steps = _clean_steps((script or {}).get("steps") or []) if script else []
    except ScriptError as e:
        steps, bad = [], "脚本定义非法：%s" % e          # 照样落盘取证，别把坏脚本静默吞掉
    sid = str((script or {}).get("id") or "script")
    name = str((script or {}).get("name") or sid)
    try:
        timeout = float(timeout or (script or {}).get("timeout") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    log = log or (lambda *a: None)
    w = int(getattr(dev, "w", 0) or 1440)
    h = int(getattr(dev, "h", 0) or 3200)
    sdir = evidence_dir(sid, outdir)
    res = {"script_id": sid, "started": _now(), "ended": "", "status": "failed",
           "steps_done": 0, "error": "", "step_records": [],
           "name": name, "dir": sdir, "timeout_s": timeout}
    log("[script] %s（%s）开始：%d 步 / 超时 %.0fs / 屏幕 %dx%d / 证据 %s"
        % (name, sid, len(steps), timeout, w, h, sdir))
    t_begin = time.time()
    deadline = t_begin + timeout
    if not steps:
        res["error"] = "脚本没有步骤（steps=[]）"
    else:
        for i, step in enumerate(steps, 1):
            rec = {"i": i, "op": step["op"], "ok": False, "ms": 0,
                   "before": "", "after": "", "error": ""}
            t0 = time.time()
            try:
                if time.time() >= deadline:
                    raise ScriptError("超时（%.0fs）在第 %d 步前" % (timeout, i))
                _wake_ok(dev, log)
                # 坐标先校验再截图：越界就立刻停，绝不把「点不到」当成「点到了」
                if step["op"] == "tap":
                    check_point(step["x"], step["y"], w, h, "tap")
                elif step["op"] in SWIPES:
                    check_point(step["x1"], step["y1"], w, h, step["op"])
                    check_point(step["x2"], step["y2"], w, h, step["op"])
                rec["before"] = _shot(dev, sdir, i, "before")
                _do_op(dev, step, w, h)
                rec["after"] = _shot(dev, sdir, i, "after")
                rec["ok"] = True
            except Exception as e:
                rec["error"] = "%s: %s" % (type(e).__name__, e)
                rec["ms"] = int((time.time() - t0) * 1000)
                res["error"] = "第 %d 步（%s）失败 -> 立刻停，后面的步一步都没跑：%s" % (
                    i, step["op"], e)
                res["step_records"].append(rec)
                log("[script] %s 第 %d/%d 步 %s 失败：%s"
                    % (sid, i, len(steps), step["op"], rec["error"]))
                break
            rec["ms"] = int((time.time() - t0) * 1000)
            res["step_records"].append(rec)
            log("[script] %s 第 %d/%d 步 %s ok（%dms）"
                % (sid, i, len(steps), step["op"], rec["ms"]))
    res["steps_done"] = sum(1 for r in res["step_records"] if r["ok"])
    if bad:
        res["error"] = bad
    if steps and not res["error"] and res["steps_done"] == len(steps):
        res["status"] = "ok"
    else:
        res["status"] = "failed"
        if not res["error"]:
            res["error"] = "没有跑完（%d/%d 步）" % (res["steps_done"], len(steps))
    res["ended"] = _now()
    try:
        with open(os.path.join(sdir, "run.json"), "w", encoding="utf8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("[script] run.json 写失败：%s" % e)
    record_run(sid, res["status"], res["steps_done"], res["error"])
    log("[script] %s 收工：%s（%d/%d 步，%.1fs）%s"
        % (sid, res["status"], res["steps_done"], len(steps), time.time() - t_begin,
           "" if res["status"] == "ok" else " / " + res["error"][:120]))
    return res
