# -*- coding: utf-8 -*-
"""桌面工具箱入口（打包 exe 用）。

冻结(windowed/noconsole)模式下 sys.stdout/stderr 为 None，print 会抛异常 →
这里先把它们重定向到 exe 同目录的 toolbox.log，再启动托盘主程序。
"""
import os
import sys
import time

# 防御性修复：清理指向已失效临时目录的残存 TCL/TK 环境变量并保证正确注入
for _lib_var, _sub in (('TCL_LIBRARY', '_tcl_data'), ('TK_LIBRARY', '_tk_data')):
    _val = os.environ.get(_lib_var)
    if not _val or not os.path.isdir(_val):
        if getattr(sys, 'frozen', False):
            _t = os.path.join(getattr(sys, '_MEIPASS', ''), _sub)
            if os.path.isdir(_t):
                os.environ[_lib_var] = _t
        else:
            _u = os.path.join(sys.base_prefix, 'tcl', 'tcl8.6' if 'TCL' in _lib_var else 'tk8.6')
            if os.path.isdir(_u):
                os.environ[_lib_var] = _u

# [2026-10-04] 单例保护:8790 已有监听即静默退出——防 guard 与开机自启竞态产生
# 无端口僵尸实例(10-03 起实测双实例,僵尸不退出不占端口纯占内存)。
def _already_running():
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 8790), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False

if _already_running():
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, "toolbox.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " singleton: 8790 already running, exit")
    except Exception:
        pass
    raise SystemExit(0)


def _setup_logging():
    """冻结模式：无论 stdout 是什么，一律重定向到 exe 同目录的日志。"""
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    try:
        f = open(os.path.join(base, "toolbox.log"), "a",
                 encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = f
        sys.stderr = f
    except Exception as e:
        try:
            with open(os.path.join(base, "emergency.log"), "a", encoding="utf-8") as ef:
                ef.write(f"Failed to open toolbox.log: {e}\n")
        except Exception:
            pass
    import time
    print(f"\n=== toolbox start {time.strftime('%Y-%m-%d %H:%M:%S')} ===",
          flush=True)


def main():
    _setup_logging()
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import tray_app
        tray_app.main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        if sys.stderr and hasattr(sys.stderr, "flush"):
            sys.stderr.flush()



if __name__ == "__main__":
    main()
