# -*- coding: utf-8 -*-
"""剪贴板历史：后台线程轮询系统剪贴板，自动记录最近复制的文本。

设计要点：
- 轮询（0.6s）而非 AddClipboardFormatListener：监听器需要消息窗口，与托盘
  线程的消息循环纠缠风险大；轮询纯标准库 + ctypes，延迟对历史记录无感。
- 只记录 CF_UNICODETEXT 文本；与最近一条相同跳过（兼防抖），与更早条目重复
  则 MRU 置顶并更新时间戳。
- 「复制回剪贴板」成功后该条 MRU 置顶，监听线程随后读到相同内容会命中
  「与最近一条相同」去重——自身写入天然不入历史，无需抑制标记。
- 落盘 exe 同目录 clipboard_cache.json（frozen 时 __file__ 指向 _MEIPASS，
  禁止用它拼数据路径，见交接文档 §12.2）。
"""
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

MAX_ITEMS = 100          # 历史条数上限（需求 50~100，取上限）
MAX_CHARS = 20000        # 单条字符上限，超出截断存储（防大文本撑爆内存/文件）
POLL_INTERVAL = 0.6      # 轮询间隔（秒）

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
STORE = BASE_DIR / "clipboard_cache.json"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_lock = threading.RLock()
_items = []              # 新条目在前：[{id, text, ts, chars, truncated}]
_enabled = True
_next_id = 1
_watcher_started = False
_stop_event = threading.Event()   # 模块卸载时置位：监听线程真正退出（非暂停）
# 删除/清空时内容往往仍驻留系统剪贴板，若不抑制会被监听当作新复制补录回来。
# 只抑制「与驻留内容相同」的读取；一旦剪贴板出现别的内容即解除（之后真实复制
# 该内容仍会正常记录）。
_skip_text = None

# Win32 声明（64 位进程句柄必须显式 restype=c_void_p，见交接文档 §14.6）
_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32
_u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_u32.OpenClipboard.argtypes = [wintypes.HWND]
_u32.OpenClipboard.restype = wintypes.BOOL
_u32.GetClipboardData.restype = ctypes.c_void_p
_u32.GetClipboardData.argtypes = [wintypes.UINT]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalLock.argtypes = [ctypes.c_void_p]
_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def _read_clipboard_text():
    """读当前剪贴板文本（只读，不清空）。被占用 / 非文本返回 None。"""
    try:
        if not _u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        if not _u32.OpenClipboard(None):
            return None                 # 被其他程序占用，跳过本轮
        try:
            h = _u32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            p = _k32.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                _k32.GlobalUnlock(h)
        finally:
            _u32.CloseClipboard()
    except Exception as e:              # noqa: BLE001
        print(f"[clipboard] 读取异常: {e}", file=sys.stderr)
        return None


def _save():
    """原子落盘（tmp + os.replace）。持锁调用。"""
    try:
        data = {
            "version": 1,
            "enabled": _enabled,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": _items,
        }
        tmp = str(STORE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STORE)
    except Exception as e:              # noqa: BLE001
        print(f"[clipboard] 落盘失败: {e}", file=sys.stderr)


def _load():
    global _items, _enabled, _next_id
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
        items = [it for it in (data.get("items") or [])
                 if isinstance(it, dict) and it.get("text")]
        items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        _items = items[:MAX_ITEMS]
        _enabled = bool(data.get("enabled", True))
        _next_id = max([int(it.get("id") or 0) for it in _items] + [0]) + 1
    except FileNotFoundError:
        pass
    except Exception as e:              # noqa: BLE001
        print(f"[clipboard] 缓存读取失败: {e}", file=sys.stderr)


