# -*- coding: utf-8 -*-
"""翻译工具箱主程序（托盘常驻）。

运行：python tray_app.py（或双击 启动划词翻译.bat）
热键（默认；Ctrl+Alt+T/E 已被本机其他程序占用，故用 Ctrl+Shift 系）：
  Ctrl+Shift+T  选中文本 -> 翻成中文
  Ctrl+Shift+E  选中文本 -> 翻成英文
  Ctrl+Alt+S    框选屏幕区域 -> OCR 识别 -> 翻成中文
托盘：
  双击 = 打开/唤起工作台；右键 = 本地翻译模型状态 / 打开工作台 / 启停本地翻译模型 / 退出
  （菜单上的「本地翻译模型」= 本地翻译后端 llama-server，端口 8899，
    与常驻的界面服务 8790 无关）
工作台窗口：
  pywebview(WebView2 系统组件) 原生独立窗口，无地址栏无标签页、不依赖 Chrome；
  点 X 仅隐藏回托盘；再次双击托盘唤起。pywebview 不可用时降级 Chrome --app。
线程模型：主线程跑 pywebview GUI（6.x 强制主线程），
  托盘 + 全局热键 + Win32 消息循环在后台线程。
"""
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path

# 确保能 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from selection import (get_selected_text, load_config, parse_hotkey,
                       save_config)
from translate_backend import (PORT, HOST, port_busy, start_server,
                               stop_server, wait_ready)
from popup import TransPopup
from tray_icon import TrayIcon

# ==================== 原生窗口（pywebview / WebView2 系统组件） ====================
try:
    import webview
    _WEBVIEW_OK = True
    _WEBVIEW_ERR = None
except Exception as _wv_err:            # noqa: BLE001
    _WEBVIEW_OK = False
    _WEBVIEW_ERR = _wv_err
print(f"[window] import webview 状态: ok={_WEBVIEW_OK}, err={_WEBVIEW_ERR}", flush=True)


_WIN_TITLE = "小宇工具箱"

import dark_frame                       # DWM 暗色边框注入（2026-09-20）
import epic_claimer                     # Epic 周免（游戏专区）：PROBE/CLICK 脚本与判定


def _on_win_shown():
    """窗口每次显示（含托盘唤起）后，按持久化偏好刷新标题栏/边框配色。"""
    try:
        dark_frame.apply_to_title(_WIN_TITLE)
    except Exception as ex:              # noqa: BLE001
        print(f"[window] 暗色边框注入失败: {ex}", file=sys.stderr)
_win_open_evt = threading.Event()       # 请求创建/显示窗口
_win_quit_evt = threading.Event()       # 全局退出
_win_obj = None                         # 当前 pywebview Window（主线程创建）


