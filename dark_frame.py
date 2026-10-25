# -*- coding: utf-8 -*-
"""原生窗口暗色边框注入（2026-09-20）。

问题：pywebview 壳窗口的标题栏/边框由 Windows DWM 绘制，跟随**系统主题**
（AppsUseLightTheme），与网页内 CSS 的暗/亮主题无关——系统亮色时网页切暗色，
边框依旧白。
修法：对窗口 HWND 调 DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE)。
属性值分代：build 19041+ 用 20，更早预发布版是 19——两个都试，取先成功者。
偏好持久化在 exe 同目录 window_theme.txt（dark/light），默认 dark（页面默认主题）。
"""
import ctypes
import os
import sys

user32 = None
dwmapi = None
try:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except Exception:                                   # 非 Windows 兜底
    pass

# DWMWA_USE_IMMERSIVE_DARK_MODE：正式版=20，19041 前预发布=19
_ATTRS = (20, 19)

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PREF_FILE = os.path.join(BASE_DIR, "window_theme.txt")


def get_pref():
    """当前期望的边框主题：dark / light（默认 dark，与 webui 默认主题一致）。"""
    try:
        with open(PREF_FILE, "r", encoding="ascii") as f:
            v = f.read().strip().lower()
            return v if v in ("dark", "light") else "dark"
    except OSError:
        return "dark"


def set_pref(mode):
    if mode not in ("dark", "light"):
        return False
    try:
        with open(PREF_FILE, "w", encoding="ascii") as f:
            f.write(mode)
        return True
    except OSError:
        return False


def apply_to_hwnd(hwnd, enable):
    """对单个 HWND 开关暗色标题栏；返回是否成功。"""
    if not (hwnd and dwmapi):
        return False
    val = ctypes.c_int(1 if enable else 0)
    for attr in _ATTRS:
        hr = ctypes.c_long(0)
        try:
            res = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(hwnd)), ctypes.c_uint(attr),
                ctypes.byref(val), ctypes.sizeof(val))
            hr.value = res
        except Exception:
            continue
        if hr.value == 0:
            return True
    return False


def apply_to_title(title):
    """按窗口标题找原生窗口并套用持久化偏好（找不到窗口返回 False）。"""
    if not user32:
        return False
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    return apply_to_hwnd(hwnd, get_pref() == "dark")
