#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coin11/teach.py —— 点位置教学工具（看截图 → 点位置 → 记成可复用规则）

用法:
    python coin11/teach.py --serial 192.168.1.8:5555 --port 8777

打开 http://127.0.0.1:8777 ，在手机截图上点任意位置：
页面把点击换算成设备真实像素坐标，填个标签点【保存为规则】，
就追加一行到 coin11/targets/targets.jsonl，并把这张截图存到
coin11/targets/shots/<ts>.png ，供自动化脚本复用。

安全边界:
  * 只监听 127.0.0.1；
  * 唯一会操作手机的命令是 “input tap x y”，且必须用户在页面里
    亲手点【试点击】并二次确认后才会发出；
  * 没有 swipe / keyevent / am start，没有任何批量或循环点击。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import adbutils
from PIL import Image

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SERIAL_FILE = os.path.join(_ROOT, ".phone_serial")
_ALLOWED_HOSTS = ("127.0.0.1", "localhost")


# --------------------------------------------------------------------------- adb

def _adb_binary() -> str:
    """优先用 adbutils 自带的 adb，其次退回 PATH 里的 adb。"""
    try:
        path = adbutils.adb_path()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    return "adb"


ADB = _adb_binary()


def _run(argv, timeout):
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)


def grab_png(serial: str, timeout: int = 60) -> bytes:
    """只读：抓一张当前屏幕（adb exec-out screencap -p）。"""
    proc = _run([ADB, "-s", serial, "exec-out", "screencap", "-p"], timeout)
    data = proc.stdout or b""
    if proc.returncode != 0 or not data.startswith(b"\x89PNG"):
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise RuntimeError("adb 抓图失败 (rc=%s) %s" % (proc.returncode, err or "(无 stderr)"))
    return data


def tap_point(serial: str, x: int, y: int, timeout: int = 30):
    """唯一会操作手机的动作：adb shell input tap x y。"""
    argv = [ADB, "-s", serial, "shell", "input", "tap", str(int(x)), str(int(y))]
    proc = _run(argv, timeout)
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
    return argv, proc.returncode, err


def png_size(png: bytes):
    with Image.open(io.BytesIO(png)) as im:
        im.load()
        return im.size


def now_ts() -> str:
    ms = int(time.time() * 1000) % 1000
    return time.strftime("%Y%m%d_%H%M%S") + "_%03d" % ms


def default_serial() -> str:
    try:
        with open(_SERIAL_FILE, encoding="utf-8") as fh:
            serial = fh.read().strip()
            if serial:
                return serial
    except OSError:
        pass
    try:
        for dev in adbutils.AdbClient().list():
            serial = getattr(dev, "serial", "") or ""
            if serial and getattr(dev, "state", "device") == "device":
                return serial
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------- 教学会话

