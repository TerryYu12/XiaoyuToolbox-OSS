# -*- coding: utf-8 -*-
"""SpaceSniffer 交互式磁盘空间透视引擎 (SpaceSniffer Treemap Engine)

功能：
1. 快速目录树聚合与多线程高速尺寸分析 (基于 os.scandir，极速低开销)
2. 嵌套矩形树图 (Squarified Treemap) 结构生成与层次化数据构建
3. 智能项目与文件类型识别 (代码工程、模型权重、缓存垃圾、系统文件、音视频媒体等)
4. 多级钻取 (Drill-Down / Drill-Up) 与内存缓存加速
5. Windows 资源管理器原生定位 (Reveal in Explorer)
"""
from __future__ import annotations
import os
import re
import sys
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, List, Tuple

from .scanner import human, is_reparse
from .profiler import OriginInspector

# 内存缓存：path -> (timestamp, data_dict)
_CACHE: Dict[str, Tuple[float, dict]] = {}
_CACHE_TTL = 60.0  # 60 秒有效期

# 常见扩展名分类
_MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".mp3", ".wav", ".flac", ".ogg",
               ".png", ".jpg", ".jpeg", ".webp", ".psd", ".rpp", ".svg", ".gif"}
_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz", ".iso", ".exe", ".msi"}
_AI_EXTS = {".safetensors", ".gguf", ".ckpt", ".bin", ".pth", ".pt", ".onnx", ".h5"}
_CODE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".rs", ".go", ".cpp", ".c",
              ".h", ".java", ".cs", ".html", ".css", ".json", ".yaml", ".yml", ".md"}

_CACHE_KEYWORDS = {"cache", "temp", "tmp", "crashpad", "logs", "log", "__pycache__",
                  "node_modules", ".gradle", "deliveryoptimization", "softwaredistribution"}
_AI_KEYWORDS = {"huggingface", "models", "ollama", "comfyui", "checkpoints", "loras",
                "diffusers", "torch", "weights"}

_DIR_STATS_CACHE: Dict[str, Tuple[float, Tuple[int, int, int, float]]] = {}
_DIR_CACHE_TTL = 300.0


def _fast_dir_stats(path: str) -> Tuple[int, int, int, float]:
    """多级高速遍历计算目录大小、文件数、子目录数与最新修改时间（带 5 分钟极速内存缓存）。"""
    norm_p = os.path.normpath(path)
    now = time.time()
    cached = _DIR_STATS_CACHE.get(norm_p)
    if cached and (now - cached[0] < _DIR_CACHE_TTL):
        return cached[1]

    total_size = 0
    total_files = 0
    total_dirs = 0
    latest_mtime = 0.0
    stack = [norm_p]

    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        p = entry.path
                        if entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            total_size += st.st_size
                            total_files += 1
                            if st.st_mtime > latest_mtime:
                                latest_mtime = st.st_mtime
                        elif entry.is_dir(follow_symlinks=False):
                            if not is_reparse(p):
                                stack.append(p)
                                total_dirs += 1
                    except OSError:
                        pass
        except OSError:
            pass

    res = (total_size, total_files, total_dirs, latest_mtime)
    _DIR_STATS_CACHE[norm_p] = (now, res)
    return res


def classify_item(path: str, is_dir: bool, name: str) -> Tuple[str, str, str]:
    """识别项的类型、标识符与简述。
    返回 (category, category_name, badge_color)
    category 候选：project / ai_model / cache / system / media / archive / general
    """
    np = path.replace("\\", "/").lower()
    name_lower = name.lower()

    # 1. 项目代码工程与版本控制判定
    if name_lower in (".git", ".svn", ".hg") or np.endswith("/.git") or "/.git/" in np:
        return "project", "Git 仓库核心元数据", "#059669"

    if is_dir:
        # 直接包含版本控制或核心清单
        if os.path.exists(os.path.join(path, ".git")):
            return "project", "Git 仓库工程", "#10b981"
        if any(os.path.exists(os.path.join(path, f)) for f in ["package.json", "Cargo.toml", "pyproject.toml"]):
            return "project", "项目代码工程", "#10b981"

    # 2. AI 模型与权重判定
    if any(k in np for k in _AI_KEYWORDS) or any(name_lower.endswith(ext) for ext in _AI_EXTS):
        return "ai_model", "AI 模型/权重", "#8b5cf6"

    # 3. 缓存与临时垃圾
    if any(k in name_lower for k in _CACHE_KEYWORDS):
        return "cache", "缓存/临时垃圾", "#f59e0b"

    # 4. 系统关键目录
    if any(k in np for k in ["/windows", "/program files", "/programdata", "/appdata/local/microsoft"]):
        return "system", "系统与安装程序", "#3b82f6"

    # 5. 音视频与设计媒体
    _, ext = os.path.splitext(name_lower)
    if ext in _MEDIA_EXTS or "artbook" in np or "assets" in np:
        return "media", "音视频/设计媒体", "#ec4899"

    # 6. 安装包与压缩归档
    if ext in _ARCHIVE_EXTS:
        return "archive", "压缩归档/安装包", "#06b6d4"

    # 7. 代码源文件
    if ext in _CODE_EXTS:
        return "project", "代码源文件", "#10b981"

    return "general", "常规文件/目录", "#64748b"


