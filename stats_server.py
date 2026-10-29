# -*- coding: utf-8 -*-
"""Token 统计后端：静态页面 + JSON API，跑在 127.0.0.1:8790。

零外部依赖（标准库 http.server），只读本地数据文件（Hermes / CCS /
Codex rollout，见 stats_api.py）。
既可作为独立进程运行（python stats_server.py），
也可被 tray_app 在后台线程里拉起（start_in_thread()）。

API:
  GET /api/stats/summary?days=all|1|7|30
  GET /api/stats/agents
  GET /api/stats/models?days=
  GET /api/stats/daily
  GET /api/stats/health
  其它路径 -> webui/ 下的静态文件
"""
import datetime
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.request

import stats_api
import ai_hub
import translate_backend
import dark_frame
import clipboard_history
import clash_node_check
import clash_inspector
import epic_claimer
from diskcleaner import api as cleaner_api
import module_runtime


try:
    import ai_fetcher
except Exception as _af_err:            # noqa: BLE001
    ai_fetcher = None
    print(f"[stats] ai_fetcher 不可用: {_af_err}", file=sys.stderr)

BASE = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"
PORT = 8790

# 原生工作台窗口唤起回调（供 tray_app 注册）
_window_show_callback = None


def set_window_show_callback(cb):
    global _window_show_callback
    _window_show_callback = cb


# 翻译弹窗实例（供 tray_app 注册；浮窗大小设置的实时预览用）
_trans_popup = None


def set_trans_popup(popup):
    global _trans_popup
    _trans_popup = popup


def _find_webui():
    """前端目录解析：exe 同目录优先（方便迭代），否则用打包内置的。"""
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(sys.executable), "webui"))
    else:
        cands.append(os.path.join(BASE, "webui"))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "webui"))
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


WEBUI = _find_webui()

# 仪表盘 pulse 端点的单飞缓存（见 do_GET 里 /api/stats/pulse 的说明）
_ZP_LOCK = threading.Lock()
_ZP_CACHE = {"ts": 0.0, "data": None}

# v-t 图时间筛选端点（/api/stats/rate-series）的单飞缓存：按窗口分别缓存 2s
_VT_LOCK = threading.Lock()
_VT_CACHE = {}          # range_min -> {"ts": float, "data": payload}


# ============================================================
# 仪表盘 · 天气卡（2026-10-03）
# 数据源 Open-Meteo（免 key，本机实测可达）：geocoding 中文名 → 经纬度 →
# forecast 实况 + 4 日预报。按城市缓存 30 分钟；失败结果缓存 2 分钟，
# 防断网时前端每次刷新都打真请求。
# 不做 IP 自动定位：本机常驻代理（Clash），出口 IP 的城市不是真实所在城市。
# ============================================================
_WEATHER_LOCK = threading.Lock()
_WEATHER_CACHE = {}          # city -> {"ts": epoch, "data": payload}
_WEATHER_TTL = 1800          # 成功结果缓存 30 分钟
_WEATHER_ERR_TTL = 120       # 失败结果缓存 2 分钟（防断网轮询打爆上游）
_WMO_TEXT = {
    0: "晴", 1: "大部晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷暴", 96: "雷暴冰雹", 99: "雷暴冰雹",
}


def _http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "XiaoyuToolbox"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _weather_fetch(city):
    from urllib.parse import quote
    geo = _http_get_json(
        "https://geocoding-api.open-meteo.com/v1/search?name="
        + quote(city) + "&count=1&language=zh&format=json", timeout=8)
    results = (geo or {}).get("results") or []
    if not results:
        return {"ok": False, "error": f"找不到城市「{city}」"}
    g = results[0]
    lat, lon = g["latitude"], g["longitude"]
    label = " ".join(x for x in (g.get("admin1"), g.get("name")) if x)
    fc = _http_get_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "is_day,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min"
        "&timezone=auto&forecast_days=4", timeout=8)
    cur = fc.get("current") or {}
    daily = fc.get("daily") or {}
    days = []
    dates = daily.get("time") or []
    for i, d in enumerate(dates[:4]):
        days.append({
            "date": d,
            "code": daily.get("weather_code", [None] * len(dates))[i],
            "max": daily.get("temperature_2m_max", [None] * len(dates))[i],
            "min": daily.get("temperature_2m_min", [None] * len(dates))[i],
        })
    code = cur.get("weather_code")
    return {
        "ok": True,
        "city": city,
        "resolved": label,
        "text": _WMO_TEXT.get(code, "未知"),
        "current": {
            "temp": cur.get("temperature_2m"),
            "feels": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind": cur.get("wind_speed_10m"),
            "is_day": cur.get("is_day", 1),
            "code": code,
        },
        "daily": days,
        "fetched_at": int(time.time()),
    }


def weather_card(city):
    city = (city or "").strip()[:40]
    if not city:
        return {"ok": False, "error": "未设置城市"}
    now = time.time()
    with _WEATHER_LOCK:
        hit = _WEATHER_CACHE.get(city)
        if hit:
            ttl = _WEATHER_ERR_TTL if not hit["data"].get("ok") else _WEATHER_TTL
            if now - hit["ts"] < ttl:
                return dict(hit["data"])
    try:
        data = _weather_fetch(city)
    except Exception as exc:  # noqa: BLE001 — 断网/超时都转成卡片可显示的错误
        data = {"ok": False, "error": f"天气获取失败: {str(exc)[:80]}"}
    with _WEATHER_LOCK:
        _WEATHER_CACHE[city] = {"ts": time.time(), "data": data}
        # 缓存只留最近 8 个城市，防历史城市把表撑大
        if len(_WEATHER_CACHE) > 8:
            for k in sorted(_WEATHER_CACHE, key=lambda k: _WEATHER_CACHE[k]["ts"])[:-8]:
                _WEATHER_CACHE.pop(k, None)
    return data


# ============================================================
# 仪表盘 · 系统监控卡（2026-10-03）
# psutil 已是运行时依赖（requirements.txt），本地只读，2s 轻缓存防多页面
# 轮询叠加。CPU% 用「距上次调用」增量：距上次超 3s 时补一次 250ms 阻塞采样，
# 兼顾首屏（首次调用必为 0）与准确度。
# ============================================================
try:
    import psutil
except Exception as _ps_err:    # noqa: BLE001
    psutil = None
    print(f"[stats] psutil 不可用，系统监控卡停摆: {_ps_err}", file=sys.stderr)

_SYSMON_LOCK = threading.Lock()
_SYSMON_CACHE = {"ts": 0.0, "data": None}
_SYSMON_LAST = {"ts": 0.0, "net": None}
_CPU_NAME = {"v": None}
if psutil:
    try:
        psutil.cpu_percent(interval=None)   # 建立基线，避免首屏恒 0
    except Exception:
        pass


