# -*- coding: utf-8 -*-
"""清理执行器：安全删除。

安全机制（学习自 oh-my-disk-cleaner / MangoDisk safety-first）：
1. dry-run 预览：默认只报告不删除
2. 回收站删除：默认走回收站（可反悔），--purge 才永久删
3. 文件锁检测：正在使用的文件跳过
4. 路径白名单护栏：绝不碰系统关键目录（Windows/System32 等）
5. 操作日志：每次删除记录到 JSON，可审计
"""
from __future__ import annotations
import os
import re
import sys
import time
import json
import stat
import shutil
from dataclasses import dataclass, field
from typing import Optional

from .analyzer import Candidate
from .scanner import human

# ---------- 回收站支持（Windows 用 winshell/send2trash，缺失则降级） ----------
_SEND2TRASH = None
try:
    from send2trash import send2trash as _SEND2TRASH  # type: ignore
except ImportError:
    pass

# ---------- 护栏：绝不删除的路径 ----------
# 子树级：这些前缀下的一切都不碰（精确前缀匹配）
PROTECTED_PREFIXES = [
    "c:/windows/system32",
    "c:/windows/syswow64",
    "c:/windows/assembly",
    "c:/windows/servicing",
    "c:/windows/winsxs",
    "c:/windows/boot",
    "c:/program files/windows defender",
    "c:/program files (x86)/windows defender",
    "c:/boot",
]

# 动态护栏（不写死盘符/用户名）：
_PROGFILES_RE = re.compile(r"^[a-z]:/program files(\s\(x86\))?(/|$)")
# Python 运行时/应用内置环境：清它们的内部文件等于破坏程序本身
_RUNTIME_RES = [
    re.compile(r"^[a-z]:/users/[^/]+/appdata/roaming/uv/(python|tools)(/|$)"),
    re.compile(r"^[a-z]:/users/[^/]+/appdata/local/python(/|$)"),
    re.compile(r"^[a-z]:/users/[^/]+/\.workbuddy(/|$)"),
]
# "目录根本身"拒绝（相等匹配，不影响其子项）：用户主目录、AppData 三根、
# 系统目录根等。防止 --paths 手滑把整个 User Profile 送走。
_SELF_RES = [
    re.compile(r"^[a-z]:/users/[^/]+$"),
    re.compile(r"^[a-z]:/users/[^/]+/appdata$"),
    re.compile(r"^[a-z]:/users/[^/]+/appdata/local$"),
    re.compile(r"^[a-z]:/users/[^/]+/appdata/roaming$"),
    re.compile(r"^[a-z]:/users/[^/]+/appdata/localow$"),
    re.compile(r"^[a-z]:/users/[^/]+/\.cache$"),
    re.compile(r"^[a-z]:/windows$"),
    re.compile(r"^[a-z]:/programdata$"),
    re.compile(r"^[a-z]:/program files$"),
    re.compile(r"^[a-z]:/program files \(x86\)$"),
]


def _norm_lower(p: str) -> str:
    return p.replace("\\", "/").lower().rstrip("/")


def is_protected(path: str) -> bool:
    np = path.replace("\\", "/").lower()
    # 盘符根目录本身（c:/、c:、f:/ 等）
    if re.match(r"^[a-z]:/?$", np):
        return True
    np = np.rstrip("/")
    for pre in PROTECTED_PREFIXES:
        if np == pre or np.startswith(pre + "/"):
            return True
    if _PROGFILES_RE.match(np):
        return True
    for rx in _RUNTIME_RES:
        if rx.match(np):
            return True
    for rx in _SELF_RES:
        if rx.match(np):
            return True
    return False


@dataclass
class CleanResult:
    path: str
    action: str          # deleted / moved_to_trash / skipped / failed
    size: int = 0
    reason: str = ""


