# -*- coding: utf-8 -*-
"""框架无关的 HTTP API 层：扫描状态机 + 路由分发。

两个入口共用这一层（避免逻辑漂移）：
- server.py            独立 Web UI（http.server 壳）
- 小宇工具箱 stats_server  桌面工具箱的常驻服务（见 toolbox/INTEGRATION.md）

调用方只负责 HTTP 壳：解析请求 → dispatch(method, path, body) →
按返回的 (handled, status, payload) 发 JSON；未处理的路径由调用方
继续自己的路由/静态文件逻辑。

安全设计（不可回退）：
- clean 只接受显式 paths 列表（无"按风险全清"接口）
- 一律回收站，无永久删除入口
- 候选默认全不选，勾选 = 用户确认
"""
from __future__ import annotations
import os
import re
import threading
import traceback

from .scanner import scan, targeted_scan, top_dirs, human
from .analyzer import analyze, summarize
from .cleaner import clean, recycle_bin_size, empty_recycle_bin
from .rules import ALL_RULES, CATEGORY_NAMES, RISK_NAMES
from . import perm

LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "diskcleaner", "logs")

# 全局状态（单用户本地工具，简单起见用全局；UI 服务是单实例）
STATE = {
    "roots": [],           # 本次扫描的盘/目录列表
    "scan_result": None,   # 单盘时为 ScanResult；多盘时为 None（用 drive_meta）
    "candidates": [],
    "drive_meta": [],      # 各盘扫描元信息
    "scanning": False,
    "scan_progress": "",
    "last_report": None,
    "locked_stats": {"count": 0, "size": 0},  # 非管理员被滤掉的 needs_admin 统计
    "cleaning": {"running": False, "done": 0, "total": 0, "current": ""},
    "error": None,
}
LOCK = threading.Lock()
# 清理任务互斥：同一时刻只允许一个清理在跑（扫描可以和清理并行）
CLEAN_ACTIVE = threading.Semaphore(1)


def _clean_worker(selected: list, candidate_paths: list, dry_run: bool):
    """后台清理线程：dry-run 或实际删除，完成后写 last_report。

    预览也走后台：20 万项的护栏检查（并行 stat）要几十秒，
    同步会卡住 HTTP 请求，UI 表现为"预览没反应"。
    """
    def prog(done, total, current):
        with LOCK:
            STATE["cleaning"] = {"running": True, "done": done,
                                 "total": total, "current": current[:90]}

    try:
        report = clean(selected, dry_run=dry_run, use_trash=True,
                       log_dir=LOG_DIR, progress=prog)
        rep = {
            "dry_run": dry_run,
            "total_freed": report.total_freed,
            "total_freed_h": human(report.total_freed),
            "physical_freed": report.physical_freed,
            "physical_freed_h": human(report.physical_freed),
            "disk_deltas": {
                d: {
                    "before": v["before"],
                    "before_h": human(v["before"]),
                    "after": v["after"],
                    "after_h": human(v["after"]),
                    "delta": v["delta"],
                    "delta_h": human(v["delta"]),
                }
                for d, v in report.disk_deltas.items()
            },
            "total_failed": report.total_failed,
            "total_skipped": report.total_skipped,
            # 回收站模式的"释放"是逻辑值，文件仍在 $Recycle.Bin 占地
            "recycle_bin_size": recycle_bin_size(STATE["roots"]),
            "recycle_bin_size_h": human(recycle_bin_size(STATE["roots"])),
            "log_file": report.log_file,
            "results": [r.__dict__ for r in report.results[:2000]],
            "results_truncated": len(report.results) > 2000,
            "cleaned_candidates": [] if dry_run else candidate_paths,
        }
        with LOCK:
            STATE["last_report"] = rep
    except Exception as ex:
        with LOCK:
            STATE["last_report"] = {"error": f"清理异常: {ex}",
                                    "traceback": traceback.format_exc()}
    finally:
        # 释放任务锁（start_clean_async 里 acquire）。泄漏后果：
        # 任务完成后所有新提交永远报"已有清理任务在进行"（实测事故）
        CLEAN_ACTIVE.release()
        with LOCK:
            STATE["cleaning"] = {"running": False, "done": 0, "total": 0, "current": ""}


