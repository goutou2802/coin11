# -*- coding: utf-8 -*-
"""coin11 打包入口：同一个 exe，两副面孔。

    coin11.exe                 起面板（dash.py）：这次真由我起服务才开浏览器，否则只打印地址
    coin11.exe --run-auto ...  当子进程跑 auto.py —— 面板原来用 python.exe 起子进程，
                               打包后既没有 python.exe 也没有 auto.py 文件，
                               于是让 exe 自己带 --run-auto 再起一份。

没打包（源码模式）时这里一个字都不改：不设 COIN11_ROOT、不设精简模式，
所有路径与改动前逐字一致 —— 开发流程零回归。
"""
import os
import socket
import sys
import threading
import time
import webbrowser

_HERE = os.path.dirname(os.path.abspath(__file__))
_FROZEN = bool(getattr(sys, "frozen", False))


def _port():
    """面板端口：默认 8899；被占用时用 COIN11_PORT 换（dash.py 认同一个变量）。"""
    try:
        return int((os.environ.get("COIN11_PORT") or "").strip() or 8899)
    except ValueError:
        return 8899


def _url():
    return "http://127.0.0.1:%d/" % _port()


def _app_dir():
    """exe 所在目录（dist/coin11）；源码模式 = coin11/ 本身。"""
    return os.path.dirname(os.path.abspath(sys.executable)) if _FROZEN else _HERE


def _res_dir():
    """PyInstaller 的 datas 根（onedir 是 <app>/_internal）；源码模式 = coin11/。"""
    return getattr(sys, "_MEIPASS", _HERE) if _FROZEN else _HERE


def _seed(src, dst):
    """首次启动把随包的数据拷进可写目录（递归）；已存在的**不覆盖**（用户教出来的不冲掉）。"""
    if not os.path.isdir(src):
        return 0
    n = 0
    for cur, _dirs, files in os.walk(src):
        rel = os.path.relpath(cur, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out, exist_ok=True)
        for name in files:
            s, d = os.path.join(cur, name), os.path.join(out, name)
            if os.path.exists(d):
                continue
            try:
                with open(s, "rb") as f:
                    blob = f.read()
                with open(d, "wb") as f:
                    f.write(blob)
                n += 1
            except OSError:
                pass
    return n


def bootstrap():
    """打包版才做的事：可写数据根 + adb 指路 + 同学版默认。源码模式原样不动。"""
    if not _FROZEN:
        return
    app, res = _app_dir(), _res_dir()
    data = os.path.join(app, "data")
    for sub in (os.path.join(data, "coin11", "targets"),
                os.path.join(data, "coin11", "state"),
                os.path.join(data, "evidence")):
        os.makedirs(sub, exist_ok=True)
    _seed(os.path.join(res, "resources", "targets"), os.path.join(data, "coin11", "targets"))
    # 离线自测（--selftest）的夹具：开发机的 evidence 不随包，只带被断言引用的那几个帧
    _seed(os.path.join(res, "resources", "selftest"), os.path.join(data, "selftest"))
    os.environ.setdefault("COIN11_ROOT", data)
    os.environ.setdefault("COIN11_SELFTEST_ROOT", os.path.join(data, "selftest"))
    # 同学的手机型号不可能预登记在 DEV_MODEL_LABEL 里，设备表改成「在线就自动配」
    os.environ.setdefault("COIN11_DEVICE_AUTO", "1")
    # 2026-11-09 用户定案：分发版和自用版一样「有轮次、留证据」，靠面板【记录管理】删旧记录，
    # 所以这里**不再**开精简模式（COIN11_STUDENT 不再设）。
    # 唯一还差的一样是教学页（单独的 teach.py 服务，没打进包）：让面板把那个按钮藏掉，别留死链。
    os.environ.setdefault("COIN11_NO_TEACH", "1")
    # 同学的 cmd 默认是 GBK：打印不了的字替换掉，别让一条 print 掀翻整个面板线程
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    adb_dir = os.path.join(app, "adb")
    adb = os.path.join(adb_dir, "adb.exe")
    if os.path.isfile(adb):
        os.environ.setdefault("ADB", adb)                    # dash.py 认这个
        os.environ.setdefault("ADBUTILS_ADB_PATH", adb)      # adbutils 认这个
        os.environ["PATH"] = adb_dir + os.pathsep + os.environ.get("PATH", "")


def _port_busy():
    """这个端口上是不是已经有东西在应答（= 已经有一份面板在跑）。"""
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", _port()))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _open_when_up(timeout=20.0):
    """等面板端口通了再开浏览器（服务起来要一两秒）。"""
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", _port()))
            break
        except OSError:
            time.sleep(0.2)
        finally:
            s.close()
    try:
        webbrowser.open(_url())
    except Exception:  # noqa: BLE001
        pass


def run_dash():
    # 开浏览器这件事统一由 dash.py 负责（它知道端口是不是真的由自己起、也能用 --app 模式开窗口，
    # 这样【⏻ 关闭面板】才能把那个窗口一起关掉）。这里只提示一句，绝不重复开第二个。
    if _FROZEN and _port_busy():
        print("[coin11] 面板已经在跑了：%s（这次不开窗口，也不重复起服务）" % _url())
    import dash
    return dash.main()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    bootstrap()
    if "--run-auto" in argv:
        i = argv.index("--run-auto")
        del argv[i]
        import auto
        return auto.main(argv)
    return run_dash()


if __name__ == "__main__":
    sys.exit(main() or 0)