@dataclass
class CleanReport:
    dry_run: bool
    results: list = field(default_factory=list)
    total_freed: int = 0
    physical_freed: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    log_file: str = ""
    disk_deltas: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = []
        mode = "DRY-RUN（预览，未实际删除）" if self.dry_run else "已执行"
        lines.append(f"=== 清理报告 [{mode}] ===")
        deleted = [r for r in self.results if r.action in ("deleted", "moved_to_trash")]
        skipped = [r for r in self.results if r.action == "skipped"]
        failed = [r for r in self.results if r.action == "failed"]
        for r in deleted:
            lines.append(f"  [{'回收站' if r.action=='moved_to_trash' else '删除'}] {human(r.size):>10}  {r.path}")
        for r in skipped:
            lines.append(f"  [跳过] {human(r.size):>10}  {r.path}  ({r.reason})")
        for r in failed:
            lines.append(f"  [失败] {human(r.size):>10}  {r.path}  ({r.reason})")
        lines.append(f"--- 逻辑释放: {human(self.total_freed)} | 跳过: {self.total_skipped} | 失败: {self.total_failed} ---")
        if not self.dry_run and self.disk_deltas:
            for drv, d in self.disk_deltas.items():
                lines.append(f"  [{drv} 盘物理增加] {human(d['delta'])} (可用空间: {human(d['before'])} -> {human(d['after'])})")
        return "\n".join(lines)


def _quarantine_dir() -> str:
    """隔离目录：send2trash 不可用时，删除降级为移入此处（可手动恢复）。"""
    base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "diskcleaner", "quarantine")
    os.makedirs(base, exist_ok=True)
    return base


def _quarantine(path: str) -> str:
    """把文件/目录移入隔离区，返回新路径。同盘用 rename（秒级），跨盘用 move。"""
    qd = _quarantine_dir()
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r'[<>:"|?*]', "_", os.path.basename(path.rstrip("/\\")))
    dest = os.path.join(qd, f"{ts}-{safe_name}")
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(qd, f"{ts}-{safe_name}.{n}")
        n += 1
    try:
        os.rename(path, dest)
    except OSError:
        shutil.move(path, dest)
    return dest


def _is_locked(path: str) -> bool:
    """检测文件是否被占用（尝试以独占写方式打开）。"""
    if os.path.isdir(path):
        return False
    try:
        with open(path, "a"):
            pass
        return False
    except (PermissionError, OSError):
        return True


def _on_rm_error(func, p, exc_info):
    """rmtree 错误回调：尝试去只读属性后重试。"""
    try:
        os.chmod(p, stat.S_IWRITE)
        func(p)
    except Exception:
        raise


# ---------- 运行时保护：绝不删除正在运行的进程自身所在目录 ----------
# 场景：清理 C:\Users\*\AppData\Local\Temp 时，本工具/其他 agent 的临时工作目录
# （如 hermes_kernel_*）也在 Temp 下，必须跳过，否则会把自己跑崩。
RUNTIME_TOKENS = ("hermes_kernel", "diskcleaner-quarantine",
                  "_mei", "pyinstaller", "xiaoyutoolbox")


def _norm_slash(p: str) -> str:
    return p.replace("\\", "/").lower().rstrip("/")


def _in_runtime_tree(path: str) -> bool:
    """path 是否落在"当前进程运行时子树"内（自身或其后代），是则跳过。"""
    np = _norm_slash(path)
    bases = []
    try:
        bases.append(_norm_slash(sys.executable))
    except Exception:
        pass
    try:
        bases.append(_norm_slash(os.getcwd()))
    except Exception:
        pass
    for b in bases:
        if b and (np == b or np.startswith(b + "/")):
            return True
    for tok in RUNTIME_TOKENS:
        if tok in np:
            return True
    return False


def _dir_size(path: str) -> int:
    """递归累加目录内文件大小（采用 scandir 避免逐文件二次 stat 的系统调用损耗）。"""
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                        elif e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _remove_one_file(path: str, use_trash: bool) -> None:
    """删除单个文件：回收站 or 永久。失败抛异常由调用方计数。"""
    if use_trash and _SEND2TRASH:
        _SEND2TRASH(path)
    else:
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)


def _send2trash_batch(items: list) -> tuple:
    """批量移入回收站，失败二分重试。

    items: [(path, size), ...]
    Returns: (freed_bytes, failed_items)

    一次 SHFileOperation 送一批（比逐文件快 1-2 个数量级）；
    批内混有占用文件时整批失败 → 二分缩小范围，O(k·log n) 定位。
    """
    if not items:
        return 0, []
    if len(items) > 500:
        # 超大批分块，避免单次调用过大
        freed = 0
        failed = []
        for i in range(0, len(items), 500):
            f, fl = _send2trash_batch(items[i:i + 500])
            freed += f
            failed.extend(fl)
        return freed, failed
    try:
        _SEND2TRASH([p for p, _ in items])
        return sum(s for _, s in items), []
    except Exception:
        if len(items) == 1:
            p, s = items[0]
            # SHFileOperation 可能部分成功（先移走了无锁项才在占用项上抛异常），
            # 文件已不在 = 实际已进回收站，找回计数
            if not os.path.exists(p):
                return s, []
            return 0, items
        mid = len(items) // 2
        f1, fa = _send2trash_batch(items[:mid])
        f2, fb = _send2trash_batch(items[mid:])
        return f1 + f2, fa + fb


