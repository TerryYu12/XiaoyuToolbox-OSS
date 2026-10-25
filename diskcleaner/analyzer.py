# -*- coding: utf-8 -*-
"""分析器：扫描结果 × 规则 → 可清理候选清单。

关键设计：
- 目录级匹配优先（整目录命中规则就不再列子项，避免重复计数）
- "保留最新 N 个" 策略（针对 dist/*.exe、outputs/*.apk 这类）
- 按风险+类别+大小排序，供 UI 勾选
"""
from __future__ import annotations
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .scanner import ScanResult, human, is_reparse
from .rules import (ALL_RULES, Rule, classify, matches_any, norm,
                    CATEGORY_NAMES, RISK_NAMES, rules_for_drive, drive_of,
                    in_protected_zone, RuleIndex)
from . import perm


@dataclass
class Candidate:
    path: str
    size: int
    is_dir: bool
    mtime: float = 0.0
    rule_id: str = ""
    category: str = "other"
    risk: str = "review"
    title: str = ""
    description: str = ""
    # "保留最新"策略相关
    keep_group: str = ""       # 同组内保留最新
    keep_latest: int = 0       # 保留几个
    selected: bool = False     # UI 默认勾选状态
    # 合并分组（小项收纳）
    is_group: bool = False     # True = 代表一组同规则小项
    group_count: int = 0       # 组内项数
    group_paths: list = field(default_factory=list)
    # 智能画像与归属溯源 (Profiler)
    owner_app: str = ""
    cache_type: str = ""
    purpose: str = ""
    recreate_cost: str = ""
    advice: str = ""
    insight: str = ""
    active_refs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["size_h"] = human(self.size)
        d["mtime_h"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime)) if self.mtime else ""
        d["category_name"] = CATEGORY_NAMES.get(self.category, self.category)
        d["risk_name"] = RISK_NAMES.get(self.risk, self.risk)
        return d


# 哪些规则用"保留最新 N 个"策略（glob 形如 dir/*.ext）
KEEP_LATEST_RULES = {
    "music-dist-old": ("f:/ai flies/hermes work/163 music/dist", 2),
    "xy-old-apk": ("f:/ai flies/hermes work/xy reader/outputs", 2),
    "electron-dist-old": (None, 2),  # None=按所在目录分组
}


