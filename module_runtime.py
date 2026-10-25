# -*- coding: utf-8 -*-
"""模块运行时管理器：卸载 = 停止实体 + 路由物理隔离，安装 = 载入。

权威状态：用户本机 %LOCALAPPDATA%\\XiaoyuToolbox\\appearance.json 的
modules.installed（与 WebUI 模块管理同一份；stats_server 对它只做形状校验）。
- 路由闸 gate() 每请求现读（mtime+大小缓存），卸载即刻生效，无需重启 8790；
- uninstall/install 先写安装表再执行启停动作，动作失败不回滚状态（steps 如实回报）。

依赖方向（防循环导入）：
  tray_app → module_runtime ← stats_server
                   clash_node_check（均在动作函数内懒加载）
tray_app 进程内动作（划词热键、复制即翻、8899 引擎）通过 register_actions 注册。
"""
import json
import os
import shutil
import sys
import threading

# -------------------------------------------------- 模块表（与 webui/app.js MODULE_DEFS 对齐）

CORE_MODULES = ("dashboard", "status", "agent-codex")            # 核心，常驻不可卸载
AI_GROUP = ("ai-briefing", "ai-news", "ai-rankings", "ai-compare")
# appearance.json 缺 modules 字段时按此默认集处理（与前端 MODULE_DEFAULT_INSTALLED 一致）
DEFAULT_INSTALLED = ("ai-briefing", "ai-news", "ai-rankings", "ai-compare",
                     "translate", "cleaner",
                     )

MODULE_NAMES = {
    "dashboard": "仪表盘", "ai-briefing": "每日要闻", "ai-news": "实时快讯",
    "ai-rankings": "模型天梯", "ai-compare": "模型对比",
    "agent-codex": "OpenAI 专区",
    "cleaner": "磁盘清理",
    "nodes": "节点检测", "translate": "翻译引擎", "clipboard": "剪贴板历史", "status": "设置",
    "games": "Epic喜加一",
}
ALL_MODULE_IDS = tuple(MODULE_NAMES)

# 路由前缀 → 模块 id（按最具体前缀优先匹配；核心接口不列=不拦）。
# ai 专区按子路径细分到四个模块页面。
_ROUTE_TABLE = (
    ("/api/ai/news", "ai-news"),
    ("/api/ai/briefings", "ai-briefing"),
    ("/api/ai/rankings", "ai-rankings"),
    ("/api/ai/models", "ai-compare"),
    ("/api/ai/image", "ai-briefing"),          # 要闻图片代理归每日要闻
    ("/api/cleaner/", "cleaner"),
    ("/api/clashnodes", "nodes"),
    ("/api/clash/inspector", "nodes"),
    ("/api/clipboard/", "clipboard"),
    ("/api/translate", "translate"),
    ("/api/codex/", "agent-codex"),
    ("/api/games/", "games"),
)

# 卸载时 UI 需警示的连带影响（前端确认模态引用）
SIDE_EFFECTS = {
}

# -------------------------------------------------- 路径

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _appearance_path():
    root = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(root, "XiaoyuToolbox", "appearance.json")


# -------------------------------------------------- 安装表现读（mtime 缓存）

_cache_lock = threading.Lock()
_cache_sig = None       # (mtime_ns, size)
_cache_ids = None       # 冻结的安装 id 元组


def _read_installed_raw():
    """读 appearance.json 的 modules.installed。返回 (ids_tuple, valid)。"""
    path = _appearance_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        modules = raw.get("modules") if isinstance(raw, dict) else None
        installed = modules.get("installed") if isinstance(modules, dict) else None
        if isinstance(installed, list):
            ids = tuple(x for x in installed if isinstance(x, str) and x)
            return ids, True
        return tuple(DEFAULT_INSTALLED), False     # 缺字段 = 默认集
    except FileNotFoundError:
        return tuple(DEFAULT_INSTALLED), False
    except Exception:
        # 配置损坏不阻塞：按默认集放行（与 load_appearance_config 容错口径一致）
        return tuple(DEFAULT_INSTALLED), False


def installed_ids():
    """当前安装表（现读 + mtime/大小缓存，外部改动即刻生效）。"""
    global _cache_sig, _cache_ids
    path = _appearance_path()
    sig = None
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    with _cache_lock:
        if sig == _cache_sig and _cache_ids is not None:
            return _cache_ids
    ids, _ = _read_installed_raw()
    with _cache_lock:
        _cache_sig = sig
        _cache_ids = ids
        return _cache_ids


def module_enabled(mod_id):
    """模块是否处于安装态（核心恒 True；未登记的 id 视为未安装）。"""
    if mod_id in CORE_MODULES:
        return True
    if mod_id not in MODULE_NAMES:
        return False
    return mod_id in installed_ids()


def set_installed(mod_id, want_install):
    """改写安装表并落盘（原子替换；不改 appearance.json 其它键）。"""
    ids, _ = _read_installed_raw()
    rest = [x for x in ids if x != mod_id]
    if want_install:
        rest.append(mod_id)
    path = _appearance_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    raw["modules"] = {"installed": rest}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # 立刻失效缓存（os.replace 会改 mtime，但同秒内 ns 精度兜不住的平台要防）
    global _cache_sig, _cache_ids
    with _cache_lock:
        _cache_sig = None
        _cache_ids = None