def _cpu_name():
    if _CPU_NAME["v"] is not None:
        return _CPU_NAME["v"]
    name = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            name = (winreg.QueryValueEx(k, "ProcessorNameString")[0] or "").strip()
    except Exception:
        name = ""
    _CPU_NAME["v"] = name
    return name


def _sysmon_collect():
    now = time.time()
    gap = now - _SYSMON_LAST["ts"]
    # CPU：恒用 200ms 阻塞采样。不能用非阻塞增量（interval=None）——桌面窗口
    # 与浏览器页同时在轮询时，psutil 全局采样状态被多个调用方共享，增量窗口
    # 被切碎，两次 collect 间隔极短时恒返回 0。接口有 2s 缓存兜底，代价可忽略。
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    net = psutil.net_io_counters()
    down = up = 0.0
    if _SYSMON_LAST["net"] and gap > 0:
        down = max(0, (net.bytes_recv - _SYSMON_LAST["net"][0]) / gap)
        up = max(0, (net.bytes_sent - _SYSMON_LAST["net"][1]) / gap)
    _SYSMON_LAST.update(ts=now, net=(net.bytes_recv, net.bytes_sent))
    # 磁盘：系统盘 + 数据所在盘（去重）。frozen 下 BASE 在 _MEIPASS 临时解包
    # 目录（通常 C 盘 Temp），数据目录必须按 exe 所在目录算盘符。
    base_dir = (os.path.dirname(sys.executable)
                if getattr(sys, "frozen", False) else BASE)
    data_drive = os.path.splitdrive(base_dir)[0].upper() or "C:"
    drives, seen = [], set()
    for letter in ["C:", data_drive]:
        key = letter.upper()
        if key in seen:
            continue
        seen.add(key)
        try:
            du = psutil.disk_usage(letter + "\\")
            drives.append({"drive": letter, "total": du.total,
                           "used": du.used, "percent": du.percent})
        except Exception:
            pass
        if len(drives) >= 2:
            break
    return {
        "ok": True,
        "cpu": round(cpu, 1),
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "cpu_name": _cpu_name(),
        "mem": {"total": mem.total, "used": mem.total - mem.available,
                "percent": mem.percent},
        "net": {"down_bps": round(down), "up_bps": round(up)},
        "disks": drives,
        "uptime_s": int(now - psutil.boot_time()),
        "ts": int(now),
    }


def sysmon_card():
    with _SYSMON_LOCK:
        if _SYSMON_CACHE["data"] is not None and time.time() - _SYSMON_CACHE["ts"] < 2.0:
            return _SYSMON_CACHE["data"]
        if psutil is None:
            data = {"ok": False, "error": "psutil 不可用"}
        else:
            try:
                data = _sysmon_collect()
            except Exception as exc:  # noqa: BLE001
                data = {"ok": False, "error": f"系统监控失败: {str(exc)[:80]}"}
        _SYSMON_CACHE.update(ts=time.time(), data=data)
        return data


def app_version():
    """程序版本：单一真相是仓库根的 `VERSION`（打包时进 exe）。

    查找顺序刻意是「包内 → exe 同目录 → 仓库根」，与 `_find_webui()` 的「先看 exe 同目录」
    相反。理由：这个字段要回答的是"用户到底跑的哪个构建"，必须以打包进去的那份为准；
    反过来先读 exe 同目录的话，用户目录里一个陈旧的 VERSION 就能让新旧 exe 自报同一个
    版本号——而那正是它要解决的问题。exe 同目录与仓库根只是热更/开发态的兜底。

    背景（2026-09-22）：此前是硬编码 "2.0.0"，而分发包命名已经到 v2.1-20260920，两边脱节，
    导致用户报问题时无法确认他跑的是哪个构建。
    """
    cands = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "VERSION"))
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(sys.executable), "VERSION"))
    cands.append(os.path.join(BASE, "VERSION"))
    for c in cands:
        try:
            with open(c, encoding="utf-8") as f:
                v = (f.read() or "").strip()
            if v:
                return v
        except OSError:
            continue
    return "unknown"


# -------------------------------------------------- 版本与更新检查
_UPDATE_CACHE = {"ts": 0.0, "data": None}
_UPDATE_CACHE_TTL = 3600  # 1 小时缓存
_UPDATE_LOCK = threading.Lock()
GITHUB_RELEASE_API = "https://api.github.com/repos/TerryYu12/XiaoyuToolbox-OSS/releases/latest"


def _parse_version(v_str):
    if not v_str or not isinstance(v_str, str):
        return ()
    s = v_str.strip().lstrip("vV")
    parts = []
    for seg in s.split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            parts.append(int(num))
    return tuple(parts)