def start_clean_async(selected: list, candidate_paths: list,
                      dry_run: bool) -> tuple:
    """启动后台清理/预览。返回 (ok, payload)。"""
    if not CLEAN_ACTIVE.acquire(blocking=False):
        return False, {"error": "已有清理任务在进行"}
    try:
        with LOCK:
            STATE["cleaning"] = {"running": True, "done": 0,
                                 "total": 0, "current": "准备中..."}
        t = threading.Thread(target=_clean_worker,
                             args=(selected, candidate_paths, dry_run),
                             daemon=True)
        t.start()
        return True, {"ok": True, "submitted": len(candidate_paths),
                      "dry_run": dry_run}
    except Exception:
        CLEAN_ACTIVE.release()
        raise


def _scan_worker(roots: list, use_cache: bool = False, deep: bool = False):
    """后台扫描线程（支持多盘，默认靶向快速扫描，deep=True 全盘遍历）。"""
    with LOCK:
        STATE["scanning"] = True
        STATE["error"] = None
        STATE["locked_stats"] = {"count": 0, "size": 0}
        mode_str = "全盘深扫" if deep else "靶向快扫"
        STATE["scan_progress"] = f"开始{mode_str} {', '.join(roots)} ..."
    is_admin = perm.is_admin()
    try:
        from .winapp2 import get_winapp2_rules
        extra_rules = get_winapp2_rules()
        active_rules = ALL_RULES + extra_rules if extra_rules else ALL_RULES
        all_cands, meta, locked_all = [], [], {"count": 0, "size": 0}

        def prog(d, n):
            with LOCK:
                STATE["scan_progress"] = f"{n} 文件 | {d[:70]}"

        for i, root in enumerate(roots):
            with LOCK:
                if len(roots) > 1:
                    STATE["scan_progress"] = f"({i+1}/{len(roots)}) 扫描 {root} ..."
                else:
                    STATE["scan_progress"] = "扫描中..."
            if not os.path.isdir(root):
                continue
            is_drive_root = bool(re.match(r"^[a-zA-Z]:[\\/]?$", root.strip()))
            if is_drive_root and not deep:
                res = targeted_scan(root, rules=active_rules, progress=prog)
            else:
                res = scan(root, use_cache=use_cache, progress=prog)
            with LOCK:
                STATE["scan_progress"] = "分析候选..."
            locked = {"count": 0, "size": 0}
            cands = analyze(res, rules=active_rules, include_files=True, admin=is_admin,
                            locked_stats=locked)
            locked_all["count"] += locked["count"]
            locked_all["size"] += locked["size"]
            summ = summarize(cands)
            drv = root[:1].upper()
            for c in cands:
                c.title = f"[{drv}] {c.title}" if len(roots) > 1 else c.title
            all_cands.extend(cands)
            meta.append({"drive": drv, "root": res.root,
                         "total": res.total_size, "total_h": human(res.total_size),
                         "nfiles": res.nfiles, "elapsed": round(res.elapsed, 1),
                         "cand_count": summ["count"], "cand_size": summ["total_size"],
                         "cand_size_h": summ["total_size_h"],
                         "selected_h": summ["total_selected_h"],
                         "errors": res.errors[:10]})
        risk_order = {"safe": 0, "review": 1, "caution": 2}
        all_cands.sort(key=lambda c: (risk_order.get(c.risk, 1), -c.size))
        with LOCK:
            STATE["scan_result"] = None
            STATE["candidates"] = all_cands
            STATE["drive_meta"] = meta
            STATE["locked_stats"] = locked_all
            STATE["scanning"] = False
            STATE["scan_progress"] = (
                f"完成: {len(meta)} 个盘, 发现候选 {len(all_cands)} 项 ({human(sum(m['cand_size'] for m in meta))})")
    except Exception as ex:
        with LOCK:
            STATE["scanning"] = False
            STATE["error"] = f"{ex}\n{traceback.format_exc()}"