# -------------------------------------------------- 路由闸

def route_module(path):
    """API 路径 → 模块 id；不属于任何模块（核心/未知）返回 None。"""
    if not path:
        return None
    for prefix, mod_id in _ROUTE_TABLE:
        if path == prefix or path.startswith(prefix):
            return mod_id
    return None


def gate(path):
    """True=放行；False=该路径所属模块已卸载，调用方应返回 404。"""
    mod_id = route_module(path)
    if mod_id is None:
        return True
    if path == "/api/ai/image" or path.startswith("/api/ai/image?"):
        # 图片代理是「每日要闻」与「实时快讯」共用的帖子配图设施：
        # 只卸载其中一个模块时另一个页面的帖子图不能跟着 404。
        return any(module_enabled(m) for m in AI_GROUP)
    return module_enabled(mod_id)


# -------------------------------------------------- 进程内动作注册（tray_app 用）

_ext_lock = threading.Lock()
_ext_actions = {}       # mod_id -> {"stop": [fn], "start": [fn]}


def register_actions(mod_id, stop_fns=(), start_fns=()):
    """注册模块的进程内启停回调（tray_app 启动时注册热键/复制即翻/8899）。"""
    with _ext_lock:
        entry = _ext_actions.setdefault(mod_id, {"stop": [], "start": []})
        entry["stop"].extend(stop_fns)
        entry["start"].extend(start_fns)


def _run_ext(mod_id, phase, steps):
    with _ext_lock:
        fns = list(_ext_actions.get(mod_id, {}).get(phase, ()))
    for fn in fns:
        try:
            fn()
            steps.append(f"{MODULE_NAMES.get(mod_id, mod_id)} 内部动作[{phase}]完成")
        except Exception as exc:        # noqa: BLE001
            steps.append(f"内部动作[{phase}]失败: {exc}")


# -------------------------------------------------- 各模块动作（懒加载，幂等）

def _act_clipboard_stop(steps):
    import clipboard_history
    clipboard_history.stop_watcher()
    steps.append("剪贴板监听线程已停止")


def _act_clipboard_start(steps):
    import clipboard_history
    clipboard_history.start_watcher()
    steps.append("剪贴板监听线程已启动")


def _ai_scheduler_sync(steps):
    """AI 四模块与调度器联动：任一安装→跑（幂等），全部卸载→停。"""
    import ai_fetcher
    if any(module_enabled(m) for m in AI_GROUP):
        ai_fetcher.start_scheduler()
        steps.append("AI 定时调度保持运行（仍有 AI 模块安装）")
    else:
        ai_fetcher.stop_scheduler()
        steps.append("AI 定时调度已停止（AI 模块全部卸载）")




def _act_nodes_stop(steps):
    import clash_node_check
    ret = clash_node_check.stop_scan()
    steps.append("节点扫描已停止" if ret.get("stopped") else "当前无进行中的扫描")


def _act_epic_scheduler_sync(steps, enabled):
    """游戏专区的 Epic 周免调度启停（幂等；领取进行中不强停，窗口自然结束）。"""
    import epic_claimer
    if enabled:
        epic_claimer.start_scheduler()
        steps.append("Epic 周免检测调度已启动")
    else:
        epic_claimer.stop_scheduler()
        steps.append("Epic 周免检测调度已停止")


_UNINSTALL_ACTIONS = {
    "clipboard": _act_clipboard_stop,
    "nodes": _act_nodes_stop,
}
_INSTALL_ACTIONS = {
    "clipboard": _act_clipboard_start,
}
# 安装/卸载都要同步调度器（AI 组联动）
for _m in AI_GROUP:
    _UNINSTALL_ACTIONS[_m] = lambda steps: _ai_scheduler_sync(steps)
    _INSTALL_ACTIONS[_m] = lambda steps: _ai_scheduler_sync(steps)
# 游戏专区：卸载停调度，安装恢复调度（同 AI 组模式）
_UNINSTALL_ACTIONS["games"] = lambda steps: _act_epic_scheduler_sync(steps, False)
_INSTALL_ACTIONS["games"] = lambda steps: _act_epic_scheduler_sync(steps, True)


# -------------------------------------------------- 数据文件清单（deleteData 白名单）

def _localappdata(rel):
    root = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(root, rel)


