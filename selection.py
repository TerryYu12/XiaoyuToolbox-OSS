# -*- coding: utf-8 -*-
"""划词翻译主程序：全局热键 + 选中文本抓取 + 翻译弹窗。

用法：双击 启动划词翻译.bat 运行（托盘常驻）。
操作：选中任意文本 -> Ctrl+Alt+T 翻译；Ctrl+Alt+E 英译中。
      双击托盘图标切换后端开/关；右键托盘退出。

选中文本抓取策略（分层降级）：
  1) 主动获取当前焦点控件的选中文本（能拿到就不动剪贴板）
  2) 拿不到 -> 临时备份剪贴板 -> 发 Ctrl+C -> 读剪贴板 -> 还原
"""
import ctypes
import json
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import win32clipboard as w_clip
import win32con

from translate_backend import (PORT, HOST, start_server, stop_server,
                               translate_stream, wait_ready, port_busy)

# Windows 常量
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# 修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
# 本地化 WM_HOTKEY 消息循环
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
# GetAsyncKeyState：高位=当前按下（用于等修饰键松开）
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

def _app_dir():
    """配置所在目录：打包成 exe 后是 exe 同目录，开发时是模块目录。

    坑（实测）：onefile 冻结环境下 __file__ 指向临时解包目录，
    直接用它会在临时目录里读/写一份"默认配置"（热键回到 Ctrl+Alt+T，
    而本机该组合被占用 → 注册失败 ZH=0）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = _app_dir()
CONFIG_PATH = BASE / "config.json"

DEFAULT_CONFIG = {
    "model": "Qwen3.5-4B-Q4_K_M.gguf",
    "ngl": 99,
    "threads": 6,
    "ctx": 4096,
    # 默认用 Ctrl+Shift 系：Ctrl+Alt+T/E 在本机已被其他程序占用（实测注册失败）
    "hotkey_zh": "ctrl+shift+t",   # 选中 -> 翻成中文
    "hotkey_en": "ctrl+shift+e",   # 选中 -> 翻成英文
    "hotkey_ocr": "ctrl+alt+s",    # 框选 OCR -> 翻成中文
    "hotkey_clip": "ctrl+alt+v",   # 唤起工作台 -> 剪贴板历史页
    "auto_start_backend": False,
    "max_chars": 3000,           # 单次翻译上限，防止误选整篇
    "clipboard_watch": False,    # 复制即翻译（借鉴 CopyTranslator，默认关）
    "popup_scale": 1.0,          # 弹窗大小倍率（设置页滑杆可调，实时预览）
    "direction_key": None,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------- 剪贴板
def backup_clipboard():
    """抓取当前剪贴板里所有格式，返回可恢复的快照。"""
    snap = {}
    try:
        w_clip.OpenClipboard()
        fmt = 0
        while True:
            fmt = w_clip.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            try:
                data = w_clip.GetClipboardData(fmt)
                if isinstance(data, (bytes, str)):
                    snap[fmt] = data
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            w_clip.CloseClipboard()
        except Exception:
            pass
    return snap


def restore_clipboard(snap):
    try:
        w_clip.OpenClipboard()
        w_clip.EmptyClipboard()
        for fmt, data in snap.items():
            if isinstance(data, str):
                w_clip.SetClipboardData(win32con.CF_UNICODETEXT, data)
            else:
                # 二进制格式走原始内存注册
                w_clip.SetClipboardData(fmt, data)
    except Exception:
        pass
    finally:
        try:
            w_clip.CloseClipboard()
        except Exception:
            pass


MOD_VKS = (0x11, 0x10, 0x12, 0x5B, 0x5C)   # Ctrl / Shift / Alt / LWin / RWin


def wait_modifiers_released(timeout=1.2):
    """等用户在热键按下后松开修饰键。

    keybd_event 模拟的 Ctrl+C 若叠加着用户仍按住的 Ctrl+Shift，就变成
    Ctrl+Shift+C —— 在 Chromium/Electron 里是"检查元素"不是复制：
    实测症状=「在 Hermes 里选了字，按热键却提示没有选中文字」。
    先等修饰键全部松开，再发 Ctrl+C 就干净了。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in MOD_VKS):
            return True
        time.sleep(0.03)
    return False