def start_scan_async(roots: list, use_cache: bool = False, deep: bool = False) -> tuple:
    """启动后台扫描。返回 (ok, error_payload)。roots 已校验存在性。"""
    with LOCK:
        if STATE["scanning"]:
            return False, {"error": "已有扫描在进行"}
        STATE["roots"] = list(roots)
        STATE["scan_result"] = None
        STATE["candidates"] = []
        STATE["drive_meta"] = []
    t = threading.Thread(target=_scan_worker, args=(roots, use_cache, deep), daemon=True)
    t.start()
    return True, {"ok": True, "roots": roots}


def dispatch(method: str, path: str, body: dict) -> tuple:
    """API 路由。返回 (handled, status, payload)；handled=False 表示不归本层管。"""
    # 兼容工具箱前缀路由：/api/cleaner/xxx -> /api/xxx
    if path.startswith("/api/cleaner/"):
        path = "/api/" + path[len("/api/cleaner/"):]
    if method == "GET":
        return _dispatch_get(path)
    if method == "POST":
        return _dispatch_post(path, body or {})
    return False, 200, {}


def _dispatch_get(path: str) -> tuple:
    if path == "/api/drives":
        from .cli import list_drives
        from .winapp2 import get_winapp2_path
        return True, 200, {"drives": list_drives(),
                           "sysdrive": os.environ.get("SystemDrive", "C:"),
                           "is_admin": perm.is_admin(),
                           "has_winapp2": os.path.isfile(get_winapp2_path())}

    if path == "/api/winapp2_info":
        from .winapp2 import get_winapp2_path, get_winapp2_rules, summary
        wa_path = get_winapp2_path()
        exists = os.path.isfile(wa_path)
        sz = os.path.getsize(wa_path) if exists else 0
        rules = get_winapp2_rules() if exists else []
        return True, 200, {
            "exists": exists,
            "path": wa_path,
            "size": sz,
            "size_h": human(sz),
            "rule_count": len(rules),
            "summary": summary() if exists else "未安装 winapp2 社区规则库",
        }

    if path == "/api/status":
        with LOCK:
            return True, 200, {
                "roots": STATE["roots"],
                "scanning": STATE["scanning"],
                "scan_progress": STATE["scan_progress"],
                "error": STATE["error"],
                "has_result": bool(STATE["candidates"]) or bool(STATE["drive_meta"]) or STATE["scan_result"] is not None,
                "candidate_count": len(STATE["candidates"]),
                "cleaning": dict(STATE["cleaning"]),
            }

    if path == "/api/result":
        with LOCK:
            if not STATE["candidates"] and not STATE["drive_meta"]:
                return True, 400, {"error": "尚未扫描"}
            cands = STATE["candidates"]
            meta = STATE["drive_meta"]
            summ = summarize(cands)
            total = sum(m["total"] for m in meta)
            nfiles = sum(m["nfiles"] for m in meta)
            elapsed = sum(m["elapsed"] for m in meta)
            payload = {
                "roots": STATE["roots"],
                "total_size": total,
                "total_size_h": human(total),
                "nfiles": nfiles,
                "elapsed": round(elapsed, 1),
                "drives": meta,
                "summary": summ,
                "perm": {
                    "is_admin": perm.is_admin(),
                    "locked_count": STATE["locked_stats"].get("count", 0),
                    "locked_size": STATE["locked_stats"].get("size", 0),
                    "locked_size_h": human(STATE["locked_stats"].get("size", 0)),
                },
                "candidates": [c.to_dict() for c in cands],
            }
            res = STATE["scan_result"]
            if res is not None:
                payload["top_dirs"] = [[human(s), p] for s, p in
                                       top_dirs(res, n=30, min_size=100 * 2**20)]
            return True, 200, payload

    if path == "/api/rules":
        rules = []
        for r in ALL_RULES:
            rules.append({
                "id": r.id, "category": r.category,
                "category_name": CATEGORY_NAMES.get(r.category, r.category),
                "risk": r.risk, "risk_name": RISK_NAMES.get(r.risk, r.risk),
                "title": r.title, "pattern": r.pattern,
                "description": r.description,
                "needs_admin": r.needs_admin,
            })
        return True, 200, {"rules": rules, "count": len(rules)}

    if path == "/api/report":
        with LOCK:
            rep = STATE["last_report"]
        return True, 200, rep or {"error": "尚无清理报告"}

    return False, 200, {}