def check_app_update(force=False):
    """检查小宇工具箱开源版 GitHub Releases 最新版本。

    带 1 小时内存缓存，避免频繁请求触发 GitHub API 速率限制 (60次/小时)。
    force=True 时强制绕过缓存即时请求。
    """
    now = time.time()
    cur_ver = app_version()
    with _UPDATE_LOCK:
        if not force and _UPDATE_CACHE["data"] is not None:
            if now - _UPDATE_CACHE["ts"] < _UPDATE_CACHE_TTL:
                return _UPDATE_CACHE["data"]

    req_headers = {
        "User-Agent": f"XiaoyuToolbox/{cur_ver}",
        "Accept": "application/vnd.github.v3+json",
    }
    req = urllib.request.Request(GITHUB_RELEASE_API, headers=req_headers)

    proxy_candidates = [None]
    for port in (7890, 7897, 10808, 10809):
        proxy_candidates.append({
            "http": f"http://127.0.0.1:{port}",
            "https": f"http://127.0.0.1:{port}",
        })

    raw_bytes = None
    last_exc = None
    for p in proxy_candidates:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(p)) if p else urllib.request.build_opener()
            with opener.open(req, timeout=4) as resp:
                raw_bytes = resp.read()
                break
        except urllib.error.HTTPError as e:
            last_exc = e
            break
        except Exception as e:
            last_exc = e
            continue

    try:
        if raw_bytes is None:
            if isinstance(last_exc, urllib.error.HTTPError):
                raise last_exc
            raise last_exc or RuntimeError("无法连接到 GitHub 服务")

        data = json.loads(raw_bytes.decode("utf-8"))
        tag_name = data.get("tag_name", "")
        latest_ver = tag_name.lstrip("vV") if tag_name else "unknown"
        cur_tuple = _parse_version(cur_ver)
        latest_tuple = _parse_version(latest_ver)
        has_update = bool(latest_tuple and cur_tuple and latest_tuple > cur_tuple)

        download_url = data.get("html_url", "")
        for asset in data.get("assets", []):
            name = asset.get("name", "").lower()
            if name.endswith((".zip", ".exe")):
                download_url = asset.get("browser_download_url") or download_url
                break

        res = {
            "ok": True,
            "has_update": has_update,
            "current_version": cur_ver,
            "latest_version": latest_ver,
            "release_title": data.get("name") or tag_name,
            "release_notes": data.get("body", ""),
            "release_url": data.get("html_url", ""),
            "download_url": download_url,
            "published_at": data.get("published_at", ""),
            "checked_at": now,
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            res = {
                "ok": True,
                "has_update": False,
                "current_version": cur_ver,
                "latest_version": cur_ver,
                "message": "当前已是最新版本（暂无已发布的 Release）",
                "checked_at": now,
            }
        elif e.code == 403:
            res = {
                "ok": False,
                "error": "GitHub API 访问受限（请求超频），请稍后再试",
                "current_version": cur_ver,
                "checked_at": now,
            }
        else:
            res = {
                "ok": False,
                "error": f"检查更新失败 (HTTP {e.code}): {e.reason}",
                "current_version": cur_ver,
                "checked_at": now,
            }
    except Exception as e:  # noqa: BLE001
        res = {
            "ok": False,
            "error": f"无法连接到更新服务: {str(e)[:80]}",
            "current_version": cur_ver,
            "checked_at": now,
        }

    with _UPDATE_LOCK:
        _UPDATE_CACHE["ts"] = now
        _UPDATE_CACHE["data"] = res
    return res


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

_WEBUI_VER = {"t": 0.0, "v": ""}


def webui_version():
    """前端静态资源里最新的 mtime（3 秒缓存），作为"前端已更新"的版本戳。

    为什么需要它：桌面端窗口是**常驻**的——点 X 只是 `hide()`，`_on_win_closing`
    返回 False 阻止销毁（见 tray_app），于是 WebView 只在窗口创建那一刻加载过一次
    前端，之后改 `webui/` 桌面端一直显示旧页面（表现为"改了前端但界面没变"，
    且**每个页面都这样**，2026-09-21 踩到）。前端轮询本接口，版本变了就自己
    `location.reload()`。
    """
    now = time.time()
    if _WEBUI_VER["v"] and now - _WEBUI_VER["t"] < 3:
        return _WEBUI_VER["v"]
    newest = 0.0
    for root, _dirs, files in os.walk(WEBUI):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in (".html", ".js", ".css", ".svg"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
            except OSError:
                pass
    _WEBUI_VER.update(t=now, v=f"{newest:.3f}")
    return _WEBUI_VER["v"]


_APPEARANCE_DEFAULTS = {
    "theme": "dark", "lastDarkTheme": "dark", "lastLightTheme": "light",
    "fontFamily": "default", "fontScale": "1", "bgImage": "",
    "bgOpacity": 100, "bgBrightness": 100, "bgZoom": 100,
    "bgX": 0, "bgY": 0, "cardOpacity": 100, "cardBlur": 0,
}
_APPEARANCE_THEMES = {"dark", "light", "ocean", "violet", "forest", "sepia", "slate"}
_APPEARANCE_FONTS = {"default", "yahei", "simsun", "kaiti"}
_APPEARANCE_SCALES = {"0.9", "1", "1.1", "1.25"}
_APPEARANCE_IMAGE_PREFIX = "data:image/jpeg;base64,"
_APPEARANCE_IMAGE_MAX = 3_500_000


def _appearance_config_path():
    """用户外观配置放在用户本机目录，避免跟 exe、源码或打包版本绑定。"""
    root = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(root, "XiaoyuToolbox", "appearance.json")


def _appearance_int(raw, key, low, high, default):
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(low, min(high, value))


def _appearance_pan(raw, key, limit):
    try:
        value = round(float(raw.get(key, 0)), 1)
    except (TypeError, ValueError, OverflowError):
        value = 0
    return max(-limit, min(limit, value))


def _sanitize_appearance_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("外观配置必须是 JSON 对象")
    out = dict(_APPEARANCE_DEFAULTS)
    for key, allowed in (("theme", _APPEARANCE_THEMES),
                         ("lastDarkTheme", _APPEARANCE_THEMES),
                         ("lastLightTheme", _APPEARANCE_THEMES),
                         ("fontFamily", _APPEARANCE_FONTS),
                         ("fontScale", _APPEARANCE_SCALES)):
        value = raw.get(key)
        if isinstance(value, str) and value in allowed:
            out[key] = value
    image = raw.get("bgImage", "")
    if image:
        if (not isinstance(image, str) or len(image) > _APPEARANCE_IMAGE_MAX
                or not image.startswith(_APPEARANCE_IMAGE_PREFIX)):
            raise ValueError("背景图格式不受支持或文件过大")
        try:
            image_bytes = base64.b64decode(image.split(",", 1)[1], validate=True)
        except Exception as exc:
            raise ValueError("背景图数据无效") from exc
        if not image_bytes.startswith(b"\xff\xd8\xff"):
            raise ValueError("背景图不是有效的 JPEG")
        out["bgImage"] = image
    out["bgOpacity"] = _appearance_int(raw, "bgOpacity", 0, 100, 100)
    out["bgBrightness"] = _appearance_int(raw, "bgBrightness", 50, 150, 100)
    out["bgZoom"] = _appearance_int(raw, "bgZoom", 100, 250, 100)
    pan_limit = min(50, max(0, (out["bgZoom"] - 100) // 2))
    out["bgX"] = _appearance_pan(raw, "bgX", pan_limit)
    out["bgY"] = _appearance_pan(raw, "bgY", pan_limit)
    out["cardOpacity"] = _appearance_int(raw, "cardOpacity", 0, 100, 100)
    out["cardBlur"] = _appearance_int(raw, "cardBlur", 0, 24, 0)
    # 仪表盘卡片布局（编辑模式的顺序/尺寸/删除列表，2026-10-03）：
    # 存这里（用户本机 appearance.json）而不是浏览器 localStorage——
    # WebView2 每次启动用临时 user-data 目录，安装更新后 localStorage 会丢。
    layout = raw.get("dashboardLayout")
    if isinstance(layout, dict):
        try:
            blob = json.dumps(layout, ensure_ascii=False)
            if len(blob) <= 64_000:
                out["dashboardLayout"] = layout
        except Exception:
            pass
    # 仪表盘布局命名预设集 + 激活预设名（最多 20 个，单个 64KB 上限）
    presets = raw.get("dashboardPresets")
    if isinstance(presets, dict):
        clean = {}
        for pk, pv in list(presets.items())[:20]:
            if isinstance(pk, str) and 0 < len(pk) <= 40 and isinstance(pv, dict):
                try:
                    if len(json.dumps(pv, ensure_ascii=False)) <= 64_000:
                        clean[pk] = pv
                except Exception:
                    pass
        out["dashboardPresets"] = clean
    active = raw.get("dashboardActivePreset")
    if isinstance(active, str) and active in out.get("dashboardPresets", {}):
        out["dashboardActivePreset"] = active
    # 功能模块安装表（2026-10-03）：installed 为模块 id 字符串列表；缺字段 = 前端默认集。
    # id 合法性与核心模块裁决在前端模块表，后端只做形状与大小约束。
    modules = raw.get("modules")
    if isinstance(modules, dict):
        installed = modules.get("installed")
        if isinstance(installed, list):
            cleaned = []
            for item in installed:
                if isinstance(item, str) and 0 < len(item) <= 64 and item not in cleaned:
                    cleaned.append(item)
                if len(cleaned) >= 64:
                    break
            out["modules"] = {"installed": cleaned}
    # 仪表盘新卡配置（2026-10-03）：图片卡的图片（dataURL）与天气卡城市。
    # 形如 {"img-1": {"img": "data:image/jpeg;base64,...", "fit": "contain"},
    #        "weather": {"city": "昆明"}}。图片只收 jpeg/png 两种 dataURL 前缀
    # （前端上传时已统一重编码），单张 ≤3.5MB（与 bgImage 同限），总量 ≤11MB。
    cards = raw.get("dashboardCards")
    if isinstance(cards, dict):
        clean_cards = {}
        total = 0
        for ck, cv in list(cards.items())[:32]:
            if not (isinstance(ck, str) and 0 < len(ck) <= 40 and isinstance(cv, dict)):
                continue
            entry = {}
            img = cv.get("img", "")
            if img:
                if (not isinstance(img, str) or len(img) > _APPEARANCE_IMAGE_MAX
                        or not img.startswith(("data:image/jpeg;base64,",
                                               "data:image/png;base64,"))):
                    continue
                entry["img"] = img
            fit = cv.get("fit")
            if fit in ("contain", "cover", "fill"):
                entry["fit"] = fit
            city = cv.get("city")
            if isinstance(city, str) and 0 < len(city.strip()) <= 40:
                entry["city"] = city.strip()
            timezone = cv.get("timezone")
            if isinstance(timezone, str) and 0 < len(timezone.strip()) <= 60:
                entry["timezone"] = timezone.strip()
            if not entry:
                continue
            total += len(json.dumps({ck: entry}, ensure_ascii=False))
            if total > 11_000_000:
                break
            clean_cards[ck] = entry
        out["dashboardCards"] = clean_cards
    return out


def load_appearance_config():
    path = _appearance_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {"exists": True, "config": _sanitize_appearance_config(raw)}
    except FileNotFoundError:
        return {"exists": False, "config": dict(_APPEARANCE_DEFAULTS)}
    except Exception:
        # 配置损坏不阻塞启动；前端会用 localStorage 回退并重新写入有效文件。
        return {"exists": False, "config": dict(_APPEARANCE_DEFAULTS)}


# 保存时 body 未携带、但文件里已有的键（dashboardLayout/modules 有各自的写入方，
# 外观卡整包保存不会带它们）——沿用文件现值，避免一次外观保存把别的键冲掉。
_APPEARANCE_PRESERVED_KEYS = ("dashboardLayout", "dashboardPresets", "dashboardActivePreset",
                              "modules", "dashboardCards")


def save_appearance_config(raw):
    config = _sanitize_appearance_config(raw)
    path = _appearance_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        existing = None
    if isinstance(existing, dict):
        for key in _APPEARANCE_PRESERVED_KEYS:
            if key not in raw and key in existing:
                config[key] = existing[key]
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".appearance-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
    return config


def _since(days):
    if not days:
        return None
    if str(days).strip() == "1":
        # 自然日「今日」：今日 00:00:00 本地时间戳（严防跨入昨日下午产生跨天杂质数据）
        now = datetime.datetime.now()
        midnight = datetime.datetime(now.year, now.month, now.day, 0, 0, 0)
        return midnight.timestamp()
    try:
        d = float(days)
        return time.time() - d * 86400
    except (ValueError, TypeError):
        return None


def _since_from_query(q):
    """窗口起点（秒级时间戳）。

    ?start= / ?since= 优先——仪表盘「自定义」范围按浏览器系统时区换算成 UTC 秒传入；
    缺省回退 ?days=N 折算（其中 days=1 为今日 00:00:00）。"""
    v = (q.get("start") or [""])[0]
    if v:
        try:
            ts = float(v)
            if ts > 0:
                return ts
        except ValueError:
            pass
    s = (q.get("since") or [""])[0]
    if s:
        try:
            ts = float(s)
            if ts > 0:
                return ts
        except ValueError:
            pass
    return _since(_days_from_query(q))


def _days_from_query(q):
    v = (q.get("days") or ["all"])[0]
    if v in ("all", "", None):
        return None
    try:
        return int(v)
    except ValueError:
        return None


def health():
    """两个数据源的状态（前端状态条用）。"""
    out = {"hermes": [], "ccs": {}}
    for name in stats_api.AGENTS:
        db = stats_api._agent_db(name)
        ok = os.path.exists(db)
        out["hermes"].append({
            "agent": name, "path": db, "ok": ok,
            "size_mb": round(os.path.getsize(db) / 1e6, 1) if ok else 0,
            "mtime": (time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(os.path.getmtime(db)))
                      if ok else None),
        })
    p = stats_api.CCS_DB
    ok = os.path.exists(p)
    out["ccs"] = {
        "path": p, "ok": ok,
        "size_mb": round(os.path.getsize(p) / 1e6, 2) if ok else 0,
        "mtime": (time.strftime("%Y-%m-%d %H:%M",
                                time.localtime(os.path.getmtime(p)))
                  if ok else None),
    }
    return out


def _read_cfg():
    # 必须读 exe 同目录的 config.json（frozen 下 __file__ 指向 _MEIPASS，
    # 与 selection.save_config 的写路径分裂会导致改键后读回旧值，实测踩坑）
    import selection
    try:
        with open(selection.CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def toolbox_features():
    """主页功能清单（小宇工具箱多专区驾驶舱）。"""
    return [
        {"id": "translate", "name": "翻译专区", "icon": "translate",
         "desc": "本地离线大模型 / 云端极速 / 自定义 AI API 三引擎自由切换，支持划词即译与框选 OCR",
         "page": "#/translate", "ready": True},
        {"id": "ai", "name": "AI 专区", "icon": "sparkles",
         "desc": "每日全球 AI 前沿快讯速递，多维度权威大模型天梯排行榜（综合/代码/开源/性价比）",
         "page": "#/ai", "ready": True},
        {"id": "tokens", "name": "Token 统计 (直达)", "icon": "chart",
         "desc": "各 AI 工具与 Agent 的 Token 消耗明细、缓存命中率及成本精确核算",
         "page": "#/agent/token", "ready": True},
    ]



def toolbox_status():
    """工具箱整体状态（主页展示 + 翻译页用）。"""
    cfg = _read_cfg()
    trans_cfg = translate_backend.load_trans_engine_config()

    st = {
        "app_name": "小宇工具箱",
        "version": app_version(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "features": toolbox_features(),
        "hotkeys": {
            "zh": cfg.get("hotkey_zh", "ctrl+shift+t"),
            "en": cfg.get("hotkey_en", "ctrl+shift+e"),
            "ocr": cfg.get("hotkey_ocr", "ctrl+alt+s"),
            "clip": cfg.get("hotkey_clip", "ctrl+alt+v"),
        },
        "stats_port": PORT,
        "trans_config": trans_cfg,
    }
    try:
        import translate_backend as tb
        st["translate"] = {
            "running": bool(tb.port_busy()),
            "port": tb.PORT,
            "engine": trans_cfg.get("trans_engine", "cloud"),
            "model": tb.model_path().name if tb.port_busy() else "Qwen3.5-4B-Q4_K_M.gguf",
        }
    except Exception as e:
        st["translate"] = {"running": False, "error": str(e)[:80]}
    try:
        st["ocr"] = {"available": True, "engine": "Windows.Media.Ocr"}
    except Exception:
        st["ocr"] = {"available": False}
    return st


def toolbox_translate(text, to="zh", engine=None):
    """多引擎翻译调用。"""
    t0 = time.time()
    out = translate_backend.translate_unified(text, to=to, engine=engine)
    return {"translation": out, "ms": int((time.time() - t0) * 1000),
            "to": to, "engine": engine}, None


# ---------------------------------------------------------------- 热键设置
HOTKEY_ACTIONS = [
    ("hotkey_zh", "zh"), ("hotkey_en", "en"),
    ("hotkey_ocr", "ocr"), ("hotkey_clip", "clip"),
]


def hotkeys_state():
    """当前四个热键的键位与注册状态（托盘线程维护注册结果）。"""
    cfg = _read_cfg()
    from selection import DEFAULT_CONFIG
    hotkeys = {act: cfg.get(field, DEFAULT_CONFIG[field])
               for field, act in HOTKEY_ACTIONS}
    try:
        import tray_app
        status = dict(tray_app.HOTKEY_STATUS)
    except Exception:                       # noqa: BLE001
        status = {}                         # 独立跑 stats_server 时无托盘
    return {"ok": True, "hotkeys": hotkeys, "status": status,
            "captured": bool(status)}


def _validate_hotkey(value, field, all_cfg):
    """校验键位串：至少一个修饰键 + 一个合法主键，且不与其他热键重复。"""
    from selection import DEFAULT_CONFIG, KEY_MAP
    parts = [p.strip().lower() for p in (value or "").split("+") if p.strip()]
    mods = [p for p in parts[:-1] if p in ("ctrl", "alt", "shift", "win")]
    main = parts[-1] if parts else ""
    if not mods or not main or main in ("ctrl", "alt", "shift", "win"):
        return "需要至少一个修饰键（Ctrl/Alt/Shift/Win）加一个主键"
    if main not in KEY_MAP:
        return "主键不支持（可用：字母/数字/F1~F12/方向键/翻页/Insert/Delete 等）"
    for f, act in HOTKEY_ACTIONS:
        if f == field:
            continue
        other = all_cfg.get(f, DEFAULT_CONFIG[f])
        if other and other.lower() == value.lower():
            return f"与另一个热键冲突（{other}），请换一个组合"
    return None


def save_hotkey(field, value):
    """保存单个热键并请求热键线程重注册。"""
    if field not in ("hotkey_zh", "hotkey_en", "hotkey_ocr", "hotkey_clip"):
        return {"ok": False, "error": "未知热键"}, 400
    cfg = _read_cfg()
    err = _validate_hotkey(value, field, cfg)
    if err:
        return {"ok": False, "error": err}, 400
    from selection import load_config, save_config
    full = load_config()
    full[field] = value.strip().lower()
    save_config(full)
    try:
        import tray_app
        tray_app.request_hotkey_reload()
        hint = "已保存并重新注册，如被其他程序占用请在状态里确认"
    except Exception:                       # noqa: BLE001
        hint = "已保存（独立服务模式，重启托盘后生效）"
    return {"ok": True, "hint": hint}, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoyuToolboxServer/" + app_version()

    def log_message(self, fmt, *args):
        pass                                    # 静音访问日志

    # -------------------------------------------------- 工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEBUI, rel))
        if not full.startswith(os.path.normpath(WEBUI)):
            return self._json({"error": "forbidden"}, 403)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.exists(full):
            return self._json(
                {"error": "not found", "hint": f"webui/ 里还没有 {rel}",
                 "webui_dir": WEBUI}, 404)
        with open(full, "rb") as f:
            data = f.read()
        ext = os.path.splitext(full)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # 本地工作台前端要求随 exe/webui 更新即时生效，禁止启发式缓存。
        # no-cache（无 ETag/Last-Modified 可验证）在 WebView2/Chrome 里仍会被
        # 复用旧响应——改 no-store 才能保证导航必取最新（2026-10-03 实测）。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _proxy_brief_image(self, src):
        """快讯/要闻帖子配图经本机 8790 转发（见 ai_fetcher.fetch_remote_image 的说明）。

        跨源直连会被 Chromium 按 CORP 直接拒发，由后端取回、以同源响应发出即可。
        加长缓存头：同一张帖子图会随翻页反复请求，让浏览器自己缓存，服务端不做内存缓存。
        """
        if ai_fetcher is None:
            return self._json({"error": "ai_fetcher 不可用"}, 503)
        try:
            data, ext = ai_fetcher.fetch_remote_image(src)
        except ValueError as ex:
            return self._json({"error": str(ex)}, 400)
        except Exception as ex:                       # noqa: BLE001
            return self._json({"error": f"取图失败: {str(ex)[:120]}"}, 502)
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # -------------------------------------------------- 路由
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            # 模块物理隔离闸：已卸载模块的接口一律 404（核心接口不在映射表，不受影响）
            if not module_runtime.gate(p):
                return self._json({"error": "not found"}, 404)
            if p == "/api/modules/state":
                return self._json(module_runtime.module_state())
            if p.startswith("/api/cleaner/"):
                handled, code, payload = cleaner_api.dispatch("GET", p, {})
                if handled:
                    return self._json(payload, code)
            if p == "/api/stats/summary":
                days = _days_from_query(q)
                return self._json(stats_api.summary(_since_from_query(q)))
            if p == "/api/stats/agents":
                days = _days_from_query(q)
                return self._json(stats_api.hermes_agents(_since_from_query(q)))
            if p == "/api/stats/models":
                days = _days_from_query(q)
                return self._json(stats_api.ccs_models(_since_from_query(q)))
            if p == "/api/stats/model-sources":
                days = _days_from_query(q)
                return self._json(stats_api.model_sources_overview(_since_from_query(q)))
            if p == "/api/stats/daily":
                return self._json(stats_api.ccs_daily())
            if p == "/api/stats/tools":
                days = _days_from_query(q)
                return self._json(stats_api.tools_overview(_since_from_query(q)))
            if p == "/api/appearance":
                return self._json(load_appearance_config())
            if p == "/api/stats/health":
                return self._json(health())
            # 仪表盘「实时脉搏」：速率/小时堆叠/请求波/请求流/工具日序列。
            # 30s 轮询接口，只做轻量只读——CC 账号 5 小时窗口并的是巡检快照
            # 单飞 + 2s 缓存：/tools 全盘扫描等重请求并发时（多常驻页面 +
            # 热重载 reload 风暴），pulse 会被 GIL/IO 拖到 10s+；这里把计算
            # 串行化，排队的请求 2s 内直接命中缓存秒回。
            if p == "/api/stats/pulse":
                with _ZP_LOCK:
                    if _ZP_CACHE["data"] is not None and time.time() - _ZP_CACHE["ts"] < 2.0:
                        return self._json(_ZP_CACHE["data"])
                    payload = stats_api.pulse()
                    _ZP_CACHE.update(ts=time.time(), data=payload)
                    return self._json(payload)

            # 仪表盘 v-t 图时间筛选：指定滚动窗口的速率序列。
            # 默认 1 小时窗口由 pulse 的 minutes 直接覆盖（前端不请求本路由），
            # 3h/6h/12h/24h 走这里独立取数（stats_api.rate_series，桶宽自适应，
            # 与 pulse 分钟桶同一实现）；单飞 + 按窗口 2s 缓存，避免多页并发的重复算。
            if p == "/api/stats/rate-series":
                try:
                    rng = int((q.get("range") or ["60"])[0])
                except (TypeError, ValueError):
                    rng = -1
                if rng not in stats_api.VT_RANGES:
                    return self._json({"error": "invalid range",
                                       "allowed": sorted(stats_api.VT_RANGES)}, 400)
                with _VT_LOCK:
                    c = _VT_CACHE.get(rng)
                    if c and time.time() - c["ts"] < 2.0:
                        return self._json(c["data"])
                    payload = stats_api.rate_series(rng)
                    _VT_CACHE[rng] = {"ts": time.time(), "data": payload}
                    return self._json(payload)

            # === 仪表盘新增卡（2026-10-03）：天气 / 系统监控 ===
            if p == "/api/weather":
                return self._json(weather_card((q.get("city") or [""])[0]))
            if p == "/api/sysmon":
                return self._json(sysmon_card())

            # === AI 专区接口 ===
            if p == "/api/ai/news":
                cat = (q.get("category") or ["all"])[0]
                kw = (q.get("search") or [""])[0]
                return self._json(ai_hub.load_ai_news(cat, kw))
            if p == "/api/ai/rankings":
                cat = (q.get("category") or ["overall"])[0]
                return self._json(ai_hub.load_ai_rankings(cat))
            if p == "/api/ai/rankings/refresh/status":
                """手动刷新天梯榜的进度（前端 2.5s 轮询，见 ai_fetcher._rank_refresh）。"""
                if ai_fetcher is None:
                    return self._json({"error": "ai_fetcher 不可用"}, 503)
                source = (q.get("source") or [None])[0]
                return self._json(ai_fetcher.rankings_refresh_status(source))
            if p == "/api/ai/briefings":
                limit = (q.get("limit") or ["30"])[0]
                return self._json(ai_hub.load_ai_briefings(limit))
            if p == "/api/ai/models":
                kw = (q.get("q") or [""])[0]
                limit = (q.get("limit") or ["300"])[0]
                source = (q.get("source") or ["aa"])[0]
                board = (q.get("board") or ["text"])[0]
                result = ai_hub.load_ai_models(kw, limit, source, board)
                return self._json(result, 400 if result.get("status") == "error" else 200)
            if p == "/api/ai/image":
                return self._proxy_brief_image((q.get("u") or [""])[0])
            if p == "/api/webui/version":
                return self._json({"version": webui_version(), "app_version": app_version(), "webui_dir": WEBUI})
            # === Agent 专区与账号切换接口 ===
            if p == "/api/codex/status":
                import codex_switcher
                return self._json(codex_switcher.get_codex_status())

            if p == "/api/translate/config":
                return self._json(translate_backend.load_trans_engine_config())

            # === 剪贴板历史接口 ===
            if p == "/api/clipboard/history":
                return self._json(clipboard_history.get_state())

            # === 游戏专区接口 ===
            if p == "/api/games/epic":
                return self._json(epic_claimer.get_state())

            if p == "/api/clashnodes/env":
                try: return self._json(clash_node_check.env_status())
                except ValueError as ex: return self._json({"error": str(ex)}, 400)
            if p == "/api/clashnodes/status":
                return self._json(clash_node_check.status())
            if p == "/api/clashnodes/results":
                fk = (q.get("filter") or ["all"])[0]
                kw = (q.get("q") or [""])[0]
                return self._json(clash_node_check.results(fk, kw))
            if p == "/api/clashnodes/history":
                node = (q.get("node") or [None])[0]
                try:
                    if node:
                        return self._json(clash_node_check.history_node(node))
                    limit = int((q.get("limit") or ["30"])[0])
                except ValueError:
                    return self._json({"error": "limit 必须是整数"}, 400)
                return self._json(clash_node_check.history_rounds(limit))
            if p == "/api/clashnodes/health":
                online = (q.get("online") or ["0"])[0] in ("1", "true")
                try:
                    return self._json(clash_node_check.health_check(include_online=online))
                except Exception as exc:      # noqa: BLE001
                    return self._json({"error": f"体检失败: {exc}"}, 500)
            if p == "/api/clashnodes/writeback":
                try:
                    return self._json(clash_node_check.writeback_preview())
                except ValueError as ex:
                    return self._json({"error": str(ex)}, 400)
            if p == "/api/clashnodes/export":
                fk = (q.get("filter") or ["all"])[0]
                kw = (q.get("q") or [""])[0]
                filename, blob = clash_node_check.export_csv(fk, kw)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
                return
            if p == "/api/clashnodes/report":
                # Markdown 检测报告（脱敏）；每次请求重新生成并滚动落盘
                try:
                    filename, text = clash_node_check.build_report()
                except ValueError as ex:
                    return self._json({"error": str(ex)}, 400)
                blob = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
                return
            if p == "/api/clashnodes/export-config":
                # 完整配置导出（组员按检测结果重排）；文件含节点凭据，仅落本机
                try:
                    filename, text = clash_node_check.export_config()
                except ValueError as ex:
                    return self._json({"error": str(ex)}, 400)
                blob = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-yaml; charset=utf-8")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
                return

            # === Clash 实时 IP & 链路诊断仪 ===
            if p == "/api/clash/inspector/current":
                force = (q.get("force") or ["0"])[0] in ("1", "true")
                return self._json(clash_inspector.probe_current_ip(force=force))
            if p == "/api/clash/inspector/trace":
                target = (q.get("domain") or [""])[0]
                if not target:
                    return self._json({"ok": False, "error": "domain 参数必填"}, 400)
                return self._json(clash_inspector.trace_target_website(target))
            if p == "/api/clash/inspector/dns":
                return self._json(clash_inspector.probe_dns_diagnostics())
            if p == "/api/clash/inspector/ip":
                target = (q.get("ip") or [""])[0].strip()
                if not target:
                    return self._json({"ok": False, "error": "ip 参数必填"}, 400)
                return self._json(clash_inspector.probe_ip_info(target))
            if p == "/api/clash/inspector/cfscore":
                side = (q.get("side") or ["direct"])[0]
                force = (q.get("force") or ["0"])[0] in ("1", "true")
                if side not in ("proxy", "direct"):
                    return self._json({"ok": False, "error": "side 只能是 proxy/direct"}, 400)
                return self._json(clash_inspector.probe_cf_score(side=side, force=force))
            if p == "/api/clash/inspector/neighbors":
                return self._json(clash_inspector.probe_neighbors())

            # === 额度监控接口 ===
            # === 热键设置接口 ===
            if p == "/api/hotkeys":
                return self._json(hotkeys_state())

            # === 基础工具箱状态 ===
            if p == "/api/toolbox/status":
                return self._json(toolbox_status())
            if p == "/api/toolbox/features":
                return self._json({"features": toolbox_features()})
            if p == "/api/toolbox/check_update":
                force = q.get("force", ["0"])[0] in ("1", "true", "True")
                return self._json(check_app_update(force=force))

            # === 原生窗口跨进程呼起接口 ===
            if p == "/api/window/theme":
                return self._json({"ok": True, "theme": dark_frame.get_pref()})
            if p == "/api/window/show":
                if _window_show_callback:
                    try:
                        ok = _window_show_callback()
                        return self._json({"ok": bool(ok), "status": "invoked"})
                    except Exception as ex:
                        return self._json({"ok": False, "error": str(ex)}, 500)
                return self._json({"ok": False, "error": "未注册窗口回调（可能为纯后端模式）"}, 503)
            if p == "/api/popup/scale":
                try:
                    from selection import load_config
                    s = float(load_config().get("popup_scale", 1.0) or 1.0)
                except Exception:
                    s = 1.0
                return self._json({"ok": True, "scale": s})

            return self._static(p)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        # 模块物理隔离闸：已卸载模块的接口一律 404，且不读取请求体
        if not module_runtime.gate(u.path):
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            # 4MB 旧限装不下 4 张图片卡的图（单张可达 3.5MB），放宽到 12MB
            if u.path == "/api/appearance" and n > 12_000_000:
                return self._json({"error": "外观配置过大"}, 413)
            if u.path == "/api/appearance" and n <= 0:
                return self._json({"error": "外观配置为空"}, 400)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            if u.path == "/api/appearance":
                return self._json({"error": "外观配置 JSON 无效"}, 400)
            body = {}
        try:
            # === 功能模块管理（核心接口：本身不经过模块闸） ===
            if u.path == "/api/modules/install":
                ret = module_runtime.install_module(
                    str(body.get("id") or ""))
                return self._json(ret, 200 if ret.get("ok") else 400)
            if u.path == "/api/modules/uninstall":
                ret = module_runtime.uninstall_module(
                    str(body.get("id") or ""),
                    delete_data=bool(body.get("deleteData")))
                return self._json(ret, 200 if ret.get("ok") else 400)
            if u.path.startswith("/api/cleaner/"):
                handled, code, payload = cleaner_api.dispatch("POST", u.path, body)
                if handled:
                    return self._json(payload, code)
            if u.path == "/api/appearance":
                try:
                    save_appearance_config(body)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json({"ok": True})

            # === 多引擎翻译接口 ===
            if u.path == "/api/translate":
                text = (body.get("text") or "").strip()
                to = body.get("to") or "zh"
                engine = body.get("engine") or None
                if to not in ("zh", "en"):
                    to = "zh"
                if not text:
                    return self._json({"error": "文本为空"}, 400)
                if len(text) > 5000:
                    text = text[:5000]

                # 若明确指定本地引擎，检查 8899 是否就绪
                cfg = translate_backend.load_trans_engine_config()
                actual_engine = engine or cfg.get("trans_engine", "cloud")
                if actual_engine == "local" and not translate_backend.port_busy():
                    return self._json({"error": "本地模型后端未启动（8899 无监听），请启动模型或切换为云端/API引擎"}, 503)

                t0 = time.time()
                try:
                    out = translate_backend.translate_unified(text, to=to, engine=actual_engine)
                    return self._json({
                        "translation": out,
                        "to": to,
                        "ms": int((time.time() - t0) * 1000),
                        "engine": actual_engine,
                    })
                except Exception as ex:
                    return self._json({"error": f"翻译执行失败: {str(ex)[:150]}"}, 500)

            # === 翻译引擎配置保存 ===
            if u.path == "/api/translate/config":
                ok = translate_backend.save_trans_engine_config(body)
                return self._json({"ok": ok, "config": translate_backend.load_trans_engine_config()})

            # === AI 专区操作接口 ===
            if u.path == "/api/ai/news/refresh":
                """手动触发新闻/排行榜抓取（异步执行，立即返回）。"""
                if ai_fetcher:
                    threading.Thread(target=ai_fetcher.refresh_all,
                                     daemon=True).start()
                    return self._json({"ok": True, "hint": "抓取已在后台执行，几秒后刷新页面可见最新数据"})
                return self._json({"ok": False, "error": "ai_fetcher 不可用"}, 500)

            if u.path == "/api/ai/rankings/refresh":
                """手动刷新所有榜源，或用 source 参数只刷新一个模型数据库。"""
                if ai_fetcher:
                    source = (parse_qs(u.query).get("source") or [None])[0]
                    try:
                        started = ai_fetcher.run_refresh_rankings_async(source)
                    except ValueError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    return self._json({"ok": True, "started": started,
                                       "hint": "抓取已在后台执行" if started else "已有刷新任务在运行"})
                return self._json({"ok": False, "error": "ai_fetcher 不可用"}, 500)

            # === Agent 专区操作接口 ===
            if u.path == "/api/codex/switch":
                target = body.get("target") or "big"
                import codex_switcher
                return self._json(codex_switcher.switch_codex_account(target))

            if u.path == "/api/codex/save_current":
                name = body.get("name") or ""
                profile_id = body.get("id")
                import codex_switcher
                return self._json(codex_switcher.save_current_as_profile(name, profile_id))

            if u.path == "/api/codex/profile/save":
                name = body.get("name") or ""
                auth_content = body.get("auth_content") or ""
                profile_id = body.get("id")
                import codex_switcher
                return self._json(codex_switcher.add_or_update_profile(name, auth_content, profile_id))

            if u.path == "/api/codex/profile/delete":
                profile_id = body.get("id") or ""
                import codex_switcher
                return self._json(codex_switcher.delete_codex_profile(profile_id))

            if u.path == "/api/codex/reorder":
                order = body.get("order") or []
                import codex_switcher
                return self._json(codex_switcher.reorder_codex_profiles(order))

            if u.path == "/api/codex/profile/move":
                pid = body.get("id") or ""
                direction = body.get("direction") or "up"
                import codex_switcher
                return self._json(codex_switcher.move_codex_profile(pid, direction))

            if u.path == "/api/clashnodes/config":
                try:
                    return self._json(clash_node_check.save_config(body if isinstance(body, dict) else {}))
                except ValueError as ex:
                    return self._json({"error": str(ex)}, 400)
            if u.path == "/api/clashnodes/start":
                action = body.get("action") if isinstance(body, dict) else None
                try:
                    if action == "start":
                        # 原样透传给 start_scan 做类型校验（先归一会把非法输入静默放行）
                        subs = body.get("subscriptions") if isinstance(body, dict) else None
                        return self._json({"ok": True, "started": clash_node_check.start_scan(subs)})
                    if action == "stop":
                        return self._json(clash_node_check.stop_scan())
                    if action == "reset":
                        return self._json(clash_node_check.reset_results())
                    if action == "retest":
                        node = str(body.get("node") or "").strip()
                        if not node:
                            raise ValueError("retest 必须带 node（节点名或 probe 编号）")
                        return self._json({"ok": True, "result": clash_node_check.retest(node)})
                    if action in ("writeback_apply", "writeback_remove"):
                        if not body.get("confirm"):
                            raise ValueError("写回/移除必须带 confirm=true（写前会备份并校验）")
                        result = (clash_node_check.writeback_apply() if action == "writeback_apply"
                                  else clash_node_check.writeback_remove())
                        return self._json(result)
                    raise ValueError(f"未知 action: {action!r}（支持 start/stop/reset/retest/writeback_apply/writeback_remove）")
                except ValueError as ex:
                    return self._json({"error": str(ex)}, 400)

            # === 剪贴板历史操作接口 ===
            if u.path == "/api/clipboard/action":
                action = body.get("action") or ""
                if action == "copy":
                    return self._json(
                        clipboard_history.copy_item(int(body.get("id") or 0)))
                if action == "delete":
                    return self._json(
                        clipboard_history.delete_item(int(body.get("id") or 0)))
                if action == "clear":
                    return self._json(clipboard_history.clear_items())
                if action == "toggle":
                    return self._json(clipboard_history.set_enabled(
                        body.get("enabled")))
                return self._json({"error": f"未知 action: {action}"}, 400)

            # === 游戏专区操作接口 ===
            if u.path == "/api/games/epic/refresh":
                return self._json(epic_claimer.refresh_now(force=True))
            if u.path == "/api/games/epic/claim":
                offer_ids = body.get("offerIds")
                return self._json(epic_claimer.start_claim(
                    offer_ids if isinstance(offer_ids, list) else None,
                    source='manual'))
            if u.path == "/api/games/epic/auto":
                return self._json(epic_claimer.set_auto(body.get("enabled")))

            # === 热键改键接口 ===
            if u.path == "/api/hotkeys":
                ret, code = save_hotkey(body.get("key") or "",
                                        body.get("value") or "")
                return self._json(ret, code)

            # === 原生窗口跨进程呼起接口 ===
            if u.path == "/api/window/theme":
                mode = (body.get("theme") or "").strip().lower()
                if mode not in ("dark", "light"):
                    return self._json({"ok": False, "error": "theme 需为 dark/light"}, 400)
                dark_frame.set_pref(mode)
                applied = dark_frame.apply_to_title("小宇工具箱")
                return self._json({"ok": True, "theme": mode, "applied": applied})

            if u.path == "/api/window/show":
                if _window_show_callback:
                    try:
                        ok = _window_show_callback()
                        return self._json({"ok": bool(ok), "status": "invoked"})
                    except Exception as ex:
                        return self._json({"ok": False, "error": str(ex)}, 500)
                return self._json({"ok": False, "error": "未注册窗口回调（可能为纯后端模式）"}, 503)

            # === 翻译浮窗大小：保存到 config.json 并弹出真实预览（设置页滑杆） ===
            if u.path == "/api/popup/scale":
                try:
                    s = float(body.get("scale"))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "scale 需为数字"}, 400)
                s = round(max(0.7, min(1.8, s)), 2)
                try:
                    from selection import load_config, save_config
                    cfg = load_config()
                    cfg["popup_scale"] = s
                    save_config(cfg)
                except Exception as ex:
                    return self._json({"ok": False, "error": f"保存失败: {ex}"}, 500)
                preview = False
                if _trans_popup is not None:
                    try:
                        _trans_popup.show_preview(s)
                        preview = True
                    except Exception:
                        preview = False
                return self._json({"ok": True, "scale": s, "preview": preview})

            return self._json({"error": "not found"}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": f"翻译失败: {str(e)[:120]}"}, 500)


def start_in_thread():
    """在后台线程里起服务（供 tray_app 用）。端口已占则跳过。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex((HOST, PORT)) == 0:
            print(f"[stats] 端口 {PORT} 已在监听，跳过启动")
            return None
    # 启动闸：已卸载模块的常驻线程不拉起（安装表 = appearance.json modules.installed）
    if any(module_runtime.module_enabled(m) for m in module_runtime.AI_GROUP):
        if ai_fetcher:
            ai_fetcher.start_scheduler()    # 每天 19:00 抓新闻/排行榜 + 启动补偿
    else:
        print("[modules] AI 专区全部未安装：跳过 ai_fetcher 调度", flush=True)
    if module_runtime.module_enabled("clipboard"):
        clipboard_history.start_watcher()   # 剪贴板历史监听
    else:
        print("[modules] clipboard 模块未安装：跳过剪贴板监听", flush=True)
    if module_runtime.module_enabled("games"):
        epic_claimer.start_scheduler()      # Epic 周免检测 + 自动领取
    else:
        print("[modules] games 模块未安装：跳过 Epic 周免调度", flush=True)
    stats_api.warm_caches()             # 预热 Codex / Claude 本机扫描，首屏不等冷扫
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[stats] 仪表盘服务已启动: http://{HOST}:{PORT}/", flush=True)
    return srv


def main():
    if any(module_runtime.module_enabled(m) for m in module_runtime.AI_GROUP):
        if ai_fetcher:
            ai_fetcher.start_scheduler()
    if module_runtime.module_enabled("clipboard"):
        clipboard_history.start_watcher()
    if module_runtime.module_enabled("games"):
        epic_claimer.start_scheduler()
    stats_api.warm_caches()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"Token 统计服务: http://{HOST}:{PORT}/  (Ctrl+C 退出)", flush=True)
    print(f"  webui 目录: {WEBUI}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