def _remove_tree(path: str, size_hint: int, use_trash: bool,
                 _depth: int = 0) -> tuple:
    """把文件/目录移入回收站（或永久删），**遇占用项跳过而非整体失败**。

    策略：先尝试整体移动（最快，同盘 rename）；整体失败（通常因目录内有
    正在使用的文件）则逐项降级——遍历顶层条目，能整体移动的子目录整体移动，
    含占用文件的再往下递归，占用/受保护项跳过。

    Returns: (ok, freed_bytes, moved_count, failed_count, note)
    """
    # 运行时保护：正在运行的进程子树，整体跳过
    if _in_runtime_tree(path):
        return False, 0, 0, 1, "运行时保护(进程占用)，跳过"

    # 单文件
    if not os.path.isdir(path):
        try:
            sz = size_hint or os.path.getsize(path)
        except OSError:
            sz = 0
        try:
            _remove_one_file(path, use_trash)
            return True, sz, 1, 0, ""
        except Exception as ex:
            return False, 0, 0, 1, f"占用/权限: {ex.__class__.__name__}"

    # 目录整体尝试：仅回收站模式（send2trash 对目录是原子 rename，
    # 要么整体进回收站要么完全不动，失败后逐项降级不会漏计）。
    # purge 模式跳过整体尝试——rmtree 失败前可能已删一半，计数会失真，
    # 直接逐项删（每个文件删前 stat，计数精确）。
    if use_trash and _SEND2TRASH:
        try:
            _SEND2TRASH(path)
            return True, (size_hint or 0), 1, 0, ""
        except Exception:
            pass

    # 整体失败 → 逐项降级。深度护栏防循环。
    if _depth >= 16:
        return False, 0, 0, 1, "层级过深，跳过"
    try:
        entries = list(os.scandir(path))
    except OSError as ex:
        return False, 0, 0, 1, f"无法读取目录: {ex.__class__.__name__}"

    freed = moved = failed = 0
    subdirs = []
    file_items = []          # [(path, size)] 待批量回收站
    for e in entries:
        p = e.path
        if _in_runtime_tree(p):
            failed += 1
            continue
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = os.path.isdir(p)
        if is_dir:
            subdirs.append(p)
        else:
            try:
                sz = e.stat(follow_symlinks=False).st_size
            except OSError:
                sz = 0
            file_items.append((p, sz))

    # --- 子目录：先整体试（一次调用覆盖整棵子树，最快），失败才递归 ---
    for p in subdirs:
        sub_size = _dir_size(p) if (use_trash and _SEND2TRASH) else 0
        done_whole = False
        if use_trash and _SEND2TRASH:
            try:
                _SEND2TRASH(p)
                freed += sub_size
                moved += 1
                done_whole = True
            except Exception:
                done_whole = False
        if not done_whole:
            ok, f, m, fl, _ = _remove_tree(p, sub_size, use_trash, _depth + 1)
            freed += f; moved += m; failed += fl

    # --- 文件：批量送回收站（二分定位占用项）或逐个永久删 ---
    if use_trash and _SEND2TRASH:
        f, fl = _send2trash_batch(file_items)
        freed += f
        moved += len(file_items) - len(fl)
        failed += len(fl)
    else:
        for p, sz in file_items:
            try:
                _remove_one_file(p, use_trash)
                freed += sz; moved += 1
            except Exception:
                failed += 1

    # 尽量删掉腾空后的外壳目录（自底向上，递归时已处理子层）
    try:
        os.rmdir(path)
    except OSError:
        pass
    note = f"整目录移动失败→逐项降级: 移动{moved}项, 跳过{failed}项(占用/权限)"
    return moved > 0, freed, moved, failed, note


def _rmtree_whole(path: str) -> None:
    """永久删除整目录（去只读重试）。保留给外部调用；_remove_tree 不再用它
    做整体尝试（部分失败会破坏计数）。"""
    shutil.rmtree(path, onerror=_on_rm_error)