def _dispatch_post(path: str, body: dict) -> tuple:
    if path == "/api/scan":
        # 兼容两种入参：{roots: [...]} 多盘 / {root: "F:\\"} 单盘
        roots_in = body.get("roots") or []
        if not roots_in and body.get("root"):
            roots_in = [body["root"]]
        roots = []
        for r in roots_in:
            r = str(r).strip()
            if not r:
                continue
            roots.append(r)
        # 去重 + 存在性校验
        from .cli import _normalize_root
        uniq, seen = [], set()
        for r in roots:
            r = _normalize_root(r)
            k = os.path.normcase(os.path.abspath(r))
            if k in seen:
                continue
            seen.add(k)
            if not os.path.isdir(r):
                return True, 400, {"error": f"目录不存在: {r}"}
            uniq.append(r)
        if not uniq:
            return True, 400, {"error": "未指定扫描盘/目录"}
        deep = bool(body.get("deep", False))
        ok, payload = start_scan_async(uniq, deep=deep)
        return True, (200 if ok else 409), payload

    if path == "/api/empty_recycle_bin":
        drive = body.get("drive")
        ok = empty_recycle_bin(drive)
        with LOCK:
            rb_sz = recycle_bin_size(STATE.get("roots"))
        return True, 200, {
            "ok": ok,
            "recycle_bin_size": rb_sz,
            "recycle_bin_size_h": human(rb_sz)
        }

    if path == "/api/select":
        # body: {paths: [...], selected: bool} 或 {rule_ids: [...], selected: bool}
        paths = set(body.get("paths", []))
        rule_ids = set(body.get("rule_ids", []))
        selected = bool(body.get("selected", True))
        with LOCK:
            changed = 0
            for c in STATE["candidates"]:
                if (c.path in paths) or (rule_ids and c.rule_id in rule_ids):
                    c.selected = selected
                    changed += 1
        return True, 200, {"ok": True, "changed": changed}

    if path == "/api/clean":
        # 安全设计：Web/工具箱入口一律走回收站，且只清显式传入的 paths。
        # 不提供 purge、不提供"按 rule/risk 批量清理"。
        # 预览和执行一律转后台任务（20 万项的护栏检查/删除都要分钟级，
        # 同步会卡住 HTTP 请求，配合 ThreadingHTTPServer 页面才不整体卡死），
        # UI 轮询 /api/status 的 cleaning 进度，完成后取 /api/report。
        dry_run = bool(body.get("dry_run", True))
        paths = body.get("paths")
        with LOCK:
            cands = list(STATE["candidates"])
            if paths is None:
                selected = [c for c in cands if c.selected]  # 兼容旧 UI 勾选模式
            else:
                # 显式 paths = 选中语义：勾选状态以请求为准，
                # 不看候选对象上的 selected 标志（可能未同步）
                sel = set(paths)
                selected = [c for c in cands if c.path in sel]
                for c in selected:
                    c.selected = True
        if not selected:
            return True, 400, {"error": "没有选中任何项"}

        candidate_paths = [c.path for c in selected]
        ok, payload = start_clean_async(selected, candidate_paths, dry_run)
        if not ok:
            return True, 409, payload
        return True, 200, payload

    if path == "/api/elevate":
        ok = perm.elevate_restart()
        return True, 200, {"ok": ok}

    if path == "/api/update_winapp2":
        from .winapp2 import download_winapp2
        ok, msg = download_winapp2()
        return True, (200 if ok else 500), {"ok": ok, "message": msg}

    return False, 200, {}