def _exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _ensure_webrtc_leak_guard(storage_path):
    """WebView2 防 WebRTC 真 IP 泄漏：启动前向 userDataFolder 注入 pref（2026-10-14）。

    Edge/WebView2 引擎既不支持 --force-webrtc-ip-handling-policy 参数，也不读浏览器策略
    注册表；唯一有效杠杆是 Preferences 里的 webrtc.ip_handling_policy=disable_non_proxied_udp
    （+ multiple_routes_enabled/nonproxied_udp_enabled 两只 boolean）——随后 WebRTC 不再
    发送非代理 UDP/STUN，真实公网 IP 不再进入 ICE 候选。pref 仅在浏览器进程启动时读取，
    故必须在 webview.start 之前写入。对照实验与细节见 skill「webrtc-leak-hardening」。
    幂等合并；任何失败只记日志、不阻断启动。
    """
    try:
        prefs_path = os.path.join(storage_path, "EBWebView", "Default", "Preferences")
        os.makedirs(os.path.dirname(prefs_path), exist_ok=True)
        data = {}
        if os.path.exists(prefs_path):
            raw = open(prefs_path, encoding="utf-8").read().strip()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("Preferences 不是 JSON 对象")
        webrtc = data.setdefault("webrtc", {})
        if not isinstance(webrtc, dict):
            webrtc = data["webrtc"] = {}
        want = {
            "ip_handling_policy": "disable_non_proxied_udp",
            "multiple_routes_enabled": False,
            "nonproxied_udp_enabled": False,
        }
        if all(webrtc.get(k) == v for k, v in want.items()):
            return
        webrtc.update(want)
        with open(prefs_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print("[webrtc] 已写入 WebRTC 防泄漏 pref:", prefs_path, flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[webrtc] pref 注入失败（不影响启动）: {ex}", file=sys.stderr)


def _on_win_closing(window):
    """点 X：隐藏回托盘（不销毁）；托盘「退出」时放行销毁。

    返回 False = 阻止本次关闭（pywebview closing 事件约定）。
    """
    if _win_quit_evt.is_set():
        return None
    window.hide()
    return False


def _win_loop():
    """主线程专属：pywebview GUI 循环。窗口销毁后可按需重建。"""
    print("[window] _win_loop 启动", flush=True)
    global _win_obj
    if not _WEBVIEW_OK:
        print(f"[window] pywebview 不可用（{_WEBVIEW_ERR}），"
              "工作台窗口回退 Chrome App 模式", flush=True)
        _win_quit_evt.wait()
        return
    icon = os.path.join(_exe_dir(), "qwen_toolbox.ico")
    while not _win_quit_evt.is_set():
        if not _win_open_evt.wait(timeout=0.5):
            continue
        if _win_quit_evt.is_set():
            break
        _win_open_evt.clear()
        try:
            print("[window] create_window...", flush=True)
            _win_obj = webview.create_window(
                _WIN_TITLE, f"http://127.0.0.1:{STATS_PORT}/",
                width=1380, height=880, min_size=(1080, 680),
                background_color="#090a0f",
                # pywebview 默认会注入 `user-select: none`；工作台中的普通文字
                # 需要能像浏览器页面一样拖选并复制。
                text_select=True,
            )
            _win_obj.events.closing += _on_win_closing
            _win_obj.events.shown += _on_win_shown
            kwargs = {"gui": "edgechromium"}
            # Epic 周免领取窗口需要登录态跨重启保留：关闭 InPrivate，启用持久 profile。
            # 主工作台同受影响（外观设置 localStorage 兜底由此更可靠），2026-10-10 起。
            kwargs["private_mode"] = False
            kwargs["storage_path"] = os.path.join(_exe_dir(), "runtime", "webview_profile")
            # WebRTC 真 IP 防泄漏：必须写在 webview 启动前（2026-10-14，详见下方函数注释）
            _ensure_webrtc_leak_guard(kwargs["storage_path"])
            # icon 参数实测（2026-09-19）：传入时会令 WebView2 初始化挂起，暂不启用
            # if os.path.exists(icon):
            #     kwargs["icon"] = icon
            print("[window] webview.start...", flush=True)
            webview.start(**kwargs)     # 阻塞至窗口真正销毁
            print("[window] GUI 循环返回", flush=True)
        except Exception as ex:         # noqa: BLE001
            print(f"[window] 原生窗口异常: {ex}", flush=True)
            _win_obj = None
            time.sleep(1)
    print(f"[window] _win_loop 循环结束，_win_quit_evt={_win_quit_evt.is_set()}", flush=True)


def request_window_show():
    """请求显示工作台原生窗口（线程安全：托盘/热键线程可调）。"""
    print("[window] request_window_show 被调用", flush=True)
    global _win_obj
    if not _WEBVIEW_OK:
        return _open_in_browser_fallback()
    w = _win_obj
    if w is not None:
        try:
            w.show()                    # 窗口还在（隐藏态）：直接唤起
            hwnd = user32.FindWindowW(None, _WIN_TITLE)
            if hwnd:
                user32.ShowWindow(hwnd, 0x09)
                user32.SetForegroundWindow(hwnd)
            dark_frame.apply_to_title(_WIN_TITLE)   # 隐藏→显示时 DWM 可能重绘回白边
            return True
        except Exception:               # noqa: BLE001
            _win_obj = None             # 窗口已失效：走重建
    _win_open_evt.set()
    return True


def _activate_existing_window():
    """单实例二次启动：跨进程唤起已有原生窗口。"""
    hwnd = user32.FindWindowW(None, _WIN_TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 0x09)       # SW_RESTORE（隐藏/最小化都拉回）
    user32.SetForegroundWindow(hwnd)
    return True


# ==================== Epic 周免半自动领取窗口（游戏专区） ====================

_CLAIM_WIN_TITLE = "Epic 周免领取"
_claim_win_obj = None                   # 领取窗口（独立于工作台 _win_obj，随用户关闭）


def _claim_banner(win, text, kind="info"):
    """在领取窗口页面顶部注入横幅（CDP 执行不受页面 CSP 限制）。"""
    color = "#3b82f6" if kind == "info" else ("#16a34a" if kind == "ok" else "#dc2626")
    js = ("(function(){var b=document.getElementById('xy-claim-banner');"
          "if(!b){b=document.createElement('div');b.id='xy-claim-banner';"
          "document.body.appendChild(b);}"
          "b.textContent=" + json.dumps(text, ensure_ascii=False) + ";"
          "b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:999999;"
          "padding:10px 16px;color:#fff;font:14px/1.5 sans-serif;text-align:center;"
          "background:" + color + "';})()")
    try:
        win.evaluate_js(js)
    except Exception:                   # noqa: BLE001 页面跳转中可能失败，下轮再试
        pass


def _claim_foreground(win):
    """领取窗口置前（登录/需要人工时）。"""
    hwnd = user32.FindWindowW(None, _CLAIM_WIN_TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 0x09)
        user32.SetForegroundWindow(hwnd)


def _claim_click(win, text):
    """按精确文本点击可见按钮。文本经 json.dumps 生成 JS 字面量，防注入。"""
    js = epic_claimer.CLICK_JS_FMT.replace('"__TEXT__"',
                                           json.dumps(text, ensure_ascii=False))
    try:
        return win.evaluate_js(js)
    except Exception:                   # noqa: BLE001
        return None


def _claim_drive_one(win, item, idx, timeout=90):
    """单个游戏的页面状态机：轮询探测→点击，直到 owned/完成/超时。"""
    deadline = time.time() + timeout
    login_hinted = False
    captcha_hinted = False
    last_kind = ''
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            probe = win.evaluate_js(epic_claimer.PROBE_JS)
        except Exception:               # noqa: BLE001 窗口被用户关闭等
            return 'failed'
        kind, text = epic_claimer.judge_page(probe)
        if kind != last_kind:
            print(f"[epic] {item['title']} -> {kind}" + (f" ({text})" if text else ""),
                  flush=True)
            desc_map = {
                'get': '获取中',
                'checkout': '订单确认中',
                'age_gate': '年龄验证',
                'cookie': '提示确认',
                'captcha': '安全验证中',
                'login': '需要登录',
                'owned': '已在库中',
                'done': '领取成功'
            }
            epic_claimer.claim_progress_update(idx, desc_map.get(kind, kind))
            last_kind = kind
        if kind == 'owned':
            _claim_banner(win, f"《{item['title']}》已在库中", "ok")
            time.sleep(1.5)
            return 'owned'
        if kind == 'done':
            time.sleep(2)
            return 'claimed'
        if kind == 'captcha':
            if not captcha_hinted:
                captcha_hinted = True
                _claim_banner(win, "检测到安全验证，若弹出验证码请在窗口中完成，完成后自动继续", "warn")
                _claim_foreground(win)
            continue
        if kind in ('get', 'checkout', 'age_gate', 'cookie') and text:
            ret = _claim_click(win, text)
            if ret == 'not-found':
                time.sleep(1)
            elif kind == 'age_gate':
                time.sleep(2)
        elif kind == 'login' and not login_hinted:
            login_hinted = True
            _claim_banner(win, "请在窗口中登录 Epic 账号，登录后自动继续领取", "info")
            _claim_foreground(win)
    return 'failed'


def _claim_fallback_browser(items, on_done):
    """领取窗口不可用（pywebview 异常/工作台窗口从未创建）：开系统浏览器手动领。"""
    import webbrowser
    for item in items:
        try:
            webbrowser.open(item['url'])
        except Exception:               # noqa: BLE001
            pass
    threading.Thread(target=lambda: (time.sleep(1), on_done(['failed'] * len(items))),
                     daemon=True).start()
    return True


def _open_claim_window(items, on_done):
    """Epic 周免半自动领取（epic_claimer 回调，可能由调度线程触发）。

    真实 WebView2 逐个打开详情页，注入 JS 探测「立即领取/下订单/在库中/登录」
    状态并自动点击；登录或验证码由用户在窗口内完成，登录态随持久 profile 保留。
    返回 True=已启动（结果经 on_done(results) 回写，result ∈ claimed/owned/failed）。
    """
    global _claim_win_obj
    if not _WEBVIEW_OK or _win_obj is None:
        # GUI 循环未运行（工作台窗口从未创建）时无法动态建窗 → 回退浏览器
        return _claim_fallback_browser(items, on_done)

    def driver():
        results = [None] * len(items)
        win = None
        try:
            win = webview.create_window(
                _CLAIM_WIN_TITLE, items[0]['url'],
                width=1100, height=760, background_color="#101014",
            )
            _claim_win_obj = win
            loaded_evt = threading.Event()
            win.events.loaded += lambda: loaded_evt.set()
            for idx, item in enumerate(items):
                epic_claimer.claim_progress_update(idx, '页面加载中')
                if idx > 0:
                    loaded_evt.clear()
                    win.load_url(item['url'])
                    loaded_evt.wait(15)
                else:
                    if not win.events.loaded.is_set():
                        loaded_evt.wait(10)
                time.sleep(2)       # SPA 首屏渲染缓冲
                results[idx] = _claim_drive_one(win, item, idx)
            ok = [t for t, r in zip(items, results) if r in ('claimed', 'owned')]
            _claim_banner(win,
                          f"领取流程结束：成功 {len(ok)}/{len(items)}，窗口稍后自动关闭",
                          "ok" if ok else "warn")
        except Exception as ex:     # noqa: BLE001
            print(f"[epic] 领取驱动异常: {ex}", flush=True)
            for i, r in enumerate(results):
                if r is None:
                    results[i] = 'failed'

        def _late_close():
            time.sleep(60)
            try:
                win.destroy()
            except Exception:       # noqa: BLE001
                pass

        threading.Thread(target=_late_close, daemon=True).start()
        try:
            if _win_obj is not None:
                _win_obj.show()     # 主窗口可能处于隐藏态，回收焦点
        except Exception:           # noqa: BLE001
            pass
        on_done([r or 'failed' for r in results])

    threading.Thread(target=driver, daemon=True).start()
    return True


def _notify_existing_window_show(timeout=1.5):
    """跨进程通过 HTTP 指令唤起已有实例的窗口。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{STATS_PORT}/api/window/show",
            headers={"User-Agent": "XiaoyuToolbox-Launcher"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok"))
    except Exception:
        return False


def _kill_other_toolbox_instances():
    """清理非当前进程的其它卡死或幽灵 XiaoyuToolbox 进程。

    两种形态都要认（2026-09-24 补）：打包版进程名是 XiaoyuToolbox.exe，源码版是
    python/pyw 跑 toolbox_main.py。原来只匹配 exe 名，导致 pythonw 幽灵实例清不掉——
    实测出现过「一个占着 8790、另一个无窗口无端口的幽灵」长期并存。
    """
    try:
        import psutil
    except ImportError:
        return 0
    my_pids = {os.getpid()}
    try:
        ppid = psutil.Process(os.getpid()).ppid()
        if ppid:
            my_pids.add(ppid)
    except Exception:
        pass

    killed = 0
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            name = (p.info.get('name') or '').lower()
            pid = p.info.get('pid')
            if pid in my_pids:
                continue
            is_toolbox = 'xiaoyutoolbox' in name
            if not is_toolbox and name.startswith('python'):
                cmdline = " ".join(p.info.get('cmdline') or []).lower()
                is_toolbox = 'toolbox_main' in cmdline
            if is_toolbox:
                proc = psutil.Process(pid)
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except Exception:
                        pass
                proc.kill()
                killed += 1
        except Exception:
            pass
    return killed


def _open_in_browser_fallback(url=None):
    """降级：pywebview 不可用时用 Chrome --app（历史方案）。"""
    url = url or f"http://127.0.0.1:{STATS_PORT}/"
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe")
    ]
    for b in candidates:
        if os.path.exists(b):
            try:
                subprocess.Popen([
                    b,
                    f"--app={url}",
                    "--window-size=1380,880"
                ])
                return True
            except Exception as ex:
                print(f"[app] 启动独立应用视窗异常: {ex}", file=sys.stderr)

    try:
        os.startfile(url)
    except Exception:
        webbrowser.open(url)
    return True

user32 = ctypes.windll.user32

# 统计仪表盘端口（与 stats_server.PORT 一致）
STATS_PORT = 8790


def _auto_direction(text):
    """自动判翻译方向：含 CJK/假名/谚文 → 译英，否则译中。"""
    for ch in text:
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF
                or 0xAC00 <= o <= 0xD7AF):
            return "en"
    return "zh"

# 热键 ID
HK_ZH = 9001
HK_EN = 9002
HK_OCR = 9003
HK_CLIP = 9004

# 各热键注册结果（stats_server /api/hotkeys 读这里反馈给前端）
HOTKEY_STATUS = {"zh": False, "en": False, "ocr": False, "clip": False}
_hotkey_reload_evt = threading.Event()  # 改键后由 HTTP 线程置位，热键线程重注册


def request_hotkey_reload():
    """线程安全：请求热键线程重新注册全部热键（改键保存后调用）。"""
    _hotkey_reload_evt.set()


def _open_clipboard_page():
    """热键动作：唤起工作台并直达剪贴板历史页。"""
    if not _WEBVIEW_OK:
        _open_in_browser_fallback(f"http://127.0.0.1:{STATS_PORT}/#/clipboard")
        return
    request_window_show()

    def _jump():
        # 窗口首次创建需数秒：重试直到 WebView 就绪可执行 JS
        for _ in range(16):
            w = _win_obj
            if w is not None:
                try:
                    w.evaluate_js("if(location.hash!=='#/clipboard')"
                                  "location.hash='#/clipboard';void 0")
                    return
                except Exception:       # noqa: BLE001
                    pass
            time.sleep(0.5)
    threading.Thread(target=_jump, daemon=True).start()

# ctypes.wintypes 没有 LPARAM/LRESULT 语义缺口，热键注册用原生即可
user32.RegisterHotKey.restype = ctypes.c_int
user32.UnregisterHotKey.restype = ctypes.c_int
# PeekMessageW 显式声明，避免默认 int 截断
user32.PeekMessageW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]


class RegionSelector:
    """全屏半透明框选器：拖出矩形后返回 (x, y, w, h)，ESC 取消。

    持久 UI 线程 + 请求队列（避免反复新建 Tk 导致跨线程 GC 崩溃）。
    """

    def __init__(self):
        self._req = queue.Queue()
        self._res = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        root = tk.Tk()
        root.withdraw()
        while True:
            item = self._req.get()
            if item is None:
                break
            try:
                res = self._select_on(root)
            except Exception as e:
                print("框选失败:", e, file=sys.stderr)
                res = None
            self._res.put(res)

    def _select_on(self, root):
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.35)
        except Exception:
            pass
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{sw}x{sh}+0+0")
        win.configure(bg="black")
        canvas = tk.Canvas(win, bg="black", highlightthickness=0,
                           cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        state = {"result": None, "rect": None, "start": (0, 0)}

        def on_down(e):
            state["start"] = (e.x, e.y)
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                    outline="red", width=2)

        def on_drag(e):
            if state["rect"]:
                x0, y0 = state["start"]
                canvas.coords(state["rect"], x0, y0, e.x, e.y)

        def on_up(e):
            x0, y0 = state["start"]
            x0, y0 = min(x0, e.x), min(y0, e.y)
            x1, y1 = max(state["start"][0], e.x), max(state["start"][1], e.y)
            if x1 - x0 > 8 and y1 - y0 > 8:
                state["result"] = (x0, y0, x1 - x0, y1 - y0)
            win.destroy()

        def on_esc(e):
            state["result"] = None
            win.destroy()

        canvas.bind("<ButtonPress-1>", on_down)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_up)
        win.bind("<Escape>", on_esc)
        win.focus_force()
        win.wait_window()
        return state["result"]

    def select(self, timeout=60):
        self._req.put(True)
        try:
            return self._res.get(timeout=timeout)
        except queue.Empty:
            return None


class ToolboxApp:
    def __init__(self):
        self.cfg = load_config()
        # 默认补上 OCR / 剪贴板热键（老配置升级）
        changed = False
        if "hotkey_ocr" not in self.cfg:
            self.cfg["hotkey_ocr"] = "ctrl+alt+s"
            changed = True
        if "hotkey_clip" not in self.cfg:
            self.cfg["hotkey_clip"] = "ctrl+alt+v"
            changed = True
        if changed:
            save_config(self.cfg)
        self.proc = None
        self.popup = TransPopup(self.cfg, host=HOST, port=PORT)
        self.tray = None
        self._selector = None      # 框选器复用（每次新建会泄漏 Tk 解释器）
        self._want_exit = False
        self._backend_state = "stopped"  # stopped / starting / running / error
        self._selection_busy = False     # 划词取词正借用剪贴板（监听让路）
        self._clip_watch_stop = threading.Event()   # translate 模块卸载时置位
        self._clip_watch_thread = None

    # -------------------------------------------------- 后端
    def ensure_backend(self, force=False):
        """拉起本地翻译后端。

        force=False（默认）走「启动时自启」路径，受 auto_start_backend 门禁；
        force=True 用于用户显式动作（托盘启停、引擎=local 的划词）——
        否则把 auto_start_backend 关掉会把手动启动一起挡死。
        """
        if (force or self.cfg.get("auto_start_backend", False)) and not port_busy():
            self._backend_state = "starting"
            try:
                self.proc = start_server(
                    model=self.cfg["model"], ngl=self.cfg["ngl"],
                    threads=self.cfg["threads"], ctx=self.cfg["ctx"])
                if wait_ready():
                    self._backend_state = "running"
                else:
                    self._backend_state = "error"
            except Exception as e:
                print("后端启动失败:", e, file=sys.stderr)
                self._backend_state = "error"
        elif port_busy():
            self._backend_state = "running"

    def toggle_backend(self):
        if port_busy():
            self._kill_by_port()
            self._backend_state = "stopped"
        else:
            threading.Thread(target=self.ensure_backend,
                             kwargs={"force": True}, daemon=True).start()

    def _kill_by_port(self):
        # CREATE_NO_WINDOW: windowed exe 下 netstat/taskkill 会闪控制台黑框
        no_win = 0x08000000
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=8,
                creationflags=no_win)
            for line in out.stdout.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=8,
                                   creationflags=no_win)
        except Exception as e:
            print("杀进程失败:", e, file=sys.stderr)

    # -------------------------------------------------- 热键
    def register_hotkeys(self):
        """按模块安装表注册热键：translate 卸载不注册划词/OCR，clipboard 卸载不注册唤起。

        RegisterHotKey 线程绑定——本方法只能在托盘消息循环线程调用
        （启动时或 _hotkey_reload_evt 触发的热重载分支）。
        """
        try:
            from module_runtime import module_enabled
            trans_on = module_enabled("translate")
            clip_on = module_enabled("clipboard")
        except Exception:                   # noqa: BLE001
            trans_on = clip_on = True       # module_runtime 异常时不挡热键
        m1, v1 = parse_hotkey(self.cfg["hotkey_zh"])
        m2, v2 = parse_hotkey(self.cfg["hotkey_en"])
        m3, v3 = parse_hotkey(self.cfg.get("hotkey_ocr", "ctrl+alt+s"))
        m4, v4 = parse_hotkey(self.cfg.get("hotkey_clip", "ctrl+alt+v"))
        ok1 = trans_on and user32.RegisterHotKey(0, HK_ZH, m1 | 0x4000, v1)
        ok2 = trans_on and user32.RegisterHotKey(0, HK_EN, m2 | 0x4000, v2)
        ok3 = trans_on and user32.RegisterHotKey(0, HK_OCR, m3 | 0x4000, v3)
        ok4 = clip_on and user32.RegisterHotKey(0, HK_CLIP, m4 | 0x4000, v4)
        HOTKEY_STATUS.update({"zh": bool(ok1), "en": bool(ok2),
                              "ocr": bool(ok3), "clip": bool(ok4)})
        return ok1, ok2, ok3, ok4

    def unregister_hotkeys(self):
        for hk in (HK_ZH, HK_EN, HK_OCR, HK_CLIP):
            user32.UnregisterHotKey(0, hk)

    # -------------------------------------------------- 翻译动作
    def _backend_or_start(self):
        # 只有引擎=local 才需要 llama-server；cloud/api 引擎不该把它拉起来
        try:
            from translate_backend import load_trans_engine_config
            eng = load_trans_engine_config().get("trans_engine", "cloud")
        except Exception:
            eng = "cloud"
        if eng != "local":
            return True
        if not port_busy():
            self._toast("后端未启动，正在拉起…")
            self.ensure_backend(force=True)
            if not port_busy():
                self._toast("后端启动失败，检查模型/显存")
                return False
        return True

    def do_translate(self, to):
        if not self._backend_or_start():
            return
        # 取词会备份/借道/还原剪贴板，监听循环在这段时间必须让路，
        # 否则模拟 Ctrl+C 写入的选中文本会被当成一次「复制」再次翻译
        self._selection_busy = True
        try:
            text = get_selected_text(self.cfg)
        finally:
            self._selection_busy = False
        if not text:
            print(f"[translate] {to}: no selection", flush=True)
            self._toast("没有选中文字")
            return
        preview = text if len(text) <= 40 else text[:37] + "…"
        print(f"[translate] {to}: got {len(text)} chars: {preview}",
              flush=True)
        self.popup.show(text, to)

    def do_ocr_translate(self, to="zh"):
        """框选屏幕 -> OCR -> 翻译。"""
        if not self._backend_or_start():
            return
        try:
            from ocr import ocr_region
        except ImportError as e:
            self._toast(f"OCR 模块不可用: {e}")
            return
        self._toast("拖选要翻译的屏幕区域（ESC 取消）…")
        # 复用同一个框选器实例（每次新建会泄漏 Tk 解释器）
        if self._selector is None:
            self._selector = RegionSelector()
        region = self._selector.select()
        if not region:
            print("[ocr] cancelled", flush=True)
            return
        x, y, w, h = region
        print(f"[ocr] region: {x},{y} {w}x{h}", flush=True)
        self._toast("识别中…")
        try:
            text, eng = ocr_region(x, y, w, h, lang="auto")
        except Exception as e:
            self._toast(f"OCR 失败: {e}")
            return
        text = (text or "").strip()
        print(f"[ocr] text {len(text)} chars (engine={eng}): "
              f"{text[:40]!r}", flush=True)
        if not text:
            self._toast("没识别出文字")
            return
        self.popup.show(text, to)

    def _toast(self, msg):
        try:
            self.popup._toast(msg)
        except Exception:
            pass

    # -------------------------------------------------- 剪贴板监听（复制即翻）
    def toggle_clip_watch(self):
        self.cfg["clipboard_watch"] = not self.cfg.get("clipboard_watch", False)
        save_config(self.cfg)
        if self.cfg["clipboard_watch"]:
            self.set_clip_watch_running(True)   # 线程可能因模块联动已退出，重建
        self._toast("剪贴板监听已开启：复制任意文字即自动翻译"
                    if self.cfg["clipboard_watch"] else "剪贴板监听已关闭")

    def _clipboard_watch_loop(self):
        """复制即翻译（交互模式借鉴 CopyTranslator，见 README 致谢）。

        轮询剪贴板序列号，变化且内容有效才弹窗翻译。三重防误触发：
        弹窗自己的「复制」按钮写入不回环（popup._copied_text）；
        划词取词借用期让路（_selection_busy）；同内容 2 秒去抖。
        translate 模块卸载时 _clip_watch_stop 置位，线程真正退出。
        """
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
        last_seq = 0
        dirty = False
        last_text = ""
        last_t = 0.0
        while not self._clip_watch_stop.wait(0.6):
            try:
                seq = user32.GetClipboardSequenceNumber()
            except Exception:
                continue
            if seq and seq != last_seq:
                last_seq = seq
                dirty = True
            if not dirty or not self.cfg.get("clipboard_watch", False):
                continue
            if self._selection_busy:
                continue          # 取词借用期：吞掉这次变化
            dirty = False
            text = ""
            try:
                import win32clipboard as w_clip
                import win32con
                w_clip.OpenClipboard()
                try:
                    text = w_clip.GetClipboardData(win32con.CF_UNICODETEXT) or ""
                finally:
                    w_clip.CloseClipboard()
            except Exception:
                continue          # 剪贴板被其他程序占用，本轮放弃
            text = (text or "").strip()
            if not text or len(text) > self.cfg.get("max_chars", 3000):
                continue
            now = time.monotonic()
            if text == getattr(self.popup, "_copied_text", None):
                continue          # 弹窗自己写入的内容，防回环
            if text == last_text and now - last_t < 2.0:
                continue          # 同内容 2 秒去抖
            last_text, last_t = text, now
            self.popup.show(text, _auto_direction(text))
        print("[tray] 复制即翻监听线程已退出（translate 模块卸载）", flush=True)

    def set_clip_watch_running(self, on):
        """复制即翻线程启停（translate 模块安装/卸载联动）。

        on=True 时仅在用户原 clipboard_watch 配置开启的前提下重建线程，
        不改变用户偏好本身。
        """
        if not on:
            self._clip_watch_stop.set()
            self._clip_watch_thread = None
            return
        if not self.cfg.get("clipboard_watch", False):
            return                # 用户本来就没开复制即翻，不替他打开
        if self._clip_watch_thread and self._clip_watch_thread.is_alive():
            return
        self._clip_watch_stop.clear()
        self._clip_watch_thread = threading.Thread(
            target=self._clipboard_watch_loop, daemon=True)
        self._clip_watch_thread.start()

    # -------------------------------------------------- 主循环
    def _register_module_actions(self):
        """把进程内启停动作注册进 module_runtime（须在 start_in_thread 之前）。"""
        import module_runtime
        module_runtime.register_actions(
            "translate",
            stop_fns=(
                lambda: self.set_clip_watch_running(False),
                lambda: self._kill_by_port(),       # 8899 无论由谁拉起都按端口杀
                request_hotkey_reload,
            ),
            start_fns=(
                lambda: self.set_clip_watch_running(True),
                request_hotkey_reload,              # 8899 保持按需门禁，不自动拉
            ))
        module_runtime.register_actions(
            "clipboard",
            stop_fns=(request_hotkey_reload,),      # HK_CLIP 随 reload 卸下
            start_fns=(request_hotkey_reload,))
        # 游戏专区：注册 Epic 周免领取窗口回调（调度/页面按钮经 epic_claimer 触发）
        epic_claimer.set_claim_window_opener(_open_claim_window)

    def run(self):
        self._register_module_actions()
        try:
            from module_runtime import module_enabled
            trans_installed = module_enabled("translate")
        except Exception:                       # noqa: BLE001
            trans_installed = True
        if trans_installed:
            threading.Thread(target=self.ensure_backend, daemon=True).start()
        else:
            print("[modules] translate 模块未安装：跳过 8899 自启检查", flush=True)
        # 复制即翻译监听（默认关，托盘菜单开关；translate 卸载时不启动）
        if trans_installed:
            self.set_clip_watch_running(True)
        # 顺带拉起 Token 统计仪表盘服务（8790，供 webui 前端调用真数据）
        try:
            from stats_server import (start_in_thread, set_window_show_callback,
                                      set_trans_popup)
            set_window_show_callback(request_window_show)
            set_trans_popup(self.popup)     # 设置页浮窗大小实时预览用
            start_in_thread()
        except Exception as e:
            print("统计服务启动失败:", e, file=sys.stderr)
        ok1, ok2, ok3, ok4 = self.register_hotkeys()
        print(f"热键注册: ZH={ok1} EN={ok2} OCR={ok3} CLIP={ok4}")
        print(f"  {self.cfg['hotkey_zh']} 选中翻中文")
        print(f"  {self.cfg['hotkey_en']} 选中翻英文")
        print(f"  {self.cfg.get('hotkey_ocr')} 框选OCR翻中文")
        print(f"  {self.cfg.get('hotkey_clip')} 唤起剪贴板历史")

        # 启动时自动呼起原生独立桌面应用视窗
        threading.Thread(target=lambda: (time.sleep(0.8), request_window_show()),
                         daemon=True).start()

        # 真托盘
        self.tray = TrayIcon(
            on_double_click=request_window_show,
            on_status=lambda: self._backend_state if not port_busy()
            else "running",
            on_dashboard=request_window_show,
            on_toggle=lambda: threading.Thread(target=self.toggle_backend,
                                               daemon=True).start(),
            on_clip_toggle=self.toggle_clip_watch,
            on_clip_state=lambda: bool(self.cfg.get("clipboard_watch", False)),
            on_exit=self.request_exit)
        tray_ok = self.tray.start()
        print(f"托盘图标: {'OK' if tray_ok else '创建失败（见上方 [tray] 报错）'}",
              flush=True)

        class MSG(ctypes.Structure):
            # 血泪教训（实测逮到的真 bug）：Windows x64 下 WPARAM/LPARAM 是
            # 8 字节（UINT_PTR/LONG_PTR）。若用 c_ulong（Windows 上仅 4 字节）
            # 结构体会错位——wParam 读到的是 message 后的填充字节（垃圾），
            # 表现为「热键注册成功、按下去毫无反应」：WM_HOTKEY 收到了但
            # 热键 ID 永远匹配不上。必须用 c_size_t / c_ssize_t。
            _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                        ("time", ctypes.c_uint), ("pt_x", ctypes.c_long),
                        ("pt_y", ctypes.c_long)]
        msg = MSG()
        while not self._want_exit:
            if _hotkey_reload_evt.is_set():
                # 改键热重载：Unregister/Register 必须在注册线程内执行
                _hotkey_reload_evt.clear()
                self.unregister_hotkeys()
                ok = self.register_hotkeys()
                print(f"[hotkey] 热重载: ZH={ok[0]} EN={ok[1]} "
                      f"OCR={ok[2]} CLIP={ok[3]}", flush=True)
            if user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
                if msg.message == 0x0312:  # WM_HOTKEY
                    print(f"[hotkey] id={msg.wParam}", flush=True)
                    if msg.wParam == HK_ZH:
                        threading.Thread(target=self.do_translate, args=("zh",),
                                         daemon=True).start()
                    elif msg.wParam == HK_EN:
                        threading.Thread(target=self.do_translate, args=("en",),
                                         daemon=True).start()
                    elif msg.wParam == HK_OCR:
                        threading.Thread(target=self.do_ocr_translate,
                                         daemon=True).start()
                    elif msg.wParam == HK_CLIP:
                        threading.Thread(target=_open_clipboard_page,
                                         daemon=True).start()
            else:
                time.sleep(0.02)
        self.shutdown()

    def request_exit(self):
        print("[window] request_exit 被调用！", flush=True)
        self._want_exit = True
        _win_quit_evt.set()
        w = _claim_win_obj
        if w is not None:
            try:
                w.destroy()             # 领取窗口还开着时先关（webview.start 等全部窗口销毁）
            except Exception:           # noqa: BLE001
                pass
        w = _win_obj
        if w is not None:
            try:
                w.destroy()             # 触发 closing（退出中放行）→ GUI 循环返回
            except Exception:           # noqa: BLE001
                pass

    def shutdown(self):
        self.unregister_hotkeys()
        stop_server(self.proc)
        if self.tray:
            self.tray.stop()


def main():
    # 单实例保护与智能唤起：如果已有实例在运行，尝试多通道唤醒或接管重启
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    _mutex = kernel32.CreateMutexW(None, False, "Global\\XiaoyuToolboxMutex")
    if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        print("小宇工具箱已在后台运行，正在尝试唤起已有工作台...", flush=True)
        # 通道 1：Win32 句柄唤起（同桌面且窗口未销毁时最快）
        win32_ok = _activate_existing_window()
        # 通道 2：HTTP 路由跨进程唤起（通过 8790 端口命令后台主动 show）
        http_ok = _notify_existing_window_show()

        # 稍等片刻检测桌面是否已呈现窗口
        time.sleep(0.4)
        hwnd = user32.FindWindowW(None, _WIN_TITLE)
        if (hwnd and user32.IsWindowVisible(hwnd)) or win32_ok:
            if hwnd:
                user32.SetForegroundWindow(hwnd)
            return

        # 若两个通道都未能呈现可见窗口（后台进程残留卡死或在隔离环境）
        MB_YESNO = 0x00000004
        MB_ICONQUESTION = 0x00000020
        MB_SYSTEMMODAL = 0x00001000
        IDYES = 6
        ans = user32.MessageBoxW(
            0,
            "检测到小宇工具箱后台进程已在运行，但未能在桌面唤起显示窗口。\n\n"
            "这通常是因为程序已最小化至右下角系统托盘，或者后台进程出现残留假死。\n\n"
            "是否强制重启小宇工具箱？\n\n"
            "【是】：自动清理旧进程并启动全新工作台\n"
            "【否】：保留当前后台进程并退出",
            "小宇工具箱 · 运行提示",
            MB_YESNO | MB_ICONQUESTION | MB_SYSTEMMODAL
        )
        if ans == IDYES:
            print("用户选择强制重启，正在清理旧残留进程...", flush=True)
            _kill_other_toolbox_instances()
            time.sleep(0.6)
            if _mutex:
                kernel32.CloseHandle(_mutex)
            _mutex = kernel32.CreateMutexW(None, False, "Global\\XiaoyuToolboxMutex")
        else:
            return
    app = ToolboxApp()
    # 托盘 + 全局热键 + Win32 消息循环 → 后台线程
    # 主线程专属：pywebview 原生窗口 GUI 循环（6.x 强制主线程）
    bg = threading.Thread(target=app.run, daemon=True)
    bg.start()
    try:
        _win_loop()
    finally:
        print("[window] main() 进入 finally 清理", flush=True)
        _win_quit_evt.set()
        _win_open_evt.set()
        bg.join(timeout=3)                  # 给消息循环线程时间收尾（注销热键/停后端/删托盘）
    os._exit(0)


if __name__ == "__main__":
    main()