def _release_stuck_modifiers():
    """注入 Shift/Alt/Win 抬起事件，清掉用户仍物理按住的修饰键。

    wait_modifiers_released 超时（用户按住热键 1.2s 不松，很常见的按键
    习惯）后直接发 Ctrl+C，物理 Shift 会把注入的 C 叠成 Ctrl+Shift+C：
    实测在 Edge 里表现为剪贴板纹丝不动 + DevTools「检查元素」被意外
    弹出，后续重试还会复制到 Elements 面板里的页面源码。
    注入 KEYUP 后系统键盘状态视为已松开，注入的 C 只与 Ctrl 组合；
    用户随后松手产生的物理 KEYUP 幂等，无感知。
    """
    for vk in (0x10, 0x12, 0x5B, 0x5C):   # Shift / Alt / LWin / RWin
        user32.keybd_event(vk, 0, 0x0002, 0)   # KEYEVENTF_KEYUP


def _clipboard_sequence():
    """剪贴板序号：任何进程写剪贴板（含 Empty）都会 +1。"""
    user32.GetClipboardSequenceNumber.restype = ctypes.c_int
    return user32.GetClipboardSequenceNumber()


def get_selected_text(cfg):
    """获取选中文本：先主动查 UI Automation/控件，拿不到再借道剪贴板。"""
    # 1) 通用优先：直接尝试读取已选中文本（对多数编辑器有效）
    text = try_get_selection_uia()
    if text:
        return text.strip()
    # 2) 借道剪贴板：备份 -> Ctrl+C -> 读 -> 还原
    snap = backup_clipboard()
    before = (snap.get(win32con.CF_UNICODETEXT) or "").strip()
    seq_before = _clipboard_sequence()
    try:
        # 等用户松开热键修饰键；超时也没松就强制注入抬起，
        # 否则模拟 Ctrl+C 叠加物理 Shift 变 Ctrl+Shift+C 复制失败
        wait_modifiers_released()
        _release_stuck_modifiers()
        # Ctrl+C
        user32.keybd_event(0x11, 0, 0, 0)        # VK_CONTROL down
        user32.keybd_event(0x43, 0, 0, 0)        # 'C' down
        time.sleep(0.05)
        user32.keybd_event(0x43, 0, 0x0002, 0)   # 'C' up
        user32.keybd_event(0x11, 0, 0x0002, 0)   # VK_CONTROL up
        for _ in range(40):                      # 等剪贴板更新，最多 ~1s
            time.sleep(0.025)
            try:
                w_clip.OpenClipboard()
                got = w_clip.GetClipboardData(win32con.CF_UNICODETEXT)
                w_clip.CloseClipboard()
                # 以序号判定复制确实发生：无选中时 Ctrl+C 不写剪贴板；
                # 只比内容会把"选中文字恰好==旧剪贴板内容"误判为失败
                if (got and got.strip()
                        and _clipboard_sequence() != seq_before):
                    return got.strip()
            except Exception:
                try:
                    w_clip.CloseClipboard()
                except Exception:
                    pass
        return None
    finally:
        # 无论如何还原剪贴板
        restore_clipboard(snap)


def try_get_selection_uia():
    """尝试通过 UI Automation 取焦点控件的选中文本。多数场景拿不到，返回 None。"""
    try:
        import comtypes  # 未安装时跳过
    except ImportError:
        return None
    # 预留扩展位：comtypes 调 IUIAutomationTextPattern
    return None


# ---------------------------------------------------------------- 热键
def parse_hotkey(s):
    """'ctrl+alt+t' -> (mod, vk)"""
    parts = [p.strip().lower() for p in s.split("+")]
    mod = 0
    vk = 0
    for p in parts:
        if p == "ctrl":
            mod |= MOD_CONTROL
        elif p == "alt":
            mod |= MOD_ALT
        elif p == "shift":
            mod |= MOD_SHIFT
        elif p == "win":
            mod |= MOD_WIN
        else:
            vk = vk_from_key(p)
    return mod, vk


MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

KEY_MAP = {
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46,
    "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C,
    "m": 0x4D, "n": 0x4E, "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52,
    "s": 0x53, "t": 0x54, "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58,
    "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    # 功能键 / 导航键（UI 改键捕获支持，与前端 normalizeHotkeyKey 词表一致）
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "ins": 0x2D, "del": 0x2E, "space": 0x20, "tab": 0x09,
}


def vk_from_key(k):
    if len(k) == 1 and k in KEY_MAP:
        return KEY_MAP[k]
    return KEY_MAP.get(k, 0)
