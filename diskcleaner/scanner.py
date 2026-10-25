# -*- coding: utf-8 -*-
"""扫描引擎：快速聚合目录大小，跳过 reparse 链接避免双计，支持增量缓存。

学习自 oh-my-disk-cleaner 的 progressive scanning + incremental cache 思路。
"""
from __future__ import annotations
import os
import time
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional

# Windows reparse 检测
try:
    _ISJUNCTION = os.path.isjunction  # py3.12+
except AttributeError:  # pragma: no cover
    def _ISJUNCTION(p):  # type: ignore
        return False


def is_reparse(path: str) -> bool:
    """是否为 symlink / junction（这类目录不进入遍历，避免重复计数）。"""
    try:
        if os.path.islink(path):
            return True
    except OSError:
        pass
    try:
        if _ISJUNCTION(path):
            return True
    except OSError:
        pass
    return False


@dataclass
class DirStat:
    path: str
    size: int = 0          # 含子目录
    own_size: int = 0      # 仅本目录直属文件
    nfiles: int = 0
    ndirs: int = 0
    mtime: float = 0.0     # 最近修改时间（子树最大）


@dataclass
class ScanResult:
    root: str
    elapsed: float
    total_size: int
    nfiles: int
    ndirs: int
    errors: list = field(default_factory=list)
    reparse_skipped: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)   # path -> DirStat(own_size) 原始
    aggregated: dict = field(default_factory=dict)  # path -> 含子树总大小
    files: list = field(default_factory=list)   # [(path, size, mtime)] 仅 >= collect_min_size
    collect_min_size: int = 1024                # 文件清单收集阈值
    files_complete: bool = False                # files 是否为本次扫描实时收集（缓存加载为 False）

    def to_json(self) -> str:
        return json.dumps({
            "root": self.root,
            "elapsed": round(self.elapsed, 2),
            "total_size": self.total_size,
            "nfiles": self.nfiles,
            "ndirs": self.ndirs,
            "errors": self.errors[:50],
            "aggregated": self.aggregated,
        }, ensure_ascii=False)


def _cache_path(root: str) -> str:
    """扫描缓存文件路径（基于 root 的哈希）。"""
    base = os.path.expanduser("~/.cache/diskcleaner")
    if os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                            "diskcleaner")
    os.makedirs(base, exist_ok=True)
    h = hashlib.md5(root.encode("utf-8", "ignore")).hexdigest()[:12]
    return os.path.join(base, f"scan-{h}.json")