class TeachSession:
    """保存“当前这一屏截图”，并把用户点出来的坐标写进规则库。"""

    def __init__(self, serial: str, data_dir: str):
        self.serial = serial
        self.data_dir = data_dir
        self.shots_dir = os.path.join(data_dir, "shots")
        self.targets_path = os.path.join(data_dir, "targets.jsonl")
        self.pending_dir = os.path.join(data_dir, "pending")
        self.lock = threading.Lock()
        self.png = b""
        self.ts = ""
        self.image_hash = ""
        self.screen_w = 0
        self.screen_h = 0
        self.shot_written = False
        self.error = ""
        self.version = 0
        os.makedirs(self.shots_dir, exist_ok=True)

    def capture(self, from_file: str = "") -> bool:
        with self.lock:
            try:
                png = open(from_file, "rb").read() if from_file else grab_png(self.serial)
                w, h = png_size(png)
            except Exception as exc:
                self.error = "%s: %s" % (type(exc).__name__, exc)
                return False
            self.png = png
            self.ts = now_ts()
            self.image_hash = hashlib.sha256(png).hexdigest()
            self.screen_w, self.screen_h = w, h
            self.shot_written = False
            self.error = ""
            self.version += 1
            return True

    def state(self) -> dict:
        return {
            "ok": not self.error,
            "error": self.error,
            "serial": self.serial,
            "screen_w": self.screen_w,
            "screen_h": self.screen_h,
            "has_shot": bool(self.png),
            "shot_ts": self.ts,
            "image_hash": self.image_hash,
            "v": self.version,
            "shots_dir": self.shots_dir,
            "targets_path": self.targets_path,
        }

    def targets(self, limit: int = 1000) -> list:
        rows = []
        try:
            with open(self.targets_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return rows[-limit:]

    # ---- 待教学：auto.py 遇到不会的界面时落盘的 pending/*.json + *.png ----

    def pending_items(self, limit: int = 200) -> list:
        """只回 state=pending 的（教过的 answered 不再出现在待教学里），最新在最前。"""
        out = []
        if not os.path.isdir(self.pending_dir):
            return out
        for name in sorted(os.listdir(self.pending_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.pending_dir, name), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("state") != "pending":
                continue
            ts = str(rec.get("ts") or name[:-5])
            rec["ts"] = ts
            rec.setdefault("question", "")
            rec.setdefault("label_hint", "")
            rec.setdefault("serial", "")
            rec["has_shot"] = bool(rec.get("screenshot")) and os.path.exists(
                os.path.join(self.pending_dir, "%s.png" % ts))
            rec["shot_url"] = "/api/pending/shot?ts=%s" % quote(ts)
            out.append(rec)
        out.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
        return out[:limit]

    def _pending_files(self, ts: str):
        ts = str(ts or "")
        if not re.fullmatch(r"[0-9A-Za-z_.-]{1,64}", ts):
            raise ValueError("pending ts 不合法: %r" % ts)
        return (os.path.join(self.pending_dir, ts + ".json"),
                os.path.join(self.pending_dir, ts + ".png"))

    def pending_png(self, ts: str) -> bytes:
        _, png = self._pending_files(ts)
        if not os.path.exists(png):
            raise ValueError("这条待教学没有截图: %s" % os.path.basename(png))
        with open(png, "rb") as fh:
            return fh.read()

    def save_pending(self, ts, label, x, y, note="") -> dict:
        """在**历史截图**上教一次：写一行规则进 targets.jsonl，并把 pending 标成 answered。"""
        jpath, png_path = self._pending_files(ts)
        if not os.path.exists(jpath):
            raise ValueError("找不到这条待教学: %s" % ts)
        with open(jpath, encoding="utf-8") as fh:
            rec = json.load(fh)
        if rec.get("state") != "pending":
            raise ValueError("这条已经教过了（state=%s）" % rec.get("state"))
        if not os.path.exists(png_path):
            raise ValueError("这条待教学的截图丢了: %s" % os.path.basename(png_path))
        with open(png_path, "rb") as fh:
            png = fh.read()
        w, h = png_size(png)
        x, y = int(x), int(y)
        if not (0 <= x < w and 0 <= y < h):
            raise ValueError("坐标超出截图范围: (%d, %d)，截图 %dx%d" % (x, y, w, h))
        question = str(rec.get("question") or "").strip()
        q_tag = question[:40]
        note = (note or "").strip()
        note = ("%s｜%s" % (note, q_tag)) if (note and q_tag) else (note or q_tag)
        label = ((label or "").strip() or str(rec.get("label_hint") or "").strip()
                 or q_tag or ("pending_%s" % ts))
        with self.lock:
            shot_name = "%s.png" % ts
            with open(os.path.join(self.shots_dir, shot_name), "wb") as fh:
                fh.write(png)
            row = {
                "label": label,
                "serial": rec.get("serial") or self.serial,
                "x": x,
                "y": y,
                "screen_w": w,
                "screen_h": h,
                "ts": ts,
                "note": note,
                "screenshot_name": shot_name,
                "image_hash": hashlib.sha256(png).hexdigest(),
                "from_pending": ts,
            }
            with open(self.targets_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            rec.update(state="answered",
                       answered_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       answer={"x": x, "y": y, "label": label, "note": note},
                       rule=row)
            tmp = jpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, jpath)
        return {"row": row, "pending": rec}

    def save(self, label, x, y, note="") -> dict:
        label = (label or "").strip()
        note = (note or "").strip()
        # 标签留空时自动生成：优先用备注当作标签，用户只写说明也能保存。
        if not label:
            label = note if note else ("target_%s" % self.ts)
        if not self.png:
            raise ValueError("还没有截图，先点【刷新截图】")
        x, y = int(x), int(y)
        if not (0 <= x < self.screen_w and 0 <= y < self.screen_h):
            raise ValueError("坐标超出屏幕范围: (%d, %d)，屏幕 %dx%d"
                             % (x, y, self.screen_w, self.screen_h))
        with self.lock:
            shot_name = "%s.png" % self.ts
            shot_path = os.path.join(self.shots_dir, shot_name)
            if not (self.shot_written and os.path.exists(shot_path)):
                with open(shot_path, "wb") as fh:
                    fh.write(self.png)
                self.shot_written = True
            row = {
                "label": label,
                "serial": self.serial,
                "x": x,
                "y": y,
                "screen_w": self.screen_w,
                "screen_h": self.screen_h,
                "ts": self.ts,
                "note": note,
                "screenshot_name": shot_name,
                "image_hash": self.image_hash,
            }
            with open(self.targets_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def tap(self, x, y, confirm) -> dict:
        """唯一会操作手机的地方：只发 input tap，且必须二次确认。"""
        if confirm is not True:
            raise PermissionError("试点击需要二次确认（confirm=true）")
        if not self.png:
            raise ValueError("还没有截图")
        x, y = int(x), int(y)
        if not (0 <= x < self.screen_w and 0 <= y < self.screen_h):
            raise ValueError("坐标超出屏幕范围: (%d, %d)，屏幕 %dx%d"
                             % (x, y, self.screen_w, self.screen_h))
        argv, rc, err = tap_point(self.serial, x, y)
        time.sleep(0.8)
        self.capture()
        return {"tapped": [x, y], "argv": argv, "rc": rc, "stderr": err,
                "state": self.state()}


_APP = None  # type: TeachSession


# ------------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "teach/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _err(self, code, message):
        self._json({"ok": False, "error": message}, code)

    def _local_request(self) -> bool:
        """只接受本机页面发来的请求，挡住 DNS rebinding / 跨站提交。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if host not in _ALLOWED_HOSTS:
            self._err(403, "只接受 127.0.0.1 / localhost 访问")
            return False
        origin = self.headers.get("Origin")
        if origin and (urlparse(origin).hostname or "").lower() not in _ALLOWED_HOSTS:
            self._err(403, "拒绝跨站来源: %s" % origin)
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/shot.png":
            if _APP.png:
                self._send(200, _APP.png, "image/png")
            else:
                self._err(404, "还没有截图")
        elif path == "/api/state":
            if self._local_request():
                self._json(_APP.state())
        elif path == "/api/targets":
            if self._local_request():
                rows = _APP.targets()
                self._json({"ok": True, "targets": rows, "count": len(rows),
                            "targets_path": _APP.targets_path})
        elif path == "/api/pending":
            if self._local_request():
                items = _APP.pending_items()
                self._json({"ok": True, "count": len(items), "items": items,
                            "pending_dir": _APP.pending_dir})
        elif path == "/api/pending/shot":
            qs = parse_qs(urlparse(self.path).query)
            ts = (qs.get("ts") or [""])[0]
            try:
                png = _APP.pending_png(ts)
            except ValueError as exc:
                self._err(404, str(exc))
            else:
                self._send(200, png, "image/png")
        elif path == "/favicon.ico":
            self._send(204)
        else:
            self._err(404, "没有这个地址: %s" % path)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._local_request():
            return
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" not in ctype:
            self._err(415, "只接受 application/json")
            return
        try:
            data = self._body()
        except Exception as exc:
            self._err(400, "请求体不是合法 JSON: %s" % exc)
            return

        if path == "/api/capture":
            ok = _APP.capture()
            state = _APP.state()
            state["ok"] = ok
            self._json(state, 200 if ok else 500)
        elif path == "/api/save":
            try:
                row = _APP.save(data.get("label"), data.get("x"), data.get("y"),
                                data.get("note", ""))
            except (ValueError, TypeError) as exc:
                self._err(400, str(exc))
                return
            self._json({"ok": True, "row": row, "state": _APP.state()})
        elif path == "/api/pending/save":
            try:
                res = _APP.save_pending(data.get("ts"), data.get("label"),
                                        data.get("x"), data.get("y"),
                                        data.get("note", ""))
            except (ValueError, TypeError) as exc:
                self._err(400, str(exc))
                return
            self._json({"ok": True, "row": res["row"],
                        "pending": res["pending"],
                        "pending_count": len(_APP.pending_items()),
                        "state": _APP.state()})
        elif path == "/api/tap":
            try:
                result = _APP.tap(data.get("x"), data.get("y"), data.get("confirm"))
            except PermissionError as exc:
                self._err(403, str(exc))
                return
            except (ValueError, TypeError) as exc:
                self._err(400, str(exc))
                return
            result["ok"] = True
            self._json(result)
        else:
            self._err(404, "没有这个接口: %s" % path)


# ------------------------------------------------------------------------ 页面

PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>点位置教学 · coin11/teach.py</title>
<style>
  :root { --bg:#11141a; --panel:#181d26; --line:#2a3240; --fg:#e6edf3; --dim:#8b98a9;
          --acc:#3b82f6; --ok:#22c55e; --danger:#ef4444; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 "Segoe UI",system-ui,-apple-system,"Microsoft YaHei",sans-serif; }
  header { padding:10px 16px; border-bottom:1px solid var(--line); background:var(--panel);
           display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; position:sticky; top:0; z-index:9; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header .meta { color:var(--dim); font-size:12px; }
  code { background:#0b0e13; padding:1px 5px; border-radius:4px; font-size:12px; }
  main { display:flex; gap:14px; padding:14px; align-items:flex-start; }
  .left { flex:1 1 auto; min-width:0; }
  .right { flex:0 0 380px; display:flex; flex-direction:column; gap:12px; position:sticky; top:60px; }
  @media (max-width:1100px){ main{flex-direction:column;} .right{position:static;width:100%;flex:1 1 auto;} }
  .toolbar { display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap; }
  #zoom-label { color:var(--dim); font-size:12px; }
  #frame { background:#0b0e13; border:1px solid var(--line); border-radius:8px; padding:8px;
           overflow:auto; max-height:calc(100vh - 130px); }
  #stage { position:relative; display:inline-block; line-height:0; }
  #shot { display:block; height:auto; cursor:crosshair; user-select:none; background:#000; }
  #mark { position:absolute; width:0; height:0; pointer-events:none; }
  #mark::before, #mark::after { content:""; position:absolute; background:#ff2d55; box-shadow:0 0 3px #000; }
  #mark::before { left:-13px; top:-1px; width:26px; height:2px; }
  #mark::after  { top:-13px; left:-1px; height:26px; width:2px; }
  #mark span { position:absolute; left:12px; top:-30px; background:#ff2d55; color:#fff; font-size:11px;
               line-height:1.4; padding:2px 6px; border-radius:4px; white-space:nowrap; }
  .stage-msg { color:var(--dim); padding:40px 10px; font-size:13px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
  .card > label, .card > .lbl-sm { display:block; color:var(--dim); font-size:12px; margin:10px 0 4px; }
  .card > label:first-child { margin-top:0; }
  input[type=text] { width:100%; padding:8px 10px; border-radius:6px; border:1px solid var(--line);
                     background:#0b0e13; color:var(--fg); font:13px/1.4 inherit; }
  input[type=text]:focus { outline:1px solid var(--acc); border-color:var(--acc); }
  .coords { margin:12px 0; padding:10px; background:#0b0e13; border-radius:6px; border:1px solid var(--line); }
  .coords b { color:#7dd3fc; font-size:15px; font-variant-numeric:tabular-nums; }
  .coords .hint { color:var(--dim); font-size:11px; margin-top:4px; word-break:break-all; }
  button { padding:8px 12px; border-radius:6px; border:1px solid var(--line); background:#232a36;
           color:var(--fg); font:13px/1.2 inherit; cursor:pointer; }
  button:hover { border-color:#3d4a5c; background:#2b3441; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.primary { width:100%; margin-top:10px; background:var(--acc); border-color:var(--acc); color:#fff; font-weight:600; }
  button.primary:hover { background:#2f74e0; }
  button.danger { width:100%; margin-top:8px; background:#3a1d1d; border-color:#612727; color:#ff9c9c; }
  button.danger:hover { background:#4a2222; border-color:var(--danger); }
  .warn { margin-top:8px; color:#ffb86b; font-size:11px; }
  .row { display:flex; align-items:center; gap:8px; justify-content:space-between; }
  .row b { font-size:13px; }
  .row .count { color:var(--dim); font-size:12px; margin-left:auto; margin-right:6px; }
  .list { margin-top:8px; max-height:230px; overflow:auto; font-size:12px; }
  .item { display:flex; gap:8px; padding:5px 6px; border-bottom:1px solid #1f2530; align-items:baseline; }
  .item:hover { background:#1d2430; }
  .item .lbl { color:#7dd3fc; font-weight:600; word-break:break-all; }
  .item .pos { color:var(--fg); font-variant-numeric:tabular-nums; margin-left:auto; white-space:nowrap; }
  .item .ts { color:var(--dim); font-size:11px; white-space:nowrap; }
  .empty { color:var(--dim); padding:8px 2px; }
  #log { margin:0; max-height:150px; overflow:auto; font:11px/1.5 ui-monospace,Consolas,monospace;
         color:#9fb0c4; white-space:pre-wrap; word-break:break-all; }
  .card.log { background:#0b0e13; }
  .tabs { display:flex; gap:8px; padding:8px 16px; border-bottom:1px solid var(--line);
          background:var(--panel); position:sticky; top:0; z-index:9; }
  .tab { background:#161b23; }
  .tab.active { background:var(--acc); border-color:var(--acc); color:#fff; font-weight:600; }
  .banner { background:#3a2c14; border:1px solid #6b4e1d; color:#ffd28a; padding:8px 10px;
            border-radius:8px; margin-bottom:8px; display:flex; gap:10px; align-items:center;
            flex-wrap:wrap; font-size:12px; }
  .pitem { display:flex; gap:10px; align-items:flex-start; padding:8px; border-bottom:1px solid #1f2530;
           cursor:pointer; }
  .pitem:hover { background:#1d2430; }
  .pitem .thumb { width:72px; height:auto; border:1px solid var(--line); border-radius:4px;
                  background:#000; flex:0 0 auto; }
  .pitem .thumb.ph { width:72px; height:120px; display:flex; align-items:center;
                     justify-content:center; color:var(--dim); font-size:11px; }
  .pitem .pbody { flex:1 1 auto; min-width:0; }
  .pitem .q { font-weight:600; word-break:break-word; }
  .pitem .pmeta { color:var(--dim); font-size:11px; margin-top:3px; word-break:break-word; }
  .pitem .pick { flex:0 0 auto; }
</style>
</head>
<body>
<header>
  <h1>点位置教学</h1>
  <div class="meta">设备 <code id="m-serial">-</code> · 屏幕 <span id="m-size">-</span> · 当前截图 <code id="m-shot">-</code></div>
  <div class="meta" id="m-hash"></div>
</header>

<nav class="tabs">
  <button class="tab active" id="tab-live">实时截图</button>
  <button class="tab" id="tab-pending">待教学 (<span id="p-count">0</span>)</button>
</nav>

<main id="pane-live">
  <section class="left">
    <div id="mode-banner" class="banner" hidden>
      <span>正在标注历史截图 <code id="p-mode-ts">-</code></span>
      <span id="p-mode-q"></span>
      <button id="btn-back-live">回到实时截图</button>
    </div>
    <div class="toolbar">
      <button id="btn-capture" title="重新抓一张当前屏幕">刷新截图</button>
      <button id="btn-zoom-out" title="缩小">−</button>
      <button id="btn-fit" title="适应窗口宽度">适应</button>
      <button id="btn-zoom-in" title="放大">＋</button>
      <span id="zoom-label">-</span>
    </div>
    <div id="frame">
      <div id="stage">
        <img id="shot" alt="手机截图" draggable="false">
        <div id="mark" hidden><span></span></div>
      </div>
      <div class="stage-msg" id="stage-msg">正在抓图…</div>
    </div>
    <div class="meta" style="color:var(--dim);font-size:12px;margin-top:8px">
      在截图上点一下 → 右侧显示设备真实坐标 → 填标签 → <b>保存为规则</b>。
    </div>
  </section>

  <aside class="right">
    <div class="card">
      <label for="in-label">标签 label（可以留空，留空就自动用下面的说明当代号）</label>
      <input id="in-label" type="text" list="label-suggests" autocomplete="off"
             placeholder="popup_close_x / tab_my_taobao / coin_enter / freeze">
      <datalist id="label-suggests"></datalist>

      <label for="in-note">备注 note（可选）</label>
      <input id="in-note" type="text" autocomplete="off" placeholder="这个位置是干什么用的">

      <div class="coords">
        <div>当前点击（设备像素）<b id="cur-coord">未点击</b></div>
        <div class="hint" id="cur-scale">缩放换算：-</div>
      </div>

      <button id="btn-save" class="primary">保存为规则</button>
      <button id="btn-tap" class="danger">试点击（真的点手机）</button>
      <div class="warn">⚠ 试点击只发 <code>input tap</code>，并且弹窗二次确认后才会发出；不会滑动、不按键、不启动 App。</div>
    </div>

    <div class="card">
      <div class="row">
        <b>已保存目标</b>
        <span class="count" id="list-count">共 0 条</span>
        <button id="btn-list" title="重新读取 targets.jsonl">列表</button>
        <button id="btn-copy" title="复制全部到剪贴板">复制</button>
      </div>
      <div class="list" id="list"><div class="empty">还没有保存任何目标</div></div>
    </div>

    <div class="card log"><pre id="log"></pre></div>
  </aside>
</main>

<section id="pane-pending" hidden>
  <div class="card" style="margin:14px">
    <div class="row">
      <b>待教学</b>
      <span class="count" id="p-info">-</span>
      <button id="btn-p-refresh" title="重新读取 pending 目录">刷新</button>
    </div>
    <div class="list" id="p-list" style="max-height:none">
      <div class="empty">正在读取…</div>
    </div>
  </div>
</section>

<script>
"use strict";
const $ = (s) => document.querySelector(s);
const img = $("#shot");
let S = { ok:true, error:"", serial:"", screen_w:0, screen_h:0, has_shot:false,
          shot_ts:"", image_hash:"", v:0 };
let LAST = null;     // 设备真实像素坐标 {x, y}
let dispW = 0;       // 当前 CSS 显示宽度
let fitMode = true;

/* ---------- 坐标换算（纯函数，测试直接打这两个） ---------- */
function cssToDevice(cssX, cssY, rectW, rectH, screenW, screenH) {
  if (!rectW || !rectH || !screenW || !screenH) return null;
  return { x: Math.round(cssX * (screenW / rectW)),
           y: Math.round(cssY * (screenH / rectH)) };
}
function deviceToCss(x, y, rectW, rectH, screenW, screenH) {
  if (!rectW || !rectH || !screenW || !screenH) return null;
  return { x: x * (rectW / screenW), y: y * (rectH / screenH) };
}

/* ---------- 小工具 ---------- */
function log(msg) {
  const el = $("#log");
  const t = new Date().toTimeString().slice(0, 8);
  el.textContent += "[" + t + "] " + msg + "\n";
  el.scrollTop = el.scrollHeight;
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}
async function getJSON(url) {
  try { return await (await fetch(url, { cache: "no-store" })).json(); }
  catch (e) { log("请求失败 " + url + ": " + e); return { ok:false, error:String(e) }; }
}
async function post(url, body) {
  try {
    const res = await fetch(url, { method:"POST", cache:"no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    return await res.json();
  } catch (e) { log("请求失败 " + url + ": " + e); return { ok:false, error:String(e) }; }
}

/* ---------- 渲染 ---------- */
function renderMeta() {
  $("#m-serial").textContent = S.serial || "-";
  $("#m-size").textContent = S.screen_w ? (S.screen_w + "×" + S.screen_h) : "-";
  $("#m-shot").textContent = S.has_shot ? (S.shot_ts + ".png") : (S.error || "无");
  $("#m-hash").textContent = S.image_hash ? ("sha256 " + S.image_hash.slice(0, 12)) : "";
  $("#stage-msg").style.display = S.has_shot ? "none" : "block";
  $("#stage-msg").textContent = S.error ? ("抓图失败：" + S.error + "（点【刷新截图】重试）") : "正在抓图…";
}

function applyWidth() {
  if (!img.naturalWidth) { dispW = 0; return; }
  const w = Math.max(120, Math.min(Math.round(dispW || img.naturalWidth), img.naturalWidth * 4));
  img.style.width = w + "px";
  img.style.height = "auto";
  updateScaleText();
  drawMark();
}
function fitWidth() {
  if (!img.naturalWidth) return;
  const box = $("#frame").clientWidth - 20;
  dispW = Math.min(img.naturalWidth, Math.max(160, box));
  fitMode = true;
  applyWidth();
}
function updateScaleText() {
  const r = img.getBoundingClientRect();
  if (!r.width || !S.screen_w) { $("#zoom-label").textContent = "-"; return; }
  const k = S.screen_w / r.width;
  $("#zoom-label").textContent = "显示宽 " + Math.round(r.width) + "px · 1 CSS px = " +
    k.toFixed(3) + " 设备像素";
}
function drawMark() {
  const m = $("#mark");
  if (!LAST || !S.screen_w) { m.hidden = true; return; }
  const r = img.getBoundingClientRect();
  const p = deviceToCss(LAST.x, LAST.y, r.width, r.height, S.screen_w, S.screen_h);
  if (!p) { m.hidden = true; return; }
  m.hidden = false;
  m.style.left = p.x + "px";
  m.style.top = p.y + "px";
  m.querySelector("span").textContent = LAST.x + ", " + LAST.y;
}

function setImage() {
  if (!S.has_shot) { renderMeta(); return; }
  const keep = fitMode ? null : dispW;
  img.onload = () => {
    fitMode = true;
    if (keep) { dispW = keep; fitMode = false; applyWidth(); } else fitWidth();
    renderMeta();
  };
  img.src = "/shot.png?v=" + Date.now();
  renderMeta();
}

async function loadState() {
  S = await getJSON("/api/state");
  renderMeta();
  if (S.has_shot) setImage();
  await renderList();
  await renderPending();
}

/* ---------- 待教学（auto.py 不会的界面） ---------- */
let MODE = "live";      // live = 实时截图 / pending = 标注历史截图
let PTS = "";           // 正在标注的 pending ts
let PITEMS = [];

function switchTab(name) {
  $("#tab-live").classList.toggle("active", name === "live");
  $("#tab-pending").classList.toggle("active", name === "pending");
  $("#pane-live").hidden = name !== "live";
  $("#pane-pending").hidden = name !== "pending";
  if (name === "pending") renderPending();
}

async function renderPending() {
  const r = await getJSON("/api/pending");
  PITEMS = (r && r.items) || [];
  $("#p-count").textContent = String(PITEMS.length);
  $("#p-info").textContent = "共 " + PITEMS.length + " 条待教 · 目录 " + ((r && r.pending_dir) || "-");
  const box = $("#p-list");
  if (!PITEMS.length) {
    box.innerHTML = '<div class="empty">没有待教学的问题。auto.py 遇到不会的界面就会出现在这里。</div>';
    return;
  }
  box.innerHTML = PITEMS.map((p) =>
    '<div class="pitem" data-ts="' + esc(p.ts) + '">' +
      (p.has_shot
        ? '<img class="thumb" loading="lazy" src="/api/pending/shot?ts=' + encodeURIComponent(p.ts) + '&t=' + Date.now() + '" alt="">'
        : '<div class="thumb ph">无截图</div>') +
      '<div class="pbody"><div class="q">' + esc(p.question || "(没有问题描述)") + "</div>" +
        '<div class="pmeta">' + esc(p.ts) + " · " + esc(p.serial || "-") + " · " +
          (p.screen_w || 0) + "×" + (p.screen_h || 0) +
          (p.label_hint ? " · 建议标签 " + esc(p.label_hint) : "") +
          (p.kind ? " · " + esc(p.kind) : "") + "</div></div>" +
      '<button class="pick">标注</button>' +
    "</div>").join("");
  box.querySelectorAll(".pitem").forEach((el) => {
    el.onclick = () => openPending(el.getAttribute("data-ts"));
  });
}

async function openPending(ts) {
  const item = PITEMS.find((x) => String(x.ts) === String(ts));
  if (!item) { log("这条待教学已经不在了（可能刚被教过）"); await renderPending(); return; }
  if (!item.has_shot) { log("这条没有截图，无法标注"); return; }
  MODE = "pending"; PTS = String(ts);
  S = { ok:true, error:"", serial:item.serial || "", screen_w:item.screen_w || 0,
        screen_h:item.screen_h || 0, has_shot:true, shot_ts:String(ts), image_hash:"", v:0 };
  LAST = null;
  $("#cur-coord").textContent = "未点击";
  $("#cur-scale").textContent = "缩放换算：-";
  $("#mode-banner").hidden = false;
  $("#p-mode-ts").textContent = String(ts);
  $("#p-mode-q").textContent = item.question || "";
  $("#in-label").value = item.label_hint || "";
  $("#in-note").value = item.question || "";
  $("#btn-capture").disabled = true;
  $("#btn-tap").disabled = true;
  switchTab("live");
  img.onload = () => { renderMeta(); fitWidth(); drawMark(); };
  img.src = "/api/pending/shot?ts=" + encodeURIComponent(ts) + "&t=" + Date.now();
  renderMeta();
  log("标注待教学 " + ts + " → 在历史截图上点正确位置，标签已按建议填好");
}

function backToLive() {
  MODE = "live"; PTS = "";
  $("#mode-banner").hidden = true;
  $("#btn-capture").disabled = false;
  $("#btn-tap").disabled = false;
  LAST = null; drawMark();
  $("#cur-coord").textContent = "未点击";
  $("#cur-scale").textContent = "缩放换算：-";
  loadState();
}

async function renderList() {
  const r = await getJSON("/api/targets");
  const rows = (r && r.targets) || [];
  $("#list-count").textContent = "共 " + rows.length + " 条";
  const box = $("#list");
  if (!rows.length) { box.innerHTML = '<div class="empty">还没有保存任何目标</div>'; return; }
  const ds = $("#label-suggests");
  const labels = [...new Set(rows.map((t) => t.label))].reverse();
  ds.innerHTML = labels.map((l) => '<option value="' + esc(l) + '">').join("");
  box.innerHTML = rows.slice().reverse().map((t) =>
    '<div class="item"><div class="lbl">' + esc(t.label) + "</div>" +
    '<div class="pos">(' + t.x + ", " + t.y + ")</div>" +
    '<div class="ts">' + esc(t.ts) + "</div></div>").join("");
}

/* ---------- 交互 ---------- */
img.addEventListener("click", (ev) => {
  if (!img.naturalWidth || !S.screen_w) return;
  const r = img.getBoundingClientRect();
  const cx = ev.clientX - r.left, cy = ev.clientY - r.top;
  const d = cssToDevice(cx, cy, r.width, r.height, S.screen_w, S.screen_h);
  if (!d) return;
  LAST = d;
  $("#cur-coord").textContent = "(" + d.x + ", " + d.y + ")";
  $("#cur-scale").textContent = "页面点 (" + Math.round(cx) + ", " + Math.round(cy) + ") @ " +
    Math.round(r.width) + "px 宽 → 设备 (" + d.x + ", " + d.y + ") @ " +
    S.screen_w + "×" + S.screen_h + "（比例 ×" + (S.screen_w / r.width).toFixed(3) + "）";
  drawMark();
});

$("#btn-save").onclick = async () => {
  if (!LAST) { log("先在截图上点一下位置"); return; }
  const label = $("#in-label").value.trim();   // 可留空：服务端会用备注/建议标签当代号
  const note = $("#in-note").value.trim();
  if (MODE === "pending") {
    const r = await post("/api/pending/save", { ts: PTS, label: label, x: LAST.x, y: LAST.y, note: note });
    if (!r.ok) { log("保存失败：" + r.error); return; }
    log("已教 " + r.row.label + " → (" + r.row.x + ", " + r.row.y + ")  规则已写进 targets.jsonl" +
        "；待教学剩 " + r.pending_count + " 条");
    PTS = ""; MODE = "live";
    $("#mode-banner").hidden = true;
    $("#btn-capture").disabled = false;
    $("#btn-tap").disabled = false;
    LAST = null;
    $("#cur-coord").textContent = "未点击";
    $("#cur-scale").textContent = "缩放换算：-";
    drawMark();
    await renderList();
    switchTab("pending");
    return;
  }
  const r = await post("/api/save", { label: label, x: LAST.x, y: LAST.y, note: note });
  if (!r.ok) { log("保存失败：" + r.error); return; }
  log("已保存 " + r.row.label + " → (" + r.row.x + ", " + r.row.y + ")  截图 " + r.row.screenshot_name);
  LAST = null;
  $("#cur-coord").textContent = "未点击";
  $("#cur-scale").textContent = "缩放换算：-";
  drawMark();
  await renderList();
};

$("#tab-live").onclick = () => {
  if (MODE === "pending") { backToLive(); return; }
  switchTab("live");
};
$("#tab-pending").onclick = () => switchTab("pending");
$("#btn-back-live").onclick = () => backToLive();
$("#btn-p-refresh").onclick = async () => { await renderPending(); log("待教学列表已刷新"); };

$("#btn-capture").onclick = async () => {
  log("正在抓图…");
  $("#btn-capture").disabled = true;
  const r = await post("/api/capture", {});
  $("#btn-capture").disabled = false;
  if (!r.ok) { S = r; renderMeta(); log("抓图失败：" + (r.error || "未知错误")); return; }
  S = r; setImage(); drawMark();
  log("已刷新截图 " + S.shot_ts + ".png (" + S.screen_w + "×" + S.screen_h + ")");
};

$("#btn-tap").onclick = async () => {
  if (!LAST) { log("先在截图上点一下位置，再试点击"); return; }
  const ok = window.confirm(
    "真的要点击手机吗？\n\n" +
    "设备: " + S.serial + "\n" +
    "坐标: (" + LAST.x + ", " + LAST.y + ")  设备像素\n\n" +
    "确认后执行的命令只有这一条：\n" +
    "adb -s " + S.serial + " shell input tap " + LAST.x + " " + LAST.y + "\n\n" +
    "这会在手机屏幕上真的点一下，无法撤销。");
  if (!ok) { log("已取消试点击"); return; }
  log("试点击 (" + LAST.x + ", " + LAST.y + ") …");
  $("#btn-tap").disabled = true;
  const r = await post("/api/tap", { x: LAST.x, y: LAST.y, confirm: true });
  $("#btn-tap").disabled = false;
  if (!r.ok) { log("试点击失败：" + r.error); return; }
  log("已发送 input tap，正在重新截图…");
  S = r.state; setImage(); drawMark();
};

$("#btn-list").onclick = async () => { await renderList(); log("列表已重新读取"); };

$("#btn-copy").onclick = async () => {
  const r = await getJSON("/api/targets");
  const rows = (r && r.targets) || [];
  if (!rows.length) { log("没有可复制的内容"); return; }
  const text = rows.map((t) => t.label + "\t(" + t.x + ", " + t.y + ")\t" +
    t.screen_w + "x" + t.screen_h + "\t" + t.ts).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    log("已复制 " + rows.length + " 条到剪贴板");
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); log("已复制 " + rows.length + " 条（兼容模式）"); }
    catch (e2) { log("复制失败，请手动选中列表复制"); }
    document.body.removeChild(ta);
  }
};

$("#btn-fit").onclick = () => { fitWidth(); };
$("#btn-zoom-in").onclick = () => {
  if (!img.naturalWidth) return;
  fitMode = false;
  dispW = Math.min(img.naturalWidth * 4, (dispW || img.naturalWidth) * 1.25);
  applyWidth();
};
$("#btn-zoom-out").onclick = () => {
  if (!img.naturalWidth) return;
  fitMode = false;
  dispW = Math.max(160, (dispW || img.naturalWidth) * 0.8);
  applyWidth();
};

window.addEventListener("resize", () => { if (fitMode) fitWidth(); else drawMark(); });
window.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && document.activeElement.id === "in-label") $("#btn-save").click();
});

log("页面就绪；在截图上点位置 → 填标签 → 保存为规则");
loadState();
</script>
</body>
</html>

"""


# ------------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="点位置教学工具：看截图点位置，把点击记成可复用规则")
    ap.add_argument("--serial", default="",
                    help="设备序列号，如 192.168.1.8:5555（默认读 coin11/.phone_serial）")
    ap.add_argument("--port", type=int, default=8777, help="本地端口（默认 8777）")
    ap.add_argument("--host", default="127.0.0.1", help="只允许 127.0.0.1 / localhost")
    ap.add_argument("--data-dir", default=os.path.join(_ROOT, "targets"),
                    help="输出目录（默认 coin11/targets）")
    ap.add_argument("--shot", default="", help="不从手机抓图，改用一张已有 PNG（离线教学）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args(argv)

    global _APP
    if args.host not in _ALLOWED_HOSTS:
        ap.error("为了安全只允许绑定 127.0.0.1（当前 --host=%s）" % args.host)

    serial = args.serial or default_serial()
    if not serial:
        ap.error("没有可用设备：请用 --serial 指定，例如 --serial 192.168.1.8:5555")

    data_dir = os.path.abspath(args.data_dir)
    _APP = TeachSession(serial, data_dir)

    print("=" * 62)
    print(" 点位置教学工具  coin11/teach.py")
    print("=" * 62)
    print(" 设备      : %s" % serial)
    print(" adb       : %s" % ADB)
    print(" 规则库    : %s" % _APP.targets_path)
    print(" 截图目录  : %s" % _APP.shots_dir)

    if args.shot:
        if _APP.capture(from_file=args.shot):
            print(" 初始截图  : 本地文件 %s (%dx%d)"
                  % (args.shot, _APP.screen_w, _APP.screen_h))
        else:
            print(" 初始截图失败: %s" % _APP.error)
    else:
        print(" 正在抓第一张截图 …")
        if _APP.capture():
            print(" 初始截图  : %dx%d, %s.png"
                  % (_APP.screen_w, _APP.screen_h, _APP.ts))
        else:
            print(" 初始截图失败: %s（页面里点【刷新截图】可重试）" % _APP.error)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(" 端口 %d 起不来: %s" % (args.port, exc))
        return 2
    srv.daemon_threads = True
    url = "http://127.0.0.1:%d" % args.port
    print(" 访问地址  : %s" % url)
    print("=" * 62)
    print(" 只监听 127.0.0.1；唯一的手机操作是用户亲手确认的 input tap。")
    print(" Ctrl+C 结束服务。")
    sys.stdout.flush()

    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