def _dir_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def analyze(res: ScanResult, rules=None, include_files: bool = True,
            min_size: int = 1024, merge_small: bool = True,
            merge_threshold: int = 32 * 1024 * 1024,
            admin: bool = None,
            locked_stats: Optional[dict] = None) -> list:
    """生成候选清单。

    Args:
        res: 扫描结果
        rules: 规则列表（默认全部）
        include_files: 是否扫描单文件规则（如 *.pyc、*.exe）
        min_size: 最小大小过滤（字节）
        merge_small: 把同规则的小项（< merge_threshold）合并成组，避免 UI 海量行
        merge_threshold: 小于此大小的同规则项合并（默认 32MB）
        admin: 是否管理员权限。None=自动检测。非管理员时 needs_admin 规则
               的候选直接不进清单（避免整批 WinError 5 删除失败）。
        locked_stats: 可选 dict，非管理员被滤掉的 needs_admin 候选统计
               写入 {"count": n, "size": bytes}，供 UI 提示"提权可多清多少"。
    """
    rules = rules or ALL_RULES
    # 按扫描根的盘符筛规则：扫 D 盘时排除 c:/windows、f:/temp 等绑定其他盘的规则
    scan_drive = drive_of(res.root)
    if scan_drive:
        rules = rules_for_drive(scan_drive, rules)
    if admin is None:
        admin = perm.is_admin()
    candidates: dict = {}   # path -> Candidate（去重用）
    risk_order = {"safe": 0, "review": 1, "caution": 2}
    now = time.time()
    # 前缀索引：规则数多（winapp2 接入后数千条）时避免逐条扫描
    index = RuleIndex(rules)

    def _rule_allowed(r: Rule) -> bool:
        return admin or not r.needs_admin

    def _note_locked(size: int):
        if locked_stats is not None:
            locked_stats["count"] = locked_stats.get("count", 0) + 1
            locked_stats["size"] = locked_stats.get("size", 0) + size

    # 1. 目录级匹配
    needs_mtime = any(r.min_age_days > 0 for r in rules)  # 只有时效规则才需要 mtime
    for path, size in res.aggregated.items():
        if path == res.root:
            continue
        if size < min_size:
            continue
        mt = _dir_mtime(path) if needs_mtime else 0.0
        matched = index.match(norm(path), True, mt, now)
        if not matched:
            continue
        # 保护区：区域内只放行显式豁免（allow_in_protected）的规则
        if in_protected_zone(path):
            matched = [r for r in matched if r.allow_in_protected]
            if not matched:
                continue
        # 权限：needs_admin 规则在非管理员环境下滤除并记录统计
        if not admin:
            locked = [r for r in matched if r.needs_admin]
            if locked:
                _note_locked(size)
            matched = [r for r in matched if _rule_allowed(r)]
            if not matched:
                continue
        # 取风险最高的规则（safe < review < caution）
        matched.sort(key=lambda r: -risk_order.get(r.risk, 1))
        r = matched[0]
        # 缓存根目录自身不列为候选（整根移动易因占用整体失败），只列其内部子项
        if r.list_children_only:
            continue
        c = Candidate(
            path=path, size=size, is_dir=True, mtime=mt,
            rule_id=r.id, category=r.category, risk=r.risk,
            title=r.title, description=r.description,
        )
        # 去重：如果父目录已作为候选，子目录不再单列（除非子目录规则风险更低/更具体）
        candidates[path] = c

    # 目录候选的 norm 集合（供文件级匹配做 O(深度) 祖先检查）
    dirset_norm = {norm(p) for p, c in candidates.items() if c.is_dir}

    # 2. 文件级匹配（*.pyc、*.exe、*.apk、thumbcache_*.db 等）
    if include_files:
        # 优先复用扫描时顺带收集的文件清单（省去二次走树）；
        # 缓存加载的结果（files_complete=False）回退到实时遍历
        if getattr(res, "files_complete", False) and res.collect_min_size <= min_size:
            file_iter = iter(res.files)
        else:
            file_iter = _iter_files(res.root)
        for path, size, mt in file_iter:
            if size < min_size:
                continue
            matched = index.match(norm(path), False, mt, now)
            if not matched:
                continue
            # 保护区：区域内只放行显式豁免的规则
            if in_protected_zone(path):
                matched = [r for r in matched if r.allow_in_protected]
                if not matched:
                    continue
            if not admin:
                locked = [r for r in matched if r.needs_admin]
                if locked:
                    _note_locked(size)
                matched = [r for r in matched if _rule_allowed(r)]
                if not matched:
                    continue
            matched.sort(key=lambda r: -risk_order.get(r.risk, 1))
            r = matched[0]
            # 如果该文件已被某个目录候选覆盖（整目录删），跳过单列
            if dirset_norm and _ancestor_in_set(norm(path), dirset_norm):
                continue
            c = Candidate(
                path=path, size=size, is_dir=False, mtime=mt,
                rule_id=r.id, category=r.category, risk=r.risk,
                title=r.title, description=r.description,
            )
            candidates[path] = c

    # 3. 应用"保留最新 N 个"策略
    _apply_keep_latest(candidates)

    # 4. 去掉被父候选覆盖的子候选（避免重复计数）
    _dedupe_nested(candidates)

    # 4b. 合并同规则的小项（避免 UI 里几千行 __pycache__/.pyc）
    if merge_small:
        candidates = _merge_small(candidates, merge_threshold)

    # 5. 排序：风险升序(safe先) → 大小降序
    risk_order = {"safe": 0, "review": 1, "caution": 2}
    result = sorted(candidates.values(),
                    key=lambda c: (risk_order.get(c.risk, 1), -c.size))

    # 6. 默认全部不勾选：清单是给用户审阅的，勾选 = 确认。
    # （教训：以前 safe 默认全选，一键执行就把全盘 __pycache__ 连同
    #   Program Files / Python 运行时里的一起清了）
    for c in result:
        c.selected = False

    # 7. 智能画像与归属溯源丰富
    try:
        from .profiler import enrich_candidates
        enrich_candidates(result)
    except Exception:
        pass

    return result


def _is_ancestor(parent: str, child: str) -> bool:
    """child 是否在 parent 之下（不含相等）。"""
    p = norm(parent).rstrip("/") + "/"
    c = norm(child)
    return c.startswith(p) and c != parent.rstrip("/")


def _ancestor_in_set(np: str, dirset: set) -> bool:
    """np（已 norm 的路径）是否有任一严格祖先在 dirset 中。O(深度)，替代 O(n) 的逐个 _is_ancestor。"""
    parts = np.split("/")
    # parts 形如 ['f:', 'a', 'b']；从最长祖先往下查到盘符
    for i in range(len(parts) - 1, 0, -1):
        if "/".join(parts[:i]) in dirset:
            return True
    return False