def get_spacesniffer_tree(target_path: str, max_depth: int = 2,
                          min_size: int = 512 * 1024) -> dict:
    """获取指定路径的高性能 SpaceSniffer 矩形树图数据。

    Args:
        target_path: 目标路径（如 "F:\\", "F:\\AI-hub\\workroom", "C:\\Users\\11" 等）
        max_depth: 嵌套展示深度（默认 2 级，展示当前层及直属子层）
        min_size: 最小文件/目录过滤阈值（低于此值合并为「其他小项」，默认 512KB）
    """
    norm_target = os.path.normpath(target_path)
    if not os.path.exists(norm_target):
        raise FileNotFoundError(f"指定路径不存在: {target_path}")

    # 查缓存
    now = time.time()
    cache_key = f"{norm_target}|{max_depth}|{min_size}"
    if cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    is_dir = os.path.isdir(norm_target)
    base_name = os.path.basename(norm_target) or norm_target
    parent_path = os.path.dirname(norm_target)
    if parent_path == norm_target:
        parent_path = ""

    inspector = OriginInspector()
    project_owner = inspector.probe_project_owner(norm_target)
    is_proj = bool(project_owner) or (is_dir and os.path.exists(os.path.join(norm_target, ".git")))
    proj_name = project_owner[0] if project_owner else (base_name if is_proj else "")

    if not is_dir:
        # 单文件处理
        sz = os.path.getsize(norm_target)
        cat, cat_name, color = classify_item(norm_target, False, base_name)
        res = {
            "path": norm_target,
            "name": base_name,
            "parent": parent_path,
            "size": sz,
            "size_h": human(sz),
            "file_count": 1,
            "dir_count": 0,
            "is_dir": False,
            "category": cat,
            "category_name": cat_name,
            "color": color,
            "is_project": is_proj,
            "project_name": proj_name,
            "children": [],
        }
        _CACHE[cache_key] = (now, res)
        return res

    # 目录处理：读取直属子项
    try:
        entries = list(os.scandir(norm_target))
    except (PermissionError, OSError) as ex:
        res = {
            "path": norm_target,
            "name": base_name,
            "parent": parent_path,
            "size": 0,
            "size_h": "0 B",
            "file_count": 0,
            "dir_count": 0,
            "is_dir": True,
            "category": "system",
            "category_name": "受限目录",
            "color": "#ef4444",
            "is_project": False,
            "project_name": "",
            "error": str(ex),
            "children": [],
        }
        return res

    dirs_to_calc = []
    files_info = []

    for e in entries:
        try:
            p = e.path
            if e.is_dir(follow_symlinks=False):
                if not is_reparse(p):
                    dirs_to_calc.append(p)
            elif e.is_file(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)
                files_info.append((p, e.name, st.st_size, st.st_mtime))
        except OSError:
            pass

    # 并行多线程计算子目录尺寸（8 线程极速聚合）
    dir_results: Dict[str, Tuple[int, int, int, float]] = {}
    if dirs_to_calc:
        with ThreadPoolExecutor(max_workers=min(8, len(dirs_to_calc))) as pool:
            futures = {pool.submit(_fast_dir_stats, d): d for d in dirs_to_calc}
            for fut in futures:
                d = futures[fut]
                try:
                    dir_results[d] = fut.result()
                except Exception:
                    dir_results[d] = (0, 0, 0, 0.0)

    # 构建第 1 层孩子节点
    raw_children = []
    total_tree_size = sum(sz for _, _, sz, _ in files_info) + sum(r[0] for r in dir_results.values())
    total_tree_files = len(files_info) + sum(r[1] for r in dir_results.values())
    total_tree_dirs = len(dirs_to_calc) + sum(r[2] for r in dir_results.values())

    # 组装目录项
    for d in dirs_to_calc:
        sz, nfiles, ndirs, mt = dir_results.get(d, (0, 0, 0, 0.0))
        d_name = os.path.basename(d)
        cat, cat_name, color = classify_item(d, True, d_name)
        d_proj = inspector.probe_project_owner(d)
        is_sub_proj = bool(d_proj) or os.path.exists(os.path.join(d, ".git"))
        sub_proj_name = d_proj[0] if d_proj else (d_name if is_sub_proj else "")

        # 直属子节点保持轻量：前端 Treemap 负责渲染直属层级；
        # 双击钻取时直接调取该目录，享受内存缓存加速，避免父层全盘重复深度遍历
        nested_children = []

        raw_children.append({
            "path": d,
            "name": d_name,
            "size": sz,
            "size_h": human(sz),
            "file_count": nfiles,
            "dir_count": ndirs,
            "is_dir": True,
            "category": cat,
            "category_name": cat_name,
            "color": color,
            "is_project": is_sub_proj,
            "project_name": sub_proj_name,
            "children": nested_children,
        })

    # 组装文件项
    for p, fname, sz, mt in files_info:
        cat, cat_name, color = classify_item(p, False, fname)
        raw_children.append({
            "path": p,
            "name": fname,
            "size": sz,
            "size_h": human(sz),
            "file_count": 1,
            "dir_count": 0,
            "is_dir": False,
            "category": cat,
            "category_name": cat_name,
            "color": color,
            "is_project": False,
            "project_name": "",
            "children": [],
        })

    # 按体积排序
    raw_children.sort(key=lambda c: -c["size"])

    # 过滤与合并小项：小于 min_size 的聚合为「其他小文件/小目录」
    filtered_children = []
    tiny_size = 0
    tiny_count = 0

    for c in raw_children:
        if c["size"] >= min_size:
            c["percent"] = round(c["size"] / total_tree_size * 100, 1) if total_tree_size > 0 else 0
            filtered_children.append(c)
        else:
            tiny_size += c["size"]
            tiny_count += (c["file_count"] or 1)

    if tiny_size > 0:
        filtered_children.append({
            "path": os.path.join(norm_target, "(misc_small_items)"),
            "name": f"其他微小文件与目录 ({tiny_count} 项)",
            "size": tiny_size,
            "size_h": human(tiny_size),
            "percent": round(tiny_size / total_tree_size * 100, 1) if total_tree_size > 0 else 0,
            "file_count": tiny_count,
            "dir_count": 0,
            "is_dir": True,
            "category": "general",
            "category_name": "碎片文件",
            "color": "#475569",
            "is_project": False,
            "project_name": "",
            "children": [],
        })

    cat, cat_name, color = classify_item(norm_target, True, base_name)
    result = {
        "path": norm_target,
        "name": base_name,
        "parent": parent_path,
        "size": total_tree_size,
        "size_h": human(total_tree_size),
        "file_count": total_tree_files,
        "dir_count": total_tree_dirs,
        "is_dir": True,
        "category": cat,
        "category_name": cat_name,
        "color": color,
        "is_project": is_proj,
        "project_name": proj_name,
        "children": filtered_children,
    }

    _CACHE[cache_key] = (now, result)
    return result