def recycle_bin_size(drives: list = None) -> int:
    """各盘 $Recycle.Bin 的实际占用（字节）。

    用途：回收站模式的"释放 X"是逻辑值——文件还在回收站里占着磁盘，
    清空回收站后才真正释放。报告里同时给这个数字，避免"清了 20GB
    磁盘没变多"的困惑。只统计顶层遍历，不做深度聚合。
    """
    total = 0
    if not drives:
        import string
        drives = [f"{d}:\\" for d in string.ascii_uppercase
                  if os.path.exists(f"{d}:\\")]
    for d in drives:
        rb = os.path.join(d, "$Recycle.Bin")
        if not os.path.isdir(rb):
            continue
        for root, _dirs, files in os.walk(rb):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    return total


def empty_recycle_bin(drive: Optional[str] = None) -> bool:
    """静默、非阻塞清空回收站（免弹窗、免卡死）。

    使用 Windows 原生 Win32 API SHEmptyRecycleBinW，
    传入 SHERB_NOCONFIRMATION (1) | SHERB_NOPROGRESSUI (2) | SHERB_NOSOUND (4) = 7。
    """
    if os.name != "nt":
        return False
    import ctypes
    try:
        root = f"{drive[0].upper()}:\\" if drive and drive[0].isalpha() else None
        res = ctypes.windll.shell32.SHEmptyRecycleBinW(None, root, 7)
        return res == 0
    except Exception:
        return False


