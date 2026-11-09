# -*- coding: utf-8 -*-
"""面板/入口：已经在跑的时候，也要把页面打开（这次不重复起服务）。"""
import io

# 1) dash.py：端口被占用 -> 把页面打开，然后退出
p = r"C:\Users\28024\Documents\deepseek-harness\default-workspace\coin11\dash.py"
s = io.open(p, encoding="utf-8").read()
old = '''    if _port_in_use():
        print("[dash] 已经有面板在 http://%s:%d/ 跑着了，本次不再启动（直接开浏览器即可）"
              % (HOST, PORT))
        return 0'''
assert s.count(old) == 1, ("dash port_in_use", s.count(old))
new = '''    if _port_in_use():
        # 2026-11-09 用户反馈「分发版不自动打开监控页面」：原来这里只打印一句就退了，等于
        # 「面板已经在跑的时候，再双击一次什么都不发生」。改成：不再重复起服务，但**把页面打开**。
        _note_browser("端口 %d 已被占用：不重复起服务，只打开页面" % PORT)
        open_panel_window("http://%s:%d/" % (HOST, PORT))
        return 0'''
s = s.replace(old, new, 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("patched dash.main")

# 2) app_entry.py：打包版这条分支同理
p2 = r"__WINBASE__\app_entry.py"
s = io.open(p2, encoding="utf-8").read()
old2 = '''    if _FROZEN and _port_busy():
        print("[coin11] 面板已经在跑了：%s（这次不开窗口，也不重复起服务）" % _url())
    import dash
    return dash.main()'''
assert s.count(old2) == 1, ("entry", s.count(old2))
new2 = '''    import dash
    if _FROZEN and _port_busy():
        # 已经在跑了：不重复起服务，但把页面打开（用户双击第二次就是想要那个页面）
        print("[coin11] 面板已经在跑了，打开页面：%s" % _url(), flush=True)
        try:
            dash.open_panel_window(_url())
        except Exception:  # noqa: BLE001
            pass
        return 0
    return dash.main()'''
s = s.replace(old2, new2, 1)
io.open(p2, "w", encoding="utf-8", newline="").write(s)
print("patched app_entry")
