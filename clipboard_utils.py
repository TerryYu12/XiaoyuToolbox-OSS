"""公共剪贴板工具：零依赖的 64 位 ctypes 原生剪贴板直写。

供剪贴板历史与 Agent 美化等模块共用的独立实现（开源版解耦产物，
见 docs/superpowers/plans/2026-10-25-opensource-edition-plan.md §2.1）。
"""

import ctypes
from ctypes import wintypes


def copy_text_to_clipboard(text):
    """任意文本写入系统剪贴板（ctypes 直写，前端剪贴板 API 被拒时的兜底）。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "内容为空"}

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    # 64 位进程必须显式声明指针类型，否则 GlobalLock 返回值被截断为 32 位 int（NULL）
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u32.EmptyClipboard.argtypes = []
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    if not u32.OpenClipboard(None):
        return {"ok": False, "error": "剪贴板被其他程序占用"}
    try:
        u32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return {"ok": False, "error": "内存分配失败"}
        p = k32.GlobalLock(h)
        if not p:
            return {"ok": False, "error": "剪贴板内存锁定失败"}
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(CF_UNICODETEXT, h):
            k32.GlobalFree(h)
            return {"ok": False, "error": "写入剪贴板失败"}
        return {"ok": True, "hint": "已复制到系统剪贴板"}
    finally:
        u32.CloseClipboard()