def reveal_in_explorer(path: str) -> Tuple[bool, str]:
    """在 Windows 资源管理器中高亮选中文件或打开目录。"""
    if not os.path.exists(path):
        return False, f"路径不存在: {path}"
    norm_p = os.path.normpath(path)
    try:
        if os.name == "nt":
            if os.path.isfile(norm_p):
                subprocess.Popen(["explorer.exe", f"/select,{norm_p}"])
            else:
                os.startfile(norm_p)
            return True, "已在资源管理器中打开"
        else:
            return False, "仅支持 Windows 系统"
    except Exception as ex:
        return False, f"打开失败: {ex}"


def launch_native_spacesniffer(path: str = "") -> Tuple[bool, str, int]:
    """启动官方原生 SpaceSniffer.exe，对指定驱动器或目录展开极速透视。

    零延迟、原生 C++ 多线程动态流式渲染，完美复刻 SpaceSniffer 官方体验。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "..", "runtime", "spacesniffer", "SpaceSniffer.exe"),
        os.path.join(here, "..", "..", "runtime", "spacesniffer", "SpaceSniffer.exe"),
        os.path.join(os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.abspath("."),
                     "runtime", "spacesniffer", "SpaceSniffer.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
                     "UderzoSoftware.SpaceSniffer_Microsoft.Winget.Source_8wekyb3d8bbwe", "SpaceSniffer.exe"),
    ]
    exe_path = None
    for c in cands:
        norm = os.path.normpath(c)
        if os.path.isfile(norm):
            exe_path = norm
            break
    if not exe_path:
        import shutil
        exe_path = shutil.which("SpaceSniffer.exe") or shutil.which("spacesniffer")

    if not exe_path or not os.path.isfile(exe_path):
        return False, "未找到原生 SpaceSniffer.exe，请确保 runtime/spacesniffer/ 目录完整", 0

    target = (path or "").strip()
    cmd = [exe_path]
    if target:
        norm_t = os.path.normpath(target)
        if os.path.exists(norm_t):
            cmd.extend(["scan", norm_t])

    try:
        DETACHED_PROCESS = 0x00000008
        p = subprocess.Popen(cmd, creationflags=DETACHED_PROCESS if os.name == "nt" else 0, close_fds=True)
        return True, f"已调起官方原生 SpaceSniffer (PID {p.pid})", p.pid
    except Exception as ex:
        return False, f"调起官方原生 SpaceSniffer 失败: {ex}", 0