def _iter_files(root: str):
    """遍历产出 (path, size, mtime)，跳过 reparse。"""
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
                            st = e.stat(follow_symlinks=False)
                            yield e.path, st.st_size, st.st_mtime
                    except OSError:
                        pass
        except OSError:
            pass


def _apply_keep_latest(candidates: dict):
    """对"保留最新 N 个"规则：同组文件按 mtime 排序，标记保留项。"""
    for rule_id, (group_dir, keep_n) in KEEP_LATEST_RULES.items():
        # 收集该规则的所有文件候选
        group: dict = {}   # group_key -> [Candidate]
        for c in candidates.values():
            if c.rule_id != rule_id or c.is_dir:
                continue
            if group_dir:
                gk = norm(group_dir)
            else:
                gk = norm(os.path.dirname(c.path))
            group.setdefault(gk, []).append(c)
        # 每组保留最新 keep_n 个，其余标记为可删
        for gk, items in group.items():
            items.sort(key=lambda c: -c.mtime)
            for i, c in enumerate(items):
                c.keep_group = gk
                c.keep_latest = keep_n
                if i < keep_n:
                    # 保留项：从候选移除（不展示为可删）
                    candidates.pop(c.path, None)


def _dedupe_nested(candidates: dict):
    """去掉被父候选完全覆盖的子候选。O(n·depth)：沿路径向上查祖先集合。"""
    keys = list(candidates.keys())
    normed = {k: norm(k) for k in keys}
    all_norm = set(normed.values())
    to_remove = []
    for k in keys:
        np = normed[k]
        parts = np.split("/")
        # 检查是否存在严格祖先也在候选集中
        for i in range(len(parts) - 1, 0, -1):
            if "/".join(parts[:i]) in all_norm:
                to_remove.append(k)
                break
    for p in to_remove:
        candidates.pop(p, None)


def _merge_small(candidates: dict, threshold: int) -> dict:
    """同规则的小项（< threshold）合并成一个组候选。

    - 组候选 path 取组内最大项的路径（保持唯一性），is_group=True，
      group_paths 记录全部真实路径（clean 时逐个处理）
    - 大项（>= threshold）保持独立，方便用户单独审阅
    - 每组至少 5 项才值得合并，否则保持原样
    """
    groups: dict = {}   # rule_id -> [Candidate]
    out: dict = {}
    for k, c in candidates.items():
        if c.size < threshold:
            groups.setdefault(c.rule_id, []).append(c)
        else:
            out[k] = c
    for rid, items in groups.items():
        if len(items) < 5:
            for c in items:
                out[c.path] = c
            continue
        items.sort(key=lambda c: -c.size)
        rep_c = items[0]
        g = Candidate(
            path=rep_c.path, size=sum(c.size for c in items),
            is_dir=rep_c.is_dir, mtime=rep_c.mtime,
            rule_id=rid, category=rep_c.category, risk=rep_c.risk,
            title=rep_c.title,  # 标题保持干净，组数由展示层用 group_count 渲染
            description=(rep_c.description + " " if rep_c.description else "") +
                        f"（{len(items)} 个同类小项合并，清理时全部处理）",
            is_group=True, group_count=len(items),
            group_paths=[[c.path, c.size, c.is_dir] for c in items],
        )
        out[g.path] = g
    return out


def summarize(candidates: list) -> dict:
    """按类别+风险汇总，供 UI 展示。"""
    by_cat: dict = {}
    by_risk: dict = {}
    total = 0
    total_selected = 0
    for c in candidates:
        by_cat.setdefault(c.category, []).append(c)
        by_risk.setdefault(c.risk, []).append(c)
        total += c.size
        if c.selected:
            total_selected += c.size
    return {
        "total_size": total,
        "total_size_h": human(total),
        "total_selected": total_selected,
        "total_selected_h": human(total_selected),
        "count": len(candidates),
        "by_category": {
            CATEGORY_NAMES.get(k, k): {
                "count": len(v),
                "size": sum(c.size for c in v),
                "size_h": human(sum(c.size for c in v)),
            } for k, v in by_cat.items()
        },
        "by_risk": {
            RISK_NAMES.get(k, k): {
                "count": len(v),
                "size": sum(c.size for c in v),
                "size_h": human(sum(c.size for c in v)),
            } for k, v in by_risk.items()
        },
    }
