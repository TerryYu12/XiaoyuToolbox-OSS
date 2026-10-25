# -*- coding: utf-8 -*-
"""权限检测：判断当前进程是否有管理员权限。

用途：系统级垃圾位置（C:\\Windows\\Temp、SoftwareDistribution、Prefetch、
事件日志、ProgramData\\...\\WER 等）的 ACL 拒绝普通用户写入/删除，
硬删只会得到一批 WinError 5。标记 needs_admin 的规则在非管理员环境下
直接不进候选（见 analyzer.analyze 的 admin 过滤），UI 另行提示提权收益。
"""
from __future__ import annotations
import os
import sys

_cached: bool = None


def is_admin() -> bool:
    """当前进程是否以管理员运行。非 Windows 视为 True（无此概念）。"""
    global _cached
    if _cached is not None:
        return _cached
    if os.name != "nt":
        _cached = True
        return _cached
    try:
        import ctypes
        _cached = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        _cached = False
    return _cached


def reset_cache():
    """测试用：清掉进程内缓存。"""
    global _cached
    _cached = None


def elevation_hint(locked_count: int, locked_size: int) -> str:
    """生成给 UI 的提权提示文案。"""
    if is_admin() or not locked_count:
        return ""
    from .scanner import human
    return (f"以管理员身份运行可多清理 {locked_count} 项系统垃圾"
            f"（共 {human(locked_size)}）：Windows 临时文件、更新缓存、"
            f"错误报告等需要管理员权限")


def elevate_restart() -> bool:
    """以管理员身份重新启动当前工具箱/程序（弹出 UAC 提权提示）。"""
    if os.name != "nt":
        return False
    import ctypes
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = ""
    else:
        exe = sys.executable
        args = " ".join([f'"{a}"' for a in sys.argv])
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, args, os.getcwd(), 1
        )
        return ret > 32
    except Exception:
        return False