def add_item(text, ts=None):
    """记录一条复制内容（去重 + MRU + 上限 + 落盘）。返回是否产生变化。

    含删除驻留抑制：被删除/清空后仍驻留剪贴板的内容不再入历史，
    直到剪贴板出现别的内容为止（见 _skip_text）。
    """
    global _next_id, _skip_text
    text = text or ""
    if not text.strip():
        return False
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]
    with _lock:
        if _skip_text is not None:
            if text == _skip_text:
                return False            # 删除后的驻留内容：抑制补录
            _skip_text = None           # 剪贴板已换成新内容，解除抑制
        if _items and _items[0]["text"] == text:
            return False                # 与最近一条相同：跳过（兼防抖）
        for i, it in enumerate(_items):
            if it["text"] == text:      # 与更早条目重复：MRU 置顶
                it["ts"] = ts or time.time()
                _items.insert(0, _items.pop(i))
                _save()
                return True
        _items.insert(0, {"id": _next_id, "text": text,
                          "ts": ts or time.time(), "chars": len(text),
                          "truncated": truncated})
        _next_id += 1
        del _items[MAX_ITEMS:]
        _save()
        return True


def _watch_loop():
    while not _stop_event.wait(POLL_INTERVAL):
        try:
            with _lock:
                enabled = _enabled
            if not enabled:
                continue
            text = _read_clipboard_text()
            if text:
                add_item(text)          # 抑制/去重/MRU 均在 add_item 内处理
        except Exception as e:          # noqa: BLE001
            print(f"[clipboard] 监听异常: {e}", file=sys.stderr)
    print("[clipboard] 监听线程已退出（模块卸载）", flush=True)


def start_watcher():
    """启动监听线程（幂等）。由 stats_server.start_in_thread() / 模块安装拉起。"""
    global _watcher_started
    with _lock:
        if _watcher_started:
            return
        _watcher_started = True
        _stop_event.clear()
        _load()
    threading.Thread(target=_watch_loop, daemon=True,
                     name="clipboard-watch").start()
    print(f"[clipboard] 剪贴板监听已启动（上限 {MAX_ITEMS} 条，"
          f"存储 {STORE}）", flush=True)


def stop_watcher():
    """停止监听线程（模块卸载）。线程退出后 start_watcher 可重新拉起。"""
    global _watcher_started
    with _lock:
        if not _watcher_started:
            return
        _watcher_started = False
    _stop_event.set()


# -------------------------------------------------- REST API 用

def get_state():
    """全量历史 + 监听状态（100 条上限下直接全量返回，前端本地过滤）。"""
    with _lock:
        return {"ok": True, "enabled": _enabled, "total": len(_items),
                "max_items": MAX_ITEMS, "items": [dict(it) for it in _items]}


def copy_item(item_id):
    """把历史条目写回系统剪贴板；成功后 MRU 置顶。"""
    with _lock:
        it = next((x for x in _items if x["id"] == item_id), None)
        text = it["text"] if it else None
    if text is None:
        return {"ok": False, "error": "条目不存在或已被清理"}
    from clipboard_utils import copy_text_to_clipboard
    ret = copy_text_to_clipboard(text)
    if ret.get("ok"):
        with _lock:
            for i, x in enumerate(_items):
                if x["id"] == item_id:
                    x["ts"] = time.time()
                    _items.insert(0, _items.pop(i))
                    break
            _save()
        ret["items"] = get_state()["items"]
    return ret


def delete_item(item_id):
    global _skip_text
    with _lock:
        hit = next((x for x in _items if x["id"] == item_id), None)
        if not hit:
            return {"ok": True, "deleted": False}
        _items[:] = [x for x in _items if x["id"] != item_id]
        # 被删内容正是当前剪贴板驻留内容时抑制补录，直到剪贴板换新内容
        cur = _read_clipboard_text()
        if cur is not None and cur == hit["text"]:
            _skip_text = hit["text"]
        _save()
        return {"ok": True, "deleted": True}


def clear_items():
    global _skip_text
    with _lock:
        cur = _read_clipboard_text()
        if cur is not None and cur.strip():
            _skip_text = cur
        _items.clear()
        _save()
    return {"ok": True, "hint": "已清空剪贴板历史"}


def set_enabled(on):
    global _enabled
    with _lock:
        _enabled = bool(on)
        _save()
    return {"ok": True, "enabled": _enabled,
            "hint": "监听已开启" if _enabled else "监听已暂停（历史保留）"}
