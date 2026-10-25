# -*- coding: utf-8 -*-
"""系统托盘图标（纯 ctypes，零第三方依赖）。

Shell_NotifyIconW + 自绘 16x16 图标 + 隐藏消息窗口接收托盘事件。
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

# Windows API
shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# 句柄类参数必须显式声明 64 位，否则默认 int 截断 →
# CreateWindowExW 的 hInstance 会 OverflowError（实测：打包后 ASLR 地址变高就崩，
# 表现=托盘图标偶发创建失败）
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.RegisterClassW.restype = ctypes.c_ushort
user32.RegisterClassW.argtypes = [ctypes.c_void_p]

# 菜单/窗口句柄一律按 64 位声明（同上面 CreateWindowExW 的理由：默认 c_int 会
# 截断句柄；winuser.h 里 HMENU/HWND 都是指针宽度）
user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.AppendMenuW.restype = ctypes.c_int
user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_wchar_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                  ctypes.c_void_p]
user32.DestroyMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.DestroyWindow.restype = ctypes.c_int
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.PostMessageW.restype = ctypes.c_int
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_ssize_t]
user32.SetForegroundWindow.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_ICON = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_TIP = 0x00000004

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1        # 托盘回调消息
WM_TASKBAR_CREATED = user32.RegisterWindowMessageW("TaskbarCreated")

WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_NULL = 0x0000

# 弹出菜单标志（winuser.h）。TPM_RETURNCMD 是本文件的坑源：
# 打开它，系统**不会**为选中项补发 WM_COMMAND，选中结果只能从函数返回值取。
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020    # 菜单底边贴光标 → 托盘在屏幕底部时向上弹
TPM_VERTICAL = 0x0040
TPM_RETURNCMD = 0x0100

IDI_TRAY = 1001
IDM_STATUS = 2001
IDM_TOGGLE = 2002
IDM_EXIT = 2003
IDM_DASH = 2004
IDM_CLIP = 2005

# 托盘上说的“服务”一直是本地翻译后端（llama-server，8899），但菜单里只写
# “服务”，跟常驻的界面服务 8790 混淆——2026-09-22 起统一按名字叫。
# 状态文案与托盘菜单、状态气泡共用这一张表（on_status 的返回值做 key）。
MODEL_STATE_TEXT = {"running": "运行中", "stopped": "已停止",
                    "starting": "启动中…", "error": "启动失败"}


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD), ("pt", POINT),
    ]


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", wintypes.INT), ("cbWndExtra", wintypes.INT),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


def make_tray_icon(color_bgr=0x26A69A):
    """自绘 16x16 图标：深色底 + 白色 T 笔画（翻译感）。"""
    # FillRect 在 user32（不是 gdi32），且必须显式声明签名
    user32.FillRect.restype = ctypes.c_int
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.HBRUSH]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreatePen.restype = wintypes.HPEN
    gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
    gdi32.MoveToEx.restype = wintypes.BOOL
    gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi32.LineTo.restype = wintypes.BOOL
    gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.CreateBitmap.restype = wintypes.HBITMAP
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                    wintypes.UINT, ctypes.c_void_p]
    user32.CreateIconIndirect.restype = wintypes.HICON
    user32.CreateIconIndirect.argtypes = [ctypes.c_void_p]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, 16, 16)
    old = gdi32.SelectObject(memdc, bmp)
    brush = gdi32.CreateSolidBrush(color_bgr)
    rc = RECT(0, 0, 16, 16)
    user32.FillRect(memdc, ctypes.byref(rc), brush)
    gdi32.DeleteObject(brush)
    # 白色 T
    pen = gdi32.CreatePen(0, 2, 0xFFFFFF)
    oldpen = gdi32.SelectObject(memdc, pen)
    gdi32.MoveToEx(memdc, 3, 4, None)
    gdi32.LineTo(memdc, 12, 4)
    gdi32.MoveToEx(memdc, 7, 4, None)
    gdi32.LineTo(memdc, 7, 12)
    gdi32.SelectObject(memdc, oldpen)
    gdi32.DeleteObject(pen)
    gdi32.SelectObject(memdc, old)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, hdc)
    # mask：单色全 0（即整图不透明区域=彩色位）
    maskdc = gdi32.CreateCompatibleDC(hdc)
    maskbmp = gdi32.CreateBitmap(16, 16, 1, 1, None)
    gdi32.SelectObject(maskdc, maskbmp)
    gdi32.DeleteDC(maskdc)
    ii = ICONINFO(True, 0, 0, maskbmp, bmp)
    hicon = user32.CreateIconIndirect(ctypes.byref(ii))
    return hicon


# 窗口过程（ctypes 回调）
TRAY_WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LONG, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
# DefWindowProc 签名必须声明，否则 LPARAM 当 Python int 传会 OverflowError
# wintypes 没有 LRESULT，LPARAM/LRESULT 在 64 位下是 c_long
user32.DefWindowProcW.restype = ctypes.c_long
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]


class TrayIcon:
    """托盘图标。回调在独立线程的消息循环里执行。"""

    def __init__(self, on_double_click=None, on_status=None, on_exit=None,
                 on_dashboard=None, on_toggle=None,
                 on_clip_toggle=None, on_clip_state=None,
                 tooltip="小宇工具箱 · 开发者工作台"):
        self.on_double_click = on_double_click or (lambda: None)
        self.on_status = on_status or (lambda: "running")
        self.on_exit = on_exit or (lambda: None)
        self.on_dashboard = on_dashboard or (lambda: None)
        self.on_toggle = on_toggle or (lambda: None)
        self.on_clip_toggle = on_clip_toggle or (lambda: None)
        self.on_clip_state = on_clip_state or (lambda: False)
        self.tooltip = tooltip
        self.hwnd = None
        self.hicon = None
        self.failed = False
        self._wndproc = None  # 必须持有引用，否则 GC 崩溃
        self._thread = None
        self._ready = threading.Event()

    def _load_icon(self):
        """优先加载应用真实图标 qwen_toolbox.ico，失败则降级到微靛蓝自绘图标。"""
        candidates = [
            os.path.join(getattr(sys, '_MEIPASS', ''), 'qwen_toolbox.ico'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qwen_toolbox.ico'),
            os.path.abspath('qwen_toolbox.ico')
        ]
        for p in candidates:
            if p and os.path.exists(p):
                try:
                    h = user32.LoadImageW(None, p, 1, 16, 16, 0x00000010)
                    if h:
                        return h
                except Exception:
                    pass
        return make_tray_icon(0xd26a5e)

    def start(self):
        """启动托盘线程；返回 True=托盘图标创建成功。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        return not self.failed

    def _run(self):
        try:
            self._run_inner()
        except Exception as e:
            self.failed = True
            print(f"[tray] 创建失败: {e}", flush=True)
        finally:
            self._ready.set()

    def _run_inner(self):
        wc = WNDCLASS()
        proc = TRAY_WNDPROC(self._wnd_proc)
        self._wndproc = proc  # 保引用防 GC
        wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "XiaoyuTrayWnd"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if atom == 0 and kernel32.GetLastError() != 1410:  # 1410 = ERROR_CLASS_ALREADY_EXISTS
            self.failed = True
            return
        self.hwnd = user32.CreateWindowExW(
            0, "XiaoyuTrayWnd", "小宇工具箱", 0, 0, 0, 0, 0, 0, 0,
            wc.hInstance, 0)
        if not self.hwnd:
            self.failed = True
            return
        self.hicon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = IDI_TRAY
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP | 0x00000010  # NIF_INFO 弹出系统气泡通知
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self.hicon
        nid.szTip = self.tooltip[:127]
        nid.szInfoTitle = "小宇工具箱已启动"
        nid.szInfo = "小宇工具箱已在后台常驻 (若托盘未见请点击任务栏 ^ 折叠区)"
        nid.dwInfoFlags = 0x00000001  # NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._ready.set()
        # 消息循环
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        # 收尾
        nid.uFlags = 0
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        if self.hicon:
            user32.DestroyIcon(self.hicon)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            mouse = lparam & 0xFFFF
            if mouse == WM_LBUTTONDBLCLK:
                self.on_double_click()
            elif mouse == WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == IDM_STATUS:
                self._show_status_balloon(hwnd)
            elif cmd == IDM_DASH:
                self.on_dashboard()
            elif cmd == IDM_TOGGLE:
                self.on_toggle()        # 启停本地翻译模型，不是打开窗口
            elif cmd == IDM_CLIP:
                self.on_clip_toggle()   # 剪贴板监听翻译（复制即翻）开关
            elif cmd == IDM_EXIT:
                self._quit(hwnd)
            return 0
        if msg == WM_TASKBAR_CREATED:
            # 资源管理器重启后托盘会丢，重建
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = hwnd
            nid.uID = IDI_TRAY
            nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = self.hicon
            nid.szTip = self.tooltip[:127]
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd):
        menu = user32.CreatePopupMenu()
        state = self.on_status()
        user32.AppendMenuW(menu, 0x00000000, IDM_STATUS,
                           f"本地翻译模型: {MODEL_STATE_TEXT.get(state, state)}")
        user32.AppendMenuW(menu, 0x00000000, IDM_DASH, "打开小宇工作台 (桌面视窗)")
        # 动作词按“8899 是否在监听”写：on_status 只在端口忙时返回 running，
        # 所以 stopped/starting/error 三种都该显示「启动」（toggle_backend
        # 的判据也是 port_busy()，两边不脱节）
        user32.AppendMenuW(menu, 0x00000000, IDM_TOGGLE,
                           "关闭本地翻译模型" if state == "running"
                           else "启动本地翻译模型")
        clip_on = bool(self.on_clip_state())
        user32.AppendMenuW(menu, 0x00000000, IDM_CLIP,
                           "关闭剪贴板监听翻译（复制即翻）" if clip_on
                           else "开启剪贴板监听翻译（复制即翻）")
        user32.AppendMenuW(menu, 0x00000800, 0, None)  # 分隔线
        user32.AppendMenuW(menu, 0x00000000, IDM_EXIT, "退出小宇工具箱")
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 必须在前台，否则菜单不消失
        user32.SetForegroundWindow(hwnd)
        # TPM_RETURNCMD：系统不会补发 WM_COMMAND，选中项只在返回值里。
        # 旧代码把返回值丢了 → _wnd_proc 的 WM_COMMAND 分支拿不到任何消息，
        # 四个菜单项（状态/工作台/开关服务/退出）全部变成死键，实测右键点
        # “退出小宇工具箱”毫无反应。这里改为把返回值自己分发给 WM_COMMAND 分支。
        cmd = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_VERTICAL | TPM_RETURNCMD,
            pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        # MSDN 的 TrackPopupMenu 用法要求：菜单消失后补一条 WM_NULL，
        # 否则点菜单外部时可能收不起来
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        if cmd:
            self._wnd_proc(hwnd, WM_COMMAND, cmd, 0)

    def _show_status_balloon(self, hwnd):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = hwnd
        nid.uID = IDI_TRAY
        nid.uFlags = 0x00000010  # NIF_INFO
        nid.szInfoTitle = "小宇工具箱"
        state = self.on_status()
        nid.szInfo = (f"本地翻译模型: {MODEL_STATE_TEXT.get(state, state)}\n"
                      "快捷键: Ctrl+Shift+T/E, Ctrl+Alt+S")
        nid.dwInfoFlags = 0x00000001  # NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _quit(self, hwnd):
        self.on_exit()
        user32.DestroyWindow(hwnd)
        user32.PostQuitMessage(0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_QUIT, 0, 0)


if __name__ == "__main__":
    # 冒烟测试：托盘出现 6 秒后自动退
    t = TrayIcon(on_double_click=lambda: print("双击"),
                 on_status=lambda: "running")
    t.start()
    print("托盘已创建，等 6 秒...")
    time.sleep(6)
    t.stop()
    print("OK")