def scan(root: str, use_cache: bool = False, max_age_sec: int = 3600,
         progress=None, collect_min_size: int = 1024) -> ScanResult:
    """遍历 root，聚合每个目录的子树大小。

    Args:
        root: 起始目录
        use_cache: 若为 True 且缓存未过期，直接返回缓存
        max_age_sec: 缓存有效期
        progress: 可选回调 progress(current_dir, files_seen)
        collect_min_size: 顺带收集 >= 此大小的文件清单（供文件级规则匹配，省二次走树）
    """
    root = os.path.abspath(root)
    cache_file = _cache_path(root)

    if use_cache and os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < max_age_sec:
            try:
                with open(cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                res = ScanResult(
                    root=data["root"], elapsed=data["elapsed"],
                    total_size=data["total_size"], nfiles=data["nfiles"],
                    ndirs=data["ndirs"], errors=data.get("errors", []),
                    aggregated=data["aggregated"],
                    collect_min_size=collect_min_size,
                )
                res._from_cache = True
                return res
            except (json.JSONDecodeError, KeyError):
                pass

    own: dict = {}      # path -> own_size
    nfiles_map: dict = {}
    mtime_map: dict = {}
    errors: list = []
    reparse: list = []
    big_files: list = []   # [(path, size, mtime)]
    files_total = 0
    dirs_total = 0
    stack = [root]
    t0 = time.time()

    while stack:
        d = stack.pop()
        if progress and dirs_total % 200 == 0:
            try:
                progress(d, files_total)
            except Exception:
                pass
        s = 0
        subdirs = []
        latest = 0.0
        try:
            with os.scandir(d) as it:
                for e in it:
                    p = e.path
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if is_reparse(p):
                                reparse.append(p)
                                continue
                            subdirs.append(p)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            sz = st.st_size
                            s += sz
                            files_total += 1
                            if st.st_mtime > latest:
                                latest = st.st_mtime
                            if sz >= collect_min_size:
                                big_files.append((p, sz, st.st_mtime))
                        # symlink 文件：跳过（不计入，避免双计）
                        elif e.is_symlink():
                            reparse.append(p)
                    except OSError:
                        pass
            stack.extend(subdirs)
            dirs_total += 1 + len(subdirs)
        except OSError as ex:
            errors.append(f"{d}: {ex}")
        own[d] = s
        nfiles_map[d] = files_total
        mtime_map[d] = latest

    # 自底向上聚合
    aggregated = dict(own)
    for d in sorted(aggregated, key=lambda p: -p.count(os.sep)):
        parent = os.path.dirname(d)
        if parent and parent in aggregated:
            aggregated[parent] += aggregated[d]

    res = ScanResult(
        root=root,
        elapsed=time.time() - t0,
        total_size=aggregated.get(root, sum(own.values())),
        nfiles=files_total,
        ndirs=dirs_total,
        errors=errors,
        reparse_skipped=reparse,
        stats=own,
        aggregated=aggregated,
        files=big_files,
        collect_min_size=collect_min_size,
        files_complete=True,
    )

    # 写缓存
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({
                "root": res.root, "elapsed": res.elapsed,
                "total_size": res.total_size, "nfiles": res.nfiles,
                "ndirs": res.ndirs, "errors": errors[:50],
                "aggregated": {k: v for k, v in aggregated.items()},
            }, f, ensure_ascii=False)
    except OSError:
        pass

    res._from_cache = False
    return res


def _fast_walk_dir(target: str, collect_min_size: int = 0,
                    big_files: Optional[list] = None) -> tuple[int, int, int, float]:
    """使用 os.scandir 快速遍历目录（避免 os.walk 逐文件 stat 的系统调用损耗，提速 6 倍）。
    返回 (total_size, nfiles, ndirs, latest_mtime)。
    """
    s = 0
    files_count = 0
    dirs_count = 0
    latest = 0.0
    stack = [target]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        p = e.path
                        if e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            sz = st.st_size
                            s += sz
                            files_count += 1
                            if st.st_mtime > latest:
                                latest = st.st_mtime
                            if big_files is not None and sz >= collect_min_size:
                                big_files.append((p, sz, st.st_mtime))
                        elif e.is_dir(follow_symlinks=False):
                            if not is_reparse(p):
                                stack.append(p)
                                dirs_count += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return s, files_count, dirs_count, latest


def targeted_scan(root: str, rules=None, progress=None,
                  collect_min_size: int = 1024) -> ScanResult:
    """靶向快速扫描：只针对规则已知的前缀与重点缓存目录定向探测，秒级完成。

    相比从盘根全盘深度遍历（百万文件遍历需数分钟），靶向扫描仅解析
    与当前盘相关的规则前缀目录（如 Temp、缓存、下载残留等），
    对绝大多数常规清理场景提速 10~20 倍。
    """
    import glob
    from .rules import ALL_RULES, rules_for_drive, drive_of, expand_placeholders, norm

    t0 = time.time()
    root = os.path.abspath(root)
    drv = drive_of(root)
    if rules is None:
        rules = rules_for_drive(drv) if drv else ALL_RULES

    # 1. 解析所有规则的靶向目录模式
    target_patterns = set()
    for r in rules:
        p = norm(expand_placeholders(r.pattern))
        if drv and p.startswith("?:/"):
            p = f"{drv}:/" + p[4:]
        if p.startswith("**"):
            continue
        if "/**" in p:
            p = p[:p.index("/**")]
        if not r.match_dir:
            p = os.path.dirname(p)
        target_patterns.add((p, r.list_children_only))

    # 2. 定位磁盘上实际存在的具体根目录
    found_targets = []
    root_norm = os.path.normcase(root.rstrip("\\/"))
    for pat, children_only in target_patterns:
        for resolved in glob.glob(pat):
            if os.path.exists(resolved):
                res_norm = os.path.normpath(resolved)
                if os.path.normcase(res_norm).startswith(root_norm):
                    found_targets.append((res_norm, children_only))

    own: dict = {}
    big_files: list = []
    files_total = 0
    dirs_total = 0
    errors: list = []

    # 3. 靶向遍历（采用 scandir 高速遍历）
    for idx, (target, children_only) in enumerate(found_targets):
        if progress and idx % 5 == 0:
            try:
                progress(target, files_total)
            except Exception:
                pass

        if os.path.isfile(target):
            try:
                sz = os.path.getsize(target)
                own[target] = sz
                files_total += 1
                if sz >= collect_min_size:
                    big_files.append((target, sz, os.path.getmtime(target)))
            except OSError as ex:
                errors.append(f"{target}: {ex}")
        elif not children_only:
            try:
                s, fc, dc, _ = _fast_walk_dir(target, collect_min_size, big_files)
                own[target] = s
                files_total += fc
                dirs_total += dc
            except OSError as ex:
                errors.append(f"{target}: {ex}")
        else:
            try:
                with os.scandir(target) as it:
                    for e in it:
                        p = e.path
                        try:
                            if e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                own[p] = st.st_size
                                files_total += 1
                                if st.st_size >= collect_min_size:
                                    big_files.append((p, st.st_size, st.st_mtime))
                            elif e.is_dir(follow_symlinks=False):
                                if is_reparse(p):
                                    continue
                                s, fc, dc, _ = _fast_walk_dir(p)
                                own[p] = s
                                files_total += fc
                                dirs_total += dc + 1
                        except OSError:
                            pass
            except OSError as ex:
                errors.append(f"{target}: {ex}")

    # 自底向上聚合
    aggregated = dict(own)
    for d in sorted(aggregated, key=lambda p: -p.count(os.sep)):
        parent = os.path.dirname(d)
        if parent and parent in aggregated:
            aggregated[parent] += aggregated[d]

    return ScanResult(
        root=root,
        elapsed=time.time() - t0,
        total_size=aggregated.get(root, sum(own.values())),
        nfiles=files_total,
        ndirs=dirs_total,
        errors=errors,
        reparse_skipped=[],
        stats=own,
        aggregated=aggregated,
        files=big_files,
        collect_min_size=collect_min_size,
        files_complete=True,
    )


def top_dirs(res: ScanResult, n: int = 60, min_size: int = 50 * 2**20,
             max_depth_below_root: Optional[int] = None) -> list:
    """返回最大的 n 个目录（含子树大小），用于报告。"""
    root_depth = res.root.rstrip(os.sep).count(os.sep)
    items = []
    for p, size in res.aggregated.items():
        if size < min_size:
            continue
        if p == res.root:
            continue
        if max_depth_below_root is not None:
            depth = p.rstrip(os.sep).count(os.sep) - root_depth
            if depth > max_depth_below_root:
                continue
        items.append((size, p))
    items.sort(reverse=True)
    return items[:n]


def top_files(root: str, n: int = 80, min_size: int = 20 * 2**20) -> list:
    """返回最大的 n 个文件。"""
    files = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if is_reparse(e.path):
                                continue
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            sz = e.stat(follow_symlinks=False).st_size
                            if sz >= min_size:
                                files.append((sz, e.path))
                    except OSError:
                        pass
        except OSError:
            pass
    files.sort(reverse=True)
    return files[:n]


def human(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PB"