# (路径, 展示名)；路径为 BASE_DIR 下相对名，目录以 / 结尾；特殊前缀 LOCALAPPDATA: 见 _data_abs
DATA_FILES = {
    "clipboard": [("clipboard_cache.json", "剪贴板历史记录")],
    "ai-briefing": [("ai_briefings_cache.json", "每日要闻缓存")],
    "ai-news": [("ai_news_cache.json", "实时快讯缓存")],
    "ai-rankings": [("ai_rankings_cache.json", "模型天梯缓存"),
                    ("ai_media_metadata_cache.json", "榜单媒体元数据")],
    "ai-compare": [("ai_models_cache.json", "模型对比缓存")],
    "nodes": [("clash_node_audit_cache.json", "节点审计缓存"),
              ("clash_node_history/", "节点历史快照目录"),
              ("clash_node_reports/", "节点检测报告目录（含节点凭据）")],
    "cleaner": [("LOCALAPPDATA:diskcleaner\\winapp2.ini", "winapp2 社区规则库（可重新下载）")],
    "games": [("epic_claimer_cache.json", "Epic 周免检测缓存与领取记录")],
}
# （runtime/webview_profile 属主窗口与领取窗口共享的 WebView2 持久 profile，同理不删）


def data_files(mod_id):
    """模块的可删数据清单 [(绝对路径, 展示名, is_dir)]；未登记模块返回 []。"""
    out = []
    for spec, name in DATA_FILES.get(mod_id, ()):
        if spec.startswith("LOCALAPPDATA:"):
            abs_path = _localappdata(spec[len("LOCALAPPDATA:"):])
        else:
            abs_path = os.path.join(BASE_DIR, spec)
        out.append((abs_path, name, spec.endswith("/")))
    return out


def _delete_data(mod_id, steps):
    removed, failed = [], []
    for abs_path, name, is_dir in data_files(mod_id):
        # 白名单防御：BASE_DIR 相对路径与 LOCALAPPDATA: 两种来源，均不允许越出
        try:
            if abs_path.startswith(BASE_DIR):
                rel = os.path.relpath(abs_path, BASE_DIR)
                if rel.startswith(".."):
                    raise ValueError("路径越出数据目录")
            try:
                if is_dir and os.path.isdir(abs_path):
                    shutil.rmtree(abs_path)
                    removed.append(name)
                elif os.path.isfile(abs_path):
                    os.remove(abs_path)
                    removed.append(name)
                else:
                    continue            # 本就不存在
            except OSError as exc:
                failed.append(f"{name}: {exc}")
        except (ValueError, OSError) as exc:
            failed.append(f"{name}: {exc}")
    if removed:
        steps.append("已删除数据: " + "、".join(removed))
    if failed:
        steps.append("删除失败: " + "；".join(failed))
    return removed


# -------------------------------------------------- 对外主入口（stats_server /api/modules/* 调用）

def uninstall_module(mod_id, delete_data=False):
    """卸载：先落盘安装表（gate 立即 404），再停实体，可选删数据。返回 {ok, steps, deleted}。"""
    steps, deleted = [], []
    if mod_id in CORE_MODULES or mod_id not in MODULE_NAMES:
        return {"ok": False, "error": "模块不存在或为核心模块，不可卸载", "steps": steps, "deleted": []}
    try:
        set_installed(mod_id, False)
        steps.append("安装表已更新并落盘（接口即刻物理隔离）")
    except OSError as exc:
        return {"ok": False, "error": f"安装表写入失败: {exc}", "steps": steps, "deleted": []}
    _run_ext(mod_id, "stop", steps)
    act = _UNINSTALL_ACTIONS.get(mod_id)
    if act:
        try:
            act(steps)
        except Exception as exc:        # noqa: BLE001
            steps.append(f"{MODULE_NAMES[mod_id]} 停止动作异常: {exc}")
    if delete_data:
        deleted = _delete_data(mod_id, steps)
    _log(f"uninstall {mod_id} deleteData={delete_data} steps={steps}")
    return {"ok": True, "module": mod_id, "steps": steps, "deleted": deleted}


def install_module(mod_id):
    """安装：先落盘安装表（gate 放行），再载入实体。返回 {ok, steps}。"""
    steps = []
    if mod_id not in MODULE_NAMES:
        return {"ok": False, "error": "模块不存在", "steps": steps}
    try:
        set_installed(mod_id, True)
        steps.append("安装表已更新并落盘（接口即刻放行）")
    except OSError as exc:
        return {"ok": False, "error": f"安装表写入失败: {exc}", "steps": steps}
    _run_ext(mod_id, "start", steps)
    act = _INSTALL_ACTIONS.get(mod_id)
    if act:
        try:
            act(steps)
        except Exception as exc:        # noqa: BLE001
            steps.append(f"{MODULE_NAMES[mod_id]} 载入动作异常: {exc}")
    _log(f"install {mod_id} steps={steps}")
    return {"ok": True, "module": mod_id, "steps": steps}


def module_state():
    """全部模块的安装态与可删数据清单（/api/modules/state 用）。"""
    ids = installed_ids()
    items = []
    for mod_id in ALL_MODULE_IDS:
        items.append({
            "id": mod_id,
            "name": MODULE_NAMES[mod_id],
            "core": mod_id in CORE_MODULES,
            "installed": mod_id in CORE_MODULES or mod_id in ids,
            "sideEffect": SIDE_EFFECTS.get(mod_id, ""),
            "dataFiles": [{"path": name, "dir": is_dir}
                          for _, name, is_dir in data_files(mod_id)],
        })
    return {"ok": True, "installed": list(ids), "modules": items}


def _log(msg):
    print(f"[modules] {msg}", flush=True)