def clean(candidates: list, dry_run: bool = True, use_trash: bool = True,
          log_dir: Optional[str] = None, progress=None) -> CleanReport:
    """执行清理。

    Args:
        candidates: Candidate 列表（只处理 selected=True 的；组候选自动展开）
        dry_run: True=只预览不删
        use_trash: True=移入回收站（可恢复），False=永久删除
        log_dir: 日志目录
        progress: 回调 progress(done, total, current_path)

    性能要点：文件项批量送回收站（500 个/批，SHFileOperation 一次一批）。
    早期实现逐文件调用，20 万项的 Temp 组要数小时；批量后是分钟级。
    目录项保留逐个处理（整树移动失败要逐项降级）。
    """
    report = CleanReport(dry_run=dry_run)

    # 展开组候选为工作项 (path, size, is_dir)
    todo: list = []
    for c in candidates:
        if not c.selected:
            continue
        if getattr(c, "is_group", False) and getattr(c, "group_paths", None):
            for item in c.group_paths:
                todo.append((item[0], item[1], bool(item[2])))
        else:
            todo.append((c.path, c.size, c.is_dir))
    total = len(todo)
    done_count = 0

    drives_involved = set()
    for p, _, _ in todo:
        if len(p) >= 2 and p[1] == ":":
            drives_involved.add(p[:2].upper() + "\\")
    before_free = {}
    if not dry_run:
        for d in drives_involved:
            try:
                before_free[d] = shutil.disk_usage(d).free
            except Exception:
                pass

    def tick(n=1, path=""):
        nonlocal done_count
        done_count += n
        if progress:
            try:
                progress(done_count, total, path)
            except Exception:
                pass

    # ---- Pass 1：护栏检查 + dry-run 判定 ----
    # 存在性检查并行 stat：大组（如 20 万文件的 Temp）在 HDD 上串行 stat
    # 要几十分钟，且收集阶段不上报进度，UI 表现为"准备中 [0/0] 长时间
    # 不动"（实测事故）。并行 8 线程 + 逐项 tick 修复。
    exec_dirs: list = []    # [(path, size)] 目录：逐个 _remove_tree
    exec_files: list = []   # [(path, size)] 文件：批量送回收站
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        exists_flags = list(pool.map(os.path.exists, [t[0] for t in todo]))
    for (path, size, is_dir), ex_flag in zip(todo, exists_flags):
        # 护栏 1：受保护路径
        if is_protected(path):
            report.results.append(CleanResult(path, "skipped", size, "系统保护路径"))
            report.total_skipped += 1
            tick(1, path)
            continue
        # 护栏 2：路径不存在
        if not ex_flag:
            report.results.append(CleanResult(path, "skipped", size, "路径不存在"))
            report.total_skipped += 1
            tick(1, path)
            continue
        if dry_run:
            action = "moved_to_trash" if use_trash else "deleted"
            report.results.append(CleanResult(path, action, size, "dry-run"))
            report.total_freed += size
            tick(1, path)
            continue
        if is_dir:
            exec_dirs.append((path, size))
            tick(1, path)
        else:
            if use_trash and _SEND2TRASH:
                exec_files.append((path, size))
                tick(1, path)
            elif use_trash and not _SEND2TRASH:
                # 降级：移入隔离目录（绝不静默永久删）
                try:
                    dest = _quarantine(path)
                    report.results.append(
                        CleanResult(path, "moved_to_trash", size,
                                    f"send2trash 不可用，已移入隔离区: {dest}"))
                    report.total_freed += size
                except Exception as ex:
                    report.results.append(CleanResult(path, "failed", size, str(ex)))
                    report.total_failed += 1
                tick(1, path)
            else:
                # purge 模式：文件逐个删（os.remove 本身快，无需批量）
                if _is_locked(path):
                    report.results.append(CleanResult(path, "skipped", size, "文件正在使用"))
                    report.total_skipped += 1
                else:
                    try:
                        _remove_one_file(path, False)
                        report.results.append(CleanResult(path, "deleted", size, ""))
                        report.total_freed += size
                    except Exception as ex:
                        report.results.append(CleanResult(path, "failed", size, str(ex)))
                        report.total_failed += 1
                tick(1, path)

    # ---- Pass 2a：目录项逐个（整树移动失败自动逐项降级）----
    for path, size in exec_dirs:
        # 护栏 3：文件锁对目录项无意义（send2trash 目录原子失败再降级）
        try:
            ok, freed, moved, failed, note = _remove_tree(path, size, use_trash)
            action = "moved_to_trash" if use_trash else "deleted"
            if ok:
                report.results.append(CleanResult(path, action, freed, note))
                report.total_freed += freed
                report.total_failed += failed
            elif note.startswith("运行时保护") or note.startswith("无法读取目录"):
                report.results.append(CleanResult(path, "skipped", size, note))
                report.total_skipped += 1
            else:
                report.results.append(CleanResult(path, "failed", size,
                                                  note or "删除失败"))
                report.total_failed += 1
        except Exception as ex:
            report.results.append(CleanResult(path, "failed", size, str(ex)))
            report.total_failed += 1
        tick(1, path)

    # ---- Pass 2b：文件项并发批量送回收站 ----
    # send2trash 批量接口快于逐项 ~10 倍；再叠加多线程分片并发。
    # 注意：HDD 上 ~160 文件/s 是磁盘物理极限（每文件 2-3 次 IO），
    # 并发主要在 SSD 上有收益；HDD 上 20 万文件就是 20 分钟量级。
    if exec_files and use_trash and _SEND2TRASH:
        BATCH = 500
        chunks = [exec_files[i:i + BATCH] for i in range(0, len(exec_files), BATCH)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for chunk, (freed, failed) in zip(chunks, pool.map(_send2trash_batch, chunks)):
                failed_set = {p for p, _ in failed}
                for p, s in chunk:
                    if p in failed_set:
                        report.results.append(CleanResult(p, "failed", s,
                                                          "移入回收站失败（占用/权限）"))
                        report.total_failed += 1
                    else:
                        report.results.append(CleanResult(p, "moved_to_trash", s, ""))
                        report.total_freed += s
                tick(len(chunk), chunk[0][0])

    # 计算物理磁盘可用空间增量
    if not dry_run and before_free:
        total_phys = 0
        for d, b_free in before_free.items():
            try:
                a_free = shutil.disk_usage(d).free
                delta = max(0, a_free - b_free)
                report.disk_deltas[d] = {
                    "before": b_free,
                    "after": a_free,
                    "delta": delta,
                }
                total_phys += delta
            except Exception:
                pass
        report.physical_freed = total_phys

    # 写日志
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        log_file = os.path.join(log_dir, f"clean-{ts}{'-dryrun' if dry_run else ''}.json")
        try:
            results = report.results
            truncated = len(results) > 2000
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump({
                    "time": ts, "dry_run": dry_run, "use_trash": use_trash,
                    "total_freed": report.total_freed,
                    "total_freed_h": human(report.total_freed),
                    "physical_freed": report.physical_freed,
                    "physical_freed_h": human(report.physical_freed),
                    "disk_deltas": report.disk_deltas,
                    "results_truncated": truncated,
                    "results": [r.__dict__ for r in results[:2000]],
                }, f, ensure_ascii=False, indent=2)
            report.log_file = log_file
        except OSError:
            pass

    return report
